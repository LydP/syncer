# Divergence UX

Parent: [Map: Local file syncer](../map.md)
Type: grilling
Status: resolved
Blocked by: 03, 04

## Question

When a check finds a diverged replica (modified locally since last sync), what does the user see and what choices do they get?

Decide: how divergence is surfaced in the review tree vs. ordinary "changed" drift; what per-file options exist (overwrite from master / skip / keep replica's version permanently?); whether the user can view what changed locally before deciding, and at what fidelity (list of files / actual diff — the latter was flagged a stretch); what happens to the snapshot when the user chooses "skip" (does the replica stay flagged forever?); and the "changed on both sides" case (master moved on AND replica was edited locally).

Depends on the tree structure from [Review-and-sync tree UI](04-review-and-sync-tree-ui.md) and the state definitions from [Divergence state model](03-divergence-state-model.md). Consult `CONTEXT.md`.

## Answer

### The resolution dialog

A **conflict** (04/05's `diverged`, `both_changed`, `no_baseline` categories — now a formal glossary term) is never ticked in the main review tree; it is **resolved** through a dedicated dialog, opened two ways:

- **Per-file** — a "Resolve" affordance on the conflicted leaf opens the dialog scoped to that one file.
- **Per-replica** — a "Resolve conflicts" affordance on the replica branch opens the same dialog as a **queue**: resolving one file immediately advances to the next unresolved conflict in that replica, closing when the queue empties or the user dismisses it.

One dialog, one action set, for all three conflict categories — only the diff content shown adapts:

| Category | Diff shown |
|---|---|
| `diverged` | replica vs. baseline |
| `both_changed` | two diffs: master vs. baseline, and replica vs. baseline |
| `no_baseline` | direct master vs. replica, with a "no record of a previous sync" callout instead of a baseline diff |

**Diff fidelity**: a real line diff for text files (the driving use cases are Markdown skill files and a resume, all text — not the stretch goal the ticket worried about). A binary file, or a text file over a size threshold, falls back to **metadata only** ("Binary file — diff unavailable" / size threshold note, plus size/mtime for each side) — no best-effort diff attempted.

### The three actions

Every conflicted file offers the same three choices, and each **applies immediately** on confirmation — independent of the tree's tick-and-Sync staging model; resolving a conflict is a self-contained action, not something queued for a later "Sync" click.

- **Overwrite from master** — copies master's content in; baseline updates to master's hash (ordinary sync outcome).
- **Skip for now** — a pure no-op. The baseline is **not** written (confirms [03](03-divergence-state-model.md)'s existing answer). The file is re-flagged as a conflict on every future check, forever, until the user picks one of the other two actions. No snooze/dismiss state — deliberately not introduced.
- **Keep replica's version** — adopts the replica's current content as the new baseline (so it stops being flagged as *content*-different from what's tracked) **and** sets a new persistent **kept** flag (formal glossary term — see Domain model below) so the tree keeps showing it as a deliberate departure from master rather than plain "In sync". The kept flag **clears automatically** the next time the master genuinely moves past the kept version — the file then re-enters ordinary drift (`changed`, safe drift) like any other file, with no lingering historical marker.

### Bulk resolution

The replica branch also offers **per-category bulk actions** — scoped separately per conflict category, not a single blanket "resolve everything":

- For `diverged`: "Keep all as-is" (marks every diverged file `kept`) and "Overwrite all from master".
- For `both_changed`: the same pair, scoped to that category.
- `no_baseline` is **excluded from any bulk action** — always per-file, per [03](03-divergence-state-model.md)'s existing "no baseline → explicit per-file confirmation only" rule.

A bulk **overwrite** requires its own confirmation ("This discards N local edits — overwrite from master?"), consistent with how master-deletion ([08](08-deletion-and-rename-handling.md)) is gated behind its own confirm before anything destructive happens. A bulk **keep** needs no confirm — it discards nothing, it only stops flagging content that already exists.

### Correction to [Divergence state model](03-divergence-state-model.md)

`state.json`'s per-file entry (`{hash, size, mtime}`) gains a fourth, optional field recording the kept flag (e.g. `"kept": true`) — set by the "keep replica's version" action, cleared the next time that file's entry is overwritten by an ordinary drift-sync. Everything else about the schema, write timing, and the no-baseline handling in ticket 03 stands unchanged.

### Domain model

`CONTEXT.md` gains three terms this ticket's decisions require: **Conflict** (the umbrella over divergence / changed-on-both-sides / no-baseline), **Resolve** (the user's decision on a conflict — the conflict-side counterpart to sync), and **Kept** (the persistent state left by choosing the replica's version). No ADR: the kept-flag schema addition is a correction to ticket 03's baseline shape (same pattern as [05](05-check-engine.md)'s correction to [04](04-review-and-sync-tree-ui.md)'s glyphs), not a hard-to-reverse architectural call.
