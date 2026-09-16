import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from compliance_engine import MonimeComplianceEngine

st.set_page_config(page_title="Monime Risk & Analytics Hub", layout="wide", page_icon="🛡️")

st.title("🛡️ Monime Risk Intelligence & Analytics Portal")
st.caption("Automated daily transaction risk scoring, anomaly detection, and capital reserve projections.")

uploaded_file = st.sidebar.file_uploader("Upload Daily CRM Transaction Export (CSV)", type=["csv"])

if uploaded_file:
    engine = MonimeComplianceEngine()
    df_raw, primary_date = engine.load_and_preprocess(uploaded_file)
    df_analyzed, merchant_summary = engine.analyze_risk(df_raw)

    # Sidebar Controls
    st.sidebar.markdown("---")
    st.sidebar.header("🔍 Dynamic Filters")
    selected_tiers = st.sidebar.multiselect(
        "Filter by Risk Tier", 
        options=merchant_summary['Risk_Tier'].unique(),
        default=merchant_summary['Risk_Tier'].unique()
    )

    filtered_summary = merchant_summary[merchant_summary['Risk_Tier'].isin(selected_tiers)]

    # Top Executive KPI Cards
    kpi1, kpi2, kpi3, kpi4 = st.columns(4)
    kpi1.metric("Primary Processing Date", str(primary_date))
    kpi2.metric("Total Daily Volume", f"SLL {df_analyzed['Amount'].sum():,.2f}")
    kpi3.metric("High-Value Transactions (≥10k)", df_analyzed['is_high_value'].sum())
    kpi4.metric("Tier 3 High Risk Merchants", (merchant_summary['Total_Risk_Score'] >= 66).sum())

    st.markdown("---")

    # Layout Tabs
    tab1, tab2, tab3 = st.tabs(["📊 Portfolio Risk Analytics", "⚠️ Anomaly & Velocity Radar", "💰 Reserve & Settlement Actions"])

    with tab1:
        col_left, col_right = st.columns([2, 1])
        
        with col_left:
            fig_bar = px.bar(
                filtered_summary.sort_values('Total_Risk_Score', ascending=False),
                x='Space Name',
                y='Total_Risk_Score',
                color='Risk_Tier',
                title="Merchant Risk Scores across Portfolio",
                color_discrete_map={
                    "Tier 3 (High Risk - EDD & Hold)": "#FF4B4B",
                    "Tier 2 (Medium Risk - Reserve & Review)": "#FFAA00",
                    "Tier 1 (Low Risk - Auto Settlement)": "#00CC66"
                }
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        with col_right:
            avg_score = merchant_summary['Total_Risk_Score'].mean()
            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number",
                value=avg_score,
                title={'text': "Platform Risk Index"},
                gauge={
                    'axis': {'range': [0, 100]},
                    'bar': {'color': "#003366"},
                    'steps': [
                        {'range': [0, 35], 'color': "#00CC66"},
                        {'range': [35, 65], 'color': "#FFAA00"},
                        {'range': [65, 100], 'color': "#FF4B4B"}
                    ]
                }
            ))
            st.plotly_chart(fig_gauge, use_container_width=True)

    with tab2:
        st.subheader("Hourly Transaction & Off-Hours Velocity")
        df_analyzed['hour'] = df_analyzed['datetime'].dt.hour
        hourly_df = df_analyzed.groupby(['hour', 'Actual_Provider']).size().reset_index(name='txns')
        
        fig_hourly = px.area(hourly_df, x='hour', y='txns', color='Actual_Provider', title="Hourly Transaction Concentration (GMT)")
        st.plotly_chart(fig_hourly, use_container_width=True)

        st.subheader("High Value vs Total Volume Distribution")
        fig_scatter = px.scatter(
            filtered_summary,
            x="total_volume",
            y="Total_Risk_Score",
            size="high_value_count",
            color="Risk_Tier",
            hover_name="Space Name",
            log_x=True,
            title="Volume vs Risk Exposure Map"
        )
        st.plotly_chart(fig_scatter, use_container_width=True)

    with tab3:
        st.subheader("Recommended Settlement & Rolling Reserve Holds")
        
        # Calculate Rolling Reserves
        def calc_reserve(row):
            if row['Total_Risk_Score'] >= 66:
                return row['total_volume'] * 0.15
            elif row['Total_Risk_Score'] >= 36:
                return row['total_volume'] * 0.05
            return 0.0

        filtered_summary['Rolling_Reserve_Hold'] = filtered_summary.apply(calc_reserve, axis=1)
        
        st.dataframe(
            filtered_summary[[
                'Space Name', 'Risk_Tier', 'Total_Risk_Score', 
                'total_volume', 'Rolling_Reserve_Hold', 'high_value_count', 'off_hours_count'
            ]].sort_values(by='Total_Risk_Score', ascending=False),
            column_config={
                "Total_Risk_Score": st.column_config.ProgressColumn("Risk Score", format="%d", min_value=0, max_value=100),
                "total_volume": st.column_config.NumberColumn("Total Vol (SLL)", format="%.2f"),
                "Rolling_Reserve_Hold": st.column_config.NumberColumn("Reserve Holdback (SLL)", format="%.2f"),
            },
            use_container_width=True
        )

else:
    st.info("Upload a daily payment CSV export in the sidebar to view automated analytics.")