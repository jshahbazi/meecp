# Qwen2.5 0.5B compatibility fixture

This directory retains the publisher's Apache-2.0 license for the development-only general text generation fixture. Weights remain ignored under `models/` and are not distributed in this repository.

- Publisher: [Qwen/Qwen2.5-0.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF).
- Immutable revision: `9217f5db79a29953eb74d5343926648285ec7e67`.
- Selected file: `qwen2.5-0.5b-instruct-q4_k_m.gguf` (Q4_K_M), 491,400,032 bytes.
- Weight SHA-256: `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`.
- [Original license](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/blob/9217f5db79a29953eb74d5343926648285ec7e67/LICENSE), SHA-256 `832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e`.

The tokenizer and chat template are embedded in those exact weights. Their measured identities and the selected private engine closure are recorded in [the compatibility ledger](../../docs/PHASE3_LLM_COMPATIBILITY.md). No model-quality or broader-family compatibility claim follows from the synthetic execution checks.
