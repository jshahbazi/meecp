# Third-party notices

## YOLO11n

- Publisher: Ultralytics.
- Checkpoint: `yolo11n.pt`, official release `v8.3.0`.
- Source: https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
- Software used for export: https://github.com/ultralytics/ultralytics/tree/v8.3.221
- Model documentation: https://docs.ultralytics.com/models/yolo11/
- License: GNU Affero General Public License, version 3, included as `LICENSE`.
- Modification: exported to fixed-shape FP32 ONNX, opset 17, with metadata date normalized for reproducibility. Weights were not retrained.
- Exact hashes, sizes, and export toolchain: `model-provenance.json`.

## Runtime

The runnable folder includes CPython 3.12.13 from uv's managed python-build-standalone distribution. CPython's Python Software Foundation license and accompanying source notices are included as `LICENSE-PYTHON`, copied from https://github.com/python/cpython/blob/v3.12.13/LICENSE. Runtime distribution sources: https://github.com/astral-sh/python-build-standalone.

The runtime dependency set is recorded in `uv.lock` and the hash-checked `requirements.txt`. Wheel distributions include their license files under `runtime/lib/python3.12/site-packages/` in the runnable folder:

| Component | Upstream | License |
| --- | --- | --- |
| ONNX Runtime | https://github.com/microsoft/onnxruntime | MIT |
| NumPy | https://github.com/numpy/numpy | BSD-3-Clause, with bundled-component notices |
| Pillow | https://github.com/python-pillow/Pillow | MIT-CMU / historical Pillow notices, with bundled-component notices |
| FlatBuffers | https://github.com/google/flatbuffers | Apache-2.0 |
| Packaging | https://github.com/pypa/packaging | Apache-2.0 or BSD-2-Clause |
| Protobuf | https://github.com/protocolbuffers/protobuf | BSD-3-Clause |

The FlatBuffers wheel declares Apache-2.0 but omits the full license text. A copy of the Apache-2.0 license is included as `LICENSE-FLATBUFFERS`, sourced from https://www.apache.org/licenses/LICENSE-2.0.txt.

Build-only dependencies such as PyTorch, torchvision, ONNX, and Ultralytics are excluded from the runnable folder; their source locations, versions, and wheel licenses remain available through `uv.lock` and their installed distributions when building from source.
