# meecp

- Build a standalone Apple silicon macOS app. Keep it independent of Brekko.
- Read `docs/DESIGN.md` before changing component contracts.
- Use `uv` for Python dependency management. Keep dependencies locked.
- Never log evidence, prompts, responses, options, tokens, API keys, or exception dumps.
- Bind local services to 127.0.0.1 and authenticate every route; preserve Host/Origin checks.
- Serialize complete decisions against the single-slot model. Cache only identical consecutive token sequences.
- Model/runtime files and generated bundles are ignored. Keep provenance and licenses in Git.
- Write documentation in Markdown. Do not use emojis or shell heredocs.
- Use Conventional Commits, `git ai-commit codex`, and author `Codex <noreply@github.com>`.
- Do not create a remote or PR without a user-selected destination. Commit all intended source changes locally.
