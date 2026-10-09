"""模板导入记录的数据模型与 JSON 编解码（``imports/<id>/import.json``）。

为什么单独一个模块
------------------
本模块装的全是**数据**：一次导入记录长什么样、它怎么落成 JSON、又怎么从 JSON 还原。
导入的用例（收件 / 重解析 / 确认 / 登记）与暂存目录的回收都读它，但都不该把它塞在
自己的文件里——``server/templates.py`` 一度到 814 行（NFR-09 上限 800），而拆出来的
第一刀应该是"数据 vs 行为"，不是把某个方法挪个地方：数据被谁都能读，方法不可以。

与 :mod:`server.staging` 的分工
-------------------------------
本模块回答"这份导入记录说了什么"；:mod:`server.staging` 回答"这个暂存目录能不能回收"。
后者的判据（**已登记**）读的正是本模块的 :attr:`TemplateImportRecord.registration`，
故两者之间传递的是**记录本身**，而不是"是否已登记"这个已经判过的布尔——把判过的结论
传出去，就等于有了第二处判定。

编解码必须一一对应
------------------
:func:`read_record` 与 :func:`record_payload` 是一对。**手工写的那几个字段**（``stages``
里的 pydantic 错误快照、``findings`` / ``decisions`` / ``summary`` / ``registration``）
在两边都要显式处理：``dataclasses.asdict`` 只递归 dataclass，把 pydantic 模型原样留在
字典里，而 ``json.dumps`` 不认识它们。漏掉一侧的表现不是报错，而是"写进去的东西读不回来"
——例如 ``registered_at`` 以字符串形态还原，下一次写盘时 ``.isoformat()`` 才炸，
而那时离"哪个字段没解析"已经很远了。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Final

from docfidelity.contracts.errors import EngineErrorInfo
from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import ReviewAction, ReviewOverride
from docfidelity.pipeline import ContractSummary, ImportStage

__all__ = [
    "IMPORT_RECORD_NAME",
    "PACKAGE_DIRNAME",
    "SOURCE_DIRNAME",
    "ReviewDecision",
    "TemplateImportRecord",
    "TemplateRegistration",
    "read_record",
    "record_payload",
]

#: 暂存目录内的文件 / 目录名（``imports/<import_id>/`` 的固定布局）。
IMPORT_RECORD_NAME: Final = "import.json"
PACKAGE_DIRNAME: Final = "package"
SOURCE_DIRNAME: Final = "source"


@dataclass(frozen=True)
class ReviewDecision:
    """一次待确认项处理（P05 第 4 步的一条）。"""

    item_id: str
    action: ReviewAction
    value: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class TemplateRegistration:
    """一次登记的结果（P05 第 5 步的回执）。"""

    template_id: str
    version: str
    ref: str
    registered_at: datetime
    package_hash: str = ""
    source_hash: str = ""
    doc_type: str | None = None
    registered_by: str | None = None
    #: 是否**覆盖**了一次已有登记（同一 ``template_id@version`` 换了内容）。
    replaced: bool = False
    #: 是否已解析出契约。``False`` 表示这份登记只有原件（"稍后重试解析"）。
    parsed: bool = True
    #: 登记后复核包完整性得到的**问题清单**（空 = 通过）。登记前预检过一遍，
    #: 这里是写完之后的复核——写盘本身也可能出错，两者不能合并成一次。
    #: 与 :attr:`TemplateImportRecord.integrity` 同类型（``list[str]``）：同一个概念
    #: 在一个模块里用两种容器，读的人就得记住"哪个能改、哪个能加"，而它们都是 JSON
    #: 里的一个数组。
    integrity: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TemplateImportRecord:
    """一次模板导入的完整记录（P05 第 2 步与第 3 步的全部数据）。

    ``decisions`` 是**留痕**（谁在什么时候确认了什么），而"还剩几项要确认"由它推出的
    :meth:`pending` 决定——与门禁裁决同一口径：**没有留痕就是没办结**（"已办结"不等于
    "可通过"，也不等于"跳过"）。
    """

    import_id: str
    created_at: datetime
    source_filename: str
    source_size_bytes: int
    #: 最近一次**重解析**的时刻。``None`` = 这条记录自导入之后从没重解析过。
    #: 与 ``created_at`` 分开记：``created_at`` 是"这份导入什么时候发生的"（是它的身份），
    #: 而重解析不改变这个身份。旧记录没有这个键，故默认 ``None``（向后兼容）。
    reparsed_at: datetime | None = None
    template_id: str = ""
    version: str = ""
    template_id_source: str = ""
    version_source: str = ""
    source_hash: str | None = None
    doc_type: str | None = None
    notes: str | None = None
    author: str | None = None
    register_only: bool = False
    package_dir: str | None = None
    package_hash: str | None = None
    stages: list[ImportStage] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    decisions: list[ReviewOverride] = field(default_factory=list)
    integrity: list[str] = field(default_factory=list)
    summary: ContractSummary | None = None
    registration: TemplateRegistration | None = None

    @property
    def parsed(self) -> bool:
        return not self.register_only and self.package_dir is not None

    @property
    def version_source_explicit(self) -> bool:
        """编号/版本是不是**人给的**（而不是从文档里猜的）。

        界面据此决定"登记前还要不要人补一下编号"——但**不**由界面推断：它只是把服务端
        给的来源原样显示（``explicit`` / ``document`` / ``filename``）。
        """
        return self.template_id_source == "explicit" and self.version_source == "explicit"

    def pending(self) -> list[Finding]:
        """还没有留痕的待确认项（按报告里的次序）。"""
        decided = {entry.item_id for entry in self.decisions}
        return [item for item in self.findings if (item.finding_id or "") not in decided]

    @property
    def reparse_blocked_reason(self) -> str | None:
        """不能重解析的原因（**服务端措辞**）；``None`` = 可以重解析。

        界面据此置灰按钮并直接把这句话显示出来——它自己不归纳"为什么不能重试"
        （硬规则 15）。与 :attr:`can_reparse` 成对，且判定只有这一处。
        """
        if self.registration is not None:
            return (
                f"该导入已登记为 {self.registration.ref}；登记之后的模板包是不可变的历史，"
                "要新的解析结果请重新导入一份原件"
            )
        return None

    @property
    def can_reparse(self) -> bool:
        return self.reparse_blocked_reason is None


def read_record(payload: Mapping[str, Any]) -> TemplateImportRecord:
    """从 ``import.json`` 的内容还原记录。**宽松读取**：认得出来的字段才取。

    ``registered_at`` / ``created_at`` / ``reparsed_at`` 三个时刻都在这里显式解析回
    :class:`~datetime.datetime`。① 与 :func:`record_payload` 里的 ``.isoformat()``
    一一对应；② 更重要：**只解析一部分**会让"再写一次"变成一次崩溃——``TemplateRegistration``
    是普通 dataclass，字符串时间戳不会当场报错，直到下一次 ``.isoformat()`` 才发现它不是
    时间。这条正是把编解码收在一个模块里的理由。
    """
    raw = dict(payload)
    raw["stages"] = [
        ImportStage(
            stage=str(item.get("stage", "")),
            ok=bool(item.get("ok", False)),
            note=item.get("note"),
            error=(
                None if item.get("error") is None else EngineErrorInfo.model_validate(item["error"])
            ),
        )
        for item in raw.get("stages", [])
    ]
    raw["findings"] = [Finding.model_validate(item) for item in raw.get("findings", [])]
    raw["decisions"] = [ReviewOverride.model_validate(item) for item in raw.get("decisions", [])]
    summary = raw.get("summary")
    raw["summary"] = None if summary is None else ContractSummary(**summary)
    registration = raw.get("registration")
    raw["registration"] = None if registration is None else _registration_of(registration)
    raw["created_at"] = datetime.fromisoformat(str(raw["created_at"]))
    reparsed = raw.get("reparsed_at")
    raw["reparsed_at"] = None if reparsed is None else datetime.fromisoformat(str(reparsed))
    return TemplateImportRecord(**raw)


def _registration_of(payload: Mapping[str, Any]) -> TemplateRegistration:
    """还原登记回执；``registered_at`` 必须解析回时间（理由见 :func:`read_record`）。"""
    raw = dict(payload)
    raw["registered_at"] = datetime.fromisoformat(str(raw["registered_at"]))
    return TemplateRegistration(**raw)


def record_payload(record: TemplateImportRecord) -> dict[str, Any]:
    """记录 → 可 JSON 化的字典（与 :func:`read_record` 一一对应）。"""
    payload = asdict(record)
    payload["created_at"] = record.created_at.isoformat()
    payload["reparsed_at"] = None if record.reparsed_at is None else record.reparsed_at.isoformat()
    # ``asdict`` 只递归 dataclass，``ImportStage.error`` 是 pydantic 模型，故要显式 dump。
    payload["stages"] = [
        {
            "stage": stage.stage,
            "ok": stage.ok,
            "note": stage.note,
            "error": None if stage.error is None else stage.error.model_dump(mode="json"),
        }
        for stage in record.stages
    ]
    payload["findings"] = [item.model_dump(mode="json") for item in record.findings]
    payload["decisions"] = [item.model_dump(mode="json") for item in record.decisions]
    payload["summary"] = None if record.summary is None else asdict(record.summary)
    if record.registration is not None:
        payload["registration"] = {
            **asdict(record.registration),
            "registered_at": record.registration.registered_at.isoformat(),
        }
    return payload
