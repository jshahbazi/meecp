# Playground model provenance

## Bonsai 1.7B

- Publisher: Prism ML.
- Repository: [prism-ml/Ternary-Bonsai-1.7B-gguf](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf).
- Immutable revision: `983b5dec2ff16aab79990711ba0f828a499a7e6a`.
- File: `Ternary-Bonsai-1.7B-Q2_0_g64.gguf`.
- Download: https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/resolve/983b5dec2ff16aab79990711ba0f828a499a7e6a/Ternary-Bonsai-1.7B-Q2_0_g64.gguf
- Size: `490163968` bytes.
- SHA-256: `6d0ecb3d9055969b5cde332b6fdb60e67ed3599e9f73e56b977731e1467e5c91`.
- Format: mainline-compatible ternary `Q2_0`, group size 64.
- License: Apache-2.0; see the pinned publisher [LICENSE](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/blob/983b5dec2ff16aab79990711ba0f828a499a7e6a/LICENSE) and [NOTICE](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/blob/983b5dec2ff16aab79990711ba0f828a499a7e6a/NOTICE.txt).

## MiniCPM5 2B Claude Fable5 1 Thinking Agentic

- Upstream base: [openbmb/MiniCPM5-2B](https://huggingface.co/openbmb/MiniCPM5-2B), immutable revision `f97400052a43d642bbc6e9975e2397e3ae6a6b52`.
- Fine-tune: [GnLOLot/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic](https://huggingface.co/GnLOLot/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic), immutable revision `934bcdfc20af23e97971704ab21ae3a8bdf7373c`.
- Quantization repository: [mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF](https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF), immutable revision `02dba166d01676e36710a7de183d6a5e0689b5f5`.
- File: `MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-IQ2_XXS.gguf`.
- Download: https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF/resolve/02dba166d01676e36710a7de183d6a5e0689b5f5/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-IQ2_XXS.gguf
- Size: `799476064` bytes.
- SHA-256: `de17e225b332223a86fb8f26238779ba2607f5e95ee995f5abae31e0c50fef0e`.
- GGUF metadata: architecture `llama`; 42 blocks; embedding length 2048; context length 131072; BOS ID 0; EOS ID 1; `add_bos_token=false`; `add_eos_token=false`; quantization version 2.
- Tensor types: `IQ2_XXS` (247 tensors), `Q4_K` (42), `F32` (85), `Q2_K` (6), and `Q5_K` (1). These names use the installed ggml enum (`GGML_TYPE_IQ2_XXS = 16`, `GGML_TYPE_IQ2_XS = 17`); the GGUF contains mixed tensor types.
- Embedded `tokenizer.chat_template`: compared with the exact upstream `chat_template.jinja` at the pinned base revision. It is not byte-identical: the embedded template adds tool-call conversion logic for `<tool_sep>` and changes the final tool-call condition to `message.tool_calls and not has_tool_sep`. The shared base rendering, BOS handling, role formatting, reasoning prefill, and generation prompt structure are otherwise retained.
- License: Apache-2.0. The exact Apache license text used by the MiniCPM project is [licenses/MiniCPM5-2B-LICENSE](licenses/MiniCPM5-2B-LICENSE), retrieved from the primary [OpenBMB/MiniCPM license at immutable commit `316cfb1cea39f39cfa16b4f5703b77495c2340be`](https://github.com/OpenBMB/MiniCPM/blob/316cfb1cea39f39cfa16b4f5703b77495c2340be/LICENSE).

### Q4_K_M variant

- Repository and immutable revision: [mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF](https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF/tree/02dba166d01676e36710a7de183d6a5e0689b5f5), `02dba166d01676e36710a7de183d6a5e0689b5f5`.
- File: `MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-Q4_K_M.gguf`.
- Download: https://huggingface.co/mradermacher/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic-i1-GGUF/resolve/02dba166d01676e36710a7de183d6a5e0689b5f5/MiniCPM5-2B-Claude-Fable5-1-Thinking-Agentic.i1-Q4_K_M.gguf
- Size: `1561319776` bytes.
- SHA-256: `7a255ce8710a9d17eba9afe141ef2b7c1c3a2e9b9da9b51ce693b342df54710b`.
- Metadata matches the IQ2 artifact: Llama architecture, 42 blocks, 2048 embedding, 6144 feed-forward, 16/2 attention heads, 131072 context, and the same embedded chat template. Tensor types are `Q4_K` (253), `Q6_K` (43), and `F32` (85).

Controlled runtime comparison: the MiniCPM IQ2 artifact failed the same greeting/follow-up probe on both CPU and Metal with the same runtime and prompt format, while the MiniCPM Q4_K_M artifact passed. The IQ2 artifact remains retained for CLI negative-profile testing and is excluded from the GUI model selector.

## Qwen3.5 2B Q4_K_M

- Quantization repository: [unsloth/Qwen3.5-2B-GGUF](https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/tree/f6d5376be1edb4d416d56da11e5397a961aca8ae), immutable revision `f6d5376be1edb4d416d56da11e5397a961aca8ae`.
- Base model: [Qwen/Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc), immutable revision `15852e8c16360a2fea060d615a32b45270f8a8fc`.
- File: `Qwen3.5-2B-Q4_K_M.gguf`.
- Download: https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf
- Size: `1280835840` bytes.
- SHA-256: `aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223`.
- Local model path: `models/qwen3.5-2b/Qwen3.5-2B-Q4_K_M.gguf`.
- Runtime probes passed the greeting and `42` follow-up using the same local runtime. The GGUF reports Qwen3.5 architecture; its native chat template emits no BOS token and opens active reasoning with `<think>`.
- License: Apache-2.0; see [licenses/Qwen3.5-2B-LICENSE](licenses/Qwen3.5-2B-LICENSE), the primary official license copy.

### Full precision reasoning variant

- Repository and immutable revision: [unsloth/Qwen3.5-2B-GGUF](https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/tree/f6d5376be1edb4d416d56da11e5397a961aca8ae), `f6d5376be1edb4d416d56da11e5397a961aca8ae`.
- Base model: [Qwen/Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc), immutable revision `15852e8c16360a2fea060d615a32b45270f8a8fc`.
- File: `Qwen3.5-2B-BF16.gguf`.
- Download: https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-BF16.gguf
- Size: `3775709216` bytes.
- SHA-256: `dc11be2ca4519954a3e4b013ef59497b5522209987d67891851113eda89619f1`.
- Local model path: `models/qwen3.5-2b/Qwen3.5-2B-BF16.gguf`.
- License: Apache-2.0; see [licenses/Qwen3.5-2B-LICENSE](licenses/Qwen3.5-2B-LICENSE).
- Validation: SHA-256 and byte count match the pinned artifact. With the existing Metal runtime and native Qwen template, the application's two-turn arithmetic check passed. The native GUI greeting passed at temperature 0.2. The temperature-zero greeting exhausted 2048 output tokens without closing its thinking block; see [local verification](README.md#local-verification).

## Qwen3 0.6B full precision BF16

- Base model: [Qwen/Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B/tree/c1899de289a04d12100db370d81485cdf75e47ca), immutable revision `c1899de289a04d12100db370d81485cdf75e47ca`.
- Conversion repository: [unsloth/Qwen3-0.6B-GGUF](https://huggingface.co/unsloth/Qwen3-0.6B-GGUF/tree/50968a4468ef4233ed78cd7c3de230dd1d61a56b), immutable revision `50968a4468ef4233ed78cd7c3de230dd1d61a56b`.
- File: `Qwen3-0.6B-BF16.gguf`.
- Download: https://huggingface.co/unsloth/Qwen3-0.6B-GGUF/resolve/50968a4468ef4233ed78cd7c3de230dd1d61a56b/Qwen3-0.6B-BF16.gguf
- Size: `1198182848` bytes.
- SHA-256: `f9c9f1d3c1e21755b82d4e165f88dbbbd4355646d632fb5d6cef7c66ed4ee04e`.
- Local model path: `models/qwen3-0.6b/Qwen3-0.6B-BF16.gguf`.
- Local metadata: GGUF v3; architecture `qwen3`; 28 layers; embedding length 1024; 310 tensors; 596,049,920 parameters calculated from tensor dimensions. All tensor types are floating point: 197 BF16 tensors and 113 F32 tensors. The download passed exact byte-count and SHA-256 verification.
- Tokenizer: pretokenizer `qwen2`; `add_bos_token=false`; EOS ID 151645 (`<|im_end|>`); no BOS ID declared in the GGUF metadata.
- The embedded context metadata is 40960. The official model card states 32768, so the app caps choices at 32768 and defaults to 8192.
- The embedded native template is 4905 bytes (SHA-256 `5da44855ab7e0641774d5ed0ff0b4f89671d01df7d316e54d06557a962211cb4`). It differs from the pinned base tokenizer's 4168-byte template (SHA-256 `a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8`); the runtime uses the GGUF's native Jinja template. The official Qwen3 thinking template leaves the assistant role open and the model emits its own thinking opener; direct mode adds the closed empty thinking prefix. Completed history supplies final answers only.
- Sampling: thinking uses temperature 0.6, top p 0.95, top k 20, min p 0; direct mode uses temperature 0.7, top p 0.8, top k 20, min p 0, following the pinned official card. Presence penalty 0 and repetition penalty 1 are the app's neutral defaults.
- License: Apache-2.0; retained verbatim from the pinned official base in [licenses/Qwen3-0.6B-LICENSE](licenses/Qwen3-0.6B-LICENSE), SHA-256 `832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e`.
- Functional results are recorded in [local verification](README.md#local-verification).

## Falcon3 1B Instruct Q4_K_M

- Publisher: Technology Innovation Institute (TII).
- Base model: [tiiuae/Falcon3-1B-Instruct](https://huggingface.co/tiiuae/Falcon3-1B-Instruct/tree/28ba2251970a01dd1edc7ba7dad2eb71216ccfdf), immutable revision `28ba2251970a01dd1edc7ba7dad2eb71216ccfdf`.
- Official GGUF repository: [tiiuae/Falcon3-1B-Instruct-GGUF](https://huggingface.co/tiiuae/Falcon3-1B-Instruct-GGUF/tree/fc404df7cfbd32eaefbcbeb2079aba684e894d39), immutable revision `fc404df7cfbd32eaefbcbeb2079aba684e894d39`.
- File: `Falcon3-1B-Instruct-q4_k_m.gguf`.
- Download: https://huggingface.co/tiiuae/Falcon3-1B-Instruct-GGUF/resolve/fc404df7cfbd32eaefbcbeb2079aba684e894d39/Falcon3-1B-Instruct-q4_k_m.gguf
- Size: `1057044608` bytes.
- SHA-256: `54a4d303f5fb238db640433a61a600e308bfbb40cca2d7b451092e247df46616`.
- Local model path: `models/falcon3-1b/Falcon3-1B-Instruct-q4_k_m.gguf`.
- Local metadata: GGUF v3; architecture `llama`; 165 tensors; context length 8192; tokenizer `gpt2`, pretokenizer `falcon3`; EOS ID 11 (`<|endoftext|>`). No BOS ID is declared in the GGUF metadata.
- The embedded native template uses `<|system|>`, `<|user|>`, and `<|assistant|>` role tokens. This direct-answer profile sends all ordered messages and preserves plain assistant history; it adds no reasoning prefix or budget.
- License: the pinned model card identifies TII Falcon-LLM License 2.0 and points to the [official terms](https://falconllm.tii.ae/falcon-terms-and-conditions.html). The December 2024 section is retained in [licenses/Falcon-LLM-LICENSE.md](licenses/Falcon-LLM-LICENSE.md). This is a Falcon license, distinct from the Apache licenses of the other playground models.

## Falcon-H1 Tiny 90M Instruct Q4_K_M

- Publisher: Technology Innovation Institute (TII).
- Base model: [tiiuae/Falcon-H1-Tiny-90M-Instruct](https://huggingface.co/tiiuae/Falcon-H1-Tiny-90M-Instruct/tree/e6389502a0b12cd8da894b395ba5bf7436873b16), immutable revision `e6389502a0b12cd8da894b395ba5bf7436873b16`.
- Official GGUF repository: [tiiuae/Falcon-H1-Tiny-90M-Instruct-GGUF](https://huggingface.co/tiiuae/Falcon-H1-Tiny-90M-Instruct-GGUF/tree/578d134817c19bc48991ccff9a14709d664261cc), immutable revision `578d134817c19bc48991ccff9a14709d664261cc`.
- File: `Falcon-H1-Tiny-90M-Instruct-Q4_K_M.gguf`.
- Download: https://huggingface.co/tiiuae/Falcon-H1-Tiny-90M-Instruct-GGUF/resolve/578d134817c19bc48991ccff9a14709d664261cc/Falcon-H1-Tiny-90M-Instruct-Q4_K_M.gguf
- Size: `58600064` bytes.
- SHA-256: `e7d655345a876ecb073ea86f5ce68e412b99090c34a6bde8a2982bc48afb2612`.
- Local model path: `models/falcon-h1-tiny-90m/Falcon-H1-Tiny-90M-Instruct-Q4_K_M.gguf`.
- Local metadata: GGUF v3; architecture `falcon-h1` (hybrid Transformer/Mamba); 386 tensors; context length 262144; tokenizer `gpt2`, pretokenizer `falcon-h1`; BOS ID 17; EOS ID 11. The app defaults to 4096 context, with choices through 32768.
- The 2707-byte embedded ChatML template is byte-identical to the base model's pinned `chat_template.jinja` (SHA-256 `48bcdc7b00244baa76ae8fbc6fb0f2e39253809fbeaa482579200f7588de53a0`). It has no thinking prefill. This profile preserves plain assistant history and uses native formatting with reasoning disabled.
- License: the pinned model card identifies Falcon-LLM License and points to the same [official terms](https://falconllm.tii.ae/falcon-terms-and-conditions.html). The model license section is retained in [licenses/Falcon-LLM-LICENSE.md](licenses/Falcon-LLM-LICENSE.md).

The playground's Falcon integration is built using Falcon LLM technology from the Technology Innovation Institute. Both downloads were verified locally against the exact published byte counts and SHA-256 hashes. Neither model's pinned generation configuration prescribes numeric sampling values; temperature 0.2, top k 40, top p 0.95, min p 0, and penalties 0/1 are playground defaults. Functional results are recorded in [local verification](README.md#local-verification).
