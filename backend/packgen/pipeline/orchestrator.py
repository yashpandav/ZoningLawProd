"""Deterministic planning pipeline orchestrator.

Runs the full stage sequence and returns a FloorPlanJSON:

  DesignBrief + EnvelopeResult
      │
      ▼
  generate_space_program          → SpaceProgram, warnings
      │
      ▼
  build_adjacency                 → AdjacencyMatrix  (LLM optional)
      │
      ▼
  solve_core                      → CoreSpec
      │
      ▼
  solve_floor × storey            → list[RoomModel], WallNetwork
      │
      ▼
  assemble StoreyModel + StairModel → FloorPlanJSON

Run with openai_client=None for fully deterministic mode (no network).
"""
from __future__ import annotations

import math
from typing import Optional

from ..adjacency.graph_builder import build_adjacency
from ..ai.schema import (
    FloorPlanJSON, FloorPlanMetadata, RoomModel, StairModel, StoreyModel, WallModel,
)
from ..geometry import EnvelopeResult
from ..program.space_program import generate_space_program
from ..schemas.contracts import (
    AdjacencyMatrix, CoreSpec, DesignBrief, WallNetwork, WallSegment,
)
from ..solver.layout_solver import solve_floor
from ..stacking.core_solver import solve_core

# Default OBC stair geometry (most compact values — matches core_solver defaults)
_RISER_M = 0.200
_TREAD_M = 0.235


def generate_floor_plan(
    brief: DesignBrief,
    envelope: EnvelopeResult,
    target_floors: Optional[int] = None,
    target_stacking: str = "vertical",
    road_bearing_deg: Optional[float] = None,
    openai_client=None,
    floor_to_floor_m: float = 2.85,
    fsi: Optional[float] = None,
    building_type: Optional[str] = None,
    max_height_m: Optional[float] = None,
    ward: int | str | None = None,
) -> FloorPlanJSON:
    """Run the full deterministic planning pipeline for one building option.

    Pass openai_client=None (default) for a fully deterministic run that makes
    no network calls.  Pass an OpenAI client to enable LLM adjacency adjustments.

    target_floors:
        Number of above-grade storeys. When None, derived from max_height_m using
        2.85 m/floor; defaults to 2 if neither is provided.

    Raises ValueError if the brief is completely infeasible (envelope too small
    to fit even the OBC minimums).
    """
    # Derive effective floor count for solver loops. Mirrors generate_space_program
    # derivation so both agree; generate_space_program emits the user-facing warning.
    _effective_floors = target_floors
    if _effective_floors is None:
        if max_height_m is not None:
            _effective_floors = max(1, int(max_height_m / floor_to_floor_m))
        else:
            _effective_floors = 2

    # ── 1. Space program ─────────────────────────────────────────────────────
    # Pass original target_floors (possibly None) + max_height_m so that
    # generate_space_program emits the derivation warning when appropriate.
    program, prog_warnings = generate_space_program(
        brief, envelope, target_floors,
        fsi=fsi, building_type=building_type, max_height_m=max_height_m,
        ward=ward,
    )

    # ── 2. Adjacency matrix ──────────────────────────────────────────────────
    # Disable LLM when no client supplied — avoids network calls in tests/CI
    allow_llm = openai_client is not None
    adjacency = build_adjacency(
        program, road_bearing_deg,
        openai_client=openai_client,
        allow_llm=allow_llm,
    )

    # ── 3. Vertical core (stair + wet columns) ───────────────────────────────
    core = solve_core(program, envelope.envelope_2d, _effective_floors, floor_to_floor_m)

    # ── 4. Per-storey room layout ────────────────────────────────────────────
    storey_models: list[StoreyModel] = []

    # Basement first (if present)
    basement_rooms = [r for r in program.rooms if r.storey == -1]
    if basement_rooms:
        rms, net = solve_floor(
            basement_rooms, envelope.envelope_2d, core, adjacency, -1,
            stacking=target_stacking,
        )
        storey_models.append(StoreyModel(
            level=-1,
            elevation_m=round(-floor_to_floor_m, 3),
            floor_to_floor_m=floor_to_floor_m,
            walls=_network_to_wall_models(net, floor_to_floor_m),
            rooms=rms,
        ))

    # Above-grade storeys
    for idx in range(_effective_floors):
        storey_rooms = [r for r in program.rooms if r.storey == idx]
        if not storey_rooms:
            continue
        rms, net = solve_floor(
            storey_rooms, envelope.envelope_2d, core, adjacency, idx,
            stacking=target_stacking,
        )
        storey_models.append(StoreyModel(
            level=idx,
            elevation_m=round(idx * floor_to_floor_m, 3),
            floor_to_floor_m=floor_to_floor_m,
            walls=_network_to_wall_models(net, floor_to_floor_m),
            rooms=rms,
        ))

    if not storey_models:
        raise ValueError(
            "Space program produced no rooms — brief may be infeasible for this envelope."
        )

    # ── 5. Stair model ───────────────────────────────────────────────────────
    stairs = [_make_stair_model(core, _effective_floors, floor_to_floor_m)]

    # ── 6. Assemble FloorPlanJSON ────────────────────────────────────────────
    n_units = len(brief.units)
    warnings_note = (
        " | ".join(prog_warnings[:2]) if prog_warnings else ""
    )
    rationale = (
        f"{_effective_floors}-storey, {n_units} unit(s), {target_stacking} stacking"
        + (f". Note: {warnings_note}" if warnings_note else "")
    )

    return FloorPlanJSON(
        metadata=FloorPlanMetadata(
            typology_label="Solver-generated",
            rationale=rationale[:400],
        ),
        storeys=storey_models,
        stairs=stairs,
    )


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _network_to_wall_models(
    network: WallNetwork,
    height_m: float,
) -> list[WallModel]:
    """Convert a WallNetwork (contracts) to WallModel list (ai/schema).

    WallSegment and WallModel share the same `type` Literal values and
    start/end/thickness_mm fields — only height_m / fire_rating_min are added.
    """
    result: list[WallModel] = []
    for seg in network.segments:
        result.append(WallModel(
            id=seg.id,
            start=seg.start,
            end=seg.end,
            type=seg.type,            # type: ignore[arg-type]
            thickness_mm=seg.thickness_mm,
            height_m=height_m,
        ))
    return result


def _make_stair_model(
    core: CoreSpec,
    target_floors: int,
    floor_to_floor_m: float,
) -> StairModel:
    """Build a StairModel from the core spec stair footprint."""
    s = core.stair_rect
    # OBC-minimum riser/tread (most compact)
    riser_mm = 200
    tread_mm = 235
    n_risers = math.ceil(floor_to_floor_m / (riser_mm / 1000))
    n_risers = max(2, min(25, n_risers))    # clamp to StairModel field bounds

    return StairModel(
        id="stair_core_0",
        footprint=[[s.x0, s.y0], [s.x1, s.y0], [s.x1, s.y1], [s.x0, s.y1]],
        from_level=0,
        to_level=min(target_floors - 1, 3),
        tread_count=n_risers,
        tread_mm=tread_mm,
        riser_mm=riser_mm,
        direction="up_north",
    )
