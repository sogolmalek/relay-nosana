"""Ledger tests: report numbers must be reproducible from SQL."""
import sqlite3
from pathlib import Path

import pytest

from relay_nosana.ledger import init_db, log_call, log_deployment, query_summary, open_db


@pytest.fixture
def tmp_db(tmp_path):
    p = tmp_path / "test.sqlite"
    return p


def test_ledger_schema_created(tmp_db):
    conn = init_db(tmp_db)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "calls" in tables
    assert "deployments" in tables
    conn.close()


def test_log_and_query_call(tmp_db):
    with open_db(tmp_db) as conn:
        log_call(
            conn,
            run_id="test-run",
            phase="calibrate",
            task_type="label",
            instance_id="1:label",
            recipient_id="mock:deterministic",
            mode="raw",
            tokens_in=400,
            tokens_out=10,
            latency_ms=5.0,
            cost_usd=0.001,
            score=1.0,
            parse_ok=True,
        )
        log_call(
            conn,
            run_id="test-run",
            phase="calibrate",
            task_type="label",
            instance_id="2:label",
            recipient_id="mock:deterministic",
            mode="raw",
            tokens_in=350,
            tokens_out=10,
            latency_ms=4.5,
            cost_usd=0.001,
            score=0.0,
            parse_ok=True,
        )

    with open_db(tmp_db) as conn:
        summary = query_summary(conn, "test-run")
        assert summary["run_id"] == "test-run"
        cells = summary["cells"]
        assert len(cells) == 1
        cell = cells[0]
        assert cell["task_type"] == "label"
        assert cell["n"] == 2
        assert abs(cell["mean_score"] - 0.5) < 0.001


def test_log_deployment(tmp_db):
    with open_db(tmp_db) as conn:
        log_deployment(
            conn,
            deployment_id="dep-001",
            model="llama-3.1-8b",
            market="market-a",
            t_requested=1000.0,
            t_ready=1120.0,
            t_stopped=3000.0,
            gpu_hours=(3000.0 - 1000.0) / 3600.0,
            credits_before=10.0,
            credits_after=7.5,
        )

    with open_db(tmp_db) as conn:
        rows = conn.execute("SELECT * FROM deployments WHERE deployment_id='dep-001'").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["model"] == "llama-3.1-8b"
        assert abs(row["gpu_hours"] - 2000 / 3600) < 0.001


def test_deployment_upsert(tmp_db):
    """log_deployment with the same ID updates t_stopped and credits_after."""
    with open_db(tmp_db) as conn:
        log_deployment(conn, deployment_id="dep-002", t_requested=100.0)
        log_deployment(conn, deployment_id="dep-002", t_stopped=500.0, gpu_hours=0.1, credits_after=5.0)

    with open_db(tmp_db) as conn:
        rows = conn.execute("SELECT * FROM deployments WHERE deployment_id='dep-002'").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["t_stopped"] == 500.0
        assert row["credits_after"] == 5.0


def test_sql_query_mean_score(tmp_db):
    """Run the actual queries.sql-style query and verify the result."""
    with open_db(tmp_db) as conn:
        for i, score in enumerate([1.0, 0.0, 1.0, 1.0, 0.0]):
            log_call(
                conn,
                run_id="r1",
                phase="calibrate",
                task_type="label",
                instance_id=f"{i}:label",
                recipient_id="mock:det",
                mode="compiled",
                tokens_in=100,
                tokens_out=10,
                latency_ms=5.0,
                cost_usd=0.0,
                score=score,
                parse_ok=True,
            )

    with open_db(tmp_db) as conn:
        row = conn.execute(
            """
            SELECT AVG(score) AS mean_score, COUNT(*) AS n
            FROM calls
            WHERE run_id='r1' AND phase='calibrate' AND score IS NOT NULL
            """,
        ).fetchone()
        assert row["n"] == 5
        assert abs(row["mean_score"] - 0.60) < 0.001


def test_parse_fail_tracked(tmp_db):
    with open_db(tmp_db) as conn:
        log_call(conn, run_id="r2", phase="calibrate", task_type="label",
                 instance_id="1", recipient_id="mock", mode="raw",
                 tokens_in=100, tokens_out=0, latency_ms=5.0,
                 parse_ok=False, error="parse_failed")

    with open_db(tmp_db) as conn:
        row = conn.execute(
            "SELECT parse_ok FROM calls WHERE run_id='r2'"
        ).fetchone()
        assert row["parse_ok"] == 0
