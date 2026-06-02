"""Generate SVG floor plan previews — wall-line style with lot boundary and setbacks."""
from __future__ import annotations

import math
from typing import Optional

from .fixtures import (
    extract_wall_edges,
    get_exterior_edges_for_cell,
    get_window_for_edge,
    place_stair_treads,
    place_bathroom_fixtures,
    place_kitchen_fixtures,
)
from .geometry import EnvelopeResult
from .typology.selector import FitResult, PlacedCell


# Role → light fill for colour-coding (30% opacity — walls dominate the visual)
_ROLE_FILL: dict[str, str] = {
    "bedroom":        "rgba(179,205,227,0.30)",
    "master_bedroom": "rgba(141,184,216,0.30)",
    "living":         "rgba(253,219,199,0.30)",
    "dining":         "rgba(255,243,205,0.35)",
    "kitchen":        "rgba(217,240,163,0.30)",
    "bathroom":       "rgba(161,217,155,0.30)",
    "powder_room":    "rgba(116,196,118,0.30)",
    "laundry":        "rgba(204,229,255,0.35)",
    "stair":          "rgba(200,200,200,0.30)",
    "corridor":       "rgba(245,245,245,0.30)",
    "entry":          "rgba(217,217,217,0.25)",
    "mechanical":     "rgba(214,216,219,0.35)",
    "storage":        "rgba(226,227,229,0.35)",
    "balcony":        "rgba(199,251,199,0.25)",
    "garage":         "rgba(195,196,199,0.35)",
    "void":           "rgba(255,255,255,0.05)",
}

_ROLE_LABEL: dict[str, str] = {
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

_DOOR_ROLES = {
    "entry":          0.900,
    "living":         0.900,
    "bedroom":        0.800,
    "master_bedroom": 0.800,
    "bathroom":       0.700,
    "powder_room":    0.700,
}

# Wall thickness in metres (rendered proportionally)
_WALL_T_M = 0.100


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _door_arc_svg(hx: float, hy: float, door_w_px: float) -> str:
    """Return SVG for a door: panel line + quarter-circle arc."""
    # Panel: hinge (hx,hy) → (hx + door_w_px, hy)
    # Arc:   quarter circle from (hx+door_w, hy) sweeping up to (hx, hy-door_w)
    ex = hx + door_w_px
    ey = hy
    ax = hx
    ay = hy - door_w_px
    return (
        f'<line x1="{hx:.1f}" y1="{hy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
        f'stroke="#555" stroke-width="1" />'
        f'<path d="M {ex:.1f},{ey:.1f} A {door_w_px:.1f},{door_w_px:.1f} 0 0,1 {ax:.1f},{ay:.1f}" '
        f'fill="none" stroke="#555" stroke-width="0.8" stroke-dasharray="3,2"/>'
    )


def generate_svg(
    fit: FitResult,
    er: Optional[EnvelopeResult] = None,
    px_per_m: float = 30.0,
    pad_px: float = 40.0,
) -> str:
    """Return an SVG string with wall-line floor plan style.

    Each storey is drawn in a column. Wall thickness is rendered as a 2px
    outer line plus a 1px inner line offset inward. Door arcs are shown for
    rooms with doors. The lot boundary and setback lines appear for the
    ground-floor column (when er is provided).
    """
    placed = fit.placed_cells
    if not placed:
        return "<svg xmlns='http://www.w3.org/2000/svg'><text>No cells</text></svg>"

    storeys = sorted({pc.cell.storey for pc in placed})
    spacing_m = fit.fit_frontage_m + 3.0   # matches DXF storey column gap

    # Collect all absolute model coords for viewBox — cells ONLY.
    # Never include lot_local here: a wrong/oversized polygon (e.g. neighbourhood boundary)
    # would push min/max far beyond the building footprint, making the floor plan invisible.
    all_pts: list[tuple[float, float]] = []
    for i, s in enumerate(storeys):
        x_off = i * spacing_m
        for pc in placed:
            if pc.cell.storey == s:
                all_pts += [(pc.x0 + x_off, pc.y0), (pc.x1 + x_off, pc.y1)]

    min_x = min(p[0] for p in all_pts)
    min_y = min(p[1] for p in all_pts)
    max_x = max(p[0] for p in all_pts)
    max_y = max(p[1] for p in all_pts)

    # Guard: only draw lot boundary / setback context when the lot polygon is proportional
    # to the building footprint (≤ 10× span in either direction).  Prevents a 1747 m lot
    # from compressing the entire floor plan to a 1-pixel speck.
    _draw_lot_ctx = False
    if er:
        _lot_b = er.lot_local.bounds   # (minx, miny, maxx, maxy)
        _cell_span_x = max_x - min_x
        _cell_span_y = max_y - min_y
        _lot_span_x  = _lot_b[2] - _lot_b[0]
        _lot_span_y  = _lot_b[3] - _lot_b[1]
        _draw_lot_ctx = (
            _lot_span_x <= _cell_span_x * 10
            and _lot_span_y <= _cell_span_y * 10
        )

    def tx(x: float) -> float:
        return (x - min_x) * px_per_m + pad_px

    def ty(y: float) -> float:
        return (max_y - y) * px_per_m + pad_px

    total_w = (max_x - min_x) * px_per_m + 2 * pad_px
    total_h = (max_y - min_y) * px_per_m + 2 * pad_px + 84

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_w:.0f}" height="{total_h:.0f}" '
        f'viewBox="0 0 {total_w:.0f} {total_h:.0f}" '
        f'style="background:#ffffff;font-family:monospace">'
    )

    # Grid paper background
    parts.append(
        '<defs>'
        '<pattern id="grid" width="30" height="30" patternUnits="userSpaceOnUse">'
        '<path d="M 30 0 L 0 0 0 30" fill="none" stroke="#e8e8e8" stroke-width="0.5"/>'
        '</pattern>'
        '</defs>'
    )
    parts.append(f'<rect width="{total_w:.0f}" height="{total_h:.0f}" fill="url(#grid)"/>')

    # Ground-floor lot boundary and envelope (first storey column = index 0)
    if _draw_lot_ctx and storeys:
        s0 = storeys[0]
        # Lot boundary — heavy dashed grey
        lot_pts_str = " ".join(
            f"{tx(x):.1f},{ty(y):.1f}" for x, y in er.lot_local.exterior.coords
        )
        parts.append(
            f'<polygon points="{lot_pts_str}" '
            f'fill="none" stroke="#999" stroke-width="1.5" stroke-dasharray="8,4"/>'
        )

        # Setback lines — thin dashed orange
        for edge_name, line in er.setback_lines.items():
            pts = list(line.coords)
            if len(pts) >= 2:
                x1s, y1s = tx(pts[0][0]), ty(pts[0][1])
                x2s, y2s = tx(pts[-1][0]), ty(pts[-1][1])
                dist = er.setbacks_applied.get(edge_name, 0.0)
                parts.append(
                    f'<line x1="{x1s:.1f}" y1="{y1s:.1f}" x2="{x2s:.1f}" y2="{y2s:.1f}" '
                    f'stroke="#f59e0b" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>'
                )
                mx = (x1s + x2s) / 2
                my = (y1s + y2s) / 2
                parts.append(
                    f'<text x="{mx:.1f}" y="{my - 4:.1f}" text-anchor="middle" '
                    f'font-size="7" fill="#b45309">{_esc(edge_name)} {dist:.1f}m</text>'
                )

        # Envelope outline — blue
        env = er.envelope_2d
        geoms = [env] if env.geom_type == "Polygon" else list(env.geoms)
        for g in geoms:
            env_pts_str = " ".join(
                f"{tx(x):.1f},{ty(y):.1f}" for x, y in g.exterior.coords
            )
            parts.append(
                f'<polygon points="{env_pts_str}" '
                f'fill="rgba(51,153,255,0.04)" stroke="#3399ff" stroke-width="1.5"/>'
            )

    wall_t_px = _WALL_T_M * px_per_m  # wall thickness in pixels

    _NO_WINDOW_ROLES_SVG = frozenset({
        "stair", "corridor", "entry", "mechanical", "storage", "void", "balcony",
    })

    # Draw each storey
    for i, s in enumerate(storeys):
        x_off = i * spacing_m
        storey_cells = [pc for pc in placed if pc.cell.storey == s]

        # Pre-compute exterior edge map for this storey (used for window placement)
        try:
            _edge_counts = extract_wall_edges(storey_cells)
        except Exception:
            _edge_counts = {}

        # Storey label above
        cx_m = x_off + fit.fit_frontage_m / 2
        top_y_m = max(pc.y1 for pc in storey_cells)
        label_txt = f"Storey {s}" if s >= 0 else "Basement"
        parts.append(
            f'<text x="{tx(cx_m):.1f}" y="{ty(top_y_m) - 8:.1f}" '
            f'text-anchor="middle" font-size="11" font-weight="bold" fill="#333">'
            f'{_esc(label_txt)}</text>'
        )

        for pc in storey_cells:
            sx0 = tx(pc.x0 + x_off)
            sy0 = ty(pc.y1)          # y-flip: y1 becomes SVG top
            w_px = pc.width_m  * px_per_m
            h_px = pc.depth_m  * px_per_m
            fill = _ROLE_FILL.get(pc.cell.role, "rgba(240,240,240,0.3)")

            # When the solver provided a real polygon, render as <polygon> path.
            # Fall back to <rect> for stamp-path cells (polygon=None).
            if getattr(pc, "polygon", None):
                # Convert model coords to SVG pixel space
                svg_pts = " ".join(
                    f"{tx(x + x_off):.1f},{ty(y):.1f}" for x, y in pc.polygon
                )
                # Light fill
                parts.append(
                    f'<polygon points="{svg_pts}" fill="{fill}"/>'
                )
                # Outer wall line (2px, dark)
                parts.append(
                    f'<polygon points="{svg_pts}" '
                    f'fill="none" stroke="#333" stroke-width="2"/>'
                )
            else:
                # Light fill
                parts.append(
                    f'<rect x="{sx0:.1f}" y="{sy0:.1f}" '
                    f'width="{w_px:.1f}" height="{h_px:.1f}" '
                    f'fill="{fill}"/>'
                )

                # Outer wall line (2px, dark)
                parts.append(
                    f'<rect x="{sx0:.1f}" y="{sy0:.1f}" '
                    f'width="{w_px:.1f}" height="{h_px:.1f}" '
                    f'fill="none" stroke="#333" stroke-width="2"/>'
                )

                # Inner wall line (1px, medium grey) — creates double-line wall look
                t = wall_t_px
                if w_px > 2 * t + 2 and h_px > 2 * t + 2:
                    parts.append(
                        f'<rect x="{sx0 + t:.1f}" y="{sy0 + t:.1f}" '
                        f'width="{w_px - 2*t:.1f}" height="{h_px - 2*t:.1f}" '
                        f'fill="none" stroke="#777" stroke-width="0.8"/>'
                    )

            # Door arc for rooms with doors
            t = wall_t_px
            door_m = _DOOR_ROLES.get(pc.cell.role)
            if door_m and w_px > door_m * px_per_m + 6 and h_px > door_m * px_per_m + 6:
                door_px = door_m * px_per_m
                # Hinge at bottom-left (in SVG = bottom of rect = sy0+h_px)
                hx = sx0 + t
                hy = sy0 + h_px - t
                parts.append(_door_arc_svg(hx, hy, door_px))

            # Window glazing (light-blue rect on exterior wall edges)
            if pc.cell.role not in _NO_WINDOW_ROLES_SVG and _edge_counts:
                try:
                    for _edge_name in get_exterior_edges_for_cell(pc, _edge_counts):
                        _win = get_window_for_edge(pc, _edge_name)
                        if _win is None:
                            continue
                        if _win.edge in ("bottom", "top"):
                            _wx = tx(_win.x0 + x_off)
                            _ww = (_win.x1 - _win.x0) * px_per_m
                            _wy = (sy0 + h_px - 2.5) if _win.edge == "bottom" else (sy0 - 2.5)
                            parts.append(
                                f'<rect x="{_wx:.1f}" y="{_wy:.1f}" '
                                f'width="{_ww:.1f}" height="5" '
                                f'fill="#ADD8E6" stroke="#4a9bc4" '
                                f'stroke-width="0.6" opacity="0.9"/>'
                            )
                        else:
                            _wy = ty(_win.y1)
                            _wh = (_win.y1 - _win.y0) * px_per_m
                            _wx = (sx0 - 2.5) if _win.edge == "left" else (sx0 + w_px - 2.5)
                            parts.append(
                                f'<rect x="{_wx:.1f}" y="{_wy:.1f}" '
                                f'width="5" height="{_wh:.1f}" '
                                f'fill="#ADD8E6" stroke="#4a9bc4" '
                                f'stroke-width="0.6" opacity="0.9"/>'
                            )
                except Exception:
                    pass  # window placement is best-effort; skip on error

            # Room label and area (only if cell is large enough to hold text)
            cx = sx0 + w_px / 2
            cy = sy0 + h_px / 2
            if pc.cell.role == "corridor":
                role_label = "Circulation"
            else:
                role_label = _ROLE_LABEL.get(pc.cell.role, pc.cell.role.replace("_", " ").title())
            if role_label and w_px > 22 and h_px > 22:
                unit_suffix = chr(65 + pc.cell.unit_id) if pc.cell.unit_id >= 0 else "Shared"
                unit_tag = f" · Unit {unit_suffix}" if pc.cell.unit_id >= 0 else ""
                area_m2 = round((pc.x1 - pc.x0) * (pc.y1 - pc.y0), 1)
                area_tag = f"{area_m2:.1f} m²"
                parts.append(
                    f'<text x="{cx:.1f}" y="{cy - 5:.1f}" '
                    f'text-anchor="middle" font-size="9" font-weight="600" fill="#222">'
                    f'{_esc(role_label + unit_tag)}</text>'
                )
                parts.append(
                    f'<text x="{cx:.1f}" y="{cy + 7:.1f}" '
                    f'text-anchor="middle" font-size="7.5" fill="#555">'
                    f'{_esc(area_tag)}</text>'
                )

        # Stair treads, handrails, and fixture outlines (drawn after cells so they're on top)
        for pc in storey_cells:
            if pc.cell.role == "stair":
                try:
                    _layout = place_stair_treads(pc)
                    for (lx0, ly0), (lx1, ly1) in _layout.treads:
                        parts.append(
                            f'<line x1="{tx(lx0 + x_off):.1f}" y1="{ty(ly0):.1f}" '
                            f'x2="{tx(lx1 + x_off):.1f}" y2="{ty(ly1):.1f}" '
                            f'stroke="#888" stroke-width="0.7" stroke-dasharray="3,2"/>'
                        )
                    for (lx0, ly0), (lx1, ly1) in _layout.handrails:
                        parts.append(
                            f'<line x1="{tx(lx0 + x_off):.1f}" y1="{ty(ly0):.1f}" '
                            f'x2="{tx(lx1 + x_off):.1f}" y2="{ty(ly1):.1f}" '
                            f'stroke="#555" stroke-width="0.9"/>'
                        )
                    _ann_x, _ann_y = _layout.annotation_pt
                    parts.append(
                        f'<text x="{tx(_ann_x + x_off):.1f}" y="{ty(_ann_y):.1f}" '
                        f'text-anchor="middle" font-size="7" font-weight="bold" fill="#555">'
                        f'{_esc(_layout.direction)}</text>'
                    )
                except Exception:
                    pass  # stair tread rendering is best-effort; skip on error

            elif pc.cell.role in ("bathroom", "powder_room"):
                try:
                    for _sym in place_bathroom_fixtures(pc):
                        _ss_x = tx(_sym.x + x_off)
                        _ss_y = ty(_sym.y + _sym.h)
                        _sw   = _sym.w * px_per_m
                        _sh   = _sym.h * px_per_m
                        if _sw > 2 and _sh > 2:
                            parts.append(
                                f'<rect x="{_ss_x:.1f}" y="{_ss_y:.1f}" '
                                f'width="{_sw:.1f}" height="{_sh:.1f}" '
                                f'fill="rgba(255,255,255,0.55)" stroke="#888" stroke-width="0.6"/>'
                            )
                            if _sw > 8 and _sh > 8:
                                parts.append(
                                    f'<text x="{_ss_x + _sw/2:.1f}" y="{_ss_y + _sh/2 + 2:.1f}" '
                                    f'text-anchor="middle" font-size="5" fill="#666">'
                                    f'{_esc(_sym.label)}</text>'
                                )
                except Exception:
                    pass  # bathroom fixture rendering is best-effort; skip on error

            elif pc.cell.role == "kitchen":
                try:
                    for _sym in place_kitchen_fixtures(pc):
                        _ss_x = tx(_sym.x + x_off)
                        _ss_y = ty(_sym.y + _sym.h)
                        _sw   = _sym.w * px_per_m
                        _sh   = _sym.h * px_per_m
                        if _sw > 2 and _sh > 2:
                            parts.append(
                                f'<rect x="{_ss_x:.1f}" y="{_ss_y:.1f}" '
                                f'width="{_sw:.1f}" height="{_sh:.1f}" '
                                f'fill="rgba(255,255,255,0.45)" stroke="#888" stroke-width="0.6"/>'
                            )
                            if _sw > 8 and _sh > 8:
                                parts.append(
                                    f'<text x="{_ss_x + _sw/2:.1f}" y="{_ss_y + _sh/2 + 2:.1f}" '
                                    f'text-anchor="middle" font-size="5" fill="#666">'
                                    f'{_esc(_sym.label)}</text>'
                                )
                except Exception:
                    pass  # kitchen fixture rendering is best-effort; skip on error

    # Build room summary for footer (skip corridor/void)
    _by_role: dict[str, int] = {}
    for _pc in placed:
        _by_role[_pc.cell.role] = _by_role.get(_pc.cell.role, 0) + 1
    _summary_roles = {r: c for r, c in _by_role.items() if r not in ("corridor", "void")}
    room_summary = " · ".join(
        f"{count}×{role.replace('_', ' ')}"
        for role, count in sorted(_summary_roles.items())
    )

    # Legend strip at bottom — only show roles that actually appear in this plan
    # Extend height by 14px to fit the extra room summary line
    legend_y = total_h - 66
    parts.append(
        f'<rect x="0" y="{legend_y - 4}" width="{total_w:.0f}" height="70" '
        f'fill="#f5f5f5" stroke="#ddd" stroke-width="0.5"/>'
    )

    # Human-readable legend label for each role
    _LEGEND_LABEL: dict[str, str] = {
        "bedroom":        "Bedroom",
        "master_bedroom": "Master Bed",
        "living":         "Living",
        "dining":         "Dining",
        "kitchen":        "Kitchen",
        "bathroom":       "Bath",
        "powder_room":    "Powder Rm",
        "laundry":        "Laundry",
        "stair":          "Stair",
        "corridor":       "Circulation",
        "entry":          "Entry",
        "mechanical":     "Mechanical",
        "storage":        "Storage",
        "balcony":        "Balcony",
        "garage":         "Garage",
        "void":           "Void",
    }

    # Collect roles present in this plan and build legend items from _ROLE_FILL
    present_roles = {pc.cell.role for pc in placed}
    legend_items = [
        (fill_col, _LEGEND_LABEL.get(role, role.replace("_", " ").title()))
        for role, fill_col in _ROLE_FILL.items()
        if role in present_roles and role != "void"
    ]

    # Render legend swatches — wrap onto two rows if needed
    lx = 12
    legend_row_y = legend_y + 2
    items_per_row = max(1, int((total_w - 24) // 80))
    for idx, (fill_col, lbl) in enumerate(legend_items):
        row = idx // items_per_row
        col = idx % items_per_row
        lx = 12 + col * 80
        ly = legend_row_y + row * 14
        # Convert rgba fill to a solid-ish hex for the swatch
        parts.append(
            f'<rect x="{lx}" y="{ly}" width="10" height="10" '
            f'rx="2" fill="{fill_col}" stroke="#888" stroke-width="0.8"/>'
            f'<text x="{lx + 13}" y="{ly + 9}" font-size="9" fill="#444">{_esc(lbl)}</text>'
        )

    legend_text_y = legend_y + 30
    parts.append(
        f'<text x="10" y="{legend_text_y}" font-size="9" fill="#666">'
        f'{_esc(fit.typology.label)}'
        f'  ·  {fit.typology.units_produced} unit(s)'
        f'  ·  GFA {fit.gfa_m2:.0f} m²'
        f'  ·  {fit.fit_frontage_m:.1f}×{fit.fit_depth_m:.1f} m footprint'
        f'  ·  By-law 569-2013'
        f'</text>'
    )
    if room_summary:
        parts.append(
            f'<text x="10" y="{legend_text_y + 13}" font-size="8.5" fill="#555">'
            f'{_esc(room_summary)}'
            f'</text>'
        )
    parts.append(
        f'<text x="10" y="{legend_text_y + 27}" font-size="8" fill="#999" font-style="italic">'
        f'PRELIMINARY STUDY — NOT FOR CONSTRUCTION — Verify with a registered planner'
        f'</text>'
    )

    parts.append("</svg>")
    return "\n".join(parts)
