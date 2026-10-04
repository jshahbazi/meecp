# Native agent provider setup

This native checkpoint starts from `62093ac2f28b4993bffa61dd8477cb98a04c508d`. It adds provider configuration to the existing Agent Assistance controls through the normal authenticated owner transport. The host registry implementation is a separate checkpoint; this patch does not change backend adapters, preparation components, recipes or published bundles.

The provider editor offers Refresh, Add, Edit and Remove. Its initial adapter is OpenAI Responses at `https://api.openai.com/v1/responses`. Users enter a profile name, one to sixteen explicit model IDs and an API key in a secure field. Editing with a blank key retains the saved key; entering a replacement rotates it. Model compatibility remains unverified. The host stores credentials in its existing private secret store, with private directories and records; this is not Keychain storage.

The API-key and explicit-model terminology follows the [official OpenAI documentation](https://developers.openai.com/api/docs). This checkpoint makes no model choice or account-access claim.

The field and its active AppKit field editor are cleared before submission, on cancel, on invalidation, on window close and when quitting. No key is copied into setup persistence, metadata, status text or test output. The submitted owner request necessarily contains the entered key. Configuration performs no connection test, source transfer or provider request and changes no execution gate.

The UI uses the closed owner API agreed with the host registry lane:

| Action | Route | Binding |
| --- | --- | --- |
| List | `GET /v1/agent-providers` | Metadata and the trusted adapter description; no credential values or references |
| Create | `POST /v1/agent-providers` | OpenAI adapter, name, explicit model IDs, empty configuration and entered credentials |
| Edit or rotate | `POST /v1/agent-providers/{provider_id}` | Exact expected provider generation; empty credentials retain the saved key |
| Remove | `POST /v1/agent-providers/{provider_id}/remove` | Exact expected provider generation |

Responses are bounded and checked against the advertised closed metadata shape, trusted destination and disabled execution flags. The native request dispatcher requires `api_version: 1`; the matching host wire repair is `cd7c0405fd922367418cac19c63a272c8f12c5b5`. Rotation must change the credential generation; a retained key or removal must preserve it. A lost mutation acknowledgment clears the editor and requires a list refresh. The UI never automatically repeats a create request. Invalidated replies cannot restore stale profiles or editor state. After the parent replaces a lost or settled host connection, a separate request-token fence permits fresh discovery without accepting old callbacks. A same-host invalidation keeps the request busy until its callback settles.

An open editor or pending configuration request participates in the existing parent navigation guard. Provider changes invalidate held transfer reviews. Loading ordinary agent options requires a saved draft; configuring providers does not. The model picker requires an explicit choice before source review. When registered provider options advertise a generation, a reviewed plan must bind that same generation. The existing exact-file and identity-only metadata review, known-job cancellation, uncertain start retry and ordinary preparation handoff remain in place.

The optional owner `recipe_choices` contract contains at most 128 exact displayed recipe references, bounded human labels, `compatible` or `unresolved` metadata eligibility, and at most one compatible recommendation. It covers the same references as `recipes`. The chooser selects only that host-issued recommendation and does not infer eligibility from file contents. It labels the supported paths “Python app with an existing dependency lock” and “Python app needing dependency resolution.” Explicit choices survive refresh while their exact reference remains available; a disappearing or changed reference requires a new choice. Unresolved choices cannot enable transfer review. Without choice metadata, the initial selection is a placeholder. `recipes_truncated: true` suppresses automatic selection and visibly asks users to narrow the enabled recipe list. Static metadata eligibility proves no runtime safety or provider compatibility.

## Verification

`uv run --locked pytest -q --tb=no tests/test_agent_provider_native.py` compiles and exercises the actual AppKit handlers through `scripts/verify_agent_preparation_view.py`, using synthetic owner replies and a private HOME. Its controls cover create, retained-key edits, rotation, removal, local validation, lost acknowledgments, invalidated list and mutation replies, reconnect fencing, host conflicts, secret-bearing or untrusted metadata rejection, secure-field clearing, explicit model selection, stale provider generation, navigation ownership, and zero configuration-triggered analysis requests. Cold unlocked options place an unrelated recipe first and prove the exact recommended adapter is selected. Additional controls cover preserved and disappearing explicit references, ambiguity, truncated lists, undisplayed recommendations and duplicate recommendations. The existing agent controls also exercise cancellation races, uncertain acknowledgment ownership and candidate handoff.

The compiled native `--agent-preparation-ui-self-test` uses the same actual handlers. Swift transport and installation tests and the existing native UI self-tests remain required. This is source and handler evidence. Packaged owner-socket configuration, visual acceptance, live credentials/model compatibility, source transfer and final installed A5 acceptance remain open. Synthetic keys and replies prove no live provider behavior.

The final focused packet passed 61 Python checks with 64 existing opt-in scanner controls skipped in 8.47 seconds. Swift passed 18 checks with three installed-lifecycle opt-in skips. The release build succeeded, and all six native contract/UI self-tests passed with their owned processes joined and isolated HOME/state. The actual secret scanner and whitespace checks passed. No provider dispatch, model run or production credential/state read occurred.
