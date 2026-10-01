"""Check the packaged question contract over both MCP transports and local API."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from verify_app import check, unpack, wait_ready

STATE = "The report has two of its three required sections."
SCORE = {"type": "score", "instructions": "Rate how complete the report is.",
         "criteria": ["No required sections", "Some required sections", "All required sections"]}
INVALID_SCORE = {**SCORE, "criteria": {"0": "No required sections", "1": "Some required sections", "2": "All required sections"}}
OTHER_QUESTIONS = [
    {"type": "noul", "instructions": "Is the report complete?"},
    {"type": "choice", "instructions": "Select the report status.", "criteria": ["incomplete", "complete"]},
    {"type": "choice", "instructions": "Select the report status.", "criteria": {"incomplete": "Missing a required section", "complete": "Contains all required sections"}},
]


async def verify(session):
    await session.initialize()
    tool = next(t for t in (await session.list_tools()).tools if t.name == "jevk5_decide")
    schema = Draft202012Validator(tool.inputSchema)
    arguments = {"state": STATE, "question": SCORE}
    invalid_arguments = {"state": STATE, "question": INVALID_SCORE}
    check(schema.is_valid(arguments), "Tool schema rejected ordered score levels")
    check(not schema.is_valid(invalid_arguments), "Tool schema still allows score mappings")
    check((await session.call_tool("jevk5_decide", invalid_arguments)).isError, "Score mapping reached inference")
    scored = unpack(await session.call_tool("jevk5_decide", arguments))["answer"]
    check(scored["type"] == "score" and list(scored["probabilities"]) == ["0", "1", "2"], "Score level indices or order changed")
    expected = sum(int(k) * v for k, v in scored["probabilities"].items())
    check(abs(scored["score"] - expected) < 1e-12, "Score readout changed")
    for question in OTHER_QUESTIONS:
        payload = {"state": STATE, "question": question}
        check(schema.is_valid(payload), "A supported question shape disappeared from the schema")
        check(unpack(await session.call_tool("jevk5_decide", payload))["answer"]["type"] == question["type"], "A supported question shape stopped working")
    return scored


async def main(args):
    connection = json.loads((Path.home() / "Library/Application Support/JevK5/connection.json").read_text())
    headers = {"Authorization": "Bearer " + connection["token"]}
    async with httpx.AsyncClient(base_url=connection["endpoint"], headers=headers, timeout=150, trust_env=False) as client:
        await wait_ready(client)
        async with streamable_http_client(connection["endpoint"] + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=150)) as session:
                http_answer = await verify(session)
        valid = await client.post("/api/decide", json={"state": STATE, "question": SCORE})
        check(valid.status_code == 200 and valid.json()["answer"] == http_answer, "API and MCP ordered scores differ")
        invalid = await client.post("/api/decide", json={"state": STATE, "question": INVALID_SCORE})
        check(invalid.status_code == 400, "API accepted a score mapping")
    with open(os.devnull, "w") as error_sink:
        params = StdioServerParameters(command=str(args.app.resolve() / "Contents/MacOS/jevk5-mcp"), args=[])
        async with stdio_client(params, errlog=error_sink) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=150)) as session:
                check(await verify(session) == http_answer, "Stdio and HTTP ordered scores differ")
    receipt = {"synthetic_only": True, "score_schema": "ordered array only, 2–16 levels",
               "transports": ["streamable-http", "stdio", "local-api"],
               "score_readout_and_level_order_preserved": True, "score_mappings_rejected": True,
               "noul_and_choice_forms_preserved": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--app", type=Path, default=root / "dist/JevK5.app")
    parser.add_argument("--output", type=Path, default=root / "test-artifacts/score-contract.json")
    asyncio.run(main(parser.parse_args()))
