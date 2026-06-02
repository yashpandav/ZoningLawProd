"""DXF export: AIA NCS layer structure, by-law annotations, modelspace + paper layouts.

Output: DXF R2018 (.dxf) written to a file path or returned as bytes.

AIA / NCS layers (U.S. National CAD Standard):
  A-SITE-BLDG   — lot boundary (yellow/2, heavy)
  A-FLOR-OTLN   — buildable envelope outline (white/7, heavy)
  A-WALL-FULL   — exterior walls full-height (white/7, heavy)
  A-WALL-INTR   — interior partitions (white/7, light)
  A-WALL-FIRE   — fire-rated demising walls between units (red/1, medium)
  A-WALL-PATT   — wall hatch fills (grey/8)
  A-DOOR        — door leaf + swing arc (yellow/2)
  A-DOOR-IDEN   — door tags / pocket door (yellow/2)
  A-GLAZ        — window glazing (cyan/4)
  A-GLAZ-SILL   — window sill lines (cyan/4)
  A-FLOR-ROOM   — room hatch fills for colour-coding (cyan/4)
  A-FLOR-IDEN   — room labels with area (green/3)
  A-FLOR-STRS   — stair treads and handrails (blue/5)
  A-FLOR-FIXT   — bathroom / kitchen fixture outlines (colour 11)
  A-ANNO-TEXT   — general text annotations (green/3)
  A-ANNO-DIMS   — dimension chains (yellow/2)
  A-ANNO-TTLB   — title block text (white/7)
  A-ANNO-NPLT   — non-plotting construction lines (grey/8, no-plot)
  A-AREA        — area calculation boundary (cyan/4)
  A-ROOF-OTLN   — roof outline (white/7, heavy)
  A-SITE-INFO   — setback lines + annotations (magenta/6, dashed)
  A-BLAW-NOTE   — by-law section citations (red/1)
  A-DISC        — non-plotting AI disclosure + provenance (grey/8)
"""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Optional, Union

import ezdxf
from ezdxf import colors
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from ezdxf.layouts import Modelspace
from shapely.geometry import box as _shapely_box
from shapely.ops import unary_union as _shapely_union

from .fixtures import (
    extract_wall_edges,
    get_exterior_edges_for_cell,
    get_window_for_edge,
    place_stair_treads,
    place_bathroom_fixtures,
    place_kitchen_fixtures,
    WindowSpec,
    RectSymbol,
    StairLayout,
)
from .geometry import EnvelopeResult
from .obc import OBCResult
from .typology.models import Cell
from .typology.selector import FitResult, PlacedCell


# ---------------------------------------------------------------------------
# Wall & door constants
# ---------------------------------------------------------------------------

_WALL_T_EXT   = 0.200   # exterior wall thickness (m)
_WALL_T_PART  = 0.075   # interior partition thickness (m)
_WALL_T_DEMIS = 0.100   # demising (fire-rated) wall between units (m)

_DOOR_ROLES = {
    "entry":          0.900,
    "living":         0.900,
    "bedroom":        0.800,
    "master_bedroom": 0.800,
    "bathroom":       0.700,
    "powder_room":    0.700,
}


# ---------------------------------------------------------------------------
# Layer definitions  (name, color_index, lineweight_mm*100, plot)
# ---------------------------------------------------------------------------

_LAYERS = [
    # name                 ACI color        lw   plot
    ("A-SITE-BLDG",      2,               25,  True),   # yellow per NCS
    ("A-FLOR-OTLN",      colors.WHITE,    50,  True),
    ("A-WALL-FULL",      colors.WHITE,    50,  True),   # exterior walls (was A-FLOR-WALL)
    ("A-WALL-INTR",      colors.WHITE,    13,  True),   # interior partitions (was A-FLOR-WALL-INTR)
    ("A-WALL-FIRE",      colors.RED,      25,  True),   # fire-rated demising (was A-WALL-FIRE-RATD)
    ("A-WALL-PATT",      8,                0,  True),   # wall hatches
    ("A-DOOR",           2,                0,  True),   # door leaf + swing arc (yellow)
    ("A-DOOR-IDEN",      2,                0,  True),   # door tags / pocket doors (yellow)
    ("A-GLAZ",           colors.CYAN,      0,  True),
    ("A-GLAZ-SILL",      colors.CYAN,      0,  True),   # window sill lines
    ("A-FLOR-ROOM",      colors.CYAN,      0,  True),
    ("A-FLOR-IDEN",      colors.GREEN,     0,  True),   # room labels with area
    ("A-FLOR-STRS",      5,                0,  True),   # stair treads/handrails (blue)
    ("A-FLOR-FIXT",      11,               0,  True),   # fixtures (ACI 11 per NCS)
    ("A-ANNO-TEXT",      colors.GREEN,     0,  True),
    ("A-ANNO-DIMS",      2,                0,  True),   # dimensions yellow per NCS
    ("A-ANNO-TTLB",      colors.WHITE,    13,  True),
    ("A-ANNO-NPLT",      8,                0,  False),  # non-plotting construction lines
    ("A-AREA",           colors.CYAN,      0,  True),   # area calc boundary
    ("A-ROOF-OTLN",      colors.WHITE,    50,  True),   # roof outline
    ("A-SITE-INFO",      colors.MAGENTA,  13,  True),
    ("A-BLAW-NOTE",      colors.RED,       0,  True),
    ("A-DISC",           8,                0,  False),  # non-plotting disclosure
    ("3D_MASSING",       8,                0,  False),
]

# Room-role → ACI color index for hatch fill
_ROLE_COLOR: dict[str, int] = {
    "bedroom":        150,
    "master_bedroom": 140,
    "living":         30,
    "dining":         40,
    "kitchen":        50,
    "bathroom":       170,
    "powder_room":    170,
    "laundry":        180,
    "stair":          8,
    "corridor":       9,
    "entry":          9,
    "mechanical":     8,
    "storage":        8,
    "balcony":        3,
    "void":           0,
}

# Role labels for room annotations (full English names for AutoCAD readability)
_ROLE_ABBREV: dict[str, str] = {
    "bedroom":        "Bedroom",
    "master_bedroom": "Master Bedroom",
    "living":         "Living Room",
    "dining":         "Dining Room",
    "kitchen":        "Kitchen",
    "bathroom":       "Bathroom",
    "powder_room":    "Powder Room",
    "laundry":        "Laundry",
    "stair":          "Stair",
    "corridor":       "Corridor",
    "entry":          "Entry",
    "mechanical":     "Mechanical",
    "storage":        "Storage",
    "balcony":        "Balcony",
    "home_office":    "Home Office",
    "study":          "Study",
    "mudroom":        "Mudroom",
    "void":           "",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_layers(doc: Drawing) -> None:
    lt = doc.layers
    for name, color, lw, plot in _LAYERS:
        if name not in lt:
            layer = lt.new(name)
        else:
            layer = lt.get(name)
        layer.color = color
        layer.lineweight = lw
        layer.plot = plot


def _add_lwpolyline_closed(msp: Modelspace, points: list[tuple], layer: str) -> None:
    msp.add_lwpolyline(points, close=True, dxfattribs={"layer": layer})


def _add_hatch(msp: Modelspace, points: list[tuple], color: int, layer: str) -> None:
    hatch = msp.add_hatch(color=color, dxfattribs={"layer": layer})
    hatch.paths.add_polyline_path(points, is_closed=True)
    hatch.set_solid_fill()


def _add_text(msp, text: str, x: float, y: float,
              height: float, layer: str, color: int = colors.BYLAYER) -> None:
    attribs: dict = {"layer": layer, "height": height}
    if color != colors.BYLAYER:
        attribs["color"] = color
    msp.add_text(text, dxfattribs=attribs).set_placement(
        (x, y), align=TextEntityAlignment.MIDDLE_CENTER
    )


def _add_dim_linear(msp, p1, p2, offset: float, layer: str,
                    citation: str = "") -> None:
    """Linear dimension. Detects horizontal vs vertical and positions base correctly."""
    override: dict = {"dimtxt": 0.22, "dimasz": 0.18}
    if citation:
        override["dimpost"] = f"{citation}\n<>"
    dx = abs(p2[0] - p1[0])
    dy = abs(p2[1] - p1[1])
    if dx >= dy:
        # Horizontal → base line goes below
        base = (p1[0], min(p1[1], p2[1]) + offset)
    else:
        # Vertical → base line goes to the left
        base = (min(p1[0], p2[0]) + offset, p1[1])
    dim = msp.add_linear_dim(
        base=base, p1=p1, p2=p2,
        dimstyle="EZDXF",
        override=override,
        dxfattribs={"layer": layer},
    )
    dim.render()


def _overlap_1d(a0: float, a1: float, b0: float, b1: float) -> float:
    """Length of overlap between two 1-D intervals."""
    return max(0.0, min(a1, b1) - max(a0, b0))


# ---------------------------------------------------------------------------
# Double-line wall drawing
# ---------------------------------------------------------------------------

def _draw_wall_pair(msp, pts_outer: list[tuple], wall_t: float,
                    layer_outer: str, layer_inner: str) -> None:
    """Draw outer + inner polylines for double-line wall representation."""
    _add_lwpolyline_closed(msp, pts_outer, layer_outer)
    # Build inset polygon (offset all coords inward by wall_t)
    xs = [p[0] for p in pts_outer]
    ys = [p[1] for p in pts_outer]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    w, h = x1 - x0, y1 - y0
    t = wall_t
    if w > 2 * t and h > 2 * t:
        inner = [
            (x0 + t, y0 + t),
            (x1 - t, y0 + t),
            (x1 - t, y1 - t),
            (x0 + t, y1 - t),
        ]
        _add_lwpolyline_closed(msp, inner, layer_inner)


def _draw_door_symbol(msp, hinge_x: float, hinge_y: float,
                      door_w: float, swing_deg: float, layer: str) -> None:
    """Draw a door: straight panel line + quarter-circle arc.

    hinge_x/y: corner where door is hinged.
    swing_deg: CCW angle of swing (0 = door panel along +X axis).
    """
    rad = math.radians(swing_deg)
    # Door panel line: hinge → open position
    panel_end_x = hinge_x + door_w * math.cos(rad)
    panel_end_y = hinge_y + door_w * math.sin(rad)
    msp.add_line(
        (hinge_x, hinge_y), (panel_end_x, panel_end_y),
        dxfattribs={"layer": layer},
    )
    # Arc from closed position (swing_deg+90) back to open (swing_deg)
    start_a = swing_deg
    end_a   = swing_deg + 90.0
    msp.add_arc(
        center=(hinge_x, hinge_y),
        radius=door_w,
        start_angle=start_a,
        end_angle=end_a,
        dxfattribs={"layer": layer},
    )


def _choose_door_swing(
    pc: "PlacedCell",
    storey_cells: list,
    x_off: float,
    front_y: float,
) -> "tuple[float, float, float] | None":
    """Return (hinge_x, hinge_y, swing_deg) for pc's door, or None if too small.

    Face priority:
      1. South face (y0) when cell is on the front row of the storey.
      2. Any face shared with an adjacent corridor / stair / entry cell.
      3. West face (x0) as default fallback.

    swing_deg is the CCW angle of the open-position door panel; the arc always
    sweeps +90° inward (into the current room).
    """
    door_w = _DOOR_ROLES.get(pc.cell.role)
    if door_w is None:
        return None

    w = pc.x1 - pc.x0
    d = pc.y1 - pc.y0
    if w < door_w + 0.3 or d < door_w + 0.3:
        return None

    T   = _WALL_T_EXT
    ax0 = pc.x0 + x_off
    ax1 = pc.x1 + x_off
    EPS = 0.02

    # Pre-compute the four face tuples: (hinge_x, hinge_y, swing_deg)
    # Each hinge is at the "push" corner — the panel lies along the wall, arc sweeps INTO room.
    face_south = (ax0 + T, pc.y0 + T,  0.0)   # south wall → panel +X, arc → +Y (into room)
    face_north = (ax1 - T, pc.y1 - T, 180.0)  # north wall → panel -X, arc → -Y (into room)
    face_east  = (ax1 - T, pc.y0 + T,  90.0)  # east wall  → panel +Y, arc → -X (into room)
    face_west  = (ax0 + T, pc.y1 - T, 270.0)  # west wall  → panel -Y, arc → +X (into room)

    # Priority 1: front row — door faces the street (south wall)
    if abs(pc.y0 - front_y) < EPS:
        return face_south

    # Priority 2: any adjacent circulation cell
    _CIRC = {"corridor", "stair", "entry"}
    for other in storey_cells:
        if other is pc or other.cell.role not in _CIRC:
            continue
        # South face of pc abuts north face of other?
        if (abs(pc.y0 - other.y1) < EPS
                and _overlap_1d(pc.x0, pc.x1, other.x0, other.x1) >= door_w * 0.5):
            return face_south
        # North face of pc abuts south face of other?
        if (abs(pc.y1 - other.y0) < EPS
                and _overlap_1d(pc.x0, pc.x1, other.x0, other.x1) >= door_w * 0.5):
            return face_north
        # East face of pc abuts west face of other?
        if (abs(pc.x1 - other.x0) < EPS
                and _overlap_1d(pc.y0, pc.y1, other.y0, other.y1) >= door_w * 0.5):
            return face_east
        # West face of pc abuts east face of other?
        if (abs(pc.x0 - other.x1) < EPS
                and _overlap_1d(pc.y0, pc.y1, other.y0, other.y1) >= door_w * 0.5):
            return face_west

    # Priority 3: default to west face
    return face_west


# ---------------------------------------------------------------------------
# Cell → polygons per storey
# ---------------------------------------------------------------------------

def _placed_to_pts(pc: PlacedCell) -> list[tuple]:
    """Room outline in local coords. Uses real polygon when present (fallback: AABB)."""
    if pc.polygon:
        return list(pc.polygon)
    return [(pc.x0, pc.y0), (pc.x1, pc.y0), (pc.x1, pc.y1), (pc.x0, pc.y1)]


def _room_outline_pts(pc: PlacedCell, x_off: float = 0.0) -> list[tuple]:
    """Room outline shifted by x_off for side-by-side storey layout in modelspace."""
    if pc.polygon:
        return [(x + x_off, y) for x, y in pc.polygon]
    return [
        (pc.x0 + x_off, pc.y0), (pc.x1 + x_off, pc.y0),
        (pc.x1 + x_off, pc.y1), (pc.x0 + x_off, pc.y1),
    ]


def _cell_shapely(pc: PlacedCell, x_off: float = 0.0):
    """Shapely Polygon for a cell (real outline or AABB); used for union/intersection."""
    from shapely.geometry import Polygon as _Poly
    return _Poly(_room_outline_pts(pc, x_off))


# ---------------------------------------------------------------------------
# WallNetwork-path drawing helpers (solver route)
# ---------------------------------------------------------------------------

def _draw_wall_segment(
    msp: Modelspace,
    p0: tuple,
    p1: tuple,
    seg_type: str,
) -> None:
    """Draw one WallSegment using the correct layer and line style.

    Exterior walls → double parallel lines on A-WALL-FULL.
    Party walls → single line on A-WALL-FIRE.
    Interior partitions / loadbearing → single line on A-WALL-INTR.
    This produces ONE graphical object per segment (no doubled walls).
    """
    if seg_type == "exterior":
        x0, y0 = p0
        x1, y1 = p1
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            return
        t = _WALL_T_EXT / 2
        nx = -(y1 - y0) / length * t
        ny =  (x1 - x0) / length * t
        msp.add_line((x0 + nx, y0 + ny), (x1 + nx, y1 + ny),
                     dxfattribs={"layer": "A-WALL-FULL"})
        msp.add_line((x0 - nx, y0 - ny), (x1 - nx, y1 - ny),
                     dxfattribs={"layer": "A-WALL-FULL"})
    elif seg_type == "party":
        msp.add_line(p0, p1, dxfattribs={"layer": "A-WALL-FIRE"})
    else:   # interior_partition, interior_loadbearing
        msp.add_line(p0, p1, dxfattribs={"layer": "A-WALL-INTR"})


def _draw_wall_network_for_storey(
    msp: Modelspace,
    segments: list,
    x_off: float,
) -> None:
    """Draw all WallSegments for one storey, each segment exactly once."""
    for seg in segments:
        p0 = (seg.start[0] + x_off, seg.start[1])
        p1 = (seg.end[0]   + x_off, seg.end[1])
        _draw_wall_segment(msp, p0, p1, seg.type)


# ---------------------------------------------------------------------------
# Main DXF builder
# ---------------------------------------------------------------------------

_3D_STOREY_HEIGHT: dict[int, float] = {-1: 2.4, 0: 3.0, 1: 2.7, 2: 2.7, 3: 2.7}
_3D_DEFAULT_H = 2.7


def _storey_elevation_3d(storey: int) -> float:
    if storey == -1:
        return -2.4
    if storey == 0:
        return 0.0
    return 3.0 + (storey - 1) * _3D_DEFAULT_H


def _add_room_box_3d(msp: Modelspace, x0: float, y0: float, x1: float, y1: float,
                     z0: float, z1: float, color_aci: int) -> None:
    """Add a 3-D box for one room using six 3DFace quads."""
    verts = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),  # bottom ring
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),  # top ring
    ]
    faces = [
        (0, 1, 2, 3),  # floor
        (4, 5, 6, 7),  # ceiling
        (0, 1, 5, 4),  # front wall
        (1, 2, 6, 5),  # right wall
        (2, 3, 7, 6),  # rear wall
        (3, 0, 4, 7),  # left wall
    ]
    for f in faces:
        pts = [verts[i] for i in f]
        face = msp.add_3dface(pts)
        face.dxf.layer = "3D_MASSING"
        face.dxf.color = color_aci


def _draw_3d_massing(msp: Modelspace, fit: FitResult) -> None:
    """Add extruded 3-D room boxes to the 3D_MASSING layer.

    Storeys are stacked at their actual elevations so the model appears as a
    real building volume when the AutoCAD viewport is switched to isometric.
    """
    for pc in fit.placed_cells:
        s = pc.cell.storey
        z0 = _storey_elevation_3d(s)
        z1 = z0 + _3D_STOREY_HEIGHT.get(s, _3D_DEFAULT_H)
        color_aci = _ROLE_COLOR.get(pc.cell.role, 8)
        _add_room_box_3d(msp, pc.x0, pc.y0, pc.x1, pc.y1, z0, z1, color_aci)


# ---------------------------------------------------------------------------
# Architectural symbol drawing helpers
# ---------------------------------------------------------------------------

_WIN_INNER_OFFSET = 0.07   # 70mm from exterior wall face to first glass line
_WIN_GAP          = 0.06   # 60mm air gap between glass panes
_WIN_OUTER_OFFSET = _WIN_INNER_OFFSET + _WIN_GAP   # 130mm to second glass line


def _draw_window_glazing(msp: Modelspace, win: WindowSpec, x_off: float) -> None:
    """Draw a double-glazing window symbol on A-GLAZ layer.

    Two parallel lines (glass panes) + two short perpendicular jamb lines.
    The lines are placed within the exterior wall thickness (200 mm).
    """
    attribs = {"layer": "A-GLAZ", "color": colors.CYAN}
    e = win.edge

    if e in ("bottom", "top"):
        wx0 = win.x0 + x_off
        wx1 = win.x1 + x_off
        wall_y = win.y0   # y0 == y1 for horizontal windows
        sign = 1.0 if e == "bottom" else -1.0   # positive = into the cell
        g1 = wall_y + sign * _WIN_INNER_OFFSET
        g2 = wall_y + sign * _WIN_OUTER_OFFSET
        msp.add_line((wx0, g1), (wx1, g1), dxfattribs=attribs)
        msp.add_line((wx0, g2), (wx1, g2), dxfattribs=attribs)
        msp.add_line((wx0, g1), (wx0, g2), dxfattribs=attribs)  # left jamb
        msp.add_line((wx1, g1), (wx1, g2), dxfattribs=attribs)  # right jamb
    else:
        wy0 = win.y0
        wy1 = win.y1
        wall_x = win.x0   # x0 == x1 for vertical windows
        sign = 1.0 if e == "left" else -1.0   # positive = into the cell
        g1 = wall_x + sign * _WIN_INNER_OFFSET
        g2 = wall_x + sign * _WIN_OUTER_OFFSET
        msp.add_line((g1, wy0), (g1, wy1), dxfattribs=attribs)
        msp.add_line((g2, wy0), (g2, wy1), dxfattribs=attribs)
        msp.add_line((g1, wy0), (g2, wy0), dxfattribs=attribs)  # bottom jamb
        msp.add_line((g1, wy1), (g2, wy1), dxfattribs=attribs)  # top jamb


def _draw_stair_layout_dxf(msp: Modelspace, layout: StairLayout, x_off: float) -> None:
    """Draw stair treads (solid), handrails (dashed), and UP/DN annotation."""
    tread_attr  = {"layer": "A-FLOR-STRS", "color": 5}
    rail_attr   = {"layer": "A-FLOR-STRS", "color": 5, "linetype": "DASHED"}
    for (x0, y0), (x1, y1) in layout.treads:
        msp.add_line((x0 + x_off, y0), (x1 + x_off, y1), dxfattribs=tread_attr)
    for (x0, y0), (x1, y1) in layout.handrails:
        msp.add_line((x0 + x_off, y0), (x1 + x_off, y1), dxfattribs=rail_attr)
    ax, ay = layout.annotation_pt
    _add_text(msp, layout.direction, ax + x_off, ay, 0.18, "A-FLOR-STRS", color=5)


def _draw_rect_symbol_dxf(msp: Modelspace, sym: RectSymbol, x_off: float) -> None:
    """Draw a fixture footprint rectangle + label on A-FLOR-FIXT."""
    attribs = {"layer": "A-FLOR-FIXT", "color": colors.GREEN}
    pts = [
        (sym.x + x_off, sym.y),
        (sym.x + sym.w + x_off, sym.y),
        (sym.x + sym.w + x_off, sym.y + sym.h),
        (sym.x + x_off, sym.y + sym.h),
    ]
    msp.add_lwpolyline(pts, close=True, dxfattribs=attribs)
    cx = sym.x + sym.w / 2 + x_off
    cy = sym.y + sym.h / 2
    _add_text(msp, sym.label, cx, cy, 0.12, "A-FLOR-FIXT", color=colors.GREEN)


def _draw_arch_symbols(msp: Modelspace, storey_cells: list, x_off: float) -> None:
    """Windows + stair treads + bathroom/kitchen fixtures for one storey."""
    try:
        edge_counts = extract_wall_edges(storey_cells)
        for pc in storey_cells:
            if pc.cell.role not in ("stair", "corridor", "entry",
                                    "mechanical", "storage", "void", "balcony"):
                for edge_name in get_exterior_edges_for_cell(pc, edge_counts):
                    win = get_window_for_edge(pc, edge_name)
                    if win:
                        _draw_window_glazing(msp, win, x_off)
        for pc in storey_cells:
            if pc.cell.role == "stair":
                _draw_stair_layout_dxf(msp, place_stair_treads(pc), x_off)
            elif pc.cell.role in ("bathroom", "powder_room"):
                for sym in place_bathroom_fixtures(pc):
                    _draw_rect_symbol_dxf(msp, sym, x_off)
            elif pc.cell.role == "kitchen":
                for sym in place_kitchen_fixtures(pc):
                    _draw_rect_symbol_dxf(msp, sym, x_off)
    except Exception:
        pass   # symbols are best-effort; never fail the export


def _cell_bounds(fit: FitResult) -> tuple[float, float, float, float]:
    """Return (cx0, cy0, cx1, cy1) — bounding box of all placed cells in modelspace.

    Accounts for the side-by-side storey layout (x_off = storey_index * spacing).
    """
    storeys_sorted = sorted({pc.cell.storey for pc in fit.placed_cells})
    spacing_x = fit.fit_frontage_m + 3.0
    sidx = {s: i for i, s in enumerate(storeys_sorted)}
    xs = []
    ys = []
    for pc in fit.placed_cells:
        off = sidx[pc.cell.storey] * spacing_x
        xs += [pc.x0 + off, pc.x1 + off]
        ys += [pc.y0, pc.y1]
    return min(xs), min(ys), max(xs), max(ys)


def _lot_is_proportional(er: EnvelopeResult, cx0: float, cy0: float,
                          cx1: float, cy1: float) -> bool:
    """Return True when the lot polygon is ≤ 10× the floor-plan span in each axis.

    A lot polygon larger than this (e.g. a neighbourhood boundary submitted by mistake)
    would dominate the modelspace and make the floor plan invisible.
    """
    lot_b = er.lot_local.bounds
    return (
        (lot_b[2] - lot_b[0]) <= (cx1 - cx0) * 10
        and (lot_b[3] - lot_b[1]) <= (cy1 - cy0) * 10
    )


def build_dxf(
    envelope_result: EnvelopeResult,
    fit: FitResult,
    obc: OBCResult,
    output_path: Optional[Union[str, Path]] = None,
    address: str = "",
    wall_networks: Optional[list] = None,   # list[WallNetwork]; None → legacy path
) -> bytes:
    """Build a DXF R2018 document and return its bytes.

    All geometry is in metres. Storeys are laid out side-by-side in modelspace.
    A paper-space layout at A1 (1189×841 mm) with 1:100 viewport is added.

    wall_networks: when provided (solver path), walls are drawn ONE segment per
    WallNetwork entry using the correct layer.  When None, falls back to the
    legacy per-cell box approach (stamp path — no regression).
    """
    doc = ezdxf.new("R2018")
    doc.units = 6  # metres
    _setup_layers(doc)
    msp = doc.modelspace()

    # Build storey→segments map for the solver path
    _wall_network_map: Optional[dict] = None
    if wall_networks:
        _wall_network_map = {wn.storey: wn.segments for wn in wall_networks}

    # Compute cell bounding box once — used to anchor all annotations and guard lot drawing.
    cx0, cy0, cx1, cy1 = _cell_bounds(fit)
    use_lot = _lot_is_proportional(envelope_result, cx0, cy0, cx1, cy1)

    _draw_lot_and_envelope(msp, envelope_result, use_lot=use_lot)
    _draw_storeys(msp, fit, _wall_network_map)
    _draw_setback_annotations(msp, envelope_result, use_lot=use_lot)
    _draw_bylaw_citations(msp, envelope_result, fit, cx0=cx0, cy1=cy1)
    _draw_obc_violations(msp, fit, obc)
    _draw_disclosure(msp, fit, envelope_result, cx0=cx0, cy0=cy0)
    _draw_3d_massing(msp, fit)

    # "NOT FOR CONSTRUCTION" stamp anchored to cells (never drifts with oversized lot)
    _add_text(
        msp,
        "NOT FOR CONSTRUCTION",
        (cx0 + cx1) / 2,
        cy0 - 1.2,
        0.50,
        "A-BLAW-NOTE",
        colors.RED,
    )

    _build_paper_layouts(doc, envelope_result, fit, address=address)

    if output_path:
        doc.saveas(str(output_path))

    text_buf = io.StringIO()
    doc.write(text_buf)
    return text_buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Sub-drawing functions
# ---------------------------------------------------------------------------

def _draw_lot_and_envelope(msp: Modelspace, er: EnvelopeResult,
                           use_lot: bool = True) -> None:
    if use_lot:
        lot_pts = list(er.lot_local.exterior.coords)
        _add_lwpolyline_closed(msp, lot_pts, "A-SITE-BLDG")

        # Only draw the setback envelope when the lot is proportional — for an oversized
        # lot the envelope can be 1745 m wide even after depth-limiting.
        env = er.envelope_2d
        if not env.is_empty:
            geoms = [env] if env.geom_type == "Polygon" else list(env.geoms)
            for g in geoms:
                pts = list(g.exterior.coords)
                _add_lwpolyline_closed(msp, pts, "A-FLOR-OTLN")


def _draw_storeys(msp: Modelspace, fit: FitResult,
                  wall_network_map: Optional[dict] = None) -> None:
    """Draw all storeys side-by-side.

    wall_network_map: {storey_int: list[WallSegment]} — when present for a storey,
    steps 2+3 use the exact WallNetwork segments (each drawn once).
    When None or the storey is not in the map, falls back to the legacy per-cell
    box derivation (stamp path — no regression).
    """
    storeys = sorted({pc.cell.storey for pc in fit.placed_cells})
    spacing_x = fit.fit_frontage_m + 3.0

    for i, s in enumerate(storeys):
        x_off = i * spacing_x
        storey_cells = [pc for pc in fit.placed_cells if pc.cell.storey == s]

        # -- 1. Room hatches (colour-coding) --
        # _room_outline_pts uses the real polygon when available, AABB otherwise.
        for pc in storey_cells:
            pts = _room_outline_pts(pc, x_off)
            color = _ROLE_COLOR.get(pc.cell.role, 9)
            if color > 0:
                _add_hatch(msp, pts, color, "A-FLOR-ROOM")

        # -- 2+3. Walls --
        network_segs = (wall_network_map or {}).get(s)
        if network_segs is not None:
            # Solver path: draw ONE entity per WallSegment, no doubled walls.
            _draw_wall_network_for_storey(msp, network_segs, x_off)
        else:
            # Legacy path: derive walls from cell bounding boxes (stamp path).
            # Use _cell_shapely so L-shaped rooms use their real outline.
            cell_boxes = {
                id(pc): _cell_shapely(pc, x_off)
                for pc in storey_cells
            }
            building_poly = _shapely_union(list(cell_boxes.values()))
            perims = [building_poly] if building_poly.geom_type == "Polygon" else list(building_poly.geoms)
            for poly in perims:
                outer_pts = list(poly.exterior.coords)
                _draw_wall_pair(msp, outer_pts, _WALL_T_EXT, "A-WALL-FULL", "A-WALL-INTR")

            for a_idx, pc_a in enumerate(storey_cells):
                for pc_b in storey_cells[a_idx + 1:]:
                    shared = cell_boxes[id(pc_a)].intersection(cell_boxes[id(pc_b)])
                    if shared.is_empty or shared.geom_type not in ("LineString", "MultiLineString"):
                        continue
                    lines = [shared] if shared.geom_type == "LineString" else list(shared.geoms)
                    a_uid, b_uid = pc_a.cell.unit_id, pc_b.cell.unit_id
                    is_service = lambda uid: uid < 0
                    is_demising = (
                        a_uid != b_uid
                        and not is_service(a_uid)
                        and not is_service(b_uid)
                    )
                    part_layer = "A-WALL-FIRE" if is_demising else "A-WALL-INTR"
                    for ln in lines:
                        msp.add_lwpolyline(
                            list(ln.coords),
                            dxfattribs={"layer": part_layer},
                        )

        # -- 4. Door symbols --
        front_y = min(pc.y0 for pc in storey_cells)
        for pc in storey_cells:
            swing = _choose_door_swing(pc, storey_cells, x_off, front_y)
            if swing is None:
                continue
            hinge_x, hinge_y, swing_deg = swing
            _draw_door_symbol(
                msp,
                hinge_x=hinge_x,
                hinge_y=hinge_y,
                door_w=_DOOR_ROLES[pc.cell.role],
                swing_deg=swing_deg,
                layer="A-DOOR",
            )

        # -- 5. Room labels --
        for pc in storey_cells:
            abs_x0 = pc.x0 + x_off
            cx = abs_x0 + pc.width_m / 2
            cy = pc.y0 + pc.depth_m / 2
            role_label = _ROLE_ABBREV.get(pc.cell.role, pc.cell.role.replace("_", " ").title())
            if not role_label:
                continue
            unit_suffix = f" — Unit {chr(65 + pc.cell.unit_id)}" if pc.cell.unit_id >= 0 else ""
            label = role_label + unit_suffix
            area_txt = f"{pc.area_m2:.1f} m²"
            _add_text(msp, label,    cx, cy + 0.22, 0.24, "A-FLOR-IDEN")
            _add_text(msp, area_txt, cx, cy - 0.15, 0.17, "A-FLOR-IDEN")

        # -- 6. Architectural symbols (windows, stair treads, fixtures) --
        _draw_arch_symbols(msp, storey_cells, x_off)

        # -- 7. Storey label + dimension chains --
        if storey_cells:
            label = f"STOREY {s}" if s >= 0 else "BASEMENT"
            x0_all = min(pc.x0 for pc in storey_cells) + x_off
            x1_all = max(pc.x1 for pc in storey_cells) + x_off
            y0_all = min(pc.y0 for pc in storey_cells)
            y1_all = max(pc.y1 for pc in storey_cells)
            _add_text(
                msp, label,
                x0_all + fit.fit_frontage_m / 2,
                y1_all + 0.5,
                0.4, "A-ANNO-TEXT",
            )
            _add_dim_linear(msp, (x0_all, y0_all), (x1_all, y0_all), -1.2, "A-ANNO-DIMS")
            _add_dim_linear(msp, (x0_all, y0_all), (x0_all, y1_all), -1.5, "A-ANNO-DIMS")


def _draw_setback_annotations(msp: Modelspace, er: EnvelopeResult,
                               use_lot: bool = True) -> None:
    if not use_lot:
        return   # setback lines are 500m-extended clutter when the lot polygon is bad

    _CITE = "§10.20.40.10"

    lot = er.lot_local
    bx0, by0, bx1, by1 = lot.bounds
    lot_mid_x = (bx0 + bx1) / 2
    lot_mid_y = (by0 + by1) / 2

    # Anchor points on the four lot-boundary faces (for dimension p1)
    lot_face_pt = {
        "front": (lot_mid_x, by0),
        "rear":  (lot_mid_x, by1),
        "left":  (bx0, lot_mid_y),
        "right": (bx1, lot_mid_y),
    }

    # Offsets that push the dimension line 1 m outside the lot boundary.
    # Vertical dims (front/rear) → dim line is 1 m to the LEFT.
    # Horizontal dims (left/right) → dim line is 1 m BELOW.
    off_vert  = bx0 - 1.0 - lot_mid_x   # puts base_x at bx0 - 1.0
    off_horiz = by0 - 1.0 - lot_mid_y   # puts base_y at by0 - 1.0

    for edge_name, sb_line in er.setback_lines.items():
        pts  = list(sb_line.coords)
        dist = er.setbacks_applied.get(edge_name, 0.0)

        # -- Dashed setback line --
        msp.add_lwpolyline(
            pts,
            dxfattribs={"layer": "A-SITE-INFO", "linetype": "DASHED"},
        )

        if dist < 0.05:
            continue   # zero setback — no dimension needed

        # -- Perpendicular dimension arrow --
        # p_lot: point on the lot boundary face at the lot centre-line
        # p_sb:  matching point on the setback line
        # For front/rear the paired X is lot_mid_x; for left/right the paired Y is lot_mid_y.
        sb_mid   = sb_line.interpolate(0.5, normalized=True)
        p_lot    = lot_face_pt[edge_name]

        if edge_name in ("front", "rear"):
            p_sb = (lot_mid_x, sb_mid.y)
            off  = off_vert
        else:                                  # "left" / "right"
            p_sb = (sb_mid.x, lot_mid_y)
            off  = off_horiz

        _add_dim_linear(msp, p_lot, p_sb, off, "A-ANNO-DIMS", citation=_CITE)

        # -- Compact text label on the setback line itself --
        # Smaller font (0.18 m) since the dimension entity already carries the value.
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        _add_text(
            msp,
            f"{dist:.1f}m  {_CITE}",
            mx, my, 0.18, "A-SITE-INFO",
        )


def _draw_bylaw_citations(msp: Modelspace, er: EnvelopeResult, fit: FitResult,
                           cx0: float = 0.0, cy1: float = 0.0) -> None:
    # Each entry is either a plain str (drawn in red) or (str, aci_color).
    notes: list = [
        f"Zone: {fit.typology.eligible_zones[0]}  |  By-law 569-2013 (City of Toronto)",
        f"Typology: {fit.typology.label}  ({fit.typology.units_produced} unit(s))",
        f"Lot: {er.lot_width_m:.1f}m × {er.lot_depth_m:.1f}m  ({er.lot_area_m2:.0f}m²)",
        f"GFA: {fit.gfa_m2:.1f}m²  |  Footprint: {fit.fit_frontage_m:.1f}×{fit.fit_depth_m:.1f}m",
        "Setbacks: §10.20.40.10  |  Depth Limit: §10.20.40.20",
        "Angular Plane (RD): §40.10.40.70  |  OBC Part 9 (2024)",
        "SCALE: 1:100  UNITS: METRES",
    ]
    if er.angular_plane_applied:
        notes.append("Angular plane clipping applied (§40.10.40.70)")

    # Contextual front-yard warning — amber so it stands out from general citations
    zone = (fit.typology.eligible_zones[0] if fit.typology.eligible_zones else "")
    if zone.startswith(("R ", "RD", "RS", "RT")):
        notes.insert(3, (
            "⚠ FRONT YARD IS CONTEXTUAL — verify §10.20.40.10 street average before finalizing",
            colors.YELLOW,
        ))

    # Anchor to the TOP of the cell bounding box (cy1), not the lot polygon.
    # This keeps annotations adjacent to the floor plan regardless of lot size.
    x = cx0
    y = cy1 + 2.5
    for j, entry in enumerate(notes):
        if isinstance(entry, tuple):
            text, col = entry
        else:
            text, col = entry, colors.RED
        _add_text(msp, text, x + 8.0, y + j * 0.55, 0.22, "A-BLAW-NOTE", col)


def _draw_obc_violations(msp: Modelspace, fit: FitResult, obc: OBCResult) -> None:
    if not obc.violations:
        return
    x = fit.placed_cells[0].x0 if fit.placed_cells else 0.0
    y = -3.0
    _add_text(msp, "OBC COMPLIANCE NOTES", x, y, 0.3, "A-BLAW-NOTE", colors.RED)
    for k, v in enumerate(obc.violations):
        icon = "✖ " if v.severity == "error" else "⚠ "
        msg = f"{icon}[{v.code_ref}] U{v.unit_id} S{v.storey} {v.cell_role}: {v.message}"
        _add_text(msp, msg[:80], x, y - 0.5 * (k + 1), 0.18, "A-BLAW-NOTE", colors.RED)


def _draw_disclosure(msp: Modelspace, fit: FitResult, er: EnvelopeResult,
                     cx0: float = 0.0, cy0: float = 0.0) -> None:
    """Write provenance data to non-plotting A-DISC layer."""
    import hashlib, json
    meta = {
        "typology_id":   fit.typology.id,
        "option":        fit.option,
        "gfa_m2":        round(fit.gfa_m2, 2),
        "lot_w":         round(er.lot_width_m, 2),
        "lot_d":         round(er.lot_depth_m, 2),
    }
    h = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()[:12]
    lines = [
        "AI-GENERATED PRELIMINARY STUDY — NOT FOR CONSTRUCTION OR PERMIT SUBMISSION",
        "Generated by Toronto Zoning AI (packgen) using By-law 569-2013 + OBC 2024.",
        "All setbacks, heights, and areas are indicative. Verify with a registered planner.",
        f"Parameter hash: {h}  |  Typology: {fit.typology.id}  |  Option: {fit.option}",
    ]
    # Anchor to the BOTTOM of the cell bounding box (cy0), not the lot polygon.
    x = cx0
    y = cy0 - 3.0
    for k, line in enumerate(lines):
        _add_text(msp, line, x + 10.0, y - k * 0.45, 0.18, "A-DISC", 8)


def _add_paper_sheet(doc: Drawing, name: str, ms_cx: float, ms_cy: float,
                     ms_w: float, ms_h: float, scale: int,
                     title_lines: list, address: str = "") -> None:
    """Add one paperspace layout at the given scale with title block."""
    sheet_w, sheet_h = 0.420, 0.297   # A3 in metres (manageable viewport math)
    tb_h = 0.050                       # title block strip at bottom

    try:
        layout = doc.new_layout(name)
    except Exception:
        return

    # Sheet border
    layout.add_lwpolyline(
        [(0.005, 0.005), (sheet_w - 0.005, 0.005),
         (sheet_w - 0.005, sheet_h - 0.005), (0.005, sheet_h - 0.005)],
        close=True, dxfattribs={"layer": "A-ANNO-TTLB"},
    )
    # Title strip divider
    layout.add_line(
        (0.005, tb_h), (sheet_w - 0.005, tb_h),
        dxfattribs={"layer": "A-ANNO-TTLB"},
    )

    # Viewport
    vp_area_w = sheet_w - 0.01
    vp_area_h = sheet_h - tb_h - 0.01
    vp_cx = sheet_w / 2
    vp_cy = tb_h + vp_area_h / 2
    view_h = vp_area_h * scale   # model-space metres visible

    try:
        vp = layout.add_viewport(
            center=(vp_cx, vp_cy),
            size=(vp_area_w, vp_area_h),
            view_center_point=(ms_cx, ms_cy),
            view_height=view_h,
        )
        vp.dxf.status = 1
    except Exception:
        pass

    # Title block text
    tb_cx = sheet_w / 2
    lines = [
        (name, 0.007, True),
        (address, 0.005, False) if address else None,
        (f"Scale 1:{scale}  ·  By-law 569-2013  ·  OBC Part 9 (2024)", 0.004, False),
        ("  ·  ".join(tl for tl in title_lines if tl), 0.004, False),
        ("PRELIMINARY — NOT FOR CONSTRUCTION — PackGen AI", 0.004, False),
    ]
    cur_y = tb_h - 0.006
    for row in lines:
        if row is None:
            continue
        text, h, bold = row
        if not text:
            cur_y -= h * 1.2
            continue
        attribs = {"layer": "A-ANNO-TTLB", "height": h}
        if bold:
            attribs["color"] = colors.WHITE
        layout.add_text(text, dxfattribs=attribs).set_placement(
            (tb_cx, cur_y), align=TextEntityAlignment.MIDDLE_CENTER,
        )
        cur_y -= h * 1.6


def _build_paper_layouts(doc: Drawing, er: EnvelopeResult, fit: FitResult, address: str = "") -> None:
    """Create multi-paperspace layouts: Site Plan (1:200) + per-storey floor plans (1:50) + Roof."""
    storeys_sorted = sorted({pc.cell.storey for pc in fit.placed_cells})
    spacing_x = fit.fit_frontage_m + 3.0
    sidx = {s: i for i, s in enumerate(storeys_sorted)}

    # Cell bounding box — all storeys laid out side-by-side.
    cx0, cy0, cx1, cy1 = _cell_bounds(fit)
    cell_cx = (cx0 + cx1) / 2
    cell_cy = (cy0 + cy1) / 2
    cell_w  = cx1 - cx0
    cell_h  = cy1 - cy0

    lot_b = er.lot_local.bounds   # (x0,y0,x1,y1)
    # Use lot extent for Site/Roof plans only when it's proportional to the building.
    # A 1747m lot with a 9m building would make the building invisible in the viewport.
    use_lot = _lot_is_proportional(er, cx0, cy0, cx1, cy1)
    site_cx = (lot_b[0] + lot_b[2]) / 2 if use_lot else cell_cx
    site_cy = (lot_b[1] + lot_b[3]) / 2 if use_lot else cell_cy
    site_w  = (lot_b[2] - lot_b[0] + 4) if use_lot else (cell_w + 6)
    site_h  = (lot_b[3] - lot_b[1] + 4) if use_lot else (cell_h + 6)

    common_lines = [
        f"Typology: {fit.typology.label}",
        f"Units: {fit.typology.units_produced}",
        f"GFA: {fit.gfa_m2:.0f} m²",
        f"Lot: {er.lot_width_m:.1f}×{er.lot_depth_m:.1f} m",
    ]

    # -- Site Plan (1:200) ---------------------------------------------------
    _add_paper_sheet(
        doc, "Site Plan",
        ms_cx=site_cx, ms_cy=site_cy,
        ms_w=site_w, ms_h=site_h,
        scale=200,
        title_lines=common_lines,
        address=address,
    )

    # -- Floor plan per storey (1:50) ----------------------------------------
    storey_names = {-1: "Basement", 0: "Ground Floor", 1: "Second Floor", 2: "Third Floor", 3: "Fourth Floor"}
    for s in storeys_sorted:
        cells = [pc for pc in fit.placed_cells if pc.cell.storey == s]
        if not cells:
            continue
        x_off = sidx[s] * spacing_x
        xs = [pc.x0 + x_off for pc in cells] + [pc.x1 + x_off for pc in cells]
        ys = [pc.y0 for pc in cells] + [pc.y1 for pc in cells]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        ms_w = max(xs) - min(xs) + 4.0
        ms_h = max(ys) - min(ys) + 4.0
        lname = storey_names.get(s, f"Floor {s + 1}")
        _add_paper_sheet(
            doc, lname,
            ms_cx=cx, ms_cy=cy,
            ms_w=ms_w, ms_h=ms_h,
            scale=50,
            title_lines=common_lines,
            address=address,
        )

    # -- Roof Plan (1:200, same view as site) ---------------------------------
    _add_paper_sheet(
        doc, "Roof Plan",
        ms_cx=site_cx, ms_cy=site_cy,
        ms_w=site_w, ms_h=site_h,
        scale=200,
        title_lines=common_lines,
        address=address,
    )
