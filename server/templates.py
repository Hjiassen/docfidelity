"""模板入库的用例层（P05 第 2~5 步 / DD-7.1 / DD-13.2 的 ``templates/*``）。

三件事，三个落点
----------------
====================  ===========================================================
① 导入暂存             ``<storage_root>/imports/<import_id>/``（**磁盘**）
② 登记（入模板库）      ``<storage_root>/templates/<tid>/<ver>/``（磁盘）+ 登记表（**仓储**）
③ 人的确认            包内 ``overrides.json``（随包走，生成期按 ``overrides_ref`` 取）
====================  ===========================================================

为什么导入记录在磁盘、登记在仓储
--------------------------------
导入记录与一个目录**一一对应**：原件、解析出的模板包、以及"这次解析说了什么"的
``import.json`` 是同一份东西的三面。只把记录搬进库、把包留在磁盘，就要额外维护
"库里有记录而目录被删了"这种半死状态的判据；把整份包也序列化进库，则会出现第二份
契约 JSON，而它与包目录里那份必然漂移——"生成期只查契约"这条决策要的正是一份真相。
故暂存取 ``Workspace`` 的同一个模式（``<root>/jobs/<job_id>/``）。

登记不同：它是**库**——要能按文档类型筛、按版本列、长期留存、被历史产物引用。所以
它的元数据走仓储（``storage.RegisteredTemplate``），包本体仍在磁盘。

``imports/`` 与 ``templates/`` **都不在保留策略的管辖内**（见 ``settings`` 的两个属性）：
暂存里带着"稍后重试解析"的承诺，库里存着历史产物的引用键，两者被按天数清掉都会让
已经给出的承诺失效。暂存目录的清理是独立的一轮。

解析为什么不抛异常
------------------
DD-7.1 的失败姿态是分级的，最重的一条是"``unpack`` 失败 → 仅登记原件、稍后重试"。
所以解析失败落在 :attr:`TemplateImportRecord.register_only` 上，由界面渲染成
"原件已收到、解析未完成、可以再试一次"；只有**调用方自己的**问题（上传闸门、
路径越界、版本冲突）才抛。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING, Any, Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import (
    ReviewOverride,
    TemplateOverrides,
)
from docfidelity.pipeline import (
    import_template as run_import,
)
from docfidelity.pipeline import (
    path_segment,
    read_overrides,
    register_package,
    verify_package,
    write_overrides,
)
from docfidelity.platform.fs import ensure_dir, ensure_within, iter_files, read_bytes
from docfidelity.platform.hashing import sha256_hex

from .ids import new_id
from .import_record import (
    IMPORT_RECORD_NAME,
    PACKAGE_DIRNAME,
    SOURCE_DIRNAME,
    ReviewDecision,
    TemplateImportRecord,
    TemplateRegistration,
    read_record,
    record_payload,
)
from .staging import StagingObject, plan_staging_purge, reclaimability
from .storage import RegisteredTemplate
from .uploads import UploadProbe, validate_upload, zip_members

if TYPE_CHECKING:
    from .settings import ServerSettings
    from .storage import Repository, TemplateRepository

__all__ = [
    "CONFLICT_POLICIES",
    "IMPORT_RECORD_NAME",
    "PACKAGE_DIRNAME",
    "SOURCE_DIRNAME",
    "ReviewDecision",
    "TemplateImportMixin",
    "TemplateImportRecord",
    "TemplateRegistration",
    "resolve_template",
    "same_template_content",
    "template_source_path",
    # 转出 ``pipeline`` 的读包入口：服务端读"人确认过什么"（包内 ``overrides.json``）
    # 只能经接缝，不得自己 import ``registry``。列进 ``__all__`` 而非另起转发函数，
    # 是为了让"它来自 pipeline"这件事从导入行就能看出来。
    "read_overrides",
]

#: 冲突处置策略（``POST /templates/imports/{id}/register`` 的 ``on_conflict``）。
#:
#: 只有两种，且**没有**"自动改个版本号"这一种：版本号是项目配置与历史产物的引用键，
#: 自动加后缀会让引用在一瞬间全部指向一个没人记得的版本。要"新版本"就显式给出新的
#: ``version``（那时默认的 ``abort`` 本来就不会冲突）。
CONFLICT_POLICIES: Final[frozenset[str]] = frozenset({"abort", "overwrite"})


def resolve_template(repository: TemplateRepository, ref: str) -> RegisteredTemplate | None:
    """把配置里的模板引用 ``<template_id>@<version>`` 解析成**具体一版**。

    与 ``server.profiles.resolve_profile`` 是同一件事的两份实现吗？不是——模板引用
    **永远带版本**（``DocumentEntry.template_ref`` 的契约就是 ``template_id@version``），
    所以这里没有"裸标识落到哪一版"那条规则；有那条规则的是档案（``profile_id``）。

    两处判断必须一致
    ----------------
    解析不出返回 ``None``、**已废弃的不返回**。理由与档案那边逐字相同：废弃就是
    "别再拿它产出新东西了"，让废弃标记在构建路径上失效，等于把它降级成一句备注。
    冻结输入快照据此如实报缺项，而不是产出一份引用了废弃模板的文档。

    ``ref`` 形状不对（缺 ``@``、两段有一为空）也算解析不出：拼错的引用与不存在的
    引用，对调用方的下一步动作是同一句"去看项目配置里这一栏"。
    """
    template_id, _, version = (ref or "").partition("@")
    if not (template_id and version):
        return None
    entry = repository.find_template(template_id, version)
    if entry is None or entry.deprecated:
        return None
    return entry


def template_source_path(entry: RegisteredTemplate) -> Path | None:
    """模板**原件**（``.docx``）的绝对路径；没有原件记录时返回 ``None``。

    原件不在模板包里（包里只有 ``contract/`` 那份契约），而是与它并排放在
    ``<package_dir>/source/<filename>``——两条登记路径（解析成功的、以及 DD-7.1 的
    "仅登记原件、稍后重试"）都会把原件复制到那里，故"原件在哪"只有一个答案。

    ``source_filename`` 可能为空（早期记录或人工造的行），此时如实返回 ``None``：
    渲染的第一件事就是打开原件，编不出一个文件名的话，构建应该报"缺原件"而不是
    猜一个路径去 ``open``。
    """
    if not entry.source_filename or not entry.package_dir:
        return None
    return Path(entry.package_dir) / SOURCE_DIRNAME / entry.source_filename


class TemplateImportMixin:
    """模板导入 / 裁决 / 登记（``Services`` 的第三个 mixin，见 ``server.services``）。

    与另外两个 mixin 一样，它**只**用 ``self.repository`` / ``self.settings`` / ``self.now``
    ——不持有自己的状态，"同一个 Services 对象"这件事才在三个文件之间都成立。
    依赖的宿主字段在 ``TYPE_CHECKING`` 里声明（不能声明成真属性，见 ``maintenance``
    模块的说明）。
    """

    if TYPE_CHECKING:
        repository: Repository
        settings: ServerSettings
        now: Callable[[], datetime]

    # ------------------------------------------------------------ 暂存目录
    def _import_dir(self, import_id: str) -> Path:
        """暂存目录，**不创建**。``import_id`` 当路径片段用**先校验**（硬约束 14）。

        不创建是刻意的：``get_import`` 也要经过它，若顺手把目录建出来，一次"读一个
        不存在的导入"就会在磁盘上留下一个空目录——暂存目录的清理是独立的一轮，
        而它扫出来的那些"空壳"会让人以为导入成功过。要写的地方各自 ``ensure_dir``。
        """
        return self.settings.imports_root / path_segment(import_id, field="import_id")

    def _read_import(self, import_id: str) -> TemplateImportRecord:
        path = self._import_dir(import_id) / IMPORT_RECORD_NAME
        if not path.is_file():
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到模板导入记录：{import_id}",
                where={"import_id": import_id},
                remediation=[
                    "确认标识拼写；重新导入会得到一个新的 import_id",
                    "按保留策略清理过的暂存记录无法再读取",
                ],
            )
        return read_record(json.loads(path.read_text(encoding="utf-8")))

    def _write_import(self, record: TemplateImportRecord) -> TemplateImportRecord:
        ensure_dir(self._import_dir(record.import_id)).joinpath(IMPORT_RECORD_NAME).write_text(
            json.dumps(record_payload(record), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return record

    # ------------------------------------------------------- 暂存目录的回收
    def _try_read_import(self, import_id: str) -> TemplateImportRecord | None:
        """读导入记录；读不到就返回 ``None``（**不抛**）。

        与 :meth:`_read_import` 的差别只在失败姿态，而这个差别是刻意的：那条路径是
        "人指名要看这一份"，读不到就是 ``E_NOT_FOUND``；这条是**扫目录**，一个坏掉的
        目录不该把整张清单废掉——它恰恰是最该被看见的那一项。捕获面宽是有意的：
        ``read_record`` 对残缺 JSON 会抛 ``KeyError`` / ``ValueError``，连同
        pydantic 的校验错误一起，都归成"这份记录读不出来"。
        """
        try:
            return self._read_import(import_id)
        except (EngineError, ValueError, KeyError, TypeError):
            return None

    def staging_imports(self) -> list[StagingObject]:
        """暂存根目录下的全部导入目录（**含不可回收的**，各附理由）。

        不可回收的也列出来，而不是过滤掉：界面上"这里还有一份没登记完的导入"本身就是要
        让人看见的信息（与 ``precheck`` 的 ``skipped`` 同一种姿态——**没做不等于通过**）。
        """
        root = self.settings.imports_root
        if not root.is_dir():
            return []
        items: list[StagingObject] = []
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            record = self._try_read_import(directory.name)
            registration = None if record is None else record.registration
            registered = registration is not None
            reclaimable, reason = reclaimability(readable=record is not None, registered=registered)
            # 记录读不出来时退回目录 mtime：它在界面上只是"大概什么时候来的"，
            # 而判据（能不能回收）压根不看时间——见 ``server.staging`` 的模块文档。
            created_at = (
                datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc)
                if record is None
                else record.created_at
            )
            items.append(
                StagingObject(
                    import_id=directory.name,
                    path=str(directory),
                    size_bytes=sum(path.stat().st_size for path in iter_files(directory)),
                    created_at=created_at,
                    readable=record is not None,
                    registered=registered,
                    template_ref=None if registration is None else registration.ref,
                    reclaimable=reclaimable,
                    blocked_reason=reason,
                )
            )
        return items

    def purge_staging(self, *, requested: Sequence[str]) -> tuple[StagingObject, ...]:
        """回收**已登记**的导入暂存目录。

        模板库（``templates/``）与仓储里的登记行**都不动**：回收的只是暂存那份副本，
        已登记的模板照常可用、照常被历史产物引用。
        """
        plan = plan_staging_purge(self.staging_imports(), requested=requested)
        for item in plan:
            # 路径从 ``staging_imports`` 来（自己扫出来的），仍走一次 ``ensure_within``：
            # 删目录这类动作不该只看"它是自己扫出来的"。
            rmtree(ensure_within(self.settings.imports_root, item.path), ignore_errors=True)
        return plan

    # ------------------------------------------------------------ 第 1~3 步
    def import_template(
        self,
        *,
        filename: str,
        payload: bytes,
        doc_type: str | None = None,
        template_id: str | None = None,
        version: str | None = None,
        notes: str | None = None,
        author: str | None = None,
    ) -> TemplateImportRecord:
        """收件 → 解析 → 出报告（P05 第 1~3 步，一个请求走完）。

        **四道上传闸门在这里重跑一遍**（与 ``POST /uploads`` 共用 ``validate_upload``）：
        DD-13.2 把 ``templates/imports`` 定义成 multipart 的"上传并入库"，于是这条路径
        不能假定"文件已经过闸门"。共用同一份判定，故两处不会漂移。
        """
        probe = UploadProbe(
            filename=filename,
            size=len(payload),
            head=payload[:8],
            digest=sha256_hex(payload),
            # 成员名必须真的读出来：第④道宏闸门全靠它，留空等于那道闸门不存在。
            members=zip_members(payload),
        )
        receipt = validate_upload(probe, self.settings)

        import_id = new_id("i_")
        stage_dir = ensure_dir(self._import_dir(import_id))
        # 原件另存一份到暂存目录，而不是只留上传目录里的那份：上传件受保留策略管辖
        # （默认 30 天），而"稍后重试解析"的承诺不能因为保留期到了就失效。
        source_path = ensure_dir(stage_dir / SOURCE_DIRNAME) / receipt.filename
        source_path.write_bytes(payload)

        result = run_import(
            source_path,
            import_id=import_id,
            template_id=template_id,
            version=version,
            doc_type=doc_type,
            notes=notes,
            target_dir=stage_dir / PACKAGE_DIRNAME,
        )

        record = TemplateImportRecord(
            import_id=import_id,
            created_at=result.created_at,
            source_filename=result.source_filename,
            source_size_bytes=result.source_size_bytes,
            template_id=result.template_id,
            version=result.version,
            template_id_source=result.template_id_source,
            version_source=result.version_source,
            source_hash=result.source_hash,
            doc_type=doc_type,
            notes=notes,
            author=author,
            register_only=result.register_only,
            package_dir=(
                None
                if result.package_dir is None
                else str(result.package_dir.relative_to(self.settings.storage_root))
            ),
            package_hash=result.package_hash,
            stages=result.stages,
            findings=result.findings,
            integrity=result.integrity,
            summary=result.summary,
        )
        return self._write_import(record)

    def get_import(self, import_id: str) -> TemplateImportRecord:
        return self._read_import(import_id)

    # ------------------------------------------------------ 重解析（第 3 步重跑）
    def reparse_import(self, import_id: str) -> TemplateImportRecord:
        """对暂存里的原件**再解析一次**（DD-7.1 的"仅登记原件、稍后重试"）。

        为什么可以重试
        --------------
        :meth:`import_template` 把原件**另存一份**到 ``imports/<id>/source/``，而不是只留
        上传目录里那份，正是为了这条路径：上传件受保留策略管辖，而"稍后重试解析"的承诺
        不能因为保留期到了就失效。故重解析只用暂存里的那份原件，**不接受新的上传**——
        "换个原件重来"是另一件事，它必须得到一个新的 ``import_id``，否则同一条记录的
        ``source_hash`` 会在人不知情的时候变掉，而那个摘要已经写进了登记回执。

        三条判据（先拒绝、再动手）
        --------------------------
        1. **已登记的不得重解析**（``E_VERSION_CONFLICT``）。登记之后
           ``templates/<tid>/<ver>/`` 就是不可变的历史；就地重解析会让暂存里的包与已登记
           的包悄悄分叉，而下一次登记看上去就成了一次"同版本换内容"的覆盖——那正是
           ``VERSION_CONFLICT`` 要挡的形状。要新的解析结果，就重新导入一份。
        2. **包目录必须先清空**。``write_package`` 对非空目标目录直接报
           ``E_VERSION_CONFLICT``（它**不静默覆盖**，见 ``registry.package_io``），
           所以留着上一次解析的残件会让重解析必然失败——而残件恰恰是"上一次没跑完"
           留下的，也就是最需要重试的那一种。
        3. **人给的编号/版本续用，解析出来的不续用**。这是本方法最容易做错的一处：
           传下去的 ``template_id`` / ``version`` 会被 ``detect_identity`` 记成
           ``explicit``。若把"上次从文件名里猜出来的版本"当成人给的再传一遍，重解析之后
           这条记录的来源就会**从 filename 变成 explicit**——界面据此不再提示人补编号，
           而那个编号其实从来没有人确认过。

        **留痕不动**：``decisions`` 原样保留。它是"谁在什么时候确认了什么"的历史，重解析
        不改变历史；而"还剩几项要确认"由 :meth:`TemplateImportRecord.pending` 从**新的**
        ``findings`` 算，故上一轮确认过、这一轮不再出现的项不会继续挂在清单上。
        """
        record = self._read_import(import_id)
        # 判据走 ``can_reparse``，不在这里重写 ``registration is not None``：界面置灰与
        # 服务端拒绝必须是**同一处判定**，否则会出现"按钮能点、点了报错"。
        if not record.can_reparse:
            registration = record.registration
            raise EngineError(
                ErrorCode.VERSION_CONFLICT,
                f"导入 {import_id} 已登记，不能再重解析",
                where={
                    "import_id": import_id,
                    "registered_ref": "" if registration is None else registration.ref,
                },
                remediation=[
                    "需要一个新解析结果时请重新导入一份原件（会得到新的 import_id）",
                    "确要在同一版本上换成新内容，请走登记接口的 on_conflict=overwrite",
                ],
            )

        source_path = self._import_dir(import_id) / SOURCE_DIRNAME / record.source_filename
        if not source_path.is_file():
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"暂存里找不到原件：{record.source_filename}",
                where={"import_id": import_id, "source": record.source_filename},
                remediation=[
                    "该导入的原件可能已被回收；重新导入一份",
                    "暂存记录读得到、原件不在，说明目录被外部动过",
                ],
            )

        package_dir = self._import_dir(import_id) / PACKAGE_DIRNAME
        if package_dir.is_dir():
            rmtree(package_dir, ignore_errors=True)

        result = run_import(
            source_path,
            import_id=import_id,
            template_id=record.template_id if record.template_id_source == "explicit" else None,
            version=record.version if record.version_source == "explicit" else None,
            doc_type=record.doc_type,
            notes=record.notes,
            target_dir=package_dir,
        )
        return self._write_import(
            replace(
                record,
                reparsed_at=self.now(),
                template_id=result.template_id,
                version=result.version,
                template_id_source=result.template_id_source,
                version_source=result.version_source,
                source_hash=result.source_hash,
                register_only=result.register_only,
                package_dir=(
                    None
                    if result.package_dir is None
                    else str(result.package_dir.relative_to(self.settings.storage_root))
                ),
                package_hash=result.package_hash,
                stages=result.stages,
                findings=result.findings,
                integrity=result.integrity,
                summary=result.summary,
            )
        )

    # ---------------------------------------------------------------- 第 4 步
    def review_import(
        self,
        import_id: str,
        *,
        operator: str,
        decisions: Sequence[ReviewDecision],
    ) -> TemplateImportRecord:
        """写入待确认项的处理留痕（P05 第 4 步）。

        **逐条留痕、可重复提交**：同一个 ``item_id`` 再提交一次就是追加一条，最后一条
        胜出（``TemplateOverrides.decision_of`` 同一口径）。不做"一次定死"，因为改主意
        是正常行为，而"改主意"与"当初就选这个"在审计上必须能分开看。
        """
        record = self._read_import(import_id)
        if not decisions:
            raise EngineError(
                ErrorCode.REVIEW_VALUE_REQUIRED,
                "没有提交任何待确认项",
                where={"import_id": import_id},
                remediation=["至少提交一条处理意见；不需要确认时不需要调用本接口"],
            )
        known = {item.finding_id for item in record.findings}
        moment = self.now()
        appended: list[ReviewOverride] = []
        for decision in decisions:
            if decision.item_id not in known:
                raise EngineError(
                    ErrorCode.NOT_FOUND,
                    f"待确认项不在本次导入里：{decision.item_id}",
                    where={"import_id": import_id, "item_id": decision.item_id},
                    remediation=["按本次导入报告的待确认项标识提交", "重新导入以取得新的标识"],
                )
            value = (decision.value or "").strip()
            if decision.action == "specify" and value == "":
                raise EngineError(
                    ErrorCode.REVIEW_VALUE_REQUIRED,
                    f"「指定取值」必须给出取值：{decision.item_id}",
                    where={"import_id": import_id, "item_id": decision.item_id},
                    remediation=[
                        "为这一条填写取值；",
                        "或改为「接受解析结果」——它不改变解析产出，只留下「已确认」的留痕",
                    ],
                )
            finding = next(item for item in record.findings if item.finding_id == decision.item_id)
            appended.append(
                ReviewOverride(
                    item_id=decision.item_id,
                    rule_id=finding.rule_id,
                    action=decision.action,
                    value=value if decision.action == "specify" else None,
                    operator=operator,
                    note=(decision.note or "").strip() or None,
                    decided_at=moment,
                    evidence=finding.evidence,
                )
            )
        updated = self._replace(record, decisions=[*record.decisions, *appended])
        # 留痕同时落**包内**（``overrides.json``）：那是生成期真正会读的那一份
        # （``DocumentEntry.overrides_ref`` 指向包内文件）。只留在暂存记录里的话，
        # 登记之后这份"人确认过什么"就丢了。
        if record.package_dir is not None:
            write_overrides(self._package_path(record), self._overrides_of(updated))
        return self._write_import(updated)

    # ---------------------------------------------------------------- 第 5 步
    def register_template(
        self,
        import_id: str,
        *,
        on_conflict: str = "abort",
        template_id: str | None = None,
        version: str | None = None,
        operator: str | None = None,
    ) -> TemplateImportRecord:
        """把这次导入登记进模板库（P05 第 5 步）。

        ``template_id`` / ``version`` 可在此**最终定版**（覆盖解析时得到的值）：解析结果
        与版本号无关，而"文件编号"往往要等人看过报告才敢确认——故重写清单是这一步的
        正常动作，不是补丁。改版本号**不会**改变 ``package_hash``（内容摘要不含清单）。
        """
        record = self._read_import(import_id)
        if on_conflict not in CONFLICT_POLICIES:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"未知的冲突处置：{on_conflict!r}",
                where={"on_conflict": on_conflict, "allowed": sorted(CONFLICT_POLICIES)},
                remediation=["on_conflict 取 abort（保留旧版，报冲突）或 overwrite（覆盖）"],
            )
        final_id = (template_id or record.template_id).strip()
        final_version = (version or record.version).strip()
        if final_id == "" or final_version == "":
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                "登记前必须有模板编号与版本号",
                where={"template_id": final_id, "version": final_version},
                remediation=[
                    "在登记表单里补上文件编号与版本（解析未能从文档中取得）",
                    "或在导入时直接给出这两个属性",
                ],
            )
        # 先判空再裁路径片段：``path_segment`` 对空串报的是"不能作为路径片段"，
        # 而这里真正的问题是"还没有编号"，两句话指向的动作不一样（一个去填表单，
        # 一个去查标识拼写）。非空的编号再过一次 ``path_segment``（硬约束 14）。
        final_id = path_segment(final_id, field="template_id")
        final_version = path_segment(final_version, field="version")

        library_dir = ensure_within(
            self.settings.templates_root, self.settings.templates_root / final_id / final_version
        )
        existing = self.repository.find_template(final_id, final_version)
        moment = self.now()
        replaced = False
        if existing is not None:
            same = same_template_content(
                existing, package_hash=record.package_hash, source_hash=record.source_hash
            )
            if not same and on_conflict != "overwrite":
                raise EngineError(
                    ErrorCode.VERSION_CONFLICT,
                    f"模板包已登记过且内容不同：{existing.ref}",
                    where={
                        "template_ref": existing.ref,
                        "existing_package_hash": existing.package_hash,
                        "incoming_package_hash": record.package_hash or "",
                        "registered_at": existing.registered_at.isoformat(),
                    },
                    remediation=[
                        "换一个版本号后重新提交（模板版本是配置与历史产物的引用键）",
                        "或确认覆盖：旧版本可能已被既有文档引用",
                    ],
                )
            # 目标目录必须先清空再写：``write_package`` 拒绝写非空目录，而残留的旧文件
            # （上一次的块原件、指纹）会留在新包里，让"这份包对应哪次解析"从目录本身
            # 再也看不出来。删除只发生在这里——包目录由服务端独占。
            #
            # **内容相同也走这条路**：那时没有任何东西被换掉，重登记是幂等的；
            # 但包里的 ``overrides.json`` 可能刚更新过，所以还是要重写一遍。
            # ``replaced`` 只在**内容真的换了一份**时为真——它对界面回答的是
            # "旧版被替换了、既有引用要当心"，而幂等重登记不构成那个风险。
            rmtree(library_dir, ignore_errors=True)
            replaced = not same

        registration = self._register_into(
            record,
            library_dir=library_dir,
            template_id=final_id,
            version=final_version,
            registered_by=operator or record.author,
            moment=moment,
            replaced=replaced,
        )
        updated = self._replace(
            record,
            template_id=final_id,
            version=final_version,
            registration=registration,
        )
        self.repository.add_template(
            RegisteredTemplate(
                template_id=final_id,
                version=final_version,
                package_hash=registration.package_hash,
                source_hash=registration.source_hash,
                package_dir=str(library_dir),
                registered_at=moment,
                doc_type=record.doc_type,
                source_filename=record.source_filename,
                notes=record.notes,
                registered_by=registration.registered_by,
            )
        )
        return self._write_import(updated)

    # ------------------------------------------------------------ 内部实现
    def _register_into(
        self,
        record: TemplateImportRecord,
        *,
        library_dir: Path,
        template_id: str,
        version: str,
        registered_by: str | None,
        moment: datetime,
        replaced: bool,
    ) -> TemplateRegistration:
        """把这次导入的内容写进库目录，返回回执。

        **两条路径都要把原件复制进库**（见 :meth:`_copy_source`）。曾经只有"仅登记
        原件"那条做复制，解析成功的那条只写了契约——于是库里存着一个**没有原件的
        模板包**：它能回答"这份体例有哪些槽位"，却拿不出任何可以往里写字的文档。
        表现是构建一路走到 ``render`` 才报 ``E_TEMPLATE_UNREADABLE``，而登记那一刻
        一切正常。这是"构建路径整体不落地"时看不见、接上真阶段体就立刻暴露的一类缺陷。
        """
        if record.package_dir is None:
            # "仅登记原件、稍后重试解析"：库里存的是**原件**，不是一个能用的模板包。
            # 界面据此显示"未解析"，生成期不会选中它——这比把一次失败的解析
            # 说成"入库成功"诚实得多。
            self._copy_source(record, library_dir)
            return TemplateRegistration(
                template_id=template_id,
                version=version,
                ref=f"{template_id}@{version}",
                registered_at=moment,
                source_hash=record.source_hash or "",
                doc_type=record.doc_type,
                registered_by=registered_by,
                replaced=replaced,
                parsed=False,
                integrity=[],
            )

        written = register_package(
            self._package_path(record),
            library_dir,
            template_id=template_id,
            version=version,
            overrides=self._overrides_of(record),
        )
        # 写完之后**再复核一次**：登记前的预检查的是暂存包，而"写进库之后读得回来吗"
        # 是另一件事（磁盘满、权限、部分写入）。合并成一次检查会漏掉后者，
        # 而后者恰恰是"界面说登记成功、生成期读不到"这种最难查的故障。
        problems = verify_package(written.package_dir)
        # 原件在 ``register_package`` **之后**复制：``write_package`` 拒写非空目录
        # （``E_VERSION_CONFLICT``），先放 ``source/`` 会让这次登记整个失败。
        self._copy_source(record, library_dir)
        return TemplateRegistration(
            template_id=template_id,
            version=version,
            ref=f"{template_id}@{version}",
            registered_at=moment,
            package_hash=written.package_hash,
            source_hash=record.source_hash or "",
            doc_type=record.doc_type,
            registered_by=registered_by,
            replaced=replaced,
            parsed=True,
            integrity=problems,
        )

    def _copy_source(self, record: TemplateImportRecord, library_dir: Path) -> Path:
        """把导入暂存里的原件复制进库（``<library_dir>/source/<filename>``）。

        原件为什么必须进库：渲染的**第一步**就是打开它——``render`` 要的是这份 ``.docx``，
        不是契约。而暂存目录（``imports/``）是**可回收**的（见 ``server.staging``：
        它不在保留策略内，登记完就允许清理），所以库里的这一份才是长期可用的原件；
        少了它，"照去年那份文档再生成一份"就再也没有输入了。

        路径口径与 ``docfidelity.builder`` 的 ``template_source_path`` 严格对齐
        （``<package_dir>/source/<source_filename>``）：两边各拼一次就会出现
        "登记时放在 A、构建时去 B 找"。
        """
        source = self._import_dir(record.import_id) / SOURCE_DIRNAME / record.source_filename
        target = ensure_dir(library_dir / SOURCE_DIRNAME) / record.source_filename
        target.write_bytes(read_bytes(source))
        return target

    def _package_path(self, record: TemplateImportRecord) -> Path:
        relative = record.package_dir or ""
        return ensure_within(self.settings.storage_root, self.settings.storage_root / relative)

    @staticmethod
    def _overrides_of(record: TemplateImportRecord) -> TemplateOverrides:
        return TemplateOverrides(
            template_ref=f"{record.template_id}@{record.version}",
            entries=list(record.decisions),
        )

    @staticmethod
    def _replace(record: TemplateImportRecord, **updates: Any) -> TemplateImportRecord:
        return replace(record, **updates)


def same_template_content(
    existing: RegisteredTemplate, *, package_hash: str | None, source_hash: str | None
) -> bool:
    """已有登记与这次导入是不是**同一份内容**。

    判据分两级，顺序是刻意的：

    ① **包哈希**（解析产出的摘要）优先。它是"这份模板解析出了什么"的身份，
       两个不同的原件若解析出同一个包，就该算同一份。
    ② 包哈希缺任一边（未解析的登记）时退到**原件哈希**：解析都还没成功，能比的
       只有"是不是同一份文件"。这一级让"把同一份坏文件再传一次"变成幂等重登记，
       而不是一个要人去决策的冲突。

    两级都比不了才返回 ``False``：内容没法比时"同一份"这个判断只能靠人，
    算成"相同"会让一次真正的重试被静默当成已登记。

    公开而不再是模块私有的，是因为**档案包导入**（``server.profiles`` 的
    ``import_profile_package``）也要做同一个判断——它随包带来的模板包同样可能已经
    在库里了。抄一份到那边就会出现"两条导入路径对'同一份'给两个结论"，而其中之一
    必然与界面告诉用户的话相反。
    """
    if existing.package_hash and package_hash:
        return existing.package_hash == package_hash
    if existing.source_hash and source_hash:
        return existing.source_hash == source_hash
    return False
