# Prepared-app Python signing

Developer ID builds give only `Contents/Resources/host/preparation-toolchain/python/bin/python3.12` the `com.apple.security.cs.disable-library-validation` entitlement. This private interpreter runs prepared Python apps with their exact locked upstream wheels. App preparation does not hold the developer's signing key.

The signed A1 acceptance run reached the real package probe and failed there. Four isolated import controls reproduced the boundary with Python 3.12.13: the signed interpreter loaded Developer ID signed SDK extensions, rejected upstream native extensions, and the development interpreter loaded both. The immutable diagnostic is `/private/tmp/mcpme-a1-signed-python-loader-diagnostic-v2.json`, SHA256 `dfd2342d66768e0e26cc73db981cc2a3fc0099739f2152d1ed9980b4dd9bd098`. Both input trees were preserved and every diagnostic child joined.

[Apple's entitlement documentation](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.disable-library-validation) describes this exception for processes loading third-party libraries. The interpreter retains hardened runtime and timestamp signing. The menu app, host SDK Python, helpers, libraries and frozen signed TextGen runtime receive no exception. The frozen TextGen profile carries its own same-team native closure.

Scope controls exercise the signing command applied to each interpreter and the app executable, as well as the excluded helper, alias, library and TextGen paths. Acceptance still requires a fresh signed app, the actual callable and existing MCP installation paths, strict bundle signatures and preserved source/runtime inventories.
