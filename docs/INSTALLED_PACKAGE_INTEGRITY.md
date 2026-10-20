# Installed-package integrity

Python may create new `__pycache__` files or replace stale timestamp-based
bytecode during an import. These writes violate the package's exact artifact
lock. A host restart after an update can expose that earlier mutation as
`artifact_tree_mismatch`, `artifact_size_mismatch`, or `artifact_hash_mismatch`.

The host protects installed trees at `packages/<id>/<digest>` before execution:

- Installation and upgrade copies remove owner write permissions before
  publishing the tree. Directories and executables use mode 0500; other files
  use 0400. Existing valid installed trees receive those permissions on their
  first uncached load. Source folders and preparation snapshots stay writable.
- File copies preserve source modification timestamps in both the APFS clone
  and byte-copy paths, preserving timestamp-based cache validity on relocation.
- Package cache stamps enumerate the complete tree without following symlinks.
  Added files/directories, removed entries, mode changes, and changed file
  metadata force validation against the original lock.
- Source-built Python launchers pass `-B` explicitly. Environment-only
  `PYTHONDONTWRITEBYTECODE` is insufficient when Python uses `-I` or `-E`.
  The frozen signed TextGen runtime retains its original bytes and provenance;
  installed-tree permissions also protect those existing launchers.
- Host, legacy app, and experimental Uzu builds filter runtime caches and
  remove source-backed bytecode after dependency installation. Sourceless
  dependency bytecode is retained. Preparation launchers already pass `-B`.

These permissions guard against accidental writes. A program running as the
owner can change them; enforceable filesystem/network isolation remains deferred.
Package programs must put mutable files in the supplied slot data and runtime
directories. A program that requires writing beside its executable needs an
adapter change before it can run from the installed tree.

The host never changes an artifact lock to accept runtime mutations and never
silently deletes unexpected package files. A previously damaged installation
still needs recovery from the original verified bundle. Removing generated
cache residue is only safe after proving that all locked artifacts are intact;
changed locked bytecode requires reinstalling verified contents.

Regression coverage includes launch/import through a real installed gateway
with stale locked bytecode and isolated Python, prevention of both file writes
and new directories, migration of existing trees, complete-tree cache
invalidation, copy timestamps, compiled launcher flags/argument forwarding,
and build-time source-backed cache cleanup. Signed application and physical
model acceptance retain their separately recorded source scope.

The complete shipped-catalog regression also validates release-policy producer
pins and seeds every active recipe. Changes to shared host helpers can alter
more than one component identity; each affected component receives a fresh
version and immutable recipe while historical bundles and owner opt-outs stay
intact.
