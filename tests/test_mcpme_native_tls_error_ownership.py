"""Pure ABI controls for native macOS trust-evaluation error ownership."""
from __future__ import annotations

import ctypes
import platform
import ssl
import types

import pytest


if platform.system() != "Darwin":
    pytest.skip("macOS native trust ABI only", allow_module_level=True)

from mcpme._native_tls import _macos


@pytest.fixture(autouse=True)
def fake_native_frameworks(monkeypatch):
    def forbidden(*_):
        raise AssertionError("unexpected native framework call")

    core = types.SimpleNamespace(
        CFErrorRef=_macos.CFErrorRef,
        CFErrorGetCode=forbidden,
        CFErrorCopyDescription=forbidden,
        CFRelease=forbidden,
        CFStringGetCStringPtr=forbidden,
        CFStringGetCString=forbidden,
    )
    security = types.SimpleNamespace(
        SecTrustResultType=ctypes.c_uint32,
        SecTrustEvaluateWithError=forbidden,
        SecTrustGetTrustResult=forbidden,
    )
    monkeypatch.setattr(_macos, "CoreFoundation", core)
    monkeypatch.setattr(_macos, "Security", security)
    assert not isinstance(core, ctypes.CDLL)
    assert not isinstance(security, ctypes.CDLL)


def _error_pointer(value: int = 1):
    return ctypes.cast(ctypes.c_void_p(value), _macos.CFErrorRef)


def _set_error(output, value):
    ctypes.cast(output, ctypes.POINTER(_macos.CFErrorRef))[0] = value


def test_success_with_null_error_does_not_release(monkeypatch):
    released = []

    def evaluate(_, output):
        _set_error(output, _macos.CFErrorRef())
        return 1

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFRelease", lambda ref: released.append(ctypes.cast(ref, ctypes.c_void_p).value))
    _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert released == []


def test_false_with_error_releases_error_and_description_once(monkeypatch):
    error = _error_pointer()
    description = ctypes.c_void_p(2)
    released = []

    def evaluate(_, output):
        _set_error(output, error)
        return 0

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorGetCode", lambda _: -67808)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorCopyDescription", lambda _: description)
    monkeypatch.setattr(_macos.CoreFoundation, "CFRelease", lambda ref: released.append(ctypes.cast(ref, ctypes.c_void_p).value))
    monkeypatch.setattr(_macos, "_cf_string_ref_to_str", lambda _: "failure")
    monkeypatch.setattr(_macos.Security, "SecTrustGetTrustResult", lambda *_: None)
    with pytest.raises(ssl.SSLCertVerificationError):
        _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert released == [2, 1]


def test_false_with_null_error_fails_closed_without_corefoundation_calls(monkeypatch):
    calls = []

    def evaluate(_, output):
        _set_error(output, _macos.CFErrorRef())
        return 0

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorGetCode", lambda *_: calls.append("code"))
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorCopyDescription", lambda *_: calls.append("description"))
    monkeypatch.setattr(_macos.Security, "SecTrustGetTrustResult", lambda *_: None)
    with pytest.raises(ssl.SSLCertVerificationError, match="Certificate verification failed"):
        _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert calls == []


@pytest.mark.parametrize("stage", ["evaluate", "code", "description", "conversion", "trust_result"])
def test_error_pointer_is_released_when_any_failure_stage_raises(monkeypatch, stage):
    error = _error_pointer()
    released = []

    def evaluate(_, output):
        _set_error(output, error)
        if stage == "evaluate":
            raise RuntimeError("evaluate")
        return 0

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorGetCode", lambda _: (_ for _ in ()).throw(RuntimeError("code")) if stage == "code" else -67808)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorCopyDescription", lambda _: (_ for _ in ()).throw(RuntimeError("description")) if stage == "description" else ctypes.c_void_p(2))
    monkeypatch.setattr(_macos, "_cf_string_ref_to_str", lambda _: (_ for _ in ()).throw(RuntimeError("conversion")) if stage == "conversion" else "failure")
    monkeypatch.setattr(_macos.Security, "SecTrustGetTrustResult", lambda *_: (_ for _ in ()).throw(RuntimeError("trust_result")) if stage == "trust_result" else None)
    monkeypatch.setattr(_macos.CoreFoundation, "CFRelease", lambda ref: released.append(ctypes.cast(ref, ctypes.c_void_p).value))
    with pytest.raises(RuntimeError):
        _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert released == [1] if stage == "evaluate" else [2, 1]


def test_cert_none_known_error_still_releases_error(monkeypatch):
    error = _error_pointer()
    released = []

    def evaluate(_, output):
        _set_error(output, error)
        return 0

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorGetCode", lambda _: _macos.CFConst.errSecNotTrusted)
    monkeypatch.setattr(_macos.CoreFoundation, "CFRelease", lambda ref: released.append(ctypes.cast(ref, ctypes.c_void_p).value))
    _macos._verify_peercerts_impl_macos_10_14(context, object())
    assert released == [1]


def test_unexpected_result_still_releases_error(monkeypatch):
    error = _error_pointer()
    released = []

    def evaluate(_, output):
        _set_error(output, error)
        return 2

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFRelease", lambda ref: released.append(ctypes.cast(ref, ctypes.c_void_p).value))
    with pytest.raises(ssl.SSLError, match="Unknown result"):
        _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert released == [1]
