import pandas as pd
import numpy as np

class MonimeComplianceEngine:
    def __init__(self, high_val_threshold=10000, velocity_sec_threshold=2):
        self.high_val_threshold = high_val_threshold
        self.velocity_sec_threshold = velocity_sec_threshold
        
    def load_and_preprocess(self, file_path_or_buffer):
        """Loads CSV, normalizes schema variations, and filters to single primary date."""
        df = pd.read_csv(file_path_or_buffer)
        
        # 1. Normalize Timestamp Column
        time_col = None
        if 'Create Time' in df.columns and df['Create Time'].dropna().count() > 0:
            time_col = 'Create Time'
        elif 'Provider Name' in df.columns and pd.to_datetime(df['Provider Name'], errors='coerce').notna().sum() > 0:
            time_col = 'Provider Name'
            
        if time_col:
            df['datetime'] = pd.to_datetime(df[time_col].astype(str).str.slice(0, 24), errors='coerce')
        else:
            df['datetime'] = pd.NaT

        # 2. Normalize Provider / Channel Name
        if 'Provider ID' in df.columns and 'Provider Name' in df.columns:
            if pd.to_datetime(df['Provider Name'], errors='coerce').notna().sum() > 0:
                df['Actual_Provider'] = df['Provider ID']
            else:
                df['Actual_Provider'] = df['Provider Name']
        else:
            df['Actual_Provider'] = df.get('Provider ID', 'Mobile Money')

        # 3. Single-Day Auto-Isolation (Drop previous/out-of-scope dates)
        df['date'] = df['datetime'].dt.date
        if not df['date'].dropna().empty:
            primary_date = df['date'].mode()[0]
            df_filtered = df[df['date'] == primary_date].copy()
        else:
            primary_date = None
            df_filtered = df.copy()
            
        return df_filtered, primary_date

    def analyze_risk(self, df):
        """Applies Monime Compliance Strategy scoring rules to merchant data."""
        # Industry Risk Mapping (35% weight)
        high_risk_kw = ['BET', 'BETTING', 'GAMING', 'CRYPTO', 'SWAP', 'CASINO', 'REMIT']
        med_risk_kw = ['FMCG', 'EXPRESS', 'RIDE', 'GYM', 'HOTEL']
        
        def calc_industry_risk(name):
            n = str(name).upper()
            if any(k in n for k in high_risk_kw):
                return 35
            elif any(k in n for k in med_risk_kw):
                return 20
            elif 'MINISTRY' in n or 'GOVT' in n or 'HEALTH' in n:
                return 5
            return 15

        df['Industry_Risk_Score'] = df['Space Name'].apply(calc_industry_risk)

        # Anomaly Flags
        df['is_off_hours'] = df['datetime'].dt.hour.isin([23, 0, 1, 2, 3, 4])
        df['is_high_value'] = df['Amount'] >= self.high_val_threshold
        df['is_missing_ref'] = df['Reference'].isna() | (df['Reference'].astype(str).str.strip() == '')

        # Velocity check per merchant
        df_sorted = df.sort_values(by=['Space Name', 'datetime'])
        df_sorted['time_delta'] = df_sorted.groupby('Space Name')['datetime'].diff().dt.total_seconds()
        df['is_rapid_fire'] = df_sorted['time_delta'] <= self.velocity_sec_threshold

        # Aggregated Merchant Metrics
        merchant_summary = df.groupby('Space Name').agg(
            total_txns=('ID', 'count'),
            total_volume=('Amount', 'sum'),
            avg_ticket=('Amount', 'mean'),
            max_ticket=('Amount', 'max'),
            high_value_count=('is_high_value', 'sum'),
            off_hours_count=('is_off_hours', 'sum'),
            missing_ref_count=('is_missing_ref', 'sum'),
            rapid_fire_count=('is_rapid_fire', 'sum'),
            industry_risk=('Industry_Risk_Score', 'first')
        ).reset_index()

        # Volume & Governance Scores
        def calc_vol_risk(row):
            if row['total_volume'] > 500000 or row['avg_ticket'] > 1000:
                return 25
            elif row['total_volume'] > 50000 or row['avg_ticket'] > 200:
                return 15
            return 5

        merchant_summary['vol_risk'] = merchant_summary.apply(calc_vol_risk, axis=1)
        merchant_summary['governance_risk'] = 10  # Standard corporate baseline
        
        # Composite Merchant Risk Score (0 - 100)
        merchant_summary['Total_Risk_Score'] = (
            merchant_summary['industry_risk'] + 
            merchant_summary['vol_risk'] + 
            merchant_summary['governance_risk'] +
            (merchant_summary['high_value_count'] * 3).clip(0, 20) +
            (merchant_summary['off_hours_count'] > 50).astype(int) * 10
        ).clip(0, 100)

        # Assign Tier
        def assign_tier(score):
            if score >= 66:
                return "Tier 3 (High Risk - EDD & Hold)"
            elif score >= 36:
                return "Tier 2 (Medium Risk - Reserve & Review)"
            return "Tier 1 (Low Risk - Auto Settlement)"

        merchant_summary['Risk_Tier'] = merchant_summary['Total_Risk_Score'].apply(assign_tier)
        
        return df, merchant_summary