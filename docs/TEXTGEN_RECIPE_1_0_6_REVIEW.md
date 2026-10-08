# Text-generation recipe 1.0.6 refresh

This checkpoint mints the next immutable text-generation recipe after the
backend source composition froze at `916283b`. The trusted family version is
`1.0.6`; published recipe directories `text-generation-v1`, `1.0.1` through
`1.0.5` remain unchanged and remain catalog history only through their existing
records and lifecycle handling.

The new bundle was generated into the previously nonexistent
`templates/builtin-recipes/text-generation-v1-0-6` directory with
`write_textgen_recipe`, using `PYTHONPATH=backend uv run --locked`. It contains
seven files and validates to digest
`29214dca106f1e1e0e2c8895b6a1a9ee658386b432fcb0fc08b96d5da54d736c`.

Only these source controls changed: `backend/meecp/preparation_textgen.py`
version `1.0.5` to `1.0.6`, the one text-generation catalog entry, and the
exact public model-provenance path in `.github/scripts/scan_secrets.py`.
Changing the family version changes the producer identity for that module and
therefore the new component and recipe digests. Existing bundle bytes and
unrelated catalog entries and historical registry records remain preserved.
No runtime, provider, model, credential or
signing operation was used.

The source, old-bundle inventory, new-bundle validation, catalog identity,
public-provenance equality, and focused test/scanner outcomes are recorded in
the accompanying tracked public metadata packet. Git stores the bundle and
packet as ordinary source files; their author's local `0600` mode is not a
portable confidentiality guarantee.

Focused verification passed 90 text-generation tests, 20 lifecycle harness
tests, and the fixed-count secret scanner. Core recipe checks passed 81 tests;
one pre-existing CLI assertion failed because this integration catalog already
contains the agent recipe entries that the older test expected to omit
(`test_meecp_recipe_cli.py::test_owner_add_enable_variant_and_restart_through_private_cli`).
That failure is recorded as `catalog_expected_subset_stale` in the author
packet. Root verification corrected the CLI test's closed inventory to all seven
current recipe IDs.

## Root verification

The integrated checkpoint is `a2049ef`. A different author independently
verified all 12 checkpoint files and exact current component bindings, all six
earlier bundles containing 42 files, the seven-file new bundle, unchanged public
provenance and the single new catalog identity. All 57 static controls passed.
The private immutable review receipt is
`/private/tmp/mcpme-textgen-106-root-static-review-v2.json`, mode `0600`, SHA-256
`cb99c44cc355cd0b66e5ac9ba2ddfe05a27debfbdd8c2d834a53512581904edc`.

The previous static checker omitted the manifest's default empty `config`
field from its expected step dictionaries. Its failed v1 receipt is preserved.
The v2 checker validates all four exact keys, empty configuration and all exact
component/version/digest bindings.

Root's focused run passed 99 checks with no skips in 12.7 seconds, including
seven new durable historical-upgrade controls and the corrected CLI inventory.
These controls cover disable/revoke/remove/deprecate across seeding and restart,
retained exact historical snapshots, collection tombstones and unchanged
provenance/current bindings. They execute no historical worker.
Actual inference, the newly built coherent host, signed installed acceptance and
the final whole-phase review remain open.
