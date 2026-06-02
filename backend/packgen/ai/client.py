"""OpenAI wrapper for FloorPlanJSON generation.

Calls GPT-4.1 with structured output (JSON schema), validates the response with
``plan_validator``, and injects errors into a second attempt if attempt 1 fails.
System prompt is passed once; OpenAI's prompt-caching TTL (5 min) means
repeated calls in the same session are cheaper.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from pydantic import ValidationError
from shapely.geometry import Polygon

from .plan_validator import ValidationReport, validate_plan
from .prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from .schema import FloorPlanJSON

logger = logging.getLogger(__name__)

_MODEL = "gpt-4.1"
_TIMEOUT_S = 45.0
_MAX_TOKENS = 6000


def _make_user_prompt(
    wkt_envelope: str,
    zone_code: str,
    height_max_m: float,
    storeys_max: int,
    building_depth_max_m: float,
    side_yard_left_m: float,
    side_yard_right_m: float,
    front_yard_m: float,
    rear_yard_m: float,
    multiplex_units: int,
    gs_allowed: bool,
    ls_allowed: bool,
    special_provisions_summary: str,
    typology_id: str,
    typology_label: str,
    stacking_axis: str,
    unit_count: int,
    unit_mix: list[dict],
    must_have_rooms: str,
    free_text_notes: str,
    prior_errors: Optional[list[str]] = None,
) -> str:
    prompt = USER_PROMPT_TEMPLATE.format(
        wkt_envelope=wkt_envelope,
        zone_code=zone_code,
        height_max_m=height_max_m,
        storeys_max=storeys_max,
        building_depth_max_m=building_depth_max_m,
        side_yard_left_m=side_yard_left_m,
        side_yard_right_m=side_yard_right_m,
        front_yard_m=front_yard_m,
        rear_yard_m=rear_yard_m,
        multiplex_units=multiplex_units,
        gs_allowed=gs_allowed,
        ls_allowed=ls_allowed,
        special_provisions_summary=special_provisions_summary,
        typology_id=typology_id,
        typology_label=typology_label,
        stacking_axis=stacking_axis,
        unit_count=unit_count,
        unit_mix=json.dumps(unit_mix),
        must_have_rooms=must_have_rooms,
        free_text_notes=free_text_notes or "none",
    )
    if prior_errors:
        prompt += (
            "\n\nYour previous output had these validation errors. Fix ALL of them and resubmit:\n"
            + "\n".join(f"  - {e}" for e in prior_errors)
        )
    return prompt


def generate_floor_plan(
    *,
    wkt_envelope: str,
    zone_code: str,
    height_max_m: float = 10.0,
    storeys_max: int = 3,
    building_depth_max_m: float = 17.0,
    side_yard_left_m: float = 0.9,
    side_yard_right_m: float = 0.9,
    front_yard_m: float = 4.5,
    rear_yard_m: float = 7.5,
    multiplex_units: int = 4,
    gs_allowed: bool = True,
    ls_allowed: bool = False,
    special_provisions_summary: str = "",
    typology_id: str = "stacked_duplex",
    typology_label: str = "Stacked Duplex",
    stacking_axis: str = "vertical",
    unit_count: int = 2,
    unit_mix: Optional[list[dict]] = None,
    must_have_rooms: str = "",
    free_text_notes: str = "",
    envelope_polygon: Optional[Polygon] = None,
) -> tuple[FloorPlanJSON | None, ValidationReport | None, int]:
    """Call GPT-4.1 to generate a FloorPlanJSON, with one retry on validation failure.

    Returns:
        (plan, report, attempt_used)
        - plan is None if both attempts failed or produced an error JSON
        - attempt_used is 1 or 2
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("openai package not installed")
        return None, None, 0

    client = OpenAI(timeout=_TIMEOUT_S)
    unit_mix = unit_mix or [{"bedrooms": 2, "target_m2": 85}] * unit_count

    def _call(temperature: float, prior_errors: Optional[list[str]] = None) -> dict | None:
        user_msg = _make_user_prompt(
            wkt_envelope=wkt_envelope,
            zone_code=zone_code,
            height_max_m=height_max_m,
            storeys_max=storeys_max,
            building_depth_max_m=building_depth_max_m,
            side_yard_left_m=side_yard_left_m,
            side_yard_right_m=side_yard_right_m,
            front_yard_m=front_yard_m,
            rear_yard_m=rear_yard_m,
            multiplex_units=multiplex_units,
            gs_allowed=gs_allowed,
            ls_allowed=ls_allowed,
            special_provisions_summary=special_provisions_summary,
            typology_id=typology_id,
            typology_label=typology_label,
            stacking_axis=stacking_axis,
            unit_count=unit_count,
            unit_mix=unit_mix,
            must_have_rooms=must_have_rooms,
            free_text_notes=free_text_notes,
            prior_errors=prior_errors,
        )
        try:
            resp = client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=_MAX_TOKENS,
            )
            raw = resp.choices[0].message.content or "{}"
            return json.loads(raw)
        except Exception as e:
            logger.warning("LLM call failed: %s", e)
            return None

    # Attempt 1 — temperature 0.4
    raw1 = _call(temperature=0.4)
    if raw1 and "error" not in raw1:
        try:
            plan1 = FloorPlanJSON.model_validate(raw1)
            report1 = validate_plan(plan1, envelope_polygon, max_height_m=height_max_m)
            if report1.valid:
                return plan1, report1, 1
            # Attempt 2 — temperature 0.2, inject errors
            logger.info("Attempt 1 invalid (%d errors), retrying", len(report1.errors))
            raw2 = _call(temperature=0.2, prior_errors=report1.errors)
            if raw2 and "error" not in raw2:
                plan2 = FloorPlanJSON.model_validate(raw2)
                report2 = validate_plan(plan2, envelope_polygon, max_height_m=height_max_m)
                return plan2, report2, 2
        except (ValidationError, ValueError) as e:
            logger.warning("Schema validation failed on attempt 1: %s", e)
            # Attempt 2 with the schema error
            raw2 = _call(temperature=0.2, prior_errors=[str(e)])
            if raw2 and "error" not in raw2:
                try:
                    plan2 = FloorPlanJSON.model_validate(raw2)
                    report2 = validate_plan(plan2, envelope_polygon, max_height_m=height_max_m)
                    return plan2, report2, 2
                except Exception:
                    pass

    return None, None, 2
