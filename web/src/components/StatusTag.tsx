/**
 * 状态标签：作业状态 / 阶段状态 / 级别 / 门禁结论 / 产物种类。
 *
 * 7.6.3 的硬约束：**颜色不作唯一区分，级别一律带文字标签**。
 * 所以这里的每个标签都同时给出文字（`zh.*`）与颜色；只上色不上字的写法在本项目里
 * 等于把信息编码进颜色，色觉差异与黑白打印都会直接丢失信息。
 *
 * 另一条：标签只做"翻译"（枚举值 → 中文），**不做判断**。哪个状态算"坏"由服务端
 * 决定，这里只是把服务端给的取值渲染出来。
 */

import type { ReactNode } from "react";

import { zh } from "../i18n/zh";
import type {
  ArtifactKind,
  FindingStatus,
  JobStatus,
  Severity,
  StageStatus,
  Verdict,
} from "../api/types";

type Tone = "neutral" | "info" | "ok" | "warn" | "danger";

function Tag({ tone, children }: { tone: Tone; children: ReactNode }): ReactNode {
  return <span className={`tag tag--${tone}`}>{children}</span>;
}

/* ------------------------------------------------------------- 作业状态 */
const JOB_STATUS_TONE: Record<JobStatus, Tone> = {
  queued: "neutral",
  running: "info",
  awaiting_decision: "warn",
  succeeded: "ok",
  failed: "danger",
  partial_failure: "warn",
  canceled: "neutral",
};

export function JobStatusTag({ status }: { status: JobStatus }): ReactNode {
  return <Tag tone={JOB_STATUS_TONE[status]}>{zh.job.status[status]}</Tag>;
}

/* ------------------------------------------------------------- 阶段状态 */
const STAGE_STATUS_TONE: Record<StageStatus, Tone> = {
  pending: "neutral",
  running: "info",
  succeeded: "ok",
  failed: "danger",
  skipped: "neutral",
};

export function StageStatusTag({ status }: { status: StageStatus }): ReactNode {
  return <Tag tone={STAGE_STATUS_TONE[status]}>{zh.job.stageStatus[status]}</Tag>;
}

/* ----------------------------------------------------------------- 级别 */
const SEVERITY_TONE: Record<Severity, Tone> = {
  blocker: "danger",
  major: "warn",
  minor: "neutral",
};

export function SeverityTag({ severity }: { severity: Severity }): ReactNode {
  return <Tag tone={SEVERITY_TONE[severity]}>{zh.severity[severity]}</Tag>;
}

/**
 * 把契约里的 `string | null` 收窄到已知级别。
 *
 * 两处用它（P12 的待办表、P02 的待办 Top N），故放在这里而不是各写一份：
 * 判据是"级别集合是契约闭集"，而 `null` 表示**这一条没有级别**（决策类待办就是如此，
 * 它不由门禁规则产生），不是"级别未知"。收窄成 `null` 而不是强转，是为了让服务端
 * 多出一个级别时界面显示"—"，而不是让 `SeverityTag` 去查一张没有那个键的表。
 */
export function severityOf(severity: string | null | undefined): Severity | null {
  return severity === "blocker" || severity === "major" || severity === "minor"
    ? severity
    : null;
}

/* ----------------------------------------------------------- 条目状态 */
const FINDING_STATUS_TONE: Record<FindingStatus, Tone> = {
  open: "warn",
  waived: "neutral",
  fixed: "ok",
};

export function FindingStatusTag({ status }: { status: FindingStatus }): ReactNode {
  return <Tag tone={FINDING_STATUS_TONE[status]}>{zh.findingStatus[status]}</Tag>;
}

/* ----------------------------------------------------------- 门禁结论 */
const VERDICT_TONE: Record<Verdict, Tone> = {
  pass: "ok",
  warning: "warn",
  blocked: "danger",
};

export function VerdictTag({ verdict }: { verdict: Verdict }): ReactNode {
  return <Tag tone={VERDICT_TONE[verdict]}>{zh.verdict[verdict]}</Tag>;
}

/* ------------------------------------------------------------- 产物种类 */
export function ArtifactKindTag({ kind }: { kind: ArtifactKind }): ReactNode {
  return <Tag tone="neutral">{zh.artifactKind[kind]}</Tag>;
}
