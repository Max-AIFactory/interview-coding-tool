"""Coding workspace logic through the API: offsets, overlaps, confidential, multi-code,
merge/undo, batch atomicity, passes and reliability metrics (sections 4.1, 6.3, 6.4, 6.9, 9)."""
from app.services.analysis import kappa
from conftest import code_quote, locate


def test_overlapping_segments_two_codes_counted_in_matrix(demo):
    """Acceptance test 2."""
    client, codes = demo
    a = code_quote(client, codes, "DE-00", "the metering and baseline rules for aggregated loads are still unclear", ["F4", "BAR"], valence=-1)
    b = code_quote(client, codes, "DE-00", "baseline rules for aggregated loads are still unclear, so grid operators", ["F5"], valence=-1)
    iv = client.get("/api/v1/interviews/DE-00").json()
    par = next(p for p in iv["paragraphs"] if p["id"] == a["paragraph_id"])
    ids = {s["id"] for s in par["segments"]}
    assert {a["id"], b["id"]} <= ids
    m = client.get("/api/v1/analysis/matrix").json()
    assert m["cells"][str(codes["F4"]["id"])]["DE-00"] == 1
    assert m["cells"][str(codes["F5"]["id"])]["DE-00"] == 1
    assert m["cells"][str(codes["BAR"]["id"])]["DE-00"] == 1
    co = client.get("/api/v1/analysis/cooccurrence").json()
    k = f"{min(codes['F4']['id'], codes['F5']['id'])}:{max(codes['F4']['id'], codes['F5']['id'])}"
    assert co["cells"][k] == 1


def test_offsets_are_code_points(demo):
    client, codes = demo
    hit = locate(client, "CN-00", "虚拟电厂")
    iv = client.get("/api/v1/interviews/CN-00").json()
    par = next(p for p in iv["paragraphs"] if p["id"] == hit["paragraph_id"])
    assert par["text"][hit["start_char"]:hit["end_char"]] == "虚拟电厂"
    seg = client.post("/api/v1/segments", json={"paragraph_id": hit["paragraph_id"], "start_char": hit["start_char"],
                                                "end_char": hit["end_char"], "codes": [{"code_id": codes["F3"]["id"]}]}).json()
    assert seg["text_snapshot"] == "虚拟电厂"


def test_server_side_validation(demo):
    client, codes = demo
    iv = client.get("/api/v1/interviews/DE-00").json()
    ipar = next(p for p in iv["paragraphs"] if p["speaker"] == "I")
    rpar = next(p for p in iv["paragraphs"] if p["speaker"] == "R")
    conf = next(p for p in iv["paragraphs"] if p["confidential"])
    f1 = codes["F1"]["id"]
    def post(body):
        return client.post("/api/v1/segments", json=body)
    assert post({"paragraph_id": ipar["id"], "start_char": 0, "end_char": 3, "codes": [{"code_id": f1}]}).status_code == 422
    assert post({"paragraph_id": rpar["id"], "start_char": 0, "end_char": 10 ** 6, "codes": [{"code_id": f1}]}).status_code == 422
    assert post({"paragraph_id": rpar["id"], "start_char": 5, "end_char": 5, "codes": [{"code_id": f1}]}).status_code == 422
    assert post({"paragraph_id": rpar["id"], "start_char": 0, "end_char": 3, "codes": [{"code_id": 999999}]}).status_code == 422
    assert post({"paragraph_id": rpar["id"], "start_char": 0, "end_char": 3, "codes": [{"code_id": f1, "valence": 2}]}).status_code == 422
    assert post({"paragraph_id": rpar["id"], "start_char": 0, "end_char": 3, "codes": [{"code_id": f1, "agency": "state"}]}).status_code == 422
    c = conf["confidential"][0]
    r = post({"paragraph_id": conf["id"], "start_char": c["start_char"] + 1, "end_char": c["start_char"] + 5, "codes": [{"code_id": f1}]})
    assert r.status_code == 422 and "confidential" in r.json()["detail"]
    assert post({"paragraph_id": rpar["id"], "start_char": 0, "end_char": 3, "codes": []}).status_code == 422


def test_merge_codes_preserves_codings_logs_and_undo(demo):
    """Acceptance test 3."""
    client, codes = demo
    emt = codes["EMT"]["id"]
    a = client.post("/api/v1/codes", json={"name": "Pilot fatigue", "definition": "d", "coding_rule": "r", "parent_id": emt}).json()
    b = client.post("/api/v1/codes", json={"name": "Programme weariness", "definition": "d", "coding_rule": "r", "parent_id": emt}).json()
    codes[a["code_key"]] = a; codes[b["code_key"]] = b
    s1 = code_quote(client, codes, "CN-00", "the aggregator segment is young", [a["code_key"]], valence=-1, agency="GOV-CENTRAL")
    s2 = code_quote(client, codes, "CN-00", "Aggregators therefore cannot build a stable business case", [a["code_key"], b["code_key"]])
    client.post("/api/v1/memos", json={"scope": "code", "ref_id": str(a["id"]), "text": "memo of A"})
    client.put("/api/v1/settings/pilot_phase", json={"value": True})
    r = client.post("/api/v1/codes/merge", json={"source_id": a["id"], "target_id": b["id"], "reason": "same phenomenon"})
    assert r.status_code == 200 and r.json()["moved"] == 2
    g1 = client.get(f"/api/v1/segments/{s1['id']}").json()
    assert [c["code_id"] for c in g1["codes"]] == [b["id"]]
    assert g1["codes"][0]["valence"] == -1 and g1["codes"][0]["agency"] == "GOV-CENTRAL"
    g2 = client.get(f"/api/v1/segments/{s2['id']}").json()
    assert [c["code_id"] for c in g2["codes"]] == [b["id"]]          # no duplicate
    flat = {c["id"]: c for c in client.get("/api/v1/codes?include_retired=true").json()["flat"]}
    assert flat[a["id"]]["active"] is False and flat[b["id"]]["active"] is True
    log = client.get("/api/v1/changelog").json()
    merges = [e for e in log if e["action"] == "merge"]
    assert merges and all(e["phase"] == "pilot" for e in merges) and "same phenomenon" in merges[0]["reason"]
    memo = client.get(f"/api/v1/memos?scope=code&ref_id={b['id']}").json()[0]
    assert "memo of A" in memo["text"]
    # undo restores the original state
    r = client.post("/api/v1/undo")
    assert r.json()["ok"]
    flat = {c["id"]: c for c in client.get("/api/v1/codes?include_retired=true").json()["flat"]}
    assert flat[a["id"]]["active"] is True
    g1 = client.get(f"/api/v1/segments/{s1['id']}").json()
    assert [c["code_id"] for c in g1["codes"]] == [a["id"]]
    g2 = client.get(f"/api/v1/segments/{s2['id']}").json()
    assert {c["code_id"] for c in g2["codes"]} == {a["id"], b["id"]}
    assert client.get(f"/api/v1/memos?scope=code&ref_id={b['id']}").json() == []
    assert not [e for e in client.get("/api/v1/changelog").json() if e["action"] == "merge"]
    # redo re-applies
    assert client.post("/api/v1/redo").json()["ok"]
    assert client.get(f"/api/v1/codes?include_retired=true").json()["flat"]
    flat = {c["id"]: c for c in client.get("/api/v1/codes?include_retired=true").json()["flat"]}
    assert flat[a["id"]]["active"] is False


def test_retire_with_reassign_and_delete_segment_undo(demo):
    client, codes = demo
    sub = client.post("/api/v1/codes", json={"name": "Data access", "definition": "d", "coding_rule": "r", "parent_id": codes["F6"]["id"]}).json()
    codes[sub["code_key"]] = sub
    s = code_quote(client, codes, "DE-00", "Data access is the real bottleneck", [sub["code_key"]], valence=-1)
    r = client.post(f"/api/v1/codes/{sub['id']}/retire", json={"reason": "too fine", "reassign": "parent"})
    assert r.status_code == 200 and r.json()["moved"] == 1
    g = client.get(f"/api/v1/segments/{s['id']}").json()
    assert [c["code_key"] for c in g["codes"]] == ["F6"]
    # retired code cannot be assigned
    r = client.post(f"/api/v1/segments/{s['id']}/codes", json={"code_id": sub["id"]})
    assert r.status_code == 422
    # delete segment + undo
    assert client.delete(f"/api/v1/segments/{s['id']}").status_code == 200
    assert client.get(f"/api/v1/segments/{s['id']}").status_code == 404
    client.post("/api/v1/undo")
    assert client.get(f"/api/v1/segments/{s['id']}").status_code == 200
    # reason required
    assert client.patch(f"/api/v1/codes/{codes['F1']['id']}", json={"name": "New name"}).status_code == 422
    assert client.patch(f"/api/v1/codes/{codes['F1']['id']}", json={"color": "#123456"}).status_code == 200


def test_batch_is_atomic_and_undoable_as_one(demo):
    client, codes = demo
    hit = locate(client, "DE-00", "Capital was never the main issue")
    iv = client.get("/api/v1/interviews/DE-00").json()
    conf = next(p for p in iv["paragraphs"] if p["confidential"])
    c = conf["confidential"][0]
    ops = [{"op": "create_segment", "paragraph_id": hit["paragraph_id"], "start_char": hit["start_char"], "end_char": hit["end_char"], "codes": [{"code_id": codes["F6"]["id"]}]},
           {"op": "create_segment", "paragraph_id": conf["id"], "start_char": c["start_char"], "end_char": c["end_char"], "codes": [{"code_id": codes["F6"]["id"]}]}]
    r = client.post("/api/v1/segments/batch", json={"operations": ops})
    assert r.status_code == 422 and "Operation 1" in r.json()["detail"]
    assert client.get("/api/v1/interviews/DE-00").json()["segment_count"] == 0     # nothing written
    ops[1] = {"op": "create_segment", "paragraph_id": hit["paragraph_id"], "start_char": 0, "end_char": 7, "codes": [{"code_id": codes["ROLE"]["id"]}]}
    r = client.post("/api/v1/segments/batch", json={"operations": ops})
    assert r.status_code == 200 and len(r.json()) == 2
    assert client.get("/api/v1/interviews/DE-00").json()["segment_count"] == 2
    client.post("/api/v1/undo")
    assert client.get("/api/v1/interviews/DE-00").json()["segment_count"] == 0


def test_idempotency_key(demo):
    client, codes = demo
    hit = locate(client, "DE-00", "Capital was never the main issue")
    body = {"paragraph_id": hit["paragraph_id"], "start_char": hit["start_char"], "end_char": hit["end_char"], "codes": [{"code_id": codes["F6"]["id"]}]}
    r1 = client.post("/api/v1/segments", json=body, headers={"Idempotency-Key": "k1"})
    r2 = client.post("/api/v1/segments", json=body, headers={"Idempotency-Key": "k1"})
    assert r1.json()["id"] == r2.json()["id"] and r2.headers["X-Idempotent-Replay"] == "true"
    assert client.get("/api/v1/interviews/DE-00").json()["segment_count"] == 1


def test_revision_counter_and_last_used(demo):
    client, codes = demo
    r0 = client.get("/api/v1/revision").json()["revision"]
    code_quote(client, codes, "DE-00", "Capital was never the main issue", ["F6"])
    assert client.get("/api/v1/revision").json()["revision"] > r0
    assert client.get("/api/v1/codes").json()["last_used_code_id"] == codes["F6"]["id"]


def test_interactions_and_confidential_marking(demo):
    client, codes = demo
    s = code_quote(client, codes, "DE-00", "the regulatory gap blocks market formation", ["F4", "INT"], valence=-1)
    r = client.post("/api/v1/interactions", json={"segment_id": s["id"], "from_code_id": codes["STR-I"]["id"], "to_code_id": codes["F4"]["id"], "mechanism": "blocking", "condition": "no baseline rules -> no market"})
    assert r.status_code == 201 and r.json()["country"] == "DE" and r.json()["interview_id"] == "DE-00"
    v = client.get("/api/v1/analysis/interactions").json()
    assert len(v["interactions"]) == 1 and "flowchart" in v["mermaid"]
    # manual confidential marking over the coded segment requires confirmation, then deletes it (undoable)
    r = client.post("/api/v1/confidential", json={"paragraph_id": s["paragraph_id"], "start_char": s["start_char"], "end_char": s["end_char"]})
    assert r.status_code == 422
    r = client.post("/api/v1/confidential", json={"paragraph_id": s["paragraph_id"], "start_char": s["start_char"], "end_char": s["end_char"], "delete_overlapping": True})
    assert r.status_code == 201
    assert client.get(f"/api/v1/segments/{s['id']}").status_code == 404
    it = client.get("/api/v1/interactions").json()[0]
    assert it["segment_id"] is None                     # interaction kept, unlinked
    client.post("/api/v1/undo")
    assert client.get(f"/api/v1/segments/{s['id']}").status_code == 200
    assert client.get("/api/v1/interactions").json()[0]["segment_id"] == s["id"]


def test_kappa_formula():
    assert kappa(10, 0, 0, 10) == 1.0
    assert kappa(0, 5, 5, 0) == -1.0
    assert kappa(0, 0, 0, 10) == 1.0                    # both never present -> perfect agreement
    assert abs(kappa(20, 5, 10, 15) - 0.4) < 1e-9        # po=0.7, pe=0.5


def test_recoding_pass_and_reliability_report(demo):
    """Acceptance test 9 (fixture: hand-computed)."""
    client, codes = demo
    # pass 1
    code_quote(client, codes, "DE-00", "The technical side works.", ["F1"])
    code_quote(client, codes, "DE-00", "Capital was never the main issue", ["F6"])
    code_quote(client, codes, "DE-00", "provincial demand-response pilots in Jiangsu", ["ML"])
    p2 = client.post("/api/v1/interviews/DE-00/passes").json()
    assert p2["name"].startswith("recode-")
    assert client.post("/api/v1/interviews/DE-00/passes").status_code == 422     # only one open pass
    # pass 2 view hides pass-1 codings
    view = client.get(f"/api/v1/interviews/DE-00?pass_id={p2['id']}").json()
    assert view["segment_count"] == 0
    code_quote(client, codes, "DE-00", "The technical side works.", ["F1"], coding_pass_id=p2["id"])          # agree
    code_quote(client, codes, "DE-00", "Capital was never the main issue", ["F6", "BAR"], coding_pass_id=p2["id"])  # partial
    # ML paragraph not coded in pass 2 -> disagreement
    rep = client.get("/api/v1/reliability/DE-00").json()
    assert rep["paragraphs"] == 9
    assert rep["agreeing_paragraphs"] == 7 and rep["percent_agreement"] == 77.8
    per = {r["code_key"]: r for r in rep["per_code"]}
    assert per["F1"]["kappa"] == 1.0 and per["F6"]["kappa"] == 1.0
    assert per["BAR"]["both"] == 0 and per["BAR"]["only_pass2"] == 1 and per["BAR"]["kappa"] == 0.0
    assert per["ML"]["only_pass1"] == 1 and per["ML"]["kappa"] == 0.0
    assert len(rep["disagreements"]) == 2
    d = rep["disagreements"][0]
    r = client.put(f"/api/v1/reliability/{p2['id']}/notes/{d['paragraph_id']}", json={"note": "BAR forgotten in pass 1"})
    assert r.status_code == 200
    assert client.get("/api/v1/reliability/DE-00").json()["disagreements"][0]["note"] == "BAR forgotten in pass 1"
    # finish -> locked -> no more codings in pass 2; primary untouched
    assert client.post(f"/api/v1/passes/{p2['id']}/finish").json()["locked"] == 1
    r = client.post("/api/v1/segments", json={"paragraph_id": view["paragraphs"][1]["id"], "start_char": 0, "end_char": 5, "codes": [{"code_id": codes['F1']['id']}], "coding_pass_id": p2["id"]})
    assert r.status_code == 422 and "locked" in r.json()["detail"]
    assert client.get("/api/v1/interviews/DE-00").json()["segment_count"] == 3


def test_crosstab_matches_hand_checked_fixture(demo):
    """Acceptance test 5: crosstab functions x country against a hand-built fixture."""
    client, codes = demo
    code_quote(client, codes, "DE-00", "The technical side works.", ["F1"], valence=1)
    code_quote(client, codes, "DE-00", "Capital was never the main issue", ["F6"], valence=1)
    code_quote(client, codes, "DE-00", "Data access is the real bottleneck", ["F6", "BAR"], valence=-1)
    code_quote(client, codes, "CN-00", "The market is not really a market yet", ["F4", "BAR"], valence=-1)
    code_quote(client, codes, "CN-00", "Trust has to be earned event by event", ["F5"], valence=0)
    code_quote(client, codes, "CN-00", "We learn mostly by doing", ["F1"], valence=1)
    ct = client.get("/api/v1/analysis/crosstab?attribute=country").json()
    rows = {r["code_key"]: r["cells"] for r in ct["rows"]}
    assert rows["F1"]["DE"]["segments"] == 1 and rows["F1"]["CN"]["segments"] == 1
    assert rows["F1"]["DE"]["valence"] == {"-1": 0, "0": 0, "1": 1}
    assert rows["F6"]["DE"]["segments"] == 2 and rows["F6"]["DE"]["interviews"] == 1
    assert rows["F6"]["DE"]["valence"] == {"-1": 1, "0": 0, "1": 1}
    assert rows["F6"]["CN"]["segments"] == 0
    assert rows["F4"]["CN"]["segments"] == 1 and rows["F4"]["CN"]["valence"]["-1"] == 1
    assert rows["F5"]["CN"]["valence"]["0"] == 1
    assert rows["BAR"]["DE"]["segments"] == 1 and rows["BAR"]["CN"]["segments"] == 1
    for k in ("F2", "F3", "F7"):
        assert rows[k]["DE"]["segments"] == 0 and rows[k]["CN"]["segments"] == 0
    ag = client.get("/api/v1/analysis/agency").json()
    f6 = next(r for r in ag["rows"] if r["code_key"] == "F6")
    assert f6["cells"]["DE"]["unset"] == 2
    bars = client.get("/api/v1/analysis/barriers").json()
    assert list(bars["groups"]["DE"].keys()) == ["Resource Mobilization"]
    assert list(bars["groups"]["CN"].keys()) == ["Market Formation"]
