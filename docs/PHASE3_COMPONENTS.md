# Phase 3 component and static-inspection checkpoint

Status: component contract and code-free inspection implemented, 2026-10-03.
This checkpoint supplies contracts for the [automatic preparation plan](AUTOMATIC_PREPARATION.md).
It does not enable a compiler, dependency resolver, source probe or preparation runner.

## Host-owned component contract

[`preparation_components.py`](../backend/meecp/preparation_components.py) defines
`ComponentProtocol.validate_source(descriptor, draft, recipe) -> SourceBinding`.
The host constructs a `ComponentRegistry` from implementations it owns. A source
or recipe bundle cannot register a Python module, plugin path or executable hook.
Every recipe step selects an exact component ID, semantic version and SHA-256.
Unknown identities and mismatches between the descriptor and recipe fail before
component validation.

`SourceBinding` is immutable. It contains the component identity, original source
revision, exact selected-file identities, an interface digest, an optional
dependency-lock digest and bounded canonical nonsecret metadata. The registry
checks that returned file identities belong to the draft and that the returned
component/source identities match the requested validation. This is source
validation data, not a runtime verification receipt.

`PythonSourceComponent` contains the descriptor-v1 Python-specific checks: source
`uv.lock` and `pyproject.toml`, matching lock hash, a nonempty source license, and
module source. It verifies actual snapshot bytes when the draft has a snapshot
path. The common registry does not require a Python module, lock, license or
dependency object from other components. The existing catalog's exact component
identities remain the Python builder implementation identities.

`PreparationDescriptorV2` is a closed common envelope with recipe/package
identity, exact component selections, component inputs, tools, artifacts,
resources, declared access, lifecycle and synthetic fixtures. Each admitted host
component supplies a closed input schema. The existing v1 parser and digest are
unchanged; v1 descriptors are never rewritten as v2. V2 is available to host
integration code through `parse_component_descriptor`; management API admission
and runtime execution require their own integration and gates.

A fake native component test validates a Swift source without Python module,
dependency-lock or source-license fields in the common descriptor. This proves
contract routing only. It supplies no native compilation or runtime support.

## Static inspection contract

[`preparation_inspection.py`](../backend/meecp/preparation_inspection.py) exposes
`inspect_source(draft) -> dict`. It uses the already imported snapshot, never the
mutable original directory. It checks the exact closed selected-file tree,
original byte hashes and original folder digest serialization before parsing,
then rechecks snapshot bytes after AST/TOML inspection. The original folder
revision and file identities remain unchanged.

The result separates `observed` metadata from `proposed` candidates. Every
candidate has `verified: false`; the result advertises no verified capability or
execution availability. Candidate IDs bind the source revision and exact
proposal, including evidence file hashes and source line numbers.

The supported proposal subset is a top-level, undecorated synchronous Python
function with named `str`, `int`, `float` or `bool` parameters and a declared
dictionary result. Static primitive defaults are accepted. The inspector never
evaluates annotations, defaults, decorators or function bodies. Unsupported
annotations, evaluated defaults, positional-only/variadic signatures, duplicate
function definitions and shadowed builtin annotation names remain observations
requiring resolution. Generated input/output schemas are proposals; their
operational correctness still requires an authorized actual tool probe.

Candidates include generated nonsecret synthetic arguments and an empty
expected-result choice (`expected: null`). The user or a separately authorized
resolver must provide the expected semantic result. Missing dependency locks
are `dependency_resolution_required`; inspection never invents a source lock or
changes source identity. Material effects remain a required choice.

Bounded `pyproject.toml` metadata identifies project roots. Multiple roots remain
an explicit service-root selection. Multiple interface candidates remain an
explicit interface selection. Existing Python MCP candidates come from declared
module metadata or declared scripts in a project that declares MCP. Their tools
remain unknown until authorized runtime discovery. A script callable also needs
an explicit launch binding; its declaration does not prove that `python -m`
launches the server. Candidate modules use source-snapshot-relative paths; the
original project declaration is retained separately.

Inspection bounds include 32 project roots, 64 KiB per TOML document, 128 Python
files, 256 KiB per Python AST, 2 MiB total parsed Python, 10,000 AST nodes, depth
64, 32 callable parameters and 128 candidates. Source reads retain the intake's
2,048-file, 16 MiB/file and 64 MiB aggregate bounds. Malformed or excessive
metadata produces fixed error codes without source text or exception dumps.

## Checkpoint evidence

`uv run --locked pytest tests/test_preparation_components.py tests/test_preparation_inspection.py -q`
passed **43 tests**. The combined preparation, recipe, component and inspection
suites passed **139 tests**. `git diff --check` passed.

Tests cover source nonexecution, generated typed choices, missing expected
results, original-location mutation, snapshot mutation before/during parsing,
stale hashes, unbound tree additions, symlinks, parser limits, malicious AST
expressions, root ambiguity, dynamic MCP discovery, immutable binding data,
unknown component identities, closed component inputs and the non-Python fake
component. These gates do not establish automatic build or installed-host
end-to-end completion; those require the remaining Phase 3 runner, verifier,
native integration and runtime acceptance work.
