"""Toronto Zoning By-law 569-2013 — per-parameter validation.

Each public function takes proposed build values and a ``ResolvedZoning``
(or individual resolved values) and returns a ``ValidationResult`` with
``status = 'ok' | 'variance' | 'violation'``.

'variance' — exceeds the by-law limit but within the Committee of
             Adjustment (CoA) minor-variance tolerance from
             ``variance_thresholds.yaml``.
'violation' — exceeds both the by-law limit AND the CoA tolerance.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

import yaml

from .rules.code_rules import EAVE_MAX_ENCROACHMENT_M, is_fsi_exempt
from .zoning_resolver import ResolvedParam, ResolvedZoning

if TYPE_CHECKING:
    from .schemas.zoning_snapshot import ZoningSnapshot

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Status = Literal["ok", "variance", "violation", "exempt", "na"]


@dataclass
class ValidationResult:
    status: Status
    message: str
    citation: str
    param_key: str = ""
    proposed: Any = None
    limit: Any = None
    tolerance: Any = None


# ---------------------------------------------------------------------------
# Threshold loader
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _load_thresholds() -> dict:
    path = Path(__file__).parent.parent / "variance_thresholds.yaml"
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _thr(section: str, key: str, default: float = 0.0) -> float:
    return float(_load_thresholds().get(section, {}).get(key, default))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(key: str, limit: Any, proposed: Any, citation: str) -> ValidationResult:
    return ValidationResult(
        status="ok",
        message=f"Compliant. Proposed {proposed} vs limit {limit}.",
        citation=citation,
        param_key=key,
        proposed=proposed,
        limit=limit,
    )


def _variance(key: str, limit: Any, proposed: Any, tol: Any, citation: str, msg: str) -> ValidationResult:
    return ValidationResult(
        status="variance",
        message=msg,
        citation=citation,
        param_key=key,
        proposed=proposed,
        limit=limit,
        tolerance=tol,
    )


def _violation(key: str, limit: Any, proposed: Any, citation: str, msg: str) -> ValidationResult:
    return ValidationResult(
        status="violation",
        message=msg,
        citation=citation,
        param_key=key,
        proposed=proposed,
        limit=limit,
    )


def _na(key: str, reason: str, citation: str = "") -> ValidationResult:
    return ValidationResult(status="na", message=reason, citation=citation, param_key=key)


def _exempt(key: str, reason: str, citation: str = "") -> ValidationResult:
    return ValidationResult(status="exempt", message=reason, citation=citation, param_key=key)


# ---------------------------------------------------------------------------
# 1. Building envelope
# ---------------------------------------------------------------------------

def validate_front_yard(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "front_yard_setback_m"
    p: Optional[ResolvedParam] = resolved.params.get(key)
    if p is None:
        return _na(key, "Parameter not present for this zone.")
    limit_m: float = float(p.value)
    tol = _thr("building_envelope", "front_yard_setback_m")
    delta = limit_m - proposed_m          # positive → under setback minimum
    if delta <= 0:
        return _ok(key, limit_m, proposed_m, p.citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_m, tol, p.citation,
                         f"Front yard {proposed_m:.2f} m is {delta:.2f} m below the {limit_m:.2f} m minimum — "
                         f"within CoA minor variance tolerance of {tol} m.")
    if delta <= EAVE_MAX_ENCROACHMENT_M:
        return _variance(key, limit_m, proposed_m, EAVE_MAX_ENCROACHMENT_M, "§10.5.40.60",
                         f"Front yard {proposed_m:.2f} m is {delta:.2f} m below the {limit_m:.2f} m minimum. "
                         "Within permitted eave/bay encroachment range (§10.5.40.60). "
                         "Verify the encroaching element qualifies.")
    return _violation(key, limit_m, proposed_m, p.citation,
                      f"Front yard {proposed_m:.2f} m is {delta:.2f} m below the {limit_m:.2f} m minimum, "
                      f"exceeding the CoA variance tolerance of {tol} m. A Zoning Board Appeal may be required.")


def validate_rear_yard(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "rear_yard_setback_m"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Parameter not present for this zone.")
    limit_m = float(p.value)
    tol = _thr("building_envelope", "rear_yard_setback_m")
    delta = limit_m - proposed_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_m, p.citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_m, tol, p.citation,
                         f"Rear yard {proposed_m:.2f} m is {delta:.2f} m short of the {limit_m:.2f} m minimum.")
    if delta <= EAVE_MAX_ENCROACHMENT_M:
        return _variance(key, limit_m, proposed_m, EAVE_MAX_ENCROACHMENT_M, "§10.5.40.60",
                         f"Rear yard {proposed_m:.2f} m is {delta:.2f} m short of the {limit_m:.2f} m minimum. "
                         "Within permitted eave/bay encroachment range (§10.5.40.60). "
                         "Verify the encroaching element qualifies.")
    return _violation(key, limit_m, proposed_m, p.citation,
                      f"Rear yard {proposed_m:.2f} m is {delta:.2f} m short of the {limit_m:.2f} m minimum. "
                      "Exceeds CoA variance tolerance — ZBA or court approval required.")


def validate_side_yard(
    proposed_left_m: float,
    proposed_right_m: float,
    resolved: ResolvedZoning,
) -> list[ValidationResult]:
    results = []
    tol = _thr("building_envelope", "side_yard_setback_m")
    for side, proposed_m in (("left", proposed_left_m), ("right", proposed_right_m)):
        key = f"side_yard_setback_{side}_m"
        p = resolved.params.get(key)
        if p is None:
            results.append(_na(key, f"Side yard ({side}) not resolved for this zone."))
            continue
        limit_m = float(p.value)
        delta = limit_m - proposed_m
        if delta <= 0:
            results.append(_ok(key, limit_m, proposed_m, p.citation))
        elif delta <= tol:
            results.append(_variance(key, limit_m, proposed_m, tol, p.citation,
                                     f"Side yard ({side}) {proposed_m:.2f} m is {delta:.2f} m below minimum {limit_m:.2f} m. "
                                     f"Tight CoA tolerance: {tol} m."))
        elif delta <= EAVE_MAX_ENCROACHMENT_M:
            results.append(_variance(key, limit_m, proposed_m, EAVE_MAX_ENCROACHMENT_M, "§10.5.40.60",
                                     f"Side yard ({side}) {proposed_m:.2f} m is {delta:.2f} m below minimum {limit_m:.2f} m. "
                                     "Within permitted eave/bay encroachment range (§10.5.40.60). "
                                     "Verify the encroaching element qualifies."))
        else:
            results.append(_violation(key, limit_m, proposed_m, p.citation,
                                      f"Side yard ({side}) {proposed_m:.2f} m is {delta:.2f} m short — "
                                      f"exceeds CoA tolerance of {tol} m."))
    return results


def validate_building_height(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "building_height_max_m"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Height limit not applicable for this zone.")
    limit_m = float(p.value)
    tol = _thr("building_envelope", "building_height_max_m")
    delta = proposed_m - limit_m           # positive → over limit
    if delta <= 0:
        return _ok(key, limit_m, proposed_m, p.citation)
    # Check multiplex floor exemption: 474-2023 guarantees min 10.0 m
    if proposed_m <= 10.0 and resolved.params.get("multiplex_fsi_exempt", None):
        return ValidationResult(
            status="ok",
            message=f"Height {proposed_m:.2f} m exceeds zone default {limit_m:.2f} m but is ≤10.0 m — "
                    "permitted under By-law 474-2023 multiplex height floor.",
            citation="By-law 474-2023 / §10.20.40.10",
            param_key=key,
            proposed=proposed_m,
            limit=10.0,
        )
    if delta <= tol:
        return _variance(key, limit_m, proposed_m, tol, p.citation,
                         f"Height {proposed_m:.2f} m exceeds {limit_m:.2f} m limit by {delta:.2f} m — "
                         f"within CoA tolerance of {tol} m.")
    return _violation(key, limit_m, proposed_m, p.citation,
                      f"Height {proposed_m:.2f} m exceeds {limit_m:.2f} m limit by {delta:.2f} m, "
                      f"beyond CoA tolerance {tol} m.")


def validate_building_depth(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "building_depth_m"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Building depth limit not applicable for this zone.")
    limit_m = float(p.value)
    tol = _thr("building_envelope", "building_depth_m")
    delta = proposed_m - limit_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_m, p.citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_m, tol, p.citation,
                         f"Building depth {proposed_m:.2f} m exceeds {limit_m:.2f} m limit by {delta:.2f} m.")
    return _violation(key, limit_m, proposed_m, p.citation,
                      f"Building depth {proposed_m:.2f} m exceeds {limit_m:.2f} m limit by {delta:.2f} m, "
                      "beyond CoA tolerance. A Zoning Board Appeal is required.")


def validate_main_wall_height_side(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "main_wall_height_side_m"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Main wall height (side) not applicable.")
    limit_m = float(p.value)
    tol = _thr("building_envelope", "main_wall_height_side_m")
    delta = proposed_m - limit_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_m, p.citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_m, tol, p.citation,
                         f"Side main wall height {proposed_m:.2f} m exceeds {limit_m:.2f} m limit by {delta:.2f} m.")
    return _violation(key, limit_m, proposed_m, p.citation,
                      f"Side main wall height {proposed_m:.2f} m exceeds {limit_m:.2f} m limit by {delta:.2f} m.")


def validate_main_wall_height_frontrear(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "main_wall_height_frontrear_m"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Main wall height (front/rear) not applicable.")
    limit_m = float(p.value)
    tol = _thr("building_envelope", "main_wall_height_frontrear_m")
    delta = proposed_m - limit_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_m, p.citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_m, tol, p.citation,
                         f"Front/rear main wall height {proposed_m:.2f} m exceeds {limit_m:.2f} m by {delta:.2f} m.")
    return _violation(key, limit_m, proposed_m, p.citation,
                      f"Front/rear main wall height {proposed_m:.2f} m exceeds {limit_m:.2f} m by {delta:.2f} m.")


# ---------------------------------------------------------------------------
# 2. Density
# ---------------------------------------------------------------------------

def validate_fsi(
    proposed_fsi: float,
    proposed_units: int,
    resolved: ResolvedZoning,
    ward: int | str | None = None,
) -> ValidationResult:
    key = "fsi_max"
    p = resolved.params.get(key)
    # Multiplex exemption: 2–4 units (474-2023) or 5–6 units in Ward 23 (654-2025)
    if is_fsi_exempt(proposed_units, zone_base=resolved.zone_code, ward=ward):
        return _exempt(key,
                       f"FSI regulation does not apply to {proposed_units}-unit multiplex (§10.20.40.40(1)(C) / By-law 474-2023).",
                       "§10.20.40.40(1)(C)")
    if p is None or p.value is None:
        return _na(key, "No FSI limit for this zone (By-law 66-2024: no 'd' suffix present).",
                   "§10.10.40.40(1)(B) / By-law 66-2024")
    limit = float(p.value)
    tol = _thr("density", "fsi_max")
    delta = proposed_fsi - limit
    if delta <= 0:
        return _ok(key, limit, proposed_fsi, p.citation)
    if delta <= tol:
        return _variance(key, limit, proposed_fsi, tol, p.citation,
                         f"FSI {proposed_fsi:.2f} exceeds cap {limit:.2f} by {delta:.2f} — within CoA tolerance.")
    return _violation(key, limit, proposed_fsi, p.citation,
                      f"FSI {proposed_fsi:.2f} exceeds cap {limit:.2f} by {delta:.2f}. CoA tolerance is {tol}.")


def validate_lot_coverage(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "lot_coverage_pct_max"
    p = resolved.params.get(key)
    if p is None or p.value is None:
        return _na(key, "No lot coverage limit for this parcel (no overlay map value).",
                   "§10.20.30.40(1)(B)")
    limit = float(p.value)
    tol = _thr("density", "lot_coverage_pct")
    delta = proposed_pct - limit
    if delta <= 0:
        return _ok(key, limit, proposed_pct, p.citation)
    if delta <= tol:
        return _variance(key, limit, proposed_pct, tol, p.citation,
                         f"Lot coverage {proposed_pct:.1f}% exceeds {limit:.1f}% by {delta:.1f} pts — within CoA tolerance {tol} pts.")
    return _violation(key, limit, proposed_pct, p.citation,
                      f"Lot coverage {proposed_pct:.1f}% exceeds {limit:.1f}% by {delta:.1f} pts — beyond CoA tolerance.")


def validate_dwelling_unit_count(
    proposed_units: int,
    resolved: ResolvedZoning,
    ward: Optional[str] = None,
) -> ValidationResult:
    key = "dwelling_unit_count"
    p = resolved.params.get(key)
    zone_base = resolved.zone_code

    # Determine the as-of-right limit under current amendments
    if zone_base in ("R", "RD", "RS", "RT", "RM", "RA"):
        # By-law 474-2023: 4 units citywide
        base_limit = 4
        # By-law 654-2025: 6 units in TEY / Ward 23
        tey_eligible = bool(ward and (ward.startswith("toronto-east-york") or str(ward) == "23"))
        unit_limit = 6 if tey_eligible else 4
        citation = "By-law 474-2023 (474-2023); By-law 654-2025 (TEY/Ward 23)"
    else:
        unit_limit = resolved.params.get("dwelling_unit_count", {}).value if p else None  # type: ignore[union-attr]
        if unit_limit is None:
            return _na(key, "Dwelling unit rules vary for this zone — consult Chapter 10.", "§10.20.30.20")
        citation = p.citation if p else "§10.20.30.20"

    if proposed_units <= unit_limit:
        return _ok(key, unit_limit, proposed_units, citation)

    tol = int(_thr("density", "dwelling_unit_count"))
    if tol == 0:
        return _violation(key, unit_limit, proposed_units, citation,
                          f"{proposed_units} units exceeds as-of-right limit of {unit_limit}. "
                          "No minor variance available — a Zoning By-law Amendment (ZBA) is required.")
    # If tol > 0, variance might be possible
    if proposed_units - unit_limit <= tol:
        return _variance(key, unit_limit, proposed_units, tol, citation,
                         f"{proposed_units} units is {proposed_units - unit_limit} over the {unit_limit}-unit limit.")
    return _violation(key, unit_limit, proposed_units, citation,
                      f"{proposed_units} units exceeds the {unit_limit}-unit limit by {proposed_units - unit_limit}. ZBA required.")


# ---------------------------------------------------------------------------
# 3. Parking
# ---------------------------------------------------------------------------

def validate_min_parking_spaces(
    proposed_spaces: float,
    units: int,
    resolved: ResolvedZoning,
) -> ValidationResult:
    """Chapter 200 §200.5.10.1 — minimum parking per unit count.

    Returns a WARNING (variance) when parking is below the minimum — since the
    minimum may be waived near transit or via By-law 223-2025 variance.
    """
    key = "min_parking_spaces"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Minimum parking not resolved for this lot.")
    min_spaces = float(p.value)
    if proposed_spaces >= min_spaces:
        return _ok(key, min_spaces, proposed_spaces, p.citation)
    return _variance(key, min_spaces, proposed_spaces, min_spaces - proposed_spaces, p.citation,
                     f"Parking: {proposed_spaces:.1f} spaces provided, minimum is {min_spaces:.1f} for "
                     f"{units} unit(s). Minimum may be waived near rapid transit (By-law 223-2025) or by "
                     "Committee of Adjustment — verify with a planner. (VERIFY_FOR_LOT)")


def validate_driveway_max_width(
    proposed_width_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    """§200.15.1.10 — maximum driveway width."""
    key = "driveway_max_width_m"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Driveway width limit not resolved for this lot.")
    limit_m = float(p.value)
    delta = proposed_width_m - limit_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_width_m, p.citation)
    return _violation(key, limit_m, proposed_width_m, p.citation,
                      f"Driveway {proposed_width_m:.2f} m exceeds the §200.15.1.10 maximum of "
                      f"{limit_m:.2f} m for this frontage. A wider driveway requires Committee of "
                      "Adjustment approval. (VERIFY_FOR_LOT)")


def validate_parking_residents(
    proposed_spaces: int,
    resolved: ResolvedZoning,
    near_transit: bool = False,
) -> ValidationResult:
    key = "parking_spaces_residents"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Parking requirement not resolved.")

    # By-law 223-2025: near rapid transit → 0 required for multiplex
    if near_transit:
        return _ok(key, 0, proposed_spaces,
                   "By-law 223-2025: 0 resident spaces required near rapid transit for multiplex projects.")

    required = int(p.value)
    tol = int(_thr("parking", "parking_spaces_residents"))
    if proposed_spaces >= required:
        return _ok(key, required, proposed_spaces, p.citation)
    deficit = required - proposed_spaces
    if deficit <= tol:
        return _variance(key, required, proposed_spaces, tol, p.citation,
                         f"Parking: {proposed_spaces} of {required} required spaces provided.")
    return _violation(key, required, proposed_spaces, p.citation,
                      f"Parking: {proposed_spaces} spaces provided, {required} required. "
                      "No minor variance allowed for parking — deficit requires ZBA or By-law 223-2025 exemption.")


def validate_parking_visitor(
    proposed_spaces: int,
    resolved: ResolvedZoning,
    num_units: int = 1,
) -> ValidationResult:
    key = "parking_spaces_visitor"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Visitor parking not resolved.")
    required = int(p.value)
    tol = int(_thr("parking", "parking_spaces_visitor"))
    if proposed_spaces >= required:
        return _ok(key, required, proposed_spaces, p.citation)
    if (proposed_spaces - required) >= -tol:
        return _variance(key, required, proposed_spaces, tol, p.citation,
                         f"Visitor parking: {proposed_spaces} of {required} required.")
    return _violation(key, required, proposed_spaces, p.citation,
                      f"Visitor parking: {proposed_spaces} of {required} required. No variance permitted.")


# ---------------------------------------------------------------------------
# 4. Landscape
# ---------------------------------------------------------------------------

def validate_front_yard_landscaping(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "front_yard_landscaping_pct"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Front yard landscaping requirement not resolved.")
    required = float(p.value)
    tol = _thr("landscape", "front_yard_landscaping_pct")
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, p.citation)
    if shortfall <= tol:
        return _variance(key, required, proposed_pct, tol, p.citation,
                         f"Front yard landscaping {proposed_pct:.1f}% is {shortfall:.1f} pts below {required:.1f}% minimum.")
    return _violation(key, required, proposed_pct, p.citation,
                      f"Front yard landscaping {proposed_pct:.1f}% is {shortfall:.1f} pts below {required:.1f}% minimum. Exceeds CoA tolerance.")


def validate_front_yard_soft_landscaping(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "front_yard_soft_landscaping_pct"
    p = resolved.params.get(key)
    if p is None:
        return _na(key, "Soft landscaping requirement not resolved.")
    required = float(p.value)
    tol = _thr("landscape", "front_yard_soft_landscaping_pct")
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, p.citation)
    if shortfall <= tol:
        return _variance(key, required, proposed_pct, tol, p.citation,
                         f"Soft landscaping {proposed_pct:.1f}% is {shortfall:.1f} pts below {required:.1f}% minimum.")
    return _violation(key, required, proposed_pct, p.citation,
                      f"Soft landscaping {proposed_pct:.1f}% is {shortfall:.1f} pts below {required:.1f}% minimum.")


# ---------------------------------------------------------------------------
# 5. Projections / encroachments
# ---------------------------------------------------------------------------

def validate_eaves(
    proposed_projection_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "eaves_max_into_side_m"
    p = resolved.params.get(key)
    limit_m = float(p.value) if p else 0.9
    tol = _thr("projections", "eaves_max_into_side_m")
    citation = p.citation if p else "§10.5.40.60"
    delta = proposed_projection_m - limit_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_projection_m, citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_projection_m, tol, citation,
                         f"Eaves project {proposed_projection_m:.2f} m, max {limit_m:.2f} m.")
    return _violation(key, limit_m, proposed_projection_m, citation,
                      f"Eaves project {proposed_projection_m:.2f} m, exceeding {limit_m:.2f} m limit.")


def validate_rear_deck_ground(
    proposed_encroachment_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "rear_deck_ground_encroachment_m"
    p = resolved.params.get(key)
    limit_m = float(p.value) if p else 2.5
    tol = _thr("projections", "rear_deck_ground_encroachment_m")
    citation = p.citation if p else "§10.5.40.60(1)(C)(ii)"
    delta = proposed_encroachment_m - limit_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_encroachment_m, citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_encroachment_m, tol, citation,
                         f"Ground deck encroachment {proposed_encroachment_m:.2f} m exceeds {limit_m:.2f} m limit by {delta:.2f} m.")
    return _violation(key, limit_m, proposed_encroachment_m, citation,
                      f"Ground deck encroachment {proposed_encroachment_m:.2f} m exceeds {limit_m:.2f} m limit.")


def validate_rear_deck_upper(
    proposed_encroachment_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "rear_deck_upper_encroachment_m"
    p = resolved.params.get(key)
    limit_m = float(p.value) if p else 1.5
    tol = _thr("projections", "rear_deck_upper_encroachment_m")
    citation = p.citation if p else "§10.5.40.60"
    delta = proposed_encroachment_m - limit_m
    if delta <= 0:
        return _ok(key, limit_m, proposed_encroachment_m, citation)
    if delta <= tol:
        return _variance(key, limit_m, proposed_encroachment_m, tol, citation,
                         f"Upper deck encroachment {proposed_encroachment_m:.2f} m exceeds {limit_m:.2f} m limit.")
    return _violation(key, limit_m, proposed_encroachment_m, citation,
                      f"Upper deck encroachment {proposed_encroachment_m:.2f} m exceeds {limit_m:.2f} m limit.")


# ---------------------------------------------------------------------------
# 6. Sustainability validators
# ---------------------------------------------------------------------------

def validate_permeable_surface(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "permeable_surface_pct"
    p = resolved.params.get(key)
    required = float(p.value) if p else 50.0
    citation = p.citation if p else "TGS v4 Tier 1 EC 1.2"
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, citation)
    return _variance(key, required, proposed_pct, shortfall, citation,
                     f"Permeable surface {proposed_pct:.1f}% is {shortfall:.1f} pts below TGS Tier 1 requirement of {required:.1f}%.")


def validate_native_plant_pct(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "native_plant_pct"
    p = resolved.params.get(key)
    required = float(p.value) if p else 50.0
    citation = p.citation if p else "TGS v4 Tier 1 EC 1.3"
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, citation)
    return _variance(key, required, proposed_pct, shortfall, citation,
                     f"Native plant proportion {proposed_pct:.1f}% is {shortfall:.1f} pts below TGS Tier 1 minimum {required:.1f}%.")


def validate_water_balance_retention(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "water_balance_retention_pct"
    p = resolved.params.get(key)
    required = float(p.value) if p else 50.0
    citation = p.citation if p else "TGS v4 Tier 1 WR 1.1"
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, citation)
    return _variance(key, required, proposed_pct, shortfall, citation,
                     f"Stormwater retention {proposed_pct:.1f}% is {shortfall:.1f} pts below TGS Tier 1 minimum {required:.1f}%.")


def validate_tss_removal(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "tss_removal_pct"
    p = resolved.params.get(key)
    required = float(p.value) if p else 80.0
    citation = p.citation if p else "TGS v4 Tier 1 WR 1.2"
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, citation)
    return _variance(key, required, proposed_pct, shortfall, citation,
                     f"TSS removal {proposed_pct:.1f}% is {shortfall:.1f} pts below TGS Tier 1 minimum {required:.1f}%.")


def validate_potable_water_reduction(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "potable_water_reduction_pct"
    p = resolved.params.get(key)
    required = float(p.value) if p else 40.0
    citation = p.citation if p else "TGS v4 Tier 1 WE 1.1"
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, citation)
    return _variance(key, required, proposed_pct, shortfall, citation,
                     f"Potable water reduction {proposed_pct:.1f}% is {shortfall:.1f} pts below TGS Tier 1 minimum {required:.1f}%.")


def validate_cool_paving(
    proposed_pct: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "cool_paving_pct"
    p = resolved.params.get(key)
    required = float(p.value) if p else 75.0
    citation = p.citation if p else "TGS v4 Tier 1 UHI 1.1"
    shortfall = required - proposed_pct
    if shortfall <= 0:
        return _ok(key, required, proposed_pct, citation)
    return _variance(key, required, proposed_pct, shortfall, citation,
                     f"Cool paving {proposed_pct:.1f}% is {shortfall:.1f} pts below TGS Tier 1 minimum {required:.1f}%.")


# ---------------------------------------------------------------------------
# 7. Amenity validators
# ---------------------------------------------------------------------------

def validate_amenity_total(
    proposed_m2_per_unit: float,
    num_units: int,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "amenity_total_m2_per_unit"
    if num_units < 20:
        return _exempt(key, f"Amenity area requirement applies only to buildings with ≥20 units (this building has {num_units}).",
                       "§10.10.40.50(1)")
    p = resolved.params.get(key)
    required = float(p.value) if p else 4.0
    citation = p.citation if p else "§10.10.40.50(1)"
    shortfall = required - proposed_m2_per_unit
    if shortfall <= 0:
        return _ok(key, required, proposed_m2_per_unit, citation)
    return _violation(key, required, proposed_m2_per_unit, citation,
                      f"Amenity area {proposed_m2_per_unit:.1f} m²/unit is below {required:.1f} m²/unit minimum for ≥20 unit building.")


def validate_amenity_indoor(
    proposed_m2_per_unit: float,
    num_units: int,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "amenity_indoor_min_m2_per_unit"
    if num_units < 20:
        return _exempt(key, "Indoor amenity requirement applies only to buildings with ≥20 units.", "§10.10.40.50(1)")
    p = resolved.params.get(key)
    required = float(p.value) if p else 2.0
    citation = p.citation if p else "§10.10.40.50(1)"
    shortfall = required - proposed_m2_per_unit
    if shortfall <= 0:
        return _ok(key, required, proposed_m2_per_unit, citation)
    return _violation(key, required, proposed_m2_per_unit, citation,
                      f"Indoor amenity {proposed_m2_per_unit:.1f} m²/unit below {required:.1f} m²/unit minimum.")


# ---------------------------------------------------------------------------
# 8. Garden suite validators
# ---------------------------------------------------------------------------

def validate_gs_gfa(
    proposed_gfa_m2: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "gs_gfa_m2"
    p = resolved.params.get("garden_suite")
    if p and not p.value:
        return _na(key, "Garden suite not permitted or not applicable for this lot.", "§150.7")
    limit = 60.0
    delta = proposed_gfa_m2 - limit
    if delta <= 0:
        return _ok(key, limit, proposed_gfa_m2, "§150.7.60.70(1)(C)")
    return _violation(key, limit, proposed_gfa_m2, "§150.7.60.70(1)(C)",
                      f"Garden suite GFA {proposed_gfa_m2:.1f} m² exceeds {limit:.1f} m² ground-floor maximum.")


def validate_gs_height(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "gs_max_height_m"
    p = resolved.params.get(key)
    limit = float(p.value) if p else 6.0
    citation = p.citation if p else "§150.7.60.40(1)"
    delta = proposed_m - limit
    if delta <= 0:
        return _ok(key, limit, proposed_m, citation)
    if delta <= 0.5:
        return _variance(key, limit, proposed_m, 0.5, citation,
                         f"Garden suite height {proposed_m:.2f} m exceeds {limit:.2f} m limit by {delta:.2f} m.")
    return _violation(key, limit, proposed_m, citation,
                      f"Garden suite height {proposed_m:.2f} m exceeds {limit:.2f} m limit.")


def validate_gs_rear_setback(
    proposed_m: float,
    resolved: ResolvedZoning,
) -> ValidationResult:
    key = "gs_rear_setback_m"
    p = resolved.params.get(key)
    limit = float(p.value) if p else 1.5
    citation = p.citation if p else "§150.7.60.20(2)"
    delta = limit - proposed_m
    if delta <= 0:
        return _ok(key, limit, proposed_m, citation)
    return _violation(key, limit, proposed_m, citation,
                      f"Garden suite rear setback {proposed_m:.2f} m is below the {limit:.2f} m minimum.")


# ---------------------------------------------------------------------------
# 9. Batch validator
# ---------------------------------------------------------------------------

def validate_all(
    proposed: dict,
    resolved: ResolvedZoning,
    *,
    near_transit: bool = False,
    ward: Optional[str] = None,
) -> list[ValidationResult]:
    """Run every applicable validator and return results.

    ``proposed`` keys mirror ResolvedParam keys, e.g.::

        {
          "front_yard_setback_m": 4.5,
          "rear_yard_setback_m": 7.5,
          "side_yard_setback_left_m": 0.9,
          "side_yard_setback_right_m": 0.9,
          "building_height_max_m": 10.0,
          "building_depth_m": 17.0,
          "fsi_max": 0.55,
          "lot_coverage_pct_max": 35.0,
          "dwelling_unit_count": 2,
          "parking_spaces_residents": 1,
          "parking_spaces_visitor": 0,
          "front_yard_landscaping_pct": 50.0,
          "front_yard_soft_landscaping_pct": 75.0,
          "eaves_max_into_side_m": 0.9,
          "rear_deck_ground_encroachment_m": 2.0,
          "rear_deck_upper_encroachment_m": 1.2,
          "main_wall_height_side_m": 7.5,
          "main_wall_height_frontrear_m": 7.5,
          "permeable_surface_pct": 50.0,
          "native_plant_pct": 50.0,
          "water_balance_retention_pct": 50.0,
          "tss_removal_pct": 80.0,
          "potable_water_reduction_pct": 40.0,
          "cool_paving_pct": 75.0,
          "amenity_total_m2_per_unit": 4.0,
          "amenity_indoor_min_m2_per_unit": 2.0,
          "gs_gfa_m2": 55.0,
          "gs_max_height_m": 5.5,
          "gs_rear_setback_m": 1.5,
        }
    """
    results: list[ValidationResult] = []
    p = proposed

    def _get(key: str):
        return p.get(key)

    # Building envelope
    if (v := _get("front_yard_setback_m")) is not None:
        results.append(validate_front_yard(float(v), resolved))

    if (v := _get("rear_yard_setback_m")) is not None:
        results.append(validate_rear_yard(float(v), resolved))

    left = _get("side_yard_setback_left_m")
    right = _get("side_yard_setback_right_m")
    if left is not None or right is not None:
        results.extend(validate_side_yard(
            float(left) if left is not None else 99.0,
            float(right) if right is not None else 99.0,
            resolved,
        ))

    if (v := _get("building_height_max_m")) is not None:
        results.append(validate_building_height(float(v), resolved))

    if (v := _get("building_depth_m")) is not None:
        results.append(validate_building_depth(float(v), resolved))

    if (v := _get("main_wall_height_side_m")) is not None:
        results.append(validate_main_wall_height_side(float(v), resolved))

    if (v := _get("main_wall_height_frontrear_m")) is not None:
        results.append(validate_main_wall_height_frontrear(float(v), resolved))

    # Density
    units = int(_get("dwelling_unit_count") or 1)
    results.append(validate_dwelling_unit_count(units, resolved, ward=ward))

    if (v := _get("fsi_max")) is not None:
        results.append(validate_fsi(float(v), units, resolved, ward=ward))

    if (v := _get("lot_coverage_pct_max")) is not None:
        results.append(validate_lot_coverage(float(v), resolved))

    # Parking
    if (v := _get("parking_spaces_residents")) is not None:
        results.append(validate_parking_residents(int(v), resolved, near_transit=near_transit))

    if (v := _get("parking_spaces_visitor")) is not None:
        results.append(validate_parking_visitor(int(v), resolved, num_units=units))

    if (v := _get("min_parking_spaces")) is not None:
        results.append(validate_min_parking_spaces(float(v), units, resolved))

    if (v := _get("driveway_max_width_m")) is not None:
        results.append(validate_driveway_max_width(float(v), resolved))

    # Landscape
    if (v := _get("front_yard_landscaping_pct")) is not None:
        results.append(validate_front_yard_landscaping(float(v), resolved))

    if (v := _get("front_yard_soft_landscaping_pct")) is not None:
        results.append(validate_front_yard_soft_landscaping(float(v), resolved))

    # Projections
    if (v := _get("eaves_max_into_side_m")) is not None:
        results.append(validate_eaves(float(v), resolved))

    if (v := _get("rear_deck_ground_encroachment_m")) is not None:
        results.append(validate_rear_deck_ground(float(v), resolved))

    if (v := _get("rear_deck_upper_encroachment_m")) is not None:
        results.append(validate_rear_deck_upper(float(v), resolved))

    # Sustainability
    if (v := _get("permeable_surface_pct")) is not None:
        results.append(validate_permeable_surface(float(v), resolved))

    if (v := _get("native_plant_pct")) is not None:
        results.append(validate_native_plant_pct(float(v), resolved))

    if (v := _get("water_balance_retention_pct")) is not None:
        results.append(validate_water_balance_retention(float(v), resolved))

    if (v := _get("tss_removal_pct")) is not None:
        results.append(validate_tss_removal(float(v), resolved))

    if (v := _get("potable_water_reduction_pct")) is not None:
        results.append(validate_potable_water_reduction(float(v), resolved))

    if (v := _get("cool_paving_pct")) is not None:
        results.append(validate_cool_paving(float(v), resolved))

    # Amenity
    if (v := _get("amenity_total_m2_per_unit")) is not None:
        results.append(validate_amenity_total(float(v), units, resolved))

    if (v := _get("amenity_indoor_min_m2_per_unit")) is not None:
        results.append(validate_amenity_indoor(float(v), units, resolved))

    # Garden suite
    if (v := _get("gs_gfa_m2")) is not None:
        results.append(validate_gs_gfa(float(v), resolved))

    if (v := _get("gs_max_height_m")) is not None:
        results.append(validate_gs_height(float(v), resolved))

    if (v := _get("gs_rear_setback_m")) is not None:
        results.append(validate_gs_rear_setback(float(v), resolved))

    return results


def validate_against_snapshot(
    proposed: dict,
    snapshot: "ZoningSnapshot",
    *,
    near_transit: bool = False,
    ward: Optional[str] = None,
) -> list[ValidationResult]:
    """Run setback/FSI/depth checks using snapshot values ONLY.

    Constructs a minimal synthetic ResolvedZoning from the ZoningSnapshot fields
    and runs the standard validators against it — never re-resolves from zone_symbol.
    This guarantees the validator uses the same parameter values as the generator.

    Only checks parameters stored in the snapshot; broader checks (parking,
    landscaping, etc.) are skipped since those values are not in the snapshot.
    """
    from .zoning_resolver import ResolvedZoning, _make_param

    def _sp(key: str, value, unit: str, citation: str, label: str):
        return _make_param(
            key, value, unit=unit, source="postGIS",
            citation=citation,
            editable_basic=True, editable_advanced=True,
            label=f"{label} (snapshot)",
            description="Value from ZoningSnapshot audit trail — not re-resolved.",
            category="building_envelope",
        )

    snap_params: dict = {}
    snap_params["front_yard_setback_m"] = _sp(
        "front_yard_setback_m", snapshot.front_setback_m, "m",
        "§10.20.40.70(1) [RD], §10.5.40.70(1) [contextual]", "Front yard")
    snap_params["rear_yard_setback_m"] = _sp(
        "rear_yard_setback_m", snapshot.rear_setback_m, "m",
        "§10.20.40.70(2) [RD], §10.10.40.70 [R]", "Rear yard")
    snap_params["side_yard_setback_left_m"] = _sp(
        "side_yard_setback_left_m", snapshot.left_setback_m, "m",
        "§10.20.40.70(3) [RD], §10.10.40.70(3) [R]", "Side yard (left)")
    snap_params["side_yard_setback_right_m"] = _sp(
        "side_yard_setback_right_m", snapshot.right_setback_m, "m",
        "§10.20.40.70(3) [RD], §10.10.40.70(3) [R]", "Side yard (right)")
    if snapshot.height_max_m is not None:
        snap_params["building_height_max_m"] = _sp(
            "building_height_max_m", snapshot.height_max_m, "m",
            "§10.20.40.10", "Max building height")
    snap_params["building_depth_m"] = _sp(
        "building_depth_m", snapshot.building_depth_max_m, "m",
        "§10.20.40.30", "Max building depth")
    snap_params["fsi_max"] = _sp(
        "fsi_max", snapshot.fsi, "FSI",
        "§10.20.40.40 / By-law 66-2024", "Max FSI")
    snap_params["multiplex_fsi_exempt"] = _sp(
        "multiplex_fsi_exempt", snapshot.fsi_exempt, "bool",
        "§10.20.40.40(1)(C) / By-law 474-2023", "Multiplex FSI exempt")
    if snapshot.max_coverage_pct is not None:
        snap_params["lot_coverage_pct_max"] = _sp(
            "lot_coverage_pct_max", snapshot.max_coverage_pct, "%",
            "Lot Coverage Overlay Map", "Max lot coverage")

    zone_code = snapshot.zone_symbol.split()[0].split("(")[0].strip().upper()
    synthetic = ResolvedZoning(
        zone_code=zone_code,
        zone_label_full=snapshot.zone_symbol,
        params=snap_params,
        amendment_flags=[],
        warnings=list(snapshot.warnings),
        categories=[],
    )

    results: list[ValidationResult] = []
    p = proposed

    if (v := p.get("front_yard_setback_m")) is not None:
        results.append(validate_front_yard(float(v), synthetic))
    if (v := p.get("rear_yard_setback_m")) is not None:
        results.append(validate_rear_yard(float(v), synthetic))
    left = p.get("side_yard_setback_left_m")
    right = p.get("side_yard_setback_right_m")
    if left is not None or right is not None:
        results.extend(validate_side_yard(
            float(left) if left is not None else 99.0,
            float(right) if right is not None else 99.0,
            synthetic,
        ))
    if (v := p.get("building_height_max_m")) is not None and "building_height_max_m" in snap_params:
        results.append(validate_building_height(float(v), synthetic))
    if (v := p.get("building_depth_m")) is not None:
        results.append(validate_building_depth(float(v), synthetic))
    if (v := p.get("fsi_max")) is not None:
        units = int(p.get("dwelling_unit_count") or 1)
        results.append(validate_fsi(float(v), units, synthetic, ward=ward))
    if (v := p.get("lot_coverage_pct_max")) is not None and "lot_coverage_pct_max" in snap_params:
        results.append(validate_lot_coverage(float(v), synthetic))

    return results


def summarize(results: list[ValidationResult]) -> dict:
    """Return {ok, variance, violation, exempt, na} counts."""
    counts: dict[str, int] = {"ok": 0, "variance": 0, "violation": 0, "exempt": 0, "na": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def build_compliance_rows(
    resolved: ResolvedZoning,
    proposed: Optional[dict] = None,
    validation_results: Optional[list] = None,
) -> list[dict]:
    """Build a flat list of compliance rows for the PDF report table.

    Each row: {parameter, proposed, limit, unit, status, citation, amendment}.
    Used by ``pdf_writer._s7_compliance_audit()``.

    ``validation_results`` is the output of ``validate_all()`` — used to merge
    compliance status into each row.  If omitted, status defaults to 'na'.
    """
    proposed = proposed or {}

    # Build param_key → status lookup from validation results
    status_map: dict[str, str] = {}
    if validation_results:
        for vr in validation_results:
            if vr.param_key:
                status_map[vr.param_key] = vr.status

    rows: list[dict] = []

    AUDITABLE_KEYS = [
        # Building envelope
        "front_yard_setback_m", "rear_yard_setback_m",
        "side_yard_setback_left_m", "side_yard_setback_right_m",
        "building_height_max_m", "building_depth_m",
        "main_wall_height_side_m", "main_wall_height_frontrear_m",
        "main_wall_height_flat_roof_m", "parapet_extension_max_m",
        "step_back_above_storey_n_m",
        # Density
        "dwelling_unit_count", "fsi_max", "lot_coverage_pct_max",
        "multiplex_fsi_exempt",
        # Parking
        "parking_spaces_residents", "parking_spaces_visitor",
        "bicycle_long_term", "driveway_width_m",
        # Landscape
        "front_yard_landscaping_pct", "front_yard_soft_landscaping_pct",
        "rear_yard_soft_landscaping_pct", "permeable_surface_pct",
        "ravine_top_of_bank_setback_m",
        # Projections
        "eaves_max_into_side_m", "bay_window_projection_max_m",
        "rear_deck_ground_encroachment_m", "rear_deck_upper_encroachment_m",
        "front_porch_encroachment_m",
        # Sustainability
        "tgs_tier", "native_plant_pct", "water_balance_retention_pct",
        "tss_removal_pct", "potable_water_reduction_pct", "cool_paving_pct",
        # Special provisions
        "secondary_suite", "garden_suite", "laneway_suite",
        "gs_gfa_m2", "gs_max_height_m", "gs_rear_setback_m",
    ]

    for key in AUDITABLE_KEYS:
        rp = resolved.params.get(key)
        if rp is None:
            continue
        proposed_val = proposed.get(key, rp.value)
        rows.append({
            "parameter": rp.label,
            "proposed":  proposed_val,
            "limit":     rp.value,
            "unit":      rp.unit,
            "citation":  rp.citation,
            "amendment": rp.amendment_flag or "",
            "status":    status_map.get(key, "na"),
        })

    return rows
