import importlib.util
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prompt_lab", ROOT / "scripts/test_preparation_prompts.py")
LAB = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LAB)


def case(expected, schema=None):
    return {
        "id": "case",
        "prompt": "Return the answer.",
        "schema": schema or {"type": "object", "properties": {"answer": {"type": "string"}},
                              "required": ["answer"], "additionalProperties": False},
        "expected": expected,
    }


def test_prompt_comparison_keeps_inputs_schema_and_gold_fixed():
    old = json.loads((LAB.FIXTURES / "cases_with_placeholders.json").read_text())
    new = json.loads((LAB.FIXTURES / "cases.json").read_text())
    assert len(old) == len(new) == 12
    for before, after in zip(old, new):
        assert {k: v for k, v in before.items() if k != "prompt"} == {k: v for k, v in after.items() if k != "prompt"}
        question, marker, _ = before["prompt"].partition("\n\nReturn this shape:")
        revised_question, revised_marker, _ = after["prompt"].partition("\n\nReturn only")
        assert marker and revised_marker and question == revised_question


@pytest.mark.parametrize("response", [
    ' {"answer":"ok"} trailing',
    '```json\n{"answer":"ok"}\n```',
    '{"answer":"ok","extra":1}',
    '{"answer":"ok","answer":"ok"}',
    '{"answer":NaN}',
    '{"answer":1e999}',
])
def test_grader_rejects_non_exact_json(response):
    result = LAB.grade(response, case({"answer": "ok"}))
    assert result["passed"] is False
    assert result["failure"] in {"invalid_json", "schema_mismatch", "wrong_answer"}


def test_grader_preserves_list_order_and_rejects_boolean_as_number():
    fixture = case({"values": [1, 2]}, {
        "type": "object", "properties": {"values": {"type": "array", "items": {"type": "number"}}},
        "required": ["values"], "additionalProperties": False,
    })
    assert LAB.grade('{"values":[2,1]}', fixture)["failure"] == "wrong_answer"
    numeric = case({"value": 1}, {
        "type": "object", "properties": {"value": {"type": "number"}},
        "required": ["value"], "additionalProperties": False,
    })
    assert LAB.grade('{"value":true}', numeric)["passed"] is False


@pytest.mark.parametrize("keyword", ["enum", "const"])
def test_load_cases_rejects_answer_leaking_schema(keyword):
    schema = {"type": "object", "properties": {"answer": {"type": "string", keyword: ["ok"] if keyword == "enum" else "ok"}},
              "required": ["answer"], "additionalProperties": False}
    with pytest.raises(LAB.LabError, match="schema_must_describe_shape_only"):
        LAB.load_cases(json.dumps([case({"answer": "ok"}, schema)]))


def test_infer_timeout_kills_process_group(tmp_path, monkeypatch):
    pidfile = tmp_path / "worker.pid"
    worker = tmp_path / "engine"
    worker.write_text("#!/usr/bin/env python3\nimport os,time\nfrom pathlib import Path\nPath(os.environ['PIDFILE']).write_text(str(os.getpid()))\ntime.sleep(60)\n")
    worker.chmod(0o700)
    args = SimpleNamespace(engine=worker, model=tmp_path / "model", context=128, tokens=64,
                           seed=1, temperature=0.0, timeout=0.3)
    monkeypatch.setenv("PIDFILE", str(pidfile))
    _, metadata = LAB.infer("{}", args, None)
    assert metadata["runtime_failure"] == "timeout"
    pid = int(pidfile.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed out worker survived process-group cleanup")


def test_run_report_contains_metadata_only(monkeypatch, tmp_path, capsys):
    cases = json.dumps([case({"answer": "ok"})])
    cases_path = tmp_path / "cases.json"
    system_path = tmp_path / "system.txt"
    model = tmp_path / "model"
    engine = tmp_path / "engine"
    cases_path.write_text(cases)
    system_path.write_text("system")
    model.write_bytes(b"model")
    engine.write_bytes(b"engine")
    monkeypatch.setattr(LAB, "preflight", lambda args: {"model_sha256": "m", "engine_sha256": "e", "engine_build": "b"})
    monkeypatch.setattr(LAB, "infer", lambda rendered, args, schema: ('{"answer":"ok"}', {"runtime_ok": True, "wall_seconds": 0.01}))
    output = tmp_path / "report.json"
    assert LAB.main(["--cases", str(cases_path), "--system-file", str(system_path), "run", "--model", str(model),
                     "--engine", str(engine), "--mode", "plain", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert "response" not in json.dumps(report)
    assert '"answer":"ok"' not in output.read_text()
    assert '"answer":"ok"' not in capsys.readouterr().out


def test_prompt_override_does_not_change_gold(monkeypatch, tmp_path):
    cases_path = tmp_path / "cases.json"
    system_path = tmp_path / "system.txt"
    override = tmp_path / "override.txt"
    cases_path.write_text(json.dumps([case({"answer": "gold"})]))
    system_path.write_text("system")
    override.write_text("Return a different answer.")
    monkeypatch.setattr(LAB, "preflight", lambda args: {"model_sha256": "m", "engine_sha256": "e", "engine_build": "b"})
    monkeypatch.setattr(LAB, "infer", lambda rendered, args, schema: ('{"answer":"wrong"}', {"runtime_ok": True, "wall_seconds": 0.01}))
    assert LAB.main(["--cases", str(cases_path), "--system-file", str(system_path), "run", "--model", str(tmp_path / "m"),
                     "--engine", str(tmp_path / "e"), "--prompt-file", str(override), "--mode", "plain"]) == 1


def test_paired_modes_share_seed_and_repetitions_increment(monkeypatch, tmp_path):
    cases_path = tmp_path / "cases.json"
    system_path = tmp_path / "system.txt"
    cases_path.write_text(json.dumps([case({"answer": "ok"})]))
    system_path.write_text("system")
    monkeypatch.setattr(LAB, "preflight", lambda args: {"model_sha256": "m", "engine_sha256": "e", "engine_build": "b"})
    seen = []
    def fake_infer(rendered, args, schema):
        seen.append((args.seed, schema is not None))
        return '{"answer":"ok"}', {"runtime_ok": True, "wall_seconds": 0.01}
    monkeypatch.setattr(LAB, "infer", fake_infer)
    assert LAB.main(["--cases", str(cases_path), "--system-file", str(system_path), "run", "--model", str(tmp_path / "m"),
                     "--engine", str(tmp_path / "e"), "--repeat", "2", "--seed", "10"]) == 0
    assert seen == [(10, False), (10, True), (11, False), (11, True)]


def test_invalid_settings_are_rejected_before_preflight(monkeypatch, tmp_path):
    cases_path = tmp_path / "cases.json"
    system_path = tmp_path / "system.txt"
    cases_path.write_text(json.dumps([case({"answer": "ok"})]))
    system_path.write_text("system")
    monkeypatch.setattr(LAB, "preflight", lambda args: (_ for _ in ()).throw(AssertionError("preflight called")))
    with pytest.raises(LAB.LabError, match="invalid_run_settings"):
        LAB.main(["--cases", str(cases_path), "--system-file", str(system_path), "run", "--repeat", "0"])
