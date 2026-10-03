"""Host-generated onboarding forms over immutable source and recipe choices.

Clients submit opaque field answers. They never construct descriptor fragments
or evaluate recipe/source code. Runtime discovery and missing dependency locks
remain explicit resolution requirements, separate from original source bytes.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Annotated, Literal, Protocol

from pydantic import Field, ValidationError, model_validator

from .contracts import Digest, Tool
from .preparation_components import read_snapshot
from .preparation_contracts import (Contract, Name, PreparationError, RecipeReference,
    SourceIdentity, UUID, canonical_bytes, check_schema, contract_digest,
    parse_descriptor, schema_accepts)
from .preparation_inspection import inspect_source

MAX_FIELDS = 256
MAX_CHOICES = 128
FIELD_KINDS = frozenset({"string", "integer", "number", "boolean", "choice", "file", "schema-preview", "object"})
CONSTRAINT_KEYS = frozenset({"min_length", "max_length", "minimum", "maximum"})


class FormChoice(Contract):
    id: Digest
    label: Name


class RecipeChoice(FormChoice):
    recipe: RecipeReference


class FormField(Contract):
    id: Digest
    label: Name
    description: Annotated[str, Field(max_length=4096)] = ""
    kind: Literal["string", "integer", "number", "boolean", "choice", "file", "schema-preview", "object"]
    required: bool
    constraints: dict[str, int | float] = Field(default_factory=dict)
    choices: list[FormChoice] = Field(default_factory=list, max_length=MAX_CHOICES)
    value: Any = None
    schema_: dict | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def bounded(self):
        if not set(self.constraints) <= CONSTRAINT_KEYS or len({item.id for item in self.choices}) != len(self.choices):
            raise PreparationError("invalid_preparation_form")
        if self.kind == "choice" and not self.choices or self.kind != "choice" and self.choices:
            raise PreparationError("invalid_preparation_form")
        allowed = ({"min_length", "max_length"} if self.kind in {"string", "file"}
                   else {"minimum", "maximum"} if self.kind in {"integer", "number"} else set())
        if set(self.constraints) - allowed or self.kind == "schema-preview" and (self.required or self.value is not None):
            raise PreparationError("invalid_preparation_form")
        if self.kind in {"object", "schema-preview"}:
            if self.schema_ is None:
                raise PreparationError("invalid_preparation_form")
            if len(canonical_bytes(self.schema_)) > 16384:
                raise PreparationError("preparation_form_limit")
            check_schema(self.schema_)
        elif self.schema_ is not None:
            raise PreparationError("invalid_preparation_form")
        for name in ("min_length", "max_length"):
            if name in self.constraints and (type(self.constraints[name]) is not int
                    or not 0 <= self.constraints[name] <= 16384):
                raise PreparationError("invalid_preparation_form")
        if ("min_length" in self.constraints and "max_length" in self.constraints
                and self.constraints["min_length"] > self.constraints["max_length"]):
            raise PreparationError("invalid_preparation_form")
        if ("minimum" in self.constraints and "maximum" in self.constraints
                and self.constraints["minimum"] > self.constraints["maximum"]):
            raise PreparationError("invalid_preparation_form")
        if self.value is not None:
            self.accept(self.value)
        return self

    def accept(self, value):
        encoded = canonical_bytes(value)
        if value is None and not self.required:
            return value
        valid = {"string": type(value) is str, "file": type(value) is str,
                 "integer": type(value) is int, "number": type(value) in {int, float},
                 "boolean": type(value) is bool, "choice": type(value) is str,
                 "object": type(value) is dict, "schema-preview": False}[self.kind]
        if not valid:
            raise PreparationError("invalid_preparation_form_answer")
        if self.kind == "object" and len(encoded) > 16384:
            raise PreparationError("preparation_form_limit")
        if self.kind == "file":
            from pathlib import Path
            if not Path(value).is_absolute():
                raise PreparationError("invalid_preparation_form_answer")
        if self.kind == "choice" and value not in {item.id for item in self.choices}:
            raise PreparationError("invalid_preparation_form_answer")
        if self.kind in {"string", "file"}:
            if not self.constraints.get("min_length", 0) <= len(value) <= self.constraints.get("max_length", 16384):
                raise PreparationError("invalid_preparation_form_answer")
        if self.kind in {"integer", "number"}:
            if ("minimum" in self.constraints and value < self.constraints["minimum"]
                    or "maximum" in self.constraints and value > self.constraints["maximum"]):
                raise PreparationError("invalid_preparation_form_answer")
        if self.kind == "object" and not schema_accepts(self.schema_, value):
            raise PreparationError("invalid_preparation_form_answer")
        return value


class OnboardingFormV1(Contract):
    schema_version: Literal[1]
    import_id: UUID
    revision: int = Field(ge=1)
    source_revision: SourceIdentity
    form_digest: Digest
    candidate_id: Digest | None
    recipe: RecipeReference | None
    candidate_choices: list[FormChoice] = Field(max_length=MAX_CHOICES)
    recipe_choices: list[RecipeChoice] = Field(max_length=MAX_CHOICES)
    fields: list[FormField] = Field(max_length=MAX_FIELDS)
    tools: list[Tool] = Field(max_length=128)
    blockers: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] = Field(max_length=128)
    execution_available: bool

    @model_validator(mode="after")
    def consistent(self):
        for items in (self.fields, self.candidate_choices, self.recipe_choices):
            if len({item.id for item in items}) != len(items):
                raise PreparationError("invalid_preparation_form")
        if self.candidate_id is not None and self.candidate_id not in {item.id for item in self.candidate_choices}:
            raise PreparationError("invalid_preparation_form")
        if self.recipe is not None and self.recipe not in [item.recipe for item in self.recipe_choices]:
            raise PreparationError("invalid_preparation_form")
        if self.form_digest != contract_digest("preparation-onboarding-form-v1",
                self.model_dump(by_alias=True, exclude={"form_digest"})):
            raise PreparationError("preparation_form_digest_mismatch")
        return self


def _identity(key: str, value: Any) -> str:
    return contract_digest("preparation-form-field-v1", {"key": key, "value": value})


def _field(key, *, label, kind, required=True, value=None, choices=None, schema=None, constraints=None, description=""):
    return FormField(id=_identity("field", key), label=label, kind=kind, required=required,
        value=value, choices=[choice.model_dump() for choice in choices or []],
        schema=schema, constraints=constraints or {}, description=description)


def _choice(namespace: str, value: str, label: str | None = None):
    return FormChoice(id=_identity(namespace, value), label=label or value)


@dataclass(frozen=True)
class FormContext:
    form: OnboardingFormV1
    candidate: dict | None
    recipe_record: dict | None
    bindings: dict[str, tuple]
    draft: dict


class OnboardingProvider(Protocol):
    """Only reviewed host code implements providers, never recipe bundle hooks."""
    def accepts(self, candidate: dict, recipe: dict) -> bool: ...
    def fields(self, draft: dict, candidate: dict, recipe: dict) -> tuple[list[FormField], dict[str, tuple], list[str]]: ...
    def resolve(self, context: FormContext, values: dict[str, Any]) -> dict: ...


class PythonCallableFormProvider:
    def accepts(self, candidate, recipe):
        return (candidate["kind"] == "python-callable" and len(recipe["manifest"]["steps"]) == 1
                and recipe["manifest"]["steps"][0]["component"] == "python-function-v1")

    def fields(self, draft, candidate, recipe):
        fields, bindings = [], {}
        blockers = [code for code in candidate["unresolved"] if code not in
                    {"expected_result_required", "effects_declaration_required", "service_root_selection_required"}]
        def add(key, binding, **kwargs):
            field = _field(key, **kwargs)
            fields.append(field)
            bindings[field.id] = binding
        add("app-name", ("name",), label="App name", kind="string", value=draft["name"],
            constraints={"min_length": 1, "max_length": 256})
        memory_ceiling = min(4096, draft["requirements"].get("memory_mb", 4096))
        if memory_ceiling < 64:
            blockers.append("unsupported_resource_requirement")
            memory_ceiling = 64
        add("memory", ("memory",), label="Memory budget (MiB)", kind="integer", value=min(128, memory_ceiling),
            constraints={"minimum": 64, "maximum": memory_ceiling})
        licenses = candidate.get("license_candidates", [])
        if licenses:
            choices = [_choice("source-license", path) for path in licenses]
            add("source-license", ("license", {item.id: path for item, path in zip(choices, licenses)}),
                label="Source license", kind="choice", choices=choices, value=choices[0].id if len(choices) == 1 else None)
        effects = [_choice("effects", "none", "No additional file or network access")]
        add("effects", ("effects",), label="App access", kind="choice", choices=effects, value=effects[0].id,
            description="This declares the supported app's access. Review preparation execution separately.")
        fixture = candidate["fixture"]
        tool = candidate["tools"][0]
        add("fixture-arguments", ("arguments",), label="Test input", kind="object", value=fixture["arguments"],
            schema=tool["input_schema"], description="Use synthetic input for the independent runtime test.")
        add("fixture-expected", ("expected",), label="Expected test result", kind="object",
            schema=tool["output_schema"], description="The verifier compares the exact structured result with this object.")
        schema = recipe["manifest"]["input_schema"]
        required = set(schema.get("required", []))
        if required - {item["field"] for item in recipe["manifest"]["form"] if item["kind"] != "schema-preview"}:
            blockers.append("recipe_form_incomplete")
        for item in recipe["manifest"]["form"]:
            key, kind = item["field"], item["kind"]
            subschema = schema.get("properties", {}).get(key, {})
            constraints = {target: subschema[source] for source, target in
                (("minLength", "min_length"), ("maxLength", "max_length"), ("minimum", "minimum"), ("maximum", "maximum"))
                if source in subschema}
            choices, value, mapping = [], item.get("default"), None
            if kind == "enum":
                choices = [_choice("recipe-enum:" + key, option) for option in item["options"]]
                mapping = {choice.id: option for choice, option in zip(choices, item["options"])}
                value = next((choice.id for choice in choices if mapping[choice.id] == value), None)
                kind = "choice"
            # V1 recipe file fields mean a relative path in the original source,
            # not an arbitrary local artifact selection. Keep that identity.
            if kind == "file":
                kind = "string"
            add("recipe-input:" + key, ("input", key, mapping), label=item["label"], kind=kind,
                required=key in required, value=value, choices=choices, constraints=constraints)
        requirements = draft["requirements"]
        if requirements.get("tools") and set(requirements["tools"]) != {tool["name"] for tool in candidate["tools"]}:
            blockers.append("tool_requirement_mismatch")
        if requirements.get("profile") not in {None, "mcp-stdio"}:
            blockers.append("unsupported_execution_profile")
        if requirements.get("secret_references"):
            blockers.append("secret_reference_unavailable")
        return fields, bindings, blockers

    def resolve(self, context, values):
        candidate, recipe = context.candidate, context.recipe_record
        resolved, inputs = {}, {}
        for field_id, binding in context.bindings.items():
            if field_id not in values:
                continue
            value = values[field_id]
            if binding[0] == "input":
                inputs[binding[1]] = binding[2][value] if binding[2] else value
            elif binding[0] == "license":
                resolved["license"] = binding[1][value]
            else:
                resolved[binding[0]] = value
        root = candidate["root"]
        snapshot = read_snapshot(context.draft)
        lock = snapshot.by_path().get(("" if root == "." else root + "/") + "uv.lock")
        if lock is None:
            raise PreparationError("dependency_resolution_required")
        return {"schema_version": 1, "recipe": context.form.recipe.model_dump(),
            "package": {"id": "local.app-" + context.form.import_id, "version": "1.0.0", "name": resolved["name"]},
            "interface": {"kind": "python-callable", "module": candidate["module"],
                          "function": candidate["function"], "tools": candidate["tools"]},
            "dependencies": {"project": root, "lock_sha256": lock.sha256, "python_version": "3.12.13", "mcp_version": "1.30.0"},
            "source_license": resolved["license"], "inputs": inputs,
            "resources": {"memory_mb": resolved["memory"]},
            "validation": [{"id": "synthetic", "tool": candidate["function"], "arguments": resolved["arguments"],
                            "expected": resolved["expected"], "synthetic": True}]}

    def restore_fields(self, fields, bindings, descriptor):
        """Restore typed choices from the same pinned source/interface/recipe."""
        value = parse_descriptor(descriptor)
        restored = []
        for field in fields:
            binding = bindings[field.id]
            key = binding[0]
            default = field.value
            if key == "memory":
                default = min(value.resources.memory_mb, field.constraints["maximum"])
            elif key == "license":
                default = next((identity for identity, path in binding[1].items() if path == value.source_license), None)
            elif key in {"arguments", "expected"}:
                default = getattr(value.validation[0], key)
            elif key == "input":
                default = value.inputs.get(binding[1])
                if binding[2] is not None:
                    default = next((identity for identity, option in binding[2].items() if option == default), None)
            restored.append(FormField.model_validate({**field.model_dump(by_alias=True), "value": default}))
        return restored


class PreparationForms:
    def __init__(self, store, *, providers=None, preferred_recipes=()):
        self.store, self.imports, self.recipes = store, store.imports, store.recipes
        self.providers = tuple(providers) if providers is not None else (PythonCallableFormProvider(),)
        if len(preferred_recipes) > MAX_CHOICES:
            raise PreparationError("preparation_form_limit")
        self.preferred_recipes = tuple(RecipeReference.model_validate(value) for value in preferred_recipes)

    def context(self, import_id, revision, *, candidate_id=None, recipe=None, execution_available=False):
        with self.imports._lock:
            draft = copy.deepcopy(self.imports._get(import_id, revision))
            self.imports._mutable(draft)
        inspection = inspect_source(draft)
        previous = self.store.selected_descriptor(import_id)
        if previous is not None and previous["source_revision"] != inspection["source_revision"]:
            previous = None
        candidates = inspection["candidates"]
        if len(candidates) > MAX_CHOICES:
            raise PreparationError("preparation_form_limit")
        candidate_choices = [FormChoice(id=item["candidate_id"], label=
            ((item.get("function") or item.get("declared_entrypoint") or item["module"]) + " in " + item["module"])[:256])
            for item in candidates]
        if candidate_id is None:
            if previous is not None and previous["candidate_id"] in {item["candidate_id"] for item in candidates}:
                candidate_id = previous["candidate_id"]
            elif len(candidates) == 1:
                candidate_id = candidates[0]["candidate_id"]
        candidate = next((item for item in candidates if item["candidate_id"] == candidate_id), None)
        if candidate_id is not None and candidate is None:
            raise PreparationError("preparation_candidate_mismatch")
        try:
            selected = RecipeReference.model_validate(recipe) if recipe is not None else None
        except ValidationError:
            raise PreparationError("invalid_preparation_recipe") from None
        records, recipe_choices, providers = [], [], {}
        if candidate is not None:
            for record in self.recipes.list():
                if record["state"] != "enabled" or record["revoked"]:
                    continue
                matching = [provider for provider in self.providers if provider.accepts(candidate, record)]
                if len(matching) > 1:
                    raise PreparationError("ambiguous_preparation_provider")
                if not matching:
                    continue
                reference = RecipeReference(id=record["recipe_id"], version=record["version"], digest=record["digest"])
                records.append(record)
                choice = RecipeChoice(id=_identity("recipe-choice", reference.model_dump()),
                    label=(record["recipe_id"] + " " + record["version"])[:256], recipe=reference.model_dump())
                recipe_choices.append(choice)
                providers[reference.digest] = matching[0]
            if len(recipe_choices) > MAX_CHOICES:
                raise PreparationError("preparation_form_limit")
            if selected is None and previous is not None and previous["candidate_id"] == candidate_id:
                saved = previous["descriptor"]["recipe"]
                selected = next((choice.recipe for choice in recipe_choices if choice.recipe.model_dump() == saved), None)
            if selected is None:
                preferred = [choice.recipe for choice in recipe_choices if choice.recipe in self.preferred_recipes]
                if len(preferred) == 1:
                    selected = preferred[0]
            if selected is None and len(recipe_choices) == 1:
                selected = recipe_choices[0].recipe
        record = next((item for item in records if selected is not None and
            (item["recipe_id"], item["version"], item["digest"]) == (selected.id, selected.version, selected.digest)), None)
        if selected is not None and record is None:
            raise PreparationError("preparation_recipe_mismatch")
        fields, bindings, blockers = [], {}, []
        if candidate is None:
            blockers.append("supported_interface_selection_required" if not candidates else "interface_selection_required")
        elif record is None:
            blockers.append("recipe_selection_required" if recipe_choices else "compatible_recipe_required")
            blockers.extend(candidate["unresolved"])
        else:
            self.recipes.resolve(selected.id, selected.version, selected.digest)
            fields, bindings, blockers = providers[selected.digest].fields(draft, candidate, record)
            if (previous is not None and previous["source_revision"] == inspection["source_revision"]
                    and previous["candidate_id"] == candidate_id
                    and previous["descriptor"]["recipe"] == selected.model_dump()
                    and hasattr(providers[selected.digest], "restore_fields")):
                fields = providers[selected.digest].restore_fields(fields, bindings, previous["descriptor"])
        payload = {"schema_version": 1, "import_id": import_id, "revision": revision,
            "source_revision": inspection["source_revision"], "candidate_id": candidate_id,
            "recipe": selected.model_dump() if selected else None,
            "candidate_choices": [item.model_dump() for item in candidate_choices],
            "recipe_choices": [item.model_dump() for item in recipe_choices],
            "fields": [item.model_dump(by_alias=True) for item in fields],
            "tools": [Tool.model_validate(item).model_dump() for item in candidate["tools"]] if candidate else [],
            "blockers": sorted(set(blockers)), "execution_available": execution_available is True}
        digest = contract_digest("preparation-onboarding-form-v1", payload)
        form = OnboardingFormV1.model_validate({**payload, "form_digest": digest})
        with self.imports._lock:
            current = self.imports._get(import_id, revision)
            self.imports._mutable(current)
            if current["source"] != draft["source"]:
                raise PreparationError("source_identity_mismatch")
        return FormContext(form, candidate, record, bindings, draft)

    def form(self, *args, **kwargs):
        return self.context(*args, **kwargs).form.model_dump(by_alias=True)

    def resolve(self, import_id, revision, *, candidate_id, recipe, form_digest, answers, execution_available=False):
        context = self.context(import_id, revision, candidate_id=candidate_id, recipe=recipe,
                               execution_available=execution_available)
        if context.form.form_digest != form_digest:
            raise PreparationError("stale_preparation_form")
        if context.form.blockers or context.candidate is None or context.recipe_record is None:
            raise PreparationError(context.form.blockers[0] if context.form.blockers else "incomplete_preparation_form")
        if not isinstance(answers, dict):
            raise PreparationError("invalid_preparation_form_answer")
        canonical_bytes(answers)
        fields = {field.id: field for field in context.form.fields if field.kind != "schema-preview"}
        if not set(answers) <= set(fields):
            raise PreparationError("unknown_preparation_form_field")
        values = {}
        for field in fields.values():
            value = answers.get(field.id, field.value)
            if value is None:
                if field.required:
                    raise PreparationError("preparation_form_answer_required")
                continue
            values[field.id] = field.accept(value)
        providers = [provider for provider in self.providers if provider.accepts(context.candidate, context.recipe_record)]
        if len(providers) != 1:
            raise PreparationError("ambiguous_preparation_provider")
        # A provider receives a private immutable draft copy, never host/API or
        # installation authority. It cannot be registered by a recipe bundle.
        descriptor = parse_descriptor(providers[0].resolve(context, values))
        self.store._bound_descriptor(import_id, revision, descriptor.model_dump(exclude_none=True), enforce_selection=False)
        return descriptor, context
