from fastapi import FastAPI, UploadFile, File
import pandas as pd
from compliance_engine import MonimeComplianceEngine

app = FastAPI()
engine = MonimeComplianceEngine()

@app.get("/")
def home():
    return {"status": "Monime Compliance Engine API is Live"}

@app.post("/api/analyze")
async def analyze_csv(file: UploadFile = File(...)):
    df_raw, primary_date = engine.load_and_preprocess(file.file)
    df_analyzed, merchant_summary = engine.analyze_risk(df_raw)
    
    return {
        "status": "success",
        "primary_date": str(primary_date),
        "total_transactions": len(df_analyzed),
        "high_risk_merchants": merchant_summary[merchant_summary['Total_Risk_Score'] >= 66].to_dict(orient="records")
    }