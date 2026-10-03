# Programmatic preparation agent backend

Status: bounded proposal backend checkpoint, 2026-10-03. A5 remains open. The installed host and native preparation routes do not expose this module yet. No real provider generation run has passed at this checkpoint: API credential presence checks were negative and the local Ollama endpoint was unavailable. Installed CLI binaries do not establish suitable programmatic access or logging behavior.

## Public Python seam

`AnalysisRequestV1` in `backend/mcpme/preparation_agent_contracts.py` implements `analysis-request-v1`. It binds an import UUID, draft revision, existing source revision, selected UTF-8 file paths/byte counts/SHA-256, provider backend/model/opaque credential reference, authority UUID/expiry, user intent, permitted operations and usage limits. Unknown fields, duplicate selected paths, traversal paths and execution tools fail validation. Source input is an exact in-memory map supplied by the host's existing capture layer. The backend never discovers files or follows source paths.

`AgentCoordinator(store, backend, validate_current)` accepts a trusted backend and a synchronous host callback. The callback **must raise** unless the exact request digest, current import revision/source identity, source-transfer scope, provider/model and authority are still approved. A request object cannot authorize itself. The coordinator invokes this callback at creation, before each dispatch, after each response, and at publication. The integration owner must implement that callback against host-owned authority and draft state before exposing a route.

1. Validate and review the request with the owner; persist host authority over its digest.
2. Call `coordinator.create(request)` once; it consumes that authority UUID.
3. Await `coordinator.run(attempt_id, request, selected_source_bytes)`.
4. Poll `store.get(attempt_id)` for bounded state/usage receipts. Await `coordinator.cancel(attempt_id)` to settle local I/O before returning cancellation success.
5. Read `store.proposal(attempt_id, request, validate_current)` for typed untrusted artifacts.

Create one `AgentAttemptStore` under a host-owned mode-0700 state directory. Keep it open for the coordinator's lifetime. Call `close()` only after active runs settle. Its exclusive lock prevents another live coordinator from recovering the same journal. The database and lock are owner-only files; the host must supply a trusted parent directory. On restart, unfinished attempts become interrupted, their generation changes, and calls are never replayed. Request content, source text, credentials and raw provider responses are not journal entries. The artifacts table intentionally stores only requested typed proposal files for review; it is private artifact storage, not a provider transcript.

## Concrete transport and orchestration

`OpenAIResponsesBackend(resolve_credential)` uses the fixed `https://api.openai.com/v1/responses` origin. The host resolves the opaque credential reference in memory. The transport does not read environment variables, CLI credentials or Keychain itself. It disables environment proxies and redirects, supplies no remote tools, requests stateless non-background responses and structured JSON, bounds response bytes, and rejects refusals, incomplete results, tool calls, malformed usage and invalid proposal paths. Errors expose fixed codes. The optional test transport argument is for trusted test code and is not a client-facing configuration field.

Interface analysis, dependency closure and fixture proposals run concurrently within the selected parallel limit. The adapter task receives those typed proposals and selected source, then proposes integration files. Each task can only return files under its own `proposed/<role>/` namespace. Integration rejects path conflicts and binds every artifact set to request digest, attempt and generation. No source or proposal code is executed. Imported text cannot supply tools, installation authority, credentials, public ports or catalog admission.

The interface follows the official [Responses creation API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create) and [structured outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs). `store=false` controls response storage in this API; it is not a claim of zero provider retention. Only providers with the required structured-output behavior can be selected after actual compatibility verification.

## Bounds and recovery

Each attempt permits exactly four provider calls with no automatic retry, at most three concurrent calls, a whole-attempt deadline, a maximum sum of outbound request-body bytes, a per-call output-token ceiling and a final artifact-byte ceiling. The journal admits at most four pending attempts and retains at most 1,024 attempt records. Reservations are persisted before dispatch. Input is bounded in bytes; reported input/output token usage is accounted after each successful response. The implementation does not claim an exact input-token preflight ceiling or a monetary limit. Unknown monetary-limit fields are rejected; monetary admission needs separately verified rate information and a spend-reservation contract.

Cancellation, disconnect or host restart may leave remote billing unknown. Full output reservations for calls without reported usage remain recorded; the module never equates local socket closure with remote inference cancellation or zero spend. `local_io_settled` describes local I/O only. Cancellation closes and awaits every local request task before terminal cancellation. The fixed synchronous provider mode prevents remote tool/process execution. A failed lane cancels and awaits its siblings. Stale draft revisions, revoked authority, expired requests and old generations cannot publish a proposal.

The host must independently validate proposed descriptors, source/adapter digests, dependency closure and requested execution scope. Any build or probe requires the existing completed descriptor and execution authority. Import attachment must recheck its normal job/revision/receipt binding. This proposal API has no attachment, install or recipe-enablement method. A successful proposal is not a runnable package or verification receipt.

## Reproduction and remaining A5 evidence

Run contract and transport simulations with:

```sh
uv run --locked pytest -q tests/test_preparation_agent.py
```

Run the opt-in real provider probe only with a configured `OPENAI_API_KEY` and explicit compatible model:

```sh
uv run --locked python scripts/verify_preparation_agent.py --model SELECTED_MODEL
```

The probe generates a fresh unfamiliar synthetic source function, transfers only that source and intent, and prints identity/count/usage receipts. It never prints or logs source, credentials or model responses. The temporary private proposal store is removed on exit. Missing credentials produce `provider_credential_unavailable` and a nonzero exit. Mock HTTP tests are explicitly simulations and cannot close A5.

Full A5 still requires an actual compatible provider run, independently verified generated behavior, installed-host authority/candidate integration, and end-to-end cancellation/restart/stale-admission evidence across that integration. Native capability advertisement must remain disabled until those checks pass.
