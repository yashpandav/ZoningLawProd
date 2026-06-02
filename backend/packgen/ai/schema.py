"""Pydantic v2 models for FloorPlanJSON — the structured output produced by the LLM.

Coordinate system (local CAD frame):
  Origin = front-left interior corner of buildable envelope at established grade
  +X     = right, parallel to front lot line (looking from street)
  +Y     = away from street (depth direction)
  Units  = metres
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class WallModel(BaseModel):
    id: str = Field(max_length=40)
    start: list[float] = Field(min_length=2, max_length=2)
    end: list[float] = Field(min_length=2, max_length=2)
    type: Literal["exterior", "party", "interior_loadbearing", "interior_partition"]
    thickness_mm: int = Field(default=200, ge=50, le=400)
    height_m: float = Field(default=2.7, ge=2.0, le=6.0)
    fire_rating_min: Literal[0, 30, 45, 60, 90] = 0

    @property
    def length_m(self) -> float:
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        return (dx**2 + dy**2) ** 0.5


class DoorModel(BaseModel):
    id: str = Field(max_length=40)
    wall_id: str = Field(max_length=40)
    position_along_wall_m: float = Field(ge=0.0)
    width_m: float = Field(ge=0.61, le=1.8)
    height_m: float = Field(default=2.1, ge=2.03, le=2.5)
    swing: Literal["left_in", "right_in", "left_out", "right_out", "slide", "pocket", "double"]
    fire_rating_min: int = Field(default=0, ge=0)
    connects_rooms: list[str] = Field(default_factory=list, max_length=2)


class WindowModel(BaseModel):
    id: str = Field(max_length=40)
    wall_id: str = Field(max_length=40)
    position_along_wall_m: float = Field(ge=0.0)
    width_m: float = Field(ge=0.3, le=4.0)
    sill_m: float = Field(ge=0.0, le=1.5)
    head_m: float = Field(ge=0.5, le=3.0)
    operable: bool = True
    egress_compliant: bool = False
    clear_opening_m2: Optional[float] = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check_head_above_sill(self) -> "WindowModel":
        if self.head_m <= self.sill_m:
            raise ValueError("head_m must be greater than sill_m")
        return self


class RoomModel(BaseModel):
    id: str = Field(max_length=40)
    label: str = Field(max_length=80)
    polygon: list[list[float]] = Field(min_length=3)
    category: Literal[
        "bedroom", "living", "dining", "kitchen", "living_dining_kitchen",
        "bathroom", "powder", "stair", "corridor", "mech", "storage",
        "laundry", "entry", "balcony", "garage",
    ]
    dwelling_unit_id: Optional[str] = Field(default=None, max_length=20)
    area_m2: Optional[float] = Field(default=None, ge=0.0)


class StoreyModel(BaseModel):
    level: int = Field(ge=-1, le=4)
    elevation_m: float = Field(ge=-5.0, le=20.0)
    floor_to_floor_m: float = Field(default=2.7, ge=2.4, le=4.0)
    walls: list[WallModel]
    doors: list[DoorModel] = Field(default_factory=list)
    windows: list[WindowModel] = Field(default_factory=list)
    rooms: list[RoomModel]


class StairModel(BaseModel):
    id: str = Field(max_length=40)
    footprint: list[list[float]] = Field(min_length=3)
    from_level: int = Field(ge=-1, le=3)
    to_level: int = Field(ge=0, le=4)
    tread_count: int = Field(ge=2, le=25)
    tread_mm: int = Field(ge=235, le=355)
    riser_mm: int = Field(ge=125, le=200)
    direction: Literal["up_north", "up_south", "up_east", "up_west"]


class FloorPlanMetadata(BaseModel):
    typology_label: Optional[str] = Field(default=None, max_length=80)
    rationale: Optional[str] = Field(default=None, max_length=400)


class FloorPlanJSON(BaseModel):
    """Top-level model for the LLM-generated floor plan."""
    units_m: Literal["meters"] = "meters"
    metadata: Optional[FloorPlanMetadata] = None
    storeys: list[StoreyModel] = Field(min_length=1, max_length=4)
    stairs: list[StairModel] = Field(default_factory=list)


def get_json_schema() -> dict:
    """Return the JSON Schema for FloorPlanJSON for use in OpenAI structured outputs."""
    return FloorPlanJSON.model_json_schema()


def get_json_schema_str() -> str:
    return json.dumps(get_json_schema(), indent=2)
