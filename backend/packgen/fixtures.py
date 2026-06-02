"""Deterministic parametric fixture and window placement for PackGen.

Adds architectural symbols to placed-cell floor plans:
  - Windows on exterior walls (role-specific width, 10% glazing OBC rule)
  - Stair tread lines + handrails + UP/DN annotation
  - Bathroom fixture layout (toilet, sink, shower/tub) by room area
  - Kitchen counter layout (straight or L-shape) by aspect ratio

All geometry is computed from PlacedCell coordinates (metres, CAD frame).
No LLM calls are made here. The rendering layer (dxf_writer, svg_preview)
applies any storey x-offsets when drawing.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Storey constants (match ifc_writer / dxf_writer)
# ---------------------------------------------------------------------------

_STOREY_HEIGHT: dict[int, float] = {-1: 2.4, 0: 3.0, 1: 2.7, 2: 2.7, 3: 2.7}
_DEFAULT_STOREY_H = 2.7
_WALL_T = 0.200   # exterior wall thickness used for glazing symbol depth


# ---------------------------------------------------------------------------
# Role sets
# ---------------------------------------------------------------------------

_NO_WINDOW_ROLES = frozenset({
    "stair", "corridor", "entry", "mechanical", "storage", "void", "balcony",
})
_BATHROOM_ROLES = frozenset({"bathroom", "powder_room"})


# ---------------------------------------------------------------------------
# Desired window width by role (OBC 10% natural light rule drives this)
# ---------------------------------------------------------------------------

_WINDOW_WIDTH: dict[str, float] = {
    "bedroom":        1.200,
    "master_bedroom": 1.200,
    "living":         1.800,
    "dining":         1.500,
    "kitchen":        0.900,
    "bathroom":       0.600,
    "powder_room":    0.600,
    "laundry":        0.600,
}
_DEFAULT_WINDOW_W = 0.900


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WindowSpec:
    """A window segment on a cell's exterior wall edge.

    For horizontal edges (bottom/top):  x0 < x1, y0 == y1 == wall y-coord.
    For vertical edges (left/right):    y0 < y1, x0 == x1 == wall x-coord.
    """
    x0: float
    y0: float
    x1: float
    y1: float
    edge: str       # "bottom" | "top" | "left" | "right"
    storey: int


@dataclass
class RectSymbol:
    """Rectangular fixture footprint in the CAD frame (metres)."""
    x: float        # lower-left corner x
    y: float        # lower-left corner y
    w: float        # width
    h: float        # height (depth)
    label: str
    storey: int


@dataclass
class StairLayout:
    """Stair tread lines, handrails, and UP/DN annotation."""
    treads:         list    # list of ((x0,y0),(x1,y1)) model-space segments
    handrails:      list    # same format
    annotation_pt:  tuple   # (x, y) for "UP" / "DN" text
    direction:      str     # "UP" | "DN"
    storey:         int


# ---------------------------------------------------------------------------
# Edge helpers (shared with ifc_writer logic but independent)
# ---------------------------------------------------------------------------

def _canonical_edges(pc) -> dict[str, tuple]:
    """Return the 4 canonical edge tuples for a PlacedCell rectangle."""
    x0, y0, x1, y1 = pc.x0, pc.y0, pc.x1, pc.y1
    return {
        "bottom": ((min(x0, x1), y0), (max(x0, x1), y0)),
        "top":    ((min(x0, x1), y1), (max(x0, x1), y1)),
        "left":   ((x0, min(y0, y1)), (x0, max(y0, y1))),
        "right":  ((x1, min(y0, y1)), (x1, max(y0, y1))),
    }


def extract_wall_edges(placed_cells) -> dict:
    """Return {(p0, p1): count} for all cell edges in placed_cells.

    count == 1  →  exterior (perimeter) wall
    count == 2  →  interior / shared wall between two cells
    """
    edge_counts: Counter = Counter()
    for pc in placed_cells:
        for edge_tuple in _canonical_edges(pc).values():
            edge_counts[edge_tuple] += 1
    return dict(edge_counts)


def get_exterior_edges_for_cell(pc, edge_counts: dict) -> list[str]:
    """Return which sides of pc are exterior walls (appear only once)."""
    cell_e = _canonical_edges(pc)
    return [name for name, key in cell_e.items() if edge_counts.get(key, 0) == 1]


# ---------------------------------------------------------------------------
# Window placement
# ---------------------------------------------------------------------------

def get_window_for_edge(pc, edge_name: str) -> WindowSpec | None:
    """Compute a single WindowSpec for one exterior edge of pc.

    The window is centered on the edge, width clamped to [600mm, 2400mm]
    and at most 60% of the edge length.  Returns None if the edge is too
    short to fit a minimum window.
    """
    desired_w = _WINDOW_WIDTH.get(pc.cell.role, _DEFAULT_WINDOW_W)

    if edge_name in ("bottom", "top"):
        edge_len = pc.x1 - pc.x0
        win_w = max(0.60, min(desired_w, edge_len * 0.60, 2.40))
        if edge_len < 0.80:
            return None
        cx = (pc.x0 + pc.x1) / 2
        wall_y = pc.y0 if edge_name == "bottom" else pc.y1
        return WindowSpec(
            x0=cx - win_w / 2, y0=wall_y,
            x1=cx + win_w / 2, y1=wall_y,
            edge=edge_name, storey=pc.cell.storey,
        )

    else:  # left / right
        edge_len = pc.y1 - pc.y0
        win_w = max(0.60, min(desired_w, edge_len * 0.60, 2.40))
        if edge_len < 0.80:
            return None
        cy = (pc.y0 + pc.y1) / 2
        wall_x = pc.x0 if edge_name == "left" else pc.x1
        return WindowSpec(
            x0=wall_x, y0=cy - win_w / 2,
            x1=wall_x, y1=cy + win_w / 2,
            edge=edge_name, storey=pc.cell.storey,
        )


def place_windows(storey_cells: list) -> list[WindowSpec]:
    """Return all WindowSpecs for a single storey's placed cells."""
    edge_counts = extract_wall_edges(storey_cells)
    result: list[WindowSpec] = []
    for pc in storey_cells:
        if pc.cell.role in _NO_WINDOW_ROLES:
            continue
        for edge_name in get_exterior_edges_for_cell(pc, edge_counts):
            win = get_window_for_edge(pc, edge_name)
            if win:
                result.append(win)
    return result


# ---------------------------------------------------------------------------
# Stair tread placement
# ---------------------------------------------------------------------------

def place_stair_treads(pc) -> StairLayout:
    """Return tread lines, handrails, and annotation for a stair cell."""
    s = pc.cell.storey
    storey_h = _STOREY_HEIGHT.get(s, _DEFAULT_STOREY_H)
    n_treads = max(3, int(storey_h / 0.275))

    w = pc.x1 - pc.x0
    d = pc.y1 - pc.y0
    treads: list = []
    handrails: list = []

    if w >= d:
        # Wide stair cell → treads run left-right (horizontal lines stepping in y)
        step = d / (n_treads + 1)
        for i in range(1, n_treads + 1):
            y = pc.y0 + i * step
            treads.append(((pc.x0, y), (pc.x1, y)))
        # Handrails along the long (horizontal) sides, 50 mm inset
        handrails.append(((pc.x0, pc.y0 + 0.05), (pc.x1, pc.y0 + 0.05)))
        handrails.append(((pc.x0, pc.y1 - 0.05), (pc.x1, pc.y1 - 0.05)))
        annotation_pt = ((pc.x0 + pc.x1) / 2, pc.y0 + step * 0.6)
    else:
        # Tall stair cell → treads run up-down (vertical lines stepping in x)
        step = w / (n_treads + 1)
        for i in range(1, n_treads + 1):
            x = pc.x0 + i * step
            treads.append(((x, pc.y0), (x, pc.y1)))
        # Handrails along the long (vertical) sides, 50 mm inset
        handrails.append(((pc.x0 + 0.05, pc.y0), (pc.x0 + 0.05, pc.y1)))
        handrails.append(((pc.x1 - 0.05, pc.y0), (pc.x1 - 0.05, pc.y1)))
        annotation_pt = (pc.x0 + step * 0.6, (pc.y0 + pc.y1) / 2)

    direction = "DN" if s > 0 else "UP"
    return StairLayout(
        treads=treads, handrails=handrails,
        annotation_pt=annotation_pt,
        direction=direction, storey=s,
    )


# ---------------------------------------------------------------------------
# Bathroom fixture placement
# ---------------------------------------------------------------------------

def place_bathroom_fixtures(pc) -> list[RectSymbol]:
    """Return toilet, sink, shower, tub symbols sized to room area.

    Small  < 4 m²: toilet + sink
    Full  4–7 m²: toilet + sink + shower
    Master ≥ 7 m²: toilet + double sink + bathtub + shower
    All fixtures placed against the back wall (maximum y edge of cell).
    Symbols are clamped to cell bounds; any that don't fit are omitted.
    """
    area = (pc.x1 - pc.x0) * (pc.y1 - pc.y0)
    x0, y0, x1, y1 = pc.x0, pc.y0, pc.x1, pc.y1
    s = pc.cell.storey
    raw: list[RectSymbol] = []

    if area < 4.0:
        # toilet + wall-hung sink
        raw.append(RectSymbol(x=x0 + 0.05, y=y1 - 0.75, w=0.50, h=0.70, label="WC",   storey=s))
        raw.append(RectSymbol(x=x1 - 0.45, y=y1 - 0.30, w=0.40, h=0.25, label="SINK", storey=s))
    elif area < 7.0:
        # toilet + pedestal sink + shower
        raw.append(RectSymbol(x=x0 + 0.05, y=y1 - 0.75, w=0.50, h=0.70, label="WC",   storey=s))
        raw.append(RectSymbol(x=x0 + 0.60, y=y1 - 0.50, w=0.45, h=0.45, label="SINK", storey=s))
        raw.append(RectSymbol(x=x1 - 0.95, y=y1 - 0.95, w=0.90, h=0.90, label="SHW",  storey=s))
    else:
        # toilet + double vanity sink + bathtub + shower
        raw.append(RectSymbol(x=x0 + 0.05, y=y1 - 0.75, w=0.50, h=0.70, label="WC",     storey=s))
        raw.append(RectSymbol(x=x0 + 0.60, y=y1 - 0.55, w=0.90, h=0.50, label="SINK×2", storey=s))
        raw.append(RectSymbol(x=x1 - 1.75, y=y0 + 0.05, w=1.70, h=0.80, label="BATH",   storey=s))
        raw.append(RectSymbol(x=x1 - 0.95, y=y1 - 0.95, w=0.90, h=0.90, label="SHW",    storey=s))

    return _clamp_symbols(raw, x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# Kitchen fixture placement
# ---------------------------------------------------------------------------

def place_kitchen_fixtures(pc) -> list[RectSymbol]:
    """Return counter, sink, cooktop, fridge symbols.

    Aspect ratio > 1.5 (wide or tall) → straight counter along long wall.
    Otherwise → L-shape counter on two walls.
    """
    x0, y0, x1, y1 = pc.x0, pc.y0, pc.x1, pc.y1
    w = x1 - x0
    d = y1 - y0
    s = pc.cell.storey
    CD = 0.60   # counter depth
    raw: list[RectSymbol] = []

    if w / max(d, 0.01) > 1.5:
        # Wide cell → straight counter along back wall (y1)
        raw.append(RectSymbol(x=x0 + 0.05, y=y1 - CD, w=w - 0.10, h=CD,   label="CTR",  storey=s))
        sink_w = 0.50
        sk_x = x0 + w / 2 - sink_w / 2
        raw.append(RectSymbol(x=sk_x,       y=y1 - CD + 0.05, w=sink_w, h=0.40, label="SNK",  storey=s))
        raw.append(RectSymbol(x=sk_x + sink_w + 0.05, y=y1 - CD + 0.05, w=0.60, h=0.55, label="COOK", storey=s))
        raw.append(RectSymbol(x=x0 + 0.05,  y=y0 + 0.05, w=0.70, h=0.80, label="FRDG", storey=s))

    elif d / max(w, 0.01) > 1.5:
        # Tall cell → straight counter along right wall (x1)
        raw.append(RectSymbol(x=x1 - CD, y=y0 + 0.05, w=CD,   h=d - 0.10, label="CTR",  storey=s))
        sink_h = 0.40
        sk_y = y0 + d / 2 - sink_h / 2
        raw.append(RectSymbol(x=x1 - CD + 0.05, y=sk_y, w=0.50, h=sink_h, label="SNK",  storey=s))
        raw.append(RectSymbol(x=x1 - CD + 0.05, y=sk_y + sink_h + 0.05, w=0.55, h=0.60, label="COOK", storey=s))
        raw.append(RectSymbol(x=x0 + 0.05, y=y0 + 0.05, w=0.70, h=0.80, label="FRDG",  storey=s))

    else:
        # Roughly square → L-shape: back wall + right wall
        raw.append(RectSymbol(x=x0 + 0.05, y=y1 - CD, w=w - CD - 0.05, h=CD, label="CTR",  storey=s))
        raw.append(RectSymbol(x=x1 - CD,   y=y0 + 0.05, w=CD, h=d - CD - 0.05, label="CTR", storey=s))
        # Sink in corner of L
        raw.append(RectSymbol(x=x1 - CD + 0.05, y=y1 - CD + 0.05, w=0.50, h=0.50, label="SNK",  storey=s))
        # Cooktop on back segment
        raw.append(RectSymbol(x=x0 + 0.10, y=y1 - CD + 0.05, w=0.60, h=0.55, label="COOK", storey=s))
        # Fridge at front-left
        raw.append(RectSymbol(x=x0 + 0.05, y=y0 + 0.05, w=0.70, h=0.80, label="FRDG", storey=s))

    return _clamp_symbols(raw, x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp_symbols(
    symbols: list[RectSymbol],
    x0: float, y0: float, x1: float, y1: float,
) -> list[RectSymbol]:
    """Drop any symbol whose footprint falls (partially or fully) outside the cell."""
    ok = []
    for s in symbols:
        if (s.x >= x0 - 1e-4 and s.y >= y0 - 1e-4 and
                s.x + s.w <= x1 + 1e-4 and s.y + s.h <= y1 + 1e-4):
            ok.append(s)
    return ok
