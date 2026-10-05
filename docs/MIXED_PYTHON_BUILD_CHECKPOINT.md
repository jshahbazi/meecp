# Mixed Python application directory build checkpoint

The trusted Python builder explicitly selects a host-owned snapshot adapter for
validated `application_directory` contract version 2. Python source, binary
assets, and unknown regular payloads remain in the exact captured closure. The
adapter uses the existing bounded, held-descriptor reader before and after the
copy, and validates the destination's complete membership, hashes, and private
modes before returning. Cancellation checks run at entry, for each validated
file, for each copy, and during the final rechecks.

The published `templates/prepare.py` bytes and its version 1 source digest
semantics remain unchanged. Its SHA-256 is
`e4d78d4fca94ce5c388b1961c813484bbff849d364732b8abe88783436847f68`.
Published `resolved-python-v1` bundle bytes remain retained. Its recipe digest
is `64b4a9727453b41ebbecc5ed5ec94f8abe7a96d1bdbc7096e9c26553b2f3b49f`
and component version 1.0.0 digest is
`876ec0a299ff6b028ac8b747f72c610fc7f53c119cac4bf00de1e2405558f04f`.

The current resolved component and default recipe are separately minted as
version 1.0.1. The new bundle is in
`templates/builtin-recipes/resolved-python-v1-0-1`. The active default catalog
points to the new version. Saved old records remain unchanged; disabled,
deprecated, revoked, and removed decisions prevent implicit new-version
enablement. Current generated forms admit only the exact current component
reference. Independent recipe scaffolding binds that same reference.

The focused source, builder, version, form, default lifecycle, and native harness
packet passed 127 tests. Five additional acceptance inventory controls reject
unlisted files, changed bytes, links, and dirty source metadata before target
imports. Independent review found and reproduced a nested destination-directory
replacement gap; the repair adds full destination validation and actual
replacement, extra-file, byte, and mode mutation regressions.

`scripts/verify_phase3_mixed_python.py` drives a tiny ordinary callable directory
with a PNG and an unknown binary payload through real native resolution and
generated forms. It removes the original directory immediately after capture,
then requires build/probe, same-revision attachment, CLI installation, HTTP and
stdio SDK calls, restarts, and independent recipe lifecycle retention. It uses
only the selected host's exact current resolved family in its trusted private
factory. Production execution defaults stay disabled. It preserves the settled
prepared package and actual import/descriptor metadata for subsequent restricted
probe verification.

The actual clean-host native run passed in 63.312 seconds against source
`341de900be285addfd8934b066c5fd90f7bae25f`. All 4,759 payload files matched the
exact manifest in the source host and relocated copy before target imports.
The source host was independently rechecked after execution; the relocated
copy was cleaned up after owned shutdown without a final whole-host hash pass.
The retained prepared package was validated again after SDK calls and restarts.
The build receipt SHA-256 is
`80914e12fefee9f22750c1423673203019f5dbc149bbe61f79272055f370dd2f` and the
reviewed native binary stayed at
`73d24f21cdaefdd9a05dd2436305027ed6de1d32396b887595d399b852c24c72`.

The four-file source closure was captured as version 2 and the original was
deleted before resolution. Real native forms reviewed revision 2; preparation
finished five commands, acquired 29 wheels, checked one declared fixture, and
attached at that same revision. The actual verifier recorded 68,861,952 peak
memory bytes against the application's 128 MiB declaration. CLI installation
and three calls per SDK transport passed, including normal restart and
enabled/disabled independent recipe restart. Every harness-owned child joined.

The bounded mode-0600 actual receipt is retained at
`dist/mixed-python-native-341de90.json`, SHA-256
`b4130b01ac2fa7f924d41ef79b55c5faf2f2ca4d42433f214fc6d24ffed7633f`.
The settled prepared package and actual import, descriptor selection, and
binding receipts are retained under
`dist/mixed-python-review-evidence-341de90` for independent restricted probe
verification. The component reference is `resolved-python-v1@1.0.1`, digest
`3c24410b46415cb83b9d945ebf0049f552e9f7493943164f311939a79b49000e`;
the recipe bundle digest is
`086f3cbe68fc6412baad7dafb2f1c9c132275606460af030dd32391c8852d239`.

An earlier actual run against source `20040136f108718eb62702215f1c4f8047efdabe`
failed with `resource_blocked` after resolution and revision review, before
build/probe. Its bounded receipt remains at `dist/mixed-python-native-2004013.json`.
The inherited controller removed its safely joined failed workspace, so the
blocking resource dimension cannot be established from that retained receipt.
The controller now retains failed state and reports bounded capacity metadata.
The successful repeat kept the same resource budgets and product bytes; it did
not waive the failure or prove hard aggregate resource enforcement.

Independent review reran 153 tests with five opt-in skips, verified the original
nested destination replacement now fails, and held the actual adapter worker
inside `PreparationRunner._thread`: repeated cancellation waited until release,
then left zero tracked workers, callbacks, and publication tasks. The actual
secret scanner passed.

Changing the common builder also changes the text generation implementation closure, so
the old frozen TextGen 1.0.4 bundle is correctly blocked in this intermediate
source. One existing broad default-list assertion therefore fails until root
integration mints coherent TextGen 1.0.5; its expectation was retained. The
adapter packet does not restamp that published family.

This checkpoint does not close filesystem/network confinement, hard aggregate
resources, signed distribution, model accuracy, or the final Phase 3 adversarial
review. Complete native text generation acceptance remains a separate open gate.
