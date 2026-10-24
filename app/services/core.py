"""Core domain services.  Every function that writes takes a ``Tx`` (from
``Database.write``) so that every action shares one code path and one undo
journal.  Validation errors raise ``ValidationError`` (HTTP 422)."""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from ..db import AGENCY_VALUES, Database, Tx, now_iso
from .importer import ParsedTranscript

VALENCES = {-1, 0, 1}
# Agency: uniform value list, no free text; empty = unset
AGENCIES = set(AGENCY_VALUES)
AGENCY_ORDER = list(AGENCY_VALUES)
AGENCY_MEANINGS: dict[str, str] = {
    "GOV-CENTRAL": "central government / NDRC / NEA / BMWK / legislator",
    "GOV-PROV": "province, federal state, municipality",
    "REGULATOR": "BNetzA and similar",
    "GRID": "grid companies, TSO, DSO",
    "SOE": "state-owned enterprises other than grid",
    "MARKET": "private market actors incl. aggregators, banks, customers",
    "ASSOC": "associations",
    "RESEARCH": "academia",
    "MULTI": "explicitly several actor types jointly (co-steering)",
}
AGENCY_TOOLTIP = "Agency: which actor type brought about the situation or could change it (Huang & Malkin 2026)"
QUOTE_BLOCK_MESSAGE = "Segment overlaps a quotation-blocked passage and cannot be a quote candidate."
MECHANISMS = {"inducing", "blocking"}
MEMO_SCOPES = {"code", "interview", "segment", "general"}
MAX_DEPTH = 3


class ValidationError(ValueError):
    pass


class NotFound(LookupError):
    pass


def phase(db_or_tx) -> str:
    row = db_or_tx.one("SELECT value FROM settings WHERE key='pilot_phase'")
    return "pilot" if row and str(row["value"]).lower() == "true" else "main"


# ---------------------------------------------------------------------------
# interviews
# ---------------------------------------------------------------------------

def list_interviews(db: Database) -> list[dict[str, Any]]:
    rows = db.query("""
        SELECT i.*,
          (SELECT COUNT(*) FROM segments s JOIN paragraphs p ON p.id = s.paragraph_id
             JOIN coding_passes cp ON cp.id = s.coding_pass_id
           WHERE p.interview_id = i.id AND cp.name = 'primary') AS segment_count,
          (SELECT COUNT(*) FROM paragraphs p WHERE p.interview_id = i.id AND p.speaker = 'R') AS respondent_turns,
          (SELECT MAX(COALESCE(s.updated_at, s.created_at)) FROM segments s JOIN paragraphs p ON p.id = s.paragraph_id
           WHERE p.interview_id = i.id) AS last_edited
        FROM interviews i ORDER BY i.country, i.id""")
    return rows


def primary_pass_id(db_or_tx, interview_id: str) -> int:
    row = db_or_tx.one("SELECT id FROM coding_passes WHERE interview_id=? AND name='primary'", (interview_id,))
    if not row:
        raise NotFound(f"Interview {interview_id} has no primary coding pass.")
    return row["id"]


def _segment_rows(db_or_tx, where: str, params: tuple | list) -> list[dict[str, Any]]:
    segs = db_or_tx.query(f"""
        SELECT s.*, p.interview_id, p.para_no, p.idx AS paragraph_index, p.timestamp, i.country, i.role_label,
               i.role_category, i.pilot
        FROM segments s JOIN paragraphs p ON p.id = s.paragraph_id JOIN interviews i ON i.id = p.interview_id
        WHERE {where} ORDER BY i.id, p.idx, s.start_char, s.end_char, s.id""", params)
    if not segs:
        return []
    ids = [s["id"] for s in segs]
    codes_by_seg: dict[int, list[dict]] = {}
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        q = ",".join("?" for _ in chunk)
        for r in db_or_tx.query(f"""
            SELECT sc.segment_id, sc.code_id, sc.valence, sc.agency, c.code_key, c.name, c.color, c.type
            FROM segment_codes sc JOIN codes c ON c.id = sc.code_id WHERE sc.segment_id IN ({q})
            ORDER BY c.sort_order""", chunk):
            codes_by_seg.setdefault(r["segment_id"], []).append({
                "code_id": r["code_id"], "code_key": r["code_key"], "name": r["name"], "color": r["color"],
                "type": r["type"], "valence": r["valence"], "agency": r["agency"]})
    inter_by_seg: dict[int, list[dict]] = {}
    for chunk_start in range(0, len(ids), 500):
        chunk = ids[chunk_start:chunk_start + 500]
        q = ",".join("?" for _ in chunk)
        for r in db_or_tx.query(f"SELECT * FROM interactions WHERE segment_id IN ({q})", chunk):
            inter_by_seg.setdefault(r["segment_id"], []).append(r)
    par_ids = list({s["paragraph_id"] for s in segs})
    en_by_par: dict[int, list[tuple[int, int]]] = {}
    for chunk_start in range(0, len(par_ids), 500):
        chunk = par_ids[chunk_start:chunk_start + 500]
        q = ",".join("?" for _ in chunk)
        for r in db_or_tx.query(f"SELECT paragraph_id, start_char, end_char FROM en_original_ranges "
                                f"WHERE paragraph_id IN ({q})", chunk):
            en_by_par.setdefault(r["paragraph_id"], []).append((r["start_char"], r["end_char"]))
    nq_by_par: dict[int, list[tuple[int, int]]] = {}
    for chunk_start in range(0, len(par_ids), 500):
        chunk = par_ids[chunk_start:chunk_start + 500]
        q = ",".join("?" for _ in chunk)
        for r in db_or_tx.query(f"SELECT paragraph_id, start_char, end_char FROM no_quote_ranges "
                                f"WHERE paragraph_id IN ({q})", chunk):
            nq_by_par.setdefault(r["paragraph_id"], []).append((r["start_char"], r["end_char"]))
    for s in segs:
        s["codes"] = codes_by_seg.get(s["id"], [])
        s["interactions"] = inter_by_seg.get(s["id"], [])
        s["quote_candidate"] = bool(s["quote_candidate"])
        s["segment_ref"] = segment_ref(s["interview_id"], s["id"])
        s["en_original"] = en_status(s["start_char"], s["end_char"], en_by_par.get(s["paragraph_id"], []))
        s["quotation_block"] = en_status(s["start_char"], s["end_char"], nq_by_par.get(s["paragraph_id"], []))
    return segs


def segment_ref(interview_id: str, segment_id: int) -> str:
    """Stable segment reference: '<interview_id>-S<segment id>'."""
    return f"{interview_id}-S{segment_id}"


def quotation_block_status(tx, paragraph_id: int, start: int, end: int) -> str:
    """'none' | 'partial' | 'full' overlap of [start, end) with the paragraph's [NO-QUOTE] ranges."""
    ranges = [(r["start_char"], r["end_char"]) for r in tx.query(
        "SELECT start_char, end_char FROM no_quote_ranges WHERE paragraph_id=?", (paragraph_id,))]
    return en_status(start, end, ranges)


def en_status(start: int, end: int, ranges: list[tuple[int, int]]) -> str:
    """'full' if the segment lies inside an [EN-ORIGINAL] range, 'partial' if it overlaps one, else 'none'."""
    if any(a <= start and end <= b for a, b in ranges):
        return "full"
    if any(start < b and end > a for a, b in ranges):
        return "partial"
    return "none"


EN_LABEL = {"full": "yes", "partial": "partly", "none": "no"}
NQ_LABEL = EN_LABEL  # quotation block: yes | partly | no


def get_interview(db: Database, interview_id: str, pass_id: int | None = None) -> dict[str, Any]:
    iv = db.one("SELECT * FROM interviews WHERE id=?", (interview_id,))
    if not iv:
        raise NotFound(f"Interview {interview_id} not found.")
    passes = db.query("SELECT * FROM coding_passes WHERE interview_id=? ORDER BY id", (interview_id,))
    if pass_id is None:
        pass_id = primary_pass_id(db, interview_id)
    if not any(p["id"] == pass_id for p in passes):
        raise ValidationError(f"Coding pass {pass_id} does not belong to interview {interview_id}.")
    paragraphs = db.query("SELECT * FROM paragraphs WHERE interview_id=? ORDER BY idx", (interview_id,))
    conf = db.query("""SELECT c.* FROM confidential_ranges c JOIN paragraphs p ON p.id = c.paragraph_id
                       WHERE p.interview_id=? ORDER BY c.paragraph_id, c.start_char""", (interview_id,))
    conf_by_par: dict[int, list] = {}
    for c in conf:
        conf_by_par.setdefault(c["paragraph_id"], []).append(c)
    en_by_par: dict[int, list] = {}
    for e in db.query("""SELECT e.* FROM en_original_ranges e JOIN paragraphs p ON p.id = e.paragraph_id
                         WHERE p.interview_id=? ORDER BY e.paragraph_id, e.start_char""", (interview_id,)):
        en_by_par.setdefault(e["paragraph_id"], []).append(e)
    nq_by_par: dict[int, list] = {}
    for e in db.query("""SELECT n.* FROM no_quote_ranges n JOIN paragraphs p ON p.id = n.paragraph_id
                         WHERE p.interview_id=? ORDER BY n.paragraph_id, n.start_char""", (interview_id,)):
        nq_by_par.setdefault(e["paragraph_id"], []).append(e)
    segs = _segment_rows(db, "s.coding_pass_id = ?", (pass_id,))
    seg_by_par: dict[int, list] = {}
    for s in segs:
        seg_by_par.setdefault(s["paragraph_id"], []).append(s)
    for p in paragraphs:
        p["confidential"] = conf_by_par.get(p["id"], [])
        p["en_original"] = en_by_par.get(p["id"], [])
        p["no_quote"] = nq_by_par.get(p["id"], [])
        p["segments"] = seg_by_par.get(p["id"], [])
    iv["pilot"] = bool(iv["pilot"])
    return {"interview": iv, "paragraphs": paragraphs, "passes": passes, "pass_id": pass_id,
            "segment_count": len(segs)}


def patch_interview(tx: Tx, interview_id: str, status: str | None, notes: str | None,
                    role_label: str | None = None, role_category: str | None = None) -> dict[str, Any]:
    """Edit interview details through the normal write path (one undo entry, no schema change).
    Only fields that are given and differ from the stored value are written."""
    from .importer import ALLOWED_ROLES
    iv = tx.one("SELECT * FROM interviews WHERE id=?", (interview_id,))
    if not iv:
        raise NotFound(f"Interview {interview_id} not found.")
    changes: dict[str, Any] = {}
    if status is not None:
        if status not in {"not-started", "in-progress", "done"}:
            raise ValidationError("status must be not-started, in-progress or done.")
        changes["status"] = status
    if notes is not None:
        changes["notes"] = notes
    if role_label is not None:
        role_label = role_label.strip()
        if not role_label:
            raise ValidationError("role_label must not be empty.")
        changes["role_label"] = role_label
    if role_category is not None:
        role_category = role_category.strip()
        if role_category != iv["role_category"] and role_category not in ALLOWED_ROLES:
            raise ValidationError(f"role_category {role_category!r} is not allowed. Allowed: {' | '.join(ALLOWED_ROLES)}.")
        changes["role_category"] = role_category
    changes = {k: v for k, v in changes.items() if v != iv[k]}
    tx.description = f"Edit interview {interview_id}"
    return tx.update("interviews", {"id": interview_id}, changes) or iv


def locate(db: Database, interview_id: str, text: str, paragraph_index: int | None = None,
           para_no: int | None = None, timestamp: str | None = None) -> list[dict[str, Any]]:
    if not text:
        raise ValidationError("text must not be empty.")
    sql = "SELECT id, idx, para_no, speaker, text, timestamp FROM paragraphs WHERE interview_id=? AND speaker='R'"
    params: list[Any] = [interview_id]
    if paragraph_index is not None:
        sql += " AND idx=?"
        params.append(paragraph_index)
    if para_no is not None:
        sql += " AND para_no=?"
        params.append(para_no)
    if timestamp:
        sql += " AND timestamp=?"
        params.append(timestamp.strip())
    out = []
    for p in db.query(sql + " ORDER BY idx", params):
        start = 0
        while True:
            pos = p["text"].find(text, start)
            if pos == -1:
                break
            out.append({"paragraph_id": p["id"], "paragraph_index": p["idx"], "para_no": p["para_no"],
                        "timestamp": p["timestamp"], "start_char": pos, "end_char": pos + len(text)})
            start = pos + 1
    return out


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def apply_anonymization(text: str, mapping: list[dict[str, str]]) -> tuple[str, int]:
    n = 0
    for m in mapping:
        term = m["term"]
        if not term:
            continue
        c = text.count(term)
        if c:
            text = text.replace(term, m["replacement"])
            n += c
    return text, n


def import_transcript(db: Database, parsed: ParsedTranscript, filename: str, mode: str = "new",
                      anonymize: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """mode: 'new' (refuse duplicates) or 'reimport' (keep existing codings, re-attach)."""
    meta = parsed.meta
    iid = meta["id"]
    existing = db.one("SELECT * FROM interviews WHERE id=?", (iid,))
    if existing and mode != "reimport":
        raise ValidationError(
            f"Interview {iid} already exists. Choose 're-import keeping existing codings' to replace the text.")
    mapping = db.query("SELECT term, replacement FROM anonymization_map ORDER BY LENGTH(term) DESC")
    anon_hits = 0
    texts: list[str] = []
    confs: list[list[tuple[int, int]]] = []
    ens: list[list[tuple[int, int]]] = []
    nqs: list[list[tuple[int, int]]] = []
    for p in parsed.paragraphs:
        t = p.text
        conf = list(p.confidential)
        en = list(p.en_original)
        nq = list(p.no_quote)
        if anonymize and mapping:
            t2, n = apply_anonymization(t, mapping)
            if n and conf:
                # re-derive confidential ranges after replacement by re-running on the marked text
                conf = _shift_ranges_after_replace(p.text, conf, mapping)
            if n and en:
                en = _shift_ranges_after_replace(p.text, en, mapping)
            if n and nq:
                nq = _shift_ranges_after_replace(p.text, nq, mapping)
            anon_hits += n
            t = t2
        texts.append(t)
        confs.append(conf)
        ens.append(en)
        nqs.append(nq)
    remaining = 0
    if mapping:
        for t in texts:
            for m in mapping:
                remaining += t.count(m["term"])

    report: dict[str, Any] = {
        "id": iid, "meta": meta, "paragraphs": len(parsed.paragraphs),
        "respondent_turns": sum(1 for p in parsed.paragraphs if p.speaker == "R"),
        "confidential_ranges": sum(len(c) for c in confs),
        "en_original_ranges": sum(len(e) for e in ens),
        "no_quote_ranges": sum(len(e) for e in nqs),
        "timestamps": parsed.stripped_timestamps,           # paragraphs with a kept [hh:mm:ss]
        "stripped_timestamps": parsed.stripped_timestamps,  # legacy name of the same count
        "warnings": list(parsed.warnings), "anonymization_replacements": anon_hits,
        "mapped_terms_remaining": remaining, "mode": "reimport" if existing else "new",
        "reattached": 0, "reattach_failed": [], "dry_run": dry_run,
    }
    if remaining:
        report["warnings"].append(
            f"{remaining} occurrence(s) of mapped anonymization terms remain in the text.")

    old_segments: list[dict] = []
    old_pars: dict[int, dict] = {}
    if existing:
        old_pars = {p["id"]: p for p in db.query("SELECT * FROM paragraphs WHERE interview_id=?", (iid,))}
        old_segments = _segment_rows(db, "p.interview_id = ?", (iid,))
        # plan re-attachment (by paragraph index + text match)
        by_idx = {p.idx: n for n, p in enumerate(parsed.paragraphs)}
        plan = []
        for s in old_segments:
            old_p = old_pars[s["paragraph_id"]]
            n = by_idx.get(old_p["idx"])
            target = None
            if n is not None and parsed.paragraphs[n].speaker == "R":
                pos = texts[n].find(s["text_snapshot"])
                if pos != -1:
                    target = (n, pos, pos + len(s["text_snapshot"]))
            if target is None:
                report["reattach_failed"].append({
                    "segment_id": s["id"], "para_no": old_p["para_no"], "text": s["text_snapshot"][:120],
                    "codes": [c["code_key"] for c in s["codes"]]})
            else:
                plan.append((s, target))
        report["reattached"] = len(plan)
        report["existing_segments"] = len(old_segments)
    if dry_run:
        return report

    with db.write("Import transcript", record_undo=False) as tx:
        if existing:
            tx.execute("UPDATE interviews SET country=?, role_category=?, role_label=?, date=?, duration_min=?, "
                       "language_original=?, interview_mode=?, pilot=?, notes=?, source_filename=?, imported_at=? "
                       "WHERE id=?",
                       (meta["country"], meta["role_category"], meta["role_label"], meta["date"],
                        meta["duration_min"], meta["language_original"], meta["interview_mode"], meta["pilot"],
                        meta["notes"] or existing["notes"], filename, now_iso(), iid))
            old_par_ids = list(old_pars.keys())
            q = ",".join("?" for _ in old_par_ids) or "NULL"
            tx.execute(f"UPDATE interactions SET segment_id=NULL WHERE segment_id IN "
                       f"(SELECT id FROM segments WHERE paragraph_id IN ({q}))", old_par_ids)
            tx.execute(f"DELETE FROM segment_codes WHERE segment_id IN "
                       f"(SELECT id FROM segments WHERE paragraph_id IN ({q}))", old_par_ids)
            tx.execute(f"DELETE FROM segments WHERE paragraph_id IN ({q})", old_par_ids)
            tx.execute(f"DELETE FROM confidential_ranges WHERE paragraph_id IN ({q})", old_par_ids)
            tx.execute(f"DELETE FROM en_original_ranges WHERE paragraph_id IN ({q})", old_par_ids)
            tx.execute(f"DELETE FROM no_quote_ranges WHERE paragraph_id IN ({q})", old_par_ids)
            tx.execute(f"DELETE FROM reliability_notes WHERE paragraph_id IN ({q})", old_par_ids)
            tx.execute(f"DELETE FROM paragraphs WHERE id IN ({q})", old_par_ids)
        else:
            tx.execute("INSERT INTO interviews(id, country, role_category, role_label, date, duration_min, "
                       "language_original, interview_mode, pilot, notes, source_filename, imported_at, status) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'not-started')",
                       (iid, meta["country"], meta["role_category"], meta["role_label"], meta["date"],
                        meta["duration_min"], meta["language_original"], meta["interview_mode"], meta["pilot"],
                        meta["notes"], filename, now_iso()))
            tx.execute("INSERT INTO coding_passes(interview_id, name, started_at, finished_at, locked) "
                       "VALUES (?,?,?,NULL,0)", (iid, "primary", now_iso()))
        new_par_ids: list[int] = []
        for n, p in enumerate(parsed.paragraphs):
            cur = tx.execute("INSERT INTO paragraphs(interview_id, idx, para_no, speaker, text, timestamp) "
                             "VALUES (?,?,?,?,?,?)", (iid, p.idx, p.para_no, p.speaker, texts[n], p.timestamp))
            pid = cur.lastrowid
            new_par_ids.append(pid)
            for (a, b) in confs[n]:
                tx.execute("INSERT INTO confidential_ranges(paragraph_id, start_char, end_char, source) "
                           "VALUES (?,?,?,'tag')", (pid, a, b))
            for (a, b) in ens[n]:
                tx.execute("INSERT INTO en_original_ranges(paragraph_id, start_char, end_char) VALUES (?,?,?)",
                           (pid, a, b))
            for (a, b) in nqs[n]:
                tx.execute("INSERT INTO no_quote_ranges(paragraph_id, start_char, end_char) VALUES (?,?,?)",
                           (pid, a, b))
        if existing:
            unflagged = 0
            for s, (n, a, b) in plan:
                pid = new_par_ids[n]
                quote = int(s["quote_candidate"])
                if quote and en_status(a, b, nqs[n]) != "none":
                    # the new file blocks this passage: keep the coding, drop the quote flag
                    quote = 0
                    unflagged += 1
                cur = tx.execute(
                    "INSERT INTO segments(paragraph_id, start_char, end_char, text_snapshot, comment, "
                    "quote_candidate, coding_pass_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (pid, a, b, texts[n][a:b], s["comment"], quote, s["coding_pass_id"],
                     s["created_at"], now_iso()))
                new_sid = cur.lastrowid
                for c in s["codes"]:
                    tx.execute("INSERT INTO segment_codes(segment_id, code_id, valence, agency) VALUES (?,?,?,?)",
                               (new_sid, c["code_id"], c["valence"], c["agency"]))
                for it in s["interactions"]:
                    tx.execute("UPDATE interactions SET segment_id=? WHERE id=?", (new_sid, it["id"]))
            if unflagged:
                report["warnings"].append(
                    f"{unflagged} re-attached quote candidate(s) now overlap a [NO-QUOTE] passage; "
                    f"the quote-candidate flag was removed.")
    db.clear_undo()
    return report


def _shift_ranges_after_replace(original: str, ranges: list[tuple[int, int]], mapping: list[dict]) -> list[tuple[int, int]]:
    """Re-compute confidential ranges when anonymization changes the text on import."""
    # Mark range boundaries with sentinel characters, replace, then find sentinels.
    S, E = "", ""
    pieces = []
    pos = 0
    for a, b in ranges:
        pieces.append(original[pos:a]); pieces.append(S); pieces.append(original[a:b]); pieces.append(E)
        pos = b
    pieces.append(original[pos:])
    marked = "".join(pieces)
    marked, _ = apply_anonymization(marked, mapping)
    out = []
    clean = []
    i = 0
    cur_start = None
    for ch in marked:
        if ch == S:
            cur_start = i
        elif ch == E:
            if cur_start is not None:
                out.append((cur_start, i))
            cur_start = None
        else:
            clean.append(ch)
            i += 1
    return out


# ---------------------------------------------------------------------------
# codes
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()
    return s[:24] or "CODE"


def code_frequencies(db_or_tx) -> dict[int, dict[str, int]]:
    rows = db_or_tx.query("""
        SELECT sc.code_id, i.country, COUNT(DISTINCT sc.segment_id) AS n
        FROM segment_codes sc JOIN segments s ON s.id = sc.segment_id
        JOIN coding_passes cp ON cp.id = s.coding_pass_id
        JOIN paragraphs p ON p.id = s.paragraph_id JOIN interviews i ON i.id = p.interview_id
        WHERE cp.name = 'primary' GROUP BY sc.code_id, i.country""")
    freq: dict[int, dict[str, int]] = {}
    for r in rows:
        f = freq.setdefault(r["code_id"], {"total": 0, "DE": 0, "CN": 0})
        f[r["country"]] = f.get(r["country"], 0) + r["n"]
        f["total"] += r["n"]
    return freq


def code_list(db_or_tx, include_retired: bool = False) -> list[dict[str, Any]]:
    rows = db_or_tx.query("SELECT * FROM codes ORDER BY sort_order, id")
    freq = code_frequencies(db_or_tx)
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["frequency"] = freq.get(r["id"], {"total": 0, "DE": 0, "CN": 0})
        r["active"] = bool(r["active"])
        # depth & path
        d, p, names = 0, r["parent_id"], [r["name"]]
        while p:
            d += 1
            par = by_id.get(p)
            if not par:
                break
            names.append(par["name"])
            p = par["parent_id"]
        r["depth"] = d
        r["path"] = " / ".join(reversed(names))
    if not include_retired:
        rows = [r for r in rows if r["active"]]
    return rows


def code_tree(db_or_tx, include_retired: bool = False) -> list[dict[str, Any]]:
    rows = code_list(db_or_tx, include_retired)
    by_id = {r["id"]: dict(r, children=[]) for r in rows}
    roots = []
    for r in by_id.values():
        parent = by_id.get(r["parent_id"]) if r["parent_id"] else None
        if parent:
            parent["children"].append(r)
        else:
            roots.append(r)
    # aggregate frequency including sub-codes
    def agg(node: dict) -> dict[str, int]:
        tot = dict(node["frequency"])
        for c in node["children"]:
            sub = agg(c)
            for k in tot:
                tot[k] += sub[k]
        node["frequency_with_children"] = tot
        return tot
    for r in roots:
        agg(r)
    return roots


def get_code(db_or_tx, code_id: int) -> dict[str, Any]:
    row = db_or_tx.one("SELECT * FROM codes WHERE id=?", (code_id,))
    if not row:
        raise NotFound(f"Code {code_id} not found.")
    return row


def _depth(tx: Tx, parent_id: int | None) -> int:
    d = 0
    while parent_id:
        d += 1
        row = tx.one("SELECT parent_id FROM codes WHERE id=?", (parent_id,))
        if not row:
            raise ValidationError(f"Parent code {parent_id} does not exist.")
        parent_id = row["parent_id"]
    return d


def _log(tx: Tx, code_id: int | None, action: str, field: str | None, old: Any, new: Any, reason: str) -> None:
    tx.insert("code_changelog", {
        "code_id": code_id, "timestamp": now_iso(), "action": action, "field": field,
        "old_value": None if old is None else str(old), "new_value": None if new is None else str(new),
        "reason": reason or "", "phase": phase(tx)})


def create_code(tx: Tx, name: str, definition: str, coding_rule: str, parent_id: int | None = None,
                code_key: str | None = None, ctype: str | None = None, anchor_example: str = "",
                color: str | None = None, reason: str = "") -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValidationError("name is required.")
    if not (definition or "").strip() or not (coding_rule or "").strip():
        raise ValidationError("definition and coding_rule are required for every new code.")
    if parent_id is not None:
        parent = get_code(tx, parent_id)
        if not parent["active"]:
            raise ValidationError("Cannot create a sub-code under a retired code.")
        if _depth(tx, parent_id) + 1 >= MAX_DEPTH:
            raise ValidationError(f"Hierarchy is limited to {MAX_DEPTH} levels.")
        ctype = ctype or ("inductive" if parent["type"] in ("inductive",) else parent["type"])
        color = color or parent["color"]
    ctype = ctype or "inductive"
    if ctype not in {"function", "structural", "context", "crosscutting", "learning", "role", "inductive"}:
        raise ValidationError(f"Invalid code type {ctype!r}.")
    key = (code_key or "").strip() or _slug(name)
    base, n = key, 1
    while tx.one("SELECT 1 FROM codes WHERE code_key=?", (key,)):
        n += 1
        key = f"{base}-{n}"
    max_order = tx.one("SELECT COALESCE(MAX(sort_order), -1) AS m FROM codes WHERE parent_id IS ?",
                       (parent_id,))["m"]
    row = tx.insert("codes", {
        "code_key": key, "name": name, "parent_id": parent_id, "type": ctype,
        "definition": definition.strip(), "coding_rule": coding_rule.strip(),
        "anchor_example": (anchor_example or "").strip(), "color": color or "#ffbb78",
        "sort_order": max_order + 1, "active": 1, "created_at": now_iso(), "retired_at": None})
    _log(tx, row["id"], "create", None, None, f"{key}: {name}", reason or "created during coding")
    tx.description = f"Create code {name}"
    return row


def patch_code(tx: Tx, code_id: int, changes: dict[str, Any], reason: str = "") -> dict[str, Any]:
    code = get_code(tx, code_id)
    logged_fields = {"name", "definition", "coding_rule", "anchor_example", "parent_id"}
    touched_logged = [k for k in changes if k in logged_fields and changes[k] != code.get(k)]
    if touched_logged and not (reason or "").strip():
        raise ValidationError("A one-line reason is required for renaming, editing or re-parenting a code.")
    upd: dict[str, Any] = {}
    for k in ("name", "definition", "coding_rule", "anchor_example", "color"):
        if k in changes and changes[k] is not None:
            v = str(changes[k]).strip() if k != "color" else str(changes[k])
            if k == "name" and not v:
                raise ValidationError("name must not be empty.")
            if v != code[k]:
                upd[k] = v
    if "parent_id" in changes and changes["parent_id"] != code["parent_id"]:
        new_parent = changes["parent_id"]
        if new_parent is not None:
            if new_parent == code_id:
                raise ValidationError("A code cannot be its own parent.")
            # cycle check
            p = new_parent
            while p:
                if p == code_id:
                    raise ValidationError("Cannot move a code under one of its own sub-codes.")
                p = (tx.one("SELECT parent_id FROM codes WHERE id=?", (p,)) or {}).get("parent_id")
            # depth check incl. subtree height
            def height(cid: int) -> int:
                kids = tx.query("SELECT id FROM codes WHERE parent_id=?", (cid,))
                return 1 + max([height(k["id"]) for k in kids], default=0)
            if _depth(tx, new_parent) + 1 + height(code_id) > MAX_DEPTH:
                raise ValidationError(f"Hierarchy is limited to {MAX_DEPTH} levels.")
        upd["parent_id"] = new_parent
    if "position" in changes and changes["position"] is not None:
        parent_id = upd.get("parent_id", code["parent_id"])
        siblings = [r["id"] for r in tx.query(
            "SELECT id FROM codes WHERE parent_id IS ? AND id != ? ORDER BY sort_order, id", (parent_id, code_id))]
        pos = max(0, min(int(changes["position"]), len(siblings)))
        siblings.insert(pos, code_id)
        for i, sid in enumerate(siblings):
            if sid == code_id:
                upd["sort_order"] = i
            else:
                tx.update("codes", {"id": sid}, {"sort_order": i})
    elif "sort_order" in changes and changes["sort_order"] is not None:
        upd["sort_order"] = int(changes["sort_order"])
    for k in touched_logged:
        action = "rename" if k == "name" else "reparent" if k == "parent_id" else "edit"
        _log(tx, code_id, action, k, code.get(k), changes[k], reason)
    row = tx.update("codes", {"id": code_id}, upd) or code
    tx.description = f"Edit code {code['name']}"
    return row


def code_segment_count(tx: Tx, code_id: int) -> int:
    return tx.one("SELECT COUNT(*) AS n FROM segment_codes WHERE code_id=?", (code_id,))["n"]


def _move_codings(tx: Tx, from_id: int, to_id: int) -> int:
    rows = tx.query("SELECT * FROM segment_codes WHERE code_id=?", (from_id,))
    moved = 0
    for r in rows:
        exists = tx.one("SELECT 1 FROM segment_codes WHERE segment_id=? AND code_id=?", (r["segment_id"], to_id))
        tx.delete("segment_codes", {"segment_id": r["segment_id"], "code_id": from_id})
        if not exists:
            tx.insert("segment_codes", {"segment_id": r["segment_id"], "code_id": to_id,
                                        "valence": r["valence"], "agency": r["agency"]})
        moved += 1
    for it in tx.query("SELECT id, from_code_id, to_code_id FROM interactions WHERE from_code_id=? OR to_code_id=?",
                       (from_id, from_id)):
        tx.update("interactions", {"id": it["id"]}, {
            "from_code_id": to_id if it["from_code_id"] == from_id else it["from_code_id"],
            "to_code_id": to_id if it["to_code_id"] == from_id else it["to_code_id"]})
    return moved


def retire_code(tx: Tx, code_id: int, reason: str, reassign: str = "keep") -> dict[str, Any]:
    code = get_code(tx, code_id)
    if not code["active"]:
        raise ValidationError("Code is already retired.")
    if not (reason or "").strip():
        raise ValidationError("A one-line reason is required to retire a code.")
    if tx.one("SELECT 1 FROM codes WHERE parent_id=? AND active=1", (code_id,)):
        raise ValidationError("Retire or move the active sub-codes first.")
    if reassign not in {"keep", "parent"}:
        raise ValidationError("reassign must be 'keep' or 'parent'.")
    moved = 0
    if reassign == "parent":
        if not code["parent_id"]:
            raise ValidationError("Code has no parent to reassign codings to.")
        moved = _move_codings(tx, code_id, code["parent_id"])
    row = tx.update("codes", {"id": code_id}, {"active": 0, "retired_at": now_iso()})
    _log(tx, code_id, "retire", "active", "1", "0",
         f"{reason} (codings {'reassigned to parent' if reassign == 'parent' else 'kept on retired code'}: {moved if reassign == 'parent' else code_segment_count(tx, code_id)})")
    tx.description = f"Retire code {code['name']}"
    return {"code": row, "moved": moved}


def merge_codes(tx: Tx, source_id: int, target_id: int, reason: str) -> dict[str, Any]:
    if source_id == target_id:
        raise ValidationError("Source and target must differ.")
    src, dst = get_code(tx, source_id), get_code(tx, target_id)
    if not src["active"] or not dst["active"]:
        raise ValidationError("Both codes must be active.")
    if not (reason or "").strip():
        raise ValidationError("A one-line reason is required to merge codes.")
    if tx.one("SELECT 1 FROM codes WHERE parent_id=? AND active=1", (source_id,)):
        raise ValidationError("Move or retire the sub-codes of the source code first.")
    moved = _move_codings(tx, source_id, target_id)
    # memos: concatenate
    src_memos = tx.query("SELECT * FROM memos WHERE scope='code' AND ref_id=?", (str(source_id),))
    if src_memos:
        text = "\n\n".join(m["text"] for m in src_memos if m["text"].strip())
        if text:
            dst_memo = tx.one("SELECT * FROM memos WHERE scope='code' AND ref_id=? ORDER BY id LIMIT 1",
                              (str(target_id),))
            merged = f"[merged from {src['name']} on {now_iso()[:10]}]\n{text}"
            if dst_memo:
                new_text = (dst_memo["text"].rstrip() + "\n\n" + merged) if dst_memo["text"].strip() else merged
                tx.update("memos", {"id": dst_memo["id"]}, {"text": new_text, "updated_at": now_iso()})
                tx.insert("memo_versions", {"memo_id": dst_memo["id"], "text": new_text, "saved_at": now_iso()})
            else:
                m = tx.insert("memos", {"scope": "code", "ref_id": str(target_id), "text": merged,
                                        "created_at": now_iso(), "updated_at": now_iso()})
                tx.insert("memo_versions", {"memo_id": m["id"], "text": merged, "saved_at": now_iso()})
    tx.update("codes", {"id": source_id}, {"active": 0, "retired_at": now_iso()})
    _log(tx, source_id, "merge", "merged_into", src["code_key"], dst["code_key"],
         f"{reason} ({moved} coding(s) moved to {dst['name']})")
    _log(tx, target_id, "merge", "merged_from", src["code_key"], dst["code_key"], reason)
    tx.description = f"Merge {src['name']} into {dst['name']}"
    return {"moved": moved, "source": get_code(tx, source_id), "target": get_code(tx, target_id)}


def descendant_ids(db_or_tx, code_id: int) -> list[int]:
    out = [code_id]
    stack = [code_id]
    while stack:
        cid = stack.pop()
        for r in db_or_tx.query("SELECT id FROM codes WHERE parent_id=?", (cid,)):
            out.append(r["id"])
            stack.append(r["id"])
    return out


def code_segments(db: Database, code_id: int, include_sub: bool = False) -> list[dict[str, Any]]:
    ids = descendant_ids(db, code_id) if include_sub else [code_id]
    q = ",".join("?" for _ in ids)
    return _segment_rows(db, f"s.id IN (SELECT segment_id FROM segment_codes WHERE code_id IN ({q})) "
                             "AND s.coding_pass_id IN (SELECT id FROM coding_passes WHERE name='primary')", ids)


def changelog(db: Database, code_id: int | None = None) -> list[dict[str, Any]]:
    sql = """SELECT cl.*, c.code_key, c.name AS code_name FROM code_changelog cl
             LEFT JOIN codes c ON c.id = cl.code_id"""
    params: list = []
    if code_id is not None:
        sql += " WHERE cl.code_id=?"
        params.append(code_id)
    return db.query(sql + " ORDER BY cl.id DESC", params)


# ---------------------------------------------------------------------------
# segments
# ---------------------------------------------------------------------------

def _paragraph(tx, paragraph_id: int) -> dict[str, Any]:
    p = tx.one("SELECT * FROM paragraphs WHERE id=?", (paragraph_id,))
    if not p:
        raise NotFound(f"Paragraph {paragraph_id} not found.")
    return p


def validate_range(tx, paragraph: dict[str, Any], start: int, end: int, ignore_conf: bool = False) -> None:
    if paragraph["speaker"] != "R":
        raise ValidationError("Only respondent (R:) paragraphs can be coded.")
    n = len(paragraph["text"])
    if not (isinstance(start, int) and isinstance(end, int)):
        raise ValidationError("start_char and end_char must be integers (Unicode code points).")
    if start < 0 or end > n or start >= end:
        raise ValidationError(f"Offsets out of bounds: need 0 <= start < end <= {n}, got {start}..{end}.")
    if not ignore_conf:
        for c in tx.query("SELECT start_char, end_char FROM confidential_ranges WHERE paragraph_id=?",
                          (paragraph["id"],)):
            if start < c["end_char"] and end > c["start_char"]:
                raise ValidationError("Selection overlaps a confidential range; confidential text cannot be coded.")


def _check_pass(tx, pass_id: int, interview_id: str) -> dict[str, Any]:
    cp = tx.one("SELECT * FROM coding_passes WHERE id=?", (pass_id,))
    if not cp:
        raise ValidationError(f"Coding pass {pass_id} does not exist.")
    if cp["interview_id"] != interview_id:
        raise ValidationError("Coding pass does not belong to this interview.")
    if cp["locked"]:
        raise ValidationError("This coding pass is finished and locked.")
    return cp


def _check_code_assignment(tx, code_id: int, valence: Any, agency: Any) -> tuple[int, str | None]:
    code = tx.one("SELECT id, active FROM codes WHERE id=?", (code_id,))
    if not code:
        raise ValidationError(f"Code {code_id} does not exist.")
    if not code["active"]:
        raise ValidationError(f"Code {code_id} is retired and cannot be assigned.")
    v = 0 if valence is None else valence
    if v not in VALENCES:
        raise ValidationError("valence must be -1, 0 or 1.")
    a = agency if agency not in ("", None) else None
    if a is not None and a not in AGENCIES:
        raise ValidationError(f"agency must be one of {sorted(AGENCIES)} or unset.")
    return int(v), a


def _touch_interview(tx: Tx, interview_id: str) -> None:
    iv = tx.one("SELECT status FROM interviews WHERE id=?", (interview_id,))
    if iv and iv["status"] == "not-started":
        tx.update("interviews", {"id": interview_id}, {"status": "in-progress"})


def _set_last_used(tx: Tx, code_id: int) -> None:
    tx.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('last_used_code_id', ?)", (str(code_id),))


def get_segment(db_or_tx, segment_id: int) -> dict[str, Any]:
    rows = _segment_rows(db_or_tx, "s.id = ?", (segment_id,))
    if not rows:
        raise NotFound(f"Segment {segment_id} not found.")
    return rows[0]


def create_segment(tx: Tx, paragraph_id: int, start_char: int, end_char: int, codes: list[dict[str, Any]],
                   comment: str = "", quote_candidate: bool = False, coding_pass_id: int | None = None) -> dict[str, Any]:
    p = _paragraph(tx, paragraph_id)
    validate_range(tx, p, start_char, end_char)
    if coding_pass_id is None:
        coding_pass_id = primary_pass_id(tx, p["interview_id"])
    _check_pass(tx, coding_pass_id, p["interview_id"])
    if not codes:
        raise ValidationError("At least one code is required.")
    seen: set[int] = set()
    checked = []
    for c in codes:
        cid = int(c["code_id"])
        if cid in seen:
            raise ValidationError(f"Code {cid} listed twice.")
        seen.add(cid)
        v, a = _check_code_assignment(tx, cid, c.get("valence"), c.get("agency"))
        checked.append((cid, v, a))
    if quote_candidate and quotation_block_status(tx, paragraph_id, start_char, end_char) != "none":
        raise ValidationError(QUOTE_BLOCK_MESSAGE)
    row = tx.insert("segments", {
        "paragraph_id": paragraph_id, "start_char": start_char, "end_char": end_char,
        "text_snapshot": p["text"][start_char:end_char], "comment": comment or "",
        "quote_candidate": 1 if quote_candidate else 0, "coding_pass_id": coding_pass_id,
        "created_at": now_iso(), "updated_at": now_iso()})
    for cid, v, a in checked:
        tx.insert("segment_codes", {"segment_id": row["id"], "code_id": cid, "valence": v, "agency": a})
    _set_last_used(tx, checked[-1][0])
    _touch_interview(tx, p["interview_id"])
    tx.description = f"Code segment {p['interview_id']} ¶{p['para_no']}"
    return get_segment(tx, row["id"])


def patch_segment(tx: Tx, segment_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    seg = tx.one("SELECT * FROM segments WHERE id=?", (segment_id,))
    if not seg:
        raise NotFound(f"Segment {segment_id} not found.")
    p = _paragraph(tx, seg["paragraph_id"])
    _check_pass(tx, seg["coding_pass_id"], p["interview_id"])
    upd: dict[str, Any] = {}
    start = changes.get("start_char", seg["start_char"])
    end = changes.get("end_char", seg["end_char"])
    if start != seg["start_char"] or end != seg["end_char"]:
        validate_range(tx, p, start, end)
        upd.update({"start_char": start, "end_char": end, "text_snapshot": p["text"][start:end]})
    if "comment" in changes and changes["comment"] is not None:
        upd["comment"] = str(changes["comment"])
    if "quote_candidate" in changes and changes["quote_candidate"] is not None:
        upd["quote_candidate"] = 1 if changes["quote_candidate"] else 0
    # quotation block: neither flag a blocked segment nor move a flagged one into a blocked range
    final_quote = upd.get("quote_candidate", seg["quote_candidate"])
    if final_quote and ("quote_candidate" in upd or "start_char" in upd) \
            and quotation_block_status(tx, seg["paragraph_id"], start, end) != "none":
        raise ValidationError(QUOTE_BLOCK_MESSAGE)
    if upd:
        upd["updated_at"] = now_iso()
        tx.update("segments", {"id": segment_id}, upd)
    tx.description = f"Edit segment {p['interview_id']} ¶{p['para_no']}"
    return get_segment(tx, segment_id)


def delete_segment(tx: Tx, segment_id: int) -> None:
    seg = tx.one("SELECT * FROM segments WHERE id=?", (segment_id,))
    if not seg:
        raise NotFound(f"Segment {segment_id} not found.")
    p = _paragraph(tx, seg["paragraph_id"])
    _check_pass(tx, seg["coding_pass_id"], p["interview_id"])
    for it in tx.query("SELECT id FROM interactions WHERE segment_id=?", (segment_id,)):
        tx.update("interactions", {"id": it["id"]}, {"segment_id": None})
    for sc in tx.query("SELECT code_id FROM segment_codes WHERE segment_id=?", (segment_id,)):
        tx.delete("segment_codes", {"segment_id": segment_id, "code_id": sc["code_id"]})
    for m in tx.query("SELECT id FROM memos WHERE scope='segment' AND ref_id=?", (str(segment_id),)):
        for v in tx.query("SELECT id FROM memo_versions WHERE memo_id=?", (m["id"],)):
            tx.delete("memo_versions", {"id": v["id"]})
        tx.delete("memos", {"id": m["id"]})
    tx.delete("segments", {"id": segment_id})
    tx.description = f"Delete segment {p['interview_id']} ¶{p['para_no']}"


def add_segment_code(tx: Tx, segment_id: int, code_id: int, valence: Any = 0, agency: Any = None) -> dict[str, Any]:
    seg = tx.one("SELECT * FROM segments WHERE id=?", (segment_id,))
    if not seg:
        raise NotFound(f"Segment {segment_id} not found.")
    p = _paragraph(tx, seg["paragraph_id"])
    _check_pass(tx, seg["coding_pass_id"], p["interview_id"])
    v, a = _check_code_assignment(tx, code_id, valence, agency)
    if tx.one("SELECT 1 FROM segment_codes WHERE segment_id=? AND code_id=?", (segment_id, code_id)):
        raise ValidationError("This code is already assigned to the segment.")
    tx.insert("segment_codes", {"segment_id": segment_id, "code_id": code_id, "valence": v, "agency": a})
    tx.update("segments", {"id": segment_id}, {"updated_at": now_iso()})
    _set_last_used(tx, code_id)
    tx.description = f"Add code to segment {p['interview_id']} ¶{p['para_no']}"
    return get_segment(tx, segment_id)


def patch_segment_code(tx: Tx, segment_id: int, code_id: int, valence: Any = None, agency: Any = None,
                       agency_set: bool = False) -> dict[str, Any]:
    seg = tx.one("SELECT * FROM segments WHERE id=?", (segment_id,))
    if not seg:
        raise NotFound(f"Segment {segment_id} not found.")
    p = _paragraph(tx, seg["paragraph_id"])
    _check_pass(tx, seg["coding_pass_id"], p["interview_id"])
    cur = tx.one("SELECT * FROM segment_codes WHERE segment_id=? AND code_id=?", (segment_id, code_id))
    if not cur:
        raise NotFound("This code is not assigned to the segment.")
    upd: dict[str, Any] = {}
    if valence is not None:
        if valence not in VALENCES:
            raise ValidationError("valence must be -1, 0 or 1.")
        upd["valence"] = int(valence)
    if agency_set:
        a = agency if agency not in ("", None) else None
        if a is not None and a not in AGENCIES:
            raise ValidationError(f"agency must be one of {sorted(AGENCIES)} or unset.")
        upd["agency"] = a
    if upd:
        tx.update("segment_codes", {"segment_id": segment_id, "code_id": code_id}, upd)
        tx.update("segments", {"id": segment_id}, {"updated_at": now_iso()})
    tx.description = f"Edit coding {p['interview_id']} ¶{p['para_no']}"
    return get_segment(tx, segment_id)


def remove_segment_code(tx: Tx, segment_id: int, code_id: int) -> dict[str, Any]:
    seg = tx.one("SELECT * FROM segments WHERE id=?", (segment_id,))
    if not seg:
        raise NotFound(f"Segment {segment_id} not found.")
    p = _paragraph(tx, seg["paragraph_id"])
    _check_pass(tx, seg["coding_pass_id"], p["interview_id"])
    if not tx.one("SELECT 1 FROM segment_codes WHERE segment_id=? AND code_id=?", (segment_id, code_id)):
        raise NotFound("This code is not assigned to the segment.")
    n = tx.one("SELECT COUNT(*) AS n FROM segment_codes WHERE segment_id=?", (segment_id,))["n"]
    if n <= 1:
        raise ValidationError("A segment needs at least one code; delete the segment instead.")
    tx.delete("segment_codes", {"segment_id": segment_id, "code_id": code_id})
    tx.update("segments", {"id": segment_id}, {"updated_at": now_iso()})
    tx.description = f"Remove code from segment {p['interview_id']} ¶{p['para_no']}"
    return get_segment(tx, segment_id)


def batch(tx: Tx, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for i, op in enumerate(operations):
        kind = op.get("op")
        try:
            if kind == "create_segment":
                r = create_segment(tx, op["paragraph_id"], op["start_char"], op["end_char"], op.get("codes") or [],
                                   op.get("comment", ""), bool(op.get("quote_candidate")), op.get("coding_pass_id"))
            elif kind == "patch_segment":
                r = patch_segment(tx, op["segment_id"], op)
            elif kind == "delete_segment":
                delete_segment(tx, op["segment_id"]); r = {"deleted": op["segment_id"]}
            elif kind == "add_code":
                r = add_segment_code(tx, op["segment_id"], op["code_id"], op.get("valence", 0), op.get("agency"))
            elif kind == "patch_code":
                r = patch_segment_code(tx, op["segment_id"], op["code_id"], op.get("valence"), op.get("agency"),
                                       "agency" in op)
            elif kind == "remove_code":
                r = remove_segment_code(tx, op["segment_id"], op["code_id"])
            else:
                raise ValidationError(f"Unknown op {kind!r}.")
        except (ValidationError, NotFound, KeyError) as exc:
            msg = f"missing field {exc}" if isinstance(exc, KeyError) else str(exc)
            raise ValidationError(f"Operation {i} ({kind}): {msg}")
        results.append(r)
    tx.description = f"Batch of {len(operations)} coding operation(s)"
    return results


# ---------------------------------------------------------------------------
# interactions
# ---------------------------------------------------------------------------

def list_interactions(db: Database, interview_id: str | None = None, country: str | None = None) -> list[dict]:
    sql = """SELECT it.*, a.code_key AS from_key, a.name AS from_name, b.code_key AS to_key, b.name AS to_name,
                    s.text_snapshot AS segment_text, p.para_no, p.timestamp
             FROM interactions it JOIN codes a ON a.id = it.from_code_id JOIN codes b ON b.id = it.to_code_id
             LEFT JOIN segments s ON s.id = it.segment_id LEFT JOIN paragraphs p ON p.id = s.paragraph_id
             WHERE 1=1"""
    params: list = []
    if interview_id:
        sql += " AND it.interview_id=?"; params.append(interview_id)
    if country:
        sql += " AND it.country=?"; params.append(country)
    return db.query(sql + " ORDER BY it.id", params)


def _interaction_payload(tx: Tx, data: dict[str, Any], existing: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = dict(existing or {})
    for k in ("segment_id", "interview_id", "from_code_id", "to_code_id", "mechanism", "country",
              "description", "condition"):
        if k in data and data[k] is not None or (k in data and existing is None):
            out[k] = data[k]
    if out.get("segment_id") is not None:
        seg = tx.one("SELECT s.id, p.interview_id, i.country FROM segments s JOIN paragraphs p ON p.id=s.paragraph_id "
                     "JOIN interviews i ON i.id=p.interview_id WHERE s.id=?", (out["segment_id"],))
        if not seg:
            raise ValidationError(f"Segment {out['segment_id']} does not exist.")
        out.setdefault("interview_id", seg["interview_id"])
        out["interview_id"] = out.get("interview_id") or seg["interview_id"]
        out["country"] = out.get("country") or seg["country"]
    if out.get("interview_id"):
        iv = tx.one("SELECT country FROM interviews WHERE id=?", (out["interview_id"],))
        if not iv:
            raise ValidationError(f"Interview {out['interview_id']} does not exist.")
        out["country"] = out.get("country") or iv["country"]
    for k in ("from_code_id", "to_code_id"):
        if not out.get(k):
            raise ValidationError(f"{k} is required.")
        if not tx.one("SELECT 1 FROM codes WHERE id=?", (out[k],)):
            raise ValidationError(f"Code {out[k]} does not exist.")
    if out.get("mechanism") not in MECHANISMS:
        raise ValidationError("mechanism must be 'inducing' or 'blocking'.")
    if out.get("country") not in ("DE", "CN"):
        raise ValidationError("country must be DE or CN (derived from the segment/interview if omitted).")
    out["description"] = out.get("description") or ""
    out["condition"] = out.get("condition") or ""
    return out


def create_interaction(tx: Tx, data: dict[str, Any]) -> dict[str, Any]:
    payload = _interaction_payload(tx, data)
    row = tx.insert("interactions", {
        "segment_id": payload.get("segment_id"), "interview_id": payload.get("interview_id"),
        "from_code_id": payload["from_code_id"], "to_code_id": payload["to_code_id"],
        "mechanism": payload["mechanism"], "country": payload["country"],
        "description": payload["description"], "condition": payload["condition"], "created_at": now_iso()})
    tx.description = "Create interaction"
    return row


def patch_interaction(tx: Tx, interaction_id: int, data: dict[str, Any]) -> dict[str, Any]:
    cur = tx.one("SELECT * FROM interactions WHERE id=?", (interaction_id,))
    if not cur:
        raise NotFound(f"Interaction {interaction_id} not found.")
    payload = _interaction_payload(tx, data, cur)
    upd = {k: payload[k] for k in ("segment_id", "interview_id", "from_code_id", "to_code_id", "mechanism",
                                   "country", "description", "condition") if payload.get(k) != cur.get(k)}
    tx.description = "Edit interaction"
    return tx.update("interactions", {"id": interaction_id}, upd) or cur


def delete_interaction(tx: Tx, interaction_id: int) -> None:
    if not tx.one("SELECT 1 FROM interactions WHERE id=?", (interaction_id,)):
        raise NotFound(f"Interaction {interaction_id} not found.")
    tx.delete("interactions", {"id": interaction_id})
    tx.description = "Delete interaction"


# ---------------------------------------------------------------------------
# confidential ranges
# ---------------------------------------------------------------------------

def create_confidential(tx: Tx, paragraph_id: int, start_char: int, end_char: int,
                        delete_overlapping: bool = False) -> dict[str, Any]:
    p = _paragraph(tx, paragraph_id)
    n = len(p["text"])
    if start_char < 0 or end_char > n or start_char >= end_char:
        raise ValidationError(f"Offsets out of bounds: need 0 <= start < end <= {n}.")
    overlapping = tx.query("SELECT id FROM segments WHERE paragraph_id=? AND start_char < ? AND end_char > ?",
                           (paragraph_id, end_char, start_char))
    if overlapping and not delete_overlapping:
        raise ValidationError(
            f"{len(overlapping)} coded segment(s) overlap this range. Confirm deletion of those segments "
            f"(delete_overlapping=true) or shrink the range.")
    for s in overlapping:
        delete_segment(tx, s["id"])
    row = tx.insert("confidential_ranges", {"paragraph_id": paragraph_id, "start_char": start_char,
                                            "end_char": end_char, "source": "manual"})
    tx.description = f"Mark confidential {p['interview_id']} ¶{p['para_no']}"
    return row


def delete_confidential(tx: Tx, range_id: int) -> None:
    if not tx.one("SELECT 1 FROM confidential_ranges WHERE id=?", (range_id,)):
        raise NotFound(f"Confidential range {range_id} not found.")
    tx.delete("confidential_ranges", {"id": range_id})
    tx.description = "Remove confidential marking"


# ---------------------------------------------------------------------------
# memos
# ---------------------------------------------------------------------------

def list_memos(db: Database, scope: str | None = None, ref_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM memos WHERE 1=1"
    params: list = []
    if scope:
        sql += " AND scope=?"; params.append(scope)
    if ref_id is not None:
        sql += " AND ref_id=?"; params.append(str(ref_id))
    rows = db.query(sql + " ORDER BY updated_at DESC, id DESC", params)
    for r in rows:
        r["versions"] = db.scalar("SELECT COUNT(*) FROM memo_versions WHERE memo_id=?", (r["id"],))
    return rows


def create_memo(tx: Tx, scope: str, ref_id: str | None, text: str) -> dict[str, Any]:
    if scope not in MEMO_SCOPES:
        raise ValidationError(f"scope must be one of {sorted(MEMO_SCOPES)}.")
    if scope != "general" and not ref_id:
        raise ValidationError("ref_id is required for code/interview/segment memos.")
    row = tx.insert("memos", {"scope": scope, "ref_id": None if scope == "general" else str(ref_id),
                              "text": text or "", "created_at": now_iso(), "updated_at": now_iso()})
    tx.insert("memo_versions", {"memo_id": row["id"], "text": text or "", "saved_at": now_iso()})
    tx.description = "Create memo"
    return row


def update_memo(tx: Tx, memo_id: int, text: str) -> dict[str, Any]:
    cur = tx.one("SELECT * FROM memos WHERE id=?", (memo_id,))
    if not cur:
        raise NotFound(f"Memo {memo_id} not found.")
    if text == cur["text"]:
        return cur
    row = tx.update("memos", {"id": memo_id}, {"text": text, "updated_at": now_iso()})
    tx.insert("memo_versions", {"memo_id": memo_id, "text": text, "saved_at": now_iso()})
    tx.description = "Edit memo"
    return row


def memo_versions(db: Database, memo_id: int) -> list[dict]:
    if not db.one("SELECT 1 FROM memos WHERE id=?", (memo_id,)):
        raise NotFound(f"Memo {memo_id} not found.")
    return db.query("SELECT * FROM memo_versions WHERE memo_id=? ORDER BY id DESC", (memo_id,))


# ---------------------------------------------------------------------------
# coding passes
# ---------------------------------------------------------------------------

def start_recode_pass(tx: Tx, interview_id: str) -> dict[str, Any]:
    if not tx.one("SELECT 1 FROM interviews WHERE id=?", (interview_id,)):
        raise NotFound(f"Interview {interview_id} not found.")
    open_pass = tx.one("SELECT * FROM coding_passes WHERE interview_id=? AND name!='primary' AND locked=0",
                       (interview_id,))
    if open_pass:
        raise ValidationError(f"An unfinished re-coding pass ({open_pass['name']}) already exists; finish it first.")
    name = f"recode-{_dt.date.today().isoformat()}"
    n = 1
    base = name
    while tx.one("SELECT 1 FROM coding_passes WHERE interview_id=? AND name=?", (interview_id, name)):
        n += 1
        name = f"{base}-{n}"
    row = tx.insert("coding_passes", {"interview_id": interview_id, "name": name, "started_at": now_iso(),
                                      "finished_at": None, "locked": 0})
    tx.description = f"Start re-coding pass {interview_id}"
    return row


def finish_pass(tx: Tx, pass_id: int) -> dict[str, Any]:
    cp = tx.one("SELECT * FROM coding_passes WHERE id=?", (pass_id,))
    if not cp:
        raise NotFound(f"Coding pass {pass_id} not found.")
    if cp["name"] == "primary":
        raise ValidationError("The primary pass cannot be finished/locked; mark the interview as done instead.")
    if cp["locked"]:
        return cp
    tx.description = f"Finish re-coding pass {cp['interview_id']}"
    return tx.update("coding_passes", {"id": pass_id}, {"locked": 1, "finished_at": now_iso()}) or cp


def save_reliability_note(tx: Tx, pass_id: int, paragraph_id: int, note: str) -> dict[str, Any]:
    if not tx.one("SELECT 1 FROM coding_passes WHERE id=?", (pass_id,)):
        raise NotFound(f"Coding pass {pass_id} not found.")
    cur = tx.one("SELECT * FROM reliability_notes WHERE pass_id=? AND paragraph_id=?", (pass_id, paragraph_id))
    tx.description = "Edit reliability resolution note"
    if cur:
        return tx.update("reliability_notes", {"id": cur["id"]}, {"note": note}) or cur
    return tx.insert("reliability_notes", {"pass_id": pass_id, "paragraph_id": paragraph_id, "note": note})
