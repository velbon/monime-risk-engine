#!/usr/bin/env python3
"""Import a daily Monime payments CSV into Jube, with resumable state.

Usage:
    ./import_transactions.py <file.csv> [--limit N] [--dry-run] [--delete-on-success]

Setup (once per shell session):
    read -rs JUBE_PASSWORD && export JUBE_PASSWORD

The model GUID is read from MONIME_MODEL_GUID, or from the file
`.monime_model_guid` beside this script (written by create_monime_model.py).

Typical day:
    ./import_transactions.py payments-2026-09-17.csv --limit 1   # smoke test
    ./import_transactions.py payments-2026-09-17.csv             # full import
    rm payments-2026-09-17.csv                                   # reclaim space

If a run is interrupted, re-run the same command. The script reads
`payments-2026-09-17.csv.imported` (if it exists) to skip already-sent
transactions and resume from where it stopped.

Options:
    --limit N             Import only the first N rows.
    --dry-run             Print payloads; send nothing, no credentials needed.
    --delete-on-success   Delete the CSV only if every row returned HTTP 200.
    --reset-state         Delete the `.imported` file and start over.
"""
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path

import requests

BASE_URL = "http://localhost:5001"
AUTH_URL = f"{BASE_URL}/api/Authentication/ByUserNamePassword"
GUID_FILE = Path(__file__).resolve().parent / ".monime_model_guid"

# ---------------------------------------------------------------------------
# CSV layout
#
# The 2026-09-16 export was malformed: its header declared 16 columns but every
# data row carried only 15, because "Provider ID" values were never written.
# That shifts the last two fields left, so "Provider Name" holds the provider
# and "Create Time" reads as empty.
#
# A future export may well be fixed. Rather than assume either shape, the
# layout is detected per file from the first data row. Both layouts expose the
# same key names to build_payload(), so the mapping below is layout-agnostic.
# ---------------------------------------------------------------------------
LAYOUT_FIXED = [
    "ID", "Space ID", "Space Name", "Status", "Name", "Financial Account ID",
    "Amount", "Currency", "Total Charge Amount", "Reference",
    "Provider Txn Reference", "Order ID", "Order Number",
    "Provider ID", "Provider Name", "Create Time",
]

# Same as above with "Provider ID" absent; trailing names realigned to content.
LAYOUT_SHIFTED = [
    "ID", "Space ID", "Space Name", "Status", "Name", "Financial Account ID",
    "Amount", "Currency", "Total Charge Amount", "Reference",
    "Provider Txn Reference", "Order ID", "Order Number",
    "Provider Name", "Create Time",
]

# Columns build_payload() requires from whichever layout is in play.
REQUIRED_KEYS = {
    "ID", "Space ID", "Space Name", "Status", "Financial Account ID", "Amount",
    "Currency", "Total Charge Amount", "Reference", "Provider Txn Reference",
    "Order ID", "Order Number", "Provider Name", "Create Time",
}


def detect_layout(header, first_row):
    """Choose a column layout from the first data row's field count."""
    if len(first_row) == len(LAYOUT_FIXED):
        layout, note = LAYOUT_FIXED, "16 columns (export appears corrected)"
    elif len(first_row) == len(LAYOUT_SHIFTED):
        layout, note = LAYOUT_SHIFTED, "15 columns (known 'Provider ID' export defect)"
    else:
        raise SystemExit(
            f"Unrecognised CSV layout: header has {len(header)} columns, "
            f"data rows have {len(first_row)}.\n"
            f"Expected {len(LAYOUT_SHIFTED)} or {len(LAYOUT_FIXED)}. "
            "The export format has changed; update LAYOUT_* in this script."
        )

    missing = REQUIRED_KEYS - set(layout)
    if missing:
        raise SystemExit(f"Layout is missing required columns: {sorted(missing)}")

    # Guard against a wholesale schema change that happens to keep the count.
    if header and header[0].strip() != "ID":
        raise SystemExit(
            f"Unexpected first header column {header[0]!r} (expected 'ID'). "
            "Verify this is a Monime payments export."
        )

    return layout, note


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_payload(row):
    """Map CSV columns to the model's Request XPath names (case-sensitive)."""
    return {
        "TxnId": row["ID"],
        "TxnDateTime": row["Create Time"],
        "AccountId": row["Financial Account ID"],
        "Amount": to_float(row["Amount"]),
        "Currency": row["Currency"],
        "ChargeAmount": to_float(row["Total Charge Amount"]),
        "Status": row["Status"],
        "SpaceId": row["Space ID"],
        "SpaceName": row["Space Name"],
        "PaymentReference": row["Reference"],
        "ProviderTxnReference": row["Provider Txn Reference"],
        "OrderId": row["Order ID"],
        "OrderNumber": row["Order Number"],
        "ProviderName": row["Provider Name"],
    }


class JubeClient:
    """Holds a JWT and re-authenticates before it expires."""

    def __init__(self, username, password, api_url):
        self._username = username
        self._password = password
        self._api_url = api_url
        self._token = None
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self.session = requests.Session()

    def _authenticate(self):
        resp = self.session.post(
            AUTH_URL,
            json={"userName": self._username, "password": self._password},
            timeout=15,
        )
        if resp.status_code != 200:
            raise SystemExit(f"Authentication failed: HTTP {resp.status_code} {resp.text[:200]}")

        body = resp.json()
        self._token = body.get("token")
        if not self._token:
            raise SystemExit(f"Authentication response contained no token: {body}")

        raw_expiry = (body.get("expiration") or "").replace("Z", "+00:00")
        try:
            self._expires_at = datetime.fromisoformat(raw_expiry)
            if self._expires_at.tzinfo is None:
                self._expires_at = self._expires_at.replace(tzinfo=timezone.utc)
        except ValueError:
            self._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

        print(f"Authenticated as {self._username}; token expires {self._expires_at:%Y-%m-%d %H:%M:%S %Z}")

    def auth_header(self):
        if self._token is None or datetime.now(timezone.utc) >= self._expires_at - timedelta(seconds=60):
            self._authenticate()
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    def invoke(self, payload):
        return self.session.post(
            self._api_url, json=payload, headers=self.auth_header(), timeout=30
        )


def state_path(csv_path):
    """Sidecar file recording TxnIds already accepted by Jube for this CSV."""
    return csv_path.with_suffix(csv_path.suffix + ".imported")


def load_state(path):
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as handle:
        return {line.strip() for line in handle if line.strip()}


def resolve_model_guid():
    guid = os.environ.get("MONIME_MODEL_GUID", "").strip()
    if guid:
        return guid
    if GUID_FILE.exists():
        return GUID_FILE.read_text().strip()
    raise SystemExit(
        "No model GUID available.\n"
        "Run create_monime_model.py, then either:\n"
        "    export MONIME_MODEL_GUID=<guid>\n"
        f"  or write it to {GUID_FILE}"
    )


def parse_args(argv):
    if not argv or argv[0] in ("--help", "-h"):
        print(__doc__)
        sys.exit(0)

    csv_path = Path(argv[0])
    if not csv_path.exists():
        raise SystemExit(f"File not found: {csv_path}")

    limit, dry_run, delete_on_success, reset_state = None, False, False, False
    i = 1
    while i < len(argv):
        if argv[i] == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
            i += 2
        elif argv[i] == "--dry-run":
            dry_run = True
            i += 1
        elif argv[i] == "--delete-on-success":
            delete_on_success = True
            i += 1
        elif argv[i] == "--reset-state":
            reset_state = True
            i += 1
        else:
            raise SystemExit(f"Unrecognised argument: {argv[i]}\n{__doc__}")
    return csv_path, limit, dry_run, delete_on_success, reset_state


def open_rows(handle):
    """Return (layout, note, rows) with the first data row put back in place."""
    reader = csv.reader(handle)
    header = next(reader, None)
    if header is None:
        raise SystemExit("CSV is empty.")
    first_row = next(reader, None)
    if first_row is None:
        raise SystemExit("CSV has a header but no data rows.")
    layout, note = detect_layout(header, first_row)
    return layout, note, chain([first_row], reader)


def main():
    csv_path, limit, dry_run, delete_on_success, reset_state = parse_args(sys.argv[1:])

    if dry_run:
        with open(csv_path, encoding="utf-8", newline="") as handle:
            layout, note, rows = open_rows(handle)
            print(f"Layout: {note}\n")
            for count, raw in enumerate(rows, 1):
                if limit is not None and count > limit:
                    break
                if len(raw) != len(layout):
                    print(f"[{count}] Malformed row: {len(raw)} fields, expected {len(layout)}")
                    continue
                print(json.dumps(build_payload(dict(zip(layout, raw))), indent=2))
        return

    model_guid = resolve_model_guid()
    api_url = f"{BASE_URL}/api/Invoke/EntityAnalysisModel/{model_guid}"

    password = os.environ.get("JUBE_PASSWORD")
    if not password:
        raise SystemExit(
            "JUBE_PASSWORD is not set.\n"
            "Supply it without leaving it in shell history:\n"
            "    read -rs JUBE_PASSWORD && export JUBE_PASSWORD"
        )

    state_file = state_path(csv_path)
    if reset_state and state_file.exists():
        state_file.unlink()
        print(f"Removed {state_file}; starting from the first row.")

    already_sent = load_state(state_file)

    client = JubeClient(os.environ.get("JUBE_USER", "Administrator"), password, api_url)
    statuses = Counter()
    consecutive_auth_failures = 0
    resumed = 0

    print(f"Importing {csv_path} -> model {model_guid}")
    if already_sent:
        print(f"Resuming: {len(already_sent)} transactions already recorded in {state_file.name}")

    # Append-and-flush after every accepted row, so an interrupted run (Ctrl-C,
    # crash, laptop sleep) never re-sends what Jube already took.
    with open(csv_path, encoding="utf-8", newline="") as handle, \
            open(state_file, "a", encoding="utf-8") as state_handle:
        layout, note, rows = open_rows(handle)
        print(f"Layout: {note}\n")

        for count, raw in enumerate(rows, 1):
            if limit is not None and count > limit:
                break

            if len(raw) != len(layout):
                statuses["malformed_row"] += 1
                print(f"[{count}] Skipped: {len(raw)} fields, expected {len(layout)}")
                continue

            payload = build_payload(dict(zip(layout, raw)))
            txn_id = payload["TxnId"]

            if txn_id in already_sent:
                resumed += 1
                continue

            try:
                res = client.invoke(payload)
            except requests.RequestException as exc:
                statuses["request_error"] += 1
                print(f"[{count}] {txn_id}: {type(exc).__name__}: {exc}")
                continue

            statuses[res.status_code] += 1

            if res.status_code in (401, 403):
                consecutive_auth_failures += 1
                print(f"[{count}] {txn_id}: HTTP {res.status_code} {res.text[:200]}")
                if consecutive_auth_failures >= 3:
                    raise SystemExit("Aborting: three consecutive authorization failures.")
                continue

            consecutive_auth_failures = 0

            if res.status_code < 400:
                state_handle.write(f"{txn_id}\n")
                state_handle.flush()
                os.fsync(state_handle.fileno())

            if res.status_code >= 400:
                print(f"[{count}] {txn_id}: HTTP {res.status_code} {res.text[:200]}")
            elif limit is not None and limit <= 5:
                print(f"[{count}] {txn_id}: HTTP {res.status_code}")
                print(res.text)
            elif count % 500 == 0:
                print(f"[{count}] imported; running totals: {dict(statuses)}")

    sent = sum(statuses.values())
    ok = statuses.get(200, 0)
    print("\nFinished. Response breakdown:")
    for key, count in sorted(statuses.items(), key=lambda kv: str(kv[0])):
        print(f"  {key}: {count}")
    if resumed:
        print(f"  skipped (already imported): {resumed}")
    print(f"  {ok}/{sent} newly sent rows returned HTTP 200.")

    # A clean pass means nothing failed now and nothing is left outstanding.
    clean = (sent == 0 or ok == sent) and (resumed + ok) > 0

    if not clean:
        print(
            f"\nImport incomplete. State kept in {state_file.name};"
            f" re-run the same command to retry only what is missing."
        )
        return

    if limit is not None:
        print(f"\nPartial run (--limit {limit}). State kept in {state_file.name}.")
        return

    if not delete_on_success:
        print(f"\nImport clean. To reclaim space:\n    rm {csv_path} {state_file}")
        return

    csv_path.unlink()
    state_file.unlink()
    print(f"\nDeleted {csv_path} and {state_file.name}.")


if __name__ == "__main__":
    sys.exit(main())
