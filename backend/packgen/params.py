"""Parameter validation and default-derivation for the pack generator.

Reads zone symbol + parcel context → returns validated EnvelopeParams
that can be passed directly to geometry.build_envelope().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from packgen.zoning_resolver import ResolvedZoning


@dataclass
class EnvelopeParams:
    """Validated parameters for geometry.build_envelope()."""
    front_setback_m: float
    rear_setback_m: float
    left_setback_m: float
    right_setback_m: float
    max_height_m: Optional[float]
    max_coverage_pct: Optional[float]
    include_laneway: bool
    lot_frontage_m: Optional[float]
    units_target: int
    layout_option: Optional[str]    # "A" | "B" | None
    ward: Optional[int]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Zone-specific defaults (By-law 569-2013 §10.xx.40)
# ---------------------------------------------------------------------------

def _rd_defaults(zone_symbol: str) -> dict:
    """Return setback defaults for RD zones (§10.20.40)."""
    return {
        "front": 6.0,    # §10.20.40.70(1) [RD], contextual per §10.5.40.70(1)
        "rear":  7.5,    # §10.20.40.70(2) [RD] — greater of 7.5 or 25% depth
        "side":  0.9,    # §10.20.40.70(3) [RD] — graduated by frontage
        "coverage": 33.0,
    }


def _rs_defaults(zone_symbol: str) -> dict:
    return {
        "front": 6.0,
        "rear":  7.5,
        "side":  0.6,
        "coverage": 33.0,
    }


def _r_defaults(zone_symbol: str) -> dict:
    """Generic residential fallback."""
    return {
        "front": 3.0,
        "rear":  6.0,
        "side":  0.6,
        "coverage": 45.0,
    }


def _cr_defaults(zone_symbol: str) -> dict:
    return {
        "front": 0.0,
        "rear":  3.0,
        "side":  0.0,
        "coverage": None,
    }


_ZONE_DEFAULTS: dict[str, callable] = {
    "RD": _rd_defaults,
    "RS": _rs_defaults,
    "R":  _r_defaults,
    "RM": _r_defaults,
    "RT": _r_defaults,
    "CR": _cr_defaults,
}


def _zone_defaults(zone_symbol: str) -> dict:
    for prefix, fn in _ZONE_DEFAULTS.items():
        if zone_symbol.startswith(prefix):
            return fn(zone_symbol)
    return _r_defaults(zone_symbol)


# ---------------------------------------------------------------------------
# Exception override merging
# ---------------------------------------------------------------------------

def _apply_exception_overrides(defaults: dict, exception_constraints: dict) -> dict:
    """Merge LLM-extracted exception constraints over zone defaults."""
    merged = dict(defaults)
    ec = exception_constraints or {}
    if "front_setback_m" in ec:
        merged["front"] = float(ec["front_setback_m"])
    if "rear_setback_m" in ec:
        merged["rear"] = float(ec["rear_setback_m"])
    if "side_setback_m" in ec:
        merged["side"] = float(ec["side_setback_m"])
    if "lot_coverage_pct" in ec:
        merged["coverage"] = float(ec["lot_coverage_pct"])
    if "max_height_m" in ec:
        merged["max_height"] = float(ec["max_height_m"])
    return merged


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# DEPRECATED — use resolved_to_envelope_params instead.
# This function uses hardcoded zone defaults that disagree with zoning_resolver.
# Kept for backward compatibility only.
def derive_params(
    zone_symbol: str,
    *,
    exception_constraints: Optional[dict] = None,
    override: Optional[dict] = None,
    units_target: int = 1,
    layout_option: Optional[str] = None,
    ward: Optional[int] = None,
    lot_frontage_m: Optional[float] = None,
    include_laneway: bool = False,
) -> EnvelopeParams:
    """Derive and validate EnvelopeParams from zone + optional overrides.

    exception_constraints: dict from /api/exception-constraints (LLM-extracted).
    override: dict of manual user overrides (highest priority).
    """
    warnings: list[str] = []
    defaults = _zone_defaults(zone_symbol)

    if exception_constraints:
        defaults = _apply_exception_overrides(defaults, exception_constraints)
        warnings.append(
            "Exception constraints applied — review by-law section for accuracy."
        )

    # Manual overrides win over everything
    if override:
        for key, val in override.items():
            if key in ("front", "rear", "left", "side", "right", "coverage",
                       "max_height"):
                defaults[key] = float(val)

    front = float(defaults.get("front", 3.0))
    rear  = float(defaults.get("rear", 6.0))
    side  = float(defaults.get("side", 0.6))
    left  = float(defaults.get("left", side))
    right = float(defaults.get("right", side))
    coverage = defaults.get("coverage", None)
    if coverage is not None:
        coverage = float(coverage)
    max_height = defaults.get("max_height", None)
    if max_height is not None:
        max_height = float(max_height)

    # Contextual front-yard warning for R-series zones (§10.5.40.70(1))
    # (A) abutting one neighbour with a building ≤15 m → match that building's front yard;
    # (B) between two neighbours → average of both.
    _r_series = ("R ", "RD", "RS", "RT")
    _front_overridden = override and "front" in override
    if zone_symbol.startswith(_r_series) and not _front_overridden:
        warnings.append(
            "Front yard setback is CONTEXTUAL (§10.5.40.70(1)): "
            "(A) if abutting one neighbour with a building ≤15 m from your lot → match "
            "that building's front yard; (B) between two neighbours → average of both. "
            f"The value used here ({front:.1f}m) is a zone default approximation. "
            "Verify the actual neighbour setbacks before finalizing the design."
        )

    # Sanity checks
    if front < 0 or rear < 0 or left < 0 or right < 0:
        warnings.append("One or more setbacks resolved to a negative value — clamped to 0.")
        front = max(0.0, front)
        rear  = max(0.0, rear)
        left  = max(0.0, left)
        right = max(0.0, right)

    if coverage is not None and not (0 < coverage <= 100):
        warnings.append(f"Coverage {coverage}% out of range; ignoring.")
        coverage = None

    return EnvelopeParams(
        front_setback_m=front,
        rear_setback_m=rear,
        left_setback_m=left,
        right_setback_m=right,
        max_height_m=max_height,
        max_coverage_pct=coverage,
        include_laneway=include_laneway,
        lot_frontage_m=lot_frontage_m,
        units_target=units_target,
        layout_option=layout_option,
        ward=ward,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Resolver-backed entry point (preferred over derive_params)
# ---------------------------------------------------------------------------

def resolved_to_envelope_params(
    resolved: "ResolvedZoning",
    lot_data: dict,
    override: Optional[dict],
    units_target: int,
    layout_option: Optional[str],
    include_laneway: bool,
) -> EnvelopeParams:
    """Map ResolvedZoning.params → EnvelopeParams for build_envelope.

    Reads the authoritative By-law values computed by zoning_resolver.resolve_zoning(),
    then applies any manual overrides last. Coverage is set to None for zones where the
    bylaw does not impose a default limit (R, RD) — only real overlay values are used.
    """
    p = resolved.params
    warnings: list[str] = []
    zone_base = resolved.zone_code
    frontage_m = lot_data.get("lot_frontage_m")

    def _val(key: str, fallback: float) -> float:
        param = p.get(key)
        if param is not None and param.value is not None:
            return float(param.value)
        return fallback

    # Graduated RD side-yard fallback (mirrors zoning_resolver._rd_side_yard)
    from packgen.zoning_resolver import _rd_side_yard
    _default_side = _rd_side_yard(frontage_m) if zone_base == "RD" else 0.6

    front = _val("front_yard_setback_m", 6.0)
    rear  = _val("rear_yard_setback_m",  7.5)
    left  = _val("side_yard_setback_left_m",  _default_side)
    right = _val("side_yard_setback_right_m", _default_side)

    # Coverage: use resolved value ONLY when the overlay actually provides one.
    # R/RD have no default lot coverage cap in By-law 569-2013 (§10.20.30.40(1)(B)).
    cov_param = p.get("lot_coverage_pct_max")
    coverage: Optional[float] = (
        float(cov_param.value)
        if cov_param is not None
        and cov_param.value is not None
        and cov_param.source in ("overlay", "postGIS")
        else None
    )

    # Height from resolver (10.0 m default for residential)
    ht_param = p.get("building_height_max_m")
    max_height: Optional[float] = (
        float(ht_param.value) if ht_param is not None and ht_param.value is not None
        else None
    )

    # Carry resolver warnings forward
    warnings.extend(resolved.warnings)

    # Contextual front-yard warning for R-series zones (§10.5.40.70(1))
    _r_series = ("R", "RD", "RS", "RT")
    _front_overridden = bool(override and "front" in override)
    if zone_base in _r_series and not _front_overridden:
        warnings.append(
            "Front yard setback is CONTEXTUAL (§10.5.40.70(1)): "
            "(A) if abutting one neighbour with a building ≤15 m from your lot → match "
            "that building's front yard; (B) between two neighbours → average of both. "
            f"The value used here ({front:.1f} m) is the zone default approximation. "
            "Verify the actual neighbour setbacks before finalising the design."
        )

    # Manual overrides win over everything (highest priority)
    if override:
        for key, val in override.items():
            if key == "front":       front    = float(val)
            elif key == "rear":      rear     = float(val)
            elif key == "left":      left     = float(val)
            elif key == "right":     right    = float(val)
            elif key == "side":
                left  = float(val)
                right = float(val)
            elif key == "coverage":  coverage   = float(val) if val is not None else None
            elif key == "max_height": max_height = float(val)

    if front < 0 or rear < 0 or left < 0 or right < 0:
        warnings.append("One or more setbacks resolved to a negative value — clamped to 0.")
        front = max(0.0, front)
        rear  = max(0.0, rear)
        left  = max(0.0, left)
        right = max(0.0, right)

    if coverage is not None and not (0 < coverage <= 100):
        warnings.append(f"Coverage {coverage}% out of range; ignoring.")
        coverage = None

    return EnvelopeParams(
        front_setback_m=front,
        rear_setback_m=rear,
        left_setback_m=left,
        right_setback_m=right,
        max_height_m=max_height,
        max_coverage_pct=coverage,
        include_laneway=include_laneway,
        lot_frontage_m=frontage_m,
        units_target=units_target,
        layout_option=layout_option,
        ward=lot_data.get("ward"),
        warnings=warnings,
    )
