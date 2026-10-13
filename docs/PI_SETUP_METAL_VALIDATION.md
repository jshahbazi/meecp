# Pi native Metal probe validation

Recorded on 2026-10-13 on an Apple M5 Max running macOS 26.7.1. This receipt
covers the host-selected independent GPU probe policy. The complete ColGrep MCP
package has not passed independent localization fixtures under the installed
host; that remains a package acceptance requirement.

## Policy and native checks

The default Pi sandbox denied Metal device creation. The GPU probe policy adds
only `AGXDeviceUserClient`, `IOSurfaceRootUserClient`, and the
`com.apple.MTLCompilerService` lookup. Reads, writes, networking, owned process
groups, and credential handling retain their existing boundaries.

- A native compute program created a residency set, compiled a shader, and
  checked its numerical output. The same program failed device creation under
  the default policy. Protected file reads, package writes, and a live loopback
  connection remained denied under the GPU policy.
- The exact Stark generation worker from commit
  `9bc3ddb8e050c4d887aed13197a27bad2a4a185c` loaded the converted
  `lightonai/colgrep-agent-2B` checkpoint and generated four tokens with a
  256-token context. Its ordinary sandbox launch returned
  `metal_initialization_failed`. This checks native execution and readiness;
  it does not establish arbitrary localization quality or full-context behavior.
- The actual ColGrep executable and staged ONNX search model built a fresh
  index and searched a two-function synthetic Python repository. Ordinary
  indexing failed with `No Metal GPU device found`; the GPU policy completed
  indexing and search. No network permission was granted. The tested binary's
  explicit `--force-cpu` indexing and search also passed under the default
  sandbox, so the reported CPU-option failure was not reproduced on this fixture.
- Host tests check GPU selection after resource admission, ordinary-command
  denial, and cancellation retaining the reservation until owned processes are
  stopped and reaped. The scoped Pi tool/setup/general-setup suites passed
  140 tests. Six additional admission checks cover preparation, resolution, and
  capture starting during a held CPU or GPU probe: they reject work above the
  budget and admit it after the probe releases its charge.

## Artifact identities

These SHA-256 identities bind the runtime and model bytes used above. Native
responses, prompts, and token vectors are not recorded.

| Artifact | SHA-256 |
| --- | --- |
| Stark generation worker | `8a945373ee155998a274d9d4bb876f4389c0123c25d89aeb53d09680d2fe36ff` |
| Converted ColGrep agent weights | `86325c4d8191aa39275f68cd417dabbb51ead1d719be4e7eba1133644b06cd5b` |
| ColGrep executable | `a30ed7657cf2a93d4f67ca378b78a798efda1541ec80f76e0e9960f9a758075a` |
| ONNX Runtime 1.23.0 dylib | `d3859aecdb70ea099f5b5f4185fe16f0527c6680b18731e6e96fc971ec767cca` |
| Search model ONNX weights | `eac35bdaa862e2762e6455337f7a3e704b05dbc4259f00929fcc8e10207f11c7` |
| Search model tokenizer | `a388b94942e98e5c661c6c23f919842285738bfd123a0d148dea0c56287505d0` |

The corrected application must be installed and its host restarted before the
policy affects an existing installation. Pi's in-memory conversation does not
survive that restart; staged files and the draft are retained. Generated app
bundles and model/runtime files remain ignored.
