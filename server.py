##!/usr/bin/env python
import os
from typing import List, Optional, Tuple
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from pylibrelinkup import PyLibreLinkUp
from pylibrelinkup.api_url import APIUrl
from pylibrelinkup.exceptions import RedirectError

# --- config / env ---
load_dotenv()
EMAIL = os.getenv("LIBRE_EMAIL")
PASSWORD = os.getenv("LIBRE_PASSWORD")
REGION = getattr(APIUrl, os.getenv("LIBRE_REGION", "EU"))  # EU/EU2/US/DE/FR...
# cache TTL: how long we reuse the last fetched "latest" reading (seconds)
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "60"))
# cap points returned by /history (downsample target)
HISTORY_MAX_POINTS = int(os.getenv("HISTORY_MAX_POINTS", "1000"))

app = FastAPI(title="LibreLinkUp API (cached + stale fallback)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ok for prototypes; tighten for prod
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- simple in-memory state ---
_client: Optional[PyLibreLinkUp] = None
_latest_cache: Optional[Tuple[datetime, dict]] = None  # (cached_at, payload)

# --- helpers ---
def _mmol_to_mgdl(v: float) -> int:
    return int(round(v * 18))

def _make_client(api: APIUrl) -> PyLibreLinkUp:
    global _client
    if _client is not None:
        return _client
    cli = PyLibreLinkUp(email=EMAIL, password=PASSWORD, api_url=api)
    try:
        cli.authenticate()
    except RedirectError as e:
        api2 = e.args[0] if isinstance(e.args[0], APIUrl) else api
        cli = PyLibreLinkUp(email=EMAIL, password=PASSWORD, api_url=api2)
        cli.authenticate()
    _client = cli
    return cli

def _get_patient(cli: PyLibreLinkUp):
    patients = cli.get_patients()
    if not patients:
        raise HTTPException(404, "No shared patients on this account.")
    return patients[0]

def _downsample_stride(n: int, target: int) -> int:
    if n <= target:
        return 1
    return max(1, n // target)

# --- endpoints ---
@app.get("/health")
def health():
    return {"ok": True, "region": str(REGION), "cache_ttl_sec": CACHE_TTL_SEC}

@app.get("/glucose/latest")
def latest():
    if not EMAIL or not PASSWORD:
        raise HTTPException(500, "Server not configured: missing LIBRE_EMAIL / LIBRE_PASSWORD")

    global _latest_cache
    now = datetime.now(timezone.utc)

    # 1) serve from cache if still "fresh"
    if _latest_cache:
        cached_at, payload = _latest_cache
        if (now - cached_at) <= timedelta(seconds=CACHE_TTL_SEC):
            return payload  # fresh enough; no Abbott call

    # 2) try to fetch fresh
    try:
        cli = _make_client(REGION)
        patient = _get_patient(cli)
        m = cli.latest(patient_identifier=patient)
        payload = {
            "value_mmol_l": m.value,
            "value_mg_dl": _mmol_to_mgdl(m.value),
            "trend": getattr(m.trend, "name", str(m.trend)),
            "timestamp": m.timestamp.isoformat(),
        }
        _latest_cache = (now, payload)
        return payload
    except Exception:
        # 3) fallback: return the last known good value (marked as stale)
        if _latest_cache:
            _, payload = _latest_cache
            return {**payload, "stale": True}
        # nothing cached yet → propagate a service error
        raise HTTPException(503, "Upstream temporarily unavailable")

@app.get("/glucose/history")
def history(hours: int = Query(24, ge=1, le=168)):
    if not EMAIL or not PASSWORD:
        raise HTTPException(500, "Server not configured: missing LIBRE_EMAIL / LIBRE_PASSWORD")

    cli = _make_client(REGION)
    patient = _get_patient(cli)

    # LLU returns ~1-min resolution points
    series: List = cli.graph(patient_identifier=patient)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    pts = [p for p in series if getattr(p, "timestamp", now) >= cutoff]
    pts.sort(key=lambda p: p.timestamp)

    stride = _downsample_stride(len(pts), HISTORY_MAX_POINTS)
    if stride > 1:
        pts = pts[::stride]

    data = [
        {
            "timestamp": p.timestamp.isoformat(),
            "mmol": p.value,
            "mgdl": _mmol_to_mgdl(p.value),
            "trend": getattr(p.trend, "name", str(p.trend)),
        }
        for p in pts
    ]

    return {"points": data, "hours": hours, "count": len(data)}


