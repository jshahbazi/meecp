# Text generation HTTP cancellation checkpoint

## Observed failure

The installed native text generation run against clean source
`d968b84aa702c554aa396aed1349304dbe1bab85` completed capture, generated forms,
preparation, same-revision attachment, installation, and successful HTTP and
stdio calls. Its explicit HTTP cancellation check failed: the pinned SDK
received a successful `CallToolResult` with `isError=false` after sending the
request's cancellation notification. Successful generation does not satisfy
the cancellation requirement.

The host's build receipt SHA-256 is
`ad2276421cdacc7b88a0d010d926480e101fc4122760589aaa4867ee1424abc1`.
All 4,739 manifest files and all 32 private text generation engine files were
independently checked before the run. The matching reviewed native binary
SHA-256 is
`73d24f21cdaefdd9a05dd2436305027ed6de1d32396b887595d399b852c24c72`.
Failed receipts and isolated state remain retained. Every harness-owned child
was joined; the final host ledger had no owners.

## Repair

The service now uses bounded stateful MCP HTTP sessions: at most eight sessions
with a 300-second idle timeout. A cancellation notification is a separate HTTP
POST. SDK 1.30.0 creates a fresh server session for each stateless POST, so that
notification could not find the earlier request's cancellation scope. Stateful
sessions retain that scope across the two authenticated POSTs.

The engine remains shared. The existing inference queue retains ownership until
its thread joins after cancellation. Authentication, Host and Origin checks,
the request body limit, and tool contracts are preserved. Session deletion
cancels the session's active request and admits a later session after cleanup.

## Verification

`tests/test_textgen_service_cancellation.py` uses actual TCP loopback HTTP,
Uvicorn, the production service factory and bounded generator, and the locked
MCP SDK 1.30.0. Its small controlled transport holds an actual inference thread
until the test releases it. It does not load a model or measure generation
quality.

The cancellation test failed against the original stateless service in 1.39
seconds, with cleanup joined. All four tests passed after the repair:

- Cancellation produces the actual SDK error while inference remains active,
  pending, and unsettled; thread release then settles ownership.
- DELETE cancels an active request without starting another engine.
- Unauthorized requests, invalid Host or Origin, and deleted session IDs retain
  their route denials.
- Eight sessions are admitted; the ninth is denied; normal SDK DELETE cleanup
  reclaims admission.

The focused generation, service, cancellation, and HTTP gateway packet passed
57 tests in 6.50 seconds. A separate reviewer reran all four HTTP tests and
tested 16 simultaneous initializes: eight admitted and eight denied with 503.
The reviewer also accelerated only the test's idle timer to 50 milliseconds:
an active inference held for 150 milliseconds retained its session and
ownership, then settled and expired after release. Both checks retained one
engine start and joined their controlled workers.

The independent mode-0600, content-free receipt SHA-256 is
`d08cbcab90b5b9e8a20000cc299aed3572479f98c5077c24950169ff18725468`.

Reviewed source SHA-256 values:

| File | SHA-256 |
| --- | --- |
| `backend/mcpme_textgen/service.py` | `b2681c6b799cc93a8a59f1a3c768e70a4dfb9ee5bb34fcf8b0a12ca3639c8101` |
| `tests/test_textgen_service_cancellation.py` | `fac4fd6d2d57a87b895296805ca1e70b84fa82b66e1807db3fbc3edaab19da1a` |
| SDK HTTP session manager | `d602d0b8e8c57138170e14f3c9901fc3feef5aa34457fd4a6ee3a114e6096f88` |
| SDK shared session | `473c151ed51ec617466e63b525c42a0f31db997c2812be78477deff40e3e9ba0` |

## Remaining evidence

This checkpoint repairs and verifies the HTTP transport failure with controlled
inference ownership. A new coherent text generation family version and clean
complete host must include the repair before the full captured and folder
native acceptance is rerun. The failed frozen host remains unchanged.

The complete A4 native installed gate, aggregate hard resource enforcement,
filesystem and network isolation, signed distribution, and final Phase 3
adversarial review remain open. These tests make no model accuracy claim.
