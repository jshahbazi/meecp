"""Verify signed local text preparation through the ordinary host launcher.

The supplied app must already admit its exact engine/task closure. Synthetic
model selections and form choices use the production native handlers. This
driver cannot enable a family or supply an execution factory.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re

import verify_phase3_native_onboarding as common
import verify_phase3_native_textgen as textgen
import verify_phase3_public_native as public


def public_textgen_capabilities(value, engine, task):
    identity = public.public_capabilities(value)
    execution = value["preparation_execution"]
    families = {tuple(item) for item in execution.get("families", ())}
    closures = {frozenset(tuple(item) for item in group)
        for group in execution.get("recipe_component_sets", ())}
    common.require(task in families and frozenset({engine, task}) in closures,
        "public_textgen_family_not_admitted")
    return identity


class PublicTextgenHarness(textgen.Harness):
    async def start(self, state, environment, home, *, enabled):
        return await common.Harness.start(self, state, environment, home, enabled=False)

    async def public_defaults(self):
        from mcpme.preparation_textgen import ENGINE_ID, TASK_ID, VERSION, references
        common.require(VERSION == "1.0.18", "public_textgen_producer_version_changed")
        keys = {item.id: item.key for item in references()}
        base, state, home, environment = self.new_case("public-textgen-capabilities")
        child = await self.start(state, environment, home, enabled=False)
        try:
            identity = public_textgen_capabilities(
                await self.cli(state, environment, home, "capabilities", "--json"),
                keys[ENGINE_ID], keys[TASK_ID])
            recipe = await self.recipe(state, environment, home)
            common.require(recipe["state"] == "enabled" and recipe["version"] == VERSION,
                "public_textgen_recipe_unavailable")
            common.require((await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == [],
                "public_textgen_review_executed_work")
            return {"passed": True, "admission_digest": identity, "recipe_version": VERSION,
                "review_created_no_jobs": True}
        finally:
            await self.shutdown(child, state)


async def verify(host_path, native_path, model, report):
    common._host_paths(host_path)
    build = common.verify_host_payload(host_path)
    common.require(native_path.is_absolute() and native_path.resolve() == native_path
        and native_path.is_file() and os.access(native_path, os.X_OK), "native_acceptance_binary_required")
    app = native_path.parents[2]
    common.require(app.suffix == ".app" and native_path.parent == app / "Contents/MacOS"
        and host_path == app / "Contents/Resources/host", "public_signed_app_required")
    common.verify_native_app_signature(native_path)
    common.require(model.is_file() and model.stat().st_size == textgen.MODEL_BYTES
        and common.digest(model) == textgen.MODEL_SHA256, "textgen_selected_model_identity_mismatch")
    before = public.app_inventory(app)
    report.update({"host_source_commit": build["source_commit"], "host_source_tree_dirty": False,
        "host_build_sha256": common.digest(host_path / "build.json"),
        "host_artifacts_checked": len(build["artifact_sha256"]), "native_binary_sha256": common.digest(native_path),
        "input_app_files_checked": len(before), "ordinary_host_launcher": True,
        "execution_factory_injected": False, "development_execution_only": False,
        "selected_model_sha256": textgen.MODEL_SHA256, "selected_model_bytes": textgen.MODEL_BYTES})
    with textgen.isolated_workspace(report) as (root, lease):
        host, native = common.relocate_host_and_native(root, host_path, native_path, report)
        textgen._host_paths(host); textgen._host_imports(host)
        identity = await asyncio.to_thread(textgen.sealed_host_identity, host)
        harness = PublicTextgenHarness(root, host, native, lease, model)
        try:
            code, _ = await harness.run([str(native), "--artifact-capture-ui-self-test"],
                {"HOME": str(root), "PATH": "/usr/bin:/bin", "TMPDIR": str(root)}, root)
            common.require(code == 0, "native_capture_ui_contracts_failed")
            report["native_capture_ui_contracts"] = True
            report["public_default_capabilities"] = await harness.public_defaults()
            for label in textgen.CASES:
                report["cases"][label] = await harness.textgen_case(label)
                report["cases"][label]["ordinary_host_launcher"] = True
            textgen.check_origins(host)
            common.require(await asyncio.to_thread(textgen.sealed_host_identity, host) == identity,
                "textgen_sealed_host_mutated")
            report["sealed_host_unchanged"] = True
        finally:
            try:
                await harness.cleanup()
            except BaseException:
                lease["safe_to_remove"] = False
                raise
            finally:
                report["owned_children_joined"] = not harness.children
        common.verify_native_app_signature(native)
    report["input_app_unchanged"] = before == public.app_inventory(app)
    report["selected_model_unchanged"] = (model.stat().st_size == textgen.MODEL_BYTES
        and common.digest(model) == textgen.MODEL_SHA256)
    common.verify_native_app_signature(native_path)
    report["verified"] = (report["owned_children_joined"] and report["input_app_unchanged"]
        and report["selected_model_unchanged"] and len(report["cases"]) == 2
        and all(item["passed"] for item in report["cases"].values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "native", "model"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "public_native_local_textgen",
        "cases": {}, "limits": ["physical_dialog_not_operated", "hard_aggregate_live_memory_unverified",
            "synthetic_generation_does_not_measure_language_quality", "final_adversarial_review_open"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True),
            args.model.resolve(strict=True), report))
    except (Exception, KeyboardInterrupt) as error:
        report["verified"] = False
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
