/**
 * 接口调用层：一个函数对应一个端点。
 *
 * 三条纪律：
 * ① **不做业务判断**——不在这里"根据状态算该显示什么"，那是页面的事，而页面的
 *    依据也只能是服务端给的结论（14.5 / NFR-22）；
 * ② **不缓存**——缓存策略由 TanStack Query 在调用点声明，这里只发请求；
 * ③ 路径里的 id 一律 `encodeURIComponent`——产物 id 已经保证 URL 安全（`a_<摘要>`），
 *    但项目名/docKey 来自用户，编码是这一层该负的责任。
 */

import { request } from "./client";
import {
  normalizeArtifactCompare,
  normalizeCleanupPlan,
  normalizeDriftReport,
  normalizeDomainModelLibrary,
  normalizeDomainModelRow,
  normalizeExpired,
  normalizeInbox,
  normalizeJob,
  normalizeJobDetail,
  normalizeJobList,
  normalizePrecheck,
  normalizeProfileDetail,
  normalizeProfileImport,
  normalizeProfileLibrary,
  normalizeProfileRegistration,
  normalizeProjectArtifacts,
  normalizeProjectOverview,
  normalizeReport,
  normalizeSource,
  normalizeSourceItems,
  normalizeSourceList,
  normalizeStaging,
  normalizeStagingPurge,
  normalizeTemplateDetail,
  normalizeTemplateDiff,
  normalizeTemplateImport,
  normalizeTemplateLibrary,
  normalizeTemplateReferences,
} from "./normalize";
import type {
  AdjudicationRequest,
  AdjudicationResponse,
  Artifact,
  ArtifactCompareOut,
  ArtifactCompareRequest,
  CleanupPlanOut,
  ConfigPutResponse,
  ConfigVersionOut,
  DriftReportOut,
  DomainModelDeprecateRequest,
  DomainModelLibraryResponse,
  DomainModelRegisterRequest,
  DomainModelRegistrationOut,
  ExpiredResponse,
  InboxResponse,
  Job,
  JobDetail,
  JobListResponse,
  ModelCapabilities,
  ModelHealth,
  PrecheckReportOut,
  ProfileDeprecateRequest,
  ProfileDetailOut,
  ProfileImportOut,
  ProfileLibraryResponse,
  ProfileRegisterRequest,
  ProfileRegistrationOut,
  ProjectArtifactsOut,
  ProjectConfig,
  ProjectOut,
  ProjectOverviewOut,
  RegisteredDomainModelOut,
  RegisteredProfileOut,
  RegisteredTemplateOut,
  RegisterRequest,
  Report,
  ReviewRequest,
  Schemas,
  SettingsView,
  SourceCreateRequest,
  SourceCreateResponse,
  SourceItemsOut,
  SourceListResponse,
  SourceProbeOut,
  StagingPurgeOut,
  StagingResponse,
  TemplateDeprecateRequest,
  TemplateDetailOut,
  TemplateDiffOut,
  TemplateImportOut,
  TemplateLibraryResponse,
  TemplateReferencesOut,
  UploadOut,
} from "./types";

/**
 * 首启装载（`server/seed.py`）的结果视图，随 `/health` 一起下发。
 *
 * 三个字段回答三件事，缺一件就要去看日志：`skipped`（**为什么没动手**——库不是空的）、
 * `seeded`（装进去的是哪几份，用 `id@version` 引用键）、`errors`（逐份隔离的失败原文）。
 *
 * 只在**配了装载**时才出现（没配即 `null`）：一个恒为 `null` 的字段会被读成"装载失败了"。
 */
export interface DemoSeedView {
  skipped: boolean;
  seeded: string[];
  errors: string[];
}

/**
 * `/health` 的响应。
 *
 * 手写而非从 OpenAPI 生成：该端点**不返回业务数据**，它的用途是"部署者一眼看到
 * 流水线是否就绪"，且刻意放在根路径（非版本化）。若它也纳入契约，就会出现
 * "探针格式变更需要走版本流程"的荒谬约束。
 *
 * `worker_mode` 与 `demo_seed` 是**部署形态**的两条事实（DD-18.1），设置页据此做
 * "界面显示"与"实际部署"的一致性核对——它们不属于 `SettingsView`，因为那是**配置**
 * （可改），而这两条是**运行现状**（只读）。
 */
export interface HealthResponse {
  status: string;
  storage_root: string;
  repository: string;
  stages: { registered: string[]; missing: string[] };
  build_ready: boolean;
  model_enabled: boolean;
  worker_running: boolean;
  /** 本进程有没有在消费作业。`external` 表示消费由独立进程承担（`df-worker`）。 */
  worker_mode: "in-process" | "external";
  /** 首启装载结果；**没配装载时为 `null`**（而不是"装了 0 份"）。 */
  demo_seed: DemoSeedView | null;
}

const enc = encodeURIComponent;

/* ------------------------------------------------------------------ 项目 */
export const projectsApi = {
  list: (includeArchived = false): Promise<ProjectOut[]> =>
    request("/projects", { query: { include_archived: includeArchived } }),

  create: (name: string, demo = false): Promise<ProjectOut> =>
    request("/projects", { method: "POST", json: { name, demo } }),

  get: (projectId: string): Promise<ProjectOut> => request(`/projects/${enc(projectId)}`),

  rename: (projectId: string, name: string): Promise<ProjectOut> =>
    request(`/projects/${enc(projectId)}`, { method: "PATCH", json: { name } }),

  setArchived: (projectId: string, archived: boolean): Promise<ProjectOut> =>
    request(`/projects/${enc(projectId)}`, { method: "PATCH", json: { archived } }),

  demoInit: (projectId: string): Promise<ProjectConfig> =>
    request(`/projects/${enc(projectId)}/demo-init`, { method: "POST" }),

  getConfig: (projectId: string): Promise<ProjectConfig> =>
    request(`/projects/${enc(projectId)}/config`),

  putConfig: (
    projectId: string,
    config: ProjectConfig,
    author?: string,
  ): Promise<ConfigPutResponse> =>
    request(`/projects/${enc(projectId)}/config`, {
      method: "PUT",
      json: config,
      query: { author },
    }),

  listConfigVersions: (projectId: string): Promise<ConfigVersionOut[]> =>
    request(`/projects/${enc(projectId)}/config/versions`),

  /**
   * 某一个历史版本的配置**本体**（P07 的「变更留痕 / 回滚」）。
   *
   * 响应形状与"读最新"完全相同（就是 `ProjectConfig`），这样"回滚 = 以历史版本为内容
   * 再写一版"不需要专用通道：界面拿到它，原样 `putConfig` 一次即可。服务端不提供
   * `POST /config/rollback` 是有意的——那会引入一条"配置版本可以跳回去"的隐含规则，
   * 而实际发生的事只是"又写了一版内容与旧版相同的新版本"。
   */
  configVersion: (projectId: string, version: number): Promise<ProjectConfig> =>
    request(`/projects/${enc(projectId)}/config/versions/${enc(String(version))}`),

  /**
   * P02 项目总览的一屏数据。
   *
   * 指标 / 三步引导 / 待办 Top N / 最近作业 / 前置检查**一次取齐**：让界面拉四份列表
   * 自己数，等于把"什么算完成一步""最近一次构建算不算成功"这些业务结论摊到浏览器里
   * （NFR-22），而且每加一张卡片就多一处口径。
   */
  overview: (projectId: string): Promise<ProjectOverviewOut> =>
    request<Schemas["ProjectOverviewOut"]>(`/projects/${enc(projectId)}/overview`).then(
      normalizeProjectOverview,
    ),

  /**
   * P14 的漂移清单。
   *
   * `fact_level_available=false` 时**必须**把 `fact_level_note` 显示出来：那是"这一类
   * 判不了"的官方说明，不是可以省略的备注（硬规则 11：缺能力跳过不得渲染成没有漂移）。
   */
  drift: (projectId: string): Promise<DriftReportOut> =>
    request<Schemas["DriftReportOut"]>(`/projects/${enc(projectId)}/drift`).then(
      normalizeDriftReport,
    ),

  /**
   * 前置检查（P08 的"为什么不能发起"）。
   *
   * `docKeys` 为空/省略 ⇒ 不发送请求体，服务端检查配置里全部启用文档。**注意**别传空数组：
   * 服务端会把 `[]` 读成"一份都没选中"（`no_documents`），而界面上的"还没选"是 UI 状态、
   * 不是服务端规则，那一条要由调用点自己拦（见 `P08Jobs`）。
   *
   * 返回值里的 `ok` 是**服务端结论**，调用点直接拿它决定按钮可用性，不按 `issues` 是否为空再推一遍。
   */
  validate: (projectId: string, docKeys?: readonly string[]): Promise<PrecheckReportOut> =>
    request<Schemas["PrecheckReportOut"]>(`/projects/${enc(projectId)}/config/validate`, {
      method: "POST",
      ...(docKeys === undefined ? {} : { json: { doc_keys: [...docKeys] } }),
    }).then(normalizePrecheck),
};

/* ------------------------------------------------------------------ 上传 */
export const uploadsApi = {
  /** 收件（四道闸门在服务端；这里只负责把字节递过去）。 */
  create: (file: File, purpose = "template"): Promise<UploadOut> => {
    const form = new FormData();
    form.append("file", file);
    return request("/uploads", { method: "POST", form, query: { purpose } });
  },

  list: (): Promise<UploadOut[]> => request("/uploads"),
};

/* ------------------------------------------------------------------ 作业 */
export const jobsApi = {
  /**
   * 发起构建。
   *
   * `author` 只落成留痕（`Job.initiated_by`，作业列表的「发起人」列）——**不进**
   * `inputs_hash`，所以"换个人点同一个按钮"不会让断点失效。
   */
  submit: (projectId: string, docKeys?: readonly string[], author?: string): Promise<Job> =>
    request<Schemas["Job"]>(`/projects/${enc(projectId)}/jobs`, {
      method: "POST",
      json: {
        doc_keys: docKeys === undefined ? null : [...docKeys],
        author: author === undefined || author === "" ? null : author,
      },
    }).then(normalizeJob),

  listByProject: (projectId: string, status?: string): Promise<JobListResponse> =>
    request<Schemas["JobListResponse"]>(`/projects/${enc(projectId)}/jobs`, {
      query: { status },
    }).then(normalizeJobList),

  list: (status?: string): Promise<JobListResponse> =>
    request<Schemas["JobListResponse"]>("/jobs", { query: { status } }).then(normalizeJobList),

  detail: (jobId: string): Promise<JobDetail> =>
    request<Schemas["JobDetail"]>(`/jobs/${enc(jobId)}`).then(normalizeJobDetail),

  cancel: (jobId: string): Promise<Job> =>
    request<Schemas["Job"]>(`/jobs/${enc(jobId)}/cancel`, { method: "POST" }).then(normalizeJob),

  /** `mode=stage` 只重跑失败阶段（复用上游断点）；`mode=full` 丢弃全部断点从头跑。 */
  retry: (jobId: string, mode: "stage" | "full" = "stage"): Promise<Job> =>
    request<Schemas["Job"]>(`/jobs/${enc(jobId)}/retry`, {
      method: "POST",
      query: { mode },
    }).then(normalizeJob),

  resume: (jobId: string, fromStage?: string): Promise<Job> =>
    request<Schemas["Job"]>(`/jobs/${enc(jobId)}/resume`, {
      method: "POST",
      json: { from_stage: fromStage ?? null },
    }).then(normalizeJob),

  rerunFailed: (jobId: string): Promise<Job[]> =>
    request<Schemas["Job"][]>(`/jobs/${enc(jobId)}/rerun-failed`, { method: "POST" }).then(
      (items) => items.map(normalizeJob),
    ),

  report: (jobId: string): Promise<Report> =>
    request<Schemas["Report"]>(`/jobs/${enc(jobId)}/report`).then(normalizeReport),

  artifacts: (jobId: string): Promise<Artifact[]> => request(`/jobs/${enc(jobId)}/artifacts`),
};

/* -------------------------------------------------------------- 产物与对比 */
/**
 * P11：项目级产物聚合与两两比对。
 *
 * 与 `jobsApi.artifacts(jobId)` 的分工：那个是"某次作业的产物清单"（P09 用），
 * 这个是**整个项目**的产物按"编排作业 → 子作业 → 文档"三层分组（P11 用）。
 * 两者不能合成一个：前者不含跨作业的历史，后者的读者要的是"这个项目一共产出过什么"。
 */
export const artifactsApi = {
  listByProject: (projectId: string): Promise<ProjectArtifactsOut> =>
    request<Schemas["ProjectArtifactsOut"]>(`/projects/${enc(projectId)}/artifacts`).then(
      normalizeProjectArtifacts,
    ),

  /**
   * 比对两份 docx 产物。
   *
   * 两份里只要有一份不是 docx，由**服务端**报 `E_SCHEMA_INVALID`（"该产物不是可比的
   * 文档"）；比对器不认识的 `mode` 报同一条错误码。两者都不在界面预判——否则那句
   * 补救话术（"只比对 kind=docx 的产物"）就没机会出现了。
   */
  compare: (body: ArtifactCompareRequest): Promise<ArtifactCompareOut> =>
    request<Schemas["ArtifactCompareOut"]>("/artifacts/compare", {
      method: "POST",
      json: body,
    }).then(normalizeArtifactCompare),
};

/* ---------------------------------------------------------------- 裁决 */
export const inboxApi = {
  /** 待裁决项聚合（跨作业）。`projectId` 省略即全站。 */
  list: (projectId?: string): Promise<InboxResponse> =>
    request<Schemas["InboxResponse"]>("/inbox", { query: { project_id: projectId } }).then(
      normalizeInbox,
    ),
};

export const decisionsApi = {
  /**
   * 提交一次裁决。
   *
   * `subjectId` = 待裁决项标识（判定 `finding_id` 或决策 `decision_id`），取自 `/inbox`
   * 的 `subject_id` 或报告里某条判定的 `finding_id`。
   *
   * `reason` 只有 `waive` 需要；缺了由**服务端**报 `E_WAIVER_REASON_REQUIRED`，
   * 前端不预判（把业务规则写成"按钮点不动"会让使用者看不到那句补救话术）。
   */
  submit: (subjectId: string, body: AdjudicationRequest): Promise<AdjudicationResponse> =>
    request<Schemas["AdjudicationResponse"]>(
      `/findings/${enc(subjectId)}/decisions`,
      { method: "POST", json: body },
    ),
};

/* ------------------------------------------------------------------ 模板 */
/**
 * 模板入库与模板库（P05 / P03）。
 *
 * 第 1~3 步是**一个请求**：`POST /templates/imports` 直接把原件的字节收进来，
 * 解析完连报告一起回。所以这里**没有**"先 `POST /uploads` 拿 upload_id、
 * 再提交 import"这样的两步——那条路要求服务端在两次请求之间保持一份上传件，
 * 而它带来的只是一次多余的往返（`/uploads` 仍然服务于"先收件、稍后再用"的场景）。
 */
export const templatesApi = {
  /**
   * 导入模板（第 1~3 步）。
   *
   * 解析失败**不是错误**：响应里 `register_only=true` / `parsed=false`。
   * 调用点不应把这条路径当成异常（DD-7.1 允许"仅登记原件、稍后重试"）。
   */
  import: (
    file: File,
    options: {
      docType?: string;
      templateId?: string;
      version?: string;
      notes?: string;
      author?: string;
    } = {},
  ): Promise<TemplateImportOut> => {
    const form = new FormData();
    form.append("file", file);
    if (options.docType !== undefined && options.docType !== "") {
      form.append("doc_type", options.docType);
    }
    if (options.templateId !== undefined && options.templateId !== "") {
      form.append("template_id", options.templateId);
    }
    if (options.version !== undefined && options.version !== "") {
      form.append("version", options.version);
    }
    if (options.notes !== undefined && options.notes !== "") {
      form.append("notes", options.notes);
    }
    if (options.author !== undefined && options.author !== "") {
      form.append("author", options.author);
    }
    return request<Schemas["TemplateImportOut"]>("/templates/imports", {
      method: "POST",
      form,
    }).then(normalizeTemplateImport);
  },

  /** 读回导入报告（第 2~3 步）。`import_id` 由导入响应给出，可用于刷新后再看。 */
  getImport: (importId: string): Promise<TemplateImportOut> =>
    request<Schemas["TemplateImportOut"]>(`/templates/imports/${enc(importId)}`).then(
      normalizeTemplateImport,
    ),

  /**
   * 提交待确认项处理（第 4 步）。
   *
   * `operator` 必填且是**自由文本**（P0 无登录态，"责任人"只能由填的人署名）；
   * `action=specify` 不带 `value` 由**服务端**报 `E_REVIEW_VALUE_REQUIRED`，
   * 前端不预判——否则那句补救话术就没机会出现在使用者眼前。
   */
  review: (importId: string, body: ReviewRequest): Promise<TemplateImportOut> =>
    request<Schemas["TemplateImportOut"]>(`/templates/imports/${enc(importId)}/decisions`, {
      method: "POST",
      json: body,
    }).then(normalizeTemplateImport),

  /**
   * 登记进库（第 5 步）。
   *
   * 冲突（同 `id@version` 但内容不同）由服务端报 `E_VERSION_CONFLICT`，
   * 并在 `where` 里带出旧/新 `package_hash`；界面据此让人**显式选择**
   * "换个版本号"或"确认覆盖"，绝不自动改版本号。
   */
  register: (importId: string, body: RegisterRequest): Promise<TemplateImportOut> =>
    request<Schemas["TemplateImportOut"]>(`/templates/imports/${enc(importId)}/register`, {
      method: "POST",
      json: body,
    }).then(normalizeTemplateImport),

  /**
   * 重解析（第 3 步的重试）。
   *
   * 只对**尚未登记**的导入有意义：登记之后 `source_hash` 已写进回执，换内容必须走
   * 登记接口的 `on_conflict=overwrite`。能不能重解析由服务端给（`can_reparse` /
   * `reparse_blocked_reason`），界面据此置灰并**原样显示那句话**，不自己归纳。
   *
   * 读的是暂存目录里的原件副本，**不收新上传**——换原件请重新导入（会得到新的 import_id）。
   */
  reparse: (importId: string): Promise<TemplateImportOut> =>
    request<Schemas["TemplateImportOut"]>(
      `/templates/imports/${enc(importId)}/reparse`,
      { method: "POST" },
    ).then(normalizeTemplateImport),

  /** 模板库列表；给 `templateId` 即只列该编号的各版本（与详情端点同一响应形状）。 */
  list: (templateId?: string): Promise<TemplateLibraryResponse> =>
    request<Schemas["TemplateLibraryResponse"]>("/templates", {
      query: { template_id: templateId },
    }).then(normalizeTemplateLibrary),

  /**
   * 某编号的全部版本。
   *
   * 与 `detail` 分开是因为**详情要带上整份契约**（样式表 + 骨架 + 版块索引），
   * 而"只列版本"是一个轻得多的请求——P03 的表格只需要后者。
   */
  versions: (templateId: string): Promise<TemplateLibraryResponse> =>
    request<Schemas["TemplateLibraryResponse"]>(`/templates/${enc(templateId)}/versions`).then(
      normalizeTemplateLibrary,
    ),

  /** 模板包详情（P04 的八个页签一次取齐）。`version` 留空即当前有效的那一版。 */
  detail: (templateId: string, version?: string): Promise<TemplateDetailOut> =>
    request<Schemas["TemplateDetailOut"]>(`/templates/${enc(templateId)}`, {
      query: { version },
    }).then(normalizeTemplateDetail),

  /**
   * 版本之间的字段级差异。
   *
   * `from` / `to` 是线上的参数名（服务端用 `alias` 对齐设计文档，因为 `from` 是
   * Python 关键字）。分区与 `blocking` 判定由服务端算好，界面不自己筛（NFR-22）。
   */
  diff: (templateId: string, from: string, to: string): Promise<TemplateDiffOut> =>
    request<Schemas["TemplateDiffOut"]>(`/templates/${enc(templateId)}/diff`, {
      query: { from, to },
    }).then(normalizeTemplateDiff),

  /** 废弃某版本。**只标不删**：包目录原样保留，已产出的文档照旧可复现。重复废弃幂等。 */
  deprecate: (templateId: string, body: TemplateDeprecateRequest): Promise<RegisteredTemplateOut> =>
    request<Schemas["RegisteredTemplateOut"]>(`/templates/${enc(templateId)}/deprecate`, {
      method: "POST",
      json: body,
    }),

  /**
   * 反向引用：**哪些档案版本引用了这个模板包**（P04 概要页签的「引用该包的文档」）。
   *
   * `version` 省略即"该编号的任意版本"。`active_count`（未废弃的引用数）由服务端算：
   * 界面要回答的是"还能不能废弃这一版"，而那条判断是"有没有未废弃的档案还在引用它"。
   */
  references: (templateId: string, version?: string): Promise<TemplateReferencesOut> =>
    request<Schemas["TemplateReferencesOut"]>(`/templates/${enc(templateId)}/references`, {
      query: { version },
    }).then(normalizeTemplateReferences),
};

/* ---------------------------------------------------------------- 领域模型库 */
/**
 * 领域模型库（P03 的「领域模型」页签）。
 *
 * 它是档案的**词汇表**：档案登记时按它校验 `for_each` 引用的类型（FR-M2-23，未登记
 * 即**拒绝登记**）。所以这一层的顺序是"先词汇表、后档案"——那不是界面上的偏好，
 * 而是服务端判据：空库下登记档案会被 `E_DOMAIN_KIND_UNKNOWN` 拒掉。
 *
 * **没有 `version` 参数**（与模板库 / 档案库刻意不同）：身份就是 `kind`。重复登记
 * 只有三种结局——内容相同幂等 / 内容不同 `409` / 显式 `on_conflict=overwrite` 覆盖，
 * 三者都由服务端按**声明内容**判定，前端不预判。
 */
export const domainModelsApi = {
  list: (options: { includeDeprecated?: boolean } = {}): Promise<DomainModelLibraryResponse> =>
    request<Schemas["DomainModelLibraryResponse"]>("/domain-models", {
      query: { include_deprecated: options.includeDeprecated ?? true },
    }).then(normalizeDomainModelLibrary),

  /**
   * 登记一份声明。
   *
   * `model` 直接就是那份契约（**不另造一份同构的输入模型**）。`on_conflict` 缺省
   * `abort`——覆盖是一次**显式选择**，不是默认行为（默认覆盖会让一次手滑悄悄换掉
   * 已经"生效"的词汇表，而所有引用它的档案的校验口径会跟着变）。
   */
  register: (body: DomainModelRegisterRequest): Promise<DomainModelRegistrationOut> =>
    request<Schemas["DomainModelRegistrationOut"]>("/domain-models", {
      method: "POST",
      json: body,
    }),

  detail: (kind: string): Promise<RegisteredDomainModelOut> =>
    request<Schemas["RegisteredDomainModelOut"]>(`/domain-models/${enc(kind)}`).then(
      normalizeDomainModelRow,
    ),

  /** 只标不删：类型是已登记档案的引用键，也是历史产物的追溯前缀。理由必填。 */
  deprecate: (
    kind: string,
    body: DomainModelDeprecateRequest,
  ): Promise<RegisteredDomainModelOut> =>
    request<Schemas["RegisteredDomainModelOut"]>(`/domain-models/${enc(kind)}/deprecate`, {
      method: "POST",
      json: body,
    }).then(normalizeDomainModelRow),
};

/* -------------------------------------------------------------- 文档类型档案 */
/**
 * 档案库（P03 的「文档类型档案」页签）。
 *
 * 版本号必须由调用方给出（缺省 `1.0`）：档案是人写的一份 JSON，**没有**文档内版本号
 * 可解析（模板包才可以从原件里读出版本）。故 `version` 不是"自动升版"，
 * 要新版本就得显式传。
 */
export const profilesApi = {
  list: (options: { docType?: string; includeDeprecated?: boolean } = {}): Promise<ProfileLibraryResponse> =>
    request<Schemas["ProfileLibraryResponse"]>("/profiles", {
      query: {
        doc_type: options.docType,
        include_deprecated: options.includeDeprecated ?? true,
      },
    }).then(normalizeProfileLibrary),

  /**
   * 登记一个档案版本。
   *
   * `profile` 直接就是那份契约（**不另造一份同构的输入模型**）。校验引用是否已登记
   * 由**服务端**读模板库后判定，故空库下引用 `TPL-*` 的档案会被拒（`E_PROFILE_TEMPLATE_MISSING`）
   * ——那句补救话术（"先在模板库登记这些包"）由服务端给，前端不预判。
   */
  register: (body: ProfileRegisterRequest): Promise<ProfileRegistrationOut> =>
    request<Schemas["ProfileRegistrationOut"]>("/profiles", {
      method: "POST",
      json: body,
    }).then(normalizeProfileRegistration),

  detail: (profileId: string, version?: string): Promise<ProfileDetailOut> =>
    request<Schemas["ProfileDetailOut"]>(`/profiles/${enc(profileId)}`, {
      query: { version },
    }).then(normalizeProfileDetail),

  deprecate: (profileId: string, version: string, body: ProfileDeprecateRequest): Promise<RegisteredProfileOut> =>
    request<Schemas["RegisteredProfileOut"]>(
      `/profiles/${enc(profileId)}/versions/${enc(version)}/deprecate`,
      { method: "POST", json: body },
    ),

  /**
   * 导入一份**档案包**（zip）——验收 45 的收件端那一步。
   *
   * 与 `register` 的分工：`register` 收的是**一份档案本体**，且要求引用到的模板包
   * **已经在库**；`importPackage` 收的是**一整包**（档案本体 + 它引用的模板包 + 样例输入
   * 与适用判据说明），服务端先把包里的模板登记好、再登记档案。也就是说这两个端点对应
   * 的正是"对面什么都没给我"和"对面把整包给了我"两种情形。
   *
   * 走 multipart（`FormData`）：包是二进制 zip，不能塞进 JSON 字段。
   *
   * 包本身不可用时由**服务端**报 `E_PROFILE_PACKAGE_INVALID`（不是 zip、条目越界、
   * 缺 `profile.json`、引用了包里没有的模板……）。界面不预判——那些判据都在服务端，
   * 且补救话术由服务端给出。
   */
  importPackage: (
    file: File,
    options: { version?: string; onConflict?: string; author?: string } = {},
  ): Promise<ProfileImportOut> => {
    const form = new FormData();
    form.append("file", file);
    return request<Schemas["ProfileImportOut"]>("/profiles/import", {
      method: "POST",
      form,
      query: {
        version: options.version,
        on_conflict: options.onConflict,
        author: options.author,
      },
    }).then(normalizeProfileImport);
  },
};

/* ------------------------------------------------------------------ 事实源 */
/**
 * 事实源声明与接入（P06）。
 *
 * `probe` 与 `items` 的分工写在**服务端**：前者回答"这条来源现在通不通"（**永不抛**，
 * `ok=false` 是一次正常回答），后者回答"通的话里面是什么"（**会抛**）。
 * 前端不把两者合成一步——合并后界面就分不清"连不通"与"连得通但内容是空的"，
 * 而这两种状态要给的下一步动作完全不同。
 */
export const sourcesApi = {
  list: (projectId: string): Promise<SourceListResponse> =>
    request<Schemas["SourceListResponse"]>(`/projects/${enc(projectId)}/sources`).then(
      normalizeSourceList,
    ),

  create: (projectId: string, body: SourceCreateRequest): Promise<SourceCreateResponse> =>
    request<Schemas["SourceCreateResponse"]>(`/projects/${enc(projectId)}/sources`, {
      method: "POST",
      json: body,
    }).then((raw) => ({ ...raw, source: normalizeSource(raw.source) })),

  remove: (projectId: string, sourceId: string): Promise<{ removed: string }> =>
    request(`/projects/${enc(projectId)}/sources/${enc(sourceId)}`, { method: "DELETE" }),

  probe: (projectId: string, sourceId: string): Promise<SourceProbeOut> =>
    request<Schemas["SourceProbeOut"]>(
      `/projects/${enc(projectId)}/sources/${enc(sourceId)}/probe`,
      { method: "POST" },
    ).then((raw) => ({ ...raw, warnings: raw.warnings ?? [] })),

  items: (projectId: string, sourceId: string, limit?: number): Promise<SourceItemsOut> =>
    request<Schemas["SourceItemsOut"]>(
      `/projects/${enc(projectId)}/sources/${enc(sourceId)}/items`,
      { query: { limit } },
    ).then(normalizeSourceItems),
};

/* ------------------------------------------------------------------ 系统 */
export const systemApi = {
  settings: (): Promise<SettingsView> => request("/settings"),

  updateSettings: (patch: Record<string, unknown>): Promise<SettingsView> =>
    request("/settings", { method: "PUT", json: { patch } }),

  modelHealth: (): Promise<ModelHealth> => request("/settings/model/health"),

  /**
   * 模型能力现状：受限解码探测 + 基线评测状态。
   *
   * 与 `modelHealth` 不同，这个端点**不出网**（一项查本机依赖、一项读存储里的报告），
   * 所以挂载时直接取，不需要「探测」按钮。
   */
  modelCapabilities: (): Promise<ModelCapabilities> => request("/settings/model/capabilities"),

  expired: (): Promise<ExpiredResponse> =>
    request<Schemas["ExpiredResponse"]>("/maintenance/expired").then(normalizeExpired),

  cleanup: (objects: readonly string[]): Promise<CleanupPlanOut> =>
    request<Schemas["CleanupPlanOut"]>("/maintenance/cleanup", {
      method: "POST",
      json: { objects: [...objects] },
    }).then(normalizeCleanupPlan),

  /**
   * 暂存目录清单（`imports/`）。
   *
   * 与「已过期对象」分开：暂存不在保留策略内，它的判据是**登记了没有**——一份带着
   * "稍后重试解析"承诺的导入不该被当成垃圾。`reclaimable=false` 的项照样列出，
   * 并且带着服务端给的原因（`blocked_reason`），界面的处置就是照抄那句话。
   */
  staging: (): Promise<StagingResponse> =>
    request<Schemas["StagingResponse"]>("/maintenance/staging").then(normalizeStaging),

  /**
   * 回收暂存目录中**已登记**的导入。
   *
   * 传 `objects` 是纪律（同 `cleanup`）：空列表等于"把能删的都删了吧"，服务端会以
   * `E_CONFIRM_REQUIRED` 拒掉；列出的对象必须已在可回收集合内，否则整体拒绝。
   */
  purgeStaging: (objects: readonly string[]): Promise<StagingPurgeOut> =>
    request<Schemas["StagingPurgeOut"]>("/maintenance/staging/purge", {
      method: "POST",
      json: { objects: [...objects] },
    }).then(normalizeStagingPurge),

  /** 服务级探针；不走 `/api/v1`（见 `RequestOptions.absolute`）。 */
  health: (): Promise<HealthResponse> => request("/health", { absolute: true }),
};
