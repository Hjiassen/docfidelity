/**
 * P07 的六个编辑区块（页面装配在 `P07Config.tsx`）。
 *
 * 为什么拆一个文件
 * --------------
 * 七个区域塞进一个组件会越过本目录里同类页面的体量（P04 也是这么拆的：详情页 +
 * `p04Sections.tsx`）。拆的界线是"块"：每块只拿自己那一份子结构 + 一个 `onChange`，
 * 于是"这一块改了会动到谁"在函数签名上就写明了——整份配置是**一起提交**的（PUT 的语义），
 * 一处漏带字段等于把它清空，这条风险靠"每块只改自己那一份"来兜。
 *
 * 三个"这一块可能不在"的位置
 * ------------------------
 * `terminology` / `pagination` / `routing` 在生成类型里是可选的（契约给了
 * `default_factory`，而生成器对工厂默认值无法写出 `default`），线路上实际恒有值。
 * 这里**不编一份默认值**去兜（那等于在浏览器里定义"一份空配置长什么样"），
 * 而是显式渲染一句"这一轮服务端没给这一块"——正常看不到，看到就是异常，如实说出来。
 *
 * 每个可改的控件都对应配置文件里的一行
 * --------------------------------
 * 硬规则 10 在这里的具体做法：新增行时补的 `format_version` 取**契约声明的默认值**
 * （`"1.0.0"`），而不是界面自己发明的版本；开放映射（`param_overrides`）用 JSON 编辑，
 * 因为它的值是任意对象，`键=值` 那种编法会把数字静默变成字符串。
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type {
  DecisionLevel,
  DecisionPointRoute,
  DecisionRoute,
  DecisionStrategy,
  DocumentEntry,
  PaginationPolicy,
  RoutingConfig,
  RuleSetRef,
  Terminology,
  TerminologyEntry,
  TerminologyScope,
} from "../api/types";
import { routes } from "../app/routes";
import { zh } from "../i18n/zh";

/** 契约给新增对象的 `format_version` 默认值（`docs/spec/` 里的 config 家族）。 */
export const FORMAT_VERSION = "1.0.0";

/* ------------------------------------------------------------------ 小工具 */

/** 逗号（半角/全角/顿号）分隔 ⇄ 字符串数组。空串与纯空白都算"没有"。 */
function parseList(text: string): string[] {
  return text
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

function formatList(items: readonly string[] | undefined): string {
  return (items ?? []).join("，");
}

/** `键=值` 文本 ⇄ 开放映射（`severity_overrides`：值是字符串，故这个编法不丢类型）。 */
function parsePairs(text: string): Record<string, string> {
  const pairs: Record<string, string> = {};
  for (const chunk of parseList(text)) {
    const index = chunk.indexOf("=");
    if (index <= 0) continue;
    const key = chunk.slice(0, index).trim();
    const value = chunk.slice(index + 1).trim();
    if (key !== "" && value !== "") pairs[key] = value;
  }
  return pairs;
}

function formatPairs(map: Record<string, string> | undefined): string {
  return Object.entries(map ?? {})
    .map(([key, value]) => `${key}=${value}`)
    .join("，");
}

function missingBlock(): ReactNode {
  return (
    <p className="small muted" style={{ marginBottom: 0 }}>
      {zh.p07.blockMissing}
    </p>
  );
}

function SectionCard({
  title,
  note,
  actions,
  children,
}: {
  title: string;
  note?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}): ReactNode {
  return (
    <div className="card">
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>{title}</h2>
        </div>
        {actions !== undefined && <div className="page-head__actions">{actions}</div>}
      </div>
      {note !== undefined && <p className="faint small">{note}</p>}
      {children}
    </div>
  );
}

/**
 * 把 JSON 对象收窄成 `param_overrides` 的形状（值是对象）。
 *
 * 这处断言是**有运行时校验兜着的**（同 P08 读 `where` 里 `problems` 的写法）：
 * 逐个值检查"是对象、不是数组、不是 null"，任何一项不符就返回 `null` 让调用点不写回。
 * 断言只负责把"检查过的形状"告诉类型系统，不负责跳过检查。
 */
function asParamOverrides(
  parsed: Record<string, unknown>,
): Record<string, Record<string, unknown>> | null {
  const result: Record<string, Record<string, unknown>> = {};
  for (const [key, item] of Object.entries(parsed)) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return null;
    result[key] = item as Record<string, unknown>;
  }
  return result;
}

/** JSON 对象编辑器（`param_overrides` 这类开放嵌套映射）。解析或形状不符时**不**往外传。 */
function JsonObjectField({
  value,
  onChange,
}: {
  value: Record<string, Record<string, unknown>> | undefined;
  onChange: (next: Record<string, Record<string, unknown>>) => void;
}): ReactNode {
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [bad, setBad] = useState(false);

  return (
    <>
      <textarea
        className="mono small"
        rows={4}
        value={text}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          let parsed: unknown;
          try {
            parsed = JSON.parse(next);
          } catch {
            setBad(true);
            return;
          }
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            setBad(true);
            return;
          }
          const accepted = asParamOverrides(parsed as Record<string, unknown>);
          if (accepted === null) {
            setBad(true);
            return;
          }
          setBad(false);
          onChange(accepted);
        }}
      />
      {bad && <div className="faint small">{zh.p07.jsonBad}</div>}
    </>
  );
}

/* ------------------------------------------------- 区域 1：文档族清单 */

export function DocumentsSection({
  documents,
  onChange,
}: {
  documents: readonly DocumentEntry[];
  onChange: (next: DocumentEntry[]) => void;
}): ReactNode {
  const replace = (index: number, patch: Partial<DocumentEntry>): void => {
    onChange(documents.map((item, at) => (at === index ? { ...item, ...patch } : item)));
  };
  const move = (index: number, delta: number): void => {
    const target = index + delta;
    if (target < 0 || target >= documents.length) return;
    const next = [...documents];
    const [moved] = next.splice(index, 1);
    if (moved === undefined) return;
    next.splice(target, 0, moved);
    onChange(next);
  };

  return (
    <SectionCard
      title={zh.p07.secDocuments}
      note={zh.p07.secDocumentsNote}
      actions={
        <button
          type="button"
          className="btn btn--sm"
          onClick={() =>
            onChange([
              ...documents,
              {
                doc_key: "",
                title: "",
                profile_id: "",
                template_ref: "",
                source_ids: [],
                overrides_ref: null,
                enabled: true,
                format_version: FORMAT_VERSION,
              },
            ])
          }
        >
          {zh.p07.docAdd}
        </button>
      }
    >
      {documents.length === 0 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p07.docEmpty}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{zh.p07.docKey}</th>
                <th>{zh.p07.docTitle}</th>
                <th>{zh.p07.docProfile}</th>
                <th>{zh.p07.docTemplate}</th>
                <th>{zh.p07.docSources}</th>
                <th>{zh.p07.docOverrides}</th>
                <th>{zh.p07.docEnabled}</th>
                <th>{zh.p07.colActions}</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc, index) => (
                <tr key={`${doc.doc_key}-${index}`}>
                  <td>
                    <input
                      className="mono small"
                      value={doc.doc_key}
                      onChange={(event) => replace(index, { doc_key: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      value={doc.title}
                      onChange={(event) => replace(index, { title: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={doc.profile_id}
                      placeholder="profile_id"
                      onChange={(event) => replace(index, { profile_id: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={doc.template_ref}
                      placeholder={zh.p07.docRefPlaceholder}
                      onChange={(event) => replace(index, { template_ref: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={formatList(doc.source_ids)}
                      placeholder={zh.p07.docSourcesPlaceholder}
                      onChange={(event) =>
                        replace(index, { source_ids: parseList(event.target.value) })
                      }
                    />
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={doc.overrides_ref ?? ""}
                      onChange={(event) =>
                        replace(index, {
                          overrides_ref: event.target.value === "" ? null : event.target.value,
                        })
                      }
                    />
                  </td>
                  <td className="small">
                    <label className="row" style={{ gap: 6, alignItems: "center" }}>
                      <input
                        type="checkbox"
                        style={{ width: "auto" }}
                        checked={doc.enabled}
                        onChange={(event) => replace(index, { enabled: event.target.checked })}
                      />
                    </label>
                  </td>
                  <td className="small">
                    <div className="row" style={{ gap: 4 }}>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => move(index, -1)}
                      >
                        {zh.p07.docUp}
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm"
                        onClick={() => move(index, 1)}
                      >
                        {zh.p07.docDown}
                      </button>
                      <button
                        type="button"
                        className="btn btn--sm btn--danger"
                        onClick={() => onChange(documents.filter((_, at) => at !== index))}
                      >
                        {zh.p07.docRemove}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SectionCard>
  );
}

/* --------------------------------------------- 区域 2：章节装配映射 */

export function MappingSection({ documents }: { documents: readonly DocumentEntry[] }): ReactNode {
  return (
    <SectionCard
      title={zh.p07.secMapping}
      note={
        <>
          {zh.p07.secMappingNote}{" "}
          <Link to={routes.p03Templates}>{zh.p07.secMappingGo}</Link>
        </>
      }
      actions={
        <span className="muted small">{zh.p07.secMappingOverrides}</span>
      }
    >
      {documents.length === 0 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p07.docEmpty}
        </p>
      ) : (
        <ul className="small" style={{ marginBottom: 0 }}>
          {documents.map((doc) => (
            <li key={doc.doc_key}>
              <span className="mono">{doc.doc_key}</span>：{" "}
              {doc.overrides_ref === null || doc.overrides_ref === undefined || doc.overrides_ref === ""
                ? zh.app.none
                : <span className="mono">{doc.overrides_ref}</span>}
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

/* ----------------------------------------------------- 区域 3：术语表 */

const SCOPES: readonly TerminologyScope[] = ["project", "global", "none"];

export function TerminologySection({
  terminology,
  onChange,
}: {
  terminology: Terminology | undefined;
  onChange: (next: Terminology) => void;
}): ReactNode {
  if (terminology === undefined) {
    return <SectionCard title={zh.p07.secTerminology}>{missingBlock()}</SectionCard>;
  }
  const entries = terminology.entries ?? [];
  const replace = (index: number, patch: Partial<TerminologyEntry>): void => {
    onChange({ ...terminology, entries: entries.map((item, at) => (at === index ? { ...item, ...patch } : item)) });
  };

  return (
    <SectionCard
      title={zh.p07.secTerminology}
      note={zh.p07.secTerminologyNote}
      actions={
        <>
          <label className="field" style={{ minWidth: 160 }}>
            <span>{zh.p07.termScope}</span>
            <select
              value={terminology.scope}
              onChange={(event) =>
                onChange({ ...terminology, scope: event.target.value as TerminologyScope })
              }
            >
              {SCOPES.map((scope) => (
                <option key={scope} value={scope}>
                  {zh.p07.termScopeOf[scope]}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() =>
              onChange({
                ...terminology,
                entries: [
                  ...entries,
                  { term: "", canonical: null, forbidden: [], note: null, format_version: FORMAT_VERSION },
                ],
              })
            }
          >
            {zh.p07.termAdd}
          </button>
        </>
      }
    >
      {entries.length === 0 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p07.termEmpty}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{zh.p07.termTitle}</th>
                <th>{zh.p07.termCanonical}</th>
                <th>{zh.p07.termForbidden}</th>
                <th>{zh.p07.termNote}</th>
                <th>{zh.p07.colActions}</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry, index) => (
                <tr key={`${entry.term}-${index}`}>
                  <td>
                    <input
                      className="mono small"
                      value={entry.term}
                      placeholder={zh.p07.termPlaceholder}
                      onChange={(event) => replace(index, { term: event.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={entry.canonical ?? ""}
                      onChange={(event) =>
                        replace(index, {
                          canonical: event.target.value === "" ? null : event.target.value,
                        })
                      }
                    />
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={formatList(entry.forbidden)}
                      placeholder={zh.p07.termForbiddenPlaceholder}
                      onChange={(event) =>
                        replace(index, { forbidden: parseList(event.target.value) })
                      }
                    />
                  </td>
                  <td>
                    <input
                      value={entry.note ?? ""}
                      onChange={(event) =>
                        replace(index, { note: event.target.value === "" ? null : event.target.value })
                      }
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      onClick={() =>
                        onChange({ ...terminology, entries: entries.filter((_, at) => at !== index) })
                      }
                    >
                      {zh.p07.docRemove}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SectionCard>
  );
}

/* --------------------------------------------- 区域 4：分页与版式策略 */

/** 五个布尔开关（`PaginationPolicy` 里除整数项之外的字段）。 */
type PaginationFlag =
  | "page_break_before_chapter"
  | "keep_heading_with_next"
  | "keep_table_header_row"
  | "avoid_row_split"
  | "avoid_figure_orphan";

/**
 * 改一个布尔项。
 *
 * 写成"先复制、再按键赋值"而不是 `{ ...policy, [key]: value }`：后者在 `key` 是联合类型时
 * 会被推成带字符串索引的类型，而 `PaginationPolicy` 里还有一个整数项——推断出来的索引签名
 * 会把那一项也声明成布尔。按键赋值是唯一既能保持准确类型、又不必写五个分支的写法
 * （这五个键的属性类型统一是布尔，故联合键上的写入是合法的）。
 */
function withFlag(policy: PaginationPolicy, key: PaginationFlag, value: boolean): PaginationPolicy {
  const next: PaginationPolicy = { ...policy };
  next[key] = value;
  return next;
}

export function PaginationSection({
  pagination,
  onChange,
}: {
  pagination: PaginationPolicy | undefined;
  onChange: (next: PaginationPolicy) => void;
}): ReactNode {
  if (pagination === undefined) {
    return <SectionCard title={zh.p07.secPagination}>{missingBlock()}</SectionCard>;
  }
  const flags: ReadonlyArray<{ key: PaginationFlag; label: string }> = [
    { key: "page_break_before_chapter", label: zh.p07.pageBreakBeforeChapter },
    { key: "keep_heading_with_next", label: zh.p07.keepHeadingWithNext },
    { key: "keep_table_header_row", label: zh.p07.keepTableHeaderRow },
    { key: "avoid_row_split", label: zh.p07.avoidRowSplit },
    { key: "avoid_figure_orphan", label: zh.p07.avoidFigureOrphan },
  ];

  return (
    <SectionCard
      title={zh.p07.secPagination}
      note={
        <>
          {zh.p07.secPaginationNote}{" "}
          <Link to={routes.p03Templates}>{zh.p07.paginationGo}</Link>
        </>
      }
    >
      <div className="form-row">
        {flags.map((item) => (
          <label key={item.key} className="row small" style={{ gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={pagination[item.key]}
              onChange={(event) => onChange(withFlag(pagination, item.key, event.target.checked))}
            />
            <span>{item.label}</span>
          </label>
        ))}
        <label className="field">
          <span>{zh.p07.minBodyLinesAfterHeading}</span>
          <input
            type="number"
            min={0}
            value={String(pagination.min_body_lines_after_heading)}
            onChange={(event) =>
              onChange({
                ...pagination,
                min_body_lines_after_heading: Number.parseInt(event.target.value, 10) || 0,
              })
            }
          />
        </label>
      </div>
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p07.minBodyLinesNote}
      </p>
    </SectionCard>
  );
}

/* ------------------------------------------------- 区域 5：门禁规则集 */

export function RuleSetsSection({
  ruleSets,
  onChange,
}: {
  ruleSets: readonly RuleSetRef[];
  onChange: (next: RuleSetRef[]) => void;
}): ReactNode {
  const replace = (index: number, patch: Partial<RuleSetRef>): void => {
    onChange(ruleSets.map((item, at) => (at === index ? { ...item, ...patch } : item)));
  };

  return (
    <SectionCard
      title={zh.p07.secRules}
      note={zh.p07.secRulesNote}
      actions={
        <button
          type="button"
          className="btn btn--sm"
          onClick={() =>
            onChange([
              ...ruleSets,
              {
                rule_set_id: "",
                included_rules: [],
                excluded_rules: [],
                severity_overrides: {},
                param_overrides: {},
                format_version: FORMAT_VERSION,
              },
            ])
          }
        >
          {zh.p07.ruleAdd}
        </button>
      }
    >
      {ruleSets.length === 0 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p07.ruleEmpty}
        </p>
      ) : (
        <div className="stack">
          {ruleSets.map((ruleSet, index) => (
            <div key={`${ruleSet.rule_set_id}-${index}`} className="card card--quiet">
              <div className="form-row">
                <label className="field">
                  <span>{zh.p07.ruleSetId}</span>
                  <input
                    className="mono small"
                    value={ruleSet.rule_set_id}
                    onChange={(event) => replace(index, { rule_set_id: event.target.value })}
                  />
                </label>
                <label className="field">
                  <span>{zh.p07.ruleInclude}</span>
                  <input
                    className="mono small"
                    value={formatList(ruleSet.included_rules)}
                    placeholder={zh.p07.ruleListPlaceholder}
                    onChange={(event) =>
                      replace(index, { included_rules: parseList(event.target.value) })
                    }
                  />
                </label>
                <label className="field">
                  <span>{zh.p07.ruleExclude}</span>
                  <input
                    className="mono small"
                    value={formatList(ruleSet.excluded_rules)}
                    placeholder={zh.p07.ruleListPlaceholder}
                    onChange={(event) =>
                      replace(index, { excluded_rules: parseList(event.target.value) })
                    }
                  />
                </label>
                <label className="field">
                  <span>{zh.p07.ruleSeverity}</span>
                  <input
                    className="mono small"
                    value={formatPairs(ruleSet.severity_overrides)}
                    placeholder={zh.p07.rulePairsPlaceholder}
                    onChange={(event) =>
                      replace(index, { severity_overrides: parsePairs(event.target.value) })
                    }
                  />
                </label>
              </div>
              <details style={{ marginTop: 8 }}>
                <summary className="small muted">{zh.p07.ruleParams}</summary>
                <JsonObjectField
                  value={ruleSet.param_overrides}
                  onChange={(next) => replace(index, { param_overrides: next })}
                />
              </details>
              <p className="faint small" style={{ marginBottom: 0 }}>
                {zh.p07.rulePairsNote}
              </p>
              <button
                type="button"
                className="btn btn--sm btn--danger"
                style={{ marginTop: 8 }}
                onClick={() => onChange(ruleSets.filter((_, at) => at !== index))}
              >
                {zh.p07.docRemove}
              </button>
            </div>
          ))}
        </div>
      )}
    </SectionCard>
  );
}

/* ------------------------------------------------- 区域 6：决策点路由 */

const ROUTES: readonly DecisionRoute[] = ["rule", "small_model", "optional_large_model", "human"];
const STRATEGIES: readonly DecisionStrategy[] = ["rule_only", "rule_then_model", "model_then_rule"];
const LEVELS: readonly DecisionLevel[] = ["auto", "confirm", "manual"];
/** `fallback` 在契约里是**闭集**且必填（`Literal["rule_default", "skip", "queue"]`）。 */
const FALLBACKS: readonly DecisionPointRoute["fallback"][] = ["rule_default", "skip", "queue"];

/** `null` 表示"这一项留空"，即"由内置归属与默认策略决定"。 */
function emptyAsNull(text: string): string | null {
  return text.trim() === "" ? null : text.trim();
}

/** 新增一个决策点时的骨架：契约里 `fallback` 与 `format_version` 是必填的。 */
const NEW_DECISION_POINT: DecisionPointRoute = {
  fallback: "rule_default",
  format_version: FORMAT_VERSION,
};

export function RoutingSection({
  routing,
  onChange,
}: {
  routing: RoutingConfig | undefined;
  onChange: (next: RoutingConfig) => void;
}): ReactNode {
  if (routing === undefined) {
    return <SectionCard title={zh.p07.secRouting}>{missingBlock()}</SectionCard>;
  }
  const points = Object.entries(routing.decision_points ?? {});

  const replace = (id: string, patch: Partial<DecisionPointRoute>): void => {
    const current = routing.decision_points?.[id] ?? NEW_DECISION_POINT;
    onChange({
      ...routing,
      decision_points: { ...routing.decision_points, [id]: { ...current, ...patch } },
    });
  };
  const rename = (from: string, to: string): void => {
    const entries = points.map(([key, value]) => [key === from ? to : key, value] as const);
    onChange({ ...routing, decision_points: Object.fromEntries(entries) });
  };

  return (
    <SectionCard
      title={zh.p07.secRouting}
      note={zh.p07.secRoutingNote}
      actions={
        <>
          <label className="row small" style={{ gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={routing.model_enabled}
              onChange={(event) => onChange({ ...routing, model_enabled: event.target.checked })}
            />
            <span>{zh.p07.routingModelEnabled}</span>
          </label>
        </>
      }
    >
      <p className="faint small">{zh.p07.routingModelNote}</p>

      <div className="row row--wrap" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>{zh.p07.decisionPoints}</h3>
        <button
          type="button"
          className="btn btn--sm"
          onClick={() =>
            onChange({
              ...routing,
              decision_points: { ...routing.decision_points, "": NEW_DECISION_POINT },
            })
          }
        >
          {zh.p07.decisionAdd}
        </button>
      </div>

      {points.length === 0 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p07.decisionEmpty}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{zh.p07.decisionPointId}</th>
                <th>{zh.p07.decisionRoute}</th>
                <th>{zh.p07.decisionStrategy}</th>
                <th>{zh.p07.decisionLevel}</th>
                <th>{zh.p07.decisionThreshold}</th>
                <th>{zh.p07.decisionFallback}</th>
                <th>{zh.p07.decisionModel}</th>
                <th>{zh.p07.colActions}</th>
              </tr>
            </thead>
            <tbody>
              {points.map(([id, route], index) => (
                <tr key={`${id}-${index}`}>
                  <td>
                    <input
                      className="mono small"
                      value={id}
                      onChange={(event) => rename(id, event.target.value)}
                    />
                  </td>
                  <td>
                    <select
                      value={route.route ?? ""}
                      onChange={(event) =>
                        replace(id, { route: (event.target.value || null) as DecisionRoute | null })
                      }
                    >
                      <option value="">{zh.p07.decisionInherit}</option>
                      {ROUTES.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.routeOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      value={route.strategy ?? ""}
                      onChange={(event) =>
                        replace(id, {
                          strategy: (event.target.value || null) as DecisionStrategy | null,
                        })
                      }
                    >
                      <option value="">{zh.p07.decisionInherit}</option>
                      {STRATEGIES.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.strategyOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      value={route.level ?? ""}
                      onChange={(event) =>
                        replace(id, { level: (event.target.value || null) as DecisionLevel | null })
                      }
                    >
                      <option value="">{zh.p07.decisionInherit}</option>
                      {LEVELS.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.levelOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="number"
                      step="0.05"
                      min={0}
                      max={1}
                      value={route.confidence_threshold === null || route.confidence_threshold === undefined
                        ? ""
                        : String(route.confidence_threshold)}
                      onChange={(event) =>
                        replace(id, {
                          confidence_threshold:
                            event.target.value === "" ? null : Number(event.target.value),
                        })
                      }
                    />
                  </td>
                  <td>
                    <select
                      value={route.fallback}
                      onChange={(event) =>
                        replace(id, {
                          fallback: event.target.value as DecisionPointRoute["fallback"],
                        })
                      }
                    >
                      {FALLBACKS.map((item) => (
                        <option key={item} value={item}>
                          {zh.p07.fallbackOf[item]}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      className="mono small"
                      value={route.model ?? ""}
                      onChange={(event) => replace(id, { model: emptyAsNull(event.target.value) })}
                    />
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      onClick={() =>
                        onChange({
                          ...routing,
                          decision_points: Object.fromEntries(
                            points.filter(([key]) => key !== id),
                          ),
                        })
                      }
                    >
                      {zh.p07.docRemove}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p07.thresholdNote}
      </p>
    </SectionCard>
  );
}
