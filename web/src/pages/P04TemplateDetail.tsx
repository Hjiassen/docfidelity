/**
 * P04 模板包详情 —— 对应需求 **FR-M2-16 / DD-7.4**；入口 `/templates/:templateId`（站点地图 7.3.2）。
 *
 * 这一页回答什么问题
 * ----------------
 * 「这份模板包的契约长什么样，它到底是不是和清单一致」。八个页签是**契约的分区视图**
 * （概要 / 样式契约 / 页面设置 / 结构骨架 / 版块索引 / 编号体系 / 待清理占位 / 指纹），
 * 全部取自 `GET /templates/{id}` 的同一个响应——界面数不出任何一个计数，也不判
 * 任何一条"是否阻断"（NFR-22：判定与统计都在服务端）。
 *
 * 「未解析」必须与「检查全绿」区分开
 * ------------------------------
 * 解析没成功的登记里 `package` 与 `summary` 都是 `null`，两条检查清单也是**空数组**。
 * 若按"空数组 ⇒ 没问题"渲染，得到的是一张全绿的检查表——而这正是硬规则 11 要挡的
 * "缺能力跳过伪装成通过"。故 `parsed === false` 时整页按「未解析」渲染：不画页签、
 * 不画检查表，只说明"没有可查的东西"以及怎么补救。
 *
 * 两条检查为什么都要看
 * ----------------
 * `integrity` 回答"包被外部改过没有"（包内容 vs 清单），`fingerprint_problems` 回答
 * "改完指纹又动过内容没有"（指纹 vs 包内容）。**第二条只有在第一条查过之后才有意义**
 * ——所以两条并列显示、各自如实报出，界面不合并成一个"健康"布尔值。
 *
 * 版本对比是**按需**的
 * ----------------
 * 对比要现算两份契约的字段级差异，比"打开详情"重得多，故不做成进页即算。分区划分与
 * `blocking` 判定都由服务端给（`blocking_sections` / `changed_sections`），界面只显示
 * ——否则"哪些变更会阻断"这个口径就会在前端长出第二份。
 *
 * 本文件只管"取哪一份数据"
 * ----------------------
 * 八个页签与版本对比的渲染在 `./p04Sections`（那条接缝是纯粹的"props 进、节点出"）。
 * 这里留下的是页面壳：页头与版本选择、两条检查、版本列表、加载与错误态。
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { templatePackageUrl } from "../api/client";
import { templatesApi } from "../api/endpoints";
import type { RegisteredTemplateOut, TemplateDetailOut } from "../api/types";
import { routes } from "../app/routes";
import { Tabs } from "../components/Tabs";
import { ErrorPanel, KeyValues, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";
import { TAB_ITEMS, TabBody, DiffCard, DataTable } from "./p04Sections";
import type { TabId } from "./p04Sections";

/* ------------------------------------------------------------ 两条检查 */

function ChecksPanel({ detail }: { detail: TemplateDetailOut }): ReactNode {
  return (
    <div className="grid grid--cards">
      <div className="card">
        <h2>{zh.p04.integrityTitle}</h2>
        {detail.integrity.length === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            {zh.p04.integrityOk}
          </p>
        ) : (
          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
            {detail.integrity.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="card">
        <h2>{zh.p04.fingerprintProblemsTitle}</h2>
        {detail.fingerprint_problems.length === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            {zh.p04.fingerprintOk}
          </p>
        ) : (
          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
            {detail.fingerprint_problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        )}
      </div>
      <p className="faint small" style={{ gridColumn: "1 / -1", margin: 0 }}>
        {zh.p04.checksNote}
      </p>
    </div>
  );
}

/* ---------------------------------------------------------- 版本列表 */

function VersionsCard({
  versions,
  onOpen,
}: {
  versions: readonly RegisteredTemplateOut[];
  onOpen: (version: string) => void;
}): ReactNode {
  return (
    <div className="card">
      <h2>{zh.p04.versionsTitle}</h2>
      <DataTable
        columns={[
          zh.p04.versionLabel,
          zh.p03.colDocType,
          zh.p03.colHash,
          zh.p03.colRegisteredAt,
          zh.p03.colActions,
        ]}
      >
        {versions.map((item) => (
          <tr key={item.ref}>
            <td className="mono small">
              {item.ref}
              {item.deprecated && <span className="tag tag--warn">{zh.p03.deprecatedTag}</span>}
            </td>
            <td className="small">{item.doc_type ?? zh.app.none}</td>
            <td className="mono small">
              {item.package_hash === "" ? zh.app.none : item.package_hash}
            </td>
            <td className="small">{formatTime(item.registered_at)}</td>
            <td className="small">
              <div className="row" style={{ gap: 6 }}>
                <button type="button" className="btn btn--sm" onClick={() => onOpen(item.version)}>
                  {zh.p04.openVersion}
                </button>
                <a
                  className="btn btn--sm"
                  href={templatePackageUrl(item.template_id, item.version)}
                >
                  {zh.p03.export}
                </a>
              </div>
            </td>
          </tr>
        ))}
      </DataTable>
    </div>
  );
}

/* ---------------------------------------------------------- 反向引用 */

/**
 * 「谁引用了这个模板包」。
 *
 * 放在**概要页签**（需求 P04 的区块划分），但取数留在页面壳里：`p04Sections` 那条接缝
 * 是"props 进、节点出"的纯渲染，加一个 `useQuery` 会把它变成半页组件。
 *
 * 这一块只给**文档这一半**：作业侧的反查链还没有索引，服务端在端点文档里如实标注了。
 * 界面对应的说法也照实写（`refsHalfNote`）——需求要的是"作业与文档"，只给一半而不说明，
 * 会被读成"没有作业引用过"，那是两个完全不同的结论。
 */
function ReferencesCard({ templateId, version }: { templateId: string; version: string }): ReactNode {
  const refs = useQuery({
    queryKey: ["template-references", templateId, version],
    queryFn: () => templatesApi.references(templateId, version),
  });

  return (
    <div className="card card--quiet">
      <h3>{zh.p04.refsTitle}</h3>

      {refs.isPending && <Loading />}
      {refs.isError && <ErrorPanel error={refs.error} />}

      {refs.data !== undefined &&
        (refs.data.items.length === 0 ? (
          <p className="small muted" style={{ marginBottom: 0 }}>
            {zh.p04.refsEmpty}
          </p>
        ) : (
          <>
            <p className="muted small" style={{ marginTop: 0 }}>
              {zh.p04.refsCount(refs.data.count, refs.data.active_count)}
            </p>
            <DataTable
              columns={[
                zh.p03.colRef,
                zh.p03.profileColTitle,
                zh.p03.colDocType,
                zh.p03.colStatus,
                zh.p03.colActions,
              ]}
            >
              {refs.data.items.map((item) => (
                <tr key={item.ref}>
                  <td className="mono small">{item.ref}</td>
                  <td className="small">{item.title}</td>
                  <td className="small mono">{item.doc_type}</td>
                  <td className="small">
                    {item.deprecated ? (
                      <span className="tag tag--warn">{zh.p03.deprecatedTag}</span>
                    ) : (
                      <span className="tag tag--ok">{zh.p03.availableTag}</span>
                    )}
                  </td>
                  <td className="small">
                    <Link className="btn btn--sm" to={routes.p03Templates}>
                      {zh.p04.refsGoProfiles}
                    </Link>
                  </td>
                </tr>
              ))}
            </DataTable>
          </>
        ))}

      <p className="faint small">{zh.p04.refsNote}</p>
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p04.refsHalfNote}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function P04TemplateDetail(): ReactNode {
  const { templateId = "" } = useParams<{ templateId: string }>();
  const [version, setVersion] = useState("");
  const [tab, setTab] = useState<TabId>("overview");

  // 空串 = "当前有效的那一版"——由服务端决定取哪一版，界面不自己挑（NFR-22）。
  const detail = useQuery({
    queryKey: ["template-detail", templateId, version],
    queryFn: () => templatesApi.detail(templateId, version === "" ? undefined : version),
  });

  const head = (extra?: ReactNode): ReactNode => (
    <div className="page-head">
      <div>
        <h1>{zh.p04.title}</h1>
        <p className="mono">{templateId}</p>
      </div>
      {extra !== undefined && <div className="page-head__actions">{extra}</div>}
    </div>
  );

  if (detail.isPending) {
    return (
      <>
        {head()}
        <Loading />
      </>
    );
  }

  if (detail.isError) {
    return (
      <>
        {head()}
        <ErrorPanel error={detail.error} />
        <p>
          <Link className="btn btn--sm" to={routes.p03Templates}>
            {zh.p04.back}
          </Link>
        </p>
      </>
    );
  }

  const data = detail.data;
  const entry = data.entry;
  const unparsed = !data.parsed || data.package === null;

  return (
    <>
      {head(
        <>
          <Link className="btn btn--sm" to={routes.p03Templates}>
            {zh.p04.back}
          </Link>
          <a className="btn btn--sm" href={templatePackageUrl(entry.template_id, entry.version)}>
            {zh.p04.exportPackage}
          </a>
        </>,
      )}

      <div className="card">
        <div className="row row--wrap" style={{ marginBottom: 12 }}>
          <span className={unparsed ? "tag tag--danger" : "tag tag--ok"}>
            {unparsed ? zh.p04.unparsedTag : zh.p04.parsedTag}
          </span>
          {entry.deprecated && <span className="tag tag--warn">{zh.p03.deprecatedTag}</span>}
          <span className="muted small">{entry.doc_type ?? zh.app.none}</span>
          <label className="field" style={{ minWidth: 220, marginLeft: "auto" }}>
            <span>{zh.p04.versionLabel}</span>
            <select value={version} onChange={(event) => setVersion(event.target.value)}>
              <option value="">{zh.p03.allVersions}</option>
              {data.versions.map((item) => (
                <option key={item.ref} value={item.version}>
                  {item.ref}
                </option>
              ))}
            </select>
          </label>
        </div>

        <KeyValues
          items={[
            { label: zh.p03.colHash, value: <span className="mono">{entry.package_hash}</span> },
            { label: zh.p03.colSource, value: entry.source_filename ?? zh.app.none },
            { label: zh.p03.colRegisteredAt, value: formatTime(entry.registered_at) },
            { label: zh.p03.colBy, value: entry.registered_by ?? zh.app.none },
            { label: zh.p03.colNotes, value: entry.notes ?? zh.app.none },
            ...(entry.deprecated_reason === null || entry.deprecated_reason === undefined
              ? []
              : [{ label: zh.p03.deprecateReason, value: entry.deprecated_reason }]),
          ]}
        />
      </div>

      {unparsed ? (
        <div className="card card--quiet">
          <p className="muted small" style={{ margin: 0 }}>
            {zh.p04.unparsedNotice}
          </p>
        </div>
      ) : (
        <>
          <ChecksPanel detail={data} />

          <div className="card">
            <Tabs items={TAB_ITEMS} value={tab} onChange={setTab} />
            <TabBody tab={tab} detail={data} />
          </div>

          {/* 概要页签的第四块：谁引用了这一版（取数在页面壳，渲染在 `p04Sections` 之外）。 */}
          {tab === "overview" && (
            <ReferencesCard templateId={entry.template_id} version={entry.version} />
          )}

          {/* 版本对比的基准态按 `ref` 重建：换了版本就该重新选基准，而不是留着旧值。 */}
          <DiffCard
            key={entry.ref}
            templateId={entry.template_id}
            versions={data.versions}
            current={entry}
          />

          <VersionsCard versions={data.versions} onOpen={setVersion} />
        </>
      )}
    </>
  );
}
