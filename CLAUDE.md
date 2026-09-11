# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**Syncer** is a planned general-purpose, offline, Windows-only tool that keeps many copies of a
file or folder current with a single canonical original. Driving use cases: keeping Claude Code
skill folders current across many project directories (each project taking its own subset), and
mirroring one master resume file into many directories.

**There is no application code yet.** The repo currently holds only planning artifacts. Do not
create source files, a build, or tests unless a task explicitly starts the build effort. There
are no build / lint / test commands.

This is **not a git repository**. There is no branch to put work on; research findings and specs
are captured as files under the effort directory instead (see below).

## Where things live

| Path | What |
|------|------|
| `CONTEXT.md` | Domain glossary — the authoritative vocabulary. Read first. |
| `docs/adr/` | Architecture Decision Records. Read any that touch your area; flag contradictions rather than silently overriding (`docs/agents/domain.md`). |
| `.scratch/<feature-slug>/` | Issue tracker: one directory per feature. `spec.md` is the spec; `issues/NN-<slug>.md` are tickets numbered from `01`. Conventions in `docs/agents/issue-tracker.md`. |
| `.scratch/local-file-syncer/` | The active planning effort. `map.md` is the wayfinder map; `issues/` holds its tickets; `research-*.md` holds captured research. |
| `.claude/skills/` | Project-local agent skills (grilling, domain-modeling, research, prototype, wayfinder, tdd, to-spec, to-tickets, setup-matt-pocock-skills, …). |

## The active effort: `local-file-syncer`

Planning is driven by the `/wayfinder` skill against `.scratch/local-file-syncer/map.md`.

- **The effort ends at a build-ready `spec.md`** (ticket `09`). Do not implement the tool from
  the tickets — building is a separate future effort.
- **Ticket lifecycle** (`docs/agents/issue-tracker.md` → Wayfinding operations): a `Type:` line
  (`research` / `prototype` / `grilling` / `task`) and `Status:` line (`open` / `claimed` /
  `resolved`); `Blocked by: NN, NN` gates a ticket until every listed ticket is `resolved`. To
  work a ticket: pick the lowest-numbered open, unblocked, unclaimed ticket; set `Status: claimed`
  and save before starting; on finish append an `## Answer` heading, set `Status: resolved`, then
  add a one-line pointer to the map's "Decisions so far".
- Ticket `01` (free-tool survey) concluded **build from scratch** — no free/offline/Windows tool
  covers a one-master-to-many-replicas rule model with divergence flagging.

## Settled design constraints (from `map.md` Notes — not open questions)

One-way fan-out, master wins, no merge / no reverse push · one sync rule per syncable unit ·
Python + PySide desktop GUI · on-demand **check** only (no filesystem watching) · Windows only ·
content-based comparison (hashing), not timestamps · human-readable `config.toml` (user intent)
plus a separate `state.json` (last-sync data), both GUI-managed · first-time provisioning of a
missing replica handled like any other update · deletions propagate but are confirmed as their
own category. Engine is standard-library-based (`os.walk` + `hashlib` + `shutil.copy2`) — no
third-party sync library. (`filecmp.dircmp` was originally named here; ticket `05` dropped it —
a two-way compare can't express the three-way baseline/master/replica model.)

**ADR 0001 — portable storage**: everything Syncer generates (`config.toml`, `state.json`,
`backups/`, `logs/`) lives beside the executable in `base_dir` (`dirname(sys.executable)` when
frozen, else project root), never `%APPDATA%` / registry / temp. `base_dir` must be user-writable
(hard error, no fallback). Future readers must not "fix" this deviation from the `%APPDATA%`
convention.

## Working conventions

- **Use the glossary's terms exactly** when naming domain concepts in tickets, specs, or
  proposals: *master*, *replica*, *sync rule*, *check*, *sync*, *drift*, *divergence*. Do not
  drift to the synonyms `CONTEXT.md` lists under *Avoid* (source, destination, mirror, push, …).
  A concept missing from the glossary is a signal — note it for `/domain-modeling`.
- If files like `CONTEXT.md` or ADRs are absent for an area, proceed silently; the
  `/domain-modeling` skill creates them lazily when terms or decisions actually get resolved.

## Development workflow

Each step below is run at the user's discretion — they may run all steps in one session or spread them across multiple sessions.

1. `/tdd` — implement using red-green-refactor; work through the full PRD behavior list. 
2. `/simplify` — reuse, simplification, and altitude cleanups; applies fixes automatically
3. `/code-review` — correctness and deeper review pass; sees the cleaned-up code from steps 2 and 3
4. `/security-review` — security pass
5. **Commit** — captures all changes and the updated graph together