# jevk5-llamacpp-v1

Template version 1. Package schema 1, host API 1, Apple silicon macOS 26+, `model-service`, tools-only MCP `2025-11-25`.

This is the tested JevK5 recipe, not a generic GGUF adapter. Use the existing [preparer](../../scripts/prepare_jevk5_package.py), [native exec launcher](../../scripts/jevk5-package-launcher.c), and [private descriptor entrypoints](../../backend/mcpme_jevk5/__main__.py). Read [DESIGN.md](../../docs/DESIGN.md) before altering the decision contract.

The pinned model is `JevK5-4B-v0.3-Q8_0`: 4,482,402,720 bytes, SHA-256 `aea433883bc7ed399f2fbd539e53d2eac7caf71a946fe6650995a413979d4a30`, model revision `ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a`, and upstream scorer `f944fe37ff1d5ed3830aa4c8d88b7189c8c1268a`. The declared engine is llama.cpp build `11146-7fe450e19305b828c199d602c23a8337aaa1f03b`; the recipe assumes that tested build and its relocated ggml/Metal/CPU dependency closure.

Preparation requires a verified legacy JevK5 bundle, a freshly built private mcpme host, and the pinned model at `models/`:

```sh
uv run --locked python scripts/prepare_jevk5_package.py \
  --legacy-app /absolute/path/to/verified/JevK5.app \
  --host /absolute/path/to/mcpme-host \
  --output /absolute/path/to/agent-workspace/jevk5-package
```

The preparer verifies every relocated llama artifact against the legacy bundle's `runtime-manifest.json` and checks the pinned weights before copying. It includes private Python, locked dependencies, vendored scorer/licenses, model provenance, and new native launchers. It removes the old fixed-name reaper; the host owns both the real engine and the adapter. The adapter depends on engine readiness, receives endpoint/key/generation through a private inherited descriptor, binds loopback, and does not launch another engine. Two clients share one inference slot, one complete-decision lock, and the exact consecutive-token cache.

A raw Homebrew `llama-server` is not the relocated `llama/llama-server.real` artifact in a verified bundle. Its bytes, plugin loading paths, ABI, version, and library closure must be established before packaging. A raw-engine mismatch, changed receipt, unknown ggml backend-directory layout, missing Metal plugin/license, different model/profile, or unsupported credential/readiness injection blocks preparation. Do not repair a mismatch by rewriting its expected hash or substituting another model. The [legacy builder](../../scripts/build.py) performs relocation and signing for its supported layout; changed raw engines require an independently reviewed build and compatibility check. A listening port does not establish model readiness.

The existing preparer derives a source identity from its adapter/scorer build. It does not consume a Phase 2 import export. Returning a result to a raw-source draft additionally requires proving that the selected snapshot is the source actually used, preserving the export's exact `source.revision` in the manifest, regenerating the lock, and recording the exact draft/revision. The generic stdio generator does not perform this reconciliation. Until an audited preparation recipe can establish that mapping, retain the draft as preparation needed and use a directly validated prepared JevK5 package through the prepared-package route. A model file by itself is insufficient; raw model intake and universal engine preparation are outside this onboarding milestone.

For candidate verification, use two real MCP clients, compare `jevk5_decide`/`jevk5_status` contracts, check the exact engine and model identities, confirm one engine instance, and verify synthetic fresh/repeated/changed inputs give cache-hit flags false/true/false. Model functional checks do not establish calibration or general accuracy. [Phase 1 verification](../../docs/MCPME_VERIFICATION.md) records the tested lifecycle and transport behavior.
