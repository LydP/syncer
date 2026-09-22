# Syncer

A general-purpose, offline, Windows-only tool that keeps copies of files and folders current with a single canonical original. It exists because maintaining the same content (Claude Code skill folders, a resume) across many project directories by hand is error-prone.

## Language

**Master**:
The canonical copy of a file or folder — the one source of truth that all other copies are made to match. A master path may be listed by more than one sync rule.
_Avoid_: source, original, home copy

**Replica**:
A destination path that receives one or more masters' content, kept in sync one-way from those masters. A folder-type master lands under a subfolder of the replica named for the master's own basename (so a rule's several masters don't collide); a file-type master lands directly at the replica path plus its filename. A replica path may be listed by more than one sync rule.
_Avoid_: destination, mirror, target, copy

**Replica name**:
An optional, human-chosen label for a replica path, shown in place of the path wherever the replica appears (the path stays reachable). Belongs to the path itself, not to a rule: every rule that lists the replica shows the same name. Unique across all replicas, ignoring case; a replica with no name is shown by its path.
_Avoid_: alias, nickname, label

**Landing path**:
Where one master's content comes to rest inside a replica, as a path relative to the replica root: a folder-type master's `<basename>/` subfolder and everything beneath it, or a file-type master's bare filename. Computed from configuration alone, with no filesystem read. It is the key every per-file comparison is made against, so a replica's drift report doesn't care which master contributed a given file. Two masters landing at the same path inside a shared replica is a **Cross-rule namespace collision**; within a single rule, config validation makes that impossible.
_Avoid_: namespace (fine informally, but **Cross-rule namespace collision** owns the word), subfolder (only true for a folder-type master), prefix

**Sync rule**:
A set of masters paired with a set of replicas: every master in the rule is kept in sync to every replica in the rule. The unit of configuration. Rules are independent of each other, and both a master and a replica may belong to more than one rule (e.g. a shared "baseline skills" master, or a shared project folder as replica, can each be reused by more than one rule). "One rule per project" is just what a rule looks like when its owner chooses not to split its masters across rules — not a distinct mechanic. A rule must have at least one master to exist, but may be saved with no replicas yet, so it can be built out before it has anywhere to sync to.
_Avoid_: mapping, pair, job

**Check**:
An on-demand scan that compares each master against its replicas by file content and reports the drift, without changing anything.
_Avoid_: scan, diff, compare

**Sync**:
Applying a master's content to a replica (or a chosen subtree of it) so they match. Always one-way, master to replica.
_Avoid_: push, copy, update, apply

**Drift**:
Any content difference between a master and a replica: files the replica is missing, files that differ, and files the replica has that the master has deleted.

**Divergence**:
The specific case where a replica was modified locally after its last sync, so overwriting it from the master would lose those local edits. A subset of drift that the tool flags distinctly.

**Safe drift**:
Drift a check can apply without a decision: a file the replica is missing, or one the master changed while the replica did not. The only drift eligible for "sync all".

**Converged**:
The case where both the master and a replica changed since the last sync but ended up with identical content. Nothing to sync; the tool silently brings its last-sync record up to date.

**Changed on both sides**:
The conflict where both the master and a replica were edited since the last sync and no longer match. Distinct from divergence (where only the replica changed) — the tool flags it and refuses to overwrite silently.

**No sync history**:
The state where a file differs between master and replica and the tool has no record of a previous sync for it — typically the first check of a replica that already holds hand-copied files. The two sides can be compared, but there is no way to tell which one changed, so it is a conflict: resolved per file or in bulk from the conflict dialog, never part of "sync all safe changes". Shown to the user as "Differs from master (no sync history)".
_Avoid_: no baseline (internal category name only), can't compare (the sides *can* be compared — what's unknown is which one changed)

**Conflict**:
A drift the tool cannot apply on its own: divergence, changed on both sides, or a file with no sync history. Requires the user to resolve it; never touched by "sync all safe changes."
_Avoid_: divergence (only one of the three conflict cases), cross-rule namespace collision (a structural config fact, not a per-file drift — there's nothing to resolve by content, only by editing config)

**Conflict queue**:
The ordered list of a replica's (or a whole rule's) unresolved conflicts that a "Resolve conflicts" dialog steps through one at a time, in the same order as the review tree.
_Avoid_: queue (alone — ambiguous outside the resolve-conflicts context)

**Conflict view**:
Everything shown to the user when resolving a single conflicted file: one or two content diffs (or the reason each isn't available) plus any explanatory note. Built fresh each time the conflict queue lands on a file.

**Content diff**:
The line-by-line comparison between two versions of one file's text, shown inside a conflict view to help the user decide how to resolve it. Distinct from Check, which reports only which files drifted, never their line-level content.
_Avoid_: diff (bare "diff" collides with Check's own _Avoid_ entry — always say "content diff" so it isn't mistaken for the check operation)

**Resolve**:
The user's decision on a conflicted file: overwrite it from master, skip it for now, or keep the replica's version. The conflict counterpart to sync — sync applies safe drift, resolving settles a conflict, and resolving may choose not to apply master's version at all.

**Kept**:
A file where the user resolved a conflict by choosing to keep the replica's version. The tool remembers this choice and keeps showing the file as a deliberate departure from master (not "in sync") until the master changes again, at which point it becomes ordinary drift.
_Avoid_: resolved (covers all three resolution outcomes, not just this one), ignored (skip is the no-op that leaves no record; kept leaves one)

**Master missing**:
The state where one of a rule's masters — the whole directory, or the one file for a file-type master — doesn't exist or can't be read at check time, as distinct from individual files having been deleted from a master that's still there. Evaluated per master: blocks ordinary sync for that master's namespace only, across every replica in the rule, until the user explicitly unlocks it, so a bad path or an unmounted drive can't masquerade as "this master deleted everything" — while the rule's other masters keep syncing normally.
_Avoid_: master_deleted (the per-file category for content removed while the rule's master root is still present)

**Session**:
One run of the app, together with the live working set it holds in memory: the loaded sync rules, each rule's most recent check result, which masters the user has unlocked, which files are ticked, and which rules are queued to be checked. None of it is written to disk — it is rebuilt from `config.toml` and `state.json` at each launch, which is what "in-session only" means wherever this glossary uses the phrase (see **Master missing**). Distinct from the last-sync record in `state.json`, which does persist: a session holds that record and writes it back, but the session's own contents end when the window closes.
_Avoid_: workspace, controller, manager, application state

**App update**:
Replacing the running Syncer build with a newer released version, started only when the user explicitly asks for it. Concerns the tool itself, never masters or replicas, and must leave the user's own data (configuration, last-sync records, backups, logs) untouched.
_Avoid_: update (bare — collides with **Sync**'s _Avoid_ list), upgrade, self-update

**Dependency**:
A directional, global relationship between two master paths: "A depends on B" means wherever A is synced, B is expected to sit alongside it in the same replicas — B lands at its own landing path, never nested inside A's. Declared by hand only (never auto-detected from content); keyed by path, so a dependency can name a master path that no rule currently lists. Independent of any rule, like **Replica name** — set once, applies everywhere A appears. No cycles (including self-dependency): rejected at validation time, the same tier as duplicate-master validation. A master may have several dependencies, and be depended on by several masters.
_Avoid_: requirement (implies enforcement — a dependency is always skippable), optional dependency (redundant; every dependency here is optional)

**Unmet dependency**:
The standing fact that a rule contains master A, A depends on master B, and the rule does not contain B. A config-shape fact evaluated from `config.toml` alone (like **Cross-rule namespace collision**), not something `check()`'s filesystem walk discovers, so it's shown as a rule-level badge in the review tree (alongside **Master missing**), not folded into the per-file bucket model. Detected at rule-edit time and re-flagged at every check, for as long as it stays true. The user may accept it per pair, which adds B to the rule as an ordinary master (a real, visible config edit, after which B is checked and synced like any other) — this satisfies that pair, nothing left to silence. Re-evaluated live: changing the dependency edge (pointing A at a different B) or removing B from the rule again produces a fresh unmet dependency, not a resurfacing of an old one.
_Avoid_: acknowledged, dismissed (superseded — see **Ignore dependencies**)

**Ignore dependencies**:
A per-rule checkbox, set in the rule editor, that blanket-silences every dependency still unmet for that rule — past, present, or future — without adding any of their masters. A dependency the user already accepted (its master added to the rule) is satisfied, not silenced, and unaffected by the box. The rule-level badge from **Unmet dependency** stays visible either way; checking the box only changes its tone, from an actionable "unmet dependency — accept?" prompt to a passive "unmet dependency — ignored for this rule" note.
_Avoid_: acknowledge, dismiss (the box is a standing rule setting, not a per-instance action)

**Cross-rule namespace collision**:
The state where two or more sync rules that share a replica have masters landing at the same physical path within it — a folder-type master's `<basename>/` folder, or a file-type master's bare filename, whichever the colliding masters compute to. A config authoring fact, not a per-file drift: detected structurally from the rules' configuration alone, with no filesystem walk needed. Blocks ordinary sync for just that landing path, in every rule that contributes to it, while each colliding rule's other masters keep syncing normally — mirrors **Master missing**'s per-master scoping, but unlike it, has no unlock: the fix (rename a colliding master, or stop sharing the replica) is fully within the user's control, so the tool never offers to proceed anyway.
_Avoid_: conflict (see **Conflict**'s _Avoid_ line), basename collision (the actual key is the computed landing path, so a folder-type and file-type master of the same name collide too, not just identical basenames)
