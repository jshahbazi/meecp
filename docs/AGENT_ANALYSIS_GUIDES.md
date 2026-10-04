# Reviewed agent request contracts

The analysis host binds a selected recipe, trusted component guide, source content
selection and metadata-only selection into one reviewed plan and request identity.
Every provider call carries the selected content and its original file identities,
the separately selected metadata records, recipe identity and the assigned public
role contract. Metadata records contain only a relative path, original byte count
and SHA-256. Selecting an identity does not authorize transferring file contents.

The native review offers separate unchecked content and identity controls. It marks
common project, lock and license identities as recommendations. The review displays
the exact selected identities and guide digest before starting analysis. The API
accepts `selected_metadata_paths` alongside `selected_paths`; they must be sorted,
unique, disjoint selections from the captured source. At most 32 metadata records
are permitted. An original metadata file may be up to 16 MiB, while each transferred
record remains bounded to a 256-character relative path, 64-character digest and
integer byte count. Actual serialized requests, including guidance, schemas,
metadata and preceding proposals, consume the existing transfer budget.
Identity-only selection supports captured text and binary files with the same
whole-byte identity proof. Content selection remains limited to text. Native
content and identity controls for one path are mutually exclusive.

## Trusted guide registration

`AnalysisGuideRegistry` accepts host-owned `AnalysisGuideV1` values. Each guide
names one exact component ID, version and implementation digest. It applies only
to a resolved recipe with that single component. Imported recipe manifests and
source files cannot supply guide instructions, choose tools or add disclosure.
The selected recipe remains separately bound by its exact digest and registry epoch.
The durable plan journal retains only the guide ID, digest and component reference.
Review and dispatch reconstruct the public instructions from the trusted registry
and reject a changed binding. Instruction text is absent from the journal.

Each guide contains four bounded role instructions and an acyclic dependency list
in the existing interface, dependencies, fixtures, adapter order. The coordinator
dispatches ready roles within the reviewed parallelism limit. The Python adapter
guide runs interface and dependency analysis concurrently, lets fixtures consume
the agreed interface, and lets the adapter consume all three proposals.

The locked guide specifies every accepted proposal path, closed JSON shape,
wrapper module/function, original project/lock/license references and the original
lock SHA-256 requirement. Missing reviewed facts remain unresolved. The existing
proposal validators remain authoritative and unchanged. Static proposal analysis
and adoption execute no source or dependency resolver.

New trusted families can register another exact guide through the host's `guides`
constructor parameter. The Python guide factory supports the separate unlocked
requirements contract; it grants no resolution or network authority. Registration
must accompany a reviewed component implementation. Changing published component
bytes requires a new immutable version; adding a guide does not restamp a component.
The default registry includes the separately reviewed locked and unlocked Python
adapter components. The unlocked contract proposes requirements; the ordinary
host resolver selects and records exact versions after its separate review.

## Continuity and verification

The host captures a metadata baseline before its joined off-loop proof of selected
content and metadata-file bytes. It retains that baseline across the join and checks
it before dispatch, subsequent calls and candidate use. Changed original files,
ancestor identities, guide registrations or reviewed selections invalidate the
request. These checks retain the documented trusted same-user filesystem boundary.

The request-inspecting synthetic transport in
`tests/test_preparation_agent_request_readiness.py` constructs its response using
only encoded request material. Its dependency proposal takes the original lock
hash from reviewed records. This establishes request sufficiency for the tested
synthetic scalar app. The optional ordinary pipeline test uses the same transport
through derivation, adoption, confined build, MCP probe, attachment and relocation.
Neither simulation establishes live-provider compatibility, model quality or final
signed and installed host acceptance.
