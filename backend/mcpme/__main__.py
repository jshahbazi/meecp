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
from .connections import connection_names
from .grants import GrantError
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


async def call(state_dir: Path, method: str, path: str, body: dict | None = None, *,
               credential_file: Path | None = None) -> dict:
    connection = read_private_json(state_dir / "manager-connection.json")
    if credential_file is None:
        token = connection["token"]
    else:
        from .grants import read_credential
        # Reopen for every call; a removed or unsafe file never selects owner authority.
        token = read_credential(credential_file)["token"]
    timeout = 300 if path in {"/v1/install/plan", "/v1/migrations/jevk5/plan", "/v1/imports"} or path.endswith("/attach") else 30
    async with httpx.AsyncClient(base_url=endpoint(connection), timeout=timeout, follow_redirects=False, trust_env=False,
                                 transport=private_transport(socket_path(state_dir, "manager")),
                                 headers={"Authorization": "Bearer " + token}) as client:
        async with client.stream(method, path, json=body) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise ValueError("response_limit")
            from .policy import load_json
            value = load_json(bytes(data))
        if not isinstance(value, dict):
            raise ValueError()
        return value


def bundled_app() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    for parent in root.parents:
        if parent.suffix == ".app" and (parent / "Contents/MacOS/mcpme").is_file():
            return parent
    return None


def cli_launcher() -> Path:
    if app := bundled_app():
        return app / "Contents/MacOS/mcpme"
    root = Path(__file__).resolve().parents[2]
    source = root / "scripts/mcpme"
    return source if source.is_file() else root / "bin/mcpme"

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
    cli.add_argument("--credential-file", type=Path,
                     help="Use this scoped management credential, without owner fallback.")
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
    for name in ["inspect", "start", "stop", "restart", "remove"]:
        item = slots.add_parser(name)
        item.add_argument("slot_id", type=uuid_argument)
        item.add_argument("--json", action="store_true")
    configure = slots.add_parser("configure")
    configure.add_argument("slot_id", type=uuid_argument)
    configure.add_argument("--start-policy", choices=["manual", "host"], required=True)
    configure.add_argument("--json", action="store_true")
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
    inspect = install.add_parser("inspect")
    inspect.add_argument("--plan", type=uuid_argument, required=True)
    inspect.add_argument("--json", action="store_true")
    authorize = commands.add_parser("authorize")
    scope = authorize.add_mutually_exclusive_group(required=True)
    scope.add_argument("--plan", type=uuid_argument)
    scope.add_argument("--read-only", action="store_true")
    scope.add_argument("--prepare", action="store_true")
    scope.add_argument("--slot", type=uuid_argument, action="append", dest="slot_ids")
    authorize.add_argument("--action", choices=["start", "stop", "restart"], action="append", dest="actions")
    authorize.add_argument("--ttl-seconds", type=int)
    authorize.add_argument("--json", action="store_true")
    grants = commands.add_parser("grants").add_subparsers(dest="action", required=True)
    grants.add_parser("list").add_argument("--json", action="store_true")
    for name in ("inspect", "revoke"):
        item = grants.add_parser(name)
        item.add_argument("grant_id", type=uuid_argument)
        item.add_argument("--json", action="store_true")
    management = commands.add_parser("management").add_subparsers(dest="action", required=True)
    management.add_parser("connect")
    management.add_parser("export").add_argument("--json", action="store_true")
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
    recipes = commands.add_parser("recipes").add_subparsers(dest="action", required=True)
    scaffold = recipes.add_parser("scaffold")
    scaffold.add_argument("--output", type=Path, required=True)
    scaffold.add_argument("--id", dest="recipe_id", required=True)
    scaffold.add_argument("--version", default="1.0.0")
    scaffold.add_argument("--component", choices=["python-function-v1", "existing-mcp-v1"], default="python-function-v1")
    scaffold.add_argument("--json", action="store_true")
    for action in ("check", "lock", "add"):
        item = recipes.add_parser(action)
        item.add_argument("--path", type=Path, required=True)
        item.add_argument("--json", action="store_true")
    recipes.add_parser("list").add_argument("--json", action="store_true")
    for action in ("inspect", "enable", "disable", "revoke", "remove", "deprecate"):
        item = recipes.add_parser(action)
        item.add_argument("--id", dest="recipe_id", required=True)
        item.add_argument("--version", required=True)
        item.add_argument("--json", action="store_true")
    preparations = commands.add_parser("preparations").add_subparsers(dest="action", required=True)
    preparation_plan = preparations.add_parser("plan")
    preparation_plan.add_argument("--import", dest="import_id", type=uuid_argument, required=True)
    preparation_plan.add_argument("--revision", type=int, required=True)
    preparation_plan.add_argument("--descriptor", type=Path, required=True)
    preparation_plan.add_argument("--json", action="store_true")
    preparations.add_parser("list").add_argument("--json", action="store_true")
    for action in ("get", "cancel"):
        item = preparations.add_parser(action)
        item.add_argument("job_id", type=uuid_argument)
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
    credential = getattr(args, "credential_file", None)
    if credential is not None:
        credential = credential.absolute()
    async def api(method, path, body=None):
        if credential is None:
            return await call(state, method, path, body)
        return await call(state, method, path, body, credential_file=credential)

    if credential is not None and args.command in {"serve", "connect", "connections"}:
        return {"error": {"code": "management_credential_not_invocation"}}
    if args.command == "management":
        if credential is None:
            return {"error": {"code": "management_credential_required"}}
        from .grants import read_credential
        grant = read_credential(credential)
        if args.action == "connect":
            from .management import run_management
            await run_management(state, credential)
            return None
        return {"grant_id": grant["grant_id"], "configuration": {"mcpServers": {
            "mcpme-management": {"command": str(cli_launcher()), "args": ["--state-dir", str(state),
                "--credential-file", str(credential), "management", "connect"]}}}}
    if args.command == "authorize":
        if args.plan is not None:
            if args.actions or args.ttl_seconds not in {None, 900}:
                return {"error": {"code": "invalid_grant_request"}}
            body = {"mode": "install", "plan_id": args.plan, "ttl_seconds": 900}
        elif args.slot_ids is not None:
            if not args.actions:
                return {"error": {"code": "invalid_grant_request"}}
            body = {"mode": "lifecycle", "slot_ids": args.slot_ids, "actions": args.actions,
                    "ttl_seconds": args.ttl_seconds if args.ttl_seconds is not None else 3600}
        else:
            if args.actions:
                return {"error": {"code": "invalid_grant_request"}}
            body = {"mode": "read" if args.read_only else "prepare",
                    "ttl_seconds": args.ttl_seconds if args.ttl_seconds is not None else 3600}
        return await api("POST", "/v1/grants", body)
    if args.command == "grants":
        if args.action == "list":
            return await api("GET", "/v1/grants")
        route = "/v1/grants/" + args.grant_id
        return await api("GET", route) if args.action == "inspect" else await api("POST", route + "/revoke", {})
    if args.command == "serve":
        from .supervisor import serve
        await serve(state, args.reaper, args.parent_pid)
        return None
    if args.command == "validate":
        package = validate_package(args.package.absolute())
        return {"valid": True, "package_digest": package.digest, "package": package.manifest.model_dump()}
    if args.command == "recipes":
        if args.action in {"scaffold", "check", "lock"}:
            from .recipe_sdk import check_recipe, relock_recipe, scaffold_recipe
            if args.action == "scaffold":
                return scaffold_recipe(args.output, args.recipe_id, args.version, args.component)
            if args.action == "lock":
                return relock_recipe(args.path.absolute())
            return check_recipe(args.path.absolute())
        if args.action == "list":
            return await api("GET", "/v1/recipes")
        if args.action == "add":
            return await api("POST", "/v1/recipes", {"path": str(args.path.absolute())})
        return await api("POST", "/v1/recipes/" + args.action,
                         {"recipe_id": args.recipe_id, "version": args.version})
    if args.command == "preparations":
        if args.action == "plan":
            from .recipe_sdk import read_contract_file
            if args.revision < 1:
                from .preparation_contracts import PreparationError
                raise PreparationError("invalid_import_revision")
            descriptor = read_contract_file(args.descriptor)
            return await api("POST", "/v1/preparations/plan",
                             {"import_id": args.import_id, "revision": args.revision, "descriptor": descriptor})
        if args.action == "list":
            return await api("GET", "/v1/preparations")
        route = "/v1/preparations/" + args.job_id
        return await api("GET", route) if args.action == "get" else await api("POST", route + "/cancel", {})
    if args.command == "capabilities":
        return await api("GET", "/v1/capabilities")
    if args.command == "slots":
        if args.action == "list":
            return await api("GET", "/v1/slots")
        if args.action == "inspect":
            return await api("GET", "/v1/slots/" + args.slot_id)
        if args.action == "configure":
            return await api("PATCH", "/v1/slots/" + args.slot_id, {"start_policy": args.start_policy})
        return await api("POST", f"/v1/slots/{args.slot_id}/{args.action}", {})
    if args.command == "install":
        if args.action == "inspect":
            return await api("GET", "/v1/plans/" + args.plan)
        if args.action == "apply":
            return await api("POST", "/v1/install/apply", {"plan_id": args.plan})
        if args.import_id is not None:
            if args.revision is None or args.name is not None or args.start or args.start_policy != "manual":
                return {"error": {"code": "invalid_import_plan"}}
            return await api("POST", "/v1/install/plan", {"import_id": args.import_id, "revision": args.revision})
        if args.revision is not None:
            return {"error": {"code": "invalid_install_plan"}}
        body = {"package": str(args.package.absolute()), "start_policy": args.start_policy, "start": args.start}
        if args.name is not None:
            body["name"] = args.name
        return await api("POST", "/v1/install/plan", body)
    if args.command == "imports":
        if args.action == "list":
            return await api("GET", "/v1/imports")
        if args.action == "create":
            body = {"source": str(args.source.absolute()), "name": args.name, "intent": args.intent,
                    "start": args.start, "start_policy": args.start_policy}
            if args.document_role is not None:
                body["document_role"] = args.document_role
            if args.files is not None:
                body["files"] = args.files
            return await api("POST", "/v1/imports", body)
        route = "/v1/imports/" + args.draft_id
        if args.action == "inspect":
            return await api("GET", route)
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
        return await api("POST", route + "/" + args.action, body)
    if args.command == "operations":
        return await api("GET", "/v1/operations/" + args.operation_id)
    if args.command == "connections":
        slot = await api("GET", "/v1/slots/" + args.slot)
        if "error" in slot:
            return slot
        launcher = cli_launcher()
        name = slot.get("connection_name")
        if name is None:
            # Older running hosts omit the additive snapshot field. Resolve their
            # full installed-app list so normalization collisions remain distinct.
            listing = await api("GET", "/v1/slots")
            if "error" in listing:
                return listing
            name = connection_names((item["slot_id"], item["name"]) for item in listing["slots"])[args.slot]
        return {"slot_id": args.slot, "connection_name": name,
                "endpoint": f"http://127.0.0.1:{slot['public_port']}/mcp",
                "configuration": {"mcpServers": {name: {"command": str(launcher),
                    "args": ["--state-dir", str(state), "connect", "--slot", args.slot]}}}}
    if args.command == "connect":
        from .bridge import run_bridge
        if bundled_app() is not None:
            await ensure_host(state)
        connection = read_private_json(state / "slots" / args.slot / "connection.json")
        if connection.get("slot_id") != args.slot:
            raise ValueError()
        from .limits import DEFAULT_REQUEST_BYTES
        await run_bridge(endpoint(connection), connection["token"], socket_path=socket_path(state, args.slot),
                         max_request_bytes=connection.get("max_request_bytes", DEFAULT_REQUEST_BYTES))
        return None
    if args.command == "migrate-jevk5":
        if args.action == "plan":
            payload = {
                "package": str(args.package.absolute()), "legacy_app": str(args.legacy_app.absolute()),
                "legacy_state": str(args.legacy_state.absolute())}
            if args.legacy_login_disabled:
                payload["legacy_login_disabled"] = True
            return await api("POST", "/v1/migrations/jevk5/plan", payload)
        if args.action == "apply":
            return await api("POST", "/v1/migrations/jevk5/apply", {"transfer_id": args.transfer_id})
        if args.action == "inspect":
            return await api("GET", "/v1/migrations/" + args.transfer_id)
        return await api("POST", "/v1/migrations/" + args.transfer_id + "/" + args.action, {})
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
        if args.command != "connect" and not (args.command == "management" and args.action == "connect"):
            from .preparation_contracts import PreparationError
            emit({"error": {"code": error.code if isinstance(error, (PackageError, GrantError, PreparationError)) else "host_unavailable"}})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
