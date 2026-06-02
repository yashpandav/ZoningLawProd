"""Typology and Cell dataclasses for the stamp library."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Tuple


@dataclass(frozen=True)
class Cell:
    """A normalized room/zone cell in [0,1]² per-storey space.

    (x0,y0)=front-left, (x1,y1)=rear-right.
    y=0 is the front (street-facing) wall; y=1 is the rear wall.
    """
    role: Literal[
        "bedroom", "master_bedroom", "living", "dining", "kitchen",
        "bathroom", "powder_room", "laundry", "stair", "corridor",
        "entry", "mechanical", "storage", "balcony", "void"
    ]
    unit_id: int        # 0-indexed dwelling unit; -1 = shared/common
    storey: int         # 0=ground, -1=basement, 1=second, 2=third, 3=fourth
    x0: float           # normalized [0, 1]
    y0: float           # normalized [0, 1]; 0=front
    x1: float
    y1: float
    min_area_m2: float = 0.0
    min_dim_m: float = 0.0
    needs_egress_window: bool = False
    is_stretchable: bool = True  # corridor cells can absorb stretch; bedrooms resist

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def depth(self) -> float:
        return self.y1 - self.y0

    @property
    def area_norm(self) -> float:
        return self.width * self.depth


@dataclass(frozen=True)
class TemplateZone:
    """A region in the [0,1]² normalized space where one or more rooms can be placed.

    Zones tile the floor plan without overlapping. Each zone has a list of
    valid roles that can be placed in it, and constraints on subdivision.
    """
    zone_id: str            # e.g. "front_public_ground"
    storey: int
    x0: float
    y0: float
    x1: float
    y1: float
    valid_roles: Tuple[str, ...]        # e.g. ("living", "dining", "kitchen")
    max_subdivisions: int = 1           # how many rooms can share this zone
    subdivision_axis: str = "x"         # "x" or "y" — how to split if subdivided
    is_circulation: bool = False        # corridor/stair zones — auto-filled
    notes: str = ""


@dataclass
class TypologyTemplate:
    """The template version of a typology — replaces stamp_cells for new typologies.

    A Typology can have either stamp_cells (legacy) OR template_zones (new).
    The selector checks `t.has_template()` to know which path to take.
    """
    zones: Tuple[TemplateZone, ...]
    structural_rules: dict              # e.g. {"stair_must_align_across_storeys": True}

    def zones_for_storey(self, storey: int) -> Tuple[TemplateZone, ...]:
        return tuple(z for z in self.zones if z.storey == storey)


@dataclass(frozen=True)
class Typology:
    """A building typology template.

    `stamp_cells` is a flat tuple of Cell objects across all storeys.
    Selection and fitting use only the summary metrics; cells are used
    by the DXF writer and OBC checker.
    """
    id: str
    label: str
    units_produced: int
    stacking_axis: Literal["vertical", "horizontal", "mixed"]
    min_frontage_m: float
    max_frontage_m: float
    min_depth_m: float
    max_depth_m: float
    target_storeys: int
    requires_basement: bool
    target_gfa_per_unit_m2: Tuple[float, float]   # (min, max)
    stamp_cells: Tuple[Cell, ...]
    corridor_axis: Literal["central", "end", "spine"]
    stair_position: Literal["internal", "end", "split"]
    eligible_zones: Tuple[str, ...]
    eligible_wards: Optional[Tuple[int, ...]]      # None = citywide
    notes: str
    version_introduced: str = "569-2013 OC 2024-04-01"
    template: Optional[Any] = field(default=None, hash=False, compare=False)  # TypologyTemplate when migrated

    def has_template(self) -> bool:
        return self.template is not None

    def storeys_count(self) -> int:
        """Number of above-grade storeys (excludes basement)."""
        return len({c.storey for c in self.stamp_cells if c.storey >= 0})

    def cells_for_storey(self, storey: int) -> list[Cell]:
        return [c for c in self.stamp_cells if c.storey == storey]

    def dwelling_cells(self, unit_id: int) -> list[Cell]:
        return [c for c in self.stamp_cells if c.unit_id == unit_id]
