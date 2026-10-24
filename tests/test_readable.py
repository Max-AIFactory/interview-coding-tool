"""Readable interview reports: one HTML page per interview plus an overview page."""
from pathlib import Path

from conftest import code_quote, locate

QUOTE = "the metering and baseline rules for aggregated loads are still unclear"


def _coded(demo):
    client, codes = demo
    seg = code_quote(client, codes, "DE-00", QUOTE, ["F4", "BAR"], valence=-1, agency="GRID",
                     comment="PRIVATE-COMMENT-123", quote_candidate=True)
    r = client.post("/api/v1/interactions", json={
        "from_code_id": codes["F4"]["id"], "to_code_id": codes["F6"]["id"], "mechanism": "blocking",
        "segment_id": seg["id"], "description": "no rules no contracts", "condition": "if rules stay unclear"})
    assert r.status_code == 201, r.text
    return client, codes, seg


def test_report_shows_coding_but_no_comment_or_confidential_text(demo):
    client, codes, seg = _coded(demo)
    r = client.get("/api/v1/readable/DE-00")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    h = r.text
    assert seg["segment_ref"] in h
    assert "Market Formation" in h and "(F4)" in h and "Barrier / Blocking Mechanism" in h
    assert "agency GRID" in h and "−1" in h
    assert "★" not in h and "quote candidate" not in h     # internal working marker is not shown
    assert "+1 inducing, 0 neutral, −1 blocking" in h
    assert "Resource Mobilization" in h and "(blocking)" in h
    assert "no rules no contracts" in h and "if rules stay unclear" in h
    assert "00:04:12" in h and "Aggregator, flexibility marketer" in h
    assert "PRIVATE-COMMENT-123" not in h                      # comments are never shown
    assert "legal dispute" not in h                            # confidential text is never shown
    assert "[confidential passage removed]" in h


def test_pages_are_self_contained(demo):
    client, codes, seg = _coded(demo)
    for path in ("/api/v1/readable/DE-00", "/api/v1/readable"):
        h = client.get(path).text
        assert "<script" not in h
        assert 'src="http' not in h and 'href="http' not in h and "@import" not in h and "url(" not in h


def test_recoding_pass_is_not_shown(demo):
    client, codes = demo
    p = client.post("/api/v1/interviews/DE-00/passes").json()
    loc = locate(client, "DE-00", "Data access is the real bottleneck")
    r = client.post("/api/v1/segments", json={
        "paragraph_id": loc["paragraph_id"], "start_char": loc["start_char"], "end_char": loc["end_char"],
        "codes": [{"code_id": codes["F6"]["id"]}], "coding_pass_id": p["id"]})
    assert r.status_code == 201, r.text
    h = client.get("/api/v1/readable/DE-00").text
    assert r.json()["segment_ref"] not in h
    assert "<dt>Coded segments</dt><dd>0</dd>" in h


def test_overview_and_written_files(demo):
    client, codes, seg = _coded(demo)
    idx = client.get("/api/v1/readable").text
    assert "DE-00" in idx and "CN-00" in idx and ">F4<" in idx and "/api/v1/readable/DE-00" in idx
    assert "Generated with the QDA coding tool" in idx
    r = client.post("/api/v1/readable/export", json={"interview_ids": ["DE-00"]})
    assert r.status_code == 200, r.text
    body = r.json()
    folder = Path(body["path"])
    assert folder.name == "readable_reports"
    assert sorted(body["files"]) == ["DE-00.html", "index.html"]
    assert seg["segment_ref"] in (folder / "DE-00.html").read_text(encoding="utf-8")
    index = (folder / "index.html").read_text(encoding="utf-8")
    assert 'href="DE-00.html"' in index and "CN-00" not in index


def test_categories_follow_codebook_order(demo):
    client, codes = demo
    code_quote(client, codes, "DE-00", "The smart meter rollout is the obvious one.", ["BAR", "CTX-SEC", "F4"])
    h = client.get("/api/v1/readable/DE-00").text
    assert h.index("Market Formation") < h.index("Sectoral context") < h.index("Barrier / Blocking Mechanism")
    idx = client.get("/api/v1/readable").text
    assert idx.index(">F4<") < idx.index(">CTX-SEC<") < idx.index(">BAR<")


def test_single_site_label_and_emt_theme_from_notes(demo):
    client, codes = demo
    code_quote(client, codes, "DE-00", "Capital was never the main issue for us, we are VC-financed.", ["F6"],
               comment="own money only SECRETNOTE1 (single-site)")
    code_quote(client, codes, "DE-00", "Battery storage is also changing the picture", ["EMT"],
               comment="new demand SECRETNOTE2, proposal EMT-ALPHA, short description")
    code_quote(client, codes, "DE-00", "People are: we compete with the big utilities", ["EMT"],
               comment="Proposal EMT-BETA, see also 00:01:12")
    code_quote(client, codes, "DE-00", "Data access is the real bottleneck", ["F6"],
               comment="proposal EMT-ALPHA on a segment without the EMT category")
    h = client.get("/api/v1/readable/DE-00").text
    assert h.count(">single-site</span>") == 2                    # one box + the legend
    assert "Theme: EMT-ALPHA" in h
    assert "Theme: EMT-BETA" in h      # capital "Proposal" is recognised
    assert h.count("Theme: ") == 2                                   # no theme without the EMT category
    assert "SECRETNOTE1" not in h and "SECRETNOTE2" not in h       # the notes themselves stay hidden
    assert "Emergent themes: EMT-ALPHA (1); EMT-BETA (1)" in h


def test_emt_theme_is_shown_exactly_as_written(demo):
    client, codes = demo
    code_quote(client, codes, "DE-00", "Battery storage is also changing the picture", ["EMT"],
               comment="proposal EMT-NewIdea, something else")
    assert "Theme: EMT-NewIdea" in client.get("/api/v1/readable/DE-00").text


def test_interview_memo_after_transcript(demo):
    client, codes = demo
    seg = code_quote(client, codes, "DE-00", QUOTE, ["F4"])
    m = client.post("/api/v1/memos", json={"scope": "interview", "ref_id": "DE-00",
                                           "text": "Strongest function: OLDVERSION.\nOpen questions: none."})
    assert m.status_code == 201, m.text
    r = client.put(f"/api/v1/memos/{m.json()['id']}",
                   json={"text": "Strongest function: NEWVERSION <b>bold</b>.\nOpen questions: none yet."})
    assert r.status_code == 200, r.text
    client.post("/api/v1/memos", json={"scope": "segment", "ref_id": str(seg["id"]), "text": "SEGMENTMEMO"})
    h = client.get("/api/v1/readable/DE-00").text
    assert "<h2 id=\"memo\">Interview memo</h2>" in h and 'href="#memo"' in h
    assert h.index("<h2>Transcript</h2>") < h.index("Interview memo</h2>")      # memo comes after the transcript
    assert "<b>Strongest function:</b> NEWVERSION &lt;b&gt;bold&lt;/b&gt;." in h  # current version, escaped
    assert "OLDVERSION" not in h and "SEGMENTMEMO" not in h
    assert "Interview memo" not in client.get("/api/v1/readable/CN-00").text       # no memo, no section


def test_unknown_interview_is_404(demo):
    client, codes = demo
    assert client.get("/api/v1/readable/XX-99").status_code == 404
    assert client.post("/api/v1/readable/export", json={"interview_ids": ["XX-99"]}).status_code == 404
