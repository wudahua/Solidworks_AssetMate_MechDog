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


def get_sw():
    """Connect to the running SolidWorks instance, or start one. Cached."""
    global _SW
    if _SW is not None:
        return _SW
    _, win, _ = _com()
    try:
        _SW = win.GetActiveObject("SldWorks.Application")
    except Exception:
        _SW = win.Dispatch("SldWorks.Application")
    return _SW


def variant(value, vartype):
    pythoncom, _, VARIANT = _com()
    return VARIANT(vartype, value)


def double_array(values):
    """VT_ARRAY | VT_R8 — a plain Python list silently fails for many SW APIs."""
    pythoncom, _, _ = _com()
    return variant(list(values), pythoncom.VT_ARRAY | pythoncom.VT_R8)


def byref_dispatch_null():
    """Null Callout argument for SelectByID2 (byref IDispatch).

    SelectByID2 fails with '类型不匹配' unless the Callout argument is passed
    this way (docs/com-quirks.md, section 3).
    """
    pythoncom, _, _ = _com()
    return variant(None, pythoncom.VT_BYREF | pythoncom.VT_DISPATCH)


def open_document(path, doc_type=SW_DOC_ASSEMBLY):
    """Open path silently, reusing the active document when it is the same file."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    sw = get_sw()
    doc = sw.ActiveDoc
    if doc is not None and os.path.normcase(doc.GetPathName) == os.path.normcase(path):
        return doc
    pythoncom, _, _ = _com()
    errors = variant(0, pythoncom.VT_BYREF | pythoncom.VT_I4)
    warnings = variant(0, pythoncom.VT_BYREF | pythoncom.VT_I4)
    doc = sw.OpenDoc6(path, doc_type, SW_OPEN_SILENT, "", errors, warnings)
    if doc is None:
        raise RuntimeError(
            "SolidWorks could not open the document. It may be open read-only "
            "elsewhere, or another file with the same name is already open "
            "(see docs/com-quirks.md, section 2)."
        )
    return doc


def get_components(doc, refresh=False):
    """Fetch once per document and cache. Never call this in a loop (quirks section 4).

    The cached COM pointers are validated with a cheap property read; if the
    document was closed they raise and the entry is dropped.
    """
    key = os.path.normcase(doc.GetPathName)
    if not refresh:
        cached = _COMPONENTS_CACHE.get(key)
        if cached:
            try:
                cached[0].Name2
                return cached
            except Exception:
                _COMPONENTS_CACHE.pop(key, None)
    components = list(doc.GetComponents(False) or [])
    if components:
        _COMPONENTS_CACHE[key] = components
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


def error(code, message, hint=None):
    payload = {"ok": False, "error": code, "message": message}
    if hint:
        payload["hint"] = hint
    return payload
