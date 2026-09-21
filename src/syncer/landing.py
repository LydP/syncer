"""Landing-path arithmetic: where a master's content lands inside a replica.

A dir master lands under its own `<basename>/` folder, a file master directly
at its filename (CONTEXT.md's **Landing path**). Everything here is config
data only — nothing touches the disk.

This module sits *below* `config.py` (`storage -> landing -> config -> ...`),
so `Master` is imported under `TYPE_CHECKING` only. That is deliberate and
load-bearing: `config.default_rule_name`, `find_master_conflict` and the rule
validation all call the basename helpers defined here, so a runtime import of
`config` from this module would be circular. Don't tidy it into a plain
import. Only `master.path` and `master.type` are ever read — never
`isinstance`, never constructing a `Master` — which is what makes that safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from syncer.config import Master


def master_basename(path: str) -> str:
    return os.path.basename(os.path.normpath(path))


def master_basename_key(path: str) -> str:
    """A master's basename identity: what must be unique within a rule, and
    what two rules' masters collide on inside a shared replica.
    """
    return os.path.normcase(master_basename(path))


def replica_abs_path(replica_root: str, rel_path: str) -> str:
    """The on-disk path of a replica-relative `rel_path` — replicas are
    always a namespaced landing tree, so it's a plain join. A function rather
    than a `LandingMap` method: it needs no masters, and a method would
    advertise a dependency that doesn't exist.
    """
    return os.path.join(replica_root, *rel_path.split("/"))


class LandingSplit(NamedTuple):
    """`LandingMap.split`'s result."""

    master: Master | None
    nested: bool
    rest: str


@dataclass(frozen=True)
class LandingMap:
    masters: list[Master]

    @cached_property
    def _by_key(self) -> dict[str, Master]:
        return {master_basename_key(master.path): master for master in self.masters}

    def split(self, rel_path: str) -> LandingSplit:
        """`rel_path` split at its first separator: the master its head names
        (matched case-insensitively), whether anything follows the head, that
        remainder with its casing intact.
        """
        head, sep, rest = rel_path.replace("\\", "/").partition("/")
        return LandingSplit(self._by_key.get(os.path.normcase(head)), bool(sep), rest)

    def owns(self, rel_path: str) -> bool:
        """Whether `rel_path` — a replica-relative landing path — falls in the
        namespace of one of the masters: a file master's bare filename, or a
        dir master's landing folder itself or anything under it. The landing
        folder's own path stays owned so a file or junction sitting where that
        folder belongs is reported as a type mismatch / unreadable, not
        silently dropped. A landing path no *currently configured* master
        claims is left over from a master since removed from the rule, which
        check() reconciles away silently (issue #21) and reconcile_with_config
        purges from the baseline (issue #22).
        """
        master, nested, _ = self.split(rel_path)
        return master is not None and (master.type == "dir" or not nested)

    def master_abs(self, rel_path: str) -> str:
        """The master-side file behind a landing-path `rel_path` — the inverse
        of the namespacing `owns` checks — for sync.py/conflict.py to resolve a
        FileChange back to master-side bytes. A file master's landing path is
        its bare filename, so it maps straight to the master itself.
        """
        master, nested, rest = self.split(rel_path)
        if master is not None:
            if master.type == "dir" and nested:
                return os.path.join(master.path, *rest.split("/"))
            if master.type == "file" and not nested:
                return master.path
        raise ValueError(f"{rel_path!r} is not a file landing path of any configured master")
