"""Native projected views: aligned orthographic children and arbitrary orientations.

Why this exists (ZJZL adoption batch 3, 2026-11-28)
--------------------------------------------------
`place_views` positions views by arithmetic: it computes a sheet point, drops a view there,
and hopes the result is a proper projection. Nothing about that is native — the new view is
not linked to a parent, does not inherit its configuration or scale, and "is this the top
view?" is answered by the orientation NAME, which is a string.

This module uses SolidWorks' own machinery instead:

    IDrawingDoc.CreateUnfoldedViewAt3        aligned child of a base view, first/third angle
                                             decided by the SHEET, not by us
    IDrawingDoc.CreateDrawViewFromModelView3 an arbitrary direction, taken from a temporary
                                             model-view orientation that is then restored

Both verify what came back: parent relation, model path, configuration, normal direction,
scale, and placement. A batch that fails part-way deletes what it created — a half-built
projection block is worse than none, because the sheet looks plausible.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from uuid import uuid4

from .. import sw_session
from . import geometry as geo
from .native_com import (
    ComScopeError, Native, activate_sheet, drawing_scope, execute, model_views, named_view,
    optional, path_key, save_drawing, sheet_point, sheet_properties, source_path, value_of,
)

# axis in the base view, sign of the offset for a FIRST-angle layout
SIDES = {"right": (0, 1), "left": (0, -1), "top": (1, 1), "bottom": (1, -1)}


def projection_positions(origin, directions, offset, first_angle):
    """Sheet centres for the requested projections. PURE.

    First angle: the view seen from the top goes BELOW the base view, from the right goes to
    its LEFT (the object sits between the viewer and the sheet). Third angle is the reverse.
    Measured reference: GB A3 template reports `first_angle=True`, and SolidWorks places 上视
    below the front view — the same convention this formula reproduces.
    """
    if not directions or len(directions) != len(set(directions)) \
            or set(directions) - SIDES.keys():
        raise ComScopeError("bad_input",
                            "Directions must be unique and chosen from top, bottom, left, right")
    if not math.isfinite(offset) or offset <= 0:
        raise ComScopeError("bad_input", "Projection offset must be positive and finite")
    positions = {}
    for direction in directions:
        axis, sign = SIDES[direction]
        position = list(origin[:2])
        position[axis] += offset * sign * (-1 if first_angle else 1)
        positions[direction] = position
    return positions


def view_transform(view):
    """The 16-number frame of a drawing view; a zero scale means it is unusable."""
    matrix = optional(lambda: value_of(view, "ModelToViewTransform"))
    data = tuple(float(v) for v in (value_of(matrix, "ArrayData") if matrix is not None else ()))
    if len(data) < 13 or not all(math.isfinite(v) for v in data) or data[12] <= 0:
        raise ComScopeError("view_transform_unavailable",
                            "Could not read transform for %r" % value_of(view, "GetName2"))
    return data


def view_normal(data):
    return geo.axis_vector(data, 2)


def view_record(view):
    return {"name": value_of(view, "GetName2"),
            "position_m": [round(float(v), 6) for v in view.Position[:2]],
            "outline_m": [round(float(v), 6) for v in value_of(view, "GetOutline")],
            "configuration": view.ReferencedConfiguration}


def check_layout(native, sheet, view):
    """The new view must be on the sheet and must not overlap any other view."""
    name = value_of(view, "GetName2")
    left, bottom, right, top = value_of(view, "GetOutline")
    props = sheet_properties(sheet)
    if not (0 <= left < right <= props["width_m"] and 0 <= bottom < top <= props["height_m"]):
        raise ComScopeError("view_outside_sheet",
                            "Outline for %r does not fit on the sheet" % name)
    for other in model_views(native, sheet):
        if value_of(other, "GetName2") == name:
            continue
        x1, y1, x2, y2 = value_of(other, "GetOutline")
        if max(left, x1) < min(right, x2) and max(bottom, y1) < min(top, y2):
            raise ComScopeError("view_overlap",
                                "View %r overlaps %r"
                                % (name, value_of(other, "GetName2")))


def select_view(extension, name):
    return bool(value_of(extension, "SelectByID2", name, "DRAWINGVIEW",
                         0.0, 0.0, 0.0, False, 0, None, 0))


@contextmanager
def view_changes(native, path, doc, drawing, sheet, extension):
    """Delete every view created inside the block if anything raises. Reports failed undo."""
    created = []
    try:
        yield created
    except Exception as cause:
        if created:
            try:
                native.document(path, sw_session.SW_DOC_DRAWING)
                activate_sheet(native, drawing, sheet)
                for raw in reversed(created):
                    name = value_of(native.cast(raw, "IView"), "GetName2")
                    doc.ClearSelection2(True)
                    if not select_view(extension, name) or not extension.DeleteSelection2(0):
                        raise RuntimeError("SolidWorks did not delete %r" % name)
                    if any(value_of(view, "GetName2") == name
                           for view in model_views(native, sheet)):
                        raise RuntimeError("View %r remains on the sheet" % name)
            except Exception as exc:
                raise ComScopeError("rollback_failed",
                                    "%s. View cleanup failed: %s" % (cause, exc)) from exc
        raise
    finally:
        doc.ClearSelection2(True)


def _create_orthographic(drawing_path, base_view, directions, offset_m, sheet_name, save):
    path = source_path(drawing_path, (".slddrw",))
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    parent = named_view(native, sheet, base_view)
    parent_matrix = view_transform(parent)
    model_path = path_key(parent.GetReferencedModelName())
    configuration = parent.ReferencedConfiguration
    first_angle = sheet_properties(sheet)["first_angle"]
    positions = projection_positions(parent.Position, directions, offset_m, first_angle)
    expected = {direction: tuple(SIDES[direction][1] * value for value in
                                 geo.axis_vector(parent_matrix, SIDES[direction][0]))
                for direction in positions}
    for point in positions.values():
        sheet_point(sheet, point)

    for view in model_views(native, sheet):
        if view.Type != 4:  # swDrawingProjectedView
            continue
        base = native.cast(value_of(view, "GetBaseView"), "IView")
        if base is None or value_of(base, "GetName2") != base_view:
            continue
        normal = view_normal(view_transform(view))
        for direction, target in expected.items():
            if math.dist(normal, target) < 1e-6:
                raise ComScopeError("projection_exists",
                                    "%r projection already exists as %r"
                                    % (direction, value_of(view, "GetName2")))

    activate_sheet(native, drawing, sheet)
    created_views = {}
    with view_changes(native, path, doc, drawing, sheet, extension) as created:
        for direction, point in positions.items():
            doc.ClearSelection2(True)
            if not drawing.ActivateView(base_view) or not select_view(extension, base_view):
                raise ComScopeError("view_selection_failed",
                                    "Could not select projection parent %r" % base_view)
            raw = drawing.CreateUnfoldedViewAt3(*point, 0.0, False)
            if raw is None:
                raise ComScopeError("projection_failed",
                                    "SolidWorks did not create the %r projection" % direction)
            created.append(raw)
            view = native.cast(raw, "IView")
            doc.EditRebuild3()
            matrix = view_transform(view)
            actual_base = native.cast(value_of(view, "GetBaseView"), "IView")
            if (view.Type != 4 or actual_base is None
                    or value_of(actual_base, "GetName2") != base_view
                    or path_key(view.GetReferencedModelName()) != model_path
                    or view.ReferencedConfiguration != configuration
                    or math.dist(view_normal(matrix), expected[direction]) > 1e-6
                    or not math.isclose(matrix[12], parent_matrix[12], rel_tol=1e-6)
                    or math.dist(view.Position[:2], point) > 0.001):
                raise ComScopeError("projection_verification_failed",
                                    "Native %r view readback differs from the request" % direction)
            check_layout(native, sheet, view)
            created_views[direction] = {**view_record(view),
                                        "normal_model": [round(v, 6) for v in view_normal(matrix)]}
        warnings = save_drawing(doc) if save else None
    return {"drawing_path": path, "sheet": sheet.GetName(), "base_view": base_view,
            "projection": "first" if first_angle else "third", "views": created_views,
            "saved": bool(save), "save_warnings": warnings, "readback_verified": True}


def create_orthographic_views(drawing_path, base_view, directions, offset_m=0.09,
                              sheet=None, save=False):
    """Aligned native projections, laid out by the sheet's own angle convention."""
    return execute(_create_orthographic, drawing_path, base_view, directions, offset_m,
                   sheet, save)


def _unit(vector):
    if len(vector) != 3 or not all(math.isfinite(float(v)) for v in vector):
        raise ComScopeError("bad_input", "Orientation vectors must have three finite components")
    values = [float(v) for v in vector]
    largest = max(abs(v) for v in values)
    if not largest:
        raise ComScopeError("bad_input", "Orientation vectors cannot be zero")
    scaled = [v / largest for v in values]
    length = math.hypot(*scaled)
    return tuple(v / length for v in scaled)


def rotation(normal, up):
    """Row-major 3x3 that looks along `normal` with `up` as the vertical. PURE."""
    normal = _unit(normal)
    up = _unit(up)
    right = (up[1] * normal[2] - up[2] * normal[1],
             up[2] * normal[0] - up[0] * normal[2],
             up[0] * normal[1] - up[1] * normal[0])
    if math.hypot(*right) < 1e-8:
        raise ComScopeError("bad_input", "Normal and up must not be parallel")
    length = math.hypot(*right)
    right = tuple(v / length for v in right)
    up = (normal[1] * right[2] - normal[2] * right[1],
          normal[2] * right[0] - normal[0] * right[2],
          normal[0] * right[1] - normal[1] * right[0])
    return tuple(value for row in zip(right, up, normal) for value in row)


def _create_custom(drawing_path, model_path, normal, up, position_m, configuration,
                   sheet_name, save):
    matrix = rotation(normal, up)
    path = source_path(drawing_path, (".slddrw",))
    model_path = source_path(model_path, (".sldprt", ".sldasm"))
    kind = sw_session.SW_DOC_PART if model_path.lower().endswith(".sldprt") \
        else sw_session.SW_DOC_ASSEMBLY
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    point = sheet_point(sheet, position_m)
    model = native.document(model_path, kind)
    if configuration is not None and configuration not in (model.GetConfigurationNames() or ()):
        native.document(path, sw_session.SW_DOC_DRAWING)
        raise ComScopeError("configuration_not_found",
                            "Model has no configuration %r" % configuration)

    viewport = native.cast(value_of(model, "ActiveView"), "IModelView")
    original = native.cast(value_of(viewport, "Orientation3"), "IMathTransform")
    original_data = tuple(original.ArrayData)
    math_utility = native.cast(native.app.GetMathUtility(), "IMathUtility")
    requested = math_utility.CreateTransform(sw_session.double_array((*matrix, *original_data[9:])))
    name = "ZJZL_%s" % uuid4().hex
    created_view = None
    with view_changes(native, path, doc, drawing, sheet, extension) as created:
        try:
            try:
                viewport.Orientation3 = requested
                current = native.cast(value_of(viewport, "Orientation3"), "IMathTransform")
                if math.dist(tuple(current.ArrayData)[:9], matrix) > 1e-6:
                    raise ComScopeError("orientation_failed",
                                        "The model viewport rejected the requested orientation")
                model.NameView(name)
                if name not in (model.GetModelViewNames() or ()):
                    raise ComScopeError("orientation_failed",
                                        "SolidWorks did not register the temporary view")
            finally:
                viewport.Orientation3 = original
                current = native.cast(value_of(viewport, "Orientation3"), "IMathTransform")
                if math.dist(tuple(current.ArrayData), original_data) > 1e-6:
                    raise ComScopeError("viewport_restore_failed",
                                        "Could not restore the original model viewport")
            native.document(path, sw_session.SW_DOC_DRAWING)
            activate_sheet(native, drawing, sheet)
            raw = drawing.CreateDrawViewFromModelView3(model_path, name, *point)
            if raw is None:
                raise ComScopeError("custom_view_failed",
                                    "SolidWorks did not create a drawing view")
            created.append(raw)
            created_view = raw
            view = native.cast(raw, "IView")
            if configuration is not None:
                view.ReferencedConfiguration = configuration
            view.Position = sw_session.double_array(point[:2])
        finally:
            try:
                model = native.document(model_path, kind)
                if name in (model.GetModelViewNames() or ()):
                    if not model.DeleteNamedView(name) or name in (model.GetModelViewNames() or ()):
                        raise ComScopeError("orientation_cleanup_failed",
                                            "Could not remove temporary model view %r" % name)
            finally:
                native.document(path, sw_session.SW_DOC_DRAWING)

        doc.EditRebuild3()
        view = native.cast(created_view, "IView")
        actual = view_transform(view)
        if (math.dist(actual[:9], matrix) > 1e-6
                or math.dist(view.Position[:2], point[:2]) > 0.001
                or path_key(view.GetReferencedModelName()) != path_key(model_path)
                or (configuration is not None and view.ReferencedConfiguration != configuration)):
            raise ComScopeError("custom_view_verification_failed",
                                "The drawing view readback differs from the request")
        check_layout(native, sheet, view)
        record = view_record(view)
        warnings = save_drawing(doc) if save else None
    return {"drawing_path": path, "sheet": sheet.GetName(), "view": record,
            "rotation": [round(v, 9) for v in actual[:9]],
            "saved": bool(save), "save_warnings": warnings, "readback_verified": True}


def create_custom_view(drawing_path, model_path, normal, up, position_m,
                       configuration=None, sheet=None, save=False):
    """A drawing view along an arbitrary model direction, with the viewport restored."""
    return execute(_create_custom, drawing_path, model_path, normal, up, position_m,
                   configuration, sheet, save)
