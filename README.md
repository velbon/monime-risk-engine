# monime-risk-engine

AML and compliance tool for Monime payments. Daily CRM payment exports are
stored permanently in Postgres, scored against AML rules with a stated reason
for every point, and turned into alerts that analysts review. Every upload,
analysis, alert decision and profile change is written to an append-only audit
log.

## Parts

| File | Role |
|---|---|
| `streamlit_app.py` | Dashboard: upload exports, risk scores, alerts review, merchant profiles, audit trail, PDF report |
| `api/index.py` | FastAPI on Vercel: upload and read scores/alerts over HTTP |
| `pipeline.py` | Stores exports, runs the rules, reads results (used by both of the above) |
| `compliance_engine.py` | The AML rules, as pure functions |
| `aml_settings.toml` | Thresholds, keyword lists, points and tiers |
| `csv_loader.py` | Reads both known Monime export layouts |
| `db.py` | Postgres connection and schema (created automatically) |

## Rules

Per merchant, per currency, per trading day (GMT):

- **Industry**: from the merchant profile, otherwise guessed from whole words in the name
- **High value**: transactions at or above the currency's threshold
- **Structuring**: several transactions just below that threshold
- **Missing reference**: share of transactions without a payment reference
- **Off-hours**: share of transactions 23:00–05:00 compared with the platform as a whole
- **Round amounts**: most transactions are exact multiples of a round unit
- **Burst**: a sudden concentration of transactions within a few minutes
- **Volume spike**: day volume against the merchant's own 30-day median (needs 5 days of history)
- **New merchant**: high volume shortly after a merchant first appears
- **Declared volume**: volume above the declared daily volume in the merchant profile
- **Duplicate provider reference**: the same provider reference on several transactions

Per customer, where the export includes a phone number: one number dealing with
several merchants in a day, and repeated sub-threshold payments by one number.
Most Monime exports have no payer ID column, so these cover only a few percent
of transactions until the export includes one.

Scores add up to 0–100: Tier 3 at 66+, Tier 2 at 36+. Tune everything in
`aml_settings.toml`, and confirm the high-value thresholds against current
BSL/FIU reporting guidance.

## Setup

1. Create a Postgres database (for example Neon) and copy its connection string.
2. **Streamlit Community Cloud**: in the app's *Settings → Secrets* add

   ```toml
   DATABASE_URL = "postgresql://..."
   ```

3. **Vercel**: add the environment variables `DATABASE_URL` and `API_KEY`
   (a long random string). API calls must send `X-API-Key: <API_KEY>`, and
   should send `X-Actor: <name>` for the audit log. Vercel limits uploads to
   about 4.5 MB, so use the dashboard for larger exports.

The tables are created on first connection.

## API

```bash
curl -H "X-API-Key: $API_KEY" -H "X-Actor: jane" -F file=@payments.csv https://monime-risk-engine.vercel.app/api/upload
curl -H "X-API-Key: $API_KEY" "https://monime-risk-engine.vercel.app/api/scores?date=2026-09-15"
curl -H "X-API-Key: $API_KEY" "https://monime-risk-engine.vercel.app/api/alerts?status=open"
```

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
createdb monime_aml_test
TEST_DATABASE_URL=postgresql://localhost/monime_aml_test pytest
DATABASE_URL=postgresql://localhost/monime_aml_dev streamlit run streamlit_app.py
```

The database tests wipe `TEST_DATABASE_URL`, so point it at a throwaway database.
