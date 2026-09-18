from importlib.metadata import PackageNotFoundError, version

PROJECT_NAME = "syncer"
# Ships at a standalone build's root, listing that build's own files, so an
# app update knows exactly which files to swap — named once here so the build
# script and the update apply can't drift.
APP_UPDATE_MANIFEST_FILENAME = "app-update-manifest.json"


def release_tag(version: str) -> str:
    """The git tag a release of `version` is published under."""
    return f"v{version}"


def release_asset_name(version: str) -> str:
    """The zipped standalone build attached to a release of `version` —
    named once here so the build script and the update check can't drift."""
    return f"{PROJECT_NAME}-{release_tag(version)}-windows.zip"


def app_version() -> str | None:
    """The installed distribution's version, or None when its metadata is absent.

    ADR 0003 makes `pyproject.toml` the single source of truth and has the app
    read it back at runtime; a source tree that was never installed has no
    metadata to read, so callers present the app without a version rather than
    inventing one.
    """
    try:
        return version(PROJECT_NAME)
    except PackageNotFoundError:
        return None
