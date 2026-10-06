"""Owner-requested connectivity checks, independent of preparation execution.

This settings-only route never accepts captured source or changes provider state,
release admission, preparation plans or jobs. The admitted preparation adapter's
source identity remains unchanged by this separately bounded synthetic transport.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from .preparation_agent_backend import ENDPOINT
from .preparation_agent_contracts import AgentUsageV1
from .preparation_agent_providers import AgentProviderError, Identifier, RemoveProvider

PROVIDER_TEST_SECONDS = 15
PROVIDER_TEST_OUTPUT_TOKENS = 64
MAX_RESPONSE_BYTES = 16 * 1024
ERRORS = frozenset({"agent_provider_test_busy", "agent_provider_authentication_failed",
    "agent_provider_model_unavailable", "agent_provider_rate_limited", "agent_provider_test_incomplete",
    "agent_provider_test_timeout", "agent_provider_test_failed"})


class ProviderTestError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else "agent_provider_test_failed"
        super().__init__(self.code)


class ProviderTestInput(RemoveProvider):
    model: Identifier


class ProviderConnectionTester:
    def __init__(self, registry, *, transport=None):
        self.registry = registry
        self._transport = transport
        self._test_active = False

    async def test_connection(self, provider_id, payload, *, owner_authorized=False):
        if owner_authorized is not True:
            raise AgentProviderError("agent_provider_owner_required")
        try:
            choices = ProviderTestInput.model_validate(payload)
        except Exception:
            raise AgentProviderError("agent_provider_request_rejected") from None
        # The registry lock makes the selected generation, configured model and
        # credential resolver one snapshot. No secret is read from the request.
        with self.registry._lock:
            record = self.registry._record(self.registry._read(), provider_id)
            if record.status != "active" or record.generation != choices.expected_generation:
                raise AgentProviderError("agent_provider_configuration_stale")
            if choices.model not in record.models:
                raise AgentProviderError("agent_provider_request_rejected")
            configuration = self.registry.configuration(provider_id)
            if record.adapter_id != "openai-responses-v1" or configuration.destination != ENDPOINT:
                raise AgentProviderError("agent_provider_adapter_unavailable")
            if self._test_active:
                raise ProviderTestError("agent_provider_test_busy")
            self._test_active = True
        try:
            # Re-resolve immediately before transport and again before reporting
            # success. Rotation, removal and revocation invalidate this result.
            secret = self.registry._resolve(provider_id, record.generation, record.credential_generation)["api_key"]
            await self._request(secret, choices.model)
            self.registry._resolve(provider_id, record.generation, record.credential_generation)
            return {"schema_version": 1, "provider_id": provider_id, "generation": record.generation,
                "model": choices.model, "status": "connected"}
        except (AgentProviderError, ProviderTestError):
            raise
        except Exception:
            raise ProviderTestError("agent_provider_test_failed") from None
        finally:
            with self.registry._lock:
                self._test_active = False

    async def _request(self, secret, model):
        body = json.dumps({"model": model, "store": False, "background": False,
            "max_output_tokens": PROVIDER_TEST_OUTPUT_TOKENS, "tools": [], "tool_choice": "none",
            "truncation": "disabled", "input": "Reply with OK."}, separators=(",", ":")).encode()
        try:
            async with asyncio.timeout(PROVIDER_TEST_SECONDS):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                        timeout=PROVIDER_TEST_SECONDS, transport=self._transport) as client:
                    async with client.stream("POST", ENDPOINT, content=body,
                            headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json",
                                "Accept-Encoding": "identity"}) as response:
                        if response.status_code != 200:
                            codes = {401: "agent_provider_authentication_failed", 403: "agent_provider_model_unavailable",
                                404: "agent_provider_model_unavailable", 429: "agent_provider_rate_limited"}
                            raise ProviderTestError(codes.get(response.status_code, "agent_provider_test_failed"))
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise ProviderTestError("agent_provider_test_failed")
                        result = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=MAX_RESPONSE_BYTES):
                            if len(result) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise ProviderTestError("agent_provider_test_failed")
                            result.extend(chunk)
            value = json.loads(result)
            try:
                usage = AgentUsageV1.model_validate({key: value["usage"][key] for key in ("input_tokens", "output_tokens")})
                if usage.output_tokens > PROVIDER_TEST_OUTPUT_TOKENS:
                    raise ProviderTestError("agent_provider_test_incomplete")
                # Reasoning models can spend the tiny test budget before text.
                # This still verifies authentication and model request access.
                if (value.get("status") == "incomplete" and
                        value.get("incomplete_details", {}).get("reason") == "max_output_tokens"):
                    return
                if value.get("status") != "completed":
                    raise ProviderTestError("agent_provider_test_incomplete")
                texts = [item["text"] for output in value["output"] if output.get("type") == "message"
                    and output.get("role") == "assistant" for item in output["content"] if item.get("type") == "output_text"]
                if not texts or not all(isinstance(text, str) and text.strip() for text in texts):
                    raise ProviderTestError("agent_provider_test_incomplete")
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ProviderTestError("agent_provider_test_incomplete") from None
        except TimeoutError:
            raise ProviderTestError("agent_provider_test_timeout") from None
        except ProviderTestError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            raise ProviderTestError("agent_provider_test_failed") from None


def provider_test_routes(local_policy, registry, *, tester=None):
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from .policy import LocalPolicy, load_json
    if not isinstance(local_policy, LocalPolicy):
        raise AgentProviderError("agent_provider_owner_required")
    connection_tester = tester if tester is not None else ProviderConnectionTester(registry)
    if connection_tester.registry is not registry:
        raise AgentProviderError("agent_provider_request_rejected")

    async def dispatch(request):
        principal = request.scope.get("mcpme.principal", {})
        if principal.get("owner") is not True or principal.get("grant_id") is not None:
            return JSONResponse({"api_version": 1, "error": {"code": "grant_scope"}}, status_code=403)
        try:
            data = await request.body()
            if len(data) > 64 * 1024:
                raise AgentProviderError("agent_provider_request_rejected")
            result = await connection_tester.test_connection(request.path_params["provider_id"], load_json(data), owner_authorized=True)
            return JSONResponse(result | {"api_version": 1})
        except (AgentProviderError, ProviderTestError) as error:
            status = {"agent_provider_configuration_stale": 409, "agent_provider_not_found": 404}.get(error.code, 400)
            return JSONResponse({"api_version": 1, "error": {"code": error.code}}, status_code=status)
        except Exception:
            return JSONResponse({"api_version": 1, "error": {"code": "agent_provider_request_rejected"}}, status_code=400)

    local_policy.app.routes.append(Route("/v1/agent-providers/{provider_id}/test", dispatch, methods=["POST"]))
    return local_policy
