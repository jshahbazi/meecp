"""Compile and exercise actual AppKit handlers; no provider/network dispatch."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="mcpme-agent-view-") as temporary:
        private = Path(temporary).resolve()
        source = private / "main.swift"
        source.write_text('import AppKit\nlet _ = NSApplication.shared\nexit(runAgentPreparationContractSelfTest() ? 0 : 1)\n')
        executable = private / "contract"
        inputs = [root / "Sources/McpmeMenuBar" / name for name in (
            "Brand.swift", "WorkspaceStyle.swift", "PreparationForms.swift", "PreparationResolution.swift", "AgentProviderSettingsView.swift", "AgentPreparationView.swift")]
        commands = [("compile", ["/usr/bin/swiftc", *map(str, inputs), str(source), "-o", str(executable)]),
                    ("handlers", [str(executable)])]
        for stage, argv in commands:
            process = subprocess.Popen(argv, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(private), "TMPDIR": str(private)})
            try:
                result = process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
                print("stage=" + stage + " code=owned_deadline"); return 1
            if result != 0:
                print("stage=" + stage + " code=contract_failed"); return 1
        print("stage=complete code=agent_view_handlers_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
