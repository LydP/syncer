# Syncer

A general-purpose, offline, Windows-only tool that keeps copies of files and folders current with
their canonical originals.

It exists because keeping the same content identical across many project directories by hand is
error-prone. Point Syncer at your **masters**, list the **replicas** they should land in, and run
a **check** to see exactly what has drifted before anything is written.

## Status

**Usable, still growing.** Released builds are on the
[Releases page](https://github.com/LydP/syncer/releases); what changed in each is in its release
notes. Further work is tracked as [GitHub issues](https://github.com/LydP/syncer/issues).

## Install

1. Download `syncer-vX.Y.Z-windows.zip` from the latest
   [release](https://github.com/LydP/syncer/releases/latest).
2. Extract it to a folder you can write to (not `Program Files`) and run `syncer.exe`.

Syncer keeps its configuration, sync records, backups and logs in that same folder, so the
folder is the whole install: move it and everything comes with it. To upgrade, use
**Help → Check for updates…**. It replaces the program files and leaves your data alone.

## How it works

- **Masters → replicas, one way.** A **sync rule** pairs one or more **masters** (each a file or
  folder) with a list of **replica** absolute paths — every master in a rule syncs to every
  replica in it. Sync is always one-way, master to replica — no merge, no reverse push. A
  folder-type master lands under its own subfolder in the replica (named for the master's
  basename) so several masters in one rule don't collide; a file-type master lands directly in
  the replica under its own filename. A master or replica can be reused by more than one rule,
  and a replica can be given a short **name** shown in place of its path.
- **Check before sync.** An on-demand **check** compares each master against its replicas by file
  content (not timestamps) and reports the **drift**: files the replica is missing, files that
  differ, and files the master has deleted. Nothing changes until you confirm.
- **Divergence is flagged.** If a replica was edited locally since its last sync, overwriting it
  would lose those edits. Syncer calls this out distinctly from ordinary drift.
- **Deletions are their own step.** Propagated deletions are shown as a separate category and
  confirmed separately from updates.
- **Shared replicas are protected.** If two rules' masters would land at the same path inside a
  replica they both target, sync is blocked for just that path in both rules until you rename a
  master or stop sharing the replica.
- **Dependencies between masters.** You can declare that one master needs another alongside it
  wherever it's synced. A rule that has the first but not the second gets flagged, and you can
  add the missing master with one click or turn these prompts off for that rule.
- **Review at any level.** A GUI shows check results as an expandable tree (rule → replica →
  folder → file); you can sync a whole rule or a single file. Before a check, the same tree
  previews what each replica should hold.

### Settled constraints

Windows only · Python + PySide desktop GUI · on-demand check only (no filesystem watching) ·
content-based comparison · human-readable `config.toml` plus a separate `state.json`, both
GUI-managed · portable storage: everything Syncer generates lives beside the executable, never in
`%APPDATA%` or the registry (see [ADR 0001](docs/adr/0001-portable-self-contained-storage.md)).

### Out of scope

Real-time watching · reverse or bidirectional sync · three-way merge · macOS / Linux · network
or cloud sync · pulling upstream repos (done by hand).

## Repository layout

| Path | What |
|------|------|
| `src/syncer/` | Application source (src-layout). |
| `tests/` | Pytest suite. |
| `scripts/` | Build tooling — `build.py` drives the Nuitka standalone build. |
| [`docs/releases/`](docs/releases/) | Hand-written release notes, one `vX.Y.Z.md` per release. |
| [`CONTEXT.md`](CONTEXT.md) | Domain glossary — master, replica, sync rule, check, sync, drift, divergence. |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records. |
| [`docs/agents/`](docs/agents/) | How agent skills consume this repo (issue tracker, domain docs). |
| `.scratch/local-file-syncer/` | The completed planning effort — historical record: `map.md`, resolved `issues/NN-*.md`, and the `spec.md` it produced. |
| [`CLAUDE.md`](CLAUDE.md) | Guidance for Claude Code. |

## Build workflow

Work is tracked as [GitHub issues](https://github.com/LydP/syncer/issues), worked roughly in
issue-number order; check the tracker for current status rather than relying on numbers written
here. Set up a dev environment with:

```
python -m venv venv
venv\Scripts\pip install -e ".[dev]"       # add ",gui" too for GUI work
venv\Scripts\python -m pytest
```

### Cutting a release

1. Bump `version` in `pyproject.toml`.
2. Write the notes in `docs/releases/vX.Y.Z.md`, covering only changes a user would notice.
   The in-app update dialog shows them.
3. Commit, then tag `vX.Y.Z` and push the tag. The release workflow checks that the tag matches
   `pyproject.toml`, builds and zips the app, and publishes the GitHub Release with those notes.

Releases are immutable once published, so the notes must be committed before the tag is pushed.
See [ADR 0003](docs/adr/0003-packaging-and-versioning.md).

## Planning history

Planning was driven by the `/wayfinder` skill against
[`.scratch/local-file-syncer/map.md`](.scratch/local-file-syncer/map.md) and ended at the
build-ready [`spec.md`](.scratch/local-file-syncer/spec.md). Kept for reference; new work goes to
GitHub issues, not new tickets there.
