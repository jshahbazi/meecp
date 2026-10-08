from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from meecp.pi_questions import PiQuestionError, PiQuestions
from meecp.settings import SecretStore


@pytest.fixture
def questions(tmp_path):
    return PiQuestions(SecretStore(tmp_path / "private"))


@pytest.fixture
def context():
    return str(uuid.uuid4()), str(uuid.uuid4()), 3


async def pending(questions, context, payload=None):
    task = asyncio.create_task(questions.ask(*context, payload or {
        "kind": "text", "question": "Which calendar should this app use?", "options": ["Work", "Personal"],
    }))
    await asyncio.sleep(0)
    question = questions.public(*context)
    assert question is not None
    return task, question


async def test_answer_resumes_same_live_question_without_persisting_conversation(questions, context):
    task, question = await pending(questions, context)
    assert question["job_id"] == context[0] and question["revision"] == 3
    question["options"].append("Mutation")
    assert questions.public(*context)["options"] == ["Work", "Personal"]
    assert questions.answer(context[0], question["id"], *context[1:], "Personal\nWeekends") == {"answered": True}
    assert questions.public(*context) is None
    assert await task == {"kind": "text", "answer": "Personal\nWeekends"}
    assert list(questions.secrets.records.iterdir()) == []


async def test_secret_answer_returns_only_opaque_reference(questions, context):
    value = "test-private-credential-value"
    task, question = await pending(questions, context, {
        "kind": "secret", "question": "Enter the calendar API credential.", "credential_name": "CALENDAR_API_KEY",
    })
    ack = questions.answer(context[0], question["id"], *context[1:], value)
    result = await task
    assert value not in json.dumps(ack) + json.dumps(result) + json.dumps(question)
    assert set(result) == {"kind", "credential_name", "secret_id"}
    assert result["kind"] == "secret" and result["credential_name"] == "CALENDAR_API_KEY"
    assert questions.secrets.resolve({"CALENDAR_API_KEY": result["secret_id"]}) == {"CALENDAR_API_KEY": value}
    references = questions.credentials(context[0])
    assert references == {"CALENDAR_API_KEY": result["secret_id"]}
    references.clear()
    assert questions.credentials(context[0])
    assert questions.public(*context) is None


@pytest.mark.parametrize("change", ["question", "import", "revision"])
async def test_stale_answer_is_rejected_before_secret_creation(questions, context, change):
    task, question = await pending(questions, context, {
        "kind": "secret", "question": "API credential?", "credential_name": "API_KEY",
    })
    arguments = [context[0], question["id"], context[1], context[2]]
    arguments[{"question": 1, "import": 2, "revision": 3}[change]] = 4 if change == "revision" else str(uuid.uuid4())
    with pytest.raises(PiQuestionError, match="^pi_question_changed$"):
        questions.answer(*arguments, "private-value")
    assert not task.done() and questions.secrets.count() == 0
    questions.cancel(context[0])
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_clears_question_and_cannot_accept_or_restart_it(questions, context):
    task, question = await pending(questions, context)
    questions.cancel(context[0])
    assert questions.public(*context) is None
    with pytest.raises(PiQuestionError, match="^pi_question_changed$"):
        questions.answer(context[0], question["id"], *context[1:], "Late answer")
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(asyncio.CancelledError):
        await questions.ask(*context, {"kind": "text", "question": "A late question"})


async def test_task_cancellation_clears_pending_question(questions, context):
    task, _ = await pending(questions, context)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert questions.public(*context) is None


async def test_cannot_overwrite_question_or_answer_twice(questions, context):
    task, question = await pending(questions, context)
    with pytest.raises(PiQuestionError, match="^pi_question_in_progress$"):
        await questions.ask(*context, {"kind": "text", "question": "Another question"})
    questions.answer(context[0], question["id"], *context[1:], "Answer")
    with pytest.raises(PiQuestionError, match="^pi_question_changed$"):
        questions.answer(context[0], question["id"], *context[1:], "Another answer")
    assert await task == {"kind": "text", "answer": "Answer"}


async def test_new_question_gets_new_id_after_answer(questions, context):
    task, first = await pending(questions, context)
    questions.answer(context[0], first["id"], *context[1:], "Answer")
    await task
    task, second = await pending(questions, context)
    assert first["id"] != second["id"]
    with pytest.raises(PiQuestionError, match="^pi_question_changed$"):
        questions.answer(context[0], first["id"], *context[1:], "Late answer")
    questions.close()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("payload", [
    {}, {"kind": [], "question": "Question"}, {"kind": "text", "question": "Question", "extra": "private"},
    {"kind": "text", "question": "\0private"}, {"kind": "text", "question": "é" * 4097},
    {"kind": "text", "question": "Question", "credential_name": "API_KEY"},
    {"kind": "text", "question": "Question", "options": ["Same", "Same"]},
    {"kind": "secret", "question": "Question"},
    {"kind": "secret", "question": "Question", "credential_name": "api-key"},
    {"kind": "secret", "question": "Question", "credential_name": "API_KEY", "options": ["A"]},
])
async def test_invalid_questions_fail_with_fixed_error(questions, context, payload):
    with pytest.raises(PiQuestionError, match="^pi_question_invalid$"):
        await questions.ask(*context, payload)
    assert questions.public(*context) is None


async def test_invalid_answer_keeps_pending_question(questions, context):
    task, question = await pending(questions, context)
    with pytest.raises(PiQuestionError, match="^pi_answer_invalid$"):
        questions.answer(context[0], question["id"], *context[1:], "\0private")
    assert not task.done() and questions.public(*context) is not None
    questions.close()
    with pytest.raises(asyncio.CancelledError):
        await task
