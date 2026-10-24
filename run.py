#!/usr/bin/env python
"""Entry point.

    python run.py              start the app and open http://127.0.0.1:8765
    python run.py --no-browser start without opening a browser
    python run.py --port=8766  start on another port (e.g. a test copy next to a running instance)
    python run.py backup       create a backup now
    python run.py export-all   write all exports to exports/<timestamp>/
    python run.py demo         import samples/DE-00.md and samples/CN-00.md (skips existing ids)
    python run.py test         run the test suite (pytest)

Opening an older project file runs the pending schema migrations (backup first) and prints them.
"""
from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8765


def _project():
    from app.main import CorruptDatabase, Project
    try:
        return Project(BASE)
    except CorruptDatabase as exc:
        print(f"ERROR: data/project.db failed the integrity check ({exc}).")
        from app.services.backup import BackupManager
        bm = BackupManager(BASE / "data" / "project.db", BASE / "backups")
        good = bm.latest_good_backup()
        if good:
            print(f"Latest good backup: {good}")
            print("Restore it with:  python run.py restore " + good.name)
        else:
            print("No good backup found in backups/.")
        sys.exit(2)


def cmd_serve(open_browser: bool = True, port: int = PORT) -> None:
    import uvicorn
    from app.main import CorruptDatabase, create_app
    try:
        app = create_app(BASE)
    except CorruptDatabase:
        _project()  # prints the restore hint and exits
        return
    url = f"http://{HOST}:{port}"
    print(f"Local QDA tool running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")


def cmd_backup() -> None:
    p = _project()
    target = p.backup("cli")
    print(f"Backup written: {target}")
    p.db.close()


def cmd_export_all() -> None:
    from app.services.export import run_export
    p = _project()
    result = run_export(p.db, p.exports_dir)
    print(f"Exported {len(result['files'])} files to {result['path']}")
    for w in result["warnings"]:
        print("WARNING:", w)
    p.db.close()


def cmd_restore(filename: str) -> None:
    from app.main import Project
    from app.services.backup import BackupManager
    import shutil
    src = BASE / "backups" / Path(filename).name
    if not src.exists():
        print(f"Backup not found: {src}")
        sys.exit(2)
    db_path = BASE / "data" / "project.db"
    bm = BackupManager(db_path, BASE / "backups")
    if db_path.exists():
        try:
            safety = bm.backup_now(None, "before restore (cli)")
            print(f"Safety copy of the current file: {safety.name}")
        except Exception as exc:  # noqa: BLE001
            broken = db_path.with_name("project.corrupt.db")
            shutil.copy2(db_path, broken)
            print(f"Current file could not be backed up ({exc}); copied to {broken.name}")
    for suffix in ("", "-wal", "-shm"):
        pth = Path(str(db_path) + suffix)
        if pth.exists():
            pth.unlink()
    shutil.copy2(src, db_path)
    print(f"Restored {src.name} -> {db_path}")
    p = Project(BASE)
    print("Integrity check:", p.db.integrity_check())
    p.db.close()


def cmd_demo() -> None:
    from app.services import core
    from app.services.importer import parse_transcript
    p = _project()
    for name in ("DE-00.md", "CN-00.md"):
        path = BASE / "samples" / name
        parsed = parse_transcript(path.read_text(encoding="utf-8"), name)
        if p.db.one("SELECT 1 FROM interviews WHERE id=?", (parsed.meta["id"],)):
            print(f"{parsed.meta['id']} already imported, skipping.")
            continue
        report = core.import_transcript(p.db, parsed, name)
        print(f"Imported {report['id']}: {report['paragraphs']} paragraphs ({report['timestamps']} with timestamp), "
              f"{report['confidential_ranges']} confidential, {report['en_original_ranges']} EN-original, "
              f"{report['no_quote_ranges']} quotation-blocked range(s).")
    p.backup("after demo import")
    p.db.close()


def cmd_test() -> int:
    import pytest
    return pytest.main(["-q", str(BASE / "tests")])


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    cmd = args[0] if args else "serve"
    port = PORT
    for f in flags:
        if f.startswith("--port="):
            try:
                port = int(f.split("=", 1)[1])
            except ValueError:
                print("usage: python run.py --port=8766")
                sys.exit(1)
    if cmd == "serve":
        cmd_serve(open_browser="--no-browser" not in flags, port=port)
    elif cmd == "backup":
        cmd_backup()
    elif cmd == "export-all":
        cmd_export_all()
    elif cmd == "demo":
        cmd_demo()
    elif cmd == "restore":
        if len(args) < 2:
            print("usage: python run.py restore <backup filename>")
            sys.exit(1)
        cmd_restore(args[1])
    elif cmd == "test":
        sys.exit(cmd_test())
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
