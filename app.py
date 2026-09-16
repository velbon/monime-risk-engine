import streamlit as st
import pandas as pd
import plotly.express as px
from compliance_engine import MonimeComplianceEngine

st.set_page_config(page_title="Monime Daily Compliance Analytics", layout="wide")

st.title("🛡️ Monime Daily Payment Analytics & Risk Engine")
st.markdown("Automated high-risk component detection and merchant risk scoring aligned with Monime Compliance Strategy.")

uploaded_file = st.sidebar.file_uploader("Upload CRM Daily Transactions CSV", type=["csv"])

if uploaded_file:
    engine = MonimeComplianceEngine()
    df_raw, primary_date = engine.load_and_preprocess(uploaded_file)
    df_analyzed, merchant_summary = engine.analyze_risk(df_raw)

    st.sidebar.success(f"Isolated Primary Date: **{primary_date}**")
    st.sidebar.info(f"Analyzed Transactions: **{len(df_analyzed):,}**")

    # Key Performance & Risk Indicators
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Single-Day Volume", f"SLL {df_analyzed['Amount'].sum():,.2f}")
    col2.metric("Active Merchants", len(merchant_summary))
    col3.metric("High Value Txns (≥10k)", df_analyzed['is_high_value'].sum())
    col4.metric("Tier 3 High Risk Merchants", (merchant_summary['Total_Risk_Score'] >= 66).sum())

    st.markdown("---")

    # Risk Distribution & Merchant Breakdown
    st.subheader("1. Merchant Risk Scoring Breakdown")
    fig_risk = px.bar(
        merchant_summary.sort_values('Total_Risk_Score', ascending=False).head(15),
        x='Space Name',
        y='Total_Risk_Score',
        color='Risk_Tier',
        title="Top 15 Merchants by Risk Score",
        color_discrete_map={
            "Tier 3 (High Risk - EDD & Hold)": "#FF4B4B",
            "Tier 2 (Medium Risk - Reserve & Review)": "#FFAA00",
            "Tier 1 (Low Risk - Auto Settlement)": "#00CC66"
        }
    )
    st.plotly_chart(fig_risk, use_container_width=True)

    # Hourly Velocity Analysis
    st.subheader("2. Off-Hours & Hourly Transaction Velocity")
    df_analyzed['hour'] = df_analyzed['datetime'].dt.hour
    hourly_counts = df_analyzed.groupby(['hour', 'Actual_Provider']).size().reset_index(name='txns')
    fig_hourly = px.line(hourly_counts, x='hour', y='txns', color='Actual_Provider', title="Transaction Distribution by Hour (GMT)")
    st.plotly_chart(fig_hourly, use_container_width=True)

    # Detailed High-Risk Audit Log
    st.subheader("3. Merchant Risk Summary Table")
    st.dataframe(
        merchant_summary.sort_values(by='Total_Risk_Score', ascending=False),
        column_config={
            "Total_Risk_Score": st.column_config.ProgressColumn("Risk Score", format="%d", min_value=0, max_value=100),
            "total_volume": st.column_config.NumberColumn("Total Vol (SLL)", format="%.2f"),
        },
        use_container_width=True
    )

    # CSV Exporter
    csv_data = merchant_summary.to_csv(index=False).encode('utf-8')
    st.download_button("Download Compliance Risk Audit Log (CSV)", data=csv_data, file_name=f"monime_risk_audit_{primary_date}.csv", mime="text/csv")
else:
    st.info("Please upload a daily CRM payment CSV export in the sidebar to begin analysis.")