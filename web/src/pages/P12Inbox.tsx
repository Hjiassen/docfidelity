/**
 * P12 裁决待办中心 —— 对应需求 **FR-M7-06 / NFR-15 / 验收 39**；入口 `/inbox`（站点地图 7.3.2）。
 *
 * 这一页回答的唯一问题
 * ------------------
 * 「还有谁要裁决」。判据由服务端给出（`server/adjudication.py`：**没有裁决留痕**才在列表里），
 * 所以办结一项它就立刻消失——界面不做任何筛选、排序或去重。
 *
 * 因此**待办为空 ≠ 都通过了**：空列表只说"没人要看"，能不能定稿看各作业报告里的结论。
 * 那句话是刻意写进空态的——把这两件事混起来的后果是"带病定稿"。
 *
 * 为什么是**跨项目**的页
 * --------------------
 * 入口在顶部栏的待裁决徽标上（`DecisionBadge`），它按 7.3.1 的分工承载跨项目的东西。
 * 服务端 `/inbox` 支持 `project_id` 过滤，但本页不做那个开关：一个"跨项目待办中心"
 * 再套一层项目过滤，使用者先要判断"我在哪个范围"，而这页的存在意义正是
 * "别管在哪个项目，先看还有谁要签字"。
 *
 * 两类待办同列
 * -----------
 * **门禁判定**（未豁免的阻断项）与**待确认决策**（低置信 / 兜底 / 路由给人工）共用一条
 * 提交路径（DD-13.2 只给了一条）。分开列会让使用者以为要跑两个地方。
 *
 * `resolvable` 是服务端给的
 * ------------------------
 * 决策项可能还没有报告（门禁阶段没跑到那个程度），此时裁决无处留痕。服务端回
 * `resolvable=false` + `unresolvable_reason`，界面照抄并**不给提交入口**——
 * 原因来自服务端规则，不是这里猜的（硬约束 15）。
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { inboxApi } from "../api/endpoints";
import type { AdjudicationResponse, InboxItemOut } from "../api/types";
import { routes } from "../app/routes";
import { AdjudicationForm, AdjudicationOutcome } from "../components/AdjudicationForm";
import { SeverityTag, severityOf } from "../components/StatusTag";
import { EmptyState, ErrorPanel, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";

/** 待办是"等一下再看"的东西，不像作业详情那样需要秒级跟随。 */
const POLL_MS = 15_000;

/** 一条待办属于哪一类（`server.adjudication.AdjudicationKind`）。 */
function kindLabel(kind: string): string {
  return kind === "finding" ? zh.p12.kindFinding : zh.p12.kindDecision;
}

function ItemRow({
  item,
  selected,
  onSelect,
}: {
  item: InboxItemOut;
  selected: boolean;
  onSelect: () => void;
}): ReactNode {
  const severity = severityOf(item.severity);  return (
    <tr>
      <td>
        <span className="tag tag--neutral">{kindLabel(item.kind)}</span>
      </td>
      <td>
        {item.title}
        {item.detail !== null && item.detail !== undefined && item.detail !== "" && (
          <div className="faint small">{item.detail}</div>
        )}
      </td>
      <td>
        {severity === null ? <span className="faint">{zh.app.none}</span> : <SeverityTag severity={severity} />}
      </td>
      <td className="small muted">{formatTime(item.created_at)}</td>
      <td>
        <div className="row" style={{ gap: 6 }}>
          {/* 跳作业详情用作业真实所在的项目，而不是"当前页面在哪个项目"。 */}
          <Link className="btn btn--sm" to={routes.p09JobDetail(item.project_id, item.job_id)}>
            {zh.p12.openJob}
          </Link>
          {item.resolvable ? (
            <button type="button" className="btn btn--sm btn--primary" onClick={onSelect}>
              {selected ? zh.app.close : zh.adjudication.title}
            </button>
          ) : (
            <span className="muted small" title={item.unresolvable_reason ?? ""}>
              {zh.p12.unresolvableTag}
            </span>
          )}
        </div>
      </td>
    </tr>
  );
}

export function P12Inbox(): ReactNode {
  const [selected, setSelected] = useState<string | null>(null);
  // 回报留在页面级：办结一项它就立刻从 `/inbox` 消失，表单会随之卸载，
  // 而"作业接下来会怎样"（`advance` / `note`）必须还能看见（同 `P10Report`）。
  const [outcome, setOutcome] = useState<AdjudicationResponse | null>(null);

  const inbox = useQuery({
    queryKey: ["inbox", "list"],
    queryFn: () => inboxApi.list(),
    refetchInterval: POLL_MS,
  });

  const items = inbox.data?.items ?? [];
  const current = items.find((item) => item.subject_id === selected) ?? null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p12.title}</h1>
          <p>{zh.p12.subtitle}</p>
        </div>
        <div className="page-head__actions">
          <span className="muted small">{zh.p12.count(inbox.data?.count ?? items.length)}</span>
        </div>
      </div>

      {inbox.isPending && <Loading />}
      {inbox.isError && <ErrorPanel error={inbox.error} />}

      {inbox.isSuccess && items.length === 0 && (
        <EmptyState
          title={zh.p12.emptyTitle}
          body={zh.p12.emptyBody}
          action={
            <Link className="btn btn--primary" to={routes.p01Projects}>
              {zh.p12.emptyAction}
            </Link>
          }
        />
      )}

      {outcome !== null && <AdjudicationOutcome result={outcome} />}

      {inbox.isSuccess && items.length > 0 && (
        <>
          <div className="card">
            <h2>{zh.p12.listTitle}</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{zh.p12.colKind}</th>
                    <th>{zh.p12.colItem}</th>
                    <th>{zh.p12.colSeverity}</th>
                    <th>{zh.p12.colCreatedAt}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <ItemRow
                      key={item.subject_id}
                      item={item}
                      selected={selected === item.subject_id}
                      onSelect={() => setSelected(selected === item.subject_id ? null : item.subject_id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <p className="faint small" style={{ marginBottom: 0 }}>
              {zh.p12.orderNote}
            </p>
          </div>

          {current !== null && (
            <>
              <div className="card">
                <h2>{zh.adjudication.subject}</h2>
                <p className="muted small" style={{ marginTop: 0 }}>
                  <span className="mono">{current.subject_id}</span>
                  <br />
                  {current.title}
                </p>
              </div>
              <AdjudicationForm
                subjectId={current.subject_id}
                onResult={(result) => {
                  setOutcome(result);
                  setSelected(null);
                }}
              />
            </>
          )}

          <p className="faint small">{zh.p12.sourceNote}</p>
        </>
      )}
    </>
  );
}
