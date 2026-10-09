"""``pipeline.checkpoint``：复用判定与两种仓储。

这一层是"续跑可信不可信"的唯一开关，所以每条判定都要**双向**断言：
该复用的复用、不该复用的绝不复用（版本变了、输入变了、断点存错位置、文件坏了）。
只测"有效断点能复用"是拦不住"永远复用"的实现的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.job import STAGE_IMPL_VERSION, StageCheckpoint
from docfidelity.pipeline import (
    CHECKPOINT_SUFFIX,
    CorruptCheckpoint,
    FileCheckpointStore,
    MemoryCheckpointStore,
    ReuseDecision,
    decide_reuse,
    impl_version_of,
    path_segment,
)

pytestmark = pytest.mark.unit


def _checkpoint(
    stage: str = "render", *, version: int | None = None, inputs_hash: str = "h1"
) -> StageCheckpoint:
    return StageCheckpoint(
        stage=stage,
        inputs_hash=inputs_hash,
        outputs={"out": f"{stage}/out.docx"},
        version=STAGE_IMPL_VERSION[stage] if version is None else version,
    )


class TestDecideReuse:
    def test_missing_checkpoint_must_run(self) -> None:
        decision = decide_reuse(None, stage="render", inputs_hash="h1")
        assert decision == ReuseDecision("missing")
        assert decision.must_run and not decision.reusable

    def test_matching_checkpoint_is_reusable(self) -> None:
        decision = decide_reuse(_checkpoint(), stage="render", inputs_hash="h1")
        assert decision.reusable
        assert decision.checkpoint is not None
        assert decision.checkpoint.outputs == {"out": "render/out.docx"}

    def test_version_bump_invalidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 阶段实现改了口径 —— 旧断点必须失效，否则续跑会给出"语义错误的绿"
        previous_version = STAGE_IMPL_VERSION["render"]
        monkeypatch.setitem(STAGE_IMPL_VERSION, "render", previous_version + 1)
        decision = decide_reuse(
            _checkpoint(version=previous_version), stage="render", inputs_hash="h1"
        )
        assert decision.reason == "version-mismatch"
        assert decision.must_run

    def test_inputs_change_invalidates(self) -> None:
        decision = decide_reuse(_checkpoint(inputs_hash="old"), stage="render", inputs_hash="new")
        assert decision.reason == "inputs-changed"
        assert decision.must_run

    def test_checkpoint_of_another_stage_is_treated_as_missing(self) -> None:
        # 断点记的是"render"，却在问"compose" —— 与没有断点同义
        decision = decide_reuse(_checkpoint(stage="render"), stage="compose", inputs_hash="h1")
        assert decision.reason == "missing"

    def test_priority_is_missing_then_version_then_inputs(self) -> None:
        # 版本不匹配优先于输入不匹配：两条都对不上时，界面要说"实现变了"
        decision = decide_reuse(
            _checkpoint(version=99, inputs_hash="old"), stage="render", inputs_hash="new"
        )
        assert decision.reason == "version-mismatch"

    def test_every_reason_has_a_human_readable_explanation(self) -> None:
        for reason in ("missing", "version-mismatch", "inputs-changed", "reusable"):
            assert ReuseDecision(reason).explain()  # type: ignore[arg-type]

    def test_unknown_stage_has_no_impl_version(self) -> None:
        with pytest.raises(ConfigurationError):
            impl_version_of("polish")


class TestPathSegment:
    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b"])
    def test_rejects_non_single_segment(self, bad: str) -> None:
        with pytest.raises(ConfigurationError):
            path_segment(bad, field="job_id")

    def test_accepts_ordinary_identifier(self) -> None:
        assert path_segment("j-1042_c1", field="job_id") == "j-1042_c1"


class TestMemoryStore:
    def test_round_trip(self) -> None:
        store = MemoryCheckpointStore()
        assert store.load("j1", "render") is None
        store.save("j1", _checkpoint())
        loaded = store.load("j1", "render")
        assert loaded is not None and loaded.outputs == {"out": "render/out.docx"}

    def test_save_overwrites_same_stage(self) -> None:
        store = MemoryCheckpointStore()
        store.save("j1", _checkpoint(inputs_hash="h1"))
        store.save("j1", _checkpoint(inputs_hash="h2"))
        loaded = store.load("j1", "render")
        assert loaded is not None and loaded.inputs_hash == "h2"

    def test_stages_are_scoped_to_job(self) -> None:
        store = MemoryCheckpointStore()
        store.save("j1", _checkpoint("render"))
        store.save("j2", _checkpoint("compose"))
        assert set(store.stages("j1")) == {"render"}
        assert set(store.stages("j2")) == {"compose"}


class TestFileStore:
    def test_round_trip_and_layout(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        store.save("j1", _checkpoint())
        path = store.path_for("j1", "render")
        assert path == tmp_path / "jobs" / "j1" / "work" / f"render{CHECKPOINT_SUFFIX}"
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["stage"] == "render"
        assert payload["outputs"] == {"out": "render/out.docx"}

    def test_load_absent_returns_none(self, tmp_path: Path) -> None:
        assert FileCheckpointStore(tmp_path).load("j1", "render") is None

    def test_save_leaves_no_temporary_files(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        store.save("j1", _checkpoint())
        leftovers = [p for p in tmp_path.rglob("*") if p.name.endswith(".dftmp")]
        assert leftovers == []

    def test_overwrite_replaces_previous_content(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        store.save("j1", _checkpoint(inputs_hash="h1"))
        store.save("j1", _checkpoint(inputs_hash="h2"))
        loaded = store.load("j1", "render")
        assert loaded is not None and loaded.inputs_hash == "h2"

    def test_stages_enumerates_every_recorded_stage(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        store.save("j1", _checkpoint("render"))
        store.save("j1", _checkpoint("qa"))
        assert set(store.stages("j1")) == {"render", "qa"}

    def test_broken_json_raises_corrupt_not_silent_rerun(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        path = store.path_for("j1", "render")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(CorruptCheckpoint) as caught:
            store.load("j1", "render")
        # 坏断点是"重跑"，不是"作业失败"
        assert caught.value.fatal is False
        assert caught.value.to_info().where["path"] == str(path)

    def test_non_object_payload_raises_corrupt(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        path = store.path_for("j1", "render")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(CorruptCheckpoint):
            store.load("j1", "render")

    def test_contract_violation_raises_corrupt(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        path = store.path_for("j1", "render")
        path.parent.mkdir(parents=True, exist_ok=True)
        # version 缺失 —— 契约校验失败，不得当成"有效断点"
        path.write_text(json.dumps({"stage": "render", "inputs_hash": "h1"}), encoding="utf-8")
        with pytest.raises(CorruptCheckpoint):
            store.load("j1", "render")

    def test_job_id_cannot_escape_the_root(self, tmp_path: Path) -> None:
        store = FileCheckpointStore(tmp_path)
        with pytest.raises(ConfigurationError):
            store.save("../evil", _checkpoint())

    def test_file_and_memory_stores_agree(self, tmp_path: Path) -> None:
        # 两个实现的判定必须一致：否则"测试用内存、生产用文件"会给出两个结论
        memory = MemoryCheckpointStore()
        disk = FileCheckpointStore(tmp_path)
        memory.save("j1", _checkpoint("qa"))
        disk.save("j1", _checkpoint("qa"))
        assert memory.load("j1", "qa").outputs == disk.load("j1", "qa").outputs  # type: ignore[union-attr]
        assert decide_reuse(memory.load("j1", "qa"), stage="qa", inputs_hash="h1") == decide_reuse(
            disk.load("j1", "qa"), stage="qa", inputs_hash="h1"
        )
