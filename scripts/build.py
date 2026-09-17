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


def read_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def main() -> int:
    version = read_version()
    args = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--enable-plugins=pyside6",
        "--assume-yes-for-downloads",
        # ADR 0003: the app reads its version at runtime via
        # importlib.metadata, which needs the dist-info bundled.
        "--include-distribution-metadata=syncer",
        f"--output-dir={OUTPUT_DIR}",
        "--output-filename=syncer.exe",
        "--windows-console-mode=disable",
        "--product-name=Syncer",
        "--file-description=Syncer",
        f"--file-version={version}",
        f"--product-version={version}",
        str(ENTRY_POINT),
    ]
    print("Running:", " ".join(args))
    result = subprocess.run(args, cwd=REPO_ROOT)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
