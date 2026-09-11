# Deletion & rename handling

Parent: [Map: Local file syncer](../map.md)
Type: grilling
Status: resolved
Blocked by: 05

## Question

How are deletions and renames in the master reflected in replicas?

Decide: how "the master deleted this file" is presented in the review (a distinct category, confirmed separately from updates — already agreed; work out the granularity and wording); whether a rename in the master is detected as a rename or simply treated as delete-old + create-new (and if detected, how — content hash match?); what happens to a replica file that is under the rule's tree but the master never had it (leave it? flag it? it may be a local addition, which overlaps with divergence); whether an emptied directory in the master causes the replica directory to be removed; and whether deletes are ever done without an explicit tick even under "sync all" (agreed: no).

Depends on the change taxonomy from [Check engine](05-check-engine.md). Consult `CONTEXT.md`.

## Answer

### Rename detection: not in v1

A master rename is never detected — it always falls through to the existing taxonomy as two independent, unrelated changes: the old path categorises `master_deleted`, the new path categorises `new`. Hash-based rename detection was considered and rejected for v1: it has real ambiguity (which deleted candidate pairs with which new one when several files share content; a renamed-and-edited file won't hash-match at all) for a driving use case (skill folders, one resume file) where renames are rare. The fallback isn't wrong, just two confirms instead of one smart move. Not added to "Not yet specified" — this is a decided-against, not an open question — but worth revisiting post-v1 if real usage shows renames are common.

### `replica_only` (in replica, never in master, no baseline): informational only

Distinct from divergence — divergence requires a baseline showing the file used to match master at some point; `replica_only` never existed in the sync relationship at all (e.g. a file the user manually dropped into a replica folder). Treated at the same tier as `unreadable`: shown in the tree for visibility, **never checkable, never auto-deleted**. Full invisibility risked hiding a genuine mistake; offering a delete action would mean the tool destroying a file it never put there, against "one-way fan-out, no reverse push." No promotion to a `CONTEXT.md` glossary term — stays an implementation-level category name, per [Check engine](05-check-engine.md)'s existing precedent for the finer taxonomy states.

### `master_deleted` confirm mechanics

Ticked individually in the tree exactly like safe drift (per [04](04-review-and-sync-tree-ui.md)), but applying a batch that includes any ticked `master_deleted` files pops **one dialog per apply action**, itemising every ticked master-deleted file (path + replica) with a single "Yes, delete N files" acknowledgment — not a per-file confirm. Keeps a multi-file restructure to one click of visible confirmation instead of N interruptions.

### Emptied master directory → replica directory removed, silently

Once every master-deleted file under some replica subdirectory has been confirmed and applied, the tool removes the now-empty replica directory as a **silent post-deletion cleanup pass** — no separate confirm, since the risk was already accepted when the constituent files were confirmed. Directories remain non-entities in the taxonomy (per [05](05-check-engine.md)); this is cosmetic cleanup after the fact, not a new sync-unit concept.

### New state: master missing (whole master root gone, not just individual files)

If a rule's entire master — the whole directory, or the one file for a file-type rule — doesn't exist or can't be read at check time, the check does **not** fall through to ordinary per-file `master_deleted` drift (which would flood every replica file into one deletable batch — a one-click wipe risk from something as mundane as an unmounted drive or a bad path). Instead the whole rule reports a distinct **master missing** state (new `CONTEXT.md` term), blocking ordinary tick-and-sync for that rule.

The user can still deliberately clear replicas: a per-rule **"I know the master is missing — unlock"** acknowledgment (one click, not per-file) drops that rule back into the normal tree, every file shown as ordinary `master_deleted`, individually tickable, going through the same batch-delete confirm above. The unlock is a one-time, in-session acknowledgment — **not persisted to `state.json`** — so reopening the app or re-running check re-blocks if the master is still missing. This reuses the existing tree/tick/confirm machinery rather than inventing a second, blunter deletion pathway, and still lets the user selectively keep files rather than being forced into all-or-nothing.

If the master reappears after a partial clear, no special-casing is needed: kept files compare normally against the returned master, and files already deleted from the replica fall into the existing "locally-deleted replica file → `new`" edge case from [05](05-check-engine.md) — master wins, re-provisions.

**Corrects [04](04-review-and-sync-tree-ui.md)**: the rule list (left pane) needs a distinct status for a rule in the master-missing state, separate from ordinary drift counts — detail left to [07](07-gui-rule-management.md)/spec assembly, not reopened here.

### Domain model

`CONTEXT.md` gains **Master missing** (added this session) — a rule-level state distinct from the per-file `master_deleted` category. `replica_only` stays implementation-level (not promoted). No ADR: none of these decisions are hard to reverse or architecture-level, just behavior spec.
