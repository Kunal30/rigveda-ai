import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from rigveda import facts
from rigveda.server import ServerConfig, make_handler
from rigveda.store import connect
from rigveda.suggest import classify, suggestions

STATES = [{"value": "", "text": "Select…"}, {"value": "CA", "text": "California"}, {"value": "WA", "text": "Washington"}]


@pytest.mark.parametrize("field, key", [
    ({"label": "First name", "name": "fname"}, "given_name"),
    ({"name": "lastName"}, "family_name"),
    ({"label": "Full legal name"}, "full_name"),
    ({"label": "Date of birth", "placeholder": "MM/DD/YYYY"}, "date_of_birth"),
    ({"autocomplete": "bday-year"}, "date_of_birth"),
    ({"type": "email", "name": "x1"}, "email"),
    ({"label": "Mobile number"}, "phone"),
    ({"label": "Street address", "name": "address1"}, "address.street"),
    ({"label": "Apt / Suite", "name": "address2"}, "address.unit"),
    ({"label": "ZIP code"}, "address.postal_code"),
    ({"label": "PIN code"}, "address.postal_code"),  # Indian postal code, not a secret PIN
    ({"label": "State", "type": "select"}, "address.state"),
    ({"label": "Passport number", "name": "passportNo"}, "passport.number"),
    ({"label": "Passport expiry date"}, "passport.expiry"),
    ({"label": "Driver's license number"}, "drivers_license.number"),
    ({"label": "Place of birth"}, "place_of_birth"),
    ({"label": "Nationality"}, "nationality"),
    ({"label": "Email address"}, "email"),
    ({"name": "q", "placeholder": "Search"}, ""),
])
def test_classify(field, key) -> None:
    assert classify(field) == key


@pytest.mark.parametrize("field", [
    {"label": "Password", "type": "password"},
    {"label": "Card number", "autocomplete": "cc-number"},
    {"name": "cardnumber"},
    {"label": "CVV"},
    {"label": "Father's name"},
    {"label": "Emergency contact phone"},
    {"label": "Username"},
    {"label": "Verification code"},
])
def test_never_suggests_into_secrets_or_other_people(field) -> None:
    assert classify(field) == ""


@pytest.fixture
def profile(config):
    con = connect(config.database_path)
    for key, value in [("given_name", "Kunal"), ("date_of_birth", "1996-03-30"), ("address.state", "WA"),
                       ("nationality", "INDIAN"), ("gender", "M"), ("phone", "(480) 410-5749")]:
        facts.add_manual(con, key, value)
    with con:  # a pending fact must never be suggested
        con.execute("INSERT INTO facts(key, value, norm, status, created_at) VALUES ('email', 'x@y.z', 'x@y.z', 'pending', 0)")
    return con


def values(con, field):
    return [(s["value"], s["option"]) for s in suggestions(con, field)]


def test_dates_follow_the_field_format(profile) -> None:
    assert values(profile, {"label": "Date of birth", "placeholder": "MM/DD/YYYY"}) == [("03/30/1996", None)]
    assert values(profile, {"label": "Date of birth", "placeholder": "DD/MM/YYYY"}) == [("30/03/1996", None)]
    assert values(profile, {"label": "Birth date", "type": "date"}) == [("1996-03-30", None)]
    assert values(profile, {"label": "Birth year", "placeholder": "YYYY"}) == [("1996", None)]
    months = [{"value": str(i), "text": m} for i, m in enumerate(["Month", "January", "February", "March"])]
    assert values(profile, {"label": "Date of birth month", "type": "select", "options": months}) == [("03", "3")]


def test_dropdowns_get_the_matching_option(profile) -> None:
    assert values(profile, {"label": "State", "type": "select", "options": STATES}) == [("WA", "WA")]
    states_by_name = [{"value": "wash", "text": "Washington"}]
    assert values(profile, {"label": "State", "type": "select", "options": states_by_name}) == [("WA", "wash")]
    countries = [{"value": "US", "text": "United States"}, {"value": "IN", "text": "India"}]
    assert values(profile, {"label": "Nationality", "type": "select", "options": countries}) == [("INDIAN", "IN")]
    genders = [{"value": "", "text": "Select…"}, {"value": "f", "text": "Female"}, {"value": "m", "text": "Male"}]
    assert values(profile, {"label": "Gender", "type": "select", "options": genders}) == [("M", "m")]
    assert values(profile, {"label": "State", "type": "select", "options": STATES[:2]}) == []  # no matching option


def test_only_approved_facts_are_suggested(profile) -> None:
    assert values(profile, {"type": "email"}) == []
    assert values(profile, {"label": "First name"})[0][0] == "Kunal"


@pytest.fixture
def server(config, profile):
    handler = make_handler(ServerConfig(config.database_path, "m", "h", 5, token="s3cret"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def post(port, body, token=None):
    conn = HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    conn.request("POST", "/api/suggest", json.dumps(body), headers)
    response = conn.getresponse()
    return response.status, json.loads(response.read())


def test_suggest_endpoint_needs_the_token(server) -> None:
    fields = {"fields": [{"label": "First name"}, {"label": "Password", "type": "password"}]}
    assert post(server, fields)[0] == 401
    assert post(server, fields, "wrong")[0] == 401
    status, data = post(server, fields, "s3cret")
    assert status == 200 and data["results"][0][0]["value"] == "Kunal" and data["results"][1] == []
    assert post(server, {"fields": []}, "s3cret")[0] == 400


def test_suggest_is_off_without_a_token(config, profile) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ServerConfig(config.database_path, "m", "h", 5)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert post(httpd.server_address[1], {"fields": [{"label": "First name"}]}, "anything")[0] == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_token_file_is_private(tmp_path) -> None:
    import os
    import stat

    from rigveda.config import load_token, token_path

    token = load_token()
    assert load_token() == token and len(token) > 30
    assert stat.S_IMODE(os.stat(token_path()).st_mode) == 0o600
    assert load_token(renew=True) != token


def test_address_parts_come_from_the_same_home(tmp_path, config, monkeypatch) -> None:
    from rigveda import llm
    from rigveda.indexer import index_paths

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "old.txt").write_text("Kunal lives at 1 Old St, Seattle")
    (docs / "other.txt").write_text("Kunal, Seattle")
    (docs / "new.txt").write_text("Kunal lives at 9 New Rd, Sammamish")
    import os
    os.utime(docs / "new.txt", (2_000_000_000, 2_000_000_000))  # the newest file
    found = {"old.txt": [("address.street", "1 Old St"), ("address.city", "Seattle")], "other.txt": [("address.city", "Seattle")],
             "new.txt": [("address.street", "9 New Rd"), ("address.city", "Sammamish")]}

    def model(host, model, prompt):
        name = next(name for name in found if found[name][0][1] in prompt)
        return [{"key": key, "value": value, "evidence": value} for key, value in found[name]]

    monkeypatch.setattr(llm, "extract_facts", model)
    config.owner_name = "Kunal"
    index_paths(config.database_path, [str(docs)], config, log=lambda message: None)
    con = connect(config.database_path)
    for fact in facts.listing(con):
        facts.decide(con, fact["id"], "approved")
    assert values(con, {"label": "Street address"})[0][0] == "9 New Rd"
    assert values(con, {"label": "City"})[0][0] == "Sammamish"  # although Seattle is in more files
