"""PostgreSQL 访问层（psycopg3 + 连接池）。

连接配置（二选一）：
  1. DATABASE_URL=postgresql://user:pass@host:5432/dbname
  2. PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE 环境变量

录音文件用 PG Large Object 存储，不落本地磁盘（部署无需持久卷）。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

_lock = threading.Lock()
_pool = None


def db_ready() -> bool:
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("PGHOST"))


def _conninfo() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "interview_review"),
        connect_timeout=10,
    )


def get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool

                _pool = ConnectionPool(
                    _conninfo(),
                    min_size=1,
                    max_size=6,
                    kwargs={"row_factory": dict_row},
                    open=True,
                    timeout=20,
                )
    return _pool


class conn_ctx:
    def __enter__(self):
        self._cm = get_pool().connection()
        self.conn = self._cm.__enter__()
        return self.conn

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def query_one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> None:
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


# 幂等 DDL：新老库都能跑
DDL = """
CREATE TABLE IF NOT EXISTS app_users (
  id          SERIAL PRIMARY KEY,
  email       TEXT UNIQUE,
  username    TEXT,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS interviews (
  id             SERIAL PRIMARY KEY,
  user_email     TEXT NOT NULL,
  company        TEXT,
  role_title     TEXT,
  round_name     TEXT,
  interview_date DATE,
  source         TEXT,
  transcript     TEXT,
  raw_transcript TEXT,
  segments       JSONB,
  audio_oid      OID,
  audio_name     TEXT,
  audio_size     BIGINT,
  audio_type     TEXT,
  duration_sec   INTEGER,
  status         TEXT DEFAULT 'queued',
  stage          TEXT,
  overall_score  INTEGER,
  outcome        TEXT,
  analysis       JSONB,
  error_msg      TEXT,
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  updated_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_interviews_user ON interviews(user_email, created_at DESC);

-- 老库升级：追加缺失字段（幂等）
ALTER TABLE interviews ADD COLUMN IF NOT EXISTS raw_transcript TEXT;
ALTER TABLE interviews ADD COLUMN IF NOT EXISTS segments JSONB;
ALTER TABLE interviews ADD COLUMN IF NOT EXISTS stage TEXT;

CREATE TABLE IF NOT EXISTS qa_items (
  id            SERIAL PRIMARY KEY,
  interview_id  INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
  seq           INTEGER,
  category      TEXT,
  question      TEXT,
  answer_digest TEXT,
  score         INTEGER,
  strengths     TEXT,
  issues        TEXT,
  better_answer TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qa_interview ON qa_items(interview_id, seq);
CREATE INDEX IF NOT EXISTS idx_qa_category ON qa_items(category);
"""


def init_schema() -> None:
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)


def upsert_user(user: dict) -> None:
    execute(
        """
        INSERT INTO app_users (email, username)
        VALUES (%s, %s)
        ON CONFLICT (email) DO UPDATE SET username = EXCLUDED.username
        """,
        (user["email"], user.get("name")),
    )


# ---------- 音频：PG Large Object ----------

def save_audio(blob: bytes) -> int:
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT lo_from_bytea(0, %s) AS oid", (blob,))
            return cur.fetchone()["oid"]


def read_audio(oid: int) -> bytes:
    with conn_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT lo_get(%s) AS data", (oid,))
            row = cur.fetchone()
            return bytes(row["data"]) if row else b""


def drop_audio(oid: int) -> None:
    try:
        execute("SELECT lo_unlink(%s)", (oid,))
    except Exception:
        pass
