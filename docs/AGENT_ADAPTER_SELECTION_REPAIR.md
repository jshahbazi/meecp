# Generated adapter selection checkpoint

The locked generated-adapter checkpoint `33fe6e0` passed its actual private
build, SDK verification, attachment and relocation pipeline. Independent review
then reproduced a selection-publication defect: changing the separately derived
wrapper after full validation could still commit a selection through both
agent adoption and direct descriptor selection. Both controls produced revision
2 with a selection; neither attached or executed an app.

The root-owned repair `fe7d6d2` adds the registered family's synchronous
current-input check to descriptor admission and selection commit. The adapter
family compares the original and derived file metadata against the same durable
baseline captured around its full validation. Admission does not read source
bytes, start asynchronous work or capture a replacement baseline. The same
helper is used for managed-job authority. Optional family hooks must return
literal `True`; asynchronous or nonliteral results are rejected.

Independent different-author review cleared this source checkpoint with 172
passing checks and five explicitly opt-in built-host tests skipped. The controls
include both original mutation reproductions, successful idempotent selection,
cleanup handles, restored bytes and modification timestamps, replaced derived
collections, cancellation, and rejected asynchronous authority hooks. Both
mutation paths now retain revision 1 with no selection or attachment. All 16
published locked-adapter files and six legacy files still match their original
hashes.

Root also independently ran the actual pipeline on the frozen source
composition: private build, confined official SDK verification, matching-revision
attachment, original source and preparation-state removal, and a separate
confined relocated-package verification. It passed with five private build
commands, one tool, one fixture, four simulated provider calls and zero live
provider calls. All owners joined; all 4,782 private host substrate files and
103 source-composition files were checked before and after.

The pipeline uses the unchanged `68a7533` private runtime substrate plus the
explicitly hashed `fe7d6d2` source composition. It does not establish that the
old host contains the new implementation, or close final signed/installed native
acceptance. Unlocked-directory preparation, a selected live provider/model,
combined artifact acceptance and whole-phase adversarial review remain open.

| Evidence | SHA-256 |
| --- | --- |
| Independent checkpoint review | `db744703258ea91dd891da3dc2f02412bfe16a17d7c36deaf2dc485c6517ac4d` |
| Root source composition | `a74bf8176566aea85ad5e11b0daa617c3bc14c3813a64aeed00f6c61bb60175c` |
| Root actual pipeline | `7995312f09afe4f8a2b5b5302cc93b3370d389105d16547bf361dd2c4d4909e0` |

Content-free receipts are retained separately in private review storage. They
contain no source, prompts, provider responses or credentials. The locked
component remains `agent-python-adapter-v1@1.0.0`, digest
`28c1d862b947f294dcd0a7f038c32f3d9cf4e31ea20f81d91ab800b13d0b8274`;
its recipe remains `mcpme.agent-python-adapter@1.0.0`, digest
`ed6413e202c9f9e8de682c324b2e46695c9c59e1bb44d15db6a5f423dce3cc04`.
