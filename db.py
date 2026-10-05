"""Postgres connection and schema for the Monime AML store.

Set DATABASE_URL to a Postgres connection string, for example the one Neon
provides. The schema is created on first use and only ever added to.
"""
import os

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id              BIGSERIAL PRIMARY KEY,
    filename        TEXT NOT NULL,
    file_sha256     TEXT NOT NULL UNIQUE,
    uploaded_by     TEXT NOT NULL,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    layout          TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    inserted_count  INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id                 TEXT PRIMARY KEY,
    upload_id              BIGINT NOT NULL REFERENCES uploads(id),
    created_at             TIMESTAMPTZ,
    trading_date           DATE,
    space_id               TEXT NOT NULL,
    space_name             TEXT NOT NULL,
    status                 TEXT,
    name                   TEXT,
    counterparty_msisdn    TEXT,
    financial_account_id   TEXT,
    amount                 NUMERIC(18, 2) NOT NULL,
    currency               TEXT NOT NULL,
    charge_amount          NUMERIC(18, 2),
    reference              TEXT,
    provider_txn_reference TEXT,
    order_id               TEXT,
    order_number           TEXT,
    provider_code          TEXT,
    provider_name          TEXT
);
CREATE INDEX IF NOT EXISTS transactions_day_space ON transactions (trading_date, space_id, currency);
CREATE INDEX IF NOT EXISTS transactions_counterparty ON transactions (counterparty_msisdn)
    WHERE counterparty_msisdn IS NOT NULL;

CREATE TABLE IF NOT EXISTS merchant_profiles (
    space_id              TEXT PRIMARY KEY,
    space_name            TEXT,
    category              TEXT,
    industry_risk         TEXT CHECK (industry_risk IN ('high', 'medium', 'low')),
    declared_daily_volume NUMERIC(18, 2),
    declared_currency     TEXT NOT NULL DEFAULT 'SLE',
    min_score             INTEGER CHECK (min_score BETWEEN 0 AND 100),
    notes                 TEXT,
    updated_by            TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS risk_runs (
    id           BIGSERIAL PRIMARY KEY,
    trading_date DATE NOT NULL,
    run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_by       TEXT NOT NULL,
    upload_id    BIGINT REFERENCES uploads(id),
    settings     JSONB NOT NULL,
    coverage     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS risk_runs_day ON risk_runs (trading_date, id DESC);

CREATE TABLE IF NOT EXISTS merchant_scores (
    run_id             BIGINT NOT NULL REFERENCES risk_runs(id),
    space_id           TEXT NOT NULL,
    space_name         TEXT NOT NULL,
    currency           TEXT NOT NULL,
    txn_count          INTEGER NOT NULL,
    total_volume       NUMERIC(18, 2) NOT NULL,
    avg_ticket         NUMERIC(18, 2) NOT NULL,
    max_ticket         NUMERIC(18, 2) NOT NULL,
    high_value_count   INTEGER NOT NULL,
    structuring_count  INTEGER NOT NULL,
    off_hours_count    INTEGER NOT NULL,
    missing_ref_count  INTEGER NOT NULL,
    round_amount_count INTEGER NOT NULL,
    peak_window_count  INTEGER NOT NULL,
    industry_risk      TEXT NOT NULL,
    score              INTEGER NOT NULL,
    tier               TEXT NOT NULL,
    reserve_hold       NUMERIC(18, 2) NOT NULL,
    reasons            JSONB NOT NULL,
    PRIMARY KEY (run_id, space_id, currency)
);

CREATE TABLE IF NOT EXISTS alerts (
    id             BIGSERIAL PRIMARY KEY,
    trading_date   DATE NOT NULL,
    rule_code      TEXT NOT NULL,
    subject_type   TEXT NOT NULL,
    subject_id     TEXT NOT NULL,
    subject_name   TEXT,
    currency       TEXT NOT NULL,
    severity       TEXT NOT NULL,
    detail         TEXT NOT NULL,
    evidence       JSONB NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'investigating', 'closed - no issue', 'reported to FIU')),
    status_note    TEXT,
    first_run_id   BIGINT REFERENCES risk_runs(id),
    last_run_id    BIGINT REFERENCES risk_runs(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trading_date, rule_code, subject_type, subject_id, currency)
);
CREATE INDEX IF NOT EXISTS alerts_status ON alerts (status, trading_date DESC);

-- Append-only record of who did what. Updates and deletes are refused.
CREATE TABLE IF NOT EXISTS audit_log (
    id     BIGSERIAL PRIMARY KEY,
    at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'
);

CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();
"""


def database_url(url=None):
    url = url or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_engine(url=None, serverless=False):
    """Create an engine. serverless=True avoids holding pooled connections."""
    kwargs = {"poolclass": NullPool} if serverless else {"pool_pre_ping": True, "pool_size": 3}
    # Pooled connection strings (Neon's PgBouncer) can't rely on server-side prepared statements.
    return create_engine(database_url(url), connect_args={"prepare_threshold": None}, **kwargs)


def init_schema(engine):
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(727465)"))
        conn.exec_driver_sql(SCHEMA)
