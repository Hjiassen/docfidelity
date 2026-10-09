"""模板包契约查询（DD-2.1 ``registry/contract.py``）。

这是**生成期唯一的模板知识来源**。DD 决策二说得很直白：模板解析前置入库，
生成期只查契约、绝不重新打开模板原件。所以本模块的接口要足够回答渲染期的
每一个问题——查不到就是查不到，不允许"回头去看看原件"。

缓存策略
--------
模板包是**只读**的（写盘只发生在入库阶段），所以进程内按 ``ref`` 缓存是安全的。
缓存的是完整的 :class:`TemplatePackage`，而不是零散字段：按字段缓存意味着
"这次查到的样式和上次查到的版心可能来自模板包的两个不同版本"，那比不缓存更糟。
"""

from __future__ import annotations

from pathlib import Path

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import (
    BlockEntry,
    BlockKind,
    OutlineSkeleton,
    SlotSpec,
    StyleContract,
    TemplatePackage,
)

from .package_io import read_package

#: 模板包库的目录布局：``<root>/<template_id>@<version>/``。
#: 用 ``@`` 而不是斜杠分层，是为了让 ``ref`` 在界面上可以直接作为一个整体路径呈现。


class ContractQuery:
    """模板包查询器。所有方法都只读契约，不触碰 OOXML。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._cache: dict[str, TemplatePackage] = {}

    @property
    def root(self) -> Path:
        return self._root

    # ---------------------------------------------------------------- 加载
    def package_dir(self, ref: str) -> Path:
        """``ref`` 对应的模板包目录。"""
        return self._root / ref

    def available(self) -> list[str]:
        """库里全部模板包的 ``ref``（``template_id@version``），升序。"""
        if not self._root.is_dir():
            return []
        return sorted(item.name for item in self._root.iterdir() if item.is_dir())

    def package(self, ref: str) -> TemplatePackage:
        """加载模板包（带进程内缓存）。

        Raises
        ------
        EngineError
            ``E_PROFILE_TEMPLATE_MISSING``：库里没有该 ``ref``。
            ``E_PACKAGE_INCOMPLETE``：包存在但不完整。
        """
        cached = self._cache.get(ref)
        if cached is not None:
            return cached
        directory = self.package_dir(ref)
        if not directory.is_dir():
            raise EngineError(
                ErrorCode.PROFILE_TEMPLATE_MISSING,
                f"模板包不存在：{ref}",
                where={"ref": ref, "root": str(self._root)},
                remediation=["先入库该模板", "确认模板包已写入模板库目录"],
            )
        package = read_package(directory)
        self._cache[ref] = package
        return package

    def forget(self, ref: str | None = None) -> None:
        """丢弃缓存。入库覆盖某个模板包之后必须调用，否则会继续用旧契约。"""
        if ref is None:
            self._cache.clear()
        else:
            self._cache.pop(ref, None)

    # ---------------------------------------------------------------- 查询
    def style(self, ref: str, style_id: str) -> StyleContract | None:
        return self.package(ref).style_by_id(style_id)

    def outline(self, ref: str) -> OutlineSkeleton:
        return self.package(ref).outline

    def slot(self, ref: str, slot_id: str) -> SlotSpec | None:
        """按 ``slot_id`` 查槽位。``OutlineSkeleton.slots`` 是扁平索引，直查即可。"""
        for slot in self.package(ref).outline.slots:
            if slot.slot_id == slot_id:
                return slot
        return None

    def block(self, ref: str, block_id: str) -> BlockEntry | None:
        for block in self.package(ref).blocks.blocks:
            if block.block_id == block_id:
                return block
        return None

    def blocks_of_kind(self, ref: str, kind: BlockKind) -> list[BlockEntry]:
        return [block for block in self.package(ref).blocks.blocks if block.kind == kind]

    def used_style_ids(self, ref: str) -> set[str]:
        """模板中真正被使用过的样式 ID。

        生成期新注入内容只允许引用这里的样式——引用一个模板里存在但从未使用的
        样式，产物在 Word 里可能显示正常，但导 PDF / 换字号时就会露馅（FR-M2-13）。
        """
        used = self.package(ref).used_styles
        return {item.style_id for item in (*used.p_style, *used.tbl_style)}


__all__ = ["ContractQuery"]
