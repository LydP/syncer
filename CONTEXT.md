# Syncer

A general-purpose, offline, Windows-only tool that keeps copies of files and folders current with a single canonical original. It exists because maintaining the same content (Claude Code skill folders, a resume) across many project directories by hand is error-prone.

## Language

**Master**:
The canonical copy of a file or folder — the one source of truth that all other copies are made to match. A master path may be listed by more than one sync rule.
_Avoid_: source, original, home copy

**Replica**:
A destination path that receives one or more masters' content, kept in sync one-way from those masters. A folder-type master lands under a subfolder of the replica named for the master's own basename (so a rule's several masters don't collide); a file-type master lands directly at the replica path plus its filename. A replica path may be listed by more than one sync rule.
_Avoid_: destination, mirror, target, copy

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

**Conflict**:
A drift the tool cannot apply on its own: divergence, changed on both sides, or a no-baseline mismatch. Requires the user to resolve it; never touched by "sync all safe changes."
_Avoid_: divergence (only one of the three conflict cases)

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
The state where a sync rule's entire master — the whole directory, or the one file for a file-type rule — doesn't exist or can't be read at check time, as distinct from individual files having been deleted from a master that's still there. Blocks ordinary sync for that rule until the user explicitly unlocks it, so a bad path or an unmounted drive can't masquerade as "the master deleted everything."
_Avoid_: master_deleted (the per-file category for content removed while the rule's master root is still present)
