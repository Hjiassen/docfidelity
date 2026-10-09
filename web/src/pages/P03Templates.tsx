/**
 * P03 模板与档案库 —— 对应需求 **FR-M1-04 / FR-M1-13 / FR-M2-16 / FR-M2-23~27**；
 * 入口 `/templates`（站点地图 7.3.2）。
 *
 * 本页装的是站点地图上的三类资产，故分成三个页签
 * ------------------------------------------
 * ① **模板包**：`GET /templates`，一个版本一行；
 * ② **领域模型**：`GET /domain-models`，一个 `kind` 一行——**没有版本**（见 `./p03DomainModels`）；
 * ③ **文档类型档案**：`GET /profiles`，同样是"一个版本一行"（`profile_id@version` 才是
 *    配置与历史产物的引用键，`DocumentEntry.profile_id` 引用的正是它）。
 *
 * 页签顺序就是依赖顺序：模板包是"能读到的结构"，领域模型是"档案引用它的词汇表"，
 * 档案把两者绑在一起（`bindings.template_packages` + `projections.for_each`）。
 *
 * 一个版本一行，不是一份资产一行
 * ----------------------------
 * 按编号聚合会让人以为"这个模板有一份当前版本"，而实际被引用的是某一个具体版本；
 * 于是同一编号的各版本并列，每行给全 `ref`，谁被引用就一目了然。
 *
 * 「未解析」为什么必须标出来
 * ------------------------
 * DD-7.1 允许"解析失败 → 仅登记原件、稍后重试"，这类登记里 `package_hash` 为空。
 * 不标的话它会和正常登记长得一模一样，直到有人用它发起构建才发现选错了版本。
 *
 * 「档案引用的模板包在不在库」为什么只在详情里标
 * ------------------------------------------
 * `RegisteredProfileOut`（列表行）里**没有** `bindings`——那是档案本体里的字段，
 * 只有 `GET /profiles/{id}` 才带回来。故行级标不出这个标记，界面也不去猜（硬规则 17：
 * 列表不显示契约里没有的字段）。打开详情后，档案声明的引用与模板库的现有引用是两份
 * **服务端清单**，并排显示、逐条标出在不在库，界面不下"能不能用"的结论。
 *
 * 登记档案为什么要手写 JSON
 * -----------------------
 * 档案是人写的一份契约（不像模板包能从原件里解析出来），版本号也没有"文档内版本号"
 * 可读，故必须显式给出。这里不做"表单 → JSON"的映射：那样等于在前端再定义一遍
 * 档案有哪些字段，而契约已经是唯一答案了。
 *
 * 「导入档案包」与「以 JSON 登记」为什么是两个入口（验收 45）
 * ------------------------------------------------------
 * 它们收的东西不同，差别不是格式而是**依赖**：JSON 登记要求引用到的模板包**已经在库**
 * （登记期校验 `bindings.template_packages`），而档案包把档案与它引用的模板包一起带过来
 * （服务端先登记模板、再登记档案）。合成一个入口就得让前端猜"这份输入里带没带模板包"，
 * 而那是包的结构、只有服务端解得出来。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { profileExportUrl, templatePackageUrl } from "../api/client";
import { profilesApi, templatesApi } from "../api/endpoints";
import type {
  ProfileDetailOut,
  ProfileImportOut,
  RegisteredProfileOut,
  RegisteredTemplateOut,
  Schemas,
} from "../api/types";
import { routes } from "../app/routes";
import { Tabs } from "../components/Tabs";
import type { TabItem } from "../components/Tabs";
import { EmptyState, ErrorPanel, KeyValues, Loading, formatTime } from "../components/primitives";
import { zh } from "../i18n/zh";
import { DomainModelsTab } from "./p03DomainModels";

type AppliesStatement = Schemas["AppliesStatement"];
type DocTypeProfile = Schemas["DocTypeProfile-Output"];
type ProjectionSpec = Schemas["ProjectionSpec-Output"];

type TabId = "templates" | "domains" | "profiles";

const TAB_ITEMS: ReadonlyArray<TabItem<TabId>> = [
  { id: "templates", label: zh.p03.tabTemplates },
  // 顺序即依赖顺序，不是偏好：模板包是"能读到的结构"，领域模型是"档案引用它的词汇表"，
  // 档案又把两者绑在一起（bindings.template_packages + projections.for_each）。
  { id: "domains", label: zh.p03.tabDomainModels },
  { id: "profiles", label: zh.p03.tabProfiles },
];

/* ------------------------------------------------------------ 模板包页签 */

function TemplateRow({ item }: { item: RegisteredTemplateOut }): ReactNode {
  const unparsed = item.package_hash === "";
  return (
    <tr>
      <td>
        <span className="mono">{item.template_id}</span>
        <span className="faint">@</span>
        <span className="mono">{item.version}</span>
        <div className="faint small mono">{item.ref}</div>
      </td>
      <td>
        <span className="row row--wrap">
          {item.deprecated && <span className="tag tag--warn">{zh.p03.deprecatedTag}</span>}
          {unparsed && <span className="tag tag--danger">{zh.p03.unparsedTag}</span>}
          {!item.deprecated && !unparsed && (
            <span className="tag tag--ok">{zh.p03.availableTag}</span>
          )}
        </span>
      </td>
      <td>{item.doc_type ?? zh.app.none}</td>
      <td className="mono small">{unparsed ? zh.app.none : item.package_hash}</td>
      <td className="small">
        {item.source_filename ?? zh.app.none}
        {item.source_hash !== "" && (
          <div className="faint small mono">{item.source_hash.slice(0, 16)}…</div>
        )}
      </td>
      <td className="small">
        {formatTime(item.registered_at)}
        {item.registered_by !== null && item.registered_by !== undefined && (
          <div className="faint small">
            {zh.generic.by}
            {item.registered_by}
          </div>
        )}
      </td>
      <td className="small">{item.notes ?? zh.app.none}</td>
      <td className="small">
        <div className="row" style={{ gap: 6 }}>
          <Link className="btn btn--sm" to={routes.p04TemplateDetail(item.template_id)}>
            {zh.p03.open}
          </Link>
          <a className="btn btn--sm" href={templatePackageUrl(item.template_id, item.version)}>
            {zh.p03.export}
          </a>
        </div>
      </td>
    </tr>
  );
}

function TemplatesTab(): ReactNode {
  const [filter, setFilter] = useState("");

  // 过滤交给**服务端**（`?template_id=`）：界面自己筛会把"库里一共有几个版本"这个
  // 计数弄成"筛出来几个"，而那个计数是页面上唯一能说明"库是不是空的"的数字。
  const library = useQuery({
    queryKey: ["templates", filter],
    queryFn: () => templatesApi.list(filter.trim() === "" ? undefined : filter.trim()),
  });

  return (
    <div className="card">
      <div className="row row--wrap">
        <label className="field" style={{ flex: 1 }}>
          <span>{zh.p03.filterLabel}</span>
          <input
            value={filter}
            placeholder={zh.p03.allVersions}
            onChange={(event) => setFilter(event.target.value)}
          />
        </label>
        <span className="muted small">
          {library.data === undefined ? "" : zh.p03.count(library.data.count)}
        </span>
        <button type="button" className="btn btn--sm" onClick={() => void library.refetch()}>
          {zh.app.refresh}
        </button>
      </div>

      {library.isPending && <Loading />}
      {library.isError && <ErrorPanel error={library.error} />}

      {library.data !== undefined &&
        (library.data.items.length === 0 ? (
          <EmptyState
            title={zh.p03.emptyTitle}
            body={zh.p03.emptyNote}
            action={
              <Link className="btn btn--sm btn--primary" to={routes.p05TemplateImport}>
                {zh.p03.goImport}
              </Link>
            }
          />
        ) : (
          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>{zh.p03.colRef}</th>
                  <th>{zh.p03.colStatus}</th>
                  <th>{zh.p03.colDocType}</th>
                  <th>{zh.p03.colHash}</th>
                  <th>{zh.p03.colSource}</th>
                  <th>{zh.p03.colRegisteredAt}</th>
                  <th>{zh.p03.colNotes}</th>
                  <th>{zh.p03.colActions}</th>
                </tr>
              </thead>
              <tbody>
                {library.data.items.map((item) => (
                  <TemplateRow key={item.ref} item={item} />
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </div>
  );
}

/* ------------------------------------------------------------ 档案页签 */

interface FlatProjection {
  spec: ProjectionSpec;
  depth: number;
}

function flattenProjections(
  specs: readonly ProjectionSpec[],
  depth = 0,
  acc: FlatProjection[] = [],
): FlatProjection[] {
  for (const spec of specs) {
    acc.push({ spec, depth });
    flattenProjections(spec.children ?? [], depth + 1, acc);
  }
  return acc;
}

function AppliesBlock({ applies }: { applies: AppliesStatement }): ReactNode {
  return (
    <>
      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileApplies}</h3>
      <KeyValues
        items={[
          { label: zh.p03.appliesStructure, value: applies.structure_enumerable },
          { label: zh.p03.appliesContent, value: applies.content_traceable },
          { label: zh.p03.appliesLayout, value: applies.layout_source },
        ]}
      />
      <p className="faint small" style={{ marginBottom: 12 }}>
        {zh.p03.profileAppliesNote}
      </p>
    </>
  );
}

function RequiredTemplates({
  refs,
  inLibrary,
}: {
  refs: readonly string[];
  inLibrary: ReadonlySet<string>;
}): ReactNode {
  return (
    <>
      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileRequiredTemplates}</h3>
      {refs.length === 0 ? (
        <p className="muted small" style={{ margin: "0 0 6px" }}>
          {zh.p03.profileNoRequiredTemplates}
        </p>
      ) : (
        <p className="row row--wrap" style={{ margin: "0 0 6px" }}>
          {refs.map((ref) => {
            const present = inLibrary.has(ref);
            return (
              <span key={ref} className={present ? "tag tag--ok" : "tag tag--danger"}>
                {ref} · {present ? zh.p03.inLibrary : zh.p03.missingFromLibrary}
              </span>
            );
          })}
        </p>
      )}
      <p className="faint small" style={{ marginBottom: 12 }}>
        {zh.p03.profileTemplateNote}
      </p>
    </>
  );
}

function ProjectionsBlock({ projections }: { projections: readonly ProjectionSpec[] }): ReactNode {
  const flat = flattenProjections(projections);
  return (
    <>
      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileProjections}</h3>
      {flat.length === 0 ? (
        <p className="muted small" style={{ margin: "0 0 12px" }}>
          {zh.p03.projEmpty}
        </p>
      ) : (
        <div className="table-wrap" style={{ marginBottom: 12 }}>
          <table>
            <thead>
              <tr>
                <th>{zh.p03.projForEach}</th>
                <th>{zh.p03.projUnit}</th>
                <th>{zh.p03.projLayout}</th>
                <th>{zh.p03.projSlot}</th>
                <th>{zh.p03.projGroupBy}</th>
                <th>{zh.p03.projOrderBy}</th>
                <th>{zh.p03.projFields}</th>
                <th>{zh.p03.projRequired}</th>
              </tr>
            </thead>
            <tbody>
              {flat.map(({ spec, depth }, index) => (
                <tr key={`${spec.for_each}-${index}`}>
                  <td className="mono small">
                    {"· ".repeat(depth)}
                    {spec.for_each}
                  </td>
                  <td className="small">{spec.unit}</td>
                  <td className="small">{spec.layout}</td>
                  <td className="mono small">{spec.slot ?? zh.app.none}</td>
                  <td className="mono small">{spec.group_by ?? zh.app.none}</td>
                  <td className="mono small">{spec.order_by ?? zh.app.none}</td>
                  <td className="small mono">{(spec.fields ?? []).join(", ") || zh.app.none}</td>
                  <td className="small mono">{(spec.required ?? []).join(", ") || zh.app.none}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function ProfileDetailPanel({
  detail,
  inLibrary,
  onClose,
}: {
  detail: ProfileDetailOut;
  inLibrary: ReadonlySet<string>;
  onClose: () => void;
}): ReactNode {
  const entry = detail.entry;
  const document: DocTypeProfile = detail.document;
  const bindings = document.bindings;
  const genre = document.genre;
  const fingerprint = document.fingerprint;
  const sections = Object.entries(fingerprint?.sections ?? {});

  return (
    <div className="card">
      <div className="page-head">
        <div>
          <h2 style={{ margin: 0 }}>{zh.p03.profileDetailTitle(entry.ref)}</h2>
          <p className="muted small" style={{ margin: 0 }}>
            {entry.title}
          </p>
        </div>
        <div className="page-head__actions">
          <a className="btn btn--sm" href={profileExportUrl(entry.profile_id, entry.version)}>
            {zh.p03.export}
          </a>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            {zh.app.close}
          </button>
        </div>
      </div>

      <KeyValues
        items={[
          { label: zh.p03.colDocType, value: document.doc_type },
          { label: zh.p03.profileColTitle, value: document.title },
          { label: zh.p03.profileColFingerprint, value: <span className="mono">{entry.fingerprint}</span> },
          { label: zh.p03.profileColRegisteredAt, value: formatTime(entry.registered_at) },
          { label: zh.p03.colBy, value: entry.registered_by ?? zh.app.none },
        ]}
      />

      <AppliesBlock applies={document.applies} />

      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileGenre}</h3>
      <p className="row row--wrap" style={{ margin: "0 0 6px" }}>
        {(genre.allowed_unit_types ?? []).length === 0 ? (
          <span className="faint small">{zh.app.none}</span>
        ) : (
          (genre.allowed_unit_types ?? []).map((unit) => (
            <span key={unit} className="tag tag--neutral">
              {unit}
            </span>
          ))
        )}
      </p>
      <p className="muted small" style={{ marginTop: 0 }}>
        {zh.p03.profileAllowedUnits}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileRuleSets}</h3>
      <p className="row row--wrap" style={{ margin: "0 0 12px" }}>
        {(bindings?.rule_sets ?? []).length === 0 ? (
          <span className="faint small">{zh.app.none}</span>
        ) : (
          (bindings?.rule_sets ?? []).map((ref) => (
            <span key={ref} className="tag tag--info">
              {ref}
            </span>
          ))
        )}
      </p>

      <RequiredTemplates refs={bindings?.template_packages ?? []} inLibrary={inLibrary} />

      <ProjectionsBlock projections={document.projections ?? []} />

      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileColFingerprint}</h3>
      {sections.length === 0 ? (
        <p className="muted small" style={{ margin: "0 0 6px" }}>
          {zh.app.none}
        </p>
      ) : (
        <KeyValues
          items={sections.map(([name, digest]) => ({
            label: name,
            value: <span className="mono">{digest}</span>,
          }))}
        />
      )}
      <p className="faint small">{zh.p03.profileFingerprintNote}</p>

      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileContract}</h3>
      <pre className="code">{JSON.stringify(document, null, 2)}</pre>
    </div>
  );
}

function ProfileRow({
  item,
  selected,
  onSelect,
  onDeprecate,
}: {
  item: RegisteredProfileOut;
  selected: boolean;
  onSelect: () => void;
  onDeprecate: () => void;
}): ReactNode {
  return (
    <tr>
      <td>
        <span className="mono">{item.profile_id}</span>
        <span className="faint">@</span>
        <span className="mono">{item.version}</span>
        <div className="faint small">{item.title}</div>
      </td>
      <td className="small">
        {item.deprecated ? (
          <span className="tag tag--warn">{zh.p03.deprecatedTag}</span>
        ) : (
          <span className="tag tag--ok">{zh.p03.availableTag}</span>
        )}
      </td>
      <td>{item.doc_type}</td>
      <td className="mono small">{item.fingerprint}</td>
      <td className="small">
        {formatTime(item.registered_at)}
        {item.registered_by !== null && item.registered_by !== undefined && (
          <div className="faint small">
            {zh.generic.by}
            {item.registered_by}
          </div>
        )}
      </td>
      <td className="small">
        <div className="row" style={{ gap: 6 }}>
          <button type="button" className="btn btn--sm" onClick={onSelect} aria-expanded={selected}>
            {selected ? zh.app.close : zh.p03.profileDetail}
          </button>
          <a className="btn btn--sm" href={profileExportUrl(item.profile_id, item.version)}>
            {zh.p03.export}
          </a>
          {/* 已废弃再点一次是幂等的（服务端保留最早那次理由），故不给禁用态。 */}
          <button type="button" className="btn btn--sm" onClick={onDeprecate}>
            {zh.p03.deprecate}
          </button>
        </div>
      </td>
    </tr>
  );
}

/**
 * 导入**档案包**（zip）——验收 45 的收件端入口。
 *
 * 与下面那个「以 JSON 登记档案」的分工，正是"对面把整包给了我"与"我自己有一份档案本体"
 * 两种情形：JSON 登记要求引用到的模板包**已经在库**，而档案包把档案与它引用的模板包
 * 一起带过来（服务端先登记模板、再登记档案）。
 *
 * 回执里两件事分开显示，不合成一句「导入成功」
 * ------------------------------------------
 * `templates` 与 `profile` 可以一件成一件不成——随包的模板登记成功、档案本体因版本冲突
 * 被拒时，库里确实多了模板包。合成一句会让那次部分成功消失。所以：
 * ① 档案登记失败 ⇒ 错误面板照常显示（服务端给的话术与错误码），**同时**把已登记的模板
 *    列出来；② `unused_templates` 必须显示——那是"包里带了、但档案没有引用、故没登记"
 *    的诚实回报，不显示就等于把它们悄悄丢掉了。
 */
function ProfileImportForm({ onDone }: { onDone: (message: string) => void }): ReactNode {
  const [file, setFile] = useState<File | null>(null);
  const [version, setVersion] = useState("");
  const [author, setAuthor] = useState("");
  const [onConflict, setOnConflict] = useState("abort");
  const client = useQueryClient();

  const importer = useMutation({
    mutationFn: (picked: File) =>
      profilesApi.importPackage(picked, {
        // 版本留空 = 取包内自报的那一版（`包说明.json` 的 `profile_version`）。这里**不填默认值**：
        // 填一个 "1.0" 会把"用包里自报的版本"这条路径在界面上彻底堵死，而那正是导出端
        // 在包说明里写下版本号的目的。
        version: version.trim() === "" ? undefined : version.trim(),
        onConflict,
        author: author.trim() === "" ? undefined : author.trim(),
      }),
    onSuccess: (result) => {
      setFile(null);
      onDone(zh.p03.profileImportDone(result.profile.ref));
      // 模板库与档案库都会变（包里可能带来新模板），故两处都失效。
      void client.invalidateQueries({ queryKey: ["profiles"] });
      void client.invalidateQueries({ queryKey: ["profile-detail"] });
      void client.invalidateQueries({ queryKey: ["templates"] });
    },
  });

  return (
    <div className="card">
      <h2>{zh.p03.profileImportTitle}</h2>
      <p className="muted small">{zh.p03.profileImportHint}</p>

      <div className="form-row" style={{ marginBottom: 12 }}>
        <label className="field" style={{ flex: 1 }}>
          <span>{zh.p03.profileImportPick}</span>
          <input
            type="file"
            accept=".zip,application/zip"
            onChange={(event) => {
              setFile(event.target.files?.item(0) ?? null);
              // 换一个文件就清掉上一次的回执，否则旧结果会挂在新文件旁边。
              importer.reset();
            }}
          />
        </label>
        <label className="field">
          <span>{zh.p03.versionLabel}</span>
          <input
            value={version}
            placeholder={zh.p03.profileImportVersionPlaceholder}
            onChange={(event) => setVersion(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p03.authorLabel}</span>
          <input
            value={author}
            placeholder={zh.p03.authorPlaceholder}
            onChange={(event) => setAuthor(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p03.onConflictLabel}</span>
          <select value={onConflict} onChange={(event) => setOnConflict(event.target.value)}>
            <option value="abort">{zh.p03.onConflictAbort}</option>
            <option value="overwrite">{zh.p03.onConflictOverwrite}</option>
          </select>
        </label>
      </div>

      {importer.isError && <ErrorPanel error={importer.error} />}

      <button
        type="button"
        className="btn btn--primary btn--sm"
        disabled={importer.isPending || file === null}
        onClick={() => {
          if (file !== null) importer.mutate(file);
        }}
      >
        {importer.isPending ? zh.p03.profileImportSubmitting : zh.p03.profileImportSubmit}
      </button>

      {importer.data !== undefined && <ProfileImportReceipt result={importer.data} />}
    </div>
  );
}

/** 导入回执的明细：**先给模板那一段**，因为档案失败时它才是唯一还成立的那半。 */
function ProfileImportReceipt({ result }: { result: ProfileImportOut }): ReactNode {
  return (
    <div style={{ marginTop: 12 }}>
      <h3 style={{ marginBottom: 6 }}>{zh.p03.profileImportTemplates}</h3>
      {result.templates.length === 0 ? (
        <p className="muted small" style={{ marginTop: 0 }}>
          {zh.p03.profileImportNoTemplates}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>{zh.p03.colRef}</th>
                <th>{zh.p03.colHash}</th>
                <th>{zh.p03.colStatus}</th>
              </tr>
            </thead>
            <tbody>
              {result.templates.map((item) => (
                <tr key={item.ref}>
                  <td className="mono small">{item.ref}</td>
                  <td className="mono small">{item.package_hash}</td>
                  <td className="small">
                    {item.replaced ? (
                      <span className="tag tag--warn">{zh.p03.profileImportReplaced}</span>
                    ) : (
                      <span className="tag tag--ok">{zh.p03.availableTag}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result.unused_templates.length > 0 && (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p03.profileImportUnused}
          <span className="mono">{result.unused_templates.join("、")}</span>
        </p>
      )}

      {result.profile.skipped.length > 0 && (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p03.registerSkippedNote}
          <span className="mono">{result.profile.skipped.join("、")}</span>
        </p>
      )}
      {result.profile.findings.length > 0 && (
        <p className="small muted" style={{ marginBottom: 0 }}>
          {zh.p03.registerFindingsNote}
          <span className="mono">{result.profile.findings.join("、")}</span>
        </p>
      )}
    </div>
  );
}

function ProfileRegisterForm({
  onDone,
}: {
  onDone: (message: string) => void;
}): ReactNode {
  const [raw, setRaw] = useState("");
  const [version, setVersion] = useState("1.0");
  const [author, setAuthor] = useState("");
  const [onConflict, setOnConflict] = useState("abort");
  const [parseError, setParseError] = useState(false);
  const client = useQueryClient();

  const register = useMutation({
    mutationFn: (profile: unknown) =>
      profilesApi.register({
        profile: profile as Schemas["DocTypeProfile-Input"],
        version: version.trim() === "" ? "1.0" : version.trim(),
        author: author.trim() === "" ? null : author.trim(),
        on_conflict: onConflict,
      }),
    onSuccess: (result) => {
      setRaw("");
      setParseError(false);
      onDone(result.replaced ? zh.p03.registerReplaced(result.ref) : zh.p03.registerDone(result.ref));
      void client.invalidateQueries({ queryKey: ["profiles"] });
    },
  });

  function submit(): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      setParseError(true);
      return;
    }
    setParseError(false);
    register.mutate(parsed);
  }

  return (
    <div className="card">
      <h2>{zh.p03.registerTitle}</h2>
      <p className="muted small">{zh.p03.registerHint}</p>

      <label className="field" style={{ marginBottom: 12 }}>
        <span>{zh.p03.profileContract}</span>
        <textarea
          rows={12}
          className="mono"
          value={raw}
          placeholder={zh.p03.registerPlaceholder}
          onChange={(event) => setRaw(event.target.value)}
        />
      </label>

      <div className="form-row" style={{ marginBottom: 12 }}>
        <label className="field">
          <span>{zh.p03.versionLabel}</span>
          <input value={version} onChange={(event) => setVersion(event.target.value)} />
        </label>
        <label className="field">
          <span>{zh.p03.authorLabel}</span>
          <input
            value={author}
            placeholder={zh.p03.authorPlaceholder}
            onChange={(event) => setAuthor(event.target.value)}
          />
        </label>
        <label className="field">
          <span>{zh.p03.onConflictLabel}</span>
          <select value={onConflict} onChange={(event) => setOnConflict(event.target.value)}>
            <option value="abort">{zh.p03.onConflictAbort}</option>
            <option value="overwrite">{zh.p03.onConflictOverwrite}</option>
          </select>
        </label>
      </div>

      {parseError && (
        <p className="small" style={{ color: "var(--danger)", marginTop: 0 }}>
          {zh.p03.invalidJson}
        </p>
      )}
      {register.isError && <ErrorPanel error={register.error} />}

      <button
        type="button"
        className="btn btn--primary btn--sm"
        disabled={register.isPending || raw.trim() === ""}
        onClick={submit}
      >
        {register.isPending ? zh.p03.registerSubmitting : zh.p03.registerSubmit}
      </button>
    </div>
  );
}

function ProfilesTab(): ReactNode {
  const [docType, setDocType] = useState("");
  const [showDeprecated, setShowDeprecated] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [deprecating, setDeprecating] = useState<RegisteredProfileOut | null>(null);
  const [reason, setReason] = useState("");
  const [operator, setOperator] = useState("");
  const client = useQueryClient();

  const library = useQuery({
    queryKey: ["profiles", docType, showDeprecated],
    queryFn: () =>
      profilesApi.list({
        docType: docType.trim() === "" ? undefined : docType.trim(),
        includeDeprecated: showDeprecated,
      }),
  });

  // 模板库的现有引用：与档案声明的引用**并排显示**用，界面不据此下结论。
  const templateLibrary = useQuery({
    queryKey: ["templates", ""],
    queryFn: () => templatesApi.list(),
  });
  const inLibrary = new Set((templateLibrary.data?.items ?? []).map((item) => item.ref));

  const detail = useQuery({
    queryKey: ["profile-detail", selected],
    queryFn: () => profilesApi.detail(selected ?? ""),
    enabled: selected !== null,
  });

  const deprecate = useMutation({
    mutationFn: (item: RegisteredProfileOut) =>
      profilesApi.deprecate(item.profile_id, item.version, {
        reason: reason.trim(),
        operator: operator.trim() === "" ? null : operator.trim(),
      }),
    onSuccess: () => {
      setDeprecating(null);
      setReason("");
      setMessage(zh.p03.deprecateDone);
      void client.invalidateQueries({ queryKey: ["profiles"] });
      void client.invalidateQueries({ queryKey: ["profile-detail"] });
    },
  });

  const items = library.data?.items ?? [];

  return (
    <>
      {/* 两个入口并列，顺序就是"对面给了什么"的顺序：整包 ⇒ 只有一份档案本体。 */}
      <ProfileImportForm onDone={setMessage} />
      <ProfileRegisterForm onDone={setMessage} />

      <div className="card">
        <div className="row row--wrap">
          <label className="field" style={{ flex: 1 }}>
            <span>{zh.p03.profileFilterLabel}</span>
            <input
              value={docType}
              placeholder={zh.p03.profileVersions}
              onChange={(event) => setDocType(event.target.value)}
            />
          </label>
          <label className="row small" style={{ gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={showDeprecated}
              onChange={(event) => setShowDeprecated(event.target.checked)}
            />
            <span>{zh.p03.profileShowDeprecated}</span>
          </label>
          <span className="muted small">
            {library.data === undefined ? "" : zh.p03.profileCount(library.data.count)}
          </span>
          <button type="button" className="btn btn--sm" onClick={() => void library.refetch()}>
            {zh.app.refresh}
          </button>
        </div>

        {message !== null && (
          <p className="small" style={{ color: "var(--ok)", marginBottom: 8 }}>
            {message}
          </p>
        )}

        {library.isPending && <Loading />}
        {library.isError && <ErrorPanel error={library.error} />}

        {library.data !== undefined &&
          (items.length === 0 ? (
            <EmptyState title={zh.p03.profileEmptyTitle} body={zh.p03.profileEmptyNote} />
          ) : (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>{zh.p03.profileColTitle}</th>
                    <th>{zh.p03.colStatus}</th>
                    <th>{zh.p03.colDocType}</th>
                    <th>{zh.p03.profileColFingerprint}</th>
                    <th>{zh.p03.colRegisteredAt}</th>
                    <th>{zh.p03.colActions}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <ProfileRow
                      key={item.ref}
                      item={item}
                      selected={selected === item.ref}
                      onSelect={() => {
                        setMessage(null);
                        setSelected(selected === item.ref ? null : item.ref);
                      }}
                      onDeprecate={() => {
                        setMessage(null);
                        setReason("");
                        setDeprecating(item);
                      }}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ))}
      </div>

      {selected !== null && detail.isPending && <Loading />}
      {selected !== null && detail.isError && <ErrorPanel error={detail.error} />}
      {selected !== null && detail.data !== undefined && (
        <ProfileDetailPanel
          detail={detail.data}
          inLibrary={inLibrary}
          onClose={() => setSelected(null)}
        />
      )}

      {deprecating !== null && (
        <div className="card">
          <h2>{zh.p03.deprecateTitle(deprecating.ref)}</h2>
          <p className="muted small">{zh.p03.deprecateNote}</p>
          <div className="form-row" style={{ marginBottom: 12 }}>
            <label className="field">
              <span>{zh.p03.deprecateReason}</span>
              <input
                value={reason}
                placeholder={zh.p03.deprecateReasonPlaceholder}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            <label className="field">
              <span>{zh.p03.authorLabel}</span>
              <input
                value={operator}
                placeholder={zh.p03.authorPlaceholder}
                onChange={(event) => setOperator(event.target.value)}
              />
            </label>
          </div>
          {deprecate.isError && <ErrorPanel error={deprecate.error} />}
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className="btn btn--sm btn--danger"
              disabled={deprecate.isPending || reason.trim() === ""}
              onClick={() => deprecate.mutate(deprecating)}
            >
              {zh.p03.deprecate}
            </button>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => {
                setDeprecating(null);
                setReason("");
              }}
            >
              {zh.app.cancel}
            </button>
          </div>
        </div>
      )}

      {selected === null && deprecating === null && items.length > 0 && (
        <p className="faint small">{zh.p03.profileTemplateNote}</p>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ 页面 */

export function P03Templates(): ReactNode {
  const [tab, setTab] = useState<TabId>("templates");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{zh.p03.title}</h1>
          <p>{zh.p03.subtitle}</p>
        </div>
        <Link className="btn btn--primary btn--sm" to={routes.p05TemplateImport}>
          {zh.p03.goImport}
        </Link>
      </div>

      <Tabs items={TAB_ITEMS} value={tab} onChange={setTab} />

      {tab === "templates" && <TemplatesTab />}
      {tab === "domains" && <DomainModelsTab />}
      {tab === "profiles" && <ProfilesTab />}
    </>
  );
}
