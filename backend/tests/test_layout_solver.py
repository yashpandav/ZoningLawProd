"""Tests for solve_floor — deterministic 2D guillotine layout solver."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from shapely.geometry import box as shapely_box, Polygon
from shapely.ops import unary_union

from packgen.solver.layout_solver import solve_floor, _guillotine, _Rect, _cluster_two
from packgen.schemas.contracts import (
    AdjacencyEdge, AdjacencyMatrix, BriefUnit, BriefRoomSpec,
    CoreSpec, DesignBrief, ProgramRoom, Rect, SpaceProgram,
)
from packgen.adjacency.graph_builder import build_adjacency, DEFAULT_WEIGHTS
from packgen.program.space_program import generate_space_program
from packgen.geometry import EnvelopeResult
from packgen.stacking.core_solver import solve_core


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env_result(w: float = 10.0, d: float = 10.0) -> EnvelopeResult:
    poly = shapely_box(0.0, 0.0, w, d)
    return EnvelopeResult(
        envelope_2d=poly, lot_local=poly,
        setback_lines={}, setbacks_applied={},
        lot_width_m=w, lot_depth_m=d, lot_area_m2=w * d,
        rotation_deg=0.0, origin_mtm=(0.0, 0.0),
        angular_plane_applied=False, depth_limit_m=17.0, warnings=[],
    )


def _empty_core(present_on: list[int] = None) -> CoreSpec:
    """CoreSpec that is not present on storey 0 (obstacles not applied)."""
    return CoreSpec(
        stair_rect=Rect(x0=0.0, y0=0.0, x1=1.0, y1=1.0),
        wet_columns=[],
        present_on_storeys=present_on or [1, 2, 3],   # NOT storey 0
    )


def _empty_adjacency() -> AdjacencyMatrix:
    return AdjacencyMatrix(edges=[])


def _make_rooms(roles_areas: list[tuple[str, float, str]]) -> list[ProgramRoom]:
    """Build ProgramRooms from (role, area_m2, zone_class) triples."""
    rooms = []
    for i, (role, area, zone) in enumerate(roles_areas):
        rooms.append(ProgramRoom(
            id=f"{role}_{i}",
            role=role,           # type: ignore[arg-type]
            unit_id=0,
            storey=0,
            target_area_m2=area,
            zone_class=zone,     # type: ignore[arg-type]
        ))
    return rooms


def _total_area(rooms) -> float:
    return sum(r.area_m2 or 0 for r in rooms)


def _shapely_rooms(room_models) -> list[Polygon]:
    return [shapely_box(
        min(v[0] for v in r.polygon), min(v[1] for v in r.polygon),
        max(v[0] for v in r.polygon), max(v[1] for v in r.polygon),
    ) for r in room_models]


# ---------------------------------------------------------------------------
# Acceptance test 1: 5 rooms tile the region (no core obstacles on storey 0)
# ---------------------------------------------------------------------------

def test_void_room_target_area_does_not_exceed_pydantic_limit():
    """OBC-max clipping on large lots must not produce void rooms with
    target_area_m2 > 200, which would fail ProgramRoom Pydantic validation."""
    from packgen.schemas.contracts import ProgramRoom
    # Huge envelope — bedroom region will be enormous, leftover area > 200 m²
    env = shapely_box(0.0, 0.0, 30.0, 30.0)   # 900 m²
    rooms = [_make_rooms([("bedroom", 11.0, "private")])[0]]
    core = _empty_core(present_on=[99])

    # Must NOT raise ProgramRoom validation error
    room_models, _ = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)

    # All room models must have finite, non-negative areas
    for rm in room_models:
        assert rm.area_m2 is not None and rm.area_m2 > 0, \
            f"Room {rm.id} has invalid area {rm.area_m2}"


def test_5_rooms_tile_region():
    """Room polygons must tile the working region with ≤1% gap."""
    rooms = _make_rooms([
        ("living",   20.0, "public"),
        ("dining",   12.0, "public"),
        ("kitchen",  10.0, "service"),
        ("bedroom",  11.0, "private"),
        ("bathroom",  5.0, "private"),
    ])
    env = shapely_box(0.0, 0.0, 10.0, 10.0)   # 100 m² region
    core = _empty_core(present_on=[99])         # no obstacles on storey 0

    room_models, _ = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)

    # OBC area-cap clipping may add void rooms to absorb trimmed space — exclude them
    non_void = [r for r in room_models if r.label.lower() != "void"]
    assert len(non_void) == 5

    total = sum(m.area_m2 for m in room_models)   # void included — region still tiles
    region_area = env.area
    assert abs(total - region_area) / region_area <= 0.01, (
        f"Area gap: placed={total:.2f} m², region={region_area:.2f} m²"
    )


def test_5_rooms_no_overlaps():
    """Placed room polygons must not overlap."""
    rooms = _make_rooms([
        ("living",   20.0, "public"),
        ("dining",   12.0, "public"),
        ("kitchen",  10.0, "service"),
        ("bedroom",  11.0, "private"),
        ("bathroom",  5.0, "private"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])

    room_models, _ = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)
    polys = _shapely_rooms(room_models)

    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            overlap = polys[i].intersection(polys[j]).area
            assert overlap < 0.02, (
                f"Rooms {i} and {j} overlap by {overlap:.4f} m²"
            )


def test_every_room_meets_min_clear_dim():
    """Every placed room must have min(width, height) ≥ its min_clear_dim."""
    rooms = _make_rooms([
        ("bedroom",  11.0, "private"),
        ("bedroom",  11.0, "private"),
        ("living",   18.0, "public"),
        ("kitchen",   9.0, "service"),
        ("bathroom",  4.0, "private"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])

    room_models, _ = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)

    from packgen.rules.code_rules import ROOM_MIN_DIM_M
    id_to_role = {r.id: r.role for r in rooms}

    for m in room_models:
        role = id_to_role.get(m.id, m.id.split("_")[0])
        min_d = ROOM_MIN_DIM_M.get(role, 0.0)
        if min_d == 0:
            continue
        w = max(v[0] for v in m.polygon) - min(v[0] for v in m.polygon)
        h = max(v[1] for v in m.polygon) - min(v[1] for v in m.polygon)
        assert min(w, h) >= min_d - 0.15, (
            f"{m.id} ({role}): min_dim={min(w,h):.2f} < {min_d}"
        )


# ---------------------------------------------------------------------------
# Acceptance test 2: wall network — exactly one segment per shared edge
# ---------------------------------------------------------------------------

def test_wall_network_no_duplicate_segments():
    """No two WallSegments may be collinear and overlapping duplicates."""
    rooms = _make_rooms([
        ("living",  20.0, "public"),
        ("kitchen", 10.0, "service"),
        ("bedroom", 10.0, "private"),
        ("bathroom", 5.0, "private"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])

    _, network = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)

    # Build canonical (start,end) pairs and check uniqueness
    seen: set[frozenset[tuple[float, float]]] = set()
    for seg in network.segments:
        pair = frozenset({tuple(seg.start), tuple(seg.end)})
        assert pair not in seen, (
            f"Duplicate wall segment: {seg.start} → {seg.end}"
        )
        seen.add(pair)


def test_interior_walls_have_both_room_ids():
    """Every interior_partition / party WallSegment must have left and right room ids."""
    rooms = _make_rooms([
        ("living",  20.0, "public"),
        ("kitchen", 10.0, "service"),
        ("bedroom", 10.0, "private"),
        ("bathroom", 5.0, "private"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])

    _, network = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)

    interior_types = {"interior_partition", "interior_loadbearing", "party"}
    for seg in network.segments:
        if seg.type in interior_types:
            assert seg.left_room_id  is not None, f"Interior seg {seg.id} missing left_room_id"
            assert seg.right_room_id is not None, f"Interior seg {seg.id} missing right_room_id"


def test_exterior_walls_present():
    """Exterior walls must be generated for every room touching the envelope boundary."""
    rooms = _make_rooms([
        ("living",  50.0, "public"),
        ("bedroom", 50.0, "private"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])

    _, network = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)
    exterior = [s for s in network.segments if s.type == "exterior"]
    assert len(exterior) >= 4, "Expected ≥4 exterior wall segments for a 2-room plan"


# ---------------------------------------------------------------------------
# Acceptance test 3: kitchen–dining adjacency → they share a wall
# ---------------------------------------------------------------------------

def test_kitchen_dining_share_wall():
    """kitchen and dining (weight 1.0 pair) must share a wall segment."""
    # 2-room case: kitchen + dining → cluster keeps them together → adjacent rects
    rooms = _make_rooms([
        ("dining",  12.0, "public"),
        ("kitchen", 10.0, "service"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])

    adj = AdjacencyMatrix(edges=[
        AdjacencyEdge(a="dining_0", b="kitchen_1", weight=1.0, type="adjacent"),
    ])
    _, network = solve_floor(rooms, env, core, adj, storey=0)

    interior = [
        s for s in network.segments
        if s.type == "interior_partition"
        and {s.left_room_id, s.right_room_id} == {"dining_0", "kitchen_1"}
    ]
    assert len(interior) == 1, (
        "Expected exactly 1 shared wall between kitchen and dining"
    )


def test_kitchen_dining_adjacent_in_larger_plan():
    """kitchen + dining must share a wall even with bedrooms and a stair present."""
    rooms = _make_rooms([
        ("living",   20.0, "public"),
        ("dining",   12.0, "public"),
        ("kitchen",  10.0, "service"),
        ("bedroom",  11.0, "private"),
        ("bathroom",  5.0, "private"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])
    adj  = _empty_adjacency()

    _, network = solve_floor(rooms, env, core, adj, storey=0)

    kitchen_id = next(r.id for r in rooms if r.role == "kitchen")
    dining_id  = next(r.id for r in rooms if r.role == "dining")
    shared = [
        s for s in network.segments
        if {s.left_room_id, s.right_room_id} == {kitchen_id, dining_id}
    ]
    assert len(shared) == 1, (
        f"Expected 1 shared wall between kitchen and dining, got {len(shared)}"
    )


# ---------------------------------------------------------------------------
# Party walls between units
# ---------------------------------------------------------------------------

def test_cross_unit_shared_wall_is_party():
    """Shared edge between rooms of different units must be type='party'."""
    rooms = [
        ProgramRoom(id="liv_u0", role="living",  unit_id=0, storey=0, target_area_m2=50.0, zone_class="public"),
        ProgramRoom(id="liv_u1", role="living",  unit_id=1, storey=0, target_area_m2=50.0, zone_class="public"),
    ]
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])

    _, network = solve_floor(rooms, env, core, _empty_adjacency(), storey=0)
    party = [s for s in network.segments if s.type == "party"]
    assert len(party) >= 1, "Expected a party wall between unit 0 and unit 1"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_solve_floor_deterministic():
    """Same inputs must produce identical output on two calls."""
    rooms = _make_rooms([
        ("living",  20.0, "public"),
        ("kitchen", 10.0, "service"),
        ("bedroom", 11.0, "private"),
        ("bathroom", 5.0, "private"),
    ])
    env  = shapely_box(0.0, 0.0, 10.0, 10.0)
    core = _empty_core(present_on=[99])
    adj  = _empty_adjacency()

    models1, net1 = solve_floor(rooms, env, core, adj, storey=0)
    models2, net2 = solve_floor(rooms, env, core, adj, storey=0)

    assert len(models1) == len(models2)
    for m1, m2 in zip(models1, models2):
        assert m1.polygon == m2.polygon
    assert len(net1.segments) == len(net2.segments)


# ---------------------------------------------------------------------------
# Core obstacle is subtracted
# ---------------------------------------------------------------------------

def test_core_obstacle_reduces_working_area():
    """When the stair is present on the storey, room areas must be ≤ envelope area."""
    brief = DesignBrief(units=[BriefUnit(unit_id=1, rooms=[
        BriefRoomSpec(role="bedroom",  count=2, storey_preference=0),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ])])
    env_result = _make_env_result(10.0, 12.0)
    program, _ = generate_space_program(brief, env_result, target_floors=2)
    storey_rooms = [r for r in program.rooms if r.storey == 0]

    core = solve_core(program, env_result.envelope_2d, target_floors=2)
    adj  = build_adjacency(program, road_bearing_deg=None, allow_llm=False)

    room_models, network = solve_floor(
        storey_rooms, env_result.envelope_2d, core, adj, storey=0
    )

    total_placed = sum(m.area_m2 for m in room_models)
    env_area = env_result.envelope_2d.area
    # Rooms can't exceed envelope area (stair takes some)
    assert total_placed <= env_area + 0.5
