/**
 * P08 作业列表与发起构建 —— 对应需求 **FR-M7-03 / FR-M7-15 / 4.8（同项目串行 / 子作业并行）**；
 * 入口 `/projects/:pid/jobs`（站点地图 7.3.2）。
 *
 * 为什么它在首批里
 * ----------------
 * 设计文档 19.1 给 TASK-P0-15 的验收判据是「最小闭环可在浏览器跑通」，而首批页面
 * （P01 / P05 / P09 / P10 / P13）里**没有任何一处能发起构建**：`POST /projects/{pid}/jobs`
 * 的界面入口按站点地图归 P08。缺了它闭环就断在"作业从哪来"——P09 / P10 只能看别人
 * 用 curl 造出来的作业。故本页随批补入。
 *
 * 本轮只做服务端真能落地的那部分
 * ------------------------------
 * 规格的发起面板含"模板包版本 / 事实源快照 / 模型开关 / 是否导出 PDF 与目检图 / 备注"，
 * 而 `SubmitBuild` 只接受 `doc_keys` 与 `author`。按 DD-14.5「界面每个"能改的"必须对应
 * 配置里的一行」，**不渲染点不动的控件**——一个按下去没反应的开关比没有这个开关更糟。
 *
 * 「不能发起」的两种情形要分清（NFR-22：界面不承载业务判断）
 * --------------------------------------------------------
 * ① **拦住**：所选文档缺 `template_ref` / `profile_id`，或流水线阶段未注册。这是
 *    **服务端前置检查**（`POST /config/validate`，DD-8）的结论，界面直接读它的 `ok`
 *    与 `issues`（连补救话术一起照抄），不自己按字段判空再推一遍。
 * ② **只告知、不拦**：已有未结束的作业。同项目串行是**执行期**由项目锁保证的，提交时
 *    服务端**不拒绝**（新作业照常入队，只是排在后面）。这里若禁用按钮，就是前端自造了
 *    一条服务端没有的规则，使用者会误以为"必须等它跑完才能发起"。
 *
 * 为什么调**两次**同一端点
 * ----------------------
 * ① `configCheck`：整份配置里全部启用文档的检查（不传 `doc_keys`）。它回答"表格里哪几行
 *    引用不全"，与勾选无关——标注只对已勾选的文档出现，会让使用者以为"刚才还是好的"。
 * ② `selectionCheck`：本次所选文档的检查（传 `doc_keys`）。它回答"现在能不能发起"。
 * 合并成一次的代价是二者必舍其一：要么表格标注随勾选漂移，要么按钮可用性由前端过滤
 * `issues` 得出——后者正是硬约束 15 禁的"界面自造规则"。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { jobsApi, projectsApi, systemApi } from "../api/endpoints";
import { isAbsent } from "../api/types";
import type { Job, PrecheckReportOut } from "../api/types";
import { routes } from "../app/routes";
import { JobStatusTag } from "../components/StatusTag";
import { EmptyState, ErrorPanel, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";

/** 未结束（还会占着这个项目）的作业状态。`awaiting_decision` 也在内：它同样没有终态。 */
const UNFINISHED: readonly string[] = ["queued", "running", "awaiting_decision"];

/** 列表轮询间隔。列表是"看一眼现状"的页面，不像作业详情那样需要秒级跟随。 */
const POLL_MS = 5000;

function toggle(list: readonly string[], value: string): string[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

/**
 * 从检查结果里读出「哪份文档缺哪个字段」。
 *
 * `PrecheckIssueOut.where` 在契约里是 `Record<string, unknown>`（不同问题的形状不同），
 * 故这里**逐字段收窄**：形状不对就当作没有。绝不 `as` 强转——强转出来的错误值会让表格
 * 上的「引用不全」标注撒一个没人能发现的谎。
 */
function missingFieldsByDoc(report: PrecheckReportOut): Map<string, string[]> {
  const found = new Map<string, string[]>();
  for (const issue of report.issues) {
    const problems = issue.where?.["problems"];
    if (!Array.isArray(problems)) continue;
    for (const problem of problems) {
      if (typeof problem !== "object" || problem === null) continue;
      const record = problem as Record<string, unknown>;
      const docKey = record["doc_key"];
      const field = record["field"];
      if (typeof docKey !== "string" || typeof field !== "string") continue;
      found.set(docKey, [...(found.get(docKey) ?? []), field]);
    }
  }
  return found;
}

/**
 * 从检查结果里读出「哪份文档引用了还没登记的资产」。
 *
 * 与服务端的两条 kind 一一对应：`missing_references` 管"**没填**"（上面那个函数），
 * `unregistered_references` 管"填了、库里没有"（本函数）。两者的修复动作不同——前者去补
 * 配置，后者去补库里的资产——所以界面上也分成两个标注，不合成一句"引用有问题"。
 *
 * 取值形状同样**逐字段收窄**，理由同上：形状不对就当没有，绝不强转。
 */
function unregisteredByDoc(report: PrecheckReportOut): Map<string, string[]> {
  const found = new Map<string, string[]>();
  for (const issue of report.issues) {
    const problems = issue.where?.["unregistered"];
    if (!Array.isArray(problems)) continue;
    for (const problem of problems) {
      if (typeof problem !== "object" || problem === null) continue;
      const record = problem as Record<string, unknown>;
      const docKey = record["doc_key"];
      const field = record["field"];
      const ref = record["ref"];
      if (typeof docKey !== "string" || typeof field !== "string") continue;
      const shown = typeof ref === "string" ? `${field}=${ref}` : field;
      found.set(docKey, [...(found.get(docKey) ?? []), shown]);
    }
  }
  return found;
}

/* --------------------------------------------------------------- 作业行 */

function JobRow({
  job,
  children,
  projectId,
  expanded,
  onToggleExpand,
  onAskCancel,
  confirming,
  onCancel,
  busy,
}: {
  job: Job;
  children: readonly Job[];
  projectId: string;
  expanded: boolean;
  onToggleExpand: () => void;
  onAskCancel: () => void;
  confirming: boolean;
  onCancel: () => void;
  busy: boolean;
}): ReactNode {
  const progress = job.progress;
  const failed = progress?.failed ?? 0;

  return (
    <>
      <tr>
        <td>
          <div className="row" style={{ gap: 8 }}>
            {children.length > 0 && (
              <button
                type="button"
                className="btn btn--sm"
                onClick={onToggleExpand}
                aria-expanded={expanded}
              >
                {expanded ? zh.p08.hideChildren : zh.p08.showChildren}
              </button>
            )}
            <Link className="mono small" to={routes.p09JobDetail(projectId, job.job_id)}>
              {job.job_id}
            </Link>
          </div>
        </td>
        <td>
          <span className="tag tag--neutral">{zh.job.kind[job.kind]}</span>
          {job.doc_key !== null && <span className="mono small"> {job.doc_key}</span>}
        </td>
        <td>
          <JobStatusTag status={job.status} />
        </td>
        <td>
          {isAbsent(progress) ? (
            <span className="faint">{zh.app.none}</span>
          ) : (
            <span>
              {zh.p08.childProgressDone(progress.done, progress.total)}
              {failed > 0 && <span className="muted small">（失败 {failed}）</span>}
            </span>
          )}
        </td>
        <td>{job.artifact_ids.length}</td>
        <td className="small muted">{formatTime(job.created_at)}</td>
        <td className="small">
          {isAbsent(job.initiated_by) || job.initiated_by === "" ? (
            <span className="faint">{zh.app.none}</span>
          ) : (
            job.initiated_by
          )}
        </td>
        <td>
          <div className="row" style={{ gap: 6 }}>
            <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, job.job_id)}>
              {zh.p08.enter}
            </Link>
            {(job.status === "queued" || job.status === "running") &&
              (confirming ? (
                <>
                  <button type="button" className="btn btn--sm btn--danger" disabled={busy} onClick={onCancel}>
                    {zh.p08.cancelYes}
                  </button>
                  <button type="button" className="btn btn--sm" onClick={onAskCancel}>
                    {zh.p08.cancelNo}
                  </button>
                </>
              ) : (
                <button type="button" className="btn btn--sm" onClick={onAskCancel}>
                  {zh.p08.cancel}
                </button>
              ))}
          </div>
        </td>
      </tr>
      {expanded &&
        children.map((child) => (
          <tr key={child.job_id}>
            <td>
              <span className="mono small muted">{child.job_id}</span>
            </td>
            <td>
              <span className="mono small">{child.doc_key ?? zh.app.none}</span>
            </td>
            <td>
              <JobStatusTag status={child.status} />
            </td>
            <td className="faint">{zh.app.none}</td>
            <td>{child.artifact_ids.length}</td>
            <td className="small muted">{formatTime(child.created_at)}</td>
            <td className="small">
              {isAbsent(child.initiated_by) || child.initiated_by === "" ? (
                <span className="faint">{zh.app.none}</span>
              ) : (
                child.initiated_by
              )}
            </td>
            <td>
              <Link className="btn btn--sm" to={routes.p09JobDetail(projectId, child.job_id)}>
                {zh.p08.enter}
              </Link>
            </td>
          </tr>
        ))}
    </>
  );
}

/* ----------------------------------------------------------------- 页面 */

export function P08Jobs(): ReactNode {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const [selected, setSelected] = useState<readonly string[]>([]);
  const [expanded, setExpanded] = useState<readonly string[]>([]);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [author, setAuthor] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const config = useQuery({
    queryKey: ["project", projectId, "config"],
    queryFn: () => projectsApi.getConfig(projectId),
    enabled: projectId !== "",
  });

  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => systemApi.settings(),
    staleTime: 60_000,
  });

  const jobs = useQuery({
    queryKey: ["jobs", projectId],
    queryFn: () => jobsApi.listByProject(projectId),
    enabled: projectId !== "",
    refetchInterval: POLL_MS,
  });

  const documents = config.data?.documents ?? [];
  const enabledDocs = documents.filter((doc) => doc.enabled);
  const chosen = enabledDocs.filter((doc) => selected.includes(doc.doc_key));
  const chosenKeys = chosen.map((doc) => doc.doc_key);

  /**
   * 两份前置检查（理由见文件头）。`selectionCheck` 没勾选时不发请求——"还没勾"是界面
   * 状态，不是服务端规则，那一条由下面的 `blockedNoneSelected` 表达。
   */
  const configCheck = useQuery({
    queryKey: ["precheck", projectId, "all"],
    queryFn: () => projectsApi.validate(projectId),
    enabled: projectId !== "" && config.isSuccess,
  });

  const selectionCheck = useQuery({
    queryKey: ["precheck", projectId, "selected", chosenKeys.join(",")],
    queryFn: () => projectsApi.validate(projectId, chosenKeys),
    enabled: projectId !== "" && chosenKeys.length > 0,
  });

  const submit = useMutation({
    mutationFn: (payload: { docKeys: readonly string[]; author: string }) =>
      jobsApi.submit(projectId, payload.docKeys, payload.author),
    onSuccess: (job) => {
      setSelected([]);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      navigate(routes.p09JobDetail(projectId, job.job_id));
    },
  });

  const cancel = useMutation({
    mutationFn: (jobId: string) => jobsApi.cancel(jobId),
    onSuccess: () => {
      setConfirming(null);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  /* ------------------------------------------------------------ 派生量 */

  const items = jobs.data?.items ?? [];

  const childrenByParent = new Map<string, Job[]>();
  const roots: Job[] = [];
  const orphans: Job[] = [];
  for (const job of items) {
    // `parent_job_id` 在生成的类型里是 `string | null | undefined`；先归一到 `null`，
    // 否则下面的 `=== null` 判完仍剩 `undefined`，当 Map 的键用会直接报错。
    const parent = job.parent_job_id ?? null;
    if (parent === null) {
      roots.push(job);
      continue;
    }
    // 子作业归属父作业；父作业不在本次返回里（例如按状态过滤过）就单独列，别丢。
    if (items.some((candidate) => candidate.job_id === parent)) {
      const bucket = childrenByParent.get(parent) ?? [];
      bucket.push(job);
      childrenByParent.set(parent, bucket);
    } else {
      orphans.push(job);
    }
  }

  const modelByConfig = config.data?.routing?.model_enabled ?? false;
  const modelBySettings = settings.data?.model.enabled ?? false;

  // 逐行标注取自**整份配置**的检查（与勾选无关），保证"勾了才变红"不会发生。
  // 两个 Map 对应服务端的两条 kind：`missing_references`（没填）与 `unregistered_references`
  // （填了、库里没有）。分开算、分开展示，因为补救动作不同（补配置 vs 补库）。
  const missingByDoc = configCheck.data === undefined ? new Map<string, string[]>() : missingFieldsByDoc(configCheck.data);
  const unregisteredMap =
    configCheck.data === undefined ? new Map<string, string[]>() : unregisteredByDoc(configCheck.data);

  /**
   * 「当前无法发起」的原因。全部来自**服务端**（NFR-22 / 硬约束 15）：
   * 配置层面的三条是界面状态（没加载完 / 没勾选），其余逐条照抄检查结果的 `message`
   * 与 `remediation`——那句话术是这条规则的唯一表述，界面自己编一份就会与服务端漂移。
   */
  const blocked: { reason: string; fixes: readonly string[] }[] = [];
  if (config.isPending) {
    blocked.push({ reason: zh.p08.blockedConfigLoading, fixes: [] });
  } else if (config.isError) {
    blocked.push({ reason: zh.p08.blockedConfigFailed, fixes: [] });
  } else if (enabledDocs.length === 0) {
    blocked.push({ reason: zh.p08.blockedNoDocuments, fixes: [] });
  } else if (selected.length === 0) {
    blocked.push({ reason: zh.p08.blockedNoneSelected, fixes: [] });
  } else if (selectionCheck.isPending) {
    blocked.push({ reason: zh.p08.blockedPrecheckPending, fixes: [] });
  } else if (selectionCheck.isError) {
    blocked.push({ reason: zh.p08.blockedPrecheckFailed, fixes: [] });
  } else if (!selectionCheck.data.ok) {
    // `ok` 是服务端结论，直接照它走；不按 `issues` 是否为空再推一遍。
    for (const issue of selectionCheck.data.issues) {
      blocked.push({ reason: issue.message, fixes: issue.remediation });
    }
  }

  const unfinished = items.filter((job) => UNFINISHED.includes(job.status));
  const busy = submit.isPending || cancel.isPending;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p08.title}</h1>
          <p>{zh.p08.subtitle}</p>
        </div>
      </div>

      {submit.isError && <ErrorPanel error={submit.error} />}
      {cancel.isError && <ErrorPanel error={cancel.error} />}

      {/* ------------------------------------------------------ 发起面板 */}
      <div className="card">
        <h2>{zh.p08.launch}</h2>

        {config.isPending && <Loading />}
        {config.isError && <ErrorPanel error={config.error} />}

        {config.isSuccess && (
          <>
            <p className="muted small">{zh.p08.docFamiliesHint}</p>

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 40 }} />
                    <th>{zh.p08.docFamilies}</th>
                    <th>{zh.p08.docTitle}</th>
                    <th className="mono">profile_id</th>
                    <th className="mono">template_ref</th>
                  </tr>
                </thead>
                <tbody>
                  {documents.map((doc) => {
                    const missing = missingByDoc.get(doc.doc_key) ?? [];
                    const unregistered = unregisteredMap.get(doc.doc_key) ?? [];
                    const picked = selected.includes(doc.doc_key);
                    return (
                      <tr key={doc.doc_key}>
                        <td>
                          <input
                            type="checkbox"
                            style={{ width: "auto" }}
                            checked={picked}
                            disabled={!doc.enabled}
                            onChange={() => setSelected((current) => toggle(current, doc.doc_key))}
                          />
                        </td>
                        <td className="mono">{doc.doc_key}</td>
                        <td>
                          {doc.title}
                          {!doc.enabled && (
                            <span className="tag tag--neutral"> {zh.p08.disabledTag}</span>
                          )}
                          {doc.enabled && missing.length > 0 && (
                            <span className="tag tag--warn">
                              {" "}
                              {zh.p08.incompleteRefTag}：{missing.join("、")}
                            </span>
                          )}
                          {doc.enabled && unregistered.length > 0 && (
                            <span className="tag tag--danger">
                              {" "}
                              {zh.p08.unregisteredRefTag}：{unregistered.join("、")}
                            </span>
                          )}
                        </td>
                        <td className="mono small">{doc.profile_id === "" ? zh.app.none : doc.profile_id}</td>
                        <td className="mono small">{doc.template_ref === "" ? zh.app.none : doc.template_ref}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="faint small">{zh.p08.refTagsNote}</p>

            <div className="row row--wrap" style={{ marginTop: 12 }}>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => setSelected(enabledDocs.map((doc) => doc.doc_key))}
              >
                {zh.p08.selectAll}
              </button>
              <button type="button" className="btn btn--sm" onClick={() => setSelected([])}>
                {zh.p08.clearSelection}
              </button>
              <span className="muted small">{zh.p08.selectedCount(selected.length)}</span>
            </div>

            <label className="small" htmlFor="submit-author" style={{ marginTop: 14 }}>
              {zh.p08.author}
            </label>
            <input
              id="submit-author"
              type="text"
              maxLength={200}
              value={author}
              placeholder={zh.p08.authorPlaceholder}
              onChange={(event) => setAuthor(event.target.value)}
            />
            <p className="faint small">{zh.p08.authorNote}</p>

            <dl className="grid grid--kv small" style={{ marginTop: 14 }}>
              <dt>{zh.p08.modelScopeProject}</dt>
              <dd>{modelByConfig ? zh.p08.modelOn : zh.p08.modelOff}</dd>
              <dt>{zh.p08.modelScopeDeployment}</dt>
              <dd>
                {modelBySettings ? zh.p08.modelOn : zh.p08.modelOff}
                <Link className="btn btn--link btn--sm" to={routes.p13Settings}>
                  {zh.p08.goSettings}
                </Link>
              </dd>
            </dl>
            <p className="faint small">{zh.p08.modelNote}</p>

            {blocked.length > 0 && (
              <div className="banner" style={{ marginTop: 14 }}>
                <strong>{zh.p08.blockedTitle}</strong>
                <ul className="small" style={{ margin: "6px 0 0 0" }}>
                  {blocked.map((item) => (
                    <li key={item.reason}>
                      {item.reason}
                      {item.fixes.length > 0 && (
                        <div className="faint small">
                          {zh.p08.precheckFixes}：{item.fixes.join("；")}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* 前置检查的**正**结论与**跳过项**都要说出来：跳过不等于通过。 */}
            {blocked.length === 0 && selectionCheck.data !== undefined && (
              <div className="card card--quiet" style={{ marginTop: 14 }}>
                <h3>{zh.p08.precheckTitle}</h3>
                <p className="small" style={{ marginTop: 0 }}>
                  {zh.p08.precheckOk}
                </p>
                {selectionCheck.data.skipped.length > 0 && (
                  <>
                    <p className="faint small" style={{ marginBottom: 0 }}>
                      {zh.p08.precheckSkipped}
                    </p>
                    <ul className="faint small mono" style={{ margin: "4px 0 0 0" }}>
                      {selectionCheck.data.skipped.map((check) => (
                        <li key={check}>{check}</li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}

            {/* 只告知、不拦：服务端在提交时不拒绝排队（见文件头 ②）。 */}
            {blocked.length === 0 && unfinished.length > 0 && (
              <p className="muted small" style={{ marginTop: 12 }}>
                {zh.p08.queueNotice}
              </p>
            )}

            <div className="row" style={{ marginTop: 14 }}>
              <button
                type="button"
                className="btn btn--primary"
                disabled={busy || blocked.length > 0}
                onClick={() => submit.mutate({ docKeys: chosenKeys, author: author.trim() })}
              >
                {submit.isPending ? zh.p08.submitting : zh.p08.submit}
              </button>
              <Link className="btn" to={routes.p07Config(projectId)}>
                {zh.nav.config}
              </Link>
            </div>

            <details style={{ marginTop: 16 }}>
              <summary className="small muted">{zh.p08.pendingParamsTitle}</summary>
              <p className="faint small">{zh.p08.pendingParamsNote}</p>
            </details>
          </>
        )}
      </div>

      {/* ------------------------------------------------------ 作业历史 */}
      <div className="card">
        <h2>{zh.p08.list}</h2>

        {jobs.isPending && <Loading />}
        {jobs.isError && <ErrorPanel error={jobs.error} />}

        {jobs.isSuccess && items.length === 0 && (
          <EmptyState
            title={zh.p08.listEmptyTitle}
            body={zh.p08.listEmptyBody}
            action={
              <Link className="btn" to={routes.p07Config(projectId)}>
                {zh.p08.listEmptyAction}
              </Link>
            }
          />
        )}

        {items.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{zh.p08.jobId}</th>
                  <th>{zh.p08.kind}</th>
                  <th>{zh.p09.status}</th>
                  <th>{zh.p08.childProgress}</th>
                  <th>{zh.p08.artifactCount}</th>
                  <th>{zh.p08.createdAt}</th>
                  <th>{zh.p08.author}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {roots.map((job) => (
                  <JobRow
                    key={job.job_id}
                    job={job}
                    children={childrenByParent.get(job.job_id) ?? []}
                    projectId={projectId}
                    expanded={expanded.includes(job.job_id)}
                    onToggleExpand={() => setExpanded((current) => toggle(current, job.job_id))}
                    confirming={confirming === job.job_id}
                    onAskCancel={() =>
                      setConfirming(confirming === job.job_id ? null : job.job_id)
                    }
                    onCancel={() => cancel.mutate(job.job_id)}
                    busy={busy}
                  />
                ))}
                {orphans.map((job) => (
                  <JobRow
                    key={job.job_id}
                    job={job}
                    children={[]}
                    projectId={projectId}
                    expanded={false}
                    onToggleExpand={() => undefined}
                    confirming={confirming === job.job_id}
                    onAskCancel={() =>
                      setConfirming(confirming === job.job_id ? null : job.job_id)
                    }
                    onCancel={() => cancel.mutate(job.job_id)}
                    busy={busy}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="faint small">{zh.p08.missingColumnsNote}</p>
      </div>
    </>
  );
}
