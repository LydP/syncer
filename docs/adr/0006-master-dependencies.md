# Masters can declare optional, global dependencies on other masters

## Status

accepted

## Decision

A **Dependency** is a directional, global relationship between two master paths — "A depends on B" — declared by hand, independent of any rule, keyed by path like **Replica name**. It means: wherever A is synced, B is expected to land alongside it in the same replicas (at its own landing path, never nested inside A's). No cycles, including self-dependency. A master may have several dependencies and be depended on by several masters. A dependency edge stores `{path, type}` on both ends, since accepting one has to add a fully-formed `Master` to a rule; matching "does this rule already contain A/B" is by path alone.

An **Unmet dependency** — rule contains A, A depends on B, rule doesn't contain B — is a config-shape fact (like **Cross-rule namespace collision**), not a per-file check category. It surfaces as a rule-level badge in the review tree, alongside **Master missing**, both at rule-edit time and at every check, for as long as it stays true. The user resolves it per pair by accepting: B is added to the rule as an ordinary, visible master — a real config edit, after which B is checked and synced like any other master, with no separate "this master came from a dependency" bookkeeping.

Instead of the per-pair acknowledgment **Kept** uses, unmet dependencies are silenced via **Ignore dependencies**: a single checkbox per rule that blanket-silences every dependency still unmet for that rule (past, present or future), without adding any of their masters. The badge stays visible either way — checked only changes its tone, from "unmet dependency — accept?" to "unmet dependency — ignored for this rule." The fact is re-evaluated live: editing the dependency edge, or removing an accepted B from the rule again, produces a fresh unmet dependency, not a resurfacing of an old one.

Dependencies are never auto-detected from file content (e.g. parsing a `SKILL.md` for calls to other skills) — declared by hand only.

## Why

Driving case: Claude Code skill folders, where one skill's instructions call another. A user syncing a "calls-other-skills" skill as a master wants to know its callee should probably ship alongside it, without Syncer forcing the callee in or silently syncing it behind the scenes. Keeping the relationship global (not per-rule) matches how the same skill pair recurs across many project rules — declaring it once, like a replica name, avoids redeclaring it per rule. Making "accept" a real, visible master addition (not a hidden background sync) means every other already-built mechanism — check, the review tree, cross-rule namespace collision, master missing — applies to a dependency-derived master with zero new logic; the alternative (silently syncing B without it appearing in the rule) would need a parallel, invisible sync path next to the one that already exists.

## Considered Options

- **Per-pair persistent acknowledgment** (mirroring **Kept**): dismissing a single A→B notification remembers that exact dismissal. Rejected as the primary mechanism — with several masters each depending on several others, dismissing one pair at a time is exactly the per-rule tedium the user opened this whole effort trying to avoid; a single per-rule "ignore dependencies" checkbox collapses N dismissals into one decision.
- **Per-master-in-rule or per-edge checkboxes**: finer-grained ignore scopes than per-rule. Rejected for the same reason — per-rule is already the unit the user thinks in when building out a project's masters, and finer grains reintroduce the per-pair tedium the checkbox exists to avoid.
- **Silent background sync on accept** (B syncs to replicas without appearing in the rule's master list). Rejected — `config.toml` stays the single statement of what syncs where; a hidden sync path would need its own review-tree representation, collision detection, and master-missing handling, duplicating machinery that already exists for ordinary masters.
- **Auto-detecting dependencies from content** (e.g. scanning a skill's instructions for references to other skills). Rejected — Syncer is general-purpose (skills, a resume, anything), and detection would tie the mechanism to one content format; declared-by-hand keeps it format-agnostic. Out of scope for this effort, revisit only if a specific format's detection is worth a dedicated future effort.

## Consequences

- New global config shape and a new per-rule flag. The field/TOML-shape level, left open when this ADR was accepted, was settled in [Config schema: TOML shape for dependency edges and Ignore dependencies flag](https://github.com/LydP/syncer/issues/54): edges live in a top-level `[[dependency]]` array-of-tables in `config.toml`, alongside `[[rule]]` and `[[replica]]`, each side a `{path, type}` table reusing the existing `Master` dataclass (hand-writable inline; `tomli_w` renders it back as `[dependency.master]` / `[dependency.depends_on]` sub-tables, which parse identically); the flag is `ignore_dependencies: bool = False` on `SyncRule`. An edge has no id — `(path_key(master.path), path_key(depends_on.path))` is its identity, so casing and `/` vs `\` spellings name the one edge. `_validate_dependencies` rejects a duplicate pair (`DuplicateDependencyError`) and a cycle, self-edges included (`DependencyCycleError`), from both `load_config` and `save_config` — the two call sites `_validate_rules` and `_validate_replica_names` already use. A path's `type` is deliberately not cross-checked for agreement across edges, matching how `config.py` doesn't cross-check `type` for a shared master path today.
- `CONTEXT.md` gains **Dependency**, **Unmet dependency**, and **Ignore dependencies**.
- No new `Conflict` category and no changes needed to `conflict.py` — an unmet dependency is never a per-file drift, so it never enters the conflict queue.
- Exact GUI mechanics (where dependencies are declared/edited, one-by-one vs. bulk accept, badge wording) are deliberately left to a prototyping round, mirroring how the GUI rule editor for multi-master rules (#18) was resolved via prototyping rather than decided in the abstract.
