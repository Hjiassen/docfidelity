"""通过发行入口点发现模型实现；内核只依赖适配器协议。"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import cast

from docfidelity.contracts.decisions import ModelAdapter

from .decisioning.defaults import NullAdapter

MODEL_ADAPTER_GROUP = "docfidelity.model_adapters"


def create_model_adapter(
    *,
    model: str,
    base_url: str,
    api_key: str | None = None,
) -> ModelAdapter:
    """旧工厂的兼容发现入口；组合根也可直接注入适配器实例。"""
    providers = entry_points(group=MODEL_ADAPTER_GROUP, name="openai")
    if not providers:
        return NullAdapter()
    provider = next(iter(providers)).load()
    return cast(ModelAdapter, provider(model=model, base_url=base_url, api_key=api_key))
