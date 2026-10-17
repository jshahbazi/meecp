"""Bounded live setup messages. This state never enters a journal or file."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import time
import uuid

MAX_TEXT_BYTES = 8192
MAX_USER_MESSAGES = 64
MAX_QUEUED_MESSAGES = 8
MAX_HISTORY_EVENTS = 128
MAX_HISTORY_BYTES = 256 * 1024
MAX_SESSIONS = 8
TERMINAL_TTL_SECONDS = 300


class PiConversationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def plain_text(value):
    if not isinstance(value, str) or not value.strip():
        raise PiConversationError("pi_message_invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        size = MAX_TEXT_BYTES + 1
    if size > MAX_TEXT_BYTES or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise PiConversationError("pi_message_invalid")
    return value


@dataclass
class _Session:
    events: deque = field(default_factory=deque)
    identities: dict = field(default_factory=dict)
    inbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(MAX_QUEUED_MESSAGES))
    sequence: int = 0
    bytes: int = 0
    terminal_at: float | None = None
    expiry: asyncio.TimerHandle | None = None
    accepting: bool = True


class PiConversation:
    """One active session and a small expiring terminal history, in RAM only."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.sessions = {}

    def reap(self):
        now = self.clock()
        for job_id, session in list(self.sessions.items()):
            if session.terminal_at is not None and now - session.terminal_at >= TERMINAL_TTL_SECONDS:
                self.clear(job_id)

    def begin(self, job_id):
        self.reap()
        if len(self.sessions) >= MAX_SESSIONS:
            oldest = next((key for key, value in self.sessions.items() if value.terminal_at is not None), None)
            if oldest is None:
                raise PiConversationError("pi_message_limit")
            self.clear(oldest)
        self.sessions[job_id] = _Session()

    def active(self, job_id):
        self.reap()
        session = self.sessions.get(job_id)
        return session if session is not None and session.terminal_at is None else None

    def append(self, job_id, role, text, *, message_id=None):
        self.reap()
        session = self.sessions.get(job_id)
        if session is None:
            return
        text = plain_text(text)
        if role not in {"user", "assistant"}:
            raise PiConversationError("pi_protocol_invalid")
        session.sequence += 1
        event = {"sequence": session.sequence, "role": role, "text": text, "message_id": message_id}
        size = len(text.encode("utf-8"))
        session.events.append(event)
        session.bytes += size
        while len(session.events) > MAX_HISTORY_EVENTS or session.bytes > MAX_HISTORY_BYTES:
            session.bytes -= len(session.events.popleft()["text"].encode("utf-8"))

    def enqueue(self, job_id, message_id, text):
        try:
            if not isinstance(message_id, str) or str(uuid.UUID(message_id)) != message_id:
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise PiConversationError("pi_message_invalid") from None
        text = plain_text(text)
        session = self.active(job_id)
        if session is None or not session.accepting:
            raise PiConversationError("pi_message_unavailable")
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        if message_id in session.identities:
            previous = session.identities[message_id]
            if previous["digest"] != digest:
                raise PiConversationError("pi_message_changed")
            return previous["future"], True
        if len(session.identities) >= MAX_USER_MESSAGES:
            raise PiConversationError("pi_message_limit")
        if session.inbox.full():
            raise PiConversationError("pi_message_busy")
        session.inbox.put_nowait({"type": "user_message", "id": message_id, "text": text})
        future = asyncio.get_running_loop().create_future()
        # A disconnected requester does not cancel accepted worker input. Read
        # fixed failures even if no requester remains to await acknowledgment.
        future.add_done_callback(lambda result: None if result.cancelled() else result.exception())
        session.identities[message_id] = {"digest": digest, "future": future, "text": text, "sent": False}
        return future, False

    def sent(self, job_id, message_id):
        session = self.active(job_id)
        if session is None or message_id not in session.identities:
            raise PiConversationError("pi_protocol_invalid")
        session.identities[message_id]["sent"] = True

    def acknowledge(self, job_id, message_id):
        session = self.active(job_id)
        entry = session.identities.get(message_id) if session is not None else None
        if entry is None or not entry["sent"] or entry["future"].done():
            raise PiConversationError("pi_protocol_invalid")
        self.append(job_id, "user", entry.pop("text"), message_id=message_id)
        entry["future"].set_result(None)

    def stop_input(self, job_id):
        session = self.sessions.get(job_id)
        if session is not None:
            session.accepting = False
            for entry in session.identities.values():
                entry.pop("text", None)
                if not entry["future"].done():
                    entry["future"].set_exception(PiConversationError("pi_message_unavailable"))
            while not session.inbox.empty():
                session.inbox.get_nowait()

    def read(self, job_id, after, *, can_message):
        if type(after) is not int or after < 0 or after > 2**53 - 1:
            raise PiConversationError("pi_message_invalid")
        self.reap()
        session = self.sessions.get(job_id)
        if session is None:
            return {"job_id": job_id, "events": [], "cursor": 0, "first_sequence": 0,
                    "reset": after != 0, "available": False, "can_message": False}
        first = session.events[0]["sequence"] if session.events else 0
        reset = after < first - 1 or after > session.sequence
        events = [dict(event) for event in session.events if reset or event["sequence"] > after]
        writable = (can_message and session.accepting and session.terminal_at is None and not session.inbox.full()
                    and len(session.identities) < MAX_USER_MESSAGES)
        return {"job_id": job_id, "events": events, "cursor": session.sequence,
                "first_sequence": first, "reset": reset, "available": True, "can_message": writable}

    def finish(self, job_id, *, clear=False):
        if clear:
            self.clear(job_id)
            return
        session = self.sessions.get(job_id)
        if session is not None and session.terminal_at is None:
            self.stop_input(job_id)
            session.terminal_at = self.clock()
            try:
                session.expiry = asyncio.get_running_loop().call_later(TERMINAL_TTL_SECONDS, self.clear, job_id)
            except RuntimeError:
                pass
            while not session.inbox.empty():
                session.inbox.get_nowait()
            session.identities.clear()

    def clear(self, job_id):
        self.stop_input(job_id)
        session = self.sessions.pop(job_id, None)
        if session is not None:
            if session.expiry is not None:
                session.expiry.cancel()
            session.events.clear()
            session.identities.clear()
            while not session.inbox.empty():
                session.inbox.get_nowait()

    def close(self):
        for job_id in list(self.sessions):
            self.clear(job_id)
