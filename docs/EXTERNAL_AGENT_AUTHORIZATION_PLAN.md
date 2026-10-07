# Native authorization for external management agents

Status: proposed implementation plan. No runtime behavior described as proposed here is implemented by this document.

Date: 2026-10-07.

## Objective and decisions

Let an ordinary MCP client such as Claude Code launch mcpme, request management access, and receive authorization through the native app. The normal exported connection must contain no credential-file path, bearer token, or secret environment variable. Approved connections should be remembered across client sessions and host restarts, with owner-visible revocation.

Keep the existing stdio facade and private Unix transport. Retain the current grant scopes, per-request authorization, and exact preparation, resolution, and installation bindings. This work changes how authorization is obtained and managed; it does not add unrestricted execution or grant external clients owner authority.

The selected persistence model is a remembered connection policy plus fresh, short-lived session credentials. Persist consent and its scope ceiling. Do not persist an indefinitely reusable external-agent bearer as the remembered identity.

## Current implementation

- `backend/mcpme/__main__.py` rejects `management connect` without a credential file and includes that file reference in `management export` output. Its generic `call()` helper selects the owner token when a credential file is absent. Removing the argument without separating these code paths would grant unintended owner authority.
- `backend/mcpme/management.py` implements a static typed tool catalog and dispatches calls using the supplied file credential. Its stdio process is separate from the resident supervisor.
- `backend/mcpme/grants.py` and `backend/mcpme/management_workflow_grants.py` provide expiring, revocable grant records and exact operation bindings. Credential-file publication is coupled to issuance today.
- `backend/mcpme_local_transport.py` verifies private Unix socket ownership and permissions. Management clients have no TCP fallback.
- `Sources/McpmeMenuBar/main.swift` already holds the owner's manager connection, performs authenticated private requests, polls host state, and fences callbacks against host replacement. Native alerts and review sheets already exist.
- Internal Pi setup now runs through a separate owner-only `/v1/pi/*` API and workspace/process broker. The Add App UI defaults to Pi and retains **Have your agent do it** for external preparation. Pi does not use management MCP grants.
- No pending external-connection request queue or native management authorization UI currently exists.

See [Management MCP](MANAGEMENT_MCP.md), [mcpme design](MCPME_DESIGN.md), [management preparation workflows](MANAGEMENT_PREPARATION_WORKFLOWS.md), and [Pi setup](PI_SETUP.md) for the implemented contracts.

## Compatibility with updated main

Reviewed on 2026-10-07 against `main` commit `38a109d1500bbbd93d393e890072994b8b718a38`, which merges Pi app setup in PR #41. The original plan was based on `f6ea59f73e5a50575a83a2285c21288a54cf7088`. The authorization architecture remains applicable. Implementation must include the Pi access boundary and native coordination rules below.

| Updated area | Evidence in the reviewed main | Consequence for this plan |
| --- | --- | --- |
| Management facade, CLI, grants, and private transport | `management.py`, `__main__.py`, `grants.py`, `management_workflow_grants.py`, and `mcpme_local_transport.py` are unchanged from the original base. | Keep the existing authorization refactor, bootstrap channel, remembered policy, and exact-action grant design. |
| Internal Pi setup | `supervisor.py` declares `pi_setup.credentials` as `owner_only`; `pi_setup_routes()` rejects a non-owner or any principal with a grant ID before route dispatch. | All `/v1/pi/*` routes remain owner-only. No existing exact preparation/resolution grant authorizes a Pi job. |
| Add App and connection exports | `main.swift` retains external instructions/specification export in **Have your agent do it** and per-app **Copy Connection**. | Add a distinct management connection export and saved-policy view. Do not attach connection consent to a setup draft. |
| Questions and settings | `PiSetupView.swift` holds transient question/credential state and its own epoch; app requests retain host-generation fencing. | Authorization needs its own presentation epoch and must preserve Pi input, provider settings, and installation review. |
| Supervisor shutdown and installed stdio processes | The host joins Pi shutdown with other runners and adds resource admission/listener checks for installed stdio packages. | Bootstrap shutdown must close its admission and settle sessions without bypassing existing process cleanup or package resource policy. |

External setup remains the existing import/attach/plan/apply workflow, including separately granted preparation/resolution execution where supported. An external agent can prepare a package in its own workspace and return it through that workflow. The popup changes its management authorization; it does not make the agent a caller of Pi's internal coding tools.

Preserve the owner-only fence for Pi settings, import creation through `/v1/pi/imports`, job creation/inspection/listing, answers, and cancellation. Neither base sessions nor exact-action credentials may configure the Pi provider, read Pi questions/credentials, start Pi work, or invoke its `run_command`/process broker. A future external-to-Pi delegation feature would require a separate explicit authority contract and is outside this plan.

### Review verification

The following existing compatibility tests passed on the reviewed main source after merging it into this documentation branch:

```sh
uv run --locked pytest -q \
  tests/test_mcpme_management_cli.py \
  tests/test_mcpme_management_mcp.py \
  tests/test_mcpme_grants.py \
  tests/test_management_workflow_grants.py \
  tests/test_local_transport.py \
  tests/test_pi_setup.py::test_pi_routes_require_owner_host_and_origin
```

Result: **156 passed in 71.88 seconds**, with 42 temporary-directory cleanup warnings involving protected synthetic bundles. No runtime source or tests were changed by this review. The selected Pi test checks unauthenticated access, a preparation grant against settings/jobs, owner access, and Host/Origin rejection; it does not establish the future all-route/all-grant matrix. Source inspection confirms the shared Pi principal fence. New popup, session, modal coordination, renewal, and installed Claude Code behavior remain unimplemented and unverified.

## User flow

1. In mcpme, choose **Copy Management Connection** from management connection settings. mcpme creates an unapproved connection profile and exports the installed launcher plus a public profile ID. Adding that configuration does not approve access. The existing per-app **Copy Connection** and Add App's **Have your agent do it** actions keep their separate purposes.
2. Claude Code launches the executable. MCP initialization and static tool discovery complete immediately.
3. mcpme displays a native request: the reported client name, observed launch identity, requested access, and the meaning of remembering it. The proposed setup scope is **Inspect apps and create setup plans**. The owner can approve, deny, or reduce the requested scope. Remembering is selected by default, matching the requested product behavior.
4. Approval records the selected policy and gives the connected facade a fresh scoped session credential over private IPC. Credential material stays inside mcpme processes.
5. On a later connection, mcpme re-evaluates the saved identity mode, installation/account, and requested scope. A passing policy may automatically approve a new session within that scope. This does not authenticate a client merely because it reports the saved name. Changed verified identity or broader requested scope produces another popup.
6. Running preparation/resolution or applying an installation produces a separate native review of the exact plan. Approval creates the existing exact operation authority. The connected agent never needs to copy a grant file.
7. **Approved Connections** shows saved profiles and active sessions. **Disconnect** ends a session; **Revoke Access** disables the policy and its derived active authority.

Proposed exported entry, with placeholders for the actual installed launcher and a generated public ID:

```json
{
  "mcpServers": {
    "mcpme-management": {
      "command": "<absolute-installed-launcher>",
      "args": ["management", "connect", "--profile", "<public-profile-uuid>"]
    }
  }
}
```

The profile ID selects a remembered registration. It is not a bearer credential and is insufficient to authorize a connection by itself. Resolve the containing installation and state directory before looking up the profile. Default-state installations use the current CLI default and need no state-directory argument; a nondefault export retains its explicit state selector. Do not search arbitrary state directories or accept an ambiguous profile lookup.

Bare `management connect` can request one-connection approval without a profile. Durable remembering requires the stable profile exported by mcpme; do not discover a saved approval by matching only a client-supplied name.

## Remembered identity and trust boundary

Vanilla MCP stdio does not supply authenticated application identity. `initialize.clientInfo` is client-supplied display metadata. The mcpme bridge executable is shared by every integration. A public UUID, reported name, parent PID, or generic Node/Python executable alone cannot establish a unique trusted client.

Preserve the documented same-macOS-user trust boundary. This is consent and scope control for local integrations, not isolation from another process with equivalent account permissions. Do not claim the popup cryptographically authenticates Claude Code.

Implement a versioned launch-identity resolver and test it before committing to automatic remembered approval:

- Obtain bridge peer identity and process ancestry from the OS, rather than trusting fields supplied in the bootstrap request. Validate liveness and process start identity to address PID reuse and parent exit races. Ancestry/PID observations bind a live request; they are not independent authentication factors.
- For a signed native launcher, evaluate its designated signing requirement and resolved executable identity. Define which normal signed updates retain approval and which publisher/path changes require review.
- For interpreter-based launchers, a shared interpreter is insufficient. Evaluate whether the invoked entry point and its integrity can be observed dependably without collecting full command lines or secret arguments. Never guess when attribution is ambiguous.
- If reliable client attribution is unavailable, offer approval for the current connection. Remembering in this case requires a separate explicit owner choice of **Remember this integration for this macOS account**. Bind that policy to the particular profile, installation, and account; it is not a global allowlist. Never label it as verified application identity. Its automatic access is limited to inspection and planning; executing a plan still requires native review.
- Bind every policy to the mcpme state-installation identity. Copies of configuration targeting another installation require approval there.

Do not require Claude Code to implement a new keypair or credential protocol. Such a requirement would undermine compatibility with ordinary MCP clients. A stronger authenticated-client enrollment protocol can be considered separately if a client supports it.

## Authorization architecture

### Request-only bootstrap channel

Add a dedicated private Unix listener, created by the resident supervisor, for connection bootstrap. It exposes only bounded request/status/settlement frames, not manager routes or management tools. Accept these frames exclusively on this listener, never on a TCP port, manager HTTP route, or header-claimed private context. The server verifies the peer UID against the state owner, and the client verifies socket ownership and permissions. Reuse the private transport validation rules; no TCP/proxy fallback.

Publish a versioned owner-private bootstrap discovery record containing only socket location, installation identity, generation, and liveness metadata. The bridge validates that record against the already resolved installation; it does not read the owner-token connection file to discover authorization. State-installation identity, verified socket/peer properties, and generation determine transport provenance. Host/Origin headers do not establish this provenance.

This is the single deliberately credential-free entry point. It can ask for consent but cannot issue authority. Do not demand a preexisting bearer or inherited secret to request the first popup: an ordinary client directly launches the executable and has no such secret to provide.

Keep the requesting IPC connection open until delivery or settlement. Bind each request to the live connection, peer process identity, supervisor generation, public profile, requested scope digest, and finite deadline. Request IDs and profile IDs do not authorize token retrieval on another connection. On reconnect, create a new request rather than recovering a previous handoff by identifier.

Pending requests are ephemeral, bounded, and rate-limited per profile and account. Deduplicate using canonical profile ID, live IPC connection identity, scope digest, installation identity, and generation. Queue native dialogs, show one at a time, and limit repeated denied requests. Request metadata is a typed schema with bounded labels; it contains no raw client prompts, evidence, full command lines, arbitrary explanatory text, or credential material.

### Owner decision channel

Add owner-authenticated manager routes for listing requests, inspecting their sanitized summaries, approving, denying, listing saved policies, disconnecting sessions, and revoking policies. Scoped external credentials cannot call the owner decision routes. The unauthenticated bootstrap listener must not expose them.

Approval atomically revalidates request state, process/connection liveness, generation, policy revision, scope digest, and any exact plan binding. A duplicate decision cannot issue additional authority. Deliver the resulting credential only to the requesting live IPC connection. The UI receives approval metadata, never a token or grant-file path.

Use a distinct listener/context to prove that a request arrived over private bootstrap IPC. Never trust an HTTP header or body field claiming that provenance.

### Policy and session credentials

Store remembered profiles in owner-private state using the existing persistence conventions. Records contain a public ID, schema version, label, `identity_mode` (`verified_launcher` or `account_scoped_integration`), identity policy/observations, installation/account identity, allowed scope ceiling, session lifetime ceiling, approval revision, and created/updated/revoked timestamps. Store no bearer token in this metadata. The popup and saved-connection view display the selected identity mode.

Represent access with a versioned finite enum and canonical ordered scope set, mapped to existing grant modes and selected-slot lifecycle permissions. Derive UI labels and the scope digest from that schema. Reject unknown labels and invalid combinations; owner scope reduction can select only a subset of the proposed ceiling. Remembered base access never includes exact execution/install authority, recipe mutation, startup policy changes, app removal, or owner grant administration.

Every external session/action credential produces a non-owner principal and remains subject to the current route-specific grant checks. Adding a new owner-only capability to the host must not implicitly add it to a remembered scope or the MCP catalog. In particular, preserve the `/v1/pi/*` principal fence for every grant mode and for renewed sessions.

Refactor issuance to separate grant validation/ledger publication from optional CLI credential-file export. Reuse the existing scope and exact-operation authorization engine. The supervisor mints a fresh random session bearer, retains only its hash and associations in the authoritative ledger, and delivers the raw bearer once over the still-bound bootstrap connection. The bridge holds it only in memory and supplies it through an explicit session-auth provider on subsequent manager requests. Missing management credentials never select the owner's token. Do not create a competing scope evaluator.

Use a prepared handoff record until the bound bridge acknowledges receipt. Activate the session only after acknowledgment, or revoke the prepared record on deadline/disconnect. Losing acknowledgment must not leave usable authority. Approval, publication, delivery, and acknowledgment all retain request/generation binding.

Bind sessions to profile approval revision and supervisor generation. Each authenticated call checks the authoritative profile state/revision as well as token expiry and scope. Atomically changing profile revocation/scope revision blocks dependent session calls immediately; scope increase needs owner approval.

Keep three lifetimes distinct: remembered policy persists until revoked or replaced; a session ends on expiry/disconnect/host restart; exact operation authority retains its existing ledger and operation semantics. After restart the old bearer fails, a new bootstrap re-evaluates the saved policy, and a new bearer is minted only if its checks pass. Revocation blocks further session calls and new authority; cancellation of already-started jobs follows existing explicit operation controls. Reconnection does not replay work.

Initial session lifetime ceiling: one hour. A live connection may renew within the remembered policy after rechecking identity, approval revision, and scope; renewal cannot broaden authority. EOF, termination, or loss of the bootstrap lease revokes session access. Explicit file-grant clients retain their existing lifetime behavior.

The facade may hold base session authority and separate exact-action authority. Select credentials through a typed authorization provider for each operation. An absent, expired, revoked, or inappropriate credential is an error, never a request to use the owner's token.

### Native integration

Add an authorization coordinator owned by `McpmeApp`; do not move authority decisions into presentation views. Extend existing polling with a lightweight pending-request summary and use existing host-generation fencing on every callback.

Give the coordinator its own presentation epoch and states `idle`, `queued`, `presenting`, `submitting`, and `settled`. Bind callbacks and decisions to that epoch as well as host generation and the backend request bindings. Include coordinator invalidation in `resetHostRequestState()`, termination, and explicit host stop; clear its own state without resetting Pi questions or provider input.

Present an app-owned panel or sheet that works when the management window is closed. It shows useful names and access descriptions; IDs/digests belong in expandable review details. Do not show tokens, file references, or raw source data. Provide **Allow**, **Deny**, a **Remember this connection** control, and explicit access choices.

Arbitrate presentation with existing installation/recovery sheets, confirmation dialogs, Pi provider settings, and active credential entry. Queue external authorization while another modal or protected credential interaction owns focus; do not stack sheets, close another window, switch setup tabs, or cancel its work to display a popup. A normal inline Pi question is not itself a modal: preserve its text, question ID, answer state, and job progress if an independent authorization panel is shown. Running Pi in the background must not block connection approval for the entire job. Recheck presentation eligibility when the interaction ends, and revalidate the request before showing it. A queued request remains pending until its existing two-minute deadline; UI contention never pauses that deadline or implies consent.

Pi questions and secret answers retain their existing job/import/revision/question binding and private delivery. Do not reuse that question endpoint, payload, credential store, or job journal for external consent. Authorization summaries use their own bounded client metadata and schema-derived scope labels; they do not copy Pi question text or source/provider content.

Exact operation review should reuse the current installation/preparation review models. For a tool requiring escalation, return `authorization_required` without executing it. The agent submits a typed exact-action proposal through `mcpme_request_access`, including operation family, existing plan ID/digest, and required attempt/idempotency binding. Create a separate pending request tied to the already-approved session; do not convert it into another base-connection request.

The owner UI fetches the authoritative plan summary, revalidates it at approval, and issues the existing exact operation grant to the bound session. The agent explicitly calls the operation after approval. Remembered consent never automatically approves a newly proposed plan. Provider transfer disclosures remain part of the existing preparation review. Exact plan identity and safe digest details must be inspectable before approval without exposing raw source, prompts, credentials, or command lines.

The approval coordinator clears pending UI state on app quit or host replacement. Decisions target request ID, generation, scope digest, and policy revision; a stale panel cannot approve a new request.

## MCP startup, launch, and errors

Do not wait for human approval inside `initialize`. Claude Code has an MCP startup timeout, and changing client timeout settings should not be a prerequisite for this feature. [Claude Code MCP reference](https://code.claude.com/docs/en/mcp)

Handle initialization, ping, and discovery locally with a stable versioned catalog: the existing static management tools plus `mcpme_request_access` and `mcpme_get_authorization_status`. This is an additive catalog change; update exact-catalog tests and client compatibility records. Before approval, `tools/list` returns static schemas only, and every existing tool invocation remains gated. No live host data, slot IDs, paths, recipes, resources, or manager capabilities are returned before authorization.

The request tool accepts a typed scope proposal and returns promptly. For an exported profile, the bridge initiates bootstrap only after receiving `notifications/initialized`; malformed/pre-initialization traffic cannot generate a popup. Status is one of `not_requested`, `pending`, `approved`, `denied`, `expired`, `revoked`, or `unavailable`. Denied/pending management calls return stable structured errors containing only safe status/request identifiers. Status is limited to the calling IPC connection's requests; arbitrary profile/request IDs cannot query another caller's state.

Clients can poll status with bounded backoff and explicitly call the intended operation after approval. Do not automatically replay a rejected/pending tool call when the owner clicks Allow. Correct MCP initialization and lifecycle behavior remain required. [MCP lifecycle specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)

If the native app is absent, the facade may attempt one launch of its verified containing installed app bundle through the platform launch mechanism. Keep a launch-attempt marker per connection/profile/deadline so status polling cannot launch it repeatedly. It must not launch an arbitrary path supplied by the MCP client, create a second supervisor, or treat launching as consent. The existing app lifecycle starts its host and begins request polling. Initialization still completes independently; the facade reports `pending` while launch/startup is in progress and registers with the bootstrap listener when available.

When the app is already running, activation and reopened-window handling must use the same presentation arbitration. An authorization request must not replace an active setup draft, dismiss a Pi question/settings window, or steal focus from credential entry.

If bundle lookup, app launch, host startup, or approval UI is unavailable, settle as sanitized `authorization_unavailable` by the bounded deadline with an instruction to open mcpme. A queued dialog behind another modal remains pending; a headless/noninteractive session or failed activation cannot imply approval. Headless/unattended clients can continue using explicit file grants. Do not retry launch indefinitely or auto-approve when UI is unavailable.

Pending requests expire after two minutes initially. Denial, deadline, child EOF/death, cancellation, app quit, and supervisor replacement settle requests without usable authority. Test approval-versus-disconnect and delivery-loss races; grants minted for a lost handoff must be revoked.

## Implementation sequence

1. **Identity and protocol spike.** Test installed Claude Code variants, safe OS launch attribution, peer credential APIs, app activation, and immediate initialization with the app absent. Establish the verified-client versus account-scoped remembered-policy behavior. This gates the identity promise in the UI, not the rest of the feature.
2. **Authorization refactor.** Split owner CLI, explicit file-grant, and approved-session credentials into explicit code paths. Separate grant issuance from file publication. Preserve existing CLI behavior and add tests proving management cannot fall through to owner authority.
3. **Broker and persistence.** Add the private request-only listener, bounded pending-request state, remembered policy records, session issuance/renewal/revocation, owner decision routes, and generation/connection fencing. Preserve all Pi route fences and close bootstrap admission before joining existing host shutdown cleanup.
4. **Native authorization UI.** Add the coordinator, popup, saved-connection controls, distinct management export action, and exact-plan reviews. Reuse owner transport and native lifecycle protections. Exercise cancel/revoke, host replacement, and contention with Pi questions/settings and installation sheets before wiring the default export.
5. **Management facade and export.** Add profile arguments, static request/status tools, per-call session credential selection, lifecycle cleanup, verified app launch, and credential-free native export. Preserve explicit `--credential-file` as an intentional compatibility mode; invalid explicit credentials fail rather than triggering popup or owner fallback.
6. **Integration verification and documentation.** Run real MCP SDK and installed Claude Code flows, update implemented contracts and status, and make the new export the native default only after its acceptance criteria pass.

Dependency-aware parallel work: after the spike defines the shared schemas, backend authorization/broker work and native presentation work can proceed concurrently. Facade integration depends on broker contracts. End-to-end verification depends on all three. Use scoped agents within the available concurrency limit.

Primary implementation areas: `backend/mcpme/__main__.py`, `management.py`, `grants.py`, `management_workflow_grants.py`, `supervisor.py`, `backend/mcpme_local_transport.py`, and native controller/views under `Sources/McpmeMenuBar/`. Integrate with the current `PiSetupView` only for presentation/lifecycle coordination; preserve `pi_setup.py` and its internal broker's owner-only contracts. Keep new broker/policy components in focused modules rather than expanding unrelated model code.

## Acceptance criteria

- Exported normal configuration includes only launcher/state selection/public profile metadata; no credential path, bearer, secret environment value, or manual grant-file setup.
- Per-app **Copy Connection** output is unchanged; external setup instructions/specification exports retain their draft bindings. **Copy Management Connection** creates a separate profile and never starts Pi or authorizes a draft by being copied.
- A real stdio client initializes and discovers the stable tool catalog within a two-second local test budget while approval is pending or the app is absent. Human response time is outside this startup budget.
- Fresh unapproved clients cannot call manager data/mutation routes or access owner authority. Approval creates precisely the selected base scope.
- A remembered profile reconnects across client and host restarts under its approved identity policy, receiving a new session token. Changed identity, another state installation, scope increase, revocation, and ambiguous verified-client attribution follow the documented reapproval behavior.
- Duplicate requests/decisions, valid request-ID replay on another live connection, malformed frames, socket impersonation, another UID, unsafe permissions, and TCP attempts cannot obtain authority.
- Test copied profile IDs against a different installation, explicit account-scoped fallback consent, simultaneous different-scope requests for one profile, and stale approval after a policy revision. Neither MCP names nor a shared interpreter establish verified client approval.
- Revocation, expiry, scope reduction, disconnect, app quit, and host replacement block the next unauthorized call. Concurrent sessions cannot receive each other's credentials.
- Preparation/resolution execution and installation retain exact plan/digest/idempotency bindings. Modified plans and replay attempts cannot reuse approval. Startup policy changes, removal, and grant issuance/revocation remain owner-only.
- Every Pi route rejects external base/action credentials, including after renewal. Cover all routes and grant modes, malformed owner/grant combinations, and credential absence; no denial falls back to owner credentials. External management cannot access Pi provider settings, job questions/answers, or its execution broker.
- Popup cancellation, denial, timeout, lost delivery, and approval/disconnect races leave no usable orphan session. Existing operations are not silently replayed or canceled solely by client reconnection.
- App launch occurs at most once per bounded attempt despite repeated polling, selects only the containing installed bundle, and cannot bypass consent. Headless sessions, failed activation, and modal contention have bounded, nonapproving outcomes.
- Pi inline questions, active secret entry, provider settings, installation/recovery sheets, and a second queued connection request coexist with authorization without lost input, two simultaneous sheets, tab changes, or canceled jobs. Host replacement while a popup is open rejects its stale callback and clears authorization state. UI contention expires normally and cannot pause the request deadline; background Pi alone does not block authorization.
- Logs, stderr, process arguments, environment, exported configuration, errors, and authorization UI contain no credential values, raw client/model prompts, evidence, model responses, or exception dumps. Authorization requests display only bounded client metadata and schema-derived labels/review metadata. Existing Pi inline questions retain their separate transient question contract. Internal private policy metadata follows the declared schema.
- Existing explicit file-grant tests, management scope tests, private socket tests, and installed app connection tests continue to pass. Add targeted broker/native/session tests and end-to-end coverage; a documentation-only plan does not establish these results.

## Compatibility and rollout

Ordinary installed-app invocation connections retain their existing behavior. This proposal concerns management setup authority.

Existing credential-file registrations continue to work. Offer a native replacement export rather than editing unrelated client configuration. Revoking an old file grant is a separate explicit owner action when retiring it.

Introduce versioned policy storage with atomic publication and conservative handling of corrupt/unknown records. Disable automatic approval for unsupported policy versions. Rollback disables popup bootstrap and saved-policy activation without altering installed apps or granting broader authority. Explain any stale new-client configuration and offer the established explicit-grant connection path.

After implementation, update `MANAGEMENT_MCP.md`, `MCPME_DESIGN.md`, `MCPME_STATUS.md`, native connection instructions, and verification records to distinguish proposed behavior from measured acceptance evidence.
