"""Toronto Zoning By-law 569-2013 — parameter resolver.

Parses a zone symbol (e.g. ``RD (f10.5)(a300)(d0.6)(HT8.5)``) plus optional
parcel context and returns a ``ResolvedZoning`` containing all regulatory
parameters with citations, edit-mode tags, and provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

Source = Literal["zone_suffix", "overlay", "postGIS", "amendment", "default", "derived", "gis"]
EditMode = Literal["basic", "advanced", "gis"]  # "gis" = not editable


@dataclass
class ResolvedParam:
    key: str
    value: Any                  # float | int | bool | str | None
    unit: str                   # "m", "%", "int", "bool", ""
    source: Source
    citation: str
    editable_basic: bool
    editable_advanced: bool
    label: str
    description: str
    category: str               # category id e.g. "building_envelope"
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    amendment_flag: Optional[str] = None
    options: Optional[list] = None   # for enum params


@dataclass
class ResolvedZoning:
    zone_code: str
    zone_label_full: str
    params: dict[str, ResolvedParam]
    amendment_flags: list[str]
    warnings: list[str]
    categories: list[dict]      # ordered list of {id, label, param_keys}


# ---------------------------------------------------------------------------
# Zone-suffix parsing
# ---------------------------------------------------------------------------

def _parse_zone_suffixes(zone_symbol: str) -> dict:
    """Extract numeric/string values from zone-label suffix annotations.

    Handles patterns like ``(f10.5)``, ``(a300)``, ``(d0.6)``, ``(u4)``,
    ``(HT8.5)``, ``(ST2)``, ``(x736)`` embedded in a Toronto zone label.
    """
    out: dict = {}
    text = zone_symbol.upper()

    def _extract(pattern: str, key: str, as_int: bool = False):
        m = re.search(pattern, text)
        if m:
            val = float(m.group(1))
            out[key] = int(val) if as_int else val

    _extract(r'\bF([\d.]+)',  "frontage_min_m")
    _extract(r'\bA([\d.]+)', "area_min_m2")
    _extract(r'\bD([\d.]+)', "fsi_max")
    _extract(r'\bU([\d.]+)', "units_max", as_int=True)
    _extract(r'\bHT([\d.]+)', "height_max_m")
    _extract(r'\bST([\d.]+)', "storeys_max", as_int=True)

    # Exception number (x736) — lower-case 'x' in original label
    m = re.search(r'\bX([\d]+)', text)
    if m:
        out["exception_number"] = int(m.group(1))

    return out


def _zone_base(zone_symbol: str) -> str:
    """Return the base zone code, e.g. 'RD' from 'RD (f10.5)(a300)'."""
    return zone_symbol.split()[0].split("(")[0].strip().upper()


# ---------------------------------------------------------------------------
# Side-yard table (By-law 569-2013 §10.20.40.70(3) — RD zone)
# ---------------------------------------------------------------------------

def _rd_side_yard(frontage_m: Optional[float]) -> float:
    """Return minimum side yard setback for RD zone based on frontage."""
    if frontage_m is None:
        return 0.9
    f = frontage_m
    if f < 6:    return 0.6
    if f < 12:   return 0.9
    if f < 15:   return 1.2
    if f < 18:   return 1.5
    if f < 24:   return 1.8
    if f < 30:   return 2.4
    return 3.0


def _rear_yard_min(lot_depth_m: Optional[float], zone_base: str) -> float:
    """Return minimum rear yard setback.

    RD §10.20.40.70(2): greater of 7.5 m or 25% of lot depth.
    RS §10.40.40.70:    greater of 7.5 m or 25% of lot depth.
    R  §10.10.40.70:    flat 7.5 m (no 25%-depth rule).
    RT §10.60.40.70:    flat 7.5 m.
    RM §10.80:          treat as flat 7.5 m conservatively (per-zone review needed).
    """
    if zone_base in ("R", "RT", "RM"):
        return 7.5
    if lot_depth_m:
        return max(7.5, round(lot_depth_m * 0.25, 1))
    return 7.5


def _building_depth_max_m(lot_depth_m: Optional[float], frontage_m: Optional[float]) -> float:
    """§10.20.40.30(1) — max building depth (front-to-rear).

    Baseline 19.0 m for frontage ≤18 m. The deep-lot condition in §10.20.40.20(3)
    extends building *length*, not depth — depth stays 19.0 m.
    """
    return 19.0


def _building_length_max_m(lot_depth_m: Optional[float], frontage_m: Optional[float]) -> Optional[float]:
    """§10.20.40.20(1) — max building length (street-parallel).

    17.0 m baseline for frontage ≤18 m. Deep-lot exception → 19 m (§10.20.40.20(3)).
    Returns None for frontage >18 m (governed by different regulations).
    """
    if frontage_m is not None and frontage_m > 18.0:
        return None
    depth = lot_depth_m or 0
    front = frontage_m or 0
    if (depth >= 36 and front < 10) or (depth >= 40 and front >= 10):
        return 19.0
    return 17.0


# ---------------------------------------------------------------------------
# Amendment loader
# ---------------------------------------------------------------------------

_AMENDMENTS: list[dict] = []

def _load_amendments() -> list[dict]:
    global _AMENDMENTS
    if _AMENDMENTS:
        return _AMENDMENTS
    path = Path(__file__).parent.parent / "amendments.yaml"
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f)
            _AMENDMENTS = data.get("amendments", [])
    return _AMENDMENTS


def _active_amendment_flags(
    zone_base: str,
    lot_data: dict,
    suffix_vals: dict,
) -> list[str]:
    """Return human-readable amendment flags relevant to this lot."""
    flags: list[str] = []
    amendments = _load_amendments()

    for a in amendments:
        if a.get("consolidated"):
            continue
        scope = a.get("scope", "citywide")
        # Simple scope matching — extend as needed
        if scope == "citywide" or scope.startswith("district"):
            if a.get("in_force") and a.get("id"):
                flags.append(
                    f"By-law {a['id']} ({a.get('summary', '')})"
                )
    return flags


# ---------------------------------------------------------------------------
# Parameter definitions
# ---------------------------------------------------------------------------

def _make_param(
    key: str,
    value: Any,
    *,
    unit: str,
    source: Source,
    citation: str,
    editable_basic: bool,
    editable_advanced: bool,
    label: str,
    description: str,
    category: str,
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
    amendment_flag: Optional[str] = None,
    options: Optional[list] = None,
) -> ResolvedParam:
    return ResolvedParam(
        key=key, value=value, unit=unit, source=source, citation=citation,
        editable_basic=editable_basic, editable_advanced=editable_advanced,
        label=label, description=description, category=category,
        min_val=min_val, max_val=max_val,
        amendment_flag=amendment_flag, options=options,
    )


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------

def resolve_zoning(
    zone_symbol: str,
    lot_data: Optional[dict] = None,
    exception_constraints: Optional[dict] = None,
    overlay_data: Optional[dict] = None,
) -> ResolvedZoning:
    """Resolve all By-law 569-2013 parameters for the given lot.

    Parameters
    ----------
    zone_symbol:
        Full zone label from map.toronto.ca, e.g. ``RD (f10.5)(a300)(d0.6)``.
    lot_data:
        Dict with keys ``lot_area_m2``, ``lot_frontage_m``, ``lot_depth_m``,
        ``ward``, ``is_corner_lot``, ``is_through_lot``, ``has_lane_abuttal``.
    exception_constraints:
        Dict from ``/api/exception-constraints`` LLM extraction.
    overlay_data:
        Dict with overlay values (height, coverage, storeys).
    """
    lot = lot_data or {}
    ov  = overlay_data or {}
    ec  = exception_constraints or {}

    zone_base   = _zone_base(zone_symbol)
    suffix_vals = _parse_zone_suffixes(zone_symbol)
    warnings: list[str] = []

    frontage_m  = lot.get("lot_frontage_m") or suffix_vals.get("frontage_min_m")
    depth_m     = lot.get("lot_depth_m")
    area_m2     = lot.get("lot_area_m2")
    ward        = lot.get("ward")
    is_corner   = bool(lot.get("is_corner_lot", False))
    is_through  = bool(lot.get("is_through_lot", False))
    has_lane    = bool(lot.get("has_lane_abuttal", False))
    is_shallow  = bool(
        (depth_m and frontage_m and depth_m < 36 and frontage_m < 10)
        or (depth_m and frontage_m and depth_m < 40 and frontage_m >= 10)
    ) if depth_m else False

    is_res = zone_base in ("R", "RD", "RS", "RT", "RM", "RA", "RAC")
    is_cr  = zone_base.startswith("CR")

    # ── Overlay / suffix value extraction ──────────────────────────────────
    # Priority: explicit overlay_data > zone suffix > exception constraint > code default
    # Friendly key aliases (height_m, coverage_pct, overlay_fsi) are accepted alongside
    # the legacy "overlay_height_HT" / "overlay_lot_coverage_pct" keys so callers don't
    # need to know the internal naming scheme.
    height_max_overlay = (
        ov.get("overlay_height_HT")
        or ov.get("height_m")          # friendly alias from PackRequest
        or suffix_vals.get("height_max_m")
        or ec.get("max_height_m")
    )
    storeys_max_overlay = (
        ov.get("overlay_storeys_ST")
        or suffix_vals.get("storeys_max")
    )
    coverage_overlay = (
        ov.get("overlay_lot_coverage_pct")
        or ov.get("coverage_pct")      # friendly alias from PackRequest
        or ec.get("lot_coverage_pct")
    )
    # FSI: overlay value wins over zone suffix d-value and exception constraints
    _fsi_from_overlay = ov.get("overlay_fsi") or ov.get("fsi")
    _fsi_from_suffix  = suffix_vals.get("fsi_max") or ec.get("max_fsi")
    fsi_from_suffix   = _fsi_from_overlay or _fsi_from_suffix   # combined (legacy var name kept)

    # Default max height
    if height_max_overlay:
        ht_max = float(height_max_overlay)
        ht_src: Source = "overlay" if ov.get("overlay_height_HT") else "zone_suffix"
        ht_citation = "Height Overlay Map / §10.20.40.10(1)(A)"
    elif is_res:
        ht_max = 10.0
        ht_src = "default"
        ht_citation = "§10.20.40.10(1)(B)"
    else:
        ht_max = None
        ht_src = "default"
        ht_citation = "§10.20.40.10"

    # Side-yard minimum (base value; may be overridden per-side for CR zones below)
    side_min_m = (
        float(ec.get("side_setback_m", 0))
        if ec.get("side_setback_m")
        else _rd_side_yard(frontage_m) if zone_base == "RD"
        else 0.6 if zone_base in ("RS", "RT", "RM", "R")
        else 0.0
    )
    side_src: Source = "default" if not ec.get("side_setback_m") else "postGIS"

    # Per-side setbacks (may differ for CR lots abutting residential)
    left_min_m  = side_min_m
    right_min_m = side_min_m
    left_src:  Source = side_src
    right_src: Source = side_src

    # Rear-yard minimum
    rear_min_m = (
        float(ec.get("rear_setback_m")) if ec.get("rear_setback_m")
        else _rear_yard_min(depth_m, zone_base) if is_res
        else 3.0 if is_cr
        else 6.0
    )
    rear_src: Source = "default" if not ec.get("rear_setback_m") else "postGIS"

    # Front-yard
    front_min_m = (
        float(ec.get("front_setback_m")) if ec.get("front_setback_m")
        else 6.0 if is_res
        else 0.0 if is_cr
        else 3.0
    )
    front_src: Source = "default" if not ec.get("front_setback_m") else "postGIS"

    # CR zone: override setbacks based on abutting residential zones
    # §40.x.40.70 — setback from residential zone
    # TODO: verify exact sub-article number in CR chapter of By-law 569-2013
    _R_SERIES = ("R", "RD", "RS", "RT", "RM", "RA")
    if is_cr:
        _abutting = lot.get("abutting_zones") or {}
        if not _abutting:
            warnings.append(
                "CR zone: setback from abutting residential zones not verified "
                "(abutting zone data not provided). If this lot abuts an "
                "R/RD/RS/RT/RM/RA zone, a 7.5 m setback applies."
            )
        else:
            if not ec.get("rear_setback_m"):
                rear_zone = _abutting.get("rear", "")
                rear_min_m = 7.5 if any(rear_zone.startswith(z) for z in _R_SERIES) else 3.0
                rear_src = "default"
            if not ec.get("side_setback_m"):
                left_zone  = _abutting.get("left", "")
                right_zone = _abutting.get("right", "")
                left_min_m  = 7.5 if any(left_zone.startswith(z)  for z in _R_SERIES) else 0.0
                right_min_m = 7.5 if any(right_zone.startswith(z) for z in _R_SERIES) else 0.0
                left_src = right_src = "default"

    # Building depth (§10.20.40.30) and length (§10.20.40.20) — separate dimensions
    bd_max_m = (
        float(ec.get("max_building_depth_m")) if ec.get("max_building_depth_m")
        else _building_depth_max_m(depth_m, frontage_m) if is_res
        else None
    )
    bd_src: Source = "default" if not ec.get("max_building_depth_m") else "postGIS"

    bl_max_m = (
        float(ec.get("max_building_length_m")) if ec.get("max_building_length_m")
        else _building_length_max_m(depth_m, frontage_m) if is_res
        else None
    )
    bl_src: Source = "default" if not ec.get("max_building_length_m") else "postGIS"

    # Units
    units_by_law = (
        int(ec.get("max_units")) if ec.get("max_units")
        else int(suffix_vals.get("units_max")) if suffix_vals.get("units_max")
        else 6 if (is_res and zone_base in ("RD", "RS", "RT", "RM", "R"))
        else None
    )
    units_src: Source = "zone_suffix" if suffix_vals.get("units_max") else "amendment"

    # FSI
    has_d_suffix = "fsi_max" in suffix_vals
    fsi_max_val = float(fsi_from_suffix) if fsi_from_suffix else None
    # Source provenance: overlay wins over suffix, suffix wins over amendment default
    fsi_src: Source = (
        "overlay"     if _fsi_from_overlay else
        "zone_suffix" if has_d_suffix       else
        "amendment"
    )
    fsi_amendment = None if (has_d_suffix or _fsi_from_overlay) else "By-law 66-2024: FSI not limited for zones without 'd' suffix"

    # Coverage
    cov_max = float(coverage_overlay) if coverage_overlay else None
    cov_src: Source = "overlay" if coverage_overlay else "default"
    cov_note = "No coverage limit unless Lot Coverage Overlay Map shows a value (§10.20.30.40(1)(B))"

    # Parking defaults per Chapter 200
    parking_default = 1 if is_res else 0
    bike_default = 2 if is_res else 1

    # Chapter 200 computed minimum parking (VERIFY_FOR_LOT — zone/context-specific)
    from packgen.rules.code_rules import (
        DRIVEWAY_MAX_WIDTH_NARROW_M, DRIVEWAY_MAX_WIDTH_WIDE_M,
        PARKING_MIN_NEAR_TRANSIT, PARKING_MIN_PER_UNIT_MULTIPLEX,
        PARKING_MIN_PER_UNIT_STANDARD, is_fsi_exempt,
    )
    _near_transit = bool(lot.get("near_transit", False))
    _units_count  = int(lot.get("units_count", 1))
    if _near_transit:
        _min_per_unit = PARKING_MIN_NEAR_TRANSIT
    elif is_fsi_exempt(_units_count, zone_base=zone_base, ward=lot.get("ward")):
        _min_per_unit = PARKING_MIN_PER_UNIT_MULTIPLEX
    else:
        _min_per_unit = PARKING_MIN_PER_UNIT_STANDARD
    _min_parking = _min_per_unit * _units_count
    _driveway_max = (
        DRIVEWAY_MAX_WIDTH_NARROW_M if (frontage_m or 99) < 10.0
        else DRIVEWAY_MAX_WIDTH_WIDE_M
    )

    # ── Amendment flags ─────────────────────────────────────────────────────
    amendment_flags = _active_amendment_flags(zone_base, lot, suffix_vals)

    # ── Build parameter dict ────────────────────────────────────────────────
    params: dict[str, ResolvedParam] = {}

    # --- Category 1: Lot context (GIS, not editable) ---

    params["zone_code"] = _make_param(
        "zone_code", zone_base, unit="", source="gis",
        citation="§1.40.20", editable_basic=False, editable_advanced=False,
        label="Zone code", description="Base zone classification from map.toronto.ca.",
        category="lot_context",
    )
    params["zone_label_full"] = _make_param(
        "zone_label_full", zone_symbol, unit="", source="gis",
        citation="§1.40.20", editable_basic=False, editable_advanced=False,
        label="Zone label (full)", description="Complete zone label including suffix overrides.",
        category="lot_context",
    )
    if frontage_m is not None:
        params["lot_frontage_m"] = _make_param(
            "lot_frontage_m", round(frontage_m, 1), unit="m", source="postGIS",
            citation="", editable_basic=False, editable_advanced=False,
            label="Lot frontage", description="Width of the lot at the front lot line.",
            category="lot_context", min_val=0, max_val=500,
        )
    if depth_m is not None:
        params["lot_depth_m"] = _make_param(
            "lot_depth_m", round(depth_m, 1), unit="m", source="postGIS",
            citation="", editable_basic=False, editable_advanced=False,
            label="Lot depth", description="Depth of the lot from front to rear lot line.",
            category="lot_context", min_val=0, max_val=500,
        )
    if area_m2 is not None:
        params["lot_area_m2"] = _make_param(
            "lot_area_m2", round(area_m2, 0), unit="m²", source="postGIS",
            citation="", editable_basic=False, editable_advanced=False,
            label="Lot area", description="Total lot area from the City parcel database.",
            category="lot_context", min_val=0, max_val=50000,
        )
    params["is_corner_lot"] = _make_param(
        "is_corner_lot", is_corner, unit="bool", source="gis",
        citation="§10.20.40.70(6)", editable_basic=False, editable_advanced=True,
        label="Corner lot", description="Lot that abuts a street on two sides. Triggers 3.0 m flanking side yard if frontage ≥12 m.",
        category="lot_context",
    )
    params["is_through_lot"] = _make_param(
        "is_through_lot", is_through, unit="bool", source="gis",
        citation="§10.20.40.70", editable_basic=False, editable_advanced=True,
        label="Through lot", description="Lot that abuts streets on both front and rear.",
        category="lot_context",
    )
    params["has_lane_abuttal"] = _make_param(
        "has_lane_abuttal", has_lane, unit="bool", source="gis",
        citation="§150.8.30.20", editable_basic=False, editable_advanced=True,
        label="Lane abuttal", description="Lot has a laneway at the rear ≥3.5 m wide — required for laneway suite eligibility.",
        category="lot_context",
    )
    params["is_shallow_lot"] = _make_param(
        "is_shallow_lot", is_shallow, unit="bool", source="derived",
        citation="§10.20.40.20(1)", editable_basic=False, editable_advanced=False,
        label="Shallow lot", description="Depth <36 m (frontage <10 m) or <40 m (frontage ≥10 m). Building depth limit is 17 m.",
        category="lot_context",
    )
    if height_max_overlay:
        params["overlay_height_HT"] = _make_param(
            "overlay_height_HT", float(height_max_overlay), unit="m", source="overlay",
            citation="Height Overlay Map", editable_basic=False, editable_advanced=False,
            label="Height overlay (HT)", description="Maximum building height from the City Height Overlay Map. Overrides zone default.",
            category="lot_context",
        )
    if storeys_max_overlay:
        params["overlay_storeys_ST"] = _make_param(
            "overlay_storeys_ST", int(storeys_max_overlay), unit="storeys", source="overlay",
            citation="Storey Overlay Map / §10.20.40.10(2)", editable_basic=False, editable_advanced=False,
            label="Storey overlay (ST)", description="Maximum number of storeys from overlay map. Multiplexes (2–6 units) are exempt per By-law 474-2023.",
            category="lot_context",
        )
    if cov_max:
        params["overlay_lot_coverage_pct"] = _make_param(
            "overlay_lot_coverage_pct", cov_max, unit="%", source="overlay",
            citation="Lot Coverage Overlay Map", editable_basic=False, editable_advanced=False,
            label="Coverage overlay", description="Maximum lot coverage from City overlay map.",
            category="lot_context",
        )

    # --- Category 2: Building envelope ---

    params["front_yard_setback_m"] = _make_param(
        "front_yard_setback_m", front_min_m, unit="m", source=front_src,
        citation="§10.20.40.70(1) [RD], §10.10.40.70(1) [R], §10.5.40.70(1) [contextual]",
        editable_basic=True, editable_advanced=True,
        label="Front yard setback",
        description=(
            "Minimum setback from the front lot line. In R-series zones this is CONTEXTUAL "
            "(§10.5.40.70(1)): (A) if abutting one neighbour with a building ≤15 m from your "
            "lot → match that building's front yard; (B) between two neighbours → average of "
            f"both. Zone default shown: {front_min_m} m. Verify neighbour setbacks before finalising."
        ),
        category="building_envelope", min_val=0.0, max_val=15.0,
    )
    _rear_citation = (
        "§40.x.40.70 — setback from residential zone [CR]"
        if is_cr else
        "§10.20.40.70(2) [RD], §10.10.40.70 [R]"
    )
    params["rear_yard_setback_m"] = _make_param(
        "rear_yard_setback_m", rear_min_m, unit="m", source=rear_src,
        citation=_rear_citation,
        editable_basic=True, editable_advanced=True,
        label="Rear yard setback",
        description=(
            f"CR: 7.5 m if rear lot line abuts R/RD/RS/RT/RM/RA zone; 3.0 m otherwise (§40.x.40.70)."
            if is_cr else
            (
                f"RD/RS: greater of 7.5 m or 25% of lot depth ({depth_m:.1f} m × 0.25 = {round((depth_m or 0)*0.25,1)} m). "
                "R/RT/RM: flat 7.5 m (no 25%-depth rule per §10.10.40.70)."
            ) if depth_m else "RD/RS: greater of 7.5 m or 25% of lot depth. R/RT/RM: flat 7.5 m."
        ),
        category="building_envelope", min_val=0.0, max_val=30.0,
    )
    _side_citation = (
        "§40.x.40.70 — setback from residential zone [CR]"
        if is_cr else
        "§10.20.40.70(3) [RD], §10.10.40.70(3) [R]"
    )
    params["side_yard_setback_left_m"] = _make_param(
        "side_yard_setback_left_m", left_min_m, unit="m", source=left_src,
        citation=_side_citation,
        editable_basic=True, editable_advanced=True,
        label="Side yard (left)",
        description=(
            f"CR: 7.5 m if abutting R/RD/RS/RT/RM/RA zone; 0.0 m otherwise (§40.x.40.70)."
            if is_cr else
            f"Minimum side yard. RD zone scales by frontage: {frontage_m:.1f} m frontage → {left_min_m} m minimum." if frontage_m else
            "Minimum side yard setback."
        ),
        category="building_envelope", min_val=0.0, max_val=10.0,
    )
    params["side_yard_setback_right_m"] = _make_param(
        "side_yard_setback_right_m", right_min_m, unit="m", source=right_src,
        citation=_side_citation,
        editable_basic=True, editable_advanced=True,
        label="Side yard (right)",
        description=(
            "CR: 7.5 m if abutting R/RD/RS/RT/RM/RA zone; 0.0 m otherwise (§40.x.40.70)."
            if is_cr else
            "Same minimum as left side yard unless corner lot."
        ),
        category="building_envelope", min_val=0.0, max_val=10.0,
    )
    # Corner lot flankage side yard (§10.20.40.70(6))
    if zone_base == "RD" and is_corner and frontage_m and frontage_m >= 12.0:
        params["side_yard_flankage_m"] = _make_param(
            "side_yard_flankage_m", 3.0, unit="m", source="default",
            citation="§10.20.40.70(6)",
            editable_basic=False, editable_advanced=True,
            label="Flankage side yard (corner lot)",
            description=(
                "Corner lot with frontage ≥12 m: the side yard on the flankage street "
                "side must be 3.0 m (§10.20.40.70(6)), replacing the standard graduated "
                "minimum. The generator does not yet determine which side abuts the "
                "flankage street — verify and apply to the correct side before finalising."
            ),
            category="building_envelope", min_val=0.0, max_val=10.0,
        )
        warnings.append(
            "Corner lot (§10.20.40.70(6)): a 3.0 m flankage side yard applies on the "
            "street-facing side. Verify which side abuts the flankage street before "
            "finalising setbacks ('side_yard_flankage_m' param added)."
        )
    params["building_height_max_m"] = _make_param(
        "building_height_max_m", ht_max or 10.0, unit="m", source=ht_src,
        citation=ht_citation,
        editable_basic=True, editable_advanced=True,
        label="Max building height",
        description="Maximum height of the building from established grade. By-law 474-2023 guarantees a 10.0 m floor for multiplexes regardless of ST overlay.",
        category="building_envelope", min_val=3.0, max_val=50.0,
        amendment_flag="By-law 474-2023: 10.0 m height floor for multiplex (2–4 units) even in ST overlay zones" if is_res else None,
    )
    if bd_max_m:
        params["building_depth_m"] = _make_param(
            "building_depth_m", bd_max_m, unit="m", source=bd_src,
            citation="§10.20.40.30(1)",
            editable_basic=True, editable_advanced=True,
            label="Max building depth",
            description=f"Maximum building depth measured from the required front yard setback (§10.20.40.30(1)). Baseline 19 m for frontage ≤18 m. Current lot: {bd_max_m} m.",
            category="building_envelope", min_val=5.0, max_val=25.0,
        )
    params["side_yard_no_window_reduction"] = _make_param(
        "side_yard_no_window_reduction", False, unit="bool", source="default",
        citation="§10.10.40.70(4)",
        editable_basic=False, editable_advanced=True,
        label="No-window side yard reduction",
        description="Side yard may be reduced to 0.45 m if no windows or doors face that side, for permitted building types ≤13.0 m in height.",
        category="building_envelope",
    )
    params["building_height_max_storeys"] = _make_param(
        "building_height_max_storeys", storeys_max_overlay, unit="storeys", source="overlay" if storeys_max_overlay else "default",
        citation="Storey Overlay Map / §10.20.40.10(2)",
        editable_basic=False, editable_advanced=True,
        label="Max storeys (overlay)",
        description="Maximum number of above-grade storeys from the storey overlay. Multiplexes (2–6 units) are exempt from the ST limit per By-law 474-2023.",
        category="building_envelope", min_val=1, max_val=20,
        amendment_flag="By-law 474-2023: multiplex buildings exempt from storey overlay (ST) limits",
    )
    params["main_wall_height_side_m"] = _make_param(
        "main_wall_height_side_m", max(7.0, (ht_max or 10.0) - 2.5), unit="m", source="derived",
        citation="§10.20.40.10(2)",
        editable_basic=False, editable_advanced=True,
        label="Main wall height (side)",
        description="Maximum height of the main wall on the side façade = greater of 7.0 m or (HT − 2.5 m). Required over ≥70% of the side wall length.",
        category="building_envelope", min_val=4.0, max_val=20.0,
    )
    params["main_wall_height_frontrear_m"] = _make_param(
        "main_wall_height_frontrear_m", max(7.0, (ht_max or 10.0) - 2.5), unit="m", source="derived",
        citation="§10.20.40.10(2)",
        editable_basic=False, editable_advanced=True,
        label="Main wall height (front/rear)",
        description="Maximum height of the main wall on front/rear façades. Thresholds by frontage: ≥60% of wall width (frontage ≥7.5 m), ≥50% (≥15 m), ≥40% (≥24 m).",
        category="building_envelope", min_val=4.0, max_val=20.0,
    )
    params["main_wall_height_flat_roof_m"] = _make_param(
        "main_wall_height_flat_roof_m", max(7.2, (ht_max or 10.0) - 2.5), unit="m", source="derived",
        citation="§10.20.40.10(4)(A)",
        editable_basic=False, editable_advanced=True,
        label="Main wall height (flat roof)",
        description="For flat-roofed buildings: maximum wall height = greater of 7.2 m or (HT − 2.5 m). Above this, additional walls need 1.4 m setback (§10.20.40.10(4)(B)).",
        category="building_envelope", min_val=4.0, max_val=20.0,
    )
    params["parapet_extension_max_m"] = _make_param(
        "parapet_extension_max_m", 0.3, unit="m", source="default",
        citation="§10.20.40.10(5)",
        editable_basic=False, editable_advanced=True,
        label="Parapet extension",
        description="Maximum parapet extension above the maximum height: 0.3 m.",
        category="building_envelope", min_val=0.0, max_val=0.5,
    )
    params["angular_plane_rear_active"] = _make_param(
        "angular_plane_rear_active", False, unit="bool", source="default",
        citation="§150.7.60.30(2) / §150.8.60.30(2) / By-law 1260-2024",
        editable_basic=False, editable_advanced=True,
        label="Rear angular plane active",
        description="45° angular plane applies from the rear lot line for garden/laneway suites and Avenue mid-rise rear transition. Check applicable by-laws.",
        category="building_envelope",
    )
    params["first_floor_height_above_grade_max_m"] = _make_param(
        "first_floor_height_above_grade_max_m", 1.2, unit="m", source="default",
        citation="§10.10.40.10(6)",
        editable_basic=False, editable_advanced=True,
        label="Ground floor entrance height",
        description="Main pedestrian entrance must be no more than 1.2 m above established grade.",
        category="building_envelope", min_val=0.0, max_val=3.0,
    )
    params["step_back_above_storey_n_m"] = _make_param(
        "step_back_above_storey_n_m", 1.4, unit="m", source="default",
        citation="§10.20.40.10(4)(B)",
        editable_basic=False, editable_advanced=True,
        label="Stepback above main wall",
        description="Additional walls above the main wall height must step back 1.4 m from the façade.",
        category="building_envelope", min_val=0.0, max_val=5.0,
    )
    params["platforms_per_unit"] = _make_param(
        "platforms_per_unit", 2, unit="int", source="amendment",
        citation="§10.20.40.50(3) / By-law 474-2023",
        editable_basic=False, editable_advanced=True,
        label="Platforms per unit",
        description="Maximum 2 platforms (decks/balconies) per dwelling unit — one on the front side, one on the rear.",
        category="building_envelope", min_val=0, max_val=4,
    )
    params["building_length_max_m"] = _make_param(
        "building_length_max_m", bl_max_m, unit="m", source=bl_src,
        citation="§10.20.40.20(1)",
        editable_basic=False, editable_advanced=True,
        label="Max building length (street-parallel)",
        description=(
            "Maximum building length measured parallel to the street (§10.20.40.20(1)). "
            "Baseline 17 m for frontage ≤18 m; extends to 19 m on deep lots (§10.20.40.20(3)). "
            "None for wide lots (frontage >18 m) — governed by other regulations. "
            f"Current lot: {bl_max_m} m."
        ),
        category="building_envelope", min_val=5.0, max_val=25.0,
    )
    params["angular_plane_apex_height_m"] = _make_param(
        "angular_plane_apex_height_m", 4.0, unit="m", source="default",
        citation="§150.7.60.30(2) / §150.8.60.30(2)",
        editable_basic=False, editable_advanced=True,
        label="Angular plane apex height",
        description="Height at which the 45° rear angular plane begins for garden/laneway suites — 4.0 m above established grade at the rear main wall.",
        category="building_envelope", min_val=2.0, max_val=10.0,
    )
    params["angular_plane_setback_origin_m"] = _make_param(
        "angular_plane_setback_origin_m", 7.5, unit="m", source="default",
        citation="By-law 1260-2024 / §150.8.60.30(2)",
        editable_basic=False, editable_advanced=True,
        label="Angular plane setback origin",
        description="Distance from rear lot line at which the 45° avenue mid-rise rear-transition angular plane begins. Standard 7.5 m on shallow lots at 10.5 m height.",
        category="building_envelope", min_val=0.0, max_val=20.0,
    )
    params["angular_plane_degrees"] = _make_param(
        "angular_plane_degrees", 45.0, unit="°", source="default",
        citation="§150.7.60.30(2) / By-law 1260-2024",
        editable_basic=False, editable_advanced=True,
        label="Angular plane angle",
        description="Standard rear angular plane angle is 45°. Avenue mid-rise rear transition uses 45° per By-law 1260-2024.",
        category="building_envelope", min_val=30.0, max_val=60.0,
    )
    params["roof_slope_max_v_per_h"] = _make_param(
        "roof_slope_max_v_per_h", round(5 / 3, 3), unit="V:H", source="default",
        citation="§10.10.40.10(4)",
        editable_basic=False, editable_advanced=True,
        label="Max roof slope",
        description="Roof above 2nd storey on a detached house may not exceed 5:3 (vertical:horizontal) (§10.10.40.10(4)).",
        category="building_envelope", min_val=0.0, max_val=5.0,
    )
    params["dormer_max_width_pct_of_wall"] = _make_param(
        "dormer_max_width_pct_of_wall", 40.0, unit="%", source="default",
        citation="§10.10.40.10(5)",
        editable_basic=False, editable_advanced=True,
        label="Dormer max width",
        description="Dormer is not treated as the main wall if it faces ≤40% of the main wall width below (§10.10.40.10(5)).",
        category="building_envelope", min_val=0, max_val=100,
    )
    params["daylight_triangle_size_m"] = _make_param(
        "daylight_triangle_size_m", 7.5, unit="m", source="default",
        citation="§5.10.175 / Toronto sight-triangle convention",
        editable_basic=False, editable_advanced=True,
        label="Daylight triangle",
        description="7.5 m × 7.5 m sight triangle at street intersections. Fences within this zone restricted to 0.75 m height (§5.10.175).",
        category="building_envelope", min_val=5.0, max_val=15.0,
    )

    # --- Category 3: Density ---

    params["dwelling_unit_count"] = _make_param(
        "dwelling_unit_count", 1, unit="int", source="amendment",
        citation="By-law 474-2023 / §10.20.30.20",
        editable_basic=True, editable_advanced=True,
        label="Dwelling unit count",
        description=(
            "1–4 units as-of-right citywide (By-law 474-2023). "
            "5–6 units in Toronto–East York and Ward 23 (By-law 654-2025)."
        ),
        category="density", min_val=1, max_val=6,
        amendment_flag="By-law 474-2023: 1–4 units as-of-right; By-law 654-2025: 5–6 units in TEY/Ward 23",
    )
    params["fsi_max"] = _make_param(
        "fsi_max", fsi_max_val, unit="FSI", source=fsi_src,
        citation="§10.10.40.40(1)(B) / By-law 66-2024",
        editable_basic=True, editable_advanced=True,
        label="Max FSI",
        description=(
            "Floor Space Index cap. Zone 'd' suffix sets the limit. "
            "If no 'd' suffix, FSI is 'not limited by this regulation' (By-law 66-2024). "
            "FSI does not apply to duplex/triplex/fourplex (§10.20.40.40(1)(C))."
        ),
        category="density", min_val=0.1, max_val=10.0,
        amendment_flag=fsi_amendment,
    )
    params["lot_coverage_pct_max"] = _make_param(
        "lot_coverage_pct_max", cov_max, unit="%", source=cov_src,
        citation="§10.20.30.40(1)(B) / Lot Coverage Overlay Map",
        editable_basic=True, editable_advanced=True,
        label="Max lot coverage",
        description=cov_note if not cov_max else f"Maximum lot coverage from overlay: {cov_max}%.",
        category="density", min_val=1.0, max_val=100.0,
    )
    params["multiplex_fsi_exempt"] = _make_param(
        "multiplex_fsi_exempt", True, unit="bool", source="amendment",
        citation="§10.20.40.40(1)(C) / By-law 474-2023",
        editable_basic=False, editable_advanced=True,
        label="Multiplex FSI exempt",
        description="Duplex/triplex/fourplex buildings are exempt from FSI regulation (§10.20.40.40(1)(C)).",
        category="density",
    )
    params["bedrooms_per_unit_avg"] = _make_param(
        "bedrooms_per_unit_avg", None, unit="avg/unit", source="amendment",
        citation="By-law 654-2025",
        editable_basic=False, editable_advanced=True,
        label="Avg bedrooms per unit",
        description="Sixplex by-law: average ≤3 bedrooms/unit in 3+ unit buildings; maximum 8 bedrooms in a duplex. Applies only in TEY/Ward 23.",
        category="density", min_val=0, max_val=8,
        amendment_flag="By-law 654-2025: bedroom caps for fiveplex/sixplex in TEY/Ward 23",
    )

    # --- Category 4: Floor area details ---

    params["basement_excluded_gfa"] = _make_param(
        "basement_excluded_gfa", True, unit="bool", source="default",
        citation="§800.50",
        editable_basic=False, editable_advanced=True,
        label="Basement excluded from GFA",
        description="Basement floor area is excluded from GFA calculation (§800.50).",
        category="floor_area_details",
    )
    params["mechanical_excluded_gfa"] = _make_param(
        "mechanical_excluded_gfa", True, unit="bool", source="default",
        citation="§800.50",
        editable_basic=False, editable_advanced=True,
        label="Mechanical excluded from GFA",
        description="Mechanical penthouse/rooms excluded from GFA.",
        category="floor_area_details",
    )
    params["attached_garage_excluded_gfa"] = _make_param(
        "attached_garage_excluded_gfa", True, unit="bool", source="default",
        citation="§800.50",
        editable_basic=False, editable_advanced=True,
        label="Garage excluded from GFA",
        description="Attached garage floor area excluded from GFA.",
        category="floor_area_details",
    )
    params["ancillary_building_gfa_excluded"] = _make_param(
        "ancillary_building_gfa_excluded", True, unit="bool", source="default",
        citation="§150.7.60.50(1) / §150.8.60.50(1)",
        editable_basic=False, editable_advanced=True,
        label="Garden/laneway suite excluded from GFA",
        description="Garden suite and laneway suite floor area is excluded from the main building's GFA calculation.",
        category="floor_area_details",
    )
    params["min_dwelling_unit_width_townhouse_m"] = _make_param(
        "min_dwelling_unit_width_townhouse_m", 5.0, unit="m", source="default",
        citation="§10.60.40.1(3)",
        editable_basic=False, editable_advanced=True,
        label="Min unit width (townhouse)",
        description="Townhouse/street townhouse: minimum 5.0 m per unit without a private driveway; 6.0 m with a private driveway.",
        category="floor_area_details", min_val=3.0, max_val=15.0,
    )

    # --- Category 5: Parking & loading ---

    params["parking_spaces_residents"] = _make_param(
        "parking_spaces_residents", parking_default, unit="int", source="default",
        citation="Chapter 200 / By-law 223-2025",
        editable_basic=True, editable_advanced=True,
        label="Resident parking spaces",
        description="Minimum resident parking spaces. Many multiplex projects (near transit or major street) now require 0 spaces. Verify with Chapter 200.",
        category="parking_loading", min_val=0, max_val=20,
        amendment_flag="By-law 223-2025: reduced minimums near transit/major streets",
    )
    params["min_parking_spaces"] = _make_param(
        "min_parking_spaces", _min_parking, unit="int", source="default",
        citation="§200.5.10.1",
        editable_basic=True, editable_advanced=True,
        label="Min parking spaces (Chapter 200)",
        description=(
            f"Minimum parking based on {_units_count} unit(s): "
            f"{_min_per_unit}/unit × {_units_count} = {_min_parking:.1f} spaces. "
            + ("Near-transit reduction applied — minimum is 0 (VERIFY_FOR_LOT). " if _near_transit else
               "Multiplex rate (0.5/unit) applied (VERIFY_FOR_LOT). " if is_fsi_exempt(_units_count, zone_base=zone_base, ward=lot.get("ward")) else
               "Standard rate (1/unit) applied (VERIFY_FOR_LOT). ")
            + "All values are guidance only; verify with Chapter 200 and a planner."
        ),
        category="parking_loading", min_val=0, max_val=50,
        amendment_flag="By-law 223-2025: transit proximity may reduce minimum to 0" if _near_transit else None,
    )
    params["driveway_max_width_m"] = _make_param(
        "driveway_max_width_m", _driveway_max, unit="m", source="default",
        citation="§200.15.1.10",
        editable_basic=False, editable_advanced=True,
        label="Max driveway width (§200.15.1.10)",
        description=(
            f"{'Narrow' if (frontage_m or 99) < 10.0 else 'Standard'}-frontage lot: "
            f"max driveway width = {_driveway_max:.1f} m "
            f"({'frontage < 10 m' if (frontage_m or 99) < 10.0 else 'frontage ≥ 10 m'}, "
            "§200.15.1.10). VERIFY_FOR_LOT."
        ),
        category="parking_loading", min_val=2.0, max_val=12.0,
    )
    params["parking_spaces_visitor"] = _make_param(
        "parking_spaces_visitor", 0, unit="int", source="default",
        citation="§200.5.10.1 / By-law 223-2025",
        editable_basic=True, editable_advanced=True,
        label="Visitor parking spaces",
        description="Default 0.1/unit for apartment. By-law 223-2025 reduces small-apartment requirements to 1 space if ≤60 units on a major street.",
        category="parking_loading", min_val=0, max_val=20,
    )
    params["driveway_width_m"] = _make_param(
        "driveway_width_m",
        2.6 if (frontage_m or 99) < 6 else min(6.0, (frontage_m or 6)) if (frontage_m or 99) < 23 else 9.0,
        unit="m", source="default",
        citation="§10.5.50.10",
        editable_basic=False, editable_advanced=True,
        label="Max driveway width",
        description="<6 m frontage → max 2.6 m; 6–23 m → lesser of 6.0 m or side-by-side; >23 m → lesser of 9.0 m or cumulative.",
        category="parking_loading", min_val=0, max_val=12.0,
    )
    params["vehicle_entrance_through_front_wall_allowed"] = _make_param(
        "vehicle_entrance_through_front_wall_allowed",
        False if (frontage_m or 99) <= 7.6 else True,
        unit="bool", source="default",
        citation="§10.10.80.40(1)",
        editable_basic=False, editable_advanced=True,
        label="Vehicle entrance through front wall",
        description="Not permitted if lot frontage ≤7.6 m (§10.10.80.40(1)).",
        category="parking_loading",
    )
    params["bicycle_long_term"] = _make_param(
        "bicycle_long_term", bike_default, unit="int", source="default",
        citation="Chapter 230 / By-law 1116-2025 (proposed)",
        editable_basic=True, editable_advanced=True,
        label="Long-term bicycle spaces",
        description="Long-term bicycle parking. Apartment default: 0.9/unit (proposed reduction to 0.75/unit under By-law 1116-2025). Garden/laneway suites: min 2 spaces.",
        category="parking_loading", min_val=0, max_val=50,
        amendment_flag="By-law 1116-2025 (proposed): reduces apartment bicycle minimum from 0.9 to 0.75/unit",
    )
    params["bicycle_short_term"] = _make_param(
        "bicycle_short_term", 0, unit="int", source="default",
        citation="Chapter 230",
        editable_basic=False, editable_advanced=True,
        label="Short-term bicycle spaces",
        description="Short-term bicycle parking: 0.1/unit default; must be within 30 m of a pedestrian entrance.",
        category="parking_loading", min_val=0, max_val=30,
    )
    params["ev_ready_pct_residential"] = _make_param(
        "ev_ready_pct_residential", 100, unit="%", source="default",
        citation="§200.5.1.10(14)(A)",
        editable_basic=False, editable_advanced=True,
        label="EV-ready % (residential)",
        description="100% of residential parking spaces in apartment/mixed-use must be Level 2 EV-ready (§200.5.1.10(14)(A)). Other cases: 25% (§200.5.1.10(14)(B)).",
        category="parking_loading", min_val=0, max_val=100,
    )
    params["parking_space_dim_m"] = _make_param(
        "parking_space_dim_m", "5.6×2.6×2.0", unit="L×W×H m", source="default",
        citation="Chapter 200",
        editable_basic=False, editable_advanced=True,
        label="Parking space dimensions",
        description="Standard parking space: 5.6 m long × 2.6 m wide × 2.0 m vertical clearance (Chapter 200).",
        category="parking_loading",
    )
    params["accessible_parking_space_dim_m"] = _make_param(
        "accessible_parking_space_dim_m", "5.6×3.4×2.1+1.5m aisle", unit="spec", source="default",
        citation="By-law 333-2025 / Chapter 200",
        editable_basic=False, editable_advanced=True,
        label="Accessible parking dimensions",
        description="5.6 m × 3.4 m × 2.1 m clearance + 1.5 m barrier-free aisle on one side (By-law 333-2025).",
        category="parking_loading",
    )
    params["ev_outlet_in_space"] = _make_param(
        "ev_outlet_in_space", True, unit="bool", source="default",
        citation="§200.5.1.10(2)(E)",
        editable_basic=False, editable_advanced=True,
        label="EV outlet within parking space",
        description="EV equipment must be within 0.25 m of two sides of the space and ≥5.35 m from the drive aisle (§200.5.1.10(2)(E)).",
        category="parking_loading",
    )
    params["bicycle_space_dim"] = _make_param(
        "bicycle_space_dim", "horizontal_1.8×0.6×1.22", unit="type", source="default",
        citation="Chapter 230",
        editable_basic=False, editable_advanced=True,
        label="Bicycle space type",
        description="Horizontal rack: 1.8 m × 0.6 m × 1.22 m. Vertical: 1.2 m × 0.6 m × 1.9 m (Chapter 230).",
        category="parking_loading",
        options=["horizontal_1.8×0.6×1.22", "vertical_1.2×0.6×1.9"],
    )
    params["front_yard_parking_pad"] = _make_param(
        "front_yard_parking_pad", False, unit="bool", source="default",
        citation="Toronto Municipal Code Chapter 918",
        editable_basic=False, editable_advanced=True,
        label="Front yard parking pad",
        description="A Chapter 918 application to the Committee of Adjustment is required before installing a front yard parking pad.",
        category="parking_loading",
    )
    params["loading_spaces"] = _make_param(
        "loading_spaces", 0, unit="int", source="default",
        citation="Chapter 220",
        editable_basic=False, editable_advanced=True,
        label="Loading spaces",
        description="Off-street loading spaces per Chapter 220. Typically 0 for residential buildings <20 units.",
        category="parking_loading", min_val=0, max_val=10,
    )

    # --- Category 6: Landscape & site ---

    front_frontage = frontage_m or 10
    if front_frontage < 6:
        landscape_pct = 100.0
    elif front_frontage < 15:
        landscape_pct = 50.0
    else:
        landscape_pct = 60.0

    params["front_yard_landscaping_pct"] = _make_param(
        "front_yard_landscaping_pct", landscape_pct, unit="%", source="derived",
        citation="§10.5.50.10(1)",
        editable_basic=False, editable_advanced=True,
        label="Front yard landscaping",
        description="Minimum % of front yard not covered by permitted driveway that must be landscaping. <6 m frontage → 100%; 6–15 m → 50%; ≥15 m → 60%.",
        category="landscape_site", min_val=0, max_val=100,
    )
    params["front_yard_soft_landscaping_pct"] = _make_param(
        "front_yard_soft_landscaping_pct", 75.0, unit="%", source="default",
        citation="§10.5.50.10(1)(D)",
        editable_basic=False, editable_advanced=True,
        label="Front yard soft landscaping",
        description="At least 75% of the required front yard landscaping must be soft landscaping (no artificial turf).",
        category="landscape_site", min_val=0, max_val=100,
    )
    params["rear_yard_soft_landscaping_pct"] = _make_param(
        "rear_yard_soft_landscaping_pct", 50.0 if (frontage_m or 0) > 6 else 0.0,
        unit="%", source="derived",
        citation="§10.5.50.10",
        editable_basic=False, editable_advanced=True,
        label="Rear yard soft landscaping",
        description="If frontage >6 m, 50% of the rear yard area must be soft landscaping.",
        category="landscape_site", min_val=0, max_val=100,
    )
    params["permeable_surface_pct"] = _make_param(
        "permeable_surface_pct", 50.0, unit="%", source="default",
        citation="Toronto Green Standard v4 Tier 1 / EC 1.2",
        editable_basic=False, editable_advanced=True,
        label="Permeable surface",
        description="TGS Tier 1: retain 50% of average annual rainfall on-site (equivalent to 5 mm per event).",
        category="landscape_site", min_val=0, max_val=100,
    )
    params["ravine_top_of_bank_setback_m"] = _make_param(
        "ravine_top_of_bank_setback_m", 10.0, unit="m", source="default",
        citation="Official Plan §3.4.8(a)",
        editable_basic=False, editable_advanced=True,
        label="Ravine setback",
        description="10 m development setback from top-of-bank (greater where slope is unstable). Applies only if overlay_ravine is true.",
        category="landscape_site", min_val=0, max_val=50,
    )
    params["tree_protection_zone_radius_m"] = _make_param(
        "tree_protection_zone_radius_m", None, unit="m", source="gis",
        citation="Toronto Municipal Code Ch. 813",
        editable_basic=False, editable_advanced=False,
        label="Tree protection zone radius",
        description="Any tree ≥30 cm DBH on private property is protected. TPZ radius varies by DBH per Toronto Tree Protection Specifications. Verify via City arborist.",
        category="landscape_site",
    )
    params["boulevard_landscape_zone"] = _make_param(
        "boulevard_landscape_zone", False, unit="bool", source="gis",
        citation="City of Toronto Right-of-Way Management",
        editable_basic=False, editable_advanced=False,
        label="Boulevard landscape zone",
        description="Property abuts a designated public boulevard landscape zone. Public realm design restrictions apply — City approval required for any curb cut or boulevard alteration.",
        category="landscape_site",
    )

    # --- Category 7: Projections / encroachments ---

    # General encroachment allowances (display-only — NOT applied to envelope geometry)
    params["eave_encroachment_m"] = _make_param(
        "eave_encroachment_m", 0.9, unit="m", source="default",
        citation="§10.5.40.60(7)",
        editable_basic=False, editable_advanced=True,
        label="Max eave overhang",
        description=(
            "Eaves and roof overhangs may project up to 0.9 m into any required yard "
            "(§10.5.40.60(7)). This is NOT subtracted from the setback envelope — the "
            "building WALL must still respect the full setback; only the overhang beyond "
            "the wall is permitted."
        ),
        category="projections", min_val=0, max_val=1.5,
    )
    params["bay_encroachment_m"] = _make_param(
        "bay_encroachment_m", 0.9, unit="m", source="default",
        citation="§10.5.40.60(5)",
        editable_basic=False, editable_advanced=True,
        label="Bay window / projection allowance",
        description=(
            "Bay windows and cantilevered projections may encroach up to 0.9 m into "
            "front or rear yards (§10.5.40.60(5)). Display-only — do not subtract "
            "from the setback envelope."
        ),
        category="projections", min_val=0, max_val=1.5,
    )

    params["eaves_max_into_side_m"] = _make_param(
        "eaves_max_into_side_m", 0.9, unit="m", source="default",
        citation="§10.5.40.60",
        editable_basic=False, editable_advanced=True,
        label="Eaves projection (side)",
        description="Eaves may project max 0.9 m into the required side yard, but no closer than 0.3 m from the lot line.",
        category="projections", min_val=0, max_val=1.5,
    )
    params["bay_window_projection_max_m"] = _make_param(
        "bay_window_projection_max_m", 0.75, unit="m", source="default",
        citation="§10.5.40.60(6)",
        editable_basic=False, editable_advanced=True,
        label="Bay window projection",
        description="Max 0.75 m into front or rear setback; must not exceed 65% of the main wall width.",
        category="projections", min_val=0, max_val=1.5,
    )
    params["rear_deck_ground_encroachment_m"] = _make_param(
        "rear_deck_ground_encroachment_m",
        min(2.5, round((rear_min_m or 7.5) * 0.5, 1)),
        unit="m", source="derived",
        citation="§10.5.40.60(1)(C)(ii)",
        editable_basic=False, editable_advanced=True,
        label="Rear deck (ground) encroachment",
        description="Ground-level deck: lesser of 2.5 m or 50% of the required rear yard setback into the rear setback.",
        category="projections", min_val=0, max_val=5.0,
    )
    params["rear_deck_upper_encroachment_m"] = _make_param(
        "rear_deck_upper_encroachment_m",
        min(1.5, round((rear_min_m or 7.5) * 0.5, 1)),
        unit="m", source="derived",
        citation="§10.5.40.60",
        editable_basic=False, editable_advanced=True,
        label="Rear deck (upper) encroachment",
        description="Upper-level deck/balcony: lesser of 1.5 m or 50% of the required rear yard setback.",
        category="projections", min_val=0, max_val=3.0,
    )
    params["front_porch_encroachment_m"] = _make_param(
        "front_porch_encroachment_m", 1.5, unit="m", source="default",
        citation="§10.5.40.60",
        editable_basic=False, editable_advanced=True,
        label="Front porch encroachment",
        description="Front porch, stairs, columns ≤33 cm wide, and guardrails ≤107 cm high may project up to 1.5 m into the front yard setback.",
        category="projections", min_val=0, max_val=3.0,
    )
    params["ac_hvac_projection_max_m"] = _make_param(
        "ac_hvac_projection_max_m", 0.6, unit="m", source="default",
        citation="§10.5.40.60",
        editable_basic=False, editable_advanced=True,
        label="AC/HVAC projection",
        description="Wall-mounted mechanical equipment (AC condensers, heat pumps) may project up to 0.6 m into a required setback (§10.5.40.60).",
        category="projections", min_val=0.0, max_val=1.5,
    )
    params["chimney_breast_max_width_m"] = _make_param(
        "chimney_breast_max_width_m", 2.0, unit="m", source="default",
        citation="§10.5.40.60",
        editable_basic=False, editable_advanced=True,
        label="Chimney breast max width",
        description="A chimney breast ≤2.0 m wide may project up to 0.6 m into a required setback (§10.5.40.60).",
        category="projections", min_val=0.0, max_val=4.0,
    )

    # --- Category 8: Amenity (applies to buildings ≥20 units) ---

    params["amenity_total_m2_per_unit"] = _make_param(
        "amenity_total_m2_per_unit", 4.0, unit="m²/unit", source="default",
        citation="§10.10.40.50(1)",
        editable_basic=False, editable_advanced=True,
        label="Total amenity area",
        description="Applies to buildings with ≥20 dwelling units. Minimum 4.0 m² per unit total amenity (indoor + outdoor).",
        category="amenity", min_val=0, max_val=20,
    )
    params["amenity_indoor_min_m2_per_unit"] = _make_param(
        "amenity_indoor_min_m2_per_unit", 2.0, unit="m²/unit", source="default",
        citation="§10.10.40.50(1)",
        editable_basic=False, editable_advanced=True,
        label="Indoor amenity area",
        description="Minimum 2.0 m² indoor amenity per unit (of the 4.0 m² total).",
        category="amenity", min_val=0, max_val=10,
    )
    params["amenity_outdoor_min_total_m2"] = _make_param(
        "amenity_outdoor_min_total_m2", 40.0, unit="m²", source="default",
        citation="§10.10.40.50(1)",
        editable_basic=False, editable_advanced=True,
        label="Min outdoor amenity total",
        description="Minimum 40 m² total outdoor amenity space for ≥20 unit buildings.",
        category="amenity", min_val=0, max_val=500,
    )
    params["amenity_green_roof_share_max_pct"] = _make_param(
        "amenity_green_roof_share_max_pct", 25.0, unit="%", source="default",
        citation="§10.10.40.50(1)",
        editable_basic=False, editable_advanced=True,
        label="Green roof share of amenity",
        description="At most 25% of the required outdoor amenity area may be counted as green roof (§10.10.40.50(1)).",
        category="amenity", min_val=0, max_val=100,
    )

    # --- Category 9: Accessibility ---

    params["barrier_free_entry_count"] = _make_param(
        "barrier_free_entry_count", 1, unit="int", source="default",
        citation="OBC / Toronto Accessibility Design Guidelines",
        editable_basic=False, editable_advanced=True,
        label="Barrier-free entrances",
        description="At least one accessible entrance required (TADG). Clear path width ≥1.5 m.",
        category="accessibility", min_val=0, max_val=10,
    )
    params["barrier_free_path_width_m"] = _make_param(
        "barrier_free_path_width_m", 1.5, unit="m", source="default",
        citation="Toronto Accessibility Design Guidelines",
        editable_basic=False, editable_advanced=True,
        label="Barrier-free path width",
        description="Minimum 1.5 m clear width for barrier-free paths to the accessible entrance.",
        category="accessibility", min_val=0.9, max_val=5.0,
    )
    params["accessible_parking_count"] = _make_param(
        "accessible_parking_count", 0, unit="int", source="default",
        citation="By-law 333-2025 / Chapter 200",
        editable_basic=False, editable_advanced=True,
        label="Accessible parking spaces",
        description="5.6 m × 3.4 m × 2.1 m clearance + 1.5 m barrier-free aisle (By-law 333-2025).",
        category="accessibility", min_val=0, max_val=10,
    )
    params["visitable_suite_count"] = _make_param(
        "visitable_suite_count", 0, unit="int", source="default",
        citation="OBC Part 3 / Toronto Accessibility Design Guidelines",
        editable_basic=False, editable_advanced=True,
        label="Visitable suites",
        description="Number of suites designed to be visitable — barrier-free entrance, accessible path, and ground-floor powder room (TADG).",
        category="accessibility", min_val=0, max_val=50,
    )

    # --- Category 10: Sustainability (Toronto Green Standard v4 Tier 1) ---

    params["tgs_tier"] = _make_param(
        "tgs_tier", 1, unit="", source="default",
        citation="Toronto Green Standard v4 / Municipal Code",
        editable_basic=False, editable_advanced=True,
        label="TGS Tier",
        description="Toronto Green Standard Tier 1 is mandatory. Tier 2 and 3 are voluntary.",
        category="sustainability",
        options=[1, 2, 3],
    )
    params["ev_ready_residential"] = _make_param(
        "ev_ready_residential", True, unit="bool", source="default",
        citation="TGS v4 AQ 1.1 / §200.5.1.10(14)(A)",
        editable_basic=False, editable_advanced=True,
        label="EV-ready (residential)",
        description="Each unit with a parking space must have an energized outlet or EVSE Level 2 charger; 100% of shared parking must be Level 2 ready.",
        category="sustainability",
    )
    params["native_plant_pct"] = _make_param(
        "native_plant_pct", 50.0, unit="%", source="default",
        citation="TGS v4 Tier 1 EC 1.3",
        editable_basic=False, editable_advanced=True,
        label="Native plant %",
        description="TGS Tier 1: ≥50% of plant species by count must be native Ontario species.",
        category="sustainability", min_val=0, max_val=100,
    )
    params["green_roof_required"] = _make_param(
        "green_roof_required",
        False,   # only mandatory for buildings ≥2,000 m² GFA
        unit="bool", source="default",
        citation="Municipal Code Ch. 492 / Toronto Green Roof By-law",
        editable_basic=False, editable_advanced=True,
        label="Green roof required",
        description="Mandatory for buildings ≥2,000 m² GFA. Low-rise alternative: 25% pollinator-friendly green roof (TGS Tier 1).",
        category="sustainability",
    )
    params["water_balance_retention_pct"] = _make_param(
        "water_balance_retention_pct", 50.0, unit="%", source="default",
        citation="TGS v4 Tier 1 WR 1.1",
        editable_basic=False, editable_advanced=True,
        label="Stormwater retention",
        description="Retain 50% of average annual rainfall on-site (equivalent to 5 mm per event).",
        category="sustainability", min_val=0, max_val=100,
    )
    _tree_vol = round(((0.4 * (area_m2 or 0)) / 66) * 30, 1) if area_m2 else 30.0
    _tree_vol = max(30.0, _tree_vol)
    params["tree_canopy_soil_volume_m3"] = _make_param(
        "tree_canopy_soil_volume_m3", _tree_vol, unit="m³", source="derived",
        citation="TGS v4 Tier 1 EC 1.1",
        editable_basic=False, editable_advanced=True,
        label="Tree canopy soil volume",
        description=f"EC 1.1 formula: (40% of site area ÷ 66 m²) × 30 m³; min 30 m³ per tree. Calculated: {_tree_vol} m³.",
        category="sustainability", min_val=0, max_val=5000,
    )
    params["surface_parking_trees_ratio"] = _make_param(
        "surface_parking_trees_ratio", "1:5", unit="", source="default",
        citation="TGS v4 Tier 1 EC 1.1",
        editable_basic=False, editable_advanced=True,
        label="Surface parking tree ratio",
        description="One tree required per 5 surface parking spaces (TGS Tier 1 EC 1.1).",
        category="sustainability",
        options=["1:5"],
    )
    params["tss_removal_pct"] = _make_param(
        "tss_removal_pct", 80.0, unit="%", source="default",
        citation="TGS v4 Tier 1 WR 1.2",
        editable_basic=False, editable_advanced=True,
        label="TSS removal %",
        description="Remove 80% of total suspended solids from stormwater runoff (TGS Tier 1 WR 1.2).",
        category="sustainability", min_val=0, max_val=100,
    )
    params["potable_water_reduction_pct"] = _make_param(
        "potable_water_reduction_pct", 40.0, unit="%", source="default",
        citation="TGS v4 Tier 1 WE 1.1",
        editable_basic=False, editable_advanced=True,
        label="Potable water reduction %",
        description="Reduce potable water use by 40% vs. baseline using low-flow fixtures (TGS Tier 1 WE 1.1).",
        category="sustainability", min_val=0, max_val=100,
    )
    params["cool_paving_pct"] = _make_param(
        "cool_paving_pct", 75.0, unit="%", source="default",
        citation="TGS v4 Tier 1 UHI 1.1",
        editable_basic=False, editable_advanced=True,
        label="Cool paving %",
        description="≥75% of non-roof hardscape must use high-albedo or permeable materials to reduce urban heat island effect (TGS Tier 1 UHI 1.1).",
        category="sustainability", min_val=0, max_val=100,
    )

    # --- Special provisions ---

    params["secondary_suite"] = _make_param(
        "secondary_suite", is_res, unit="bool", source="default",
        citation="§150.10",
        editable_basic=False, editable_advanced=True,
        label="Secondary suite",
        description="As-of-right in detached, semi-detached, and townhouse buildings (§150.10). Does not count towards multiplex unit limit.",
        category="special_provisions",
    )
    params["garden_suite"] = _make_param(
        "garden_suite", is_res, unit="bool", source="default",
        citation="§150.7",
        editable_basic=False, editable_advanced=True,
        label="Garden suite",
        description="As-of-right in residential zones. Max 60 m² ground / 120 m² total (2 storeys). Max height 6.0 m (6.3 m in Beach area). Rear setback 1.5 m. Separation from main house: 5.0 m if ≤4 m tall, 7.5 m if >4 m.",
        category="special_provisions",
    )
    params["laneway_suite"] = _make_param(
        "laneway_suite", has_lane, unit="bool", source="gis",
        citation="§150.8",
        editable_basic=False, editable_advanced=True,
        label="Laneway suite",
        description="Eligible only if lot has a ≥3.5 m laneway at the rear (§150.8.30.20). Max 10 m long × 8 m wide. Max height 6.3 m. Rear setback 0 m (no openings) or 1.0 m (with openings).",
        category="special_provisions",
    )
    params["heritage_permit_required"] = _make_param(
        "heritage_permit_required", False, unit="bool", source="gis",
        citation="Ontario Heritage Act Part IV / V",
        editable_basic=False, editable_advanced=True,
        label="Heritage permit required",
        description="Exterior changes visible from the street require a Heritage Alteration Permit for Part IV designated or Part V HCD properties. Heritage Impact Assessment typically required.",
        category="special_provisions",
    )
    params["ravine_permit_required"] = _make_param(
        "ravine_permit_required", False, unit="bool", source="gis",
        citation="Municipal Code Ch. 658",
        editable_basic=False, editable_advanced=True,
        label="Ravine permit required",
        description="Development in the Ravine and Natural Feature Protection overlay requires a permit under Municipal Code Chapter 658.",
        category="special_provisions",
    )
    params["trca_permit_required"] = _make_param(
        "trca_permit_required", False, unit="bool", source="gis",
        citation="O.Reg. 166/06",
        editable_basic=False, editable_advanced=True,
        label="TRCA permit required",
        description="Development in TRCA regulated areas requires a permit under Ontario Regulation 166/06.",
        category="special_provisions",
    )
    params["avenue_overlay"] = _make_param(
        "avenue_overlay", False, unit="bool", source="gis",
        citation="Chapter 400 / By-law 1260-2024",
        editable_basic=False, editable_advanced=True,
        label="Avenue overlay",
        description="Property is on a designated Avenue — mid-rise development permissions apply (Chapter 400, updated by By-law 1260-2024).",
        category="special_provisions",
    )
    params["multi_tenant_house"] = _make_param(
        "multi_tenant_house", False, unit="bool", source="default",
        citation="§150.25 / By-law 58-2026",
        editable_basic=False, editable_advanced=True,
        label="Multi-tenant house",
        description="A multi-tenant house (rooming house) is permitted subject to §150.25 and updated under By-law 58-2026.",
        category="special_provisions",
    )
    params["archaeological_assessment_required"] = _make_param(
        "archaeological_assessment_required", False, unit="bool", source="gis",
        citation="Ontario Heritage Act / Stage 1–2 Archaeological Assessment",
        editable_basic=False, editable_advanced=True,
        label="Archaeological assessment",
        description="Stage 1 or 2 archaeological assessment may be required if the lot falls within a designated potential area of archaeological concern (PAAC). Confirm with City Heritage Planning.",
        category="special_provisions",
    )
    params["holding_symbol"] = _make_param(
        "holding_symbol", None, unit="", source="gis",
        citation="§900 / Planning Act §36",
        editable_basic=False, editable_advanced=True,
        label="Holding symbol (H)",
        description="If an (H) holding symbol applies, no development may proceed until the holding by-law is removed by Council under Planning Act §36. Triggers a Zoning By-law Amendment application.",
        category="special_provisions",
    )
    # Suite-specific envelope params — emitted when a garden or laneway suite is possible
    if is_res and (has_lane or is_res):   # garden suites: all residential; laneway: lane abuttal
        params["suite_max_height_m"] = _make_param(
            "suite_max_height_m", 6.0, unit="m", source="default",
            citation="§150.1.40.10 / §150.7.60.40(1)",
            editable_basic=False, editable_advanced=True,
            label="Suite max height",
            description=(
                "Maximum height of a garden or laneway suite: 6.0 m from established grade "
                "(§150.7.60.40(1)). Laneway suites may reach 6.3 m with ≥7.5 m separation "
                "from the main house (§150.8.60.40(1))."
            ),
            category="special_provisions", min_val=3.0, max_val=8.0,
        )
        params["suite_min_rear_setback_m"] = _make_param(
            "suite_min_rear_setback_m", 1.0, unit="m", source="default",
            citation="§150.1.40.70 / §150.7.60.20(2)",
            editable_basic=False, editable_advanced=True,
            label="Suite min rear setback",
            description=(
                "Minimum setback of a garden or laneway suite from the rear lot line: "
                "1.0 m when openings face the lane/rear; 0.0 m when no openings face the "
                "rear (laneway suite only, §150.8.60.20(2))."
            ),
            category="special_provisions", min_val=0.0, max_val=5.0,
        )
        params["suite_min_side_setback_m"] = _make_param(
            "suite_min_side_setback_m", 1.5, unit="m", source="default",
            citation="§150.1.40.70 / §150.7.60.20(3)",
            editable_basic=False, editable_advanced=True,
            label="Suite min side setback",
            description=(
                "Minimum setback of a garden suite from each side lot line: 1.5 m when "
                "openings face that side; 0.6 m when no openings face that side "
                "(§150.7.60.20(3))."
            ),
            category="special_provisions", min_val=0.0, max_val=5.0,
        )

    # Garden suite child block (§150.7)
    _gs_rear = max(1.5, round((rear_min_m or 7.5) * 0.5, 1)) if (depth_m or 0) > 45 else 1.5
    params["gs_gfa_m2"] = _make_param(
        "gs_gfa_m2", 60.0, unit="m²", source="default",
        citation="§150.7.60.70(1)(C)",
        editable_basic=False, editable_advanced=True,
        label="Garden suite — max GFA (ground)",
        description="Max garden suite footprint: 60 m² on ground level; 120 m² total over 2 storeys (§150.7.60.70(1)(C)).",
        category="special_provisions", min_val=10, max_val=120,
    )
    params["gs_max_height_m"] = _make_param(
        "gs_max_height_m", 6.0, unit="m", source="default",
        citation="§150.7.60.40(1)",
        editable_basic=False, editable_advanced=True,
        label="Garden suite — max height",
        description="Max garden suite height: 6.0 m default; 6.3 m in Beach area (§150.7.60.40(1)).",
        category="special_provisions", min_val=3.0, max_val=8.0,
    )
    params["gs_rear_setback_m"] = _make_param(
        "gs_rear_setback_m", _gs_rear, unit="m", source="derived",
        citation="§150.7.60.20(2)",
        editable_basic=False, editable_advanced=True,
        label="Garden suite — rear setback",
        description=f"Min 1.5 m rear setback; half of suite height if lot depth >45 m. Calculated: {_gs_rear} m (§150.7.60.20(2)).",
        category="special_provisions", min_val=0.0, max_val=10.0,
    )
    params["gs_side_setback_m"] = _make_param(
        "gs_side_setback_m", 1.5, unit="m", source="default",
        citation="§150.7.60.20(3)",
        editable_basic=False, editable_advanced=True,
        label="Garden suite — side setback",
        description="≥1.5 m with openings; ≥0.6 m without openings; capped at the greater of 10% of frontage (§150.7.60.20(3)).",
        category="special_provisions", min_val=0.0, max_val=10.0,
    )
    params["gs_separation_m"] = _make_param(
        "gs_separation_m", 5.0, unit="m", source="default",
        citation="§150.7.60.30(1)",
        editable_basic=False, editable_advanced=True,
        label="Garden suite — separation from house",
        description="5.0 m if suite height ≤4 m; 7.5 m if suite height >4 m (§150.7.60.30(1)).",
        category="special_provisions", min_val=0.0, max_val=15.0,
    )
    params["gs_angular_plane_active"] = _make_param(
        "gs_angular_plane_active", True, unit="bool", source="default",
        citation="§150.7.60.30(2)",
        editable_basic=False, editable_advanced=True,
        label="Garden suite — angular plane active",
        description="45° rear angular plane applies from 4.0 m height at the rear main wall of the garden suite (§150.7.60.30(2)).",
        category="special_provisions",
    )
    # Laneway suite child block (§150.8) — only populated if lane abuttal present
    params["ls_max_length_m"] = _make_param(
        "ls_max_length_m", 10.0, unit="m", source="default",
        citation="§150.8.60.10(1)",
        editable_basic=False, editable_advanced=True,
        label="Laneway suite — max length",
        description="Maximum laneway suite length: 10.0 m parallel to the rear lot line (§150.8.60.10(1)).",
        category="special_provisions", min_val=3.0, max_val=12.0,
    )
    params["ls_max_width_m"] = _make_param(
        "ls_max_width_m", 8.0, unit="m", source="default",
        citation="§150.8.60.10(2)",
        editable_basic=False, editable_advanced=True,
        label="Laneway suite — max width",
        description="Maximum laneway suite width (perpendicular to rear lot line): 8.0 m (§150.8.60.10(2)).",
        category="special_provisions", min_val=3.0, max_val=10.0,
    )
    params["ls_rear_setback_m"] = _make_param(
        "ls_rear_setback_m", 0.0, unit="m", source="default",
        citation="§150.8.60.20(2)",
        editable_basic=False, editable_advanced=True,
        label="Laneway suite — rear setback",
        description="0.0 m if no openings face the lane; 1.0 m if openings face the lane (§150.8.60.20(2)).",
        category="special_provisions", min_val=0.0, max_val=3.0,
    )
    params["ls_max_height_m"] = _make_param(
        "ls_max_height_m", 6.3, unit="m", source="default",
        citation="§150.8.60.40(1)",
        editable_basic=False, editable_advanced=True,
        label="Laneway suite — max height",
        description="Maximum height 6.3 m when separation from main house is ≥7.5 m (§150.8.60.40(1)).",
        category="special_provisions", min_val=3.0, max_val=8.0,
    )
    params["ls_separation_m"] = _make_param(
        "ls_separation_m", 7.5, unit="m", source="default",
        citation="§150.8.60.40(2)",
        editable_basic=False, editable_advanced=True,
        label="Laneway suite — separation from house",
        description="Minimum 7.5 m separation between laneway suite and main house for maximum height to apply (§150.8.60.40(2)).",
        category="special_provisions", min_val=0.0, max_val=20.0,
    )

    # ── Ordered category list ───────────────────────────────────────────────
    categories = [
        {"id": "lot_context",        "label": "Lot Context",               "mode": "gis"},
        {"id": "building_envelope",  "label": "Building Envelope",         "mode": "basic"},
        {"id": "density",            "label": "Density & GFA",             "mode": "basic"},
        {"id": "floor_area_details", "label": "Floor Area Details",        "mode": "advanced"},
        {"id": "parking_loading",    "label": "Parking & Loading",         "mode": "basic"},
        {"id": "landscape_site",     "label": "Landscape & Site",          "mode": "advanced"},
        {"id": "projections",        "label": "Projections / Encroachments","mode": "advanced"},
        {"id": "amenity",            "label": "Amenity (≥20 units)",       "mode": "advanced"},
        {"id": "accessibility",      "label": "Accessibility",             "mode": "advanced"},
        {"id": "sustainability",     "label": "Sustainability (TGS v4)",   "mode": "advanced"},
        {"id": "special_provisions", "label": "Special Provisions",        "mode": "advanced"},
    ]

    # Attach param_keys to each category
    for cat in categories:
        cat["param_keys"] = [k for k, p in params.items() if p.category == cat["id"]]

    if not is_res:
        warnings.append(
            f"Zone {zone_base} is not a standard residential zone. "
            "Some parameters may not apply."
        )

    return ResolvedZoning(
        zone_code=zone_base,
        zone_label_full=zone_symbol,
        params=params,
        amendment_flags=amendment_flags,
        warnings=warnings,
        categories=categories,
    )
