/**
 * P03 的**领域模型库**页签 —— 对应需求 **FR-M1-13 / FR-M2-23**。
 *
 * 为什么它自成一个页签，而不是塞进档案详情
 * ------------------------------------
 * 领域模型与模板包、文档类型档案是**三层正交的外部数据**（需求 4.9.2）：一份领域模型
 * 可被多份档案复用。故它的生命周期不属于任何一份档案——把它挂进档案详情会让人以为
 * "改这个类型只影响眼下这份档案"。它先于档案存在：档案的内容投影 `for_each` 引用它的
 * `kind`，**登记档案时**就逐条校验（未登记即拒绝登记）。
 *
 * 「参与校验」这一列为什么由服务端给
 * ------------------------------
 * 废弃的声明仍留在库里（共 `count` 条），但不再进注册表；"有几个类型真的参与校验"
 * 是 `active_count` 由服务端算好的。界面自己按 `deprecated` 数一遍就有了第二处口径，
 * 而它漂移的表现很具体——登记档案被拒，界面却显示"库里有类型"。
 *
 * 为什么没有版本选择
 * ----------------
 * 领域模型的身份就是 `kind`，**没有版本维度**（引用它的 `for_each` 带不上版本）。
 * 所以本页既不显示版本，也不给"看历史版本"的入口——那是模板包与档案才有的维度。
 *
 * 登记为什么不给表单
 * ----------------
 * 同档案页签：声明本身就是契约，做个"表单 → JSON"的映射等于在前端再定义一遍它有哪些
 * 字段，而契约已经是唯一答案。故只给一个 JSON 文本框。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";

import { domainModelsApi } from "../api/endpoints";
import type {
  DomainModelOut,
  RegisteredDomainModelOut,
  Schemas,
} from "../api/types";
import { EmptyState, ErrorPanel, KeyValues, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";

type FieldSchema = Schemas["FieldSchema-Output"];

/* ------------------------------------------------------------------ 小件 */

/** 一串标签。空数组给显式文案，不留白——留白会让人分不清"没有"与"没显示"。 */
function TagList({
  values,
  tone,
  empty,
  mono = true,
}: {
  values: readonly string[];
  tone: string;
  empty: string;
  mono?: boolean;
}): ReactNode {
  if (values.length === 0) {
    return (
      <p className="muted small" style={{ margin: "0 0 6px" }}>
        {empty}
      </p>
    );
  }
  return (
    <p className="row row--wrap" style={{ margin: "0 0 6px" }}>
      {values.map((value) => (
        <span key={value} className={`tag ${tone}`}>
          {mono ? <span className="mono">{value}</span> : value}
        </span>
      ))}
    </p>
  );
}

function FieldTable({ model }: { model: DomainModelOut }): ReactNode {
  const entries = Object.entries(model.fields);
  if (entries.length === 0) {
    return (
      <p className="muted small" style={{ margin: "0 0 12px" }}>
        {zh.p03.dmFieldNone}
      </p>
    );
  }
  return (
    <div className="table-wrap" style={{ marginBottom: 12 }}>
      <table>
        <thead>
          <tr>
            <th>{zh.p03.dmFieldName}</th>
            <th>{zh.p03.dmFieldType}</th>
            <th>{zh.p03.dmFieldRequired}</th>
            <th>{zh.p03.dmFieldValues}</th>
            <th>{zh.p03.dmFieldOf}</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([name, schema]: [string, FieldSchema]) => (
            <tr key={name}>
              <td className="mono small">{name}</td>
              <td className="small">{schema.type}</td>
              <td className="small">
                {schema.required ? zh.p03.dmRequiredYes : zh.p03.dmRequiredNo}
              </td>
              {/* 取值域只对 enum 有意义；`of` 只对 list 有意义，其余留白而不是填 `—`。 */}
              <td className="mono small">{(schema.values ?? []).join(", ")}</td>
              <td className="mono small">{schema.of ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------ 详情面板 */

function DomainModelDetailPanel({
  item,
  onClose,
  onDeprecate,
}: {
  item: RegisteredDomainModelOut;
  onClose: () => void;
  onDeprecate: () => void;
}): ReactNode {
  const model = item.model;
  return (
    <div className="card">
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>{zh.p03.dmDetailTitle(item.kind)}</h2>
          <p className="muted small" style={{ margin: 0 }}>
            {item.title}
          </p>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={onDeprecate}>
            {zh.p03.deprecate}
          </button>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            {zh.app.close}
          </button>
        </div>
      </div>

      {item.deprecated && (
        <p className="small" style={{ color: "var(--warn)", marginTop: 0 }}>
          {zh.p03.dmDeprecatedNote(item.deprecated_reason ?? "")}
          <br />
          <span className="faint">{zh.p03.dmDeprecatedMeans}</span>
        </p>
      )}

      <KeyValues
        items={[
          { label: zh.p03.dmColKind, value: <span className="mono">{item.kind}</span> },
          { label: zh.p03.dmColFieldCount, value: String(item.field_count) },
          {
            label: zh.p03.profileColFingerprint,
            value: <span className="mono">{item.fingerprint}</span>,
          },
          { label: zh.p03.colRegisteredAt, value: formatTime(item.registered_at) },
          { label: zh.p03.colBy, value: item.registered_by ?? zh.app.none },
        ]}
      />

      <h3 style={{ marginBottom: 6 }}>{zh.p03.dmKeyFields}</h3>
      <TagList values={model.key} tone="tag--info" empty={zh.app.none} />
      <p className="faint small" style={{ marginBottom: 12 }}>
        {zh.p03.dmKeyFieldsNote}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p03.dmComparable}</h3>
      <TagList values={model.comparable} tone="tag--neutral" empty={zh.app.none} />
      <p className="faint small" style={{ marginBottom: 12 }}>
        {zh.p03.dmComparableNote}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p03.dmTraceable}</h3>
      <TagList values={model.traceable} tone="tag--neutral" empty={zh.app.none} />
      <p className="faint small" style={{ marginBottom: 12 }}>
        {zh.p03.dmTraceableNote}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p03.dmFields}</h3>
      <FieldTable model={model} />

      <h3 style={{ marginBottom: 6 }}>{zh.p03.dmDeclaration}</h3>
      {/* 声明本体整份透出：界面上能看到的每一列都来自它，不做二次加工。 */}
      <pre className="code">{JSON.stringify(model, null, 2)}</pre>
    </div>
  );
}

/* ------------------------------------------------------------------ 行 */

function DomainModelRow({
  item,
  selected,
  onSelect,
}: {
  item: RegisteredDomainModelOut;
  selected: boolean;
  onSelect: () => void;
}): ReactNode {
  return (
    <tr>
      <td>
        <span className="mono">{item.kind}</span>
        <div className="faint small">{item.title}</div>
      </td>
      <td className="small">
        {item.deprecated ? (
          <span className="tag tag--warn">{zh.p03.deprecatedTag}</span>
        ) : (
          <span className="tag tag--ok">{zh.p03.availableTag}</span>
        )}
      </td>
      <td className="small">{item.field_count}</td>
      <td className="mono small">{item.fingerprint}</td>
      <td className="small">
        {formatTime(item.registered_at)}
        {item.registered_by !== null && item.registered_by !== undefined && (
          <div className="faint small">
            {zh.generic.by}
            {item.registered_by}
          </div>
        )}
      </td>
      <td className="small">
        <button type="button" className="btn btn--sm" onClick={onSelect} aria-expanded={selected}>
          {selected ? zh.app.close : zh.p03.dmDetail}
        </button>
      </td>
    </tr>
  );
}

/* ------------------------------------------------------------ 登记表单 */

function DomainModelRegisterForm({ onDone }: { onDone: (message: string) => void }): ReactNode {
  const [raw, setRaw] = useState("");
  const [author, setAuthor] = useState("");
  const [onConflict, setOnConflict] = useState("abort");
  const [parseError, setParseError] = useState(false);
  const client = useQueryClient();

  const register = useMutation({
    mutationFn: (declaration: unknown) =>
      domainModelsApi.register({
        // 声明本体直接进请求体：契约已经是唯一答案，前端不重排字段。
        model: declaration as Schemas["DomainModel-Input"],
        author: author.trim() === "" ? null : author.trim(),
        on_conflict: onConflict,
      }),
    onSuccess: (result) => {
      setRaw("");
      setParseError(false);
      onDone(
        result.replaced
          ? zh.p03.dmRegisterReplaced(result.kind)
          : zh.p03.dmRegisterDone(result.kind),
      );
      void client.invalidateQueries({ queryKey: ["domain-models"] });
    },
  });

  function submit(): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      setParseError(true);
      return;
    }
    setParseError(false);
    register.mutate(parsed);
  }

  return (
    <div className="card">
      <h2>{zh.p03.dmRegisterTitle}</h2>
      <p className="muted small">{zh.p03.dmRegisterHint}</p>

      <label className="field" style={{ marginBottom: 12 }}>
        <span>{zh.p03.dmDeclaration}</span>
        <textarea
          rows={12}
          className="mono"
          value={raw}
          placeholder={zh.p03.dmRegisterPlaceholder}
          onChange={(event) => setRaw(event.target.value)}
        />
      </label>

      <div className="form-row" style={{ marginBottom: 12 }}>
        <label className="field">
          <span>{zh.p03.authorLabel}</span>
          <input
            value={author}
            placeholder={zh.p03.authorPlaceholder}
            onChange={(event) => setAuthor(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p03.onConflictLabel}</span>
          <select value={onConflict} onChange={(event) => setOnConflict(event.target.value)}>
            <option value="abort">{zh.p03.onConflictAbort}</option>
            <option value="overwrite">{zh.p03.onConflictOverwrite}</option>
          </select>
        </label>
      </div>

      {parseError && (
        <p className="small" style={{ color: "var(--danger)", marginTop: 0 }}>
          {zh.p03.invalidJson}
        </p>
      )}
      {register.isError && <ErrorPanel error={register.error} />}

      <button
        type="button"
        className="btn btn--primary btn--sm"
        disabled={register.isPending || raw.trim() === ""}
        onClick={submit}
      >
        {register.isPending ? zh.p03.registerSubmitting : zh.p03.registerSubmit}
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ 页签 */

export function DomainModelsTab(): ReactNode {
  const [showDeprecated, setShowDeprecated] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [deprecating, setDeprecating] = useState<RegisteredDomainModelOut | null>(null);
  const [reason, setReason] = useState("");
  const [operator, setOperator] = useState("");
  const client = useQueryClient();

  // 过滤交给**服务端**（`?include_deprecated=`）：界面自己筛会把 `count` 弄成
  // "筛出来几条"，而那个数字正是"参与校验的类型够不够"的唯一线索。
  const library = useQuery({
    queryKey: ["domain-models", showDeprecated],
    queryFn: () => domainModelsApi.list({ includeDeprecated: showDeprecated }),
  });

  const deprecate = useMutation({
    mutationFn: (item: RegisteredDomainModelOut) =>
      domainModelsApi.deprecate(item.kind, {
        reason: reason.trim(),
        operator: operator.trim() === "" ? null : operator.trim(),
      }),
    onSuccess: () => {
      setDeprecating(null);
      setReason("");
      setMessage(zh.p03.deprecateDone);
      void client.invalidateQueries({ queryKey: ["domain-models"] });
    },
  });

  const items = library.data?.items ?? [];
  const selectedItem = items.find((item) => item.kind === selected) ?? null;

  return (
    <>
      <p className="muted small" style={{ marginTop: 0 }}>
        {zh.p03.dmNote}
      </p>

      <DomainModelRegisterForm onDone={setMessage} />

      <div className="card">
        <div className="row row--wrap">
          <label className="row small" style={{ gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={showDeprecated}
              onChange={(event) => setShowDeprecated(event.target.checked)}
            />
            <span>{zh.p03.profileShowDeprecated}</span>
          </label>
          <span className="muted small">
            {library.data === undefined
              ? ""
              : `${zh.p03.dmCount(library.data.count)} · ${zh.p03.dmActiveCount(library.data.active_count)}`}
          </span>
          <button type="button" className="btn btn--sm" onClick={() => void library.refetch()}>
            {zh.app.refresh}
          </button>
        </div>

        {message !== null && (
          <p className="small" style={{ color: "var(--ok)", marginBottom: 8 }}>
            {message}
          </p>
        )}

        {library.isPending && <Loading />}
        {library.isError && <ErrorPanel error={library.error} />}

        {library.data !== undefined &&
          (items.length === 0 ? (
            <EmptyState title={zh.p03.dmEmptyTitle} body={zh.p03.dmEmptyNote} />
          ) : (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>{zh.p03.dmColKind}</th>
                    <th>{zh.p03.colStatus}</th>
                    <th>{zh.p03.dmColFieldCount}</th>
                    <th>{zh.p03.profileColFingerprint}</th>
                    <th>{zh.p03.colRegisteredAt}</th>
                    <th>{zh.p03.colActions}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <DomainModelRow
                      key={item.kind}
                      item={item}
                      selected={selected === item.kind}
                      onSelect={() => {
                        setMessage(null);
                        setSelected(selected === item.kind ? null : item.kind);
                      }}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ))}

        <p className="faint small" style={{ marginBottom: 0 }}>
          {zh.p03.dmNoVersionNote}
        </p>
      </div>

      {selectedItem !== null && (
        <DomainModelDetailPanel
          item={selectedItem}
          onClose={() => setSelected(null)}
          onDeprecate={() => {
            setMessage(null);
            setReason("");
            setDeprecating(selectedItem);
          }}
        />
      )}

      {deprecating !== null && (
        <div className="card">
          <h2>{zh.p03.deprecateTitle(deprecating.kind)}</h2>
          <p className="muted small">{zh.p03.deprecateNote}</p>
          <p className="faint small">{zh.p03.dmDeprecatedMeans}</p>
          <div className="form-row" style={{ marginBottom: 12 }}>
            <label className="field">
              <span>{zh.p03.deprecateReason}</span>
              <input
                value={reason}
                placeholder={zh.p03.deprecateReasonPlaceholder}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{zh.p03.authorLabel}</span>
              <input
                value={operator}
                placeholder={zh.p03.authorPlaceholder}
                onChange={(event) => setOperator(event.target.value)}
              />
            </label>
          </div>
          {deprecate.isError && <ErrorPanel error={deprecate.error} />}
          <div className="row" style={{ gap: 8 }}>
            {/* 理由必填是**服务端**规则（空理由 422），故这里的禁用态是那条规则的镜像，
                而不是界面自造的约束（硬规则 15）。 */}
            <button
              type="button"
              className="btn btn--sm btn--danger"
              disabled={deprecate.isPending || reason.trim() === ""}
              onClick={() => deprecate.mutate(deprecating)}
            >
              {zh.p03.deprecate}
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => {
                setDeprecating(null);
                setReason("");
              }}
            >
              {zh.app.cancel}
            </button>
          </div>
        </div>
      )}
    </>
  );
}
