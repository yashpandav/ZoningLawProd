"""Geometry and OBC validation for LLM-generated FloorPlanJSON.

All checks return a ``ValidationReport`` — the fallback chain uses ``.valid``
to decide whether to retry or fall through to the template placer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .schema import FloorPlanJSON, StoreyModel, WallModel


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def __str__(self) -> str:
        lines = [f"ERRORS ({len(self.errors)}):"] + [f"  - {e}" for e in self.errors]
        if self.warnings:
            lines += [f"WARNINGS ({len(self.warnings)}):"] + [f"  - {w}" for w in self.warnings]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wall_length(wall: WallModel) -> float:
    dx = wall.end[0] - wall.start[0]
    dy = wall.end[1] - wall.start[1]
    return math.hypot(dx, dy)


def _wall_as_line(wall: WallModel) -> LineString:
    return LineString([wall.start, wall.end])


def _room_polygon(room_coords: list[list[float]]) -> Polygon:
    return Polygon(room_coords)


def _all_wall_ids(storey: StoreyModel) -> set[str]:
    return {w.id for w in storey.walls}


def _endpoint_connected(walls: list[WallModel], tol: float = 0.05) -> bool:
    """Check that every wall endpoint is shared by at least one other wall (within tolerance)."""
    endpoints: list[tuple[float, float]] = []
    for w in walls:
        endpoints.append((w.start[0], w.start[1]))
        endpoints.append((w.end[0], w.end[1]))

    for ep in endpoints:
        shared = sum(
            1 for w in walls
            if (
                math.hypot(w.start[0] - ep[0], w.start[1] - ep[1]) < tol
                or math.hypot(w.end[0] - ep[0], w.end[1] - ep[1]) < tol
            )
        )
        if shared < 2:
            return False
    return True


# ---------------------------------------------------------------------------
# Per-storey checks
# ---------------------------------------------------------------------------

def _check_storey(storey: StoreyModel, envelope: Polygon, report: ValidationReport) -> None:
    prefix = f"[level {storey.level}]"

    # 1. Wall references
    wall_ids = _all_wall_ids(storey)
    for d in storey.doors:
        if d.wall_id not in wall_ids:
            report.errors.append(f"{prefix} door {d.id} references unknown wall_id {d.wall_id!r}")
    for w in storey.windows:
        if w.wall_id not in wall_ids:
            report.errors.append(f"{prefix} window {w.id} references unknown wall_id {w.wall_id!r}")

    # 2. Room polygons valid + inside envelope
    room_ids = {r.id for r in storey.rooms}
    for room in storey.rooms:
        poly = _room_polygon(room.polygon)
        if not poly.is_valid:
            report.errors.append(f"{prefix} room {room.id} has invalid polygon")
            continue
        if poly.area < 0.5:
            report.errors.append(f"{prefix} room {room.id} area {poly.area:.2f} m2 is suspiciously small")

        # OBC minimum area checks
        if room.category == "bedroom" and poly.area < 6.0:
            report.errors.append(
                f"{prefix} bedroom {room.id} area {poly.area:.2f} m2 < 6.0 m2 OBC minimum"
            )
        elif room.category in ("living", "dining") and poly.area < 13.5:
            report.errors.append(
                f"{prefix} living room {room.id} area {poly.area:.2f} m2 < 13.5 m2 OBC minimum"
            )
        elif room.category == "kitchen" and poly.area < 4.2:
            report.errors.append(
                f"{prefix} kitchen {room.id} area {poly.area:.2f} m2 < 4.2 m2 OBC minimum"
            )

        if envelope is not None and not envelope.buffer(0.5).contains(poly.centroid):
            report.warnings.append(
                f"{prefix} room {room.id} centroid outside buildable envelope"
            )

    # 3. Every room (except balcony) must have at least one door connecting to it
    rooms_with_doors: set[str] = set()
    for d in storey.doors:
        for rid in d.connects_rooms:
            if rid != "outside":
                rooms_with_doors.add(rid)
    for room in storey.rooms:
        if room.category not in ("balcony",) and room.id not in rooms_with_doors:
            report.errors.append(f"{prefix} room {room.id!r} has no connecting door")

    # 4. Door connects_rooms references valid room ids
    for d in storey.doors:
        for rid in d.connects_rooms:
            if rid != "outside" and rid not in room_ids:
                report.errors.append(
                    f"{prefix} door {d.id} connects to unknown room {rid!r}"
                )

    # 5. Bedroom egress window check (OBC §9.9.10.1)
    bedroom_ids = {r.id for r in storey.rooms if r.category == "bedroom"}
    # Map wall_id → exterior status (approximate: exterior or not party)
    exterior_wall_ids = {
        w.id for w in storey.walls if w.type in ("exterior",)
    }
    # For each bedroom, check for an exterior egress-compliant window
    bedroom_window_map: dict[str, list] = {bid: [] for bid in bedroom_ids}
    for win in storey.windows:
        if win.wall_id in exterior_wall_ids:
            for room in storey.rooms:
                if room.category == "bedroom":
                    # Heuristic: the window is associated with this bedroom if the room polygon
                    # is near the wall it references
                    for w in storey.walls:
                        if w.id == win.wall_id:
                            wall_line = _wall_as_line(w)
                            room_poly = _room_polygon(room.polygon)
                            if room_poly.distance(wall_line) < 0.5:
                                bedroom_window_map[room.id].append(win)

    for bid in bedroom_ids:
        wins = bedroom_window_map.get(bid, [])
        has_egress = any(
            w.egress_compliant
            and (w.clear_opening_m2 is None or w.clear_opening_m2 >= 0.35)
            and w.sill_m <= 1.0
            for w in wins
        )
        if not has_egress:
            report.errors.append(
                f"{prefix} bedroom {bid} missing OBC-compliant egress window "
                f"(need egress_compliant=true, clear_opening_m2>=0.35, sill_m<=1.0)"
            )

    # 6. Wall self-intersection (basic check: no wall crosses another)
    lines = [_wall_as_line(w) for w in storey.walls]
    for i, l1 in enumerate(lines):
        for j, l2 in enumerate(lines[i + 1:], start=i + 1):
            if l1.crosses(l2):
                report.errors.append(
                    f"{prefix} walls {storey.walls[i].id} and {storey.walls[j].id} cross each other"
                )


# ---------------------------------------------------------------------------
# Multi-storey checks
# ---------------------------------------------------------------------------

def _check_stairs(plan: FloorPlanJSON, report: ValidationReport) -> None:
    if len(plan.storeys) > 1 and not plan.stairs:
        report.errors.append("multi-storey plan has no stair defined")

    for stair in plan.stairs:
        if stair.tread_mm < 235:
            report.errors.append(f"stair {stair.id} tread {stair.tread_mm}mm < 235mm OBC minimum")
        if not (125 <= stair.riser_mm <= 200):
            report.errors.append(f"stair {stair.id} riser {stair.riser_mm}mm outside 125-200mm range")


def _check_height(plan: FloorPlanJSON, max_height_m: float, report: ValidationReport) -> None:
    if max_height_m <= 0:
        return
    for storey in plan.storeys:
        top = storey.elevation_m + storey.floor_to_floor_m
        if top > max_height_m + 0.1:
            report.errors.append(
                f"Level {storey.level} top elevation {top:.2f}m exceeds max_height_m {max_height_m:.2f}m"
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_plan(
    plan: FloorPlanJSON,
    envelope: Polygon | None = None,
    max_height_m: float = 0.0,
) -> ValidationReport:
    """Validate a FloorPlanJSON against geometry rules and OBC minimums.

    Args:
        plan:          Parsed and schema-validated FloorPlanJSON.
        envelope:      Buildable envelope polygon in local CAD frame (optional).
        max_height_m:  Maximum building height from zoning; 0 to skip.

    Returns:
        ValidationReport with .valid = True if no errors.
    """
    report = ValidationReport()

    if not plan.storeys:
        report.errors.append("plan has no storeys")
        return report

    for storey in plan.storeys:
        _check_storey(storey, envelope, report)

    _check_stairs(plan, report)
    if max_height_m > 0:
        _check_height(plan, max_height_m, report)

    return report
