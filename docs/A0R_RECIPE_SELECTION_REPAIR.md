# A0R saved recipe selection repair and producer 1.0.7

This checkpoint repairs a reproduced recipe-selection defect. When an app had a saved descriptor and that exact recipe was disabled, deprecated, revoked or removed, reopening the ordinary form selected another compatible recipe automatically. The saved descriptor still named the original recipe, but the displayed form silently changed implementation. The [original recipe contract](AUTOMATIC_PREPARATION.md#easy-authoring-and-addition) requires an explicit replacement.

The form now restores the saved exact recipe only while it remains eligible. If it is unavailable, the form returns no selection and an existing selection/compatibility blocker. Preferred and sole-recipe defaults remain available for fresh candidate choices. An explicit eligible replacement works; reopening or resolving that form does not overwrite the saved descriptor before owner review.

## Reproduction and verification

The independent reproduction used the frozen `7344a55` packaged interpreter/backend and real owner Unix socket. For each of the four lifecycle actions it saved a descriptor, removed original source and authoring bundle, closed/restarted the manager, then compared omitted, explicit-original and explicit-replacement requests. All four omitted requests silently substituted the builtin callable recipe. Twenty-four requests and eight manager lifecycles settled with zero jobs, source execution or provider calls.

The same four owner-socket controls passed against the repaired source backend and frozen private interpreter: zero substitutions, preserved descriptor, unavailable explicit-original rejection and successful explicit replacement. This source result does not claim that the old frozen host contains the repair.

Seventeen new form regressions cover all four actions, sole/preferred alternatives, actual store reopen, preserved saved descriptors, explicit replacement, no alternatives and re-enablement. Marker-bearing source never executes during those controls.

| Check | Result |
| --- | --- |
| Complete form test file | 33 passed, zero skipped |
| Combined recipe/CLI/catalog/evidence/forms/TextGen source checks | 189 passed, zero skipped |
| Independent form/API/TextGen upgrade/host/component checks | 96 passed, zero skipped |
| Actual Gitleaks scanner-fixture suite, including its explicit integration opt-in | 95 passed, zero skipped |

The new bundle copies the same reviewed public model provenance. The scanner retains its one exact public digest, exact path binding, generic-api-key rule and AND condition; only the exact `text-generation-v1-0-7/model-provenance.json` path is added. Other values, paths and rules still detect findings.

## Immutable producer update

The text-generation producer binds all reviewed backend modules. Changing the common form implementation requires a new component/recipe version. Version 1.0.7 is freshly generated; every earlier bundle retains its original bytes and lock.

| Identity | Value |
| --- | --- |
| Recipe | `mcpme.text-generation@1.0.7` |
| Recipe digest | `5fb5ffaea35b0ff1cf1117966b7d38ff7492fac64e549963118d2e89bd31561a` |
| Engine component digest | `cfa3ae34ba8fb8b9ce5a54214b7cd62db274043d86d2c5b8907affd341659c27` |
| Task component digest | `3e3ae10c0957f2d944d945bd6a6c32376f052fcd7a70726e93b9358d9115ffa4` |

The independent audit recomputed 95 producer source hashes and ten asset hashes, checked the seven-file closed bundle and component/dependency/config bindings, and verified that only the current TextGen catalog entry changes. All 15 prior builtin bundle trees equal their original Git trees, including seven TextGen bundles/49 files. Frozen agent component closures of eight and 17 files retain their original bytes and digests.

Upgrade controls now independently cover both retained 1.0.5 and 1.0.6 metadata against the current version: disable/revoke/remove/deprecate remain opted out across restart, enabled historical records retain their exact snapshot, and collection tombstones prevent automatic re-enablement. These tests execute no historical worker or model.

## Private evidence

All receipts are new immutable mode-0600 files under `/private/tmp`. Raw logs stay in memory.

| Receipt | SHA-256 |
| --- | --- |
| `mcpme-a0r-saved-selection-1364-v2.json` | `eb6444b461f49536cd1d2c19c1f0e263a039a07734782b2fc456d76d50c2e2de` |
| `mcpme-a0r-saved-selection-source-repair-v1.json` | `86f3832a6a92ea4b5d499f833bcc4575d9600b87f490a254c8a799f3c79108fe` |
| `mcpme-a0r-producer-107-independent-v1.json` | `fcaaa00b9a9dacd1eac90c85f74b2f5af3488351b9234bdd35bbe7e82dd53b43` |

## Remaining acceptance

A0R remains in progress. The next coherent host/native pair must repeat packaged catalog and repaired-selection controls, native Add Recipe/management, the independently authored recipe's real build/SDK/attachment/restart path, and lifecycle/race/retention cases. The native lifecycle harness is being updated to admit the exact currently registered resolved-Python component rather than hardcoding an obsolete version.

The accepted declarative scope uses the common development acceptance harness under exact owner preparation authority and mandatory independent verification. There is no author-facing `recipes test` command; the original command list was proposed, and this checkpoint does not advertise it. Additional executable preparation workers retain A0R.06's separate ownership/capability gate.

No new inference, live provider, signed installation or public capability enablement follows from this checkpoint. See [the phase 3 status table](PHASE3_STATUS.md).
