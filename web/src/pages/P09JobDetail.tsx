/**
 * P09 作业详情 —— 对应需求 **FR-M7-03 / FR-M7-04 / FR-M7-15 / FR-M7-16 / NFR-19 / 4.8**；
 * 入口 `/projects/:pid/jobs/:jid`（站点地图 7.3.2）。
 *
 * 三条不能破的约定
 * ----------------
 * ① **聚合状态由服务端给**：编排作业的"到哪一步了"用 `JobDetail.progress`，
 *    前端**不数**子作业（7.8 非目标 / NFR-22）。多份文档时"数错了"不会报错，
 *    只会让人按一个错的进度去判断要不要等。
 * ② **续跑提示用服务端的 `resume_plan`**：界面上那句"将从 X 阶段继续"不是前端
 *    按状态猜的，而是 `GET /jobs/{jid}` 里算好的。
 * ③ **事件流断线必须可见**：长任务里"界面没反应"和"任务卡住"无法区分是最糟糕的
 *    体验，所以连接状态与心跳永远显示（`EventStream` 的右上角标签）。
 *
 * 状态刷新策略（DD-14.3）
 * ----------------------
 * 阶段进度以 SSE 事件为主；作业**整体**状态另用 3s 轮询兜底——SSE 只推"发生了什么"，
 * 不重发完整状态，因此只靠事件拼不出"现在是什么状态"。终态后轮询自动停。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { downloadUrl } from "../api/client";
import { jobsApi } from "../api/endpoints";
import { isAbsent, isSettled } from "../api/types";
import type { Job, JobDetail } from "../api/types";
import { DEFAULT_EVENT_LIMIT, useJobEvents } from "../api/useJobEvents";
import { routes } from "../app/routes";
import { EventStream } from "../components/EventStream";
import { StageTimeline } from "../components/StageTimeline";
import { ArtifactKindTag, JobStatusTag } from "../components/StatusTag";
import {
  EmptyState,
  ErrorPanel,
  KeyValues,
  Loading,
  formatBytes,
  formatTime,
} from "../components/primitives";
import { zh } from "../i18n/zh";

/** 非终态时的兜底轮询间隔。 */
const POLL_MS = 3000;

function ChildMatrix({ children, projectId }: { children: readonly Job[]; projectId: string }): ReactNode {
  if (children.length === 0) {
    return <p className="muted small">{zh.p09.childMatrixEmpty}</p>;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>{zh.p09.docKey}</th>
            <th>{zh.p09.status}</th>
            <th>{zh.p09.currentStage}</th>
            <th>{zh.p09.artifacts}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {children.map((child) => (
            <tr key={child.job_id}>
              <td>
                <span className="strong">{child.doc_key ?? child.job_id}</span>
                <br />
                <span className="faint small mono">{child.job_id}</span>
              </td>
              <td>
                <JobStatusTag status={child.status} />
              </td>
              <td>{child.progress?.current_child ?? zh.app.none}</td>
              <td>
                {child.artifact_ids.length === 0 ? (
                  <span className="faint">{zh.app.none}</span>
                ) : (
                  <span className="mono small">{child.artifact_ids.length}</span>
                )}
              </td>
              <td>
                <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, child.job_id)}>
                  {zh.p09.enterChild}
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JobBody({ detail, projectId, jobId }: { detail: JobDetail; projectId: string; jobId: string }): ReactNode {
  const queryClient = useQueryClient();
  const job = detail.job;
  const settled = isSettled(job.status);
  const [eventRevision, setEventRevision] = useState(0);
  const stream = useJobEvents(jobId, DEFAULT_EVENT_LIMIT, eventRevision);

  const artifacts = useQuery({
    queryKey: ["artifacts", jobId, settled],
    queryFn: () => jobsApi.artifacts(jobId),
  });

  useEffect(() => {
    if (!stream.settled) return;
    void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    void queryClient.invalidateQueries({ queryKey: ["artifacts", jobId] });
  }, [jobId, queryClient, stream.settled]);

  const invalidate = (): void => {
    setEventRevision((previous) => previous + 1);
    void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    void queryClient.invalidateQueries({ queryKey: ["artifacts", jobId] });
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
  };

  const cancel = useMutation({ mutationFn: () => jobsApi.cancel(jobId), onSuccess: invalidate });
  const retryStage = useMutation({
    mutationFn: () => jobsApi.retry(jobId, "stage"),
    onSuccess: invalidate,
  });
  const rerunFull = useMutation({
    mutationFn: () => jobsApi.retry(jobId, "full"),
    onSuccess: invalidate,
  });
  const resumeFrom = useMutation({
    mutationFn: (stage: string) => jobsApi.resume(jobId, stage),
    onSuccess: invalidate,
  });
  const rerunFailed = useMutation({ mutationFn: () => jobsApi.rerunFailed(jobId), onSuccess: invalidate });

  const busy =
    cancel.isPending || retryStage.isPending || rerunFull.isPending || resumeFrom.isPending || rerunFailed.isPending;
  const actionError = cancel.error ?? retryStage.error ?? rerunFull.error ?? resumeFrom.error ?? rerunFailed.error;

  const failed = job.status === "failed" || job.status === "canceled" || job.status === "partial_failure";
  const artifactItems = artifacts.data ?? [];
  const firstChild: Job | undefined = detail.children[0];

  return (
    <>
      {actionError !== null && actionError !== undefined && <ErrorPanel error={actionError} />}

      <div className="card">
        <div className="page-head" style={{ marginBottom: 10 }}>
          <div>
            <h1 style={{ fontSize: 18 }}>
              {zh.p09.title} <span className="mono faint small">{job.job_id}</span>
            </h1>
            <p>
              {zh.job.kind[job.kind]}
              {job.doc_key !== null && ` · ${job.doc_key}`}
            </p>
          </div>
          <div className="page-head__actions">
            <JobStatusTag status={job.status} />
            {!settled && (
              <button type="button" className="btn btn--sm" disabled={busy} onClick={() => cancel.mutate()}>
                {zh.p09.cancelJob}
              </button>
            )}
          </div>
        </div>

        {failed && (
          <div className="banner" style={{ background: "var(--danger-weak)", borderColor: "var(--danger)" }}>
            {isAbsent(job.error) ? (
              zh.job.status[job.status]
            ) : (
              <>
                <span className="strong">{job.error.message}</span>
                {Object.keys(job.error.where ?? {}).length > 0 && (
                  <span className="mono small"> {JSON.stringify(job.error.where)}</span>
                )}
              </>
            )}
            {job.status === "partial_failure" && <div className="small">{zh.p09.partialNote}</div>}
          </div>
        )}

        {job.status === "awaiting_decision" && (
          <div className="banner">
            <strong>{zh.p09.decisionPanel}</strong>
            <div className="small">{zh.p09.awaitingNote}</div>
            <div className="row row--wrap" style={{ marginTop: 8 }}>
              <Link className="btn btn--sm btn--primary" to={routes.p10Report(projectId, jobId)}>
                {zh.p09.goDecision}
              </Link>
              <Link className="btn btn--sm" to={routes.p12Inbox}>
                {zh.nav.inbox}
              </Link>
            </div>
          </div>
        )}

        <div className="row row--wrap">
          <span className="muted small">{zh.job.resumePlan(detail.resume_plan)}</span>
          <span className="faint small">{zh.job.completedNotRerun}</span>
        </div>
      </div>

      {job.kind === "orchestration" && (
        <div className="card">
          <h2>{zh.p09.childMatrix}</h2>
          <ChildMatrix children={detail.children} projectId={projectId} />
          {failed && detail.children.some((child) => child.status === "failed") && (
            <button
              type="button"
              className="btn btn--primary"
              style={{ marginTop: 12 }}
              disabled={busy}
              onClick={() => rerunFailed.mutate()}
            >
              {zh.p09.rerunFailed}
            </button>
          )}
        </div>
      )}

      <div className="card">
        <h2>{zh.p09.stageTimeline}</h2>
        <StageTimeline
          stages={detail.stages}
          busy={busy}
          emptyHint={
            job.kind === "orchestration"
              ? zh.job.stagesEmptyOrchestration
              : zh.job.stagesEmptyDocument
          }
          emptyAction={
            job.kind === "orchestration" && firstChild !== undefined ? (
              <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, firstChild.job_id)}>
                {zh.p09.enterChild}
              </Link>
            ) : undefined
          }
          onResumeFrom={settled ? undefined : (stage) => resumeFrom.mutate(stage)}
        />
      </div>

      {failed && (
        <div className="card">
          <h2>{zh.p09.failurePanel}</h2>
          <div className="row row--wrap">
            <button type="button" className="btn btn--primary" disabled={busy} onClick={() => retryStage.mutate()}>
              {zh.p09.retryStage}
            </button>
            {detail.resume_plan.length > 0 && (
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => resumeFrom.mutate(detail.resume_plan[0] ?? "")}
              >
                {zh.p09.resumeFromStage}（{detail.resume_plan[0]}）
              </button>
            )}
            <button type="button" className="btn btn--danger" disabled={busy} onClick={() => rerunFull.mutate()}>
              {zh.p09.rerunFull}
            </button>
          </div>
          <p className="faint small">{zh.job.completedNotRerun}</p>
        </div>
      )}

      <div className="card">
        <h2>{zh.p09.eventStream}</h2>
        <EventStream
          events={stream.events}
          connected={stream.connected}
          settled={stream.settled}
          jobId={jobId}
        />
      </div>

      <div className="card">
        <h2>{zh.p09.inputSnapshot}</h2>
        <KeyValues
          items={[
            {
              label: zh.p09.inputsHash,
              value: <span className="mono small">{job.inputs_hash === "" ? zh.app.none : job.inputs_hash}</span>,
            },
            {
              label: zh.generic.createdAt,
              value: formatTime(job.created_at),
            },
          ]}
        />
      </div>

      <div className="card">
        <h2>{zh.p09.artifactQuick}</h2>
        {artifacts.isPending && <Loading />}
        {artifacts.isError && <ErrorPanel error={artifacts.error} />}
        {artifacts.isSuccess && artifactItems.length === 0 && (
          <p className="muted small">
            {zh.p09.noArtifacts}
            {job.kind === "orchestration" && zh.p09.noArtifactsOrchestration}。
          </p>
        )}
        {artifactItems.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{zh.p09.artifacts}</th>
                  <th>{zh.p09.artifactType}</th>
                  <th>{zh.p09.artifactSize}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {artifactItems.map((artifact) => (
                  <tr key={artifact.artifact_id}>
                    <td className="mono small">{artifact.relative_path}</td>
                    <td>
                      <ArtifactKindTag kind={artifact.kind} />
                    </td>
                    <td>{formatBytes(artifact.size_bytes)}</td>
                    <td>
                      <a className="btn btn--sm" href={downloadUrl(artifact.artifact_id)} download>
                        {zh.p09.download}
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

export function P09JobDetail(): ReactNode {
  const params = useParams();
  const projectId = params["projectId"] ?? "";
  const jobId = params["jobId"] ?? "";

  const detail = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => jobsApi.detail(jobId),
    enabled: jobId !== "",
    refetchInterval: (query) => (isSettled(query.state.data?.job.status) ? false : POLL_MS),
  });

  if (detail.isPending) return <Loading />;
  if (detail.isError) {
    // 取不到详情只有两种原因：id 不存在，或产物已被保留策略回收（服务端给的错误码）。
    // 两者都该走空态而不是错误面板——它不是"操作失败"，而是"这里没有东西"。
    return (
      <EmptyState
        title={zh.p09.title}
        body={zh.p09.loadFailed}
        action={
          <Link className="btn" to={routes.p08Jobs(projectId)}>
            {zh.p09.backToJobs}
          </Link>
        }
      />
    );
  }
  return <JobBody key={jobId} detail={detail.data} projectId={projectId} jobId={jobId} />;
}
