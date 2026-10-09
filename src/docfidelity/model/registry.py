"""领域模型注册表（DD-6.1）。

注册表里装的是**数据**：领域模型由项目以档案形式提供，引擎只按它们声明的
``key`` / ``fields`` / ``comparable`` / ``traceable`` 行事，**不对任何具体类型分支**
（决策六；验收 42 由 ``scripts/check_forbidden_words.py`` 的 AST 检索兜底）。

为什么注册表要独立成一层而不是一个 ``dict``
--------------------------------------------
1. **未知类型必须显式失败**。``for_each`` 写错一个字符串，如果退化成"查出空集合"，
   投影会安静地产出零个单元，最后表现为"生成的文档缺了一整章"——而报告是绿的。
   ``get()`` 一律抛 ``E_DOMAIN_KIND_UNKNOWN``，把错误顶到档案登记期（FR-M2-23）。
2. **字段语义只有一处出口**。``key`` / ``comparable`` / ``traceable`` 三组声明分别被
   去重、版本 diff、追溯矩阵消费；让它们各自去翻 ``DomainModel`` 就会长出三套取用口径。
3. ``member_kind`` 承担 ``for_each: "<kind>.<字段>"`` 的嵌套集合解析——成员类型由
   字段 schema 的 ``of`` 声明决定，引擎不去猜。
"""

from __future__ import annotations

from collections.abc import Iterable

from docfidelity.contracts.domain import DomainModel, FieldSchema
from docfidelity.contracts.errors import EngineError, ErrorCode

__all__ = ["DomainModelRegistry"]


class DomainModelRegistry:
    """``kind`` → 领域模型声明的登记表。

    ``kind`` 取值完全开放（FR-M1-13）：注册表不校验它像不像某个"已知类型"，
    因为它本来就不知道任何类型。
    """

    def __init__(self, models: Iterable[DomainModel] | None = None) -> None:
        self._models: dict[str, DomainModel] = {}
        for model in models or ():
            self.register(model)

    # -- 登记与检索 --------------------------------------------------------

    def register(self, model: DomainModel) -> None:
        """登记一份领域模型声明。

        Raises
        ------
        EngineError
            ``kind`` 已登记且本次声明与既有声明不同。同一 ``kind`` 只允许一份声明——
            "类型 → 声明"必须是单值映射，否则同一份事实在不同调用点上会被解释成
            不同结构，而这种不一致不会在任何地方报错。重复登记**完全相同**的声明
            是幂等的（配置文件被多次加载是常态）。
        """
        existing = self._models.get(model.kind)
        if existing is not None and existing != model:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"领域类型 {model.kind!r} 已登记，且本次声明与既有声明不一致",
                where={
                    "kind": model.kind,
                    "existing_title": existing.title,
                    "new_title": model.title,
                    "existing_key": existing.key,
                    "new_key": model.key,
                },
                remediation=[
                    "为新的声明换一个 kind",
                    "或修正档案中的重复定义——同一 kind 只能有一份声明",
                ],
            )
        self._models[model.kind] = model

    def get(self, kind: str) -> DomainModel:
        """取领域模型声明。

        Raises
        ------
        EngineError
            ``E_DOMAIN_KIND_UNKNOWN``——类型未登记。绝不返回 ``None`` 或空声明：
            调用方一旦能"拿到一个空模型接着跑"，缺失就会伪装成"这一章本来就没内容"。
        """
        model = self._models.get(kind)
        if model is None:
            raise EngineError(
                ErrorCode.DOMAIN_KIND_UNKNOWN,
                f"未登记的领域类型：{kind!r}",
                where={"kind": kind, "registered": self.kinds()},
                remediation=[
                    "在项目领域模型档案中补充该类型的声明（kind / title / key / fields）",
                    "或修正引用它的 for_each（DD-7.5 双向校验）",
                ],
            )
        return model

    def has(self, kind: str) -> bool:
        return kind in self._models

    def kinds(self) -> list[str]:
        """已登记的全部类型，**按字典序**（保证报告与错误信息可复现）。"""
        return sorted(self._models)

    def __len__(self) -> int:
        return len(self._models)

    def __contains__(self, kind: object) -> bool:
        return isinstance(kind, str) and kind in self._models

    # -- 三组声明的取用口径（唯一出口） --------------------------------------

    def key_fields(self, kind: str) -> list[str]:
        """标识键字段名，顺序即键序。"""
        return list(self.get(kind).key)

    def comparable_fields(self, kind: str) -> list[str]:
        """参与稳定化比对的字段名。"""
        return list(self.get(kind).comparable)

    def traceable_fields(self, kind: str) -> list[str]:
        """构成追溯边标识的字段名。"""
        return list(self.get(kind).traceable)

    def field_schema(self, kind: str, name: str) -> FieldSchema | None:
        """字段声明；字段未声明时返回 ``None``（调用方决定这是错误还是可选项）。"""
        return self.get(kind).fields.get(name)

    def member_kind(self, kind: str, path: str) -> str | None:
        """解析 ``path`` 末端字段声明的成员类型。

        ``path`` 形如 ``"parameters"`` 或 ``"detail.steps"``（对象字段可以下钻）。
        返回 ``None`` 表示该字段没有声明成员类型——即它不是 ``type: list`` 且带 ``of``
        的字段。调用方据此报错，**不要**在这里猜一个默认类型。
        """
        head, *rest = path.split(".")
        schema = self.get(kind).fields.get(head)
        for step in rest:
            if schema is None or schema.fields is None:
                return None
            schema = schema.fields.get(step)
        if schema is None:
            return None
        return schema.of
