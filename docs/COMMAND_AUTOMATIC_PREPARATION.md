# Native command preparation

The trusted `host-command-json-v1` family converts a captured application directory into an explicit schema-2 host-command candidate. Its current development probe verifies the MCP interface, synthetic output, cancellation and native ownership. The original acceptance gate remains open: the native helper does not provide aggregate tree memory/process enforcement or a positive measured memory report, and the common verified-attachment gate must continue rejecting that missing evidence. Public admission remains disabled.

## Supported source and questions

The [default registration checkpoint](COMMAND_DEFAULT_REGISTRATION.md) makes
these static questions available on a supported packaged host with an exact
format-3 helper inventory. Recipe metadata and helper identity do not enable
execution.

The source is `application_directory` contract version 2. The host byte reader verifies the exact captured file closure, executable flags, text/binary kinds and source identity without following links. Legacy text-directory and document contracts retain their existing behavior.

The initial family accepts a thin arm64 Mach-O executable with a macOS system loader and system-only dynamic libraries. Inspection parses bounded load commands without launching the source. External libraries, loader environment commands, rpaths and unknown loader commands are rejected. At most eight executable candidates and eight schema pairs are considered.

An ordinary `<name>.input.schema.json` and matching `<name>.output.schema.json` declare the JSON object interface. Both schemas must satisfy the existing bounded host-command structural subset. Missing or unsupported schemas remain blockers. The form asks for the app/tool name, description, fixed arguments, synthetic input and expected result, deadline, memory reservation, source notice, and explicit confirmation of the JSON interface and absence of external effects. It does not ask the user to write a package manifest. Dynamic argument substitution and arbitrary runtime dependency resolution are unsupported.

The trusted copy builder maps the entire original closure beneath `source/`, gives execute permission only to the selected executable, and emits the full command/tool/license/artifact contract. It does not install dependencies or execute selected source. The command runs with fixed argv, an empty environment, package-local cwd, bounded JSON stdin/stdout, and discarded stderr. The source identity and full command contract remain bound independently of the later native completion record.

## Job authority and recovery

Preparation uses no synthetic installed slot. `CommandPreparationOwner` binds the job, attempt, complete job binding, candidate, synthetic fixtures, helper, platform policy and receipt format. A trusted runner constructor provides the helper capability. An API, recipe or source file cannot construct that capability.

Before registration in the common ownership store, the owner durably writes `owner.json`, an empty `invocations.json`, and an initialized format-3 native journal under the private workspace's `command-probe-v3` directory. Each invocation intent, containing only instance/spec/authority digests, is durable before calling the native executor. The native executor itself durably records its entry before `Popen`. Payloads, source bytes, argv, request IDs and command results are not written to these journals.

The executor owns its helper in an independent session. The target's confined fork/exec group cannot escape with `setsid`, `setpgid`, or `posix_spawn`. The helper retains the original leader identity and publishes format-3 completion only after the reviewed descendant-settlement protocol. The MCP protocol session runs inside the host and owns no outer reaper group that could kill the helper while leaving its target group alive.

Recovery acquires the existing private journal lock without creating a missing file. It requires the exact owner metadata, invocation intents and valid native journal. Every native entry must match a prior intent and exact helper/policy/spec identities and have a valid format-3 completion proof. Missing files, unmatched entries, old receipt formats and native receipt failures retain ownership and reservations. An intent absent from the existing initialized journal proves nonlaunch only after the writer lock is released, using the two durable ordering constraints above. No PID discovery, adoption, individual signaling or execution replay is involved.

`recover_command_ownership` returns a digest over the validated owner, intents and native journal. The common store independently recomputes it before deleting its owner row. Native completion cannot substitute for current import revision, recipe, descriptor, candidate or job authority.

Package/source hashing during owner registration runs on a joined worker outside the SQLite lock and write transaction. The short registration transaction rechecks current authority, revision, state and complete job binding. Protocol publication also hashes on a joined worker and rechecks authority, deadline and session state after it returns, before native launch or each response write. Cancellation joins these workers before releasing their ownership. Private pipe setup transfers descriptor ownership before its first await so canceled setup cannot later close a reused descriptor.

## Probe and remaining integration

The specialist verifier uses an actual MCP SDK `ClientSession` over private bounded pipes. It compares the complete tool schema and every declared synthetic fixture, closes and joins owned work, settles command receipts, and rehashes the package before returning functional results. The preparation-only protocol constructor requires an explicit trusted publication callback; installed sessions cannot override their slot/generation guard with it.

The shared source, form, candidate and SQLite contracts are integrated in development. Tests use actual captured directory bytes after removal of the original directory, generic questions, the copy builder, the SDK and native descendants. A complete runner test controls distribution discovery only and exercises the real command family and settlement. Deterministic 200 ms worker barriers cover concurrent status reads and cancel/revision winners at registration, session admission, invocation and response publication; a separate constructor cancellation test verifies the runner retains the just-created owner until joined cleanup.

The verifier reports unavailable aggregate memory as zero, never as an invented positive measurement. That report is intentionally insufficient for the unchanged common verified-attachment gate, and the real managed tests assert rejection with no attachment. Full directory-to-attachment/install/relocation acceptance requires actual resource evidence and enforcement, independent review, and the final bundled/native release checks. Passing the component and native ownership tests does not close A3.
