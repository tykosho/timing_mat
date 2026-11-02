"""
Parses a registration CSV (e.g. exported from Google Forms / Sheets) into participant rows.

Header matching is forgiving: "Full Name", "Email Address", "Bib Number", "Tag EPC" etc. all work.
Recognised columns (any order, extras are ignored):
  name  (or "first name" + "last name")   required
  email                                   optional
  bib                                     optional - auto-assigned when blank
  epc / tag                               optional - when the tag is already known
  notes / goal / comments                 optional
"""
import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple

from core.db import RegistrationError, normalize_epc

_NAME_HEADERS = ("name", "full name", "participant name", "runner name", "participant", "runner")
_EPC_HEADERS = ("epc", "tag", "tag id", "tag epc", "rfid", "rfid tag", "rfid epc")
_NOTE_HEADERS = ("notes", "note", "comments", "comment", "goal", "target")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (header or "").lower()).strip()


def _find(headers: Dict[str, str], exact: Tuple[str, ...] = (), contains: Tuple[str, ...] = (),
          avoid: Tuple[str, ...] = ()) -> Optional[str]:
    """Returns the original header whose normalised form matches (exact names first, then substrings)."""
    for want in exact:
        for norm, original in headers.items():
            if norm == want:
                return original
    for norm, original in headers.items():
        if any(c in norm for c in contains) and not any(a in norm for a in avoid):
            return original
    return None


def _detect_delimiter(first_line: str) -> str:
    counts = {d: first_line.count(d) for d in (",", ";", "\t")}
    return max(counts, key=counts.get) if any(counts.values()) else ","


def parse_registration_csv(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns (rows, errors). Each row: {line, name, email, bib (int|None), epc (str|None), notes}.
    Each error: {line, reason}. Raises RegistrationError if the file has no usable name column.
    """
    text = (text or "").lstrip("﻿")
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise RegistrationError("The CSV is empty")

    reader = csv.DictReader(io.StringIO(text), delimiter=_detect_delimiter(lines[0]))
    headers = {_norm(h): h for h in (reader.fieldnames or []) if h and _norm(h)}

    name_col = _find(headers, exact=_NAME_HEADERS)
    first_col = _find(headers, contains=("first",), avoid=("email",))
    last_col = _find(headers, contains=("last", "surname", "family"), avoid=("email",))
    if not name_col and not (first_col and last_col):
        name_col = _find(headers, contains=("name",), avoid=("email", "emergency", "contact", "team", "club"))
    if not name_col and not (first_col and last_col):
        raise RegistrationError("Couldn't find a name column. Add a 'Name' (or 'First Name' + 'Last Name') column.")

    email_col = _find(headers, contains=("email", "e mail"))
    bib_col = _find(headers, exact=("bib", "bib number", "bib no", "bib num"), contains=("bib",))
    epc_col = _find(headers, exact=_EPC_HEADERS)
    notes_col = _find(headers, exact=_NOTE_HEADERS, contains=("note", "comment", "goal"))

    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for i, raw in enumerate(reader, start=2):  # line 1 is the header
        if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
            continue  # blank line

        def cell(col: Optional[str]) -> str:
            return (raw.get(col) or "").strip() if col else ""

        name = cell(name_col) if name_col else f"{cell(first_col)} {cell(last_col)}".strip()
        if not name:
            errors.append({"line": i, "reason": "missing name"})
            continue

        bib = None
        bib_text = cell(bib_col)
        if bib_text:
            if not bib_text.lstrip("#").isdigit() or int(bib_text.lstrip("#")) <= 0:
                errors.append({"line": i, "name": name, "reason": f"invalid bib '{bib_text}'"})
                continue
            bib = int(bib_text.lstrip("#"))

        try:
            epc = normalize_epc(cell(epc_col))
        except RegistrationError as e:
            errors.append({"line": i, "name": name, "reason": str(e)})
            continue

        rows.append({"line": i, "name": name, "email": cell(email_col), "bib": bib, "epc": epc, "notes": cell(notes_col)})
    return rows, errors
