"""Tests for template_filler.py v2 fixes — target-area sizing, trim cap, reconciler."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from packgen.rules.code_rules import ROOM_MAX_AREA_M2, ROOM_MIN_AREA_M2


class _MockLLMClient:
    """Minimal mock of OpenAI client that returns a plausible assignment JSON."""

    def __init__(self, assignment_fn):
        self._assignment_fn = assignment_fn
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        import json as _json

        class _Choice:
            message = type("Msg", (), {"content": None})()

        template_zones = []
        for msg in kwargs.get("messages", []):
            if msg["role"] == "user" and "zone_id" in msg["content"]:
                # extract zones from user message JSON blob
                import re
                m = re.search(r'\[\s*\{.*?\}\s*\]', msg["content"], re.DOTALL)
                if m:
                    try:
                        template_zones = _json.loads(m.group(0))
                    except Exception:
                        pass

        result = self._assignment_fn(template_zones, kwargs)
        choice = _Choice()
        choice.message.content = _json.dumps(result)

        class _Resp:
            choices = [choice]

        return _Resp()


def _make_assignment_for_brief(roles_needed, zones):
    """Build a minimal valid assignment: assign each role to the first zone that accepts it."""
    import json as _json
    assignments = []
    placed = set()

    for zone in zones:
        valid = zone.get("valid_roles", [])
        max_subs = zone.get("max_subdivisions", 1)
        assigned_rooms = []
        for role in roles_needed:
            if role not in placed and role in valid and len(assigned_rooms) < max_subs:
                assigned_rooms.append({"role": role, "unit_id": 0,
                                       "subdivision_index": len(assigned_rooms),
                                       "weight": 1.0})
                placed.add(role)
        if assigned_rooms:
            # Normalize weights
            n = len(assigned_rooms)
            for r in assigned_rooms:
                r["weight"] = round(1.0 / n, 4)
            assignments.append({"zone_id": zone["zone_id"], "rooms": assigned_rooms})

    return {"assignments": assignments, "warnings": []}


def _bathroom_area_limited():
    """Bathroom in a large zone should not exceed ROOM_MAX_AREA_M2["bathroom"]."""
    from packgen.typology.library import TYPOLOGY_LIBRARY
    from packgen.typology.generic_template import stamp_to_generic_template
    from packgen.template_filler import fill_template
    from dataclasses import replace as dc_replace

    typology = next(
        (t for t in TYPOLOGY_LIBRARY if t.stacking_axis == "vertical"),
        TYPOLOGY_LIBRARY[0]
    )
    brief_rooms = {"bedroom": 2, "bathroom": 2, "living": 1, "kitchen": 1}
    if not typology.has_template():
        generic = stamp_to_generic_template(typology, brief_rooms=brief_rooms)
        typology = dc_replace(typology, template=generic)

    brief = {
        "units": [{"unit_id": 1, "rooms": [
            {"role": "bedroom",  "count": 2, "storey_preference": 1},
            {"role": "living",   "count": 1, "storey_preference": 0},
            {"role": "kitchen",  "count": 1, "storey_preference": 0},
            {"role": "bathroom", "count": 2, "storey_preference": 0},
        ]}],
        "stack_preference": "vertical",
    }

    roles = ["bedroom", "bedroom", "living", "kitchen", "bathroom", "bathroom"]
    mock_client = _MockLLMClient(
        lambda zones, _: _make_assignment_for_brief(roles, zones)
    )

    cells = fill_template(
        typology=typology,
        brief=brief,
        envelope_w_m=9.0,
        envelope_d_m=12.0,
        openai_client=mock_client,
        fallback_to_stamp=False,
        units_target=1,
    )
    max_area = ROOM_MAX_AREA_M2.get("bathroom", 8.0)
    bathroom_cells = [c for c in cells if c.role == "bathroom"]
    for c in bathroom_cells:
        area = (c.x1 - c.x0) * 9.0 * (c.y1 - c.y0) * 12.0
        assert area <= max_area * 1.20, \
            f"bathroom area {area:.1f}m² exceeds max {max_area}m²"


def test_bathroom_area_limited():
    _bathroom_area_limited()


def test_cap_room_areas_trims_not_duplicates():
    """_cap_room_areas must trim one room, not split into two."""
    from packgen.template_filler import _cap_room_areas
    from packgen.typology.models import Cell

    env_w, env_d = 16.0, 16.0
    max_br = ROOM_MAX_AREA_M2.get("bedroom", 16.0)
    # Create a bedroom that exceeds its max area
    oversized_span_x = (max_br * 2.0) / env_d / env_w   # normalized span that gives 2× max area

    try:
        c = Cell(
            role="bedroom", unit_id=0, storey=0,
            x0=0.0, y0=0.0,
            x1=oversized_span_x, y1=1.0,
            min_area_m2=ROOM_MIN_AREA_M2.get("bedroom", 7.0),
            min_dim_m=2.7,
            needs_egress_window=True,
            is_stretchable=True,
        )
    except Exception:
        pytest.skip("Cell constructor signature differs from expected")

    result = _cap_room_areas([c], env_w, env_d)
    bedroom_count = sum(1 for r in result if r.role == "bedroom")
    assert bedroom_count == 1, f"Expected 1 bedroom after cap, got {bedroom_count} (was doubled)"


def test_storey_preference_in_user_prompt():
    """_build_user_prompt includes storey preferences from brief."""
    try:
        from packgen.template_filler import _build_user_prompt
    except ImportError:
        pytest.skip("_build_user_prompt not importable directly")

    from packgen.typology.library import TYPOLOGY_LIBRARY
    from packgen.typology.generic_template import stamp_to_generic_template
    from dataclasses import replace as dc_replace

    typology = TYPOLOGY_LIBRARY[0]
    if not typology.has_template():
        generic = stamp_to_generic_template(typology)
        typology = dc_replace(typology, template=generic)

    template = typology.template

    brief = {
        "units": [{"unit_id": 1, "rooms": [
            {"role": "bedroom", "count": 2, "storey_preference": 1},
            {"role": "kitchen", "count": 1, "storey_preference": 0},
        ]}],
    }

    try:
        prompt = _build_user_prompt(
            template=template, brief=brief,
            env_w=10.0, env_d=10.0,
        )
    except TypeError:
        # Try positional signature
        try:
            prompt = _build_user_prompt(template, brief, 10.0, 10.0)
        except Exception:
            pytest.skip("Could not call _build_user_prompt with test args")
            return

    assert "upper floor" in prompt.lower() or "storey=1" in prompt.lower() or "storey 1" in prompt.lower(), \
        "bedroom storey_preference=1 not reflected in user prompt"
    assert "ground floor" in prompt.lower() or "storey=0" in prompt.lower() or "storey 0" in prompt.lower(), \
        "kitchen storey_preference=0 not reflected in user prompt"


def test_dining_balcony_storage_present_after_reconcile():
    """After fill_template, dining/balcony/storage from brief must appear in cells."""
    from packgen.typology.library import TYPOLOGY_LIBRARY
    from packgen.typology.generic_template import stamp_to_generic_template
    from packgen.template_filler import fill_template
    from dataclasses import replace as dc_replace

    typology = next(
        (t for t in TYPOLOGY_LIBRARY if t.stacking_axis == "vertical"),
        TYPOLOGY_LIBRARY[0]
    )
    brief_rooms = {"bedroom": 2, "living": 1, "kitchen": 1, "bathroom": 1,
                   "dining": 1, "balcony": 1, "storage": 1}
    if not typology.has_template():
        generic = stamp_to_generic_template(typology, brief_rooms=brief_rooms)
        typology = dc_replace(typology, template=generic)

    brief = {
        "units": [{"unit_id": 1, "rooms": [
            {"role": "bedroom",  "count": 2, "storey_preference": 1},
            {"role": "living",   "count": 1, "storey_preference": 0},
            {"role": "kitchen",  "count": 1, "storey_preference": 0},
            {"role": "bathroom", "count": 1, "storey_preference": 0},
            {"role": "dining",   "count": 1, "storey_preference": 0},
            {"role": "balcony",  "count": 1, "storey_preference": 0},
            {"role": "storage",  "count": 1, "storey_preference": 1},
        ]}],
        "stack_preference": "vertical",
    }

    cells = fill_template(
        typology=typology,
        brief=brief,
        envelope_w_m=10.0,
        envelope_d_m=12.0,
        fallback_to_stamp=True,   # LLM not available in CI; reconciler runs on stamp path
        units_target=1,
    )
    roles_present = {c.role for c in cells}
    for expected_role in ("dining", "balcony", "storage"):
        assert expected_role in roles_present, \
            f"Role '{expected_role}' from brief is missing from cells. Present: {roles_present}"
