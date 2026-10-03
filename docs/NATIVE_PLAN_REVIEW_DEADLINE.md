# Native preparation review deadline

Native preparation review calls `POST /v1/preparations/plan-form`. This route
validates the selected source and the complete private preparation toolchain
before returning the reviewed execution plan. It now receives the existing
300-second preparation request budget. GET requests and status polls retain
their 30-second budget.

An actual native captured-GGUF run against the sealed host from
`2f1a448eaf2d2918380a649cbbc41671a748613f` stopped at preparation review with the
original 30-second deadline. A fresh owned host over the retained draft measured
38.185 seconds for that exact route; it returned an available plan with no
blockers. Form generation took 0.066 seconds. The selected model was
491,400,032 bytes, SHA-256
`74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`.

With the route deadline changed, the actual native flow completed capture and
review at revision 2. Native binary SHA-256:
`73d24f21cdaefdd9a05dd2436305027ed6de1d32396b887595d399b852c24c72`.
The retained bounded acceptance receipt SHA-256 is
`1902359823c16c12da219d606582954a054e1a3daa77a5441a740fbe0c2c0f07`.
All owned children joined. Swift validation passed 18 tests with 3 skips, and
the dashboard, management, capture, preparation and native self-tests passed.

The subsequent build stopped with `textgen_private_engine_required` before any
command launched. Independent inspection found that this CLI checkpoint host
contained none of the 32 required textgen runtime artifacts. That packaging
prerequisite requires a fresh sealed host with its reviewed engine closure.
This deadline checkpoint establishes successful native plan review; installed
native text generation and the remaining Phase 3 gates require separate evidence.
