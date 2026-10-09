/**
 * P10 门禁报告与裁决 —— 对应需求 **FR-M5-14 / FR-M5-15 / FR-M5-16 / FR-M7-06**；
 * 入口 `/projects/:pid/jobs/:jid/report`（站点地图 7.3.2）。
 *
 * 本页最重要的一条：**空报告与"全通过"必须可区分**（P10 状态与空态）
 * ------------------------------------------------------------------
 * 报告缺失意味着门禁阶段没有执行（例如作业在渲染阶段就失败了），它和"检查过了、
 * 没有任何问题"是两件完全不同的事。把前者显示成"0 项未通过"，等于告诉使用者
 * "可以定稿了"——这正是本项目最不希望发生的交付事故。所以本页对
 * `E_REPORT_MISSING` 走**独立的空态文案**，绝不落进"全通过"分支。
 *
 * 裁决写在哪
 * ----------
 * 裁决走 `POST /findings/{fid}/decisions`，留痕落在**报告本体**（`Report.adjudications`）
 * 并由服务端重算结论与摘要。故本页提交完要重取报告——不重取的话，页面上那句"结论：阻断"
 * 会一直停在旧值上，而报告的留痕区已经多了一条。
 * 待裁决的候选条目与 `GET /inbox` **同一判据**：未豁免的阻断项（严重 / 轻微是信息、
 * 不阻塞交付）。判据是服务端的，这里只照列（NFR-22）。
 *
 * 本页因此有四块**只有服务端能给**的清单，各列各的、互不代替
 * ------------------------------------------------------
 * ① **待裁决条目**（未豁免的阻断项）+ 逐条的提交入口；
 * ② **裁决留痕**（`Report.adjudications`，含已办结的、以及决策项的）；
 * ③ **豁免清单**（`Report.waivers`：豁免只是三种动作之一，另有有效期与作用域）；
 * ④ **跳过的规则**（`Report.skipped`：没跑 ≠ 跑过且通过）。
 *
 * 已办结的条目**照样列在 ①** 并标注最近一次动作，只是不隐藏也不禁用：服务端允许对
 * 同一条重复裁决（后一条留痕生效），界面自行禁止就是自造一条服务端没有的规则
 * （硬约束 15）。真正的"还剩谁要看"由 ② 与 `/inbox` 回答。
 *
 * 一处降级（显式可见，不静默）
 * --------------------------
 * **按类别分组**：`Finding` 契约里没有 category 字段，故本页按**级别**分组
 * （级别是服务端给的，不需要前端推断）。
 */

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, downloadText } from "../api/client";
import { jobsApi } from "../api/endpoints";
import type { Adjudication, AdjudicationResponse, Finding, Report, Severity } from "../api/types";
import { routes } from "../app/routes";
import { AdjudicationForm, AdjudicationOutcome } from "../components/AdjudicationForm";
import { EmptyState, ErrorPanel, KeyValues, Loading, formatTime } from "../components/primitives";
import { FindingStatusTag, SeverityTag, VerdictTag } from "../components/StatusTag";
import { zh } from "../i18n/zh";

/** 报告缺失的服务端错误码（`services.get_report`）。 */
const REPORT_MISSING = "E_REPORT_MISSING";

const GROUP_ORDER: readonly Severity[] = ["blocker", "major", "minor"];

function findingsToMarkdown(report: Report): string {
  const cell = (text: string): string => text.replaceAll("|", "\\|");
  const lines = [
    `# 门禁报告 ${report.report_id}`,
    "",
    `- 作业：\`${report.job_id}\``,
    `- 生成时间：${formatTime(report.generated_at)}`,
    `- 结论：${zh.verdict[report.verdict]}`,
    report.summary === null ? "" : `- 摘要：${report.summary}`,
    "",
    "| 规则号 | 级别 | 状态 | 说明 | 定位 | 建议 |",
    "| --- | --- | --- | --- | --- | --- |",
  ];
  for (const finding of report.findings) {
    lines.push(
      [
        finding.rule_id,
        zh.severity[finding.severity],
        zh.findingStatus[finding.status],
        cell(finding.message),
        cell(describeLocation(finding)),
        cell(finding.suggestion ?? ""),
      ].join(" | "),
    );
  }

  // 裁决留痕与豁免清单都要进导出：**它们是这份报告的审计面**（谁以什么理由放行）。
  // 只导出判定表等于把"带病定稿"的痕迹留在页面上、不进留存的那一份。
  lines.push(
    "",
    `## ${zh.adjudication.ledger}`,
    "",
    `| ${zh.adjudication.subject} | ${zh.adjudication.action} | ${zh.adjudication.by} | ${zh.adjudication.reason} | ${zh.generic.createdAt} |`,
    "| --- | --- | --- | --- | --- |",
  );
  for (const item of report.adjudications) {
    lines.push(
      [
        `\`${item.subject_id}\``,
        zh.adjudication.actions[item.action],
        cell(item.operator),
        cell(item.reason ?? ""),
        formatTime(item.created_at),
      ].join(" | "),
    );
  }

  lines.push(
    "",
    `## ${zh.p10.waiversTitle}`,
    "",
    `| ${zh.p10.ruleId} | ${zh.adjudication.by} | ${zh.adjudication.reason} | ${zh.p10.waiverScopeLabel} | ${zh.p10.waiverExpiresAt} |`,
    "| --- | --- | --- | --- | --- |",
  );
  for (const waiver of report.waivers) {
    lines.push(
      [
        waiver.rule_id,
        cell(waiver.operator),
        cell(waiver.reason),
        zh.p10.waiverScopeOf[waiver.scope],
        waiver.expires_at == null || waiver.expires_at === "" ? zh.p10.waiverNoExpiry : formatTime(waiver.expires_at),
      ].join(" | "),
    );
  }

  // 「跳过」也要进：报告里没有它，读导出的人会把"没跑"当成"跑过且通过"。
  lines.push("", `## ${zh.p10.skippedTitle}`, "");
  if (report.skipped.length === 0) {
    lines.push(zh.p10.skippedEmpty);
  } else {
    for (const rule of report.skipped) lines.push(`- ${rule}`);
  }

  return `${lines.join("\n")}\n`;
}

function describeLocation(finding: Finding): string {
  const parts = Object.entries(finding.location ?? {})
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}=${String(value)}`);
  return parts.length === 0 ? zh.app.none : parts.join(", ");
}

function FindingRow({ finding }: { finding: Finding }): ReactNode {
  return (
    <tr>
      <td className="mono">{finding.rule_id}</td>
      <td>
        <SeverityTag severity={finding.severity} />
      </td>
      <td>
        <FindingStatusTag status={finding.status} />
      </td>
      <td>
        {finding.message}
        {finding.evidence !== null && finding.evidence !== undefined && (
          <div className="faint small">证据：{finding.evidence}</div>
        )}
      </td>
      <td className="mono small">{describeLocation(finding)}</td>
      <td className="small">{finding.suggestion ?? zh.app.none}</td>
    </tr>
  );
}

function ReportBody({ report, jobId, projectId }: { report: Report; jobId: string; projectId: string }): ReactNode {
  const [onlyBlocker, setOnlyBlocker] = useState(false);

  const counts = useMemo(() => {
    // 计数是对服务端数组的**长度统计**，不涉及任何判断（NFR-22 禁的是"前端裁决"）。
    const tally = { blocker: 0, major: 0, minor: 0, open: 0, waived: 0 };
    for (const finding of report.findings) {
      tally[finding.severity] += 1;
      if (finding.status === "open") tally.open += 1;
      if (finding.status === "waived") tally.waived += 1;
    }
    return tally;
  }, [report.findings]);

  const grouped = useMemo(() => {
    const visible = onlyBlocker
      ? report.findings.filter((finding) => finding.severity === "blocker")
      : report.findings;
    return GROUP_ORDER.map((severity) => ({
      severity,
      items: visible.filter((finding) => finding.severity === severity),
    })).filter((group) => group.items.length > 0);
  }, [report.findings, onlyBlocker]);

  // 直接用服务端的 verdict 表达"能否定稿"，不重新裁决（`can_finalize` 的唯一出口在 qa 规则引擎）。
  const finalizable = report.verdict !== "blocked";

  return (
    <>
      <div className="card">
        <h2>{zh.p10.summary}</h2>
        <div className="row row--wrap" style={{ marginBottom: 12 }}>
          <VerdictTag verdict={report.verdict} />
          <span className={finalizable ? "tag tag--ok" : "tag tag--danger"}>
            {zh.p10.canFinalize}：{finalizable ? zh.p10.canFinalizeYes : zh.p10.canFinalizeNo}
          </span>
          {!finalizable && <span className="muted small">{zh.p10.canFinalizeReason}</span>}
        </div>

        <KeyValues
          items={[
            { label: zh.p10.counts.blocker, value: counts.blocker },
            { label: zh.p10.counts.major, value: counts.major },
            { label: zh.p10.counts.minor, value: counts.minor },
            { label: zh.findingStatus.open, value: counts.open },
            { label: zh.p10.counts.waived, value: counts.waived },
            { label: zh.generic.createdAt, value: formatTime(report.generated_at) },
          ]}
        />

        {report.summary !== null && report.summary !== "" && (
          <p className="muted" style={{ marginBottom: 0 }}>
            {report.summary}
          </p>
        )}
      </div>

      {report.findings.length === 0 ? (
        <EmptyState
          title={zh.p10.passedTitle}
          body={zh.p10.passedBody}
          action={
            <Link className="btn btn--primary" to={routes.p11Artifacts(projectId)}>
              {zh.p10.goArtifacts}
            </Link>
          }
        />
      ) : (
        <div className="card">
          <div className="row">
            <h2 style={{ margin: 0 }}>{zh.p10.ruleGroups}</h2>
            <span style={{ flex: 1 }} />
            <label className="row small" style={{ width: "auto" }}>
              <input
                type="checkbox"
                checked={onlyBlocker}
                onChange={(event) => setOnlyBlocker(event.target.checked)}
                style={{ width: "auto" }}
              />
              {zh.p10.onlyBlocker}
            </label>
          </div>

          {grouped.map((group) => (
            <div key={group.severity} style={{ marginTop: 16 }}>
              <h3>
                <SeverityTag severity={group.severity} /> <span className="muted small">({group.items.length})</span>
              </h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{zh.p10.ruleId}</th>
                      <th>{zh.p10.severity}</th>
                      <th>{zh.p10.status}</th>
                      <th>{zh.p10.message}</th>
                      <th>{zh.p10.location}</th>
                      <th>{zh.p10.suggestion}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.items.map((finding) => (
                      <FindingRow key={`${finding.rule_id}-${JSON.stringify(finding.location)}`} finding={finding} />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}

      <AdjudicationSection report={report} />

      <div className="card">
        <h2>{zh.p10.exportTitle}</h2>
        <div className="row row--wrap">
          <button
            type="button"
            className="btn"
            onClick={() => downloadText(`${jobId}-report.md`, findingsToMarkdown(report), "text/markdown")}
          >
            {zh.p10.exportReport}（Markdown）
          </button>
          <button
            type="button"
            className="btn"
            onClick={() =>
              downloadText(`${jobId}-report.json`, JSON.stringify(report, null, 2), "application/json")
            }
          >
            {zh.p10.exportReport}（JSON）
          </button>
          <button
            type="button"
            className="btn"
            disabled={report.waivers.length === 0}
            onClick={() =>
              downloadText(`${jobId}-waivers.json`, JSON.stringify(report.waivers, null, 2), "application/json")
            }
          >
            {zh.p10.exportWaivers}
          </button>
        </div>
        <p className="faint small">{zh.p10.exportNote}</p>
      </div>
    </>
  );
}

/**
 * 某条待裁决项**最近一次**裁决（后者胜出）。
 *
 * 这是**查找**，不是规则：服务端也有同名方法（`Report.adjudication_of`），但方法不随
 * JSON 一起过来，故这里按 `subject_id` 取最后一条。谁算待裁决项、什么算办结，判据仍在
 * 服务端（`server/adjudication.py`），此处只把那一行的事实标出来。
 */
function latestAdjudication(report: Report, subjectId: string): Adjudication | null {
  const found = report.adjudications.filter((item) => item.subject_id === subjectId);
  return found.length === 0 ? null : (found[found.length - 1] ?? null);
}

function CandidateRow({
  report,
  finding,
  selected,
  onSelect,
}: {
  report: Report;
  finding: Finding;
  selected: boolean;
  onSelect: () => void;
}): ReactNode {
  const latest = finding.finding_id == null ? null : latestAdjudication(report, finding.finding_id);
  return (
    <tr>
      <td className="mono">{finding.rule_id}</td>
      <td>
        <SeverityTag severity={finding.severity} />
      </td>
      <td>
        {finding.message}
        <div className="faint small mono">{describeLocation(finding)}</div>
      </td>
      <td>
        {latest === null ? (
          <span className="tag tag--warn">{zh.p10.waitingTag}</span>
        ) : (
          <span className="tag tag--neutral">
            {zh.p10.adjudicatedTag} · {zh.adjudication.actions[latest.action]}
          </span>
        )}
      </td>
      <td>
        <button type="button" className="btn btn--sm btn--primary" onClick={onSelect}>
          {selected ? zh.app.close : zh.adjudication.title}
        </button>
      </td>
    </tr>
  );
}

/**
 * 裁决区（本页的四块清单，见文件头）。
 *
 * 选中态只存 `subject_id`：报告被重取之后（裁决会触发重取）列表本身会变，存整条
 * `Finding` 会让"刚办结的那条"继续以旧状态显示在选中卡里。
 */
function AdjudicationSection({ report }: { report: Report }): ReactNode {
  const [selected, setSelected] = useState<string | null>(null);
  // 回报留在**本节**而不是表单里：`waive` 之后这条判定就不再是候选，表单会随列表一起
  // 被卸载，而"作业接下来会怎样"（`advance` / `note`）正好是使用者最要看的那句。
  const [outcome, setOutcome] = useState<AdjudicationResponse | null>(null);

  // 候选 = 未豁免的阻断项（与 `server/adjudication.py::_finding_subjects` 同一判据）。
  const candidates = report.findings.filter(
    (finding) => finding.severity === "blocker" && finding.status !== "waived",
  );
  const current =
    candidates.find((finding) => finding.finding_id !== null && finding.finding_id === selected) ?? null;

  const ledger = [...report.adjudications].sort((left, right) =>
    left.created_at.localeCompare(right.created_at),
  );

  return (
    <>
      <div className="card">
        <div className="row row--wrap">
          <h2 style={{ margin: 0 }}>{zh.p10.decisions}</h2>
          <span style={{ flex: 1 }} />
          <Link className="btn btn--sm" to={routes.p12Inbox}>
            {zh.p10.inboxLink}
          </Link>
        </div>
        <p className="muted" style={{ marginTop: 4 }}>
          {zh.p10.decisionsHint}
        </p>

        {candidates.length === 0 ? (
          <p className="muted small" style={{ marginBottom: 0 }}>
            {report.adjudications.length > 0 ? zh.p10.decisionsSettled : zh.p10.decisionsEmpty}
          </p>
        ) : (
          <>
            <h3>
              {zh.p10.waitingTitle} <span className="muted small">({candidates.length})</span>
            </h3>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{zh.p10.ruleId}</th>
                    <th>{zh.p10.severity}</th>
                    <th>{zh.p10.message}</th>
                    <th>{zh.p10.status}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((finding) => (
                    <CandidateRow
                      key={finding.finding_id ?? `${finding.rule_id}-${describeLocation(finding)}`}
                      report={report}
                      finding={finding}
                      selected={finding.finding_id !== null && finding.finding_id === selected}
                      onSelect={() => setSelected(finding.finding_id ?? null)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <p className="faint small" style={{ marginBottom: 0 }}>
              {zh.p10.waitingNote}
            </p>
          </>
        )}

        {current !== null && (
          <>
            <div className="card card--quiet" style={{ marginTop: 12 }}>
              <h3>{zh.adjudication.subject}</h3>
              <p className="muted small" style={{ marginTop: 0, marginBottom: 0 }}>
                <span className="mono">{current.finding_id}</span>
                <br />
                [{current.rule_id}] {current.message}
              </p>
            </div>
            <AdjudicationForm
              subjectId={current.finding_id ?? null}
              onResult={(result) => {
                setOutcome(result);
                setSelected(null);
              }}
            />
          </>
        )}
      </div>

      {/* 回报渲染在**卡片之外**：它必须活过表单被卸载的那一刻。 */}
      {outcome !== null && <AdjudicationOutcome result={outcome} />}

      <div className="card">
        <h2>{zh.adjudication.ledger}</h2>
        {ledger.length === 0 ? (
          <p className="muted small" style={{ marginBottom: 0 }}>
            {zh.adjudication.ledgerEmpty}
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{zh.adjudication.subject}</th>
                  <th>{zh.adjudication.action}</th>
                  <th>{zh.adjudication.by}</th>
                  <th>{zh.adjudication.reason}</th>
                  <th>{zh.generic.createdAt}</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((item) => (
                  <tr key={item.adjudication_id}>
                    <td className="mono small">{item.subject_id}</td>
                    <td>
                      <span className="tag tag--neutral">{zh.adjudication.actions[item.action]}</span>
                    </td>
                    <td>{item.operator}</td>
                    <td className="small">
                      {item.reason == null || item.reason === "" ? (
                        <span className="faint">{zh.app.none}</span>
                      ) : (
                        item.reason
                      )}
                    </td>
                    <td className="small muted">{formatTime(item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="faint small" style={{ marginBottom: 0 }}>
          {zh.adjudication.ledgerNote}
        </p>
      </div>

      <div className="card">
        <h2>{zh.p10.waiversTitle}</h2>
        {report.waivers.length === 0 ? (
          <p className="muted small" style={{ marginBottom: 0 }}>
            {zh.p10.waiversEmpty}
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{zh.p10.ruleId}</th>
                  <th>{zh.adjudication.by}</th>
                  <th>{zh.adjudication.reason}</th>
                  <th>{zh.p10.waiverScopeLabel}</th>
                  <th>{zh.p10.waiverExpiresAt}</th>
                </tr>
              </thead>
              <tbody>
                {report.waivers.map((waiver) => (
                  <tr key={waiver.waiver_id}>
                    <td className="mono">{waiver.rule_id}</td>
                    <td>{waiver.operator}</td>
                    <td className="small">{waiver.reason}</td>
                    <td className="small">{zh.p10.waiverScopeOf[waiver.scope]}</td>
                    <td className="small muted">
                      {waiver.expires_at == null || waiver.expires_at === "" ? (
                        zh.p10.waiverNoExpiry
                      ) : (
                        formatTime(waiver.expires_at)
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card card--quiet">
        <h2>{zh.p10.skippedTitle}</h2>
        {report.skipped.length === 0 ? (
          <p className="muted small" style={{ marginBottom: 0 }}>
            {zh.p10.skippedEmpty}
          </p>
        ) : (
          <>
            <ul className="small mono" style={{ marginTop: 0 }}>
              {report.skipped.map((rule) => (
                <li key={rule}>{rule}</li>
              ))}
            </ul>
            <p className="faint small" style={{ marginBottom: 0 }}>
              {zh.p10.skippedNote}
            </p>
          </>
        )}
      </div>
    </>
  );
}

export function P10Report(): ReactNode {
  const params = useParams();
  const projectId = params["projectId"] ?? "";
  const jobId = params["jobId"] ?? "";

  const report = useQuery({
    queryKey: ["report", jobId],
    queryFn: () => jobsApi.report(jobId),
    enabled: jobId !== "",
    // 报告缺失是**正常情形**（门禁阶段可能没跑到），重试三次只是把同一件事说三遍。
    retry: false,
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p10.title}</h1>
          <p>{zh.p10.subtitle}</p>
        </div>
        <div className="page-head__actions">
          <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, jobId)}>
            {zh.p10.backToJob}
          </Link>
        </div>
      </div>

      {report.isPending && <Loading />}

      {report.isError &&
        (report.error instanceof ApiError && report.error.code === REPORT_MISSING ? (
          <EmptyState
            title={zh.p10.emptyReportTitle}
            body={zh.p10.emptyReportBody}
            action={
              <Link className="btn" to={routes.p09JobDetail(projectId, jobId)}>
                {zh.p10.backToStages}
              </Link>
            }
          />
        ) : (
          <ErrorPanel error={report.error} />
        ))}

      {report.isSuccess && <ReportBody report={report.data} jobId={jobId} projectId={projectId} />}
    </>
  );
}
