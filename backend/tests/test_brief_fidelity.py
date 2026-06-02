"""Solver-path brief fidelity integration tests.

Tests that generate_floor_plan produces exactly what the brief specifies.
These run the full solver path (no LLM) with stacking="horizontal" which
exercises the per-unit column division added in layout_solver.py.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from shapely.geometry import box as shapely_box

from packgen.pipeline.orchestrator import generate_floor_plan
from packgen.geometry import EnvelopeResult
from packgen.schemas.contracts import BriefRoomSpec, BriefUnit, DesignBrief
from packgen.rules.code_rules import ROOM_MIN_AREA_M2, ROOM_MAX_AREA_M2


def _make_envelope(width_m: float = 16.7, depth_m: float = 16.7) -> EnvelopeResult:
    """Minimal EnvelopeResult for testing — flat rectangular lot."""
    poly = shapely_box(0, 0, width_m, depth_m)
    lot_poly = shapely_box(0, 0, width_m + 2, depth_m + 2)
    return EnvelopeResult(
        envelope_2d=poly,
        lot_local=lot_poly,
        setback_lines={},
        setbacks_applied={"front": 1.0, "rear": 1.0, "left": 0.5, "right": 0.5},
        lot_width_m=width_m + 2,
        lot_depth_m=depth_m + 2,
        lot_area_m2=(width_m + 2) * (depth_m + 2),
        rotation_deg=0.0,
        origin_mtm=(0.0, 0.0),
        angular_plane_applied=False,
        depth_limit_m=17.0,
        warnings=[],
    )


def _make_fourplex_brief() -> DesignBrief:
    """4-unit horizontal brief: 3 bed / 1 living / 3 bath / kitchen + dining + balcony + storage."""
    rooms_per_unit = [
        BriefRoomSpec(role="bedroom",  count=3, storey_preference=1),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=3, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="dining",   count=1, storey_preference=0),
        BriefRoomSpec(role="balcony",  count=1, storey_preference=0),
        BriefRoomSpec(role="storage",  count=1, storey_preference=1),
    ]
    return DesignBrief(
        units=[BriefUnit(unit_id=i + 1, rooms=rooms_per_unit) for i in range(4)],
        stacking_pref="horizontal",
    )


def test_unit_columns_do_not_overlap():
    """In a horizontal fourplex, Unit A rooms must not overlap Unit B rooms in x."""
    brief = _make_fourplex_brief()
    env = _make_envelope(16.7, 16.7)
    plan = generate_floor_plan(brief, env, target_floors=2,
                               target_stacking="horizontal", openai_client=None)

    storey_0 = next((s for s in plan.storeys if s.level == 0), None)
    assert storey_0 is not None, "No ground floor storey"

    by_unit: dict[int, list] = {}
    for r in storey_0.rooms:
        if r.dwelling_unit_id is not None:
            uid = int(r.dwelling_unit_id)
            by_unit.setdefault(uid, []).append(r)

    if len(by_unit) < 2:
        pytest.skip("Only one unit on ground floor — stacking may be vertical")

    unit_x_ranges = {}
    for uid, rooms in by_unit.items():
        xs = [v[0] for r in rooms for v in r.polygon]
        unit_x_ranges[uid] = (min(xs), max(xs))

    unit_ids = sorted(unit_x_ranges)
    for i in range(len(unit_ids) - 1):
        uid_a, uid_b = unit_ids[i], unit_ids[i + 1]
        _, a_max = unit_x_ranges[uid_a]
        b_min, _ = unit_x_ranges[uid_b]
        assert a_max <= b_min + 0.2, (
            f"Unit {uid_a} x-max {a_max:.2f} overlaps Unit {uid_b} x-min {b_min:.2f}"
        )


def test_storey_preference_honored():
    """Bedrooms (pref=1) appear on storey 1; kitchens (pref=0) on storey 0."""
    brief = _make_fourplex_brief()
    env = _make_envelope(16.7, 16.7)
    plan = generate_floor_plan(brief, env, target_floors=2,
                               target_stacking="horizontal", openai_client=None)

    storey_0_labels = {
        r.label.lower().replace(" ", "_")
        for s in plan.storeys if s.level == 0
        for r in s.rooms
    }
    storey_1_labels = {
        r.label.lower().replace(" ", "_")
        for s in plan.storeys if s.level == 1
        for r in s.rooms
    }

    assert "kitchen" in storey_0_labels, f"kitchen missing from storey 0; got {storey_0_labels}"
    assert "bedroom" in storey_1_labels, f"bedroom missing from storey 1; got {storey_1_labels}"


def test_room_areas_within_obc_bounds():
    """No room exceeds ROOM_MAX_AREA_M2 by more than 20%."""
    brief = _make_fourplex_brief()
    env = _make_envelope(16.7, 16.7)
    plan = generate_floor_plan(brief, env, target_floors=2,
                               target_stacking="horizontal", openai_client=None)

    all_rooms = [r for s in plan.storeys for r in s.rooms]
    for room in all_rooms:
        role = room.label.lower().replace(" ", "_")
        max_area = ROOM_MAX_AREA_M2.get(role)
        if max_area and room.area_m2:
            assert room.area_m2 <= max_area * 1.20, (
                f"{role} area {room.area_m2:.1f}m² exceeds max {max_area}m²"
            )


def test_per_unit_independent_expansion():
    """Units with different room configs each get their OWN rooms."""
    unit1_rooms = [
        BriefRoomSpec(role="bedroom", count=3, storey_preference=1),
        BriefRoomSpec(role="living",  count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen", count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=2, storey_preference=0),
    ]
    unit2_rooms = [
        BriefRoomSpec(role="bedroom", count=2, storey_preference=1),
        BriefRoomSpec(role="living",  count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen", count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ]
    brief = DesignBrief(
        units=[BriefUnit(unit_id=1, rooms=unit1_rooms),
               BriefUnit(unit_id=2, rooms=unit2_rooms)],
        stacking_pref="vertical",
    )
    env = _make_envelope(10.0, 12.0)
    plan = generate_floor_plan(brief, env, target_floors=2,
                               target_stacking="vertical", openai_client=None)

    all_rooms = [r for s in plan.storeys for r in s.rooms]
    # Unit 0 (unit_id=1, 0-indexed=0): 3 bedrooms
    u0_bedrooms = [r for r in all_rooms
                   if r.label.lower() == "bedroom" and r.dwelling_unit_id == "0"]
    # Unit 1 (unit_id=2, 0-indexed=1): 2 bedrooms
    u1_bedrooms = [r for r in all_rooms
                   if r.label.lower() == "bedroom" and r.dwelling_unit_id == "1"]

    assert len(u0_bedrooms) == 3, f"unit 0 should have 3 bedrooms, got {len(u0_bedrooms)}"
    assert len(u1_bedrooms) == 2, f"unit 1 should have 2 bedrooms, got {len(u1_bedrooms)}"


def test_horizontal_stacking_has_per_unit_stairs():
    """Horizontal stacking: each unit gets its own stair cell."""
    from packgen.program.space_program import generate_space_program
    unit_rooms = [
        BriefRoomSpec(role="bedroom", count=2, storey_preference=1),
        BriefRoomSpec(role="living",  count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen", count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ]
    brief = DesignBrief(
        units=[BriefUnit(unit_id=1, rooms=unit_rooms),
               BriefUnit(unit_id=2, rooms=unit_rooms)],
        stacking_pref="horizontal",
    )
    env = _make_envelope(14.0, 12.0)
    program, warnings = generate_space_program(brief, env, target_floors=2)

    stair_rooms = [r for r in program.rooms if r.role == "stair"]
    # Each unit should have its own stair
    assert len(stair_rooms) == 2, f"expected 2 stairs (one per unit), got {len(stair_rooms)}"
    stair_unit_ids = {r.unit_id for r in stair_rooms}
    assert len(stair_unit_ids) == 2, f"stairs should belong to different units, got {stair_unit_ids}"


def test_vertical_stacking_has_shared_stair():
    """Vertical stacking: one shared stair (unit_id=-1) for the building."""
    from packgen.program.space_program import generate_space_program
    unit_rooms = [
        BriefRoomSpec(role="bedroom", count=2, storey_preference=1),
        BriefRoomSpec(role="living",  count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen", count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ]
    brief = DesignBrief(
        units=[BriefUnit(unit_id=1, rooms=unit_rooms),
               BriefUnit(unit_id=2, rooms=unit_rooms)],
        stacking_pref="vertical",
    )
    env = _make_envelope(10.0, 12.0)
    program, warnings = generate_space_program(brief, env, target_floors=2)

    stair_rooms = [r for r in program.rooms if r.role == "stair"]
    assert len(stair_rooms) == 1, f"expected 1 shared stair, got {len(stair_rooms)}"
    assert stair_rooms[0].unit_id == -1, f"shared stair should have unit_id=-1"


def test_room_count_matches_brief():
    """4-unit horizontal fourplex: every brief role appears with the correct count."""
    brief = _make_fourplex_brief()
    env = _make_envelope(16.7, 16.7)
    plan = generate_floor_plan(brief, env, target_floors=2,
                               target_stacking="horizontal", openai_client=None)

    all_rooms = [r for s in plan.storeys for r in s.rooms]

    def _count(label: str) -> int:
        return sum(1 for r in all_rooms if r.label.lower() == label)

    # 4 units × 3 bedrooms = 12; void rooms may absorb OBC-clipped space — exclude them
    assert _count("bedroom") == 12,  f"bedrooms: {_count('bedroom')} (expected 12)"
    assert _count("kitchen") == 4,   f"kitchens: {_count('kitchen')} (expected 4)"
    assert _count("living")  == 4,   f"living: {_count('living')} (expected 4)"
    # Dining/balcony/storage: solver path emits them directly from space_program
    assert _count("dining")  == 4,   f"dining: {_count('dining')} (expected 4)"
    assert _count("balcony") == 4,   f"balconies: {_count('balcony')} (expected 4)"
    assert _count("storage") == 4,   f"storage: {_count('storage')} (expected 4)"
