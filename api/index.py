"""HTTP API for the Monime AML store.

Every /api route needs the header `X-API-Key` matching the API_KEY environment
variable; if API_KEY is not set, the routes refuse all requests. Send
`X-Actor: <your name>` so the audit log records who made the call.

Vercel caps request bodies at about 4.5 MB, so upload larger exports through
the dashboard instead.
"""
import hmac
import os
from datetime import date
from functools import lru_cache
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile

import pipeline
from csv_loader import CsvFormatError
from db import get_engine, init_schema

app = FastAPI(title="Monime AML API")


@lru_cache(maxsize=1)
def engine():
    eng = get_engine(serverless=True)
    init_schema(eng)
    return eng


def actor(x_api_key: Optional[str] = Header(None), x_actor: Optional[str] = Header(None)):
    expected = os.environ.get("API_KEY")
    if not expected:
        raise HTTPException(503, "API_KEY is not configured on the server.")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Missing or invalid X-API-Key.")
    return f"api:{(x_actor or 'unknown').strip()[:100]}"


def resolve_date(value: Optional[date]):
    if value:
        return value
    dates = pipeline.analysed_dates(engine())
    if not dates:
        raise HTTPException(404, "No data has been analysed yet.")
    return dates[0]


@app.get("/")
def home():
    return {"status": "Monime Compliance Engine API is Live"}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), who: str = Depends(actor)):
    data = await file.read()
    try:
        result = pipeline.ingest_and_analyze(engine(), file.filename or "upload.csv", data, who)
    except CsvFormatError as exc:
        raise HTTPException(422, str(exc))
    return result


# Kept for existing callers: same as /api/upload.
app.post("/api/analyze")(upload)


@app.get("/api/dates")
def dates(who: str = Depends(actor)):
    return {"dates": pipeline.analysed_dates(engine())}


@app.get("/api/scores")
def scores(trading_date: Optional[date] = Query(None, alias="date"), who: str = Depends(actor)):
    day = resolve_date(trading_date)
    run = pipeline.latest_run(engine(), day)
    if not run:
        raise HTTPException(404, f"No analysis for {day}.")
    df = pipeline.scores_for_run(engine(), run["id"])
    return {"trading_date": day, "run": run, "merchants": df.drop(columns=["run_id"]).to_dict("records")}


@app.get("/api/alerts")
def alerts(trading_date: Optional[date] = Query(None, alias="date"),
           status: Optional[list[str]] = Query(None), who: str = Depends(actor)):
    df = pipeline.list_alerts(engine(), trading_date, status)
    return {"alerts": df.to_dict("records")}
