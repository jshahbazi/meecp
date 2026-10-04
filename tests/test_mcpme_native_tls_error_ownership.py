"""Pure ABI controls for native macOS trust-evaluation error ownership."""
from __future__ import annotations

import ctypes
import platform
import ssl

import pytest


if platform.system() != "Darwin":
    pytest.skip("macOS native trust ABI only", allow_module_level=True)

from mcpme._native_tls import _macos


def _error_pointer(value: int = 1):
    return ctypes.cast(ctypes.c_void_p(value), _macos.CFErrorRef)


def test_success_with_null_error_does_not_release(monkeypatch):
    released = []

    def evaluate(_, output):
        output[0] = _macos.CFErrorRef()
        return 1

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFRelease", released.append)
    _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert released == []


def test_false_with_error_releases_error_and_description_once(monkeypatch):
    error = _error_pointer()
    description = ctypes.c_void_p(2)
    released = []

    def evaluate(_, output):
        output[0] = error
        return 0

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorGetCode", lambda _: -67808)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorCopyDescription", lambda _: description)
    monkeypatch.setattr(_macos.CoreFoundation, "CFRelease", released.append)
    monkeypatch.setattr(_macos, "_cf_string_ref_to_str", lambda _: "failure")
    monkeypatch.setattr(_macos.Security, "SecTrustGetTrustResult", lambda *_: None)
    with pytest.raises(ssl.SSLCertVerificationError):
        _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert released == [description, error]


def test_false_with_null_error_fails_closed_without_corefoundation_calls(monkeypatch):
    calls = []

    def evaluate(_, output):
        output[0] = _macos.CFErrorRef()
        return 0

    monkeypatch.setattr(_macos.Security, "SecTrustEvaluateWithError", evaluate)
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorGetCode", lambda *_: calls.append("code"))
    monkeypatch.setattr(_macos.CoreFoundation, "CFErrorCopyDescription", lambda *_: calls.append("description"))
    monkeypatch.setattr(_macos.Security, "SecTrustGetTrustResult", lambda *_: None)
    with pytest.raises(ssl.SSLCertVerificationError, match="Certificate verification failed"):
        _macos._verify_peercerts_impl_macos_10_14(ssl.create_default_context(), object())
    assert calls == []
