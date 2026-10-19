import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from rigveda import actions, voice
from rigveda.server import ServerConfig, make_handler


@pytest.fixture
def listener(tmp_path, monkeypatch, config):
    """A listener with no microphone, speakers or models: just the typed-command socket."""
    monkeypatch.setattr(voice, "SOCKET", tmp_path / "voice.sock")
    monkeypatch.setattr(voice, "HISTORY", tmp_path / "history.jsonl")
    monkeypatch.setattr(voice, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(actions, "understand", lambda command, config: [actions.Step("time", "")])
    monkeypatch.setattr(actions, "carry_out", lambda steps, config, log=print, **options: "It's noon.")
    heard = voice.Listener.__new__(voice.Listener)
    heard.config, heard.log, heard.shown = config, lambda message: None, ("", 0.0, 0.0)
    heard.typed()
    return heard


def test_typed_command_is_carried_out_and_remembered(listener) -> None:
    assert voice.send("Rigved, what time is it?") == {"did": ["time"], "said": "It's noon."}
    [entry] = voice.history()
    assert entry["heard"] == "what time is it" and entry["typed"] is True
    assert oct(voice.SOCKET.stat().st_mode & 0o777) == "0o600"


def test_server_passes_typed_commands_on(listener, tmp_path) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ServerConfig(tmp_path / "db.sqlite", "model", "http://x", 5)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        http = HTTPConnection("127.0.0.1", httpd.server_address[1])
        http.request("POST", "/api/voice", json.dumps({"text": "time"}), {"Content-Type": "application/json"})
        response = http.getresponse()
        assert response.status == 200 and json.loads(response.read())["said"] == "It's noon."
        voice.SOCKET.unlink()
        http.request("POST", "/api/voice", json.dumps({"text": "time"}), {"Content-Type": "application/json"})
        assert http.getresponse().status == 503
    finally:
        httpd.shutdown()
