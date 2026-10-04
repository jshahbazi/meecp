# Signed app root metadata identity

Cold copied signed apps could exit during ordinary startup with
`invalid_release_policy`. The retained diagnostic isolated a change in only
the enclosing app directory's ctime after its original identity capture.
A separate untouched-copy observation saw macOS add `com.apple.macl` during
startup. The latter supports an operating-system metadata explanation; it does
not establish the exact writer in the failing observation.

The shared signed artifact verifier and command admission's local comparison
now exclude ctime only for their exact, already validated enclosing app
directory. The verifier derives this path from the source-owned running host's
fixed `Contents/Resources/host` placement and validates its signature, receipts,
and payload. No configuration, request, provider, environment variable, or
directory-name search selects the exception. The held proof stores the path in
a private frozen field excluded from its constructor. Manually constructed or
copied dataclass proofs retain strict comparisons by default.

The original baseline remains held. Comparisons retain the app directory's
device, inode, type and permissions, UID, GID, link count, size, and mtime. Every
descendant directory and regular file retains its full prior identity,
including ctime; nested directories named `.app` receive no exception. Existing
outer-ancestor identity checks, exact membership, hashes, signed provenance,
policy bytes, ownership, queue and final native-creation fences remain in place.

The root's GID, filesystem flags and POSIX extended ACL are independently bound
before signature validation. One no-follow directory descriptor is held for
the proof's lifetime and closed when its metadata owner is released. Each
comparison checks its descriptor and path identity before and after reading
the ACL through Darwin libc. The SDK's canonical big-endian `acl_copy_ext`
representation preserves ordered entries, raw UUID qualifiers and flags;
inspection is limited to 128 entries and 4096 bytes. There are no subprocesses
or account-name lookups at current-authority checks. An absent ACL is the
Darwin `acl_get_fd_np` ENOENT result on the held live descriptor; unreadable,
unsupported, invalid, oversized or failed inspections deny admission.
The private metadata owner retains a fixed baseline by implementation; it is
not a language-enforced immutable object or a client-supplied authority object.

This stops treating ctime alone as execution authority while preserving ACL
and filesystem flag revocation. Other extended-attribute history, including
macOS MACL changes, is not independently bound. The original same-user scope
and pathname race limitations remain; this does not claim a hostile-user or
kernel-enforced confinement boundary.

Controls use private synthetic artifact metadata and simulated signature
results, with real same-mode directory `chmod` to change ctime alone. They cover
mutation during both signature boundaries and later held checks, strict manual
proofs, root structural/permission/mtime/inode changes, a simulated UID change,
and descendant ctime, inode, permissions and restored-byte mutations. Actual
private ACL add/remove/replace and filesystem flag changes deny both shared
and command-local checks. Owned group-switch and explicit stat-fixture controls
cover GID changes before and during ACL inspection. A cosmetic root xattr update preserves the held
nonempty ACL across repeated checks. The same
two positive controls fail with the original unchanged Git modules loaded in
RAM. No real signed app, installed state, credential, model, or provider is
modified or executed by these source controls.

Published components, helpers, recipe bundles and runtime assets are unchanged.
The backend change requires a new centrally minted TextGen producer version;
existing versions must not be restamped. Fresh signed cold-copy startup and
public native acceptance remain separate required checks after that mint.
