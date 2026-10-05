"""Public signed CLI/native command acceptance, plus labeled private faults.

The ordinary native, preparation, install, restart and relocation flow starts
the shipped CLI. It requires an already active signed release policy and never
constructs execution admission or supplies a serve factory. Existing topology
and crash controls retain their separate trusted private embedding scope.
"""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

import verify_phase3_native_command as command

common = command.onboarding
require = common.require
COMPONENT = ("host-command-json-v1", "1.0.1", "a216f2ed58294410850afc4bd63f9b9b63c945464a1f10d1b735bd8de26cd607")
INSTALLED_PLATFORM = "3898759c85d08268db02963c0e822e2bd3c9657d337e1e5c5708e0bb0738c83b"
RESOURCE_PLATFORM = "03a5e27b4931b1eaa705f0a4be373bc202ac7dd3bb4d71a438c2eb6ffeae1b8a"
CERTIFICATE = "EFE6CD2666B9106E68724C38ADD1C405267A2F94"
TEAM = "S5BT2PRN5B"
POLICY_PATH = "templates/release-policies/phase3-v1.json"


def _identity(path):
    value = path.lstat()
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
        value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def snapshot(app):
    """Complete bounded regular-file signed input identity; never source text."""
    root = _identity(app)
    require(stat.S_ISDIR(root[2]) and root[3] == os.getuid() and not root[2] & 0o6022,
        "public_command_input_mode_invalid")
    records, total, files, directories = {".": (root, None)}, 0, 0, 1
    for path in app.rglob("*"):
        before = _identity(path)
        require(before[3] == os.getuid() and not before[2] & 0o6022, "public_command_input_mode_invalid")
        if stat.S_ISDIR(before[2]):
            directories += 1
            require(directories <= 24096, "public_command_input_limit")
            records[path.relative_to(app).as_posix()] = (before, None)
            continue
        require(stat.S_ISREG(before[2]) and before[4] == 1 and path.resolve() == path,
            "public_command_input_type_invalid")
        total += before[5]
        files += 1
        require(files <= 20000 and total <= 4 * 1024**3, "public_command_input_limit")
        records[path.relative_to(app).as_posix()] = (before, common.digest(path))
        require(_identity(path) == before, "public_command_input_changed")
    require(files > 0, "public_command_input_empty")
    return records


def _json(path):
    try:
        value = path.lstat()
        require(stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid() and value.st_nlink == 1
            and not value.st_mode & 0o6022 and value.st_size <= 64 * 1024 and path.resolve() == path,
            "public_command_policy_invalid")
        def unique(pairs):
            result = {}
            for key, item in pairs:
                require(key not in result, "public_command_policy_invalid")
                result[key] = item
            return result
        data = path.read_bytes()
        result = json.loads(data, object_pairs_hook=unique,
            parse_constant=lambda _: require(False, "public_command_policy_invalid"))
        require(type(result) is dict, "public_command_policy_invalid")
        return data, result
    except FileNotFoundError:
        raise common.AcceptanceError("public_command_policy_missing") from None
    except (OSError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, common.AcceptanceError):
            raise
        raise common.AcceptanceError("public_command_policy_invalid") from None


def active_policy(host):
    data, value = _json(host / POLICY_PATH)
    expected = {"component": list(COMPONENT), "installed_platform_policy": INSTALLED_PLATFORM,
        "resource_platform_policy": RESOURCE_PLATFORM}
    require(set(value) == {"schema_version", "policy_id", "preparation", "command", "agent"}
        and type(value["schema_version"]) is int and value["schema_version"] == 1,
        "public_command_policy_invalid")
    require(value["command"] == expected, "public_command_release_inactive")
    preparation = value["preparation"]
    component = dict(zip(("id", "version", "digest"), COMPONENT))
    require(type(preparation) is dict
        and set(preparation) == {"families", "recipe_component_sets", "resolution_candidate_kinds"}
        and type(preparation["families"]) is list and type(preparation["recipe_component_sets"]) is list
        and component in preparation["families"] and [component] in preparation["recipe_component_sets"],
        "public_command_family_not_admitted")
    return hashlib.sha256(data).hexdigest()


def signature(path, *, deep=False):
    requirement = '=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf = H"' + CERTIFICATE + '"'
    try:
        results = [subprocess.run(["/usr/bin/codesign", *arguments, str(path)], capture_output=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}) for arguments in (
                ["--verify", "--strict", *(["--deep"] if deep else []), "-R", requirement],
                ["--display", "--verbose=4"], ["--display", "--entitlements", ":-"])]
        require(all(result.returncode == 0 for result in results), "public_command_signature_invalid")
        detail = results[1].stderr.decode("utf8", "replace")
        require("(runtime)" in detail and re.search(r"^Timestamp=.+$", detail, re.M)
            and re.search(r"^TeamIdentifier=" + TEAM + r"$", detail, re.M)
            and b"<key>" not in results[2].stdout + results[2].stderr, "public_command_signature_invalid")
    except (OSError, subprocess.SubprocessError):
        raise common.AcceptanceError("public_command_signature_invalid") from None


def review_answers(form):
    values = {"Synthetic test input": {"ok": True}, "Expected synthetic result": {"ok": True},
        "Fixed command arguments": {"args": ["measured-tree"]}, "Command deadline (milliseconds)": 2000}
    answers = {}
    for field in form["fields"]:
        if field["kind"] == "schema-preview":
            continue
        if field["label"] in values:
            answers[field["id"]] = values[field["label"]]
        elif field["label"] in {"Confirm the documented command interface", "Confirm the documented command effects",
                "Source license or notice"}:
            require(field["kind"] == "choice" and len(field["choices"]) == 1, "public_command_review_choice_changed")
            answers[field["id"]] = field["choices"][0]["id"]
        else:
            require(not field["required"] or field.get("value") is not None, "public_command_review_field_changed")
            if field.get("value") is not None:
                answers[field["id"]] = field["value"]
    return answers


UPGRADE_STABLE_FIELDS = ("slot_id", "package_digest", "package_path", "settings_digest",
    "settings_path", "data_path", "public_port", "credential_ref", "start_policy", "wake_suppressed")


def operation_snapshot(state):
    path = state / "registry.sqlite"
    details = path.lstat()
    require(stat.S_ISREG(details.st_mode) and details.st_uid == os.getuid()
        and stat.S_IMODE(details.st_mode) == 0o600 and path.resolve() == path,
        "public_command_upgrade_unsafe_registry")
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rows = database.execute("SELECT operation_id,action,slot_id,generation,status,error_code,"
            "created_at,updated_at,idempotency_key,details FROM operations ORDER BY operation_id").fetchall()
    finally:
        database.close()
    return {"operation_count": len(rows),
        "completed_install_count": sum(row[1] == "install" and row[4] == "completed" for row in rows),
        "upgrade_count": sum(row[1] == "upgrade" for row in rows),
        "identity_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()}


def upgrade_transition(before, after, operations_before, operations_after):
    require(before.get("start_policy") == "host" and before.get("wake_suppressed") is False,
        "public_command_upgrade_restart_policy_changed")
    require(all(key in before and key in after and before[key] == after[key]
        for key in UPGRADE_STABLE_FIELDS), "public_command_upgrade_slot_identity_changed")
    generation = before.get("generation")
    require(type(generation) is int and generation >= 1 and type(after.get("generation")) is int
        and after["generation"] == generation + 1, "public_command_upgrade_runtime_generation_changed")
    require(operations_before["operation_count"] == operations_before["completed_install_count"] == 1
        and operations_before["upgrade_count"] == 0 and operations_after == operations_before,
        "public_command_upgrade_operation_history_changed")
    return {"runtime_generation_before": generation, "runtime_generation_after": after["generation"],
        "expected_runtime_generation_delta": 1, "runtime_generation_transition_verified": True,
        "installed_identity_preserved": True, "operation_history_preserved": True,
        "operation_history_sha256": operations_before["identity_sha256"]}


class PublicCommandHarness(command.CommandHarness):
    def __init__(self, *args, fixture, report, upgrade_app=None):
        super().__init__(*args, fixture=fixture)
        self.public_report = report
        self.upgrade_app = upgrade_app

    async def start(self, state, environment, home, *, enabled):
        self.public_report["public_cli_starts"] = self.public_report.get("public_cli_starts", 0) + 1
        return await common.Harness.start(self, state, environment, home, enabled=False)

    async def defaults(self):
        common.stage("production_defaults", "defaults")
        active_policy(self.host)
        base, state, home, environment = self.new_case("public-command-defaults")
        source = base / "ordinary source"; source.mkdir(mode=0o700)
        original = self.source_fixture(source, "defaults")
        child = await self.start(state, environment, home, enabled=False)
        try:
            caps = await self.cli(state, environment, home, "capabilities", "--json")
            execution = caps.get("preparation_execution", {})
            require(caps.get("package_schema_versions") == [1, 2]
                and caps.get("profiles") == ["mcp-stdio", "mcp-http", "model-service", "host-command"],
                "public_command_install_capability_inactive")
            require(caps["automatic_preparation"] is True and caps["recipe_execution"] is True
                and list(COMPONENT) in execution.get("families", [])
                and [list(COMPONENT)] in execution.get("recipe_component_sets", []), "public_command_capability_inactive")
            draft = await self.cli(state, environment, home, "imports", "create", "--source", source,
                "--source-mode", "automatic", "--name", "Public command review fixture", "--json")
            require(draft["source"]["kind"] == "application_directory", "public_command_intake_changed")
            form = await self.manager(state, "/v1/preparations/form", {"import_id": draft["draft_id"], "revision": draft["revision"]})
            require(form["execution_available"] is True and form["resolution_required"] is False
                and form["recipe"]["id"] == "mcpme.native-command" and form["recipe"]["version"] == "1.0.1",
                "public_command_form_inactive")
            recipe = await self.cli(state, environment, home, "recipes", "inspect", "--id", form["recipe"]["id"],
                "--version", form["recipe"]["version"], "--json")
            require(recipe["state"] == "enabled" and recipe["revoked"] is False and recipe["digest"] == form["recipe"]["digest"]
                and {(item["component"], item["version"], item["digest"]) for item in recipe["manifest"]["steps"]} == {COMPONENT},
                "public_command_recipe_not_admitted")
            review = await self.manager(state, "/v1/preparations/plan-form", {"import_id": draft["draft_id"],
                "revision": draft["revision"], "candidate_id": form["candidate_id"], "recipe": form["recipe"],
                "form_digest": form["form_digest"], "answers": review_answers(form)})
            require(review["execution_available"] is True and review["blockers"] == []
                and review["recipe"] == form["recipe"] and type(review["plan_id"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", review["plan_digest"]), "public_command_review_inactive")
            require((await self.cli(state, environment, home, "preparations", "list", "--json"))["preparations"] == []
                and (await self.cli(state, environment, home, "resolutions", "list", "--json"))["resolutions"] == [],
                "public_command_review_created_job")
            self.source_check(source, original, remove=True)
            self.public_report["public_review"] = {"exact_family_and_singleton_closure": True,
                "installed_schema_two_and_command_profile_advertised": True,
                "generated_form_execution_available": True, "review_execution_available": True,
                "review_created_jobs": False, "recipe_digest": recipe["digest"], "plan_digest": review["plan_digest"]}
            return True
        finally:
            await self.shutdown(child, state)

    async def case(self, label):
        result = await super().case(label)
        result["public_execution_authority"] = "shipped_signed_release_policy"
        result["public_cli_factory_or_admission_injection"] = False
        result["additional_private_fault_controls"] = {"scope": "trusted_private_embedding",
            "public_fault_authority_verified": False, "controls": result.pop("command_topology")}
        if self.upgrade_app is not None:
            result["cross_build_upgrade"] = await self.upgrade(result)
        else:
            result["cross_build_upgrade"] = {"verified": False, "second_signed_app_supplied": False}
        return result

    async def upgrade(self, original):
        from mcpme.contracts import validate_package
        require(original["source_removed"] is True and original["original_state_removed"] is True,
            "public_command_upgrade_source_not_removed")
        state = self.root / "relocation/state"
        _, _, home, environment = self.new_case("cross-build-upgrade")
        first = await self.start(state, environment, home, enabled=False)
        try:
            slots = (await self.cli(state, environment, home, "slots", "list", "--json"))["slots"]
            require(len(slots) == 1 and slots[0]["package_digest"] == original["package_digest"],
                "public_command_upgrade_original_slot_changed")
            slot = slots[0]
            package_root = Path(slot["package_path"])
            package = validate_package(package_root)
            require(package.digest == original["package_digest"] and package.manifest.profile == "host-command",
                "public_command_upgrade_original_package_changed")
            tools = [tool.mcp_schema() for tool in package.manifest.tools]
            require(hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest() == original["full_tool_contract_sha256"],
                "public_command_upgrade_original_tools_changed")
            before = snapshot(package_root)
            operations_before = operation_snapshot(state)
        finally:
            await self.shutdown(first, state)
        upgrade_root = self.root / "second signed build"; upgrade_root.mkdir(mode=0o700)
        copied = {}
        host, native = common.relocate_host_and_native(upgrade_root, self.upgrade_app / "Contents/Resources/host",
            self.upgrade_app / "Contents/MacOS/mcpme", copied)
        require(copied["native_bundle_preserved"] is True, "public_command_upgrade_bundle_not_preserved")
        active_policy(host)
        successor = PublicCommandHarness(upgrade_root, host, native, self.lease,
            fixture=self.fixture, report=self.public_report)
        successor.children = self.children
        successor.states.append(state)
        second = await successor.start(state, environment, home, enabled=False)
        try:
            slots = (await successor.cli(state, environment, home, "slots", "list", "--json"))["slots"]
            require(len(slots) == 1, "public_command_upgrade_slot_identity_changed")
            transition = upgrade_transition(slot, slots[0], operations_before, operation_snapshot(state))
            calls = [("run", {"ok": True}, {"ok": True})]
            http = await successor.sdk(state, environment, home, slot["slot_id"], tools, calls, "http")
            stdio = await successor.sdk(state, environment, home, slot["slot_id"], tools, calls, "stdio")
        finally:
            await successor.shutdown(second, state)
            await successor.cleanup()
        require(not successor.children and snapshot(package_root) == before
            and validate_package(package_root).digest == package.digest, "public_command_upgrade_sealed_package_changed")
        return {"verified": True, "second_signed_app_supplied": True, "same_state_reopened": True,
            "reinstalled": False, "selected_source_available": False, **transition,
            "package_digest": package.digest, "full_tool_contract_sha256": original["full_tool_contract_sha256"],
            "sealed_package_preserved": True, "http_calls": http, "stdio_calls": stdio,
            "ordinary_owned_children_joined": True, "execution_authority": "shipped_signed_release_policy"}


async def verify(host, native, fixture, report, *, upgrade_app=None):
    app = common.native_app_root(native)
    require(app is not None and host == app / "Contents/Resources/host", "public_command_signed_app_required")
    require(fixture.is_absolute() and fixture.resolve() == fixture and stat.S_ISREG(fixture.lstat().st_mode)
        and os.access(fixture, os.X_OK), "synthetic_command_fixture_required")
    before, fixture_before = snapshot(app), (_identity(fixture), common.digest(fixture))
    upgrade_before = snapshot(upgrade_app) if upgrade_app is not None else None
    report.update(original_signed_app_files=sum(record[1] is not None for record in before.values()), original_signed_app_snapshot_sha256=
        hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(), fixture_sha256=fixture_before[1])
    try:
        report["active_release_policy_sha256"] = active_policy(host)
        common.verify_host_payload(host)
        signature(app, deep=True)
        for name in ("mcpme-command-reap", "mcpme-command-resource-reap"):
            signature(host / "bin" / name)
        if upgrade_app is not None:
            require(upgrade_app != app and upgrade_app.suffix == ".app"
                and common.digest(upgrade_app / "Contents/Resources/app-build.json") != common.digest(app / "Contents/Resources/app-build.json"),
                "public_command_second_build_required")
            upgrade_host = upgrade_app / "Contents/Resources/host"
            report["upgrade_active_release_policy_sha256"] = active_policy(upgrade_host)
            common.verify_host_payload(upgrade_host)
            signature(upgrade_app, deep=True)
            for name in ("mcpme-command-reap", "mcpme-command-resource-reap"):
                require(common.digest(upgrade_host / "bin" / name) == common.digest(host / "bin" / name),
                    "public_command_upgrade_helper_identity_changed")
                signature(upgrade_host / "bin" / name)
            report["upgrade_signed_app_files"] = sum(record[1] is not None for record in upgrade_before.values())
            report["shared_command_helpers_sha256"] = {name: common.digest(host / "bin" / name)
                for name in ("mcpme-command-reap", "mcpme-command-resource-reap")}
        factory = partial(PublicCommandHarness, fixture=fixture, report=report, upgrade_app=upgrade_app)
        await common.verify(host, native, report, harness_factory=factory, cases=("public_native_command",))
    finally:
        if "production_defaults_disabled" in report:
            report["public_release_defaults_active"] = report.pop("production_defaults_disabled")
        report["development_execution_only"] = False
        report["original_signed_app_preserved"] = snapshot(app) == before
        report["original_fixture_preserved"] = (_identity(fixture), common.digest(fixture)) == fixture_before
        if upgrade_app is not None:
            report["original_upgrade_signed_app_preserved"] = snapshot(upgrade_app) == upgrade_before
            require(report["original_upgrade_signed_app_preserved"], "public_command_upgrade_original_input_changed")
        require(report["original_signed_app_preserved"] and report["original_fixture_preserved"], "public_command_original_inputs_changed")
    report["driver_children_joined"] = report.pop("owned_children_joined")
    report.update(all_ownership_settled=False, reconciliation_required=True, unknown_legacy_child_joined=False,
        public_ordinary_native_install_ownership_settled=all(item["ordinary_native_install_ownership_settled"] is True
            for item in report["cases"].values()))
    report["cross_build_upgrade_verified"] = (upgrade_app is not None and all(
        item["cross_build_upgrade"]["verified"] is True for item in report["cases"].values()))
    if upgrade_app is not None:
        require(report["cross_build_upgrade_verified"], "public_command_upgrade_not_verified")
    if upgrade_app is None:
        report.setdefault("limits", []).append("cross_build_upgrade_unverified")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("host", "native", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--upgrade-app", type=Path, help="Second signed build sharing the reviewed command helper bytes.")
    args = parser.parse_args()
    os.umask(0o077); logging.disable(logging.CRITICAL)
    report = {"schema_version": 1, "verified": False, "scope": "public_signed_native_installed_command",
        "cases": {}, "public_cli_starts": 0, "limits": ["physical_dialog_clicks_unverified",
            "hard_aggregate_memory_unverified", "complete_lifetime_measurement_unverified",
            "additional_topology_crash_controls_use_private_embedding",
            "deliberate_ancestor_group_loss_retains_unjoined_legacy_gateway_authority"]}
    try:
        asyncio.run(verify(args.host.resolve(strict=True), args.native.resolve(strict=True), args.fixture.resolve(strict=True), report,
            upgrade_app=args.upgrade_app.resolve(strict=True) if args.upgrade_app is not None else None))
    except (Exception, KeyboardInterrupt) as error:
        failure, _ = common.bounded_failure(error)
        report["verified"] = False
        report["diagnostic"] = {**getattr(failure, "context", getattr(failure, "acceptance_context", common.DIAGNOSTIC)),
            "code": failure.code if isinstance(failure, common.AcceptanceError) else "public_command_acceptance_failed",
            "type": type(failure).__name__ if type(failure).__name__ in {"KeyError", "ValueError", "TypeError", "AttributeError",
                "AcceptanceError", "TimeoutError", "ExceptionGroup", "FileNotFoundError", "OSError"} else "Exception"}
        if isinstance(failure, common.AcceptanceError) and hasattr(failure, "native_stage"):
            report["diagnostic"]["native_stage"] = failure.native_stage
        if isinstance(failure, common.AcceptanceError) and hasattr(failure, "native_observation"):
            report["native_observation"] = failure.native_observation
    try:
        common.write_private_json(args.output.absolute(), report)
    except Exception:
        report["verified"] = False
    print(json.dumps({"verified": report["verified"], "scope": report["scope"], "diagnostic": report.get("diagnostic")}, sort_keys=True))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
