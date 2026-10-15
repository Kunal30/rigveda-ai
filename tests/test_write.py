import json
import re
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from rigveda import facts, llm
from rigveda.indexer import index_paths
from rigveda.server import ServerConfig, make_handler
from rigveda.store import connect
from rigveda.write import tidy, write

RESUME = ("Kunal Suthar, Software Engineer at Contoso Robotics since 2023. Built the fleet telemetry pipeline in Rust "
          "and cut alert noise by 40 percent. MS in Computer Science, Arizona State University.")


@pytest.fixture
def indexed(tmp_path, config):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "resume.txt").write_text(RESUME)
    (docs / "recipe.txt").write_text("Mix flour and water, then knead the dough.")
    index_paths(config.database_path, [str(docs)], config, log=lambda message: None)
    config.owner_name = "Kunal Suthar"
    con = connect(config.database_path)
    facts.add_manual(con, "employer", "Contoso Robotics")
    facts.add_manual(con, "passport.number", "Z1234567")
    con.close()
    return config


@pytest.fixture
def model(monkeypatch):
    """Records each prompt and answers with whatever the test sets."""
    calls = {"prompts": [], "reply": "I build telemetry pipelines at Contoso Robotics.", "used": None}

    def answer(host, name, prompt):
        calls["prompts"].append(prompt)
        resume = [int(n) for n in re.findall(r"\[(\d+)\][^\n]*resume\.txt", prompt)]  # by default, the résumé
        return {"text": calls["reply"], "used": resume if calls["used"] is None else calls["used"]}
    monkeypatch.setattr(llm, "write_text", answer)
    return calls


def test_draft_is_grounded_in_files_and_profile(indexed, model) -> None:
    field = {"label": "Describe your most recent role", "maxLength": 1500}
    result = write(indexed.database_path, indexed, {"field": field, "title": "Careers"})
    prompt = model["prompts"][0]
    assert "fleet telemetry pipeline" in prompt and "Employer: Contoso Robotics" in prompt
    assert "Describe your most recent role" in prompt and "at most 1500 characters" in prompt
    assert "Z1234567" not in prompt  # ID numbers are never given to the model for writing
    assert result["mode"] == "draft" and result["text"].startswith("I build")
    assert [source["name"] for source in result["sources"]] == ["resume.txt"]  # only what the text drew on
    assert "recipe.txt" not in prompt  # passages that don't name you are left out


def test_continuing_text_shows_where_the_cursor_is(indexed, model) -> None:
    model["reply"], model["used"] = "I led the telemetry work.", [99]  # a passage number that doesn't exist
    result = write(indexed.database_path, indexed, {"before": "Dear team,\nAt Contoso", "after": "\nThanks"})
    assert "Dear team,\nAt Contoso▮\nThanks" in model["prompts"][0]
    assert result["mode"] == "continue" and result["text"] == " I led the telemetry work." and result["sources"] == []


def test_recordings_are_not_used_for_writing(indexed, model, tmp_path) -> None:
    con = connect(indexed.database_path)
    with con:  # as a film's subtitles would be indexed
        con.execute("UPDATE documents SET kind='video' WHERE path LIKE '%resume.txt'")
    con.close()
    write(indexed.database_path, indexed, {"instruction": "telemetry pipeline in Rust"})
    assert "fleet telemetry" not in model["prompts"][0]


def test_tidy_cleans_up_model_output() -> None:
    assert tidy('Here is a draft:\n\n"I like Rust."', {}, []) == "I like Rust."
    assert tidy("<think>hmm</think>I like Rust.", {}, []) == "I like Rust."
    # an ID number that slipped through is dropped with its sentence
    assert tidy("I like Rust. My passport is Z 123 4567. I live in Sammamish.", {}, ["Z1234567"]) == \
        "I like Rust. I live in Sammamish."
    # the model repeating the end of your text, and spacing after it
    assert tidy("I joined in 2023. Since then I grew.", {"before": "I joined in 2023."}, []) == " Since then I grew."
    assert tidy(", and more.", {"before": "Rust"}, []) == ", and more."
    assert tidy("I work on robots.", {"before": "At my current job I"}, []) == " work on robots."
    assert tidy("current job I work on robots.", {"before": "At my current job I"}, []) == " work on robots."
    assert tidy("I am writing to apply.", {"before": "Dear hiring manager,\u00a0"}, []) == "I am writing to apply."
    # a text box's character limit, cut at a sentence
    assert tidy("One. Two two. Three three three.", {"field": {"maxLength": 20}, "before": ""}, []) == "One. Two two."


@pytest.fixture
def server(indexed):
    handler = make_handler(ServerConfig(indexed.database_path, "m", "h", 5, indexed, token="s3cret"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None):
    conn = HTTPConnection("127.0.0.1", port)
    conn.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read()
    return response.status, json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data


def test_write_endpoint(server, model) -> None:
    status, data = request(server, "POST", "/api/write", {"instruction": "a short bio"})
    assert status == 200 and data["text"].startswith("I build") and data["sources"]
    assert request(server, "POST", "/api/write", {"before": "  "})[0] == 400
    assert request(server, "POST", "/api/write", {"before": 5})[0] == 400
    assert request(server, "GET", "/write")[0] == 200


def test_write_endpoint_reports_a_missing_model(server, monkeypatch) -> None:
    def down(*args):
        raise llm.OllamaError("Could not reach Ollama")
    monkeypatch.setattr(llm, "write_text", down)
    status, data = request(server, "POST", "/api/write", {"instruction": "a short bio"})
    assert status == 503 and "Ollama" in data["error"]
