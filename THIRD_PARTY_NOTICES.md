# Third-party notices

The app bundles public JevK5 model weights and a minimal upstream Python client. Their source, licenses, and attribution remain separate from the application code.

The first mcpme CLI host bundles CPython 3.12.13 and the server dependencies in `uv.lock`. It does not include the JevK5 weights or inference engine. Its build receipt records source-file and launcher hashes plus the compiler identity. The deterministic example uses the macOS system Swift/Foundation runtime and includes its source and MIT license in the prepared package.

The native mcpme app wraps that host with AppKit and ServiceManagement. The separately prepared `local.jevk5` package includes the pinned model and verified relocated engine below, its own private Python and locked dependencies, the unmodified vendored scorer, and the host-attached adapter in `backend/mcpme_jevk5`. Its package lock records the complete artifact tree. The recovery builder preserves the original JevK5 bundle and changes only a copied bundle's native handoff entrypoint and signatures.

| Component | Pinned source / local build | License |
|---|---|---|
| JevK5 prompt and GGUF client | allebee/jevk5 `f944fe37ff1d5ed3830aa4c8d88b7189c8c1268a` (v0.3.3) | Apache-2.0; see `vendor/jevk5/LICENSE` and `NOTICE` |
| JevK5-4B v0.3 Q8 GGUF | alibiserikbay/JevK5-GGUF `ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a` | Apache-2.0; Qwen/model/dataset notices retained in the JevK5 NOTICE and model card |
| llama.cpp | Homebrew 0.5.0, build 11146 / `7fe450e19305b828c199d602c23a8337aaa1f03b` | MIT |
| ggml, Metal/CPU/BLAS backends | Homebrew 0.25.3 | MIT |
| LLVM OpenMP (`libomp`) | Bundled dependency of the CPU backend | Apache-2.0 with LLVM exception |
| OpenSSL | Bundled dependency of llama-server | Apache-2.0 |
| CPython standalone runtime | uv-managed CPython 3.12.13, python-build-standalone | Python Software Foundation and included component licenses |
| MCP Python SDK | 1.30.0, official modelcontextprotocol/python-sdk | MIT |
| Server dependencies | Exact versions in `uv.lock` | Licenses retained in bundled wheel metadata |

The app's Resources include runtime license files and the complete package license metadata from the installed wheels. CPython includes its own component license directory. Vendored JevK5 files are unmodified upstream files; the application-specific cache and service wrapper is in `backend/jevk5_app`. Bundled Mach-O files have relocated load paths; the copied ggml library's compiled Homebrew backend directory string is disabled so plugins load from the bundle's working directory. Source and packaged SHA-256 values are recorded in `runtime-manifest.json`.

Upstream links: [JevK5](https://github.com/allebee/jevk5), [model](https://huggingface.co/alibiserikbay/JevK5-GGUF/tree/ec67b0bfce5119a8b11a2cdb430bb43e3fa3e82a), [llama.cpp](https://github.com/ggml-org/llama.cpp), [ggml](https://github.com/ggml-org/ggml), [MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
