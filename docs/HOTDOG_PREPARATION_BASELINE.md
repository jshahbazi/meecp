# Hotdog preparation baseline

Recorded 2026-10-02 from the separate external preparation workspace. This is a local validation record used to define the automatic-preparation milestone; it is not a CI or general accuracy result.

| Binding | Recorded value |
| --- | --- |
| Source baseline | `1d723e5af50639aa40702091c3e39e47dc3020dd` includes the ordinary hotdog example |
| Import | `a90d7f04-6d37-4a84-9180-150c970cd256`, revision 2 |
| Selected snapshot identity | `sha256:d8123c391f8f01eae50d696e0a16e5f1875fdfd0d1e19f63a45693c3e6443ed0` |
| Prepared package | `local.hotdog-or-not-hotdog`, version `1.0.0`, `python-function-v1`, CPU `mcp-stdio` |
| Final package digest | `f84ed101d048f29d917fc633fb1f7dff2db6c733f57e8fb391fb610c69aa8f8e` |
| ONNX SHA-256 | `7ba932db5d6c0c75ed957ebc0b58adcc2ddfc59b3388ec35e4ac6eaf7d1a6d33` |
| Source snapshot | 14 files verified against exported sizes and SHA-256 |
| Package closure | 4,432 locked artifacts, 199,370,354 artifact bytes |
| Host attachment return | `ready_to_plan`, revision 2, no blockers, expected digest, no installation operation/slot in that receipt |

The source snapshot omitted model/runtime artifacts under the source-intake policy. Preparation copied an existing local ONNX export only after matching the imported model-provenance hash and size. The source files stayed unchanged. The private runtime used CPython 3.12.13, MCP SDK 1.30.0 and locked hash-checked wheels. All 35 installed distributions retained license notices; the omitted FlatBuffers/HTTPX wheel notices were supplemented from their versioned upstream sources.

## Measured validation

All 19 source tests passed in 4.37 seconds. The 31 package checks were:

1. Snapshot access denial probe.
2. Sandbox availability.
3. MCP protocol 2025-11-25.
4. Tools-only capability advertisement.
5. Full tool contract equality.
6. Real inference on generated blank PNG.
7. Expected negative verdict for blank PNG.
8. Correct blank-PNG dimensions.
9. Real inference on generated wide PNG.
10. Expected negative verdict for wide PNG.
11. Correct wide-PNG dimensions.
12. Real inference on generated blank GIF.
13. Expected negative verdict for blank GIF.
14. Correct blank-GIF dimensions.
15. Confidence boundary 0.05.
16. Confidence boundary 0.95.
17. Schema rejection for missing arguments.
18. Schema rejection for relative image path.
19. Schema rejection for boolean threshold.
20. Schema rejection for low threshold.
21. Schema rejection for high threshold.
22. Schema rejection for extra argument.
23. Sanitized missing-file failure.
24. Sanitized corrupt-image failure.
25. Sanitized directory-input failure.
26. Sanitized denied-source failure.
27. Three concurrent client calls completed through serialized inference.
28. MCP ping.
29. Controlled positive tensor NMS and box scaling.
30. Controlled positive tensor threshold filtering.
31. Controlled winning-class selection.

The real inference inputs were generated white images: PNG 32×24, PNG 1024×1 and GIF 8×6. Client call durations were 504.7 ms cold and 19.8/18.0 ms warm. Peak child RSS was 277.9 MiB; the package declared 512 MiB and one session. These observations cover small synthetic images. Maximum accepted 12 MiB/24-million-pixel input behavior was not measured in this run and is an automatic-release gate.

The relocated package launched through its private entrypoint from an unrelated directory with deliberately unusable global Python settings. The service's validation policy denied the original source, checkout, authoring/dependency environments, uv-managed Python, Homebrew, network operations and package writes. This was a validation policy; it does not establish an installed per-slot OS sandbox.

The package was sealed and statically validated after the runtime checks. The host independently validated and copied that exact final digest during attachment. Future automatic preparation must verify its final sealed candidate and mandatory before/after digest equality as specified in the [implementation plan](AUTOMATIC_PREPARATION.md).

## Claim boundaries and evidence

Positive model recognition and generalization accuracy were not evaluated. Controlled positive tensors validate detector postprocessing; they are not a positive-model fixture. The user subsequently reported that the import worked. That report does not extend the recorded attachment receipt into an independently measured installation/runtime result.

The local source records for this summary were the package's `validation-summary.json`, `VALIDATION.md`, `preparation.json`, model/runtime/provenance receipts, final static-validation result, and the host's `attachment.json`. Only bounded metadata and synthetic check descriptions are recorded here. Models, runtime trees, image fixtures, credential values and raw protocol/build transcripts are excluded from Git.
