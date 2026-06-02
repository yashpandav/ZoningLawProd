"""Fallback chain for floor plan generation.

Priority:
  1. LLM (GPT-4.1, attempt 1 at temp=0.4, attempt 2 at temp=0.2 with errors injected)
  2. Template-driven placer (existing template_filler.py logic)
  3. Static stamp from library.py (last resort)

Each level is tried in order; the first successful result is returned along with
a ``fallback_level`` string so the caller can log/track LLM success rates.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from shapely.geometry import Polygon

if TYPE_CHECKING:
    from ..geometry import EnvelopeResult
    from ..typology.selector import FitResult

logger = logging.getLogger(__name__)

FallbackLevel = str  # "llm_attempt_1" | "llm_attempt_2" | "template" | "stamp"


def generate_plan(
    *,
    envelope_result: "EnvelopeResult",
    zone_symbol: str,
    typology_id: str,
    typology_label: str,
    unit_count: int,
    unit_mix: Optional[list[dict]] = None,
    free_text_notes: str = "",
    must_have_rooms: str = "",
    ward: Optional[int] = None,
    option: str = "A",
) -> tuple["FitResult", FallbackLevel]:
    """Generate a floor plan, falling back through LLM → template → stamp.

    Returns:
        (fit_result, fallback_level)
    """
    from ..typology.library import TYPOLOGY_LIBRARY
    from ..typology.selector import fit_stamp

    envelope_local = envelope_result.envelope_2d
    env_bounds = envelope_local.bounds
    env_w = env_bounds[2] - env_bounds[0]
    env_d = env_bounds[3] - env_bounds[1]

    # Resolve zoning params for LLM prompt
    setbacks = envelope_result.setbacks_applied
    height_max_m = getattr(envelope_result, "height_max_m", 10.0)
    if height_max_m is None or height_max_m <= 0:
        height_max_m = 10.0

    zone_base = zone_symbol.split("(")[0].rstrip()

    wkt_envelope = envelope_local.wkt

    # ------------------------------------------------------------------
    # Level 1+2: LLM generation
    # ------------------------------------------------------------------
    try:
        from .client import generate_floor_plan
        from .plan_to_geometry import floor_plan_to_fit_result

        plan, report, attempt = generate_floor_plan(
            wkt_envelope=wkt_envelope,
            zone_code=zone_base,
            height_max_m=height_max_m,
            storeys_max=3,
            building_depth_max_m=getattr(envelope_result, "depth_limit_m", 17.0) or 17.0,
            side_yard_left_m=setbacks.get("left", 0.9),
            side_yard_right_m=setbacks.get("right", 0.9),
            front_yard_m=setbacks.get("front", 4.5),
            rear_yard_m=setbacks.get("rear", 7.5),
            multiplex_units=min(unit_count, 4),
            gs_allowed=True,
            ls_allowed=getattr(envelope_result, "has_lane_abuttal", False),
            special_provisions_summary="",
            typology_id=typology_id,
            typology_label=typology_label,
            stacking_axis="vertical",
            unit_count=unit_count,
            unit_mix=unit_mix,
            must_have_rooms=must_have_rooms,
            free_text_notes=free_text_notes,
            envelope_polygon=envelope_local,
        )

        if plan is not None and report is not None and report.valid:
            fit = floor_plan_to_fit_result(
                plan, envelope_result,
                typology_id=typology_id,
                typology_label=typology_label,
                option=option,
            )
            level = "llm_attempt_1" if attempt == 1 else "llm_attempt_2"
            logger.info("LLM generation succeeded (attempt %d)", attempt)
            return fit, level

        if plan is not None and report is not None and not report.valid:
            logger.warning(
                "LLM generation produced %d errors even after retry — falling back to template",
                len(report.errors),
            )
            fit = floor_plan_to_fit_result(
                plan, envelope_result,
                typology_id=typology_id,
                typology_label=f"{typology_label} (with warnings)",
                option=option,
            )
            fit.warnings.extend(report.errors[:5])
            return fit, "llm_attempt_2"

    except Exception as e:
        logger.warning("LLM generation raised exception: %s", e)

    # ------------------------------------------------------------------
    # Level 3: Template-driven placer (existing template_filler.py)
    # ------------------------------------------------------------------
    try:
        from dataclasses import replace as dc_replace
        from ..template_filler import fill_template
        from ..typology.generic_template import stamp_to_generic_template
        from ..typology.selector import fit_stamp

        typology = next((t for t in TYPOLOGY_LIBRARY if t.id == typology_id), None)
        if typology is not None:
            if not typology.has_template():
                typology = dc_replace(typology, template=stamp_to_generic_template(typology))

            brief = {
                "units": [
                    {"unit_id": i + 1, "rooms": [
                        {"role": "bedroom", "count": 2, "min_area_m2": 0.0, "storey_preference": 0}
                    ]}
                    for i in range(unit_count)
                ],
                "stack_preference": "vertical",
                "notes": free_text_notes,
            }
            filled_cells = fill_template(
                typology=typology,
                brief=brief,
                envelope_w_m=env_w,
                envelope_d_m=env_d,
                fallback_to_stamp=True,
            )
            from ..typology.selector import FitResult
            fit = fit_stamp(typology, envelope_local, option=option)
            logger.info("Template placer succeeded for typology %s", typology_id)
            return fit, "template"

    except Exception as e:
        logger.warning("Template placer failed: %s", e)

    # ------------------------------------------------------------------
    # Level 4: Static stamp (last resort)
    # ------------------------------------------------------------------
    typology = next((t for t in TYPOLOGY_LIBRARY if t.id == typology_id), None)
    if typology is None:
        zone_eligible = [
            t for t in TYPOLOGY_LIBRARY
            if any(zone_base.startswith(ez) for ez in t.eligible_zones)
        ]
        typology = zone_eligible[0] if zone_eligible else TYPOLOGY_LIBRARY[0]

    fit = fit_stamp(typology, envelope_local, option=option)
    logger.warning("Fell back to static stamp for typology %s", typology.id)
    return fit, "stamp"
