# Syncer

A general-purpose, offline, Windows-only tool that keeps copies of files and folders current with
a single canonical original.

It exists because keeping the same content — Claude Code skill folders, a resume — identical
across many project directories by hand is error-prone. Point Syncer at one **master**, list the
places it should be copied to, and run a **check** to see exactly what has drifted before anything
is written.

## Status

**Building.** Planning finished at a build-ready [`spec.md`](.scratch/local-file-syncer/spec.md);
the build itself is tracked as [GitHub issues](https://github.com/LydP/syncer/issues), one per
spec area, worked roughly in order. Issue `#1` (project scaffolding & portable storage) has an
initial implementation; nothing is released yet.

## How it will work

- **Master → replicas, one way.** A **sync rule** pairs one master (file or folder) with a list
  of **replica** absolute paths. Sync is always one-way, master to replica — no merge, no reverse
  push. One rule per syncable unit; each replica can take its own subset.
- **Check before sync.** An on-demand **check** compares each master against its replicas by file
  content (not timestamps) and reports the **drift**: files the replica is missing, files that
  differ, and files the master has deleted. Nothing changes until you confirm.
- **Divergence is flagged.** If a replica was edited locally since its last sync, overwriting it
  would lose those edits. Syncer calls this out distinctly from ordinary drift.
- **Deletions are their own step.** Propagated deletions are shown as a separate category and
  confirmed separately from updates.
- **Review at any level.** A GUI shows check results as an expandable tree (rule → replica →
  folder → file); you can sync a whole rule or a single file.

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
| [`CONTEXT.md`](CONTEXT.md) | Domain glossary — master, replica, sync rule, check, sync, drift, divergence. |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records. |
| [`docs/agents/`](docs/agents/) | How agent skills consume this repo (issue tracker, domain docs). |
| `.scratch/local-file-syncer/` | The completed planning effort — historical record: `map.md`, resolved `issues/NN-*.md`, and the `spec.md` it produced. |
| [`CLAUDE.md`](CLAUDE.md) | Guidance for Claude Code. |

## Build workflow

Work is tracked as [GitHub issues](https://github.com/LydP/syncer/issues) `#1`–`#8`, one per
`spec.md` section, worked roughly in issue-number order. Set up a dev environment with:

```
python -m venv venv
venv\Scripts\pip install -e ".[dev]"       # add ",gui" too for GUI work
venv\Scripts\python -m pytest
```

## Planning history

Planning was driven by the `/wayfinder` skill against
[`.scratch/local-file-syncer/map.md`](.scratch/local-file-syncer/map.md) and ended at the
build-ready [`spec.md`](.scratch/local-file-syncer/spec.md). Kept for reference; new work goes to
GitHub issues, not new tickets there.
