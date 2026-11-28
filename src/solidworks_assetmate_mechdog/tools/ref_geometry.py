"""Visible drawing geometry with scoped, persistent references.

Why this exists (ZJZL adoption batch 2, 2026-11-28)
--------------------------------------------------
Selecting a drawing entity used to be a guessing game: `list_drawing_edges` returns an
index into whatever `GetVisibleEntities2` happened to yield, and that index is only valid
in the same session, on the same view, for the same model state. Every dimension tool then
had to prove after the fact that it had attached to the right geometry — by comparing
positions, which cannot distinguish two coincident edges.

This module reads the geometry ONCE and gives each entity a reference that survives:

    {"drawing_path": ..., "sheet": ..., "view": ..., "kind": "edge",
     "component": None, "id": "<base64 of GetPersistReference3>"}

`ref_dimension.resolve_visible` turns that back into a live entity and verifies scope, so a
dimension can be re-checked after save/reopen instead of trusted.

Endpoint source: typed `IEdge.GetCurveParams3()`. Late-bound access to that method fails with
"找不到成员" on this build (measured 2026-11-28); the typed interface answers 12/12 edges.
When it is unavailable the row says so — it never silently reports a zero-length curve.
"""
from __future__ import annotations

import math
from collections import defaultdict

from pydantic import BaseModel, Field, ValidationError

from .. import sw_session
from . import geometry as geo
from .native_com import (
    ComScopeError, Native, drawing_scope, execute, model_views, named_view, optional,
    path_key, persist, read_reference, source_path, value_of,
)


class GeometryReference(BaseModel, extra="forbid"):
    """A scoped pointer to one visible entity. `id` is the persistent payload."""

    drawing_path: str
    sheet: str
    view: str
    kind: str = Field(pattern="^(edge|vertex)$")
    component: str | None = None
    id: str = Field(min_length=1, max_length=65536)


def _reference(path, sheet, view_name, kind, component, identifier):
    if identifier is None:
        return None
    return GeometryReference(drawing_path=path, sheet=sheet, view=view_name,
                             kind=kind, component=component, id=identifier).model_dump()


def component_transform(native, owner):
    """Model-space placement of one assembly occurrence (None for a part view)."""
    if owner is None:
        return None
    matrix = native.cast(owner.Transform2, "IMathTransform")
    if matrix is None:
        raise ComScopeError("component_transform_unavailable",
                            "Could not read transform for %r" % owner.Name2)
    return tuple(matrix.ArrayData)


def visible_components(native, view):
    """Map drawing occurrences to (occurrence, assembly owner). Part views map to None.

    `IView.GetVisibleComponents` returns drawing occurrences whose `Name2` can carry the
    root-assembly prefix, so matching by name alone is ambiguous; the path must agree too.
    """
    model = native.cast(view.ReferencedDocument, "IModelDoc2")
    if model is None or not view.IsModelLoaded():
        raise ComScopeError("model_unavailable", "The view must reference a loaded model")
    if model.GetType() == 1:
        return {None: (None, None)}
    if model.GetType() != 2:
        raise ComScopeError("model_unavailable", "Expected a part or assembly view")
    assembly = native.cast(model, "IAssemblyDoc")
    owners = {owner.Name2: owner for owner in (
        native.cast(obj, "IComponent2") for obj in (value_of(assembly, "GetComponents", False) or ()))}
    components = {}
    for raw in (value_of(view, "GetVisibleComponents") or ()):
        component = native.cast(raw, "IComponent2")
        name = component.Name2
        keys = {name, name.partition("/")[2]} & owners.keys()
        matching = [owners[key] for key in keys
                    if path_key(owners[key].GetPathName()) == path_key(component.GetPathName())]
        if len(matching) != 1:
            raise ComScopeError("component_scope_unavailable",
                                "Could not uniquely map drawing occurrence %r" % name)
        owner = matching[0]
        if owner.Name2 in components:
            raise ComScopeError("component_scope_unavailable",
                                "Duplicate drawing occurrence for %r" % owner.Name2)
        components[owner.Name2] = (component, owner)
    return components


def visible_entities(view, component, kind):
    return value_of(view, "GetVisibleEntities2", component, 1 if kind == "edge" else 2) or ()


def edge_row(native, raw, component_matrix, view_matrix):
    """One edge as a serialisable row plus its vertices. Never invents endpoints."""
    edge = native.cast(raw, "IEdge")
    if edge is None:
        return None, ()
    params = optional(lambda: edge.GetCurveParams3())
    curve = native.cast(optional(lambda: value_of(edge, "GetCurve")), "ICurve")
    row = {"curve": "other"}
    model_points = []
    if params is not None:
        import win32com.client as win

        params = win.Dispatch(params)
        start = optional(lambda: params.StartPoint)
        end = optional(lambda: params.EndPoint)
        sense = optional(lambda: params.Sense)
        if start is not None and end is not None:
            if sense is False:
                start, end = end, start
            model_points = [tuple(float(v) for v in start), tuple(float(v) for v in end)]
        row["curve_type"] = optional(lambda: params.CurveType)
        row["u_range"] = [optional(lambda: params.UMinValue), optional(lambda: params.UMaxValue)]
    else:
        row["endpoints_source"] = "unavailable"
    if model_points:
        if component_matrix:
            model_points = [tuple(geo.point_through(component_matrix, p)) for p in model_points]
        # MEASURED 2026-11-28: `ICurveParamData.StartPoint/EndPoint` is NOT raw model space
        # on this build — for door.SLDPRT the values span 0.160 x 0.215 m centred on the
        # origin, while `IVertex.GetPoint` returns the model coordinates (0..0.160). The
        # params are the view's LOCAL frame (origin at the view centre), which is also the
        # frame `ModelToViewTransform` translation refers to. Naming them "model" made two
        # consumers disagree by the view offset; the local frame maps to the sheet directly.
        row["endpoints_local_m"] = [list(p) for p in model_points]
        row["endpoints_sheet_m"] = [geo.point_through(view_matrix, p)[:2] for p in model_points]
    if curve is not None:
        if bool(optional(lambda: value_of(curve, "IsLine"))):
            row["curve"] = "line"
            if len(model_points) == 2:
                row["length_m"] = math.dist(*model_points)
        elif bool(optional(lambda: value_of(curve, "IsCircle"))):
            cx, cy, cz, nx, ny, nz, radius = [float(v) for v in curve.CircleParams]
            centre, normal = (cx, cy, cz), (nx, ny, nz)
            if component_matrix:
                centre = tuple(geo.point_through(component_matrix, centre))
                normal = [sum(v * b for v, b in zip(normal, geo.axis_vector(component_matrix, axis)))
                          for axis in range(3)]
                radius *= component_matrix[12]
            u_range = row.get("u_range") or [None, None]
            sweep = None
            if None not in u_range:
                sweep = abs(float(u_range[1]) - float(u_range[0]))
            row.update(curve="circle" if (sweep is not None and math.isclose(sweep, math.tau, abs_tol=1e-7))
                       else "arc",
                       centre_model_m=list(centre),
                       centre_sheet_m=geo.point_through(view_matrix, centre)[:2],
                       radius_m=radius, sweep_rad=sweep,
                       projected_axis_ratio=min(abs(sum(a * b for a, b in zip(
                           normal, geo.axis_vector(view_matrix, 2)))), 1.0))
    vertices = []
    for name in ("GetStartVertex", "GetEndVertex"):
        vertex = optional(lambda: getattr(edge, name))
        if vertex is None:
            continue
        point = optional(lambda: value_of(vertex, "GetPoint"))
        if point is not None:
            point = tuple(float(v) for v in point)
            if component_matrix:
                point = tuple(geo.point_through(component_matrix, point))
            vertices.append((vertex, point))
    return row, vertices


def _probe(drawing_path, view_name, sheet_name, component_name, max_edges, budget_s):
    path = source_path(drawing_path, (".slddrw",))
    native = Native()
    _, _, sheet, extension = drawing_scope(native, path, sheet_name)
    view = named_view(native, sheet, view_name)
    sheet_name = sheet.GetName()
    frame = geo.view_frame(view)
    if frame is None:
        raise ComScopeError("view_transform_unavailable",
                            "Could not read the view transform for %r" % view_name)
    view_matrix = frame["raw"]
    components = visible_components(native, view)
    if component_name is not None:
        if component_name not in components:
            raise ComScopeError("component_not_visible",
                                "Component %r is not visible in %r" % (component_name, view_name))
        components = {component_name: components[component_name]}
    import time as _time

    started = _time.time()
    edges, vertices, issues = [], [], defaultdict(int)
    notes = {}
    truncated = None
    for name, (component, owner) in components.items():
        if truncated and truncated.startswith("budget_s"):
            break
        try:
            matrix = component_transform(native, owner)
        except ComScopeError as exc:
            issues[exc.code] += 1
            continue
        by_vertex = defaultdict(list)
        # `IEdge.GetStartVertex` answers only for some edges on this build (measured
        # 2026-11-28: circular edges give an object, plain lines give None). Endpoints from
        # `GetCurveParams3` are always there, so vertices are also matched by COORDINATE —
        # a shared corner has the same point from every edge that meets it, so the match is
        # unambiguous.
        local_points = []
        for raw in visible_entities(view, component, "edge"):
            if len(edges) >= int(max_edges):
                truncated = "max_edges=%d reached" % int(max_edges)
                break
            if _time.time() - started > float(budget_s):
                truncated = "budget_s=%.0f exceeded" % float(budget_s)
                break
            identifier = persist(extension, raw)
            result = optional(lambda: edge_row(native, raw, matrix, view_matrix))
            row, candidates = result if result else (None, ())
            if row is None:
                issues["unreadable_curve"] += 1
                row = {"curve": "unavailable"}
            ref = _reference(path, sheet_name, view_name, "edge", name, identifier)
            issues["unavailable_reference"] += ref is None
            edges.append({"component": name, "reference": ref, **row})
            local_points.extend(tuple(p) for p in (row.get("endpoints_local_m") or []))
            for vertex, point in candidates:
                vertex_id = persist(extension, vertex)
                if vertex_id:
                    by_vertex[vertex_id].append(point)
        model_points = []
        for raw in visible_entities(view, component, "vertex"):
            own = optional(lambda: value_of(raw, "GetPoint"))
            if own is not None:
                own = tuple(float(v) for v in own)
                model_points.append(tuple(geo.point_through(matrix, own)) if matrix else own)
        origin = None
        if local_points and model_points:
            # Both bboxes describe the same visible geometry, so their centres differ by
            # exactly the view's model origin. Derived, not assumed: if a view is clipped the
            # two clips agree and the difference still cancels.
            def _centre(points):
                return [sum(p[axis] for p in points) / len(points) / 2.0
                        + (min(p[axis] for p in points) + max(p[axis] for p in points)) / 2.0 / 2.0
                        for axis in range(3)]
            mc = [(min(p[a] for p in model_points) + max(p[a] for p in model_points)) / 2
                  for a in range(3)]
            lc = [(min(p[a] for p in local_points) + max(p[a] for p in local_points)) / 2
                  for a in range(3)]
            origin = [mc[a] - lc[a] for a in range(3)]
        for raw in visible_entities(view, component, "vertex"):
            if _time.time() - started > float(budget_s):
                truncated = truncated or ("budget_s=%.0f exceeded during vertices"
                                          % float(budget_s))
                break
            identifier = persist(extension, raw)
            ref = _reference(path, sheet_name, view_name, "vertex", name, identifier)
            own = optional(lambda: value_of(raw, "GetPoint"))
            model_point = None
            if own is not None:
                model_point = tuple(float(v) for v in own)
                if matrix:
                    model_point = tuple(geo.point_through(matrix, model_point))
            local_point = None
            if model_point is not None and origin is not None:
                local_point = tuple(model_point[a] - origin[a] for a in range(3))
            issues["unavailable_reference"] += ref is None
            issues["vertex_sheet_position_unavailable"] += local_point is None
            vertices.append({"component": name, "reference": ref,
                             "point_model_m": list(model_point) if model_point is not None else None,
                             "point_local_m": list(local_point) if local_point is not None else None,
                             "point_sheet_m": geo.point_through(view_matrix, local_point)[:2]
                             if local_point is not None else None})
        silhouettes = optional(lambda: value_of(view, "GetVisibleEntityCount2", component, 4))
        if silhouettes:
            # A design fact, not a defect: silhouette entities are the projected outline and
            # deliberately not offered for selection. Counting it as an issue made every
            # ordinary view report `partial`.
            notes["silhouette_entities_excluded"] = (
                notes.get("silhouette_entities_excluded", 0) + int(silhouettes))
    return {"drawing_path": path, "sheet": sheet_name, "view": view_name,
            "configuration": view.ReferencedConfiguration,
            "coordinate_frame": {
                "endpoints": "view_local", "vertices": "model",
                "model_origin_in_local_m": [round(v, 6) for v in origin] if origin else None,
                "note": ("`ICurveParamData` 给的是视图局部坐标；`IVertex.GetPoint` 给的是模型坐标。"
                         "两者相差的就是视图原点，本回执按包围盒中心反推，"
                         "以便 `point_*` 三个字段可以直接对比。")},
            "frame": {
                "scale": frame["scale"],
                "translation_m": [round(v, 6) for v in frame["translation_m"]]},
            "edges": edges, "vertices": vertices,
            "truncated": bool(truncated),
            "truncated_reason": truncated,
            "status": "partial" if (issues or truncated) else "complete",
            "issues": {key: count for key, count in issues.items() if count},
            "notes": notes}


def probe_geometry(drawing_path, view_name, sheet=None, component=None,
                   max_edges=400, budget_s=90.0):
    """Every visible edge/vertex in one view, each with a scoped persistent reference.

    `max_edges` / `budget_s` are not politeness: a `GetPersistReference3` per entity is one
    COM round trip, and measured 2026-11-28 a cabinet assembly view had not returned after
    six minutes (the monitor bracket's 165 edges take seconds). Truncation is REPORTED —
    `truncated: true` plus what stopped it — so a caller never mistakes a partial scan for a
    small view.
    """
    return execute(_probe, drawing_path, view_name, sheet, component, max_edges, budget_s)


def reference_from(payload):
    """Validate a caller's reference dict into GeometryReference, or raise a clear error."""
    try:
        return GeometryReference.model_validate(payload)
    except ValidationError as exc:
        raise ComScopeError("bad_reference",
                            "Invalid geometry reference: %s" % exc.errors()[0].get("msg")) from exc


def resolve_visible(native, extension, view, components, reference, path, sheet_name, view_name):
    """Turn a reference back into a live entity, refusing any scope mismatch."""
    if (path_key(reference.drawing_path) != path_key(path) or reference.sheet != sheet_name
            or reference.view != view_name or reference.component not in components):
        raise ComScopeError("reference_scope_mismatch",
                            "The reference belongs to a different drawing, sheet, view or occurrence")
    raw, state = extension.GetObjectByPersistReference3(read_reference(reference.id))
    if raw is None or state != 0:
        raise ComScopeError("stale_reference",
                            "SolidWorks could not resolve the reference (state %s)" % state)
    component = components[reference.component][0]
    for entity in visible_entities(view, component, reference.kind):
        if native.app.IsSame(raw, entity) == 1:
            return entity
    raise ComScopeError("stale_reference",
                        "The referenced entity is no longer visible in the requested view")
