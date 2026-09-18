from importlib.metadata import PackageNotFoundError, version

PROJECT_NAME = "syncer"


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
