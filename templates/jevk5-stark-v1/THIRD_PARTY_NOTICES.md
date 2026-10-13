# Experimental Stark JevK5 package notices

This local experimental package includes the converted JevK5 BF16 checkpoint,
the pinned Stark probability-scoring worker, the Meecp adapter, and a private
CPython runtime with the dependencies from Meecp's `uv.lock`. The package lock
records every shipped artifact. `preparation-receipt.json` records selected
input hashes and the generator sources; provenance contains no prompt, decision,
token, probability, credential, or raw build output.

| Component | Source and provenance | License |
| --- | --- | --- |
| JevK5 model | `alibiserikbay/JevK5` revision `c4f7fdb3aeab5582336406e78d3bef11bf98833d`; converted BF16 artifacts bound by `provenance/JEVK5_MODEL.json` | Apache-2.0; `licenses/JEVK5_MODEL_LICENSE` |
| JevK5 prompt and answer formatter | `allebee/jevk5` revision `f944fe37ff1d5ed3830aa4c8d88b7189c8c1268a`, unchanged vendored files | Apache-2.0; `vendor/jevk5/LICENSE` and `vendor/jevk5/NOTICE` |
| Stark worker and inherited Uzu sources | Exact Git revision, tree, dependencies and worker bytes in `engines/stark.lock.json` and `provenance/STARK_BUILD_RECEIPT.json`; included upstream attribution in `licenses/stark/` | MIT and retained component notices |
| Lalamo conversion | Revision `831accb8252a6b6606c378048caeb44178f1a050`; conversion identity in `provenance/JEVK5_MODEL.json` | MIT; `licenses/LALAMO_LICENSE` |
| Private CPython | CPython 3.12.13 from the selected bundled Meecp host; `python-runtime-origin.json` and preparation hashes bind the copied runtime | Python Software Foundation and component licenses under `python/licenses/` |
| Python server dependencies | Locked Meecp dependencies; exact copied files bound by `meecp.lock.json` | Component licenses retained in the copied wheel metadata under `python/site-packages/` |

The package includes no Lalamo or Uzu Python runtime dependency. Conversion is a
separate development action; the installed engine loads the existing converted
folder with the Rust worker. Meecp's native ownership reaper is copied unchanged
from the selected host artifact and remains bound to that host's build receipt.
