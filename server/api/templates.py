"""模板入库端点（DD-13.2 的 ``templates/*``，P05 第 1~5 步 + 模板库）。

一个模块为什么装下六条路径
--------------------------
它们描述的是**同一条流水线的连续面**：``imports`` 收件并解析 → 读回报告 →
逐条确认待确认项 → 登记进库，最后 ``GET /templates`` 把库里有什么列出来。
把"收件""确认""登记"拆到不同文件里，"待确认项还剩几项"这个判据就会在
报告读取处与提交处各写一遍，而两处的分歧表现是"提交了却还提示有待确认项"。

**同步 ``def`` 而不是 ``async def``**：本组端点的成本在磁盘 I/O（解析、写包）与
仓储读写，而用例层是同步的（与 ``uploads.py`` / ``projects.py`` 同一口径）。

上传闸门为什么在用例层也不在路由层重写
--------------------------------------
``POST /templates/imports`` 与 ``POST /uploads`` **共用** ``server.uploads`` 的四道判据
（用例层调 ``validate_upload``）。路由层只做"把 multipart 读成 bytes"这件事，
故分块读的写法与 ``uploads.py`` 一致，判定口径则是同一份。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile

from docfidelity.pipeline import summarize

from ..services import Services
from ..templates import ReviewDecision
from .deps import services_of
from .files import zip_response
from .schemas import (
    ContractSummaryOut,
    RegisteredTemplateOut,
    RegisterRequest,
    ReviewRequest,
    TemplateImportOut,
    TemplateLibraryResponse,
)
from .schemas_assets import (
    TemplateDeprecateRequest,
    TemplateDetailOut,
    TemplateDiffOut,
)
from .schemas_project import TemplateReferencesOut
from .uploads import read_bounded

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["templates"])

ServicesDep = Annotated[Services, Depends(services_of)]


@router.post(
    "/templates/imports",
    response_model=TemplateImportOut,
    status_code=201,
    summary="导入模板（解析 + 入库报告，P05 第 1~3 步）",
)
def import_template(
    file: Annotated[UploadFile, File(description="模板原件（.docx）")],
    services: ServicesDep,
    doc_type: Annotated[
        str | None, Form(description="文档类型标记；**只是取值**，引擎不认识它")
    ] = None,
    template_id: Annotated[str | None, Form(description="模板编号；可留空由解析推断")] = None,
    version: Annotated[str | None, Form(description="版本号；可留空由解析推断")] = None,
    notes: Annotated[str | None, Form(description="备注")] = None,
    author: Annotated[str | None, Form(description="导入人；只落留痕")] = None,
) -> TemplateImportOut:
    """一次请求走完"收件 → 解析 → 出报告"。

    解析失败**不报 4xx**：DD-7.1 允许"仅登记原件、稍后重试"，此时响应里
    ``register_only=True`` 且 ``parsed=False``，界面据此渲染"原件已收到、解析未完成、
    可以再试一次"。只有上传闸门拒绝（415/413）与包写不进暂存目录（409）才是错误。
    """
    payload = read_bounded(file.file, services.settings.max_upload_bytes)
    record = services.import_template(
        filename=file.filename or "",
        payload=payload,
        doc_type=doc_type,
        template_id=template_id,
        version=version,
        notes=notes,
        author=author,
    )
    return TemplateImportOut.of(record)


@router.get(
    "/templates/imports/{import_id}",
    response_model=TemplateImportOut,
    summary="读取导入报告（P05 第 2~3 步）",
)
def get_import(import_id: str, services: ServicesDep) -> TemplateImportOut:
    return TemplateImportOut.of(services.get_import(import_id))


@router.post(
    "/templates/imports/{import_id}/reparse",
    response_model=TemplateImportOut,
    summary="重解析暂存原件（「稍后重试解析」）",
)
def reparse_import(import_id: str, services: ServicesDep) -> TemplateImportOut:
    """对**已收下的原件**再解析一次——P05 上「原件已收到、解析未完成、可以再试一次」的落点。

    **不接收新上传**：换原件重来是另一件事，它要得到新的 ``import_id``（否则同一条记录的
    原件摘要会变，而那个摘要已经写进登记回执）。已登记的导入不重解析（``E_VERSION_CONFLICT``）；
    上一轮的确认留痕原样保留，而"还剩几项要确认"按新解析结果算。
    """
    return TemplateImportOut.of(services.reparse_import(import_id))


@router.post(
    "/templates/imports/{import_id}/decisions",
    response_model=TemplateImportOut,
    summary="提交待确认项处理（P05 第 4 步）",
)
def review_import(
    import_id: str,
    body: ReviewRequest,
    services: ServicesDep,
) -> TemplateImportOut:
    """逐条留痕、可重复提交。

    ``action=specify`` 不带 ``value`` 报 ``E_REVIEW_VALUE_REQUIRED``（422）。
    提交后原样回整份导入记录，界面无需再取一次报告。
    """
    decisions = [
        ReviewDecision(
            item_id=item.item_id,
            action=item.action,
            value=item.value,
            note=item.note,
        )
        for item in body.decisions
    ]
    record = services.review_import(import_id, operator=body.operator, decisions=decisions)
    return TemplateImportOut.of(record)


@router.post(
    "/templates/imports/{import_id}/register",
    response_model=TemplateImportOut,
    summary="登记进模板库（P05 第 5 步）",
)
def register_template(
    import_id: str,
    body: RegisterRequest,
    services: ServicesDep,
) -> TemplateImportOut:
    """把这次导入登记进库；同 ``id@version`` 已存在且内容不同时报 ``E_VERSION_CONFLICT``。

    冲突时带出**旧/新的 ``package_hash`` 与旧登记的 ``registered_at``**，让界面能如实
    显示"这个版本号已被占用、旧版可能已被既有文档引用"，而不是笼统地说一句"失败"。
    """
    record = services.register_template(
        import_id,
        on_conflict=body.on_conflict,
        template_id=body.template_id,
        version=body.version,
        operator=body.operator,
    )
    return TemplateImportOut.of(record)


@router.get("/templates", response_model=TemplateLibraryResponse, summary="模板库列表")
def list_templates(
    services: ServicesDep,
    template_id: Annotated[str | None, Query(description="只列该编号的各版本")] = None,
    include_deprecated: Annotated[bool, Query(description="是否含已弃用版本")] = True,
) -> TemplateLibraryResponse:
    items = [
        RegisteredTemplateOut.of(item)
        for item in services.list_templates(
            template_id=template_id, include_deprecated=include_deprecated
        )
    ]
    return TemplateLibraryResponse(items=items, count=len(items))


@router.get(
    "/templates/{template_id}/versions",
    response_model=TemplateLibraryResponse,
    summary="某模板的全部版本",
)
def list_template_versions(template_id: str, services: ServicesDep) -> TemplateLibraryResponse:
    """与列表端点共用响应形状：同一份数据在两处长得不一样，前端就得写两套解析。"""
    items = [
        RegisteredTemplateOut.of(item) for item in services.list_templates(template_id=template_id)
    ]
    return TemplateLibraryResponse(items=items, count=len(items))


@router.get(
    "/templates/{template_id}",
    response_model=TemplateDetailOut,
    summary="模板包详情（契约，P04 的八个页签）",
)
def template_detail(
    template_id: str,
    services: ServicesDep,
    version: Annotated[str | None, Query(description="留空取最新一版")] = None,
) -> TemplateDetailOut:
    """一次把 P04 要的全部数据取齐：登记元数据 + 版本列表 + 契约本体 + 摘要卡 + 两条检查。

    ``package=None`` 表示这次登记**只有原件**（解析没成功）。此时 ``parsed=False``，
    两个检查清单为空——界面必须按"未解析"渲染，而不是画成一张全绿的检查表。
    """
    entry, package, integrity, fingerprint_problems = services.template_detail(template_id, version)
    versions = services.list_templates(template_id=template_id)
    return TemplateDetailOut(
        entry=RegisteredTemplateOut.of(entry),
        parsed=entry.parsed,
        package=package,
        summary=None if package is None else ContractSummaryOut.of(summarize(package)),
        versions=[RegisteredTemplateOut.of(item) for item in versions],
        integrity=integrity,
        fingerprint_problems=fingerprint_problems,
    )


@router.get(
    "/templates/{template_id}/diff",
    response_model=TemplateDiffOut,
    summary="版本差异（?from&to）",
)
def template_diff(
    template_id: str,
    services: ServicesDep,
    from_version: Annotated[str, Query(alias="from", description="基准版本")],
    to_version: Annotated[str, Query(alias="to", description="对比版本")],
) -> TemplateDiffOut:
    """``from`` / ``to`` 是 Python 关键字，故形参另取名、用 ``alias`` 把线上名字对齐设计。"""
    return TemplateDiffOut.of(services.template_diff(template_id, from_version, to_version))


@router.post(
    "/templates/{template_id}/deprecate",
    response_model=RegisteredTemplateOut,
    summary="废弃某版本（需理由）",
)
def deprecate_template(
    template_id: str,
    body: TemplateDeprecateRequest,
    services: ServicesDep,
) -> RegisteredTemplateOut:
    """**只标不删**：包目录原样保留，已产出的文档照旧可复现。重复废弃是幂等的。"""
    return RegisteredTemplateOut.of(
        services.deprecate_template(
            template_id,
            version=body.version,
            reason=body.reason,
            operator=body.operator,
        )
    )


@router.get(
    "/templates/{template_id}/references",
    response_model=TemplateReferencesOut,
    summary="引用了这个模板包的档案（P04 概要页签）",
)
def template_references(
    template_id: str,
    services: ServicesDep,
    version: Annotated[str | None, Query(description="留空 = 该编号的任意版本")] = None,
) -> TemplateReferencesOut:
    """反向引用：模板包 → 引用它的档案版本。

    ``version`` 省略即"该编号的任意版本"。两个粒度都要，见
    :meth:`server.template_library.TemplateLibraryMixin.template_references`。

    **P04 的「引用该包的作业与文档」目前只有"文档"这一半**：作业侧要按
    ``DocumentEntry.template_ref`` 反查配置与作业历史，那条链需要索引，尚未落地。
    ``declared_in`` 如实标出引用出现在档案的哪个位置，不把半份答案说成完整的。
    """
    return TemplateReferencesOut.of(
        template_id=template_id,
        version=version,
        items=services.template_references(template_id, version),
    )


@router.get("/templates/{template_id}/package", summary="导出模板包（zip）")
def export_template(
    template_id: str,
    services: ServicesDep,
    version: Annotated[str | None, Query(description="留空取最新一版")] = None,
) -> Response:
    """导出件是"交给第三方复现"的凭据：同一份包导出两次**字节完全相同**。"""
    filename, payload = services.export_template(template_id, version)
    return zip_response(filename, payload)
