"""候选检测与结构索引，原件始终只读。"""

from pathlib import Path

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import (
    CandidateKind,
    ImportDraft,
    SelectionRange,
    StructureNode,
    TemplateCandidate,
)
from docfidelity.ooxml.doc import qn, xpath
from docfidelity.ooxml.effective_fields import toc_ranges
from docfidelity.ooxml.effective_index import content_roots, index_structure
from docfidelity.ooxml.package import DOCUMENT_PART, DocxPackage
from docfidelity.platform.hashing import sha256_file


def inspect_template(source: Path, *, import_id: str) -> ImportDraft:
    """检查源文件并生成稳定节点与独立检测状态。"""
    try:
        package = DocxPackage.open(source)
        roots = content_roots(package)
        nodes = index_structure(package)
        if not any(n.part == DOCUMENT_PART for n in nodes):
            raise EngineError(ErrorCode.TEMPLATE_UNREADABLE, "原件正文为空")
        candidates: list[TemplateCandidate] = []
        top = [n for n in nodes if n.part == DOCUMENT_PART and n.top_level]
        body = roots[DOCUMENT_PART]
        cover_end = next((i for i, b in enumerate(body) if xpath(b, "./w:pPr/w:sectPr")), None)
        cover_nodes = [n for n in top if cover_end is not None and n.block_index <= cover_end]
        if any(n.heading_level is not None for n in cover_nodes):
            cover_nodes = []
        toc_spans = toc_ranges(body)
        toc_nodes = [
            n for n in top if any(start <= n.block_index <= end for start, end in toc_spans)
        ]
        cover_nodes = [n for n in cover_nodes if n not in toc_nodes]
        groups: list[tuple[CandidateKind, list[StructureNode]]] = [
            ("cover", cover_nodes),
            ("toc", toc_nodes),
            ("body", top),
        ]
        for kind, group in groups:
            ranges = [
                SelectionRange(kind=kind, start_node_id=n.node_id, end_node_id=n.node_id)
                for n in group
            ]
            if kind == "toc":
                by_index = {n.block_index: n for n in top}
                ranges = [
                    SelectionRange(
                        kind="toc",
                        start_node_id=by_index[start].node_id,
                        end_node_id=by_index[end].node_id,
                    )
                    for start, end in toc_spans
                    if start in by_index and end in by_index
                ]
            candidates.append(
                TemplateCandidate(
                    candidate_id=kind,
                    kind=kind,
                    status="detected" if group else "unrecognized",
                    ranges=ranges,
                    reason="结构检测命中" if group else "未识别，可人工指定完整范围",
                )
            )
        roles: tuple[CandidateKind, ...] = ("header", "footer")
        for kind in roles:
            ranges = [
                SelectionRange(kind=kind, start_node_id=n.node_id, end_node_id=n.node_id)
                for n in nodes
                if n.top_level
                and n.part != DOCUMENT_PART
                and roots[n.part].tag == qn(f"w:{'hdr' if kind == 'header' else 'ftr'}")
            ]
            candidates.append(
                TemplateCandidate(
                    candidate_id=kind,
                    kind=kind,
                    status="detected" if ranges else "unrecognized",
                    ranges=ranges,
                    reason="关系引用检测命中" if ranges else "未识别，可人工指定完整范围",
                )
            )
        return ImportDraft(
            import_id=import_id,
            source_sha256=sha256_file(source),
            source_size=source.stat().st_size,
            nodes=nodes,
            candidates=candidates,
        )
    except OSError as exc:
        raise EngineError(ErrorCode.TEMPLATE_UNREADABLE, "无法读取原件") from exc
