import fnmatch
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field

from syncer.config import SyncRule, normalize_replica_path

_IGNORED_DIR_NAMES = {".git", ".svn", ".hg", "__pycache__"}
# "*.syncer-tmp-*": the executor's own overwrite temp files, orphaned only by a
# crash mid-copy — never real replica content.
_IGNORED_FILE_PATTERNS = (
    "*.pyc",
    "~$*",
    "Thumbs.db",
    "desktop.ini",
    ".DS_Store",
    "*.syncer-tmp-*",
)
# One precompiled alternation instead of a per-file sweep of fnmatch calls, each
# of which re-normcases both arguments. Windows-only tool, so matching is always
# case-insensitive (what fnmatch gave us via normcase anyway).
_IGNORED_FILE_RE = re.compile(
    "|".join(fnmatch.translate(pattern) for pattern in _IGNORED_FILE_PATTERNS),
    re.IGNORECASE,
)

_HASH_CHUNK_BYTES = 65536
# Recorded in state.json; a stored baseline hashed with anything else is unusable.
HASH_ALGO = "sha256"


def _is_ignored_file(name: str) -> bool:
    return _IGNORED_FILE_RE.match(name) is not None


@dataclass(frozen=True)
class BaselineEntry:
    hash: str
    size: int
    mtime: float
    kept: bool = False
    # The master's hash at keep time; None if the master was absent then.
    # Only meaningful when kept is True.
    kept_master_hash: str | None = None


@dataclass(frozen=True)
class FileChange:
    rel_path: str
    category: str
    master_present: bool
    replica_present: bool
    baseline_present: bool
    master_size: int | None = None
    replica_size: int | None = None
    baseline_size: int | None = None
    baseline_mtime: float | None = None
    detail: str | None = None
    baseline_stale: bool = False

    @property
    def is_deletion(self) -> bool:
        """True when the executor applies this change by removing the replica
        file rather than copying master's content onto it: `master_deleted`,
        or a conflict with no master copy to overwrite from — a `both_changed`
        promoted from it (master gone, replica edited), or a `diverged` kept
        entry whose master was already absent at keep time — whose resolution
        is "overwrite from master".
        """
        return self.category == "master_deleted" or (
            self.category in ("both_changed", "diverged") and not self.master_present
        )


@dataclass(frozen=True)
class ReplicaCheckResult:
    replica_path: str
    replica_exists: bool
    has_baseline: bool
    files: list[FileChange] = field(default_factory=list)
    walk_errors: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CheckResult:
    rule_id: str
    rule_name: str
    master_type: str
    master_missing: bool
    replicas: list[ReplicaCheckResult] = field(default_factory=list)


@dataclass(frozen=True)
class _SideEntry:
    """One path as found on one side, carrying every fact the walk already
    learned about it — so nothing downstream re-stats or re-derives it.
    """

    rel_path: str  # POSIX-separated, in the casing this side actually uses
    kind: str  # "file" | "dir" | "unreadable"
    abs_path: str | None = None
    size: int | None = None
    mtime: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Side:
    entries: dict[str, _SideEntry]  # keyed by os.path.normcase(rel_path)
    exists: bool
    walk_errors: list[dict] = field(default_factory=list)
    # normcased POSIX rel paths of directories that couldn't be listed, mapped
    # to the error text; "" is the side's root itself.
    unlisted_dirs: dict[str, str] = field(default_factory=dict)

    @property
    def root_unlisted(self) -> bool:
        return "" in self.unlisted_dirs

    def unlisted_detail(self, key: str, include_root: bool) -> str | None:
        """The listing error hiding `key` on this side, if a folder above it
        couldn't be listed. The master's root is excluded by callers: that case
        is reported rule-wide as master missing.
        """
        # normcase turns "/" into "\\" on Windows, so split on either.
        parts = key.replace("\\", "/").split("/")[:-1]
        for depth in range(0 if include_root else 1, len(parts) + 1):
            message = self.unlisted_dirs.get(os.path.normcase("/".join(parts[:depth])))
            if message is not None:
                return message
        return None


def hash_file(path: str) -> str:
    digest = hashlib.new(HASH_ALGO)
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def baseline_from_disk(
    path: str, *, kept: bool = False, kept_master_hash: str | None = None
) -> BaselineEntry:
    """A baseline entry recording `path`'s current bytes.

    Always the *replica's* own file — that's what a future check() compares
    against, whether the bytes got there by a copy from the master (sync.py)
    or by the user keeping the replica's version (conflict.py). Size and mtime
    must come from the same file as the hash: _entry_hash uses them as the
    cache key for skipping a re-hash.
    """
    stat = os.stat(path)
    return BaselineEntry(
        hash=hash_file(path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        kept=kept,
        kept_master_hash=kept_master_hash,
    )


def _file_entry(rel_path: str, abs_path: str) -> _SideEntry:
    try:
        stat = os.stat(abs_path)
    except OSError as exc:
        return _SideEntry(rel_path, "unreadable", abs_path=abs_path, detail=str(exc))
    return _SideEntry(
        rel_path, "file", abs_path=abs_path, size=stat.st_size, mtime=stat.st_mtime
    )


def _is_dir_link(path: str) -> bool:
    """A symlink or an NTFS junction. os.path.islink misses junctions, and
    os.walk(followlinks=False) still descends into them.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or (
        getattr(st, "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT
    )


def _scan_tree(root: str) -> tuple[dict[str, _SideEntry], list[dict], dict[str, str]]:
    entries: dict[str, _SideEntry] = {}
    walk_errors: list[dict] = []
    unlisted_dirs: dict[str, str] = {}

    def _on_error(exc: OSError) -> None:
        walk_errors.append({"path": exc.filename or "", "message": str(exc)})
        if exc.filename:
            rel_dir = os.path.relpath(exc.filename, root).replace(os.sep, "/")
            unlisted_dirs[os.path.normcase("" if rel_dir == "." else rel_dir)] = str(exc)

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_error, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        # Build child keys by joining onto the directory's own relative path;
        # os.path.relpath per file would redo a getcwd + two normpaths each time.
        prefix = "" if rel_dir == "." else f"{rel_dir}/"

        kept_dirnames = []
        for name in dirnames:
            if name in _IGNORED_DIR_NAMES:
                continue
            rel_path = prefix + name
            full_path = os.path.join(dirpath, name)
            if _is_dir_link(full_path):
                # A directory symlink/junction isn't recursed into (pruned from
                # dirnames below); report it in place of the files it would
                # otherwise have contributed.
                entries[os.path.normcase(rel_path)] = _SideEntry(
                    rel_path,
                    "unreadable",
                    detail="symlink or junction to a folder is not followed",
                )
                continue
            # Recorded only so a file-on-one-side/folder-on-the-other clash can
            # be spotted; directories are never units of change themselves.
            entries[os.path.normcase(rel_path)] = _SideEntry(rel_path, "dir")
            kept_dirnames.append(name)
        dirnames[:] = kept_dirnames

        for name in filenames:
            if _is_ignored_file(name):
                continue
            rel_path = prefix + name
            entries[os.path.normcase(rel_path)] = _file_entry(
                rel_path, os.path.join(dirpath, name)
            )
    return entries, walk_errors, unlisted_dirs


def _scan_side(path: str, master_type: str, single_file_name: str) -> _Side:
    exists = os.path.exists(path)
    if master_type == "file":
        entries = {}
        if exists:
            key = os.path.normcase(single_file_name)
            if os.path.isfile(path):
                entries[key] = _file_entry(single_file_name, path)
            else:
                # A folder where the one file should be: a type mismatch, not
                # an absent file (which would read as new / master_deleted).
                entries[key] = _SideEntry(single_file_name, "dir")
        return _Side(entries, exists)
    if not exists:
        return _Side({}, False)
    entries, walk_errors, unlisted_dirs = _scan_tree(path)
    return _Side(entries, True, walk_errors, unlisted_dirs)


def _comparable_keys(side: _Side) -> set[str]:
    return {key for key, entry in side.entries.items() if entry.kind != "dir"}


def _entry_hash(
    entry: _SideEntry,
    baseline_entry: BaselineEntry | None,
    cache: dict[str, str] | None = None,
) -> str:
    # Size/mtime are advisory hints only: matching both means we may reuse the
    # stored hash instead of re-reading the file.
    if (
        baseline_entry is not None
        and entry.size == baseline_entry.size
        and entry.mtime == baseline_entry.mtime
    ):
        return baseline_entry.hash
    if cache is not None and entry.abs_path in cache:
        return cache[entry.abs_path]
    digest = hash_file(entry.abs_path)
    if cache is not None:
        cache[entry.abs_path] = digest
    return digest


# Keyed by (replica matches its baseline hash, master matches its baseline hash).
# For an ordinary entry both sides share `hash`, so (True, True) is unreachable
# once the sides are known to differ; only a `kept` entry lands there.
_CATEGORY_BY_BASELINE_MATCH = {
    (True, True): "kept",
    (True, False): "changed",
    (False, True): "diverged",
    (False, False): "both_changed",
}


def _master_baseline_hash(baseline_entry: BaselineEntry) -> str | None:
    """What the master is compared against (spec.md §9): a `kept` entry's
    master against its own hash from keep time (None if absent then), since
    `hash` is the replica's kept content.
    """
    return baseline_entry.kept_master_hash if baseline_entry.kept else baseline_entry.hash


def _categorize_present_both(
    master_hash: str, replica_hash: str, baseline_entry: BaselineEntry | None
) -> tuple[str, bool]:
    baseline_hash = baseline_entry.hash if baseline_entry is not None else None
    if master_hash == replica_hash:
        # Converged: content now matches but the baseline is stale.
        return "in_sync", baseline_entry is not None and master_hash != baseline_hash
    if baseline_entry is None:
        return "no_baseline", False
    return (
        _CATEGORY_BY_BASELINE_MATCH[
            (replica_hash == baseline_hash, master_hash == _master_baseline_hash(baseline_entry))
        ],
        False,
    )


@dataclass(frozen=True)
class _ReplicaPlan:
    """Everything one replica's comparison needs, enumerated up front so the
    file total is known before any categorising starts.
    """

    replica: str
    side: _Side
    baseline: dict[str, BaselineEntry]
    baseline_by_key: dict[str, str]
    keys: list[str]


def _check_replica(
    master_side: _Side,
    plan: _ReplicaPlan,
    master_hashes: dict[str, str],
    on_file=None,
    cancel=None,
) -> ReplicaCheckResult:
    # rel_path matching is case-insensitive on Windows (os.path.normcase), but
    # the reported key preserves master's actual casing (falling back to the
    # baseline's, then the replica's, when master doesn't have that path).
    files = []
    for key in plan.keys:
        master = master_side.entries.get(key)
        replica = plan.side.entries.get(key)
        baseline_entry = plan.baseline.get(plan.baseline_by_key.get(key))
        if master is not None:
            rel_path = master.rel_path
        elif key in plan.baseline_by_key:
            rel_path = plan.baseline_by_key[key]
        else:
            rel_path = replica.rel_path
        master_present = master is not None and master.kind != "dir"
        replica_present = replica is not None and replica.kind != "dir"
        baseline_stale = False
        detail = None

        if on_file is not None:
            on_file(rel_path)
        if cancel is not None and cancel():
            break

        if master is not None and master.kind == "unreadable":
            category = "unreadable"
            detail = master.detail
        elif replica is not None and replica.kind == "unreadable":
            category = "unreadable"
            detail = replica.detail
        elif master is None and (
            hidden := master_side.unlisted_detail(key, include_root=False)
        ) is not None:
            # Absent only because its folder couldn't be listed — not deleted.
            category = "unreadable"
            detail = f"master folder couldn't be listed: {hidden}"
        elif replica is None and (
            hidden := plan.side.unlisted_detail(key, include_root=True)
        ) is not None:
            category = "unreadable"
            detail = f"replica folder couldn't be listed: {hidden}"
        elif master is not None and replica is not None and master.kind != replica.kind:
            category = "unreadable"
            detail = "type mismatch: a file on one side, a folder on the other"
        elif master_present and replica_present:
            try:
                # A kept entry's size/mtime are the replica's own, from keep
                # time - never a valid stat shortcut for the master's hash.
                master_baseline = None if baseline_entry and baseline_entry.kept else baseline_entry
                master_hash = _entry_hash(master, master_baseline, master_hashes)
                replica_hash = _entry_hash(replica, baseline_entry)
            except OSError as exc:
                category = "unreadable"
                detail = str(exc)
            else:
                category, baseline_stale = _categorize_present_both(
                    master_hash, replica_hash, baseline_entry
                )
        elif master_present:
            category = "new"
        elif replica_present and baseline_entry is not None:
            try:
                replica_hash = _entry_hash(replica, baseline_entry)
            except OSError as exc:
                category = "unreadable"
                detail = str(exc)
            else:
                # An absent master matches only a kept-while-absent baseline;
                # otherwise it has "changed" by being deleted.
                category = _CATEGORY_BY_BASELINE_MATCH[
                    (
                        replica_hash == baseline_entry.hash,
                        _master_baseline_hash(baseline_entry) is None,
                    )
                ]
                if category == "changed":
                    category = "master_deleted"
        elif replica_present:
            category = "replica_only"
        else:
            # Gone from master and replica alike; only a stale baseline entry
            # remains. No taxonomy category covers "nothing exists" — nothing
            # to report or sync, so it's dropped rather than shown as drift.
            continue

        files.append(
            FileChange(
                rel_path=rel_path,
                category=category,
                master_present=master_present,
                replica_present=replica_present,
                baseline_present=baseline_entry is not None,
                baseline_stale=baseline_stale,
                detail=detail,
                master_size=master.size if master_present else None,
                replica_size=replica.size if replica_present else None,
                baseline_size=baseline_entry.size if baseline_entry is not None else None,
                baseline_mtime=baseline_entry.mtime if baseline_entry is not None else None,
            )
        )
    return ReplicaCheckResult(
        replica_path=normalize_replica_path(plan.replica),
        replica_exists=plan.side.exists,
        has_baseline=bool(plan.baseline),
        files=files,
        walk_errors=master_side.walk_errors + plan.side.walk_errors,
    )


def check(rule: SyncRule, baseline=None, progress=None, cancel=None) -> CheckResult:
    baseline = baseline or {}
    single_file_name = os.path.basename(rule.master)

    def scan(path: str) -> _Side:
        return _scan_side(path, rule.master_type, single_file_name)

    # The master is invariant across replicas: enumerated once per rule, and its
    # file hashes memoised for the run, so an N-replica fan-out reads it once.
    master_side = scan(rule.master)
    master_keys = _comparable_keys(master_side)
    master_hashes: dict[str, str] = {}

    plans = []
    for replica in rule.replicas:
        replica_baseline = baseline.get(normalize_replica_path(replica), {})
        baseline_by_key = {os.path.normcase(p): p for p in replica_baseline}
        side = scan(replica)
        plans.append(
            _ReplicaPlan(
                replica=replica,
                side=side,
                baseline=replica_baseline,
                baseline_by_key=baseline_by_key,
                keys=sorted(master_keys | _comparable_keys(side) | set(baseline_by_key)),
            )
        )

    progress_total = sum(len(plan.keys) for plan in plans)
    done = 0

    def on_file(rel_path: str) -> None:
        nonlocal done
        done += 1
        progress(done, progress_total, rel_path)

    replicas = []
    for plan in plans:
        replicas.append(
            _check_replica(
                master_side,
                plan,
                master_hashes,
                on_file=on_file if progress is not None else None,
                cancel=cancel,
            )
        )
        if cancel is not None and cancel():
            break
    return CheckResult(
        rule_id=rule.id,
        rule_name=rule.name,
        master_type=rule.master_type,
        # Gone, or present but its root can't be listed: either way every file
        # would otherwise read as master_deleted, so block it rule-wide.
        master_missing=not master_side.exists or master_side.root_unlisted,
        replicas=replicas,
    )
