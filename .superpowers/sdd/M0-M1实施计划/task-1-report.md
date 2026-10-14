# Task 1 report: effective template selection and freeze

## Public contract

`contracts/imports.py` defines `ImportDraft`, `TemplateSelection`, `TemplateVersion`, and
the supporting node, candidate, variable, prototype, cleanup and format-binding models.
`source_sha256` and `source_size` identify the immutable upload. Structure nodes use
`part`, an XML XPath captured during inspection, and a stable `node_id` scoped to the
import. Candidate `status` is independent from selection: `detected`, `unrecognized`,
and `unsupported` are not collapsed into an absence claim. `confirmed_absent` records an
explicit user decision.

Variable keys must be Python identifiers that are not keywords. A variable points to one
complete selected paragraph node. Freeze writes a content-control tag
`df:variable:<key>` and keeps the paragraph/run properties around an empty text run.
Content insertion is represented by `df:insert:content` in the effective document. The
renderer can therefore fill a known marker without searching old text.

`FormatBindings` maps heading levels, body/list/table/image prototypes and the content
insertion part. Table prototypes include a row sidecar taken from the same source table;
the row records its column count for the renderer.

## Frozen package and hashes

The immutable directory contains `effective.docx`, `selection.json`, `contract.json`,
`prototypes/*.xml` (and table row sidecars), and `cleanup-report.json`. The upload is
never copied into the directory. `file_hashes` covers every payload except the contract;
`content_hash` is a canonical JSON digest of the contract content after removing the
identity fields (`template_id`, `version`, `name`) and the digest itself. `effective_bundle`
is the renderer-facing loader: it checks path traversal, exact declared files, every file
digest, and the contract digest without opening the DOCX or collecting semantic nodes.

## Cleanup behavior

The package is rebuilt from an allowlist. Historical text, comments, properties,
embedded files, external relationships, unreferenced media, field caches and unsupported
objects are excluded. Direct paragraph/run/table formatting and section page setup are
retained. Safe page fields (`PAGE`, `NUMPAGES`, `SECTIONPAGES`) are retained without their
cached text. Header/footer relationships selected by a range are remapped; inherited
section references and first/even settings are preserved. An explicitly selected internal
image is copied to a new media part only after relationship and format validation.

Ordinary selected body blocks supply empty sidecar prototypes; the effective body does
not retain old empty tables/paragraphs. Layout blocks, variable containers, selected
images and safe fields remain where their frozen insertion position requires them. TOC
field/cache text is always removed; TOC is not treated as a renderer capability.

## Tests and evidence

`tests/unit/test_effective_templates.py` contains 23 focused tests covering source
immutability, malformed input, detection/selection separation, manual ranges, overlap and
duplicate binding errors, variable format preservation, same-table row prototypes,
unsupported objects, external/unreferenced assets, section relationships, safe fields,
TOC cleanup, package opening, content-control markers, hash stability, source deletion,
tamper detection and version immutability.

RED evidence: before implementation, collection failed with
`ModuleNotFoundError: docfidelity.registry.candidates`. GREEN evidence: the focused suite
passes (`23 passed`), strict mypy passes for contracts/ooxml and owned registry modules,
ruff passes, and `check_forbidden_words.py` reports zero hits. The full unit suite reached
`1477 passed` with two gate failures caused by the temporary literal `"table"` branch;
the branch was removed and the forbidden-word gate now passes. A fresh full-suite rerun
was not repeated after that one-line gate fix.

## Limitations

The allowlist intentionally supports the WordprocessingML structures exercised by the
fixture. Complex drawing, chart, OLE and external linked media are classified as
unsupported or excluded; users must explicitly choose supported internal images.
Legacy VML text-box containers are cleaned and bindable; modern drawing text-box
geometry is not advertised as supported.
Styles/numbering are retained as sanitized XML and the renderer must consume prototypes
and bindings rather than reparse the original upload.

## Commit

Implementation commit: `283a6aa` (`feat(registry): freeze cleaned effective template`).

Final renderer API: import `load_template_version` from
`docfidelity.ooxml.effective_bundle`; `registry.freeze` re-exports it for callers.
`FormatBindings` fields are `heading: dict[int, str]`, `body: str | None`,
`list_paragraph: str | None`, `tables: list[str]`, `image: str | None`,
`insertion_part` and `insertion_marker`. Prototype IDs map to `FrozenPrototype.path`;
table `row_path` is a standalone empty `w:tr`; `columns` is its physical cell count.
Variable markers wrap one paragraph in `w:sdt/w:sdtContent`. Fill runs within that
paragraph and preserve their properties. The insertion marker wraps an empty paragraph;
new blocks belong immediately before that control or inside its content wrapper, before
the final section properties. `StructureKind` is a string enum for inspected node types.

## Review Fix Round 1

The initial TOC limitation and unconditional source-format statements above are
superseded by this round. All seven findings in `task-1-review.md` are addressed:

1. Selected TOC keeps a validated TOC instruction and its layout position in a
   `df:toc:<ordinal>` content control. Simple and cross-paragraph complex fields freeze
   as a safe `w:fldSimple` with `w:dirty=true`; old entries, nested field caches and old
   bookmarks are cleared while paragraph and run properties remain. Settings request
   `updateFields=true`. `FrozenToc(part, marker, instruction)` is in
   `FormatBindings.toc`. Only numeric level-range switches and the known flag switches
   are accepted; switches containing old bookmark names or arbitrary text are rejected.
   Tests: `test_review_selected_toc_retains_safe_field_and_empty_formats`,
   `test_review_toc_cached_run_format_is_retained`,
   `test_review_unsafe_toc_switches_are_rejected`.
2. `TemplateSelection.formats` persists `FormatSelection(page_setup, heading, body,
   numbering, tables)`, all defaulting to true. False explicitly selects the frozen
   neutral baseline: A4 with 1440-twip margins, independent DFNeutral styles, Arial
   11-point body and 14-point headings, plain list paragraph, and a neutral table.
   Turning off numbering clears paragraph AND style `numPr`, and retains an empty
   numbering part/relationship for new IR list semantics. Neutral headings 1..9, body
   and lists are frozen even when the source has no corresponding prototype.
   Tests: the five parameterized format-toggle cases,
   `test_review_explicit_neutral_formats_exist_without_source_prototypes`,
   `test_review_neutral_body_does_not_inherit_source_default_style`.
3. Numbering `lvlText` retains number placeholders and punctuation/symbols only;
   business words are removed and counted in `CleanupReport.cleared_numbering_labels`.
   `w:tab/@w:pos` and leader survive cleanup. Tests:
   `test_review_numbering_scrubs_business_labels`,
   `test_review_numbering_tab_positions_are_retained`.
4. Heading inspection follows style `outlineLvl` and the `basedOn` chain with cycle
   protection. The custom heading is a heading prototype, not the body prototype.
   Test: `test_review_custom_heading_inherits_outline`.
5. A sanitized theme and its document relationship close retained theme font/color
   dependencies. Theme extensions, free text and business names are excluded.
   Test: `test_review_theme_dependencies_are_closed_and_cleaned`.
6. Excluded complete sections no longer produce section-break paragraphs. Remaining
   sections resolve inherited default/first/even header and footer references, retaining
   their own first-page and odd/even settings. Tests:
   `test_review_cover_cancel_removes_section_break`,
   `test_review_remaining_section_inherits_first_even_headers`.
7. The support index now includes unsupported content such as footnotes/endnotes;
   explicitly selecting its block fails, while the default skipped block and related
   note part are excluded. Test:
   `test_review_selected_footnote_is_rejected_and_unselected_excluded`.

### Renderer Coordination

`FrozenPrototype.column_policy` is `fixed` by default. Source table prototypes must
declare a positive `columns` count. The explicitly selected neutral table uses
`column_policy=flexible`, `columns=None`, a same-table one-cell row sidecar, and a fixed
baseline width equal to the frozen final section's usable width. The renderer may expand
that same neutral row's cell by IR column count; fixed source tables still reject column
mismatch. Test: `test_review_explicit_neutral_table_exists_without_source_table`.

### Execution Evidence

All commands used `D:/PythonProject/docfidelity/.venv/Scripts/python.exe` in the
isolated worktree. The first review suite run was RED: `12 failed` (missing format
contract plus the six reproduced behavior failures). Additional RED runs demonstrated
lost TOC run color, missing neutral headings, inherited-default-style exposure, nine
lost numbering tab positions, and the missing explicit neutral table. The final focused
command was:

```text
python -m pytest tests/unit/test_effective_review.py tests/unit/test_effective_templates.py --tb=short
44 passed
```

The earlier scoped legacy registry/OOXML command returned `198 passed`. Strict mypy of
all contracts/OOXML and owned registry modules returned `Success: no issues found in
35 source files`. Owned-file ruff checks passed. `lint-imports` with `PYTHONUTF8=1`
returned `16 kept, 0 broken` (the default Windows GBK mode first failed to decode its
UTF-8 configuration). Forbidden words, line limit and kernel-no-Web gates passed.
Full unit/API/schema integration checks are reserved for the controller as instructed.

No render, server or generated schema file was changed in this round.
