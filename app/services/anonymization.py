"""Anonymization helper: project-level term -> replacement list,
scan for hits, apply on demand with offset-safe rewriting of coded segments.
The mapping never leaves the project database."""
from __future__ import annotations

from typing import Any

from ..db import Database, now_iso
from .core import ValidationError, NotFound


def list_mapping(db: Database) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM anonymization_map ORDER BY LENGTH(term) DESC, term")


def add_mapping(db: Database, term: str, replacement: str) -> dict[str, Any]:
    term = (term or "").strip()
    replacement = (replacement or "").strip()
    if len(term) < 2:
        raise ValidationError("term must have at least 2 characters.")
    if not replacement:
        raise ValidationError("replacement must not be empty.")
    if term in replacement:
        raise ValidationError("replacement must not contain the term itself.")
    with db.write("Add anonymization term", record_undo=False) as tx:
        tx.execute("INSERT OR REPLACE INTO anonymization_map(term, replacement) VALUES (?,?)", (term, replacement))
        row = tx.one("SELECT * FROM anonymization_map WHERE term=?", (term,))
    return row


def delete_mapping(db: Database, mapping_id: int) -> None:
    if not db.one("SELECT 1 FROM anonymization_map WHERE id=?", (mapping_id,)):
        raise NotFound(f"Mapping {mapping_id} not found.")
    with db.write("Delete anonymization term", record_undo=False) as tx:
        tx.execute("DELETE FROM anonymization_map WHERE id=?", (mapping_id,))


def scan(db: Database, interview_id: str | None = None) -> list[dict[str, Any]]:
    """Every occurrence of every mapped term in the transcripts (incl. interviewer turns)."""
    mapping = list_mapping(db)
    if not mapping:
        return []
    sql = "SELECT p.*, i.role_label FROM paragraphs p JOIN interviews i ON i.id = p.interview_id"
    params: list = []
    if interview_id:
        sql += " WHERE p.interview_id=?"; params.append(interview_id)
    hits = []
    for p in db.query(sql + " ORDER BY i.id, p.idx", params):
        for m in mapping:
            start = 0
            while True:
                pos = p["text"].find(m["term"], start)
                if pos == -1:
                    break
                a, b = max(0, pos - 50), min(len(p["text"]), pos + len(m["term"]) + 50)
                hits.append({"mapping_id": m["id"], "term": m["term"], "replacement": m["replacement"],
                             "interview_id": p["interview_id"], "paragraph_id": p["id"], "para_no": p["para_no"],
                             "speaker": p["speaker"], "start_char": pos, "end_char": pos + len(m["term"]),
                             "context": ("…" if a else "") + p["text"][a:b] + ("…" if b < len(p["text"]) else "")})
                start = pos + 1
    return hits


def check_text(text: str, mapping: list[dict[str, Any]]) -> list[str]:
    """Return the mapped terms that still occur in ``text``."""
    return [m["term"] for m in mapping if m["term"] and m["term"] in text]


def _shift(s: int, e: int, a: int, b: int, rep_len: int) -> tuple[int, int]:
    delta = rep_len - (b - a)
    if a < s < b:
        ns = a
    elif s >= b:
        ns = s + delta
    else:
        ns = s
    if e >= b:
        ne = e + delta
    elif a < e < b:
        ne = a + rep_len
    else:
        ne = e
    return ns, ne


def apply(db: Database, hits: list[dict[str, Any]] | None = None, interview_id: str | None = None) -> dict[str, Any]:
    """Replace mapped terms in the stored paragraph text.  ``hits`` (from scan) restricts
    the replacement to the selected occurrences; None = all hits.  Segment and
    confidential offsets are shifted, text snapshots re-derived, the undo log cleared."""
    selected = hits if hits is not None else scan(db, interview_id)
    if not selected:
        return {"replaced": 0, "paragraphs": 0}
    by_par: dict[int, list[dict]] = {}
    for h in selected:
        by_par.setdefault(h["paragraph_id"], []).append(h)
    replaced = 0
    with db.write("Apply anonymization", record_undo=False) as tx:
        for pid, phits in by_par.items():
            p = tx.one("SELECT * FROM paragraphs WHERE id=?", (pid,))
            if not p:
                continue
            text = p["text"]
            # validate hits against current text and sort descending so offsets stay valid
            valid = []
            for h in phits:
                a, b = int(h["start_char"]), int(h["end_char"])
                if 0 <= a < b <= len(text) and text[a:b] == h["term"]:
                    valid.append((a, b, h["replacement"]))
            valid.sort(reverse=True)
            # drop overlapping selections (keep the later one)
            filtered, last_a = [], None
            for a, b, rep in valid:
                if last_a is not None and b > last_a:
                    continue
                filtered.append((a, b, rep))
                last_a = a
            segs = tx.query("SELECT id, start_char, end_char FROM segments WHERE paragraph_id=?", (pid,))
            confs = tx.query("SELECT id, start_char, end_char FROM confidential_ranges WHERE paragraph_id=?", (pid,))
            ens = tx.query("SELECT id, start_char, end_char FROM en_original_ranges WHERE paragraph_id=?", (pid,))
            nqs = tx.query("SELECT id, start_char, end_char FROM no_quote_ranges WHERE paragraph_id=?", (pid,))
            for a, b, rep in filtered:
                text = text[:a] + rep + text[b:]
                for s in segs:
                    s["start_char"], s["end_char"] = _shift(s["start_char"], s["end_char"], a, b, len(rep))
                for c in confs + ens + nqs:
                    c["start_char"], c["end_char"] = _shift(c["start_char"], c["end_char"], a, b, len(rep))
                replaced += 1
            for e in ens:
                tx.execute("UPDATE en_original_ranges SET start_char=?, end_char=? WHERE id=?",
                           (e["start_char"], e["end_char"], e["id"]))
            for e in nqs:
                tx.execute("UPDATE no_quote_ranges SET start_char=?, end_char=? WHERE id=?",
                           (e["start_char"], e["end_char"], e["id"]))
            tx.execute("UPDATE paragraphs SET text=? WHERE id=?", (text, pid))
            for s in segs:
                tx.execute("UPDATE segments SET start_char=?, end_char=?, text_snapshot=?, updated_at=? WHERE id=?",
                           (s["start_char"], s["end_char"], text[s["start_char"]:s["end_char"]], now_iso(), s["id"]))
            for c in confs:
                tx.execute("UPDATE confidential_ranges SET start_char=?, end_char=? WHERE id=?",
                           (c["start_char"], c["end_char"], c["id"]))
    db.clear_undo()
    return {"replaced": replaced, "paragraphs": len(by_par)}
