"""Immutable record of the resolved zoning params used to build an envelope.

Saved as 'zoning_snapshot.json' in every generated ZIP.  All downstream
validation should use the snapshot values — never re-resolve from the zone
symbol — so the envelope and the validator can never disagree.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ZoningSnapshot(BaseModel):
    """Frozen record of resolved By-law 569-2013 parameters for one generation run.

    Fields map directly to the values used by build_envelope so a planner can
    reproduce or audit every decision from the JSON alone.
    """
    model_config = {"frozen": True}

    zone_symbol:           str                # full label, e.g. "RD (f10.5)(d0.6)"
    resolved_at:           str                # ISO-8601 UTC timestamp

    # Setbacks applied to the principal building envelope (metres)
    front_setback_m:       float              # §10.20.40.70(1) / §10.10.40.70(1)
    rear_setback_m:        float              # §10.20.40.70(2) / §10.10.40.70
    left_setback_m:        float              # §10.20.40.70(3) / §10.10.40.70(3)
    right_setback_m:       float              # §10.20.40.70(3) / §10.10.40.70(3)

    # Building dimension limits
    building_depth_max_m:  float              # §10.20.40.30 — front-to-rear
    building_length_max_m: Optional[float]    # §10.20.40.20 — street-parallel; None for wide lots

    # Density
    fsi:                   Optional[float]    # Floor Space Index cap; None = not limited
    fsi_exempt:            bool               # True for 2–4 unit multiplexes (By-law 474-2023)
    max_coverage_pct:      Optional[float]    # None = no overlay coverage limit

    # Height
    height_max_m:          Optional[float]    # from overlay or zone default

    # Provenance
    overlay_source:        str                # "map_overlay" | "zone_suffix" | "code_default"

    # All warnings emitted during resolution (params + resolver)
    warnings:              list[str]
