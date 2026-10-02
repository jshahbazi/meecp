"""Explicit build-time download/export. The running app never downloads a model."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
WEIGHTS_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt"
WEIGHTS_SHA256 = "0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1"


def prepare() -> None:
    workspace = ROOT / ".build"
    workspace.mkdir(exist_ok=True)
    os.environ["YOLO_CONFIG_DIR"] = str(workspace / "ultralytics")
    os.environ["YOLO_AUTOINSTALL"] = "false"
    os.environ["MPLCONFIGDIR"] = str(workspace / "matplotlib")
    logging.disable(logging.CRITICAL)
    checkpoint = workspace / "yolo11n.pt"
    if not checkpoint.exists():
        with urllib.request.urlopen(WEIGHTS_URL, timeout=60) as response:
            data = response.read(16 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != WEIGHTS_SHA256:
            raise ValueError("checkpoint_integrity_failed")
        checkpoint.write_bytes(data)
    if hashlib.sha256(checkpoint.read_bytes()).hexdigest() != WEIGHTS_SHA256:
        raise ValueError("checkpoint_integrity_failed")
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        import onnx
        from ultralytics import YOLO

        model = YOLO(str(checkpoint))
        assert model.names[52] == "hot dog"
        exported = Path(model.export(format="onnx", imgsz=640, batch=1, dynamic=False,
                                     simplify=False, opset=17, nms=False, device="cpu"))
        graph = onnx.load(exported)
        for prop in graph.metadata_props:
            if prop.key == "date":
                prop.value = "2026-10-02T00:00:00"
        onnx.checker.check_model(graph)
        destination = ROOT / "models" / "yolo11n.onnx"
        destination.parent.mkdir(exist_ok=True)
        onnx.save(graph, destination)
    receipt = {"model": "Ultralytics YOLO11n", "task": "COCO object detection",
               "class": {"id": 52, "name": "hot dog"}, "license": "AGPL-3.0",
               "source": {"url": WEIGHTS_URL, "sha256": WEIGHTS_SHA256,
                          "bytes": checkpoint.stat().st_size},
               "onnx": {"path": "models/yolo11n.onnx", "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                        "bytes": destination.stat().st_size},
               "export": {"format": "onnx", "imgsz": 640, "batch": 1, "dynamic": False,
                          "simplify": False, "opset": 17, "nms": False, "device": "cpu"},
               "toolchain": {package: importlib.metadata.version(package)
                             for package in ["ultralytics", "torch", "torchvision", "onnx"]},
               "docs": "https://docs.ultralytics.com/models/yolo11/"}
    (ROOT / "model-provenance.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    try:
        prepare()
    except Exception:
        print("Model preparation failed. Check the locked build dependencies and internet connection.")
        sys.exit(1)
    print("YOLO11n model prepared and hash-verified. Detection can now run offline.")
