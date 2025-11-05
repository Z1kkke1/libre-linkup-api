#!/usr/bin/env python
import os
from typing import List
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from pylibrelinkup import PyLibreLinkUp
from pylibrelinkup.api_url import APIUrl
from pylibrelinkup.exceptions import RedirectError

# --- env ---
load_dotenv()
EMAIL = os.getenv("LIBRE_EMAIL")
PASSWORD = os.getenv("LIBRE_PASSWORD")
REGION = getattr(APIUrl, os.getenv("LIBRE_REGION", "EU"))  # EU/EU2/US/DE/FR...

# --- FastAPI app (TOHLE JE DŮLEŽITÉ) ---
app = FastAPI(title="LibreLinkUp API (latest + history)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # pro frontend prototypy
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- helpers ---
def make_client(api: APIUrl) -> PyLibreLinkUp:
    cli = PyLibreLinkUp(email=EMAIL, password=PASSWORD, api_url=api)
    try:
        cli.authenticate()
    except RedirectError as e:
        api2 = e.args[0] if isinstance(e.args[0], APIUrl) else api
        cli = PyLibreLinkUp(email=EMAIL, password=PASSWORD, api_url=api2)
        cli.authenticate()
    return cli

def get_patient(cli: PyLibreLinkUp):
    patients = cli.get_patients()
    if not patients:
        raise HTTPException(404, "No shared patients on this account.")
    return patients[0]

def mmoll_to_mgdl(v: float) -> int:
    return int(round(v * 18))

def downsample_stride(n: int, target: int = 1000) -> int:
    if n <= target:
        return 1
    return max(1, n // target)

# --- endpoints ---
@app.get("/health")
def health():
    return {"ok": True, "region": str(REGION)}

@app.get("/glucose/latest")
def latest():
    if not EMAIL or not PASSWORD:
        raise HTTPException(500, "Server not configured: missing LIBRE_EMAIL / LIBRE_PASSWORD")
    cli = make_client(REGION)
    patient = get_patient(cli)
    m = cli.latest(patient_identifier=patient)
    return {
        "value_mmol_l": m.value,
        "value_mg_dl": mmoll_to_mgdl(m.value),
        "trend": getattr(m.trend, "name", str(m.trend)),
        "timestamp": m.timestamp.isoformat(),
    }

@app.get("/glucose/history")
def history(hours: int = Query(24, ge=1, le=168)):
    if not EMAIL or not PASSWORD:
        raise HTTPException(500, "Server not configured: missing LIBRE_EMAIL / LIBRE_PASSWORD")
    cli = make_client(REGION)
    patient = get_patient(cli)
    series: List = cli.graph(patient_identifier=patient)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    pts = [p for p in series if getattr(p, "timestamp", now) >= cutoff]
    pts.sort(key=lambda p: p.timestamp)

    stride = downsample_stride(len(pts), target=1000)
    pts = pts[::stride] if stride > 1 else pts

    data = [
        {
            "timestamp": p.timestamp.isoformat(),
            "mmol": p.value,
            "mgdl": mmoll_to_mgdl(p.value),
            "trend": getattr(p.trend, "name", str(p.trend)),
        }
        for p in pts
    ]
    return {"points": data, "hours": hours, "count": len(data)}
