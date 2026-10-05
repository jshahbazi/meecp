# Native recipe fixture canonical-path repair

The independently reviewed `fb1f237` driver rejected a valid Python-created private fixture before consuming its nonce. Foundation's `URL.resolvingSymlinksInPath()` normalized `/private/tmp` to `/tmp` on the tested Mac, while Python's `Path.resolve()` and POSIX `realpath()` selected `/private/tmp`. The compiled malformed-fixture negatives could therefore pass while every valid fixture failed.

This repair changes only native fixture path checks, the compiled driver controls, and this evidence document. A bounded POSIX `realpath()` helper now supplies canonical comparisons for the existing file and directory identity guards. The selftest canonicalizes the existing temporary parent before creating its private workspace. Nofollow opens, lstat/fstat identity equality, private ownership/modes, exact file metadata, nonce expiry and exclusive one-shot claim remain in force.

A new compiled positive admission control writes the fixture using Python-canonical paths and requires the exact private nonce claim to appear once. It then rejects a deliberately invalid synthetic manager connection. Replaying the same fixture must preserve the claim's inode and bytes. This control proves admission; it does not claim an owner request succeeded. The original twelve compiled controls remain unchanged, and independently authored actual owner-socket null-selection, explicit replacement and stale-manager controls must also pass against the new immutable binary.

The original `fb1f237` source, native binary `9253a779bb86b61b72138f1f1a4b033a51741782822e9bffca094f1864d3f344`, source receipts and failed real-socket controls remain preserved. The new private source/build/selftest packet records the repaired exact inputs and binary hash. No backend, catalog, recipe bundle, published component, public execution gate or installed user state changes in this repair.

Actual integrated A0R lifecycle, independent preparation/install/restart and whole Phase 3 completion remain separate gates.
