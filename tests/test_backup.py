"""Backup rotation, restore, integrity and crash-safety (sections 7, 9.4, 9.8)."""
import datetime as dt
import sqlite3
from pathlib import Path

from app.services.backup import select_to_keep, backup_filename
from app.main import Project, CorruptDatabase
from conftest import BASE, SAMPLES, import_md, code_quote


def _names(days_back: list[float], now: dt.datetime) -> list[str]:
    return [backup_filename(now - dt.timedelta(days=d)) for d in days_back]


def test_rotation_policy():
    now = dt.datetime(2026, 9, 23, 12, 0, 0)
    # 3 per day for 100 days
    days = [d + h / 24 for d in range(100) for h in (1, 8, 16)]
    names = _names(days, now)
    keep = select_to_keep(names, now=now, min_keep=20)
    by_age = {n: now - dt.datetime.strptime(n[8:25], "%Y-%m-%d_%H%M%S") for n in names}
    last7 = [n for n in names if by_age[n] <= dt.timedelta(days=7)]
    assert all(n in keep for n in last7)                      # keep everything from the last 7 days
    mid = [n for n in keep if dt.timedelta(days=7) < by_age[n] <= dt.timedelta(days=60)]
    mid_days = {n[8:18] for n in mid}
    assert len(mid) == len(mid_days)                          # one per day
    old = [n for n in keep if by_age[n] > dt.timedelta(days=60)]
    weeks = {dt.datetime.strptime(n[8:25], "%Y-%m-%d_%H%M%S").isocalendar()[:2] for n in old}
    assert len(old) == len(weeks) and len(old) >= 5           # one per week
    # never fewer than 20
    few = _names([100 + i for i in range(25)], now)
    assert len(select_to_keep(few, now=now, min_keep=20)) >= 20


def test_backup_created_on_start_and_import_and_manual(project):
    client, p = project
    n0 = len(p.backups.list_backups())
    assert n0 >= 1                                             # on start
    assert import_md(client, SAMPLES / "DE-00.md").status_code == 200
    assert len(p.backups.list_backups()) >= n0 + 1             # after import
    r = client.post("/api/v1/backup")
    assert r.status_code == 200 and r.json()["filename"].startswith("project_")
    # secondary folder copy
    sec = p.base_dir if False else p.backups_dir.parent / "secondary"
    client.put("/api/v1/settings/secondary_backup_dir", json={"value": str(sec)})
    r = client.post("/api/v1/backup")
    assert (sec / r.json()["filename"]).exists()


def test_delete_project_file_and_restore_from_backup(project, tmp_path):
    """Acceptance test 8: delete the project file, restore from backup, everything intact."""
    client, p = project
    assert import_md(client, SAMPLES / "DE-00.md").status_code == 200
    codes = {c["code_key"]: c for c in client.get("/api/v1/codes").json()["flat"]}
    seg = code_quote(client, codes, "DE-00", "The technical side works.", ["F1"], valence=1)
    fn = client.post("/api/v1/backup").json()["filename"]
    # simulate loss of the working file
    p.db.close()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p.db_path) + suffix)
        if f.exists():
            f.unlink()
    assert not p.db_path.exists()
    p.db = __import__("app.db", fromlist=["Database"]).Database(p.db_path)
    p.db.migrate()
    r = client.post("/api/v1/restore", json={"filename": fn})
    assert r.status_code == 200, r.text
    iv = client.get("/api/v1/interviews/DE-00").json()
    segs = [s for pp in iv["paragraphs"] for s in pp["segments"]]
    assert len(segs) == 1 and segs[0]["id"] == seg["id"] and segs[0]["codes"][0]["valence"] == 1
    assert client.get("/api/v1/undo/state").json()["can_undo"] is False   # undo history cleared


def test_crash_mid_coding_keeps_last_completed_action(tmp_path):
    """Acceptance test 4: kill the process mid-coding; reopen; last completed action present."""
    from app.db import Database
    from app.seed import seed_codebook
    from app.services import core
    from app.services.importer import parse_transcript
    data = tmp_path / "data"
    db = Database(data / "project.db")
    db.migrate(); seed_codebook(db)
    core.import_transcript(db, parse_transcript((SAMPLES / "DE-00.md").read_text(encoding="utf-8"), "DE-00.md"), "DE-00.md")
    f1 = db.one("SELECT id FROM codes WHERE code_key='F1'")["id"]
    hit = core.locate(db, "DE-00", "The technical side works.")[0]
    with db.write("code") as tx:
        core.create_segment(tx, hit["paragraph_id"], hit["start_char"], hit["end_char"], [{"code_id": f1}])
    # second action "in flight": transaction opened, never committed -> process dies
    db.conn.execute("BEGIN IMMEDIATE")
    db.conn.execute("INSERT INTO segments(paragraph_id,start_char,end_char,text_snapshot,coding_pass_id) VALUES (?,?,?,?,?)",
                    (hit["paragraph_id"], 0, 5, "x", 1))
    del db  # connection dropped without COMMIT (simulated kill)
    db2 = Database(data / "project.db")
    assert db2.integrity_check() == "ok"
    assert db2.scalar("SELECT COUNT(*) FROM segments") == 1
    assert db2.one("SELECT text_snapshot FROM segments")["text_snapshot"] == "The technical side works."


def test_corrupt_db_refused_and_good_backup_found(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    backups = tmp_path / "backups"; backups.mkdir()
    # a valid backup
    good = sqlite3.connect(str(backups / backup_filename()))
    good.execute("CREATE TABLE t(x)"); good.commit(); good.close()
    (data / "project.db").write_bytes(b"this is not a database at all" * 100)
    try:
        Project(BASE, data_dir=data, backups_dir=backups, exports_dir=tmp_path / "exports")
        assert False, "corrupt file must be refused"
    except CorruptDatabase:
        pass
    from app.services.backup import BackupManager
    assert BackupManager(data / "project.db", backups).latest_good_backup() is not None
