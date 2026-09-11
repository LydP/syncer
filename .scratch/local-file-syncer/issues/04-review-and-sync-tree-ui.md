# Review-and-sync tree UI

Parent: [Map: Local file syncer](../map.md)
Type: prototype
Status: resolved
Blocked by: 01

## Question

What does the review screen look like, where a check's results are shown and the user syncs at any level of the hierarchy?

Prototype an expandable tree (rule -> replica -> folder -> file) where each node carries its sync state (in sync / new / changed / will-delete / diverged) and a sync action. Work out: how partial selection rolls up and down the tree; how the delete category is kept visually separate and confirmed on its own; what "sync all" covers and what it deliberately excludes (deletes, diverged replicas); how a diverged replica is marked in the tree (hand-off to [Divergence UX](06-divergence-ux.md) for the resolution flow); and how much of a large tree is shown at once.

Build it as a throwaway PySide mock (or lighter) reacting to fake check data. Link the prototype from the answer. Consult `CONTEXT.md` and the `prototype` skill.

## Answer

### Prototype

Throwaway PySide6 app: [`../prototype/review_prototype.py`](../prototype/review_prototype.py) (run notes + variant table in [`../prototype/README.md`](../prototype/README.md); headless roll-up check in `_smoke.py`). Four variants of the review screen switchable from a floating bar, all reacting to one fake check result (a skill folder fanned to 3 replicas — ordinary drift / fully in sync / diverged + a foreign file — plus a single-file `resume.md` master to 2 replicas, covering every change category).

Variants tried: **A** one unified `rule › replica › folder › file` tree; **B** two-pane, left list = replicas; **C** flat list grouped by change category, no folder tree; **D** two-pane, left list = rules, right tree = `replica › folder › file` for the selected rule.

**Chosen: Variant D.** The rule is the unit of configuration (per `CONTEXT.md`) and the natural unit of review — you check and sync one rule at a time. A is too dense once several rules each fan out to many replicas; B buries the rule and makes "sync this whole rule" a multi-select chore; C loses the folder structure that makes a partial sync legible. Exact chrome (button set, labels, placement, the left-list row design) is refinement for spec assembly / ticket 07, not settled here.

### The review screen

- **Two panes.** Left: the sync rules, each row showing rule name, master path, replica count, and an aggregate one-line status (`N to sync, M to delete, K conflict` / `in sync`). Right: for the selected rule, a tree with **each replica as a top-level branch**, then folders, then files. One rule reviewed and synced at a time.
- **Per-node state.** Every leaf carries its change category from [05](05-check-engine.md)'s taxonomy with a glyph + colour (`=` in sync, `+` new, `~` changed, `−` master-deleted, `!` diverged, `≠` changed-on-both-sides, `?` no-baseline). Branch rows (replica, folder) show a rolled-up count.

  > **Correction ([ticket 05](05-check-engine.md)):** the glyphs are dropped — the user does not want symbols to learn. Each leaf shows a **plain-text phrase** instead (05's answer has the label per category: "In sync", "Missing from replica", "Updated in master", "Edited in this replica since last sync", "Changed in master and replica", "No record of a previous sync — can't compare", "Deleted from master", "Only in the replica", "Couldn't read this file"). Colour-grouping into the three buckets below is unchanged. 05 also finalised the taxonomy: `converged` (both sides changed to identical content) renders as "In sync"; `unreadable` is display-only, never checkable.

### Selection roll-up

- **One click = full tick + roll-down.** Clicking any branch (replica or folder) ticks it and every syncable descendant under it. Clicking a fully-ticked branch clears it and its descendants.
- **Partial is computed, never clicked.** A branch shows the tri-state dash only when some — not all — of its selectable descendants are ticked (e.g. after you untick one file). The user can never land on partial by clicking; Qt's `ItemIsUserTristate` / `ItemIsAutoTristate` are both deliberately off, and the parent state is recomputed bottom-up after every change.
- **Roll-up stops at the rule.** Selection spans the selected rule's replicas (via the right-pane tree) but there is no cross-rule "tick everything" in this screen — that is what "Sync all safe changes" is for.

### The three action categories, kept apart

| Category | In the tree | In "Sync all safe" | How it is applied |
|---|---|---|---|
| **Safe drift** — `new`, `changed` | checkable, rolls up/down | included | overwrite from master on tick |
| **Master-deleted** — `will_delete` | checkable, flagged red with `−` | **excluded** | own confirm step before anything is removed (granularity + wording owned by [08](08-deletion-and-rename-handling.md)) |
| **Conflict** — `diverged`, `both_changed`, `no_baseline` | **not checkable**; row is display-only, distinctly coloured, labelled "needs a decision → ticket 06" | **excluded** | resolved only through [06](06-divergence-ux.md)'s flow; this screen never overwrites or skips a conflict silently |

- **"Sync all safe changes"** ignores the current tick selection entirely: it applies every `new` + `changed` file across the rule (or across all rules, if invoked from the main window), and touches nothing in the other two categories. This is the settled "master wins, but only for uncontested drift" behaviour.
- **A diverged replica** is marked at the replica branch (rolled-up conflict count, distinct colour) and at each conflicted file leaf. The branch is still expandable so the user can see which files conflict, but neither the branch nor those leaves offer a tick — the only affordance is "resolve → ticket 06".

### How much of a large tree is shown

- Default expansion reveals only subtrees that **contain drift**; fully-in-sync folders and replicas are collapsed and represented by their parent's rolled-up summary. In-sync leaves are shown greyed when a containing folder is expanded, for context, but are never checkable.
- **No virtualisation / lazy loading for v1.** The driving use cases (skill folders, a single resume file) are small; `QTreeWidget` populated eagerly from the check result is fine. If [05](05-check-engine.md)'s performance work shows big trees are realistic and slow, revisit under the map's "performance strategy for large folders" fog item — this is a rendering concern, separable from the layout decided here.

### Domain model

No `CONTEXT.md` change. Confirms [03](03-divergence-state-model.md)'s flagged candidate terms *converged* / *changed on both sides* as worth a glossary entry when [05](05-check-engine.md) fixes the full taxonomy; also *safe drift* as the name for the tick-and-sync-all-eligible set. No ADR — no surprising or convention-breaking call here.

### Deferred to later tickets / spec assembly

- Divergence resolution flow, per-file conflict options, viewing what changed locally → [06](06-divergence-ux.md).
- Delete confirmation granularity and wording, emptied-directory handling → [08](08-deletion-and-rename-handling.md).
- Main-window chrome, where "check" is triggered, how the review screen is entered and dismissed, multi-rule "sync all" entry point → [07](07-gui-rule-management.md).
- Final button set / labels / visual design of the review screen → [09](09-assemble-spec.md), pulling from the prototype.
