# Full BF16 Sol retry

Use every criterion and ordering in [SOL_BF16_PROTOCOL.md](SOL_BF16_PROTOCOL.md) unchanged: same checkpoint, candidate/control binaries, native acceptance, 243 cases, twelve serial loads in two mirrored six-block cycles, 5832 measured responses, 36 checked warmups, temperature, context, cache policy, scoring helpers and independent calculations.

The first incomplete attempt stopped during block five after four completed blocks. One sampled background process exceeded the predeclared 200% CPU guard (265.1%); no recognized model/compiler/inference workload was detected. All owned workers were reaped. Its aggregate-only metadata cannot attribute the historical spike. Preserve `test-artifacts/jev-sol-bf16-full-20261011` and exclude it entirely from final timing.

The retry uses versioned harness, monitor and audit adapters so all bound sources from the incomplete attempt remain immutable. Monitoring retains the peak background process's PID and executable basename in private samples. Raw command lines and model data are never retained. Public host summaries contain only scalar counts, CPU/RSS aggregates and hashes. The same 200% CPU guard still stops the run. Final sampling or receipt failures now return a fixed failure code without an exception dump or a fabricated successful isolation receipt.

Release other chats between interrupted attempts. Coordinate the retry after their active immutable campaigns finish. Do not change benchmark selection or combine partial results with the retry. Rerun both complete cycles; rehash all sources/runtime/model artifacts and preserve independent scalar reproduction before publishing.
