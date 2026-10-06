"""Public default discovery and private request-only cases keep distinct scope."""
import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_phase3_public_release_native_agent")


# Literal combined policy for TextGen 1.0.12 and service connector 1.0.0.
# Keep its real ComponentReference shape and producer hashes in this regression.
ROOT_POLICY_JSON = '''{
  "schema_version": 1,
  "policy_id": "phase3-initial-families-v1",
  "preparation": {
    "families": [
      {
        "id": "resolved-python-v1",
        "version": "1.0.1",
        "digest": "3c24410b46415cb83b9d945ebf0049f552e9f7493943164f311939a79b49000e"
      },
      {
        "id": "yolo11-coco-detect-v1",
        "version": "1.0.0",
        "digest": "b48bde76ca30787de2e41a6b984c0c80a79b3a4b6ec75637f3147ccfadec3e99"
      },
      {
        "id": "host-command-json-v1",
        "version": "1.0.1",
        "digest": "a216f2ed58294410850afc4bd63f9b9b63c945464a1f10d1b735bd8de26cd607"
      },
      {
        "id": "text-generate-v1",
        "version": "1.0.12",
        "digest": "60ee19b6a5989e75481f1886d85b545f2f7948eca3496c0ab8b3cc3cb0d0fa2d"
      },
      {
        "id": "service-text-generate-v1",
        "version": "1.0.0",
        "digest": "bc540561a87cba329d1d62d837596d6318a2d740fc4f6cf49b3eab95a9145d75"
      }
    ],
    "recipe_component_sets": [
      [
        {
          "id": "python-function-v1",
          "version": "1.0.0",
          "digest": "b28f98582aeff09e344b4a7049af7fdadbaf19436dc6724638ab64b6a9139d4e"
        }
      ],
      [
        {
          "id": "resolved-python-v1",
          "version": "1.0.1",
          "digest": "3c24410b46415cb83b9d945ebf0049f552e9f7493943164f311939a79b49000e"
        }
      ],
      [
        {
          "id": "onnxruntime-cpu-v1",
          "version": "1.0.0",
          "digest": "ec982f8489de85afd79d6aa715431082e1bba29be48ca06eef457dbcc362d8dc"
        },
        {
          "id": "yolo11-coco-detect-v1",
          "version": "1.0.0",
          "digest": "b48bde76ca30787de2e41a6b984c0c80a79b3a4b6ec75637f3147ccfadec3e99"
        }
      ],
      [
        {
          "id": "host-command-json-v1",
          "version": "1.0.1",
          "digest": "a216f2ed58294410850afc4bd63f9b9b63c945464a1f10d1b735bd8de26cd607"
        }
      ],
      [
        {
          "id": "llamacpp-cpu-v1",
          "version": "1.0.12",
          "digest": "7800e0d271ee220b6feff59b9df1959c22387d57b5485414473c302ba8c502fa"
        },
        {
          "id": "text-generate-v1",
          "version": "1.0.12",
          "digest": "60ee19b6a5989e75481f1886d85b545f2f7948eca3496c0ab8b3cc3cb0d0fa2d"
        }
      ],
      [
        {
          "id": "openai-chat-completions-v1",
          "version": "1.0.0",
          "digest": "01d9379a0ed5fdd9fc86e1b6aeb47465f153162af57157cfbd42f6644f338587"
        },
        {
          "id": "loopback-http-bearer-v1",
          "version": "1.0.0",
          "digest": "2f93a9f9258fa5d2b6b9c854f9e828d2f43ceeb7f0108183759e9f428fbaa4dc"
        },
        {
          "id": "service-text-generate-v1",
          "version": "1.0.0",
          "digest": "bc540561a87cba329d1d62d837596d6318a2d740fc4f6cf49b3eab95a9145d75"
        }
      ]
    ],
    "resolution_candidate_kinds": [
      "python-callable",
      "python-mcp"
    ]
  },
  "command": {
    "component": [
      "host-command-json-v1",
      "1.0.1",
      "a216f2ed58294410850afc4bd63f9b9b63c945464a1f10d1b735bd8de26cd607"
    ],
    "installed_platform_policy": "3898759c85d08268db02963c0e822e2bd3c9657d337e1e5c5708e0bb0738c83b",
    "resource_platform_policy": "03a5e27b4931b1eaa705f0a4be373bc202ac7dd3bb4d71a438c2eb6ffeae1b8a"
  },
  "agent": {
    "components": [],
    "providers": [],
    "unlocked_resolution": false
  }
}
'''
ROOT_POLICY = json.loads(ROOT_POLICY_JSON)


def references():
    from mcpme.preparation_components import ComponentReference
    from mcpme.preparation_textgen import ENGINE_ID, TASK_ID
    keys = {item["id"]: ComponentReference.model_validate(item).key
        for group in ROOT_POLICY["preparation"]["recipe_component_sets"] for item in group}
    return keys[ENGINE_ID], keys[TASK_ID]


ENGINE, TASK = references()


@pytest.mark.parametrize("damage", [None, "null-agent", "missing-agent", "extra-agent", "integer-flag",
    "enabled-agent", "provider", "component", "extra-policy", "extra-preparation", "schema-bool",
    "bad-reference", "bad-command", "missing-command", "duplicate-closure"])
def test_actual_candidate_policy_requires_typed_exact_closed_record(driver, damage):
    value = copy.deepcopy(ROOT_POLICY)
    if damage == "null-agent": value["agent"] = None
    elif damage == "missing-agent": value.pop("agent")
    elif damage == "extra-agent": value["agent"]["extra"] = False
    elif damage == "integer-flag": value["agent"]["unlocked_resolution"] = 0
    elif damage == "enabled-agent": value["agent"]["unlocked_resolution"] = True
    elif damage == "provider": value["agent"]["providers"] = [{}]
    elif damage == "component": value["agent"]["components"] = [{}]
    elif damage == "extra-policy": value["extra"] = False
    elif damage == "extra-preparation": value["preparation"]["extra"] = False
    elif damage == "schema-bool": value["schema_version"] = True
    elif damage == "bad-reference": value["preparation"]["families"][0]["digest"] = "invalid"
    elif damage == "bad-command": value["command"]["component"][0] = "other-command-v1"
    elif damage == "missing-command": value.pop("command")
    elif damage == "duplicate-closure": value["preparation"]["recipe_component_sets"].append(
        value["preparation"]["recipe_component_sets"][0])
    if damage is None:
        policy = driver.closed_release_policy(value)
        assert policy.model_dump(mode="json") == ROOT_POLICY
        assert policy.admissions()[1] is None
        assert driver.combined_capabilities(capabilities(driver), *references(), policy) == "a" * 64
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.closed_release_policy(value)


@pytest.mark.parametrize("previous_version", [None, "1.0.9", "1.0.10", "1.0.11", "1.0.13"])
def test_textgen_keys_bind_actual_current_producer_to_literal_policy(driver, monkeypatch, previous_version):
    from mcpme import preparation_textgen
    assert ENGINE[0] == preparation_textgen.ENGINE_ID == "llamacpp-cpu-v1"
    assert TASK[0] == preparation_textgen.TASK_ID == "text-generate-v1"
    assert preparation_textgen.VERSION == "1.0.19"
    if previous_version is None:
        with pytest.raises(driver.common.AcceptanceError, match="combined_textgen_version_changed"):
            driver.textgen_keys()
    else:
        monkeypatch.setattr(preparation_textgen, "VERSION", previous_version)
        with pytest.raises(driver.common.AcceptanceError, match="combined_textgen_version_changed"):
            driver.textgen_keys()


def test_historical_driver_accepts_only_explicit_frozen_12_refs(driver, monkeypatch):
    from types import SimpleNamespace
    from mcpme import preparation_textgen
    monkeypatch.setattr(preparation_textgen, "VERSION", "1.0.12")
    monkeypatch.setattr(preparation_textgen, "references", lambda: [
        SimpleNamespace(id=ENGINE[0], key=ENGINE), SimpleNamespace(id=TASK[0], key=TASK)])
    assert driver.textgen_keys() == (ENGINE, TASK)


def test_historical_policy_literal_remains_frozen():
    assert hashlib.sha256(ROOT_POLICY_JSON.encode()).hexdigest() == (
        "c8fa615def941e66e85a68ac0e7cc46dd7caf1797918fc03fdd5277ac2326522")
    assert json.loads(ROOT_POLICY_JSON)["agent"] == {
        "components": [], "providers": [], "unlocked_resolution": False}


def test_historical13_policy_changes_only_textgen_references():
    selected12 = json.loads(selected12_fixture_bytes())
    expected = copy.deepcopy(selected12)
    expected["policy_id"] = "phase3-live-agent-acceptance-candidate-v1"
    expected["preparation"]["families"][3] = {"id": "text-generate-v1", "version": "1.0.13",
        "digest": "936c30b0ede738344713d37d62cb71dbfea59b7375dfa24ea023d9693bf9274a"}
    expected["preparation"]["recipe_component_sets"][4] = [
        {"id": "llamacpp-cpu-v1", "version": "1.0.13",
         "digest": "899aa082f51bbe079968ce4540fa801215adcb9f8a2fc622844e4e63ee133c9e"},
        {"id": "text-generate-v1", "version": "1.0.13",
         "digest": "936c30b0ede738344713d37d62cb71dbfea59b7375dfa24ea023d9693bf9274a"}]
    assert json.loads(historical13_fixture_bytes()) == expected


def test_historical15_policy_changes_only_textgen_references():
    from mcpme.preparation_release_policy import POLICY_PATH
    expected = json.loads(historical14_fixture_bytes())
    expected["preparation"]["families"][3] = {"id": "text-generate-v1", "version": "1.0.15",
        "digest": "3914324a17e9cbf5b5e12212cb0142bc53725e19d8a6ae9dc613da1ccea2dc83"}
    expected["preparation"]["recipe_component_sets"][4] = [
        {"id": "llamacpp-cpu-v1", "version": "1.0.15",
         "digest": "145c1d8b5a6fcb0926415cb456127f15afe6353c7e2aedfab0184c3e09796720"},
        {"id": "text-generate-v1", "version": "1.0.15",
         "digest": "3914324a17e9cbf5b5e12212cb0142bc53725e19d8a6ae9dc613da1ccea2dc83"}]
    assert json.loads(historical15_fixture_bytes()) == expected


def historical15_fixture_bytes():
    raw = (Path(__file__).resolve().parent / "fixtures/phase3-selected-textgen-1-0-15-policy.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "7187e19fdc75827c807632996e38aab264e84be58fb689c478f5907eaedc911a"
    return raw


def historical_locked_fixture_bytes():
    raw = (Path(__file__).resolve().parent / "fixtures/phase3-selected-locked-agent-policy.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "fb28a5310c62edf00616d36a372313610f2739757f92e4a73f38181aee07f77f"
    return raw


def test_historical_locked_policy_adds_only_exact_locked_component():
    expected = json.loads(historical15_fixture_bytes())
    locked = {"id": "agent-python-adapter-v1", "version": "1.0.0",
        "digest": "28c1d862b947f294dcd0a7f038c32f3d9cf4e31ea20f81d91ab800b13d0b8274"}
    expected["preparation"]["families"].append(locked)
    expected["preparation"]["recipe_component_sets"].append([locked])
    expected["agent"]["components"].append(locked)
    assert json.loads(historical_locked_fixture_bytes()) == expected


def historical16_fixture_bytes():
    raw = (Path(__file__).resolve().parent / "fixtures/phase3-selected-textgen-1-0-16-policy.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "4a0da3723dfa279ecf80c9a4c49f0d7db6e60db4f6fb0e7e0f8ae53875ac1bcd"
    return raw


def historical17_fixture_bytes():
    raw = (Path(__file__).resolve().parent / "fixtures/phase3-selected-textgen-1-0-17-policy.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "e5fa5ab67aa5b44d13ae5733beef701b0dbb4bf2935f2d481787c06183afbe8b"
    return raw


def historical18_fixture_bytes():
    raw = (Path(__file__).resolve().parent / "fixtures/phase3-selected-textgen-1-0-18-policy.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "1ac3ec293600b8603ce230756be4e524573f0fcbd7576535109c61955d75fde0"
    return raw


def test_current_policy_matches_frozen_18_policy_with_current_identities():
    from mcpme.preparation_release_policy import POLICY_PATH, ReleasePolicy
    from mcpme.preparation_textgen import references
    from mcpme.preparation_agent_guides import builtin_analysis_guides
    refs = {
        "llamacpp-cpu-v1": {"id": "llamacpp-cpu-v1", "version": "1.0.19",
            "digest": "2e560f2a4094fe8e79db9dab229e42233f1c1903c1d158226e8ab96ade452331"},
        "text-generate-v1": {"id": "text-generate-v1", "version": "1.0.19",
            "digest": "887bd8995ac0089e0fd81583837157bacf8c2f5adbf0ed370c7135db54b77b9f"},
        "agent-python-adapter-v1": {"id": "agent-python-adapter-v1", "version": "1.0.3",
            "digest": "91f64be7b083517881ff0d9a91af1389f7cc151fc2adb49abbf6e217bc904fec"},
        "agent-unlocked-python-adapter-v1": {"id": "agent-unlocked-python-adapter-v1", "version": "1.0.3",
            "digest": "a34b6de0ced6964bbe51ff8f049863216b51f815b4ea1a5deb4b619403262aed"}}
    assert {r.id: r.model_dump() for r in references()} == {k: v for k, v in refs.items() if not k.startswith("agent-")}
    assert {g.component.id: g.component.model_dump() for g in builtin_analysis_guides()} == {k: v for k, v in refs.items() if k.startswith("agent-")}
    expected = json.loads(historical18_fixture_bytes())
    expected["preparation"]["families"] = [refs.get(x["id"], x) for x in expected["preparation"]["families"]]
    expected["preparation"]["recipe_component_sets"] = [[refs.get(x["id"], x) for x in group] for group in expected["preparation"]["recipe_component_sets"]]
    expected["agent"]["components"] = [refs.get(x["id"], x) for x in expected["agent"]["components"]]
    expected["agent"]["providers"][0]["adapter_digest"] = "216c9fac8d10d60ffce2ee33ea8f58978fea078636d1e058299dfd44d19b747e"
    raw = (Path(__file__).resolve().parents[1] / POLICY_PATH).read_bytes()
    assert json.loads(raw) == expected
    assert hashlib.sha256(raw).hexdigest() == "3434e8e362cd088fcfdd0fa8692ddb2b919eaa61e2bc43116c84b497e9efa513"
    preparation, agent, _ = ReleasePolicy.model_validate_json(raw).admissions()
    assert agent.components <= preparation.families


def historical14_fixture_bytes():
    path = Path(__file__).resolve().parent / "fixtures/phase3-selected-textgen-1-0-14-policy.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "5bb20b7a8cde39368a5cc3d03a92a4ed3f69d92ac56c317b6f696432863925ea"
    return raw


def historical13_fixture_bytes():
    path = Path(__file__).resolve().parent / "fixtures/phase3-selected-textgen-1-0-13-policy.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "8a9a8fd69217352b49e0e338d4d652df8cebcac86609edae1190a88cd2d9a592"
    return raw


def selected12_fixture_bytes():
    raw = historical13_fixture_bytes()
    for current, old in ((b"1.0.13", b"1.0.12"),
                         (b"899aa082f51bbe079968ce4540fa801215adcb9f8a2fc622844e4e63ee133c9e", b"7800e0d271ee220b6feff59b9df1959c22387d57b5485414473c302ba8c502fa"),
                         (b"936c30b0ede738344713d37d62cb71dbfea59b7375dfa24ea023d9693bf9274a", b"60ee19b6a5989e75481f1886d85b545f2f7948eca3496c0ab8b3cc3cb0d0fa2d")):
        raw = raw.replace(current, old)
    # The selected 1.0.12 policy is retained as a byte-exact fixture.
    assert hashlib.sha256(raw).hexdigest() == "4c5569f10a50a360e5c8e1cffc26cf637f5795d3a2562fc635ca2d9d419e4375"
    return raw


def test_lifecycle_policy_guard_accepts_historical_and_current_bytes(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    lifecycle = importlib.import_module("verify_phase3_recipe_lifecycle")
    management = importlib.import_module("verify_phase3_public_management")
    host = tmp_path / "host"
    policy_path = host / management.POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    for raw, expected_scope, expected in ((ROOT_POLICY_JSON.encode(), "closed", "c8fa615def941e66e85a68ac0e7cc46dd7caf1797918fc03fdd5277ac2326522"),
                          (selected12_fixture_bytes(), "selected_model_accepted", "4c5569f10a50a360e5c8e1cffc26cf637f5795d3a2562fc635ca2d9d419e4375"),
                          (historical13_fixture_bytes(), "selected_model_accepted", "8a9a8fd69217352b49e0e338d4d652df8cebcac86609edae1190a88cd2d9a592"),
                          (historical14_fixture_bytes(), "selected_model_accepted", "5bb20b7a8cde39368a5cc3d03a92a4ed3f69d92ac56c317b6f696432863925ea"),
                          (historical15_fixture_bytes(), "selected_model_accepted", "7187e19fdc75827c807632996e38aab264e84be58fb689c478f5907eaedc911a"),
                          (historical16_fixture_bytes(), "selected_model_accepted", "4a0da3723dfa279ecf80c9a4c49f0d7db6e60db4f6fb0e7e0f8ae53875ac1bcd"),
                          (historical17_fixture_bytes(), "selected_model_accepted", "e5fa5ab67aa5b44d13ae5733beef701b0dbb4bf2935f2d481787c06183afbe8b"),
                          (historical18_fixture_bytes(), "selected_model_accepted", "1ac3ec293600b8603ce230756be4e524573f0fcbd7576535109c61955d75fde0"),
                          ((Path(__file__).resolve().parents[1] / management.POLICY_PATH).read_bytes(), "selected_model_accepted", "3434e8e362cd088fcfdd0fa8692ddb2b919eaa61e2bc43116c84b497e9efa513")):
        policy_path.write_bytes(raw)
        scope, observed = lifecycle.verify_lifecycle_policy(host)
        assert observed == expected and scope == expected_scope


@pytest.mark.parametrize("mutation", ["malformed", "extra_family", "extra_closure", "command", "agent_model", "policy_id"])
def test_lifecycle_policy_guard_rejects_mutated_current_bytes(tmp_path, monkeypatch, mutation):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    lifecycle = importlib.import_module("verify_phase3_recipe_lifecycle")
    management = importlib.import_module("verify_phase3_public_management")
    host = tmp_path / "host"
    policy_path = host / management.POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    current_raw = (Path(__file__).resolve().parents[1] / management.POLICY_PATH).read_bytes()
    current = json.loads(current_raw)
    if mutation == "malformed":
        raw = b"{\"schema_version\":1} trailing"
    else:
        if mutation == "extra_family":
            current["preparation"]["families"].append({"id": management.PYTHON[0],
                "version": management.PYTHON[1], "digest": management.PYTHON[2]})
        elif mutation == "extra_closure":
            current["preparation"]["recipe_component_sets"].append([{"id": management.PYTHON[0],
                "version": management.PYTHON[1], "digest": management.PYTHON[2]}])
        elif mutation == "command":
            current["command"]["installed_platform_policy"] = "f" * 64
        elif mutation == "agent_model":
            current["agent"]["providers"][0]["models"].append("gpt-6.1-sol-extra")
        else:
            current["policy_id"] = "unexpected-policy"
        baseline = (json.dumps(json.loads(current_raw), indent=2) + "\n").encode()
        assert baseline == current_raw
        raw = (json.dumps(current, indent=2) + "\n").encode()
    policy_path.write_bytes(raw)
    monkeypatch.setattr(management, "typed_policy_scope", lambda _host: pytest.fail("typed validation reached"))
    with pytest.raises(lifecycle.onboarding.AcceptanceError, match="lifecycle_policy_not_reviewed"):
        lifecycle.verify_lifecycle_policy(host)


def capabilities(driver):
    return {"automatic_preparation": True, "recipe_execution": True,
        "package_schema_versions": [1, 2], "profiles": ["mcp-stdio", "mcp-http", "model-service", "host-command"],
        "preparation_resolution": {"execution": True, "candidate_kinds": ["python-callable", "python-mcp"],
            "producer_versions": ["1.0.1"]},
        "preparation_execution": {"admission_digest": "a" * 64,
            "families": [[item["id"], item["version"], item["digest"]] for item in ROOT_POLICY["preparation"]["families"]],
            "recipe_component_sets": [[[item["id"], item["version"], item["digest"]] for item in group]
                for group in ROOT_POLICY["preparation"]["recipe_component_sets"]]}}


@pytest.mark.parametrize("damage", [None, "python", "resolution", "onnx", "command", "command-closure",
    "schema", "profile", "engine", "task", "agent-family", "agent-closure", "version", "version10", "wrong-engine-alias",
    "extra-family", "extra-closure", "service-family", "service-protocol", "service-transport", "service-closure"])
def test_combined_discovery_requires_all_exact_families_and_closed_agent(driver, damage):
    value = copy.deepcopy(capabilities(driver)); engine, task = ENGINE, TASK
    execution = value["preparation_execution"]
    if damage == "python": execution["families"][0][2] = "f" * 64
    elif damage == "resolution": value["preparation_resolution"]["producer_versions"] = ["1.0.0"]
    elif damage == "onnx": execution["recipe_component_sets"][2].pop(0)
    elif damage == "command": execution["families"][2][2] = "f" * 64
    elif damage == "command-closure": execution["recipe_component_sets"][3].append(list(TASK))
    elif damage == "schema": value["package_schema_versions"] = [1]
    elif damage == "profile": value["profiles"].pop()
    elif damage == "engine": execution["recipe_component_sets"][4][0][2] = "f" * 64
    elif damage == "task": execution["families"][3][2] = "f" * 64
    elif damage == "agent-family": execution["families"].append(["agent-python-adapter-v1", "1.0.0", "f" * 64])
    elif damage == "agent-closure": execution["recipe_component_sets"].append([["agent-python-adapter-v1", "1.0.0", "f" * 64]])
    elif damage == "version": engine = (ENGINE[0], "1.0.9", ENGINE[2])
    elif damage == "version10": engine = (ENGINE[0], "1.0.10", ENGINE[2])
    elif damage == "wrong-engine-alias":
        engine = ("textgen-engine-v1", ENGINE[1], ENGINE[2])
        execution["recipe_component_sets"][4][0][0] = engine[0]
    elif damage == "extra-family": execution["families"].append(["extra-v1", "1.0.0", "f" * 64])
    elif damage == "extra-closure": execution["recipe_component_sets"].append([["extra-v1", "1.0.0", "f" * 64]])
    elif damage == "service-family": execution["families"][4][2] = "f" * 64
    elif damage == "service-protocol": execution["recipe_component_sets"][5][0][2] = "f" * 64
    elif damage == "service-transport": execution["recipe_component_sets"][5][1][2] = "f" * 64
    elif damage == "service-closure": execution["recipe_component_sets"][5].pop()
    policy = driver.closed_release_policy(copy.deepcopy(ROOT_POLICY))
    if damage is None:
        assert driver.combined_capabilities(value, engine, task, policy) == "a" * 64
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.combined_capabilities(value, engine, task, policy)


@pytest.mark.parametrize("enabled", [False, True])
async def test_only_explicit_private_case_start_uses_existing_factory(driver, monkeypatch, enabled):
    calls = []
    async def ordinary(*args, **kwargs): calls.append(("ordinary", kwargs)); return "owned"
    async def private(*args, **kwargs): calls.append(("private", kwargs)); return "owned"
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    monkeypatch.setattr(driver.agent.Harness, "start", private)
    harness = object.__new__(driver.PublicReleaseAgentHarness); harness.combined_report = {}
    assert await harness.start("state", {}, "home", enabled=enabled) == "owned"
    assert calls == [("private" if enabled else "ordinary", {"enabled": enabled})]
    assert harness.combined_report == {"private_embedding_starts" if enabled else "ordinary_public_cli_starts": 1}


@pytest.mark.parametrize("failure", [None, "providers", "live", "preparation", "resolution", "agent-job", "missing-ledger"])
async def test_defaults_use_only_public_read_routes_and_require_empty_ledgers(driver, tmp_path, monkeypatch, failure):
    root = tmp_path.resolve()
    harness = driver.PublicReleaseAgentHarness(root, root, root, {"safe_to_remove": True}, report={})
    policy = root / driver.command.POLICY_PATH; policy.parent.mkdir(parents=True)
    policy.write_text(ROOT_POLICY_JSON)
    calls, starts = [], []
    async def ordinary(*args, **kwargs): starts.append(kwargs); return "owned"
    async def shutdown(*args): pass
    async def cli(state, environment, home, *args):
        calls.append(args)
        if args == ("capabilities", "--json"): return capabilities(driver)
        if args == ("preparations", "list", "--json"): return {"preparations": ["bad"] if failure == "preparation" else []}
        assert args == ("resolutions", "list", "--json")
        return {"resolutions": ["bad"] if failure == "resolution" else []}
    async def get(state, route):
        assert route == "/v1/agent-providers"
        return {"providers": ["bad"] if failure == "providers" else [], "live_execution_enabled": failure == "live"}
    counts = dict.fromkeys(("preparation_jobs", "resolution_jobs", "agent_host_jobs", "attempts"), 0)
    if failure == "agent-job": counts["agent_host_jobs"] = 1
    elif failure == "missing-ledger": counts.pop("attempts")
    monkeypatch.setattr(driver.common.Harness, "start", ordinary)
    monkeypatch.setattr(harness, "shutdown", shutdown); monkeypatch.setattr(harness, "cli", cli); monkeypatch.setattr(harness, "get", get)
    monkeypatch.setattr(driver, "textgen_keys", lambda: (ENGINE, TASK)); monkeypatch.setattr(driver.agent, "job_counts", lambda _: counts)
    if failure is None:
        assert await harness.defaults() is True
        assert harness.combined_report["public_defaults"]["execution_factory_injected"] is False
        assert harness.combined_report["public_defaults"]["review_created_jobs"] is False
        assert harness.combined_report["public_defaults"]["typed_release_policy_validated"] is True
    else:
        with pytest.raises(driver.common.AcceptanceError): await harness.defaults()
        assert "public_defaults" not in harness.combined_report
    assert starts == [{"enabled": False}]
    assert not any("start" in call or "apply" in call or "create" in call for call in calls)


@pytest.mark.parametrize("damage", ["null-agent", "extra-policy", "integer-agent-flag"])
async def test_invalid_policy_cannot_start_the_public_default_host(driver, tmp_path, monkeypatch, damage):
    root = tmp_path.resolve()
    harness = driver.PublicReleaseAgentHarness(root, root, root, {"safe_to_remove": True}, report={})
    value = copy.deepcopy(ROOT_POLICY)
    if damage == "null-agent": value["agent"] = None
    elif damage == "extra-policy": value["unreviewed"] = True
    else: value["agent"]["unlocked_resolution"] = 0
    path = root / driver.command.POLICY_PATH; path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value))
    async def unexpected(*args, **kwargs): raise AssertionError("public host must not start")
    monkeypatch.setattr(driver.common.Harness, "start", unexpected)
    with pytest.raises(driver.common.AcceptanceError): await harness.defaults()
    assert harness.combined_report == {}


@pytest.mark.parametrize("damage", [None, "calls", "live", "http", "source", "relocation"])
async def test_private_cases_keep_exact_simulation_and_cleanup_scope(driver, monkeypatch, damage):
    result = {"passed": True, "simulated_provider": True, "provider_calls": 4, "live_provider_calls": 0,
        "http_calls": 2, "stdio_calls": 2, "pending_proposal_handoff": True, "source_removed": True,
        "captured_source_removed": True, "restart_invoked": True, "relocated_confined_probe": True}
    if damage == "calls": result["provider_calls"] = 5
    elif damage == "live": result["live_provider_calls"] = 1
    elif damage == "http": result["http_calls"] = 1
    elif damage == "source": result["captured_source_removed"] = False
    elif damage == "relocation": result["relocated_confined_probe"] = False
    async def private(self, label): return result
    monkeypatch.setattr(driver.agent.Harness, "case", private)
    harness = object.__new__(driver.PublicReleaseAgentHarness)
    if damage is None:
        observed = await harness.case("scalar")
        assert observed["execution_authority"] == "trusted_private_embedding"
        assert observed["public_agent_execution_verified"] is False and observed["execution_factory_injected"] is True
        assert observed["captured_state_retained_until_owned_cleanup"] is True
    else:
        with pytest.raises(driver.common.AcceptanceError): await harness.case("scalar")


@pytest.mark.parametrize("damage", [None, "bytes", "mode", "symlink", "hardlink"])
def test_fixture_source_inputs_are_bounded_regular_owned_bytes(driver, tmp_path, damage):
    path = tmp_path.resolve() / "fixture.py"; path.write_bytes(b"Synthetic fixture source"); path.chmod(0o600)
    before = driver.input_file(path)
    if damage == "bytes": path.write_bytes(b"Changed synthetic source")
    elif damage == "mode": path.chmod(0o666)
    elif damage == "symlink": original = path.with_suffix(".held"); path.rename(original); path.symlink_to(original)
    elif damage == "hardlink": (path.with_suffix(".alias")).hardlink_to(path)
    if damage == "bytes": assert driver.input_file(path) != before
    elif damage is None: assert driver.input_file(path) == before
    else:
        with pytest.raises(driver.common.AcceptanceError): driver.input_file(path)


@pytest.mark.parametrize("fault", [None, "open-agent", "app-change", "fixture-change", "ownership", "missing-case", "missing-defaults"])
async def test_signed_composition_preserves_inputs_and_cannot_project_incomplete_cases(driver, tmp_path, monkeypatch, fault):
    app = tmp_path.resolve() / "Synthetic.app"
    host = app / "Contents/Resources/host"
    native = app / "Contents/MacOS/mcpme"
    native.parent.mkdir(parents=True); native.write_bytes(b"Synthetic native metadata"); native.chmod(0o700)
    policy = host / driver.command.POLICY_PATH; policy.parent.mkdir(parents=True)
    policy_value = copy.deepcopy(ROOT_POLICY)
    if fault == "open-agent": policy_value["agent"] = {}
    policy.write_text(json.dumps(policy_value))
    (host / "build.json").write_bytes(b"Synthetic build receipt metadata")
    scripts = tmp_path / "scripts"; scripts.mkdir()
    old = scripts / "old.py"; old.write_bytes(b"Synthetic old driver source")
    fixture = scripts / "agent_acceptance_fixture.py"; fixture.write_bytes(b"Synthetic request fixture source")
    monkeypatch.setattr(driver.agent, "__file__", str(old))
    monkeypatch.setattr(driver.common, "_host_paths", lambda _: None)
    monkeypatch.setattr(driver.common, "verify_host_payload", lambda _: {"source_commit": "a" * 40})
    signatures, runs = [], []
    monkeypatch.setattr(driver.command, "signature", lambda path, **kw: signatures.append(path))
    monkeypatch.setattr(driver.common, "verify_native_app_signature", lambda path: signatures.append(path))
    async def existing(_host, _native, report, *, harness_factory, cases):
        runs.append(cases)
        harness = harness_factory(tmp_path, host, native, {"safe_to_remove": True})
        assert type(harness) is driver.PublicReleaseAgentHarness and harness.combined_report is report
        report.update(verified=True, owned_children_joined=fault != "ownership", production_defaults_disabled=True,
            cases={name: {"passed": True, "provider_calls": 4} for name in cases if fault != "missing-case" or name == "scalar"})
        if fault != "missing-defaults": report["public_defaults"] = {"passed": True}
        if fault == "app-change": (host / "build.json").write_bytes(b"Changed synthetic app metadata")
        elif fault == "fixture-change": fixture.write_bytes(b"Changed synthetic request fixture source")
    monkeypatch.setattr(driver.common, "verify", existing)
    report = {"verified": False, "cases": {}}
    if fault in {"open-agent", "app-change", "fixture-change"}:
        with pytest.raises(driver.common.AcceptanceError): await driver.verify(host, native, report)
        assert report["verified"] is False
    else:
        await driver.verify(host, native, report)
        assert report["verified"] is (fault is None)
        assert report["public_agent_execution_verified"] is False and report["model_inference_calls"] == 0
        assert report["original_signed_app_preserved"] and report["original_agent_fixture_and_driver_preserved"]
        assert "production_defaults_disabled" not in report
        assert report["public_release_defaults_and_closed_agent_verified"] is True
    if fault == "open-agent": assert not runs and not signatures
    else: assert runs == [driver.agent.CASES]
