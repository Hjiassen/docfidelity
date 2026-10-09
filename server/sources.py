"""事实源的用例层（DD-13.2 的 ``projects/{pid}/sources/*``；DD-5.1）。

三件事，三个落点
----------------
====================  ===========================================================
① 配置期校验          ``pipeline.sources.check_config``（**转发**）
② 试接入 / 条目浏览    ``pipeline.sources.probe_source`` / ``load_items``（**转发**）
③ 声明与探测留痕      ``fact_sources`` 表（**仓储**，每项目一份列表）
====================  ===========================================================

与模板 / 档案不同的地方：来源**没有磁盘本体**
--------------------------------------------
模板包与档案是"先有资产再登记"，本体在磁盘目录里；一条事实源声明就是**一行配置**
（外加"上次探测结果是什么"这份留痕）。所以它整份进仓储，没有"库与目录对不上"
那种半死状态。

``root`` 相对什么解析
--------------------
相对**存储根**（``settings.storage_root``），而不是某个作业的工作区。理由是
"试接入"这个动作发生在**作业之外**（人在界面上点了一个按钮），此时没有任何作业
工作区可作参照；而上传件本来就落在 ``<storage_root>/uploads/<uid>/`` 之下，
于是"上传一份导出文件 → 在来源配置里引用它"是一条能走通的路。

构建期与"试接入"必须同源
--------------------------
构建期的 ``ingest`` 阶段（``docfidelity.builder.stages.ingest_stage``）**已经真的
消费来源声明**了，它跑的就是这里那份 ``SourceConfig``。两个调用点因此必须共用同一个
解析根：本模块经 :meth:`SourcesMixin._source_root` 取存储根，而编排把它**冻进**
输入快照的 ``source_root``（``JobRunner.build_payload``）交给阶段体。
不共用会得到一个很具体的坏结果——"试接入说通了、构建时说找不到"。
这也正是 ``ingest.loader`` 把 ``SourceGuard`` 定成唯一解析点的原因。

探测留痕不参与判定
------------------
``last_probe_ok`` / ``item_count`` 只是**留痕**：来源能不能用只由"现在探一次"回答。
但 ``None`` 与 ``False`` 必须区分——前者是"从没探过"，后者是"探了、没通"，
界面上一句"未探测"与一句"上次探测失败"要给不同的下一步动作。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import SourceConfig
from docfidelity.pipeline import (
    SourceItems,
    check_config,
    connector_names,
    load_items,
    path_segment,
    probe_source,
)

from .storage import FactSourceRecord

if TYPE_CHECKING:
    from .settings import ServerSettings
    from .storage import Repository

__all__ = [
    "SourceMixin",
    "SourceProbeResult",
]

#: 条目浏览器默认上限（转发给 ``pipeline.load_items``）。200 条足够铺满几屏，
#: 又不至于让"打开来源详情"变成一次几秒的白屏——截断了会如实标出来。
DEFAULT_ITEM_LIMIT: Final = 200


@dataclass(frozen=True)
class SourceProbeResult:
    """一次试接入的结果（``POST …/sources/{sid}/probe``）。"""

    source_id: str
    ok: bool
    detail: str
    item_count: int | None
    probed_at: datetime
    warnings: list[str]


class SourceMixin:
    """事实源声明与接入（``Services`` 的第五个 mixin，见 ``server.services``）。

    只**用** ``self.repository`` / ``self.settings`` / ``self.now``。
    """

    if TYPE_CHECKING:
        repository: Repository
        settings: ServerSettings
        now: Callable[[], datetime]

    # ---------------------------------------------------------------- 列表
    def list_sources(self, project_id: str) -> list[FactSourceRecord]:
        self.repository.get_project(project_id)
        return self.repository.list_sources(project_id)

    def get_source(self, project_id: str, source_id: str) -> FactSourceRecord:
        self.repository.get_project(project_id)
        return self._find_source(project_id, source_id)

    def source_types(self) -> list[str]:
        """可选接入器注册名（界面下拉用）。**是数据不是枚举**：注册表可由 entry point 扩充。"""
        return connector_names()

    # ---------------------------------------------------------------- 新增
    def add_source(
        self, project_id: str, *, config: SourceConfig, author: str | None = None
    ) -> tuple[FactSourceRecord, bool]:
        """登记一条来源声明，返回 ``(记录, 本次是否新建)``。

        只做**配置期**校验（``pipeline.sources.check_config``：接入器认不认识、
        ``root`` 解不解得开）——它不读文件内容，故不会因为"今天那份文件被移走了"
        而失败。真去读文件是 ``probe`` 的事，"能不能登记"与"现在通不通"是两回事。

        已存在同 ``source_id`` 时：配置**相同**即幂等（返回既有记录、``created=False``），
        不同则拒绝（``E_SOURCE_CONFIG_INVALID``）。不做静默覆盖的理由与模板登记一致
        ——"覆盖"要是一次有意识的选择（先删再增，或换个 id），而不是一次笔误的后果。
        """
        self.repository.get_project(project_id)
        source_id = (
            path_segment(config.source_id.strip(), field="source_id")
            if (config.source_id.strip())
            else ""
        )
        if source_id == "":
            raise EngineError(
                ErrorCode.SOURCE_CONFIG_INVALID,
                "来源缺少 source_id",
                where={"project_id": project_id},
                remediation=["给出 source_id：事实与冲突记录都引用它"],
            )

        problems = check_config(config, workspace_root=self._source_root())
        if problems:
            raise EngineError(
                ErrorCode.SOURCE_CONFIG_INVALID,
                f"来源声明不可用：{source_id}",
                where={
                    "project_id": project_id,
                    "source_id": source_id,
                    "source_type": config.source_type,
                    "problems": problems,
                    "known_types": connector_names(),
                },
                remediation=[
                    "按 problems 逐条修正后重新提交",
                    "source_type 取已登记的接入器名；root 需落在存储根之内",
                ],
            )

        existing = self.repository.find_source(project_id, source_id)
        if existing is not None:
            if existing.config == config:
                return existing, False
            raise EngineError(
                ErrorCode.SOURCE_CONFIG_INVALID,
                f"来源标识已被占用且配置不同：{source_id}",
                where={
                    "project_id": project_id,
                    "source_id": source_id,
                    "existing_source_type": existing.source_type,
                    "incoming_source_type": config.source_type,
                },
                remediation=[
                    "先删除该来源再重新声明（DELETE /projects/{pid}/sources/{sid}）",
                    "或换一个 source_id——它会被既有事实与追溯边引用",
                ],
            )

        return (
            self.repository.add_source(
                FactSourceRecord(
                    project_id=project_id,
                    config=config,
                    created_at=self.now(),
                    created_by=author,
                )
            ),
            True,
        )

    def remove_source(self, project_id: str, source_id: str) -> bool:
        """删除来源声明。

        **不影响历史产出**：事实包与追溯边里记的是 ``source_id``（一个字符串），
        不是这条记录。所以删掉来源只是"以后不再接这一份"，而不是"以前的文档作废"。
        """
        self.repository.get_project(project_id)
        found = self.repository.remove_source(
            project_id, path_segment(source_id, field="source_id")
        )
        if not found:
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到来源：{source_id}",
                where={"project_id": project_id, "source_id": source_id},
                remediation=["按 GET /projects/{pid}/sources 列出本项目已声明的来源"],
            )
        return True

    # ------------------------------------------------------------ 试接入
    def probe(self, project_id: str, source_id: str) -> SourceProbeResult:
        """试接入并**留下探测痕迹**（``POST …/sources/{sid}/probe``）。

        **永不抛错**（转发的是 ``pipeline.probe_source``，它转发的是
        ``ingest.probe_connector``）：这个端点服务于界面上的一个按钮，失败要显示成
        "这条来源现在连不通 + 为什么"，而不是一个 500。连"接入器名不认识"也是
        一条正常回答——那个名字正可能是使用者刚敲进去的。
        """
        entry = self.get_source(project_id, source_id)
        result = probe_source(entry.config, workspace_root=self._source_root())
        moment = self.now()
        updated = self.repository.save_source(
            replace(
                entry,
                last_probed_at=moment,
                last_probe_ok=result.ok,
                last_probe_detail=result.detail,
                item_count=result.item_count,
            )
        )
        return SourceProbeResult(
            source_id=updated.source_id,
            ok=result.ok,
            detail=result.detail,
            item_count=result.item_count,
            probed_at=moment,
            warnings=list(result.warnings),
        )

    # ---------------------------------------------------------- 条目浏览
    def source_items(
        self, project_id: str, source_id: str, *, limit: int = DEFAULT_ITEM_LIMIT
    ) -> SourceItems:
        """真接入一次，把条目取回来给界面看（``GET …/sources/{sid}/items``）。

        与 :meth:`probe` 的关键差别是**它会抛**：探针回答"能不能通"，条目浏览器回答
        "通的话里面是什么"，读不出来就是一次失败请求，要被显示成错误而不是一份空清单
        ——空清单会被读成"这份来源本来就没有对象"。

        具体抛哪个码由接入层定，三种都可能：护栏先于读文件把关（``E_UPLOAD_TYPE`` /
        ``E_UPLOAD_SIZE`` 等）、路径越界（``E_PATH_ESCAPE``）、接入器自己诊断出的内容
        问题（如 ``E_CONFIG_INVALID``，带文件与行列定位）。本层**不翻译**这些码：
        它们是"为什么读不出来"的唯一答案，转一道手就会变形。
        """
        entry = self.get_source(project_id, source_id)
        return load_items(entry.config, workspace_root=self._source_root(), limit=limit)

    # ---------------------------------------------------------------- 内部
    def _find_source(self, project_id: str, source_id: str) -> FactSourceRecord:
        found = self.repository.find_source(project_id, path_segment(source_id, field="source_id"))
        if found is None:
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到来源：{source_id}",
                where={"project_id": project_id, "source_id": source_id},
                remediation=["按 GET /projects/{pid}/sources 列出本项目已声明的来源"],
            )
        return found

    def _source_root(self) -> Path:
        """``root`` 的解析基点（见模块文档）。

        收在一个方法里而不是各处写 ``self.settings.storage_root``：构建期也要用同一个
        根（编排把它冻进 ``payload["source_root"]``），"改哪里"这个问题于是只有一个答案。
        """
        return self.settings.storage_root
