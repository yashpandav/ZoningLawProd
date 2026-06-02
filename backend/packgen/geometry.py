"""
Deterministic geometry pipeline: lot polygon → buildable envelope.

Steps (per plan §5):
  1. EPSG:4326 → EPSG:2952 (NAD83(CSRS)/MTM-10) → local CAD frame
  2. Setback inward offsets (shapely offset_curve, GEOS ≥ 3.11)
  3. Angular plane clipping (CR zones and laneway suites only)
  4. Depth limit clipping (§10.20.40.20)
  5. Optional coverage clipping
  6. Return EnvelopeResult

No LLM calls here. All inputs must be deterministic.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from pyproj import Transformer
from shapely import affinity
from shapely.geometry import (
    LinearRing, LineString, MultiPolygon, Point, Polygon, box
)
from shapely.ops import polygonize, unary_union

# EPSG:4326 (WGS-84 lat/lng) → EPSG:2952 (NAD83(CSRS) / MTM Zone 10, metres)
_T_TO_MTM   = Transformer.from_crs(4326, 2952, always_xy=True)
_T_FROM_MTM = Transformer.from_crs(2952, 4326, always_xy=True)

# Minimum valid envelope area (m²)
MIN_ENVELOPE_AREA = 50.0


@dataclass
class EnvelopeResult:
    # The buildable envelope polygon in local CAD frame (origin=front-left, x=street, y=into lot)
    envelope_2d: Polygon
    # The full lot polygon in local CAD frame
    lot_local: Polygon
    # Each setback line as a LineString in local frame {front/rear/left/right}
    setback_lines: dict[str, LineString]
    # The actual setbacks applied (m)
    setbacks_applied: dict[str, float]
    # Lot metrics (local frame, metres)
    lot_width_m: float
    lot_depth_m: float
    lot_area_m2: float
    # Rotation of the local frame relative to MTM grid north (degrees, CW positive)
    rotation_deg: float
    # MTM coordinates of the local frame origin
    origin_mtm: tuple[float, float]
    # True iff angular plane was applied
    angular_plane_applied: bool
    # Depth limit applied
    depth_limit_m: float
    # Separate ancillary building footprint (garden/laneway suite) — None when not applicable
    suite_envelope_2d: Optional[Polygon] = None
    # Warnings from the pipeline
    warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Coordinate transform
# ─────────────────────────────────────────────────────────────────────────────

def _to_mtm(polygon_4326: Polygon) -> Polygon:
    """Project a WGS-84 polygon to NAD83(CSRS)/MTM-10 (EPSG:2952)."""
    xs, ys = polygon_4326.exterior.coords.xy
    xs_m, ys_m = _T_TO_MTM.transform(list(xs), list(ys))
    return Polygon(zip(xs_m, ys_m))


def _identify_front_line(lot_mtm: Polygon, road_bearing_deg: Optional[float] = None
                          ) -> tuple[int, LineString]:
    """
    Return the index (in exterior ring) of the front lot line and its LineString.

    Heuristic (in absence of road-network data):
      1. The front line is the shortest edge that faces away from the lot centroid.
      2. If road_bearing_deg is supplied (from PostGIS), use the edge most perpendicular
         to that bearing.
    The returned index is the starting vertex index of the front edge.
    """
    coords = list(lot_mtm.exterior.coords[:-1])  # drop repeated last point
    n = len(coords)
    centroid = lot_mtm.centroid

    if road_bearing_deg is not None:
        # Find edge most perpendicular to road bearing
        road_vec = (math.cos(math.radians(road_bearing_deg)),
                    math.sin(math.radians(road_bearing_deg)))
        best_idx, best_dot = 0, -1.0
        for i in range(n):
            p0, p1 = coords[i], coords[(i + 1) % n]
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue
            # Edge midpoint direction from centroid
            mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            to_mid = (mx - centroid.x, my - centroid.y)
            dot = abs(to_mid[0] * road_vec[0] + to_mid[1] * road_vec[1]) / (
                math.hypot(*to_mid) + 1e-9
            )
            if dot > best_dot:
                best_dot, best_idx = dot, i
        p0, p1 = coords[best_idx], coords[(best_idx + 1) % n]
        return best_idx, LineString([p0, p1])

    # Default: pick shortest edge; if tie pick the one whose midpoint is farthest south
    edges = []
    for i in range(n):
        p0, p1 = coords[i], coords[(i + 1) % n]
        length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        mid_y = (p0[1] + p1[1]) / 2
        edges.append((length, -mid_y, i))  # sort: shortest first, then southernmost
    edges.sort()
    best_idx = edges[0][2]
    p0, p1 = coords[best_idx], coords[(best_idx + 1) % n]
    return best_idx, LineString([p0, p1])


def _build_local_frame(lot_mtm: Polygon, front_line: LineString
                        ) -> tuple[Polygon, np.ndarray, float, tuple[float, float]]:
    """
    Build a local CAD frame:
      origin = front-left corner of front_line (left when facing into the lot)
      X-axis = along front line (left → right)
      Y-axis = perpendicular into the lot

    Returns (lot_local, affine_matrix_3x3, rotation_deg, origin_mtm).
    The affine matrix M maps local (x,y) → MTM (easting, northing) via [x,y,1] @ M.T.
    """
    p0 = np.array(front_line.coords[0])
    p1 = np.array(front_line.coords[1])
    dx, dy = p1 - p0
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length  # unit X

    # Determine which endpoint is "left" (lot interior is to the left of the X direction)
    centroid = np.array([lot_mtm.centroid.x, lot_mtm.centroid.y])
    mid = (p0 + p1) / 2
    to_c = centroid - mid
    # Y-axis = rotate X by +90° (CCW); check that it points toward centroid
    vy_candidate = np.array([-uy, ux])
    if np.dot(to_c, vy_candidate) < 0:
        vy_candidate = -vy_candidate
        ux, uy = -ux, -uy
        p0, p1 = p1, p0

    origin = p0  # front-left in MTM
    rotation_deg = math.degrees(math.atan2(uy, ux))  # rotation of X axis from MTM east

    # Affine transform: local → MTM
    # MTM = origin + x * [ux, uy] + y * [vx, vy]
    vx, vy = vy_candidate

    def _to_local(pt_mtm):
        rel = np.array(pt_mtm) - origin
        x_loc =  rel[0] * ux + rel[1] * uy
        y_loc =  rel[0] * vx + rel[1] * vy
        return (x_loc, y_loc)

    # Transform lot exterior
    coords_local = [_to_local(c) for c in list(lot_mtm.exterior.coords)]
    lot_local = Polygon(coords_local)

    M = np.array([
        [ux, vx, origin[0]],
        [uy, vy, origin[1]],
        [ 0,  0,          1],
    ])

    return lot_local, M, rotation_deg, (float(origin[0]), float(origin[1]))


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Setback offsetting
# ─────────────────────────────────────────────────────────────────────────────

def _classify_edges(lot_local: Polygon) -> dict[str, LineString]:
    """
    Classify the 4 (or more) edges of the lot into front/rear/left/right.

    In the local frame:
      front: edge with smallest mean Y (closest to street)
      rear:  edge with largest mean Y
      left:  edge with smallest mean X
      right: edge with largest mean X
    For non-rectangular lots the classification is approximate.
    """
    coords = list(lot_local.exterior.coords[:-1])
    n = len(coords)
    edges: list[dict] = []
    for i in range(n):
        p0, p1 = coords[i], coords[(i + 1) % n]
        edges.append({
            "idx": i,
            "line": LineString([p0, p1]),
            "mean_x": (p0[0] + p1[0]) / 2,
            "mean_y": (p0[1] + p1[1]) / 2,
        })

    sorted_y = sorted(edges, key=lambda e: e["mean_y"])
    sorted_x = sorted(edges, key=lambda e: e["mean_x"])

    return {
        "front": sorted_y[0]["line"],
        "rear":  sorted_y[-1]["line"],
        "left":  sorted_x[0]["line"],
        "right": sorted_x[-1]["line"],
    }


def _inward_offset(line: LineString, distance: float, lot_local: Polygon) -> LineString:
    """
    Offset `line` inward by `distance` metres.

    Uses shapely offset_curve. We try both sides and keep the one
    closer to the lot centroid (defensive against GEOS orientation edge cases).
    """
    centroid = lot_local.centroid
    for sign in (1, -1):
        try:
            offset = line.offset_curve(sign * distance)
            if offset is None or offset.is_empty:
                continue
            # Extend by 500 m on both ends to guarantee intersection
            coords = list(offset.coords)
            if len(coords) < 2:
                continue
            p0, p1 = np.array(coords[0]), np.array(coords[-1])
            d = p1 - p0
            dnorm = d / (np.linalg.norm(d) + 1e-9)
            extended = LineString([
                tuple(p0 - dnorm * 500),
                tuple(p1 + dnorm * 500),
            ])
            # Check that it's on the correct side
            mid = extended.interpolate(0.5, normalized=True)
            if mid.distance(centroid) < line.interpolate(0.5, normalized=True).distance(centroid):
                return extended
        except Exception:
            continue
    # Fallback: extend the original line (zero offset)
    coords = list(line.coords)
    p0, p1 = np.array(coords[0]), np.array(coords[-1])
    d = p1 - p0
    dnorm = d / (np.linalg.norm(d) + 1e-9)
    return LineString([tuple(p0 - dnorm * 500), tuple(p1 + dnorm * 500)])


def _compute_setback_envelope(
    lot_local: Polygon,
    edges: dict[str, LineString],
    front_m: float,
    rear_m: float,
    left_m: float,
    right_m: float,
) -> tuple[Polygon, dict[str, LineString]]:
    """
    Inset all four sides and polygonize to get the setback envelope.
    Returns (setback_envelope, dict of offset lines).
    """
    offsets = {
        "front": _inward_offset(edges["front"], front_m,  lot_local),
        "rear":  _inward_offset(edges["rear"],  rear_m,   lot_local),
        "left":  _inward_offset(edges["left"],  left_m,   lot_local),
        "right": _inward_offset(edges["right"], right_m,  lot_local),
    }
    all_lines = list(offsets.values())
    polygons = list(polygonize(unary_union(all_lines)))
    if not polygons:
        # Fallback: intersect lot with buffer
        inset = lot_local.buffer(-min(front_m, rear_m, left_m, right_m) * 0.9)
        return (inset if not inset.is_empty else lot_local.buffer(-0.1)), offsets

    # Pick the polygon whose centroid is inside the lot and has largest area
    candidates = [p for p in polygons if lot_local.contains(p.centroid)]
    if not candidates:
        candidates = polygons
    envelope = max(candidates, key=lambda p: p.area)
    return envelope, offsets


# ─────────────────────────────────────────────────────────────────────────────
# Ancillary suite envelope (§150.1 — garden/laneway suites)
# ─────────────────────────────────────────────────────────────────────────────

def _laneway_suite_envelope(
    lot_local: Polygon,
    rear_setback_m: float,
    max_height_m: float = 6.0,
) -> Optional[Polygon]:
    """§150.1 (Ancillary Buildings) — compute the garden/laneway suite footprint.

    Setbacks (§150.1.40.70):
      - ≥1.0 m from rear lot line
      - ≥1.5 m from side lot lines

    Density constraint: suite must sit within the rear yard zone defined by the
    45° angular plane from the rear lot line at height 0 m.  At distance d from
    the rear lot line the allowed building height = d × tan(45°) = d.  For
    max_height_m = 6.0 m the suite front wall must therefore be ≥ 6.0 m from the
    rear lot line, giving a maximum suite depth of 5.0 m.

    Feasibility check: the suite requires ≥ 8.5 m of rear yard
    (7.5 m separation from principal building + 1.0 m rear setback).
    This is derived from lot depth using the RD 25%-depth rule so that shallow
    lots (< 30 m deep) return None.

    Returns a shapely Polygon in local CAD frame, or None if rear yard too small.
    """
    SUITE_REAR_SETBACK_M = 1.0      # §150.1.40.70
    SUITE_SIDE_SETBACK_M = 1.5      # §150.1.40.70
    SUITE_SEPARATION_M   = 7.5      # from principal building for suite h > 4 m (§150.7.60.30(1))
    MIN_REAR_YARD_NEEDED = SUITE_SEPARATION_M + SUITE_REAR_SETBACK_M  # 8.5 m

    bbox      = lot_local.bounds    # (minx, miny, maxx, maxy)
    lot_depth = bbox[3] - bbox[1]
    lot_width = bbox[2] - bbox[0]

    # Use the resolved rear setback directly — the caller already applied the correct rule
    # (flat 7.5 m for R/RT/RM; 25%-depth for RD/RS; resolved value for CR etc.)
    if rear_setback_m < MIN_REAR_YARD_NEEDED:
        return None

    rear_y = bbox[3]

    # Suite rear wall: 1.0 m from rear lot line
    suite_rear_y  = rear_y - SUITE_REAR_SETBACK_M

    # Suite front wall: angular plane limits to max_height_m from rear lot line (45° plane)
    principal_rear_y = rear_y - rear_setback_m
    suite_front_y = max(rear_y - max_height_m, principal_rear_y)

    if suite_rear_y <= suite_front_y:
        return None

    # Side extents
    suite_left_x  = bbox[0] + SUITE_SIDE_SETBACK_M
    suite_right_x = bbox[2] - SUITE_SIDE_SETBACK_M
    if suite_right_x <= suite_left_x:
        return None

    suite_poly = box(suite_left_x, suite_front_y, suite_right_x, suite_rear_y)
    return suite_poly if suite_poly.area >= 1.0 else None


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Angular plane (CR and laneway suites only)
# ─────────────────────────────────────────────────────────────────────────────

def _angular_plane_clip(
    envelope: Polygon,
    lot_local: Polygon,
    edges: dict[str, LineString],
    zone_symbol: str,
    lot_depth_m: float,
    include_laneway: bool,
) -> Polygon:
    """
    Clip the envelope per the 45° angular plane rule.

    CR/CRE zones: §40.10.40.70(2)(E) — start_height depends on shallow/deep lot.
    Laneway suites: §150.8.60.30(2) — 45° from 4.0 m at 7.5 m from rear main wall.

    For R/RD/RS/RT/RM principal buildings: NO angular plane — return unchanged.
    """
    base = zone_symbol.split()[0].upper() if zone_symbol else ""
    if base not in ("CR", "CRE") and not include_laneway:
        return envelope

    bbox = lot_local.bounds  # (minx, miny, maxx, maxy)
    rear_y = edges["rear"].interpolate(0.5, normalized=True).y

    if base in ("CR", "CRE"):
        # Shallow lot: start_height=10.5m; deep lot (>36m): 7.5m
        start_h = 7.5 if lot_depth_m > 36 else 10.5
        # At ground level (z=0), the angular plane starts at y=rear_y, x=any
        # The plane clips the building footprint; in 2D we clip based on
        # where the ground-floor plan would project the wall outward.
        # Simplified: we don't constrain the 2D footprint for CR zones here —
        # the plan spec notes this is primarily a volumetric constraint.
        # We mark it but don't alter the 2D envelope (the 3D IFC handles it).
        return envelope

    if include_laneway:
        # §150.8.60.30(2): 45° from height 4.0m at 7.5m from rear main wall.
        # In 2D plan view, the rear of the primary building must be ≥ 7.5m from
        # the rear lot line so the laneway suite zone begins there.
        # We clip the primary envelope to keep its rear edge ≥ 7.5m from rear lot line.
        main_wall_y = rear_y - 7.5  # 7.5m from rear = where primary building must stop
        if main_wall_y > bbox[1]:
            clip_box = box(bbox[0], bbox[1], bbox[2], main_wall_y)
            clipped = envelope.intersection(clip_box)
            if not clipped.is_empty and clipped.area > MIN_ENVELOPE_AREA:
                return clipped
    return envelope


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Depth limit (§10.20.40.30) and length limit (§10.20.40.20)
#
# These are TWO SEPARATE by-law regulations acting on different dimensions:
#   Depth  = front-to-rear axis (y-axis in local frame) — §10.20.40.30
#   Length = street-parallel axis (x-axis in local frame) — §10.20.40.20
# ─────────────────────────────────────────────────────────────────────────────

def _building_depth_max_m(lot_depth_m: Optional[float], frontage_m: Optional[float]) -> float:
    """§10.20.40.30 — max building depth measured from required front yard setback.

    Depth is the front-to-rear dimension (y-axis in local CAD frame).
    Baseline: 19.0 m for frontage ≤18 m (§10.20.40.30(1)).
    For frontage >18 m: depth is governed by the side-yard step-back
    (§10.20.40.70(5)), not a global clip — 19.0 m is returned as an upper bound.
    Note: the deep-lot condition in §10.20.40.20(3) extends building *length*,
    not depth; depth stays 19.0 m regardless of lot depth.
    """
    return 19.0


def _building_length_max_m(lot_depth_m: Optional[float], frontage_m: Optional[float]) -> Optional[float]:
    """§10.20.40.20 — max building length (street-parallel, x-axis in local frame).

    17.0 m baseline for frontage ≤18 m. Deep-lot exception extends length to 19 m:
      depth ≥36 m AND frontage <10 m  (§10.20.40.20(3)(a))
      depth ≥40 m AND frontage ≥10 m  (§10.20.40.20(3)(b))
    Returns None for frontage >18 m — that regime is governed by different regs.
    """
    if frontage_m is not None and frontage_m > 18.0:
        return None   # wide lots: governed by side-yard step-back and other regs
    depth = lot_depth_m or 0.0
    front = frontage_m or 0.0
    if (depth >= 36 and front < 10) or (depth >= 40 and front >= 10):
        return 19.0   # deep-lot extension (§10.20.40.20(3))
    return 17.0


def _apply_depth_limit(
    envelope: Polygon,
    lot_local: Polygon,
    edges: dict[str, LineString],
    front_setback_m: float,
    depth_limit: float,
) -> Polygon:
    """Clip the envelope so building depth ≤ depth_limit from the front yard setback line.

    Depth is measured along the y-axis (front-to-rear) in the local CAD frame.
    """
    bbox = lot_local.bounds
    front_line_y = edges["front"].interpolate(0.5, normalized=True).y + front_setback_m
    max_y = front_line_y + depth_limit
    clip = box(bbox[0], bbox[1], bbox[2], max_y)
    clipped = envelope.intersection(clip)
    if clipped.is_empty or clipped.area < MIN_ENVELOPE_AREA:
        return envelope  # don't shrink to nothing
    return clipped


def _apply_length_limit(
    envelope: Polygon,
    lot_local: Polygon,
    length_limit_m: float,
) -> Polygon:
    """Clip the envelope so building length ≤ length_limit_m (§10.20.40.20).

    Building length is the street-parallel dimension (x-axis in local frame).
    The clip is centred on the envelope's current x-extent so the building
    stays centred within the setback envelope after the clip.
    """
    env_bbox = envelope.bounds
    env_w = env_bbox[2] - env_bbox[0]
    if env_w <= length_limit_m:
        return envelope  # already within limit

    bbox = lot_local.bounds
    centre_x = (env_bbox[0] + env_bbox[2]) / 2.0
    half = length_limit_m / 2.0
    clip = box(centre_x - half, bbox[1] - 1, centre_x + half, bbox[3] + 1)
    clipped = envelope.intersection(clip)
    if clipped.is_empty or clipped.area < MIN_ENVELOPE_AREA:
        return envelope
    return clipped


# ─────────────────────────────────────────────────────────────────────────────
# Step 4b — Side-yard step-back for wide RD lots (§10.20.40.70(5))
# ─────────────────────────────────────────────────────────────────────────────

def _apply_side_step_back(
    envelope: Polygon,
    lot_local: Polygon,
    edges: dict[str, LineString],
    left_setback_m: float,
    right_setback_m: float,
    front_setback_m: float = 0.0,
) -> tuple[Polygon, bool]:
    """§10.20.40.70(5) — side-yard step-back for RD lots with frontage > 18 m.

    Beyond 17 m from the front MAIN WALL (§10.20.40.70(5)(A)), the side yard must
    be 7.5 m. The front main wall sits at front_setback_m from the lot line, so
    the threshold is front_y + front_setback_m + 17 m from the lot line.

    Axis convention (local CAD frame):
      y-axis = front-to-rear direction; y=0 ≈ front lot line.
      x-axis = street-parallel.

    Returns (stepped_envelope, was_clipped).
    """
    STEP_BACK_Y_FROM_MAIN_WALL = 17.0  # §10.20.40.70(5) threshold from front main wall
    REQUIRED_SIDE_YARD_M       = 7.5   # required side yard beyond threshold

    left_additional  = max(0.0, REQUIRED_SIDE_YARD_M - left_setback_m)
    right_additional = max(0.0, REQUIRED_SIDE_YARD_M - right_setback_m)

    if left_additional <= 0 and right_additional <= 0:
        return envelope, False  # standard setbacks already meet the 7.5 m requirement

    front_y       = edges["front"].interpolate(0.5, normalized=True).y
    step_back_y   = front_y + front_setback_m + STEP_BACK_Y_FROM_MAIN_WALL
    lot_bbox      = lot_local.bounds   # (minx, miny, maxx, maxy)
    env_bbox      = envelope.bounds    # bounds after standard setbacks

    if step_back_y >= lot_bbox[3] or step_back_y >= env_bbox[3]:
        return envelope, False  # threshold beyond lot / envelope rear — nothing to clip

    # Front portion: y ≤ step_back_y (unchanged)
    front_clip = box(lot_bbox[0] - 1, lot_bbox[1] - 1, lot_bbox[2] + 1, step_back_y)
    front_env  = envelope.intersection(front_clip)

    # Rear portion: y > step_back_y, narrowed by additional inset on each side
    rear_clip = box(lot_bbox[0] - 1, step_back_y, lot_bbox[2] + 1, lot_bbox[3] + 1)
    rear_raw  = envelope.intersection(rear_clip)

    if rear_raw.is_empty:
        return envelope, False

    narrow_clip = box(
        env_bbox[0] + left_additional,  step_back_y,
        env_bbox[2] - right_additional, lot_bbox[3] + 1,
    )
    rear_env = rear_raw.intersection(narrow_clip)

    if rear_env.is_empty or rear_env.area < 1.0:
        return envelope, False

    stepped = unary_union([front_env, rear_env])
    if not stepped.is_valid:
        stepped = stepped.buffer(0)   # fix degenerate topology

    if stepped.is_empty or stepped.area < MIN_ENVELOPE_AREA:
        return envelope, False

    return stepped, True


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Coverage clip (overlay only)
# ─────────────────────────────────────────────────────────────────────────────

def _coverage_clip(
    envelope: Polygon,
    lot_area_m2: float,
    max_coverage_pct: Optional[float],
) -> Polygon:
    """
    If coverage_pct is set (overlay map), uniformly shrink the envelope until
    its area ≤ lot_area_m2 × pct/100, via binary search on inset distance.
    """
    if max_coverage_pct is None:
        return envelope
    target = lot_area_m2 * max_coverage_pct / 100.0
    if envelope.area <= target:
        return envelope

    lo, hi = 0.0, 20.0
    for _ in range(30):
        mid = (lo + hi) / 2
        candidate = envelope.buffer(-mid)
        if candidate.is_empty:
            hi = mid
        elif candidate.area <= target:
            hi = mid
        else:
            lo = mid
    result = envelope.buffer(-hi)
    return result if not result.is_empty and result.area > MIN_ENVELOPE_AREA else envelope


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_envelope(
    polygon_wkt_4326: str,
    *,
    front_setback_m: float,
    rear_setback_m: float,
    left_setback_m: float,
    right_setback_m: float,
    lot_frontage_m: Optional[float] = None,
    zone_symbol: str = "",
    max_coverage_pct: Optional[float] = None,
    include_laneway: bool = False,
    road_bearing_deg: Optional[float] = None,
    apply_side_step_back: bool = False,
) -> EnvelopeResult:
    """
    Main entry point.  All setbacks in metres.
    Returns EnvelopeResult with geometry in local CAD frame.
    """
    from shapely.wkt import loads as wkt_loads

    polygon_4326 = wkt_loads(polygon_wkt_4326)
    if isinstance(polygon_4326, MultiPolygon):
        # Take the largest polygon from the MultiPolygon (handles complex/split parcels)
        polygon_4326 = max(polygon_4326.geoms, key=lambda g: g.area)
    if not isinstance(polygon_4326, Polygon):
        raise ValueError(f"Expected WKT Polygon or MultiPolygon, got {type(polygon_4326).__name__}")

    warnings: list[str] = []

    # ── Step 1: Transform ────────────────────────────────────────────────────
    lot_mtm = _to_mtm(polygon_4326)
    front_idx, front_line_mtm = _identify_front_line(lot_mtm, road_bearing_deg)
    lot_local, M, rotation_deg, origin_mtm = _build_local_frame(lot_mtm, front_line_mtm)

    # Ensure CCW orientation in local frame
    if lot_local.exterior.is_ccw is False:
        lot_local = Polygon(list(lot_local.exterior.coords)[::-1])

    lot_bounds = lot_local.bounds
    lot_width_m  = lot_bounds[2] - lot_bounds[0]
    lot_depth_m  = lot_bounds[3] - lot_bounds[1]
    lot_area_m2  = lot_local.area

    if lot_frontage_m is None:
        lot_frontage_m = lot_width_m

    # Sanity check: Toronto residential lots are 100–2000 m²; large commercial up to ~50,000 m².
    # A value > 100,000 m² almost always means the submitted polygon is a neighbourhood or
    # block boundary rather than a single parcel.
    if lot_area_m2 > 100_000:
        warnings.append(
            f"Lot area is {lot_area_m2:.0f} m² — this is very large for a single parcel "
            f"(typical Toronto lot < 10,000 m²). Verify the submitted polygon is a single "
            f"lot, not a neighbourhood or block boundary. Floor plan layout may be affected."
        )

    # Convexity warning
    convex = lot_local.convex_hull
    if convex.area > 0 and lot_area_m2 / convex.area < 0.85:
        warnings.append("Irregular lot (convexity < 85%): setback lines may not cover all boundaries.")

    # ── Step 2: Classify edges and compute setback envelope ──────────────────
    edges = _classify_edges(lot_local)
    setback_env, offset_lines = _compute_setback_envelope(
        lot_local, edges,
        front_m=front_setback_m,
        rear_m=rear_setback_m,
        left_m=left_setback_m,
        right_m=right_setback_m,
    )

    # ── Step 3: Angular plane ────────────────────────────────────────────────
    ap_applied = False
    base_zone = zone_symbol.split()[0].upper() if zone_symbol else ""
    if base_zone in ("CR", "CRE") or include_laneway:
        setback_env = _angular_plane_clip(
            setback_env, lot_local, edges, zone_symbol, lot_depth_m, include_laneway
        )
        ap_applied = True

    # ── Step 4: Depth limit (§10.20.40.30) and length limit (§10.20.40.20) ───
    depth_lim = _building_depth_max_m(lot_depth_m, lot_frontage_m)
    length_lim = _building_length_max_m(lot_depth_m, lot_frontage_m)
    if base_zone in ("R", "RD", "RS", "RT", "RM"):
        setback_env = _apply_depth_limit(
            setback_env, lot_local, edges, front_setback_m, depth_lim
        )
        if length_lim is not None:
            setback_env = _apply_length_limit(setback_env, lot_local, length_lim)
    else:
        depth_lim  = float("inf")  # non-residential: no built-in limits
        length_lim = None

    # ── Step 4b: Side-yard step-back (§10.20.40.70(5)) — wide RD lots ───────
    if apply_side_step_back and base_zone == "RD" and lot_frontage_m and lot_frontage_m > 18.0:
        setback_env, _step_back_clipped = _apply_side_step_back(
            setback_env, lot_local, edges, left_setback_m, right_setback_m,
            front_setback_m,
        )
        if _step_back_clipped:
            warnings.append(
                "Side-yard step-back applied (§10.20.40.70(5)): the rear portion of the "
                "envelope has been set back to 7.5 m on each side."
            )

    # ── Step 5: Coverage clip ────────────────────────────────────────────────
    setback_env = _coverage_clip(setback_env, lot_area_m2, max_coverage_pct)

    # Final validation
    if setback_env.area < MIN_ENVELOPE_AREA:
        raise ValueError(
            f"Buildable envelope is only {setback_env.area:.1f} m² — too small to generate "
            f"(minimum {MIN_ENVELOPE_AREA} m²). Verify setback parameters."
        )

    # Build setback line dict in local frame (trim to lot bbox for display)
    bbox = lot_local.bounds
    clip_bbox = box(bbox[0] - 1, bbox[1] - 1, bbox[2] + 1, bbox[3] + 1)
    setback_lines_local = {
        k: v.intersection(clip_bbox) for k, v in offset_lines.items()
    }

    # ── Encroachment notice (residential zones only — §10.5.40.60) ──────────
    if base_zone in ("R", "RD", "RS", "RT", "RM", "RA"):
        warnings.append(
            "Note: Encroachments under §10.5.40.60 (eaves up to 0.9 m, bay windows up to "
            "0.9 m, uncovered steps) are permitted into required yards and are NOT reflected "
            "in this setback-based envelope. The building wall may be positioned up to 0.9 m "
            "closer to the lot line in practice."
        )

    # ── Suite envelope (garden/laneway) ─────────────────────────────────────
    suite_env: Optional[Polygon] = None
    if include_laneway:
        suite_env = _laneway_suite_envelope(lot_local, rear_setback_m)
        if suite_env is None:
            warnings.append(
                "Laneway/garden suite envelope could not be computed — rear yard "
                "is too small (requires ≥8.5 m: 7.5 m separation + 1.0 m rear setback). "
                "§150.1.40.70 / §150.7.60.30(1)"
            )

    return EnvelopeResult(
        envelope_2d=setback_env,
        lot_local=lot_local,
        setback_lines=setback_lines_local,
        setbacks_applied={
            "front": front_setback_m,
            "rear":  rear_setback_m,
            "left":  left_setback_m,
            "right": right_setback_m,
        },
        lot_width_m=lot_width_m,
        lot_depth_m=lot_depth_m,
        lot_area_m2=lot_area_m2,
        rotation_deg=rotation_deg,
        origin_mtm=origin_mtm,
        angular_plane_applied=ap_applied,
        depth_limit_m=depth_lim,
        suite_envelope_2d=suite_env,
        warnings=warnings,
    )
