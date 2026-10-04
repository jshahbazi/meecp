"""Verify signed Python/MCP/ONNX preparation through the ordinary host launcher.

The supplied app must already admit these families through its shipped release
policy. This driver supplies synthetic source and native form choices. It never
injects a Supervisor, factory, admission, environment feature flag, or provider.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import struct
import time
import uuid
import zlib

import verify_phase3_native_onboarding as common
import verify_phase3_native_onnx as native_onnx

PYTHON_CASES = ("callable", "dynamic_mcp")

RESOLVED = ("resolved-python-v1", "1.0.1",
    "3c24410b46415cb83b9d945ebf0049f552e9f7493943164f311939a79b49000e")
ONNX = ("onnxruntime-cpu-v1", "1.0.0",
    "ec982f8489de85afd79d6aa715431082e1bba29be48ca06eef457dbcc362d8dc")
HOTDOG = ("yolo11-coco-detect-v1", "1.0.0",
    "b48bde76ca30787de2e41a6b984c0c80a79b3a4b6ec75637f3147ccfadec3e99")


def public_capabilities(value):
    common.require(value.get("automatic_preparation") is True
        and value.get("recipe_execution") is True, "public_execution_unavailable")
    resolution = value.get("preparation_resolution", {})
    common.require(resolution.get("execution") is True
        and resolution.get("candidate_kinds") == ["python-callable", "python-mcp"]
        and "1.0.1" in resolution.get("producer_versions", ()), "public_resolution_unavailable")
    execution = value.get("preparation_execution", {})
    digest = execution.get("admission_digest")
    common.require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
        "public_admission_identity_missing")
    families = {tuple(item) for item in execution.get("families", ())}
    closures = {frozenset(tuple(item) for item in group)
        for group in execution.get("recipe_component_sets", ())}
    common.require({RESOLVED, HOTDOG} <= families
        and {frozenset({RESOLVED}), frozenset({ONNX, HOTDOG})} <= closures,
        "public_family_not_admitted")
    return digest


class PublicPythonHarness(common.Harness):
    async def start(self, state, environment, home, *, enabled):
        # The base's false branch is exactly bin/mcpme serve. The caller's
        # selection changes no execution authority, including on restart.
        return await common.Harness.start(self, state, environment, home, enabled=False)

    async def native_preparation(self, base, state, home, environment, source, label, selected_recipe):
        common.require(selected_recipe is None, "public_fixture_recipe_override_unsupported")
        nonce = str(uuid.uuid4())
        fixture, output = base / "native-directory.json", base / "native-receipt.json"
        manager = (state / "manager-connection.json").stat(follow_symlinks=False)
        selected = source.stat(follow_symlinks=False)
        identity = {"device": manager.st_dev, "inode": manager.st_ino, "bytes": manager.st_size,
            "mode": stat.S_IMODE(manager.st_mode), "modified_seconds": manager.st_mtime_ns // 1_000_000_000,
            "modified_nanoseconds": manager.st_mtime_ns % 1_000_000_000,
            "changed_seconds": manager.st_ctime_ns // 1_000_000_000,
            "changed_nanoseconds": manager.st_ctime_ns % 1_000_000_000}
        names = ["dynamic_one", "dynamic_two"] if label == "dynamic_mcp" else ["double"]
        answers = {}
        for name in names:
            suffix = " · " + name if label == "dynamic_mcp" else ""
            answers["Test input" + suffix] = {"n": 7}
            answers["Expected test result" + suffix] = {"value": 14}
        common.write_private_json(state / ("native-public-directory-" + nonce + ".json"), {
            "schema_version": 1, "fixture_nonce": nonce, "expires_at": int(time.time()) + 300,
            "manager_file": identity, "source_directory": {"device": selected.st_dev, "inode": selected.st_ino}})
        common.write_private_json(fixture, {"schema_version": 1, "fixture_nonce": nonce,
            "state_dir": str(state), "output_path": str(output), "source_directory": str(source),
            "answers_by_label": answers})
        code, _ = await self.run([str(self.native), "--public-directory-native-acceptance", str(fixture)],
            environment, home, timeout=330)
        common.require(output.is_file(), "native_receipt_missing")
        receipt = common.private_json(output, 256 * 1024)
        common.require(code == 0 and receipt.get("passed") is True
            and receipt.get("native_directory_selection") is True, "public_native_directory_failed")
        observed = await self.cli(state, environment, home, "imports", "inspect", receipt["draft_id"], "--json")
        revision = receipt.get("source_selection_revision")
        common.require(type(revision) is int and revision >= receipt["initial_revision"]
            and revision < observed["revision"], "public_source_selection_revision_mismatch")
        resolution = await self.cli(state, environment, home, "resolutions", "get", receipt["resolution_job_id"], "--json")
        common.require(resolution["request"]["import_id"] == observed["draft_id"]
            and resolution["request"]["revision"] == revision
            and resolution["request"]["source_revision"] == observed["source"]["revision"],
            "public_resolution_origin_mismatch")
        inspected = await self.cli(state, environment, home, "preparations", "inspect-source",
            "--import", observed["draft_id"], "--revision", observed["revision"], "--json")
        candidates = [item for item in inspected["candidates"] if item["kind"] ==
            ("python-mcp" if label == "dynamic_mcp" else "python-callable")]
        common.require(len(candidates) == 1 and candidates[0]["candidate_id"] == receipt["candidate_id"],
            "public_selected_candidate_mismatch")
        # The native receipt and durable request independently bind the revision
        # immediately after folder selection/save; inspection supplies its same
        # immutable source. This projection is never written back to the host.
        origin = {"draft_id": observed["draft_id"], "revision": revision, "source": observed["source"]}
        return origin, candidates, receipt, output

    async def case(self, label):
        value = await common.Harness.case(self, label)
        value.update(native_directory_selection=True, ordinary_host_launcher=True)
        return value

    async def defaults(self):
        base, state, home, environment = self.new_case("public-capabilities")
        child = await self.start(state, environment, home, enabled=False)
        try:
            identity = public_capabilities(await self.cli(state, environment, home, "capabilities", "--json"))
            source = base / "source"; source.mkdir(mode=0o700)
            self.source_fixture(source, "callable")
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--name", "Public callable fixture", "--json")
            form = await self.manager(state, "/v1/preparations/form",
                {"import_id": draft["draft_id"], "revision": draft["revision"]})
            common.require(form["execution_available"] is True and form["resolution_available"] is True
                and form["resolution_required"] is True, "public_form_execution_unavailable")
            common.require((await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == []
                and (await self.cli(state, environment, home, "resolutions", "list", "--json"))["resolutions"] == [],
                "public_review_executed_work")
            return {"passed": True, "admission_digest": identity, "review_created_no_jobs": True}
        finally:
            await self.shutdown(child, state)


class PublicMixedHarness(PublicPythonHarness):
    def source_fixture(self, source, label):
        super().source_fixture(source, label)
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
        image = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        image += chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b"")
        (source / "sample.png").write_bytes(image)
        (source / "unknown.bin").write_bytes(b"\x00\xff\x01\x02\x03")
        return {path.name: common.digest(path) for path in source.iterdir()}


class PublicOnnxHarness(native_onnx.Harness):
    async def start(self, state, environment, home, *, enabled):
        return await common.Harness.start(self, state, environment, home, enabled=False)


def app_inventory(app):
    records = {}
    for path in app.rglob("*"):
        details = path.lstat()
        common.require(stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode), "public_app_unsafe_member")
        if stat.S_ISREG(details.st_mode):
            records[path.relative_to(app).as_posix()] = {
                "sha256": common.digest(path), "bytes": details.st_size, "mode": stat.S_IMODE(details.st_mode)}
            common.require(len(records) <= 20000, "public_app_member_limit")
    return records


async def verify(host_path, native_path, model, report):
    common._host_paths(host_path)
    build = common.verify_host_payload(host_path)
    common.require(native_path.is_absolute() and native_path.resolve() == native_path
        and native_path.is_file() and os.access(native_path, os.X_OK), "native_acceptance_binary_required")
    app = native_path.parents[2]
    common.require(app.suffix == ".app" and native_path.parent == app / "Contents/MacOS"
        and host_path == app / "Contents/Resources/host", "public_signed_app_required")
    common.verify_native_app_signature(native_path)
    before = app_inventory(app)
    model_before = common.digest(model)
    report.update({"host_source_commit": build["source_commit"], "host_source_tree_dirty": False,
        "host_build_sha256": common.digest(host_path / "build.json"),
        "host_artifacts_checked": len(build["artifact_sha256"]), "native_binary_sha256": common.digest(native_path),
        "input_app_files_checked": len(before), "input_app_inventory_digest": hashlib.sha256(
            json.dumps(before, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "ordinary_host_launcher": True, "execution_factory_injected": False, "development_execution_only": False,
        "selected_model_sha256": model_before})
    with common.isolated_workspace(report) as (root, lease):
        host, native = common.relocate_host_and_native(root, host_path, native_path, report)
        common.verify_host_payload(host); common._host_imports(host)
        harnesses = []
        try:
            roots = {label: root / ("mcpme-native-onboarding-" + label) for label in ("python", "mixed", "onnx")}
            for directory in roots.values():
                directory.mkdir(mode=0o700)
            python = PublicPythonHarness(roots["python"], host, native, lease); harnesses.append(python)
            report["public_default_capabilities"] = await python.defaults()
            for case in PYTHON_CASES:
                report["cases"][case] = await python.case(case)
            mixed = PublicMixedHarness(roots["mixed"], host, native, lease); harnesses.append(mixed)
            report["cases"]["mixed_content"] = await mixed.case("callable")
            detector = PublicOnnxHarness(roots["onnx"], host, native, lease, model); harnesses.append(detector)
            report["wrong_weights_native_capture"] = await detector.wrong_weights_denial()
            for case in native_onnx.CASES:
                report["cases"]["onnx_" + case] = await detector.onnx_case(case)
            common._check_module_origins(host)
        finally:
            cleanup_error = None
            try:
                for harness in reversed(harnesses):
                    try:
                        await harness.cleanup()
                    except BaseException as error:
                        lease["safe_to_remove"] = False
                        if cleanup_error is None:
                            cleanup_error = error
            finally:
                report["owned_children_joined"] = all(not harness.children for harness in harnesses)
            if cleanup_error is not None:
                raise cleanup_error
        common.verify_native_app_signature(native)
    report["input_app_unchanged"] = before == app_inventory(app)
    report["selected_model_unchanged"] = model_before == common.digest(model)
    common.verify_native_app_signature(native_path)
    report["verified"] = (report["owned_children_joined"] and report["input_app_unchanged"]
        and report["selected_model_unchanged"] and len(report["cases"]) == 5
        and all(item["passed"] for item in report["cases"].values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "native", "model"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "public_native_python_mcp_onnx",
        "cases": {}, "limits": ["physical_dialog_not_operated", "hard_aggregate_live_memory_unverified",
            "synthetic_fixtures_do_not_measure_recognition_accuracy", "other_family_gates_separate"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True),
            args.model.resolve(strict=True), report))
    except (Exception, KeyboardInterrupt) as error:
        error, nested = common.bounded_failure(error)
        code = getattr(error, "code", "acceptance_failed")
        report["diagnostic"] = {"code": code if isinstance(code, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code) else "acceptance_failed"}
        if nested:
            report["diagnostic"]["nested_types"] = nested
    if args.output:
        try:
            common.write_private_json(args.output.absolute(), report)
        except Exception:
            report["verified"] = False; report["report_write_error"] = "acceptance_report_write_failed"
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
