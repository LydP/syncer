# Sync application — how ticked changes are executed

Parent: [Map: Local file syncer](../map.md)
Type: grilling
Status: resolved
Blocked by: 05, 08

## Question

Once the user has ticked a set of changes in the review tree (or hit "Sync all safe changes"), how are they actually applied to a replica?

Decide: the copy mechanics (`shutil.copy2`, temp-file-then-`os.replace` for atomicity, directory creation, preserving master casing); whether and how a file is backed up into `backups/` before it is overwritten or deleted (retention — keep how many? per-run subdir? prune policy?); the deletion mechanics for `master_deleted` files (and whether an emptied replica directory is removed — coordinate with [08](08-deletion-and-rename-handling.md)); what is written to `logs/` (one log per run? format? level of detail?); ordering of operations within a sync; what happens on partial failure mid-run (a copy throws halfway through "sync all" — stop? continue and collect errors? roll back?); how `state.json` is updated after a full vs. partial sync (the merge behaviour is already fixed by [03](03-divergence-state-model.md) — this ticket wires it to the executor); and what the user sees during and after a sync (progress, a result summary, errors).

This is the executor that consumes [05](05-check-engine.md)'s `CheckResult` and [08](08-deletion-and-rename-handling.md)'s deletion semantics. Divergence-conflict resolution is **not** here — [06](06-divergence-ux.md) owns that; this ticket only applies changes the review screen deemed applicable. Consult `CONTEXT.md` and [ADR 0001](../../../docs/adr/0001-portable-self-contained-storage.md).

## Answer

### Copy mechanics

- **New files** (nothing at that path on the replica yet — `new` category, or first-time provisioning): `shutil.copy2()` straight to the final path. Missing parent directories are created first via `os.makedirs(exist_ok=True)`.
- **Overwrites** (`changed` category, an existing replica file): write to a **same-directory temp file** named `<filename>.syncer-tmp-<uuid8>` (same directory required for `os.replace()`'s same-volume atomicity; the uuid8 suffix avoids collisions between concurrent operations), then `os.replace()` it onto the final path. On success the rename *is* the cleanup — nothing named `.syncer-tmp-*` is ever left behind. On any failure before the replace completes, a `finally` block removes the orphaned temp file (best-effort — a further error removing it is swallowed, not raised). A true in-memory temp isn't possible: `os.replace()` requires two real on-disk paths on the same volume.
- **Casing**: no active case-correction. Replica/master matching is case-insensitive on Windows ([03](03-divergence-state-model.md)/[05](05-check-engine.md)); a casing-only difference is left untouched (already `in_sync`, not a copy target). Casing only ever changes as a side effect of a real content copy landing under `shutil.copy2`'s target name. Not revisited for symmetry with a future POSIX build — case-sensitive matching there makes a casing mismatch two unrelated paths (`replica_only` + `new`), not the same file needing a rename, so there's no POSIX behavior to be consistent *with*. Revisit if/when a POSIX build actually exists.

### Backups: none in v1

`backups/` stays reserved by [ADR 0001](../../../docs/adr/0001-portable-self-contained-storage.md) but unused. Master already wins by design, and everything the executor applies was already flagged safe drift or explicitly confirmed (master-deleted batch, or a [06](06-divergence-ux.md) resolution) — there's no ambiguity a pre-overwrite snapshot would be hedging against, and it would add real complexity (retention, pruning, disk growth) for a personal one-way tool. Revisit only if real usage shows people want an undo button.

### Deletion mechanics

Applying a confirmed `master_deleted` file removes it (`os.remove`); no backup (above). Once every `master_deleted` file under some replica subdirectory has been applied, the now-empty directory is removed, and pruning **walks upward**: if removing it leaves its parent empty too, that parent is removed as well, repeating up the chain — stopping at (never removing) the replica root itself. Matches [08](08-deletion-and-rename-handling.md)'s "silent post-deletion cleanup, no separate confirm."

### Ordering

Within one replica's applied batch: **copies before deletes** (provisioning/updating first means a failure partway leaves the replica more complete, never emptier; nothing requires deleting first). Across replicas, and across rules for a cross-rule "Sync all safe changes": **strictly sequential**, one replica fully processed before the next starts — no parallelism. Matches [05](05-check-engine.md)'s single-threaded `check()` shape and keeps progress/cancellation and the result summary simple.

### Partial-failure handling

**Continue and collect errors.** A copy or delete that throws does not stop the run; every other file in the batch is still attempted. Failures are collected (path + message) and surfaced in the post-run summary (below) and the log. A file that failed keeps its pre-sync category on the next check, since its baseline entry (below) is simply never written for it — no rollback needed, since every operation is independently idempotent.

### `logs/` output

One plain-text log file per run: `logs/sync-<ISO8601-timestamp>.log`. One line per file operation (path, action taken, outcome), plus a trailing summary line (counts by outcome, error count, duration). This is the only durable record of *what happened* in a run — `state.json` holds only the resulting baseline, not history.

### `state.json` write timing

Committed **once per replica, immediately after that replica finishes** (not buffered for the whole run) — same tmp+`os.replace()` atomic pattern [02](02-sync-rule-config-model.md)/[03](03-divergence-state-model.md) already use for config/state writes, and the same per-file merge semantics [03](03-divergence-state-model.md) fixed (only entries for files actually copied are updated; everything else untouched; `last_sync` bumped). Per-replica commit means a multi-replica, possibly multi-rule "Sync all safe changes" that's interrupted partway keeps every replica that already finished, rather than risking the whole run's progress on one in-memory buffer.

### User-visible feedback

`sync(rule, applied_changes, progress(done, total, current_path), cancel() -> bool)` — mirrors [05](05-check-engine.md)'s `check()` signature so [07](07-gui-rule-management.md) wires both to the same worker-thread pattern. After a run completes, a result summary dialog shows counts by outcome (copied, deleted, skipped-on-error) plus a scrollable list of any errors (path + message); per-file success detail is not repeated in the dialog since it's already in the log.

### Domain model

No `CONTEXT.md` change — nothing here introduces a new domain concept, only implementation behavior. No ADR — none of these calls are hard to reverse or architecture-level (same reasoning as [08](08-deletion-and-rename-handling.md)); they're behavior spec for the executor.
