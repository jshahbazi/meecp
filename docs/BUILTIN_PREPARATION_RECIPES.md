# Built-in preparation recipe metadata

`preparation_builtin_recipes.seed_builtin_recipes(recipe_store)` makes the
shipped Python callable recipe available for generated setup questions. Call it
from host startup before presenting onboarding. This checkpoint does not alter
Supervisor, enable the preparation runner, execute source or pass runtime gates.

The host-owned catalog is `templates/builtin-recipes/catalog.json`. Each entry
pins an immutable recipe ID, version, bundle directory and exact closed-tree
digest. The bundle pins the trusted Python component hash, contains its complete
metadata license/notices and has empty recipe inputs/form fields. App identity,
interface, effects, resources and synthetic fixtures belong to the host's
generated questions. No scaffold-only label or precision fields are required.

The seeder validates catalog shape, bundle closure, exact current host component
identities and static compatibility. It accepts at most 16 catalog entries, 32
files per built-in bundle and 256 KiB of validated bytes per bundle. The shared
bundle reader applies its own read-time bounds. Adding a built-in recipe changes
the metadata catalog and bundle; the seeder contains no recipe-name dispatcher.

The returned object includes `schema_version`, `catalog_digest`,
`execution_available: false` and per-recipe statuses. Each status has an exact
`recipe: {id, version, digest}`, `seed_state`, `recipe_state`, `ready`, fixed
blockers and `execution_available: false`. `ready` means the current registry
metadata admits this exact enabled version. Normal recipe resolution still
verifies captured bundle bytes before use. Pass the ready recipe references as
preferred choices to generated forms; an explicit user choice takes precedence.

The separate `builtin_recipe_seeds` table retains immutable identity and lifecycle
tombstones independently of recipe records. On first admission it records a
pending seed, then uses the existing static `RecipeStore.add` snapshot operation.
Only the untouched initial proposed record can become metadata-enabled. That
transition and its terminal seed marker commit in one import-store transaction.
The seed performs a narrow compare-and-swap rather than calling the explicit
user re-enablement API. A concurrent disable, revoke, removal or other epoch
change wins.

A process exit before or after registration resumes from the pending marker.
An interrupted enable transaction rolls back both writes. A completed seed is
never automatically repeated, even if its recipe record is later collected.
Preexisting records are preserved, including proposed records and version
collisions. An explicit opt-out also prevents a later catalog version of the
same recipe ID from silently restoring the default. Catalog updates may seed a
new version when the earlier version remains enabled; older records and evidence
retain their identities. The user can explicitly add/enable a desired version
through recipe management.

Changed component or bundle bytes require a new immutable recipe version and
updated catalog pin. Existing enabled evidence is not silently restamped when
host admission identities change. The seeder reports stale evidence or
compatibility blockers without rewriting user state. A missing/corrupt shipped
catalog raises a fixed `builtin_recipe_catalog_invalid` error; individual recipe
failures produce bounded statuses.

Focused tests cover exact pinned metadata, empty inputs, concurrent cold starts,
closed catalogs, immutable collisions, lifecycle preservation, state surviving
restart, real process exits at four registration/commit boundaries and generated
form suitability when that integration module is available.
