# Native model capture

**Add App** includes **Choose a local model**. Select ONNX or GGUF, choose the
weights file, and choose the accompanying license text. Capture stays disabled
until both files pass the bounded local file checks. The review displays their
names, total size, a five-minute capture deadline, a 64 MiB reservation and
unverified publisher trust. A local file selection and its hash identify bytes;
they do not establish publisher authenticity.

Capture copies data into the owner's private artifact store. It does not execute
the selected model or source. The host determines whether a registered recipe
accepts the resulting collection. The frozen ONNX checkpoint accepts its exact
reviewed YOLO11n export and AGPL license. Choosing GGUF exposes its honest
compatibility result from the installed catalog; the selector alone does not
provide an LLM recipe or enable execution.

Enter the app name, intended tool behavior and startup choices in the source
card. Capture can create a new draft. When an eligible saved folder, document or
captured-model draft is selected, **Add model to the saved draft** attaches the
new collection to that exact saved revision. Supplemental capture preserves an
ordinary folder's original namespace and source identity.

The view calls the existing authenticated owner socket:

| Action | Closed route and binding |
| --- | --- |
| Capture | `POST /v1/artifact-captures` with `AcquisitionRequestV1` local-files entries, `CapturePolicyV1`, a create-import or exact-revision attach-to-import target, and one stable UUID retry key |
| Poll | `GET /v1/artifact-captures/{capture_id}` for the returned job identity |
| Cancel | `POST /v1/artifact-captures/{capture_id}/cancel` with an empty object |
| Recover | `POST /v1/artifact-captures/{capture_id}/recover` with an empty object |
| Adopt | `GET /v1/imports/{import_id}` matching the settled capture's result identity and revision; the parent adopts it and loads generated preparation questions |

The model entry has the selected format, model-weights role and an explicit
license reference. The separate license entry has text format and license role.
The disk ceiling is the selected byte total plus 1 MiB. Individual files are
limited to 16 GiB; the license file is limited to 16 MiB. Native directory checks
use `O_NOFOLLOW`; the host independently reopens, copies, hashes and verifies the
selected entries through its production capture implementation.

An unknown start response retains the exact request and retry key. A known stale
target rejection occurred before host job creation, so the user can refresh the
saved draft and choose the files again. A returned failed job with retained
ownership or resources remains associated with its capture ID and exposes
recovery. Navigation and editing cannot replace an active or unsettled capture.
The saved setup records its known capture ID for resumption. Sanitized messages
use fixed error codes and never include exception text, model bytes or license
contents.

Successful settlement requires ownership settled, no retained reservation and a
valid collection digest. The parent also checks the admitted draft's identity,
exact revision and source association before displaying it. Capture IDs remain
separate from preparation job IDs. Generated form review, managed execution and
independent package verification retain their existing capability checks.

## Verification

`--artifact-capture-ui-self-test` exercises the actual buttons and handlers:
missing/unsafe files, stable unknown-response retry, cancellation, retained
ownership recovery, restored status, changed draft rejection, stale admission,
resource-blocked ownership and exact parent draft adoption. The installed ONNX
verifier invokes the same handlers through the real socket. A separate visible
native run exercised both open panels and the normal Capture button; its evidence
is recorded in [installed ONNX acceptance](NATIVE_INSTALLED_ONNX_ACCEPTANCE.md).

Public automatic preparation remains disabled. Intake, capture and displaying
generated questions do not enable recipe execution.
