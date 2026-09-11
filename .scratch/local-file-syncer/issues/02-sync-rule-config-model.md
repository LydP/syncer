# Sync-rule config model

Parent: [Map: Local file syncer](../map.md)
Type: grilling
Status: resolved
Blocked by: 01

## Question

What does a sync rule contain, and how is the set of rules stored on disk?

Decide: the fields of a sync rule (name/id, master path, whether the master is a file or a directory, list of replica absolute paths, per-rule options if any); the config file format (TOML / JSON / YAML) and its schema; the fixed on-disk location (e.g. `%APPDATA%\syncer\config.toml`); how replicas are identified within a rule; whether one replica path may be governed by more than one rule (and if not, how that's prevented); and how the config relates to the separate state file from [Divergence state model](03-divergence-state-model.md).

Consult `CONTEXT.md` for vocabulary. If ticket 01 recommends reusing a library, honour any format it imposes.

## Answer

### Format & location

- **`config.toml`** — TOML. Reads with stdlib `tomllib` (Python 3.11+); writes via `tomli-w` or a hand-rendered serialiser (schema is small and flat). Chosen over JSON (double-backslash path escaping, no comments) and YAML (third-party parser, whitespace-fragile). TOML literal strings hold Windows paths verbatim: `master = 'C:\Users\Lloyd\...'`.
- **Portable / self-contained storage** — every file Syncer generates lives inside its install directory (`base_dir`), never the registry, `%APPDATA%`, `%LOCALAPPDATA%`, `%TEMP%`, or the POSIX equivalents. See [ADR 0001](../../../docs/adr/0001-portable-self-contained-storage.md). Rationale: one storage model across the planned Windows/macOS/Linux versions; no performance cost; matches "a folder you drop somewhere and run".
  - `base_dir` = `dirname(sys.executable)` when frozen, else the project root.
  - Contents: `config.toml`, `state.json`, `backups/config-<timestamp>.toml` (last N kept, written on every config save), `logs/` if later tickets add logging.
  - `base_dir` must be user-writable — checked on startup, hard blocking error if not, **no fallback** to `%APPDATA%`.
  - Distribution consequence: ship as a self-contained folder, not a system installer; not into `Program Files` / a writable `.app` bundle / `/usr/bin`.

### Config schema

```toml
version = 1              # for future migration (see fog: "Config versioning / migration")

[settings]              # reserved for global (non-per-rule) options; filled by tickets 03 (hash algo), 05 (global ignore), 07 (window geometry)

[[rule]]
id = "<uuid4>"          # generated at rule creation, immutable; state.json keys off this
name = "cursor-rules skill"   # human label shown in GUI, freely editable, need not be unique
master = 'C:\Users\Lloyd\.claude\skills\cursor-rules'
master_type = "dir"     # "dir" | "file" — stored explicitly, set from the filesystem at creation
replicas = [
  'C:\MyStuff\ProjectA\.claude\skills\cursor-rules',
  'C:\MyStuff\ProjectB\.claude\skills\cursor-rules',
]
ignore = []             # reserved; ticket 05 defines semantics and defaults
```

The schema is OS-neutral in structure; only the path *values* are platform-specific (config files were never portable between OSes).

### Rule identity & fields

- **`id`**: UUID4 string, set once at creation, never changes. It is the join key to `state.json`, so `name`, `master`, and `replicas` can all change without orphaning last-sync history.
- **`name`**: free-text label. GUI nudges toward uniqueness but does not enforce it.
- **`master`**: absolute path.
- **`master_type`**: `"dir"` or `"file"`, stored explicitly (set by inspecting the filesystem when the rule is created). Lets a check run and report coherently when the master is temporarily missing, and stops file/dir semantics flipping silently. A stored-vs-disk mismatch at check time is surfaced as an **error**, never auto-corrected.
- **`replicas`**: array of absolute path strings.
- **`ignore`**: array, reserved for ticket 05.

### Replica identity

- A replica is identified by its **normalised absolute path**: `os.path.normcase(os.path.normpath(os.path.abspath(p)))` (lowercases drive + separators on Windows, collapses `..`, `/` → `\`; a no-op on case-sensitive POSIX filesystems).
- No separate per-replica id.
- Editing a replica's path in the GUI = **remove old + add new**. The old last-sync snapshot is dropped; the next check treats the new path as first-time provisioning. Correct behaviour: a different path is a different location.

### Uniqueness across rules

- **Replica paths unique across all rules** (normalised comparison). Two rules targeting one replica = two masters overwriting the same location. Enforced by GUI validation on add/edit and by config-load validation, which rejects the file with an error naming the conflict.
- **Master paths unique across all rules** — "one rule per syncable unit"; to sync one master to more places, add replicas to its existing rule.
- **Exact duplicates blocked; nested / overlapping paths allowed** for now, possibly with a warning. Full overlap detection is deferred unless it proves to bite.

### Config ↔ state boundary

- `config.toml` is **pure user intent** (rules, masters, replicas, options). It records nothing about what a check found or when a sync last ran.
- `state.json` holds **all** last-sync data, keyed by **rule `id` + normalised replica path**.
- Editing `config.toml` never writes to `state.json` directly. Garbage-collecting state entries for deleted rules / removed replicas is a GUI action (ticket 07).
- `config.toml` writes are **atomic**: serialise to `config.toml.tmp` in the same directory, then `os.replace()`. Combined with `backups/`, a bad write is always recoverable. State-file atomicity / interrupted-sync consistency is ticket 03's.

### Downstream effects on the map

- **Packaging & run model fog** (Not yet specified): annotated — portable storage favours a PyInstaller one-folder build or a plain script; a one-file exe unpacks to a temp dir and fights the "everything beside the executable" rule. Final call still deferred to the build effort.
- **New scope for ticket 07**: a single-instance lock (`syncer.lock` in `base_dir`, stale-lock detection by PID) to stop two copies of a portable tool running against the same state.
- No change to `CONTEXT.md` — no new domain vocabulary; `config.toml` / `state.json` / `base_dir` are implementation, not glossary.
