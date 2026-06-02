"""Fill a TypologyTemplate with rooms based on an architect brief.

Pipeline:
  1. Auto-fill circulation zones (stair, corridor) — never sent to LLM.
  2. Build LLM prompt from non-circulation template zones + brief.
  3. Call GPT-4.1 with JSON-only response_format.
  4. Parse and validate assignments (zone_id must exist, role must be valid).
  5. Merge LLM assignments with auto-filled circulation.
  6. Deterministically compute Cell coordinates within each zone.
  7. Return Cell tuple ready for fit_stamp.

The LLM never outputs coordinates — only room→zone assignments with weights.
Geometry is computed deterministically from weights + zone boundaries.
"""
from __future__ import annotations

import json
from dataclasses import replace as _dc_replace
from typing import Optional

from .typology.models import Cell, Typology, TypologyTemplate, TemplateZone
from .rules.code_rules import (
    ROOM_MIN_AREA_M2 as _OBC_MIN_AREA,
    ROOM_MIN_DIM_M as _MIN_DIM,
    ROOM_MAX_AREA_M2 as _MAX_ROOM_AREA,
    normalize_role as _normalize_role,
)

# Target areas for stamp-path cell sizing (between OBC min and max)
_ROOM_TARGET_AREA_M2: dict[str, float] = {
    "bedroom":        10.5,
    "master_bedroom": 14.0,
    "living":         20.0,
    "dining":         11.0,
    "kitchen":        10.0,
    "bathroom":        5.5,
    "powder_room":     3.5,
    "laundry":         4.0,
    "entry":           4.5,
    "corridor":        3.5,
    "mechanical":      4.0,
    "storage":         4.5,
    "balcony":         7.0,
    "stair":           5.0,
    "void":            0.0,
    "garage":         17.0,
}


class TemplateFillError(Exception):
    """Raised when the LLM output cannot be validated against the template."""


def _make_brief_cell(
    *,
    role: str,
    uid: int,
    storey: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> Cell:
    """Construct a Cell for a brief room appended by the reconciler."""
    return Cell(
        role=role,
        unit_id=uid,
        storey=storey,
        x0=round(x0, 6),
        y0=round(y0, 6),
        x1=round(x1, 6),
        y1=round(y1, 6),
        min_area_m2=_OBC_MIN_AREA.get(role, 0.0),
        min_dim_m=_MIN_DIM.get(role, 0.0),
        needs_egress_window=role in ("bedroom", "master_bedroom"),
        is_stretchable=True,
    )


def _reconcile_with_brief(
    cells: tuple[Cell, ...],
    brief: dict,
    template: TypologyTemplate,
    env_w: float,
    env_d: float,
) -> tuple[Cell, ...]:
    """Safety net: add any brief rooms not produced by the LLM assignment.

    Compares placed room counts against the brief and appends missing rooms
    into the most suitable template zone.
    """
    from collections import Counter
    placed_counts: Counter = Counter(
        (c.role, c.unit_id)
        for c in cells
        if c.role not in ("corridor", "stair", "void", "entry")
    )

    cells_list = list(cells)

    for unit in brief.get("units", []):
        uid = unit.get("unit_id", 1) - 1
        for room in unit.get("rooms", []):
            role = _normalize_role(str(room.get("role", "")))
            need = room.get("count", 1)
            have = placed_counts.get((role, uid), 0)
            missing = need - have
            if missing <= 0:
                continue

            sp = room.get("storey_preference", 0)
            candidate_zones = sorted(
                [z for z in template.zones if not z.is_circulation],
                key=lambda z: (abs(z.storey - sp), z.storey)
            )
            if not candidate_zones:
                continue

            zone = candidate_zones[0]
            target_a = _ROOM_TARGET_AREA_M2.get(role, _OBC_MIN_AREA.get(role, 5.0))
            cross_m = (
                (zone.y1 - zone.y0) * env_d if zone.subdivision_axis == "x"
                else (zone.x1 - zone.x0) * env_w
            )
            if cross_m < 0.05:
                cross_m = 1.0
            span_m = target_a / cross_m

            for _ in range(missing):
                if zone.subdivision_axis == "x":
                    span_norm = min(span_m / env_w, zone.x1 - zone.x0)
                    cells_list.append(_make_brief_cell(
                        role=role, uid=uid, storey=zone.storey,
                        x0=zone.x0, y0=zone.y0,
                        x1=round(zone.x0 + span_norm, 6), y1=zone.y1,
                    ))
                else:
                    span_norm = min(span_m / env_d, zone.y1 - zone.y0)
                    cells_list.append(_make_brief_cell(
                        role=role, uid=uid, storey=zone.storey,
                        x0=zone.x0, y0=zone.y0,
                        x1=zone.x1, y1=round(zone.y0 + span_norm, 6),
                    ))
                placed_counts[(role, uid)] += 1

    return tuple(cells_list)


def fill_template(
    typology: Typology,
    brief: dict,                  # RoomBriefModel.model_dump()
    envelope_w_m: float,
    envelope_d_m: float,
    *,
    openai_client=None,
    fallback_to_stamp: bool = True,
    units_target: int = 1,
    target_floors: "Optional[int]" = None,
    target_stacking: "Optional[str]" = None,
) -> tuple[Cell, ...]:
    """Return Cell tuple in normalized [0,1]² space, ready for fit_stamp.

    target_floors:   exact storey count the user specified in the Parameter Tweaker.
    target_stacking: 'vertical' | 'horizontal' — how units must be arranged.

    If LLM fails and fallback_to_stamp=True, silently returns typology.stamp_cells.
    If fallback_to_stamp=False and LLM fails, raises TemplateFillError.
    """
    def _capped_stamp() -> tuple[Cell, ...]:
        capped = _cap_room_areas(list(typology.stamp_cells), envelope_w_m, envelope_d_m)
        capped = _enforce_min_dims(capped, envelope_w_m, envelope_d_m)
        # Run reconciler even on stamp fallback so brief rooms are never silently dropped
        if typology.has_template():
            capped = list(_reconcile_with_brief(
                tuple(capped), brief, typology.template, envelope_w_m, envelope_d_m
            ))
        return tuple(capped)

    if not typology.has_template():
        return _capped_stamp()

    template: TypologyTemplate = typology.template

    # Auto-fill circulation zones so LLM only handles habitable rooms
    circ_assignments = _auto_fill_circulation(template)

    try:
        user_assignments = _call_llm_for_assignments(
            template, brief, envelope_w_m, envelope_d_m, openai_client,
            units_target=units_target,
            target_floors=target_floors,
            target_stacking=target_stacking,
        )
    except Exception as exc:
        if fallback_to_stamp:
            return _capped_stamp()
        raise TemplateFillError(f"LLM call failed: {exc}") from exc

    # Validate LLM-generated assignments against the template
    try:
        _validate_assignments(user_assignments, template)
    except TemplateFillError:
        if fallback_to_stamp:
            return _capped_stamp()
        raise

    # Merge: LLM + auto-filled circulation
    combined = {
        "assignments": user_assignments.get("assignments", []) + circ_assignments,
        "warnings": user_assignments.get("warnings", []),
    }

    # Fill any zones the LLM left empty to prevent floor-plate voids
    combined = _fill_empty_zones(combined, template)

    cells = _materialize_cells(combined, template, envelope_w_m, envelope_d_m)
    cells = _reconcile_with_brief(cells, brief, template, envelope_w_m, envelope_d_m)
    return cells


# ---------------------------------------------------------------------------
# Auto-fill circulation zones
# ---------------------------------------------------------------------------

def _auto_fill_circulation(template: TypologyTemplate) -> list[dict]:
    result = []
    for z in template.zones:
        if z.is_circulation and z.valid_roles:
            result.append({
                "zone_id": z.zone_id,
                "rooms": [{
                    "role": z.valid_roles[0],
                    "unit_id": -1,  # shared/common
                    "subdivision_index": 0,
                    "weight": 1.0,
                }],
            })
    return result


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm_for_assignments(
    template: TypologyTemplate,
    brief: dict,
    env_w: float,
    env_d: float,
    client,
    *,
    units_target: int = 1,
    target_floors: "Optional[int]" = None,
    target_stacking: "Optional[str]" = None,
) -> dict:
    """Call GPT-4.1 to assign rooms to template zones.

    Returns dict: {"assignments": [...], "warnings": [...]}
    """
    if client is None:
        from openai import OpenAI
        client = OpenAI()

    system_prompt = _build_system_prompt(target_floors=target_floors, target_stacking=target_stacking)
    user_prompt = _build_user_prompt(
        template, brief, env_w, env_d,
        units_target=units_target,
        target_floors=target_floors,
        target_stacking=target_stacking,
    )

    resp = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=2000,
        timeout=20.0,
    )
    raw = resp.choices[0].message.content or "{}"
    return json.loads(raw)


def _build_system_prompt(
    target_floors: "Optional[int]" = None,
    target_stacking: "Optional[str]" = None,
) -> str:
    # Build a hard-constraint block that the LLM MUST follow
    hard_rules: list[str] = []
    if target_floors:
        storey_range = {1: "storey=0 only", 2: "storeys 0 and 1",
                        3: "storeys -1, 0, and 1", 4: "storeys -1, 0, 1, and 2"}
        storeys_desc = storey_range.get(target_floors, f"{target_floors} storeys")
        hard_rules.append(
            f"FLOOR COUNT (non-negotiable): The building has EXACTLY {target_floors} storey(s) "
            f"({storeys_desc}). Do NOT assign rooms to zones outside this storey range. "
            f"Simply omit those zones from your assignments — leave them out entirely."
        )
    if target_stacking == "vertical":
        hard_rules.append(
            "STACKING (non-negotiable): Units are stacked VERTICALLY — unit A is on the lower "
            "floor(s), unit B on the upper floor(s). Do NOT place two units side-by-side on the "
            "same floor. Each unit occupies a distinct set of storeys."
        )
    elif target_stacking == "horizontal":
        hard_rules.append(
            "STACKING (non-negotiable): Units are arranged HORIZONTALLY side-by-side on the same "
            "floor level. Do NOT stack units vertically. Each unit occupies the same set of storeys."
        )
    hard_block = (
        "\n\nHARD CONSTRAINTS — these override all other rules:\n"
        + "\n".join(f"  • {r}" for r in hard_rules)
        + "\n"
    ) if hard_rules else ""

    return (
        "You are assigning rooms to template zones for a Toronto residential\n"
        "floor plan (Toronto By-law 569-2013). The architect has provided a brief;\n"
        "the typology has been selected; your job is to decide which rooms go in\n"
        "which zones.\n\n"
        "You DO NOT output coordinates. You DO NOT change zone boundaries.\n"
        "You DO NOT invent new zones. You only assign rooms (with role,\n"
        "unit_id, and a weight indicating relative size within the zone) to\n"
        "existing non-circulation zones."
        + hard_block + "\n"
        "Rules you must follow:\n"
        "1. Every habitable room from the brief must be placed in exactly one zone.\n"
        "2. A room's role must appear in that zone's valid_roles list.\n"
        "3. If a zone has max_subdivisions=N, at most N rooms can be assigned to it.\n"
        "4. The sum of weights within a single zone must equal 1.0 (split the\n"
        "   zone proportionally).\n"
        "5. Only place bedrooms in zones with rear_private or front_public in the\n"
        "   zone_id — these face the exterior and satisfy §9.7 egress requirements.\n"
        "6. Each dwelling unit must have at least: 1 living room, 1 kitchen,\n"
        "   1 bathroom, and 1 bedroom (§9.3).\n"
        "7. If the brief requests more rooms than the template can hold,\n"
        "   add a warning to the warnings array — do not silently omit rooms.\n"
        "8. unit_id in your response is 0-indexed: brief Unit 1 → unit_id=0,\n"
        "   brief Unit 2 → unit_id=1, etc.\n"
        "9. CRITICAL — STOREY PREFERENCE: each room in the brief has a\n"
        "   'storey_preference' field you MUST respect:\n"
        "     storey_preference=0  → assign to a zone with storey=0 (ground floor).\n"
        "     storey_preference=1  → assign to a zone with storey≥1 (upper floor).\n"
        "     storey_preference=-1 → assign to a zone with storey=-1 (basement).\n"
        "   If the only suitable zone for a role is on a different storey than\n"
        "   requested, use it but add a warning. Never silently ignore preferences.\n"
        "10. ROOM SIZING — The geometry engine enforces target areas automatically.\n"
        "    Your weights are PROPORTIONAL HINTS only — NOT final sizes.\n"
        "    - One room per zone: set weight=1.0 (engine trims to target area).\n"
        "    - Multiple rooms per zone: set weights proportional to relative sizes.\n"
        "      Example: bedroom (10m²) + bathroom (5m²) → weights 0.67 and 0.33.\n"
        "    - Weights MUST sum to 1.0 within each zone assignment.\n"
        "    - Do NOT try to make rooms fill their zone — the engine handles this.\n"
        "    - Do NOT add storage or corridor as weight-fillers.\n"
        "11. OBC MINIMUM DIMENSIONS (§9.8.3.2): check BEFORE placing a room.\n"
        "    Compute: zone_width=(x1-x0)*building_width, zone_depth=(y1-y0)*building_depth.\n"
        "    If zone is narrower than the room's minimum, choose a different zone.\n"
        "    Minimums: bedroom ≥2.1m both axes; master_bedroom ≥2.7m; kitchen ≥1.8m;\n"
        "    bathroom ≥1.5m. For shared zones, each room's allocated slice must meet\n"
        "    its minimum (weight × zone_dim ≥ min_dim).\n\n"
        "12. BRIEF FIDELITY — STRICT: only rooms explicitly in the brief may appear.\n"
        "    - DO NOT add storage, void, or any filler rooms not in the brief.\n"
        "    - If a zone cannot hold any brief room (wrong role, storey, or already\n"
        "      full), OMIT that zone from your assignments entirely — no placeholder.\n"
        "    - If brief rooms exceed zone capacity, add a warning to 'warnings'.\n\n"
        "subdivision_index is 0-indexed within the zone (first room=0, second=1…).\n\n"
        "Return ONLY valid JSON in this exact shape:\n"
        '{\n'
        '  "assignments": [\n'
        '    {\n'
        '      "zone_id": "...",\n'
        '      "rooms": [\n'
        '        {"role": "...", "unit_id": 0, "subdivision_index": 0, "weight": 1.0}\n'
        '      ]\n'
        '    }\n'
        '  ],\n'
        '  "warnings": []\n'
        '}\n'
    )


def _build_user_prompt(
    template: TypologyTemplate,
    brief: dict,
    env_w: float,
    env_d: float,
    *,
    units_target: int = 1,
    target_floors: "Optional[int]" = None,
    target_stacking: "Optional[str]" = None,
) -> str:
    # Exclude circulation zones — those are auto-filled; LLM only handles habitable
    zones_desc = [
        {
            "zone_id": z.zone_id,
            "storey": z.storey,
            "valid_roles": list(z.valid_roles),
            "max_subdivisions": z.max_subdivisions,
            "subdivision_axis": z.subdivision_axis,
            "actual_area_m2": round((z.x1 - z.x0) * env_w * (z.y1 - z.y0) * env_d, 1),
            "notes": z.notes or None,
        }
        for z in template.zones
        if not z.is_circulation
    ]

    if units_target == 1:
        # Single dwelling: all rooms belong to the same unit (unit_id=0), zones on both floors
        unit_note = (
            "\nSINGLE-UNIT BRIEF: The brief describes ONE dwelling unit that spans both floors. "
            "Assign ALL habitable rooms from the brief to unit_id=0. "
            "Zones on storey=1 are still part of the same unit_id=0 (e.g. upper bedrooms/bathrooms). "
            "Do NOT invent a second unit (unit_id=1 with habitable rooms). "
            "If a zone has no matching brief room, OMIT it from assignments entirely.\n"
        )
    else:
        # Multi-unit: the brief describes ONE unit's room program; replicate across all units
        unit_note = (
            f"\nMULTI-UNIT BRIEF: The brief describes the room program for ONE dwelling unit. "
            f"The building has {units_target} units total (unit_id 0 through {units_target-1}). "
            f"Replicate the brief's rooms for EACH unit in the template:\n"
            f"  - Assign bedrooms, living, kitchen, bathrooms to every unit_id 0..{units_target-1}.\n"
            f"  - Use the storey_preference values to place rooms on the correct floor for each unit.\n"
            f"  - Each unit is a self-contained dwelling with its own living/kitchen/bathroom/bedroom.\n"
            f"  - If a zone has no matching brief room, OMIT it from your assignments entirely.\n"
        )

    # Build a hard-constraint reminder at the top of the user message
    # so the LLM sees it immediately before the zone list.
    hard_lines: list[str] = []
    if target_floors:
        storey_map = {1: "storey=0 only", 2: "storeys 0 and 1",
                      3: "storeys -1, 0, and 1", 4: "storeys -1, 0, 1, and 2"}
        storeys_desc = storey_map.get(target_floors, f"{target_floors} storeys")
        hard_lines.append(
            f"FLOOR COUNT = {target_floors} ({storeys_desc}). "
            f"Assign NOTHING to zones outside these storeys. "
            f"Simply omit zones on other storeys — do not add placeholders."
        )
    if target_stacking == "vertical":
        hard_lines.append(
            "STACKING = VERTICAL. Each unit occupies a distinct set of floors stacked on top of "
            "each other. Do NOT place two units side by side on the same floor."
        )
    elif target_stacking == "horizontal":
        hard_lines.append(
            "STACKING = HORIZONTAL. All units sit side-by-side on the same floor level. "
            "Do NOT stack units vertically across different floors."
        )
    hard_reminder = (
        "\n⚠ HARD CONSTRAINTS (non-negotiable — apply before all other rules):\n"
        + "\n".join(f"  • {l}" for l in hard_lines)
        + "\n"
    ) if hard_lines else ""

    # Build storey preference summary from brief
    storey_prefs = []
    for unit in brief.get("units", []):
        uid_out = unit.get("unit_id", 1) - 1
        for room in unit.get("rooms", []):
            sp = room.get("storey_preference", 0)
            pref_word = {-1: "basement only", 0: "ground floor", 1: "upper floor"}.get(
                sp, f"storey {sp}"
            )
            storey_prefs.append(
                f"  unit_id={uid_out} {room.get('role','?')}×{room.get('count',1)}: {pref_word}"
            )

    floor_placement_block = (
        "\n\nFLOOR PLACEMENT — architect's explicit preferences (MUST be respected):\n"
        + ("\n".join(storey_prefs) if storey_prefs else "  (none specified)")
        + "\nOnly assign a room to a zone whose storey matches its preference above.\n"
        + "If the preferred storey has no suitable zone, use the closest available storey.\n"
    )

    return (
        f"Building: {env_w:.1f}m wide × {env_d:.1f}m deep.\n"
        f"{hard_reminder}"
        f"{unit_note}\n"
        f"Assignable template zones (circulation zones are auto-filled):\n"
        f"{json.dumps(zones_desc, indent=2)}\n\n"
        f"Structural rules: {json.dumps(template.structural_rules)}\n\n"
        f"Architect brief (0-indexed unit_id; unit 1 in brief = unit_id 0 in output):\n"
        f"{json.dumps(brief, indent=2)}"
        f"{floor_placement_block}"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_assignments(assignments: dict, template: TypologyTemplate) -> None:
    """Raise TemplateFillError if any assignment violates the template."""
    zones_by_id = {z.zone_id: z for z in template.zones}

    for a in assignments.get("assignments", []):
        zid = a.get("zone_id")
        if zid not in zones_by_id:
            raise TemplateFillError(f"Unknown zone_id: {zid!r}")

        zone = zones_by_id[zid]
        rooms = a.get("rooms", [])

        if len(rooms) > zone.max_subdivisions:
            # Clamp instead of raising — keeps the plan alive
            rooms = rooms[:zone.max_subdivisions]
            a["rooms"] = rooms

        total_weight = 0.0
        for r in rooms:
            role = _normalize_role(str(r.get("role", "")))
            # storage/void are universal flex roles accepted in any zone as remainder filler
            if role not in ("storage", "void") and role not in zone.valid_roles:
                raise TemplateFillError(
                    f"Role {role!r} is not valid for zone {zid!r} "
                    f"(valid: {zone.valid_roles})"
                )
            total_weight += float(r.get("weight", 0))

        if rooms and abs(total_weight - 1.0) > 0.05:
            # Re-normalize instead of raising — keeps the plan alive
            total = sum(float(r.get("weight", 0)) for r in rooms)
            if total > 0:
                for r in rooms:
                    r["weight"] = float(r.get("weight", 0)) / total

    # Each unit must have minimum required rooms.
    units_seen = _collect_unit_rooms(assignments)
    all_non_shared = [u for u in units_seen if u != -1]
    for uid, roles in units_seen.items():
        if uid == -1:
            continue  # shared/circulation — no dwelling requirements
        # For single-unit layouts (unit_id=0 spanning both floors) OR when the LLM assigns
        # only 1 unique unit_id, skip the per-unit dwelling check — the full room set may
        # be spread across storey 0 and storey 1 and validation happens via brief completeness.
        if len(all_non_shared) == 1:
            continue
        required = {"living", "kitchen", "bathroom"}
        has_bedroom = "bedroom" in roles or "master_bedroom" in roles
        missing = required - roles
        if missing or not has_bedroom:
            raise TemplateFillError(
                f"Unit {uid} is missing required rooms: "
                f"{missing}"
                + ("" if has_bedroom else " and has no bedroom")
            )


def _collect_unit_rooms(assignments: dict) -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for a in assignments.get("assignments", []):
        for r in a.get("rooms", []):
            uid = int(r.get("unit_id", 0))
            role = _normalize_role(str(r.get("role", "")))
            result.setdefault(uid, set()).add(role)
    return result


# ---------------------------------------------------------------------------
# Empty-zone filler (prevents floor-plate voids)
# ---------------------------------------------------------------------------

def _fill_empty_zones(combined: dict, template: TypologyTemplate) -> dict:
    """Fill unassigned non-circulation zones with corridor cells.

    Zones the LLM left unassigned become circulation/passage space rather than
    phantom storage.  Corridor is architecturally honest (it IS leftover floor
    plate that would typically become hallway or landing); storage is not —
    it was producing 30-50 m² mystery storage rooms that confused the output.

    Zones that are genuinely small (< 2 m² at max buildable dims) are skipped
    entirely to avoid hairline cells in the DXF.
    """
    assigned = {a["zone_id"] for a in combined.get("assignments", [])}
    extras = []
    for zone in template.zones:
        if zone.is_circulation or zone.zone_id in assigned:
            continue
        extras.append({
            "zone_id": zone.zone_id,
            "rooms": [{"role": "corridor", "unit_id": -1,
                       "subdivision_index": 0, "weight": 1.0}],
        })
    if not extras:
        return combined
    return {
        "assignments": combined.get("assignments", []) + extras,
        "warnings": combined.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# Room-area capper (prevents oversized rooms)
# ---------------------------------------------------------------------------

def _enforce_min_dims(cells: list[Cell], env_w: float, env_d: float) -> list[Cell]:
    """Attempt to fix cells that violate OBC minimum dimensions.

    Strategy (in order):
    1. Try to expand the under-sized dimension by absorbing adjacent corridor/void
       cells on the same storey that share an edge.
    2. If expansion is not possible, keep the cell at its original role and
       flag it — the OBC checker downstream will report the violation explicitly.

    We no longer silently downgrade habitable rooms to 'storage': that was hiding
    constraint violations behind phantom storage cells, producing plans that had
    zero OBC violations on paper but 40m² of unexplained storage in practice.
    """
    # Build a quick lookup: storey → list of (index, cell)
    by_storey: dict[int, list[tuple[int, Cell]]] = {}
    for i, c in enumerate(cells):
        by_storey.setdefault(c.storey, []).append((i, c))

    result = list(cells)

    for i, c in enumerate(cells):
        min_d = _MIN_DIM.get(c.role, 0.0)
        if min_d == 0.0:
            continue
        actual_w = (result[i].x1 - result[i].x0) * env_w
        actual_d = (result[i].y1 - result[i].y0) * env_d
        if actual_w >= min_d and actual_d >= min_d:
            continue  # already compliant

        tol = 1e-4
        expanded = False

        # Try to absorb an adjacent corridor/void cell on the same storey
        for j, nc in by_storey.get(c.storey, []):
            if j == i or nc.role not in ("corridor", "void", "storage"):
                continue
            ci = result[i]
            nj = result[j]
            # Check x-adjacency (ci is to the left of nj, or right)
            if abs(ci.x1 - nj.x0) < tol and abs(ci.y0 - nj.y0) < tol and abs(ci.y1 - nj.y1) < tol:
                # Extend ci rightward to absorb nj
                result[i] = _dc_replace(ci, x1=nj.x1)
                result[j] = _dc_replace(nj, x0=nj.x1)  # collapse neighbour to zero width
                expanded = True
                break
            if abs(nj.x1 - ci.x0) < tol and abs(ci.y0 - nj.y0) < tol and abs(ci.y1 - nj.y1) < tol:
                # Extend ci leftward
                result[i] = _dc_replace(ci, x0=nj.x0)
                result[j] = _dc_replace(nj, x1=nj.x0)
                expanded = True
                break
            # Check y-adjacency
            if abs(ci.y1 - nj.y0) < tol and abs(ci.x0 - nj.x0) < tol and abs(ci.x1 - nj.x1) < tol:
                result[i] = _dc_replace(ci, y1=nj.y1)
                result[j] = _dc_replace(nj, y0=nj.y1)
                expanded = True
                break
            if abs(nj.y1 - ci.y0) < tol and abs(ci.x0 - nj.x0) < tol and abs(ci.x1 - nj.x1) < tol:
                result[i] = _dc_replace(ci, y0=nj.y0)
                result[j] = _dc_replace(nj, y1=nj.y0)
                expanded = True
                break

        # Whether or not expansion succeeded, keep the cell at its original role.
        # The OBC checker will report violations; they won't be hidden as storage.

    # Filter out cells that were collapsed to zero size during absorption
    return [c for c in result if (c.x1 - c.x0) > 1e-4 and (c.y1 - c.y0) > 1e-4]


def _replace_cell(c: Cell, **kwargs) -> Cell:
    """Return a copy of Cell c with the given fields overridden."""
    return _dc_replace(c, **kwargs)


def _cap_room_areas(cells: list[Cell], env_w: float, env_d: float) -> list[Cell]:
    """Trim cells whose actual area exceeds _MAX_ROOM_AREA for their role.

    When a habitable room is too large, TRIM it (shorten the longer axis to
    reach 105% of max area) and convert the remainder to corridor space.
    This prevents _cap_room_areas from doubling room counts.

    Non-habitable roles (stair, corridor, entry, etc.) are never trimmed.
    """
    result: list[Cell] = []
    for c in cells:
        max_a = _MAX_ROOM_AREA.get(c.role)
        if max_a is None:
            result.append(c)
            continue
        actual_w = (c.x1 - c.x0) * env_w
        actual_d = (c.y1 - c.y0) * env_d
        actual_a = actual_w * actual_d
        if actual_a <= max_a * 1.15:
            result.append(c)
            continue
        # Trim along longer real-metre axis; add corridor for excess
        trim_factor = (max_a * 1.05) / actual_a
        if actual_w >= actual_d:
            new_x1 = c.x0 + (c.x1 - c.x0) * trim_factor
            # Keep trimmed cell
            result.append(_replace_cell(c, x1=round(new_x1, 6)))
            # Corridor for remainder
            corr_w = (c.x1 - new_x1) * env_w
            corr_d = actual_d
            if corr_w >= 0.5 and corr_d >= 0.5:
                result.append(_replace_cell(c, role="corridor", unit_id=-1,
                                            x0=round(new_x1, 6), x1=c.x1,
                                            is_stretchable=False))
        else:
            new_y1 = c.y0 + (c.y1 - c.y0) * trim_factor
            result.append(_replace_cell(c, y1=round(new_y1, 6)))
            corr_w = actual_w
            corr_d = (c.y1 - new_y1) * env_d
            if corr_w >= 0.5 and corr_d >= 0.5:
                result.append(_replace_cell(c, role="corridor", unit_id=-1,
                                            y0=round(new_y1, 6), y1=c.y1,
                                            is_stretchable=False))
    return result


# ---------------------------------------------------------------------------
# Deterministic coordinate materialization
# ---------------------------------------------------------------------------

def _make_cell(
    *,
    role: str,
    unit_id: int,
    storey: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    is_stretchable: bool = True,
) -> Cell:
    """Construct a Cell with OBC defaults for the given role."""
    return Cell(
        role=role,
        unit_id=unit_id,
        storey=storey,
        x0=round(x0, 6),
        y0=round(y0, 6),
        x1=round(x1, 6),
        y1=round(y1, 6),
        min_area_m2=_OBC_MIN_AREA.get(role, 0.0),
        min_dim_m=_MIN_DIM.get(role, 0.0),
        needs_egress_window=role in ("bedroom", "master_bedroom"),
        is_stretchable=is_stretchable,
    )


def _materialize_cells(
    assignments: dict,
    template: TypologyTemplate,
    env_w: float,
    env_d: float,
) -> tuple[Cell, ...]:
    """Compute Cell coordinates from validated assignments using target-area sizing.

    Instead of filling each room proportionally to its LLM-provided weight
    (which caused bathrooms to span entire zones), each room is sized to its
    target area from _ROOM_TARGET_AREA_M2. Weights from the LLM are used only
    as proportional hints when total target spans would overflow.
    """
    zones_by_id = {z.zone_id: z for z in template.zones}
    cells: list[Cell] = []

    for a in assignments.get("assignments", []):
        zone_id = a.get("zone_id")
        zone = zones_by_id.get(zone_id)
        if zone is None:
            continue
        rooms = sorted(a.get("rooms", []), key=lambda r: r.get("subdivision_index", 0))
        if not rooms:
            continue

        axis = zone.subdivision_axis  # "x" or "y"
        if axis == "x":
            zone_axis_m  = (zone.x1 - zone.x0) * env_w
            zone_cross_m = (zone.y1 - zone.y0) * env_d
        else:
            zone_axis_m  = (zone.y1 - zone.y0) * env_d
            zone_cross_m = (zone.x1 - zone.x0) * env_w

        if zone_cross_m < 0.05:
            zone_cross_m = 1.0  # safe fallback

        # Compute target span per room based on target area
        for r in rooms:
            role = _normalize_role(str(r.get("role", "")))
            target_a = _ROOM_TARGET_AREA_M2.get(role, _OBC_MIN_AREA.get(role, 5.0))
            # Clamp between OBC min and OBC max
            target_a = max(_OBC_MIN_AREA.get(role, 0.0),
                           min(target_a, _MAX_ROOM_AREA.get(role, target_a * 2)))
            r["_tspan"] = target_a / zone_cross_m

        total_target = sum(r["_tspan"] for r in rooms)

        if total_target >= zone_axis_m * 0.95:
            scale = (zone_axis_m * 0.95) / total_target
            for r in rooms:
                r["_tspan"] *= scale
            remainder_m = zone_axis_m * 0.05
        else:
            remainder_m = zone_axis_m - total_target

        for r in rooms:
            r["weight"] = r["_tspan"] / zone_axis_m

        # Materialize
        if axis == "x":
            offset = zone.x0
            for r in rooms:
                span = (zone.x1 - zone.x0) * r["weight"]
                role = _normalize_role(str(r.get("role", "")))
                cells.append(_make_cell(
                    role=role,
                    unit_id=int(r.get("unit_id", 0)),
                    storey=zone.storey,
                    x0=offset, y0=zone.y0,
                    x1=offset + span, y1=zone.y1,
                ))
                offset += span
            if remainder_m > 0.5:
                cells.append(_make_cell(
                    role="corridor", unit_id=-1, storey=zone.storey,
                    x0=offset, y0=zone.y0,
                    x1=zone.x1, y1=zone.y1,
                    is_stretchable=False,
                ))
        else:
            offset = zone.y0
            for r in rooms:
                span = (zone.y1 - zone.y0) * r["weight"]
                role = _normalize_role(str(r.get("role", "")))
                cells.append(_make_cell(
                    role=role,
                    unit_id=int(r.get("unit_id", 0)),
                    storey=zone.storey,
                    x0=zone.x0, y0=offset,
                    x1=zone.x1, y1=offset + span,
                ))
                offset += span
            if remainder_m > 0.5:
                cells.append(_make_cell(
                    role="corridor", unit_id=-1, storey=zone.storey,
                    x0=zone.x0, y0=offset,
                    x1=zone.x1, y1=zone.y1,
                    is_stretchable=False,
                ))

    cells = _cap_room_areas(cells, env_w, env_d)
    cells = _enforce_min_dims(cells, env_w, env_d)
    return tuple(cells)
