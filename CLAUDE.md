# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**Syncer** is a planned general-purpose, offline, Windows-only tool that keeps many copies of a
file or folder current with a single canonical original. Driving use cases: keeping Claude Code
skill folders current across many project directories (each project taking its own subset), and
mirroring one master resume file into many directories.

**Application code has started.** The planning effort produced a build-ready `spec.md`, and the
build effort is now underway, tracked as GitHub issues (see below), not local tickets. Issue `#1`
(project scaffolding & portable storage) has an implementation on disk
(`src/syncer/storage.py` + `tests/test_storage.py`) but is not yet committed/merged — check
`git status` / issue `#1`'s state before assuming it's done.

This **is a git repository** (`origin` → `github.com/LydP/syncer`). Build work happens on
branches/PRs against GitHub issues in the usual way.

**Build / test commands** (`pyproject.toml`, src-layout under `src/syncer/`, tests under
`tests/`): PySide6 is an optional `gui` extra, not a core dependency, so non-GUI work doesn't need
a ~250MB download.

- Install for non-GUI dev work: `pip install -e ".[dev]"`
- Install with the GUI extra too (needed for the review-UI / GUI issues): `pip install -e ".[dev,gui]"`
- Run tests: `pytest` (from an activated venv) or `venv/Scripts/python.exe -m pytest`

## Where things live

| Path | What |
|------|------|
| `src/syncer/` | Application source, src-layout. `storage.py` (issue `#1`) resolves `base_dir`, checks writability, and provisions the storage layout — see ADR 0001. |
| `tests/` | Pytest suite, one `test_<module>.py` per `src/syncer/<module>.py`. |
| `CONTEXT.md` | Domain glossary — the authoritative vocabulary. Read first. |
| `docs/adr/` | Architecture Decision Records. Read any that touch your area; flag contradictions rather than silently overriding (`docs/agents/domain.md`). |
| GitHub Issues (`gh issue list`) | **The issue tracker.** Active build-effort tickets `#1`–`#8`, one per spec area (scaffolding, config model, check engine, state model, sync executor, review UI, divergence UX, GUI/first-run). Conventions in `docs/agents/issue-tracker.md`. |
| `.scratch/local-file-syncer/` | The **completed** planning effort — historical record, not an active tracker. `map.md` is the wayfinder map; `issues/` holds its resolved tickets; `spec.md` is the build-ready spec it produced. |
| `.claude/skills/` | Project-local agent skills (grilling, domain-modeling, research, prototype, wayfinder, tdd, to-spec, to-tickets, setup-matt-pocock-skills, …). |

## Planning effort (complete): `local-file-syncer`

Planning was driven by the `/wayfinder` skill against `.scratch/local-file-syncer/map.md` and
ended at the build-ready `spec.md` (ticket `09`, resolved). Kept for reference; do not add new
tickets here — new work goes to GitHub issues.

- Ticket `01` (free-tool survey) concluded **build from scratch** — no free/offline/Windows tool
  covers a one-master-to-many-replicas rule model with divergence flagging.

## The active effort: building the tool

Tracked as GitHub issues `#1`–`#8`, each scoped to a `spec.md` section — see
`docs/agents/issue-tracker.md` for the `gh` conventions. No labels/dependencies are set on them
yet; work roughly in issue-number order since later issues (executor, UI, divergence UX) build on
earlier ones (scaffolding, config/state models, check engine). Use the standard `Development
workflow` below per issue.

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