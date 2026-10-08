# Native agent acceptance against a combined signed release

`scripts/verify_phase3_public_release_native_agent.py` accepts a clean signed app
whose shipped policy admits ordinary Python/MCP, ONNX, CLI and TextGen `1.0.12`
with engine `llamacpp-cpu-v1`. Its agent record must contain exactly empty
`components` and `providers` lists and `unlocked_resolution: false`. It adds no
public gate or runtime setting.

The initial host starts through the shipped `bin/meecp serve` command. Its actual
capabilities must identify the exact ordinary family and recipe closures,
ordinary resolution producer `1.0.1`, package schemas `[1, 2]`, and the four
supported execution profiles. After relocating the signed app, the packaged
`ReleasePolicy` parser validates the complete policy and its closed agent record;
the actual capability families and closures must match that typed policy exactly.
Agent families or closures are rejected. The
provider registry must be empty, live execution false, and both API job lists
and all four preparation/resolution/agent ledger counts must be zero.

The two generated-adapter cases retain the existing development harness in
`verify_phase3_native_agent.py`. Each supplies a private trusted factory and
the reviewed request-derived `httpx.MockTransport` provider. This path bypasses
the public release loader by design. The provider sees only the encoded reviewed
request, and makes exactly four simulated role calls per case. It supplies no
dependency version or lock; the ordinary owned resolver performs that work.
There is no configured credential, live provider call or model inference.

The scalar and dependency cases exercise the production native agent selection,
proposal handoff, generated form, owned resolution, preparation/probe, matching
attachment, ordinary installation, actual SDK calls through HTTP and stdio,
source removal, restart, and a relocated confined package probe. Each case
requires two calls per transport. Captured source is removed; the state ledger
is retained until mandatory owned cleanup settles. Unsettled ownership retains
the isolated workspace. The existing native owner/nonce/scope fences and shared
whole-app relocation helper remain unchanged.

The driver verifies the original complete signed app file/directory identities
and hashes before and after, checks strict Developer ID app signatures, and
preserves the original request-only fixture and both harness source files.
Reports record public default discovery separately from private case authority.
`public_agent_execution_verified` and `a5_scope_complete` remain false. Passing
this harness does not close live provider, public agent, physical dialog,
aggregate resource, lifetime accounting, or separate ordinary-family execution
acceptance. Existing private proofs remain unchanged.

Run the new script with the candidate's private Python runtime and explicit
`--host`, `--native` and fresh `--output` paths. The host and native executable
must belong to the same `.app`. The output is private mode `0600`; existing
reports are rejected. Raw native/CLI/provider source and output stay in RAM.
Only bounded status/count summaries are printed. Actual execution must be
coordinated with other acceptance lanes; this harness does not need external
model files.
