"""Questions about this computer itself ("how much disk space is left?", "what's using my memory?", "is the GPU
busy?"), answered from commands that only look: the local model picks a few from a list, each is checked against
what that program may be asked without changing anything, run without a shell, and the local model says what the
output means in a sentence or two. Nothing here leaves the computer.
"""

from __future__ import annotations

import glob
import json
import mimetypes
import os
import re
import shlex
import subprocess
from functools import lru_cache
from pathlib import Path
from shutil import which

from .config import Config

mimetypes.add_type("video/x-matroska", ".mkv")  # not in Python's own list
mimetypes.add_type("audio/x-matroska", ".mka")
ANY = re.compile(r".*", re.S)
NONE = re.compile(r"")


def _first(*allowed: str, options: str = r"(?:-[-\w=,.]+ )*") -> re.Pattern:
    """Arguments whose first word (after options) is one of `allowed`: "systemctl status x", not "systemctl stop x"."""
    return re.compile(rf"{options}(?:{'|'.join(allowed)})(?: .*)?", re.S)


def _without(*forbidden: str) -> re.Pattern:
    """Any arguments but these, which write, delete or run something ("find -delete", "journalctl --vacuum-size")."""
    return re.compile(rf"(?!.*(?:^| )(?:{'|'.join(forbidden)}))(?:.*)", re.S)


# What each program may be asked: a pattern for all its arguments, joined by spaces. Programs not here never run.
READ_ONLY: dict[str, re.Pattern] = {
    **{name: ANY for name in ("df", "free", "uptime", "uname", "lsblk", "lscpu", "lsusb", "lspci", "lsmod", "findmnt",
                              "ls", "stat", "ps", "pgrep", "sensors", "whoami", "id", "cal", "lsb_release",
                              "nproc", "which", "whatis", "who", "wc", "getent", "groups", "vmstat",
                              "iostat", "lsof", "pidof")},
    "ss": _without(r"-K", r"--kill"),
    "file": _without(r"-C", r"--compile"),
    "sensors": _without(r"-s", r"--set"),
    "date": _without(r"-s", r"--set\S*"),
    "hostname": NONE,
    "du": _without(r"/\*?(?= |$)"),  # not the whole disk: minutes, to say what's at home
    "find": _without(r"/\*?(?= |$)", r"-delete", r"-exec\S*", r"-ok\S*", r"-fprint\S*", r"-fls"),
    "journalctl": _without(r"--vacuum\S*", r"--rotate", r"--flush", r"--sync", r"--relinquish-var", r"--setup-keys",
                           r"--update-catalog", r"-f", r"--follow"),
    "systemctl": _first("status", "list-units", "list-timers", "list-unit-files", "is-active", "is-enabled",
                        "is-failed", "show", "cat", "--failed"),
    "ip": re.compile(r"(?:-\w+ )*(?:a|addr|address|r|route|l|link|n|neigh)(?: show(?: .*)?)?"),
    "nmcli": re.compile(r"(?:(?:-t|--terse|-f \S+|--fields \S+) )*(?:general(?: status)?|radio|networking|"
                        r"device(?: status| show(?: .*)?| wifi list(?: .*)?)?|connection(?: show(?: .*)?)?)"),
    "nvidia-smi": re.compile(r"(?:(?:--query-gpu=\S+|--query-compute-apps=\S+|--format=\S+|-q|-L|--list-gpus)(?: |$))*"),
    "upower": re.compile(r"-e|--enumerate|-d|--dump|(?:-i|--show-info) \S+"),
    "dpkg": _first("-l", "--list", "-L", "--listfiles", "-s", "--status", "-S", "--search", options=""),
    "apt": re.compile(r"list(?: --upgradable| --installed)*(?: \S+)?"),
    "snap": _first("list", "info", "version", options=""),
    "flatpak": _first("list", "info", options=""),
    "timedatectl": re.compile(r"(?:status)?"),
    "hostnamectl": re.compile(r"(?:status)?"),
    "localectl": re.compile(r"(?:status)?"),
    "loginctl": _first("list-sessions", "list-users", "show-session", "session-status", options=""),
    "pactl": _first("info", "list", r"get-\S+", options=""),
    "wpctl": _first("status", "get-volume", options=""),
    "playerctl": _first("status", "metadata", "-l", "--list-all", options=r"(?:-p \S+ |--player=\S+ )*"),
    "gsettings": _first("get", "list-keys", options=""),
    "xdg-mime": _first("query", options=""),
    "cat": re.compile(r"(?:(?:/proc|/sys)/(?!\S*(?:environ|kcore|kmsg|/mem\b|\.\.))\S+ ?)+"),
}
HWMON = Path("/sys/class/hwmon")
WHAT = {"k10temp": "CPU (AMD)", "coretemp": "CPU (Intel)", "amdgpu": "graphics built into the CPU", "nvme": "SSD",
        "acpitz": "motherboard", "gigabyte_wmi": "motherboard", "iwlwifi_1": "Wi-Fi card"}


def temperatures() -> str:
    """Every temperature the hardware reports, in degrees Celsius, by what it's of; and the NVIDIA GPU's."""
    lines = []
    for monitor in sorted(HWMON.glob("hwmon*")):
        try:
            name = (monitor / "name").read_text().strip()
        except OSError:
            continue
        for sensor in sorted(monitor.glob("temp*_input")):
            try:
                degrees = int(sensor.read_text()) / 1000
                label = (sensor.with_name(sensor.name.replace("_input", "_label"))).read_text().strip()
            except OSError:
                label = sensor.name.removesuffix("_input")
            except ValueError:
                continue
            lines.append(f"{WHAT.get(name, name)} {label}: {degrees:.0f} °C")
    gpu = _run(["nvidia-smi", "--query-gpu=name,temperature.gpu", "--format=csv,noheader"]) if which("nvidia-smi") else ""
    if gpu and not gpu.startswith("("):
        lines += [f"GPU {line.split(',')[0].strip()}: {line.split(',')[-1].strip()} °C" for line in gpu.splitlines()]
    return "\n".join(lines) or "(no temperatures reported)"


# Checks done here rather than by a program: what each is for, and how.
BUILT_IN = {"temperatures": ("temperatures: every temperature sensor (CPU, GPU, SSD, motherboard) in degrees Celsius",
                             temperatures)}
MOST_COMMANDS = 3
MOST_OUTPUT = 3000  # characters of each command's output the model reads
SECONDS = 10

CHOOSE_FORMAT = {"type": "object", "properties": {"commands": {"type": "array", "items": {"type": "string"}}},
                 "required": ["commands"]}
CHOOSE_PROMPT = """A question about this Linux computer (Ubuntu, GNOME on Wayland, an NVIDIA GPU) is to be answered by running up to {most} commands that only look at it. The programs that may be run, and what each is for:
{programs}

Rules: no pipes, redirection, sudo or shell features; one program and its arguments per command; prefer human-readable output (-h); ~ is the home folder, and * matches files (du -sh ~/* for what's biggest at home); for temperatures use temperatures; for what failed, journalctl -b -p err -n 30 and systemctl --failed; never all of / with du or find. Allowed forms: systemctl status/list-units/is-active/show; ip addr/route; nmcli device/connection show; nvidia-smi --query-gpu=...--format=csv; apt list --upgradable; cat only of /proc or /sys; ps with --sort and -o.
Question: {question}
Reply with JSON: the commands to run, in order. If no command here can answer it, reply with none."""
ANSWER_PROMPT = """You are answering, aloud, a question about the user's own computer, from the output of commands run on it just now.
Question: {question}

{outputs}

Answer in one short sentence to be spoken (two only if the question needs it): round numbers and say units in words (gigabytes, percent), no paths, markdown or command names unless asked. Say only what the output shows; never guess or add what it doesn't say. If the answer is a long list, give how many and the top few. If the output doesn't answer the question, say so plainly."""


@lru_cache(maxsize=None)
def described(program: str) -> str:
    """What a program is for, in its manual's words ("df - report file system space usage")."""
    if program in BUILT_IN:
        return BUILT_IN[program][0]
    try:
        said = subprocess.run(["whatis", "-l", program], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return program
    line = next((line for line in said.splitlines() if line.startswith(program + " ")), "")
    return f"{program}: {line.split(' - ', 1)[1].strip()}" if " - " in line else program


def programs() -> list[str]:
    """The programs in READ_ONLY that are installed here."""
    return [*BUILT_IN, *(name for name in READ_ONLY if which(name))]


def allowed(command: str) -> list[str] | None:
    """The command as arguments to run, if it only looks; None if it isn't allowed. No shell ever sees it. What the
    model adds out of habit is dropped, not run: a leading sudo, and anything piped after the first command ("ps
    --sort -%mem | head": the output is cut short here anyway)."""
    command = re.sub(r"^\s*sudo\s+", "", command.split(" | ", 1)[0])
    if re.search(r"[|;&<>`$\\]|\bsudo\b", command):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if parts in ([name] for name in BUILT_IN):
        return parts
    if not parts or parts[0] not in READ_ONLY:
        return None
    if not READ_ONLY[parts[0]].fullmatch(" ".join(parts[1:])):
        return None
    home, arguments = str(Path.home()), [parts[0]]
    for part in parts[1:]:
        part = re.sub(r"^~(?=/|$)", home, part)
        arguments += sorted(glob.glob(part))[:200] or [part] if re.search(r"[*?]", part) else [part]  # "du -sh ~/*"
    return arguments


def _run(arguments: list[str], most: int = MOST_OUTPUT) -> str:
    """What a command printed (cut short, for the model to read), or why it didn't."""
    if arguments[0] in BUILT_IN:
        return BUILT_IN[arguments[0]][1]()
    try:
        done = subprocess.run(arguments, capture_output=True, text=True, timeout=SECONDS, stdin=subprocess.DEVNULL,
                              env={**os.environ, "LANG": "C", "LC_ALL": "C", "SYSTEMD_PAGER": "", "PAGER": "cat"})
    except subprocess.TimeoutExpired:
        return "(took too long; stopped)"
    except OSError as error:
        return f"(couldn't run: {error})"
    output = done.stdout.strip() or done.stderr.strip() or "(no output)"
    return output[:most] + ("\n(… cut short)" if len(output) > most else "")


def check(question: str, config: Config, log=print) -> str:
    """Answer a question about this computer from read-only commands; what to say."""
    from . import llm

    listed = "\n".join(described(name) for name in programs())
    try:
        reply = llm.quick(config.ollama, config.model, CHOOSE_PROMPT.format(most=MOST_COMMANDS, programs=listed,
                          question=question), timeout=60, format=CHOOSE_FORMAT, options={"temperature": 0})
        chosen = [str(command) for command in json.loads(reply)["commands"]][:MOST_COMMANDS]
    except (llm.OllamaError, ValueError, KeyError, TypeError):
        return "I couldn't work out how to check that."
    runs = []
    for command in chosen:
        arguments = allowed(command)
        log(f"check {question!r}: {command!r}" + ("" if arguments else " — not allowed, not run"))
        if arguments:
            runs.append(f"$ {command}\n{_run(arguments)}")
    if not runs:
        return "I couldn't find a safe way to check that."
    try:
        return llm.quick(config.ollama, config.model, ANSWER_PROMPT.format(question=question, outputs="\n\n".join(runs)),
                         timeout=60, options={"temperature": 0}).strip()
    except llm.OllamaError:
        return "I checked, but couldn't put the answer into words."


# ── changes that can be undone: services, connections, sound, power mode, default apps ──

# What's never stopped, restarted or turned off from here: what keeps the desktop, sound, network or Rigved going.
PROTECTED = re.compile(r"^(?:gnome-|org\.gnome\.|gdm|display-manager|dbus|systemd-|polkit|pipewire|wireplumber|"
                       r"xdg-desktop-portal|at-spi|NetworkManager|user@|session-|getty|ssh|rigveda-)", re.I)
OURS = re.compile(r"^rigveda-")
OPPOSITE = {"start": "stop", "stop": "start", "enable": "disable", "disable": "enable", "up": "down", "down": "up",
            "on": "off", "off": "on", "connect": "disconnect", "disconnect": "connect"}
MOST_CHANGES = 3
DO_FORMAT = CHOOSE_FORMAT
DO_PROMPT = """Something is to be changed on this Linux computer (Ubuntu, GNOME), in a way that can be undone, by up to {most} commands of these forms only:
systemctl [--user] start|stop|restart|enable|disable <service>   (--user for the services under "yours")
nmcli connection up|down <connection>
nmcli radio wifi|all on|off
bluetoothctl connect|disconnect <device address>   or   bluetoothctl power on|off
wpctl set-default <id>   (the sound output or input to use, by its number below)
wpctl set-mute @DEFAULT_AUDIO_SINK@|@DEFAULT_AUDIO_SOURCE@ 1|0|toggle
powerprofilesctl set performance|balanced|power-saver
xdg-mime default <app>.desktop <mime type>   (the app that opens a kind of file, e.g. video/x-matroska)
{risky}

What there is here (services called rigveda-… are you, this assistant: rigveda-listen hears the user, rigveda-serve and rigveda-orb show the orb):
{context}

Do only what was asked: stopping is not disabling, and nothing more is added.

Wanted: {wanted}
Reply with JSON: the commands, in order, using names exactly as listed. If none of these forms can do it, reply with none."""


def _act(arguments: list[str], seconds: float = 120) -> tuple[bool, str]:
    """Run a change: (done, what it printed). System services wait for your password in GNOME's own dialog."""
    try:
        done = subprocess.run(arguments, capture_output=True, text=True, timeout=seconds, stdin=subprocess.DEVNULL,
                              env={**os.environ, "LANG": "C", "LC_ALL": "C"})
    except subprocess.TimeoutExpired:
        return False, "it took too long"
    except OSError as error:
        return False, str(error)
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def services(user: bool) -> list[str]:
    """The services there are, running or not, without ".service"."""
    scope = ["--user"] if user else []
    found = set()
    for listing in (["list-units", "--all"], ["list-unit-files"]):
        output = _run(["systemctl", *scope, *listing, "--type=service", "--no-legend", "--plain", "--no-pager"], most=10**6)
        found |= {line.split()[0].removesuffix(".service") for line in output.splitlines()
                  if line.split() and line.split()[0].endswith(".service") and not re.search(r"@|\\x|^app-", line.split()[0])}
    return sorted(found)


def sound() -> dict[str, list[tuple[str, str, bool]]]:
    """Sound outputs ("sinks") and inputs ("sources"): (id, name, in use), from wpctl."""
    found: dict[str, list[tuple[str, str, bool]]] = {"sinks": [], "sources": []}
    part = ""
    for line in _run(["wpctl", "status"]).split("Video")[0].splitlines():
        if heading := re.search(r"(Sinks|Sources|Filters|Streams|Devices):", line):
            part = heading.group(1).lower()
            continue
        if part in found and (match := re.search(r"(\*)?\s+(\d+)\.\s+(.+?)(?:\s+\[vol:.*)?$", line)):
            found[part].append((match.group(2), match.group(3).strip(), bool(match.group(1))))
    return found


def connections() -> list[str]:
    output = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    return [line.rsplit(":", 1)[0].replace("\\:", ":") for line in output.splitlines()
            if ":" in line and not line.endswith((":loopback", ":bridge"))]


def devices() -> dict[str, str]:
    """Paired Bluetooth devices: address → name and what it is ("LIFT MAC (mouse)")."""
    found = {}
    for address, name in re.findall(r"Device ([0-9A-F:]{17}) (.+)", _run(["bluetoothctl", "devices"])):
        icon = re.search(r"Icon: (\S+)", _run(["bluetoothctl", "info", address]))
        kind = icon.group(1).split("-")[-1] if icon else ""
        found[address] = f"{name} ({kind})" if kind and kind.lower() not in name.lower() else name
    return found


def launchers() -> dict[str, str]:
    """Apps by name → their launcher's file name, which is what xdg-mime takes ("VLC media player" → vlc_vlc.desktop)."""
    from .actions import apps

    return {name: path.name for name, path in apps().values()}


def situation() -> dict:
    from .risky import processes

    return {"system": services(False), "user": services(True), "sound": sound(), "connections": connections(),
            "bluetooth": devices(), "power": _run(["powerprofilesctl", "get"]).strip(), "apps": launchers(),
            "processes": processes()}


def _named(unit: str) -> str:
    """A service as it's called: "snap.docker.dockerd (docker)"."""
    match = re.fullmatch(r"snap\.([\w-]+)\.[\w-]+", unit)
    return f"{unit} ({match.group(1)})" if match else unit


def _spoken(unit: str) -> str:
    """A service as you'd say it: "snap.docker.dockerd" → docker, "rigveda-orb" → the orb."""
    match = re.fullmatch(r"snap\.([\w-]+)\.[\w-]+", unit)
    return {"rigveda-listen": "my listening", "rigveda-orb": "the orb", "rigveda-serve": "the orb's server"}.get(
        unit, match.group(1) if match else unit)


def _unit(called: str, there: list[str]) -> str | None:
    """The service meant: as named, or a snap's daemon by the snap's name ("docker" → snap.docker.dockerd)."""
    if called in there:
        return called
    snapped = [unit for unit in there if unit.startswith(f"snap.{called}.")]
    return next((unit for unit in snapped if unit.endswith("d")), snapped[0] if len(snapped) == 1 else None)


def described_situation(seen: dict) -> str:
    from . import risky

    outputs = "; ".join(f"{id} {name}{' (in use)' if used else ''}" for id, name, used in seen["sound"]["sinks"])
    inputs = "; ".join(f"{id} {name}{' (in use)' if used else ''}" for id, name, used in seen["sound"]["sources"])
    return "\n".join([f"Services, yours (--user): {', '.join(seen['user'])}",
                      f"Services, the system's: {', '.join(map(_named, seen['system']))}",
                      f"Apps (for xdg-mime): {'; '.join(f'{name} = {file}' for name, file in sorted(seen['apps'].items()))}",
                      f"Sound outputs: {outputs or 'none'}", f"Sound inputs: {inputs or 'none'}",
                      f"Network connections: {', '.join(seen['connections'])}",
                      f"Bluetooth devices: {'; '.join(f'{address} {name}' for address, name in seen['bluetooth'].items()) or 'none'}",
                      f"Power mode now: {seen['power']}", risky.described(seen)])


def planned(command: str, seen: dict) -> tuple[list[str], list[str] | None, str] | str:
    """A change as (what to run, what undoes it, what to say), or why it won't be done. Only the forms in DO_PROMPT,
    only on things that are there, nothing that keeps the desktop or Rigved going."""
    try:
        parts = shlex.split(re.sub(r"^\s*sudo\s+", "", command))
    except ValueError:
        return "I couldn't make sense of that."
    text = " ".join(parts)
    if match := re.fullmatch(r"systemctl( --user)? (start|stop|restart|enable|disable) (\S+?)(?:\.service)?", text):
        user, verb, called = bool(match.group(1)), match.group(2), match.group(3)
        unit = _unit(called, seen["user" if user else "system"])
        if unit is None:
            unit = _unit(called, seen["system" if user else "user"])
            if unit is None:
                return f"There's no service called {called}."
            user = not user  # the model put --user on a system service, or left it off one of yours
        if verb != "start" and verb != "enable" and PROTECTED.search(unit) and not (OURS.search(unit) and verb == "restart"):
            return ("That's me: I'd stop hearing you." if OURS.search(unit)
                    else f"I'll leave {unit} alone: the desktop needs it.")
        scope = ["--user"] if user else []
        undo = ["systemctl", *scope, OPPOSITE[verb], unit] if verb in OPPOSITE else None
        past = {"start": "Started", "stop": "Stopped", "restart": "Restarted", "enable": "Turned on at startup:",
                "disable": "No longer starting at startup:"}[verb]
        return ["systemctl", *scope, verb, unit], undo, f"{past} {_spoken(unit)}."
    if match := re.fullmatch(r"nmcli connection (up|down) (.+)", text):
        verb, name = match.groups()
        if name not in seen["connections"]:
            return f"There's no network connection called {name}."
        return (["nmcli", "connection", verb, name], ["nmcli", "connection", OPPOSITE[verb], name],
                f"{'Connected to' if verb == 'up' else 'Disconnected from'} {name}.")
    if match := re.fullmatch(r"nmcli radio (wifi|all) (on|off)", text):
        radio, state = match.groups()
        return (["nmcli", "radio", radio, state], ["nmcli", "radio", radio, OPPOSITE[state]],
                f"{'Wi-Fi' if radio == 'wifi' else 'All radios'} {state}.")
    if match := re.fullmatch(r"bluetoothctl (connect|disconnect) ([0-9A-Fa-f:]{17})", text):
        verb, address = match.group(1), match.group(2).upper()
        if address not in seen["bluetooth"]:
            return "That Bluetooth device isn't paired with this computer."
        return (["bluetoothctl", verb, address], ["bluetoothctl", OPPOSITE[verb], address],
                f"{'Connected' if verb == 'connect' else 'Disconnected'} {seen['bluetooth'][address]}.")
    if match := re.fullmatch(r"bluetoothctl power (on|off)", text):
        state = match.group(1)
        return ["bluetoothctl", "power", state], ["bluetoothctl", "power", OPPOSITE[state]], f"Bluetooth {state}."
    if match := re.fullmatch(r"wpctl set-default (\d+)", text):
        for part in ("sinks", "sources"):
            named = {id: name for id, name, _ in seen["sound"][part]}
            if match.group(1) in named:
                before = next((id for id, _, used in seen["sound"][part] if used), None)
                return (["wpctl", "set-default", match.group(1)], ["wpctl", "set-default", before] if before else None,
                        f"Sound {'now plays through' if part == 'sinks' else 'now comes from'} {named[match.group(1)]}.")
        return "There's no sound output or input with that number."
    if match := re.fullmatch(r"wpctl set-mute (@DEFAULT_AUDIO_SINK@|@DEFAULT_AUDIO_SOURCE@) (1|0|toggle)", text):
        which_one, state = match.groups()
        if "SOURCE" in which_one and state != "0":
            return ("If I mute the microphone I can't hear you ask me to unmute it. Mute me from the orb instead, "
                    "and the talk key still works.")
        undo_state = {"1": "0", "0": "1", "toggle": "toggle"}[state]
        thing = "Sound" if "SINK" in which_one else "The microphone"
        return (["wpctl", "set-mute", which_one, state], ["wpctl", "set-mute", which_one, undo_state],
                f"{thing} {'muted' if state == '1' else 'unmuted' if state == '0' else 'muted or unmuted'}.")
    if match := re.fullmatch(r"powerprofilesctl set (performance|balanced|power-saver)", text):
        before = seen["power"]
        return (["powerprofilesctl", "set", match.group(1)],
                ["powerprofilesctl", "set", before] if before in ("performance", "balanced", "power-saver") else None,
                f"Power mode: {match.group(1).replace('-', ' ')}.")
    if match := re.fullmatch(r"xdg-mime default (\S+\.desktop) ([\w.+-]+/[\w.+-]+)", text):
        app, mime = match.groups()
        if app not in seen["apps"].values():
            return f"There's no app called {app.removesuffix('.desktop')}."
        before = _run(["xdg-mime", "query", "default", mime]).strip()
        return (["xdg-mime", "default", app, mime], ["xdg-mime", "default", before, mime] if before.endswith(".desktop") else None,
                f"{(mimetypes.guess_extension(mime) or mime.split('/')[-1].removeprefix('x-')).lstrip('.').upper()} files now open with "
                f"{next(name for name, file in seen['apps'].items() if file == app)}.")
    return "That's not something I'll change by voice yet."


def do(wanted: str, config: Config, log=print, opened: list | None = None) -> tuple[bool, str]:
    """Make a change, chosen by the local model from DO_PROMPT's forms and checked: (done, said). What can be undone
    is done at once; what can't (risky.py) is asked about, exactly, and done on your yes (`opened` keeps the
    question, as the conversation's "confirm")."""
    from . import catalog, llm, risky

    seen = situation()
    try:
        reply = llm.quick(config.ollama, config.model, DO_PROMPT.format(most=MOST_CHANGES, context=described_situation(seen),
                          wanted=wanted, risky=risky.FORMS), timeout=60, format=DO_FORMAT, options={"temperature": 0})
        chosen = [str(command) for command in json.loads(reply)["commands"]][:MOST_CHANGES]
    except (llm.OllamaError, ValueError, KeyError, TypeError):
        return False, "I couldn't work out how to do that."
    if not chosen:
        return False, "That's not something I can change by voice yet."
    said, done, asking = [], False, []
    for command in chosen:
        plan = risky.planned(command, seen)
        if plan is None:
            plan = planned(command, seen)
        log(f"do {wanted!r}: {command!r}" + (f" — not done: {plan}" if isinstance(plan, str) else ""))
        if isinstance(plan, str):
            said.append(plan)
            continue
        if isinstance(plan, dict):
            if plan["kind"] == "quit" and not _meant(plan, wanted):
                said.append("That isn't running at the moment, so there's nothing to quit.")
                continue
            merged = next((other for other in asking if other["kind"] == "quit" == plan["kind"] and other["force"] == plan["force"]), None)
            if merged:  # several processes chosen one by one: one question
                merged["pids"].update(plan["pids"])
                names = sorted(set(merged["pids"].values()))
                many = f", {len(merged['pids'])} processes" if len(merged["pids"]) > 1 else ""
                merged["ask"] = f"{'Force quit' if plan['force'] else 'Quit'} {' and '.join(names)}{many}"
                merged["said"] = f"{'Force quit' if plan['force'] else 'Quit'} {' and '.join(names)}."
            else:
                asking.append(plan)
            continue
        run, undo, line = plan
        ok, output = _act(run)
        if not ok:
            log(f"  {run} failed: {output[:300]}")
            needed = re.search(r"authenticat|not authorized|access denied|interactive", output, re.I)
            said.append("That needs your password, and you didn't give it." if needed else f"That didn't work: {output[:120]}")
            continue
        done = True
        said.append(line)
        if undo:
            catalog.remember({"run": undo, "said": line})
    if asking:
        question = "; ".join(plan["ask"] for plan in asking)
        if opened is not None:
            opened.append({"kind": "confirm", "name": question, "thing": json.dumps([["run", json.dumps(asking)]])})
        from .actions import line

        said.append(line(config, "sure", what=question))
        done = True
    return done, " ".join(said)


def _meant(plan: dict, wanted: str) -> bool:
    """Whether the programs to quit are the ones asked for: named in what was said ("quit firefox" is never Chrome),
    or asked for by what they're doing ("whatever is using the most memory")."""
    said = wanted.lower()
    if re.search(r"\b(?:most|biggest|heaviest|hog\w*|using|frozen|stuck|not responding|hung|slow\w*)\b", said):
        return True
    return any(name.lower()[:5] in said or name.lower() in said for name in plan["pids"].values())


def carry(plans: list[dict], log=print, told=None) -> tuple[bool, str]:
    """What was asked about, after your yes (risky.carry): (done, said)."""
    from . import catalog, risky

    said, done = [], False
    for plan in plans:
        ok, line, undo = risky.carry(plan, log, told)
        log(f"yes → {plan['kind']}: {'done' if ok else 'not done'}: {line}")
        said.append(line)
        done = done or ok
        if undo:
            catalog.remember(undo)
    return done, " ".join(said)
