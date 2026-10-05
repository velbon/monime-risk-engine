"""Parse Monime payments CSV exports into one normalised DataFrame.

Monime has produced (at least) two export shapes:

* 13 columns with "Channel Txn Reference" and JavaScript-style timestamps such
  as "Wed Sep 16 2026 00:58:19 GMT+0000 (Greenwich Mean Time)".
* A 16-column header whose data rows carry only 15 fields. "Order Number" is
  never written, so the provider code (m17), provider name (Orange Money) and
  ISO timestamp land under "Order Number", "Provider ID" and "Provider Name",
  and "Create Time" reads as empty.

Both are mapped onto the same canonical columns so the rest of the system never
has to care which export it was given.
"""
import csv
import hashlib
import io
import re

import pandas as pd

# Canonical column -> header names seen in Monime exports.
ALIASES = {
    "txn_id": ["ID"],
    "space_id": ["Space ID"],
    "space_name": ["Space Name"],
    "status": ["Status"],
    "name": ["Name"],
    "financial_account_id": ["Financial Account ID"],
    "amount": ["Amount"],
    "currency": ["Currency"],
    "charge_amount": ["Total Charge Amount"],
    "reference": ["Reference"],
    "provider_txn_reference": ["Provider Txn Reference", "Channel Txn Reference"],
    "order_id": ["Order ID"],
    "order_number": ["Order Number"],
    "provider_code": ["Provider ID"],
    "provider_name": ["Provider Name"],
    "created_at": ["Create Time"],
}
HEADER_TO_CANONICAL = {h: c for c, headers in ALIASES.items() for h in headers}

# The defective export: rows are one field short, with Order Number missing.
DEFECTIVE_HEADER = [
    "ID", "Space ID", "Space Name", "Status", "Name", "Financial Account ID",
    "Amount", "Currency", "Total Charge Amount", "Reference",
    "Provider Txn Reference", "Order ID", "Order Number", "Provider ID",
    "Provider Name", "Create Time",
]
DEFECTIVE_ROW_COLUMNS = [
    "txn_id", "space_id", "space_name", "status", "name",
    "financial_account_id", "amount", "currency", "charge_amount",
    "reference", "provider_txn_reference", "order_id",
    "provider_code", "provider_name", "created_at",
]

REQUIRED = {"txn_id", "space_id", "space_name", "amount", "currency", "created_at"}

COLUMNS = list(ALIASES) + ["counterparty_msisdn", "trading_date"]

JS_DATE = re.compile(r"^\w{3} (\w{3} \d{1,2} \d{4} \d{2}:\d{2}:\d{2}) GMT([+-]\d{4})")
# Sierra Leone numbers: 232 + 8 digits, also written with a leading 0 or +232.
MSISDN = re.compile(r"(?<!\d)(?:\+?232|0)?(\d{8})(?!\d)")


class CsvFormatError(ValueError):
    pass


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_times(values: pd.Series) -> pd.Series:
    """Parse ISO and JavaScript Date.toString() timestamps to UTC."""
    values = values.fillna("").astype(str).str.strip()
    js = values.str.extract(JS_DATE)
    is_js = js[0].notna()
    out = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns, UTC]")
    if is_js.any():
        out[is_js] = pd.to_datetime(
            js.loc[is_js, 0] + " " + js.loc[is_js, 1],
            format="%b %d %Y %H:%M:%S %z", utc=True,
        )
    rest = ~is_js & (values != "")
    if rest.any():
        out[rest] = pd.to_datetime(values[rest], utc=True, errors="coerce", format="ISO8601")
    return out


def extract_msisdn(names: pd.Series) -> pd.Series:
    digits = names.fillna("").astype(str).str.extract(MSISDN)[0]
    return ("232" + digits).where(digits.notna())


def _blank_to_none(series: pd.Series) -> pd.Series:
    series = series.astype("string").str.strip()
    return series.mask(series == "")


def load_csv(data: bytes):
    """Return (DataFrame of canonical columns, layout description)."""
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        raise CsvFormatError("The file is empty.")
    header = [h.strip() for h in header]
    rows = [r for r in reader if any(field.strip() for field in r)]
    if not rows:
        raise CsvFormatError("The file has a header but no transactions.")

    width = len(rows[0])
    if width == len(header):
        unknown = [h for h in header if h not in HEADER_TO_CANONICAL]
        if unknown:
            raise CsvFormatError(f"Unrecognised columns {unknown}; is this a Monime payments export?")
        columns = [HEADER_TO_CANONICAL[h] for h in header]
        layout = f"{len(header)} columns"
    elif header == DEFECTIVE_HEADER and width == len(DEFECTIVE_ROW_COLUMNS):
        columns = DEFECTIVE_ROW_COLUMNS
        layout = "16-column header, 15-field rows (Order Number missing)"
    else:
        raise CsvFormatError(
            f"The header has {len(header)} columns but rows have {width} fields; "
            "the export format has changed."
        )

    bad = [i + 2 for i, r in enumerate(rows) if len(r) != width]
    if bad:
        raise CsvFormatError(f"Rows with a different number of fields at lines {bad[:10]}.")
    missing = REQUIRED - set(columns)
    if missing:
        raise CsvFormatError(f"Missing required columns: {sorted(missing)}")

    df = pd.DataFrame(rows, columns=columns)

    for col in ALIASES:
        if col not in df.columns:
            df[col] = None
        else:
            df[col] = _blank_to_none(df[col])

    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["charge_amount"] = pd.to_numeric(df["charge_amount"], errors="coerce")
    df["status"] = df["status"].str.lower()
    df["currency"] = df["currency"].str.upper()
    df["created_at"] = parse_times(df["created_at"])
    # Sierra Leone is on GMT all year, so the trading day is the UTC date.
    df["trading_date"] = df["created_at"].dt.date
    df["counterparty_msisdn"] = extract_msisdn(df["name"])

    if df["txn_id"].isna().any():
        raise CsvFormatError("Some rows have no transaction ID.")
    if df["amount"].isna().any():
        raise CsvFormatError("Some rows have a missing or non-numeric amount.")
    df = df.drop_duplicates("txn_id")
    return df[COLUMNS], layout
