"""Search, retrieval, analysis views and intra-coder reliability.
All functions are read-only and only look at the *primary* coding pass unless
stated otherwise.  Confidential text never leaves the database through here."""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Any

from ..db import Database
from .core import AGENCY_ORDER, ValidationError, NotFound, _segment_rows, descendant_ids, code_list

PRIMARY = "s.coding_pass_id IN (SELECT id FROM coding_passes WHERE name='primary')"
# matrix columns: the agency value list plus 'unset' for codings without attribution
AGENCY_VALUES = AGENCY_ORDER + ["unset"]


def mask_confidential(text: str, ranges: list[dict[str, Any]]) -> str:
    """Replace confidential ranges of a paragraph text with a placeholder."""
    if not ranges:
        return text
    out, pos = [], 0
    for r in sorted(ranges, key=lambda x: x["start_char"]):
        a, b = r["start_char"], r["end_char"]
        if a < pos:
            a = pos
        out.append(text[pos:a])
        out.append("[CONFIDENTIAL]")
        pos = max(pos, b)
    out.append(text[pos:])
    return "".join(out)


def confidential_by_paragraph(db: Database) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list] = defaultdict(list)
    for r in db.query("SELECT * FROM confidential_ranges"):
        out[r["paragraph_id"]].append(r)
    return out


# ---------------------------------------------------------------------------
# filters shared by retrieval / analysis
# ---------------------------------------------------------------------------

def _filter_sql(country: str | None = None, role_category: str | None = None, pilot: bool | None = None,
                interview_id: str | None = None) -> tuple[str, list]:
    sql, params = "", []
    if country:
        sql += " AND i.country = ?"; params.append(country)
    if role_category:
        sql += " AND i.role_category = ?"; params.append(role_category)
    if pilot is not None:
        sql += " AND i.pilot = ?"; params.append(1 if pilot else 0)
    if interview_id:
        sql += " AND i.id = ?"; params.append(interview_id)
    return sql, params


def retrieve(db: Database, code_id: int | None = None, include_sub: bool = False, country: str | None = None,
             role_category: str | None = None, pilot: bool | None = None, valence: int | None = None,
             agency: str | None = None, quote_candidate: bool | None = None, interview_id: str | None = None,
             q: str | None = None) -> list[dict[str, Any]]:
    where = PRIMARY
    fs, params = _filter_sql(country, role_category, pilot, interview_id)
    where += fs
    if code_id is not None:
        ids = descendant_ids(db, code_id) if include_sub else [code_id]
        marks = ",".join("?" for _ in ids)
        where += f" AND s.id IN (SELECT segment_id FROM segment_codes WHERE code_id IN ({marks})"
        params.extend(ids)
        if valence is not None:
            where += " AND valence = ?"; params.append(int(valence))
        if agency:
            if agency == "unset":
                where += " AND agency IS NULL"
            else:
                where += " AND agency = ?"; params.append(agency)
        where += ")"
    else:
        if valence is not None:
            where += " AND s.id IN (SELECT segment_id FROM segment_codes WHERE valence = ?)"; params.append(int(valence))
        if agency:
            if agency == "unset":
                where += " AND s.id IN (SELECT segment_id FROM segment_codes WHERE agency IS NULL)"
            else:
                where += " AND s.id IN (SELECT segment_id FROM segment_codes WHERE agency = ?)"; params.append(agency)
    if quote_candidate is not None:
        where += " AND s.quote_candidate = ?"; params.append(1 if quote_candidate else 0)
    if q:
        where += " AND (s.text_snapshot LIKE ? OR s.comment LIKE ?)"; params.extend([f"%{q}%", f"%{q}%"])
    rows = _segment_rows(db, where, params)
    if quote_candidate:
        # quotation-blocked passages never appear in a quote list, even for legacy flags
        rows = [s for s in rows if s["quotation_block"] == "none"]
    return rows


def search(db: Database, q: str, country: str | None = None, speaker: str | None = None,
           limit: int = 500) -> list[dict[str, Any]]:
    q = (q or "").strip()
    if len(q) < 2:
        raise ValidationError("Search text must have at least 2 characters.")
    conf = confidential_by_paragraph(db)
    sql = """SELECT p.*, i.country, i.role_label FROM paragraphs p JOIN interviews i ON i.id = p.interview_id
             WHERE p.text LIKE ? """
    params: list[Any] = [f"%{q}%"]
    if country:
        sql += " AND i.country = ?"; params.append(country)
    if speaker in ("I", "R"):
        sql += " AND p.speaker = ?"; params.append(speaker)
    sql += " ORDER BY i.id, p.idx"
    hits = []
    ql = q.lower()
    for p in db.query(sql, params):
        tl = p["text"].lower()
        start = 0
        ranges = conf.get(p["id"], [])
        while True:
            pos = tl.find(ql, start)
            if pos == -1:
                break
            end = pos + len(q)
            start = pos + 1
            if any(pos < r["end_char"] and end > r["start_char"] for r in ranges):
                continue
            a, b = max(0, pos - 60), min(len(p["text"]), end + 60)
            window_ranges = [{"start_char": max(0, r["start_char"] - a), "end_char": min(b - a, r["end_char"] - a)}
                             for r in ranges if r["end_char"] > a and r["start_char"] < b]
            snippet = mask_confidential(p["text"][a:b], window_ranges)
            hits.append({"interview_id": p["interview_id"], "paragraph_id": p["id"], "para_no": p["para_no"],
                         "timestamp": p["timestamp"],
                         "paragraph_index": p["idx"], "speaker": p["speaker"], "country": p["country"],
                         "role_label": p["role_label"], "start_char": pos, "end_char": end,
                         "snippet": ("…" if a > 0 else "") + snippet + ("…" if b < len(p["text"]) else "")})
            if len(hits) >= limit:
                return hits
    return hits


# ---------------------------------------------------------------------------
# analysis views
# ---------------------------------------------------------------------------

def _codes_in_order(db: Database, include_retired: bool = False) -> list[dict[str, Any]]:
    """Codes flattened in tree order (depth-first)."""
    rows = code_list(db, include_retired=True)
    by_parent: dict[Any, list] = defaultdict(list)
    for r in rows:
        by_parent[r["parent_id"]].append(r)
    out: list[dict] = []

    def walk(pid, depth):
        for r in sorted(by_parent.get(pid, []), key=lambda x: (x["sort_order"], x["id"])):
            r["depth"] = depth
            out.append(r)
            walk(r["id"], depth + 1)
    walk(None, 0)
    if not include_retired:
        out = [r for r in out if r["active"]]
    return out


def _all_segments(db: Database, **filters) -> list[dict[str, Any]]:
    fs, params = _filter_sql(**filters)
    return _segment_rows(db, PRIMARY + fs, params)


def matrix(db: Database, country: str | None = None, role_category: str | None = None,
           pilot: bool | None = None, with_children: bool = False) -> dict[str, Any]:
    codes = _codes_in_order(db)
    segs = _all_segments(db, country=country, role_category=role_category, pilot=pilot)
    ivs = db.query("SELECT id, country, role_category, role_label, pilot FROM interviews ORDER BY country, id")
    if country:
        ivs = [i for i in ivs if i["country"] == country]
    if role_category:
        ivs = [i for i in ivs if i["role_category"] == role_category]
    if pilot is not None:
        ivs = [i for i in ivs if bool(i["pilot"]) == pilot]
    cells: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for s in segs:
        for c in s["codes"]:
            cells[c["code_id"]][s["interview_id"]] += 1
    if with_children:
        parent_of = {c["id"]: c["parent_id"] for c in codes}
        agg: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for cid, row in cells.items():
            p = cid
            while p:
                for iid, n in row.items():
                    agg[p][iid] += n
                p = parent_of.get(p)
        cells = agg
    return {
        "codes": [{"id": c["id"], "code_key": c["code_key"], "name": c["name"], "depth": c["depth"],
                   "type": c["type"], "color": c["color"]} for c in codes],
        "interviews": ivs,
        "cells": {str(k): dict(v) for k, v in cells.items()},
        "totals": {str(k): sum(v.values()) for k, v in cells.items()},
    }


def crosstab(db: Database, attribute: str = "country", pilot: bool | None = None) -> dict[str, Any]:
    if attribute not in ("country", "role_category"):
        raise ValidationError("attribute must be 'country' or 'role_category'.")
    codes = _codes_in_order(db)
    segs = _all_segments(db, pilot=pilot)
    values = sorted({s[attribute] for s in segs} | ({"DE", "CN"} if attribute == "country" else set()))
    if attribute == "country":
        values = ["DE", "CN"]
    table: dict[int, dict[str, dict[str, Any]]] = {}
    for c in codes:
        table[c["id"]] = {v: {"segments": 0, "interviews": set(), "valence": {"-1": 0, "0": 0, "1": 0},
                              "segment_ids": []} for v in values}
    for s in segs:
        v = s[attribute]
        for c in s["codes"]:
            cell = table.get(c["code_id"], {}).get(v)
            if cell is None:
                continue
            cell["segments"] += 1
            cell["interviews"].add(s["interview_id"])
            cell["valence"][str(c["valence"])] += 1
            cell["segment_ids"].append(s["id"])
    rows = []
    for c in codes:
        r = {"code_id": c["id"], "code_key": c["code_key"], "name": c["name"], "depth": c["depth"],
             "type": c["type"], "cells": {}}
        for v in values:
            cell = table[c["id"]][v]
            r["cells"][v] = {"segments": cell["segments"], "interviews": len(cell["interviews"]),
                             "valence": cell["valence"], "segment_ids": cell["segment_ids"]}
        rows.append(r)
    return {"attribute": attribute, "values": values, "rows": rows}


def cooccurrence(db: Database, country: str | None = None) -> dict[str, Any]:
    segs = _all_segments(db, country=country)
    by_par: dict[int, list] = defaultdict(list)
    for s in segs:
        by_par[s["paragraph_id"]].append(s)
    counts: dict[tuple[int, int], int] = defaultdict(int)
    diag: dict[int, int] = defaultdict(int)
    for s in segs:
        for c in s["codes"]:
            diag[c["code_id"]] += 1
    for par_segs in by_par.values():
        for s in par_segs:
            for a, b in combinations(sorted({c["code_id"] for c in s["codes"]}), 2):
                counts[(a, b)] += 1
        for s1, s2 in combinations(par_segs, 2):
            if s1["start_char"] < s2["end_char"] and s2["start_char"] < s1["end_char"]:
                pairs = set()
                for c1 in s1["codes"]:
                    for c2 in s2["codes"]:
                        if c1["code_id"] != c2["code_id"]:
                            pairs.add(tuple(sorted((c1["code_id"], c2["code_id"]))))
                for pr in pairs:
                    counts[pr] += 1
    used = set(diag.keys())
    codes = [c for c in _codes_in_order(db, include_retired=True) if c["id"] in used]
    return {
        "codes": [{"id": c["id"], "code_key": c["code_key"], "name": c["name"], "color": c["color"]} for c in codes],
        "cells": {f"{a}:{b}": n for (a, b), n in counts.items()},
        "diagonal": {str(k): v for k, v in diag.items()},
    }


def interactions_view(db: Database) -> dict[str, Any]:
    from .core import list_interactions
    rows = list_interactions(db)
    funcs = [c for c in _codes_in_order(db, include_retired=True) if c["type"] == "function"]
    fkeys = [c["code_key"] for c in funcs]
    names = {c["code_key"]: c["name"] for c in funcs}
    mat: dict[str, dict[str, dict[str, dict[str, int]]]] = {
        cc: {a: {b: {"inducing": 0, "blocking": 0} for b in fkeys} for a in fkeys} for cc in ("DE", "CN")}
    for r in rows:
        cc = r["country"]
        if cc in mat and r["from_key"] in names and r["to_key"] in names:
            mat[cc][r["from_key"]][r["to_key"]][r["mechanism"]] += 1
    # mermaid
    lines = ["flowchart LR"]
    for cc in ("DE", "CN"):
        lines.append(f"  subgraph {cc}")
        for k in fkeys:
            lines.append(f"    {cc}_{k}[\"{names[k]}\"]")
        for a in fkeys:
            for b in fkeys:
                cell = mat[cc][a][b]
                if cell["inducing"]:
                    lines.append(f"    {cc}_{a} -->|inducing ({cell['inducing']})| {cc}_{b}")
                if cell["blocking"]:
                    lines.append(f"    {cc}_{a} -.->|blocking ({cell['blocking']})| {cc}_{b}")
        lines.append("  end")
    return {"interactions": rows, "functions": [{"code_key": k, "name": names[k]} for k in fkeys],
            "matrix": mat, "mermaid": "\n".join(lines)}


def agency_matrix(db: Database) -> dict[str, Any]:
    funcs = [c for c in _codes_in_order(db) if c["type"] == "function"]
    segs = _all_segments(db)
    out = {c["id"]: {cc: {a: 0 for a in AGENCY_VALUES} for cc in ("DE", "CN")} for c in funcs}
    fids = set(out.keys())
    for s in segs:
        for c in s["codes"]:
            if c["code_id"] in fids:
                out[c["code_id"]][s["country"]][c["agency"] or "unset"] += 1
    return {"agencies": AGENCY_VALUES,
            "rows": [{"code_id": c["id"], "code_key": c["code_key"], "name": c["name"], "cells": out[c["id"]]}
                     for c in funcs]}


def _function_codes_of(segment: dict, par_segs: list[dict]) -> set[str]:
    keys = {c["name"] for c in segment["codes"] if c["type"] == "function"}
    for o in par_segs:
        if o["id"] != segment["id"] and o["start_char"] < segment["end_char"] and segment["start_char"] < o["end_char"]:
            keys |= {c["name"] for c in o["codes"] if c["type"] == "function"}
    return keys


def barriers_view(db: Database) -> dict[str, Any]:
    segs = _all_segments(db)
    by_par: dict[int, list] = defaultdict(list)
    for s in segs:
        by_par[s["paragraph_id"]].append(s)
    groups: dict[str, dict[str, list]] = {"DE": defaultdict(list), "CN": defaultdict(list)}
    for s in segs:
        if not any(c["code_key"] == "BAR" for c in s["codes"]):
            continue
        funcs = _function_codes_of(s, by_par[s["paragraph_id"]]) or {"(no function co-coded)"}
        for f in sorted(funcs):
            groups[s["country"]][f].append(s)
    order = [c["name"] for c in _codes_in_order(db) if c["type"] == "function"] + ["(no function co-coded)"]
    return {"functions": order, "groups": {cc: {f: groups[cc][f] for f in order if groups[cc].get(f)}
                                            for cc in ("DE", "CN")}}


def learning_view(db: Database) -> dict[str, Any]:
    ml = db.one("SELECT id FROM codes WHERE code_key='ML'")
    segs = retrieve(db, code_id=ml["id"], include_sub=True) if ml else []
    return {"DE": [s for s in segs if s["country"] == "DE"], "CN": [s for s in segs if s["country"] == "CN"]}


def crosscase(db: Database, code_id: int, include_sub: bool = True) -> dict[str, Any]:
    code = db.one("SELECT * FROM codes WHERE id=?", (code_id,))
    if not code:
        raise NotFound(f"Code {code_id} not found.")
    segs = retrieve(db, code_id=code_id, include_sub=include_sub)
    out = {"code": code, "DE": [], "CN": [], "summary": {}}
    for cc in ("DE", "CN"):
        sub = [s for s in segs if s["country"] == cc]
        out[cc] = sub
        val = {"-1": 0, "0": 0, "1": 0}
        for s in sub:
            for c in s["codes"]:
                if c["code_id"] == code_id or (include_sub and c["code_id"] in descendant_ids(db, code_id)):
                    val[str(c["valence"])] += 1
        out["summary"][cc] = {"segments": len(sub), "interviews": len({s["interview_id"] for s in sub}),
                              "valence": val}
    return out


# ---------------------------------------------------------------------------
# intra-coder reliability
# ---------------------------------------------------------------------------

def kappa(a: int, b: int, c: int, d: int) -> float | None:
    """Cohen's kappa for a 2x2 present/absent table: a=both present, b=only pass 1,
    c=only pass 2, d=both absent."""
    n = a + b + c + d
    if n == 0:
        return None
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return round((po - pe) / (1 - pe), 4)


def reliability_report(db: Database, interview_id: str, pass_id: int | None = None) -> dict[str, Any]:
    iv = db.one("SELECT * FROM interviews WHERE id=?", (interview_id,))
    if not iv:
        raise NotFound(f"Interview {interview_id} not found.")
    p1 = db.one("SELECT * FROM coding_passes WHERE interview_id=? AND name='primary'", (interview_id,))
    if pass_id is None:
        p2 = db.one("SELECT * FROM coding_passes WHERE interview_id=? AND name!='primary' ORDER BY id DESC LIMIT 1",
                    (interview_id,))
    else:
        p2 = db.one("SELECT * FROM coding_passes WHERE id=? AND interview_id=?", (pass_id, interview_id))
    if not p2:
        raise ValidationError("No re-coding pass exists for this interview yet.")
    pars = db.query("SELECT * FROM paragraphs WHERE interview_id=? AND speaker='R' ORDER BY idx", (interview_id,))
    conf = confidential_by_paragraph(db)
    s1 = _segment_rows(db, "s.coding_pass_id = ?", (p1["id"],))
    s2 = _segment_rows(db, "s.coding_pass_id = ?", (p2["id"],))
    sets1: dict[int, set[int]] = defaultdict(set)
    sets2: dict[int, set[int]] = defaultdict(set)
    for s in s1:
        sets1[s["paragraph_id"]] |= {c["code_id"] for c in s["codes"]}
    for s in s2:
        sets2[s["paragraph_id"]] |= {c["code_id"] for c in s["codes"]}
    codes = {c["id"]: c for c in code_list(db, include_retired=True)}
    notes = {r["paragraph_id"]: r["note"] for r in db.query("SELECT * FROM reliability_notes WHERE pass_id=?",
                                                              (p2["id"],))}
    n = len(pars)
    agree = 0
    per_code: dict[int, dict[str, int]] = defaultdict(lambda: {"a": 0, "b": 0, "c": 0, "d": 0})
    all_codes = set()
    for p in pars:
        all_codes |= sets1[p["id"]] | sets2[p["id"]]
    disagreements = []
    pooled = {"a": 0, "b": 0, "c": 0, "d": 0}
    for p in pars:
        A, B = sets1[p["id"]], sets2[p["id"]]
        if A == B:
            agree += 1
        else:
            disagreements.append({
                "paragraph_id": p["id"], "para_no": p["para_no"],
                "text": mask_confidential(p["text"], conf.get(p["id"], [])),
                "pass1": [codes[c]["name"] for c in sorted(A, key=lambda x: codes[x]["sort_order"])],
                "pass2": [codes[c]["name"] for c in sorted(B, key=lambda x: codes[x]["sort_order"])],
                "only_pass1": [codes[c]["name"] for c in sorted(A - B)],
                "only_pass2": [codes[c]["name"] for c in sorted(B - A)],
                "note": notes.get(p["id"], ""),
            })
        for cid in all_codes:
            k = "a" if (cid in A and cid in B) else "b" if cid in A else "c" if cid in B else "d"
            per_code[cid][k] += 1
            pooled[k] += 1
    per_code_rows = []
    for cid in sorted(all_codes, key=lambda x: (codes[x]["sort_order"], x)):
        t = per_code[cid]
        per_code_rows.append({
            "code_id": cid, "code_key": codes[cid]["code_key"], "name": codes[cid]["name"],
            "both": t["a"], "only_pass1": t["b"], "only_pass2": t["c"], "neither": t["d"],
            "percent_agreement": round(100 * (t["a"] + t["d"]) / n, 1) if n else None,
            "kappa": kappa(t["a"], t["b"], t["c"], t["d"]),
        })
    return {
        "interview": iv, "pass1": p1, "pass2": p2, "paragraphs": n, "agreeing_paragraphs": agree,
        "percent_agreement": round(100 * agree / n, 1) if n else None,
        "pooled_kappa": kappa(pooled["a"], pooled["b"], pooled["c"], pooled["d"]),
        "per_code": per_code_rows, "disagreements": disagreements,
        "segments_pass1": len(s1), "segments_pass2": len(s2),
    }
