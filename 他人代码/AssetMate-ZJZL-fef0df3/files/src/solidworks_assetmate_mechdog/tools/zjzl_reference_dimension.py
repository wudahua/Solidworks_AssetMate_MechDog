"""Create driven drawing dimensions from verified native geometry references."""
from __future__ import annotations

import math

from ..sw_session import call_method
from .zjzl_geometry import component_transform, projected_span, resolve_visible, visible_components
from .zjzl_native import (
    Native, ZJZLError, activate_sheet, drawing_scope, execute, named_view, persist,
    rollback_annotation, save_drawing, sheet_point, source_path,
)
from .zjzl_orthographic import axis_vector, transform


# IDimension::GetType reports the unit family, IDisplayDimension::Type2 the style.
OPERATIONS = {
    "aligned": ("AddDimension2", {2, 9, 11, 12}),
    "horizontal": ("AddHorizontalDimension2", {2, 11}),
    "vertical": ("AddVerticalDimension2", {2, 12}),
    "angular": ("AddDimension2", {3, 16}),
    "diameter": ("AddDiameterDimension2", {6, 15}),
    "radius": ("AddRadialDimension2", {5, 14}),
}


def _displays(native, view):
    raw = view.GetFirstDisplayDimension5()
    while raw is not None:
        display = native.cast(raw, "IDisplayDimension")
        yield display
        raw = display.GetNext5()


def attachments_match(native, annotation, entities):
    attached = annotation.GetAttachedEntities3() or ()
    return len(entities) == len(attached) and all(
        any(native.app.IsSame(actual, entity) == 1 for actual in attached) for entity in entities
    )


def _validate_selection(native, kind, refs, entities, view, components):
    if kind in ("diameter", "radius"):
        if len(refs) != 1 or refs[0].kind != "edge":
            raise ZJZLError("bad_input", "A radial dimension requires one circular edge")
        edge = native.cast(entities[0], "IEdge")
        curve = native.cast(edge.GetCurve(), "ICurve")
        if curve is None or not curve.IsCircle():
            raise ZJZLError("bad_input", "A radial dimension requires a circular edge")
        normal = curve.CircleParams[3:6]
        owner = components[refs[0].component][1]
        matrix = component_transform(native, owner)
        if matrix is not None:
            normal = [sum(a * b for a, b in zip(normal, axis_vector(matrix, i))) for i in range(3)]
        view_normal = axis_vector(transform(native, view), 2)
        if not math.isclose(abs(sum(a * b for a, b in zip(normal, view_normal))), 1.0, abs_tol=1e-6):
            raise ZJZLError("bad_input", "A radial dimension requires a face-on circular edge")
        return
    if kind != "angular" and len(refs) == 2 and all(ref.kind == "vertex" for ref in refs):
        return
    count = 2 if kind == "angular" else 1
    if len(refs) == count and all(ref.kind == "edge" for ref in refs):
        edges = [native.cast(entity, "IEdge") for entity in entities]
        curves = [native.cast(edge.GetCurve(), "ICurve") for edge in edges]
        if all(curve is not None and curve.IsLine() for curve in curves):
            return
    raise ZJZLError("bad_input", "Use two straight edges for angular dimensions, or one straight edge or two vertices for linear dimensions")


def _add(drawing_path, view_name, kind, references, position_m, expected_value, tolerance, sheet_name, save):
    if kind not in OPERATIONS or not 1 <= len(references) <= 2:
        raise ZJZLError("bad_input", "Choose a supported dimension kind and one or two references")
    if (not math.isfinite(tolerance) or tolerance < 0
            or expected_value is not None and (not math.isfinite(expected_value) or expected_value < 0)):
        raise ZJZLError("bad_input", "Expected value and tolerance must be finite and nonnegative")
    path = source_path(drawing_path, (".slddrw",))
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    view = named_view(native, sheet, view_name)
    sheet_name = sheet.GetName()
    point = sheet_point(sheet, position_m)
    components = visible_components(native, view)
    entities = [resolve_visible(native, extension, view, components, ref, path, sheet_name, view_name)
                for ref in references]
    if len(entities) == 2 and native.app.IsSame(*entities) == 1:
        raise ZJZLError("bad_input", "Do not use the same entity twice")
    _validate_selection(native, kind, references, entities, view, components)
    operation, styles = OPERATIONS[kind]
    span = None
    axes = {"horizontal": 0, "vertical": 1, "aligned": None}
    if kind in axes:
        span = projected_span(native, view, components, references, entities, axes[kind])
        if span <= 1e-6:
            raise ZJZLError("bad_input", "The references have zero projected separation for the requested kind")
    for existing in _displays(native, view):
        if (existing.Type2 in styles
                and attachments_match(native, native.cast(call_method(existing, "GetAnnotation"), "IAnnotation"), entities)):
            previous = native.cast(existing.GetDimension2(0), "IDimension")
            if span is None or math.isclose(previous.SystemValue * 1000, span, abs_tol=0.01):
                raise ZJZLError("dimension_exists", "A dimension of this type already references this geometry")

    activate_sheet(native, drawing, sheet)
    doc.ClearSelection2(True)
    annotation = None
    try:
        if not drawing.ActivateView(view_name):
            raise ZJZLError("view_selection_failed", f"Could not activate {view_name!r}")
        for index, entity in enumerate(entities):
            if not view.SelectEntity(entity, index > 0):
                raise ZJZLError("entity_selection_failed", "Could not select the exact geometry in its view")
        raw = getattr(doc, operation)(*point)
        if raw is None:
            raise ZJZLError("dimension_creation_failed", "SolidWorks did not create the dimension")
        display = native.cast(raw, "IDisplayDimension")
        annotation = native.cast(call_method(display, "GetAnnotation"), "IAnnotation")
        dimension = native.cast(display.GetDimension2(0), "IDimension")
        display.ShowParenthesis = True
        unit = "deg" if kind == "angular" else "mm"
        value = math.degrees(dimension.SystemValue) if unit == "deg" else dimension.SystemValue * 1000
        identifier = persist(extension, display)
        display_type, parameter_type, driven_state = display.Type2, dimension.GetType(), dimension.DrivenState
        if display_type not in styles or parameter_type != (1 if unit == "deg" else 0) or driven_state != 1:
            raise ZJZLError("dimension_verification_failed",
                            f"Unexpected native dimension type={display_type}, parameter_type={parameter_type}, driven_state={driven_state}")
        if (not display.ShowParenthesis or annotation.IsDangling() or annotation.Visible != 1 or not identifier
                or not attachments_match(native, annotation, entities)
                or not any(native.app.IsSame(display, d) == 1 for d in _displays(native, view))):
            raise ZJZLError("dimension_verification_failed", "Could not verify the native dimension visibility, persistence or view attachments")
        if not math.isfinite(value) or span is not None and not math.isclose(value, span, abs_tol=0.01):
            raise ZJZLError("dimension_verification_failed", f"Read {value:g} {unit}, projected separation is {span} {unit}")
        if expected_value is not None and abs(value - expected_value) > tolerance:
            raise ZJZLError("dimension_value_mismatch", f"Read {value:g} {unit}, expected {expected_value} ± {tolerance:g}")
        record = {"drawing_path": path, "sheet": sheet_name, "view": view_name, "dimension_id": identifier,
                  "name": dimension.FullName, "kind": kind, "reference": True, "value": value, "unit": unit,
                  "position_m": list(annotation.GetPosition()[:2]),
                  "attachments": [ref.model_dump() for ref in references], "readback_verified": True}
        warnings = save_drawing(doc) if save else None
        return {**record, "saved": save, "save_warnings": warnings}
    except Exception as exc:
        if annotation is not None:
            rollback_annotation(doc, extension, annotation, exc)
        raise
    finally:
        doc.ClearSelection2(True)


def add_reference_dimension(drawing_path, view_name, kind, references, position_m, expected_value, tolerance, sheet, save):
    return execute(_add, drawing_path, view_name, kind, references, position_m, expected_value, tolerance, sheet, save)
