"""Selection publication keeps the original separately derived input proof."""
import json
import os
from pathlib import Path
import shutil

import pytest

from meecp.preparation_contracts import PreparationError
from meecp.preparation_agent_adapter_store import CODE_PATH
from test_preparation_agent_adapter import adapter_foundation, analyze

pytestmark = pytest.mark.asyncio


def at_checked_return(preparations, monkeypatch, change):
    original = preparations.prepare_descriptor_selection
    observed = []

    def held(*args, **kwargs):
        checked = original(*args, **kwargs)
        descriptor = json.loads(checked.descriptor)
        identity = descriptor["components"][0]["input"]["derivation_id"]
        change(identity)
        observed.append(identity)
        return checked

    monkeypatch.setattr(preparations, "prepare_descriptor_selection", held)
    return observed


async def select(path, preparations, draft, host, plan, job):
    if path == "agent":
        return await host.adopt({"action": "adopt-candidate", "job_id": job["job_id"],
            "proposal_digest": job["proposal_digest"]})
    descriptor, observed = host._candidate(host._job(job["job_id"]))
    candidate, resolution = host._candidate_binding(descriptor, observed)
    return preparations.select_descriptor(draft["draft_id"], draft["revision"],
        descriptor.model_dump(mode="json"), candidate_id=candidate,
        form_digest=plan["plan_digest"], resolution_digest=resolution)


@pytest.mark.parametrize("path", ["agent", "direct"])
@pytest.mark.parametrize("change", ["wrapper", "restored-bytes-and-mtime", "license",
    "interface", "replace-collection", "cancel-derivation"])
async def test_changed_derived_inputs_after_full_validation_cannot_commit_selection(
        adapter_foundation, tmp_path, monkeypatch, path, change):
    imports, _, preparations, draft, derivations, _, _ = adapter_foundation
    host, plan, job = await analyze(adapter_foundation, tmp_path)
    saved = {}

    def mutate(identity):
        saved.update(derivations.get(identity))
        collection = derivations.root / identity
        if change == "cancel-derivation":
            derivations.cancel(identity)
        elif change == "replace-collection":
            retained = tmp_path / "original-derived-collection"
            collection.rename(retained)
            shutil.copytree(retained, collection)
        elif change == "restored-bytes-and-mtime":
            wrapper = collection / CODE_PATH
            details, original = wrapper.stat(), wrapper.read_bytes()
            wrapper.write_bytes(original + b"\n# synthetic changed wrapper\n")
            wrapper.write_bytes(original)
            os.utime(wrapper, ns=(details.st_atime_ns, details.st_mtime_ns))
        else:
            name = {"wrapper": CODE_PATH, "license": "LICENSE", "interface": "adapter.json"}[change]
            with (collection / name).open("ab") as stream:
                stream.write(b"\n# synthetic selection boundary change\n")

    held = at_checked_return(preparations, monkeypatch, mutate)
    try:
        with pytest.raises(PreparationError):
            await select(path, preparations, draft, host, plan, job)
        assert len(held) == 1
        current = imports.inspect(draft["draft_id"])
        assert current["revision"] == draft["revision"] == 1
        assert current["source"] == draft["source"] and current["attachment"] is None
        assert preparations.selected_descriptor(draft["draft_id"]) is None
        assert preparations.list() == [] and len(job["calls"]) == 4
        after = derivations.get(held[0])
        assert after["source_metadata"] == saved["source_metadata"]
        assert after["derived_metadata"] == saved["derived_metadata"]
    finally:
        await host.close()


@pytest.mark.parametrize("path", ["agent", "direct"])
async def test_unchanged_selection_uses_metadata_inside_commit(
        adapter_foundation, tmp_path, monkeypatch, path):
    from meecp import preparation_agent_adapter_store as module
    imports, _, preparations, draft, _, _, _ = adapter_foundation
    host, plan, job = await analyze(adapter_foundation, tmp_path)
    original = module.read_snapshot

    def check_reads(*args, **kwargs):
        assert not imports._connection.in_transaction, "selection transaction read source bytes"
        return original(*args, **kwargs)

    held = at_checked_return(preparations, monkeypatch,
        lambda identity: monkeypatch.setattr(module, "read_snapshot", check_reads))
    try:
        await select(path, preparations, draft, host, plan, job)
        current = imports.inspect(draft["draft_id"])
        assert len(held) == 1 and current["revision"] == 2
        assert current["source"] == draft["source"] and current["attachment"] is None
        assert preparations.selected_descriptor(draft["draft_id"])["revision"] == 2
    finally:
        await host.close()


@pytest.mark.parametrize("result", [False, None, 1, "true", "coroutine", "async-function"])
async def test_selection_rejects_non_synchronous_family_authority(
        adapter_foundation, tmp_path, monkeypatch, result):
    imports, _, preparations, draft, _, family, _ = adapter_foundation
    host, plan, job = await analyze(adapter_foundation, tmp_path)
    calls = []

    async def async_checker(*args):
        calls.append("async-ran")
        return True

    def replace_checker(identity):
        checker = (async_checker if result == "async-function" else
            lambda *args: async_checker() if result == "coroutine" else result)
        monkeypatch.setattr(family, "check_current", checker)

    held = at_checked_return(preparations, monkeypatch, replace_checker)
    try:
        with pytest.raises(PreparationError, match="agent_candidate_rejected"):
            await select("agent", preparations, draft, host, plan, job)
        assert len(held) == 1 and not calls
        assert imports.inspect(draft["draft_id"])["revision"] == 1
        assert preparations.selected_descriptor(draft["draft_id"]) is None
        assert host.get(job["job_id"])["error_code"] == "agent_candidate_rejected"
    finally:
        await host.close()
