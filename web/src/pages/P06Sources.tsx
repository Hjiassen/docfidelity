/**
 * P06 事实源 —— 对应需求 **FR-M3-01~04 / DD-5.1**；入口 `/projects/:projectId/sources`（站点地图 7.3.2）。
 *
 * 来源是一条**配置**，磁盘上没有本体
 * -------------------------------
 * 与模板包 / 档案不同，来源本身不落文件：它是一行配置（`SourceConfig`）外加一份
 * 「上次探测结果」的留痕。真正的内容在它指向的 `root` 里，由接入器现读。
 * 故本页的增删都只动登记，删掉一条来源不影响历史产出——事实包与追溯边里记的是
 * **来源标识**（一个字符串），不是这条配置。
 *
 * 「试接入」与「条目」为什么是两个按钮
 * -------------------------------
 * 服务端把它们分成两个端点，是因为它们回答不同的问题、失败方式也不同：
 * ① **试接入**（`/probe`）**永不抛错**——`ok=false` 是一次正常回答（HTTP 200），
 *    意思是"现在连不通"，原因写在 `detail` 里。它服务于"这条配置对不对"。
 * ② **条目**（`/items`）**会抛错**——它真读一次文件。若把两者合成一步，
 *    界面就分不清"连不通"与"连得通但里面是空的"，而这两种状态要给的下一步动作
 *    完全不同（改配置 vs 换一份数据）。
 *
 * 新增为什么不做"先校验再提交"
 * -------------------------
 * `POST` 时服务端会做**配置期校验**（接入器认不认识、`root` 解不解得开），但刻意
 * **不读文件内容**——今天那份文件被移走不该让登记失败。所以界面上"能不能登记"的判据
 * 只有服务端有，本页不预判（硬约束 15：禁用判据必须来自服务端）。
 *
 * 可选接入器不是前端枚举
 * -------------------
 * `known_types` 由 `GET /projects/{pid}/sources` 一并给出，它来自**服务端注册表**
 * （可由 entry point 扩充）。契约里允许的名字（如 `markdown`）若还没有接入器，
 * 出现在下拉里也会在登记时被服务端拒掉——那句补救话术由服务端给。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { useParams } from "react-router-dom";

import { sourcesApi } from "../api/endpoints";
import type { Schemas, SourceItemsOut, SourceOut, SourceProbeOut } from "../api/types";
import { EmptyState, ErrorPanel, KeyValues, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";

type Source = SourceOut;

/** 探测留痕：`null`（从没探过）与 `false`（探了没通）是两件事，故不折成布尔。 */
function ProbeTag({ source }: { source: Source }): ReactNode {
  const state = source.last_probe_ok;
  if (state === null || state === undefined) {
    return <span className="faint small">{zh.p06.neverProbed}</span>;
  }
  return (
    <span className={state ? "tag tag--ok" : "tag tag--danger"}>
      {state ? zh.p06.probedOk : zh.p06.probedFailed}
    </span>
  );
}

function ItemsPanel({
  projectId,
  sourceId,
  onClose,
}: {
  projectId: string;
  sourceId: string;
  onClose: () => void;
}): ReactNode {
  const items = useQuery({
    queryKey: ["source-items", projectId, sourceId],
    queryFn: () => sourcesApi.items(projectId, sourceId),
  });

  return (
    <div className="card">
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>{zh.p06.itemsTitle(sourceId)}</h2>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={onClose}>
            {zh.app.close}
          </button>
        </div>
      </div>

      {items.isPending && <Loading />}
      {items.isError && <ErrorPanel error={items.error} />}

      {items.data !== undefined && <ItemsBody data={items.data} />}
    </div>
  );
}

function ItemsBody({ data }: { data: SourceItemsOut }): ReactNode {
  const kinds = Object.entries(data.kinds);
  return (
    <>
      <p className="muted small">
        {zh.p06.itemsTotal(data.total)}
        {data.truncated && (
          <>
            {" · "}
            <span style={{ color: "var(--warn)" }}>{zh.p06.itemsTruncated(data.facts.length)}</span>
          </>
        )}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p06.kindsTitle}</h3>
      {kinds.length === 0 ? (
        <p className="muted small" style={{ marginTop: 0 }}>
          {zh.p06.itemsNoRows}
        </p>
      ) : (
        <KeyValues items={kinds.map(([kind, count]) => ({ label: kind, value: count }))} />
      )}

      <h3 style={{ marginBottom: 6 }}>{zh.p06.colItems}</h3>
      {data.facts.length === 0 ? (
        <p className="muted small" style={{ marginTop: 0 }}>
          {zh.p06.itemsNoRows}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{zh.p06.colKind}</th>
                <th>{zh.p06.colKey}</th>
                <th>{zh.p06.colFields}</th>
              </tr>
            </thead>
            <tbody>
              {data.facts.map((fact, index) => (
                <tr key={`${fact.kind}-${fact.key.join("|")}-${index}`}>
                  <td className="small">{fact.kind}</td>
                  <td className="mono small">{fact.key.join(" · ")}</td>
                  <td className="small mono">{Object.keys(fact.payload ?? {}).join(", ") || zh.app.none}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.assets.length > 0 && (
        <>
          <h3 style={{ marginBottom: 6 }}>{zh.p06.assetsTitle}</h3>
          <p className="row row--wrap" style={{ marginTop: 0 }}>
            {data.assets.map((asset) => (
              <span key={asset.logical_name} className="tag tag--neutral">
                {asset.logical_name}
              </span>
            ))}
          </p>
        </>
      )}

      {data.warnings.length > 0 && (
        <>
          <h3 style={{ marginBottom: 6 }}>{zh.p06.warningsTitle}</h3>
          <ul className="small">
            {data.warnings.map((warning) => (
              <li key={warning.code + warning.message}>
                <span className="mono small">{warning.code}</span> {warning.message}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

function ProbeResult({ result }: { result: SourceProbeOut }): ReactNode {
  return (
    <div className="card card--quiet">
      <p className="small" style={{ marginTop: 0 }}>
        <span className="mono">{result.source_id}</span>{" "}
        <span className={result.ok ? "tag tag--ok" : "tag tag--danger"}>
          {result.ok ? zh.p06.probedOk : zh.p06.probedFailed}
        </span>
      </p>
      <KeyValues
        items={[
          { label: zh.p06.probeDetail, value: result.detail },
          { label: zh.p06.colItems, value: result.item_count ?? zh.app.none },
          { label: zh.p06.colLastProbe, value: formatTime(result.probed_at) },
        ]}
      />
      {result.warnings.length > 0 && (
        <ul className="small">
          {result.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p06.probeNote}
      </p>
    </div>
  );
}

function SourceRow({
  source,
  onProbe,
  onItems,
  onAskRemove,
  onCancelRemove,
  onRemove,
  busy,
  confirming,
}: {
  source: Source;
  onProbe: () => void;
  onItems: () => void;
  onAskRemove: () => void;
  onCancelRemove: () => void;
  onRemove: () => void;
  busy: boolean;
  confirming: boolean;
}): ReactNode {
  return (
    <tr>
      <td>
        <span className="mono">{source.source_id}</span>
        {source.title !== null && source.title !== undefined && (
          <div className="faint small">{source.title}</div>
        )}
      </td>
      <td className="small mono">{source.source_type}</td>
      <td className="mono small">{source.root ?? zh.app.none}</td>
      <td className="small">{source.priority}</td>
      <td className="small">
        {source.enabled ? (
          <span className="tag tag--ok">{zh.p06.enabledTag}</span>
        ) : (
          <span className="tag tag--warn">{zh.p06.disabledTag}</span>
        )}
      </td>
      <td className="small">
        <ProbeTag source={source} />
        {source.last_probe_detail !== null && source.last_probe_detail !== undefined && (
          <div className="faint small">{source.last_probe_detail}</div>
        )}
        {source.last_probed_at !== null && source.last_probed_at !== undefined && (
          <div className="faint small">{formatTime(source.last_probed_at)}</div>
        )}
      </td>
      <td className="small">{source.item_count ?? zh.app.none}</td>
      <td className="small">
        <div className="row" style={{ gap: 6 }}>
          <button type="button" className="btn btn--sm" disabled={busy} onClick={onProbe}>
            {busy ? zh.p06.probing : zh.p06.probe}
          </button>
          <button type="button" className="btn btn--sm" onClick={onItems}>
            {zh.p06.items}
          </button>
          {/*
           * 二次确认做成**行内**而不是 `window.confirm`：后者会阻塞整个页面、
           * 无法承载"删掉的到底是什么"这句说明（同 P08 的取消作业、P13 的清理）。
           */}
          {confirming ? (
            <>
              <button type="button" className="btn btn--sm btn--danger" onClick={onRemove}>
                {zh.app.confirm}
              </button>
              <button type="button" className="btn btn--sm" onClick={onCancelRemove}>
                {zh.app.cancel}
              </button>
            </>
          ) : (
            <button type="button" className="btn btn--sm btn--danger" onClick={onAskRemove}>
              {zh.p06.remove}
            </button>
          )}
        </div>
        {confirming && (
          <div className="faint small" style={{ marginTop: 4, maxWidth: 320 }}>
            {zh.p06.removeConfirm(source.source_id)}
            {zh.p06.removeNote}
          </div>
        )}
      </td>
    </tr>
  );
}

function SourceForm({ projectId, knownTypes }: { projectId: string; knownTypes: readonly string[] }): ReactNode {
  const [sourceId, setSourceId] = useState("");
  const [sourceType, setSourceType] = useState(knownTypes[0] ?? "");
  const [title, setTitle] = useState("");
  const [root, setRoot] = useState("");
  const [priority, setPriority] = useState("0");
  const [enabled, setEnabled] = useState(true);
  const [author, setAuthor] = useState("");
  const [bindings, setBindings] = useState("[]");
  const [options, setOptions] = useState("{}");
  const [message, setMessage] = useState<string | null>(null);
  const client = useQueryClient();

  const create = useMutation({
    mutationFn: () =>
      sourcesApi.create(projectId, {
        author: author.trim() === "" ? null : author.trim(),
        config: {
          source_id: sourceId.trim(),
          /*
           * 下拉里给的是**服务端注册表**里的接入器名（`known_types`），而契约里
           * `source_type` 是一个固定的字面量联合。这里必须显式收窄：把 `string`
           * 直接当那个联合用会让"注册表多出一个契约里没有的名字"这件事在编译期
           * 消失，而它恰恰要在服务端被拒掉（那句补救话术由服务端给）。
           */
          source_type: sourceType as Schemas["SourceConfig"]["source_type"],
          title: title.trim() === "" ? null : title.trim(),
          root: root.trim() === "" ? null : root.trim(),
          priority: Number.parseInt(priority, 10) || 0,
          enabled,
          bindings: JSON.parse(bindings) as Schemas["SourceConfig"]["bindings"],
          options: JSON.parse(options) as Schemas["SourceConfig"]["options"],
          format_version: "1.0.0",
        },
      }),
    onSuccess: (result) => {
      setMessage(result.created ? zh.p06.created : zh.p06.alreadyExists);
      setSourceId("");
      setTitle("");
      setRoot("");
      void client.invalidateQueries({ queryKey: ["sources", projectId] });
    },
  });

  return (
    <div className="card">
      <h2>{zh.p06.addTitle}</h2>

      <div className="form-row" style={{ marginBottom: 12 }}>
        <label className="field">
          <span>{zh.p06.sourceId}</span>
          <input
            value={sourceId}
            placeholder={zh.p06.sourceIdPlaceholder}
            onChange={(event) => setSourceId(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p06.sourceType}</span>
          <select value={sourceType} onChange={(event) => setSourceType(event.target.value)}>
            {knownTypes.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{zh.p06.sourceTitle}</span>
          <input
            value={title}
            placeholder={zh.p06.sourceTitlePlaceholder}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p06.root}</span>
          <input
            value={root}
            placeholder={zh.p06.rootPlaceholder}
            onChange={(event) => setRoot(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p06.priority}</span>
          <input
            type="number"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p06.authorLabel}</span>
          <input
            value={author}
            placeholder={zh.p06.authorPlaceholder}
            onChange={(event) => setAuthor(event.target.value)}
          />
        </label>
      </div>

      <div className="row row--wrap" style={{ marginBottom: 12 }}>
        <label className="row small" style={{ gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          <span>{zh.p06.enabled}</span>
        </label>
        <span className="faint small">{zh.p06.priorityNote}</span>
      </div>

      <p className="faint small">{zh.p06.rootNote}</p>
      <p className="faint small">{zh.p06.typeNote}</p>

      <div className="form-row" style={{ marginBottom: 12 }}>
        <label className="field">
          <span>{zh.p06.bindings}</span>
          <textarea value={bindings} rows={6} onChange={(event) => setBindings(event.target.value)} />
        </label>
        <label className="field">
          <span>{zh.p06.options}</span>
          <textarea value={options} rows={6} onChange={(event) => setOptions(event.target.value)} />
        </label>
      </div>

      {message !== null && (
        <p className="small" style={{ color: "var(--ok)" }}>
          {message}
        </p>
      )}
      {create.isError && <ErrorPanel error={create.error} />}

      <button
        type="button"
        className="btn btn--primary btn--sm"
        disabled={
          create.isPending || sourceId.trim() === "" || sourceType === "" || knownTypes.length === 0
        }
        onClick={() => create.mutate()}
      >
        {create.isPending ? zh.p06.creating : zh.p06.create}
      </button>
    </div>
  );
}

export function P06Sources(): ReactNode {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const [probeOf, setProbeOf] = useState<string | null>(null);
  const [itemsOf, setItemsOf] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const client = useQueryClient();

  const list = useQuery({
    queryKey: ["sources", projectId],
    queryFn: () => sourcesApi.list(projectId),
  });

  const probe = useMutation({
    mutationFn: (sourceId: string) => sourcesApi.probe(projectId, sourceId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["sources", projectId] });
    },
  });

  const remove = useMutation({
    mutationFn: (sourceId: string) => sourcesApi.remove(projectId, sourceId),
    onSuccess: () => {
      setMessage(zh.p06.removed);
      setItemsOf(null);
      void client.invalidateQueries({ queryKey: ["sources", projectId] });
    },
  });

  const items: readonly Source[] = list.data?.items ?? [];
  const knownTypes: readonly string[] = list.data?.known_types ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p06.title}</h1>
          <p>{zh.p06.subtitle}</p>
        </div>
        <div className="page-head__actions">
          <span className="muted small">{list.data === undefined ? "" : `${items.length}`}</span>
          <button type="button" className="btn btn--sm" onClick={() => void list.refetch()}>
            {zh.app.refresh}
          </button>
        </div>
      </div>

      {list.isPending && <Loading />}
      {list.isError && <ErrorPanel error={list.error} />}

      {list.data !== undefined && (
        <>
          <SourceForm projectId={projectId} knownTypes={knownTypes} />

          <div className="card">
            <div className="row row--wrap" style={{ marginBottom: 8 }}>
              <h2 style={{ margin: 0 }}>{zh.p06.title}</h2>
              <span className="faint small">
                {zh.p06.unknownTypes}：{knownTypes.join(" / ") || zh.app.none}
              </span>
            </div>

            {message !== null && (
              <p className="small" style={{ color: "var(--ok)" }}>
                {message}
              </p>
            )}
            {remove.isError && <ErrorPanel error={remove.error} />}
            {probe.isError && <ErrorPanel error={probe.error} />}

            {items.length === 0 ? (
              <EmptyState title={zh.p06.emptyTitle} body={zh.p06.emptyNote} />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{zh.p06.colSource}</th>
                      <th>{zh.p06.colType}</th>
                      <th>{zh.p06.colRoot}</th>
                      <th>{zh.p06.colPriority}</th>
                      <th>{zh.p06.colEnabled}</th>
                      <th>{zh.p06.colLastProbe}</th>
                      <th>{zh.p06.colItems}</th>
                      <th>{zh.p06.colActions}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((source) => (
                      <SourceRow
                        key={source.source_id}
                        source={source}
                        busy={probe.isPending && probe.variables === source.source_id}
                        onProbe={() => {
                          setMessage(null);
                          setProbeOf(source.source_id);
                          probe.mutate(source.source_id);
                        }}
                        onItems={() => {
                          setMessage(null);
                          setItemsOf(source.source_id);
                        }}
                        confirming={removing === source.source_id}
                        onAskRemove={() => {
                          setMessage(null);
                          setRemoving(source.source_id);
                        }}
                        onCancelRemove={() => setRemoving(null)}
                        onRemove={() => {
                          setRemoving(null);
                          remove.mutate(source.source_id);
                        }}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {probeOf !== null && probe.data !== undefined && probe.variables === probeOf && (
            <ProbeResult result={probe.data} />
          )}

          {itemsOf !== null && (
            <ItemsPanel projectId={projectId} sourceId={itemsOf} onClose={() => setItemsOf(null)} />
          )}
        </>
      )}
    </>
  );
}
