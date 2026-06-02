"""Tests: construct one valid instance of each contract model."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from pydantic import ValidationError

from packgen.schemas.contracts import (
    BriefRoomSpec, BriefUnit, ParkingSpec, DesignBrief,
    ProgramRoom, SpaceProgram,
    AdjacencyEdge, AdjacencyMatrix,
    Rect, CoreSpec, StructuralGrid,
    WallSegment, WallNetwork,
)
from packgen.rules.code_rules import ROOM_MIN_AREA_M2


# ---------------------------------------------------------------------------
# DesignBrief
# ---------------------------------------------------------------------------

def test_design_brief_minimal():
    brief = DesignBrief(
        units=[BriefUnit(unit_id=1, rooms=[
            BriefRoomSpec(role="bedroom", count=2, storey_preference=1),
            BriefRoomSpec(role="living",  count=1, storey_preference=0),
            BriefRoomSpec(role="kitchen", count=1, storey_preference=0),
            BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
        ])],
    )
    assert len(brief.units) == 1
    assert brief.stacking_pref == "vertical"
    assert brief.parking.count == 0


def test_design_brief_full():
    brief = DesignBrief(
        units=[
            BriefUnit(unit_id=1, rooms=[BriefRoomSpec(role="living", count=1)]),
            BriefUnit(unit_id=2, rooms=[BriefRoomSpec(role="living", count=1)]),
        ],
        parking=ParkingSpec(count=2, type="garage"),
        stacking_pref="horizontal",
        orientation_prefs=["living_south"],
        budget_tier="mid",
        notes="Client wants open plan.",
    )
    assert brief.parking.type == "garage"
    assert brief.budget_tier == "mid"


# ---------------------------------------------------------------------------
# ProgramRoom / SpaceProgram
# ---------------------------------------------------------------------------

def test_program_room_defaults_from_code_rules():
    room = ProgramRoom(
        id="br_u0_0",
        role="bedroom",
        unit_id=0,
        storey=1,
        target_area_m2=10.0,
        zone_class="private",
        exterior_required=True,
    )
    assert room.min_clear_dim_m == 2.1     # from code_rules ROOM_MIN_DIM_M
    assert room.wet is False


def test_program_room_below_obc_min_raises():
    floor = ROOM_MIN_AREA_M2["bedroom"]   # 7.0
    with pytest.raises(ValidationError, match="OBC minimum"):
        ProgramRoom(
            id="br_bad",
            role="bedroom",
            unit_id=0,
            storey=0,
            target_area_m2=floor - 1.0,  # too small
            zone_class="private",
        )


def test_space_program_total_area_by_storey():
    program = SpaceProgram(rooms=[
        ProgramRoom(id="r0", role="living", unit_id=0, storey=0, target_area_m2=16.0, zone_class="public"),
        ProgramRoom(id="r1", role="kitchen", unit_id=0, storey=0, target_area_m2=8.0, zone_class="service", wet=True),
        ProgramRoom(id="r2", role="bedroom", unit_id=0, storey=1, target_area_m2=10.0, zone_class="private"),
    ])
    by_storey = program.total_area_by_storey()
    assert by_storey[0] == pytest.approx(24.0)
    assert by_storey[1] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# AdjacencyMatrix
# ---------------------------------------------------------------------------

def test_adjacency_matrix_valid():
    matrix = AdjacencyMatrix(edges=[
        AdjacencyEdge(a="r0", b="r1", weight=1.0, type="adjacent"),
        AdjacencyEdge(a="r0", b="r2", weight=-1.0, type="separate"),
    ])
    assert matrix.weight("r0", "r1") == 1.0
    assert matrix.weight("r1", "r0") == 1.0   # symmetric lookup
    assert matrix.weight("r0", "r99") == 0.0  # undefined → 0


def test_adjacency_self_edge_raises():
    with pytest.raises(ValidationError):
        AdjacencyEdge(a="r0", b="r0", weight=1.0, type="adjacent")


def test_adjacency_duplicate_edge_raises():
    with pytest.raises(ValidationError, match="Duplicate"):
        AdjacencyMatrix(edges=[
            AdjacencyEdge(a="r0", b="r1", weight=0.8, type="adjacent"),
            AdjacencyEdge(a="r1", b="r0", weight=0.5, type="near"),  # same pair
        ])


# ---------------------------------------------------------------------------
# CoreSpec
# ---------------------------------------------------------------------------

def test_core_spec_valid():
    core = CoreSpec(
        stair_rect=Rect(x0=4.0, y0=2.0, x1=6.0, y1=5.0),
        wet_columns=[Rect(x0=0.0, y0=3.0, x1=1.0, y1=5.0)],
        present_on_storeys=[0, 1, 2],
    )
    assert core.stair_rect.area_m2 == pytest.approx(6.0)
    assert core.present_on_storeys == [0, 1, 2]  # sorted de-duped


def test_rect_inverted_raises():
    with pytest.raises(ValidationError):
        Rect(x0=5.0, y0=0.0, x1=3.0, y1=4.0)   # x1 < x0


# ---------------------------------------------------------------------------
# StructuralGrid
# ---------------------------------------------------------------------------

def test_structural_grid_lines():
    grid = StructuralGrid(spacing_m=3.0, offset_m=0.0, axis="x")
    lines = grid.grid_lines(envelope_span_m=9.0)
    assert lines == [0.0, 3.0, 6.0, 9.0]


# ---------------------------------------------------------------------------
# WallNetwork
# ---------------------------------------------------------------------------

def test_wall_network_valid():
    network = WallNetwork(
        storey=0,
        segments=[
            WallSegment(id="w0", start=[0.0, 0.0], end=[6.0, 0.0], type="exterior"),
            WallSegment(id="w1", start=[6.0, 0.0], end=[6.0, 8.0], type="exterior"),
            WallSegment(id="w2", start=[3.0, 0.0], end=[3.0, 8.0],
                        type="interior_partition", left_room_id="r0", right_room_id="r1"),
        ],
    )
    assert len(network.exterior_segments()) == 2
    assert len(network.segments_for_room("r0")) == 1


def test_wall_segment_zero_length_raises():
    with pytest.raises(ValidationError, match="zero length"):
        WallSegment(id="w_bad", start=[1.0, 1.0], end=[1.0, 1.0], type="interior_partition")


def test_wall_network_duplicate_id_raises():
    with pytest.raises(ValidationError, match="Duplicate"):
        WallNetwork(storey=0, segments=[
            WallSegment(id="w0", start=[0.0, 0.0], end=[3.0, 0.0], type="exterior"),
            WallSegment(id="w0", start=[3.0, 0.0], end=[6.0, 0.0], type="exterior"),
        ])
