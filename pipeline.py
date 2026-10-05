"""Store Monime exports in Postgres, run the AML rules, and read results back.

The dashboard (streamlit_app.py) and the API (api/index.py) both go through
these functions, so they always see the same data.
"""
import json
from datetime import timedelta

import pandas as pd
from sqlalchemy import text

from compliance_engine import load_settings, score_day
from csv_loader import file_sha256, load_csv

TXN_COLUMNS = [
    "txn_id", "created_at", "trading_date", "space_id", "space_name", "status",
    "name", "counterparty_msisdn", "financial_account_id", "amount", "currency",
    "charge_amount", "reference", "provider_txn_reference", "order_id",
    "order_number", "provider_code", "provider_name",
]
ALERT_STATUSES = ["open", "investigating", "closed - no issue", "reported to FIU"]


def _native(value):
    """Convert pandas/numpy scalars to plain Python values for the driver."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item"):
        return value.item()
    return value


def audit(conn, actor, action, detail=None):
    conn.execute(
        text("INSERT INTO audit_log (actor, action, detail) VALUES (:actor, :action, CAST(:detail AS jsonb))"),
        {"actor": actor, "action": action, "detail": json.dumps(detail or {}, default=str)},
    )


def ingest(engine, filename, data, actor):
    """Store an export. Re-uploading the same file, or overlapping exports, never duplicates rows."""
    sha = file_sha256(data)
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id, uploaded_at, uploaded_by FROM uploads WHERE file_sha256 = :sha"), {"sha": sha}
        ).first()
        if existing:
            return {"upload_id": existing.id, "already_uploaded": True,
                    "uploaded_at": existing.uploaded_at, "uploaded_by": existing.uploaded_by,
                    "rows": 0, "inserted": 0, "duplicates": 0, "dates": []}

        df, layout = load_csv(data)
        upload_id = conn.execute(
            text("""INSERT INTO uploads (filename, file_sha256, uploaded_by, layout, row_count,
                                         inserted_count, duplicate_count)
                    VALUES (:f, :sha, :by, :layout, :rows, 0, 0) RETURNING id"""),
            {"f": filename, "sha": sha, "by": actor, "layout": layout, "rows": len(df)},
        ).scalar_one()

        columns = TXN_COLUMNS + ["upload_id"]
        cursor = conn.connection.driver_connection.cursor()
        cursor.execute("CREATE TEMP TABLE staging (LIKE transactions) ON COMMIT DROP")
        with cursor.copy(f"COPY staging ({', '.join(columns)}) FROM STDIN") as copy:
            for record in df.to_dict("records"):
                copy.write_row([_native(record[c]) for c in TXN_COLUMNS] + [upload_id])
        cursor.execute("""INSERT INTO transactions SELECT * FROM staging
                          ON CONFLICT (txn_id) DO NOTHING RETURNING trading_date""")
        inserted_dates = [row[0] for row in cursor.fetchall()]
        cursor.close()

        inserted = len(inserted_dates)
        conn.execute(
            text("UPDATE uploads SET inserted_count = :i, duplicate_count = :d WHERE id = :id"),
            {"i": inserted, "d": len(df) - inserted, "id": upload_id},
        )
        dates = sorted({d for d in inserted_dates if d is not None})
        audit(conn, actor, "upload", {"upload_id": upload_id, "filename": filename, "rows": len(df),
                                      "inserted": inserted, "dates": dates})

    return {"upload_id": upload_id, "already_uploaded": False, "layout": layout, "rows": len(df),
            "inserted": inserted, "duplicates": len(df) - inserted, "dates": dates}


def analyze_date(engine, trading_date, actor, upload_id=None, settings=None):
    """Score one trading day from stored data and record the run, scores and alerts."""
    settings = settings or load_settings()
    lookback = settings["rules"]["spike_lookback_days"]
    with engine.connect() as conn:
        txns = pd.read_sql(
            text(f"SELECT {', '.join(TXN_COLUMNS)} FROM transactions WHERE trading_date = :d"),
            conn, params={"d": trading_date},
        )
        history = pd.read_sql(
            text("""SELECT space_id, currency, trading_date, SUM(amount) AS volume
                    FROM transactions
                    WHERE status = 'completed' AND trading_date >= :start AND trading_date < :d
                    GROUP BY space_id, currency, trading_date"""),
            conn, params={"d": trading_date, "start": trading_date - timedelta(days=lookback)},
        )
        seen = conn.execute(
            text("SELECT space_id, MIN(trading_date) FROM transactions GROUP BY space_id")
        ).all()
        profiles = pd.read_sql(text("SELECT * FROM merchant_profiles"), conn)

    for frame, cols in ((txns, ["amount", "charge_amount"]), (history, ["volume"])):
        for col in cols:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype(float)
    profiles["declared_daily_volume"] = pd.to_numeric(profiles["declared_daily_volume"], errors="coerce")
    txns["created_at"] = pd.to_datetime(txns["created_at"], utc=True)
    first_seen = {space: day for space, day in seen}
    if seen:
        first_seen["global"] = min(day for _, day in seen)

    result = score_day(txns, history, first_seen, profiles, settings)

    with engine.begin() as conn:
        run_id = conn.execute(
            text("""INSERT INTO risk_runs (trading_date, run_by, upload_id, settings, coverage)
                    VALUES (:d, :by, :u, CAST(:s AS jsonb), CAST(:c AS jsonb)) RETURNING id"""),
            {"d": trading_date, "by": actor, "u": upload_id,
             "s": json.dumps(settings), "c": json.dumps(result.coverage)},
        ).scalar_one()

        if not result.scores.empty:
            score_rows = []
            for record in result.scores.to_dict("records"):
                row = {k: _native(v) for k, v in record.items() if k != "reasons"}
                row.update(run_id=run_id, reasons=json.dumps(record["reasons"]))
                score_rows.append(row)
            conn.execute(text("""
                INSERT INTO merchant_scores (run_id, space_id, space_name, currency, txn_count,
                    total_volume, avg_ticket, max_ticket, high_value_count, structuring_count,
                    off_hours_count, missing_ref_count, round_amount_count, peak_window_count,
                    industry_risk, score, tier, reserve_hold, reasons)
                VALUES (:run_id, :space_id, :space_name, :currency, :txn_count, :total_volume,
                    :avg_ticket, :max_ticket, :high_value_count, :structuring_count,
                    :off_hours_count, :missing_ref_count, :round_amount_count, :peak_window_count,
                    :industry_risk, :score, :tier, :reserve_hold, CAST(:reasons AS jsonb))"""),
                score_rows)

        new_alerts = 0
        for a in result.alerts:
            inserted = conn.execute(text("""
                INSERT INTO alerts (trading_date, rule_code, subject_type, subject_id, subject_name,
                    currency, severity, detail, evidence, first_run_id, last_run_id)
                VALUES (:d, :rule_code, :subject_type, :subject_id, :subject_name, :currency,
                    :severity, :detail, CAST(:evidence AS jsonb), :run, :run)
                ON CONFLICT (trading_date, rule_code, subject_type, subject_id, currency) DO UPDATE
                SET severity = EXCLUDED.severity, detail = EXCLUDED.detail,
                    evidence = EXCLUDED.evidence, subject_name = EXCLUDED.subject_name,
                    last_run_id = EXCLUDED.last_run_id, updated_at = now()
                RETURNING (xmax = 0) AS inserted"""),
                {**a, "evidence": json.dumps(a["evidence"]), "d": trading_date, "run": run_id},
            ).scalar_one()
            new_alerts += int(inserted)

        audit(conn, actor, "analysis", {"run_id": run_id, "trading_date": trading_date,
                                        "merchants": len(result.scores), "alerts": len(result.alerts),
                                        "new_alerts": new_alerts})

    return {"run_id": run_id, "trading_date": trading_date, "merchants": len(result.scores),
            "alerts": len(result.alerts), "new_alerts": new_alerts, "coverage": result.coverage}


def ingest_and_analyze(engine, filename, data, actor):
    upload = ingest(engine, filename, data, actor)
    runs = [analyze_date(engine, d, actor, upload["upload_id"]) for d in upload["dates"]]
    return {"upload": upload, "runs": runs}


# --- Reads -----------------------------------------------------------------

def analysed_dates(engine):
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            text("SELECT DISTINCT trading_date FROM risk_runs ORDER BY trading_date DESC"))]


def latest_run(engine, trading_date):
    with engine.connect() as conn:
        row = conn.execute(text("""SELECT id, run_at, run_by, coverage FROM risk_runs
                                   WHERE trading_date = :d ORDER BY id DESC LIMIT 1"""),
                           {"d": trading_date}).mappings().first()
    return dict(row) if row else None


def scores_for_run(engine, run_id):
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT * FROM merchant_scores WHERE run_id = :r ORDER BY score DESC"),
                         conn, params={"r": run_id})
    for col in ("total_volume", "avg_ticket", "max_ticket", "reserve_hold"):
        df[col] = df[col].astype(float)
    return df


def transactions_for_day(engine, trading_date, columns=("space_name", "currency", "amount", "created_at", "provider_name")):
    with engine.connect() as conn:
        df = pd.read_sql(text(f"SELECT {', '.join(columns)} FROM transactions WHERE trading_date = :d"),
                         conn, params={"d": trading_date})
    if "amount" in df:
        df["amount"] = df["amount"].astype(float)
    return df


def list_alerts(engine, trading_date=None, statuses=None):
    clauses, params = [], {}
    if trading_date is not None:
        clauses.append("trading_date = :d")
        params["d"] = trading_date
    if statuses:
        clauses.append("status = ANY(:statuses)")
        params["statuses"] = list(statuses)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT id, trading_date, severity, rule_code, subject_type, subject_id, subject_name,
                   currency, detail, status, status_note, evidence, updated_at
            FROM alerts {where}
            ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id"""),
            conn, params=params)


def update_alert(engine, alert_id, status, note, actor):
    if status not in ALERT_STATUSES:
        raise ValueError(f"Unknown status {status!r}")
    with engine.begin() as conn:
        before = conn.execute(text("SELECT status, status_note FROM alerts WHERE id = :id FOR UPDATE"),
                              {"id": alert_id}).first()
        if before is None:
            raise ValueError(f"No alert {alert_id}")
        conn.execute(text("""UPDATE alerts SET status = :s, status_note = :n, updated_at = now()
                             WHERE id = :id"""), {"s": status, "n": note, "id": alert_id})
        audit(conn, actor, "alert_status", {"alert_id": alert_id, "from": before.status,
                                            "to": status, "note": note})


def merchant_profiles(engine):
    """Every merchant seen in the data, with its profile fields (blank when none saved)."""
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT s.space_id, COALESCE(p.space_name, s.space_name) AS space_name,
                   p.category, p.industry_risk, p.declared_daily_volume, p.declared_currency,
                   p.min_score, p.notes, p.updated_by, p.updated_at
            FROM (SELECT DISTINCT ON (space_id) space_id, space_name FROM transactions
                  ORDER BY space_id, created_at DESC NULLS LAST) s
            LEFT JOIN merchant_profiles p USING (space_id)
            ORDER BY 2"""), conn)
    df["declared_daily_volume"] = pd.to_numeric(df["declared_daily_volume"], errors="coerce")
    df["min_score"] = pd.to_numeric(df["min_score"], errors="coerce").astype("Int64")
    return df


PROFILE_FIELDS = ["category", "industry_risk", "declared_daily_volume", "declared_currency", "min_score", "notes"]


def save_profile(engine, space_id, space_name, fields, actor):
    values = {f: _native(fields.get(f)) for f in PROFILE_FIELDS}
    values["declared_currency"] = values["declared_currency"] or "SLE"
    if values["industry_risk"] not in (None, "high", "medium", "low"):
        raise ValueError("industry_risk must be high, medium or low")
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO merchant_profiles (space_id, space_name, category, industry_risk,
                declared_daily_volume, declared_currency, min_score, notes, updated_by, updated_at)
            VALUES (:space_id, :space_name, :category, :industry_risk, :declared_daily_volume,
                :declared_currency, :min_score, :notes, :actor, now())
            ON CONFLICT (space_id) DO UPDATE SET space_name = EXCLUDED.space_name,
                category = EXCLUDED.category, industry_risk = EXCLUDED.industry_risk,
                declared_daily_volume = EXCLUDED.declared_daily_volume,
                declared_currency = EXCLUDED.declared_currency, min_score = EXCLUDED.min_score,
                notes = EXCLUDED.notes, updated_by = EXCLUDED.updated_by, updated_at = now()"""),
            {**values, "space_id": space_id, "space_name": space_name, "actor": actor})
        audit(conn, actor, "merchant_profile", {"space_id": space_id, "space_name": space_name, **values})


def list_uploads(engine):
    with engine.connect() as conn:
        return pd.read_sql(text("""SELECT id, filename, uploaded_by, uploaded_at, layout, row_count,
                                          inserted_count, duplicate_count
                                   FROM uploads ORDER BY id DESC"""), conn)


def audit_trail(engine, limit=200):
    with engine.connect() as conn:
        return pd.read_sql(text("SELECT at, actor, action, detail FROM audit_log ORDER BY id DESC LIMIT :n"),
                           conn, params={"n": limit})
