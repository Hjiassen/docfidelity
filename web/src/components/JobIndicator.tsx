/**
 * 作业指示器（7.5.1）。
 *
 * **不可隐藏**：长任务若没有指示器，使用者会重复提交——这是作业化系统最常见的
 * 资源浪费。所以"没有进行中的作业"也要占位显示，而不是整块消失。
 *
 * 内容口径（7.5.1）：
 * - 编排作业：`子作业 3/5 完成 · 当前：详细设计`
 * - 子作业：`渲染注入 · 62%`
 *
 * 计数与"当前是谁"都来自服务端的 `ChildProgress`，前端不自己数子作业
 * （NFR-22：界面不做业务判断；聚合规则唯一来源是服务端）。
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { jobsApi } from "../api/endpoints";
import type { Job } from "../api/types";
import { zh } from "../i18n/zh";
import { routes } from "../app/routes";

/** 轻量刷新：SSE 只覆盖"正在看的那个作业"，全局指示器靠低频轮询兜住其它作业。 */
const POLL_MS = 15_000;

function describe(job: Job): string {
  // `progress` 在契约里是 `ChildProgress | None`，生成类型因此是 `| undefined`。
  // 折成 `null` 之后，"没有进度"只剩一种表示，下面的分支也就不必两处都判。
  const progress = job.progress ?? null;
  if (job.kind === "orchestration") {
    const done = progress?.done ?? 0;
    const total = progress?.total ?? job.child_job_ids.length;
    return zh.job.indicatorOrchestration(done, total, progress?.current_child ?? null);
  }
  const stage = progress?.current_child ?? null;
  // 分母为 0 时**不给百分比**：显示 "0%" 会被读成"刚开始"，而真相是"没有可计量单位"。
  if (progress === null || progress.total <= 0) {
    return stage ?? zh.job.kind.document;
  }
  return zh.job.indicatorChild(
    stage ?? zh.job.kind.document,
    Math.round((progress.done / progress.total) * 100),
  );
}

export function JobIndicator(): ReactNode {
  const { data } = useQuery({
    queryKey: ["jobs", "running"],
    queryFn: () => jobsApi.list("running"),
    refetchInterval: POLL_MS,
  });

  const job = data?.items[0];
  if (job === undefined) {
    return (
      <span className="job-indicator" title={zh.job.indicatorIdle}>
        {zh.job.indicatorIdle}
      </span>
    );
  }

  return (
    <Link className="job-indicator" to={routes.p09JobDetail(job.project_id, job.job_id)}>
      <i className="job-indicator__pulse" aria-hidden="true" />
      <span>{describe(job)}</span>
    </Link>
  );
}
