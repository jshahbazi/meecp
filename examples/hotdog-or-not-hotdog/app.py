"""Authenticated loopback photo-upload application using the standard library."""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import signal
import threading
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from detector import MAX_BYTES, ROOT, DetectorError, HotdogDetector


class PhotoServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), PhotoHandler)
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.detector = HotdogDetector()
        self.detector.load()
        self.page = (ROOT / "index.html").read_bytes()
        self.admission = threading.BoundedSemaphore(1)
        self.bootstrap_available = True
        self.bootstrap_lock = threading.Lock()

    @property
    def launch_url(self) -> str:
        return f"{self.origin}/?bootstrap={self.token}"

    def handle_error(self, request, client_address):
        pass


class PhotoHandler(BaseHTTPRequestHandler):
    server_version = "NotHotdog"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def reply(self, status: int, payload: bytes, kind: str = "application/json", headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def error(self, status: int, code: str):
        self.close_connection = True
        self.reply(status, json.dumps({"error": code}).encode())

    def authorized(self, allow_bootstrap: bool = False) -> bool:
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            self.error(403, "invalid_host")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self.error(403, "invalid_origin")
            return False
        if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
            self.error(403, "invalid_origin")
            return False
        if allow_bootstrap:
            query = parse_qs(urlsplit(self.path).query)
            supplied = query.get("bootstrap", [""])[0]
            with self.server.bootstrap_lock:
                if self.server.bootstrap_available and hmac.compare_digest(supplied.encode(), self.server.token.encode()):
                    self.server.bootstrap_available = False
                    self.reply(303, b"", headers={"Location": "/", "Set-Cookie":
                               f"session={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
                    return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            supplied = cookie["session"].value if "session" in cookie else ""
        except CookieError:
            supplied = ""
        if not hmac.compare_digest(supplied.encode(), self.server.token.encode()):
            self.error(401, "unauthorized")
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.authorized(allow_bootstrap=path == "/"):
            return
        if path == "/":
            self.reply(200, self.server.page, "text/html; charset=utf-8")
        elif path == "/api/status":
            self.reply(200, json.dumps({"ready": True, "model": "YOLO11n / COCO / ONNX CPU"}).encode())
        else:
            self.error(404, "not_found")

    def do_POST(self):
        if not self.authorized():
            return
        if self.headers.get("Origin") != self.server.origin:
            self.error(403, "invalid_origin")
            return
        if self.path != "/api/detect":
            self.error(404, "not_found")
            return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            self.error(400, "invalid_body")
            return
        if self.headers.get("Content-Type") != "application/octet-stream":
            self.error(415, "unsupported_content_type")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.error(400, "invalid_body")
            return
        if not 0 < length <= MAX_BYTES:
            self.error(413, "image_size_limit")
            return
        if not self.server.admission.acquire(blocking=False):
            self.error(429, "detector_busy")
            return
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                self.error(400, "invalid_body")
                return
            result = self.server.detector.detect(data, self.headers.get("X-Confidence", "0.35"))
            self.reply(200, json.dumps(result).encode())
        except DetectorError as error:
            self.error(400, str(error))
        except Exception:
            self.error(500, "detection_failed")
        finally:
            self.server.admission.release()

    def do_OPTIONS(self):
        if self.authorized():
            self.error(405, "method_not_allowed")

    do_HEAD = do_OPTIONS
    do_PUT = do_OPTIONS
    do_PATCH = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_TRACE = do_OPTIONS

    def send_error(self, code, message=None, explain=None):
        if code == 501 and not self.authorized():
            return
        self.error(code, "invalid_request")


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the local Not Hotdog photo app.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = PhotoServer(args.port)
    except (DetectorError, OSError):
        print("App unavailable. Run Setup.command in this folder and try again.")
        return 1
    print("Not Hotdog is running locally. Close this terminal or press Control-C to stop.")
    if not args.no_browser:
        webbrowser.open(server.launch_url)
    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
