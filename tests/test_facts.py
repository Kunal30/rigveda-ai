import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from rigveda import facts, llm
from rigveda.indexer import index_paths
from rigveda.server import ServerConfig, make_handler
from rigveda.store import connect

LEASE = ("Lease between resident Kunal Vinay Kumar Suthar and the owner USCMF Boulder Creek LLC. "
         "Apartment B34, 4425 Issaquah Pine Lake Rd. SE, Sammamish, Washington 98075. Born 10 March 1996.")


def _quiet(message: str) -> None:
    pass


def fake_model(host: str, model: str, prompt: str) -> list[dict]:
    """Answers like the real model would for LEASE, plus one invented fact that the checks must drop."""
    if "Kunal Vinay Kumar Suthar" not in prompt:
        return []
    return [
        {"key": "full_name", "value": "Kunal Vinay Kumar Suthar", "evidence": "resident Kunal Vinay Kumar Suthar"},
        {"key": "address.postal_code", "value": "98075", "evidence": "Washington 98075"},
        {"key": "date_of_birth", "value": "1996-03-10", "evidence": "Born 10 March 1996"},
        {"key": "passport.number", "value": "Z1234567", "evidence": "passport Z1234567"},  # not in the text
        {"key": "favourite_colour", "value": "blue", "evidence": "Apartment B34"},  # not a known key
    ]


@pytest.fixture
def lease_index(tmp_path: Path, config, monkeypatch):
    monkeypatch.setattr(llm, "extract_facts", fake_model)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "lease.txt").write_text(LEASE)
    (docs / "recipe.txt").write_text("Mix flour and water.")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    return docs


def test_grounded_drops_values_the_text_does_not_contain() -> None:
    assert facts.grounded("address.postal_code", "98075", "Washington 98075", LEASE)
    assert not facts.grounded("passport.number", "Z1234567", "passport Z1234567", LEASE)
    assert not facts.grounded("full_name", "Kunal Suthar", "Kunal Suthar", LEASE)  # words not adjacent in the text
    assert facts.grounded("date_of_birth", "1996-03-10", "Born 10 March 1996", LEASE)
    assert not facts.grounded("date_of_birth", "10 March 1996", "Born 10 March 1996", LEASE)  # dates must be ISO


def test_indexing_finds_pending_facts_with_sources(lease_index, config) -> None:
    con = connect(config.database_path)
    found = {fact["key"]: fact for fact in facts.listing(con)}
    assert set(found) == {"full_name", "address.postal_code", "date_of_birth"}
    assert all(fact["status"] == "pending" for fact in found.values())
    source = found["full_name"]["sources"][0]
    assert source["path"].endswith("lease.txt") and source["evidence"] == "resident Kunal Vinay Kumar Suthar"


def test_owner_name_limits_which_passages_are_read(tmp_path: Path, config, monkeypatch) -> None:
    prompts = []
    monkeypatch.setattr(llm, "extract_facts", lambda host, model, prompt: prompts.append(prompt) or [])
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "mine.txt").write_text("Phone of Kunal Suthar: 555 0100")
    (docs / "other.txt").write_text("Tenant Jane Roe, phone 555 0199")
    config.owner_name = "Kunal Suthar"
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    assert len(prompts) == 1 and "Kunal Suthar" in prompts[0] and "Jane" not in prompts[0]


def test_unchanged_documents_are_not_scanned_twice(lease_index, config, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(llm, "extract_facts", lambda *args: calls.append(args) or [])
    index_paths(config.database_path, [str(lease_index)], config, log=_quiet)
    assert calls == []


def test_deleted_file_drops_pending_but_keeps_approved_facts(lease_index, config) -> None:
    con = connect(config.database_path)
    by_key = {fact["key"]: fact["id"] for fact in facts.listing(con)}
    facts.decide(con, by_key["full_name"], "approved")
    con.close()
    (lease_index / "lease.txt").unlink()
    index_paths(config.database_path, [str(lease_index)], config, log=_quiet)
    remaining = facts.listing(connect(config.database_path))
    assert [(fact["key"], fact["status"], fact["sources"]) for fact in remaining] == [("full_name", "approved", [])]


def test_rejected_fact_stays_rejected_when_found_again(lease_index, config) -> None:
    con = connect(config.database_path)
    zip_id = next(fact["id"] for fact in facts.listing(con) if fact["key"] == "address.postal_code")
    facts.decide(con, zip_id, "rejected")
    facts.scan(con, config, _quiet, rescan=True)
    assert next(fact for fact in facts.listing(con) if fact["id"] == zip_id)["status"] == "rejected"


def test_correcting_a_value_merges_with_an_existing_fact(lease_index, config) -> None:
    con = connect(config.database_path)
    typed = facts.add_manual(con, "full_name", "Kunal V. Suthar")
    found = next(fact["id"] for fact in facts.listing(con) if fact["key"] == "full_name" and fact["id"] != typed)
    assert facts.decide(con, typed, "approved", "kunal vinay kumar  suthar") == found
    names = [fact for fact in facts.listing(con) if fact["key"] == "full_name"]
    assert len(names) == 1 and names[0]["status"] == "approved" and names[0]["sources"]


def test_add_manual_rejects_unknown_keys(config) -> None:
    with pytest.raises(ValueError):
        facts.add_manual(connect(config.database_path), "favourite_colour", "blue")


def test_mask_and_normalize() -> None:
    assert facts.mask("123456789") == "•••••6789"
    same = [("phone", "(480)410-5749", "+14804105749"), ("ssn", "123-45-6789", "123456789"),
            ("address.street", "4425 Issaquah Pine Lake Rd. SE", "4425 ISSAQUAH PINE LAKE RD SE"),
            ("address.unit", "APT. NO. 216E", "216 E"), ("email", "Me@Example.com", "me@example.com")]
    for key, a, b in same:
        assert facts.normalize(key, a) == facts.normalize(key, b), key
    assert facts.normalize("address.postal_code", "98125") != facts.normalize("address.postal_code", "98125-8428")


def test_plausible_rejects_relatives_and_recent_birth_dates(config) -> None:
    config.owner_name = "Kunal Vinay Kumar Suthar"
    assert facts.plausible("full_name", "KUNAL V SUTHAR", config)
    assert not facts.plausible("full_name", "CHANDRIKA VINAY KUMAR SUTHAR", config)  # a parent, named on the passport
    assert facts.plausible("date_of_birth", "1996-10-03", config)
    assert not facts.plausible("date_of_birth", "2026-07-28", config)  # the date a lease was signed
    assert facts.plausible("ssn", "123-45-6789", config)
    assert not facts.plausible("ssn", "***-**-6789", config) and not facts.plausible("ssn", "XXX-XX-6789", config)


def test_renormalize_merges_and_keeps_decisions(config) -> None:
    con = connect(config.database_path)
    kept = facts.add_manual(con, "ssn", "123-45-6789")
    with con:  # as stored by an older version that compared values differently
        old = con.execute("INSERT INTO facts(key, value, norm, status, created_at) VALUES ('ssn', '123456789', 'stale', 'pending', 0)").lastrowid
        con.execute("UPDATE facts SET norm='stale-too' WHERE id=?", (kept,))
    facts.renormalize(con)
    rows = con.execute("SELECT id, status FROM facts").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "approved" and {rows[0]["id"]} <= {kept, old}


@pytest.fixture
def server(config):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ServerConfig(config.database_path, "m", "h", 5)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, method: str, path: str, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, data, {"Content-Type": "application/json", **(headers or {})})
    response = conn.getresponse()
    return response.status, json.loads(response.read() or b"null") if response.getheader("Content-Type", "").startswith("application/json") else None


def test_review_api_approves_facts(lease_index, server) -> None:
    status, data = _request(server, "GET", "/api/facts")
    assert status == 200 and data["keys"]["date_of_birth"] == "Date of birth"
    fact = next(fact for fact in data["facts"] if fact["key"] == "date_of_birth")
    assert _request(server, "POST", f"/api/facts/{fact['id']}", {"status": "approved"})[0] == 200
    assert _request(server, "POST", "/api/facts", {"key": "email", "value": "me@example.com"})[0] == 200
    approved = {fact["key"] for fact in _request(server, "GET", "/api/facts")[1]["facts"] if fact["status"] == "approved"}
    assert approved == {"date_of_birth", "email"}
    assert _request(server, "POST", "/api/facts/99999", {"status": "approved"})[0] == 404
    assert _request(server, "GET", "/facts")[0] == 200


def test_other_sites_cannot_use_the_server(lease_index, server) -> None:
    # A page on another domain pointed at 127.0.0.1 (DNS rebinding) sends its own Host header.
    assert _request(server, "GET", "/api/facts", headers={"Host": "evil.example:8000"})[0] == 403
    # A plain cross-site form post cannot set the JSON content type.
    assert _request(server, "POST", "/api/facts", {"key": "email", "value": "x@y.z"}, {"Content-Type": "text/plain"})[0] == 400
    assert _request(server, "GET", "/api/facts", headers={"Host": "localhost:8000"})[0] == 200
