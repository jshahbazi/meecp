"""Actual native resolved-Python build of a complete mixed directory closure.

Trusted development-only admission is restricted to the selected host's exact
current ResolvedPython reference. The ordinary production CLI stays disabled.
No model is loaded; fixtures contain only a tiny synthetic callable and assets.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil

import verify_phase3_native_onboarding as native


class MixedPythonHarness(native.Harness):
    def __init__(self, *args, evidence_directory):
        super().__init__(*args)
        self.evidence_directory = evidence_directory

    def source_fixture(self, source, label):
        super().source_fixture(source, label)
        (source / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\n\0bounded synthetic asset")
        (source / "unknown.payload").write_bytes(b"unknown\0bounded synthetic payload")
        self.mixed_files = {path.name: native.digest(path) for path in source.iterdir()}
        self.mixed_source = source
        return dict(self.mixed_files)

    async def cli(self, state, environment, home, *arguments, expected_error=None):
        selected = (arguments[:2] == ("imports", "create")
            and hasattr(self, "mixed_source") and "--source" in arguments
            and Path(arguments[arguments.index("--source") + 1]) == self.mixed_source)
        if selected:
            arguments = (*arguments, "--source-mode", "automatic")
        result = await super().cli(state, environment, home, *arguments, expected_error=expected_error)
        if selected:
            native.require(result["source"]["kind"] == "application_directory"
                and result["source"]["contract_version"] == 2
                and {item["path"]: item["sha256"] for item in result["source"]["files"]} == self.mixed_files,
                "mixed_intake_binding_mismatch")
            self.mixed_draft = result
            self.mixed_snapshot = Path(result["source"]["snapshot_path"])
            shutil.rmtree(self.mixed_source)
        return result

    def source_check(self, source, original_files, *, remove=False):
        native.require(source == self.mixed_source and not source.exists()
            and original_files == self.mixed_files
            and {path.name: native.digest(path) for path in self.mixed_snapshot.iterdir()} == self.mixed_files,
            "mixed_snapshot_binding_mismatch")

    async def case(self, label):
        try:
            result = await super().case(label)
        except BaseException as error:
            self.lease["safe_to_remove"] = False
            if isinstance(error, native.AcceptanceError) and error.code == "resource_blocked":
                state = self.states[-1]
                policy = native.private_json(state / "resource-policy.json")
                error.resource_observation = {"memory_budget_mb": policy["memory_budget_mb"],
                    "headroom_mb": policy["headroom_mb"], "disk_free_bytes": shutil.disk_usage(state).free,
                    "owned_children_joined": not self.children}
            raise
        from meecp.imports import ImportStore
        from meecp.contracts import validate_package
        from meecp.preparation_resolved_python import ResolvedPythonComponent
        with ImportStore(self.states[-1]) as store:
            draft = store.inspect(self.mixed_draft["draft_id"])
            package = validate_package(Path(draft["attachment"]["path"]))
            native.require(package.digest == result["package_digest"]
                and {path.name: native.digest(path) for path in (package.root / "source").iterdir()} == self.mixed_files,
                "mixed_package_source_closure_mismatch")
            native.require(all((path.stat().st_mode & 0o777) == 0o600
                for path in (package.root / "source").iterdir()), "mixed_source_mode_mismatch")
            native.require(not self.children and native.preparation_ownership_count(self.states[-1]) == 0,
                "mixed_evidence_ownership_unsettled")
            row = store._connection.execute("SELECT data FROM preparation_selections WHERE import_id=?",
                (draft["draft_id"],)).fetchone()
            native.require(row is not None, "mixed_selection_missing")
            selection = json.loads(row["data"])
            reference = ResolvedPythonComponent().reference
            selected, = selection["descriptor"]["components"]
            native.require(tuple(selected["component"][key] for key in ("id", "version", "digest")) == reference.key,
                "mixed_current_component_mismatch")
            evidence = self.evidence_directory
            native.require(evidence.is_absolute() and evidence.parent.resolve() == evidence.parent
                and not evidence.exists() and not evidence.is_symlink(), "mixed_private_evidence_required")
            evidence.mkdir(mode=0o700)
            shutil.copytree(package.root, evidence / "package", symlinks=True)
            native.require(validate_package(evidence / "package").digest == package.digest,
                "mixed_retained_package_changed")
            native.write_private_json(evidence / "import.json", draft)
            native.write_private_json(evidence / "selection.json", selection)
            native.write_private_json(evidence / "binding.json", {"source_revision": draft["source"]["revision"],
                "import_revision": draft["revision"], "package_digest": package.digest,
                "component": reference.model_dump(), "selected_descriptor_digest": selection["descriptor_digest"],
                "owned_children_joined": True, "preparation_owners": 0})
        result.update({"application_directory_contract_version": 2,
            "binary_and_unknown_assets_retained": True, "original_removed_before_resolution": True,
            "current_component": reference.model_dump(), "retained_evidence_directory": str(evidence),
            "retained_import_sha256": native.digest(evidence / "import.json"),
            "retained_selection_sha256": native.digest(evidence / "selection.json"),
            "retained_binding_sha256": native.digest(evidence / "binding.json")})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "mixed_python_directory_native_installed",
        "cases": {}, "limits": ["model_accuracy_unmeasured", "hard_aggregate_resources_unverified",
            "filesystem_network_isolation_unverified", "whole_phase3_adversarial_review_pending"]}
    implementation = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        asyncio.run(native.verify(args.host.resolve(strict=True), args.native.resolve(strict=True), report,
            harness_factory=lambda *values: MixedPythonHarness(*values, evidence_directory=args.evidence_dir.absolute()),
            cases=("independent_recipe",)))
    except BaseException as error:
        bounded, types = native.bounded_failure(error)
        report["error_code"] = bounded.code if isinstance(bounded, native.AcceptanceError) else "mixed_python_acceptance_failed"
        report["error_type"] = type(bounded).__name__[:64]
        report["error_context"] = getattr(bounded, "context", getattr(error, "acceptance_context", dict(native.DIAGNOSTIC)))
        if types:
            report["nested_error_types"] = types
        for name in ("native_stage", "native_observation", "resource_observation"):
            if hasattr(bounded, name):
                report[name] = getattr(bounded, name)
    report["harness_sha256"] = implementation
    native.write_private_json(args.output.absolute(), report)
    print(json.dumps({"verified": report["verified"], "scope": report["scope"],
        "cases_passed": sum(item.get("passed") is True for item in report["cases"].values()),
        **({"error_code": report["error_code"], "error_type": report["error_type"]} if "error_code" in report else {})}, sort_keys=True))
    raise SystemExit(0 if report["verified"] else 1)


if __name__ == "__main__":
    main()
