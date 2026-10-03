# Application-directory intake

Automatic directory intake is an explicit source-selection mode. Native source
selection supplies it; the CLI accepts `imports create --source-mode automatic`.
The API accepts `source_mode: "automatic"`. Existing callers keep legacy text
selection and source digest semantics.

Ordinary directories containing only supported text sources retain the original
folder contract and identity. A selection containing binary or unrecognized
regular files uses `application_directory`, contract version 2. This contract
binds sorted path/hash/size records plus matching executable and text/binary
metadata in a separate digest domain. Captured files stay mode 0600; original
executable metadata never grants execution authority.

Selection uses held directory descriptors and no-follow regular-file reads.
Symlinks, hardlinks, special files, changed identities and credential material
are rejected. Existing private/generated-path exclusions remain in effect;
all other regular payloads are included, regardless of suffix. The bounds are
2,048 files, 10,000 streamed entries, 64 directory levels, 16 MiB per file and
64 MiB total. Automatic copying runs outside the SQLite transaction. Large model
artifacts continue through the separately bounded artifact-capture contract.

The reader rechecks the complete private snapshot inventory, file permissions,
classification, bytes and metadata identity. Inspection never invokes selected
code. An unsupported directory can be captured without being executable: each
trusted family still needs its own interface, runtime, provenance, resource and
authority checks before preparation. Automatic command attachment and public
execution remain gated separately.

Schema-2 host-command candidates bind the complete command block, licenses and
original source artifacts, including their executable flags and provenance.
They use `preparation-host-command-candidate-v1`; old candidate domains stay
unchanged. Schema-2 recipe metadata is compatible only when every selected and
transitive host component explicitly declares that package schema. Static
compatibility does not enable execution.
