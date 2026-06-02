"""Single source of truth for OBC Part 9 room-area and dimension constants.

All other packgen modules must import from here instead of maintaining local
copies.  Where the old tables disagreed, values here match OBC Part 9.
"""
from __future__ import annotations

# OBC Part 9 minimum areas (m²)
ROOM_MIN_AREA_M2: dict[str, float] = {
    "bedroom":        7.0,   # OBC §9.8.3.2
    "master_bedroom": 10.0,  # OBC §9.8.3.2
    "living":         13.5,  # OBC §9.8.3.2
    "dining":         7.0,   # OBC §9.8.3.2
    "kitchen":        4.5,   # OBC §9.8.3.2
    "bathroom":       3.0,   # OBC §9.8.3.4
    "powder_room":    1.8,
    "laundry":        1.5,
    "stair":          3.5,
    "corridor":       2.5,
    "entry":          2.0,
    "mechanical":     2.0,
    "storage":        1.5,
    "balcony":        4.0,   # practical minimum for usable outdoor space
    "void":           0.0,
    "garage":         14.0,  # 1 parking space: 2.6m × 5.4m = 14.04m² (§200.5.10.30)
}

# OBC Part 9 minimum clear dimensions (m) — §9.8.3.2 requires 2.1 m in each principal dim
ROOM_MIN_DIM_M: dict[str, float] = {
    "bedroom":        2.1,   # OBC §9.8.3.2
    "master_bedroom": 2.7,   # OBC §9.8.3.2
    "living":         3.0,
    "dining":         2.4,
    "kitchen":        1.8,   # OBC §9.8.3.2
    "bathroom":       1.5,
    "powder_room":    0.9,
    "laundry":        1.2,
    "stair":          0.86,  # OBC §9.8.2 clear stair width
    "corridor":       0.9,
    "entry":          1.0,
    "mechanical":     1.0,
    "storage":        1.0,
    "balcony":        1.5,
    "void":           0.0,
    "garage":         2.6,   # minimum stall width (§200.5.10.30)
}

# Realistic maximum areas per role (m²) — cells above these are split in template_filler
ROOM_MAX_AREA_M2: dict[str, float] = {
    "bedroom":        16.0,
    "master_bedroom": 22.0,
    "living":         32.0,
    "dining":         22.0,
    "kitchen":        16.0,
    "bathroom":        8.0,
    "powder_room":     4.5,
    "laundry":         8.0,
    "entry":           8.0,
    "stair":          12.0,
    "corridor":       15.0,
    "mechanical":     10.0,
    "storage":        12.0,
    "balcony":        20.0,
    "void":           float("inf"),
    "garage":         200.0,  # generous upper bound for multi-car garages
}

# Canonical role set — must stay in sync with Cell.role Literal in typology/models.py
VALID_ROLES: frozenset[str] = frozenset({
    "bedroom", "master_bedroom", "living", "dining", "kitchen",
    "bathroom", "powder_room", "laundry", "stair", "corridor",
    "entry", "mechanical", "storage", "balcony", "void",
    "garage",   # parking space reservation (§200.5.10.30); exterior_required=True
})

# Roles that require an operable egress window (OBC §9.7)
EGRESS_ROLES: frozenset[str] = frozenset({"bedroom", "master_bedroom"})

ROLE_ALIASES: dict[str, str] = {
    "lounge":       "living",
    "great_room":   "living",
    "family_room":  "living",
    "dining_room":  "dining",
    "bath":         "bathroom",
    "wc":           "powder_room",
    "toilet":       "powder_room",
    "utility":      "mechanical",
    "furnace":      "mechanical",
    "closet":       "storage",
    "den":          "bedroom",
    "office":       "bedroom",
    "mudroom":      "entry",
    "foyer":        "entry",
    "hall":         "corridor",
    "hallway":      "corridor",
    "stairs":       "stair",
    "staircase":    "stair",
    "laundry_room": "laundry",
}


def normalize_role(role: str) -> str:
    """Map any role string to a canonical Cell role. Falls back to 'storage'."""
    r = role.lower().strip().replace(" ", "_").replace("-", "_")
    if r in VALID_ROLES:
        return r
    mapped = ROLE_ALIASES.get(r)
    if mapped:
        return mapped
    return "storage"


# ---------------------------------------------------------------------------
# Parking rules — Chapter 200, By-law 569-2013
# All values are guidance; mark VERIFY_FOR_LOT before using in a permit set.
# ---------------------------------------------------------------------------

# §200.15.1.10 Driveway width limits
DRIVEWAY_MAX_WIDTH_NARROW_M = 6.0    # lots with frontage < 10 m   (VERIFY_FOR_LOT)
DRIVEWAY_MAX_WIDTH_WIDE_M   = 9.0    # lots with frontage ≥ 10 m   (VERIFY_FOR_LOT)

# §200.5.10.1 Residential minimum parking
# R/RD: 1 space/unit (detached/semi); multiplex: 0.5/unit; near transit: 0.
PARKING_MIN_PER_UNIT_STANDARD  = 1.0   # detached / semi (VERIFY_FOR_LOT)
PARKING_MIN_PER_UNIT_MULTIPLEX = 0.5   # duplex/triplex/fourplex (VERIFY_FOR_LOT)
PARKING_MIN_NEAR_TRANSIT       = 0.0   # within 500m of rapid transit (VERIFY_FOR_LOT)

# §200.5.10.30 Parking space dimensions
PARKING_SPACE_WIDTH_M  = 2.6    # minimum stall width
PARKING_SPACE_LENGTH_M = 5.4    # minimum stall depth
PARKING_AISLE_WIDTH_M  = 6.0    # drive aisle width for 90° parking


# ---------------------------------------------------------------------------
# FSI / density exemptions (By-law 474-2023 / 66-2024)
# ---------------------------------------------------------------------------

MULTIPLEX_ROLES: frozenset[str] = frozenset({"duplex", "triplex", "fourplex", "multiplex"})

# Building types exempt from FSI per §10.20.40.40(C) and §10.10.40.40
# (By-law 474-2023 / 66-2024 Toronto multiplex as-of-right reform)
FSI_EXEMPT_BUILDING_TYPES: frozenset[str] = MULTIPLEX_ROLES


# ---------------------------------------------------------------------------
# Permitted encroachments (§10.5.40.60)
# ---------------------------------------------------------------------------

# These reduce the effective setback for WALL placement because the element
# projecting beyond the wall is permitted under §10.5.40.60.  Do NOT
# auto-subtract from envelope setbacks — display-only allowances for architects.
EAVE_MAX_ENCROACHMENT_M   = 0.9    # §10.5.40.60(7) — eaves/roof overhangs into any yard
BAY_MAX_ENCROACHMENT_M    = 0.9    # §10.5.40.60(5) — bay windows / cantilevered projections
PORCH_DEPTH_MAX_M         = 2.0    # covered porch max projection into front yard
STEPS_MAX_ENCROACHMENT_M  = 1.5    # exterior stairs, uncovered


def is_fsi_exempt(
    units_count: int,
    building_type: str | None = None,
    zone_base: str | None = None,
    ward: int | str | None = None,
) -> bool:
    """Return True if this building is exempt from FSI under By-law 474-2023 or 654-2025.

    By-law 474-2023: Duplexes (2), triplexes (3), fourplexes (4) exempt from FSI
    in R-category zones (R/RD/RS/RT/RM) per §10.20.40.40(C).
    By-law 654-2025: Fiveplexes/sixplexes (5–6) exempt in Ward 23 / Toronto-East York.
    When zone_base is None (legacy callers) the zone check is skipped for backward compat.
    """
    if building_type and building_type.lower() in FSI_EXEMPT_BUILDING_TYPES:
        return True
    _R_CATEGORY = {"R", "RD", "RS", "RT", "RM"}
    # 654-2025: 5–6 units in Toronto–East York / Ward 23 are FSI-exempt.
    # TODO: Expand _tey check if 654-2025 scope extends beyond Ward 23 (see amendments.yaml scope field).
    if zone_base is None or zone_base in _R_CATEGORY:
        _ward_str = str(ward) if ward else ""
        _tey = _ward_str == "23" or _ward_str.startswith("toronto-east-york")
        if _tey and 5 <= units_count <= 6:
            return True
    # 474-2023: 2–4 units R-category
    if zone_base is not None and zone_base not in _R_CATEGORY:
        return False
    return 2 <= units_count <= 4
