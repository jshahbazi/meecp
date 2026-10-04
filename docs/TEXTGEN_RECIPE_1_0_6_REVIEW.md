# Text-generation recipe 1.0.6 refresh

This checkpoint mints the next immutable text-generation recipe after the
backend source composition froze at `916283b`. The trusted family version is
`1.0.6`; published recipe directories `text-generation-v1`, `1.0.1` through
`1.0.5` remain unchanged and remain catalog history only through their existing
records and lifecycle handling.

The new bundle was generated into the previously nonexistent
`templates/builtin-recipes/text-generation-v1-0-6` directory with
`write_textgen_recipe`, using `PYTHONPATH=backend uv run --locked`. It contains
seven files, all mode `0600`, and validates to digest
`29214dca106f1e1e0e2c8895b6a1a9ee658386b432fcb0fc08b96d5da54d736c`.

Only these source controls changed: `backend/mcpme/preparation_textgen.py`
version `1.0.5` to `1.0.6`, the one text-generation catalog entry, and the
exact public model-provenance path in `.github/scripts/scan_secrets.py`.
Changing the family version changes the producer identity for that module and
therefore the new component and recipe digests. Existing bundle bytes and
catalog entries remain preserved. No runtime, provider, model, credential or
signing operation was used.

The source, old-bundle inventory, new-bundle validation, catalog identity,
public-provenance equality, and focused test/scanner outcomes are recorded in
the accompanying private packet.

Focused verification passed 90 text-generation tests, 20 lifecycle harness
tests, and the fixed-count secret scanner. Core recipe checks passed 81 tests;
one pre-existing CLI assertion failed because this integration catalog already
contains the agent recipe entries that the older test expected to omit
(`test_mcpme_recipe_cli.py::test_owner_add_enable_variant_and_restart_through_private_cli`).
That failure is recorded as `catalog_expected_subset_stale`; no source change
was made for it.
