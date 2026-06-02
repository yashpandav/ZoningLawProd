import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Request
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from rate_limiter import limiter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional
import asyncpg
import httpx

from query import (
    init_vertex, load_backref, get_system_status,
    retrieve as rag_retrieve,
    sanitize_question,
    build_rich_parcel_context,
    _format_context,
    _build_messages,
    _SYSTEM_PROMPT,
    CHAT_MODEL,
    sanitize_output,
)
from quick_answer import _QUICK_SYSTEM, _QUICK_MODEL
import query as _query_module
import memory


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan — replaces deprecated @app.on_event."""
    global _pool
    _pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    print("✅ DB pool ready")
    await memory.ensure_schema(_pool)
    init_vertex()
    load_backref()
    yield
    await _pool.close()


# Rate limits protect against abuse while allowing genuine architectural research.
# A single architect session rarely needs more than 10 chat messages per minute.
app = FastAPI(title="Toronto Zoning API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
_allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_allowed_origins, allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

if os.getenv("ENABLE_PACKGEN", "false").lower() == "true":
    try:
        from generate_pack_router import router as _pack_router
        app.include_router(_pack_router)
        print("✅ PackGen router mounted at /api/generate-pack/*")
    except Exception as _e:
        print(f"⚠️  PackGen router disabled ({type(_e).__name__}): {_e}")

# Typology library — loaded once at startup, available regardless of ENABLE_PACKGEN.
# Only the typology dataclasses are needed here; no heavy geometry/DXF deps required.
try:
    from packgen.typology.library import TYPOLOGY_LIBRARY as _TYPOLOGY_LIBRARY
    _packgen_available = True
    print(f"✅ Typology library loaded ({len(_TYPOLOGY_LIBRARY)} typologies)")
except Exception as _typ_err:
    _TYPOLOGY_LIBRARY = []
    _packgen_available = False
    print(f"⚠️  Typology library unavailable ({type(_typ_err).__name__}): {_typ_err}")


@app.get("/api/packgen/typologies")
async def list_typologies(
    zone_symbol: str   = Query(..., description="Zone symbol, e.g. 'RD (f3.0; d0.6)'"),
    frontage_m: float  = Query(default=None, ge=0.0, le=200.0),
    depth_m:    float  = Query(default=None, ge=0.0, le=300.0),
    units_target: int  = Query(default=1, ge=1, le=8),
):
    """Return all typologies from the library, filtered and sorted by eligibility.

    Always mounted — does not depend on ENABLE_PACKGEN.  Returns 503 only when
    the packgen Python dependencies (shapely, ezdxf, …) are not installed at all.
    """
    if not _packgen_available:
        raise HTTPException(503, "PackGen dependencies not installed on this server")

    zone_base = zone_symbol.split("(")[0].rstrip()
    result = []
    for t in _TYPOLOGY_LIBRARY:
        # Eligibility: zone prefix match + physical fit within lot dimensions
        if not any(zone_base.startswith(ez) for ez in t.eligible_zones):
            eligible = False
        elif frontage_m is not None and t.min_frontage_m > frontage_m + 0.5:
            eligible = False
        elif depth_m is not None and t.min_depth_m > depth_m + 0.5:
            eligible = False
        else:
            eligible = True

        unit_match = abs(t.units_produced - units_target) <= 1

        result.append({
            "id":             t.id,
            "label":          t.label,
            "units_produced": t.units_produced,
            "stacking_axis":  t.stacking_axis,
            "min_frontage_m": t.min_frontage_m,
            "max_frontage_m": t.max_frontage_m,
            "min_depth_m":    t.min_depth_m,
            "max_depth_m":    t.max_depth_m,
            "target_storeys": t.target_storeys,
            "gfa_per_unit":   list(t.target_gfa_per_unit_m2),
            "notes":          t.notes,
            "eligible":       eligible,
            "unit_match":     unit_match,
        })

    # Sort: eligible first, then unit-matching, then alphabetical by id
    result.sort(key=lambda x: (not x["eligible"], not x["unit_match"], x["id"]))
    return {"typologies": result}

DB_URL = os.getenv("DB_URL", "postgresql://user:pass@localhost:5433/toronto_zoning")
_pool: asyncpg.Pool = None

# ── Parcel LRU cache ──────────────────────────────────────────────────────────
# Avoids re-running two PostGIS spatial queries on every chat message.
# Key: (round(lat, 4), round(lng, 4)) — ~11 m precision, well within one parcel.
# Holds the last 512 decoded parcel dicts (each ~2 KB ≈ 1 MB total).
_PARCEL_CACHE_MAX = 512
_parcel_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def _cache_key(lat: float, lng: float) -> tuple:
    return (round(lat, 4), round(lng, 4))


def _parcel_cache_get(lat: float, lng: float) -> "dict | None":
    key = _cache_key(lat, lng)
    if key in _parcel_cache:
        _parcel_cache.move_to_end(key)   # mark recently used
        return _parcel_cache[key]
    return None


def _parcel_cache_put(lat: float, lng: float, parcel: dict) -> None:
    key = _cache_key(lat, lng)
    _parcel_cache[key] = parcel
    _parcel_cache.move_to_end(key)
    while len(_parcel_cache) > _PARCEL_CACHE_MAX:
        _parcel_cache.popitem(last=False)  # evict LRU entry

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

BYLAW_BASE = "https://www.toronto.ca/zoning/bylaw_amendments/ZBL_NewProvision_Chapter"

# Zone defaults used when overlay data is absent.  Overlay values always win.
ZONE_DEFAULTS = {
    # R/RD/RS/RT/RM: coverage/FSI/front-yard intentionally omitted or nulled in _compute_constraints()
    #   because By-law 569-2013 regulates these zones via setbacks, depth limits, and angular
    #   planes — NOT fixed percentages.  Values kept here only for the non-residential zones.
    "R":   {"max_coverage_pct": None, "max_height_m": 10.0, "max_fsi": None, "front_yard_min_m": None, "rear_yard_min_m": 7.5, "side_yard_min_m": 0.9},
    "RD":  {"max_coverage_pct": None, "max_height_m": 10.0, "max_fsi": 0.6,  "front_yard_min_m": None, "rear_yard_min_m": 7.5, "side_yard_min_m": 1.2},  # Ch.10.20.40.50 → 1.2m
    "RS":  {"max_coverage_pct": None, "max_height_m": 10.0, "max_fsi": 0.75, "front_yard_min_m": 3.0, "rear_yard_min_m": 7.5, "side_yard_min_m": 0.6},
    "RT":  {"max_coverage_pct": None, "max_height_m": 12.0, "max_fsi": 0.85, "front_yard_min_m": 3.0, "rear_yard_min_m": 7.5, "side_yard_min_m": 0.6},
    "RM":  {"max_coverage_pct": None, "max_height_m": 14.0, "max_fsi": 1.0,  "front_yard_min_m": 3.0, "rear_yard_min_m": 7.5, "side_yard_min_m": 0.6},
    "RA":  {"max_coverage_pct": 35,   "max_height_m": None, "max_fsi": 2.0,  "front_yard_min_m": 3.0, "rear_yard_min_m": 7.5, "side_yard_min_m": 1.2},
    "CR":  {"max_coverage_pct": 100,  "max_height_m": None, "max_fsi": 3.0,  "front_yard_min_m": 0.0, "rear_yard_min_m": 0.0, "side_yard_min_m": 0.0},
    "CL":  {"max_coverage_pct": 100,  "max_height_m": None, "max_fsi": 2.0,  "front_yard_min_m": 0.0, "rear_yard_min_m": 0.0, "side_yard_min_m": 0.0},
    "E":   {"max_coverage_pct": 65,   "max_height_m": None, "max_fsi": 1.0,  "front_yard_min_m": 3.0, "rear_yard_min_m": 3.0, "side_yard_min_m": 0.0},
    "O":   {"max_coverage_pct": 20,   "max_height_m": 10.0, "max_fsi": 0.3,  "front_yard_min_m": 3.0, "rear_yard_min_m": 3.0, "side_yard_min_m": 1.5},
}

# By-law 156-2023 (multiplex): 4 units as-of-right across all R-category zones.
# RM has no fixed cap in the base zone → None.
ZONE_MAX_UNITS = {"R": 4, "RD": 4, "RS": 4, "RT": 4}

# Base zones where coverage/FSI/front-yard are controlled contextually, not by a fixed %.
_RESIDENTIAL_ZONES = frozenset({"R", "RD", "RS", "RT", "RM"})

CHAPTER_NAMES = {
    "1":"Administration","2":"Compliance","5":"General Regulations (all zones)",
    "10":"Residential Zone Category","15":"Residential Apartment Zone",
    "20":"Commercial Local Zone","25":"Commercial Residential Zone",
    "30":"Commercial Zone","40":"Commercial Residential Zone",
    "50":"Commercial Residential Employment Zone","60":"Employment Industrial Zone",
    "70":"Institutional Zone","80":"Institutional Zone (School/Place of Worship)",
    "90":"Open Space Zone","95":"Natural Area Zone",
    "100":"Utility and Transportation Zone","150":"Specific Use Regulations",
    "200":"Parking Space Regulations","210":"Accessible Parking",
    "220":"Bicycle Parking","230":"Loading Space Regulations",
    "600":"Overlay Regulations","800":"Definitions",
    "900":"Site Specific Exceptions","970":"Policy Area Overlay",
    "990":"Zoning By-law Maps","995":"Overlay Maps (Height, Coverage, Parking)",
}

POLICY_ID_MAP = {
    "1":"Major Street — Type 1 (arterial: Yonge, Bloor, King, Queen etc.)",
    "2":"Major Street — Type 2","3":"Major Street — Type 3 (collector road)",
    "4":"Major Street — Type 4",
}
PARKING_ZONE_MAP = {
    "A":"Zone A — no minimum parking required (downtown core)",
    "B":"Zone B — reduced parking minimums apply",
}
ZN_STATUS_MAP = {
    0:"UNDER APPEAL — original By-law 569-2013 provisions",
    1:"UNDER APPEAL — post-2013 amendment",
    2:"In full force and effect",
    3:"In full force and effect (amended since 2013)",
}

# ─────────────────────────────────────────────────────────────────────────────
# CONSTRAINT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _base_zone(zone_symbol: str) -> "str | None":
    """'RD (u1)' → 'RD', 'CR 1.5(c1; r1.5)' → 'CR'"""
    if not zone_symbol:
        return None
    return re.split(r'[\s(]', zone_symbol.strip())[0].upper()


def _compute_constraints(r: dict) -> dict:
    """Derive building-envelope constraints from already-decoded parcel data.
    Overlay values always override zone defaults. Residential zones use contextual
    rules (setbacks, coverage) not fixed percentages — those are returned as None."""
    zone     = _base_zone(r.get("zone_symbol"))
    defaults = ZONE_DEFAULTS.get(zone, {})

    lot_area     = r.get("lot_area_m2")      # already None-ified if -1
    lot_frontage = r.get("lot_frontage_m")

    # Coverage (Change 1): overlay wins; residential without overlay → None
    # (Toronto residential zones use contextual lot coverage, not a fixed %)
    cov_overlay = r.get("coverage_overlay_pct")
    if cov_overlay is not None:
        max_coverage = float(cov_overlay)
        cov_source = "overlay"
    elif zone in _RESIDENTIAL_ZONES:
        max_coverage = None
        cov_source = "none"
    else:
        max_coverage = defaults.get("max_coverage_pct") or r.get("base_coverage_pct")
        cov_source = "default"

    # Height: overlay wins
    height_overlay = r.get("height_overlay_m")
    if height_overlay is not None:
        max_height = float(height_overlay)
        height_source = "overlay"
    else:
        max_height = defaults.get("max_height_m")
        height_source = "default"

    # FSI (Change 2): PostGIS wins; R zone → None (no FSI cap); others use defaults
    fsi = r.get("floor_space_index")
    if fsi not in (None, -1):
        max_fsi = fsi
        fsi_source = "postGIS"
    elif zone == "R":
        max_fsi = None
        fsi_source = "none"
    else:
        max_fsi = defaults.get("max_fsi")
        fsi_source = "default"

    # Units: fixed for residential zones per By-law 156-2023, None otherwise
    max_units = ZONE_MAX_UNITS.get(zone)

    # Setbacks (Change 3): R and RD use contextual front yard (not a fixed min)
    setback_overlay = r.get("setback_area_type")
    if zone in ("R", "RD"):
        front_yard = None
        front_source = "none"   # contextual: avg of neighbours on the street
    else:
        front_yard = defaults.get("front_yard_min_m")
        front_source = "overlay" if setback_overlay else "default"
    rear_yard  = defaults.get("rear_yard_min_m")
    side_yard  = defaults.get("side_yard_min_m")

    # Parking
    park_code    = r.get("parking_zone_code")
    parking_zone = park_code if park_code in ("A", "B") else "standard"
    parking_min  = 0 if park_code in ("A", "B") else 1

    # Bicycle: max(1, floor(max_units × 0.5)) for residential, 1 otherwise
    bicycle_min = max(1, int((max_units or 0) * 0.5)) if max_units else 1

    # Lot depth (Change 10): derived from area ÷ frontage when both are known
    if lot_area is not None and lot_frontage is not None and lot_frontage > 0:
        lot_depth_m = round(lot_area / lot_frontage, 1)
        depth_source = "derived"
    else:
        lot_depth_m = None
        depth_source = "none"

    # Max building depth (Change 9): residential only, varies by lot depth
    # Toronto By-law: 17 m standard; 19 m on deeper lots (>36 m)
    if zone in _RESIDENTIAL_ZONES:
        max_building_depth_m = 19.0 if (lot_depth_m and lot_depth_m > 36) else 17.0
        bldg_depth_source = "default"
    else:
        max_building_depth_m = None
        bldg_depth_source = "none"

    # Data sources provenance (Change 12)
    data_sources = {
        "max_coverage_pct":     cov_source,
        "max_height_m":         height_source,
        "max_fsi":              fsi_source,
        "front_yard_min_m":     front_source,
        "rear_yard_min_m":      "default",
        "side_yard_min_m":      "default",
        "parking_min_spaces":   "overlay" if park_code in ("A", "B") else "default",
        "lot_area_m2":          "postGIS" if lot_area is not None else "none",
        "lot_frontage_m":       "postGIS" if lot_frontage is not None else "none",
        "lot_depth_m":          depth_source,
        "max_building_depth_m": bldg_depth_source,
    }

    return {
        "lot_area_m2":           lot_area,
        "lot_frontage_m":        lot_frontage,
        "lot_depth_m":           lot_depth_m,
        "max_coverage_pct":      max_coverage,
        "max_height_m":          max_height,
        "max_fsi":               max_fsi,
        "max_units":             max_units,
        "front_yard_min_m":      front_yard,
        "rear_yard_min_m":       rear_yard,
        "side_yard_min_m":       side_yard,
        "parking_min_spaces":    parking_min,
        "parking_zone":          parking_zone,
        "bicycle_parking_min":   bicycle_min,
        "max_building_depth_m":  max_building_depth_m,
        "exception_number":      r.get("exception_number"),
        "exception_overrides":   {},
        "data_sources":          data_sources,
    }


# In-memory LRU cache for exception constraint overrides — keyed by (exception_number, zone).
# Capped at 512 entries (Toronto has ~900 Chapter 900 exceptions; this covers common ones).
_EXC_CACHE_MAX = 512
_exc_constraint_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def _exc_cache_put(key: tuple, value: dict) -> None:
    """Write to the exception-constraint LRU cache, evicting the oldest entry if full."""
    _exc_constraint_cache[key] = value
    _exc_constraint_cache.move_to_end(key)
    while len(_exc_constraint_cache) > _EXC_CACHE_MAX:
        _exc_constraint_cache.popitem(last=False)

# ─────────────────────────────────────────────────────────────────────────────
# LINK BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def chapter_link(chapter_num):
    if not chapter_num: return None
    ch = str(chapter_num).strip()
    return {"file":f"Chapter{ch}.htm","url":f"{BYLAW_BASE}{ch}.htm",
            "chapter":ch,"description":CHAPTER_NAMES.get(ch,f"Chapter {ch}")}

def section_link(chapter_num, section_id):
    if not chapter_num: return None
    ch = str(chapter_num).strip()
    if not section_id: return chapter_link(ch)
    sec = re.sub(r'\s*\([^)]*\)', '', str(section_id)).strip()
    parts = sec.split('.')
    # 1-part: Chapter{ch}.htm  |  2-part: Chapter{ch}_{p2}.htm  |  3+: Chapter{ch}_{p2}.htm#{sec}
    if len(parts) >= 3:
        file = f"Chapter{parts[0]}_{parts[1]}.htm"
        url  = f"{BYLAW_BASE}{parts[0]}_{parts[1]}.htm#{sec}"
    elif len(parts) == 2:
        file = f"Chapter{parts[0]}_{parts[1]}.htm"
        url  = f"{BYLAW_BASE}{parts[0]}_{parts[1]}.htm"
    else:
        file = f"Chapter{ch}.htm"
        url  = f"{BYLAW_BASE}{ch}.htm"
    return {"file": file, "url": url,
            "chapter": ch, "section": section_id,
            "description": CHAPTER_NAMES.get(ch, f"Chapter {ch}") + f" — Section {section_id}"}

def exception_link(exception_ref):
    if not exception_ref: return None
    m   = re.match(r'(900(?:\.\d+)+)', str(exception_ref))
    sec = m.group(1) if m else "900"
    parts = sec.split('.')
    # Build correct subdocument URL: Chapter900_3.htm#900.3.10
    if len(parts) >= 2:
        file = f"Chapter{parts[0]}_{parts[1]}.htm"
        url  = f"{BYLAW_BASE}{parts[0]}_{parts[1]}.htm#{sec}" if len(parts) >= 3 else f"{BYLAW_BASE}{parts[0]}_{parts[1]}.htm"
    else:
        file = "Chapter900.htm"
        url  = f"{BYLAW_BASE}900.htm"
    return {"file": file, "url": url,
            "chapter": "900", "section": sec, "exception_ref": exception_ref,
            "description": f"Chapter 900 — Exception {exception_ref}"}

def overlay_chapter_link(htm_value):
    if not htm_value: return None
    m = re.match(r'Chapter(\d+)(?:_(\d+))?\.htm', str(htm_value))
    if not m: return None
    ch, sub = m.group(1), m.group(2)
    if sub:
        # e.g. Chapter150_8.htm → the subdocument IS the section; no anchor needed
        file = f"Chapter{ch}_{sub}.htm"
        url  = f"{BYLAW_BASE}{ch}_{sub}.htm"
        return {"file": file, "url": url,
                "chapter": ch, "description": CHAPTER_NAMES.get(ch, f"Chapter {ch}") + f" §{ch}.{sub}"}
    return {"file": f"Chapter{ch}.htm", "url": f"{BYLAW_BASE}{ch}.htm",
            "chapter": ch, "description": CHAPTER_NAMES.get(ch, f"Chapter {ch}")}

# ─────────────────────────────────────────────────────────────────────────────
# SQL
# ─────────────────────────────────────────────────────────────────────────────

BASE_QUERY = """
SELECT
  z.zn_zone AS zone_symbol, z.zn_string AS zone_label,
  z.excptn_no AS exception_number, z.zn_excptn AS exception_text,
  z.zn_status AS zone_status, z.zn_holding AS holding_zone,
  z.gen_zone AS general_zone_code,
  z.frontage AS lot_frontage_m, z.zn_area AS lot_area_m2,
  z.units AS max_units, z.density AS density,
  z.fsi_total AS floor_space_index, z.coverage AS base_coverage_pct,
  z.prcnt_comm AS fsi_commercial, z.prcnt_res AS fsi_residential,
  z.prcnt_emmp AS fsi_employment, z.prcnt_offc AS fsi_office,
  z.stand_set AS standard_setback_code,
  z.zbl_chapt AS bylaw_chapter, z.zbl_sectn AS bylaw_section,
  z.zbl_excptn AS bylaw_exception_ref,
  h.ht_label AS height_overlay_m, h.ht_stories AS height_overlay_storeys,
  h.ht_string AS height_overlay_label,
  lc.prcnt_cver AS coverage_overlay_pct,
  pk.zn_parkzone AS parking_zone_code,
  pa.policy_id AS policy_area_id, pa.chapt_200 AS policy_chapter_htm,
  bs.ch600_area_type AS setback_area_type, bs.bylaw_sectionlink AS setback_chapter_htm,
  rh.rmh_area AS rooming_house_area, rh.rmg_string AS rooming_house_code,
  rh.rmg_hs_no AS rooming_house_max_count, rh.chap150_25 AS rooming_house_chapter_htm,
  ST_AsText(z.geom) AS lot_polygon_wkt
FROM zoning_area z
LEFT JOIN height_overlay h ON ST_Contains(h.geom,ST_SetSRID(ST_Point($2,$1),4326))
LEFT JOIN lot_coverage_overlay lc ON ST_Contains(lc.geom,ST_SetSRID(ST_Point($2,$1),4326))
LEFT JOIN parking_zone_overlay pk ON ST_Contains(pk.geom,ST_SetSRID(ST_Point($2,$1),4326))
LEFT JOIN policy_area_overlay pa ON ST_Contains(pa.geom,ST_SetSRID(ST_Point($2,$1),4326))
LEFT JOIN building_setback_overlay bs ON ST_Contains(bs.geom,ST_SetSRID(ST_Point($2,$1),4326))
LEFT JOIN rooming_house_overlay rh ON ST_Contains(rh.geom,ST_SetSRID(ST_Point($2,$1),4326))
WHERE ST_DWithin(z.geom,ST_SetSRID(ST_Point($2,$1),4326),0.0002)
ORDER BY z.geom <-> ST_SetSRID(ST_Point($2,$1),4326)
LIMIT 1;
"""

RETAIL_QUERY = """
SELECT pr.linear_name_full_legal AS street_name, pr.bylaw_sectionlink AS retail_chapter_htm,
  ROUND(ST_Distance(ST_Transform(pr.geom,32617),
    ST_Transform(ST_SetSRID(ST_Point($2,$1),4326),32617))::numeric,1) AS distance_m
FROM priority_retail_overlay pr
WHERE ST_DWithin(ST_Transform(pr.geom,32617),
  ST_Transform(ST_SetSRID(ST_Point($2,$1),4326),32617),30)
ORDER BY distance_m ASC LIMIT 1;
"""

# ─────────────────────────────────────────────────────────────────────────────
# REVERSE GEOCODING
# ─────────────────────────────────────────────────────────────────────────────

_NOMINATIM_URL  = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_UA   = "Toronto-Zoning-AI/1.0"
_GEOCODE_TIMEOUT = 3.0   # seconds


async def _reverse_geocode(lat: float, lng: float) -> str:
    """Return a short civic address string for (lat, lng) via Nominatim, or '' on failure."""
    try:
        async with httpx.AsyncClient(timeout=_GEOCODE_TIMEOUT) as client:
            resp = await client.get(
                _NOMINATIM_URL,
                params={"format": "json", "lat": lat, "lon": lng, "addressdetails": "1"},
                headers={"User-Agent": _NOMINATIM_UA},
            )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        a = data.get("address", {})
        # Build "house_number street, city" — fall back to display_name excerpt if missing
        parts = []
        if a.get("house_number"):
            parts.append(a["house_number"])
        street = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
        if street:
            parts.append(street)
        city = a.get("city") or a.get("town") or a.get("municipality") or ""
        addr = " ".join(parts)
        if city and city.lower() not in addr.lower():
            addr = f"{addr}, {city}" if addr else city
        return addr.strip() or data.get("display_name", "").split(",")[0].strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# DECODE PARCEL
# ─────────────────────────────────────────────────────────────────────────────

def decode(row: dict, retail_row: dict | None, address: str = "") -> dict:
    r = dict(row)
    status_code           = r.get("zone_status")
    r["zone_status_text"] = ZN_STATUS_MAP.get(status_code,"Unknown")
    r["zone_under_appeal"]= status_code in (0,1)

    for f in ("lot_frontage_m","lot_area_m2","floor_space_index",
              "fsi_commercial","fsi_residential","fsi_employment","fsi_office"):
        if r.get(f) == -1: r[f] = None
    if r.get("height_overlay_storeys") in (None,-1):
        r["height_overlay_storeys"] = None

    r["parking_zone"]         = PARKING_ZONE_MAP.get(r.get("parking_zone_code"))
    r["road_classification"]  = POLICY_ID_MAP.get(
        str(r.get("policy_area_id") or ""),
        "Local street (not on Policy Area Overlay Map)"
    )
    r["downtown_setback_applies"] = r.get("setback_area_type") is not None
    r["rooming_house_permitted"]  = r.get("rooming_house_area")  is not None

    if retail_row:
        r["priority_retail_street"]     = retail_row["street_name"]
        r["priority_retail_distance_m"] = float(retail_row["distance_m"]) if retail_row["distance_m"] is not None else None
        r["retail_frontage_required"]   = True
        r["retail_chapter_htm"]         = retail_row["retail_chapter_htm"]
    else:
        r["priority_retail_street"]     = None
        r["priority_retail_distance_m"] = None
        r["retail_frontage_required"]   = False
        r["retail_chapter_htm"]         = None

    r["chapter_links"] = _build_chapter_links(r)
    r["constraints"]   = _compute_constraints(r)
    r["address"]       = address
    # Keep ai_context for backward compat — but /api/chat now uses build_rich_parcel_context()
    r["ai_context"]    = _build_ai_context(r)
    return r


def _build_chapter_links(r: dict) -> dict:
    links = {}
    links["zone_chapter"] = section_link(r.get("bylaw_chapter"),r.get("bylaw_section"))
    if r.get("bylaw_exception_ref"):
        links["exception_chapter"] = exception_link(r.get("bylaw_exception_ref"))
    if r.get("height_overlay_m") is not None:
        links["height_overlay_chapter"] = {
            "file":"Chapter995.htm","url":f"{BYLAW_BASE}995.htm#995.20",
            "chapter":"995","section":"995.20",
            "description":"Chapter 995 — Height Overlay Map (Section 995.20)"}
    if r.get("coverage_overlay_pct") is not None:
        links["coverage_overlay_chapter"] = {
            "file":"Chapter995.htm","url":f"{BYLAW_BASE}995.htm#995.30",
            "chapter":"995","section":"995.30",
            "description":"Chapter 995 — Lot Coverage Overlay (Section 995.30)"}
    if r.get("parking_zone_code"):
        links["parking_regulations_chapter"] = {
            "file":"Chapter200.htm","url":f"{BYLAW_BASE}200.htm",
            "chapter":"200","description":"Chapter 200 — Parking Space Regulations"}
        links["parking_overlay_chapter"] = {
            "file":"Chapter995.htm","url":f"{BYLAW_BASE}995.htm#995.50",
            "chapter":"995","section":"995.50",
            "description":"Chapter 995 — Parking Zone Overlay (Section 995.50)"}
    if r.get("policy_chapter_htm"):
        links["policy_area_chapter"]      = overlay_chapter_link(r.get("policy_chapter_htm"))
    if r.get("setback_chapter_htm"):
        links["building_setback_chapter"] = overlay_chapter_link(r.get("setback_chapter_htm"))
    if r.get("rooming_house_chapter_htm"):
        links["rooming_house_chapter"]    = overlay_chapter_link(r.get("rooming_house_chapter_htm"))
    if r.get("retail_chapter_htm"):
        links["retail_frontage_chapter"]  = overlay_chapter_link(r.get("retail_chapter_htm"))
    links["general_regulations_chapter"] = {
        "file":"Chapter5.htm","url":f"{BYLAW_BASE}5.htm",
        "chapter":"5","description":"Chapter 5 — General Regulations"}
    links["definitions_chapter"] = {
        "file":"Chapter800.htm","url":f"{BYLAW_BASE}800.htm",
        "chapter":"800","description":"Chapter 800 — Definitions"}
    return links


def _build_ai_context(r: dict) -> str:
    """Compact context string (kept for backward compat with debug endpoints)."""
    links = r.get("chapter_links",{})
    def lu(key): return (links.get(key) or {}).get("url","")
    parts = [
        "=== PARCEL ===",
        f"Zone: {r.get('zone_symbol')} | {r.get('zone_label')}",
        f"Chapter {r.get('bylaw_chapter')} Section {r.get('bylaw_section')} — {lu('zone_chapter')}",
    ]
    if r.get("zone_under_appeal"):
        parts.append("⚠️ UNDER APPEAL")
    if r.get("exception_number"):
        parts.append(f"Exception #{r['exception_number']} — {lu('exception_chapter')}")
    parts += [
        f"Frontage: {r.get('lot_frontage_m')}m | Area: {r.get('lot_area_m2')}m² | Units: {r.get('max_units')}",
        f"FSI: {r.get('floor_space_index')} | Coverage: {r.get('base_coverage_pct')}%",
        f"Height overlay: {r.get('height_overlay_m')}m | Coverage overlay: {r.get('coverage_overlay_pct')}%",
        f"Parking: {r.get('parking_zone') or 'Standard'} | Road: {r.get('road_classification')}",
    ]
    return "\n".join(str(p) for p in parts if p is not None)


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODEL
# ─────────────────────────────────────────────────────────────────────────────

_HISTORY_CAP = 20   # keep last 20 turns (40 messages) to prevent token overflow


class ChatRequest(BaseModel):
    lat:        float
    lng:        float
    message:    str = Field(..., min_length=1, max_length=5000)
    user_id:    str = "anonymous"
    session_id: Optional[str] = None
    history:    list[dict] = []  # kept for /api/quick-chat; ignored by /api/chat (server manages history)


class FeedbackRequest(BaseModel):
    message_id: str
    session_id: str
    user_id:    str = "anonymous"
    rating:     int  # 1 = thumbs up, -1 = thumbs down


def _build_system_with_memory(parcel_facts: list[dict]) -> str:
    """Prepend confirmed session parameters to the system prompt."""
    if not parcel_facts:
        return _SYSTEM_PROMPT
    block = "\n".join(f"  - {f['key']}: {f['value']}" for f in parcel_facts[:20])
    return (
        _SYSTEM_PROMPT
        + "\n\n=== ESTABLISHED PARAMETERS (confirmed earlier in this session) ===\n"
        + block
        + "\nDefer to live PARCEL DATA above for authoritative values."
    )


def _build_messages_with_memory(
    system: str,
    full_prompt: str,
    db_history: list[dict],
    summary: Optional[str] = None,
) -> list[dict]:
    """Build OpenAI messages with optional rolling summary and DB-loaded history."""
    msgs: list[dict] = [{"role": "system", "content": system}]
    if summary:
        msgs.append({"role": "user",      "content": f"[Session summary]: {summary}"})
        msgs.append({"role": "assistant", "content": "Understood. I have context from our earlier discussion."})
    for m in db_history:
        role    = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content", "").strip()
        if content:
            msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": full_prompt})
    return msgs


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/parcel")
async def get_parcel(lat: float, lng: float):
    print(f"\n{'='*60}")
    print(f"[/api/parcel] Request: lat={lat}  lng={lng}")
    if not (43.58 <= lat <= 43.86):
        raise HTTPException(400,f"Latitude {lat} outside Toronto")
    if not (-79.64 <= lng <= -79.11):
        tip = " Did you forget the minus sign?" if lng > 0 else ""
        raise HTTPException(400,f"Longitude {lng} outside Toronto.{tip}")

    async with _pool.acquire() as c1, _pool.acquire() as c2:
        row, retail, address = await asyncio.gather(
            c1.fetchrow(BASE_QUERY, lat, lng),
            c2.fetchrow(RETAIL_QUERY, lat, lng),
            _reverse_geocode(lat, lng),
        )

    if not row:
        print(f"[/api/parcel] No zone polygon found within 16m")
        return {"found": False, "lat": lat, "lng": lng,
                "message": "No zone polygon within 16m."}

    raw = dict(row)
    print(f"[/api/parcel] PostGIS BASE_QUERY result:")
    print(f"   zone_symbol={raw.get('zone_symbol')}  zone_label={raw.get('zone_label')}")
    print(f"   exception_number={raw.get('excptn_no')}  zone_status={raw.get('zn_status')}")
    print(f"   bylaw_chapter={raw.get('zbl_chapt')}  bylaw_section={raw.get('zbl_sectn')}")
    print(f"   height_overlay={raw.get('ht_label')}  coverage_overlay={raw.get('prcnt_cver')}  parking_zone={raw.get('zn_parkzone')}")
    print(f"   address={address!r}")
    if retail:
        print(f"[/api/parcel] RETAIL_QUERY result: street={dict(retail).get('street_name')}  dist={dict(retail).get('distance_m')}m")
    else:
        print(f"[/api/parcel] RETAIL_QUERY result: (no priority retail within 30m)")

    result = decode(dict(row), dict(retail) if retail else None, address=address)
    result.update({"found": True, "lat": lat, "lng": lng})
    _parcel_cache_put(lat, lng, result)
    print(f"[/api/parcel] Decoded → zone={result.get('zone_symbol')}  exception={result.get('exception_number')}  status={result.get('zone_status_text')}")
    print(f"{'='*60}")
    return result


@app.post("/api/chat")
@limiter.limit("20/minute")
async def chat(request: Request, req: ChatRequest):
    _t_e2e = time.perf_counter()
    print(f"\n{'#'*60}")
    print(f"[/api/chat] user={req.user_id[:8]}  session={req.session_id or 'new'}  lat={req.lat}  lng={req.lng}")
    print(f"[/api/chat] Message: {req.message!r}")
    if not (43.58 <= req.lat <= 43.86):
        raise HTTPException(400, f"Latitude {req.lat} outside Toronto")
    if not (-79.64 <= req.lng <= -79.11):
        raise HTTPException(400, f"Longitude {req.lng} outside Toronto")
    if not req.message.strip():
        raise HTTPException(400, "message cannot be empty")

    try:
        clean_q = sanitize_question(req.message)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # ── Parcel lookup (cached or PostGIS) ────────────────────────────────────
    parcel = _parcel_cache_get(req.lat, req.lng)
    if parcel is None:
        print(f"[/api/chat] Cache MISS — running PostGIS")
        async with _pool.acquire() as c1, _pool.acquire() as c2:
            row, retail = await asyncio.gather(
                c1.fetchrow(BASE_QUERY, req.lat, req.lng),
                c2.fetchrow(RETAIL_QUERY, req.lat, req.lng),
            )
        if not row:
            raise HTTPException(404, "No zoning data for this location.")
        parcel = decode(dict(row), dict(retail) if retail else None)
        parcel.update({"found": True, "lat": req.lat, "lng": req.lng})
        _parcel_cache_put(req.lat, req.lng, parcel)
    else:
        print(f"[/api/chat] Cache HIT — skipping PostGIS")

    zone_symbol      = parcel.get("zone_symbol") or ""
    bylaw_chapter    = str(parcel.get("bylaw_chapter") or "")
    exception_number = parcel.get("exception_number")

    # ── Memory: resolve user + session (best-effort, non-blocking) ───────────
    mem_user_id    = None
    mem_session_id = req.session_id
    db_history     = []
    session_summary = None
    parcel_facts   = []

    try:
        mem_user_id    = await memory.get_or_create_user(_pool, req.user_id)
        mem_session_id = mem_session_id or await memory.get_or_create_session(
            _pool, mem_user_id, req.lat, req.lng, zone_symbol, exception_number
        )
        sess_row = await _pool.fetchrow(
            "SELECT summary, message_count FROM parcel_sessions WHERE id=$1",
            mem_session_id,
        )
        if sess_row:
            has_summary = bool(sess_row["summary"]) and sess_row["message_count"] > 20
            msg_limit   = 10 if has_summary else 15
            db_history  = await memory.load_messages(_pool, mem_session_id, limit=msg_limit)
            session_summary = sess_row["summary"] if has_summary else None
        parcel_facts = await memory.get_parcel_params(_pool, mem_session_id)
        print(f"[/api/chat] session={mem_session_id[:8]}  history={len(db_history)} msgs  facts={len(parcel_facts)}")
    except Exception as exc:
        print(f"[/api/chat] Memory init (non-fatal): {exc}")

    print(f"[/api/chat] zone={zone_symbol}  ch={bylaw_chapter}  exc={exception_number}")

    # ── Retrieval ─────────────────────────────────────────────────────────────
    t_retrieve = time.perf_counter()
    try:
        chunks = await asyncio.to_thread(
            rag_retrieve,
            question         = clean_q,
            zone_symbol      = zone_symbol,
            bylaw_chapter    = bylaw_chapter,
            exception_number = exception_number,
        )
    except Exception as exc:
        print(f"[/api/chat] retrieve() failed: {exc}")
        raise HTTPException(500, f"Retrieval error: {exc}")

    retrieve_ms = int((time.perf_counter() - t_retrieve) * 1000)

    # ── Build prompt + messages ───────────────────────────────────────────────
    parcel_context = build_rich_parcel_context(parcel)
    rag_context    = _format_context(chunks)
    full_prompt = (
        f"=== PARCEL DATA (City of Toronto GIS — authoritative) ===\n"
        f"{parcel_context}\n\n"
        f"=== RETRIEVED BY-LAW EXCERPTS ({len(chunks)} sections) ===\n"
        f"{rag_context}\n\n"
        f"=== ARCHITECT'S QUESTION ===\n"
        f"{clean_q}\n\n"
        f"=== YOUR RESPONSE ===\n"
        f"Follow reasoning steps. Give complete answer with actual value, "
        f"section citations, and chapter URL."
    )
    system = _build_system_with_memory(parcel_facts)
    msgs   = _build_messages_with_memory(system, full_prompt, db_history, session_summary)
    sections = [c.get("section_id", "") for c in chunks]

    print(f"[/api/chat] Retrieval done in {retrieve_ms}ms — streaming synthesis now ({CHAT_MODEL})...")

    # ── Stream synthesis ──────────────────────────────────────────────────────
    async def gen_sse():
        full_text = ""
        t_synth   = time.perf_counter()
        try:
            stream = await _query_module._openai_async.chat.completions.create(
                model       = CHAT_MODEL,
                messages    = msgs,
                temperature = 0.2,
                stream      = True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    token = delta.content
                    full_text += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            synth_ms = int((time.perf_counter() - t_synth) * 1000)
            e2e_ms   = int((time.perf_counter() - _t_e2e) * 1000)
            reply    = sanitize_output(full_text)

            print(f"\n[/api/chat] Stream done: {len(reply)}ch  retrieve={retrieve_ms}ms  synth={synth_ms}ms  e2e={e2e_ms}ms")
            print(f"{'#'*60}\n")

            # Persist the message pair now so we can include the message_id in the
            # done event — the frontend uses it for thumbs-up/down feedback.
            asst_id = None
            if mem_session_id and mem_user_id:
                try:
                    asst_id = await memory.save_message_pair(
                        _pool, mem_session_id, mem_user_id, clean_q, reply,
                        sections_used=sections, zone_symbol=zone_symbol, chunks_count=len(chunks),
                    )
                except Exception as save_exc:
                    print(f"[/api/chat] save_message_pair failed: {save_exc}")

            yield f"data: {json.dumps({'type': 'done', 'reply': reply, 'session_id': mem_session_id, 'message_id': asst_id, 'sections_used': sections, 'zone_symbol': zone_symbol, 'bylaw_chapter': bylaw_chapter, 'chunks_count': len(chunks), 'parcel_found': True})}\n\n"

            # Background: extract facts + maybe summarize (save_message_pair already done above)
            if mem_session_id and mem_user_id and asst_id:
                async def _bg():
                    try:
                        await asyncio.gather(
                            memory.extract_and_save_facts(
                                _pool, _query_module._openai_async,
                                mem_session_id, mem_user_id, clean_q, reply, asst_id,
                            ),
                            memory.maybe_summarize(
                                _pool, _query_module._openai_async, mem_session_id,
                            ),
                            return_exceptions=True,
                        )
                    except Exception as bg_exc:
                        print(f"[/api/chat] background memory task: {bg_exc}")
                asyncio.create_task(_bg())

        except Exception as exc:
            print(f"[/api/chat] Stream error: {type(exc).__name__}: {exc}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        gen_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# MEMORY / SESSION ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def get_session(user_id: str, lat: float, lng: float):
    """
    Look up any prior session for this (user, parcel).
    Returns {found: False} or {found: True, session_id, message_count, summary, params}.
    """
    try:
        uid  = await memory.get_or_create_user(_pool, user_id)
        info = await memory.get_session_info(_pool, uid, lat, lng)
        if not info:
            return {"found": False}
        return {"found": True, **info}
    except Exception as exc:
        print(f"[/api/sessions] error: {exc}")
        return {"found": False}


@app.get("/api/session/messages")
async def get_session_messages(session_id: str, limit: int = 20):
    """Return recent messages for a session (for frontend display on resume)."""
    try:
        msgs = await memory.load_messages_for_display(_pool, session_id, limit=limit)
        return {"messages": msgs}
    except Exception as exc:
        print(f"[/api/session/messages] error: {exc}")
        return {"messages": []}


@app.get("/api/session/params")
async def get_session_params(session_id: str):
    """Return confirmed parcel parameters extracted from a session."""
    try:
        params = await memory.get_parcel_params(_pool, session_id)
        return {"params": params}
    except Exception as exc:
        print(f"[/api/session/params] error: {exc}")
        return {"params": []}


@app.post("/api/quick-chat")
@limiter.limit("30/minute")
async def quick_chat(request: Request, req: ChatRequest):
    """
    Fast, plain-English answer streamed via SSE.
    Uses skip_rerank=True and a condensed prompt for minimal latency.
    History is intentionally ignored — each question is answered fresh.
    """
    _t_e2e = time.perf_counter()
    print(f"\n{'~'*60}")
    print(f"[/api/quick-chat] lat={req.lat}  lng={req.lng}  msg={req.message!r}")
    if not (43.58 <= req.lat <= 43.86):
        raise HTTPException(400, f"Latitude {req.lat} outside Toronto")
    if not (-79.64 <= req.lng <= -79.11):
        raise HTTPException(400, f"Longitude {req.lng} outside Toronto")
    if not req.message.strip():
        raise HTTPException(400, "message cannot be empty")

    # Sanitize before streaming starts so errors surface as HTTP 400
    try:
        clean_q = sanitize_question(req.message)
    except ValueError as e:
        raise HTTPException(400, str(e))

    parcel = _parcel_cache_get(req.lat, req.lng)
    if parcel is None:
        print(f"[/api/quick-chat] Cache MISS — running PostGIS")
        async with _pool.acquire() as c1, _pool.acquire() as c2:
            row, retail = await asyncio.gather(
                c1.fetchrow(BASE_QUERY, req.lat, req.lng),
                c2.fetchrow(RETAIL_QUERY, req.lat, req.lng),
            )
        if not row:
            raise HTTPException(404, "No zoning data for this location.")
        parcel = decode(dict(row), dict(retail) if retail else None)
        parcel.update({"found": True, "lat": req.lat, "lng": req.lng})
        _parcel_cache_put(req.lat, req.lng, parcel)
    else:
        print(f"[/api/quick-chat] Cache HIT")

    zone_symbol      = parcel.get("zone_symbol") or ""
    bylaw_chapter    = str(parcel.get("bylaw_chapter") or "")
    exception_number = parcel.get("exception_number")

    # Retrieval in thread (skip reranker — saves ~800ms for quick mode)
    t_retrieve = time.perf_counter()
    try:
        chunks = await asyncio.to_thread(
            rag_retrieve,
            question         = clean_q,
            zone_symbol      = zone_symbol,
            bylaw_chapter    = bylaw_chapter,
            exception_number = exception_number,
            skip_rerank      = True,
        )
    except Exception as exc:
        print(f"[/api/quick-chat] retrieve() failed: {exc}")
        raise HTTPException(500, f"Retrieval error: {exc}")

    retrieve_ms = int((time.perf_counter() - t_retrieve) * 1000)

    # Condensed by-law context: section IDs + first 200 chars of text
    parcel_ctx  = build_rich_parcel_context(parcel)
    bylaw_lines: list[str] = []
    for c in chunks:
        sid    = c.get("section_id", "?")
        title  = c.get("section_title", "")
        text   = (c.get("text") or "").strip()
        src    = c.get("source", "")
        is_exc = c.get("is_exception") or src in ("exception", "exception_direct")
        prefix   = "EXCEPTION" if is_exc else "RULE"
        override = " [OVERRIDES base zone]" if is_exc else ""
        bylaw_lines.append(f"[{prefix}] {sid} — {title}{override}\n{text}")
    bylaw_ctx = "\n\n".join(bylaw_lines) if bylaw_lines else "(No sections retrieved.)"

    prompt = (
        f"=== PARCEL DATA ===\n{parcel_ctx}\n\n"
        f"=== RELEVANT BY-LAW SECTIONS ({len(chunks)}) ===\n{bylaw_ctx}\n\n"
        f"=== QUESTION ===\n{clean_q}\n\n"
        f"Answer in plain English, covering all relevant rules completely, following the format exactly."
    )
    sections = [c.get("section_id", "") for c in chunks]

    print(f"[/api/quick-chat] Retrieval done in {retrieve_ms}ms — streaming synthesis now ({_QUICK_MODEL})...")

    async def gen_sse():
        full_text = ""
        t_synth   = time.perf_counter()
        try:
            stream = await _query_module._openai_async.chat.completions.create(
                model       = _QUICK_MODEL,
                messages    = [
                    {"role": "system", "content": _QUICK_SYSTEM},
                    {"role": "user",   "content": prompt},
                ],
                temperature = 0.1,
                stream      = True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    token = delta.content
                    full_text += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            synth_ms = int((time.perf_counter() - t_synth) * 1000)
            e2e_ms   = int((time.perf_counter() - _t_e2e) * 1000)
            reply    = sanitize_output(full_text)

            print(f"\n[/api/quick-chat] Stream done: reply={len(reply)}ch  retrieve={retrieve_ms}ms  synth={synth_ms}ms  e2e={e2e_ms}ms")
            print(f"{'~'*60}\n")

            yield f"data: {json.dumps({'type': 'done', 'reply': reply, 'sections_used': sections, 'zone_symbol': zone_symbol, 'bylaw_chapter': bylaw_chapter, 'chunks_count': len(chunks), 'parcel_found': True})}\n\n"

        except Exception as exc:
            print(f"[/api/quick-chat] Stream error: {type(exc).__name__}: {exc}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        gen_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# PAID ANALYSIS REPORT
# ─────────────────────────────────────────────────────────────────────────────

_ANALYSIS_REPORT_SYSTEM = """You are a senior Toronto zoning consultant preparing a formal compliance report for a client's building permit application. Write a complete, professional report. Never truncate — completeness is mandatory.

Use markdown formatting with these exact sections in order:

## Executive Summary
2–3 sentences: overall compliance status, the single most important finding, and bottom-line recommendation for the client.

## Compliance Analysis
Review every parameter below. For each, state: current value | by-law limit | ✓ Compliant or ✗ Non-compliant | the specific By-law 569-2013 section that governs it. If non-compliant, state the exact fix required.

Parameters to cover (include all that apply):
- Lot coverage / footprint
- GFA and FSI
- Building height
- Number of dwelling units (if applicable)
- Front yard setback
- Rear yard setback
- Side yard setback(s)
- Angular plane / 45° rule (residential zones)
- Building depth limit (residential zones)
- Parking spaces
- Bicycle parking spaces

## Site Constraints & Overlays
List all active overlays (height overlay, coverage overlay, parking zone, policy area, downtown setback, etc.), site-specific exceptions, and any holding zone or appeal status. Cite the overlay chapter for each.

## Risk Assessment
Rate overall permit risk: **Low**, **Medium**, or **High** — with justification.
Flag:
- Parameters within 5% of their limit (measurement variance risk)
- Any provisions under appeal that could change requirements
- Contextual standards that may be challenged by neighbours or the Committee of Adjustment

## Permit Application Checklist
Ordered checklist of what the client needs to prepare, including:
- Required architectural drawings (site plan, floor plans, elevations, sections)
- Surveys and studies required
- Pre-consultation with the City of Toronto (if recommended)
- Minor variance or Committee of Adjustment application (if any parameter is non-compliant)
- Approximate timeline

## Design Optimization
2–3 concrete, actionable suggestions to maximize density or value while remaining fully compliant. Be specific to this parcel and zone.

## Next Steps
Numbered action plan with recommended professional contacts (registered planner, land surveyor, architect).

Important rules:
- Cite specific section numbers from By-law 569-2013 (e.g. Section 10.5.20.30, Section 200.15.1)
- All measurements in metric
- Explain contextual standards clearly (e.g. front yard averaging)
- Note any garden suite potential under By-law 156-2023 if the parcel is residential
- Tone: authoritative professional, not hedging — the client needs clear guidance
- End with: "Note: This report is generated from City of Toronto GIS data. Verify with a registered planner before permit submission."
"""


class AnalyzeReportRequest(BaseModel):
    lat:              float
    lng:              float
    zone_symbol:      str
    # Building parameters
    footprint_m2:     float
    gfa_m2:           float
    height_m:         float
    units:            int   = 1
    front_yard_m:     float
    rear_yard_m:      float
    side_yard_m:      float
    parking_spaces:   int
    bicycle_spaces:   int
    building_depth_m: "float | None" = None
    # Computed compliance results
    overall_compliant: bool
    violations:        list[str] = []
    coverage_pct:      "float | None" = None
    live_fsi:          "float | None" = None
    floor_count:       int = 1


@app.post("/api/analyze-report")
@limiter.limit("10/minute")
async def analyze_report(request: Request, req: AnalyzeReportRequest):
    """
    Professional compliance analysis report — SSE streaming, full RAG + gpt-4.1.
    Uses the Voyage reranker and 16K output for maximum accuracy and completeness.
    """
    if not (43.58 <= req.lat <= 43.86):
        raise HTTPException(400, f"Latitude {req.lat} outside Toronto")
    if not (-79.64 <= req.lng <= -79.11):
        raise HTTPException(400, f"Longitude {req.lng} outside Toronto")

    _t_e2e = time.perf_counter()
    print(f"\n{'$'*60}")
    print(f"[/api/analyze-report] lat={req.lat} lng={req.lng} zone={req.zone_symbol}")

    # Parcel lookup (cache preferred)
    parcel = _parcel_cache_get(req.lat, req.lng)
    if parcel is None:
        print(f"[/api/analyze-report] Cache MISS — running PostGIS")
        async with _pool.acquire() as c1, _pool.acquire() as c2:
            row, retail = await asyncio.gather(
                c1.fetchrow(BASE_QUERY, req.lat, req.lng),
                c2.fetchrow(RETAIL_QUERY, req.lat, req.lng),
            )
        if not row:
            raise HTTPException(404, "No zoning data for this location.")
        parcel = decode(dict(row), dict(retail) if retail else None)
        parcel.update({"found": True, "lat": req.lat, "lng": req.lng})
        _parcel_cache_put(req.lat, req.lng, parcel)
    else:
        print(f"[/api/analyze-report] Cache HIT")

    exception_number = parcel.get("exception_number")
    bylaw_chapter    = str(parcel.get("bylaw_chapter") or "")
    c                = parcel.get("constraints", {})
    ov               = c.get("exception_overrides", {})

    # Build a rich, structured analysis question for the RAG retrieval
    viol_text = ("; ".join(req.violations)) if req.violations else "none — all parameters appear compliant"
    retrieval_q = (
        f"Full compliance analysis for zone {req.zone_symbol}: "
        f"footprint {req.footprint_m2}m², GFA {req.gfa_m2}m² (FSI {req.live_fsi}), "
        f"height {req.height_m}m, {req.units} units, front yard {req.front_yard_m}m, "
        f"rear yard {req.rear_yard_m}m, side yard {req.side_yard_m}m, "
        f"parking {req.parking_spaces}, bicycle {req.bicycle_spaces}. "
        + (f"Building depth {req.building_depth_m}m. " if req.building_depth_m else "")
        + (f"Exception #{exception_number}. " if exception_number else "")
        + f"Violations: {viol_text}. "
        f"What are the setback requirements, coverage rules, FSI limits, height limits, "
        f"parking requirements, angular plane rules, permit checklist, and risk factors?"
    )

    t_retrieve = time.perf_counter()
    try:
        clean_q = sanitize_question(retrieval_q[:2000])
        chunks  = await asyncio.to_thread(
            rag_retrieve,
            question         = clean_q,
            zone_symbol      = req.zone_symbol,
            bylaw_chapter    = bylaw_chapter,
            exception_number = exception_number,
        )
    except Exception as exc:
        print(f"[/api/analyze-report] retrieve() failed: {exc}")
        raise HTTPException(500, f"Retrieval error: {exc}")

    retrieve_ms = int((time.perf_counter() - t_retrieve) * 1000)
    print(f"[/api/analyze-report] Retrieved {len(chunks)} chunks in {retrieve_ms}ms")

    parcel_context = build_rich_parcel_context(parcel)
    rag_context    = _format_context(chunks)

    def fmt(v): return str(v) if v is not None else "—"

    analysis_prompt = f"""=== PARCEL DATA (City of Toronto GIS — authoritative) ===
{parcel_context}

=== RETRIEVED BY-LAW EXCERPTS ({len(chunks)} sections) ===
{rag_context}

=== PROPOSED DEVELOPMENT PARAMETERS ===
Zone: {req.zone_symbol}
Lot area: {fmt(c.get('lot_area_m2'))} m² | Frontage: {fmt(c.get('lot_frontage_m'))} m | Depth: {fmt(c.get('lot_depth_m'))} m
{f"Exception #{exception_number} applies — base zone rules modified" if exception_number else "No site-specific exception"}

Building configuration (proposed):
  Footprint:      {req.footprint_m2} m²  ({fmt(req.coverage_pct)}% coverage)  — By-law limit: {fmt(ov.get('max_coverage_pct') or c.get('max_coverage_pct'))}%
  GFA:            {req.gfa_m2} m²  (FSI {fmt(req.live_fsi)})            — FSI limit: {fmt(ov.get('max_fsi') or c.get('max_fsi'))}
  Height:         {req.height_m} m  ({req.floor_count} floors)              — Height limit: {fmt(ov.get('max_height_m') or c.get('max_height_m'))} m
  Units:          {req.units}                                               — Unit limit: {fmt(ov.get('max_units') or c.get('max_units'))}
  Front yard:     {req.front_yard_m} m                                      — Min: {fmt(ov.get('front_yard_min_m') or c.get('front_yard_min_m'))} m
  Rear yard:      {req.rear_yard_m} m                                       — Min: {fmt(ov.get('rear_yard_min_m') or c.get('rear_yard_min_m'))} m
  Side yard:      {req.side_yard_m} m                                       — Min: {fmt(ov.get('side_yard_min_m') or c.get('side_yard_min_m'))} m
{f"  Building depth: {req.building_depth_m} m" if req.building_depth_m else ""}
  Parking:        {req.parking_spaces} spaces                               — Min: {fmt(c.get('parking_min_spaces', 1))}
  Bicycle:        {req.bicycle_spaces} spaces                               — Min: {fmt(c.get('bicycle_parking_min', 1))}

Overall compliance status: {"✓ COMPLIANT — all parameters within limits" if req.overall_compliant else f"✗ NON-COMPLIANT — {len(req.violations)} violation(s)"}
{("Violations detected: " + " | ".join(req.violations)) if req.violations else ""}

=== TASK ===
Prepare the complete professional compliance report following the system instructions exactly.
Include all sections. Cite specific By-law 569-2013 section numbers throughout.
"""

    msgs = [
        {"role": "system", "content": _ANALYSIS_REPORT_SYSTEM},
        {"role": "user",   "content": analysis_prompt},
    ]

    print(f"[/api/analyze-report] Streaming synthesis ({CHAT_MODEL})…")

    async def gen_sse():
        full_text = ""
        t_synth   = time.perf_counter()
        try:
            stream = await _query_module._openai_async.chat.completions.create(
                model       = CHAT_MODEL,
                messages    = msgs,
                temperature = 0.15,
                stream      = True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    token = delta.content
                    full_text += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            synth_ms = int((time.perf_counter() - t_synth) * 1000)
            e2e_ms   = int((time.perf_counter() - _t_e2e)  * 1000)
            reply    = sanitize_output(full_text)

            print(f"\n[/api/analyze-report] Done: {len(reply)}ch | retrieve={retrieve_ms}ms synth={synth_ms}ms e2e={e2e_ms}ms")
            print(f"{'$'*60}\n")

            yield f"data: {json.dumps({'type': 'done', 'reply': reply, 'chunks_count': len(chunks)})}\n\n"

        except Exception as exc:
            print(f"[/api/analyze-report] Stream error: {type(exc).__name__}: {exc}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        gen_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTION CONSTRAINT EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

_EXC_EXTRACT_SYSTEM = """You are a Toronto zoning by-law parser. \
Extract ONLY the numeric overrides from the exception text below. \
Return a JSON object and nothing else — no markdown, no explanation, no extra keys. \
Use these exact key names if the override exists: \
max_height_m, max_coverage_pct, max_fsi, max_units, \
front_yard_min_m, rear_yard_min_m, side_yard_min_m, parking_min_spaces. \
If a field is not overridden by this exception, omit it. \
Values must be numbers (not strings). \
Example output: {"max_height_m": 9.5, "max_coverage_pct": 40}"""

_EXC_VALID_KEYS = frozenset({
    "max_height_m", "max_coverage_pct", "max_fsi", "max_units",
    "front_yard_min_m", "rear_yard_min_m", "side_yard_min_m", "parking_min_spaces",
})


@app.get("/api/exception-constraints")
@limiter.limit("60/minute")
async def exception_constraints(request: Request, exception_number: int, zone: str):
    """Extract structured numeric overrides for a site-specific exception.
    Calls the RAG pipeline once to fetch the exception text, then uses Gemini Flash
    to parse out the numeric overrides.  Results are cached in-memory.
    """
    cache_key = (exception_number, zone)
    if cache_key in _exc_constraint_cache:
        _exc_constraint_cache.move_to_end(cache_key)   # mark as recently used
        return _exc_constraint_cache[cache_key]

    # Retrieve the exception chunks via the existing RAG pipeline
    try:
        chunks = await asyncio.to_thread(
            rag_retrieve,
            question=(
                f"What are the building envelope constraints — height, coverage, FSI, "
                f"setbacks, units — for exception {exception_number}?"
            ),
            zone_symbol      = zone,
            bylaw_chapter    = "900",
            exception_number = exception_number,
        )
    except Exception as exc:
        print(f"[exception-constraints] retrieve() failed: {exc}")
        _exc_cache_put(cache_key, {})
        return {}

    exc_text = "\n\n".join(
        f"Section {c.get('section_id', '')} — {c.get('section_title', '')}\n{(c.get('text') or '').strip()}"
        for c in chunks
        if (c.get("is_exception") or c.get("source", "") in ("exception", "exception_direct"))
    )
    if not exc_text.strip():
        # Fall back to all chunks if no exception-tagged ones found
        exc_text = "\n\n".join(
            f"Section {c.get('section_id', '')}\n{(c.get('text') or '').strip()}"
            for c in chunks
        )

    if not exc_text.strip():
        _exc_cache_put(cache_key, {})
        return {}

    if _query_module._openai is None:
        _exc_cache_put(cache_key, {})
        return {}

    prompt = f"Exception #{exception_number} for zone {zone}:\n\n{exc_text}"
    try:
        resp = _query_module._openai.chat.completions.create(
            model           = os.getenv("QUICK_ANSWER_MODEL", "gpt-4.1-mini"),
            messages        = [
                {"role": "system", "content": _EXC_EXTRACT_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            temperature     = 0.0,
            max_tokens      = 512,
            response_format = {"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        overrides = json.loads(raw)
        # Keep only known keys with numeric values
        overrides = {
            k: v for k, v in overrides.items()
            if k in _EXC_VALID_KEYS and isinstance(v, (int, float))
        }
    except Exception as exc:
        print(f"[exception-constraints] LLM parse failed: {exc}")
        overrides = {}

    print(f"[exception-constraints] exception={exception_number} zone={zone} overrides={overrides}")
    _exc_cache_put(cache_key, overrides)
    return overrides


# ─────────────────────────────────────────────────────────────────────────────
# FEEDBACK
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/feedback")
async def submit_feedback(req: FeedbackRequest):
    if req.rating not in (1, -1):
        raise HTTPException(400, "rating must be 1 or -1")
    try:
        await _pool.execute(
            """INSERT INTO message_feedback (message_id, session_id, user_id, rating)
               VALUES ($1::uuid, $2::uuid, $3::uuid, $4)
               ON CONFLICT DO NOTHING""",
            req.message_id, req.session_id, req.user_id, req.rating,
        )
    except Exception as exc:
        print(f"[feedback] DB error: {exc}")
        raise HTTPException(500, "Failed to save feedback")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG / HEALTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    db_ok,db_error = False,None
    if _pool:
        try:
            async with _pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_ok = True
        except Exception as exc:
            db_error = str(exc)
    rag = get_system_status()
    models_ok = (
        _query_module._VOYAGE_CLIENT is not None and
        _query_module._SPARSE_EMBEDDER is not None
    )
    checks = {
        "db":     db_ok,
        "llm":    rag.get("llm_ready", False),
        "qdrant": rag.get("qdrant_ready", False),
        "models": models_ok,
    }
    return {"ok":all(checks.values()),"checks":checks,"db_error":db_error,"rag":rag}


@app.get("/api/debug/qdrant/{collection}")
async def debug_qdrant(collection: str):
    from query import _qdrant
    info      = _qdrant.get_collection(collection)
    sample,_  = _qdrant.scroll(collection, limit=3, with_payload=True)
    return {
        "collection":    collection,
        "points_count":  info.points_count,
        "sample_chunks": [
            {"section_id":p.payload.get("section_id"),
             "section_title":p.payload.get("section_title"),
             "zone_symbol":p.payload.get("zone_symbol"),
             "content_type":p.payload.get("content_type"),
             "token_count":p.payload.get("token_count")}
            for p in sample
        ],
    }


@app.get("/api/debug/tables")
async def debug_tables():
    async with _pool.acquire() as conn:
        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
        result = []
        for t in tables:
            n = t["table_name"]
            c = await conn.fetchval(f"SELECT COUNT(*) FROM {n}")
            result.append({"table":n,"rows":c})
    return {"tables":result}


@app.get("/api/debug/columns/{table_name}")
async def debug_columns(table_name: str):
    allowed = {"zoning_area","height_overlay","lot_coverage_overlay","parking_zone_overlay",
               "policy_area_overlay","building_setback_overlay","rooming_house_overlay","priority_retail_overlay"}
    if table_name not in allowed:
        raise HTTPException(400,f"Allowed: {sorted(allowed)}")
    async with _pool.acquire() as conn:
        cols   = await conn.fetch(
            "SELECT column_name,data_type FROM information_schema.columns WHERE table_name=$1 ORDER BY ordinal_position",
            table_name)
        sample = await conn.fetch(f"SELECT * FROM {table_name} LIMIT 3")
        count  = await conn.fetchval(f"SELECT COUNT(*) FROM {table_name}")
    return {"table":table_name,"row_count":count,
            "columns":[{"name":r["column_name"],"type":r["data_type"]} for r in cols],
            "sample_rows":[dict(r) for r in sample]}


@app.get("/api/debug/srid/{table_name}")
async def debug_srid(table_name: str):
    allowed = {"zoning_area","height_overlay","lot_coverage_overlay","parking_zone_overlay",
               "policy_area_overlay","building_setback_overlay","rooming_house_overlay","priority_retail_overlay"}
    if table_name not in allowed:
        raise HTTPException(400,"Unknown table")
    async with _pool.acquire() as conn:
        srid = await conn.fetchval(f"SELECT ST_SRID(geom) FROM {table_name} LIMIT 1")
        bbox = await conn.fetchrow(
            f"SELECT ST_XMin(ST_Extent(geom)) min_lng,ST_XMax(ST_Extent(geom)) max_lng,"
            f"ST_YMin(ST_Extent(geom)) min_lat,ST_YMax(ST_Extent(geom)) max_lat FROM {table_name}")
    return {"table":table_name,"srid":srid,"bbox":dict(bbox)}