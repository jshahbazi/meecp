# Automatic app setup

Status: implemented workflow, 2026-10-06. Live model reliability is not established by the synthetic checks.

## User flow

1. In **Manage Apps → Add App**, choose the project folder and describe what the app should do.
2. Choose a saved provider and model. Configure credentials and use **Test Connection** in **Settings → LLM Providers**.
3. Choose **Prepare App**. This saves the instructions, creates a current preparation plan, and starts the host-owned workflow.
4. Follow the progress while mcpme generates the adapter, resolves dependencies when needed, builds the app, and tests its tools. Failures show the failing condition and a next action.
5. Review the verified app's tools, access, and configuration, then choose **Add App**. Preparation does not install the app.

The folder is selected as a unit; there is no per-file checklist in the normal flow. All eligible text files are included automatically. Binary files remain local; the provider receives their paths, sizes, and content hashes. The transfer disclosure identifies the provider, model, file counts, and maximum paid requests before preparation starts. Loading options and plans does not contact the provider.

**Refresh Folder** creates a new snapshot while preserving the app name and instructions. The previous draft remains saved. It is disabled while a setup owns active work. **Advanced setup** exposes the existing guided, separate analysis, and external coding-agent paths; recipe authoring is outside the normal Add App flow.

## Host contract

The automatic path currently supports the admitted Python adapter recipes: projects with a dependency lock, and projects requiring dependency resolution. Other app kinds retain their existing manual workflows. Ordinary Python projects receive the exact bundled MCP SDK and its dependency closure automatically. The original project and lock remain bound in the package; conflicting dependency versions stop preparation with a specific error. Installed dependency license texts are retained from their wheel or the selected source, and the SDK overlay records the host artifact hashes.

`POST /v1/app-setups/plans` binds the import revision and source snapshot to the selected provider, model, recipe, and execution limits. The native view verifies the returned provider identity and folder counts. `POST /v1/app-setups/start` accepts the plan ID, digest, and an idempotency key. A retried uncertain start reuses that key.

`GET /v1/app-setups/jobs/{id}` returns durable progress and a bounded diagnostic containing a code, explanation, and next action. `POST /v1/app-setups/jobs/{id}/cancel` requests cancellation; navigation remains blocked until owned local work settles. A host restart marks unfinished work interrupted rather than replaying provider calls.

The workflow uses existing host-owned proposal validation, dependency resolution, confined builds, runtime probes, and package verification. The model proposes files and typed setup data; it receives no arbitrary shell or ambient filesystem tools. The host may request missing proposal details, then repair the adapter after an eligible build or probe failure. Once accepted, tool schemas and test fixtures remain fixed through adapter repair. A `ready` job returns the verified, attached draft for the separate installation review.

## Current limits

| Scope | Limit |
| --- | --- |
| Provider source selection | 32 text files, 128 KiB combined; details for 32 other files |
| Provider calls | 8 total, including corrections and repair |
| Provider transfer | 2 MiB across the workflow |
| Output | 8,192 tokens per call |
| Repair and preparation | At most 2 correction/repair phases and 2 preparation attempts |
| Complete workflow | 1,200 seconds |
| Each preparation | 600 seconds, 4 GiB disk, 2,048 MiB memory |
| Dependency resolution | 300 seconds, 2 GiB disk |
| Dependency network | `pypi.org`, `files.pythonhosted.org` |

Truncated inventories and oversized selections fail before dispatch; the host does not silently choose a subset. The request limit is not a monetary ceiling, and cancellation cannot establish that remote usage or charges have stopped. Signed release admission, provider admission, and runner availability remain required.

## Verification

Native contract checks cover local-only preflight, save-and-replan before dispatch, idempotent retry, cancellation settlement, stale response rejection, concrete error display, and one-time ready handoff. Synthetic workflow checks exercise the bounded control flow. An opt-in hotdog pipeline check used the real bundled ONNX model, an intentionally broken adapter, a failed confined test, and an adapter-only repair that passed the unchanged test. Its provider responses were simulated; it does not establish success rates for live LLM-generated apps.

Implementation: `Sources/McpmeMenuBar/AppSetupView.swift`, `backend/mcpme/app_setup.py`, and `backend/mcpme/app_setup_contracts.py`.
