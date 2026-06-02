"""OBC Part 9 compliance check for placed stamps.

Reference: Ontario Building Code, Part 9 (Housing and Small Buildings), 2024 edition.
Section references below are to Part 9 unless otherwise noted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .rules.code_rules import ROOM_MIN_AREA_M2 as _MIN_AREA, ROOM_MIN_DIM_M as _MIN_DIM
from .typology.models import Cell
from .typology.selector import FitResult, PlacedCell

# Stair widths (§9.8.4.1)
_STAIR_MIN_WIDTH_M = 0.9      # clear width

# Only habitable rooms carry OBC Part 9 minimum area mandates.
# Stair/corridor/entry values in ROOM_MIN_AREA_M2 are design targets, not OBC requirements.
_HABITABLE_ROLES = frozenset({
    "bedroom", "master_bedroom", "living", "dining", "kitchen",
    "bathroom", "powder_room", "laundry",
})

# Egress window (§9.7.4): min opening 0.35 m² with min dim 380 mm
_EGRESS_MIN_AREA_M2 = 0.35
_EGRESS_MIN_DIM_M   = 0.38    # 380 mm

# Ceiling heights (§9.8.3.4)
_HABITABLE_MIN_HEIGHT_M = 2.4   # We cannot check z from 2D cells; flagged as assumption.
_BASEMENT_MIN_HEIGHT_M  = 1.95


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class OBCViolation:
    severity: Literal["error", "warning"]
    cell_role: str
    unit_id: int
    storey: int
    code_ref: str
    message: str


@dataclass
class OBCResult:
    pass_: bool
    violations: list[OBCViolation] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def add(self, severity, role, uid, storey, ref, msg):
        self.violations.append(
            OBCViolation(severity=severity, cell_role=role,
                         unit_id=uid, storey=storey,
                         code_ref=ref, message=msg)
        )
        if severity == "error":
            self.pass_ = False


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

def check_obc(fit: FitResult) -> OBCResult:
    """Run OBC Part 9 checks on a FitResult.

    Only 2D geometry is known; ceiling-height checks are noted as assumptions.
    """
    result = OBCResult(pass_=True)
    result.assumptions.append(
        "Ceiling heights assumed ≥ 2.4 m above grade and ≥ 1.95 m in basement "
        "(§9.8.3.4) — verify in 3D model."
    )
    result.assumptions.append(
        "Egress window openings assumed ≥ 0.35 m² with min 380 mm dimension "
        "(§9.7.4) — verify in architectural drawings."
    )

    placed = fit.placed_cells
    typology = fit.typology

    # Group cells by (unit_id, storey)
    by_unit: dict[int, list[PlacedCell]] = {}
    for pc in placed:
        by_unit.setdefault(pc.cell.unit_id, []).append(pc)

    # --- Room area / dimension checks ---
    for pc in placed:
        role = pc.cell.role
        uid  = pc.cell.unit_id
        s    = pc.cell.storey

        min_a = max(
            pc.cell.min_area_m2,
            _MIN_AREA.get(role, 0.0) if role in _HABITABLE_ROLES else 0.0,
        )
        min_d = max(pc.cell.min_dim_m,   _MIN_DIM.get(role, 0.0))

        if min_a > 0 and pc.area_m2 < min_a - 0.01:
            result.add(
                "error", role, uid, s,
                "OBC §9.8.3.1",
                f"Unit {uid} storey {s} {role}: area {pc.area_m2:.2f} m² < "
                f"minimum {min_a:.1f} m²"
            )

        if min_d > 0:
            if pc.width_m < min_d - 0.01 or pc.depth_m < min_d - 0.01:
                result.add(
                    "error", role, uid, s,
                    "OBC §9.8.3.2",
                    f"Unit {uid} storey {s} {role}: dimensions "
                    f"{pc.width_m:.2f}×{pc.depth_m:.2f} m — "
                    f"minimum dimension {min_d:.2f} m not met"
                )

    # --- Egress window marker (only flag cells that must have one but are too small) ---
    for pc in placed:
        if pc.cell.needs_egress_window:
            # We can't verify actual window in 2D; just check room is large enough to fit one
            if pc.area_m2 < 4.0:
                result.add(
                    "warning", pc.cell.role, pc.cell.unit_id, pc.cell.storey,
                    "OBC §9.7.4",
                    f"Unit {pc.cell.unit_id} storey {pc.cell.storey} "
                    f"{pc.cell.role}: room may be too small for egress window — verify."
                )

    # --- Each dwelling unit must have ≥1 bedroom with egress ---
    for uid, cells in by_unit.items():
        if uid < 0:   # shared/common areas
            continue
        has_bedroom = any(
            pc.cell.role in ("bedroom", "master_bedroom") for pc in cells
        )
        has_egress_bedroom = any(
            pc.cell.role in ("bedroom", "master_bedroom")
            and pc.cell.needs_egress_window
            for pc in cells
        )
        if not has_bedroom:
            result.add(
                "error", "bedroom", uid, -99,
                "OBC §9.8.3.3",
                f"Unit {uid} has no bedroom cell defined."
            )
        elif not has_egress_bedroom:
            result.add(
                "warning", "bedroom", uid, -99,
                "OBC §9.7.4",
                f"Unit {uid} has no bedroom marked needs_egress_window=True."
            )

    # --- Stair width ---
    for pc in placed:
        if pc.cell.role == "stair":
            if pc.width_m < _STAIR_MIN_WIDTH_M - 0.01 and pc.depth_m < _STAIR_MIN_WIDTH_M - 0.01:
                result.add(
                    "error", "stair", pc.cell.unit_id, pc.cell.storey,
                    "OBC §9.8.4.1",
                    f"Stair unit {pc.cell.unit_id} storey {pc.cell.storey}: "
                    f"clear width {min(pc.width_m, pc.depth_m):.2f} m < {_STAIR_MIN_WIDTH_M} m"
                )

    # --- Each above-grade unit must have living + kitchen ---
    for uid, cells in by_unit.items():
        if uid < 0:
            continue
        above_cells = [pc for pc in cells if pc.cell.storey >= 0]
        if not above_cells:
            continue
        roles = {pc.cell.role for pc in above_cells}
        for required in ("living", "kitchen"):
            if required not in roles:
                result.add(
                    "warning", required, uid, -99,
                    "OBC §9.8.3.3",
                    f"Unit {uid} is missing a '{required}' cell."
                )

    # --- Basement ceiling note ---
    for pc in placed:
        if pc.cell.storey == -1 and pc.cell.role not in ("mechanical", "storage", "void"):
            result.assumptions.append(
                f"Basement unit {pc.cell.unit_id} {pc.cell.role}: "
                f"ceiling height ≥ 1.95 m required (§9.8.3.4) — verify."
            )
            break   # one note is enough

    return result
