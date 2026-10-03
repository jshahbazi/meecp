# Preparing an app package with an external agent

Phase 2 onboarding uses a versioned setup export, a selected private source snapshot, and an explicitly invoked agent preparation recipe. The host never imports source modules, runs a build script, or starts an agent during intake, attachment, or planning. A prepared package runs only during the authorized installation/runtime checks. Package contracts and lifecycle behavior remain defined in [MCPME_DESIGN.md](MCPME_DESIGN.md).

## Preparation templates

| Identifier | Input | Recipe and current limits |
| --- | --- | --- |
| `python-function-v1` | One selected Python callable | [Adapter and generator](../templates/python-function-v1/README.md); private CPython, locked wheels, explicit schemas, CPU-only stdio sessions. |
| `existing-mcp-v1` | Foreground native/Python stdio MCP server or a low-level MCP server factory | [Generator and guidance](../templates/existing-mcp-v1/README.md); exact tools, complete runtime closure, and an authenticated descriptor-configured HTTP wrapper when selected. Unbundled Node remains unsupported. |
| `jevk5-llamacpp-v1` | Raw pinned GGUF and a verified relocated llama.cpp build | [Export-driven model recipe](../templates/jevk5-llamacpp-v1/README.md); automatic manifest/lock generation with exact selected artifact and draft receipt checks. Unknown engines and model profiles remain blockers. |

All three identifiers describe versioned authoring starting points. The host currently supports package schema 1, host API 1, Apple silicon macOS 26+, and tools-only MCP `2025-11-25`. Template availability does not make an arbitrary source compatible. Reference-document search/read services have no supported template in this milestone; Markdown/text briefs can supply setup requirements.

## Export and prepare

Create or resume a draft through Add App or the `imports` CLI. Record the intended named tools, their input/output behavior, synthetic examples, side effects, access requirements, and resource limits before preparation. Missing intent or document role remains unresolved until supplied. Export the current draft:

```sh
umask 077
scripts/mcpme imports export DRAFT_UUID --revision REVISION --json > /absolute/path/to/agent-workspace/setup-export.json
```

The export contains `draft_id`, integer `revision`, and `source` with the exact `revision`, an absolute `snapshot_path`, and selected `files` containing package-relative `path`, SHA-256, and byte `size`. Folder, document and model snapshots use a directory root. Model artifact collections additionally carry their versioned roles and executable flags. Source identity includes selected uncommitted files; generated caches, local environments, and credentials are excluded by intake. Structured requirements and host/template compatibility accompany the source references. No credential values appear in the export.

Use the selected snapshot, not the mutable original checkout. Give the export to an external agent explicitly authorized to prepare this source. Build in that agent's workspace; do not run the generator from host intake or a management route. The agent identifies a narrow callable/service interface, writes a tool specification and access declaration, and invokes the template generator. Neither the user nor the host has to hand-write a package manifest or lockfile for the supported Python, stdio, HTTP factory or pinned JevK5 paths.

For the Python callable generator, a selected `arithmetic.py` exporting `add_numbers(left, right)` and a selected `LICENSE` work with the [synthetic tool specification](../templates/python-function-v1/tool.example.json). The selected source must carry a usable license; preserve that license and attribution rather than inventing one for third-party source. The generator copies and hashes the selected files without executing them, packages the adapter and private runtime, and performs static package validation. The output's `source_revision` exactly equals the setup export's `source.revision`; an agent must not replace it with a recomputed adapter or Git identity.

Preparation requires `uv`, managed CPython 3.12.13, and the committed root `pyproject.toml`/`uv.lock` dependency baseline. Build-time tools remain in the agent workspace. The generated package receives its own materialized interpreter, hash-checked wheel dependencies, `uv.lock`, requirements export, runtime receipt, license files, and complete per-artifact package lock. Runtime launch requires no uv, global Python, Homebrew, editable checkout, or runtime downloads. When adding dependencies, copy the baseline into an agent-owned project, resolve it with `uv`, retain MCP SDK 1.30.0, and supply `--dependency-project`. The generator installs wheels only and rejects another MCP SDK revision.

When starting from a distributed host, copy its `templates/`, `backend/mcpme/`, `pyproject.toml`, and `uv.lock` into the external agent workspace, preserving that directory layout. Use the workspace copy as the `--project` path in the template commands. This gives the generator its static validator and locked dependencies without creating a build environment inside the signed app bundle.

Declared filesystem/network access and side effects must match actual behavior. Access metadata and annotations do not enforce an operating-system sandbox. Programs run as the current user. Packages can declare [typed nonsecret configuration and logical secret names](CONFIGURATION_SECRETS.md); prepared adapters read their selected values from the private `MCPME_SETTINGS_FD` descriptor. Keep material out of source, fixtures, packages, and preparation exports. Detached child processes, adoption of existing services, unsupported callbacks, model artifacts without a supported verified engine and tool profile, and reference-document services remain blocked where their host support is missing. Preserve unresolved requirements and use content-free error codes; do not include source text, production inputs, response bodies, credentials, or exception dumps in preparation diagnostics.

## Synthetic verification

Verify the package with a real locked-SDK MCP client before return. Initialize using the supported revision, compare the full tool contract, call each tool with a synthetic fixture and its expected result, and check invalid input and failure behavior. Function outputs must be JSON objects; the callable adapter uses keyword arguments and validates both input and output. Test relocation by moving the complete package and denying access to the original source. A successful static generator result proves artifact/schema consistency; it does not establish runtime behavior, calibrated model accuracy, publisher trust, or host installation authority.

Keep validation fixtures and compact checks in the external agent workspace. Report counts, schema equality, identity, and content-free outcomes. Avoid saving raw request/response transcripts or build errors. The trusted host independently verifies artifacts and protocol contracts during the eventual authorized candidate checks.

## Return to the draft

An optional manifest `validation.fixtures` array contains at most four approved
synthetic tool calls. Each fixture supplies `tool`, `arguments` and the exact
expected structured JSON object. Static validation checks the declared tool and
its input/output schemas; the authorized candidate check runs the calls before
exposure and rejects an error or mismatched result with `smoke_probe_failed`.
Keep each fixture below 16 KiB and include no production inputs or secrets. Probe
writes during an upgrade use the inactive candidate data clone. The preparation
generator accepts a closed validation specification through `--validation-spec`.
After a generation is verified, restart, rollback and exposed recovery repeat
readiness and protocol checks without replaying business fixtures against
accepted user data.

Changing draft intent, tool requirements or source identity invalidates an old
attachment. Configuration-only edits can explicitly use `imports revise
--carry-attachment`: the host revalidates the sealed package and compatible
settings before carrying it into the new revision. This exception never permits
an old external-agent return to attach to a newer revision.

Attach the complete package to the same draft and integer revision from the export:

```sh
scripts/mcpme imports attach DRAFT_UUID --revision REVISION \
  --package /absolute/path/to/agent-workspace/prepared-package --json
scripts/mcpme install plan --import DRAFT_UUID --revision REVISION --json
```

Attachment statically validates and snapshots the result. It requires the manifest's `source_revision` to match the selected source identity, rejects stale draft revisions, and does not install or run the service. Changes to tools, access, or requirements must be reflected in a revised draft and exact installation plan. Later edits to the agent's output or original checkout do not change the accepted snapshot.

Apply the reviewed plan only within the user's authorized scope. Installation and client registration are separate actions. Once a draft is submitted, follow its linked operation; edits, replacement attachments, draft cancellation, and expiration cannot rewrite an executing candidate. A failed or canceled terminal installation keeps its submitted history and needs a new draft/plan for another attempt.
