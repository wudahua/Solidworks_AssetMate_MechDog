"""Typed SolidWorks COM helpers: interface casting, document scoping, transactions.

Why this module exists (2026-11-28, ZJZL adoption batch 1)
---------------------------------------------------------
This toolkit talks to SolidWorks through LATE-BOUND dispatch almost everywhere. Three
independent failures in the last month had the same shape — a method that the type library
declares, but that late binding cannot resolve:

    InsertBOMBalloon2   on IModelDoc      (v0.8.364)
    InsertWeldSymbol3   on IModelDoc      (v0.8.365)
    InsertModelAnnotations4 on IDrawingDoc (v0.8.366)
    GetCurveParams3     on IEdge          (measured 2026-11-28: "找不到成员")

Each one was first written down as "this build does not support it". They were all resolved
by casting the SAME COM object to the interface that declares the method. The ZJZL pack
(contributed 2026-11-28) makes that casting a first-class primitive instead of a per-call
workaround, and adds two things this toolkit never had:

  * `persist` / `read_reference` — a stable entity id via `GetPersistReference3`, so a
    dimension or balloon can be checked again after save/reopen instead of being trusted;
  * `rollback_annotation` / view rollback — a write that fails verification is removed,
    not left on the sheet.

Attribution: the design is taken from the ZJZL pack (`zjzl_native.py`, baseline v0.8.351).
Adapted here: error codes map onto this toolkit's vocabulary, and `execute()` never hides a
COM-bridge loss.

Nothing in this module writes to a drawing by itself; it is infrastructure for the tools
that follow.
"""
from __future__ import annotations

import base64
import math
import os

from .. import sw_session


class ComScopeError(RuntimeError):
    """A tool-level failure with a machine-readable code.

    Deliberately NOT a COM error: the tools catch this, map `code` into this toolkit's
    receipt vocabulary and return a structured error. A COM error must stay a COM error so
    that `sw_session.is_bridge_lost_error` still recognises it.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def path_key(path):
    """Case-insensitive, symlink-resolved key for comparing document paths."""
    return os.path.normcase(os.path.realpath(path)) if path else None


def source_path(path, extensions):
    """Validate an absolute path with one of `extensions`, return its real path."""
    if not os.path.isabs(path) or os.path.splitext(path)[1].lower() not in extensions:
        raise ComScopeError("bad_input",
                            "Expected an absolute path to %s" % ", ".join(extensions))
    return os.path.realpath(path)


class Native:
    """The typed view of the running SolidWorks session.

    `cast()` is the whole point: `IModelDoc`, `IDrawingDoc`, `IEdge` are three views of the
    same object, and a method only exists on one of them. Late binding resolves by name and
    fails on the others; `QueryInterface` asks the object for the interface that declares
    the method.
    """

    def __init__(self):
        from win32com.client import gencache

        self.dynamic = sw_session.get_sw()
        revision = sw_session.sw_version(self.dynamic)
        if not revision:
            raise ComScopeError("com_unavailable", "SolidWorks did not report a revision")
        major = int(str(revision).split(".")[0])
        self.library = gencache.EnsureModule(
            "{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, major, 0)
        if self.library is None:
            raise ComScopeError("com_typelib_unavailable",
                                "gencache could not load the SolidWorks type library")
        self.app = self.cast(self.dynamic, "ISldWorks")
        self.revision = revision

    def cast(self, obj, interface):
        """Return `obj` as `interface`, or None. Never invents an object."""
        import pythoncom

        if obj is None:
            return None
        cls = getattr(self.library, interface, None)
        if cls is None:
            raise ComScopeError("com_interface_unavailable",
                                "Type library has no interface %r" % interface)
        if isinstance(obj, cls):
            return obj
        dispatch = getattr(obj, "_oleobj_", obj)
        return cls(dispatch.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch))

    def active(self, path, kind):
        """The document at `path` if it is the ACTIVE document, else None."""
        active = self.cast(self.app.ActiveDoc, "IModelDoc2")
        if active is None or active.GetType() != kind:
            return None
        if path_key(active.GetPathName()) != path_key(path):
            return None
        return active

    def document(self, path, kind):
        """Open (or reuse) exactly `path`, and leave it ACTIVE.

        Two failure modes this guards: SolidWorks silently opening a different file (a
        same-named copy), and `open_document` returning a document that is not active — in
        both cases every later "selected entity" would belong to the wrong window.
        """
        current = self.active(path, kind)
        if current is not None:
            return current
        opened = self.cast(self.app.GetOpenDocumentByName(path), "IModelDoc2")
        if opened is None:
            opened = self.cast(sw_session.open_document(path, kind), "IModelDoc2")
        if opened is None or path_key(opened.GetPathName()) != path_key(path) \
                or opened.GetType() != kind:
            raise ComScopeError("document_mismatch",
                                "SolidWorks opened a different document for %s" % path)
        for key in (path, opened.GetTitle()):
            if self.active(path, kind) is not None:
                break
            _activate_quietly(self.dynamic, key)
        current = self.active(path, kind)
        if current is None:
            raise ComScopeError("document_mismatch",
                                "Could not activate the exact document %s" % path)
        return current


def _activate_quietly(sw, key):
    """Activate by path or title; ignore failures so the caller can report one reason."""
    from .drawing import _activate

    try:
        _activate(sw, key)
    except Exception:
        return False
    return True


def optional(call):
    """Run a read that may legitimately be unsupported; return None for those only.

    A lost COM bridge is NOT "unsupported" — it is re-raised so the caller's retry/reconnect
    path still sees it.
    """
    import pythoncom

    try:
        return call()
    except (pythoncom.com_error, AttributeError, TypeError) as exc:
        if sw_session.is_bridge_lost_error(exc) or sw_session.is_transient_com_error(exc):
            raise
        return None


def execute(operation, *args):
    """Run `operation` and turn its outcome into this toolkit's receipt shape."""
    try:
        return sw_session.ok(**operation(*args))
    except ComScopeError as exc:
        return sw_session.error(_map_code(exc.code), str(exc))
    except Exception as exc:
        if sw_session.is_bridge_lost_error(exc):
            try:
                sw_session.reconnect(reason="COM bridge lost during a typed operation")
            except Exception:
                pass
            return sw_session.error("com_bridge_lost", str(exc),
                                    "Reopen and inspect the document before retrying a write.")
        return sw_session.error("com_operation_failed", str(exc))


def _map_code(code):
    """Map the pack's generic codes onto the vocabulary this toolkit already uses."""
    return {
        "bad_input": "bad_input",
        "view_not_found": "view_not_found",
        "sheet_not_found": "sheet_not_found",
        "save_failed": "save_failed",
        "rollback_failed": "rollback_failed",
        "com_unavailable": "com_unavailable",
    }.get(code, code)


def value_of(obj, name, *args):
    """Read `obj.name` whether the build exposes it as a property or a method.

    `ISheet.GetProperties2` is a property here, `IDrawingDoc.GetCurrentSheet` is a method;
    calling one as the other raises `'function' object has no attribute ...`, which reads
    like a missing API. This is the same confusion that cost three rounds before (see the
    module docstring), so it is handled once, here.
    """
    attr = getattr(obj, name)
    if callable(attr) and not isinstance(attr, type):
        return attr(*args)
    return attr


def drawing_scope(native, path, sheet_name):
    """(doc, drawing, sheet, extension) for a drawing, with `sheet` resolved by name."""
    doc = native.document(path, sw_session.SW_DOC_DRAWING)
    drawing = native.cast(doc, "IDrawingDoc")
    if drawing is None:
        raise ComScopeError("document_mismatch", "%s is not a drawing" % path)
    if sheet_name is None:
        sheet = native.cast(value_of(drawing, "GetCurrentSheet"), "ISheet")
    else:
        if sheet_name not in (value_of(drawing, "GetSheetNames") or ()):
            raise ComScopeError("sheet_not_found",
                                "Drawing has no sheet named %r" % sheet_name)
        sheet = native.cast(value_of(drawing, "Sheet", sheet_name), "ISheet")
    if sheet is None:
        raise ComScopeError("sheet_not_found", "Could not read the active sheet")
    extension = native.cast(doc.Extension, "IModelDocExtension")
    return doc, drawing, sheet, extension


def activate_sheet(native, drawing, sheet):
    """Make `sheet` active; a failed activation is an error, not something to write through."""
    name = sheet.GetName()
    current = native.cast(value_of(drawing, "GetCurrentSheet"), "ISheet")
    if current is not None and current.GetName() == name:
        return
    if not value_of(drawing, "ActivateSheet", name):
        raise ComScopeError("sheet_activation_failed",
                            "Could not activate sheet %r" % name)


def model_views(native, sheet):
    """Every non-sheet view on the sheet, as typed IView."""
    views = (native.cast(raw, "IView") for raw in (value_of(sheet, "GetViews") or ()))
    return [view for view in views if view is not None and view.Type != 1]


def named_view(native, sheet, name):
    """Exactly one model view called `name`; ambiguity is an error, not a guess."""
    matches = [view for view in model_views(native, sheet) if view.GetName2() == name]
    if len(matches) != 1:
        raise ComScopeError("view_not_found",
                            "Expected one model view named %r on %r"
                            % (name, sheet.GetName()))
    return matches[0]


def sheet_properties(sheet):
    """(width_m, height_m, first_angle) — 0.8.374.

    `GetProperties2` is a PROPERTY here and returns 8 numbers; this toolkit had been using
    `GetProperties` (7 numbers) because a call on the 8-value form raised. Measured
    2026-11-28 on an A3 GB sheet: [8, 12, 1, 2, 1, 0.42, 0.297, 1.0] — width/height at
    [5]/[6], and the trailing value is the sheet's first/third-angle flag the projection
    tools need. Index 4 is kept in the receipt because which slot carries the flag differs
    between builds; `first_angle` below prefers the last slot and falls back to [4].
    """
    values = list(optional(lambda: value_of(sheet, "GetProperties2")) or ())
    if len(values) < 7:
        values = list(optional(lambda: value_of(sheet, "GetProperties")) or ())
    if len(values) < 7:
        raise ComScopeError("sheet_properties_unavailable",
                            "Could not read the sheet size")
    width, height = float(values[5]), float(values[6])
    flag = None
    if len(values) >= 8:
        flag = bool(values[7])
    elif len(values) >= 5:
        flag = bool(values[4])
    return {"width_m": width, "height_m": height, "first_angle": flag,
            "raw": [float(v) for v in values]}


def sheet_point(sheet, point):
    """Validate a sheet point in metres and return it as a 3-tuple."""
    props = sheet_properties(sheet)
    if len(point) != 2 or not all(math.isfinite(float(v)) for v in point):
        raise ComScopeError("bad_input", "Expected two finite sheet coordinates in metres")
    if not (0 < float(point[0]) < props["width_m"]
            and 0 < float(point[1]) < props["height_m"]):
        raise ComScopeError("outside_sheet",
                            "Position %s lies outside %r" % (list(point), sheet.GetName()))
    return (float(point[0]), float(point[1]), 0.0)


def save_drawing(doc):
    """Save silently and refuse to report success on a non-zero error count."""
    saved, errors, warnings = doc.Save3(1, 0, 0)
    if not saved or errors:
        raise ComScopeError("save_failed",
                            "SolidWorks Save3 returned errors=%s, warnings=%s"
                            % (errors, warnings))
    return int(warnings)


def persist(extension, entity):
    """Stable id for an entity, or None when the build cannot produce one."""
    data = optional(lambda: extension.GetPersistReference3(entity))
    if not data:
        return None
    return base64.urlsafe_b64encode(bytes(data)).decode("ascii")


def read_reference(identifier):
    """Decode a `persist` id back to the byte payload SolidWorks expects."""
    if not identifier or len(identifier) > 65536:
        raise ComScopeError("bad_reference", "Expected a persistent entity identifier")
    try:
        return base64.b64decode(identifier, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ComScopeError("bad_reference", "Invalid persistent entity identifier") from exc


def rollback_annotation(doc, extension, annotation, cause):
    """Delete an annotation whose verification failed; report if the delete failed too."""
    doc.ClearSelection2(True)
    try:
        if not annotation.Select(False) or not extension.DeleteSelection2(0):
            doc.ClearSelection2(True)
            if (not sw_session.call_method(annotation, "Select3", False, None)
                    or not extension.DeleteSelection2(0)):
                raise RuntimeError("SolidWorks did not delete the annotation")
    except Exception as exc:
        raise ComScopeError("rollback_failed",
                            "%s. Annotation cleanup failed: %s" % (cause, exc)) from exc
