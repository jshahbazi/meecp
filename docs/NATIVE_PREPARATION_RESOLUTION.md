# Native dependency and tool resolution

The preparation workspace accepts host-generated resolution requirements before package preparation. A supported Python source can require dependency resolution, and a foreground Python MCP source can also require tool discovery. The native client displays the host's selected candidate, operations, permitted registry hosts, deadline, disk budget and memory budget. It requests a static resolution plan and submits its exact ID and digest with one stable retry key when the user chooses **Resolve Reviewed Inputs**.

The workspace polls durable resolution jobs and displays the host's measured command, wheel, byte, file and tool counters. Closing the window or losing the connection preserves host work. **Cancel Resolution** requests joined cancellation; an interrupted job with retained ownership or resources exposes **Recover Resolution**. Source identity and import ID determine association, so metadata revision changes retain the owned job while a different source clears it.

After a successful, settled resolution, the workspace asks the host for a fresh generated form with the exact result digest. The same digest accompanies the form answers when package preparation is reviewed. The host must accept the result for the selected revision and explicitly carry that reference when adopting a new metadata revision. The UI cannot provide a dependency lock, descriptor, execution policy or tool schema.

Public resolution and preparation availability remain host-controlled. Native self-tests verify UI contracts and stale-response handling; they do not establish the integrated release gates in [AUTOMATIC_PREPARATION.md](AUTOMATIC_PREPARATION.md).

The owner CLI exposes the same durable jobs through `mcpme resolutions plan`, `start`, `list`, `get`, `cancel` and `recover`. Planning takes an import ID, revision, exact inspected candidate and closed resolution policy file. Starting requires the reviewed plan ID, plan digest and stable UUID idempotency key. Recovery reconciles owned work and does not replay execution. Existing preparation grants cannot access resolution routes. Ordinary CLI and native launches keep resolution and preparation execution unavailable until the installed acceptance gates pass.

## Native acceptance driver

The explicit test command drives the real preparation view and the production authenticated Unix-socket request and parent draft-adoption callbacks against an already launched host:

```sh
.build/debug/McpmeMenuBar --preparation-native-acceptance /absolute/path/to/case.json
```

The host must expose its normal owner-only `manager-connection.json` and manager socket. Test execution requires an intentionally enabled development host factory; this command grants no backend execution capability. Acceptance should use a relocated packaged host with fresh caches and independent package installation, runtime invocation and restart verification.

The bounded fixture object has these fields:

| Field | Meaning |
| --- | --- |
| `state_dir` | Absolute path to the development host's private state directory. |
| `draft_id` | UUID of the imported source draft. |
| `output_path` | Absolute path for a new receipt file; an existing file is rejected. |
| `answers_by_label` | Object containing explicit synthetic answers for uniquely labeled generated questions. Answers must satisfy the host-supplied field type and constraints. |
| `candidate_id` | Optional exact digest selecting a detected candidate. |
| `recipe_id` | Optional ID selecting a compatible registered recipe version. |

The driver loads the saved draft, obtains a generated form, reviews and starts resolution if needed, waits for settled success, obtains the resolved form, applies only the supplied synthetic answers, reviews preparation through the parent's metadata revision adoption, starts the exact preparation plan, and waits for attachment. It writes a mode-0600 receipt with plan/job IDs, digests, source/revision association, measured counters and requested route names. It records no credentials, source text or test content. A failed receipt identifies the phase reached and any sanitized job error code.

Run the focused native contracts with `--preparation-ui-self-test`, the full management contracts with `--management-ui-self-test`, and transport/unit coverage with `swift test`. The native acceptance receipt confirms form-to-attachment behavior only; independent verification still needs to prove the installed package's complete runtime contract and the other required acceptance families.

The [installed native onboarding harness](NATIVE_INSTALLED_ONBOARDING_ACCEPTANCE.md)
extends that receipt check through real installation, complete HTTP/stdio SDK
contracts, source removal, host restart and independently scaffolded recipe
selection and lifecycle retention.
