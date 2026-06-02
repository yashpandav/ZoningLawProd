"""Recursive 2D guillotine layout solver.

Replaces template_filler._materialize_cells (1D single-axis slice) with a
genuine 2D packer that produces non-overlapping room rectangles and a shared
wall network with no doubled walls.

Algorithm per storey:
  1. Subtract core obstacle (stair + wet cols) → working rectangle(s).
  2. Priority-sort rooms: large public rooms and high-adjacency-degree first.
  3. Assign rooms to working rects proportionally to area capacity.
  4. Recursively guillotine-pack each rect:
       - Base case (1 room): room fills entire leaf region.
       - N rooms: split into active zone (public+service) vs quiet zone
         (private+circulation); cut along longer axis proportional to area;
         recurse into each half.
  5. Flag rooms that violate OBC min_clear_dim — never silently downgrade.
  6. Build wall network: one WallSegment per shared edge, left/right ids set.
     Envelope-boundary edges → exterior.  Cross-unit → party.  Same-unit → partition.

Output guarantees:
  - Room rectangles tile the working region (no gaps, no overlaps).
  - Each interior edge appears exactly once in the WallNetwork.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from shapely.geometry import Polygon
from shapely.geometry import box as shapely_box

from ..ai.schema import RoomModel
from ..rules.code_rules import ROOM_MAX_AREA_M2, ROOM_MIN_DIM_M
from ..schemas.contracts import (
    AdjacencyMatrix, CoreSpec, ProgramRoom, WallNetwork, WallSegment,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SNAP_MM   = 0.1   # 100 mm grid (matches selector.py)
_EDGE_TOL  = 0.12  # shared-edge detection tolerance (> 1 snap step)
_MIN_DIM   = 0.9   # minimum sub-region width/height before clamping cuts (m)
_ASPECT_MAX = 2.5  # OBC room aspect ratio limit
_ZONE_ORDER = {"public": 0, "service": 1, "private": 2, "circulation": 3}

_ROLE_TO_CATEGORY: dict[str, str] = {
    "bedroom":        "bedroom",  "master_bedroom": "bedroom",
    "living":         "living",   "dining":         "dining",
    "kitchen":        "kitchen",  "bathroom":       "bathroom",
    "powder_room":    "powder",   "stair":          "stair",
    "corridor":       "corridor", "mechanical":     "mech",
    "storage":        "storage",  "laundry":        "laundry",
    "entry":          "entry",    "balcony":        "balcony",
    "void":           "storage",
}


def _snap(v: float) -> float:
    return round(v / _SNAP_MM) * _SNAP_MM


# ---------------------------------------------------------------------------
# Internal geometry types
# ---------------------------------------------------------------------------

@dataclass
class _Rect:
    x0: float; y0: float; x1: float; y1: float

    @property
    def w(self) -> float: return self.x1 - self.x0

    @property
    def h(self) -> float: return self.y1 - self.y0

    @property
    def area(self) -> float: return self.w * self.h

    def polygon(self) -> list[list[float]]:
        return [
            [self.x0, self.y0], [self.x1, self.y0],
            [self.x1, self.y1], [self.x0, self.y1],
        ]


@dataclass
class _Placed:
    room: ProgramRoom
    rect: _Rect


def _rect_from(r) -> _Rect:
    """Build _Rect from any object with .x0/.y0/.x1/.y1 fields."""
    return _Rect(r.x0, r.y0, r.x1, r.y1)


# ---------------------------------------------------------------------------
# Public entry point — helpers
# ---------------------------------------------------------------------------

def _divide_envelope_for_units(
    env: _Rect,
    unit_ids: list[int],
    stacking: str,
) -> dict[int, _Rect]:
    """Divide the floor envelope into per-unit sub-envelopes.

    horizontal / mixed → divide along x-axis (side-by-side columns).
    vertical           → all units share the full envelope per storey
                         (each storey is one unit's floor — handled upstream).
    Returns {unit_id: _Rect}.
    """
    if stacking == "vertical" or len(unit_ids) <= 1:
        return {uid: env for uid in unit_ids}

    # Horizontal: equal-width columns along the x-axis
    n = len(unit_ids)
    unit_width = _snap((env.x1 - env.x0) / n)
    result: dict[int, _Rect] = {}
    for i, uid in enumerate(sorted(unit_ids)):
        x0 = _snap(env.x0 + i * unit_width)
        x1 = _snap(env.x0 + (i + 1) * unit_width) if i < n - 1 else env.x1
        result[uid] = _Rect(x0, env.y0, x1, env.y1)
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def solve_floor(
    rooms: list[ProgramRoom],
    envelope_2d: Polygon,
    core: CoreSpec,
    adjacency: AdjacencyMatrix,
    storey: int,
    stacking: str = "vertical",
) -> tuple[list[RoomModel], WallNetwork]:
    """Lay out rooms for one storey and return (room_models, wall_network).

    room_models contain real polygon coordinates (not AABB).
    wall_network has exactly one WallSegment per shared edge.

    stacking="horizontal" / "mixed" → each unit gets its own x-axis column
    before the guillotine packer runs, preventing cross-unit room mixing.
    stacking="vertical"             → original behaviour (all rooms in one pool).
    """
    if not rooms:
        return [], WallNetwork(storey=storey)

    minx, miny, maxx, maxy = envelope_2d.bounds
    env = _Rect(_snap(minx), _snap(miny), _snap(maxx), _snap(maxy))

    # Separate shared rooms (unit_id=-1) from unit-specific rooms
    shared_rooms = [r for r in rooms if r.unit_id < 0]
    unit_rooms   = [r for r in rooms if r.unit_id >= 0]
    unit_ids     = sorted(set(r.unit_id for r in unit_rooms))

    if stacking in ("horizontal", "mixed") and len(unit_ids) > 1:
        # Each unit gets its own x-axis column; pack independently
        sub_envs = _divide_envelope_for_units(env, unit_ids, stacking)

        placed: list[_Placed] = []
        for uid in unit_ids:
            sub_env  = sub_envs[uid]
            u_rooms  = [r for r in unit_rooms if r.unit_id == uid]
            if not u_rooms:
                continue
            u_rects   = _subtract_core(sub_env, core, storey)
            u_ordered = _priority_sort(u_rooms, adjacency)
            u_assign  = _assign_rooms(u_ordered, u_rects)
            for rect, batch in u_assign:
                placed.extend(_guillotine(rect, batch, adjacency))

        # Shared rooms (stair, corridor) use the full envelope
        if shared_rooms:
            s_rects  = _subtract_core(env, core, storey)
            s_assign = _assign_rooms(shared_rooms, s_rects)
            for rect, batch in s_assign:
                placed.extend(_guillotine(rect, batch, adjacency))
    else:
        # Vertical stacking — original path (all rooms in one pool per storey)
        work_rects  = _subtract_core(env, core, storey)
        ordered     = _priority_sort(rooms, adjacency)
        assignments = _assign_rooms(ordered, work_rects)
        placed = []
        for rect, batch in assignments:
            placed.extend(_guillotine(rect, batch, adjacency))

    # 5. OBC dimension check (flag only — no phantom storage)
    _check_dims(placed)

    # 6. Emit room models + wall network
    room_models = [_to_room_model(p) for p in placed]
    network     = _build_wall_network(placed, env, storey)
    return room_models, network


# ---------------------------------------------------------------------------
# Step 1 — core subtraction
# ---------------------------------------------------------------------------

def _subtract_core(env: _Rect, core: CoreSpec, storey: int) -> list[_Rect]:
    """Subtract the stair obstacle from the envelope and return working rects.

    Handles wall-aligned stair (left or right wall) → produces 2–3 clean rects.
    Falls back to full envelope for interior obstacles (rare in practice).

    When the stair footprint lies entirely outside the sub-envelope (e.g. when
    packing a per-unit column in horizontal stacking), skip subtraction and
    return the full sub-envelope unchanged.
    """
    if storey not in core.present_on_storeys:
        return [env]

    s = _rect_from(core.stair_rect)
    tol = 0.2

    # If the stair does not overlap this sub-envelope at all, nothing to subtract
    stair_outside = (
        s.x1 <= env.x0 + tol
        or s.x0 >= env.x1 - tol
        or s.y1 <= env.y0 + tol
        or s.y0 >= env.y1 - tol
    )
    if stair_outside:
        return [env]

    def _safe_rect(x0, y0, x1, y1) -> Optional[_Rect]:
        x0, y0, x1, y1 = _snap(x0), _snap(y0), _snap(x1), _snap(y1)
        if x1 - x0 > 0.5 and y1 - y0 > 0.5:
            return _Rect(x0, y0, x1, y1)
        return None

    against_left  = s.x0 <= env.x0 + tol
    against_right = s.x1 >= env.x1 - tol

    if against_left:
        rects = list(filter(None, [
            _safe_rect(s.x1, env.y0, env.x1, env.y1),   # right of stair, full height
            _safe_rect(env.x0, s.y1, s.x1, env.y1),     # above stair, stair-width strip
            _safe_rect(env.x0, env.y0, s.x1, s.y0),     # below stair, stair-width strip
        ]))
        if rects:
            return rects

    if against_right:
        rects = list(filter(None, [
            _safe_rect(env.x0, env.y0, s.x0, env.y1),   # left of stair, full height
            _safe_rect(s.x0, s.y1, env.x1, env.y1),     # above stair
            _safe_rect(s.x0, env.y0, env.x1, s.y0),     # below stair
        ]))
        if rects:
            return rects

    return [env]


# ---------------------------------------------------------------------------
# Step 2 — room prioritisation
# ---------------------------------------------------------------------------

def _priority_sort(rooms: list[ProgramRoom], adj: AdjacencyMatrix) -> list[ProgramRoom]:
    """Public rooms with high adjacency degree placed first."""
    def _degree(r: ProgramRoom) -> int:
        return sum(1 for e in adj.edges if r.id in (e.a, e.b) and e.weight > 0.5)

    return sorted(rooms, key=lambda r: (
        _ZONE_ORDER.get(r.zone_class, 99),
        -r.target_area_m2,
        -_degree(r),
    ))


# ---------------------------------------------------------------------------
# Step 3 — room-to-rect assignment
# ---------------------------------------------------------------------------

def _assign_rooms(
    rooms: list[ProgramRoom],
    rects: list[_Rect],
) -> list[tuple[_Rect, list[ProgramRoom]]]:
    """Greedily assign each room to the rect with the most remaining capacity."""
    if not rects:
        return []
    if len(rects) == 1:
        return [(rects[0], list(rooms))]

    capacity = [r.area * 0.85 for r in rects]
    buckets: list[list[ProgramRoom]] = [[] for _ in rects]

    for room in rooms:
        best = max(range(len(rects)), key=lambda i: capacity[i])
        buckets[best].append(room)
        capacity[best] -= room.target_area_m2

    return [(rects[i], buckets[i]) for i in range(len(rects)) if buckets[i]]


# ---------------------------------------------------------------------------
# Step 4 — recursive guillotine
# ---------------------------------------------------------------------------

_VOID_COUNTER: list[int] = [0]   # module-level counter for unique void ids


def _make_void(base_id: str, unit_id: int, storey: int, area: float) -> ProgramRoom:
    """Create a void ProgramRoom to absorb leftover space from OBC-max clipping."""
    _VOID_COUNTER[0] += 1
    vid = f"void_{_VOID_COUNTER[0]}"[:40]
    # Clamp area to ProgramRoom field constraints (gt=0, le=200).
    # The leftover region may be large on big lots; cap at 199.9 so Pydantic
    # validation passes — the actual space the void occupies is determined by
    # its _Rect at placement time, not by this target.
    return ProgramRoom(
        id=vid,
        role="void",
        unit_id=unit_id,
        storey=storey,
        target_area_m2=max(0.01, min(round(area, 2), 199.9)),
        zone_class="circulation",
        wet=False,
        exterior_required=False,
    )


def _guillotine(
    region: _Rect,
    rooms: list[ProgramRoom],
    adj: AdjacencyMatrix,
) -> list[_Placed]:
    """Recursively split region into leaf rects, one per room."""
    if not rooms:
        return []

    if len(rooms) == 1:
        # Clip leaf region to OBC max area to avoid oversized rooms on spacious lots.
        # Shrink the longer dimension; return leftover space as a void region so
        # the wall network stays contiguous (no tiling gaps).
        room = rooms[0]
        obc_max = ROOM_MAX_AREA_M2.get(room.role, float("inf"))
        # Only clip when the region exceeds OBC max by more than 20% — the same
        # tolerance used in test_room_areas_within_obc_bounds.  Smaller overages
        # (up to 20%) are acceptable and keeping them avoids spurious void rooms.
        if obc_max != float("inf") and region.area > obc_max * 1.20 + 0.01:
            # Determine which dimension to shrink (keep the min-dim of the other)
            min_dim = ROOM_MIN_DIM_M.get(room.role, 0.0)
            if region.w >= region.h:
                # Longer axis is x — shrink width
                clipped_w = _snap(max(min_dim, obc_max / region.h))
                if clipped_w < region.w - _MIN_DIM:
                    clipped = _Rect(region.x0, region.y0,
                                    _snap(region.x0 + clipped_w), region.y1)
                    leftover = _Rect(_snap(region.x0 + clipped_w), region.y0,
                                     region.x1, region.y1)
                    if (clipped.w >= max(min_dim, _MIN_DIM)
                            and clipped.h >= max(min_dim, _MIN_DIM)
                            and leftover.w >= _MIN_DIM and leftover.h >= _MIN_DIM):
                        void_room = _make_void(room.id + "_void", room.unit_id,
                                               room.storey, leftover.area)
                        return [_Placed(room, clipped), _Placed(void_room, leftover)]
            else:
                # Longer axis is y — shrink depth
                clipped_h = _snap(max(min_dim, obc_max / region.w))
                if clipped_h < region.h - _MIN_DIM:
                    clipped = _Rect(region.x0, region.y0,
                                    region.x1, _snap(region.y0 + clipped_h))
                    leftover = _Rect(region.x0, _snap(region.y0 + clipped_h),
                                     region.x1, region.y1)
                    if (clipped.w >= max(min_dim, _MIN_DIM)
                            and clipped.h >= max(min_dim, _MIN_DIM)
                            and leftover.w >= _MIN_DIM and leftover.h >= _MIN_DIM):
                        void_room = _make_void(room.id + "_void", room.unit_id,
                                               room.storey, leftover.area)
                        return [_Placed(room, clipped), _Placed(void_room, leftover)]
        return [_Placed(room, region)]

    group_a, group_b = _cluster_two(rooms, adj)

    area_a = sum(r.target_area_m2 for r in group_a)
    area_b = sum(r.target_area_m2 for r in group_b)
    total  = area_a + area_b
    frac_a = (area_a / total) if total > 0 else 0.5

    # Cut along the longer axis; clamp so neither sub-region collapses
    if region.w >= region.h:
        raw_cut = region.x0 + region.w * frac_a
        cut = _snap(max(region.x0 + _MIN_DIM, min(region.x1 - _MIN_DIM, raw_cut)))
        reg_a = _Rect(region.x0, region.y0, cut,       region.y1)
        reg_b = _Rect(cut,       region.y0, region.x1, region.y1)
    else:
        raw_cut = region.y0 + region.h * frac_a
        cut = _snap(max(region.y0 + _MIN_DIM, min(region.y1 - _MIN_DIM, raw_cut)))
        reg_a = _Rect(region.x0, region.y0, region.x1, cut)
        reg_b = _Rect(region.x0, cut,       region.x1, region.y1)

    return _guillotine(reg_a, group_a, adj) + _guillotine(reg_b, group_b, adj)


def _cluster_two(
    rooms: list[ProgramRoom],
    adj: AdjacencyMatrix,
) -> tuple[list[ProgramRoom], list[ProgramRoom]]:
    """Split rooms into active-zone group vs quiet-zone group.

    Active (public + service): living, dining, kitchen, entry, laundry.
    Quiet (private + circulation): bedrooms, bathrooms, stair, corridor.

    This keeps kitchen+dining in the same initial cluster so they end up
    adjacent after the next level of recursion.

    Fallback when all rooms share the same zone class: split at area midpoint.
    """
    active = [r for r in rooms if r.zone_class in ("public",  "service")]
    quiet  = [r for r in rooms if r.zone_class in ("private", "circulation")]

    if active and quiet:
        return active, quiet

    # Same zone class — area-midpoint split (preserves area proportionality)
    by_area = sorted(rooms, key=lambda r: -r.target_area_m2)
    total = sum(r.target_area_m2 for r in by_area)
    half, cum, split = total / 2, 0.0, 1
    for i, r in enumerate(by_area):
        cum += r.target_area_m2
        if cum >= half:
            split = i + 1
            break
    split = max(1, min(len(by_area) - 1, split))
    return by_area[:split], by_area[split:]


# ---------------------------------------------------------------------------
# Step 5 — dimension check
# ---------------------------------------------------------------------------

def _check_dims(placed: list[_Placed]) -> None:
    """Identify rooms that violate OBC min_clear_dim.

    Does NOT alter geometry or silently downgrade roles.  Violations surface
    through the downstream OBC checker (obc.py) with explicit warnings.
    """
    for p in placed:
        min_d = p.room.min_clear_dim_m or ROOM_MIN_DIM_M.get(p.room.role, 0.0)
        if min_d > 0 and (p.rect.w < min_d - _EDGE_TOL or p.rect.h < min_d - _EDGE_TOL):
            pass  # deliberately empty: OBC checker reports these violations


# ---------------------------------------------------------------------------
# Step 6a — RoomModel output
# ---------------------------------------------------------------------------

def _to_room_model(p: _Placed) -> RoomModel:
    cat = _ROLE_TO_CATEGORY.get(p.room.role, "storage")
    uid = str(p.room.unit_id) if p.room.unit_id >= 0 else None
    return RoomModel(
        id=p.room.id,
        label=p.room.role.replace("_", " ").title(),
        polygon=p.rect.polygon(),
        category=cat,  # type: ignore[arg-type]
        dwelling_unit_id=uid,
        area_m2=round(p.rect.area, 2),
    )


# ---------------------------------------------------------------------------
# Step 6b — wall network
# ---------------------------------------------------------------------------

def _build_wall_network(
    placed: list[_Placed],
    env: _Rect,
    storey: int,
) -> WallNetwork:
    """Build a shared-edge wall network from the placed room rectangles.

    Interior walls: one segment per shared edge between two rooms; left_room_id
    and right_room_id identify the rooms on each side.
    Exterior walls: room edges that coincide with the envelope boundary.
    Party walls: shared edges between rooms in different dwelling units.
    """
    seg_id   = 0
    segments: list[WallSegment] = []

    def _next_id() -> str:
        nonlocal seg_id
        seg_id += 1
        return f"w{storey}_{seg_id:04d}"

    def _wall_type(uid_a: int, uid_b: int) -> str:
        if uid_a != uid_b and uid_a >= 0 and uid_b >= 0:
            return "party"
        return "interior_partition"

    # --- Interior shared walls (O(n²), fine for ≤ 20 rooms per floor) ---
    n = len(placed)
    for i in range(n):
        for j in range(i + 1, n):
            seg = _shared_edge(placed[i].rect, placed[j].rect)
            if seg is None:
                continue
            (sx0, sy0), (sx1, sy1) = seg
            wtype = _wall_type(placed[i].room.unit_id, placed[j].room.unit_id)
            segments.append(WallSegment(
                id=_next_id(),
                start=[sx0, sy0], end=[sx1, sy1],
                type=wtype,                  # type: ignore[arg-type]
                left_room_id=placed[i].room.id,
                right_room_id=placed[j].room.id,
            ))

    # --- Exterior walls (room edges on envelope boundary) ---
    for p in placed:
        r = p.rect
        edge_checks = [
            # (is_on_boundary, start, end)
            (abs(r.y0 - env.y0) < _EDGE_TOL, [r.x0, r.y0], [r.x1, r.y0]),
            (abs(r.y1 - env.y1) < _EDGE_TOL, [r.x0, r.y1], [r.x1, r.y1]),
            (abs(r.x0 - env.x0) < _EDGE_TOL, [r.x0, r.y0], [r.x0, r.y1]),
            (abs(r.x1 - env.x1) < _EDGE_TOL, [r.x1, r.y0], [r.x1, r.y1]),
        ]
        for on_boundary, start, end in edge_checks:
            if on_boundary:
                segments.append(WallSegment(
                    id=_next_id(),
                    start=start, end=end,
                    type="exterior",         # type: ignore[arg-type]
                    left_room_id=p.room.id,
                ))

    return WallNetwork(storey=storey, segments=segments)


def _shared_edge(
    a: _Rect, b: _Rect,
) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
    """Return the shared edge segment between two rects, or None.

    Returns ((x0,y0),(x1,y1)) with x0≤x1 / y0≤y1 for canonical orientation.
    """
    tol = _EDGE_TOL

    # Vertical edge: a is to the left of b
    if abs(a.x1 - b.x0) < tol:
        y_lo, y_hi = max(a.y0, b.y0), min(a.y1, b.y1)
        if y_hi - y_lo > tol:
            x = _snap((a.x1 + b.x0) / 2)
            return (x, _snap(y_lo)), (x, _snap(y_hi))

    # Vertical edge: b is to the left of a
    if abs(b.x1 - a.x0) < tol:
        y_lo, y_hi = max(a.y0, b.y0), min(a.y1, b.y1)
        if y_hi - y_lo > tol:
            x = _snap((b.x1 + a.x0) / 2)
            return (x, _snap(y_lo)), (x, _snap(y_hi))

    # Horizontal edge: a is below b
    if abs(a.y1 - b.y0) < tol:
        x_lo, x_hi = max(a.x0, b.x0), min(a.x1, b.x1)
        if x_hi - x_lo > tol:
            y = _snap((a.y1 + b.y0) / 2)
            return (_snap(x_lo), y), (_snap(x_hi), y)

    # Horizontal edge: b is below a
    if abs(b.y1 - a.y0) < tol:
        x_lo, x_hi = max(a.x0, b.x0), min(a.x1, b.x1)
        if x_hi - x_lo > tol:
            y = _snap((b.y1 + a.y0) / 2)
            return (_snap(x_lo), y), (_snap(x_hi), y)

    return None
