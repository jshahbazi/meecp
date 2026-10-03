# Codex CLI preparation backend feasibility

Checked 2026-10-03 against installed `codex-cli 0.159.0-alpha.12.1`. This bounded investigation did not establish a Codex transport that satisfies `analysis-request-v1`. The agent provider enum remains unchanged and A5 remains open. No model generation was launched, no selected repository source was transferred, and no authentication values were read or copied.

## Local evidence

The binary is the signed app's embedded CLI at `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`, SHA-256 `1180e2d56ea06ec583092acd933345685da3441cb1769a436d76dbf320613e75`.

| Read-only command | Observed capability |
| --- | --- |
| `codex --version` | Exact version above. |
| `codex login status` | Logged in using ChatGPT. No secret values were returned. |
| `codex exec --help` | Supports `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `--json`, `--output-schema`, stdin prompts and `--sandbox read-only`. No output-token limit option. |
| `codex features list` | `token_budget` and `rollout_budget` are under development and disabled. Shell, unified exec, apps, plugins and other tools are separately enabled by default. |
| `codex debug models --bundled` | Bundled models include freeform patch tools; the catalog contains no output-token-limit field. Only selected metadata field names/types were inspected. |
| `codex app-server generate-json-schema --experimental --out TEMP` | Locally generated `ThreadStartParams` includes `ephemeral` and `dynamicTools`; `TurnStartParams` includes `outputSchema` and permissions but no token ceiling. `ThreadGoalSetParams` contains `tokenBudget`. Temporary generated schemas were removed. |

The official [non-interactive guide](https://learn.chatgpt.com/docs/non-interactive-mode) confirms structured JSON execution, saved CLI authentication reuse, and suppression of session rollout files through `--ephemeral`. Those are useful building blocks for a future adapter.

## Acceptance gaps

**Token enforcement is unproved.** The [configuration schema](https://learn.chatgpt.com/docs/config-schema.json) has experimental weighted `rollout_budget.limit_tokens`, while `token_budget` config concerns compaction reminders. It supplies no guarantee here that generation stops at the preparation attempt's output-token reservation. Goal budgets are a separate app-server feature. The documented `tool_output_token_limit` bounds stored tool context, not provider generation. A deadline or rejecting a large completed result cannot substitute for a proved generation/spend bound.

**Tool and transfer isolation are unproved.** Read-only sandboxing does not establish that the model cannot read unrelated files. Empty app-server `dynamicTools` does not, by itself, remove built-in tools. The selected source map in the existing HTTP backend is stronger than assuming that a CLI launched in an empty directory will only see those bytes. A viable CLI adapter needs an authoritative tool manifest and enforced denials for every unselected read, shell/process action, provider tool and injected local capability. Prompt instructions to avoid tools cannot grant that guarantee.

**Log behavior still needs a dedicated proof.** `--ephemeral` covers rollout persistence. The [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) separately documents history persistence, log directories, project instructions and tool settings. The tested CLI supports ignoring user config, but this investigation did not verify that all startup context, diagnostic storage, inherited features and error paths exclude prompts and responses. No generated prompt was used to experiment with this unresolved boundary.

**Owned process recovery is not implemented for this transport.** The HTTP adapter's local cancellation contract cannot be reused as evidence for a child CLI process. A CLI implementation would need the existing native owner/reaper journal, bounded stdout/stderr parsing in memory, cancellation joins, restart settlement and retained unknown provider-usage reservations. These mechanisms should be added only after the underlying invocation contract is established.

## Decision and next evidence

Keep the concrete HTTP Responses backend as the implemented provider and retain its explicit credential blocker. Do not advertise an integrated Codex option from this checkpoint. This is a finding about the verified invocation contract, not a claim that Codex can never support the route.

A further spike needs documented or implementation-grounded answers for the hard generation bound, complete empty-tool configuration, and all-content logging suppression on this exact CLI build. Then an independently reviewed invocation can run generated synthetic bytes inside a scoped filesystem boundary, prove attempted forbidden reads/tool calls fail, and exercise parent death, cancellation and late output before host integration. No new user API key was requested during this investigation.
