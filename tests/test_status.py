import json
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from rigveda import facts, status
from rigveda.server import ServerConfig, make_handler
from rigveda.store import connect


@pytest.fixture
def con(config, tmp_path):
    con = connect(config.database_path)
    unreadable = tmp_path / "locked.pdf"
    unreadable.write_bytes(b"%PDF")
    now = int(time.time())
    with con:
        for name, when in [("a.txt", now), ("b.txt", now), ("c.pdf", now - 3 * 86400), ("old.txt", now - 90 * 86400)]:
            con.execute("INSERT INTO documents(path, modified_ns, size, indexed_at, kind) VALUES (?, 0, 1, ?, ?)",
                        (str(tmp_path / name), when, "pdf" if name.endswith("pdf") else "text"))
    facts.add_manual(con, "given_name", "Kunal")
    with con:
        con.execute("INSERT INTO facts(key, value, norm, status, created_at) VALUES ('email', 'x@y.z', 'x@y.z', 'pending', ?)", (now,))
    return con, unreadable, tmp_path


def test_collect_counts_files_facts_and_days(con, monkeypatch) -> None:
    con, unreadable, folder = con
    monkeypatch.setattr(status, "journal", lambda: [
        f"2026-10-16T23:27:41-07:00  Skipping {unreadable}: ValueError: document closed or encrypted",
        f"2026-10-16T23:27:41-07:00  Skipping {folder / 'a.txt'}: read later",  # indexed since, so no longer skipped
        f"2026-10-16T23:27:41-07:00  Skipping {folder / 'gone.pdf'}: deleted since",
        "2026-10-16T23:27:41-07:00  Initial index: 0 updated, 4 unchanged, 3 skipped, 0 removed"])
    monkeypatch.setattr(status, "_run", lambda *command: "")  # no systemd in tests
    report = status.collect(con, "http://127.0.0.1:9")  # nothing listens there
    assert report["documents"]["total"] == 4 and report["documents"]["kinds"] == {"text": 3, "pdf": 1}
    assert report["facts"] == {"pending": 1, "approved": 1, "rejected": 0}
    assert report["skipped"] == [{"path": str(unreadable), "reason": "ValueError: document closed or encrypted"}]
    assert [s["state"] for s in report["services"]] == ["unknown", "unknown"] and report["ollama"]["state"] == "failed"
    days = report["indexed_per_day"]
    assert len(days) == status.DAYS and days[-1]["count"] == 2 and days[-4]["count"] == 1 and sum(d["count"] for d in days) == 3
    assert report["facts_per_day"][-1]["count"] == 2
    assert len(report["log"]) == 4


def test_status_page_and_api_need_no_token(con, config, monkeypatch) -> None:
    monkeypatch.setattr(status, "_run", lambda *command: "")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ServerConfig(config.database_path, "m", "http://127.0.0.1:9", 5, token="s3cret")))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        for path, kind in [("/status", "text/html"), ("/api/status", "application/json")]:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read()
            assert response.status == 200 and response.getheader("Content-Type").startswith(kind)
        assert json.loads(body)["documents"]["total"] == 4
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_orb_page_is_served_with_its_window_flag(config) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ServerConfig(config.database_path, "m", "h", 5)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1])
        conn.request("GET", "/orb?native=1")
        response = conn.getresponse()
        assert response.status == 200 and b"/api/status" in response.read()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_system_reports_cpu_memory_and_gpu(monkeypatch) -> None:
    monkeypatch.setattr(status, "_run", lambda *command: "NVIDIA GeForce RTX 5090, 13, 16794, 32607, 31\n")
    report = status.system()
    assert 0 <= report["cpu"]["percent"] <= 100 and report["cpu"]["threads"] >= 1
    assert 0 < report["memory"]["used"] < report["memory"]["total"] and 0 < report["memory"]["percent"] < 100
    assert report["gpu"] == {"name": "RTX 5090", "percent": 13.0, "memory_used": 16794 * 2**20,
                             "memory_total": 32607 * 2**20, "temperature": 31.0}
    monkeypatch.setattr(status, "_run", lambda *command: "")  # no NVIDIA driver
    assert status.system()["gpu"] is None


def test_processes_are_grouped_by_app_with_cpu_memory_and_gpu(monkeypatch) -> None:
    import os
    monkeypatch.setattr(status, "_run", lambda *command: (
        "# gpu pid type sm mem enc dec jpg ofa fb ccpm command\n"
        f"    0 {os.getpid()} C 37 5 - - - - 2048 0 python\n"))
    report = status.processes()
    me = next(proc for app in report["apps"] for proc in app["processes"] if proc["pid"] == os.getpid())
    assert me["gpu"] == 37.0 and me["gpu_memory"] == 2048 * 2**20 and me["rss"] > 0 and me["threads"] >= 1
    assert me["state"] in "RSD" and "pytest" in me["command"] and me["user"]
    app = next(app for app in report["apps"] if me in app["processes"])
    assert app["count"] >= 1 and app["gpu_memory"] >= me["gpu_memory"]
    assert report["count"] >= len(report["apps"]) and report["cpus"] >= 1
    assert status._app_name(["/usr/bin/python3", "/x/orb.py", "8765"], "python3") == "orb.py"
    assert status._app_name([], "kworker/0:1") == "Kernel threads"
    assert status._app_name(["/opt/google/chrome/chrome --type=gpu-process --render-node-override=/dev/dri/renderD128"], "chrome") == "chrome"


def test_activity_reports_drive_pace_recent_files_and_every_service_log(con, monkeypatch) -> None:
    con, _, _ = con
    now = 1_800_000_000
    with con:
        con.executemany("INSERT INTO drive_files(id, name, mime, size, modified, link, state, note, done_at) VALUES (?, ?, ?, ?, '', '', ?, ?, ?)",
                        [("a", "Visa.pdf", "application/pdf", 10, "indexed", "", now - 10),
                         ("b", "broken.zip", "application/zip", 20, "failed", "BadZipFile", now - 5),
                         ("c", "empty.txt", "text/plain", 0, "empty", "0 bytes", now - 1),
                         ("d", "later.pdf", "application/pdf", 30, "pending", "", None)])
    lines = [{"__REALTIME_TIMESTAMP": str(now * 10**6), "_SYSTEMD_USER_UNIT": "rigveda-drive-index.service", "MESSAGE": "Drive: reading documents and emails"},
             {"__REALTIME_TIMESTAMP": str(now * 10**6), "_SYSTEMD_USER_UNIT": "init.scope", "USER_UNIT": "rigveda.service",
              "MESSAGE": "Stopped rigveda.service - Rigveda AI"},
             {"__REALTIME_TIMESTAMP": str(now * 10**6), "_SYSTEMD_USER_UNIT": "rigveda.service", "MESSAGE": [1, 2]}]
    monkeypatch.setattr(status, "_run", lambda *command: "active" if "is-active" in command
                        else "\n".join(json.dumps(line) for line in lines))
    report = status.activity(con, now=now)
    drive = report["drive"]
    assert drive["running"] and drive["rate"] == round(2 / status.PACE_WINDOW, 2) and drive["eta"] is not None
    assert [file["name"] for file in drive["recent"]] == ["broken.zip", "Visa.pdf"]  # newest first, 0-byte files left out
    assert report["log"] == [{"time": now, "source": "Google Drive", "message": "Drive: reading documents and emails"}]
