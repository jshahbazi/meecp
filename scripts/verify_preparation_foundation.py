"""Verify static recipe/planning routes through a relocated private CLI host.

Uses only synthetic local metadata and fresh temporary state. Executes no source
or recipe worker, installs no package and never touches the user's host state.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from meecp_local_transport import socket_path  # noqa: E402


async def verify(host_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="meecp-preparation-foundation-") as temporary:
        root = Path(temporary).resolve()
        host = root / "relocated-host"
        shutil.copytree(host_path, host, symlinks=True)
        launcher, reaper = host / "bin/meecp", host / "bin/meecp-reap"
        state = root / "state"
        # No development tools/Python cache are discoverable by these commands.
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "en_US.UTF-8",
                       "PYTHONHOME": "/nonexistent", "PYTHONUSERBASE": "/nonexistent"}
        (root / "meecp.py").write_text("raise RuntimeError('unsafe current-directory module imported')\n")
        process = None

        def command(*arguments, expected_error=None):
            result = subprocess.run([str(launcher), "--state-dir", str(state), *arguments],
                cwd=root, env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            try:
                value = json.loads(result.stdout)
            except ValueError:
                raise ValueError("foundation_cli_failed") from None
            if expected_error is not None:
                if value.get("error", {}).get("code") != expected_error:
                    raise ValueError("foundation_rejection_mismatch")
            elif result.returncode or "error" in value:
                raise ValueError("foundation_cli_failed")
            return value

        async def start():
            child = subprocess.Popen([str(launcher), "--state-dir", str(state), "serve", "--reaper", str(reaper)],
                cwd=root, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                async with asyncio.timeout(30):
                    while child.poll() is None:
                        if (state / "manager-connection.json").exists():
                            try:
                                command("capabilities", "--json")
                                return child
                            except ValueError:
                                pass
                        await asyncio.sleep(0.05)
                raise ValueError("foundation_host_failed")
            except BaseException:
                if child.poll() is None:
                    child.terminate()
                    await asyncio.to_thread(child.wait, 10)
                raise

        try:
            process = await start()
            capabilities = command("capabilities", "--json")
            assert capabilities["automatic_preparation"] is False and capabilities["recipe_execution"] is False
            bundle = root / "external-author-recipe"
            scaffold = command("recipes", "scaffold", "--output", str(bundle), "--id", "local.independent", "--json")
            checked = command("recipes", "check", "--path", str(bundle), "--json")
            assert checked["digest"] == scaffold["digest"] and checked["runnable"] is False
            added = command("recipes", "add", "--path", str(bundle), "--json")
            enabled = command("recipes", "enable", "--id", "local.independent", "--version", "1.0.0", "--json")
            assert added["state"] == "proposed" and enabled["state"] == "enabled" and enabled["runnable"] is False

            source = root / "synthetic-source"
            source.mkdir()
            (source / "validator.py").write_text("raise RuntimeError('source executed during static planning')\ndef validate_json(document): return {'valid': True}\n")
            (source / "LICENSE").write_text("Synthetic fixture notice.\n")
            (source / "pyproject.toml").write_text("[project]\nname='synthetic'\nversion='1.0.0'\n")
            lock = b"version = 1\nrevision = 3\n"
            (source / "uv.lock").write_bytes(lock)
            draft = command("imports", "create", "--source", str(source), "--name", "Synthetic preparation", "--intent", "Expose synthetic validator", "--json")
            descriptor = {
                "schema_version": 1, "recipe": {"id": enabled["recipe_id"], "version": enabled["version"], "digest": enabled["digest"]},
                "package": {"id": "local.synthetic", "version": "1.0.0", "name": "Synthetic validator"},
                "interface": {"kind": "python-callable", "module": "validator", "function": "validate_json", "tools": [{
                    "name": "validate_json", "description": "Synthetic validator", "input_schema": {"type": "object", "properties": {"document": {"type": "string"}},
                        "required": ["document"], "additionalProperties": False},
                    "output_schema": {"type": "object", "properties": {"valid": {"type": "boolean"}}, "required": ["valid"]}}]},
                "dependencies": {"project": ".", "lock_sha256": hashlib.sha256(lock).hexdigest(), "python_version": "3.12.13", "mcp_version": "1.30.0"},
                "source_license": "LICENSE", "inputs": {"label": "Synthetic", "precision": 2}, "resources": {"memory_mb": 128},
                "validation": [{"id": "synthetic", "tool": "validate_json", "arguments": {"document": "{}"}, "expected": {"valid": True}, "synthetic": True}],
            }
            descriptor_path = root / "descriptor.json"
            descriptor_path.write_text(json.dumps(descriptor))
            planned = command("preparations", "plan", "--import", draft["draft_id"], "--revision", "1", "--descriptor", str(descriptor_path), "--json")
            assert planned["execution_available"] is False
            assert planned["source_revision"] == draft["source"]["revision"]
            assert command("preparations", "list", "--json")["preparations"] == []
            assert command("slots", "list", "--json")["slots"] == []
            grant = command("authorize", "--prepare", "--ttl-seconds", "60", "--json")
            command("--credential-file", grant["credential_file"], "recipes", "list", "--json", expected_error="grant_scope")

            # Editing the original selection cannot change the stored bytes.
            (bundle / "README.md").write_text("Changed original author workspace.\n")
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
            (state / "manager-connection.json").unlink(missing_ok=True)
            process = await start()
            inspected = command("recipes", "inspect", "--id", "local.independent", "--version", "1.0.0", "--json")
            assert inspected["digest"] == enabled["digest"] and inspected["state"] == "enabled"
            assert command("slots", "list", "--json")["slots"] == []
            return {"verified": True, "scope": "static_preparation_foundation", "relocation": True,
                    "development_path_absent": True, "recipe_restart_retention": True,
                    "recipe_digest": enabled["digest"], "owner_only_registry": True,
                    "source_execution": False, "automatic_preparation": False, "installed_slots": 0}
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 10)
            # Only this temporary state's manager sockets are eligible here.
            private = socket_path(state, "manager")
            private.unlink(missing_ok=True)
            try:
                private.parent.rmdir()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(verify(args.host.absolute())), sort_keys=True))
    except Exception:
        print(json.dumps({"verified": False, "error": "preparation_foundation_verification_failed"}))
        raise SystemExit(2) from None
