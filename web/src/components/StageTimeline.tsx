/**
 * 阶段时间线（7.5 / P09）。
 *
 * 一行一个阶段：状态、起止时间、耗时、**是否留有断点**。
 *
 * "是否有断点"必须显式标出：它决定了"重跑"会不会真的重跑。没有断点的阶段重跑时
 * 必然执行；有断点且输入未变的阶段会被直接复用（FR-M7-03）。界面若不给这一列，
 * 使用者会对"点了重跑但看起来秒过"产生怀疑——而那其实是正常行为。
 */

import type { ReactNode } from "react";

import type { Stage, StageName } from "../api/types";
import { isAbsent } from "../api/types";
import { zh } from "../i18n/zh";
import { formatDuration, formatTime } from "./primitives";
import { StageStatusTag } from "./StatusTag";

export interface StageTimelineProps {
  stages: readonly Stage[];
  /** 允许"从本阶段续跑"时才给（已完成/跳过的阶段不给，避免无意义的动作）。 */
  onResumeFrom?: (stage: StageName) => void;
  /** 正在提交续跑请求时禁用按钮。 */
  busy?: boolean;
  /**
   * 没有阶段时的说明。
   *
   * 必须由调用方给：**空列表有两种截然不同的原因**——"编排作业本身不跑阶段"
   * （阶段在子作业里）与"作业刚发起、阶段还没落"（等一会儿就会有）。用一句通用文案
   * 盖住两者，会让人对着一个编排作业反复刷新。
   */
  emptyHint: string;
  /** 空态的动作（14.5：空态必须给下一步）。 */
  emptyAction?: ReactNode;
}

export function StageTimeline({
  stages,
  onResumeFrom,
  busy = false,
  emptyHint,
  emptyAction,
}: StageTimelineProps): ReactNode {
  if (stages.length === 0) {
    return (
      <div className="empty">
        <h3>{zh.job.stagesEmpty}</h3>
        <p>{emptyHint}</p>
        {emptyAction}
      </div>
    );
  }

  return (
    <ul className="timeline">
      {stages.map((stage) => (
        <li key={stage.name}>
          <span
            className={`timeline__dot timeline__dot--${stage.status}`}
            aria-hidden="true"
            title={zh.job.stageStatus[stage.status]}
          />
          <span className="strong">{zh.job.stage[stage.name]}</span>
          <span className="row row--wrap small">
            <StageStatusTag status={stage.status} />
            <span className="faint">
              {formatTime(stage.started_at)} → {formatTime(stage.finished_at)}
            </span>
            <span className="muted">{formatDuration(stage.started_at, stage.finished_at)}</span>
            <span className={isAbsent(stage.checkpoint) ? "faint" : "muted"}>
              {isAbsent(stage.checkpoint) ? zh.job.checkpointNo : zh.job.checkpointYes}
            </span>
            {!isAbsent(stage.error) && (
              <span className="tag tag--danger">{stage.error.message}</span>
            )}
          </span>
          <span>
            {onResumeFrom !== undefined && stage.status !== "succeeded" && stage.status !== "skipped" && (
              <button
                type="button"
                className="btn btn--sm"
                disabled={busy}
                onClick={() => onResumeFrom(stage.name)}
              >
                {zh.p09.resumeFromStage}
              </button>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}
