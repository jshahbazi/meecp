# App setup with Pi

Status: implemented in the current source, 2026-10-06. This document describes
the native Add App flow and its host contracts. General installation reliability
across unfamiliar projects has not been established.

## Add an app

1. Open **Add App**. Choose or drop a source folder, choose a Markdown/text setup
   file, paste a public HTTPS project link, or describe the app you want. For a
   single Python or JavaScript source file, choose its containing folder.
2. Describe the tools your assistant should be able to call, including useful
   inputs and outputs. Choose **Use these instructions** for a setup document.
3. Open **Model Settings…** once to select the provider and model used by Pi.
   Save an API key for a remote provider, or configure an existing compatible
   local model endpoint.
4. Select **Set Up App**. Pi inspects the staged source, writes the adapter and
   package, installs supported dependencies, runs checks, and repairs failures.
5. Review the verified package's tools, declared access, configuration, and
   startup choices. Select **Add App** or **Add & Start App** to install it.

Pi can write a new integration without a registered recipe. The package
manifest, lockfile, launcher, provenance, licenses, and verification fixtures are
generated setup outputs. Recipe selection and authoring are absent from the
normal Pi flow.

**Advanced setup options** contains the included JSON Utilities app, existing
service connection, model capture, publisher package download, and earlier
preparation workflows. Existing prepared packages can still proceed directly to
review. Reference-document services and model-engine packages have their own
supported paths; the Pi verifier currently accepts `mcp-stdio` packages.

Closing the window leaves an active setup running in the host. **Cancel Setup**
stops owned setup work. Source files and the saved draft are retained. A host
shutdown cancels active runs; interrupted conversations are not replayed on the
next launch. Successful preparation attaches a package to the exact draft
revision and opens review. Preparation does not install or start the app.

## Model configuration and data

The initial provider choices are `openai`, `anthropic`, `google`, and
`openai-compatible`. Enter an exact model ID supported by the pinned Pi catalog
for a built-in provider. The compatible provider accepts an explicit model ID
and endpoint. HTTPS is supported; HTTP is restricted to `localhost`, `127.0.0.1`,
or `::1`. Endpoints cannot contain credentials, query strings, or fragments.
Local compatible servers may omit the API key; the worker supplies a fixed
nonsecret placeholder required by its HTTP client.

The provider key is saved through mcpme's private secret store. Settings reads
return configuration metadata and readiness, never the key or its secret-store
identity. Keys enter the worker through its private stdin protocol and remain
outside model messages, generated packages, and subprocess environments. The
worker uses the API key supplied for that job and does not load the user's Pi
credential files or environment credentials.

Saving settings sends no source files. **Set Up App** authorizes the coding run
against the selected source and intent. The configured model can receive staged
source, generated files, host-supplied package/schema guidance, and tool results.
For a remote provider, these requests go to that provider. The UI and durable
job records contain progress metadata and fixed error codes; there is no model
conversation, prompt, provider-response, or command-output log.

## Execution and dependency boundaries

The host copies the selected source snapshot into a private workspace and checks
its revision and hashes. Pi runs through the SDK with an in-memory conversation,
settings, credential store, and model store. The resource loader provides no
discovered instructions, extensions, skills, templates, or MCP connections.
Automatic model catalog refresh, compaction, agent retries, and cache warming are
disabled. Individual provider requests allow at most two transient retries. The
worker only advertises these host tools:

| Tool | Host behavior |
| --- | --- |
| `read_file` | Reads a bounded UTF-8 file from the workspace or selected host resources. |
| `write_file` | Writes a bounded UTF-8 file inside the private workspace. |
| `run_command` | Runs an explicit argument array in a macOS sandbox with workspace writes, selected read access, owned process lifetime, and no network access. |
| `fetch_url` | Downloads a bounded public HTTPS file through the host's address, redirect, and transfer policy; an expected SHA-256 can be supplied. |
| `install_dependencies` | Resolves a static Python dependency declaration through the host and publishes a private, locked runtime. |
| `check_package` | Independently validates `package/` and probes its MCP fixtures; fixed failure feedback lets Pi repair the package and repeat the check. |

The filesystem broker rejects traversal, symlinks, unsafe file identities, and
writes outside its workspace. Commands use a private HOME/TMPDIR and approved
runtime paths. The host owns and reaps their process groups. Tool results return
to the in-memory coding session so the agent can fix compilation or test
failures. Runtime identity and hashes are checked before launching Pi.

Python dependency setup reads static `pyproject.toml` metadata, uses bundled
Python 3.12.13 and `uv`, and pins MCP 1.30.0 for generated adapters. The host
resolves public PyPI wheels, checks and downloads their artifacts, then installs
offline into a copied private runtime. It retains the canonical `uv.lock`,
requirements, dependency versions, and notices. Direct dependency URLs, dynamic
project metadata, build hooks, source builds, and unsupported Python versions
are rejected or excluded from this dependency path.

Node dependency installation currently returns
`pi_node_dependencies_unavailable`. Dependency-free JavaScript can use the
bundled Node runtime during preparation. A generated Node package still needs
its own verified runtime and complete dependency closure. A compiler, platform
facility, runtime topology, or dependency outside the supported host contract
can prevent setup; a capable coding model does not establish compatibility.

## Verification and installation

Pi writes `package/mcpme.package.json`, `package/mcpme.lock.json`, the launcher and
runtime closure, and `package/verification.json`. Synthetic fixtures have the form
`{tool, arguments, expected}`. Every declared tool must have a fixture, and the
expected value is its exact MCP `structuredContent`. The fixtures are included
in the package lock.

The generic [lock helper](../templates/pi_package.py) seals the files after Pi
writes a custom integration. Run it with the host-supplied Python using
`-I -S templates/pi_package.py package`; `--primary-license` selects a declared
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
executes the fixtures, and compares the structured results. Package hashes are
checked again after the probe. A model's statement that setup succeeded is
insufficient to reach package review.

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

One setup job runs at a time. Jobs have bounded messages, tool calls, events,
downloads, subprocess output, and deadlines. Saved job states are `running`,
`ready`, `failed`, and `canceled`. Changing provider settings affects subsequent
jobs; an existing job keeps its captured configuration.

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

The portable [live acceptance CLI](../scripts/verify_pi_setup_live.py) repeats the
fixed synthetic source and three known gateway fixtures against a separately
built host. Invoking it makes paid OpenAI requests with the supplied key. Build
into a fresh output path, then run:

```sh
uv run --locked python scripts/build_mcpme_host.py --output dist/pi-live-host
uv run --locked python scripts/verify_pi_setup_live.py \
  --host-root dist/pi-live-host --key-file /absolute/path/openai-key.txt \
  --model gpt-5.4
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
