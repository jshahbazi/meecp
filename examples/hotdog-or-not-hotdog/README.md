# Hotdog or Not Hotdog

A small standalone photo app inspired by *Silicon Valley*. Upload an image, press **Detect**, and get **HOTDOG** or **NOT HOTDOG**. Detected hotdogs have bounding boxes and a model confidence score.

## Try it

Double-click **Launch.command** in this folder. A terminal opens and the app launches in your default browser. Choose or drop a photo, then press **Detect**. Close the terminal or press **Control-C** to stop the app.

The supplied runnable folder includes private Python, locked dependencies, and model weights. No setup, account, API key, or internet connection is needed to use it. It targets Apple silicon macOS. This is a local development app launched through Terminal.

JPEG, PNG, WebP, BMP, and GIF images are accepted, up to 12 MiB and 24 million pixels. Animated images use their first frame. Photo orientation is corrected from EXIF metadata. The threshold starts at 0.35; lower values accept weaker detections.

**NOT HOTDOG means that no hotdog was detected at the selected threshold.** YOLO can miss hotdogs, and its detection score is not a calibrated probability that an image contains one. This demo has no custom training or broad accuracy evaluation.

## How it works

[Ultralytics YOLO11n](https://docs.ultralytics.com/models/yolo11/) is a pretrained COCO object detector. The app uses the `hot dog` class (class ID 52), runs a fixed 640-pixel FP32 ONNX model on the CPU, and applies confidence filtering and nonmaximum suppression. The source checkpoint, exported model hashes, export settings, and toolchain versions are recorded in `model-provenance.json`.

`app.py` serves the upload interface using Python's standard library. `detector.py` implements inference and also exports the ordinary Python function `detect_hotdog(image_path=None, confidence=0.35, *, image_base64=None)`. Pass exactly one absolute local image path or an actual image encoded as base64 (including an image data URL). It returns a JSON-compatible dictionary with the verdict, score, original-pixel boxes, dimensions, model name, and elapsed inference time. Errors use short content-free codes.

The service binds to `127.0.0.1` on an available port, authenticates browser requests with a random session cookie, checks Host and Origin, and serializes inference. Photos and results stay in memory. The app does not save uploads, write detection history, or make runtime network requests. Source development setup downloads dependencies and a hash-pinned model explicitly.

This folder contains an ordinary application. It has no host-specific package manifest, adapter, install hooks, or automatic registration. `mcp-tool.json` supplies the optional tool contract for an external agent preparing a Python callable adapter.

## Images supplied by an agent

An MCP adapter must use the current `mcp-tool.json` contract and the current `detector.py`. Older prepared packages requiring `image_path` need to be prepared and installed again; editing the original source does not update a sealed installation.

If the client exposes an attachment's local path, the agent can pass that path directly. If it exposes the image bytes, it can pass them through `image_base64` as canonical base64 or a `data:image/png;base64,...` URL (JPEG, WebP, BMP, and GIF are also accepted). Inline data is decoded in memory. Both inline and file inputs allow up to 12 MiB. A prepared adapter must declare `lifecycle.max_request_bytes: 17825792` (17 MiB) so base64 and JSON framing fit through the host bridge and gateway. Packages without an explicit declaration retain the 1 MiB request default. Larger images need client-side resizing/compression before they are passed to the detector.

MCP tool calls carry [explicit JSON arguments](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#calling-tools); they do not automatically carry the conversation's attachments. A client showing an image to its vision model does not establish that its agent can retrieve the file bytes. Attachment access depends on the client and session. The server cannot recover an image from visual context, a private attachment identifier, or a path on another computer. Do not have the model invent a path or generate base64 from what it sees. No file chooser, clipboard reading, or remote image fetching is performed.

For a client that exposes attachment paths or bytes, use: "Run the hotdog detector on this attached image. Use its available attachment path, or pass its actual image bytes through image_base64."

## Develop from source

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed. From the source folder:

```sh
uv sync --locked --group build --group dev
uv run --locked --group build python prepare_model.py
uv run --locked python app.py
```

`Setup.command` performs dependency installation and model export. Model and runtime files are generated locally and excluded from Git. The model is exported using pinned Ultralytics, PyTorch, torchvision, and ONNX versions. Runtime inference needs only NumPy, Pillow, and ONNX Runtime.

Run the focused checks:

```sh
uv run --locked python -m pytest -q
```

The delivered build passed 19 focused checks for preprocessing, postprocessing, upload validation, authentication, and request handling. A smoke check at threshold 0.35 detected the hotdog-positive COCO128 photo and rejected eight selected negative photos; inference took 14–22 ms on the build Mac. The browser was checked with both verdicts and bounding boxes. The relocated private runtime also performed real inference with the original source model unavailable, and its launcher started and stopped without uv or a global Python. These are functional checks; COCO128 contains training images and this small selection does not measure generalization accuracy. Fixture source: https://github.com/ultralytics/assets/releases/download/v0.0.0/coco128.zip.

To build another runnable folder after model preparation:

```sh
uv export --locked --no-dev --no-group build --no-emit-project --format requirements-txt --output-file requirements.txt
uv run --locked python build_demo.py --output /absolute/path/to/new-folder
```

The builder requires a new output directory, copies a relocatable managed CPython runtime, and installs only hash-checked wheels from the locked runtime dependency list. Preserve the complete folder when moving it.

## Licensing

The demo source and bundled YOLO model are provided under AGPL-3.0; see `LICENSE`. Third-party runtime dependencies retain their own licenses. See `THIRD_PARTY_NOTICES.md`. This is an independent fan-inspired demonstration.
