"""PDF README generator for the PackGen ZIP export.

Produces a permit-ready preliminary report with 9 sections:
  1  Site Summary
  2  Zoning Rules Applied
  3  Buildable Envelope
  4  Typology Selected
  5  Room Schedule
  6  OBC Compliance Summary
  7  By-law Citations
  8  Assumptions & Limitations
  9  AI Disclosure

All content is derived from the structured data passed in — no LLM call,
so this runs synchronously without adding API latency.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

from .geometry import EnvelopeResult
from .obc import OBCResult
from .typology.selector import FitResult


# ---------------------------------------------------------------------------
# AI design narrative (GPT-4.1-mini, 8 s timeout, graceful fallback)
# ---------------------------------------------------------------------------

def _generate_narrative(
    typology_name: str,
    unit_count: int,
    zone_symbol: str,
    brief_summary: str = "",
) -> str:
    """Return a 2–3 sentence design intent statement, or a deterministic fallback."""
    try:
        from openai import OpenAI
        client = OpenAI(timeout=8.0)
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            max_tokens=130,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a licensed architect writing a brief design intent statement "
                        "for a Toronto zoning by-law compliant residential building. "
                        "Write exactly 2–3 sentences. Be specific about the typology, unit count, "
                        "and zoning context. Professional but accessible tone. Do not mention AI."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Typology: {typology_name}. "
                        f"Units: {unit_count}. "
                        f"Zone: {zone_symbol}. "
                        + (f"Room brief: {brief_summary}" if brief_summary else "")
                    ),
                },
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return (
            f"This {typology_name} development provides {unit_count} residential "
            f"unit{'s' if unit_count != 1 else ''} within the {zone_symbol} zone, "
            f"designed to maximize the permitted building envelope while satisfying "
            f"Ontario Building Code Part 9 requirements."
        )

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_BLUE        = colors.HexColor("#1a3a6b")
_BLUE_LIGHT  = colors.HexColor("#2d5fa0")
_GREY_LIGHT  = colors.HexColor("#f5f5f5")
_GREY_BORDER = colors.HexColor("#cccccc")
_GREEN       = colors.HexColor("#2e7d32")
_RED         = colors.HexColor("#c62828")
_AMBER       = colors.HexColor("#e65100")
_VIOLET      = colors.HexColor("#6d28d9")
_WHITE       = colors.white

_STATUS_COLORS = {
    "ok":        _GREEN,
    "variance":  _AMBER,
    "violation": _RED,
    "exempt":    _VIOLET,
    "na":        colors.grey,
}
_STATUS_LABELS = {
    "ok":        "OK",
    "variance":  "Variance",
    "violation": "Violation",
    "exempt":    "Exempt",
    "na":        "N/A",
}

# ---------------------------------------------------------------------------
# Style sheet
# ---------------------------------------------------------------------------

_BASE = getSampleStyleSheet()


def _style(**kwargs) -> ParagraphStyle:
    parent = _BASE["Normal"]
    return ParagraphStyle("_dynamic", parent=parent, **kwargs)


H1  = _style(fontSize=18, textColor=_BLUE,       spaceAfter=6,  spaceBefore=14, fontName="Helvetica-Bold")
H2  = _style(fontSize=13, textColor=_BLUE_LIGHT, spaceAfter=4,  spaceBefore=10, fontName="Helvetica-Bold")
H3  = _style(fontSize=10, textColor=_BLUE,       spaceAfter=2,  spaceBefore=6,  fontName="Helvetica-Bold")
BODY = _style(fontSize=9,  leading=13, spaceAfter=3)
SMALL= _style(fontSize=8,  textColor=colors.grey, leading=11)
BOLD = _style(fontSize=9,  fontName="Helvetica-Bold", leading=13)
WARN = _style(fontSize=9,  textColor=_AMBER, leading=13)
ERR  = _style(fontSize=9,  textColor=_RED,   leading=13)


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------

_TABLE_STYLE = TableStyle([
    ("BACKGROUND",  (0, 0), (-1, 0), _BLUE),
    ("TEXTCOLOR",   (0, 0), (-1, 0), _WHITE),
    ("FONTNAME",    (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE",    (0, 0), (-1, -1), 8),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _GREY_LIGHT]),
    ("GRID",        (0, 0), (-1, -1), 0.5, _GREY_BORDER),
    ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ("RIGHTPADDING",(0, 0), (-1, -1), 6),
    ("TOPPADDING",  (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING",(0,0), (-1, -1), 3),
    ("VALIGN",      (0, 0), (-1, -1), "TOP"),
])


def _kv_table(rows: list[tuple[str, str]], col_widths=(2.2*inch, 4.5*inch)) -> Table:
    data = [[Paragraph(k, BOLD), Paragraph(str(v), BODY)] for k, v in rows]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("FONTSIZE",    (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [_WHITE, _GREY_LIGHT]),
        ("GRID",        (0, 0), (-1, -1), 0.5, _GREY_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",(0, 0), (-1, -1), 6),
        ("TOPPADDING",  (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0,0), (-1, -1), 3),
    ]))
    return t


def _section_rule() -> HRFlowable:
    return HRFlowable(width="100%", thickness=1, color=_BLUE_LIGHT, spaceAfter=4)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _s1_site_summary(er: EnvelopeResult, zone_symbol: str, exc_num: Optional[int]) -> list:
    rows = [
        ("Zone symbol",      zone_symbol or "—"),
        ("Exception №",     str(exc_num) if exc_num else "None"),
        ("Lot width",        f"{er.lot_width_m:.2f} m"),
        ("Lot depth",        f"{er.lot_depth_m:.2f} m"),
        ("Lot area",         f"{er.lot_area_m2:.0f} m²"),
        ("By-law",          "Toronto Zoning By-law 569-2013"),
        ("OBC edition",      "Ontario Building Code 2024"),
    ]
    return [
        Paragraph("1  Site Summary", H2), _section_rule(),
        _kv_table(rows), Spacer(1, 8),
    ]


def _s2_zoning_rules(er: EnvelopeResult, zone_symbol: str = "") -> list:
    sb = er.setbacks_applied
    rows = [
        ("Front setback",    f"{sb.get('front', 0):.2f} m  (§10.20.40.10)"),
        ("Rear setback",     f"{sb.get('rear', 0):.2f} m  (§10.20.40.10)"),
        ("Side setback — left",  f"{sb.get('left', 0):.2f} m"),
        ("Side setback — right", f"{sb.get('right', 0):.2f} m"),
        ("Angular plane",    "Applied (§40.10.40.70)" if er.angular_plane_applied else "Not applicable"),
        ("Depth limit",
         f"{er.depth_limit_m:.1f} m  (§10.20.40.20)" if er.depth_limit_m < 1e6 else "Not applicable"),
    ]
    # Insert contextual front-yard note immediately after the front setback row
    base = (zone_symbol or "").split("(")[0].rstrip()
    if base.startswith(("R", "RD", "RS", "RT")):
        rows.insert(1, ("Front yard (note)", "CONTEXTUAL §10.20.40.10 — verify street average"))
    return [
        Paragraph("2  Zoning Rules Applied", H2), _section_rule(),
        _kv_table(rows), Spacer(1, 8),
    ]


def _s3_envelope(er: EnvelopeResult) -> list:
    env_area = er.envelope_2d.area
    coverage = env_area / er.lot_area_m2 * 100 if er.lot_area_m2 > 0 else 0
    rows = [
        ("Buildable envelope area", f"{env_area:.1f} m²"),
        ("Lot coverage",            f"{coverage:.1f}%"),
        ("Coordinate frame",        "Local CAD (x = street-parallel, y = depth)"),
        ("Projection",              "EPSG:2952 NAD83(CSRS) / MTM Zone 10"),
    ]
    elems: list = [
        Paragraph("3  Buildable Envelope", H2), _section_rule(),
        _kv_table(rows),
    ]
    if er.warnings:
        elems.append(Spacer(1, 4))
        for w in er.warnings:
            elems.append(Paragraph(f"⚠  {w}", WARN))
    elems.append(Spacer(1, 8))
    return elems


def _s4_typology(fit: FitResult) -> list:
    t = fit.typology
    rows = [
        ("Typology ID",      t.id),
        ("Label",            t.label),
        ("Units produced",   str(t.units_produced)),
        ("Stacking axis",    t.stacking_axis),
        ("Target storeys",   str(t.target_storeys)),
        ("Requires basement", "Yes" if t.requires_basement else "No"),
        ("Fit — frontage",   f"{fit.fit_frontage_m:.2f} m"),
        ("Fit — depth",      f"{fit.fit_depth_m:.2f} m"),
        ("Scale x / y",      f"{fit.scale_x:.3f} / {fit.scale_y:.3f}"),
        ("Gross floor area", f"{fit.gfa_m2:.1f} m²"),
    ]
    elems: list = [
        Paragraph("4  Typology Selected", H2), _section_rule(),
        _kv_table(rows),
    ]
    if fit.warnings:
        elems.append(Spacer(1, 4))
        for w in fit.warnings:
            elems.append(Paragraph(f"⚠  {w}", WARN))
    elems.append(Spacer(1, 8))
    return elems


def _s5_room_schedule(fit: FitResult) -> list:
    elems: list = [Paragraph("5  Room Schedule", H2), _section_rule()]

    # Group by storey
    storeys = sorted({pc.cell.storey for pc in fit.placed_cells})
    storey_labels = {-1: "Basement", 0: "Ground Floor", 1: "2nd Floor", 2: "3rd Floor", 3: "4th Floor"}

    for s in storeys:
        cells_here = [pc for pc in fit.placed_cells if pc.cell.storey == s]
        label = storey_labels.get(s, f"Floor {s + 1}")
        elems.append(Paragraph(label, H3))

        headers = ["Unit", "Role", "Width (m)", "Depth (m)", "Area (m²)"]
        data = [headers]
        for pc in sorted(cells_here, key=lambda p: (p.cell.unit_id, p.cell.role)):
            data.append([
                str(pc.cell.unit_id) if pc.cell.unit_id >= 0 else "common",
                pc.cell.role,
                f"{pc.width_m:.2f}",
                f"{pc.depth_m:.2f}",
                f"{pc.area_m2:.2f}",
            ])

        col_w = [0.6*inch, 1.4*inch, 1.1*inch, 1.1*inch, 1.1*inch]
        t = Table(data, colWidths=col_w)
        t.setStyle(_TABLE_STYLE)
        elems += [t, Spacer(1, 6)]

    return elems + [Spacer(1, 4)]


def _s6_obc(obc: OBCResult) -> list:
    status_text = "PASS" if obc.pass_ else "FAIL"
    status_col  = _GREEN if obc.pass_ else _RED
    status_style = _style(fontSize=12, fontName="Helvetica-Bold", textColor=status_col, spaceAfter=6)

    elems: list = [
        Paragraph("6  OBC Compliance Summary  (Part 9, 2024)", H2),
        _section_rule(),
        Paragraph(f"Overall result: {status_text}", status_style),
    ]

    if obc.violations:
        elems.append(Paragraph("Violations / warnings:", BOLD))
        for v in obc.violations:
            clr = ERR if v.severity == "error" else WARN
            prefix = "✗" if v.severity == "error" else "⚠"
            elems.append(Paragraph(
                f"{prefix}  [{v.code_ref}] Unit {v.unit_id} / Storey {v.storey} / "
                f"{v.cell_role}: {v.message}", clr,
            ))

    if obc.assumptions:
        elems.append(Spacer(1, 4))
        elems.append(Paragraph("Assumptions (cannot be verified from 2D plan):", BOLD))
        for a in obc.assumptions:
            elems.append(Paragraph(f"• {a}", SMALL))

    return elems + [Spacer(1, 8)]


def _fmt_val(v, unit: str = "") -> str:
    """Format a parameter value for display in the compliance table."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    s = f"{v:.2f}" if isinstance(v, float) else str(v)
    return f"{s} {unit}".strip() if unit else s


def _s7_compliance_audit(compliance_rows: list) -> list:
    """Section 7: full parameter compliance table, color-coded by status."""
    if not compliance_rows:
        return []

    counts: dict[str, int] = {}
    for row in compliance_rows:
        s = row.get("status", "na")
        counts[s] = counts.get(s, 0) + 1

    parts = []
    for s, lbl in [("violation", "Violations"), ("variance", "Variances"),
                   ("ok", "Compliant"), ("exempt", "Exempt")]:
        if counts.get(s, 0):
            parts.append(f"{counts[s]} {lbl}")

    elems: list = [
        Paragraph("7  Compliance Audit — By-law 569-2013 Parameters", H2),
        _section_rule(),
        Paragraph("  |  ".join(parts) or "All parameters within limits.", BOLD),
        Spacer(1, 6),
    ]

    col_w = [1.95*inch, 0.85*inch, 0.85*inch, 0.72*inch, 2.1*inch]
    headers = ["Parameter", "Proposed", "Limit", "Status", "Reference"]
    data: list = [headers]
    style_cmds: list = [
        ("BACKGROUND",    (0, 0), (-1, 0), _BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), _WHITE),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_WHITE, _GREY_LIGHT]),
        ("GRID",          (0, 0), (-1, -1), 0.5, _GREY_BORDER),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]

    for i, row in enumerate(compliance_rows, start=1):
        status     = row.get("status", "na")
        clr        = _STATUS_COLORS.get(status, colors.grey)
        lbl        = _STATUS_LABELS.get(status, "N/A")
        unit       = row.get("unit", "")
        proposed_s = _fmt_val(row.get("proposed"), unit)
        limit_s    = _fmt_val(row.get("limit"), unit)
        citation   = row.get("citation", "—")
        ref_str    = f"{citation} *" if row.get("amendment") else citation

        status_sty = _style(fontSize=7, fontName="Helvetica-Bold", textColor=clr, leading=9)
        data.append([
            Paragraph(row.get("parameter", "—"), SMALL),
            Paragraph(proposed_s, SMALL),
            Paragraph(limit_s, SMALL),
            Paragraph(lbl, status_sty),
            Paragraph(ref_str, SMALL),
        ])
        style_cmds.append(("TEXTCOLOR", (3, i), (3, i), clr))

    t = Table(data, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle(style_cmds))
    elems += [t, Spacer(1, 8)]

    if any(r.get("amendment") for r in compliance_rows):
        elems.append(Paragraph(
            "* Field affected by an unconsolidated amendment — verify current text with Toronto Building.",
            SMALL,
        ))
        elems.append(Spacer(1, 4))

    return elems


def _s8_citations(compliance_rows: Optional[list] = None) -> list:
    """Section 8: by-law & OBC citations; dynamically extends from compliance audit rows."""
    core_cites = [
        ("§10.20.40.10",  "Minimum setback requirements for RD zones — front, rear, and side yards."),
        ("§10.20.40.20",  "Maximum building depth measured from the required front yard setback line."),
        ("§10.20.40.30",  "Lot coverage maximums for residential zones."),
        ("§10.20.20.10",  "Minimum lot frontage requirements for residential zones."),
        ("§10.20.60.10",  "Maximum floor space index (FSI) for residential zones."),
        ("§10.20.80.10",  "Front yard landscaping requirements for residential lots."),
        ("§10.20.80.30",  "Permeable surface — minimum 30% of non-building lot area."),
        ("§40.10.40.70",  "Angular plane regulations for CR and CRE zones."),
        ("§150.7.80.10",  "Garden suite maximum GFA — lesser of 60 m2 or 40% of principal dwelling."),
        ("§150.7.80.20",  "Garden suite maximum height — 6.0 m ridge height."),
        ("§150.8.60.30",  "Laneway suite 45 degree angular plane rule — 4.0 m start height."),
        ("§200.15.10.10", "Parking standards — required spaces per dwelling unit by zone type."),
        ("§200.15.10.30", "Bicycle parking requirements — long-term and short-term ratios."),
        ("§220.5.10",     "Toronto Green Standard (TGS) Tier 1 — mandatory sustainable design measures."),
        ("OBC §9.8.3.1",  "Minimum room areas — bedrooms 7 m2, master bedroom 10 m2, living 13.5 m2."),
        ("OBC §9.8.3.2",  "Minimum room dimensions — no habitable room dimension < 2.1 m."),
        ("OBC §9.7.4",    "Egress window requirements — min opening 0.35 m2, min dimension 380 mm."),
        ("OBC §9.8.4.1",  "Stair minimum clear width — 0.9 m."),
        ("OBC §9.8.3.4",  "Minimum ceiling heights — 2.4 m above grade, 1.95 m in basement."),
    ]

    all_cites: list = list(core_cites)
    if compliance_rows:
        seen = {c for c, _ in core_cites}
        for row in compliance_rows:
            cit = row.get("citation", "")
            if cit and cit not in seen:
                seen.add(cit)
                all_cites.append((cit, row.get("parameter", "")))

    bylaw = sorted(
        [(c, d) for c, d in all_cites if not c.startswith("OBC")],
        key=lambda x: x[0],
    )
    obc = [(c, d) for c, d in all_cites if c.startswith("OBC")]
    sorted_cites = bylaw + obc

    elems: list = [Paragraph("8  By-law & Code Citations", H2), _section_rule()]
    headers = ["Reference", "Description / Parameter"]
    data = [headers] + [[c, d] for c, d in sorted_cites]
    col_w = [1.5*inch, 5.2*inch]
    t = Table(data, colWidths=col_w)
    t.setStyle(_TABLE_STYLE)
    elems += [t, Spacer(1, 8)]
    return elems


def _s9_assumptions() -> list:
    items = [
        "All coordinates are in the local CAD frame derived from the PostGIS parcel polygon "
        "(EPSG:4326 → EPSG:2952 MTM-10 → local frame). Metric (metres) throughout.",
        "Storey heights assumed: basement 2.4 m, ground floor 3.0 m, upper floors 2.7 m. "
        "Verify final ceiling heights in the structural drawings.",
        "Lot polygon from PostGIS may not match the registered survey. "
        "Confirm legal lot dimensions before permit application.",
        "Setbacks derived from the base zone symbol and any exception override extracted via LLM. "
        "Verify with Toronto Building for overlays and site-specific agreements.",
        "Front yard setback for R-series zones is contextual (§10.20.40.10) and equals the "
        "average of the two flanking properties on the same block face. The value shown in "
        "this report is the zone default approximation and must be verified against surveyed "
        "neighbouring front yards before any permit application.",
        "Angular plane analysis is preliminary. A registered architect must confirm compliance for "
        "permit submission.",
        "This preliminary layout does not constitute approved construction documents and is for "
        "design feasibility assessment only.",
        "No geotechnical, structural, mechanical, or electrical engineering has been performed.",
        "Accessibility requirements (OBC Part 4, AODA) have not been assessed.",
        "Heritage overlay, floodplain, and environmental designations are not checked here.",
    ]
    elems: list = [Paragraph("9  Assumptions & Limitations", H2), _section_rule()]
    for item in items:
        elems.append(Paragraph(f"• {item}", BODY))
    return elems + [Spacer(1, 8)]


def _s9_disclosure() -> list:
    text = (
        "This document was generated automatically by <b>PackGen</b>, an AI-assisted preliminary "
        "floor-plan tool built on the Toronto Zoning By-law 569-2013 RAG system. "
        "The layout, OBC compliance check, and all zoning parameters were computed "
        "algorithmically from public GIS and by-law data without human review.<br/><br/>"
        "This is a <b>preliminary feasibility document only</b>. It is not a set of approved "
        "construction documents, a building permit, or professional engineering or architectural "
        "advice. Before any design, construction, or permit application, engage a licensed "
        "architect or professional engineer registered in Ontario.<br/><br/>"
        "AI-generated content may contain errors. All numeric values must be independently "
        "verified against the current Toronto Zoning By-law, the Ontario Building Code (2024), "
        "and applicable overlay and exception schedules.<br/><br/>"
        "Generated: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )
    return [
        Paragraph("10  AI Disclosure & Professional Responsibility", H2),
        _section_rule(),
        Paragraph(text, BODY),
        Spacer(1, 8),
    ]


# ---------------------------------------------------------------------------
# Header / footer callbacks
# ---------------------------------------------------------------------------

def _on_page(canvas, doc):
    canvas.saveState()
    # Header bar
    canvas.setFillColor(_BLUE)
    canvas.rect(0.5*inch, LETTER[1] - 0.6*inch, 7.5*inch, 0.35*inch, fill=1, stroke=0)
    canvas.setFillColor(_WHITE)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(0.6*inch, LETTER[1] - 0.45*inch, "PackGen — Preliminary Floor Plan Report")
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(8.0*inch, LETTER[1] - 0.45*inch, "Toronto Zoning By-law 569-2013")
    # Footer
    canvas.setFillColor(colors.grey)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(0.5*inch, 0.4*inch,
                      "PRELIMINARY — NOT FOR CONSTRUCTION — AI-generated, unreviewed by a licensed professional")
    canvas.drawRightString(8.0*inch, 0.4*inch, f"Page {doc.page}")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_pdf(
    envelope_result: EnvelopeResult,
    fit: FitResult,
    obc: OBCResult,
    *,
    zone_symbol: str = "",
    exception_number: Optional[int] = None,
    brief_summary: str = "",
    compliance_rows: Optional[list] = None,
) -> bytes:
    """Assemble and return the report as PDF bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.5*inch, rightMargin=0.5*inch,
        topMargin=0.85*inch, bottomMargin=0.7*inch,
    )

    # Generate AI design narrative (best-effort, 8 s timeout)
    narrative = _generate_narrative(
        typology_name=fit.typology.label,
        unit_count=fit.typology.units_produced,
        zone_symbol=zone_symbol or "—",
        brief_summary=brief_summary,
    )

    story: list = []

    # Cover
    story += [
        Spacer(1, 0.3*inch),
        Paragraph("Preliminary Floor Plan Report", H1),
        Paragraph(
            f"Zone: <b>{zone_symbol or '—'}</b>  |  "
            f"Typology: <b>{fit.typology.label}</b>  |  "
            f"Units: <b>{fit.typology.units_produced}</b>  |  "
            f"GFA: <b>{fit.gfa_m2:.0f} m²</b>",
            BODY,
        ),
        Spacer(1, 4),
        HRFlowable(width="100%", thickness=2, color=_BLUE),
        Spacer(1, 8),
        Paragraph("Design Intent", H3),
        Paragraph(narrative, BODY),
        Spacer(1, 10),
    ]

    story += _s1_site_summary(envelope_result, zone_symbol, exception_number)
    story += _s2_zoning_rules(envelope_result, zone_symbol)
    story += _s3_envelope(envelope_result)
    story += _s4_typology(fit)
    story.append(PageBreak())
    story += _s5_room_schedule(fit)
    story += _s6_obc(obc)
    story.append(PageBreak())
    if compliance_rows:
        story += _s7_compliance_audit(compliance_rows)
        story.append(PageBreak())
    story += _s8_citations(compliance_rows)
    story += _s9_assumptions()
    story += _s9_disclosure()

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buf.getvalue()
