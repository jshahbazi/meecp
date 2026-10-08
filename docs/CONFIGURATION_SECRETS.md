# Package configuration and secret references

Prepared packages can declare typed nonsecret configuration and logical secret names. The owner selects configuration and immutable secret references when planning an installation. The exact plan binds those choices; the installed generation retains them across host restart. The native setup window and owner CLI/API support this flow. Editing installed configuration creates a newly reviewed [generation upgrade](UPGRADES_RECOVERY.md).

## Declare the package contract

Query `meecp capabilities --json` for `configuration.schema_versions` before targeting this optional package-schema-1 extension. Older hosts reject unknown manifest fields. A manifest may contain:

```json
{
  "configuration": {
    "schema_version": 1,
    "fields": {
      "greeting": {"type": "string", "required": true, "min_length": 1, "max_length": 64},
      "enabled": {"type": "boolean", "default": true},
      "attempts": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5}
    }
  },
  "access": {"secrets": ["service_key"]}
}
```

The configuration schema is closed and versioned. Fields accept strict strings, integers, or booleans; integer `1` cannot stand in for boolean `true`. Defaults and enumerated choices must satisfy the field's type and bounds. String fields support `min_length`/`max_length`; integers support `minimum`/`maximum`. `enum` accepts at most 32 choices. Nested values, floating-point values, null, arbitrary schemas, and external references are unsupported.

There are at most 32 fields and 16 secret names. Logical names match `[A-Za-z][A-Za-z0-9_]{0,63}`. Effective nonsecret configuration is capped at 16 KiB of UTF-8 JSON, including defaults. Strings are capped at 4,096 characters and integers at the host's exact JSON integer bound. Unknown fields and missing required choices fail before package execution.

Configuration values appear in plans, owner/read-authorized details, and exported draft requirements. Supply only nonsecret values there. Credential material belongs in the private secret store. Names and descriptions must also be nonsecret.

## Create a reference and review the plan

Use an existing owner-only UTF-8 file containing the exact secret bytes. The CLI requires current-user ownership, mode 0600, a regular file with one link, safe ancestors, and no symlink. Values are nonempty, at most 4,096 UTF-8 bytes, with no NUL. A trailing newline is retained as part of the value.

```sh
scripts/meecp secrets create --name "Service credential" --file /absolute/private/service-secret.txt --json
scripts/meecp secrets list --json
scripts/meecp secrets inspect SECRET_UUID --json
```

Creation returns `secret_id`, `name`, `status`, and `created_at`. Inspection never returns a value, value hash, or value length. Records live in the host's owner-only `secrets/records` directory; this milestone uses private files, without a Keychain backend. The host retains at most 4,096 reference records. List returns the latest 100 metadata summaries and the total count.

Save this settings envelope in a private mode-0600 JSON file, replacing `SECRET_UUID` with the returned canonical UUID:

```json
{
  "schema_version": 1,
  "configuration": {"greeting": "Hello", "enabled": true},
  "secret_references": {"service_key": "SECRET_UUID"}
}
```

Both maps are required when supplying an envelope. The secret map must exactly match the package's declared logical names. An optional field with no selected value or default stays absent. Planning fills defaults and binds the effective settings and reference IDs into `settings_digest` and the complete `plan_digest`:

```sh
scripts/meecp install plan --package /absolute/path/to/package --settings /absolute/private/settings.json --name "My App" --json
scripts/meecp install inspect --plan PLAN_UUID --json
scripts/meecp install apply --plan PLAN_UUID --json
scripts/meecp operations get OPERATION_UUID --json
```

Plans and slot state contain reference IDs, with no credential material or credential-derived digests. Missing, revoked, unsafe, or corrupt references produce `secret_reference_unavailable`. The host checks references at plan/apply, before launch, after spawn acknowledgment, and before candidate exposure. Edited settings fail their generation-bound digest check. A retry of an already recorded installation recovers its operation.

For source imports, save `configuration` and a `secret_references` map in the draft's `requirements` through `imports revise --requirements /absolute/private/requirements.json`. The external preparation export contains those nonsecret choices and opaque IDs. The returned package must declare a compatible contract. `install plan --import` uses that exact draft revision; `--settings` cannot override it. Old label-list secret references remain blocked until replaced with owner-managed IDs. Automated preparation jobs retain their existing unsupported-secret boundary.

## Runtime delivery and revocation

The supervisor gives each selected package process a private inherited descriptor named by the numeric `MCPME_SETTINGS_FD` environment variable. Read it once to EOF and close it. It contains `{ "schema_version": 1, "configuration": {...}, "secrets": {...} }`, where `secrets` maps declared logical names to resolved strings. A Python adapter can read it with:

```python
import json
import os

with os.fdopen(int(os.environ.pop("MCPME_SETTINGS_FD")), "rb") as stream:
    settings = json.loads(stream.read(524289))
configuration = settings["configuration"]
selected_secrets = settings["secrets"]
```

Packages declaring configuration or secrets receive this descriptor, including when all optional values are absent. Packages without either declaration retain their existing launch interface. Stdio session children and declared model-service processes receive only the selected package settings. The gateway receives no secret material. Descriptor delivery has a finite deadline; failed or canceled delivery waits for the owned child's cleanup. The host puts no values in argv, environment values, package artifacts, import exports, operation journals, or error responses. Package authors must preserve the same privacy rules in their own code.

```sh
scripts/meecp secrets revoke SECRET_UUID --json
```

References are immutable. Rotation creates a new reference and requires a newly reviewed installation or configuration upgrade. Revocation durably replaces the active record with a metadata tombstone and prevents later resolution and new starts. Already delivered values may remain in a running process; stop that app when retiring its credential. Removing an app retains its nonsecret settings and referenced records. This is not a secure-erasure guarantee.

All four secret-store API routes require owner authority: `POST/GET /v1/secrets`, `GET /v1/secrets/{secret_id}`, and `POST /v1/secrets/{secret_id}/revoke` with `{}`. Creation accepts exactly `{ "name": ..., "value": ... }`. Existing read, prepare, lifecycle, and install grants cannot administer or inspect the store. An exact install grant covers the secret references already reviewed in that plan. Ordinary slot invocation credentials retain no management authority.

## Verification scope

`uv run --locked python scripts/verify_settings.py --host /absolute/path/to/host --package /absolute/path/to/json-utilities-package` builds a synthetic prepared stdio package using the bundled private runtime. It exercises private secret-file input, metadata-only owner commands, missing choices, scoped-grant denial, exact plans, duplicate apply, real HTTP and stdio SDK calls, restart, and revoked-reference failure. The fixture returns booleans without returning secret material. This check uses a temporary installation, synthetic values, and relocated paths; it does not access the user's normal installation.

Native configuration and installed settings upgrades use the same contract and private delivery. [PHASE2_COMPLETION.md](PHASE2_COMPLETION.md) records the remaining final acceptance checks. Reference-document services and integrated provider preparation remain later work.
