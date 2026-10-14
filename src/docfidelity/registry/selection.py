"""选择是持久化数据；全部节点与范围在服务端验证。"""

import keyword

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import ImportDraft, StructureNode, TemplateSelection


def default_selection(draft: ImportDraft) -> TemplateSelection:
    """未识别不启用；支持的正文块和已检测布局默认选中。"""
    by_id = {n.node_id: n for n in draft.nodes}
    unsupported = {(n.part, n.block_index) for n in draft.nodes if not n.supported}
    candidates = [
        r for c in draft.candidates if c.status == "detected" and c.kind != "body" for r in c.ranges
    ]
    optional = [
        r
        for r in candidates
        if not any(
            (by_id[r.start_node_id].part, i) in unsupported
            for i in range(by_id[r.start_node_id].block_index, by_id[r.end_node_id].block_index + 1)
        )
    ]
    # 被跳过的完整候选仍占据范围，避免正文默认选择重新收回其中的块。
    occupied = {
        (by_id[r.start_node_id].part, i)
        for r in candidates
        for i in range(by_id[r.start_node_id].block_index, by_id[r.end_node_id].block_index + 1)
    }
    body = [
        r
        for c in draft.candidates
        if c.kind == "body"
        for r in c.ranges
        if (by_id[r.start_node_id].part, by_id[r.start_node_id].block_index) not in occupied
        and (by_id[r.start_node_id].part, by_id[r.start_node_id].block_index) not in unsupported
    ]
    return TemplateSelection(
        import_id=draft.import_id, source_sha256=draft.source_sha256, ranges=[*optional, *body]
    )


def validate_selection(draft: ImportDraft, selection: TemplateSelection) -> set[tuple[str, int]]:
    """返回选中块；拒绝越界、重叠与重复绑定。"""

    def fail(reason: str) -> None:
        raise EngineError(ErrorCode.SCHEMA_INVALID, reason)

    if selection.source_sha256 != draft.source_sha256 or selection.import_id != draft.import_id:
        fail("选择不属于当前原件")
    by_id = {n.node_id: n for n in draft.nodes}
    if len(set(selection.confirmed_absent)) != len(selection.confirmed_absent) or any(
        r.kind in selection.confirmed_absent for r in selection.ranges
    ):
        fail("确认不存在的版块不能同时选择，且不得重复确认")
    selected: set[tuple[str, int]] = set()
    for span in selection.ranges:
        start, end = by_id.get(span.start_node_id), by_id.get(span.end_node_id)
        if start is None or end is None:
            fail("选择范围引用不存在的节点")
        assert start is not None and end is not None
        if (
            not start.top_level
            or not end.top_level
            or start.part != end.part
            or start.block_index > end.block_index
        ):
            fail("选择范围必须是同一部件的完整顺序块")
        if (span.kind in {"header", "footer"}) != (start.part != "word/document.xml"):
            fail("选择范围的布局角色与部件不一致")
        if span.kind in {"header", "footer"}:
            parts = {
                by_id[r.start_node_id].part
                for c in draft.candidates
                if c.kind == span.kind
                for r in c.ranges
            }
            if start.part not in parts:
                fail("页眉页脚的选择角色与引用不一致")
        positions = {(start.part, i) for i in range(start.block_index, end.block_index + 1)}
        if positions & selected:
            fail("选择范围重叠")
        selected.update(positions)
    if any(not n.supported and (n.part, n.block_index) in selected for n in draft.nodes):
        fail("选中范围含不支持的嵌入对象")
    keys: set[str] = set()
    bound: list[StructureNode] = []
    for variable in selection.variables:
        if not variable.key.isidentifier() or keyword.iskeyword(variable.key):
            fail("变量 key 必须为标识符")
        node = by_id.get(variable.node_id)
        if (
            node is None
            or node.kind != "paragraph"
            or (node.part, node.block_index) not in selected
        ):
            fail("变量必须定位到已选择的完整文字容器")
        assert node is not None
        if variable.key in keys or any(
            node.part == old.part
            and (
                node.xpath == old.xpath
                or node.xpath.startswith(old.xpath + "/")
                or old.xpath.startswith(node.xpath + "/")
            )
            for old in bound
        ):
            fail("变量重复绑定或文字容器重叠")
        keys.add(variable.key)
        bound.append(node)
    if len(set(selection.fixed_assets)) != len(selection.fixed_assets):
        fail("固定素材重复选择")
    for asset in selection.fixed_assets:
        node = by_id.get(asset)
        if (
            node is None
            or node.kind != "image"
            or not node.supported
            or (node.part, node.block_index) not in selected
        ):
            fail("固定素材不受支持或未选择所在范围")
    return selected
