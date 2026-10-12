"""Authenticated private HTTP facade over the exact native scoring protocol."""
from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from jevk5_app.contracts import ServiceError
from jevk5_app.server import LocalPolicy
from .worker import _ids, _json, _number


def make_app(worker, token: str, *, port: int, generation: int, nonce: str):
    async def health(_request):
        return JSONResponse({"ready": worker.is_ready(), "generation": generation, "nonce": nonce,
                             "engine": "stark", "cache_policy": worker.cache_policy})

    async def operation(request):
        name = request.url.path[1:]
        try:
            try:
                value = _json(await request.body())
                if not isinstance(value, dict):
                    raise ValueError()
                if name == "tokenize":
                    if (set(value) != {"text", "add_special_tokens"}
                            or not isinstance(value["text"], str) or not value["text"]
                            or type(value["add_special_tokens"]) is not bool):
                        raise ValueError()
                elif name == "score":
                    if (set(value) != {"input_token_ids", "candidate_token_ids", "context_tokens", "temperature"}
                            or not _ids(value["input_token_ids"], worker.vocab_size) or not value["input_token_ids"]
                            or not _ids(value["candidate_token_ids"], worker.vocab_size)
                            or not 2 <= len(value["candidate_token_ids"]) <= 16
                            or len(set(value["candidate_token_ids"])) != len(value["candidate_token_ids"])
                            or type(value["context_tokens"]) is not int or not 1 <= value["context_tokens"] <= worker.context_tokens
                            or not _number(value["temperature"]) or value["temperature"] <= 0):
                        raise ValueError()
                    if len(value["input_token_ids"]) > value["context_tokens"]:
                        raise ServiceError("context_limit", "The decision exceeds the model context budget.", 400)
                elif value:
                    raise ValueError()
            except ServiceError:
                if worker.is_ready():
                    await worker.request("reset_cache", {})
                raise
            except Exception:
                if worker.is_ready():
                    await worker.request("reset_cache", {})
                raise ServiceError("invalid_request", "Invalid scoring request.", 400) from None
            return JSONResponse(await worker.request(name, value))
        except ServiceError as exc:
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            await worker.start()
            yield
        finally:
            await worker.close()

    app = Starlette(routes=[Route("/health", health, methods=["GET"]),
                            *(Route("/" + name, operation, methods=["POST"])
                              for name in ("tokenize", "score", "reset_cache"))], lifespan=lifespan)
    return LocalPolicy(app, token, port=port)
