"""Tests for build_adjacency — deterministic path and LLM fallback."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from packgen.adjacency.graph_builder import (
    build_adjacency,
    DEFAULT_WEIGHTS,
    _pair_key,
    _bearing_to_cardinal,
)
from packgen.schemas.contracts import (
    DesignBrief, BriefUnit, BriefRoomSpec, SpaceProgram, ProgramRoom,
)
from packgen.program.space_program import generate_space_program
from packgen.geometry import EnvelopeResult
from shapely.geometry import box as shapely_box


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(w: float = 10.0, d: float = 12.0) -> EnvelopeResult:
    poly = shapely_box(0.0, 0.0, w, d)
    return EnvelopeResult(
        envelope_2d=poly, lot_local=poly,
        setback_lines={}, setbacks_applied={},
        lot_width_m=w, lot_depth_m=d, lot_area_m2=w * d,
        rotation_deg=0.0, origin_mtm=(0.0, 0.0),
        angular_plane_applied=False, depth_limit_m=17.0, warnings=[],
    )


def _small_program() -> SpaceProgram:
    """Single-unit 2-bed/liv/kit/bath program for determinism tests."""
    brief = DesignBrief(units=[BriefUnit(unit_id=1, rooms=[
        BriefRoomSpec(role="bedroom",  count=2, storey_preference=1),
        BriefRoomSpec(role="living",   count=1, storey_preference=0),
        BriefRoomSpec(role="kitchen",  count=1, storey_preference=0),
        BriefRoomSpec(role="bathroom", count=1, storey_preference=0),
    ])])
    program, _ = generate_space_program(brief, _make_envelope(), target_floors=2)
    return program


def _fake_client(response_json: dict):
    """Return a mock OpenAI client whose chat.completions.create returns response_json."""
    choice = SimpleNamespace(message=SimpleNamespace(content=json.dumps(response_json)))
    resp   = SimpleNamespace(choices=[choice])
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


# ---------------------------------------------------------------------------
# Acceptance test 1: deterministic path (allow_llm=False)
# ---------------------------------------------------------------------------

def test_allow_llm_false_no_network_call():
    """allow_llm=False must return a matrix without touching any network."""
    program = _small_program()
    matrix = build_adjacency(program, road_bearing_deg=None, allow_llm=False)
    # Must produce at least some edges
    assert len(matrix.edges) > 0


def test_allow_llm_false_edges_match_default_weights():
    """Every edge weight must equal the DEFAULT_WEIGHTS entry for that role pair."""
    program = _small_program()
    matrix = build_adjacency(program, road_bearing_deg=None, allow_llm=False)

    id_to_role = {r.id: r.role for r in program.rooms}
    for e in matrix.edges:
        key = _pair_key(id_to_role[e.a], id_to_role[e.b])
        expected = DEFAULT_WEIGHTS.get(key, 0.0)
        assert abs(e.weight - expected) < 1e-6, (
            f"Edge {e.a}↔{e.b} (roles {key}): weight={e.weight}, expected={expected}"
        )


def test_allow_llm_false_matrix_symmetric():
    """AdjacencyMatrix.weight(a,b) == weight(b,a) for all edges."""
    program = _small_program()
    matrix = build_adjacency(program, road_bearing_deg=None, allow_llm=False)
    for e in matrix.edges:
        assert matrix.weight(e.a, e.b) == matrix.weight(e.b, e.a)


def test_allow_llm_false_no_cross_unit_edges():
    """Edges between different non-shared units must not appear."""
    brief = DesignBrief(units=[
        BriefUnit(unit_id=1, rooms=[BriefRoomSpec(role="living", count=1)]),
        BriefUnit(unit_id=2, rooms=[BriefRoomSpec(role="living", count=1)]),
    ])
    program, _ = generate_space_program(brief, _make_envelope(), target_floors=2)
    matrix = build_adjacency(program, road_bearing_deg=None, allow_llm=False)

    id_to_uid = {r.id: r.unit_id for r in program.rooms}
    for e in matrix.edges:
        ua, ub = id_to_uid[e.a], id_to_uid[e.b]
        assert ua == ub or ua == -1 or ub == -1, (
            f"Cross-unit edge {e.a}(u{ua})↔{e.b}(u{ub}) should not exist"
        )


# ---------------------------------------------------------------------------
# Acceptance test 2: malformed LLM response → silent fallback
# ---------------------------------------------------------------------------

def test_malformed_json_returns_deterministic():
    """LLM returning garbage JSON must silently fall back to default matrix."""
    program   = _small_program()
    det_matrix = build_adjacency(program, None, allow_llm=False)

    bad_client = MagicMock()
    bad_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="NOT JSON {{{{"))]
    )
    llm_matrix = build_adjacency(program, None, openai_client=bad_client, allow_llm=True)

    assert len(llm_matrix.edges) == len(det_matrix.edges)
    det_pairs  = {frozenset({e.a, e.b}): e.weight for e in det_matrix.edges}
    llm_pairs  = {frozenset({e.a, e.b}): e.weight for e in llm_matrix.edges}
    assert det_pairs == llm_pairs


def test_llm_exception_returns_deterministic():
    """Network exception from the LLM client must silently fall back."""
    program    = _small_program()
    det_matrix = build_adjacency(program, None, allow_llm=False)

    broken_client = MagicMock()
    broken_client.chat.completions.create.side_effect = TimeoutError("no network")
    llm_matrix = build_adjacency(program, None, openai_client=broken_client, allow_llm=True)

    det_pairs = {frozenset({e.a, e.b}): e.weight for e in det_matrix.edges}
    llm_pairs = {frozenset({e.a, e.b}): e.weight for e in llm_matrix.edges}
    assert det_pairs == llm_pairs


def test_llm_returns_none_content():
    """LLM returning None content must silently fall back."""
    program = _small_program()
    det_matrix = build_adjacency(program, None, allow_llm=False)

    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
    )
    llm_matrix = build_adjacency(program, None, openai_client=client, allow_llm=True)
    assert len(llm_matrix.edges) == len(det_matrix.edges)


# ---------------------------------------------------------------------------
# LLM delta application
# ---------------------------------------------------------------------------

def test_valid_delta_applied():
    """A valid LLM delta must shift the edge weight by exactly the clamped delta."""
    program    = _small_program()
    det_matrix = build_adjacency(program, None, allow_llm=False)

    # Pick a kitchen↔living edge that definitely exists (weight 0.6 from defaults)
    kitchen_id = next(r.id for r in program.rooms if r.role == "kitchen")
    living_id  = next(r.id for r in program.rooms if r.role == "living")
    base_w = det_matrix.weight(kitchen_id, living_id)
    assert base_w == pytest.approx(0.6)

    delta = 0.3
    client = _fake_client({"adjustments": [{"a": kitchen_id, "b": living_id, "delta": delta}]})
    llm_matrix = build_adjacency(program, None, openai_client=client, allow_llm=True)
    assert llm_matrix.weight(kitchen_id, living_id) == pytest.approx(base_w + delta, abs=0.001)


def test_delta_clamped_to_max():
    """A delta exceeding ±0.4 must be clamped before application."""
    program = _small_program()
    kitchen_id = next(r.id for r in program.rooms if r.role == "kitchen")
    living_id  = next(r.id for r in program.rooms if r.role == "living")

    # delta=0.9 → clamped to 0.4
    client = _fake_client({"adjustments": [{"a": kitchen_id, "b": living_id, "delta": 0.9}]})
    llm_matrix = build_adjacency(program, None, openai_client=client, allow_llm=True)
    expected = min(1.0, 0.6 + 0.4)     # base 0.6 + clamped delta 0.4
    assert llm_matrix.weight(kitchen_id, living_id) == pytest.approx(expected, abs=0.001)


def test_invalid_room_id_ignored():
    """An adjustment referencing an unknown room ID must be silently dropped."""
    program    = _small_program()
    det_matrix = build_adjacency(program, None, allow_llm=False)

    client = _fake_client({
        "adjustments": [{"a": "nonexistent_room_xyz", "b": "also_fake", "delta": 0.4}]
    })
    llm_matrix = build_adjacency(program, None, openai_client=client, allow_llm=True)
    det_pairs  = {frozenset({e.a, e.b}): e.weight for e in det_matrix.edges}
    llm_pairs  = {frozenset({e.a, e.b}): e.weight for e in llm_matrix.edges}
    assert det_pairs == llm_pairs


def test_self_edge_in_llm_response_ignored():
    """An adjustment where a == b must be silently ignored."""
    program = _small_program()
    kitchen_id = next(r.id for r in program.rooms if r.role == "kitchen")
    det_matrix = build_adjacency(program, None, allow_llm=False)

    client = _fake_client({
        "adjustments": [{"a": kitchen_id, "b": kitchen_id, "delta": 0.4}]
    })
    llm_matrix = build_adjacency(program, None, openai_client=client, allow_llm=True)
    assert len(llm_matrix.edges) == len(det_matrix.edges)


# ---------------------------------------------------------------------------
# Road bearing helper
# ---------------------------------------------------------------------------

def test_bearing_to_cardinal():
    assert _bearing_to_cardinal(0.0)   == "north"
    assert _bearing_to_cardinal(90.0)  == "east"
    assert _bearing_to_cardinal(180.0) == "south"
    assert _bearing_to_cardinal(270.0) == "west"
    assert _bearing_to_cardinal(360.0) == "north"
    assert _bearing_to_cardinal(45.0)  == "north-east"
