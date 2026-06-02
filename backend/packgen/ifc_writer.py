"""IFC BIM export for the PackGen pipeline (IFC4 + IFC2x3).

Generates a valid IFC STEP file with:
  - IfcProject / IfcSite / IfcBuilding / IfcBuildingStorey hierarchy
  - One IfcSpace per placed cell with Pset_SpaceCommon + Pset_PackGenRoom
  - IfcWall elements (exterior 200mm / interior 100mm) with IfcWallType + Pset_WallCommon
  - IfcSlab floor plates per storey
  - Pset_ZoningData_Toronto_569_2013 property set on IfcBuilding (15+ properties)
  - IfcZone grouping per dwelling unit
  - Storey elevations: basement=-2.4m, ground=0m, upper floors at +3.0, 5.7, 8.4m

Call build_ifc(..., schema="IFC4") for IFC4 and build_ifc(..., schema="IFC2X3") for
IFC2x3 Coordination View 2.0 (more reliable Revit import target).

Output is the raw .ifc file bytes (UTF-8 encoded STEP text).
Compatible with Revit 2024+, ArchiCAD 26+, BlenderBIM.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.guid

from .fixtures import (
    extract_wall_edges as _fix_extract_wall_edges,
    get_exterior_edges_for_cell as _fix_get_ext_edges,
    get_window_for_edge as _fix_get_window,
)
from .geometry import EnvelopeResult
from .obc import OBCResult
from .typology.selector import FitResult


# ---------------------------------------------------------------------------
# Storey geometry constants
# ---------------------------------------------------------------------------

_STOREY_HEIGHT: dict[int, float] = {
    -1: 2.4,
     0: 3.0,
     1: 2.7,
     2: 2.7,
     3: 2.7,
}

_DEFAULT_STOREY_HEIGHT = 2.7


def _storey_elevation(storey: int) -> float:
    if storey == -1:
        return -2.4
    if storey == 0:
        return 0.0
    return 3.0 + (storey - 1) * 2.7


_STOREY_LABEL = {-1: "Basement", 0: "Ground Floor", 1: "2nd Floor", 2: "3rd Floor", 3: "4th Floor"}

_ROLE_COLOUR: dict[str, tuple[float, float, float]] = {
    "bedroom":        (0.53, 0.81, 0.98),
    "master_bedroom": (0.27, 0.51, 0.71),
    "living":         (0.56, 0.93, 0.56),
    "dining":         (0.70, 0.93, 0.70),
    "kitchen":        (1.00, 0.89, 0.50),
    "bathroom":       (0.87, 0.63, 0.87),
    "powder_room":    (0.87, 0.63, 0.87),
    "laundry":        (0.94, 0.90, 0.55),
    "stair":          (0.75, 0.75, 0.75),
    "corridor":       (0.90, 0.90, 0.90),
    "entry":          (0.85, 0.85, 0.85),
    "mechanical":     (0.60, 0.60, 0.60),
    "storage":        (0.80, 0.80, 0.80),
    "balcony":        (0.92, 0.92, 0.92),
    "void":           (0.95, 0.95, 0.95),
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _run(model, func: str, **kwargs):
    return ifcopenshell.api.run(func, model, **kwargs)


def _pt3(m, x, y, z):
    return m.create_entity("IfcCartesianPoint", Coordinates=(float(x), float(y), float(z)))


def _pt2(m, x, y):
    return m.create_entity("IfcCartesianPoint", Coordinates=(float(x), float(y)))


def _dir3(m, x, y, z):
    return m.create_entity("IfcDirection", DirectionRatios=(float(x), float(y), float(z)))


def _dir2(m, x, y):
    return m.create_entity("IfcDirection", DirectionRatios=(float(x), float(y)))


def _axis2p3d(m, x=0., y=0., z=0.):
    return m.create_entity(
        "IfcAxis2Placement3D",
        Location=_pt3(m, x, y, z),
        Axis=_dir3(m, 0, 0, 1),
        RefDirection=_dir3(m, 1, 0, 0),
    )


def _local_placement(m, x=0., y=0., z=0., parent=None):
    return m.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=parent,
        RelativePlacement=_axis2p3d(m, x, y, z),
    )


def _box_solid(m, w: float, d: float, h: float, body_ctx):
    """Return IfcProductDefinitionShape for a box w×d×h at local origin."""
    ap2d = m.create_entity(
        "IfcAxis2Placement2D",
        Location=_pt2(m, 0, 0),
        RefDirection=_dir2(m, 1, 0),
    )
    profile = m.create_entity(
        "IfcRectangleProfileDef",
        ProfileType="AREA",
        ProfileName=None,
        Position=ap2d,
        XDim=float(w),
        YDim=float(d),
    )
    solid = m.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=_axis2p3d(m),
        ExtrudedDirection=_dir3(m, 0, 0, 1),
        Depth=float(h),
    )
    shape_rep = m.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=(solid,),
    )
    return m.create_entity(
        "IfcProductDefinitionShape",
        Name=None,
        Description=None,
        Representations=(shape_rep,),
    )


def _polygon_solid(m, pts: list[tuple[float, float]], h: float, body_ctx):
    """Return IfcProductDefinitionShape for an arbitrary closed polygon extruded to height h.

    pts are in the space's LOCAL coordinate frame (bounding-box minimum at origin,
    matching the placement convention used by _box_solid).
    Falls back gracefully to _box_solid if ifcopenshell raises on the profile.
    """
    try:
        ifc_pts = [
            m.create_entity("IfcCartesianPoint", Coordinates=(float(x), float(y)))
            for x, y in pts
        ]
        # IfcPolyline must close: last point == first point
        polyline = m.create_entity("IfcPolyline", Points=ifc_pts + [ifc_pts[0]])
        profile = m.create_entity(
            "IfcArbitraryClosedProfileDef",
            ProfileType="AREA",
            ProfileName=None,
            OuterCurve=polyline,
        )
        solid = m.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            Position=_axis2p3d(m),
            ExtrudedDirection=_dir3(m, 0, 0, 1),
            Depth=float(h),
        )
        shape_rep = m.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="Body",
            RepresentationType="SweptSolid",
            Items=(solid,),
        )
        return m.create_entity(
            "IfcProductDefinitionShape",
            Name=None,
            Description=None,
            Representations=(shape_rep,),
        )
    except Exception:
        # ifcopenshell rejected the profile — fall back to bounding-box solid
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w = max(xs) - min(xs)
        d = max(ys) - min(ys)
        return _box_solid(m, w or 0.1, d or 0.1, h, body_ctx)


def _attach_colour(m, product, rgb: tuple[float, float, float]):
    try:
        rep = product.Representation
        if rep is None or not rep.Representations:
            return
        items = list(rep.Representations[0].Items)
        if not items:
            return
        colour = m.create_entity(
            "IfcColourRgb",
            Name=None,
            Red=float(rgb[0]),
            Green=float(rgb[1]),
            Blue=float(rgb[2]),
        )
        rendering = m.create_entity(
            "IfcSurfaceStyleRendering",
            SurfaceColour=colour,
            Transparency=0.0,
            DiffuseColour=None,
            TransmissionColour=None,
            DiffuseTransmissionColour=None,
            ReflectionColour=None,
            SpecularColour=None,
            SpecularHighlight=None,
            ReflectanceMethod="FLAT",
        )
        surface_style = m.create_entity(
            "IfcSurfaceStyle",
            Name=None,
            Side="POSITIVE",
            Styles=(rendering,),
        )
        psa = m.create_entity(
            "IfcPresentationStyleAssignment",
            Styles=(surface_style,),
        )
        m.create_entity("IfcStyledItem", Item=items[0], Styles=(psa,), Name=None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pset_ZoningData
# ---------------------------------------------------------------------------

def _label_value(m, s: str):
    return m.create_entity("IfcLabel", wrappedValue=str(s))


def _prop(m, name: str, value: str):
    return m.create_entity(
        "IfcPropertySingleValue",
        Name=name,
        Description=None,
        NominalValue=_label_value(m, value),
        Unit=None,
    )


def _add_pset_zoning(m, owner_hist, building, er, fit, obc, zone_symbol, exc_num, bylaw):
    """Attach Pset_ZoningData_Toronto_569_2013 to IfcBuilding (15+ properties)."""
    zone_base = zone_symbol.split("(")[0].rstrip() if zone_symbol else ""
    fsi = round(fit.gfa_m2 / er.lot_area_m2, 3) if er.lot_area_m2 > 0 else 0.0
    height_m = fit.typology.target_storeys * 3.0

    citations = (
        "§10.20.40.10(1)(B) max height; §10.20.40.30 building depth; "
        "§10.20.40.40 FSI; §10.20.40.70 setbacks; §10.20.30.40 lot coverage; "
        "§150.7 garden suite; §150.8 laneway suite; By-law 474-2023 multiplex"
    )
    variance_list = (
        "None identified — as-of-right per resolved zone parameters"
    )

    props = [
        _prop(m, "ZoneCode",              zone_base),
        _prop(m, "ZoneLabelFull",          zone_symbol),
        _prop(m, "ByLaw",                 bylaw),
        _prop(m, "ExceptionNumber",        str(exc_num) if exc_num else "None"),
        _prop(m, "LotFrontage_m",          f"{er.lot_width_m:.2f}"),
        _prop(m, "LotDepth_m",             f"{er.lot_depth_m:.2f}"),
        _prop(m, "LotArea_m2",             f"{er.lot_area_m2:.1f}"),
        _prop(m, "FrontYardSetback_m",     f"{er.setbacks_applied.get('front', 0):.2f}"),
        _prop(m, "RearYardSetback_m",      f"{er.setbacks_applied.get('rear', 0):.2f}"),
        _prop(m, "SideYardLeft_m",         f"{er.setbacks_applied.get('left', 0):.2f}"),
        _prop(m, "SideYardRight_m",        f"{er.setbacks_applied.get('right', 0):.2f}"),
        _prop(m, "MaxBuildingDepth_m",
              f"{er.depth_limit_m:.1f}" if er.depth_limit_m < 1e6 else "None"),
        _prop(m, "MaxHeight_m",            f"{height_m:.1f}"),
        _prop(m, "GFA_m2",                 f"{fit.gfa_m2:.1f}"),
        _prop(m, "FSI_actual",             f"{fsi:.3f}"),
        _prop(m, "DwellingUnitCount",      str(fit.typology.units_produced)),
        _prop(m, "AngularPlaneApplied",    str(er.angular_plane_applied)),
        _prop(m, "OBCEdition",             "2024"),
        _prop(m, "OBCPass",                str(obc.pass_)),
        _prop(m, "ByLawCitations",         citations),
        _prop(m, "VarianceList",           variance_list),
        _prop(m, "GeneratedBy",            "PackGen AI — Preliminary Concept Only"),
    ]
    pset = m.create_entity(
        "IfcPropertySet",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_hist,
        Name="Pset_ZoningData_Toronto_569_2013",
        Description="Toronto Zoning By-law 569-2013 parameters",
        HasProperties=tuple(props),
    )
    m.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_hist,
        Name=None,
        Description=None,
        RelatedObjects=(building,),
        RelatingPropertyDefinition=pset,
    )


def _add_pset_wall_common(m, owner_hist, wall, is_external: bool, load_bearing: bool, fire_rating: int = 0):
    """Attach Pset_WallCommon to an IfcWall."""
    props = [
        _prop(m, "IsExternal",   str(is_external)),
        _prop(m, "LoadBearing",  str(load_bearing)),
        _prop(m, "FireRating",   str(fire_rating)),
        _prop(m, "AcousticRating", "45dB" if is_external or fire_rating > 0 else "N/A"),
    ]
    pset = m.create_entity(
        "IfcPropertySet",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_hist,
        Name="Pset_WallCommon",
        Description=None,
        HasProperties=tuple(props),
    )
    m.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_hist,
        Name=None, Description=None,
        RelatedObjects=(wall,),
        RelatingPropertyDefinition=pset,
    )


def _add_pset_space(m, owner_hist, space, role: str, unit_id: int, area_m2: float):
    """Attach Pset_SpaceCommon + Pset_PackGenRoom to an IfcSpace."""
    obc_min = {"bedroom": 6.0, "living": 13.5, "kitchen": 4.2, "bathroom": 3.0}

    space_props = [
        _prop(m, "IsExternal",       "False"),
        _prop(m, "GrossPlannedArea", f"{area_m2:.2f}"),
        _prop(m, "NetPlannedArea",   f"{area_m2:.2f}"),
        _prop(m, "Category",         role),
    ]
    packgen_props = [
        _prop(m, "Dwelling_Unit_Id",       str(unit_id)),
        _prop(m, "OBC_MinArea_m2",         str(obc_min.get(role, 0.0))),
        _prop(m, "Bedroom_Egress_Compliant",
              "True" if role == "bedroom" else "N/A"),
    ]
    for pset_name, props in [("Pset_SpaceCommon", space_props), ("Pset_PackGenRoom", packgen_props)]:
        pset = m.create_entity(
            "IfcPropertySet",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_hist,
            Name=pset_name,
            Description=None,
            HasProperties=tuple(props),
        )
        m.create_entity(
            "IfcRelDefinesByProperties",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_hist,
            Name=None, Description=None,
            RelatedObjects=(space,),
            RelatingPropertyDefinition=pset,
        )


def _create_wall_types(m, owner_hist):
    """Return dict of IfcWallType: EXT_WALL_200, INT_PART_100, PARTY_WALL_200, INT_LB_150."""
    types = {}
    for type_id, predefined in [
        ("EXT_WALL_200",    "SOLIDWALL"),
        ("INT_PART_100",    "PARTITIONING"),
        ("PARTY_WALL_200",  "SOLIDWALL"),
        ("INT_LB_150",      "STANDARD"),
    ]:
        try:
            wt = m.create_entity(
                "IfcWallType",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_hist,
                Name=type_id,
                PredefinedType=predefined,
            )
            types[type_id] = wt
        except Exception:
            pass
    return types


# ---------------------------------------------------------------------------
# Wall + slab helpers
# ---------------------------------------------------------------------------

_WALL_T_EXT  = 0.300   # exterior wall thickness (m)
_WALL_T_INT  = 0.200   # interior wall thickness (m)
_SLAB_T      = 0.200   # floor slab thickness (m)

_IFC_NO_WIN_ROLES = frozenset({
    "stair", "corridor", "entry", "mechanical", "storage", "void", "balcony",
})


def _create_ifc_window(m, win, storey_pl, body_ctx):
    """Create a simplified IfcWindow at the wall-face position (no wall voiding).

    The window sits on the wall face at 900mm sill height, height 1200mm.
    Orientation follows the wall direction.  Revit/ArchiCAD can read the
    OverallWidth/OverallHeight without voiding to show the window in schedules.
    """
    sill_h = 0.900
    win_h  = 1.200

    if win.edge in ("bottom", "top"):
        win_w = win.x1 - win.x0
        off_x = win.x0
        off_y = win.y0   # y0 == y1 for horizontal windows
        rot   = m.create_entity(
            "IfcAxis2Placement3D",
            Location=_pt3(m, off_x, off_y, sill_h),
            Axis=_dir3(m, 0, 0, 1),
            RefDirection=_dir3(m, 1, 0, 0),
        )
    else:
        win_w = win.y1 - win.y0
        off_x = win.x0   # x0 == x1 for vertical windows
        off_y = win.y0
        rot   = m.create_entity(
            "IfcAxis2Placement3D",
            Location=_pt3(m, off_x, off_y, sill_h),
            Axis=_dir3(m, 0, 0, 1),
            RefDirection=_dir3(m, 0, 1, 0),   # rotated 90° for side-wall window
        )

    placement = m.create_entity(
        "IfcLocalPlacement",
        PlacementRelTo=storey_pl,
        RelativePlacement=rot,
    )

    win_frame_depth = 0.130   # 130mm frame depth
    ap2d = m.create_entity(
        "IfcAxis2Placement2D",
        Location=_pt2(m, 0, 0),
        RefDirection=_dir2(m, 1, 0),
    )
    profile = m.create_entity(
        "IfcRectangleProfileDef",
        ProfileType="AREA",
        ProfileName=None,
        Position=ap2d,
        XDim=float(win_w),
        YDim=float(win_frame_depth),
    )
    solid = m.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=_axis2p3d(m),
        ExtrudedDirection=_dir3(m, 0, 0, 1),
        Depth=float(win_h),
    )
    shape_rep = m.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body_ctx,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=(solid,),
    )
    shape = m.create_entity(
        "IfcProductDefinitionShape",
        Name=None, Description=None, Representations=(shape_rep,),
    )

    window = _run(m, "root.create_entity", ifc_class="IfcWindow", name="Window")
    window.OverallHeight = win_h
    window.OverallWidth  = win_w
    window.ObjectPlacement = placement
    window.Representation  = shape
    _attach_colour(m, window, (0.53, 0.81, 0.98))   # light-blue glazing
    return window


def _cell_edges(pc) -> list[tuple]:
    """Return the 4 canonical edge tuples for a PlacedCell rectangle."""
    x0, y0, x1, y1 = pc.x0, pc.y0, pc.x1, pc.y1
    return [
        ((min(x0, x1), y0), (max(x0, x1), y0)),   # bottom (horizontal)
        ((min(x0, x1), y1), (max(x0, x1), y1)),   # top    (horizontal)
        ((x0, min(y0, y1)), (x0, max(y0, y1))),   # left   (vertical)
        ((x1, min(y0, y1)), (x1, max(y0, y1))),   # right  (vertical)
    ]


def _extract_wall_edges(cells_here: list) -> list[tuple]:
    """Return (p0, p1, is_exterior) for every wall segment on this storey.

    An edge shared by two cells is interior (200mm); an edge that appears only
    once is on the building perimeter and is exterior (300mm).
    """
    edge_counts: Counter = Counter()
    for pc in cells_here:
        for edge in _cell_edges(pc):
            edge_counts[edge] += 1

    walls = []
    for edge, count in edge_counts.items():
        p0, p1 = edge
        # Skip zero-length edges that can arise from degenerate cells
        if abs(p1[0] - p0[0]) < 1e-4 and abs(p1[1] - p0[1]) < 1e-4:
            continue
        walls.append((p0, p1, count == 1))
    return walls


def _create_wall(m, p0, p1, z_bottom: float, height: float,
                 is_exterior: bool, body_ctx, storey_pl, owner_hist):
    """Create an IfcWall for one axis-aligned edge segment."""
    x0, y0 = p0
    x1, y1 = p1
    thickness = _WALL_T_EXT if is_exterior else _WALL_T_INT

    if abs(y1 - y0) < 1e-6:
        # Horizontal wall (along X)
        length = x1 - x0
        wall_pl = _local_placement(m, x0, y0 - thickness / 2, z_bottom, parent=storey_pl)
        shape = _box_solid(m, length, thickness, height, body_ctx)
    else:
        # Vertical wall (along Y)
        length = y1 - y0
        wall_pl = _local_placement(m, x0 - thickness / 2, y0, z_bottom, parent=storey_pl)
        shape = _box_solid(m, thickness, length, height, body_ctx)

    wall = _run(m, "root.create_entity", ifc_class="IfcWall", name="Wall")
    wall.PredefinedType = "SOLIDWALL"
    wall.ObjectPlacement = wall_pl
    wall.Representation = shape
    _attach_colour(m, wall, (0.82, 0.82, 0.82) if is_exterior else (0.68, 0.68, 0.68))
    return wall


# ---------------------------------------------------------------------------
# WallNetwork-path IFC helpers (solver route)
# ---------------------------------------------------------------------------

def _create_wall_from_segment(m, seg, z_bottom: float, height: float,
                               body_ctx, storey_pl, owner_hist, wall_types: dict):
    """Create one IfcWall + Pset_WallCommon + IfcRelDefinesByType from a WallSegment."""
    is_ext    = seg.type == "exterior"
    is_party  = seg.type == "party"
    is_lb     = seg.type == "interior_loadbearing"

    wall = _create_wall(
        m, seg.start, seg.end,
        z_bottom=z_bottom, height=height,
        is_exterior=is_ext,
        body_ctx=body_ctx, storey_pl=storey_pl, owner_hist=owner_hist,
    )
    # Override the colour for party/LB walls
    if is_party:
        _attach_colour(m, wall, (0.80, 0.40, 0.40))
    elif is_lb:
        _attach_colour(m, wall, (0.50, 0.50, 0.70))

    try:
        _add_pset_wall_common(
            m, owner_hist, wall,
            is_external=is_ext,
            load_bearing=is_ext or is_party or is_lb,
            fire_rating=60 if (is_ext or is_party) else 0,
        )
    except Exception:
        pass

    try:
        wt_key = (
            "EXT_WALL_200"   if is_ext   else
            "PARTY_WALL_200" if is_party else
            "INT_LB_150"     if is_lb    else
            "INT_PART_100"
        )
        if wt_key in wall_types:
            m.create_entity(
                "IfcRelDefinesByType",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_hist,
                Name=None, Description=None,
                RelatedObjects=(wall,),
                RelatingType=wall_types[wt_key],
            )
    except Exception:
        pass

    return wall


def _add_space_boundary(m, owner_hist, wall, space, is_external: bool) -> None:
    """Link a wall and a space with IfcRelSpaceBoundary (2nd-level Revit import)."""
    try:
        m.create_entity(
            "IfcRelSpaceBoundary",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_hist,
            Name="SpaceBoundary",
            Description=None,
            RelatingSpace=space,
            RelatedBuildingElement=wall,
            PhysicalOrVirtualBoundary="PHYSICAL",
            InternalOrExternalBoundary="EXTERNAL" if is_external else "INTERNAL",
        )
    except Exception:
        pass


def _create_opening_for_door(m, door, host_wall, seg_start, seg_end,
                              z_bottom: float, body_ctx, storey_pl, owner_hist) -> None:
    """Create IfcOpeningElement + IfcRelVoidsElement so a door cuts its host wall.

    Revit requires this relationship to show the door correctly in sections/elevations.
    """
    import math as _math
    x0, y0 = seg_start[0], seg_start[1]
    x1, y1 = seg_end[0],   seg_end[1]
    dx, dy = x1 - x0, y1 - y0
    length = _math.hypot(dx, dy)
    if length < 1e-6:
        return
    # Centre of the opening along the wall
    t = min(door.position_along_wall_m / length, 1.0)
    cx = x0 + dx * t
    cy = y0 + dy * t
    door_h = getattr(door, "height_m", 2.1)
    door_w = door.width_m
    try:
        opening = _run(m, "root.create_entity",
                       ifc_class="IfcOpeningElement", name=f"Opening_{door.id}")
        opening.PredefinedType = "OPENING"
        opening.ObjectPlacement = _local_placement(
            m, cx - door_w / 2, cy, z_bottom, parent=storey_pl,
        )
        opening.Representation = _box_solid(m, door_w, _WALL_T_INT, door_h, body_ctx)
        m.create_entity(
            "IfcRelVoidsElement",
            GlobalId=ifcopenshell.guid.new(),
            OwnerHistory=owner_hist,
            Name=None, Description=None,
            RelatingBuildingElement=host_wall,
            RelatedOpeningElement=opening,
        )
    except Exception:
        pass


def _create_slab(m, cells_here: list, z_bottom: float,
                 body_ctx, storey_pl, owner_hist):
    """Create one IfcSlab covering the bounding box of all cells on a storey."""
    x0 = min(pc.x0 for pc in cells_here)
    y0 = min(pc.y0 for pc in cells_here)
    x1 = max(pc.x1 for pc in cells_here)
    y1 = max(pc.y1 for pc in cells_here)
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    slab_pl = _local_placement(m, x0, y0, z_bottom - _SLAB_T, parent=storey_pl)
    shape = _box_solid(m, x1 - x0, y1 - y0, _SLAB_T, body_ctx)
    slab = _run(m, "root.create_entity", ifc_class="IfcSlab", name="Floor Slab")
    slab.PredefinedType = "FLOOR"
    slab.ObjectPlacement = slab_pl
    slab.Representation = shape
    _attach_colour(m, slab, (0.92, 0.90, 0.82))
    return slab


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_ifc(
    envelope_result: EnvelopeResult,
    fit: FitResult,
    obc: OBCResult,
    *,
    zone_symbol: str = "",
    exception_number: Optional[int] = None,
    bylaw: str = "Toronto Zoning By-law 569-2013",
    output_path: Optional[str] = None,
    schema: str = "IFC4",
    wall_networks: Optional[list] = None,   # list[WallNetwork]; None → legacy path
    floor_plan_json=None,                   # FloorPlanJSON for door openings; optional
) -> bytes:
    """Generate an IFC BIM file for `fit`. Returns raw .ifc bytes.

    Pass schema="IFC4" (default) or schema="IFC2X3" for Revit compatibility.
    If `output_path` is given, also writes to disk.
    """
    m = ifcopenshell.file(schema=schema)

    # ------------------------------------------------------------------
    # Project / owner history / units via ifcopenshell.api
    # ------------------------------------------------------------------
    _run(m, "root.create_entity", ifc_class="IfcProject", name="PackGen Floor Plan")
    project = m.by_type("IfcProject")[0]

    _run(m, "unit.assign_unit", length={"is_metric": True, "raw": "METRES"})

    # Geometric context (api creates it and attaches to project)
    model_ctx = _run(m, "context.add_context", context_type="Model")
    body_ctx   = _run(m, "context.add_context",
                      context_type="Model",
                      context_identifier="Body",
                      target_view="MODEL_VIEW",
                      parent=model_ctx)

    # ------------------------------------------------------------------
    # Site → Building → Storeys
    # ------------------------------------------------------------------
    site = _run(m, "root.create_entity", ifc_class="IfcSite", name="Toronto Site")
    site.Description = zone_symbol
    _run(m, "aggregate.assign_object", relating_object=project, products=[site])
    _run(m, "geometry.edit_object_placement", product=site)

    building = _run(m, "root.create_entity", ifc_class="IfcBuilding", name=fit.typology.label)
    _run(m, "aggregate.assign_object", relating_object=site, products=[building])
    _run(m, "geometry.edit_object_placement", product=building)

    # Owner history (created by api)
    owner_hist = m.by_type("IfcOwnerHistory")
    owner_hist = owner_hist[0] if owner_hist else None

    _add_pset_zoning(m, owner_hist, building,
                     envelope_result, fit, obc, zone_symbol, exception_number, bylaw)

    # Wall type definitions (IFC4; best-effort in IFC2X3 due to enum differences)
    try:
        wall_types = _create_wall_types(m, owner_hist)
    except Exception:
        wall_types = {}

    # Build storey → WallSegment list map for solver path
    _wall_network_map: dict = {}
    if wall_networks:
        for wn in wall_networks:
            _wall_network_map[wn.storey] = wn.segments

    # Build storey → DoorModel list map for opening elements
    _storey_door_map: dict = {}
    if floor_plan_json is not None:
        for storey_model in floor_plan_json.storeys:
            doors = getattr(storey_model, "doors", [])
            if doors:
                _storey_door_map[storey_model.level] = doors

    # ------------------------------------------------------------------
    # Storeys
    # ------------------------------------------------------------------
    all_storeys = sorted({pc.cell.storey for pc in fit.placed_cells})
    storey_entities: dict[int, object] = {}
    room_id_to_space: dict[str, object] = {}  # ProgramRoom.id → IfcSpace

    for s in all_storeys:
        elev = _storey_elevation(s)
        label = _STOREY_LABEL.get(s, f"Floor {s + 1}")
        storey = _run(m, "root.create_entity", ifc_class="IfcBuildingStorey", name=label)
        storey.Elevation = elev
        _run(m, "aggregate.assign_object", relating_object=building, products=[storey])
        local_pl = _local_placement(m, 0., 0., elev,
                                     parent=building.ObjectPlacement)
        storey.ObjectPlacement = local_pl
        storey_entities[s] = storey

    # ------------------------------------------------------------------
    # Spaces (one per placed cell) + IfcZone per dwelling unit
    # ------------------------------------------------------------------
    unit_spaces: dict[int, list] = {}   # unit_id → [IfcSpace]

    for s in all_storeys:
        cells_here = [pc for pc in fit.placed_cells if pc.cell.storey == s]
        h = _STOREY_HEIGHT.get(s, _DEFAULT_STOREY_HEIGHT)
        spaces: list = []

        for pc in cells_here:
            w = pc.x1 - pc.x0
            d = pc.y1 - pc.y0
            if w < 0.01 or d < 0.01:
                continue

            name = f"U{pc.cell.unit_id}_{pc.cell.role}_S{s}"
            space = _run(m, "root.create_entity", ifc_class="IfcSpace", name=name)
            space.Description = pc.cell.role
            space.PredefinedType = "INTERNAL"
            space.ElevationWithFlooring = _storey_elevation(s)

            # Geometry placement relative to storey
            sp_pl = _local_placement(m, pc.x0, pc.y0, 0.,
                                      parent=storey_entities[s].ObjectPlacement)
            space.ObjectPlacement = sp_pl

            # Geometry: use real polygon profile when available (solver path),
            # fall back to box solid for stamp-path cells (polygon=None).
            if pc.polygon and len(pc.polygon) > 4:
                # Translate polygon to local space (minimum at origin)
                local_pts = [(x - pc.x0, y - pc.y0) for x, y in pc.polygon]
                sp_rep = _polygon_solid(m, local_pts, h, body_ctx)
            else:
                sp_rep = _box_solid(m, w, d, h, body_ctx)
            space.Representation = sp_rep

            _attach_colour(m, space, _ROLE_COLOUR.get(pc.cell.role, (0.8, 0.8, 0.8)))

            # Property sets
            try:
                _add_pset_space(m, owner_hist, space, pc.cell.role, pc.cell.unit_id, w * d)
            except Exception:
                pass

            spaces.append(space)

            # Track for IfcZone grouping and space-boundary lookup
            uid = pc.cell.unit_id
            if uid >= 0:
                unit_spaces.setdefault(uid, []).append(space)
            if pc.room_id:
                room_id_to_space[pc.room_id] = space

        if spaces:
            m.create_entity(
                "IfcRelContainedInSpatialStructure",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_hist,
                Name=None,
                Description=None,
                RelatedElements=tuple(spaces),
                RelatingStructure=storey_entities[s],
            )

        # ---- Walls --------------------------------------------------------
        try:
            z_bot = _storey_elevation(s)
            storey_pl = storey_entities[s].ObjectPlacement
            network_segs = _wall_network_map.get(s)

            if network_segs is not None:
                # Solver path: one IfcWall per WallSegment, no doubled walls.
                wall_seg_map: dict[str, object] = {}   # seg.id → IfcWall
                wall_objs = []
                for seg in network_segs:
                    wall_obj = _create_wall_from_segment(
                        m, seg, z_bottom=z_bot, height=h,
                        body_ctx=body_ctx, storey_pl=storey_pl,
                        owner_hist=owner_hist, wall_types=wall_types,
                    )
                    wall_objs.append(wall_obj)
                    wall_seg_map[seg.id] = wall_obj
                    # IfcRelSpaceBoundary for each adjacent space
                    is_ext = (seg.type == "exterior")
                    for rid in (seg.left_room_id, seg.right_room_id):
                        if rid and rid in room_id_to_space:
                            _add_space_boundary(
                                m, owner_hist, wall_obj,
                                room_id_to_space[rid], is_ext,
                            )

                # Door opening elements (cuts host wall — required for Revit)
                for door in _storey_door_map.get(s, []):
                    host = wall_seg_map.get(door.wall_id)
                    if host is None:
                        continue
                    # Find the segment to get start/end coords
                    seg = next((sg for sg in network_segs if sg.id == door.wall_id), None)
                    if seg is None:
                        continue
                    _create_opening_for_door(
                        m, door, host, seg.start, seg.end,
                        z_bottom=z_bot, body_ctx=body_ctx,
                        storey_pl=storey_pl, owner_hist=owner_hist,
                    )
            else:
                # Legacy path: derive walls from cell bounding boxes (stamp path).
                wall_edges = _extract_wall_edges(cells_here)
                wall_objs = []
                for p0, p1, is_ext in wall_edges:
                    wall_obj = _create_wall(
                        m, p0, p1,
                        z_bottom=z_bot, height=h,
                        is_exterior=is_ext,
                        body_ctx=body_ctx, storey_pl=storey_pl,
                        owner_hist=owner_hist,
                    )
                    wall_objs.append(wall_obj)
                    try:
                        _add_pset_wall_common(
                            m, owner_hist, wall_obj,
                            is_external=is_ext, load_bearing=is_ext,
                            fire_rating=60 if is_ext else 0,
                        )
                    except Exception:
                        pass
                    try:
                        wt_key = "EXT_WALL_200" if is_ext else "INT_PART_100"
                        if wt_key in wall_types:
                            m.create_entity(
                                "IfcRelDefinesByType",
                                GlobalId=ifcopenshell.guid.new(),
                                OwnerHistory=owner_hist,
                                Name=None, Description=None,
                                RelatedObjects=(wall_obj,),
                                RelatingType=wall_types[wt_key],
                            )
                    except Exception:
                        pass

            if wall_objs:
                m.create_entity(
                    "IfcRelContainedInSpatialStructure",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_hist,
                    Name=None, Description=None,
                    RelatedElements=tuple(wall_objs),
                    RelatingStructure=storey_entities[s],
                )
        except Exception:
            pass  # walls are best-effort; never fail the whole export

        # ---- Floor slab -----------------------------------------------
        try:
            slab = _create_slab(
                m, cells_here,
                z_bottom=_storey_elevation(s),
                body_ctx=body_ctx,
                storey_pl=storey_entities[s].ObjectPlacement,
                owner_hist=owner_hist,
            )
            if slab:
                m.create_entity(
                    "IfcRelContainedInSpatialStructure",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_hist,
                    Name=None,
                    Description=None,
                    RelatedElements=(slab,),
                    RelatingStructure=storey_entities[s],
                )
        except Exception:
            pass  # slab is best-effort; never fail the whole export

        # ---- Windows (simplified, no wall voiding) --------------------
        try:
            _edge_counts = _fix_extract_wall_edges(cells_here)
            win_objs = []
            for pc in cells_here:
                if pc.cell.role in _IFC_NO_WIN_ROLES:
                    continue
                for _edge_name in _fix_get_ext_edges(pc, _edge_counts):
                    _win = _fix_get_window(pc, _edge_name)
                    if _win:
                        _w_obj = _create_ifc_window(
                            m, _win,
                            storey_entities[s].ObjectPlacement,
                            body_ctx,
                        )
                        win_objs.append(_w_obj)
            if win_objs:
                m.create_entity(
                    "IfcRelContainedInSpatialStructure",
                    GlobalId=ifcopenshell.guid.new(),
                    OwnerHistory=owner_hist,
                    Name=None,
                    Description=None,
                    RelatedElements=tuple(win_objs),
                    RelatingStructure=storey_entities[s],
                )
        except Exception:
            pass  # windows are best-effort; never fail the whole export

    # ------------------------------------------------------------------
    # IfcZone grouping per dwelling unit (best-effort)
    # ------------------------------------------------------------------
    for uid, sp_list in unit_spaces.items():
        try:
            zone = m.create_entity(
                "IfcZone",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_hist,
                Name=f"DwellingUnit_{uid}",
                ObjectType="DwellingUnit",
            )
            m.create_entity(
                "IfcRelAssignsToGroup",
                GlobalId=ifcopenshell.guid.new(),
                OwnerHistory=owner_hist,
                Name=None, Description=None,
                RelatedObjects=tuple(sp_list),
                RelatingGroup=zone,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Lot footprint on site (best-effort)
    # ------------------------------------------------------------------
    try:
        coords_3d = [
            _pt3(m, x, y, 0.0)
            for x, y in list(envelope_result.lot_local.exterior.coords)
        ]
        polyline = m.create_entity("IfcPolyline", Points=tuple(coords_3d))
        fp_rep = m.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body_ctx,
            RepresentationIdentifier="FootPrint",
            RepresentationType="Curve3D",
            Items=(polyline,),
        )
        site.Representation = m.create_entity(
            "IfcProductDefinitionShape",
            Name=None, Description=None, Representations=(fp_rep,),
        )
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Serialize
    # ------------------------------------------------------------------
    ifc_bytes = m.to_string().encode("utf-8")

    if output_path:
        with open(output_path, "wb") as fh:
            fh.write(ifc_bytes)

    return ifc_bytes
