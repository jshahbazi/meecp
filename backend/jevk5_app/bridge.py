"""MCP stdio adapter: open the menu app, reuse its one resident model."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.types import ToolAnnotations

from .contracts import Question, ServiceError, validate_decision
from .server import DECIDE_DESCRIPTION, SafeMCP, preserve_literal_evidence
from .state import ENDPOINT
from meecp_local_transport import private_transport, socket_path


class Bridge:
    def __init__(self, state_dir: Path, app_path: Path):
        self.state_dir, self.app_path = state_dir, app_path
        self.token: str | None = None

    def connection(self):
        path = self.state_dir / "connection.json"
        try:
            if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise ValueError()
            value = json.loads(path.read_text())
            if value.get("endpoint") != ENDPOINT or not isinstance(value.get("token"), str):
                raise ValueError()
            self.token = value["token"]
        except Exception:
            raise ServiceError("app_unavailable", "The menu bar app connection is unavailable.") from None

    async def request(self, path: str, payload: dict | None = None):
        self.connection()
        try:
            async with httpx.AsyncClient(timeout=180 if path == "/api/decide" else 2, trust_env=False,
                    follow_redirects=False, transport=private_transport(socket_path(self.state_dir, "jevk5"))) as client:
                headers = {"Authorization": "Bearer " + self.token}
                response = await (client.get(ENDPOINT + path, headers=headers) if payload is None else
                                  client.post(ENDPOINT + path, headers=headers, json=payload))
            if response.status_code >= 400:
                # Trust only known, content-free messages from our own authenticated service.
                codes = {400: ("invalid_decision", "The decision request was rejected."),
                         409: ("paused", "The model is paused. Resume it from the menu bar."),
                         429: ("queue_full", "The local decision queue is full. Retry shortly.")}
                code, message = codes.get(response.status_code, ("app_unavailable", "The local model is unavailable."))
                raise ServiceError(code, message, response.status_code)
            return response.json()
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("app_unavailable", "The menu bar app connection is unavailable.") from None

    async def ensure_app(self):
        try:
            await self.request("/api/status")
            return
        except ServiceError:
            pass
        if not self.app_path.is_dir() or self.app_path.suffix != ".app":
            raise ServiceError("app_unavailable", "The menu bar app bundle is unavailable.")
        process = await asyncio.create_subprocess_exec("/usr/bin/open", "-a", str(self.app_path),
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await process.wait() != 0:
            raise ServiceError("app_unavailable", "The menu bar app could not be opened.")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                await self.request("/api/status")
                return
            except ServiceError:
                await asyncio.sleep(0.2)
        raise ServiceError("app_unavailable", "The menu bar app did not become available.")


async def run_bridge(state_dir: Path, app_path: Path):
    bridge = Bridge(state_dir, app_path)
    await bridge.ensure_app()
    mcp = SafeMCP("JevK5", instructions=DECIDE_DESCRIPTION, log_level="CRITICAL")

    @mcp.tool(description=DECIDE_DESCRIPTION, annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def jevk5_decide(state: str | dict[str, Any] | list[Any], question: Question) -> dict[str, Any]:
        evidence, validated = validate_decision(state, question)
        return await bridge.request("/api/decide", {"state": evidence, "question": validated})

    @mcp.tool(description="Report local model readiness, pause state, model identity, aggregate counts, and MCP endpoint. Contains no evidence or decisions.",
              annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def jevk5_status() -> dict[str, Any]:
        return await bridge.request("/api/status")

    preserve_literal_evidence(mcp)

    await mcp.run_stdio_async()
