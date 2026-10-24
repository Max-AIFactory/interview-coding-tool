"""Transcript importer.

One format only: Markdown with YAML frontmatter, UTF-8 (BOM and CRLF tolerated),
``[hh:mm:ss] I:`` / ``[hh:mm:ss] R:`` speaker labels at the start of blank-line
separated paragraphs.  The leading timestamp is kept as ``ParsedParagraph.timestamp``
and stripped from the text; square brackets in the text are plain text except for
the three tags ``[CONFIDENTIAL]``, ``[EN-ORIGINAL]`` and ``[NO-QUOTE]``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ALLOWED_COUNTRIES = {"DE", "CN"}
ALLOWED_ROLES = [
    "Aggregator, flexibility marketer",
    "Grid operator, energy company",
    "Equipment, storage and EMS manufacturer",
    "Research, consulting",
    "Association, policy",
    "Other",
]
ALLOWED_LANGUAGES = {"de", "en", "zh"}
ALLOWED_MODES = {"video", "phone", "in-person"}
REQUIRED_FIELDS = ["id", "country", "role_category", "role_label", "date"]

TIMESTAMP_RE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*")
SPEAKER_RE = re.compile(r"^(I|R):\s*")
CONF_OPEN = "[CONFIDENTIAL]"
CONF_CLOSE = "[/CONFIDENTIAL]"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class ImportError_(ValueError):
    """Raised with a human-readable message; maps to HTTP 422."""


@dataclass
class ParsedParagraph:
    idx: int
    speaker: str
    text: str
    confidential: list[tuple[int, int]] = field(default_factory=list)
    en_original: list[tuple[int, int]] = field(default_factory=list)
    no_quote: list[tuple[int, int]] = field(default_factory=list)
    para_no: int | None = None
    line_no: int = 0
    timestamp: str | None = None  # 'hh:mm:ss' from the leading [hh:mm:ss], stripped from ``text``


@dataclass
class ParsedTranscript:
    meta: dict[str, Any]
    paragraphs: list[ParsedParagraph]
    warnings: list[str] = field(default_factory=list)
    stripped_timestamps: int = 0  # number of paragraphs whose leading [hh:mm:ss] was kept in ``timestamp``

    @property
    def timestamps(self) -> int:
        return self.stripped_timestamps


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _strip_comment(v: str) -> str:
    # a ' #' starts a comment unless inside quotes
    if v.startswith(("'", '"')):
        q = v[0]
        end = v.find(q, 1)
        if end != -1:
            return v[: end + 1]
        return v
    idx = v.find(" #")
    if idx != -1:
        v = v[:idx]
    if v.startswith("#"):
        return ""
    return v.strip()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    """Returns (meta, body, body_start_line). Very small YAML subset: ``key: value``."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ImportError_("Missing YAML frontmatter: file must start with a line containing only '---'.")
    meta: dict[str, Any] = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            break
        s = line.strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        if ":" not in s:
            raise ImportError_(f"Frontmatter line {i + 1} is not 'key: value': {line!r}")
        key, _, val = s.partition(":")
        key = key.strip()
        val = _unquote(_strip_comment(val.strip()))
        meta[key] = val
        i += 1
    else:
        raise ImportError_("Frontmatter is not closed: no second '---' line found.")
    body = "\n".join(lines[i + 1:])
    return meta, body, i + 2


def validate_meta(meta: dict[str, Any]) -> dict[str, Any]:
    missing = [f for f in REQUIRED_FIELDS if not str(meta.get(f, "")).strip()]
    if missing:
        raise ImportError_(f"Frontmatter is missing required field(s): {', '.join(missing)}.")
    out: dict[str, Any] = {}
    out["id"] = str(meta["id"]).strip()
    if not ID_RE.match(out["id"]):
        raise ImportError_(f"Invalid interview id {out['id']!r}: use letters, digits, '-' or '_' (e.g. DE-00).")
    country = str(meta["country"]).strip().upper()
    if country not in ALLOWED_COUNTRIES:
        raise ImportError_(f"country must be DE or CN, got {meta['country']!r}.")
    out["country"] = country
    role = str(meta["role_category"]).strip()
    if role not in ALLOWED_ROLES:
        raise ImportError_(
            f"role_category {role!r} is not allowed. Allowed: {' | '.join(ALLOWED_ROLES)}.")
    out["role_category"] = role
    out["role_label"] = str(meta["role_label"]).strip()
    date = str(meta["date"]).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ImportError_(f"date must be YYYY-MM-DD, got {date!r}.")
    out["date"] = date
    dur = str(meta.get("duration_min", "")).strip()
    if dur:
        try:
            out["duration_min"] = int(float(dur))
        except ValueError:
            raise ImportError_(f"duration_min must be a number, got {dur!r}.")
    else:
        out["duration_min"] = None
    lang = str(meta.get("language_original", "")).strip().lower()
    if lang and lang not in ALLOWED_LANGUAGES:
        raise ImportError_(f"language_original must be de, en or zh, got {lang!r}.")
    out["language_original"] = lang or None
    mode = str(meta.get("interview_mode", "")).strip().lower()
    if mode and mode not in ALLOWED_MODES:
        raise ImportError_(f"interview_mode must be video, phone or in-person, got {mode!r}.")
    out["interview_mode"] = mode or None
    pilot = str(meta.get("pilot", "false")).strip().lower()
    if pilot not in {"true", "false", "yes", "no", "1", "0", ""}:
        raise ImportError_(f"pilot must be true or false, got {pilot!r}.")
    out["pilot"] = 1 if pilot in {"true", "yes", "1"} else 0
    out["notes"] = str(meta.get("notes", "")).strip()
    return out


EN_OPEN = "[EN-ORIGINAL]"
EN_CLOSE = "[/EN-ORIGINAL]"
NQ_OPEN = "[NO-QUOTE]"
NQ_CLOSE = "[/NO-QUOTE]"
# kind -> (open tag, close tag). The only three inline tags the format interprets; every other
# square bracket (anonymisation placeholders, editorial notes) is ordinary, codable text.
TAGS = {"confidential": (CONF_OPEN, CONF_CLOSE), "en_original": (EN_OPEN, EN_CLOSE), "no_quote": (NQ_OPEN, NQ_CLOSE)}


def normalize_timestamp(h: str, m: str, s: str | None) -> str:
    """'[4:12]' -> '00:04:12', '[1:02:03]' -> '01:02:03', '[00:04:05]' -> '00:04:05'."""
    if s is None:  # mm:ss without hours
        return f"00:{int(h):02d}:{m}"
    return f"{int(h):02d}:{m}:{s}"


def extract_tags(text: str, where: str) -> tuple[str, dict[str, list[tuple[int, int]]]]:
    """Strip all supported inline tags and return (cleaned text, ranges per kind) with
    ranges as code-point offsets into the cleaned text.  Different kinds may overlap
    or enclose each other; the same kind must not nest.  Whitespace directly inside
    a tag pair is not part of the range."""
    # find every marker occurrence
    markers: list[tuple[int, str, bool, int]] = []  # (pos, kind, is_open, length)
    for kind, (o, c) in TAGS.items():
        for tag, is_open in ((o, True), (c, False)):
            start = 0
            while True:
                pos = text.find(tag, start)
                if pos == -1:
                    break
                markers.append((pos, kind, is_open, len(tag)))
                start = pos + len(tag)
    markers.sort()
    out: list[str] = []
    out_len = 0
    pos = 0
    prev_char = ""
    open_at: dict[str, int | None] = {k: None for k in TAGS}
    ranges: dict[str, list[tuple[int, int]]] = {k: [] for k in TAGS}

    def take(chunk: str) -> None:
        nonlocal out_len, prev_char
        # a stripped tag must not leave a double space behind ("year. [TAG] That" -> "year. That")
        if pos > 0 and prev_char == " " and chunk.startswith(" "):
            chunk = chunk[1:]
        out.append(chunk)
        out_len += len(chunk)
        if chunk:
            prev_char = chunk[-1]

    for mpos, kind, is_open, mlen in markers:
        take(text[pos:mpos])
        o_tag, c_tag = TAGS[kind]
        if is_open:
            if open_at[kind] is not None:
                raise ImportError_(f"{where}: nested '{o_tag}' tags are not allowed.")
            open_at[kind] = out_len
        else:
            if open_at[kind] is None:
                raise ImportError_(f"{where}: '{c_tag}' without opening tag.")
            a, b = open_at[kind], out_len
            inner = "".join(out)[a:b]
            lead = len(inner) - len(inner.lstrip())
            trail = len(inner) - len(inner.rstrip())
            if b - trail > a + lead:
                ranges[kind].append((a + lead, b - trail))
            open_at[kind] = None
        pos = mpos + mlen
    take(text[pos:])
    for kind, start in open_at.items():
        if start is not None:
            raise ImportError_(f"{where}: '{TAGS[kind][0]}' is never closed.")
    return "".join(out), ranges


def extract_confidential(text: str, where: str) -> tuple[str, list[tuple[int, int]]]:
    """Backward-compatible helper: cleaned text + confidential ranges only."""
    cleaned, ranges = extract_tags(text, where)
    return cleaned, ranges["confidential"]


def parse_transcript(raw: str, filename: str = "") -> ParsedTranscript:
    if filename and not filename.lower().endswith(".md"):
        raise ImportError_(f"Only .md files are accepted (got {filename!r}).")
    if raw.startswith("﻿"):
        raw = raw[1:]
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    meta_raw, body, body_start = parse_frontmatter(raw)
    meta = validate_meta(meta_raw)
    if filename:
        stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:-3]
        if stem != meta["id"]:
            # rule: filename = interview ID; warn instead of refusing so renamed files still import
            pass
    warnings: list[str] = []
    if filename:
        stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:-3]
        if stem != meta["id"]:
            warnings.append(f"Filename '{stem}.md' differs from frontmatter id '{meta['id']}'.")

    paragraphs: list[ParsedParagraph] = []
    stripped = 0
    # split body into blank-line separated blocks, remembering line numbers
    lines = body.split("\n")
    block: list[str] = []
    block_start = 0
    blocks: list[tuple[int, list[str]]] = []
    for i, line in enumerate(lines):
        if line.strip() == "":
            if block:
                blocks.append((block_start, block))
                block = []
        else:
            if not block:
                block_start = i
            block.append(line)
    if block:
        blocks.append((block_start, block))

    para_no = 0
    for idx, (start, blines) in enumerate(blocks):
        first = blines[0]
        line_no = body_start + start
        m = TIMESTAMP_RE.match(first)
        timestamp = None
        if m:
            first = first[m.end():]
            timestamp = normalize_timestamp(m.group(1), m.group(2), m.group(3))
            stripped += 1
        sm = SPEAKER_RE.match(first)
        if not sm:
            snippet = first[:40]
            raise ImportError_(
                f"Line {line_no}: paragraph does not start with 'I:' or 'R:': {snippet!r}")
        speaker = sm.group(1)
        first = first[sm.end():]
        text = "\n".join([first.rstrip()] + [l.rstrip() for l in blines[1:]]).strip()
        text, tags = extract_tags(text, f"Line {line_no}")
        if not text.strip():
            raise ImportError_(f"Line {line_no}: empty {'respondent' if speaker == 'R' else 'interviewer'} turn.")
        p = ParsedParagraph(idx=idx, speaker=speaker, text=text, confidential=tags["confidential"],
                            en_original=tags["en_original"], no_quote=tags["no_quote"], line_no=line_no,
                            timestamp=timestamp)
        if speaker == "R":
            para_no += 1
            p.para_no = para_no
        paragraphs.append(p)
    if not paragraphs:
        raise ImportError_("Transcript body is empty: no 'I:' / 'R:' paragraphs found.")
    if not any(p.speaker == "R" for p in paragraphs):
        raise ImportError_("Transcript contains no respondent ('R:') turns.")
    return ParsedTranscript(meta=meta, paragraphs=paragraphs, warnings=warnings, stripped_timestamps=stripped)


def decode_upload(data: bytes, filename: str) -> str:
    if not filename.lower().endswith(".md"):
        raise ImportError_(f"Only .md files are accepted (got {filename!r}).")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportError_(f"File is not valid UTF-8 (byte {exc.start}). Save the transcript as UTF-8.")
