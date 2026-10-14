"""Installation plans preserve registered and pending public port ownership."""
import pytest

from meecp.contracts import validate_package
from meecp.private import write_private_json
from meecp.supervisor import HostError, Supervisor, bind_listener, preferred_listener
from test_meecp_packages import make_package


@pytest.fixture
async def host(tmp_path):
    value = Supervisor(tmp_path / "state", tmp_path / "unused-reaper")
    try:
        yield value
    finally:
        await value.close()


def register(host, package, port, state="stopped"):
    slot = host.registry.add_slot(package_digest=package.digest, package_path=package.root,
        name="Existing", public_port=port)
    if state == "ownership_lost":
        slot = host.registry.update_slot(slot.id, observed_state="ownership_lost")
    return slot


@pytest.mark.parametrize("state", ["stopped", "ownership_lost"])
async def test_new_plan_skips_registered_unbound_preferred_port(host, tmp_path, state):
    package = validate_package(make_package(tmp_path / "source"))
    slot = register(host, package, 19386, state)
    connection = host.slot_directory(slot.id) / "connection.json"
    write_private_json(connection, {"slot_id": slot.id, "endpoint": "http://127.0.0.1:19386/mcp", "token": "synthetic-existing-invocation-token"})
    original_connection = connection.read_bytes()
    with bind_listener(19386) as available:
        assert available.getsockname()[1] == 19386
    plan = host.plan_install({"package": str(package.root)})
    assert plan["public_port"] != 19386
    current = host.registry.get_slot(slot.id)
    assert current.public_port == 19386 and current.observed_state == state
    assert connection.read_bytes() == original_connection


@pytest.mark.parametrize("closed", [False, True])
async def test_new_plan_skips_pending_plan_reserved_port(host, tmp_path, closed):
    package = make_package(tmp_path / "source")
    first = host.plan_install({"package": str(package)})
    if closed:
        host.plan_listeners[first["plan_id"]].close()
    second = host.plan_install({"package": str(package)})
    assert second["public_port"] != first["public_port"]
    assert set(host.plan_listeners) == {first["plan_id"], second["plan_id"]}


@pytest.mark.parametrize("owner", ["slot", "plan", "closed-plan"])
async def test_explicit_collision_fails_before_durable_plan_or_staging(host, tmp_path, owner):
    package = validate_package(make_package(tmp_path / "source"))
    if owner == "slot":
        register(host, package, 19386, "ownership_lost")
        port = 19386
    else:
        first = host.plan_install({"package": str(package.root)})
        port = first["public_port"]
        if owner == "closed-plan":
            host.plan_listeners[first["plan_id"]].close()
    plans = {path.name for path in (host.root / "plans").glob("*.json")}
    staging = {path.name for path in (host.root / "staging").glob("*")}
    listeners = set(host.plan_listeners)
    with pytest.raises(HostError, match="^port_conflict$"):
        host.plan_install({"package": str(package.root)}, public_port=port)
    assert {path.name for path in (host.root / "plans").glob("*.json")} == plans
    assert {path.name for path in (host.root / "staging").glob("*")} == staging
    assert set(host.plan_listeners) == listeners


async def test_async_explicit_collision_reclaims_prepared_snapshot(host, tmp_path):
    package = validate_package(make_package(tmp_path / "source"))
    register(host, package, 19386, "ownership_lost")
    before = {path.name for path in (host.root / "staging").glob("*")}
    with pytest.raises(HostError, match="^port_conflict$"):
        await host.plan_install_async({"package": str(package.root)}, public_port=19386)
    assert {path.name for path in (host.root / "staging").glob("*")} == before
    assert not list((host.root / "plans").glob("*.json")) and not host.plan_listeners


def test_preferred_listener_skips_exclusions_before_binding(monkeypatch):
    calls = []
    selected = FakeListener(19388)
    def bind(port=0):
        calls.append(port)
        assert port == 19388
        return selected
    monkeypatch.setattr("meecp.supervisor.bind_listener", bind)
    assert preferred_listener({19386, 19387}) is selected
    assert calls == [19388] and not selected.closed


class FakeListener:
    def __init__(self, port):
        self.port, self.closed = port, False

    def getsockname(self):
        return "127.0.0.1", self.port

    def close(self):
        self.closed = True


def test_ephemeral_fallback_holds_rejected_ports_then_closes_them(monkeypatch):
    rejected, selected = FakeListener(51000), FakeListener(51001)
    calls = []
    def bind(port=0):
        calls.append(port)
        if port:
            raise HostError("port_conflict", 409)
        if calls.count(0) == 1:
            return rejected
        assert not rejected.closed
        return selected
    monkeypatch.setattr("meecp.supervisor.bind_listener", bind)
    assert preferred_listener({51000}) is selected
    assert calls[:100] == list(range(19386, 19486))
    assert calls[-2:] == [0, 0] and rejected.closed and not selected.closed


def test_fallback_failure_closes_rejected_sockets_and_stays_bounded(monkeypatch):
    rejected = FakeListener(51000)
    ephemeral_calls = 0
    def bind(port=0):
        nonlocal ephemeral_calls
        if port:
            raise HostError("port_conflict", 409)
        ephemeral_calls += 1
        if ephemeral_calls == 1:
            return rejected
        raise HostError("port_conflict", 409)
    monkeypatch.setattr("meecp.supervisor.bind_listener", bind)
    with pytest.raises(HostError, match="^port_conflict$"):
        preferred_listener({51000})
    assert ephemeral_calls == 2 and rejected.closed


async def test_registered_slot_capacity_rejects_without_creating_plan(host, tmp_path):
    package = validate_package(make_package(tmp_path / "source"))
    for port in range(20000, 20016):
        register(host, package, port)
    with pytest.raises(HostError, match="^capacity_exceeded$"):
        host.plan_install({"package": str(package.root)})
    assert not host.plan_listeners and not list((host.root / "plans").glob("*.json"))
