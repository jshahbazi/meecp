from meecp.__main__ import (SERVE_EXIT_FAILURE, SERVE_EXIT_HOST_OWNED,
                             SERVE_EXIT_INVALID_POLICY, main, serve_exit_status)
from meecp.preparation_contracts import PreparationError
from meecp.supervisor import HostError


def test_serve_startup_statuses_are_bounded_and_non_sensitive():
    assert serve_exit_status(HostError("host_already_running")) == SERVE_EXIT_HOST_OWNED
    assert serve_exit_status(PreparationError("invalid_release_policy")) == SERVE_EXIT_INVALID_POLICY
    assert serve_exit_status(PreparationError("invalid_release_artifact")) == SERVE_EXIT_INVALID_POLICY
    assert serve_exit_status(HostError("resource_policy_required")) == SERVE_EXIT_FAILURE
    assert serve_exit_status(RuntimeError("details are never surfaced")) == SERVE_EXIT_FAILURE


def test_main_maps_actual_serve_failures_without_changing_cli_failures(monkeypatch, capsys):
    async def fail_host(_args):
        raise HostError("host_already_running")
    monkeypatch.setattr("meecp.__main__.execute", fail_host)
    assert main(["serve", "--reaper", "/tmp/reaper"]) == SERVE_EXIT_HOST_OWNED
    assert "host_already_running" not in capsys.readouterr().out

    async def fail_policy(_args):
        raise PreparationError("invalid_release_policy")
    monkeypatch.setattr("meecp.__main__.execute", fail_policy)
    assert main(["serve", "--reaper", "/tmp/reaper"]) == SERVE_EXIT_INVALID_POLICY
    capsys.readouterr()

    async def fail_unknown(_args):
        raise RuntimeError("private exception details")
    monkeypatch.setattr("meecp.__main__.execute", fail_unknown)
    assert main(["serve", "--reaper", "/tmp/reaper"]) == SERVE_EXIT_FAILURE
    assert "private exception details" not in capsys.readouterr().out

    async def fail_cli(_args):
        raise HostError("host_already_running")
    monkeypatch.setattr("meecp.__main__.execute", fail_cli)
    assert main(["capabilities", "--json"]) == 2
