"""Actual Darwin confinement with the original private runtime, no provider."""
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from meecp.preparation_probe_confinement import restricted_profile
from meecp.preparation_service_contracts import ServiceConnectionV1
from meecp.preparation_service_verifier import confined_profile
from test_service_source_contracts import connection


@pytest.mark.parametrize("action", ["selected", "other-port", "other-loopback", "non-loopback", "outside-write", "spawn", "fork", "setsid", "setpgid"])
def test_actual_service_confined_authority(tmp_path, action):
    selected = os.environ.get("MCPME_TEST_SERVICE_HOST")
    if not selected:
        pytest.skip("explicit composed private service host required")
    root = Path(selected) / "preparation-toolchain"
    interpreter = root / "python/bin/python3.12"
    home = tmp_path / "home"; home.mkdir(mode=0o700)
    temporary = tmp_path / "temporary"; temporary.mkdir(mode=0o700)
    listeners = [socket.socket(), socket.socket()]
    try:
        for listener in listeners:
            listener.bind(("127.0.0.1", 0)); listener.listen(1)
        port, other = [listener.getsockname()[1] for listener in listeners]
        raw = connection(); raw["endpoint"]["port"] = port
        policy = confined_profile(restricted_profile(root, interpreter, home, temporary), ServiceConnectionV1.model_validate(raw))
        operation = {
            "selected": f"s=socket.socket(socket.AF_INET); s.settimeout(2); s.connect(('127.0.0.1',{port})); s.close()",
            "other-port": f"s=socket.socket(socket.AF_INET); s.settimeout(2); s.connect(('127.0.0.1',{other}))",
            "other-loopback": f"s=socket.socket(socket.AF_INET); s.settimeout(2); s.connect(('127.0.0.2',{port}))",
            "non-loopback": f"s=socket.socket(socket.AF_INET); s.settimeout(2); s.connect(('192.0.2.1',{port}))",
            "outside-write": f"open({str(tmp_path / 'outside')!r},'w').close()",
            "spawn": "subprocess.run(['/usr/bin/true'],check=True)",
            "fork": "pid=os.fork(); os._exit(91) if pid==0 else os.waitpid(pid,0)",
            "setsid": "os.setsid()", "setpgid": "os.setpgid(0,0)",
        }[action]
        code = "import os,socket,subprocess,json\ntry:\n " + operation + "\n print(json.dumps({'allowed':True}))\nexcept PermissionError:\n print(json.dumps({'allowed':False}))\n"
        result = subprocess.run(["/usr/bin/sandbox-exec", "-p", policy, str(interpreter), "-I", "-S", "-B", "-c", code],
            env={"PATH":"/usr/bin:/bin","HOME":str(home),"TMPDIR":str(temporary)}, capture_output=True, timeout=5)
        assert result.returncode == 0, "service_confinement_control_failed"
        assert json.loads(result.stdout) == {"allowed": action == "selected"}
        assert not (tmp_path / "outside").exists()
    finally:
        for listener in listeners: listener.close()
