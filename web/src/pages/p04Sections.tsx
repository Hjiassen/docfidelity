/**
 * P04 的**契约分区视图**与**版本对比**（页面的可渲染部分）。
 *
 * 与 `P04TemplateDetail` 分工
 * -------------------------
 * 本模块只做一件事：把 `TemplateDetailOut` / `TemplateDiffOut` 里的契约切片画出来
 * （八个页签 + 变更明细）。页面壳（页头、版本选择、两条检查、版本列表、加载与错误态）
 * 留在页面上——它管的是"取哪一份数据"，本模块管的是"这一份数据长什么样"。
 *
 * 拆出来的理由只有一个：一个文件塞下"取数 + 八种渲染 + 对比"会顶到行数上限，
 * 而这条接缝本身是干净的（本模块**只有 props 进、React 节点出**，不发请求、
 * 不持状态，除了版本对比那一次按需的 `diff` 调用）。
 *
 * 纪律：所有计数、分区划分、「会不会阻断」都**由服务端算好**，本模块只显示，
 * 不做任何换算或筛选（NFR-22：界面不承载业务判断）。
 */

import { useMutation } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import type { ReactNode } from "react";

import { templatesApi } from "../api/endpoints";
import type {
  RegisteredTemplateOut,
  Schemas,
  TemplateDetailOut,
  TemplateDiffOut,
} from "../api/types";
import type { TabItem } from "../components/Tabs";
import { ErrorPanel, KeyValues } from "../components/primitives";
import { zh } from "../i18n/zh";

/* 契约里的嵌套结构：生成类型里都有，这里就地起短名，避免满页 `Schemas["…"]`。 */
type BlockEntry = Schemas["BlockEntry"];
type ChapterNode = Schemas["ChapterNode"];
type FieldDelta = Schemas["FieldDeltaOut"];
type HeadingBinding = Schemas["HeadingBinding"];
type NumberLevel = Schemas["NumberLevel"];
type NumInstance = Schemas["NumInstance"];
type PageSetupContract = Schemas["PageSetupContract"];
type PlaceholderEntry = Schemas["PlaceholderEntry"];
type SectionSpec = Schemas["SectionSpec"];
type SlotSpec = Schemas["SlotSpec"];
type StyleContract = Schemas["StyleContract"];

export type TabId =
  | "overview"
  | "styles"
  | "pageSetup"
  | "outline"
  | "blocks"
  | "numbering"
  | "placeholders"
  | "fingerprint";

/* --------------------------------------------------------------- 小工具 */

function yesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return zh.app.none;
  return value ? zh.generic.yes : zh.generic.no;
}

/** 定位符：`part · xpath [index]`，缺项不占位。 */
function locatorText(locator: Schemas["BlockLocator"] | null | undefined): string {
  if (locator === null || locator === undefined) return zh.app.none;
  const tail = locator.index === null || locator.index === undefined ? "" : ` [${locator.index}]`;
  return `${locator.part} · ${locator.xpath}${tail}`;
}

/** 一张统一的只读表：列名 + 行数据（cells 里放 ReactNode，便于内嵌标签）。 */
export function DataTable({
  columns,
  children,
}: {
  columns: readonly string[];
  children: ReactNode;
}): ReactNode {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/** 没有数据时的一句说明（避免出现"一张只有表头的表"）。 */
function NoRows({ text }: { text: string }): ReactNode {
  return (
    <p className="muted small" style={{ margin: 0 }}>
      {text}
    </p>
  );
}

/* ------------------------------------------------------------ 概要页签 */

function OverviewTab({ detail }: { detail: TemplateDetailOut }): ReactNode {
  const summary = detail.summary;
  if (summary === null) return <NoRows text={zh.p04.fingerprintNoPartition} />;
  const blocks = Object.entries(summary.block_counts);
  const parts = Object.entries(summary.placeholder_parts);

  return (
    <>
      <KeyValues
        items={[
          { label: zh.p04.sumStyleCount, value: summary.style_count },
          { label: zh.p04.sumParagraphStyles, value: summary.paragraph_style_count },
          { label: zh.p04.sumTableStyles, value: summary.table_style_count },
          { label: zh.p04.sumUsedParagraph, value: summary.used_paragraph_styles },
          { label: zh.p04.sumUsedTable, value: summary.used_table_styles },
          { label: zh.p04.sumNumberingLevels, value: summary.numbering_levels },
          { label: zh.p04.sumNumberingInstances, value: summary.numbering_instances },
          { label: zh.p04.sumHeadingBindings, value: summary.heading_bindings },
          { label: zh.p04.sumTablePrototypes, value: summary.table_prototypes },
          { label: zh.p04.sumSlots, value: summary.slot_count },
          { label: zh.p04.sumDeclaredSlots, value: summary.declared_slot_count },
          { label: zh.p04.sumSlotCompletion, value: summary.slot_completion.toFixed(2) },
          { label: zh.p04.sumChapters, value: summary.chapter_count },
          { label: zh.p04.sumMaxHeading, value: summary.max_heading_level },
          { label: zh.p04.sumPlaceholders, value: summary.placeholder_count },
        ]}
      />

      <h3 style={{ marginBottom: 6 }}>{zh.p04.blockCountsTitle}</h3>
      {blocks.length === 0 ? (
        <NoRows text={zh.p04.blocksNoRows} />
      ) : (
        <KeyValues items={blocks.map(([kind, count]) => ({ label: kind, value: count }))} />
      )}

      <h3 style={{ marginBottom: 6 }}>{zh.p04.placeholderPartsTitle}</h3>
      {parts.length === 0 ? (
        <NoRows text={zh.p04.placeholdersNoRows} />
      ) : (
        <KeyValues items={parts.map(([part, count]) => ({ label: part, value: count }))} />
      )}

      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p04.sumNote}
      </p>
    </>
  );
}

/* --------------------------------------------------------- 样式契约页签 */

function StylesTab({ styles }: { styles: readonly StyleContract[] }): ReactNode {
  if (styles.length === 0) return <NoRows text={zh.p04.styleNoRows} />;
  return (
    <DataTable
      columns={[
        zh.p04.styleName,
        zh.p04.styleId,
        zh.p04.styleType,
        zh.p04.styleFont,
        zh.p04.styleSize,
        zh.p04.styleBold,
        zh.p04.styleSpacing,
        zh.p04.styleIndent,
        zh.p04.styleBasedOn,
      ]}
    >
      {styles.map((style) => (
        <tr key={style.style_id}>
          <td>{style.name}</td>
          <td className="mono small">{style.style_id}</td>
          <td className="small">{style.type}</td>
          <td className="small">
            {style.font_latin ?? zh.app.none}
            <div className="faint small">{style.font_east_asian ?? zh.app.none}</div>
          </td>
          <td className="small">{style.size_pt ?? zh.app.none}</td>
          <td className="small">{yesNo(style.bold)}</td>
          <td className="small">{style.line_spacing ?? zh.app.none}</td>
          <td className="small">
            {style.indent_left_twips ?? zh.app.none}
            <div className="faint small">{style.indent_first_line_twips ?? zh.app.none}</div>
          </td>
          <td className="mono small">{style.based_on ?? zh.app.none}</td>
        </tr>
      ))}
    </DataTable>
  );
}

/* --------------------------------------------------------- 页面设置页签 */

function PageSetupTab({ pageSetup }: { pageSetup: PageSetupContract }): ReactNode {
  const sections: readonly SectionSpec[] = pageSetup.sections ?? [];
  return (
    <>
      <KeyValues
        items={[
          {
            label: zh.p04.pageSize,
            value: `${pageSetup.page_size.width_twips} × ${pageSetup.page_size.height_twips}`,
          },
          { label: zh.p04.orientation, value: pageSetup.page_size.orientation },
        ]}
      />
      <h3 style={{ marginBottom: 6 }}>{zh.p04.marginsTitle}</h3>
      <KeyValues
        items={[
          { label: zh.p04.marginTop, value: pageSetup.margins.top_twips },
          { label: zh.p04.marginBottom, value: pageSetup.margins.bottom_twips },
          { label: zh.p04.marginLeft, value: pageSetup.margins.left_twips },
          { label: zh.p04.marginRight, value: pageSetup.margins.right_twips },
        ]}
      />
      <h3 style={{ marginBottom: 6 }}>{zh.p04.sectionsTitle}</h3>
      {sections.length === 0 ? (
        <NoRows text={zh.p04.sectionsNoRows} />
      ) : (
        <DataTable
          columns={[
            zh.p04.colIndex,
            zh.p04.pageSize,
            zh.p04.orientation,
            zh.p04.marginsTitle,
            zh.p04.headerFooterRoles,
            zh.p04.startType,
            zh.p04.outlineSlots,
          ]}
        >
          {sections.map((section) => (
            <tr key={section.index}>
              <td className="small">{section.index}</td>
              <td className="small mono">
                {section.page_size.width_twips} × {section.page_size.height_twips}
              </td>
              <td className="small">{section.page_size.orientation}</td>
              <td className="small mono">
                {section.margins.top_twips}/{section.margins.bottom_twips}/
                {section.margins.left_twips}/{section.margins.right_twips}
              </td>
              <td className="small">
                {(section.header_footer_roles ?? []).join(" / ") || zh.app.none}
              </td>
              <td className="small">{section.start_type ?? zh.app.none}</td>
              <td className="small">
                {(section.diff_from_previous ?? []).length === 0
                  ? zh.app.none
                  : (section.diff_from_previous ?? []).join(" · ")}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </>
  );
}

/* --------------------------------------------------------- 结构骨架页签 */

function ChapterTree({
  nodes,
  depth = 0,
}: {
  nodes: readonly ChapterNode[];
  depth?: number;
}): ReactNode {
  return (
    <ul style={{ margin: "4px 0 0", paddingLeft: depth === 0 ? 0 : 16 }}>
      {nodes.map((node, index) => (
        <li key={`${node.title}-${index}`}>
          <span className="mono small faint">L{node.level}</span> {node.title}
          {node.style_id !== null && node.style_id !== undefined && (
            <span className="faint small mono"> · {node.style_id}</span>
          )}
          {(node.children ?? []).length > 0 && (
            <ChapterTree nodes={node.children ?? []} depth={depth + 1} />
          )}
        </li>
      ))}
    </ul>
  );
}

function SlotRows({ slots }: { slots: readonly SlotSpec[] }): ReactNode {
  return (
    <>
      {slots.map((slot) => (
        <tr key={slot.slot_id}>
          <td className="mono small">{slot.slot_id}</td>
          <td className="small">
            <span className={slot.required ? "tag tag--warn" : "tag tag--neutral"}>
              {yesNo(slot.required)}
            </span>
          </td>
          <td className="small">{(slot.unit_types ?? []).join(" · ") || zh.app.none}</td>
          <td className="mono small">{slot.locator}</td>
        </tr>
      ))}
    </>
  );
}

function OutlineTab({ detail }: { detail: TemplateDetailOut }): ReactNode {
  const outline = detail.package?.outline;
  if (outline === null || outline === undefined) return <NoRows text={zh.p04.outlineNoRows} />;
  const tree = outline.chapter_tree ?? [];
  const slots = outline.slots ?? [];

  return (
    <>
      <h3 style={{ marginBottom: 6 }}>{zh.p04.outlineAllowedBlocks}</h3>
      <p className="row row--wrap" style={{ margin: "0 0 12px" }}>
        {(outline.allowed_block_types ?? []).length === 0 ? (
          <span className="faint small">{zh.app.none}</span>
        ) : (
          (outline.allowed_block_types ?? []).map((kind) => (
            <span key={kind} className="tag tag--neutral">
              {kind}
            </span>
          ))
        )}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p04.outlineChapters}</h3>
      {tree.length === 0 ? <NoRows text={zh.p04.outlineNoRows} /> : <ChapterTree nodes={tree} />}

      <h3 style={{ marginBottom: 6, marginTop: 14 }}>{zh.p04.outlineSlots}</h3>
      {slots.length === 0 ? (
        <NoRows text={zh.p04.outlineNoRows} />
      ) : (
        <DataTable
          columns={[zh.p04.slotId, zh.p04.slotRequired, zh.p04.slotUnitTypes, zh.p04.slotLocator]}
        >
          <SlotRows slots={slots} />
        </DataTable>
      )}
    </>
  );
}

/* --------------------------------------------------------- 版块索引页签 */

function BlocksTab({ blocks }: { blocks: readonly BlockEntry[] }): ReactNode {
  if (blocks.length === 0) return <NoRows text={zh.p04.blocksNoRows} />;
  return (
    <DataTable
      columns={[zh.p04.blockId, zh.p04.blockKind, zh.p04.blockLocator, zh.p04.blockCrossRef]}
    >
      {blocks.map((block) => (
        <tr key={block.block_id}>
          <td className="mono small">{block.block_id}</td>
          <td className="small">{block.kind}</td>
          <td className="mono small">{locatorText(block.locator)}</td>
          <td className="small">
            {block.cross_template_ref ? (
              <span className="tag tag--warn">{yesNo(true)}</span>
            ) : (
              <span className="faint">{yesNo(false)}</span>
            )}
          </td>
        </tr>
      ))}
    </DataTable>
  );
}

/* --------------------------------------------------------- 编号体系页签 */

function NumberLevelRows({ levels }: { levels: readonly NumberLevel[] }): ReactNode {
  return (
    <>
      {levels.map((level) => (
        <tr key={level.ilvl}>
          <td className="small">{level.ilvl}</td>
          <td className="mono small">{level.lvl_text}</td>
          <td className="small">{level.num_fmt}</td>
          <td className="small">{level.start}</td>
          <td className="small">{level.indent_hanging_twips ?? zh.app.none}</td>
        </tr>
      ))}
    </>
  );
}

function NumberingTab({ detail }: { detail: TemplateDetailOut }): ReactNode {
  const numbering = detail.package?.numbering;
  if (numbering === null || numbering === undefined) return <NoRows text={zh.p04.numberingNoRows} />;

  const abstractNum = numbering.abstract_num ?? [];
  const instances: readonly NumInstance[] = numbering.num ?? [];
  const bindings: readonly HeadingBinding[] = numbering.heading_bindings ?? [];

  return (
    <>
      <h3 style={{ marginBottom: 6 }}>{zh.p04.numberingLevels}</h3>
      {abstractNum.length === 0 ? (
        <NoRows text={zh.p04.numberingNoRows} />
      ) : (
        abstractNum.map((abstract) => (
          <div key={abstract.abstract_num_id} style={{ marginBottom: 12 }}>
            <p className="muted small mono" style={{ margin: "0 0 4px" }}>
              {zh.p04.abstractNumId}
              {abstract.abstract_num_id}
            </p>
            <DataTable
              columns={[
                zh.p04.ilvl,
                zh.p04.lvlText,
                zh.p04.numFmt,
                zh.p04.startAt,
                zh.p04.styleIndent,
              ]}
            >
              <NumberLevelRows levels={abstract.levels ?? []} />
            </DataTable>
          </div>
        ))
      )}

      <h3 style={{ marginBottom: 6 }}>{zh.p04.numberingInstances}</h3>
      {instances.length === 0 ? (
        <NoRows text={zh.p04.numberingNoRows} />
      ) : (
        <DataTable columns={[zh.p04.numId, zh.p04.abstractNumId]}>
          {instances.map((instance) => (
            <tr key={instance.num_id}>
              <td className="small">{instance.num_id}</td>
              <td className="small">{instance.abstract_num_id}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3 style={{ marginBottom: 6 }}>{zh.p04.numberingBindings}</h3>
      {bindings.length === 0 ? (
        <NoRows text={zh.p04.numberingNoRows} />
      ) : (
        <DataTable columns={[zh.p04.styleId, zh.p04.numId, zh.p04.ilvl]}>
          {bindings.map((binding) => (
            <tr key={binding.style_id}>
              <td className="mono small">{binding.style_id}</td>
              <td className="small">{binding.num_id}</td>
              <td className="small">{binding.ilvl}</td>
            </tr>
          ))}
        </DataTable>
      )}
    </>
  );
}

/* ------------------------------------------------------- 待清理占位页签 */

function PlaceholdersTab({ entries }: { entries: readonly PlaceholderEntry[] }): ReactNode {
  if (entries.length === 0) return <NoRows text={zh.p04.placeholdersNoRows} />;
  return (
    <>
      <DataTable
        columns={[zh.p04.placeholderText, zh.p04.placeholderLocator, zh.p04.placeholderRule]}
      >
        {entries.map((entry, index) => (
          <tr key={`${entry.text}-${index}`}>
            <td>{entry.text}</td>
            <td className="mono small">{locatorText(entry.locator)}</td>
            <td className="mono small">{entry.proposed_rule_id ?? zh.app.none}</td>
          </tr>
        ))}
      </DataTable>
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p04.placeholdersNoRows}
      </p>
    </>
  );
}

/* ------------------------------------------------------------- 指纹页签 */

function FingerprintTab({ detail }: { detail: TemplateDetailOut }): ReactNode {
  const fingerprint = detail.package?.fingerprint ?? null;
  if (fingerprint === null) return <NoRows text={zh.p04.fingerprintNoPartition} />;
  const sections = Object.entries(fingerprint.sections ?? {});

  return (
    <>
      <KeyValues
        items={[
          { label: zh.p04.fingerprintDigest, value: <span className="mono">{fingerprint.digest}</span> },
        ]}
      />
      <h3 style={{ marginBottom: 6 }}>{zh.p04.fingerprintSections}</h3>
      {sections.length === 0 ? (
        <NoRows text={zh.p04.fingerprintNoPartition} />
      ) : (
        <DataTable columns={[zh.p04.fingerprintSections, zh.p04.fingerprintDigest]}>
          {sections.map(([name, digest]) => (
            <tr key={name}>
              <td className="small">{name}</td>
              <td className="mono small">{digest}</td>
            </tr>
          ))}
        </DataTable>
      )}
      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p04.fingerprintNote}
      </p>
    </>
  );
}

/* ------------------------------------------------------------ 版本对比 */

function DeltaRows({ changes }: { changes: readonly FieldDelta[] }): ReactNode {
  return (
    <>
      {changes.map((change, index) => (
        <tr key={`${change.path}-${index}`}>
          <td className="mono small">{change.path}</td>
          <td className="small">{change.description}</td>
          <td className="mono small">{change.before ?? zh.app.none}</td>
          <td className="mono small">{change.after ?? zh.app.none}</td>
        </tr>
      ))}
    </>
  );
}

function DiffResult({ diff }: { diff: TemplateDiffOut }): ReactNode {
  if (diff.identical) {
    return (
      <p className="muted small" style={{ marginBottom: 0 }}>
        {zh.p04.diffIdentical}
      </p>
    );
  }
  return (
    <>
      <KeyValues
        items={[
          { label: zh.p04.diffFrom, value: <span className="mono">{diff.before_ref}</span> },
          { label: zh.p04.diffTo, value: <span className="mono">{diff.after_ref}</span> },
          { label: zh.p04.diffSummary, value: diff.summary },
        ]}
      />

      <h3 style={{ marginBottom: 6 }}>{zh.p04.diffBlockingTitle}</h3>
      <p className="row row--wrap" style={{ margin: "0 0 12px" }}>
        {diff.blocking_sections.length === 0 ? (
          <span className="faint small">{zh.app.none}</span>
        ) : (
          diff.blocking_sections.map((name) => (
            <span key={name} className="tag tag--danger">
              {name}
            </span>
          ))
        )}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p04.diffChangedTitle}</h3>
      <p className="row row--wrap" style={{ margin: "0 0 12px" }}>
        {diff.changed_sections.length === 0 ? (
          <span className="faint small">{zh.app.none}</span>
        ) : (
          diff.changed_sections.map((name) => (
            <span key={name} className="tag tag--info">
              {name}
            </span>
          ))
        )}
      </p>

      <h3 style={{ marginBottom: 6 }}>{zh.p04.diffChangelogTitle}</h3>
      <DataTable
        columns={[zh.p04.diffPath, zh.p04.diffDescription, zh.p04.diffBefore, zh.p04.diffAfter]}
      >
        {diff.sections.map((section) => (
          <Fragment key={section.section}>
            <tr>
              <td colSpan={4}>
                <span className="mono small">{section.section}</span>
                {section.blocking && <span className="tag tag--danger">{zh.p04.diffBlockingTitle}</span>}
                <span className="faint small"> {section.impact}</span>
              </td>
            </tr>
            <DeltaRows changes={section.changes} />
          </Fragment>
        ))}
      </DataTable>

      <p className="faint small" style={{ marginBottom: 0 }}>
        {zh.p04.diffNote}
      </p>
    </>
  );
}

export function DiffCard({
  templateId,
  versions,
  current,
}: {
  templateId: string;
  versions: readonly RegisteredTemplateOut[];
  current: RegisteredTemplateOut;
}): ReactNode {
  const others = versions.filter((item) => item.ref !== current.ref);
  const [before, setBefore] = useState(others[0]?.version ?? "");
  const [after, setAfter] = useState(current.version);

  const compare = useMutation({
    mutationFn: () => templatesApi.diff(templateId, before, after),
  });

  if (others.length === 0) {
    return (
      <div className="card card--quiet">
        <h2>{zh.p04.diffTitle}</h2>
        <p className="muted small" style={{ margin: 0 }}>
          {zh.p04.diffNeedTwoVersions}
        </p>
      </div>
    );
  }

  return (
    <div className="card">
      <h2>{zh.p04.diffTitle}</h2>
      <div className="form-row" style={{ marginBottom: 12 }}>
        <label className="field">
          <span>{zh.p04.diffFrom}</span>
          <select value={before} onChange={(event) => setBefore(event.target.value)}>
            {versions.map((item) => (
              <option key={item.ref} value={item.version}>
                {item.ref}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{zh.p04.diffTo}</span>
          <select value={after} onChange={(event) => setAfter(event.target.value)}>
            {versions.map((item) => (
              <option key={item.ref} value={item.version}>
                {item.ref}
              </option>
            ))}
          </select>
        </label>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <button
            type="button"
            className="btn btn--primary btn--sm"
            disabled={compare.isPending || before === after}
            onClick={() => compare.mutate()}
          >
            {compare.isPending ? zh.app.loading : zh.p04.diffRun}
          </button>
        </div>
      </div>

      {compare.isError && <ErrorPanel error={compare.error} />}
      {compare.data !== undefined && <DiffResult diff={compare.data} />}
    </div>
  );
}

export const TAB_ITEMS: ReadonlyArray<TabItem<TabId>> = [
  { id: "overview", label: zh.p04.tabs.overview },
  { id: "styles", label: zh.p04.tabs.styles },
  { id: "pageSetup", label: zh.p04.tabs.pageSetup },
  { id: "outline", label: zh.p04.tabs.outline },
  { id: "blocks", label: zh.p04.tabs.blocks },
  { id: "numbering", label: zh.p04.tabs.numbering },
  { id: "placeholders", label: zh.p04.tabs.placeholders },
  { id: "fingerprint", label: zh.p04.tabs.fingerprint },
];

export function TabBody({
  tab,
  detail,
}: {
  tab: TabId;
  detail: TemplateDetailOut;
}): ReactNode {
  const pkg = detail.package;
  switch (tab) {
    case "overview":
      return <OverviewTab detail={detail} />;
    case "styles":
      return <StylesTab styles={pkg?.styles ?? []} />;
    case "pageSetup":
      return pkg === null
        ? <NoRows text={zh.p04.sectionsNoRows} />
        : <PageSetupTab pageSetup={pkg.page_setup} />;
    case "outline":
      return <OutlineTab detail={detail} />;
    case "blocks":
      return <BlocksTab blocks={pkg?.blocks?.blocks ?? []} />;
    case "numbering":
      return <NumberingTab detail={detail} />;
    case "placeholders":
      return <PlaceholdersTab entries={pkg?.placeholders?.entries ?? []} />;
    case "fingerprint":
      return <FingerprintTab detail={detail} />;
  }
}
