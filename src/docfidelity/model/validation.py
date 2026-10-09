"""领域对象与档案的校验（DD-6.3 / DD-7.5）。

两级校验，失败姿态刻意不同
--------------------------
===================  ==================================================  ====================
校验                 触发条件                                            失败姿态
===================  ==================================================  ====================
``validate_object``  ``kind`` 未登记 / 标识键缺失或为空 / 字段类型不符   抛 ``EngineError``（硬失败）
``validate_profile`` 档案引用了未登记的 ``for_each``                    抛 ``EngineError``（拒绝登记）
``validate_profile`` 槽位声明的单元类型超出档案允许范围                  返回 ``Finding``（可复核）
``collect_findings`` 声明为必填的字段缺值 / ``enum`` 取值越界             返回 ``Finding``（可复核）
===================  ==================================================  ====================

硬失败与软告警的分界不是"严重程度"，而是**能否继续安全地跑下去**：

- 标识键缺失：该对象此后无法被去重、无法被引用、无法进入追溯矩阵——继续跑只会
  产出一份"看起来完整、实际丢了几条"的文档。所以接入阶段即失败（FR-M1-12）。
- 字段类型不符：投影取值时会静默拿到一个形状不对的值，错误会一路漂到渲染层才炸。
- ``for_each`` 指向未登记的类型：投影会安静地返回零个单元（DD-6.1 的注释说的正是
  这件事），所以档案登记期就拒绝（FR-M2-23）。
- 必填缺值 / 枚举越界：内容仍然可生成，只是有洞——按 NFR-26 进裁决队列，不阻断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from docfidelity.contracts.domain import DomainModel, DomainObject, FieldSchema
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.profile import DocTypeProfile, ProjectionSpec
from docfidelity.contracts.qa import Finding, Location

from .registry import DomainModelRegistry

__all__ = [
    "DOMAIN_RULE_ID",
    "PROFILE_RULE_ID",
    "collect_findings",
    "is_blank",
    "require_registered_kind",
    "validate_object",
    "validate_objects",
    "validate_profile",
]

#: 接入校验的规则编号（FR-M1-12）。
DOMAIN_RULE_ID: str = "FR-M1-12"

#: 档案登记与双向校验的规则编号（FR-M2-23）。
PROFILE_RULE_ID: str = "FR-M2-23"


# -- 硬校验 ---------------------------------------------------------------


def validate_object(obj: DomainObject, registry: DomainModelRegistry) -> None:
    """校验单个领域对象，不合格即抛 :class:`EngineError`。

    Raises
    ------
    EngineError
        ``E_DOMAIN_KIND_UNKNOWN``（类型未登记）、``E_DOMAIN_KEY_MISSING``
        （标识键缺失 / 为空 / 段数与声明不符）、``E_DOMAIN_TYPE_MISMATCH``
        （已声明字段的取值类型不符）。
    """
    model = registry.get(obj.kind)
    _check_key(model, obj, where={"kind": obj.kind})
    for name, value in obj.fields.items():
        schema = model.fields.get(name)
        if schema is None:
            # 未声明的字段不报错：领域模型可以比事实源窄（前向兼容，需求 10.6）。
            # 拒收它们会让"上游多给了一列"变成"整份事实源接入失败"。
            continue
        _check_value(schema, value, name, registry, where={"kind": obj.kind, "field": name})


def validate_objects(objects: Sequence[DomainObject], registry: DomainModelRegistry) -> None:
    """逐个校验；错误信息带上对象引用，便于在接入报告里定位到具体条目。"""
    for obj in objects:
        try:
            validate_object(obj, registry)
        except EngineError as error:
            error.where.setdefault("object", obj.ref)
            raise


def _check_key(model: DomainModel, obj: DomainObject, *, where: dict[str, Any]) -> None:
    if len(obj.key) != len(model.key):
        raise EngineError(
            ErrorCode.DOMAIN_KEY_MISSING,
            f"{obj.kind!r} 的标识键段数为 {len(obj.key)}，与声明的 {len(model.key)} 段不符（{model.key}）",
            where={**where, "declared_key": model.key, "actual_key": obj.key},
            remediation=["按领域模型声明的 key 顺序补齐标识键取值"],
        )
    blanks = [name for name, value in zip(model.key, obj.key, strict=True) if not value.strip()]
    if blanks:
        raise EngineError(
            ErrorCode.DOMAIN_KEY_MISSING,
            f"{obj.kind!r} 的标识键取值为空：{blanks}",
            where={**where, "blank_key_fields": blanks},
            remediation=[
                "在事实源中补齐该字段，或在接入器的归一化里给出确定性的缺省值",
                "标识键是去重与追溯的唯一句柄，留空会让两条不同记录被折叠成一条",
            ],
        )


def _check_value(
    schema: FieldSchema,
    value: Any,
    name: str,
    registry: DomainModelRegistry,
    *,
    where: dict[str, Any],
) -> None:
    if value is None:
        return
    if not _matches(schema, value):
        raise EngineError(
            ErrorCode.DOMAIN_TYPE_MISMATCH,
            f"字段 {name!r} 的取值类型与声明不符：声明为 {schema.type}，实际为 {type(value).__name__}",
            where={**where, "declared_type": schema.type, "actual_type": type(value).__name__},
            remediation=[
                "修正事实源的取值，或在领域模型档案中修改该字段的类型声明",
            ],
        )
    if schema.type == "list" and schema.of is not None:
        _check_members(schema.of, value, name, registry, where=where)
    elif schema.type == "object" and schema.fields is not None and isinstance(value, Mapping):
        for sub_name, sub_schema in schema.fields.items():
            if sub_name in value:
                _check_value(
                    sub_schema,
                    value[sub_name],
                    f"{name}.{sub_name}",
                    registry,
                    where={**where, "nested_field": sub_name},
                )


def _check_members(
    member_kind: str,
    value: Sequence[Any],
    name: str,
    registry: DomainModelRegistry,
    *,
    where: dict[str, Any],
) -> None:
    """校验 ``list`` 成员：必须是对象，且标识键已填。

    成员对象只做**标识键**校验，不递归做全字段类型校验——嵌套成员的 schema 校验
    属于接入器的职责（``ingest``），这里只守住"没有键就无法被引用"这条底线。
    """
    model = registry.get(member_kind)
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise EngineError(
                ErrorCode.DOMAIN_TYPE_MISMATCH,
                f"字段 {name!r} 的第 {index} 个成员应为对象，实际为 {type(entry).__name__}",
                where={**where, "index": index, "member_kind": member_kind},
                remediation=[f"把该成员改为对象，或把字段 {name!r} 的 of 改为正确的成员类型"],
            )
        blanks = [key for key in model.key if not str(entry.get(key) or "").strip()]
        if blanks:
            raise EngineError(
                ErrorCode.DOMAIN_KEY_MISSING,
                f"字段 {name!r} 的第 {index} 个成员缺少标识键取值：{blanks}",
                where={
                    **where,
                    "index": index,
                    "member_kind": member_kind,
                    "blank_key_fields": blanks,
                },
                remediation=["补齐成员对象的标识键，或从该列表中移除它"],
            )


def _matches(schema: FieldSchema, value: Any) -> bool:
    declared = schema.type
    if declared == "bool":
        # bool 必须在 int 之前判定：Python 里 True 是 int 的实例。
        return isinstance(value, bool)
    if declared == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "float":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if declared in {"str", "path", "asset"}:
        return isinstance(value, str)
    if declared == "datetime":
        return isinstance(value, str | datetime)
    if declared == "enum":
        # 只查"是个字符串"；取值是否落在 values 里由 _violates_domain 判，
        # 那是软告警——事实源用了个同义词就让整份接入失败，代价远大于收益。
        return isinstance(value, str)
    if declared == "list":
        return isinstance(value, list | tuple)
    if declared == "object":
        return isinstance(value, Mapping)
    return True


def _violates_domain(schema: FieldSchema, value: Any) -> bool:
    """取值越出 ``enum`` 声明的取值域。"""
    return (
        schema.type == "enum"
        and isinstance(value, str)
        and schema.values is not None
        and value not in schema.values
    )


# -- 软告警 ---------------------------------------------------------------


def collect_findings(
    objects: Sequence[DomainObject], registry: DomainModelRegistry
) -> list[Finding]:
    """收集不阻断构建、但需要人工复核的问题。

    产出的 ``Finding`` 顺序**按传入顺序**（接入器应给出确定性顺序），便于报告 diff。
    """
    findings: list[Finding] = []
    for obj in objects:
        if not registry.has(obj.kind):
            continue
        model = registry.get(obj.kind)
        for name, schema in model.fields.items():
            present = obj.fields.get(name)
            if schema.required and is_blank(present):
                findings.append(
                    Finding(
                        rule_id=DOMAIN_RULE_ID,
                        severity="major",
                        message=f"{obj.ref} 缺少声明为必填的字段：{name}",
                        evidence=f"{model.title}.{name}",
                        suggestion="补齐该字段的实际取值，或把它在领域模型档案中改为非必填",
                    )
                )
            elif _violates_domain(schema, present):
                findings.append(
                    Finding(
                        rule_id=DOMAIN_RULE_ID,
                        severity="minor",
                        message=f"{obj.ref} 的字段 {name} 取值不在声明的取值域内",
                        evidence=f"{present!r} 不在 {schema.values!r}",
                        suggestion="确认是事实源用词不一致还是取值域声明过窄",
                    )
                )
    return findings


def is_blank(value: Any) -> bool:
    """值为"空"的判定：``None`` / 空串 / 空列表 / 空字典。

    只这一处实现：过滤、必填校验、分组标题回退都要用它，三份实现里只要有一份把
    ``0`` 或 ``False`` 判成空，就会在某个字段上悄悄丢内容。
    """
    return value is None or value == "" or value == [] or value == {}


# -- 档案登记前置校验（DD-7.5 第 1、3 条） ---------------------------------


def validate_profile(profile: DocTypeProfile, registry: DomainModelRegistry) -> list[Finding]:
    """档案登记前的双向校验。

    覆盖 DD-7.5 的两条（其余两条需要模板包，属 ``registry`` 的职责，不在这里）：

    1. ``projections[].for_each`` 与 ``children[].for_each`` 的领域类型均已登记 ——
       **未登记即拒绝登记**（FR-M2-23）。理由见模块文档：放行等于让投影安静地
       返回空集合，最终表现为"文档少了一整章而报告全绿"。
    2. ``chapter_skeleton[].slots[].unit_types`` ⊂ ``allowed_unit_types`` ——
       返回 ``Finding``。这是档案内部的自相矛盾，不一定立刻致命（可能只是还没写完），
       但必须让人看见。

    Returns
    ------
    list[Finding]
        第 3 条命中的问题；无问题返回空列表。
    """
    findings: list[Finding] = []
    for index, spec in enumerate(profile.projections):
        _check_projection(spec, f"projections[{index}]", registry)
    allowed = set(profile.genre.allowed_unit_types)
    if allowed:
        for chapter in profile.genre.chapter_skeleton:
            for slot in chapter.slots:
                outside = [unit for unit in slot.unit_types if unit not in allowed]
                if outside:
                    findings.append(
                        Finding(
                            rule_id=PROFILE_RULE_ID,
                            severity="major",
                            message=(
                                f"档案 {profile.profile_id} 的槽位 {slot.slot_id} 声明了 "
                                f"未被 allowed_unit_types 允许的单元类型：{outside}"
                            ),
                            location=Location(chapter_path=[chapter.title]),
                            evidence=f"allowed_unit_types={profile.genre.allowed_unit_types}",
                            suggestion="把该单元类型加入 allowed_unit_types，或收窄槽位声明",
                        )
                    )
    return findings


def require_registered_kind(expression: str, registry: DomainModelRegistry, *, trail: str) -> str:
    """校验 ``for_each`` 的基础类型已登记，返回该基础类型。

    这是**唯一**一处"表达式 → 基础类型"的解析与检查，档案登记期（``validate_profile``）
    与求值期（``evaluate``）共用。两处都拦的理由不同：登记期拦的是档案本身，
    求值期拦的是绕过登记直接调 `evaluate` 的调用方（脚本、测试、将来的 SDK）。

    Raises
    ------
    EngineError
        ``E_DOMAIN_KIND_UNKNOWN``——未登记。必须在**求值前**抛，因为放行之后的
        表现是"投影安静地返回零个单元"，最终变成"生成的文档少了一整章而报告全绿"。
    """
    base = expression.partition(".")[0]
    if not registry.has(base):
        raise EngineError(
            ErrorCode.DOMAIN_KIND_UNKNOWN,
            f"{trail} 引用了未登记的领域类型：{expression!r}",
            where={"trail": trail, "for_each": base, "registered": registry.kinds()},
            remediation=[
                "在项目领域模型档案中补上该类型声明",
                "或修正 for_each——登记/求值期的失败比运行期少一章文档要便宜得多",
            ],
        )
    return base


def _check_projection(spec: ProjectionSpec, trail: str, registry: DomainModelRegistry) -> None:
    require_registered_kind(spec.for_each, registry, trail=f"档案投影 {trail}")
    for index, child in enumerate(spec.children):
        _check_projection(child, f"{trail}.children[{index}]", registry)
