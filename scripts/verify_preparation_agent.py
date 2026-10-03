"""Opt-in real-provider proposal probe; never represents full A5 acceptance.

Run with uv run --locked python scripts/verify_preparation_agent.py --model MODEL
after explicitly configuring OPENAI_API_KEY. Only generated synthetic source is
transferred. The script prints identity/count receipts, never source or output.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from mcpme.preparation_agent_backend import OpenAIResponsesBackend
from mcpme.preparation_agent_contracts import AnalysisRequestV1
from mcpme.preparation_agent_coordinator import AgentAttemptStore, AgentCoordinator
from mcpme.preparation_contracts import PreparationError


async def verify(model):
    secret = os.environ.get("OPENAI_API_KEY")
    if not secret:
        return {"status": "blocked", "reason": "provider_credential_unavailable", "a5_passed": False}
    name = "reconcile_" + secrets.token_hex(4)
    offset = secrets.randbelow(19) + 1
    source = {"unfamiliar_ledger.py": (
        f"def {name}(rows: list[dict]) -> dict:\n"
        "    totals = {}\n"
        "    for row in rows:\n"
        "        category = row['category'].strip().casefold()\n"
        f"        amount = int(row['units']) * {offset}\n"
        "        totals[category] = totals.get(category, 0) + amount\n"
        "    return dict(sorted(totals.items()))\n"
    ).encode()}
    request = AnalysisRequestV1.model_validate({
        "import_id": str(uuid.uuid4()), "revision": 1, "source_revision": "sha256:" + hashlib.sha256(next(iter(source.values()))).hexdigest(),
        "authority_id": str(uuid.uuid4()), "expires_at": int(time.time()) + 300,
        "provider": {"backend": "openai-responses-v1", "model": model, "credential_ref": "probe-key"},
        "selected_files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                           for path, data in source.items()],
        "intent": "Expose the selected function as an MCP tool. Propose exact schemas, a private Python adapter, dependency closure, and synthetic fixtures including duplicate normalized categories and negative units. Identify unresolved package requirements.",
        "permitted_tools": ["read-selected-source", "propose-files"],
        "limits": {"wall_seconds": 240},
    })
    with tempfile.TemporaryDirectory(prefix="mcpme-agent-probe-") as temporary:
        store = AgentAttemptStore(Path(temporary) / "state")
        try:
            # This probe owns a generated source binding; installed-host draft
            # and authority validation must replace this callback in production.
            def validate(candidate):
                if candidate.digest != request.digest:
                    raise PreparationError("agent_probe_binding_mismatch")
            coordinator = AgentCoordinator(store, OpenAIResponsesBackend(lambda _: secret), validate)
            attempt = coordinator.create(request)
            try:
                result = await coordinator.run(attempt["attempt_id"], request, source)
            except PreparationError:
                return {"status": "failed", "reason": "provider_probe_failed", "a5_passed": False}
            record = store.get(attempt["attempt_id"])
            return {"status": "proposal_protocol_passed", "a5_passed": False,
                    "request_digest": request.digest, "proposal_digest": result.digest,
                    "task_count": len(result.tasks), "file_count": sum(len(task.files) for task in result.tasks),
                    "unresolved_count": sum(len(task.unresolved) for task in result.tasks),
                    "calls": len(record["calls"]),
                    "usage": {key: sum(call["usage"][key] for call in record["calls"])
                              for key in ("input_tokens", "output_tokens")},
                    "remaining_gates": ["independent_generated_code_verification", "installed_host_authority_and_candidate_admission"]}
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Explicit provider model identifier; no automatic substitution")
    arguments = parser.parse_args()
    try:
        result = asyncio.run(verify(arguments.model))
    except Exception:
        result = {"status": "failed", "reason": "probe_failed", "a5_passed": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "proposal_protocol_passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
