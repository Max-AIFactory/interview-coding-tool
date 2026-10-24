"""SQLite access layer.

Design goals:
* one serialized write path (RLock + one transaction per request, WAL mode)
* every tracked write records a row-level before/after journal into ``undo_log``
  so every action can be undone and redone
* a project-wide revision counter (settings.revision) bumps on every write
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("qda.db")

SCHEMA_VERSION = 5

# Agency value list; '' / NULL = unset.
AGENCY_VALUES: tuple[str, ...] = ("GOV-CENTRAL", "GOV-PROV", "REGULATOR", "GRID", "SOE", "MARKET", "ASSOC",
                                  "RESEARCH", "MULTI")
# Mapping of the pre-v2.1 values applied by migration 0005.
LEGACY_AGENCY_MAP: dict[str, str | None] = {
    "public": "GOV-CENTRAL", "private": "MARKET", "co-steering": "MULTI", "status quo/unclear": None}
_AGENCY_CHECK = "agency IS NULL OR agency IN (" + ", ".join(f"'{a}'" for a in AGENCY_VALUES) + ")"

# Tables whose rows are journaled for undo/redo.  Value = primary-key columns.
TRACKED_TABLES: dict[str, tuple[str, ...]] = {
    "segments": ("id",),
    "segment_codes": ("segment_id", "code_id"),
    "interactions": ("id",),
    "confidential_ranges": ("id",),
    "memos": ("id",),
    "memo_versions": ("id",),
    "codes": ("id",),
    "code_changelog": ("id",),
    "coding_passes": ("id",),
    "interviews": ("id",),
    "reliability_notes": ("id",),
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS interviews (
    id TEXT PRIMARY KEY,
    country TEXT NOT NULL CHECK (country IN ('DE','CN')),
    role_category TEXT NOT NULL,
    role_label TEXT NOT NULL,
    date TEXT NOT NULL,
    duration_min INTEGER,
    language_original TEXT,
    interview_mode TEXT,
    pilot INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    source_filename TEXT,
    imported_at TEXT,
    status TEXT NOT NULL DEFAULT 'not-started' CHECK (status IN ('not-started','in-progress','done'))
);
CREATE TABLE IF NOT EXISTS paragraphs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id TEXT NOT NULL REFERENCES interviews(id),
    idx INTEGER NOT NULL,
    para_no INTEGER,
    speaker TEXT NOT NULL CHECK (speaker IN ('I','R')),
    text TEXT NOT NULL,
    timestamp TEXT
);
CREATE INDEX IF NOT EXISTS ix_paragraphs_interview ON paragraphs(interview_id, idx);
CREATE TABLE IF NOT EXISTS confidential_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id),
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('tag','manual'))
);
CREATE INDEX IF NOT EXISTS ix_conf_par ON confidential_ranges(paragraph_id);
CREATE TABLE IF NOT EXISTS en_original_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id),
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_en_par ON en_original_ranges(paragraph_id);
CREATE TABLE IF NOT EXISTS no_quote_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id),
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_nq_par ON no_quote_ranges(paragraph_id);
CREATE TABLE IF NOT EXISTS codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES codes(id),
    type TEXT NOT NULL CHECK (type IN ('function','structural','context','crosscutting','learning','role','inductive')),
    definition TEXT NOT NULL DEFAULT '',
    coding_rule TEXT NOT NULL DEFAULT '',
    anchor_example TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#888888',
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    retired_at TEXT
);
CREATE TABLE IF NOT EXISTS coding_passes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id TEXT NOT NULL REFERENCES interviews(id),
    name TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    locked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_passes_interview ON coding_passes(interview_id);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id),
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    text_snapshot TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    quote_candidate INTEGER NOT NULL DEFAULT 0,
    coding_pass_id INTEGER NOT NULL REFERENCES coding_passes(id),
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_segments_par ON segments(paragraph_id);
CREATE INDEX IF NOT EXISTS ix_segments_pass ON segments(coding_pass_id);
CREATE TABLE IF NOT EXISTS segment_codes (
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    code_id INTEGER NOT NULL REFERENCES codes(id),
    valence INTEGER NOT NULL DEFAULT 0 CHECK (valence IN (-1,0,1)),
    agency TEXT CHECK (__AGENCY_CHECK__),
    PRIMARY KEY (segment_id, code_id)
);
CREATE INDEX IF NOT EXISTS ix_segcodes_code ON segment_codes(code_id);
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id INTEGER REFERENCES segments(id),
    interview_id TEXT REFERENCES interviews(id),
    from_code_id INTEGER NOT NULL REFERENCES codes(id),
    to_code_id INTEGER NOT NULL REFERENCES codes(id),
    mechanism TEXT NOT NULL CHECK (mechanism IN ('inducing','blocking')),
    country TEXT CHECK (country IS NULL OR country IN ('DE','CN')),
    description TEXT NOT NULL DEFAULT '',
    condition TEXT NOT NULL DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL CHECK (scope IN ('code','interview','segment','general')),
    ref_id TEXT,
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS memo_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id INTEGER NOT NULL REFERENCES memos(id),
    text TEXT NOT NULL,
    saved_at TEXT
);
CREATE TABLE IF NOT EXISTS code_changelog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id INTEGER REFERENCES codes(id),
    timestamp TEXT,
    action TEXT NOT NULL CHECK (action IN ('create','rename','edit','merge','split','retire','reparent')),
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    reason TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL CHECK (phase IN ('pilot','main'))
);
CREATE TABLE IF NOT EXISTS anonymization_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL UNIQUE,
    replacement TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_json TEXT NOT NULL,
    inverse_json TEXT NOT NULL,
    timestamp TEXT,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reliability_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id INTEGER NOT NULL REFERENCES coding_passes(id),
    paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id),
    note TEXT NOT NULL DEFAULT '',
    UNIQUE (pass_id, paragraph_id)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT
);
""".replace("__AGENCY_CHECK__", _AGENCY_CHECK)


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


class Tx:
    """A recording transaction.  All writes to tracked tables must go through
    insert/update/delete so the inverse can be journaled."""

    def __init__(self, conn: sqlite3.Connection, record: bool = True):
        self.conn = conn
        self.record = record
        self.changes: list[dict[str, Any]] = []
        self.description = ""

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _pk_where(table: str, pk: dict[str, Any]) -> tuple[str, list[Any]]:
        cols = TRACKED_TABLES[table]
        return " AND ".join(f"{c} = ?" for c in cols), [pk[c] for c in cols]

    def _fetch(self, table: str, pk: dict[str, Any]) -> dict[str, Any] | None:
        where, params = self._pk_where(table, pk)
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
        return dict(row) if row else None

    # -- tracked writes --------------------------------------------------
    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        cols = list(row.keys())
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        cur = self.conn.execute(sql, [row[c] for c in cols])
        if table in TRACKED_TABLES:
            pkcols = TRACKED_TABLES[table]
            if pkcols == ("id",) and "id" not in row:
                pk = {"id": cur.lastrowid}
            else:
                pk = {c: row[c] for c in pkcols}
            after = self._fetch(table, pk)
            self.changes.append({"op": "insert", "table": table, "pk": pk, "before": None, "after": after})
            return after or dict(row)
        d = dict(row)
        if "id" not in d:
            d["id"] = cur.lastrowid
        return d

    def update(self, table: str, pk: dict[str, Any] | Any, changes: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(pk, dict):
            pk = {"id": pk}
        if not changes:
            return self._fetch(table, pk) if table in TRACKED_TABLES else None
        tracked = table in TRACKED_TABLES
        before = self._fetch(table, pk) if tracked else None
        if tracked:
            where, params = self._pk_where(table, pk)
        else:
            where, params = " AND ".join(f"{k} = ?" for k in pk), list(pk.values())
        sets = ", ".join(f"{k} = ?" for k in changes)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", list(changes.values()) + params)
        if tracked:
            after = self._fetch(table, pk)
            self.changes.append({"op": "update", "table": table, "pk": pk, "before": before, "after": after})
            return after
        return None

    def delete(self, table: str, pk: dict[str, Any] | Any) -> None:
        if not isinstance(pk, dict):
            pk = {"id": pk}
        if table in TRACKED_TABLES:
            before = self._fetch(table, pk)
            if before is None:
                return
            where, params = self._pk_where(table, pk)
            self.conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            self.changes.append({"op": "delete", "table": table, "pk": pk, "before": before, "after": None})
        else:
            where = " AND ".join(f"{k} = ?" for k in pk)
            self.conn.execute(f"DELETE FROM {table} WHERE {where}", list(pk.values()))

    # -- untracked / raw -------------------------------------------------
    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def apply_changes(conn: sqlite3.Connection, changes: list[dict[str, Any]], direction: str) -> None:
    """direction = 'inverse' (undo) or 'forward' (redo)."""
    seq = list(reversed(changes)) if direction == "inverse" else list(changes)
    for ch in seq:
        table, pk = ch["table"], ch["pk"]
        target = ch["before"] if direction == "inverse" else ch["after"]
        where = " AND ".join(f"{c} = ?" for c in pk)
        params = list(pk.values())
        if target is None:
            conn.execute(f"DELETE FROM {table} WHERE {where}", params)
        else:
            exists = conn.execute(f"SELECT 1 FROM {table} WHERE {where}", params).fetchone()
            if exists:
                sets = ", ".join(f"{k} = ?" for k in target)
                conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", list(target.values()) + params)
            else:
                cols = list(target.keys())
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                    [target[c] for c in cols],
                )


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.conn: sqlite3.Connection = self._open()
        self.dirty_since_backup = False

    # -- lifecycle -------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()

    def reopen(self) -> None:
        with self.lock:
            self.conn = self._open()

    def integrity_check(self) -> str:
        with self.lock:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
            return row[0] if row else "unknown"

    def schema_version(self) -> int:
        with self.lock:
            has = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
            if not has:
                return 0
            row = self.conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            return int(row[0]) if row else 0

    def migrate(self) -> list[str]:
        """Create/upgrade schema. Returns list of applied migration names."""
        applied: list[str] = []
        with self.lock:
            v = self.schema_version()
            if v < 1:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in SCHEMA_SQL.split(";"):
                        if stmt.strip():
                            self.conn.execute(stmt)
                    # SCHEMA_SQL is always the current schema, so a fresh file starts at the latest version
                    self.conn.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version',?)",
                        (str(SCHEMA_VERSION),))
                    self.conn.execute(
                        "INSERT OR IGNORE INTO settings(key,value) VALUES('revision','0')")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                applied.append(f"0001_initial (schema version {SCHEMA_VERSION})")
                v = SCHEMA_VERSION
            if v < 2:
                # [EN-ORIGINAL] ranges
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute(
                        "CREATE TABLE IF NOT EXISTS en_original_ranges ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id), "
                        "start_char INTEGER NOT NULL, end_char INTEGER NOT NULL)")
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS ix_en_par ON en_original_ranges(paragraph_id)")
                    self.conn.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version','2')")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                applied.append("0002_en_original_ranges")
                v = 2
            if v < 3:
                # paragraph timestamps (position = ¶n + hh:mm:ss + offsets)
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    cols = {r[1] for r in self.conn.execute("PRAGMA table_info(paragraphs)").fetchall()}
                    if "timestamp" not in cols:
                        self.conn.execute("ALTER TABLE paragraphs ADD COLUMN timestamp TEXT")
                    self.conn.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version','3')")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                applied.append("0003_paragraph_timestamp (existing paragraphs keep NULL; re-import fills them)")
                v = 3
            if v < 4:
                # [NO-QUOTE] ranges (quotation block)
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute(
                        "CREATE TABLE IF NOT EXISTS no_quote_ranges ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "paragraph_id INTEGER NOT NULL REFERENCES paragraphs(id), "
                        "start_char INTEGER NOT NULL, end_char INTEGER NOT NULL)")
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS ix_nq_par ON no_quote_ranges(paragraph_id)")
                    self.conn.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version','4')")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                applied.append("0004_no_quote_ranges")
                v = 4
            if v < 5:
                # agency value list: rebuild segment_codes with the new
                # CHECK constraint and remap the stored values.
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    changed = self._migrate_agency_values()
                    self.conn.execute(
                        "INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version','5')")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                applied.append(f"0005_agency_values ({changed} segment_codes row(s) remapped)")
        return applied

    def _migrate_agency_values(self) -> int:
        """Rebuild ``segment_codes`` with the v2.1 agency CHECK and map legacy values.
        Returns the number of rows whose agency changed.  Runs inside an open transaction."""
        rows = self.conn.execute("SELECT segment_id, code_id, valence, agency FROM segment_codes").fetchall()
        changed = 0
        mapped = []
        for r in rows:
            a = r["agency"]
            if a in LEGACY_AGENCY_MAP:
                new = LEGACY_AGENCY_MAP[a]
                changed += 1
            elif a in AGENCY_VALUES or a is None:
                new = a
            else:  # unknown legacy value: do not invent an actor type
                new = None
                changed += 1
            mapped.append((r["segment_id"], r["code_id"], r["valence"], new))
        self.conn.execute("DROP INDEX IF EXISTS ix_segcodes_code")
        self.conn.execute("ALTER TABLE segment_codes RENAME TO segment_codes_old")
        self.conn.execute(
            "CREATE TABLE segment_codes ("
            "segment_id INTEGER NOT NULL REFERENCES segments(id), "
            "code_id INTEGER NOT NULL REFERENCES codes(id), "
            "valence INTEGER NOT NULL DEFAULT 0 CHECK (valence IN (-1,0,1)), "
            f"agency TEXT CHECK ({_AGENCY_CHECK}), "
            "PRIMARY KEY (segment_id, code_id))")
        self.conn.executemany("INSERT INTO segment_codes(segment_id, code_id, valence, agency) VALUES (?,?,?,?)",
                              mapped)
        self.conn.execute("DROP TABLE segment_codes_old")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_segcodes_code ON segment_codes(code_id)")
        if changed:
            # journaled before/after rows still carry the old values; they could not be re-applied
            self.conn.execute("DELETE FROM undo_log")
        log.info("agency migration: %d row(s) remapped", changed)
        return changed

    # -- transactions ----------------------------------------------------
    @contextmanager
    def write(self, description: str = "", record_undo: bool = True) -> Iterator[Tx]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            tx = Tx(self.conn, record_undo)
            tx.description = description
            try:
                yield tx
                if record_undo and tx.changes:
                    desc = tx.description or description
                    self.conn.execute("DELETE FROM undo_log WHERE undone = 1")
                    inverse = [
                        {"op": c["op"], "table": c["table"], "pk": c["pk"],
                         "before": c["after"], "after": c["before"]}
                        for c in reversed(tx.changes)
                    ]
                    self.conn.execute(
                        "INSERT INTO undo_log(action_json, inverse_json, timestamp, undone) VALUES (?,?,?,0)",
                        (
                            json.dumps({"description": desc, "changes": tx.changes}, ensure_ascii=False),
                            json.dumps({"description": desc, "changes": inverse}, ensure_ascii=False),
                            now_iso(),
                        ),
                    )
                    self.conn.execute(
                        "DELETE FROM undo_log WHERE id NOT IN "
                        "(SELECT id FROM undo_log ORDER BY id DESC LIMIT 500)")
                self.conn.execute(
                    "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='revision'")
                self.conn.execute("COMMIT")
                self.dirty_since_backup = True
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            yield self.conn

    # -- convenience reads -----------------------------------------------
    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return r[0] if r else None

    def revision(self) -> int:
        return int(self.scalar("SELECT value FROM settings WHERE key='revision'") or 0)

    def setting(self, key: str, default: Any = None) -> Any:
        v = self.scalar("SELECT value FROM settings WHERE key=?", (key,))
        return default if v is None else v

    def set_setting(self, key: str, value: Any) -> None:
        with self.write(record_undo=False):
            self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

    # -- undo / redo -----------------------------------------------------
    def undo_state(self) -> dict[str, Any]:
        with self.lock:
            u = self.conn.execute(
                "SELECT id, action_json FROM undo_log WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
            r = self.conn.execute(
                "SELECT id, action_json FROM undo_log WHERE undone=1 ORDER BY id ASC LIMIT 1").fetchone()
            return {
                "can_undo": u is not None,
                "can_redo": r is not None,
                "undo_description": json.loads(u["action_json"])["description"] if u else None,
                "redo_description": json.loads(r["action_json"])["description"] if r else None,
            }

    def _bump(self) -> None:
        self.conn.execute(
            "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='revision'")

    def undo(self) -> dict[str, Any] | None:
        with self.lock:
            u = self.conn.execute(
                "SELECT id, action_json FROM undo_log WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
            if not u:
                return None
            action = json.loads(u["action_json"])
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                apply_changes(self.conn, action["changes"], "inverse")
                self.conn.execute("UPDATE undo_log SET undone=1 WHERE id=?", (u["id"],))
                self._bump()
                self.conn.execute("COMMIT")
                self.dirty_since_backup = True
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return {"undone": action["description"]}

    def redo(self) -> dict[str, Any] | None:
        with self.lock:
            r = self.conn.execute(
                "SELECT id, action_json FROM undo_log WHERE undone=1 ORDER BY id ASC LIMIT 1").fetchone()
            if not r:
                return None
            action = json.loads(r["action_json"])
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                apply_changes(self.conn, action["changes"], "forward")
                self.conn.execute("UPDATE undo_log SET undone=0 WHERE id=?", (r["id"],))
                self._bump()
                self.conn.execute("COMMIT")
                self.dirty_since_backup = True
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            return {"redone": action["description"]}

    def clear_undo(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM undo_log")
