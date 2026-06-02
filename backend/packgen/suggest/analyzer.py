"""Metric-driven plan analysis and suggestion engine.

Every Suggestion is backed by a computed metric; the LLM (optional) only
re-ranks and narrates — it cannot invent suggestions not grounded in a rule.

Usage:
    suggestions = analyze_plan(plan, brief, envelope)
    # with LLM narration (pass openai_client):
    suggestions = analyze_plan(plan, brief, envelope, openai_client=client)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Optional

from shapely.geometry import Polygon as _Poly

from ..ai.schema import FloorPlanJSON, RoomModel, StoreyModel
from ..geometry import EnvelopeResult, _laneway_suite_envelope  # provisional private import (internal module)
from ..schemas.contracts import BriefRoomSpec, BriefUnit, DesignBrief

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DAYLIGHT_MIN        = 0.08   # OBC §9.7 — 8% of floor area as window area
_CIRC_RATIO_MAX      = 0.18   # > 18% corridor/GFA → wasteful
_ASPECT_MAX          = 2.5    # OBC maximum room aspect ratio
_WET_RUN_LONG_M      = 4.5    # wet rooms more than this apart → plumbing concern
_GFA_SLACK_THRESHOLD = 12.0   # m² of unused GFA budget → suggest extra unit
_SUITE_MIN_AREA_M2   = 30.0   # minimum useful garden suite footprint
_LLM_TIMEOUT         = 10.0
_LLM_MAX_TOKENS      = 600

_HABITABLE_CATEGORIES = frozenset({
    "bedroom", "living", "dining", "kitchen", "living_dining_kitchen",
})
_WET_CATEGORIES = frozenset({"kitchen", "bathroom", "powder", "laundry"})
_CIRCULATION_CATEGORIES = frozenset({"corridor", "stair"})
_NON_GFA_CATEGORIES = frozenset({"balcony", "garage"})


# ---------------------------------------------------------------------------
# Suggestion dataclass
# ---------------------------------------------------------------------------

@dataclass
class Suggestion:
    """One actionable suggestion backed by a computed metric.

    parametric_change: a dict that apply_parametric_change() can consume to
    produce a modified (DesignBrief, target_floors) suitable for re-running
    generate_floor_plan.
    """
    title: str
    rationale: str          # cites the metric value
    rule_id: str            # stable key for LLM validation / deduplication
    metric_value: float     # raw value that triggered the rule
    parametric_change: dict  # re-solvable delta
    est_impact: str          # human-readable impact estimate
    priority: int = 2        # 1=high, 2=medium, 3=low
    llm_narration: str = ""  # optional LLM-written sentence (set after narration)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _room_area(room: RoomModel) -> float:
    if room.area_m2 is not None and room.area_m2 > 0:
        return room.area_m2
    try:
        return _Poly(room.polygon).area
    except Exception:
        return 0.0


def _room_centroid(room: RoomModel) -> tuple[float, float]:
    try:
        c = _Poly(room.polygon).centroid
        return (c.x, c.y)
    except Exception:
        xs = [p[0] for p in room.polygon]
        ys = [p[1] for p in room.polygon]
        return (sum(xs) / len(xs), sum(ys) / len(ys))


def _room_aspect(room: RoomModel) -> float:
    xs = [p[0] for p in room.polygon]
    ys = [p[1] for p in room.polygon]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    if min(w, h) < 1e-6:
        return float("inf")
    return max(w, h) / min(w, h)


def _window_area_for_storey(storey: StoreyModel) -> float:
    return sum(w.width_m * max(w.head_m - w.sill_m, 0.0) for w in storey.windows)


def _egress_window_count(storey: StoreyModel) -> int:
    return sum(1 for w in storey.windows if w.egress_compliant)


# ---------------------------------------------------------------------------
# Metric computations
# ---------------------------------------------------------------------------

def _compute_gfa(plan: FloorPlanJSON) -> float:
    total = 0.0
    for storey in plan.storeys:
        if storey.level < 0:
            continue
        for room in storey.rooms:
            if room.category not in _NON_GFA_CATEGORIES:
                total += _room_area(room)
    return total


def _compute_circulation_ratio(plan: FloorPlanJSON, gfa: float) -> float:
    if gfa < 1.0:
        return 0.0
    circ = sum(
        _room_area(r)
        for s in plan.storeys
        for r in s.rooms
        if r.category in _CIRCULATION_CATEGORIES
    )
    return circ / gfa


def _daylight_per_bedroom(plan: FloorPlanJSON) -> list[tuple[str, float]]:
    """Return (room.id, daylight_score) for each bedroom/living room."""
    results: list[tuple[str, float]] = []
    for storey in plan.storeys:
        total_storey_area = sum(_room_area(r) for r in storey.rooms) or 1.0
        win_area = _window_area_for_storey(storey)
        habitable_rooms = [r for r in storey.rooms if r.category in _HABITABLE_CATEGORIES]
        if not habitable_rooms:
            continue
        for room in habitable_rooms:
            room_area = _room_area(room)
            if room_area < 1e-6:
                continue
            # Allocate window area proportionally to room floor area
            share = (room_area / total_storey_area) * win_area
            score = share / room_area
            results.append((room.id, score))
    return results


def _aspect_outliers(plan: FloorPlanJSON) -> list[tuple[str, float]]:
    """Return (room.id, aspect_ratio) for rooms exceeding _ASPECT_MAX."""
    out = []
    for storey in plan.storeys:
        for room in storey.rooms:
            if len(room.polygon) < 3:
                continue
            asp = _room_aspect(room)
            if asp > _ASPECT_MAX:
                out.append((room.id, asp))
    return out


def _egress_violations(plan: FloorPlanJSON) -> list[str]:
    """Return room.ids of bedrooms without any egress window on their storey."""
    violations = []
    for storey in plan.storeys:
        egress_count = _egress_window_count(storey)
        bedrooms = [r for r in storey.rooms if r.category == "bedroom"]
        if bedrooms and egress_count < len(bedrooms):
            # Not enough egress windows for all bedrooms
            for room in bedrooms[egress_count:]:
                violations.append(room.id)
    return violations


def _wet_run_length(plan: FloorPlanJSON) -> float:
    """Return the maximum distance between any two wet rooms across all storeys."""
    wet_centroids: list[tuple[float, float]] = []
    for storey in plan.storeys:
        for room in storey.rooms:
            if room.category in _WET_CATEGORIES:
                wet_centroids.append(_room_centroid(room))
    if len(wet_centroids) < 2:
        return 0.0
    max_dist = 0.0
    for i in range(len(wet_centroids)):
        for j in range(i + 1, len(wet_centroids)):
            dx = wet_centroids[i][0] - wet_centroids[j][0]
            dy = wet_centroids[i][1] - wet_centroids[j][1]
            d = math.hypot(dx, dy)
            if d > max_dist:
                max_dist = d
    return max_dist


def _unused_gfa(plan: FloorPlanJSON, envelope: EnvelopeResult, gfa: float) -> float:
    """GFA budget remaining after placed rooms (positive → unused capacity)."""
    above_grade_storeys = len({s.level for s in plan.storeys if s.level >= 0}) or 1
    budget = envelope.envelope_2d.area * above_grade_storeys * 0.82
    return max(0.0, budget - gfa)


# ---------------------------------------------------------------------------
# Rule set → Suggestions
# ---------------------------------------------------------------------------

def _rules(
    plan: FloorPlanJSON,
    brief: DesignBrief,
    envelope: EnvelopeResult,
    gfa: float,
) -> list[Suggestion]:
    suggestions: list[Suggestion] = []
    n_units = len(brief.units)
    above_grade = len({s.level for s in plan.storeys if s.level >= 0}) or 1

    # R1 — Egress / bedroom daylight
    violations = _egress_violations(plan)
    if violations:
        n = len(violations)
        suggestions.append(Suggestion(
            rule_id="egress_missing",
            title="Bedroom egress windows missing",
            rationale=(
                f"{n} bedroom(s) on this storey have no OBC §9.7 egress-compliant window. "
                "Adding a floor spreads bedrooms to exterior walls."
            ),
            metric_value=float(n),
            parametric_change={"type": "increase_floors", "delta": 1},
            est_impact=f"Moves {n} bedroom(s) to exterior walls with egress window access",
            priority=1,
        ))

    # R2 — Poor habitable-room daylight (< 8% rule)
    daylight = _daylight_per_bedroom(plan)
    poor = [(rid, sc) for rid, sc in daylight if sc < _DAYLIGHT_MIN]
    if poor:
        worst_score = min(sc for _, sc in poor)
        suggestions.append(Suggestion(
            rule_id="poor_daylight",
            title="Low daylight ratio in habitable rooms",
            rationale=(
                f"{len(poor)} habitable room(s) have a daylight ratio below the OBC §9.7 "
                f"target of {_DAYLIGHT_MIN*100:.0f}%. Worst: {worst_score*100:.1f}%."
            ),
            metric_value=worst_score,
            parametric_change={"type": "increase_floors", "delta": 1},
            est_impact="Adding a floor increases exterior wall exposure for upper-storey bedrooms",
            priority=2,
        ))

    # R3 — High circulation ratio
    circ_ratio = _compute_circulation_ratio(plan, gfa)
    if circ_ratio > _CIRC_RATIO_MAX:
        suggestions.append(Suggestion(
            rule_id="high_circulation",
            title="Excess corridor area",
            rationale=(
                f"Corridor/stair area is {circ_ratio*100:.0f}% of GFA "
                f"(target ≤ {_CIRC_RATIO_MAX*100:.0f}%). "
                "Horizontal stacking eliminates shared vertical circulation."
            ),
            metric_value=circ_ratio,
            parametric_change={"type": "change_stacking", "value": "horizontal"},
            est_impact=f"Reduces circulation area by ~{(circ_ratio - 0.10) * gfa:.0f} m²",
            priority=2,
        ))

    # R4 — Aspect ratio outliers
    outliers = _aspect_outliers(plan)
    if outliers:
        worst_id, worst_asp = max(outliers, key=lambda x: x[1])
        suggestions.append(Suggestion(
            rule_id="aspect_outlier",
            title="Poorly proportioned room",
            rationale=(
                f"Room '{worst_id}' has an aspect ratio of {worst_asp:.1f} "
                f"(max {_ASPECT_MAX}). Narrow rooms are not practically furnishable."
            ),
            metric_value=worst_asp,
            parametric_change={"type": "change_stacking", "value": "horizontal"},
            est_impact="Horizontal stacking produces wider rooms on narrower lots",
            priority=2,
        ))

    # R5 — Unused GFA → add a rental unit
    unused = _unused_gfa(plan, envelope, gfa)
    if unused >= _GFA_SLACK_THRESHOLD:
        suggestions.append(Suggestion(
            rule_id="unused_gfa",
            title=f"~{unused:.0f} m² of GFA budget unused",
            rationale=(
                f"The buildable envelope has ~{unused:.0f} m² of GFA budget not used by "
                "the current brief. Adding a studio unit maximises the investment."
            ),
            metric_value=unused,
            parametric_change={
                "type": "add_unit",
                "rooms": [
                    {"role": "living",   "count": 1, "storey_preference": 0},
                    {"role": "kitchen",  "count": 1, "storey_preference": 0},
                    {"role": "bathroom", "count": 1, "storey_preference": 0},
                ],
            },
            est_impact=f"~{unused:.0f} m² new rental unit; increases rental income potential",
            priority=2,
        ))

    # R6 — Wet run length
    wet_run = _wet_run_length(plan)
    if wet_run > _WET_RUN_LONG_M:
        suggestions.append(Suggestion(
            rule_id="wet_run_long",
            title="Wet rooms spread across building",
            rationale=(
                f"Wet rooms (kitchen, bathrooms, laundry) are up to {wet_run:.1f} m apart. "
                f"Plumbing stacks ideally run within {_WET_RUN_LONG_M} m of each other."
            ),
            metric_value=wet_run,
            parametric_change={"type": "change_stacking", "value": "vertical"},
            est_impact="Vertical stacking aligns wet rooms on successive floors, shortening pipe runs",
            priority=3,
        ))

    # R7 — Garden suite potential (§150.1 / §150.7)
    # Use pre-computed suite_envelope_2d when include_laneway was True in the request;
    # otherwise attempt the computation from the resolved rear setback.
    suite_poly = envelope.suite_envelope_2d
    if suite_poly is None:
        rear_setback = envelope.setbacks_applied.get("rear", 0.0)
        try:
            suite_poly = _laneway_suite_envelope(envelope.lot_local, rear_setback)
        except Exception:
            suite_poly = None

    if suite_poly is not None and suite_poly.area >= _SUITE_MIN_AREA_M2:
        suggestions.append(Suggestion(
            rule_id="garden_suite_potential",
            title="Garden suite potential",
            rationale=(
                f"The rear yard supports a {suite_poly.area:.0f} m² ancillary suite "
                "(§150.1 / §150.7). Toronto's 2022 garden suite by-law permits this "
                "as-of-right with no minor variance required. Typical rental income: "
                "$1,800–2,400/month."
            ),
            metric_value=round(suite_poly.area, 1),
            parametric_change={"include_laneway": True},
            est_impact=(
                f"Additional {suite_poly.area:.0f} m² unit; does not count against "
                "principal building FSI or unit count."
            ),
            priority=2,
        ))

    return suggestions


# ---------------------------------------------------------------------------
# LLM narration (optional, strictly additive)
# ---------------------------------------------------------------------------

def narrate_suggestions(
    suggestions: list[Suggestion],
    brief: DesignBrief,
    openai_client,
) -> list[Suggestion]:
    """Re-order and add a one-sentence narration from the LLM.

    Validation: the LLM may only reference rule_ids that appear in the input list.
    Anything the LLM invents is silently dropped.
    """
    if not suggestions or openai_client is None:
        return suggestions

    known_ids = {s.rule_id for s in suggestions}
    sugg_block = "\n".join(
        f"  {i+1}. rule_id={s.rule_id} | title={s.title} | metric={s.metric_value:.2f}"
        for i, s in enumerate(suggestions)
    )
    budget_tier = getattr(brief, "budget_tier", None) or "mid"
    n_units = len(brief.units)

    system_prompt = (
        "You are a Toronto residential architect advising a developer. "
        "You receive a list of plan-analysis suggestions — each is rule-derived and "
        "metric-backed. Your task:\n"
        "1. Re-order them by relevance for this client profile (most impactful first).\n"
        "2. Write ONE short sentence per suggestion (plain English, no jargon).\n"
        "3. Return ONLY valid JSON. DO NOT invent suggestions not in the list.\n\n"
        "Return exactly:\n"
        '{\n  "ordered": [\n'
        '    {"rule_id": "<exact rule_id>", "sentence": "<one sentence>"}\n'
        '  ]\n}\n'
        "Only include rule_ids from the input list."
    )
    user_prompt = (
        f"Client: {n_units}-unit development, budget_tier={budget_tier}.\n\n"
        f"Suggestions to narrate and re-order:\n{sugg_block}"
    )

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=_LLM_MAX_TOKENS,
            timeout=_LLM_TIMEOUT,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        ordered = data.get("ordered", [])

        # Validate: only keep items whose rule_id exists in our input
        id_to_sugg = {s.rule_id: s for s in suggestions}
        result: list[Suggestion] = []
        seen: set[str] = set()
        for item in ordered:
            rid = item.get("rule_id", "")
            if rid not in known_ids or rid in seen:
                continue
            seen.add(rid)
            s = id_to_sugg[rid]
            s.llm_narration = str(item.get("sentence", ""))[:300]
            result.append(s)

        # Append any suggestions the LLM dropped (preserve completeness)
        for s in suggestions:
            if s.rule_id not in seen:
                result.append(s)

        return result
    except Exception:
        return suggestions   # silent fallback to deterministic order


# ---------------------------------------------------------------------------
# Parametric change application
# ---------------------------------------------------------------------------

def apply_parametric_change(
    brief: DesignBrief,
    change: dict,
    target_floors: int = 2,
) -> tuple[DesignBrief, int]:
    """Apply one parametric_change dict and return (new_brief, new_target_floors).

    All output is valid input for generate_floor_plan(brief, envelope, target_floors).
    """
    change_type = change.get("type", "")

    if change_type == "increase_floors":
        delta = int(change.get("delta", 1))
        new_floors = max(1, min(4, target_floors + delta))
        return brief, new_floors

    if change_type == "reduce_bedrooms":
        unit_id = int(change.get("unit_id", 1))
        delta = int(change.get("delta", -1))    # negative = remove
        new_units = []
        for u in brief.units:
            if u.unit_id == unit_id:
                new_rooms = []
                removed = 0
                for r in u.rooms:
                    if r.role == "bedroom" and removed < abs(delta):
                        # Remove one bedroom if we haven't removed enough yet
                        new_count = max(0, r.count + delta)
                        removed += r.count - new_count
                        if new_count > 0:
                            new_rooms.append(r.model_copy(update={"count": new_count}))
                    else:
                        new_rooms.append(r)
                # Keep unit even if rooms shrink; ensure at least 1 room
                if new_rooms:
                    new_units.append(u.model_copy(update={"rooms": new_rooms}))
                else:
                    new_units.append(u)
            else:
                new_units.append(u)
        return brief.model_copy(update={"units": new_units}), target_floors

    if change_type == "change_stacking":
        value = str(change.get("value", "vertical"))
        if value not in ("vertical", "horizontal", "mixed"):
            value = "vertical"
        return brief.model_copy(update={"stacking_pref": value}), target_floors

    if change_type == "add_unit":
        room_specs_raw = change.get("rooms", [
            {"role": "living",   "count": 1, "storey_preference": 0},
            {"role": "kitchen",  "count": 1, "storey_preference": 0},
            {"role": "bathroom", "count": 1, "storey_preference": 0},
        ])
        new_uid = max(u.unit_id for u in brief.units) + 1
        new_rooms = [
            BriefRoomSpec(
                role=r["role"],                          # type: ignore[arg-type]
                count=r.get("count", 1),
                storey_preference=r.get("storey_preference", 0),
            )
            for r in room_specs_raw
        ]
        new_unit = BriefUnit(unit_id=new_uid, rooms=new_rooms)
        return brief.model_copy(update={"units": list(brief.units) + [new_unit]}), target_floors

    # Unknown type → no change
    return brief, target_floors


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_plan(
    plan: FloorPlanJSON,
    brief: DesignBrief,
    envelope: EnvelopeResult,
    openai_client=None,
) -> list[Suggestion]:
    """Analyse a solved floor plan and return metric-backed Suggestions.

    Deterministic metric computation always runs first.
    If openai_client is provided, an optional LLM step re-orders and narrates
    the suggestions — it cannot add suggestions not backed by a rule.
    """
    gfa = _compute_gfa(plan)
    suggestions = _rules(plan, brief, envelope, gfa)

    # Sort by priority (high first), then metric severity descending
    suggestions.sort(key=lambda s: (s.priority, -abs(s.metric_value)))

    if openai_client is not None:
        suggestions = narrate_suggestions(suggestions, brief, openai_client)

    return suggestions
