/**
 * P11 产物与对比 —— 对应需求 **FR-M7-07 / FR-M5-12 / FR-M5-13 / FR-M6-10**；入口
 * `/projects/:projectId/artifacts`（站点地图 7.3.2）。
 *
 * 三层分组由服务端给
 * ----------------
 * `GET /projects/{pid}/artifacts` 已经按"编排作业 → 子作业 → 文档"分好组，并给出
 * `count` / `total_bytes`。界面不做求和，也不自己按 `job_id` 拼层级：分组的粒度是
 * 一条业务判据（"一份子作业恰好对应一份文档"），把它摊到浏览器里就会有两处定义。
 *
 * 「未经门禁」为什么能落在这一页
 * --------------------------
 * 需求 P11 的状态要求是"有产物但报告缺失 → 标注该产物未经门禁，仅作草稿"。判据只能是
 * **服务端事实**：`ArtifactGroupOut.report_verdict` 为 `null` 即"没有门禁报告"。
 * 让界面按"作业状态不是 succeeded"去猜，猜错的方向很糟——把草稿显示成成品。
 *
 * 「版本对比」只转发服务端结论
 * ------------------------
 * `POST /artifacts/compare` 返回差异 + `equal`(服务端结论) + `normalized`(是否已剔除
 * 易变标识) + `truncated`。界面**不**按三个数组是否为空再推一遍"两份一样吗"：
 * 那条判据的唯一实现在比对器里，推第二遍就有了两个答案。
 *
 * 三块显式不落地的能力
 * ----------------
 * ① **渲染目检**（缩略图墙 / 放大 / 章节定位 / 越界与占位残留标注）：需要本机 Office
 *    工具链，且这类判定目前由门禁规则给出（结论在报告页）；
 * ② **页数变化**：需要渲染后的分页信息，产物契约里没有页数字段——编一个页数比不给更糟；
 * ③ **「使用的模板包版本」**：产物只记路径 / 哈希 / 大小，模板版本在作业的输入快照里，
 *    故行内给"进作业详情"的入口，而不是在这一页编一列。
 * 三处都在页面上写明，不静默省略（硬规则 11 的口径：缺能力要说出来）。
 *
 * 「清理」不在本页新建入口
 * ----------------------
 * 回收走设置页既有的 `/maintenance/*`（先列已过期对象、再逐个点名回收）。这里放一个
 * 链接而不是第二个清理按钮：两处清理会各自演化，而"哪些对象算过期"只能有一个答案。
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { artifactPackageUrl, downloadUrl } from "../api/client";
import { artifactsApi } from "../api/endpoints";
import type {
  Artifact,
  ArtifactCompareOut,
  ArtifactGroupOut,
  CompareMode,
} from "../api/types";
import { routes } from "../app/routes";
import { ArtifactKindTag, JobStatusTag, VerdictTag } from "../components/StatusTag";
import {
  EmptyState,
  ErrorPanel,
  KeyValues,
  Loading,
  formatBytes,
  formatTime,
} from "../components/primitives";
import { zh } from "../i18n/zh";

/** 三档分辨率的顺序即下拉顺序（与 `pipeline.artifacts.COMPARE_MODES` 一致，粗 → 细）。 */
const MODES: readonly CompareMode[] = ["structure", "text", "contract"];

/** 分组标题上的文档名。子作业的 `doc_key` 就是"文档"那一层的名字（服务端不再多包一层）。 */
function groupTitle(group: ArtifactGroupOut): string {
  return group.doc_key ?? group.job_id;
}

/* ---------------------------------------------------------------- 门禁结论 */

function VerdictCell({ group }: { group: ArtifactGroupOut }): ReactNode {
  if (group.report_verdict === null) {
    return (
      <span className="tag tag--warn" title={zh.p11.draftNote}>
        {zh.p11.draftTag}
      </span>
    );
  }
  return <VerdictTag verdict={group.report_verdict} />;
}

/* ---------------------------------------------------------------- 产物行 */

function ArtifactRow({ item }: { item: Artifact }): ReactNode {
  return (
    <tr>
      <td className="mono small">{item.artifact_id}</td>
      <td>
        <ArtifactKindTag kind={item.kind} />
      </td>
      <td className="mono small">{item.relative_path}</td>
      <td className="small">{formatBytes(item.size_bytes)}</td>
      <td className="small muted">{formatTime(item.created_at)}</td>
      <td className="mono small">{item.sha256.slice(0, 12)}</td>
      <td>
        <a className="btn btn--sm" href={downloadUrl(item.artifact_id)} download>
          {zh.p11.download}
        </a>
      </td>
    </tr>
  );
}

/* -------------------------------------------------------------- 一层分组 */

function GroupCard({ group, projectId, depth }: { group: ArtifactGroupOut; projectId: string; depth: number }): ReactNode {
  return (
    <div className="card" style={depth === 0 ? undefined : { marginLeft: 18 }}>
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>
            {groupTitle(group)}{" "}
            <span className="faint small">{zh.p11.jobKindOf[group.kind]}</span>
          </h2>
          <p className="faint small" style={{ margin: 0 }}>
            <span className="mono">{group.job_id}</span> · {formatTime(group.created_at)}
          </p>
        </div>
        <div className="page-head__actions">
          <JobStatusTag status={group.status} />
          <VerdictCell group={group} />
          <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, group.job_id)}>
            {zh.p11.openJob}
          </Link>
          <Link
            className="btn btn--sm"
            to={routes.p10Report(projectId, group.job_id)}
          >
            {zh.p11.report}
          </Link>
          <a className="btn btn--sm" href={artifactPackageUrl(group.job_id)} download>
            {zh.p11.package}
          </a>
        </div>
      </div>

      {group.report_verdict === null && (
        <p className="small" style={{ marginTop: 0, color: "var(--warn)" }}>
          {zh.p11.draftNote}
        </p>
      )}

      {group.artifacts.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{zh.p11.colArtifactId}</th>
                <th>{zh.p11.colKind}</th>
                <th>{zh.p11.colPath}</th>
                <th>{zh.p11.colSize}</th>
                <th>{zh.p11.colCreatedAt}</th>
                <th>{zh.p11.colHash}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {group.artifacts.map((item) => (
                <ArtifactRow key={item.artifact_id} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {group.children.map((child) => (
        <GroupCard key={child.job_id} group={child} projectId={projectId} depth={depth + 1} />
      ))}
    </div>
  );
}

/* -------------------------------------------------------------- 版本对比 */

function ComparePanel({ artifacts }: { artifacts: readonly Artifact[] }): ReactNode {
  const [baselineId, setBaselineId] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [mode, setMode] = useState<CompareMode>("structure");

  const compare = useMutation({
    mutationFn: () =>
      artifactsApi.compare({
        baseline_id: baselineId,
        candidate_id: candidateId,
        mode,
      }),
  });

  return (
    <div className="card">
      <h2>{zh.p11.compareTitle}</h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        {zh.p11.compareHint}
      </p>

      {artifacts.length < 2 ? (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p11.compareNeedTwo}
        </p>
      ) : (
        <>
          <div className="form-row">
            <label className="field">
              <span>{zh.p11.compareBaseline}</span>
              <select value={baselineId} onChange={(event) => setBaselineId(event.target.value)}>
                <option value="">{zh.p11.comparePick}</option>
                {artifacts.map((item) => (
                  <option key={item.artifact_id} value={item.artifact_id}>
                    {item.artifact_id} · {zh.artifactKind[item.kind]}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{zh.p11.compareCandidate}</span>
              <select value={candidateId} onChange={(event) => setCandidateId(event.target.value)}>
                <option value="">{zh.p11.comparePick}</option>
                {artifacts.map((item) => (
                  <option key={item.artifact_id} value={item.artifact_id}>
                    {item.artifact_id} · {zh.artifactKind[item.kind]}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{zh.p11.compareMode}</span>
              <select value={mode} onChange={(event) => setMode(event.target.value as CompareMode)}>
                {MODES.map((item) => (
                  <option key={item} value={item}>
                    {zh.p11.compareModeOf[item]}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <p className="faint small">{zh.p11.compareModeNote[mode]}</p>

          <button
            type="button"
            className="btn btn--primary btn--sm"
            disabled={compare.isPending || baselineId === "" || candidateId === ""}
            onClick={() => compare.mutate()}
          >
            {compare.isPending ? zh.p11.comparing : zh.p11.compareRun}
          </button>

          {compare.isError && <ErrorPanel error={compare.error} />}
          {compare.data !== undefined && <CompareResult result={compare.data} />}
          <p className="faint small" style={{ marginBottom: 0 }}>
            {zh.p11.comparePageNote}
          </p>
        </>
      )}
    </div>
  );
}

function DiffList({ title, items }: { title: string; items: readonly string[] }): ReactNode {
  if (items.length === 0) return null;
  return (
    <>
      <h3 style={{ marginBottom: 4 }}>
        {title} <span className="faint small">{items.length}</span>
      </h3>
      <ul className="small mono" style={{ marginTop: 0 }}>
        {items.map((item, index) => (
          <li key={`${item}-${index}`}>{item}</li>
        ))}
      </ul>
    </>
  );
}

function CompareResult({ result }: { result: ArtifactCompareOut }): ReactNode {
  const diff = result.diff;
  return (
    <div className="card card--quiet" style={{ marginTop: 12 }}>
      <p className="small" style={{ marginTop: 0 }}>
        <span
          className={diff.equal ? "tag tag--ok" : "tag tag--warn"}
          style={{ marginRight: 8 }}
        >
          {diff.equal ? zh.p11.compareEqual : zh.p11.compareDiff}
        </span>
        {zh.p11.compareModeOf[diff.mode]} ·{" "}
        {zh.p11.compareCounts(diff.added.length, diff.removed.length, diff.changed.length)}
      </p>

      <KeyValues
        items={[
          { label: zh.p11.compareBaseline, value: <span className="mono">{diff.baseline_ref}</span> },
          { label: zh.p11.compareCandidate, value: <span className="mono">{diff.candidate_ref}</span> },
        ]}
      />

      {diff.normalized && (
        <p className="faint small" style={{ marginBottom: 4 }}>
          {zh.p11.compareNormalized}
        </p>
      )}
      {diff.truncated && (
        <p className="small" style={{ marginBottom: 4, color: "var(--warn)" }}>
          {zh.p11.compareTruncated}
        </p>
      )}

      <DiffList title={zh.p11.compareAdded} items={diff.added} />
      <DiffList title={zh.p11.compareRemoved} items={diff.removed} />
      <DiffList title={zh.p11.compareChanged} items={diff.changed} />

      {diff.notes.length > 0 && (
        <>
          <h3 style={{ marginBottom: 4 }}>{zh.p11.compareNotes}</h3>
          <ul className="small" style={{ marginTop: 0 }}>
            {diff.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function P11Artifacts(): ReactNode {
  const { projectId = "" } = useParams<{ projectId: string }>();

  const list = useQuery({
    queryKey: ["artifacts", projectId],
    queryFn: () => artifactsApi.listByProject(projectId),
    enabled: projectId !== "",
  });

  const groups = list.data?.groups ?? [];
  // 可对比的对象：两份 docx 之间比。这里**不过滤**下拉项——"只比对 docx"是服务端规则
  // （`server.artifacts._comparable`），界面自己筛一遍就有了第二个判据；选错了会拿到
  // 服务端那句带补救话术的错误，比一个静默消失的选项更说得清。
  const flat: Artifact[] = groups.flatMap((group) => [
    ...group.artifacts,
    ...group.children.flatMap((child) => child.artifacts),
  ]);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p11.title}</h1>
          <p>{zh.p11.subtitle}</p>
        </div>
        <div className="page-head__actions">
          {list.data !== undefined && (
            <>
              <span className="muted small">{zh.p11.count(list.data.count)}</span>
              <span className="muted small">{zh.p11.totalBytes(formatBytes(list.data.total_bytes))}</span>
            </>
          )}
          <button type="button" className="btn btn--sm" onClick={() => void list.refetch()}>
            {zh.app.refresh}
          </button>
        </div>
      </div>

      {list.isPending && <Loading />}
      {list.isError && <ErrorPanel error={list.error} />}

      {list.isSuccess && groups.length === 0 && (
        <EmptyState
          title={zh.p11.emptyTitle}
          body={zh.p11.emptyBody}
          action={
            <Link className="btn btn--primary" to={routes.p08Jobs(projectId)}>
              {zh.p11.emptyAction}
            </Link>
          }
        />
      )}

      {list.isSuccess && groups.length > 0 && (
        <>
          <p className="faint small">{zh.p11.templateVersionNote}</p>
          <p className="faint small">{zh.p11.packageNote}</p>

          {groups.map((group) => (
            <GroupCard key={group.job_id} group={group} projectId={projectId} depth={0} />
          ))}
        </>
      )}

      {list.isSuccess && <ComparePanel artifacts={flat} />}

      <div className="card card--quiet">
        <h2>{zh.p11.checkTitle}</h2>
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p11.checkNote}
        </p>
      </div>

      <div className="card card--quiet">
        <div className="page-head">
          <div>
            <h2 style={{ margin: 0 }}>{zh.p11.cleanupTitle}</h2>
          </div>
          <div className="page-head__actions">
            <Link className="btn btn--sm" to={routes.p13Settings}>
              {zh.p11.cleanupGo}
            </Link>
          </div>
        </div>
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p11.cleanupNote}
        </p>
      </div>
    </>
  );
}
