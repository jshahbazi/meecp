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
