"""Trusted measured preparation capability reaches the closed host runner."""
import hashlib
import pytest

from mcpme.host_command import HostCommandAdmission
from mcpme.preparation_command_owner import CommandPreparationResourceAdmission
from mcpme.preparation_contracts import PreparationError
from mcpme.preparation_runner import PreparationRunner
from mcpme.supervisor import Supervisor
from test_command_default_registration import packaged


def admissions(packaged):
    root, _, _ = packaged
    helper = root / "bin/mcpme-command-reap"
    digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    # These checks never launch. Actual helpers and platform admission have
    # separate native SDK/lifecycle tests.
    return (HostCommandAdmission(helper, digest),
        CommandPreparationResourceAdmission(helper, digest, "a" * 64))


@pytest.mark.parametrize("factory", ["default", "explicit"])
async def test_explicit_host_capability_reaches_runner_without_enabling_it(tmp_path, packaged, factory):
    _, reaper, _ = packaged
    command, resource = admissions(packaged)
    options = {"preparation_runner_factory": PreparationRunner} if factory == "explicit" else {}
    host = Supervisor(tmp_path / "state", reaper, command_admission=command,
        command_resource_admission=resource, **options)
    try:
        assert host.commands.admission is command
        assert host.preparation_runner.command_admission is command
        assert host.preparation_runner.command_resource_admission is resource
        assert host.preparation_runner.enabled is False
        assert not host.preparation_runner.enabled_families and not host.preparation_runner.tasks
        assert not host.owners
    finally:
        await host.close()


async def test_inventory_does_not_supply_measured_admission(tmp_path, packaged):
    _, reaper, _ = packaged
    host = Supervisor(tmp_path / "state", reaper)
    try:
        assert host.commands.admission is None
        assert host.preparation_runner.command_admission is None
        assert host.preparation_runner.command_resource_admission is None
        assert host.preparation_runner.enabled is False and not host.owners
    finally:
        await host.close()


@pytest.mark.parametrize("invalid", ["missing-command", "untyped-command", "untyped-resource"])
def test_untyped_or_unpaired_capability_is_denied_before_state_creation(tmp_path, packaged, invalid):
    _, reaper, _ = packaged
    command, resource = admissions(packaged)
    if invalid == "missing-command":
        command = None
    elif invalid == "untyped-command":
        command = {"helper": str(command.helper)}
    else:
        resource = {"helper": str(resource.helper)}
    state = tmp_path / "must-not-create"
    with pytest.raises(PreparationError, match="command_preparation_not_admitted"):
        Supervisor(state, reaper, command_admission=command, command_resource_admission=resource)
    assert not state.exists()
