"""通过发行入口点发现内容生成器，编排不反向导入 agents。"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import cast

from docfidelity.contracts.generation import ContentGenerator

CONTENT_GENERATOR_GROUP = "docfidelity.content_generators"


def create_content_generator(
    *, model: str, base_url: str, api_key: str | None = None, timeout: float = 60
) -> ContentGenerator | None:
    """适配器不可用时返回 None，由撰写规则显式降级。"""
    if not model or not base_url:
        return None
    try:
        providers = entry_points(group=CONTENT_GENERATOR_GROUP, name="openai")
        if not providers:
            return None
        factory = next(iter(providers)).load()
        return cast(
            ContentGenerator,
            factory(model=model, base_url=base_url, api_key=api_key, timeout=timeout),
        )
    except Exception:
        return None


__all__ = ["CONTENT_GENERATOR_GROUP", "create_content_generator"]
