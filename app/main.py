"""FastAPI application factory + project state (db, backups, paths)."""
from __future__ import annotations

import json
import logging
import logging.handlers
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .db import SCHEMA_VERSION, Database, now_iso
from .seed import seed_codebook
from .services.backup import BackupManager

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

log = logging.getLogger("qda")

SHORTCUTS = {
    "1": "F1", "2": "F1b", "3": "F2", "4": "F3", "5": "F4", "6": "F5", "7": "F6", "8": "F7",
    "s": "STR", "c": "CTX", "b": "BAR", "i": "INT", "m": "ML", "r": "ROLE", "e": "EMT",
}


class CorruptDatabase(RuntimeError):
    pass


class Project:
    """Everything that belongs to one project folder (data/, backups/, exports/)."""

    def __init__(self, base_dir: Path, data_dir: Path | None = None, backups_dir: Path | None = None,
                 exports_dir: Path | None = None):
        self.base_dir = Path(base_dir)
        self.data_dir = Path(data_dir) if data_dir else self.base_dir / "data"
        self.backups_dir = Path(backups_dir) if backups_dir else self.base_dir / "backups"
        self.exports_dir = Path(exports_dir) if exports_dir else self.base_dir / "exports"
        self.version = __version__
        for d in (self.data_dir, self.backups_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._setup_logging()
        self.db_path = self.data_dir / "project.db"
        self.backups = BackupManager(self.db_path, self.backups_dir,
                                     secondary_dir_getter=lambda: self._setting("secondary_backup_dir"),
                                     interval_getter=lambda: int(self._setting("backup_interval_min") or 15))
        self.corrupt: str | None = None
        self.db = self._open_db()

    def _setup_logging(self) -> None:
        root = logging.getLogger("qda")
        if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
            return
        root.setLevel(logging.INFO)
        h = logging.handlers.RotatingFileHandler(self.data_dir / "app.log", maxBytes=2_000_000, backupCount=3,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)

    def _setting(self, key: str) -> str:
        try:
            return str(self.db.setting(key, "") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _open_db(self) -> Database:
        existed = self.db_path.exists()
        if existed:
            # integrity check before touching anything
            probe = sqlite3.connect(str(self.db_path))
            try:
                result = probe.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                result = f"error: {exc}"
            finally:
                probe.close()
            if result != "ok":
                self.corrupt = result
                log.error("integrity check failed: %s", result)
                raise CorruptDatabase(result)
        db = Database(self.db_path)
        upgrade = False
        if existed and db.schema_version() < 1:
            pass  # empty file, nothing to back up
        elif existed and db.schema_version() < SCHEMA_VERSION:
            target = self.backups.backup_now(db.conn, "before migration")
            upgrade = True
            print(f"Schema upgrade {db.schema_version()} -> {SCHEMA_VERSION}; backup written: {target.name}")
        applied = db.migrate()
        if applied:
            log.info("migrations applied: %s", applied)
            if upgrade:
                for name in applied:  # e.g. '0005_agency_values (3 segment_codes row(s) remapped)'
                    print(f"Migration applied: {name}")
        seed_codebook(db)
        return db

    # -- backup / restore -------------------------------------------------
    def backup(self, reason: str = "manual") -> Path:
        target = self.backups.backup_now(self.db.conn, reason)
        self.db.dirty_since_backup = False
        return target

    def restore(self, filename: str) -> dict[str, Any]:
        src = self.backups_dir / Path(filename).name
        if not src.exists():
            raise FileNotFoundError(f"Backup {filename} not found in {self.backups_dir}.")
        probe = sqlite3.connect(str(src))
        try:
            ok = probe.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            probe.close()
        if ok != "ok":
            raise ValueError(f"Backup {filename} fails the integrity check: {ok}")
        safety = self.backup("before restore")
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy2(src, self.db_path)
        self.db = Database(self.db_path)
        self.db.migrate()
        seed_codebook(self.db)
        self.db.clear_undo()
        log.info("restored from %s (safety backup %s)", src.name, safety.name)
        return {"restored_from": src.name, "safety_backup": safety.name, "revision": self.db.revision()}

    def shortcuts(self) -> dict[str, str]:
        return dict(SHORTCUTS)

    def close(self) -> None:
        try:
            self.backup("shutdown")
        except Exception as exc:  # noqa: BLE001
            log.exception("shutdown backup failed: %s", exc)
        self.backups.stop_timer()
        self.db.close()


def create_app(base_dir: Path | None = None, data_dir: Path | None = None, backups_dir: Path | None = None,
               exports_dir: Path | None = None, start_timer: bool = True) -> FastAPI:
    base_dir = Path(base_dir) if base_dir else BASE_DIR
    project = Project(base_dir, data_dir, backups_dir, exports_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        project.backup("start")
        if start_timer:
            project.backups.start_timer(lambda: project.db.dirty_since_backup, lambda: project.backup("periodic"))
        log.info("app started (project %s)", project.db_path)
        yield
        project.close()
        log.info("app stopped")

    app = FastAPI(title="Local QDA Tool — TIS Interview Coding", version=__version__, lifespan=lifespan,
                  description="Local, single-user qualitative coding tool for a Technological Innovation System "
                              "analysis.",
                  docs_url="/docs", openapi_url="/openapi.json")
    app.state.project = project
    app.include_router(router)

    @app.middleware("http")
    async def revision_and_idempotency(request: Request, call_next):
        key = request.headers.get("Idempotency-Key")
        if key and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            row = project.db.one("SELECT status_code, response_json FROM idempotency_keys WHERE key=?", (key,))
            if row:
                resp = Response(content=row["response_json"], status_code=row["status_code"],
                                media_type="application/json")
                resp.headers["X-Revision"] = str(project.db.revision())
                resp.headers["X-Idempotent-Replay"] = "true"
                return resp
        response = await call_next(request)
        if key and request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 500:
            body = b""
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                body += chunk
            try:
                with project.db.write(record_undo=False) as tx:
                    tx.execute("INSERT OR IGNORE INTO idempotency_keys(key, status_code, response_json, created_at) "
                               "VALUES (?,?,?,?)", (key, response.status_code, body.decode("utf-8"), now_iso()))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not store idempotency key: %s", exc)
            response = Response(content=body, status_code=response.status_code,
                                headers={k: v for k, v in response.headers.items()
                                         if k.lower() not in ("content-length",)},
                                media_type=response.media_type)
        try:
            response.headers["X-Revision"] = str(project.db.revision())
        except Exception:  # noqa: BLE001
            pass
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=500, content={"detail": f"Internal error: {exc}"})

    return app
