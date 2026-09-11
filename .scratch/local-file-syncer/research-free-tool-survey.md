# Research: Free tools for one-way fan-out sync with review

Ticket: [01-free-tool-survey](issues/01-free-tool-survey.md)
Date: 2026-09-09

## The question

Does a free (not trial), fully-offline, Windows-compatible tool already do what this
effort wants:

- one **master** fanning out **one-way** to **many replicas** at explicit absolute paths;
- replicas grouped as reusable **sync rules**, each rule taking its own subset;
- an **on-demand check** (no watching);
- a **review-and-confirm** step at file granularity before anything is written;
- **propagated deletions** shown as a separate, separately-confirmed category;
- **divergence** flagging: a replica edited locally since its last sync is called out
  distinctly from ordinary "files differ", before it is overwritten.

If no single tool does all of this, is there a free Python library that can be the
check/copy engine, so only the GUI + rule model needs building?

## Scoring dimensions

| # | Dimension |
|---|-----------|
| A | Free (not a time-limited trial) |
| B | Fully offline (no account, no network peer, no cloud) |
| C | One -> many fan-out as a first-class idea, vs pairwise only |
| D | Reusable rule/group with per-rule subset |
| E | On-demand check (vs continuous watcher) |
| F | File-granularity review-and-confirm UI (vs fire-and-forget / log dump) |
| G | Deletions surfaced as their own confirmable category |
| H | Divergence: "replica changed since last sync" flagged distinctly from "differs" |

## Tools assessed

### FreeFileSync 14.x

- **A - Free.** Genuinely free, no trial, no nag screen, ad-free since v10. The paid
  "Donation Edition" only adds convenience extras (auto-updater, parallel copy, email
  notification, portable ZIP); nothing needed here is behind the paywall.
  Source: <https://freefilesync.org/faq.php>
- **B - Offline.** Runs fully offline; offline manual ships in the install folder.
  Source: <https://freefilesync.org/faq.php>
- **C - Partial.** No first-class "one source, many targets" object. You approximate
  fan-out by adding multiple **folder pairs** that all use the same left (source) folder;
  the source is scanned only once and the pairs run sequentially. It works, but the
  data model is still a flat list of pairs, not "one master -> N replicas".
  Sources: <https://freefilesync.org/forum/viewtopic.php?t=10218>,
  <https://freefilesync.org/forum/viewtopic.php?t=7730>
- **D - Weak.** No named, reusable rule grouping. Per-replica subsets are possible only
  by giving each folder pair its own include/exclude filter. There is no "this skill
  folder, these five projects, that project takes a subset" grouping - you would hand-
  maintain a filter per pair.
- **E - Yes.** Compare is an explicit button; sync is a separate explicit button.
- **F - Good, with caveats.** After Compare, the main grid lists every file with its
  category and proposed direction. You can right-click rows to exclude them or override
  direction before syncing. Before sync runs it shows a summary dialog (N to copy, M to
  delete). It is a grid, not an expandable per-node tree with roll-up selection, and the
  final confirm is a count, not per-file checkboxes - but file-level control does exist.
- **G - Partial.** Deletions appear as their own category (own icon/colour) in the grid
  and are counted separately in the pre-sync dialog. There is an optional "warn if a
  significant difference is detected" (large fraction of files changed) and a "warn if a
  database file is missing". There is no dedicated "confirm these deletions on their own"
  step distinct from the rest of the sync.
  Sources: <https://freefilesync.org/manual.php?topic=synchronization-settings>,
  <https://freefilesync.org/forum/viewtopic.php?t=12976>
- **H - This is the real gap.** In **Mirror** mode FreeFileSync does not use the
  `sync.ffs_db` database to reason about "the target changed since last sync"; it
  compares master vs replica directly and, if the replica file is newer, still shows it
  as "differs, will be overwritten left -> right" with no distinct divergence flag and no
  warning. Community requests for a "you are about to overwrite a newer file" warning
  have not been implemented.
  **Update** mode can enable "use database to detect changes", which does prevent
  overwriting a target that is newer than the source - closer to what we want - but it
  is still surfaced as a generic conflict, not as "you edited this replica", and Update
  mode by design does not propagate deletions (a hard requirement here).
  A file-content comparison mode exists but only yields the "conflict (same date,
  different size)" category, again generic.
  Sources: <https://freefilesync.org/manual.php?topic=comparison-settings>,
  <https://freefilesync.org/forum/viewtopic.php?t=10815>,
  <https://freefilesync.org/forum/viewtopic.php?t=4127>,
  <https://freefilesync.org/manual.php?topic=synchronization-variants> (mirror = "exact
  copy of the source", no last-sync reasoning documented)

Verdict: closest of any tool. Fails C/D (no rule model, no first-class fan-out) and H
(no divergence concept in the mode that also does deletions). Adopting it means giving
up the rule model and the divergence guarantee, which are the two things the effort
exists to provide.

### Syncthing 2.x

- **A - Free** (MPL-2.0, open source).
- **B - Fails.** Syncthing is a peer-to-peer sync daemon. Even for a single physical
  machine you would need two folders on two device IDs and a running connection between
  them; it is built around a cluster of devices, not "these absolute paths on this PC".
  Source: <https://docs.syncthing.net/users/foldertypes.html>
- **C - Yes** in spirit (one shared folder -> many devices).
- **D - No** rule/subset model; each device gets the whole shared folder.
- **E - Fails.** Continuous real-time scanning + watching, not on-demand. Watching the
  masters is explicitly out of scope for this effort.
- **F - Fails.** No review-and-confirm. "Send Only" folders ignore inbound changes and
  offer an "Override Changes" button that force-pushes the local state cluster-wide with
  no per-file review.
  Source: <https://docs.syncthing.net/users/foldertypes.html>
- **G - No.** H - No (it has out-of-sync detection but no confirm-before-overwrite).

Verdict: wrong shape entirely - a networked daemon, continuous, no review.

### Unison 2.53

- **A - Free** (GPL). **B - Offline** (can sync two local roots on one host).
- **C - Fails.** Fundamentally **pairwise**: two roots per profile. "Larger groups" are
  documented only as "multiple pairwise synchronizations" - i.e. N hand-written profiles.
  Source: <https://manpages.ubuntu.com/manpages/xenial/man1/unison-2.48.1.html>
- **D - No.** A profile is a pair, not a reusable rule with a replica list.
- **E - Yes** (explicit run).
- **F - Yes, genuinely good.** The GTK GUI lists every changed item with a proposed
  direction; the user can flip direction per item and then commit. `-confirmbigdel`
  prompts on whole-path deletes.
- **G - Partial** (big-delete confirmation only).
- **H - Yes for the pair case.** Unison keeps an archive (state from last sync) and
  detects "both replicas changed" as a conflict. One-way is forced with `-force <root>`
  or `-prefer`. But this is per-pair; there is no fan-out.

Verdict: has the review UI and the last-sync-state idea, but pairwise-only kills it for
fan-out, and the Windows GTK build is clunky to run and to embed in anything.

### robocopy / `ROBOCOPY /MIR` + scripting

- **A - Free**, built into Windows. **B - Offline.**
- **C - Fails.** One source, one destination per invocation. Fan-out = a scripted loop.
- **D - No** (whatever you script).
- **E - Yes** (you run the script).
- **F - Fails.** `/L` gives a list-only dry run, but that is a log dump to review by eye,
  not an interactive per-file confirm. `/MIR` itself never prompts.
  Source: <https://learn.microsoft.com/> robocopy reference; community consensus that
  `/MIR` has no confirmation and `/L` is the only safety net.
- **G - Fails.** Deletions from `/MIR` are not a separate confirmable category; they are
  interleaved in the log.
- **H - Fails.** robocopy compares size + timestamp (or `/XO`), has no memory of a prior
  sync, so it cannot tell "replica was edited" from "master moved on".

Verdict: a copy primitive, not a solution. Could be shelled out to as the copy step, but
Python's `shutil` is a better fit for a Python app.

### rsync on Windows (cwRsync Free Edition, MSYS2 rsync, rsync.net client)

- **A - Free** (cwRsync Free Edition is GPL rsync repackaged). **B - Offline** (local
  paths work).
  Source: <https://community.chocolatey.org/packages/rsync>
- **C - Fails.** One source -> one dest per run; fan-out = scripted loop.
- **D - No.**
- **E - Yes.**
- **F - Fails.** `--dry-run --itemize-changes` gives an excellent textual preview, and
  `--checksum` gives content-based comparison, but there is no interactive confirm - you
  read the itemized output, then re-run without `--dry-run`.
  Source: <https://linux.die.net/man/1/rsync>
- **G - Fails.** `--delete` removals show in the itemized list (`*deleting`) but are not
  a separately-confirmed category.
- **H - Fails.** Like robocopy, rsync makes the destination match the source; it has no
  last-sync archive, so no divergence concept. (Unison, by the same author, added the
  archive precisely because rsync lacks it.)

Verdict: strong preview and content comparison, but no rule model, no review UI, no
divergence, pairwise only.

### Beyond Compare - free tier

- **A - Fails.** There is **no free tier**. 30-day trial, then a paid licence
  (Standard ~USD 35, Pro ~USD 70). After the trial it stops.
  Source: search consensus; scootersoftware pricing.

Not free -> out.

### SyncToy 2.1 (Microsoft)

- **A - Free.** **B - Offline.**
- **Fatal: abandoned and unavailable.** Discontinued; official download pulled by
  Microsoft in January 2021; not supported on Windows 11 (needs .NET 3.5 + compatibility
  mode); only obtainable from third-party mirrors.
  Source: <https://learn.microsoft.com/en-us/answers/questions/4344873/synctoy-and-windows-11>
- **C - Partial** (multiple folder pairs, same left). **D - No.** **E - Yes.**
- **F - Weak.** "Preview" lists actions before Run; no per-file confirm tree.
- **G - No.** **H - No** (Echo mode compares attributes, no last-sync divergence).

Verdict: dead software; do not build on it.

### Bvckup 2 - free tier

- **A - Fails.** **No free tier.** 2-week trial, then unlicensed mode: backups are
  auto-disabled after each run and the window cannot be minimized. Paid product.
  Source: <https://bvckup2.com/terms/>

Not free -> out. (It is one-way mirror with delta copy and would otherwise be
interesting, but it is pairwise, has no review-and-confirm, and no rule model.)

## Python libraries as a check/copy engine

The map already fixes: Python + PySide GUI, content-based comparison, human-readable
config file + separate state file, on-demand check, divergence as a distinct category.
So the question is narrow: does a library remove the check-engine or copy-engine work
(ticket 05)?

### `dirsync` (tkhyn/dirsync, MIT)

- One-way directory sync; `--sync`, `--update`, `--diff` actions; `--content` for
  content comparison; `--purge` for delete propagation; `diff` reports without changing
  anything; importable as `from dirsync import sync`.
  Source: <https://github.com/tkhyn/dirsync>
- **Gaps for us:** no concept of a last-sync snapshot, so it **cannot compute
  divergence** - it only knows master-vs-replica right now. Its reporting is delivered
  through a `logging` logger, not a structured result object, so feeding a review tree
  from it means parsing log lines. It also owns the copy loop, so you inherit its
  choices. It would save maybe a day of walk/copy code while making the divergence and
  review-tree work harder.

### `filecmp` (Python standard library)

- `filecmp.dircmp(a, b, shallow=False)` recursively yields `left_only`, `right_only`,
  `diff_files`, `same_files`, `common_funny`, `funny_files`, and `subdirs` for recursion
  - i.e. exactly the master-vs-replica content diff, with **zero dependencies**.
  `shallow=False` forces real content comparison.
  Source: <https://docs.python.org/3/library/filecmp.html>
- Recursion is manual (walk `dircmp.subdirs`), and `dircmp` caches results, which is fine
  for an on-demand check. This is the natural backbone of the check engine.

### `dirtools` / `dirtools2` (tsileo/dirtools, MIT)

- `Dir`, `Dir.hash()` (hash of per-file hashes), and crucially `DirState`: snapshot a
  directory, serialise to JSON, later diff two states to get
  `{created, deleted, deleted_dirs, updated}`.
  Source: <https://github.com/tsileo/dirtools>,
  <https://dirtools.readthedocs.io/en/latest/>
- This is a working model of the **state file** ticket 03 needs. Worth reading as prior
  art, but the package is old and lightly maintained (`dirtools2` is the py3 fork), so
  vendoring the idea (a JSON map of relative path -> content hash) is safer than adding
  the dependency.

### `watchdog`

- Filesystem **event** monitoring. The effort explicitly rules out watching masters
  (on-demand check only). Not relevant.

### `pyrsync2` / `librsync` bindings

- Delta-transfer algorithms for sending diffs over a wire. Overkill for local copies;
  `shutil.copy2` is the right primitive locally.

## Conclusion

No free, non-trial, offline, Windows tool covers the destination. Every candidate fails
on at least one of: not actually free (Beyond Compare, Bvckup 2), wrong shape (Syncthing
is a networked continuous daemon), pairwise-only with no rule model (Unison, robocopy,
rsync), or - for the one strong contender, FreeFileSync - **no first-class one-master ->
many-replica rule model with per-rule subsets, and no divergence concept in the sync
mode that also propagates deletions.** Divergence flagging (H) and the reusable rule
model (C/D) are precisely the reasons this effort exists, and they are exactly what the
field does not give you for free.

On the library side, no package removes the hard part. The check engine is thin on top
of the standard library: `os.walk` + `hashlib` (content hashing / state file) +
`filecmp.dircmp(shallow=False)` (recursive master-vs-replica diff) + `shutil.copy2` /
directory creation (the copy step). Divergence is then a small custom step: compare the
current replica against the stored last-sync hash map. `dirsync` is the nearest
off-the-shelf engine but its lack of last-sync state and its log-based reporting mean it
would complicate the divergence and review-tree work rather than simplify it.
`dirtools.DirState` is a useful design reference for the state file but not worth taking
as a dependency.

### Recommendation: (c) build from scratch

Build the tool as planned. Use the Python standard library as the engine toolkit
(`filecmp`, `hashlib`, `os.walk`, `shutil`) rather than adopting a third-party sync
library; treat `dirtools.DirState` as prior art for the ticket 03 state file. Do not
adopt FreeFileSync - it is the only tool worth a second look, but bending it to this
model means losing the rule model and the divergence guarantee, and it cannot be
embedded in or driven by the intended PySide app anyway.

The destination in `map.md` stands as written; no redraw is needed. Downstream tickets
02 (sync-rule config model), 03 (divergence state model) and 04 (review-and-sync tree
UI), all "Blocked by: 01", are now unblocked and proceed as originally scoped. Ticket 05
(check engine) should note that the engine is standard-library-based, not a wrapped
dependency.

## Sources

- FreeFileSync FAQ (free vs Donation Edition, offline, Windows): <https://freefilesync.org/faq.php>
- FreeFileSync manual - synchronization settings: <https://freefilesync.org/manual.php?topic=synchronization-settings>
- FreeFileSync manual - comparison settings: <https://freefilesync.org/manual.php?topic=comparison-settings>
- FreeFileSync forum - one source to multiple targets: <https://freefilesync.org/forum/viewtopic.php?t=10218>, <https://freefilesync.org/forum/viewtopic.php?t=7730>
- FreeFileSync forum - both sides changed / conflict handling: <https://freefilesync.org/forum/viewtopic.php?t=10815>
- FreeFileSync forum - overwriting newer files, no warning: <https://freefilesync.org/forum/viewtopic.php?t=4127>
- FreeFileSync forum - Mirror vs Update / deletion risk: <https://freefilesync.org/forum/viewtopic.php?t=12976>
- FreeFileSync licensing (Donation Edition, private use): <https://freefilesync.org/faq.php>, <https://github.com/hkneptune/FreeFileSync>
- Syncthing folder types (Send Only / Receive Only / Override / Revert): <https://docs.syncthing.net/users/foldertypes.html>
- Unison man page (pairwise, multiple pairwise syncs, GUI, confirmbigdel): <https://manpages.ubuntu.com/manpages/xenial/man1/unison-2.48.1.html>
- Unison ArchWiki (Windows, GUI usage): <https://wiki.archlinux.org/title/Unison>
- robocopy /MIR + /L behaviour (no confirmation, list-only dry run): howtogeek / PDQ / petri robocopy guides
- rsync man page (--dry-run, --itemize-changes, --delete, --checksum): <https://linux.die.net/man/1/rsync>
- cwRsync Free Edition (GPL, Windows): <https://community.chocolatey.org/packages/rsync>
- Beyond Compare - trial only, no free tier: scootersoftware pricing / search consensus
- SyncToy discontinued, download pulled Jan 2021, not on Win11: <https://learn.microsoft.com/en-us/answers/questions/4344873/synctoy-and-windows-11>
- Bvckup 2 terms - 2-week trial, unlicensed mode disables backups: <https://bvckup2.com/terms/>
- dirsync (MIT, actions, --content, --purge, --diff, importable): <https://github.com/tkhyn/dirsync>
- Python filecmp.dircmp / cmpfiles: <https://docs.python.org/3/library/filecmp.html>
- dirtools / DirState (hash, created/deleted/updated, JSON state): <https://github.com/tsileo/dirtools>, <https://dirtools.readthedocs.io/en/latest/>
