import ssl
import urllib.error

import pytest

from syncer import release_asset_name
from syncer.storage import SyncerError
from syncer.update import GitHubReleaseSource, UpdateCheckError, UpdateOffer, check_for_update

SHA256_HEX = "ab" * 32


def _release(tag="v0.2.1", *, notes="Fixes things.", digest=f"sha256:{SHA256_HEX}", **overrides):
    """A GitHub `/releases/latest` payload carrying the build asset for `tag`."""
    version = tag.removeprefix("v")
    name = release_asset_name(version)
    release = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "body": notes,
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://github.com/LydP/syncer/releases/download/{tag}/{name}",
                "digest": digest,
            }
        ],
    }
    release.update(overrides)
    return release


class FakeSource:
    def __init__(self, release):
        self._release = release

    def fetch_latest_release(self):
        return self._release


def test_a_newer_release_is_offered_with_its_notes_asset_url_and_sha256():
    source = FakeSource(_release("v0.2.1", notes="Fixes things."))

    offer = check_for_update(source, current_version="0.2.0")

    assert offer == UpdateOffer(
        version="0.2.1",
        release_notes="Fixes things.",
        asset_url="https://github.com/LydP/syncer/releases/download/v0.2.1/syncer-v0.2.1-windows.zip",
        sha256=SHA256_HEX,
    )


@pytest.mark.parametrize("tag", ["v0.2.0", "v0.1.9"])
def test_the_same_or_an_older_release_is_never_offered(tag):
    source = FakeSource(_release(tag))

    assert check_for_update(source, current_version="0.2.0") is None


def test_versions_compare_numerically_not_as_text():
    source = FakeSource(_release("v0.10.0"))

    assert check_for_update(source, current_version="0.9.0") is not None


def test_no_release_at_all_is_up_to_date():
    source = FakeSource(None)

    assert check_for_update(source, current_version="0.2.0") is None


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_a_draft_or_prerelease_is_never_offered(flag):
    source = FakeSource(_release("v0.2.1", **{flag: True}))

    assert check_for_update(source, current_version="0.2.0") is None


@pytest.mark.parametrize("tag", ["0.2.1", "v0.2", "v0.2.1-beta.1", "v0.2.1.4", "latest"])
def test_a_tag_that_is_not_strict_vX_Y_Z_is_never_offered(tag):
    source = FakeSource(_release(tag))

    assert check_for_update(source, current_version="0.2.0") is None


def test_a_newer_release_without_the_windows_build_asset_is_an_error():
    release = _release("v0.2.1")
    release["assets"][0]["name"] = "Source code (zip)"

    with pytest.raises(UpdateCheckError, match="0.2.1"):
        check_for_update(FakeSource(release), current_version="0.2.0")


def test_update_check_errors_are_user_facing_syncer_errors():
    assert issubclass(UpdateCheckError, SyncerError)


@pytest.mark.parametrize(
    "digest", [None, "", "sha256:", "md5:" + "ab" * 16, "sha256:not-hex", f"sha256:{SHA256_HEX[:-2]}"]
)
def test_a_release_whose_asset_has_no_usable_sha256_digest_is_an_error(digest):
    source = FakeSource(_release("v0.2.1", digest=digest))

    with pytest.raises(UpdateCheckError, match="0.2.1"):
        check_for_update(source, current_version="0.2.0")


def test_the_digest_is_offered_as_lowercase_hex():
    source = FakeSource(_release("v0.2.1", digest=f"sha256:{SHA256_HEX.upper()}"))

    offer = check_for_update(source, current_version="0.2.0")

    assert offer.sha256 == SHA256_HEX


def test_the_running_version_defaults_to_the_installed_distributions(monkeypatch):
    monkeypatch.setattr("syncer.update.app_version", lambda: "0.2.0")

    offer = check_for_update(FakeSource(_release("v0.2.1")))

    assert offer.version == "0.2.1"


class ExplodingSource:
    def fetch_latest_release(self):
        raise AssertionError("the release source must not be contacted")


def test_a_build_with_no_version_metadata_cannot_check_and_never_contacts_the_source(monkeypatch):
    monkeypatch.setattr("syncer.update.app_version", lambda: None)

    with pytest.raises(UpdateCheckError, match="version"):
        check_for_update(ExplodingSource())


def test_a_running_version_that_is_not_plain_X_Y_Z_cannot_check():
    with pytest.raises(UpdateCheckError, match="0.2.0.dev1"):
        check_for_update(ExplodingSource(), current_version="0.2.0.dev1")


def test_errors_raised_by_the_release_source_reach_the_caller():
    class OfflineSource:
        def fetch_latest_release(self):
            raise UpdateCheckError("offline")

    with pytest.raises(UpdateCheckError, match="offline"):
        check_for_update(OfflineSource(), current_version="0.2.0")


def test_github_source_fetches_the_latest_release_json_with_the_documented_headers(github):
    github.reply_json(_release("v0.2.1"))

    release = GitHubReleaseSource(base_url=github.base_url).fetch_latest_release()

    assert release == _release("v0.2.1")
    (path, headers), = github.requests
    assert path == "/repos/LydP/syncer/releases/latest"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["user-agent"].startswith("Syncer")


def test_github_source_treats_404_as_no_release_yet(github):
    github.reply(404, b'{"message": "Not Found"}')

    assert GitHubReleaseSource(base_url=github.base_url).fetch_latest_release() is None


@pytest.mark.parametrize(
    "status, headers",
    [
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}),
        (429, {"x-ratelimit-reset": "1893456000"}),
    ],
)
def test_github_source_reports_a_rate_limit_with_the_time_it_resets(github, status, headers):
    github.reply(status, b'{"message": "rate limit exceeded"}', headers)

    with pytest.raises(UpdateCheckError, match=r"rate limit.*\d{1,2}:\d{2}"):
        GitHubReleaseSource(base_url=github.base_url).fetch_latest_release()


@pytest.mark.parametrize(
    "status, body", [(403, b'{"message": "Forbidden"}'), (503, b"Service Unavailable")]
)
def test_github_source_reports_an_error_that_is_not_a_rate_limit_by_its_status(
    github, status, body
):
    github.reply(status, body)

    with pytest.raises(UpdateCheckError, match=f"HTTP {status}"):
        GitHubReleaseSource(base_url=github.base_url).fetch_latest_release()


def test_github_source_reports_an_unreachable_host():
    # An injected refusal: a real one to a closed loopback port costs ~2s on
    # Windows, which retries the connect before giving up.
    def opener(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    with pytest.raises(UpdateCheckError, match="reach GitHub"):
        GitHubReleaseSource(opener=opener).fetch_latest_release()


def test_github_source_reports_a_timeout(github):
    github.reply_json(_release("v0.2.1"), delay=1.0)

    with pytest.raises(UpdateCheckError, match="timed out"):
        GitHubReleaseSource(base_url=github.base_url, timeout=0.1).fetch_latest_release()


@pytest.mark.parametrize("body", [b"<html>captive portal</html>", b"[1, 2]", b'"text"'])
def test_github_source_reports_a_response_that_is_not_a_release_object(github, body):
    github.reply(200, body)

    with pytest.raises(UpdateCheckError, match="unexpected"):
        GitHubReleaseSource(base_url=github.base_url).fetch_latest_release()


def test_github_source_reports_a_certificate_failure_with_a_way_forward():
    def opener(request, timeout):
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("unable to get local issuer certificate")
        )

    with pytest.raises(UpdateCheckError, match="certificate.*github.com in a browser"):
        GitHubReleaseSource(opener=opener).fetch_latest_release()
