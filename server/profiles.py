"""文档类型档案的用例层（DD-13.2 的 ``profiles/*``；FR-M2-23~27）。

三件事，三个落点
----------------
====================  ===========================================================
① 登记校验            ``pipeline.profiles.validate_profile``（**转发**，不自实现）
② 档案本体            ``<storage_root>/profiles/<profile_id>/<version>/profile.json``
③ 登记元数据          ``doc_type_profiles`` 表（**仓储**，供列表 / 筛选 / 废弃）
====================  ===========================================================

为什么本体在磁盘、元数据在仓储
------------------------------
与模板包同一个理由（见 :mod:`server.templates`）：档案要能**整包导出给第三方**
（FR-M2-25），磁盘上的 ``profile.json`` 才是原件；而"按文档类型筛、按版本列、
标记废弃"是库的本分。把整份档案也序列化进库会出现第二份契约 JSON，
与磁盘那份必然漂移——"生成期只查契约"要的正是一份真相。

领域模型从哪里来：**库**，不是项目配置
--------------------------------------
第 1、3 条校验（``for_each`` 引用的领域类型是否已登记、槽位单元类型是否越界）需要
``DomainModelRegistry``，而第 1 条按 FR-M2-23 要在**登记期**就拒绝。

登记期没有"项目"这个概念——``doc_type_profiles`` 的主键是 ``(profile_id, version)``，
``POST /profiles`` 也不带 ``project_id``。所以领域模型不能挂在 ``ProjectConfig`` 上
（那样这条校验永远做不了），只能和模板包、档案一样**进库**：见
:mod:`server.domain_models` 与需求 4.9.2 的"三层外部数据正交"。

于是第 1、3 条在生产路径下**真跑**：``domain_models`` 由 HTTP 层从库里读好后以
**纯参**传入（与 ``known_template_refs`` 同一姿态——本层不认识仓储）。空库传的是
**空注册表**而不是 ``None``，第 1 条会如实失败，这正是 FR-M2-23 要的。

做不了的校验仍然必须说出来
--------------------------
``skipped`` 的语义没有变，只是**生产路径下它应当是空的**：空 = "全部查过了"。
它仍非空的情形只有一种——调用方拿不到库（脚本、单测、将来的 SDK）。那时结论会
如实记进登记回执并回给界面（硬规则 11：缺能力跳过必须进 summary，不得算作通过），
而**不是**悄悄跳过、也不是伪造成"已校验"。

档案没有"文档内版本号"可解析
---------------------------
模板包可以从原件里解析出版本号，档案不行——它是人写的一份 JSON。故 ``version``
必须由调用方给出；缺省取 ``"1.0"``，要让新版本覆盖旧的就得**显式**给出新版本号
（同 ``template_id@version`` 的口径：版本号是配置与历史产物的引用键）。

档案包为什么不在这个文件里
--------------------------
FR-M2-25 的**打包与解包**（验收 45）在 :mod:`server.profile_package`：那边只管字节与
磁盘，不认识仓储、不认识登记。本模块保留"把包内那份档案登记进去"这最后一步
（:meth:`ProfileMixin.import_profile_package`），因为登记要的是这里已有的那份判据。

直接动因是 NFR-09 的单文件行数上限（两条路径加上判定实现一次把本文件推到 1100 行以上），
但**分界线取自职责而不是行数**：真按行数切，会切出"前半段函数在 A、后半段在 B"这种
必须在两个文件间跳着读的东西。分完之后两边都只依赖 :mod:`server.templates` 与
:mod:`server.storage` **一个方向**（档案包模块 import 本模块的常量，反之不成立），
所以 :meth:`export_profile` / :meth:`import_profile_package` 这两个"要同时认识两边"
的入口留在这里，而不需要任何跨 mixin 的调用。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.pipeline import (
    domain_registry,
    fingerprint_profile,
    path_segment,
    validate_profile,
)
from docfidelity.platform.fs import atomic_write_text, ensure_dir

from .profile_package import (
    APPLICABILITY_FILENAME,
    PACKAGE_LIMITS,
    PACKAGE_NOTES_FILENAME,
    PROFILE_PACKAGE_FORMAT,
    SAMPLE_INPUT_FILENAME,
    TEMPLATE_BUNDLE_DIRNAME,
    BundledTemplate,
    build_profile_package,
    import_bundled_templates,
    read_profile_package,
)
from .storage import RegisteredProfile
from .templates import CONFLICT_POLICIES

if TYPE_CHECKING:
    from .settings import ServerSettings
    from .storage import ProfileRepository, Repository

#: 包相关的常量与两个回执类型**从 :mod:`server.profile_package` 转出**，不在本模块
#: 另立一份：那边定义、这边引用。列进 ``__all__`` 而非另起转发常量，是为了让
#: "它来自档案包模块"这件事从导入行就能看出来（同 ``server.templates`` 转出
#: ``read_overrides`` 的处置）。
__all__ = [
    "APPLICABILITY_FILENAME",
    "BundledTemplate",
    "DEFAULT_PROFILE_VERSION",
    "PACKAGE_LIMITS",
    "PACKAGE_NOTES_FILENAME",
    "PROFILE_FILENAME",
    "PROFILE_PACKAGE_FORMAT",
    "PROFILE_POLICIES",
    "ProfileMixin",
    "ProfilePackageImport",
    "ProfileRegistration",
    "SAMPLE_INPUT_FILENAME",
    "TEMPLATE_BUNDLE_DIRNAME",
    "read_profile_document",
    "resolve_profile",
]

#: 档案目录内的文件名。与 ``registry`` 的 ``manifest.json`` 同一个角色：
#: 它是"这份档案是什么"的唯一原件，别的东西（指纹、废弃标记）都是它的派生。
PROFILE_FILENAME: Final = "profile.json"

#: 省缺版本号。档案没有文档内版本号可解析（见模块文档），故给一个常量起点；
#: 它不是"自动升版"——要新版本必须显式传 ``version``。
DEFAULT_PROFILE_VERSION: Final = "1.0"

#: 冲突处置策略。与模板登记**同一份闭集**（``server.templates``）：两种动作的语义
#: （保留旧版并报冲突 / 覆盖）与"没有自动改版本号这一种"的理由完全一致，
#: 各写一份迟早会出现"两个端点对同一件事接受不同的词"。
PROFILE_POLICIES: Final[frozenset[str]] = CONFLICT_POLICIES


@dataclass(frozen=True)
class ProfileRegistration:
    """一次档案登记的回执（P04 / P03 的行内操作结果）。"""

    profile_id: str
    version: str
    ref: str
    doc_type: str
    title: str
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    #: 是否**覆盖**了一次已有登记（同 ``profile_id@version`` 换了内容）。
    replaced: bool = False
    #: 本次**做不了**的校验（空 = 全部都做了）。界面必须显示，见模块文档。
    #: 生产路径下应为空；非空只意味着"调用方拿不到库"。
    skipped: list[str] = field(default_factory=list)
    #: 非致命的结构性问题（如槽位单元类型越界）。登记**不顺带修复**，只如实回报。
    #: 第 1、3 条跑起来之后它才会非空——见 :meth:`ProfileMixin.register_profile`。
    findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProfilePackageImport:
    """一次档案包导入的回执（DD-13.2 的 ``POST /profiles/import``）。"""

    #: 包内档案本体走的是与 ``POST /profiles`` **同一条**登记路径，故回执里带的是
    #: 那份登记回执本身，而不是把它的字段抄一遍——抄一遍就会有两份"校验做了哪些"
    #: 的说法（``skipped`` / ``findings`` 各一份，迟早不一致）。
    registration: ProfileRegistration
    #: 随包登记进来的模板包，按包内目录名排序（稳定，便于界面与快照比对）。
    templates: list[BundledTemplate]
    #: 收件方**没要**的东西：包里带了、但这份档案并没有引用的模板包。如实列出而不是
    #: 静默忽略——多出来的东西要么是发件方包打错了，要么是两边版本对不上，
    #: 两种都要人看一眼。
    unused_templates: list[str] = field(default_factory=list)


def read_profile_document(entry: RegisteredProfile) -> DocTypeProfile:
    """读回一份已登记档案的本体（磁盘上的 ``profile.json``）。

    提成**模块级**函数而不是留在 mixin 里：模板库那条"哪些档案引用了这个包"的反向查询
    （``server.template_library``）也要读档案本体，而它是另一个 mixin。让它去调
    ``ProfileMixin._read_document`` 会打破"每个 mixin 只用宿主字段"的边界；
    把读取本身拿出来，两个 mixin 就都只是这个函数的调用方。

    ``entry.profile_dir`` 是登记时写下的**绝对路径**，不在这里重新拼（拼法只有
    ``ProfileMixin._profile_dir`` 一处，硬约束 14 的校验也在那里）。
    """
    path = Path(entry.profile_dir) / PROFILE_FILENAME
    if not path.is_file():
        raise EngineError(
            ErrorCode.PROFILE_INVALID,
            f"档案本体缺失：{entry.ref}",
            where={"profile_id": entry.profile_id, "version": entry.version, "path": str(path)},
            remediation=[
                "档案目录被外部清理过；重新登记该版本",
                "本库目录不受保留策略管辖，故出现此状态意味着人手改动过存储根",
            ],
        )
    return DocTypeProfile.model_validate_json(path.read_text(encoding="utf-8"))


def resolve_profile(repository: ProfileRepository, ref: str) -> RegisteredProfile | None:
    """把配置里的档案引用解析成**具体一版**；解析不出返回 ``None``。

    两种写法都认，且**带版本的那个优先**
    ------------------------------------
    需求里 P08 的文档族清单把这一栏写作 ``profile_id@version``（"决定体例与投影
    规则"），FR-M2-24 也要求"按 ``profile_id@version`` 取用"。而契约字段
    ``DocumentEntry.profile_id`` 是存量配置里的裸标识。两种都认的结果是：新配置能
    精确锁定版本，存量配置仍然能构建。

    裸标识落到哪一版：**最新登记的、未废弃的那一版**（``list_profiles`` 已按
    ``(profile_id, registered_at, version)`` 升序）。这是一个**新规则**，写在
    这里而不是各调用点各推一遍：构建期拿哪一版，是"这份产物由哪份档案产出"
    （FR-M2-26 的可追溯性）的前提，两个调用点口径不同就会出现"报告说 A 版、
    产物按 B 版做"。

    **全部版本都已废弃**时返回 ``None``：废弃就是"别再用了"，拿最新那版废弃的
    继续构建等于让废弃标记失效。调用方（冻结输入快照）据此如实报缺项。
    """
    if "@" in ref:
        profile_id, _, version = ref.partition("@")
        if profile_id and version:
            return repository.find_profile(profile_id, version)
    entries = [entry for entry in repository.list_profiles(profile_id=ref) if not entry.deprecated]
    return entries[-1] if entries else None


class ProfileMixin:
    """档案库（``Services`` 的第四个 mixin，见 ``server.services``）。

    与另外三个 mixin 一样，它**只**用 ``self.repository`` / ``self.settings`` / ``self.now``。
    """

    if TYPE_CHECKING:
        repository: Repository
        settings: ServerSettings
        now: Callable[[], datetime]

    # ------------------------------------------------------------ 路径与读取
    def _profile_dir(self, profile_id: str, version: str) -> Path:
        """档案目录，**不创建**。两个片段当路径用之前**先校验**（硬约束 14）。"""
        root = self.settings.profiles_root
        return (
            root
            / path_segment(profile_id, field="profile_id")
            / path_segment(version, field="version")
        )

    def _find_profile(self, profile_id: str, version: str) -> RegisteredProfile:
        entry = self.repository.find_profile(profile_id, version)
        if entry is None:
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到档案版本：{profile_id}@{version}",
                where={"profile_id": profile_id, "version": version},
                remediation=[
                    "按 GET /api/v1/profiles 列出已登记的档案与版本",
                    "或确认「档案标识 @ 版本」的拼写",
                ],
            )
        return entry

    def profile_document(self, profile_id: str, version: str | None = None) -> DocTypeProfile:
        """读回档案本体（磁盘上的 ``profile.json``）。

        ``version`` 为空取**最新一版**（按 ``registered_at``）：界面上"打开这份档案"
        想看的就是当前有效的那一版。
        """
        entry = self._latest_or(self._versions_of(profile_id), version, profile_id)
        return self._read_document(entry)

    def _read_document(self, entry: RegisteredProfile) -> DocTypeProfile:
        return read_profile_document(entry)

    def _versions_of(self, profile_id: str) -> list[RegisteredProfile]:
        return self.repository.list_profiles(profile_id=profile_id)

    @staticmethod
    def _latest_or(
        entries: Sequence[RegisteredProfile], version: str | None, profile_id: str
    ) -> RegisteredProfile:
        if version is not None:
            for entry in entries:
                if entry.version == version:
                    return entry
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到档案版本：{profile_id}@{version}",
                where={"profile_id": profile_id, "version": version},
                remediation=["列出该档案的全部版本后重试"],
            )
        if not entries:
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到档案：{profile_id}",
                where={"profile_id": profile_id},
                remediation=["按 GET /api/v1/profiles 列出已登记的档案"],
            )
        # 列表按 ``(profile_id, registered_at, version)`` 升序，故末项即最新一版；
        # 同刻登记时以 ``version`` 为平局键——与仓储的排序口径同一份（见 protocols）。
        return entries[-1]

    # ---------------------------------------------------------------- 登记
    def register_profile(
        self,
        *,
        document: DocTypeProfile,
        version: str = DEFAULT_PROFILE_VERSION,
        author: str | None = None,
        on_conflict: str = "abort",
        known_template_refs: Sequence[str] | None = None,
        domain_models: Sequence[DomainModel] = (),
    ) -> ProfileRegistration:
        """登记一个档案版本（DD-13.2 的 ``POST /profiles``）。

        ``known_template_refs`` 与 ``domain_models`` 都由 HTTP 层从库里读好后传入，
        而不是在这里现查：校验函数（``pipeline.profiles``）刻意不认识仓储，
        "谁去读库"这件事只有一处——与 ``precheck`` 的同名口径一致
        （硬规则 22 的"入参全是纯数据"）。

        ``domain_models`` 缺省是**空序列**而不是 ``None``，这个差别是刻意的：
        它表示"库是空的"（第 1 条如实失败，FR-M2-23 要的正是这个），而不是
        "这次没能力查"（那才走 ``registry=None`` 的 ``skipped`` 路径）。
        调用方若把"忘了传"与"库里没有"混成同一个默认值，就会让一次漏传表现为
        "校验通过"——那是最坏的一种失败（硬规则 11）。

        校验**不通过即拒绝**（``E_PROFILE_TEMPLATE_MISSING`` / ``E_DOMAIN_KIND_UNKNOWN``）：
        放行等于让投影安静地返回空集合，最终表现为"文档少了一整章而报告全绿"。

        回执里的 ``findings`` 与 ``skipped`` **不是一回事**：

        * ``skipped`` 是"**没查**"。生产路径下（注册表与模板库都给全）它应当是**空**的；
          非空只意味着一件事——调用方拿不到库；
        * ``findings`` 是"查了、结论是小毛病"（如槽位声明的单元类型越界）。它现在
          **真的会非空**：第 1、3 条跑起来之后，档案自身的自相矛盾才第一次可见。
        """
        check_conflict_policy(on_conflict)
        profile_id = document.profile_id.strip()
        if profile_id == "":
            raise EngineError(
                ErrorCode.PROFILE_INVALID,
                "档案缺少 profile_id",
                where={"doc_type": document.doc_type},
                remediation=[
                    "在档案里填上 profile_id（它是配置 DocumentEntry.profile_id 的引用键）"
                ],
            )
        profile_id = path_segment(profile_id, field="profile_id")
        version = path_segment((version or "").strip() or DEFAULT_PROFILE_VERSION, field="version")

        check = validate_profile(
            document,
            registry=domain_registry(domain_models),
            known_template_refs=known_template_refs,
        )
        for info in check.error_infos():
            raise EngineError(
                info.code, info.message, where=info.where, remediation=info.remediation
            )

        fingerprint = fingerprint_profile(document)
        stored = document.model_copy(update={"fingerprint": fingerprint})
        existing = self.repository.find_profile(profile_id, version)
        moment = self.now()
        replaced = False
        if existing is not None:
            same = existing.fingerprint == fingerprint.digest
            if not same and on_conflict != "overwrite":
                raise EngineError(
                    ErrorCode.VERSION_CONFLICT,
                    f"档案已登记过且内容不同：{existing.ref}",
                    where={
                        "profile_ref": existing.ref,
                        "existing_fingerprint": existing.fingerprint,
                        "incoming_fingerprint": fingerprint.digest,
                        "registered_at": existing.registered_at.isoformat(),
                    },
                    remediation=[
                        "换一个版本号后重新提交（档案版本是配置与历史产物的引用键）",
                        "或确认覆盖：旧版本可能已被既有文档引用",
                    ],
                )
            replaced = not same

        directory = self._profile_dir(profile_id, version)
        ensure_dir(directory)
        target = directory / PROFILE_FILENAME
        # 原子写：档案本体是"生成期只查契约"的那一份真相，读到半截文件比读不到更糟。
        atomic_write_text(target, stored.model_dump_json(indent=2))

        entry = RegisteredProfile(
            profile_id=profile_id,
            version=version,
            doc_type=document.doc_type,
            title=document.title,
            profile_dir=str(directory),
            fingerprint=fingerprint.digest,
            registered_at=moment,
            registered_by=author,
            # ``overwrite`` 是"重新提交"的正常路径：把上一次的废弃标记一并清掉，
            # 否则"覆盖一个已废弃版本"会得到一份**不可用**的新档案（界面按废弃过滤）。
            deprecated=False,
            deprecated_reason=None,
        )
        self.repository.add_profile(entry)
        return ProfileRegistration(
            profile_id=profile_id,
            version=version,
            ref=entry.ref,
            doc_type=document.doc_type,
            title=document.title,
            fingerprint=fingerprint.digest,
            registered_at=moment,
            registered_by=author,
            replaced=replaced,
            skipped=list(check.skipped),
            findings=[finding.message for finding in check.findings],
        )

    # ---------------------------------------------------------------- 列表
    def list_profiles(
        self,
        *,
        profile_id: str | None = None,
        doc_type: str | None = None,
        include_deprecated: bool = True,
    ) -> list[RegisteredProfile]:
        entries = self.repository.list_profiles(profile_id=profile_id, doc_type=doc_type)
        if include_deprecated:
            return entries
        return [entry for entry in entries if not entry.deprecated]

    def get_profile(self, profile_id: str, version: str | None = None) -> RegisteredProfile:
        return self._latest_or(self._versions_of(profile_id), version, profile_id)

    # ---------------------------------------------------------------- 废弃
    def deprecate_profile(
        self,
        profile_id: str,
        version: str,
        *,
        reason: str,
        operator: str | None = None,
    ) -> RegisteredProfile:
        """标记一个档案版本为废弃（**不删除**）。

        只标不删：档案版本是历史产物的引用键（``DocumentEntry.profile_id``），
        删掉它等于让"这份文档是按哪份体例产出的"永久失去答案。废弃的效果是
        "列默认不再展示、发起新构建时不再可选"，而已经产出的文档照旧可追溯。

        ``reason`` 必填且不得为空白：一次没有理由的废弃在三个月后读起来与"误操作"
        无法区分（与 ``E_WAIVER_REASON_REQUIRED`` 同一口径——留痕的价值全在理由里）。
        """
        if not (reason or "").strip():
            raise EngineError(
                ErrorCode.PROFILE_INVALID,
                "废弃档案必须给出理由",
                where={"profile_id": profile_id, "version": version},
                remediation=["在请求体里给出 reason：为什么这个版本不再适用"],
            )
        entry = self._find_profile(
            path_segment(profile_id, field="profile_id"), path_segment(version, field="version")
        )
        if entry.deprecated:
            # 幂等：重复废弃不报错、也不改写原有的理由与时间——那是最早那次留痕。
            return entry
        return self.repository.save_profile(
            replace(entry, deprecated=True, deprecated_reason=reason.strip())
        )

    # ---------------------------------------------------------------- 导出
    def export_profile(self, profile_id: str, version: str | None = None) -> tuple[str, bytes]:
        """导出档案包（FR-M2-25 / 验收 45），返回 ``(建议文件名, zip 字节)``。

        包的内容、以及"为什么连模板包一起打"见
        :func:`server.profile_package.build_profile_package`；本方法只负责**取哪一版**
        （``version`` 留空 = 当前有效的那一版）并把档案本体读出来。

        引用的模板包不在库里（或已废弃、或读不回来）时**拒绝导出**，而不是打一个缺件的
        包出去：只发 ``profile.json`` 的包不是"打包交付"，而少了什么要到收件方那边才
        发现——那时"谁该补什么"已经说不清。
        """
        entry = self._latest_or(self._versions_of(profile_id), version, profile_id)
        return build_profile_package(entry, self._read_document(entry), self.repository)

    # ---------------------------------------------------------------- 导入
    def import_profile_package(
        self,
        *,
        payload: bytes,
        version: str | None = None,
        on_conflict: str = "abort",
        author: str | None = None,
        domain_models: Sequence[DomainModel] = (),
    ) -> ProfilePackageImport:
        """导入档案包（DD-13.2 的 ``POST /profiles/import``；FR-M2-25 的另一半）。

        一步做两件事，顺序是刻意的：

        1. **先登记随包带来的模板包**——档案的 ``bindings.template_packages`` 要在
           登记期校验"引用的模板在不在库里"（FR-M2-23）。顺序反过来的话，一次**正确**的
           导入会被自己刚交上来的档案判成"引用了未登记的模板"，而报错指向档案的
           ``bindings``、根因却在调用顺序上。
        2. **再登记档案本体**——走的是与 ``POST /profiles`` **同一条**路径
           （:meth:`register_profile`），故校验、冲突处置与 ``skipped`` / ``findings``
           的口径完全一致。收件方因此不能靠"换个入口"绕过任何一条校验。

        解包与全部判据在 :func:`server.profile_package.read_profile_package`——那里
        **在动手之前**把可判定性检查一次做完（"半个包不留"）；本方法只管顺序与登记。

        ``version`` 留空取包内自报的那一版（``包说明.json`` 的 ``profile_version``）；
        老包没有它，回落 ``DEFAULT_PROFILE_VERSION``，与 ``POST /profiles`` 同口径。
        """
        check_conflict_policy(on_conflict)
        prepared = read_profile_package(payload, profile_filename=PROFILE_FILENAME)
        templates = import_bundled_templates(
            prepared,
            repository=self.repository,
            settings=self.settings,
            now=self.now,
            on_conflict=on_conflict,
            author=author,
        )
        registration = self.register_profile(
            document=prepared.document,
            version=version or prepared.version or DEFAULT_PROFILE_VERSION,
            author=author,
            on_conflict=on_conflict,
            known_template_refs=[item.ref for item in self.repository.list_templates()],
            domain_models=domain_models,
        )
        return ProfilePackageImport(
            registration=registration,
            templates=templates,
            unused_templates=list(prepared.unused),
        )


def check_conflict_policy(on_conflict: str) -> None:
    """冲突处置策略必须是 :data:`PROFILE_POLICIES` 里的一个，否则拒。

    提成模块级函数而不是在两个入口各写一遍：``POST /profiles`` 与
    ``POST /profiles/import`` 收的是同一个开关，两处各写一份就会出现"换个入口报的
    错误码不一样"，而界面按码渲染可操作动作。
    """
    if on_conflict in PROFILE_POLICIES:
        return
    raise EngineError(
        ErrorCode.CONFIG_INVALID,
        f"未知的冲突处置：{on_conflict!r}",
        where={"on_conflict": on_conflict, "allowed": sorted(PROFILE_POLICIES)},
        remediation=["on_conflict 取 abort（保留旧版，报冲突）或 overwrite（覆盖）"],
    )
