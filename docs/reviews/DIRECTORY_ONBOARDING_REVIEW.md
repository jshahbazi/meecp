# Directory-to-app architecture review

Review date: 2026-10-03. Code baseline: `1fa4dd7196f70823f1b80764f69ed80b0f8b7a9a`. This is an implementation review and proposed delivery order. Automatic preparation is still unavailable at this baseline. The [foundation](../PREPARATION_FOUNDATION.md) provides metadata, registry and admission contracts; it does not execute builders.

## Recommendation

Make **Phase 3: automatic app preparation** the next product milestone. Move the existing multiple-model-engine milestone to Phase 4. Selecting a directory should let mcpme inspect what is present, establish the desired tools, assemble a supported adapter and its dependencies, verify the resulting package, and continue to the existing Add App review. The user should supply purpose, choices and credentials when necessary. mcpme should generate package files, locks and preparation descriptors.

Keep the host centered on package identity, MCP contracts, process ownership and lifecycle. Put language, adapter, provider and model knowledge in versioned components and recipes. Python, JevK5 and the hotdog detector are useful acceptance fixtures. They must not become branches in the common onboarding architecture.

An app can be a small local adapter calling GLM, DeepSeek, Bedrock or an existing local LLM. Source language, exposed tools and execution target are independent choices. A directory can contain several languages, several services, a frontend, model assets and provider configuration simultaneously. Inspection should return candidates and evidence rather than assign one permanent app type.

The immediate bundled starter and prepared-package improvements are worthwhile Phase 2 usability work. They give users a working app and an understandable acquisition path. Completion of those changes does not establish directory-to-app preparation.

## What the current design gets right

The [automatic preparation plan](../AUTOMATIC_PREPARATION.md) already specifies most of the necessary boundaries:

- Static intake does not run repository code. Immutable inputs, exact recipe versions and source revisions bind execution and attachment.
- A recipe can compose supported components; a new runtime authority or process topology requires host implementation and acceptance.
- Preparation and installation have distinct authority and recovery rules. Every preparation route returns to the same sealed-package attachment and installation flow.
- An independent verifier checks the actual result. Recipe output and agent assurances cannot authorize installation or satisfy their own host gates.
- Engine compatibility and task interpretation are separate. A weight file does not define an MCP interface.
- Generated native forms and independently added recipes are explicit acceptance requirements.
- Unknown GUI applications require an API, CLI or specific automation adapter. The design does not promise that an agent can derive useful tools from any executable.

The [foundation implementation](../../backend/mcpme/preparations.py) also addresses a difficult race correctly: cancellation, recipe revocation, draft identity and verified package identity are checked when attachment is admitted. Preserve that work while adding execution.

## Concrete gaps in the implementation

| Evidence in the reviewed code | Consequence | Required next change |
| --- | --- | --- |
| `preparation_contracts.py:266–287` permits only `python-callable` and `python-mcp`, with Python module identities and fixed CPython/MCP dependency versions. | A non-Python recipe cannot describe its entrypoint or dependency closure. | Introduce a versioned common envelope with component-owned, strictly validated bindings. Preserve descriptor-v1 parsing and digests. |
| `preparations.py:90–106` selects components from the Python interface kind, checks `uv.lock`/`pyproject.toml`, resolves Python module filenames and accepts only stdio preparation. | Adding Node or a native MCP executable currently requires edits to the common planner. | Move these checks into the registered Python preparation capability; let the common planner validate resolved component contracts and identities. |
| `recipes.py:40–46,233–242` recognizes two bundled components and rejects executable recipes. Registered recipes remain `runnable: false`. | Static recipe composition is implemented; runtime extension is unproven. | Implement the controlled runner and independent verifier, then pass a fresh-recipe execution gate. |
| `templates/prepare.py:118–154` obtains managed Python and locked wheels through `uv`; Python launchers are assembled at `:157–183`. | The development generator is not an installed-app preparation toolchain. | Ship a pinned private toolchain and verify it with development PATH, checkout and caches unavailable. |
| `templates/prepare.py:233–248` can package a selected prebuilt executable. | There is a useful existing-MCP packaging seam, but it does not discover an arbitrary executable's tools or bundle its native dependencies. | Add binary intake, executable/runtime closure checks and a non-Python MCP recipe before advertising this automatic path. |
| `preparation_contracts.py:305–308` fixes CPU-only resources; `contracts.py:197–205` requires language-model context metadata for model artifacts. | Preparation cannot describe every model topology; resident vision would need a different model contract. | Keep the first CPU detector explicit. Introduce discriminated model metadata only when a supported resident/GPU family needs it. |
| `preparations.py:119–125` reports unavailable runner/verifier capabilities; `:138–143` fixes the candidate to stdio. | No directory is automatically prepared through the management API today. | Add execution endpoints only with owned execution, cancellation/recovery and real verification. |

The package layer is already broader than the preparation layer: [PackageManifest](../../backend/mcpme/contracts.py) supports `mcp-stdio`, constrained `mcp-http` and `model-service`. HTTP validation at `contracts.py:439–448` requires a particular single-service readiness and identity contract. An arbitrary HTTP server URL does not satisfy it.

Two statements in the older preparation plan need reconciliation when the roadmap is updated: its header says implementation has not started, despite the merged foundation; its starting-point table says HTTP-only packages are unsupported, despite the current constrained HTTP package implementation. The narrower statement remains correct: the automatic preparation planner does not support HTTP preparation yet.

## A common inspection and recipe interface

Use four independently versioned objects. The following is a proposed interface, not a claim about existing public endpoints.

| Object | Common content | Family-specific content |
| --- | --- | --- |
| Inspection result | Source identity; selected-file scope; bounded observations; candidate IDs; evidence references; unresolved choices; blockers. | Host-owned parsers recognize project metadata, exports, executable metadata, model headers and declared service interfaces. |
| Preparation descriptor | Exact recipe/component identities; requested MCP tools; input/artifact identities; target binding; effects/resources; lifecycle; synthetic acceptance fixtures. | Strict component input schemas describe callable bindings, entrypoints, dependency projects, engine settings or provider configuration. |
| Execution plan | Exact input closure; typed build/acquisition/probe steps; network destinations; resource/deadline bounds; execution authority. | Each admitted component contributes a validated step implementation and expected output contract. |
| Result and attestation | Sealed package digest; source/descriptor/component identities; actual interface; independent gate evidence; attachment binding. | Component-specific compatibility and semantic checks supplement mandatory host checks. |

The inspection result should represent three different facts: **observed** metadata, **proposed** interpretation, and **verified** capability. Finding `package.json`, a Python annotation, a `.gguf` suffix or an API hostname only supplies evidence for a candidate. It does not prove a useful interface, complete dependencies or operational compatibility.

For a monorepo, enumerate bounded service roots and let the user select the intended service. For a mixed application, compose the selected parts. Do not recursively import every function, select the first manifest or expose a generic command-execution tool. Inspect the selected snapshot, and preserve explicit inclusion/exclusion choices when a source exceeds intake limits.

An exact supplied descriptor can resolve a candidate automatically. Otherwise, collect only choices that remain consequential: the service root, what the assistant should do, conflicting entrypoints, material side effects, provider/endpoint, or missing configuration. The UI should generate supported schemas and fixtures from known templates and constrained source metadata; complex semantics can go through agent proposal. Asking a user to write JSON schemas, locks or manifests is not completion of automatic onboarding.

A missing dependency lock should start a supported dependency-resolution step in a private preparation workspace. It should not require editing the user's repository. The resolved lock and acquisition origins become exact inputs before package verification. Dependency inference that cannot be resolved reliably remains a focused blocker; arbitrary repository install scripts must not run during inspection.

Represent original dependency requirements and generated resolution artifacts separately. A bounded resolution attempt binds the source, recipe, allowed origins and execution budget before running; its resulting lock receives an exact identity and derivation receipt. The final build descriptor binds that derived artifact without pretending it was present in the original snapshot or rewriting the source identity. Discovery of a dynamic MCP interface needs the same distinction between an authorized discovery attempt and the final tool contract. This requires an explicit contract extension: the current planner demands a source-snapshot lock and complete tools before reserving a job. Expanded effects or acquisition scope require an updated binding before execution continues.

Do not weaken the current descriptor with an unrestricted dictionary of commands or provider settings. Keep a closed common envelope and a closed, versioned schema for every selected component. The host validates paths, secret references, capabilities and effect ceilings independently. Runtime-specific validators belong to admitted capabilities, and source/recipe code never supplies executable inspection hooks. New semantics must be explicitly advertised; old descriptor-v1 identities remain stable.

The existing `inputs` field and recipe forms provide a starting point, but they do not remove the required Python interface/dependency fields. Schema evolution and a planner refactor are necessary to make this contract genuinely usable across families.

## Source family and execution target

| Selected source | What inspection can suggest | What preparation must establish |
| --- | --- | --- |
| Existing MCP project | Declared entrypoint, language, transport and dependency metadata. | Private runtime closure; supported MCP capabilities; real tool discovery under execution authority; full tool contracts; lifecycle and relocation. |
| Python library/function | Statically visible callables, signatures and project dependencies. | Desired function, argument/result semantics, adapter, locked wheels/runtime, fixtures and effects. Dynamic imports/decorators may prevent reliable static inference. |
| Node project | Workspaces, package-manager lock, declared scripts/exports and likely service roots. | Selected service, private Node/runtime closure, explicit script/build behavior, native-addon compatibility and actual MCP or adapter contract. A package script is executable source. |
| CLI or native program | Executable/build metadata and documented interfaces. | Fixed invocation contract, typed input/output, dependency closure, architecture, bounded output and host-owned child cleanup. Running `--help` is execution. |
| Model assets | File identities, declared format, candidate engine/task compatibility. | Exact engine, task adapter, preprocessing/tokenization, artifacts, licenses, real inference and measured resource bounds. |
| GUI app or frontend | Bundle/project metadata and declared APIs. | A useful supported API/CLI/automation surface, permissions, lifecycle and semantic fixtures. Visual presence alone supplies no MCP tool contract. |
| Document/setup brief | Text metadata and possible role. | Whether it is reference information or requirements; a tested retrieval/service adapter for the information route. |

Execution target is a separate selection:

| Target | Package and ownership | Required behavior |
| --- | --- | --- |
| Local computation | Host owns the packaged adapter/runtime and declared children. | Closed runtime dependencies, process cleanup, resource admission and functional checks. |
| Remote API/provider | Host owns a local MCP adapter with a protocol/auth component; the provider owns inference. | Declared endpoints/data transfer, selected secret references, account/model configuration, time/output/usage limits and adapter probes. No fabricated weight files or local engine metadata. |
| Existing local LLM endpoint | Host owns its connector; another application owns the endpoint and model. | Explicit endpoint selection, protocol/identity checks, availability errors and no adoption, restart or termination of the external server. |
| Managed local engine | Host owns adapter, engine and weights through the supported process graph. | Exact artifacts, authenticated readiness, dependency order, actual inference, memory/GPU admission and supervised cancellation. |

A connector can be implemented in Python, Node or a native binary. A Python app can call a remote provider. A local model can expose an API. These combinations should compose rather than multiply host-specific app categories.

For GLM, DeepSeek and Bedrock, select a tested protocol/auth component and model/endpoint configuration. A compatible request format may allow reuse, but it does not establish equivalent streaming, structured-output, cancellation, context or authentication behavior. Keep provider names in recipe/catalog metadata and tested configurations rather than native window or supervisor dispatch branches.

Authentication requires explicit capability selection. Z.AI documents bearer-key authentication and distinct endpoint guidance for its coding offering. AWS documents both SigV4 and Bedrock API-key authentication, together with IAM requirements. Consequently, even “Bedrock” should not imply one universal auth mechanism. The selected adapter must bind the actual endpoint/API/auth combination. These are upstream API facts, not claims that mcpme currently supports either provider. [Z.AI API introduction](https://docs.z.ai/api-reference/introduction), [Amazon Bedrock endpoints](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html).

Reuse the current configuration/secret-reference machinery where it fits. The preparation planner currently rejects secret references, so automatic provider preparation needs an explicit extension. For expiring AWS credentials, choose and test a credential-refresh mechanism; do not silently inherit the user's shell environment or assume an immutable secret record can refresh a session. Do not expose credentials in descriptors, packages, journals or diagnostics.

The connector review should say what inputs leave the machine and where. Adapter code should constrain destinations, redirects and credential forwarding, while the UI retains the existing distinction between declared access and OS-enforced isolation. Local endpoints require a separate policy from public provider endpoints. Checks should establish the selected endpoint and model configuration without claiming cryptographic proof of remote model weights.

Bound each request's input/output, duration, concurrency and supported usage measures. A local timeout or MCP cancellation can end waiting while a provider continues inference or billing; report that outcome honestly and do not automatically retry an ambiguous billable operation. Keep prompt/response content out of host telemetry. Persisted templates, if needed, are explicitly selected package/source assets, not captured conversation logs.

A preparation agent's provider account is also distinct from an installed app's provider account. Configuring one must not implicitly authorize source transfer or runtime calls for the other.

## Deterministic preparation and agent assistance

Use deterministic components whenever the interface and compatibility are known. They can generate adapters, resolve supported dependencies, copy private runtimes, write manifests/locks, seal packages and run fixtures without an LLM account.

Agent assistance is valuable for unfamiliar source semantics, adapter implementation, dependency investigation and proposed tool contracts. It should be an optional analysis/implementation backend on the same job pipeline. Start with selected source and bounded analysis authority; bind proposed interface, source changes and execution scope before builds/probes. Generated code still needs the same runtime closure, ownership and independent acceptance gates.

The agent route should produce a concrete adapter or a specific unresolved requirement. A generic GUI application with no supported integration surface can remain unsupported. A provider name without an account, intended tools or endpoint cannot become a tested service automatically. Adding model access does not resolve these missing contracts.

## Process ownership and toolchain constraints

Two process graphs must be verified: preparation-time tools and installed runtime processes. The current stdio reaper does not establish ownership of arbitrary compiler, package-manager, CLI or executable-recipe descendants. Prove cancellation, launch acknowledgments, inherited descriptors and parent death for each supported topology. If ownership is lost, retain reservations and block replacement until reconciliation.

The first worker should reuse deterministic builder logic through a controlled service interface, not shell out to an unspecified development installation of `templates/prepare.py`. Pin and ship the private preparation runtime and resolver, retain required notices, restrict supported build steps, and exercise dependency acquisition with a clean cache. Source builds and package lifecycle scripts introduce additional execution scope and dependency closure work. Native compilation also needs an explicit compiler/SDK availability and redistribution decision; a user's Xcode installation is not an invisible product dependency.

Output packages must work after relocation with the source checkout, preparation workspace and global language runtimes unavailable. Dependency acquisition belongs to preparation, never ordinary app startup. A supported build is reproducible from recorded inputs to the extent promised by its recipe; do not claim bit-for-bit reproducibility without measuring it.

## Bounded delivery sequence and acceptance

| Milestone | Deliverable | Release evidence |
| --- | --- | --- |
| Phase 2 usability | Bundled working starter, clear prepared-package acquisition and honest source-preparation state. | Native Add App succeeds from a released/relocated app; no claim of automatic source assembly. |
| Phase 3.1: owned stdio preparation | Private preparation toolchain, runner/verifier, static source candidates, generated inputs and native progress for Python callable and existing Python MCP recipes. | Separate A1 cases from directory selection through preparation, exact attachment, installation, invocation and restart. No prompt copying or user-written manifest. Cancellation/crash/revision races pass. |
| Phase 3.2: extensible automatic milestone | Generalized descriptor/planner seam, independently added recipe, binary artifacts and exact CPU ONNX task recipe. | A0/A0R/A1/A2 from the existing plan, including real inference and measured image limits. A new recipe runs through forms, preparation and restart with zero host dispatcher/UI/import edits. |
| Phase 3.3: additional adapters | Individually gated Node/prebuilt-native MCP, supervised CLI, provider/local-endpoint connectors and bounded integrated-agent preparation. | Every advertised family has its own runtime/ownership or protocol/auth gate. At least one non-Python implementation proves the generalized contract. Each provider configuration gets actual authorized probes; mocks prove only local behavior. |
| Phase 4: broader managed inference | Second engine, broader resident/GPU profiles and additional task adapters. | Actual model/engine identity, measured memory/contention, enforced admission, cancellation and JevK5 regression parity. |

These are dependency boundaries, not a requirement to finish every family before delivering useful functionality. Component/toolchain investigations and UI work can run in parallel. Phase 3.1 is a bounded implementation slice; the previously specified first usable automatic milestone still requires the A0/A0R/A1/A2 evidence. Changing that release claim should be explicit.

Do not treat two Python variants as proof of ecosystem independence. The fresh recipe gate proves extension within existing capabilities; a separate non-Python gate proves the dependency and entrypoint abstraction. A remote connector proves another target type while reusing an existing adapter runtime. These are different acceptance claims.

All automatic routes require a real MCP client to compare full tools/schemas and execute nonsecret functional fixtures against the sealed result. Before and after probes, verify package identity. Independently test stale source/revision, duplicate start, cancellation/revocation during attach, restart with surviving work, dependency failure, missing notices and content-free errors. Installation retains its own existing verification; a passing preparation receipt does not replace it.

## Next executable implementation packet

Implement **installed-host automatic stdio preparation** next. Scope the first candidate to an explicitly selected simple Python callable and a separately selected foreground Python MCP server, while removing Python checks from the common planner behind a versioned component-validation seam.

1. Freeze the component validation/result interface and an inspection-candidate schema, including bounded discovery/resolution attempts and derived lock identities. Preserve descriptor-v1 behavior and use a new version for broader semantics. Seed a non-Python fake component to prove routing does not depend on Python module or lockfile fields; label this a contract test only.
2. Package the pinned private builder toolchain. Produce the Python runtime, locked dependency closure and complete notices without a development checkout, global `uv` or warm managed-Python cache.
3. Implement the owned preparation runner and independent verifier against the durable foundation. Add job-bound start/inspect/cancel API and CLI behavior with exact authority, process settlement and attachment semantics. Do not expose a caller-controlled “verified” endpoint.
4. Add bounded source inspection and generated input selection. For the initial supported callable subset, generate the adapter/schema from supported metadata and obtain any missing semantic choice or synthetic expected result through ordinary native fields. For existing MCP, perform discovery only inside an authorized probe job, then bind the actual exposed interface before final admission.
5. Connect the native Prepare action to real stage/progress/error data and the existing attachment/review flow. Closing and reopening the window resumes the job; canceled or stale work cannot publish a package.
6. Verify each path from a relocated built app with an empty preparation cache and unavailable source/global runtime after sealing. Execute real tools, restart the host, and test cancellation and source-revision races. Preserve the external generator as a supported development path.

The immediate blockers are the absent private preparation toolchain, absent execution/probe supervisor, absent native candidate/form binding and Python-only planner contract. They are implementation work, not missing user package files. Binary intake and ONNX resource evidence block the full A2 milestone. Native/CLI descendants need proven ownership; provider connectors need a selected protocol/auth/credential-refresh contract and authorized integration fixtures. Generalized GUI automation and arbitrary source builds remain separate capability work until those contracts can be stated and tested.
