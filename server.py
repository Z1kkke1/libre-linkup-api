#!/usr/bin/env python
import os
import sqlite3
from typing import List, Optional, Tuple
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from pylibrelinkup import PyLibreLinkUp
from pylibrelinkup.api_url import APIUrl
from pylibrelinkup.exceptions import RedirectError, LLUAPIRateLimitError

# --- ENV / config ---
load_dotenv()
EMAIL = os.getenv("LIBRE_EMAIL")
PASSWORD = os.getenv("LIBRE_PASSWORD")
REGION = getattr(APIUrl, os.getenv("LIBRE_REGION", "EU"))

# Caching & throttling
CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "120"))            # jak dlouho vracet cache
MIN_FETCH_INTERVAL_SEC = int(os.getenv("MIN_FETCH_INTERVAL_SEC", "70"))  # min. rozestup mezi LLU fetchema
BACKOFF_AFTER_429_SEC = int(os.getenv("BACKOFF_AFTER_429_SEC", "240"))   # pauza po 429 (Too Many Requests)
HISTORY_MAX_POINTS = int(os.getenv("HISTORY_MAX_POINTS", "1000"))

# Events store (SQLite) – varování: na Render Free se při redeployi může smazat
DB_PATH = os.getenv("DB_PATH", "data.db")
API_KEY = os.getenv("EVENTS_API_KEY")  # potřeba pro /events

app = FastAPI(title="LibreLinkUp API (cache + throttle + events)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # pro produkci přitvrď
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- LLU client & cache state (in-memory) ---
_client: Optional[PyLibreLinkUp] = None
_latest_cache: Optional[Tuple[datetime, dict]] = None  # (cached_at, payload)
_last_fetch_at: Optional[datetime] = None              # kdy jsme naposledy tahali z LLU
_next_allowed_fetch_at: Optional[datetime] = None      # kdy smíme zase tahat (throttle/backoff)

# --- DB helpers (SQLite) ---
def db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events(
            id TEXT PRIMARY KEY,
            type TEXT CHECK(type IN ('bolus','basal')) NOT NULL,
            dose INTEGER NOT NULL,
            ts   TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def require_key(request: Request):
    key = request.headers.get("Authorization", "")
    if key.startswith("Bearer "):
        token = key.split(" ", 1)[1].strip()
    else:
        token = request.query_params.get("key")
    if not API_KEY or token != API_KEY:
        raise HTTPException(401, "Unauthorized: set EVENTS_API_KEY and include key")

# --- LLU helpers ---
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

def _now():
    return datetime.now(timezone.utc)

# --- endpoints: health ---
@app.get("/health")
def health():
    return {
        "ok": True,
        "region": str(REGION),
        "cache_ttl_sec": CACHE_TTL_SEC,
        "min_fetch_interval_sec": MIN_FETCH_INTERVAL_SEC,
        "backoff_after_429_sec": BACKOFF_AFTER_429_SEC,
        "events_storage": f"sqlite://{DB_PATH}",
        "events_auth": "API key required" if API_KEY else "NO KEY SET (blocked)"
    }

# --- endpoints: glucose ---
@app.get("/glucose/latest")
def latest():
    if not EMAIL or not PASSWORD:
        raise HTTPException(500, "Server not configured: missing LIBRE_EMAIL / LIBRE_PASSWORD")

    global _latest_cache, _last_fetch_at, _next_allowed_fetch_at
    now = _now()

    # 1) Pokud máme čerstvou cache, vrať ji
    if _latest_cache:
        cached_at, payload = _latest_cache
        if (now - cached_at) <= timedelta(seconds=CACHE_TTL_SEC):
            return payload

    # 2) Throttle: pokud je příliš brzo od posledního fetch / nebo běží backoff, vrať (stale) cache
    if _next_allowed_fetch_at and now < _next_allowed_fetch_at:
        if _latest_cache:
            _, payload = _latest_cache
            return {**payload, "stale": True, "throttled_until": _next_allowed_fetch_at.isoformat()}
        raise HTTPException(429, "Throttled; try later")

    if _last_fetch_at and (now - _last_fetch_at) < timedelta(seconds=MIN_FETCH_INTERVAL_SEC):
        if _latest_cache:
            _, payload = _latest_cache
            return {**payload, "stale": True}
        # kdyby nebyla cache, hold fetchneme i tak (výjimečně)

    # 3) Pokus o čerstvý fetch
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
        _last_fetch_at = now
        _next_allowed_fetch_at = now + timedelta(seconds=MIN_FETCH_INTERVAL_SEC)
        return payload

    except LLUAPIRateLimitError:
        # 429 → nastavíme backoff, vrátíme poslední známou (stale)
        _next_allowed_fetch_at = now + timedelta(seconds=BACKOFF_AFTER_429_SEC)
        if _latest_cache:
            _, payload = _latest_cache
            return {**payload, "stale": True, "backoff_until": _next_allowed_fetch_at.isoformat()}
        raise HTTPException(429, "Rate limited by LLU; try later")

    except Exception:
        # jiná chyba → vrať stale pokud máme
        if _latest_cache:
            _, payload = _latest_cache
            return {**payload, "stale": True}
        raise HTTPException(503, "Upstream temporarily unavailable")

@app.get("/glucose/history")
def history(hours: int = Query(24, ge=1, le=168)):
    if not EMAIL or not PASSWORD:
        raise HTTPException(500, "Server not configured: missing LIBRE_EMAIL / LIBRE_PASSWORD")

    cli = _make_client(REGION)
    patient = _get_patient(cli)

    series: List = cli.graph(patient_identifier=patient)  # cca 1min body
    now = _now()
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

# --- endpoints: insulin events (server persistence) ---
@app.get("/events")
def list_events(request: Request,
                since: Optional[str] = None,
                until: Optional[str] = None,
                limit: int = Query(500, ge=1, le=5000)):
    require_key(request)
    q = "SELECT id,type,dose,ts FROM events"
    clauses, params = [], []
    if since:
        clauses.append("ts >= ?"); params.append(since)
    if until:
        clauses.append("ts <= ?"); params.append(until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
    conn = db()
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}

@app.post("/events")
async def create_event(request: Request):
    require_key(request)
    body = await request.json()
    typ = (body.get("type") or "").lower()
    dose = body.get("dose")
    ts = body.get("timestamp") or _now().isoformat()

    if typ not in ("bolus", "basal"):
        raise HTTPException(422, "type must be 'bolus' or 'basal'")
    if not isinstance(dose, int):
        raise HTTPException(422, "dose must be integer")

    ev_id = str(uuid4())
    conn = db()
    conn.execute("INSERT INTO events(id,type,dose,ts) VALUES (?,?,?,?)",
                 (ev_id, typ, dose, ts))
    conn.commit()
    conn.close()
    return {"ok": True, "event": {"id": ev_id, "type": typ, "dose": dose, "timestamp": ts}}

@app.delete("/events/{event_id}")
def delete_event(event_id: str, request: Request):
    require_key(request)
    conn = db()
    cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "event not found")
    return {"ok": True, "deleted": event_id}



