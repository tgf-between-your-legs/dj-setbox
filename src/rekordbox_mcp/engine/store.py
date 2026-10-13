"""Engine store: the planner's own SQLite database.

Everything the engine learns or decides lives here, never in rekordbox's master.db:
cached per-beat analysis, verified landings and grid fixes, plans, the ledger of cues
the engine wrote, and ratings. Keyed by rekordbox content id, so playlist edits never
invalidate it.

Location: $RBMCP_ENGINE_DB, else data/engine.db in the checkout (or ~/.dj-setbox/ when installed). Schema changes are
append-only migrations (MIGRATIONS); `Store()` upgrades an older file in place.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

_MIGRATE_LOCK = threading.Lock()

MIGRATIONS: list[str] = [
    # 1: phase 1 - track memory, plans, ledger
    """
    CREATE TABLE track_analysis (
        track_id TEXT PRIMARY KEY,
        source_key TEXT NOT NULL,          -- analysis files' size+mtime; a change re-analyses
        version INTEGER NOT NULL,          -- features.ANALYSIS_VERSION
        computed_at TEXT NOT NULL,
        data TEXT NOT NULL                 -- JSON, see features.TrackFeatures.to_json
    );
    CREATE TABLE track_name (
        track_id TEXT PRIMARY KEY,
        title TEXT, artist TEXT,           -- display names for cue comments / run sheets
        source TEXT NOT NULL DEFAULT 'manual',
        updated_at TEXT NOT NULL
    );
    CREATE TABLE landing (
        track_id TEXT PRIMARY KEY,
        bar INTEGER NOT NULL,              -- grid bar the track is played for (hook/drop)
        confidence REAL,
        source TEXT NOT NULL,              -- auto | hint | ear | seed
        note TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE exit_pin (
        track_id TEXT PRIMARY KEY,
        bar INTEGER NOT NULL,              -- OUT DONE bar the planner must use
        note TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE grid_fix (
        track_id TEXT PRIMARY KEY,
        beat_offset INTEGER NOT NULL,      -- real bar starts this many beats from the grid's bar (e.g. -2)
        note TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE pair_pin (
        a_id TEXT NOT NULL, b_id TEXT NOT NULL,
        family TEXT,                       -- blend | cut | NULL
        technique TEXT,                    -- preferred technique or NULL
        note TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (a_id, b_id)
    );
    CREATE TABLE plan (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id TEXT,
        name TEXT,
        profile TEXT NOT NULL,
        params TEXT NOT NULL,              -- JSON: pins/overrides used
        summary TEXT,                      -- JSON: counts, minutes
        status TEXT NOT NULL DEFAULT 'draft',  -- draft | applied | superseded
        created_at TEXT NOT NULL,
        applied_at TEXT,
        backup_path TEXT
    );
    CREATE TABLE plan_track (
        plan_id INTEGER NOT NULL REFERENCES plan(plan_id) ON DELETE CASCADE,
        pos INTEGER NOT NULL,
        track_id TEXT NOT NULL,
        s INTEGER NOT NULL, land INTEGER NOT NULL, e INTEGER,
        family TEXT, technique TEXT, overlap INTEGER,   -- describe the transition INTO the next track
        play_sec REAL,
        info TEXT,                         -- JSON: why, flags
        PRIMARY KEY (plan_id, pos)
    );
    CREATE TABLE plan_cue (
        plan_id INTEGER NOT NULL REFERENCES plan(plan_id) ON DELETE CASCADE,
        track_id TEXT NOT NULL,
        bar INTEGER NOT NULL, beat INTEGER NOT NULL,
        color TEXT NOT NULL, comment TEXT NOT NULL,
        role TEXT                          -- in_start | in_done | in | out_start | out_done
    );
    CREATE INDEX plan_cue_by_plan ON plan_cue(plan_id, track_id);
    CREATE TABLE cue_ledger (
        cue_id TEXT PRIMARY KEY,           -- djmdCue.ID the engine created (or adopted)
        track_id TEXT NOT NULL,
        plan_id INTEGER,
        position_ms INTEGER, color TEXT, comment TEXT,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    CREATE INDEX ledger_by_track ON cue_ledger(track_id);
    CREATE TABLE rating (
        rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER,
        a_id TEXT NOT NULL, b_id TEXT NOT NULL,
        verdict INTEGER NOT NULL,          -- 1 works, 0 doesn't
        source TEXT NOT NULL,              -- rekordbox | sheet | manual | tracklist
        weight REAL NOT NULL DEFAULT 1.0,
        features TEXT,                     -- JSON snapshot of the transition when rated
        note TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (plan_id, a_id, b_id, source)
    );
    """,
    # 2: phase 2 - audition sessions
    """
    CREATE TABLE audition (
        audition_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        playlist_id TEXT,
        pairs TEXT NOT NULL,               -- JSON [[a_id, b_id], ...] in audition order
        staged_at TEXT NOT NULL
    );
    """,
    # 3: phase 4 - learned weights and tracklists
    """
    CREATE TABLE model (
        model_id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile TEXT NOT NULL,
        created_at TEXT NOT NULL,
        n_train INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 0,
        data TEXT NOT NULL                 -- JSON: feature names, weights, scaling, eval report
    );
    CREATE TABLE tracklist (
        tracklist_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, dj TEXT, source_url TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE tracklist_entry (
        tracklist_id INTEGER NOT NULL REFERENCES tracklist(tracklist_id) ON DELETE CASCADE,
        pos INTEGER NOT NULL,
        raw TEXT NOT NULL,
        start_sec REAL,
        track_id TEXT,                     -- matched library track or NULL
        match_score REAL, match_source TEXT,
        est_land INTEGER, est_exit INTEGER, -- estimated from timestamps + grid
        PRIMARY KEY (tracklist_id, pos)
    );
    """,
    # 4: strict pair pins (yours: always honoured) vs soft ones (imported: honoured when they suit)
    """
    ALTER TABLE pair_pin ADD COLUMN strict INTEGER NOT NULL DEFAULT 1;
    """,
]


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def default_path() -> Path:
    env = os.environ.get("RBMCP_ENGINE_DB")
    if env:
        return Path(env)
    from ..paths import engine_db
    return engine_db()


class Store:
    """Small typed facade over the engine DB. One connection per thread; safe to share."""

    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else default_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._mem_conn: Optional[sqlite3.Connection] = None
        self.migrate()

    # ------------------------------------------------------------------ plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        if str(self.path) == ":memory:":
            if self._mem_conn is None:
                self._mem_conn = self._connect()
            return self._mem_conn
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    def _connect(self) -> sqlite3.Connection:
        # several processes (MCP server, web app, CLI) may share the file: wait for locks instead of failing
        c = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 30000")
        c.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            with _MIGRATE_LOCK:
                if c.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                    c.execute("PRAGMA journal_mode = WAL")
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self.conn
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise

    def migrate(self) -> int:
        """Apply pending migrations, each atomically (schema + user_version in one transaction).
        Safe when several threads or processes open the same file at once: a loser re-reads the
        version and carries on."""
        c = self.conn
        with _MIGRATE_LOCK:
            while True:
                ver = c.execute("PRAGMA user_version").fetchone()[0]
                if ver >= len(MIGRATIONS):
                    return len(MIGRATIONS)
                try:
                    c.executescript(f"BEGIN IMMEDIATE;\n{MIGRATIONS[ver]}\nPRAGMA user_version = {ver + 1};\nCOMMIT;")
                except sqlite3.OperationalError:
                    if c.in_transaction:
                        c.execute("ROLLBACK")
                    if c.execute("PRAGMA user_version").fetchone()[0] > ver:
                        continue        # someone else applied it
                    raise

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def q1(self, sql: str, args: Iterable[Any] = ()) -> Optional[dict]:
        r = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ analysis cache
    def get_analysis(self, track_id: str, source_key: str, version: int) -> Optional[dict]:
        r = self.q1("SELECT data FROM track_analysis WHERE track_id=? AND source_key=? AND version=?",
                    (str(track_id), source_key, version))
        return json.loads(r["data"]) if r else None

    def put_analysis(self, track_id: str, source_key: str, version: int, data: dict) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO track_analysis VALUES (?,?,?,?,?)",
                      (str(track_id), source_key, version, now(), json.dumps(data, separators=(",", ":"))))

    # ------------------------------------------------------------------ per-track facts
    def set_name(self, track_id: str, title: Optional[str], artist: Optional[str], source: str = "manual") -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO track_name VALUES (?,?,?,?,?)", (str(track_id), title, artist, source, now()))

    def names(self) -> dict[str, dict]:
        return {r["track_id"]: r for r in self.q("SELECT * FROM track_name")}

    def set_landing(self, track_id: str, bar: int, source: str = "ear", confidence: Optional[float] = None,
                    note: Optional[str] = None) -> None:
        if source not in ("auto", "hint", "ear", "seed"):
            raise ValueError("source must be auto | hint | ear | seed")
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO landing VALUES (?,?,?,?,?,?)",
                      (str(track_id), int(bar), confidence, source, note, now()))

    def landings(self) -> dict[str, dict]:
        return {r["track_id"]: r for r in self.q("SELECT * FROM landing")}

    def set_exit_pin(self, track_id: str, bar: Optional[int], note: Optional[str] = None) -> None:
        with self.tx() as c:
            if bar is None:
                c.execute("DELETE FROM exit_pin WHERE track_id=?", (str(track_id),))
            else:
                c.execute("INSERT OR REPLACE INTO exit_pin VALUES (?,?,?,?)", (str(track_id), int(bar), note, now()))

    def exit_pins(self) -> dict[str, int]:
        return {r["track_id"]: r["bar"] for r in self.q("SELECT * FROM exit_pin")}

    def set_grid_fix(self, track_id: str, beat_offset: int, note: Optional[str] = None) -> None:
        with self.tx() as c:
            if beat_offset == 0:
                c.execute("DELETE FROM grid_fix WHERE track_id=?", (str(track_id),))
            else:
                c.execute("INSERT OR REPLACE INTO grid_fix VALUES (?,?,?,?)", (str(track_id), int(beat_offset), note, now()))

    def grid_fixes(self) -> dict[str, int]:
        return {r["track_id"]: r["beat_offset"] for r in self.q("SELECT * FROM grid_fix")}

    def set_pair_pin(self, a_id: str, b_id: str, family: Optional[str] = None, technique: Optional[str] = None,
                     note: Optional[str] = None, strict: bool = True) -> None:
        with self.tx() as c:
            if family is None and technique is None:
                c.execute("DELETE FROM pair_pin WHERE a_id=? AND b_id=?", (str(a_id), str(b_id)))
            else:
                c.execute("INSERT OR REPLACE INTO pair_pin (a_id, b_id, family, technique, note, updated_at, strict) "
                          "VALUES (?,?,?,?,?,?,?)", (str(a_id), str(b_id), family, technique, note, now(), int(strict)))

    def pair_pins(self) -> dict[tuple[str, str], dict]:
        return {(r["a_id"], r["b_id"]): r for r in self.q("SELECT * FROM pair_pin")}

    # ------------------------------------------------------------------ plans
    def save_plan(self, *, playlist_id: Optional[str], name: str, profile: str, params: dict, summary: dict,
                  tracks: list[dict], cues: list[dict]) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO plan (playlist_id, name, profile, params, summary, created_at) VALUES (?,?,?,?,?,?)",
                            (playlist_id, name, profile, json.dumps(params), json.dumps(summary), now()))
            pid = int(cur.lastrowid)
            c.executemany("INSERT INTO plan_track VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          [(pid, t["pos"], t["track_id"], t["s"], t["land"], t.get("e"), t.get("family"),
                            t.get("technique"), t.get("overlap"), t.get("play_sec"), json.dumps(t.get("info") or {}))
                           for t in tracks])
            c.executemany("INSERT INTO plan_cue VALUES (?,?,?,?,?,?,?)",
                          [(pid, q["track_id"], q["bar"], q["beat"], q["color"], q["comment"], q.get("role"))
                           for q in cues])
        return pid

    def plan(self, plan_id: int) -> dict:
        p = self.q1("SELECT * FROM plan WHERE plan_id=?", (plan_id,))
        if p is None:
            raise ValueError(f"no plan {plan_id}")
        p["params"] = json.loads(p["params"] or "{}")
        p["summary"] = json.loads(p["summary"] or "{}")
        p["tracks"] = self.q("SELECT * FROM plan_track WHERE plan_id=? ORDER BY pos", (plan_id,))
        for t in p["tracks"]:
            t["info"] = json.loads(t["info"] or "{}")
        p["cues"] = self.q("SELECT * FROM plan_cue WHERE plan_id=? ORDER BY track_id, bar, beat", (plan_id,))
        return p

    def plans(self, playlist_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        if playlist_id:
            return self.q("SELECT plan_id, playlist_id, name, profile, status, created_at, applied_at, summary FROM plan "
                          "WHERE playlist_id=? ORDER BY plan_id DESC LIMIT ?", (playlist_id, limit))
        return self.q("SELECT plan_id, playlist_id, name, profile, status, created_at, applied_at, summary FROM plan "
                      "ORDER BY plan_id DESC LIMIT ?", (limit,))

    def mark_applied(self, plan_id: int, backup_path: Optional[str]) -> None:
        with self.tx() as c:
            p = c.execute("SELECT playlist_id FROM plan WHERE plan_id=?", (plan_id,)).fetchone()
            if p and p[0]:
                c.execute("UPDATE plan SET status='superseded' WHERE playlist_id=? AND status='applied'", (p[0],))
            c.execute("UPDATE plan SET status='applied', applied_at=?, backup_path=? WHERE plan_id=?",
                      (now(), backup_path, plan_id))

    # ------------------------------------------------------------------ ledger
    def ledger(self, track_ids: Optional[Iterable[str]] = None, live_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM cue_ledger"
        conds, args = [], []
        if live_only:
            conds.append("deleted_at IS NULL")
        if track_ids is not None:
            ids = [str(t) for t in track_ids]
            conds.append(f"track_id IN ({','.join('?' * len(ids))})")
            args += ids
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return self.q(sql, args)

    def ledger_add(self, rows: list[dict]) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO cue_ledger VALUES (?,?,?,?,?,?,?,NULL)",
                          [(str(r["cue_id"]), str(r["track_id"]), r.get("plan_id"), r.get("position_ms"),
                            r.get("color"), r.get("comment"), now()) for r in rows])

    def ledger_mark_deleted(self, cue_ids: Iterable[str]) -> None:
        with self.tx() as c:
            c.executemany("UPDATE cue_ledger SET deleted_at=? WHERE cue_id=?", [(now(), str(i)) for i in cue_ids])

    # ------------------------------------------------------------------ ratings
    def add_rating(self, a_id: str, b_id: str, works: bool, source: str, plan_id: Optional[int] = None,
                   weight: float = 1.0, features: Optional[dict] = None, note: Optional[str] = None) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO rating (plan_id, a_id, b_id, verdict, source, weight, features, note, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (plan_id, str(a_id), str(b_id), 1 if works else 0, source, weight,
                       json.dumps(features) if features else None, note, now()))

    def ratings(self, source: Optional[str] = None) -> list[dict]:
        rows = self.q("SELECT * FROM rating" + (" WHERE source=?" if source else "") + " ORDER BY rating_id",
                      (source,) if source else ())
        for r in rows:
            r["features"] = json.loads(r["features"]) if r["features"] else None
        return rows
