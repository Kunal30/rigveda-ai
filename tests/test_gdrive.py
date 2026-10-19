import json
import stat
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import pytest

from rigveda import gdrive

KEY = {"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "s3cret",
                     "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def key(tmp_path):
    source = tmp_path / "client_secret_123.json"
    source.write_text(json.dumps(KEY))
    gdrive.setup(source)
    return source


def private(path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) == 0o600


def test_setup_moves_a_desktop_key_somewhere_private(key, tmp_path) -> None:
    assert json.loads(gdrive.client_path().read_text()) == KEY and private(gdrive.client_path())
    assert not key.exists()  # left in Downloads it would be indexed
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": KEY["installed"]}))
    with pytest.raises(gdrive.DriveError, match="Desktop app"):
        gdrive.setup(web)
    assert web.exists()  # a rejected file is left where it was


def test_connect_signs_in_through_the_loopback_listener(key, monkeypatch) -> None:
    exchanged = {}

    def fake_post(url, fields):
        exchanged.update(fields)
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3599, "scope": gdrive.SCOPE}

    def browser(url):  # Google's consent page sends the browser back to our listener with a code
        query = parse_qs(urlsplit(url).query)
        assert query["scope"] == [gdrive.SCOPE] and query["code_challenge_method"] == ["S256"]
        back = f"{query['redirect_uri'][0]}/?state={query['state'][0]}&code=4/abc&scope={gdrive.SCOPE}"
        with urlopen(back) as response:
            assert b"connected" in response.read()
        return True

    monkeypatch.setattr(gdrive, "_post", fake_post)
    monkeypatch.setattr(gdrive, "account", lambda: {"email": "me@gmail.com", "name": "Me"})
    assert gdrive.connect(browser, timeout=10)["email"] == "me@gmail.com"
    assert exchanged["code"] == "4/abc" and exchanged["grant_type"] == "authorization_code" and exchanged["code_verifier"]
    assert gdrive.status()["email"] == "me@gmail.com" and private(gdrive.token_path())
    assert json.loads(gdrive.token_path().read_text())["refresh_token"] == "rt"


def test_connect_rejects_an_answer_with_the_wrong_state(key, monkeypatch) -> None:
    def browser(url):
        query = parse_qs(urlsplit(url).query)
        try:
            urlopen(f"{query['redirect_uri'][0]}/?state=forged&code=x")
        except Exception:
            pass
        return True

    with pytest.raises(gdrive.DriveError, match="didn't finish"):
        gdrive.connect(browser, timeout=2)


def test_expired_access_is_renewed_from_the_stored_sign_in(key, monkeypatch) -> None:
    gdrive._write_private(gdrive.token_path(), {"access_token": "old", "refresh_token": "rt", "expires_at": 0, "email": "me@gmail.com"})
    monkeypatch.setattr(gdrive, "_post", lambda url, fields: {"access_token": "new", "expires_in": 3600}
                        if fields["refresh_token"] == "rt" and fields["grant_type"] == "refresh_token" else {})
    assert gdrive.access_token() == "new"
    saved = json.loads(gdrive.token_path().read_text())
    assert saved["refresh_token"] == "rt" and saved["email"] == "me@gmail.com" and saved["expires_at"] > time.time()


def test_disconnect_revokes_and_forgets(key, monkeypatch) -> None:
    revoked = []
    gdrive._write_private(gdrive.token_path(), {"access_token": "a", "refresh_token": "rt", "expires_at": 0})
    monkeypatch.setattr(gdrive, "_post", lambda url, fields: revoked.append((url, fields["token"])) or {})
    assert gdrive.disconnect() and revoked == [(gdrive.REVOKE_URL, "rt")]
    assert not gdrive.token_path().exists() and gdrive.status() is None and not gdrive.disconnect()


@pytest.mark.parametrize("mime, group", [
    ("application/vnd.google-apps.document", "Google Docs, Sheets & Slides"),
    ("application/vnd.google-apps.form", "Other Google items (Forms, Drawings…)"),
    ("application/vnd.google-apps.folder", None),
    ("application/pdf", "PDFs"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word, Excel & PowerPoint"),
    ("application/msword", "Older Office files (.doc, .xls, .ppt)"),
    ("text/csv", "Text & data"),
    ("image/heic", "Images"),
    ("video/mp4", "Video"),
    ("application/zip", "Archives"),
    ("application/octet-stream", "Other"),
])
def test_category(mime, group) -> None:
    assert gdrive.category(mime) == group


def test_survey_counts_and_sizes_by_group() -> None:
    items = [
        {"mimeType": "application/vnd.google-apps.folder"},
        {"mimeType": "application/vnd.google-apps.document", "ownedByMe": True},
        {"mimeType": "application/pdf", "size": "1000", "ownedByMe": True},
        {"mimeType": "application/pdf", "size": "500", "ownedByMe": False},
        {"mimeType": "video/mp4", "size": "9000000", "ownedByMe": True},
    ]
    report = gdrive.survey(iter(items))
    assert report["folders"] == 1 and report["files"] == 4
    assert report["groups"]["PDFs"] == {"handling": "read", "files": 2, "bytes": 1500, "shared": 1}
    assert report["readable_files"] == 3 and report["readable_bytes"] == 1500
    assert report["groups"]["Video"]["handling"] == "skipped"


def test_files_follows_every_page(key, monkeypatch) -> None:
    pages = {None: {"files": [{"id": "1"}], "nextPageToken": "p2"}, "p2": {"files": [{"id": "2"}]}}
    monkeypatch.setattr(gdrive, "api", lambda path, params: pages[params.get("pageToken")])
    assert [item["id"] for item in gdrive.files()] == ["1", "2"]


def test_sizes_read_like_a_person_would_say_them() -> None:
    assert [gdrive.size(n) for n in (512, 1536, 300.87 * 2**30)] == ["512 B", "1.5 KB", "300.9 GB"]


def test_cancelled_sign_in_page_escapes_googles_error(key) -> None:
    pages = []

    def browser(url):
        query = parse_qs(urlsplit(url).query)
        try:
            urlopen(f"{query['redirect_uri'][0]}/?state={query['state'][0]}&error=%3Cscript%3Ex%3C/script%3E")
        except Exception as error:  # a 400 page
            pages.append(error.read().decode())
        return True

    with pytest.raises(gdrive.DriveError, match="didn't finish"):
        gdrive.connect(browser, timeout=5)
    assert "&lt;script&gt;" in pages[0] and "<script>" not in pages[0]
