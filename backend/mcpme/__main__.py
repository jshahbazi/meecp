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
    async with httpx.AsyncClient(base_url=endpoint(connection), timeout=30, follow_redirects=False, trust_env=False,
                                 headers={"Authorization": "Bearer " + connection["token"]}) as client:
        response = await client.request(method, path, json=body)
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value


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
    plan.add_argument("--package", type=Path, required=True)
    plan.add_argument("--name")
    plan.add_argument("--start-policy", choices=["manual", "host"], default="manual")
    plan.add_argument("--start", action="store_true", help="Bind install-and-start authority into this plan.")
    plan.add_argument("--json", action="store_true")
    apply = install.add_parser("apply")
    apply.add_argument("--plan", type=uuid_argument, required=True)
    apply.add_argument("--json", action="store_true")
    operation = commands.add_parser("operations").add_subparsers(dest="action", required=True).add_parser("get")
    operation.add_argument("operation_id", type=uuid_argument)
    operation.add_argument("--json", action="store_true")
    export = commands.add_parser("connections").add_subparsers(dest="action", required=True).add_parser("export")
    export.add_argument("--slot", type=uuid_argument, required=True)
    export.add_argument("--json", action="store_true")
    connect = commands.add_parser("connect")
    connect.add_argument("--slot", type=uuid_argument, required=True)
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
        body = {"package": str(args.package.absolute()), "start_policy": args.start_policy, "start": args.start}
        if args.name is not None:
            body["name"] = args.name
        return await call(state, "POST", "/v1/install/plan", body)
    if args.command == "operations":
        return await call(state, "GET", "/v1/operations/" + args.operation_id)
    if args.command == "connections":
        slot = await call(state, "GET", "/v1/slots/" + args.slot)
        if "error" in slot:
            return slot
        launcher = Path(__file__).resolve().parents[2] / "scripts" / "mcpme"
        if not launcher.is_file():
            launcher = Path(__file__).resolve().parents[2] / "bin" / "mcpme"
        return {"slot_id": args.slot, "endpoint": f"http://127.0.0.1:{slot['public_port']}/mcp",
                "configuration": {"mcpServers": {args.slot: {"command": str(launcher),
                    "args": ["--state-dir", str(state), "connect", "--slot", args.slot]}}}}
    if args.command == "connect":
        from .bridge import run_bridge
        connection = read_private_json(state / "slots" / args.slot / "connection.json")
        if connection.get("slot_id") != args.slot:
            raise ValueError()
        await run_bridge(endpoint(connection), connection["token"])
        return None
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
