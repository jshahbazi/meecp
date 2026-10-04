"""Build a local native playground using the already installed model/runtime."""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT.parent.parent / '.build/bonsai-playground')
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    app = args.output.resolve() / 'Bonsai Playground.app'
    contents = app / 'Contents'
    binary = contents / 'MacOS' / 'BonsaiPlayground'
    binary.parent.mkdir(parents=True, exist_ok=True)
    (contents / 'Resources').mkdir(exist_ok=True)
    subprocess.run([
        '/usr/bin/swiftc', '-O', '-swift-version', '5', '-parse-as-library',
        '-target', 'arm64-apple-macos14.0', '-framework', 'AppKit', '-framework', 'Foundation',
        str(ROOT / 'BonsaiPlayground.swift'), str(ROOT / 'Conversation.swift'),
        str(ROOT / 'SystemPromptEditor.swift'), '-o', str(binary),
    ], check=True)
    info = {
        'CFBundleName': 'Bonsai Playground',
        'CFBundleDisplayName': 'Bonsai Playground',
        'CFBundleIdentifier': 'dev.mcpme.bonsai-playground',
        'CFBundleExecutable': 'BonsaiPlayground',
        'CFBundlePackageType': 'APPL',
        'CFBundleVersion': '2',
        'CFBundleShortVersionString': '0.2.0',
        'LSMinimumSystemVersion': '14.0',
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
        'NSSupportsAutomaticTermination': False,
    }
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)], check=True)
    print(str(app))
    if args.open:
        subprocess.run(['/usr/bin/open', '-n', str(app)], check=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError):
        print('Playground build failed; check the compiler diagnostics and local build tools.')
        raise SystemExit(1)
