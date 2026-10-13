/**
 * P07 文档配置（生成清单） —— 对应需求 **FR-M7-08 / FR-M3-10 / FR-M3-14 / FR-M2-05 /
 * FR-M2-23 / FR-M4-05**；入口 `/projects/:projectId/config`（站点地图 7.3.2）。
 *
 * 七个区域怎么落
 * ------------
 * 需求给本页列了七个区域，契约里能承载的是六个区块（`DocumentsSection` /
 * `MappingSection` / `TerminologySection` / `PaginationSection` / `RuleSetsSection` /
 * `RoutingSection`）加一块"变更留痕"。三处**契约里没有**的东西没有编出来，而是在对应区块
 * 里写明它归谁：
 * ① 「章节 → 领域类型 → 生成方式」的映射属于**档案的投影规则**（P03 档案详情），
 *    项目配置里只有每份文档的 `overrides_ref` 这一层覆盖；
 * ② 「必须分页的版块 / 连排区间 / 图片缩放上限 / 表格列宽归一」属于**模板包的版块声明**
 *    与样式契约（P04）；
 * ③ 「可用规则集清单」「决策点归属表」在内核里（规则集注册表 / `DECISION_POINTS`），
 *    接口没有把它们作为枚举暴露出来——本页因此只列**已登记的引用**，写错的值由服务端
 *    在校验与构建时指出。界面不另立一份"有哪些规则集"的答案（那正是硬规则 18 要挡的形状）。
 *
 * 保存是整份提交，所以草稿必须完整
 * -----------------------------
 * `PUT /config` 的语义是"我提交了这份配置"（DD-13.6：不覆盖历史，版本号由服务端派生）。
 * 于是编辑器持有**整份** `ProjectConfig` 草稿（六个区块各自只改自己那一份子结构），
 * 提交时原样送回去——漏带一个字段就等于把它清空。
 *
 * 版本号不从这里读
 * -------------
 * 契约里 `ProjectConfig.version` 只是配置体自带的一个字段，而**权威版本号**在
 * `GET /config/versions` 的历史里（仓储派生、严格递增）。所以"当前 vN"取自版本列表末条。
 * 提交时把草稿的 `version` 设为"当前 + 1"，让库里存下的这一份配置体自己也说得通
 * （否则它永远是 1，读回来的人会以为这是第一版）。
 *
 * 校验用的是**服务端上的**那一版
 * ---------------------------
 * `POST /config/validate` 是前置检查的唯一实现（`server/precheck.collect`），它读的是
 * 已保存的配置。编辑器里未保存的改动不参与校验——这句话写在按钮旁边，否则"改了没保存
 * 却校验通过"会被读成"改动没问题"。
 *
 * 非目标：不做可视化拖拽编排（需求 P07 明列），顺序用"上移 / 下移"表达。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, downloadText } from "../api/client";
import { projectsApi } from "../api/endpoints";
import type { PrecheckReportOut, ProjectConfig } from "../api/types";
import { routes } from "../app/routes";
import { ErrorPanel, Loading } from "../components/primitives";
import { zh } from "../i18n/zh";
import {
  DocumentsSection,
  FORMAT_VERSION,
  MappingSection,
  PaginationSection,
  RuleSetsSection,
  TerminologySection,
} from "./p07Sections";
import { RoutingSection } from "./p07RoutingSection";

/** "这个项目还没有配置"的服务端错误码（`Services.get_config`）。 */
const CONFIG_MISSING = "E_CONFIG_INVALID";

/* ------------------------------------------------------------ 校验结果 */

function ValidateResult({ report }: { report: PrecheckReportOut }): ReactNode {
  return (
    <div className="card card--quiet">
      <h3 style={{ marginTop: 0 }}>
        {report.ok ? zh.p07.validateOk : zh.p07.validateFailed}
      </h3>
      {!report.ok && (
        <ul className="small">
          {report.issues.map((issue) => (
            <li key={`${issue.kind}-${issue.message}`}>
              {issue.message}
              {issue.remediation.length > 0 && (
                <div className="faint small">{issue.remediation.join("；")}</div>
              )}
            </li>
          ))}
        </ul>
      )}
      {/* 跳过项始终列出：跳过不等于通过。 */}
      {report.skipped.length > 0 && (
        <>
          <p className="faint small" style={{ marginBottom: 0 }}>
            {zh.p07.validateSkipped}
          </p>
          <ul className="faint small mono" style={{ margin: "4px 0 0 0" }}>
            {report.skipped.map((check) => (
              <li key={check}>{check}</li>
            ))}
          </ul>
        </>
      )}
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p07.validateStale}
      </p>
    </div>
  );
}

/* ---------------------------------------------------------- 变更留痕 */

function VersionsCard({
  projectId,
  onLoad,
  loadingVersion,
}: {
  projectId: string;
  onLoad: (version: number) => void;
  loadingVersion: number | null;
}): ReactNode {
  const versions = useQuery({
    queryKey: ["config-versions", projectId],
    queryFn: () => projectsApi.listConfigVersions(projectId),
    enabled: projectId !== "",
  });

  return (
    <div className="card">
      <h2>{zh.p07.secVersions}</h2>
      <p className="faint small">{zh.p07.secVersionsNote}</p>

      {versions.isPending && <Loading />}
      {versions.isError && <ErrorPanel error={versions.error} />}

      {versions.data !== undefined &&
        (versions.data.length === 0 ? (
          <p className="small muted" style={{ marginBottom: 0 }}>
            {zh.p07.versionEmpty}
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{zh.p07.colVersion}</th>
                  <th>{zh.p07.colAuthor}</th>
                  <th>{zh.p07.colCreatedAt}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {[...versions.data].reverse().map((item) => (
                  <tr key={item.version}>
                    <td className="mono">v{item.version}</td>
                    <td className="small">{item.author ?? zh.app.none}</td>
                    <td className="small muted">{item.created_at}</td>
                    <td>
                      <button
                        type="button"
                        className="btn btn--sm"
                        disabled={loadingVersion !== null}
                        onClick={() => onLoad(item.version)}
                      >
                        {loadingVersion === item.version ? zh.p07.versionLoading : zh.p07.versionLoad}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </div>
  );
}

/* ------------------------------------------------------------- 编辑器 */

function ConfigEditor({
  config,
  projectId,
  currentVersion,
}: {
  config: ProjectConfig;
  projectId: string;
  currentVersion: number | null;
}): ReactNode {
  const client = useQueryClient();
  const [draft, setDraft] = useState<ProjectConfig>(config);
  const [author, setAuthor] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null);

  const patch = (part: Partial<ProjectConfig>): void => {
    setMessage(null);
    setDraft((current) => ({ ...current, ...part }));
  };

  const save = useMutation({
    mutationFn: () =>
      projectsApi.putConfig(
        projectId,
        // 版本号取"当前 + 1"：仓储那边仍会自己派生权威号（它不接受调用方指定），
        // 这里只是让存下的这一份配置体自己也说得通（否则它永远是 1）。
        { ...draft, version: (currentVersion ?? 0) + 1 },
        author.trim() === "" ? undefined : author.trim(),
      ),
    onSuccess: (result) => {
      setMessage(zh.p07.saved(result.version));
      void client.invalidateQueries({ queryKey: ["config", projectId] });
      void client.invalidateQueries({ queryKey: ["config-versions", projectId] });
      void client.invalidateQueries({ queryKey: ["overview", projectId] });
    },
  });

  const validate = useMutation({
    mutationFn: () => projectsApi.validate(projectId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["overview", projectId] });
    },
  });

  const loadVersion = useMutation({
    mutationFn: (version: number) => projectsApi.configVersion(projectId, version),
    onSuccess: (loaded, version) => {
      setDraft(loaded);
      setLoadedVersion(version);
      setMessage(null);
      validate.reset();
    },
  });

  const importFile = (file: File): void => {
    void file.text().then((text) => {
      try {
        const parsed: unknown = JSON.parse(text);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          setMessage(zh.p07.importBadJson);
          return;
        }
        // 只替换内容、保留本项目的 project_id：配置体里的 project_id 与路径不一致时
        // 服务端会拒（`Services.put_config`），这是它该管的事，界面不提前改。
        setDraft({ ...(parsed as ProjectConfig), project_id: projectId });
        setLoadedVersion(null);
        setMessage(zh.p07.importNote);
      } catch {
        setMessage(zh.p07.importBadJson);
      }
    });
  };

  return (
    <>
      <div className="row row--wrap" style={{ marginBottom: 8 }}>
        <span className="mono small">
          {zh.p07.versionLabel(currentVersion ?? draft.version)}
        </span>
        {loadedVersion !== null && (
          <span className="tag tag--info">{zh.p07.loadedTag(loadedVersion)}</span>
        )}
        {save.isPending && <span className="muted small">{zh.p07.saving}</span>}
      </div>

      <div className="card">
        <div className="form-row">
          <label className="field">
            <span>{zh.p07.author}</span>
            <input
              value={author}
              placeholder={zh.p07.authorPlaceholder}
              onChange={(event) => setAuthor(event.target.value)}
            />
          </label>
        </div>
        <p className="faint small">{zh.p07.authorNote}</p>
        <p className="faint small">{zh.p07.versionNote}</p>

        <div className="row row--wrap">
          <button
            type="button"
            className="btn btn--primary btn--sm"
            disabled={save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? zh.p07.saving : zh.p07.save}
          </button>
          <button
            type="button"
            className="btn btn--sm"
            disabled={validate.isPending}
            onClick={() => validate.mutate()}
          >
            {validate.isPending ? zh.p07.validating : zh.p07.validate}
          </button>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() =>
              downloadText(
                `${projectId}-config.json`,
                JSON.stringify(draft, null, 2),
                "application/json",
              )
            }
          >
            {zh.p07.exportConfig}
          </button>
          <label className="btn btn--sm" style={{ display: "inline-flex", alignItems: "center" }}>
            {zh.p07.importConfig}
            <input
              type="file"
              accept="application/json,.json"
              style={{ display: "none" }}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file !== undefined) importFile(file);
                event.target.value = "";
              }}
            />
          </label>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => {
              setDraft(config);
              setLoadedVersion(null);
              setMessage(null);
            }}
          >
            {zh.p07.reload}
          </button>
        </div>

        {message !== null && (
          <p className="small" style={{ color: "var(--ok)", marginBottom: 0 }}>
            {message}
          </p>
        )}
        {save.isError && <ErrorPanel error={save.error} />}
        {loadVersion.isError && <ErrorPanel error={loadVersion.error} />}
      </div>

      {validate.data !== undefined && <ValidateResult report={validate.data} />}
      {validate.isError && <ErrorPanel error={validate.error} />}

      <DocumentsSection
        documents={draft.documents ?? []}
        onChange={(documents) => patch({ documents })}
      />
      <MappingSection documents={draft.documents ?? []} />
      <TerminologySection
        terminology={draft.terminology}
        onChange={(terminology) => patch({ terminology })}
      />
      <PaginationSection
        pagination={draft.pagination}
        onChange={(pagination) => patch({ pagination })}
      />
      <RuleSetsSection ruleSets={draft.rule_sets ?? []} onChange={(rule_sets) => patch({ rule_sets })} />
      <RoutingSection routing={draft.routing} onChange={(routing) => patch({ routing })} />

      <VersionsCard
        projectId={projectId}
        loadingVersion={loadVersion.isPending ? (loadVersion.variables ?? null) : null}
        onLoad={(version) => loadVersion.mutate(version)}
      />
    </>
  );
}

/* ------------------------------------------------------------------ 空态 */

function EmptyConfig({ projectId }: { projectId: string }): ReactNode {
  const client = useQueryClient();
  const demo = useMutation({
    mutationFn: () => projectsApi.demoInit(projectId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["config", projectId] });
      void client.invalidateQueries({ queryKey: ["config-versions", projectId] });
      void client.invalidateQueries({ queryKey: ["projects"] });
    },
  });
  const blank = useMutation({
    mutationFn: () =>
      // 只给 project_id 与空文档清单：其余字段由**服务端**按契约默认值补齐（Pydantic 的
      // `default_factory`），界面不自己编一份默认配置——那份默认值将来变了，界面不会知道。
      // 契约里必填的只有 `project_id` / `version` / `format_version` 三项，后者取契约声明的默认值。
      projectsApi.putConfig(projectId, {
        project_id: projectId,
        version: 1,
        format_version: FORMAT_VERSION,
        documents: [],
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["config", projectId] });
      void client.invalidateQueries({ queryKey: ["config-versions", projectId] });
      void client.invalidateQueries({ queryKey: ["overview", projectId] });
    },
  });

  return (
    <div className="card">
      <h2>{zh.p07.emptyTitle}</h2>
      <p className="small">{zh.p07.emptyBody}</p>

      <div className="row row--wrap">
        <button
          type="button"
          className="btn btn--primary btn--sm"
          disabled={blank.isPending}
          onClick={() => blank.mutate()}
        >
          {zh.p07.emptyBlank}
        </button>
        <button
          type="button"
          className="btn btn--sm"
          disabled={demo.isPending}
          onClick={() => demo.mutate()}
        >
          {zh.p07.emptyDemo}
        </button>
      </div>
      <p className="faint small">{zh.p07.emptyBlankNote}</p>
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p07.emptyDemoNote}
      </p>

      {(blank.isError || demo.isError) && (
        <ErrorPanel error={blank.isError ? blank.error : demo.error} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function P07Config(): ReactNode {
  const { projectId = "" } = useParams<{ projectId: string }>();

  const config = useQuery({
    queryKey: ["config", projectId],
    queryFn: () => projectsApi.getConfig(projectId),
    enabled: projectId !== "",
    retry: false,
  });

  const versions = useQuery({
    queryKey: ["config-versions", projectId],
    queryFn: () => projectsApi.listConfigVersions(projectId),
    enabled: projectId !== "",
  });

  // 权威版本号取自历史列表末条（仓储派生、严格递增）；`ProjectConfig.version` 只是配置体
  // 自带的一个字段，历史里存下来的那一份可能带着旧值。
  const currentVersion = versions.data?.at(-1)?.version ?? null;

  const missing =
    config.error instanceof ApiError && config.error.code === CONFIG_MISSING;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p07.title}</h1>
          <p>{zh.p07.subtitle}</p>
        </div>
        <div className="page-head__actions">
          <Link className="btn btn--sm" to={routes.p08Jobs(projectId)}>
            {zh.p02.quickBuild}
          </Link>
        </div>
      </div>

      {config.isPending && <Loading />}
      {config.isError && !missing && <ErrorPanel error={config.error} />}
      {missing && <EmptyConfig projectId={projectId} />}

      {config.data !== undefined && (
        // 以版本号为 key 重挂载：保存 / 读回历史版本之后，草稿必须重新从新数据初始化，
        // 否则"编辑器里显示旧值、库里已是新值"这种状态会一直挂着。
        <ConfigEditor
          key={`v${currentVersion ?? config.data.version}`}
          config={config.data}
          projectId={projectId}
          currentVersion={currentVersion}
        />
      )}
    </>
  );
}
