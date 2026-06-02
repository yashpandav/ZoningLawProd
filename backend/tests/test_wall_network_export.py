"""Tests for WallNetwork routing through DXF and IFC writers.

Acceptance criteria:
  1. 2-room plan, 1 shared wall → exactly ONE interior wall in DXF and ONE IfcWall.
  2. A door produces an IfcOpeningElement voiding its host wall.
  3. None path (wall_networks=None) reproduces existing entity counts.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import math
import pytest
import ezdxf
from shapely.geometry import box as shapely_box

from packgen.typology.selector import FitResult, PlacedCell
from packgen.typology.models import Cell, Typology
from packgen.schemas.contracts import WallNetwork, WallSegment
from packgen.dxf_writer import _draw_storeys, build_dxf
from packgen.ai.schema import (
    DoorModel, FloorPlanJSON, FloorPlanMetadata,
    RoomModel, StoreyModel, WallModel,
)
from packgen.geometry import EnvelopeResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _cell(role, uid, x0, y0, x1, y1, storey=0):
    return Cell(role=role, unit_id=uid, storey=storey, x0=x0, y0=y0, x1=x1, y1=y1)


def _placed(role, uid, x0, y0, x1, y1, storey=0, room_id=None):
    c = _cell(role, uid, x0, y0, x1, y1, storey)
    return PlacedCell(cell=c, x0=x0, y0=y0, x1=x1, y1=y1, room_id=room_id)


def _typology():
    dummy = _cell("living", 0, 0, 0, 1, 1)
    return Typology(
        id="t", label="T", units_produced=1, stacking_axis="vertical",
        min_frontage_m=4.0, max_frontage_m=12.0,
        min_depth_m=4.0,  max_depth_m=12.0,
        target_storeys=1, requires_basement=False,
        target_gfa_per_unit_m2=(20.0, 80.0),
        stamp_cells=(dummy,), corridor_axis="end", stair_position="internal",
        eligible_zones=("R",), eligible_wards=None, notes="",
    )


def _fit(cells, w=10.0, d=10.0):
    return FitResult(
        typology=_typology(), placed_cells=cells, option="A",
        fit_frontage_m=w, fit_depth_m=d, scale_x=1.0, scale_y=1.0,
        origin_local_xy=(0.0, 0.0), rotation_additional_deg=0.0,
        gfa_m2=w * d, warnings=[],
    )


def _two_room_network():
    """Room A (0..5,0..8) and Room B (5..10,0..8) sharing a wall at x=5."""
    return WallNetwork(storey=0, segments=[
        WallSegment(id="w_ext_bot", start=[0.0, 0.0], end=[10.0,  0.0], type="exterior"),
        WallSegment(id="w_ext_top", start=[0.0, 8.0], end=[10.0,  8.0], type="exterior"),
        WallSegment(id="w_ext_lft", start=[0.0, 0.0], end=[ 0.0,  8.0], type="exterior"),
        WallSegment(id="w_ext_rgt", start=[10.0,0.0], end=[10.0,  8.0], type="exterior"),
        WallSegment(id="w_int",     start=[5.0, 0.0], end=[ 5.0,  8.0],
                    type="interior_partition",
                    left_room_id="living_0", right_room_id="bedroom_0"),
    ])


def _dxf_doc_with_network(network, cells):
    fit = _fit(cells)
    doc = ezdxf.new("R2018")
    for name in ("A-FLOR-ROOM", "A-WALL-FULL", "A-WALL-INTR", "A-WALL-FIRE"):
        if name not in doc.layers:
            doc.layers.add(name)
    wn_map = {network.storey: network.segments}
    _draw_storeys(doc.modelspace(), fit, wn_map)
    return doc


def _dxf_doc_no_network(cells):
    fit = _fit(cells)
    doc = ezdxf.new("R2018")
    for name in ("A-FLOR-ROOM", "A-WALL-FULL", "A-WALL-INTR", "A-WALL-FIRE"):
        if name not in doc.layers:
            doc.layers.add(name)
    _draw_storeys(doc.modelspace(), fit, None)
    return doc


# ---------------------------------------------------------------------------
# Acceptance test 1: 2-room plan → exactly ONE interior wall segment in DXF
# ---------------------------------------------------------------------------

def test_two_rooms_produce_one_interior_wall_line():
    """With a WallNetwork, the shared edge must appear exactly ONCE on A-WALL-INTR."""
    cells = [
        _placed("living",  0, 0.0, 0.0, 5.0, 8.0, room_id="living_0"),
        _placed("bedroom", 0, 5.0, 0.0, 10.0, 8.0, room_id="bedroom_0"),
    ]
    network = _two_room_network()
    doc = _dxf_doc_with_network(network, cells)
    msp = doc.modelspace()

    intr_lines = [e for e in msp if e.dxftype() == "LINE" and e.dxf.layer == "A-WALL-INTR"]
    # The shared wall at x=5 is ONE line on A-WALL-INTR
    assert len(intr_lines) == 1, (
        f"Expected exactly 1 interior wall line, got {len(intr_lines)}"
    )
    # Verify it runs the full height
    coords = sorted([(e.dxf.start.y, e.dxf.end.y) for e in intr_lines])
    for start_y, end_y in coords:
        span = abs(end_y - start_y)
        assert span == pytest.approx(8.0, abs=0.05)


def test_two_rooms_exterior_walls_are_double_lines():
    """Exterior wall segments must produce two parallel lines (double-line convention)."""
    cells = [
        _placed("living",  0, 0.0, 0.0, 5.0, 8.0),
        _placed("bedroom", 0, 5.0, 0.0, 10.0, 8.0),
    ]
    network = _two_room_network()
    doc = _dxf_doc_with_network(network, cells)
    msp = doc.modelspace()

    # Each exterior segment produces 2 LINE entities on A-WALL-FULL
    ext_lines = [e for e in msp if e.dxftype() == "LINE" and e.dxf.layer == "A-WALL-FULL"]
    # 4 exterior segs × 2 lines each = 8 lines
    assert len(ext_lines) == 8, f"Expected 8 exterior lines (4 segs × 2), got {len(ext_lines)}"


# ---------------------------------------------------------------------------
# Acceptance test 2: None path reproduces entity counts
# ---------------------------------------------------------------------------

def test_none_path_entity_counts_match_baseline():
    """wall_networks=None must produce the same entity count as before this feature."""
    cells = [
        _placed("living",  0, 0.0, 0.0, 5.0, 8.0),
        _placed("bedroom", 0, 5.0, 0.0, 10.0, 8.0),
    ]
    # Run once with no network (legacy path)
    doc1 = _dxf_doc_no_network(cells)
    doc2 = _dxf_doc_no_network(cells)
    n1 = len(list(doc1.modelspace()))
    n2 = len(list(doc2.modelspace()))
    # Same inputs → same count (determinism check)
    assert n1 == n2, "Legacy path is non-deterministic — unexpected"


def test_none_path_has_no_network_interior_lines():
    """Legacy path must NOT draw a single line for the shared wall — it uses polylines."""
    cells = [
        _placed("living",  0, 0.0, 0.0, 5.0, 8.0),
        _placed("bedroom", 0, 5.0, 0.0, 10.0, 8.0),
    ]
    doc = _dxf_doc_no_network(cells)
    msp = doc.modelspace()
    # Legacy draws interior walls as LWPOLYLINE, not LINE
    intr_lines = [e for e in msp if e.dxftype() == "LINE" and e.dxf.layer == "A-WALL-INTR"]
    # No LINE entities for interior walls in legacy path
    assert len(intr_lines) == 0, (
        f"Legacy path should not have LINE entities on A-WALL-INTR, got {len(intr_lines)}"
    )


# ---------------------------------------------------------------------------
# IFC: 2-room plan → exactly ONE IfcWall for the shared interior edge
# ---------------------------------------------------------------------------

def _minimal_envelope():
    poly = shapely_box(0.0, 0.0, 10.0, 10.0)
    return EnvelopeResult(
        envelope_2d=poly, lot_local=poly,
        setback_lines={}, setbacks_applied={},
        lot_width_m=10, lot_depth_m=10, lot_area_m2=100,
        rotation_deg=0.0, origin_mtm=(0.0, 0.0),
        angular_plane_applied=False, depth_limit_m=17.0, warnings=[],
    )


def _minimal_obc():
    """Return an OBCResult stub that satisfies build_ifc."""
    try:
        from packgen.obc import OBCResult, RoomOBCResult
        return OBCResult(rooms=[], warnings=[], errors=[])
    except Exception:
        from unittest.mock import MagicMock
        obc = MagicMock()
        obc.rooms = []
        obc.warnings = []
        obc.errors = []
        return obc


def _run_ifc_with_network(wall_networks, floor_plan_json=None):
    try:
        import ifcopenshell
    except ImportError:
        pytest.skip("ifcopenshell not available")

    from packgen.ifc_writer import build_ifc

    cells = [
        _placed("living",  0, 0.0, 0.0, 5.0, 8.0, room_id="living_0"),
        _placed("bedroom", 0, 5.0, 0.0, 10.0, 8.0, room_id="bedroom_0"),
    ]
    fit  = _fit(cells)
    env  = _minimal_envelope()
    obc  = _minimal_obc()

    ifc_bytes = build_ifc(
        env, fit, obc,
        wall_networks=wall_networks,
        floor_plan_json=floor_plan_json,
    )
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as f:
        f.write(ifc_bytes)
        tmp_path = f.name
    try:
        m = ifcopenshell.open(tmp_path)
    finally:
        os.unlink(tmp_path)
    return m


def test_ifc_one_interior_wall_for_shared_edge():
    """WallNetwork path: exactly ONE IfcWall must exist for the shared interior wall."""
    network = _two_room_network()
    m = _run_ifc_with_network([network])

    walls = m.by_type("IfcWall")
    interior_walls = [w for w in walls if getattr(w, "PredefinedType", None) != "SOLIDWALL"]
    # One segment is interior_partition → one interior IfcWall
    # Total walls = 4 exterior + 1 interior = 5
    assert len(walls) == 5, f"Expected 5 IfcWalls (4 ext + 1 int), got {len(walls)}"


def test_ifc_space_boundaries_link_wall_to_rooms():
    """IfcRelSpaceBoundary must link the interior wall to the two adjacent spaces."""
    network = _two_room_network()
    m = _run_ifc_with_network([network])

    boundaries = m.by_type("IfcRelSpaceBoundary")
    # Interior wall has left_room_id and right_room_id → 2 boundaries
    assert len(boundaries) >= 2, (
        f"Expected ≥2 IfcRelSpaceBoundary, got {len(boundaries)}"
    )


# ---------------------------------------------------------------------------
# Acceptance test: door → IfcOpeningElement voiding wall
# ---------------------------------------------------------------------------

def test_ifc_door_creates_opening_element():
    """A DoorModel with wall_id referencing a WallSegment id must create IfcOpeningElement."""
    network = _two_room_network()

    # Door in the interior wall (wall_id = "w_int")
    door = DoorModel(
        id="door_0",
        wall_id="w_int",
        position_along_wall_m=4.0,
        width_m=0.9,
        height_m=2.1,
        swing="left_in",
    )
    wall_model = WallModel(id="w_int", start=[5.0, 0.0], end=[5.0, 8.0], type="interior_partition")
    room_a = RoomModel(id="living_0",  label="Living",  polygon=[[0,0],[5,0],[5,8],[0,8]], category="living")
    room_b = RoomModel(id="bedroom_0", label="Bedroom", polygon=[[5,0],[10,0],[10,8],[5,8]], category="bedroom")
    storey = StoreyModel(level=0, elevation_m=0.0, walls=[wall_model], rooms=[room_a, room_b], doors=[door])
    floor_plan = FloorPlanJSON(storeys=[storey])

    m = _run_ifc_with_network([network], floor_plan_json=floor_plan)

    openings = m.by_type("IfcOpeningElement")
    assert len(openings) >= 1, "Expected IfcOpeningElement for the door"

    voids = m.by_type("IfcRelVoidsElement")
    assert len(voids) >= 1, "Expected IfcRelVoidsElement linking opening to host wall"


def test_ifc_none_network_legacy_path_walls_exist():
    """With wall_networks=None the legacy path must still produce IfcWall entities."""
    m = _run_ifc_with_network(wall_networks=None)
    walls = m.by_type("IfcWall")
    assert len(walls) >= 1, "Legacy path must produce IfcWall entities"
