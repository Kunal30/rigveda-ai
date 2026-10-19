"""Health of the running app for the status page: services, index size, facts, skipped files and recent log lines,
plus live CPU, GPU and memory use for the orb, overall and per app.

Service state and log lines come from systemd and the journal. Where those aren't available (tests, a manual
`rigveda serve`), the services read as "unknown" and the log sections are empty.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

UNITS = {"rigveda.service": "File watcher", "rigveda-serve.service": "Local server"}
DRIVE_UNIT = "rigveda-drive-index.service"
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
        "drive": drive(con),
    }


def drive(con: sqlite3.Connection) -> dict | None:
    """Google Drive indexing progress, and whether a run is going now; None before Drive was connected."""
    from . import drive_index

    report = drive_index.progress(con)
    if not report["files"]:
        return None
    running = _run("systemctl", "--user", "is-active", DRIVE_UNIT).strip() == "active"
    return {**report, "running": running}


LOG_UNITS = {**UNITS, DRIVE_UNIT: "Google Drive"}
PACE_WINDOW = 300  # seconds of finished files the reading speed is measured over


def activity_log(lines: int = 80) -> list[dict]:
    """Recent messages from every Rigveda service, oldest first, each with the service it came from."""
    out = _run("journalctl", "--user", *(arg for unit in LOG_UNITS for arg in ("-u", unit)), "-n", str(lines * 2),
               "-o", "json", "--output-fields=MESSAGE,_SYSTEMD_USER_UNIT,USER_UNIT", "--no-pager")
    entries = []
    for line in out.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("MESSAGE")
        if not isinstance(message, str) or NOISE.search(message):  # non-text messages arrive as lists of bytes
            continue
        unit = entry.get("USER_UNIT") or entry.get("_SYSTEMD_USER_UNIT") or ""  # USER_UNIT: systemd starting or stopping one
        entries.append({"time": int(entry.get("__REALTIME_TIMESTAMP", 0)) // 1_000_000, "source": LOG_UNITS.get(unit, unit),
                        "message": re.sub(r"(\.service) - .*", r"\1", message).removesuffix(" Press Ctrl+C to stop.")})
    return entries[-lines:]


def activity(con: sqlite3.Connection, now: float | None = None) -> dict:
    """What Rigveda is doing right now: Drive progress with its pace, the files it just read, and the log."""
    now = time.time() if now is None else now
    report = drive(con)
    if report:
        recent = con.execute("SELECT COUNT(*) FROM drive_files WHERE done_at >= ? AND note != '0 bytes'",
                             (int(now) - PACE_WINDOW,)).fetchone()[0]
        left = sum(stage["files"] - stage["done"] for stage in report["stages"])
        rate = recent / PACE_WINDOW
        report["rate"] = round(rate, 2)
        report["eta"] = int(left / rate) if rate and report["running"] else None
        report["recent"] = [dict(row) for row in con.execute(
            "SELECT name, mime, size, state, note, done_at FROM drive_files WHERE done_at IS NOT NULL "
            "AND note != '0 bytes' ORDER BY done_at DESC, rowid DESC LIMIT 12")]
    return {"checked_at": int(now), "drive": report, "log": activity_log()}


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
    return {"cpu": {"percent": cpu_percent(), "threads": _cpus()}, "memory": memory(), "gpu": gpu()}


TICKS = os.sysconf("SC_CLK_TCK")
APPS = 25  # per measure
PROCESSES_PER_APP = 25
PAGE = os.sysconf("SC_PAGE_SIZE")
_procs = {"ticks": {}, "time": 0.0, "cpu": {}}
_procs_lock = threading.Lock()


def _cpus() -> int:
    with open("/proc/stat") as stat:
        return sum(1 for line in stat if line.startswith("cpu") and line[3:4].isdigit()) or 1


def _app_name(command: list[str], comm: str) -> str:
    """What to group a process under: the program's name, or the script's for Python and Node."""
    if not command:
        return "Kernel threads"
    if len(command) == 1 and " " in command[0]:  # Chrome and others rewrite their command line as one string
        command = command[0].split()
    program = command[0].rsplit("/", 1)[-1]
    if program.startswith(("python", "node")) and len(command) > 1:
        script = next((arg for arg in command[1:] if not arg.startswith("-")), "")
        if script:
            return script.rsplit("/", 1)[-1] if script != "-m" else program
    return program or comm


def _read_processes() -> list[dict]:
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    users: dict[int, str] = {}
    result = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/stat") as file:
                raw = file.read()
            with open(f"/proc/{entry.name}/statm") as file:
                rss = int(file.read().split()[1]) * PAGE
            with open(f"/proc/{entry.name}/cmdline", "rb") as file:
                command = [part.decode(errors="replace") for part in file.read().split(b"\0") if part]
            uid = entry.stat().st_uid
        except (OSError, ValueError, IndexError):
            continue  # the process ended while being read
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        fields = raw[raw.rindex(")") + 2:].split()
        if uid not in users:
            try:
                users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                users[uid] = str(uid)
        result.append({"pid": int(entry.name), "name": _app_name(command, comm), "state": fields[0],
                       "ticks": int(fields[11]) + int(fields[12]), "threads": int(fields[17]),
                       "started": int(boot + int(fields[19]) / TICKS), "rss": rss, "user": users[uid],
                       "command": " ".join(command)[:300] or f"[{comm}]"})
    return result


def gpu_processes() -> dict[int, dict]:
    """GPU compute use (%) and memory (bytes) per process id, from nvidia-smi."""
    usage = {}
    for line in _run("nvidia-smi", "pmon", "-c", "1", "-s", "um").splitlines():
        parts = line.split()
        if line.startswith("#") or len(parts) < 10 or not parts[1].isdigit():
            continue
        busy = float(parts[3]) if parts[3] != "-" else 0.0
        memory = int(parts[9]) * 2**20 if parts[9].isdigit() else 0
        usage[int(parts[1])] = {"gpu": busy, "gpu_memory": memory}
    return usage


def processes() -> dict:
    """Every process with its CPU use since the last call (% of the whole machine), memory and GPU use, grouped
    by app, biggest CPU users first."""
    with _procs_lock:
        if not _procs["ticks"]:
            _procs["ticks"], _procs["time"] = {proc["pid"]: proc["ticks"] for proc in _read_processes()}, time.monotonic()
            time.sleep(0.25)
        now = time.monotonic()
        current = _read_processes()
        elapsed = now - _procs["time"]
        if elapsed >= 0.5 or not _procs["cpu"]:  # several viewers polling at once share a reading
            capacity = elapsed * TICKS * _cpus()
            _procs["cpu"] = {proc["pid"]: round(100 * max(0, proc["ticks"] - _procs["ticks"].get(proc["pid"], proc["ticks"])) / capacity, 1)
                             for proc in current}
            _procs["ticks"], _procs["time"] = {proc["pid"]: proc["ticks"] for proc in current}, now
        cpu = _procs["cpu"]
    total = memory()["total"]
    gpu_use = gpu_processes()
    apps: dict[str, dict] = {}
    for proc in current:
        proc.pop("ticks")
        proc["cpu"] = cpu.get(proc["pid"], 0.0)
        proc["memory"] = round(100 * proc["rss"] / total, 2)
        proc.update(gpu_use.get(proc["pid"], {"gpu": 0.0, "gpu_memory": 0}))
        app = apps.setdefault(proc["name"], {"name": proc["name"], "cpu": 0.0, "rss": 0, "memory": 0.0, "gpu": 0.0,
                                             "gpu_memory": 0, "processes": []})
        for key in ("cpu", "rss", "memory", "gpu", "gpu_memory"):
            app[key] += proc[key]
        app["processes"].append(proc)
    for app in apps.values():
        app["cpu"], app["memory"] = round(app["cpu"], 1), round(app["memory"], 2)
        app["processes"].sort(key=lambda proc: (-proc["cpu"], -proc["rss"]))
    # The busiest apps by each measure; the long tail of idle helpers isn't worth sending every 2 seconds.
    keep = {}
    for key in ("cpu", "rss", "gpu", "gpu_memory"):
        for app in sorted(apps.values(), key=lambda app: -app[key])[:APPS]:
            keep[app["name"]] = app
    for app in keep.values():
        app["count"] = len(app["processes"])
        app["processes"] = app["processes"][:PROCESSES_PER_APP]
    return {"cpus": _cpus(), "memory_total": total, "count": len(current), "apps_total": len(apps),
            "apps": sorted(keep.values(), key=lambda app: (-app["cpu"], -app["rss"]))}
