"""Convert any typology's stamp_cells into a TypologyTemplate.

Used when an architect provides a room brief for a typology that doesn't have
an explicit template defined. The stamp's cell positions define the spatial
zones; the LLM assigns rooms from the brief within those zones.

Circulation cells (stair/corridor/entry) are auto-filled and never sent to the
LLM. The remaining cells are bucketed into three spatial bands:
  front_public  (y_mid < 0.40) → living, dining
  mid_service   (0.40 ≤ y_mid < 0.65) → kitchen, bathroom, laundry, powder_room
  rear_private  (y_mid ≥ 0.65) → bedroom, master_bedroom, den, storage

Side-by-side typologies (multiple unit_ids on the same storey) get per-unit
zone sets so their spatial columns don't overlap.
"""
from __future__ import annotations

from collections import defaultdict

from .models import Cell, TemplateZone, Typology, TypologyTemplate


_CIRC_ROLES    = frozenset({"stair", "corridor", "entry"})
_PRIVATE_ROLES = frozenset({"bedroom", "master_bedroom", "den", "storage"})
_SERVICE_ROLES = frozenset({"kitchen", "bathroom", "powder_room", "laundry", "mechanical"})

_BAND_CUTOFFS = (0.40, 0.65)

_BAND_META: dict[str, tuple[tuple[str, ...], str]] = {
    "front_public": (
        ("living", "dining", "balcony", "entry", "storage",
         "powder_room", "bedroom", "master_bedroom"),
        "x",
    ),
    "mid_service": (
        ("kitchen", "bathroom", "powder_room", "laundry", "mechanical",
         "dining", "storage", "balcony"),
        "y",
    ),
    "rear_private": (
        ("bedroom", "master_bedroom", "den", "storage", "bathroom",
         "balcony", "dining", "laundry"),
        "y",
    ),
}


def _band_name(cell: Cell) -> str:
    """Role-aware band assignment.

    Private rooms (bedrooms) always map to rear_private regardless of y-position.
    This prevents upper-floor bedroom cells from being classified as front_public
    just because their y-range starts at 0. Service rooms similarly always land in
    mid_service. Only public rooms (living/dining) use the y-position threshold.
    """
    if cell.role == "balcony":
        y_mid = (cell.y0 + cell.y1) / 2
        # Balcony: rear if in back half, front otherwise
        return "rear_private" if y_mid >= _BAND_CUTOFFS[1] else "front_public"
    if cell.role in _PRIVATE_ROLES:
        return "rear_private"
    if cell.role in _SERVICE_ROLES:
        return "mid_service"
    # living, dining, void — use y-position
    y_mid = (cell.y0 + cell.y1) / 2
    if y_mid < _BAND_CUTOFFS[0]:
        return "front_public"
    if y_mid < _BAND_CUTOFFS[1]:
        return "mid_service"
    return "rear_private"


def _zones_for_unit(
    live_cells: list[Cell],
    storey: int,
    uid: int,
    brief_rooms: "dict[str, int] | None" = None,
) -> list[TemplateZone]:
    """Return front/mid/rear TemplateZones for a single unit's live cells."""
    by_band: dict[str, list[Cell]] = defaultdict(list)
    for c in live_cells:
        by_band[_band_name(c)].append(c)

    zones: list[TemplateZone] = []
    for band, (roles, axis) in _BAND_META.items():
        band_cells = by_band.get(band, [])
        if not band_cells:
            continue
        x0 = min(c.x0 for c in band_cells)
        y0 = min(c.y0 for c in band_cells)
        x1 = max(c.x1 for c in band_cells)
        y1 = max(c.y1 for c in band_cells)
        band_count = max(1, len(band_cells))
        if brief_rooms is not None:
            # Count how many brief rooms could land in this band's valid_roles
            brief_in_band = sum(brief_rooms.get(r, 0) for r in roles)
            band_count = max(band_count, brief_in_band)
        max_subdivisions = max(4, band_count + 1)
        zones.append(TemplateZone(
            zone_id=f"{band}_u{uid}_s{storey}",
            storey=storey,
            x0=x0, y0=y0, x1=x1, y1=y1,
            valid_roles=roles,
            max_subdivisions=max_subdivisions,
            subdivision_axis=axis,
        ))
    return zones


def stamp_to_generic_template(
    typology: "Typology",
    brief_rooms: "dict[str, int] | None" = None,
) -> "TypologyTemplate":
    """Return a TypologyTemplate derived from the typology's stamp_cells.

    The resulting template is accepted by fill_template() in template_filler.py.
    Stacked typologies produce one zone set per storey; side-by-side typologies
    produce per-unit zone columns on shared storeys.

    brief_rooms: optional dict mapping canonical role → total room count from the
    architect's brief. Used to set max_subdivisions high enough that the LLM can
    place all brief rooms without hitting the subdivision cap.
    """
    by_storey: dict[int, list[Cell]] = defaultdict(list)
    for c in typology.stamp_cells:
        by_storey[c.storey].append(c)

    zones: list[TemplateZone] = []

    for storey, cells in sorted(by_storey.items()):
        circ_cells = [c for c in cells if c.role in _CIRC_ROLES]
        live_cells = [c for c in cells if c.role not in _CIRC_ROLES]

        # Circulation zones — pre-filled by _auto_fill_circulation, never sent to LLM
        seen: set[str] = set()
        for c in circ_cells:
            key = f"{c.role}_{c.x0:.4f}_{c.y0:.4f}_{c.storey}"
            if key in seen:
                continue
            seen.add(key)
            zones.append(TemplateZone(
                zone_id=f"circ_{c.role}_u{c.unit_id}_s{storey}_{len(zones)}",
                storey=storey,
                x0=c.x0, y0=c.y0, x1=c.x1, y1=c.y1,
                valid_roles=(c.role,),
                max_subdivisions=1,
                is_circulation=True,
            ))

        if not live_cells:
            continue

        # Detect side-by-side layout: multiple distinct non-negative unit_ids on this storey
        unit_ids = sorted({c.unit_id for c in live_cells if c.unit_id >= 0})

        if len(unit_ids) >= 2:
            unit_x: dict[int, tuple[float, float]] = {}
            for uid in unit_ids:
                uid_cells = [c for c in live_cells if c.unit_id == uid]
                unit_x[uid] = (
                    min(c.x0 for c in uid_cells),
                    max(c.x1 for c in uid_cells),
                )

            # Side-by-side: unit x-ranges are largely non-overlapping
            max_overlap_frac = 0.0
            for i, ua in enumerate(unit_ids):
                for ub in unit_ids[i + 1:]:
                    xa0, xa1 = unit_x[ua]
                    xb0, xb1 = unit_x[ub]
                    overlap = max(0.0, min(xa1, xb1) - max(xa0, xb0))
                    span = min(xa1 - xa0, xb1 - xb0)
                    if span > 0:
                        max_overlap_frac = max(max_overlap_frac, overlap / span)

            if max_overlap_frac < 0.30:
                for uid in unit_ids:
                    uid_live = [c for c in live_cells if c.unit_id == uid]
                    zones.extend(_zones_for_unit(uid_live, storey, uid, brief_rooms=brief_rooms))
                continue

        # Single-unit storey (stacked) — group all live cells under one unit_id
        uid = unit_ids[0] if unit_ids else 0
        zones.extend(_zones_for_unit(live_cells, storey, uid, brief_rooms=brief_rooms))

    return TypologyTemplate(zones=tuple(zones), structural_rules={})
