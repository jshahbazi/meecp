from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from meecp.pi_dependencies import _node_bin_launchers, _node_lock, install_dependencies, node_project
from meecp.pi_tools import PiToolBroker
from meecp.preparation_contracts import PreparationError
from test_preparation_processes import group_reaper


def test_node_metadata_accepts_project_specific_scripts_and_lock_rejects_credentials():
    manifest = {"name": "custom-app", "dependencies": {"@modelcontextprotocol/sdk": "^1.0.0"},
        "scripts": {"build": "custom compiler invocation"}}
    assert node_project(json.dumps(manifest).encode()) == manifest
    lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/test": {
        "resolved": "https://user:secret@registry.npmjs.org/test.tgz", "version": "1.0.0"}}}
    with pytest.raises(PreparationError, match="credentials_unsupported"):
        _node_lock(json.dumps(lock).encode())


def test_bin_launcher_keeps_target_script_location_and_regular_files(tmp_path):
    from meecp.pi_environment import copy_regular_tree
    source = tmp_path / "source"
    executable = source / "node_modules/package with spaces/bin/command.js"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env node\nrequire('../lib/main.js')\n")
    executable.chmod(0o755)
    bin_directory = source / "node_modules/.bin"
    bin_directory.mkdir()
    (bin_directory / "command").symlink_to("../package with spaces/bin/command.js")
    target = tmp_path / "target with spaces"
    copy_regular_tree(source, target)
    assert _node_bin_launchers(source, target) == 1
    launcher = target / "node_modules/.bin/command"
    assert not launcher.is_symlink() and launcher.stat().st_nlink == 1
    value = launcher.read_text()
    assert "'../package with spaces/bin/command.js'" in value
    assert '"$@"' in value and ".meecp-node/bin" in value
    assert "require('../lib/main.js')" not in value


@pytest.mark.skipif(sys.platform != "darwin" or not os.environ.get("MCPME_PI_NODE_LIVE_HOST"),
                    reason="explicit public registry acceptance")
async def test_real_npm_lock_install_and_relocated_private_node(tmp_path, group_reaper):
    host = Path(os.environ["MCPME_PI_NODE_LIVE_HOST"]).resolve()
    broker = PiToolBroker(tmp_path.resolve() / "workspace", group_reaper, read_roots=(host,))
    broker.host_root = host
    project = broker.workspace / "app"
    project.mkdir()
    local = project / "local-helper"
    local.mkdir()
    (local / "package.json").write_text('{"name":"local-helper","version":"1.0.0","main":"index.js","bin":{"local-cli":"bin/run.js"}}')
    (local / "index.js").write_text("module.exports=value=>value.toUpperCase()")
    (local / "bin").mkdir()
    (local / "lib").mkdir()
    (local / "bin/run.js").write_text("#!/usr/bin/env node\nrequire('../lib/build.js')(process.argv.slice(2))\n")
    (local / "lib/build.js").write_text("module.exports=args=>{const n=require('is-number'),h=require('../index.js');if(!n('42')||h('ok')!=='OK'||args.length!==1||args[0]!=='argument with spaces')process.exit(2);require('fs').writeFileSync('built.txt','built')}")
    (project / "package.json").write_text(json.dumps({"name": "pi-node-acceptance", "version": "1.0.0",
        "dependencies": {"is-number": "7.0.0", "local-helper": "file:./local-helper"},
        "scripts": {"postinstall": "exit 99", "build": "local-cli 'argument with spaces'"}}))
    (project / ".env").write_text("excluded credential fixture")
    (project / "credentials.json").write_text('{"fixture":"excluded"}')
    (project / "LICENSE").write_text("synthetic source license")
    try:
        result = await install_dependencies(broker, {"ecosystem": "node", "project_path": "app"}, host)
        assert result["success"], result
        metadata = json.loads(result["output"])
        runtime = Path(metadata["runtime_path"])
        assert metadata["dependency_count"] >= 2
        assert metadata["bin_launcher_count"] == 1
        assert (runtime / ".meecp-node/licenses/NODE-LICENSE.txt").is_file()
        notices = json.loads(Path(metadata["dependency_notices_path"]).read_bytes())
        number = next(item for item in notices["distributions"] if item["name"] == "is-number")
        assert number["version"] == "7.0.0" and number["integrity"].startswith("sha512-")
        assert (runtime / "LICENSE").read_text() == "synthetic source license"
        assert not (runtime / ".env").exists() and not (runtime / "credentials.json").exists()
        assert (project / "package-lock.json").read_bytes() == (runtime / "package-lock.json").read_bytes()
        relocated = broker.workspace / "relocated project with spaces"
        shutil.move(runtime, relocated)
        node = relocated / Path(metadata["node_path"]).relative_to(runtime)
        execution = await broker._run({"argv": [str(node), str(host / "pi-runtime/npm/bin/npm-cli.js"),
            "--no-audit", "--no-fund", "--logs-max=0", "run", "build"],
            "cwd": str(relocated), "timeout_seconds": 20,
            "env": {"PATH": str(node.parent) + ":/usr/bin:/bin"}})
        assert execution["success"], execution
        assert (relocated / "built.txt").read_text() == "built"
        assert not any(path.is_symlink() for path in relocated.rglob("*"))
        assert not list(broker.workspace.glob(".meecp-dependencies-*"))
    finally:
        await broker.close()
