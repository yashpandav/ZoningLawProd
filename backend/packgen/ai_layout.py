"""
AI layout coordinate generation — ARCHIVED, NOT CALLED IN PRODUCTION PIPELINE.

As of 2026-05-23, the production PackGen pipeline uses stamp-based typology
selection (generate_pack_router._run_ai_layout → selector.fit_stamp) rather than
LLM coordinate generation. This module is preserved because:
  - check_feasibility() and the RoomBrief dataclasses may be used in future
    client-side feasibility pre-checks.
  - The coordinate generation approach may be revisited for non-standard lot shapes.

Do not call call_layout_ai() or build_ai_fit_result() in new code without first
updating them for the current pipeline.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Literal, Optional

from shapely.geometry import Polygon

from .typology.models import Cell, Typology
from .typology.selector import FitResult, PlacedCell
from .rules.code_rules import (
    VALID_ROLES as _VALID_ROLES,
    ROLE_ALIASES as _ROLE_ALIASES,
    ROOM_MIN_AREA_M2 as _OBC_MIN_AREA,
    EGRESS_ROLES as _EGRESS_ROLES,
    normalize_role as _normalize_role,
)


# ---------------------------------------------------------------------------
# Input dataclasses (user-facing brief)
# ---------------------------------------------------------------------------

@dataclass
class RoomSpec:
    role: str
    count: int = 1
    min_area_m2: float = 0.0
    storey_preference: int = 0   # preferred storey; 0=ground, 1=upper, -1=basement

@dataclass
class UnitBrief:
    unit_id: int          # 1-indexed (user-facing)
    rooms: list[RoomSpec] = field(default_factory=list)

@dataclass
class RoomBrief:
    units: list[UnitBrief] = field(default_factory=list)
    stack_preference: Literal["vertical", "horizontal"] = "vertical"
    notes: str = ""


# ---------------------------------------------------------------------------
# Feasibility result
# ---------------------------------------------------------------------------

@dataclass
class FeasibilityResult:
    feasible: bool
    estimated_storeys: int
    total_min_area_m2: float
    available_area_m2: float
    message: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AI output dataclasses (parsed from JSON response)
# ---------------------------------------------------------------------------

@dataclass
class AIRoom:
    role: str
    unit_id: int      # 0-indexed (matches Cell.unit_id convention)
    storey: int
    x0: float
    y0: float
    x1: float
    y1: float
    label: str = ""
    needs_egress_window: bool = False

@dataclass
class AIStoreyLayout:
    storey: int
    rooms: list[AIRoom] = field(default_factory=list)

@dataclass
class AILayoutSchedule:
    storeys: list[AIStoreyLayout] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Feasibility check
# ---------------------------------------------------------------------------

def check_feasibility(
    brief: RoomBrief,
    envelope_area_m2: float,
    envelope_w_m: float,
    envelope_d_m: float,
) -> FeasibilityResult:
    """Pre-AI feasibility gate. Rejects impossible briefs before the LLM call."""
    warnings: list[str] = []

    total_min = 0.0
    for unit in brief.units:
        for room in unit.rooms:
            role = _normalize_role(room.role)
            obc_min = _OBC_MIN_AREA.get(role, 2.0)
            room_min = max(room.min_area_m2, obc_min)
            total_min += room_min * room.count

        # Add stair allowance per unit (roughly 3.5m² per unit)
        has_stair = any(_normalize_role(r.role) == "stair" for r in unit.rooms)
        if not has_stair:
            total_min += 3.5

    # Add corridor budget (10% of total rooms)
    total_min *= 1.10

    # Estimate storeys needed
    footprint_area = envelope_w_m * envelope_d_m
    usable_per_storey = footprint_area * 0.85   # 85% efficiency
    if usable_per_storey < 1.0:
        return FeasibilityResult(
            feasible=False,
            estimated_storeys=0,
            total_min_area_m2=total_min,
            available_area_m2=0.0,
            message=f"Envelope too small: footprint {footprint_area:.1f}m².",
        )

    estimated_storeys = math.ceil(total_min / usable_per_storey)
    estimated_storeys = max(1, min(estimated_storeys, 4))  # cap at 4

    available = usable_per_storey * estimated_storeys

    if total_min > available * 1.15:  # 15% tolerance
        return FeasibilityResult(
            feasible=False,
            estimated_storeys=estimated_storeys,
            total_min_area_m2=round(total_min, 1),
            available_area_m2=round(available, 1),
            message=(
                f"Room brief requires at least {total_min:.0f}m² but the "
                f"{estimated_storeys}-storey envelope provides only {available:.0f}m². "
                "Reduce room count or units_target."
            ),
        )

    if envelope_w_m < 3.5:
        warnings.append(
            f"Envelope frontage {envelope_w_m:.1f}m is very narrow — "
            "some room arrangements may not fit."
        )

    return FeasibilityResult(
        feasible=True,
        estimated_storeys=estimated_storeys,
        total_min_area_m2=round(total_min, 1),
        available_area_m2=round(available, 1),
        message=f"Feasible: {estimated_storeys} storey(s), ~{available:.0f}m² available for {total_min:.0f}m² required.",
        warnings=warnings,
    )


# ===========================================================================
# ARCHIVED: LLM coordinate generation — not called in production
# ===========================================================================
# Everything below this line is superseded by stamp-based selection.
# Preserved for reference and potential future use. Do not call in new code.
# ===========================================================================

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_ai_prompt(
    brief: RoomBrief,
    envelope_w_m: float,
    envelope_d_m: float,
    zone_symbol: str,
    feasibility: FeasibilityResult,
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the layout AI call."""

    system_prompt = f"""You are an expert residential architect and floor plan generator.
Your task is to generate a room-by-room floor plan layout for a Toronto residential building,
strictly following Ontario Building Code (OBC) Part 9 and Toronto Zoning By-law 569-2013.

## Coordinate System
- Each storey uses a normalized 2D grid: x∈[0,1] (left=0, right=1) × y∈[0,1] (front/street=0, rear=1).
- The actual envelope is {envelope_w_m:.2f}m wide × {envelope_d_m:.2f}m deep.
- Rooms MUST NOT extend outside [0,1] on either axis.
- Rooms on the SAME storey MUST NOT overlap.

## Storey Numbers
- -1 = basement (below grade)
-  0 = ground floor
-  1 = second floor
-  2 = third floor
-  3 = fourth floor

## Valid Room Roles (use EXACTLY one of these strings):
bedroom, master_bedroom, living, dining, kitchen, bathroom, powder_room,
laundry, stair, corridor, entry, mechanical, storage, balcony, void

## OBC Minimum Areas (m²) — your layout must meet these:
- bedroom: 7.0m² (min dimension 2.1m) — needs_egress_window: true
- master_bedroom: 10.0m² (min dimension 2.7m) — needs_egress_window: true
- living: 13.5m²
- dining: 7.0m²
- kitchen: 4.5m²
- bathroom: 3.0m²
- powder_room: 1.8m²
- stair: min clear width 0.9m

## Rules
1. Each dwelling unit MUST have: ≥1 bedroom with egress window, kitchen, living area, ≥1 bathroom.
2. Each storey needs a stair cell (role="stair") unless it's a single-storey building.
3. Rooms must be rectangles (defined by x0,y0,x1,y1 with x0<x1, y0<y1).
4. Place habitable rooms (bedrooms, living) away from party walls where possible.
5. unit_id uses 0-based indexing: first unit = 0, second = 1, etc.
6. For shared elements (stairs, corridors), use unit_id = -1.

## Output JSON Schema
Return ONLY valid JSON matching this schema exactly:
{{
  "storeys": [
    {{
      "storey": <int>,
      "rooms": [
        {{
          "role": "<valid_role>",
          "unit_id": <int>,
          "x0": <float 0-1>,
          "y0": <float 0-1>,
          "x1": <float 0-1>,
          "y1": <float 0-1>,
          "label": "<human label>",
          "needs_egress_window": <bool>
        }}
      ]
    }}
  ],
  "warnings": ["<any layout notes or compromises>"]
}}
"""

    # Build room summary per unit
    unit_lines = []
    for unit in brief.units:
        uid_0 = unit.unit_id - 1  # convert to 0-indexed
        rooms_by_role: dict[str, int] = {}
        for r in unit.rooms:
            role = _normalize_role(r.role)
            rooms_by_role[role] = rooms_by_role.get(role, 0) + r.count
        room_list = ", ".join(f"{cnt}×{role}" for role, cnt in rooms_by_role.items())
        unit_lines.append(f"  Unit {uid_0} (0-indexed): {room_list}")

    units_description = "\n".join(unit_lines)
    stacking = brief.stack_preference
    notes_section = f"\nAdditional notes from client: {brief.notes}" if brief.notes.strip() else ""

    user_prompt = f"""Zone: {zone_symbol}
Envelope: {envelope_w_m:.2f}m wide × {envelope_d_m:.2f}m deep
Estimated storeys needed: {feasibility.estimated_storeys}
Stack preference: {stacking} ({"units stacked vertically, one above the other" if stacking == "vertical" else "units side by side"})

Units required:
{units_description}{notes_section}

Generate a complete floor plan layout. Place all rooms for all units across the {feasibility.estimated_storeys} storey(s).
Maximize natural light (bedrooms and living rooms near perimeter). Minimize corridor waste.
Stairs should be centrally accessible. Kitchens and bathrooms can share wet walls.

Return ONLY the JSON object — no explanation, no markdown."""

    return system_prompt, user_prompt


# ---------------------------------------------------------------------------
# AI call
# ---------------------------------------------------------------------------

def call_layout_ai(
    system_prompt: str,
    user_prompt: str,
    client,    # openai.OpenAI instance
    model: str = "gpt-4.1",
    timeout_secs: float = 45.0,
) -> AILayoutSchedule:
    """Call the layout AI and parse its JSON response into AILayoutSchedule."""
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=4096,
        timeout=timeout_secs,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI returned invalid JSON: {e}\nRaw: {raw[:500]}")

    storeys: list[AIStoreyLayout] = []
    for s_data in data.get("storeys", []):
        storey_num = int(s_data.get("storey", 0))
        rooms: list[AIRoom] = []
        for r in s_data.get("rooms", []):
            role = _normalize_role(str(r.get("role", "storage")))
            rooms.append(AIRoom(
                role=role,
                unit_id=int(r.get("unit_id", 0)),
                storey=storey_num,
                x0=float(r.get("x0", 0.0)),
                y0=float(r.get("y0", 0.0)),
                x1=float(r.get("x1", 1.0)),
                y1=float(r.get("y1", 1.0)),
                label=str(r.get("label", role.replace("_", " ").title())),
                needs_egress_window=bool(r.get("needs_egress_window", role in _EGRESS_ROLES)),
            ))
        if rooms:
            storeys.append(AIStoreyLayout(storey=storey_num, rooms=rooms))

    return AILayoutSchedule(
        storeys=storeys,
        warnings=list(data.get("warnings", [])),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_ai_schedule(
    schedule: AILayoutSchedule,
    brief: RoomBrief,
    envelope_w_m: float,
    envelope_d_m: float,
) -> list[str]:
    """Return a list of warning strings for layout issues. Does not raise."""
    warnings: list[str] = list(schedule.warnings)

    all_rooms = [r for s in schedule.storeys for r in s.rooms]

    # Check bounds
    for r in all_rooms:
        if r.x0 < -0.01 or r.x1 > 1.01 or r.y0 < -0.01 or r.y1 > 1.01:
            warnings.append(
                f"Room {r.role} (unit {r.unit_id}, storey {r.storey}) "
                f"extends outside [0,1]² bounds: ({r.x0:.2f},{r.y0:.2f})-({r.x1:.2f},{r.y1:.2f})"
            )
        if r.x1 <= r.x0 or r.y1 <= r.y0:
            warnings.append(
                f"Room {r.role} (unit {r.unit_id}, storey {r.storey}) has zero/negative area."
            )

    # Check each unit has required rooms
    for unit in brief.units:
        uid_0 = unit.unit_id - 1
        unit_rooms = [r for r in all_rooms if r.unit_id == uid_0]
        unit_roles = {r.role for r in unit_rooms}
        for required in ("bedroom", "kitchen", "living", "bathroom"):
            if required not in unit_roles and "master_bedroom" not in unit_roles:
                if required == "bedroom":
                    continue  # master_bedroom already checked
                if required not in unit_roles:
                    warnings.append(f"Unit {uid_0}: missing '{required}' room in AI layout.")

    # OBC area checks
    for r in all_rooms:
        obc_min = _OBC_MIN_AREA.get(r.role, 0.0)
        if obc_min > 0:
            w_m = (r.x1 - r.x0) * envelope_w_m
            d_m = (r.y1 - r.y0) * envelope_d_m
            area_m2 = w_m * d_m
            if area_m2 < obc_min * 0.90:  # 10% tolerance for rounding
                warnings.append(
                    f"Room {r.role} (unit {r.unit_id}, storey {r.storey}): "
                    f"area {area_m2:.1f}m² may be below OBC minimum {obc_min}m²."
                )

    return warnings


# ---------------------------------------------------------------------------
# Coordinate conversion: normalized [0,1]² → absolute PlacedCell metres
# ---------------------------------------------------------------------------

_SNAP_MM = 0.1  # 100mm snap grid


def _snap(v: float) -> float:
    return round(v / _SNAP_MM) * _SNAP_MM


def schedule_to_placed_cells(
    schedule: AILayoutSchedule,
    envelope_local: Polygon,
) -> list[PlacedCell]:
    """Convert AI's normalized rooms to absolute PlacedCell in local CAD frame."""
    minx, miny, maxx, maxy = envelope_local.bounds
    env_w = maxx - minx
    env_d = maxy - miny

    placed: list[PlacedCell] = []
    for storey_layout in schedule.storeys:
        for r in storey_layout.rooms:
            # Clamp to [0,1] before scaling
            rx0 = max(0.0, min(1.0, r.x0))
            ry0 = max(0.0, min(1.0, r.y0))
            rx1 = max(0.0, min(1.0, r.x1))
            ry1 = max(0.0, min(1.0, r.y1))

            if rx1 <= rx0 or ry1 <= ry0:
                continue  # skip degenerate rooms

            abs_x0 = _snap(minx + rx0 * env_w)
            abs_y0 = _snap(miny + ry0 * env_d)
            abs_x1 = _snap(minx + rx1 * env_w)
            abs_y1 = _snap(miny + ry1 * env_d)

            if abs_x1 <= abs_x0 or abs_y1 <= abs_y0:
                continue

            cell = Cell(
                role=r.role,  # type: ignore[arg-type]  # validated by _normalize_role
                unit_id=r.unit_id,
                storey=r.storey,
                x0=rx0,
                y0=ry0,
                x1=rx1,
                y1=ry1,
                min_area_m2=_OBC_MIN_AREA.get(r.role, 0.0),
                min_dim_m=0.0,
                needs_egress_window=r.needs_egress_window,
                is_stretchable=True,
            )
            placed.append(PlacedCell(cell=cell, x0=abs_x0, y0=abs_y0, x1=abs_x1, y1=abs_y1))

    return placed


# ---------------------------------------------------------------------------
# Build synthetic Typology and FitResult
# ---------------------------------------------------------------------------

def build_ai_fit_result(
    schedule: AILayoutSchedule,
    brief: RoomBrief,
    envelope_local: Polygon,
    zone_symbol: str,
    option: Literal["A", "B"] = "A",
    feasibility: Optional[FeasibilityResult] = None,
    ai_warnings: Optional[list[str]] = None,
) -> FitResult:
    """Assemble a FitResult from AI layout output. Fully compatible with downstream pipeline."""
    placed = schedule_to_placed_cells(schedule, envelope_local)

    minx, miny, maxx, maxy = envelope_local.bounds
    env_w = maxx - minx
    env_d = maxy - miny

    # Compute GFA (above-grade, non-balcony/void)
    gfa = sum(
        pc.area_m2
        for pc in placed
        if pc.cell.storey >= 0 and pc.cell.role not in ("balcony", "void")
    )

    # Count units
    units_set = {pc.cell.unit_id for pc in placed if pc.cell.unit_id >= 0}
    n_units = len(units_set) if units_set else len(brief.units)

    storeys_set = {pc.cell.storey for pc in placed}
    n_storeys = max(1, len({s for s in storeys_set if s >= 0}))
    has_basement = -1 in storeys_set

    # Build stamp_cells tuple (normalized [0,1]² cells for Typology metadata)
    stamp_cells = tuple(pc.cell for pc in placed)

    # Synthetic Typology (metadata only — not used for geometry by downstream)
    unit_labels = f"{n_units}-unit"
    typology = Typology(
        id="ai-generated",
        label=f"AI Layout — {unit_labels} ({n_storeys}-storey)",
        units_produced=n_units,
        stacking_axis="vertical" if brief.stack_preference == "vertical" else "horizontal",
        min_frontage_m=round(env_w * 0.8, 1),
        max_frontage_m=round(env_w, 1),
        min_depth_m=round(env_d * 0.8, 1),
        max_depth_m=round(env_d, 1),
        target_storeys=n_storeys,
        requires_basement=has_basement,
        target_gfa_per_unit_m2=(round(gfa / max(n_units, 1) * 0.8, 1),
                                round(gfa / max(n_units, 1) * 1.2, 1)),
        stamp_cells=stamp_cells,
        corridor_axis="central",
        stair_position="internal",
        eligible_zones=(zone_symbol or "R",),
        eligible_wards=None,
        notes=(
            f"AI-generated layout via GPT-4.1. "
            f"Brief: {', '.join(brief.notes.split()[:10]) if brief.notes else 'standard'}. "
            "Preliminary only — verify with licensed architect."
        ),
    )

    warnings: list[str] = list(ai_warnings or [])
    if feasibility and feasibility.warnings:
        warnings.extend(feasibility.warnings)

    return FitResult(
        typology=typology,
        placed_cells=placed,
        option=option,
        fit_frontage_m=round(env_w, 2),
        fit_depth_m=round(env_d, 2),
        scale_x=1.0,
        scale_y=1.0,
        origin_local_xy=(minx, miny),
        rotation_additional_deg=0.0,
        gfa_m2=round(gfa, 1),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

