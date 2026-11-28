"""Create a drawing view from model-space normal and up vectors."""
from __future__ import annotations

import math
from uuid import uuid4

from ..sw_session import double_array
from .zjzl_native import Native, ZJZLError, activate_sheet, drawing_scope, execute, path_key, save_drawing, sheet_point, source_path
from .zjzl_orthographic import check_layout, transform, view_changes, view_record


def _unit(vector):
    if len(vector) != 3 or not all(math.isfinite(value) for value in vector):
        raise ZJZLError("bad_input", "Orientation vectors must have three finite components")
    largest = max(abs(value) for value in vector)
    if not largest:
        raise ZJZLError("bad_input", "Orientation vectors cannot be zero")
    scaled = [value / largest for value in vector]
    length = math.hypot(*scaled)
    return tuple(value / length for value in scaled)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def rotation(normal, up):
    normal = _unit(normal)
    right = _cross(_unit(up), normal)
    if math.hypot(*right) < 1e-8:
        raise ZJZLError("bad_input", "Normal and up must not be parallel")
    right = _unit(right)
    up = _cross(normal, right)
    return tuple(value for row in zip(right, up, normal) for value in row)


def _create(drawing_path, model_path, normal, up, position_m, configuration, sheet_name, save):
    matrix = rotation(normal, up)
    path = source_path(drawing_path, (".slddrw",))
    model_path = source_path(model_path, (".sldprt", ".sldasm"))
    kind = 1 if model_path.lower().endswith(".sldprt") else 2
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    point = sheet_point(sheet, position_m)
    model = native.document(model_path, kind)
    if configuration is not None and configuration not in (model.GetConfigurationNames() or ()):
        native.document(path, 3)
        raise ZJZLError("configuration_not_found", f"Model has no configuration {configuration!r}")

    viewport = native.cast(model.ActiveView, "IModelView")
    original = native.cast(viewport.Orientation3, "IMathTransform")
    original_data = tuple(original.ArrayData)
    math_utility = native.cast(native.app.GetMathUtility(), "IMathUtility")
    requested = math_utility.CreateTransform(double_array((*matrix, *original_data[9:])))
    name = f"ZJZL_{uuid4().hex}"
    with view_changes(native, path, doc, drawing, sheet, extension) as created:
        try:
            try:
                viewport.Orientation3 = requested
                current = native.cast(viewport.Orientation3, "IMathTransform")
                if math.dist(current.ArrayData[:9], matrix) > 1e-6:
                    raise ZJZLError("orientation_failed", "The model viewport rejected the requested orientation")
                model.NameView(name)
                if name not in (model.GetModelViewNames() or ()):
                    raise ZJZLError("orientation_failed", "SolidWorks did not register the temporary ZJZL view")
            finally:
                viewport.Orientation3 = original
                current = native.cast(viewport.Orientation3, "IMathTransform")
                if math.dist(current.ArrayData, original_data) > 1e-6:
                    raise ZJZLError("viewport_restore_failed", "Could not restore the original model viewport")
            native.document(path, 3)
            activate_sheet(native, drawing, sheet)
            raw = drawing.CreateDrawViewFromModelView3(model_path, name, *point)
            if raw is None:
                raise ZJZLError("custom_view_failed", "SolidWorks did not create a drawing view")
            created.append(raw)
            view = native.cast(raw, "IView")
            if configuration is not None:
                view.ReferencedConfiguration = configuration
            view.Position = double_array(point[:2])
        finally:
            try:
                model = native.document(model_path, kind)
                if name in (model.GetModelViewNames() or ()):
                    if not model.DeleteNamedView(name) or name in (model.GetModelViewNames() or ()):
                        raise ZJZLError("orientation_cleanup_failed", f"Could not remove temporary model view {name}")
            finally:
                native.document(path, 3)

        doc.EditRebuild3()
        actual = transform(native, view)
        if (math.dist(actual[:9], matrix) > 1e-6 or math.dist(view.Position[:2], point[:2]) > 0.001
                or path_key(view.GetReferencedModelName()) != path_key(model_path)
                or configuration is not None and view.ReferencedConfiguration != configuration):
            raise ZJZLError("custom_view_verification_failed", "The drawing view readback differs from the requested orientation or placement")
        check_layout(native, sheet, view)
        record = view_record(view)
        warnings = save_drawing(doc) if save else None
    return {"drawing_path": path, "sheet": sheet.GetName(), "view": record,
            "rotation": list(actual[:9]), "saved": save, "save_warnings": warnings, "readback_verified": True}


def create_custom_view(drawing_path, model_path, normal, up, position_m, configuration, sheet, save):
    return execute(_create, drawing_path, model_path, normal, up, position_m, configuration, sheet, save)
