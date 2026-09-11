# Divergence state model

Parent: [Map: Local file syncer](../map.md)
Type: grilling
Status: resolved
Blocked by: 01

## Question

What is persisted after each sync so a later check can tell "this replica was modified locally since last sync" (divergence) apart from "the master moved on" (ordinary drift)?

Decide: what a post-sync snapshot records (per-file content hashes? mtimes? size?); which hash algorithm and why; whether the snapshot is keyed per replica or per (rule, replica); where the state file lives and its format; how divergence is computed from (snapshot, current replica, current master); what happens when no snapshot exists yet (first check, or state file deleted); and how the state file stays consistent if a sync is interrupted partway.

Consult `CONTEXT.md`. Coordinates with [Sync-rule config model](02-sync-rule-config-model.md) on file location and with [Check engine](05-check-engine.md) on how the comparison runs.

## Answer

### The baseline

After every sync, `state.json` records, per file, **one SHA-256 content hash** plus the file's **size** and **mtime**. Size and mtime are advisory only — a check engine ([05](05-check-engine.md)) may skip re-hashing a file whose size and mtime both match the baseline, but they are never trusted for a correctness decision. Comparison stays content-based (a settled constraint).

**One hash per file, not two (no separate master-side / replica-side hash).** A completed sync makes the replica byte-identical to the master, so a single hash captured at that moment *is* the last-synced state of both sides. A later check compares the current master and the current replica each against this one baseline.

- **Hash algorithm**: SHA-256 (`hashlib`, stdlib-only per [01](01-free-tool-survey.md), universally understood). The algorithm name is stored in `state.json`; a snapshot whose `hash_algo` differs from the current default is treated as "no baseline" (see below). Revisit only if [05](05-check-engine.md)'s performance work proves hashing is the bottleneck.

### `state.json` shape

Lives in `base_dir` beside the executable ([ADR 0001](../../../docs/adr/0001-portable-self-contained-storage.md)), JSON, GUI-managed. Keyed by **rule `id` + normalised replica path** (per [02](02-sync-rule-config-model.md)).

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
            "SKILL.md":         { "hash": "a1b2…", "size": 4096, "mtime": 1757423520.0 },
            "references/api.md": { "hash": "c3d4…", "size":  812, "mtime": 1757423519.0 }
          }
        }
      }
    }
  }
}
```

- Flat file→hash map; **no directory entries**. An empty directory in the master has no baseline representation — whether an emptied/removed master directory deletes the replica's directory is [08](08-deletion-and-rename-handling.md)'s call.
- `files` keys are relative POSIX-style paths (`/` separator). The key **preserves the master's actual casing** (needed for `shutil.copy2` and display); lookups/matching are **case-insensitive on Windows**, case-sensitive on future POSIX builds. A master rename differing only by case reads as the same file with a possible content change; the sync corrects the replica's casing.
- Replica path key normalised per [02](02-sync-rule-config-model.md) (`normcase`+`normpath`+`abspath`; lowercased on Windows).
- `last_sync` is per replica, ISO-8601 UTC.
- A single-file master (`master_type = "file"`) uses a one-entry `files` map keyed by the filename.
- `version` supports future migration (fog item "Config versioning / migration", deferred to the build effort).

### Baseline-dependent calls this ticket owns

From comparing `(baseline B, master-now M, replica-now R)` per file. [05](05-check-engine.md) assembles the *complete* per-file taxonomy; these are the distinctions that **require the baseline**:

| Situation | Call |
|---|---|
| M ≠ B, R == B | **drift** — plain master update, eligible for "sync all" |
| M == B, R ≠ B | **divergence** — replica edited locally; excluded from "sync all"; resolution flow is [06](06-divergence-ux.md) |
| M ≠ B, R ≠ B, M == R | **converged** — silently refresh baseline, no action |
| M ≠ B, R ≠ B, M ≠ R | **changed on both sides** — conflict; flagged; resolution is [06](06-divergence-ux.md) |
| in B, absent from M, R == B | **master-deleted** — the baseline is what proves the file once existed; "will-delete" category, confirmed separately ([08](08-deletion-and-rename-handling.md) owns wording/granularity) |

Any file with **no baseline entry** (new in master, foreign file in the replica, first-ever check) → cannot be divergence; categorisation passes to [05](05-check-engine.md) / [08](08-deletion-and-rename-handling.md).

### Write timing & consistency

- **Commit-at-end.** New hashes are held in memory during a sync and written once at the end via `state.json.tmp` + `os.replace()` (same atomic pattern as [02](02-sync-rule-config-model.md)'s config write).
- **Interrupted sync**: a sync killed partway writes *nothing* — the baseline still reflects the previous sync, so on the next check the files that did copy show up as "drift" (M ≠ B, R == B) and are re-copied harmlessly. No partial-write reasoning, no separate master/replica hashes needed.
- **Partial / subtree sync** ([04](04-review-and-sync-tree-ui.md) allows syncing at any tree level): the baseline is **merged** — entries for the files actually copied are updated/added, every other entry is left untouched, `last_sync` is bumped. A replica's baseline can be a patchwork from several partial syncs; each entry means "state when *this file* was last synced".
- **"Skip" on a diverged file**: the baseline entry is **never written**. The file stays flagged as diverged on every subsequent check until it is synced (overwrite → baseline updates) or [06](06-divergence-ux.md) offers another resolution (e.g. "accept replica's version" → baseline adopts the replica's current hash).
- **No rolling backups of `state.json`** (unlike `config.toml`). It is machine-generated, rewritten every sync, and self-healing — atomic writes plus the corrupt-file quarantine below are enough.

### No baseline for a replica

Occurs on the first-ever check, a deleted `state.json`, or a `hash_algo` mismatch.

- **Replica absent or an empty directory** → first-time provisioning (a settled constraint — handled like any update); sync copies the master in, nothing to lose.
- **Replica has content but no baseline** → the whole replica is marked **"no baseline"**; every file that differs from the master is flagged for **explicit per-file confirmation**; nothing from this replica is included in "sync all". Divergence cannot be computed without a baseline, so silent overwrite is refused.
- **Corrupt `state.json`** → renamed to `state.json.corrupt-<timestamp>`, processing starts from empty (every replica becomes "no baseline"), a warning is surfaced.

### Domain model

No `CONTEXT.md` change. The existing *divergence* definition holds exactly; "baseline" / `state.json` / `base_dir` are implementation, not glossary (consistent with [02](02-sync-rule-config-model.md)). **Candidate glossary terms for `/domain-modeling`**: *converged* and *changed on both sides* — flag these when [05](05-check-engine.md) builds the full change taxonomy. No ADR: the one-hash-not-two choice is the only surprising call and is fully explained by the sync invariant, with the `version` field keeping the format migratable.
