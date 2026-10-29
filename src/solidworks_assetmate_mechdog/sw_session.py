"""Shared SolidWorks COM session layer.

Rules baked in from real debugging (see docs/com-quirks.md):
- OpenDoc6 Errors/Warnings must be byref VARIANTs.
- Reuse the already-active document; never reopen blindly (memory vs disk).
- GetComponents is slow: callers fetch once, then group in memory.

pywin32 is imported lazily so the package stays importable off-Windows.
"""
from __future__ import annotations

import os

SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2
SW_DOC_DRAWING = 3
SW_OPEN_SILENT = 1

_SW = None
# Measured: GetComponents costs ~130 s on a 46-component assembly (2026-10-26).
# Cache per document path so tree -> dry-run -> apply -> tree stays interactive.
# Pass refresh=True after the user adds/removes components outside this server.
_COMPONENTS_CACHE = {}


def _com():
    try:
        import pythoncom
        import win32com.client as win
        from win32com.client import VARIANT
    except ImportError as exc:  # non-Windows or pywin32 missing
        raise RuntimeError(
            "pywin32 is required and SolidWorks must run on this Windows machine. "
            "Install with: pip install pywin32"
        ) from exc
    return pythoncom, win, VARIANT


def sw_version(sw=None):
    """Return the SolidWorks revision string, or None.

    Capability conclusions are version-dependent: `InsertBalloon` is UNKNOWNNAME on SW2026
    and absent on SW2024; `InsertBomTable4(Indented)` works on SW2024 SP4 and returns None
    on SW2026. Without the revision in the payload, a failure is indistinguishable from a
    wrong-configuration failure.
    """
    try:
        app = sw or get_sw()
        return str(app.RevisionNumber)
    except Exception:
        return None


def get_sw():
    """Connect to the running SolidWorks instance, or start one. Cached.

    Always wrapped in a DYNAMIC (late-bound) dispatcher on purpose. If
    %TEMP%\\gen_py holds a makepy cache for the SldWorks type library, win32com
    hands back an early-bound wrapper whose OpenDoc6 declares Errors/Warnings as
    plain byref ints and calls int() on our byref VARIANT — every tool dies with
    "int() argument must be ... not 'VARIANT'". Late binding accepts the VARIANT
    and does not depend on whether some other program generated gen_py.
    """
    global _SW
    if _SW is not None:
        return _SW
    _, win, _ = _com()
    from win32com.client import dynamic as dyn

    try:
        obj = win.GetActiveObject("SldWorks.Application")
    except Exception:
        obj = win.Dispatch("SldWorks.Application")
    try:
        _SW = dyn.Dispatch(obj)
    except Exception:
        # Last resort: an early-bound wrapper here would reintroduce the byref
        # VARIANT crash, so make the fallback visible in logs.
        import logging

        logging.getLogger(__name__).warning(
            "win32com.client.dynamic.Dispatch failed; using the raw COM object. "
            "If every tool fails with 'int() argument ... not VARIANT', this is why."
        )
        _SW = obj
    return _SW


def variant(value, vartype):
    pythoncom, _, VARIANT = _com()
    return VARIANT(vartype, value)


def double_array(values):
    """VT_ARRAY | VT_R8 — a plain Python list silently fails for many SW APIs."""
    pythoncom, _, _ = _com()
    return variant(list(values), pythoncom.VT_ARRAY | pythoncom.VT_R8)


def call_method(obj, name, *args):
    """Invoke a SolidWorks method through DISPATCH_METHOD, bypassing pywin32's guesswork.

    win32com decides method-vs-property from the type library, and for a number of
    SolidWorks members it decides WRONG: it dispatches them as PROPERTYGET and the call
    raises `DISP_E_MEMBERNOTFOUND` (0x80020003), which reads exactly like "this API does
    not exist". It does exist. Calling the same DISPID with DISPATCH_METHOD works.

    Confirmed cases (each cost hours before being found):
      * `ModelDoc2.InsertGtol`      — geometric tolerance "impossible" for two days
      * `INote.GetAnnotation`       — notes could not be positioned
      * `IFeature.FirstFeature` / `GetNextFeature` / `GetDefinition` — feature walk

    So: for any of those (and anything that reports MEMBERNOTFOUND on a method that the
    documentation says exists) use this instead of attribute access. `static_check.py`
    fails the build when a known trap is called directly.
    """
    import pythoncom

    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    flag = getattr(obj, "_FlagAsMethod", None)
    if flag is not None:
        try:
            flag(name)
        except Exception:
            pass
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


# Members win32com dispatches as properties although they are methods. Kept here so the
# static check and the helper share one list.
PROPERTY_TRAP_METHODS = (
    "InsertGtol",
    "GetAnnotation",
    "GetAnnotation2",
    "FirstFeature",
    "GetNextFeature",
    "GetDefinition",
    "SetPosition2",
    "Select3",
    "SetScale",
)


def byref_dispatch_null():
    """Null Callout argument for SelectByID2 (byref IDispatch).

    SelectByID2 fails with '类型不匹配' unless the Callout argument is passed
    this way (docs/com-quirks.md, section 3).
    """
    pythoncom, _, _ = _com()
    return variant(None, pythoncom.VT_BYREF | pythoncom.VT_DISPATCH)


_DOC_TYPE_BY_EXT = {
    ".sldprt": SW_DOC_PART,
    ".sldasm": SW_DOC_ASSEMBLY,
    ".slddrw": SW_DOC_DRAWING,
}


def open_document(path, doc_type=None):
    """Open path silently, reusing the active document when it is the same file.

    doc_type defaults to being inferred from the extension. Hard-coding the assembly type
    made `get_mass_properties` and `export_preview` fail on every part with a misleading
    "file may be locked" message, when the real cause was `OpenDoc6` returning None for a
    type mismatch (reported 2026-10-28).
    """
    path = os.path.abspath(path)
    if not os.path.exists(path):
        # FileNotFoundError(path) stringifies to a bare path; keep the reason.
        raise FileNotFoundError(
            "file not found: %s (check drive, folder and extension "
            ".SLDASM/.SLDPRT)" % path
        )
    extension = os.path.splitext(path)[1].lower()
    if doc_type is None:
        doc_type = _DOC_TYPE_BY_EXT.get(extension, SW_DOC_ASSEMBLY)
    sw = get_sw()
    doc = sw.ActiveDoc
    if doc is not None and os.path.normcase(doc.GetPathName) == os.path.normcase(path):
        return doc
    pythoncom, _, _ = _com()
    errors = variant(0, pythoncom.VT_BYREF | pythoncom.VT_I4)
    warnings = variant(0, pythoncom.VT_BYREF | pythoncom.VT_I4)
    doc = sw.OpenDoc6(path, doc_type, SW_OPEN_SILENT, "", errors, warnings)
    if doc is None:
        # Keep the COM detail: errors=2097152 is the type-mismatch branch, and dropping it
        # sent one reporter down two wrong paths (read-only, same-name lock).
        error_code = None
        try:
            error_code = int(errors.value)
        except Exception:
            pass
        hint = ("OpenDoc6 returned null with errors=%s for doc_type=%s (%s). "
                "The most common cause is a document-type mismatch, not a file lock."
                % (error_code, doc_type, extension or "no extension"))
        raise RuntimeError(
            "SolidWorks could not open the document. It may be open read-only "
            "elsewhere, or another file with the same name is already open; "
            "doc_type/extension mismatch is another common cause. %s "
            "(see docs/com-quirks.md, section 2)." % hint
        )
    # A reopened document means previously cached component pointers are stale.
    invalidate_components(path)
    return doc


def get_components(doc, refresh=False):
    """Fetch once per document and cache. Never call this in a loop (quirks section 4).

    Cache validity is checked two ways because a stale IComponent2 does NOT always
    raise after the document is closed and reopened from outside: it may silently
    return empty strings (Name2 == ""). So we require (a) the same COM document
    identity and (b) a non-empty first component name.
    """
    key = os.path.normcase(doc.GetPathName)
    if not refresh:
        entry = _COMPONENTS_CACHE.get(key)
        if entry:
            entry_ole, cached = entry
            same_doc = False
            try:
                same_doc = entry_ole == doc._oleobj_
            except Exception:
                same_doc = False
            if same_doc and cached:
                try:
                    if str(cached[0].Name2 or "").strip():
                        return cached
                except Exception:
                    pass
            _COMPONENTS_CACHE.pop(key, None)
    components = list(doc.GetComponents(False) or [])
    if components:
        try:
            _COMPONENTS_CACHE[key] = (doc._oleobj_, components)
        except Exception:
            pass
    return components


def invalidate_components(path=None):
    if path is None:
        _COMPONENTS_CACHE.clear()
    else:
        _COMPONENTS_CACHE.pop(os.path.normcase(os.path.abspath(path)), None)


def part_name(component_name):
    """'C Linkage-6' -> 'C Linkage' (instance suffix stripped)."""
    head, sep, tail = component_name.rpartition("-")
    return head if sep and tail.isdigit() else component_name


def rgb_of(values):
    if not values:
        return None
    return [round(float(v), 4) for v in values[:3]]


def ok(**payload):
    payload["ok"] = True
    return payload


def error(code, message, hint=None, **extra):
    payload = {"ok": False, "error": code, "message": message}
    if hint:
        payload["hint"] = hint
    if extra:
        payload.update(extra)
    return payload
