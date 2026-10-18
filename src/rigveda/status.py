"""Health of the running app for the status page: services, index size, facts, skipped files and recent log lines,
plus live CPU, GPU and memory use for the orb.

Service state and log lines come from systemd and the journal. Where those aren't available (tests, a manual
`rigveda serve`), the services read as "unknown" and the log sections are empty.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

UNITS = {"rigveda.service": "File watcher", "rigveda-serve.service": "Local server"}
DAYS = 30  # how far back the activity charts go
SKIP = re.compile(r"^Skipping (?P<path>/.+?): (?P<reason>.+)$")
NOISE = re.compile(r"^(Stopping|Stopped) |: Consumed ")  # systemd bookkeeping around every restart


def _run(*command: str) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def services() -> list[dict]:
    """Each systemd user service: running, stopped or failed, since when, and how often it restarted."""
    result = []
    for unit, label in UNITS.items():
        props = dict(line.split("=", 1) for line in
                     _run("systemctl", "--user", "show", unit, "-p", "ActiveState,ActiveEnterTimestamp,NRestarts").splitlines()
                     if "=" in line)
        result.append({"unit": unit, "label": label, "state": props.get("ActiveState") or "unknown",
                       "since": props.get("ActiveEnterTimestamp", ""), "restarts": int(props.get("NRestarts") or 0)})
    return result


def ollama(host: str) -> dict:
    """Whether the local model server answers; facts scanning and answers need it."""
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=2) as response:
            models = [model["name"] for model in json.load(response).get("models", [])]
        return {"state": "active", "models": models}
    except (OSError, ValueError):
        return {"state": "failed", "models": []}


def journal(lines: int = 300) -> list[str]:
    """Recent messages from both services, oldest first, as "time  message"."""
    out = _run("journalctl", "--user", *(arg for unit in UNITS for arg in ("-u", unit)), "-n", str(lines),
               "-o", "short-iso", "--no-pager")
    entries = []
    for line in out.splitlines():
        parts = line.split(" ", 3)  # time, host, process, message
        if len(parts) == 4 and parts[0][:1].isdigit() and not NOISE.search(parts[3]):
            message = re.sub(r"(\.service) - .*", r"\1", parts[3]).removesuffix(" Press Ctrl+C to stop.")
            entries.append(parts[0] + "  " + message)
    return entries


def skipped(con: sqlite3.Connection, entries: list[str]) -> list[dict]:
    """Files the watcher couldn't read and that are still missing from the index."""
    found = {}
    for entry in entries:
        match = SKIP.match(entry.split("  ", 1)[-1])
        if match:
            found[match["path"]] = match["reason"]
    indexed = {row[0] for row in con.execute("SELECT path FROM documents")}
    return [{"path": path, "reason": reason} for path, reason in found.items()
            if path not in indexed and Path(path).exists()]


def per_day(con: sqlite3.Connection, sql: str, now: float) -> list[dict]:
    """Counts per local calendar day for the last DAYS days, oldest first, zeros included."""
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    days = [time.strftime("%Y-%m-%d", time.localtime(today - 86400 * back)) for back in range(DAYS - 1, -1, -1)]
    counts = dict(con.execute(sql, (today - 86400 * (DAYS - 1),)).fetchall())
    return [{"day": day, "count": counts.get(day, 0)} for day in days]


def collect(con: sqlite3.Connection, ollama_host: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    entries = journal()
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM documents GROUP BY kind ORDER BY COUNT(*) DESC").fetchall())
    statuses = dict(con.execute("SELECT status, COUNT(*) FROM facts GROUP BY status").fetchall())
    last = con.execute("SELECT MAX(indexed_at) FROM documents").fetchone()[0]
    return {
        "checked_at": int(now),
        "services": services(),
        "ollama": ollama(ollama_host),
        "documents": {"total": sum(kinds.values()), "kinds": kinds, "last_indexed": last,
                      "chunks": con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                      "scanned": con.execute("SELECT COUNT(*) FROM fact_scans").fetchone()[0]},
        "facts": {status: statuses.get(status, 0) for status in ("pending", "approved", "rejected")},
        "skipped": skipped(con, entries),
        "indexed_per_day": per_day(con, "SELECT date(indexed_at, 'unixepoch', 'localtime') AS day, COUNT(*) FROM documents "
                                        "WHERE indexed_at >= ? GROUP BY day", now),
        "facts_per_day": per_day(con, "SELECT date(created_at, 'unixepoch', 'localtime') AS day, COUNT(*) FROM facts "
                                      "WHERE created_at >= ? GROUP BY day", now),
        "log": entries[-15:],
    }


_cpu = {"sample": None, "time": 0.0, "percent": 0.0}
_cpu_lock = threading.Lock()


def _cpu_times() -> tuple[int, int]:
    """(idle, total) jiffies across all CPUs since boot."""
    with open("/proc/stat") as stat:
        values = [int(value) for value in stat.readline().split()[1:]]
    return values[3] + values[4], sum(values[:8])  # idle + iowait; user … steal


def cpu_percent() -> float:
    """CPU use since the previous call, measured over at least 0.2 s so the first reading isn't empty."""
    with _cpu_lock:
        if _cpu["sample"] is None:
            _cpu["sample"] = _cpu_times()
            time.sleep(0.2)
        if time.monotonic() - _cpu["time"] >= 0.5:  # several viewers polling at once share a reading
            idle, total = _cpu_times()
            last_idle, last_total = _cpu["sample"]
            if total > last_total:
                _cpu["percent"] = round(100 * (1 - (idle - last_idle) / (total - last_total)), 1)
            _cpu["sample"], _cpu["time"] = (idle, total), time.monotonic()
        return _cpu["percent"]


def memory() -> dict:
    info = {}
    with open("/proc/meminfo") as meminfo:
        for line in meminfo:
            name, value = line.split(":", 1)
            info[name] = int(value.split()[0]) * 1024
    total, available = info["MemTotal"], info.get("MemAvailable", info["MemFree"])
    return {"total": total, "used": total - available, "percent": round(100 * (total - available) / total, 1)}


def gpu() -> dict | None:
    """The first NVIDIA GPU's use, memory and temperature, or None without one."""
    out = _run("nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
               "--format=csv,noheader,nounits").strip().splitlines()
    try:
        name, percent, used, total, temperature = [part.strip() for part in out[0].split(",")]
        return {"name": name.removeprefix("NVIDIA ").removeprefix("GeForce "), "percent": float(percent),
                "memory_used": int(used) * 2**20, "memory_total": int(total) * 2**20, "temperature": float(temperature)}
    except (IndexError, ValueError):
        return None


def system() -> dict:
    return {"cpu": {"percent": cpu_percent(), "threads": os.cpu_count()}, "memory": memory(), "gpu": gpu()}
