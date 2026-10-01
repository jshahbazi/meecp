"""Synthetic end-to-end checks; record timings/counts, never prompts or answers."""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import logging
import math
import os
from pathlib import Path
import statistics
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

logging.disable(logging.CRITICAL)
STATE = "Refunds require a receipt and a purchase within 30 days. The customer bought 12 days ago and has no receipt."
QUESTION = {"type": "noul", "instructions": "Is a refund permitted under the policy?"}


def check(condition, description):
    if not condition:
        raise RuntimeError(description)


def unpack(result):
    check(not result.isError, "MCP decision returned an error")
    if result.structuredContent:
        return result.structuredContent
    return json.loads(next(c.text for c in result.content if c.type == "text"))


async def wait_ready(client):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        try:
            response = await client.get("/api/status")
        except httpx.ConnectError:
            await asyncio.sleep(0.2)
            continue
        check(response.status_code == 200, "Authenticated status failed")
        status = response.json()
        if status["ready"]:
            return status
        check(status["state"] != "error", "Model startup failed")
        await asyncio.sleep(0.2)
    raise RuntimeError("Model readiness timed out")


async def main(args):
    deadline = time.monotonic() + 30
    while not (args.state_dir / "connection.json").exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    connection = json.loads((args.state_dir / "connection.json").read_text())
    endpoint = connection["endpoint"]
    headers = {"Authorization": "Bearer " + connection["token"]}
    receipt = {"app": str(args.app), "synthetic_only": True, "checks": [], "cache": {}, "transports": []}
    async with httpx.AsyncClient(base_url=endpoint, headers=headers, timeout=150, trust_env=False) as client:
        initial = await wait_ready(client)
        receipt["model"] = initial["model"]
        # Every validation check records its outcome, not request or response content.
        for name, kwargs, expected in [
            ("authentication", {"headers": {"Authorization": ""}}, 401),
            ("origin", {"headers": {"Origin": "https://untrusted.invalid"}}, 403),
            ("host", {"headers": {"Host": "untrusted.invalid"}}, 403),
        ]:
            response = await client.get("/api/status", **kwargs)
            check(response.status_code == expected, name + " validation failed")
            receipt["checks"].append(name)
        for name, kwargs, expected in [
            ("malformed_json", {"content": b"{"}, 400),
            ("body_limit", {"content": b" " * (1024 * 1024 + 1)}, 413),
            ("criteria_limit", {"json": {"state": STATE, "question": {"type": "choice", "instructions": "Choose.", "criteria": [str(i) for i in range(17)]}}}, 400),
            ("context_limit", {"json": {"state": "alpha " * 9000, "question": QUESTION}}, 400),
        ]:
            response = await client.post("/api/decide", **kwargs)
            check(response.status_code == expected, name + " validation failed")
            receipt["checks"].append(name)
        async with streamablehttp_client(endpoint + "/mcp", headers=headers, timeout=timedelta(seconds=150)) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=150)) as session:
                await session.initialize()
                tools = await session.list_tools()
                check({t.name for t in tools.tools} == {"jevk5_decide", "jevk5_status"}, "Unexpected MCP tool surface")
                check(all(t.annotations and t.annotations.readOnlyHint and not t.annotations.destructiveHint and not t.annotations.openWorldHint for t in tools.tools), "Incorrect local decision tool annotations")
                first = unpack(await session.call_tool("jevk5_decide", {"state": STATE, "question": QUESTION}))
                check(first["cache"]["hit"] is False, "First decision reused a changed prompt")
                repeats = []
                max_delta = 0
                for _ in range(10):
                    repeated = unpack(await session.call_tool("jevk5_decide", {"state": STATE, "question": QUESTION}))
                    check(repeated["cache"]["hit"] is True, "Identical prompt did not reuse cache")
                    check(repeated["input_tokens"] == first["input_tokens"], "Cache changed logical token count")
                    max_delta = max(max_delta, abs(repeated["answer"]["noul"] - first["answer"]["noul"]))
                    repeats.append(repeated["latency_ms"])
                check(max_delta == 0, "Repeated decision probability changed")
                changed = unpack(await session.call_tool("jevk5_decide", {"state": STATE + " A receipt was subsequently supplied.", "question": QUESTION}))
                check(changed["cache"]["hit"] is False, "Changed evidence reused cache")
                receipt["cache"] = {"fresh_ms": first["latency_ms"], "repeat_median_ms": statistics.median(repeats),
                                    "repeats": len(repeats), "max_probability_delta": max_delta, "input_tokens": first["input_tokens"]}
                receipt["transports"].append("streamable-http")
                receipt["checks"].extend(["initialize", "list_tools", "exact_repeat_cache", "changed_input"])
                literal = "{\"literal\":\"evidence\"}"
                literal_api = (await client.post("/api/decide", json={"state": literal, "question": QUESTION})).json()
                literal_mcp = unpack(await session.call_tool("jevk5_decide", {"state": literal, "question": QUESTION}))
                check(literal_mcp["cache"]["hit"] is True and literal_mcp["answer"] == literal_api["answer"], "MCP changed a literal JSON-looking evidence string")
                receipt["checks"].append("literal_string_transport_parity")
        # Typed readout and concurrent clients use the same model process.
        choice = {"type": "choice", "instructions": "Select the parcel outcome.", "criteria": {"delivered": "Reached the intended address", "misdelivered": "Reached another address", "unknown": "Delivery destination is uncertain"}}
        score = {"type": "score", "instructions": "Rate how complete the evidence is.", "criteria": ["No evidence", "Partial evidence", "Complete evidence"]}
        responses = await asyncio.gather(client.post("/api/decide", json={"state": "Parcel delivered to No. 17; the customer lives at No. 71.", "question": choice}),
                                         client.post("/api/decide", json={"state": "The report has two of its three required sections.", "question": score}))
        for response in responses:
            check(response.status_code == 200, "Concurrent decision failed")
            answer = response.json()["answer"]
            check(math.isclose(sum(answer["probabilities"].values()), 1, abs_tol=1e-12), "Typed probabilities not normalized")
        current = (await client.get("/api/status")).json()
        check(current["llama_pid"] == initial["llama_pid"], "Concurrent clients changed the model process")
        receipt["checks"].extend(["choice", "score", "concurrency_single_model"])
        with open(os.devnull, "w") as error_sink:
            params = StdioServerParameters(command=str(args.app / "Contents/MacOS/jevk5-mcp"), args=[])
            async with stdio_client(params, errlog=error_sink) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=150)) as session:
                    await session.initialize()
                    stdio_tools = (await session.list_tools()).tools
                    check(len(stdio_tools) == 2 and all(t.annotations and t.annotations.readOnlyHint and not t.annotations.openWorldHint for t in stdio_tools), "Stdio tools or annotations unavailable")
                    stdio_status = unpack(await session.call_tool("jevk5_status"))
                    check(stdio_status["backend_pid"] == initial["backend_pid"], "Stdio created another backend")
                    unpack(await session.call_tool("jevk5_decide", {"state": STATE, "question": QUESTION}))
                    receipt["transports"].append("stdio")
        paused = await client.post("/api/control", json={"action": "pause"})
        check(paused.status_code == 200 and paused.json()["paused"] and paused.json()["llama_pid"] is None, "Pause failed to unload the model")
        check((await client.post("/api/decide", json={"state": STATE, "question": QUESTION})).status_code == 409, "Paused decision did not fail")
        check((await client.post("/api/control", json={"action": "resume"})).status_code == 200, "Resume failed")
        resumed = await wait_ready(client)
        check(resumed["llama_pid"] != initial["llama_pid"], "Resume did not launch a fresh model")
        after_resume = (await client.post("/api/decide", json={"state": STATE, "question": QUESTION})).json()
        check(after_resume["cache"]["hit"] is False, "Resume retained a stale cache identity")
        receipt["checks"].extend(["pause_unloads", "paused_rejected", "resume_fresh_cache"])
        receipt["final_counts"] = resumed["counts"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"passed": len(receipt["checks"]), "transports": receipt["transports"], "cache": receipt["cache"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=Path(__file__).resolve().parents[1] / "dist/JevK5.app")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / "Library/Application Support/JevK5")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "test-artifacts/integration.json")
    asyncio.run(main(parser.parse_args()))
