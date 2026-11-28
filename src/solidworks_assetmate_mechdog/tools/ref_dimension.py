"""Driven (reference) drawing dimensions on verified native geometry.

Why this exists (ZJZL adoption batch 2, 2026-11-28)
--------------------------------------------------
Two long-standing defects made dimensions untrustworthy:

1. `AddDimension2` does not guarantee the dimension TYPE it returns. This toolkit recorded
   that a pair of parallel edges produced an angular dimension, and that it had to delete
   and rebuild afterwards. The per-type entry points below (`AddHorizontalDimension2`,
   `AddVerticalDimension2`, `AddDiameterDimension2`, `AddRadialDimension2`) remove the guess:
   if SolidWorks returns the wrong style, the call fails instead of leaving a wrong dimension.

2. Attachment was verified by comparing positions, which cannot tell two coincident edges
   apart. `IAnnotation.GetAttachedEntities3` + `ISldWorks.IsSame` answers the real question.

A reference dimension (bracketed, driven) is what a shop needs when the value must come from
geometry but must not drive it: `IDisplayDimension.ShowParenthesis` + `IDimension.DrivenState
== 1`, both read back before the call is reported as done.
"""
from __future__ import annotations

import math

from .. import sw_session
from . import geometry as geo
from .native_com import (
    ComScopeError, Native, activate_sheet, drawing_scope, execute, named_view, optional,
    persist, rollback_annotation, save_drawing, sheet_point, source_path, value_of,
)
from .ref_geometry import (
    component_transform, reference_from, resolve_visible, visible_components,
)

# kind -> (API on IModelDoc, acceptable IDisplayDimension.Type2 values)
OPERATIONS = {
    "aligned": ("AddDimension2", {2, 9, 11, 12}),
    "horizontal": ("AddHorizontalDimension2", {2, 11}),
    "vertical": ("AddVerticalDimension2", {2, 12}),
    "angular": ("AddDimension2", {3, 16}),
    "diameter": ("AddDiameterDimension2", {6, 15}),
    "radius": ("AddRadialDimension2", {5, 14}),
}

AXES = {"horizontal": 0, "vertical": 1, "aligned": None}


def displays(native, view):
    """Every display dimension in the view, in SolidWorks' own order."""
    raw = optional(lambda: value_of(view, "GetFirstDisplayDimension5"))
    while raw is not None:
        display = native.cast(raw, "IDisplayDimension")
        if display is None:
            return
        yield display
        raw = display.GetNext5()


def attachments_match(native, annotation, entities):
    """Does the annotation attach to EXACTLY these entities? Identity, not proximity."""
    attached = annotation.GetAttachedEntities3() or ()
    if len(entities) != len(attached):
        return False
    return all(any(native.app.IsSame(actual, entity) == 1 for actual in attached)
               for entity in entities)


def validate_selection(native, kind, refs, entities, view, components):
    """Reject selections SolidWorks would accept but that mean something else."""
    if kind in ("diameter", "radius"):
        if len(refs) != 1 or refs[0].kind != "edge":
            raise ComScopeError("bad_input", "A radial dimension requires one circular edge")
        curve = native.cast(optional(lambda: value_of(native.cast(entities[0], "IEdge"), "GetCurve")),
                            "ICurve")
        if curve is None or not bool(optional(lambda: value_of(curve, "IsCircle"))):
            raise ComScopeError("bad_input", "A radial dimension requires a circular edge")
        normal = [float(v) for v in curve.CircleParams[3:6]]
        matrix = component_transform(native, components[refs[0].component][1])
        if matrix is not None:
            normal = [sum(a * b for a, b in zip(normal, geo.axis_vector(matrix, i)))
                      for i in range(3)]
        view_normal = geo.axis_vector(geo.view_frame(view)["raw"], 2)
        if not math.isclose(abs(sum(a * b for a, b in zip(normal, view_normal))), 1.0,
                            abs_tol=1e-6):
            raise ComScopeError("bad_input",
                                "A radial dimension requires a face-on circular edge")
        return
    if kind != "angular" and len(refs) == 2 and all(ref.kind == "vertex" for ref in refs):
        return
    count = 2 if kind == "angular" else 1
    if len(refs) == count and all(ref.kind == "edge" for ref in refs):
        curves = [native.cast(optional(lambda: value_of(native.cast(e, "IEdge"), "GetCurve")), "ICurve")
                  for e in entities]
        if all(c is not None and bool(optional(lambda: value_of(c, "IsLine"))) for c in curves):
            return
    raise ComScopeError(
        "bad_input",
        "Use two straight edges for angular dimensions, or one straight edge or two "
        "vertices for linear dimensions")


def projected_span(native, view, components, refs, entities, axis):
    """Sheet distance the requested dimension should read, in millimetres. PURE-ish.

    Used as an independent check on the value SolidWorks returns: if the two disagree by
    more than 0.01 mm the dimension attached to something other than the requested geometry.
    """
    from .ref_geometry import edge_row

    frame = geo.view_frame(view)
    if frame is None:
        raise ComScopeError("view_transform_unavailable", "Could not read the view transform")
    view_matrix = frame["raw"]
    if refs[0].kind == "edge":
        owner = components[refs[0].component][1]
        info, _ = edge_row(native, entities[0], component_transform(native, owner), view_matrix)
        points = (info or {}).get("endpoints_sheet_m") or []
    else:
        points = []
        for reference, entity in zip(refs, entities):
            component, owner = components[reference.component]
            owner_matrix = component_transform(native, owner)
            candidates = []
            for raw in (value_of(view, "GetVisibleEntities2", component, 1) or ()):
                result = optional(lambda: edge_row(native, raw, owner_matrix, view_matrix))
                if not result:
                    continue
                for vertex, point in result[1]:
                    if vertex is not None and native.app.IsSame(entity, vertex) == 1:
                        candidates.append(point)
            if not candidates or any(math.dist(p, candidates[0]) > 1e-8 for p in candidates):
                raise ComScopeError("dimension_verification_failed",
                                    "Could not verify the projected vertex endpoints")
            points.append(geo.point_through(view_matrix, candidates[0])[:2])
    if len(points) < 2:
        raise ComScopeError("dimension_verification_failed",
                            "Could not read both endpoints of the selected geometry")
    span = math.dist(*points) if axis is None else abs(points[0][axis] - points[1][axis])
    return span / frame["scale"] * 1000


def _add(drawing_path, view_name, kind, references, position_m, expected_value, tolerance,
         sheet_name, save):
    if kind not in OPERATIONS or not 1 <= len(references) <= 2:
        raise ComScopeError("bad_input",
                            "Choose one of %s and one or two references"
                            % sorted(OPERATIONS))
    if (not math.isfinite(tolerance) or tolerance < 0
            or expected_value is not None
            and (not math.isfinite(expected_value) or expected_value < 0)):
        raise ComScopeError("bad_input", "Expected value and tolerance must be finite and nonnegative")
    path = source_path(drawing_path, (".slddrw",))
    refs = [reference_from(item) for item in references]
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    view = named_view(native, sheet, view_name)
    sheet_name = sheet.GetName()
    point = sheet_point(sheet, position_m)
    components = visible_components(native, view)
    entities = [resolve_visible(native, extension, view, components, ref, path,
                                sheet_name, view_name) for ref in refs]
    if len(entities) == 2 and native.app.IsSame(*entities) == 1:
        raise ComScopeError("bad_input", "Do not use the same entity twice")
    validate_selection(native, kind, refs, entities, view, components)
    operation, styles = OPERATIONS[kind]
    span = None
    if kind in AXES:
        span = projected_span(native, view, components, refs, entities, AXES[kind])
        if span <= 1e-6:
            raise ComScopeError("bad_input",
                                "The references have zero projected separation for this kind")
    for existing in displays(native, view):
        if existing.Type2 not in styles:
            continue
        annotation = native.cast(sw_session.call_method(existing, "GetAnnotation"), "IAnnotation")
        if annotation is None or not attachments_match(native, annotation, entities):
            continue
        previous = native.cast(existing.GetDimension2(0), "IDimension")
        if previous is not None and (span is None
                                     or math.isclose(previous.SystemValue * 1000, span,
                                                     abs_tol=0.01)):
            raise ComScopeError("dimension_exists",
                                "A dimension of this type already references this geometry")

    activate_sheet(native, drawing, sheet)
    doc.ClearSelection2(True)
    annotation = None
    try:
        if not drawing.ActivateView(view_name):
            raise ComScopeError("view_selection_failed",
                                "Could not activate %r" % view_name)
        for index, entity in enumerate(entities):
            if not view.SelectEntity(entity, index > 0):
                raise ComScopeError("entity_selection_failed",
                                    "Could not select the exact geometry in its view")
        raw = getattr(doc, operation)(*point)
        if raw is None:
            raise ComScopeError("dimension_creation_failed",
                                "SolidWorks did not create the dimension")
        display = native.cast(raw, "IDisplayDimension")
        annotation = native.cast(sw_session.call_method(display, "GetAnnotation"), "IAnnotation")
        dimension = native.cast(display.GetDimension2(0), "IDimension")
        if annotation is None or dimension is None:
            raise ComScopeError("dimension_creation_failed",
                                "The returned display dimension is incomplete")
        display.ShowParenthesis = True
        unit = "deg" if kind == "angular" else "mm"
        value = (math.degrees(dimension.SystemValue) if unit == "deg"
                 else dimension.SystemValue * 1000)
        identifier = persist(extension, display)
        display_type = display.Type2
        parameter_type = dimension.GetType()
        driven_state = dimension.DrivenState
        if (display_type not in styles
                or parameter_type != (1 if unit == "deg" else 0)
                or driven_state != 1):
            raise ComScopeError(
                "dimension_verification_failed",
                "Unexpected native dimension type=%s, parameter_type=%s, driven_state=%s"
                % (display_type, parameter_type, driven_state))
        if (not display.ShowParenthesis or annotation.IsDangling() or annotation.Visible != 1
                or not identifier or not attachments_match(native, annotation, entities)
                or not any(native.app.IsSame(display, other) == 1
                           for other in displays(native, view))):
            raise ComScopeError(
                "dimension_verification_failed",
                "Could not verify the native dimension visibility, persistence or attachments")
        if not math.isfinite(value) or (span is not None
                                        and not math.isclose(value, span, abs_tol=0.01)):
            raise ComScopeError("dimension_verification_failed",
                                "Read %g %s, projected separation is %s %s"
                                % (value, unit, span, unit))
        if expected_value is not None and abs(value - expected_value) > tolerance:
            raise ComScopeError("dimension_value_mismatch",
                                "Read %g %s, expected %g ± %g"
                                % (value, unit, expected_value, tolerance))
        record = {"drawing_path": path, "sheet": sheet_name, "view": view_name,
                  "dimension_id": identifier, "name": dimension.FullName, "kind": kind,
                  "reference": True, "value": value, "unit": unit,
                  "position_m": [round(float(v), 6) for v in annotation.GetPosition()[:2]],
                  "attachments": [ref.model_dump() for ref in refs],
                  "readback_verified": True, "saved": bool(save)}
        if save:
            record["save_warnings"] = save_drawing(doc)
        return record
    except Exception as exc:
        if annotation is not None:
            rollback_annotation(doc, extension, annotation, exc)
        raise
    finally:
        doc.ClearSelection2(True)


def add_reference_dimension(drawing_path, view_name, kind, references, position_m,
                            expected_value=None, tolerance=0.01, sheet=None, save=False):
    """A bracketed, driven dimension attached to exact visible geometry."""
    return execute(_add, drawing_path, view_name, kind, references, position_m,
                   expected_value, tolerance, sheet, save)
