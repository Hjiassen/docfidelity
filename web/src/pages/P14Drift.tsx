/**
 * P14 漂移监控 —— 对应需求 **FR-M6-09 / 8.3**；入口 `/projects/:projectId/drift`（站点地图 7.3.2）。
 *
 * 这一页存在的意义是**在构建之前**说明"该重跑什么"
 * ---------------------------------------------
 * 判据是服务端的一次确定性时间比较（`server/overview.py` 的 `build_drift_report`）：
 * 某份文档**最后一次成功产出**的时刻早于**最新配置版本**的写入时刻 ⇒ 它可能已经过期。
 * 等构建跑完再说就晚了——那时新产物已经盖上旧配置的内容。所以比较放在服务端，
 * 界面只照列（NFR-22）。
 *
 * 「判不了」必须说出来，且要显眼
 * ---------------------------
 * 需求 P14 列了三类漂移（已变更未同步 / 已同步未验证 / 长期未动），对象是接口 / 表 / 字段。
 * 三类都是**事实级**判据，需要两份**事实快照**做比对，而快照持久化尚未落地。
 * 于是本页只给出"配置级"的对应物（`never_built` / `changed_not_synced`），
 * 并把 `fact_level_available=false` 与 `fact_level_note`（服务端给的说明）**顶在清单前面**：
 * 把"查不了"渲染成"没有漂移"，使用者会以为文档是最新的——这是最坏的一种误导，
 * 也是硬规则 11（缺能力跳过不得算作通过）在这里的具体形态。
 *
 * 空态要说清因果
 * ------------
 * "无事实快照 → 需先完成一次构建以建立事实快照"这句是需求原文，本页照写，
 * 并给「去发起构建」这一条动作。
 *
 * 非目标：不在本页直接发起"建议修订项"作业——那是构建的另一种输入形态，
 * 落点在作业页；本页给的是"重新产出"的入口。
 */

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { projectsApi } from "../api/endpoints";
import type { DriftItemOut } from "../api/types";
import { routes } from "../app/routes";
import { EmptyState, ErrorPanel, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";

/**
 * 漂移类型的标签。
 *
 * `kind` 在契约里是 `str`（服务端刻意留成开放字符串："加第三类时不必改载荷形状"），
 * 故这里是**查表 + 原值兜底**。兜底显示服务端给的原值而不是"其它"：多出来的一类
 * 恰恰是最该被看见的那种，编一个"其它"会把它藏起来。
 */
const KIND_NAMES: Record<string, string> = zh.p14.kinds;

function kindLabel(kind: string): string {
  return KIND_NAMES[kind] ?? kind;
}

function DriftRow({ item, projectId }: { item: DriftItemOut; projectId: string }): ReactNode {
  return (
    <tr>
      <td className="mono">{item.doc_key}</td>
      <td>
        <span className="tag tag--warn">{kindLabel(item.kind)}</span>
      </td>
      <td className="small">
        {item.detail}
        {item.last_generated_at !== null && item.last_generated_at !== undefined && (
          <div className="faint small">上次成功产出：{formatTime(item.last_generated_at)}</div>
        )}
      </td>
      <td className="small muted">
        {item.last_job_id === null || item.last_job_id === undefined ? (
          zh.app.none
        ) : (
          <span className="mono">{item.last_job_id}</span>
        )}
      </td>
      <td>
        <div className="row" style={{ gap: 6 }}>
          {item.last_job_id !== null && item.last_job_id !== undefined && (
            <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, item.last_job_id)}>
              {zh.p14.openJob}
            </Link>
          )}
          <Link className="btn btn--sm btn--primary" to={routes.p08Jobs(projectId)}>
            {zh.p14.buildNow}
          </Link>
        </div>
      </td>
    </tr>
  );
}

export function P14Drift(): ReactNode {
  const { projectId = "" } = useParams<{ projectId: string }>();

  const drift = useQuery({
    queryKey: ["drift", projectId],
    queryFn: () => projectsApi.drift(projectId),
    enabled: projectId !== "",
  });

  const items = drift.data?.items ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p14.title}</h1>
          <p>{zh.p14.subtitle}</p>
        </div>
        <div className="page-head__actions">
          {drift.data !== undefined && (
            <span className="muted small">{zh.p14.checked(drift.data.checked_documents)}</span>
          )}
          <button type="button" className="btn btn--sm" onClick={() => void drift.refetch()}>
            {zh.app.refresh}
          </button>
        </div>
      </div>

      {drift.isPending && <Loading />}
      {drift.isError && <ErrorPanel error={drift.error} />}

      {drift.data !== undefined && (
        <>
          {/*
           * 这一段**只在判不了时**出现，且用 `fact_level_note`（服务端文本）而不是
           * 界面自写的一句话：它是"为什么这一类判不了"的官方说明，改了它要跟着改。
           */}
          {!drift.data.fact_level_available && (
            <div className="card card--quiet">
              <h2 style={{ marginBottom: 6 }}>{zh.p14.factLevelTitle}</h2>
              <p className="small" style={{ marginBottom: 6 }}>
                {drift.data.fact_level_note}
              </p>
              <p className="faint small" style={{ marginBottom: 0 }}>
                {zh.p14.snapshotHint}
              </p>
            </div>
          )}

          {items.length === 0 ? (
            <EmptyState
              title={zh.p14.emptyTitle}
              body={zh.p14.emptyBody}
              action={
                <Link className="btn btn--primary" to={routes.p08Jobs(projectId)}>
                  {zh.p14.emptyAction}
                </Link>
              }
            />
          ) : (
            <div className="card">
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{zh.p14.colDoc}</th>
                      <th>{zh.p14.colKind}</th>
                      <th>{zh.p14.colDetail}</th>
                      <th>{zh.p14.colLastGenerated}</th>
                      <th>{zh.p14.colActions}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => (
                      <DriftRow key={`${item.doc_key}-${item.kind}`} item={item} projectId={projectId} />
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="faint small" style={{ marginBottom: 0 }}>
                {zh.p14.orderNote}
              </p>
            </div>
          )}

          <p className="faint small">{zh.p14.judgedNote}</p>
          <p className="faint small">
            {zh.p14.configHint}
            <Link to={routes.p07Config(projectId)}>{zh.p14.goConfig}</Link>
          </p>
        </>
      )}
    </>
  );
}
