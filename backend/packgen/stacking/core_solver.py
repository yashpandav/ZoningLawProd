"""Deterministic stair-core and wet-column solver.

Fixes ONE core position (stair footprint + wet columns) valid on ALL storeys,
before any room layout happens.  This is the vertical spine — per-storey room
layout runs with the core as a fixed obstacle.

OBC references used:
  §9.8.2  minimum stair clear width  → ROOM_MIN_DIM_M["stair"] = 0.86 m
  §9.8.3  minimum tread depth 235 mm, maximum riser 200 mm
  §9.8.3.2 minimum stair area       → ROOM_MIN_AREA_M2["stair"] = 3.5 m²
"""
from __future__ import annotations

import math
from typing import Optional

from shapely.geometry import Polygon
from shapely.geometry import box as shapely_box

from ..rules.code_rules import ROOM_MIN_AREA_M2, ROOM_MIN_DIM_M
from ..schemas.contracts import CoreSpec, Rect, SpaceProgram

# ---------------------------------------------------------------------------
# OBC stair geometry constants (most compact values → smallest footprint)
# ---------------------------------------------------------------------------

_RISER_M            = 0.200   # max riser height (OBC §9.8.3; StairModel.riser_mm ≤ 200)
_TREAD_M            = 0.235   # min tread depth  (OBC §9.8.3; StairModel.tread_mm ≥ 235)
_STAIR_MIN_WIDTH_M  = ROOM_MIN_DIM_M["stair"]    # 0.86 m clear width (OBC §9.8.2)
_STAIR_MIN_AREA_M2  = ROOM_MIN_AREA_M2["stair"]  # 3.5 m²

# Grid search resolution (metres)
_SEARCH_STEP = 0.5

# Wet-column nominal dimensions (m) — enough to host a bathroom or kitchen wet wall
_WET_COL_W = 0.9
_WET_COL_D = 1.0

# Scoring weights
_W_WALL  = 1.5   # prefer touching a side wall
_W_DEPTH = 0.8   # prefer vertically centred
_W_ENTRY = 3.0   # penalty for blocking the front entry zone


def _snap(v: float) -> float:
    """Snap to 100 mm grid (mirrors selector.py convention)."""
    return round(v / 0.1) * 0.1


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def solve_core(
    program: SpaceProgram,
    envelope_2d: Polygon,
    target_floors: int,
    floor_to_floor_m: float = 2.85,
) -> CoreSpec:
    """Return a CoreSpec with stair + wet columns valid on every storey.

    The same stair_rect is returned for all storeys — that is the whole point.
    Wet columns are placed adjacent to the stair to minimise expected pipe runs.
    """
    # 1. Stair footprint size
    stair_w, stair_d = _stair_footprint(floor_to_floor_m)

    # 2. Find best placement (try both orientations)
    best_rect, best_score = _find_best_core(envelope_2d, stair_w, stair_d)
    alt_rect, alt_score   = _find_best_core(envelope_2d, stair_d, stair_w)

    if best_rect is None and alt_rect is None:
        # Envelope too small — clip to what fits
        best_rect = _fallback_rect(envelope_2d, stair_w, stair_d)
    elif alt_rect is not None and (best_rect is None or alt_score < best_score):
        best_rect = alt_rect

    x0, y0, x1, y1 = best_rect  # type: ignore[misc]
    stair_rect = Rect(x0=x0, y0=y0, x1=x1, y1=y1)

    # 3. Wet columns adjacent to stair
    wet_columns = _place_wet_columns(stair_rect, envelope_2d)

    # 4. Storey list: all above-grade + basement if any room is at storey -1
    present: list[int] = list(range(0, target_floors))
    if any(r.storey == -1 for r in program.rooms):
        present = [-1] + present

    return CoreSpec(stair_rect=stair_rect, wet_columns=wet_columns, present_on_storeys=present)


# ---------------------------------------------------------------------------
# Step 1 — stair footprint dimensions
# ---------------------------------------------------------------------------

def _stair_footprint(floor_to_floor_m: float) -> tuple[float, float]:
    """Return (width_m, run_m) using OBC-minimum values for the most compact flight.

    width_m: ≥ _STAIR_MIN_WIDTH_M and wide enough so area ≥ _STAIR_MIN_AREA_M2.
    run_m  : number of risers × tread depth.
    """
    n_risers = math.ceil(floor_to_floor_m / _RISER_M)
    run_m    = _snap(n_risers * _TREAD_M)

    # Width must satisfy both: clear-width minimum AND area minimum
    min_width_from_area = _STAIR_MIN_AREA_M2 / run_m if run_m > 0 else _STAIR_MIN_WIDTH_M
    width_m = _snap(max(_STAIR_MIN_WIDTH_M, min_width_from_area) + 1e-9)  # +epsilon before snap

    # Double-check after snap
    if width_m * run_m < _STAIR_MIN_AREA_M2 - 1e-4:
        width_m = _snap(_STAIR_MIN_AREA_M2 / run_m + 0.05)

    return width_m, run_m


# ---------------------------------------------------------------------------
# Step 2–3 — grid search and scoring
# ---------------------------------------------------------------------------

def _find_best_core(
    envelope_2d: Polygon,
    stair_w: float,
    stair_d: float,
) -> tuple[Optional[tuple[float, float, float, float]], float]:
    """Search a coarse grid for the best (x0,y0,x1,y1) placement.

    Returns (best_rect_tuple, best_score).  best_rect is None if nothing fits.
    """
    minx, miny, maxx, maxy = envelope_2d.bounds
    cx_env = (minx + maxx) / 2
    cy_env = (miny + maxy) / 2

    xs = _grid(minx, maxx - stair_w, _SEARCH_STEP)
    ys = _grid(miny, maxy - stair_d, _SEARCH_STEP)

    best: Optional[tuple[float, float, float, float]] = None
    best_score = float("inf")

    for x0 in xs:
        x1 = _snap(x0 + stair_w)
        for y0 in ys:
            y1 = _snap(y0 + stair_d)
            if not envelope_2d.contains(shapely_box(x0, y0, x1, y1)):
                continue
            score = _score(x0, y0, x1, y1, cx_env, cy_env, minx, maxx, miny)
            if score < best_score:
                best_score = score
                best = (x0, y0, x1, y1)

    return best, best_score


def _grid(start: float, end: float, step: float) -> list[float]:
    """Snapped grid positions from start to end (inclusive when it fits)."""
    result: list[float] = []
    v = _snap(start)
    while v <= end + 1e-6:
        result.append(v)
        v = _snap(v + step)
    return result


def _score(
    x0: float, y0: float, x1: float, y1: float,
    cx_env: float, cy_env: float,
    minx: float, maxx: float, miny: float,
) -> float:
    """Score a candidate core placement (lower = better).

    Preferences:
    1. Against either side wall (typical Toronto row-house — party wall location)
    2. Vertically centred in the building depth (minimises corridor length to bedrooms)
    3. Not blocking the front-entry zone (bottom 1.5 m of envelope)
    """
    cy = (y0 + y1) / 2

    # Distance from nearest side wall (0 = touching, good)
    wall_dist = min(abs(x0 - minx), abs(x1 - maxx))

    # Deviation from vertical centre
    depth_dev = abs(cy - cy_env)

    # Penalty for blocking front door zone
    entry_penalty = _W_ENTRY if y0 < miny + 1.5 else 0.0

    return _W_WALL * wall_dist + _W_DEPTH * depth_dev + entry_penalty


def _fallback_rect(
    envelope_2d: Polygon,
    stair_w: float,
    stair_d: float,
) -> tuple[float, float, float, float]:
    """Place at envelope origin, clipped to what fits."""
    minx, miny, maxx, maxy = envelope_2d.bounds
    w = _snap(min(stair_w, maxx - minx))
    d = _snap(min(stair_d, maxy - miny))
    return (minx, miny, _snap(minx + w), _snap(miny + d))


# ---------------------------------------------------------------------------
# Step 4 — wet-column placement
# ---------------------------------------------------------------------------

def _place_wet_columns(stair_rect: Rect, envelope_2d: Polygon) -> list[Rect]:
    """Place 1–2 wet-wall columns adjacent to the stair core.

    Tries all four sides of the stair and keeps candidates that fit inside the
    envelope.  Scores by proximity to the envelope centroid (central position
    minimises expected pipe run to kitchen/bathroom).  Returns the best 1–2
    columns, ensuring they are on different axes so they serve both depth zones.
    """
    minx, miny, maxx, maxy = envelope_2d.bounds
    cx_env = (minx + maxx) / 2
    cy_env = (miny + maxy) / 2

    scored: list[tuple[float, str, Rect]] = []
    for side in ("left", "right", "front", "rear"):
        rect = _wet_col_for_side(stair_rect, side)
        if rect is None:
            continue
        if not envelope_2d.contains(shapely_box(rect.x0, rect.y0, rect.x1, rect.y1)):
            continue
        cx = (rect.x0 + rect.x1) / 2
        cy = (rect.y0 + rect.y1) / 2
        dist = math.hypot(cx - cx_env, cy - cy_env)
        scored.append((dist, side, rect))

    if not scored:
        return []

    scored.sort(key=lambda t: t[0])

    # Always take the best candidate
    result = [scored[0][2]]
    best_axis = "x" if scored[0][1] in ("left", "right") else "y"

    # Add a second candidate on a different axis (to serve depth-wise separation)
    for _, side, rect in scored[1:]:
        axis = "x" if side in ("left", "right") else "y"
        if axis != best_axis:
            result.append(rect)
            break

    return result


def _wet_col_for_side(stair: Rect, side: str) -> Optional[Rect]:
    """Return a wet-column Rect touching the stair on the given side."""
    w, d = _WET_COL_W, _WET_COL_D
    try:
        if side == "left":
            return Rect(
                x0=_snap(stair.x0 - w), y0=stair.y0,
                x1=stair.x0,            y1=_snap(stair.y0 + d),
            )
        if side == "right":
            return Rect(
                x0=stair.x1,            y0=stair.y0,
                x1=_snap(stair.x1 + w), y1=_snap(stair.y0 + d),
            )
        if side == "front":
            return Rect(
                x0=stair.x0,            y0=_snap(stair.y0 - d),
                x1=_snap(stair.x0 + w), y1=stair.y0,
            )
        # rear
        return Rect(
            x0=stair.x0,            y0=stair.y1,
            x1=_snap(stair.x0 + w), y1=_snap(stair.y1 + d),
        )
    except Exception:
        return None
