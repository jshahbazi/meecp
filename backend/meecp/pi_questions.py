"""Ephemeral questions during Pi setup, with separately stored credentials.

Question text and ordinary answers belong to the live conversation. This helper
never writes them to the job journal or emits them through exception messages.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import uuid
from typing import Any

from .settings import SettingsError


class PiQuestionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identity(value: Any) -> str:
    if not isinstance(value, str):
        raise PiQuestionError("pi_question_invalid")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise PiQuestionError("pi_question_invalid") from None
    return value


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise PiQuestionError("pi_question_invalid")
    try:
        valid = len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        valid = False
    if not valid or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise PiQuestionError("pi_question_invalid")
    return value


@dataclass
class _Pending:
    question: dict[str, Any]
    future: asyncio.Future


class PiQuestions:
    """One pending question per live job, fenced to its exact import revision.

    The caller supplies the host's private SecretStore. A secret answer creates
    an immutable value there and returns only its opaque reference to Pi.
    """

    def __init__(self, secrets):
        self.secrets = secrets
        self._pending: dict[str, _Pending] = {}
        self._credentials: dict[str, dict[str, str]] = {}
        self._canceled: set[str] = set()
        self._closed = False

    @staticmethod
    def _context(job_id: Any, import_id: Any, revision: Any) -> None:
        _identity(job_id)
        _identity(import_id)
        if type(revision) is not int or revision < 1:
            raise PiQuestionError("pi_question_invalid")

    async def ask(self, job_id: str, import_id: str, revision: int, payload: Any) -> dict[str, Any]:
        self._context(job_id, import_id, revision)
        if self._closed or job_id in self._canceled:
            raise asyncio.CancelledError()
        if job_id in self._pending:
            raise PiQuestionError("pi_question_in_progress")
        if (not isinstance(payload, dict) or set(payload) - {"kind", "question", "options", "credential_name"}
                or not {"kind", "question"} <= set(payload) or not isinstance(payload["kind"], str)
                or payload["kind"] not in {"text", "secret"}):
            raise PiQuestionError("pi_question_invalid")
        question = _text(payload["question"], 8192)
        options = payload.get("options", [])
        if not isinstance(options, list) or len(options) > 16:
            raise PiQuestionError("pi_question_invalid")
        options = [_text(option, 256) for option in options]
        if len(set(options)) != len(options):
            raise PiQuestionError("pi_question_invalid")
        credential_name = payload.get("credential_name")
        if payload["kind"] == "secret":
            if (not isinstance(credential_name, str)
                    or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", credential_name) is None or options):
                raise PiQuestionError("pi_question_invalid")
        elif credential_name is not None:
            raise PiQuestionError("pi_question_invalid")
        public = {"id": str(uuid.uuid4()), "job_id": job_id, "import_id": import_id, "revision": revision,
                  "kind": payload["kind"], "question": question, "options": options,
                  "credential_name": credential_name}
        pending = _Pending(public, asyncio.get_running_loop().create_future())
        self._pending[job_id] = pending
        try:
            return await pending.future
        finally:
            if self._pending.get(job_id) is pending:
                self._pending.pop(job_id, None)

    def public(self, job_id: str, import_id: str, revision: int) -> dict[str, Any] | None:
        self._context(job_id, import_id, revision)
        pending = self._pending.get(job_id)
        if pending is None or pending.future.done():
            return None
        question = pending.question
        if question["import_id"] != import_id or question["revision"] != revision:
            raise PiQuestionError("pi_question_changed")
        return {**question, "options": list(question["options"])}

    def answer(self, job_id: str, question_id: str, import_id: str, revision: int, value: Any) -> dict[str, bool]:
        self._context(job_id, import_id, revision)
        _identity(question_id)
        pending = self._pending.get(job_id)
        if self._closed or pending is None or pending.future.done():
            raise PiQuestionError("pi_question_changed")
        question = pending.question
        if (question["id"] != question_id or question["import_id"] != import_id or question["revision"] != revision):
            raise PiQuestionError("pi_question_changed")
        try:
            answer = _text(value, 4096 if question["kind"] == "secret" else 8192)
        except PiQuestionError:
            raise PiQuestionError("pi_answer_invalid") from None
        if question["kind"] == "secret":
            try:
                reference = self.secrets.create(question["credential_name"], answer)["secret_id"]
            except SettingsError:
                raise PiQuestionError("pi_credentials_unavailable") from None
            self._credentials.setdefault(job_id, {})[question["credential_name"]] = reference
            result = {"kind": "secret", "credential_name": question["credential_name"], "secret_id": reference}
        else:
            result = {"kind": "text", "answer": answer}
        pending.future.set_result(result)
        return {"answered": True}

    def credentials(self, job_id: str) -> dict[str, str]:
        _identity(job_id)
        return dict(self._credentials.get(job_id, {}))

    def restore_credentials(self, job_id: str, references: Any) -> None:
        """Restore exact opaque bindings for a fresh authorized setup attempt."""
        _identity(job_id)
        if (not isinstance(references, dict) or len(references) > 64
                or any(not isinstance(name, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) is None
                       or not isinstance(reference, str) for name, reference in references.items())):
            raise PiQuestionError("pi_credentials_unavailable")
        try:
            self.secrets.resolve(references)
        except SettingsError:
            raise PiQuestionError("pi_credentials_unavailable") from None
        self._credentials[job_id] = dict(references)

    def cancel(self, job_id: str) -> None:
        _identity(job_id)
        self._canceled.add(job_id)
        pending = self._pending.pop(job_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()

    def forget(self, job_id: str) -> None:
        self.cancel(job_id)
        self._credentials.pop(job_id, None)

    def close(self) -> None:
        self._closed = True
        for job_id in list(self._pending):
            self.cancel(job_id)
        self._credentials.clear()
