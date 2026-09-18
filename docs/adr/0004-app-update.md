# App update: user-triggered, manifest-driven in-place swap with set-aside rollback

## Status

accepted

## Decision

Syncer can replace its own build with a newer GitHub Release (an **App update**, see `CONTEXT.md`),
started only by the user (Help → Check for updates…). The design has six parts:

- **Offline by default.** The app makes no network contact unless the user clicks "Check for
  updates". There is no background or on-launch check. The dialog shows the current version, the
  new version and its release notes, with Install and restart / Cancel; it says "You're on the
  latest version" otherwise, surfaces network errors plainly (never silently retried), and is
  disabled while a check or a sync is running.
- **What's offered.** `GET /repos/LydP/syncer/releases/latest`, stdlib `urllib` only, unauthenticated.
  That endpoint already excludes drafts and prereleases. A release is offered only if its tag is a
  strict `vX.Y.Z` and its version is higher than the running build's; there are no downgrades and
  only the latest newer release is offered.
- **Integrity.** The downloaded `syncer-vX.Y.Z-windows.zip` is streamed to `base_dir`, hashed as it
  arrives, and verified against the SHA-256 in GitHub's own per-asset `digest` field before
  anything is swapped. An empty `digest` **refuses** the app update. No separate checksum asset is
  published. Immutable releases are enabled on the repo, so a published tag's assets never change;
  a botched release is fixed by bumping the version, never by re-uploading.
- **Manifest-driven swap.** Every build ships `app-update-manifest.json` (written by
  `scripts/build.py`): a JSON array of every file the build contains, relative to `base_dir`. The
  new manifest's files are laid over the install and files the old manifest listed but the new one
  doesn't are removed. Anything in no manifest (`config.toml`, `state.json`, `backups/`, `logs/`)
  is never touched. Manifest paths are validated as relative and never naming user data.
- **Swap mechanism.** The running app swaps its own files; there is no helper binary and no script.
  It stages the extracted zip in `base_dir/.app-update/staging/`, moves the old build's files (and
  any the new manifest will overwrite) into `.app-update/old/`, then moves the staged files into
  place. Windows won't let a running `.exe`, `.dll`, `.pyd` or Qt plugin be overwritten or deleted,
  but it does let them be renamed or moved within the same volume, and that is the whole trick.
  Renames retry a bounded number of times, because Defender can briefly lock new files.
- **Rollback.** The old build stays on disk in `.app-update/old/` until the new build has launched
  successfully. After the swap the old process hides its window, releases `syncer.lock`, launches
  the new exe with a hand-off flag, and **supervises** it. At a success point in startup (after Qt
  and imports load, **before any user-data write**) the new build atomically writes
  `.app-update/launched-ok`. If the new build exits or times out before that marker, the
  still-alive old process moves its own files back, retakes the lock, shows its window again and
  reports the error. If the swap itself fails partway, what was done so far is reversed. On
  success the old process exits and `.app-update/` is deleted. There is no manual-rollback UI.

## Why

Resolves the auto-update deferral in ADR 0003. That ADR expected to revisit it once the tool was
distributed through the user's website; the trigger that actually fired is different: the user
runs Syncer on several of their own machines and manually downloading, unzipping and replacing
each release on each of them is tedious. The audience is still the user alone, so **code signing
stays deferred** — ADR 0003's website-distribution trigger for it hasn't fired.

The shape follows from ADR 0001. User data lives beside the executable, in the same folder the
update replaces, so a naive "delete the folder and extract the new one" would destroy it. That
rules out any whole-folder swap and forces a per-file, manifest-driven one. It also rules out
touching temp directories or the registry. The manifest is the only thing that distinguishes
"the build's files" from "the user's files" without a fragile deny-list.

Rename-while-running was verified live against the real `v0.1.0` build (all 55 files moved aside
in ~23 ms while the app kept running), and it is what makes a helper unnecessary. It also makes
rollback possible: a new build that fails on a missing DLL or an import error dies before any of
its own code runs, so detection and restore have to happen in *another* process, and the old build,
still running because its files were renamed rather than deleted, is that process.

Network contact is opt-in because the app's identity is an offline tool that handles a user's own
files; a background phone-home would be a surprise. Verifying against GitHub's `digest` rather than
a checksum we publish ourselves gives the same guarantee from the same channel with one less
asset to produce, and refusing on an empty `digest` (the field is nullable) means an unverifiable
download is never installed.

## Considered Options

- **Whole-folder swap** (rename `base_dir` aside, extract a fresh one): rejected. `base_dir` can't
  be renamed while a process's working directory is inside it (Explorer launches it that way), and
  it holds user data.
- **Separate helper exe, or copying our own exe to a temp name to do the swap**: rejected. The DLL
  search order loads `python311.dll` and Qt from `base_dir`, so a copy of the app would lock the
  very files it has to replace, and a second binary adds a build and shipping burden.
- **Generated `.bat` / PowerShell script**: rejected. Failures are invisible (no console), the
  restore logic is brittle, and Controlled Folder Access doesn't trust script engines.
- **`MOVEFILE_DELAY_UNTIL_REBOOT`** (replace at next reboot): rejected. It needs admin, writes to
  HKLM, and breaks ADR 0001.
- **Nuitka's automatic-updates plugin**: rejected. It is onefile-only and a commercial feature.
- **Background or on-launch update checks**: rejected for this effort as contradicting
  offline-by-default; a possible future opt-in.
- **A separately published checksum asset**: rejected in favor of GitHub's `digest`; see Why.
- **`certifi` or `truststore` for TLS**: rejected for now. On Windows `ssl.create_default_context()`
  loads the system certificate store, and Nuitka doesn't change that. Reconsider `truststore` only
  if certificate errors show up on real machines.
- **`packaging` for SemVer comparison**: rejected. Tags are constrained to `vX.Y.Z` by the release
  workflow, so a strict regex to an `(int, int, int)` tuple is enough and keeps the engine
  standard-library-only.

## Consequences

- `base_dir` gains a working folder, `.app-update/`. It is never listed in a manifest, and the
  check/sync machinery must never treat it as user data.
- **Rollback only ever happens before the new build touches user data.** The success point must
  therefore precede the first `state.json` or `config.toml` write in startup (`reconcile_and_save`
  currently writes `state.json` during startup, so the apply ticket has to place the marker ahead
  of it). This is what lets `schema_version` for `config.toml`/`state.json` stay deferred: no
  cross-version file format risk exists while rollback never follows a data write.
- `lock.release_lock` unlinks the lock unconditionally today. It needs a PID check or a hand-off
  path so the exiting old build can't delete the new build's `syncer.lock`.
- After the swap the old process must not lazy-load modules from the new files, so it has to
  import everything it needs beforehand. `ssl` must be bundled: `v0.1.0` has no `_ssl.pyd`, so the
  standalone build has to be told to include it.
- The check and the download block, so both run off the Qt GUI thread.
- A crash or power loss in the middle of the swap itself leaves the install needing recovery from
  the journal or by hand; that window is small (tens of milliseconds) and accepted.
- Immutable releases are enabled through the repo's Settings → Releases page only; there is no
  working REST API for the setting, so it can't be verified or re-applied from `gh`.
- **Not verified:** rename-while-running has only been tested on NTFS. ADR 0001 names USB sticks
  as a valid home for `base_dir`, and FAT32/exFAT behaviour is untested. Tracked in
  [App update: decision map](https://github.com/LydP/syncer/issues/31)'s Not yet specified.
- Implementation is tracked in that map's child tickets: the update check, the apply step
  (download, verify, manifest swap, rollback), the Check for updates dialog, and cutting `v0.2.0`
  and `v0.2.1` to verify an app update end to end.
