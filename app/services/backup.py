"""Backup & integrity.

* backup of data/project.db on start, shutdown, every N minutes if changed,
  after every import, before every migration or restore
* filename project_YYYY-MM-DD_HHMMSS.db
* rotation: keep everything from the last 7 days, then one per day for 60 days,
  then one per week; never fewer than the most recent 20
* optional secondary folder (a locally synced folder); the app makes no network calls
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("qda.backup")

FILE_RE = re.compile(r"^project_(\d{4}-\d{2}-\d{2})_(\d{6})\.db$")


def backup_filename(ts: _dt.datetime | None = None) -> str:
    ts = ts or _dt.datetime.now()
    return f"project_{ts.strftime('%Y-%m-%d_%H%M%S')}.db"


def parse_backup_time(name: str) -> _dt.datetime | None:
    m = FILE_RE.match(name)
    if not m:
        return None
    return _dt.datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S")


def select_to_keep(names: list[str], now: _dt.datetime | None = None, min_keep: int = 20) -> set[str]:
    """Pure rotation policy. Returns the set of filenames to keep."""
    now = now or _dt.datetime.now()
    dated = [(parse_backup_time(n), n) for n in names]
    dated = [(t, n) for t, n in dated if t is not None]
    dated.sort(reverse=True)  # newest first
    keep: set[str] = set()
    # never fewer than the most recent min_keep
    for _, n in dated[:min_keep]:
        keep.add(n)
    day_seen: set[str] = set()
    week_seen: set[str] = set()
    for t, n in dated:
        age = now - t
        if age <= _dt.timedelta(days=7):
            keep.add(n)
        elif age <= _dt.timedelta(days=60):
            key = t.strftime("%Y-%m-%d")
            if key not in day_seen:
                day_seen.add(key)
                keep.add(n)
        else:
            iso = t.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
            if key not in week_seen:
                week_seen.add(key)
                keep.add(n)
    return keep


class BackupManager:
    def __init__(self, db_path: Path, backups_dir: Path,
                 secondary_dir_getter: Callable[[], str] | None = None,
                 interval_getter: Callable[[], int] | None = None):
        self.db_path = Path(db_path)
        self.backups_dir = Path(backups_dir)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.secondary_dir_getter = secondary_dir_getter or (lambda: "")
        self.interval_getter = interval_getter or (lambda: 15)
        self._timer_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_backup: _dt.datetime | None = None
        self.last_error: str | None = None

    # -- core ------------------------------------------------------------
    def backup_now(self, source_conn: sqlite3.Connection | None = None, reason: str = "manual") -> Path:
        """Create a consistent copy of the project database."""
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now()
        target = self.backups_dir / backup_filename(ts)
        # avoid same-second collision
        n = 0
        while target.exists():
            n += 1
            target = self.backups_dir / backup_filename(ts + _dt.timedelta(seconds=n))
        if source_conn is not None:
            dest = sqlite3.connect(str(target))
            try:
                source_conn.backup(dest)
            finally:
                dest.close()
        else:
            src = sqlite3.connect(str(self.db_path))
            dest = sqlite3.connect(str(target))
            try:
                src.backup(dest)
            finally:
                dest.close()
                src.close()
        self.last_backup = ts
        self.last_error = None
        log.info("backup (%s) -> %s", reason, target.name)
        self.rotate()
        self._copy_secondary(target)
        return target

    def _copy_secondary(self, target: Path) -> None:
        sec = (self.secondary_dir_getter() or "").strip()
        if not sec:
            return
        try:
            sec_path = Path(sec)
            sec_path.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, sec_path / target.name)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"secondary backup failed: {exc}"
            log.warning(self.last_error)

    def rotate(self) -> list[str]:
        names = [p.name for p in self.backups_dir.glob("project_*.db")]
        keep = select_to_keep(names)
        removed = []
        for n in names:
            if n not in keep:
                try:
                    (self.backups_dir / n).unlink()
                    removed.append(n)
                except OSError as exc:
                    log.warning("could not remove old backup %s: %s", n, exc)
        return removed

    def list_backups(self) -> list[dict]:
        out = []
        for p in sorted(self.backups_dir.glob("project_*.db"), reverse=True):
            st = p.stat()
            out.append({"filename": p.name, "size": st.st_size,
                        "created_at": _dt.datetime.fromtimestamp(st.st_mtime).replace(microsecond=0).isoformat()})
        return out

    def latest_good_backup(self) -> Path | None:
        for p in sorted(self.backups_dir.glob("project_*.db"), reverse=True):
            try:
                c = sqlite3.connect(str(p))
                ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                c.close()
                if ok:
                    return p
            except sqlite3.Error:
                continue
        return None

    # -- timer -----------------------------------------------------------
    def start_timer(self, is_dirty: Callable[[], bool], do_backup: Callable[[], None]) -> None:
        def loop() -> None:
            while not self._timer_stop.is_set():
                try:
                    minutes = max(1, int(self.interval_getter() or 15))
                except (TypeError, ValueError):
                    minutes = 15
                if self._timer_stop.wait(minutes * 60):
                    break
                try:
                    if is_dirty():
                        do_backup()
                except Exception as exc:  # noqa: BLE001
                    log.exception("periodic backup failed: %s", exc)
        self._thread = threading.Thread(target=loop, name="backup-timer", daemon=True)
        self._thread.start()

    def stop_timer(self) -> None:
        self._timer_stop.set()
