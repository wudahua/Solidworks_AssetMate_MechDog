"""Read selectable native drawing geometry and verify scoped references."""
from __future__ import annotations

from collections import defaultdict
from typing import Literal
import math

from pydantic import BaseModel, Field

from .zjzl_native import Native, ZJZLError, drawing_scope, execute, named_view, optional, path_key, persist, read_reference, source_path
from .zjzl_orthographic import axis_vector, transform


class GeometryReference(BaseModel, extra="forbid"):
    drawing_path: str
    sheet: str
    view: str
    kind: Literal["edge", "vertex"]
    component: str | None = None
    id: str = Field(min_length=1, max_length=65536)


def point_through(matrix, point):
    return [sum(value * basis for value, basis in zip(point, axis_vector(matrix, axis))) * matrix[12]
            + matrix[9 + axis] for axis in range(3)]


def visible_components(native, view):
    model = native.cast(view.ReferencedDocument, "IModelDoc2")
    if model is None or not view.IsModelLoaded():
        raise ZJZLError("model_unavailable", "The view must reference a loaded model")
    if model.GetType() == 1:
        return {None: (None, None)}
    if model.GetType() != 2:
        raise ZJZLError("model_unavailable", "Expected a part or assembly view")
    assembly = native.cast(model, "IAssemblyDoc")
    owners = {owner.Name2: owner for owner in (
        native.cast(obj, "IComponent2") for obj in assembly.GetComponents(False) or ())}
    components = {}
    for raw in view.GetVisibleComponents() or ():
        component = native.cast(raw, "IComponent2")
        name = component.Name2
        # Drawing occurrences can include the root assembly prefix.
        matching = [owners[key] for key in {name, name.partition("/")[2]} & owners.keys()
                    if path_key(owners[key].GetPathName()) == path_key(component.GetPathName())]
        if len(matching) != 1:
            raise ZJZLError("component_scope_unavailable", f"Could not uniquely map drawing occurrence {name!r}")
        owner = matching[0]
        if owner.Name2 in components:
            raise ZJZLError("component_scope_unavailable", f"Duplicate drawing occurrence for {owner.Name2!r}")
        components[owner.Name2] = (component, owner)
    return components


def visible_entities(view, component, kind):
    return view.GetVisibleEntities2(component, 1 if kind == "edge" else 2) or ()


def component_transform(native, owner):
    if owner is None:
        return None
    matrix = native.cast(owner.Transform2, "IMathTransform")
    if matrix is None:
        raise ZJZLError("component_transform_unavailable", f"Could not read transform for {owner.Name2!r}")
    return tuple(matrix.ArrayData)


def _edge_data(native, raw, component_matrix, view_matrix):
    edge = native.cast(raw, "IEdge")
    params = native.cast(edge.GetCurveParams3(), "ICurveParamData")
    curve = native.cast(edge.GetCurve(), "ICurve")
    if params is None or curve is None:
        return None, ()
    start, end = params.StartPoint, params.EndPoint
    if not params.Sense:
        start, end = end, start
    owner_points = (start, end)
    model_points = [point_through(component_matrix, p) for p in owner_points] if component_matrix else owner_points
    row = {
        "curve": "other",
        "curve_type": params.CurveType,
        "endpoints_model_m": [list(p) for p in model_points],
        "endpoints_sheet_m": [point_through(view_matrix, p)[:2] for p in model_points],
    }
    if curve.IsLine():
        row.update(curve="line", length_m=math.dist(*model_points))
    elif curve.IsCircle():
        cx, cy, cz, nx, ny, nz, radius = curve.CircleParams
        center = (cx, cy, cz)
        normal = (nx, ny, nz)
        if component_matrix:
            center = point_through(component_matrix, center)
            normal = [sum(value * basis for value, basis in zip(normal, axis_vector(component_matrix, axis)))
                      for axis in range(3)]
            radius *= component_matrix[12]
        sweep = abs(params.UMaxValue - params.UMinValue)
        ratio = abs(sum(a * b for a, b in zip(normal, axis_vector(view_matrix, 2))))
        row.update(curve="circle" if math.isclose(sweep, math.tau, abs_tol=1e-7) else "arc",
                   center_model_m=list(center), center_sheet_m=point_through(view_matrix, center)[:2],
                   radius_m=radius, sweep_rad=sweep, projected_axis_ratio=min(ratio, 1.0))
    vertices = tuple(zip((edge.GetStartVertex(), edge.GetEndVertex()), model_points))
    return row, vertices


def _reference(path, sheet, view_name, kind, component, identifier):
    if identifier is None:
        return None
    return GeometryReference(drawing_path=path, sheet=sheet, view=view_name,
                             kind=kind, component=component, id=identifier).model_dump()


def _probe(drawing_path, view_name, sheet_name, component_name):
    path = source_path(drawing_path, (".slddrw",))
    native = Native()
    _, _, sheet, extension = drawing_scope(native, path, sheet_name)
    view = named_view(native, sheet, view_name)
    sheet_name = sheet.GetName()
    view_matrix = transform(native, view)
    components = visible_components(native, view)
    if component_name is not None:
        if component_name not in components:
            raise ZJZLError("component_not_visible", f"Component {component_name!r} is not visible in {view_name!r}")
        components = {component_name: components[component_name]}
    edges, vertices, issue_counts = [], [], defaultdict(int)
    for name, (component, owner) in components.items():
        try:
            component_data = component_transform(native, owner)
        except ZJZLError as exc:
            issue_counts[exc.code] += 1
            continue
        endpoints = defaultdict(list)
        for raw in visible_entities(view, component, "edge"):
            identifier = persist(extension, raw)
            result = optional(lambda: _edge_data(native, raw, component_data, view_matrix))
            row, candidates = result if result else (None, ())
            if row is None:
                issue_counts["unreadable_curve"] += 1
                row = {"curve": "unavailable"}
            ref = _reference(path, sheet_name, view_name, "edge", name, identifier)
            issue_counts["unavailable_reference"] += ref is None
            edges.append({"component": name, "reference": ref, **row})
            for vertex, point in candidates:
                vertex_id = persist(extension, vertex) if vertex is not None else None
                if vertex_id:
                    endpoints[vertex_id].append(point)
        for raw in visible_entities(view, component, "vertex"):
            identifier = persist(extension, raw)
            ref = _reference(path, sheet_name, view_name, "vertex", name, identifier)
            points = endpoints.get(identifier, ())
            point = points[0] if points and all(math.dist(p, points[0]) < 1e-8 for p in points) else None
            issue_counts["unavailable_reference"] += ref is None
            issue_counts["unverified_vertex_position"] += point is None
            vertices.append({"component": name, "reference": ref,
                             "point_model_m": list(point) if point is not None else None,
                             "point_sheet_m": point_through(view_matrix, point)[:2] if point is not None else None})
        silhouettes = optional(lambda: view.GetVisibleEntityCount2(component, 4))
        if silhouettes:
            issue_counts["silhouette_entities_excluded"] += silhouettes
    issues = {key: count for key, count in issue_counts.items() if count}
    return {"drawing_path": path, "sheet": sheet_name, "view": view_name,
            "configuration": view.ReferencedConfiguration, "edges": edges, "vertices": vertices,
            "status": "partial" if issues else "complete", "issues": issues}


def projected_span(native, view, components, references, entities, axis):
    view_matrix = transform(native, view)
    if references[0].kind == "edge":
        owner = components[references[0].component][1]
        info, _ = _edge_data(native, entities[0], component_transform(native, owner), view_matrix)
        points = info["endpoints_sheet_m"]
    else:
        points = []
        for reference, entity in zip(references, entities):
            component, owner = components[reference.component]
            owner_matrix = component_transform(native, owner)
            candidates = []
            for raw in visible_entities(view, component, "edge"):
                result = optional(lambda: _edge_data(native, raw, owner_matrix, view_matrix))
                if result:
                    candidates.extend(point for vertex, point in result[1]
                                      if vertex is not None and native.app.IsSame(entity, vertex) == 1)
            if not candidates or any(math.dist(p, candidates[0]) > 1e-8 for p in candidates):
                raise ZJZLError("dimension_verification_failed", "Could not verify the projected vertex endpoints")
            points.append(point_through(view_matrix, candidates[0])[:2])
    span = math.dist(*points) if axis is None else abs(points[0][axis] - points[1][axis])
    return span / view_matrix[12] * 1000


def resolve_visible(native, extension, view, components, reference, path, sheet_name, view_name):
    if (path_key(reference.drawing_path) != path_key(path) or reference.sheet != sheet_name
            or reference.view != view_name or reference.component not in components):
        raise ZJZLError("reference_scope_mismatch", "The reference belongs to a different drawing, sheet, view or occurrence")
    raw, state = extension.GetObjectByPersistReference3(read_reference(reference.id))
    if raw is None or state != 0:
        raise ZJZLError("stale_reference", f"SolidWorks could not resolve the reference (state {state})")
    component = components[reference.component][0]
    for entity in visible_entities(view, component, reference.kind):
        if native.app.IsSame(raw, entity) == 1:
            return entity
    raise ZJZLError("stale_reference", "The referenced entity is no longer visible in the requested view")


def probe_geometry(drawing_path, view_name, sheet, component):
    return execute(_probe, drawing_path, view_name, sheet, component)
