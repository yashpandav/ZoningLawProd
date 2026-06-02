"""Tests for solve_core — deterministic, no LLM."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import math
import pytest
from shapely.geometry import box as shapely_box, Polygon

from packgen.stacking.core_solver import (
    solve_core, _stair_footprint, _snap, _WET_COL_W, _WET_COL_D,
)
from packgen.rules.code_rules import ROOM_MIN_AREA_M2, ROOM_MIN_DIM_M
from packgen.schemas.contracts import DesignBrief, BriefUnit, BriefRoomSpec, SpaceProgram
from packgen.program.space_program import generate_space_program
from packgen.geometry import EnvelopeResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _rect_envelope(w: float = 10.0, d: float = 12.0) -> Polygon:
    return shapely_box(0.0, 0.0, w, d)


def _make_program(units: int = 1, floors: int = 2) -> SpaceProgram:
    rooms_per_unit = [
        BriefRoomSpec(role="bedroom",  count=2, storey_preference=1),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ]
    brief = DesignBrief(units=[
        BriefUnit(unit_id=i + 1, rooms=rooms_per_unit) for i in range(units)
    ])
    env = EnvelopeResult(
        envelope_2d=_rect_envelope(), lot_local=_rect_envelope(),
        setback_lines={}, setbacks_applied={},
        lot_width_m=10, lot_depth_m=12, lot_area_m2=120,
        rotation_deg=0.0, origin_mtm=(0.0, 0.0),
        angular_plane_applied=False, depth_limit_m=17.0, warnings=[],
    )
    program, _ = generate_space_program(brief, env, target_floors=floors)
    return program


# ---------------------------------------------------------------------------
# Acceptance test 1: stair inside envelope, same rect for all storeys
# ---------------------------------------------------------------------------

def test_stair_rect_inside_10x12_envelope():
    """stair_rect must lie entirely within the 10×12 envelope."""
    program = _make_program()
    envelope = _rect_envelope(10.0, 12.0)
    core = solve_core(program, envelope, target_floors=2)

    s = core.stair_rect
    assert s.x0 >= -0.01 and s.x1 <= 10.01, f"Stair x-range [{s.x0},{s.x1}] outside [0,10]"
    assert s.y0 >= -0.01 and s.y1 <= 12.01, f"Stair y-range [{s.y0},{s.y1}] outside [0,12]"


def test_stair_present_on_all_above_grade_storeys():
    """present_on_storeys must contain every above-grade storey."""
    program = _make_program(floors=3)
    core = solve_core(program, _rect_envelope(), target_floors=3)
    assert set(core.present_on_storeys) >= {0, 1, 2}


def test_stair_excludes_basement_when_none_in_program():
    """If no basement rooms exist, -1 must not appear in present_on_storeys."""
    program = _make_program(floors=2)
    core = solve_core(program, _rect_envelope(), target_floors=2)
    assert -1 not in core.present_on_storeys


def test_stair_includes_basement_when_present():
    """If any program room has storey=-1, present_on_storeys must include -1."""
    from packgen.schemas.contracts import ProgramRoom
    program = _make_program(floors=2)
    # Inject a basement room
    basement_room = ProgramRoom(
        id="mech_s_0", role="mechanical", unit_id=-1, storey=-1,
        target_area_m2=3.5, zone_class="service",
    )
    program = program.model_copy(update={"rooms": program.rooms + [basement_room]})
    core = solve_core(program, _rect_envelope(), target_floors=2)
    assert -1 in core.present_on_storeys


# ---------------------------------------------------------------------------
# Acceptance test 2: footprint area ≥ OBC minimum
# ---------------------------------------------------------------------------

def test_stair_footprint_area_gte_obc_min():
    """Stair footprint area must be ≥ ROOM_MIN_AREA_M2['stair'] = 3.5 m²."""
    w, d = _stair_footprint(2.85)
    area = w * d
    assert area >= ROOM_MIN_AREA_M2["stair"] - 1e-3, (
        f"Stair area {area:.3f} m² < OBC minimum {ROOM_MIN_AREA_M2['stair']} m²"
    )


def test_stair_footprint_width_gte_clear_width():
    """Stair width must be ≥ ROOM_MIN_DIM_M['stair'] = 0.86 m."""
    w, _ = _stair_footprint(2.85)
    assert w >= ROOM_MIN_DIM_M["stair"] - 1e-3, (
        f"Stair width {w:.3f} m < OBC clear minimum {ROOM_MIN_DIM_M['stair']} m"
    )


@pytest.mark.parametrize("ftf", [2.4, 2.7, 2.85, 3.0, 3.5])
def test_stair_footprint_area_for_various_floor_heights(ftf):
    """OBC area minimum must hold across all realistic floor-to-floor heights."""
    w, d = _stair_footprint(ftf)
    assert w * d >= ROOM_MIN_AREA_M2["stair"] - 1e-3


# ---------------------------------------------------------------------------
# Wet columns adjacent to stair
# ---------------------------------------------------------------------------

def test_wet_columns_adjacent_to_stair():
    """Every wet column must share an edge with the stair_rect (touching, not overlapping)."""
    program = _make_program()
    core = solve_core(program, _rect_envelope(), target_floors=2)

    s = core.stair_rect
    for wc in core.wet_columns:
        # One of the four touching conditions must hold (within 100 mm snap tolerance)
        tol = 0.15
        left_ok  = abs(wc.x1 - s.x0) < tol
        right_ok = abs(wc.x0 - s.x1) < tol
        front_ok = abs(wc.y1 - s.y0) < tol
        rear_ok  = abs(wc.y0 - s.y1) < tol
        assert left_ok or right_ok or front_ok or rear_ok, (
            f"Wet column [{wc.x0},{wc.y0}]-[{wc.x1},{wc.y1}] does not touch "
            f"stair [{s.x0},{s.y0}]-[{s.x1},{s.y1}]"
        )


def test_wet_columns_inside_envelope():
    """All wet columns must lie inside the envelope."""
    program = _make_program()
    envelope = _rect_envelope(10.0, 12.0)
    core = solve_core(program, envelope, target_floors=2)

    for wc in core.wet_columns:
        assert wc.x0 >= -0.01 and wc.x1 <= 10.01
        assert wc.y0 >= -0.01 and wc.y1 <= 12.01


def test_wet_column_count_between_1_and_2():
    """Solver must return 1 or 2 wet columns."""
    program = _make_program()
    core = solve_core(program, _rect_envelope(), target_floors=2)
    assert 1 <= len(core.wet_columns) <= 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic():
    """Same inputs → identical CoreSpec on two calls."""
    program  = _make_program()
    envelope = _rect_envelope()
    c1 = solve_core(program, envelope, target_floors=2)
    c2 = solve_core(program, envelope, target_floors=2)

    assert c1.stair_rect.x0 == c2.stair_rect.x0
    assert c1.stair_rect.y0 == c2.stair_rect.y0
    assert c1.stair_rect.x1 == c2.stair_rect.x1
    assert c1.stair_rect.y1 == c2.stair_rect.y1
    assert c1.present_on_storeys == c2.present_on_storeys


# ---------------------------------------------------------------------------
# Non-rectangular envelope (L-shape)
# ---------------------------------------------------------------------------

def test_lshaped_envelope_stair_inside():
    """Core solver must work on non-rectangular envelopes."""
    # L-shape: 10×12 minus top-right 4×5 corner
    outer = shapely_box(0.0, 0.0, 10.0, 12.0)
    notch = shapely_box(6.0, 7.0, 10.0, 12.0)
    l_shape = outer.difference(notch)

    program = _make_program()
    core = solve_core(program, l_shape, target_floors=2)

    # Stair must be fully inside the L-shape
    from shapely.geometry import box as sb
    stair_box = sb(core.stair_rect.x0, core.stair_rect.y0,
                   core.stair_rect.x1, core.stair_rect.y1)
    assert l_shape.contains(stair_box) or l_shape.buffer(0.12).contains(stair_box), (
        "Stair footprint falls outside L-shaped envelope"
    )


# ---------------------------------------------------------------------------
# Stair not blocking front entry
# ---------------------------------------------------------------------------

def test_stair_prefers_not_blocking_front_entry():
    """Stair should not be placed at y0 < 1.5 m (entry zone) unless unavoidable."""
    # Use a very deep lot where avoiding the front is easy
    program = _make_program()
    core = solve_core(program, _rect_envelope(8.0, 20.0), target_floors=2)
    # With 20 m depth there's always room away from entry zone
    assert core.stair_rect.y0 >= 1.4, (
        f"Stair placed in entry zone: y0={core.stair_rect.y0:.2f} m"
    )
