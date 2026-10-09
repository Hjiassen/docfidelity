"""冻结输入的读取——阶段体与编排之间的**唯一方言**，以及工作区布局的**唯一出处**。

为什么需要这一层
----------------
``StageContext.payload`` 是一个 ``Mapping``（DD-11.3 刻意如此：新增一种输入不必改
编排签名）。裸着用它代价很具体：五个阶段体各写一次 ``payload["profile"]``，于是
"编排少给了一项"会在五个地方分别表现成 ``KeyError`` / ``None`` / 静默走默认值三副
面孔，而它们本该是**同一句**"编排与阶段体的方言不一致"。

:class:`BuildInputs` 把那个 Mapping 解析成一个具名对象：一处解析、一处报错、
一次说清缺了哪几项。

工作区布局为什么也在这里
------------------------
``Workspace`` 的说明写着"阶段体可以放心地用逻辑名取放文件"——而"逻辑名"必须
**唯一**。五个阶段体各拼一次路径，就会出现"``ingest`` 写 ``work/facts.json``、
``compose`` 读 ``work/fact_snapshot.json``"这种谁也查不出来的错配：两个阶段各自
都对，合起来什么都不产出。故那份布局在本模块里只有一个定义处。

与 ``payload`` 的分工（一个容易搞混的地方）
-------------------------------------------
``payload`` 只冻结``build_payload ``**给得了**的东西：声明与路径（``SourceConfig`` /
``DocTypeProfile`` / 模板包目录）。**阶段之间传递的中间态走工作区**——``run_stage``
只把 ``payload`` 递给阶段体，**不递上游阶段的输出**（阶段体拿到的是一个只含输入的
``Mapping``）。所以"上游产出在哪"这件事由本模块的常量回答，而不是由 ``payload``
回答；``payload`` 是"这次构建的输入"，常量是"这条流水线的形状"。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from docfidelity.contracts.config import PaginationPolicy, RuleSetRef, Terminology
from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.fact import SourceConfig
from docfidelity.contracts.profile import DocTypeProfile

__all__ = [
    "FACTS_REL",
    "FINDINGS_DIRNAME",
    "IR_REL",
    "RENDER_REL",
    "REPORT_REL",
    "TRACES_REL",
    "BuildInputs",
    "findings_rel",
]

# ------------------------------------------------------------------ 工作区布局
#
# 全部相对**作业工作区根**（``Workspace.put_*`` 的 ``relative`` 参数，也是断点里
# ``outputs`` 存的那个形状）。中间态在 ``work/``、交付物在 ``artifacts/``（DD-11.7）。
#
#: ``ingest`` → 多源合并后的事实快照（``FactSnapshot``）。
FACTS_REL: Final = "work/facts.json"
#: ``compose`` → 内容 IR（``ContentIR``）。
IR_REL: Final = "work/ir.json"
#: ``compose`` → 追溯矩阵（``TraceMatrix``）。与 IR 分文件：IR 是渲染的输入，
#: 追溯是门禁与 P11 的输入，两者的消费方不同，混在一个文件里会让"只想看结构"
#: 的调用方也得把追溯边全读进来。
TRACES_REL: Final = "work/traces.json"
#: ``render`` → 本次产物的**清单**（文件名 / 相对路径 / 包摘要）。
#:
#: 为什么要一个清单而不是让 ``qa`` 自己按 ``output_filename()`` 重算一遍：重算要求
#: 两次调用用的是**同一份** ``RenderPolicy``（``PropertiesPolicy.filename`` 一改，
#: 重算就指向另一个名字）。清单把"这一轮到底写了哪个文件"变成一条**事实**，
#: 而不是一条需要复现的推导。
RENDER_REL: Final = "work/render.json"
#: ``qa`` → 门禁报告。逻辑名必须叫 ``report``（``server.jobs.REPORT_OUTPUT_KEY``），
#: 与下面的 ``REPORT_REL`` 是两件事：前者是断点里的键，后者是文件位置。
REPORT_REL: Final = "work/report.json"

#: 各阶段自报的判定。``qa`` 阶段把它们作为 ``QaContext.upstream`` 并进报告——
#: 阶段体的 ``findings`` 只进事件流，不进报告；不落盘就没有第二条路把它们带到
#: 报告里（``run_stage`` 不向上游要 findings）。
FINDINGS_DIRNAME: Final = "work/findings"


def findings_rel(stage: str) -> str:
    """某阶段自报判定的落点。一个阶段一个文件，互不覆盖。"""
    return f"{FINDINGS_DIRNAME}/{stage}.json"


# ------------------------------------------------------------------ 输入解析
@dataclass(frozen=True)
class BuildInputs:
    """一次构建的**冻结输入**（``server.jobs.JobRunner.build_payload`` 的解析结果）。

    全部字段都是**声明或路径**，没有一个是"阶段跑出来的东西"——那是工作区的事
    （见模块文档）。故同一个 :class:`BuildInputs` 在五个阶段体里都成立，不需要
    "这次是哪个阶段"这个上下文。
    """

    project_id: str
    doc_key: str
    #: 模板包引用 ``<template_id>@<version>``；写进 ``ContentIR.template_ref``。
    template_ref: str
    profile: DocTypeProfile
    domain_models: tuple[DomainModel, ...]
    sources: tuple[SourceConfig, ...]
    terminology: Terminology
    pagination: PaginationPolicy
    rule_sets: tuple[RuleSetRef, ...]
    #: 来源 ``root`` 的解析基点（**存储根**，不是作业工作区）。
    #:
    #: 必须由编排冻进来，而不是在阶段体里推：``server.sources`` 的"试接入"按存储根
    #: 解析（那处的文档写明了理由——试接入发生在作业之外，没有工作区可作参照），
    #: 而构建期若按工作区解析，就会出现"界面上试接入通了、构建时找不到文件"。
    #: 两个解析根是这个功能最容易踩的一处，故它是**必填**、不给缺省。
    source_root: Path
    #: 外部词表（文档类型名 / 领域概念名）。``compose`` 用它做 IR 纯净度自检，
    #: **只查引擎表面**（单元键名与 ``unit_id``/``type``/``level``），事实数据的值
    #: 由 ``qa.type_agnostic`` 复核——见 ``compose.purity`` 的说明。
    purity_words: tuple[str, ...] = ()
    #: 模板包目录（内含 ``contract/*.json``）。``render`` / ``qa`` 要用；
    #: ``ingest`` / ``compose`` 不用，故**缺了不算整体失败**——见 :meth:`template_dir`。
    template_package_dir: Path | None = None
    #: 模板**原件**（``.docx``）。渲染的第一件事就是打开它。
    template_docx: Path | None = None
    #: 本机 Word 域刷新姿态：``None`` = 自动探测（历史行为），``False`` = 明确关闭。
    #:
    #: 它必须是**冻结输入**而不是阶段体自己探测的：探测结果（"这台机器装没装
    #: Word"）直接决定产物字节（页码刷没刷），把它留给阶段体会让同一份输入在
    #: 两种机器上产出不同的产物，NFR-02 的确定性当场失效。
    office_enabled: bool | None = None
    #: ``office_enabled=False`` 时给报告的人读原因。空 = 探测不到（不是关掉的）。
    office_reason: str | None = None

    @classmethod
    def of(cls, payload: Mapping[str, Any]) -> BuildInputs:
        """解析冻结输入；缺项**立刻**报错，不做部分默认。

        ``sources`` / ``domain_models`` 是**容器**：允许为空（"一份来源都没启用"、
        "库里一个领域类型都没登记"都是有意义的输入），但不允许**缺键**——两者混同
        会让"编排少冻了一项"伪装成"这次构建确实没有来源"，而后者不会失败，
        只会产出一份内容空白的文档。
        ``profile`` 是**对象**：空的就是没有，报错文案由 :func:`_unresolved_profile`
        给出（它要带上原始引用，才能让人知道该去补哪一份档案）。
        """
        _require(payload, "project_id", "doc_key", "template_ref", "source_root")
        _require_container(payload, "sources", "domain_models")
        if not payload.get("profile"):
            raise _unresolved_profile(payload)
        return cls(
            project_id=_text(payload, "project_id"),
            doc_key=_text(payload, "doc_key"),
            template_ref=_text(payload, "template_ref"),
            profile=DocTypeProfile.model_validate(payload["profile"]),
            domain_models=tuple(
                DomainModel.model_validate(item) for item in payload["domain_models"]
            ),
            sources=tuple(SourceConfig.model_validate(item) for item in payload["sources"]),
            terminology=Terminology.model_validate(payload.get("terminology") or {}),
            pagination=PaginationPolicy.model_validate(payload.get("pagination") or {}),
            rule_sets=tuple(
                RuleSetRef.model_validate(item) for item in payload.get("rule_sets") or ()
            ),
            source_root=Path(str(payload["source_root"])),
            purity_words=tuple(str(word) for word in payload.get("purity_words") or ()),
            template_package_dir=_optional_path(payload, "template_package_dir"),
            template_docx=_optional_path(payload, "template_docx"),
            office_enabled=_optional_bool(payload, "office_enabled"),
            office_reason=_optional_text(payload, "office_reason"),
        )

    def template_dir(self) -> Path:
        """模板包目录；**没冻进来就报错**（渲染/门禁才需要它，故不参与整体校验）。

        Raises
        ------
        ConfigurationError
            ``E_CONFIG_INVALID``：模板包目录未冻结。常见成因是这份模板**只登记了
            原件、解析未成功**（``RegisteredTemplate.parsed`` 为假）——那时库里
            没有契约，构建无从谈起。
        """
        return _required_path(
            self.template_package_dir, "template_package_dir", settings="模板包目录"
        )

    def docx_path(self) -> Path:
        """模板原件路径；**没冻进来就报错**（同上：只有渲染需要它）。"""
        return _required_path(self.template_docx, "template_docx", settings="模板原件")


# ------------------------------------------------------------------ 内部
def _require(payload: Mapping[str, Any], *keys: str) -> None:
    """标量键必须存在且**非空**（判空口径与硬规则 15 一致：``if not x`` 空串也算缺）。"""
    missing = [key for key in keys if not payload.get(key)]
    if missing:
        raise _missing(missing)


def _require_container(payload: Mapping[str, Any], *keys: str) -> None:
    """容器键必须存在，但**允许为空**——见 :meth:`BuildInputs.of` 的说明。"""
    missing = [key for key in keys if key not in payload]
    if missing:
        raise _missing(missing)


def _missing(missing: list[str]) -> ConfigurationError:
    return ConfigurationError(
        f"构建输入快照缺项：{sorted(missing)}",
        where={"missing": sorted(missing)},
        remediation=[
            "检查编排层（server.jobs.JobRunner.build_payload）是否冻结了这项输入",
            "这类缺项是编排与阶段体的方言不一致，不是使用者配置的问题",
        ],
    )


def _unresolved_profile(payload: Mapping[str, Any]) -> ConfigurationError:
    """档案**引用解析不出**——与"编排少冻了一项"是两件事，故不并进 :func:`_missing`。

    区分它们很实际：``_missing`` 的补救动作是"去改代码"（编排漏了），这一条的补救
    动作是"去补库或改配置"。合成一条的话，使用者在界面上只会看到一句
    "构建输入快照缺项：['profile']"，而真正该做的那件事（把档案登记上去）不在里面。

    ``where`` 里带上原始引用（``profile_id``）：它是使用者唯一能拿去搜索的字符串。
    """
    ref = payload.get("profile_id")
    return ConfigurationError(
        f"构建输入快照里没有档案本体：配置中的 profile_id = {ref!r}"
        if ref
        else "构建输入快照里没有档案本体，且配置里也没有给出 profile_id",
        where={"missing": ["profile"], "profile_id": ref},
        remediation=[
            "确认该档案已登记（P03 → 文档类型档案），且没有被废弃——废弃的那一版不参与构建",
            "若 profile_id 写作 profile_id@version，确认那个版本存在",
            "档案目录被外部清理过时也会如此（档案本体是磁盘上的 profile.json）",
        ],
    )


def _text(payload: Mapping[str, Any], key: str) -> str:
    return str(payload[key])


def _optional_path(payload: Mapping[str, Any], key: str) -> Path | None:
    raw = payload.get(key)
    return None if not raw else Path(str(raw))


#: 只认这三种写法（不区分大小写）。JSON 的 ``true``/``false`` 与从 ``.env`` 转写
#: 过来的 ``"true"``/``"false"`` 是同一件事的两种载体，两者都要收。
_TRUE_WORDS: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
_FALSE_WORDS: Final[frozenset[str]] = frozenset({"false", "0", "no"})


def _optional_bool(payload: Mapping[str, Any], key: str) -> bool | None:
    """可选布尔：键缺失或为 ``null`` 即 ``None``（= 调用方没表态，按缺省姿态走）。

    **不写 ``bool(raw)``**：字符串 ``"false"`` 是真值，于是"我明明把 Word 关了"
    会静默变成"开着"，而症状（产物多出一次 Word 刷新、字节随机器而变）出现在很远
    的下游。写不出来的值一律报错，让错误停在冻结这一步。
    """
    raw = payload.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    word = str(raw).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigurationError(
        f"构建输入快照的 {key} 不是布尔：{raw!r}",
        where={"key": key, "value": str(raw)},
        remediation=[
            f"改成 JSON 布尔（true / false）或 {'/'.join(sorted(_TRUE_WORDS | _FALSE_WORDS))} 之一",
            "或删掉这个键，让调用方按缺省姿态决定",
        ],
    )


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    raw = payload.get(key)
    return None if raw is None or raw == "" else str(raw)


def _required_path(value: Path | None, key: str, *, settings: str) -> Path:
    if value is None:
        raise ConfigurationError(
            f"构建输入快照里没有{settings}（{key}）",
            where={"missing": [key]},
            remediation=[
                "确认这份模板包已**解析**入库（未解析的登记只有原件，没有契约）",
                "或检查编排层是否冻结了该项输入",
            ],
        )
    return value
