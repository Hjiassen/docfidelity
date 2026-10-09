"""``pipeline.stages``：阶段推进、断点复用、续跑与失败隔离。

这一轮验收判据是「作业可续跑」，所以续跑必须被**端到端**证明：跑到一半失败、
修好之后重跑，前两个阶段一次都不再执行，第三个阶段接着跑完。
只断言"重跑能成功"是拦不住的——那与"从头重跑一遍"在返回值上完全一样。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import (
    CorruptCheckpoint,
    DocumentRun,
    FileCheckpointStore,
    MemoryCheckpointStore,
    StageContext,
    StageOutcome,
    StageRegistry,
    Workspace,
    aggregate,
    plan_resume,
    rerun_failed,
    run_document,
    run_stage,
)
from tests.fixtures.pipeline_fixture import (
    ScriptedStage,
    all_stages,
    build_registry,
    clock,
    document_job,
)

pytestmark = pytest.mark.unit


def _workspace(tmp_path: Path, job_id: str = "j-doc-1") -> Workspace:
    return Workspace(tmp_path, job_id)


class TestStageRegistry:
    def test_resolve_returns_registered_function(self) -> None:
        stage = ScriptedStage("render")
        assert build_registry(stage).resolve("render") is stage

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = build_registry(ScriptedStage("render"))
        with pytest.raises(EngineError):
            registry.register("render", ScriptedStage("render"))

    def test_explicit_replace_allows_swapping_a_stage(self) -> None:
        # 换渲染策略、塞一个必失败的阶段，都不该改编排代码
        registry = build_registry(ScriptedStage("render"))
        replacement = ScriptedStage("render")
        registry.register("render", replacement, replace=True)
        assert registry.resolve("render") is replacement

    def test_unknown_stage_name_is_rejected(self) -> None:
        with pytest.raises(EngineError):
            build_registry(ScriptedStage("render")).register("polish", ScriptedStage("polish"))

    def test_unregistered_stage_fails_loudly(self) -> None:
        # 不能返回一个"空跑"的占位实现 —— 那会让作业悄悄少一个阶段
        with pytest.raises(EngineError) as caught:
            StageRegistry().resolve("render")
        assert caught.value.code == ErrorCode.CONFIG_INVALID

    def test_registered_and_missing_are_complementary(self) -> None:
        registry = build_registry(ScriptedStage("ingest"), ScriptedStage("qa"))
        assert registry.registered() == ("ingest", "qa")
        assert registry.missing() == ("compose", "render", "refresh")
        assert "qa" in registry and "render" not in registry


class TestWorkspace:
    def test_directories_follow_the_layout(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        assert workspace.work_dir == tmp_path / "jobs" / "j-doc-1" / "work"
        assert workspace.artifacts_dir == tmp_path / "jobs" / "j-doc-1" / "artifacts"
        assert workspace.inputs_dir == tmp_path / "jobs" / "j-doc-1" / "inputs"

    def test_resolve_rejects_escape(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as caught:
            _workspace(tmp_path).resolve("../evil.txt")
        assert caught.value.code == ErrorCode.PATH_ESCAPE

    def test_put_text_returns_relative_path_and_writes_file(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        relative = workspace.put_text("work/note.txt", "内容")
        assert relative == "work/note.txt"
        assert (tmp_path / "jobs" / "j-doc-1" / "work" / "note.txt").read_text(
            encoding="utf-8"
        ) == "内容"

    def test_json_round_trip(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        workspace.put_json("work/payload.json", {"b": 1, "a": [2, 3]})
        assert workspace.read_json("work/payload.json") == {"a": [2, 3], "b": 1}

    def test_bytes_round_trip(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        workspace.put_bytes("artifacts/out.bin", b"\x00\x01")
        assert (tmp_path / "jobs" / "j-doc-1" / "artifacts" / "out.bin").read_bytes() == b"\x00\x01"


class TestRunStage:
    def test_first_run_executes_and_writes_checkpoint(self, tmp_path: Path) -> None:
        stage = ScriptedStage("render")
        store = MemoryCheckpointStore()
        run = run_stage(
            job=document_job(),
            stage="render",
            registry=build_registry(stage),
            store=store,
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert run.status == "succeeded"
        assert run.executed and not run.reused
        assert stage.call_count == 1
        checkpoint = store.load("j-doc-1", "render")
        assert checkpoint is not None and checkpoint.inputs_hash == "h1"
        # 断点里的相对路径必须指向真文件（下游阶段按逻辑名取件）
        assert (_workspace(tmp_path).job_dir / run.outputs["render"]).is_file()
        assert run.record().status == "succeeded"
        assert run.record().progress == 1.0

    def test_fresh_run_records_the_checkpoint_it_just_wrote(self, tmp_path: Path) -> None:
        """**本次执行成功**也要报出断点引用。

        这一条是界面那一列的判据：``StageTimeline`` 用它回答"重跑会不会真的重跑"。
        若只在"沿用了断点"时才填，则每个刚跑完的成功的阶段都会被显示成"无断点"，
        使用者据此以为"重跑会重新执行一遍"——而磁盘上断点明明在。
        """
        run = run_stage(
            job=document_job(),
            stage="render",
            registry=build_registry(ScriptedStage("render")),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        ref = run.record().checkpoint
        assert ref is not None, "本次执行成功并落了断点，引用不得为空"
        assert ref.stage == "render"
        assert ref.inputs_hash == "h1", "必须填真实摘要，不能填占位串"

    def test_reused_run_records_the_same_ref(self, tmp_path: Path) -> None:
        stage = ScriptedStage("render")
        registry, store = build_registry(stage), MemoryCheckpointStore()
        workspace = _workspace(tmp_path)
        kwargs = {
            "job": document_job(),
            "stage": "render",
            "registry": registry,
            "store": store,
            "workspace": workspace,
            "inputs_hash": "h1",
            "now": clock,
        }
        first = run_stage(**kwargs)  # type: ignore[arg-type]
        second = run_stage(**kwargs)  # type: ignore[arg-type]
        assert second.record().checkpoint == first.record().checkpoint

    def test_failed_and_skipped_stages_report_no_checkpoint(self, tmp_path: Path) -> None:
        """失败与跳过都不落断点，故两处都不得报出引用（否则界面会说"重跑会复用"）。"""
        failed = run_stage(
            job=document_job(),
            stage="render",
            registry=build_registry(ScriptedStage("render", failures=1)),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        skipped = run_stage(
            job=document_job(),
            stage="refresh",
            registry=build_registry(ScriptedStage("refresh", skip_reason="无需刷新")),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert failed.record().checkpoint is None
        assert skipped.record().checkpoint is None

    def test_second_run_reuses_without_executing(self, tmp_path: Path) -> None:
        stage = ScriptedStage("render")
        registry, store = build_registry(stage), MemoryCheckpointStore()
        workspace = _workspace(tmp_path)
        kwargs = {
            "job": document_job(),
            "stage": "render",
            "registry": registry,
            "store": store,
            "workspace": workspace,
            "inputs_hash": "h1",
            "now": clock,
        }
        first = run_stage(**kwargs)  # type: ignore[arg-type]
        second = run_stage(**kwargs)  # type: ignore[arg-type]
        assert stage.call_count == 1, "有效断点必须零执行"
        assert second.reused and not second.executed
        assert second.outputs == first.outputs
        assert second.reuse.explain() == "断点有效，沿用产出"

    def test_changed_inputs_force_a_rerun(self, tmp_path: Path) -> None:
        stage = ScriptedStage("render")
        registry, store = build_registry(stage), MemoryCheckpointStore()
        workspace = _workspace(tmp_path)
        base = {
            "job": document_job(),
            "stage": "render",
            "registry": registry,
            "store": store,
            "workspace": workspace,
            "now": clock,
        }
        run_stage(**base, inputs_hash="h1")  # type: ignore[arg-type]
        rerun = run_stage(**base, inputs_hash="h2")  # type: ignore[arg-type]
        assert stage.call_count == 2
        assert rerun.reuse.reason == "inputs-changed"

    def test_failure_writes_no_checkpoint(self, tmp_path: Path) -> None:
        stage = ScriptedStage("render", failures=1)
        store = MemoryCheckpointStore()
        run = run_stage(
            job=document_job(),
            stage="render",
            registry=build_registry(stage),
            store=store,
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert run.status == "failed"
        assert run.error is not None and run.error.message.startswith("注入的失败")
        assert store.load("j-doc-1", "render") is None, "失败不是产出，不得落断点"

    def test_skip_is_not_a_failure_and_writes_no_checkpoint(self, tmp_path: Path) -> None:
        stage = ScriptedStage("refresh", skip_reason="目录无需刷新")
        store = MemoryCheckpointStore()
        run = run_stage(
            job=document_job(),
            stage="refresh",
            registry=build_registry(stage),
            store=store,
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert run.status == "skipped"
        assert run.skipped_reason == "目录无需刷新"
        # 不适用没有产出，故不落断点；下次会被重新判定（判定是廉价的）
        assert store.load("j-doc-1", "refresh") is None
        assert run.record().status == "skipped"

    def test_engine_error_becomes_a_structured_failure(self, tmp_path: Path) -> None:
        def _raising(context: StageContext) -> StageOutcome:
            raise EngineError(
                ErrorCode.PRECHECK_FAILED, "预检未通过", where={"stage": context.stage}
            )

        run = run_stage(
            job=document_job(),
            stage="compose",
            registry=StageRegistry({"compose": _raising}),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert run.status == "failed"
        assert run.error is not None and run.error.code == ErrorCode.PRECHECK_FAILED

    def test_bare_exception_is_contained_by_the_orchestrator(self, tmp_path: Path) -> None:
        stage = ScriptedStage("render", raises=ValueError("阶段体里的 bug"))
        run = run_stage(
            job=document_job(),
            stage="render",
            registry=build_registry(stage),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert run.status == "failed"
        assert run.error is not None and run.error.code == ErrorCode.INTERNAL

    def test_corrupt_checkpoint_is_reported_and_the_stage_reruns(self, tmp_path: Path) -> None:
        class _Corrupting(MemoryCheckpointStore):
            """读断点必抛 ``CorruptCheckpoint``：模拟盘上的断点坏了。"""

            def load(self, job_id: str, stage: str) -> None:
                raise CorruptCheckpoint(Path("jobs") / job_id / "work" / f"{stage}.json", "坏掉了")

        stage = ScriptedStage("render")
        run = run_stage(
            job=document_job(),
            stage="render",
            registry=build_registry(stage),
            store=_Corrupting(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        # 坏断点不得被当成"有效"复用：重跑，并留一条可追溯的判定
        assert run.status == "succeeded"
        assert stage.call_count == 1
        assert [item.rule_id for item in run.findings] == ["FR-M7-04"]
        assert run.findings[0].severity == "major"


class TestRunDocument:
    def test_all_stages_succeed_in_order(self, tmp_path: Path) -> None:
        stages = all_stages()
        run = run_document(
            job=document_job(),
            registry=build_registry(*stages),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert isinstance(run, DocumentRun)
        assert [item.stage for item in run.runs] == ["ingest", "compose", "render", "refresh", "qa"]
        assert run.status == "succeeded"
        assert run.stopped_at is None
        assert run.ran() == ("ingest", "compose", "render", "refresh", "qa")
        assert run.reused() == ()

    def test_failure_stops_the_pipeline_and_keeps_upstream_checkpoints(
        self, tmp_path: Path
    ) -> None:
        stages = all_stages(render={"failures": 1})
        store = MemoryCheckpointStore()
        run = run_document(
            job=document_job(),
            registry=build_registry(*stages),
            store=store,
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert run.status == "failed"
        assert run.stopped_at == "render"
        assert run.ran() == ("ingest", "compose", "render")
        # 失败隔离：上游断点原样保留，下游不再执行
        assert set(store.stages("j-doc-1")) == {"ingest", "compose"}
        assert stages[3].call_count == 0 and stages[4].call_count == 0

    def test_resume_from_the_failed_stage(self, tmp_path: Path) -> None:
        stages = all_stages(render={"failures": 1})
        registry, store = build_registry(*stages), MemoryCheckpointStore()
        workspace = _workspace(tmp_path)
        base = {
            "job": document_job(),
            "registry": registry,
            "store": store,
            "workspace": workspace,
            "inputs_hash": "h1",
            "now": clock,
        }
        first = run_document(**base)  # type: ignore[arg-type]
        assert first.status == "failed"

        # 上游断点让"从哪里续跑"可被算出
        assert plan_resume(job_id="j-doc-1", store=store, inputs_hash="h1") == (
            "render",
            "refresh",
            "qa",
        )

        stages[2].failures = 0  # 修好
        second = run_document(**base)  # type: ignore[arg-type]
        assert second.status == "succeeded"
        assert second.reused() == ("ingest", "compose")
        assert second.ran() == ("render", "refresh", "qa")
        assert stages[0].call_count == 1 and stages[1].call_count == 1, "上游不得重跑"
        assert stages[2].call_count == 2, "失败阶段续跑时重试一次"

    def test_outputs_of_reused_stages_survive_the_resume(self, tmp_path: Path) -> None:
        stages = all_stages(render={"failures": 1})
        registry, store = build_registry(*stages), MemoryCheckpointStore()
        workspace = _workspace(tmp_path)
        base = {
            "job": document_job(),
            "registry": registry,
            "store": store,
            "workspace": workspace,
            "inputs_hash": "h1",
            "now": clock,
        }
        first = run_document(**base)  # type: ignore[arg-type]
        stages[2].failures = 0
        second = run_document(**base)  # type: ignore[arg-type]
        # 上游是"沿用产出"而不是"重新产出"，断点里的路径必须原样带过来
        assert second.outputs()["ingest"] == first.outputs()["ingest"]
        assert second.outputs()["compose"] == first.outputs()["compose"]
        assert set(second.outputs()) == {"ingest", "compose", "render", "refresh", "qa"}

    def test_plan_resume_is_empty_after_a_full_success(self, tmp_path: Path) -> None:
        store = MemoryCheckpointStore()
        run_document(
            job=document_job(),
            registry=build_registry(*all_stages()),
            store=store,
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert plan_resume(job_id="j-doc-1", store=store, inputs_hash="h1") == ()

    def test_stage_records_are_contract_stages(self, tmp_path: Path) -> None:
        store = MemoryCheckpointStore()
        run = run_document(
            job=document_job(),
            registry=build_registry(*all_stages()),
            store=store,
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        records = run.stage_records()
        assert [record.name for record in records] == list(run.ran())
        assert all(record.status == "succeeded" for record in records)

    def test_resume_survives_a_reopened_file_store(self, tmp_path: Path) -> None:
        """模拟"服务重启"：断点从盘上重新读出来后，续跑仍从失败阶段开始。

        这是 FR-M7-04 / 3.2 场景 G 的核心形态——内存仓储下测出来的续跑，
        在进程重启这件事上一无所知。故这里必须走真盘。
        """
        stages = all_stages(compose={"failures": 1})
        workspace = _workspace(tmp_path)
        base = {
            "job": document_job(),
            "registry": build_registry(*stages),
            "workspace": workspace,
            "inputs_hash": "h1",
            "now": clock,
        }
        first = run_document(store=FileCheckpointStore(tmp_path), **base)  # type: ignore[arg-type]
        assert first.status == "failed"
        assert first.stopped_at == "compose"
        assert first.ran() == ("ingest", "compose")

        # 进程重启：只从盘上重建仓储
        reopened = FileCheckpointStore(tmp_path)
        assert set(reopened.stages("j-doc-1")) == {"ingest"}
        assert plan_resume(job_id="j-doc-1", store=reopened, inputs_hash="h1") == (
            "compose",
            "render",
            "refresh",
            "qa",
        )

        stages[1].failures = 0
        second = run_document(store=reopened, **base)  # type: ignore[arg-type]
        assert second.status == "succeeded"
        assert second.reused() == ("ingest",)
        assert second.ran() == ("compose", "render", "refresh", "qa")
        assert stages[0].call_count == 1, "重启后上游不得重跑"


class TestOrchestrationFlow:
    """编排作业层面：单份失败不牵连其余（FR-M7-15 / FR-M7-16）。"""

    def test_one_child_failing_does_not_touch_the_other(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        good, bad = all_stages(), all_stages(render={"failures": 1})

        first = run_document(
            job=document_job("c1"),
            registry=build_registry(*good),
            store=store,
            workspace=Workspace(tmp_path, "c1"),
            inputs_hash="h1",
            now=clock,
        )
        second = run_document(
            job=document_job("c2"),
            registry=build_registry(*bad),
            store=store,
            workspace=Workspace(tmp_path, "c2"),
            inputs_hash="h1",
            now=clock,
        )
        assert first.status == "succeeded"
        assert second.status == "failed"

        children = [
            document_job("c1", status=first.status),
            document_job("c2", status=second.status),
        ]
        # 单份失败 → 编排是 partial_failure，而不是 failed：成功那份的产物仍然呈现
        assert aggregate(children) == "partial_failure"

        reset = rerun_failed(children)
        assert [child.job_id for child in reset] == ["c2"], "只重跑失败的子作业"
        assert children[0].status == "succeeded", "已成功的子作业不得被改动"

        bad[2].failures = 0
        resumed = run_document(
            job=document_job("c2", status="running"),
            registry=build_registry(*bad),
            store=store,
            workspace=Workspace(tmp_path, "c2"),
            inputs_hash="h1",
            now=clock,
        )
        assert resumed.status == "succeeded"
        # c2 从失败阶段续跑；c1 的断点与产物完全没被动过
        assert resumed.reused() == ("ingest", "compose")
        assert set(store.stages("c1")) == {"ingest", "compose", "render", "refresh", "qa"}
        assert good[0].call_count == 1
