"""Create a purpose-built Jube model for Monime payment transactions.

Run with credentials supplied via the environment:

    read -rs JUBE_PASSWORD && export JUBE_PASSWORD
    python3 create_monime_model.py

One-time operation. Prints the server-allocated model GUID at the end; put that
in transactions.py (or export it as MONIME_MODEL_GUID).

Property names below are taken from the live OpenAPI schema
(Jube.Dto.EntityAnalysisModel.EntityAnalysisModelDto), and the engine settings
mirror the working "Detailed Account Financial Transactions" model rather than
being invented.
"""
import os
import sys
from pathlib import Path

import requests

# import_transactions.py reads the GUID from here when MONIME_MODEL_GUID is unset.
GUID_FILE = Path(__file__).resolve().parent / ".monime_model_guid"

BASE_URL = "http://localhost:5001"
AUTH_URL = f"{BASE_URL}/api/Authentication/ByUserNamePassword"
MODEL_URL = f"{BASE_URL}/api/EntityAnalysisModel/"
XPATH_URL = f"{BASE_URL}/api/EntityAnalysisModelRequestXPath/"
SYNC_URL = f"{BASE_URL}/api/EntityAnalysisModelSynchronisationSchedule"

MODEL_NAME = "Monime Financial Transactions"

# Jube data types, per the Request XPath documentation:
# 1 String, 2 Integer, 3 Float, 4 Date, 5 Boolean, 6 Latitude, 7 Longitude
STRING, INTEGER, FLOAT, DATE, BOOLEAN = 1, 2, 3, 4, 5

# Exactly the 14 columns the Monime export actually provides -- nothing else.
#
# Defaults are deliberately empty. The whole point of a purpose-built model is
# that no field silently resolves to invented demo data; a blank is visible as
# missing, whereas "OlaRoseGoldPhone6" is not.
#
# (name, xpath, data type, search key)
FIELDS = [
    ("TxnId",                "$.TxnId",                STRING, False),  # entry key
    ("TxnDateTime",          "$.TxnDateTime",          DATE,   False),  # reference date
    ("AccountId",            "$.AccountId",            STRING, True),   # aggregation key
    ("Amount",               "$.Amount",               FLOAT,  False),
    ("Currency",             "$.Currency",             STRING, False),
    ("ChargeAmount",         "$.ChargeAmount",         FLOAT,  False),
    ("Status",               "$.Status",               STRING, False),
    ("SpaceId",              "$.SpaceId",              STRING, False),
    ("SpaceName",            "$.SpaceName",            STRING, False),
    ("PaymentReference",     "$.PaymentReference",     STRING, False),
    ("ProviderTxnReference", "$.ProviderTxnReference", STRING, False),
    ("OrderId",              "$.OrderId",              STRING, False),
    ("OrderNumber",          "$.OrderNumber",          STRING, False),
    ("ProviderName",         "$.ProviderName",         STRING, False),
]


def authenticate(session, username, password):
    resp = session.post(
        AUTH_URL, json={"userName": username, "password": password}, timeout=15
    )
    if resp.status_code != 200:
        raise SystemExit(f"Authentication failed: HTTP {resp.status_code} {resp.text[:300]}")
    token = resp.json().get("token")
    if not token:
        raise SystemExit("Authentication returned no token.")
    session.headers.update(
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    print(f"Authenticated as {username}.")


def find_existing_model(session):
    """Return (id, guid) if a model of this name already exists, else None."""
    resp = session.get(MODEL_URL, timeout=15)
    if resp.status_code != 200:
        return None
    for row in resp.json():
        if row.get("name") == MODEL_NAME:
            return row.get("id"), row.get("guid")
    return None


def create_model(session):
    # The server allocates the GUID; we read it back from the response.
    body = {
        "name": MODEL_NAME,
        "entryName": "TxnId",
        "entryXPath": "$.TxnId",
        "referenceDateName": "TxnDateTime",
        "referenceDateXPath": "$.TxnDateTime",
        # 1 = take the reference date from the payload XPath (not server "Now"),
        # which matters here because the CSV is a historical backfill.
        "referenceDatePayloadLocationTypeId": 1,
        "enableCache": True,
        "cacheFetchLimit": 100,
        "cacheTtlInterval": "d",
        "cacheTtlIntervalValue": 1,
        "enableTtlCounter": True,
        "enableRdbmsArchive": True,
        "enableActivationArchive": True,
        "enableSanctionCache": True,
        "maxResponseElevation": 10,
        "enableResponseElevationLimit": False,
        "enableActivationWatcher": True,
        "maxActivationWatcherInterval": "h",
        "maxActivationWatcherValue": 1,
        "maxActivationWatcherThreshold": 100,
        "activationWatcherSample": 1,
        "enableImplicitAsync": False,
        "enableTrace": False,
        "enableLogs": False,
        "enableSampling": False,
        "active": True,
        "locked": False,
    }
    resp = session.post(MODEL_URL, json=body, timeout=15)
    if resp.status_code not in (200, 201):
        raise SystemExit(f"Model creation failed: HTTP {resp.status_code} {resp.text[:500]}")
    result = resp.json()
    return result.get("id"), result.get("guid")


def add_xpaths(session, model_id):
    created, failed = 0, 0
    for name, xpath, data_type_id, is_search_key in FIELDS:
        body = {
            "entityAnalysisModelId": model_id,
            "name": name,
            "xPath": xpath,
            "dataTypeId": data_type_id,
            "defaultValue": "",
            "cache": True,
            "responsePayload": True,
            "reportTable": True,
            "searchKey": is_search_key,
            "active": True,
            "locked": False,
            "enableSuppression": False,
            "encryptionId": 0,
            "version": 1,
        }
        if is_search_key:
            # Mirrors the settings on the existing model's AccountId search key.
            body.update({
                "searchKeyTtlInterval": "d",
                "searchKeyTtlIntervalValue": 1,
                "searchKeyFetchLimit": 100,
                "searchKeyCache": False,
            })

        resp = session.post(XPATH_URL, json=body, timeout=15)
        if resp.status_code in (200, 201):
            print(f"  added  {name:22} {xpath:26} {'SEARCH KEY' if is_search_key else ''}")
            created += 1
        else:
            print(f"  FAILED {name:22} HTTP {resp.status_code} {resp.text[:300]}")
            failed += 1
    return created, failed


def main():
    password = os.environ.get("JUBE_PASSWORD")
    if not password:
        raise SystemExit(
            "JUBE_PASSWORD is not set.\n"
            "    read -rs JUBE_PASSWORD && export JUBE_PASSWORD"
        )
    username = os.environ.get("JUBE_USER", "Administrator")

    session = requests.Session()
    authenticate(session, username, password)

    existing = find_existing_model(session)
    if existing:
        raise SystemExit(
            f"A model named {MODEL_NAME!r} already exists "
            f"(id {existing[0]}, guid {existing[1]}).\n"
            "Delete it in the Jube UI first, or edit MODEL_NAME in this script."
        )

    print("\nCreating model...")
    model_id, model_guid = create_model(session)
    print(f"  id {model_id}, guid {model_guid}")

    print("\nAdding request XPaths...")
    created, failed = add_xpaths(session, model_id)
    print(f"\nCreated {created} of {len(FIELDS)} fields; {failed} failed.")
    if failed:
        raise SystemExit("Aborting before synchronisation due to failures above.")

    resp = session.post(SYNC_URL, json={}, timeout=15)
    if resp.status_code in (200, 201, 204):
        print("Model synchronisation scheduled.")
    else:
        print(
            f"Synchronisation returned HTTP {resp.status_code} {resp.text[:300]}\n"
            "Synchronise manually via Entity » Synchronisation in the Jube UI."
        )

    GUID_FILE.write_text(f"{model_guid}\n")

    print(f"\nModel GUID: {model_guid}")
    print(f"Written to {GUID_FILE}, so daily imports need no further setup:")
    print("    ./import_transactions.py <file.csv> --limit 1")


if __name__ == "__main__":
    sys.exit(main())
