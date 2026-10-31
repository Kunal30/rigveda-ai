import os
from pathlib import Path

import pytest

from rigveda import actions, conversation, names
from rigveda.actions import Step
from rigveda.config import Config

FILMS = ["Downloads/Iron Man 3 (2013) (1080p DS4K BluRay x265 10-bit HDR AAC 7.1) [WeSLeY].mkv",
         "Downloads/multimedia/Iron.Man.3.2013.REMASTERED.1080p.BluRay.x265-GalaxyRG265[TGx]/Iron.Man.3.2013.REMASTERED.1080p.BluRay.x265-GalaxyRG265.mkv",
         "Downloads/multimedia/Iron.Man.3.2013.REMASTERED.1080p.BluRay.x265-GalaxyRG265[TGx]/source.txt",
         "Downloads/multimedia/Iron Man 3 2013 UHD BluRay 2160p.mkv.fdmdownload",
         "Downloads/Iron Man [1080p]/Iron.Man.2008.1080p.BrRip.x264.YIFY.mp4",
         "Downloads/Iron.Man.2.2010.1080p.BluRay.x265/Iron.Man.2.2010.1080p.BluRay.x265.mkv",
         "Documents/lease.pdf", ".cache/Iron Man 3.mkv", "github/app/node_modules/iron-man-3/index.js"]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"  # where conftest points the name search
    for age, film in enumerate(FILMS):
        (home / film).parent.mkdir(parents=True, exist_ok=True)
        (home / film).write_bytes(b"")
        os.utime(home / film, (1_700_000_000 - age * 60,) * 2)  # equally good matches go newest first: these in order
    return home


def best(asked: str) -> list[str]:
    return [match.path.name for match in names.meant(names.matches(asked))]


def test_a_film_is_found_by_its_name_however_its_written(home: Path) -> None:
    for asked in ("iron man 3", "Iron Man three", "iron men 3", "ironman 3", "IRON MAN III"):
        found = best(asked)
        assert len(found) == 2 and all(name.endswith(".mkv") for name in found), asked  # not its notes, nor half a download
    assert best("iron man") == ["Iron.Man.2008.1080p.BrRip.x264.YIFY.mp4"]  # not the sequels
    assert best("iron man 2") == ["Iron.Man.2.2010.1080p.BluRay.x265.mkv"]
    assert best("the lease") == ["lease.pdf"] and best("spider man") == []


def test_hidden_folders_and_code_clutter_arent_searched(home: Path) -> None:
    assert not any(".cache" in str(path) or "node_modules" in str(path) for path in names.files())


def test_a_film_is_said_by_its_title_and_year() -> None:
    assert names.spoken("/x/Iron.Man.3.2013.REMASTERED.1080p.BluRay.mkv") == "Iron Man 3 (2013)"
    assert names.spoken("/x/lease.pdf") == "lease"
    assert names.spoken("/x/Mirzapur- The Movie (2026)-trailer.mkv") == "Mirzapur The Movie (2026), the trailer"


def test_rigved_asks_which_copy_and_takes_your_answer(home: Path) -> None:
    opened: list[dict] = []
    reply = actions.carry_out([Step("open", "iron man 3")], Config(), log=lambda message: None, dry_run=True, opened=opened)
    assert reply == "I found two of Iron Man 3 (2013): the HDR WeSLeY one, or the remastered one. Which one would you like?"
    conversation.add("open iron man 3", reply, "voice", opened[-1])
    for said, picked in (("the remastered one", "REMASTERED"), ("the second one", "REMASTERED"), ("the HDR one", "WeSLeY"),
                         ("WeSLeY", "WeSLeY"), ("either", "WeSLeY")):
        [step] = actions.understand(said, Config())
        assert step.kind == "open" and picked in step.target, said
    assert names.choose("what time is it", conversation.choices()) is None
    reply = actions.carry_out([Step("open", step.target)], Config(), log=lambda message: None, dry_run=True)
    assert reply == "Found it: Iron Man 3 (2013)."
    assert conversation.last_opened() is None  # a question isn't something opened, for "open it"


def test_a_trailer_is_never_the_film_and_it_opens_in_the_app_named(home: Path, monkeypatch) -> None:
    folder = home / "Downloads/Mirzapur- The Movie (2026) 2160p 4K SDR WEB-DL Hindi ESub ~ RemuxDoc"
    (folder / "Trailers").mkdir(parents=True)
    (folder / "Mirzapur- The Movie (2026) 2160p 4K SDR WEB-DL Hindi ESub ~ RemuxDoc.mkv").write_bytes(b"")
    (folder / "Trailers/Mirzapur- The Movie (2026)-trailer.mkv").write_bytes(b"")
    assert best("mirzapur movie") == ["Mirzapur- The Movie (2026) 2160p 4K SDR WEB-DL Hindi ESub ~ RemuxDoc.mkv"]
    assert best("mirzapur trailer") == ["Mirzapur- The Movie (2026)-trailer.mkv"]
    vlc = ("VLC media player", Path("/apps/vlc.desktop"))
    monkeypatch.setattr(actions, "apps", lambda: {"vlc media player": vlc, "vlc": vlc})
    launched = []
    monkeypatch.setattr(actions, "launch", lambda *command: launched.append(command))
    said = "Okay, open Mirzapur Movie in VLC Media Player."
    steps = actions.understand(said, Config())
    assert steps == [Step("open", "mirzapur movie in vlc media player")]  # one thing, not the film and then VLC
    assert actions.carry_out(steps, Config(), log=lambda message: None) == \
        "Found it: Mirzapur The Movie (2026) in VLC media player."
    assert launched == [("gio", "launch", "/apps/vlc.desktop", str(folder / "Mirzapur- The Movie (2026) 2160p 4K SDR WEB-DL Hindi ESub ~ RemuxDoc.mkv"))]


def test_a_choice_that_fits_them_all_takes_the_best(home: Path) -> None:
    paths = [str(home / film) for film in FILMS[:2]]
    assert names.choose("Uh, the MKV one?", paths) == paths[0]  # both are: either will do


def test_which_one_stays_open_until_something_is_opened(home: Path, monkeypatch) -> None:
    paths = [str(home / film) for film in FILMS[:2]]
    conversation.add("open iron man 3", "Which one?", "voice", {"kind": "choices", "name": "Iron Man 3", "thing": "\n".join(paths)})
    conversation.add("uh", "Take your time.", "voice")  # something else said meanwhile
    assert conversation.choices() == paths
    conversation.add("the second one", "Found it.", "voice", {"kind": "file", "name": "x", "thing": paths[1]})
    assert conversation.choices() == []
    conversation.add("open iron man 3", "Which one?", "voice", {"kind": "choices", "name": "Iron Man 3", "thing": "\n".join(paths)})
    monkeypatch.setattr(conversation.time, "time", lambda: 10**10)  # long after, it's forgotten
    assert conversation.asking() is None


def test_mkv_films_open_in_vlc_unless_another_app_is_named(home: Path, monkeypatch) -> None:
    vlc, totem = ("VLC media player", Path("/apps/vlc.desktop")), ("Videos", Path("/apps/totem.desktop"))
    monkeypatch.setattr(actions, "apps", lambda: {"vlc media player": vlc, "videos": totem})
    launched = []
    monkeypatch.setattr(actions, "launch", lambda *command: launched.append(command))
    actions.carry_out([Step("open", "iron man 2")], Config(), log=lambda message: None)
    actions.carry_out([Step("open", "iron man")], Config(), log=lambda message: None)  # an .mp4: the desktop's choice
    actions.carry_out([Step("open", "iron man 2 in videos")], Config(), log=lambda message: None)
    film = str(home / FILMS[5])
    assert launched == [("gio", "launch", "/apps/vlc.desktop", film), ("gio", "open", str(home / FILMS[4])),
                        ("gio", "launch", "/apps/totem.desktop", film)]


def test_the_orb_shows_the_choice_and_a_click_answers_it(home: Path, monkeypatch, tmp_path: Path) -> None:
    import json
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer

    from rigveda import voice
    from rigveda.server import ServerConfig, make_handler

    paths = [str(home / film) for film in FILMS[:2]]
    sent = []
    monkeypatch.setattr(voice, "send", lambda text: sent.append(text) or {"did": ["open"], "said": "Found it."})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ServerConfig(tmp_path / "db.sqlite", "model", "http://x", 5)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        http = HTTPConnection("127.0.0.1", httpd.server_address[1])

        def call(method, path, body=None):
            http.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
            response = http.getresponse()
            return response.status, json.loads(response.read())

        assert call("GET", "/api/choices") == (200, {"asking": None})
        conversation.add("open iron man 3", "Which one?", "voice", {"kind": "choices", "name": "Iron Man 3 (2013)", "thing": "\n".join(paths)})
        status, body = call("GET", "/api/choices")
        assert [option["label"] for option in body["asking"]["options"]] == ["the HDR WeSLeY one", "the remastered one"]
        assert call("POST", "/api/choose", {"index": 1}) == (200, {"did": ["open"], "said": "Found it."})
        assert sent == ["the second one"]  # taken just as if said
        assert call("POST", "/api/choose", {"index": 5})[0] == 409
    finally:
        httpd.shutdown()


def test_the_orbs_script_runs(tmp_path: Path) -> None:
    # 2026-10-30: a second `opened` in the script stopped all of it, and the orb froze
    import re
    import shutil
    import subprocess

    from rigveda import server

    if not shutil.which("node"):
        pytest.skip("node isn't installed to check the script")
    script = tmp_path / "orb.js"
    script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", server.ORB_HTML, re.S)))
    checked = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
