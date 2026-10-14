"""Ephemeral live setup input, exact context, owner scope and bounded recovery."""
import asyncio
import json
from pathlib import Path
import shutil
import time
import uuid

import httpx
import pytest

from meecp.grants import read_credential
from meecp.pi_conversation import (
    MAX_HISTORY_BYTES, MAX_HISTORY_EVENTS, MAX_QUEUED_MESSAGES, MAX_TEXT_BYTES,
    PiConversation, PiConversationError,
)
from meecp.pi_setup import PiSetupError
from meecp.supervisor import management_app
from test_meecp_imports import source_folder
import test_pi_setup as setup_fixtures
import test_preparation_processes as process_fixtures

enable_fake_work = setup_fixtures.enable_fake_work
host = setup_fixtures.host
group_reaper = process_fixtures.group_reaper


def context(job):
    return {key: job[key] for key in ("import_id", "revision", "source_revision")}


def message(job, text="Use the requested engine.", identity=None):
    return {**context(job), "message_id": identity or str(uuid.uuid4()), "text": text}


async def running(host, tmp_path, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    enable_fake_work(host.pi_setup, monkeypatch, started=started, release=release)
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    job = await host.pi_setup.start({"import_id": draft["draft_id"], "revision": draft["revision"]})
    await asyncio.wait_for(started.wait(), 3)
    return job, release


def test_ring_is_bounded_and_cursor_reports_eviction_and_unavailable():
    conversation, job_id = PiConversation(), str(uuid.uuid4())
    conversation.begin(job_id)
    for _ in range(MAX_HISTORY_EVENTS + 2):
        conversation.append(job_id, "assistant", "x" * MAX_TEXT_BYTES)
    response = conversation.read(job_id, 0, can_message=True)
    assert response["reset"] and response["cursor"] == MAX_HISTORY_EVENTS + 2
    assert len(response["events"]) <= MAX_HISTORY_EVENTS
    assert sum(len(event["text"].encode()) for event in response["events"]) <= MAX_HISTORY_BYTES
    assert not conversation.read(job_id, response["cursor"], can_message=True)["events"]
    assert conversation.read(job_id, response["cursor"] + 1, can_message=True)["reset"]
    response["events"][0]["text"] = "mutated"
    assert conversation.read(job_id, 0, can_message=True)["events"][0]["text"] != "mutated"
    conversation.close()
    assert conversation.read(job_id, 3, can_message=True) == {
        "job_id": job_id, "events": [], "cursor": 0, "first_sequence": 0,
        "reset": True, "available": False, "can_message": False,
    }


async def test_terminal_timer_reaps_without_a_reader(monkeypatch):
    monkeypatch.setattr("meecp.pi_conversation.TERMINAL_TTL_SECONDS", .01)
    conversation, job_id = PiConversation(), str(uuid.uuid4())
    conversation.begin(job_id)
    conversation.append(job_id, "assistant", "Verified.")
    conversation.finish(job_id)
    assert not conversation.read(job_id, 0, can_message=True)["can_message"]
    await asyncio.sleep(.03)
    assert not conversation.sessions


@pytest.mark.parametrize("text", ["", " \n", "private\0value", "é" * (MAX_TEXT_BYTES // 2 + 1), "\ud800"])
def test_invalid_input_never_echoes_content(text):
    conversation, job_id = PiConversation(), str(uuid.uuid4())
    conversation.begin(job_id)
    with pytest.raises(PiConversationError, match="^pi_message_invalid$"):
        conversation.enqueue(job_id, str(uuid.uuid4()), text)
    assert not conversation.read(job_id, 0, can_message=True)["events"]


async def test_messages_are_idempotent_bounded_private_and_cleared_on_cancel(host, tmp_path, monkeypatch):
    job, _ = await running(host, tmp_path, monkeypatch)
    manager, payload = host.pi_setup, message(job)
    first = asyncio.create_task(manager.messages(job["id"], payload))
    duplicate = asyncio.create_task(manager.messages(job["id"], payload))
    await asyncio.sleep(0)
    session = manager.conversation.active(job["id"])
    assert session.inbox.qsize() == 1 and not first.done() and not duplicate.done()
    with pytest.raises(PiConversationError, match="^pi_message_changed$"):
        await manager.messages(job["id"], {**payload, "text": "Changed request"})
    pending = []
    for _ in range(MAX_QUEUED_MESSAGES - 1):
        pending.append(asyncio.create_task(manager.messages(job["id"], message(job))))
    await asyncio.sleep(0)
    with pytest.raises(PiConversationError, match="^pi_message_busy$"):
        await manager.messages(job["id"], message(job))
    response = manager.conversation_events(job["id"], {**context(job), "after": 0})
    assert not response["events"] and not response["can_message"]
    manager.conversation.sent(job["id"], payload["message_id"])
    manager.conversation.acknowledge(job["id"], payload["message_id"])
    assert (await first)["duplicate"] is False and (await duplicate)["duplicate"] is True
    assert len(manager.conversation_events(job["id"], {**context(job), "after": 0})["events"]) == 1
    assert payload["text"] not in json.dumps(manager.get(job["id"]))
    assert payload["text"] not in (manager.jobs_root / (job["id"] + ".json")).read_text()
    await manager.cancel(job["id"])
    outcomes = await asyncio.gather(*pending, return_exceptions=True)
    assert all(isinstance(value, PiConversationError) and value.code == "pi_message_unavailable" for value in outcomes)
    assert not manager.conversation_events(job["id"], {**context(job), "after": 8})["available"]
    with pytest.raises(PiConversationError, match="^pi_message_unavailable$"):
        await manager.messages(job["id"], message(job))
    await manager.close()


@pytest.mark.parametrize("kind", ["text", "secret"])
async def test_question_route_is_authoritative_and_secret_never_becomes_event(host, tmp_path, monkeypatch, kind):
    job, _ = await running(host, tmp_path, monkeypatch)
    manager = host.pi_setup
    question = {"kind": kind, "question": "Which account?"}
    if kind == "secret":
        question["credential_name"] = "ACCOUNT_KEY"
    task = asyncio.create_task(manager._ask(job, question))
    await asyncio.sleep(0)
    pending = manager.public_job(job)["question"]
    with pytest.raises(PiConversationError, match="^pi_question_pending$"):
        await manager.messages(job["id"], message(job, "private-credential-sentinel"))
    assert not manager.conversation_events(job["id"], {**context(job), "after": 0})["can_message"]
    manager.answer(job["id"], {"question_id": pending["id"], "import_id": job["import_id"],
        "revision": job["revision"], "value": "synthetic-answer-sentinel"})
    await task
    events = manager.conversation_events(job["id"], {**context(job), "after": 0})["events"]
    assert len(events) == (1 if kind == "text" else 0)
    if events:
        assert events[0]["role"] == "user" and events[0]["message_id"] == pending["id"]
    assert "synthetic-answer-sentinel" not in json.dumps(manager.get(job["id"]))
    await manager.close()
    assert not manager.conversation.sessions


async def test_stale_draft_or_job_context_cannot_read_or_steer(host, tmp_path, monkeypatch):
    job, _ = await running(host, tmp_path, monkeypatch)
    manager = host.pi_setup
    for key, value in (("import_id", str(uuid.uuid4())), ("revision", job["revision"] + 1),
                       ("source_revision", "changed-source")):
        with pytest.raises(PiSetupError, match="^pi_source_changed$"):
            await manager.messages(job["id"], {**message(job), key: value})
    host.imports.revise(job["import_id"], job["revision"], intent="A different app")
    from meecp.imports import ImportError
    with pytest.raises(ImportError):
        manager.conversation_events(job["id"], {**context(job), "after": 0})
    await manager.close()


async def test_worker_terminal_race_rejects_unacknowledged_input(host, tmp_path, monkeypatch):
    job, _ = await running(host, tmp_path, monkeypatch)
    manager = host.pi_setup
    pending = asyncio.create_task(manager.messages(job["id"], message(job)))
    await asyncio.sleep(0)
    manager.conversation.stop_input(job["id"])
    with pytest.raises(PiConversationError, match="^pi_message_unavailable$"):
        await pending
    response = manager.conversation_events(job["id"], {**context(job), "after": 0})
    assert not response["events"] and not response["can_message"]
    with pytest.raises(PiConversationError, match="^pi_message_unavailable$"):
        await manager.messages(job["id"], message(job))
    await manager.close()


async def test_live_routes_require_owner_host_and_origin_and_exact_query(host, tmp_path, monkeypatch):
    job, _ = await running(host, tmp_path, monkeypatch)
    port, owner = 19886, "synthetic-owner"
    grant = host.grants.issue({"mode": "prepare", "ttl_seconds": 60})
    token = read_credential(Path(grant["credential_file"]))["token"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=management_app(host, owner, port)),
                                 base_url=f"http://127.0.0.1:{port}") as client:
        route = f"/v1/pi/jobs/{job['id']}/conversation"
        query = {**context(job), "after": 0}
        assert (await client.get(route, params=query)).status_code == 401
        assert (await client.get(route, params=query, headers={"Authorization": "Bearer " + token})).status_code == 403
        headers = {"Authorization": "Bearer " + owner}
        assert (await client.get(route, params=query, headers=headers)).status_code == 200
        assert (await client.post(route, json=query, headers=headers)).status_code == 200
        assert (await client.post(route, json={**query, "extra": "value"}, headers=headers)).status_code == 400
        assert (await client.post(route, json={**query, "revision": True}, headers=headers)).status_code == 400
        for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
            assert (await client.get(route, params=query, headers={**headers, **extra})).status_code == 403
            assert (await client.post(route, json=query, headers={**headers, **extra})).status_code == 403
        assert (await client.get(route, params={**query, "extra": "value"}, headers=headers)).status_code == 400
        send = f"/v1/pi/jobs/{job['id']}/messages"
        assert (await client.post(send, json=message(job))).status_code == 401
        assert (await client.post(send, json=message(job), headers={"Authorization": "Bearer " + token})).status_code == 403
        for extra in ({"Host": "invalid.test"}, {"Origin": "https://invalid.test"}):
            assert (await client.post(send, json=message(job), headers={**headers, **extra})).status_code == 403
        payload = message(job)
        request = asyncio.create_task(client.post(send, json=payload, headers=headers))
        await asyncio.sleep(0)
        host.pi_setup.conversation.sent(job["id"], payload["message_id"])
        host.pi_setup.conversation.acknowledge(job["id"], payload["message_id"])
        assert (await request).json()["accepted"] is True
        assert (await client.get(route, params=query, headers=headers)).json()["events"][0]["role"] == "user"
    await host.pi_setup.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_owned_real_sdk_transport_accepts_input_during_a_host_tool(host, tmp_path, monkeypatch,
                                                                      group_reaper, capfd, caplog, cancelled):
    """Exercise the complete host/worker duplex protocol with a local fixture."""
    node = shutil.which("node")
    worker = Path(__file__).resolve().parents[1] / "agents/pi/worker.mjs"
    if node is None or not (worker.parent / "node_modules/@earendil-works/pi-coding-agent").is_dir():
        pytest.skip("pinned local Pi SDK fixture requires npm ci")
    requests, owned = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    tool_settled = asyncio.Event()

    async def provider(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            requests.append(json.loads(await reader.readexactly(length)))
            if len(requests) == 1:
                delta, finish = {"tool_calls": [{"index": 0, "id": "fixture-tool", "type": "function",
                    "function": {"name": "capabilities", "arguments": "{}"}}]}, "tool_calls"
            else:
                delta, finish = {"content": "The integration is ready for verification."}, "stop"
            chunks = []
            for value, reason in (({"role": "assistant", **delta}, None), ({}, finish)):
                frame = {"id": "local-fixture", "object": "chat.completion.chunk", "created": 1,
                    "model": "fixture-model", "choices": [{"index": 0, "delta": value, "finish_reason": reason}]}
                chunks.append("data: " + json.dumps(frame) + "\n\n")
            body = ("".join(chunks) + "data: [DONE]\n\n").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(provider, "127.0.0.1", 0)
    manager = host.pi_setup
    host.reaper = group_reaper
    draft = host.imports.create(source_folder(tmp_path), name="Validator", intent="Expose validation")
    now = time.time()
    job = {"id": str(uuid.uuid4()), "import_id": draft["draft_id"], "revision": draft["revision"],
        "source_revision": draft["source"]["revision"], "status": "running", "stage": "starting",
        "created_at": now, "updated_at": now, "code": None, "package": None}
    manager._update(job)
    manager.conversation.begin(job["id"])
    workspace = tmp_path / "duplex-workspace"
    workspace.mkdir(mode=0o700)

    class Broker:
        async def invoke(self, name, arguments):
            assert name == "capabilities" and arguments == {}
            entered.set()
            try:
                await release.wait()
                return {"success": True, "output": "{}"}
            finally:
                await asyncio.sleep(.01)
                tool_settled.set()

    async def check(*args):
        return {"success": True, "output": "package_verified"}

    from meecp.processes import ProcessOwner
    original_launch = ProcessOwner.launch

    async def track_launch(instance, *args, **kwargs):
        process = await original_launch(instance, *args, **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(ProcessOwner, "launch", track_launch)
    monkeypatch.setattr(manager, "_check_stage", check)
    settings = {"provider": "openai-compatible", "model": "fixture-model",
        "base_url": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"}
    task = asyncio.create_task(manager._run_agent(job, workspace, Broker(), (Path(node), worker),
                                                settings, "synthetic-provider-key"))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        payload = message(job, "Use the original selected engine.")
        reply = asyncio.create_task(manager.messages(job["id"], payload))
        assert (await asyncio.wait_for(reply, 10))["accepted"] is True
        assert not task.done(), "the host must acknowledge steering before the owned tool completes"
        if cancelled:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await asyncio.wait_for(task, 10)
            assert any(any(value["role"] == "user" and payload["text"] in json.dumps(value["content"])
                           for value in request["messages"]) for request in requests)
        assert tool_settled.is_set(), "owned tool cancellation must settle before the worker returns"
        events = manager.conversation_events(job["id"], {**context(job), "after": 0})
        assert {event["role"] for event in events["events"]} == ({"user"} if cancelled else {"assistant", "user"})
        assert not events["can_message"]
        assert owned and all(process.returncode is not None and process.wrapper.returncode is not None for process in owned)
        assert not list(workspace.iterdir())
        assert "synthetic-provider-key" not in json.dumps(events) + "".join(capfd.readouterr()) + caplog.text
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
        await manager.close()
