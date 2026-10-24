"""Importer unit tests: frontmatter, speaker labels, timestamps, confidential tags,
umlauts/Chinese, duplicates, rejection of non-.md files."""
from pathlib import Path

import pytest

from app.services.importer import ImportError_, parse_transcript, decode_upload, extract_confidential, extract_tags
from conftest import SAMPLES, import_md

HEAD = """---
id: T-01
country: DE
role_category: Research, consulting
role_label: Researcher at a German institute
date: 2026-09-10
---
"""


def test_parses_sample_with_umlauts_chinese_and_timestamp():
    t = parse_transcript((SAMPLES / "DE-00.md").read_text(encoding="utf-8"), "DE-00.md")
    assert t.meta["id"] == "DE-00" and t.meta["country"] == "DE" and t.meta["pilot"] == 1
    assert t.stripped_timestamps == len(t.paragraphs) == 18          # every paragraph carries [hh:mm:ss]
    assert t.paragraphs[4].timestamp == "00:04:12" and t.paragraphs[4].text.startswith("Where do the revenues")
    assert any("ü" in p.text for p in t.paragraphs)
    cn = parse_transcript((SAMPLES / "CN-00.md").read_text(encoding="utf-8"), "CN-00.md")
    assert any("虚拟电厂" in p.text for p in cn.paragraphs)
    assert cn.meta["language_original"] == "zh"
    # paragraph numbering: only respondent turns get ¶n
    r_turns = [p for p in t.paragraphs if p.speaker == "R"]
    assert [p.para_no for p in r_turns] == list(range(1, len(r_turns) + 1))
    assert all(p.para_no is None for p in t.paragraphs if p.speaker == "I")


def test_timestamp_prefix_is_kept_per_paragraph_and_stripped_from_text():
    t = parse_transcript(HEAD + "\n[00:12:34] I: Hello?\n\n[1:02:03] R: Yes.\n\n[4:05] R: Short.\n\nR: None.\n", "T-01.md")
    assert t.stripped_timestamps == 3
    assert [p.text for p in t.paragraphs] == ["Hello?", "Yes.", "Short.", "None."]
    assert [p.timestamp for p in t.paragraphs] == ["00:12:34", "01:02:03", "00:04:05", None]


def test_no_quote_tags_create_ranges_and_are_stripped():
    text, tags = extract_tags("a [NO-QUOTE] secret plan [/NO-QUOTE] b [EN-ORIGINAL] c [/EN-ORIGINAL] d", "x")
    assert text == "a secret plan b c d"
    assert tags["no_quote"] == [(2, 13)] and text[2:13] == "secret plan"
    assert tags["en_original"] == [(16, 17)]
    with pytest.raises(ImportError_, match="nested"):
        extract_tags("[NO-QUOTE] a [NO-QUOTE] b [/NO-QUOTE] [/NO-QUOTE]", "x")
    with pytest.raises(ImportError_, match="never closed"):
        extract_tags("x [NO-QUOTE] y", "x")
    # placeholders and editorial notes are NOT tags
    text, tags = extract_tags("[the TSO] said [Private small talk removed.] nothing [word hard to understand]", "x")
    assert text == "[the TSO] said [Private small talk removed.] nothing [word hard to understand]"
    assert all(not v for v in tags.values())
    t = parse_transcript((SAMPLES / "DE-00.md").read_text(encoding="utf-8"), "DE-00.md")
    tagged = [p for p in t.paragraphs if p.no_quote]
    assert len(tagged) == 1
    a, b = tagged[0].no_quote[0]
    assert tagged[0].text[a:b].startswith("One of the big utilities tried to have us excluded")
    assert "[NO-QUOTE]" not in tagged[0].text


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "DE-77.md"


def test_real_transcript_format_fixture_imports_unchanged(client):
    """Real-format fixture: BOM, CRLF, long notes line with colons/brackets, [hh:mm:ss] on every
    paragraph (two consecutive ones identical), bracket placeholders and editorial notes kept verbatim,
    one [CONFIDENTIAL] and one [NO-QUOTE] passage, a 2000+ character single-line paragraph."""
    raw = FIXTURE.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    t = parse_transcript(raw.decode("utf-8"), "DE-77.md")
    m = t.meta
    assert m["id"] == "DE-77" and m["country"] == "DE" and m["role_category"] == "Equipment, storage and EMS manufacturer"
    assert m["date"] == "2026-07-22" and m["duration_min"] == 38 and m["language_original"] == "de"
    assert m["interview_mode"] == "video" and m["pilot"] == 0
    assert m["notes"].startswith("Anonymised test fixture: [the manufacturer] replaces the employer")
    assert "00:18:10 to 00:19:02" in m["notes"] and m["notes"].endswith("checked 2026-07-29.")
    assert all(p.timestamp for p in t.paragraphs) and t.stripped_timestamps == len(t.paragraphs) == 11
    assert t.paragraphs[3].timestamp == t.paragraphs[4].timestamp == "00:04:15"
    assert t.paragraphs[0].speaker == "R" and t.paragraphs[0].para_no == 1        # R: may come first
    assert max(len(p.text) for p in t.paragraphs) > 2000 and all("\n" not in p.text for p in t.paragraphs)
    # placeholders and editorial notes stay in the text, tags are stripped and become ranges
    r1 = t.paragraphs[0].text
    assert r1.startswith("[Greeting omitted.] Sure, go ahead.") and "[the manufacturer]" in r1 and "[a competitor]" in r1
    r3 = t.paragraphs[2].text
    assert "[Interruption: phone rings, respondent mutes it.]" in r3
    r5 = t.paragraphs[4].text
    assert "[inaudible, approx. two seconds]" in r5 and "[NO-QUOTE]" not in r5 and len(t.paragraphs[4].no_quote) == 1
    a, b = t.paragraphs[4].no_quote[0]
    assert r5[a:b].startswith("Our management wants to drop") and r5[a:b].endswith("must not appear anywhere.")
    r7 = t.paragraphs[6].text
    assert r7.startswith("[the field trial] put 2,300 residential heat pumps") and "[CONFIDENTIAL]" not in r7
    assert "[Personal remarks about the respondent's family removed, 00:18:10 to 00:19:02.]" in r7 and len(t.paragraphs[6].confidential) == 1
    # and through the API, without any change to the file
    r = client.post("/api/v1/interviews/import", files={"file": ("DE-77.md", raw, "text/markdown")})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["timestamps"] == 11 and rep["no_quote_ranges"] == 1 and rep["confidential_ranges"] == 1
    iv = client.get("/api/v1/interviews/DE-77").json()
    assert iv["interview"]["notes"] == m["notes"] and iv["interview"]["duration_min"] == 38
    pars = iv["paragraphs"]
    assert [p["timestamp"] for p in pars][:5] == ["00:00:06", "00:00:38", "00:01:02", "00:04:15", "00:04:15"]
    assert pars[0]["text"] == r1 and pars[4]["text"] == r5 and pars[6]["text"] == r7
    nq = pars[4]["no_quote"][0]
    assert pars[4]["text"][nq["start_char"]:nq["end_char"]] == r5[a:b]
    assert pars[6]["confidential"] and pars[6]["text"][pars[6]["confidential"][0]["start_char"]:].startswith("Most of the units were in a housing cooperative")


def test_missing_speaker_label_names_the_line():
    with pytest.raises(ImportError_) as e:
        parse_transcript(HEAD + "\nI: Q\n\nThis has no label.\n", "T-01.md")
    assert "Line" in str(e.value) and "I:' or 'R:" in str(e.value)


def test_required_fields_and_enums():
    with pytest.raises(ImportError_, match="missing required"):
        parse_transcript("---\nid: X\ncountry: DE\n---\nR: hi\n", "X.md")
    bad = HEAD.replace("country: DE", "country: FR")
    with pytest.raises(ImportError_, match="country must be DE or CN"):
        parse_transcript(bad + "\nR: hi\n", "T-01.md")
    bad = HEAD.replace("role_category: Research", "role_category: Consultant")
    with pytest.raises(ImportError_, match="role_category"):
        parse_transcript(bad + "\nR: hi\n", "T-01.md")


def test_confidential_tags_create_ranges_and_are_stripped():
    text, ranges = extract_confidential("abc [CONFIDENTIAL]secret[/CONFIDENTIAL] def", "x")
    assert text == "abc secret def" and ranges == [(4, 10)]
    with pytest.raises(ImportError_, match="never closed"):
        extract_confidential("a [CONFIDENTIAL] b", "x")
    with pytest.raises(ImportError_, match="without opening"):
        extract_confidential("a [/CONFIDENTIAL] b", "x")


def test_en_original_tags_create_ranges_and_may_overlap_confidential():
    text, tags = extract_tags("a [EN-ORIGINAL] b [CONFIDENTIAL] c [/EN-ORIGINAL] d [/CONFIDENTIAL] e", "x")
    assert text == "a b c d e"                       # no double spaces left behind
    assert tags["en_original"] == [(2, 5)] and text[2:5] == "b c"
    assert tags["confidential"] == [(4, 7)] and text[4:7] == "c d"
    with pytest.raises(ImportError_, match="nested"):
        extract_tags("[EN-ORIGINAL] a [EN-ORIGINAL] b [/EN-ORIGINAL] [/EN-ORIGINAL]", "x")
    with pytest.raises(ImportError_, match="never closed"):
        extract_tags("x [EN-ORIGINAL] y", "x")
    with pytest.raises(ImportError_, match="without opening"):
        extract_tags("x [/EN-ORIGINAL] y", "x")
    t = parse_transcript((SAMPLES / "DE-00.md").read_text(encoding="utf-8"), "DE-00.md")
    tagged = [p for p in t.paragraphs if p.en_original]
    assert len(tagged) == 1
    a, b = tagged[0].en_original[0]
    assert tagged[0].text[a:b] == "That speed is something we could learn from."
    assert "[EN-ORIGINAL]" not in tagged[0].text


def test_multiline_paragraph_keeps_line_breaks():
    t = parse_transcript(HEAD + "\nR: first line\nsecond line\n", "T-01.md")
    assert t.paragraphs[0].text == "first line\nsecond line"


def test_non_md_and_non_utf8_rejected():
    with pytest.raises(ImportError_, match="Only .md"):
        decode_upload(b"x", "transcript.txt")
    with pytest.raises(ImportError_, match="UTF-8"):
        decode_upload("ü".encode("latin-1"), "t.md")


def test_api_rejects_txt_and_duplicates(client, tmp_path):
    r = client.post("/api/v1/interviews/import", files={"file": ("DE-00.txt", b"---\nid: X\n---\nR: hi", "text/plain")})
    assert r.status_code == 422 and ".md" in r.json()["detail"]
    assert import_md(client, SAMPLES / "DE-00.md").status_code == 200
    r = import_md(client, SAMPLES / "DE-00.md")
    assert r.status_code == 422 and "already exists" in r.json()["detail"]
    # dry run never writes
    r = import_md(client, SAMPLES / "CN-00.md", dry_run=True)
    assert r.status_code == 200 and r.json()["dry_run"] is True
    assert client.get("/api/v1/interviews/CN-00").status_code == 404


def test_reimport_keeps_codings_and_reports_failures(client, tmp_path):
    assert import_md(client, SAMPLES / "DE-00.md").status_code == 200
    codes = {c["code_key"]: c for c in client.get("/api/v1/codes").json()["flat"]}
    loc = client.post("/api/v1/interviews/DE-00/locate", json={"text": "The technical side works."}).json()[0]
    r = client.post("/api/v1/segments", json={"paragraph_id": loc["paragraph_id"], "start_char": loc["start_char"],
                                              "end_char": loc["end_char"], "codes": [{"code_id": codes["F1"]["id"]}]})
    assert r.status_code == 201
    loc2 = client.post("/api/v1/interviews/DE-00/locate", json={"text": "Capital was never the main issue"}).json()[0]
    client.post("/api/v1/segments", json={"paragraph_id": loc2["paragraph_id"], "start_char": loc2["start_char"],
                                          "end_char": loc2["end_char"], "codes": [{"code_id": codes["F6"]["id"]}]})
    # change the text of the second quote in a modified copy -> that coding cannot be re-attached
    src = (SAMPLES / "DE-00.md").read_text(encoding="utf-8").replace("Capital was never the main issue", "Money was never the main issue")
    src = src.replace("The technical side works.", "Well. The technical side works.")
    p = tmp_path / "DE-00.md"
    p.write_text(src, encoding="utf-8")
    r = import_md(client, p, mode="reimport", dry_run=True)
    assert r.status_code == 200
    rep = r.json()
    assert rep["reattached"] == 1 and len(rep["reattach_failed"]) == 1
    r = import_md(client, p, mode="reimport")
    assert r.status_code == 200
    iv = client.get("/api/v1/interviews/DE-00").json()
    segs = [s for pp in iv["paragraphs"] for s in pp["segments"]]
    assert len(segs) == 1 and segs[0]["text_snapshot"] == "The technical side works."
    assert segs[0]["codes"][0]["code_key"] == "F1"


def test_reimport_syncs_timestamps_and_no_quote_ranges(client, tmp_path):
    assert import_md(client, SAMPLES / "DE-00.md").status_code == 200
    codes = {c["code_key"]: c for c in client.get("/api/v1/codes").json()["flat"]}
    iv = client.get("/api/v1/interviews/DE-00").json()
    assert [p["timestamp"] for p in iv["paragraphs"]][:3] == ["00:00:05", "00:00:20", "00:02:10"]
    assert sum(len(p["no_quote"]) for p in iv["paragraphs"]) == 1
    # a quote candidate that the new file puts inside a [NO-QUOTE] passage loses its flag (with a warning)
    loc = client.post("/api/v1/interviews/DE-00/locate", json={"text": "The technical side works."}).json()[0]
    r = client.post("/api/v1/segments", json={"paragraph_id": loc["paragraph_id"], "start_char": loc["start_char"],
                                              "end_char": loc["end_char"], "codes": [{"code_id": codes["F1"]["id"]}],
                                              "quote_candidate": True})
    assert r.status_code == 201 and r.json()["quotation_block"] == "none"
    src = (SAMPLES / "DE-00.md").read_text(encoding="utf-8")
    src = src.replace("[00:02:31] R: The technical side works.", "[00:02:45] R: [NO-QUOTE] The technical side works. [/NO-QUOTE]")
    src = src.replace("[00:00:05] I:", "I:")                       # a paragraph without timestamp -> NULL
    p = tmp_path / "DE-00.md"
    p.write_text(src, encoding="utf-8")
    r = import_md(client, p, mode="reimport")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["timestamps"] == 17 and rep["no_quote_ranges"] == 2 and rep["reattached"] == 1
    assert any("quote-candidate flag was removed" in w for w in rep["warnings"])
    iv = client.get("/api/v1/interviews/DE-00").json()
    ts = [p["timestamp"] for p in iv["paragraphs"]]
    assert ts[0] is None and ts[1] == "00:00:20" and ts[3] == "00:02:45"
    seg = [s for pp in iv["paragraphs"] for s in pp["segments"]][0]
    assert seg["quotation_block"] == "full" and seg["quote_candidate"] is False and seg["timestamp"] == "00:02:45"
