# Frozen signed TextGen runtime

TextGen 1.0.8 introduces `textgen-signed-cpu-v1`, an explicitly supplied,
immutable Developer ID runtime asset. Earlier TextGen bundles and their original
engine inputs remain unchanged. This is a local development acceptance profile;
it does not enable public preparation, notarization, or release publication.

## Producer and identity

`scripts/build_signed_textgen_runtime.py` accepts five explicit paths: the pinned
engine Resources directory, a validated private toolchain host, and three fresh
output paths for the build workspace, archive, and metadata. It does not search
for application assets, Python installations, models, or credentials. The local
release operator authorizes use of the unique available Developer ID Application
identity; certificate names and raw command output are not recorded.

The producer verifies the original engine closure and locked dependency project,
acquires hash-pinned wheels through the existing controlled acquisition client,
and assembles private CPython and site packages. It signs every Mach-O file with
one Developer ID identity, hardened runtime, and a secure timestamp. The two
launcher paths contain identical signed bytes. The profile has no entitlement
exceptions, including no library-validation exception.

The archive contains the exact engine, launchers, Python runtime, installed
dependencies, and their licenses and provenance. It contains no model or host
backend code. `templates/text-generate-signed-v1/signed-runtime.json` binds the
archive's complete digest and size and every member's path, digest, size, and
executable bit. The companion provenance records original input identities,
wheel identities, producer source hashes, and native transformations. Installed
wheel RECORD files reflect those transformations while preserving upstream wheel
hashes and license records. Signing certificate fingerprints and Team IDs are
public certificate metadata; signing keys are never stored in the asset.

The checked-in manifest is the trust source. An archive cannot replace it with
self-declared metadata. The recipe carries a compact reference to this exact
manifest and archive; the component digest also binds the full manifest and
producer module closure. This keeps the existing recipe-size limit intact.
The [producer source-composition supplement](TEXTGEN_SIGNED_RUNTIME_SOURCE_COMPOSITION.md)
explains the original checkout base and three added files, and identifies the
exact Git objects needed to recover the producer source.

## Distribution and preparation

The host, app, and DMG builders accept `--textgen-signed-asset` with the exact
local archive. No remote asset URL is invented. A later build must receive the
same archive and preserve its bytes. A new signing operation produces a new
asset identity and requires a new immutable component/recipe version.

Admission holds a no-follow archive descriptor, verifies its complete hash and
size, and validates every member before creating any runtime member. Traversal,
duplicates, missing or extra files, links, special files, extended headers,
unexpected modes, and changed member bytes are rejected. A second pass copies
only the declared regular files; complete tree validation and native signature
verification finish before any runtime execution.

Developer ID app signing verifies the frozen runtime's exact signing identity
and skips its native bytes. It signs the surrounding app, refreshes ordinary
host inventories, and verifies the runtime again after the outer bundle seal.
A different app signing certificate cannot silently re-sign the asset. The
original engine-directory release guard remains in place.

Ordinary A4 preparation uses an A4-specific copy operation. It never signs,
resolves dependencies, downloads wheels, or modifies the shared Python builder.
The trusted adapter sources are copied separately from the host and checked
against the component identity. Model capture, exact model and license checks,
tokenizer identity, process ownership, cancellation, resource policy, private
SDK probing, and receipt binding retain their existing governed paths. Package
validation additionally checks the complete frozen runtime bytes and rejects
extra native/runtime members.

## Evidence boundaries

The initial asset has 2,506 files and 34 signed Mach-O members. Its archive
SHA-256 is `fabbfa2b2960a5ea0d6d54866f2ced4e806269b5b84eb6a133e41d273839c92e`.
Private Python loaded the signed Pydantic and cryptography native extensions,
MCP, Uvicorn, and SSL from that runtime with no entitlement exception. These
checks perform no model inference. An earlier import assertion incorrectly
required `__file__` on the built-in `_sqlite3` module; that failed harness receipt
is preserved separately.

Focused controls cover complete archive admission, malformed members, runtime
mutation, pre-build rejection, exact release preservation, wrong signing
identity, ad hoc replacement, and historical recipe lifecycle preservation.
The Developer ID app built from clean `2e29ee3` passed strict signature checks,
whole-app relocation, seven compiled native selftests, and packaged catalog
checks. Its actual native create and supplement flows both passed in one
479.308-second run. Each case prepared and attached the reviewed package,
installed it, made three HTTP and three stdio fixture calls, restarted it,
preserved its installed runtime after recipe disablement, and invoked the
relocated package after removing the isolated selection and captured state. Each preparation
job acquired zero wheels and ran zero dependency commands. All owned children
joined, and the source, 7,551-file signed app, and original model were unchanged.
The flow receipt SHA-256 is
`0592a9f960b4579646f30efbba94955d3fab99144794f5039d8316b3a76e1a02`;
the complete source/app/model composition receipt SHA-256 is
`768f6dfc2b67e091a3cd4dd5ae6b043d3ce1fd37fffc1fb69f46738d962c426e`.

These are automated native-handler and SDK checks with explicit private test
admission. They do not claim physical UI acceptance, notarization, model quality,
hard aggregate memory enforcement, public execution enablement, or whole-phase
completion. Independent review and the broader Phase 3 gates remain separate.
