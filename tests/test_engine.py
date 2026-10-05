from datetime import date, timedelta

import pandas as pd
import pytest

from compliance_engine import TIER_HIGH, TIER_LOW, industry_from_name, load_settings, score_day
from csv_loader import CsvFormatError, load_csv

SETTINGS = load_settings()


def txns(rows):
    """Build a day of transactions from (space_name, amount, 'HH:MM:SS', extras) tuples."""
    records = []
    for i, row in enumerate(rows):
        space, amount, clock = row[:3]
        extra = row[3] if len(row) > 3 else {}
        records.append({
            "txn_id": f"t{i}", "space_id": f"spc-{space}", "space_name": space,
            "status": "completed", "amount": float(amount), "currency": "SLE",
            "reference": f"ref{i}", "provider_txn_reference": f"p{i}",
            "counterparty_msisdn": None,
            "created_at": pd.Timestamp(f"2026-09-15 {clock}", tz="UTC"),
            **extra,
        })
    return pd.DataFrame(records)


def reasons(result, space):
    row = result.scores[result.scores["space_name"] == space].iloc[0]
    return {r["rule"]: r["points"] for r in row["reasons"]}


def rules_fired(result):
    return {a["rule_code"] for a in result.alerts}


def test_industry_keywords_match_whole_words_only():
    assert industry_from_name("The Betts Insurance", SETTINGS) == ("unknown", None)
    assert industry_from_name("ELEPHANT BET", SETTINGS) == ("high", "BET")
    assert industry_from_name("Core Fitness Gym", SETTINGS)[0] == "medium"


def test_profile_industry_overrides_name_guess():
    day = txns([("RaySwap Company Limited", 50, "10:00:00")])
    profiles = pd.DataFrame([{"space_id": "spc-RaySwap Company Limited", "industry_risk": "high"}])
    assert reasons(score_day(day, profiles=profiles), "RaySwap Company Limited")["INDUSTRY"] == 25


def test_single_large_ticket_is_not_medium_risk_on_its_own():
    result = score_day(txns([("Mams Radiance", 1250, "10:00:00")]))
    assert result.scores.iloc[0]["tier"] == TIER_LOW


def test_structuring_just_below_threshold():
    day = txns([("Shop", 9500, f"10:0{i}:00") for i in range(3)])
    result = score_day(day)
    assert "STRUCTURING" in reasons(result, "Shop")
    assert "STRUCTURING" in rules_fired(result)


def test_high_value_points_are_capped():
    day = txns([("Shop", 20000, f"10:{i:02d}:00") for i in range(20)])
    assert reasons(score_day(day), "Shop")["HIGH_VALUE"] == SETTINGS["points"]["high_value_max"]


def test_missing_reference_share():
    day = txns([("Shop", 10, f"10:0{i}:00", {"reference": None}) for i in range(6)])
    assert reasons(score_day(day), "Shop")["MISSING_REFERENCE"] == SETTINGS["points"]["missing_ref_max"]


def test_off_hours_is_relative_to_platform():
    night = [("Night", 10, f"02:{i:02d}:00") for i in range(25)]
    day_shop = [("Day", 10, f"12:{i:02d}:00") for i in range(50)]
    result = score_day(txns(night + day_shop))
    assert "OFF_HOURS" in reasons(result, "Night")
    assert "OFF_HOURS" not in reasons(result, "Day")


def test_busy_merchant_is_not_a_burst_but_a_sudden_spike_is():
    steady = [("Steady", 10, f"{8 + i // 60:02d}:{i % 60:02d}:00") for i in range(600)]
    spiky = [("Spiky", 10, f"{8 + i:02d}:00:00") for i in range(10)]
    spiky += [("Spiky", 10, f"15:00:{i:02d}") for i in range(40)]
    result = score_day(txns(steady + spiky))
    assert "BURST" not in reasons(result, "Steady")
    assert "BURST" in reasons(result, "Spiky")


def test_volume_spike_needs_history():
    day = txns([("Shop", 1000, f"10:0{i}:00") for i in range(5)])
    assert "VOLUME_SPIKE" not in reasons(score_day(day), "Shop")
    history = pd.DataFrame([
        {"space_id": "spc-Shop", "currency": "SLE", "trading_date": date(2026, 9, 1) + timedelta(days=i), "volume": 1000.0}
        for i in range(6)
    ])
    result = score_day(day, history=history)
    assert reasons(result, "Shop")["VOLUME_SPIKE"] == SETTINGS["points"]["volume_spike"]


def test_declared_volume_uses_profile_and_currency():
    day = txns([("Shop", 1000, f"10:0{i}:00") for i in range(5)])
    profile = {"space_id": "spc-Shop", "declared_daily_volume": 2000.0, "declared_currency": "SLE"}
    assert "DECLARED_VOLUME" in reasons(score_day(day, profiles=pd.DataFrame([profile])), "Shop")
    profile["declared_currency"] = "USD"
    assert "DECLARED_VOLUME" not in reasons(score_day(day, profiles=pd.DataFrame([profile])), "Shop")


def test_new_merchant_only_once_system_has_older_data():
    day = txns([("Shop", 60000, "10:00:00")])
    seen = {"spc-Shop": date(2026, 9, 15)}
    assert "NEW_MERCHANT" not in reasons(score_day(day, first_seen={**seen, "global": date(2026, 9, 15)}), "Shop")
    assert "NEW_MERCHANT" in reasons(score_day(day, first_seen={**seen, "global": date(2026, 9, 1)}), "Shop")


def test_currencies_are_scored_separately():
    day = txns([("Shop", 100, "10:00:00"), ("Shop", 600, "10:01:00", {"currency": "USD"})])
    result = score_day(day)
    assert sorted(result.scores["currency"]) == ["SLE", "USD"]
    usd = result.scores[result.scores["currency"] == "USD"].iloc[0]
    assert "HIGH_VALUE" in {r["rule"] for r in usd["reasons"]}


def test_counterparty_across_merchants():
    phone = {"counterparty_msisdn": "23233825179"}
    day = txns([(s, 10, "10:00:00", phone) for s in ("A", "B", "C")])
    assert "COUNTERPARTY_MULTI_MERCHANT" in rules_fired(score_day(day))


def test_min_score_from_profile():
    day = txns([("Shop", 10, "10:00:00")])
    profiles = pd.DataFrame([{"space_id": "spc-Shop", "min_score": 70}])
    row = score_day(day, profiles=profiles).scores.iloc[0]
    assert row["score"] == 70 and row["tier"] == TIER_HIGH


def test_loader_handles_both_export_layouts():
    js = (b"ID,Space ID,Space Name,Status,Name,Financial Account ID,Amount,Currency,Total Charge Amount,"
          b"Reference,Channel Txn Reference,Order Number,Create Time\n"
          b"spm-1,spc-1,Shop,completed,Transfer to 23233825179,fac-1,10000,SLE,80,R1,MP1,O1,"
          b"Wed Sep 16 2026 00:58:19 GMT+0000 (Greenwich Mean Time)\n")
    df, _ = load_csv(js)
    assert df.iloc[0]["created_at"] == pd.Timestamp("2026-09-16 00:58:19", tz="UTC")
    assert df.iloc[0]["counterparty_msisdn"] == "23233825179"

    shifted = (b"ID,Space ID,Space Name,Status,Name,Financial Account ID,Amount,Currency,Total Charge Amount,"
               b"Reference,Provider Txn Reference,Order ID,Order Number,Provider ID,Provider Name,Create Time\n"
               b"spm-2,spc-1,Shop,completed,,fac-1,16.2,SLE,0.13,,MP2,O2,m17,Orange Money,2026-09-16T01:04:55Z\n")
    df, layout = load_csv(shifted)
    row = df.iloc[0]
    assert "15-field" in layout
    assert (row["provider_name"], row["provider_code"]) == ("Orange Money", "m17")
    assert row["created_at"] == pd.Timestamp("2026-09-16 01:04:55", tz="UTC")
    assert row["reference"] is None or pd.isna(row["reference"])


def test_loader_rejects_unknown_files():
    with pytest.raises(CsvFormatError):
        load_csv(b"foo,bar\n1,2\n")
