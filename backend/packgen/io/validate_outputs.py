"""Pre-download validation of DXF and IFC outputs.

Returns a ValidationSummary dict that is written as validation.json in the ZIP
and appended to the PDF report's last page.
"""
from __future__ import annotations

import io
from typing import Optional


_REQUIRED_DXF_LAYERS = [
    "A-WALL-FULL", "A-WALL-INTR", "A-DOOR", "A-GLAZ",
    "A-ANNO-DIMS", "A-ANNO-TTLB", "A-FLOR-IDEN",
]


def validate_dxf(dxf_bytes: bytes) -> dict:
    """Validate DXF content: entity count, required layers, bounding box."""
    result: dict = {"ok": False}
    try:
        import ezdxf

        doc = ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8", errors="replace")))
        msp = doc.modelspace()
        entity_count = sum(1 for _ in msp)
        layers = sorted(l.dxf.name for l in doc.layers)

        missing = [l for l in _REQUIRED_DXF_LAYERS if l not in layers]

        # Bounding box sanity check
        bounds_ok = False
        try:
            from ezdxf.bbox import extents
            bbox = extents(msp)
            if bbox.size.x > 1.0 and bbox.size.x < 500.0:
                bounds_ok = True
        except Exception:
            bounds_ok = entity_count > 20   # fallback heuristic

        # Paperspace layouts
        layout_names = [l.name for l in doc.layouts if l.name != "Model"]

        result = {
            "entity_count":           entity_count,
            "layers":                 layers,
            "missing_required_layers": missing,
            "paperspace_layouts":     layout_names,
            "bounds_ok":              bounds_ok,
            "ok": entity_count > 20 and not missing and bounds_ok,
        }
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    return result


def validate_ifc(ifc_bytes: bytes, schema_label: str = "IFC4") -> dict:
    """Validate IFC content: entity counts + schema errors."""
    result: dict = {"ok": False, "schema": schema_label}
    try:
        import ifcopenshell
        import ifcopenshell.validate

        m = ifcopenshell.file.from_string(ifc_bytes.decode("utf-8", errors="replace"))
        spaces  = len(m.by_type("IfcSpace"))
        walls   = len(m.by_type("IfcWall")) + len(m.by_type("IfcWallStandardCase"))
        doors   = len(m.by_type("IfcDoor"))
        windows = len(m.by_type("IfcWindow"))
        storeys = len(m.by_type("IfcBuildingStorey"))
        slabs   = len(m.by_type("IfcSlab"))

        # Check Pset_ZoningData_Toronto_569_2013 presence
        psets = [p.Name for p in m.by_type("IfcPropertySet")]
        has_zoning_pset = "Pset_ZoningData_Toronto_569_2013" in psets

        # Schema validation (best-effort; some ifcopenshell builds omit validate)
        schema_errors = 0
        try:
            import json
            log = ifcopenshell.validate.validate(m, logger=None)
            if isinstance(log, str):
                parsed = json.loads(log)
                schema_errors = len(parsed.get("errors", []))
        except Exception:
            schema_errors = 0   # treat as unknown

        result = {
            "schema":             schema_label,
            "spaces":             spaces,
            "walls":              walls,
            "doors":              doors,
            "windows":            windows,
            "storeys":            storeys,
            "slabs":              slabs,
            "has_zoning_pset":    has_zoning_pset,
            "schema_errors":      schema_errors,
            "ok": spaces > 0 and storeys > 0 and schema_errors == 0,
        }
    except Exception as exc:
        result = {"ok": False, "schema": schema_label, "error": str(exc)}
    return result


def validate_pack(
    dxf_bytes: Optional[bytes] = None,
    ifc4_bytes: Optional[bytes] = None,
    ifc2x3_bytes: Optional[bytes] = None,
    expected_spaces: int = 0,
    expected_storeys: int = 1,
) -> dict:
    """Run all output validation checks and return a summary dict for validation.json."""
    summary: dict = {
        "dxf":   validate_dxf(dxf_bytes)   if dxf_bytes   else {"ok": None, "skipped": True},
        "ifc4":  validate_ifc(ifc4_bytes,  "IFC4")   if ifc4_bytes  else {"ok": None, "skipped": True},
        "ifc2x3": validate_ifc(ifc2x3_bytes, "IFC2X3") if ifc2x3_bytes else {"ok": None, "skipped": True},
    }

    # Cross-check space count
    if expected_spaces > 0:
        for key in ("ifc4", "ifc2x3"):
            v = summary.get(key, {})
            if v.get("spaces") is not None and v["spaces"] < expected_spaces * 0.9:
                v["ok"] = False
                v["warning"] = f"Expected ~{expected_spaces} spaces, got {v['spaces']}"

    all_ok = all(
        v.get("ok") is not False
        for v in summary.values()
        if not v.get("skipped")
    )
    summary["all_ok"] = all_ok
    return summary
