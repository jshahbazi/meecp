"""Model-free checks for the installed Stark acceptance harness's gates.

Synthetic fixtures verify failing numerical/type/cache identities, selection
rules, aggregate timing scope and output privacy. These tests never claim the
real macOS/package/native acceptance gate has run.
"""
from __future__ import annotations

import json
import asyncio
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_stark_jevk5_integration as harness


def result(hit=False):
    answer = {"type": "noul", "noul": .25, "confidence": .75, "input_tokens": 3}
    expected = {"decision": answer, "input_tokens": 3}
    value = {"answer": dict(answer), "input_tokens": 3, "cache": {"hit": hit},
             "model": {"profile": harness.PROFILE, "context_tokens": harness.CONTEXT}}
    return value, expected


def population():
    cases, rows = [], []
    for start, tier in ((20, "easy"), (300, "original"), (1100, "hard")):
        for index in range(16):
            name = "fixture-" + str(start) + "-" + str(index)
            cases.append({"id": name, "tier": tier})
            rows.extend({"case_id": name, "condition": condition, "input_tokens": start + index}
                        for condition in harness.CONDITIONS)
    # A synthetic row cannot affect the frozen public-case quartiles.
    cases.append({"id": "fixture-synthetic", "tier": "synthetic"})
    rows.extend({"case_id": "fixture-synthetic", "condition": condition, "input_tokens": 21}
                for condition in harness.CONDITIONS)
    return cases, rows


def blocks(llama=False):
    names = ["native_before", *(["llama_before"] if llama else []), "api_after", "mcp_after"]
    return {name: [{"condition": condition, "wall_ms": float(index + 1)}
                  for index in range(12) for condition in harness.CONDITIONS] for name in names}


@pytest.mark.parametrize("left,right", [(1, 1.0), (True, 1), (0.0, -0.0), ([1.0], [1]),
                                         ({"a": [1.0]}, {"a": [1]})])
def test_exact_rejects_changed_decoded_types_or_float_bits(left, right):
    assert not harness.exact(left, right)


def test_exact_nested_native_values():
    value = {"probabilities": [.25, .75], "tokens": [1, 2], "type": "choice"}
    assert harness.exact(value, json.loads(json.dumps(value)))
    assert not harness.exact(value, {**value, "probabilities": [.75, .25]})


def test_selection_is_length_only_midpoint_quartiles():
    cases, rows = population()
    chosen = harness.selected_cases(cases, rows)
    assert [item["index"] for item in chosen] == [2, 6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46]
    assert [item["bucket"] for item in chosen] == ["short"] * 4 + ["medium"] * 4 + ["long"] * 4
    assert len({item["index"] for item in chosen}) == 12
    for row in rows:
        row["latency_ms"] = 10000 - row["input_tokens"]
        row["probabilities"] = [1.0, 0.0]
    assert chosen == harness.selected_cases(cases, list(reversed(rows)))


def test_selection_fails_when_any_public_length_bucket_is_incomplete():
    cases, rows = population()
    cases = [case for case in cases if case["tier"] != "hard"]
    with pytest.raises(harness.IntegrationFailure, match="^selection_bucket_too_small$"):
        harness.selected_cases(cases, rows)


def test_independent_calibration_preserves_fixed_temperature():
    logits = [3.0, 2.0, -1.0]
    weights = [math.exp((item - 3) / harness.TEMPERATURE) for item in logits]
    probabilities = [weight / math.fsum(weights) for weight in weights]
    assert harness.calibration_error(logits, probabilities) <= 1e-12
    wrong = [math.exp(item - 3) for item in logits]
    with pytest.raises(harness.IntegrationFailure, match="^calibration_formula_failed$"):
        harness.calibration_error(logits, [weight / math.fsum(wrong) for weight in wrong])


@pytest.mark.parametrize("probabilities", [[.1, .1], [.5, float("nan")], [1.1, -.1], [1, 0], [.5]])
def test_invalid_native_probability_vectors_fail_closed(probabilities):
    with pytest.raises(harness.IntegrationFailure, match="^probabilities_invalid$"):
        harness.calibration_error([0.0, 0.0], probabilities)


@pytest.mark.parametrize("enabled", [False, True])
def test_typed_answer_parity_checks_cache_policy_explicitly(enabled):
    value, expected = result(enabled)
    harness.validate_result(value, expected, hit=enabled)
    with pytest.raises(harness.IntegrationFailure, match="^cache_expectation_failed$"):
        harness.validate_result(value, expected, hit=not enabled)


def test_typed_answer_changed_float_or_identity_fails():
    value, expected = result()
    value["answer"]["noul"] = math.nextafter(.25, 1)
    with pytest.raises(harness.IntegrationFailure, match="^typed_answer_bit_equality_failed$"):
        harness.validate_result(value, expected)
    value, expected = result()
    value["model"]["context_tokens"] = 4096
    with pytest.raises(harness.IntegrationFailure, match="^result_identity_invalid$"):
        harness.validate_result(value, expected)


@pytest.mark.parametrize("llama", [False, True])
def test_simple_timing_snapshot_keeps_scope_and_condition_populations(llama):
    report = harness.timing_summary(blocks(llama))
    assert report["selected_cases"] == 12
    assert report["precise_performance_claim"] is False
    assert report["background_activity_controlled"] is False
    assert report["order"] == list(blocks(llama))
    assert report["results"]["fresh"]["api_after"]["samples"] == 12
    assert report["results"]["identical_repeat"]["mcp_after"]["after_over_native_before_ratio_of_means"] == 1
    assert "excludes rendering/tokenization" in report["measurement_scope"]["native_before"]


def test_timing_does_not_accept_partial_or_replaced_blocks():
    value = blocks()
    value["api_after"].pop()
    with pytest.raises(harness.IntegrationFailure, match="^timing_population_invalid$"):
        harness.timing_summary(value)
    with pytest.raises(harness.IntegrationFailure, match="^timing_blocks_invalid$"):
        harness.timing_summary({"llama_before": []})


def test_private_new_report_does_not_overwrite_or_follow_symlinks(tmp_path):
    report = tmp_path / "REPORT.json"
    harness.write_report(report, {"accepted": True, "cases": 243})
    assert report.stat().st_mode & 0o077 == 0
    assert json.loads(report.read_bytes()) == {"accepted": True, "cases": 243}
    with pytest.raises(harness.IntegrationFailure, match="^report_path_invalid$"):
        harness.write_report(report, {})
    linked = tmp_path / "linked.json"
    linked.symlink_to(report)
    with pytest.raises(harness.IntegrationFailure, match="^report_path_invalid$"):
        harness.write_report(linked, {})


def test_state_requires_a_fresh_private_directory(tmp_path):
    state = harness.new_private_directory(tmp_path / "state")
    assert state.stat().st_mode & 0o077 == 0
    with pytest.raises(harness.IntegrationFailure, match="^private_directory_exists$"):
        harness.new_private_directory(state)


async def test_http_checker_keeps_response_content_out_of_errors():
    class Client:
        async def request(self, *args, **kwargs):
            return SimpleNamespace(status_code=500, content=b"private-evidence-and-credential")
    with pytest.raises(harness.IntegrationFailure, match="^http_status_invalid$"):
        await harness.checked_json(Client(), "POST", "/fixture", payload={"private": "request"})


async def test_reset_uses_authenticated_client_and_native_ack():
    calls = []
    class Client:
        async def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return SimpleNamespace(status_code=200, content=b"{}", json=lambda: {"cache_reset": True})
    await harness.reset(Client())
    assert calls == [("POST", "/reset_cache", {"json": {}})]


def test_main_sanitizes_unexpected_exception_text(monkeypatch, tmp_path, capsys):
    async def fail(_args):
        raise RuntimeError("private-evidence-probabilities-token-api-key")
    monkeypatch.setattr(harness, "run", fail)
    arguments = []
    for flag in ("package", "host", "state", "build", "reference-directory", "native-reaper"):
        arguments.extend(["--" + flag, str(tmp_path / flag)])
    arguments.extend(["--report", str(tmp_path / "REPORT.json")])
    assert harness.main(arguments) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"accepted": False, "error": "integration_failed", "stage": "arguments"}
    assert captured.err == ""
    assert not (tmp_path / "REPORT.json").exists()


def test_main_argument_errors_are_content_free(capsys):
    assert harness.main(["--private-evidence-credential"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"accepted": False, "error": "arguments_invalid", "stage": "arguments"}
    assert captured.err == ""


def test_ownership_gate_requires_resident_gateway_and_reservation_settlement():
    host = SimpleNamespace(residents={}, reservations={}, ledgers={}, owners={}, workers={})
    harness.slot_settled(host, "fixture", gateway_stopped=True)
    host.reservations["fixture"] = {"gpu": True}
    with pytest.raises(harness.IntegrationFailure, match="^slot_resource_unsettled$"):
        harness.slot_settled(host, "fixture", gateway_stopped=True)


def test_nested_native_descendant_inventory_is_transitive():
    parents = {100: 1, 101: 100, 102: 101, 103: 102, 200: 1, 201: 200}
    assert harness.descendants(parents, {100}) == {100, 101, 102, 103}
    assert harness.descendants(parents, {100, 200}) == set(parents)


def test_bundled_interpreter_gate_does_not_accept_only_a_reaper(tmp_path, monkeypatch):
    host = tmp_path / "host"
    (host / "python/bin").mkdir(parents=True)
    (host / "bin").mkdir()
    python = host / "python/bin/python3"
    python.write_bytes(b"synthetic-runtime")
    (host / "bin/meecp-reap").write_bytes(b"synthetic-reaper")
    monkeypatch.setattr(harness.sys, "executable", "/different/interpreter")
    with pytest.raises(harness.IntegrationFailure, match="^bundled_interpreter_required$"):
        harness.host_paths(host)
    monkeypatch.setattr(harness.sys, "executable", str(python))
    assert harness.host_paths(host) == (host, python, host / "bin/meecp-reap")


def test_nested_sdk_task_groups_expose_only_authored_allowlisted_code():
    error = ExceptionGroup("private-protocol-evidence", [
        ValueError("private-model-reply-token-api-key"),
        ExceptionGroup("private-sdk-wrapper", [harness.IntegrationFailure("sdk_decision_failed")]),
    ])
    assert harness.sanitized_failure_code(error) == "sdk_decision_failed"
    assert harness.sanitized_failure_code(ExceptionGroup("private", [ValueError("private")])) == "integration_failed"


def test_failure_class_and_stage_cannot_carry_input_derived_text():
    error = harness.IntegrationFailure("private-evidence-and-api-key")
    assert str(error) == "integration_failed"
    assert harness.sanitized_failure_code(error) == "integration_failed"
    harness.set_stage("private-evidence-and-api-key")
    assert harness.CURRENT_STAGE == "arguments"
    harness.set_stage("queue_and_cancellation")
    assert harness.CURRENT_STAGE == "queue_and_cancellation"


def test_upstream_code_classes_are_not_treated_as_authored_harness_failures():
    class UpstreamFailure(ValueError):
        code = "private-model-evidence"
    assert harness.sanitized_failure_code(UpstreamFailure("private-api-key")) == "integration_failed"


def test_main_reports_nested_code_and_fixed_stage_without_exception_data(monkeypatch, tmp_path, capsys):
    async def fail(_args):
        harness.set_stage("http_mcp_sdk")
        raise ExceptionGroup("private-evidence-api-key", [RuntimeError("private-response"),
            ExceptionGroup("private-worker-wrapper", [harness.IntegrationFailure("sdk_status_invalid")])])
    monkeypatch.setattr(harness, "run", fail)
    arguments = []
    for flag in ("package", "host", "state", "build", "reference-directory", "native-reaper"):
        arguments.extend(["--" + flag, str(tmp_path / flag)])
    arguments.extend(["--report", str(tmp_path / "REPORT.json")])
    assert harness.main(arguments) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"accepted": False, "error": "sdk_status_invalid", "stage": "http_mcp_sdk"}
    assert captured.err == ""
    assert not (tmp_path / "REPORT.json").exists()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("change,code", [(None, None), ("engine", "queue_engine_identity_changed"),
                                        ("count", "queue_execution_not_preserved")])
async def test_sdk_wait_cancellation_requires_two_preserved_decisions_and_same_engine(enabled, change, code):
    value, expected = result(enabled)
    reference = {"cases": [{"state": "synthetic", "question": {"type": "noul", "instructions": "fixture"}}],
                 "worker": {"rows": [expected]}}
    posts = gets = cancelled = 0
    class Public:
        async def post(self, _path, **_kwargs):
            nonlocal posts
            posts += 1
            if 8 < posts <= 16:
                body, status = {"error": {"code": "queue_full"}}, 429
            else:
                body, status = value, 200
            return SimpleNamespace(status_code=status, content=b"{}", json=lambda: body)
        async def request(self, method, path, **kwargs):
            nonlocal gets
            if method == "POST":
                return await self.post(path, **kwargs)
            gets += 1
            await asyncio.sleep(0)
            identifier = "changed" if change == "engine" and gets >= 3 else "synthetic-instance"
            count = 101 if change == "count" else 102
            status = {"ready": True, "engine_instance_id": identifier, "generation": 7,
                      "queue_depth": 1 if gets == 2 else 0,
                      "counts": {"decisions": count if gets >= 3 else 100}}
            return SimpleNamespace(status_code=200, content=b"{}", json=lambda: status)
    class Engine:
        async def request(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200, content=b"{}", json=lambda: {"cache_reset": True})
    class Session:
        async def call_tool(self, *_args):
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise
    if code is not None:
        with pytest.raises(harness.IntegrationFailure, match="^" + code + "$"):
            await harness.queue_checks(Public(), Session(), reference, Engine(), enabled=enabled)
        return
    report = await harness.queue_checks(Public(), Session(), reference, Engine(), enabled=enabled)
    assert cancelled == 1 and posts == 17
    assert report["queue_rejections"] == 8
    assert report["sdk_client_wait_cancellations"] == 1
    assert report["server_operation_cancelled"] is False
    assert report["engine_execution_preserved"] is True
    assert report["completed_server_decisions"] == 2
    assert report["shared_engine_unchanged"] is True
    assert report["recovery_cache_hit"] is enabled
