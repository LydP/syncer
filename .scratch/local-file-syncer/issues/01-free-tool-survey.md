# Survey free tools for one-way fan-out sync with review

Parent: [Map: Local file syncer](../map.md)
Type: research
Status: resolved
Blocked by: none

## Question

Does a free, offline, Windows-compatible tool already cover the destination — one master fanning out one-way to many replicas at explicit paths, grouped as reusable rules, with an on-demand check, a review-and-confirm step at file granularity, propagated deletions shown separately, and flagging of locally-modified replicas before overwrite?

Assess (at least): FreeFileSync, Syncthing, Unison, robocopy/`ROBOCOPY /MIR` + scripting, rsync-on-Windows, Beyond Compare (free tier only), SyncToy, Bvckup 2 free tier, and any Python library that provides a diff/copy engine (e.g. `dirsync`, `watchdog`+custom, `rsync`-style libs).

For each: is it free (not trial)? Fully offline? Does it do one→many fan-out or only pairwise? Does it support per-rule subsets? Does it flag divergence (replica changed since last sync) vs. just "files differ"? Does it have a review UI or is it fire-and-forget?

Deliver a recommendation on one of: (a) adopt tool X as-is — redraw the destination; (b) build, but reuse library Y as the check/copy engine; (c) build from scratch. Capture findings as a Markdown file in this effort directory and link it from the answer.

## Answer

**Recommendation: (c) build from scratch.** Full findings: [research-free-tool-survey.md](../research-free-tool-survey.md).

No free (non-trial), fully-offline, Windows tool covers the destination. Beyond Compare and Bvckup 2 are trial-only, not free. Syncthing is a networked, continuous peer-to-peer daemon with no review-and-confirm step — the wrong shape. Unison, robocopy `/MIR`, and rsync-on-Windows are all pairwise one-source-one-dest with no reusable rule model; Unison alone keeps last-sync state (divergence) but only per pair and has no fan-out. SyncToy is discontinued and unavailable on Windows 11. FreeFileSync is the only serious contender — genuinely free, offline, with a decent file-level review grid and a separate deletion category — but it has no first-class one-master-to-many-replicas rule model with per-rule subsets, and its Mirror mode (the mode that also propagates deletions) has no divergence concept: it silently overwrites a locally-edited replica with no distinct flag or warning. Divergence flagging and the reusable rule model are exactly why this effort exists, so adopting FreeFileSync means giving up its two core goals, and it cannot be embedded in the intended PySide app regardless. On the library side, no package removes the hard part: the check engine is thin on top of the standard library (`os.walk` + `hashlib` for content hashing and the state file, `filecmp.dircmp(shallow=False)` for the recursive master-vs-replica diff, `shutil.copy2` for the copy step), with divergence a small custom comparison against the stored last-sync hash map. `dirsync` is the nearest off-the-shelf engine but its lack of last-sync state and log-based reporting would complicate rather than simplify; `dirtools.DirState` is useful prior art for the ticket 03 state file but not worth a dependency. The `map.md` destination stands as written — no redraw needed. Tickets 02, 03, and 04 are unblocked and proceed as scoped.

> **Correction ([ticket 05](05-check-engine.md)):** this answer named `filecmp.dircmp(shallow=False)` as the recursive master-vs-replica diff. Ticket 05 dropped it — `dircmp` is a two-way comparison and cannot express the three-way `(baseline, master-now, replica-now)` model [ticket 03](03-divergence-state-model.md) established, nor return the content hashes needed for `state.json`. The engine is `os.walk` + `hashlib` + `shutil.copy2` only. The "thin on top of the standard library" conclusion is unchanged.
