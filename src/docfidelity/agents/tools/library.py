"""工具白名单的声明（DD-12.1 / 需求 5.5）。

这张表是**模型的动作空间**。它刻意只覆盖"五 Agent 实际会调用的工具"，因为一个工具
摆进可见面就等于给模型多一次选错的机会（需求 5.2.1 第二类短板：工具选择易错、
编造不存在的参数）。需求 5.5 列出的完整工具表（约 30 项）随对应能力落地时逐批登记，
而不是一次性铺开。

三种落地状态
------------
====================================  ================================================
``callable``                          已接线，参数是 JSON 化扁平面 → **进可见面**
``context_bound``                     已实现，但入口吃**流水线中途**的运行时上下文 →
                                      暂不进可见面（当前仅 render 的六个中途步骤）
``unbound``                           架构上没有独立入口 → 永久不进可见面
====================================  ================================================

``context_bound`` 不是"没做"，而是"差一个 JSON 化门面"。这个区分是有代价意识的：
把它记成 unbound，下一轮会有人去重写已经写好的渲染步骤。

**非 callable 的工具不声明参数**，只写 ``context_note``。理由很直接：参数表的用途是
"给模型看的形状 + 运行时校验的依据"，而这两件事对不可调用的工具都不成立；
硬凑一份参数表只会得到一份**看起来精确、实际是猜测**的文档——而它会被人当成事实引用。

JSON 门面落在哪一层（门面下沉）
------------------------------
``handler_ref`` 是**惰性点路径**（``ToolSpec.handler`` 用 ``importlib`` 在调用时解析），
不产生静态 import 边。于是"补一个 JSON 门面"不必把 OOXML 相关代码搬进 agents——把门面
写在**管这件事的那一层**，agents 只留一个字符串引用即可，``agents-has-no-ooxml`` 契约不破：

====================================  ================================================
``template_parse`` → ``registry``     ``registry.parse_template``（只解析、不落库）
``content_ir_build`` → ``compose``    ``compose.build_content_ir``（compose 层零 OOXML，
                                      理论上可住 agents，仍按"归属层"就近放置）
``qa_run`` / ``style_audit`` → ``qa`` ``qa.audit_document`` / ``qa.audit_rule_set``
``drift_check`` → ``qa``              ``qa.compare_documents``（路径进、契约出）
====================================  ================================================

**render 的六个不在此列**（``table_section`` / ``slot_plan`` / ``render_inject`` /
``pagination_apply`` / ``figure_place`` / ``toc_refresh``）：它们的入参是渲染流水线
**中途**的 ``RenderContext``（含模板副本 DOM、原型索引、已落位结果与资源加载器回调）。
从磁盘"重做"这些步骤等于在 render 层开**第二条渲染入口**，与 ADR-003"所有落笔由 render
层按确定性算法执行、全程只有一条路径"相悖。它们的门面就是 ``render.render()`` 阶段本身，
已由 pipeline 拥有，故保持 ``context_bound`` 并在此如实归因（而不是记成 unbound——那会
让人下一轮去重写已经写好的渲染步骤）。

与上游的两处落地偏离
--------------------
1. **``unit_section``（原 ``endpoint_section``）**：DD-12.1 / 需求 5.5 记为
   ``endpoint_section``。该名含领域概念（ADR-011 / NFR-28 禁止引擎标识符出现领域概念名），
   且在现架构下"按单元类型生成章节"就是 ``content_ir_build`` 加档案投影规则，
   **没有独立入口**。故本表记为 ``unit_section`` 并标 ``unbound``，原始名记在
   ``source_name`` 里保持可对照。
2. **``table_section`` 与 ``figure_place`` 的归属**：需求 5.5 把它们归在 M3（内容）组。
   本表 ``group`` 仍记 ``content``（能力归类），``handler_ref`` 指向 ``render``
   （实现位置）。两者不是一回事：几何与表格原型的确定性实现住在 render，
   而需求 5.3 的边界表把"几何计算"明确划在确定性一侧。
"""

from __future__ import annotations

from docfidelity.agents.tools.spec import ParamKind, ToolParam, ToolSpec

__all__ = ["TOOL_SPECS", "TOOLS_BY_NAME"]


def _p(name: str, kind: ParamKind, description: str, *, required: bool = True) -> ToolParam:
    return ToolParam(name=name, kind=kind, description=description, required=required)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    # ------------------------------------------------------------ 模板（M2）
    ToolSpec(
        name="template_parse",
        group="template",
        summary="解析一份 docx 原件，产出模板包契约。",
        params=(
            _p("source", "str", "模板原件（.docx）的路径"),
            _p("template_id", "str", "模板标识；原件清单里读不到时在此给定", required=False),
            _p("version", "str", "模板版本；原件清单里读不到时在此给定", required=False),
            _p("doc_type", "str", "面向的文档类型（入库时由人补，非解析所得）", required=False),
            _p("notes", "str", "入库备注（纯人工输入）", required=False),
        ),
        example='{"source": "var/incoming/hw05.docx", "template_id": "hw05", "version": "1.0.0"}',
        handler_ref="docfidelity.registry.parse_template",
        path_params=("source",),
    ),
    ToolSpec(
        name="contract_build",
        group="template",
        summary="由模板包生成契约载荷（样式 / 页面设置 / 骨架 / 槽位等分区）。",
        params=(_p("package", "template_package", "模板包契约对象"),),
        example='{"package": {"manifest": {"template_id": "demo", "version": "1.0.0"}, "page_setup": {}}}',
        handler_ref="docfidelity.registry.build_contract_payloads",
    ),
    ToolSpec(
        name="block_index",
        group="template",
        summary="读取模板包的版块资产索引（版块标识 → 资产字节）。",
        params=(_p("root", "str", "模板包所在根目录"),),
        example='{"root": "var/packages"}',
        handler_ref="docfidelity.registry.read_block_assets",
        path_params=("root",),
    ),
    ToolSpec(
        name="fingerprint_diff",
        group="template",
        summary="比对两份指纹，输出发生变化的分区路径。",
        params=(
            _p("before", "fingerprint", "旧版指纹"),
            _p("after", "fingerprint", "新版指纹"),
        ),
        example=(
            '{"before": {"digest": "a", "sections": {}}, '
            '"after": {"digest": "b", "sections": {}}}'
        ),
        handler_ref="docfidelity.registry.diff_fingerprints",
    ),
    ToolSpec(
        name="registry_put",
        group="template",
        summary="把一个模板包写入模板库；库中已有同一版本时拒绝，不静默覆盖。",
        params=(
            _p("package", "template_package", "模板包契约对象"),
            _p("target_dir", "str", "模板库目标目录"),
            _p("block_assets", "bool", "是否随包写入版块资产", required=False),
        ),
        example=(
            '{"package": {"manifest": {"template_id": "demo", "version": "1.0.0"}, '
            '"page_setup": {}}, "target_dir": "packages"}'
        ),
        handler_ref="docfidelity.registry.write_package",
        path_params=("target_dir",),
        writes=True,
        requires_approval=True,
    ),
    ToolSpec(
        name="outline_read",
        group="template",
        summary="读出一个模板包，取得其结构骨架与契约。",
        params=(_p("package_dir", "str", "模板包目录"),),
        example='{"package_dir": "var/packages/demo@1.0.0"}',
        handler_ref="docfidelity.registry.read_package",
        path_params=("package_dir",),
    ),
    # ------------------------------------------------------------ 事实（M1）
    ToolSpec(
        name="source_query",
        group="facts",
        summary="按给定事实与资产构造事实包，作为后续装配的唯一输入。",
        params=(
            _p("source_id", "str", "事实源标识"),
            _p("source_type", "str", "事实源类型标识（与来源配置一致）"),
            _p("facts", "fact_list", "已抽取的事实列表", required=False),
            _p("assets", "asset_ref_list", "随包资产引用", required=False),
            _p("warnings", "str_list", "接入期告警（原样带入事实包）", required=False),
        ),
        example='{"source_id": "src_01", "source_type": "openapi", "facts": []}',
        handler_ref="docfidelity.ingest.build_bundle",
    ),
    # ------------------------------------------------------------ 内容（M3）
    ToolSpec(
        name="content_ir_build",
        group="content",
        summary="由事实快照与档案投影规则装配出内容 IR。",
        params=(
            _p("snapshot", "fact_snapshot", "多源合并后的事实快照"),
            _p(
                "profile",
                "doc_type_profile",
                "文档类型档案（章节骨架、投影规则、术语作用域由它声明）",
            ),
            _p(
                "domain", "domain_model_list", "领域模型声明清单（for_each 指向的 kind 必须已登记）"
            ),
            _p("doc_key", "str", "写入内容 IR 的文档条目键"),
            _p("template_ref", "str", "模板包引用"),
            _p(
                "terminology", "terminology", "术语表；档案 scope=none 时一律不套用", required=False
            ),
            _p("purity_words", "str_list", "外部词表，命中即记 blocker", required=False),
        ),
        example=(
            '{"snapshot": {"project_id": "p", "merged": []}, "profile": {"profile_id": "pf"}, '
            '"domain": [], "doc_key": "overview", "template_ref": "demo@1.0.0"}'
        ),
        handler_ref="docfidelity.compose.build_content_ir",
    ),
    ToolSpec(
        name="glossary_apply",
        group="content",
        summary="按术语表就地统一 IR 中的文本（不动追溯与来源句柄）。",
        params=(
            _p("ir", "content_ir", "内容 IR"),
            _p("terms", "terminology", "术语表"),
        ),
        example=(
            '{"ir": {"project_id": "p", "doc_key": "overview", "profile_id": "pf", '
            '"template_ref": "demo@1.0.0"}, "terms": {"entries": []}}'
        ),
        handler_ref="docfidelity.compose.apply_glossary_to_ir",
    ),
    ToolSpec(
        name="trace_map",
        group="content",
        summary="由内容 IR 与领域对象构造追溯矩阵（含孤儿与未使用项）。",
        params=(
            _p("ir", "content_ir", "内容 IR"),
            _p("objects", "domain_object_list", "本次装配吃进去的全部领域对象"),
            _p("untraceable", "str_list", "结构单元 id，不计入孤儿", required=False),
        ),
        example='{"ir": {"project_id": "p", "doc_key": "overview", "profile_id": "pf", "template_ref": "demo@1.0.0"}, "objects": []}',
        handler_ref="docfidelity.compose.build_traces",
    ),
    ToolSpec(
        name="unit_section",
        group="content",
        summary="按内容单元类型生成一节内容。",
        example="现架构下由投影规则完成，无独立工具可调",
        source_name="endpoint_section",
        availability="unbound",
        context_note="没有独立入口：分单元的章节生成 = content_ir_build + 档案投影规则",
    ),
    ToolSpec(
        name="table_section",
        group="content",
        summary="按内容单元构造一张规范化表格（列宽按版心归一，只缩不放）。",
        example="渲染阶段持有渲染上下文与表格原型时调用",
        availability="context_bound",
        handler_ref="docfidelity.render.build_table",
        context_note=(
            "渲染流水线的**中途步骤**：入参 RenderContext 携带列宽基准与表格原型"
            "（模板副本 DOM 的派生物），无独立 JSON 入口；它的落笔边界与 render 阶段重合"
        ),
    ),
    # ------------------------------------------------------------ 渲染（M4）
    ToolSpec(
        name="slot_plan",
        group="render",
        summary="定位模板副本中的受保护版块与内容区，得到槽位挂载方案。",
        example="渲染阶段持有渲染上下文与模板副本时调用",
        availability="context_bound",
        handler_ref="docfidelity.render.locate_regions",
        context_note=(
            "渲染流水线的**中途步骤**：版块定位依赖模板副本的 DOM（RenderContext 内），"
            "无独立 JSON 入口；其结果只在同一次渲染内被 inject 消费"
        ),
    ),
    ToolSpec(
        name="render_inject",
        group="render",
        summary="把章节内容注入模板副本（样式层零改动，只注入内容）。",
        example="渲染阶段持有模板副本与原型索引时调用",
        availability="context_bound",
        handler_ref="docfidelity.render.inject_chapters",
        context_note=(
            "渲染流水线的**中途步骤**：模板副本、原型索引、受保护区都在 RenderContext 内，"
            "且会改写它；无独立 JSON 入口，门面即 render 阶段本身（ADR-003：落笔只有一条确定性路径）"
        ),
    ),
    ToolSpec(
        name="pagination_apply",
        group="render",
        summary="按分页策略设置与清理分页点。",
        example="渲染阶段持有渲染上下文与分页策略时调用",
        availability="context_bound",
        handler_ref="docfidelity.render.apply_pagination",
        context_note=(
            "渲染流水线的**中途步骤**：入参 RenderContext 已含前序步骤的落位结果，"
            "PaginationPolicy 只是附带的扁平参数；无独立 JSON 入口"
        ),
    ),
    ToolSpec(
        name="figure_place",
        group="render",
        summary="把一张图片按版心等比缩放后放进指定段落（只缩不放）。",
        example="渲染阶段持有渲染上下文与目标段落时调用",
        availability="context_bound",
        handler_ref="docfidelity.render.place_figure",
        context_note=(
            "渲染流水线的**中途步骤**：承载图片的段落句柄来自注入上下文（RenderContext 内），"
            "资源加载器也由调用方以工作区路径注入；无独立 JSON 入口"
        ),
    ),
    ToolSpec(
        name="toc_refresh",
        group="render",
        summary="刷新目录域；无 Office 环境时降级并如实报告。",
        example="渲染流水线保存产物之后调用",
        availability="context_bound",
        handler_ref="docfidelity.render.refresh_stage",
        writes=True,
        requires_approval=True,
        context_note=(
            "渲染流水线的**收尾步骤**：入参 RenderContext 只为记录 Finding 与取部件名；"
            "域刷新必须在产物落盘**之后**执行（DD-9.1 的有意偏离），故无独立 JSON 入口"
        ),
    ),
    # ------------------------------------------------------------ 门禁（M5）
    ToolSpec(
        name="qa_run",
        group="gate",
        summary="按内置规则集复查产物，产出门禁报告。",
        params=(
            _p("artifact", "str", "待复查产物的路径（.docx）"),
            _p("template", "template_package", "该产物所用的模板包契约"),
            _p("ir", "content_ir", "产物对应的内容 IR"),
            _p(
                "profile",
                "doc_type_profile",
                "文档类型档案；不给则跳过依赖它的规则",
                required=False,
            ),
            _p("traces", "trace_matrix", "追溯矩阵；不给则跳过追溯类规则", required=False),
            _p("terminology", "terminology", "术语表；不给则跳过术语类规则", required=False),
            _p("rule_set_ids", "str_list", "要跑的规则集标识；留空跑全部内置", required=False),
            _p("job_id", "str", "作业标识（写入报告）", required=False),
        ),
        example=(
            '{"artifact": "work/src/out.docx", "template": {"manifest": {}}, '
            '"ir": {"doc_key": "overview"}}'
        ),
        handler_ref="docfidelity.qa.audit_document",
        path_params=("artifact",),
    ),
    ToolSpec(
        name="style_audit",
        group="gate",
        summary="按指定规则集复查版式与样式类问题。",
        params=(
            _p("artifact", "str", "待复查产物的路径（.docx）"),
            _p("template", "template_package", "该产物所用的模板包契约"),
            _p("ir", "content_ir", "产物对应的内容 IR"),
            _p("rule_set_id", "str", "要跑的规则集标识"),
            _p(
                "profile",
                "doc_type_profile",
                "文档类型档案；不给则跳过依赖它的规则",
                required=False,
            ),
            _p("traces", "trace_matrix", "追溯矩阵；不给则跳过追溯类规则", required=False),
            _p("terminology", "terminology", "术语表；不给则跳过术语类规则", required=False),
            _p("job_id", "str", "作业标识（写入报告）", required=False),
        ),
        example=(
            '{"artifact": "work/src/out.docx", "template": {"manifest": {}}, '
            '"ir": {"doc_key": "overview"}, "rule_set_id": "builtin"}'
        ),
        handler_ref="docfidelity.qa.audit_rule_set",
        path_params=("artifact",),
    ),
    ToolSpec(
        name="drift_check",
        group="gate",
        summary="把候选产物与基线产物做结构比对，输出漂移清单。",
        params=(
            _p("baseline", "str", "基线产物的路径"),
            _p("candidate", "str", "候选产物的路径"),
            _p("mode", "str", "比对分辨率：structure / text / contract", required=False),
            _p("baseline_ref", "str", "基线的界面标识（原样带进结果）", required=False),
            _p("candidate_ref", "str", "候选的界面标识（原样带进结果）", required=False),
        ),
        example=(
            '{"baseline": "var/baseline.docx", "candidate": "work/src/out.docx", "mode": "structure"}'
        ),
        handler_ref="docfidelity.qa.compare_documents",
        path_params=("baseline", "candidate"),
    ),
)

#: 名称 → 规格。白名单的**唯一**查询入口。
TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}
