# Portable, self-contained storage — all files live beside the executable

## Status

accepted

## Decision

Syncer keeps everything it generates — `config.toml`, `state.json`, config backups, logs — inside its own install directory (`base_dir`, resolved as the directory of `sys.executable` when frozen, else the project root). Nothing is written to the OS registry, `%APPDATA%` / `%LOCALAPPDATA%`, `~/Library/Application Support`, `~/.config`, `~/.local/share`, or any system temp location.

## Why

Syncer is a personal tool the user runs themselves, and Mac and Linux versions are planned. Following each OS's config-location convention would mean maintaining three different location rules (and split config-vs-state directories on Linux). One "everything beside the executable" rule works identically on all three target platforms and matches how the tool is actually used: a folder you drop somewhere and run. There is no performance cost to this — file location does not affect read/write speed; the Windows `%APPDATA%` convention exists for read-only `Program Files`, roaming profiles, and backup-tool discoverability, none of which apply here.

## Consequences

- **Distribution is a self-contained folder, not a system installer.** Syncer must not be installed into `C:\Program Files\`, `/Applications` (as a writable `.app` bundle — writing inside a bundle breaks code signing), or `/usr/bin`. It lives in a user-writable location (e.g. `C:\Tools\syncer\`, a home-directory folder, a USB stick).
- The install directory must be user-writable; this is checked on startup with a hard, non-fallback error if it is not.
- On Windows this is a deliberate deviation from the `%APPDATA%` convention — future readers should not "fix" it.
- The config *schema* is OS-neutral; only the absolute path *values* inside a config file are platform-specific, and config files were never portable between OSes regardless.
- Favours a PyInstaller one-folder build (or a plain script) over a one-file exe, which would unpack to a temp directory and fight this rule. (Final packaging decision deferred to the build effort.)
