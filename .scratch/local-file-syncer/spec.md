# Syncer — build spec

Status: build-ready. This document assembles every decision made while charting
[Map: Local file syncer](map.md) into a spec someone can implement from without
re-litigating anything. It does not itself decide anything; each section links back to the
ticket that owns the decision. Building the tool is a separate future effort — nothing in
`.scratch/local-file-syncer/` implements it.

## 1. What Syncer is

A general-purpose, offline, Windows-only desktop tool (Python + PySide6) that keeps many
**replicas** of a file or folder current with a single canonical **master**: one-way fan-out,
master wins, no merge, no reverse push. Driving use cases: fanning Claude Code skill folders out
to many project directories (each project taking its own subset), and mirroring one master resume
file into several directories.

Vocabulary is authoritative in `CONTEXT.md` — use *master*, *replica*, *sync rule*, *check*,
*sync*, *drift*, *divergence*, *safe drift*, *converged*, *changed on both sides*, *conflict*,
*resolve*, *kept*, *master missing* exactly as defined there. Avoid the synonyms it lists
(source, destination, mirror, push, scan, diff, compare, mapping, pair, job, ignored, resolved
as a catch-all).

## 2. Settled constraints (non-negotiable going in)

- One-way fan-out, master wins, no merge, no reverse push.
- One sync rule per syncable unit (one master, its replicas).
- Python + PySide6 desktop GUI.
- On-demand **check** only — no filesystem watching.
- Windows only for v1 (Mac/Linux are future, informing some choices below but not built now).
- Content-based comparison (hashing), never timestamps, for any correctness decision.
- Human-readable `config.toml` (user intent) plus a separate `state.json` (last-sync data),
  both GUI-managed.
- First-time provisioning of a missing replica is handled like any other update — nothing
  special.
- Deletions propagate but are confirmed as their own category, distinct from updates.
- Engine is standard-library-based: `os.walk` + `hashlib` + `shutil.copy2`/`os.replace`. No
  `filecmp.dircmp` (see §5 — a two-way compare can't express the three-way baseline model) and
  no third-party sync library ([Survey free tools](issues/01-free-tool-survey.md)).

## 3. Storage: portable, self-contained ([ADR 0001](../../docs/adr/0001-portable-self-contained-storage.md), [Sync-rule config model](issues/02-sync-rule-config-model.md))

Everything Syncer generates lives in `base_dir` — `dirname(sys.executable)` when frozen, else the
project root — **never** the registry, `%APPDATA%`, `%LOCALAPPDATA%`, `%TEMP%`, or POSIX
equivalents. This is a deliberate, permanent deviation from the Windows convention; do not "fix"
it later.

- `base_dir` must be user-writable, checked on startup; a non-writable `base_dir` is a hard
  blocking error with **no fallback**.
- Contents of `base_dir`: `config.toml`, `state.json`, `syncer.lock`, `backups/` (reserved,
  unused in v1), `logs/`.
- Distribution is a self-contained folder (a plain script, or a PyInstaller **one-folder**
  build), not a system installer and not a one-file exe (a one-file exe unpacks to a temp dir and
  fights this rule). Final packaging call deferred to the build effort — see §11.

## 4. `config.toml` — user intent ([Sync-rule config model](issues/02-sync-rule-config-model.md))

TOML, read with stdlib `tomllib`, written via `tomli-w` or a small hand-rolled serialiser (the
schema is small and flat). Chosen over JSON (path-escaping pain) and YAML (third-party parser,
whitespace-fragile); TOML literal strings hold Windows paths verbatim.

```toml
version = 1              # for future migration

[settings]                # reserved: hash algo (03), global ignore (05), window geometry (07)

[[rule]]
id = "<uuid4>"            # generated at creation, immutable; join key to state.json
name = "cursor-rules skill"     # human label, freely editable, not required to be unique
master = 'C:\Users\Lloyd\.claude\skills\cursor-rules'
master_type = "dir"        # "dir" | "file" — set from the filesystem at creation, stored explicitly
replicas = [
  'C:\MyStuff\ProjectA\.claude\skills\cursor-rules',
  'C:\MyStuff\ProjectB\.claude\skills\cursor-rules',
]
ignore = []                 # reserved, unimplemented — see §5 Ignored paths
```

- **`id`**: UUID4, set once, never changes; the join key to `state.json` so name/master/replicas
  can change without orphaning sync history.
- **`master_type`**: stored, not re-derived at check time. A stored-vs-disk mismatch at check
  time is surfaced as an **error**, never auto-corrected.
- **Replica identity**: normalised absolute path — `os.path.normcase(os.path.normpath(os.path.abspath(p)))`.
  No separate replica id. Editing a replica's path = remove old + add new; the old last-sync
  snapshot is dropped and the new path is treated as first-time provisioning.
- **Uniqueness**: replica paths unique across *all* rules (two rules can't target one location);
  master paths unique across all rules (one rule per syncable unit — add replicas to fan a master
  out further). Enforced by GUI validation and by config-load validation, which rejects a
  conflicting file with an error naming the conflict. Exact-duplicate paths are blocked; nested /
  overlapping paths are allowed for now (full overlap detection deferred).
- **Config ↔ state boundary**: `config.toml` never records check/sync results. `state.json`
  holds *all* last-sync data, keyed by rule `id` + normalised replica path. Editing config never
  writes state directly.
- **Atomic writes**: serialise to `config.toml.tmp` in the same directory, then `os.replace()`.
  Combined with `backups/config-<timestamp>.toml` (last N kept, written on every save), a bad
  write is always recoverable.
- **Hand-editability**: `config.toml` is GUI-intended but plain TOML, tolerant of hand edits.
  Reloaded on startup and on a manual "Reload config" action (no live filesystem watch). If a
  GUI save would clobber external changes made since it last loaded, it warns first.

## 5. Check engine ([Check engine](issues/05-check-engine.md))

`check(rule, progress=None, cancel=None) -> CheckResult` — a plain, GUI-agnostic function. Never
writes anything (`CONTEXT.md`: a check reports "without changing anything").

### Walk & match

- Walk each side independently with `os.walk(top, followlinks=False)`, building
  `{rel_path: (size, mtime)}` per side. `rel_path` is POSIX-separated, relative to the master
  root (a single-file master's one entry is its filename).
- Match by `rel_path`, case-insensitive on Windows; the key preserves the master's actual casing
  for display and for `shutil.copy2`. A casing-only difference is the same file.
- The categorised path set is `keys(master) ∪ keys(replica) ∪ keys(baseline.files)`. Directories
  are not entities — only files are units of change; empty directories are neither drift nor
  foreign.
- Symlinks: a symlink **to a file** is followed (hashed/copied as its target, matching
  `shutil.copy2`'s default). A symlink **to a directory** is not recursed into and reports
  `unreadable`. Hidden files / dotfiles are not special-cased — included.
- Errors: read/permission/stat errors are caught per-path, collected as `unreadable`
  `FileChange`s with the exception text in `detail`; the check completes, never aborts.
  Directory-listing errors go in `ReplicaCheckResult.walk_errors`.

### Content comparison

- Size first — different size ⇒ different content, no hashing.
- Same size ⇒ SHA-256, streamed in 64 KB chunks (`hashlib`).
- **Re-hash skip**: if a side's current size **and** mtime both equal that side's baseline entry,
  reuse the stored baseline hash instead of re-reading the file. Size/mtime are advisory hints
  only, never trusted for a correctness call. This is the only performance lever built for v1
  (see §11).

### Ignored paths

A fixed, built-in exclusion list, applied symmetrically to master and replica:

```
.git/  .svn/  .hg/  __pycache__/  *.pyc  ~$*  Thumbs.db  desktop.ini  .DS_Store
```

`config.toml`'s `ignore` key stays reserved/unimplemented; per-rule configurable ignores are a
build-effort decision if real usage needs them.

### The complete per-file taxonomy

Internal `category` is a stable enum; the UI renders the plain-text label only — **no glyphs**
(the user does not want symbols to learn).

| `category` | condition (baseline B, master M, replica R) | UI label | Action bucket (§6) |
|---|---|---|---|
| `in_sync` | in M & R, M == R, (M == B or no B entry) | In sync | none |
| `converged` | in M & R, M == R, M ≠ B | In sync (baseline refreshed on next sync) | none |
| `new` | in M, not in R | Missing from replica | safe drift |
| `changed` | in M & R, M ≠ R, M ≠ B, R == B | Updated in master | safe drift |
| `diverged` | in M & R, M ≠ R, M == B, R ≠ B | Edited in this replica since last sync | conflict |
| `both_changed` | in M & R, M ≠ R, M ≠ B, R ≠ B | Changed in master and replica | conflict |
| `no_baseline` | in M & R, M ≠ R, path not in B | No record of a previous sync — can't compare | conflict |
| `master_deleted` | in B, not in M, R present | Deleted from master | master-deleted (own confirm) |
| `replica_only` | in R, not in M, not in B | Only in the replica | informational only, never checkable/auto-deleted |
| `unreadable` | IO/permission error, or a file/dir type mismatch at the same path | Couldn't read this file | none — display only, never checkable |
| `kept` | B is `kept`, M == B's `kept_master_hash`, R == B's `hash` | Kept replica's version | none — display only, never checkable |

Edge cases:
- **Locally-deleted replica file** (in B and M, absent from R) → `new`. Master wins uncontested;
  sync re-provisions it. Not treated as an intentional local delete.
- **`master_deleted` + replica also edited** (in B, not in M, R ≠ B) → promoted to `both_changed`,
  never a silent delete.
- **`converged`** reports as `in_sync` with an internal `baseline_stale` flag; the stale-but-equal
  entry is refreshed opportunistically the next time that replica syncs (even though the copy
  no-ops).
- **`kept`** entries compare each side against its own hash from keep time — M against
  `kept_master_hash` (absent master at keep time recorded as `None`), R against `hash` — rather
  than the ordinary M/R-vs-B rules. The moment M moves past its kept-time hash, the file re-enters
  ordinary drift (`changed`, safe drift): master wins, so no conflict is re-raised just because the
  user once chose to keep the replica.

### Return structure

```
CheckResult
  rule_id, rule_name
  master_type            "dir" | "file"
  master_missing         bool   # whole master root gone/unreadable — see §8
  replicas               [ ReplicaCheckResult ]

ReplicaCheckResult
  replica_path           normalised abs path
  replica_exists         bool
  has_baseline            bool
  files                   [ FileChange ]        # ALL files, in_sync included
  walk_errors             [ { path, message } ]  # couldn't enumerate a directory

FileChange
  rel_path                POSIX sep, master's casing
  category
  master_present, replica_present, baseline_present   bools
  master_size, replica_size                            int | null
  detail                  optional sentence, e.g. "master unreadable: PermissionError [Errno 13]"
```

`files` always includes `in_sync` entries — the UI needs them for roll-up totals and greyed
context rows; the engine returns no directory nodes, the UI builds its own roll-ups from the flat
list. A missing replica reports `replica_exists = False` and every master file as `new`. A
single-file master's `files` holds 0 or 1 entries.

### Responsiveness

`check()` takes optional `progress(done, total, current_path)` (called between files) and
`cancel() -> bool` (polled between files; returns a partial `CheckResult` if tripped). The
worker-thread (`QThread`) wiring is GUI-side (§9).

## 6. Divergence state model / `state.json` ([Divergence state model](issues/03-divergence-state-model.md), [Divergence UX](issues/06-divergence-ux.md))

After every sync, one **SHA-256 hash per file** (not two — a completed sync makes replica ==
master, so one baseline captures both) plus size/mtime as advisory re-hash-skip hints only.

```json
{
  "version": 1,
  "hash_algo": "sha256",
  "rules": {
    "<rule-uuid>": {
      "replicas": {
        "c:\\mystuff\\projecta\\.claude\\skills\\cursor-rules": {
          "last_sync": "2026-09-09T14:32:00Z",
          "files": {
            "SKILL.md":          { "hash": "a1b2…", "size": 4096, "mtime": 1757423520.0 },
            "references/api.md": { "hash": "c3d4…", "size": 812,  "mtime": 1757423519.0, "kept": true }
          }
        }
      }
    }
  }
}
```

- Flat file→hash map, no directory entries. Keys are relative POSIX-style paths preserving the
  master's actual casing; lookups are case-insensitive on Windows.
- Replica keys are the normalised absolute path (§4).
- `last_sync` is per replica, ISO-8601 UTC.
- A single-file master uses a one-entry `files` map keyed by the filename.
- Optional `kept` field (added by [Divergence UX](issues/06-divergence-ux.md)): set when the user
  chooses "keep replica's version" on a conflict; cleared automatically the next time that file's
  entry is overwritten by an ordinary drift-sync.
- `hash_algo` mismatch vs. the current default → treated as "no baseline" for that state file.
- `version` supports future migration (deferred — see §11).

### Write timing & consistency

- **Commit-at-end per replica, not per run.** New hashes are held in memory during a sync and
  written once per replica, immediately after that replica finishes, via
  `state.json.tmp` + `os.replace()`.
- **Interrupted sync** writes nothing for the in-progress replica — the previous baseline stands,
  so already-copied files show as `changed` (safe drift) next check and are re-copied harmlessly.
  Replicas that already finished in a multi-replica run keep their committed state.
- **Partial / subtree sync**: the baseline is merged — only entries for files actually copied are
  updated/added; everything else is untouched; `last_sync` is bumped.
- **"Skip" on a conflict**: the baseline entry is never written; the file is re-flagged as a
  conflict on every subsequent check, forever, until resolved. No snooze state.
- No rolling backups of `state.json` (unlike `config.toml`) — it's machine-generated and
  self-healing via atomic writes plus corrupt-file quarantine below.

### No baseline for a replica

Occurs on the first-ever check, a deleted `state.json`, or a `hash_algo` mismatch.

- Replica absent or empty → first-time provisioning, handled like any update.
- Replica has content but no baseline → the whole replica is `no_baseline`; every differing file
  needs explicit per-file confirmation; nothing from it is included in "Sync all safe changes".
- Corrupt `state.json` → renamed to `state.json.corrupt-<timestamp>`, processing starts from
  empty (every replica becomes no-baseline), a warning is surfaced.

## 7. Review-and-sync tree UI ([Review-and-sync tree UI](issues/04-review-and-sync-tree-ui.md))

Prototype (throwaway PySide6, reference for chrome/behavior, not shipped code):
[`prototype/review_prototype.py`](prototype/review_prototype.py) — run notes and the four
variants tried in [`prototype/README.md`](prototype/README.md); headless roll-up check in
[`prototype/_smoke.py`](prototype/_smoke.py). **Variant D** was chosen: two-pane, left = rule
list, right = `replica → folder → file` tree for the selected rule. The rule is the natural unit
of review — you check and sync one rule at a time.

### Layout

- **Left pane**: sync rules, each row showing name, master path, replica count, and an aggregate
  one-line status (`N to sync, M to delete, K conflict` / `in sync` / a distinct **master
  missing** status — §8).
- **Right pane**: for the selected rule, a tree with **each replica as a top-level branch**, then
  folders, then files. One rule reviewed/synced at a time; roll-up spans only that rule's
  replicas.
- Every leaf shows its category's plain-text label (§5's table) — no glyphs. Branch rows
  (replica, folder) show a rolled-up count, colour-grouped into the three action buckets below.

### Selection roll-up

- **One click = full tick + roll-down.** Clicking a branch ticks it and every syncable descendant;
  clicking a fully-ticked branch clears it and its descendants.
- **Partial is computed, never clicked.** A branch shows a tri-state dash only when some-but-not-
  all selectable descendants are ticked; Qt's `ItemIsUserTristate`/`ItemIsAutoTristate` are off,
  parent state is recomputed bottom-up after every change.
- Roll-up stops at the rule — no cross-rule "tick everything" here; that's "Sync all safe
  changes".

### The three action categories, kept apart

| Category | In the tree | In "Sync all safe changes" | Applied via |
|---|---|---|---|
| **Safe drift** (`new`, `changed`) | checkable, rolls up/down | included | overwrite from master on tick |
| **Master-deleted** (`master_deleted`) | checkable, distinctly flagged | excluded | its own itemised batch-confirm (§8) |
| **Conflict** (`diverged`, `both_changed`, `no_baseline`) | not checkable, display-only, "resolve →" | excluded | [Divergence UX](issues/06-divergence-ux.md) dialog only, never overwritten/skipped silently here |

"Sync all safe changes" ignores the current tick selection: it applies every `new` + `changed`
file across the rule (or across all rules from the main window), touching nothing else.

### Large trees

Default expansion reveals only subtrees containing drift; fully-in-sync folders/replicas collapse
into their parent's rolled-up summary. In-sync leaves show greyed for context when a folder is
expanded, never checkable. No virtualisation/lazy loading in v1 — `QTreeWidget` populated eagerly
is fine at the driving use cases' scale (see §11 for the deferred performance item).

## 8. Deletion, rename, and master-missing handling ([Deletion & rename handling](issues/08-deletion-and-rename-handling.md))

- **No rename detection in v1.** A master rename always falls through to two independent
  changes: old path → `master_deleted`, new path → `new`. Hash-based rename detection was
  considered and rejected (ambiguous with multiple same-content candidates; a renamed-and-edited
  file won't hash-match anyway) for a driving use case where renames are rare. Revisit post-v1 if
  usage shows otherwise.
- **`replica_only`** files are informational only: shown for visibility, never checkable, never
  auto-deleted (the tool never destroys a file it didn't put there).
- **`master_deleted` confirm**: ticked individually like safe drift, but applying a batch
  containing any ticked master-deleted files pops one dialog itemising every one of them
  (path + replica) with a single "Yes, delete N files" acknowledgment.
- **Emptied replica directory**: once every master-deleted file under it is confirmed and
  applied, the tool removes the now-empty directory silently, walking upward and removing newly-
  empty parents too — stopping at (never removing) the replica root.
- **Master missing** (whole master root gone/unreadable, not just individual files): the rule
  reports a distinct rule-level state and ordinary tick-and-sync is **blocked** for it — this
  prevents an unmounted drive or bad path from flooding every file into one one-click wipe. A
  per-rule, one-click **"I know the master is missing — unlock"** acknowledgment (not persisted —
  in-session only, so relaunching or re-checking re-blocks if still missing) drops the rule back
  into the normal tree, every file as ordinary `master_deleted`, going through the same
  batch-delete confirm above. If the master reappears, no special-casing is needed: existing
  edge cases (locally-deleted replica file → `new`) handle it.
- The left pane's rule-list status needs a distinct **master missing** indicator, separate from
  ordinary drift counts.

## 9. Divergence UX — resolving conflicts ([Divergence UX](issues/06-divergence-ux.md))

A conflict (`diverged`, `both_changed`, `no_baseline`) is never ticked in the review tree; it's
resolved through a dedicated dialog, opened two ways:

- **Per-file** — a "Resolve" affordance on the conflicted leaf, scoped to that file.
- **Per-replica** — "Resolve conflicts" on the replica branch opens the same dialog as a queue:
  resolving one file advances to the next unresolved conflict in that replica, closing when the
  queue empties or the user dismisses it.

One dialog, one action set, for all three categories — only the diff shown adapts:

| Category | Diff shown |
|---|---|
| `diverged` | replica vs. baseline |
| `both_changed` | two diffs: master vs. baseline, and replica vs. baseline |
| `no_baseline` | direct master vs. replica, with a "no record of a previous sync" callout |

**Diff fidelity**: a real line diff for text files. A binary file, or a text file over a size
threshold, falls back to metadata only ("Binary file — diff unavailable" / size note, plus
size/mtime per side) — no best-effort diff attempted.

### The three actions (each applies immediately on confirmation, not staged for a later Sync)

- **Overwrite from master** — copies master's content in; baseline updates to master's hash.
- **Skip for now** — pure no-op; baseline not written; re-flagged forever until resolved another
  way. No snooze/dismiss state.
- **Keep replica's version** — adopts the replica's current content as the new baseline and sets
  the persistent `kept` flag, so the tree keeps showing it as a deliberate departure from master
  rather than plain "In sync". The flag clears automatically once the master genuinely moves past
  the kept version — the file re-enters ordinary drift like any other file.

### Bulk resolution

Per-category bulk actions on the replica branch, scoped separately per category (not one blanket
"resolve everything"):

- `diverged`: "Keep all as-is" (marks every diverged file `kept`) and "Overwrite all from master".
- `both_changed`: the same pair, scoped to that category.
- `no_baseline`: **excluded from any bulk action** — always per-file.

A bulk overwrite requires its own confirmation ("This discards N local edits — overwrite from
master?"). A bulk keep needs no confirm — it discards nothing.

## 10. GUI rule management & first-run ([GUI rule management & first-run](issues/07-gui-rule-management.md))

### Main window

Two entry surfaces for every rule action (add, edit, delete, check): a persistent
toolbar/button row above or below the rule list, *and* a right-click context menu on a rule row.
Right pane is always the review tree (§7) for the selected rule.

### First run

No `config.toml` → the main window opens normally with an empty rule list and an inline
empty-state prompt in the right pane ("No sync rules yet — click **+ Add rule** to get started").
No onboarding wizard — first-run reuses the ordinary add-rule flow.

### Add rule (single modal, not a wizard)

- **Master**: two explicit buttons, "Choose file…" / "Choose folder…" — sets `master_type`
  unambiguously from which button was used.
- **Name**: pre-filled from the master's basename (extension stripped for a file), freely
  editable, not enforced unique.
- **Replicas**: add/remove list fed three ways — "Browse…" (one at a time), drag-and-drop of
  several at once (the common case), and a typed/pasted path for a not-yet-existing replica
  (validated only for "parent directory exists and is writable", never that the path itself
  exists).
- **Validation**: uniqueness conflicts (§4) surface as an inline error on the offending row, not
  a blocking modal.

### Add a replica to an existing rule

No separate lightweight action — reuse the same rule editor (`Edit`), replica list
pre-populated. Editing a replica's path is still remove-old + add-new (§4).

### Editing a rule

- **Master path is immutable after creation** (delete + recreate to point at a different
  master).
- Name is freely editable (join key is `id`).
- Removing a single replica from the editor purges just that replica's `state.json` entry
  immediately.

### Deleting a rule

Confirm dialog names the rule and states plainly that replica files are left untouched:
*"Delete '\<rule name\>'? This stops syncing it — replica files are left as-is. This can't be
undone."* Purges the rule's `state.json` entries immediately, same action.

### `state.json` reconciliation

Every config load (startup, and the manual "Reload config" action) reconciles `state.json`
against the freshly-loaded config: any entry (rule id + normalised replica path) no longer
present in `config.toml` is purged — one code path regardless of whether the rule/replica
disappeared via GUI delete, a hand-edit, or an external tool.

### Single-instance lock

`syncer.lock` (PID-based, in `base_dir`) blocks a second launch with a short dialog ("Syncer is
already running — check the existing window."), then the second instance exits — no attempt to
focus the first instance's window. **Stale-lock recovery is silent**: if the PID isn't a running
process, the new instance deletes the stale lock and proceeds, no confirmation prompt.

## 11. Sync application — the executor ([Sync application](issues/10-sync-application.md))

`sync(rule, applied_changes, progress=None, cancel=None)` — mirrors `check()`'s signature so the
GUI wires both to the same worker-thread pattern. Consumes a `CheckResult` and the set of changes
the review screen (or "Sync all safe changes", or a §9 resolution) deemed applicable. Divergence
resolution itself is not here — that's §9; this only applies changes already decided.

### Copy mechanics

- **New files**: `shutil.copy2()` straight to the final path; missing parent directories created
  first via `os.makedirs(exist_ok=True)`.
- **Overwrites**: write to a same-directory temp file `<filename>.syncer-tmp-<uuid8>`, then
  `os.replace()` onto the final path — same-directory is required for same-volume atomicity; the
  uuid8 suffix avoids collisions between concurrent operations. On success the rename is the
  cleanup; on any failure before the replace, a `finally` block best-effort removes the orphaned
  temp file.
- **Casing**: no active case-correction. A casing-only difference is already `in_sync` and not a
  copy target; casing only changes as a side effect of a real content copy. Revisit only if/when
  a POSIX build exists.

### Backups: none in v1

`backups/` stays reserved (ADR 0001) but unused — everything the executor applies was already
flagged safe drift or explicitly confirmed, so there's no ambiguity a pre-overwrite snapshot
would hedge against. Revisit only if real usage shows people want an undo button.

### Deletion mechanics

`os.remove()`, no backup. Directory pruning after the last file under it is removed walks
**upward**, removing each newly-empty parent in turn, stopping at (never removing) the replica
root. Matches §8.

### Ordering

Within one replica's batch: **copies before deletes** (a failure partway leaves the replica more
complete, never emptier). Across replicas, and across rules for a cross-rule "Sync all safe
changes": **strictly sequential**, no parallelism.

### Partial-failure handling

**Continue and collect errors.** A throwing copy/delete does not stop the run; every other file
in the batch is still attempted. Failures are collected (path + message) for the summary and log.
A failed file's baseline entry is simply never written, so it keeps its pre-sync category next
check — no rollback needed, every operation is independently idempotent.

### `logs/` output

One plain-text log per run: `logs/sync-<ISO8601-timestamp>.log`. One line per file operation
(path, action, outcome), plus a trailing summary line (counts by outcome, error count, duration).
This is the only durable record of what happened in a run — `state.json` holds only the
resulting baseline.

### `state.json` write timing

Committed once per replica, immediately after that replica finishes (§6) — not buffered for the
whole run, so an interrupted multi-replica "Sync all safe changes" keeps every replica that
already finished.

### User-visible feedback

After a run completes, a result summary dialog shows counts by outcome (copied, deleted,
skipped-on-error) plus a scrollable error list (path + message); per-file success detail isn't
repeated since it's already in the log.

## 12. Domain model additions

`CONTEXT.md` (already updated across the effort) gained, beyond the original master / replica /
sync rule / check / sync / drift / divergence: **safe drift**, **converged**, **changed on both
sides**, **conflict**, **resolve**, **kept**, **master missing**. `replica_only` and the finer
internal category names (`new`, `no_baseline`, `master_deleted`, `in_sync`) stay
implementation-level, not glossary terms.

One ADR exists: [ADR 0001 — portable, self-contained storage](../../docs/adr/0001-portable-self-contained-storage.md).
No other decision in this effort cleared the bar (hard-to-reverse + surprising-without-context +
a genuine trade-off) that would warrant one.

## 13. Deliberately deferred to the build effort

Fog items that stayed open, or were narrowed but not settled, when this map reached its
destination:

- **Packaging & run model** — plain script vs. PyInstaller one-folder build (§3 rules out a
  one-file exe; the final call is still open).
- **Performance strategy for large folders** — the only v1 lever is the check engine's
  size+mtime re-hash skip (§5) and a `progress`/`cancel`-hook shape ready for a worker thread.
  Threaded hashing, incremental walk, and tree virtualisation beyond eager `QTreeWidget` (§7)
  are all deferred until real usage shows the driving use cases (skill folders, one resume file)
  actually need them.
- **Config / state versioning and migration** — both files carry a `version` field (§4, §6) for
  this, but the migration mechanism itself is unspecified.

## 14. Out of scope (this effort and this tool, not just deferred)

- Real-time / background watching of masters — on-demand check only.
- Reverse or bidirectional sync (editing a replica and pushing back to the master).
- Three-way merge of conflicting edits.
- Non-Windows support (macOS, Linux) for v1.
- Network / cloud sync.
- Git operations (pulling the upstream Matt Pocock skill repo) — done by hand by the user.
