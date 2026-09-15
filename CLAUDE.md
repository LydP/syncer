# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**Syncer** is a planned general-purpose, offline, Windows-only tool that keeps many copies of a
file or folder current with a single canonical original. Driving use cases: keeping Claude Code
skill folders current across many project directories (each project taking its own subset), and
mirroring one master resume file into many directories.

**Application code has started.** The planning effort produced a build-ready `spec.md`, and the
build effort is now underway, tracked as GitHub issues (see below), not local tickets. Don't infer
an issue's status from this file — check `git status` for uncommitted work and `gh issue list`
for open/closed state directly; GitHub's auto-close via a commit's `Closes #N` can lag behind a
direct push to `master`, so a closed-looking issue may still show open for a while.

This **is a git repository** (`origin` → `github.com/LydP/syncer`). Build work is committed
directly to `master` against GitHub issues, no feature branches.

**Build / test commands** (`pyproject.toml`, src-layout under `src/syncer/`, tests under
`tests/`): PySide6 is an optional `gui` extra, not a core dependency, so non-GUI work doesn't need
a ~250MB download.

- Install for non-GUI dev work: `pip install -e ".[dev]"`
- Install with the GUI extra too (needed for the review-UI / GUI issues): `pip install -e ".[dev,gui]"`
- Run the full suite: `pytest` (from an activated venv) or `venv/Scripts/python.exe -m pytest`
- Run one file: `pytest tests/test_check.py`; run one test: `pytest tests/test_check.py::test_name -v`
- No linter/formatter is configured yet (no ruff/black/mypy config in `pyproject.toml`) — don't invent one unannounced.

## Where things live

| Path | What |
|------|------|
| `src/syncer/` | Application source, src-layout. `storage.py` (issue `#1`) resolves `base_dir`, checks writability, and provisions the storage layout — see ADR 0001. `config.py` (issue `#2`) loads/saves/validates `config.toml`: `load_config`/`save_config`, uniqueness validation, atomic writes with rolling backups, and `ConfigStore` for reload / warn-before-clobber. |
| `tests/` | Pytest suite, one `test_<module>.py` per `src/syncer/<module>.py`. |
| `CONTEXT.md` | Domain glossary — the authoritative vocabulary. Read first. |
| `docs/adr/` | Architecture Decision Records. Read any that touch your area; flag contradictions rather than silently overriding (`docs/agents/domain.md`). |
| GitHub Issues (`gh issue list`) | **The issue tracker.** Active build-effort tickets `#1`–`#8`, one per spec area (scaffolding, config model, check engine, state model, sync executor, review UI, divergence UX, GUI/first-run). Conventions in `docs/agents/issue-tracker.md`. |
| `.scratch/local-file-syncer/` | The **completed** planning effort — historical record, not an active tracker. `map.md` is the wayfinder map; `issues/` holds its resolved tickets; `spec.md` is the build-ready spec it produced. |
| `.claude/skills/` | Project-local agent skills (grilling, domain-modeling, research, prototype, wayfinder, tdd, to-spec, to-tickets, setup-matt-pocock-skills, …). |

## Architecture

**Module dependency chain** (each layer imports only from the ones before it):
`storage.py` → `config.py` (config.toml) + `check.py` (drift detection) → `state.py` (state.json,
imports `check.BaselineEntry`) → `sync.py` (executor) + `conflict.py` (resolution) → `review.py`
(tree/bucketing model) → `gui/` (Qt). `conflict.py` and `gui/review_pane.py` both import from
`review.py`, not from each other.

**Pure core / thin Qt adapter.** `review.py` and `conflict.py` are Qt-free: all tree-building,
category-bucketing, diff-computation and selection-rollup logic lives there and is covered by
pytest. `gui/review_pane.py` and `gui/conflict_dialog.py` only wire widgets to that logic — no
decisions live in the GUI layer. Per their own module docstrings, the `gui/` files are **not**
part of the TDD loop and are **not** unit-tested; they're verified by running the app. Keep new
logic in the pure layer even when it's GUI-triggered, so it stays testable.

**The three-way comparison model** (`check.py`, spec.md §5/§9) is the core domain logic: every
file is compared across *baseline* (the last-known-synced content, from `state.json`), *master*,
and *replica*, not just master-vs-replica — a two-way `filecmp.dircmp`-style compare can't express
it (ticket `05`, noted again below). `check()` categorizes each file into one of ten
categories (`in_sync`, `new`, `changed`, `master_deleted`, `replica_only`, `unreadable`,
`no_baseline`, `diverged`, `both_changed`, `kept`); `review.py`'s `CATEGORY_BUCKET` is the single
place that rolls those up into the four user-facing buckets (`safe` / `delete` / `conflict` /
`context`) that drive what's tickable, bulk-syncable, or routed into the conflict-resolution
dialog. A "kept" resolution (`conflict.apply_keep_replica`) stores `kept_master_hash` — the
master's hash *at keep time* — so a later `check()` compares each side against its own keep-time
hash instead of the stale baseline; don't reintroduce a plain baseline comparison for kept entries.

**Cross-cutting conventions**, applied consistently across `storage.py`/`config.py`/`state.py`/
`sync.py`:
- All on-disk writes (`config.toml`, `state.json`, replica file copies) go through
  `storage.atomic_write_bytes` / `storage.atomic_copy` (write-to-temp + `os.replace`), never a
  direct write — a crash mid-write must never leave a half-written file.
- All rel_path lookups are case-insensitive (`os.path.normcase`), reflecting Windows semantics,
  while the *displayed* casing preserves whichever side (master, then baseline, then replica) the
  path was found in first.
- User-facing, expected failures (bad config, unwritable `base_dir`, a clobbered save) raise a
  `SyncerError` subclass (defined per-module); anything else is treated as a genuine bug and
  should not be caught.

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