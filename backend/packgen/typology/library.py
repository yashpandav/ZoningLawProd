"""
12-typology stamp library — Phase 1.

All stamps are normalized to [0,1]² per storey:
  x=0: left side, x=1: right side (looking from street)
  y=0: front (street), y=1: rear

Cell roles with OBC Part 9 minimums embedded:
  bedroom           min 7.0 m², 1 dim ≥ 2.0 m, egress window required
  master_bedroom    min 9.0 m², 1 dim ≥ 2.7 m
  living            min 13.5 m² combined living/dining
  kitchen           min 4.2 m²
  bathroom          min 3.7 m²
  stair             min 860 mm clear width (is_stretchable=False)

COVERAGE NOTE:
  Residential R/RD/RS/RT zones do NOT have a fixed coverage % unless the
  coverage overlay map shows one.  The depth-limit + setbacks are the binding
  constraints for principal buildings in those zones.
"""
from .models import Cell, TemplateZone, Typology, TypologyTemplate

# ── shared zones ──────────────────────────────────────────────────────────────
_R_ZONES = ("R", "RD", "RS", "RT", "RM")


def _c(role, uid, s, x0, y0, x1, y1, *, egress=False, stretch=True,
       min_a=0.0, min_d=0.0):
    return Cell(
        role=role, unit_id=uid, storey=s,
        x0=x0, y0=y0, x1=x1, y1=y1,
        needs_egress_window=egress, is_stretchable=stretch,
        min_area_m2=min_a, min_dim_m=min_d,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. duplex-stack-classic  (2 units, 2 storeys, vertical)
#    Ground floor unit 0 · Second floor unit 1
#    Internal stair at front-right
# ─────────────────────────────────────────────────────────────────────────────
_DUPLEX_STACK = (
    # Ground — unit 0
    _c("entry",    0, 0, 0.00, 0.00, 0.20, 0.15),
    _c("stair",   -1, 0, 0.75, 0.00, 1.00, 0.30, stretch=False),
    _c("living",   0, 0, 0.00, 0.15, 0.75, 0.50, min_a=13.5),
    _c("kitchen",  0, 0, 0.75, 0.30, 1.00, 0.55, min_a=4.2),
    _c("bathroom", 0, 0, 0.75, 0.55, 1.00, 0.75, min_a=3.7),
    _c("bedroom",  0, 0, 0.00, 0.55, 0.75, 0.85, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  0, 0, 0.00, 0.85, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Second — unit 1
    _c("stair",   -1, 1, 0.75, 0.00, 1.00, 0.30, stretch=False),
    _c("living",   1, 1, 0.00, 0.00, 0.75, 0.40, min_a=13.5),
    _c("kitchen",  1, 1, 0.75, 0.30, 1.00, 0.55, min_a=4.2),
    _c("bathroom", 1, 1, 0.75, 0.55, 1.00, 0.75, min_a=3.7),
    _c("bedroom",  1, 1, 0.00, 0.50, 0.75, 0.80, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 1, 1, 0.00, 0.80, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
)

_DUPLEX_STACK_TEMPLATE = TypologyTemplate(
    zones=(
        # ── Ground floor (storey 0) — unit 0 ──────────────────────────────
        TemplateZone(
            zone_id="entry_ground",
            storey=0, x0=0.00, y0=0.00, x1=0.20, y1=0.15,
            valid_roles=("entry",),
            max_subdivisions=1, is_circulation=True,
        ),
        TemplateZone(
            zone_id="stair_anchor_ground",
            storey=0, x0=0.75, y0=0.00, x1=1.00, y1=0.30,
            valid_roles=("stair",),
            max_subdivisions=1, is_circulation=True,
            notes="Must align with stair_anchor_upper across storeys.",
        ),
        TemplateZone(
            zone_id="front_public_ground",
            storey=0, x0=0.20, y0=0.15, x1=0.75, y1=0.50,
            valid_roles=("living", "dining", "balcony"),
            max_subdivisions=3, subdivision_axis="x",
        ),
        TemplateZone(
            zone_id="mid_service_ground",
            storey=0, x0=0.75, y0=0.30, x1=1.00, y1=0.75,
            valid_roles=("kitchen", "bathroom", "powder_room", "laundry"),
            max_subdivisions=3, subdivision_axis="y",
        ),
        TemplateZone(
            zone_id="rear_private_ground",
            storey=0, x0=0.00, y0=0.55, x1=0.75, y1=1.00,
            valid_roles=("bedroom", "master_bedroom"),
            max_subdivisions=2, subdivision_axis="y",
        ),
        # ── Upper floor (storey 1) — unit 1 ───────────────────────────────
        TemplateZone(
            zone_id="stair_anchor_upper",
            storey=1, x0=0.75, y0=0.00, x1=1.00, y1=0.30,
            valid_roles=("stair",),
            max_subdivisions=1, is_circulation=True,
        ),
        TemplateZone(
            zone_id="front_public_upper",
            storey=1, x0=0.00, y0=0.00, x1=0.75, y1=0.40,
            valid_roles=("living", "dining"),
            max_subdivisions=2, subdivision_axis="x",
        ),
        TemplateZone(
            zone_id="mid_service_upper",
            storey=1, x0=0.75, y0=0.30, x1=1.00, y1=0.75,
            valid_roles=("kitchen", "bathroom", "powder_room", "laundry"),
            max_subdivisions=3, subdivision_axis="y",
        ),
        TemplateZone(
            zone_id="rear_private_upper",
            storey=1, x0=0.00, y0=0.50, x1=1.00, y1=1.00,
            valid_roles=("bedroom", "master_bedroom"),
            max_subdivisions=3, subdivision_axis="y",
        ),
    ),
    structural_rules={
        "stair_must_align_across_storeys": True,
        "unit_0_storeys": [0],
        "unit_1_storeys": [1],
    },
)

DUPLEX_STACK_CLASSIC = Typology(
    id="duplex-stack-classic",
    label="Stacked Duplex",
    units_produced=2,
    stacking_axis="vertical",
    min_frontage_m=5.5, max_frontage_m=9.0,
    min_depth_m=11.0,   max_depth_m=17.0,
    target_storeys=2, requires_basement=False,
    target_gfa_per_unit_m2=(60.0, 85.0),
    stamp_cells=_DUPLEX_STACK,
    corridor_axis="end", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Upper/lower duplex. Shared internal stair at front-right.",
    template=_DUPLEX_STACK_TEMPLATE,
)


# ─────────────────────────────────────────────────────────────────────────────
# 2. duplex-side-by-side (2 units, 2 storeys, horizontal, wide lots)
# ─────────────────────────────────────────────────────────────────────────────
_DUPLEX_SBS = (
    # Left unit — unit 0 (x=[0, 0.5])
    _c("entry",    0, 0, 0.00, 0.00, 0.10, 0.18),
    _c("stair",   -1, 0, 0.00, 0.00, 0.10, 0.30, stretch=False),
    _c("living",   0, 0, 0.10, 0.00, 0.50, 0.40, min_a=13.5),
    _c("kitchen",  0, 0, 0.10, 0.40, 0.50, 0.62, min_a=4.2),
    _c("bathroom", 0, 0, 0.00, 0.62, 0.22, 0.80, min_a=3.7),
    _c("bedroom",  0, 0, 0.22, 0.62, 0.50, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  0, 1, 0.00, 0.00, 0.50, 0.50, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 0, 1, 0.00, 0.50, 0.50, 1.00, egress=True, min_a=9.0, min_d=2.7),
    # Right unit — unit 1 (x=[0.5, 1.0])
    _c("entry",    1, 0, 0.90, 0.00, 1.00, 0.18),
    _c("stair",   -1, 0, 0.90, 0.00, 1.00, 0.30, stretch=False),
    _c("living",   1, 0, 0.50, 0.00, 0.90, 0.40, min_a=13.5),
    _c("kitchen",  1, 0, 0.50, 0.40, 0.90, 0.62, min_a=4.2),
    _c("bathroom", 1, 0, 0.78, 0.62, 1.00, 0.80, min_a=3.7),
    _c("bedroom",  1, 0, 0.50, 0.62, 0.78, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  1, 1, 0.50, 0.00, 1.00, 0.50, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 1, 1, 0.50, 0.50, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
)

DUPLEX_SIDE_BY_SIDE = Typology(
    id="duplex-side-by-side",
    label="Side-by-Side Duplex",
    units_produced=2,
    stacking_axis="horizontal",
    min_frontage_m=9.0, max_frontage_m=15.0,
    min_depth_m=11.0,   max_depth_m=17.0,
    target_storeys=2, requires_basement=False,
    target_gfa_per_unit_m2=(70.0, 100.0),
    stamp_cells=_DUPLEX_SBS,
    corridor_axis="end", stair_position="end",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Side-by-side 2-storey units sharing a common party wall.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 3-stack-bilateral-stair  (3 units, 3 storeys, vertical)
# ─────────────────────────────────────────────────────────────────────────────
_TRIPLEX_STACK = (
    # Basement — unit 2 (garden unit)
    _c("stair",   -1,-1, 0.80, 0.00, 1.00, 0.30, stretch=False),
    _c("entry",    2,-1, 0.00, 0.00, 0.20, 0.18),
    _c("living",   2,-1, 0.00, 0.18, 0.80, 0.55, min_a=13.5),
    _c("kitchen",  2,-1, 0.00, 0.55, 0.50, 0.75, min_a=4.2),
    _c("bathroom", 2,-1, 0.50, 0.55, 0.80, 0.75, min_a=3.7),
    _c("bedroom",  2,-1, 0.00, 0.75, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Ground — unit 0
    _c("stair",   -1, 0, 0.80, 0.00, 1.00, 0.30, stretch=False),
    _c("entry",    0, 0, 0.00, 0.00, 0.20, 0.18),
    _c("living",   0, 0, 0.00, 0.18, 0.80, 0.50, min_a=13.5),
    _c("kitchen",  0, 0, 0.00, 0.50, 0.50, 0.72, min_a=4.2),
    _c("bathroom", 0, 0, 0.50, 0.50, 0.80, 0.72, min_a=3.7),
    _c("bedroom",  0, 0, 0.00, 0.72, 0.55, 0.90, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  0, 0, 0.55, 0.72, 1.00, 0.90, egress=True, min_a=7.0, min_d=2.0),
    # Second — unit 1
    _c("stair",   -1, 1, 0.80, 0.00, 1.00, 0.30, stretch=False),
    _c("living",   1, 1, 0.00, 0.00, 0.80, 0.42, min_a=13.5),
    _c("kitchen",  1, 1, 0.00, 0.42, 0.50, 0.62, min_a=4.2),
    _c("bathroom", 1, 1, 0.50, 0.42, 0.80, 0.62, min_a=3.7),
    _c("bedroom",  1, 1, 0.00, 0.62, 0.55, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 1, 1, 0.55, 0.62, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
)

TRIPLEX_STACK = Typology(
    id="3-stack-bilateral-stair",
    label="Stacked Triplex with Basement Garden Suite",
    units_produced=3,
    stacking_axis="vertical",
    min_frontage_m=6.5, max_frontage_m=10.0,
    min_depth_m=12.0,   max_depth_m=17.0,
    target_storeys=3, requires_basement=True,
    target_gfa_per_unit_m2=(55.0, 80.0),
    stamp_cells=_TRIPLEX_STACK,
    corridor_axis="end", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Ground + 2nd + basement garden unit. Side stair at rear-right.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 4. triplex-front-back-back  (3 units, 2 storeys, mixed)
# ─────────────────────────────────────────────────────────────────────────────
_TRIPLEX_FBB = (
    # Front unit — unit 0 (ground + second, y=[0, 0.50])
    _c("entry",    0, 0, 0.10, 0.00, 0.30, 0.18),
    _c("living",   0, 0, 0.00, 0.00, 1.00, 0.45, min_a=13.5),
    _c("kitchen",  0, 0, 0.60, 0.00, 1.00, 0.45, min_a=4.2),
    _c("bathroom", 0, 1, 0.80, 0.00, 1.00, 0.30, min_a=3.7),
    _c("bedroom",  0, 1, 0.00, 0.00, 0.50, 0.50, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 0, 1, 0.50, 0.00, 1.00, 0.50, egress=True, min_a=9.0, min_d=2.7),
    # Rear ground — unit 1 (y=[0.50, 0.75])
    _c("stair",   -1, 0, 0.00, 0.45, 0.18, 0.55, stretch=False),
    _c("entry",    1, 0, 0.00, 0.50, 0.18, 0.62),
    _c("living",   1, 0, 0.18, 0.50, 1.00, 0.72, min_a=13.5),
    _c("kitchen",  1, 0, 0.18, 0.72, 0.65, 0.88, min_a=4.2),
    _c("bathroom", 1, 0, 0.65, 0.72, 1.00, 0.88, min_a=3.7),
    _c("bedroom",  1, 0, 0.18, 0.88, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Basement — unit 2
    _c("stair",   -1,-1, 0.00, 0.00, 0.18, 0.25, stretch=False),
    _c("entry",    2,-1, 0.18, 0.00, 0.40, 0.18),
    _c("living",   2,-1, 0.40, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  2,-1, 0.00, 0.25, 0.50, 0.55, min_a=4.2),
    _c("bathroom", 2,-1, 0.50, 0.25, 0.80, 0.55, min_a=3.7),
    _c("bedroom",  2,-1, 0.00, 0.55, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
)

TRIPLEX_FRONT_BACK = Typology(
    id="triplex-front-back-back",
    label="Triplex: Front Unit + Rear Ground + Basement",
    units_produced=3,
    stacking_axis="mixed",
    min_frontage_m=8.0, max_frontage_m=12.0,
    min_depth_m=14.0,   max_depth_m=19.0,
    target_storeys=2, requires_basement=True,
    target_gfa_per_unit_m2=(60.0, 90.0),
    stamp_cells=_TRIPLEX_FBB,
    corridor_axis="central", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Front unit spans 2 storeys; rear ground unit + basement unit at back.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 5. 2-up-2-down-shared-stair  (4 units, 2 storeys, vertical)
#    Two units per floor sharing a central stair; side-by-side per floor.
# ─────────────────────────────────────────────────────────────────────────────
_4UNIT_2UP2DOWN = (
    # Ground-left — unit 0
    _c("entry",    0, 0, 0.05, 0.00, 0.20, 0.18),
    _c("living",   0, 0, 0.00, 0.18, 0.48, 0.52, min_a=13.5),
    _c("kitchen",  0, 0, 0.00, 0.52, 0.48, 0.72, min_a=4.2),
    _c("bathroom", 0, 0, 0.00, 0.72, 0.25, 0.90, min_a=3.7),
    _c("bedroom",  0, 0, 0.25, 0.72, 0.48, 0.90, egress=True, min_a=7.0, min_d=2.0),
    # Ground-right — unit 1
    _c("entry",    1, 0, 0.80, 0.00, 0.95, 0.18),
    _c("living",   1, 0, 0.52, 0.18, 1.00, 0.52, min_a=13.5),
    _c("kitchen",  1, 0, 0.52, 0.52, 1.00, 0.72, min_a=4.2),
    _c("bathroom", 1, 0, 0.75, 0.72, 1.00, 0.90, min_a=3.7),
    _c("bedroom",  1, 0, 0.52, 0.72, 0.75, 0.90, egress=True, min_a=7.0, min_d=2.0),
    # Shared stair (ground → second)
    _c("stair",   -1, 0, 0.40, 0.00, 0.60, 0.40, stretch=False),
    # Second-left — unit 2
    _c("living",   2, 1, 0.00, 0.00, 0.48, 0.40, min_a=13.5),
    _c("kitchen",  2, 1, 0.00, 0.40, 0.48, 0.62, min_a=4.2),
    _c("bathroom", 2, 1, 0.00, 0.62, 0.22, 0.82, min_a=3.7),
    _c("bedroom",  2, 1, 0.22, 0.62, 0.48, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 2, 1, 0.00, 0.82, 0.48, 1.00, egress=True, min_a=9.0, min_d=2.7),
    # Shared stair (second)
    _c("stair",   -1, 1, 0.40, 0.00, 0.60, 0.40, stretch=False),
    # Second-right — unit 3
    _c("living",   3, 1, 0.52, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  3, 1, 0.52, 0.40, 1.00, 0.62, min_a=4.2),
    _c("bathroom", 3, 1, 0.78, 0.62, 1.00, 0.82, min_a=3.7),
    _c("bedroom",  3, 1, 0.52, 0.62, 0.78, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 3, 1, 0.52, 0.82, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
)

FOURPLEX_2UP2DOWN = Typology(
    id="2-up-2-down-shared-stair",
    label="Fourplex: 2 Up / 2 Down, Shared Central Stair",
    units_produced=4,
    stacking_axis="vertical",
    min_frontage_m=7.0, max_frontage_m=12.0,
    min_depth_m=12.0,   max_depth_m=17.0,
    target_storeys=2, requires_basement=False,
    target_gfa_per_unit_m2=(60.0, 85.0),
    stamp_cells=_4UNIT_2UP2DOWN,
    corridor_axis="central", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Two units per floor, shared central stair, suitable for wider lots.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 6. 4-stack-internal-stair  (4 units, 4 storeys, 1 unit/floor)
#    Baseline Toronto fourplex — most common new missing-middle typology.
# ─────────────────────────────────────────────────────────────────────────────
_4STACK_INT = (
    # Basement — unit 3 (garden suite)
    _c("stair",   -1,-1, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("entry",    3,-1, 0.22, 0.00, 0.45, 0.20),
    _c("living",   3,-1, 0.22, 0.20, 1.00, 0.55, min_a=13.5),
    _c("kitchen",  3,-1, 0.22, 0.55, 0.65, 0.75, min_a=4.2),
    _c("bathroom", 3,-1, 0.65, 0.55, 1.00, 0.75, min_a=3.7),
    _c("bedroom",  3,-1, 0.00, 0.75, 0.55, 1.00, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  3,-1, 0.55, 0.75, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Ground — unit 0
    _c("stair",   -1, 0, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("entry",    0, 0, 0.22, 0.00, 0.45, 0.20),
    _c("living",   0, 0, 0.22, 0.20, 1.00, 0.52, min_a=13.5),
    _c("kitchen",  0, 0, 0.22, 0.52, 0.68, 0.72, min_a=4.2),
    _c("bathroom", 0, 0, 0.68, 0.52, 1.00, 0.72, min_a=3.7),
    _c("bedroom",  0, 0, 0.00, 0.72, 0.55, 0.90, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  0, 0, 0.55, 0.72, 1.00, 0.90, egress=True, min_a=7.0, min_d=2.0),
    # Second — unit 1
    _c("stair",   -1, 1, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("living",   1, 1, 0.22, 0.00, 1.00, 0.42, min_a=13.5),
    _c("kitchen",  1, 1, 0.22, 0.42, 0.68, 0.62, min_a=4.2),
    _c("bathroom", 1, 1, 0.68, 0.42, 1.00, 0.62, min_a=3.7),
    _c("bedroom",  1, 1, 0.00, 0.62, 0.55, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 1, 1, 0.55, 0.62, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
    # Third — unit 2
    _c("stair",   -1, 2, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("living",   2, 2, 0.22, 0.00, 1.00, 0.42, min_a=13.5),
    _c("kitchen",  2, 2, 0.22, 0.42, 0.68, 0.62, min_a=4.2),
    _c("bathroom", 2, 2, 0.68, 0.42, 1.00, 0.62, min_a=3.7),
    _c("bedroom",  2, 2, 0.00, 0.62, 0.55, 0.85, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 2, 2, 0.55, 0.62, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
)

FOURPLEX_4STACK = Typology(
    id="4-stack-internal-stair",
    label="Fourplex: One Unit Per Floor, Internal Side Stair",
    units_produced=4,
    stacking_axis="vertical",
    min_frontage_m=6.0, max_frontage_m=10.0,
    min_depth_m=13.0,   max_depth_m=17.0,
    target_storeys=4, requires_basement=True,
    target_gfa_per_unit_m2=(55.0, 80.0),
    stamp_cells=_4STACK_INT,
    corridor_axis="end", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Most common Toronto fourplex. Left-side stair, 1 unit/floor + basement.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 7. 4-stack-spiral-stair  (4 units, 4 storeys, narrow lots ≤ 7.5 m)
# ─────────────────────────────────────────────────────────────────────────────
_4STACK_NARROW = (
    # Basement — unit 3
    _c("stair",   -1,-1, 0.38, 0.00, 0.62, 0.25, stretch=False),
    _c("entry",    3,-1, 0.00, 0.00, 0.38, 0.20),
    _c("living",   3,-1, 0.00, 0.20, 1.00, 0.55, min_a=13.5),
    _c("kitchen",  3,-1, 0.00, 0.55, 0.60, 0.75, min_a=4.2),
    _c("bathroom", 3,-1, 0.60, 0.55, 1.00, 0.75, min_a=3.7),
    _c("bedroom",  3,-1, 0.00, 0.75, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Ground — unit 0
    _c("stair",   -1, 0, 0.38, 0.00, 0.62, 0.25, stretch=False),
    _c("entry",    0, 0, 0.00, 0.00, 0.38, 0.20),
    _c("living",   0, 0, 0.00, 0.22, 1.00, 0.55, min_a=13.5),
    _c("kitchen",  0, 0, 0.00, 0.55, 0.60, 0.75, min_a=4.2),
    _c("bathroom", 0, 0, 0.60, 0.55, 1.00, 0.75, min_a=3.7),
    _c("bedroom",  0, 0, 0.00, 0.75, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Second — unit 1
    _c("stair",   -1, 1, 0.38, 0.00, 0.62, 0.25, stretch=False),
    _c("living",   1, 1, 0.00, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  1, 1, 0.00, 0.40, 0.62, 0.62, min_a=4.2),
    _c("bathroom", 1, 1, 0.62, 0.40, 1.00, 0.62, min_a=3.7),
    _c("bedroom",  1, 1, 0.00, 0.62, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Third — unit 2
    _c("stair",   -1, 2, 0.38, 0.00, 0.62, 0.25, stretch=False),
    _c("living",   2, 2, 0.00, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  2, 2, 0.00, 0.40, 0.62, 0.62, min_a=4.2),
    _c("bathroom", 2, 2, 0.62, 0.40, 1.00, 0.62, min_a=3.7),
    _c("bedroom",  2, 2, 0.00, 0.62, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
)

FOURPLEX_NARROW = Typology(
    id="4-stack-spiral-stair",
    label="Fourplex: Narrow Lot, Central Stair",
    units_produced=4,
    stacking_axis="vertical",
    min_frontage_m=5.5, max_frontage_m=7.5,
    min_depth_m=12.0,   max_depth_m=17.0,
    target_storeys=4, requires_basement=True,
    target_gfa_per_unit_m2=(45.0, 65.0),
    stamp_cells=_4STACK_NARROW,
    corridor_axis="central", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Narrow-lot variant; central stair allows consistent unit width across tight frontages.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 8. 2+2-side-by-side-basement  (4 units, horizontal, 2+B)
# ─────────────────────────────────────────────────────────────────────────────
_4UNIT_SBS_BSMT = (
    # Left ground — unit 0
    _c("entry",    0, 0, 0.02, 0.00, 0.22, 0.20),
    _c("stair",   -1, 0, 0.02, 0.20, 0.22, 0.55, stretch=False),
    _c("living",   0, 0, 0.22, 0.00, 0.50, 0.45, min_a=13.5),
    _c("kitchen",  0, 0, 0.22, 0.45, 0.50, 0.65, min_a=4.2),
    _c("bathroom", 0, 0, 0.22, 0.65, 0.50, 0.82, min_a=3.7),
    _c("bedroom",  0, 1, 0.00, 0.00, 0.50, 0.42, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 0, 1, 0.00, 0.42, 0.50, 0.82, egress=True, min_a=9.0, min_d=2.7),
    # Left basement — unit 2
    _c("stair",   -1,-1, 0.02, 0.00, 0.22, 0.30, stretch=False),
    _c("entry",    2,-1, 0.22, 0.00, 0.42, 0.20),
    _c("living",   2,-1, 0.22, 0.20, 0.50, 0.55, min_a=13.5),
    _c("kitchen",  2,-1, 0.22, 0.55, 0.50, 0.75, min_a=4.2),
    _c("bathroom", 2,-1, 0.22, 0.75, 0.50, 0.92, min_a=3.7),
    _c("bedroom",  2,-1, 0.00, 0.55, 0.22, 0.92, egress=True, min_a=7.0, min_d=2.0),
    # Right ground — unit 1
    _c("entry",    1, 0, 0.78, 0.00, 0.98, 0.20),
    _c("stair",   -1, 0, 0.78, 0.20, 0.98, 0.55, stretch=False),
    _c("living",   1, 0, 0.50, 0.00, 0.78, 0.45, min_a=13.5),
    _c("kitchen",  1, 0, 0.50, 0.45, 0.78, 0.65, min_a=4.2),
    _c("bathroom", 1, 0, 0.50, 0.65, 0.78, 0.82, min_a=3.7),
    _c("bedroom",  1, 1, 0.50, 0.00, 1.00, 0.42, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 1, 1, 0.50, 0.42, 1.00, 0.82, egress=True, min_a=9.0, min_d=2.7),
    # Right basement — unit 3
    _c("stair",   -1,-1, 0.78, 0.00, 0.98, 0.30, stretch=False),
    _c("entry",    3,-1, 0.58, 0.00, 0.78, 0.20),
    _c("living",   3,-1, 0.50, 0.20, 0.78, 0.55, min_a=13.5),
    _c("kitchen",  3,-1, 0.50, 0.55, 0.78, 0.75, min_a=4.2),
    _c("bathroom", 3,-1, 0.50, 0.75, 0.78, 0.92, min_a=3.7),
    _c("bedroom",  3,-1, 0.78, 0.55, 1.00, 0.92, egress=True, min_a=7.0, min_d=2.0),
)

FOURPLEX_SBS_BSMT = Typology(
    id="2+2-side-by-side-basement",
    label="Fourplex: Side-by-Side with Basement Units",
    units_produced=4,
    stacking_axis="horizontal",
    min_frontage_m=9.0, max_frontage_m=14.0,
    min_depth_m=14.0,   max_depth_m=19.0,
    target_storeys=2, requires_basement=True,
    target_gfa_per_unit_m2=(65.0, 95.0),
    stamp_cells=_4UNIT_SBS_BSMT,
    corridor_axis="end", stair_position="end",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Two main-floor 2-storey townhouses each with its own basement unit.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 9. 2+2-side-by-side-with-corridor (4 units, wide lot, horizontal, no basement)
# ─────────────────────────────────────────────────────────────────────────────
_4UNIT_SBS_COR = (
    # Left — unit 0 (ground + second)
    _c("entry",    0, 0, 0.05, 0.00, 0.25, 0.22),
    _c("stair",   -1, 0, 0.00, 0.00, 0.18, 0.38, stretch=False),
    _c("living",   0, 0, 0.18, 0.00, 0.50, 0.45, min_a=13.5),
    _c("kitchen",  0, 0, 0.18, 0.45, 0.50, 0.65, min_a=4.2),
    _c("bathroom", 0, 0, 0.18, 0.65, 0.50, 0.85, min_a=3.7),
    _c("bedroom",  0, 1, 0.00, 0.00, 0.25, 0.55, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  0, 1, 0.25, 0.00, 0.50, 0.55, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 0, 1, 0.00, 0.55, 0.50, 1.00, egress=True, min_a=9.0, min_d=2.7),
    # Right — unit 1 (ground + second)
    _c("entry",    1, 0, 0.75, 0.00, 0.95, 0.22),
    _c("stair",   -1, 0, 0.82, 0.00, 1.00, 0.38, stretch=False),
    _c("living",   1, 0, 0.50, 0.00, 0.82, 0.45, min_a=13.5),
    _c("kitchen",  1, 0, 0.50, 0.45, 0.82, 0.65, min_a=4.2),
    _c("bathroom", 1, 0, 0.50, 0.65, 0.82, 0.85, min_a=3.7),
    _c("bedroom",  1, 1, 0.75, 0.00, 1.00, 0.55, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  1, 1, 0.50, 0.00, 0.75, 0.55, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 1, 1, 0.50, 0.55, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
)

FOURPLEX_SBS_CORRIDOR = Typology(
    id="2+2-side-by-side-with-corridor",
    label="Fourplex: Side-by-Side Townhouses, Wide Lot",
    units_produced=4,
    stacking_axis="horizontal",
    min_frontage_m=11.0, max_frontage_m=18.0,
    min_depth_m=12.0,    max_depth_m=17.0,
    target_storeys=2, requires_basement=False,
    target_gfa_per_unit_m2=(75.0, 110.0),
    stamp_cells=_4UNIT_SBS_COR,
    corridor_axis="spine", stair_position="end",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Wide-lot side-by-side; two 2-storey townhouses with separate entries.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 10. 4-with-laneway (4 units + laneway suite envelope, vertical)
# ─────────────────────────────────────────────────────────────────────────────
_4UNIT_LANEWAY = (
    # Same floorplates as 4-stack-internal-stair but shallower; rear 3m reserved for laneway
    _c("stair",   -1,-1, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("entry",    3,-1, 0.22, 0.00, 0.45, 0.20),
    _c("living",   3,-1, 0.22, 0.20, 1.00, 0.55, min_a=13.5),
    _c("kitchen",  3,-1, 0.22, 0.55, 0.65, 0.75, min_a=4.2),
    _c("bathroom", 3,-1, 0.65, 0.55, 1.00, 0.75, min_a=3.7),
    _c("bedroom",  3,-1, 0.00, 0.75, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    _c("stair",   -1, 0, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("entry",    0, 0, 0.22, 0.00, 0.45, 0.20),
    _c("living",   0, 0, 0.22, 0.20, 1.00, 0.52, min_a=13.5),
    _c("kitchen",  0, 0, 0.22, 0.52, 0.68, 0.72, min_a=4.2),
    _c("bathroom", 0, 0, 0.68, 0.52, 1.00, 0.72, min_a=3.7),
    _c("bedroom",  0, 0, 0.00, 0.72, 0.55, 0.90, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  0, 0, 0.55, 0.72, 1.00, 0.90, egress=True, min_a=7.0, min_d=2.0),
    _c("stair",   -1, 1, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("living",   1, 1, 0.22, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  1, 1, 0.22, 0.40, 0.68, 0.62, min_a=4.2),
    _c("bathroom", 1, 1, 0.68, 0.40, 1.00, 0.62, min_a=3.7),
    _c("bedroom",  1, 1, 0.00, 0.62, 0.55, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 1, 1, 0.55, 0.62, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
    _c("stair",   -1, 2, 0.00, 0.00, 0.22, 0.35, stretch=False),
    _c("living",   2, 2, 0.22, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  2, 2, 0.22, 0.40, 0.68, 0.62, min_a=4.2),
    _c("bathroom", 2, 2, 0.68, 0.40, 1.00, 0.62, min_a=3.7),
    _c("bedroom",  2, 2, 0.00, 0.62, 0.55, 0.85, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 2, 2, 0.55, 0.62, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
    # Laneway envelope zone (void — drawn as separate Z-ENV annotation)
    _c("void",    -1, 0, 0.00, 0.85, 1.00, 1.00),
)

FOURPLEX_LANEWAY = Typology(
    id="4-with-laneway",
    label="Fourplex with Laneway Suite Envelope",
    units_produced=4,
    stacking_axis="vertical",
    min_frontage_m=7.5, max_frontage_m=14.0,
    min_depth_m=14.0,   max_depth_m=19.0,
    target_storeys=3, requires_basement=True,
    target_gfa_per_unit_m2=(55.0, 80.0),
    stamp_cells=_4UNIT_LANEWAY,
    corridor_axis="end", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="4-plex massing pushed to front; rear 3m reserved for laneway suite per §150.8.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 11. 5-stack-narrow  (5 units, 3 storeys + basement, Ward 23 / T&EY only)
#     Eligible under By-law 654-2025
# ─────────────────────────────────────────────────────────────────────────────
_5STACK = (
    # Basement — unit 4
    _c("stair",   -1,-1, 0.40, 0.00, 0.60, 0.28, stretch=False),
    _c("entry",    4,-1, 0.00, 0.00, 0.40, 0.20),
    _c("living",   4,-1, 0.00, 0.20, 1.00, 0.55, min_a=13.5),
    _c("kitchen",  4,-1, 0.00, 0.55, 0.55, 0.75, min_a=4.2),
    _c("bathroom", 4,-1, 0.55, 0.55, 1.00, 0.75, min_a=3.7),
    _c("bedroom",  4,-1, 0.00, 0.75, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Ground — unit 0
    _c("stair",   -1, 0, 0.40, 0.00, 0.60, 0.28, stretch=False),
    _c("entry",    0, 0, 0.00, 0.00, 0.40, 0.20),
    _c("living",   0, 0, 0.00, 0.20, 1.00, 0.52, min_a=13.5),
    _c("kitchen",  0, 0, 0.00, 0.52, 0.62, 0.72, min_a=4.2),
    _c("bathroom", 0, 0, 0.62, 0.52, 1.00, 0.72, min_a=3.7),
    _c("bedroom",  0, 0, 0.00, 0.72, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Second — unit 1
    _c("stair",   -1, 1, 0.40, 0.00, 0.60, 0.28, stretch=False),
    _c("living",   1, 1, 0.00, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  1, 1, 0.00, 0.40, 0.62, 0.60, min_a=4.2),
    _c("bathroom", 1, 1, 0.62, 0.40, 1.00, 0.60, min_a=3.7),
    _c("bedroom",  1, 1, 0.00, 0.60, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Third — unit 2
    _c("stair",   -1, 2, 0.40, 0.00, 0.60, 0.28, stretch=False),
    _c("living",   2, 2, 0.00, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  2, 2, 0.00, 0.40, 0.62, 0.60, min_a=4.2),
    _c("bathroom", 2, 2, 0.62, 0.40, 1.00, 0.60, min_a=3.7),
    _c("bedroom",  2, 2, 0.00, 0.60, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Fourth — unit 3 (penthouse / 4th storey)
    _c("stair",   -1, 3, 0.40, 0.00, 0.60, 0.28, stretch=False),
    _c("living",   3, 3, 0.00, 0.00, 1.00, 0.45, min_a=13.5),
    _c("kitchen",  3, 3, 0.00, 0.45, 0.62, 0.65, min_a=4.2),
    _c("bathroom", 3, 3, 0.62, 0.45, 1.00, 0.65, min_a=3.7),
    _c("bedroom",  3, 3, 0.00, 0.65, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
)

FIVEPLEX_NARROW = Typology(
    id="5-stack-narrow",
    label="Fiveplex: Narrow Lot (By-law 654-2025 eligible areas only)",
    units_produced=5,
    stacking_axis="vertical",
    min_frontage_m=5.5, max_frontage_m=8.0,
    min_depth_m=13.0,   max_depth_m=17.0,
    target_storeys=4, requires_basement=True,
    target_gfa_per_unit_m2=(45.0, 65.0),
    stamp_cells=_5STACK,
    corridor_axis="central", stair_position="internal",
    eligible_zones=_R_ZONES,
    eligible_wards=(23,),  # Ward 23 Scarborough North + former T&EY — checked by selector
    notes="5-unit stacked. Only eligible in Toronto & East York or Ward 23 per By-law 654-2025.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 12. 6-stack-wide  (6 units, 4 storeys, wide lots, Ward 23 / T&EY only)
# ─────────────────────────────────────────────────────────────────────────────
_6STACK = (
    # Basement — units 4 (left) and 5 (right)
    _c("stair",   -1,-1, 0.44, 0.00, 0.56, 0.30, stretch=False),
    _c("entry",    4,-1, 0.00, 0.00, 0.22, 0.20),
    _c("living",   4,-1, 0.00, 0.20, 0.44, 0.55, min_a=13.5),
    _c("kitchen",  4,-1, 0.00, 0.55, 0.44, 0.75, min_a=4.2),
    _c("bathroom", 4,-1, 0.00, 0.75, 0.44, 0.92, min_a=3.7),
    _c("bedroom",  4,-1, 0.00, 0.92, 0.44, 1.00, egress=True, min_a=7.0, min_d=2.0),
    _c("entry",    5,-1, 0.78, 0.00, 1.00, 0.20),
    _c("living",   5,-1, 0.56, 0.20, 1.00, 0.55, min_a=13.5),
    _c("kitchen",  5,-1, 0.56, 0.55, 1.00, 0.75, min_a=4.2),
    _c("bathroom", 5,-1, 0.56, 0.75, 1.00, 0.92, min_a=3.7),
    _c("bedroom",  5,-1, 0.56, 0.92, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    # Ground — units 0 (left) 1 (right)
    _c("stair",   -1, 0, 0.44, 0.00, 0.56, 0.30, stretch=False),
    _c("entry",    0, 0, 0.05, 0.00, 0.25, 0.20),
    _c("living",   0, 0, 0.00, 0.20, 0.44, 0.52, min_a=13.5),
    _c("kitchen",  0, 0, 0.00, 0.52, 0.44, 0.72, min_a=4.2),
    _c("bathroom", 0, 0, 0.00, 0.72, 0.30, 0.90, min_a=3.7),
    _c("bedroom",  0, 0, 0.30, 0.72, 0.44, 0.90, egress=True, min_a=7.0, min_d=2.0),
    _c("entry",    1, 0, 0.75, 0.00, 0.95, 0.20),
    _c("living",   1, 0, 0.56, 0.20, 1.00, 0.52, min_a=13.5),
    _c("kitchen",  1, 0, 0.56, 0.52, 1.00, 0.72, min_a=4.2),
    _c("bathroom", 1, 0, 0.70, 0.72, 1.00, 0.90, min_a=3.7),
    _c("bedroom",  1, 0, 0.56, 0.72, 0.70, 0.90, egress=True, min_a=7.0, min_d=2.0),
    # Second — units 2 (left) 3 (right)
    _c("stair",   -1, 1, 0.44, 0.00, 0.56, 0.30, stretch=False),
    _c("living",   2, 1, 0.00, 0.00, 0.44, 0.40, min_a=13.5),
    _c("kitchen",  2, 1, 0.00, 0.40, 0.44, 0.62, min_a=4.2),
    _c("bathroom", 2, 1, 0.00, 0.62, 0.30, 0.82, min_a=3.7),
    _c("bedroom",  2, 1, 0.30, 0.62, 0.44, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 2, 1, 0.00, 0.82, 0.44, 1.00, egress=True, min_a=9.0, min_d=2.7),
    _c("living",   3, 1, 0.56, 0.00, 1.00, 0.40, min_a=13.5),
    _c("kitchen",  3, 1, 0.56, 0.40, 1.00, 0.62, min_a=4.2),
    _c("bathroom", 3, 1, 0.70, 0.62, 1.00, 0.82, min_a=3.7),
    _c("bedroom",  3, 1, 0.56, 0.62, 0.70, 0.82, egress=True, min_a=7.0, min_d=2.0),
    _c("master_bedroom", 3, 1, 0.56, 0.82, 1.00, 1.00, egress=True, min_a=9.0, min_d=2.7),
)

SIXPLEX_WIDE = Typology(
    id="6-stack-wide",
    label="Sixplex: Wide Lot, 2-Per-Floor (By-law 654-2025 eligible areas only)",
    units_produced=6,
    stacking_axis="vertical",
    min_frontage_m=9.0, max_frontage_m=14.0,
    min_depth_m=14.0,   max_depth_m=19.0,
    target_storeys=4, requires_basement=True,
    target_gfa_per_unit_m2=(55.0, 80.0),
    stamp_cells=_6STACK,
    corridor_axis="central", stair_position="split",
    eligible_zones=_R_ZONES,
    eligible_wards=(23,),
    notes="Two units per floor, 4 above-grade storeys + basement. T&EY / Ward 23 only.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 13. single-detached-2storey  (1 unit, 2 storeys)
#     Ground: entry + living + kitchen + optional ground-floor rooms
#     Upper:  bedrooms + bathrooms (all same unit_id=0)
# ─────────────────────────────────────────────────────────────────────────────
_SINGLE_2STOREY = (
    # Ground — unit 0
    # Service column is 30% wide so it survives stretch + snapping at ≥ 0.86m (OBC stair min)
    _c("entry",          0, 0, 0.00, 0.00, 0.20, 0.14),
    _c("stair",         -1, 0, 0.70, 0.00, 1.00, 0.25, stretch=False),
    _c("living",         0, 0, 0.00, 0.14, 0.70, 0.52, min_a=13.5),
    _c("dining",         0, 0, 0.00, 0.52, 0.70, 0.76),
    _c("kitchen",        0, 0, 0.70, 0.25, 1.00, 0.58, min_a=4.2),
    _c("bathroom",       0, 0, 0.70, 0.58, 1.00, 0.76, min_a=3.7),
    _c("storage",        0, 0, 0.00, 0.76, 0.70, 1.00),   # rear utility / mudroom
    _c("storage",       -1, 0, 0.70, 0.76, 1.00, 1.00),   # laundry / rear utility
    # Upper — still unit 0 (single unit spans both floors)
    _c("stair",         -1, 1, 0.70, 0.00, 1.00, 0.25, stretch=False),
    _c("master_bedroom", 0, 1, 0.00, 0.00, 0.70, 0.40, egress=True, min_a=9.0, min_d=2.7),
    _c("bedroom",        0, 1, 0.00, 0.40, 0.70, 0.80, egress=True, min_a=7.0, min_d=2.0),
    _c("bathroom",       0, 1, 0.70, 0.25, 1.00, 0.55, min_a=3.7),
    _c("bathroom",       0, 1, 0.70, 0.55, 1.00, 0.83, min_a=3.7),
    _c("storage",        0, 1, 0.00, 0.80, 0.70, 1.00),   # upper loft / storage
    _c("storage",       -1, 1, 0.70, 0.83, 1.00, 1.00),   # small upper utility
)

_SINGLE_2STOREY_TEMPLATE = TypologyTemplate(
    zones=(
        # ── Ground floor (storey 0) — service column at x=[0.70,1.00] (30% width)
        TemplateZone(
            zone_id="entry_ground",
            storey=0, x0=0.00, y0=0.00, x1=0.20, y1=0.14,
            valid_roles=("entry",), max_subdivisions=1, is_circulation=True,
        ),
        TemplateZone(
            zone_id="stair_ground",
            storey=0, x0=0.70, y0=0.00, x1=1.00, y1=0.25,
            valid_roles=("stair",), max_subdivisions=1, is_circulation=True,
            notes="30%-wide service column ensures ≥ 0.86m stair after stretch + snapping.",
        ),
        TemplateZone(
            zone_id="public_ground",
            storey=0, x0=0.00, y0=0.14, x1=0.70, y1=0.52,
            valid_roles=("living", "dining", "kitchen", "bedroom", "master_bedroom", "balcony"),
            max_subdivisions=4, subdivision_axis="x",
            notes="Front living area (storey 0). Accept bedrooms/balcony here if storey_preference=0.",
        ),
        TemplateZone(
            zone_id="service_ground",
            storey=0, x0=0.70, y0=0.25, x1=1.00, y1=0.80,
            valid_roles=("kitchen", "bathroom", "powder_room", "laundry"),
            max_subdivisions=4, subdivision_axis="y",
            notes="Right service column: kitchen, powder room, laundry.",
        ),
        TemplateZone(
            zone_id="flex_ground",
            storey=0, x0=0.00, y0=0.52, x1=0.70, y1=1.00,
            valid_roles=("bedroom", "master_bedroom", "dining", "storage", "laundry"),
            max_subdivisions=3, subdivision_axis="y",
            notes="Rear ground zone: ground-floor bedroom(s) when storey_preference=0, or dining.",
        ),
        TemplateZone(
            zone_id="rear_service_ground",
            storey=0, x0=0.70, y0=0.80, x1=1.00, y1=1.00,
            valid_roles=("storage", "laundry", "bathroom"),
            max_subdivisions=2, subdivision_axis="y",
        ),
        # ── Upper floor (storey 1) — ALL unit_id=0 (single dwelling spans both floors)
        TemplateZone(
            zone_id="stair_upper",
            storey=1, x0=0.70, y0=0.00, x1=1.00, y1=0.25,
            valid_roles=("stair",), max_subdivisions=1, is_circulation=True,
        ),
        TemplateZone(
            zone_id="private_upper_front",
            storey=1, x0=0.00, y0=0.00, x1=0.70, y1=0.42,
            valid_roles=("bedroom", "master_bedroom"),
            max_subdivisions=2, subdivision_axis="y",
            notes="Front upper bedroom(s). Use when storey_preference=1 for bedrooms.",
        ),
        TemplateZone(
            zone_id="private_upper_rear",
            storey=1, x0=0.00, y0=0.42, x1=0.70, y1=0.82,
            valid_roles=("bedroom", "master_bedroom", "bathroom"),
            max_subdivisions=3, subdivision_axis="y",
            notes="Rear upper bedroom(s) or ensuite.",
        ),
        TemplateZone(
            zone_id="bath_upper",
            storey=1, x0=0.70, y0=0.25, x1=1.00, y1=0.85,
            valid_roles=("bathroom", "laundry", "powder_room", "storage"),
            max_subdivisions=4, subdivision_axis="y",
            notes="Upper bathroom column — place both bathrooms here when storey_preference=1.",
        ),
        TemplateZone(
            zone_id="flex_upper_rear",
            storey=1, x0=0.00, y0=0.82, x1=1.00, y1=1.00,
            valid_roles=("storage", "laundry", "bathroom", "balcony"),
            max_subdivisions=3, subdivision_axis="x",
            notes="Small upper rear: loft/storage/balcony.",
        ),
    ),
    structural_rules={
        "stair_must_align_across_storeys": True,
        "unit_0_storeys": [0, 1],   # single unit spans both floors
    },
)

SINGLE_DETACHED_2STOREY = Typology(
    id="single-2storey",
    label="Single-Family Detached (2-Storey)",
    units_produced=1,
    stacking_axis="vertical",
    min_frontage_m=4.5, max_frontage_m=9.0,
    min_depth_m=8.0,    max_depth_m=17.0,
    target_storeys=2, requires_basement=False,
    target_gfa_per_unit_m2=(80.0, 180.0),
    stamp_cells=_SINGLE_2STOREY,
    corridor_axis="end", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Single dwelling unit across ground + upper floor. Ground: living/kitchen. Upper: bedrooms/bathrooms.",
    template=_SINGLE_2STOREY_TEMPLATE,
)


# ─────────────────────────────────────────────────────────────────────────────
# 14. single-bungalow  (1 unit, 1 storey, all rooms on ground)
# ─────────────────────────────────────────────────────────────────────────────
_SINGLE_BUNGALOW = (
    _c("entry",    0, 0, 0.00, 0.00, 0.15, 0.15),
    _c("living",   0, 0, 0.00, 0.15, 0.65, 0.52, min_a=13.5),
    _c("kitchen",  0, 0, 0.65, 0.15, 1.00, 0.45, min_a=4.2),
    _c("bathroom", 0, 0, 0.65, 0.45, 1.00, 0.65, min_a=3.7),
    _c("bedroom",  0, 0, 0.00, 0.52, 0.65, 0.78, egress=True, min_a=7.0, min_d=2.0),
    _c("bedroom",  0, 0, 0.00, 0.78, 1.00, 1.00, egress=True, min_a=7.0, min_d=2.0),
    _c("bathroom", 0, 0, 0.65, 0.65, 1.00, 0.85, min_a=3.7),
    _c("storage",  0, 0, 0.65, 0.85, 1.00, 1.00),
)

_SINGLE_BUNGALOW_TEMPLATE = TypologyTemplate(
    zones=(
        TemplateZone(
            zone_id="entry_main",
            storey=0, x0=0.00, y0=0.00, x1=0.15, y1=0.15,
            valid_roles=("entry",), max_subdivisions=1, is_circulation=True,
        ),
        TemplateZone(
            zone_id="public_main",
            storey=0, x0=0.00, y0=0.15, x1=0.65, y1=0.52,
            valid_roles=("living", "dining", "kitchen"),
            max_subdivisions=3, subdivision_axis="x",
            notes="Front living/dining/kitchen open plan.",
        ),
        TemplateZone(
            zone_id="service_main",
            storey=0, x0=0.65, y0=0.15, x1=1.00, y1=0.65,
            valid_roles=("kitchen", "bathroom", "powder_room", "laundry"),
            max_subdivisions=3, subdivision_axis="y",
        ),
        TemplateZone(
            zone_id="private_main",
            storey=0, x0=0.00, y0=0.52, x1=0.65, y1=1.00,
            valid_roles=("bedroom", "master_bedroom"),
            max_subdivisions=3, subdivision_axis="y",
            notes="Bedroom wing.",
        ),
        TemplateZone(
            zone_id="bath_rear",
            storey=0, x0=0.65, y0=0.65, x1=1.00, y1=1.00,
            valid_roles=("bathroom", "laundry", "storage"),
            max_subdivisions=3, subdivision_axis="y",
        ),
    ),
    structural_rules={"unit_0_storeys": [0]},
)

SINGLE_BUNGALOW = Typology(
    id="single-bungalow",
    label="Single-Family Bungalow (1-Storey)",
    units_produced=1,
    stacking_axis="horizontal",
    min_frontage_m=4.5, max_frontage_m=12.0,
    min_depth_m=8.0,    max_depth_m=17.0,
    target_storeys=1, requires_basement=False,
    target_gfa_per_unit_m2=(60.0, 140.0),
    stamp_cells=_SINGLE_BUNGALOW,
    corridor_axis="central", stair_position="internal",
    eligible_zones=_R_ZONES, eligible_wards=None,
    notes="Single-storey bungalow. All rooms on ground floor.",
    template=_SINGLE_BUNGALOW_TEMPLATE,
)


# ── Public catalogue ──────────────────────────────────────────────────────────
TYPOLOGY_LIBRARY: list[Typology] = [
    SINGLE_DETACHED_2STOREY,  # 1 unit — must come first so selector picks it for single-unit briefs
    SINGLE_BUNGALOW,          # 1 unit, 1 storey
    DUPLEX_STACK_CLASSIC,
    DUPLEX_SIDE_BY_SIDE,
    TRIPLEX_STACK,
    TRIPLEX_FRONT_BACK,
    FOURPLEX_2UP2DOWN,
    FOURPLEX_4STACK,
    FOURPLEX_NARROW,
    FOURPLEX_SBS_BSMT,
    FOURPLEX_SBS_CORRIDOR,
    FOURPLEX_LANEWAY,
    FIVEPLEX_NARROW,
    SIXPLEX_WIDE,
]
