<div align="center">

<img src="docs/assets/banner.png" alt="docfidelity: template-driven DOCX generation with actual document outputs" width="100%" />

# docfidelity

**Reuse selected formatting from an existing Word document and generate a new document from new source material, without copying the old business content.**

[![CI](https://github.com/Hjiassen/docfidelity/actions/workflows/ci.yml/badge.svg)](https://github.com/Hjiassen/docfidelity/actions/workflows/ci.yml) [![Version](https://img.shields.io/badge/Version-0.0.1%20Alpha-orange.svg)](CHANGELOG.md) [![Python](https://img.shields.io/badge/Python-3.11--3.12-3776AB.svg?logo=python&logoColor=white)](pyproject.toml) [![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](deploy/compose.yml) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

[简体中文](README.md) | [English](README.en.md)

[Quick Start](#quick-start) | [Output Preview](#output-preview) | [User Guide](docs/guide/README.md) | [Deployment](deploy/README.md)

</div>

V2 is being implemented according to the [redesign](docs/设计/产品重构方案.md). M0/M1 implement effective-template import, selection, cleanup, freezing, and rendering APIs; see the [API guide](docs/guide/effective-templates.md). The quick start and screenshots below show the running legacy workflow. New content generation and the browser workspace belong to M2 and M3; see the [roadmap](ROADMAP.md) and [acceptance notes](docs/验收/P0-验收自检.md).

## Quick Start

With Git, Docker, and Docker Compose installed, start the browser workspace in three commands:

```bash
git clone https://github.com/Hjiassen/docfidelity.git
cd docfidelity
docker compose -f deploy/compose.yml up --build
```

Open <http://127.0.0.1:8000>, choose the one-click sample project action, then submit a build to inspect reports and download three DOCX files. **No API key, private templates, or Office required.** The first image build needs network access to fetch base images and dependencies.

The API serves the frontend, a separate worker processes jobs, and a named volume stores runtime data. See [deployment](deploy/README.md) for background startup and persistence, or [run from source](#run-from-source) for Python examples and the source-based workspace.

The current version is **0.0.1 Alpha**. See [limitations](#limitations) and the [roadmap](ROADMAP.md) for the current scope and remaining work.

## Output Preview

![Three actual DOCX layouts generated from the self-authored sample sources](docs/assets/outputs.png)

From left to right: sectioned design, summary design, and operating procedure. These are actual outputs of the bundled, self-authored Chinese examples, showing page 5 rendered in Word on October 8, 2026. Pagination depends on the rendering environment; rule-based document generation itself does not require Office.

Full-resolution pages: [sectioned design](docs/assets/output-sectioned.png) · [summary design](docs/assets/output-summary.png) · [operating procedure](docs/assets/output-procedure.png). See the [examples](examples/README.md) for inputs and reproduction steps.

## Overview

`docfidelity` separates fact ingestion, content composition, and DOCX rendering. It extracts facts from external sources, creates content IR according to a document profile, and injects that content into a registered template. Domain models and document types are declared in configuration rather than embedded in the engine.

The core engine, HTTP API, and browser workspace are implemented using Python, Pydantic, OOXML / python-docx, FastAPI, React, and TypeScript. Model integration uses the optional OpenAI Agents SDK. Evidence from Docker, browser workflows, the configured model, and Word pagination is documented in the [acceptance notes](docs/验收/P0-验收自检.md).

## Contents

- [Use Cases](#use-cases)
- [Features](#features)
- [Architecture](#architecture)
- [Run from Source](#run-from-source)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Development](#development)
- [Deployment](#deployment)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [Limitations](#limitations)
- [License](#license)

## Use Cases

- Generate documents from established Word templates while retaining styles, table prototypes, and source traceability.
- Produce different document structures from the same source facts, using profiles to define sections and projections.
- Manage templates, project configuration, build jobs, and quality adjudication in a browser.
- Keep deterministic document processing separate from optional model decisions, including when the model is disabled or unavailable.

## Features

| Feature | Description |
| --- | --- |
| Template fidelity | Extract template contracts during registration; reuse the base document and each table's own prototype, with quality checks against normalized style structures. |
| Configuration-driven layouts | Domain models and document profiles define fields, sections, grouping, and projections within the capabilities of existing connectors and renderers. |
| Traceability | Content IR retains fact sources; jobs include stage events, quality reports, and checkpoints. |
| Quality adjudication | Rule findings and items requiring confirmation enter an adjudication queue. Blocking findings must be resolved before treating a document as final. |
| Browser workspace | Register templates, configure projects, track builds, adjudicate findings, and download artifacts through a same-origin HTTP API. |
| Separate worker | The API and worker share the same release and persistent storage, supporting stage resumption and event replay. |
| Optional models | Disabled by default. Unavailable models fall back to rules and flag items for confirmation; installed rule-based builds can run offline. |
| Reproducible builds | With frozen inputs, models disabled, and Office disabled, bundled DOCX examples and structural snapshots are stable. Real pagination requires a fixed rendering environment. |

## Architecture

```text
Template files -> registry -> template contracts --+
Fact sources   -> ingest   -> fact bundle ---------+-> compose -> ContentIR -> render -> DOCX -> qa
Domain models and document profiles --------------+                                       -> report

Browser workspace -> HTTP API -> pipeline -> core stages
                                    ^
                              separate worker / CLI
```

`contracts` is the dependency root. `compose` does not manipulate OOXML. Templates are parsed during registration; generation reads their contracts. Agents make decisions through tools, while deterministic stages perform document operations. The base installation excludes web, database, and model SDK dependencies. See [AGENTS.md](AGENTS.md) for module boundaries.

## Run from Source

### Build the Python Examples

Python 3.11 is the minimum; the currently supported range is 3.11–3.12. Run from the repository root:

```bash
python -m venv .venv
# Linux / macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e .
python examples/build_demo.py
```

The self-authored sources produce three documents: sectioned design, summary design, and operating procedure. DOCX files are written to `var/examples/jobs/*/artifacts/`, with quality reports in each job's `work/report.json`. Once installed, these examples require no model, database, or Office. See the [example guide](examples/README.md) for arguments and inputs.

### Start the Browser Workspace

Node.js ≥ 22.12 is also required:

```bash
python -m pip install -e ".[server]"
npm --prefix web ci
npm --prefix web run build
python -m uvicorn server.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Source-based startup uses an in-memory metadata repository by default, which is cleared on restart. Follow [deployment](deploy/README.md#从源码运行) to configure a database, storage root, and initial sample templates.

## Configuration

Server settings use `DOCFIDELITY_` environment variables or `.env`; see [.env.example](.env.example). Project configuration, domain models, and document profiles define document content and structure; see [examples](examples/README.md).

| Setting | Purpose |
| --- | --- |
| `DOCFIDELITY_STORAGE_ROOT` | Storage root for templates, sources, jobs, and artifacts. |
| `DOCFIDELITY_DATABASE_URL` | Metadata database connection; source-based startup defaults to an in-memory repository. |
| `DOCFIDELITY_START_WORKER` | Enables an embedded worker for single-process startup; Compose uses a separate worker. |
| `DOCFIDELITY_SEED_DEMO_TEMPLATES` | Registers self-authored sample templates when the template library is empty. |
| `DOCFIDELITY_DETERMINISTIC_MODE` | Enables deterministic mode and avoids implicitly probing local Word. |
| `DOCFIDELITY_MODEL_ENABLED` | Model switch; defaults to `false`. |
| `DOCFIDELITY_MODEL_BASE_URL` / `DOCFIDELITY_MODEL_NAME` / `DOCFIDELITY_MODEL_API_KEY` | Model endpoint, identifier, and API key. |

Enabling models also requires `.[model]`, an explicitly configured and authorized OpenAI-compatible endpoint, and a real baseline for each decision point matching the model and configuration. There is no required model size or deployment location. Changes to the model, samples, or routing require re-evaluation. Unverified decision points fall back to rules and are flagged for confirmation. See [models and fallback](docs/guide/README.md#模型与降级).

## Project Structure

```text
src/docfidelity/      # Independently installable engine
  contracts/         # Contracts and error models
  registry/          # Template registration and contract queries
  ingest/            # Fact source ingestion
  compose/           # Content composition and IR
  ooxml/             # OOXML operations and normalization
  render/            # DOCX rendering
  qa/                # Quality rules and reports
  model/ agents/     # Optional model adapters and decision tools
  pipeline/ builder/ # Stage execution, checkpoints, and CLI
  platform/          # Filesystem, logging, and infrastructure
server/              # HTTP API, persistence, and worker
web/                 # React + TypeScript workspace
deploy/              # Dockerfile, Compose, and deployment guide
examples/            # Self-authored sources, profiles, and build examples
demo-templates/      # Self-authored template assets
tests/ scripts/      # Regression tests and engineering checks
docs/                # Requirements, design, formats, and guides
```

## Development

```bash
python -m pip install -e ".[dev,server,model]"
ruff check .
ruff format --check .
mypy src/docfidelity server
lint-imports
pytest -m "not e2e"
npm --prefix web ci
npm --prefix web run build
npm --prefix web test
```

CI separately verifies the core-only installation, full dependencies, frontend contracts, distribution packages, and Docker startup with an empty volume. See [contributing](CONTRIBUTING.md) for the complete checks, generated contract updates, and commit conventions.

## Deployment

The default deployment is for a single team's self-hosted use. It binds to loopback and has no built-in authentication. Before exposing it to other machines, configure a reverse proxy with authentication and TLS.

Compose runs the API and separate worker from the same image, storing data in the `df-data` named volume. Stopping containers retains data; `down --volumes` deletes the volume. Source deployments require persistence and migrations to be configured separately. The core wheel does not include the server package. See [deployment](deploy/README.md) for commands, backups, and operations.

## Documentation

The README is available in Chinese and English. The application UI, bundled examples, and detailed guides are currently in Chinese.

| Document | Contents |
| --- | --- |
| [User guide](docs/guide/README.md) | Builds, templates, profiles, models, and quality reports. |
| [Example guide](examples/README.md) | Self-authored inputs, three profiles, and repeat builds. |
| [Deployment guide](deploy/README.md) | Docker, source persistence, worker, and operations. |
| [Requirements](docs/需求/需求.md) | Product scope, constraints, and acceptance criteria. |
| [Detailed design](docs/设计/开发设计文档.md) | Module boundaries, contracts, and implementation design. |
| [Format specifications](docs/spec/README.md) | Public contracts for template packages, profiles, and content IR. |
| [Architecture decisions](docs/design/adr/README.md) | Key design choices. |
| [Acceptance notes](docs/验收/P0-验收自检.md) | Evidence, unresolved criteria, and verification limits. |
| [Roadmap](ROADMAP.md) | Remaining work, dates, dependencies, and completion criteria. |
| [Contributing](CONTRIBUTING.md) | Development environment, tests, and submission requirements. |
| [Security policy](SECURITY.md) | Vulnerability reporting and sensitive data handling. |
| [Governance](GOVERNANCE.md) / [Code of conduct](CODE_OF_CONDUCT.md) | Project decisions and community collaboration. |
| [Changelog](CHANGELOG.md) | Changes and migration notes. |

## Contributing

Report bugs and suggestions through [GitHub Issues](https://github.com/Hjiassen/docfidelity/issues), using only self-authored or sanitized reproduction data. Read [contributing](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md) before contributing code, connectors, profiles, tests, or documentation. Report security issues privately according to the [security policy](SECURITY.md).

## Limitations

Template fidelity is checked against normalized style structures and template contracts; it does not guarantee byte-for-byte preservation of an entire DOCX. Actual pagination depends on Word, fonts, and the printing environment. Structural snapshots do not replace pagination measurements.

The default build supports model placement of content without an explicit slot binding, gated by an explicit admission policy and a deployment-matched baseline. The configured model passed `10/10` placement cases and supplied five adopted decisions in a real default build. Other decision points still fall back because confidence is missing; models remain disabled by default. See the [user guide](docs/guide/README.md) for configuration and the [acceptance notes](docs/验收/P0-验收自检.md) for remaining gaps. Review quality reports and items requiring confirmation before approving generated documents. Fixed reports from `scripts/serve_dev.py` are not acceptance evidence.

Word COM and PDF visual inspection are optional. PyMuPDF is available under the AGPL or a commercial license; review the licensing implications before installing the `office` extra. See [NOTICE](NOTICE). Keep `.env`, private templates, source facts, and runtime data out of version control.

## License

Code and self-authored examples are licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party components and optional capabilities.
