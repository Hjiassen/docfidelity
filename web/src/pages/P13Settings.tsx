/**
 * P13 设置 —— 对应需求 **FR-M6-01 / FR-M6-05 / FR-M6-10 / NFR-13 / NFR-22**；
 * 入口 `/settings`（站点地图 7.3.2）。
 *
 * 本页守住的三条（DD-14.5）
 * ----------------------
 * ① **数据边界排在最上面**：DD-13.7 要求"数据不出内网"在界面上**可见**。所以第一张卡
 *    是只读的边界视图（部署方式 / 是否外发 / 存储位置 / 确定性姿态），不是可编辑表单——
 *    把只读事实和可改配置混在一张卡里，使用者就分不清"这是我改出来的"还是"这是本来就这样"。
 *    紧跟着的「部署形态与数据来源」（`p13Deployment.tsx`）是同一区的第二块：前者念**配置**，
 *    后者念**这台机器此刻的运行现状**（`/health` 的 `worker_mode` 与 `demo_seed`）。
 *    两处必须对得上，摆在一起才看得出来。
 *
 * ② **每个「能改的」都对应配置文件里的一行**（硬规则 10 / 7.1.1）。哪些字段可改
 *    **由服务端给出**（`SettingsView.mutable_fields`），前端不写死一份清单：写死就是两处
 *    口径，服务端收紧了可改字段集而前端还在提交旧字段，表现为"保存报错但看不出为什么"。
 *    不可改的字段一律渲染成只读块并标注原因，而不是干脆不显示——不显示会让人以为
 *    "这个项目没有这项设置"。
 *
 * ③ **界面不做业务判断**（NFR-22）。数值范围、后缀白名单、部署方式的合法性都由服务端
 *    校验并以统一错误包回带；本页只拦一种前端专属的问题：输入框里根本不是数字
 *    （那是解析失败，不是业务规则）。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { systemApi } from "../api/endpoints";
import type {
  DataBoundary,
  ModelHealth,
  RetainedObjectOut,
  SettingsView,
  StagingObject,
} from "../api/types";
import { DataBoundaryFacts } from "../components/DataBoundaryFacts";
import { EmptyState, ErrorPanel, KeyValues, Loading, formatBytes, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";
import { DeploymentCard } from "./p13Deployment";

/* ------------------------------------------------------------------ 常量 */

/** 可改字段名（与 `server/maintenance.py::MUTABLE_SETTINGS_FIELDS` **逐字对应**）。 */
const KEYS = {
  deployment: "deployment",
  modelEnabled: "model_enabled",
  modelBaseUrl: "model_base_url",
  modelName: "model_name",
  maxUploadBytes: "max_upload_bytes",
  allowedSuffixes: "allowed_upload_suffixes",
  retentionUploads: "retention_uploads_days",
  retentionWorkspace: "retention_workspace_days",
  retentionArtifacts: "retention_artifacts_days",
  workerConcurrency: "worker_concurrency",
} as const;

const DEPLOYMENTS: readonly DataBoundary["deployment"][] = ["local", "intranet", "public"];

const DEPLOYMENT_LABEL: Record<DataBoundary["deployment"], string> = {
  local: zh.p13.deploymentLocal,
  intranet: zh.p13.deploymentIntranet,
  public: zh.p13.deploymentPublic,
};

/** 表单草稿：数字一律以字符串持有，否则输入框无法被清空（`Number("") === 0` 会咬人）。 */
interface Draft {
  deployment: DataBoundary["deployment"];
  model_enabled: boolean;
  model_base_url: string;
  model_name: string;
  max_upload_bytes: string;
  allowed_upload_suffixes: string;
  retention_uploads_days: string;
  retention_workspace_days: string;
  retention_artifacts_days: string;
  worker_concurrency: string;
}

function draftFrom(view: SettingsView): Draft {
  const policy = view.retention;
  return {
    deployment: view.data_boundary.deployment,
    model_enabled: view.model.enabled,
    model_base_url: view.model.base_url ?? "",
    model_name: view.model.name ?? "",
    max_upload_bytes: String(view.uploads.max_bytes),
    allowed_upload_suffixes: view.uploads.allowed_suffixes.join(","),
    retention_uploads_days: String(policy.uploads_days),
    retention_workspace_days: String(policy.workspace_days),
    retention_artifacts_days: String(policy.artifacts_days),
    worker_concurrency: String(view.workers.concurrency),
  };
}

/** 输入框里的数字：空串与非数字一律 `null`（这是解析问题，由本层拦下）。 */
function toNumber(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

function splitSuffixes(raw: string): string[] {
  return raw
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/* ------------------------------------------------------------------ 字段封装 */

/**
 * 一个配置项 = 一行。
 *
 * `mutable` 由服务端的 `mutable_fields` 决定：不可改时渲染只读块并说明原因，
 * 而不是把控件禁用——禁用会诱使人以为是权限问题，而实际是"这一项根本不走接口"。
 */
function Field({
  label,
  configKey,
  mutable,
  hint,
  children,
  readOnlyText,
}: {
  label: string;
  configKey: string;
  mutable: boolean;
  hint?: string;
  children: ReactNode;
  readOnlyText: string;
}): ReactNode {
  if (!mutable) {
    return (
      <div className="form-row">
        <span className="form-row__label">
          {label} <span className="tag tag--neutral">{zh.p13.readonlyTag}</span>
        </span>
        <div>
          <div className="mono">{readOnlyText}</div>
          <p className="faint small" style={{ margin: 0 }}>
            <span className="mono">{configKey}</span> · {zh.p13.notMutableNote}
          </p>
        </div>
      </div>
    );
  }
  return (
    <div className="form-row">
      <label className="form-row__label" htmlFor={undefined}>
        {label}
      </label>
      <div>
        {children}
        <p className="faint small" style={{ margin: 0 }}>
          <span className="mono">{configKey}</span>
          {hint === undefined ? "" : ` · ${hint}`}
        </p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 数据边界 */

function BoundaryCard({
  boundary,
  deterministic,
  modelHealth,
}: {
  boundary: DataBoundary;
  deterministic: SettingsView["deterministic"];
  modelHealth: ModelHealth | undefined;
}): ReactNode {
  // `deterministic` 在生成类型里是可选的（服务端给了 `default_factory`，故它不进
  // OpenAPI 的 `required`）。**缺了就说缺了**，不补一份默认值——补出来的姿态与真实
  // 生效值长得一样，但那正是"配了没生效"最难查的那种谎。
  const posture = deterministic;
  return (
    <div className="card card--quiet">
      <h2>{zh.p13.boundaryTitle}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {zh.p13.boundaryPrinciple}
      </p>

      <KeyValues
        items={[
          { label: zh.p13.deployment, value: DEPLOYMENT_LABEL[boundary.deployment] },
          {
            label: zh.p13.boundaryOnline,
            value: boundary.external_calls ? zh.p13.boundaryOnlineYes : zh.p13.boundaryOnlineNo,
          },
          {
            label: zh.p13.boundaryModelEnabled,
            value: boundary.model_enabled ? zh.p13.boundaryModelEnabled : zh.p13.boundaryModelDisabled,
          },
          { label: zh.p13.boundaryStorage, value: boundary.storage_path ?? zh.app.none },
          {
            label: zh.p13.deterministicMode,
            value:
              posture === undefined ? (
                <span className="faint small">{zh.p13.deterministicUnreported}</span>
              ) : (
                <>
                  <span className="tag tag--neutral">
                    {posture.mode ? zh.p13.deterministicModeOn : zh.p13.deterministicModeOff}
                  </span>{" "}
                  <span className="faint small">{zh.p13.deterministicModeNote}</span>
                </>
              ),
          },
          {
            label: zh.p13.officeParticipation,
            value:
              posture === undefined ? (
                <span className="faint small">{zh.p13.deterministicUnreported}</span>
              ) : posture.office_enabled ? (
                <span className="tag tag--ok">{zh.p13.officeOn}</span>
              ) : (
                <>
                  <span className="tag tag--neutral">{zh.p13.officeOff}</span>{" "}
                  {/* 成因原样照抄服务端：`office_reason` 把"配置关的"与"姿态压住的"
                      分开措辞，两种处置不同（改配置 / 接受确定性姿态）。 */}
                  <span className="faint small">{posture.office_reason}</span>
                </>
              ),
          },
        ]}
      />

      {/* 出口白名单与数据卷：**唯一一处**声明，故不在这张 KeyValues 里再念一遍端点。 */}
      <DataBoundaryFacts boundary={boundary} />

      <div className="grid grid--cards" style={{ marginTop: 12 }}>
        <div>
          <h3 className="small">{zh.p13.boundaryLeaves}</h3>
          <ul className="small muted">
            {zh.p13.boundaryLeavesList.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="small">{zh.p13.boundaryStays}</h3>
          <ul className="small muted">
            {zh.p13.boundaryStaysList.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      </div>

      {modelHealth !== undefined && (
        <p className="small" style={{ marginBottom: 0 }}>
          <span className="muted">{zh.p13.modelProbeDetail}：</span>
          {modelHealth.reachable === null || modelHealth.reachable === undefined ? (
            <span className="tag tag--neutral">{zh.p13.modelProbeDisabled}</span>
          ) : modelHealth.reachable ? (
            <span className="tag tag--ok">{zh.p13.modelProbeReachable}</span>
          ) : (
            <span className="tag tag--danger">{zh.p13.modelProbeUnreachable}</span>
          )}{" "}
          <span className="mono small">{modelHealth.detail}</span>
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 模型能力 */

/**
 * 模型能力与降级（DD-12.4 / NFR-23）。
 *
 * 上面那张卡回答「**连得上吗**」（一次出网探测，要按按钮）；这张回答「**接通之后会怎么跑**」
 * ——本机有没有受限解码后端、存储里有没有基线评测报告。两者合成一张，会让「连不上」与
 * 「没有受限解码」看起来是同一件事，而处置完全不同（查网络 / 装依赖或接受降级）。
 *
 * 两项都**不出网**（`probe_grammar` 查本机依赖、`load_baseline_status` 读一份报告），
 * 所以挂载即取，不设探测按钮。
 *
 * 「未加载原因」原样显示服务端的措辞：服务端把三类情况（还没跑过 / 读不动 / 读得到）
 * 分开措辞，指向不同处置；在界面归并成一句「未配置」，人就会去查错的地方（硬规则 15）。
 */
function CapabilitiesCard(): ReactNode {
  const capabilities = useQuery({
    queryKey: ["model-capabilities"],
    queryFn: systemApi.modelCapabilities,
    retry: false,
  });

  const data = capabilities.data;
  const byPoint = data === undefined ? [] : Object.entries(data.baseline.by_point);

  return (
    <div className="card card--quiet">
      <h2>{zh.p13.capabilitiesTitle}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {zh.p13.capabilitiesIntro}
      </p>
      <p className="faint small" style={{ marginTop: 0 }}>
        {zh.p13.capabilitiesSourceNote}
      </p>

      {capabilities.isPending && <Loading />}
      {capabilities.isError && <ErrorPanel error={capabilities.error} />}

      {data !== undefined && (
        <>
          {!data.enabled && (
            <p className="small" style={{ marginTop: 0 }}>
              <span className="tag tag--neutral">{zh.p13.boundaryModelDisabled}</span>{" "}
              {zh.p13.capabilitiesDisabledNote}
            </p>
          )}

          <KeyValues
            items={[
              {
                label: zh.p13.grammarTitle,
                value: (
                  <span className={data.grammar.supported ? "tag tag--ok" : "tag tag--neutral"}>
                    {data.grammar.supported
                      ? zh.p13.grammarSupported
                      : zh.p13.grammarUnsupported}
                  </span>
                ),
              },
              { label: zh.p13.grammarBackend, value: data.grammar.backend ?? zh.app.none },
              {
                label: zh.p13.baselineTitle,
                value: (
                  <span className={data.baseline.loaded ? "tag tag--ok" : "tag tag--neutral"}>
                    {data.baseline.loaded ? zh.p13.baselineLoaded : zh.p13.baselineNotLoaded}
                  </span>
                ),
              },
              {
                label: zh.p13.baselinePath,
                value: <span className="mono small">{data.baseline.path}</span>,
              },
            ]}
          />

          {/* 受限解码说明：可用与否都照列服务端原话（可用时它给出后端名单）。 */}
          <p className="small muted" style={{ marginTop: 0 }}>
            <span className="mono">{data.grammar.detail}</span>
          </p>

          {!data.baseline.loaded && (
            <p className="small" style={{ marginBottom: 0 }}>
              <span className="muted">{zh.p13.baselineNotLoaded}：</span>
              <span className="mono small">{data.baseline.problem}</span>
            </p>
          )}

          {data.baseline.loaded && (
            <>
              <p className="small" style={{ marginTop: 0 }}>
                <span className="muted">{zh.p13.baselineModel}：</span>
                {data.baseline.model || zh.app.none} ·{" "}
                <span className="muted">{zh.p13.baselineEndpoint}：</span>
                <span className="mono small">{data.baseline.endpoint_digest?.slice(0, 12) || zh.app.none}</span>{" "}
                ·{" "}
                <span className="muted">{zh.p13.baselineDigest}：</span>
                <span className="mono small">{data.baseline.cases_digest || zh.app.none}</span>{" "}
                · <span className="muted">{zh.p13.baselinePosture}：</span>
                <span className="mono small">{data.baseline.routing_digest || zh.app.none}</span>
              </p>

              <h3 className="small">{zh.p13.baselineByPoint}</h3>
              <p className="faint small" style={{ marginTop: 0 }}>
                {zh.p13.baselineByPointNote}
              </p>
              <ul className="small muted" style={{ marginTop: 0 }}>
                {byPoint.map(([point, rate]) => (
                  <li key={point}>
                    <span className="mono">{point}</span>：{rate.toFixed(2)}
                  </li>
                ))}
              </ul>

              <h3 className="small">{zh.p13.baselineUncovered}</h3>
              {data.baseline.uncovered.length === 0 ? (
                <p className="faint small" style={{ marginTop: 0 }}>
                  {zh.p13.baselineUncoveredNone}
                </p>
              ) : (
                <>
                  <p className="faint small" style={{ marginTop: 0 }}>
                    {zh.p13.baselineUncoveredNote}
                  </p>
                  <ul className="small muted" style={{ marginTop: 0 }}>
                    {data.baseline.uncovered.map((point) => (
                      <li key={point}>
                        <span className="mono">{point}</span>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              <h3 className="small">{zh.p13.baselineFailures}</h3>
              {data.baseline.failures.length === 0 ? (
                <p className="faint small" style={{ marginTop: 0 }}>
                  {zh.p13.baselineNoFailures}
                </p>
              ) : (
                <ul className="small muted" style={{ marginTop: 0 }}>
                  {data.baseline.failures.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 过期对象 */

function ExpiredCard(): ReactNode {
  const client = useQueryClient();
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set<string>());
  const [confirming, setConfirming] = useState(false);

  const expired = useQuery({
    queryKey: ["expired"],
    queryFn: systemApi.expired,
  });

  const cleanup = useMutation({
    mutationFn: (objects: readonly string[]) => systemApi.cleanup(objects),
    onSuccess: () => {
      setSelected(new Set<string>());
      setConfirming(false);
      // 回收后登记已摘掉，候选列表必须重取——本地删一行会让"服务器上还在不在"变成猜测。
      void client.invalidateQueries({ queryKey: ["expired"] });
    },
  });

  const items: readonly RetainedObjectOut[] = expired.data?.expired ?? [];

  const grouped = useMemo(() => {
    const kinds: readonly RetainedObjectOut["kind"][] = ["upload", "workspace", "artifact"];
    return kinds
      .map((kind) => ({ kind, rows: items.filter((item) => item.kind === kind) }))
      .filter((group) => group.rows.length > 0);
  }, [items]);

  function toggle(objectId: string, checked: boolean): void {
    const next = new Set(selected);
    if (checked) next.add(objectId);
    else next.delete(objectId);
    setSelected(next);
  }

  return (
    <div className="card">
      <h2>{zh.p13.expiredTitle}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        {zh.p13.expiredIntro}
      </p>

      {expired.isPending && <Loading />}
      {expired.isError && <ErrorPanel error={expired.error} />}

      {expired.isSuccess && items.length === 0 && (
        <EmptyState
          title={zh.p13.expiredEmpty}
          body={zh.p13.retentionTitle}
          action={
            <button type="button" className="btn" onClick={() => void expired.refetch()}>
              {zh.app.refresh}
            </button>
          }
        />
      )}

      {items.length > 0 && (
        <>
          <div className="row row--wrap" style={{ marginBottom: 8 }}>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => setSelected(new Set(items.map((item) => item.object_id)))}
            >
              {zh.p13.selectAll}
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => setSelected(new Set<string>())}
            >
              {zh.p13.clearSelection}
            </button>
            <span className="muted small">{zh.p13.selectedCount(selected.size)}</span>
          </div>

          {grouped.map((group) => (
            <div key={group.kind} style={{ marginTop: 12 }}>
              <h3 className="small">
                {zh.p13.retentionKind[group.kind]}{" "}
                <span className="muted">({group.rows.length})</span>
              </h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th style={{ width: 36 }} />
                      <th>{zh.p09.docKey}</th>
                      <th>{zh.generic.objectId}</th>
                      <th>{zh.p13.sizeBytes}</th>
                      <th>{zh.generic.createdAt}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.rows.map((item) => (
                      <tr key={item.object_id}>
                        <td>
                          <input
                            type="checkbox"
                            checked={selected.has(item.object_id)}
                            onChange={(event) => toggle(item.object_id, event.target.checked)}
                            aria-label={item.object_id}
                          />
                        </td>
                        <td className="mono small">{item.path}</td>
                        <td className="mono small">{item.object_id}</td>
                        <td className="small">{item.size_bytes}</td>
                        <td className="small">{item.created_at ?? zh.app.none}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}

          {cleanup.isError && (
            <div style={{ marginTop: 12 }}>
              <ErrorPanel error={cleanup.error} />
            </div>
          )}

          {cleanup.isSuccess && (
            <p className="small tag tag--ok" style={{ marginTop: 12 }}>
              {zh.p13.cleanupDone(cleanup.data.count)}
            </p>
          )}

          <p className="faint small">{zh.p13.cleanupHint}</p>

          <div className="row row--wrap">
            {!confirming ? (
              <button
                type="button"
                className="btn btn--danger"
                disabled={selected.size === 0 || cleanup.isPending}
                onClick={() => setConfirming(true)}
              >
                {zh.p13.cleanup}
              </button>
            ) : (
              <>
                <span className="small">{zh.p13.cleanupConfirm(selected.size)}</span>
                <button
                  type="button"
                  className="btn btn--danger"
                  disabled={cleanup.isPending}
                  onClick={() => cleanup.mutate([...selected])}
                >
                  {zh.p13.cleanupConfirmYes}
                </button>
                <button type="button" className="btn" onClick={() => setConfirming(false)}>
                  {zh.app.cancel}
                </button>
              </>
            )}
          </div>
          {selected.size === 0 && !confirming && (
            <p className="faint small" style={{ marginBottom: 0 }}>
              {zh.p13.cleanupNone}
            </p>
          )}
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- 导入暂存目录 */

/**
 * 模板导入的暂存目录（`imports/`）。
 *
 * 与「过期对象」分开是**刻意的**：那一区的判据是保留策略的"过期了没有"，这一区的判据是
 * "登记了没有"——暂存里带着「稍后重试解析」的承诺，未登记就删等于让那条承诺失效。
 * 两区共用一张卡就会让人以为它们同一条规矩，而 `imports/` 根本不在保留策略里。
 *
 * 三处一律照抄服务端（硬规则 15 / NFR-22）：
 * ① 卡片顶部那句话取自响应的 `note`（服务端判据的原文），界面不自己归纳；
 * ② 每一行的"可回收 / 不可回收 + 原因"取自 `reclaimable` / `blocked_reason`；
 * ③ 不可回收的行**不可勾选**——因为服务端会因它整次拒绝（不是界面自己收紧）。
 */
function StagingCard(): ReactNode {
  const client = useQueryClient();
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set<string>());
  const [confirming, setConfirming] = useState(false);

  const staging = useQuery({
    queryKey: ["staging"],
    queryFn: systemApi.staging,
  });

  const purge = useMutation({
    mutationFn: (objects: readonly string[]) => systemApi.purgeStaging(objects),
    onSuccess: () => {
      setSelected(new Set<string>());
      setConfirming(false);
      // 回收后登记在别处的记录仍指着同一个 import_id，候选列表必须重取。
      void client.invalidateQueries({ queryKey: ["staging"] });
    },
  });

  const items: readonly StagingObject[] = staging.data?.imports ?? [];

  function toggle(importId: string, checked: boolean): void {
    const next = new Set(selected);
    if (checked) next.add(importId);
    else next.delete(importId);
    setSelected(next);
  }

  return (
    <div className="card">
      <h2>{zh.p13.stagingTitle}</h2>
      {staging.isPending && <Loading />}
      {staging.isError && <ErrorPanel error={staging.error} />}

      {staging.isSuccess && (
        <>
          {/* 判据的原文来自服务端；"出处"一行是为了让人知道这句话不是界面编的。 */}
          <p className="muted" style={{ marginTop: 0 }}>
            {staging.data.note}
          </p>
          <p className="faint small">
            {zh.p13.stagingNoteSource}：<span className="mono">GET /api/v1/maintenance/staging</span>
          </p>

          {items.length === 0 ? (
            <EmptyState
              title={zh.p13.stagingEmpty}
              body={zh.p13.stagingEmptyBody}
              action={
                <button type="button" className="btn" onClick={() => void staging.refetch()}>
                  {zh.app.refresh}
                </button>
              }
            />
          ) : (
            <>
              <p className="small muted">
                {zh.p13.stagingCount(staging.data.count, staging.data.reclaimable_count)} ·{" "}
                {formatBytes(staging.data.total_bytes)}
              </p>

              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th style={{ width: 36 }} />
                      <th>{zh.p13.stagingImportId}</th>
                      <th>{zh.p13.stagingRefColumn}</th>
                      <th>{zh.p13.sizeBytes}</th>
                      <th>{zh.generic.createdAt}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <tr key={item.import_id}>
                        <td>
                          <input
                            type="checkbox"
                            checked={selected.has(item.import_id)}
                            disabled={!item.reclaimable}
                            onChange={(event) => toggle(item.import_id, event.target.checked)}
                            aria-label={item.import_id}
                          />
                        </td>
                        <td className="mono small">
                          {item.import_id}
                          <div className="faint small">{item.path}</div>
                        </td>
                        <td className="small">
                          {item.reclaimable ? (
                            <span className="tag tag--ok">{zh.p13.stagingReclaimable}</span>
                          ) : (
                            <>
                              <span className="tag tag--neutral">{zh.p13.stagingBlocked}</span>
                              <div className="muted small">
                                {zh.p13.stagingBlockedLabel}：{item.blocked_reason}
                              </div>
                            </>
                          )}
                          <div className="faint small">
                            {item.template_ref === null || item.template_ref === undefined
                              ? item.readable
                                ? zh.p13.stagingNotRegistered
                                : zh.p13.stagingUnreadable
                              : `${zh.p13.stagingRegisteredAs} ${item.template_ref}`}
                          </div>
                        </td>
                        <td className="small">{item.size_bytes}</td>
                        <td className="small">
                          {item.created_at === null || item.created_at === undefined
                            ? zh.app.none
                            : formatTime(item.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="row row--wrap" style={{ marginTop: 8 }}>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() =>
                    setSelected(
                      new Set(items.filter((item) => item.reclaimable).map((item) => item.import_id)),
                    )
                  }
                >
                  {zh.p13.stagingSelectReclaimable}
                </button>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => setSelected(new Set<string>())}
                >
                  {zh.p13.clearSelection}
                </button>
                <span className="muted small">{zh.p13.selectedCount(selected.size)}</span>
              </div>

              {purge.isError && (
                <div style={{ marginTop: 12 }}>
                  <ErrorPanel error={purge.error} />
                </div>
              )}

              {purge.isSuccess && (
                <p className="small tag tag--ok" style={{ marginTop: 12 }}>
                  {zh.p13.stagingPurgeDone(purge.data.count)}
                </p>
              )}

              <p className="faint small">{zh.p13.stagingPurgeHint}</p>
              <p className="faint small">{zh.p13.stagingTemplatesKept}</p>

              <div className="row row--wrap">
                {!confirming ? (
                  <button
                    type="button"
                    className="btn btn--danger"
                    disabled={selected.size === 0 || purge.isPending}
                    onClick={() => setConfirming(true)}
                  >
                    {zh.p13.stagingPurge}
                  </button>
                ) : (
                  <>
                    <span className="small">{zh.p13.stagingPurgeConfirm(selected.size)}</span>
                    <button
                      type="button"
                      className="btn btn--danger"
                      disabled={purge.isPending}
                      onClick={() => purge.mutate([...selected])}
                    >
                      {zh.p13.stagingPurgeConfirmYes}
                    </button>
                    <button type="button" className="btn" onClick={() => setConfirming(false)}>
                      {zh.app.cancel}
                    </button>
                  </>
                )}
              </div>
              {selected.size === 0 && !confirming && (
                <p className="faint small" style={{ marginBottom: 0 }}>
                  {zh.p13.stagingPurgeNone}
                </p>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 表单 */

function SettingsForm({ view }: { view: SettingsView }): ReactNode {
  const client = useQueryClient();
  const formId = useId();
  const [draft, setDraft] = useState<Draft>(() => draftFrom(view));
  const [formError, setFormError] = useState<string | null>(null);
  const [probeRequested, setProbeRequested] = useState(false);

  const mutable = useMemo(() => new Set(view.mutable_fields ?? []), [view.mutable_fields]);
  const can = (key: string): boolean => mutable.has(key);

  const health = useQuery({
    queryKey: ["model-health"],
    queryFn: systemApi.modelHealth,
    enabled: probeRequested,
    retry: false,
  });

  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) => systemApi.updateSettings(patch),
    onSuccess: (data) => {
      // 服务端回的才是"现在的值"（它可能做过归一化），故直接落缓存并重置草稿。
      client.setQueryData(["settings"], data);
      setDraft(draftFrom(data));
      setFormError(null);
    },
  });

  function patchFrom(base: SettingsView, next: Draft): Record<string, unknown> {
    const original = draftFrom(base);
    const patch: Record<string, unknown> = {};
    if (can(KEYS.deployment) && next.deployment !== original.deployment) {
      patch[KEYS.deployment] = next.deployment;
    }
    if (can(KEYS.modelEnabled) && next.model_enabled !== original.model_enabled) {
      patch[KEYS.modelEnabled] = next.model_enabled;
    }
    if (can(KEYS.modelBaseUrl) && next.model_base_url !== original.model_base_url) {
      patch[KEYS.modelBaseUrl] = next.model_base_url.trim() === "" ? null : next.model_base_url.trim();
    }
    if (can(KEYS.modelName) && next.model_name !== original.model_name) {
      patch[KEYS.modelName] = next.model_name.trim() === "" ? null : next.model_name.trim();
    }
    if (can(KEYS.allowedSuffixes) && next.allowed_upload_suffixes !== original.allowed_upload_suffixes) {
      patch[KEYS.allowedSuffixes] = splitSuffixes(next.allowed_upload_suffixes);
    }
    const numeric: ReadonlyArray<readonly [string, string, string]> = [
      [KEYS.maxUploadBytes, next.max_upload_bytes, original.max_upload_bytes],
      [KEYS.retentionUploads, next.retention_uploads_days, original.retention_uploads_days],
      [KEYS.retentionWorkspace, next.retention_workspace_days, original.retention_workspace_days],
      [KEYS.retentionArtifacts, next.retention_artifacts_days, original.retention_artifacts_days],
      [KEYS.workerConcurrency, next.worker_concurrency, original.worker_concurrency],
    ];
    for (const [key, raw, before] of numeric) {
      if (!can(key) || raw === before) continue;
      const parsed = toNumber(raw);
      if (parsed === null) {
        setFormError(`「${key}」需要一个数字，当前填的是「${raw}」。`);
        continue;
      }
      patch[key] = parsed;
    }
    return patch;
  }

  function submit(): void {
    setFormError(null);
    const patch = patchFrom(view, draft);
    for (const [, raw] of Object.entries(draft).filter(([key]) =>
      key.endsWith("_days") || key === "max_upload_bytes" || key === "worker_concurrency",
    )) {
      if (typeof raw === "string" && raw.trim() !== "" && toNumber(raw) === null) {
        setFormError(`「${raw}」不是一个数字。`);
        return;
      }
    }
    if (Object.keys(patch).length === 0) {
      setFormError(zh.p13.noChanges);
      return;
    }
    save.mutate(patch);
  }

  return (
    <>
      <BoundaryCard
        boundary={view.data_boundary}
        deterministic={view.deterministic}
        modelHealth={health.data}
      />

      <DeploymentCard />

      <CapabilitiesCard />

      <div className="card">
        <h2>{zh.p13.modelTitle}</h2>
        <Field
          label={zh.p13.modelEnabled}
          configKey={KEYS.modelEnabled}
          mutable={can(KEYS.modelEnabled)}
          readOnlyText={String(view.model.enabled)}
          hint="关掉后走确定性模式：低置信决策点取规则默认值，构建不会失败（NFR-13）。"
        >
          <label className="row small" style={{ width: "auto" }}>
            <input
              type="checkbox"
              id={`${formId}-model-enabled`}
              checked={draft.model_enabled}
              onChange={(event) =>
                setDraft({ ...draft, model_enabled: event.target.checked })
              }
            />
            {zh.p13.modelEnabled}
          </label>
        </Field>

        <Field
          label={zh.p13.modelBaseUrl}
          configKey={KEYS.modelBaseUrl}
          mutable={can(KEYS.modelBaseUrl)}
          readOnlyText={view.model.base_url ?? zh.app.none}
          hint="留空表示不配置端点。"
        >
          <input
            type="text"
            value={draft.model_base_url}
            placeholder="http://127.0.0.1:11434/v1"
            onChange={(event) => setDraft({ ...draft, model_base_url: event.target.value })}
          />
        </Field>

        <Field
          label={zh.p13.modelName}
          configKey={KEYS.modelName}
          mutable={can(KEYS.modelName)}
          readOnlyText={view.model.name ?? zh.app.none}
        >
          <input
            type="text"
            value={draft.model_name}
            onChange={(event) => setDraft({ ...draft, model_name: event.target.value })}
          />
        </Field>

        <div className="form-row">
          <span className="form-row__label">
            {zh.p13.modelApiKey} <span className="tag tag--neutral">{zh.p13.readonlyTag}</span>
          </span>
          <div>
            <div className="mono">{zh.app.none}</div>
            <p className="faint small" style={{ margin: 0 }}>
              {zh.p13.modelApiKeyNote}
            </p>
          </div>
        </div>

        <div className="row row--wrap" style={{ marginTop: 8 }}>
          <button type="button" className="btn" onClick={() => setProbeRequested(true)}>
            {zh.p13.modelProbe}
          </button>
          {health.isFetching && <span className="muted small">{zh.app.loading}</span>}
          {health.isError && <span className="small">{zh.p13.modelProbeUnreachable}</span>}
          {health.isSuccess && !health.isFetching && (
            <span className="small muted">
              {zh.p13.modelProbeDetail}：
              {health.data.reachable === null || health.data.reachable === undefined
                ? zh.p13.modelProbeDisabled
                : health.data.reachable
                  ? zh.p13.modelProbeReachable
                  : zh.p13.modelProbeUnreachable}
            </span>
          )}
        </div>
      </div>

      <div className="card">
        <h2>{zh.p13.uploadTitle}</h2>
        <Field
          label={zh.p13.uploadMaxBytes}
          configKey={KEYS.maxUploadBytes}
          mutable={can(KEYS.maxUploadBytes)}
          readOnlyText={String(view.uploads.max_bytes)}
        >
          <input
            type="number"
            value={draft.max_upload_bytes}
            onChange={(event) => setDraft({ ...draft, max_upload_bytes: event.target.value })}
          />
        </Field>

        <Field
          label={zh.p13.uploadSuffixes}
          configKey={KEYS.allowedSuffixes}
          mutable={can(KEYS.allowedSuffixes)}
          readOnlyText={view.uploads.allowed_suffixes.join(", ")}
          hint={zh.p13.uploadSuffixesHint}
        >
          <input
            type="text"
            value={draft.allowed_upload_suffixes}
            placeholder=".docx,.pdf"
            onChange={(event) =>
              setDraft({ ...draft, allowed_upload_suffixes: event.target.value })
            }
          />
        </Field>
      </div>

      <div className="card">
        <h2>{zh.p13.retentionTitle}</h2>
        <Field
          label={zh.p13.retentionUploads}
          configKey={KEYS.retentionUploads}
          mutable={can(KEYS.retentionUploads)}
          readOnlyText={String(view.retention.uploads_days)}
        >
          <input
            type="number"
            value={draft.retention_uploads_days}
            onChange={(event) =>
              setDraft({ ...draft, retention_uploads_days: event.target.value })
            }
          />
        </Field>
        <Field
          label={zh.p13.retentionWorkspace}
          configKey={KEYS.retentionWorkspace}
          mutable={can(KEYS.retentionWorkspace)}
          readOnlyText={String(view.retention.workspace_days)}
        >
          <input
            type="number"
            value={draft.retention_workspace_days}
            onChange={(event) =>
              setDraft({ ...draft, retention_workspace_days: event.target.value })
            }
          />
        </Field>
        <Field
          label={zh.p13.retentionArtifacts}
          configKey={KEYS.retentionArtifacts}
          mutable={can(KEYS.retentionArtifacts)}
          readOnlyText={String(view.retention.artifacts_days)}
        >
          <input
            type="number"
            value={draft.retention_artifacts_days}
            onChange={(event) =>
              setDraft({ ...draft, retention_artifacts_days: event.target.value })
            }
          />
        </Field>
      </div>

      <div className="card">
        <h2>{zh.p13.advancedTitle}</h2>
        <Field
          label={zh.p13.workerConcurrency}
          configKey={KEYS.workerConcurrency}
          mutable={can(KEYS.workerConcurrency)}
          readOnlyText={String(view.workers.concurrency)}
        >
          <input
            type="number"
            value={draft.worker_concurrency}
            onChange={(event) => setDraft({ ...draft, worker_concurrency: event.target.value })}
          />
        </Field>
      </div>

      <div className="card">
        <h2>{zh.p13.aboutTitle}</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          {zh.p13.mutableNote}
        </p>
        <ul className="small mono">
          {(view.mutable_fields ?? []).map((field) => (
            <li key={field}>{field}</li>
          ))}
        </ul>
        {view.data_boundary.deployment !== undefined && (
          <KeyValues
            items={[
              { label: zh.generic.objectId, value: view.data_boundary.format_version },
              { label: zh.p13.deployment, value: DEPLOYMENT_LABEL[view.data_boundary.deployment] },
            ]}
          />
        )}
        <p className="faint small" style={{ marginBottom: 0 }}>
          {zh.p13.envNote}
        </p>
      </div>

      {formError !== null && (
        <p className="small" role="alert">
          <span className="tag tag--warn">{formError}</span>
        </p>
      )}
      {save.isError && <ErrorPanel error={save.error} />}
      {save.isSuccess && <p className="small tag tag--ok">{zh.p13.saved}</p>}

      <div className="row row--wrap">
        <button
          type="button"
          className="btn btn--primary"
          disabled={save.isPending}
          onClick={submit}
        >
          {zh.p13.saveSettings}
        </button>
        <button type="button" className="btn" onClick={() => setDraft(draftFrom(view))}>
          {zh.app.cancel}
        </button>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function P13Settings(): ReactNode {
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: systemApi.settings,
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p13.title}</h1>
          <p>{zh.p13.subtitle}</p>
        </div>
      </div>

      {settings.isPending && <Loading />}
      {settings.isError && <ErrorPanel error={settings.error} />}
      {settings.isSuccess && <SettingsForm view={settings.data} />}

      <ExpiredCard />

      <StagingCard />

      <div className="card card--quiet">
        <h2>{zh.p13.deployment}</h2>
        <ul className="small muted" style={{ marginBottom: 0 }}>
          {DEPLOYMENTS.map((item) => (
            <li key={item}>
              <span className="mono">{item}</span>：{DEPLOYMENT_LABEL[item]}
            </li>
          ))}
        </ul>
      </div>
    </>
  );
}
