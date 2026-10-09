/**
 * 裁决表单（P10 报告页与 P12 待办页共用）—— 对应需求 **FR-M7-06 / NFR-15**。
 *
 * 为什么做成一个组件
 * ----------------
 * 两处的提交路径是同一条（`POST /findings/{fid}/decisions`），三个动作的语义、
 * 「责任人」与「理由」的必填口径也必须一致。各写一份的症状是"在 P12 能提交、
 * 在 P10 报错"，而使用者完全看不出两边差在哪。
 *
 * 界面不预判服务端会拒绝的事（硬约束 15）
 * --------------------------------------
 * 只有一处本地拦截：**责任人为空**。它是请求参数缺失（服务端会回 422「字段必填」），
 * 而这里提前一句中文更好读。**理由留空不预判**——`waive` 缺理由由服务端报
 * `E_WAIVER_REASON_REQUIRED`，那句 message 与 remediation 是这条规则的唯一表述，
 * 界面自己编一条"必须填理由"就会与服务端漂移。
 *
 * 提交之后：只显示服务端给的东西
 * ----------------------------
 * `verdict`（重算后的结论）、`advance` / `note`（作业接下来会怎样）、`remaining_subjects`
 * 全部照抄返回值。界面**不推**"这一下点完以后作业还卡着吗"——那正是 NFR-22 禁的事。
 *
 * 回报为什么**不留在表单里**
 * ------------------------
 * 服务端允许对同一条重复裁决，但**办结后该条就不再是候选**（`configure` / `accept` 保留、
 * `waive` 会把它从列表里摘掉）。于是"表单自己显示回报"这条路边上有个坑：一提交，父组件的
 * 候选列表重取 → 条目消失 → 表单连同那份回报一起被卸载，使用者正好看不到"作业接下来会怎样"。
 * 故回报由调用点持有（`onResult`）并渲染在**表单之外**——`AdjudicationOutcome` 就是它。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";

import { decisionsApi } from "../api/endpoints";
import type { AdjudicationAction, AdjudicationResponse } from "../api/types";
import { zh } from "../i18n/zh";
import { ErrorPanel } from "./primitives";
import { VerdictTag } from "./StatusTag";

/** 三种动作的**规范顺序**（取自契约闭集 `AdjudicationAction`，不多一种）。 */
const ACTIONS: readonly AdjudicationAction[] = ["accept", "configure", "waive"];

export function AdjudicationForm({
  subjectId,
  onResult,
}: {
  /** 待裁决项标识；`null` 表示该条目没有可寻址标识（提交不了，只说明原因）。 */
  subjectId: string | null;
  /** 服务端回报的落点。**必须由调用点渲染**，理由见文件头"回报为什么不留在表单里"。 */
  onResult: (result: AdjudicationResponse) => void;
}): ReactNode {
  const [action, setAction] = useState<AdjudicationAction>("accept");
  const [operator, setOperator] = useState("");
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const submit = useMutation({
    mutationFn: () =>
      decisionsApi.submit(subjectId ?? "", {
        action,
        operator: operator.trim(),
        reason: reason.trim() === "" ? null : reason.trim(),
      }),
    onSuccess: (data) => {
      // 裁决会同时改三处：待办清单、那份报告、以及作业状态（可能回到 running）。
      void queryClient.invalidateQueries({ queryKey: ["inbox"] });
      void queryClient.invalidateQueries({ queryKey: ["report"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      setReason("");
      onResult(data);
    },
  });

  if (subjectId === null) {
    return (
      <div className="card card--quiet">
        <h3>{zh.adjudication.title}</h3>
        <p className="muted small" style={{ marginBottom: 0 }}>
          {zh.adjudication.noSubjectId}
        </p>
      </div>
    );
  }

  const blocked = operator.trim() === "" || submit.isPending;

  return (
    <div className="card card--quiet">
      <h3>{zh.adjudication.title}</h3>

      <div className="row row--wrap" style={{ gap: 12 }}>
        {ACTIONS.map((candidate) => (
          <label key={candidate} className="row small" style={{ width: "auto" }}>
            <input
              type="radio"
              name={`action-${subjectId}`}
              style={{ width: "auto" }}
              checked={action === candidate}
              onChange={() => setAction(candidate)}
            />
            {zh.adjudication.actions[candidate]}
          </label>
        ))}
      </div>
      <p className="faint small" style={{ marginTop: 4 }}>
        {zh.adjudication.actionHints[action]}
      </p>

      <label className="small" htmlFor={`operator-${subjectId}`}>
        {zh.adjudication.operator}
      </label>
      <input
        id={`operator-${subjectId}`}
        type="text"
        maxLength={200}
        value={operator}
        placeholder={zh.adjudication.operatorPlaceholder}
        onChange={(event) => setOperator(event.target.value)}
      />

      <label className="small" htmlFor={`reason-${subjectId}`}>
        {zh.adjudication.reason}
      </label>
      <textarea
        id={`reason-${subjectId}`}
        rows={2}
        maxLength={2000}
        value={reason}
        placeholder={zh.adjudication.reasonPlaceholder}
        onChange={(event) => setReason(event.target.value)}
      />
      <p className="faint small" style={{ marginTop: 0 }}>
        {zh.adjudication.reasonNote}
      </p>

      <div className="row row--wrap" style={{ marginTop: 10 }}>
        <button
          type="button"
          className="btn btn--primary"
          disabled={blocked}
          onClick={() => submit.mutate()}
        >
          {submit.isPending ? zh.adjudication.submitting : zh.adjudication.submit}
        </button>
        {operator.trim() === "" && (
          <span className="muted small">{zh.adjudication.operatorRequired}</span>
        )}
      </div>

      {submit.isError && <ErrorPanel error={submit.error} />}
    </div>
  );
}

/** 一次裁决的服务端回报：结论、还剩几项、作业接下来会怎样——**全部照抄**。 */
export function AdjudicationOutcome({ result }: { result: AdjudicationResponse }): ReactNode {
  return (
    <div className="banner" role="status">
      <div className="row row--wrap">
        <strong>
          {zh.adjudication.actions[result.adjudication.action]} · {result.adjudication.operator}
        </strong>
        <span className="faint small mono">{result.adjudication.subject_id}</span>
        <VerdictTag verdict={result.verdict} />
      </div>
      <p className="small" style={{ margin: "6px 0 0 0" }}>
        {result.remaining_subjects > 0
          ? zh.adjudication.remaining(result.remaining_subjects)
          : zh.adjudication.allSettled}
        {zh.adjudication.verdictAfter(zh.verdict[result.verdict])}
      </p>
      <p className="small" style={{ margin: "4px 0 0 0" }}>
        {result.note}
      </p>
    </div>
  );
}
