"""Data contracts for the deterministic planning pipeline.

Coordinate system (matches packgen/ai/schema.py):
  Origin = front-left interior corner of buildable envelope at established grade
  +X     = right, parallel to front lot line (looking from street)
  +Y     = away from street (depth direction)
  Units  = metres

Stage flow:
  DesignBrief → SpaceProgram → AdjacencyMatrix → CoreSpec / StructuralGrid
              → WallNetwork  → FloorPlanJSON (packgen/ai/schema.py, unchanged)
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

from ..rules.code_rules import ROOM_MIN_AREA_M2, ROOM_MIN_DIM_M, VALID_ROLES


# ---------------------------------------------------------------------------
# Shared role type alias (keeps parity with Cell.role Literal in models.py)
# ---------------------------------------------------------------------------

RoleStr = Literal[
    "bedroom", "master_bedroom", "living", "dining", "kitchen",
    "bathroom", "powder_room", "laundry", "stair", "corridor",
    "entry", "mechanical", "storage", "balcony", "void",
    "garage",   # parking space reservation (§200.5.10.30)
]


# ---------------------------------------------------------------------------
# 1. DesignBrief — extends the RoomBriefModel shape in generate_pack_router.py
# ---------------------------------------------------------------------------

class BriefRoomSpec(BaseModel):
    """One room type within a unit brief (extends RoomSpecModel)."""
    role: RoleStr
    count: int = Field(default=1, ge=1, le=20)
    min_area_m2: float = Field(default=0.0, ge=0.0, le=200.0)
    storey_preference: int = Field(default=0, ge=-1, le=5)
    must_exterior: bool = False    # caller may override per-room


class BriefUnit(BaseModel):
    """One dwelling unit within a DesignBrief (extends UnitBriefModel)."""
    unit_id: int = Field(..., ge=1, le=10)
    rooms: list[BriefRoomSpec] = Field(min_length=1)


class ParkingSpec(BaseModel):
    count: int = Field(default=0, ge=0, le=10)
    type: Literal["surface", "garage", "underground", "tandem"] = "surface"


class DesignBrief(BaseModel):
    """Top-level user brief consumed by the planning pipeline."""
    units: list[BriefUnit] = Field(min_length=1, max_length=10)
    parking: ParkingSpec = Field(default_factory=ParkingSpec)
    stacking_pref: Literal["vertical", "horizontal", "mixed"] = "vertical"
    orientation_prefs: Optional[list[str]] = Field(
        default=None,
        description="Free-text orientation hints, e.g. ['living_south', 'bedrooms_quiet']",
        max_length=10,
    )
    budget_tier: Optional[Literal["entry", "mid", "premium"]] = None
    notes: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# 2. ProgramRoom — one resolved room in the space program
# ---------------------------------------------------------------------------

class ProgramRoom(BaseModel):
    """A single room with resolved area targets and constraints.

    Derived from DesignBrief + envelope area budget. Area values pull
    defaults from code_rules.ROOM_MIN_AREA_M2 / ROOM_MIN_DIM_M.
    """
    id: str = Field(..., max_length=40, description="Unique room id, e.g. 'br_u0_0'")
    role: RoleStr
    unit_id: int = Field(..., ge=-1, le=10, description="-1 = shared/common")
    storey: int = Field(..., ge=-1, le=4)
    target_area_m2: float = Field(..., gt=0.0, le=200.0)
    area_tolerance: float = Field(
        default=0.15, ge=0.0, le=0.5,
        description="Fractional tolerance around target_area_m2 (±15% by default)",
    )
    min_clear_dim_m: float = Field(
        default=0.0, ge=0.0, le=10.0,
        description="Minimum clear dimension in each axis (populated from code_rules)",
    )
    furniture_box_m: Optional[tuple[float, float]] = Field(
        default=None,
        description="(width, depth) of minimum furnishable rectangle in metres",
    )
    exterior_required: bool = False
    wet: bool = Field(
        default=False,
        description="True for kitchen, bathroom, laundry — must align to wet column",
    )
    zone_class: Literal["public", "private", "service", "circulation"] = "private"

    @model_validator(mode="after")
    def _apply_code_rules_defaults(self) -> "ProgramRoom":
        if self.min_clear_dim_m == 0.0:
            self.min_clear_dim_m = ROOM_MIN_DIM_M.get(self.role, 0.0)
        floor = ROOM_MIN_AREA_M2.get(self.role, 0.0)
        if self.target_area_m2 < floor:
            raise ValueError(
                f"target_area_m2 {self.target_area_m2} m² is below OBC minimum "
                f"{floor} m² for role '{self.role}'"
            )
        return self


# ---------------------------------------------------------------------------
# 3. SpaceProgram — resolved list of ProgramRooms
# ---------------------------------------------------------------------------

class SpaceProgram(BaseModel):
    """Resolved space program emitted after area budgeting."""
    rooms: list[ProgramRoom] = Field(min_length=1)

    def total_area_by_storey(self) -> dict[int, float]:
        """Return {storey: total target m²} across all rooms."""
        result: dict[int, float] = {}
        for r in self.rooms:
            result[r.storey] = result.get(r.storey, 0.0) + r.target_area_m2
        return result

    def rooms_for_unit(self, unit_id: int) -> list[ProgramRoom]:
        return [r for r in self.rooms if r.unit_id == unit_id]

    def rooms_for_storey(self, storey: int) -> list[ProgramRoom]:
        return [r for r in self.rooms if r.storey == storey]


# ---------------------------------------------------------------------------
# 4. AdjacencyEdge / AdjacencyMatrix
# ---------------------------------------------------------------------------

class AdjacencyEdge(BaseModel):
    """Weighted relationship between two rooms in the space program."""
    a: str = Field(..., max_length=40, description="id of first ProgramRoom")
    b: str = Field(..., max_length=40, description="id of second ProgramRoom")
    weight: float = Field(
        ..., ge=-1.0, le=1.0,
        description="+1 = must be adjacent, -1 = must be separated, 0 = no preference",
    )
    type: Literal["adjacent", "near", "separate"] = "adjacent"

    @model_validator(mode="after")
    def _a_ne_b(self) -> "AdjacencyEdge":
        if self.a == self.b:
            raise ValueError("AdjacencyEdge: a and b must be different room ids")
        return self


class AdjacencyMatrix(BaseModel):
    """Full set of adjacency relationships for a SpaceProgram."""
    edges: list[AdjacencyEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_room_refs(self) -> "AdjacencyMatrix":
        # Collect all referenced room ids
        ids = set()
        for e in self.edges:
            ids.add(e.a)
            ids.add(e.b)
        # Duplicate-edge check (unordered pair)
        seen: set[frozenset[str]] = set()
        for e in self.edges:
            pair = frozenset({e.a, e.b})
            if pair in seen:
                raise ValueError(f"Duplicate adjacency edge between {e.a!r} and {e.b!r}")
            seen.add(pair)
        return self

    def weight(self, a: str, b: str) -> float:
        """Return the weight for a pair, 0.0 if not defined."""
        pair = frozenset({a, b})
        for e in self.edges:
            if frozenset({e.a, e.b}) == pair:
                return e.weight
        return 0.0


# ---------------------------------------------------------------------------
# 5. CoreSpec — stair + wet column positions (solved once, applied every storey)
# ---------------------------------------------------------------------------

class Rect(BaseModel):
    """Axis-aligned rectangle in the local CAD frame (metres)."""
    x0: float = Field(..., description="Left edge")
    y0: float = Field(..., description="Front edge (street side)")
    x1: float = Field(..., description="Right edge")
    y1: float = Field(..., description="Rear edge")

    @model_validator(mode="after")
    def _valid_rect(self) -> "Rect":
        if self.x1 <= self.x0:
            raise ValueError("Rect: x1 must be > x0")
        if self.y1 <= self.y0:
            raise ValueError("Rect: y1 must be > y0")
        return self

    @property
    def width_m(self) -> float:
        return self.x1 - self.x0

    @property
    def depth_m(self) -> float:
        return self.y1 - self.y0

    @property
    def area_m2(self) -> float:
        return self.width_m * self.depth_m


class CoreSpec(BaseModel):
    """Vertical core: stair footprint + wet columns, held constant across all storeys.

    Solved once (before per-floor room layout) so every floor shares the same
    structural core position.
    """
    stair_rect: Rect = Field(..., description="Stair footprint in local CAD frame (metres)")
    wet_columns: list[Rect] = Field(
        default_factory=list,
        max_length=4,
        description="Wet-wall column rectangles that kitchen/bath/laundry must touch",
    )
    present_on_storeys: list[int] = Field(
        ...,
        min_length=1,
        description="Storey indices where this core is valid (-1=basement, 0=ground, …)",
    )

    @model_validator(mode="after")
    def _storeys_sorted(self) -> "CoreSpec":
        self.present_on_storeys = sorted(set(self.present_on_storeys))
        return self


# ---------------------------------------------------------------------------
# 6. StructuralGrid — column/load-bearing grid persistent vertically
# ---------------------------------------------------------------------------

class StructuralGrid(BaseModel):
    """Regular structural column grid in one axis."""
    spacing_m: float = Field(..., gt=0.0, le=20.0, description="Column spacing in metres")
    offset_m: float = Field(
        default=0.0, ge=0.0,
        description="Distance from the local origin to the first grid line",
    )
    axis: Literal["x", "y"] = Field(
        ..., description="'x' = columns run parallel to +X; 'y' = parallel to +Y"
    )

    def grid_lines(self, envelope_span_m: float) -> list[float]:
        """Return positions of all grid lines within the envelope."""
        lines = []
        pos = self.offset_m
        while pos <= envelope_span_m + 1e-6:
            lines.append(round(pos, 4))
            pos += self.spacing_m
        return lines


# ---------------------------------------------------------------------------
# 7. WallSegment / WallNetwork
# ---------------------------------------------------------------------------

class WallSegment(BaseModel):
    """One wall segment in the half-edge wall network.

    Reuses the type Literal from WallModel in packgen/ai/schema.py so both
    representations stay vocabulary-compatible.
    """
    id: str = Field(..., max_length=40)
    start: list[float] = Field(..., min_length=2, max_length=2, description="[x, y] in metres")
    end: list[float] = Field(..., min_length=2, max_length=2, description="[x, y] in metres")
    thickness_mm: int = Field(default=200, ge=50, le=400)
    type: Literal["exterior", "party", "interior_loadbearing", "interior_partition"] = "interior_partition"
    left_room_id: Optional[str] = Field(default=None, max_length=40)
    right_room_id: Optional[str] = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _nonzero_length(self) -> "WallSegment":
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        if (dx**2 + dy**2) ** 0.5 < 1e-4:
            raise ValueError(f"WallSegment {self.id!r} has zero length")
        return self

    @property
    def length_m(self) -> float:
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        return (dx**2 + dy**2) ** 0.5


class WallNetwork(BaseModel):
    """Half-edge wall network for one storey.

    Each interior wall appears once; the left_room_id / right_room_id fields
    identify the rooms on each side, enabling shared-wall rendering in DXF/IFC
    (one wall line rather than two per-room boxes).
    """
    storey: int = Field(..., ge=-1, le=4)
    segments: list[WallSegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "WallNetwork":
        ids = [s.id for s in self.segments]
        if len(ids) != len(set(ids)):
            dupes = [i for i in set(ids) if ids.count(i) > 1]
            raise ValueError(f"Duplicate WallSegment ids: {dupes}")
        return self

    def exterior_segments(self) -> list[WallSegment]:
        return [s for s in self.segments if s.type == "exterior"]

    def segments_for_room(self, room_id: str) -> list[WallSegment]:
        return [s for s in self.segments if room_id in (s.left_room_id, s.right_room_id)]
