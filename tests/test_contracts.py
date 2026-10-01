import math

import pytest

from jevk5_app.contracts import ServiceError, validate_decision


@pytest.mark.parametrize("question", [
    {"type": "noul", "instructions": "Is this permitted?"},
    {"type": "choice", "instructions": "Choose", "criteria": ["yes", "no"]},
    {"type": "choice", "instructions": "Choose", "criteria": {"yes": "Allowed", "no": "Denied"}},
    {"type": "score", "instructions": "Rate", "criteria": ["Low", "High"]},
])
def test_accepts_upstream_question_shapes(question):
    evidence, result = validate_decision({"public": "synthetic"}, question)
    assert evidence == {"public": "synthetic"}
    assert result == question


@pytest.mark.parametrize("state,question", [
    (True, {"type": "noul", "instructions": "x"}),
    ({"x": math.nan}, {"type": "noul", "instructions": "x"}),
    ("x", {"type": "noul", "instructions": " "}),
    ("x", {"type": "noul", "instructions": "x", "criteria": ["yes", "no"]}),
    ("x", {"type": "noul", "instructions": "x", "criteria": {"other": "x"}}),
    ("x", {"type": "choice", "instructions": "x", "criteria": ["yes", "yes"]}),
    ("x", {"type": "choice", "instructions": "x", "criteria": ["only"]}),
    ("x", {"type": "choice", "instructions": "x", "criteria": [str(i) for i in range(17)]}),
    ("x", {"type": "score", "instructions": "x", "criteria": {"0": "Low", "1": "High"}}),
    ("x", {"type": "unknown", "instructions": "x"}),
])
def test_invalid_decisions_are_sanitized(state, question):
    with pytest.raises(ServiceError) as caught:
        validate_decision(state, question)
    assert caught.value.code == "invalid_decision"
    assert caught.value.status == 400
    assert "synthetic" not in str(caught.value)
