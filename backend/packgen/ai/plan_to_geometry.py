"""Convert a validated FloorPlanJSON into a FitResult-compatible object.

The DXF, SVG, IFC, and PDF writers all accept ``FitResult`` + ``EnvelopeResult``.
This module bridges the AI-generated FloorPlanJSON to those existing writers
so Feature 2 reuses all of Feature 1's export infrastructure.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from shapely.geometry import Polygon

if TYPE_CHECKING:
    from ..typology.selector import FitResult, PlacedCell
    from ..geometry import EnvelopeResult
    from .schema import FloorPlanJSON


# Map FloorPlanJSON room category → internal Cell role
_CATEGORY_TO_ROLE: dict[str, str] = {
    "bedroom":               "bedroom",
    "living":                "living",
    "dining":                "dining",
    "kitchen":               "kitchen",
    "living_dining_kitchen": "living",
    "bathroom":              "bathroom",
    "powder":                "bathroom",
    "stair":                 "stair",
    "corridor":              "corridor",
    "mech":                  "mechanical",
    "storage":               "storage",
    "laundry":               "laundry",
    "entry":                 "entry",
    "balcony":               "balcony",
    "garage":                "void",
}


def floor_plan_to_fit_result(
    plan: "FloorPlanJSON",
    envelope_result: "EnvelopeResult",
    typology_id: str = "ai_generated",
    typology_label: str = "AI Generated",
    option: str = "A",
) -> "FitResult":
    """Convert a FloorPlanJSON into a FitResult using existing Cell/PlacedCell types.

    Each FloorPlanJSON room polygon is converted to a bounding-box PlacedCell so
    the existing DXF/SVG/IFC/PDF writers can consume it without modification.
    The envelope_result provides lot dimensions and setbacks.
    """
    from ..typology.models import Cell, Typology
    from ..typology.selector import FitResult, PlacedCell

    placed_cells: list[PlacedCell] = []
    total_gfa = 0.0

    for storey in plan.storeys:
        for room in storey.rooms:
            if not room.polygon or len(room.polygon) < 3:
                continue
            poly = Polygon(room.polygon)
            if not poly.is_valid or poly.area <= 0:
                continue

            # Preserve the real polygon; AABB fields from bounds for legacy compat
            minx, miny, maxx, maxy = poly.bounds
            raw_pts = [(float(v[0]), float(v[1])) for v in room.polygon]
            role = _CATEGORY_TO_ROLE.get(room.category, "void")

            # Derive unit_id from dwelling_unit_id string (A→0, B→1, GS→99, etc.)
            uid_str = (room.dwelling_unit_id or "0").upper()
            uid_map = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "GS": 99, "LS": 98}
            unit_id = uid_map.get(uid_str, -1)
            try:
                unit_id = int(uid_str)
            except ValueError:
                pass

            cell = Cell(
                role=role,
                unit_id=unit_id,
                storey=storey.level,
                x0=round(minx, 3),
                y0=round(miny, 3),
                x1=round(maxx, 3),
                y1=round(maxy, 3),
                min_area_m2=poly.area,
                min_dim_m=min(maxx - minx, maxy - miny),
                needs_egress_window=room.category == "bedroom",
                is_stretchable=(role not in ("stair", "corridor")),
            )
            pc = PlacedCell(
                cell=cell,
                x0=round(minx, 3),
                y0=round(miny, 3),
                x1=round(maxx, 3),
                y1=round(maxy, 3),
                polygon=raw_pts,
                room_id=room.id,
            )
            placed_cells.append(pc)

            # Accumulate GFA for above-grade rooms
            if storey.level >= 0 and role not in ("void", "balcony"):
                total_gfa += poly.area

    # Build a minimal Typology dataclass (needed by FitResult)
    from ..typology.models import Typology as _Typo
    env_w = envelope_result.lot_width_m
    env_d = envelope_result.lot_depth_m
    above_grade_storeys = {pc.cell.storey for pc in placed_cells if pc.cell.storey >= 0}
    n_storeys = len(above_grade_storeys) or 1
    n_units = len({pc.cell.unit_id for pc in placed_cells if pc.cell.unit_id >= 0} or {0})

    synthetic_typology = _Typo(
        id=typology_id,
        label=typology_label,
        units_produced=n_units,
        stacking_axis="vertical" if n_storeys > 1 else "horizontal",
        min_frontage_m=max(1.0, env_w - 1.0),
        max_frontage_m=env_w + 1.0,
        min_depth_m=max(1.0, env_d - 1.0),
        max_depth_m=env_d + 1.0,
        target_storeys=n_storeys,
        requires_basement=any(pc.cell.storey < 0 for pc in placed_cells),
        target_gfa_per_unit_m2=(total_gfa / n_units * 0.8, total_gfa / n_units * 1.2)
            if n_units > 0 else (50.0, 150.0),
        stamp_cells=tuple(pc.cell for pc in placed_cells),
        corridor_axis="end",
        stair_position="internal",
        eligible_zones=("R", "RD", "RS", "RT", "RM", "RA", "C", "CR"),
        eligible_wards=None,
        notes=plan.metadata.rationale if plan.metadata else "AI-generated plan",
    )

    return FitResult(
        typology=synthetic_typology,
        placed_cells=placed_cells,
        option=option,
        fit_frontage_m=round(env_w, 2),
        fit_depth_m=round(env_d, 2),
        scale_x=1.0,
        scale_y=1.0,
        origin_local_xy=(0.0, 0.0),
        rotation_additional_deg=0.0,
        gfa_m2=round(total_gfa, 1),
        warnings=[],
    )
