import gzip
import io
import shutil
import tarfile
import zipfile
import zlib
from email.message import EmailMessage
from pathlib import Path

import pytest

from rigveda import drive_index, extract, gdrive
from rigveda.search import cite, search
from rigveda.store import connect

# ── readers ──


def texts(path: Path, config) -> list[tuple[str, str]]:
    return [(segment.location, segment.text) for segment in extract.extract_any(path, config)[1]]


def test_email_headers_html_body_and_attachments(tmp_path, config) -> None:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "bank@example.com", "kunal@example.com", "Your statement"
    message.set_content("plain version")
    message.add_alternative("<html><head><style>p{}</style></head><body><p>Balance due: $412.08</p></body></html>", subtype="html")
    message.add_attachment(b"Account 1234: closing balance 412.08", maintype="text", subtype="plain", filename="statement.txt")
    path = tmp_path / "mail.eml"
    path.write_bytes(bytes(message))
    found = texts(path, config)
    assert found[0][0] == "email" and "Subject: Your statement" in found[0][1] and "plain version" in found[0][1]
    assert ("attachment statement.txt", "Account 1234: closing balance 412.08") in found
    html_only = EmailMessage()
    html_only["Subject"] = "Visa"
    html_only.set_content("<p>Interview on <b>May 3</b></p><script>x()</script>", subtype="html")
    path.write_bytes(bytes(html_only))
    assert "Interview on May 3" in texts(path, config)[0][1] and "x()" not in texts(path, config)[0][1]


def test_archives_read_every_member_by_its_type(tmp_path, config) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as nested:
        nested.writestr("deep/note.md", "nested note")
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("docs/readme.txt", "lease ends in June")
        archive.writestr("inner.zip", inner.getvalue())
        archive.writestr("lib/App.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x37 com/example/PaymentService \x00\x01")
    found = texts(path, config)
    assert ("docs/readme.txt", "lease ends in June") in found
    assert ("inner.zip, deep/note.md", "nested note") in found
    assert any(location == "lib/App.class" and "com/example/PaymentService" in text for location, text in found)

    gz = tmp_path / "notes.txt.gz"
    gz.write_bytes(gzip.compress(b"gzipped words"))
    assert texts(gz, config) == [("notes.txt", "gzipped words")]
    tar = tmp_path / "backup.tar.gz"
    with tarfile.open(tar, "w:gz") as archive:
        data = b"tarred words"
        info = tarfile.TarInfo("a/b.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    assert texts(tar, config) == [("a/b.txt", "tarred words")]


def test_archives_inside_archives_stop_at_two_levels(tmp_path, config) -> None:
    payload = b""
    for level in range(4):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"level{level}.zip" if payload else "core.txt", payload or b"the core")
        payload = buffer.getvalue()
    path = tmp_path / "deep.zip"
    path.write_bytes(payload)
    assert texts(path, config) == []  # the text sits four archives down


def test_unknown_files_give_up_their_text(tmp_path, config) -> None:
    plain = tmp_path / "NOTES"
    plain.write_text("no extension but plain text")
    assert texts(plain, config) == [("", "no extension but plain text")]
    blob = tmp_path / "3f2a9c"  # a git loose object
    blob.write_bytes(zlib.compress(b"blob 21\x00def pay_rent(): pass"))
    assert texts(blob, config) == [("", "def pay_rent(): pass")]
    dex = tmp_path / "classes.dex"
    dex.write_bytes(b"dex\n035\x00\x01\x02Lcom/bank/TransferActivity;\x00\x03ab\x00")
    assert extract.extract_any(dex, config)[0] == "binary" and "Lcom/bank/TransferActivity;" in texts(dex, config)[0][1]


def test_subtitles_keep_only_the_words(tmp_path, config) -> None:
    path = tmp_path / "film.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi.\n")
    assert extract.extract_any(path, config) [0] == "subtitles" and texts(path, config) == [("", "Hello there.\nGeneral Kenobi.")]


@pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")), reason="LibreOffice not installed")
def test_old_office_files_are_converted(tmp_path, config) -> None:
    path = tmp_path / "letter.rtf"
    path.write_text(r"{\rtf1\ansi Offer letter: start date August 4.}")
    assert any("start date August 4" in text for _, text in texts(path, config))


# ── indexing Drive ──

DRIVE = {
    "doc1": {"name": "Visa notes", "mimeType": "application/vnd.google-apps.document", "body": b"H1B interview on May 3"},
    "big1": {"name": "Huge doc", "mimeType": "application/vnd.google-apps.document", "body": b"exported as text because it is huge"},
    "pdf1": {"name": "W2 2025.txt", "mimeType": "text/plain", "body": b"Wages 123,456"},
    "eml1": {"name": "", "mimeType": "message/rfc822", "body": b"Subject: Offer\n\nWe are pleased to offer you the role."},
    "frm1": {"name": "Survey", "mimeType": "application/vnd.google-apps.form", "body": b""},
    "nil1": {"name": "__init__.py", "mimeType": "text/x-python", "body": b""},  # 0 bytes: never downloaded
    "vid1": {"name": "clip.mp4", "mimeType": "video/mp4", "body": None},  # download refused
    "img1": {"name": "scan.zz", "mimeType": "image/zz", "body": b"\x89zz\x00 receipt TOTAL-42"},
}


@pytest.fixture
def drive(monkeypatch):
    files = {file_id: dict(item, modifiedTime="2026-10-01T10:00:00.000Z") for file_id, item in DRIVE.items()}

    def listing(progress=None, fields=""):
        for file_id, item in files.items():
            yield {"id": file_id, "name": item["name"], "mimeType": item["mimeType"], "modifiedTime": item["modifiedTime"],
                   "size": str(len(item["body"] if item["body"] is not None else b"x")),
                   "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}

    def download(path, params, destination, attempts=4):
        file_id = path.split("/")[1]
        if path.endswith("/export") and file_id == "big1" and params["mimeType"] != "text/plain":
            raise gdrive.ExportTooLarge("exportSizeLimitExceeded")
        body = files[file_id]["body"]
        assert file_id != "nil1"
        if body is None:
            raise gdrive.DriveError("403 cannotDownloadFile")
        destination.write_bytes(body)
        return len(body)

    monkeypatch.setattr(gdrive, "files", listing)
    monkeypatch.setattr(gdrive, "download", download)
    return files


@pytest.fixture
def con(config, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_index, "cache_dir", lambda: tmp_path)
    return connect(config.database_path)


def states(con) -> dict[str, str]:
    return {row["id"]: row["state"] for row in con.execute("SELECT id, state FROM drive_files")}


def test_drive_files_are_read_in_place_and_searchable(drive, con, config, tmp_path) -> None:
    from docx import Document  # the Visa notes export is a real .docx

    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph("H1B interview on May 3")
    document.save(buffer)
    drive["doc1"]["body"] = buffer.getvalue()

    assert drive_index.refresh(con, print) == {"files": 8, "added": 8, "changed": 0, "removed": 0}
    counts = drive_index.run(con, config, print)
    assert states(con) == {"doc1": "indexed", "big1": "indexed", "pdf1": "indexed", "eml1": "indexed",
                           "frm1": "skipped", "vid1": "failed", "img1": "indexed", "nil1": "empty"}
    assert counts == {"indexed": 5, "skipped": 1, "failed": 1, "empty": 1}
    assert not list(tmp_path.glob("run-*"))  # downloads are gone
    hits = search(config.database_path, "interview", 5)
    assert hits and hits[0]["path"] == "gdrive://doc1/Visa notes" and cite(hits[0]).startswith("Google Drive: Visa notes")
    kinds = dict(con.execute("SELECT path, kind FROM documents").fetchall())
    assert kinds["gdrive://eml1/"] == "email" and kinds["gdrive://img1/scan.zz"] == "binary"
    assert "role" in search(config.database_path, "pleased offer", 5)[0]["text"]


def test_changes_and_removals_follow_drive(drive, con, config) -> None:
    drive_index.refresh(con, print)
    drive_index.run(con, config, print)
    drive["pdf1"].update(body=b"Wages 200,000", modifiedTime="2026-10-02T10:00:00.000Z")
    del drive["eml1"]
    assert drive_index.refresh(con, print) == {"files": 7, "added": 0, "changed": 1, "removed": 1}
    assert states(con)["pdf1"] == "pending" and "eml1" not in states(con)
    assert con.execute("SELECT COUNT(*) FROM documents WHERE path='gdrive://eml1/'").fetchone()[0] == 0
    drive_index.run(con, config, print)
    assert "200,000" in search(config.database_path, "Wages", 5)[0]["text"]


def test_documents_come_before_media_and_images(drive, con, config) -> None:
    drive_index.refresh(con, print)
    order = []
    original = drive_index.read
    drive_index.read = lambda con, file, downloaded, config: order.append(file["id"]) or original(con, file, downloaded, config)
    try:
        drive_index.run(con, config, print)
    finally:
        drive_index.read = original
    assert order.index("vid1") > max(order.index(i) for i in ("doc1", "pdf1", "eml1")) and order[-1] == "img1"
    assert drive_index.progress(con)["stages"][3] == {"name": "images", "files": 1, "done": 1}


def test_stopping_deletes_downloads(drive, con, config, tmp_path) -> None:
    import os
    import signal

    drive_index.refresh(con, print)
    original = drive_index.read

    def stop_midway(con, file, downloaded, config):
        downloaded.result()
        os.kill(os.getpid(), signal.SIGTERM)  # what `systemctl stop` does
        return original(con, file, downloaded, config)

    drive_index.read = stop_midway
    try:
        with pytest.raises(SystemExit):
            drive_index.run(con, config, print)
    finally:
        drive_index.read = original
    assert not list(tmp_path.glob("run-*")) and signal.getsignal(signal.SIGTERM) is not drive_index._stop
