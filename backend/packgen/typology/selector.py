"""Typology selection (Step 6) and stamp fitting (Step 7)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from shapely.geometry import Polygon

from .library import TYPOLOGY_LIBRARY
from .models import Cell, Typology


# ---------------------------------------------------------------------------
# Selection result
# ---------------------------------------------------------------------------

@dataclass
class FitResult:
    typology: Typology
    placed_cells: list[PlacedCell]
    option: Literal["A", "B"]
    fit_frontage_m: float
    fit_depth_m: float
    scale_x: float
    scale_y: float
    origin_local_xy: tuple[float, float]   # (x, y) in local CAD frame metres
    rotation_additional_deg: float          # extra CCW rotation on top of envelope frame
    gfa_m2: float
    warnings: list[str]


@dataclass
class PlacedCell:
    """Absolute coords in local CAD frame (metres).

    polygon — real room outline as (x, y) tuples; None for stamp-path cells
    that only carry an AABB.  When set, the AABB fields (x0/y0/x1/y1) are
    still populated (from the polygon bounds) so all legacy code continues
    to work unchanged.
    """
    cell: Cell
    x0: float
    y0: float
    x1: float
    y1: float
    polygon: Optional[list[tuple[float, float]]] = None
    room_id: Optional[str] = None   # ProgramRoom.id; set only via plan_to_geometry

    @property
    def width_m(self) -> float:
        return self.x1 - self.x0

    @property
    def depth_m(self) -> float:
        return self.y1 - self.y0

    @property
    def area_m2(self) -> float:
        return self.width_m * self.depth_m


# ---------------------------------------------------------------------------
# OBB helpers
# ---------------------------------------------------------------------------

def _envelope_dims(envelope_local: Polygon) -> tuple[float, float]:
    """Return (frontage_m, depth_m) from the envelope AABB.

    The envelope is already in the local CAD frame (x = street-parallel,
    y = depth direction), so AABB gives us the correct dimensions directly.
    """
    minx, miny, maxx, maxy = envelope_local.bounds
    return float(maxx - minx), float(maxy - miny)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _gfa_score(typology: Typology, envelope_area_m2: float) -> float:
    """Score 0-1: how well typology fills the envelope per unit."""
    min_gfa, max_gfa = typology.target_gfa_per_unit_m2
    target = (min_gfa + max_gfa) / 2.0 * typology.units_produced
    if envelope_area_m2 <= 0:
        return 0.0
    ratio = target / envelope_area_m2
    # Perfect fit = 1.0; penalise over/under
    return max(0.0, 1.0 - abs(ratio - 1.0))


def _envelope_fit_score(typology: Typology, env_w: float, env_d: float) -> float:
    """Score 0-1: how closely typology footprint fits envelope OBB."""
    t_w = (typology.min_frontage_m + typology.max_frontage_m) / 2.0
    t_d = (typology.min_depth_m + typology.max_depth_m) / 2.0
    score_w = max(0.0, 1.0 - abs(t_w - env_w) / max(env_w, 1.0))
    score_d = max(0.0, 1.0 - abs(t_d - env_d) / max(env_d, 1.0))
    return (score_w + score_d) / 2.0


# ---------------------------------------------------------------------------
# Main selection
# ---------------------------------------------------------------------------

def select_typologies(
    envelope_local: Polygon,
    zone_symbol: str,
    units_target: int,
    ward: Optional[int] = None,
    layout_option: Optional[Literal["A", "B"]] = None,
) -> tuple[Optional[Typology], Optional[Typology]]:
    """Return (option_A_typology, option_B_typology) best matches.

    option_A = vertical stacking; option_B = horizontal (side-by-side).
    Either can be None if no valid candidate found.
    """
    env_w, env_d = _envelope_dims(envelope_local)
    env_area = envelope_local.area

    candidates = []
    for t in TYPOLOGY_LIBRARY:
        # Zone check
        zone_base = zone_symbol.split("(")[0].rstrip()
        if not any(zone_base.startswith(ez) for ez in t.eligible_zones):
            continue

        # Ward check
        if t.eligible_wards is not None and ward not in t.eligible_wards:
            continue

        # Geometry feasibility (0.5 m tolerance)
        if t.min_frontage_m > env_w + 0.5:
            continue
        if t.min_depth_m > env_d + 0.5:
            continue

        # Unit count match (allow ±1 if basement provides bonus unit)
        u = t.units_produced
        if units_target > 0 and not (u == units_target or
                                     (t.requires_basement and u == units_target + 1) or
                                     (t.requires_basement and u == units_target - 1)):
            continue

        score = (
            _envelope_fit_score(t, env_w, env_d) * 0.5
            + _gfa_score(t, env_area) * 0.3
            + (0.2 if t.stacking_axis == "vertical" else 0.0)
        )
        candidates.append((score, t))

    candidates.sort(key=lambda x: x[0], reverse=True)

    option_a, option_b = None, None
    for _, t in candidates:
        if option_a is None and t.stacking_axis in ("vertical", "mixed"):
            if layout_option is None or layout_option == "A":
                option_a = t
        if option_b is None and t.stacking_axis in ("horizontal", "mixed"):
            if layout_option is None or layout_option == "B":
                option_b = t
        if option_a and option_b:
            break

    return option_a, option_b


# ---------------------------------------------------------------------------
# Stamp fitting
# ---------------------------------------------------------------------------

_SNAP_MM = 0.1   # snap walls to 100 mm grid


def _snap(v: float) -> float:
    return round(v / _SNAP_MM) * _SNAP_MM


def _stretch_cells(
    cells: tuple[Cell, ...],
    scale_x: float,
    scale_y: float,
    max_stretch: float = 0.15,
) -> tuple[list[Cell], float, float]:
    """Non-uniform stretch corridor/stretchable cells up to ±max_stretch each axis.

    Returns (modified_cells, actual_sx, actual_sy).
    """
    sx = min(max(scale_x, 1.0 - max_stretch), 1.0 + max_stretch)
    sy = min(max(scale_y, 1.0 - max_stretch), 1.0 + max_stretch)

    stretched = []
    for c in cells:
        if c.is_stretchable:
            # Stretchable cells absorb the full scale factor
            new_x0 = _snap(c.x0 * sx)
            new_y0 = _snap(c.y0 * sy)
            new_x1 = _snap(c.x1 * sx)
            new_y1 = _snap(c.y1 * sy)
        else:
            # Non-stretchable cells: only translate centroid, keep original size
            cx = (c.x0 + c.x1) / 2 * sx
            cy = (c.y0 + c.y1) / 2 * sy
            hw = (c.x1 - c.x0) / 2
            hd = (c.y1 - c.y0) / 2
            new_x0 = _snap(cx - hw)
            new_y0 = _snap(cy - hd)
            new_x1 = _snap(cx + hw)
            new_y1 = _snap(cy + hd)

        stretched.append(
            Cell(
                role=c.role,
                unit_id=c.unit_id,
                storey=c.storey,
                x0=new_x0,
                y0=new_y0,
                x1=new_x1,
                y1=new_y1,
                min_area_m2=c.min_area_m2,
                min_dim_m=c.min_dim_m,
                needs_egress_window=c.needs_egress_window,
                is_stretchable=c.is_stretchable,
            )
        )
    return stretched, sx, sy


def fit_stamp(
    typology: Typology,
    envelope_local: Polygon,
    option: Literal["A", "B"] = "A",
) -> FitResult:
    """Fit typology stamp into envelope_local (local CAD frame, metres).

    The stamp's [0,1]² normalized space maps to (fit_frontage, fit_depth) metres.
    Origin is placed so the front edge sits flush with the front setback line (y=0
    is already the setback boundary in local frame; we place at y_min of envelope).
    """
    warnings: list[str] = []

    env_w, env_d = _envelope_dims(envelope_local)

    # Horizontal layouts rotate the stamp 90° (units stacked side-by-side become front-to-back)
    rotation_additional = 90.0 if (typology.stacking_axis == "horizontal" and option == "B") else 0.0

    # Desired footprint dimensions from typology template
    t_w = (typology.min_frontage_m + typology.max_frontage_m) / 2.0
    t_d = (typology.min_depth_m + typology.max_depth_m) / 2.0

    # Clamp to envelope OBB with ±15% stretch budget
    fit_w = min(env_w, typology.max_frontage_m)
    fit_d = min(env_d, typology.max_depth_m)
    fit_w = max(fit_w, typology.min_frontage_m)
    fit_d = max(fit_d, typology.min_depth_m)

    # Scale factors for normalized [0,1]² → metres
    scale_x = fit_w / max(t_w, 0.1)
    scale_y = fit_d / max(t_d, 0.1)

    stretched_cells, actual_sx, actual_sy = _stretch_cells(
        typology.stamp_cells, scale_x, scale_y
    )

    if abs(actual_sx - scale_x) > 0.01 or abs(actual_sy - scale_y) > 0.01:
        warnings.append(
            f"Stamp clamped to ±15% stretch: requested ({scale_x:.2f},{scale_y:.2f}), "
            f"applied ({actual_sx:.2f},{actual_sy:.2f})"
        )

    # Final fitted dimensions after clamping
    fit_w = t_w * actual_sx
    fit_d = t_d * actual_sy

    # Origin: place at envelope min corner (front-left)
    env_minx = envelope_local.bounds[0]
    env_miny = envelope_local.bounds[1]
    # Centre horizontally within envelope
    origin_x = env_minx + (env_w - fit_w) / 2.0
    origin_y = env_miny   # front edge at front setback

    # Build PlacedCell list (absolute local frame metres).
    # _stretch_cells multiplies normalized [0,1]² coords by actual_sx/sy, putting them
    # in [0, actual_sx] × [0, actual_sy] space.  Multiplying by t_w/t_d recovers metres:
    #   c.x * t_w  =  (original_x * actual_sx) * t_w  =  original_x * fit_w  ✓
    # Using fit_w (= t_w * actual_sx) instead would double-scale by actual_sx²,
    # making every stretchable room ~13-32% too large.
    placed: list[PlacedCell] = []
    for c in stretched_cells:
        abs_x0 = _snap(origin_x + c.x0 * t_w)
        abs_y0 = _snap(origin_y + c.y0 * t_d)
        abs_x1 = _snap(origin_x + c.x1 * t_w)
        abs_y1 = _snap(origin_y + c.y1 * t_d)
        placed.append(PlacedCell(cell=c, x0=abs_x0, y0=abs_y0, x1=abs_x1, y1=abs_y1))

    # Post-stretch area cap: trim oversized rooms and add corridor for excess
    from ..rules.code_rules import ROOM_MAX_AREA_M2 as _MAX_AREA
    from dataclasses import replace as _dc_rep

    capped_placed: list[PlacedCell] = []
    for pc in placed:
        max_a = _MAX_AREA.get(pc.cell.role)
        if max_a is None or pc.area_m2 <= max_a * 1.20:
            capped_placed.append(pc)
            continue
        # Trim along longer real-metre axis
        trim = (max_a * 1.05) / pc.area_m2
        if pc.width_m >= pc.depth_m:
            new_x1 = _snap(pc.x0 + pc.width_m * trim)
            capped_placed.append(PlacedCell(
                cell=pc.cell, x0=pc.x0, y0=pc.y0, x1=new_x1, y1=pc.y1,
                polygon=getattr(pc, 'polygon', None),
                room_id=getattr(pc, 'room_id', None),
            ))
            if pc.x1 - new_x1 >= 0.5:
                corr_cell = _dc_rep(
                    pc.cell, role="corridor", unit_id=-1,
                    x0=0.0, y0=0.0, x1=0.0, y1=0.0,
                    min_area_m2=0.0, min_dim_m=0.0,
                    needs_egress_window=False, is_stretchable=True,
                )
                capped_placed.append(PlacedCell(
                    cell=corr_cell, x0=new_x1, y0=pc.y0, x1=pc.x1, y1=pc.y1,
                ))
        else:
            new_y1 = _snap(pc.y0 + pc.depth_m * trim)
            capped_placed.append(PlacedCell(
                cell=pc.cell, x0=pc.x0, y0=pc.y0, x1=pc.x1, y1=new_y1,
                polygon=getattr(pc, 'polygon', None),
                room_id=getattr(pc, 'room_id', None),
            ))
            if pc.y1 - new_y1 >= 0.5:
                corr_cell = _dc_rep(
                    pc.cell, role="corridor", unit_id=-1,
                    x0=0.0, y0=0.0, x1=0.0, y1=0.0,
                    min_area_m2=0.0, min_dim_m=0.0,
                    needs_egress_window=False, is_stretchable=True,
                )
                capped_placed.append(PlacedCell(
                    cell=corr_cell, x0=pc.x0, y0=new_y1, x1=pc.x1, y1=pc.y1,
                ))
    placed = capped_placed

    # GFA = sum of above-grade storey footprints (exclude corridor cells)
    storeys = sorted({c.storey for c in typology.stamp_cells if c.storey >= 0})
    gfa = 0.0
    for s in storeys:
        for pc in placed:
            if pc.cell.storey == s and pc.cell.role not in ("balcony", "void", "corridor"):
                gfa += pc.area_m2

    return FitResult(
        typology=typology,
        placed_cells=placed,
        option=option,
        fit_frontage_m=fit_w,
        fit_depth_m=fit_d,
        scale_x=actual_sx,
        scale_y=actual_sy,
        origin_local_xy=(origin_x, origin_y),
        rotation_additional_deg=rotation_additional,
        gfa_m2=gfa,
        warnings=warnings,
    )
