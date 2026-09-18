"""Nuitka standalone build script (ADR 0003, issue #27).

Produces a onedir standalone build of Syncer under `dist/app.dist/`, with the
`gui` extra bundled and version metadata embedded from `pyproject.toml` — the
single source of truth ADR 0003 settled on. Run from an environment with the
`gui` and `build` extras installed: `pip install -e ".[gui,build]"`.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = REPO_ROOT / "src" / "syncer" / "app.py"
OUTPUT_DIR = REPO_ROOT / "dist"


def read_project_metadata() -> dict:
    """The `[project]` table: ADR 0003's single source of truth for the version,
    and for the name and description the built exe advertises."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]


def main() -> int:
    project = read_project_metadata()
    name = project["name"]
    version = project["version"]
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
    return subprocess.run(args, cwd=REPO_ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
