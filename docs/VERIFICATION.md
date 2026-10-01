# Local app verification

Verified 2026-09-30 on the user's M5 Max (40-core GPU, 128 GB), macOS 26.7.1. All decisions used synthetic evidence and public model weights. This is an integration and runtime check, not an evaluation of task accuracy.

## Delivered artifact

- Repository: `/Users/johnshahbazian/development/jevk5-menubar`, local Git branch `codex/initial-app`. No remote or PR was created, as requested.
- App: `dist/JevK5.app`, approximately 4.3 GiB including the 4.48 GB Q8 model and standalone runtimes.
- Model: `JevK5-4B-v0.3-Q8_0`, SHA-256 `aea433883bc7ed399f2fbd539e53d2eac7caf71a946fe6650995a413979d4a30`.
- Runtime: llama.cpp 0.5.0/build 11146, ggml 0.25.3, CPython 3.12.13, official MCP SDK 1.30.0. Server dependencies are pinned in `uv.lock`.
- Codex: the global `jevk5` entry was added and read back with `codex mcp get jevk5 --json`; it is enabled and points to the app's bundled stdio adapter. Startup/tool timeouts are 30/180 seconds. Other MCP entries were preserved.

## Checks

`swift build -c release` succeeded. `uv run pytest -q` passed all 34 tests. These include request validation, authentication, Host/Origin/body policies on all routes, sanitized MCP failures, strict option coverage/context/truncation, exact-consecutive caching, bounded queueing, inference cancellation retaining the shared lock, pause/resume, startup flags, parent watchdog, process locking, and first-use model hash validation. A real SDK client tests negotiation, tool discovery, structured results, literal JSON-looking evidence preservation, and read-only tool annotations against an in-process HTTP transport.

The actual packaged model passed 18 integration checks through real Streamable HTTP and stdio SDK clients. The checks covered initialization/tool listing, boolean/choice/score decisions, authentication, invalid Origin/Host, malformed JSON, oversized input, too many criteria, over-context rejection, identical repeats, changed evidence, literal-string parity, concurrent callers sharing one model, pause/unload, paused rejection, and fresh cache after resume.

On the 130-token synthetic example, the final integration run before metadata-only annotations measured 75.8 ms for the fresh service decision and 17.1 ms median over ten identical repeats. The initial bundle run measured 69.6/19.7 ms. Every repeat matched the fresh boolean probability exactly, and logical input-token counts stayed complete. These service timings exclude client transport/JSON-RPC overhead and are descriptive, not a statistically controlled benchmark.

Six process/relocation checks passed against the actual bundle:

1. SIGKILL of the owned backend terminated/reaped its supervisor and inference child.
2. A stdio client reopened the existing menu app and recovered a missing backend.
3. Terminating the menu app stopped its backend and model.
4. Moving the bundle to `dist/Relocation Test/JevK5.app` preserved stdio auto-launch.
5. The relocated bundle performed a real model decision using its own runtime.
6. Restoring the original path preserved auto-launch and readiness.

The relocated runtime's `DYLD_PRINT_LIBRARIES` output contained no Homebrew library paths. It loaded the bundled Metal, BLAS, and CPU plugins, and `--list-devices` identified MTL0 as Apple M5 Max. This catches the Homebrew compiled-plugin-directory trap; setting `GGML_BACKEND_PATH` to a directory alone was insufficient. The builder now disables that copied directory constant, sets the plugin working directory, relocates Mach-O imports, records hashes, and signs after rewriting. `codesign --verify --deep --strict` passed.

## Limits and user setup

The native app launched, owned the tested backend, and remained running after the checks. The automated UI inspection tool timed out on this accessory/menu-only app; menu clicking, clipboard copying, launch-at-login registration, and the Quit menu action were not visually verified. The source compiled and received independent correctness review; process exit and the control API were tested directly. Launch-at-login is off by default.

The app is ad hoc signed for local use. Developer ID signing, notarization, public distribution, and automatic updates are not implemented. The first packaged runtime requires Apple silicon and macOS 26. The model runs entirely locally; the app has no request history or telemetry. Owner-only local connection and verification files live in Application Support and are excluded from Git.

Codex's registration is configured and the underlying adapter has been tested. An existing Codex session may need its MCP connection restarted to refresh its tool catalog; the current chat's tool list was loaded before registration. Use Codex Settings → MCP servers → `jevk5` → Restart. The MCP server does not force the LLM to invoke the model.

## Reproduce

```sh
uv run pytest -q
open dist/JevK5.app
uv run scripts/verify_app.py
uv run scripts/verify_lifecycle.py
codex mcp get jevk5 --json
codesign --verify --deep --strict dist/JevK5.app
```

Integration receipts contain aggregate counts, numeric timing/cache differences, model identity, and check names only. They remain in ignored `test-artifacts/`. The lifecycle test deliberately terminates only verified processes belonging to this bundle and briefly moves the bundle; run it when no other client has an active decision. Build the app with `uv run scripts/build.py`; the model is kept in ignored `models/` and bundled into ignored `dist/`.
