"""Create native, aligned projections from an existing drawing view."""
from __future__ import annotations

from contextlib import contextmanager
import math

from .zjzl_native import (
    Native, ZJZLError, activate_sheet, drawing_scope, execute, model_views, named_view,
    path_key, save_drawing, sheet_point, source_path,
)


# Axis of the view normal in the base view, looking from that side of the object.
SIDES = {"right": (0, 1), "left": (0, -1), "top": (1, 1), "bottom": (1, -1)}


def projection_positions(origin, directions, offset, first_angle):
    if not directions or len(directions) != len(set(directions)) or set(directions) - SIDES.keys():
        raise ZJZLError("bad_input", "Directions must be unique and chosen from top, bottom, left, right")
    if not math.isfinite(offset) or offset <= 0:
        raise ZJZLError("bad_input", "Projection offset must be positive and finite")
    positions = {}
    for direction in directions:
        axis, sign = SIDES[direction]
        position = list(origin[:2])
        position[axis] += offset * sign * (-1 if first_angle else 1)
        positions[direction] = position
    return positions


def transform(native, view):
    matrix = native.cast(view.ModelToViewTransform, "IMathTransform")
    data = tuple(matrix.ArrayData) if matrix is not None else ()
    if len(data) < 13 or not all(math.isfinite(value) for value in data) or data[12] <= 0:
        raise ZJZLError("view_transform_unavailable", f"Could not read transform for {view.GetName2()!r}")
    return data


def axis_vector(matrix, index):
    return matrix[index:9:3]


def select_view(extension, name):
    return extension.SelectByID2(name, "DRAWINGVIEW", 0.0, 0.0, 0.0, False, 0, None, 0)


def view_record(view):
    return {"name": view.GetName2(), "position_m": list(view.Position[:2]),
            "outline_m": list(view.GetOutline()), "configuration": view.ReferencedConfiguration}


def check_layout(native, sheet, view):
    name = view.GetName2()
    left, bottom, right, top = view.GetOutline()
    width, height = sheet.GetProperties2()[5:7]
    if not (0 <= left < right <= width and 0 <= bottom < top <= height):
        raise ZJZLError("view_outside_sheet", f"Outline for {name!r} does not fit on the sheet")
    for other in model_views(native, sheet):
        if other.GetName2() == name:
            continue
        x1, y1, x2, y2 = other.GetOutline()
        if max(left, x1) < min(right, x2) and max(bottom, y1) < min(top, y2):
            raise ZJZLError("view_overlap", f"View {name!r} overlaps {other.GetName2()!r}")


@contextmanager
def view_changes(native, path, doc, drawing, sheet, extension):
    created = []
    try:
        yield created
    except Exception as cause:
        if created:
            try:
                native.document(path, 3)
                activate_sheet(native, drawing, sheet)
                for raw in reversed(created):
                    name = native.cast(raw, "IView").GetName2()
                    doc.ClearSelection2(True)
                    if not select_view(extension, name) or not extension.DeleteSelection2(0):
                        raise RuntimeError(f"SolidWorks did not delete {name!r}")
                    if any(view.GetName2() == name for view in model_views(native, sheet)):
                        raise RuntimeError(f"View {name!r} remains on the sheet")
            except Exception as exc:
                raise ZJZLError("rollback_failed", f"{cause}. View cleanup failed: {exc}") from exc
        raise
    finally:
        doc.ClearSelection2(True)


def _create(drawing_path, base_view, directions, offset_m, sheet_name, save):
    path = source_path(drawing_path, (".slddrw",))
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    parent = named_view(native, sheet, base_view)
    parent_matrix = transform(native, parent)
    model_path = path_key(parent.GetReferencedModelName())
    configuration = parent.ReferencedConfiguration
    first_angle = bool(sheet.GetProperties2()[4])
    positions = projection_positions(parent.Position, directions, offset_m, first_angle)
    expected = {direction: tuple(SIDES[direction][1] * value for value in axis_vector(
        parent_matrix, SIDES[direction][0])) for direction in positions}
    for point in positions.values():
        sheet_point(sheet, point)

    existing = {}
    for view in model_views(native, sheet):
        if view.Type != 4:  # swDrawingProjectedView
            continue
        base = native.cast(view.GetBaseView(), "IView")
        if base is None or base.GetName2() != base_view:
            continue
        normal = axis_vector(transform(native, view), 2)
        for direction, target in expected.items():
            if math.dist(normal, target) < 1e-6:
                raise ZJZLError("projection_exists", f"{direction!r} projection already exists as {view.GetName2()!r}")

    activate_sheet(native, drawing, sheet)
    with view_changes(native, path, doc, drawing, sheet, extension) as created:
        for direction, point in positions.items():
            doc.ClearSelection2(True)
            if not drawing.ActivateView(base_view) or not select_view(extension, base_view):
                raise ZJZLError("view_selection_failed", f"Could not select projection parent {base_view!r}")
            raw = drawing.CreateUnfoldedViewAt3(*point, 0.0, False)
            if raw is None:
                raise ZJZLError("projection_failed", f"SolidWorks did not create the {direction!r} projection")
            created.append(raw)
            view = native.cast(raw, "IView")
            doc.EditRebuild3()
            matrix = transform(native, view)
            actual_base = native.cast(view.GetBaseView(), "IView")
            if (view.Type != 4 or actual_base is None or actual_base.GetName2() != base_view
                    or path_key(view.GetReferencedModelName()) != model_path
                    or view.ReferencedConfiguration != configuration
                    or math.dist(axis_vector(matrix, 2), expected[direction]) > 1e-6
                    or not math.isclose(matrix[12], parent_matrix[12], rel_tol=1e-6)
                    or math.dist(view.Position[:2], point) > 0.001):
                raise ZJZLError("projection_verification_failed", f"Native {direction!r} view readback differs from the request")
            check_layout(native, sheet, view)
            existing[direction] = {**view_record(view), "normal_model": list(axis_vector(matrix, 2))}
        warnings = save_drawing(doc) if save else None
    return {"drawing_path": path, "sheet": sheet.GetName(), "base_view": base_view,
            "projection": "first" if first_angle else "third", "views": existing, "saved": save,
            "save_warnings": warnings, "readback_verified": True}


def create_orthographic_views(drawing_path, base_view, directions, offset_m, sheet, save):
    return execute(_create, drawing_path, base_view, directions, offset_m, sheet, save)
