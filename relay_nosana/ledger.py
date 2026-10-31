from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


DB_PATH = Path("out/ledger.sqlite")

DDL = """
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    phase       TEXT    NOT NULL,   -- calibrate | run | compile | synthesis
    task_type   TEXT,
    instance_id TEXT,
    recipient_id TEXT,
    mode        TEXT,               -- raw | compiled
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  REAL,
    cost_usd    REAL,
    score       REAL,
    parse_ok    INTEGER,            -- 1 | 0
    error       TEXT,
    ts          REAL                -- unix epoch
);

CREATE TABLE IF NOT EXISTS deployments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   TEXT    NOT NULL UNIQUE,
    model           TEXT,
    market          TEXT,
    t_requested     REAL,
    t_ready         REAL,
    t_stopped       REAL,
    gpu_hours       REAL,
    credits_before  REAL,
    credits_after   REAL
);
"""


def _db_path() -> Path:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def init_db(path: Path | None = None) -> sqlite3.Connection:
    p = path or _db_path()
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.commit()
    return conn


@contextmanager
def open_db(path: Path | None = None):
    conn = init_db(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_call(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    phase: str,
    task_type: str | None = None,
    instance_id: str | None = None,
    recipient_id: str | None = None,
    mode: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    latency_ms: float | None = None,
    cost_usd: float | None = None,
    score: float | None = None,
    parse_ok: bool | None = None,
    error: str | None = None,
    ts: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO calls
          (run_id, phase, task_type, instance_id, recipient_id, mode,
           tokens_in, tokens_out, latency_ms, cost_usd, score, parse_ok, error, ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, phase, task_type, instance_id, recipient_id, mode,
            tokens_in, tokens_out, latency_ms, cost_usd, score,
            int(parse_ok) if parse_ok is not None else None,
            error, ts or time.time(),
        ),
    )


def log_deployment(
    conn: sqlite3.Connection,
    *,
    deployment_id: str,
    model: str | None = None,
    market: str | None = None,
    t_requested: float | None = None,
    t_ready: float | None = None,
    t_stopped: float | None = None,
    gpu_hours: float | None = None,
    credits_before: float | None = None,
    credits_after: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO deployments
          (deployment_id, model, market, t_requested, t_ready, t_stopped,
           gpu_hours, credits_before, credits_after)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(deployment_id) DO UPDATE SET
          t_ready=excluded.t_ready,
          t_stopped=excluded.t_stopped,
          gpu_hours=excluded.gpu_hours,
          credits_after=excluded.credits_after
        """,
        (
            deployment_id, model, market, t_requested, t_ready, t_stopped,
            gpu_hours, credits_before, credits_after,
        ),
    )


def query_summary(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Return the key report numbers for a run, derived purely from SQL."""
    rows = conn.execute(
        """
        SELECT
          task_type,
          mode,
          recipient_id,
          COUNT(*) AS n,
          AVG(score) AS mean_score,
          SUM(cost_usd) AS total_cost_usd,
          AVG(tokens_in) AS mean_tokens_in,
          AVG(tokens_out) AS mean_tokens_out,
          AVG(latency_ms) AS mean_latency_ms,
          SUM(CASE WHEN parse_ok=1 THEN 1 ELSE 0 END)*1.0/COUNT(*) AS parse_ok_rate
        FROM calls
        WHERE run_id=? AND score IS NOT NULL
        GROUP BY task_type, mode, recipient_id
        ORDER BY task_type, mode, recipient_id
        """,
        (run_id,),
    ).fetchall()
    return {"run_id": run_id, "cells": [dict(r) for r in rows]}
