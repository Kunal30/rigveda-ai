import json

import pytest

from rigveda import actions, conversation, llm, machine, risky
from rigveda.actions import Step
from rigveda.config import Config

QUIET = lambda message: None  # noqa: E731


@pytest.mark.parametrize("command", ["df -h", "free -h", "ps -eo %mem,comm --sort -%mem", "systemctl status ollama",
                                     "systemctl --user list-units --failed", "ip addr show", "nmcli -t device status",
                                     "nvidia-smi --query-gpu=utilization.gpu --format=csv", "apt list --upgradable",
                                     "cat /proc/loadavg", "journalctl -b -p err -n 20", "find ~/Downloads -name *.mkv",
                                     "sudo df -h", "ps aux | head", "temperatures", "dpkg -l firefox"])
def test_commands_that_only_look_are_run(command: str) -> None:
    assert machine.allowed(command) is not None


@pytest.mark.parametrize("command", ["rm -rf ~", "systemctl stop ollama", "systemctl --user restart rigveda-listen",
                                     "nmcli radio wifi off", "nmcli connection down home", "ip link set eth0 down",
                                     "find ~ -name x -delete", "find ~ -exec rm {} ;", "journalctl --vacuum-size=1M",
                                     "date -s 2020-01-01", "hostname evil", "apt install cowsay", "cat /etc/shadow",
                                     "cat /proc/1/environ", "cat /proc/../etc/passwd", "gsettings set a b c",
                                     "ps -o comm= -p $(pgrep ollama)", "df -h; rm x", "df > /tmp/x", "ss -K",
                                     "nvidia-smi -pm 1", "snap install x", "pactl set-sink-volume 0 0%", "bash -c ls",
                                     "python3 -c 1", "playerctl pause", "upower --monitor", "du -sh /*", "find / -name x"])
def test_anything_that_could_change_something_is_not(command: str) -> None:
    assert machine.allowed(command) is None


def test_a_question_about_the_computer_is_answered_from_what_it_says(monkeypatch) -> None:
    ran, prompts = [], []
    monkeypatch.setattr(machine, "_run", lambda arguments: ran.append(arguments) or "Filesystem Size Avail\n/dev/nvme0 1.8T 1.3T")

    def model(host, model, prompt, **options):
        prompts.append(prompt)
        if "Reply with JSON: the commands" in prompt:
            return json.dumps({"commands": ["df -h", "rm -rf ~"]})
        return "About 1.3 terabytes are free."

    monkeypatch.setattr(llm, "quick", model)
    private: list = []
    reply = actions.carry_out([Step("check", "how much disk space is left")], Config(), log=QUIET, private=private)
    assert reply.endswith("About 1.3 terabytes are free.") and ran == [["df", "-h"]]  # not the rm
    assert "1.3T" in prompts[1] and private == [True]


SEEN = {"system": ["ollama", "snap.docker.dockerd", "gdm", "NetworkManager"], "user": ["rigveda-listen", "rigveda-orb", "pipewire"],
        "sound": {"sinks": [("40", "Headphones", False), ("59", "TV (HDMI)", True)], "sources": [("47", "Headset mic", True)]},
        "connections": ["home", "Hotspot"], "bluetooth": {"D2:94:74:2F:43:FA": "LIFT MAC (mouse)"}, "power": "performance",
        "apps": {"VLC media player": "vlc_vlc.desktop"},
        "processes": [{"pid": 4242, "name": "firefox", "memory": 9.0, "cpu": 3.0}, {"pid": 4243, "name": "firefox", "memory": 2.0, "cpu": 0.0},
                      {"pid": 77, "name": "gnome-shell", "memory": 3.0, "cpu": 5.0}]}


def test_changes_are_planned_with_their_undoing() -> None:
    assert machine.planned("systemctl restart ollama", SEEN) == (["systemctl", "restart", "ollama"], None, "Restarted ollama.")
    assert risky.planned("systemctl restart ollama", SEEN) is None  # not one of the risky forms
    assert machine.planned("sudo systemctl stop docker", SEEN)[:2] == (["systemctl", "stop", "snap.docker.dockerd"],
                                                                     ["systemctl", "start", "snap.docker.dockerd"])
    assert machine.planned("systemctl --user stop ollama", SEEN)[0] == ["systemctl", "stop", "ollama"]  # a system service
    assert machine.planned("wpctl set-default 40", SEEN)[1] == ["wpctl", "set-default", "59"]  # back to the TV
    assert machine.planned("powerprofilesctl set power-saver", SEEN)[1] == ["powerprofilesctl", "set", "performance"]
    assert machine.planned("bluetoothctl disconnect D2:94:74:2F:43:FA", SEEN)[2] == "Disconnected LIFT MAC (mouse)."
    assert machine.planned("nmcli connection up home", SEEN)[1] == ["nmcli", "connection", "down", "home"]


@pytest.mark.parametrize("command", ["systemctl --user stop rigveda-listen", "systemctl stop gdm", "systemctl restart NetworkManager",
                                     "systemctl --user stop pipewire", "systemctl stop sshd-made-up", "nmcli connection up neighbours",
                                     "wpctl set-mute @DEFAULT_AUDIO_SOURCE@ 1", "wpctl set-default 99", "rm -rf ~",
                                     "systemctl kill ollama", "xdg-mime default evil.desktop video/x-matroska",
                                     "bluetoothctl connect 00:11:22:33:44:55", "systemctl restart ollama; reboot"])
def test_what_keeps_things_going_or_isnt_there_is_left_alone(command: str) -> None:
    assert isinstance(machine.planned(command, SEEN), str)


def test_rigved_may_restart_itself_but_not_stop() -> None:
    assert machine.planned("systemctl --user restart rigveda-orb", SEEN)[2] == "Restarted the orb."


def test_a_change_by_voice_and_undo_that(monkeypatch, no_real_changes: list) -> None:
    monkeypatch.setattr(machine, "situation", lambda: SEEN)
    monkeypatch.setattr(llm, "quick", lambda host, model, prompt, **options: json.dumps({"commands": ["wpctl set-default 40"]}))
    reply = actions.carry_out([Step("do", "switch the sound to the headphones")], Config(), log=QUIET)
    assert reply.endswith("Sound now plays through Headphones.") and no_real_changes == [["wpctl", "set-default", "40"]]
    assert actions.understand("undo that", Config()) == [Step("setting", "undo")]
    actions.carry_out([Step("setting", "undo")], Config(), log=QUIET)
    assert no_real_changes[-1] == ["wpctl", "set-default", "59"]


def test_a_power_mode_asked_for_as_a_setting_is_still_done(monkeypatch, no_real_changes: list) -> None:
    monkeypatch.setattr(machine, "situation", lambda: SEEN)
    monkeypatch.setattr(llm, "quick", lambda host, model, prompt, **options: json.dumps({"commands": ["powerprofilesctl set power-saver"]}))
    reply = actions.carry_out([Step("setting", "power mode battery saver")], Config(), log=QUIET)
    assert reply.endswith("Power mode: power saver.") and no_real_changes == [["powerprofilesctl", "set", "power-saver"]]
