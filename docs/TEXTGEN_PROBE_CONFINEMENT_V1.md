# Pinned CPU text-generation probe confinement

This checkpoint wires restricted probes into `TextGenerationFamily.verify`.
The family supplies `TextgenProbeConfinementV1.from_host`; recipes, descriptors
and source bytes cannot select that factory or widen its policy. Ordinary direct
library verifier calls retain their optional-plan behavior. Public preparation
and installed runtime admission remain unchanged.

## Binding and ownership

The factory verifies the complete private host toolchain and requires its digest
to match the sealed receipt's sole `private-python-toolchain` runtime. Both native
launcher hashes come from that verified host closure and must match each other
and the candidate bytes. Existing model-package validation rejects unknown
model, engine closure, adapter bytes and process topology. No published launcher,
recipe or model fixture is rewritten by this checkpoint.

The frozen plan binds package/descriptor identities, the native reaper, sandbox
binary, complete current OS/kernel identity, all package file identities and
internal directory identities. A metadata baseline surrounds full package
validation and critical executable hashing, preventing mutation during capture
from becoming the accepted baseline. Package directories include modification
identity, so newly added files also invalidate the native launch guard. Private
home and temporary directories are fixed children of the separate probe
workspace. Filesystem reads/hashes run in joined workers; repeated cancellation
cannot abandon a worker or launch a process afterward.

Before each native creation, the plan's metadata guard and explicit
`expected_reaper` pin are passed to `ProcessOwner`. A journaled managed owner
must compose this guard with its current job/revision/recipe/attempt authority
check. This policy supplies no management credential or replacement authority.
Engine and adapter retain separate native owners; cancellation and result
publication still join both. Final package validation and the policy check run
after settlement. Optional fixed-graph accounting must bind the actual
`sandbox-exec` entrypoint and keeps its existing report semantics.

## Files, configuration and network

Each role may execute only its pinned native launcher and private interpreter;
the engine additionally may exec the pinned private llama server. Fork, spawn,
session and process-group changes are denied. Package/system runtime reads and
the role's private home/temp data are permitted. External source/global-runtime
reads, package writes, external connections and undeclared execution are denied
by the tested policy.

Model, endpoint and authentication configuration still arrive through the
host-created private descriptor. The existing engine additionally reads its
host-created mode-0600 key file; only that exact literal is admitted and its
identity is rechecked at native creation. Secret contents never appear in the
policy, report or journal. No pinned service/bootstrap bytes change.

Role policies use the exact-platform `tcp4 localhost:port` selectors: the engine
has its host-chosen inbound port; the adapter has its own inbound port and one
outbound engine port. Wrong-port TCP, UDP, IPv6 and Unix-socket negative controls
are separate from the authenticated real-client acceptance requirement.

**Address-binding limitation:** the tested macOS policy allows `INADDR_ANY`
binding at the selected port. A repeated synthetic wildcard-listener control
accepted a connection through the host's own nonloopback IPv4 address. Initial
nondelivery did not prove denial. This policy therefore makes no general
wildcard-listener isolation claim. The immutable production bootstrap explicitly
binds `127.0.0.1`, and its private descriptor parser rejects every other host.
The pinned-loopback native control denies nonloopback delivery. That exact
bootstrap/configuration binding is required for this trusted A4 profile.

## Evidence scope and remaining work

Small actual native fixtures cover filesystem/network restrictions, the exact
permitted endpoint, private writes and native ownership. Binding tests cover
changed package bytes, unlisted files, directory replacement, key replacement,
unbound toolchain metadata and repeated worker cancellation. Actual scheduled
native-task mutation controls require zero subprocess creations. These tests
contain no model evaluation and cannot satisfy A4 inference acceptance.

Fresh current-family CPU inference, full MCP schemas, token/queue checks,
managed preparation/attachment, installed relocation and independent review
must run after the coordinator freezes the combined backend and mints its next
immutable text-generation recipe. Old published recipe identities remain
untouched. A retained older package can supply a separately labeled policy
experiment, but cannot establish current descriptor/component eligibility.

Custom Seatbelt remains an exact-platform mechanism with a deprecated API and
maintenance limitations. Resource reports retain foreground RSS/maxima scope;
hard aggregate physical-memory enforcement and lifetime descendant accounting
are not claimed. No release gate or default capability is enabled here.

## Independent checkpoint review

Root independently matched all 12 source/control hashes in the frozen
`92f0de90` packet and passed 111 policy, verifier, family and process-graph checks
with no skips or failures. Actual small native controls include outside effects,
permitted endpoints, the pinned-loopback case and the separately recorded
wildcard limitation. The source checkpoint is integrated as `55781f0`.

The combined common/A4 review receipt has SHA-256
`9921872b7eb67b6ef9ecb03339b4fd6b46ac1569451520153647003e02f79b9e`.
This clearance covers the source and small native controls. Current-model
inference and combined installed acceptance remain open.
