"""AML rules for one trading day of Monime payments.

Everything here is a pure function of DataFrames, so the rules can be tested
without a database. pipeline.py fetches the inputs and stores the results.

Scores are built per merchant (Space) and currency from explicit rules; every
point awarded carries a reason, and every notable finding becomes an alert.
"""
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

SETTINGS_PATH = Path(__file__).resolve().parent / "aml_settings.toml"

TIER_HIGH = "Tier 3 (High Risk - EDD & Hold)"
TIER_MEDIUM = "Tier 2 (Medium Risk - Reserve & Review)"
TIER_LOW = "Tier 1 (Low Risk - Auto Settlement)"


def load_settings(path=SETTINGS_PATH):
    with open(path, "rb") as handle:
        return tomllib.load(handle)


@dataclass
class DayResult:
    scores: pd.DataFrame
    alerts: list
    coverage: dict = field(default_factory=dict)


def industry_from_name(name, settings):
    """Guess an industry risk level from whole words in the merchant name."""
    words = set(re.findall(r"[A-Z0-9]+", str(name).upper()))
    for level in ("high", "medium", "low"):
        if words & set(settings["industry"][level]):
            return level, sorted(words & set(settings["industry"][level]))[0]
    return "unknown", None


def tier_for(score, settings):
    if score >= settings["tiers"]["high"]:
        return TIER_HIGH
    if score >= settings["tiers"]["medium"]:
        return TIER_MEDIUM
    return TIER_LOW


def reserve_for(score, volume, settings):
    if score >= settings["tiers"]["high"]:
        return round(volume * settings["reserve"]["high"], 2)
    if score >= settings["tiers"]["medium"]:
        return round(volume * settings["reserve"]["medium"], 2)
    return 0.0


def peak_window_count(times, minutes):
    """Most transactions inside any rolling window of the given length."""
    if len(times) == 0:
        return 0
    series = pd.Series(1, index=pd.DatetimeIndex(times).sort_values())
    return int(series.rolling(f"{minutes}min").sum().max())


def _fmt(amount):
    return f"{amount:,.2f}"


def score_day(txns, history=None, first_seen=None, profiles=None, settings=None):
    """Score one trading day.

    txns       transactions for the day (csv_loader columns); only completed
               payments are scored.
    history    daily totals before this day: space_id, currency, trading_date, volume.
    first_seen {"global": date, space_id: date} of the earliest stored day.
    profiles   merchant_profiles rows: space_id, industry_risk, category,
               declared_daily_volume, declared_currency, min_score.
    """
    settings = settings or load_settings()
    rules, points = settings["rules"], settings["points"]
    history = history if history is not None else pd.DataFrame(
        columns=["space_id", "currency", "trading_date", "volume"])
    first_seen = first_seen or {}
    profiles = (profiles if profiles is not None else pd.DataFrame(columns=["space_id"])).set_index("space_id")

    df = txns[txns["status"].fillna("completed") == "completed"].copy()
    alerts, rows = [], []

    if df.empty:
        return DayResult(pd.DataFrame(), [], {"transactions": 0})

    df["hour"] = df["created_at"].dt.hour
    df["is_off_hours"] = df["hour"].isin(rules["off_hours"])
    df["is_missing_ref"] = df["reference"].isna()
    platform_off_share = df["is_off_hours"].mean()

    def alert(rule, subject_type, subject_id, subject_name, currency, severity, detail, evidence=None):
        alerts.append({
            "rule_code": rule, "subject_type": subject_type, "subject_id": subject_id,
            "subject_name": subject_name, "currency": currency, "severity": severity,
            "detail": detail, "evidence": evidence or {},
        })

    # Provider references should be unique; repeats suggest double posting.
    dup_refs = df[df["provider_txn_reference"].notna()
                  & df.duplicated("provider_txn_reference", keep=False)]
    dup_ref_spaces = set(dup_refs["space_id"])
    for ref, group in dup_refs.groupby("provider_txn_reference"):
        first = group.iloc[0]
        alert("DUPLICATE_PROVIDER_REF", "transaction", ref, first["space_name"], first["currency"],
              "medium", f"Provider reference {ref} appears on {len(group)} transactions.",
              {"txn_ids": group["txn_id"].tolist()})

    for (space_id, currency), g in df.groupby(["space_id", "currency"], sort=False):
        name = g["space_name"].iloc[0]
        cur = settings["currency"].get(currency)
        n = len(g)
        volume = float(g["amount"].sum())
        reasons = []

        def add(rule, pts, detail):
            reasons.append({"rule": rule, "points": int(pts), "detail": detail})

        profile = profiles.loc[space_id] if space_id in profiles.index else None

        # Industry
        level = profile["industry_risk"] if profile is not None and pd.notna(profile.get("industry_risk")) else None
        if level:
            add("INDUSTRY", points[f"industry_{level}"], f"{level.title()}-risk industry (merchant profile).")
        else:
            level, keyword = industry_from_name(name, settings)
            if keyword:
                add("INDUSTRY", points[f"industry_{level}"],
                    f"{level.title()}-risk industry guessed from the word '{keyword}' in the name.")
            else:
                add("INDUSTRY", points["industry_unknown"], "Industry unknown: no merchant profile.")

        high_value_count = band_count = round_count = 0
        if cur:
            hv = cur["high_value"]
            high_value_count = int((g["amount"] >= hv).sum())
            if high_value_count:
                add("HIGH_VALUE", min(high_value_count * points["high_value_each"], points["high_value_max"]),
                    f"{high_value_count} transaction(s) of {_fmt(hv)} {currency} or more.")
                alert("HIGH_VALUE", "merchant", space_id, name, currency, "medium",
                      f"{high_value_count} transaction(s) at or above {_fmt(hv)} {currency}.",
                      {"txn_ids": g.loc[g["amount"] >= hv, "txn_id"].tolist()[:50]})

            band = g[(g["amount"] >= hv * rules["structuring_band"]) & (g["amount"] < hv)]
            band_count = len(band)
            if band_count >= rules["structuring_min_count"]:
                add("STRUCTURING", points["structuring"],
                    f"{band_count} transactions just below the {_fmt(hv)} threshold.")
                alert("STRUCTURING", "merchant", space_id, name, currency, "high",
                      f"{band_count} transactions between {_fmt(hv * rules['structuring_band'])} and "
                      f"{_fmt(hv)} {currency}: possible structuring.",
                      {"txn_ids": band["txn_id"].tolist()[:50]})

            unit = cur["round_amount_unit"]
            round_count = int(((g["amount"] >= unit) & (g["amount"] % unit == 0)).sum())
            if round_count >= rules["round_amount_min_count"] and round_count / n >= rules["round_amount_min_share"]:
                add("ROUND_AMOUNTS", points["round_amounts"],
                    f"{round_count} of {n} transactions are round multiples of {_fmt(unit)}.")
                alert("ROUND_AMOUNTS", "merchant", space_id, name, currency, "medium",
                      f"{round_count} of {n} transactions are exact multiples of {_fmt(unit)} {currency}.")
        else:
            add("NO_THRESHOLDS", 0, f"No thresholds configured for {currency}; value rules skipped.")

        missing_ref = int(g["is_missing_ref"].sum())
        if n >= rules["missing_ref_min_txns"] and missing_ref / n >= rules["missing_ref_min_share"]:
            share = missing_ref / n
            add("MISSING_REFERENCE", round(points["missing_ref_max"] * share),
                f"{missing_ref} of {n} transactions ({share:.0%}) have no reference.")
            alert("MISSING_REFERENCE", "merchant", space_id, name, currency,
                  "medium" if share >= 0.5 else "low",
                  f"{missing_ref} of {n} transactions have no payment reference.")

        off_hours = int(g["is_off_hours"].sum())
        off_share = off_hours / n
        if (n >= rules["off_hours_min_txns"] and off_share >= rules["off_hours_min_share"]
                and off_share >= platform_off_share * rules["off_hours_platform_multiple"]):
            add("OFF_HOURS", points["off_hours"],
                f"{off_share:.0%} of transactions off-hours vs {platform_off_share:.0%} platform-wide.")
            alert("OFF_HOURS", "merchant", space_id, name, currency, "medium",
                  f"{off_hours} of {n} transactions ({off_share:.0%}) between 23:00 and 05:00 GMT; "
                  f"platform average is {platform_off_share:.0%}.")

        peak = peak_window_count(g["created_at"].dropna(), rules["burst_window_minutes"])
        span_minutes = max((g["created_at"].max() - g["created_at"].min()).total_seconds() / 60,
                           rules["burst_window_minutes"])
        avg_window = n / (span_minutes / rules["burst_window_minutes"])
        if peak >= rules["burst_min_txns"] and peak >= avg_window * rules["burst_multiple"]:
            add("BURST", points["burst"],
                f"{peak} transactions within {rules['burst_window_minutes']} minutes "
                f"(usual {avg_window:.1f}).")
            alert("BURST", "merchant", space_id, name, currency, "medium",
                  f"Peak of {peak} transactions in {rules['burst_window_minutes']} minutes, "
                  f"{peak / avg_window:.0f}x the merchant's average for the day.")

        past = history[(history["space_id"] == space_id) & (history["currency"] == currency)]
        if len(past) >= rules["spike_min_history_days"]:
            median = float(past["volume"].median())
            if median > 0 and volume / median >= rules["spike_ratio"]:
                add("VOLUME_SPIKE", points["volume_spike"],
                    f"Volume {_fmt(volume)} is {volume / median:.1f}x the {len(past)}-day median of {_fmt(median)}.")
                alert("VOLUME_SPIKE", "merchant", space_id, name, currency, "high",
                      f"Daily volume {_fmt(volume)} {currency} is {volume / median:.1f}x its median "
                      f"of {_fmt(median)} over the previous {len(past)} trading days.")

        merchant_first, global_first = first_seen.get(space_id), first_seen.get("global")
        if (cur and merchant_first and global_first and merchant_first > global_first
                and len(past) < rules["new_merchant_days"] and volume >= cur["new_merchant_volume"]):
            add("NEW_MERCHANT", points["new_merchant"],
                f"First seen {merchant_first}; already {_fmt(volume)} {currency} in a day.")
            alert("NEW_MERCHANT", "merchant", space_id, name, currency, "medium",
                  f"Merchant first seen on {merchant_first} processed {_fmt(volume)} {currency} today.")

        declared = profile.get("declared_daily_volume") if profile is not None else None
        declared_cur = (profile.get("declared_currency") or "SLE") if profile is not None else None
        if declared is not None and pd.notna(declared) and declared > 0 and declared_cur == currency:
            if volume > float(declared) * rules["declared_volume_tolerance"]:
                add("DECLARED_VOLUME", points["declared_volume"],
                    f"Volume {_fmt(volume)} exceeds declared daily volume of {_fmt(float(declared))}.")
                alert("DECLARED_VOLUME", "merchant", space_id, name, currency, "high",
                      f"Processed {_fmt(volume)} {currency} against a declared daily volume of "
                      f"{_fmt(float(declared))} ({volume / float(declared):.1f}x).")

        if space_id in dup_ref_spaces:
            add("DUPLICATE_PROVIDER_REF", points["duplicate_provider_ref"],
                "Repeated provider transaction references.")

        score = sum(r["points"] for r in reasons)
        min_score = profile.get("min_score") if profile is not None else None
        if min_score is not None and pd.notna(min_score) and score < int(min_score):
            add("MIN_SCORE", int(min_score) - score, f"Raised to the profile's minimum score of {int(min_score)}.")
            score = int(min_score)
        score = min(score, 100)

        rows.append({
            "space_id": space_id, "space_name": name, "currency": currency,
            "txn_count": n, "total_volume": round(volume, 2),
            "avg_ticket": round(volume / n, 2), "max_ticket": float(g["amount"].max()),
            "high_value_count": high_value_count, "structuring_count": band_count,
            "off_hours_count": off_hours, "missing_ref_count": missing_ref,
            "round_amount_count": round_count, "peak_window_count": peak,
            "industry_risk": level, "score": int(score), "tier": tier_for(score, settings),
            "reserve_hold": reserve_for(score, volume, settings), "reasons": reasons,
        })

    # Customer-level checks: only possible where the export names a phone number.
    known = df[df["counterparty_msisdn"].notna()]
    for (msisdn, currency), g in known.groupby(["counterparty_msisdn", "currency"]):
        merchants = g["space_name"].unique()
        if len(merchants) >= rules["counterparty_min_merchants"]:
            alert("COUNTERPARTY_MULTI_MERCHANT", "counterparty", msisdn, msisdn, currency, "medium",
                  f"{msisdn} transacted with {len(merchants)} merchants in one day.",
                  {"merchants": sorted(merchants.tolist()), "txn_ids": g["txn_id"].tolist()[:50]})
        cur = settings["currency"].get(currency)
        if cur:
            hv = cur["high_value"]
            below = g[g["amount"] < hv]
            if len(below) >= rules["structuring_min_count"] and below["amount"].sum() >= hv:
                alert("COUNTERPARTY_STRUCTURING", "counterparty", msisdn, msisdn, currency, "high",
                      f"{msisdn} made {len(below)} transactions under {_fmt(hv)} totalling "
                      f"{_fmt(float(below['amount'].sum()))} {currency}.",
                      {"txn_ids": below["txn_id"].tolist()[:50]})

    scores = pd.DataFrame(rows).sort_values("score", ascending=False, ignore_index=True)
    coverage = {
        "transactions": int(len(df)),
        "with_counterparty": int(len(known)),
        "counterparty_share": round(len(known) / len(df), 4),
        "excluded_not_completed": int(len(txns) - len(df)),
    }
    return DayResult(scores, alerts, coverage)
