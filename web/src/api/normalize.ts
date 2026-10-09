/**
 * 传输边界归一化（`types.ts` 里 `WithRequiredArrays` 的运行时那一半）。
 *
 * 为什么要有这一层：Pydantic 对带 `default_factory=list` 的字段生成的 JSON Schema 是
 * **非必填**的，于是生成类型里写成 `children?: Job[]`。但服务端序列化的是真实模型实例，
 * 空的时候给 `[]`、绝不省略字段。所以"字段缺失"这一情形只存在于类型系统里。
 *
 * 处理方式是**在边界断言一次**，而不是在每个组件里写 `?? []`。后者有两个代价：
 * 一是三十处重复；二是"契约保证"与"渲染兜底"混为一谈——过一阵就没人判断得出某个
 * `?? []` 是多余的还是必需的，于是真正的缺失会被静默吃掉。
 *
 * 纪律：这里**只补空数组**。不填默认值、不改字段名、不做单位换算——那些属于服务端。
 */

import type {
  ArtifactCompareOut,
  ArtifactGroupOut,
  CleanupPlanOut,
  ContractSummaryOut,
  DocumentDiffOut,
  DomainModelLibraryResponse,
  DomainModelOut,
  DriftReportOut,
  ExpiredResponse,
  InboxResponse,
  Job,
  JobDetail,
  JobListResponse,
  PrecheckReportOut,
  ProfileDetailOut,
  ProfileImportOut,
  ProfileLibraryResponse,
  ProfileRegistrationOut,
  ProjectArtifactsOut,
  ProjectOverviewOut,
  RegisteredDomainModelOut,
  Report,
  Schemas,
  SourceItemsOut,
  SourceListResponse,
  SourceOut,
  StagingPurgeOut,
  StagingResponse,
  TemplateDetailOut,
  TemplateDiffOut,
  TemplateImportOut,
  TemplateLibraryResponse,
  TemplateReferencesOut,
} from "./types";

export function normalizeJob(raw: Schemas["Job"]): Job {
  return {
    ...raw,
    child_job_ids: raw.child_job_ids ?? [],
    artifact_ids: raw.artifact_ids ?? [],
  };
}

export function normalizeJobList(raw: Schemas["JobListResponse"]): JobListResponse {
  return { ...raw, items: (raw.items ?? []).map(normalizeJob) };
}

export function normalizeJobDetail(raw: Schemas["JobDetail"]): JobDetail {
  return {
    ...raw,
    job: normalizeJob(raw.job),
    stages: raw.stages ?? [],
    children: (raw.children ?? []).map(normalizeJob),
    resume_plan: raw.resume_plan ?? [],
  };
}

export function normalizeReport(raw: Schemas["Report"]): Report {
  return {
    ...raw,
    findings: raw.findings ?? [],
    waivers: raw.waivers ?? [],
    adjudications: raw.adjudications ?? [],
    skipped: raw.skipped ?? [],
  };
}

export function normalizeInbox(raw: Schemas["InboxResponse"]): InboxResponse {
  return { ...raw, items: raw.items ?? [] };
}

/**
 * 前置检查结果。
 *
 * 比其它几个多一处：`issues[].remediation` 也要补空数组——它是**逐个问题**给动作的，
 * 而渲染错误三段式时"可以做什么"那一段直接读它。不补的话 `undefined.map` 会在
 * "某项检查没过、但那条问题恰好没给补救话术"时崩掉。
 */
export function normalizePrecheck(raw: Schemas["PrecheckReportOut"]): PrecheckReportOut {
  return {
    ...raw,
    issues: (raw.issues ?? []).map((issue) => ({ ...issue, remediation: issue.remediation ?? [] })),
    doc_keys: raw.doc_keys ?? [],
    skipped: raw.skipped ?? [],
    missing_stages: raw.missing_stages ?? [],
    registered_stages: raw.registered_stages ?? [],
  };
}

export function normalizeExpired(raw: Schemas["ExpiredResponse"]): ExpiredResponse {
  return { ...raw, expired: raw.expired ?? [] };
}

export function normalizeCleanupPlan(raw: Schemas["CleanupPlanOut"]): CleanupPlanOut {
  return {
    ...raw,
    uploads: raw.uploads ?? [],
    workspace: raw.workspace ?? [],
    artifacts: raw.artifacts ?? [],
  };
}

/** 暂存目录清单。**不按 `reclaimable` 过滤**：不可回收的项正是要让人看见的那些。 */
export function normalizeStaging(raw: Schemas["StagingResponse"]): StagingResponse {
  return { ...raw, imports: raw.imports ?? [], note: raw.note ?? "" };
}

/** 暂存回收回执。与 `CleanupPlanOut` 分开：前者列的是导入标识，后者列的是保留对象。 */
export function normalizeStagingPurge(raw: Schemas["StagingPurgeOut"]): StagingPurgeOut {
  return { ...raw, reclaimed: raw.reclaimed ?? [] };
}

/**
 * 模板导入记录。
 *
 * 五处数组都要补（`stages` / `findings` / `pending` / `decisions` / `integrity`）：
 * 界面第 2 步直接 `stages.map` 画进度条、第 4 步直接 `pending.map` 出清单，
 * 补在这里一处比在每张卡片里写 `?? []` 更能让"契约保证给了 `[]`"这件事只有一个出口。
 */
export function normalizeTemplateImport(raw: Schemas["TemplateImportOut"]): TemplateImportOut {
  return {
    ...raw,
    template_id: raw.template_id ?? "",
    version: raw.version ?? "",
    template_id_source: raw.template_id_source ?? "",
    version_source: raw.version_source ?? "",
    version_source_explicit: raw.version_source_explicit ?? false,
    register_only: raw.register_only ?? false,
    parsed: raw.parsed ?? false,
    package_hash: raw.package_hash ?? null,
    source_hash: raw.source_hash ?? null,
    doc_type: raw.doc_type ?? null,
    notes: raw.notes ?? null,
    author: raw.author ?? null,
    stages: (raw.stages ?? []).map((stage) => ({
      ...stage,
      error:
        stage.error === null || stage.error === undefined
          ? null
          : { ...stage.error, remediation: stage.error.remediation ?? [] },
    })),
    findings: raw.findings ?? [],
    pending: raw.pending ?? [],
    pending_count: raw.pending_count ?? 0,
    decisions: raw.decisions ?? [],
    integrity: raw.integrity ?? [],
    summary:
      raw.summary === null || raw.summary === undefined
        ? null
        : {
            ...raw.summary,
            block_counts: raw.summary.block_counts ?? {},
            placeholder_parts: raw.summary.placeholder_parts ?? {},
          },
    registration: raw.registration ?? null,
    // `reparsed_at` 是"可空字段"，**不补默认值**：`null` 与"从没重解析过"是同一件事，
    // 而给它编一个时间会把界面上的"未重解析"变成一句假话。
    can_reparse: raw.can_reparse ?? true,
    reparse_blocked_reason: raw.reparse_blocked_reason ?? null,
  };
}

export function normalizeTemplateLibrary(
  raw: Schemas["TemplateLibraryResponse"],
): TemplateLibraryResponse {
  return { ...raw, items: raw.items ?? [] };
}

/** 摘要卡：两个分布表在契约里是"可有默认值"的字典，线路上的值是 `{}`。 */
function normalizeSummary(
  raw: Schemas["ContractSummaryOut"] | null | undefined,
): ContractSummaryOut | null {
  if (raw === null || raw === undefined) return null;
  return {
    ...raw,
    block_counts: raw.block_counts ?? {},
    placeholder_parts: raw.placeholder_parts ?? {},
  };
}

/**
 * 模板包详情（P04）。
 *
 * `package` / `summary` **原样透传 `null`**：`null` 是"这次登记只有原件"这个事实本身，
 * 兜成空对象会让界面把"未解析"渲染成"解析出来什么都没有"——那正好是硬规则 11 要挡的
 * 那种伪装。
 */
export function normalizeTemplateDetail(raw: Schemas["TemplateDetailOut"]): TemplateDetailOut {
  return {
    ...raw,
    parsed: raw.parsed ?? false,
    package: raw.package ?? null,
    summary: normalizeSummary(raw.summary),
    versions: raw.versions ?? [],
    integrity: raw.integrity ?? [],
    fingerprint_problems: raw.fingerprint_problems ?? [],
  };
}

export function normalizeTemplateDiff(raw: Schemas["TemplateDiffOut"]): TemplateDiffOut {
  return {
    ...raw,
    changelog: raw.changelog ?? [],
    sections: (raw.sections ?? []).map((section) => ({
      ...section,
      changes: section.changes ?? [],
    })),
    blocking_sections: raw.blocking_sections ?? [],
    changed_sections: raw.changed_sections ?? [],
  };
}

/**
 * 一条领域模型声明。
 *
 * `comparable` / `traceable` / `fields` 在契约里都是"可有默认值"的，线路上的值分别是
 * `[]` / `[]` / `{}`——但只对**引用形态**成立。声明本体进库前会过一遍校验，
 * 那几个字段是必填语义，故这里补齐。
 */
export function normalizeDomainModel(raw: Schemas["DomainModel-Output"]): DomainModelOut {
  return {
    ...raw,
    comparable: raw.comparable ?? [],
    traceable: raw.traceable ?? [],
    fields: raw.fields ?? {},
  };
}

/** 库里的一行：本体跟着一起归一化，界面拿到的 `model` 一定是补齐过的。 */
export function normalizeDomainModelRow(
  raw: Schemas["RegisteredDomainModelOut"],
): RegisteredDomainModelOut {
  return { ...raw, model: normalizeDomainModel(raw.model) };
}

export function normalizeDomainModelLibrary(
  raw: Schemas["DomainModelLibraryResponse"],
): DomainModelLibraryResponse {
  return {
    ...raw,
    // `active_count` **不在这里算**：它是服务端的结论（见 types.ts 的说明）。
    items: (raw.items ?? []).map(normalizeDomainModelRow),
  };
}

export function normalizeProfileLibrary(
  raw: Schemas["ProfileLibraryResponse"],
): ProfileLibraryResponse {
  return { ...raw, items: raw.items ?? [] };
}

export function normalizeProfileDetail(raw: Schemas["ProfileDetailOut"]): ProfileDetailOut {
  return { ...raw, versions: raw.versions ?? [] };
}

export function normalizeProfileRegistration(
  raw: Schemas["ProfileRegistrationOut"],
): ProfileRegistrationOut {
  return { ...raw, skipped: raw.skipped ?? [], findings: raw.findings ?? [] };
}

/** 导入回执：把两个"契约里可选、线路上恒在"的数组补齐（理由同文件头）。 */
export function normalizeProfileImport(raw: Schemas["ProfileImportOut"]): ProfileImportOut {
  return {
    ...raw,
    profile: normalizeProfileRegistration(raw.profile),
    templates: raw.templates ?? [],
    unused_templates: raw.unused_templates ?? [],
  };
}

export function normalizeSource(raw: Schemas["SourceOut"]): SourceOut {
  return { ...raw, bindings: raw.bindings ?? [] };
}

export function normalizeSourceList(raw: Schemas["SourceListResponse"]): SourceListResponse {
  return { ...raw, items: (raw.items ?? []).map(normalizeSource), known_types: raw.known_types ?? [] };
}

export function normalizeSourceItems(raw: Schemas["SourceItemsOut"]): SourceItemsOut {
  return {
    ...raw,
    kinds: raw.kinds ?? {},
    facts: raw.facts ?? [],
    assets: raw.assets ?? [],
    warnings: raw.warnings ?? [],
  };
}

/* ------------------------------------------------- 项目状态（P02 / P11 / P14） */

/**
 * P02 的一屏数据。
 *
 * `last_job` 与 `precheck` **原样透传 `null`**：前者是"这个项目还没跑过作业"，
 * 后者是"服务端这次没给结论"。兜成一个默认对象会让"没有结论"画成"结论已通过"——
 * 而 P02 上的发起按钮正是拿 `precheck.ok` 决定可用性的。
 *
 * `metrics` 的缺省补成全零：它是 `default_factory`（生成类型说可选），而线路上恒有值。
 * 与其它标量默认值同一条口径（见文件头纪律）——这里的"补"是边界断言，不是业务判断：
 * 五个数的口径全部在 `server/overview.py`，界面一个都不算。
 */
export function normalizeProjectOverview(raw: Schemas["ProjectOverviewOut"]): ProjectOverviewOut {
  return {
    ...raw,
    metrics: raw.metrics ?? {
      documents: 0,
      templates: 0,
      sources: 0,
      artifacts: 0,
      pending_decisions: 0,
    },
    steps: raw.steps ?? [],
    pending: raw.pending ?? [],
    pending_total: raw.pending_total ?? 0,
    last_job:
      raw.last_job === undefined || raw.last_job === null
        ? null
        : { ...raw.last_job, progress: raw.last_job.progress ?? null },
    has_config: raw.has_config ?? false,
    precheck: raw.precheck === undefined || raw.precheck === null ? null : normalizePrecheck(raw.precheck),
  };
}

/** 一层产物分组。**递归**：`children` 的每一层都要走同一条补空规则，否则第二层又得写 `?? []`。 */
function normalizeArtifactGroup(raw: Schemas["ArtifactGroupOut"]): ArtifactGroupOut {
  return {
    ...raw,
    artifacts: raw.artifacts ?? [],
    children: (raw.children ?? []).map(normalizeArtifactGroup),
    report_verdict: raw.report_verdict ?? null,
  };
}

export function normalizeProjectArtifacts(
  raw: Schemas["ProjectArtifactsOut"],
): ProjectArtifactsOut {
  return {
    ...raw,
    groups: (raw.groups ?? []).map(normalizeArtifactGroup),
    count: raw.count ?? 0,
    total_bytes: raw.total_bytes ?? 0,
  };
}

/**
 * 一份差异报告。
 *
 * 只补四个数组；`equal` / `truncated` / `normalized` 原样透传——它们是服务端的结论，
 * 界面照抄（也正因如此，`normalizeArtifactCompare` 不会拿三个数组是否为空重算 `equal`）。
 */
function normalizeDocumentDiff(raw: Schemas["DocumentDiffOut"]): DocumentDiffOut {
  return {
    ...raw,
    added: raw.added ?? [],
    removed: raw.removed ?? [],
    changed: raw.changed ?? [],
    notes: raw.notes ?? [],
  };
}

export function normalizeArtifactCompare(raw: Schemas["ArtifactCompareOut"]): ArtifactCompareOut {
  return { ...raw, diff: normalizeDocumentDiff(raw.diff) };
}

/**
 * P14 的漂移清单。
 *
 * `fact_level_note` 的缺省补成空串，而不是补一句解释：那句注释**只能**来自服务端
 * （它是"为什么这一类判不了"的官方说明）。界面另有一条判断——`fact_level_available`
 * 为 `false` 时把 note 显示出来——所以这里补空串只会让"服务端没说原因"这件异常显形，
 * 不会伪装成"没有问题"。
 */
export function normalizeDriftReport(raw: Schemas["DriftReportOut"]): DriftReportOut {
  return {
    ...raw,
    items: (raw.items ?? []).map((item) => ({
      ...item,
      last_job_id: item.last_job_id ?? null,
      last_generated_at: item.last_generated_at ?? null,
    })),
    checked_documents: raw.checked_documents ?? 0,
    fact_level_available: raw.fact_level_available ?? false,
    fact_level_note: raw.fact_level_note ?? "",
  };
}

/** 反向引用清单。`count` / `active_count` 是服务端算的，界面不重算（更不拿 `items.length` 当答案）。 */
export function normalizeTemplateReferences(
  raw: Schemas["TemplateReferencesOut"],
): TemplateReferencesOut {
  return {
    ...raw,
    items: (raw.items ?? []).map((item) => ({ ...item, declared_in: item.declared_in ?? [] })),
    count: raw.count ?? 0,
    active_count: raw.active_count ?? 0,
  };
}
