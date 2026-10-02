import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError

import pytest

from jevk5_app.contracts import ServiceError
from jevk5_app.scorer import LocalScorer


QUESTION = {"type": "choice", "instructions": "Choose", "criteria": ["allow", "deny"]}


class FakeScorer(LocalScorer):
    def __init__(self):
        super().__init__("http://127.0.0.1:1", "private-key", 8192, temperature=1.22, knockout_temperature=0.93)
        self.ids = [1, 2, 3]
        self.requests = []
        self.response = {"truncated": False, "completion_probabilities": [{"top_logprobs": [
            {"token": "A", "logprob": -0.1}, {"token": "B", "logprob": -2.0}]}],
            "timings": {"prompt_ms": 10, "predicted_ms": 1}}
        self.fail = False

    def _transport(self, path, payload):
        self.requests.append((path, payload))
        if self.fail:
            raise RuntimeError("secret evidence must not escape")
        return {"tokens": list(self.ids)} if path == "/tokenize" else self.response


def test_exact_consecutive_reuse_and_logical_tokens():
    scorer = FakeScorer()
    first = scorer.decide("synthetic one", QUESTION)
    second = scorer.decide("synthetic one", QUESTION)
    scorer.ids = [1, 2, 4]
    scorer.decide("synthetic two", QUESTION)
    scorer.ids = [1, 2, 3]
    scorer.decide("synthetic one", QUESTION)
    completions = [p for path, p in scorer.requests if path == "/completion"]
    assert [p["cache_prompt"] for p in completions] == [False, True, False, False]
    assert first == second
    assert first["input_tokens"] == 3


def test_error_clears_previous_identity_and_does_not_echo():
    scorer = FakeScorer()
    scorer.decide("public evidence", QUESTION)
    scorer.fail = True
    with pytest.raises(ServiceError) as caught:
        scorer.decide("private contents", QUESTION)
    assert "secret" not in str(caught.value)
    assert scorer.previous_ids is None
    scorer.fail = False
    scorer.decide("public evidence", QUESTION)
    assert not scorer.cache_hit


@pytest.mark.parametrize("change,expected", [
    (lambda s: s.ids.extend([1] * 8190), "context_limit"),
    (lambda s: s.response.update(truncated=True), "truncated_prompt"),
    (lambda s: s.response.pop("truncated"), "truncated_prompt"),
    (lambda s: s.response["completion_probabilities"][0]["top_logprobs"].pop(), "incomplete_options"),
])
def test_rejects_truncation_context_and_missing_option(change, expected):
    scorer = FakeScorer()
    change(scorer)
    with pytest.raises(ServiceError) as caught:
        scorer.decide("public evidence", QUESTION)
    assert caught.value.code == expected
    assert scorer.previous_ids is None


def test_tokenization_preserves_pinned_flags():
    scorer = FakeScorer()
    scorer.decide("public evidence", QUESTION)
    assert scorer.requests[0][1]["add_special"] is False
    assert scorer.requests[0][1]["parse_special"] is True


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8080", "http://localhost:8080", "http://user@127.0.0.1:8080",
    "http://127.0.0.1:8080/model", "http://127.0.0.1:8080/?secret=1",
    "http://127.0.0.1:8080/#secret", "http://127.0.0.1:0", "http://127.0.0.1:65536",
])
def test_endpoint_must_be_strict_loopback(url):
    with pytest.raises(ValueError):
        LocalScorer(url, "key", 8192)


def test_direct_loopback_ignores_proxy_and_rejects_redirect(monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            redirect = json.loads(self.rfile.read(length) or b"{}").get("redirect")
            if self.path == "/tokenize":
                if redirect:
                    self.send_response(302); self.send_header("Location", "/completion"); self.end_headers(); return
                body = {"tokens": [1]}
            elif self.path == "/completion":
                body = {"truncated": False, "completion_probabilities": [{"top_logprobs": []}]}
            else:
                self.send_response(302); self.send_header("Location", "/tokenize"); self.end_headers(); return
            encoded = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(encoded)))
            self.end_headers(); self.wfile.write(encoded)
        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        discovered = []
        monkeypatch.setattr("urllib.request.getproxies", lambda: discovered.append(True) or {"http": "http://127.0.0.1:1"})
        scorer = LocalScorer(f"http://127.0.0.1:{server.server_port}/", "key", 8192)
        assert scorer._transport("/tokenize", {"content": "x"}) == {"tokens": [1]}
        with pytest.raises(HTTPError) as caught:
            scorer._transport("/tokenize", {"redirect": True})
        assert caught.value.code == 302
        assert discovered == []
        assert calls == ["/tokenize", "/tokenize"]
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close()
