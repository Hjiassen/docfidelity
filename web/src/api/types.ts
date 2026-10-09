/**
 * 接口类型层。
 *
 * 契约类型**全部从 OpenAPI 生成的 `schema.d.ts` 派生**——不手写重复类型（DD-14.1）。
 * 手写一份 `interface Job` 的代价是：服务端加了字段、前端类型仍是旧形状，
 * 而 TypeScript 无法告诉你"你漏了一个字段"。
 *
 * 唯一的例外见文件末尾的 `JobEvent`（SSE 载荷不在 OpenAPI 的描述范围里）。
 */

import type { components } from "./schema";

/** OpenAPI 规格里的全部 schema。需要新类型时从这里取，不要另起炉灶。 */
export type Schemas = components["schemas"];

/**
 * 把"契约里一定会有"的数组字段收窄为必存在。
 *
 * 为什么需要：Pydantic 给带 `default_factory=list` 的字段生成的 JSON Schema 是
 * **非必填**的（因为 Python 侧有默认值），于是生成类型写成 `findings?: Finding[]`。
 * 但服务端序列化的是**真实模型实例**——空的时候给的是 `[]`，不是省略字段。
 * 也就是说"缺省"这一情形只存在于类型系统里，不存在于线路上。
 *
 * 收窄只改"必存在性"，不改元素类型、不重命名字段：越界的归一化会让类型与线路脱节，
 * 那正是这套生成类型要避免的事。运行时那一半见 `normalize.ts`。
 */
type WithRequiredArrays<T, K extends keyof T> = Omit<T, K> & {
  [P in K]-?: NonNullable<T[P]>;
};

export type Adjudication = Schemas["Adjudication"];
export type AdjudicationRequest = Schemas["AdjudicationRequest"];
export type AdjudicationResponse = Schemas["AdjudicationResponse"];
export type Artifact = Schemas["Artifact"];
/**
 * 基线评测的**加载状态**（NFR-23）。
 *
 * `by_point` / `failures` / `uncovered` 在契约里带默认值 ⇒ 生成类型写成可选；但服务端
 * 序列化的是**真实实例**，空的时候给的是 `{}` / `[]`（与 `WithRequiredArrays` 同一个坑）。
 * 这里逐字段收窄"必存在性"，不改元素类型也不重命名。
 */
export type BaselineStatus = Omit<
  Schemas["BaselineStatus"],
  "by_point" | "failures" | "uncovered"
> & {
  by_point: Record<string, number>;
  failures: string[];
  uncovered: string[];
};
export type CheckpointRef = Schemas["CheckpointRef"];
export type ChildProgress = Schemas["ChildProgress"];
export type CleanupPlanOut = WithRequiredArrays<
  Schemas["CleanupPlanOut"],
  "uploads" | "workspace" | "artifacts"
>;
export type CleanupRequest = Schemas["CleanupRequest"];
export type ConfigPutResponse = Schemas["ConfigPutResponse"];
export type ConfigVersionOut = Schemas["ConfigVersionOut"];
export type DataBoundary = Schemas["DataBoundary"];
export type DecisionPointRoute = Schemas["DecisionPointRoute"];
export type DocumentEntry = Schemas["DocumentEntry"];
export type EngineErrorInfo = Schemas["EngineErrorInfo"];
export type ExpiredResponse = WithRequiredArrays<Schemas["ExpiredResponse"], "expired">;
export type Finding = Schemas["Finding"];
export type GrammarCapability = Schemas["GrammarCapability"];
export type InboxItemOut = Schemas["InboxItemOut"];
export type InboxResponse = WithRequiredArrays<Schemas["InboxResponse"], "items">;
export type Job = WithRequiredArrays<Schemas["Job"], "child_job_ids" | "artifact_ids">;
/**
 * 注意：`JobDetail.children` / `JobListResponse.items` 的元素也必须用**收窄后**的 `Job`，
 * 所以这两处不能用 `WithRequiredArrays`（它只会把"数组本身"变必填，元素仍是原始可选形状）。
 */
export type JobDetail = Omit<Schemas["JobDetail"], "job" | "stages" | "children" | "resume_plan"> & {
  job: Job;
  stages: Stage[];
  children: Job[];
  resume_plan: NonNullable<Schemas["JobDetail"]["resume_plan"]>;
};
export type JobListResponse = Omit<Schemas["JobListResponse"], "items"> & { items: Job[] };
export type Location = Schemas["Location"];
/**
 * 模型侧**能力现状**（`GET /settings/model/capabilities`）：受限解码 + 基线评测。
 *
 * 与 `ModelHealth`（"连得上吗"）分开：这个回答"**接上之后会怎么跑**"。`baseline` 用收窄后的
 * `BaselineStatus`，故卡片里可以无条件地 `.map` 而不用再判一次 undefined。
 */
export type ModelCapabilities = Omit<Schemas["ModelCapabilities"], "baseline"> & {
  baseline: BaselineStatus;
};
export type ModelHealth = Schemas["ModelHealth"];
export type ModelSettingsOut = Schemas["ModelSettingsOut"];
export type PrecheckIssueOut = WithRequiredArrays<Schemas["PrecheckIssueOut"], "remediation">;
/**
 * 与 `JobDetail` 同一个坑：`issues` **元素**也必须用收窄后的 `PrecheckIssueOut`
 * （`WithRequiredArrays` 只收窄"数组本身"，元素仍是 `remediation?`），故这里逐字段写开。
 */
export type PrecheckReportOut = Omit<
  Schemas["PrecheckReportOut"],
  "issues" | "doc_keys" | "skipped" | "missing_stages" | "registered_stages"
> & {
  issues: PrecheckIssueOut[];
  doc_keys: string[];
  skipped: string[];
  missing_stages: string[];
  registered_stages: string[];
};
export type ProjectConfig = Schemas["ProjectConfig"];
export type ProjectCreate = Schemas["ProjectCreate"];
export type ProjectOut = Schemas["ProjectOut"];
export type ProjectPatch = Schemas["ProjectPatch"];
export type Report = WithRequiredArrays<
  Schemas["Report"],
  "findings" | "waivers" | "adjudications" | "skipped"
>;
export type ResumeRequest = Schemas["ResumeRequest"];
export type RetainedObjectOut = Schemas["RetainedObjectOut"];
export type RetentionKind = RetainedObjectOut["kind"];
export type RetentionPolicy = Schemas["RetentionPolicy"];
export type SettingsPatch = Schemas["SettingsPatch"];
export type SettingsView = Schemas["SettingsView"];
export type Stage = Schemas["Stage"];
export type StagingObject = Schemas["StagingObjectOut"];
export type StagingPurgeOut = WithRequiredArrays<Schemas["StagingPurgeOut"], "reclaimed">;
export type StagingResponse = WithRequiredArrays<Schemas["StagingResponse"], "imports">;
export type SubmitBuild = Schemas["SubmitBuild"];

/* ------------------------------------------------------- 模板入库（P05 / P03） */

export type ContractSummaryOut = Omit<
  Schemas["ContractSummaryOut"],
  "block_counts" | "placeholder_parts"
> & {
  /** 两个分布表在契约里是"可有默认值"的字典，线路上的值是 `{}` 而不是缺字段。 */
  block_counts: Record<string, number>;
  placeholder_parts: Record<string, number>;
};
export type RegisterRequest = Schemas["RegisterRequest"];
export type RegisteredTemplateOut = Schemas["RegisteredTemplateOut"];
export type ReviewDecisionIn = Schemas["ReviewDecisionIn"];
export type ReviewOverride = Schemas["ReviewOverride"];
export type ReviewRequest = Schemas["ReviewRequest"];

/** 一条待确认项的处理动作是**闭集**（契约 `ReviewAction`）。 */
export type ReviewAction = ReviewDecisionIn["action"];
/** 冲突处置策略是**闭集**（`server.templates.CONFLICT_POLICIES`）。 */
export type ConflictPolicy = RegisterRequest["on_conflict"];

/**
 * 可持久化的错误快照。
 *
 * 与 `EngineErrorInfo` 的差别只有一处：`remediation` 收窄为必存在数组。界面上凡是
 * 展示错误的地方都要给"可以做什么"（错误三段式的第三段），在边界补一次比每处 `?? []`
 * 更能说明"这一项契约保证会给"。
 */
export type EngineErrorSnapshot = Omit<EngineErrorInfo, "remediation"> & {
  remediation: string[];
};

/** 入库阶段。`error` 与其它可空字段一样收窄成必存在（`null` 表示这一步没有出错）。 */
export type ImportStageOut = Omit<Schemas["ImportStageOut"], "error"> & {
  error: EngineErrorSnapshot | null;
};

/**
 * 一次导入的完整视图（P05 第 2~5 步**唯一**的响应形状）。
 *
 * 这处逐字段写开是必需的：`findings` / `pending` / `stages` / `decisions` 的**元素**
 * 也要用收窄后的类型（`WithRequiredArrays` 只把"数组本身"变必填），
 * 与 `PrecheckReportOut` 同一个坑。
 *
 * `pending` 与 `pending_count` 都由服务端给：界面不数"还剩几项"，
 * "待确认项为 0 → 跳过第 4 步"这个分支直接读 `pending_count`（NFR-22）。
 */
export type TemplateImportOut = Omit<
  Schemas["TemplateImportOut"],
  | "template_id"
  | "version"
  | "template_id_source"
  | "version_source"
  | "version_source_explicit"
  | "register_only"
  | "parsed"
  | "package_hash"
  | "source_hash"
  | "doc_type"
  | "notes"
  | "author"
  | "stages"
  | "findings"
  | "pending"
  | "pending_count"
  | "decisions"
  | "integrity"
  | "summary"
  | "registration"
  | "can_reparse"
  | "reparse_blocked_reason"
> & {
  template_id: string;
  version: string;
  template_id_source: string;
  version_source: string;
  version_source_explicit: boolean;
  register_only: boolean;
  parsed: boolean;
  package_hash: string | null;
  source_hash: string | null;
  doc_type: string | null;
  notes: string | null;
  author: string | null;
  stages: ImportStageOut[];
  findings: Finding[];
  pending: Finding[];
  pending_count: number;
  decisions: ReviewOverride[];
  integrity: string[];
  summary: ContractSummaryOut | null;
  registration: Schemas["TemplateRegistrationOut"] | null;
  /** 能不能重解析（**服务端判据**）；不能时 `reparse_blocked_reason` 是它的原话。 */
  can_reparse: boolean;
  reparse_blocked_reason: string | null;
};

export type TemplateLibraryResponse = WithRequiredArrays<
  Schemas["TemplateLibraryResponse"],
  "items"
>;

/**
 * 模板包详情（P04 的八个页签**全部**取自这里）。
 *
 * 五处逐个收窄，理由与 `TemplateImportOut` 相同：`versions` / `integrity` /
 * `fingerprint_problems` 的**元素**也要用收窄后的类型，而 `WithRequiredArrays`
 * 只把"数组本身"变必填。`package` / `summary` 为 `null` 表示这次登记**只有原件**
 * （解析没成功）——此时两个问题清单是空数组，界面必须按"未解析"渲染，
 * **不能**把空数组画成一张全绿的检查表。
 */
export type TemplateDetailOut = Omit<
  Schemas["TemplateDetailOut"],
  | "parsed"
  | "package"
  | "summary"
  | "versions"
  | "integrity"
  | "fingerprint_problems"
> & {
  parsed: boolean;
  package: Schemas["TemplatePackage"] | null;
  summary: ContractSummaryOut | null;
  versions: RegisteredTemplateOut[];
  integrity: string[];
  fingerprint_problems: string[];
};

/** 一条字段级变更（`description` 由内核生成，界面直接显示）。 */
export type FieldDeltaOut = Schemas["FieldDeltaOut"];

/** 一个分区（样式 / 页面设置 / 骨架 / …）的变更汇总。 */
export type SectionChangeOut = Omit<Schemas["SectionChangeOut"], "changes"> & {
  changes: FieldDeltaOut[];
};

/** 两个模板版本之间的差异（P04 的「版本对比」）。分区与统计都由服务端算好。 */
export type TemplateDiffOut = Omit<
  Schemas["TemplateDiffOut"],
  "changelog" | "sections" | "blocking_sections" | "changed_sections"
> & {
  changelog: string[];
  sections: SectionChangeOut[];
  blocking_sections: string[];
  changed_sections: string[];
};

export type TemplateDeprecateRequest = Schemas["TemplateDeprecateRequest"];

/* ------------------------------------------------ 领域模型库（P03，先于档案） */

/**
 * 领域模型**声明**本体。
 *
 * 生成器同样给了 `-Input` / `-Output` 两份（请求体 vs 响应），**不合并**（同档案的理由：
 * 合成一个别名会在某一侧悄悄放宽要求）。`fields` / `comparable` / `traceable` 在
 * schema 里是"可有默认值"的，但服务端序列化的是真模型实例，故在这里断言一次
 * （`normalizeDomainModel`）。
 */
export type DomainModelOut = Omit<
  WithRequiredArrays<Schemas["DomainModel-Output"], "comparable" | "traceable">,
  "fields"
> & { fields: Record<string, Schemas["FieldSchema-Output"]> };
export type DomainModelIn = Schemas["DomainModel-Input"];

/**
 * 领域模型库里的一条登记。
 *
 * **没有 `ref` / `version`**——这是与模板包 / 档案的**对立断言**：那两者的身份是
 * `id@version`，领域模型的身份就是 `kind`。引用它的 `for_each` 带不上版本，故库里
 * 不该存在多版；"改声明看不到历史"是有意的取舍（它是**词汇表**，不是产物）。
 */
export type RegisteredDomainModelOut = Omit<
  Schemas["RegisteredDomainModelOut"],
  "model"
> & { model: DomainModelOut };

/**
 * 领域模型库列表。
 *
 * `active_count` 由服务端给而不是界面按 `deprecated` 自己数：它回答的是"**有几个类型
 * 参与档案登记校验**"，而"哪些算数"的判据在服务端（`Services.domain_declarations`）。
 * 界面自己数就有第二处口径，漂移的表现很具体——登记档案被拒，界面却显示"库里有类型"。
 */
export type DomainModelLibraryResponse = Omit<
  WithRequiredArrays<Schemas["DomainModelLibraryResponse"], "items">,
  "items"
> & { items: RegisteredDomainModelOut[] };

/** 详情就是一条登记（`GET /domain-models/{kind}`），没有"版本清单"这一层。 */
export type DomainModelDetailOut = RegisteredDomainModelOut;

/** 登记回执。`replaced=True` 表示这次**覆盖**了库里那份声明（内容不同）。 */
export type DomainModelRegistrationOut = Schemas["DomainModelRegistrationOut"];

/** 登记请求：`model` 直接是那份契约，**没有 `version` 字段**（身份就是 `kind`）。 */
export type DomainModelRegisterRequest = Schemas["DomainModelRegisterRequest"];
export type DomainModelDeprecateRequest = Schemas["DomainModelDeprecateRequest"];

/* --------------------------------------------------------- 文档类型档案（P03） */

/** 档案库里的一条登记。`ref` = `profile_id@version`，正是配置里的引用形式。 */
export type RegisteredProfileOut = Schemas["RegisteredProfileOut"];
export type ProfileLibraryResponse = WithRequiredArrays<
  Schemas["ProfileLibraryResponse"],
  "items"
>;

/**
 * 档案本体。
 *
 * 生成器给了 `-Input` / `-Output` 两份：档案既出现在请求体里（登记）又出现在响应里
 * （详情），两侧的序列化形状不同。**不把两者合成一个别名**——那会在某一侧悄悄
 * 放宽要求，而"档案里有哪些字段"必须只有一个答案。
 */
export type DocTypeProfileOut = Schemas["DocTypeProfile-Output"];
export type DocTypeProfileIn = Schemas["DocTypeProfile-Input"];

/** 档案详情：登记 + 本体 + 全部版本。 */
export type ProfileDetailOut = Omit<Schemas["ProfileDetailOut"], "versions"> & {
  versions: RegisteredProfileOut[];
};

/** 登记回执。`skipped` 列出本次**做不了**的校验，界面必须显示（空才是"全查过了"）。 */
export type ProfileRegistrationOut = WithRequiredArrays<
  Schemas["ProfileRegistrationOut"],
  "skipped" | "findings"
>;

export type ProfileRegisterRequest = Schemas["ProfileRegisterRequest"];
export type ProfileDeprecateRequest = Schemas["ProfileDeprecateRequest"];

/**
 * 导入档案包的回执（验收 45）。
 *
 * `templates` / `unused_templates` 在契约里带默认值 ⇒ 生成类型写成可选，但服务端给的是
 * 真数组（与 `ProfileRegistrationOut` 同一个坑）。
 *
 * **两件事必须分开显示**（`templates` 与 `profile`）：它们可以一件成一件不成——随包带来的
 * 模板登记成功、而档案本体因版本冲突被拒时，库里确实多了模板包。合成一句「导入成功」
 * 会让那次部分成功消失得无影无踪。
 *
 * `profile` 换成本文件那个收窄过的 `ProfileRegistrationOut`（`skipped` / `findings`
 * 必存在），从而与 `POST /profiles` 的回执共用同一套字段保证——导入回执里的档案登记
 * 走的就是**同一条**登记路径，两者的形状不该有两份说法。
 */
export type ProfileImportOut = Omit<
  WithRequiredArrays<Schemas["ProfileImportOut"], "templates" | "unused_templates">,
  "profile"
> & { profile: ProfileRegistrationOut };

/* ------------------------------------------------------------- 事实源（P06） */

/** 一条来源声明。`last_probe_ok` 的 `null`（从没探过）与 `false`（探了没通）是两件事。 */
export type SourceOut = WithRequiredArrays<Schemas["SourceOut"], "bindings">;
export type SourceListResponse = Omit<
  WithRequiredArrays<Schemas["SourceListResponse"], "items" | "known_types">,
  "items"
> & { items: SourceOut[] };
export type SourceCreateRequest = Schemas["SourceCreateRequest"];
export type SourceCreateResponse = Schemas["SourceCreateResponse"];
export type SourceProbeOut = WithRequiredArrays<Schemas["SourceProbeOut"], "warnings">;
/** `kinds` 与三个数组一样带 `default_factory`，线路上是 `{}` 而不是缺字段。 */
export type SourceItemsOut = WithRequiredArrays<
  Schemas["SourceItemsOut"],
  "kinds" | "facts" | "assets" | "warnings"
>;

/* ------------------------------------------- 项目状态（P02 / P11 / P14） */

/**
 * 指标卡的五个数。
 *
 * 这里**不需要收窄**：五个字段在契约里都是 `int = 0`，而生成器对"带 `default` 的字段"
 * 一律按必存在处理（服务端序列化真实实例时它一定在）。需要收窄的是 `default_factory`
 * 那类数组——它们没有 `default` 可写，于是生成成可选（见文件头 `WithRequiredArrays`）。
 */
export type ProjectMetricsOut = Schemas["ProjectMetricsOut"];

/**
 * 三步引导的一步。
 *
 * `hint` **不做收窄**：`null`（已完成，无需接下来做什么）与有值（还没做完）是
 * 服务端给的两种事实，兜成空串会让"已完成"那一步多出一段空引导语。
 */
export type OverviewStepOut = Schemas["OverviewStepOut"];

/** 最近一次作业。`progress` 只有编排作业才有（子作业单跑时为 `null`），故收窄成"必存在但可为 `null`"。 */
export type LatestJobOut = Omit<Schemas["LatestJobOut"], "progress"> & {
  progress: ChildProgress | null;
};

/**
 * P02 的一屏数据。
 *
 * 逐字段写开的理由与 `PrecheckReportOut` 相同：`pending` / `steps` 的**元素**以及
 * `metrics` / `last_job` 都要用收窄后的类型，而 `WithRequiredArrays` 只把"数组本身"
 * 变必填。`precheck` 透传 `null`（服务端没给结论 ≠ 结论是通过）。
 */
export type ProjectOverviewOut = Omit<
  Schemas["ProjectOverviewOut"],
  "metrics" | "steps" | "pending" | "pending_total" | "last_job" | "has_config" | "precheck"
> & {
  metrics: ProjectMetricsOut;
  steps: OverviewStepOut[];
  pending: InboxItemOut[];
  pending_total: number;
  last_job: LatestJobOut | null;
  has_config: boolean;
  precheck: PrecheckReportOut | null;
};

/**
 * 一层产物分组（编排作业 → 子作业 → 文档）。
 *
 * **递归类型**，故元素也必须用收窄后的自己：`children` 里若能混进原始可选形状，
 * 界面在第二层就得重新写一遍 `?? []`——那正是这一层要收掉的东西。
 *
 * `report_verdict` 收窄成"必存在但可为 `null`"：`null` = **这份作业没有门禁报告**
 * （"未经门禁，仅作草稿"），它是服务端给的事实，不是可以省略的字段。
 */
export type ArtifactGroupOut = Omit<
  Schemas["ArtifactGroupOut"],
  "artifacts" | "children" | "report_verdict"
> & {
  artifacts: Artifact[];
  children: ArtifactGroupOut[];
  report_verdict: Verdict | null;
};

/** 项目产物列表。`count` / `total_bytes` 是**服务端算的**，界面不做求和。 */
export type ProjectArtifactsOut = Omit<
  Schemas["ProjectArtifactsOut"],
  "groups" | "count" | "total_bytes"
> & {
  groups: ArtifactGroupOut[];
  count: number;
  total_bytes: number;
};

/**
 * 两份产出的差异。
 *
 * 全是带默认值的标量/数组，故 `Required` 一次到位即可（`normalize.ts` 把缺省补齐）。
 * `equal` 是**服务端结论**：界面直接照抄，不按三个数组是否为空再推一遍"两份一样吗"。
 */
export type DocumentDiffOut = Required<Schemas["DocumentDiffOut"]>;

/** 比对请求：`mode` 是闭集（`qa.compare.COMPARE_MODES`），写成联合类型让下拉框与 `switch` 都能穷尽检查。 */
export type ArtifactCompareRequest = Schemas["ArtifactCompareRequest"];

export type ArtifactCompareOut = Omit<Schemas["ArtifactCompareOut"], "diff"> & {
  diff: DocumentDiffOut;
};

/**
 * 一条漂移项。
 *
 * `last_job_id` / `last_generated_at` 收窄成"必存在但可为 `null`"：`never_built`
 * 那条**本来就没有**上一次产出，`null` 是它的正常形态而不是缺字段。
 */
export type DriftItemOut = Omit<Schemas["DriftItemOut"], "last_job_id" | "last_generated_at"> & {
  last_job_id: string | null;
  last_generated_at: string | null;
};

/**
 * 漂移清单。
 *
 * `fact_level_available` 与 `fact_level_note` **成对使用**：前者为 `false` 时界面
 * 必须把说明显示出来——把"查不了"渲染成"没有漂移"，使用者会以为文档是最新的。
 */
export type DriftReportOut = Omit<
  Schemas["DriftReportOut"],
  "items" | "checked_documents" | "fact_level_available" | "fact_level_note"
> & {
  items: DriftItemOut[];
  checked_documents: number;
  fact_level_available: boolean;
  fact_level_note: string;
};

/** 一条"某档案版本引用了这个模板包"的记录。`ref` = `profile_id@version`，由服务端拼好。 */
export type TemplateReferenceOut = Required<Schemas["TemplateReferenceOut"]>;

/**
 * 反向引用清单。
 *
 * `count` / `active_count` 由服务端算：界面要回答的是"还能不能废弃这一版"，
 * 而那条判断是"有没有**未废弃**的档案还在引用它"，不是数量本身。
 */
export type TemplateReferencesOut = Omit<
  Schemas["TemplateReferencesOut"],
  "items" | "count" | "active_count"
> & {
  items: TemplateReferenceOut[];
  count: number;
  active_count: number;
};

export type UploadLimitsOut = Schemas["UploadLimitsOut"];
export type UploadOut = Schemas["UploadOut"];
export type Waiver = Schemas["Waiver"];
export type WorkerSettingsOut = Schemas["WorkerSettingsOut"];

/* ------------------------------------------------------- 配置的子结构（P07） */

/**
 * 配置里的六块子结构。
 *
 * 这五处**不做收窄**：它们的数组字段都有 `default_factory`（生成成可选），但界面在
 * P07 里逐块编辑，每块自己认自己的空值更直白（"没引用规则集"与"字段缺失"在这一页
 * 是同一件事——都是空表）。`project_id` / `version` 之外的字段一份都不能少，
 * 因为保存是**整份提交**（PUT 的语义），漏带一个字段就等于把它清空。
 */
export type Terminology = Schemas["Terminology"];
export type TerminologyEntry = Schemas["TerminologyEntry"];
export type PaginationPolicy = Schemas["PaginationPolicy"];
export type RoutingConfig = Schemas["RoutingConfig"];
export type RuleSetRef = Schemas["RuleSetRef"];

/** 术语的作用范围是闭集（契约 `Terminology.scope`）。 */
export type TerminologyScope = NonNullable<Terminology["scope"]>;
/** 决策点的归属 / 策略 / 等级都是闭集（契约 `DecisionPointRoute`）——写成联合类型让下拉框与 `switch` 都能穷尽检查。 */
export type DecisionRoute = NonNullable<DecisionPointRoute["route"]>;
export type DecisionStrategy = NonNullable<DecisionPointRoute["strategy"]>;
export type DecisionLevel = NonNullable<DecisionPointRoute["level"]>;

/* ------------------------------------------------------------------ 联合类型
 * 单独取出来是为了让 `switch` 能穷尽检查：漏一个状态时 TS 会报错，
 * 而不是让界面在运行时落进某个"默认分支"。
 */

export type JobStatus = Job["status"];
export type JobKind = Job["kind"];
export type StageName = Stage["name"];
export type StageStatus = Stage["status"];
export type Severity = Finding["severity"];
export type FindingStatus = Finding["status"];
export type Verdict = Report["verdict"];
export type ArtifactKind = Artifact["kind"];
/** 比对分辨率是**闭集**（契约 `ArtifactCompareRequest.mode`，与 `qa.compare.COMPARE_MODES` 同源）。 */
export type CompareMode = ArtifactCompareRequest["mode"];
/** 裁决动作是**闭集**（契约 `AdjudicationAction`）：写成联合类型让 `switch` 能穷尽检查。 */
export type AdjudicationAction = Adjudication["action"];
/** 待裁决项的类别（`server.adjudication.AdjudicationKind`）。 */
export type AdjudicationKind = InboxItemOut["kind"];

/* ------------------------------------------------------------------ 事件
 * OpenAPI **描述不了** SSE 帧的载荷（它只描述请求/响应体），所以这个类型只能手写。
 * 它与 `docfidelity.contracts.job.JobEvent` 一一对应；契约改字段时这里要同步——
 * 这是本目录里唯一一处"手写重复类型"，故在此显式标注来源。
 */

export interface JobEvent {
  format_version: string;
  event_id: number;
  job_id: string;
  type: "job" | "stage" | "decision" | "artifact" | "log";
  payload: Record<string, unknown>;
  created_at: string;
}

export type EventType = JobEvent["type"];

/** 服务端的心跳帧名（`server/sse.py` 的 `HEARTBEAT_FRAME`）。 */
export const HEARTBEAT_FRAME = "heartbeat";

/** 服务端完成历史补推后的结束帧（`server/sse.py` 的 `STREAM_END_FRAME`）。 */
export const STREAM_END_FRAME = "stream_end";

/**
 * 作业的**终态**。
 *
 * 与 `server/sse.py` 的 `SETTLED_STATUSES` 一致，用于状态呈现和停止轮询。
 * SSE 连接由 `stream_end` 关闭，避免重试历史中的旧终态提前中断补推。
 */
export const SETTLED_STATUSES: readonly JobStatus[] = [
  "succeeded",
  "failed",
  "canceled",
  "partial_failure",
];

export function isSettled(status: JobStatus | undefined | null): boolean {
  return status !== undefined && status !== null && SETTLED_STATUSES.includes(status);
}

/**
 * "服务端没给这个值"。
 *
 * Pydantic 的 `X | None = None` 字段在生成类型里是 `x?: X | null`——**两种空值都会出现**：
 * 字段可选（`undefined`）与显式为 `null` 在语义上是同一件事（"本次没有"）。
 * 于是判空必须同时覆盖两者，否则 `stage.error !== null` 在 `error` 为 `undefined` 时会
 * **误判为"有错误"**，然后在下一行 `.message` 上崩掉。
 *
 * 写成**类型谓词**（而不是返回 `boolean`）：调用点 `!isAbsent(x)` 之后要能直接访问
 * `x.message`，否则每处都得再写一次断言。
 */
export function isAbsent(value: unknown): value is null | undefined {
  return value === null || value === undefined;
}

/** 阶段顺序（与契约的 `STAGE_NAMES` 一致）。 */
export const STAGE_ORDER: readonly StageName[] = ["ingest", "compose", "render", "refresh", "qa"];

export const EVENT_TYPES: readonly EventType[] = ["job", "stage", "decision", "artifact", "log"];
