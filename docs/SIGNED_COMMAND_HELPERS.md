# Immutable signed command helpers

This asset freezes the two already signed command helpers from the clean A5
Developer ID app built from source `31b50ccd5bddb780d66d2d5f70e643704f8e13c3`.
Creating the asset copies those bytes without recompiling or signing them.
The asset profile is `command-native-v1`, version `1.0.0`; it is distinct from
the unchanged `host-command-json-v1@1.0.1` preparation component.

Installed command packages bind the exact installed helper SHA-256. Recompiling
and signing a helper during each app build changes that identity even when its
C source is unchanged, preventing a later host from opening an earlier sealed
package. Reusing this exact pair provides a stable identity for packages prepared
against it. Existing private packages bound to another helper hash still need
explicit re-preparation or a separately reviewed compatibility mechanism.

## Reviewed input

The fixed Git-owned manifest is
`templates/command-native-v1/manifest.json`. It pins the complete archive,
two helper paths, sizes, modes and hashes, original C/header identities, the
unchanged component, and the approved Developer ID certificate and Team.
`provenance.json` binds the original A5 app/host receipts and immutable Git
objects for the two producer scripts and three C/header inputs. The MIT license
is retained from that original source revision.

The archive is a local explicit producer input, SHA-256
`30539c050891beec398a8ecb69b959092f6897c2b495dc66b73e9cfb3de9b2cd`,
24,938 bytes. No remote download location is invented. The ignored binary
archive and private preservation receipts remain outside Git.

| Fixed host member | Bytes | SHA-256 |
|---|---:|---|
| `bin/mcpme-command-reap` | 70,752 | `9dab276553fec8f2039fa6d7a3ce5cc8b7aeebc01f9df730027320e72f0e6513` |
| `bin/mcpme-command-resource-reap` | 71,504 | `c2ecbc7942981430d6f1bad26809c2e42c409d75aafdc19fc6426faf52110bb5` |

Both files retain mode `0700`. The deterministic archive uses USTAR records in
lexicographic order, empty owner/group names, zero UID/GID/member times, and
gzip with an empty filename, zero time and compression level 9. Reconstruction
from the approved pair must reproduce the pinned archive digest.

## Producer contract

`scripts/signed_command_helpers.py` exposes these functions:

- `manifest()` reads the fixed reviewed manifest and verifies its closed schema,
  exact pair/source/signer identity, provenance and license hashes.
- `admit_archive(archive, destination=None)` validates the entire held archive
  before creating any output. With a destination, that directory must be fresh;
  extraction creates only the fixed pair and verifies their signatures.
- `bundle(host, archive)` copies into an existing canonical host/bin directory.
  Both fixed helper targets must be absent. It uses exclusive creation, preserves
  unrelated members, verifies both signatures, and returns the two absolute
  helper paths.
- `verify_host(host, certificate=None)` verifies both exact members and their
  signatures, returning the manifest. If supplied, the certificate must equal
  the pinned certificate. Held file and ancestor identities remain unchanged
  through signature inspection.
- `create_archive(host, output)` reconstructs the exact archive from an already
  verified pair and requires an exclusive fresh output. The script CLI exposes
  this operation through explicit `--source-host` and `--archive` arguments.

The archive path belongs only to producer code. There is no runtime API,
environment selector, feature flag, source execution or automatic acquisition.
Helpers are never run by this utility. Signature inspection invokes bounded
system codesign verification/display commands; the utility never signs or
discovers signing identities. Raw tool output is retained only in memory.

Archive admission enforces the pinned compressed identity, bounded decompression,
exact regular members, hashes and tar metadata. It rejects links, duplicate or
unknown members, path changes, PAX metadata, unsafe files and changed inputs.
Output helpers use exclusive creation. A failed copy can leave files only in
the caller's fresh owned staging area, which its build cleanup owns.

## Distribution integration

The Signed installer workflow receives the exact archive through the
`macos-release` environment secret `MCPME_COMMAND_SIGNED_ASSET_BASE64`.
`.github/scripts/prepare_command_asset.py --output PATH` checks strict bounded
base64, the fixed manifest size/hash, complete archive members and both signatures
through `admit_archive` with a fresh private destination. It publishes only the
verified archive to an absent mode-0600 output and emits its canonical path through
`GITHUB_OUTPUT`. The workflow passes that exact path to
`scripts/build_meecp_dmg.py --command-signed-asset` before any signed build.
The temporary admission stage is removed; the archive stays in `RUNNER_TEMP`
for the build and is excluded from release asset uploads. This producer input
does not select runtime authority or alter the reviewed manifest or helper bytes.

The app producer must preflight the explicit reviewed archive before modifying
its fresh staging host. It then removes only that stage's newly compiled command
helper copies, calls `bundle`, and preserves exactly the returned paths while
signing the rest of the app. Verify the pair before signing and after the final
app seal, and refresh the existing helper inventories and complete host hashes.
The copied template manifest/provenance/license and exact helper bytes become
members of the signed host inventory.

A signed build with active CLI policy must require this reviewed asset. Do not
silently recompile or re-sign a fallback pair. No new signed app-receipt field is
required: existing closed command admission validates the signed host inventory
and the original inventory schemas. The external build/acceptance receipt may
record the asset digest. This checkpoint does not edit existing app/host builders,
signing code, backend modules, release policy or published components.

## Validation and remaining acceptance

The focused controls use synthetic helper bytes and explicitly mocked signature
replies for structural failures. A separate actual check admitted and copied the
reviewed signed pair, reconstructed the byte-identical archive, verified strict
Developer ID/Team/hardened-runtime/timestamp/empty-entitlement policy, and rejected
wrong-certificate, existing-output and private-copy mutation cases. All 7,557
original A5 app files retained exact byte/stat identities across extraction and
the actual checks. No helper execution, signing, models or providers were used.

Final integration must prove two distinct app builds preserve this same pair,
then invoke a package prepared and installed under the first build through the
second with real SDK calls and settled ownership. Final signed/public CLI
acceptance remains open. The measured command scope and its false hard-aggregate
and complete-lifetime guarantees remain unchanged.
