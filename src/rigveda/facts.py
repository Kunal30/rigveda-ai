"""Personal facts profile: details such as name, birth date and address pulled out of documents for review.

Facts start as "pending" and are suggested in forms straight away; approving one only ranks it first, and
rejecting one stops it being suggested. Each fact keeps the files and the exact words it was read from.
"""

from __future__ import annotations

import re
import sqlite3
import datetime
import time
import unicodedata
from typing import Callable

from . import llm
from .config import Config

VERSION = "3"  # bump to re-scan every document after changing the prompt or the checks
MAX_CHUNKS_PER_DOCUMENT = 20  # a long book that mentions "address" often is not worth hundreds of model calls
SKIP_KINDS = {"audio", "video", "subtitles", "binary"}  # recordings and films rarely state your details; build files never do

# The keys a form can ask for, with the label shown on the review page.
KEYS = {
    "full_name": "Full name",
    "given_name": "First name",
    "family_name": "Last name",
    "date_of_birth": "Date of birth",
    "place_of_birth": "Place of birth",
    "gender": "Gender",
    "nationality": "Nationality",
    "email": "Email",
    "phone": "Phone",
    "address": "Full address",
    "address.street": "Street address",
    "address.unit": "Apartment or unit",
    "address.city": "City",
    "address.state": "State or province",
    "address.postal_code": "ZIP or postal code",
    "address.country": "Country",
    "employer": "Employer",
    "job_title": "Job title",
    "school": "School or university",
    "degree": "Degree",
    "passport.number": "Passport number",
    "passport.expiry": "Passport expiry date",
    "drivers_license.number": "Driver's license number",
    "drivers_license.state": "Driver's license state",
    "ssn": "Social Security number",
}
DATE_KEYS = {"date_of_birth", "passport.expiry"}
SENSITIVE = {"ssn", "passport.number", "drivers_license.number"}  # masked unless asked to show
STATUSES = ("pending", "approved", "rejected")


# Words that suggest a passage states someone's details; used when no owner name is set.
PERSONAL = re.compile(
    r"\b(date of birth|d\.?o\.?b|born|birth|passport|driver'?s? licen[cs]e|social security|ssn|nationality|citizenship|"
    r"address|resident|tenant|applicant|employee|e-?mail|phone|mobile)\b|[\w.+-]+@[\w-]+\.\w+",
    re.IGNORECASE,
)

Log = Callable[[str], None]


IDENTIFIERS = {"ssn", "passport.number", "drivers_license.number", "address.postal_code", "address.unit"}
UNIT_WORDS = re.compile(r"\b(apartment|apt|unit|suite|ste|no|number|flat)\b\.?|#")


def normalize(key: str, value: str) -> str:
    """Comparison form of a value, so "123-45-6789" and "123456789", "(480) 410-5749" and "+1 480 410 5749",
    or "Pine Lake Rd. SE" and "PINE LAKE RD SE" count as the same fact."""
    text = unicodedata.normalize("NFKC", value).casefold()
    if key == "phone":
        digits = re.sub(r"\D", "", text)
        return digits[-10:] if len(digits) > 10 else digits  # ignore a country code
    if key == "email":
        return text.strip()
    if key == "address.unit":
        text = UNIT_WORDS.sub(" ", text)
    if key in IDENTIFIERS:
        return re.sub(r"[\W_]+", "", text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def grounded(key: str, value: str, evidence: str, chunk: str) -> bool:
    """Keep a fact only if the text really says it: the evidence is in the passage, and so is the value
    (dates excepted, since they are rewritten as YYYY-MM-DD)."""
    passage = _squash(chunk)
    if not value.strip() or not _squash(evidence) or _squash(evidence) not in passage:
        return False
    if key in DATE_KEYS:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()))
    return _squash(value) in passage


def plausible(key: str, value: str, config: Config) -> bool:
    """Reject values that cannot be right: a redacted ID number, a birth date in the last ten years, or a name
    that isn't the owner's (documents such as passports also name parents and spouses)."""
    if key in SENSITIVE and re.search(r"[*•]|[xX]{2,}", value):
        return False  # a redacted copy such as ***-**-6789, not the number itself
    if key == "date_of_birth":
        return int(value[:4]) <= datetime.date.today().year - 10
    terms = owner_terms(config)
    if terms and key in {"full_name", "given_name"}:
        return terms[0] in re.findall(r"\w+", value.casefold())  # must include the owner's first name
    if terms and key == "family_name":
        return terms[-1] in re.findall(r"\w+", value.casefold())
    return True


def owner_terms(config: Config) -> list[str]:
    return [part for part in re.findall(r"\w+", config.owner_name.casefold()) if len(part) >= 3]


def candidate(text: str, terms: list[str]) -> bool:
    if terms:  # with a known owner, only passages that name them
        words = set(re.findall(r"\w+", text.casefold()))
        return any(term in words for term in terms)
    return bool(PERSONAL.search(text))


def prompt(text: str, config: Config) -> str:
    who = (f"{config.owner_name}, the person these files belong to" if config.owner_name else
           "the person these files belong to (for example the tenant, applicant, employee, patient or account holder)")
    keys = ", ".join(KEYS)
    return (f"Extract personal facts about {who} from the text below. Only facts about that one person; "
            "never about companies, landlords, agents or other people.\n"
            f"Use only these keys: {keys}.\n"
            "value: exactly as written in the text, except dates, which must be YYYY-MM-DD. "
            "evidence: the exact words from the text that state the fact, copied verbatim.\n"
            "If the text states no such facts, return an empty list.\n\nTEXT:\n" + text)


def add_fact(con: sqlite3.Connection, key: str, value: str, status: str = "pending") -> int:
    """The id of the fact with this key and value, created if new. An existing fact keeps its status."""
    value = " ".join(value.split())
    norm = normalize(key, value)
    row = con.execute("SELECT id FROM facts WHERE key=? AND norm=?", (key, norm)).fetchone()
    if row:
        return row[0]
    now = int(time.time())
    cursor = con.execute("INSERT INTO facts(key, value, norm, status, created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (key, value, norm, status, now, None if status == "pending" else now))
    return cursor.lastrowid


def read_document(con: sqlite3.Connection, path: str, config: Config) -> list[tuple[str, str, str, str]]:
    """Ask the model for facts in one document. Returns (key, value, location, evidence) for those the text supports."""
    terms = owner_terms(config)
    rows = con.execute("SELECT location, text FROM chunks WHERE path=? ORDER BY ordinal", (path,)).fetchall()
    found = []
    for row in [row for row in rows if candidate(row["text"], terms)][:MAX_CHUNKS_PER_DOCUMENT]:
        for item in llm.extract_facts(config.ollama, config.model, prompt(row["text"], config)):
            key, value, evidence = item.get("key", ""), str(item.get("value", "")), str(item.get("evidence", ""))
            if key in KEYS and grounded(key, value, evidence, row["text"]) and plausible(key, value.strip(), config):
                found.append((key, value.strip(), row["location"], " ".join(evidence.split())))
    return found


def scan(con: sqlite3.Connection, config: Config, log: Log, rescan: bool = False) -> int:
    """Look for facts in documents not yet scanned (or all of them with rescan). Returns documents scanned."""
    if rescan:
        with con:
            con.execute("DELETE FROM fact_scans")
    renormalize(con)
    placeholders = ",".join("?" * len(SKIP_KINDS))
    paths = [row[0] for row in con.execute(f"""SELECT path FROM documents d WHERE kind NOT IN ({placeholders})
        AND NOT EXISTS (SELECT 1 FROM fact_scans s WHERE s.path=d.path AND s.version=? AND s.model=?) ORDER BY path""",
        (*SKIP_KINDS, VERSION, config.model))]
    done = 0
    for path in paths:
        try:
            found = read_document(con, path, config)  # slow; the database stays unlocked meanwhile
        except llm.OllamaError as error:
            log(f"Facts scan paused until Ollama is reachable: {error}")
            break
        with con:
            if con.execute("SELECT 1 FROM documents WHERE path=?", (path,)).fetchone() is None:
                continue  # deleted while the model was reading it
            con.execute("DELETE FROM fact_sources WHERE path=?", (path,))  # replaced by this scan's findings
            for key, value, location, evidence in found:
                con.execute("INSERT OR IGNORE INTO fact_sources(fact_id, path, location, evidence) VALUES (?, ?, ?, ?)",
                            (add_fact(con, key, value), path, location, evidence))
            con.execute("INSERT OR REPLACE INTO fact_scans(path, version, model, scanned_at) VALUES (?, ?, ?, ?)",
                        (path, VERSION, config.model, int(time.time())))
        done += 1
        if found:
            log(f"Found {len(found)} personal fact(s) in {path}")
    prune(con)
    return done


def merge(con: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Fold one fact into another with the same key and value. A decision you made on either one is kept."""
    statuses = {row[0]: row[1] for row in con.execute("SELECT id, status FROM facts WHERE id IN (?, ?)", (source_id, target_id))}
    if statuses.get(target_id) == "pending" and statuses.get(source_id) != "pending":
        con.execute("UPDATE facts SET status=?, decided_at=(SELECT decided_at FROM facts WHERE id=?) WHERE id=?",
                    (statuses[source_id], source_id, target_id))
    con.execute("UPDATE OR IGNORE fact_sources SET fact_id=? WHERE fact_id=?", (target_id, source_id))
    con.execute("DELETE FROM facts WHERE id=?", (source_id,))


def renormalize(con: sqlite3.Connection) -> None:
    """Bring stored comparison forms up to date after `normalize` changes, merging facts that now match."""
    with con:
        for row in con.execute("SELECT id, key, value, norm FROM facts ORDER BY id").fetchall():
            norm = normalize(row["key"], row["value"])
            if norm == row["norm"]:
                continue
            other = con.execute("SELECT id FROM facts WHERE key=? AND norm=?", (row["key"], norm)).fetchone()
            if other:
                merge(con, row["id"], other[0])
            else:
                con.execute("UPDATE facts SET norm=? WHERE id=?", (norm, row["id"]))


def prune(con: sqlite3.Connection) -> None:
    """Drop pending facts whose every source file was deleted or changed. Approved and rejected ones are kept."""
    with con:
        con.execute("DELETE FROM facts WHERE status='pending' AND id NOT IN (SELECT fact_id FROM fact_sources)")


def listing(con: sqlite3.Connection, status: str | None = None) -> list[dict]:
    """Facts with their sources, grouped by key in catalog order."""
    rows = con.execute("SELECT * FROM facts" + (" WHERE status=?" if status else ""), (status,) if status else ()).fetchall()
    order = {key: index for index, key in enumerate(KEYS)}
    facts = []
    for row in rows:
        sources = con.execute("""SELECT s.path, s.location, s.evidence, d.modified_ns FROM fact_sources s
            JOIN documents d ON d.path=s.path WHERE s.fact_id=? ORDER BY d.modified_ns DESC""", (row["id"],)).fetchall()
        facts.append({
            "id": row["id"], "key": row["key"], "label": KEYS.get(row["key"], row["key"]), "value": row["value"],
            "status": row["status"], "sensitive": row["key"] in SENSITIVE,
            "newest": sources[0]["modified_ns"] if sources else 0,
            "files": len({s["path"] for s in sources}),
            "sources": [{"path": s["path"], "location": s["location"], "evidence": s["evidence"]} for s in sources],
        })
    # Within a key: what you approved first, then values seen in the most files, then the newest file.
    facts.sort(key=lambda fact: (order.get(fact["key"], len(order)), fact["status"] != "approved", -fact["files"],
                                 -fact["newest"], fact["value"]))
    return facts


def usable(con: sqlite3.Connection) -> list[dict]:
    """Facts that may be suggested: everything found or typed in except what you rejected, approved first."""
    return [fact for fact in listing(con) if fact["status"] != "rejected"]


def decide(con: sqlite3.Connection, fact_id: int, status: str, value: str | None = None) -> int:
    """Approve, reject or reset a fact, optionally correcting its value. Returns the fact's id, which changes
    when the corrected value matches another fact of the same key (the two are merged)."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    row = con.execute("SELECT key, value FROM facts WHERE id=?", (fact_id,)).fetchone()
    if row is None:
        raise KeyError(fact_id)
    now = int(time.time())
    with con:
        if value is not None and " ".join(value.split()) != row["value"]:
            value = " ".join(value.split())
            if not value:
                raise ValueError("A fact needs a value.")
            norm = normalize(row["key"], value)
            other = con.execute("SELECT id FROM facts WHERE key=? AND norm=? AND id<>?", (row["key"], norm, fact_id)).fetchone()
            if other:
                merge(con, fact_id, other[0])
                fact_id = other[0]
            else:
                con.execute("UPDATE facts SET value=?, norm=? WHERE id=?", (value, norm, fact_id))
        con.execute("UPDATE facts SET status=?, decided_at=? WHERE id=?", (status, None if status == "pending" else now, fact_id))
    return fact_id


def add_manual(con: sqlite3.Connection, key: str, value: str) -> int:
    """A fact you typed in yourself; approved straight away."""
    if key not in KEYS:
        raise ValueError(f"Unknown key {key!r}. Known keys: {', '.join(KEYS)}")
    if not value.strip():
        raise ValueError("A fact needs a value.")
    with con:
        fact_id = add_fact(con, key, value, status="approved")
    return decide(con, fact_id, "approved")


def mask(value: str) -> str:
    """Hide all but the last four characters, e.g. •••••6789."""
    return "•" * max(len(value) - 4, 0) + value[-4:]
