"""Deterministic space-program generator.

Given a DesignBrief and a buildable EnvelopeResult, produce a SpaceProgram
(rooms with target areas, NOT positions).  No LLM calls, no randomness.

Algorithm (constraint propagation):
  1. GFA budget = buildable footprint area × target_floors × _EFFICIENCY.
  2. Expand brief rooms into a flat list of (role, unit_id, storey) instances.
     Auto-add one shared stair if the building has more than one floor and
     the brief omits it.
  3. Assign target areas: scale each role's typical area proportionally to fit
     GFA, clamp to [ROOM_MIN_AREA_M2, ROOM_MAX_AREA_M2], then redistribute
     any remaining budget slack to rooms below their typical target.
  4. Stamp zone_class, wet, exterior_required, min_clear_dim per role.
  5. Return (SpaceProgram, warnings).  Warnings follow the wording used by the
     router feasibility pre-check so downstream code can surface the same text.
"""
from __future__ import annotations

from typing import Literal, Optional

from ..geometry import EnvelopeResult
from ..rules.code_rules import (
    PARKING_AISLE_WIDTH_M,
    PARKING_SPACE_LENGTH_M,
    PARKING_SPACE_WIDTH_M,
    ROOM_MAX_AREA_M2,
    ROOM_MIN_AREA_M2,
    ROOM_MIN_DIM_M,
    is_fsi_exempt,
    normalize_role,
)
from ..schemas.contracts import DesignBrief, ProgramRoom, SpaceProgram


# ---------------------------------------------------------------------------
# Storey resolution helper
# ---------------------------------------------------------------------------

def _resolve_storey(pref: int, target_floors: int) -> int:
    """Map BriefRoomSpec.storey_preference to a valid storey index.

    Rules:
      -1  → basement (always valid)
      ≥ target_floors → clamped to highest available storey (target_floors - 1)
      otherwise → returned as-is
    """
    if pref == -1:
        return -1           # basement
    if pref >= target_floors:
        return target_floors - 1   # clamp to highest available storey
    return pref

# ---------------------------------------------------------------------------
# Module-level constants (all thresholds from code_rules; none hardcoded here)
# ---------------------------------------------------------------------------

_EFFICIENCY: float = 0.82          # net-to-gross ratio matching the router pre-check
_INFEASIBLE_THRESHOLD: float = 1.25  # warn when required > budget × this factor

# Typical target area per role (m²).  Used as the proportional weight for GFA
# distribution.  Values are architecturally representative mid-range areas,
# always within [ROOM_MIN_AREA_M2, ROOM_MAX_AREA_M2].
_ROLE_TYPICAL_M2: dict[str, float] = {
    "bedroom":        11.0,
    "master_bedroom": 15.0,
    "living":         20.0,
    "dining":         12.0,
    "kitchen":        10.0,
    "bathroom":        5.0,
    "powder_room":     2.5,
    "laundry":         3.5,
    "stair":           4.5,
    "corridor":        4.0,
    "entry":           3.5,
    "mechanical":      3.5,
    "storage":         4.0,
    "balcony":         7.0,
    "void":            0.0,
}

_ZONE_CLASS: dict[str, Literal["public", "private", "service", "circulation"]] = {
    "bedroom":        "private",
    "master_bedroom": "private",
    "living":         "public",
    "dining":         "public",
    "kitchen":        "service",
    "bathroom":       "private",
    "powder_room":    "private",
    "laundry":        "service",
    "stair":          "circulation",
    "corridor":       "circulation",
    "entry":          "service",
    "mechanical":     "service",
    "storage":        "service",
    "balcony":        "private",
    "void":           "circulation",
    "garage":         "service",
}

_WET_ROLES: frozenset[str] = frozenset({"kitchen", "bathroom", "powder_room", "laundry"})

# Rooms that must touch an exterior wall (egress window / natural light)
_EXTERIOR_ROLES: frozenset[str] = frozenset({"bedroom", "master_bedroom", "living", "dining", "balcony"})

# Minimum furnishable rectangle (width_m, depth_m) per role
_FURNITURE_BOX: dict[str, tuple[float, float]] = {
    "bedroom":        (2.0, 3.0),   # single bed + clearance; OBC §9.8.3.2
    "master_bedroom": (2.5, 3.5),   # queen bed + clearance
    "living":         (3.0, 3.5),   # sofa + coffee table
    "dining":         (2.0, 2.4),   # 4-person table + chairs
    "kitchen":        (2.0, 2.5),   # galley work triangle
    "bathroom":       (1.5, 2.0),   # WC + vanity + bath/shower
    "powder_room":    (0.9, 1.5),   # WC + sink
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_space_program(
    brief: DesignBrief,
    envelope: EnvelopeResult,
    target_floors: Optional[int],
    fsi: Optional[float] = None,
    building_type: Optional[str] = None,
    parking_count: int = 0,
    max_height_m: Optional[float] = None,
    ward: int | str | None = None,
) -> tuple[SpaceProgram, list[str]]:
    """Generate a SpaceProgram from a DesignBrief and buildable envelope.

    fsi:
        Floor Space Index from the zoning resolver (d-value or overlay). When
        provided, the GFA budget is capped at fsi × lot_area unless the building
        is FSI-exempt under By-law 474-2023 (2–4 units). If None, the envelope
        budget is used without an FSI cap (backward-compatible).
    building_type:
        Optional label ("duplex", "fourplex", …) — used to confirm FSI exemption
        even when units_count alone is ambiguous.
    max_height_m:
        Resolved height limit (m). Used to derive target_floors when the caller
        does not supply it. Assumption: 2.85 m/floor (standard residential).

    Returns:
        (SpaceProgram, warnings)  — warnings is empty on a fully compliant run.
    """
    warnings: list[str] = []

    # ── 0. Derive storey count when not explicitly provided ───────────────────
    _FLOOR_TO_FLOOR_M = 2.85  # standard residential floor-to-floor height
    if target_floors is None:
        if max_height_m is not None:
            target_floors = max(1, int(max_height_m / _FLOOR_TO_FLOOR_M))
            warnings.append(
                f"Storey count derived from height limit ({max_height_m:.1f} m ÷ "
                f"{_FLOOR_TO_FLOOR_M} m/floor = {target_floors} storeys). "
                "Override with target_floors in the Parameter Tweaker if your "
                "floor-to-floor height differs."
            )
        else:
            target_floors = 2
            warnings.append(
                "No storey count or height limit provided; defaulted to 2 storeys. "
                "Set target_floors in the Parameter Tweaker to override."
            )

    # ── 1. GFA budget ────────────────────────────────────────────────────────
    footprint_area: float = envelope.envelope_2d.area
    gfa_budget: float = footprint_area * target_floors * _EFFICIENCY

    # Apply FSI density cap when FSI is explicitly provided
    if fsi is not None:
        units_count = len(brief.units)
        if is_fsi_exempt(units_count, building_type, ward=ward):
            warnings.append(
                f"FSI not binding for this {units_count}-unit building (By-law 474-2023 "
                "§10.20.40.40(C)); envelope controlled by height, setbacks, and building "
                "length/depth."
            )
        else:
            lot_area_m2 = envelope.lot_area_m2
            gfa_cap = fsi * lot_area_m2
            if gfa_budget > gfa_cap:
                warnings.append(
                    f"Brief exceeds maximum FSI of {fsi:.2f} (§10.20.40.40). "
                    f"FSI cap = {gfa_cap:.0f} m² ({fsi:.2f} × {lot_area_m2:.0f} m² lot). "
                    "Reduce storey count or room areas."
                )
            gfa_budget = min(gfa_budget, gfa_cap)

    # ── 2. Expand brief into flat room list ──────────────────────────────────
    # Each entry: (role, unit_id_0indexed, storey, caller_min_area_m2)
    _RawRoom = tuple[str, int, int, float]
    raw: list[_RawRoom] = []

    for unit in brief.units:
        uid0 = unit.unit_id - 1  # BriefUnit uses 1-indexed; ProgramRoom uses 0-indexed
        for spec in unit.rooms:
            for _ in range(spec.count):
                # Warn when storey_preference is out of range (clamped by _resolve_storey)
                if spec.storey_preference >= 1 and target_floors == 1:
                    warnings.append(
                        f"storey_preference={spec.storey_preference} requested but building "
                        f"has only 1 floor; placing on ground floor."
                    )
                storey = _resolve_storey(spec.storey_preference, target_floors)
                raw.append((normalize_role(spec.role), uid0, storey, spec.min_area_m2))

    # Auto-add stair(s) when the building is multi-storey and the brief omits it.
    # Horizontal townhouses: one stair per unit (each unit is self-contained).
    # Vertical / mixed stacking: one shared stair (unit_id = -1) for the whole building.
    has_stair = any(normalize_role(r[0]) == "stair" for r in raw)
    if not has_stair and target_floors > 1:
        if brief.stacking_pref == "horizontal":
            # One stair per unit in a horizontal townhouse
            for unit in brief.units:
                uid0 = unit.unit_id - 1
                raw.append(("stair", uid0, 0, 0.0))
        else:
            # One shared stair for vertical / mixed stacking
            raw.append(("stair", -1, 0, 0.0))

    # ── 3. Feasibility pre-check (mirrors router wording) ────────────────────
    n_units = len(brief.units)
    total_min = sum(_obc_floor(role, caller_min) for role, _, _, caller_min in raw)
    if total_min > gfa_budget * _INFEASIBLE_THRESHOLD:
        min_per_unit = total_min / max(n_units, 1)
        warnings.append(
            f"Brief requires ~{total_min:.0f} m² minimum "
            f"({n_units} unit{'s' if n_units > 1 else ''} × {min_per_unit:.0f} m²/unit) but the "
            f"{target_floors}-storey buildable envelope provides only "
            f"~{gfa_budget:.0f} m². Rooms will be smaller than target. "
            "Consider fewer bedrooms per unit or fewer total units."
        )

    # ── 4. Compute and distribute target areas ───────────────────────────────
    target_areas = _distribute_areas(raw, gfa_budget)

    # ── 5. Build ProgramRoom instances ───────────────────────────────────────
    rooms: list[ProgramRoom] = []
    counters: dict[str, int] = {}

    for (role, uid0, storey, _caller_min), target in zip(raw, target_areas):
        uid_key = "s" if uid0 < 0 else str(uid0)
        seq_key = f"{role}_{uid_key}"
        seq = counters.get(seq_key, 0)
        counters[seq_key] = seq + 1

        # Keep ids within the 40-char Field limit; role names are max 14 chars
        room_id = f"{role}_{uid_key}_{seq}"

        rooms.append(ProgramRoom(
            id=room_id,
            role=role,          # type: ignore[arg-type]  # role is always a valid Literal
            unit_id=uid0,
            storey=storey,
            target_area_m2=target,
            zone_class=_ZONE_CLASS.get(role, "private"),
            wet=(role in _WET_ROLES),
            exterior_required=(role in _EXTERIOR_ROLES),
            furniture_box_m=_FURNITURE_BOX.get(role),
            # min_clear_dim_m is filled by ProgramRoom's own model_validator
        ))

    # ── 6. Parking footprint (non-GFA reservation) ───────────────────────────
    if parking_count > 0:
        # Stalls + one aisle run (§200.5.10.30); area is outside the GFA budget.
        parking_area = round(
            parking_count * PARKING_SPACE_WIDTH_M * PARKING_SPACE_LENGTH_M
            + PARKING_AISLE_WIDTH_M * PARKING_SPACE_WIDTH_M,
            2,
        )
        rooms.append(ProgramRoom(
            id="garage_s_0",
            role="garage",  # type: ignore[arg-type]
            unit_id=-1,    # shared facility
            storey=0,
            target_area_m2=parking_area,
            zone_class="service",
            wet=False,
            exterior_required=True,
            furniture_box_m=None,
        ))

    return SpaceProgram(rooms=rooms), warnings


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _obc_floor(role: str, caller_min: float) -> float:
    """Effective OBC floor area for a role, respecting caller override."""
    return max(ROOM_MIN_AREA_M2.get(role, 0.0), caller_min)


def _distribute_areas(
    raw: list[tuple[str, int, int, float]],
    gfa_budget: float,
) -> list[float]:
    """Return a target area (m²) for each raw room entry.

    Steps:
      a) Each room starts with its role's typical area.
      b) Scale all typicals proportionally so they sum to ≤ gfa_budget.
      c) Clamp each result to [OBC floor, ROOM_MAX_AREA_M2].
      d) Redistribute any remaining budget to rooms still below their typical.
    """
    n = len(raw)
    if n == 0:
        return []

    # Effective bounds for each room
    floors = [_obc_floor(role, caller_min) for role, _, _, caller_min in raw]
    ceilings = [
        ROOM_MAX_AREA_M2.get(role, floors[i] * 3 if floors[i] > 0 else 10.0)
        for i, (role, _, _, _) in enumerate(raw)
    ]
    # Replace inf with a generous practical ceiling so arithmetic works
    ceilings = [c if c != float("inf") else max(floors[i] * 4, 10.0) for i, c in enumerate(ceilings)]

    typicals = [
        max(floors[i], min(ceilings[i], _ROLE_TYPICAL_M2.get(role, floors[i])))
        for i, (role, _, _, _) in enumerate(raw)
    ]

    total_typical = sum(typicals)

    # Scale so total ≤ gfa_budget (never scale up — let slack redistribution handle that)
    if total_typical > 0:
        scale = min(gfa_budget / total_typical, 1.0)
    else:
        scale = 1.0

    areas = [t * scale for t in typicals]

    # Clamp to [floor, ceiling]
    areas = [max(floors[i], min(ceilings[i], areas[i])) for i in range(n)]

    # Redistribute remaining budget slack proportionally to rooms below their typical
    slack = gfa_budget - sum(areas)
    if slack > 0.01:
        below = [i for i in range(n) if areas[i] < typicals[i]]
        if below:
            below_weights = [typicals[i] for i in below]
            total_bw = sum(below_weights)
            for idx, i in enumerate(below):
                share = (below_weights[idx] / total_bw) * slack
                areas[i] = min(ceilings[i], areas[i] + share)

    # Final safety clamp: never go below OBC floor (can happen on very tight budgets)
    areas = [max(floors[i], areas[i]) for i in range(n)]

    return [round(a, 2) for a in areas]
