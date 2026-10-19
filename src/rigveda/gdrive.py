"""Google Drive: read-only sign-in and a survey of what's there, from file names, types and sizes only.

Nothing is downloaded here. Signing in uses your own Google Cloud "desktop app" key (see docs/GOOGLE_DRIVE.md) and
the standard loopback flow with PKCE: your browser shows Google's consent page, and Google sends the answer back to
a one-off listener on 127.0.0.1. The key and the sign-in are stored readable only by you.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from .config import config_path

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3/"
SIGN_IN_TIMEOUT = 300

# How each kind of Drive file would be handled when indexing; "read" kinds are what Rigveda can index today.
GOOGLE = "application/vnd.google-apps."
OFFICE = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
OLD_OFFICE = {"application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint", "application/rtf",
              "application/vnd.oasis.opendocument.text", "application/vnd.oasis.opendocument.spreadsheet",
              "application/vnd.oasis.opendocument.presentation"}
TEXT = {"application/json", "application/xml", "application/x-yaml", "application/javascript", "application/x-sh"}
ARCHIVES = {"application/zip", "application/x-zip-compressed", "application/x-tar", "application/gzip",
            "application/x-gzip", "application/x-7z-compressed", "application/x-rar-compressed", "application/vnd.rar"}
CATEGORIES = {  # name: (what happens to it, shown in this order)
    "Google Docs, Sheets & Slides": "read",
    "PDFs": "read",
    "Word, Excel & PowerPoint": "read",
    "Text & data": "read",
    "Images": "optional",
    "Older Office files (.doc, .xls, .ppt)": "not yet",
    "Video": "skipped",
    "Audio": "skipped",
    "Archives": "skipped",
    "Other Google items (Forms, Drawings…)": "skipped",
    "Other": "skipped",
}


class DriveError(RuntimeError):
    pass


class ExportTooLarge(DriveError):
    """Google exports Docs, Sheets and Slides only up to 10 MB in each format."""


def _config_dir() -> Path:
    return config_path().parent


def client_path() -> Path:
    return _config_dir() / "google-client.json"


def token_path() -> Path:
    return _config_dir() / "google-token.json"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=1)
    os.chmod(path, 0o600)


def _client() -> dict:
    try:
        client = json.loads(client_path().read_text(encoding="utf-8"))["installed"]
        return {"client_id": client["client_id"], "client_secret": client["client_secret"]}
    except FileNotFoundError:
        raise DriveError("No Google key yet. Follow docs/GOOGLE_DRIVE.md, then run `rigveda drive setup FILE.json`.") from None
    except (ValueError, KeyError) as error:
        raise DriveError(f"{client_path()} isn't a Google desktop-app key: {error}") from None


def setup(source: Path) -> Path:
    """Move the downloaded key (client_secret_….json) somewhere only you can read. Moved, not copied: Downloads is
    usually a watched folder, and a copy left there would be indexed, putting the key in the searchable index."""
    try:
        data = json.loads(source.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DriveError(f"Couldn't read {source}: {error}") from None
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        raise DriveError(f"That key is for a {kind!r} app. Create one with application type \"Desktop app\" instead.")
    if not {"client_id", "client_secret"} <= data["installed"].keys():
        raise DriveError("That file is missing client_id or client_secret; download it again from the Clients page.")
    _write_private(client_path(), data)
    if source.expanduser().resolve() != client_path().resolve():
        source.expanduser().unlink()
    return client_path()


def _post(url: str, fields: dict) -> dict:
    request = Request(url, data=urlencode(fields).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            detail = json.load(error)
        except ValueError:
            detail = {}
        if detail.get("error") == "invalid_grant":
            raise DriveError("Google has signed Rigveda out of Drive (access revoked or expired). "
                             "Run `rigveda drive connect` again.") from None
        raise DriveError(f"Google refused the request: {error.code} {detail.get('error_description') or detail.get('error') or ''}".strip()) from None
    except (URLError, TimeoutError) as error:
        raise DriveError(f"Couldn't reach Google: {error}") from None


def _save_tokens(reply: dict, previous: dict | None = None) -> dict:
    tokens = dict(previous or {})
    tokens.update({"access_token": reply["access_token"], "expires_at": int(time.time()) + int(reply.get("expires_in", 3600))})
    if reply.get("refresh_token"):  # only sent on the first sign-in
        tokens["refresh_token"] = reply["refresh_token"]
    if reply.get("scope"):
        tokens["scope"] = reply["scope"]
    _write_private(token_path(), tokens)
    return tokens


def connect(open_browser: Callable[[str], object] = webbrowser.open, timeout: float = SIGN_IN_TIMEOUT) -> dict:
    """Sign in through the browser and keep a read-only Drive sign-in. Returns the account's name and email."""
    client = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state", [""])[0] != state:
                self.reply(400, "This sign-in link doesn't match. Run <code>rigveda drive connect</code> again.")
                return
            result.update({key: values[0] for key, values in query.items()})
            ok = "code" in result
            self.reply(200 if ok else 400, "Rigveda AI is connected to your Google Drive (read-only). You can close this tab."
                       if ok else f"Sign-in was cancelled ({html.escape(result.get('error', 'no answer'))}). You can close this tab.")

        def reply(self, status: int, message: str) -> None:
            body = f"<!doctype html><meta charset=utf-8><title>Rigveda AI</title><p style='font:16px system-ui;margin:3em'>{message}".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    listener = HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{listener.server_address[1]}"
    url = AUTH_URL + "?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent"})  # "consent" makes Google send a refresh token every time
    worker = threading.Thread(target=lambda: _serve_until(listener, result, timeout), daemon=True)
    worker.start()
    if not open_browser(url):
        print(f"Open this link to sign in:\n{url}")
    worker.join()
    listener.server_close()
    if "code" not in result:
        raise DriveError(f"Sign-in didn't finish: {result.get('error', 'no answer from the browser within 5 minutes')}.")
    if SCOPE not in result.get("scope", SCOPE):
        raise DriveError("Google didn't grant Drive access. Tick “See and download all your Google Drive files” when signing in.")
    tokens = _save_tokens(_post(TOKEN_URL, {**client, "code": result["code"], "code_verifier": verifier,
                                            "redirect_uri": redirect, "grant_type": "authorization_code"}))
    if "refresh_token" not in tokens:
        raise DriveError("Google didn't send a lasting sign-in. Remove Rigveda at myaccount.google.com/permissions and connect again.")
    about = account()
    tokens.update({"email": about["email"], "name": about["name"]})
    _write_private(token_path(), tokens)
    return about


def _serve_until(listener: HTTPServer, result: dict, timeout: float) -> None:
    listener.timeout = 1
    deadline = time.monotonic() + timeout
    while not result and time.monotonic() < deadline:
        listener.handle_request()


def _tokens() -> dict:
    try:
        return json.loads(token_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DriveError("Not connected to Google Drive. Run `rigveda drive connect`.") from None
    except ValueError:
        raise DriveError(f"{token_path()} is damaged. Run `rigveda drive connect` again.") from None


def access_token() -> str:
    """A current access token, renewed from the stored sign-in when it's within a minute of expiring."""
    tokens = _tokens()
    if tokens.get("expires_at", 0) - 60 > time.time():
        return tokens["access_token"]
    reply = _post(TOKEN_URL, {**_client(), "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token"})
    return _save_tokens(reply, tokens)["access_token"]


def api(path: str, params: dict | None = None, attempts: int = 4) -> dict:
    """GET a Drive API resource, retrying when Google asks to slow down."""
    url = API + path + ("?" + urlencode(params) if params else "")
    for attempt in range(attempts):
        request = Request(url, headers={"Authorization": f"Bearer {access_token()}"})
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            if error.code == 401 and attempt == 0:  # token revoked or clock skew: renew and try once more
                tokens = _tokens()
                tokens["expires_at"] = 0
                _write_private(token_path(), tokens)
                continue
            if error.code == 403:
                raise DriveError("Google refused access to Drive. Check that the Drive API is enabled for your project "
                                 "(docs/GOOGLE_DRIVE.md, step 2).") from None
            raise DriveError(f"Drive request failed: {error.code} {error.reason}") from None
        except (URLError, TimeoutError) as error:
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            raise DriveError(f"Couldn't reach Google Drive: {error}") from None
    raise DriveError("Google Drive kept asking to slow down; try again in a minute.")


def _reason(error: HTTPError) -> str:
    try:
        detail = json.load(error)["error"]
        return (detail.get("errors") or [{}])[0].get("reason") or detail.get("status") or detail.get("message", "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return ""


def download(path: str, params: dict, destination: Path, attempts: int = 4) -> int:
    """Stream a file's contents (alt=media) or an export to `destination`. Returns the bytes written."""
    url = API + path + "?" + urlencode(params)
    for attempt in range(attempts):
        request = Request(url, headers={"Authorization": f"Bearer {access_token()}"})
        try:
            with urlopen(request, timeout=120) as response, destination.open("wb") as out:
                written = 0
                while chunk := response.read(2**20):
                    out.write(chunk)
                    written += len(chunk)
                return written
        except HTTPError as error:
            reason = _reason(error)
            if reason == "exportSizeLimitExceeded":
                raise ExportTooLarge(reason) from None
            if (error.code in (429, 500, 502, 503, 504) or reason in ("rateLimitExceeded", "userRateLimitExceeded")) \
                    and attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            if error.code == 401 and attempt == 0:
                tokens = _tokens()
                tokens["expires_at"] = 0
                _write_private(token_path(), tokens)
                continue
            raise DriveError(f"{error.code} {reason or error.reason}") from None
        except (URLError, TimeoutError, ConnectionError) as error:
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            raise DriveError(f"connection lost: {error}") from None
    raise DriveError("Google Drive kept asking to slow down")


def account() -> dict:
    about = api("about", {"fields": "user(displayName,emailAddress),storageQuota(limit,usage,usageInDrive)"})
    quota = about.get("storageQuota", {})
    return {"name": about["user"].get("displayName", ""), "email": about["user"]["emailAddress"],
            "used": int(quota.get("usage", 0)), "used_in_drive": int(quota.get("usageInDrive", 0)),
            "limit": int(quota["limit"]) if quota.get("limit") else None}


def files(progress: Callable[[int], None] | None = None,
          fields: str = "id, name, mimeType, size, modifiedTime, ownedByMe") -> Iterator[dict]:
    """Every file you can see in Drive that isn't in the bin: names, types and sizes only."""
    params = {"q": "trashed = false", "pageSize": 1000, "spaces": "drive", "fields": f"nextPageToken, files({fields})"}
    seen = 0
    while True:
        page = api("files", params)
        for item in page.get("files", []):
            yield item
        seen += len(page.get("files", []))
        if progress:
            progress(seen)
        if not page.get("nextPageToken"):
            return
        params["pageToken"] = page["nextPageToken"]


def category(mime: str) -> str | None:
    """Which survey group a Drive file belongs to; None for folders."""
    if mime == GOOGLE + "folder":
        return None
    if mime in (GOOGLE + "document", GOOGLE + "spreadsheet", GOOGLE + "presentation"):
        return "Google Docs, Sheets & Slides"
    if mime.startswith(GOOGLE):
        return "Other Google items (Forms, Drawings…)"
    if mime == "application/pdf":
        return "PDFs"
    if mime in OFFICE:
        return "Word, Excel & PowerPoint"
    if mime in OLD_OFFICE:
        return "Older Office files (.doc, .xls, .ppt)"
    if mime.startswith("text/") or mime in TEXT:
        return "Text & data"
    if mime.startswith("image/"):
        return "Images"
    if mime.startswith("video/"):
        return "Video"
    if mime.startswith("audio/"):
        return "Audio"
    if mime in ARCHIVES:
        return "Archives"
    return "Other"


def survey(items: Iterator[dict]) -> dict:
    """Counts and sizes per group, split into files you own and files shared with you."""
    groups = {name: {"handling": handling, "files": 0, "bytes": 0, "shared": 0} for name, handling in CATEGORIES.items()}
    folders = 0
    for item in items:
        name = category(item.get("mimeType", ""))
        if name is None:
            folders += 1
            continue
        group = groups[name]
        group["files"] += 1
        group["bytes"] += int(item.get("size") or 0)  # Google Docs have no size
        group["shared"] += not item.get("ownedByMe", True)
    readable = [group for group in groups.values() if group["handling"] == "read"]
    return {"groups": groups, "folders": folders, "files": sum(group["files"] for group in groups.values()),
            "readable_files": sum(group["files"] for group in readable),
            "readable_bytes": sum(group["bytes"] for group in readable)}


def status() -> dict | None:
    """Who Rigveda is signed in as, without contacting Google; None when not connected."""
    try:
        tokens = _tokens()
    except DriveError:
        return None
    return {"email": tokens.get("email", ""), "name": tokens.get("name", ""), "scope": tokens.get("scope", "")}


def disconnect() -> bool:
    """Revoke Rigveda's access at Google and forget the sign-in. The key stays, so connecting again is one step."""
    try:
        tokens = _tokens()
    except DriveError:
        return False
    try:
        _post(REVOKE_URL, {"token": tokens.get("refresh_token") or tokens.get("access_token", "")})
    except DriveError:
        pass  # already revoked at Google, or offline; forgetting it locally is what matters
    token_path().unlink(missing_ok=True)
    return True


def size(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return ""

