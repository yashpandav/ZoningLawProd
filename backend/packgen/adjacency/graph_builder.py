"""Adjacency graph builder for the deterministic planning pipeline.

Turns a SpaceProgram into a weighted AdjacencyMatrix.

Execution path:
  1. Build the full matrix deterministically from DEFAULT_WEIGHTS (role-pair table).
  2. If allow_llm=True, call GPT-4.1 (same pattern as template_filler) asking ONLY
     for weight deltas and orientation hints — never coordinates.
  3. On any LLM error / timeout / invalid output → return step-1 matrix unchanged.

The LLM is therefore strictly additive: it can nudge weights within ±0.4 and add
orientation preference strings.  It cannot invent room geometry or override limits.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from ..schemas.contracts import AdjacencyEdge, AdjacencyMatrix, ProgramRoom, SpaceProgram

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_LLM_MODEL   = "gpt-4.1"
_LLM_TIMEOUT = 12.0          # seconds — same budget as template_filler
_LLM_MAX_TOKENS = 800
_LLM_MAX_DELTA  = 0.4        # maximum per-edge adjustment the LLM may apply
_NEW_EDGE_MIN_DELTA = 0.1    # LLM-introduced edges must have |delta| >= this

# ---------------------------------------------------------------------------
# DEFAULT_WEIGHTS: canonical role-pair table
#
# Keys are sorted (role_a, role_b) tuples (role_a <= role_b alphabetically).
# Values are base adjacency weights in [-1, 1]:
#   +1.0  must be adjacent (always share a wall)
#   +0.6  strongly prefer adjacency
#   +0.3  mild preference
#   -0.2  mild separation preference
#   -0.5  strongly prefer separation
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[tuple[str, str], float] = {
    # Kitchen / dining / living cluster
    ("dining",   "kitchen"):          1.0,   # always open plan
    ("dining",   "living"):           0.9,   # typically combined
    ("kitchen",  "living"):           0.6,   # often connected
    ("dining",   "entry"):            0.4,   # formal dining near entry
    ("entry",    "kitchen"):          0.3,   # service access
    ("entry",    "living"):           0.8,   # arrival opens to living
    # Bedroom / bathroom cluster
    ("bathroom", "bedroom"):          0.7,   # bath adjacent to bedroom
    ("bathroom", "master_bedroom"):   0.9,   # ensuite preferred
    ("bedroom",  "corridor"):         0.8,   # bedrooms off a corridor
    ("corridor", "master_bedroom"):   0.8,
    # Active vs quiet separation
    ("bedroom",  "living"):          -0.2,   # sleeping away from active zones
    ("living",   "master_bedroom"):  -0.3,   # stronger separation for master
    # Wet-wall stacking
    ("bathroom", "kitchen"):          0.5,   # shared riser preferred
    ("bathroom", "laundry"):          0.5,   # wet stack
    ("kitchen",  "laundry"):          0.6,   # wet stack
    ("laundry",  "mechanical"):       0.5,   # utility core
    # Circulation core
    ("corridor", "stair"):            0.9,   # stair feeds corridor
    ("entry",    "stair"):            0.7,   # vertical access near entry
    ("bathroom", "corridor"):         0.6,   # baths accessible from corridor
    ("corridor", "mechanical"):       0.5,   # mech off circulation
    ("corridor", "storage"):          0.4,   # storage off circulation
    # Outdoor / balcony
    ("balcony",  "living"):           0.8,   # balcony off living
    ("balcony",  "bedroom"):          0.6,   # bedroom terrace
    ("balcony",  "master_bedroom"):   0.7,   # master terrace preferred
    # Guest WC
    ("entry",    "powder_room"):      0.6,   # guest WC near entry
    ("living",   "powder_room"):      0.5,   # guest access from living
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_adjacency(
    program: SpaceProgram,
    road_bearing_deg: Optional[float],
    openai_client=None,
    allow_llm: bool = True,
) -> AdjacencyMatrix:
    """Return an AdjacencyMatrix for the given SpaceProgram.

    Deterministic defaults always run first.  The LLM (when allow_llm=True)
    may only adjust weights by deltas; any failure silently returns the
    deterministic matrix unchanged.
    """
    rooms = program.rooms
    valid_ids = {r.id for r in rooms}

    # Step 1 — deterministic default matrix
    edges = _build_default_edges(rooms)

    if not allow_llm:
        return AdjacencyMatrix(edges=edges)

    # Step 2 — optional LLM adjustment (exact OpenAI call pattern from template_filler)
    try:
        if openai_client is None:
            from openai import OpenAI
            openai_client = OpenAI()

        resp = openai_client.chat.completions.create(
            model=_LLM_MODEL,
            messages=[
                {"role": "system", "content": _build_system_prompt()},
                {"role": "user",   "content": _build_user_prompt(program, road_bearing_deg)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=_LLM_MAX_TOKENS,
            timeout=_LLM_TIMEOUT,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        edges = _apply_adjustments(edges, data.get("adjustments", []), valid_ids)
    except Exception:
        pass  # silent fallback — return deterministic matrix

    return AdjacencyMatrix(edges=edges)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pair_key(role_a: str, role_b: str) -> tuple[str, str]:
    """Canonical sorted key for a role pair."""
    return (min(role_a, role_b), max(role_a, role_b))


def _weight_to_type(w: float) -> str:
    if w >= 0.6:
        return "adjacent"
    if w > 0.0:
        return "near"
    return "separate"


def _build_default_edges(rooms: list[ProgramRoom]) -> list[AdjacencyEdge]:
    """Build AdjacencyEdge list from DEFAULT_WEIGHTS for all in-scope room pairs.

    Cross-unit pairs (neither room is shared) are excluded — rooms in separate
    dwelling units don't share an adjacency relationship in the program graph.
    Shared rooms (unit_id=-1, e.g. stair) are paired with all other rooms.
    """
    edges: list[AdjacencyEdge] = []
    n = len(rooms)
    for i in range(n):
        for j in range(i + 1, n):
            ra, rb = rooms[i], rooms[j]
            # Skip cross-unit pairs unless one room is shared (unit_id == -1)
            if ra.unit_id != rb.unit_id and ra.unit_id != -1 and rb.unit_id != -1:
                continue
            key = _pair_key(ra.role, rb.role)
            w = DEFAULT_WEIGHTS.get(key, 0.0)
            if w == 0.0:
                continue
            edges.append(AdjacencyEdge(
                a=ra.id, b=rb.id,
                weight=w,
                type=_weight_to_type(w),  # type: ignore[arg-type]
            ))
    return edges


def _apply_adjustments(
    edges: list[AdjacencyEdge],
    adjustments: object,
    valid_ids: set[str],
) -> list[AdjacencyEdge]:
    """Apply LLM delta adjustments to an existing edge list.

    Rules (strict validation — invalid entries silently skipped):
    - a and b must be room ids in valid_ids.
    - a != b.
    - delta must be a finite number; clamped to ±_LLM_MAX_DELTA.
    - Existing edges: weight += delta, clamped to [-1, 1].
    - New pairs with |delta| >= _NEW_EDGE_MIN_DELTA are added.
    - No duplicates.
    """
    if not isinstance(adjustments, list):
        return edges

    # Build mutable lookup keyed by frozenset pair
    edge_map: dict[frozenset[str], AdjacencyEdge] = {
        frozenset({e.a, e.b}): e for e in edges
    }

    for adj in adjustments:
        if not isinstance(adj, dict):
            continue
        a = adj.get("a")
        b = adj.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        if a not in valid_ids or b not in valid_ids:
            continue
        if a == b:
            continue
        try:
            delta = float(adj["delta"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(delta):
            continue
        delta = max(-_LLM_MAX_DELTA, min(_LLM_MAX_DELTA, delta))

        pair = frozenset({a, b})
        if pair in edge_map:
            old = edge_map[pair]
            new_w = round(max(-1.0, min(1.0, old.weight + delta)), 3)
            edge_map[pair] = AdjacencyEdge(
                a=old.a, b=old.b,
                weight=new_w,
                type=_weight_to_type(new_w),  # type: ignore[arg-type]
            )
        elif abs(delta) >= _NEW_EDGE_MIN_DELTA:
            new_w = round(max(-1.0, min(1.0, delta)), 3)
            edge_map[pair] = AdjacencyEdge(
                a=a, b=b,
                weight=new_w,
                type=_weight_to_type(new_w),  # type: ignore[arg-type]
            )

    return list(edge_map.values())


# ---------------------------------------------------------------------------
# LLM prompt builders
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    return (
        "You are a residential architect advising on room adjacency weights for a Toronto "
        "floor plan (By-law 569-2013).\n\n"
        "YOUR ONLY OUTPUT is JSON weight adjustments (deltas). "
        "You MUST NOT output coordinates, dimensions, room positions, or geometry.\n\n"
        "Each delta is a floating-point adjustment in [-0.4, +0.4]:\n"
        "  positive = strengthen adjacency (rooms should be closer / share a wall)\n"
        "  negative = strengthen separation (rooms should be farther apart)\n\n"
        "Also include orientation_prefs: a list of short strings describing site-specific "
        "placement hints (e.g. 'living_south', 'bedrooms_away_from_street'). These are "
        "advisory strings only — no coordinates.\n\n"
        "Only submit adjustments where the site orientation clearly changes the default "
        "relationship. Omit adjustments that are already correct. Keep the list short.\n\n"
        "Return ONLY valid JSON:\n"
        '{\n'
        '  "adjustments": [\n'
        '    {"a": "<room_id>", "b": "<room_id>", "delta": <float>}\n'
        '  ],\n'
        '  "orientation_prefs": ["<hint>"]\n'
        '}'
    )


def _build_user_prompt(program: SpaceProgram, road_bearing_deg: Optional[float]) -> str:
    # Orientation context
    if road_bearing_deg is not None:
        cardinal = _bearing_to_cardinal(road_bearing_deg)
        orient = (
            f"Road bearing: {road_bearing_deg:.0f}° (0=north, 90=east).\n"
            f"Street faces: {cardinal}. Adjust adjacencies for solar access and privacy.\n"
        )
    else:
        orient = "Road bearing: unknown.\n"

    # Room list
    lines = [f"  {r.id}: role={r.role}, unit={r.unit_id}, storey={r.storey}, "
             f"zone={r.zone_class}" for r in program.rooms]
    rooms_block = "\n".join(lines)

    return (
        f"{orient}\n"
        f"Room program ({len(program.rooms)} rooms):\n{rooms_block}\n\n"
        "Return weight adjustments only for relationships where this site orientation "
        "warrants a change from the defaults."
    )


def _bearing_to_cardinal(bearing_deg: float) -> str:
    """Map a compass bearing to the street-facing direction label."""
    dirs = ["north", "north-east", "east", "south-east",
            "south", "south-west", "west", "north-west"]
    idx = round((bearing_deg % 360) / 45) % 8
    return dirs[idx]
