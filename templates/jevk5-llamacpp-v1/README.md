# jevk5-llamacpp-v1

Template version 1. Package schema 1, host API 1, Apple silicon macOS 26+, `model-service`, tools-only MCP `2025-11-25`.

This recipe supports the pinned JevK5 contract. The versioned [generator](prepare.py) derives the package manifest and lock from a Phase 2 export. The compatibility [preparer](../../scripts/prepare_jevk5_package.py) also supports a verified legacy bundle. Read [DESIGN.md](../../docs/DESIGN.md) before altering the decision contract.

The pinned model is `JevK5-4B-v0.3-Q8_0`: 4,482,402,720 bytes, SHA-256 `aea433883bc7ed399f2fbd539e53d2eac7caf71a946fe6650995a413979d4a30`, model revision `ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a`, and upstream scorer `f944fe37ff1d5ed3830aa4c8d88b7189c8c1268a`. The declared engine is llama.cpp build `11146-7fe450e19305b828c199d602c23a8337aaa1f03b`; the recipe assumes that tested build and its relocated ggml/Metal/CPU dependency closure.

Begin with the raw GGUF and an explicitly selected relocated engine build. A build directory contains `llama/`, `licenses/`, and `runtime-manifest.json`; a verified legacy `JevK5.app` can supply that build. Intake reads headers, streams binary hashes, and privately copies only the declared engine closure and licenses. It never executes either artifact.

```sh
mcpme imports create --source /absolute/path/to/weights.gguf \
  --model-engine /absolute/path/to/verified-engine-build \
  --name "JevK5 Judge" --intent "Expose JevK5 decisions and status" --json
```

Select the explicit requirements through the native draft editor or `imports revise --revision N --requirements` with a JSON file containing:

```json
{
  "engine_profile": "jevk5-llamacpp-v1",
  "profile": "model-service",
  "tools": ["jevk5_decide", "jevk5_status"],
  "memory_mb": 8192
}
```

A model-only draft retains actionable `engine_artifacts_required`, `model_profile_required`, `model_service_required`, and `model_tools_required` blockers. Add its missing build with `mcpme imports engine DRAFT --revision N --model-engine BUILD --json`, then update the requirements. Both actions advance the revision. An unsupported GGUF version, weight identity, engine closure, receipt, or missing license is rejected before activation.

Export the current draft and run the generator in the external agent's workspace:

```sh
umask 077
mcpme imports export DRAFT_UUID --revision REVISION --json > setup.json
uv run --locked python templates/jevk5-llamacpp-v1/prepare.py \
  --setup-export /absolute/path/to/setup.json \
  --host /absolute/path/to/private-mcpme-host \
  --output /absolute/path/to/agent-workspace/jevk5-package
mcpme imports attach DRAFT_UUID --revision REVISION \
  --package /absolute/path/to/agent-workspace/jevk5-package --json
```

The shipped host contains the same generator and its adapter/scorer source assets. It can run through the bundled interpreter with the host's private site-packages on its import path, as demonstrated by [model acceptance](../../scripts/verify_model_phase2.py). The generator consumes the selected private snapshot, verifies every artifact and declared executable bit, checks the relocated engine's arm64 library closure and actual pinned version, and produces the manifest, lock, licenses, and preparation receipt. Engine execution happens only in this explicit foreground preparation command. A user does not edit host source or author a manifest.

`source.kind=artifact_collection`, `contract_version=1` separates these binary inputs from UTF-8 source-folder snapshots. Its canonical identity binds artifact paths, roles, sizes, hashes, and executable declarations. Weights support the 64 GiB binary intake bound; the accepted recipe remains the exact 4.48 GB pin above. Runtime artifacts have a 2 GiB aggregate bound, and license/receipt metadata remain bounded. Multi-GiB copying occurs outside SQLite write transactions. The receipt records the draft/revision, exact source inventory, engine build, and adapter/scorer source hash without source contents or build logs. Static attachment requires those exact weight/engine bytes and receipt; stale returns or edited artifacts fail.

The original legacy workflow remains available:

```sh
uv run --locked python scripts/prepare_jevk5_package.py \
  --legacy-app /absolute/path/to/verified/JevK5.app \
  --host /absolute/path/to/mcpme-host \
  --output /absolute/path/to/agent-workspace/jevk5-package
```

The preparer verifies every relocated llama artifact against the legacy bundle's `runtime-manifest.json` and checks the pinned weights before copying. It includes private Python, locked dependencies, vendored scorer/licenses, model provenance, and new native launchers. It removes the old fixed-name reaper; the host owns both the real engine and the adapter. The adapter depends on engine readiness, receives endpoint/key/generation through a private inherited descriptor, binds loopback, and does not launch another engine. Two clients share one inference slot, one complete-decision lock, and the exact consecutive-token cache.

A raw Homebrew `llama-server` is not the relocated `llama/llama-server.real` artifact in a verified bundle. Its bytes, plugin loading paths, ABI, version, and library closure must be established before packaging. A raw-engine mismatch, changed receipt, unknown ggml backend-directory layout, missing Metal plugin/license, different model/profile, or unsupported credential/readiness injection blocks preparation. Do not repair a mismatch by rewriting its expected hash or substituting another model. The [legacy builder](../../scripts/build.py) performs relocation and signing for its supported layout; changed raw engines require an independently reviewed build and compatibility check. A listening port does not establish model readiness.

The legacy workflow derives its source identity from the adapter/scorer build. The `--setup-export` workflow uses the exact import source identity instead, and supplies the receipt that proves the selected raw artifact mapping. Both produce the same host-owned process graph and adapter contract. Further GGUF models and arbitrary engine source builds require their own recipe and compatibility evidence.

For candidate verification, use two real MCP clients, compare `jevk5_decide`/`jevk5_status` contracts, check the exact engine and model identities, confirm one engine instance, and verify synthetic fresh/repeated/changed inputs give cache-hit flags false/true/false. Model functional checks do not establish calibration or general accuracy. [Phase 1 verification](../../docs/MCPME_VERIFICATION.md) records the tested lifecycle and transport behavior.
