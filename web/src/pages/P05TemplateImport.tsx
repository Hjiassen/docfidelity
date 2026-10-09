/**
 * P05 模板入库向导 —— 对应需求 **FR-M1-01 / FR-M1-03 / FR-M2-01~04 / FR-M2-12~15**；
 * 入口 `/templates/import`（站点地图 7.3.2）。
 *
 * 五步与三条验收口径
 * -----------------
 * 第 1~3 步是**一个请求**（`POST /templates/imports`）：上传闸门、解析、出报告一次走完，
 * 所以界面上"解析"与"入库报告"同属一次响应，没有中间的可提交状态。
 *
 * 需求 P05「状态与空态」定的三条，各自落在本页的一处**分支**上（都不是前端自己算的）：
 *
 * ① **解析失败 → 仅登记原件、稍后重试**：`register_only` / `parsed` 为假时，第 2 步
 *    如实说"解析未完成"，第 3 步没有摘要可看，**第 5 步照样可用**——库里存的会是原件。
 * ② **待确认项为 0 → 跳过第 4 步**：判据是服务端给的 `pending_count`。界面不数
 *    `findings.length - decisions.length`——那是把服务端的判据抄了一遍（NFR-22）。
 * ③ **重复登记 → 换版本号或确认覆盖，不静默覆盖**：服务端报
 *    `E_VERSION_CONFLICT` 并在 `where` 里带出旧/新哈希；本页**只在收到那个码之后**
 *    才显示"确认覆盖"这个按钮，且原生做法（换版本号）排在前面。
 *
 * 界面不预判服务端会拒绝的事（硬约束 15）
 * -------------------------------------
 * 本地只有两处拦截，且都不是业务规则：
 * ① 没选文件时"上传并解析"按钮不可点（请求根本发不出去）；
 * ② 责任人（`operator`）为空时"提交处理"不可点——服务端会回 422「字段必填」，
 *    而这里提前一句中文更好读，与 `AdjudicationForm` 同一口径。
 * **`specify` 缺取值不预判**：那条规则由服务端表述（`E_REVIEW_VALUE_REQUIRED` +
 * 补救话术），前端自己编一句就会与服务端漂移。
 *
 * 为什么导入标识要一直留着
 * ----------------------
 * 第 4、5 步都是对**同一次导入**的后续动作（`/templates/imports/{import_id}/...`）。
 * 页面不把它藏进内存状态之外的地方（不写 URL、不算派生值）：刷一下页面就得重新导入，
 * 而重新导入会得到一份新的包与新的待确认项——那等于把已经确认过的留痕丢掉。
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api/client";
import { systemApi, templatesApi } from "../api/endpoints";
import type {
  ConflictPolicy,
  Finding,
  ReviewAction,
  ReviewDecisionIn,
  TemplateImportOut,
} from "../api/types";
import { routes } from "../app/routes";
import { SeverityTag } from "../components/StatusTag";
import { ErrorPanel, KeyValues, Loading, formatBytes, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";

/** 冲突错误码（`contracts.errors.ErrorCode` 的 `VERSION_CONFLICT`）。 */
const VERSION_CONFLICT = "E_VERSION_CONFLICT";

/** 阶段名的中文表；服务端给的是内核阶段名，映射只在前端。 */
const STAGE_LABELS: Record<string, string> = { ...zh.p05.stageName };

/** 一步一条的待确认项处理草稿（`item_id` → 已填的表单）。 */
interface Draft {
  action: ReviewAction;
  value: string;
  note: string;
}

function stepOf(record: TemplateImportOut | null): number {
  if (record === null) return 1;
  if (record.registration !== null) return 5;
  return record.pending_count > 0 ? 4 : 5;
}

function StepBar({ current }: { current: number }): ReactNode {
  const steps = [zh.p05.stepUpload, zh.p05.stepParse, zh.p05.stepReport, zh.p05.stepDecide, zh.p05.stepRegister];
  return (
    <div className="steps">
      {steps.map((label, index) => {
        const number = index + 1;
        const klass =
          number === current
            ? "steps__item is-current"
            : number < current
              ? "steps__item is-done"
              : "steps__item";
        return (
          <span key={label} className={klass}>
            {number}. {label}
          </span>
        );
      })}
    </div>
  );
}

/** 判定位置：`Location` 里有什么就显示什么，缺的项不占位。 */
function describeLocation(finding: Finding): string {
  const location = finding.location;
  if (location === null || location === undefined) return "";
  const parts: string[] = [];
  if (location.part !== null && location.part !== undefined) parts.push(location.part);
  if (location.xpath !== null && location.xpath !== undefined) parts.push(location.xpath);
  if (location.chapter_path !== undefined && location.chapter_path.length > 0) {
    parts.push(location.chapter_path.join(" › "));
  }
  return parts.join(" · ");
}

/** 一行文本输入（本页有九处，写法必须一致）。 */
function Field({
  label,
  value,
  onChange,
  placeholder,
  hint,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  hint?: string;
}): ReactNode {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint !== undefined && (
        <span className="faint small" style={{ display: "block", marginTop: 4 }}>
          {hint}
        </span>
      )}
    </label>
  );
}

/* =========================================================== 第 2 步：解析 */
function StageList({ record }: { record: TemplateImportOut }): ReactNode {
  return (
    <>
      <h2>2. {zh.p05.stageTitle}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {zh.p05.stageHint}
      </p>
      <ul className="timeline">
        {record.stages.map((stage) => (
          <li key={stage.stage}>
            <span
              className={
                stage.ok
                  ? "timeline__dot timeline__dot--succeeded"
                  : "timeline__dot timeline__dot--failed"
              }
              aria-hidden="true"
              title={stage.ok ? zh.p05.stageOk : zh.p05.stageFailed}
            />
            <span className="strong">{STAGE_LABELS[stage.stage] ?? stage.stage}</span>
            <span className="faint small mono">{stage.stage}</span>
            {stage.note !== null && stage.note !== undefined && (
              <span className="muted small">{stage.note}</span>
            )}
            {stage.error !== null && (
              <div className="small" style={{ marginTop: 4 }}>
                <div>
                  <span className="mono">{stage.error.code}</span>：{stage.error.message}
                </div>
                {stage.error.remediation.length > 0 && (
                  <div className="muted small">
                    可以做什么：
                    <ul style={{ margin: "4px 0 0" }}>
                      {stage.error.remediation.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );
}

/**
 * 重解析（第 2~3 步的重跑）。
 *
 * 置灰与原因都**照抄服务端**（`can_reparse` / `reparse_blocked_reason`）。这里一句
 * 业务判断都没有：既不看 `registration` 是否为空，也不自己编"已登记者不可重试"——
 * 那两句一旦分开写，就会出现"按钮是灰的但说不出为什么"或者"按钮能点、点了报错"。
 *
 * 也不做乐观更新：重解析换掉的是整份报告（stages / findings / summary / 包哈希），
 * 本地拼一份新报告出来只会让人以为看到了结果——直接落服务端返回的那一份。
 */
function ReparseRow({
  record,
  onDone,
}: {
  record: TemplateImportOut;
  onDone: (next: TemplateImportOut) => void;
}): ReactNode {
  const reparse = useMutation({
    mutationFn: () => templatesApi.reparse(record.import_id),
    onSuccess: onDone,
  });

  return (
    <div style={{ marginTop: 14 }}>
      <div className="row row--wrap">
        <button
          type="button"
          className="btn"
          disabled={!record.can_reparse || reparse.isPending}
          onClick={() => reparse.mutate()}
        >
          {reparse.isPending ? zh.p05.reparsing : zh.p05.reparse}
        </button>
        {record.reparsed_at !== null && record.reparsed_at !== undefined && (
          <span className="muted small">
            {zh.p05.reparsedAt}：{formatTime(record.reparsed_at)}
          </span>
        )}
      </div>

      {record.reparse_blocked_reason !== null && (
        <p className="small" style={{ marginBottom: 0 }}>
          <span className="tag tag--neutral">{zh.p05.reparseBlocked}</span>{" "}
          <span className="muted">{record.reparse_blocked_reason}</span>
        </p>
      )}
      {record.can_reparse && (
        <p className="faint small" style={{ marginBottom: 0 }}>
          {zh.p05.reparseNote}
        </p>
      )}
      {reparse.isError && (
        <div style={{ marginTop: 10 }}>
          <ErrorPanel error={reparse.error} />
        </div>
      )}
    </div>
  );
}

/* ========================================================= 第 3 步：报告 */
function sourceLabel(source: string): string {
  if (source === "explicit") return zh.p05.sourceExplicit;
  if (source === "document") return zh.p05.sourceDocument;
  if (source === "filename") return zh.p05.sourceFilename;
  return zh.p05.sourceUnknown;
}

function SummaryCard({ record }: { record: TemplateImportOut }): ReactNode {
  const summary = record.summary;
  if (summary === null) return null;
  return (
    <>
      <h3>{zh.p05.summaryTitle}</h3>
      <p className="muted small" style={{ marginTop: 0 }}>
        {zh.p05.summaryHint}
      </p>
      <KeyValues
        items={[
          { label: "样式数", value: summary.style_count },
          { label: "段落样式", value: summary.paragraph_style_count },
          { label: "表格样式", value: summary.table_style_count },
          { label: "使用中的段落样式", value: summary.used_paragraph_styles },
          { label: "使用中的表格样式", value: summary.used_table_styles },
          { label: "编号级别", value: summary.numbering_levels },
          { label: "编号实例", value: summary.numbering_instances },
          { label: "标题绑定", value: summary.heading_bindings },
          { label: "章节数", value: summary.chapter_count },
          { label: "最深标题级别", value: summary.max_heading_level },
          { label: "表格原型", value: summary.table_prototypes },
          { label: "槽位", value: `${summary.slot_count}（已声明 ${summary.declared_slot_count}）` },
          {
            label: zh.p05.slotCompletion,
            value: `${Math.round(summary.slot_completion * 100)}%`,
          },
          { label: "占位符", value: summary.placeholder_count },
        ]}
      />
      <div className="row row--wrap" style={{ marginTop: 12 }}>
        <span className="muted small">{zh.p05.blockCounts}：</span>
        {Object.entries(summary.block_counts).map(([kind, count]) => (
          <span key={kind} className="tag tag--neutral">
            {kind} {count}
          </span>
        ))}
        {Object.entries(summary.placeholder_parts).map(([part, count]) => (
          <span key={part} className="tag tag--neutral">
            {zh.p05.placeholderParts} · {part} {count}
          </span>
        ))}
      </div>
    </>
  );
}

function ReportBody({ record }: { record: TemplateImportOut }): ReactNode {
  return (
    <>
      <h2>3. {zh.p05.reportTitle}</h2>
      <KeyValues
        items={[
          { label: "原件", value: record.source_filename },
          { label: "大小", value: formatBytes(record.source_size_bytes) },
          { label: "原件摘要", value: <span className="mono small">{record.source_hash}</span> },
          { label: "包哈希", value: <span className="mono small">{record.package_hash}</span> },
          {
            label: zh.p05.docNo,
            value: `${record.template_id || zh.app.none}（${sourceLabel(record.template_id_source)}）`,
          },
          {
            label: zh.p05.version,
            value: `${record.version || zh.app.none}（${sourceLabel(record.version_source)}）`,
          },
          {
            label: zh.p05.documentType,
            value: record.doc_type,
          },
          { label: zh.p05.remark, value: record.notes },
          { label: zh.p05.authorLabel, value: record.author },
          { label: zh.generic.createdAt, value: formatTime(record.created_at) },
        ]}
      />

      {record.integrity.length === 0 ? (
        <p className="muted small">{zh.p05.integrityOk}</p>
      ) : (
        <>
          <h3>{zh.p05.integrityTitle}</h3>
          <ul className="muted small">
            {record.integrity.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </>
      )}

      <SummaryCard record={record} />

      {record.findings.length > 0 && (
        <>
          <h3>
            {zh.p05.findingsTitle} <span className="muted small">({record.findings.length})</span>
          </h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>规则</th>
                  <th>{zh.p10.severity}</th>
                  <th>{zh.p10.message}</th>
                  <th>{zh.p05.decidedTitle}</th>
                </tr>
              </thead>
              <tbody>
                {record.findings.map((finding) => (
                  <tr key={finding.finding_id ?? finding.rule_id}>
                    <td className="mono">{finding.rule_id}</td>
                    <td>
                      <SeverityTag severity={finding.severity} />
                    </td>
                    <td>
                      {finding.message}
                      {finding.evidence !== null && finding.evidence !== undefined && (
                        <div className="faint small">证据：{finding.evidence}</div>
                      )}
                      {describeLocation(finding) !== "" && (
                        <div className="faint small mono">{describeLocation(finding)}</div>
                      )}
                      {finding.suggestion !== null && finding.suggestion !== undefined && (
                        <div className="muted small">建议：{finding.suggestion}</div>
                      )}
                    </td>
                    <td className="small">{decidedText(record, finding)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

/** 这一条最新的一次处理留痕（没有就说明还没办）。 */
function decidedText(record: TemplateImportOut, finding: Finding): string {
  const itemId = finding.finding_id ?? "";
  const matched = record.decisions.filter((entry) => entry.item_id === itemId);
  const latest = matched[matched.length - 1];
  if (latest === undefined) return zh.p10.waitingTag;
  const action = latest.action === "accept" ? zh.p05.actionAccept : zh.p05.actionSpecify;
  const value = latest.value === null ? "" : ` → ${latest.value}`;
  return `${action}${value} · ${latest.operator} · ${formatTime(latest.decided_at)}`;
}

/* ==================================================== 第 4 步：待确认项 */
function ReviewForm({
  record,
  onDone,
}: {
  record: TemplateImportOut;
  onDone: (next: TemplateImportOut) => void;
}): ReactNode {
  const [operator, setOperator] = useState("");
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  const submit = useMutation({
    mutationFn: (body: { operator: string; decisions: ReviewDecisionIn[] }) =>
      templatesApi.review(record.import_id, body),
    onSuccess: onDone,
  });

  const draftOf = (itemId: string): Draft =>
    drafts[itemId] ?? { action: "accept", value: "", note: "" };
  const update = (itemId: string, patch: Partial<Draft>): void => {
    setDrafts((current) => ({ ...current, [itemId]: { ...draftOf(itemId), ...patch } }));
  };

  // 只提交**这一屏列出的**那些待确认项（`pending` 由服务端给）。
  const decisions: ReviewDecisionIn[] = record.pending.flatMap((finding) => {
    const itemId = finding.finding_id;
    if (itemId === null || itemId === undefined) return [];
    const draft = draftOf(itemId);
    return [
      {
        item_id: itemId,
        action: draft.action,
        value: draft.action === "specify" ? draft.value : null,
        note: draft.note === "" ? null : draft.note,
      },
    ];
  });
  const operatorMissing = operator.trim() === "";

  return (
    <>
      <h2>4. {zh.p05.decideTitle}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {zh.p05.decideHint}
      </p>

      {record.pending.length === 0 ? (
        <p className="muted small">{zh.p05.decideSkipped}</p>
      ) : (
        <>
          <Field
            label={zh.p05.operator}
            value={operator}
            onChange={setOperator}
            placeholder={zh.p05.operatorPlaceholder}
            hint={zh.p05.operatorNote}
          />

          {record.pending.map((finding) => {
            const itemId = finding.finding_id ?? "";
            const draft = draftOf(itemId);
            return (
              <div key={itemId} className="card card--quiet" style={{ marginBottom: 12 }}>
                <div className="row row--wrap">
                  <SeverityTag severity={finding.severity} />
                  <span className="mono small">{finding.rule_id}</span>
                  <span className="strong">{finding.message}</span>
                </div>
                {finding.evidence !== null && finding.evidence !== undefined && (
                  <div className="faint small">证据：{finding.evidence}</div>
                )}
                {finding.suggestion !== null && finding.suggestion !== undefined && (
                  <div className="muted small">建议：{finding.suggestion}</div>
                )}

                <div className="row row--wrap" style={{ marginTop: 10 }}>
                  <label className="row small">
                    <input
                      type="radio"
                      name={`action-${itemId}`}
                      checked={draft.action === "accept"}
                      onChange={() => update(itemId, { action: "accept" })}
                    />
                    {zh.p05.actionAccept}
                    <span className="faint small">（{zh.p05.actionAcceptNote}）</span>
                  </label>
                  <label className="row small">
                    <input
                      type="radio"
                      name={`action-${itemId}`}
                      checked={draft.action === "specify"}
                      onChange={() => update(itemId, { action: "specify" })}
                    />
                    {zh.p05.actionSpecify}
                  </label>
                  {draft.action === "specify" && (
                    <input
                      value={draft.value}
                      onChange={(event) => update(itemId, { value: event.target.value })}
                      placeholder={zh.p05.valuePlaceholder}
                    />
                  )}
                </div>
                <input
                  style={{ marginTop: 8 }}
                  value={draft.note}
                  onChange={(event) => update(itemId, { note: event.target.value })}
                  placeholder={zh.p05.notePlaceholder}
                />
              </div>
            );
          })}

          <div className="row">
            <button
              type="button"
              className="btn btn--primary"
              disabled={submit.isPending || operatorMissing || decisions.length === 0}
              onClick={() => submit.mutate({ operator: operator.trim(), decisions })}
            >
              {submit.isPending ? zh.p05.decideSubmitting : zh.p05.decideSubmit}
            </button>
            <span className="muted small">
              {zh.p05.pendingLeft(record.pending_count)} · {zh.p05.decidedTitle}
              {record.decisions.length}
            </span>
          </div>
          {submit.isError && (
            <div style={{ marginTop: 12 }}>
              <ErrorPanel error={submit.error} />
            </div>
          )}
        </>
      )}
    </>
  );
}

/* ====================================================== 第 5 步：登记 */
function RegisterForm({
  record,
  onDone,
}: {
  record: TemplateImportOut;
  onDone: (next: TemplateImportOut) => void;
}): ReactNode {
  const [templateId, setTemplateId] = useState(record.template_id);
  const [version, setVersion] = useState(record.version);
  const [operator, setOperator] = useState(record.author ?? "");
  // 记下"这次点的是哪个按钮"：两个入口共用一次请求，而按钮上的字要能对上自己那一次
  // （覆盖是个重动作，它在看到结果之前一直说"覆盖中…"才让人放心）。
  const [policy, setPolicy] = useState<ConflictPolicy | null>(null);

  const register = useMutation({
    mutationFn: (onConflict: ConflictPolicy) =>
      templatesApi.register(record.import_id, {
        template_id: templateId.trim() === "" ? null : templateId.trim(),
        version: version.trim() === "" ? null : version.trim(),
        operator: operator.trim() === "" ? null : operator.trim(),
        on_conflict: onConflict,
      }),
    onSuccess: onDone,
  });

  const run = (onConflict: ConflictPolicy): void => {
    setPolicy(onConflict);
    register.mutate(onConflict);
  };

  // 冲突：只在**服务端明确报出来之后**才提供覆盖入口（硬约束 15）。
  const conflict = register.error instanceof ApiError && register.error.code === VERSION_CONFLICT;

  if (record.registration !== null) {
    const done = record.registration;
    return (
      <>
        <h2>5. {zh.p05.stepRegister}</h2>
        <div className="card card--quiet" style={{ marginBottom: 0 }}>
          <h3 style={{ marginTop: 0 }}>{zh.p05.registerOkTitle}</h3>
          <KeyValues
            items={[
              { label: zh.p05.templateRef, value: <span className="mono">{done.ref}</span> },
              { label: "包哈希", value: <span className="mono small">{done.package_hash}</span> },
              { label: zh.p05.versionSource, value: done.parsed ? "已解析" : "未解析（仅原件）" },
              { label: "登记人", value: done.registered_by },
              { label: zh.generic.createdAt, value: formatTime(done.registered_at) },
              { label: "覆盖了已有登记", value: done.replaced ? "是" : "否" },
            ]}
          />
          <p className="muted small">{zh.p05.registerOkNote}</p>
          <Link className="btn btn--sm" to={routes.p03Templates}>
            {zh.p05.goLibrary}
          </Link>
        </div>
      </>
    );
  }

  return (
    <>
      <h2>5. {zh.p05.stepRegister}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {zh.p05.registerHint}
      </p>

      <div className="row row--wrap">
        <Field
          label={zh.p05.finalDocNo}
          value={templateId}
          onChange={setTemplateId}
          placeholder={zh.p05.docNoPlaceholder}
        />
        <Field
          label={zh.p05.finalVersion}
          value={version}
          onChange={setVersion}
          placeholder={zh.p05.versionPlaceholder}
        />
        <Field
          label={zh.p05.operator}
          value={operator}
          onChange={setOperator}
          placeholder={zh.p05.operatorPlaceholder}
        />
      </div>

      <div className="row">
        <button
          type="button"
          className="btn btn--primary"
          disabled={register.isPending}
          onClick={() => run("abort")}
        >
          {register.isPending && policy === "abort" ? zh.p05.registering : zh.p05.register}
        </button>
      </div>

      {register.isError && (
        <div style={{ marginTop: 12 }}>
          {conflict && <h3>{zh.p05.conflictTitle}</h3>}
          <ErrorPanel error={register.error} />
          {conflict && (
            <>
              <p className="muted small">{zh.p05.conflictNote}</p>
              <button
                type="button"
                className="btn btn--danger"
                disabled={register.isPending}
                onClick={() => run("overwrite")}
              >
                {register.isPending && policy === "overwrite"
                  ? zh.p05.conflictOverwriting
                  : zh.p05.conflictOverwrite}
              </button>
            </>
          )}
        </div>
      )}
    </>
  );
}

/* ============================================================== 页面 */
export function P05TemplateImport(): ReactNode {
  const [picked, setPicked] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [docType, setDocType] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [version, setVersion] = useState("");
  const [notes, setNotes] = useState("");
  const [author, setAuthor] = useState("");
  const [record, setRecord] = useState<TemplateImportOut | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const settings = useQuery({ queryKey: ["settings"], queryFn: systemApi.settings });
  const allowed = settings.data?.uploads.allowed_suffixes ?? [];
  const maxBytes = settings.data?.uploads.max_bytes ?? null;

  const upload = useMutation({
    mutationFn: (file: File) =>
      templatesApi.import(file, { docType, templateId, version, notes, author }),
    onSuccess: (next) => setRecord(next),
  });

  const accept = (file: File | undefined): void => {
    if (file === undefined) return;
    setPicked(file);
    upload.reset();
  };

  const onDrop = (event: React.DragEvent<HTMLDivElement>): void => {
    event.preventDefault();
    setDragOver(false);
    accept(event.dataTransfer.files.item(0) ?? undefined);
  };

  const current = stepOf(record);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p05.title}</h1>
          <p>{zh.p05.subtitle}</p>
        </div>
        <Link className="btn btn--sm" to={routes.p03Templates}>
          {zh.p05.goLibrary}
        </Link>
      </div>

      <StepBar current={current} />

      {/* ---------------------------------------------------- 第 1 步 */}
      <div className="card">
        <h2>1. {zh.p05.stepUpload}</h2>

        <div
          className={dragOver ? "dropzone is-over" : "dropzone"}
          onDragOver={(event) => {
            event.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          onClick={() => inputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
          }}
        >
          <p style={{ margin: 0 }}>{zh.p05.dropHint}</p>
          {maxBytes !== null && (
            <p className="small faint" style={{ margin: "6px 0 0" }}>
              允许：{allowed.join(" / ") || zh.app.none}；上限 {formatBytes(maxBytes)}
            </p>
          )}
          <input
            ref={inputRef}
            type="file"
            accept={allowed.length > 0 ? allowed.join(",") : undefined}
            hidden
            onChange={(event) => accept(event.target.files?.item(0) ?? undefined)}
          />
        </div>

        <h3>{zh.p05.identityTitle}</h3>
        <p className="muted small" style={{ marginTop: 0 }}>
          {zh.p05.identityNote}
        </p>
        <div className="row row--wrap">
          <Field
            label={zh.p05.docNo}
            value={templateId}
            onChange={setTemplateId}
            placeholder={zh.p05.docNoPlaceholder}
          />
          <Field
            label={zh.p05.version}
            value={version}
            onChange={setVersion}
            placeholder={zh.p05.versionPlaceholder}
          />
          <Field
            label={zh.p05.documentType}
            value={docType}
            onChange={setDocType}
            placeholder={zh.p05.docTypePlaceholder}
          />
          <Field
            label={zh.p05.authorLabel}
            value={author}
            onChange={setAuthor}
            placeholder={zh.p05.authorPlaceholder}
          />
          <Field
            label={zh.p05.remark}
            value={notes}
            onChange={setNotes}
            placeholder={zh.p05.remarkPlaceholder}
          />
        </div>

        {picked !== null && (
          <div className="card card--quiet" style={{ marginTop: 14, marginBottom: 0 }}>
            <div className="row">
              <span className="strong">{picked.name}</span>
              <span className="muted small">{formatBytes(picked.size)}</span>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <button
                type="button"
                className="btn btn--primary"
                disabled={upload.isPending}
                onClick={() => upload.mutate(picked)}
              >
                {upload.isPending ? zh.p05.uploading : zh.p05.upload}
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setPicked(null);
                  upload.reset();
                }}
              >
                {zh.app.cancel}
              </button>
            </div>
          </div>
        )}

        {upload.isError && (
          <div style={{ marginTop: 14 }}>
            <ErrorPanel error={upload.error} />
          </div>
        )}
      </div>

      {/* --------------------------------------- 第 2~5 步（一次响应之后） */}
      {record !== null && (
        <>
          {!record.parsed && (
            <div className="card card--quiet">
              <h3 style={{ marginTop: 0 }}>{zh.p05.parseFailedTitle}</h3>
              <p className="muted" style={{ marginBottom: 0 }}>
                {zh.p05.parseFailedNote}
              </p>
              <p className="faint small" style={{ marginBottom: 0 }}>
                {zh.p05.retryHint}
              </p>
            </div>
          )}

          <div className="card">
            <StageList record={record} />
            <ReparseRow record={record} onDone={setRecord} />
          </div>

          {record.parsed && (
            <div className="card">
              <ReportBody record={record} />
            </div>
          )}

          {record.pending_count > 0 && (
            <div className="card">
              <ReviewForm record={record} onDone={setRecord} />
            </div>
          )}

          <div className="card">
            <RegisterForm record={record} onDone={setRecord} />
          </div>
        </>
      )}

      {settings.isPending && <Loading />}
    </>
  );
}
