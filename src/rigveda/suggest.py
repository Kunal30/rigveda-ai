"""Form-field suggestions: work out what a field asks for, then offer facts from your files in the shape it expects."""

from __future__ import annotations

import re
import sqlite3

from . import facts

# HTML autocomplete tokens (https://html.spec.whatwg.org/#autofill) that name a fact directly.
AUTOCOMPLETE = {
    "name": "full_name", "given-name": "given_name", "family-name": "family_name", "email": "email",
    "tel": "phone", "tel-national": "phone", "street-address": "address.street", "address-line1": "address.street",
    "address-line2": "address.unit", "address-level2": "address.city", "address-level1": "address.state",
    "postal-code": "address.postal_code", "country": "address.country", "country-name": "address.country",
    "bday": "date_of_birth", "bday-day": "date_of_birth", "bday-month": "date_of_birth", "bday-year": "date_of_birth",
    "sex": "gender", "organization": "employer", "organization-title": "job_title",
}

# Never suggest into these: passwords, one-time codes and payment cards.
FORBIDDEN_AUTOCOMPLETE = re.compile(r"(^|\s)(current-password|new-password|one-time-code|cc-[\w-]+)(\s|$)")
FORBIDDEN_TEXT = re.compile(r"\bpin\b(?! ?code)|\b(password|passcode|otp|one time|verification code|captcha|card number|credit card|"
                            r"debit card|cvv|cvc|csc|security code|card expir\w*|name on card|cardholder|iban|routing|"
                            r"account number)\b")

# What a field's label, name or placeholder says, most specific first.
PATTERNS = [
    # Someone else's details, or an account name: no suggestion rather than yours.
    (r"father|mother|spouse|husband|wife|parent|guardian|emergency|reference|referee|beneficiary|nominee|"
     r"contact person|user ?name|login|nick ?name|screen name", ""),
    (r"passport.*(expir|valid until|valid till)|(expir|valid until).*passport", "passport.expiry"),
    (r"passport", "passport.number"),
    (r"(driver'?s?|driving) licen[cs]e.*(state|issued)|licen[cs]e state|issuing state", "drivers_license.state"),
    (r"(driver'?s?|driving) licen[cs]e|\bdl\b|licen[cs]e (number|no)", "drivers_license.number"),
    (r"social security|\bssn\b", "ssn"),
    (r"(place|city|country) of birth|birth ?place|born in", "place_of_birth"),
    (r"(date|day|month|year) of birth|birth ?(date|day|month|year)|birthday|\bdob\b|\bbday\b|\bborn\b", "date_of_birth"),
    (r"nationality|citizenship|citizen of", "nationality"),
    (r"(first|given|fore) ?name|\bfname\b", "given_name"),
    (r"(last|family) ?name|surname|\blname\b", "family_name"),
    (r"middle", ""),  # not stored; better no suggestion than the wrong part of a name
    (r"e-?mail", "email"),
    (r"phone|mobile|cell|telephone|\btel\b|contact number", "phone"),
    (r"address (line )?2|\bapt\b|apartment|suite|\bunit\b|flat (no|number)", "address.unit"),
    (r"\bzip|postal|post ?code|\bpin ?code", "address.postal_code"),
    (r"\bcity\b|\btown\b", "address.city"),
    (r"\bstate\b|province|\bregion\b", "address.state"),
    (r"country", "address.country"),
    (r"street|address (line )?1|\baddress\b", "address.street"),
    (r"gender|\bsex\b", "gender"),
    (r"employer|company|organi[sz]ation|work(s|ing)? (at|for)", "employer"),
    (r"job title|position|occupation|designation", "job_title"),
    (r"school|university|college|institution", "school"),
    (r"degree|qualification", "degree"),
    (r"(full|legal|your) name|^name$|\bname\b", "full_name"),
]

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
COUNTRIES = [
    {"us", "usa", "united states", "united states of america", "america"},
    {"in", "ind", "india", "bharat"},
    {"uk", "gb", "gbr", "united kingdom", "great britain"},
    {"ca", "can", "canada"},
]
GENDERS = [{"m", "male", "man"}, {"f", "female", "woman"}]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def words(text: str) -> str:
    """"firstName", "first_name" and "First Name:" all become "first name"."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return " ".join(re.sub(r"[^\w']+|_", " ", text).lower().split())


def describe(field: dict) -> str:
    parts = [field.get(name, "") for name in ("label", "ariaLabel", "placeholder", "name", "id", "nearby")]
    return " | ".join(words(part) for part in parts if part)


def forbidden(field: dict) -> bool:
    if str(field.get("type", "")).lower() in {"password", "hidden", "file", "submit", "button", "checkbox", "radio"}:
        return True
    if FORBIDDEN_AUTOCOMPLETE.search(str(field.get("autocomplete", "")).lower()):
        return True
    own = " ".join(words(field.get(name, "")) for name in ("label", "ariaLabel", "placeholder", "name", "id"))
    return bool(FORBIDDEN_TEXT.search(own))


def date_part(field: dict, text: str) -> str:
    """"day", "month" or "year" for a field that holds one part of a date, else "" (a whole date such as
    MM/DD/YYYY mentions all three)."""
    token = str(field.get("autocomplete", "")).lower()
    for part in ("day", "month", "year"):
        if token.endswith("-" + part):
            return part
    found = [part for part, pattern in (("day", r"\bday\b|\bdd\b"), ("month", r"\bmonth\b|\bmm\b"),
                                        ("year", r"\byear\b|\byyyy\b|\byy\b")) if re.search(pattern, text)]
    return found[0] if len(found) == 1 else ""


def classify(field: dict) -> str:
    """The fact key a field asks for, or "" when unsure. Labels the person reads win over hidden names."""
    if forbidden(field):
        return ""
    for token in str(field.get("autocomplete", "")).lower().split():
        if token in AUTOCOMPLETE:
            return AUTOCOMPLETE[token]
    if str(field.get("type", "")).lower() == "email":
        return "email"
    if str(field.get("type", "")).lower() == "tel":
        return "phone"
    for name in ("label", "ariaLabel", "placeholder", "name", "id", "nearby"):
        text = words(field.get(name, ""))
        if not text:
            continue
        for pattern, key in PATTERNS:
            if re.search(pattern, text):
                return key
    return ""


def format_date(iso: str, field: dict, part: str) -> str:
    year, month, day = iso.split("-")
    if part == "year":
        return year
    if part == "month":
        return month
    if part == "day":
        return day
    if str(field.get("type", "")).lower() == "date":
        return iso
    hint = (field.get("placeholder", "") + " " + field.get("label", "")).lower()
    if re.search(r"dd\s*[/.-]\s*mm", hint):
        separator = re.search(r"dd\s*([/.-])", hint).group(1)
        return f"{day}{separator}{month}{separator}{year}"
    if re.search(r"yyyy\s*[/.-]\s*mm", hint):
        return iso
    separator = "-" if re.search(r"mm\s*-\s*dd", hint) else "/"
    return f"{month}{separator}{day}{separator}{year}"  # US order unless the field says otherwise


def aliases(key: str, value: str) -> set[str]:
    """Other ways of writing a value, used to pick the right option in a dropdown."""
    plain = words(value)
    names = {plain}
    if key in {"address.state", "drivers_license.state"}:
        for code, state in US_STATES.items():
            if plain in {code.lower(), state.lower()}:
                names |= {code.lower(), state.lower()}
    for group in (COUNTRIES if key in {"address.country", "nationality"} else GENDERS if key == "gender" else []):
        if plain in group:
            names |= group
    if key == "nationality" and plain in {"indian", "american"}:
        names |= next(group for group in COUNTRIES if ("india" if plain == "indian" else "usa") in group)
    return names


def pick_option(key: str, value: str, options: list[dict], part: str = "") -> dict | None:
    """The dropdown option that means this value, if there is one."""
    if part:
        number = int(value)
        for option in options:
            for text in (option.get("value", ""), option.get("text", "")):
                text = words(text)
                if text.isdigit() and int(text) == number:
                    return option
                if part == "month" and text in {MONTHS[number - 1], MONTHS[number - 1][:3]}:
                    return option
        return None
    wanted = aliases(key, value)
    for option in options:
        if words(option.get("value", "")) in wanted or words(option.get("text", "")) in wanted:
            return option
    return None


def suggestions(con: sqlite3.Connection, field: dict, limit: int = 3) -> list[dict]:
    """Up to `limit` values for a field from facts you have not rejected, best first, each with where it came from."""
    key = classify(field)
    if not key:
        return []
    text = describe(field)
    part = date_part(field, text) if key in facts.DATE_KEYS else ""
    usable = facts.usable(con)
    candidates = [fact for fact in usable if fact["key"] == key]
    if key.startswith("address"):
        # Your current address first, and every part of it from the same place: the newest street decides,
        # then city, ZIP and the rest come from files that mention that street.
        streets = sorted((fact for fact in usable if fact["key"] == "address.street"), key=lambda fact: -fact["newest"])
        home = {source["path"] for source in streets[0]["sources"]} if streets else set()
        candidates.sort(key=lambda fact: (not home & {source["path"] for source in fact["sources"]}, -fact["newest"]))
    results, seen = [], set()
    for fact in candidates:
        value = fact["value"]
        if key in facts.DATE_KEYS:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                continue
            value = format_date(value, field, part)
        option = None
        if field.get("options"):
            option = pick_option(key, value, field["options"], part)
            if option is None:
                continue
        if value in seen:
            continue
        seen.add(value)
        source = fact["sources"][0]["path"].rsplit("/", 1)[-1] if fact["sources"] else "added by you"
        results.append({"key": key, "label": facts.KEYS[key], "value": value, "source": source,
                        "option": option.get("value") if option else None})
        if len(results) == limit:
            break
    return results
