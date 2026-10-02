# JevK5 Menu Bar

- Build a standalone Apple silicon macOS app. Keep it independent of Brekko.
- Read `docs/DESIGN.md` before changing component contracts.
- Use `uv` for Python dependency management. Keep dependencies locked.
- Never log evidence, prompts, responses, options, tokens, API keys, or exception dumps.
- Bind local services to 127.0.0.1 and authenticate every route; preserve Host/Origin checks.
- Serialize complete decisions against the single-slot model. Cache only identical consecutive token sequences.
- Model/runtime files and generated bundles are ignored. Keep provenance and licenses in Git.
- Write documentation in Markdown. Do not use emojis or shell heredocs.
- Use Conventional Commits and `git ai-commit <agent> "<message>"`, with the actual lowercase agent name (for example, `codex` or `claude`) in the `AI-Assisted` trailer.
- Set the Git author to the agent that performed the work: `Codex <codex@agents.invalid>` or `Claude <claude@agents.invalid>`. For another agent, use its actual display name and a stable lowercase identifier at `agents.invalid`.
- Pass the author per commit, for example: `git ai-commit codex "docs: clarify agent attribution" --author="Codex <codex@agents.invalid>"`. Preserve the configured committer identity; do not change global or repository Git identity for agent attribution.
- The reserved `.invalid` addresses are attribution labels without a GitHub account link. Never use `noreply@github.com` or an unrelated account's email for agent authorship.
- Do not create a remote or PR without a user-selected destination. Commit all intended source changes locally.
