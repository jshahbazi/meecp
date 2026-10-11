# Consultant delivery and commercial readiness

Status: **Planned**, 2026-10-11. This document defines the first benchmark and conditional delivery priorities. The benchmark, reporting application, measurement harness, and paid pilots have not been implemented or run. Existing host capabilities are identified separately below.

Source checkpoint reviewed: `04abb2d` on `codex/stark-dependency-lock`. Documentation changes do not establish fresh runtime acceptance for that checkpoint. Historical acceptance retains its recorded source, artifact, platform, and provider limits.

## Outcome and decision

Test whether meecp reduces the work required to prepare, install, connect, upgrade, and support a custom local business tool. The initial buyer hypothesis is an independent automation consultant or small consultancy already delivering Python tools to Apple silicon Mac users, especially customers using assistants from more than one vendor.

The proposed paid value is lower engineering and support effort per client deployment. A successful technical comparison permits a small paid pilot; it does not establish customer demand, broad installation reliability, or profitability.

Start with one deterministic invoice-reconciliation application, two physical Macs, Claude Desktop and Cursor, a successful version upgrade, and a controlled input failure. All data is public synthetic test data. No real client records, model-scoring experiments, or GPU packages are required.

This plan does not authorize benchmark execution, paid model calls, customer outreach, production installation, release publication, or new hosting infrastructure. Those are implementation/run steps with their own required resources and existing session authority. It does not change component contracts or promote deferred roadmap items into committed implementation.

## Existing behavior and remaining work

| Capability | What works now | Remaining work or verification |
| --- | --- | --- |
| Pi preparation | A coding loop and host-owned verification are implemented. Historical acceptance for selected Python, Node dependency, and native-build tuples is recorded in [Pi setup](PI_SETUP.md#live-acceptance). Source: [verification path](../backend/meecp/pi_setup.py). | Build and freshly verify the exact current reporting artifact before measurement. Agent-authored fixtures and a few historical examples do not establish general preparation reliability. |
| Private runtime and dependencies | Supported Python packages carry private CPython, locked dependencies, provenance, licenses, and a relocatable execution closure; see [package authoring](PACKAGE_AUTHORING.md) and [Phase 3 evidence](PHASE3_STATUS.md#shared-delivery-subgoals). | Verify the exact benchmark artifacts on both Macs without the source checkout or global development tools. Do not count this alone as a unique advantage over Claude. |
| Tool verification | Package/protocol checks and synthetic fixtures exist, including exact structured results and result assertions; see [Pi verification](PI_SETUP.md#verification-and-installation). | Write a route-independent business oracle external to both the report implementation and Pi's package fixtures. Passing those fixtures does not establish the benchmark's business semantics. |
| Multiple assistants | Per-app identity, stable endpoint, invocation credentials, and connection export exist. [CLI export](../backend/meecp/__main__.py) emits configuration. | Prove the same installed package works through both actual client UIs. Export does not install client configuration. A guided connection check is a candidate improvement. |
| Process ownership and lifecycle | Supported process/session topologies have bounded admission, cancellation, reapers, readiness checks, and explicit controls; see [host design](MEECP_DESIGN.md#host-launch-contract). | Exercise this package and current artifact. Stdio packages normally use one child per session; one installation does not mean one shared process. |
| Reviewed upgrades | Package/settings generations preserve app identity and connection details. Candidate verification and pre-exposure restoration are implemented; see [upgrade rules](UPGRADES_RECOVERY.md), [state machine](../backend/meecp/upgrades.py), and [Phase 2 acceptance](PHASE2_COMPLETION.md). | Verify V1-to-V2 on both Macs and clients. After durable exposure, preserve candidate data; automatic restoration of old data or binaries is forbidden. Post-exposure repair/restore remains separate planned work. |
| Configuration and secrets | Typed configuration and immutable secret references are delivered privately. The [current store](../backend/meecp/settings.py) uses owner-only files; see [configuration contract](CONFIGURATION_SECRETS.md). | Keychain storage is planned. The first benchmark needs no business-system credential; provider credentials remain private. |
| Diagnostics | Bounded lifecycle metadata, fixed error codes, activity counters, and native status views exist; see [monitoring](MEECP_DESIGN.md#monitoring-and-local-trust). | No consultant-facing diagnostic export command or route was found in current source. Build an allowlisted support export if the benchmark shows it reduces troubleshooting. |
| Distribution | Prepared-package import, pinned HTTPS downloads, and signed/notarized host distribution machinery exist; see [onboarding](PHASE2_ONBOARDING.md), [downloads](PINNED_DOWNLOADS.md), and [release workflow](GITHUB_RELEASES.md). | Manual URL/size/hash handoff remains. The benchmark must identify its actual selected release and receipts; historical signed artifacts do not establish the current release's acceptance. |
| Updates and availability | Manual host replacement and reviewed package upgrades exist. Manual/host-start startup policies exist. | [Automatic host updates](AUTO_UPDATE_PLAN.md) are Planned; automatic package updates remain Deferred. Start on first call and automatic crash restart are Planned. Measure the existing manual workflow first. |
| Consultant deployments | A package can be installed independently on several compatible Macs. | No central inventory, customer rollout, remote health management, or entitlement service exists. A two-Mac handoff is not fleet management. |
| Trust boundary | Digests, declared access, review, private state, and scoped local authority exist. | Installed code executes with the user's macOS permissions. Declarations are not OS isolation. Limit this pilot to inspected, trusted benchmark packages. |

The [Phase 3 status record](PHASE3_STATUS.md#evidence-transfer-limits) preserves qualified signed-artifact evidence. Later [Pi acceptance](PI_SETUP.md#live-acceptance) covers additional selected integrations. Neither record transfers automatically to this benchmark or all current source. The [open-plan inventory](OPEN_PLANS.md) remains authoritative for unfinished features.

## Benchmark B1: consultant reporting delivery

### Application and independent oracle

Both routes start from the same known-good, non-MCP Python application, source revision, dependency lock, V1 task brief, and V2 change request. It reads `invoices.csv` and `payments.csv` from a configured directory. Its only exposed business operation is a bounded, read-only `reconcile_invoices(cutoff, include_aging=false)` tool returning structured results. The application also offers a deterministic CLI for independent setup checks. No arbitrary SQL, shell execution, background schedule, or SaaS connector is needed.

Use integer cents, a single declared currency, ISO dates, an inclusive payment cutoff, deterministic invoice ordering, and explicit row/size limits. Include invoices created on or before the cutoff. Match only payments at or before the cutoff to included invoices. Overpayments remain visible rather than being silently discarded. An unpaid balance is overdue when its due date is strictly before the cutoff.

Freeze this initial fixture at cutoff `2026-09-30`:

| Invoice | Amount, cents | Created | Due | Payment |
| --- | ---: | --- | --- | --- |
| I1 | 10,000 | 2026-09-01 | 2026-09-10 | 10,000 on 2026-09-05 |
| I2 | 20,000 | 2026-09-01 | 2026-10-15 | 5,000 on 2026-09-15 |
| I3 | 15,000 | 2026-09-01 | 2026-09-20 | 17,000 on 2026-09-21 |
| I4 | 5,000 | 2026-09-01 | 2026-09-01 | None |
| I5 | 7,000 | 2026-09-01 | 2026-09-27 | 7,000 on 2026-10-02; excluded at cutoff |
| I6 | 3,000 | 2026-10-01 | 2026-10-15 | None; invoice excluded at cutoff |

Add one unmatched payment of 4,000 cents to `I999` on 2026-09-22.

| Independent V1 assertion | Expected |
| --- | ---: |
| Included invoices | 5 |
| Invoiced amount | 57,000 cents |
| Matched payments at cutoff | 32,000 cents |
| Unmatched payments | 4,000 cents |
| Signed invoice balance | 25,000 cents |
| Sum of positive outstanding balances | 27,000 cents |
| Overpayment | 2,000 cents |
| Overdue positive outstanding | 12,000 cents |

Also assert exact exception identities: `I3` is overpaid; `I4` and `I5` are overdue; `I999` is unmatched. The signed balance and positive outstanding are intentionally different.

Before packaging, a separate reviewer derives the oracle without importing the report implementation or accepting an agent's claimed expected values. Freeze at least twelve named scenarios covering the base example, empty inputs, cutoff boundaries, quoted CSV fields, Unicode identifiers, row-order independence, duplicate invoice/payment IDs, unmatched references, malformed or missing columns, invalid dates/amounts, out-of-root paths, and size-limit rejection. Publish exact contracts and fixed error codes before either route starts. Inputs must never produce a partial successful report after validation failure.

V1 accepts `include_aging=false`; requesting aging returns a documented unsupported-feature code. V2 implements aging while retaining V1 behavior and the input contract. For positive outstanding balances, the base fixture's V2 buckets are current 15,000 cents; 1-7 days overdue 7,000; 8-30 days overdue 5,000; older 0. Both clients must rediscover the upgraded schema/output and pass their V2 checks. Expected values come from the independent oracle, not from the package's verification file.

### Comparison routes

| Route | Permitted workflow |
| --- | --- |
| M: meecp | Prepare the source with Pi or the documented external-agent path, independently verify, review/install, export/connect to both assistants, distribute to the recipient Mac, apply V2 through reviewed upgrade, and resolve the controlled failure. Choose and freeze one authoring path per round. |
| C: Claude/MCPB baseline | Use a coding assistant and the official MCP SDK/toolchain to adapt the same application. Package it for Claude Desktop using its strongest supported MCPB runtime. Connect the same implementation to Cursor using Cursor's supported MCP mechanism. Use the documented deployment/update method available to the selected account tier. |

Do not force Cursor to ingest MCPB, remove Claude's managed Python runtime, or withhold available configuration, secrets, distribution, or update features. Fix the intended customer account tier before scoring; do not assume enterprise tooling unavailable if the pilot buyer already owns it. Measure a separate tier only as a separately named round.

Current primary-source baseline references, reviewed 2026-10-11:

- [MCPB CLI](https://github.com/modelcontextprotocol/mcpb/blob/main/CLI.md): manifest/archive tooling and signing; the developer still supplies the useful server and workflow tests.
- [Claude-managed Python example](https://github.com/modelcontextprotocol/mcpb/blob/main/examples/hello-world-uv/README.md): Python/runtime and dependency setup are already supported.
- [Private organization distribution and updates](https://support.claude.com/en/articles/12592343-enabling-and-using-the-desktop-extension-allowlist): custom extensions and uploaded new versions are supported on Team/Enterprise.
- [Local and remote connector availability](https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors): local extensions work within supported Claude clients; MCPB is an open format.
- [Cursor MCP configuration](https://prod.cursor.com/docs/mcp): use the selected client's documented local transport/configuration path.

Recheck these owners and actual installed versions when freezing a run. Do not infer a meecp advantage from an older Claude announcement or package-format difference alone.

### Environment, operators, and pairing

Use two physical Apple silicon Macs, A and B, running compatible macOS 26+ versions. Record hardware class, memory, OS/build, app/runtime versions, account tier, source/release commits, dependency-lock/artifact hashes, and operator pseudonyms. Select two operators who did not implement meecp; train both equally on an unrelated practice tool. Practice and fixture validation are explicitly unscored.

Each route starts from a declared reset state with separate disposable host/client configuration and public synthetic working directories. Restore state between routes, including package caches and route-specific toolchains according to the frozen cold/warm policy. Claude Desktop and Cursor may be common preinstalled prerequisites. Include route-specific installation/bootstrap, authoring, configuration, distribution, upgrading, and repair time; report each stage separately so first-use cost and recurring cost remain distinguishable. Never modify a user's ordinary host, client settings, source checkout, or installed packages to create the clean state.

For each operator, run once authoring on A and handing off to B, and once authoring on B and handing off to A. The recipient environment must have no report source/build workspace or user-installed Python, Node, `uv`, or Homebrew dependency available to the tool. A fresh account alone does not prove the absence of machine-wide dependencies: use a verified clean installation or an equally strong documented denial/inventory check. Virtualized/isolated rehearsals do not count as a second physical Mac.

This yields four paired trials and eight route executions. Balance order: operator 1 uses M then C from A and C then M from B; operator 2 receives the opposite orders. Each operator performs the entire lifecycle for both routes. Record learning/carryover as a pilot limitation; do not present the pairs as independent population samples.

Use identical model/provider choices where supported. If the authoring agents cannot use the same model, report the exact difference and compare the complete workflows; make no causal claim about the preparation framework or model quality. Give both routes the same source, briefs, fixtures, permitted documentation, assistance policy, and caps. No saved route-specific solution may be copied into a scored fresh run. A practitioner should review the baseline workflow before freeze.

### Per-route procedure and measurements

1. Confirm reset/environment identities and begin the clock before route-specific setup. Prepare V1 from the frozen non-MCP application.
2. Install and connect on the authoring Mac. Run the full independent oracle suite through the installed route's configured protocol transport/runtime. Separately run the frozen actual-client UI smoke set in Claude Desktop and Cursor: V1 base report, V2 aging after upgrade, malformed-input rejection, and successful report after restoring the input.
3. Hand the prepared tool to the other clean Mac with a client-facing setup brief. Verify both clients there. Package architecture may differ between routes; record installation/instance counts rather than imposing meecp slot concepts on Claude.
4. Exercise simultaneous bounded reads from both clients, stop/restart the tool through each route's supported workflow, and reconnect. Confirm source inputs remain unchanged and tool work leaves no owned process/listener leak.
5. Deliver V2 to both Macs. Preserve selected configuration and credentials where used, discover the updated output, and rerun the full V1/V2 oracle suite through the installed transport/runtime plus the defined smoke checks in both clients. Require no manual rewriting of the business implementation on the recipient Mac.
6. Substitute a payments file missing the required amount column. Observe a fixed `INVALID_INPUT` failure and no partial success or exception dump. Diagnose it using only the route's documented support workflow, restore the valid file, and recover without reinstalling.
7. Stop disposable services and verify cleanup. Retain only permitted measurements and assertion summaries.

Time review/apply as a separate stage. Preparation and fixture verification grant no installation authority: apply must remain bound to the exact reviewed package/settings plan under existing owner authority or an exact scoped grant. A model cannot substitute another package or expand the approved effects. Existing authorization can cover the action; the benchmark must not introduce repeated confirmations for unchanged approved scope.

Keep full protocol coverage and actual-client usability results separate. The harness must exercise the exact installed implementation and selected runtime, not a development checkout or substitute server. If a route offers no supported external probe path, choose an in-client test method at G0 and freeze its timing/coverage; do not invent access to private client APIs. Both clients on both Macs receive the four defined UI smoke checks; the full oracle does not require every edge case to be selected repeatedly by an assistant. Repeated assistant tool calls must not be assumed deterministic. Do not score prose summaries or LLM arithmetic as report correctness.

Record active human minutes, wall-clock minutes, intervention count/type, first-attempt success, completion, aggregate assertion counts, handoff/upgrade/error-recovery outcomes, cleanup outcomes, and aggregate model spend if available from billing. Include every failed attempt and repair in the totals. Define an intervention as an operator action needed to correct or complete a step; routine required configuration is timed and recorded separately. A maintainer intervention is separately identified and fails the self-service completion gate.

Before scoring, freeze maximums of 90 active human minutes and 150 elapsed minutes per route lifecycle. These are initial feasibility caps, not measured performance claims. Pause only under the documented external interruption rule; provider/tool waiting stays in elapsed time. Missing capabilities, timeouts, or product source changes stop the route as a failure. Record its actual active/elapsed time, stage, and fixed reason; only a reached cap receives a cap-hit/censored flag. Failed or censored routes stay in completion denominators and cannot supply successful G2 durations. Do not replace a trial with a successful retry.

### Separate host-contract checks

The input fault above measures report-error handling. It does not establish process-crash recovery or transactional upgrade safety. Use a separate trusted, disposable lifecycle fixture for meecp-specific checks, outside the comparative timing:

- Failed candidate verification before exposure restores the verified old generation and unchanged data/configuration; a stale session cannot invoke a newly selected generation.
- A synthetic write made after candidate exposure survives an interrupted operation. Recovery preserves the selected candidate; it must not silently restore old data or an old binary. Any later restore requires its separate reviewed compatibility/write-loss plan.
- Stop remains Stop after recovery. Reaper/supervisor failure produces the documented ownership outcome, blocks unsafe replacement, and does not replay an in-flight call.

Use existing acceptance helpers where applicable. Inspect the exact current artifact and [upgrade boundaries](UPGRADES_RECOVERY.md) before choosing fault injection. Never kill an unrelated process or simulate faults in the user's installation. Do not claim automatic crash restart: it is not implemented. Do not force the baseline to implement meecp's transaction model; report its documented recovery behavior separately.

### Evidence and privacy

Proposed artifacts to build:

| Artifact | Responsibility |
| --- | --- |
| `examples/consultant-reporting/` | CC0 synthetic source/fixtures, pinned dependency contract, V1/V2 briefs, client setup instructions, and licenses. |
| `tests/consultant_reporting/` | Independent oracle and protocol assertions; no import of report calculations to derive expected values. |
| `scripts/benchmark_consultant_delivery.py` | Stage timing, fixed metadata schema, assertions, bounded cleanup, run/cap accounting, and refusal to claim missing hardware/client acceptance. |
| `docs/benchmarks/consultant-delivery/<round>/` | Frozen protocol, source/version identities, metadata-only paired results, review, limitations, and decision. No result directory is evidence until a run completes. |

Freeze the protocol, task/oracle hashes, order, versions, assistance rules, caps, and thresholds in a committed round record before scored runs. Public synthetic fixture source and task briefs may be deliberately authored as benchmark inputs; do not capture runtime prompts, responses, source rows, report bodies, transcripts, raw stdout/stderr, credential values, tokens, environment values, raw configuration paths, or exception dumps in measurement logs or results. Compare expected/actual results in memory and persist assertion IDs, counts, fixed failure codes, and artifact hashes only. Diagnostic export must use an explicit field allowlist and omit unknown fields.

Failed and interrupted attempts remain in the round ledger. A product fix or changed protocol starts a new named round; retain the previous outcome. Drift or machine interruption prevents the affected acceptance claim. Do not re-enable disabled GitHub workflows, add a remote, publish releases, or send messages to prospective customers as an incidental benchmark step.

### Decision gates

| Gate | Required evidence | Failure disposition |
| --- | --- | --- |
| G0: protocol ready | Independent oracle review; frozen contract, two-Mac environment, two operators, supported clients/tier, baseline workflow, exact artifacts, reset policy, caps, and metadata privacy checks. | Development rehearsal only. Missing resources remain explicit; no comparative pass. |
| G1: works for this case | Every frozen V1/V2 business assertion passes through the installed transport/runtime on both Macs; both actual clients pass the defined UI smoke set on each Mac; all four meecp lifecycles complete within caps without maintainer rescue; report-level input-error recovery, unchanged inputs, cleanup, and separate host-level fault/recovery checks pass. | Repair the smallest demonstrated correctness/delivery blocker. Begin a fresh recorded round after fixes. |
| G2: useful effort reduction | Four completed paired comparisons; median paired active-time savings are at least 30% **and** 15 minutes, meecp is faster in at least three pairs, and median paired elapsed-time increase is no more than 10%, using the formulas below. | No commercial-efficiency claim. Use stage data to select a focused change or revise the buyer hypothesis. |
| G3: paid use | At least three of five qualified consultants pay a fixed pilot price, complete a first supported deployment, and use it for another paid engagement or recurring deliverable during a four-week observation window. Support minutes and maintenance burden are recorded without client content. | Do not expand into fleet/hosted infrastructure. Revisit the offer using non-content feedback and actual usage outcomes. |

For each completed pair `i`, define `d_i = C_active_i - M_active_i` in minutes, `r_i = d_i / C_active_i`, and `e_i = (M_elapsed_i - C_elapsed_i) / C_elapsed_i`. Both baseline denominators must be positive. G2 requires `median(d_i) >= 15`, `median(r_i) >= 0.30`, at least three `d_i > 0`, and `median(e_i) <= 0.10`. Report paired values; do not substitute a difference or ratio of route medians.

G2 thresholds and the G3 target are proposed product decision rules to freeze before measurement, not statistically validated cutoffs or forecasts. Report every pair, stage, delta, and median. Four correlated pairs support a descriptive pilot only. A failed baseline is reported as failed; it does not supply a successful comparison duration. Fewer than four complete pairs cannot pass G2. No selective replacement, significance claim, p95 reliability claim, or population success estimate is permitted.

G3 is a separate commercial phase. Select and freeze one pilot price, scope, support allowance, recruitment qualification, and observation start before offering it. The cohort is the first five qualified offers under those rules; do not replace declines or unsuccessful participants. Payment must purchase meecp product access or a meecp license; bespoke integration/service fees alone do not satisfy the gate. Count declines, nonpayment, abandonment, and support escalation in the denominator. Three purchases do not establish profitability or renewal; renewal and acquisition cost need later evidence.

For the pilot, record realized product receipts, refunds, payment fees, aggregate model/service costs, and support/preparation minutes at a declared internal hourly cost. Report contribution after those costs and the split between one-time setup and recurring maintenance. Measure whether maintaining supported packages scales with each customer or improves through reusable tooling. A negative contribution requires a narrower supported offer, better tooling, or a revised price before expansion; do not treat consulting revenue as software margin.

## Delivery priorities and dependencies

| Priority / work item | Deliverable and completion condition | Depends on |
| --- | --- | --- |
| P0.1: source and oracle | Public reporting application, V1/V2 contracts, exact fixtures, independently reviewed assertions. No meecp host behavior change. | This plan; choose supported dependency/runtime pins. |
| P0.2: measurement and environment | Metadata-only harness, reset/run ledger, baseline recipe, clean-machine checks, and actual client checklists. Verify instrumentation using a disjoint rehearsal. | Can proceed alongside P0.1; freeze with its final identities at G0. |
| P0.3: benchmark | Complete B1 and publish the scoped result/decision. Existing manual workflows are admissible; do not first build a fleet platform or automatic updater. | G0, operators, physical Macs, selected releases/accounts, and execution resources. |
| P1.1: preparation and correctness fixes | Fix measured failures in supported adaptation, dependency closure, configuration, tool contracts, or clean-machine operation. Rerun affected checks and a fresh paired round. | G1 failure or measured preparation bottleneck. |
| P1.2: connection and support workflow | Guided checks for supported client configurations and an explicit owner-authorized support export with a fixed allowlisted schema: version, operation/generation, readiness, and fixed-error metadata. Exclude raw paths and arbitrary package metadata as well as the private content listed above. Existing authority/privacy rules remain. | Measured connection/diagnostic bottleneck; scoped contract/UI design before implementation. |
| P1.3: consultant delivery bundle | Reviewed package, digest/version identity, setup brief, client-specific configuration instructions, acceptance checklist, and a manual update/recovery procedure. Remove avoidable URL/hash transcription while preserving pinned verification. | Working package and measured handoff/upgrade bottleneck. No public marketplace or silent client edits. |
| P1.4: Keychain parity | Implement/migrate Keychain-backed secret references, preserving immutability, revocation, owner authority, and recoverability; test interrupted migration. | Before any pilot needing business credentials; coordinated with [configuration ownership](CONFIGURATION_SECRETS.md) and [open scope](OPEN_PLANS.md). |
| P1.5: signed host updater | Complete the selected [host updater plan](AUTO_UPDATE_PLAN.md), release/build identity, trust feed, shutdown/relaunch, and state-preservation acceptance. | Before a low-touch paid pilot needs ongoing host updates; destination/key/release prerequisites in that owning plan. Manual updates are allowed in B1. |
| P2: paid pilot | Five qualified consultants receive the fixed supported offer; evaluate G3 and actual support cost. Record first installation and recurring use separately. | G1/G2; applicable P1 blockers resolved; accepted selected release and bounded support offer. |
| P3: scale decision | Decide whether measured recurring demand requires package update discovery, client inventory, signed rollout channels, entitlements, or remote health management. Write scoped owning plans only for demonstrated needs. | G3 plus maintenance and unit-economics evidence. |

P1 items are candidate priorities, not a requirement to implement every feature before learning from B1. Promote only measured blockers or necessary pilot parity. Known security requirements remain mandatory even if a timing comparison cannot measure them. Keep changes in the owning plans and update [OPEN_PLANS.md](OPEN_PLANS.md) when their scope/status changes.

Preparation, source/oracle work, and harness/client setup investigation can proceed independently after the contract is agreed. Use up to three parallel workstreams plus an integrating reviewer within the actual four-agent concurrency limit. Keep oracle derivation independent of report implementation. Integrate in dependency order; do not schedule scored runs before freeze or spend operator time against a changing build. No calendar delivery estimate is committed until hardware, operators, and release resources are available.

## Explicitly deferred expansion

Automatic package update discovery/scheduling, a public marketplace, publisher onboarding, fleet administration, remote workers, remote network exposure, additional MCP capabilities, document retrieval/OCR, voice, GUI panels, and GPU-engine optimization are outside B1. Their existing Planned/Partial/Deferred states remain unchanged.

Cross-client deployment must preserve loopback-only serving, route authentication, Host/Origin checks, bounded execution, and package process ownership. No aggregate MCP endpoint or public listener is needed for this pilot. Do not infer enforced filesystem/network isolation from access declarations or packaging checks.

If later pilots demonstrate a need for central inventory or rollout coordination, a small hosted management layer can be evaluated independently of hosted tool execution. Customer tools can remain local. Hosting is a decision after measured demand and a separate authority/privacy design, not a prerequisite for the benchmark.

## Immediate next implementation task

Implement P0.1 and P0.2, review and commit the frozen G0 protocol, then obtain the missing run resources and execute P0.3. The first result must state separately: correctness for this fixture, delivery on these Macs/clients, comparative effort, and unresolved commercial demand. Until those records exist, this plan remains **Planned**.
