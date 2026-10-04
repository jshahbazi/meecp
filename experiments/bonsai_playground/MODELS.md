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
