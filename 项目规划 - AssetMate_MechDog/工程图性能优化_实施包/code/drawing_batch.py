"""Reference skeleton for scoped SolidWorks drawing batch operations.

This module does not claim a measured speedup. It provides safe preference
restoration and explicit synchronization points. It does not roll back CAD edits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pythoncom

# Verified from the local SW2024 SP4 swconst type library.
SW_INPUT_DIMVAL_ON_CREATE = 10
SW_DRAWING_AUTOMATIC_VIEW_UPDATE = 90
SW_DETAILING_NO_OPTION = 0


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


@dataclass
class BatchOptions:
    suppress_dimension_input: bool = True
    disable_automatic_view_update: bool = True
    # Do not enable this globally: selection-based drawing APIs may depend on graphics.
    disable_graphics_update: bool = False


class DrawingBatch:
    def __init__(self, doc, sw, options: Optional[BatchOptions] = None,
                 save_callback: Optional[Callable[[], None]] = None):
        self.doc = doc
        self.sw = sw
        self.options = options or BatchOptions()
        self.save_callback = save_callback
        self._old_input_dim = None
        self._old_view_update = None
        self._model_view = None
        self._old_graphics_update = None
        self._entered = False
        self.rebuild_count = 0
        self.redraw_count = 0
        self.save_count = 0

    def __enter__(self):
        if self._entered:
            raise RuntimeError("DrawingBatch cannot be entered twice")
        self._entered = True
        if self.options.suppress_dimension_input:
            self._old_input_dim = bool(
                self.sw.GetUserPreferenceToggle(SW_INPUT_DIMVAL_ON_CREATE)
            )
            if self._old_input_dim:
                self.sw.SetUserPreferenceToggle(SW_INPUT_DIMVAL_ON_CREATE, False)
        if self.options.disable_automatic_view_update:
            self._old_view_update = bool(
                self.doc.Extension.GetUserPreferenceToggle(
                    SW_DRAWING_AUTOMATIC_VIEW_UPDATE, SW_DETAILING_NO_OPTION
                )
            )
            # If already False, leave it alone and do not attribute gains to this switch.
            if self._old_view_update:
                self.doc.Extension.SetUserPreferenceToggle(
                    SW_DRAWING_AUTOMATIC_VIEW_UPDATE, SW_DETAILING_NO_OPTION, False
                )
        if self.options.disable_graphics_update:
            self._model_view = self.doc.ActiveView
            self._old_graphics_update = bool(self._model_view.EnableGraphicsUpdate)
            self._model_view.EnableGraphicsUpdate = False
        return self

    def sync(self, redraw=True):
        """Explicit synchronization before geometry-dependent reads or final output."""
        call_method(self.doc, "ForceRebuild3", False)
        self.rebuild_count += 1
        if redraw:
            self.doc.GraphicsRedraw2()
            self.redraw_count += 1

    def save(self):
        if self.save_callback is None:
            raise RuntimeError("No save_callback configured")
        self.save_callback()
        self.save_count += 1

    def __exit__(self, exc_type, exc, tb):
        restore_errors = []
        try:
            if self._old_input_dim is not None:
                self.sw.SetUserPreferenceToggle(
                    SW_INPUT_DIMVAL_ON_CREATE, self._old_input_dim
                )
        except Exception as exc:
            restore_errors.append(str(exc))
        try:
            if self._old_view_update is not None:
                self.doc.Extension.SetUserPreferenceToggle(
                    SW_DRAWING_AUTOMATIC_VIEW_UPDATE,
                    SW_DETAILING_NO_OPTION,
                    self._old_view_update,
                )
        except Exception as exc:
            restore_errors.append(str(exc))
        try:
            if self._model_view is not None and self._old_graphics_update is not None:
                self._model_view.EnableGraphicsUpdate = self._old_graphics_update
        except Exception as exc:
            restore_errors.append(str(exc))
        if restore_errors and exc is None:
            raise RuntimeError("Failed to restore SolidWorks settings: " + repr(restore_errors))
        return False
