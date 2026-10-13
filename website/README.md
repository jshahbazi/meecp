# meecp website

The public product website for [meecp.app](https://meecp.app). The source is a static, dependency-free HTML/CSS/JavaScript site hosted through Sites. Branding derives from the approved assets in `../assets/brand/`.

## Build

From the repository root, with Node.js installed:

```sh
node website/build.mjs
```

The builder copies the checked-in page, styles, script, metadata, and optimized assets to ignored `website/dist/`. There are no external fonts, trackers, forms, or browser storage. The tool explorer uses accessible tabs; the FAQ uses native disclosure controls.

## Content

The page describes the current development project. The latest public release was verified on 2026-10-13 as `v1.0.0`, published with the former `mcpme` name. Keep that release distinction current before changing the download copy. Product capability claims should follow the current README, status documents, and scoped acceptance records.

The Apps window is a labeled illustration. It is implemented with HTML/CSS and does not contain live application data.

## Publishing

Preserve the project ID in `.openai/hosting.json`. Use the Sites source workflow in an isolated checkout under the root repository's ignored `.sites-runtime/`; copy this directory's source there without `dist/`. The source helper must operate at the root of its own Git repository. Supply short-lived credentials through its hidden stdin interface, never through files or shell arguments.

Build the isolated checkout with `node build.mjs`, commit with the repository's Conventional Commit and `git ai-commit codex` conventions, then push/package through the Sites source helper. Save that exact pushed commit and archive, and deploy the saved version. The Site audience is public. Keep credentials, deployment archives, preview files, and generated output out of Git.

Cloudflare manages the `meecp.app` DNS zone. Hosting and DNS identifiers are recorded in the parent repository's `docs/WEBSITE_DEPLOYMENT.md` after publication. Preserve unrelated records when updating the Site's routing or validation records.

The consultant-delivery reporting application and benchmark remain tracked in `../docs/OPEN_PLANS.md`.
