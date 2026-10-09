"""领域模型库的用例层（FR-M1-13 / FR-M2-23）。

为什么领域模型要进库
--------------------
``pipeline.profiles.validate_profile`` 的第 1 条（``projections[].for_each`` 引用的
领域类型是否均已登记）按 FR-M2-23 要在**档案登记期**拒绝——放行等于让投影安静地
返回零个单元，最终表现为"文档少了一整章而报告全绿"。

而档案是**全局**资产（``doc_type_profiles`` 的主键是 ``(profile_id, version)``，
没有 ``project_id``）：登记那一刻，"拿哪个项目的领域模型去查"这个问题**没有答案**。
所以领域模型不能挂在 ``ProjectConfig`` 上，只能与模板包、档案一样**进库**——
需求 4.9.2 把这三层并列为正交的库资产，原话是"一个领域模型可被多份档案复用"。

三件事，三个落点
----------------
====================  ===========================================================
① 声明本体            ``domain_models`` 表（``payload`` 列，见
                      ``storage.RegisteredDomainModel``）
② 登记幂等判据        ``pipeline.fingerprint_domain_model``
③ 装配注册表          ``pipeline.domain_registry``（本模块不 import ``model``）
====================  ===========================================================

没有磁盘本体
------------
与模板包 / 档案不同：领域模型是一段几百字节的 JSON，而"整包导出给第三方"
（FR-M2-25）是**档案**的要求，不是它的。给它造一个目录布局，换来的是"磁盘上的文件和
库里的记录哪个才算原件"这个新问题——而这个问题在模板包与档案上之所以值得付代价，
是因为它们的本体大、且要独立交换。

身份是 ``kind``，没有版本
------------------------
理由写在 ``storage.RegisteredDomainModel`` 的类文档里；一句话：引用它的地方
（档案的 ``for_each``、追溯边的 ``<kind>:<key>``）写的是裸 ``kind``，**带不上版本**，
所以"哪一版"在任何调用点都表达不出来。代价是"改一份声明"看不到历史——这是有意的
取舍：领域模型是**词汇表**，不是产物。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Final

from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import fingerprint_domain_model

from .storage import DomainModelRepository, RegisteredDomainModel
from .templates import CONFLICT_POLICIES

if TYPE_CHECKING:
    from .storage import Repository

__all__ = [
    "DOMAIN_MODEL_POLICIES",
    "KIND_FORBIDDEN_CHARS",
    "DomainModelMixin",
    "DomainModelRegistration",
    "domain_declarations",
]

#: 冲突处置策略。与模板登记、档案登记**同一份闭集**（``server.templates`` 的
#: ``CONFLICT_POLICIES``）：三种资产的"重复登记"是同一件事，各写一份的话，
#: 迟早会出现"两个端点对同一个词接受不同的拼法"。
DOMAIN_MODEL_POLICIES: Final[frozenset[str]] = CONFLICT_POLICIES

#: ``kind`` 里不允许出现的字符。两个理由都实在，且都不是"图个整齐"：
#:
#: * ``DomainObjectRef`` 的字符串形式是 ``<kind>:<k1>/<k2>``（``contracts.domain``），
#:   拆分点在**第一个** ``:``——kind 里带 ``:`` 会让追溯边无法反解；
#: * ``kind`` 是 ``GET /domain-models/{kind}`` 的路径参数，硬规则 14 要求路径参数
#:   不得含 ``/``（``\\`` 一并禁掉：它在 Windows 上是同一个分隔符）。
KIND_FORBIDDEN_CHARS: Final[tuple[str, ...]] = (":", "/", "\\")


@dataclass(frozen=True)
class DomainModelRegistration:
    """一次领域模型登记的回执（P03 的行内操作结果）。"""

    kind: str
    title: str
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    #: 是否**覆盖**了一次已有登记（同 ``kind`` 换了内容）。
    replaced: bool = False


class DomainModelMixin:
    """领域模型库（``Services`` 的第七个 mixin，见 ``server.services``）。

    与另外几个 mixin 一样，它**只**用 ``self.repository`` / ``self.now``
    （领域模型没有磁盘本体，故用不到 ``settings``）。
    """

    if TYPE_CHECKING:
        repository: Repository
        now: Callable[[], datetime]

    # ---------------------------------------------------------------- 登记
    def register_domain_model(
        self,
        *,
        model: DomainModel,
        author: str | None = None,
        on_conflict: str = "abort",
    ) -> DomainModelRegistration:
        """登记一份领域模型声明（``POST /domain-models``）。

        三条与模板 / 档案完全同构的纪律，故这里不重新发明：

        1. **重复按内容判**（硬规则 24）：同 ``kind`` 且指纹相同 = 幂等；指纹不同 =
           ``E_VERSION_CONFLICT``（带出旧/新指纹），要覆盖得**显式**选
           ``on_conflict="overwrite"``。用 ``E_VERSION_CONFLICT`` 而不是另立一个码，
          是因为界面上的"确认覆盖"流程对三种资产是同一条——换码会让那条流程分叉。
        2. **覆盖清旧废弃标记**：``overwrite`` 是"重新提交"的正常路径，不清标记会得到
           一份登记成功、却因废弃而在列表与注册表里都看不见的声明（硬规则 25）。
        3. **``kind`` 先校验再用**：它是身份键，同时要当 URL 路径片段与 ``for_each``
           的引用词（见 :data:`KIND_FORBIDDEN_CHARS`）。

        Raises
        ------
        EngineError
            ``E_CONFIG_INVALID``（未知的 ``on_conflict``——与档案登记同一口径）、
            ``E_DOMAIN_MODEL_INVALID``（``kind`` 缺失 / 含非法字符）、
            ``E_VERSION_CONFLICT``（同 ``kind`` 已有不同内容的声明）。
        """
        if on_conflict not in DOMAIN_MODEL_POLICIES:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"未知的冲突处置：{on_conflict!r}",
                where={"on_conflict": on_conflict, "allowed": sorted(DOMAIN_MODEL_POLICIES)},
                remediation=["on_conflict 取 abort（保留旧版，报冲突）或 overwrite（覆盖）"],
            )
        kind = _require_kind(model)
        fingerprint = fingerprint_domain_model(model)
        existing = self.repository.find_domain_model(kind)
        moment = self.now()
        replaced = False
        if existing is not None:
            same = existing.fingerprint == fingerprint
            if not same and on_conflict != "overwrite":
                raise EngineError(
                    ErrorCode.VERSION_CONFLICT,
                    f"领域模型已登记过且内容不同：{kind}",
                    where={
                        "kind": kind,
                        "existing_fingerprint": existing.fingerprint,
                        "incoming_fingerprint": fingerprint,
                        "registered_at": existing.registered_at.isoformat(),
                    },
                    remediation=[
                        "确认覆盖：引用该类型的档案会立刻改按新声明校验",
                        "或修正声明，使它与库里那一份一致（一次类型只能有一份声明）",
                    ],
                )
            replaced = not same

        entry = RegisteredDomainModel(
            model=model,
            fingerprint=fingerprint,
            registered_at=moment,
            registered_by=author,
            # 覆盖时一并清掉旧废弃标记，理由见方法文档第 2 条。
            deprecated=False,
            deprecated_reason=None,
        )
        self.repository.add_domain_model(entry)
        return DomainModelRegistration(
            kind=kind,
            title=model.title,
            fingerprint=fingerprint,
            registered_at=moment,
            registered_by=author,
            replaced=replaced,
        )

    # ---------------------------------------------------------------- 列表
    def list_domain_models(self, *, include_deprecated: bool = True) -> list[RegisteredDomainModel]:
        entries = self.repository.list_domain_models()
        if include_deprecated:
            return entries
        return [entry for entry in entries if not entry.deprecated]

    def get_domain_model(self, kind: str) -> RegisteredDomainModel:
        entry = self.repository.find_domain_model(kind)
        if entry is None:
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到领域模型：{kind}",
                where={"kind": kind},
                remediation=["按 GET /api/v1/domain-models 列出已登记的类型"],
            )
        return entry

    def domain_declarations(self) -> list[DomainModel]:
        """**进注册表**的那些声明：已废弃的不进。转发 :func:`domain_declarations`。

        唯一一处"废弃的领域模型还算不算数"的判据。散在调用点的话，"档案登记时的
        校验"与"构建期求值"会各自取一份，而两者不一致的表现很具体——
        登记说"类型不存在"、构建却跑起来了，或者反过来。

        构建侧（``server.jobs`` 冻结输入快照）拿的是仓储而不是 ``Services``，
        故那条判据必须是**模块级**的一份，两边都转发它。
        """
        return domain_declarations(self.repository)

    # ---------------------------------------------------------------- 废弃
    def deprecate_domain_model(
        self, kind: str, *, reason: str, operator: str | None = None
    ) -> RegisteredDomainModel:
        """标记一个领域类型为废弃（**不删除**）。

        只标不删：类型是已登记档案的引用键（``for_each``）与历史产物的追溯前缀
        （``<kind>:<key>``）。删掉它，"这份文档是按哪个类型产出的"就永久失去答案。

        ``reason`` 必填且不得为空白——一次没有理由的废弃，三个月后读起来与误操作
        无法区分（与 ``deprecate_profile`` / ``E_WAIVER_REASON_REQUIRED`` 同一口径）。

        重复废弃是**幂等**的，且不改写最早那次的理由与时刻。
        """
        if not (reason or "").strip():
            raise EngineError(
                ErrorCode.DOMAIN_MODEL_INVALID,
                "废弃领域模型必须给出理由",
                where={"kind": kind},
                remediation=["在请求体里给出 reason：为什么这个类型不再适用"],
            )
        entry = self.get_domain_model(kind)
        if entry.deprecated:
            return entry
        return self.repository.save_domain_model(
            replace(entry, deprecated=True, deprecated_reason=reason.strip())
        )


def _require_kind(model: DomainModel) -> str:
    """校验并返回身份键 ``kind``。**不做任何规整**（不 trim、不替换非法字符）。

    不 trim 的理由：``kind`` 会逐字出现在 ``for_each``、追溯边与 URL 里。把它悄悄
    trim 掉，就意味着"我登记的类型名"与"我写的类型名"可以不是同一个字符串——而
    表现是"``for_each: endpoint`` 报类型不存在，可我明明登记了 ``endpoint``"。
    报错比猜便宜。

    Raises
    ------
    EngineError
        ``E_DOMAIN_MODEL_INVALID``——``kind`` 为空、含首尾空白，或含
        :data:`KIND_FORBIDDEN_CHARS` 里的字符。
    """
    kind = model.kind
    if kind == "":
        raise EngineError(
            ErrorCode.DOMAIN_MODEL_INVALID,
            "领域模型缺少 kind",
            where={"title": model.title},
            remediation=["在声明里填上 kind（它是 for_each 与追溯边的引用词）"],
        )
    if kind != kind.strip():
        raise EngineError(
            ErrorCode.DOMAIN_MODEL_INVALID,
            f"领域模型的 kind 含首尾空白：{kind!r}",
            where={"kind": kind},
            remediation=[
                "去掉 kind 首尾的空白后重新提交",
                "不做自动 trim：kind 会逐字出现在 for_each 与追溯边里，"
                "悄悄改一个字符会让引用对不上",
            ],
        )
    forbidden = sorted({char for char in KIND_FORBIDDEN_CHARS if char in kind})
    if forbidden:
        raise EngineError(
            ErrorCode.DOMAIN_MODEL_INVALID,
            f"领域模型的 kind 含不允许的字符：{forbidden}",
            where={"kind": kind, "forbidden": forbidden},
            remediation=[
                "换用不含 ':' '/' '\\\\' 的 kind",
                "':' 是追溯边 <kind>:<key> 的分隔符，'/' 是 URL 路径分隔符",
            ],
        )
    return kind


def domain_declarations(repository: DomainModelRepository) -> list[DomainModel]:
    """:meth:`DomainModelMixin.domain_declarations` 的**唯一实现**（见其文档）。

    单独抽成模块级函数是因为它有**两个调用方，而它们持有的东西不同**：用例层持有
    ``Services``，编排层（``server.jobs`` 冻结输入快照）只有 ``Repository``。
    让后者自己写一遍 ``if not entry.deprecated``，就等于把"废弃的还算不算数"
    这条判据复制成了两份——而两份判据漂开的表现很具体：**登记说"类型不存在"、
    构建却跑起来了**（或者反过来）。
    """
    return [entry.model for entry in repository.list_domain_models() if not entry.deprecated]
