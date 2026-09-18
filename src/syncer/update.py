import json
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from syncer import app_version, release_asset_name, release_tag
from syncer.storage import SyncerError

_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SHA256_DIGEST = re.compile(r"sha256:([0-9a-fA-F]{64})")
_UNEXPECTED_RESPONSE = "GitHub sent an unexpected response to the update check."


class UpdateCheckError(SyncerError):
    pass


@dataclass(frozen=True)
class UpdateOffer:
    version: str
    release_notes: str
    asset_url: str
    sha256: str


def _parse_version(version: str) -> tuple[int, int, int] | None:
    """`version` as a comparable (major, minor, patch), or None unless it's a
    plain X.Y.Z."""
    match = _VERSION.fullmatch(version)
    return None if match is None else tuple(map(int, match.groups()))


def _running_version_key(current_version: str | None) -> tuple[int, int, int]:
    if current_version is None:
        raise UpdateCheckError(
            "This copy of Syncer has no version information, so it can't tell "
            "whether a newer version exists."
        )
    key = _parse_version(current_version)
    if key is None:
        raise UpdateCheckError(
            f"Syncer's own version {current_version!r} isn't a plain X.Y.Z, "
            "so it can't be compared against a release."
        )
    return key


def check_for_update(source, current_version: str | None = None) -> UpdateOffer | None:
    if current_version is None:
        current_version = app_version()
    running = _running_version_key(current_version)
    release = source.fetch_latest_release()
    if release is None or release["draft"] or release["prerelease"]:
        return None
    version = release["tag_name"].removeprefix("v")
    key = _parse_version(version)
    if release["tag_name"] != release_tag(version) or key is None or key <= running:
        return None
    asset_name = release_asset_name(version)
    asset = next((a for a in release["assets"] if a["name"] == asset_name), None)
    if asset is None:
        raise UpdateCheckError(
            f"Release v{version} has no {asset_name} to install."
        )
    digest = _SHA256_DIGEST.fullmatch(asset.get("digest") or "")
    if digest is None:
        raise UpdateCheckError(
            f"Release v{version} has no SHA-256 digest to verify {asset_name} against."
        )
    return UpdateOffer(
        version=version,
        release_notes=release["body"],
        asset_url=asset["browser_download_url"],
        sha256=digest.group(1).lower(),
    )


class GitHubReleaseSource:
    """Syncer's latest GitHub release, fetched unauthenticated over stdlib
    HTTPS; drafts and prereleases are left for `check_for_update` to skip."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 15.0,
        opener: Callable = urllib.request.urlopen,
    ):
        self._url = f"{base_url}/repos/LydP/syncer/releases/latest"
        self._timeout = timeout
        self._opener = opener
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"Syncer/{app_version() or 'unknown'}",
        }

    def fetch_latest_release(self) -> dict | None:
        request = urllib.request.Request(self._url, headers=self._headers)
        try:
            with self._opener(request, timeout=self._timeout) as response:
                release = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise UpdateCheckError(_describe_http_error(exc)) from exc
        except OSError as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            raise UpdateCheckError(_describe_transport_error(reason)) from exc
        except ValueError as exc:
            raise UpdateCheckError(_UNEXPECTED_RESPONSE) from exc
        if not isinstance(release, dict):
            raise UpdateCheckError(_UNEXPECTED_RESPONSE)
        return release


def _describe_transport_error(reason: object) -> str:
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            "Couldn't verify GitHub's security certificate. Opening github.com "
            "in a browser once often fixes this on a machine that hasn't "
            "visited it recently."
        )
    if isinstance(reason, TimeoutError):
        return "The update check timed out waiting for GitHub."
    return f"Couldn't reach GitHub ({reason}). Check your internet connection."


def _describe_http_error(exc: urllib.error.HTTPError) -> str:
    rate_limited = exc.code == 429 or (
        exc.code == 403 and exc.headers.get("x-ratelimit-remaining") == "0"
    )
    if not rate_limited:
        return f"GitHub answered the update check with HTTP {exc.code}."
    reset = exc.headers.get("x-ratelimit-reset")
    when = (
        f"after {time.strftime('%H:%M', time.localtime(int(reset)))}"
        if reset and reset.isdigit()
        else "later"
    )
    return f"GitHub's rate limit for update checks was reached; try again {when}."
