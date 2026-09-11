# Check engine — how drift is computed

Parent: [Map: Local file syncer](../map.md)
Type: grilling
Status: resolved
Blocked by: 03

## Question

How does a check turn (master, replica, last-synced snapshot) into a categorised list of changes?

Decide: the directory-walk approach (recursive listing of master and replica); how files are matched between the two sides; the content comparison (full hash vs. size-then-hash vs. byte compare) and when it can be skipped; the exhaustive set of per-file outcomes (in sync / new in master / deleted in master / changed in master / diverged in replica / changed on both sides); how symlinks, hidden files, empty directories, and permission errors are handled; whether any paths are ignored (e.g. `.git`, editor cruft) and whether that's configurable per rule; and what the check returns as a data structure for the UI ([Review-and-sync tree UI](04-review-and-sync-tree-ui.md)) to render.

Rename detection is out of scope here — see [Deletion & rename handling](08-deletion-and-rename-handling.md). Consult `CONTEXT.md`.

Per [ticket 01](01-free-tool-survey.md), the engine is standard-library-based (`os.walk` + `hashlib`, `filecmp.dircmp(shallow=False)`, `shutil.copy2`), not a wrapped third-party dependency; `dirtools.DirState` is prior art worth reading but not a dependency.

## Answer

### `filecmp.dircmp` is dropped (correction to ticket 01 / map Notes)

Ticket 01's answer and the map's settled constraints both name `filecmp.dircmp(shallow=False)` as the recursive diff mechanism. That does not fit the model [03](03-divergence-state-model.md) established: every file is judged **three-way** against `(baseline B, master-now M, replica-now R)`, and `dircmp` is a two-way (left-tree vs right-tree) comparison with nowhere to put B, no content hashes returned (we need them for `state.json` regardless), and awkward recursion / `funny_files` handling. **`dircmp` is not used.** Ticket 01's answer, the map's decision line, and `CLAUDE.md`'s settled-constraints restatement are corrected to `os.walk` + `hashlib` + `shutil.copy2` only.

### Walk & match

- **Walk each side independently** with `os.walk(top, followlinks=False)`, building `{rel_path: (size, mtime)}` for master and for replica. `rel_path` is POSIX-separated (`/`), relative to the master root (single-file master → the one filename).
- **Match by `rel_path`.** Lookups are case-insensitive on Windows (per [03](03-divergence-state-model.md)); the key retains the master's actual casing for display and `copy2`. A replica file differing from the master only in case is the same file (a sync corrects the casing).
- The set of paths to categorise is `keys(master) ∪ keys(replica) ∪ keys(baseline.files)`. Directories are **not** entities — only files are units of change (matches [03](03-divergence-state-model.md)'s flat file map). Empty directories on either side are neither drift nor foreign; whether an emptied master directory removes the replica directory is [08](08-deletion-and-rename-handling.md)'s call.

### Content comparison

- **Size first.** Different size ⇒ different content, no hashing.
- **Same size ⇒ SHA-256**, streamed in 64 KB chunks (`hashlib`, stdlib per [01](01-free-tool-survey.md)).
- **Baseline re-hash skip:** if a side's current `size` **and** `mtime` both equal its baseline `files[rel_path]` entry, reuse the stored baseline hash instead of re-reading the file. Size/mtime are advisory hints only (per [03](03-divergence-state-model.md)) — never trusted for a correctness call, only to avoid a read.
- Comparison is `master_hash == replica_hash` (and each vs `baseline_hash`). This is the only perf lever built in for v1; anything more (threaded hashing, incremental walk) stays in the map's "performance strategy for large folders" fog item for the build effort.

### The complete per-file taxonomy

Internal `category` enum (stable identifier for code); the UI renders a plain-text phrase, **no glyphs** (correction to [04](04-review-and-sync-tree-ui.md), which specified a glyph set — the user does not want symbols to learn). Colour-grouping into the three action buckets from [04](04-review-and-sync-tree-ui.md) stays.

| `category` | condition | UI label | [04](04-review-and-sync-tree-ui.md) bucket |
|---|---|---|---|
| `in_sync` | in M & R, M == R, (M == B or no B entry) | In sync | none |
| `converged` | in M & R, M == R, M ≠ B | In sync | none (baseline stale — see below) |
| `new` | in M, not in R (whether or not in B) | Missing from replica | safe drift |
| `changed` | in M & R, M ≠ R, M ≠ B, R == B | Updated in master | safe drift |
| `diverged` | in M & R, M ≠ R, M == B, R ≠ B | Edited in this replica since last sync | conflict |
| `both_changed` | in M & R, M ≠ R, M ≠ B, R ≠ B | Changed in master and replica | conflict |
| `no_baseline` | in M & R, M ≠ R, path not in B | No record of a previous sync — can't compare | conflict |
| `master_deleted` | in B, not in M, R present | Deleted from master | master-deleted (own confirm) |
| `replica_only` | in R, not in M, not in B | Only in the replica | deferred to [08](08-deletion-and-rename-handling.md) |
| `unreadable` | IO/permission error hashing or stat-ing either side, or a file/dir type mismatch at the same path | Couldn't read this file | none — display only, never checkable |

**Edge-case calls:**
- **Locally-deleted replica file** (path in B and M, absent from R) → `new`. Master wins uncontested; the sync re-provisions it. The local delete is not treated as intentional.
- **`master_deleted` where the replica also edited the file** (path in B, not in M, R ≠ B) → promoted to `both_changed` (conflict), never a silent delete.
- **`converged`** is reported as `in_sync` with an internal `baseline_stale` flag. The **check never writes** (`CONTEXT.md`: a check reports "without changing anything"). The stale-but-equal baseline entry is refreshed opportunistically on the next sync of that replica (a "sync all safe" pass rewrites the entry even though the copy no-ops).

### Ignored paths

A **fixed, built-in exclusion list**, applied symmetrically to both master and replica (so a replica's own `.git/` is not flagged `replica_only`):

```
.git/  .svn/  .hg/  __pycache__/  *.pyc  ~$*  Thumbs.db  desktop.ini  .DS_Store
```

The `ignore` key [02](02-sync-rule-config-model.md) reserved in `config.toml` stays reserved / unimplemented — per-rule configurable ignores are a build-effort decision if the driving use cases need them.

### Symlinks, errors

- `os.walk(followlinks=False)`. A symlink **to a file** is followed — hashed and (later) copied as its target content, matching `shutil.copy2`'s default. A symlink **to a directory** is not recursed into and is reported `unreadable` with a note (avoids loops). Windows, so rare.
- **Hidden files** (dotfiles or the Windows hidden attribute) are not special-cased — included. Dotfiles like `.claude/` matter.
- **Read / permission / stat errors** are caught per-path, collected as `unreadable` `FileChange`s with the exception text in `detail`, and the check **completes** — never fatal. Directory-listing errors (can't enumerate a folder) go in `ReplicaCheckResult.walk_errors`.

### Return structure

```
CheckResult
  rule_id, rule_name
  master_type            "dir" | "file"
  replicas               [ ReplicaCheckResult ]

ReplicaCheckResult
  replica_path           normalised abs path (per 02)
  replica_exists         is anything present at that path at all
  has_baseline           bool
  files                  [ FileChange ]        # ALL files, in_sync included
  walk_errors            [ { path, message } ] # couldn't enumerate a directory

FileChange
  rel_path               POSIX sep, master's casing
  category               one of the enum above
  master_present, replica_present, baseline_present   bools
  master_size, replica_size                           int | null
  detail                 optional sentence (e.g. "master unreadable: PermissionError [Errno 13]")
```

- **All files are returned, `in_sync` included** — [04](04-review-and-sync-tree-ui.md) greys in-sync leaves for context and needs roll-up totals ("42 files, 3 changed"). The UI filters; it builds its own folder/replica roll-ups from this flat list (the engine returns no directory nodes).
- **Scope:** the primitive is `check(rule) → CheckResult` covering all of that rule's replicas. The main window's "check everything" loops rules and collects a `list[CheckResult]`.
- **A missing replica** → `replica_exists = False`, `files` categorises every master file as `new` (first-time provisioning, a settled constraint).
- **Single-file master** → `files` holds 0 or 1 `FileChange`.

### GUI responsiveness

`check()` is a plain, GUI-agnostic function taking two optional arguments: `progress(done, total, current_path)` called between files, and `cancel() -> bool` polled between files (returns a partial `CheckResult` if it trips). The worker-thread / `QThread` wiring that uses them is [07](07-gui-rule-management.md)'s job.

### New ticket spun off

Nothing in the map owns **how a ticked change is executed** — copy mechanics, pre-overwrite backup into `backups/` (ADR 0001 provisions the dir, no ticket fills it), deletion mechanics, `logs/` output, partial-failure recovery mid-"sync all". [05](05-check-engine.md) is read-only; [04](04-review-and-sync-tree-ui.md) is the UI; [06](06-divergence-ux.md) is divergence resolution; [08](08-deletion-and-rename-handling.md) is deletion *semantics*, not execution. → new ticket **[Sync application — how ticked changes are executed](10-sync-application.md)**, blocked by 05 + 08, ahead of 09.

### Domain model

`CONTEXT.md` gains three terms this taxonomy fixes (flagged as candidates by [03](03-divergence-state-model.md) and [04](04-review-and-sync-tree-ui.md)): **safe drift**, **converged**, **changed on both sides**. The finer states (`new`, `no_baseline`, `master_deleted`, `replica_only`) are implementation-level category names, not glossary concepts. No ADR — the one convention-breaking call (dropping `dircmp`) is a correction fully explained by [03](03-divergence-state-model.md)'s three-way model, recorded above and in the corrected tickets.
