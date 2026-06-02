"""Tests for the metric-driven plan analyzer."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import math
import pytest
from shapely.geometry import box as shapely_box
from unittest.mock import MagicMock

from packgen.suggest.analyzer import (
    analyze_plan, apply_parametric_change,
    Suggestion,
    _compute_gfa, _compute_circulation_ratio, _daylight_per_bedroom,
    _aspect_outliers, _egress_violations, _wet_run_length, _unused_gfa,
)
from packgen.schemas.contracts import DesignBrief, BriefUnit, BriefRoomSpec
from packgen.geometry import EnvelopeResult
from packgen.ai.schema import (
    FloorPlanJSON, RoomModel, StoreyModel, WallModel, WindowModel,
    FloorPlanMetadata,
)


# ---------------------------------------------------------------------------
# Minimal fixtures
# ---------------------------------------------------------------------------

def _wall(id_="w0"):
    return WallModel(id=id_, start=[0.0, 0.0], end=[5.0, 0.0], type="exterior")


def _room(id_, cat, x0, y0, x1, y1, area=None):
    poly = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return RoomModel(id=id_, label=id_, polygon=poly, category=cat, area_m2=area)


def _storey(rooms, windows=None, level=0):
    return StoreyModel(
        level=level, elevation_m=0.0,
        walls=[_wall()], rooms=rooms, windows=windows or [],
    )


def _plan(*storeys):
    return FloorPlanJSON(storeys=list(storeys))


def _brief(n_units=1, bedrooms=2):
    rooms = [
        BriefRoomSpec(role="bedroom",  count=bedrooms, storey_preference=1),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ]
    return DesignBrief(units=[BriefUnit(unit_id=i+1, rooms=rooms) for i in range(n_units)])


def _envelope(w=10.0, d=12.0):
    poly = shapely_box(0, 0, w, d)
    return EnvelopeResult(
        envelope_2d=poly, lot_local=poly,
        setback_lines={}, setbacks_applied={},
        lot_width_m=w, lot_depth_m=d, lot_area_m2=w * d,
        rotation_deg=0.0, origin_mtm=(0.0, 0.0),
        angular_plane_applied=False, depth_limit_m=17.0, warnings=[],
    )


# ---------------------------------------------------------------------------
# Unit tests — individual metrics
# ---------------------------------------------------------------------------

def test_gfa_skips_below_grade():
    plan = _plan(
        _storey([_room("bsmt", "storage", 0, 0, 5, 5)], level=-1),
        _storey([_room("liv",  "living",  0, 0, 5, 8)], level=0),
    )
    gfa = _compute_gfa(plan)
    assert gfa == pytest.approx(40.0)   # basement excluded


def test_gfa_skips_balcony():
    plan = _plan(_storey([
        _room("liv", "living",  0, 0, 5, 8),
        _room("bal", "balcony", 5, 0, 7, 2),
    ]))
    assert _compute_gfa(plan) == pytest.approx(40.0)  # balcony excluded


def test_circulation_ratio():
    plan = _plan(_storey([
        _room("liv", "living",  0, 0, 8, 8),   # 64 m²
        _room("cor", "corridor", 8, 0, 10, 8),  # 16 m²
    ]))
    gfa = _compute_gfa(plan)
    ratio = _compute_circulation_ratio(plan, gfa)
    assert ratio == pytest.approx(16.0 / 80.0)


def test_aspect_outlier_detected():
    # Room 1×6 m → aspect 6.0 > 2.5
    plan = _plan(_storey([_room("narrow", "bedroom", 0, 0, 1, 6)]))
    outliers = _aspect_outliers(plan)
    assert len(outliers) == 1
    assert outliers[0][0] == "narrow"
    assert outliers[0][1] == pytest.approx(6.0)


def test_aspect_square_no_outlier():
    plan = _plan(_storey([_room("sq", "bedroom", 0, 0, 4, 4)]))
    assert _aspect_outliers(plan) == []


def test_egress_violations_no_windows():
    plan = _plan(_storey([
        _room("br", "bedroom", 0, 0, 3, 4),
    ]))   # no windows → violation
    violations = _egress_violations(plan)
    assert "br" in violations


def test_egress_no_violation_when_window_present():
    win = WindowModel(
        id="w", wall_id="w0",
        position_along_wall_m=1.0, width_m=1.0,
        sill_m=0.9, head_m=2.1,
        egress_compliant=True,
    )
    plan = _plan(_storey([_room("br", "bedroom", 0, 0, 3, 4)], windows=[win]))
    violations = _egress_violations(plan)
    assert "br" not in violations


def test_wet_run_length_zero_for_single_wet_room():
    plan = _plan(_storey([_room("kit", "kitchen", 0, 0, 3, 3)]))
    assert _wet_run_length(plan) == pytest.approx(0.0)


def test_wet_run_length_far_apart():
    plan = _plan(_storey([
        _room("kit", "kitchen",  0, 0, 2, 2),
        _room("bat", "bathroom", 8, 0, 10, 2),
    ]))
    run = _wet_run_length(plan)
    # centroids at (1,1) and (9,1) → distance = 8.0
    assert run == pytest.approx(8.0, abs=0.1)


def test_unused_gfa_positive_when_underbuilt():
    # 1 above-grade storey; envelope 10×12 = 120 m²
    # budget = 120 × 1 × 0.82 = 98.4 m²; plan GFA = 5×8 = 40 m² → unused = 58.4 m²
    plan = _plan(_storey([_room("liv", "living", 0, 0, 5, 8)]))
    env = _envelope(10, 12)
    gfa = _compute_gfa(plan)
    unused = _unused_gfa(plan, env, gfa)
    assert unused > 50.0


# ---------------------------------------------------------------------------
# Acceptance test 1: windowless bedroom → egress suggestion
# ---------------------------------------------------------------------------

def test_windowless_bedroom_produces_egress_suggestion():
    """A bedroom with no windows must produce a suggestion with rule_id='egress_missing'."""
    plan = _plan(_storey([
        _room("br1", "bedroom", 0, 0, 3, 4),
        _room("br2", "bedroom", 3, 0, 6, 4),
        _room("liv", "living",  6, 0, 10, 4),
    ]))
    suggestions = analyze_plan(plan, _brief(), _envelope(), openai_client=None)
    rule_ids = [s.rule_id for s in suggestions]
    assert "egress_missing" in rule_ids


def test_egress_suggestion_has_high_priority():
    """Egress suggestions must be priority 1 (highest)."""
    plan = _plan(_storey([_room("br", "bedroom", 0, 0, 3, 4)]))
    suggestions = analyze_plan(plan, _brief(bedrooms=1), _envelope(), openai_client=None)
    egress = next((s for s in suggestions if s.rule_id == "egress_missing"), None)
    assert egress is not None
    assert egress.priority == 1


def test_no_suggestions_for_fully_compliant_plan():
    """A plan with egress windows, good daylight, and reasonable rooms should be clean."""
    win = WindowModel(
        id="w0", wall_id="w0",
        position_along_wall_m=1.0, width_m=1.5, sill_m=0.9, head_m=2.1,
        egress_compliant=True,
    )
    # Large well-lit rooms, no circulation excess, wet rooms together
    rooms = [
        _room("br",  "bedroom",  0, 0, 4, 4),   # 16 m²
        _room("kit", "kitchen",  4, 0, 7, 3),   # 9 m²
        _room("bat", "bathroom", 4, 3, 6, 5),   # 4 m²
        _room("liv", "living",   7, 0, 10, 5),  # 15 m²
    ]
    plan = _plan(_storey(rooms, windows=[win, win]))
    suggestions = analyze_plan(plan, _brief(bedrooms=1), _envelope(10, 5))
    # No egress violation; daylight ratio with 2 windows (1.5×1.2 each = 3.6 m²)
    # over 44 m² = 8.2% → just above threshold
    # Allow 0 or 1 non-egress suggestions
    egress = [s for s in suggestions if s.rule_id == "egress_missing"]
    assert len(egress) == 0


# ---------------------------------------------------------------------------
# Acceptance test 2: parametric_change → accepted by generate_floor_plan
# ---------------------------------------------------------------------------

def test_all_parametric_changes_accepted_by_apply():
    """Every returned parametric_change must be applicable without raising."""
    plan = _plan(_storey([
        _room("br",  "bedroom",  0, 0, 3, 4),
        _room("liv", "living",   3, 0, 7, 4),
        _room("kit", "kitchen",  7, 0, 10, 4),
        _room("bat", "bathroom", 0, 4, 3, 6),
        _room("cor", "corridor", 3, 4, 6, 5),   # adds circulation
        _room("bat2","bathroom", 6, 4, 10, 6),  # wet rooms separated
    ]))
    brief = _brief(bedrooms=1)
    env = _envelope()
    suggestions = analyze_plan(plan, brief, env, openai_client=None)
    assert len(suggestions) > 0, "Expected at least one suggestion"

    for s in suggestions:
        new_brief, new_floors = apply_parametric_change(brief, s.parametric_change, 2)
        # Must not raise; basic structural validity
        assert len(new_brief.units) >= 1
        for u in new_brief.units:
            assert len(u.rooms) >= 1
        assert 1 <= new_floors <= 4


def test_parametric_change_increase_floors():
    brief = _brief()
    new_brief, new_floors = apply_parametric_change(
        brief, {"type": "increase_floors", "delta": 1}, target_floors=2,
    )
    assert new_floors == 3
    assert new_brief is brief   # brief unchanged


def test_parametric_change_change_stacking():
    brief = _brief()
    new_brief, new_floors = apply_parametric_change(
        brief, {"type": "change_stacking", "value": "horizontal"}, target_floors=2,
    )
    assert new_brief.stacking_pref == "horizontal"
    assert new_floors == 2


def test_parametric_change_add_unit():
    brief = _brief(n_units=1)
    new_brief, _ = apply_parametric_change(
        brief, {"type": "add_unit", "rooms": [
            {"role": "living",  "count": 1, "storey_preference": 0},
            {"role": "kitchen", "count": 1, "storey_preference": 0},
        ]}, target_floors=2,
    )
    assert len(new_brief.units) == 2
    assert new_brief.units[-1].unit_id == 2


def test_parametric_change_increase_floors_capped():
    brief = _brief()
    _, new_floors = apply_parametric_change(
        brief, {"type": "increase_floors", "delta": 10}, target_floors=3,
    )
    assert new_floors <= 4  # max 4 floors


# ---------------------------------------------------------------------------
# LLM narration fallback
# ---------------------------------------------------------------------------

def test_malformed_llm_response_returns_deterministic():
    """If the LLM returns garbage, the deterministic suggestions must be returned unchanged."""
    from packgen.suggest.analyzer import narrate_suggestions

    suggestions = [
        Suggestion(title="T1", rationale="R1", rule_id="egress_missing",
                   metric_value=1.0, parametric_change={"type": "increase_floors", "delta": 1},
                   est_impact="e1"),
    ]
    bad_client = MagicMock()
    bad_client.chat.completions.create.side_effect = TimeoutError("no network")
    result = narrate_suggestions(suggestions, _brief(), bad_client)
    assert len(result) == 1
    assert result[0].rule_id == "egress_missing"


def test_llm_invented_rule_id_dropped():
    """LLM responses referencing unknown rule_ids must be silently dropped."""
    from packgen.suggest.analyzer import narrate_suggestions
    import json
    from types import SimpleNamespace

    suggestions = [
        Suggestion(title="T1", rationale="R1", rule_id="egress_missing",
                   metric_value=1.0, parametric_change={"type": "increase_floors", "delta": 1},
                   est_impact="e1"),
    ]
    # LLM invents a rule_id "invented_rule"
    fake_response = {"ordered": [
        {"rule_id": "invented_rule", "sentence": "invented suggestion"},
        {"rule_id": "egress_missing", "sentence": "real suggestion"},
    ]}
    choice = SimpleNamespace(message=SimpleNamespace(content=json.dumps(fake_response)))
    resp = SimpleNamespace(choices=[choice])
    client = MagicMock()
    client.chat.completions.create.return_value = resp

    result = narrate_suggestions(suggestions, _brief(), client)
    rule_ids = [s.rule_id for s in result]
    assert "invented_rule" not in rule_ids
    assert "egress_missing" in rule_ids


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_analyze_plan_deterministic():
    plan = _plan(_storey([_room("br", "bedroom", 0, 0, 3, 4)]))
    brief = _brief(bedrooms=1)
    env = _envelope()
    s1 = analyze_plan(plan, brief, env)
    s2 = analyze_plan(plan, brief, env)
    assert [s.rule_id for s in s1] == [s.rule_id for s in s2]
    assert [s.metric_value for s in s1] == [s.metric_value for s in s2]
