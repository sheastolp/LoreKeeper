"""SQLite storage: sources, chunks (+FTS5 index), transcripts, timeline projects."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

import numpy as np

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- document | url | image | video | audio
    title       TEXT NOT NULL,
    path        TEXT,                       -- file on disk (original)
    url         TEXT,
    owned       INTEGER NOT NULL DEFAULT 0, -- 1 = file lives in our library folder
    size        INTEGER,
    duration    REAL,
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    REAL NOT NULL DEFAULT 0,
    stage       TEXT,
    error       TEXT,
    warnings    TEXT,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    kind        TEXT NOT NULL,              -- text | transcript | frame | image | ocr
    text        TEXT NOT NULL,
    start       REAL,
    end         REAL,
    page        INTEGER,
    embedding   BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS transcripts (
    source_id   TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    language    TEXT,
    segments    TEXT NOT NULL,              -- [{start,end,text}]
    words       TEXT NOT NULL               -- [{w,s,e,p}]
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    data        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class DB:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
        self.version = 0  # bumps whenever chunks change -> vector cache invalidation

    # ---------------------------------------------------------- helpers
    def q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    # ---------------------------------------------------------- sources
    def add_source(self, kind: str, title: str, path: str | None = None, url: str | None = None,
                   owned: bool = False, size: int | None = None, meta: dict | None = None) -> str:
        sid = uuid.uuid4().hex[:12]
        self.run(
            "INSERT INTO sources(id,kind,title,path,url,owned,size,meta,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, kind, title, path, url, int(owned), size, json.dumps(meta or {}), time.time()),
        )
        return sid

    def update_source(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        if "meta" in fields and isinstance(fields["meta"], dict):
            fields["meta"] = json.dumps(fields["meta"])
        if "warnings" in fields and isinstance(fields["warnings"], list):
            fields["warnings"] = json.dumps(fields["warnings"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.run(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), sid))

    def merge_meta(self, sid: str, patch: dict) -> dict:
        with self.lock:
            row = self.one("SELECT meta FROM sources WHERE id=?", (sid,))
            meta = json.loads(row["meta"]) if row else {}
            meta.update(patch)
            self.update_source(sid, meta=meta)
            return meta

    def source(self, sid: str) -> dict | None:
        row = self.one("SELECT * FROM sources WHERE id=?", (sid,))
        return self._source_dict(row) if row else None

    def sources(self) -> list[dict]:
        rows = self.q(
            "SELECT s.*, (SELECT COUNT(*) FROM chunks c WHERE c.source_id=s.id) AS n_chunks, "
            "(SELECT COUNT(*) FROM transcripts t WHERE t.source_id=s.id) AS has_transcript "
            "FROM sources s ORDER BY created_at DESC"
        )
        return [self._source_dict(r) for r in rows]

    @staticmethod
    def _source_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["meta"] = json.loads(d.get("meta") or "{}")
        d["warnings"] = json.loads(d["warnings"]) if d.get("warnings") else []
        return d

    def delete_source(self, sid: str) -> None:
        self.run("DELETE FROM sources WHERE id=?", (sid,))
        self.version += 1

    # ---------------------------------------------------------- chunks
    def replace_chunks(self, sid: str, chunks: list[dict]) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("DELETE FROM chunks WHERE source_id=?", (sid,))
                for i, c in enumerate(chunks):
                    self.conn.execute(
                        "INSERT INTO chunks(source_id,ord,kind,text,start,end,page) VALUES (?,?,?,?,?,?,?)",
                        (sid, i, c.get("kind", "text"), c["text"], c.get("start"), c.get("end"), c.get("page")),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        self.version += 1

    def chunks_for(self, sid: str) -> list[dict]:
        rows = self.q(
            "SELECT id, ord, kind, text, start, end, page, embedding IS NOT NULL AS embedded "
            "FROM chunks WHERE source_id=? ORDER BY ord", (sid,))
        return [dict(r) for r in rows]

    def set_embeddings(self, ids: list[int], vectors: np.ndarray) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            for cid, vec in zip(ids, vectors):
                self.conn.execute("UPDATE chunks SET embedding=? WHERE id=?",
                                  (np.asarray(vec, dtype=np.float32).tobytes(), cid))
            self.conn.execute("COMMIT")
        self.version += 1

    # ---------------------------------------------------------- transcripts
    def save_transcript(self, sid: str, language: str, segments: list, words: list) -> None:
        self.run("INSERT OR REPLACE INTO transcripts(source_id,language,segments,words) VALUES (?,?,?,?)",
                 (sid, language, json.dumps(segments), json.dumps(words)))

    def transcript(self, sid: str) -> dict | None:
        row = self.one("SELECT * FROM transcripts WHERE source_id=?", (sid,))
        if not row:
            return None
        return {"language": row["language"], "segments": json.loads(row["segments"]),
                "words": json.loads(row["words"])}

    # ---------------------------------------------------------- projects
    def projects(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT id, name, updated_at FROM projects ORDER BY updated_at DESC")]

    def project(self, pid: str) -> dict | None:
        row = self.one("SELECT * FROM projects WHERE id=?", (pid,))
        if not row:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d

    def save_project(self, pid: str | None, name: str, data: dict) -> str:
        pid = pid or uuid.uuid4().hex[:12]
        self.run("INSERT OR REPLACE INTO projects(id,name,data,updated_at) VALUES (?,?,?,?)",
                 (pid, name, json.dumps(data), time.time()))
        return pid

    def delete_project(self, pid: str) -> None:
        self.run("DELETE FROM projects WHERE id=?", (pid,))


db = DB()
