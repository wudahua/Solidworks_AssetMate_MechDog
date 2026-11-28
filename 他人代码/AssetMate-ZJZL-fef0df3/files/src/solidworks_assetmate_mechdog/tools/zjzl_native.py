"""Typed SolidWorks calls on AssetMate's existing COM session."""
from __future__ import annotations

import base64
import math
import os

from .. import sw_session
from .drawing import _activate


class ZJZLError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def path_key(path):
    return os.path.normcase(os.path.realpath(path)) if path else None


def source_path(path, extensions):
    if not os.path.isabs(path) or os.path.splitext(path)[1].lower() not in extensions:
        raise ZJZLError("bad_input", f"Expected an absolute path to {', '.join(extensions)}")
    return os.path.realpath(path)


class Native:
    def __init__(self):
        from win32com.client import gencache

        self.dynamic = sw_session.get_sw()
        revision = sw_session.sw_version(self.dynamic)
        if not revision:
            raise ZJZLError("com_unavailable", "SolidWorks did not report a revision")
        self.library = gencache.EnsureModule(
            "{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, int(revision.split(".")[0]), 0
        )
        self.app = self.cast(self.dynamic, "ISldWorks")

    def cast(self, obj, interface):
        import pythoncom

        if obj is None:
            return None
        cls = getattr(self.library, interface)
        if isinstance(obj, cls):
            return obj
        dispatch = getattr(obj, "_oleobj_", obj)
        return cls(dispatch.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch))

    def active(self, path, kind):
        active = self.cast(self.app.ActiveDoc, "IModelDoc2")
        if active is None or active.GetType() != kind or path_key(active.GetPathName()) != path_key(path):
            return None
        return active

    def document(self, path, kind):
        current = self.active(path, kind)
        if current is not None:
            return current
        opened = self.cast(self.app.GetOpenDocumentByName(path), "IModelDoc2")
        if opened is None:
            opened = self.cast(sw_session.open_document(path, kind), "IModelDoc2")
        if path_key(opened.GetPathName()) != path_key(path) or opened.GetType() != kind:
            raise ZJZLError("document_mismatch", f"SolidWorks opened a different document for {path}")
        for key in (path, opened.GetTitle()):
            if self.active(path, kind) is not None:
                break
            _activate(self.dynamic, key)
        current = self.active(path, kind)
        if current is None:
            raise ZJZLError("document_mismatch", f"Could not activate the exact document {path}")
        return current


def optional(call):
    import pythoncom

    try:
        return call()
    except (pythoncom.com_error, AttributeError, TypeError) as exc:
        if sw_session.is_bridge_lost_error(exc) or sw_session.is_transient_com_error(exc):
            raise
        return None


def execute(operation, *args):
    try:
        return sw_session.ok(**operation(*args))
    except ZJZLError as exc:
        return sw_session.error(exc.code, str(exc))
    except Exception as exc:
        if sw_session.is_bridge_lost_error(exc):
            try:
                sw_session.reconnect(reason="ZJZL COM bridge lost")
            except Exception:
                pass  # The next tool call can reconnect again.
            return sw_session.error("com_bridge_lost", str(exc), "Reopen and inspect the drawing before retrying a write.")
        return sw_session.error("zjzl_operation_failed", str(exc))


def drawing_scope(native, path, sheet_name):
    doc = native.document(path, sw_session.SW_DOC_DRAWING)
    drawing = native.cast(doc, "IDrawingDoc")
    if sheet_name is None:
        sheet = native.cast(drawing.GetCurrentSheet(), "ISheet")
    else:
        if sheet_name not in (drawing.GetSheetNames() or ()):
            raise ZJZLError("sheet_not_found", f"Drawing has no sheet named {sheet_name!r}")
        sheet = native.cast(drawing.Sheet(sheet_name), "ISheet")
    extension = native.cast(doc.Extension, "IModelDocExtension")
    return doc, drawing, sheet, extension


def model_views(native, sheet):
    views = (native.cast(raw, "IView") for raw in (sheet.GetViews() or ()))
    return [view for view in views if view.Type != 1]


def named_view(native, sheet, name):
    matches = [view for view in model_views(native, sheet) if view.GetName2() == name]
    if len(matches) != 1:
        raise ZJZLError("view_not_found", f"Expected one model view named {name!r} on {sheet.GetName()!r}")
    return matches[0]


def activate_sheet(native, drawing, sheet):
    name = sheet.GetName()
    current = native.cast(drawing.GetCurrentSheet(), "ISheet")
    if current.GetName() != name and not drawing.ActivateSheet(name):
        raise ZJZLError("sheet_activation_failed", f"Could not activate sheet {name!r}")


def sheet_point(sheet, point):
    properties = sheet.GetProperties2()
    if len(point) != 2 or not all(math.isfinite(value) for value in point):
        raise ZJZLError("bad_input", "Expected two finite sheet coordinates in metres")
    if not (0 < point[0] < properties[5] and 0 < point[1] < properties[6]):
        raise ZJZLError("outside_sheet", f"Position {list(point)} lies outside {sheet.GetName()!r}")
    return (*point, 0.0)


def save_drawing(doc):
    saved, errors, warnings = doc.Save3(1, 0, 0)  # swSaveAsOptions_Silent
    if not saved or errors:
        raise ZJZLError("save_failed", f"SolidWorks Save3 returned errors={errors}, warnings={warnings}")
    return int(warnings)


def persist(extension, entity):
    data = optional(lambda: extension.GetPersistReference3(entity))
    return base64.urlsafe_b64encode(bytes(data)).decode("ascii") if data else None


def read_reference(identifier):
    if not identifier or len(identifier) > 65536:
        raise ZJZLError("bad_reference", "Expected a ZJZL persistent entity identifier")
    try:
        return base64.b64decode(identifier, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ZJZLError("bad_reference", "Invalid ZJZL persistent entity identifier") from exc


def rollback_annotation(doc, extension, annotation, cause):
    doc.ClearSelection2(True)
    try:
        if not annotation.Select(False) or not extension.DeleteSelection2(0):
            doc.ClearSelection2(True)
            if not sw_session.call_method(annotation, "Select3", False, None) or not extension.DeleteSelection2(0):
                raise RuntimeError("SolidWorks did not delete the annotation")
    except Exception as exc:
        raise ZJZLError("rollback_failed", f"{cause}. Annotation cleanup failed: {exc}") from exc
