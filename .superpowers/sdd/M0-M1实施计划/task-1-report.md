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
fixture. Complex drawing, chart, OLE, text-box and external linked media are classified
as unsupported or excluded; users must explicitly choose supported internal images.
Styles/numbering are retained as sanitized XML and the renderer must consume prototypes
and bindings rather than reparse the original upload.

## Commit

Pending controller integration commit: `feat(registry): freeze cleaned effective template`.
