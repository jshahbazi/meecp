import copy
import json
import uuid

import pytest

from meecp.imports import ImportStore
from meecp.preparation_contracts import ExecutionPolicy
from meecp.preparation_service_contracts import ServiceConnectionV1, ServiceSourceV1
from meecp.preparation_service_source import create, revise


def connection():
    return {"schema_version": 1,
        **{key: {"id": name, "version": "1.0.0", "digest": str(index) * 64}
           for index, (key, name) in enumerate((("protocol", "openai-chat-completions-v1"),
               ("transport_auth", "loopback-http-bearer-v1"), ("task", "service-text-generate-v1")), 1)},
        "endpoint": {"scheme": "http", "host": "127.0.0.1", "port": 8080, "base_path": "/v1"},
        "model": "synthetic-model", "secret_references": {"api_key": str(uuid.uuid4())},
        "limits": {"timeout_seconds": 30, "max_input_bytes": 16384, "max_output_tokens": 512, "max_response_bytes": 1048576}}


def selected(value):
    value = ServiceConnectionV1.model_validate(value)
    return ServiceSourceV1(kind="service_connection", contract_version=1, connection=value.model_dump(),
        revision=value.revision, files=[]).model_dump()


@pytest.mark.parametrize("fault", ["dns", "remote", "https", "bool_port", "path", "key", "proxy", "oversize", "role"])
def test_closed_no_ambient_service_identity(fault):
    value = connection()
    if fault == "dns": value["endpoint"]["host"] = "localhost"
    elif fault == "remote": value["endpoint"]["host"] = "192.0.2.1"
    elif fault == "https": value["endpoint"]["scheme"] = "https"
    elif fault == "bool_port": value["endpoint"]["port"] = True
    elif fault == "path": value["endpoint"]["base_path"] = "/other"
    elif fault == "key": value["secret_references"]["api_key"] = "synthetic-token-value"
    elif fault == "proxy": value["proxy"] = "http://127.0.0.1:1234"
    elif fault == "oversize": value["limits"]["max_response_bytes"] += 1
    elif fault == "role": value["secret_references"]["other"] = str(uuid.uuid4())
    with pytest.raises(ValueError): ServiceConnectionV1.model_validate(value)


def test_actual_source_free_draft_revision_and_restart(tmp_path):
    root = tmp_path.resolve() / "state"
    store = ImportStore(root)
    try:
        value = connection(); original = selected(value)
        draft = create(store, original, name="Synthetic service", intent="Use selected service")
        assert not (store.staging / draft["draft_id"]).exists()
        assert set(draft["source"]) == {"kind", "contract_version", "connection", "revision", "files"}
        assert draft["source"]["files"] == []
        assert "service connection" in store.export(draft["draft_id"], 1)["agent_instructions"]
        value["endpoint"]["port"] += 1
        changed = revise(store, draft["draft_id"], 1, selected(value))
        assert changed["revision"] == 2 and changed["source"]["revision"] != original["revision"]
        with pytest.raises(ValueError): revise(store, draft["draft_id"], 1, original)
    finally:
        store.close()
    with ImportStore(root) as resumed:
        assert resumed.inspect(draft["draft_id"])["source"] == changed["source"]
        resumed.cancel(draft["draft_id"], 2)


def test_portable_configuration_has_no_host_secret_identifier():
    value = ServiceConnectionV1.model_validate(connection())
    portable = value.portable()
    assert portable["auth_role"] == "api_key"
    assert value.secret_references.api_key not in json.dumps(portable)
    assert "secret_references" not in portable
    other = value.model_dump(); other["secret_references"]["api_key"] = str(uuid.uuid4())
    assert ServiceConnectionV1.model_validate(other).revision != value.revision
    assert ServiceConnectionV1.model_validate(other).portable() == portable


def test_legacy_execution_policy_serialization_has_no_new_fields():
    value = {"schema_version": 1, "operations": ["build", "probe"], "network": [],
        "wall_seconds": 60, "disk_bytes": 1048576, "memory_mb": 128}
    assert ExecutionPolicy.model_validate(value).model_dump() == value
