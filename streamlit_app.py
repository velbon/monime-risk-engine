import io
import os
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ReportLab imports for PDF Generation
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import pipeline
from compliance_engine import TIER_HIGH, TIER_LOW, TIER_MEDIUM
from csv_loader import CsvFormatError
from db import get_engine, init_schema

st.set_page_config(page_title="Monime Risk & Analytics Hub", layout="wide", page_icon="🛡️")

st.title("🛡️ Monime Risk Intelligence & Compliance Portal")
st.caption("Every uploaded CRM export is stored permanently, scored against AML rules with stated reasons, "
           "and tracked through alert review with a tamper-proof audit trail.")

TIER_COLOURS = {TIER_HIGH: "#FF4B4B", TIER_MEDIUM: "#FFAA00", TIER_LOW: "#00CC66"}


SECRET_FORMAT = 'Settings → Secrets must contain exactly one line like:  DATABASE_URL = "postgresql://..."'


def secret_database_url():
    """DATABASE_URL from st.secrets, explaining the usual ways the secret is mis-entered."""
    try:
        keys = list(st.secrets.keys())
    except Exception as exc:
        if "pars" in str(exc).lower():
            raise RuntimeError("The app's secrets are not valid TOML (often a missing "
                               f"'DATABASE_URL =' or missing quotes). {SECRET_FORMAT}") from exc
        return None  # no secrets configured
    if "DATABASE_URL" in keys:
        return st.secrets["DATABASE_URL"]
    if keys:
        raise RuntimeError(f"The secrets define {keys} but not DATABASE_URL (the name is "
                           f"case-sensitive). {SECRET_FORMAT}")
    return None


@st.cache_resource
def engine():
    url = secret_database_url() or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(f"DATABASE_URL is not set. {SECRET_FORMAT}")
    eng = get_engine(url)
    init_schema(eng)
    return eng


def signed_in_email():
    """Email from Streamlit Cloud sign-in, when viewer authentication is enabled."""
    try:
        if st.user.is_logged_in and st.user.email:
            return st.user.email
    except Exception:
        pass
    return None


# --- PDF GENERATOR FUNCTION ---
def generate_pdf_report(primary_date, currency, scores, alerts, coverage):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('DocTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=18,
                                 textColor=colors.HexColor('#003366'), spaceAfter=6)
    subtitle_style = ParagraphStyle('DocSub', parent=styles['Normal'], fontName='Helvetica-Oblique', fontSize=10,
                                    textColor=colors.HexColor('#666666'), spaceAfter=14)
    h2_style = ParagraphStyle('Heading2Custom', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=12,
                              textColor=colors.HexColor('#003366'), spaceBefore=10, spaceAfter=6)
    body_style = ParagraphStyle('BodyCustom', parent=styles['Normal'], fontName='Helvetica', fontSize=9,
                                textColor=colors.HexColor('#333333'), spaceAfter=4)
    cell_style = ParagraphStyle('Cell', parent=body_style, fontSize=7.5, leading=9, spaceAfter=0)

    story = [
        Paragraph("Monime Daily Compliance & Risk Audit Report", title_style),
        Paragraph(f"Primary Trading Date: <b>{primary_date}</b> | Currency: <b>{currency}</b> | "
                  f"Generated on: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} GMT", subtitle_style),
        Spacer(1, 10),
    ]
    open_alerts = alerts[alerts["status"].isin(["open", "investigating"])]
    story.append(Paragraph(f"""
    <b>Executive Portfolio Overview:</b><br/>
    • Total Daily Processing Volume: <b>{currency} {scores['total_volume'].sum():,.2f}</b><br/>
    • Total Transactions Analyzed: <b>{int(scores['txn_count'].sum()):,}</b><br/>
    • Active Merchants Analyzed: <b>{len(scores)}</b><br/>
    • Tier 3 (High Risk) Merchants: <b>{(scores['tier'] == TIER_HIGH).sum()}</b><br/>
    • Alerts raised: <b>{len(alerts)}</b> ({len(open_alerts)} open or under investigation)<br/>
    • Customer-level coverage: <b>{coverage.get('counterparty_share', 0):.1%}</b> of transactions carry a payer phone number
    """, body_style))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Merchant Risk Scores", h2_style))
    table_data = [["Merchant Space", "Tier", "Score", f"Daily Vol ({currency})", f"Reserve ({currency})", "Reasons"]]
    for _, row in scores.sort_values("score", ascending=False).iterrows():
        reasons = "; ".join(f"+{r['points']} {r['rule']}" for r in row["reasons"] if r["points"])
        table_data.append([
            Paragraph(str(row["space_name"])[:28], cell_style),
            " ".join(str(row["tier"]).split(" ")[:2]),
            str(int(row["score"])),
            f"{row['total_volume']:,.2f}",
            f"{row['reserve_hold']:,.2f}",
            Paragraph(reasons, cell_style),
        ])
    t = Table(table_data, colWidths=[100, 55, 35, 80, 75, 195], repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#003366')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(t)

    if not alerts.empty:
        story.append(Paragraph("Alerts", h2_style))
        alert_rows = [["Severity", "Rule", "Subject", "Detail", "Status"]]
        for _, a in alerts.iterrows():
            alert_rows.append([a["severity"], a["rule_code"], Paragraph(str(a["subject_name"]), cell_style),
                               Paragraph(str(a["detail"]), cell_style), a["status"]])
        at = Table(alert_rows, colWidths=[45, 110, 85, 220, 80], repeatRows=1)
        at.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#003366')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTSIZE', (0, 0), (-1, -1), 7.5),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(at)

    story.append(Spacer(1, 20))
    story.append(Paragraph("<b>Regulatory Compliance Note:</b> This audit report is generated in accordance with "
                           "Bank of Sierra Leone (BSL) and Financial Intelligence Unit (FIU) standards. All records "
                           "and underlying raw logs must be retained for 7 years.",
                           ParagraphStyle('FootNote', parent=body_style, fontSize=8,
                                          textColor=colors.HexColor('#888888'))))
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


# --- DATABASE CONNECTION ---
try:
    db = engine()
except Exception as exc:
    st.error(f"Cannot connect to the database: {exc}")
    st.stop()

# --- SIDEBAR: WHO + UPLOAD ---
user = signed_in_email()
if user:
    st.sidebar.markdown(f"Signed in as **{user}**")
else:
    # Keep this widget rendered on every run, or Streamlit discards what was typed.
    user = st.sidebar.text_input("Your name or email (recorded in the audit log)", key="analyst_name").strip()

uploaded_file = st.sidebar.file_uploader("Upload Daily CRM Transaction Export (CSV)", type=["csv"])
if uploaded_file and st.sidebar.button("💾 Store & analyse", type="primary", disabled=not user):
    with st.spinner("Storing transactions and running AML rules..."):
        try:
            result = pipeline.ingest_and_analyze(db, uploaded_file.name, uploaded_file.getvalue(), user)
        except CsvFormatError as exc:
            st.sidebar.error(f"Could not read this file: {exc}")
        else:
            up = result["upload"]
            if up["already_uploaded"]:
                st.sidebar.info(f"This exact file was already stored by {up['uploaded_by']} "
                                f"on {up['uploaded_at']:%Y-%m-%d %H:%M}.")
            else:
                st.sidebar.success(f"Stored {up['inserted']:,} new transactions "
                                   f"({up['duplicates']:,} already on file). "
                                   f"Analysed {', '.join(str(d) for d in up['dates']) or 'no new days'}.")
                if up["dates"]:
                    st.session_state["selected_date"] = max(up["dates"])
if uploaded_file and not user:
    st.sidebar.caption("Enter your name above to store the file.")

dates = pipeline.analysed_dates(db)
if not dates:
    st.info("No transactions stored yet. Upload a daily payment CSV export in the sidebar.")
    st.stop()

st.sidebar.markdown("---")
st.sidebar.header("🔍 Dynamic Controls & Filters")
if st.session_state.get("selected_date") not in dates:
    st.session_state["selected_date"] = dates[0]
primary_date = st.sidebar.selectbox("Trading date", dates, key="selected_date")

run = pipeline.latest_run(db, primary_date)
all_scores = pipeline.scores_for_run(db, run["id"])
currencies = sorted(all_scores["currency"].unique(), key=lambda c: (c != "SLE", c))
currency = st.sidebar.selectbox("Currency", currencies)
scores = all_scores[all_scores["currency"] == currency]
selected_tiers = st.sidebar.multiselect("Filter by Risk Tier", options=[TIER_HIGH, TIER_MEDIUM, TIER_LOW],
                                        default=[TIER_HIGH, TIER_MEDIUM, TIER_LOW])
filtered = scores[scores["tier"].isin(selected_tiers)]
day_alerts = pipeline.list_alerts(db, primary_date)
coverage = run["coverage"]

if st.sidebar.button("🔁 Re-run analysis for this date", disabled=not user,
                     help="Use after editing merchant profiles or settings."):
    pipeline.analyze_date(db, primary_date, user)
    st.rerun()

st.sidebar.caption(f"Last analysed {run['run_at']:%Y-%m-%d %H:%M} GMT by {run['run_by']}.")

# Top Executive KPI Cards
kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
kpi1.metric("Trading Date", str(primary_date))
kpi2.metric("Total Daily Volume", f"{currency} {scores['total_volume'].sum():,.2f}")
kpi3.metric("High-Value Txns", int(scores["high_value_count"].sum()))
kpi4.metric("Tier 3 High Risk", int((scores["tier"] == TIER_HIGH).sum()))
open_count = int(day_alerts["status"].isin(["open", "investigating"]).sum())
kpi5.metric("Open Alerts", open_count, delta=f"{len(day_alerts)} raised", delta_color="off")

if coverage.get("counterparty_share", 0) < 0.5:
    st.warning(f"Customer-level checks (structuring by one payer, one payer across many merchants) cover only "
               f"{coverage.get('counterparty_share', 0):.1%} of transactions, because the export has no payer "
               "ID column. Ask Monime to include the payer phone number or account in the export.")

col_pdf, _ = st.columns([1, 3])
with col_pdf:
    st.download_button(
        label="📄 Export Official PDF Compliance Audit Report",
        data=generate_pdf_report(primary_date, currency, scores, day_alerts[day_alerts["currency"] == currency],
                                 coverage),
        file_name=f"Monime_Compliance_Report_{primary_date}_{currency}.pdf",
        mime="application/pdf",
    )

st.markdown("---")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📊 Portfolio Risk Analytics", "⚠️ Anomaly & Velocity Radar", "💰 Reserves & Settlement Actions",
    f"🚨 Alerts ({open_count} open)", "🏢 Merchant Profiles", "🗂️ Uploads & Audit Trail",
])

with tab1:
    col_left, col_right = st.columns([2, 1])
    with col_left:
        fig_bar = px.bar(filtered.sort_values("score", ascending=False), x="space_name", y="score", color="tier",
                         title="Merchant Risk Scores across Portfolio", color_discrete_map=TIER_COLOURS,
                         labels={"space_name": "Merchant", "score": "Risk score", "tier": "Tier"})
        st.plotly_chart(fig_bar, width="stretch")
    with col_right:
        # Volume-weighted, so a handful of tiny merchants cannot dominate the index.
        weights = scores["total_volume"].where(scores["total_volume"] > 0, 1)
        avg_score = float((scores["score"] * weights).sum() / weights.sum()) if len(scores) else 0
        fig_gauge = go.Figure(go.Indicator(
            mode="gauge+number", value=avg_score,
            title={'text': "Platform Risk Index (volume-weighted)"},
            gauge={'axis': {'range': [0, 100]}, 'bar': {'color': "#003366"},
                   'steps': [{'range': [0, 35], 'color': "#00CC66"}, {'range': [35, 65], 'color': "#FFAA00"},
                             {'range': [65, 100], 'color': "#FF4B4B"}]}))
        st.plotly_chart(fig_gauge, width="stretch")

    unknown = int((scores["industry_risk"] == "unknown").sum())
    if unknown:
        st.info(f"{unknown} of {len(scores)} merchants have no industry on file and get a neutral industry score. "
                "Set their industry in the **Merchant Profiles** tab for more accurate scoring.")

with tab2:
    st.subheader("Hourly Transaction & Off-Hours Velocity")
    day_txns = pipeline.transactions_for_day(db, primary_date)
    day_txns = day_txns[day_txns["currency"] == currency]
    day_txns["hour"] = pd.to_datetime(day_txns["created_at"], utc=True).dt.hour
    day_txns["provider_name"] = day_txns["provider_name"].fillna("Unknown")
    hourly_df = day_txns.groupby(["hour", "provider_name"]).size().reset_index(name="txns")
    fig_hourly = px.area(hourly_df, x="hour", y="txns", color="provider_name",
                         title="Hourly Transaction Concentration (GMT)",
                         labels={"provider_name": "Provider", "txns": "Transactions", "hour": "Hour (GMT)"})
    st.plotly_chart(fig_hourly, width="stretch")

    st.subheader("Volume vs Risk Exposure Map")
    flagged = set(day_alerts.loc[day_alerts["subject_type"] == "merchant", "subject_id"])
    plot_df = filtered.assign(alerted=filtered["space_id"].isin(flagged).map({True: "Alerted", False: "No alert"}),
                              bubble=filtered["txn_count"].clip(lower=1))
    fig_scatter = px.scatter(plot_df, x="total_volume", y="score", size="bubble", color="alerted",
                             color_discrete_map={"Alerted": "#FF4B4B", "No alert": "#00CC66"},
                             hover_name="space_name", log_x=True,
                             title="Volume vs Risk Exposure (bubble size = transactions)",
                             labels={"total_volume": f"Daily volume ({currency})", "score": "Risk score"})
    st.plotly_chart(fig_scatter, width="stretch")

with tab3:
    st.subheader("Recommended Settlement & Rolling Reserve Holds")
    table = filtered.assign(
        reasons_text=filtered["reasons"].apply(
            lambda rs: " · ".join(f"+{r['points']} {r['detail']}" for r in rs if r["points"]))
    )[["space_name", "tier", "score", "txn_count", "total_volume", "reserve_hold", "high_value_count",
       "structuring_count", "off_hours_count", "missing_ref_count", "reasons_text"]]
    st.dataframe(
        table,
        column_config={
            "space_name": "Merchant",
            "tier": "Risk tier",
            "score": st.column_config.ProgressColumn("Risk Score", format="%d", min_value=0, max_value=100),
            "txn_count": "Txns",
            "total_volume": st.column_config.NumberColumn(f"Total Vol ({currency})", format="%.2f"),
            "reserve_hold": st.column_config.NumberColumn(f"Reserve Holdback ({currency})", format="%.2f"),
            "high_value_count": "High value",
            "structuring_count": "Near threshold",
            "off_hours_count": "Off-hours",
            "missing_ref_count": "No reference",
            "reasons_text": st.column_config.TextColumn("Why", width="large"),
        },
        hide_index=True, width="stretch",
    )

with tab4:
    st.subheader("Alert Review")
    status_filter = st.multiselect("Show statuses", pipeline.ALERT_STATUSES, default=["open", "investigating"])
    scope = st.radio("Dates", ["This trading date", "All dates"], horizontal=True)
    alerts_df = pipeline.list_alerts(db, primary_date if scope == "This trading date" else None, status_filter)
    if alerts_df.empty:
        st.success("No alerts match these filters.")
    else:
        editable = alerts_df[["id", "trading_date", "severity", "rule_code", "subject_name", "currency",
                              "detail", "status", "status_note"]].copy()
        edited = st.data_editor(
            editable, key=f"alerts_{primary_date}_{scope}_{'-'.join(status_filter)}",
            column_config={
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "trading_date": st.column_config.DateColumn("Date", disabled=True),
                "severity": st.column_config.TextColumn("Severity", disabled=True),
                "rule_code": st.column_config.TextColumn("Rule", disabled=True),
                "subject_name": st.column_config.TextColumn("Subject", disabled=True),
                "currency": st.column_config.TextColumn("Cur.", disabled=True),
                "detail": st.column_config.TextColumn("Detail", disabled=True, width="large"),
                "status": st.column_config.SelectboxColumn("Status", options=pipeline.ALERT_STATUSES, required=True),
                "status_note": st.column_config.TextColumn("Investigation note"),
            },
            hide_index=True, width="stretch",
        )
        changed = edited[(edited["status"] != editable["status"])
                         | (edited["status_note"].fillna("") != editable["status_note"].fillna(""))]
        if st.button(f"Save {len(changed)} change(s)", disabled=changed.empty or not user):
            for _, row in changed.iterrows():
                pipeline.update_alert(db, int(row["id"]), row["status"], row["status_note"] or None, user)
            st.success("Saved and recorded in the audit log.")
            st.rerun()
        if not user:
            st.caption("Enter your name in the sidebar to update alerts.")

with tab5:
    st.subheader("Merchant Profiles")
    st.caption("Industry risk and declared daily volume come from your merchant KYC records. Profiles take "
               "precedence over guesses from the merchant name; the minimum score keeps a merchant at or "
               "above a set risk level. Re-run the analysis afterwards to apply changes.")
    profiles = pipeline.merchant_profiles(db)
    shown = profiles[["space_id", "space_name"] + pipeline.PROFILE_FIELDS + ["updated_by"]]
    edited_profiles = st.data_editor(
        shown, key="profiles",
        column_config={
            "space_id": st.column_config.TextColumn("Space ID", disabled=True),
            "space_name": st.column_config.TextColumn("Merchant", disabled=True),
            "category": st.column_config.TextColumn("Business category"),
            "industry_risk": st.column_config.SelectboxColumn("Industry risk", options=["high", "medium", "low"]),
            "declared_daily_volume": st.column_config.NumberColumn("Declared daily volume", min_value=0, format="%.2f"),
            "declared_currency": st.column_config.SelectboxColumn("Declared currency", options=["SLE", "USD"]),
            "min_score": st.column_config.NumberColumn("Minimum score", min_value=0, max_value=100, step=1),
            "notes": st.column_config.TextColumn("Notes"),
            "updated_by": st.column_config.TextColumn("Last updated by", disabled=True),
        },
        hide_index=True, width="stretch",
    )

    def _norm(frame):
        return frame[pipeline.PROFILE_FIELDS].astype(object).where(frame[pipeline.PROFILE_FIELDS].notna(), None)

    before, after = _norm(shown), _norm(edited_profiles)
    changed_rows = [i for i in after.index if not before.loc[i].equals(after.loc[i])]
    if st.button(f"Save {len(changed_rows)} profile change(s)", disabled=not changed_rows or not user):
        for i in changed_rows:
            row = edited_profiles.loc[i]
            pipeline.save_profile(db, row["space_id"], row["space_name"], after.loc[i].to_dict(), user)
        st.success("Profiles saved. Use 'Re-run analysis for this date' in the sidebar to apply them.")
        st.rerun()

with tab6:
    st.subheader("Uploaded Exports")
    st.dataframe(pipeline.list_uploads(db), hide_index=True, width="stretch")
    st.subheader("Audit Trail (latest 200 entries, append-only)")
    trail = pipeline.audit_trail(db)
    trail["detail"] = trail["detail"].astype(str)
    st.dataframe(trail, hide_index=True, width="stretch")
