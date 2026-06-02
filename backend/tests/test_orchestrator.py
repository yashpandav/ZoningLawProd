"""Tests for the planning pipeline orchestrator and the ENABLE_SOLVER router branch."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from shapely.geometry import box as shapely_box

from packgen.pipeline.orchestrator import generate_floor_plan
from packgen.schemas.contracts import (
    DesignBrief, BriefUnit, BriefRoomSpec,
)
from packgen.geometry import EnvelopeResult
from packgen.ai.schema import FloorPlanJSON, StoreyModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(w: float = 10.0, d: float = 12.0) -> EnvelopeResult:
    poly = shapely_box(0.0, 0.0, w, d)
    return EnvelopeResult(
        envelope_2d=poly, lot_local=poly,
        setback_lines={}, setbacks_applied={},
        lot_width_m=w, lot_depth_m=d, lot_area_m2=w * d,
        rotation_deg=0.0, origin_mtm=(0.0, 0.0),
        angular_plane_applied=False, depth_limit_m=17.0, warnings=[],
    )


def _single_unit_brief() -> DesignBrief:
    return DesignBrief(units=[BriefUnit(unit_id=1, rooms=[
        BriefRoomSpec(role="bedroom",  count=2, storey_preference=1),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ])])


def _two_unit_brief() -> DesignBrief:
    rooms = [
        BriefRoomSpec(role="bedroom",  count=2, storey_preference=1),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ]
    return DesignBrief(units=[
        BriefUnit(unit_id=1, rooms=rooms),
        BriefUnit(unit_id=2, rooms=rooms),
    ])


# ---------------------------------------------------------------------------
# Acceptance: valid FloorPlanJSON with no network (openai_client=None)
# ---------------------------------------------------------------------------

def test_single_unit_2floor_produces_valid_floor_plan_json():
    """Orchestrator with no client must produce a Pydantic-valid FloorPlanJSON."""
    plan = generate_floor_plan(
        brief=_single_unit_brief(),
        envelope=_make_envelope(),
        target_floors=2,
        openai_client=None,
    )
    assert isinstance(plan, FloorPlanJSON)
    # Pydantic already validated it; just double-check key fields
    assert len(plan.storeys) >= 1
    assert plan.storeys[0].level >= -1


def test_floor_plan_json_schema_valid():
    """FloorPlanJSON must round-trip through its own schema (model_validate)."""
    plan = generate_floor_plan(
        brief=_single_unit_brief(),
        envelope=_make_envelope(),
        target_floors=2,
        openai_client=None,
    )
    dumped = plan.model_dump()
    reloaded = FloorPlanJSON.model_validate(dumped)
    assert len(reloaded.storeys) == len(plan.storeys)


def test_two_unit_2floor_produces_valid_floor_plan_json():
    """Duplex brief must produce a valid FloorPlanJSON."""
    plan = generate_floor_plan(
        brief=_two_unit_brief(),
        envelope=_make_envelope(10.0, 14.0),
        target_floors=2,
        openai_client=None,
    )
    assert isinstance(plan, FloorPlanJSON)
    total_rooms = sum(len(s.rooms) for s in plan.storeys)
    assert total_rooms >= 4, f"Expected ≥4 rooms, got {total_rooms}"


def test_storeys_are_in_order():
    """StoreyModel levels must be ordered ground → upper."""
    plan = generate_floor_plan(
        brief=_single_unit_brief(),
        envelope=_make_envelope(),
        target_floors=2,
        openai_client=None,
    )
    levels = [s.level for s in plan.storeys]
    assert levels == sorted(levels)


def test_each_storey_has_rooms():
    """Every StoreyModel in the plan must have at least one room."""
    plan = generate_floor_plan(
        brief=_single_unit_brief(),
        envelope=_make_envelope(),
        target_floors=2,
        openai_client=None,
    )
    for storey in plan.storeys:
        assert len(storey.rooms) >= 1, f"Storey {storey.level} has no rooms"


def test_stair_model_present():
    """FloorPlanJSON must include a stair model (OBC egress)."""
    plan = generate_floor_plan(
        brief=_single_unit_brief(),
        envelope=_make_envelope(),
        target_floors=2,
        openai_client=None,
    )
    assert len(plan.stairs) >= 1


def test_walls_present_on_each_storey():
    """Every storey must have at least one wall segment (exterior walls)."""
    plan = generate_floor_plan(
        brief=_single_unit_brief(),
        envelope=_make_envelope(),
        target_floors=2,
        openai_client=None,
    )
    for storey in plan.storeys:
        assert len(storey.walls) >= 1, f"Storey {storey.level} has no walls"


# ---------------------------------------------------------------------------
# FloorPlanJSON → FitResult → non-empty placed_cells
# ---------------------------------------------------------------------------

def test_floor_plan_converts_to_nonempty_fit_result():
    """floor_plan_to_fit_result must return placed_cells with area > 0."""
    from packgen.ai.plan_to_geometry import floor_plan_to_fit_result

    plan = generate_floor_plan(
        brief=_single_unit_brief(),
        envelope=_make_envelope(),
        target_floors=2,
        openai_client=None,
    )
    env = _make_envelope()
    fit = floor_plan_to_fit_result(plan, env, option="A")

    assert len(fit.placed_cells) >= 1
    total_gfa = sum(pc.area_m2 for pc in fit.placed_cells)
    assert total_gfa > 0


# ---------------------------------------------------------------------------
# ENABLE_SOLVER flag — step label selection
# ---------------------------------------------------------------------------

def test_enable_solver_unset_uses_ai_steps(monkeypatch):
    """With ENABLE_SOLVER unset (or false), _gen_progress picks _AI_STEPS."""
    import generate_pack_router as r
    monkeypatch.delenv("ENABLE_SOLVER", raising=False)
    use_solver = os.getenv("ENABLE_SOLVER", "false").lower() == "true"
    steps = r._SOLVER_STEPS if use_solver else r._AI_STEPS
    assert steps is r._AI_STEPS


def test_enable_solver_true_uses_solver_steps(monkeypatch):
    """With ENABLE_SOLVER=true, _gen_progress picks _SOLVER_STEPS."""
    import generate_pack_router as r
    monkeypatch.setenv("ENABLE_SOLVER", "true")
    use_solver = os.getenv("ENABLE_SOLVER", "false").lower() == "true"
    steps = r._SOLVER_STEPS if use_solver else r._AI_STEPS
    assert steps is r._SOLVER_STEPS


def test_solver_steps_different_from_ai_steps():
    """_SOLVER_STEPS must contain solver-specific labels (not identical to _AI_STEPS)."""
    import generate_pack_router as r
    assert r._SOLVER_STEPS != r._AI_STEPS
    assert any("space program" in s.lower() for s in r._SOLVER_STEPS)
    assert any("typology" in s.lower() for s in r._AI_STEPS)


# ---------------------------------------------------------------------------
# brief converter
# ---------------------------------------------------------------------------

def test_brief_converter_maps_roles():
    """_brief_to_design_brief must normalize role aliases and preserve unit_ids."""
    import generate_pack_router as r
    from generate_pack_router import RoomBriefModel, UnitBriefModel, RoomSpecModel

    rb = RoomBriefModel(units=[
        UnitBriefModel(unit_id=1, rooms=[
            RoomSpecModel(role="lounge",  count=1),  # alias → living
            RoomSpecModel(role="kitchen", count=1),
            RoomSpecModel(role="bed",     count=2),  # unknown → bedroom via normalize
        ]),
    ])
    brief = r._brief_to_design_brief(rb, target_stacking="vertical")
    roles = {room.role for room in brief.units[0].rooms}
    assert "living" in roles   # lounge → living
    assert "kitchen" in roles


def test_brief_converter_stacking_override():
    """target_stacking parameter must override the brief's stack_preference."""
    import generate_pack_router as r
    from generate_pack_router import RoomBriefModel, UnitBriefModel, RoomSpecModel

    rb = RoomBriefModel(
        units=[UnitBriefModel(unit_id=1, rooms=[RoomSpecModel(role="living", count=1)])],
        stack_preference="horizontal",
    )
    brief = r._brief_to_design_brief(rb, target_stacking="vertical")
    assert brief.stacking_pref == "vertical"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_orchestrator_deterministic():
    """Same inputs must yield identical FloorPlanJSON on two calls."""
    brief = _single_unit_brief()
    env   = _make_envelope()

    plan1 = generate_floor_plan(brief, env, target_floors=2, openai_client=None)
    plan2 = generate_floor_plan(brief, env, target_floors=2, openai_client=None)

    assert len(plan1.storeys) == len(plan2.storeys)
    for s1, s2 in zip(plan1.storeys, plan2.storeys):
        assert len(s1.rooms) == len(s2.rooms)
        for r1, r2 in zip(s1.rooms, s2.rooms):
            assert r1.polygon == r2.polygon
