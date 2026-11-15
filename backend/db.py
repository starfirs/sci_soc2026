"""SQLite 存储层：设计文档第 3 节的主表，外加占位符映射、整合报告与次日卡片缓存。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS segment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start    TEXT NOT NULL,
    ts_end      TEXT NOT NULL,
    text_masked TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    processed   INTEGER NOT NULL DEFAULT 0 CHECK (processed IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_segment_processed ON segment(processed);

CREATE TABLE IF NOT EXISTS daily_summary (
    date     TEXT PRIMARY KEY,
    summary  TEXT NOT NULL DEFAULT '',
    topics   TEXT NOT NULL DEFAULT '[]',
    node_ids TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS node (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    type               TEXT NOT NULL CHECK (type IN ('fact','event','person','concept','todo','day')),
    title              TEXT NOT NULL,
    content            TEXT NOT NULL,
    importance         REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','archived','superseded')),
    source_segment_ids TEXT NOT NULL DEFAULT '[]',
    embedding          BLOB,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    step               INTEGER NOT NULL DEFAULT 0 CHECK (step >= 0),
    interval_days      INTEGER NOT NULL DEFAULT 1 CHECK (interval_days >= 1),
    due_at             TEXT,
    last_reviewed_at   TEXT,
    reps               INTEGER NOT NULL DEFAULT 0,
    lapses             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_node_due ON node(status, due_at);
CREATE INDEX IF NOT EXISTS idx_node_type ON node(type);

CREATE TABLE IF NOT EXISTS edge (
    src        INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    dst        INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    relation   TEXT NOT NULL CHECK (relation IN ('related','part_of','causes','same_as')),
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (src, dst, relation)
);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst);

CREATE TABLE IF NOT EXISTS review_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    card_type   TEXT NOT NULL,
    feedback    TEXT NOT NULL CHECK (feedback IN ('remembered','fuzzy','forgot')),
    step_before INTEGER NOT NULL,
    step_after  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_log_node ON review_log(node_id);

CREATE TABLE IF NOT EXISTS daily_card (
    review_date  TEXT NOT NULL,
    node_id      INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    card_type    TEXT NOT NULL,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    step         INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'llm'
                 CHECK (source IN ('llm', 'local')),
    created_at   TEXT NOT NULL,
    PRIMARY KEY (review_date, node_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_card_date ON daily_card(review_date);

CREATE TABLE IF NOT EXISTS placeholder_map (
    placeholder TEXT PRIMARY KEY,
    real_value  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consolidation_report (
    date        TEXT PRIMARY KEY,
    report_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
