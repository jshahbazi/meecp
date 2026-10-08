"""One broken owned scope cannot suppress cleanup of other service scopes."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from meecp.preparation_contracts import PreparationError
from meecp.preparation_service_routes import revoke


@pytest.mark.parametrize("fault", ["invalid-package", "gateway-unsettled", "client-cancel"])
async def test_service_revocation_retains_and_joins_all_scopes(fault):
    entered = asyncio.Event(); resume = asyncio.Event(); stopped = []; revoked = []
    slots = [NS(id="first", generation=1), NS(id="second", generation=1)]
    package = NS(manifest=NS(profile="mcp-stdio"), lock=NS(artifacts=[NS(path=name) for name in ("service.json", "backend/meecp_service/server.py")]))
    async def static(function, slot):
        if slot.id == "first" and fault == "invalid-package": raise PreparationError("package_identity_mismatch")
        return package
    async def gateway(identifier):
        entered.set()
        if fault == "client-cancel": await resume.wait()
        stopped.append((identifier, "gateway"))
        if identifier == "first" and fault == "gateway-unsettled": raise PreparationError("ownership_lost")
    async def residents(identifier): stopped.append((identifier, "residents"))
    updates = []
    host = NS(secrets=NS(revoke=lambda key: revoked.append(key) or {"status":"revoked"}),
        preparations=NS(list=lambda: []), registry=NS(list_slots=lambda: slots,
            update_slot=lambda identifier, **values: updates.append((identifier, values)),
            get_slot=lambda identifier: next(slot for slot in slots if slot.id == identifier)),
        static_work=static, package=lambda slot: package, package_settings=lambda *args: {"secret_references":{"api_key":"selected"}},
        locks={}, stop_gateway=gateway, stop_residents=residents)
    task = asyncio.create_task(revoke(host, "selected"))
    if fault == "client-cancel":
        await entered.wait(); task.cancel(); await asyncio.sleep(0)
        assert not task.done(); resume.set()
        result = await task
        assert result == {"status":"revoked"}
    else:
        with pytest.raises(PreparationError, match="^service_revocation_incomplete$"): await task
    assert revoked == ["selected"]
    assert ("second", "gateway") in stopped and ("second", "residents") in stopped
    if fault == "gateway-unsettled":
        assert ("first", "residents") in stopped
        assert not any(identifier == "first" and values.get("observed_state") == "stopped" for identifier, values in updates)
