import importlib.util
from pathlib import Path
import plistlib
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("prepared_python_signing_builder", ROOT / "scripts/build_mcpme_app.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize("relative,release,expected", [
    ("Contents/Resources/host/preparation-toolchain/python/bin/python3.12", True, True),
    ("Contents/Resources/host/preparation-toolchain/python/bin/python3.12", False, False),
    ("Contents/MacOS/mcpme", True, False),
    ("Contents/Resources/host/python/bin/python3.12", True, False),
    ("Contents/Resources/host/preparation-toolchain/python/bin/python3", True, False),
    ("Contents/Resources/host/preparation-toolchain/python/lib/libpython3.12.dylib", True, False),
    ("Contents/Resources/host/preparation-toolchain/textgen-runtime/python/bin/python3.12", True, False),
    ("Contents/Resources/host/bin/mcpme-handoff", True, False),
])
def test_library_loading_exception_is_scoped_to_preparation_interpreter(tmp_path, relative, release, expected):
    result = builder.prepared_python_entitlements(tmp_path, tmp_path / relative, release=release)
    assert (result == builder.PREPARED_PYTHON_ENTITLEMENTS) is expected


def test_prepared_python_policy_has_only_the_required_library_loading_exception():
    assert plistlib.loads(builder.PREPARED_PYTHON_ENTITLEMENTS.read_bytes()) == {
        "com.apple.security.cs.disable-library-validation": True}


def test_interpreter_keeps_hardened_runtime_timestamp_and_explicit_entitlements():
    args = builder.signing_arguments("selected-certificate", True, entitlements=builder.PREPARED_PYTHON_ENTITLEMENTS)
    assert "--timestamp" in args and args[args.index("--options") + 1] == "runtime"
    assert args[args.index("--entitlements") + 1] == str(builder.PREPARED_PYTHON_ENTITLEMENTS)
    assert "--entitlements" not in builder.signing_arguments("selected-certificate", True)
    assert "--entitlements" not in builder.signing_arguments("-", False,
        entitlements=builder.PREPARED_PYTHON_ENTITLEMENTS)


def test_runtime_signing_applies_exception_to_only_prepared_python(monkeypatch, tmp_path):
    prepared = tmp_path / "Contents/Resources/host/preparation-toolchain/python/bin/python3.12"
    host_sdk = tmp_path / "Contents/Resources/host/python/bin/python3.12"
    app = tmp_path / "Contents/MacOS/mcpme"
    for path in (prepared, host_sdk, app):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic native file")
    calls = []

    def run(*args):
        calls.append(args)
        return "synthetic:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)" if args[0] == "/usr/bin/otool" else ""

    monkeypatch.setattr(builder, "run", run)
    assert builder.sign_runtime(tmp_path, identity="A" * 40, release=True) == 3
    signed = {args[-1]: args for args in calls if "--force" in args}
    assert "--entitlements" in signed[str(prepared)]
    assert "--entitlements" not in signed[str(host_sdk)]
    assert "--entitlements" not in signed[str(app)]
    assert all("--timestamp" in args and "runtime" in args for args in signed.values())
