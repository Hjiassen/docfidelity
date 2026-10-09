"""格式版本兼容性测试（DD-3.1）。

契约层的破坏性变更必须在这里暴露：

* 主版本不同   -> 拒绝加载，且必须带迁移指引
* 次版本更高   -> 允许加载，未识别字段**原样保留**
* 内部构造     -> 未知字段必须立刻失败（防止字段名拼错被静默吞掉）
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from docfidelity.contracts.base import (
    DEFAULT_FORMAT_VERSION,
    FORMAT_CURRENT,
    check_compatible,
    parse_version,
)
from docfidelity.contracts.errors import ErrorCode, FormatVersionError
from docfidelity.contracts.ir import ContentUnit

pytestmark = pytest.mark.unit


class TestVersionParsing:
    def test_valid_triplet(self) -> None:
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_surrounding_whitespace_tolerated(self) -> None:
        assert parse_version("  1.0.0 ") == (1, 0, 0)

    @pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0.0", "", "1.a.0", "1.0.-1"])
    def test_invalid_raises_format_version_error(self, bad: str) -> None:
        with pytest.raises(FormatVersionError) as excinfo:
            parse_version(bad)
        assert excinfo.value.code == ErrorCode.FORMAT_VERSION
        assert excinfo.value.remediation, "非法版本号也必须给出可操作动作"


class TestCompatibilityRule:
    @pytest.mark.parametrize("family", sorted(FORMAT_CURRENT))
    def test_registered_family_accepts_current(self, family: str) -> None:
        check_compatible(family, FORMAT_CURRENT[family])

    def test_higher_minor_is_forward_compatible(self) -> None:
        check_compatible("ir", "1.9.9")

    def test_major_mismatch_rejected_with_migration_hint(self) -> None:
        with pytest.raises(FormatVersionError) as excinfo:
            check_compatible("ir", "2.0.0")
        error = excinfo.value
        assert error.code == ErrorCode.FORMAT_VERSION
        assert error.fatal is True
        assert error.remediation, "主版本不兼容必须给出迁移指引（需求 10.6）"
        assert error.where["family"] == "ir"

    def test_unknown_family_rejected(self) -> None:
        with pytest.raises(FormatVersionError):
            check_compatible("no-such-family", "1.0.0")


class TestFormatModelBehaviour:
    def test_default_format_version(self) -> None:
        unit = ContentUnit(unit_id="u1", type="para")
        assert unit.format_version == DEFAULT_FORMAT_VERSION

    def test_family_current_version_lookup(self) -> None:
        assert ContentUnit.family_current_version() == FORMAT_CURRENT["ir"]

    def test_direct_construction_forbids_unknown_fields(self) -> None:
        # 内部构造严格：字段名拼错必须立刻失败，不得静默进 model_extra
        with pytest.raises(ValidationError):
            ContentUnit(unit_id="u1", type="para", typo_field=1)  # type: ignore[call-arg]

    def test_load_preserves_unknown_fields(self) -> None:
        unit = ContentUnit.load(
            {"unit_id": "u1", "type": "para", "future_field": {"a": 1}, "another": [1, 2]}
        )
        assert isinstance(unit, ContentUnit)
        assert unit.type == "para"
        assert unit.model_extra is not None
        assert unit.model_extra["future_field"] == {"a": 1}
        assert unit.model_extra["another"] == [1, 2]

    def test_load_rejects_major_mismatch(self) -> None:
        with pytest.raises(FormatVersionError):
            ContentUnit.load({"unit_id": "u1", "type": "para", "format_version": "9.0.0"})

    def test_load_still_validates_types(self) -> None:
        # 宽松加载只放行"未知字段"，不放松已知字段的类型约束
        with pytest.raises(ValidationError):
            ContentUnit.load({"unit_id": "u1", "type": "not-a-unit-type"})

    def test_dump_then_load_roundtrip_keeps_extras(self) -> None:
        unit = ContentUnit.load({"unit_id": "u1", "type": "para", "future_field": 7})
        dumped = unit.model_dump()
        assert dumped["future_field"] == 7
