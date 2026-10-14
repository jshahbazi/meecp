# Automated onboarding acceptance

Date: 2026-10-14. Scope: the development Apple silicon Mac running macOS 26,
standalone MeeCP, retained ColGrep-Agent-2B/Stark assets, and isolated host states.
This records implementation and scoped platform acceptance. It does not
establish unfamiliar-project success rates, general model quality, performance,
or the commercial two-Mac benchmark.

The [independent review and corrective checks](AUTOMATED_APP_ONBOARDING_REVIEW.md)
record defects found after the initial acceptance and their regression coverage.

## Checked behavior

| Contract | Acceptance |
| --- | --- |
| Retained resources | Captured the actual converted model and generation worker with licenses: 5,101,146,906 bytes across two resources. A fresh host retained the exact inventory and materialized independent attempt files. Stark generated from the materialized paths while the native worker had no read grant to the original inputs. |
| Source and catalog identity | Barrier tests rejected stale/canceled bindings, coalesced duplicate captures, serialized source publication against setup start, recovered committed revision/catalog rebinding, and prevented a stale display cache from adopting a newer catalog stamp. |
| Cancellation and shutdown | Resource and checkpoint copy tests kept reservations until copy threads settled; held directory descriptors prevented source/target redirection. Host interruption remained distinct from explicit cancellation. |
| Fresh setup sessions | Real host-state tests resumed preserved files/resources and opaque credential references into a new attempt, excluded session state, and returned a package through independent native MCP verification. Duplicate resumes joined one active attempt. |
| Metal compiler | Actual Apple-signed installed tools were selected and a fixed shader compiled in confinement. Exact Metal asset roots were admitted; the broad asset mount parent and generic GPU access remained denied. |
| Metal runtime | The shipped helper created a device, residency set and successful compute pipeline under the managed native policy. A separate test forced compiler availability false while native compute still passed. |
| Native dependencies | Real Stark at commit `9bc3ddb8e050c4d887aed13197a27bad2a4a185c` loaded ColGrep-Agent-2B and generated through managed native commands. Real ColGrep indexed a two-function repository and semantic search returned its fixture file. Every command settled owned processes and released its reservation. |
| Native admission | Tests verified exclusive GPU/memory waiting, cancellation before admission, listener confinement, retained charges on uncertain cleanup, and reverse disk admission while resource work was active. |
| Installation retry | A real CPU MCP package was retried through fresh import-bound plans and the native installer. Concurrent retries/plans/applies produced one operation and slot, preserved bytes/nondefault configuration/startup choices, made zero Pi/model calls, served an authenticated SDK request, and released processes/endpoints on shutdown. |
| Native UI | Built-app setup/resource self-tests passed. The installed UI showed Retry Installation for the preserved slot-conflict attempt and resource import in a new Pi setup. |
| App delivery | The standalone build and installed app passed deep strict signature checks and helper receipt validation. Owner API health returned 200 and installed source hashes matched the implementation. |

## Installed development build

The installed build receipt identifies source commit
`efefd6a42030abe91fad831d27f938f53051638a`. The ad hoc signed standalone app
passed deep strict code-seal verification after installation and restart.
Installed production source and the native helper/source hashes matched the
build. The owner API returned HTTP 200.

Acceptance then ran against the installed backend, reaper, Python runtime and
Metal helper. Compiler readiness, native compute, actual Stark generation,
ColGrep semantic indexing/search and owned cleanup all passed. Temporary native
reservations were released. The existing Colgrep slot was restored to Ready
after the update.

## Regression runs

These are scoped runs, with overlap; counts are not added together.

- 233 existing import/package/installation-recovery/capture checks passed.
- The final setup/general setup/onboarding recovery integration run passed 83 checks.
- The resource/checkpoint/retry/fetch run passed 67 checks before the added archive-tree case; the updated resource/store manager run passed all 25 checks.
- The earlier combined Pi/native/toolchain/question/resource campaign passed 230 checks. Its two deliberately deferred GPU compute cases subsequently ran and passed on the Mac.
- The real native installation retry integration check passed.
- All 33 toolchain/onboarding recovery checks passed after the cold Xcode continuation fix, including actual confined shader compilation. The controlled cases covered incomplete first launch, newly installed Xcode, automatic component acquisition, identity changes and explicit retry after acquisition failure.
- Nine real Pi SDK worker checks passed, including tool dispatch, bounded package repair, context compaction, cancellation and error redaction.
- New production modules passed scoped Ruff checks, Swift release compilation and native setup/resource UI self-tests.

The follow-up review passed 321 integrated backend checks before its final
compiler-probe cleanup fix, then all 95 affected toolchain/native/recovery checks
after that fix. Redirect/port checks passed 61 tests and the nine real Pi SDK
worker checks passed again. These runs overlap the earlier campaign.

## Provisioning and quality boundaries

The fixed Apple component command, missing tools, signature denial, changed
toolchain identity, acquisition failure and OS continuation branches are checked
with controlled runners. This Mac already had the Metal component; acceptance
did not remove it or perform a redundant multi-gigabyte component download.
MeeCP cannot accept Apple's login/license decisions. It presents the native
action, detects when Xcode becomes ready, acquires a missing Metal component,
and resumes the original preparation tool. A failed acquisition waits for a
readiness change or explicit Continue rather than repeatedly downloading.

Native generation uses the pinned actual model and a short generation fixture.
Semantic search uses the actual executable/model/runtime and a small repository.
These checks establish runtime readiness and confinement for those artifacts;
they do not qualify arbitrary repositories, generation distributions, latency,
or the model's full iterative localization quality.

Only aggregate outcomes and public artifact/provenance identities are retained
here. Prompts, responses, token vectors, raw subprocess output and credentials
are excluded. Generated runtime/model copies remain ignored.
