"""Validation and sanitized failures shared by both MCP transports."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["noul", "choice", "score"]
    instructions: str = Field(min_length=1, max_length=65536)
    criteria: dict[str, str] | list[str] | None = None


def validate_decision(state: Any, question: Question | dict) -> tuple[Any, dict]:
    try:
        q = question if isinstance(question, Question) else Question.model_validate(question)
        if not isinstance(state, (str, dict, list)):
            raise ValueError()
        # Reject non-JSON values and non-finite numbers before prompt construction.
        encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError()
        if not q.instructions.strip():
            raise ValueError()
        crit = q.criteria
        if q.type == "noul":
            if crit is not None and (not isinstance(crit, dict) or set(crit) - {"true", "false"}):
                raise ValueError()
        elif q.type == "choice":
            if not isinstance(crit, (dict, list)) or not 2 <= len(crit) <= 16:
                raise ValueError()
            labels = list(crit)
            if len(set(labels)) != len(labels) or any(not label.strip() for label in labels):
                raise ValueError()
        elif not isinstance(crit, list) or not 2 <= len(crit) <= 16:
            raise ValueError()
        if isinstance(crit, list) and any(not label.strip() for label in crit):
            raise ValueError()
        return state, q.model_dump(exclude_none=True)
    except (ValueError, TypeError, RecursionError):
        raise ServiceError("invalid_decision", "Invalid evidence or question criteria.", 400) from None
