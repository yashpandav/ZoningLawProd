"""System prompt, user prompt template, and few-shot examples for the
FloorPlanJSON LLM generator.

The SYSTEM_PROMPT is long and stable — pass it as a cached system message so
repeated calls within a session pay only for the user prompt tokens.
"""
from __future__ import annotations

SYSTEM_PROMPT = """You are PackGen-Architect, a Toronto residential architectural plan generator. You produce ONE JSON object per call, conforming exactly to the FloorPlanJSON schema. You never produce prose; you never produce markdown; you never apologize. You ALWAYS write coordinates in metres in a local Cartesian system whose origin is the front-left interior corner of the buildable envelope at established grade, +X points to the right (parallel to the front lot line, looking from the street), +Y points away from the street.

You receive:
  (A) BUILDABLE_ENVELOPE — a closed polygon (WKT) in the local frame inside which all building geometry must lie.
  (B) ZONING — resolved Toronto By-law 569-2013 parameters (max height, max storeys, building depth, side yards, multiplex rules, garden/laneway permissions).
  (C) TYPOLOGY — a TypologyTemplate identifier with zones and adjacency rules.
  (D) BRIEF — desired unit count, bedrooms, special rooms, accessibility, free-text notes.
  (E) OBC_MINIMA — the Ontario Building Code Part 9 area/dimension minimums to respect.

You MUST:
  1. Generate one continuous building footprint per storey, inside BUILDABLE_ENVELOPE, with no wall crossing the envelope.
  2. Use 200 mm thickness for exterior walls and party walls, 100 mm for interior partition walls. Wall segments must share endpoints (no gaps).
  3. Place every door on exactly one wall. Every room must have at least one door.
  4. Every bedroom must have at least one window with egress_compliant: true and clear_opening_m2 >= 0.35, no dimension < 380 mm; sill <= 1.0 m above floor.
  5. Bedrooms >= 7.0 m2 (without built-ins). Living areas >= 13.5 m2. Kitchen working area >= 4.2 m2. Hallways >= 0.86 m clear.
  6. Stair: minimum 860 mm wide, tread >= 235 mm, riser 125-200 mm, headroom >= 1.95 m. Place a stair where the typology requires inter-storey circulation.
  7. Respect TYPOLOGY adjacency rules. For a Stacked Duplex, unit B's footprint on the upper storey must align over unit A.
  8. If you cannot satisfy a constraint, return {"error": "<reason>"} and nothing else.

Avoid: rooms inside rooms, walls passing through rooms, doors floating in space, windows on party walls or demising walls, bedrooms without egress windows.

Output JSON only. Start with { and end with }."""


USER_PROMPT_TEMPLATE = """BUILDABLE_ENVELOPE (WKT, metres, local frame):
{wkt_envelope}

ZONING:
  zone: {zone_code}
  height_max_m: {height_max_m}
  storeys_max: {storeys_max}
  building_depth_max_m: {building_depth_max_m}
  side_yard_left_m: {side_yard_left_m}
  side_yard_right_m: {side_yard_right_m}
  front_yard_m: {front_yard_m}
  rear_yard_m: {rear_yard_m}
  multiplex_permitted_units: {multiplex_units}
  garden_suite_allowed: {gs_allowed}
  laneway_suite_allowed: {ls_allowed}
  notes: {special_provisions_summary}

TYPOLOGY:
  id: {typology_id}
  label: {typology_label}
  stacking: {stacking_axis}
  units: {unit_count}

BRIEF:
  units: {unit_count}
  unit_mix: {unit_mix}
  must_have: {must_have_rooms}
  free_text: {free_text_notes}

OBC_MINIMA:
  bedroom_m2: 7.0
  living_m2: 13.5
  kitchen_m2: 4.2
  hallway_clear_m: 0.86
  egress_clear_opening_m2: 0.35
  egress_min_dim_mm: 380
  stair_min_width_mm: 860
  tread_min_mm: 235
  riser_max_mm: 200

Return one FloorPlanJSON only."""


# ---------------------------------------------------------------------------
# Few-shot examples (3 typologies)
# ---------------------------------------------------------------------------
# These are included in the user prompt as reference plans to anchor the LLM
# to the correct JSON structure. Each is a minimal but valid FloorPlanJSON.

# A 8.0m x 12.0m stacked duplex:
# Ground floor: Unit A (2BR + living/kitchen/bath)
# Upper floor: Unit B (2BR + living/kitchen/bath)
EXAMPLE_STACKED_DUPLEX = {
    "units_m": "meters",
    "metadata": {
        "typology_label": "Stacked Duplex",
        "rationale": "Two stacked units maximize density on a typical 8m-wide RD lot."
    },
    "storeys": [
        {
            "level": 0,
            "elevation_m": 0.0,
            "floor_to_floor_m": 2.7,
            "walls": [
                {"id": "w0-front",  "start": [0.0, 0.0],  "end": [8.0, 0.0],  "type": "exterior",             "thickness_mm": 200},
                {"id": "w0-left",   "start": [0.0, 0.0],  "end": [0.0, 12.0], "type": "exterior",             "thickness_mm": 200},
                {"id": "w0-right",  "start": [8.0, 0.0],  "end": [8.0, 12.0], "type": "exterior",             "thickness_mm": 200},
                {"id": "w0-top",    "start": [0.0, 12.0], "end": [8.0, 12.0], "type": "party",                "thickness_mm": 200, "fire_rating_min": 60},
                {"id": "w0-p1",     "start": [5.0, 0.0],  "end": [5.0, 6.0],  "type": "interior_partition",   "thickness_mm": 100},
                {"id": "w0-p2",     "start": [0.0, 6.0],  "end": [8.0, 6.0],  "type": "interior_loadbearing", "thickness_mm": 150},
                {"id": "w0-p3",     "start": [5.0, 6.0],  "end": [5.0, 12.0], "type": "interior_partition",   "thickness_mm": 100},
                {"id": "w0-stair",  "start": [6.0, 0.0],  "end": [6.0, 3.5],  "type": "interior_partition",   "thickness_mm": 100}
            ],
            "doors": [
                {"id": "d0-entry",  "wall_id": "w0-front",  "position_along_wall_m": 1.0,  "width_m": 0.91, "swing": "right_in",  "connects_rooms": ["outside", "r0-entry"]},
                {"id": "d0-bed1",   "wall_id": "w0-p2",     "position_along_wall_m": 1.0,  "width_m": 0.81, "swing": "left_in",   "connects_rooms": ["r0-living", "r0-bed1"]},
                {"id": "d0-bed2",   "wall_id": "w0-p2",     "position_along_wall_m": 5.5,  "width_m": 0.81, "swing": "right_in",  "connects_rooms": ["r0-living", "r0-bed2"]},
                {"id": "d0-bath",   "wall_id": "w0-p3",     "position_along_wall_m": 1.0,  "width_m": 0.76, "swing": "left_in",   "connects_rooms": ["r0-living", "r0-bath"]},
                {"id": "d0-stair",  "wall_id": "w0-stair",  "position_along_wall_m": 0.5,  "width_m": 0.91, "swing": "right_out", "connects_rooms": ["r0-entry", "r0-stair"]}
            ],
            "windows": [
                {"id": "win0-liv1",  "wall_id": "w0-front", "position_along_wall_m": 1.5, "width_m": 1.5, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": False},
                {"id": "win0-bed1",  "wall_id": "w0-left",  "position_along_wall_m": 7.0, "width_m": 1.2, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": True, "clear_opening_m2": 0.42},
                {"id": "win0-bed2",  "wall_id": "w0-right", "position_along_wall_m": 7.0, "width_m": 1.2, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": True, "clear_opening_m2": 0.42},
                {"id": "win0-kit",   "wall_id": "w0-right", "position_along_wall_m": 2.0, "width_m": 0.9, "sill_m": 1.0, "head_m": 2.0, "operable": True,  "egress_compliant": False}
            ],
            "rooms": [
                {"id": "r0-entry",  "label": "Entry",        "category": "entry",   "dwelling_unit_id": "A", "polygon": [[0.0,0.0],[5.0,0.0],[5.0,2.0],[0.0,2.0]]},
                {"id": "r0-stair",  "label": "Stair to U-B", "category": "stair",   "dwelling_unit_id": None,"polygon": [[6.0,0.0],[8.0,0.0],[8.0,3.5],[6.0,3.5]]},
                {"id": "r0-living", "label": "Living/Dining/Kitchen","category": "living_dining_kitchen","dwelling_unit_id":"A","polygon": [[0.0,2.0],[8.0,2.0],[8.0,6.0],[0.0,6.0]],"area_m2": 24.0},
                {"id": "r0-bath",   "label": "Bathroom",     "category": "bathroom","dwelling_unit_id": "A", "polygon": [[5.0,6.0],[8.0,6.0],[8.0,9.0],[5.0,9.0]],"area_m2": 9.0},
                {"id": "r0-bed1",   "label": "Bedroom 1",    "category": "bedroom", "dwelling_unit_id": "A", "polygon": [[0.0,6.0],[5.0,6.0],[5.0,9.5],[0.0,9.5]],  "area_m2": 17.5},
                {"id": "r0-bed2",   "label": "Bedroom 2",    "category": "bedroom", "dwelling_unit_id": "A", "polygon": [[0.0,9.5],[8.0,9.5],[8.0,12.0],[0.0,12.0]],"area_m2": 20.0}
            ]
        },
        {
            "level": 1,
            "elevation_m": 2.7,
            "floor_to_floor_m": 2.7,
            "walls": [
                {"id": "w1-front",  "start": [0.0, 0.0],  "end": [8.0, 0.0],  "type": "exterior",             "thickness_mm": 200},
                {"id": "w1-left",   "start": [0.0, 0.0],  "end": [0.0, 12.0], "type": "exterior",             "thickness_mm": 200},
                {"id": "w1-right",  "start": [8.0, 0.0],  "end": [8.0, 12.0], "type": "exterior",             "thickness_mm": 200},
                {"id": "w1-rear",   "start": [0.0, 12.0], "end": [8.0, 12.0], "type": "exterior",             "thickness_mm": 200},
                {"id": "w1-p1",     "start": [0.0, 3.5],  "end": [8.0, 3.5],  "type": "interior_partition",   "thickness_mm": 100},
                {"id": "w1-p2",     "start": [0.0, 7.0],  "end": [8.0, 7.0],  "type": "interior_loadbearing", "thickness_mm": 150},
                {"id": "w1-p3",     "start": [5.0, 7.0],  "end": [5.0, 12.0], "type": "interior_partition",   "thickness_mm": 100}
            ],
            "doors": [
                {"id": "d1-entry",  "wall_id": "w1-front",  "position_along_wall_m": 7.0, "width_m": 0.91, "swing": "left_in",   "connects_rooms": ["outside", "r1-landing"]},
                {"id": "d1-living", "wall_id": "w1-p1",     "position_along_wall_m": 2.0, "width_m": 0.81, "swing": "right_in",  "connects_rooms": ["r1-landing", "r1-living"]},
                {"id": "d1-bed1",   "wall_id": "w1-p2",     "position_along_wall_m": 1.0, "width_m": 0.81, "swing": "left_in",   "connects_rooms": ["r1-living", "r1-bed1"]},
                {"id": "d1-bed2",   "wall_id": "w1-p2",     "position_along_wall_m": 5.5, "width_m": 0.81, "swing": "right_in",  "connects_rooms": ["r1-living", "r1-bed2"]},
                {"id": "d1-bath",   "wall_id": "w1-p3",     "position_along_wall_m": 1.0, "width_m": 0.76, "swing": "left_in",   "connects_rooms": ["r1-living", "r1-bath"]}
            ],
            "windows": [
                {"id": "win1-land",  "wall_id": "w1-front",  "position_along_wall_m": 2.0, "width_m": 1.2, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": False},
                {"id": "win1-bed1",  "wall_id": "w1-left",   "position_along_wall_m": 8.0, "width_m": 1.2, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": True, "clear_opening_m2": 0.42},
                {"id": "win1-bed2",  "wall_id": "w1-right",  "position_along_wall_m": 8.0, "width_m": 1.2, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": True, "clear_opening_m2": 0.42},
                {"id": "win1-rear",  "wall_id": "w1-rear",   "position_along_wall_m": 3.0, "width_m": 1.8, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": False}
            ],
            "rooms": [
                {"id": "r1-landing","label": "Landing",      "category": "entry",   "dwelling_unit_id": "B", "polygon": [[6.0,0.0],[8.0,0.0],[8.0,3.5],[6.0,3.5]]},
                {"id": "r1-living", "label": "Living/Dining/Kitchen","category": "living_dining_kitchen","dwelling_unit_id":"B","polygon": [[0.0,3.5],[8.0,3.5],[8.0,7.0],[0.0,7.0]],"area_m2": 28.0},
                {"id": "r1-bath",   "label": "Bathroom",     "category": "bathroom","dwelling_unit_id": "B", "polygon": [[5.0,7.0],[8.0,7.0],[8.0,10.0],[5.0,10.0]],"area_m2": 9.0},
                {"id": "r1-bed1",   "label": "Bedroom 1",    "category": "bedroom", "dwelling_unit_id": "B", "polygon": [[0.0,7.0],[5.0,7.0],[5.0,10.5],[0.0,10.5]], "area_m2": 17.5},
                {"id": "r1-bed2",   "label": "Bedroom 2",    "category": "bedroom", "dwelling_unit_id": "B", "polygon": [[0.0,10.5],[8.0,10.5],[8.0,12.0],[0.0,12.0]],"area_m2": 12.0}
            ]
        }
    ],
    "stairs": [
        {
            "id": "stair-ab",
            "footprint": [[6.0,0.0],[8.0,0.0],[8.0,3.5],[6.0,3.5]],
            "from_level": 0,
            "to_level": 1,
            "tread_count": 14,
            "tread_mm": 280,
            "riser_mm": 190,
            "direction": "up_north"
        }
    ]
}


# A 10.0m × 10.0m garden suite (single storey, 1 unit):
EXAMPLE_GARDEN_SUITE = {
    "units_m": "meters",
    "metadata": {
        "typology_label": "Garden Suite",
        "rationale": "Single-storey garden suite maximizing rear yard coverage."
    },
    "storeys": [
        {
            "level": 0,
            "elevation_m": 0.0,
            "floor_to_floor_m": 2.7,
            "walls": [
                {"id": "gs-front",  "start": [0.0, 0.0],  "end": [8.0, 0.0],  "type": "exterior", "thickness_mm": 200},
                {"id": "gs-left",   "start": [0.0, 0.0],  "end": [0.0, 7.5],  "type": "exterior", "thickness_mm": 200},
                {"id": "gs-right",  "start": [8.0, 0.0],  "end": [8.0, 7.5],  "type": "exterior", "thickness_mm": 200},
                {"id": "gs-rear",   "start": [0.0, 7.5],  "end": [8.0, 7.5],  "type": "exterior", "thickness_mm": 200},
                {"id": "gs-p1",     "start": [5.0, 0.0],  "end": [5.0, 4.5],  "type": "interior_partition", "thickness_mm": 100},
                {"id": "gs-p2",     "start": [0.0, 4.5],  "end": [8.0, 4.5],  "type": "interior_partition", "thickness_mm": 100}
            ],
            "doors": [
                {"id": "gs-d-entry", "wall_id": "gs-front", "position_along_wall_m": 1.0, "width_m": 0.91, "swing": "right_in", "connects_rooms": ["outside", "gs-entry"]},
                {"id": "gs-d-bed",   "wall_id": "gs-p2",    "position_along_wall_m": 1.0, "width_m": 0.81, "swing": "left_in",  "connects_rooms": ["gs-living", "gs-bed"]},
                {"id": "gs-d-bath",  "wall_id": "gs-p1",    "position_along_wall_m": 2.0, "width_m": 0.76, "swing": "right_in", "connects_rooms": ["gs-living", "gs-bath"]}
            ],
            "windows": [
                {"id": "gs-w1", "wall_id": "gs-front",  "position_along_wall_m": 1.5, "width_m": 1.5, "sill_m": 0.9, "head_m": 2.1, "operable": True, "egress_compliant": False},
                {"id": "gs-w2", "wall_id": "gs-left",   "position_along_wall_m": 5.5, "width_m": 1.0, "sill_m": 0.9, "head_m": 2.1, "operable": True, "egress_compliant": True, "clear_opening_m2": 0.38},
                {"id": "gs-w3", "wall_id": "gs-rear",   "position_along_wall_m": 3.0, "width_m": 1.5, "sill_m": 0.9, "head_m": 2.1, "operable": True, "egress_compliant": False}
            ],
            "rooms": [
                {"id": "gs-entry",  "label": "Entry",   "category": "entry",   "dwelling_unit_id": "GS", "polygon": [[0.0,0.0],[5.0,0.0],[5.0,2.0],[0.0,2.0]]},
                {"id": "gs-living", "label": "Living/Dining/Kitchen", "category": "living_dining_kitchen", "dwelling_unit_id": "GS", "polygon": [[0.0,2.0],[8.0,2.0],[8.0,4.5],[0.0,4.5]], "area_m2": 20.0},
                {"id": "gs-bath",   "label": "Bathroom","category": "bathroom", "dwelling_unit_id": "GS", "polygon": [[5.0,0.0],[8.0,0.0],[8.0,4.5],[5.0,4.5]]},
                {"id": "gs-bed",    "label": "Bedroom", "category": "bedroom",  "dwelling_unit_id": "GS", "polygon": [[0.0,4.5],[8.0,4.5],[8.0,7.5],[0.0,7.5]], "area_m2": 24.0}
            ]
        }
    ],
    "stairs": []
}


# Side-by-side duplex (2 units, single storey, horizontal stacking):
EXAMPLE_SIDE_BY_SIDE_DUPLEX = {
    "units_m": "meters",
    "metadata": {
        "typology_label": "Side-by-side Duplex",
        "rationale": "Two units side-by-side on a wider 12m lot, each with private entry."
    },
    "storeys": [
        {
            "level": 0,
            "elevation_m": 0.0,
            "floor_to_floor_m": 2.7,
            "walls": [
                {"id": "sb-front",  "start": [0.0, 0.0],   "end": [12.0, 0.0],  "type": "exterior",           "thickness_mm": 200},
                {"id": "sb-left",   "start": [0.0, 0.0],   "end": [0.0,  11.0], "type": "exterior",           "thickness_mm": 200},
                {"id": "sb-right",  "start": [12.0, 0.0],  "end": [12.0, 11.0], "type": "exterior",           "thickness_mm": 200},
                {"id": "sb-rear",   "start": [0.0,  11.0], "end": [12.0, 11.0], "type": "exterior",           "thickness_mm": 200},
                {"id": "sb-party",  "start": [6.0,  0.0],  "end": [6.0,  11.0], "type": "party",              "thickness_mm": 200, "fire_rating_min": 60},
                {"id": "sb-pa1",    "start": [0.0,  3.0],  "end": [6.0,  3.0],  "type": "interior_partition", "thickness_mm": 100},
                {"id": "sb-pa2",    "start": [0.0,  7.0],  "end": [6.0,  7.0],  "type": "interior_partition", "thickness_mm": 100},
                {"id": "sb-pb1",    "start": [6.0,  3.0],  "end": [12.0, 3.0],  "type": "interior_partition", "thickness_mm": 100},
                {"id": "sb-pb2",    "start": [6.0,  7.0],  "end": [12.0, 7.0],  "type": "interior_partition", "thickness_mm": 100}
            ],
            "doors": [
                {"id": "d-a-entry", "wall_id": "sb-front", "position_along_wall_m": 1.0, "width_m": 0.91, "swing": "right_in", "connects_rooms": ["outside", "ra-entry"]},
                {"id": "d-b-entry", "wall_id": "sb-front", "position_along_wall_m": 7.0, "width_m": 0.91, "swing": "right_in", "connects_rooms": ["outside", "rb-entry"]},
                {"id": "d-a-bed",   "wall_id": "sb-pa2",   "position_along_wall_m": 1.0, "width_m": 0.81, "swing": "left_in",  "connects_rooms": ["ra-living", "ra-bed"]},
                {"id": "d-a-bath",  "wall_id": "sb-pa1",   "position_along_wall_m": 3.5, "width_m": 0.76, "swing": "right_in", "connects_rooms": ["ra-entry", "ra-bath"]},
                {"id": "d-b-bed",   "wall_id": "sb-pb2",   "position_along_wall_m": 1.0, "width_m": 0.81, "swing": "left_in",  "connects_rooms": ["rb-living", "rb-bed"]},
                {"id": "d-b-bath",  "wall_id": "sb-pb1",   "position_along_wall_m": 3.5, "width_m": 0.76, "swing": "right_in", "connects_rooms": ["rb-entry", "rb-bath"]}
            ],
            "windows": [
                {"id": "w-a-liv", "wall_id": "sb-front", "position_along_wall_m": 2.0, "width_m": 1.5, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": False},
                {"id": "w-b-liv", "wall_id": "sb-front", "position_along_wall_m": 8.0, "width_m": 1.5, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": False},
                {"id": "w-a-bed", "wall_id": "sb-left",  "position_along_wall_m": 8.0, "width_m": 1.2, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": True, "clear_opening_m2": 0.42},
                {"id": "w-b-bed", "wall_id": "sb-right", "position_along_wall_m": 8.0, "width_m": 1.2, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": True, "clear_opening_m2": 0.42},
                {"id": "w-rear",  "wall_id": "sb-rear",  "position_along_wall_m": 5.0, "width_m": 2.0, "sill_m": 0.9, "head_m": 2.1, "operable": True,  "egress_compliant": False}
            ],
            "rooms": [
                {"id": "ra-entry",  "label": "Entry A",              "category": "entry",                 "dwelling_unit_id": "A", "polygon": [[0.0,0.0],[6.0,0.0],[6.0,3.0],[0.0,3.0]]},
                {"id": "ra-bath",   "label": "Bathroom A",           "category": "bathroom",              "dwelling_unit_id": "A", "polygon": [[4.0,0.0],[6.0,0.0],[6.0,3.0],[4.0,3.0]]},
                {"id": "ra-living", "label": "Living/Dining/Kitchen","category": "living_dining_kitchen", "dwelling_unit_id": "A", "polygon": [[0.0,3.0],[6.0,3.0],[6.0,7.0],[0.0,7.0]], "area_m2": 24.0},
                {"id": "ra-bed",    "label": "Bedroom A",            "category": "bedroom",               "dwelling_unit_id": "A", "polygon": [[0.0,7.0],[6.0,7.0],[6.0,11.0],[0.0,11.0]], "area_m2": 24.0},
                {"id": "rb-entry",  "label": "Entry B",              "category": "entry",                 "dwelling_unit_id": "B", "polygon": [[6.0,0.0],[12.0,0.0],[12.0,3.0],[6.0,3.0]]},
                {"id": "rb-bath",   "label": "Bathroom B",           "category": "bathroom",              "dwelling_unit_id": "B", "polygon": [[6.0,0.0],[8.0,0.0],[8.0,3.0],[6.0,3.0]]},
                {"id": "rb-living", "label": "Living/Dining/Kitchen","category": "living_dining_kitchen", "dwelling_unit_id": "B", "polygon": [[6.0,3.0],[12.0,3.0],[12.0,7.0],[6.0,7.0]], "area_m2": 24.0},
                {"id": "rb-bed",    "label": "Bedroom B",            "category": "bedroom",               "dwelling_unit_id": "B", "polygon": [[6.0,7.0],[12.0,7.0],[12.0,11.0],[6.0,11.0]], "area_m2": 24.0}
            ]
        }
    ],
    "stairs": []
}


EXAMPLES = [EXAMPLE_STACKED_DUPLEX, EXAMPLE_SIDE_BY_SIDE_DUPLEX, EXAMPLE_GARDEN_SUITE]
