"""契约层基类与格式版本策略（DD-3.1）。

版本策略
--------
===============  ==========================================================
主版本不同        拒绝加载，并给出迁移指引（需求 10.6 弃用流程）
次版本更高        允许加载，未识别字段**原样保留**（前向兼容），不得丢弃
================= ==========================================================

设计取舍
--------
基类默认 ``extra="forbid"``，因为内部构造时拼错字段名必须立刻失败。
但**加载外部文件**时需要前向兼容——两个诉求通过 :meth:`FormatModel.load`
解决：它动态生成一个允许未知字段的宽松子类用于反序列化，主类保持严格。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, model_validator
from typing_extensions import Self

from .errors import ErrorCode, FormatVersionError

_VERSION_RE: Final = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: 缺省格式版本（未声明格式家族的内部 DTO 用此值）
DEFAULT_FORMAT_VERSION: Final = "1.0.0"

#: 各格式家族当前主版本。主版本相同即允许加载；次版本允许更高。
FORMAT_CURRENT: Final[dict[str, str]] = {
    "generic": "1.0.0",
    "template": "1.0.0",
    "ir": "1.0.0",
    "domain": "1.0.0",
    "profile": "1.0.0",
    "fact": "1.0.0",
    "job": "1.0.0",
    "qa": "1.0.0",
    "config": "1.0.0",
}

_MIGRATION_HINT: Final = (
    "运行 `python scripts/export_schemas.py` 查看当前格式定义；"
    "旧格式需按 docs/spec/ 中的迁移说明升级后再加载。"
)


def parse_version(value: str) -> tuple[int, int, int]:
    """解析三段式语义化版本号，非法输入抛 :class:`FormatVersionError`。"""
    match = _VERSION_RE.match(value.strip())
    if match is None:
        raise FormatVersionError(
            ErrorCode.FORMAT_VERSION,
            f"格式版本号不合法：{value!r}，应形如 '1.0.0'",
            where={"format_version": value},
            remediation=["将 format_version 改为三段式语义化版本号"],
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def check_compatible(family: str, version: str) -> None:
    """校验格式版本兼容性：主版本必须相等。

    Raises
    ------
    FormatVersionError
        家族未注册，或主版本不一致。
    """
    expected = FORMAT_CURRENT.get(family)
    if expected is None:
        raise FormatVersionError(
            ErrorCode.FORMAT_VERSION,
            f"未注册的格式家族：{family!r}",
            where={"family": family},
            remediation=["在 contracts.base.FORMAT_CURRENT 中登记该格式家族"],
        )

    actual_major = parse_version(version)[0]
    expected_major = parse_version(expected)[0]
    if actual_major != expected_major:
        raise FormatVersionError(
            ErrorCode.FORMAT_VERSION,
            f"{family} 格式主版本不兼容：文件为 {version}，当前支持 {expected}",
            where={"family": family, "file_version": version, "supported": expected},
            remediation=[_MIGRATION_HINT],
        )


_LENIENT_CACHE: dict[type[BaseModel], type[BaseModel]] = {}


def _lenient_model(model_cls: type[BaseModel]) -> type[BaseModel]:
    """构造（并缓存）允许未知字段的宽松子类。

    宽松子类只用于**加载外部文件**，不得用于内部构造——内部构造走主类，
    字段名拼错必须立刻失败。
    """
    cached = _LENIENT_CACHE.get(model_cls)
    if cached is not None:
        return cached

    namespace: dict[str, Any] = {
        "__module__": model_cls.__module__,
        "model_config": ConfigDict(extra="allow"),
    }
    lenient = type(f"Lenient{model_cls.__name__}", (model_cls,), namespace)
    _LENIENT_CACHE[model_cls] = lenient
    return lenient


class FormatModel(BaseModel):
    """契约层统一基类。

    全部对外数据模型都继承本类，从而统一携带 ``format_version`` 并强制版本校验。
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        populate_by_name=True,
        validate_default=True,
    )

    #: 所属格式家族，决定主版本校验依据；子类覆盖。
    format_family: ClassVar[str] = "generic"

    format_version: str = DEFAULT_FORMAT_VERSION

    @model_validator(mode="after")
    def _validate_format_version(self) -> Self:
        check_compatible(self.format_family, self.format_version)
        return self

    @classmethod
    def load(cls, payload: Mapping[str, Any]) -> Self:
        """宽松加载：允许更高次版本引入的未知字段并**原样保留**。

        与 ``model_validate`` 的差异：未知字段不再报错，而是保留在
        ``model_extra`` 中（前向兼容，需求 10.6）；主版本不兼容时仍然拒绝加载。

        Examples
        --------
        >>> from docfidelity.contracts.ir import ContentUnit
        >>> unit = ContentUnit.load({"unit_id": "u1", "type": "para", "future_field": 1})
        >>> unit.type
        'para'
        """
        lenient = _lenient_model(cls)
        return lenient.model_validate(dict(payload))  # type: ignore[return-value]

    @classmethod
    def family_current_version(cls) -> str:
        """返回本模型所属格式家族的当前版本号。"""
        return FORMAT_CURRENT[cls.format_family]


__all__ = [
    "DEFAULT_FORMAT_VERSION",
    "FORMAT_CURRENT",
    "FormatModel",
    "check_compatible",
    "parse_version",
]
