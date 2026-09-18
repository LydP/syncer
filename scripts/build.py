"""Nuitka standalone build script (ADR 0003, issue #27).

Produces a onedir standalone build of Syncer under `dist/app.dist/`, with the
`gui` extra bundled and version metadata embedded from `pyproject.toml` — the
single source of truth ADR 0003 settled on. Run from an environment with the
`gui` and `build` extras installed: `pip install -e ".[gui,build]"`.

With `--release-tag vX.Y.Z` (the release workflow), the tag is checked against
that version before building and the build is zipped under `dist/` afterwards,
so the workflow never re-parses `pyproject.toml` or names the build's layout.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO_ROOT / "src" / "syncer" / "app.py"
OUTPUT_DIR = REPO_ROOT / "dist"
# Nuitka names the standalone folder after the entry point.
DIST_DIR_NAME = f"{ENTRY_POINT.stem}.dist"
# Ships at the build's own root, so an app update (issue #31) knows exactly
# which files belong to this build.
MANIFEST_FILENAME = "app-update-manifest.json"


def write_manifest(dist_dir: Path) -> None:
    """List every file Nuitka produced (relative paths, POSIX separators),
    including the manifest itself, so an app update can swap exactly this
    build's files and remove whatever an older manifest listed that this one
    doesn't. Only `dist_dir` is scanned, so user data (which never lives
    there at build time) can't end up listed — and later deleted."""
    files = sorted(
        {p.relative_to(dist_dir).as_posix() for p in dist_dir.rglob("*") if p.is_file()}
        | {MANIFEST_FILENAME}
    )
    (dist_dir / MANIFEST_FILENAME).write_text(
        json.dumps(files, indent=2) + "\n", encoding="utf-8"
    )


def read_project_metadata() -> dict:
    """The `[project]` table: ADR 0003's single source of truth for the version,
    and for the name and description the built exe advertises."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--release-tag",
        help="vX.Y.Z tag being released; must match pyproject.toml's version",
    )
    release_tag = parser.parse_args().release_tag

    project = read_project_metadata()
    name = project["name"]
    version = project["version"]
    if release_tag is not None and release_tag != f"v{version}":
        print(
            f"Tag {release_tag} does not match pyproject.toml version {version}",
            file=sys.stderr,
        )
        return 1

    args = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--enable-plugins=pyside6",
        "--assume-yes-for-downloads",
        # ADR 0003: the app reads its version at runtime via
        # importlib.metadata, which needs the dist-info bundled.
        f"--include-distribution-metadata={name}",
        f"--output-dir={OUTPUT_DIR}",
        f"--output-filename={name}.exe",
        "--windows-console-mode=disable",
        "--product-name=Syncer",
        f"--file-description={project['description']}",
        f"--file-version={version}",
        f"--product-version={version}",
        str(ENTRY_POINT),
    ]
    print("Running:", " ".join(args))
    returncode = subprocess.run(args, cwd=REPO_ROOT).returncode
    if returncode:
        return returncode

    # Every standalone build, not just tagged releases, so a local build
    # always matches what a release would ship.
    write_manifest(OUTPUT_DIR / DIST_DIR_NAME)

    if release_tag is None:
        return 0

    archive = shutil.make_archive(
        str(OUTPUT_DIR / f"{name}-v{version}-windows"),
        "zip",
        root_dir=OUTPUT_DIR,
        base_dir=DIST_DIR_NAME,
    )
    print("Archived:", archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
