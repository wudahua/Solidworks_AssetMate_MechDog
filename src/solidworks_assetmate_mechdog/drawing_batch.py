"""Scoped SolidWorks drawing batch: preference safety + explicit synchronisation (PR2).

Adapted from the reference skeleton in
`项目规划 - AssetMate_MechDog/工程图性能优化_实施包/code/drawing_batch.py`, with the two
switches **off by default** because the A/B on this machine found no measurable gain.

Measured 2026-11-03, 旋转轴, auto_dimension, interleaved (True/False/True/False):

    swInputDimValOnCreate=True   7542 ms, 8253 ms
    swInputDimValOnCreate=False  7812 ms, 7428 ms
    delta on medians:  -441 ms   (the two ranges overlap heavily)

A first single pair suggested -1417 ms; the interleaved repeat showed that was drift, not
effect. So `suppress_dimension_input` defaults to False. **Do not attribute any speedup to
these switches without re-measuring on the machine in question.**

`swDrawingAutomaticViewUpdate` (90) was already False on the drawings we tested, which the
spec explicitly calls out: when it is already off, there is nothing to gain and switching it
must not be reported as a win. `automatic_view_update` therefore also defaults to None =
"leave it alone".

What is NOT optional and is the point of this class:

* **preferences are restored even when the body raises** — a half-finished drawing pipeline
  must not leave the user's SolidWorks in a modified state
* **`sync()` is explicit** — no silent rebuild inside a per-annotation loop
* **`save()` is counted** — so a caller can assert "one save per run"
* **no fake transaction** — this does not roll back CAD edits, and does not claim to
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .sw_session import call_method  # one definition only (0.8.38)

#: Verified on this machine (SW 32.4.0 / SW2024): both are readable as METHODS with one
#: argument on IModelDoc2/SldWorks; (90) also works at application level.
SW_INPUT_DIMVAL_ON_CREATE = 10
SW_DRAWING_AUTOMATIC_VIEW_UPDATE = 90
SW_DETAILING_NO_OPTION = 0


@dataclass
class BatchOptions:
    #: Off by default — measured no gain (see module docstring).
    suppress_dimension_input: bool = False
    #: None = leave the document preference untouched. True/False forces it, and the
    #: previous value is restored on exit.
    automatic_view_update: Optional[bool] = None
    #: Never enable globally: selection-based drawing APIs may depend on graphics.
    disable_graphics_update: bool = False


class DrawingBatch:
    """Restore-safe scope for drawing work. Does not roll back CAD edits."""

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
        self.restore_errors = []
        self.switch_report = {}

    # -- lifecycle ---------------------------------------------------------------
    def __enter__(self):
        if self._entered:
            raise RuntimeError("DrawingBatch cannot be entered twice")
        self._entered = True
        if self.options.suppress_dimension_input:
            try:
                self._old_input_dim = bool(
                    self.sw.GetUserPreferenceToggle(SW_INPUT_DIMVAL_ON_CREATE))
                if self._old_input_dim:
                    self.sw.SetUserPreferenceToggle(SW_INPUT_DIMVAL_ON_CREATE, False)
                self.switch_report["suppress_dimension_input"] = {
                    "was": self._old_input_dim, "now": False}
            except Exception as exc:
                self.switch_report["suppress_dimension_input"] = {"error": str(exc)[:120]}
        if self.options.automatic_view_update is not None:
            try:
                self._old_view_update = bool(
                    self.doc.Extension.GetUserPreferenceToggle(
                        SW_DRAWING_AUTOMATIC_VIEW_UPDATE, SW_DETAILING_NO_OPTION))
                # A switch that is already in the desired state is left alone: changing it
                # would make the receipt claim an intervention that never happened.
                if self._old_view_update != bool(self.options.automatic_view_update):
                    self.doc.Extension.SetUserPreferenceToggle(
                        SW_DRAWING_AUTOMATIC_VIEW_UPDATE, SW_DETAILING_NO_OPTION,
                        bool(self.options.automatic_view_update))
                self.switch_report["automatic_view_update"] = {
                    "was": self._old_view_update,
                    "now": bool(self.doc.Extension.GetUserPreferenceToggle(
                        SW_DRAWING_AUTOMATIC_VIEW_UPDATE, SW_DETAILING_NO_OPTION)),
                    "already_off": not self._old_view_update}
            except Exception as exc:
                self.switch_report["automatic_view_update"] = {"error": str(exc)[:120]}
        if self.options.disable_graphics_update:
            try:
                self._model_view = self.doc.ActiveView
                self._old_graphics_update = bool(self._model_view.EnableGraphicsUpdate)
                self._model_view.EnableGraphicsUpdate = False
                self.switch_report["disable_graphics_update"] = {
                    "was": self._old_graphics_update, "now": False}
            except Exception as exc:
                self.switch_report["disable_graphics_update"] = {"error": str(exc)[:120]}
        return self

    def sync(self, redraw=True):
        """Explicit synchronisation: the only place a rebuild is allowed to happen."""
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
        # Restore in reverse order of application. A failure to restore is LOUD: it means
        # the user's SolidWorks is left in a state they did not choose.
        if self._model_view is not None and self._old_graphics_update is not None:
            try:
                self._model_view.EnableGraphicsUpdate = self._old_graphics_update
            except Exception as exc:
                self.restore_errors.append("graphics: %s" % str(exc)[:120])
        if self._old_view_update is not None:
            try:
                self.doc.Extension.SetUserPreferenceToggle(
                    SW_DRAWING_AUTOMATIC_VIEW_UPDATE, SW_DETAILING_NO_OPTION,
                    self._old_view_update)
            except Exception as exc:
                self.restore_errors.append("view_update: %s" % str(exc)[:120])
        if self._old_input_dim is not None:
            try:
                self.sw.SetUserPreferenceToggle(
                    SW_INPUT_DIMVAL_ON_CREATE, self._old_input_dim)
            except Exception as exc:
                self.restore_errors.append("input_dim: %s" % str(exc)[:120])

        if self.restore_errors and exc is None:
            raise RuntimeError(
                "Failed to restore SolidWorks settings: %r" % (self.restore_errors,))
        return False
