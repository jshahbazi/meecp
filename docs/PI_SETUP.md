# App setup with Pi

Status: implemented in the current source, 2026-10-14. This document describes
the native Add App flow and its host contracts. General installation reliability
across unfamiliar projects has not been established.

## Add an app

1. Open **Add App** and describe what your assistant should be able to do,
   including useful inputs and outputs.
2. Use the attachment menu for a source folder, Markdown/text instructions,
   public HTTPS project link, runtime/model resources, or a prepared package.
   For a single Python or JavaScript source file, choose its containing folder.
   Choose **Use these instructions** for a setup document. For a GGUF or other
   model file, describe the app first and choose **Model or runtime files…**;
   the source picker does not accept model files.
3. Select **Create app**. If no provider is configured, the conversation shows
   **Agent settings** inline. Select the provider and exact model, and save its
   API key or compatible local endpoint. Saving continues the requested setup
   only while the same draft and source remain selected. Agent settings are
   also available later through the conversation's details.
4. The MeeCP agent inspects the staged source, writes the adapter and package,
   acquires dependencies and runtimes, runs checks, and repairs failures. Its
   completed messages and current host progress appear in the conversation.
5. Review the verified app's tools and declared access. Choose its name,
   configuration, and startup behavior here, then select **Add App** or
   **Add & Start App** to install it.

The description, replies and setup action share one composer that stays visible
while the conversation and review scroll. Once saved, the app request remains
distinct from replies; **Edit request** explicitly changes the draft. Saved
setups appear when there are drafts to resume. Active runs show progress, questions, and cancellation;
failed or canceled runs offer **Try Again** with a readable failure explanation.
Interrupted runs offer **Resume Setup**. The host resolves the current attempt
by draft and source identity, including after a window or host restart.

Pi can investigate unfamiliar source or implement an idea from scratch without
a registered recipe or app template. It chooses the integration architecture,
build steps, dependencies, and tool schemas. The package
manifest, lockfile, launcher, provenance, licenses, and verification fixtures are
generated setup outputs. Recipe selection and authoring are absent from the
normal Pi flow.

When setup needs a decision or credential, Add App shows the question inline.
Suggested choices populate the composer; answer and select **Continue** to
resume the same coding session. Credential
answers use a secure field and remain in the host's secret store; Pi receives
an opaque binding identity rather than the credential value. Ordinary
implementation choices remain the agent's responsibility. While the agent is
working and has no pending question, the composer can send a follow-up message
through the pinned SDK's steering API. Acceptance means that the active worker
queued the message. It does not guarantee that a model follows the instruction.

The MeeCP agent is the only native preparation workflow. The former **Manual
setup** and **Have your agent do it** choices and their native integration are
retired. Their implementation types remain compiled for isolated compatibility
fixtures, without app instantiation, wiring, polling or restoration.
The conversation retains source/link attachments, model/runtime resources,
prepared-package import, configuration, review, installation and recovery.
Importing an existing prepared package works without first creating a draft or
configuring a setup provider and can proceed directly to review. The Pi verifier
currently accepts `mcp-stdio` packages; reference-document service onboarding
remains planned.

Legacy incomplete drafts with a source kind unsupported by Pi show instructions
to create a new agent draft using source/instructions or import a prepared
package. They cannot restart a retired workflow and are not resumed
automatically. Their saved files and intent remain retained. Compatible Pi
drafts keep the normal explicit resume controls, and prepared-package review
and installation recovery remain available.

The host's preparation/import APIs and CLI remain compatibility interfaces.
External-management consent and its authority checks remain separate from the
retired native setup handoff. Commented reference code and the retirement scope
are documented in [the owning conversation contract](CONVERSATIONAL_APP_SETUP.md#native-workflow-retirement).

Closing the window leaves an active setup running in the host. **Cancel Setup**
stops owned setup work. Source files and the saved draft are retained. A host
shutdown marks active runs interrupted. Resuming creates a fresh session with
retained files, verified resources and saved opaque credential references;
interrupted conversations are not replayed. Successful preparation attaches a package to the exact draft
revision and opens review. Preparation does not install or start the app.

**Add Existing Files or Runtime…** accepts model files, runtime trees and archives separately
from text source intake. MeeCP captures independent private copies, hashes and
origin/license metadata, then places them under `resources/` in each attempt.
The picker supports cancellation while capture settles. Imported resources and
completed public downloads survive setup failures. A fresh attempt rechecks
their bytes before use and avoids downloading an already retained artifact.
The source-text limit remains unchanged. Resource bounds are 16 GiB per file,
32 GiB per collection and 64 GiB for the retained resource store; actual capture
also requires host disk and memory admission. Captured archives are retained
as files and must be explicitly extracted in the workspace. Selected runtime
trees retain bundled model containers and provenance archives without opening them.

A failed or canceled installation offers **Retry Installation**. This action
uses the same verified package and effective configuration/startup choices,
creates and applies a fresh exact installation plan, and does not call Pi.
Repeated requests return the same retry draft. Operations requiring recovery
continue through the existing recovery controls.

## Model configuration and data

The initial provider choices are `openai`, `anthropic`, `google`, and
`openai-compatible`. Enter an exact model ID supported by the pinned Pi catalog
for a built-in provider. The compatible provider accepts an explicit model ID
and endpoint. HTTPS is supported; HTTP is restricted to `localhost`, `127.0.0.1`,
or `::1`. Endpoints cannot contain credentials, query strings, or fragments.
Local compatible servers may omit the API key; the worker supplies a fixed
nonsecret placeholder required by its HTTP client.

The provider key is saved through meecp's private secret store. Settings reads
return configuration metadata and readiness, never the key or its secret-store
identity. Keys enter the worker through its private stdin protocol and remain
outside model messages, generated packages, and subprocess environments. The
worker uses the API key supplied for that job and does not load the user's Pi
credential files or environment credentials.

Saving settings sends no source files. **Create app** authorizes the coding run
against the selected source and intent. The configured model can receive staged
source, generated files, host-supplied package/schema guidance, and tool results.
For a remote provider, these requests go to that provider. The UI and durable
job records contain progress metadata and fixed error codes. Live conversation
text uses a separate bounded RAM transport for the native UI. It contains user
messages and completed assistant text, with no thinking, tool output, raw
provider objects or secret answers. There is no durable model conversation,
prompt, provider-response, or command-output log.

Conversation limits are 8,192 UTF-8 bytes per message, 64 follow-up message
identities per attempt, eight queued messages, and a ring of 128 events or
256 KiB. The host retains at most eight sessions. Ready/failed history expires
after five minutes; canceled/interrupted history is cleared immediately. A host
restart clears all conversation text. Authoritative job state and retained files
remain available for fresh-session resumption. The native UI also bounds and
validates its in-memory transcript and discards it when the host reports expiry.

## Execution and dependency boundaries

The host copies the selected source snapshot into a private workspace and checks
its revision and hashes. Pi runs through the SDK with an in-memory conversation,
settings, credential store, and model store. The resource loader provides no
discovered instructions, extensions, skills, templates, or MCP connections.
Automatic context compaction summarizes long coding sessions in memory using
the configured provider. Model catalog refresh, agent retries, and cache warming
are disabled. Individual provider requests allow at most two transient retries. The
worker only advertises these host tools:

| Tool | Host behavior |
| --- | --- |
| `capabilities` | Discovers host facilities, runtime paths, and generic package requirements. |
| `preflight` | Reports selected Metal compiler/SDK, actual device/compute readiness and managed native capabilities separately. |
| `ensure_metal_toolchain` | Selects verified installed Apple tools and automatically acquires a missing Metal component when required. |
| `native_command` | Runs bounded native work through host memory/exclusive GPU admission and owned Metal confinement. A compiler is required only for source builds that request it. |
| `retain_resource` | Captures a completed workspace dependency with hashes and license/origin metadata for future attempts. |
| `list_files` | Lists and pages through workspace or selected host-resource trees. |
| `read_file` | Reads a bounded UTF-8 file, with byte offsets for large files. |
| `write_file` | Writes a bounded UTF-8 file inside the private workspace. |
| `run_command` | Runs an explicit argument array with workspace writes, selected read access, and owned process lifetime. Network access is an explicit option. |
| `start_process` / `read_process` / `stop_process` | Starts, inspects, and reaps temporary services or background work owned by this setup. |
| `fetch_url` | Downloads a public HTTPS file up to 16 GiB, following up to five validated HTTPS redirects; expected SHA-256 and exact byte size can be supplied. Completed downloads are retained for reuse. |
| `extract_archive` | Unpacks downloaded runtime, toolchain, or source archives into the workspace. |
| `prepare_environment` | Uses an available private-runtime convenience helper. |
| `install_dependencies` | Uses an available dependency-manager convenience helper and retains locked dependency metadata. |
| `ask_user` | Waits for a missing requirement or privately supplied credential. |
| `check_package` | Independently validates `package/` and probes its MCP fixtures; fixed failure feedback lets Pi repair the package and repeat the check. |

The filesystem broker rejects traversal, symlinks, unsafe file identities, and
writes outside its workspace. Commands use a private HOME/TMPDIR and approved
runtime paths. Network-enabled commands can reach network and local services;
this option is needed for package managers, builds, and integration tests and is
distinct from `fetch_url`'s public-download policy. The host owns and reaps
process groups, including temporary background services. It inspects TCP
listeners during network commands, package verification, background work, and
installed stdio sessions, then stops an owned group if it listens beyond loopback. This
is an asynchronous listener check; macOS's sandbox does not reliably distinguish
the requested wildcard bind address from an explicit loopback bind. Generated
local services must bind to loopback and authenticate their routes. Tool results return
to the in-memory coding session so the agent can fix compilation or test
failures. Runtime identity and hashes are checked before launching Pi.

Setup downloads support ordinary release and model-host redirects. Each hop
resolves and checks every destination address, connects to a vetted public IP,
and verifies TLS against that hop's hostname. Downgrades, credentials in URLs,
private destinations, loops, malformed responses, and excess hops are rejected.
Server-issued signed query strings remain transient transport data; the host
does not return or persist them. Download limits and supplied hashes still
apply. The separate pinned prepared-package ZIP intake retains its direct-URL
contract described in [Pinned package downloads](PINNED_DOWNLOADS.md).

Network-enabled setup commands receive a private, certificate-only CA bundle.
An explicitly configured host CA file takes precedence; otherwise the host
exports the Mac's default trusted public anchors. Git receives `GIT_SSL_CAINFO`,
and compatible clients receive `SSL_CERT_FILE` and `NODE_EXTRA_CA_CERTS`. Trust
is staged once per workspace. Certificate and hostname verification stay
enabled. Setup commands receive no added Keychain or private-key access.

The [download and command-trust validation record](PI_NETWORK_VALIDATION.md)
identifies the checked public release and native Git paths.

## Automated Metal preparation

MeeCP checks runtime GPU access independently of compiler availability before
coding begins. Its shipped, hash-bound helper creates a Metal device, residency
set and compute pipeline in the same confined native execution profile. A
prebuilt engine with compiled assets can run without the `metal` compiler.

Source builds use a selected verified developer directory and SDK without
changing global `xcode-select`. MeeCP selects an installed full Xcode when the
Command Line Tools do not provide Metal. If required, it runs the fixed Apple
command `xcodebuild -downloadComponent MetalToolchain` and rechecks a real
shader compilation. Apple documents this component mechanism in
[Downloading and installing additional Xcode components](https://developer.apple.com/documentation/xcode/downloading-and-installing-additional-xcode-components).
The host validates the selected Apple control executable and records the actual
developer directory, compiler, SDK and asset identities. Selected roots and the
exact Metal asset mount remain read-only to workspace commands.

MeeCP discovers and Apple-verifies the actual compiler, Git, Make and Swift
executables under the selected developer roots. It uses those paths for known
commands and child lookup, preserving bundled Python/Node precedence. Apple's
system launchers can require services unavailable in confinement; preparation
uses the selected tools directly. Updating or removing Xcode revokes stale
selection and permits fresh discovery. Settled compiler probe failures report
unavailability without blocking CPU or prebuilt preparation; ownership loss
still closes setup admission.

If a full Xcode installation or Apple's account/license/component UI is needed,
MeeCP presents **Continue Metal Setup**, opens the appropriate Apple page or
verified Xcode application, and periodically rechecks readiness. No terminal
diagnosis or resource-path handoff is requested. Once Xcode becomes ready,
MeeCP automatically acquires a missing Metal component and resumes the original
tool. A failed acquisition waits for a readiness change or explicit Continue.
Before opening Xcode, the native client independently verifies the complete
bundle's signed Xcode identifier, Apple certificate chain and authenticated
Apple team identity. It rejects MeeCP state paths and untrusted ancestor permissions,
resolves aliases, and rechecks the active waiting job after verification.
Renamed, personal and custom selected Xcode installations remain supported.
It does not silently accept an OS license or
replace the requested inference engine.

Managed native commands have explicit argv, workspace-only writes, declared
memory, bounded duration, exclusive GPU admission, loopback listener checks and
owned descendant cleanup. Declared memory is an admission estimate, not an OS
RSS/GPU-memory limit. Temporary contention shows **Waiting for available GPU
and memory** and is rechecked automatically within the active-work deadline.
Ownership uncertainty retains the reservation and closes admission. Generic
commands continue to use their existing confinement.

Commands can receive explicit nonsecret environment values. `credentials` maps
environment names to opaque secret IDs requested during that job. The host
injects their values privately and suppresses all output from credential-bearing
commands and services. Runtime secrets are declared in the package manifest and
delivered through the host's existing private settings descriptor. The host
excludes their values from settings responses and credential-bearing command
output.

Programs given credentials are authorized to handle those values. Hiding their
output does not control their file writes or outbound requests; Pi must keep
credentials out of generated files and use them only for the requested app.

Python dependency setup reads static `pyproject.toml` metadata, uses bundled
Python 3.12.13 and `uv`, and pins MCP 1.30.0 for generated adapters. The host
resolves public PyPI wheels, checks and downloads their artifacts, then installs
offline into a copied private runtime. It retains the canonical `uv.lock`,
requirements, dependency versions, and notices. Direct dependency URLs, dynamic
project metadata, build hooks, source builds, and unsupported Python versions
are rejected or excluded from this dependency path.

Node dependency setup uses the bundled Node runtime and npm in a private
project, retains the project source for ordinary builds and local dependencies,
resolves or preserves npm lock metadata, then installs the locked
dependency closure. Published runtimes retain Node's license and provenance,
dependency identities, integrity metadata, and dependency notices. This helper
disables lifecycle scripts and reports packages with pending scripts; Pi can
run required project-specific build or install commands explicitly and test
their results. It does not load the user's npm configuration or credentials.

These helpers support common Python and Node setup paths. The agent can acquire
other runtimes and toolchains with downloads, tar/zip archive extraction, and
custom build commands. Archive intake materializes safe internal links into
regular files and rejects escaping links or paths. Available system compilers
and developer tools are reported by `capabilities` and `prepare_environment`
with runtime `system`. A package can own databases or other child services
behind its stdio launcher. The final package still needs its own complete,
relocatable dependency closure.

There is no app-family allowlist in the Pi workflow. Available machine hardware,
platform facilities, source licenses, external account access, build resources,
and the host's generic execution/package contracts still affect what can work.
The agent investigates and attempts an integration; success comes from the
independent checks of the resulting package. Support for every future app is
not established by giving a coding model general tools.

## Verification and installation

Pi writes `package/meecp.package.json`, `package/meecp.lock.json`, the launcher and
runtime closure, and `package/verification.json`. Every declared tool must have
a meaningful synthetic fixture. A fixture's `expected` field compares exact
MCP `structuredContent`. Its optional `assertions` field checks the full MCP
tool result with RFC 6901 JSON Pointer paths and `equals`, `type`, `contains`,
`exists`, or `min_length` operators. Both checks can be combined. This supports
text, image, audio, and varying outputs without forcing every tool to produce a
fixed JSON object. For example, an image fixture can assert `/content/0/type`
equals `image`, `/content/0/mimeType` equals `image/png`, and `/content/0/data`
is a string with a positive minimum length. The fixtures are included in the
package lock.

The generic [lock helper](../templates/pi_package.py) seals the files after Pi
writes a custom integration. Run it with the host-supplied Python using
`-I -S -B tools/seal_package.py package` inside the staged workspace;
`--primary-license` selects a declared
license or notice file, and `--toolchain-metadata` binds recorded JSON metadata
by hash. The default primary license is the manifest's first license. Pi must
preserve the applicable third-party licenses and attribution. The helper hashes
every artifact, records its size and executable mode, and rejects unsafe files
and empty directories. It does not remove directories, change artifact modes,
execute code, validate the full manifest, or install anything. Rerun it after
changing any package file.

During the coding loop, Pi calls `check_package` with no arguments. The host
returns a fixed failure code when a contract or fixture fails, so Pi can repair
the files and rerun the independent check before finishing. The tool grants no
installation authority. The host also verifies the result after the session
ends.

The host validates the complete package and lock, then launches the candidate
from a separate probe workspace. The probe can read the package and system
runtime resources; it cannot read the original source or host toolchain. It
initializes MCP 2025-11-25, checks the exact tools and supported capabilities,
executes the fixtures, and compares the expected results and assertions. Package hashes are
checked again after the probe. A model's statement that setup succeeded is
insufficient to reach package review.

A validated `mcp-stdio` package declaring `resources.gpu: true` can use native
Metal during this independent probe on macOS 26 Apple silicon. It must declare
one session and fit the host's memory budget and exclusive GPU admission before
launch. The host selects narrow AGX device, IOSurface residency and Metal compiler
permissions; package and system reads, private probe writes, network declarations
and owned process cleanup retain their existing restrictions. GPU reservations
remain held until the probe's owned processes are stopped and reaped, including
on cancellation or failure. Generic Pi `run_command` and `start_process` tools
retain their existing GPU-denied sandbox and cannot request this capability.

Managed `native_command` preparation uses the same narrow Metal policy.
Compatibility and
model/tool correctness still require the package's actual fixtures and named
runtime validation; broader GPU preparation recipes and multiple resident GPU
scheduling remain open. Automated host preparation and recovery are governed by
[Automated app onboarding](AUTOMATED_APP_ONBOARDING.md).
The [native validation receipt](PI_SETUP_METAL_VALIDATION.md) records the checked
Metal, Stark, and ColGrep artifacts and the remaining package acceptance scope.

Successful verification attaches an immutable package snapshot to the saved
draft revision. The existing plan/apply installer handles review, authorization,
candidate readiness, startup choice, and transactional installation. A changed
draft, changed artifact, failed fixture, unsupported capability, or failed probe
cannot be reported as a ready package. Agent-generated fixtures establish the
checked examples; they do not establish correctness on arbitrary real inputs.

## Owner API

All Pi routes require the authenticated local owner. Scoped management grants
cannot configure provider credentials or start Pi jobs.

| Route | Purpose |
| --- | --- |
| `GET /v1/pi/settings` | Provider/model metadata and runtime availability. |
| `PUT /v1/pi/settings` | Save the provider, model, optional compatible endpoint, and optional API key. |
| `POST /v1/pi/imports` | Save a description and optional HTTPS project link as an ordinary import draft. |
| `GET /v1/pi/jobs` | List saved job metadata. |
| `POST /v1/pi/jobs` | Start setup for one exact import ID/revision. |
| `GET /v1/pi/jobs/{job_id}` | Read progress and the verified attachment identity. |
| `POST /v1/pi/jobs/{job_id}/cancel` | Cancel an active job with an empty JSON object. |
| `POST /v1/pi/jobs/{job_id}/answer` | Answer the current transient question for the exact question ID and import revision. |
| `POST /v1/pi/jobs/{job_id}/messages` | Send a bounded ordinary message using a UUID message identity and exact import/revision/source binding; acceptance requires worker acknowledgment. |
| `POST /v1/pi/jobs/{job_id}/conversation` | Read bounded RAM events after a sequence cursor using the exact import/revision/source binding. The native transport uses this route. |
| `GET /v1/pi/jobs/{job_id}/conversation` | Equivalent read for clients supplying the binding and cursor as query parameters. |
| `POST /v1/pi/current` | Resolve the authoritative active/ready/latest attempt for an exact import/revision/source binding. |
| `GET /v1/pi/resources/{import_id}` | Read the current retained resource inventory. |
| `POST /v1/pi/resources` | Capture selected resources, publish a new draft revision and resume an interrupted active attempt. |
| `POST /v1/pi/resources/{import_id}/cancel-capture` | Request capture cancellation; admission remains held until work settles. |
| `POST /v1/pi/jobs/{job_id}/resume` | Create or join a fresh exact-bound attempt using retained state. |
| `POST /v1/pi/jobs/{job_id}/continue-system` | Recheck the current Apple setup prerequisite. |
| `POST /v1/imports/{draft_id}/retry-install` | Create or retrieve a fresh verified-package draft for a terminal installation failure. |

One setup job runs at a time. Jobs have bounded messages, tool calls, events,
downloads, subprocess output, and a two-hour active-work deadline. Waiting for
a user answer or required Apple UI action pauses that deadline. Saved job states
are `running`, `ready`, `failed`, `canceled`, and `interrupted`. Changing provider settings affects subsequent
jobs; an existing job keeps its captured configuration.

A job waiting for input remains `running` with stage `waiting_for_user`. Its
public response includes a transient question bound to the job, import, and
revision. Answer submission includes `question_id`, `import_id`, `revision`,
and `value`. Questions and answers are never journaled. The worker allows up to
1,024 tool calls, 512 turns, and twelve final verification/repair cycles;
context compaction can preserve progress without persisting a conversation.

## Regression checks

The [conversational setup acceptance](CONVERSATIONAL_APP_SETUP_VALIDATION.md)
records the native workspace/composer checks and real SDK steering checks for
the 2026-10-14 UI integration. These qualify transport and lifecycle behavior,
not general model quality or unfamiliar-app installation success.

The final general Pi suite passed 272 tests, with two optional live dependency
checks skipped. Separate real npm checks passed installation, CLI execution,
argument forwarding, and offline builds after relocation. Nine real SDK checks
passed against a synthetic streaming provider, including context compaction.
Compatibility, recipe upgrade, policy tuple, and opt-out checks passed; listener
tests cover foreground work, independent probes, late child services, admission
closure, and shutdown during cleanup.

The expanded Pi source closure requires a fresh TextGen recipe identity. Version
1.0.21 restores cold-host recipe readiness, model capture, and lifecycle controls
with exact current component hashes. Upgrade checks preserve enabled snapshots
and owner opt-outs from earlier recipes. The historical signed Phase 3 acceptance
records retain their original source, runtime, and policy tuples; they do not
establish signed runtime acceptance for the new recipe.

## Bundled runtime

The host builder packages Pi SDK 1.0.2 and official Node 24.15.0 for macOS arm64
under `Contents/Resources/host/pi-runtime`. The npm lockfile pins the dependency
closure; Node's download is pinned by SHA-256 and npm tarball integrity is
checked. Dependency installation scripts are disabled. The complete regular-file
inventory is refreshed after final app signing. The signed Node runtime receives
the JIT entitlement needed by V8.

[Worker source and protocol](../agents/pi/README.md),
[pinned provenance](../agents/pi/provenance.json), and licenses are kept in Git.
Node, npm dependency trees, runtime downloads, and generated bundles are ignored.
Users do not need to install Pi, Node, Python, or `uv` globally to use a complete
built app.

The SDK checks use a synthetic local streaming provider and require no paid
request. They verify brokered tool execution, cancellation, provider-error
redaction, resource/credential discovery suppression, and compatible no-key
endpoints. These checks are separate from end-to-end app setup against a real
model and from verification of a particular installed app.

## Live acceptance

On 2026-10-06, one previously unseen Python text normalizer with CC0 source
passed setup using OpenAI `gpt-5.4`. Internal coding and repair reached the host's
independent package/protocol/fixture checks, attached the verified package to the
draft, completed plan/apply installation, and passed three authenticated
Streamable HTTP MCP gateway calls covering whitespace, empty input, and Unicode
case folding. No recipe was authored for the integration.

Eight earlier development runs failed or were interrupted while error feedback,
package path guidance, and newline RPC framing were changing. Those runs do not
provide a controlled model comparison or an estimate of app-setup reliability.
The successful case establishes this checked source, model, and runtime path;
additional projects and dependency ecosystems still need their own evidence.

The general workflow subsequently passed two once-only acceptance runs using
OpenAI `gpt-5.4` and the separately built `dist/pi-general-host-v2` host. A Node
CSV integration installed an actual `csv-parse` 6.2.1 npm dependency closure and
passed three authenticated gateway calls covering ordinary records, quoted
commas and newlines, and Unicode fields. A native checksum integration included
a compiled Mach-O checksum executable and a C MCP adapter that invokes it; its
three authenticated gateway calls checked empty input, `hello`, and `foobar`.
Both cases passed independent package verification, attached their packages,
completed disposable installation, and shut down their owned hosts. Disposable
provider and runtime credential stores were revoked. Neither case required an
app recipe, and neither was retried. These results establish the checked Node
dependency and native build paths; they do not establish universal installation
reliability. Later source fixes require their own regression checks.

The portable [live acceptance CLI](../scripts/verify_pi_setup_live.py) runs a
selected synthetic source and three known gateway fixtures against a separately
built host. `--case python` (the default) selects the normalizer; `--case node`
selects a JavaScript CSV parser with an external npm dependency; `--case native`
selects a C checksum program that must be compiled and invoked by the package.
These examples test reusable execution facilities and do not reveal the user's
later acceptance idea. Invoking the CLI makes paid OpenAI requests with the supplied key. Build
into a fresh output path, then run:

```sh
uv run --locked python scripts/build_meecp_host.py --output dist/pi-live-host
uv run --locked python scripts/verify_pi_setup_live.py \
  --host-root dist/pi-live-host --key-file /absolute/path/openai-key.txt \
  --model gpt-5.4 --case node
```

For an app bundle, `--host-root` is its `Contents/Resources/host` directory. The
CLI uses the bundled backend and dependencies without changing the worker or
inventories. Each run creates private, ignored state under
`test-artifacts/pi-live/<run-id>`; `--run-root` selects another private parent
directory. Progress and results contain metadata only. SIGINT or SIGTERM requests
cancellation; cleanup joins owned work, closes the disposable host, and revokes
its temporary provider credential. The prepared artifacts remain available for
inspection. The CLI itself has been checked for help, fixed argument-failure
output, and synthetic signal/deadline cancellation without another paid request;
the measured acceptance above used its development harness.
