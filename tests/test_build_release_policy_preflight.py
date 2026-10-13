"""App producers reject stale implementation pins before signing a bundle."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("app_policy_builder", SCRIPTS / "build_meecp_app.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize("returncode", [0, 1, 78])
def test_preflight_uses_packaged_python_without_state_or_raw_output(tmp_path, monkeypatch, returncode):
    calls = []
    def run(arguments, **options):
        calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, returncode)
    monkeypatch.setattr(builder.subprocess, "run", run)
    if returncode:
        with pytest.raises(SystemExit, match="^release_policy_implementation_mismatch$"):
            builder.validate_packaged_release_policy(tmp_path)
    else:
        builder.validate_packaged_release_policy(tmp_path)
    arguments, options = calls[0]
    assert arguments[:4] == [str(tmp_path / "python/bin/python3.12"), "-P", "-B", "-c"]
    assert "validate_policy_implementations" in arguments[4] and "serve" not in arguments[4]
    assert options["cwd"] == tmp_path
    assert options["env"]["PYTHONPATH"] == str(tmp_path / "backend") + ":" + str(tmp_path / "python/site-packages")
    assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["timeout"] == 60


@pytest.mark.parametrize("error", [OSError("private detail"), subprocess.TimeoutExpired("private command", 60)])
def test_preflight_tool_failure_is_sanitized(tmp_path, monkeypatch, error):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(builder.subprocess, "run", run)
    with pytest.raises(SystemExit, match="^release_policy_preflight_failed$") as raised:
        builder.validate_packaged_release_policy(tmp_path)
    assert raised.value.__suppress_context__
