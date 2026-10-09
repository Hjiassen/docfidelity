/**
 * P02 项目总览 —— 对应需求 **FR-M7-01 / NFR-21**；入口 `/projects/:projectId`（站点地图 7.3.2）。
 *
 * 本页只做一件事：**一屏回答"这个项目的文档现在处于什么状态、我下一步该做什么"**。
 *
 * 一屏数据来自一个端点
 * -------------------
 * `GET /projects/{pid}/overview` 一次给出指标 / 三步引导 / 待办 Top N / 最近作业 /
 * 前置检查。让界面拉四份列表自己数，等于把"什么算完成一步""最近一次构建算不算成功"
 * 这些**业务结论**摊到浏览器里（NFR-22），而且每加一张卡片就多一处口径。
 *
 * 数据边界条是本页的第一块，不是附注
 * ------------------------------
 * 需求 P02 区域 1 明确要求置顶给出"部署位置 / 模型开关 / 是否存在外发请求"。
 * 它取自与设置页**同一份** `settings` 数据（同一个 queryKey，故两页共享缓存，
 * 也永远不会互相矛盾）。三行"什么会离开本机"的文案与设置页**同源**
 * （`zh.p13.boundary*`）：那段话是部署事实的表述，两处各写一份必然漂移。
 *
 * 「发起构建」是导航，不做禁用
 * ------------------------
 * 需求 P02 的"三步未完成时不显示发起构建为可用"，本页按**导航**实现：那个按钮只是跳到
 * 作业页，真正能不能发起由 `POST /config/validate`（服务端唯一判据）决定，界面照抄
 * 它的 `ok` 与 `issues`。理由有两条，都是硬的：
 * ① 让第三步打勾的正是"发起构建"本身——按三步禁用会自相矛盾；
 * ② 硬约束 15：服务端提交时不拒绝的情形，界面不得自行禁用。
 *
 * 缺什么就说什么
 * ------------
 * `has_config=false` 时指标与引导没有意义（没有配置就没有可产出的文档），此时给
 * 「去文档配置」；`precheck=null` 同理（没有配置就没有可检查的对象）。这两种"没有"
 * 都单独说出来，不折成一张全零的指标卡——那是硬规则 11 要挡的伪装。
 *
 * 非目标：不做仪表盘式图表统计（需求 P02 明列）。
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { projectsApi, systemApi } from "../api/endpoints";
import type {
  DataBoundary,
  InboxItemOut,
  LatestJobOut,
  OverviewStepOut,
  ProjectOverviewOut,
} from "../api/types";
import { routes } from "../app/routes";
import { JobStatusTag, SeverityTag, severityOf } from "../components/StatusTag";
import { EmptyState, ErrorPanel, KeyValues, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";

/**
 * 三步引导的标签：`key` 在契约里是 `str`（服务端刻意留成开放字符串，加第四步时
 * 不必改载荷形状），故这里用**查表 + 原值兜底**而不是穷尽 `switch`。
 *
 * 兜底显示服务端给的原值，而不是编一个"未知"：多出来的一步恰恰是最该被看见的那种。
 */
const STEP_NAMES: Record<string, string> = zh.p02.stepNames;

function stepLabel(key: string): string {
  return STEP_NAMES[key] ?? key;
}

/** 与设置页同源的三条部署位置文案（值来自同一批 `zh.p13.*` 键）。 */
const DEPLOYMENT_LABEL: Record<DataBoundary["deployment"], string> = {
  local: zh.p13.deploymentLocal,
  intranet: zh.p13.deploymentIntranet,
  public: zh.p13.deploymentPublic,
};

/* ------------------------------------------------------------- 区域 1 数据边界 */

function BoundaryBar(): ReactNode {
  const settings = useQuery({ queryKey: ["settings"], queryFn: systemApi.settings });

  if (settings.isPending) return <Loading what={zh.p02.boundaryTitle} />;
  if (settings.isError) {
    return (
      <div className="card card--quiet">
        <h2>{zh.p02.boundaryTitle}</h2>
        <p className="small" style={{ marginBottom: 0 }}>
          {zh.p02.boundaryLoadFailed}
        </p>
        <ErrorPanel error={settings.error} />
      </div>
    );
  }

  const boundary = settings.data.data_boundary;
  return (
    <div className="card card--quiet">
      <div className="row row--wrap" style={{ marginBottom: 4 }}>
        <h2 style={{ margin: 0 }}>{zh.p02.boundaryTitle}</h2>
        <Link className="btn btn--sm" to={routes.p13Settings}>
          {zh.p02.boundaryMore}
        </Link>
      </div>
      <KeyValues
        items={[
          { label: zh.p02.boundaryDeployment, value: DEPLOYMENT_LABEL[boundary.deployment] },
          {
            label: zh.p02.boundaryModel,
            value: boundary.model_enabled ? zh.p02.boundaryModelOn : zh.p02.boundaryModelOff,
          },
          {
            label: zh.p02.boundaryExternal,
            value: boundary.external_calls
              ? zh.p02.boundaryExternalYes
              : zh.p02.boundaryExternalNo,
          },
          // 存储位置也要在一屏里看得见：它是"数据落在哪"这个问题的**唯一**答案，
          // 而"部署位置就是数据边界"这句话没有它就只是一句口号。
          { label: zh.p02.boundaryStorage, value: boundary.storage_path ?? zh.app.none },
        ]}
      />
      <div className="grid grid--cards" style={{ marginTop: 8 }}>
        <div>
          <h3 className="small">{zh.p02.boundaryLeaves}</h3>
          <ul className="small muted" style={{ marginBottom: 0 }}>
            {zh.p13.boundaryLeavesList.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="small">{zh.p02.boundaryStays}</h3>
          <ul className="small muted" style={{ marginBottom: 0 }}>
            {zh.p13.boundaryStaysList.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- 区域 2 指标卡 */

function MetricCard({
  label,
  value,
  to,
}: {
  label: string;
  value: number;
  to?: string;
}): ReactNode {
  const body = (
    <>
      <div className="metric__value">{value}</div>
      <div className="faint small">{label}</div>
    </>
  );
  return to === undefined ? (
    <div className="metric">{body}</div>
  ) : (
    <Link className="metric" to={to}>
      {body}
    </Link>
  );
}

function MetricsRow({ overview, projectId }: { overview: ProjectOverviewOut; projectId: string }): ReactNode {
  const metrics = overview.metrics;
  return (
    <>
      <div className="grid grid--cards">
        <MetricCard
          label={zh.p02.metricDocuments}
          value={metrics.documents}
          to={routes.p07Config(projectId)}
        />
        <MetricCard label={zh.p02.metricTemplates} value={metrics.templates} to={routes.p03Templates} />
        <MetricCard label={zh.p02.metricSources} value={metrics.sources} to={routes.p06Sources(projectId)} />
        <MetricCard
          label={zh.p02.metricArtifacts}
          value={metrics.artifacts}
          to={routes.p11Artifacts(projectId)}
        />
        <MetricCard label={zh.p02.metricPending} value={metrics.pending_decisions} to={routes.p12Inbox} />
      </div>
      <p className="faint small">{zh.p02.metricsNote}</p>
    </>
  );
}

/* --------------------------------------------------- 状态与空态：三步引导 + 快速入口 */

function StepRow({ step }: { step: OverviewStepOut }): ReactNode {
  return (
    <li>
      <span className={step.done ? "tag tag--ok" : "tag tag--warn"}>
        {step.done ? zh.p02.stepDone : zh.p02.stepTodo}
      </span>{" "}
      {stepLabel(step.key)}
      {step.hint !== null && step.hint !== undefined && step.hint !== "" && (
        <div className="faint small">{step.hint}</div>
      )}
    </li>
  );
}

function StepsCard({ overview, projectId }: { overview: ProjectOverviewOut; projectId: string }): ReactNode {
  return (
    <div className="card">
      <h2>{zh.p02.stepsTitle}</h2>
      <ul className="small" style={{ marginBottom: 8 }}>
        {overview.steps.map((step) => (
          <StepRow key={step.key} step={step} />
        ))}
      </ul>
      <p className="faint small">{zh.p02.stepsNote}</p>

      <h3>{zh.p02.quickTitle}</h3>
      <div className="row row--wrap">
        <Link className="btn btn--sm" to={routes.p05TemplateImport}>
          {zh.p02.quickImport}
        </Link>
        <Link className="btn btn--sm" to={routes.p06Sources(projectId)}>
          {zh.p02.quickSources}
        </Link>
        <Link className="btn btn--sm btn--primary" to={routes.p08Jobs(projectId)}>
          {zh.p02.quickBuild}
        </Link>
      </div>
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p02.quickNote}
      </p>
    </div>
  );
}

/* --------------------------------------------------------------- 前置检查 */

function PrecheckCard({ overview, projectId }: { overview: ProjectOverviewOut; projectId: string }): ReactNode {
  const report = overview.precheck;
  return (
    <div className="card">
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>{zh.p02.precheckTitle}</h2>
        </div>
        <div className="page-head__actions">
          <Link className="btn btn--sm" to={routes.p08Jobs(projectId)}>
            {zh.p02.precheckGo}
          </Link>
        </div>
      </div>

      {report === null ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p02.precheckNoConfig}
        </p>
      ) : (
        <>
          {report.ok ? (
            <p className="small" style={{ color: "var(--ok)" }}>
              {zh.p02.precheckOk}
            </p>
          ) : (
            <>
              <p className="small" style={{ color: "var(--danger)" }}>
                {zh.p02.precheckBlocked}
              </p>
              <ul className="small">
                {report.issues.map((issue) => (
                  <li key={`${issue.kind}-${issue.message}`}>
                    {issue.message}
                    {issue.remediation.length > 0 && (
                      <div className="faint small">{issue.remediation.join("；")}</div>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
          {/* 跳过项**始终**要出现：跳过不等于通过（硬规则 11）。 */}
          {report.skipped.length > 0 && (
            <>
              <p className="faint small" style={{ marginBottom: 0 }}>
                {zh.p02.precheckSkipped}
              </p>
              <ul className="faint small mono" style={{ margin: "4px 0 0 0" }}>
                {report.skipped.map((check) => (
                  <li key={check}>{check}</li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </div>
  );
}

/* --------------------------------------------------------- 区域 3 待裁决待办 */

function PendingRow({ item }: { item: InboxItemOut }): ReactNode {
  const severity = severityOf(item.severity);
  return (
    <tr>
      <td className="mono small">{item.project_id}</td>
      <td>
        {item.title}
        {item.detail !== null && item.detail !== undefined && item.detail !== "" && (
          <div className="faint small">{item.detail}</div>
        )}
      </td>
      <td>
        {severity === null ? (
          <span className="faint">{zh.app.none}</span>
        ) : (
          <SeverityTag severity={severity} />
        )}
      </td>
      <td className="small muted">{formatTime(item.created_at)}</td>
      <td>
        <Link className="btn btn--sm" to={routes.p09JobDetail(item.project_id, item.job_id)}>
          {zh.p02.pendingOpenJob}
        </Link>
      </td>
    </tr>
  );
}

function PendingCard({ overview }: { overview: ProjectOverviewOut }): ReactNode {
  const items = overview.pending;
  return (
    <div className="card">
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>{zh.p02.pendingTitle}</h2>
        </div>
        <div className="page-head__actions">
          <span className="muted small">{zh.p02.pendingTotal(overview.pending_total)}</span>
          <Link className="btn btn--sm" to={routes.p12Inbox}>
            {zh.p02.pendingAll}
          </Link>
        </div>
      </div>

      {items.length === 0 ? (
        <EmptyState title={zh.p02.pendingEmpty} body={zh.p02.pendingEmptyNote} />
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="mono">project_id</th>
                  <th>{zh.p12.colItem}</th>
                  <th>{zh.p12.colSeverity}</th>
                  <th>{zh.p12.colCreatedAt}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <PendingRow key={item.subject_id} item={item} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="faint small" style={{ marginBottom: 0 }}>
            {zh.p02.pendingShowing(items.length, overview.pending_total)}
          </p>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------- 区域 4 最近作业 */

function LastJobCard({ last, projectId }: { last: LatestJobOut; projectId: string }): ReactNode {
  const job = last.job;
  const progress = last.progress;
  return (
    <div className="card">
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>{zh.p02.lastJobTitle}</h2>
        </div>
        <div className="page-head__actions">
          <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, job.job_id)}>
            {zh.p02.lastJobOpen}
          </Link>
        </div>
      </div>

      <p className="mono small" style={{ marginTop: 0 }}>
        {job.job_id} <JobStatusTag status={job.status} />
      </p>
      <KeyValues
        items={[
          { label: zh.p02.lastJobKind, value: zh.job.kind[job.kind] },
          {
            label: zh.p02.lastJobProgress,
            value:
              progress === null
                ? zh.app.none
                : zh.p08.childProgressDone(progress.done, progress.total),
          },
          { label: zh.p02.lastJobCurrent, value: progress?.current_child ?? null },
          { label: zh.p02.lastJobArtifacts, value: last.artifact_count },
          { label: zh.p02.lastJobCreatedAt, value: formatTime(job.created_at) },
          { label: zh.p02.lastJobBy, value: job.initiated_by ?? null },
        ]}
      />
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p02.lastJobNote}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function P02Overview(): ReactNode {
  const { projectId = "" } = useParams<{ projectId: string }>();

  const overview = useQuery({
    queryKey: ["overview", projectId],
    queryFn: () => projectsApi.overview(projectId),
    enabled: projectId !== "",
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p02.title}</h1>
          <p>{zh.p02.subtitle}</p>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn--sm" onClick={() => void overview.refetch()}>
            {zh.app.refresh}
          </button>
        </div>
      </div>

      <BoundaryBar />

      {overview.isPending && <Loading />}
      {overview.isError && <ErrorPanel error={overview.error} />}

      {overview.data !== undefined && (
        <>
          <MetricsRow overview={overview.data} projectId={projectId} />

          {!overview.data.has_config && (
            <EmptyState
              title={zh.p02.configMissingTitle}
              body={zh.p02.configMissingNote}
              action={
                <Link className="btn btn--primary" to={routes.p07Config(projectId)}>
                  {zh.p02.configMissingAction}
                </Link>
              }
            />
          )}

          <StepsCard overview={overview.data} projectId={projectId} />
          <PrecheckCard overview={overview.data} projectId={projectId} />
          <PendingCard overview={overview.data} />

          {overview.data.last_job === null ? (
            <EmptyState
              title={zh.p02.lastJobTitle}
              body={zh.p02.lastJobNone}
              action={
                <Link className="btn btn--primary" to={routes.p08Jobs(projectId)}>
                  {zh.p02.lastJobNoneAction}
                </Link>
              }
            />
          ) : (
            <LastJobCard last={overview.data.last_job} projectId={projectId} />
          )}
        </>
      )}
    </>
  );
}
