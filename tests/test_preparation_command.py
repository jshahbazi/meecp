from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from mcpme.preparation_command import CommandFamily, structural_schema
from mcpme.preparation_components import SnapshotFile, SourceSnapshot, parse_component_descriptor
from mcpme.preparation_contracts import PreparationError, canonical_bytes, contract_digest
from test_preparation_command_source import image


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def source(executable=None, extra_files=None):
    files = {"bin/native": image() if executable is None else executable, "run.input.schema.json": canonical_bytes(SCHEMA),
        "run.output.schema.json": canonical_bytes(SCHEMA), "LICENSE": b"Synthetic fixture repository license.\n"}
    files.update(extra_files or {})
    records = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for path, data in sorted(files.items())]
    metadata = [{"path": path, "executable": path == "bin/native", "kind": "binary" if path == "bin/native" else "text"} for path in sorted(files)]
    identity = hashlib.sha256(b"mcpme-application-source-v2\0" + json.dumps({"contract_version": 2,
        "kind": "application_directory", "files": records, "file_metadata": metadata}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    draft = {"draft_id": str(uuid.uuid4()), "revision": 1, "name": "Synthetic command", "source": {
        "kind": "application_directory", "contract_version": 2, "digest": identity, "revision": "sha256:" + identity, "files": records,
        "file_metadata": metadata}}
    snapshot = {path: SimpleNamespace(path=path, sha256=hashlib.sha256(data).hexdigest(), size=len(data), data=data,
        executable=path == "bin/native", kind="binary" if path == "bin/native" else "text") for path, data in sorted(files.items())}
    return draft, snapshot


def selected(helper="a" * 64, executable=None, extra_files=None):
    draft, snapshot = source(executable, extra_files)
    family = CommandFamily(helper, snapshot_reader=lambda value: snapshot)
    candidate = family.inspect_source(draft)["candidates"][0]
    ref = family.component.reference
    recipe = {"manifest": {"id": "mcpme.native-command", "version": "1.0.0", "steps": [
        {"component": ref.id, "version": ref.version, "digest": ref.digest}]}, "digest": "b" * 64}
    fields, bindings, blockers = family.form_provider.fields(draft, candidate, recipe)
    assert not blockers
    values = {}
    for field in fields:
        kind = bindings[field.id][0]
        values[field.id] = (field.choices[0].id if field.choices else {"ok": True} if kind in ("arguments", "expected") else field.value)
    context = SimpleNamespace(candidate=candidate, draft=draft, recipe_record=recipe, bindings=bindings)
    descriptor = parse_component_descriptor(family.form_provider.resolve(context, values))
    return family, draft, descriptor


def test_directory_questions_copy_builder_exact_source_closure(tmp_path):
    family, draft, descriptor = selected()
    candidate = family.prepare(descriptor, draft, None, tmp_path / "candidate", None, lambda: None, acquire_wheels=None, job_id="fixture")
    assert candidate.manifest.schema_version == 2 and candidate.manifest.profile == "host-command"
    assert candidate.manifest.command.commands["run"].executable == "source/bin/native"
    assert candidate.lock.artifacts[0].source == draft["source"]["revision"]
    assert [item.model_dump() for item in candidate.lock.artifacts] == family.source_artifacts(descriptor, draft)
    assert candidate.manifest.validation.fixtures[0].expected == {"ok": True}
    assert not candidate.manifest.configuration.fields
    assert {p.relative_to(candidate.root).as_posix() for p in candidate.root.rglob("*") if p.is_file()} == {
        "mcpme.package.json", "mcpme.lock.json", *("source/" + item["path"] for item in draft["source"]["files"])}


@pytest.mark.parametrize("mutation", ["schema", "helper", "source", "effects", "args"])
def test_descriptor_does_not_expand_source_or_effect_authority(mutation):
    family, draft, descriptor = selected()
    value = descriptor.model_dump(exclude_none=True)
    if mutation == "schema":
        value["tools"][0]["input_schema"]["properties"]["ok"] = {"type": "string", "pattern": "^(a+)+$", "maxLength": 64}
    elif mutation == "helper":
        value["components"][0]["input"]["helper_sha256"] = "f" * 64
    elif mutation == "source":
        value["components"][0]["input"]["executable"] = "other"
    elif mutation == "effects":
        value["access"]["network"] = ["example.com"]
    else:
        value["components"][0]["input"]["fixed_args"] = ["x" * 1025]
    with pytest.raises((PreparationError, ValueError)):
        family.validate_inputs(parse_component_descriptor(value), draft)


@pytest.mark.parametrize("value", [{"type": "object", "patternProperties": {"^(a+)+$": {}}},
    {"type": "object", "properties": {"x": {"type": "string"}}},
    {"type": "object", "properties": {"x": {"type": "string", "maxLength": 4, "description": "\ud800"}}}])
def test_selected_schemas_are_bounded_before_any_generic_evaluation(value):
    with pytest.raises(PreparationError, match="unsupported_host_command_schema"):
        structural_schema(value)


def test_large_source_metadata_uses_complete_digest_without_truncation():
    extras = {"data/" + str(index) + "-" + "x" * 128 + ".txt": b"synthetic" for index in range(128)}
    family, draft, descriptor = selected(extra_files=extras)
    assert len(canonical_bytes(draft["source"]["file_metadata"])) > 16384
    binding = family.component.validate_source(descriptor, draft, None)
    metadata = json.loads(binding.metadata_bytes)
    assert len(binding.files) == 132 and len(binding.metadata_bytes) < 16384
    assert metadata["file_metadata_digest"] == contract_digest("preparation-command-file-metadata-v1", draft["source"]["file_metadata"])
    assert metadata["source_identity"] == draft["source"]["digest"]


def test_all_source_notices_are_preserved_with_sorted_declarations(tmp_path):
    family, draft, descriptor = selected(extra_files={"notices/NOTICE-THIRD-PARTY": b"Additional notice.\n"})
    value = descriptor.model_dump(exclude_none=True)
    value["components"][0]["input"]["license_paths"].reverse()
    descriptor = parse_component_descriptor(value)
    candidate = family.prepare(descriptor, draft, None, tmp_path / "candidate", None, lambda: None, acquire_wheels=None, job_id="fixture")
    assert candidate.manifest.licenses == ["source/LICENSE", "source/notices/NOTICE-THIRD-PARTY"]
    assert next(item for item in candidate.lock.artifacts if item.path.endswith("NOTICE-THIRD-PARTY")).license.endswith("NOTICE-THIRD-PARTY")
