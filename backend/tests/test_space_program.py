"""Tests for generate_space_program (deterministic, no LLM)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dataclasses import dataclass, field
from shapely.geometry import box as shapely_box
from shapely.geometry import LineString

import pytest

from packgen.program.space_program import generate_space_program
from packgen.schemas.contracts import DesignBrief, BriefUnit, BriefRoomSpec
from packgen.rules.code_rules import ROOM_MIN_AREA_M2, ROOM_MAX_AREA_M2
from packgen.geometry import EnvelopeResult


# ---------------------------------------------------------------------------
# Minimal EnvelopeResult factory for tests (no real geometry pipeline)
# ---------------------------------------------------------------------------

def _make_envelope(width_m: float, depth_m: float) -> EnvelopeResult:
    """Build a rectangular EnvelopeResult for testing."""
    envelope_poly = shapely_box(0.0, 0.0, width_m, depth_m)
    lot_poly      = shapely_box(0.0, 0.0, width_m + 2, depth_m + 2)
    return EnvelopeResult(
        envelope_2d=envelope_poly,
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


def _two_unit_brief() -> DesignBrief:
    """2-unit duplex brief: 2 bed / 1 liv / 1 kit / 1 bath per unit."""
    unit_rooms = [
        BriefRoomSpec(role="bedroom",  count=2, storey_preference=1),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ]
    return DesignBrief(units=[
        BriefUnit(unit_id=1, rooms=unit_rooms),
        BriefUnit(unit_id=2, rooms=unit_rooms),
    ])


# ---------------------------------------------------------------------------
# Core acceptance test (from prompt spec)
# ---------------------------------------------------------------------------

def test_2unit_10x12_2floors_areas_within_bounds():
    """Every room area is in [OBC_min, OBC_max] and Σ areas ≤ GFA budget."""
    brief    = _two_unit_brief()
    envelope = _make_envelope(10.0, 12.0)
    program, warnings = generate_space_program(brief, envelope, target_floors=2)

    gfa_budget = 10.0 * 12.0 * 2 * 0.82  # 196.8 m²

    total_area = sum(r.target_area_m2 for r in program.rooms)
    assert total_area <= gfa_budget + 0.05, (
        f"Σ areas {total_area:.2f} m² exceeds GFA budget {gfa_budget:.2f} m²"
    )

    for room in program.rooms:
        obc_min = ROOM_MIN_AREA_M2.get(room.role, 0.0)
        obc_max = ROOM_MAX_AREA_M2.get(room.role, float("inf"))
        assert room.target_area_m2 >= obc_min - 0.01, (
            f"{room.id} ({room.role}): {room.target_area_m2:.2f} < OBC min {obc_min}"
        )
        assert room.target_area_m2 <= obc_max + 0.01, (
            f"{room.id} ({room.role}): {room.target_area_m2:.2f} > OBC max {obc_max}"
        )


def test_2unit_10x12_has_shared_stair():
    """Multi-storey build auto-adds one shared stair when brief omits it."""
    program, _ = generate_space_program(_two_unit_brief(), _make_envelope(10.0, 12.0), 2)
    stairs = [r for r in program.rooms if r.role == "stair"]
    assert len(stairs) == 1
    assert stairs[0].unit_id == -1        # shared


def test_2unit_10x12_room_count():
    """Expect 2×5 brief rooms + 1 auto-stair = 11 ProgramRooms."""
    program, _ = generate_space_program(_two_unit_brief(), _make_envelope(10.0, 12.0), 2)
    # 2 units × (2 bed + 1 liv + 1 kit + 1 bath) = 10 + 1 stair
    assert len(program.rooms) == 11


def test_2unit_10x12_no_warnings():
    """10×12 envelope is large enough — no feasibility warnings."""
    _, warnings = generate_space_program(_two_unit_brief(), _make_envelope(10.0, 12.0), 2)
    assert warnings == []


# ---------------------------------------------------------------------------
# Infeasibility warning
# ---------------------------------------------------------------------------

def test_infeasible_brief_returns_warnings():
    """Tiny lot with many bedrooms must produce a non-empty warnings list."""
    brief = DesignBrief(units=[
        BriefUnit(unit_id=1, rooms=[
            BriefRoomSpec(role="bedroom", count=5),
            BriefRoomSpec(role="living",  count=1),
            BriefRoomSpec(role="kitchen", count=1),
            BriefRoomSpec(role="bathroom", count=2),
        ]),
    ])
    # 3×4 = 12 m² footprint, 1 floor → GFA = 12 × 1 × 0.82 = 9.84 m²
    # Minimum required: 5×7 + 13.5 + 4.5 + 2×3 = 35 + 13.5 + 4.5 + 6 = 59 m²  >> 9.84
    envelope = _make_envelope(3.0, 4.0)
    _, warnings = generate_space_program(brief, envelope, target_floors=1)
    assert len(warnings) > 0
    assert "m² minimum" in warnings[0]


# ---------------------------------------------------------------------------
# Metadata flags
# ---------------------------------------------------------------------------

def test_wet_rooms_flagged():
    brief = DesignBrief(units=[
        BriefUnit(unit_id=1, rooms=[
            BriefRoomSpec(role="kitchen", count=1),
            BriefRoomSpec(role="bathroom", count=1),
            BriefRoomSpec(role="bedroom", count=1, storey_preference=1),
        ]),
    ])
    program, _ = generate_space_program(brief, _make_envelope(8.0, 10.0), 2)
    wet_roles = {r.role for r in program.rooms if r.wet}
    assert "kitchen" in wet_roles
    assert "bathroom" in wet_roles
    bedroom = next(r for r in program.rooms if r.role == "bedroom")
    assert bedroom.wet is False


def test_exterior_required_rooms():
    brief = DesignBrief(units=[
        BriefUnit(unit_id=1, rooms=[
            BriefRoomSpec(role="bedroom", count=1, storey_preference=1),
            BriefRoomSpec(role="living",  count=1),
            BriefRoomSpec(role="kitchen", count=1),
            BriefRoomSpec(role="bathroom", count=1),
        ]),
    ])
    program, _ = generate_space_program(brief, _make_envelope(8.0, 10.0), 2)
    for room in program.rooms:
        if room.role in ("bedroom", "master_bedroom", "living", "dining", "balcony"):
            assert room.exterior_required is True, f"{room.role} should require exterior"
        if room.role in ("kitchen", "bathroom", "stair", "corridor"):
            assert room.exterior_required is False


def test_zone_class_assignment():
    brief = DesignBrief(units=[
        BriefUnit(unit_id=1, rooms=[
            BriefRoomSpec(role="living",  count=1),
            BriefRoomSpec(role="bedroom", count=1, storey_preference=1),
            BriefRoomSpec(role="kitchen", count=1),
            BriefRoomSpec(role="bathroom", count=1),
        ]),
    ])
    program, _ = generate_space_program(brief, _make_envelope(8.0, 10.0), 2)
    by_role = {r.role: r for r in program.rooms}
    assert by_role["living"].zone_class   == "public"
    assert by_role["bedroom"].zone_class  == "private"
    assert by_role["kitchen"].zone_class  == "service"
    assert by_role["bathroom"].zone_class == "private"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic():
    """Same inputs → identical output on two successive calls."""
    brief    = _two_unit_brief()
    envelope = _make_envelope(10.0, 12.0)
    prog1, w1 = generate_space_program(brief, envelope, 2)
    prog2, w2 = generate_space_program(brief, envelope, 2)
    assert w1 == w2
    assert len(prog1.rooms) == len(prog2.rooms)
    for r1, r2 in zip(prog1.rooms, prog2.rooms):
        assert r1.id == r2.id
        assert r1.target_area_m2 == r2.target_area_m2


# ---------------------------------------------------------------------------
# SpaceProgram helpers
# ---------------------------------------------------------------------------

def test_total_area_by_storey():
    program, _ = generate_space_program(_two_unit_brief(), _make_envelope(10.0, 12.0), 2)
    by_storey = program.total_area_by_storey()
    assert 0 in by_storey
    assert 1 in by_storey
    assert by_storey[0] > 0
    assert by_storey[1] > 0
