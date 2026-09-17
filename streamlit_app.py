import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import io
from datetime import datetime

# ReportLab imports for PDF Generation
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from compliance_engine import MonimeComplianceEngine

st.set_page_config(page_title="Monime Risk & Analytics Hub", layout="wide", page_icon="🛡️")

st.title("🛡️ Monime Risk Intelligence & Compliance Portal")
st.caption("Automated daily payment risk scoring, anomaly detection, declared volume discrepancy checks, and regulatory audit exports.")

# --- PDF GENERATOR FUNCTION ---
def generate_pdf_report(primary_date, total_vol, total_txns, merchant_summary):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=18,
        textColor=colors.HexColor('#003366'),
        spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=10,
        textColor=colors.HexColor('#666666'),
        spaceAfter=14
    )
    h2_style = ParagraphStyle(
        'Heading2Custom',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        textColor=colors.HexColor('#003366'),
        spaceBefore=10,
        spaceAfter=6
    )
    body_style = ParagraphStyle(
        'BodyCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        textColor=colors.HexColor('#333333'),
        spaceAfter=4
    )
    
    # Title & Metadata
    story.append(Paragraph("Monime Daily Compliance & Risk Audit Report", title_style))
    story.append(Paragraph(f"Primary Trading Date: <b>{primary_date}</b> | Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S GMT')}", subtitle_style))
    story.append(Spacer(1, 10))
    
    # Executive Summary Box
    summary_text = f"""
    <b>Executive Portfolio Overview:</b><br/>
    • Total Daily Processing Volume: <b>SLL {total_vol:,.2f}</b><br/>
    • Total Transactions Analyzed: <b>{total_txns:,}</b><br/>
    • Active Merchants Analyzed: <b>{len(merchant_summary)}</b><br/>
    • Tier 3 (High Risk) Merchants: <b>{(merchant_summary['Total_Risk_Score'] >= 66).sum()}</b><br/>
    • Volume Discrepancy Triggers: <b>{(merchant_summary['is_vol_discrepancy']).sum()}</b>
    """
    story.append(Paragraph(summary_text, body_style))
    story.append(Spacer(1, 12))
    
    # High Risk Merchants Table
    story.append(Paragraph("High Risk & Elevated Merchants Summary", h2_style))
    
    # Prepare Table Data
    table_data = [["Merchant Space", "Risk Tier", "Score", "Daily Vol (SLL)", "Reserve Hold (SLL)", "Discrepancy"]]
    high_risk_df = merchant_summary.sort_values(by='Total_Risk_Score', ascending=False)
    
    for idx, row in high_risk_df.iterrows():
        disc_str = "YES (ALERT)" if row['is_vol_discrepancy'] else "No"
        table_data.append([
            str(row['Space Name'])[:18],
            str(row['Risk_Tier']).split(' ')[0] + " " + str(row['Risk_Tier']).split(' ')[1],
            str(int(row['Total_Risk_Score'])),
            f"{row['total_volume']:,.2f}",
            f"{row['Rolling_Reserve_Hold']:,.2f}",
            disc_str
        ])
        
    t = Table(table_data, colWidths=[110, 100, 45, 110, 110, 65])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#003366')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 8.5),
        ('BOTTOMPADDING', (0,0), (-1,0), 6),
        ('TOPPADDING', (0,0), (-1,0), 6),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CCCCCC')),
        ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,1), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    
    story.append(t)
    story.append(Spacer(1, 20))
    story.append(Paragraph("<b>Regulatory Compliance Note:</b> This audit report is generated in accordance with Bank of Sierra Leone (BSL) and Financial Intelligence Unit (FIU) standards. All records and underlying raw logs must be retained for 7 years.", ParagraphStyle('FootNote', parent=body_style, fontSize=8, textColor=colors.HexColor('#888888'))))
    
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# --- STREAMLIT DASHBOARD UI ---

uploaded_file = st.sidebar.file_uploader("Upload Daily CRM Transaction Export (CSV)", type=["csv"])

if uploaded_file:
    engine = MonimeComplianceEngine()
    df_raw, primary_date = engine.load_and_preprocess(uploaded_file)
    df_analyzed, merchant_summary = engine.analyze_risk(df_raw)

    # 1. Declared Volume Discrepancy Calculation
    # Compares daily volume against a baseline declared threshold (e.g., 50,000 SLL)
    merchant_summary['is_vol_discrepancy'] = (
        (merchant_summary['total_volume'] > 50000) & 
        (merchant_summary['total_volume'] > (merchant_summary['avg_ticket'] * 50))
    )
    
    # Calculate Reserves
    def calc_reserve(row):
        if row['Total_Risk_Score'] >= 66:
            return row['total_volume'] * 0.15
        elif row['Total_Risk_Score'] >= 36:
            return row['total_volume'] * 0.05
        return 0.0

    merchant_summary['Rolling_Reserve_Hold'] = merchant_summary.apply(calc_reserve, axis=1)

    # Sidebar Controls
    st.sidebar.markdown("---")
    st.sidebar.header("🔍 Dynamic Controls & Filters")
    selected_tiers = st.sidebar.multiselect(
        "Filter by Risk Tier", 
        options=merchant_summary['Risk_Tier'].unique(),
        default=merchant_summary['Risk_Tier'].unique()
    )

    filtered_summary = merchant_summary[merchant_summary['Risk_Tier'].isin(selected_tiers)]

    # Top Executive KPI Cards
    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
    kpi1.metric("Trading Date", str(primary_date))
    kpi2.metric("Total Daily Volume", f"SLL {df_analyzed['Amount'].sum():,.2f}")
    kpi3.metric("High-Value Txns (≥10k)", df_analyzed['is_high_value'].sum())
    kpi4.metric("Tier 3 High Risk", (merchant_summary['Total_Risk_Score'] >= 66).sum())
    kpi5.metric("Volume Discrepancies", merchant_summary['is_vol_discrepancy'].sum(), delta="Flagged", delta_color="inverse")

    st.markdown("---")

    # PDF Export Section
    col_pdf, col_spacer = st.columns([1, 3])
    with col_pdf:
        pdf_bytes = generate_pdf_report(primary_date, df_analyzed['Amount'].sum(), len(df_analyzed), merchant_summary)
        st.download_button(
            label="📄 Export Official PDF Compliance Audit Report",
            data=pdf_bytes,
            file_name=f"Monime_Compliance_Report_{primary_date}.pdf",
            mime="application/pdf"
        )

    st.markdown("---")

    # Layout Tabs
    tab1, tab2, tab3 = st.tabs(["📊 Portfolio Risk Analytics", "⚠️ Anomaly & Velocity Radar", "💰 Reserves & Settlement Actions"])

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

        st.subheader("Volume vs Risk Exposure Map (Declared Volume Discrepancies)")
        fig_scatter = px.scatter(
            filtered_summary,
            x="total_volume",
            y="Total_Risk_Score",
            size="high_value_count",
            color="is_vol_discrepancy",
            hover_name="Space Name",
            log_x=True,
            title="Volume vs Risk Exposure Map (Red = Volume Discrepancy Flagged)"
        )
        st.plotly_chart(fig_scatter, use_container_width=True)

    with tab3:
        st.subheader("Recommended Settlement & Rolling Reserve Holds")
        
        st.dataframe(
            filtered_summary[[
                'Space Name', 'Risk_Tier', 'Total_Risk_Score', 
                'total_volume', 'Rolling_Reserve_Hold', 'is_vol_discrepancy', 'high_value_count', 'off_hours_count'
            ]].sort_values(by='Total_Risk_Score', ascending=False),
            column_config={
                "Total_Risk_Score": st.column_config.ProgressColumn("Risk Score", format="%d", min_value=0, max_value=100),
                "total_volume": st.column_config.NumberColumn("Total Vol (SLL)", format="%.2f"),
                "Rolling_Reserve_Hold": st.column_config.NumberColumn("Reserve Holdback (SLL)", format="%.2f"),
                "is_vol_discrepancy": st.column_config.CheckboxColumn("Volume Discrepancy Alert"),
            },
            use_container_width=True
        )

else:
    st.info("Upload a daily payment CSV export in the sidebar to view automated analytics.")