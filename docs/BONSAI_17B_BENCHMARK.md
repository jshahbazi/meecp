# Bonsai 1.7B local speed benchmark

Measured on October 3, 2026, on this MacBook Pro. Bonsai generated **301 tokens/s with a 2,048-token prefix** using Metal. A combined 2,048-token prompt and 128-token generation averaged **0.866 seconds** of model execution.

These results establish local execution speed. Onboarding accuracy, tool use, and installation correctness were not evaluated.

The subsequent [onboarding prompt evaluation](BONSAI_ONBOARDING_EVALUATION.md) measures bounded source discovery, preparation decisions, and adapter generation on the same model and machine.

## Machine and runtime

| Item | Value |
| --- | --- |
| Machine | Apple silicon MacBook Pro, Mac17,7 |
| Chip | Apple M5 Max, 18 CPU cores |
| Memory | 128 GB |
| OS | macOS 26.7.1, build 25G241 |
| Runtime | Installed Homebrew llama.cpp 0.5.0, build 11146 |
| Runtime commit | `7fe450e19` |
| Backend | Metal, all model layers offloaded |
| CPU threads | 6 for the reported comparison |
| Attention | Flash attention enabled |
| KV cache | FP16 |
| Batch / microbatch | 2,048 / 512 |
| Repetitions | 5 timed repetitions per throughput case, default warmup enabled |

The existing installed mcpme JevK5 service remained running. Its process was idle during the checks. No other inference workload was intentionally run alongside the reported Metal tests. This was an ordinary desktop session, with no power or thermal controls imposed.

## Model provenance and license

- Publisher: Prism ML.
- Repository: [prism-ml/Ternary-Bonsai-1.7B-gguf](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf).
- Pinned repository revision: `983b5dec2ff16aab79990711ba0f828a499a7e6a`.
- Artifact: `Ternary-Bonsai-1.7B-Q2_0_g64.gguf`.
- [Pinned download](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/resolve/983b5dec2ff16aab79990711ba0f828a499a7e6a/Ternary-Bonsai-1.7B-Q2_0_g64.gguf).
- File size: **490,163,968 bytes**, or **490.16 MB / 467.46 MiB**.
- Benchmark tensor payload: 484,218,688 bytes. The [benchmark's `model_size` field](https://github.com/ggml-org/llama.cpp/blob/7fe450e19/tools/llama-bench/llama-bench.cpp#L1439) counts tensor bytes through `llama_model_size`, excluding GGUF metadata, tokenizer, and container overhead. It describes the same downloaded artifact and is not peak RAM.
- Verified SHA256: `6d0ecb3d9055969b5cde332b6fdb60e67ed3599e9f73e56b977731e1467e5c91`.
- Runtime parameter count: 1,720,028,160.
- Format: ternary weights packed in mainline-compatible `Q2_0`, group size 64.
- License: [Apache License 2.0, pinned publisher copy](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/blob/983b5dec2ff16aab79990711ba0f828a499a7e6a/LICENSE).
- Attribution: [publisher NOTICE, pinned copy](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/blob/983b5dec2ff16aab79990711ba0f828a499a7e6a/NOTICE.txt).

The model, LICENSE, and NOTICE were downloaded to the ignored local directory `models/bonsai-1.7b/` in the original checkout. The existing runtime was reused; no new inference runtime or Python dependencies were installed. The publisher's [format guide](https://github.com/PrismML-Eng/Bonsai-demo/blob/main/MODEL-FORMATS.md) distinguishes this artifact from older fork-specific group-128 files.

## Throughput and latency

All figures below are the reported arithmetic means over five warmed repetitions. Prompt processing and generation are separate cases unless explicitly marked combined.

| Workload | Metal | CPU, 6 threads |
| --- | ---: | ---: |
| Process 512 prompt tokens | 12,606 tokens/s; 40.6 ms | 269 tokens/s; 1.91 s |
| Process 2,048 prompt tokens | 10,537 tokens/s; 194.4 ms | 158 tokens/s; 12.97 s |
| Generate 128 tokens, empty prefix | 361 tokens/s; 354.8 ms | 76.6 tokens/s; 1.67 s |
| Generate 128 tokens after 512-token prefix | 342 tokens/s; 374.7 ms | Not measured |
| Generate 128 tokens after 2,048-token prefix | 301 tokens/s; 427.2 ms | Not measured |

Combined model execution was measured directly, rather than estimated by adding the separate cases:

| Workload | Mean | Median | Observed range |
| --- | ---: | ---: | ---: |
| 512 prompt + 128 generated tokens | 0.519 s | 0.542 s | 0.406–0.595 s |
| 2,048 prompt + 128 generated tokens | 0.866 s | 0.839 s | 0.829–0.984 s |

The [llama-bench methodology](https://github.com/ggml-org/llama.cpp/blob/7fe450e19/tools/llama-bench/README.md) uses synthetic token inputs and excludes tokenization, sampling, application orchestration, and model loading. The depth tests prefill before timing decode. Their saved prefix-state buffers make their process-memory use unsuitable as a serving-memory estimate.

The combined tests include both prefill and decode. Their aggregate tokens/s is not a generation-speed measure. Variation between runs means the separate throughput figures should not be added to predict an exact request latency.

A separate fresh-process CLI smoke test used the embedded chat template, reasoning disabled, and a 4,096-token context. It passed a simple arithmetic expected-answer check and exited normally in **0.543 seconds**, including startup. Filesystem caches were warm. Only the pass flag, timings, and memory counters were retained; prompt and generated text were not logged. This is a basic runtime check, not an onboarding quality evaluation.

## Memory

macOS `/usr/bin/time -l` measured both counters. Sizes below use decimal MB.

| Process test | Peak RSS | Peak physical footprint |
| --- | ---: | ---: |
| Combined Metal benchmark, up to 2,048 prompt + 128 output | 903.36 MB | 507.55 MB |
| CLI arithmetic smoke test, 4,096-token context capacity | 1,101.56 MB | 677.45 MB |

The combined benchmark requests 2,176 context tokens, which this runtime pads to 2,304. The 4K smoke test allocates a 4K context but generates only a short answer; it does not establish memory use for a full 4K onboarding workload.

RSS includes resident file mappings. Physical footprint uses Apple's memory-pressure accounting, which treats clean model mappings and shared IOKit pages differently. The peaks can occur at different times. These counters overlap and must not be added together, nor added to Metal allocation diagnostics on unified memory. See [Apple's physical-footprint ledger definition](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/task.c).

Neither number is a measurement of machine-wide incremental RAM. An unqualified claim of “under 1 GB RAM” is not supported by the 4K test. For a small onboarding helper, a context near 2K and unloading after use are sensible initial settings to evaluate.

## Reproduction

Run from the original checkout, where the ignored model download is stored. No server is started.

```sh
BONSAI_MODEL=models/bonsai-1.7b/Ternary-Bonsai-1.7B-Q2_0_g64.gguf

# Separate prompt processing and empty-prefix generation.
/usr/bin/time -l /opt/homebrew/bin/llama-bench \
  -m "$BONSAI_MODEL" -p 512,2048 -n 128 \
  -ngl 99 -fa on -t 6 -r 5 -o json

# Generation with an existing prefix; prefill is outside the decode timer.
/opt/homebrew/bin/llama-bench \
  -m "$BONSAI_MODEL" -p 0 -n 128 -d 512,2048 \
  -ngl 99 -fa on -t 6 -r 5 -o json

# Combined prompt processing and generation, including process memory counters.
/usr/bin/time -l /opt/homebrew/bin/llama-bench \
  -m "$BONSAI_MODEL" -p 0 -n 0 -pg 512,128 -pg 2048,128 \
  -ngl 99 -fa on -t 6 -r 5 -o json

# CPU comparison, with layer, KV, and auxiliary-operation offload disabled.
/usr/bin/time -l /opt/homebrew/bin/llama-bench \
  -m "$BONSAI_MODEL" -p 512,2048 -n 128 \
  -ngl 0 -nkvo 1 -nopo 1 -fa on -t 6 -r 5 -o json
```

Local numeric artifacts are in `.build/bonsai-benchmark-20261003/` in the original checkout: `metal-speed.json`, `metal-context-speed.json`, `metal-combined-speed.json`, `cpu-speed.json`, `generation-smoke.json`, and consolidated `measurements.json`. Runtime diagnostic files contain the `/usr/bin/time -l` counters. Model files and generated artifacts are ignored by Git.

The reported CPU comparison uses the six-thread rows. The exploratory twelve-thread rows are not used in the comparison because an interrupted additional Metal launch occurred during that portion of the run. All reported speed and memory figures were independently checked by another agent. Benchmark processes exited; the downloaded model remains available for later evaluation.
