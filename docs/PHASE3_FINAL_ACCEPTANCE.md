# Phase 3 final acceptance evidence

This index freezes the signed v10/TextGen 1.0.13 evidence. The
[current source CI repair](PHASE3_RELEASE_POLICY.md#current-source-after-the-ci-repair)
uses TextGen 1.0.14; signed live acceptance has not been rerun for that source.

Snapshot: 2026-10-04. **Supported Phase 3 source/runtime acceptance is complete;
verified working-branch publication is complete.** All selected runtime workflows, final exact
preservation and the supported-scope whole-phase review pass at
24bae0f8fd0aa91ebaaba5ee2b0d7d4020cc9498/signed v10.
The ledger has 101 in-scope rows Complete, including verified D09 publication,
and three Separate gate rows. The documentation recording commit
is distinct from this runtime anchor and does not rebuild/rewrite its receipts.

The [durable final review](PHASE3_FINAL_REVIEW.md) records clearance and
independence limits. Its original private Markdown/JSON hashes below identify
the immutable review packet. The committed Markdown copy may correct punctuation
and add a delivery-recording note; its bytes must not be described as identical
to the original private Markdown.

This index records bounded owner-only metadata receipts. It contains no
credentials, credential-file paths or digests, prompts, responses, options,
source payloads, exception dumps or raw runtime logs. Local receipt links require
access to this owner's workspace; their full SHA-256 values freeze the records.

## Source and artifact binding

| Item | Exact binding |
| --- | --- |
| Current signed/runtime source | 24bae0f8fd0aa91ebaaba5ee2b0d7d4020cc9498; 811 tracked Git blobs |
| Full producer 13 Python source | 3ddaaddc5a69293bcfaad97656be0335a5131339; 811 tracked Git blobs |
| Scope delta | Six approved diagnostic/scanner paths; 805 other Git blobs identical, including backend, release policy, templates and assets |
| App inventory | 7,722 exact path/size/mode/SHA-256 records |
| Host inventory | 7,578 exact manifest members excluding build.json |
| App build.json SHA-256 | 0c9ffebd3866fbac4fb26e9a8314048fae6d01e1876b29570393118bcf1d58a0 |
| Host build.json SHA-256 | 7b3849bef0f176dd55b67d87afd67e75aca5cf68cc38c0a27d61ed75b1b229a7 |
| Native binary SHA-256 | 48ebdf6a78006d499ee7e1d2ef4fd406ca2825f6ae00fd57a4e5fa7f3255cb81 |
| Signing | Developer ID leaf EFE6CD2666B9106E68724C38ADD1C405267A2F94; Team S5BT2PRN5B; identifier com.jshahbazi.mcpme; strict seal, hardened runtime and timestamp pass |
| Actual direct entitlement reads | Preparation Python has only disable-library-validation=true; native menu binary, host Python and handoff have empty entitlements |
| Current release policy SHA-256 | 8a9a8fd69217352b49e0e338d4d652df8cebcac86609edae1190a88cd2d9a592 |
| TextGen engine/task/recipe | Version 1.0.13 |
| Engine digest | 899aa082f51bbe079968ce4540fa801215adcb9f8a2fc622844e4e63ee133c9e |
| Task digest | 936c30b0ede738344713d37d62cb71dbfea59b7375dfa24ea023d9693bf9274a |
| Recipe digest | c5fff2ecc6d6018f61fe3c65f929be389381c572149d9dbebffc11c716123465 |
| Selected preparation provider | openai-responses-v1; f651b36ab4b3d4fe6479457e5a31f129b1339c41536e02bfb53cbbf4025cebc1 |
| Sole admitted model | gpt-6.1-sol |
| Unlocked Python component | agent-unlocked-python-adapter-v1@1.0.0; 7d37411ca8119c2433fa44a59080d89a48c3618f7f909bbcb2977646b445882f |

Original published bundles, the pinned signed TextGen runtime/model and command
helper assets retain their recorded bytes. Catalog producer fingerprints bind
their stated top-level source/asset closure. Public admission separately binds
the complete verified host manifest, resolution producer files and held
artifact fences, including nested native TLS. A catalog digest alone is not a
claim that every nested host file is in its narrow fingerprint.

A later documentation/receipt-only commit records this runtime commit. It does
not retroactively change the source built into v10, its native binary or seal.

## Terminal receipt index

| Checkpoint | Terminal result and scope | Owner-only receipt | SHA-256 |
| --- | --- | --- | --- |
| Python v6, full producer 13 | 4,223 passed; one casefold skip; all 27 opt-ins passed | [mcpme-phase3-combined-full-regression-v6.json](/private/tmp/mcpme-phase3-combined-full-regression-v6.json) | 3624439c0f5e40f9d06cbe0c0642c21705edc0c67b7311338e47e12c40b3e7bb |
| Swift v6, current source | 21 passed; zero failures/skips | [mcpme-phase3-combined-swift-regression-v6.json](/private/tmp/mcpme-phase3-combined-swift-regression-v6.json) | 96761f2436f1d7ebd62431bb1b94bb08431c8289b96d672902b082b3c78f930d |
| Signed v10 build | Accepted; source 24bae0f8; 7,722 app files/7578 host members | [mcpme-phase3-combined-app-build-v10b.json](/private/tmp/mcpme-phase3-combined-app-build-v10b.json) | 2c013c5e6056a2e30552f438ba57f08d55250a4007e1c309ced4bef35174dab7 |
| V10 root independent attestation | Complete app/host/source/signatures and actual direct entitlements | [mcpme-v10-root-independent-attestation-v1.json](/private/tmp/mcpme-v10-root-independent-attestation-v1.json) | d956ad2dcbca2765f12d37939d1c39d830d6d94367ccc8186dae1d94a6333590 |
| V10 peer corrected transfer/entitlements | Six approved delta/805 unchanged Git blobs; direct entitlements | [mcpme-v10-independent-transfer-attestation-corrected.json](/private/tmp/mcpme-v10-independent-transfer-attestation-corrected.json) | fc722e16210a729d7690a81ee9f6760b74746db9d44d9ef3c05a87a1612e5809 |
| V9 ordinary Python/MCP/ONNX | Five actual signed native/installed cases pass | [mcpme-phase3-final-python-onnx-review-v4.json](/private/tmp/mcpme-phase3-final-python-onnx-review-v4.json) | 0131de21be7cddb6424f4c29027fe9ea85c8a82c82facb7831a2a913212ba7a5 |
| V9 first service connector | Owned numeric-loopback protocol fixture passes; zero real calls | [mcpme-phase3-final-service-review-v4.json](/private/tmp/mcpme-phase3-final-service-review-v4.json) | 3de5ab230ae653077994f6fe47a863cbb6074b006af9ef5cd9938fa7d343d4e2 |
| V10 management | Completed/revoked cases; six SDK sessions; owned joins | [mcpme-phase3-final-management-review-v6.json](/private/tmp/mcpme-phase3-final-management-review-v6.json) | f2c8df31430f1fed4c200ab411ca9d137bcfe5ca3efe118d3ba3b3904e8e4ee2 |
| V10 management flow | Immutable inner workflow receipt | [mcpme-phase3-final-management-flow-v6.json](/private/tmp/mcpme-phase3-final-management-flow-v6.json) | fd81894dad84b331bb9cd32d636ab99ce003303496b6cc5c0b9fdc936843ef9c |
| Failed v9 live batch | agent_handoff; unexplained; remote-call count unknown | [mcpme-phase3-final-live-agent-review-v2.json](/private/tmp/mcpme-phase3-final-live-agent-review-v2.json) | 62cb7bb4cc9657d6b1e08be6b65b6ed5852b06b611251b75b1ca21572028d672 |
| Failed v9 live flow | Preserved fixed-stage failure and cleanup scope | [mcpme-phase3-final-live-agent-flow-v2.json](/private/tmp/mcpme-phase3-final-live-agent-flow-v2.json) | f3c2291420bfe87e2932f4b0084dcc059c36827150b63b9c4b2f17bd36af18fa |
| Historical selected real-provider candidate | Four actual calls; zero retries; full native/installed flow | [mcpme-phase3-selected-live-agent-review-v1.json](/private/tmp/mcpme-phase3-selected-live-agent-review-v1.json) | 25630698622309837203a6a3e6f485ccedcdeb4b9d9c1dc6f0736caa18d79917 |
| Historical selected candidate flow | Exact signed 2fa receipt; public_release_accepted=false | [mcpme-phase3-selected-live-agent-flow-v1.json](/private/tmp/mcpme-phase3-selected-live-agent-flow-v1.json) | 862de66f0fd74814cfd47ac59b8f4fe2ff2ad026c01eaf6b29ea05ec7b90a7ae |
| V10 selected live agent | Accepted in 107.209 seconds; four real calls, zero retries; current native handoff/installed/source-free flow | [mcpme-phase3-final-live-agent-review-v3.json](/private/tmp/mcpme-phase3-final-live-agent-review-v3.json) | 555d7902c0c00253d9dbe11041be03d674275987204d09719ce0b000c9a32a81 |
| V10 selected live flow | Exact source 24bae0f8/signed app; no injected factory; public_release_accepted=false | [mcpme-phase3-final-live-agent-flow-v3.json](/private/tmp/mcpme-phase3-final-live-agent-flow-v3.json) | 4090c1ab5c5940ad3c078fa8567b290ad759e97db7e196cc8de051bb5aebf5df |
| V10 native CLI/cross-build upgrade | Accepted in 155.352 seconds; native public command/cross-build upgrade; input/older upgrade app/source preserved and owned joins | [mcpme-phase3-final-command-review-v4.json](/private/tmp/mcpme-phase3-final-command-review-v4.json) | 6c387c4fcdf497ac43084e7a08f0438fab955fbf2a0caee83ce3ee7bdad6b7fe |
| V10 command flow | Immutable current source/app CLI and cross-build workflow | [mcpme-phase3-final-command-flow-v4.json](/private/tmp/mcpme-phase3-final-command-flow-v4.json) | 092fa97922eb4a571123a5ee8972980a967cfabe5734360b8ebc75289cb537ee |
| V10 local TextGen | Accepted in 449.402 seconds; create/supplement; actual pinned Qwen CPU; original app/source/model preservation and owned joins | [mcpme-phase3-final-textgen-review-v4.json](/private/tmp/mcpme-phase3-final-textgen-review-v4.json) | e3886f700cbc7de5f8b06b1448c98bd8c901d2da90849cff3b9960d93566632d |
| V10 TextGen flow | Actual current producer 13 native/probe/attachment/installed/source-free sealed-relocation cases | [mcpme-phase3-final-textgen-flow-v4.json](/private/tmp/mcpme-phase3-final-textgen-flow-v4.json) | c8c9898639dc9afab1ca90634e9e9f8b4bf7a0064bdffc29e2ca8aba8a42b6bf |
| V10 recipe lifecycle | Accepted in 250.090 seconds; all ten cases; source clean/preserved; owned joins; zero external calls/deadline/forced stop | [mcpme-phase3-final-recipe-lifecycle-review-v4.json](/private/tmp/mcpme-phase3-final-recipe-lifecycle-review-v4.json) | 4f8d42741bd6d96ea0116e094cc5779f174a7cb4398376507c8455c0c64e6f28 |
| V10 lifecycle flow | Ten current catalog/default/lifecycle cases; public review and explicitly private barriers | [mcpme-phase3-final-recipe-lifecycle-flow-v4.json](/private/tmp/mcpme-phase3-final-recipe-lifecycle-flow-v4.json) | 59ebecd13a56f4cf9024baa616d9d2cdae57b9165b5a6c66dc348c7418492ba1 |
| Final preservation after all v10 flows | Accepted; all 7,722 app records exact/811 source clean/five pinned assets unchanged/strict signature/all known owned children joined; zero provider calls | [mcpme-signed10-final-preservation-v1.json](/private/tmp/mcpme-signed10-final-preservation-v1.json) | f24145dcf9720f7cb4d63cacdf753174ad7b9b79f356664929f043bb011c7b0a |
| Retained investigative transfer negative | Full transfer rejected because newly compiled handoff body differs; valid negative evidence result, not a product finding | [mcpme-signed9-to-10-host-code-transfer-v1.json](/private/tmp/mcpme-signed9-to-10-host-code-transfer-v1.json) | ac51f69b07ea497eb880f8fc2ee20b653c0a67fc460d0d10b5f787916100fe8f |
| Original final review Markdown | Supported source/runtime scope clear; no blocking finding; original private bytes before delivery punctuation/note | [mcpme-phase3-final-clearance-24bae-1791162132370021000.md](/private/tmp/mcpme-phase3-final-clearance-24bae-1791162132370021000.md) | e3410046199ab34c60a2436c3f41cd97aa4e75951642de262cd271a031285a37 |
| Final review metadata packet | Accepted scope/independence/evidence limits; publication was pending at review time | [mcpme-phase3-final-clearance-24bae-1791162132370021000.json](/private/tmp/mcpme-phase3-final-clearance-24bae-1791162132370021000.json) | f6817428c1081f978ef337f3b05949a9334e475fb8fd07bb1f126dabcd4dc367 |
| First verified working-branch publication | Commit 77e2828e077403291c605544efac73aa6041b573; selected origin/codex/phase3-public-acceptance; remote SHA verified 2026-10-05 01:16:15 UTC | [mcpme-phase3-first-publication-1791162975548487000.json](/private/tmp/mcpme-phase3-first-publication-1791162975548487000.json) | 64ffc91a77d2ab9c403ffb1e163a05154160fbdb66ed9920edc266a7088766f2 |
| First source/delivery secret scan | Passed 3.392 seconds; 813 tracked files/four staged docs at frozen 24bae before first publication; not a future recording/CI result | [mcpme-phase3-final-secret-scan-metadata.json](/private/tmp/mcpme-phase3-final-secret-scan-metadata.json) | 8ffab87bf5b9f9ac4b052bc4c4e8595291c0163b6f0824969a2f16d58bf5b1bb |

## Additional terminal controls

Actual v9 native TLS acceptance is retained at
[the owner-only TLS receipt](/private/tmp/mcpme-native-tls-v9-bikz0vl7/review.json), SHA-256
5646f208bf7d8668de0183d78a114cb694650b2066b30149cb1947ebfb957b9a.
Eight actual native cases pass: 22 forwarded Security evaluations, five owned
CFErrorRefs returned and five matching releases, zero outstanding errors.
Positive/concurrent and chain/hostname/expiry denials pass; fetch-policy denial
performs no evaluation, there are zero AIA fetches and no Keychain trust changes.

The prior TLS spy failure is retained separately. Its reported zero evaluations
was an instrumentation count; real Security calls had occurred before the
incorrect ctypes pointer cast failed. The corrected spy provides the actual
counts above. It changes no product source.

Producer 13 public-policy/fixture controls have 89 port-free passes, zero
failures/skips/warnings. The historical closed12, selected12 and current13
policy images remain explicit; earlier bundle bytes are preserved rather than
restamped. Current scanner has 15 unit controls and eight actual new13-bundle controls
plus full history/source coverage. Current native diagnostic callback and
request-binding controls have independent peer clearance. Their focused
source proof does not establish a successful live workflow.

## Accepted evidence transfer and limits

Full Python v6 binds source 3ddaaddc. The six-path source delta to 24bae0f8 preserves
all backend/runtime, policy, template and asset Git blobs. Its Python test result
can support that unchanged source scope with the approved diff, new focused
diagnostic/scanner controls and current Swift 21 result. It cannot claim a full
re-run against 24bae0f8 or byte-identical v9/v10 native binaries, app manifests,
signatures or outer bundle contents.

V9 ordinary Python/MCP/ONNX, service and native TLS receipts bind their exact
signed artifact. Root confirms 805 unchanged Git blobs and 162 effective toolchain requirements,
with nonartifact metadata/licenses/private Python/uv/MCP unchanged. The final
review accepts their verified unchanged-code/input scope with exact current v10
artifact and fresh helper behavior evidence; compiled-body limits below remain. Source equality or a valid signature alone does not
establish installed runtime acceptance.

V10 management independently replays the current app/source, completes its
completed/revoked scenarios, exits six SDK sessions and joins known-owned work.
Current live/CLI/TextGen/lifecycle receipts independently close their selected workflows. The management wrapper has relocated_app_unchanged=null; management relocated-app preservation is unclaimed.

### Changed native/helper scope

Independent input checks establish unchanged host Python bytes, all 162 effective
requirements and nonartifact toolchain metadata. Among 33 changed host Mach-O
members, 32 loadable bodies match with CMS excluded. The newly compiled
host/bin/mcpme-handoff loadable body differs even when UUID is masked.
Therefore the signed v9 full 4,223-test Python/27 actual host-opt-in receipt is transferred
only for unchanged code scope with independently verified inputs. It does not
become a v10 full-suite/27-opt-in replay or prove complete host/bundle byte identity.

Fresh current v10 management/live/CLI/TextGen/lifecycle independently exercise
the current helper in their recorded runtime scopes.
A negative control depending on changed compiled bytes needs its own applicable
proof. The investigative failed transfer receipt is retained as a valid
negative evidence result; it is not a product finding.

## Live failure and call accounting

Historical 2fa verifies four real OpenAI Responses calls with zero retries and
a complete native-to-installed flow. The earlier v9 batch failed at agent_handoff
after reaching a succeeded proposal. Its underlying cause remains unexplained;
remote-call count is unknown. The failed workspace was cleaned after known
owned work joined, so that count cannot be reconstructed from a retained
provider payload or journal.

The v10 diagnostic-only repair retains fixed-stage conditions and sanitized
form-error metadata for the cached proposal and active context. It also observes
existing validated owner job metadata before cleanup. Reservation/accounted-call
counts are local observations; they are not proof of a remote provider's exact
billing or accepted-call count. Invalid/missing/mismatched observations remain
unknown. No prompt/response or credential content is retained.

The bounded v10 live attempt passes in 107.209 seconds with four proven real
calls, zero automatic retries and full native pending-proposal handoff through
ordinary resolution/preparation/SDK/attachment/install/source-free/restart and
confined relocation. All 811 source files, original/relocated signed app identities and all
known owned children are preserved/joined; there is no timeout or forced stop.
The immutable receipt retains public_release_accepted=false from its driver
gate. Later supported-scope clearance is recorded separately in the final review. The prior v9 cause remains unexplained; no behavioral fix is inferred. These
local functional/ownership receipts establish no remote cancellation or remote
work settlement.

Across the historical and current successful batches there are eight verified
real calls, plus the failed v9 batch whose count remains unknown. Eight is not
the exact total including that failed batch. No further provider calls are planned.

## Supported-scope clearance and verified publication

The [final review](PHASE3_FINAL_REVIEW.md) clears supported Phase 3 source/runtime
acceptance and final qualified evidence transfer. All five selected current v10
workflows and final preservation pass. No concrete blocking review finding
remains. Together with verified D09 publication, this closes 101 in-scope rows while
preserving all stated limits and three future capability gates.

D09 is Complete from the first exact remote verification: source/delivery
commit 77e2828e077403291c605544efac73aa6041b573 was pushed to
codex/phase3-public-acceptance on origin
https://github.com/jshahbazi/mcpme.git and verified 2026-10-05 01:16:15 UTC.
Publication is established by its explicit proof, not inferred from signing or
runtime acceptance. The first delivery scan passes 813 tracked files/four staged
documents at 24bae in 3.392 seconds.

This update records that completed publication in a later documentation commit.
It does not claim the future recording commit was built/tested, has already
been pushed or passed remote CI. Frozen runtime/app receipts remain bound to
24bae/signed v10. The final review keeps its historical review-time snapshot.

## Continuing capability boundaries

A0R.06, A6.02 and A6.04 remain future gates: executable preparation workers;
additional preparation providers including DeepSeek, GLM, Bedrock or local
services; and broader runtimes/model families, GPU/Metal, resident vision and
GUI automation.

Production native handlers are tested; physical clicks remain unverified.
Functional SDK/tensor/generation/generated-adapter checks do not measure task
or general model quality. Remote/unowned settlement is unknown. Deliberate
owner-loss faults retain state/charges for reconciliation. The documented
same-user/trusted-engine scope does not prove arbitrary hostile-worker or
wildcard-listener containment. Sampled foreground footprints and separate role
peaks do not establish hard aggregate or complete lifetime resource enforcement.

The sole casefold fixture skip remains explicit. Developer ID signing is
verified; notarization, public distribution, user installation and additional
provider/runtime compatibility remain unclaimed.

Related records: [goals](PHASE3_STATUS.md),
[release authority](PHASE3_RELEASE_POLICY.md),
[trust/resource scope](PHASE3_TRUST_AND_RESOURCE_SCOPE.md),
[live acceptance](LIVE_AGENT_ACCEPTANCE.md) and
[management acceptance](PHASE3_PUBLIC_MANAGEMENT_ACCEPTANCE.md).
