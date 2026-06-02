"""FastAPI router for /api/generate-pack — behind ENABLE_PACKGEN flag.

Mount in app.py:
    import os
    if os.getenv("ENABLE_PACKGEN", "false").lower() == "true":
        from generate_pack_router import router as pack_router
        app.include_router(pack_router)
"""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from rate_limiter import limiter

from packgen.geometry import build_envelope
from packgen.packager import build_pack
from packgen.params import derive_params, resolved_to_envelope_params  # derive_params: DEPRECATED
from packgen.rules.code_rules import ROOM_MIN_AREA_M2 as _OBC_ROOM_MIN
from packgen.typology.library import TYPOLOGY_LIBRARY
from packgen.typology.selector import fit_stamp

router = APIRouter(prefix="/api", tags=["packgen"])

_PACKGEN_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="packgen")


# ---------------------------------------------------------------------------
# Room Brief Pydantic models (for AI layout mode)
# ---------------------------------------------------------------------------

class RoomSpecModel(BaseModel):
    role: str = Field(..., max_length=40)
    count: int = Field(default=1, ge=1, le=20)
    min_area_m2: float = Field(default=0.0, ge=0.0, le=200.0)
    storey_preference: int = Field(default=0, ge=-1, le=5)


class UnitBriefModel(BaseModel):
    unit_id: int = Field(..., ge=1, le=10)
    rooms: list[RoomSpecModel]


class RoomBriefModel(BaseModel):
    units: list[UnitBriefModel]
    stack_preference: str = Field(default="vertical", pattern="^(vertical|horizontal)$")
    notes: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class PackRequest(BaseModel):
    polygon_wkt: str = Field(
        ...,
        description="WKT of lot polygon in EPSG:4326 (from PostGIS parcel lookup).",
        max_length=500_000,   # Toronto lots can have highly-detailed MultiPolygon WKTs (>30KB)
    )
    zone_symbol: str = Field(..., max_length=40)
    exception_number: Optional[int] = None
    exception_constraints: Optional[dict] = None   # from /api/exception-constraints
    override: Optional[dict] = None                # manual user setback overrides
    units_target: int = Field(default=1, ge=1, le=6)
    layout_option: Optional[str] = Field(default=None, pattern="^[AB]$")
    ward: Optional[int] = None
    lot_frontage_m: Optional[float] = Field(default=None, ge=3.0, le=100.0)
    include_laneway: bool = False
    road_bearing_deg: Optional[float] = None
    address: Optional[str] = Field(default=None, max_length=200)
    typology_id: Optional[str] = Field(default=None, max_length=60)  # wizard pin
    room_brief: Optional[RoomBriefModel] = None    # if set → AI layout mode
    # Hard user constraints from the Parameter Tweaker — both are enforced strictly.
    # target_floors: exact number of storeys the building must have (1-10).
    # target_stacking: "vertical" | "horizontal" — how units are arranged.
    target_floors: Optional[int] = Field(default=None, ge=1, le=10)
    target_stacking: Optional[str] = Field(default=None, pattern="^(vertical|horizontal|mixed)$")
    # Per-parcel overlay data from the /api/parcel PostGIS response.
    # When provided these override zone-letter defaults so the generated envelope
    # and space program use the authoritative map values for this specific lot.
    overlay_height_m: Optional[float] = Field(default=None, ge=3.0, le=60.0)
    overlay_coverage_pct: Optional[float] = Field(default=None, ge=5.0, le=100.0)
    overlay_fsi: Optional[float] = Field(default=None, ge=0.1, le=20.0)
    # Abutting zone symbols for CR lots — triggers §40-series residential setback (7.5m).
    # Keys: "front" | "rear" | "left" | "right"; values: zone symbols, e.g. {"rear": "RD"}.
    abutting_zones: Optional[dict[str, str]] = Field(default=None)


_AI_STEPS = [
    "Projecting lot polygon to MTM-10…",
    "Computing setback envelope…",
    "Selecting typology…",
    "Filling template from brief (gpt-4.1)…",
    "Validating room assignments…",
    "Fitting stamp…",
    "Checking OBC compliance…",
    "Building DXF…",
    "Generating SVG previews…",
    "Exporting IFC4 massing…",
    "Writing PDF report…",
    "Packaging ZIP…",
]

# Step labels for the new deterministic solver path (ENABLE_SOLVER=true)
_SOLVER_STEPS = [
    "Projecting lot polygon to MTM-10…",
    "Computing setback envelope…",
    "Generating space program…",
    "Building adjacency graph…",
    "Solving stair core & wet columns…",
    "Laying out ground floor…",
    "Laying out upper floor(s)…",
    "Checking OBC compliance…",
    "Building DXF…",
    "Generating SVG previews…",
    "Exporting IFC4 massing…",
    "Writing PDF report…",
    "Packaging ZIP…",
]


# ---------------------------------------------------------------------------
# AI layout path — LLM selects a typology ID; geometry is deterministic
# ---------------------------------------------------------------------------

def _run_ai_layout(req: PackRequest, er, params, effective_floors: Optional[int] = None) -> list:
    """Select a typology (LLM or manual pin), fill it with rooms from the brief.

    Every typology now gets AI-driven room assignment when a room brief is
    provided. Typologies with an explicit TypologyTemplate use it directly;
    the remaining 11 get a generic template derived from their stamp geometry.
    Geometry is always deterministic — the LLM outputs room→zone assignments,
    not coordinates.
    """
    from dataclasses import replace as dc_replace
    from openai import OpenAI

    from packgen.template_filler import fill_template
    from packgen.typology.generic_template import stamp_to_generic_template

    rb = req.room_brief
    envelope_local = er.envelope_2d
    env_bounds = envelope_local.bounds
    env_w = env_bounds[2] - env_bounds[0]
    env_d = env_bounds[3] - env_bounds[1]
    # Use the brief's own unit count as the authority for the floor plan.
    # The Parameter Tweaker's units_target controls building envelope (setbacks, coverage)
    # but the architect's brief explicitly describes the actual program:
    #   - 1 unit in brief → single-family house
    #   - 2 units in brief → duplex
    #   - 4 units in brief → fourplex
    # This prevents the Parameter Tweaker's envelope slider from silently
    # replicating a single-family brief into an unwanted multi-unit building.
    units_target = len(rb.units) if rb.units else req.units_target

    # ── Brief feasibility pre-check ───────────────────────────────────────────
    # Estimate minimum area required by the brief × units_target and compare
    # against the available buildable footprint × storeys.  Catches impossible
    # briefs before the LLM wastes tokens on them.
    _brief_min_per_unit = sum(
        _OBC_ROOM_MIN.get(r.role, 3.0) * r.count
        for u in rb.units for r in u.rooms
    )
    _n_storeys = effective_floors or req.target_floors or 2
    _available_area = env_w * env_d * _n_storeys * 0.82  # 82% efficiency
    _required_area  = _brief_min_per_unit * units_target
    _feasibility_warnings: list[str] = []
    if _required_area > _available_area * 1.25:
        _feasibility_warnings.append(
            f"Brief requires ~{_required_area:.0f} m² minimum "
            f"({units_target} units × {_brief_min_per_unit:.0f} m²/unit) but the "
            f"{_n_storeys}-storey buildable envelope provides only "
            f"~{_available_area:.0f} m². Rooms will be smaller than target. "
            "Consider fewer bedrooms per unit or fewer total units."
        )

    def _build_dims(typ) -> tuple[float, float]:
        """Return (build_w_m, build_d_m) — actual fitted building size for typology.

        Replicates fit_stamp's clamping so fill_template uses the correct scale
        for area calculations.  Using raw env_w/env_d on a huge polygon lot would
        make _cap_room_areas compute wildly incorrect room sizes.
        """
        t_w = (typ.min_frontage_m + typ.max_frontage_m) / 2.0
        t_d = (typ.min_depth_m    + typ.max_depth_m)    / 2.0
        raw_sx = min(env_w, typ.max_frontage_m) / max(t_w, 0.1)
        raw_sy = min(env_d, typ.max_depth_m)    / max(t_d, 0.1)
        sx = min(max(raw_sx, 0.85), 1.15)
        sy = min(max(raw_sy, 0.85), 1.15)
        return t_w * sx, t_d * sy

    zone_base = req.zone_symbol.split("(")[0].rstrip()

    # ── Step 1: typology selection ────────────────────────────────────────────
    # If the Design Studio wizard pinned a typology, use it directly.
    typology = None
    if req.typology_id:
        typology = next((t for t in TYPOLOGY_LIBRARY if t.id == req.typology_id), None)

    if typology is None:
        # ── Build base candidate list (zone + envelope fit) ───────────────────
        all_candidates = [
            t for t in TYPOLOGY_LIBRARY
            if any(zone_base.startswith(ez) for ez in t.eligible_zones)
            and t.min_frontage_m <= env_w + 0.5
            and t.min_depth_m <= env_d + 0.5
        ]
        if not all_candidates:
            raise ValueError(
                f"No typology candidates for zone={req.zone_symbol}, "
                f"envelope={env_w:.1f}×{env_d:.1f}m."
            )

        # ── Hard-filter by user constraints BEFORE the LLM sees the list ─────
        # target_floors and target_stacking come from the Parameter Tweaker.
        # Apply them as strict pre-filters so the LLM cannot pick a wrong typology
        # regardless of how the prompt is phrased.
        #
        # Stacking filter: "vertical" → only vertical; "horizontal" → horizontal or mixed
        target_stacking = req.target_stacking or rb.stack_preference
        if target_stacking == "vertical":
            stk_filtered = [t for t in all_candidates if t.stacking_axis == "vertical"]
        elif target_stacking == "horizontal":
            stk_filtered = [t for t in all_candidates if t.stacking_axis in ("horizontal", "mixed")]
        else:
            stk_filtered = all_candidates
        candidates = stk_filtered or all_candidates   # graceful fallback

        # Floor-count filter: exact match on target_storeys
        if req.target_floors:
            fl_filtered = [t for t in candidates if t.target_storeys == req.target_floors]
            candidates = fl_filtered or candidates    # graceful fallback

        # ── Build hard-constraint block for the LLM ───────────────────────────
        hard_constraints: list[str] = []
        if req.target_floors:
            hard_constraints.append(
                f"FLOOR COUNT: The building MUST have exactly {req.target_floors} storey(s). "
                f"REJECT any typology whose target_storeys ≠ {req.target_floors}."
            )
        if target_stacking == "vertical":
            hard_constraints.append(
                "STACKING: Must be VERTICAL (units stacked on top of each other, NOT side-by-side). "
                "REJECT any typology with stacking='horizontal'."
            )
        elif target_stacking == "horizontal":
            hard_constraints.append(
                "STACKING: Must be HORIZONTAL or MIXED (units side-by-side, NOT stacked). "
                "REJECT any typology with stacking='vertical'."
            )
        if units_target:
            hard_constraints.append(
                f"UNIT COUNT: Prefer typology whose units_produced equals {units_target}. "
                "If no exact match exists, pick the closest."
            )

        constraint_block = (
            "\n\nHARD CONSTRAINTS — non-negotiable, override all other preferences:\n"
            + "\n".join(f"  • {c}" for c in hard_constraints)
        ) if hard_constraints else ""

        # ── Build candidate description for the LLM ───────────────────────────
        candidate_json = [
            {
                "id":            t.id,
                "label":         t.label,
                "units":         t.units_produced,
                "storeys":       t.target_storeys,
                "stacking":      t.stacking_axis,
                "has_basement":  t.requires_basement,
                "frontage_m":    f"{t.min_frontage_m}–{t.max_frontage_m}",
                "depth_m":       f"{t.min_depth_m}–{t.max_depth_m}",
                "gfa_per_unit":  f"{t.target_gfa_per_unit_m2[0]}–{t.target_gfa_per_unit_m2[1]}m²",
                "notes":         t.notes,
            }
            for t in candidates
        ]

        brief_desc = []
        for u in rb.units:
            rooms_txt = ", ".join(
                f"{r.count}×{r.role.replace('_',' ')} (floor {r.storey_preference})"
                for r in u.rooms
            )
            brief_desc.append(f"Unit {u.unit_id}: {rooms_txt}")

        # Summarise storey preferences to give the LLM context
        all_prefs = [r.storey_preference for u in rb.units for r in u.rooms]
        has_basement = any(p == -1 for p in all_prefs)
        all_ground   = all(p == 0 for p in all_prefs)
        storey_note  = (
            "All rooms on GROUND floor — prefer single-storey or ground-dominant typology."
            if all_ground else
            "Includes BASEMENT rooms — prefer typologies with basement units."
            if has_basement else
            f"Mixed floor preferences (floors: {sorted(set(all_prefs))})."
        )

        system_msg = (
            "You are a Toronto building code expert selecting a floor plan typology stamp. "
            "Return ONLY valid JSON with a single key 'typology_id' whose value is one of the "
            f"provided candidate ids.{constraint_block}"
        )
        user_msg = (
            f"Envelope: {env_w:.1f}m wide × {env_d:.1f}m deep\n"
            f"Zone: {req.zone_symbol}\n"
            f"Units requested: {units_target}\n"
            f"Required storeys: {req.target_floors or 'not specified'}\n"
            f"Required stacking: {target_stacking or 'not specified'}\n"
            f"Room brief:\n" + "\n".join(brief_desc) + "\n\n"
            f"Floor placement context: {storey_note}\n"
            f"Notes: {rb.notes or 'none'}\n\n"
            f"Available candidates (already filtered to match constraints):\n"
            f"{json.dumps(candidate_json, indent=2)}\n\n"
            "Select the typology_id that best matches ALL constraints above."
        )

        client = OpenAI()
        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                timeout=12.0,
            )
            raw = resp.choices[0].message.content or "{}"
            selected_id = json.loads(raw).get("typology_id", "")
        except Exception:
            selected_id = ""

        typology = next((t for t in candidates if t.id == selected_id), None)
        if typology is None:
            # Deterministic fallback: sort by constraint compliance then proximity
            def _constraint_score(t):
                stk_penalty  = 0 if (not target_stacking or t.stacking_axis == target_stacking
                                      or (target_stacking == "horizontal" and t.stacking_axis == "mixed")) else 3
                fl_penalty   = 0 if (not req.target_floors or t.target_storeys == req.target_floors) \
                                 else abs(t.target_storeys - req.target_floors)
                unit_penalty = abs(t.units_produced - units_target)
                front_delta  = abs((t.min_frontage_m + t.max_frontage_m) / 2 - env_w)
                return (stk_penalty, fl_penalty, unit_penalty, front_delta)
            typology = min(candidates, key=_constraint_score)

    # ── Step 2: ensure typology has a template ───────────────────────────────
    # Explicit templates (e.g. _DUPLEX_STACK_TEMPLATE) take priority.
    # All other typologies get a generic template derived from stamp geometry.
    if not typology.has_template():
        from packgen.rules.code_rules import normalize_role as _norm_role
        brief_rooms: dict[str, int] = {}
        for _u in rb.units:
            for _r in _u.rooms:
                _role = _norm_role(_r.role)
                brief_rooms[_role] = brief_rooms.get(_role, 0) + _r.count
        generic = stamp_to_generic_template(typology, brief_rooms=brief_rooms)
        typology = dc_replace(typology, template=generic)

    # ── Step 3: fill template from brief ─────────────────────────────────────
    build_w, build_d = _build_dims(typology)
    filled_cells = fill_template(
        typology=typology,
        brief=rb.model_dump(),
        envelope_w_m=build_w,
        envelope_d_m=build_d,
        fallback_to_stamp=True,
        units_target=units_target,
        target_floors=effective_floors or req.target_floors,
        target_stacking=req.target_stacking or rb.stack_preference,
    )
    filled_typology = dc_replace(typology, stamp_cells=filled_cells)
    fit = fit_stamp(filled_typology, envelope_local, option="A")
    if _feasibility_warnings:
        fit.warnings.extend(_feasibility_warnings)
    return [fit]


# ---------------------------------------------------------------------------
# Auto-brief helper — ensures AI layout always runs
# ---------------------------------------------------------------------------

def _auto_brief(req: PackRequest) -> PackRequest:
    """Return a copy of req with a minimal room brief generated from units_target.

    Called when no room_brief was supplied so that AI layout always runs —
    stamp selection is never used for generated plans.
    """
    units = []
    bedrooms = max(1, min(3, 4 - req.units_target))  # fewer bedrooms per unit as count grows
    for i in range(req.units_target):
        units.append(UnitBriefModel(
            unit_id=i + 1,
            rooms=[
                RoomSpecModel(role="bedroom",  count=bedrooms, min_area_m2=0, storey_preference=1),
                RoomSpecModel(role="living",   count=1,        min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="kitchen",  count=1,        min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="bathroom", count=1,        min_area_m2=0, storey_preference=0),
            ],
        ))
    return req.model_copy(update={
        'room_brief': RoomBriefModel(
            units=units,
            stack_preference="vertical",
            notes="Auto-generated brief — no room program was specified by user.",
        )
    })


# ---------------------------------------------------------------------------
# Sync heavy lifting (runs in executor)
# ---------------------------------------------------------------------------

def _brief_to_design_brief(rb: RoomBriefModel, target_stacking: Optional[str] = None):
    """Convert the router's RoomBriefModel to a pipeline DesignBrief."""
    from packgen.rules.code_rules import normalize_role
    from packgen.schemas.contracts import BriefRoomSpec, BriefUnit, DesignBrief

    units = []
    for u in rb.units:
        rooms = []
        for r in u.rooms:
            role = normalize_role(r.role)
            rooms.append(BriefRoomSpec(
                role=role,               # type: ignore[arg-type]
                count=r.count,
                min_area_m2=r.min_area_m2,
                storey_preference=r.storey_preference,
            ))
        units.append(BriefUnit(unit_id=u.unit_id, rooms=rooms))

    stacking = target_stacking or rb.stack_preference
    if stacking not in ("vertical", "horizontal", "mixed"):
        stacking = "vertical"

    return DesignBrief(units=units, stacking_pref=stacking, notes=rb.notes)


def _run_solver_layout(req: PackRequest, er, params, fsi: Optional[float] = None, effective_floors: Optional[int] = None, ward: int | str | None = None) -> list:
    """Run the new deterministic pipeline (ENABLE_SOLVER=true).

    Produces a FloorPlanJSON via the stage orchestrator, then converts it to
    two identical FitResult objects (A + B) so the existing build_pack/DXF/IFC
    writers consume it unchanged.
    """
    from packgen.ai.plan_to_geometry import floor_plan_to_fit_result
    from packgen.pipeline.orchestrator import generate_floor_plan

    if req.room_brief is None:
        req = _auto_brief(req)

    brief = _brief_to_design_brief(req.room_brief, req.target_stacking)
    target_floors = effective_floors or req.target_floors or 2

    plan = generate_floor_plan(
        brief=brief,
        envelope=er,
        target_floors=target_floors,
        target_stacking=req.target_stacking or "vertical",
        road_bearing_deg=req.road_bearing_deg,
        openai_client=None,    # deterministic — no LLM in this path
        fsi=fsi,
        max_height_m=params.max_height_m,
        ward=ward,
    )

    fit_a = floor_plan_to_fit_result(
        plan, er,
        typology_id="solver_generated",
        typology_label="Solver-generated plan",
        option="A",
    )
    fit_b = floor_plan_to_fit_result(
        plan, er,
        typology_id="solver_generated",
        typology_label="Solver-generated plan",
        option="B",
    )
    return [fit_a, fit_b]


def _run_pack(req: PackRequest) -> tuple[bytes, dict]:
    """Run the full geometry + typology/AI + DXF pipeline synchronously."""
    from packgen.zoning_resolver import resolve_zoning

    lot_data_dict = {
        "lot_frontage_m":  req.lot_frontage_m,
        "lot_depth_m":     None,   # not yet known; computed post-envelope
        "lot_area_m2":     None,
        "is_corner_lot":   False,
        "is_through_lot":  False,
        "has_lane_abuttal": req.include_laneway,
        "ward": req.ward,
        "abutting_zones":  req.abutting_zones,
        # Parking-rule inputs (Chapter 200)
        "units_count":     req.units_target,
        "near_transit":    False,   # VERIFY_FOR_LOT: set True when lot is ≤500m from rapid transit
    }
    # Per-parcel overlay values from PostGIS (authoritative for this specific lot)
    overlay_data = {
        "height_m":    req.overlay_height_m,
        "coverage_pct": req.overlay_coverage_pct,
        "overlay_fsi": req.overlay_fsi,
    }
    resolved = resolve_zoning(
        req.zone_symbol,
        lot_data=lot_data_dict,
        exception_constraints=req.exception_constraints,
        overlay_data=overlay_data,
    )
    # Extract resolved FSI for density enforcement in space_program.
    # Priority: overlay_fsi > zone suffix d-value > None (no cap).
    _fsi_param = resolved.params.get("fsi_max")
    resolved_fsi: Optional[float] = (
        float(_fsi_param.value)
        if _fsi_param is not None and _fsi_param.value is not None
        else None
    )

    params = resolved_to_envelope_params(
        resolved,
        lot_data_dict,
        req.override,
        req.units_target,
        req.layout_option,
        req.include_laneway,
    )

    # Derive effective storey count from building height when not explicitly set.
    # Standard residential floor-to-floor = 2.85 m; architects can override via
    # target_floors in the Parameter Tweaker (req.target_floors always wins).
    _FLOOR_TO_FLOOR_M = 2.85
    _max_h = params.max_height_m
    if req.target_floors is None and _max_h is not None:
        derived_floors: Optional[int] = max(1, int(_max_h / _FLOOR_TO_FLOOR_M))
    else:
        derived_floors = req.target_floors

    # Wide RD lots (frontage > 18m) require a side-yard step-back in the rear
    # portion of the envelope per §10.20.40.70(5).
    _apply_step_back = (
        resolved.zone_code == "RD"
        and (req.lot_frontage_m or 0.0) > 18.0
    )

    er = build_envelope(
        polygon_wkt_4326=req.polygon_wkt,
        front_setback_m=params.front_setback_m,
        rear_setback_m=params.rear_setback_m,
        left_setback_m=params.left_setback_m,
        right_setback_m=params.right_setback_m,
        lot_frontage_m=params.lot_frontage_m,
        zone_symbol=req.zone_symbol,
        max_coverage_pct=params.max_coverage_pct,
        include_laneway=params.include_laneway,
        road_bearing_deg=req.road_bearing_deg,
        apply_side_step_back=_apply_step_back,
    )

    # Route to deterministic solver or AI stamp pipeline based on ENABLE_SOLVER flag.
    # Default is now "true" — solver path is the primary path.
    # Set ENABLE_SOLVER=false in .env to revert to the stamp/AI path.
    use_solver = os.getenv("ENABLE_SOLVER", "true").lower() == "true"

    if use_solver:
        fits = _run_solver_layout(req, er, params, fsi=resolved_fsi, effective_floors=derived_floors, ward=req.ward)
        layout_mode = "solver"
    else:
        if req.room_brief is None:
            req = _auto_brief(req)
        fits = _run_ai_layout(req, er, params, effective_floors=derived_floors)
        layout_mode = "ai"

    # Build ZoningSnapshot — immutable record of every value used to generate this envelope.
    # Stored in the ZIP so planners and validators always use the same numbers.
    from datetime import datetime, timezone
    from packgen.rules.code_rules import is_fsi_exempt as _is_fsi_exempt
    from packgen.schemas.zoning_snapshot import ZoningSnapshot

    _fsi_param_snap = resolved.params.get("fsi_max")
    _fsi_src_str    = _fsi_param_snap.source if _fsi_param_snap else "code_default"
    _overlay_source = (
        "map_overlay"  if _fsi_src_str == "overlay"      else
        "zone_suffix"  if _fsi_src_str == "zone_suffix"  else
        "code_default"
    )
    _bd_param = resolved.params.get("building_depth_m")
    _bl_param = resolved.params.get("building_length_max_m")

    snapshot = ZoningSnapshot(
        zone_symbol           = req.zone_symbol,
        resolved_at           = datetime.now(timezone.utc).isoformat(),
        front_setback_m       = params.front_setback_m,
        rear_setback_m        = params.rear_setback_m,
        left_setback_m        = params.left_setback_m,
        right_setback_m       = params.right_setback_m,
        building_depth_max_m  = float(_bd_param.value) if _bd_param and _bd_param.value else 19.0,
        building_length_max_m = float(_bl_param.value) if _bl_param and _bl_param.value else None,
        fsi                   = resolved_fsi,
        fsi_exempt            = _is_fsi_exempt(req.units_target, zone_base=resolved.zone_code, ward=req.ward),
        max_coverage_pct      = params.max_coverage_pct,
        height_max_m          = params.max_height_m,
        overlay_source        = _overlay_source,
        warnings              = list(params.warnings) + list(resolved.warnings),
    )

    _brief_parts = []
    if req.room_brief is not None:
        for u in req.room_brief.units:
            rooms_txt = ", ".join(
                f"{r.count}×{r.role}" for r in u.rooms
            )
            _brief_parts.append(f"Unit {u.unit_id}: {rooms_txt}")
    extra = {
        "zone_symbol": req.zone_symbol,
        "exception_number": req.exception_number,
        "params_warnings": params.warnings,
        "layout_mode": layout_mode,
        "address": req.address or "",
        "brief_summary": ", ".join(_brief_parts),
        "zoning_snapshot": snapshot.model_dump(),
    }
    zip_bytes, svg_strings = build_pack(er, fits, extra_params=extra)
    meta = {
        "lot_width_m": round(er.lot_width_m, 2),
        "lot_depth_m": round(er.lot_depth_m, 2),
        "lot_area_m2": round(er.lot_area_m2, 1),
        "layout_mode": extra["layout_mode"],
        "options": [
            {
                "option": f.option,
                "typology": f.typology.label,
                "units": f.typology.units_produced,
                "gfa_m2": round(f.gfa_m2, 1),
                "fit_frontage_m": round(f.fit_frontage_m, 2),
                "fit_depth_m": round(f.fit_depth_m, 2),
                "warnings": f.warnings,
            }
            for f in fits
        ],
        "envelope_warnings": er.warnings,
        "params_warnings": params.warnings,
        "svg_a": svg_strings.get("a", ""),
        "svg_b": svg_strings.get("b", ""),
        "zip_filename": svg_strings.get("_zip_filename", "pack.zip"),
    }
    return zip_bytes, meta


# ---------------------------------------------------------------------------
# Download cache + background cleanup
# ---------------------------------------------------------------------------

_PACK_CACHE: dict[str, tuple[bytes, float]] = {}
_PACK_CACHE_TTL = 300        # 5-minute TTL
_PACK_CACHE_MAX = 50         # hard ceiling; guards against DoS memory growth

_cleanup_task: "asyncio.Task | None" = None


async def _cleanup_pack_cache():
    """Periodically evict expired entries from _PACK_CACHE."""
    import time as _t
    while True:
        await asyncio.sleep(60)
        now = _t.time()
        expired = [k for k, v in list(_PACK_CACHE.items())
                   if now - v[1] > _PACK_CACHE_TTL]
        for k in expired:
            _PACK_CACHE.pop(k, None)
        if expired:
            print(f"[pack_cache] evicted {len(expired)} expired entries, "
                  f"{len(_PACK_CACHE)} remaining")


# ---------------------------------------------------------------------------
# SSE progress streaming endpoint
# ---------------------------------------------------------------------------

async def _gen_progress(req: PackRequest):
    """Yield SSE progress events then the download URL."""
    global _cleanup_task

    # Start background cleanup the first time any generation is requested.
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_pack_cache())

    # Hard ceiling: reject if too many un-downloaded ZIPs are queued.
    if len(_PACK_CACHE) >= _PACK_CACHE_MAX:
        _busy_msg = json.dumps({'type': 'error', 'message': 'Server busy — too many pending downloads. Please try again in a few minutes.'})
        yield f"data: {_busy_msg}\n\n"
        return

    async def _run():
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_PACKGEN_EXECUTOR, _run_pack, req)

    use_solver = os.getenv("ENABLE_SOLVER", "true").lower() == "true"
    steps = _SOLVER_STEPS if use_solver else _AI_STEPS

    # Stream progress while the real task runs in background
    task = asyncio.ensure_future(_run())
    for step in steps:
        if task.done():
            break
        yield f"data: {json.dumps({'type': 'progress', 'message': step})}\n\n"
        await asyncio.sleep(0.5)

    try:
        zip_bytes, meta = await task
    except ValueError as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        return
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'Internal error: {e}'})}\n\n"
        return

    # Store in a simple in-memory slot (short TTL)
    import hashlib
    import time as _time
    token = hashlib.sha256(f"{_time.time()}".encode()).hexdigest()[:16]
    zip_filename = meta.get("zip_filename", "pack.zip")
    _PACK_CACHE[token] = (zip_bytes, _time.time(), zip_filename)

    yield f"data: {json.dumps({'type': 'done', 'token': token, 'meta': meta})}\n\n"


@router.post("/generate-pack/stream")
@limiter.limit("5/hour")
async def generate_pack_stream(request: Request, req: PackRequest):
    """SSE endpoint. Client receives progress events then a download token."""
    return StreamingResponse(
        _gen_progress(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/generate-pack/download")
async def download_pack(token: str = Query(..., min_length=16, max_length=16)):
    """Download the generated ZIP by token (valid for 5 minutes). Can be fetched multiple times."""
    import time
    now = time.time()
    # Clean up expired entries
    expired = [k for k, v in _PACK_CACHE.items() if now - v[1] > _PACK_CACHE_TTL]
    for k in expired:
        _PACK_CACHE.pop(k, None)

    entry = _PACK_CACHE.get(token, None)  # get, not pop — allow multiple fetches
    if entry is None:
        raise HTTPException(status_code=404, detail="Pack not found or expired.")
    zip_bytes, created_at, *_rest = entry
    zip_filename = _rest[0] if _rest else "pack.zip"
    if now - created_at > _PACK_CACHE_TTL:
        _PACK_CACHE.pop(token, None)
        raise HTTPException(status_code=410, detail="Pack has expired. Please regenerate.")

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


@router.post("/generate-pack/direct")
async def generate_pack_direct(req: PackRequest):
    """Synchronous endpoint — returns ZIP immediately (≤60s timeout expected for AI mode)."""
    loop = asyncio.get_event_loop()
    try:
        zip_bytes, _ = await loop.run_in_executor(_PACKGEN_EXECUTOR, _run_pack, req)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="floor_plan_pack.zip"'},
    )


# ---------------------------------------------------------------------------
# Free-text brief parser
# ---------------------------------------------------------------------------

class ParseBriefRequest(BaseModel):
    text: str = Field(..., max_length=2000)
    units_target: int = Field(default=1, ge=1, le=6)


class ParseBriefResponse(BaseModel):
    room_brief: RoomBriefModel
    confidence: float
    unrecognized_terms: list[str]


@router.post("/parse-brief", response_model=ParseBriefResponse)
@limiter.limit("20/hour")
async def parse_brief(request: Request, req: ParseBriefRequest):
    """Convert free-text architect brief into structured RoomBriefModel.

    Uses GPT-4.1-mini. On parse failure returns a minimal valid brief so the
    wizard never blocks — the frontend shows the result for human confirmation.
    """
    import json as _json
    from openai import OpenAI

    def _default_brief() -> RoomBriefModel:
        return RoomBriefModel(units=[
            UnitBriefModel(unit_id=i + 1, rooms=[
                RoomSpecModel(role="living", count=1, min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="kitchen", count=1, min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="bedroom", count=2, min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="bathroom", count=1, min_area_m2=0, storey_preference=0),
            ])
            for i in range(req.units_target)
        ])

    system_msg = (
        "You are a Toronto architect assistant. Parse the building program brief into JSON.\n\n"
        "Return ONLY valid JSON with this exact structure:\n"
        '{"units":[{"unit_id":1,"rooms":[{"role":"bedroom","count":2,"min_area_m2":0.0,"storey_preference":0},...]}],'
        '"stack_preference":"vertical","notes":"","confidence":0.85,"unrecognized_terms":[]}\n\n'
        "Valid roles: bedroom, master_bedroom, living, dining, kitchen, bathroom, powder_room, "
        "laundry, corridor, entry, storage, balcony, void\n\n"
        "storey_preference (integer): -1=basement, 0=ground floor, 1=upper/second floor\n\n"
        "Rules:\n"
        "- Every unit MUST include: living×1, kitchen×1, at least 1 bedroom or master_bedroom, at least 1 bathroom\n"
        "- Do NOT include stair cells — those are auto-generated by the geometry engine\n"
        "- If units_target is given, output exactly that many units\n"
        "- stack_preference: 'vertical' for stacked units, 'horizontal' for side-by-side\n"
        "- confidence: 0.0–1.0 reflecting how explicitly the brief specifies the layout\n"
        "- unrecognized_terms: terms you couldn't map to a valid role"
    )

    user_msg = (
        f"units_target: {req.units_target}\n\n"
        f"Brief:\n{req.text}"
    )

    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=1200,
            timeout=15.0,
        )
        raw = resp.choices[0].message.content or "{}"
        data = _json.loads(raw)

        # Coerce storey_preference to int (LLM sometimes returns strings)
        _storey_map = {"ground": 0, "basement": -1, "upper": 1, "second": 1}
        raw_units = data.get("units", [])
        coerced_units = []
        for u in raw_units[:req.units_target]:
            rooms = []
            for r in u.get("rooms", []):
                sp = r.get("storey_preference", 0)
                if isinstance(sp, str):
                    sp = _storey_map.get(sp.lower(), 0)
                rooms.append(RoomSpecModel(
                    role=str(r.get("role", "storage")),
                    count=max(1, int(r.get("count", 1))),
                    min_area_m2=0.0,
                    storey_preference=int(sp),
                ))
            # Ensure required rooms are present
            existing_roles = {rm.role for rm in rooms}
            for required in (("living", 0), ("kitchen", 0)):
                if required[0] not in existing_roles:
                    rooms.append(RoomSpecModel(role=required[0], count=1, min_area_m2=0, storey_preference=required[1]))
            if "bedroom" not in existing_roles and "master_bedroom" not in existing_roles:
                rooms.append(RoomSpecModel(role="bedroom", count=2, min_area_m2=0, storey_preference=0))
            if "bathroom" not in existing_roles:
                rooms.append(RoomSpecModel(role="bathroom", count=1, min_area_m2=0, storey_preference=0))
            coerced_units.append(UnitBriefModel(unit_id=int(u.get("unit_id", len(coerced_units) + 1)), rooms=rooms))

        # Pad to units_target if LLM returned fewer
        while len(coerced_units) < req.units_target:
            i = len(coerced_units)
            coerced_units.append(UnitBriefModel(unit_id=i + 1, rooms=[
                RoomSpecModel(role="living", count=1, min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="kitchen", count=1, min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="bedroom", count=2, min_area_m2=0, storey_preference=0),
                RoomSpecModel(role="bathroom", count=1, min_area_m2=0, storey_preference=0),
            ]))

        sp = str(data.get("stack_preference", "vertical"))
        if sp not in ("vertical", "horizontal"):
            sp = "vertical"

        return ParseBriefResponse(
            room_brief=RoomBriefModel(
                units=coerced_units,
                stack_preference=sp,
                notes=str(data.get("notes", ""))[:1000],
            ),
            confidence=float(data.get("confidence", 0.5)),
            unrecognized_terms=[str(t) for t in data.get("unrecognized_terms", [])],
        )

    except Exception:
        return ParseBriefResponse(
            room_brief=_default_brief(),
            confidence=0.0,
            unrecognized_terms=[],
        )


class SuggestRequest(BaseModel):
    polygon_wkt: str = Field(..., max_length=500_000)
    zone_symbol: str = Field(..., max_length=40)
    units_target: int = Field(default=1, ge=1, le=6)
    ward: Optional[int] = None
    brief: Optional[str] = Field(default=None, max_length=1000)
    exception_constraints: Optional[dict] = None
    override: Optional[dict] = None
    lot_frontage_m: Optional[float] = Field(default=None, ge=3.0, le=100.0)
    include_laneway: bool = False
    road_bearing_deg: Optional[float] = None
    abutting_zones: Optional[dict[str, str]] = Field(default=None)


class RankedTypologyResponse(BaseModel):
    typology_id: str
    label: str
    units_produced: int
    stacking_axis: str
    deterministic_score: float
    fits_lot: bool
    ai_reason: str
    rank: int


class SuggestResponse(BaseModel):
    ranked: list[RankedTypologyResponse]
    envelope_summary: dict


# ---------------------------------------------------------------------------
# Expanded Parameter Schema + Live Validation (Feature 1)
# ---------------------------------------------------------------------------

class ParamsSchemaRequest(BaseModel):
    zone_symbol: str = Field(..., max_length=40)
    lot_frontage_m: Optional[float] = Field(default=None, ge=0.0, le=500.0)
    lot_depth_m: Optional[float] = Field(default=None, ge=0.0, le=500.0)
    lot_area_m2: Optional[float] = Field(default=None, ge=0.0, le=50000.0)
    exception_constraints: Optional[dict] = None
    overlay_data: Optional[dict] = None
    is_corner_lot: bool = False
    is_through_lot: bool = False
    has_lane_abuttal: bool = False
    ward: Optional[str] = None


class ValidateRequest(BaseModel):
    zone_symbol: str = Field(..., max_length=40)
    proposed: dict = Field(
        ...,
        description=(
            "Map of param_key → proposed value. Keys mirror ResolvedParam keys, "
            "e.g. front_yard_setback_m, building_height_max_m, dwelling_unit_count."
        ),
    )
    lot_data: Optional[dict] = None
    exception_constraints: Optional[dict] = None
    overlay_data: Optional[dict] = None
    near_transit: bool = False
    ward: Optional[str] = None
    # When provided, validation uses snapshot values instead of re-resolving from
    # zone_symbol — guarantees the same numbers that built the envelope.
    zoning_snapshot: Optional[dict] = Field(default=None)


def _resolved_param_to_dict(p) -> dict:
    return {
        "key": p.key,
        "value": p.value,
        "unit": p.unit,
        "source": p.source,
        "citation": p.citation,
        "editable_basic": p.editable_basic,
        "editable_advanced": p.editable_advanced,
        "label": p.label,
        "description": p.description,
        "category": p.category,
        "min_val": p.min_val,
        "max_val": p.max_val,
        "amendment_flag": p.amendment_flag,
        "options": p.options,
    }


@router.get("/packgen/params/schema")
async def get_params_schema(
    zone_symbol: str = Query(..., max_length=40),
    lot_frontage_m: Optional[float] = Query(default=None, ge=0.0, le=500.0),
    lot_depth_m: Optional[float] = Query(default=None, ge=0.0, le=500.0),
    lot_area_m2: Optional[float] = Query(default=None, ge=0.0, le=50000.0),
    is_corner_lot: bool = Query(default=False),
    is_through_lot: bool = Query(default=False),
    has_lane_abuttal: bool = Query(default=False),
    ward: Optional[str] = Query(default=None, max_length=60),
):
    """Return all resolved By-law 569-2013 parameters with citations and JSON Schema metadata.

    Used by the Advanced Parameter Tweaker to populate all 10 accordion sections.
    Response is deterministic given the same inputs — no LLM call.
    """
    from packgen.zoning_resolver import resolve_zoning

    lot_data = {
        "lot_frontage_m": lot_frontage_m,
        "lot_depth_m": lot_depth_m,
        "lot_area_m2": lot_area_m2,
        "is_corner_lot": is_corner_lot,
        "is_through_lot": is_through_lot,
        "has_lane_abuttal": has_lane_abuttal,
        "ward": ward,
    }

    resolved = resolve_zoning(
        zone_symbol=zone_symbol,
        lot_data=lot_data,
    )

    return {
        "zone_code": resolved.zone_code,
        "zone_label_full": resolved.zone_label_full,
        "amendment_flags": resolved.amendment_flags,
        "warnings": resolved.warnings,
        "categories": resolved.categories,
        "params": {k: _resolved_param_to_dict(p) for k, p in resolved.params.items()},
        "param_count": len(resolved.params),
    }


@router.post("/packgen/params/validate")
async def validate_params(req: ValidateRequest):
    """Validate proposed parameter values against By-law 569-2013.

    Returns ok | variance | violation per parameter with citation and message.
    Designed to respond in ≤200 ms — no external calls.

    If ``zoning_snapshot`` is provided, validation uses the snapshot values —
    the exact params used to build the envelope — instead of re-resolving from
    zone_symbol. This guarantees the envelope and validator can never disagree.
    """
    from packgen.validators import summarize, validate_against_snapshot, validate_all
    from packgen.zoning_resolver import resolve_zoning

    if req.zoning_snapshot:
        from packgen.schemas.zoning_snapshot import ZoningSnapshot
        try:
            snapshot = ZoningSnapshot(**req.zoning_snapshot)
            results = validate_against_snapshot(
                proposed=req.proposed,
                snapshot=snapshot,
                near_transit=req.near_transit,
                ward=req.ward,
            )
        except Exception:
            # Malformed snapshot — fall back to zone_symbol resolution
            results = None
    else:
        results = None

    if results is None:
        resolved = resolve_zoning(
            zone_symbol=req.zone_symbol,
            lot_data=req.lot_data,
            exception_constraints=req.exception_constraints,
            overlay_data=req.overlay_data,
        )
        results = validate_all(
            proposed=req.proposed,
            resolved=resolved,
            near_transit=req.near_transit,
            ward=req.ward,
        )

    return {
        "summary": summarize(results),
        "results": [
            {
                "param_key": r.param_key,
                "status": r.status,
                "message": r.message,
                "citation": r.citation,
                "proposed": r.proposed,
                "limit": r.limit,
                "tolerance": r.tolerance,
            }
            for r in results
        ],
    }


@router.post("/suggest-typology", response_model=SuggestResponse)
@limiter.limit("30/hour")
async def suggest_typology(request: Request, req: SuggestRequest):
    # (existing endpoint — kept for backwards compatibility)
    """Return top-3 typologies for this lot with AI-narrated reasons.

    The deterministic score is always the authority; the LLM only narrates.
    If the OpenAI call fails, returns the same ranking with ai_reason="" on
    every entry — the badge degrades gracefully without blocking the response.
    """
    from packgen.suggest import rank_typologies
    from packgen.zoning_resolver import resolve_zoning

    _lot_data = {
        "lot_frontage_m":  req.lot_frontage_m,
        "lot_depth_m":     None,
        "lot_area_m2":     None,
        "is_corner_lot":   False,
        "is_through_lot":  False,
        "has_lane_abuttal": req.include_laneway,
        "ward": req.ward,
        "abutting_zones":  req.abutting_zones,
    }
    resolved = resolve_zoning(
        req.zone_symbol,
        lot_data=_lot_data,
        exception_constraints=req.exception_constraints,
    )
    params = resolved_to_envelope_params(
        resolved, _lot_data, req.override,
        req.units_target, None, req.include_laneway,
    )
    er = build_envelope(
        polygon_wkt_4326=req.polygon_wkt,
        front_setback_m=params.front_setback_m,
        rear_setback_m=params.rear_setback_m,
        left_setback_m=params.left_setback_m,
        right_setback_m=params.right_setback_m,
        lot_frontage_m=params.lot_frontage_m,
        zone_symbol=req.zone_symbol,
        max_coverage_pct=params.max_coverage_pct,
        include_laneway=params.include_laneway,
        road_bearing_deg=req.road_bearing_deg,
        apply_side_step_back=(resolved.zone_code == "RD" and (req.lot_frontage_m or 0.0) > 18.0),
    )

    loop = asyncio.get_event_loop()
    ranked = await loop.run_in_executor(
        _PACKGEN_EXECUTOR,
        rank_typologies,
        er.envelope_2d,
        req.zone_symbol,
        req.units_target,
        req.ward,
        req.brief,
        3,
    )
    return SuggestResponse(
        ranked=[RankedTypologyResponse(**vars(r)) for r in ranked],
        envelope_summary={
            "width_m": round(er.lot_width_m, 2),
            "depth_m": round(er.lot_depth_m, 2),
            "area_m2": round(er.lot_area_m2, 1),
        },
    )


# ---------------------------------------------------------------------------
# Design Studio — suggestion cards with massing preview SVGs (Feature 2)
# ---------------------------------------------------------------------------

class DesignStudioSuggestRequest(BaseModel):
    polygon_wkt: str = Field(..., max_length=500_000)
    zone_symbol: str = Field(..., max_length=40)
    units_target: int = Field(default=2, ge=1, le=6)
    ward: Optional[int] = None
    brief: Optional[str] = Field(default=None, max_length=1200)
    exception_constraints: Optional[dict] = None
    override: Optional[dict] = None
    lot_frontage_m: Optional[float] = Field(default=None, ge=3.0, le=100.0)
    include_laneway: bool = False
    road_bearing_deg: Optional[float] = None
    abutting_zones: Optional[dict[str, str]] = Field(default=None)


class SuggestionCard(BaseModel):
    typology_id: str
    label: str
    rationale: str
    est_gfa_m2: float
    est_units: int
    est_height_m: float
    est_storeys: int
    preview_svg: str
    suitability_score: float


class DesignStudioSuggestResponse(BaseModel):
    suggestions: list[SuggestionCard]
    envelope_summary: dict


def _build_suggestion_cards(ranked_list, er) -> list[dict]:
    """Fit stamp + generate preview SVG for each ranked typology (runs in executor)."""
    from packgen.svg_preview import generate_svg
    from packgen.typology.library import TYPOLOGY_LIBRARY
    from packgen.typology.selector import fit_stamp

    library_map = {t.id: t for t in TYPOLOGY_LIBRARY}
    cards = []
    for ranked in ranked_list:
        t = library_map.get(ranked.typology_id)
        if t is None:
            continue
        try:
            fit = fit_stamp(t, er.envelope_2d, option="A")
            raw_svg = generate_svg(fit, er)
            svg_str = raw_svg if isinstance(raw_svg, str) else raw_svg.decode("utf-8")
        except Exception:
            svg_str = ""

        est_height = round(t.target_storeys * 2.85, 1)

        cards.append({
            "typology_id": ranked.typology_id,
            "label": ranked.label,
            "rationale": ranked.ai_reason or f"{t.units_produced}-unit {t.label} — fits {er.lot_width_m:.1f}m frontage.",
            "est_gfa_m2": round(fit.gfa_m2, 0) if "fit" in dir() else round(t.target_gfa_per_unit_m2[1] * t.units_produced, 0),
            "est_units": ranked.units_produced,
            "est_height_m": est_height,
            "est_storeys": t.target_storeys,
            "preview_svg": svg_str,
            "suitability_score": round(ranked.deterministic_score, 3),
        })
    return cards


@router.post("/design-studio/suggest", response_model=DesignStudioSuggestResponse)
@limiter.limit("30/minute")
async def design_studio_suggest(request: Request, req: DesignStudioSuggestRequest):
    """Return 3-5 AI suggestion cards for the Design Studio.

    Each card includes a one-sentence rationale, GFA estimate, and a
    massing-preview SVG generated from the stamp fit. The LLM call
    (for rationale) runs in the executor alongside stamp fitting.
    Response target: <8s p95.
    """
    from packgen.suggest import rank_typologies
    from packgen.zoning_resolver import resolve_zoning

    _lot_data = {
        "lot_frontage_m":  req.lot_frontage_m,
        "lot_depth_m":     None,
        "lot_area_m2":     None,
        "is_corner_lot":   False,
        "is_through_lot":  False,
        "has_lane_abuttal": req.include_laneway,
        "ward": req.ward,
        "abutting_zones":  req.abutting_zones,
    }
    resolved = resolve_zoning(
        req.zone_symbol,
        lot_data=_lot_data,
        exception_constraints=req.exception_constraints,
    )
    params = resolved_to_envelope_params(
        resolved, _lot_data, req.override,
        req.units_target, None, req.include_laneway,
    )
    er = build_envelope(
        polygon_wkt_4326=req.polygon_wkt,
        front_setback_m=params.front_setback_m,
        rear_setback_m=params.rear_setback_m,
        left_setback_m=params.left_setback_m,
        right_setback_m=params.right_setback_m,
        lot_frontage_m=params.lot_frontage_m,
        zone_symbol=req.zone_symbol,
        max_coverage_pct=params.max_coverage_pct,
        include_laneway=params.include_laneway,
        road_bearing_deg=req.road_bearing_deg,
        apply_side_step_back=(resolved.zone_code == "RD" and (req.lot_frontage_m or 0.0) > 18.0),
    )

    loop = asyncio.get_event_loop()

    # Step 1: rank typologies (LLM narration included, 8s timeout inside)
    ranked = await loop.run_in_executor(
        _PACKGEN_EXECUTOR,
        rank_typologies,
        er.envelope_2d,
        req.zone_symbol,
        req.units_target,
        req.ward,
        req.brief,
        5,                     # top-5 candidates
    )

    if not ranked:
        return DesignStudioSuggestResponse(
            suggestions=[],
            envelope_summary={
                "width_m": round(er.lot_width_m, 2),
                "depth_m": round(er.lot_depth_m, 2),
                "area_m2": round(er.lot_area_m2, 1),
            },
        )

    # Step 2: build suggestion cards (stamp + SVG) in executor
    raw_cards = await loop.run_in_executor(
        _PACKGEN_EXECUTOR,
        _build_suggestion_cards,
        ranked,
        er,
    )

    return DesignStudioSuggestResponse(
        suggestions=[SuggestionCard(**c) for c in raw_cards],
        envelope_summary={
            "width_m": round(er.lot_width_m, 2),
            "depth_m": round(er.lot_depth_m, 2),
            "area_m2": round(er.lot_area_m2, 1),
        },
    )
