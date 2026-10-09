"""``pipeline.profiles`` 接缝单测（DD-7.5 / FR-M2-23）。

四条引脚：

① **``None`` 与 ``[]`` 必须区分**——``None`` 是"这次做不了这条校验"，``[]`` 是
   "做得了、结论是一个都没有"。混同之后，"缺能力跳过"会伪装成"校验通过"，
   而那正是硬规则 11 点名要避免的形状；
② **领域类型校验是转发，不是重写**——``model.validation`` 是唯一实现，本层只把
   "抛出"改成"结论"。故这里钉的是"错误码与 ``where`` 原样保留"；
③ **``applies`` 不进指纹**——它是人写的适用判据说明。改一句措辞不该让
   FR-M2-26 的"档案变更影响"凭空多出一批待重跑项；
④ **可满足性只转发、不重写**——``compose.precheck.satisfiable`` 的对齐口径
   （以 ``locator`` 为准）是抄一次就漂移一次的东西，故这里只钉"两者同结果"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.compose import satisfiable as compose_satisfiable
from docfidelity.contracts.errors import ErrorCode
from docfidelity.contracts.profile import (
    AppliesStatement,
    Bindings,
    DocTypeProfile,
    ProjectionSpec,
)
from docfidelity.model import DomainModelRegistry
from docfidelity.pipeline import (
    PROFILE_SECTIONS,
    fingerprint_profile,
    import_template,
    satisfiable,
    validate_profile,
)
from tests.fixtures.domain_fixture import api_models, api_profile, kitchen_profile

pytestmark = pytest.mark.unit


def _with_projection(profile: DocTypeProfile, for_each: str) -> DocTypeProfile:
    """把档案的第一条投影换成指向 ``for_each`` 的单项（去掉 ``children`` 干扰）。"""
    return profile.model_copy(
        update={
            "projections": [
                ProjectionSpec(unit="section", for_each=for_each, layout="one_section_per")
            ]
        }
    )


def _with_slot_types(profile: DocTypeProfile, *unit_types: str) -> DocTypeProfile:
    """把档案第一个槽位的 ``unit_types`` 换掉（骨架白名单不动）。"""
    skeleton = list(profile.genre.chapter_skeleton)
    first = skeleton[0]
    skeleton[0] = first.model_copy(
        update={"slots": [first.slots[0].model_copy(update={"unit_types": list(unit_types)})]}
    )
    return profile.model_copy(
        update={"genre": profile.genre.model_copy(update={"chapter_skeleton": skeleton})}
    )


def _with_allowed(profile: DocTypeProfile, *allowed: str) -> DocTypeProfile:
    return profile.model_copy(
        update={"genre": profile.genre.model_copy(update={"allowed_unit_types": list(allowed)})}
    )


def _api_registry() -> DomainModelRegistry:
    return DomainModelRegistry(api_models())


class TestValidateProfile:
    def test_passes_when_every_reference_is_known(self) -> None:
        check = validate_profile(
            api_profile(),
            registry=_api_registry(),
            known_template_refs=["TPL-API@1.0"],
        )
        assert check.ok is True
        assert check.problems() == []
        # 一条检查都没跳过：两份外部数据都给了
        assert check.skipped == []

    def test_reports_template_ref_that_is_not_registered(self) -> None:
        check = validate_profile(
            api_profile(),
            registry=_api_registry(),
            known_template_refs=["TPL-OTHER@1.0"],
        )
        assert check.ok is False
        assert check.missing_templates == ["TPL-API@1.0"]
        assert "TPL-API@1.0" in check.problems()[0]
        # 错误码来自 ``contracts.errors``，不是本模块自造的字符串
        assert check.error_infos()[0].code == ErrorCode.PROFILE_TEMPLATE_MISSING

    def test_reports_domain_kind_that_is_not_registered(self) -> None:
        """领域类型未登记 → 结论带着 ``model`` 的原错误码与 ``where``。"""
        check = validate_profile(
            _with_projection(api_profile(), "route"),
            registry=_api_registry(),
            known_template_refs=["TPL-API@1.0"],
        )
        assert check.ok is False
        assert check.kind_error is not None
        assert check.kind_error.code == ErrorCode.DOMAIN_KIND_UNKNOWN
        # ``where`` 里的 ``registered`` 是"已登记有哪些"——界面据此提示补哪一份声明
        assert "endpoint" in check.kind_error.where["registered"]
        assert check.kind_error.remediation

    def test_the_kind_list_is_data_driven_not_hardcoded(self) -> None:
        """菜谱档案配接口领域的注册表 → 一样报未登记。

        这条是"引擎不认识任何领域概念"的反证：同一个校验函数、同一个注册表类，
        换一套声明就是另一套结论，代码里没有任何针对 ``endpoint`` / ``dish`` 的分支。
        """
        check = validate_profile(kitchen_profile(), registry=_api_registry())
        assert check.kind_error is not None
        assert check.kind_error.code == ErrorCode.DOMAIN_KIND_UNKNOWN

    def test_a_kind_reference_with_dots_resolves_to_the_first_segment(self) -> None:
        """``endpoint.parameters`` 的 kind 是 ``endpoint``——取第一个点之前的一段。

        这条口径的唯一实现在 ``model.validation.require_registered_kind``；
        本层若自己按点全集切分，``a.b.c`` 会被读成一个叫 ``a.b`` 的类型。
        """
        check = validate_profile(
            _with_projection(api_profile(), "endpoint.parameters"),
            registry=_api_registry(),
            known_template_refs=[],
        )
        assert check.kind_error is None

    def test_registry_none_means_skipped_not_passed(self) -> None:
        """**这条是本模块最要紧的口径**：拿不到外部数据时说"没查"，不说"查过了没问题"。"""
        check = validate_profile(api_profile(), registry=None, known_template_refs=None)
        assert check.ok is True  # 跳过既不算通过也不算失败
        assert check.kind_error is None and check.findings == []
        assert check.missing_templates == []
        assert len(check.skipped) == 3
        assert any("领域模型" in text for text in check.skipped)
        assert any("模板库" in text for text in check.skipped)

    def test_empty_sequence_is_a_real_verdict(self) -> None:
        """``[]`` 与 ``None`` 相反：模板库确实是空的，于是每个引用都缺失。"""
        check = validate_profile(api_profile(), known_template_refs=[])
        assert check.missing_templates == ["TPL-API@1.0"]
        assert check.ok is False
        # 模板库给了，故只有领域模型那一条（它展开成两句）进 skipped
        assert len(check.skipped) == 2

    def test_reports_slot_unit_type_outside_the_skeleton_whitelist(self) -> None:
        # ``screenshot`` 不在 ``api_profile`` 的 allowed_unit_types 里
        profile = _with_slot_types(api_profile(), "para", "screenshot")
        check = validate_profile(
            profile, registry=_api_registry(), known_template_refs=["TPL-API@1.0"]
        )
        assert check.ok is False
        assert any("screenshot" in finding.message for finding in check.findings)

    def test_empty_allowed_unit_types_stays_open(self) -> None:
        """骨架没声明允许哪些单元类型 = 保持开放，不是"一概不接受"。"""
        profile = _with_allowed(_with_slot_types(api_profile(), "para", "screenshot"))
        check = validate_profile(
            profile, registry=_api_registry(), known_template_refs=["TPL-API@1.0"]
        )
        assert check.findings == []
        assert check.ok is True


class TestFingerprintProfile:
    def _profile(self, *, applies_note: str = "事实源为 OpenAPI 导出") -> DocTypeProfile:
        return api_profile().model_copy(
            update={
                "applies": AppliesStatement(
                    layout_source="TPL-API@1.0",
                    structure_enumerable="章节骨架见 genre.chapter_skeleton",
                    content_traceable=applies_note,
                )
            }
        )

    def test_is_stable_across_identical_profiles(self) -> None:
        assert (
            fingerprint_profile(api_profile()).digest == fingerprint_profile(api_profile()).digest
        )

    def test_covers_exactly_the_declared_sections(self) -> None:
        fingerprint = fingerprint_profile(api_profile())
        assert set(fingerprint.sections) == set(PROFILE_SECTIONS)
        assert "applies" not in fingerprint.sections

    def test_applies_wording_does_not_move_the_digest(self) -> None:
        """把说明写清楚一点，不该让"要重跑哪些文档"多出一批。"""
        before = fingerprint_profile(self._profile(applies_note="事实源为 OpenAPI 导出"))
        after = fingerprint_profile(self._profile(applies_note="事实源来自 OpenAPI 3.1 导出文件"))
        assert before.digest == after.digest

    def test_projection_change_moves_the_section_and_the_digest(self) -> None:
        before = fingerprint_profile(api_profile())
        after = fingerprint_profile(_with_projection(api_profile(), "route"))
        assert before.digest != after.digest
        assert before.sections["projections"] != after.sections["projections"]
        # 只动投影，不该把 genre 的分区摘要一起带走
        assert before.sections["genre"] == after.sections["genre"]


class TestSatisfiable:
    def test_forwards_to_compose_precheck(self, template_docx: Path, tmp_path: Path) -> None:
        result = import_template(
            template_docx,
            import_id="i_sat",
            template_id="TPL-1",
            version="1.0",
            target_dir=tmp_path / "package",
        )
        assert result.package is not None
        profile = api_profile()
        assert satisfiable(profile, result.package) == compose_satisfiable(profile, result.package)


def test_bindings_are_what_the_template_check_reads() -> None:
    """正对照：把引用改成模板库里有的那个，同一次校验就通过了。"""
    profile = api_profile().model_copy(
        update={"bindings": Bindings(template_packages=["TPL-API@1.0"], rule_sets=["base_style"])}
    )
    check = validate_profile(profile, registry=_api_registry(), known_template_refs=["TPL-API@1.0"])
    assert check.missing_templates == []
    assert check.ok is True
