"""产物用例：项目级聚合、取件、两两比对、整包导出（P11 产物与对比）。

为什么从 ``Services`` 拆出来
----------------------------
``Services`` 到 640 行上下，而 P11 要在这上面再加三处（项目级聚合 / 比对 / 整包导出）。
拆的分界线是**职责而不是行数**：这一族方法全部只碰产物（``artifact_ids`` → 仓储 →
工作区文件），不碰配置、不碰裁决、不碰阶段，所以它们能独立成一块，且不需要向别的
mixin 伸手。反过来，"项目总览"那类跨域聚合留在 ``Services`` 上——它天然要调裁决聚合
与前置检查，塞进 mixin 会打破"每个 mixin 只用宿主字段"的边界（见 ``services`` 模块）。

比对为什么传 id 而不传路径
--------------------------
界面手里只有产物标识。让它先查一次路径再调比对，等于把"这两个 id 对应哪个文件"
这条规则放到浏览器里；而那条规则里已经含一条业务判定（保留期过了要报
``E_ARTIFACT_EXPIRED``，不是"文件不存在"）。

整包导出为什么在服务端打
------------------------
"整套下载"要一次给出某次编排的全部产物、且**不含**工作区里的中间件
（``inputs/`` / ``checkpoints/`` / 事件日志）。让界面逐份下载再自己压，多出来的不只是
往返：它会把"哪些文件算产物"这个判定复制到客户端。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Artifact, Job, JobKind, JobStatus
from docfidelity.contracts.qa import Verdict
from docfidelity.pipeline import DocumentDiff, compare_documents
from docfidelity.platform.fs import zip_directory

if TYPE_CHECKING:
    from .jobs import JobRunner
    from .settings import ServerSettings
    from .storage import Repository

__all__ = [
    "ArtifactComparison",
    "ArtifactGroup",
    "ArtifactMixin",
    "ProjectArtifacts",
]


@dataclass(frozen=True)
class ArtifactGroup:
    """一层产物分组（需求 P11：按编排作业 / 子作业 / 文档三层分组）。

    ``children`` 只对编排作业非空；子作业自身的 ``doc_key`` 就是"文档"那一层的名字
    ——不再单独造一个"文档组"：一份子作业恰好对应一份文档（1:1），多一层会让界面
    出现一个永远只有一个孩子的层级。

    ``kind`` / ``status`` 直接取作业契约上的 ``JobKind`` / ``JobStatus``（不是 ``str``）：
    这两个值就是本组的作业那两列，写成 ``str`` 会让接口形状丢掉取值集合，前端只好
    自己写一份"状态 → 中文"的表，而那份表与 ``zh.job.status`` 迟早分叉。

    ``report_verdict`` 是**服务端事实**而不是界面的推断：``None`` = 这份作业没有门禁报告
    （需求 P11 的"该产物未经门禁，仅作草稿"就是这个情形）。让界面按"作业状态不是
    succeeded"之类去猜，等于把一条业务判据放进浏览器——而且它猜错的方式很糟：
    猜成"有报告"会让草稿看起来像成品。
    """

    job_id: str
    kind: JobKind
    doc_key: str | None
    status: JobStatus
    created_at: datetime
    artifacts: tuple[Artifact, ...] = ()
    children: tuple[ArtifactGroup, ...] = ()
    report_verdict: Verdict | None = None


@dataclass(frozen=True)
class ProjectArtifacts:
    """某项目的产物总览。``groups`` 按创建时间**倒序**（最近的排最前）。"""

    project_id: str
    groups: tuple[ArtifactGroup, ...] = ()
    #: 产物**条目数**与总字节。给"当前占用"那一处用；界面不做求和（NFR-22）。
    count: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class ArtifactComparison:
    """一次比对的结果：差异 + 参与比对的两份产物。"""

    diff: DocumentDiff
    baseline: Artifact
    candidate: Artifact


class ArtifactMixin:
    """产物族用例（``Services`` 的第七个 mixin，见 ``server.services``）。

    只**用** ``self.repository`` / ``self.runner`` / ``self.now``。
    """

    if TYPE_CHECKING:
        repository: Repository
        runner: JobRunner
        settings: ServerSettings
        now: Callable[[], datetime]

    # ---------------------------------------------------------------- 取件
    def list_artifacts(self, job_id: str) -> list[Artifact]:
        self.repository.get_job(job_id)
        return self.repository.list_artifacts(job_id)

    def artifact_path(self, artifact_id: str) -> Path:
        """产物取件路径；过期报 ``E_ARTIFACT_EXPIRED``（410），文件缺失报 ``E_NOT_FOUND``。"""
        artifact = self.repository.get_artifact(artifact_id)
        if artifact.expires_at is not None and artifact.expires_at <= self.now():
            raise EngineError(
                ErrorCode.ARTIFACT_EXPIRED,
                f"产物已过保留期：{artifact.artifact_id}",
                where={
                    "artifact_id": artifact.artifact_id,
                    "expires_at": artifact.expires_at.isoformat(),
                },
                remediation=["重新发起构建以生成新产物"],
            )
        path = self.runner.workspace(artifact.job_id).resolve(artifact.relative_path)
        if not path.is_file():
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"产物文件不存在：{artifact.relative_path}",
                where={"artifact_id": artifact_id, "relative_path": artifact.relative_path},
                remediation=["重新发起构建", "或检查维护清理是否已回收该产物"],
            )
        return path

    # ---------------------------------------------------------------- 聚合
    def project_artifacts(self, project_id: str) -> ProjectArtifacts:
        """项目级产物聚合（P11 的列表）。

        跳过**一份产物都没有的作业**：一次失败得早的作业会留下一行空分组，而一行
        "这里什么都没有"与"这份文档还没跑"在界面上长得一样，下一步动作却完全不同
        （前者去 P09 看失败原因，后者去 P08 发起构建）。

        **子作业不再单独起一行**。``list_jobs(project_id=...)`` 把子作业一并返回，而子作业
        已经挂在父作业的 ``children`` 里了；照单全收会让同一批产物在页面上出现两次、让
        ``count`` 与 ``total_bytes`` 直接翻倍（前者看起来只是"多了一个分组"，后者是一句
        假话）。父作业不在这次返回里时（按状态过滤过、或父作业已被清理）仍然单独列出，
        沿用 ``P08`` 对孤儿作业的同一条处理。

        门禁结论取 ``list_reports()`` 一次做成索引，而不是每个分组各查一次：报告仓储的
        方法已经按 ``job_id`` 可查，但"每个分组一次查询"会让这一页的查询数随作业数线性
        增长，而它要的只是一个 ``job_id → verdict`` 的字典。
        """
        self.repository.get_project(project_id)
        verdicts = {report.job_id: report.verdict for report in self.repository.list_reports()}
        jobs = self.repository.list_jobs(project_id=project_id)
        present = {job.job_id for job in jobs}
        groups: list[ArtifactGroup] = []
        count = 0
        total = 0
        for job in jobs:
            if job.parent_job_id is not None and job.parent_job_id in present:
                continue
            own = tuple(self.repository.list_artifacts(job.job_id))
            children: list[ArtifactGroup] = []
            for child_id in job.child_job_ids:
                child = self.repository.find_job(child_id)
                if child is None:
                    continue
                child_artifacts = tuple(self.repository.list_artifacts(child_id))
                if not child_artifacts:
                    continue
                children.append(
                    _group_of(child, child_artifacts, report_verdict=verdicts.get(child_id))
                )
            if not own and not children:
                continue
            # 顶层分组只构造一次：计数与列表用的必须是**同一个**对象，否则"计数口径"与
            # "渲染口径"变成两处代码（下一处改动只会改中其中一处）。
            top = _group_of(
                job,
                own,
                children=tuple(children),
                report_verdict=verdicts.get(job.job_id),
            )
            for group in (top, *children):
                count += len(group.artifacts)
                total += sum(item.size_bytes for item in group.artifacts)
            groups.append(top)
        groups.sort(key=lambda item: item.created_at, reverse=True)
        return ProjectArtifacts(
            project_id=project_id,
            groups=tuple(groups),
            count=count,
            total_bytes=total,
        )

    # ---------------------------------------------------------------- 比对
    def artifact_compare(
        self,
        *,
        baseline_id: str,
        candidate_id: str,
        mode: str = "structure",
    ) -> ArtifactComparison:
        """两份产出的稳定化比对（P11 的"版本对比"）。

        两份都必须是 docx：比对结构 / 文本的对象就是文档包本身，拿一份 PNG 目检图来比
        没有意义。这条判定放服务端，而不是靠"界面下拉框只列 docx"——下拉框拦不住的
        调用（例如直接打 API 的脚本）会拿到一份**空的差异报告**，而空差异等于
        "两份一模一样"，那是一句假话。
        """
        first = self._comparable(baseline_id)
        second = self._comparable(candidate_id)
        diff = compare_documents(
            self.artifact_path(first.artifact_id),
            self.artifact_path(second.artifact_id),
            mode=mode,
            baseline_ref=first.artifact_id,
            candidate_ref=second.artifact_id,
        )
        return ArtifactComparison(diff=diff, baseline=first, candidate=second)

    def _comparable(self, artifact_id: str) -> Artifact:
        artifact = self.repository.get_artifact(artifact_id)
        if artifact.kind != "docx":
            raise EngineError(
                ErrorCode.SCHEMA_INVALID,
                f"该产物不是可比的文档：{artifact.kind}",
                where={"artifact_id": artifact_id, "kind": artifact.kind},
                remediation=["只比对 kind=docx 的产物", "目检图请在渲染目检区查看"],
            )
        return artifact

    # ---------------------------------------------------------------- 导出
    def artifact_package(self, job_id: str) -> tuple[str, bytes]:
        """整包导出（zip），返回 ``(建议文件名, 字节)``。

        走 ``platform.fs.zip_directory`` 的唯一实现：条目排序 + 时刻钉死，
        故同一批产物导出两次字节相同——交付归档最怕的就是"同样的东西两次哈希不一样"。
        """
        job = self.repository.get_job(job_id)
        directory = self.runner.workspace(job_id).artifacts_dir
        if not directory.is_dir() or not any(directory.iterdir()):
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"该作业没有可导出的产物：{job_id}",
                where={"job_id": job_id},
                remediation=["等待作业完成", "或到 P09 查看该作业停在哪个阶段"],
            )
        name = self.repository.get_project(job.project_id).name
        return f"{name}-{job_id}.zip", zip_directory(directory)


def _group_of(
    job: Job,
    artifacts: tuple[Artifact, ...],
    *,
    children: tuple[ArtifactGroup, ...] = (),
    report_verdict: Verdict | None = None,
) -> ArtifactGroup:
    return ArtifactGroup(
        job_id=job.job_id,
        kind=job.kind,
        doc_key=job.doc_key,
        status=job.status,
        created_at=job.created_at,
        artifacts=artifacts,
        children=children,
        report_verdict=report_verdict,
    )
