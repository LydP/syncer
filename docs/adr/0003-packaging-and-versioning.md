# Packaging as a Nuitka standalone build, semver versioned manually from pyproject.toml

## Status

accepted

## Decision

Syncer is distributed as a Nuitka standalone (onedir) build: a folder containing the frozen
`.exe` plus its dependencies, zipped and attached to a GitHub Release — never a single-file exe
or an OS installer. The version lives in exactly one place, `pyproject.toml`'s `version` field;
the build reads it at build time to embed it as the `.exe`'s file/product version metadata (via
Nuitka's version-metadata flags — see `scripts/build.py`), and the running app reads
it at runtime via `importlib.metadata` for display (the main window's title bar). Versions are
bumped manually, using SemVer loosely (no strict enforcement of what counts as "breaking"), never
automatically or tied to issue/epic boundaries. A release is cut by tagging the commit
(`vX.Y.Z`) and pushing the tag, which triggers a GitHub Actions workflow on a `windows-latest`
runner: it verifies the tag matches `pyproject.toml`'s version (failing the build if they've
drifted), runs the Nuitka build, zips the standalone folder, and attaches it to a GitHub Release.

## Why

Resolves ADR 0001's deferred packaging call ("Final packaging decision deferred to the build
effort") and the versioning gap `pyproject.toml`'s static `0.1.0` left unaddressed. The user is
the sole audience today, running Syncer across their own machines, with a stated intent to
eventually host a build on their personal website — so the pipeline needs to work standalone now
without requiring rework to add a second distribution point later. Nuitka over PyInstaller:
research into current (2025/2026) tooling found Nuitka's standalone builds meaningfully less
prone to antivirus/SmartScreen false positives than PyInstaller's onefile mode (a longstanding,
still-open issue for PyInstaller), faster-starting since it's a compiled binary rather than a
bundled interpreter, and its version-metadata flags are simpler (no separate version-file to
hand-author). Onedir over onefile either way: matches ADR 0001's "everything beside the
executable" model directly — the standalone folder *is* `base_dir`, with no self-extraction-to-
temp step to fight. GitHub Actions + Releases over a purely local/manual build: the repo's
workflow already lives in GitHub issues and `git push` to `master`; wiring releases through the
same platform means "put a build on my website" later is a matter of linking a release asset, not
standing up new infrastructure.

## Considered Options

- **PyInstaller** (onefile or onedir): more mature/ubiquitous tooling, but onefile mode carries
  known AV false-positive problems, and its version metadata requires a separately-authored
  version-file rather than a CLI flag. Rejected in favor of Nuitka given the AV concern outweighs
  Nuitka's slower build times for a project cutting occasional releases, not continuous ones.
- **Single-file exe** (via either tool): rejected — fights ADR 0001's `base_dir` model
  (self-extracts to a temp dir at every launch) and carries the worst of the AV false-positive
  risk.
- **OS installer** (MSI/Setup.exe via Briefcase or similar): rejected for now — implies
  "installed" (registry entries, Program Files) which ADR 0001 already rules out; revisit only if
  a non-technical audience needs a guided install experience.
- **CalVer or a bare incrementing counter** for versioning: rejected in favor of SemVer — SemVer
  is what the tooling (Nuitka's version flags, GitHub Releases tag conventions) expects natively,
  and gives a cheap, if loosely-applied, signal for "small fix" vs "added something."
- **Version bumps tied to closed GitHub issue epics**: rejected — not all work arrives in
  epic-sized chunks (the standalone bugfixes #9–#11 didn't), so tying releases to epics would
  force awkward boundaries.
- **A `CHANGELOG.md`**: deferred, not rejected outright — the GitHub issue tracker already
  records what changed and why for a solo audience; revisit once there's a real external audience
  reading release notes. (Amended for v0.3.0: release notes now live in per-release files
  instead — see Consequences.)

## Consequences

- Supersedes ADR 0001's tentative "PyInstaller one-folder build (or a plain script)" mention —
  Nuitka standalone/onedir is now the actual final answer; the "onedir over onefile" reasoning it
  already gave still holds and doesn't need to change.
- `pyproject.toml`'s `version` field becomes load-bearing beyond packaging metadata: it's read at
  runtime (via `importlib.metadata`) for the GUI's title bar, so it must stay in sync with what's
  actually installed — no separate runtime-only version constant.
- Deferred, not decided: code signing (irrelevant while distribution is personal-only, worth
  revisiting once website distribution actually happens); a `schema_version` field for
  `config.toml`/`state.json` compatibility across app versions (no breaking schema change exists
  yet to migrate from, so adding one now would be speculative). Tracked in
  [Packaging and versioning: decision map](https://github.com/LydP/syncer/issues/26)'s Not yet
  specified.
- Auto-update was originally deferred here until website distribution, but the trigger turned out
  to be tedium updating the user's own multiple machines by hand, not a wider audience. It is now
  decided in [ADR 0004](0004-app-update.md): user-triggered and offline-by-default, distributed
  through these same GitHub Releases zips. Code signing stays deferred.
- Release notes are hand-written per release in `docs/releases/vX.Y.Z.md`, committed with the
  version bump, and passed to `gh release create --notes-file` by the release workflow (falling
  back to `--generate-notes` if the file is missing). There was a real reader for release notes
  sooner than the `CHANGELOG.md` deferral expected: ADR 0004's update dialog shows the release
  body in the app, with links not clickable. GitHub's generated notes gave that dialog only a
  compare link, because work lands on `master` directly, not through PRs. Notes describe
  user-visible changes only, not internal refactors, and don't link to issues. A release is
  immutable once published, so the notes file must exist before the tag is pushed.
- Implementation tracked in the same map's child tickets: the Nuitka build script, the GUI
  version display, the GitHub Actions release workflow, and cutting a first real release
  end-to-end.
