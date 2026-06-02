"""AI-narrated typology ranking for the Design Studio recommendation badge."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from shapely.geometry import Polygon

from ..typology.library import TYPOLOGY_LIBRARY
from ..typology.models import Typology
from ..typology.selector import _envelope_dims, _envelope_fit_score, _gfa_score


@dataclass
class RankedTypology:
    typology_id: str
    label: str
    units_produced: int
    stacking_axis: str
    deterministic_score: float   # 0..1
    fits_lot: bool
    ai_reason: str               # one sentence, "" if LLM call fails
    rank: int                    # 1 = top recommendation


def _filter_candidates(
    envelope_local: Polygon,
    zone_symbol: str,
    units_target: int,
    ward: Optional[int],
) -> list[tuple[Typology, float]]:
    env_w, env_d = _envelope_dims(envelope_local)
    env_area = envelope_local.area
    zone_base = zone_symbol.split("(")[0].rstrip()

    scored: list[tuple[Typology, float]] = []
    for t in TYPOLOGY_LIBRARY:
        if not any(zone_base.startswith(ez) for ez in t.eligible_zones):
            continue
        if t.eligible_wards is not None and ward not in t.eligible_wards:
            continue

        fits_geom = (t.min_frontage_m <= env_w + 0.5
                     and t.min_depth_m <= env_d + 0.5)
        units_match = (t.units_produced == units_target
                       or (t.requires_basement and abs(t.units_produced - units_target) == 1))

        score = (
            _envelope_fit_score(t, env_w, env_d) * 0.4
            + _gfa_score(t, env_area) * 0.3
            + (0.15 if fits_geom else 0.0)
            + (0.15 if units_match else 0.0)
        )
        scored.append((t, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def rank_typologies(
    envelope_local: Polygon,
    zone_symbol: str,
    units_target: int,
    ward: Optional[int] = None,
    brief: Optional[str] = None,
    top_n: int = 3,
) -> list[RankedTypology]:
    """Return top-N typologies with AI-narrated reasons.

    The deterministic score is authoritative; the LLM only narrates.
    If the OpenAI call fails or times out, returns the deterministic ranking
    with ai_reason="" for each entry — the badge degrades gracefully.
    """
    scored = _filter_candidates(envelope_local, zone_symbol, units_target, ward)
    if not scored:
        return []

    top = scored[:top_n]
    env_w, env_d = _envelope_dims(envelope_local)

    reasons = _narrate_with_llm(top, env_w, env_d, zone_symbol, units_target, brief)

    out: list[RankedTypology] = []
    for rank, ((t, score), reason) in enumerate(zip(top, reasons), start=1):
        out.append(RankedTypology(
            typology_id=t.id,
            label=t.label,
            units_produced=t.units_produced,
            stacking_axis=t.stacking_axis,
            deterministic_score=round(score, 3),
            fits_lot=(t.min_frontage_m <= env_w + 0.5
                      and t.min_depth_m <= env_d + 0.5),
            ai_reason=reason,
            rank=rank,
        ))
    return out


def _narrate_with_llm(
    top: list[tuple[Typology, float]],
    env_w: float,
    env_d: float,
    zone_symbol: str,
    units_target: int,
    brief: Optional[str],
) -> list[str]:
    if not top:
        return []

    try:
        from openai import OpenAI

        candidate_lines = []
        for i, (t, _score) in enumerate(top, start=1):
            gfa_min, gfa_max = t.target_gfa_per_unit_m2
            candidate_lines.append(
                f"{i}. {t.id} — {t.label}, {t.units_produced} units, "
                f"{t.stacking_axis} stacking, frontage range "
                f"{t.min_frontage_m}-{t.max_frontage_m}m, "
                f"GFA {gfa_min}-{gfa_max}m²/unit. "
                f"Notes: {t.notes}"
            )

        system_msg = (
            "You are a Toronto building code expert helping an architect choose a "
            "preliminary floor plan typology. You will receive a ranked list of "
            "typology candidates that have already been scored for geometric fit "
            "and zoning eligibility. Your job is to write ONE sentence per candidate "
            "explaining why an architect might pick it, drawing on the candidate's "
            "metadata and the lot context. Do not re-rank. Do not invent typologies "
            "that aren't in the list. Do not contradict the provided scores.\n\n"
            "Return ONLY valid JSON in this exact shape, with `reasons` as an array "
            "the same length as `candidates`, in the same order:\n"
            '{"reasons": ["sentence 1", "sentence 2", "sentence 3"]}\n\n'
            "Each sentence must be under 25 words, plain English, and mention one "
            "concrete reason (frontage fit, GFA efficiency, stacking style, basement "
            "suite eligibility, etc.). Do not start sentences with \"This typology\"."
        )

        user_msg = (
            f"Lot: {env_w:.1f}m wide × {env_d:.1f}m deep, zone {zone_symbol}.\n"
            f"Target units: {units_target}.\n"
            f"Architect's program: {brief or 'none provided'}\n\n"
            "Candidates (ranked by deterministic score, highest first):\n"
            + "\n".join(candidate_lines)
        )

        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=400,
            timeout=8.0,
        )

        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        reasons = data.get("reasons", [])

        if not isinstance(reasons, list) or len(reasons) != len(top):
            return [""] * len(top)

        result = []
        for r in reasons:
            if not isinstance(r, str) or len(r) > 200:
                result.append("")
            else:
                result.append(r)
        return result

    except Exception:
        return [""] * len(top)
