# Live generated-adapter acceptance

This is preparation for A5.12. No live provider result is established by adding
this driver or by its synthetic tests. The selected account, credential location
and model remain operator decisions. Only the registered OpenAI Responses
adapter is supported, at `https://api.openai.com/v1/responses`.

`scripts/verify_phase3_live_agent.py` defaults to producing a private review
artifact. `--model` is required; no model is inferred. The output proposes an
exact agent policy using the current adapter implementation digest and the
published unlocked Python component. It preserves the existing preparation
families and adds only the unlocked family and singleton closure. It neither
writes the tracked release policy nor changes a running host. Review artifacts
are not execution authority.

Actual execution additionally requires `--run-live`, `--host`, `--native`,
`--credential-file`, and a fresh `--output`. The supplied credential file must be
an explicit absolute canonical path to a current-user, singly linked regular
file with mode 0600 and 1–4096 visible ASCII bytes, without a newline. The driver
does not discover credentials, read an environment key, reuse another account's
state, select a base URL, or accept a provider factory. It never records a key
or a key-derived digest. Python strings cannot promise memory zeroization.

Before reading that file, the driver verifies the matching signed app/host,
closed member inventory, current policy and adapter/model/component tuple,
relocates the whole signed app through the shared preservation harness, checks
the native live-driver preflight and starts the ordinary CLI in fresh isolated
state. The production provider list must show an active live gate and no
profiles or jobs. A closed candidate fails before credential ingestion. The
owner provider route receives the selected key in memory and stores it using
the production private `SecretStore`; adding the profile does not dispatch.

The candidate must be separately reviewed and signed with exactly one live
model and the actual adapter digest, the unlocked component as an agent and
preparation head, its singleton preparation closure, and
`unlocked_resolution: true`. Current public policy remains unchanged. A
candidate run is not permission to publish that policy. This slice changes no
backend, immutable component, recipe, helper or runtime asset.

## Native and provider boundaries

`--agent-live-native-acceptance` uses the existing one-shot, expiring private
fixture, current manager identity and owner-route guards with a distinct
`native-agent-live-` marker. Its scope additionally pins the configured provider
generation. Selection requires `simulated: false`, current execution availability,
the exact provider/model, and an enabled exact recipe. The legacy simulation
flag still requires `simulated: true`; live receipts are never labeled simulated.
The fixture does not contain credentials. The preflight flag performs no
requests; the self-test uses explicitly synthetic owner replies and never sends.

The actual production review and send handlers require these exact reviewed
limits: four calls, up to three concurrent calls, 524,288 cumulative serialized
input bytes, 4,096 maximum output tokens per call, 131,072 artifact bytes and a
180-second analysis deadline. The production coordinator enforces the call,
transfer and overall analysis deadline; the backend enforces a 524,288-byte
response bound per call and sends the reviewed output-token ceiling. These are
not dollar-spend limits. Local cancellation closes and joins owned I/O; remote
provider work settlement is not asserted. The driver makes one attempt and has
no automatic retry.

## Required run and retained scope

The sole live fixture is an untyped scalar callable, an original license and an
unknown binary asset. Only the selected worker contents and explicitly reviewed
license identity go to the provider. The task asks for a `double` tool with an
integer `n` argument and structured `value` result. Real provider proposals must
pass unchanged validators; missing facts, refusal, truncation, unresolved
requirements or inconsistent generated artifacts fail the run. The driver does
not repair responses or substitute a simulation.

Success requires the real production native review/send/pending-proposal handoff,
ordinary dependency resolution and preparation, a matching attachment, installed
HTTP and stdio SDK calls, deletion of the original source, restart with both
transports, and a confined probe of a relocated package after captured-source
removal. Four completed call records must be present. Full input and relocated
app inventories and signatures are rechecked. All original owned process handles
and job ledgers must settle; otherwise the common harness retains its workspace.
Raw subprocess output remains in memory. A reserved exclusive receipt records
fixed diagnostics and metadata, including whether dispatch was attempted; an
incomplete run does not claim zero provider calls.

These checks do not establish semantic model quality, arbitrary application
compatibility, physical dialog clicks, remote-provider settlement, or public
release approval. A live run requires an exclusive installed-gateway acceptance
window. No live, model or installed-gateway workload was authorized or performed
while preparing this checkpoint.

The source tests cover inactive/wrong-model/wrong-digest candidate rejection,
explicit private credential boundaries using synthetic values, no implicit
backend construction, compiled generation/limit/simulation checks, and actual
production parent callbacks over the owner Unix socket against a closed host.
The latter must stop before the agent start route with zero jobs. A future
matching signed candidate and explicitly selected account are still required.

The checkpoint's focused run passed 120 tests. After the final script-only
preservation changes, its 22 nondispatch script controls passed again. The full
Swift suite passed 18 tests with three existing installer opt-in skips. The
initial focused run's three failures were test-fixture assumptions: intercepting
all environment lookups also intercepted Pydantic's plugin setting, the registry
create result needed its `provider` envelope, and malformed repeated-review
replies needed a fresh selection to invalidate the preceding valid test plan.
The failed receipt is retained; no provider attempt was used to resolve them.

Independent review also caught a default-entry import failure hidden by pytest's
backend import path. The follow-up adds the fixed repository backend path only
inside the nondispatch review branch and restores the previous search path
afterward. A real offline `uv run --frozen python` subprocess without PYTHONPATH
now verifies this entry point. The live branch retains its bundled-module origin
checks and receives no source bootstrap.
