"""Readable interview reports: one self-contained HTML page per interview plus an overview page.

Each report shows the whole transcript from top to bottom, the coded passages highlighted in the
category colour, and next to every respondent answer a column of boxes with the codings of that
answer (segment ID, category in full with key, valence, agency, interactions with description and
condition). The interview memo follows the transcript. Comments, segment and code memos and
quote-candidate flags are deliberately left out. Confidential text is never included. Only the
primary coding pass is shown; re-coding passes are ignored.

The pages need no internet connection: no external fonts, scripts or stylesheets. They print
cleanly ("Print, Save as PDF" in the browser).
"""
from __future__ import annotations

import datetime as _dt
import html
import re
from pathlib import Path
from typing import Any, Callable

from ..db import Database
from .anonymization import check_text, list_mapping
from .core import NotFound, code_list, get_interview, list_memos

COUNTRY_NAME = {"DE": "Germany", "CN": "China"}
AGENCY_MEANING = {
    "GOV-CENTRAL": "central government, legislator",
    "GOV-PROV": "province, federal state, municipality",
    "REGULATOR": "regulator",
    "GRID": "grid company, TSO, DSO",
    "SOE": "state-owned enterprise other than grid",
    "MARKET": "private market actor",
    "ASSOC": "association",
    "RESEARCH": "research",
    "MULTI": "several actor types jointly",
}
CONFIDENTIAL_PLACEHOLDER = "[confidential passage removed]"

# Labels derived from the coding notes. The notes themselves are never shown; only these fixed markers
# are read from them, so the report needs no change to the database.
#   "(single-site)"          -> label "single-site" on the segment box
#   "proposal EMT-<NAME>"    -> theme "EMT-<NAME>", exactly as written in the note, shown next to the
#                               Emergent Themes category (only on EMT-coded segments)
SINGLE_SITE_MARKER = "(single-site)"
_EMT_PROPOSAL = re.compile(r"proposal\s+(EMT-[A-Za-z0-9-]+)", re.IGNORECASE)


def _is_single_site(s: dict[str, Any]) -> bool:
    return SINGLE_SITE_MARKER in (s.get("comment") or "")


def _emt_theme(s: dict[str, Any]) -> str | None:
    """Theme of an EMT-coded segment: the name after the first 'proposal' in its note, exactly as written
    (e.g. 'EMT-TEST'). No renaming, no grouping."""
    if not any(c["code_key"] == "EMT" for c in s.get("codes", [])):
        return None
    m = _EMT_PROPOSAL.search(s.get("comment") or "")
    return m.group(1) if m else None
# Category order of the codebook. The tool's own sort_order counts sub-categories within their
# parent, so sorting by it would mix sub-categories with the top level. Unknown keys (e.g. EMT sub-categories
# created later) follow at the end.
CATEGORY_ORDER = ["F1", "F1b", "F2", "F3", "F4", "F5", "F6", "F7",
                  "STR", "STR-A", "STR-N", "STR-I",
                  "CTX", "CTX-TIS", "CTX-SEC", "CTX-GEO", "CTX-POL",
                  "BAR", "INT", "ML", "ROLE", "EMT"]
_RANK = {k: i for i, k in enumerate(CATEGORY_ORDER)}


def _order(code: dict[str, Any]) -> tuple:
    """Sort key: position in CATEGORY_ORDER; categories not listed there after it, by key."""
    key = code.get("code_key", "")
    return (_RANK.get(key, len(CATEGORY_ORDER)), key)

CSS = """
:root { --text:#1f2933; --muted:#6b7280; --line:#e5e7eb; --border:#d9dde3; }
* { box-sizing:border-box; }
body { margin:0; background:#f3f4f6; color:var(--text);
  font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans","Noto Sans CJK SC",sans-serif; }
.page { max-width:1180px; margin:0 auto; padding:28px 32px 56px; background:#fff; min-height:100vh; }
a { color:#2563eb; text-decoration:none; } a:hover { text-decoration:underline; }
.back { font-size:13px; }
h1 { font-size:24px; margin:6px 0 2px; }
h2 { font-size:17px; margin:28px 0 8px; }
.sub { color:var(--muted); font-size:13px; margin:0 0 14px; }
.source { font-size:13px; margin:2px 0 8px; padding:6px 10px; border-left:3px solid #2563eb; background:#f5f8ff; }
dl.meta { display:grid; grid-template-columns:max-content 1fr; gap:3px 18px; margin:10px 0 18px; font-size:14px; }
dl.meta dt { color:var(--muted); } dl.meta dd { margin:0; }
.legend { border:1px solid var(--border); border-radius:8px; padding:12px 16px; margin:0 0 22px; font-size:13px; background:#fcfcfd; }
.legend h3 { font-size:13px; margin:0 0 6px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
.legend .cats { display:flex; flex-wrap:wrap; gap:6px 18px; margin-bottom:10px; }
.legend p { margin:3px 0; }
.sw { display:inline-block; width:12px; height:12px; border-radius:3px; vertical-align:-1px; margin-right:6px; }
.turn { display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:28px; padding:12px 0; border-top:1px solid var(--line); }
.turn .when { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; margin-bottom:2px; }
.turn.i .text { color:var(--muted); font-size:14px; }
.turn.i .who { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:#9ca3af; margin-right:6px; }
.text { overflow-wrap:anywhere; }
.hl { border-radius:3px; padding:1px 1px; -webkit-box-decoration-break:clone; box-decoration-break:clone; }
.hl.ov { text-decoration:underline; text-decoration-thickness:1px; text-underline-offset:3px; }
.mk { display:inline-block; min-width:1.45em; padding:0 .35em; margin:0 3px 0 2px; border:1.5px solid; border-radius:10px;
  font-size:11px; line-height:1.4; text-align:center; font-weight:700; vertical-align:1px; background:#fff; color:#111827; }
.conf { color:var(--muted); font-style:italic; }
.codes { display:flex; flex-direction:column; gap:8px; }
.card { border:1px solid var(--border); border-left:5px solid; border-radius:6px; padding:7px 10px; font-size:13px;
  line-height:1.45; background:#fcfcfd; break-inside:avoid; page-break-inside:avoid; }
.card .head { display:flex; gap:7px; align-items:center; margin-bottom:4px; font-size:12px; color:var(--muted); }
.card .head .mk { margin:0; }
.card .ref { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; color:#374151; }
.code { margin:2px 0; }
.code .key { color:var(--muted); }
.code .val { font-weight:700; margin-left:6px; font-variant-numeric:tabular-nums; }
.code .ag { color:var(--muted); font-size:12px; margin-left:6px; }
.code .theme { display:block; margin:1px 0 0 18px; font-size:12px; color:#7c4a03; }
.lbl { display:inline-block; border:1px solid #cbd5e1; border-radius:10px; padding:0 7px; font-size:11px; line-height:1.5;
  color:#334155; background:#f1f5f9; }
.jump { font-size:13px; margin:-6px 0 14px; }
.memo { border:1px solid var(--border); border-radius:8px; padding:12px 18px; background:#fcfcfd; max-width:820px; }
.memo p { margin:7px 0; }
.memo b { font-weight:600; }
.inter { margin-top:5px; padding-top:5px; border-top:1px dashed var(--border); }
.inter .det { color:var(--muted); font-size:12px; margin-top:1px; }
table { border-collapse:collapse; font-size:13px; margin:6px 0 10px; }
th, td { border:1px solid var(--border); padding:4px 8px; text-align:left; vertical-align:top; }
th { background:#f3f4f6; font-weight:600; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; }
td.zero { color:#c0c4cc; }
.nowrap { white-space:nowrap; }
.scroll { overflow-x:auto; }
.keys { font-size:12px; color:var(--muted); }
@media (max-width:900px) { .turn { grid-template-columns:1fr; gap:8px; } .page { padding:18px 16px 40px; } }
@media print {
  @page { size:A4; margin:14mm 12mm; }
  * { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  body { background:#fff; font-size:10pt; }
  .page { max-width:none; padding:0; min-height:0; }
  .back, .jump { display:none; }
  .memo { break-inside:avoid; }
  .turn { grid-template-columns:minmax(0,1fr) 64mm; gap:6mm; padding:6pt 0; }
  .turn.i .text { font-size:9.5pt; }
  .card { font-size:8.5pt; padding:4pt 6pt; }
  .card .head, .code .ag, .inter .det { font-size:8pt; }
  a { color:inherit; }
  h2 { break-after:avoid; }
}
"""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _e(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _rgba(hex_color: str | None, alpha: float) -> str:
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return f"rgba(140,140,140,{alpha})"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return f"rgba(140,140,140,{alpha})"
    return f"rgba({r},{g},{b},{alpha})"


DASH = "–"


def _date(d: str | None) -> str:
    """'2026-01-15' -> '15 Jan 2026' (no platform-specific strftime flags)."""
    try:
        dt = _dt.datetime.strptime(str(d), "%Y-%m-%d")
    except ValueError:
        return str(d or "")
    return f"{dt.day} {dt.strftime('%b %Y')}"


def _minutes(m: Any) -> str:
    return _e(m) if m else DASH


def _valence(v: int | None) -> str:
    return {1: "+1", 0: "0", -1: "−1"}.get(v, "" if v is None else str(v))


def _page(title: str, body: str) -> str:
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{_e(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<div class=\"page\">\n"
            f"{body}\n</div>\n</body>\n</html>\n")


def _interview_ids(db: Database) -> list[str]:
    rows = db.query("SELECT id, country FROM interviews")
    return [r["id"] for r in sorted(rows, key=lambda r: (0 if r["country"] == "DE" else 1, r["id"]))]


def _codes_by_id(db: Database) -> dict[int, dict[str, Any]]:
    return {c["id"]: c for c in code_list(db, include_retired=True)}


def _code_label(code: dict[str, Any]) -> str:
    return f"{_e(code['name'])} <span class=\"key\">({_e(code['code_key'])})</span>"


# ---------------------------------------------------------------------------
# transcript rendering
# ---------------------------------------------------------------------------

def _render_text(text: str, segs: list[dict[str, Any]], numbers: dict[int, int],
                 confidential: list[dict[str, Any]]) -> str:
    """Paragraph text with highlighted coded passages, numbered markers at each segment start and
    confidential ranges replaced by a placeholder (their text is never emitted)."""
    n = len(text)
    conf = [(c["start_char"], c["end_char"]) for c in confidential]
    bounds = {0, n}
    for s in segs:
        bounds.update((s["start_char"], s["end_char"]))
    for a, b in conf:
        bounds.update((a, b))
    bs = sorted(x for x in bounds if 0 <= x <= n)
    starts: dict[int, list[dict[str, Any]]] = {}
    for s in sorted(segs, key=lambda s: (s["start_char"], -s["end_char"], s["id"])):
        starts.setdefault(s["start_char"], []).append(s)
    out: list[str] = []
    for a, b in zip(bs, bs[1:]):
        in_conf = next(((ca, cb) for ca, cb in conf if ca <= a and cb >= b), None)
        if in_conf:
            if a == in_conf[0]:
                out.append(f"<span class=\"conf\">{CONFIDENTIAL_PLACEHOLDER}</span>")
            continue
        for s in starts.get(a, []):
            color = s["codes"][0]["color"] if s["codes"] else None
            out.append(f"<span class=\"mk\" style=\"border-color:{_e(color or '#888888')}\">{numbers[s['id']]}</span>")
        piece = _e(text[a:b])
        covering = [s for s in segs if s["start_char"] <= a and s["end_char"] >= b]
        if not covering:
            out.append(piece)
            continue
        inner = min(covering, key=lambda s: (s["end_char"] - s["start_char"], s["id"]))
        color = inner["codes"][0]["color"] if inner["codes"] else None
        alpha = 0.22 if len(covering) == 1 else 0.36
        cls = "hl ov" if len(covering) > 1 else "hl"
        out.append(f"<span class=\"{cls}\" style=\"background:{_rgba(color, alpha)}\">{piece}</span>")
    return "".join(out)


def _render_card(s: dict[str, Any], number: int, codes: dict[int, dict[str, Any]]) -> str:
    color = s["codes"][0]["color"] if s["codes"] else "#888888"
    single = " <span class=\"lbl\" title=\"label from the coding notes, not a category\">single-site</span>" \
        if _is_single_site(s) else ""
    theme = _emt_theme(s)
    parts = [f"<div class=\"card\" style=\"border-left-color:{_e(color)}\">",
             f"<div class=\"head\"><span class=\"mk\" style=\"border-color:{_e(color)}\">{number}</span>"
             f"<span class=\"ref\">{_e(s['segment_ref'])}</span>{single}</div>"]
    for c in s["codes"]:
        val = "" if c["code_key"] == "INT" else f"<span class=\"val\">{_valence(c['valence'])}</span>"
        ag = f"<span class=\"ag\">agency {_e(c['agency'])}</span>" if c.get("agency") else ""
        th = f"<span class=\"theme\">Theme: {_e(theme)}</span>" if theme and c["code_key"] == "EMT" else ""
        parts.append(f"<div class=\"code\"><span class=\"sw\" style=\"background:{_e(c['color'])}\"></span>"
                     f"{_code_label(c)}{val}{ag}{th}</div>")
    for it in s.get("interactions") or []:
        fr = codes.get(it["from_code_id"], {"name": "?", "code_key": "?"})
        to = codes.get(it["to_code_id"], {"name": "?", "code_key": "?"})
        det = []
        if (it.get("description") or "").strip():
            det.append(f"<div class=\"det\">Description: {_e(it['description'])}</div>")
        if (it.get("condition") or "").strip():
            det.append(f"<div class=\"det\">Condition: {_e(it['condition'])}</div>")
        parts.append(f"<div class=\"inter\">Interaction: {_e(fr['name'])} → {_e(to['name'])} "
                     f"<span class=\"key\">({_e(it['mechanism'])})</span>{''.join(det)}</div>")
    parts.append("</div>")
    return "".join(parts)


def render_interview(db: Database, interview_id: str, index_href: str | None = "index.html") -> str:
    """HTML report of one interview (primary coding pass)."""
    data = get_interview(db, interview_id)  # primary pass by default; raises NotFound
    iv = data["interview"]
    for p in data["paragraphs"]:  # categories inside a segment in CATEGORY_ORDER (first one sets the colour)
        for s in p.get("segments", []):
            s["codes"] = sorted(s["codes"], key=_order)
    codes = _codes_by_id(db)
    segs_all = [s for p in data["paragraphs"] for s in p.get("segments", [])]

    # categories used in this interview, with segment counts, in codebook order
    used: dict[int, int] = {}
    agencies: set[str] = set()
    for s in segs_all:
        for c in s["codes"]:
            used[c["code_id"]] = used.get(c["code_id"], 0) + 1
            if c.get("agency"):
                agencies.add(c["agency"])
    used_codes = sorted((codes[cid] for cid in used if cid in codes), key=_order)
    n_single = sum(1 for s in segs_all if _is_single_site(s))
    themes: dict[str, int] = {}
    for s in segs_all:
        t = _emt_theme(s)
        if t:
            themes[t] = themes.get(t, 0) + 1

    head = []
    if index_href:
        head.append(f"<a class=\"back\" href=\"{_e(index_href)}\">← All interviews</a>")
    head.append(f"<h1>{_e(iv['id'])}</h1>")
    memo_text = _interview_memo(db, iv["id"])
    head.append("<p class=\"sub\">Readable interview report, main coding pass. "
                + ("The interview memo follows the transcript. " if memo_text else "")
                + "Comments are not included.</p>")
    minutes = iv.get("duration_min")
    head.append("<dl class=\"meta\">"
                f"<dt>Role label</dt><dd>{_e(iv['role_label'])}</dd>"
                f"<dt>Actor group</dt><dd>{_e(iv['role_category'])}</dd>"
                f"<dt>Country</dt><dd>{_e(COUNTRY_NAME.get(iv['country'], iv['country']))}</dd>"
                f"<dt>Date</dt><dd>{_e(_date(iv['date']))}</dd>"
                f"<dt>Minutes</dt><dd>{_minutes(minutes)}</dd>"
                f"<dt>Coded segments</dt><dd>{len(segs_all)}</dd>"
                "</dl>")

    legend = ["<div class=\"legend\"><h3>Categories in this interview</h3><div class=\"cats\">"]
    for c in used_codes:
        legend.append(f"<span><span class=\"sw\" style=\"background:{_e(c['color'])}\"></span>"
                      f"{_code_label(c)} · {used[c['id']]}</span>")
    if not used_codes:
        legend.append("<span>No coded segments.</span>")
    legend.append("</div>")
    legend.append("<p>Numbers in the text mark where a coded segment starts; the box with the same number "
                  "next to the answer shows its coding.</p>")
    legend.append("<p>Valence: +1 inducing, 0 neutral, −1 blocking.</p>")
    if themes:
        legend.append("<p>Emergent themes: " + "; ".join(f"{_e(t)} ({n})" for t, n in sorted(themes.items()))
                      + ". They are noted on the Emergent Themes category of the segment, not as separate categories.</p>")
    if n_single:
        legend.append(f"<p><span class=\"lbl\">single-site</span> marks single-site optimisation without pooling, "
                      f"coded as a basis for pooling or as a precursor market ({n_single} segment"
                      f"{'' if n_single == 1 else 's'}). It is a label, not a category.</p>")
    if agencies:
        legend.append("<p>Agency: " + "; ".join(f"{_e(a)} = {_e(AGENCY_MEANING.get(a, a))}"
                                                   for a in sorted(agencies)) + ".</p>")
    legend.append("</div>")
    if memo_text:
        legend.append("<p class=\"jump\"><a href=\"#memo\">↓ Jump to the interview memo</a></p>")

    turns = []
    for p in data["paragraphs"]:
        when = f"<div class=\"when\">{_e(p['timestamp'])}</div>" if p.get("timestamp") else ""
        if p["speaker"] != "R":
            text = _render_text(p["text"], [], {}, p.get("confidential", []))
            turns.append(f"<div class=\"turn i\"><div>{when}<div class=\"text\"><span class=\"who\">Interviewer</span>"
                         f"{text}</div></div><div></div></div>")
            continue
        segs = sorted(p.get("segments", []), key=lambda s: (s["start_char"], -s["end_char"], s["id"]))
        numbers = {s["id"]: i for i, s in enumerate(segs, start=1)}
        text = _render_text(p["text"], segs, numbers, p.get("confidential", []))
        cards = "".join(_render_card(s, numbers[s["id"]], codes) for s in segs)
        turns.append(f"<div class=\"turn r\"><div>{when}<div class=\"text\">{text}</div></div>"
                     f"<div class=\"codes\">{cards}</div></div>")

    body = "\n".join(head) + "\n" + "".join(legend) + "\n<h2>Transcript</h2>\n" + "\n".join(turns)
    if memo_text:
        body += "\n<h2 id=\"memo\">Interview memo</h2>\n" + _render_memo(memo_text)
    return _page(f"{iv['id']} · interview report", body)


_MEMO_LABEL = re.compile(r"^([A-Z][A-Za-z /-]{2,40}):\s+(.*)$")


def _interview_memo(db: Database, interview_id: str) -> str:
    """Current text of the interview memo (the one the memo dialog shows), or '' if there is none."""
    memos = [m for m in list_memos(db, "interview", interview_id) if (m.get("text") or "").strip()]
    return memos[0]["text"].strip() if memos else ""


def _render_memo(text: str) -> str:
    """One paragraph per line; a leading 'Label:' (e.g. 'Strongest function:') is set in bold."""
    out = []
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        m = _MEMO_LABEL.match(line)
        if m:
            out.append(f"<p><b>{_e(m.group(1))}:</b> {_e(m.group(2))}</p>")
        else:
            out.append(f"<p>{_e(line)}</p>")
    return "<div class=\"memo\">" + "".join(out) + "</div>"


# ---------------------------------------------------------------------------
# overview page
# ---------------------------------------------------------------------------

def render_index(db: Database, interview_ids: list[str] | None = None,
                 link: Callable[[str], str] = lambda iid: f"{iid}.html") -> str:
    ids = interview_ids or _interview_ids(db)
    ivs = {r["id"]: r for r in db.query("SELECT * FROM interviews")}
    codes = sorted(code_list(db, include_retired=True), key=_order)
    counts: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for r in db.query("""SELECT p.interview_id AS iid, c.code_key AS k, COUNT(DISTINCT s.id) AS n
                         FROM segments s
                         JOIN coding_passes cp ON cp.id = s.coding_pass_id AND cp.name = 'primary'
                         JOIN paragraphs p ON p.id = s.paragraph_id
                         JOIN segment_codes sc ON sc.segment_id = s.id
                         JOIN codes c ON c.id = sc.code_id
                         GROUP BY p.interview_id, c.code_key"""):
        counts.setdefault(r["iid"], {})[r["k"]] = r["n"]
    for r in db.query("""SELECT p.interview_id AS iid, COUNT(*) AS n FROM segments s
                         JOIN coding_passes cp ON cp.id = s.coding_pass_id AND cp.name = 'primary'
                         JOIN paragraphs p ON p.id = s.paragraph_id GROUP BY p.interview_id"""):
        totals[r["iid"]] = r["n"]
    keys = [c["code_key"] for c in codes if any(counts.get(i, {}).get(c["code_key"]) for i in ids)]

    rows = []
    for iid in ids:
        iv = ivs.get(iid)
        if not iv:
            continue
        minutes = iv.get("duration_min")
        rows.append(f"<tr><td><a href=\"{_e(link(iid))}\">{_e(iid)}</a></td><td>{_e(iv['role_label'])}</td>"
                    f"<td>{_e(iv['role_category'])}</td><td>{_e(COUNTRY_NAME.get(iv['country'], iv['country']))}</td>"
                    f"<td class=\"nowrap\">{_e(_date(iv['date']))}</td><td class=\"n\">{_minutes(minutes)}</td>"
                    f"<td class=\"n\">{totals.get(iid, 0)}</td></tr>")
    overview = ("<table><thead><tr><th>ID</th><th>Role label</th><th>Actor group</th><th>Country</th><th>Date</th>"
                "<th class=\"n\">Minutes</th><th class=\"n\">Coded segments</th></tr></thead><tbody>"
                + "".join(rows) + "</tbody></table>")

    head_cells = "".join(f"<th class=\"n\">{_e(k)}</th>" for k in keys)
    crow = []
    col_sum = {k: 0 for k in keys}
    for iid in ids:
        if iid not in ivs:
            continue
        cells = []
        for k in keys:
            v = counts.get(iid, {}).get(k, 0)
            col_sum[k] += v
            cells.append(f"<td class=\"n{' zero' if not v else ''}\">{v}</td>")
        crow.append(f"<tr><td><a href=\"{_e(link(iid))}\">{_e(iid)}</a></td>{''.join(cells)}</tr>")
    crow.append("<tr><th>Total</th>" + "".join(f"<th class=\"n\">{col_sum[k]}</th>" for k in keys) + "</tr>")
    matrix = ("<div class=\"scroll\"><table><thead><tr><th>ID</th>" + head_cells + "</tr></thead><tbody>"
              + "".join(crow) + "</tbody></table></div>")
    key_names = "; ".join(f"{_e(c['code_key'])} = {_e(c['name'])}" for c in codes if c["code_key"] in keys)

    now = _dt.datetime.now()
    source = (f"Generated with the QDA coding tool used for this study on "
              f"{now.day} {now.strftime('%b %Y, %H:%M')}, from the main coding pass of the project database.")
    body = ("<h1>Interview reports</h1>"
            f"<p class=\"source\">{source}</p>"
            "<p class=\"sub\">Each interview opens as a readable report with the coded passages "
            "highlighted and their coding next to each answer.</p>"
            "<h2>Interviews</h2>" + overview +
            "<h2>Coded segments per category</h2>"
            "<p class=\"sub\">Number of segments carrying the category; a segment with several categories counts "
            "once for each.</p>" + matrix + f"<p class=\"keys\">{key_names}.</p>")
    return _page("Interview reports", body)


# ---------------------------------------------------------------------------
# writing files
# ---------------------------------------------------------------------------

def export_readable(db: Database, exports_dir: Path, interview_ids: list[str] | None = None) -> dict[str, Any]:
    """Write <ID>.html for every selected interview plus index.html into exports/<timestamp>/readable_reports/."""
    all_ids = _interview_ids(db)
    ids = [i for i in all_ids if i in set(interview_ids)] if interview_ids else all_ids
    unknown = sorted(set(interview_ids or []) - set(all_ids))
    if unknown:
        raise NotFound(f"Interview(s) not found: {', '.join(unknown)}")
    now = _dt.datetime.now()
    base = Path(exports_dir) / now.strftime("%Y-%m-%d_%H%M%S")
    n = 0
    while base.exists():
        n += 1
        base = Path(exports_dir) / (now.strftime("%Y-%m-%d_%H%M%S") + f"_{n}")
    folder = base / "readable_reports"
    folder.mkdir(parents=True)
    mapping = list_mapping(db)
    files: list[str] = []
    warnings: list[str] = []

    def save(name: str, content: str) -> None:
        (folder / name).write_text(content, encoding="utf-8")
        files.append(name)
        for term in check_text(content, mapping):
            msg = f"Mapped term '{term}' still occurs in {name}."
            if msg not in warnings:
                warnings.append(msg)

    for iid in ids:
        save(f"{iid}.html", render_interview(db, iid, index_href="index.html"))
    save("index.html", render_index(db, ids))
    if warnings:
        (folder / "WARNINGS.md").write_text("# Export warnings\n\n" + "\n".join(f"- {w}" for w in warnings) + "\n",
                                            encoding="utf-8")
        files.append("WARNINGS.md")
    return {"path": str(folder), "files": files, "warnings": warnings, "interviews": ids}
