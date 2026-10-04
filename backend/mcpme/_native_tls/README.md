# Private native TLS trust

This is a private fork of [truststore 0.10.4](https://github.com/sethmlarson/truststore/tree/v0.10.4), licensed under the retained MIT [LICENSE](LICENSE). `upstream.json` records the exact official wheel URL, SHA-256, byte count, and original source hashes. The wheel was verified before extraction. It is source in the host's immutable artifact closure, rather than a runtime download.

The macOS trust adapter keeps the `CFErrorRef` returned by `SecTrustEvaluateWithError` under explicit ownership until all failure diagnostics finish, then releases it independently of the copied description.

The fork has three changes:

1. `_macos.py` calls `SecTrustSetNetworkFetchAllowed(trust, False)` before evaluation and checks the returned OSStatus. An inability to apply the restriction rejects the handshake.
2. `_api.py` removes the upstream global SSL injection and extraction helpers.
3. `__init__.py` exports only `SSLContext`.

Apple's [SecTrust implementation](https://github.com/apple-oss-distributions/Security/blob/main/OSX/sec/Security/SecTrust.c#L656-L680) applies the restriction to issuer and revocation fetching. These certificate requests otherwise bypass mcpme's vetted public socket and declared origin scope. Missing intermediates must be supplied by the peer or already available locally. Certificate validity and the original URL hostname remain verified by the platform trust evaluator.

Each transfer creates a new private context. The macOS host uses locally configured Keychain trust without inherited `SSL_CERT_FILE`, `SSL_CERT_DIR`, proxy configuration, or global SSL mutation. Non-macOS backends are retained from the exact upstream source for development checks; the application targets Apple silicon macOS.

The resolution producer identity includes every Python file in this fork. Host build receipts also hash the license, provenance, and documentation. Updates require reviewing the upstream source, preserving its license, recording its exact artifact identity, and rerunning real TLS and controlled missing-intermediate tests.
