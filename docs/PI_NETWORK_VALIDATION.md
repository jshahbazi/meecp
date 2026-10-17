# Pi download and command trust validation

Recorded on 2026-10-13 on Apple silicon/macOS 26.7.1. This scope covers artifact
acquisition during setup. It does not qualify the resulting ColGrep MCP package
or its localization quality.

## Observed blockers and resulting behavior

The installed download transport rejected every HTTP 3xx response as
`download_redirect_blocked`. A normal GitHub release locator therefore stopped
before artifact transfer. Pi's `fetch_url` now follows up to five validated
public HTTPS redirects, including server-issued signed asset URLs. DNS scope,
numeric connection pinning, TLS hostname verification, body bounds, and
optional artifact SHA-256 checks apply throughout the transfer. The durable
pinned prepared-package downloader keeps its existing default redirect denial.

Git and curl could validate GitHub certificates outside the command sandbox
with the same private HOME and filtered environment, but failed inside it.
The native app does not normally inherit a developer shell's extra CA path.
The host now stages public certificates from an explicitly configured CA file
or Apple's default trusted-anchor API and supplies Git's CA setting alongside
the existing compatible-client settings. The command sandbox receives no
additional Keychain, private-key, or security-service permissions. Trust is
cached for that workspace.

## Native and public checks

- The actual Pi `fetch_url` implementation downloaded
  [ColGrep v1.8.5's Apple silicon archive](https://github.com/lightonai/next-plaid/releases/download/v1.8.5/colgrep-aarch64-apple-darwin.tar.xz):
  16,681,924 bytes, SHA-256
  `2cd013c820a6ba9d4066c58ce7e6b75ea614dd77a9a18a88836e005f1ef126bc`,
  matching the GitHub release asset digest. Default transport use still returned
  `download_redirect_blocked` for that URL.
- With no configured CA environment, the host exported 158 public anchors and
  staged 237,795 PEM bytes. No private-key material was exported. Under the
  existing sandbox, public Git ref discovery, a shallow filtered Stark clone,
  and curl HTTPS completed successfully.
- Controlled Git TLS checks rejected an untrusted certificate and the wrong
  hostname, and accepted the explicitly configured fixture CA. A configured
  certificate file took precedence over native export; private-key blocks were
  excluded. Export failure returned a fixed error and did not disable TLS
  verification.
- Redirect checks exercise distinct-host TLS/SNI, wrong-target certificates,
  signed queries, relative redirects, loops and hop limits, mixed/private DNS,
  malformed headers and framing, concurrent requests, bounded close waits, and
  cancellation cleanup. URLs containing userinfo, fragments, or encoded control
  characters remain rejected.

The native certificate API is
[SecTrustCopyAnchorCertificates](https://developer.apple.com/documentation/security/sectrustcopyanchorcertificates%28_%3A%29);
Git's setting is
[http.sslCAInfo / GIT_SSL_CAINFO](https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpsslCAInfo).
Only aggregate outcomes, public artifact identities, and source are recorded.
Signed redirect URLs, command output, credential values, and setup conversations
are not retained.
