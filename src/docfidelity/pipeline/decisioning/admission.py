"""章节放置的独立安全校验；语义正确性由同策略基线提供证据。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from docfidelity.contracts.decision_outputs import PlacementChoice
from docfidelity.platform.hashing import digest_payload

if TYPE_CHECKING:
    from .routing import AdapterOutcome, DecisionRequest, ResolvedRoute


@dataclass(frozen=True)
class AdmissionResult:
    accepted: bool
    reason: str
    evidence: dict[str, Any]
    chosen: str | None = None


def admit_model(
    route: ResolvedRoute, request: DecisionRequest, outcome: AdapterOutcome
) -> AdmissionResult:
    """校验准入约束；不把 schema 合法或候选合法当成语义正确。"""
    evidence: dict[str, Any] = {"policy": route.admission_policy, "accepted": False}
    if not outcome.ok:
        reason = outcome.detail or outcome.error or "模型调用失败"
        return AdmissionResult(False, reason, evidence)
    if not outcome.schema_ok:
        reason = f"输出不符合 Schema：{outcome.detail}" if outcome.detail else "输出不符合 Schema"
        return AdmissionResult(False, reason, evidence)

    chosen = outcome.chosen
    if request.decision_point == "placement.section_slot":
        placement = _validate_placement(request, outcome, route.admission_policy)
        evidence.update(placement.evidence)
        if not placement.accepted:
            return AdmissionResult(False, placement.reason, evidence)
        chosen = placement.chosen

    if route.admission_policy == "require_confidence":
        if outcome.confidence is None:
            return AdmissionResult(False, "模型未提供置信度，必须待确认", evidence, chosen)
        if outcome.confidence < route.confidence_threshold:
            return AdmissionResult(
                False,
                f"置信度 {outcome.confidence} 低于阈值 {route.confidence_threshold}",
                evidence,
                chosen,
            )
        reason = "模型结论通过安全约束校验且真实置信度达标"
    else:
        reason = "章节放置通过独立安全约束校验；语义正确性由同策略基线门禁验证"
    evidence["accepted"] = True
    return AdmissionResult(True, reason, evidence, chosen)


def _path(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(part, str) and part.strip() for part in value):
        return None
    return value


def _validate_placement(
    request: DecisionRequest, outcome: AdapterOutcome, policy: str
) -> AdmissionResult:
    evidence: dict[str, Any] = {
        "validator": "placement_constraints_v1",
        "semantic_correctness": "baseline_required",
    }

    def reject(reason: str) -> AdmissionResult:
        return AdmissionResult(False, reason, evidence)

    try:
        choice = PlacementChoice.model_validate(outcome.payload, strict=True)
    except ValidationError:
        return reject("章节放置输出不符合 PlacementChoice Schema")
    if not request.target or choice.unit_ref != request.target:
        return reject("章节放置 unit_ref 与请求 target 不一致")
    evidence["unit_ref"] = choice.unit_ref
    if _path(choice.chapter_path) is None or not request.candidates:
        return reject("章节放置必须提供非空路径和候选集")
    supplied_paths = request.context.get("chapter_paths", request.inputs.get("chapter_paths"))
    if supplied_paths is not None and (
        not isinstance(supplied_paths, list)
        or any(_path(path) is None for path in supplied_paths)
        or choice.chapter_path not in supplied_paths
    ):
        return reject("章节放置路径不在完整候选路径集中")

    constraints = request.context.get("placement_constraints")
    if constraints is not None:
        if not isinstance(constraints, Mapping) or not constraints:
            return reject("独立校验缺少冻结的兼容槽位约束")
        if not all(
            isinstance(label, str) and _path(path) is not None
            for label, path in constraints.items()
        ):
            return reject("冻结的兼容槽位约束格式不合法")
        evidence["constraints_digest"] = digest_payload(dict(constraints))
        matches = [
            label for label in request.candidates if constraints.get(label) == choice.chapter_path
        ]
    elif policy == "independent_validation":
        return reject("独立校验需要冻结的 placement_constraints 兼容槽位约束")
    else:
        if supplied_paths is None:
            supplied_paths = request.inputs.get("chapters", request.candidates)
        if not isinstance(supplied_paths, list):
            return reject("章节候选路径格式不合法")
        paths = [[item] if isinstance(item, str) else item for item in supplied_paths]
        if any(_path(path) is None for path in paths) or choice.chapter_path not in paths:
            return reject("章节放置路径不在完整候选路径集中")
        label = " / ".join(choice.chapter_path)
        matches = [label] if label in request.candidates else []
    if len(matches) != 1:
        return reject("章节放置路径必须精确对应一个已提供的候选与兼容槽位")
    evidence["candidate"] = matches[0]
    evidence["chapter_path"] = list(choice.chapter_path)
    return AdmissionResult(True, "章节放置安全约束通过", evidence, matches[0])
