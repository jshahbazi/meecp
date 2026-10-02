"""Versioned local-owner CLI. stdout never contains credential values."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import uuid
from urllib.parse import urlsplit

import httpx
from mcpme_local_transport import private_transport, socket_path

from .contracts import PackageError, validate_package
from .private import read_private_json

DEFAULT_STATE = Path.home() / "Library/Application Support/mcpme"


def emit(value: dict):
    print(json.dumps({"api_version": 1, **value}, allow_nan=False))


def uuid_argument(value: str) -> str:
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("expected_slot_or_operation_uuid") from None
    return value


def endpoint(connection: dict) -> str:
    address = connection["endpoint"]
    parsed = urlsplit(address)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_connection")
    return address


async def call(state_dir: Path, method: str, path: str, body: dict | None = None) -> dict:
    connection = read_private_json(state_dir / "manager-connection.json")
    timeout = 300 if path in {"/v1/install/plan", "/v1/migrations/jevk5/plan", "/v1/imports"} or path.endswith("/attach") else 30
    async with httpx.AsyncClient(base_url=endpoint(connection), timeout=timeout, follow_redirects=False, trust_env=False,
                                 transport=private_transport(socket_path(state_dir, "manager")),
                                 headers={"Authorization": "Bearer " + connection["token"]}) as client:
        response = await client.request(method, path, json=body)
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value


def bundled_app() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    for parent in root.parents:
        if parent.suffix == ".app" and (parent / "Contents/MacOS/mcpme").is_file():
            return parent
    return None


async def ensure_host(state: Path):
    async def available():
        try:
            async with asyncio.timeout(2):
                return "error" not in await call(state, "GET", "/v1/capabilities")
        except Exception:
            return False
    if await available():
        return
    app = bundled_app()
    if app is None or state != DEFAULT_STATE:
        raise ValueError("host_unavailable")
    process = await asyncio.create_subprocess_exec("/usr/bin/open", "-g", str(app),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    async with asyncio.timeout(35):
        if await process.wait():
            raise ValueError("host_unavailable")
        while not await available():
            await asyncio.sleep(0.2)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="mcpme")
    cli.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = cli.add_subparsers(dest="command", required=True)
    service = commands.add_parser("serve")
    service.add_argument("--reaper", type=Path, required=True)
    service.add_argument("--parent-pid", type=int)
    caps = commands.add_parser("capabilities")
    caps.add_argument("--json", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("--package", type=Path, required=True)
    validate.add_argument("--json", action="store_true")
    slots = commands.add_parser("slots").add_subparsers(dest="action", required=True)
    slots.add_parser("list").add_argument("--json", action="store_true")
    for name in ["inspect", "start", "stop", "restart"]:
        item = slots.add_parser(name)
        item.add_argument("slot_id", type=uuid_argument)
        item.add_argument("--json", action="store_true")
    install = commands.add_parser("install").add_subparsers(dest="action", required=True)
    plan = install.add_parser("plan")
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--package", type=Path)
    source.add_argument("--import", dest="import_id", type=uuid_argument)
    plan.add_argument("--revision", type=int)
    plan.add_argument("--name")
    plan.add_argument("--start-policy", choices=["manual", "host"], default="manual")
    plan.add_argument("--start", action="store_true", help="Bind install-and-start authority into this plan.")
    plan.add_argument("--json", action="store_true")
    apply = install.add_parser("apply")
    apply.add_argument("--plan", type=uuid_argument, required=True)
    apply.add_argument("--json", action="store_true")
    imports = commands.add_parser("imports").add_subparsers(dest="action", required=True)
    imports.add_parser("list").add_argument("--json", action="store_true")
    create = imports.add_parser("create")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--intent", default="")
    create.add_argument("--document-role", choices=["instructions", "reference"])
    create.add_argument("--file", dest="files", action="append")
    create.add_argument("--start", action="store_true")
    create.add_argument("--start-policy", choices=["manual", "host"], default="manual")
    create.add_argument("--json", action="store_true")
    for action in ("inspect", "revise", "export", "attach", "cancel", "expire"):
        item = imports.add_parser(action)
        item.add_argument("draft_id", type=uuid_argument)
        if action != "inspect":
            item.add_argument("--revision", type=int, required=True)
        if action == "attach":
            item.add_argument("--package", type=Path, required=True)
        if action == "revise":
            item.add_argument("--name")
            item.add_argument("--intent")
            item.add_argument("--document-role", choices=["instructions", "reference"])
            item.add_argument("--start", action=argparse.BooleanOptionalAction, default=None)
            item.add_argument("--start-policy", choices=["manual", "host"])
            item.add_argument("--requirements", type=Path, help="JSON file of nonsecret tool/profile/resource requirements.")
        item.add_argument("--json", action="store_true")
    operation = commands.add_parser("operations").add_subparsers(dest="action", required=True).add_parser("get")
    operation.add_argument("operation_id", type=uuid_argument)
    operation.add_argument("--json", action="store_true")
    export = commands.add_parser("connections").add_subparsers(dest="action", required=True).add_parser("export")
    export.add_argument("--slot", type=uuid_argument, required=True)
    export.add_argument("--json", action="store_true")
    connect = commands.add_parser("connect")
    connect.add_argument("--slot", type=uuid_argument, required=True)
    migrations = commands.add_parser("migrate-jevk5").add_subparsers(dest="action", required=True)
    migration_plan = migrations.add_parser("plan")
    migration_plan.add_argument("--package", type=Path, required=True)
    migration_plan.add_argument("--legacy-app", type=Path, required=True)
    migration_plan.add_argument("--legacy-state", type=Path, default=Path.home() / "Library/Application Support/JevK5")
    migration_plan.add_argument("--legacy-login-disabled", action="store_true",
                                help="Confirm legacy Start at Login is disabled when macOS cannot identify its moved bundle.")
    migration_plan.add_argument("--json", action="store_true")
    for name in ("apply", "inspect", "rollback", "recover"):
        command = migrations.add_parser(name)
        command.add_argument("transfer_id", type=uuid_argument)
        command.add_argument("--json", action="store_true")
    return cli


async def execute(args) -> dict | None:
    state = args.state_dir.absolute()
    if args.command == "serve":
        from .supervisor import serve
        await serve(state, args.reaper, args.parent_pid)
        return None
    if args.command == "validate":
        package = validate_package(args.package.absolute())
        return {"valid": True, "package_digest": package.digest, "package": package.manifest.model_dump()}
    if args.command == "capabilities":
        return await call(state, "GET", "/v1/capabilities")
    if args.command == "slots":
        if args.action == "list":
            return await call(state, "GET", "/v1/slots")
        if args.action == "inspect":
            return await call(state, "GET", "/v1/slots/" + args.slot_id)
        return await call(state, "POST", f"/v1/slots/{args.slot_id}/{args.action}", {})
    if args.command == "install":
        if args.action == "apply":
            return await call(state, "POST", "/v1/install/apply", {"plan_id": args.plan})
        if args.import_id is not None:
            if args.revision is None or args.name is not None or args.start or args.start_policy != "manual":
                return {"error": {"code": "invalid_import_plan"}}
            return await call(state, "POST", "/v1/install/plan", {"import_id": args.import_id, "revision": args.revision})
        if args.revision is not None:
            return {"error": {"code": "invalid_install_plan"}}
        body = {"package": str(args.package.absolute()), "start_policy": args.start_policy, "start": args.start}
        if args.name is not None:
            body["name"] = args.name
        return await call(state, "POST", "/v1/install/plan", body)
    if args.command == "imports":
        if args.action == "list":
            return await call(state, "GET", "/v1/imports")
        if args.action == "create":
            body = {"source": str(args.source.absolute()), "name": args.name, "intent": args.intent,
                    "start": args.start, "start_policy": args.start_policy}
            if args.document_role is not None:
                body["document_role"] = args.document_role
            if args.files is not None:
                body["files"] = args.files
            return await call(state, "POST", "/v1/imports", body)
        route = "/v1/imports/" + args.draft_id
        if args.action == "inspect":
            return await call(state, "GET", route)
        body = {"revision": args.revision}
        if args.action == "attach":
            body["package"] = str(args.package.absolute())
        if args.action == "revise":
            for key in ("name", "intent", "document_role", "start", "start_policy"):
                if getattr(args, key) is not None:
                    body[key] = getattr(args, key)
            if args.requirements is not None:
                if args.requirements.stat().st_size > 65536:
                    return {"error": {"code": "requirements_limit"}}
                body["requirements"] = json.loads(args.requirements.read_text())
        return await call(state, "POST", route + "/" + args.action, body)
    if args.command == "operations":
        return await call(state, "GET", "/v1/operations/" + args.operation_id)
    if args.command == "connections":
        slot = await call(state, "GET", "/v1/slots/" + args.slot)
        if "error" in slot:
            return slot
        launcher = Path(__file__).resolve().parents[2] / "scripts" / "mcpme"
        if not launcher.is_file():
            launcher = Path(__file__).resolve().parents[2] / "bin" / "mcpme"
        if app := bundled_app():
            launcher = app / "Contents/MacOS/mcpme"
        return {"slot_id": args.slot, "endpoint": f"http://127.0.0.1:{slot['public_port']}/mcp",
                "configuration": {"mcpServers": {args.slot: {"command": str(launcher),
                    "args": ["--state-dir", str(state), "connect", "--slot", args.slot]}}}}
    if args.command == "connect":
        from .bridge import run_bridge
        if bundled_app() is not None:
            await ensure_host(state)
        connection = read_private_json(state / "slots" / args.slot / "connection.json")
        if connection.get("slot_id") != args.slot:
            raise ValueError()
        await run_bridge(endpoint(connection), connection["token"], socket_path=socket_path(state, args.slot))
        return None
    if args.command == "migrate-jevk5":
        if args.action == "plan":
            payload = {
                "package": str(args.package.absolute()), "legacy_app": str(args.legacy_app.absolute()),
                "legacy_state": str(args.legacy_state.absolute())}
            if args.legacy_login_disabled:
                payload["legacy_login_disabled"] = True
            return await call(state, "POST", "/v1/migrations/jevk5/plan", payload)
        if args.action == "apply":
            return await call(state, "POST", "/v1/migrations/jevk5/apply", {"transfer_id": args.transfer_id})
        if args.action == "inspect":
            return await call(state, "GET", "/v1/migrations/" + args.transfer_id)
        return await call(state, "POST", "/v1/migrations/" + args.transfer_id + "/" + args.action, {})
    raise ValueError()


def main(argv=None) -> int:
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(execute(args))
        if result is not None:
            emit(result)
            return 1 if "error" in result else 0
    except (Exception, KeyboardInterrupt) as error:
        # No error text is exposed: dependencies may include caller data in it.
        if args.command != "connect":
            emit({"error": {"code": error.code if isinstance(error, PackageError) else "host_unavailable"}})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
