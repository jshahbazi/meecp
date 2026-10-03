# Pinned ONNX hotdog task notices

The host adapter is distributed under the full MIT text in LICENSE. The detector
is adapted from the repository's AGPL-3.0 hotdog example and retains that license
in LICENSE-MODEL. Its complete Python source is included in the package. No
selected Python app code is imported into the host.

The exact YOLO11n ONNX export is identified in model-provenance.json by SHA-256
`121b1f682e2df1aff1c16dacb7ce4bedc26df016a500b18743349c16724ad85d`
and 10,701,557 bytes. Its upstream model is Ultralytics YOLO11n trained on COCO;
class 52 is `hot dog`. The full AGPL-3.0 text is LICENSE-MODEL and is also required
as an exact captured license input. The export is not retrained. Local and
supplied hashes identify bytes; the local selection remains publisher unverified.
See [Ultralytics YOLO11](https://docs.ultralytics.com/models/yolo11/) and the
[upstream weight release](https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt).

The runtime project pins ONNX Runtime 1.30.0, NumPy 2.5.3, Pillow 12.3.0,
MCP 1.30.0 and jsonschema 4.26.0. uv.lock freezes all transitive versions and
wheel hashes. The builder downloads only immutable compatible wheels through
the host's controlled transport and installs them offline with copied files.
Complete wheel license texts, native library notices and metadata are retained
inside python/site-packages and indexed in dependency-notices.json. CPython's
full PSF and bundled component notices are copied from the private toolchain.

FlatBuffers 25.12.19's wheel omits a full license file. LICENSE-FLATBUFFERS is the
exact full Apache-2.0 text from
[the pinned upstream release](https://github.com/google/flatbuffers/blob/v25.12.19/LICENSE),
SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.
HTTPX 0.28.1's wheel also omits its full license text. LICENSE-HTTPX is the exact
BSD-3-Clause text from
[the pinned upstream release](https://github.com/encode/httpx/blob/0.28.1/LICENSE.md),
SHA-256 `4ec59d544f12b5f539a3a716fd321ac58ccd8030b465221f2c880200cdf28d8d`.
Both texts are added only to those exact installed distribution directories;
the frozen wheel identities remain unchanged and the additions enter the final
sealed package lock.

Inference explicitly selects CPUExecutionProvider. The model's single float
input `[1,3,640,640]`, single float output `[1,84,8400]`, complete COCO labels,
task/export metadata and model bytes must all match. See the
[ONNX Runtime Python API](https://onnxruntime.ai/docs/api/python/api_summary.html).
