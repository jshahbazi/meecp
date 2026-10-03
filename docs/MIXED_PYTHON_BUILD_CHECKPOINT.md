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

The actual clean-host native run is pending at this source checkpoint. Changing
the common builder also changes the text generation implementation closure, so
the old frozen TextGen 1.0.4 bundle is correctly blocked in this intermediate
source. One existing broad default-list assertion therefore fails until root
integration mints coherent TextGen 1.0.5; its expectation was retained. The
adapter packet does not restamp that published family.

This checkpoint does not close filesystem/network confinement, hard aggregate
resources, signed distribution, model accuracy, or the final Phase 3 adversarial
review. Complete native text generation acceptance remains a separate open gate.
