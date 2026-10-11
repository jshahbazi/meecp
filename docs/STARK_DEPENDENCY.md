# Stark engine dependency

Meecp consumes the standalone [Stark repository](https://github.com/jshahbazi/stark) through [engines/stark.lock.json](../engines/stark.lock.json). Stark owns the engine and scoring implementation. Meecp owns its dependency selection, consumer checks and eventual package integration.

The lock records the Git commit and tree, Cargo lockfile hash, Rust toolchain, build target and features, readiness/protocol and cache contract, upstream/scoring provenance, and hashes of build configuration, scoring and validation-tool sources. The initial profile enables `optimized` with default features on `aarch64-apple-darwin`. Consecutive caching remains disabled unless the worker is launched with `--consecutive-cache`. Actual binary hashes belong to each build receipt and its acceptance record.

This is the current experimental probability-scoring build path. The historical [Uzu patch builds](UZU_SOL_CONSECUTIVE_CACHE.md#immutable-build-and-verification), accepted binaries and reports remain available for reproduction. Production scorer, HTTP/MCP and installed JevK5 package integration remain unfinished. This dependency work does not establish a new timing result or broader model compatibility.

## Build the committed pin

Run from the Meecp repository root on an Apple silicon Mac with Rust 1.98.1 and the required native build tools:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/stark_dependency.py build
```

The helper fetches the exact locked revision, creates its own source checkout, builds the worker and vocabulary-digest probe, and runs focused Rust tests. Cargo uses its committed dependency lock. The default output is a fresh ignored directory under `runtime/stark/builds/`; `--output NEW_BUILD_DIRECTORY` selects a fresh destination explicitly. Existing build directories are never overwritten.

Each output contains `source/`, `bin/stark-score`, `bin/stark-score-digest`, `DEPENDENCY_LOCK.json` and a private `BUILD_RECEIPT.json`. The receipt binds the selected source, features, build toolchain, dependency lock, binaries and test result. Verify an existing build against the current dependency lock with:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/stark_dependency.py verify \
  --build BUILD_DIRECTORY
```

`--lock PATH` selects another explicit lock file. `build --offline` uses locally available Git objects and Cargo dependencies. Ordinary builds select source through the committed lock; a local checkout is used only when its flag is supplied.

## Update to a reviewed Stark commit

The initial pin is `aa0c959d097803f5e322dea7bbd4b0301f4c7cea`. Updates require a complete 40-character commit SHA:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/stark_dependency.py update \
  --revision REVIEWED_FULL_COMMIT_SHA \
  --output NEW_BUILD_DIRECTORY
```

The helper derives a candidate lock from that commit, builds it and runs focused tests before atomically replacing the selected lock. It checks that the previous lock has not changed concurrently. Failed preparation leaves the previous lock intact. A successful update marks full native acceptance as required; build tests alone do not establish probability or calibration preservation.

Run the native acceptance check described below, review the resulting scalar report and dependency-lock diff, then commit the lock and accepted report with the consumer change. Branch names and moving `main` are not valid revision selectors. Updates are explicit development actions; the installed app does not discover or apply them automatically.

## Test a local Stark checkout

For an engine change being developed alongside Meecp:

```sh
uv run --locked --group dev --python 3.12.13 python scripts/stark_dependency.py build \
  --checkout ../stark \
  --output NEW_DEVELOPMENT_BUILD_DIRECTORY
```

The helper snapshots tracked and nonignored untracked files into its own build directory. It leaves the original checkout and its build output untouched. Dirty source is permitted for this explicit workflow and is recorded in a development-only receipt. Source symlinks are rejected and executable file permissions are preserved. Cargo.lock, the toolchain manifest, root and scorer Cargo manifests, and `.cargo/config.toml` must still match the selected dependency lock; additional `.cargo` configuration files are rejected. The override does not update the committed dependency lock.

After a fix passes consumer checks, land the engine change in Stark and use `update --revision` with its published commit SHA. Keep a generic engine regression test in Stark; keep Meecp-specific integration, configuration and packaging tests here. This makes the accepted engine commit the source for both development directions without maintaining a second engine patch stack.

## Native probability and calibration acceptance

The consumer acceptance wrapper uses the exact source and binaries recorded by the build receipt. It compares the same converted JevK5 BF16 checkpoint with the sealed native scoring reference, including all 243 cases, cold/hit pairs, context boundaries and complete-vocabulary bit digests. Its private inputs and output directory remain outside tracked documentation. The wrapper publishes only scalar counts, hashes and acceptance results.

Compile the owned-process validation reaper from the selected build's source, then run the wrapper with the private reference and verified converted model directories:

```sh
clang -std=c11 -O2 -Wall -Wextra -Werror \
  BUILD_DIRECTORY/source/scripts/stark-validation-reap.c \
  -o PRIVATE_REAPER_PATH
uv run --locked --group dev --python 3.12.13 python scripts/validate_stark_dependency.py \
  --build BUILD_DIRECTORY \
  --reference-directory PRIVATE_REFERENCE_DIRECTORY \
  --reaper PRIVATE_REAPER_PATH \
  --model VERIFIED_BF16_MODEL_DIRECTORY \
  --output-root NEW_PRIVATE_OUTPUT_DIRECTORY
```

The wrapper verifies the build before and after execution and runs the validator from that build's recorded source snapshot. It requires the admitted validation-tool hashes to match; editing the local judge cannot establish acceptance. Its private `DEPENDENCY_ACCEPTANCE.json` binds the acceptance result to the lock, source and binaries. [engines/STARK_VALIDATION.json](../engines/STARK_VALIDATION.json) records the reviewed scalar result for the committed dependency pin. Run the same wrapper on a local-override build before promoting its engine change.

Acceptance must preserve candidate logits, vocabulary log probabilities, calibrated probabilities and typed decisions by type and bits, plus cache invalidation, default-off behavior and process cleanup. The reference is native Uzu/Stark scoring; the earlier probability differences from llama.cpp are unchanged by a successful preservation check. Native KV/prefix-state reuse requires separate implementation and acceptance.

The pinned build and a dirty local checkout snapshot both passed the full native acceptance suite on the same BF16 checkpoint. Each passed 38 Rust tests, all 243 case cold/hit pairs (486 replies), 48 context-boundary pairs (96 replies), 534 complete-vocabulary cold/hit pairs (1,068 replies), 243 tokenizations and 20 sequence steps. Candidate probabilities, calibration, typed decisions and raw vocabulary digests stayed bit-exact against the frozen native reference. The [public acceptance record](../engines/STARK_VALIDATION.json) binds these results to the dependency lock, consumer helpers, source snapshots and binaries.

Model-free CI checks bind the public record to the exact pin and consumer helper hashes. A fresh full native run with the private reference and model is required after an engine change; CI record checks do not rerun that model suite. Updating a disposable lock to the same published commit also completed build and test checks, and an unknown commit was rejected while preserving the previous lock.

## Roll back

Restore the previous committed `engines/stark.lock.json`, build that pin into a fresh directory, and run the same consumer checks. Commit the restored lock and its scoped acceptance result. Keep previous immutable build directories and reports available while evaluating an update. Changing the dependency lock does not replace an already installed package.
