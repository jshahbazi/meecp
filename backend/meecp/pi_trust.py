"""Export public macOS trust anchors for otherwise confined command clients.

SecTrustCopyAnchorCertificates returns the caller's default trusted root set;
this module neither enumerates stored identities nor accesses private keys.
https://developer.apple.com/documentation/security/sectrustcopyanchorcertificates(_:)
"""

import ctypes
import platform
import ssl

MAX_ANCHORS = 512
MAX_CERTIFICATE_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 4 * 1024 * 1024


class NativeTrustError(ValueError):
    def __init__(self):
        super().__init__("native_trust_unavailable")


def native_anchor_pem() -> str:
    """Copy only certificate DER from the native trusted-anchor API, bounded."""
    if platform.system() != "Darwin":
        raise NativeTrustError()
    anchors = ctypes.c_void_p()
    foundation = None
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        security.SecTrustCopyAnchorCertificates.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        security.SecTrustCopyAnchorCertificates.restype = ctypes.c_int32
        security.SecCertificateCopyData.argtypes = [ctypes.c_void_p]
        security.SecCertificateCopyData.restype = ctypes.c_void_p
        foundation.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        foundation.CFArrayGetCount.restype = ctypes.c_long
        foundation.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        foundation.CFDataGetLength.argtypes = [ctypes.c_void_p]
        foundation.CFDataGetLength.restype = ctypes.c_long
        foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        foundation.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
        foundation.CFRelease.argtypes = [ctypes.c_void_p]
        foundation.CFRelease.restype = None
        if security.SecTrustCopyAnchorCertificates(ctypes.byref(anchors)) != 0 or not anchors:
            raise NativeTrustError()
        count = foundation.CFArrayGetCount(anchors)
        if not 1 <= count <= MAX_ANCHORS:
            raise NativeTrustError()
        blocks, size = [], 0
        for index in range(count):
            certificate = foundation.CFArrayGetValueAtIndex(anchors, index)
            if not certificate:
                raise NativeTrustError()
            data = security.SecCertificateCopyData(certificate)
            if not data:
                raise NativeTrustError()
            try:
                length = foundation.CFDataGetLength(data)
                contents = foundation.CFDataGetBytePtr(data)
                if not 1 <= length <= MAX_CERTIFICATE_BYTES or not contents:
                    raise NativeTrustError()
                block = ssl.DER_cert_to_PEM_cert(ctypes.string_at(contents, length))
                size += len(block)
                if size > MAX_BUNDLE_BYTES:
                    raise NativeTrustError()
                blocks.append(block)
            finally:
                foundation.CFRelease(data)
        return "".join(blocks)
    except (OSError, AttributeError, ValueError):
        raise NativeTrustError() from None
    finally:
        if anchors and foundation is not None:
            foundation.CFRelease(anchors)
