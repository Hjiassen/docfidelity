"""规则关闭姿态与本机受限解码能力，无模型 SDK 构造。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.decisions import AdapterOutcome, DecisionRequest, GrammarCapability
from docfidelity.contracts.errors import ErrorCode

GRAMMAR_BACKENDS: Final = ("xgrammar", "outlines")

__all__ = ["NullAdapter", "probe_grammar"]


def probe_grammar(*, prober: Callable[[], GrammarCapability] | None = None) -> GrammarCapability:
    """探测受限解码能力。默认探本机是否装了已知后端。

    探测失败**不是错误**：它只改变降级路径（"校验 → 重试 ≤ 2 → 规则兜底"），
    而那条路径本来就必须长期存在（DD-12.4 的兜底行）。
    """
    if prober is not None:
        return prober()
    found: list[str] = []
    for name in GRAMMAR_BACKENDS:
        try:
            __import__(name)
        except ImportError:
            continue
        found.append(name)
    if found:
        return GrammarCapability(
            supported=True, backend=found[0], detail=f"检出受限解码后端：{', '.join(found)}"
        )
    return GrammarCapability(
        supported=False,
        backend=None,
        detail="未检出受限解码后端，标记 grammar_disabled 并降级为「校验 + 重试 + 规则兜底」",
    )


class NullAdapter:
    """纯确定性模式（``DF_MODEL_ENABLED=false`` / 未配置模型）。

    它**不是**一个"没接好的适配层"，而是一个正式实现：模型总开关关闭时全链路走规则，
    产出仍完整（验收 39）、token 用量为 0（NFR-18）。
    """

    def available(self) -> bool:
        return False

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "null",
            "available": False,
            "model": None,
            "grammar_supported": None,
            "detail": "纯确定性模式：模型关闭，全链路走规则默认值",
        }

    def call(
        self, request: DecisionRequest, output_type: type[FormatModel] | None
    ) -> AdapterOutcome:
        return AdapterOutcome(
            ok=False,
            schema_ok=False,
            error=ErrorCode.MODEL_UNAVAILABLE,
            detail="模型未启用（纯确定性模式），按规则默认值处理",
        )
