"""Database tests. Set TEST_DATABASE_URL to an empty, disposable Postgres database."""
import os
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import pipeline
from db import get_engine, init_schema

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

HEADER = (b"ID,Space ID,Space Name,Status,Name,Financial Account ID,Amount,Currency,Total Charge Amount,"
          b"Reference,Provider Txn Reference,Order ID,Order Number,Provider ID,Provider Name,Create Time\n")


def export(*rows):
    """rows: (txn_id, space, amount, iso time)"""
    lines = [f"{t},spc-{s},{s},completed,,fac-1,{a},SLE,0,R{t},P{t},O{t},m17,Orange Money,{ts}"
             for t, s, a, ts in rows]
    return HEADER + ("\n".join(lines) + "\n").encode()


@pytest.fixture
def engine():
    engine = get_engine(URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    init_schema(engine)
    yield engine
    engine.dispose()


def test_overlapping_exports_store_each_transaction_once(engine):
    first = export(("a", "Shop", 10, "2026-09-15T10:00:00Z"), ("b", "Shop", 20, "2026-09-15T11:00:00Z"))
    second = export(("b", "Shop", 20, "2026-09-15T11:00:00Z"), ("c", "Shop", 30, "2026-09-16T09:00:00Z"))
    r1 = pipeline.ingest_and_analyze(engine, "one.csv", first, "tester")
    r2 = pipeline.ingest_and_analyze(engine, "two.csv", second, "tester")
    assert (r1["upload"]["inserted"], r2["upload"]["inserted"], r2["upload"]["duplicates"]) == (2, 1, 1)
    assert r2["upload"]["dates"] == [date(2026, 9, 16)]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM transactions")).scalar() == 3


def test_same_file_twice_is_skipped(engine):
    data = export(("a", "Shop", 10, "2026-09-15T10:00:00Z"))
    pipeline.ingest_and_analyze(engine, "one.csv", data, "tester")
    again = pipeline.ingest_and_analyze(engine, "one-copy.csv", data, "tester")
    assert again["upload"]["already_uploaded"] and again["runs"] == []


def test_alert_status_survives_reanalysis(engine):
    data = export(*[(f"t{i}", "Shop", 9500, f"2026-09-15T10:0{i}:00Z") for i in range(3)])
    pipeline.ingest_and_analyze(engine, "one.csv", data, "tester")
    alert = pipeline.list_alerts(engine).query("rule_code == 'STRUCTURING'").iloc[0]
    pipeline.update_alert(engine, int(alert["id"]), "investigating", "Called merchant", "analyst")
    run = pipeline.analyze_date(engine, date(2026, 9, 15), "tester")
    assert run["new_alerts"] == 0
    after = pipeline.list_alerts(engine).query("rule_code == 'STRUCTURING'").iloc[0]
    assert (after["status"], after["status_note"]) == ("investigating", "Called merchant")


def test_profile_changes_feed_the_next_run(engine):
    data = export(*[(f"t{i}", "Shop", 1000, f"2026-09-15T10:0{i}:00Z") for i in range(5)])
    pipeline.ingest_and_analyze(engine, "one.csv", data, "tester")
    pipeline.save_profile(engine, "spc-Shop", "Shop",
                          {"industry_risk": "high", "declared_daily_volume": 1000}, "analyst")
    run = pipeline.analyze_date(engine, date(2026, 9, 15), "analyst")
    rules = {r["rule"] for r in pipeline.scores_for_run(engine, run["run_id"]).iloc[0]["reasons"]}
    assert {"DECLARED_VOLUME", "INDUSTRY"} <= rules
    assert "DECLARED_VOLUME" in set(pipeline.list_alerts(engine)["rule_code"])


def test_audit_log_is_append_only(engine):
    pipeline.ingest_and_analyze(engine, "one.csv", export(("a", "Shop", 10, "2026-09-15T10:00:00Z")), "tester")
    actions = set(pipeline.audit_trail(engine)["action"])
    assert {"upload", "analysis"} <= actions
    for statement in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text(statement))
