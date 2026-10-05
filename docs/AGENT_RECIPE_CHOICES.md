# Agent preparation path discovery

Agent options provide bounded recipe choices so an ordinary directory can start
with a compatible preparation path. Discovery inspects captured file names,
sizes and text classifications. It performs no filesystem reads, credential
resolution, provider calls, dependency resolution or execution.

The owner options response adds:

| Field | Contract |
| --- | --- |
| `recipe_choices` | At most 128 records, in the same order and with the same exact references as `recipes`. |
| Choice `recipe` | Existing recipe `id`, `version` and `digest`. |
| Choice `label` | Public host label, at most 96 characters. Unknown recipes use their existing ID. |
| Choice `compatibility` | `compatible` or `unresolved`. This is metadata eligibility for review. |
| Choice `recommended` | Boolean; at most one true value, which must be compatible. |
| `recipes_truncated` | Boolean indicating that additional enabled recipes were omitted. All recommendations are false in this case. |

Only enabled recipes enter the displayed inventory. The host checks current
admissibility and resolves an exact trusted guide/component binding before
marking a choice compatible. Changed components, changed guide instructions and
unavailable lifecycle states do not inherit a recommendation. Imported recipe
bundles cannot register selection rules or supply discovery code.

## Current trusted rules

Both Python rules require a visible text-classified `.py` file within the
existing content selection limit of 16,384 bytes. Binary files are advertised
only in the separate identity inventory. Content and identity inventories each
contain at most 256 records.

- An existing dependency lock requires exactly one sibling `pyproject.toml` and
  `uv.lock` pair. Both must be visible, nonempty text identity records within the
  existing 16 MiB limit; a selectable Python file must be within that project.
- A directory needing dependency resolution has no sibling project/lock pair.
  Separately reviewed agent output supplies bounded requirements to the ordinary
  owned resolver.

Multiple pairs, binary or oversized pairs, unavailable source records and
unsupported source types remain unresolved. Their presence cannot silently
select a different dependency mode. These rules provide no acceptance evidence
for other runtimes or arbitrary applications.

The exact built-in recipe is preferred among compatible aliases of one trusted
guide. A unique compatible alias can be recommended when that built-in recipe is
unavailable. Multiple preferred versions, multiple aliases without a preferred
recipe, or overlapping guide families require an explicit choice.

## Extension and acceptance

Trusted host code can register an `AgentRecipeChoiceRuleV1` against a guide's
exact ID, digest and component reference, with bounded suffix/pair metadata and
a public label. New runtime rules require an accepted guide/component and their
own compatibility evidence. This registration does not add execution authority.

Native integration must retain a still-available explicit user selection, show a
choice placeholder when no recommendation exists, and reject malformed or
inconsistent recommendation records. Review and start continue to validate
source identities, recipe lifecycle, provider scope and execution gates. A
recommended recipe does not establish successful generation, installation or
live-provider compatibility.
