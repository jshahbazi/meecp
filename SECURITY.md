# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities through [GitHub private vulnerability reporting](https://github.com/jshahbazi/mcpme/security/advisories/new). The repository maintainer receives these reports privately.

Include the affected commit or release, a description of the security impact, and minimal steps to reproduce the issue. Use synthetic evidence and redact credentials, tokens, private project paths, and personal data. Do not attach private signing keys, certificates with private keys, or complete application state directories.

Keep vulnerability details out of public issues and pull requests while the report is being assessed. The maintainer will coordinate validation, remediation, and disclosure through the private report. This project does not promise a fixed response time.

## Supported versions

Security fixes are developed on `main`. Check that a report still reproduces on the latest relevant commit when practical. Older development builds do not have a guaranteed backport policy.

## Covered surfaces

Reports may concern the macOS app, local MCP services and authentication, package installation and execution, dependency handling, GitHub build and release automation, and distributed installers. A successful build or enabled GitHub scanner does not establish that these surfaces are free of vulnerabilities.
