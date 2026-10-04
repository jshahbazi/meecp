# TextGen 1.0.8 signed-asset producer source

This record supplements the frozen signed-runtime provenance without changing
its bytes, the signed archive, or the published 1.0.8 component/recipe identities.

The asset was produced while the checkout's Git HEAD was
`bcc3120f8e25296f3639d57f496e18de200b3980`. Three new producer/helper files were
present but untracked. The provenance's `source_commit` records that checkout
base; it does not identify the full composed source by itself. The five
`producer_source_sha256` entries identify the executed producer inputs. The
later application build used committed source
`2e29ee336b10c00eb8b3c58b0f08c3447a450d14`, after app-distribution integration.
Its app-builder bytes are consequently different from those used to produce
the signed asset.

There was no separate full source snapshot captured at asset-production time.
The exact producer composition can now be reconstructed from retained immutable
Git objects: start with the full tree at `bcc3120`, then add the three files
identified below from `2e29ee3`. All five recovered producer hashes were checked
against the original signed-runtime provenance. This is a later reconstruction,
not a contemporaneous filesystem capture.

| File | Git object source | SHA-256 recorded in original provenance |
| --- | --- | --- |
| `backend/mcpme/preparation_builder.py` | `bcc3120` | `04c1195c76cdb1d7aa5cbccff706f0ff0d1211b0b2c8542a458165c9d3c0a7d6` |
| `scripts/build_mcpme_app.py` | `bcc3120` | `8e61bb700836a78fcc814f09447487d8426f49b541b83ff01165f30a526255df` |
| `backend/mcpme/preparation_textgen_signed_runtime.py` | `2e29ee3`, added file | `ffe4530f3af75d62ca2c4f2b80201f3519cf891cc927486f252bb641ae99c35a` |
| `scripts/build_signed_textgen_runtime.py` | `2e29ee3`, added file | `ba8c309ccbb55091064cb80b79c0de45fc31849e164ed939917674b16978ca07` |
| `scripts/signed_textgen_asset.py` | `2e29ee3`, added file | `dce74704ca32ce82924f18179fa5c695a2121f4678884a0845fa7dbbe07dc80d` |

An exclusive private reconstruction capsule contains all 641 files in that
composed tree. Its SHA-256 is
`04bb41143ad4a9308ac524d492c7253674a58283eab5f5f122a14758205bb212`.
Every extracted file was rehashed against the reconstruction inventory. Capsule
file modes are normalized to `0600`; it preserves source bytes, not historical
filesystem identities. The private reconstruction receipt SHA-256 is
`e00e4723cea3852c57e607f0e584461c6e935b36a078f06fb47dcadb7b9d3ce1`.

This source recovery supports audit of the original signing operation. Running
the producer again would create new secure timestamps and a different signed
asset. Future builds of this profile must use the existing archive SHA-256
`fabbfa2b2960a5ea0d6d54866f2ced4e806269b5b84eb6a133e41d273839c92e`,
not replace the frozen asset with a re-signed reconstruction. A changed runtime
or producer contract requires a separately versioned immutable profile.
