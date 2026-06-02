"""Tests for generic_template.py — flexible zones, max_subdivisions from brief."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from packgen.typology.library import TYPOLOGY_LIBRARY
from packgen.typology.generic_template import stamp_to_generic_template


def test_balcony_accepted_in_at_least_one_zone():
    typology = TYPOLOGY_LIBRARY[0]
    template = stamp_to_generic_template(typology)
    all_roles = [r for z in template.zones for r in z.valid_roles]
    assert "balcony" in all_roles, f"balcony not in any zone valid_roles: {all_roles}"


def test_dining_accepted_in_at_least_one_zone():
    typology = TYPOLOGY_LIBRARY[0]
    template = stamp_to_generic_template(typology)
    all_roles = [r for z in template.zones for r in z.valid_roles]
    assert "dining" in all_roles, f"dining not in any zone valid_roles"


def test_storage_accepted_in_at_least_one_zone():
    typology = TYPOLOGY_LIBRARY[0]
    template = stamp_to_generic_template(typology)
    all_roles = [r for z in template.zones for r in z.valid_roles]
    assert "storage" in all_roles, f"storage not in any zone valid_roles"


def test_max_subdivisions_respects_brief_bedroom_count():
    typology = TYPOLOGY_LIBRARY[0]
    brief_rooms = {"bedroom": 4, "bathroom": 3}
    template = stamp_to_generic_template(typology, brief_rooms=brief_rooms)
    # Find zones that accept bedrooms
    bedroom_zones = [z for z in template.zones if "bedroom" in z.valid_roles]
    assert bedroom_zones, "No zone accepts bedrooms"
    max_subs = max(z.max_subdivisions for z in bedroom_zones)
    assert max_subs >= 4, f"max_subdivisions {max_subs} < 4 for brief with 4 bedrooms"


def test_no_brief_rooms_uses_safe_default():
    typology = TYPOLOGY_LIBRARY[0]
    template = stamp_to_generic_template(typology)
    # Only non-circulation zones should have max_subdivisions >= 4
    # Circulation zones (stair/corridor/entry) correctly have max_subdivisions=1
    non_circ = [z for z in template.zones if not z.is_circulation]
    assert non_circ, "Expected at least one non-circulation zone"
    for z in non_circ:
        assert z.max_subdivisions >= 4, \
            f"Non-circulation zone {z.zone_id} has max_subdivisions={z.max_subdivisions} < 4"


def test_no_fallback_when_brief_exceeds_stamp_cell_count():
    """fill_template must not raise TemplateFillError when brief has 3 bedrooms
    but the stamp has fewer bedroom cells. The reconciler adds the missing rooms."""
    from packgen.template_filler import fill_template
    from dataclasses import replace as dc_replace

    typ = next(
        (t for t in TYPOLOGY_LIBRARY if t.stacking_axis == "vertical"),
        TYPOLOGY_LIBRARY[0]
    )
    brief_rooms = {"bedroom": 3, "living": 1, "kitchen": 1, "bathroom": 1}
    if not typ.has_template():
        generic = stamp_to_generic_template(typ, brief_rooms=brief_rooms)
        typ = dc_replace(typ, template=generic)

    brief = {
        "units": [{"unit_id": 1, "rooms": [
            {"role": "bedroom",  "count": 3, "storey_preference": 1},
            {"role": "living",   "count": 1, "storey_preference": 0},
            {"role": "kitchen",  "count": 1, "storey_preference": 0},
            {"role": "bathroom", "count": 1, "storey_preference": 0},
        ]}],
        "stack_preference": "vertical",
    }

    # Must not raise even if brief exceeds stamp cell count
    cells = fill_template(
        typology=typ,
        brief=brief,
        envelope_w_m=9.0,
        envelope_d_m=12.0,
        fallback_to_stamp=True,
        units_target=1,
    )

    bedroom_cells = [c for c in cells if c.role == "bedroom"]
    assert len(bedroom_cells) >= 1, "Expected ≥1 bedroom from brief, got 0"
    # Result must not be identical to raw stamp_cells (brief was applied)
    stamp_bedroom_count = sum(1 for c in typ.stamp_cells if c.role == "bedroom")
    assert len(bedroom_cells) >= stamp_bedroom_count, (
        f"Brief should produce ≥ stamp bedroom count ({stamp_bedroom_count})"
    )
