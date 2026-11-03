"""Drawing creation, export, render and audit.

Verified on SolidWorks 2024 SP4 Chinese UI (2026-10-27). Traps that cost hours:

- sw.NewDocument(template, ...) returns None when the template path uses forward
  slashes. Windows backslashes only.
- Named views follow the UI language: Chinese builds answer `*前视`, not `*Front`.
  Always resolve the view name from the model's own GetModelViewNames.
- IDrawingDoc.GetViews / GetFirstView / GetOutline are PROPERTIES under late
  binding, not methods. GetViews returns a nested tuple of raw PyIDispatch.
- Coordinates are meters (A3 = 0.420 x 0.297).

Scope: geometry + views + export + audit. Dimensioning helpers live elsewhere and
are deliberately separate so a failed annotation never blocks a usable drawing.
"""
from __future__ import annotations

import contextlib
import copy
import glob
import os
import time

from .. import perf_hooks
from ..sw_session import (
    SW_DOC_DRAWING,
    SW_DOC_PART,
    call_method,
    error,
    ok,
    open_document,
    variant,
)

# `ok` is also used as a local variable name in some functions; keep the helper
# reachable under an unambiguous alias.
drawing_ok = ok

# `_call_method` was referenced by the duplicate/filter delete passes but never defined or
# imported here — every `_call_method(doc, "EditDelete")` raised NameError, was swallowed by
# the surrounding `except`, and surfaced as "N 条重复尺寸删除失败" (measured 23/23 failures).
# Keep the alias so the two delete passes actually call the retrying COM wrapper.
_call_method = call_method

# UI-language aliases, checked in order against the model's own view names.
VIEW_ALIASES = {
    "front": ("*前视", "*Front"),
    "back": ("*后视", "*Back"),
    "left": ("*左视", "*Left"),
    "right": ("*右视", "*Right"),
    "top": ("*上视", "*Top"),
    "bottom": ("*下视", "*Bottom"),
    "isometric": ("*等轴测", "*Isometric"),
}
# View insertion points on an A3 sheet (meters). Center of each view.
POSITIONS = {
    "front": (0.105, 0.150),
    "top": (0.105, 0.250),
    "right": (0.295, 0.150),
    "left": (0.295, 0.150),
    "back": (0.105, 0.060),
    "bottom": (0.105, 0.060),
    "isometric": (0.300, 0.235),
}
SUPPORTED_FORMATS = {".pdf", ".dwg", ".dxf"}


def _activate(sw, path):
    import pythoncom

    errors = variant(0, pythoncom.VT_BYREF | pythoncom.VT_I4)
    try:
        return sw.ActivateDoc3(path, False, 0, errors)
    except Exception:
        return None


def _find_template(paper):
    """Locate a drawing template without relying on user preferences."""
    want = paper.lower()
    roots = [
        r"C:\ProgramData\SOLIDWORKS",
        r"C:\ProgramData\SolidWorks",
    ]
    found = []
    for root in roots:
        found += glob.glob(os.path.join(root, "**", "*.drwdot"), recursive=True)
    if not found:
        return None
    for path in found:
        base = os.path.basename(path).lower()
        if base in ("gb_%s.drwdot" % want, "%s.drwdot" % want):
            return path
    return sorted(found)[0]


def _new_document(sw, template, attempts=8, delay=0.5):
    doc = None
    for _ in range(attempts):
        doc = sw.NewDocument(template, 0, 0, 0)
        if doc is not None:
            return doc
        time.sleep(delay)
    return None


def _iter_views(drawing):
    """Flatten GetViews (nested tuple of raw dispatch pointers) into IView objects."""
    import win32com.client as win

    flat = []

    def collect(obj):
        if isinstance(obj, (tuple, list)):
            for item in obj:
                collect(item)
        elif obj is not None:
            flat.append(win.Dispatch(obj))

    try:
        collect(drawing.GetViews)
    except Exception:
        return []
    return flat


def _place_views(views, sheet_w, sheet_h, gap=0.022, safety=None, pad=0.0):
    """Repack views using their REAL projected outlines so nothing overlaps.

    The bounding-box estimate used before creation is only good enough to pick a
    scale; an isometric view projects wider than the box, which is how views ended
    up overlapping. Here we read each view's actual outline and pack them into one
    or two columns, then move them with IView.Position (needs a VT_ARRAY|VT_R8
    VARIANT — a plain tuple is silently misread as (0, y)).
    """
    import pythoncom
    from win32com.client import VARIANT

    if safety:
        left = float(safety.get("left", sheet_w * 0.08))
        right = float(safety.get("right", sheet_w * 0.03))
        top = float(safety.get("top", sheet_h * 0.03))
        bottom = float(safety.get("bottom", sheet_h * 0.20))
    else:
        left, right = sheet_w * 0.08, sheet_w * 0.03
        top, bottom = sheet_h * 0.03, sheet_h * 0.20

    # Two insets were in play: `template_safe_area` reports top=4 mm for this template while
    # the dimension stack uses a 10 mm inset — so views and dimensions disagreed about where
    # the printable area starts, and the view top landed at 0.293 on a 0.297 sheet. Take the
    # stricter of the two.
    inset_floor = 0.010
    left = max(left, inset_floor)
    right = max(right, inset_floor)
    top = max(top, inset_floor)
    bottom = max(bottom, inset_floor)

    # Reserve room for dimension text: the projected outline covers geometry only, so a
    # view placed flush against the parts list has its dimensions drawn over the border.
    # P2-13: deflate GetOutline first — it reports every view 11.9 mm wider and taller
    # than the ink, which packs views further apart than they need to be.
    boxes = []
    for view in views:
        try:
            o = precise_outline(view) or list(view.GetOutline or [])
            boxes.append([view, (o[2] - o[0]) + pad, (o[3] - o[1]) + pad])
        except Exception:
            continue
    if not boxes:
        return {"fits": False, "columns": 0}

    avail_w = sheet_w - left - right
    avail_h = sheet_h - top - bottom
    columns = [[]]
    for cols in (2, 1):
        trial = [[] for _ in range(cols)]
        heights = [0.0] * cols
        for box in sorted(boxes, key=lambda b: -b[2]):
            index = heights.index(min(heights))
            trial[index].append(box)
            heights[index] += box[2] + gap
        total_w = sum(max((b[1] for b in col), default=0) for col in trial) + gap * (cols - 1)
        total_h = max(heights) if heights else 0.0
        columns = trial
        if total_w <= avail_w and total_h <= avail_h:
            break

    # GB templates keep a parts-list column on the left and a title block bottom-right;
    # staying inside the printable area is not enough — views must avoid those tables.
    x = left + pad / 2
    # Start a hair below the printable edge. `sheet_h - top` puts the first view's top
    # exactly ON that edge, and SW's own outline is a few mm bigger than the ink, so the
    # measured top came out at 0.293 on a 0.297 sheet (safe edge 0.287).
    frame_pad = 0.004
    for col in columns:
        col_w = max((b[1] for b in col), default=0.0)
        y = sheet_h - top - frame_pad - pad / 2
        for view, width, height in col:
            center_x = x + width / 2
            center_y = y - height / 2
            try:
                view.Position = VARIANT(
                    pythoncom.VT_ARRAY | pythoncom.VT_R8, [center_x, center_y]
                )
            except Exception:
                pass
            y -= height + gap
        x += col_w + gap

    fits = True
    for view, _, _ in boxes:
        try:
            o = list(view.GetOutline or [])
            if o[0] < 0 or o[1] < 0 or o[2] > sheet_w or o[3] > sheet_h:
                fits = False
        except Exception:
            fits = False
    return {"fits": fits, "columns": len(columns)}


def _relayout(doc):
    """Repack views after annotation: dimension text extends a view's occupancy.

    A view whose outline fitted before auto_dimension can clip its dimensions
    against the sheet border afterwards, so annotation steps re-run the packer.
    """
    views, sheets = [], []
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            sheets.append(view)
        else:
            views.append(view)
    if not sheets or not views:
        return None
    try:
        o = list(sheets[0].GetOutline)
        w, h = o[2] - o[0], o[3] - o[1]
        result = _place_views(views, w, h, safety=template_safe_area(sheets[0], w, h))
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        return result
    except Exception:
        return None


def _model_view_name(model, semantic):
    try:
        names = list(model.GetModelViewNames or ())
    except Exception:
        names = []
    for alias in VIEW_ALIASES.get(semantic, ()):
        if alias in names:
            return alias
    return None


def _model_box(model):
    """Model bounding box in metres, or None.

    IPartDoc.GetPartBox works for parts; on an assembly it can raise or return zeros, and
    the old code then silently used scale 1.0 for a 1.69 m machine (reported as D-3).
    Falls back to the union of component boxes so auto_fit has something to work with.
    """
    def _valid(value):
        try:
            values = list(value or ())
            if len(values) < 6:
                return None
            box = (abs(values[3] - values[0]), abs(values[4] - values[1]),
                   abs(values[5] - values[2]))
            return box if box[0] > 0.001 and box[1] > 0.001 else None
        except Exception:
            return None

    try:
        found = _valid(model.GetPartBox(True))
        if found:
            return found
    except Exception:
        pass
    # Assembly path: union of visible component boxes.
    try:
        components = list(model.GetComponents(False) or ())
    except Exception:
        components = []
    low, high = None, None
    for component in components:
        try:
            import win32com.client as win

            comp = win.Dispatch(component)
            box = _valid(comp.GetBox(False, False))
            if box is None:
                continue
            values = list(comp.GetBox(False, False))
            if low is None:
                low = [values[0], values[1], values[2]]
                high = [values[3], values[4], values[5]]
            else:
                low = [min(low[i], values[i]) for i in range(3)]
                high = [max(high[i], values[i + 3]) for i in range(3)]
        except Exception:
            continue
    if low is not None:
        box = (abs(high[0] - low[0]), abs(high[1] - low[1]), abs(high[2] - low[2]))
        if box[0] > 0.001 and box[1] > 0.001:
            return box
    return None


def _standard_scale(fit, tolerance=0.05):
    """Largest standard drawing scale that still fits, within a small overshoot.

    GB/T 14690 preferred series; 1:2.5 (0.4) was missing, so anything between 1:2 and 1:5
    snapped down to 1:5 — a report got a 210 mm part at 1:5 on an A3 with 90% blank.
    A strict `candidate <= fit` was the other half of the problem: raw_fit 0.1934 against
    a 0.2 candidate is a 3.4% overshoot, which `_fit_scale`'s double-counted gap easily
    absorbs, yet the strict test dropped a whole step to 1:10. The caller still gets the
    post-build `auto_scale_adjustments` stepper if the guess was too generous.
    """
    for candidate in (1.0, 0.5, 0.4, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002):
        if candidate <= fit * (1.0 + float(tolerance)) + 1e-9:
            return candidate
    return 0.002


OUTLINE_MARGIN_M = 0.0119  # measured constant: GetOutline is 11.9 mm larger per side


def precise_outline(view):
    """Projected outline in SHEET metres with the fixed GetOutline inflation removed.

    P2-13: `IView.GetOutline` inflates width AND height by a constant 11.9 mm on every
    view at every scale (measured on four views at 1:1 and 2:1 — front 59x28 reads
    70.9x39.9, top 59x80 reads 70.9x91.9, etc.). It is a fixed offset, not a projection
    error, so deflating by that constant recovers the real ink box at O(1) cost.

    This is the single source of truth for packing / overlap / out-of-sheet / hit-testing.
    A per-vertex "exact" variant is deliberately NOT provided: every route to real
    geometry points was measured dead on 2026-10-31 (a 31-face valve body):
      * `IView.GetVisibleEntities2(comp, 1)` returns SILHOUETTE edges — GetStartVertex
        returns None, only GetCurve has a value;
      * `IBody2.GetVertices` returns None for a body whose edges are all closed circles;
      * `IEdge.GetStartVertex` returns None through call_method as well;
      * `ICurve` on this build has no GetEndPoint / GetStartPoint / GetTessPoints.
    So deflation is not a shortcut, it is the only workable route. Accuracy measured
    against the rendered ink box: 58.98 mm vs 59.2 mm true (0.4%).

    Returns [x0, y0, x1, y1] in the same sheet frame as GetOutline, or None when it
    cannot be deflated safely (callers then fall back to the inflated but valid box).
    """
    try:
        o = list(view.GetOutline or [])
    except Exception:
        return None
    if len(o) < 4:
        return None
    m = OUTLINE_MARGIN_M / 2.0
    w = o[2] - o[0] - OUTLINE_MARGIN_M
    h = o[3] - o[1] - OUTLINE_MARGIN_M
    if w <= 0 or h <= 0:
        # The view is smaller than the inflation margin itself (a tiny detail view or
        # a blank one). Deflating would invert or collapse the box, which would make
        # _place_views stack views on top of each other — worse than the phantom margin.
        # Signal "cannot deflate" so every caller keeps the original GetOutline box.
        return None
    return [o[0] + m, o[1] + m, o[2] - m, o[3] - m]


def _dispatch_specific(annotation):
    """Resolve an annotation to its IDisplayDimension (or None)."""
    import win32com.client as win
    try:
        return win.Dispatch(annotation.GetSpecificAnnotation)
    except Exception:
        return None


@perf_hooks.timed("drawing_create")
def create_drawing(
    model_path,
    output_path=None,
    template=None,
    views=("front", "top", "isometric"),
    scale=None,
    auto_fit=True,
    save=True,
    view_scales=None,
    paper="A3",
    view_positions=None,
):
    """Create a native .SLDDRW from a part/assembly, add model views, optionally save.

    views: any of front/back/left/right/top/bottom/isometric. View names are
    resolved against the model, so Chinese and English SolidWorks both work.
    scale=None with auto_fit picks a standard scale from the model's projected size
    (1:1, 1:2, 1:5 ...); pass an explicit scale to override.

    view_positions: explicit sheet centres in METRES, one per view, e.g.
    [[0.12, 0.20], [0.12, 0.08], [0.30, 0.20], [0.30, 0.08]] for a 2x2 layout.
    When given, the automatic packer is skipped entirely — a 4-view sheet previously
    fell back to a single vertical column and pushed the last views outside the frame.
    """
    try:
        from ..sw_session import get_sw

        model_path = os.path.abspath(model_path)
        if not os.path.exists(model_path):
            raise FileNotFoundError("file not found: %s" % model_path)
        if os.path.splitext(model_path)[1].lower() not in (".sldprt", ".sldasm"):
            return error("bad_input", "model_path must be .SLDPRT or .SLDASM",
                         "Drawings are created FROM a model; pass the model, not a drawing.")

        template = os.path.abspath(template) if template else _find_template(str(paper).upper())
        if not template or not os.path.exists(template):
            return error("template_not_found",
                         "No drawing template found. Pass template=<path to .drwdot>.",
                         "Look under C:\\ProgramData\\SOLIDWORKS\\<version>\\templates\\*.drwdot")
        # Forward slashes make sw.NewDocument silently return None.
        template = template.replace("/", "\\")

        sw = get_sw()
        is_assembly = not model_path.lower().endswith(".sldprt")
        model = open_document(model_path, SW_DOC_PART if not is_assembly else 2)

        # A lightweight assembly draws blank views: all three came out 35.7 x 25.2 mm,
        # identical, with no geometry. Resolving the components before the first view
        # exists is what makes the drawing contain the machine rather than a placeholder.
        lightweight_unresolved = None
        if is_assembly:
            # ResolveAllLightweightComponents returns 0 when everything loaded, 2 when some
            # components stayed lightweight. Views drawn from an unresolved assembly come
            # out blank (all of them identical at 35.7 x 25.2 mm), so record the outcome
            # instead of assuming the resolve worked.
            for _attempt in range(2):
                try:
                    lightweight_unresolved = model.ResolveAllLightweightComponents(True)
                except Exception:
                    lightweight_unresolved = None
                    break
                try:
                    model.ForceRebuild3(False)
                except Exception:
                    pass
                if lightweight_unresolved == 0:
                    break

        # P1 of a 2026-11-03 report: this guard used to run AFTER `_new_document`, so every
        # time it fired it left a brand-new, never-saved drawing behind (GetPathName == '').
        # Nothing ever closed it: both cleanup paths in smoke.py match on GetPathName, and
        # `dirname('') == ''` matches nothing. The blank drawing also holds a reference to the
        # source part, so ONE leak pinned TWO documents, and it accumulated on every run.
        #
        # Checking before the document exists is the only way to not create it at all.
        if save and output_path:
            _early_out = os.path.abspath(output_path)
            if os.path.splitext(_early_out)[1].lower() != ".slddrw":
                _early_out += ".SLDDRW"
            try:
                from ..sw_session import get_sw as _get_sw
                _open_holders = []
                for _doc in (_get_sw().GetDocuments or ()):
                    try:
                        if os.path.normcase(str(_doc.GetPathName)) == os.path.normcase(_early_out):
                            _open_holders.append(str(_doc.GetTitle))
                    except Exception:
                        continue
                if _open_holders:
                    return error(
                        "path_locked_by_open_doc",
                        "target path is already open as %s" % ", ".join(_open_holders),
                        "Close that document first (close_document), or pass a different "
                        "output_path. Nothing was created — this check now runs BEFORE the "
                        "drawing exists, so no blank document is left behind.",
                        path=_early_out, open_documents=_open_holders, saved=False)
            except Exception:
                pass

        drw = _new_document(sw, template)
        if drw is None:
            return error("drawing_create_failed",
                         "SolidWorks NewDocument returned null for %s" % template,
                         "Check the template is valid and uses a Windows path with backslashes.")

        sheet = drw.GetFirstView
        outline = list(sheet.GetOutline or [])
        sheet_w = (outline[2] - outline[0]) if outline else 0.42
        sheet_h = (outline[3] - outline[1]) if outline else 0.297
        # Must be measured now: after views exist, GetLines3 also returns their
        # geometry and the title-block / parts-list detection breaks down.
        safety = template_safe_area(sheet, sheet_w, sheet_h)

        # Size the model from its bounding box so the scale is known BEFORE the first
        # view is inserted — a brand-new view reports no outline, so measuring it and
        # moving it afterwards fights the layout.
        box = _model_box(model)

        chosen_scale = float(scale) if scale else None
        # Usable region from the template itself (left band, title block, parts list).
        # Hard-coding 0.12/0.10 here and 0.05/0.03/0.245 below is what made the predicted
        # layout disagree with where SolidWorks actually placed the views (reported twice:
        # D-3 then again for assemblies).
        safe_x0 = float(safety.get("left", sheet_w * 0.14))
        safe_x1 = sheet_w - float(safety.get("right", sheet_w * 0.01))
        safe_y0 = float(safety.get("bottom", sheet_h * 0.22))
        safe_y1 = sheet_h - float(safety.get("top", sheet_h * 0.012))
        avail_w = max(safe_x1 - safe_x0, 0.05)
        avail_h = max(safe_y1 - safe_y0, 0.05)
        gap = 0.022

        # `box` is the MODEL size, but a real view outline carries a constant 11.9 mm of
        # phantom margin per side (OUTLINE_MARGIN_M). Ignoring it made every prediction
        # ~24 mm short in each direction, so views landed on the frame: measured view top
        # 0.293 vs a 0.287 safe edge on a 0.297 sheet.
        inflate = 2.0 * OUTLINE_MARGIN_M

        def _fit_scale(candidate_box):
            width_need = (candidate_box[0] + inflate) + gap + (candidate_box[2] + inflate)
            height_need = (candidate_box[1] + inflate) + gap + (candidate_box[2] + inflate)
            return min(avail_w / max(width_need, 1e-6),
                       avail_h / max(height_need, 1e-6), 1.0)

        # Why this scale: callers had no way to tell "chosen because the model is big"
        # from "chosen by accident" — a report saw a 210 mm part land at 1:5 with the
        # sheet 90% empty and `auto_scale_adjustments: []` as the only clue.
        scale_reason = None
        if chosen_scale is None and auto_fit and box and box[0] > 0 and box[1] > 0:
            raw_fit = _fit_scale(box)
            chosen_scale = _standard_scale(raw_fit)
            scale_reason = {
                "model_box_m": [round(float(v), 4) for v in box],
                "views": [str(v) for v in views],
                "raw_fit": round(float(raw_fit), 4),
                "standard_scale": chosen_scale,
                "ratio": "1:%g" % round(1.0 / chosen_scale, 3) if chosen_scale else None,
                "rule": "largest standard scale not exceeding raw_fit",
            }
        elif chosen_scale is None:
            chosen_scale = 1.0
            if auto_fit:
                scale_reason = {"standard_scale": 1.0, "rule": "model box unavailable"}

        def _layout(scale_value):
            ww, hh, dd = (box or (0.15, 0.08, 0.08))
            ww, hh, dd = (ww * scale_value + inflate, hh * scale_value + inflate,
                          dd * scale_value + inflate)
            # The grid follows the REQUESTED views. It used to always assume the 2x2 packer
            # (front+top / right+iso), so a single view was placed in the TOP row of a
            # 2-row frame and touched it: measured view top 0.293 vs a 0.287 safe edge.
            # One view -> one cell, two -> one column, three or more -> the 2x2 grid.
            wanted = [v for v in views if str(v).strip().lower() in VIEW_ALIASES]
            count = max(len(wanted), 1)
            cols = 1 if count <= 2 else 2
            rows = 1 if count <= 1 else 2
            total_w = ww if cols == 1 else (ww + gap + dd)
            total_h = hh if rows == 1 else (hh + gap + dd)
            over = (total_w > avail_w + 1e-6) or (total_h > avail_h + 1e-6)
            x_start = safe_x0 + (avail_w - total_w) / 2
            y_start = safe_y0 + (avail_h - total_h) / 2
            c1 = (x_start + total_w / 2) if cols == 1 else (x_start + ww / 2)
            c2 = x_start + ww + gap + dd / 2
            fy = y_start + total_h - hh / 2
            ty = y_start + dd / 2
            return (c1, c2, fy, ty, ww, hh, dd, over)

        # A 1.69 m assembly at 1.0 simply does not fit A3; step down until the predicted
        # layout is inside the sheet instead of emitting an out-of-frame drawing.
        if auto_fit and box and chosen_scale:
            for _ in range(4):
                probe = _layout(chosen_scale)
                if not probe[7]:
                    break
                smaller = _standard_scale(chosen_scale * 0.5)
                if smaller >= chosen_scale:
                    break
                chosen_scale = smaller

        if scale_reason is not None:
            chosen_scale = float(chosen_scale)
            scale_reason["final_scale"] = chosen_scale
            scale_reason["final_ratio"] = "1:%g" % round(1.0 / chosen_scale, 3)
            scale_reason["stepped_down"] = bool(
                scale_reason.get("standard_scale")
                and abs(chosen_scale - float(scale_reason["standard_scale"])) > 1e-9)
        col1, col2, front_y, top_y, w, h, d, _overflow = _layout(chosen_scale)
        positions = {
            "front": (col1, front_y), "back": (col1, front_y),
            "top": (col1, top_y), "bottom": (col1, top_y),
            "right": (col2, front_y), "left": (col2, front_y),
            "isometric": (col2, top_y),
        }

        # Explicit sheet centres win over the automatic packer. Callers were forced to
        # re-place views with raw COM (DISPATCH_PROPERTYPUT + VARIANT array) because a
        # 4-view sheet collapsed into one column; passing positions avoids that entirely.
        explicit_positions = False
        if view_positions:
            keys = [str(v).strip().lower() for v in views]
            for index, pos in enumerate(view_positions):
                if index >= len(keys):
                    break
                try:
                    positions[keys[index]] = (float(pos[0]), float(pos[1]))
                except Exception:
                    continue
            explicit_positions = True

        created, skipped, view_objects = [], [], []
        # Requested vs actual position for every view. Two rounds of predictor fixes did
        # nothing measurable because nothing reported the difference; without this the next
        # round would guess again.
        placement_deltas = []
        for key in [str(v).strip().lower() for v in views]:
            if key not in VIEW_ALIASES:
                skipped.append({"view": key, "reason": "unsupported_view"})
                continue
            view_name = _model_view_name(model, key)
            if not view_name:
                skipped.append({"view": key, "reason": "view_name_not_found"})
                continue
            x, y = positions.get(key, (col1, front_y))
            view = drw.CreateDrawViewFromModelView3(model_path, view_name, x, y, 0)
            if view is None:
                skipped.append({"view": key, "reason": "create_returned_null"})
                continue
            per_view_scale = float((view_scales or {}).get(key, chosen_scale))
            try:
                view.ScaleDecimal = per_view_scale
            except Exception:
                per_view_scale = float(chosen_scale)
            # `CreateDrawViewFromModelView3` IGNORES the x/y arguments on SW2024: asked for
            # (0.0995, 0.1486) and got (0.0995, 0.2393) — the view landed in the top row of
            # the template and its top edge sat on the frame (0.293 vs a 0.287 safe edge).
            # `set_view` moves it correctly (same call put the outline at 0.096..0.204), so
            # place it again after creation. This is why two rounds of predictor fixes
            # changed nothing measurable.
            # `CreateDrawViewFromModelView3` ignores x/y, and writing `Position` on the
            # freshly created view reproduces the v0.8.9 marshalling trap (requested
            # (0.2374, 0.179), actual (0.0, 0.2374)). That is fine: `_place_views` repacks
            # every view afterwards from its REAL outline, and that is the placement that
            # counts. An earlier version logged "pending_post_create_placement" here even
            # though the repack worked — a scary receipt for a working path.
            try:
                o = list(view.GetOutline or [])
                box_m = {"width_m": round(o[2] - o[0], 4), "height_m": round(o[3] - o[1], 4)}
            except Exception:
                box_m = None
            created.append({"view": key, "name": view.Name,
                            "scale": per_view_scale, "outline": box_m})
            view_objects.append(view)

        # Views report a default outline until the drawing is redrawn; repacking before
        # that packs placeholder sizes, which is how views ended up scattered across the
        # sheet instead of grouped. Force geometry first, then repack.
        if view_objects:
            try:
                doc.ForceRebuild3(False)
            except Exception:
                pass
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass

        if explicit_positions:
            # Caller placed the views; repacking would throw that away.
            layout = {"fits": True, "columns": len(views), "explicit": True}
        else:
            layout = _place_views(view_objects, sheet_w, sheet_h, safety=safety)
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass

        # Measure-then-correct. An assembly's bounding box is not obtainable (lightweight
        # components return None from GetBox, GetPartBox does not exist on IAssemblyDoc),
        # so the predicted layout cannot be trusted for the very models that need auto_fit
        # most. After the views exist we can measure them for real: if any outline leaves
        # the sheet, step the scale down and repack, up to three times.
        auto_scale_adjustments = []
        # auto_fit must not overrule a scale the caller asked for explicitly: a 4-view
        # layout was silently taken from 1:2.5 to 1:5 (and an A2 to 1:10) while the views
        # still fitted. Only step down when the caller left scale unset.
        if auto_fit and not scale and view_objects:
            sheet_box = list(sheet.GetOutline or [])
            for _attempt in range(3):
                try:
                    doc.GraphicsRedraw2()
                except Exception:
                    pass
                outside = False
                measured = []
                for candidate in view_objects:
                    try:
                        # P2-13: judge out-of-sheet on the deflated ink box, not the
                        # 11.9 mm-inflated GetOutline (which shrank scales needlessly).
                        o = precise_outline(candidate) or list(candidate.GetOutline or [])
                    except Exception:
                        continue
                    if len(o) < 4:
                        continue
                    measured.append(o)
                    if (o[0] < sheet_box[0] - 1e-6 or o[1] < sheet_box[1] - 1e-6
                            or o[2] > sheet_box[2] + 1e-6 or o[3] > sheet_box[3] + 1e-6):
                        outside = True
                if not outside:
                    break
                try:
                    current = float(view_objects[0].ScaleDecimal)
                except Exception:
                    break
                smaller = _standard_scale(current * 0.5)
                if smaller >= current:
                    break
                for candidate in view_objects:
                    try:
                        candidate.ScaleDecimal = smaller
                    except Exception:
                        pass
                auto_scale_adjustments.append({"from": current, "to": smaller})
                layout = _place_views(view_objects, sheet_w, sheet_h, safety=safety)
        try:
            drw.GraphicsRedraw2()
        except Exception:
            pass
        for item, view in zip(created, view_objects):
            try:
                o = list(view.GetOutline or [])
                item["outline"] = {"width_m": round(o[2] - o[0], 4),
                                   "height_m": round(o[3] - o[1], 4)}
                p = precise_outline(view)
                if p:
                    item["precise_outline_m"] = {
                        "width_m": round(p[2] - p[0], 4),
                        "height_m": round(p[3] - p[1], 4),
                    }
            except Exception:
                pass

        try:
            drw.GraphicsRedraw2()
        except Exception:
            pass

        result = {
            "drawing": drw.GetTitle,
            "model": model.GetTitle,
            "model_path": model_path,
            "template": template,
            "sheet": {
                "name": sheet.Name if hasattr(sheet, "Name") else None,
                "size_m": [round(outline[2] - outline[0], 4), round(outline[3] - outline[1], 4)] if outline else None,
            },
            "views": created,
            "skipped_views": skipped,
            "layout": layout,
            "safe_area": safety,
            "saved": False,
        }
        if not created:
            # Same leak, different exit: the blank document is already open at this point, so
            # it must be closed before answering.
            try:
                sw.CloseDoc(drw.GetTitle)
            except Exception:
                pass
            return error("no_views_created", "No drawing view could be created",
                         "Check the model is saved, not read-only, and has valid named views.",
                         skipped_views=skipped)

        if save and output_path:
            out = os.path.abspath(output_path)
            if os.path.splitext(out)[1].lower() != ".slddrw":
                out += ".SLDDRW"
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

            # `saved = os.path.exists(out)` was a lie whenever the path was already held by
            # an open document: SaveAsSilent fails, but the OLD file is still there, so the
            # check passed and callers believed they were editing the new drawing. A report
            # spent three rounds on this. Two guards instead:
            #   1. refuse up front if an open document already owns the path
            #   2. after saving, require the file to be newer than before
            blocker = None
            try:
                from ..sw_session import get_sw
                app = get_sw()
                open_names = []
                for doc in (app.GetDocuments or ()):
                    try:
                        if os.path.normcase(str(doc.GetPathName)) == os.path.normcase(out):
                            open_names.append(str(doc.GetTitle))
                    except Exception:
                        continue
                if open_names:
                    blocker = ("path_locked_by_open_doc",
                               "target path is already open as %r — saving would fail "
                               "silently and leave the OLD file in place" % open_names[0])
            except Exception:
                pass

            before_mtime = os.path.getmtime(out) if os.path.exists(out) else None
            if blocker is None:
                try:
                    drw.SaveAsSilent(out, False)
                except Exception as exc:
                    blocker = ("saveas_failed", str(exc)[:120])
            after_mtime = os.path.getmtime(out) if os.path.exists(out) else None
            wrote = after_mtime is not None and after_mtime != before_mtime
            if blocker is None and not wrote:
                blocker = ("save_not_written",
                           "SaveAsSilent returned without raising, but the file on disk did "
                           "not change (mtime unchanged) — treat as not saved")
            if blocker:
                result.update(saved=False, drawing_path=out, save_error=blocker[0],
                              save_reason=blocker[1])
            else:
                result.update(saved=True, drawing_path=out,
                              bytes=os.path.getsize(out) if os.path.exists(out) else 0)
        result.update(
            safe_area_used={
                "x": [round(safe_x0, 4), round(safe_x1, 4)],
                "y": [round(safe_y0, 4), round(safe_y1, 4)],
                "source": safety.get("source"),
            },
            box_used_m=[round(v, 4) for v in box] if box else None,
            auto_scale_adjustments=auto_scale_adjustments,
            placement_deltas=placement_deltas,
            # Record the FINAL scale too: the pre-layout step above can still step down
            # (a 0.2 that overflowed became 0.1 on the 履带轮 test while scale_reason kept
            # saying "1:5"), and a receipt that contradicts itself is worse than none.
            chosen_scale=chosen_scale,
            # Why the sheet got that scale (model box / view count / raw fit / the rule).
            scale_reason=scale_reason,
            outline_note=("`outline` 来自 IView.GetOutline，宽高各虚胖 %.1f mm（恒定常数，"
                          "非投影误差）；`precise_outline_m` 已内缩该常数，实测与渲染 ink bbox "
                          "吻合（58.98 mm vs 59.2 mm，0.4%%）。没有逐顶点版本："
                          "IView.GetVisibleEntities2 返回轮廓边（GetStartVertex 为 None）、"
                          "IBody2.GetVertices 对全圆边实体返回 None、ICurve 在该版本没有 "
                          "GetEndPoint/GetTessPoints，三条取点路都已实测排除。"
                          % (OUTLINE_MARGIN_M * 1000)),
            box_source=("part" if box else
                        "unavailable (assembly) — layout verified by measuring views"),
            lightweight_resolve_attempted=is_assembly,
            lightweight_unresolved=lightweight_unresolved,
        )
        return ok(**result)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("create_drawing_failed", str(exc))


@perf_hooks.timed("save")
def _save_drawing(doc):
    """Persist the drawing. Annotation steps otherwise live only in memory and the
    .SLDDRW handed to the user has views but none of the dimensions."""
    try:
        doc.SaveSilent
        return True
    except Exception:
        return False


def _open_drawing(path):
    from ..sw_session import get_sw

    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError("file not found: %s" % path)
    if os.path.splitext(path)[1].lower() != ".slddrw":
        raise ValueError("expected a .SLDDRW file, got %s" % path)
    doc = open_document(path, SW_DOC_DRAWING)
    _activate(get_sw(), doc.GetPathName or doc.GetTitle)
    return doc


def export_drawing(drawing_path, output_path, fmt=None):
    """Export an existing .SLDDRW to PDF / DWG / DXF. PDF comes from a drawing only."""
    try:
        out = os.path.abspath(output_path)
        ext = os.path.splitext(out)[1].lower()
        if fmt:
            ext = "." + fmt.strip().lower().lstrip(".")
            out = os.path.splitext(out)[0] + ext
        if ext not in SUPPORTED_FORMATS:
            return error("unsupported_format", "Supported: pdf, dwg, dxf (got %r)" % ext,
                         "STEP/STL come from models — use batch_export instead.")
        doc = _open_drawing(drawing_path)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        doc.SaveAsSilent(out, False)
        if not os.path.exists(out):
            return error("export_failed", "SolidWorks reported success but wrote no file",
                         "Check the output folder is writable and the drawing has at least one view.")
        return ok(drawing=doc.GetTitle, path=out, format=ext.lstrip("."),
                  bytes=os.path.getsize(out))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("export_failed", str(exc))


def render_drawing(pdf_path, output_dir=None, scale=2.0):
    """Render every PDF page to PNG so the result can actually be looked at."""
    try:
        pdf_path = os.path.abspath(pdf_path)
        if not os.path.exists(pdf_path):
            raise FileNotFoundError("file not found: %s" % pdf_path)
        try:
            import pypdfium2 as pdfium
        except ImportError:
            import sys as _sys

            # A report noted the obvious-looking fix (install into the system Python) does
            # nothing, because the MCP host runs this server from its own venv. Naming the
            # exact interpreter removes the guesswork.
            return error(
                "missing_dependency", "pypdfium2 is required to render PDF pages",
                "Install it into the interpreter that RUNS this server, not the system "
                "Python: \"%s\" -m pip install pypdfium2 . Since 0.8.23 it is a hard "
                "dependency, so reinstalling this package also fixes it."
                % _sys.executable)
        out_dir = os.path.abspath(output_dir) if output_dir else os.path.dirname(pdf_path)
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        pages = []
        doc = pdfium.PdfDocument(pdf_path)
        for index in range(len(doc)):
            page = doc[index]
            image = page.render(scale=float(scale)).to_pil()
            target = os.path.join(out_dir, "%s_p%d.png" % (stem, index + 1))
            image.save(target)
            pages.append({"page": index + 1, "path": target, "width": image.width,
                          "height": image.height, "bytes": os.path.getsize(target)})
        return ok(pdf=pdf_path, page_count=len(pages), pages=pages)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("render_failed", str(exc))


@perf_hooks.timed("audit")
def drawing_audit(drawing_path):
    """Structural audit: views, scales, blank views, dimension and annotation counts.

    Evidence only — it does not judge whether the chosen views or tolerances are
    manufacturable. Blank views and missing dimensions are the checks that catch
    silent failures.
    """
    try:
        doc = _open_drawing(drawing_path)
        views = _iter_views(doc)
        sheets = [v for v in views if getattr(v, "Type", None) == 1]
        model_views = [v for v in views if getattr(v, "Type", None) != 1]

        rows, warnings = [], []
        total_dims = 0
        boxes = []
        sheet_box = list(sheets[0].GetOutline) if sheets else None
        # IView.GetAnnotationCount on a model view also reports sheet-level notes (技术要求),
        # so "view 3: annotations 11" read as "this view has 11 annotations" when most were
        # sheet notes. Report the sheet's own count separately and say so, rather than
        # subtracting — the split between the two is not reliably attributable.
        sheet_annotations = None
        if sheets:
            try:
                sheet_annotations = int(sheets[0].GetAnnotationCount)
            except Exception:
                sheet_annotations = None
        for view in model_views:
            try:
                name = view.Name
            except Exception:
                name = None
            try:
                scale = view.ScaleDecimal
            except Exception:
                scale = None
            try:
                o = list(view.GetOutline or [])
                width, height = o[2] - o[0], o[3] - o[1]
            except Exception:
                width = height = None
            try:
                dims = int(view.GetDimensionCount)
            except Exception:
                dims = None
            try:
                notes = int(view.GetAnnotationCount)
            except Exception:
                notes = None
            if dims:
                total_dims += dims
            # P2-13: GetOutline is inflated by a constant 11.9 mm on every view. Overlap
            # and out-of-sheet checks must use the deflated ink box, otherwise two views
            # whose phantom margins merely touch raise a views_overlap that is not real.
            # Keep the raw width/height in the row for backwards compatibility.
            pbox = precise_outline(view)
            row = {
                "name": name,
                "scale": scale,
                "width_m": round(width, 4) if width is not None else None,
                "height_m": round(height, 4) if height is not None else None,
                "precise_width_m": round(pbox[2] - pbox[0], 4) if pbox else None,
                "precise_height_m": round(pbox[3] - pbox[1], 4) if pbox else None,
                "dimensions": dims,
                "annotations": notes,
            }
            rows.append(row)
            if width is not None and (width < 0.001 or height < 0.001):
                warnings.append({"view": name, "issue": "blank_view"})
            # A view without dimensions is normal: axonometric and detail views often
            # carry none. Measured on 100 real drawings, 79 of them would have tripped
            # this warning, so it now only fires when the SHEET has no dimensions at all.

            if width is not None and height is not None:
                try:
                    boxes.append((name, pbox or list(view.GetOutline)))
                except Exception:
                    pass
            check_box = pbox
            if check_box is None and sheet_box:
                try:
                    check_box = list(view.GetOutline)
                except Exception:
                    check_box = None
            if sheet_box and check_box:
                if (check_box[0] < sheet_box[0] or check_box[1] < sheet_box[1]
                        or check_box[2] > sheet_box[2] or check_box[3] > sheet_box[3]):
                    warnings.append({
                        "view": name, "issue": "view_outside_sheet",
                        "hint": "reduce scale or move the view",
                    })

        if not model_views:
            warnings.append({"view": None, "issue": "no_model_views"})
        if model_views and total_dims == 0:
            warnings.append({
                "view": None, "issue": "no_dimensions_anywhere",
                "hint": "the sheet carries no dimensions at all",
            })

        # Overlapping views are the failure mode a human notices first but code often
        # misses: two outlines intersect and neither is blank nor out of bounds.
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i][1], boxes[j][1]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    warnings.append({
                        "view": "%s <-> %s" % (boxes[i][0], boxes[j][0]),
                        "issue": "views_overlap",
                        "hint": "reduce scale or use fewer views per sheet",
                    })

        return ok(
            drawing=doc.GetTitle,
            path=doc.GetPathName,
            sheet_count=len(sheets),
            sheet_size_m=[
                round(list(sheets[0].GetOutline)[2] - list(sheets[0].GetOutline)[0], 4),
                round(list(sheets[0].GetOutline)[3] - list(sheets[0].GetOutline)[1], 4),
            ] if sheets else None,
            view_count=len(model_views),
            total_dimensions=total_dims,
            views=rows,
            sheet_annotations=sheet_annotations,
            warnings=warnings,
            note=("Evidence only: view choice, tolerances and manufacturability are not judged here. "
                  "A view's `annotations` count may include sheet-level notes (技术要求) — "
                  "sheet_annotations reports the sheet's own count separately."),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("audit_failed", str(exc))


def _annotation_dimension_full_name(annotation, win):
    """Model-level identity of a dimension annotation (e.g. D1@草图1@灌装阀阀体.Part).

    `IAnnotation.GetName` is only a sheet-unique label (细节项目272), so grouping by it can
    never detect the same driving dimension inserted into four different views. IDimension
    .FullName is the identity P1-6 needs; `Name` is the fallback on older builds.
    """
    try:
        specific = win.Dispatch(annotation.GetSpecificAnnotation)
    except Exception:
        return None
    try:
        dim = win.Dispatch(specific.GetDimension2(0))
    except Exception:
        return None
    for attr in ("FullName", "Name"):
        try:
            value = str(getattr(dim, attr))
        except Exception:
            continue
        if value:
            return value
    return None


@perf_hooks.timed("dim_dedupe")
def _dedupe_inserted_dimensions(doc, before_names, win):
    """Delete duplicate driving dimensions among the annotations JUST inserted (P1-6).

    SolidWorks' own `eliminate_duplicates` flag on InsertModelAnnotations3 did not stop the
    same D1@草图1 from landing once per view (56 dims across 4 views). Keep the FIRST
    occurrence of each model full name and delete the rest. Only annotations whose sheet
    name was absent before the insert are eligible, so dimensions the user already placed
    are never touched. Returns (duplicate_groups, removed_names, failures).
    """
    groups = {}
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            continue
        for raw in (view.GetAnnotations or ()):
            try:
                annotation = win.Dispatch(raw)
                if int(annotation.GetType) != 4:  # 4 = dimension annotation
                    continue
                sheet_name = str(annotation.GetName)
            except Exception:
                continue
            if sheet_name in before_names:
                continue
            full = _annotation_dimension_full_name(annotation, win)
            if full:
                groups.setdefault(full, []).append((sheet_name, annotation))
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    removed, failed = [], []
    for full, entries in duplicates.items():
        for sheet_name, annotation in entries[1:]:
            try:
                doc.ClearSelection2(True)
                if not annotation.Select(False):
                    raise RuntimeError("Select(False) returned False")
                if doc.SelectionManager.GetSelectedObjectCount2(-1) < 1:
                    raise RuntimeError("nothing selected")
                _call_method(doc, "EditDelete")
                removed.append(sheet_name)
            except Exception as exc:
                failed.append({"name": sheet_name, "full_name": full,
                               "reason": str(exc)[:80]})
    if removed:
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.ClearSelection2(True)
        except Exception:
            pass
    return duplicates, removed, failed


@perf_hooks.timed("dim_filter")
def _filter_inserted_dimensions(doc, before_names, win, keep):
    """Delete inserted dimensions the plan did not keep (N3: plan ↔ auto_dimension).

    `keep` is `planner.keep_dimensions`, e.g.
    `[{"feature": "外形", "dimension": "总长", "value_mm": 1550.12}, ...]`.

    A dimension survives when either
      * its MODEL feature name equals an entry's `feature` (full names look like
        `D1@凸台-拉伸1@PART.Part`, so the middle segment is the feature), or
      * its value matches an entry's `value_mm` within `tol` — the planner's 外形 bucket
        describes bounding-box values, not feature names, so value matching is the only
        way those entries can ever hit.

    Safety valve: if NOTHING matches, nothing is deleted and `aborted` says why. A filter
    that recognises zero dimensions is far more likely to be wrong than the sheet is.
    Returns (kept, removed, failures, aborted_reason).
    """
    features = set()
    full_prefixes = set()
    # P1 of a 2026-11-03 report: `plan_drawing` emits model feature full names like
    # `D1@草图1@飞达动力轴.Part`, while the same dimension on the SHEET is
    # `D1@草图2@飞达动力轴.Part` — the sketch NUMBER differs because the drawing side
    # numbers its own. Exact-prefix matching therefore hit 0 of 18, the safety valve
    # aborted, and the documented "plan → auto_dimension" path did nothing.
    #
    # A loose key (D-number + feature name with its trailing digits stripped) crosses that
    # gap: `D1@草图1` and `D1@草图2` both become ("D1", "草图").
    loose_keys = set()
    values = []
    for entry in keep or []:
        # P1, 2026-11-03: a report fed `[d["dimension"] for d in plan["keep_dimensions"]]` —
        # a list of STRINGS — and every one was skipped by the old `isinstance(entry, dict)`
        # guard, so the filter aborted with "0 features / 0 values". A string entry is a
        # legitimate name form; accept it as one.
        if isinstance(entry, str):
            entry = {"dimension": entry}
        if not isinstance(entry, dict):
            continue
        feature = str(entry.get("feature") or "").strip()
        if feature and feature not in ("外形", "总长", "总宽", "总高"):
            if "@" in feature:
                # `plan_drawing` returns full names like "D1@草图19". Keeping only the
                # feature segment ("草图19") never matched `parts[1]`, so the filter fell
                # through to value matching and kept every Ø42 — three copies of it.
                full_prefixes.add(feature)
            features.add(feature.split("@")[-1] if "@" in feature else feature)
            # ("D1", "草图") from "D1@草图1" — see the loose_keys note above.
            _parts = feature.split("@")
            _dnum = _parts[0].strip()
            _feat = _parts[1] if len(_parts) > 1 else ""
            _stripped = "".join(ch for ch in _feat if not ch.isdigit())
            if _dnum and _stripped:
                loose_keys.add((_dnum, _stripped))
        try:
            values.append(float(entry.get("value_mm")))
        except Exception:
            continue

    def _value_of(annotation):
        try:
            dim = win.Dispatch(win.Dispatch(annotation.GetSpecificAnnotation).GetDimension2(0))
            return float(dim.SystemValue) * 1000.0
        except Exception:
            return None

    candidates = []
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            continue
        for raw in (view.GetAnnotations or ()):
            try:
                annotation = win.Dispatch(raw)
                if int(annotation.GetType) != 4:
                    continue
                sheet_name = str(annotation.GetName)
            except Exception:
                continue
            if sheet_name in before_names:      # never touch what the user placed
                continue
            full = _annotation_dimension_full_name(annotation, win) or ""
            parts = full.split("@")
            feature = parts[1] if len(parts) >= 3 else ""
            value = _value_of(annotation)
            candidates.append((sheet_name, full, feature, value, annotation))

    if not candidates:
        return 0, [], [], "no dimensions were inserted by this call"

    def _keep(candidate):
        _, full, feature, value, _ = candidate
        # An exact identity wins outright: when the caller named specific dimensions
        # ("D1@草图19"), do NOT fall back to value matching — that is exactly how a plan
        # that kept one Ø42 ended up keeping all three.
        # Name-based matching stays EXCLUSIVE: when the caller named dimensions, a miss must
        # mean False, never "fall through to value matching". That fallback is what once kept
        # three copies of Ø42 when the plan asked for one.
        #
        # The loose key is part of the same exclusive family — it is just a name form that
        # survives the model/sheet sketch-renumbering gap (`D1@草图1` vs `D1@草图2`).
        if full_prefixes or loose_keys:
            if full_prefixes and any(full.startswith(prefix) for prefix in full_prefixes):
                return True
            if loose_keys:
                _cparts = (full or "").split("@")
                _cdnum = _cparts[0].strip() if _cparts else ""
                _cfeat = _cparts[1] if len(_cparts) > 1 else ""
                _cstripped = "".join(ch for ch in _cfeat if not ch.isdigit())
                if _cdnum and _cstripped and (_cdnum, _cstripped) in loose_keys:
                    return True
            return False
        if feature and feature in features:
            return True
        if value is None:
            return False
        for entry_value in values:
            tol = max(0.05, abs(entry_value) * 1e-4)
            if abs(value - entry_value) <= tol:
                return True
        return False

    kept = [c for c in candidates if _keep(c)]
    if not kept:
        return 0, [], [], ("none of the %d inserted dimensions matched the plan "
                           "(%d features / %d loose / %d values) — deleted nothing"
                           % (len(candidates), len(features), len(loose_keys), len(values)))

    removed, failed = [], []
    for sheet_name, full, _, _, annotation in candidates:
        if _keep((sheet_name, full, _, _, annotation)):
            continue
        try:
            doc.ClearSelection2(True)
            if not annotation.Select(False):
                raise RuntimeError("Select(False) returned False")
            if doc.SelectionManager.GetSelectedObjectCount2(-1) < 1:
                raise RuntimeError("nothing selected")
            _call_method(doc, "EditDelete")
            removed.append(sheet_name)
        except Exception as exc:
            failed.append({"name": sheet_name, "full_name": full, "reason": str(exc)[:80]})
    if removed:
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.ClearSelection2(True)
        except Exception:
            pass
    return len(kept), removed, failed, None


@perf_hooks.timed("dim_outside")
def _push_dimensions_outside(doc, win, before_names, gap_m=0.012, step_m=0.008):
    """Move the dimensions this call inserted to just OUTSIDE their view (P0-1).

    SolidWorks inserts model dimensions at a fixed ~10 mm offset whatever the part size, so
    on a Ø46 disc 25 dimensions land inside the outline and overprint each other. The
    report's workaround was to scale the view up to 4:1 — that changes what the drawing
    states instead of fixing the layout.

    Each dimension is assigned to the NEAREST outline side of the view that contains it and
    stacked outward: distance = gap + slot * step. Within one side the stacking order follows
    the other axis, so two dimensions on the same side never share a distance.

    Only annotations NOT in `before_names` are touched — a dimension the user placed by hand
    is never moved.

    Returns (placed_count, failures, sides_used).
    """
    # Split the cost of the read pass: position vs value vs outline. 4.0 s for 11
    # dimensions is ~90 ms per COM round trip, so knowing WHICH call is expensive decides
    # whether this can be optimised at all. Declared FIRST: the outline loop below uses
    # _t_outline, and putting these after the boxes list raised NameError on every call.
    _t_read_pos = 0.0
    _t_read_val = 0.0
    _t_outline = 0.0
    _t_write = 0.0
    _t_iter = 0.0
    _t_frame = 0.0
    _t0 = time.perf_counter()
    views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
    _t_iter += time.perf_counter() - _t0
    boxes = []
    for view in views:
        _t0 = time.perf_counter()
        box = precise_outline(view)
        _t_outline += time.perf_counter() - _t0
        if box and len(box) >= 4:
            boxes.append((view, box))
    if not boxes:
        return 0, [{"reason": "no view outlines readable"}], {}

    buckets = {}
    failures = []
    # Intermediate counts. The receipt only shows the final `placed`, so when a change here
    # silently stops the pass there is nothing to look at (0.8.48 happened exactly that way).
    debug = {"views": len(boxes), "annotations": 0, "skip_before": 0,
             "skip_no_position": 0, "skip_no_owner": 0, "buckets": 0, "per_side": {}}
    _t_enum = 0.0
    for view, box in boxes:
        try:
            _t0 = time.perf_counter()
            raws = list(view.GetAnnotations or ())
            _t_enum += time.perf_counter() - _t0
        except Exception:
            continue
        for raw in raws:
            try:
                annotation = win.Dispatch(raw)
                if int(annotation.GetType) != 4:
                    continue
                name = str(annotation.GetName)
            except Exception:
                continue
            debug["annotations"] += 1
            if name in before_names:
                debug["skip_before"] += 1
                continue                     # placed by the user, not by this call
            try:
                _t0 = time.perf_counter()
                position = [float(v) for v in annotation.GetPosition]
                _t_read_pos += time.perf_counter() - _t0
            except Exception:
                debug["skip_no_position"] += 1
                continue

            # Owning view: the box that contains it, else the one with the nearest centre.
            owner_box = None
            for _candidate, candidate_box in boxes:
                if (candidate_box[0] <= position[0] <= candidate_box[2]
                        and candidate_box[1] <= position[1] <= candidate_box[3]):
                    owner_box = candidate_box
                    break
            if owner_box is None:
                best = None
                for _candidate, candidate_box in boxes:
                    cx = (candidate_box[0] + candidate_box[2]) / 2.0
                    cy = (candidate_box[1] + candidate_box[3]) / 2.0
                    distance = (position[0] - cx) ** 2 + (position[1] - cy) ** 2
                    if best is None or distance < best[0]:
                        best = (distance, candidate_box)
                if best is None:
                    continue
                owner_box = best[1]

            edges = {
                "left": abs(position[0] - owner_box[0]),
                "right": abs(owner_box[2] - position[0]),
                "bottom": abs(position[1] - owner_box[1]),
                "top": abs(owner_box[3] - position[1]),
            }
            side = min(edges, key=edges.get)
            order = position[1] if side in ("left", "right") else position[0]
            # The value is needed ONLY to order dimensions inside one side, and only when
            # that side holds more than one. Reading it costs 143 ms per dimension
            # (GetDimension2 -> IDimension -> SystemValue; measured 2026-11-03), and it is
            # the single most expensive call in this function. So it is deferred: bucket
            # first, then fill in values for the buckets that actually need sorting.
            buckets.setdefault((str(view.Name), side), []).append(
                (order, annotation, position, owner_box, None))

    # ---- capacity per side, measured against the sheet frame ---------------------
    # The stack was unbounded: 16 dimensions on one side gave offset 0.012 + 15*0.008 =
    # 0.132, which from a view at y≈0.15 lands at y≈0.018 — inside the title block, far
    # below the A3 safe area (0.065). A report saw exactly that ("引线从视图底边斜拉到
    # 图纸左下角"). Spill to the opposite side first, then compress the step.
    safe = None
    # `doc.GetSheets` returned nothing usable on SW2024 (the cap never engaged and a stack
    # of 10 still printed past the frame). The sheet itself is the Type 1 view — its outline
    # is the frame, verified against a rendered 420x297 mm A3 elsewhere in this file.
    _t0 = time.perf_counter()
    for source in ("GetFirstView", "GetSheets"):
        if safe is not None:
            break
        try:
            candidate = getattr(doc, source)
            if source == "GetSheets":
                sheets = list(candidate or ())
                candidate = sheets[0] if sheets else None
            if candidate is None:
                continue
            frame = list(candidate.GetOutline or ())
            if len(frame) >= 4 and (frame[2] - frame[0]) > 0.05:
                inset = 0.010
                safe = (frame[0] + inset, frame[1] + inset,
                        frame[2] - inset, frame[3] - inset)
        except Exception:
            continue

    _t_frame += time.perf_counter() - _t0
    opposite = {"bottom": "top", "top": "bottom", "left": "right", "right": "left"}

    def _room(owner_box, side):
        if safe is None:
            return 0.20
        if side == "bottom":
            return max(owner_box[1] - safe[1], 0.0)
        if side == "top":
            return max(safe[3] - owner_box[3], 0.0)
        if side == "left":
            return max(owner_box[0] - safe[0], 0.0)
        return max(safe[2] - owner_box[2], 0.0)

    overflow_notes = []
    for (view_name, side), items in list(buckets.items()):
        if not items:
            continue
        box = items[0][3]
        room = _room(box, side)
        capacity = max(int((room - gap_m) / max(step_m, 1e-6)) + 1, 1)
        if len(items) <= capacity:
            continue
        keep, spill = items[:capacity], items[capacity:]
        buckets[(view_name, side)] = keep
        buckets.setdefault((view_name, opposite[side]), []).extend(spill)
        overflow_notes.append("%s:%s 只放得下 %d 条，%d 条改去 %s"
                              % (view_name, side, len(keep), len(spill),
                                 opposite[side]))

    # Fill in values only where ordering needs them.
    for _key, _items in buckets.items():
        if len(_items) < 2:
            continue
        for _i, (_o, _ann, _pos, _box, _val) in enumerate(_items):
            try:
                _t0 = time.perf_counter()
                _dim = win.Dispatch(
                    win.Dispatch(_ann.GetSpecificAnnotation).GetDimension2(0))
                _v = float(_dim.SystemValue) * 1000.0
                _t_read_val += time.perf_counter() - _t0
            except Exception:
                _v = 0.0
            _items[_i] = (_o, _ann, _pos, _box, _v)

    debug["buckets"] = sum(len(v) for v in buckets.values())
    debug["per_side"] = {"%s:%s" % k: len(v) for k, v in buckets.items()}
    placed = 0
    sides_used = {}
    for (view_name, side), items in buckets.items():
        # Sort so the stacking follows the view's own reading order; the slot decides the
        # distance from the outline, so equal-order items still separate.
        # Largest value outermost: slot 0 sits at `gap` from the outline, so ascending
        # order pushes the big ones to the far slots. (Descending put the Ø215 nearest the
        # view and the Ø10 out at the frame — backwards from the drafting convention.)
        items.sort(key=lambda entry: (entry[4], entry[0]))
        sides_used["%s:%s" % (view_name, side)] = len(items)
        # If the side is still over capacity after the spill, compress the step rather
        # than push past the frame — a shorter leader is readable, an off-sheet one is not.
        room = _room(items[0][3], side)
        eff_step = step_m
        if len(items) > 1 and room > gap_m:
            fit_step = (room - gap_m) / float(len(items) - 1)
            eff_step = min(step_m, max(fit_step, step_m * 0.35))
        for slot, (_order, annotation, position, box, _value) in enumerate(items):
            # Hard cap at the room actually available: `max(room, gap_m)` still pushed a
            # view that already touched the frame 12 mm past it (y reached 0.305 on a
            # 0.297 sheet). A dimension hugging the outline is readable; an off-sheet one
            # is not.
            offset = min(gap_m + slot * eff_step, max(room, 0.0))
            if side == "left":
                target = (box[0] - offset, position[1])
            elif side == "right":
                target = (box[2] + offset, position[1])
            elif side == "bottom":
                target = (position[0], box[1] - offset)
            else:
                target = (position[0], box[3] + offset)
            try:
                _t0 = time.perf_counter()
                annotation.SetPosition(float(target[0]), float(target[1]), 0)
                _t_write += time.perf_counter() - _t0
                placed += 1
            except Exception as exc:
                failures.append({"view": view_name, "side": side,
                                 "reason": str(exc)[:70]})
    # Report the split so the next optimisation is chosen from data, not from a guess.
    debug["ms_read_position"] = round(_t_read_pos * 1000.0, 1)
    debug["ms_read_value"] = round(_t_read_val * 1000.0, 1)
    debug["ms_outline"] = round(_t_outline * 1000.0, 1)
    debug["ms_write"] = round(_t_write * 1000.0, 1)
    debug["ms_iter_views"] = round(_t_iter * 1000.0, 1)
    debug["ms_enum_annotations"] = round(_t_enum * 1000.0, 1)
    debug["ms_sheet_frame"] = round(_t_frame * 1000.0, 1)
    perf_hooks.mark("dim_outside_read", time.perf_counter() - (_t_read_pos + _t_read_val + _t_outline))
    return placed, failures, sides_used, overflow_notes, debug


def survey_dimensions_by_view(drawing_path):
    """Report how dimensions are spread across the model views — read-only.

    Why this exists: a report claimed every driving dimension lands in the FIRST view
    ("文档级幂等，尺寸全集中到首个视图"). Measured on 气缸盖 2026-11-03, that does not
    reproduce — `auto_dimension(all_views=True)` distributes them:

        3 views : 20 / 10 / 9
        4 views : 20 / 10 / 9 / 0     (isometric is always empty)
        2 views : 20 / 10

    The first view does carry the most, but that is SolidWorks putting each dimension in
    the view that can actually show it, not a bug. What IS worth flagging is a view with
    ZERO dimensions — an isometric view legitimately has none, but a right/top view with
    none usually means the sheet is missing the geometry that dimension needs.

    There is no API to re-assign a dimension to a different view: an `IAnnotation` belongs
    to its view, and `SetPosition` only moves it on the sheet. `all_views=False` inserts
    only what the ACTIVE view can show (`no_dimensions_inserted` on views that show none),
    so it cannot be used to push specific dimensions elsewhere either.

    So this is a diagnostic, not a distributor: it tells you the shape of the problem
    instead of pretending to fix it.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        rows = []
        total = 0
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            dims = 0
            others = 0
            for raw in (view.GetAnnotations or ()):
                try:
                    annotation = win.Dispatch(raw)
                except Exception:
                    continue
                kind = None
                for getter in (lambda: annotation.GetType, lambda: annotation.GetType()):
                    try:
                        kind = int(getter())
                        break
                    except Exception:
                        continue
                if kind == 4:
                    dims += 1
                else:
                    others += 1
            total += dims
            rows.append({"view": str(view.Name), "dimensions": dims,
                         "other_annotations": others})
        if not rows:
            return error("no_views", "no model views on this sheet")
        widest = max(r["dimensions"] for r in rows)
        empty = [r["view"] for r in rows if r["dimensions"] == 0]
        return drawing_ok(
            drawing=doc.GetTitle, views=rows, total_dimensions=total,
            busiest_view=[r["view"] for r in rows if r["dimensions"] == widest][0],
            busiest_count=widest,
            empty_views=empty,
            note=("只读诊断。尺寸属于所在视图，API 无法把它改派到别的视图"
                  "（IAnnotation.SetPosition 只改图纸位置）；"
                  "all_views=False 只插当前视图能显示的那部分，"
                  "插不到时会返回 no_dimensions_inserted。"
                  "等轴测视图没有尺寸是正常的；右视/俯视没有尺寸通常说明图纸缺了对应几何。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("survey_failed", str(exc))


@perf_hooks.timed("dimension_total")
def auto_dimension(drawing_path, all_views=True, include_unmarked=False, eliminate_duplicates=True,
                   dimension_filter=None, place_outside=True, detect_same_value=False,
                   save=True, audit=True,
                   outside_gap_m=0.012, outside_step_m=0.008):
    """Insert the model's driving dimensions into the drawing views.

    Equivalent to SolidWorks "Insert Model Items > Dimensions". Marked-for-drawing
    dimensions only by default; include_unmarked=True brings in every driving dim
    (more complete, noisier). Returns how many annotations were inserted.

    `eliminate_duplicates=True` (default) now deletes the extra copies by MODEL full name,
    keeping the first occurrence of each driving dimension. Only dimensions inserted by
    this call are eligible — existing user-placed dimensions are never touched.

    `dimension_filter` closes the plan → insert gap (N3): pass
    `plan_drawing()["keep_dimensions"]` and everything the plan did not keep is deleted
    after insertion, so "keep 8 of 41" actually ends with ~8 dimensions on the sheet.
    Deletion uses the same mechanics as the duplicate pass, and aborts without deleting
    anything if the filter matches no inserted dimension.

    This is document-level idempotent: a driving dimension inserted once will NOT be inserted
    into a second view, whatever `all_views` / duplicate settings are used (measured
    2026-11-01: three parameter combinations, second view stayed at 0). To annotate a detail
    view with the same geometry, build a NEW dimension there instead — `list_drawing_edges`
    + `add_drawing_dimension(view_name=...)`. See check_capabilities
    `duplicate_dimension_to_view`.

    `place_outside` defaults to TRUE (changed 2026-11-03): a report measured all 18 inserted
    dimensions landing INSIDE the shaft outline, with dimension lines and the part overlapping
    each other to the point where the sheet could not be used. "Inside by default" is not a
    neutral choice — it is a default that cannot be delivered. Pass False to get the old
    behaviour. Known trade-off on crowded sheets: a side that has no room collapses into a
    vertical stack, and imported angle dimensions can arc across the page; `outside_debug`
    reports the bucketing so that is visible rather than silent.

    `place_outside=True` (P0-1) then moves everything this call inserted to just outside its
    view, stacked by side: `outside_gap_m` + slot * `outside_step_m`. SolidWorks' own offset
    is a fixed ~10 mm regardless of part size, so on a Ø46 disc 25 dimensions land inside the
    outline and overprint; scaling the view up hides the symptom instead of fixing it.
    Dimensions already on the sheet are never moved, and this path saves the drawing itself
    (the rest of the function only edits in memory).
    """
    try:
        doc = _open_drawing(drawing_path)
        # swImportModelItemsSource_e.FromEntireModel = 0
        # swInsertAnnotation_e: MarkedForDrawing = 32768, NotMarked = 524288
        types = 32768 | (524288 if include_unmarked else 0)
        doc.ClearSelection2(True)
        # P1-6: snapshot every annotation name BEFORE inserting. The dedupe pass below
        # must only consider annotations this call created, never a dimension the user
        # placed on the sheet earlier.
        import win32com.client as _win_auto

        before_names = set()
        for _v in _iter_views(doc):
            if getattr(_v, "Type", None) == 1:
                continue
            for _raw in (_v.GetAnnotations or ()):
                try:
                    before_names.add(str(_win_auto.Dispatch(_raw).GetName))
                except Exception:
                    continue
        if not all_views:
            # `all_views=False` inserts into the ACTIVE view. With none activated it inserts
            # into nothing and STILL returns success — a report saw inserted: 0 / ok: true on
            # a sheet that had 18 insertable dimensions. Activate one, or say so.
            _activated = None
            for _v in _iter_views(doc):
                if getattr(_v, "Type", None) == 1:
                    continue
                try:
                    doc.ActivateView(_v.Name)
                    _activated = str(_v.Name)
                    break
                except Exception:
                    continue
            if _activated is None:
                return error(
                    "active_view_not_set",
                    "all_views=False needs an activated view and none could be activated",
                    "Pass all_views=True, or activate a model view first.")
        _t_insert = time.perf_counter()
        inserted = doc.InsertModelAnnotations3(
            0, types, bool(all_views), not bool(eliminate_duplicates), False, True
        )
        try:
            # Must be a real METHOD call: after InsertModelAnnotations3 the new
            # annotations only show up in view.GetAnnotations once the sheet rebuilds.
            # A bare `doc.EditRebuild3` attribute access used to be a silent no-op here,
            # which made the dedupe pass below see an empty set of new dimensions.
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        perf_hooks.mark("dim_insert", _t_insert)
        count = len(inserted) if isinstance(inserted, (tuple, list)) else (1 if inserted else 0)
        if count == 0 and not all_views:
            return error(
                "no_dimensions_inserted",
                "InsertModelAnnotations3 reported nothing inserted into the active view",
                "Two very different causes looked identical before: the view has no model "
                "dimensions, or the activation did not take. Pass all_views=True to insert "
                "everywhere, or check the view really owns this geometry.")
        # No repack: a section view is x-aligned with its parent and cannot be moved
        # sideways, so repacking would stack it on top of a neighbour.
        layout = None
        # P1-6: `eliminate_duplicates` used to mean "tell SolidWorks to try". It did not
        # work (56 dims / 4 views). Now also delete the extra copies by model full name.
        duplicates, removed, dedupe_failed = {}, [], []
        if eliminate_duplicates:
            try:
                duplicates, removed, dedupe_failed = _dedupe_inserted_dimensions(
                    doc, before_names, _win_auto)
            except Exception as exc:
                dedupe_failed = [{"reason": "dedupe pass failed: %s" % str(exc)[:80]}]
        # N3: honour plan_drawing()["keep_dimensions"]. Runs after the duplicate pass so
        # the same dimension is not counted twice, and only ever deletes annotations this
        # call inserted (`before_names`).
        filter_kept, filter_removed, filter_failed, filter_aborted = None, [], [], None
        if dimension_filter:
            try:
                filter_kept, filter_removed, filter_failed, filter_aborted = \
                    _filter_inserted_dimensions(doc, before_names, _win_auto, dimension_filter)
            except Exception as exc:
                filter_aborted = "filter pass failed: %s" % str(exc)[:120]
        # PR1 of the 2026-11-03 performance spec: a pipeline run saved four times and
        # audited three times, because each sub-tool did its own. Both flags stay True by
        # default so a direct caller sees no change; the pipeline passes False and does ONE
        # save plus ONE audit at the end.
        #
        # Skipping the audit here is safe for the pipeline but NOT free of consequence: the
        # receipt fields views / warnings / dimension_gap are filled from it. When audit is
        # off they are empty and audit_skipped says so, rather than looking like "no views".
        if save:
            _save_drawing(doc)
        if audit:
            audit_data = drawing_audit(drawing_path)
        else:
            audit_data = {}
        audit = audit_data
        total = audit.get("total_dimensions") or 0

        # P1-6: report the model full names that HAD duplicates and what the automatic
        # pass actually removed. The old code only reported and told the caller to delete
        # by hand — the duplicates stayed on the sheet.
        duplicate_report = {
            full: [sheet_name for sheet_name, _ in entries]
            for full, entries in duplicates.items()
        }

        # P2-4 of a 2026-11-03 report: the sheet carried TWO `10` dimensions and
        # `duplicates_removed: 4` still reported success. The dedupe key is the model's
        # FullName, so a second dimension with the same VALUE but a different FullName — the
        # same measurement projected into another view — survives. The report's phrasing was
        # exact: "去重键是模型的 full name：只要第二条尺寸带另一个 full name（同值投影），
        # 就漏网".
        #
        # Value alone is not enough to delete on either: Ø10 and a 10 mm length are both
        # "10" and both legitimate. So this REPORTS candidates grouped by (value, type, view)
        # and lets the caller decide — deleting on value would take out real dimensions.
        # MEASURED 2026-11-03: this diagnostic was 18.4 s of auto_dimension's 27.0 s (68%).
        # It reads every annotation on every view to group them by (value, type), and it only
        # REPORTS - it never deletes anything. Paying two thirds of the main path for a hint
        # is the wrong default, so it is off unless asked for. The receipt says it was
        # skipped, because a silently missing list looks like "no duplicates found".
        same_value_candidates = []
        try:
            # Skipping the read IS the optimisation: on 旋转轴 it was 18.4 s of 27.0 s.
            # An empty annotation set makes the grouping below produce no candidates.
            _anns = (list_drawing_annotations(drawing_path) if detect_same_value else {})
            _groups = {}
            for _row in (_anns.get("annotations") or []):
                if _row.get("kind") != "dimension":
                    continue
                _value = _row.get("value_mm")
                if _value is None:
                    continue
                _key = (round(float(_value), 2), int(_row.get("dimension_type") or -1))
                _groups.setdefault(_key, []).append({
                    "name": _row.get("name"), "view": _row.get("scope"),
                    "position_m": _row.get("position_m"),
                })
            for (_value, _type), _rows in sorted(_groups.items()):
                if len(_rows) < 2:
                    continue
                same_value_candidates.append({
                    "value_mm": _value,
                    "dimension_type": _type,
                    "count": len(_rows),
                    "instances": _rows[:6],
                })
        except Exception as _exc:
            same_value_candidates = [{"error": str(_exc)[:80]}]

        notes = []
        if duplicate_report:
            worst = max(duplicate_report.items(), key=lambda kv: len(kv[1]))
            notes.append("%d 个 driving 尺寸在多个视图里重复（最多 %d 次：%s）——"
                         "已按 full name 自动删除 %d 条多余副本（保留首次出现），失败 %d 条。"
                         % (len(duplicate_report), len(worst[1]), worst[0],
                            len(removed), len(dedupe_failed)))
        _empty = [v.get("name") for v in (audit.get("views") or [])
                  if int(v.get("dimensions") or 0) == 0]
        if _empty:
            # P4: every dimension lands in the first view because InsertModelItems is
            # document-level. The depth-only features then have nowhere to live and the
            # caller gets no signal — the sheet simply looks incomplete.
            notes.append(
                "空视图：%s 上没有任何尺寸。`InsertModelItems` 是文档级的，尺寸会集中到首个视图；"
                "深度类尺寸请用 add_drawing_dimension 在侧视/剖视上单独补标。" % "、".join(_empty))
        if dedupe_failed:
            notes.append("%d 条重复尺寸删除失败，见 dedupe_failed；可用 "
                         "delete_dimensions 按视图兜底。" % len(dedupe_failed))
        if eliminate_duplicates and removed:
            notes.append(
                "去重按模型 FullName 保留**首次出现**（插入顺序主视图在前），"
                "所以剖视/局部视图上的同源副本会被删掉——深度类尺寸可能因此无处安放"
                "（实测 83 插入 → 删 44，两个剖视 0 尺寸）。需要剖视承载尺寸时，"
                "改 eliminate_duplicates=False，或用 dimension_filter 精确保留。")
        if total > 30:
            notes.append(
                "本图 %d 个尺寸；100 张真实工程图实测平均 14 个（中位 11）。"
                "全量导入（include_unmarked=True）会把模型的每个驱动尺寸都倒到图上，"
                "制图是筛选而非搬运，建议关闭它或事后删除非关键尺寸。" % total)
        if dimension_filter:
            if filter_aborted:
                notes.append("dimension_filter 未生效：%s" % filter_aborted)
            else:
                notes.append("dimension_filter 生效：保留 %s 条、删除 %d 条不符合规划"
                             "的尺寸（失败 %d 条）。"
                             % (filter_kept, len(filter_removed), len(filter_failed)))
        # P0-1: push this call's dimensions outside their views, stacked by side.
        outside_placed = 0
        outside_failed = []
        outside_sides = {}
        outside_spills = []
        # MUST be initialised here, not only inside the branch below: the receipt references
        # it unconditionally, so `place_outside=False` (the default) raised
        # "cannot access local variable 'outside_debug'" and took four smoke items down.
        # Neither static_check nor the bare-call regression looks at function-local
        # assignments — smoke was the only net.
        outside_debug = {}
        if place_outside:
            (outside_placed, outside_failed, outside_sides,
             outside_spills, outside_debug) = _push_dimensions_outside(
                doc, _win_auto, before_names,
                gap_m=float(outside_gap_m), step_m=float(outside_step_m))
            try:
                _call_method(doc, "EditRebuild3")
            except Exception:
                pass
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass
            # Explicit save: this is the only branch that edits geometry the caller asked
            # to be laid out. Honours the PR1 save flag so a pipeline run does not save
            # twice here as well.
            if save:
                try:
                    _save_drawing(doc)
                except Exception:
                    pass
            notes.append(
                "place_outside：%d 条尺寸已推到视图外按侧分层（外扩 %.0f mm + n×%.0f mm），"
                "失败 %d 条。" % (outside_placed, float(outside_gap_m) * 1000,
                                 float(outside_step_m) * 1000, len(outside_failed)))
            if outside_spills:
                # Say when the stack had to change sides or compress — otherwise the caller
                # sees a layout it did not ask for and cannot tell why.
                notes.append("分层受安全区限制：%s。" % "；".join(outside_spills))

        return ok(
            drawing=doc.GetTitle,
            inserted=count,
            layout=layout,
            include_unmarked=bool(include_unmarked),
            total_dimensions=total,
            dimension_filter_applied=bool(dimension_filter),
            filter_kept=filter_kept,
            filter_removed=len(filter_removed),
            filter_removed_names=filter_removed[:20],
            filter_failed=filter_failed[:10],
            filter_aborted=filter_aborted,
            duplicate_dimension_names=len(duplicate_report),
            duplicate_examples=dict(list(duplicate_report.items())[:5]),
            dedupe_applied=bool(eliminate_duplicates),
            duplicates_removed=len(removed),
            duplicate_names_removed=removed[:20],
            # Dimensions that share a VALUE and TYPE but survived the FullName dedupe. Not
            # deleted automatically: Ø10 and a 10 mm length are both "10" and both correct.
            # Listed so a caller can spot the stray one the report described.
            same_value_candidates=same_value_candidates,
            # An empty list must not read as "checked, none found". State which it is.
            same_value_checked=bool(detect_same_value),
            same_value_candidate_count=len(same_value_candidates),
            dedupe_failed=dedupe_failed[:10],
            # 0.8.25: an explicit count — `dedupe_failed` is truncated to 10, so a run
            # that removed 0 and failed 23 used to be indistinguishable from a clean no-op.
            dedupe_failed_count=len(dedupe_failed),
            # P3: the plan can contain entries no driving dimension can satisfy (bounding-box
            # values). Say how many, so "filter_kept: 20" is not mistaken for "all 20 made it".
            planned_derived_count=sum(
                1 for e in (dimension_filter or []) if isinstance(e, dict)
                and e.get("kind") == "derived"),
            planned_driving_count=sum(
                1 for e in (dimension_filter or []) if isinstance(e, dict)
                and e.get("kind") == "driving"),
            place_outside_applied=bool(place_outside),
            # An empty views/warnings list must not read as "the sheet has none" when the
            # audit simply was not run.
            audit_skipped=not bool(audit),
            outside_debug=outside_debug,
            # P2-5 of a 2026-11-03 report: a second call with place_outside=true returned
            # {"inserted": 1, "placed_outside": 0, "outside_debug": {"skip_before": 12,
            # "buckets": 0}} — the one new dimension stayed on the geometry AND duplicated
            # an existing one. `skip_before` skips everything that was already there, so a
            # re-run has nothing left to move. That is by design, but the receipt said
            # nothing, so "0 placed" read like "everything was already outside".
            outside_warning=(
                "%d 条新插入的尺寸没有被推到视图外（placed_outside=0）。"
                "place_outside 只处理「本次插入」的尺寸，`skip_before` 会跳过已存在的；"
                "如果这是第二次调用，新尺寸很可能与旧尺寸重复——请先核对再保留。"
                % count
                if (place_outside and count and not outside_placed) else None),
            outside_effective=bool(place_outside and outside_placed),
            placed_outside=outside_placed,
            # The split matters: 4.0 s for 11 dimensions means one of these three calls is
            # ~90 ms. Reported so the next change is chosen from numbers.
            outside_sides=outside_sides,
            outside_failed=outside_failed[:10],
            advisory=notes,
            empty_views=[v.get("name") for v in (audit.get("views") or [])
                         if int(v.get("dimensions") or 0) == 0],
            views=audit.get("views"),
            warnings=audit.get("warnings"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("auto_dimension_failed", str(exc))


def set_view_scale(drawing_path, view_name, scale, save=True):
    """Change ONE view's scale on an existing drawing.

    `create_drawing` sets the scale once and there was no way back: a report rebuilt the
    whole sheet three times because a view needed 1:2 instead of 1:5. `ScaleDecimal` takes
    a plain assignment (unlike `Position`, which needs a VARIANT PROPERTYPUT).
    """
    try:
        try:
            value = float(scale)
        except Exception:
            return error("bad_scale", "scale must be a number, got %r" % (scale,))
        if value <= 0:
            return error("bad_scale", "scale must be positive, got %r" % (scale,))
        doc = _open_drawing(drawing_path)
        target = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if str(view.Name) == str(view_name):
                target = view
                break
        if target is None:
            return error("view_not_found", "no model view named %r" % (view_name,),
                         "Use drawing_audit to list the view names on this sheet.")
        before = None
        try:
            before = round(float(target.ScaleDecimal), 6)
        except Exception:
            pass
        try:
            target.ScaleDecimal = value
        except Exception as exc:
            return error("scale_set_failed", str(exc)[:120],
                         "A locked/sheet scale blocks per-view scale changes.")
        after = None
        try:
            after = round(float(target.ScaleDecimal), 6)
        except Exception:
            pass
        try:
            _call_method(doc, "EditRebuild3")
            doc.GraphicsRedraw2()
        except Exception:
            pass
        if save:
            _save_drawing(doc)
        return drawing_ok(view=str(target.Name), scale_before=before, scale_after=after,
                          applied=bool(after is not None and abs(after - value) < 1e-9),
                          saved=bool(save))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("set_view_scale_failed", str(exc)[:120])


def move_view(drawing_path, view_name, x_m, y_m, save=True):
    """Move ONE existing view to a sheet point (metres).

    `tools/layout.py::set_view` already does this correctly — it reopens the drawing and
    writes `Position` through DISPATCH_PROPERTYPUT (a plain assignment lands at (0, x), the
    v0.8.9 trap). This exposes the same operation under the drawing-tool naming so a view
    can be nudged without rebuilding the whole sheet.
    """
    try:
        from . import layout as _layout

        return _layout.set_view(drawing_path, view_name,
                                x_m=float(x_m), y_m=float(y_m), save=bool(save))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("move_view_failed", str(exc)[:120])


def close_document(drawing_path):
    """Close a document SolidWorks still holds open.

    A report's `out/` kept two abandoned drafts it could not delete: "Device or resource
    busy" because SolidWorks still had them open, and the toolkit had no way to close
    anything. Every tool here opens documents; none of them ever let go.
    """
    try:
        from ..sw_session import get_sw

        software = get_sw()
        want = os.path.normcase(os.path.abspath(str(drawing_path)))
        closed = []
        for document in (software.GetDocuments or ()):
            try:
                if os.path.normcase(document.GetPathName or "") != want:
                    continue
                title = str(document.GetTitle)
                software.CloseDoc(title)
                closed.append(title)
            except Exception:
                continue
        return drawing_ok(
            drawing=os.path.basename(str(drawing_path)),
            closed=closed, closed_count=len(closed),
            note=("未找到已打开的该文档（可能本来就没开）。" if not closed
                  else "已关闭 %d 个。" % len(closed)),
        )
    except Exception as exc:
        return error("close_failed", str(exc)[:120],
                     "Check that SolidWorks is reachable; a busy dialog blocks CloseDoc.")


@perf_hooks.timed("center_marks")
def add_center_marks(drawing_path, include_slots=True, extended_lines=True, view_name=None,
                     save=True):
    """Auto-insert center marks and centerlines on every hole/slot in every view.

    Requires activating each view first — drawing annotation ops act on the active
    view. GetCenterMarkCount under-reports on this binding, so `views_marked` (the
    API's own success count) is the reliable signal, not the count.

    `view_name` restricts it to one view. Without it the isometric view got marks too —
    a report saw 15 cross marks scattered across a 3D pictorial, which is simply wrong
    drafting.
    """
    try:
        doc = _open_drawing(drawing_path)
        insert_type = 1 | (4 if include_slots else 0)  # Hole = 1, Slots = 4
        insert_option = 2  # swCenterMarkStyle_e.swCenterMark_Single
        processed = marked = 0
        results = []
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and str(view.Name) != str(view_name):
                continue
            processed += 1
            try:
                doc.ActivateView(view.Name)
            except Exception:
                pass
            try:
                ok_view = view.AutoInsertCenterMarks2(
                    insert_type, insert_option, bool(include_slots), bool(include_slots),
                    True, 0.0025, 0.0, bool(extended_lines), True, 0.0,
                )
            except Exception as exc:
                results.append({"view": view.Name, "ok": False, "error": str(exc)})
                continue
            if ok_view:
                marked += 1
            results.append({"view": view.Name, "ok": bool(ok_view)})
        try:
            doc.EditRebuild3
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        layout = None
        # PR1: honour save=False so a pipeline run saves once at the end. This was the last
        # of the five sub-tools still saving unconditionally.
        if save:
            _save_drawing(doc)
        return ok(
            drawing=doc.GetTitle,
            layout=layout,
            views_processed=processed,
            views_marked=marked,
            views=results,
            note="GetCenterMarkCount under-reports on this binding; views_marked is the "
                 "reliable signal. Verify visually with render_drawing.",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("center_marks_failed", str(exc))


def _nearest_circle_edge(drawing_path, view_name, x, y, tolerance_m=0.005):
    """Circular edge of `view` nearest to sheet point (x, y), with its sheet centre.

    `doc.Extension.SelectByID2("", "EDGE", x, y)` cannot hit drawing edges by point (a
    v0.8.23 finding), so every single-point call on a perfectly valid hole returned
    `edge_not_found` — the report's 3/3 failures — while the batch tool succeeded on the
    same sheet, because that one ENUMERATES edges. Do the same: enumerate the view's
    circles, project each centre onto the sheet, take the nearest. x/y then only chooses
    WHICH hole instead of having to land exactly on the curve.

    Returns (match, None) or (None, reason).
    """
    import win32com.client as win

    doc = _open_drawing(drawing_path)
    view = None
    for candidate in _iter_views(doc):
        if getattr(candidate, "Type", None) == 1:
            continue
        if view_name and candidate.Name != view_name:
            continue
        view = candidate
        break
    if view is None:
        return None, "view_not_found"
    axis_map, reason = _view_axis_map(view)
    # `_view_axis_map` reads the part box through open_document(model_path), which switches
    # the ACTIVE document to the model — the view object and every edge taken from it are
    # stale afterwards, and `edge.Select2` on a stale handle silently returns False (that is
    # why the first attempt at this fix still ended in callout_failed). Come back to the
    # drawing and re-acquire both the view and its edges.
    doc = _open_drawing(drawing_path)
    view = None
    for candidate in _iter_views(doc):
        if getattr(candidate, "Type", None) == 1:
            continue
        if view_name and candidate.Name != view_name:
            continue
        view = candidate
        break
    if view is None:
        return None, "view went away after re-opening the drawing"
    try:
        doc.ActivateView(view.Name)
    except Exception:
        pass
    doc.ClearSelection2(True)
    try:
        position = list(view.Position)
        scale = float(view.ScaleDecimal)
    except Exception:
        return None, "view position unreadable"
    try:
        edges = [win.Dispatch(e) for e in (view.GetVisibleEntities2(view, 1) or ())]
    except Exception:
        return None, "view has no visible entities"

    best = None
    for edge in edges:
        try:
            curve = win.Dispatch(edge.GetCurve)
            if curve is None or not curve.IsCircle:
                continue
            params = list(curve.CircleParams)
            centre_model = (float(params[0]), float(params[1]), float(params[2]))
            radius_m = float(params[6])
        except Exception:
            continue
        if not axis_map:
            continue
        axes = axis_map["axes"]
        centre_m = axis_map["centre_m"]
        cx = position[0] + (centre_model[axes[0]] - centre_m[axes[0]]) * scale
        cy = position[1] + (centre_model[axes[1]] - centre_m[axes[1]]) * scale
        distance = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if best is None or distance < best[0]:
            best = (distance, edge, [round(cx, 5), round(cy, 5)],
                    round(radius_m * 2000, 2),
                    [round(centre_model[0], 6), round(centre_model[1], 6),
                     round(centre_model[2], 6)])
    if best is None:
        if not axis_map:
            return None, "axes_mismatch: %s" % (reason or "no model->sheet mapping")
        return None, "no circular edge in this view"
    if best[0] > tolerance_m:
        return None, ("nearest circular edge is %.1f mm away (allowed %.0f mm)"
                      % (best[0] * 1000, tolerance_m * 1000))
    # Deliberately returns NO edge: reading the part box switched the active document, so
    # every COM handle taken here belongs to a dead one. `Select2` on such a handle reports
    # success while `AddHoleCallout2` then returns None — the symptom that made this tool
    # look impossible to fix. The caller re-finds the circle by diameter on its own handle.
    return {"centre": best[2], "diameter_mm": best[3],
            "centre_model": best[4],
            "distance_m": round(best[0], 5)}, None


def add_hole_callout(drawing_path, x, y, view_name=None, model_path=None,
                     save=True):
    """Insert a NATIVE hole callout at a sheet point (meters) on a hole edge, then read
    it back (process §4.5: the tool's success flag is not evidence).

    Read-back returns the callout text and the dimensioned value. When model_path is
    given, the value is matched against the model's cylinder inventory so a wrong edge
    (callout on a Ø6 hole that should be Ø30) is reported instead of silently accepted.
    """
    try:
        doc = _open_drawing(drawing_path)
        target = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and view.Name != view_name:
                continue
            target = view
            break
        if target is not None:
            try:
                doc.ActivateView(target.Name)
            except Exception:
                pass
        before = 0
        if target is not None:
            try:
                before = int(target.GetAnnotationCount)
            except Exception:
                before = 0
        doc.ClearSelection2(True)
        # AddHoleCallout2 needs the hole's projected EDGE selected first: the selection is
        # what carries the owning view and the circular edge into the callout. Without it
        # SolidWorks returns null even at a correct point.
        from ..sw_session import byref_dispatch_null

        # Match by ENUMERATING circles first (see _nearest_circle_edge); coordinate
        # hit-testing stays only as a fallback for views whose axis mapping is unavailable.
        circle_match, why = _nearest_circle_edge(
            drawing_path, view_name or (str(target.Name) if target is not None else None),
            float(x), float(y))
        # The helper re-opens the drawing internally, so `doc` and `target` here are stale.
        doc = _open_drawing(drawing_path)
        target = None
        for _view in _iter_views(doc):
            if getattr(_view, "Type", None) == 1:
                continue
            if view_name and _view.Name != view_name:
                continue
            target = _view
            break
        if target is not None:
            try:
                doc.ActivateView(target.Name)
            except Exception:
                pass
        # x/y chooses WHICH hole (via the nearest circle); the leader anchor is placed just
        # outside the view, exactly like the batch tool. Passing the circle centre here made
        # AddHoleCallout2 return None even with the edge correctly selected — the text
        # anchor and the edge selection compete for the same point.
        callout_point = [float(x), float(y)]
        try:
            outline = list(target.GetOutline or ())
            if len(outline) >= 4:
                callout_point = [outline[2] + 0.015, (outline[1] + outline[3]) / 2.0]
        except Exception:
            pass
        circle_matched = None
        if circle_match is not None:
            circle_matched = {"diameter_mm": circle_match["diameter_mm"],
                              "distance_m": circle_match["distance_m"],
                              "centre_m": list(circle_match["centre"])}
            callout_point = list(circle_match["centre"])
            # Re-find and select the same circle on THIS handle. Matching by diameter keeps
            # it independent of the projection that the helper already did.
            import win32com.client as _win_pick

            picked = False
            _cands = []
            # Match the EXACT circle: several holes can share a diameter, and picking by
            # diameter alone annotates a different hole than the caller pointed at (that is
            # why the first attempt selected an edge, reported count 1, and still got null).
            _want = circle_match.get("centre_model") or []
            try:
                for _edge in [_win_pick.Dispatch(e)
                              for e in (target.GetVisibleEntities2(target, 1) or ())]:
                    try:
                        _curve = _win_pick.Dispatch(_edge.GetCurve)
                        if _curve is None or not _curve.IsCircle:
                            continue
                        _params = list(_curve.CircleParams)
                        if len(_want) == 3:
                            _d = sum(abs(float(_params[i]) - float(_want[i]))
                                     for i in range(3))
                            if _d > 1e-6:
                                continue
                        elif abs(float(_params[6]) * 2000
                                 - float(circle_match["diameter_mm"])) > 0.2:
                            continue
                        _cands.append((float(_params[2]), _edge))
                    except Exception:
                        continue
            except Exception:
                _cands = []
            # A hole has two concentric circles (near face and far face) and only the near
            # one is annotatable. Same rule as the batch tool: take the smallest z.
            for _z, _edge in sorted(_cands, key=lambda entry: entry[0]):
                try:
                    if _edge.Select2(False, 0):
                        picked = True
                        break
                except Exception:
                    continue
            if not picked:
                return error("select_failed",
                             "Found the Ø%g circle but could not select it on this document"
                             % circle_match["diameter_mm"],
                             "Use add_hole_callouts (batch) for this view instead.")
        else:
            selected = False
            for sel_type in ("EDGE", "SILHOUETTE"):
                try:
                    if doc.Extension.SelectByID2("", sel_type, float(x), float(y), 0,
                                                 False, 0, byref_dispatch_null(), 0):
                        selected = True
                        break
                except Exception:
                    continue
            if not selected:
                return error("edge_not_found",
                             "No hole edge near (%.4f, %.4f)" % (x, y),
                             "Pass the hole CENTRE from list_circular_edges.",
                             nearest=why)
        callout = doc.AddHoleCallout2(callout_point[0], callout_point[1], 0.0)
        if callout is None:
            # Keep the evidence: with the circle matched and its edge selected, a null here
            # means AddHoleCallout2 itself refused — not that the point was wrong. Reporting
            # the match lets the next round tell those apart without re-running blind.
            _sel_count = None
            try:
                _sel_count = int(doc.SelectionManager.GetSelectedObjectCount2(-1))
            except Exception:
                pass
            return error(
                "callout_failed",
                "AddHoleCallout2 returned None at (%.4f, %.4f)" % tuple(callout_point),
                "Circle {dia} matched at distance {dist} m; selection before the call: "
                "{sel}. With a match and a selection, SolidWorks still refused — this "
                "circle is probably not an annotatable hole (an outer arc, or the far face). "
                "The batch tool has the same limit: on door.SLDPRT it annotates Ø8/Ø15 and "
                "never the four Ø6 circles. Pick another circle from list_circular_edges."
                .format(
                    dia=(circle_matched or {}).get("diameter_mm"),
                    dist=(circle_matched or {}).get("distance_m"),
                    sel=_sel_count),
                matched_circle=circle_matched, selected_before_call=_sel_count)

        readback = {}
        # GetText is a method on some builds and a property on others: call it, and only
        # fall back to a bare attribute read when calling raises.
        try:
            text_value = callout.GetText()
            readback["text"] = str(text_value) if not callable(text_value) else None
        except Exception:
            try:
                text_value = callout.GetText
                readback["text"] = str(text_value) if not callable(text_value) else None
            except Exception:
                readback["text"] = None
        try:
            dim = callout.GetDimension2(0)
            if dim is not None:
                readback["value_mm"] = round(float(dim.SystemValue) * 1000, 3)
        except Exception:
            pass
        if not readback.get("value_mm"):
            # A callout may hang off a display dimension rather than a plain dimension.
            try:
                readback["value_mm"] = round(float(callout.SystemValue) * 1000, 3)
            except Exception:
                pass

        match = None
        if model_path:
            try:
                from . import ledger as _ledger

                led = _ledger.build_feature_ledger(model_path, include_faces=True)
                values = sorted({round(h["diameter_mm"], 1) for h in (led.get("holes") or [])})
                got = readback.get("value_mm")
                if got is not None and values:
                    nearest = min(values, key=lambda v: abs(v - got))
                    match = {
                        "read_mm": got,
                        "nearest_model_diameter_mm": nearest,
                        "delta_mm": round(got - nearest, 3),
                        "ok": abs(got - nearest) <= max(0.2, nearest * 0.02),
                        "model_diameters_mm": values[:20],
                    }
            except Exception as exc:
                match = {"error": str(exc)[:120]}

        after = 0
        if target is not None:
            try:
                after = int(target.GetAnnotationCount)
            except Exception:
                after = 0
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        if save:
            _save_drawing(doc)
        return drawing_ok(
            drawing=doc.GetTitle,
            created=True,
            view=target.Name if target is not None else None,
            annotations_before=before,
            annotations_after=after,
            readback=readback,
            model_match=match,
            # Which circle the point resolved to. Without it a caller cannot tell "the hole
            # I meant" from "the nearest hole", and this tool used to fail outright instead.
            matched_circle=circle_matched,
            saved=bool(save),
            note="比对 model_match.ok；false 表示标注落在别处或模型无对应孔径。",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("hole_callout_failed", str(exc))


def ann_position(note):
    """Position of a note's annotation, best effort (None when unavailable).

    P3-4 of a 2026-11-03 report: `set_title_block` returned `cell_position_m: []` on three
    runs while claiming `via: "position"`, so there was no way to check WHICH cell it
    wrote. Two problems compounded:

      * only the property form was tried. `IAnnotation` members are split between
        properties and methods on this binding — `GetAnnotation` answers as a property on
        some objects and needs a call on others, and the old code only handled the first.
      * failures returned `[]`, which reads like "the position is the origin" rather than
        "unknown". An empty list and a genuine (0, 0) are indistinguishable downstream.

    Now both forms are tried and an unusable result is `None`.
    """
    # call_method, not `note.GetAnnotation()`: win32com dispatches that name as a
    # PROPERTYGET and the call form raises MEMBERNOTFOUND — the guard in static_check
    # enforces this, and it is why `ann_position` silently returned [] for so long.
    annotation = None
    try:
        annotation = call_method(note, "GetAnnotation")
    except Exception:
        annotation = None
    if annotation is None:
        try:
            annotation = note.GetAnnotation
        except Exception:
            annotation = None
    if annotation is None:
        return None
    for getter in (lambda: annotation.GetPosition, lambda: annotation.GetPosition()):
        try:
            values = list(getter() or ())
            if len(values) >= 2:
                return values
        except Exception:
            continue
    return None


def _sheet_notes(sheet):
    """All INote objects on the sheet (annotations whose type is 6)."""
    import win32com.client as win

    flat = []

    def collect(obj):
        if isinstance(obj, (tuple, list)):
            for item in obj:
                collect(item)
        elif obj is not None:
            flat.append(win.Dispatch(obj))

    try:
        collect(sheet.GetAnnotations)
    except Exception:
        return []
    notes = []
    for ann in flat:
        try:
            if ann.GetType != 6:  # swAnnotationType_Note
                continue
            notes.append(ann.GetSpecificAnnotation)
        except Exception:
            continue
    return notes


# GB A3 title-block slot centres, measured on gb_a3.drwdot. Some templates leave these
# cells BLANK (no placeholder text at all), so text matching alone cannot fill them.
TITLE_SLOTS = {
    "name": (0.391, 0.036),
    "code": (0.391, 0.022),
    "material": (0.344, 0.021),
}


def _note_at(sheet, target, tolerance=0.006):
    """Sheet note whose anchor is nearest `target` (metres), within tolerance."""
    import math
    import win32com.client as win

    try:
        raw = sheet.GetAnnotations
    except Exception:
        return None
    items = list(raw) if isinstance(raw, (tuple, list)) else ([raw] if raw else [])
    best = None
    for item in items:
        if item is None:
            continue
        try:
            ann = win.Dispatch(item)
            if ann.GetType != 6:  # notes only
                continue
            pos = list(ann.GetPosition)
            note = ann.GetSpecificAnnotation
        except Exception:
            continue
        distance = math.hypot(pos[0] - target[0], pos[1] - target[1])
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, note)
    return best[1] if best else None


def _note_name(note):
    """Reported name of a title-block cell, so the caller can verify what was written.

    P3-4: the field locator falls back to coordinates, and on a GB template those cells are
    easy to confuse — one report found the `material` slot occupied by the drawing code
    (GZ1000/8.06-02) until it was corrected. Echoing the cell's own annotation name plus its
    position makes that kind of mix-up visible in the receipt instead of only in the PDF.
    """
    if note is None:
        return None
    try:
        return str(_call_method(note, "GetAnnotation").GetName)
    except Exception:
        return None


def set_title_block(drawing_path, name=None, code=None, material=None, apply=False,
                   include_reversed=True, scale=None):
    """Fill title-block placeholders such as the literal text “图样名称” / “图样代号”.

    GB templates ship these as plain note text with quotes, so nothing is linked to
    the model and every drawing shows the placeholder. Dry-run by default; only the
    fields you pass are touched, unknown fields are reported, never invented.

    `图样代号` appears twice: in the title block (0 deg) and at the sheet's top-left
    corner rotated 180 deg — the GB convention that makes the number readable after
    folding the sheet. Both are filled so the drawing stays consistent; pass
    include_reversed=False to leave the rotated marker alone.
    """
    try:
        doc = _open_drawing(drawing_path)
        sheet = doc.GetFirstView
        notes = _sheet_notes(sheet)
        planned, unmatched, skipped_reversed, skipped_extra = [], [], [], []

        wants = []
        if name is not None:
            wants.append(("name", "图样名称", str(name)))
        if code is not None:
            wants.append(("code", "图样代号", str(code)))
        if material is not None:
            wants.append(("material", "材质", str(material)))

        # The scale cell holds a bare value like "1:5", not a labelled field, so the
        # marker-based loop below never finds it. Match the value pattern instead — and
        # only inside the title-block band, or a note that happens to read "1:5" elsewhere
        # would be overwritten.
        if scale is not None:
            import re as _re

            scale_pattern = _re.compile(r"^\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\s*$")
            try:
                sheet_box = list(sheet.GetOutline or [])
                sheet_h = (sheet_box[3] - sheet_box[1]) if len(sheet_box) >= 4 else 0.297
            except Exception:
                sheet_h = 0.297
            for note in notes:
                try:
                    text = note.GetText
                except Exception:
                    continue
                if not isinstance(text, str) or not scale_pattern.match(text):
                    continue
                try:
                    position = [round(v, 4) for v in ann_position(note)]
                except Exception:
                    position = None
                if position and position[1] > sheet_h * 0.15:
                    continue  # outside the title-block band
                planned.append({
                    "field": "scale", "before": text, "after": str(scale), "note": note,
                    "angle_deg": 0.0, "position": position,
                })
            if not any(p["field"] == "scale" for p in planned):
                unmatched.append({"field": "scale", "marker": "n:m",
                                  "reason": "title block has no scale value in the bottom band"})

        for field, marker, new_value in wants:
            candidates = []
            for note in notes:
                try:
                    text = note.GetText
                except Exception:
                    continue
                if not isinstance(text, str) or marker not in text:
                    continue
                try:
                    position = [round(v, 4) for v in ann_position(note)]
                except Exception:
                    position = None
                try:
                    angle = float(note.Angle)
                except Exception:
                    angle = 0.0
                reversed_note = abs(angle) > 0.01
                if reversed_note and not include_reversed:
                    skipped_reversed.append({
                        "field": field, "text": text,
                        "angle_deg": round(angle * 180 / 3.141592653589793, 1)})
                    continue
                if field == "material":
                    before = text
                    after = text.replace("<未指定>", new_value).replace("<未指定 >", new_value)
                    if after == before:
                        after = ("材质 %s" % new_value if "<" not in text
                                 else text.replace(
                                     text[text.find("<"):text.find(">") + 1], new_value))
                else:
                    before = text
                    after = new_value
                candidates.append({
                    "field": field, "before": before, "after": after, "note": note,
                    "angle_deg": round(angle * 180 / 3.141592653589793, 1),
                    "position": position, "_reversed": reversed_note,
                })

            if candidates:
                # P1-4: several cells can carry the marker. A GB frame repeats 图样代号 in a
                # 180-degree note OUTSIDE the frame, and writing both put a second code in
                # the corner (measured: code at [0.053, 0.285] and [0.391, 0.022]). Keep
                # ONE — upright first, then nearest the configured slot.
                slot = TITLE_SLOTS.get(field)

                def _rank(candidate, _slot=slot):
                    distance = 0.0
                    if _slot and candidate.get("position"):
                        dx = candidate["position"][0] - _slot[0]
                        dy = candidate["position"][1] - _slot[1]
                        distance = (dx * dx + dy * dy) ** 0.5
                    return (1 if candidate["_reversed"] else 0, distance)

                candidates.sort(key=_rank)
                chosen = candidates[0]
                chosen.pop("_reversed", None)
                planned.append(chosen)
                for entry in candidates[1:]:
                    skipped_extra.append({
                        "field": field, "text": entry.get("before"),
                        "position": entry.get("position"),
                        "angle_deg": entry.get("angle_deg"),
                        "reason": "another cell carries the same marker; kept the upright "
                                  "one nearest the configured slot"})
            else:
                slot = TITLE_SLOTS.get(field)
                note = _note_at(sheet, slot) if slot else None
                if note is not None:
                    try:
                        before = note.GetText
                    except Exception:
                        before = ""
                    planned.append({
                        "field": field, "before": before if isinstance(before, str) else "",
                        "after": new_value, "note": note, "via": "position",
                    })
                elif slot:
                    # P1-4 third tier: the cell can be an EMPTY table cell with no note at
                    # all — the report on gb_a3.drwdot came back with planned: [] for every
                    # field. Create the note at the slot instead of declaring it unmatched.
                    created_note = None
                    try:
                        created_note = doc.InsertNote(str(new_value))
                    except Exception:
                        created_note = None
                    if created_note is not None:
                        try:
                            import win32com.client as _win_tb

                            _created_ann = _win_tb.Dispatch(
                                _call_method(created_note, "GetAnnotation"))
                            # SetPosition2 MUST go through call_method: pywin32 dispatches it
                            # as a property and the call raises MEMBERNOTFOUND.
                            _call_method(_created_ann, "SetPosition2",
                                         float(slot[0]), float(slot[1]), 0.0)
                        except Exception:
                            pass
                        try:
                            created_note.SetHeight(0.0025)
                        except Exception:
                            pass
                        planned.append({
                            "field": field, "before": "", "after": new_value,
                            "note": created_note, "via": "created",
                        })
                    else:
                        unmatched.append({
                            "field": field, "marker": marker,
                            "reason": "no matching text and InsertNote failed"})
                else:
                    unmatched.append({"field": field, "marker": marker})

        result = {
            "drawing": doc.GetTitle,
            "planned": [dict({k: v for k, v in p.items() if k != "note"},
                             note_name=_note_name(p.get("note")),
                             cell_position_m=([round(v, 4) for v in ann_position(p["note"])]
                                              if p.get("note") is not None else None))
                        for p in planned],
            "unmatched": unmatched,
            "skipped_reversed": skipped_reversed,
            "skipped_extra_matches": skipped_extra,
            "applied": False,
        }
        if not apply:
            result["dry_run"] = True
            result["message"] = "Nothing changed. apply=true writes these title-block fields."
            return ok(**result)

        written = []
        for item in planned:
            cell = {
                "note_name": _note_name(item.get("note")),
                "cell_position_m": ([round(v, 4) for v in ann_position(item["note"])]
                                    if item.get("note") is not None else None),
            }
            try:
                item["note"].SetText(item["after"])
                written.append(dict({"field": item["field"], "before": item["before"],
                                     "after": item["after"], "ok": True}, **cell))
            except Exception as exc:
                written.append(dict({"field": item["field"], "ok": False,
                                     "error": str(exc)}, **cell))
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        doc.SaveSilent
        result.update(applied=True, written=written, saved=True)
        return ok(**result)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("title_block_failed", str(exc))


def _projection_axes(view, edges):
    """Which two model axes the view projects (xy=front, xz=top, zy=right).

    Chosen by matching the view's outline aspect ratio against the model extents.
    Dimension values are PROJECTED lengths, so this must be known before comparing
    a new dimension against existing ones (a 1253 mm edge can dimension as 37.6 mm).

    P2-13: the aspect ratio uses the DEFLATED outline. GetOutline inflates width and height
    by the same 11.9 mm, which does not preserve the ratio — on a 70.9x39.9 view the raw
    ratio is 1.78 while the real 59x28 view is 2.11, enough to pick the wrong axis pair.
    """
    try:
        box = precise_outline(view) or list(view.GetOutline)
        width, height = box[2] - box[0], box[3] - box[1]
        if width <= 0 or height <= 0:
            return None
        ratio = width / height
    except Exception:
        return None

    pts = []
    for edge in edges:
        try:
            pts.append((edge.GetStartVertex.GetPoint, edge.GetEndVertex.GetPoint))
        except Exception:
            continue
    if not pts:
        return None
    lo = [min(min(a[i], b[i]) for a, b in pts) for i in range(3)]
    hi = [max(max(a[i], b[i]) for a, b in pts) for i in range(3)]
    size = [hi[i] - lo[i] for i in range(3)]

    best, best_err = None, None
    for axes in ((0, 1), (0, 2), (2, 1)):
        a, b = axes
        if size[a] <= 1e-6 or size[b] <= 1e-6:
            continue
        err = abs(size[a] / size[b] - ratio)
        if best_err is None or err < best_err:
            best, best_err = axes, err
    return best


def _edge_model_length(edge, axes):
    """Projected length along the two axes the view shows (this equals the dimension value)."""
    try:
        p1 = edge.GetStartVertex.GetPoint
        p2 = edge.GetEndVertex.GetPoint
    except Exception:
        return None
    if axes is None:
        return None
    a, b = axes
    return ((p1[a] - p2[a]) ** 2 + (p1[b] - p2[b]) ** 2) ** 0.5


def drawing_dimension_values(doc):
    """All dimension values already on the sheet, rounded to 0.1 mm.

    IView.GetDisplayDimensions only returns a subset (13 of 56 measured), so read the
    view's annotation list instead: dimension annotations have GetType() == 4, and the
    value comes from GetSpecificAnnotation().GetDimension2(0).SystemValue.
    """
    import win32com.client as win

    values = set()
    for view in _iter_views(doc):
        try:
            raw = view.GetAnnotations
        except Exception:
            continue
        items = list(raw) if isinstance(raw, (tuple, list)) else ([raw] if raw else [])
        for item in items:
            try:
                ann = win.Dispatch(item)
                if ann.GetType != 4:  # 4 = dimension annotation
                    continue
                dim = ann.GetSpecificAnnotation.GetDimension2(0)
                if dim is not None:
                    values.add(round(float(dim.SystemValue), 4))
            except Exception:
                continue
    return values


def _nearest_edge_at_sheet_point(view, x, y, tolerance):
    """Pick the visible edge whose midpoint projects nearest to sheet point (x, y).

    Drawing edges report MODEL coordinates, so a sheet point cannot be used directly.
    The view's projection plane is inferred from its outline aspect ratio against the
    model extents (xy = front, xz = top, zy = right), then each edge midpoint is mapped
    with paper = view.Position + (model - centre) * scale.

    Returns (distance_m, (p1, p2), projection) for the NEAREST edge even when that edge
    is OUTSIDE `tolerance`, so the caller can emit a self-calibration diagnostic instead
    of a bare "not found" (P1-8). `projection` carries the plane/pos/scale/centre needed
    to report both model and sheet coordinates. Returning None means only "no visible
    edges could be enumerated" — the tolerance check belongs to the caller.
    """
    import math
    import win32com.client as win

    try:
        edges = [win.Dispatch(e) for e in (view.GetVisibleEntities2(view, 1) or ())]
        outline = list(view.GetOutline)
        scale = float(view.ScaleDecimal)
        pos = list(view.Position)
    except Exception:
        return None
    if not edges or scale == 0:
        return None

    pts = []
    for edge in edges:
        try:
            p1 = edge.GetStartVertex.GetPoint
            p2 = edge.GetEndVertex.GetPoint
            pts.append((p1, p2))
        except Exception:
            continue
    if not pts:
        return None

    lo = [min(min(a[i], b[i]) for a, b in pts) for i in range(3)]
    hi = [max(max(a[i], b[i]) for a, b in pts) for i in range(3)]
    size = [hi[i] - lo[i] for i in range(3)]
    centre = [(hi[i] + lo[i]) / 2 for i in range(3)]

    width = outline[2] - outline[0]
    height = outline[3] - outline[1]
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    planes = {
        "xy": (0, 1),
        "xz": (0, 2),
        "zy": (2, 1),
    }
    best_plane, best_err = None, None
    for name, (a, b) in planes.items():
        if size[a] <= 1e-6 or size[b] <= 1e-6:
            continue
        err = abs(size[a] / size[b] - ratio)
        if best_err is None or err < best_err:
            best_plane, best_err = (a, b), err
    if best_plane is None:
        return None
    ai, bi = best_plane

    best = None
    for (p1, p2) in pts:
        mx = (p1[ai] + p2[ai]) / 2.0
        my = (p1[bi] + p2[bi]) / 2.0
        px = pos[0] + (mx - centre[ai]) * scale
        py = pos[1] + (my - centre[bi]) * scale
        distance = math.hypot(px - x, py - y)
        if best is None or distance < best[0]:
            best = (distance, (p1, p2))
    if best is None:
        return None
    plane_name = {(0, 1): "xy", (0, 2): "xz", (2, 1): "zy"}.get((ai, bi), "?")
    projection = {"plane": plane_name, "ai": ai, "bi": bi,
                  "pos": pos, "scale": scale, "centre": centre}
    return (best[0], best[1], projection)


def _view_at_point(doc, x, y):
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            continue
        try:
            # P2-13: hit-test the deflated box. GetOutline's phantom 11.9 mm margin made
            # points that sit just outside a view answer as "inside" it.
            o = precise_outline(view) or list(view.GetOutline)
        except Exception:
            continue
        if o[0] <= x <= o[2] and o[1] <= y <= o[3]:
            return view
    return None


def _view_outline_hint(doc):
    """'视图名 [l, b, r, t]' per view, for the point_outside_views error (P3-1)."""
    parts = []
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            continue
        try:
            outline = precise_outline(view) or list(view.GetOutline)
            name = str(view.Name)
        except Exception:
            continue
        if outline and len(outline) >= 4:
            parts.append("%s [%.3f, %.3f, %.3f, %.3f]" % (
                name, outline[0], outline[1], outline[2], outline[3]))
    return "；".join(parts) if parts else "（未能读取任何视图 outline）"


def _free_spot_for_view(doc, half_w, half_h, prefer_x, prefer_y, margin=0.006, exclude=None):
    """Lowest-clash sheet position for a view of ±half_w/±half_h (P1-2).

    Detail views were dropped at a fixed height (`sheet_h * 0.30`), so the second one landed
    at y = -0.0487 m — off the sheet — and overlapped its neighbour. Placement is decided
    BEFORE the view exists, from an estimate of its drawn size: scan a 10 mm grid over the
    sheet inner box, reject any cell that overlaps an existing view outline, and take the
    free cell closest to the requested spot so repeated calls stay predictable.

    Writing `IView.Position` on a detail view afterwards only moves it vertically (x snaps
    back), so x has to be right at CreateDetailViewAt4 time.
    """
    try:
        box = list(doc.GetFirstView.GetOutline)
        sheet_w, sheet_h = box[2] - box[0], box[3] - box[1]
    except Exception:
        return None
    occupied = []
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            continue
        try:
            name = str(view.Name)
            if exclude and name == exclude:
                continue
            o = precise_outline(view) or list(view.GetOutline)
        except Exception:
            continue
        if o and len(o) >= 4:
            occupied.append((o[0] - margin, o[1] - margin, o[2] + margin, o[3] + margin))

    def _clash(x, y):
        left, right = x - half_w, x + half_w
        bottom, top = y - half_h, y + half_h
        if left < 0.008 or bottom < 0.008 or right > sheet_w - 0.008 or top > sheet_h - 0.008:
            return True
        for o in occupied:
            if not (right <= o[0] or left >= o[2] or top <= o[1] or bottom >= o[3]):
                return True
        return False

    step = 0.01
    best, best_d2 = None, None
    y = half_h + 0.008
    while y <= sheet_h - half_h - 0.008 + 1e-9:
        x = half_w + 0.008
        while x <= sheet_w - half_w - 0.008 + 1e-9:
            if not _clash(x, y):
                d2 = (x - prefer_x) ** 2 + (y - prefer_y) ** 2
                if best_d2 is None or d2 < best_d2:
                    best, best_d2 = [round(x, 4), round(y, 4)], d2
            x += step
        y += step
    return best


# Restored 0.8.25: the v0.8.24 fix bundle inserted _view_outline_hint / _free_spot_for_view
# right here and swallowed these two lines as context, leaving list_drawing_edges with an
# undefined name — `list_edges_failed name '_EDGE_CACHE' is not defined`, i.e. every edge
# coordinate read died. Caught by smoke, not by the bundle's own static check.
_EDGE_CACHE = {}
EDGE_CACHE_LIMIT = 32


def list_drawing_edges(drawing_path, view_name=None, include_points=True, flip_x=False):
    """List a view's visible edges with stable indices, so dimensions can be added
    without guessing sheet coordinates (which need a model->sheet transform this binding
    exposes unreliably).

    Performance notes — this used to be the slowest call in the toolkit (measured 335 s on
    a 1094-edge assembly view), almost all of it in fetching vertex coordinates:

        direct `edge.GetStartVertex`       335 s   (property dispatch, 3 COM calls per edge)
        sw_session.call_method             179 s   (DISPATCH_METHOD, 2x faster)
        GetCurveParams2 + evaluate          28 s   (12x faster, but only gives parameters)

    Two mitigations, both measured:

    * `include_points=False` returns indices only. Enumerating edges costs ~66 s on that
      view; adding coordinates is what pushes it past 300 s. Callers that only need to pick
      an edge by index should not pay for coordinates.
    * Results are cached per (drawing, view, include_points). The second call is instant.
      The cache is dropped when the drawing's mtime changes or after EDGE_CACHE_LIMIT
      entries, so a re-saved drawing re-reads instead of serving stale geometry.
    """
    try:
        import win32com.client as win

        from ..sw_session import call_method

        path = os.path.abspath(drawing_path)
        try:
            # flip_x belongs in the key: without it the second call returns the un-flipped
            # cached rows and the flag silently does nothing. (Measured 2026-11-03 — the
            # first attempt at flip_x appeared to have no effect for exactly this reason.)
            stamp = (path, os.path.getmtime(path), view_name, bool(include_points),
                     bool(flip_x))
        except OSError:
            stamp = None
        if stamp is not None and stamp in _EDGE_CACHE:
            cached = dict(_EDGE_CACHE[stamp])
            cached["cached"] = True
            return ok(**cached)

        doc = _open_drawing(path)
        target = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name is None or view.Name == view_name:
                target = view
                break
        if target is None:
            return error("view_not_found", "No model view found (view_name=%r)" % view_name)

        # Snapshot what the payload needs: `_view_axis_map` below opens the MODEL document
        # to read its bounding box, which switches the active document and invalidates both
        # `doc` and `target`.
        doc_title = None
        target_name = None
        view_pos = None
        view_scale = None
        try:
            doc_title = str(doc.GetTitle)
            target_name = str(target.Name)
            view_pos = [float(v) for v in target.Position]
            view_scale = float(target.ScaleDecimal)
        except Exception:
            pass

        rows = []
        raw_edges = []
        for component in (target.GetVisibleComponents or ()):
            try:
                raw_edges.extend(list(target.GetVisibleEntities2(component, 1) or ()))
            except Exception:
                continue
        if not raw_edges:
            # Some views return nothing per-component; fall back to the view itself.
            try:
                raw_edges = list(target.GetVisibleEntities2(target, 1) or ())
            except Exception:
                raw_edges = []

        for index, raw in enumerate(raw_edges):
            item = {"index": index}
            if include_points:
                try:
                    edge = win.Dispatch(raw)
                    # call_method: attribute access is dispatched as PROPERTYGET and costs
                    # roughly twice as much per edge.
                    p1 = win.Dispatch(call_method(edge, "GetStartVertex")).GetPoint
                    p2 = win.Dispatch(call_method(edge, "GetEndVertex")).GetPoint
                    item["model_start"] = [round(v, 4) for v in p1]
                    item["model_end"] = [round(v, 4) for v in p2]
                except Exception:
                    pass
                # Contour edges carry NO vertices on this binding (GetStartVertex returns
                # None), so a turned part — whose views are rings of circles — had nothing
                # to project and reported sheet_projected:false even once the axes were
                # known. `ICurve.CircleParams` still answers with centre + axis + radius,
                # which is everything needed: project the centre, scale the radius.
                if "model_start" not in item:
                    try:
                        edge = win.Dispatch(raw)
                        curve = win.Dispatch(call_method(edge, "GetCurve"))
                        params = list(curve.CircleParams or ())
                        if len(params) >= 7:
                            centre_pt = [float(params[0]), float(params[1]), float(params[2])]
                            radius = float(params[6])
                            item["model_center"] = [round(v, 4) for v in centre_pt]
                            item["radius_m"] = round(radius, 6)
                            item["is_circle"] = True
                            item["model_start"] = [round(v, 4) for v in centre_pt]
                            item["model_end"] = [round(v, 4) for v in centre_pt]
                    except Exception:
                        pass
            rows.append(item)

        # Project to sheet coordinates. `add_drawing_dimension` already does this for error
        # messages, so the transform exists — `model_*` alone cannot be used to aim at the
        # sheet, which is what a report hit ("用 index 定位这条路实际走不通").
        axis_map, projection_reason = _view_axis_map(target)
        projected = False
        if axis_map and view_pos is not None and view_scale:
            axes = axis_map["axes"]
            centre = axis_map["centre_m"]
            for item in rows:
                start = item.get("model_start")
                end = item.get("model_end")
                if not start or not end:
                    continue
                item["sheet_start"] = [
                    round(view_pos[0] + (start[axes[0]] - centre[axes[0]]) * view_scale, 5),
                    round(view_pos[1] + (start[axes[1]] - centre[axes[1]]) * view_scale, 5)]
                item["sheet_end"] = [
                    round(view_pos[0] + (end[axes[0]] - centre[axes[0]]) * view_scale, 5),
                    round(view_pos[1] + (end[axes[1]] - centre[axes[1]]) * view_scale, 5)]
                if item.get("radius_m"):
                    item["sheet_radius_m"] = round(item["radius_m"] * view_scale, 6)
                    item["sheet_center"] = list(item["sheet_start"])
                    # P3, 2026-11-03: for a circle, `sheet_start` and `sheet_end` are both
                    # the CENTRE — a closed curve has no endpoints. The docstring told
                    # callers to feed `sheet_start` to `add_drawing_dimension`, which for a
                    # circle means clicking the centre and selecting nothing.
                    #
                    # A real point on the circumference costs nothing to provide:
                    item["sheet_point_on_circle"] = [
                        round(item["sheet_center"][0] + item["sheet_radius_m"], 5),
                        round(item["sheet_center"][1], 5),
                    ]
                    item["sheet_start_is_center"] = True
                projected = True

        # P0 of a 2026-11-03 report: the sheet X coordinates came back MIRRORED about the
        # view centre — five edges on one shaft all satisfied `true = 2*centre - reported`,
        # with Y correct. The numbers looked plausible (inside the sheet, correct spacings),
        # so the only way to notice was to measure pixels in the exported PDF.
        #
        # Cause: `_view_axis_map` answers WHICH model axis a sheet axis shows, never which
        # DIRECTION. A right-side view runs +Z to the left, so a `+` projection mirrors it.
        #
        # I first tried to self-check by correlating model deltas against sheet deltas — that
        # is worthless, because both come from the same projection: they agree by
        # construction, mirrored or not. It answered "no mirror" on the exact view the report
        # had measured as mirrored.
        #
        # flip_x=True mirrors every sheet X about the view centre. Measured 2026-11-03 on
        # 旋转轴, right view at 2:1: the Ø14 collar came back at sheet x=0.12879 (right of
        # the centre 0.0888) while the rendered page puts it at 0.0487 (left). The mirror
        # is exact — 2*0.0888 - 0.12879 = 0.0488 — so a single call toggles it correctly.
        #
        # There is no way to decide this from the API: `_view_axis_map` gives the axis PAIR
        # but not its direction, and both the outline and the view position are symmetric
        # under the transform. Deriving it needs a pixel measurement, so the caller decides.
        if flip_x and projected:
            try:
                for _item in rows:
                    for _key in ("sheet_start", "sheet_end", "sheet_center"):
                        if _item.get(_key):
                            _item[_key][0] = round(2 * view_pos[0] - _item[_key][0], 5)
            except Exception:
                pass

        # So this does NOT claim the direction is right. It says the direction is UNVERIFIED
        # and that the only trustworthy check is a pixel measurement of the exported page.
        # Callers that place things by sheet coordinate (dimensions, leaders, datum tags)
        # should confirm one known feature against the PDF first.
        payload = {"drawing": doc_title, "view": target_name,
                   "edge_count": len(rows), "edges": rows,
                   "sheet_projected": projected,
                   "sheet_axis_direction_verified": bool(flip_x),
                   "sheet_flipped_x": bool(flip_x),
                   "sheet_axis_warning": (
                       "sheet X 的方向未经验证：侧视图的模型轴可能映射到图纸反方向"
                       "（2026-11-03 实测某 2:1 侧视图整体镜像，Y 正确 X 反号）。"
                       "本函数无法自证——自检只会验证自己的投影公式。"
                       "按坐标落标注前，请先导出 PDF 量一条已知要素核对。" if projected else None)}
        if not projected:
            payload["sheet_projection_reason"] = projection_reason or "no projection inputs"
        if stamp is not None:
            if len(_EDGE_CACHE) >= EDGE_CACHE_LIMIT:
                _EDGE_CACHE.clear()
            _EDGE_CACHE[stamp] = dict(payload)
        result = ok(**payload)
        result["cached"] = False
        return result
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("list_edges_failed", str(exc))


def add_distance_dimension(drawing_path, edge_a, edge_b, x_m, y_m, view_name=None,
                          save=False):
    """Dimension the distance BETWEEN two edges — the only way to get an overall length.

    P2-7 of a 2026-11-03 report: a 60 mm shaft could not be given a `60` dimension at all.
    `add_drawing_dimension` hits one edge and returns that edge's own length, so it can
    only produce the four segment lengths (18/29/3/10). The shaft's total is a SUM of
    segments — **no single edge in the view is 60 mm long** — so nothing could express it,
    and the drawing shipped with the most important dimension missing.

    `IModelDoc2.AddDimension2` creates a dimension from the current SELECTION, so picking
    two edges and calling it dimensionsthe gap between them:

        edge_a.Select2(False, 0)     # first
        edge_b.Select2(True, 0)      # append
        doc.AddDimension2(x, y, 0)   # x/y is where the text is placed

    Verified 2026-11-03 on 旋转轴: two edges 3 mm apart produced a dimension reading 3.0.
    Which pair gives the overall length depends on the view — use `list_drawing_edges` and
    pick the two outermost parallel edges.

    edge_a / edge_b are indices from `list_drawing_edges`. Returns the value it created so
    the caller can check it against the expected overall length instead of trusting it.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        target = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name is None or str(view.Name) == view_name:
                target = view
                break
        if target is None:
            return error("view_not_found", "no model view named %r" % view_name)
        doc.ActivateView(target.Name)
        doc.ClearSelection2(True)

        edges = []
        for component in (list(target.GetVisibleComponents or ())):
            try:
                edges.extend(list(target.GetVisibleEntities2(component, 1) or ()))
            except Exception:
                continue
        for label, index in (("edge_a", edge_a), ("edge_b", edge_b)):
            if index is None or index < 0 or index >= len(edges):
                return error("bad_edge_index",
                             "%s=%s out of range (%d edges)" % (label, index, len(edges)))

        first = win.Dispatch(edges[int(edge_a)])
        second = win.Dispatch(edges[int(edge_b)])
        if not first.Select2(False, 0):
            return error("select_failed", "could not select edge_a=%s" % edge_a)
        if not second.Select2(True, 0):
            return error("select_failed", "could not append edge_b=%s" % edge_b)

        raw = _invoke_as_method(doc, "AddDimension2", float(x_m), float(y_m), 0.0)
        if raw is None:
            return error(
                "distance_dimension_failed",
                "AddDimension2 returned null",
                "It needs exactly two compatible entities selected; two parallel edges of "
                "the same view normally qualify.")

        display = win.Dispatch(raw)
        value_mm = None
        try:
            dim2 = win.Dispatch(call_method(display, "GetDimension2", 0))
            value_mm = round(float(dim2.SystemValue) * 1000.0, 3)
        except Exception:
            pass
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        if save:
            _save_drawing(doc)

        return drawing_ok(
            drawing=doc.GetTitle, view=str(target.Name),
            edge_a=int(edge_a), edge_b=int(edge_b),
            value_mm=value_mm,
            position_m=[round(float(x_m), 4), round(float(y_m), 4)],
            note=("这是两条边之间的距离，不是任何一条边的长度。"
                  "总长这类「由多段相加得到」的尺寸只能这样标。"
                  "value_mm 是实际生成的值——请与期望总长核对，不要假定选对了边。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("distance_dimension_failed", str(exc))


def add_drawing_dimension(drawing_path, x=None, y=None, value=None, offset_x=0.0,
                          offset_y=-0.015, view_name=None, tolerance=0.01,
                          edge_index=None, skip_duplicates=False, known_values=None, save=True):
    """Add ONE dimension on the visible edge nearest sheet point (x, y), meters.

    This is the manual dimensioning path (process §4.4): InsertModelItems only carries
    dimensions the model already has, so IR-rebuilt parts need this to fill the gaps.

    The point does not have to be exactly on the line: the nearest visible edge within
    `tolerance` meters is selected, and the dimension is placed at (x+offset_x,
    y+offset_y). value is meters; omit it to keep the computed value.

    skip_duplicates (off by default) rejects values listed in `known_values`. It cannot
    infer them: a dimension's value is the PROJECTED length, and the same edge projects
    differently per view (1253 mm measured in 3D becomes 37.6 mm in the side view), so
    the check can only trust numbers the caller passes — in metres, as projected in the
    TARGET view. New dimensions are saved (save=True); without that they exist only in
    memory and vanish on reopen.
    """
    try:
        from ..sw_session import byref_dispatch_null

        doc = _open_drawing(drawing_path)
        # Compare in whole millimetres: the same dimension rounded to 0.1 mm still differs
        # in the last float digit between views, and drafting only cares about the number.
        existing = set()
        if skip_duplicates:
            # The annotation list is cached until the drawing rebuilds, so a dimension
            # added moments ago is invisible without this.
            try:
                doc.EditRebuild3
            except Exception:
                pass
            existing |= {round(v * 1000) for v in drawing_dimension_values(doc)}
            existing |= {round(float(v) * 1000) for v in (known_values or [])}
        view = None
        if view_name:
            for candidate in _iter_views(doc):
                if getattr(candidate, "Name", None) == view_name:
                    view = candidate
                    break
        if view is None and x is not None and y is not None:
            view = _view_at_point(doc, float(x), float(y))
        if view is not None:
            try:
                doc.ActivateView(view.Name)
            except Exception:
                pass

        doc.ClearSelection2(True)
        selected = None
        distance = None
        picked_length = None

        if edge_index is not None:
            if view is None:
                return error("view_not_found", "edge_index requires view_name")
            import win32com.client as win

            edges = [win.Dispatch(e) for e in (view.GetVisibleEntities2(view, 1) or ())]
            if edge_index < 0 or edge_index >= len(edges):
                return error("bad_index", "edge_index %d out of range (0..%d)"
                             % (edge_index, len(edges) - 1))
            try:
                target_edge = edges[edge_index]
                picked_length = _edge_model_length(target_edge, _projection_axes(view, edges))
                picked = target_edge.Select2(False, 0)
            except Exception:
                picked = False
            if not picked or doc.SelectionManager.GetSelectedObjectCount2(-1) < 1:
                return error("select_failed", "Could not select edge %d" % edge_index)
            selected = "edge_index"

        # Fast path: coordinate pick (works for some entity kinds on some builds).
        if selected is None and x is not None and y is not None:
            for candidate in ("EDGE", "SILHOUETTE", "SKETCHSEGMENT"):
                try:
                    if doc.Extension.SelectByID2("", candidate, float(x), float(y), 0,
                                                 False, 0, byref_dispatch_null(), 0):
                        selected = candidate
                        break
                except Exception:
                    continue

        if selected is None:
            # Two very different situations used to share one message (P3-1): "you gave me
            # a point that lies outside every view" was reported as "no sheet point given",
            # so the caller kept re-sending x/y that could never work. Keep them apart.
            if x is None or y is None:
                return error(
                    "missing_point",
                    "No edge selected and no sheet point given",
                    "Pass edge_index (+view_name) or sheet x/y in metres.",
                )
            if view is None:
                return error(
                    "point_outside_views",
                    "点 (%.4f, %.4f) 不在任何视图范围内" % (float(x), float(y)),
                    "可用视图范围（米，left/bottom/right/top）：%s。"
                    "改成视图内的点，或直接用 edge_index。" % _view_outline_hint(doc),
                )
            # Fall back to projecting every visible edge's midpoint onto the sheet and
            # taking the nearest one — SelectByID2 cannot hit drawing edges by coordinates.
            hit = _nearest_edge_at_sheet_point(view, float(x), float(y), float(tolerance))
            if hit is None:
                return error(
                    "segment_not_found",
                    "No visible edges could be enumerated in this view",
                    "The view may be blank, not activated, or not rebuilt yet. Activate "
                    "it and retry, or pass edge_index from list_drawing_edges.",
                )
            import win32com.client as win

            distance, midpoints, proj = hit
            if distance > float(tolerance):
                # P1-8: a bare "not found" left callers unable to tell whether x/y were
                # in sheet metres or model coordinates. Report BOTH, plus the distance,
                # so one read is enough to self-calibrate the next call.
                ai, bi = proj["ai"], proj["bi"]
                m1, m2 = midpoints
                mid_model = [(m1[i] + m2[i]) / 2.0 for i in range(3)]
                nearest_sheet = [
                    round(proj["pos"][0] + (mid_model[ai] - proj["centre"][ai]) * proj["scale"], 4),
                    round(proj["pos"][1] + (mid_model[bi] - proj["centre"][bi]) * proj["scale"], 4),
                ]
                return error(
                    "segment_not_found",
                    "No visible edge within %.1f mm of sheet point (%.4f, %.4f)"
                    % (float(tolerance) * 1000, float(x), float(y)),
                    "Nearest edge is %.2f mm away. Re-aim at nearest_edge_sheet_m, raise "
                    "tolerance, or pass edge_index from list_drawing_edges."
                    % (distance * 1000),
                    requested_sheet_m=[round(float(x), 4), round(float(y), 4)],
                    nearest_edge_sheet_m=nearest_sheet,
                    nearest_edge_model_m={
                        "start": [round(float(v), 5) for v in m1],
                        "end": [round(float(v), 5) for v in m2],
                    },
                    nearest_edge_distance_mm=round(distance * 1000, 2),
                    tolerance_mm=round(float(tolerance) * 1000, 2),
                    projection_plane=proj["plane"],
                )
            picked = None
            for edge in [win.Dispatch(e) for e in (view.GetVisibleEntities2(view, 1) or ())]:
                try:
                    p1 = edge.GetStartVertex.GetPoint
                    p2 = edge.GetEndVertex.GetPoint
                except Exception:
                    continue
                if (abs(p1[0] - midpoints[0][0]) < 1e-9 and abs(p1[1] - midpoints[0][1]) < 1e-9
                        and abs(p2[0] - midpoints[1][0]) < 1e-9 and abs(p2[1] - midpoints[1][1]) < 1e-9):
                    picked = edge
                    break
            if picked is None or not picked.Select2(False, 0):
                return error("select_failed", "Could not select the nearest edge",
                             "Retry with edge_index from list_drawing_edges.")
            try:
                picked_length = _edge_model_length(
                    picked, _projection_axes(view, [win.Dispatch(e) for e in (view.GetVisibleEntities2(view, 1) or ())]))
            except Exception:
                picked_length = None
            selected = "edge_by_point"

        if x is None or y is None:
            try:
                base_x, base_y = list(view.Position)[:2]
            except Exception:
                base_x, base_y = 0.2, 0.2
        else:
            base_x, base_y = float(x), float(y)
        display_dim = doc.AddDimension2(base_x + float(offset_x), base_y + float(offset_y), 0)
        if display_dim is None:
            doc.ClearSelection2(True)
            return error(
                "dimension_failed",
                "SolidWorks rejected the dimension at (%.4f, %.4f)" % (base_x, base_y),
                "The entity may not be dimensionable (e.g. a construction line or a "
                "silhouette that already has a driver).",
            )
        # A wrong edge still yields a dimension — it just measures something else (report:
        # edge_index 71 pointed at a Ø36 arc and the tool produced R23 of the Ø46 outline,
        # a "plausible" wrong value nobody would question). A dimension attached to nothing
        # is the cheap signal that the selection did not take.
        if edge_index is not None:
            try:
                _ann = _call_method(display_dim, "GetAnnotation")
                _types = list(_ann.GetAttachedEntityTypes or ())
                if not _types:
                    doc.ClearSelection2(True)
                    try:
                        _call_method(doc, "EditDelete")
                    except Exception:
                        pass
                    return error(
                        "edge_mismatch",
                        "Dimension at edge_index=%s attached to no geometry" % edge_index,
                        "The indexed edge and the selected edge disagree. Re-read the "
                        "indices with list_drawing_edges, or pass x/y instead.")
            except Exception:
                pass
        dim = display_dim.GetDimension2(0)
        applied = None
        try:
            applied = float(dim.SystemValue) if dim is not None else None
        except Exception:
            applied = None
        if value is not None and dim is not None:
            try:
                dim.SystemValue = float(value)
                applied = float(dim.SystemValue)
            except Exception:
                pass
        # Dimensions are per-value: the same length annotated twice on two views is a
        # drafting error, so skip values that already exist anywhere on the sheet.
        if skip_duplicates and picked_length is not None and round(picked_length * 1000) in existing:
            doc.ClearSelection2(True)
            return drawing_ok(
                drawing=doc.GetTitle, created=False, selected=selected,
                view=view.Name if view is not None else None,
                value_m=float(picked_length), skipped="duplicate_value",
                note="The same dimension already exists on this sheet.",
            )

        doc.ClearSelection2(True)
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        if save:
            # Without this the dimension exists only in memory and disappears on reopen.
            try:
                doc.SaveSilent
            except Exception:
                pass
        return drawing_ok(
            drawing=doc.GetTitle,
            created=True,
            selected=selected,
            saved=bool(save),
            view=view.Name if view is not None else None,
            distance_m=round(distance, 6) if distance is not None else None,
            value_m=applied,
            note="Verify the dimension text and its attachment, then export a PDF.",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("add_dimension_failed", str(exc))


def _template_lines(sheet, sheet_w=None, sheet_h=None):
    """Template geometry as (x1, y1, x2, y2) sheet-space segments.

    GetLines3 returns 12 numbers per line: 6 of metadata then x1,y1,z1,x2,y2,z2.
    (Read naively with a stride of 6 it looks like garbage every other record.)
    """
    try:
        raw = sheet.GetLines3
    except Exception:
        return []
    if not raw:
        return []
    values = list(raw)
    limit_x = (sheet_w or 0.6) * 1.05
    limit_y = (sheet_h or 0.45) * 1.05
    segments = []
    for base in range(0, len(values) - 5, 6):
        try:
            x1, y1, x2, y2 = values[base], values[base + 1], values[base + 3], values[base + 4]
        except Exception:
            continue
        # Each record carries 6 metadata values before the coordinates, so a naive
        # stride-6 read also yields junk like (0, 2, 1, 1); drop anything outside
        # the sheet instead of guessing the record layout.
        if not (-1e-6 <= x1 <= limit_x and -1e-6 <= y1 <= limit_y):
            continue
        if not (-1e-6 <= x2 <= limit_x and -1e-6 <= y2 <= limit_y):
            continue
        segments.append((x1, y1, x2, y2))
    return segments


def template_safe_area(sheet, sheet_w, sheet_h):
    """Usable region of a sheet: inside the frame, clear of the title block / parts list.

    GB templates vary with paper size, so the margins are measured from the template's
    own lines instead of being hard-coded for A3 (which is why A2 layouts overflowed).
    """
    segments = _template_lines(sheet, sheet_w, sheet_h)
    fallback = {
        "left": sheet_w * 0.14, "right": sheet_w * 0.01,
        "top": sheet_h * 0.012, "bottom": sheet_h * 0.22,
        "source": "ratio",
    }
    if len(segments) < 8:
        return fallback

    horizontals = [s for s in segments if abs(s[1] - s[3]) < 1e-5 and abs(s[2] - s[0]) > 0.004]
    verticals = [s for s in segments if abs(s[0] - s[2]) < 1e-5 and abs(s[3] - s[1]) > 0.004]
    if not horizontals or not verticals:
        return fallback

    # Use the outermost extent of ALL segments: larger sheets carry centre lines that
    # would otherwise be mistaken for the frame edge (A2 measured right=0.30 instead of 0.59).
    frame_left = min(min(s[0], s[2]) for s in segments)
    frame_right = max(max(s[0], s[2]) for s in segments)
    frame_bottom = min(min(s[1], s[3]) for s in segments)
    frame_top = max(max(s[1], s[3]) for s in segments)

    # Title block: the highest horizontal line sitting in the bottom-right corner.
    title_candidates = [
        s for s in horizontals
        if min(s[0], s[2]) > frame_left + (frame_right - frame_left) * 0.45
        and s[1] < frame_bottom + (frame_top - frame_bottom) * 0.30
    ]
    title_top = max((s[1] for s in title_candidates), default=frame_bottom)

    # Parts list / addendum column: the right-most vertical line in the left margin.
    # Its right edge is drawn as many short row separators, so the length threshold
    # must stay small or the column looks absent.
    list_candidates = [
        s for s in verticals
        if s[0] < frame_left + (frame_right - frame_left) * 0.30
        and max(s[1], s[3]) > frame_bottom + (frame_top - frame_bottom) * 0.30
    ]
    list_right = max((s[0] for s in list_candidates), default=frame_left)

    margin = 0.004
    # The measured left edge also catches the frame's row/column index strip, which is
    # ~30% of the sheet width on A3 — leaving that much empty would starve the views.
    # Cap the reserved band by ratio so the template still adapts across paper sizes.
    left_band = min(list_right - frame_left + margin, sheet_w * 0.14)
    bottom_band = min(title_top - frame_bottom + margin, sheet_h * 0.24)
    measured = {
        "left": frame_left + left_band,
        "right": sheet_w - frame_right + margin,
        "top": sheet_h - frame_top + margin,
        "bottom": bottom_band,
        "source": "template",
        "title_block_top": round(title_top, 4),
        "parts_list_right": round(list_right, 4),
    }

    # Sanity check before trusting the measurement. On A2 the frame scan missed the right
    # border entirely and reported a usable band of 212 mm out of 594 mm, which then drove
    # the scale down to 1:10 on a part that fits at 1:2. If the reserved margins eat more
    # than half the sheet, the scan is wrong — fall back to ratios, which are size-relative
    # and cannot collapse like that.
    reserved_x = measured["left"] + measured["right"]
    reserved_y = measured["bottom"] + measured["top"]
    if reserved_x > sheet_w * 0.5 or reserved_y > sheet_h * 0.5:
        fallback_used = dict(fallback)
        fallback_used["source"] = "ratio (template scan rejected: reserved %.0f%% x %.0f%%)" % (
            reserved_x / sheet_w * 100, reserved_y / sheet_h * 100)
        return fallback_used
    return measured


def add_flat_pattern_view(drawing_path, model_path, config_name=None, position=None,
                         scale=None, hide_bend_lines=False, flip=False):
    """Add a sheet-metal FLAT PATTERN view (the unfolded blank with bend lines).

    Sheet metal is dimensioned on its flat pattern — blank size, hole positions, bend
    directions — not on the folded view. The part must have a Flat-Pattern feature and
    be saved to disk.

    CreateFlatPatternViewFromModelView3(ModelName, ConfigName, LocX, LocY, LocZ,
    HideBendLines, FlipView) — 7 arguments, no config guessing: pass config_name or the
    part's active configuration is used.
    """
    try:
        from ..sw_session import get_sw, open_document, SW_DOC_PART

        model_path = os.path.abspath(model_path)
        if not os.path.exists(model_path):
            raise FileNotFoundError("file not found: %s" % model_path)
        if not model_path.lower().endswith(".sldprt"):
            return error("bad_input", "Flat pattern needs a sheet-metal .SLDPRT",
                         "Assemblies cannot be unfolded; pass the sheet-metal part.")
        doc = _open_drawing(drawing_path)
        sheet = doc.GetFirstView
        outline = list(sheet.GetOutline or [])
        sheet_w = (outline[2] - outline[0]) if outline else 0.42
        sheet_h = (outline[3] - outline[1]) if outline else 0.297

        if not config_name:
            try:
                model = open_document(model_path, SW_DOC_PART)
                config_name = model.ConfigurationManager.ActiveConfiguration.Name
            except Exception:
                config_name = "Default"
        config_name = config_name or "Default"

        if position:
            px, py = float(position[0]), float(position[1])
        else:
            safety = template_safe_area(sheet, sheet_w, sheet_h)
            px = safety["left"] + (sheet_w - safety["left"] - safety["right"]) / 2
            py = safety["bottom"] + (sheet_h - safety["bottom"] - safety["top"]) / 2

        view = doc.CreateFlatPatternViewFromModelView3(
            model_path, config_name, px, py, 0.0, bool(hide_bend_lines), bool(flip)
        )
        if view is None:
            return error(
                "flat_pattern_failed",
                "SolidWorks returned null for '%s' (config '%s')" % (
                    os.path.basename(model_path), config_name),
                "The part must be SHEET METAL (have a Flat-Pattern feature) and saved to disk.",
            )
        if scale:
            try:
                view.ScaleDecimal = float(scale)
            except Exception:
                pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        layout = _relayout(doc)
        _save_drawing(doc)
        try:
            o = list(view.GetOutline or [])
            box = {"width_m": round(o[2] - o[0], 4), "height_m": round(o[3] - o[1], 4)}
        except Exception:
            box = None
        return drawing_ok(
            drawing=doc.GetTitle, view=view.Name, config=config_name,
            scale=float(view.ScaleDecimal), outline=box, layout=layout,
            hide_bend_lines=bool(hide_bend_lines), flip=bool(flip),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("flat_pattern_failed", str(exc))


def _view_axis_map(view):
    """Which two MODEL axes does this view display? (P1-4)

    A cut plane given in model coordinates must be translated into the view's sketch, so
    the tool has to know which model axis each sheet axis shows. There is no reliable API
    for this on SW2024/2026 (measured 2026-10-31):
      - `IView.GetViewPlaneNormal` does not exist (property and method both fail);
      - `IView.ModelToViewTransform` returns 16 doubles that are NOT a plain row-major 4x4,
        and `IMathPoint.MultiplyTransform` does not exist either.

    So the mapping is matched by SIZE: the view's deflated outline (GetOutline minus the
    measured 11.9 mm constant, P2-13) must equal the part box extent along the two
    displayed axes. Returns (info, reason); info carries axes / centre / extent / path.
    An ambiguous match (e.g. a cube) is refused rather than guessed.

    The caller must re-activate the DRAWING afterwards: reading the part box goes through
    open_document(model_path), which switches SolidWorks' active document.
    """
    from ..sw_session import call_method

    try:
        model_path = str(call_method(view, "GetReferencedModelName") or "")
    except Exception:
        try:
            model_path = str(view.GetReferencedModelName or "")
        except Exception:
            model_path = ""
    if not model_path or not os.path.exists(model_path):
        return None, "no_referenced_model"

    # Read the view's own outline BEFORE switching documents.
    box = precise_outline(view)
    if not box:
        return None, "outline_too_small"
    width = box[2] - box[0]
    height = box[3] - box[1]
    # The outline is in SHEET metres and the part box is in MODEL metres, so the extents
    # have to be scaled before they can be compared. Without this factor every view that
    # is not 1:1 failed to match and the tool answered `axes_mismatch` — which is exactly
    # what a report saw on 30 of 30 circles, and what made single-point hole callouts
    # impossible (they need a circle centre in sheet coordinates).
    try:
        view_scale = float(view.ScaleDecimal)
    except Exception:
        view_scale = 1.0
    if view_scale <= 1e-9:
        view_scale = 1.0

    try:
        model = open_document(model_path)
        raw = list(model.GetPartBox(True) or ())
    except Exception:
        return None, "model_box_failed"
    if len(raw) < 6:
        return None, "model_box_missing"
    lo = [float(raw[i]) for i in range(3)]
    hi = [float(raw[i]) for i in range(3, 6)]
    extent = [hi[i] - lo[i] for i in range(3)]
    if min(extent) <= 1e-6:
        return None, "model_box_degenerate"
    centre = [(lo[i] + hi[i]) / 2 for i in range(3)]

    pairs = ((0, 1), (0, 2), (2, 1), (1, 0), (2, 0), (1, 2))
    scored = []
    for a, b in pairs:
        if extent[a] <= 1e-6 or extent[b] <= 1e-6:
            continue
        scored.append((abs(extent[a] * view_scale - width)
                       + abs(extent[b] * view_scale - height), (a, b)))
    if not scored:
        return None, "axes_not_found"
    scored.sort(key=lambda item: item[0])
    best_err, best = scored[0]
    if best_err > max(width, height) * 0.05:
        return None, "axes_mismatch"
    if len(scored) > 1 and abs(scored[1][0] - best_err) < 1e-6:
        # Two candidates tie. On this binding that is almost always the SAME pair in
        # reversed order — (0,2) vs (2,0), i.e. "is the model's Z across or up?" — which
        # is exactly what a turned part produces: extent [14,14,60] against a wide, flat
        # front view. Refusing outright excluded every shaft/sleeve/pin/flange from
        # coordinate-level annotation, and a report hit this on four tools in a row.
        #
        # Break the tie the way a draughtsman does: the view is drawn wide, so the model's
        # long axis lies ACROSS the sheet. Only fall back to "ambiguous" when the view is
        # itself nearly square (a cube), where no such habit applies.
        candidates = [pair for err, pair in scored if abs(err - best_err) < 1e-6]

        # Some ties are not ambiguous at all: when the two extents involved are EQUAL, the
        # reversed pair is the same mapping. 飞达动力轴 measures [35, 35, 107] and its front
        # view is a 35x35 circle, so (0,1) and (1,0) both fit perfectly and neither is
        # wrong — swapping two equal axes changes no coordinate. Refusing here excluded
        # every round part whose two radial extents happen to match, which is most of them.
        distinct = {tuple(sorted(pair)) for pair in candidates}
        if len(distinct) == 1:
            pair = candidates[0]
            if abs(extent[pair[0]] - extent[pair[1]]) < max(1e-9, extent[pair[0]] * 0.01):
                return {
                    "axes": [int(pair[0]), int(pair[1])],
                    "centre_m": centre,
                    "extent_m": extent,
                    "model_path": model_path,
                    "outline_m": [width, height],
                    "mapping_confidence": "axes_equal_interchangeable",
                    "note": ("The two projected extents are equal (%.1f mm), so the two "
                             "axis orders map identically — no ambiguity to resolve."
                             % (extent[pair[0]] * 1000)),
                }, None

        widened = width / height if height > 1e-9 else 1.0
        dominant = max(range(3), key=lambda i: extent[i])
        runner_up = sorted(extent, reverse=True)[1]
        stretched = runner_up > 1e-9 and extent[dominant] / runner_up >= 1.3
        picked = None
        if stretched and len(candidates) > 1:
            for pair in candidates:
                horizontal, vertical = pair
                if abs(widened - 1.0) < 0.2:
                    continue          # square view: the tie is genuine
                if widened > 1.2 and horizontal == dominant:
                    picked = pair
                    break
                if widened < 0.833 and vertical == dominant:
                    picked = pair
                    break
        if picked is not None:
            return {
                "axes": [int(picked[0]), int(picked[1])],
                "centre_m": centre,
                "extent_m": extent,
                "model_path": model_path,
                "outline_m": [width, height],
                "mapping_confidence": "inferred_from_aspect",
                "note": ("axes_ambiguous resolved by view aspect ratio: the model's long "
                         "axis (%d, %.1f mm) is drawn across the wider sheet direction. "
                         "Pass view_axis_hint if this is wrong." % (dominant, extent[dominant] * 1000)),
            }, None
        return None, "axes_ambiguous"
    return {
        "axes": [int(best[0]), int(best[1])],
        "centre_m": centre,
        "extent_m": extent,
        "model_path": model_path,
        "outline_m": [width, height],
    }, None


SECTION_KEEP_TOLERANCE = 0.10


def _section_sanity(source_view, section, orientation, depth):
    """Did the cut land on a real cross-section? (P1-4)

    A section plane perpendicular to the view's horizontal axis cannot change the extent
    along that axis, so the section's WIDTH must match the source view's width; same idea
    for a vertical cut and the height. Comparing exactly that dimension is geometry, not a
    heuristic.

    Measured 2026-10-31 on 灌装阀阀体 (source 59x28 mm after deflation):
      horizontal, offset -0.012 -> section 59x80, kept width 59.0 -> ratio 1.000 (correct)
      horizontal, offset +0.012 -> section 14x50, kept width 14.4 -> ratio 0.244 (wrong)
    A 10% tolerance separates those without flagging ordinary cross-sections. Partial
    sections (depth>0) legitimately shrink, so they are reported as not_checked.
    """
    src = precise_outline(source_view)
    sec = precise_outline(section)
    if not src or not sec:
        return {"checked": False,
                "reason": "outline too small for a deflated comparison"}
    src_w, src_h = src[2] - src[0], src[3] - src[1]
    sec_w, sec_h = sec[2] - sec[0], sec[3] - sec[1]
    info = {
        "checked": True,
        "method": "kept_dimension",
        "orientation": str(orientation),
        "source_size_mm": [round(src_w * 1000, 2), round(src_h * 1000, 2)],
        "section_size_mm": [round(sec_w * 1000, 2), round(sec_h * 1000, 2)],
        "tolerance": SECTION_KEEP_TOLERANCE,
    }
    if depth and float(depth) > 0:
        info["checked"] = False
        info["reason"] = ("partial section (depth>0) shrinks the view by design, so the "
                          "kept dimension is not comparable")
        return info

    # A CROSS-section is not comparable to the parent's overall extent, in either axis.
    # Cutting a shaft across its axis gives a disc the size of the LOCAL diameter: at Ø8
    # that is 8 mm against a 28 mm parent height, so the "kept height" ratio reads 0.55 and
    # the check called it suspicious — while the section was perfectly correct.
    #
    # It fails the other way too, which is worse: cutting at the Ø14 collar gives a section
    # height of exactly 14 = the parent's 14, so ratio 1.0 and the check PASSES even if the
    # cut is in the wrong place. Reported 2026-11-03 as "该拦的不拦、不该拦的拦".
    #
    # The discriminator is straight geometry: a cross-section is smaller than the parent in
    # BOTH axes; a proper longitudinal section keeps one axis at full size.
    # 0.85, not 0.7: a Ø10 disc against a 14 mm parent is 0.714 — measurably a cross-section
    # but just above the tighter threshold, and it was still being rejected. A genuine
    # longitudinal section keeps one axis at ~1.0, so 0.85 on BOTH axes still separates the
    # cases (measured: longitudinal 1.000, wrong-cut 0.244, cross-sections 0.55-0.71).
    cross_section = (sec_w < src_w * 0.85 and sec_h < src_h * 0.85)
    if cross_section:
        info["checked"] = False
        info["section_kind"] = "cross_section"
        info["reason"] = (
            "the section is smaller than the source in BOTH axes (%.1f x %.1f vs %.1f x "
            "%.1f mm), which is what a cut ACROSS the axis looks like — the parent's overall "
            "extent is not a valid reference for it, so the kept-dimension test does not "
            "apply. Acceptance relies on edge_count > 0 and hatch_count > 0 instead."
            % (sec_w * 1000, sec_h * 1000, src_w * 1000, src_h * 1000))
        return info
    if str(orientation) == "vertical":
        keep_src, keep_sec, which = src_h, sec_h, "height"
    else:
        keep_src, keep_sec, which = src_w, sec_w, "width"
    ratio = (keep_sec / keep_src) if keep_src else 0.0
    info["kept_dimension"] = which
    info["source_kept_mm"] = round(keep_src * 1000, 2)
    info["section_kept_mm"] = round(keep_sec * 1000, 2)
    info["ratio"] = round(ratio, 3)
    info["verdict"] = "ok" if abs(ratio - 1.0) <= SECTION_KEEP_TOLERANCE else "suspicious"
    return info


def add_section_view(drawing_path, view_name=None, orientation="horizontal",
                     offset_m=0.0, position=None, label="A", flip=False, depth=0.0,
                     cut_plane_normal=None, cut_plane_origin=None, verify_cut=True,
                     cut_line_ratio=0.55):
    """Create a SECTION VIEW through a model view.

    Sign convention (measured on the 灌装阀阀体 top view, 59x28x80, bypass hole at z=28):

      orientation="vertical"   cut line runs along the view's vertical axis, so offset_m
                               shifts along the *horizontal* axis. Negative = left.
                               A hole axis 15.55 mm left of centre needs offset_m=-0.01555.
      orientation="horizontal" cut line runs along the horizontal axis, so offset_m shifts
                               along the *vertical* axis — BUT the view sketch's y axis
                               points at -Z for a top view, so the sign is inverted inside
                               this function. Callers pass the model-space offset: to cut
                               at z=28 on a 0..80 part (centre 40) you pass -0.012, and
                               the tool negates it internally. Measured: -0.012 -> 59x80
                               (correct cross-section), +0.012 -> 14x50 (wrong plane).

    **Cut lines do not follow a moved parent view, and cannot be moved afterwards**
    (measured 2026-11-01: `IDrSection.GetLineInfo` is model-space and does not change when the
    view moves; `SetLineInfo` writes the section PLANE, not the drawn line; the view's sketch
    is unreachable). Place the parent view FIRST, then create the section, then never move it —
    or delete and rebuild. See check_capabilities `section_line_follows_view`.

    Both orientations take `offset_m` as model-space distance from the part centre, i.e.
    `offset_m = (target_model_coordinate - centre) * scale` in SHEET metres.

    The cut line is drawn in the view's own sketch — the view sketch coordinate system is
    `sketch = (paper - view.Position) / view.ScaleDecimal` (calibrated with Convert
    Entities). SolidWorks requires the line to live in that sketch and to be selected
    before CreateSectionViewAt5, otherwise it returns null.

    orientation: "horizontal" cuts left-right (shows the vertical section), "vertical"
    cuts top-bottom. offset_m shifts the line off the view centre (sheet metres).
    flip reverses the viewing direction (Options = 4). depth > 0 gives a partial section.

    P1-4 (0.8.21) — two additions, both aimed at "one wrong parameter must not require
    rebuilding the whole sheet":

    cut_plane_normal / cut_plane_origin
        MODEL coordinates (metres), given together. They override orientation + offset_m:
        the normal picks the cut direction (dominant part axis, must be >= 0.9 aligned;
        oblique planes have no single-line sketch representation and are refused) and the
        origin supplies the position. Mapping model -> view requires knowing which model
        axis each sheet axis shows; that is matched by size against the part box. Refusals
        are structured: `cut_plane_incomplete`, `cut_plane_oblique`,
        `cut_plane_not_in_view_plane`, `cut_plane_unmapped` (with `mapping_reason` such as
        `axes_ambiguous` for a cube, where guessing would silently cut the wrong plane).

    verify_cut (default True)
        After creation, compare the section's deflated outline against the source view's:
        a cut perpendicular to the sheet's horizontal axis cannot change the width, so
        `section_width / source_width` must be 1.0 ± 10% (height for vertical cuts).
        `edge_count == 0` means an empty view. Both failures return errors
        (`section_empty`, `section_suspicious`) WITH the view kept on the sheet, so you can
        inspect it instead of re-cutting blindly. Partial sections (depth>0) report
        `sanity.checked=false` because they shrink by design.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
        if not views:
            return error("no_views", "The drawing has no model view to section")
        target = None
        for candidate in views:
            if view_name is None or candidate.Name == view_name:
                target = candidate
                break
        if target is None:
            return error("view_not_found", "No view named %r" % view_name,
                         "Use drawing_audit to list view names.")

        doc.ActivateView(target.Name)
        pos = list(target.Position)
        scale = float(target.ScaleDecimal)
        outline = list(target.GetOutline)
        width = outline[2] - outline[0]
        height = outline[3] - outline[1]

        # P1-4: a cut plane in model coordinates has to be translated into the view sketch.
        cut_plane = None
        if cut_plane_normal is not None or cut_plane_origin is not None:
            if cut_plane_normal is None or cut_plane_origin is None:
                return error("cut_plane_incomplete",
                             "cut_plane_normal and cut_plane_origin must be passed together")
            try:
                normal = [float(v) for v in cut_plane_normal]
                origin = [float(v) for v in cut_plane_origin]
            except Exception:
                return error("bad_input", "cut_plane_normal/origin must be 3 numbers each")
            if len(normal) != 3 or len(origin) != 3:
                return error("bad_input", "cut_plane_normal/origin must be 3 numbers each")
            length = sum(v * v for v in normal) ** 0.5
            if length <= 1e-9:
                return error("bad_input", "cut_plane_normal has zero length")
            unit = [v / length for v in normal]

            axis_map, reason = _view_axis_map(target)
            # _view_axis_map reads the part through open_document, which switches the
            # active document; bring the drawing back before touching its sketch.
            doc = _open_drawing(drawing_path)
            doc.ActivateView(target.Name)
            if axis_map is None:
                return error(
                    "cut_plane_unmapped",
                    "Cannot map model coordinates onto view %r (%s)" % (target.Name, reason),
                    "Pass orientation + offset_m instead, or pick a view whose displayed "
                    "axes match the part box unambiguously.",
                    cut_plane_normal=normal, cut_plane_origin=origin,
                    mapping_reason=reason)
            axes = axis_map["axes"]
            centre_m = axis_map["centre_m"]
            dominant = max(range(3), key=lambda i: abs(unit[i]))
            if abs(unit[dominant]) < 0.9:
                return error(
                    "cut_plane_oblique",
                    "cut_plane_normal %r is not axis-aligned (dominant component %.3f)"
                    % (normal, abs(unit[dominant])),
                    "Only planes perpendicular to a part axis are supported — an oblique "
                    "plane has no single-line representation in the view sketch.",
                    cut_plane_normal=normal, cut_plane_origin=origin,
                    mapping=axis_map)
            if dominant == axes[0]:
                orientation = "vertical"
            elif dominant == axes[1]:
                orientation = "horizontal"
            else:
                return error(
                    "cut_plane_not_in_view_plane",
                    "cut_plane_normal is parallel to the viewing direction of %r"
                    % target.Name,
                    "A plane parallel to the sheet cannot be drawn as a cut line; pick a "
                    "normal along one of the two displayed axes.",
                    cut_plane_normal=normal, cut_plane_origin=origin,
                    mapping=axis_map)
            offset_m = (origin[dominant] - centre_m[dominant]) * scale
            cut_plane = {
                "normal_model": [round(v, 6) for v in normal],
                "origin_model": [round(v, 6) for v in origin],
                "dominant_axis": int(dominant),
                "derived_orientation": str(orientation),
                "derived_offset_m": round(float(offset_m), 6),
                "mapping": axis_map,
            }
        doc.ActivateView(target.Name)

        # GetOutline is in SHEET metres, but the line is drawn in the VIEW SKETCH, whose
        # origin is the view centre and whose units are MODEL space. Both length and offset
        # therefore have to go through /scale.
        #
        # The old comment here said the opposite ("dividing by scale produced a 1035 mm line
        # on a 420 mm sheet") — that measurement was taken while the line was still being
        # offset by `view.Position` into sheet coordinates, i.e. in a different coordinate
        # system. Once the line is drawn around the sketch origin, /scale is required;
        # without it the line lands far outside the geometry and SolidWorks silently returns
        # a section view that is really just a projection: `GetFaceHatchCount() == 0`,
        # a "cut" that never touched the part. Measured 2026-11-02 on 飞达动力轴:
        # FaceHatchCount 0 -> 2 and edges 13 -> 37 after this fix.
        #
        # `add_detail_view` in this same module already did this conversion correctly
        # (sketch = (sheet - view.Position) / parent_scale) — this function was the only
        # one that skipped it.
        precise_box = precise_outline(target) or outline
        view_w = precise_box[2] - precise_box[0]
        view_h = precise_box[3] - precise_box[1]
        # 0.62 of the LONG edge overshoots on any non-square view: a report's arrows stuck
        # out ~15 mm and landed on the technical-requirements block. Clamp to half the SHORT
        # edge as well, and let the caller shrink it further.
        half = min(max(view_w, view_h) * float(cut_line_ratio),
                   min(view_w, view_h) * 0.5) / scale
        offset = float(offset_m) / scale
        # A top view's sketch y axis runs at -Z, so a model-space offset applied to the
        # horizontal cut line lands mirrored. Measured on 灌装阀阀体: offset -0.012 produced
        # a 59x80 cross-section while +0.012 produced 14x50. Negate here so callers can
        # think in model coordinates for both orientations.
        if str(orientation).lower() != "vertical":
            offset = -offset

        sm = doc.SketchManager
        sm.InsertSketch(True)
        try:
            # Around the SKETCH ORIGIN — which is the view centre, not the sheet origin.
            # Adding `view.Position` here shifted the whole line off the geometry (measured
            # 76 mm across, 265 mm down on 飞达动力轴); SolidWorks accepted it and produced
            # a Type=2 view that had never cut anything.
            if str(orientation).lower() == "vertical":
                sm.CreateLine(offset, -half, 0.0,
                              offset, half, 0.0)
            else:
                sm.CreateLine(-half, offset, 0.0,
                              half, offset, 0.0)
        except Exception as exc:
            try:
                sm.InsertSketch(True)
            except Exception:
                pass
            return error("cut_line_failed", str(exc),
                         "Could not draw the cut line in the view sketch.")

        raw = sm.ActiveSketch.GetSketchSegments
        segments = list(raw) if isinstance(raw, (tuple, list)) else ([raw] if raw else [])
        segments = [s for s in segments if s is not None]
        selected = False
        for segment in segments:
            try:
                seg = win.Dispatch(segment)
                if seg.Select2(False, 0) and doc.SelectionManager.GetSelectedObjectCount2(-1):
                    selected = True
                    break
            except Exception:
                continue
        sm.InsertSketch(True)
        if not selected:
            return error("cut_line_not_selected",
                         "Drew the cut line but could not select it",
                         "SolidWorks needs the cut line selected before creating the section.")

        if position:
            place_x, place_y = float(position[0]), float(position[1])
        else:
            # Default to the free area at the sheet's right side. Placing the section
            # directly under the source view pushes it into the parts list once that
            # column starts at ~14% of the width.
            sheet_box = list(doc.GetFirstView.GetOutline)
            sheet_w = sheet_box[2] - sheet_box[0]
            sheet_h = sheet_box[3] - sheet_box[1]
            # A section stays x-aligned with its parent, so it must go above/below it.
            place_x = outline[0] + width / 2
            place_y = max(sheet_h * 0.14, outline[1] - max(height * 0.55, 0.06))

        options = 4 if flip else 0  # swCreateSectionView_ChangeDirection
        section = doc.CreateSectionViewAt5(
            place_x, place_y, 0.0, str(label), options, None, float(depth)
        )
        if section is None:
            return error(
                "section_failed",
                "CreateSectionViewAt5 returned null for %r" % target.Name,
                "The cut line must lie inside the view and cross its geometry; "
                "or an earlier failed attempt left the view in a bad state — reopen the drawing.",
                cut_plane=cut_plane,
            )
        try:
            section.ScaleDecimal = scale
        except Exception:
            pass
        # Hatching only renders when hidden lines are displayed. The view inherits the
        # template default (wireframe), and a report shipped a section view whose cut face
        # was completely blank — outline only, no hatching at all, `sanity.verdict: ok`.
        # Measured 2026-11-02: neither `IView.DisplayMode` nor `IView.ViewDisplayMode` is
        # writable through this binding ("<unknown>.DisplayMode"), and `GetDisplayMode2()`
        # already returns 2 (hidden lines) on a freshly created section. So the missing
        # hatching reported by 飞达动力轴 is NOT a display-mode problem — reverting the
        # attempt rather than shipping a no-op that looks like a fix.
        display_mode_set = None
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        # Deliberately no repack here: repacking moves the section back into the parts
        # list whenever the sheet is tight. Call drawing_audit to check the result and
        # pass `position` to place it precisely.
        layout = None
        _save_drawing(doc)
        try:
            so = list(section.GetOutline)
            box = {"width_m": round(so[2] - so[0], 4), "height_m": round(so[3] - so[1], 4)}
        except Exception:
            box = None
        precise = precise_outline(section)
        edge_count = _view_edge_count(section)
        # How many hatch faces the cut actually produced. This is the only reliable
        # "is there hatching?" signal found: `GetFaceHatchCount == 0` means the cut face is
        # blank, which is what 飞达动力轴 reported while `sanity.verdict` still said ok.
        # Verified 2026-11-02 that none of these change it: `SetAutoHatch(True)` (parts are
        # explicitly out of scope per the API docs), `ScaleHatchPattern = True` (it only
        # scales an EXISTING pattern), `SectionDepth` (0.00635 -> 0.107 -> 0.2, still 0),
        # `SetDisplayOnlySurfaceCut(True)`. So the count is reported, not repaired.
        hatch_count = None
        try:
            hatch_count = int(section.GetFaceHatchCount)
        except Exception:
            hatch_count = None

        sanity = (_section_sanity(target, section, orientation, depth)
                  if verify_cut else
                  {"checked": False, "reason": "verify_cut=False"})
        # P1-5 of a 2026-11-03 report: a section view is held in horizontal projection
        # alignment with its parent, so the y of a requested `position` is DISCARDED — and
        # nothing in the receipt said so. The report asked for [0.300, 0.130] and got an
        # outline centred at y=0.205, with `layout: null` and no `aligned_to_parent` flag;
        # the section landed on top of the end view and needed a second set_view to fix.
        #
        # The alignment is correct drawing practice, so it is kept — but the request and
        # the result must both be visible, and a dropped coordinate must be called out.
        position_requested = None
        if position and len(position) >= 2:
            position_requested = [round(float(position[0]), 4), round(float(position[1]), 4)]
        position_actual = None
        try:
            position_actual = [round(float(v), 4) for v in section.Position]
        except Exception:
            position_actual = None
        alignment_kept = None
        dropped_axis = None
        if position_requested is not None and position_actual is not None:
            dx = abs(position_actual[0] - position_requested[0])
            dy = abs(position_actual[1] - position_requested[1])
            alignment_kept = bool(dx > 1e-3 or dy > 1e-3)
            if dy > 1e-3:
                dropped_axis = "y"
            elif dx > 1e-3:
                dropped_axis = "x"

        diag = {
            "drawing": doc.GetTitle, "section": section.Name, "source_view": target.Name,
            "label": str(label), "orientation": str(orientation), "flip": bool(flip),
            "offset_m": round(float(offset_m), 6), "outline": box,
            "precise_outline_m": ([round(v, 5) for v in precise] if precise else None),
            "edge_count": edge_count, "sanity": sanity, "cut_plane": cut_plane,
            "layout": layout, "hatch_count": hatch_count,
            "position_requested_m": position_requested,
            "position_actual_m": position_actual,
            "alignment_kept": alignment_kept,
            "position_axis_ignored": dropped_axis,
            "warning": ("请求的位置 %r 的 %s 被丢弃：剖面视图与父视图 %r 保持投影对齐，"
                        "实际落在 %r。要对齐就用这个位置；不要就再调 set_view(break_alignment=True)。"
                        % (position_requested, dropped_axis, str(target.Name), position_actual)
                        if dropped_axis else None),
        }
        if edge_count == 0:
            return error(
                "section_empty",
                "The section view has 0 visible edges — the cut line missed the geometry.",
                "Move `offset_m` inside the view's outline, or pass cut_plane_origin at "
                "real geometry. The empty view is left on the sheet for inspection.",
                **diag)
        if hatch_count == 0:
            # The cut face exists but carries no hatching: the drawing is wrong even though
            # every other check passes. 飞达动力轴 reported exactly this while `verdict: ok`.
            # Root cause not found — `SetAutoHatch` (parts out of scope), `ScaleHatchPattern`
            # (scales an existing pattern), `SectionDepth`, `SetDisplayOnlySurfaceCut` all
            # leave the count at 0. Reported as an error so it cannot slip into a delivery.
            return error(
                "hatch_missing",
                "剖面视图已创建，但剖切面没有任何剖面线（GetFaceHatchCount = 0）。",
                "GB 要求剖切面必须画剖面线；这种视图在生产上不能用。"
                "请在 SolidWorks 界面手工检查该视图的区域剖面线属性（是否选了 Hatch / "
                "Material crosshatch），或换一个能正常显示剖面线的模板/零件做对照。",
                **diag)
        if sanity.get("checked") and sanity.get("verdict") == "suspicious":
            return error(
                "section_suspicious",
                "The section kept %s mm of %s mm (ratio %.3f) — the cut plane probably "
                "missed the intended cross-section."
                % (sanity.get("section_kept_mm"), sanity.get("source_kept_mm"),
                   sanity.get("ratio") or 0.0),
                "Check offset_m sign and magnitude against the source view, or pass "
                "cut_plane_origin in model coordinates. The view is kept for inspection.",
                **diag)
        return drawing_ok(**diag)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("section_failed", str(exc))


def _view_edge_count(view):
    """Count a view's visible edges without re-opening the document (P2-9).

    A DETAIL view whose boundary circle missed the parent geometry reports NO visible
    components, so this returns 0 — that is a genuinely empty view, not an API quirk.
    Measured 2026-10-31 on door.SLDPRT: boundary inside the parent outline -> 1 edge,
    boundary outside -> 0 edges. Used to reject empty detail views instead of returning
    ok:true for a view that shows nothing.
    """
    try:
        comps = list(view.GetVisibleComponents or ())
    except Exception:
        comps = []
    total = 0
    for comp in comps:
        try:
            total += len(list(view.GetVisibleEntities2(comp, 1) or ()))
        except Exception:
            continue
    return total


def _apply_detail_scale(doc, detail, scale):
    """Set a detail view's scale, clearing the parent-scale lock first. Returns readback."""
    applied = None
    try:
        doc.ActivateView(detail.Name)
    except Exception:
        pass
    for _ in range(4):
        try:
            detail.UseParentScale = False
        except Exception:
            pass
        try:
            detail.ScaleDecimal = float(scale)
        except Exception:
            pass
        try:
            applied = float(detail.ScaleDecimal)
        except Exception:
            applied = None
        if applied is not None and abs(applied - float(scale)) < 0.01:
            return applied
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
    return applied


def add_detail_view(drawing_path, view_name=None, center=None, radius_m=0.02,
                    position=None, scale=2.0, label="A", style=0, shape=0,
                    flip=False, show_label=True, circle_segments=32):
    """Add a DETAIL (enlarged) view around a circular area of a model view.

    CreateDetailViewAt4 takes TWELVE arguments — the last one is the circle's segment
    count. Passing 0 there raises "non-optional parameter" and no view is created, which
    is what made this look unsupported. 32 works.

    center/radius_m are SHEET metres — point the boundary at real detail (a hole, a
    corner, a bend), otherwise the enlarged view comes out empty. The circle is drawn in
    the source view's sketch
    (coordinates = (paper - view.Position) / view.ScaleDecimal) and selected before the
    call — otherwise SolidWorks has no boundary to enlarge.

    P2-9 (0.8.20): `scale` is the REQUESTED detail scale and is no longer overwritten by
    the parent's scale (that bug made a 2:1 request silently come out 1:2). After creation
    the new view's visible edges are counted: 0 means the boundary missed the geometry and
    the call returns `detail_empty`. If SolidWorks keeps the parent scale instead of the
    requested one, the call returns `detail_scale_locked` rather than ok:true with a quiet
    `scale_locked` flag.
    """
    try:
        import win32com.client as win

        # P2-9: `scale` is the REQUESTED detail scale. Never overwrite it with the
        # parent view's scale — that old bug made a 2:1 request silently come out 1:2.
        requested_scale = float(scale)
        doc = _open_drawing(drawing_path)
        views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
        if not views:
            return error("no_views", "The drawing has no model view to enlarge")
        target = None
        for candidate in views:
            if view_name is None or candidate.Name == view_name:
                target = candidate
                break
        if target is None:
            return error("view_not_found", "No view named %r" % view_name,
                         "Use drawing_audit to list view names.")

        doc.ActivateView(target.Name)
        pos = list(target.Position)
        parent_scale = float(target.ScaleDecimal)
        outline = list(target.GetOutline)
        if center:
            sheet_x, sheet_y = float(center[0]), float(center[1])
        else:
            sheet_x = (outline[0] + outline[2]) / 2
            sheet_y = (outline[1] + outline[3]) / 2
        # The boundary circle is drawn in the PARENT view's sketch, so convert with the
        # parent's scale, not the detail's requested one.
        sketch_x = (sheet_x - pos[0]) / parent_scale
        sketch_y = (sheet_y - pos[1]) / parent_scale
        sketch_r = float(radius_m) / parent_scale

        # P1-2: decide where the detail view will live BEFORE drawing the boundary, so a
        # crowded sheet returns `no_space` with the drawing untouched instead of producing
        # a view that hangs off the edge or stacks on its neighbour.
        if position:
            place_x, place_y = float(position[0]), float(position[1])
        else:
            ratio = requested_scale / max(parent_scale, 1e-6)
            est_half_w = float(radius_m) * ratio * 1.3 + 0.008
            est_half_h = float(radius_m) * ratio * 1.05 + 0.014
            spot = _free_spot_for_view(
                doc, est_half_w, est_half_h,
                prefer_x=min(max(outline[2] + est_half_w + 0.02, est_half_w + 0.01),
                             0.42 - est_half_w - 0.01),
                prefer_y=max(est_half_h + 0.01, (outline[1] + outline[3]) / 2),
            )
            if spot is None:
                return error(
                    "no_space",
                    "图纸放不下约 %.2f×%.2f m 的局部视图（%g:1）"
                    % (est_half_w * 2, est_half_h * 2, requested_scale),
                    "已有视图：%s。可减小 scale / radius_m、显式传 position，或换更大的图幅。"
                    % _view_outline_hint(doc),
                    estimated_size_m=[round(est_half_w * 2, 4), round(est_half_h * 2, 4)],
                    requested_scale=requested_scale,
                )
            place_x, place_y = spot

        sm = doc.SketchManager
        sm.InsertSketch(True)
        try:
            sm.CreateCircleByRadius(sketch_x, sketch_y, 0.0, sketch_r)
        except Exception as exc:
            try:
                sm.InsertSketch(True)
            except Exception:
                pass
            return error("boundary_failed", str(exc),
                         "Could not draw the detail boundary circle in the view sketch.")

        raw = sm.ActiveSketch.GetSketchSegments
        segments = list(raw) if isinstance(raw, (tuple, list)) else ([raw] if raw else [])
        segments = [s for s in segments if s is not None]
        selected = False
        for segment in segments:
            try:
                seg = win.Dispatch(segment)
                if seg.Select2(False, 0) and doc.SelectionManager.GetSelectedObjectCount2(-1):
                    selected = True
                    break
            except Exception:
                continue
        sm.InsertSketch(True)
        if not selected:
            return error("boundary_not_selected",
                         "Drew the detail circle but could not select it",
                         "SolidWorks needs the circle selected as the detail boundary.")

        detail = doc.CreateDetailViewAt4(
            place_x, place_y, 0.0, int(style), float(parent_scale), float(parent_scale),
            str(label), bool(show_label), bool(flip), False, int(shape),
            int(circle_segments),
        )
        if detail is None:
            return error(
                "detail_failed",
                "CreateDetailViewAt4 returned null for %r" % target.Name,
                "The boundary circle must lie inside the view geometry; reopen the "
                "drawing if earlier attempts left it in a bad state.",
            )
        # A detail view inherits the parent's scale (UseParentScale = True by default) and
        # silently ignores ScaleDecimal until that flag is cleared. Setting both right
        # after creation can be swallowed, so retry against the REQUESTED scale until the
        # read-back matches. Measured: _apply_detail_scale(2.0) does take effect and the
        # view is renamed "局部视图 A (2 : 1)".
        applied = _apply_detail_scale(doc, detail, requested_scale)
        if applied is None or abs(applied - requested_scale) > 0.01:
            # A freshly created detail view refuses the scale until it is committed once.
            try:
                doc.SaveSilent
            except Exception:
                pass
            applied = _apply_detail_scale(doc, detail, requested_scale)
            try:
                doc.SaveSilent
            except Exception:
                pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        _save_drawing(doc)
        try:
            d_o = list(detail.GetOutline)
            box = {"width_m": round(d_o[2] - d_o[0], 4), "height_m": round(d_o[3] - d_o[1], 4)}
        except Exception:
            box = None
        # P2-13: report the deflated box too — a detail view is smaller than the source, so
        # the fixed 11.9 mm inflation is a much larger relative error here (a 30 mm detail
        # reads 41.9 mm, i.e. 40% too wide).
        precise = precise_outline(detail)
        final_scale = applied if applied is not None else float(detail.ScaleDecimal)
        locked = abs(float(final_scale) - requested_scale) > 0.01

        # P1-2: the view exists, so its real box is known. Move it somewhere that neither
        # overlaps a neighbour nor leaves the sheet; if the sheet is full, remove the view
        # we just made and fail loudly — a view hanging off the paper is worse than a clear
        # `no_space`, because drawing_audit only catches it later.
        placement = {"requested": [round(place_x, 4), round(place_y, 4)], "x_locked": True}
        try:
            if precise and len(precise) >= 4:
                placement["measured"] = [round(v, 4) for v in precise]
                inside = (precise[0] >= -0.001 and precise[1] >= -0.001
                          and precise[2] <= 0.421 and precise[3] <= 0.298)
                placement["inside_sheet"] = bool(inside)
        except Exception:
            pass

        # P2-9: a boundary circle that missed the geometry produces an EMPTY detail view.
        # `GetVisibleComponents` is None in that case, so _view_edge_count is 0 (measured
        # on door.SLDPRT: boundary inside the parent outline -> 1 edge, outside -> 0).
        edge_count = _view_edge_count(detail)
        diag = {
            "detail": detail.Name,
            "source_view": target.Name,
            "boundary_center_m": [round(sheet_x, 4), round(sheet_y, 4)],
            "boundary_radius_m": float(radius_m),
            "source_outline_m": [round(v, 4) for v in outline],
            "edge_count": edge_count,
            "scale": final_scale,
            "requested_scale": requested_scale,
            "parent_scale": parent_scale,
            "outline": box,
            "precise_outline_m": ([round(v, 5) for v in precise] if precise else None),
            "placement": placement,
        }
        # P1-3 of a 2026-11-03 report claimed edge_count was "always 2" regardless of
        # framing. Measured on 旋转轴 (right view, centre [0.075, 0.265], 4:1):
        #     r=0.004 -> 0    r=0.006 -> 1    r=0.008 -> 2    r=0.012 -> 2    r=0.020 -> 3
        # so it does vary — but it stays small because the boundary only encloses a couple
        # of contour lines on this part. A report expecting 6-8 was looking at a part with
        # keyways and threads; this shaft has two outline lines in that window.
        #
        # What IS worth flagging is a view that rendered but shows almost nothing: 1-3
        # edges means the frame probably clamped too tight to read as a detail. Kept as a
        # distinct verdict so "too thin to use" does not look like "fine".
        # Reported as a WARNING, not an error: a plain shaft really does have only two
        # outline lines in any window, and enlarging those can be exactly what was wanted.
        # Failing the call would make the tool unusable on precisely the parts it suits.
        # `detail_empty` (0 edges) stays an error — that view shows nothing at all.
        thin_warning = None
        if 0 < edge_count < 4:
            thin_warning = (
                "这个局部视图只有 %d 条可见轮廓线，放大的内容很少。"
                "视窗中心附近本来就只有这么多线；要放大键槽/轴肩/螺纹这类特征，"
                "请换 center 或加大 radius_m。" % edge_count)
        if thin_warning:
            diag["detail_thin"] = True
            diag["warning"] = thin_warning
        if edge_count == 0:
            return error(
                "detail_empty",
                "The detail view was created but has 0 visible edges — its boundary "
                "circle fell outside the source view's geometry.",
                "Point `center` at real geometry (a hole, corner or bend) inside the "
                "source view's outline; use drawing_audit / list_drawing_edges to find it.",
                **diag)
        if locked:
            return error(
                "detail_scale_locked",
                "The detail view kept scale %.4g, not the requested %.4g — SolidWorks "
                "locked it to the parent view's scale."
                % (float(final_scale), requested_scale),
                "Save and reopen the drawing, then set the scale in the UI; or pass a "
                "scale equal to the parent's to create without a scale conflict.",
                scale_locked=True,
                scale_note=("局部视图默认继承父视图比例（UseParentScale）；本次请求的 "
                            "%.4g 未生效，实际 %.4g。" % (requested_scale, float(final_scale))),
                **diag)
        # diag already carries every measured field (detail, source_view, boundary_*,
        # edge_count, scale, requested_scale, parent_scale, outline, precise_outline_m),
        # so success and failure report exactly the same numbers.
        return drawing_ok(drawing=doc.GetTitle, label=str(label),
                          scale_locked=False, scale_note=None, **diag)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("detail_failed", str(exc))


TOLERANCE_TYPES = {
    "none": 0, "basic": 1, "bilateral": 2, "limit": 3, "symmetric": 4,
    "min": 5, "max": 6, "fit": 7, "fit_with_tolerance": 8,
}


def _find_dimension(doc, value_mm, view_name=None):
    """Dimension on the sheet whose value matches `value_mm` (whole millimetres)."""
    import win32com.client as win

    target = round(float(value_mm))
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            continue
        if view_name and view.Name != view_name:
            continue
        try:
            raw = view.GetAnnotations
        except Exception:
            continue
        items = list(raw) if isinstance(raw, (tuple, list)) else ([raw] if raw else [])
        for item in items:
            if item is None:
                continue
            try:
                ann = win.Dispatch(item)
                if ann.GetType != 4:
                    continue
                dim = ann.GetSpecificAnnotation.GetDimension2(0)
                if dim is None:
                    continue
                if round(float(dim.SystemValue) * 1000) == target:
                    return view, dim
            except Exception:
                continue
    return None, None


def add_dimension_tolerance(drawing_path, value_mm, tolerance_type="bilateral",
                           upper=0.1, lower=0.1, view_name=None, apply=True):
    """Apply a native tolerance to an existing drawing dimension.

    The dimension is located by its VALUE in millimetres (drafting tolerances are
    specified per nominal size), so no fragile coordinate picking is involved.

    tolerance_type: bilateral (default, ±upper/lower) | symmetric | limit | basic |
    min | max | fit | fit_with_tolerance | none. upper/lower are in millimetres.
    """
    try:
        doc = _open_drawing(drawing_path)
        code = TOLERANCE_TYPES.get(str(tolerance_type).lower())
        if code is None:
            return error("bad_tolerance_type",
                         "Unknown tolerance_type %r" % tolerance_type,
                         "Use one of: %s" % ", ".join(sorted(TOLERANCE_TYPES)))
        view, dim = _find_dimension(doc, value_mm, view_name)
        if dim is None:
            return error("dimension_not_found",
                         "No dimension equal to %s mm%s"
                         % (value_mm, " in view %r" % view_name if view_name else ""),
                         "Use drawing_audit to see the dimensions present on the sheet.")
        planned = {
            "value_mm": round(float(dim.SystemValue) * 1000, 3),
            "tolerance_type": str(tolerance_type).lower(),
            "upper_mm": float(upper), "lower_mm": float(lower),
            "view": view.Name if view is not None else None,
        }
        if not apply:
            return drawing_ok(dry_run=True, planned=planned,
                              message="Nothing changed. apply=true writes the tolerance.")

        try:
            dim.SetToleranceType(code)
        except Exception as exc:
            return error("tolerance_unsupported", str(exc),
                         "This dimension may not accept a tolerance (reference dimensions "
                         "and driven dimensions reject them).")
        if code != 0:
            # `SetToleranceValues` takes METRES. Passing millimetres made 0.1 mm render as
            # "±100" — a 1000x error that still reported applied:true and read back a valid
            # tolerance type (measured 2026-11-01: 50 symmetric ±0.1 -> PDF shows "50 ±100").
            try:
                dim.SetToleranceValues(float(upper) / 1000.0, float(lower) / 1000.0)
            except Exception:
                # Symmetric takes a single value on some builds.
                try:
                    dim.SetToleranceValues(float(upper) / 1000.0, float(upper) / 1000.0)
                except Exception:
                    pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        try:
            doc.SaveSilent
        except Exception:
            pass
        applied = None
        try:
            applied = int(dim.GetToleranceType)
        except Exception:
            pass
        # The tolerance VALUE lives in a text slot, not in the numeric readback: a 1000x
        # unit error looked perfect until the PDF was rendered. Report what it prints.
        rendered_text = None
        try:
            for _slot in (1, 0, 2):
                _candidate = dim.GetText(_slot)
                if _candidate:
                    rendered_text = str(_candidate)
                    break
        except Exception:
            pass
        # P2-9 of a 2026-11-03 report: h7's -0.015 came out as "8.00 / 8.00" — both limits
        # identical — while the call reported applied:true. Two separate causes:
        #
        # 1. UNIT AMBIGUITY. `SetToleranceValues` takes metres, so this function divides by
        #    1000; but the report passed lower=-1.5e-05 believing the parameter was already
        #    in metres. Treated as mm that is -0.000015 mm, which rounds to zero on the
        #    sheet. A manufacturable tolerance below 0.001 mm does not exist, so a non-zero
        #    magnitude that small is almost certainly a metre value passed to a mm slot.
        # 2. NO READBACK CHECK. `rendered_text` was returned but never judged, so a
        #    tolerance that produced nothing still said applied:true.
        unit_suspect = None
        for label, raw_value in (("upper", upper), ("lower", lower)):
            try:
                magnitude = abs(float(raw_value))
            except Exception:
                continue
            if 0 < magnitude < 1e-3:
                unit_suspect = {
                    "parameter": label,
                    "value": float(raw_value),
                    "as_mm": float(raw_value),
                    "if_it_was_metres": round(float(raw_value) * 1000, 6),
                }
                break

        # A limit tolerance whose two limits print the same is not a tolerance: the sheet
        # reads "8.00 / 8.00" and the shop loses the entire fitting allowance.
        # Compare the ARGUMENTS, not the rendered text: on this build GetText returns
        # nothing even for a correct tolerance, so gating this on rendered_text meant the
        # degenerate 0/0 case sailed through unimpeded.
        limits_identical = None
        if str(tolerance_type).lower() == "limit":
            try:
                limits_identical = abs(float(upper) - float(lower)) < 1e-9
            except Exception:
                limits_identical = None

        # Two severities, because the two cases are not equally certain:
        #
        #   identical limits -> ERROR. A limit tolerance whose halves print the same has
        #   definitely lost the allowance; that is arithmetic, not a readback quirk.
        #
        #   no readable text -> WARNING only. `GetText(slot)` returned nothing on this build
        #   even for a correctly applied -0.015 tolerance, so "I could not read it" is NOT
        #   evidence that nothing was written. Reporting it as an error would cry wolf on
        #   every tolerance on this part.
        if limits_identical:
            return error(
                "tolerance_not_rendered",
                "both limits print identically (%r) — the allowance is lost" % rendered_text,
                ("upper/lower are MILLIMETRES. If you were passing metres, multiply by "
                 "1000 (h7 lower is -0.015 mm, not -0.015 m)."
                 if unit_suspect else
                 "A limit tolerance needs upper != lower; check the values you passed."),
                planned=planned, tolerance_type_readback=applied,
                rendered_text=rendered_text,
                tolerance_not_rendered=True,
                unit_suspect=unit_suspect)

        warnings = []
        if rendered_text is None:
            warnings.append(
                "读不到这个尺寸的公差文本（GetText 各槽均为空），"
                "所以**无法确认公差是否已显示**。请导出 PDF 目视核对，不要只看 applied:true。")
        if unit_suspect:
            warnings.append(
                "参数 %s=%r 看起来是「米」当成了「毫米」：按毫米算等于 %.6g mm，图面上会消失。"
                "upper/lower 的单位是毫米。"
                % (unit_suspect["parameter"], unit_suspect["value"], unit_suspect["as_mm"]))
        return drawing_ok(applied=True, saved=True, planned=planned,
                          tolerance_type_readback=applied,
                          rendered_text=rendered_text,
                          rendered_verified=bool(rendered_text),
                          unit_suspect=unit_suspect,
                          warning=(" ".join(warnings) if warnings else None))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("tolerance_failed", str(exc))


ANNOTATION_KINDS = {
    2: "centerline", 4: "dimension", 5: "geometric_tolerance", 6: "note",
    7: "surface_finish", 8: "datum_tag", 9: "datum_target", 11: "balloon",
    12: "weld_symbol", 13: "center_mark", 14: "hole_callout",
}


TEMPLATE_TEXT = {
    "标记", "处数", "分区", "签名", "日期", "年 月 日", "阶段标记", "阶 段 标 记",
    "更改文件号", "设计", "审核", "校核", "批准", "工艺", "标准化", "重量", "比例",
    "共 张", "第 张", "图样名称", "图样代号", "材料", "数量", "更改单号",
}

# SolidWorks display format codes that reach us through IDisplayDimension.GetText(slot).
# `GetText()` with NO slot returns '' for a dimension; the value and its codes live in
# slot 1 (measured 2026-10-31: a Ø20 dimension answers GetText(0)='' and
# GetText(1)='<MOD-DIAM>'). Downstream code that only reads GetText() therefore concludes
# "this drawing has no diameter dimension" on a sheet full of Ø28/Ø23/… — that is exactly
# what delivery_check reported as P1-1.
DISPLAY_CODE_MAP = {
    "<MOD-DIAM>": "Ø",
    "<MOD-RAD>": "R",
    "<MOD-DEG>": "°",
    "<MOD-PM>": "±",
    "<MOD-SQ>": "□",
    "<MOD-SPH>": "S",
}


def _resolve_display_text(raw_text, value_mm=None):
    """Turn display format codes into what the sheet actually prints (P3-3 / P1-1).

    Returns the human-readable string. Callers keep the original as `raw_text` so a
    mismatch between code and rendering stays visible instead of being papered over.
    `<DIM>` is the value placeholder; when `value_mm` is known it is filled in, which is
    how "Ø20" is reconstructed from "<MOD-DIAM>" + SystemValue.
    """
    text = str(raw_text or "")
    if not text:
        return None
    upper = text.upper()
    for code, symbol in DISPLAY_CODE_MAP.items():
        if code in upper:
            start = 0
            while True:
                index = upper.find(code, start)
                if index < 0:
                    break
                text = text[:index] + symbol + text[index + len(code):]
                upper = text.upper()
                start = index + len(symbol)
    if "<DIM>" in upper and value_mm is not None:
        number = ("%.3f" % float(value_mm)).rstrip("0").rstrip(".")
        start = 0
        while True:
            upper = text.upper()
            index = upper.find("<DIM>")
            if index < 0:
                break
            text = text[:index] + number + text[index + 5:]
    return text or None


#: Scoped cache for list_drawing_annotations. Measured 2026-11-03: one produce_drawing
#: calls this THREE times (auto_dimension's same-value pass, the pipeline summary, and
#: delivery_check), 13.4 s each - 38% of delivery_check's 34.9 s. The last two run after
#: the sheet is final, so their answer cannot change between them.
#:
#: Opt-in and scoped rather than always-on: this is a QUERY tool, and a user asking "what
#: is on the sheet" expects the current state, not a cached one. Only the pipeline, which
#: knows when the sheet stops changing, opens the scope.
_ANNOT_CACHE = None          # None = disabled; dict = {(path, mtime_ns): payload}
_ANNOT_CACHE_HITS = 0


def annotations_cache_enable():
    """Turn the scoped cache on. Pair with annotations_cache_disable() in a finally.

    ASSETMATE_ANNOT_CACHE=0 disables it, so an A/B can isolate its contribution the same
    way ASSETMATE_LEDGER_CACHE does for the ledger.
    """
    global _ANNOT_CACHE, _ANNOT_CACHE_HITS
    if os.environ.get("ASSETMATE_ANNOT_CACHE", "1") in ("0", "false", "off"):
        _ANNOT_CACHE = None
        return
    _ANNOT_CACHE = {}
    _ANNOT_CACHE_HITS = 0


def annotations_cache_disable():
    """Turn it off and drop what was cached."""
    global _ANNOT_CACHE
    _ANNOT_CACHE = None


@contextlib.contextmanager
def annotations_cache_scope():
    """Serve repeated list_drawing_annotations calls from one read for the block."""
    global _ANNOT_CACHE, _ANNOT_CACHE_HITS
    if _ANNOT_CACHE is not None:      # already inside; do not nest or clear early
        yield
        return
    _ANNOT_CACHE = {}
    _ANNOT_CACHE_HITS = 0
    try:
        yield
    finally:
        _ANNOT_CACHE = None


@perf_hooks.timed("annotations")
def list_drawing_annotations(drawing_path, view_name=None):
    """Inventory every annotation on the sheet, classified by kind.

    Process §6.1 requires checking that datums, geometric tolerances and surface
    finishes exist and say what they should. SolidWorks can create these only through
    early binding (a C# add-in), but READING them works here — that is enough for a
    self-check to state "this drawing has no geometric tolerance at all".

    Geometric tolerance frames are read through IGtol.GetFrameValues.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        rows, counts = [], {}
        seen = set()
        _cache_key = None
        if _ANNOT_CACHE is not None:
            try:
                _cache_key = (os.path.normcase(os.path.abspath(drawing_path)),
                              os.stat(drawing_path).st_mtime_ns)
                _hit = _ANNOT_CACHE.get(_cache_key)
                if _hit is not None:
                    global _ANNOT_CACHE_HITS
                    _ANNOT_CACHE_HITS += 1
                    _out = dict(_hit)
                    _out["cached"] = True
                    return _out
            except Exception:
                _cache_key = None
        try:
            outline = list(doc.GetFirstView.GetOutline or [])
            sheet_w = (outline[2] - outline[0]) if len(outline) >= 4 else 0.42
            sheet_h = (outline[3] - outline[1]) if len(outline) >= 4 else 0.297
        except Exception:
            sheet_w, sheet_h = 0.42, 0.297

        def scan(view, scope):
            try:
                raw = view.GetAnnotations
            except Exception:
                return
            items = list(raw) if isinstance(raw, (tuple, list)) else ([raw] if raw else [])
            for item in items:
                if item is None:
                    continue
                try:
                    ann = win.Dispatch(item)
                    kind_code = int(ann.GetType)
                except Exception:
                    continue
                kind = ANNOTATION_KINDS.get(kind_code, "other_%d" % kind_code)
                # Reset per iteration: leaving the previous value in place made a callout
                # inherit the PREVIOUS annotation's text when its own read failed.
                callout_text = ""
                # A hole callout is a DisplayDimension, not its own annotation type — the
                # only way to tell it apart from an ordinary dimension is IsHoleCallout.
                if kind == "dimension":
                    try:
                        specific = ann.GetSpecificAnnotation
                        if specific is not None and bool(specific.IsHoleCallout):
                            kind = "hole_callout"
                    except Exception:
                        pass
                name = None
                try:
                    name = str(ann.GetName)
                except Exception:
                    name = None
                # IsHoleCallout is ALSO true for a radius dimension on an outer arc, and
                # `GetText` does not exist on those objects — so a text-based test silently
                # passes on unreadable text and lets "R" stand in for hole information.
                # The name prefix is reliable and costs nothing: RD* = radius dimension.
                if kind == "hole_callout" and name:
                    upper_name = name.upper()
                    callout_text = ""
                    # GetText lives on IDisplayDimension, NOT on IAnnotation — calling it on
                    # the annotation raises and the swallowed exception made every callout
                    # look like it had no text, so all of them fell through to the RD-prefix
                    # rule and even real Ø callouts were filed as radius dimensions.
                    try:
                        specific = ann.GetSpecificAnnotation
                    except Exception:
                        specific = None
                    for index in (0, 1, 2):
                        try:
                            callout_text = str(specific.GetText(index) or "")
                            if callout_text:
                                break
                        except Exception:
                            continue
                    upper_text = callout_text.upper()
                    looks_like_hole = (
                        "Ø" in callout_text or "MOD-DIAM" in upper_text
                        or upper_text.startswith("M") or " X " in upper_text
                    )
                    # Order matters: readable text that names a diameter wins over the
                    # name prefix, because a genuine hole callout can also be named RD*.
                    # Only when the text is unreadable do we fall back to the prefix.
                    if looks_like_hole:
                        pass  # keep hole_callout
                    elif upper_name.startswith("RD"):
                        kind = "radius_callout"
                    elif not callout_text:
                        # No text and no RD prefix: do not claim it is a hole callout.
                        # "Unreadable" must not read as "verified".
                        kind = "hole_callout_unverified"
                    else:
                        kind = "radius_callout"
                position = None
                try:
                    position = [round(v, 4) for v in ann.GetPosition]
                except Exception:
                    position = None
                # Dedupe on scope+name+position: the same name (D1) can be hit from several
                # scans, but a genuine duplicate has the same position too.
                key = (scope, name, tuple(position) if position else None)
                if name and key in seen:
                    continue
                if name:
                    seen.add(key)
                row = {"kind": kind, "name": name, "scope": scope}
                # `text` must be written AFTER row exists. It used to be written before
                # this line, which meant: first scanned annotation -> UnboundLocalError
                # (the tool returned annotations_failed); any later one -> the text was
                # appended to the PREVIOUS row, silently shifting data between lines.
                if kind in ("hole_callout", "radius_callout", "hole_callout_unverified"):
                    # GetText can return raw format codes like "<MOD-DIAM>" instead of "Ø".
                    # The classification above already treats MOD-DIAM as a diameter, but
                    # handing the code straight to the caller is unreadable — flag it and
                    # convert the one code we know for sure.
                    row["text"] = callout_text or None
                    if callout_text and "<MOD-" in callout_text.upper():
                        row["text_is_format_code"] = True
                        row["text"] = (callout_text.upper()
                                       .replace("<MOD-DIAM>", "Ø")
                                       .replace("<MOD-DEG>", "°"))
                if position:
                    row["position_m"] = position
                if kind == "geometric_tolerance":
                    try:
                        gtol = ann.GetSpecificAnnotation
                        row["frames"] = int(gtol.GetFrameCount)
                        row["frame1"] = [str(v) for v in gtol.GetFrameValues(1)]
                        row["text"] = str(gtol.GetText(1))
                    except Exception:
                        pass
                if kind in ("dimension", "hole_callout", "radius_callout",
                            "hole_callout_unverified"):
                    dimension_value = None
                    specific = None
                    try:
                        specific = ann.GetSpecificAnnotation
                        dim = specific.GetDimension2(0)
                        if dim is not None:
                            dimension_value = round(float(dim.SystemValue) * 1000, 3)
                            row["value_mm"] = dimension_value
                            # A callout whose text is just "Ø" / "R" (or None) tells the
                            # reader nothing, and the value is already on this row. Reports
                            # hit this four times: radius_callout text=None, hole_callout
                            # text="Ø" with no number.
                            if kind in ("hole_callout", "radius_callout",
                                        "hole_callout_unverified"):
                                # `SystemValue` is NOT consistently radius or diameter for
                                # callouts: measured on one sheet, a printed Ø8 answered 4.0
                                # (radius) while Ø15 answered 15.0 (already diameter). So
                                # trust the printed text when it carries a number, and only
                                # fall back to SystemValue — flagged — when it does not.
                                import re as _re

                                _bare = str(row.get("text") or "").strip()
                                _printed = _re.search(r"([0-9]+(?:\.[0-9]+)?)", _bare)
                                if _printed:
                                    row["diameter_mm"] = float(_printed.group(1))
                                    row["diameter_source"] = "text"
                                elif _bare in ("", "Ø", "⌀", "R"):
                                    row["diameter_mm"] = round(float(dimension_value), 3)
                                    row["diameter_source"] = "value_unverified"
                                    _prefix = ("R" if (_bare == "R"
                                                       or kind == "radius_callout")
                                               else "Ø")
                                    row["text"] = "%s%g" % (
                                        _prefix, round(float(dimension_value), 3))
                                    row["text_was_bare_symbol"] = True
                            try:
                                row["tolerance_type"] = int(dim.GetToleranceType)
                            except Exception:
                                pass
                    except Exception:
                        specific = None
                    if specific is not None:
                        # P1-1 / P3-3: `GetText()` with NO slot returns '' for a dimension —
                        # the value and its display codes live in slot 1 (measured
                        # 2026-10-31: a Ø20 dimension answers GetText(0)='' and
                        # GetText(1)='<MOD-DIAM>'). Reading no slot meant every plain
                        # dimension looked text-less, so delivery_check counted 0 Ø
                        # dimensions on a sheet that visibly printed Ø28/Ø23/Ø12/Ø18/Ø13.
                        try:
                            row["dimension_type"] = int(specific.GetType)
                        except Exception:
                            pass
                        if kind == "dimension":
                            raw_value_text = ""
                            for index in (1, 0, 2):
                                try:
                                    probe = str(specific.GetText(index) or "")
                                except Exception:
                                    probe = ""
                                if probe:
                                    raw_value_text = probe
                                    break
                            if raw_value_text:
                                row["raw_text"] = raw_value_text
                                resolved = _resolve_display_text(
                                    raw_value_text, dimension_value)
                                if resolved:
                                    row["text"] = resolved
                                if "<MOD-" in raw_value_text.upper():
                                    row["text_is_format_code"] = True
                            # P3-1 of a 2026-11-03 report: a Ø10 came back as
                            # {"raw_text": "<MOD-DIAM>", "text": "Ø", "value_mm": 10.0} — the
                            # symbol resolved but the NUMBER did not, because the format
                            # string has no <DIM> placeholder. Anyone checking the sheet had
                            # to splice value_mm in by hand.
                            #
                            # display_text is that splice, done once: the symbol(s) followed
                            # by the value. `text` and `raw_text` are untouched so the
                            # code-vs-rendering mismatch stays visible.
                            try:
                                _value = row.get("value_mm")
                                _symbols = str(row.get("text") or "")
                                if _value is not None and not any(
                                        c.isdigit() for c in _symbols):
                                    _number = ("%.2f" % float(_value)).rstrip("0").rstrip(".")
                                    row["display_text"] = (_symbols + _number).strip()
                                elif _value is not None:
                                    row["display_text"] = _symbols
                                # P3-6 of a 2026-11-03 report asked whether the "8.00 /
                                # 8.00" two-line Ø8 came from this tool. It does not: that
                                # dimension carries `tolerance_type: 3` (LIMIT) in the MODEL,
                                # and InsertModelItems brings it along. Confirmed on 旋转轴 —
                                # one Ø8 has tol 3, the other has tol 0.
                                #
                                # The values themselves are not read back here (GetTolerance
                                # Values was not available), so the text only ANNOTATES that a
                                # tolerance exists. Claiming the numbers would be invention.
                                _tol = row.get("tolerance_type")
                                if _tol and int(_tol) != 0 and row.get("display_text"):
                                    row["display_text"] = "%s (公差类型 %d，值未读回)" % (
                                        row["display_text"], int(_tol))
                            except Exception:
                                pass
                if kind == "note":
                    try:
                        text = ann.GetSpecificAnnotation.GetText
                        if isinstance(text, str):
                            # P2, 2026-11-03: the WRITE side replaces Ø with <MOD-DIAM> (and
                            # explains why), but the READ side returned the raw code, so a
                            # caller could not tell whether the sheet shows "Ø8" or the
                            # literal text. Dimensions already had `display_text`; notes now
                            # get the same treatment, plus the raw string kept aside.
                            row["text_raw"] = text
                            resolved_note = _resolve_display_text(text)
                            row["text"] = resolved_note or text
                            if resolved_note and resolved_note != text:
                                row["display_text"] = resolved_note
                                row["text_is_format_code"] = True
                    except Exception:
                        pass
                    # Title-block and frame text is template furniture, not annotation the
                    # drafter added: a freshly created A3 sheet reports 82 "notes" whose
                    # content is 审核/校核/设计/标记. Counting them hides real notes and
                    # inflates the note tally that drawing_audit and delivery_check read.
                    try:
                        position = row.get("position_m")
                        if position and sheet_w and sheet_h:
                            if position[1] < sheet_h * 0.13 and position[0] > sheet_w * 0.5:
                                kind = "title_block_text"
                    except Exception:
                        pass
                    # Position alone missed the frame zone labels (1 2 3 4 / A B C D /
                    # 分区 / 更改文件号 / 年 月 日 / 阶 段 标 记): an empty A3 sheet still
                    # reported 40 "notes", so annotation_count read 91 against 24 real
                    # annotations. Match on content too — a one- or two-character token, or
                    # a known template phrase, is never a drafter's note.
                    template_phrase = str(row.get("text") or "").strip()
                    is_furniture = template_phrase in TEMPLATE_TEXT or (
                        0 < len(template_phrase) <= 2
                        and all(ch.isalnum() for ch in template_phrase)
                    )
                    if kind in ("note", "title_block_text") and is_furniture:
                        kind = "template_text"
                row["kind"] = kind
                rows.append(row)

        for view in _iter_views(doc):
            is_sheet = getattr(view, "Type", None) == 1
            if view_name and not is_sheet and view.Name != view_name:
                continue
            scan(view, "sheet" if is_sheet else view.Name)

        # Totals are derived FROM the rows. Incrementing a counter during the scan and
        # appending the row after a dedupe `continue` made them disagree: a repeated name
        # bumped the count but produced no row, so totals said 15 dimensions while only 1
        # row existed. One source of truth instead of two.
        from collections import Counter

        counts = dict(Counter(row.get("kind") for row in rows))

        payload = drawing_ok(
            drawing=doc.GetTitle,
            totals=counts,
            # Count only annotation the drafter (or this toolkit) put there. Template
            # furniture inflated this to 91 against 24 real annotations on an empty sheet,
            # which made the "real drawings average 46.5 notes" baseline incomparable.
            annotation_count=sum(
                1 for row in rows
                if row.get("kind") not in ("title_block_text", "template_text")
            ),
            title_block_text_count=counts.get("title_block_text", 0),
            template_text_count=counts.get("template_text", 0),
            annotations=rows,
            notes=[
                "geometric_tolerance 的 frame1 依次为 公差值/基准1/基准2/基准3/符号位。",
                "surface_finish 只能报存在，内容读取需早绑定。",
            ],
        )
        if _cache_key is not None and _ANNOT_CACHE is not None:
            try:
                _ANNOT_CACHE[_cache_key] = copy.deepcopy(payload)
            except Exception:
                pass
        payload.setdefault("cached", False)
        return payload
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("annotations_failed", str(exc))


@perf_hooks.timed("delivery_check")
def delivery_check(model_path, drawing_path, ai_supplied=None, require_gdt=False,
                   strict_attachments=False):
    """Aggregate model ledger + drawing audit + annotation inventory into a verdict.

    Process §7 asks for exactly three outcomes, and forbids calling a drawing
    "manufacturable" without evidence:

      可制造        關鍵尺寸/孔/公差齐全，双重自检通过
      待设计审核     图纸对象正确，但含调用方声明为「AI 拟定」的关键值
      仅供参考/未完成 有越界/重叠/空视图、无尺寸、模型疑点未解、缺孔标注等阻塞项

    ai_supplied: list of field names whose values the caller supplied (materials,
    tolerances, fits) — anything listed here downgrades the verdict and is reported.

    Dimensions with no readable attachment are reported; strict_attachments=True also
    makes them a blocker (§6.1 literal reading). They are a warning by default because
    GetAttachedEntities2 returns nothing for about half of the imported dimensions no
    matter which import parameters are used — whether those are truly floating or just
    unreadable is not settled, so the verdict should not hinge on it.
    """
    try:
        from . import ledger as _ledger

        blockers, pending, passed, checks = [], [], [], {}
        ledger_missing = []

        def record(name, ok, detail=None, blocking=True, **extra):
            # `extra` carries the numbers behind the verdict (attached vs excused); without
            # them a reader cannot tell "8 attached" from "23 attached".
            entry = {"ok": bool(ok), "detail": detail}
            entry.update(extra)
            checks[name] = entry
            if ok:
                passed.append(name)
            elif blocking:
                blockers.append({"check": name, "detail": detail})
            else:
                pending.append({"check": name, "detail": detail})

        # ---- model side -------------------------------------------------------
        # Faces are needed to count hole candidates; the extra scan costs a few seconds.
        led = _ledger.build_feature_ledger(model_path, include_faces=True)
        if not led.get("ok"):
            return error("model_unreadable", led.get("message") or "ledger failed",
                         "Check the model opens and rebuilds before delivering a drawing.")
        model_stats = led.get("stats") or {}
        record("model_readable", True, "类别 %s" % led.get("category"))
        doubts = led.get("doubts") or []
        # P2-5: this tool makes and checks drawings; it deliberately does NOT rebuild
        # models. A source part SolidWorks flags as needing a rebuild is not a drawing
        # defect, yet it was a BLOCKER the caller could never clear from inside this
        # toolkit — so the verdict could never reach 可制造. It stays visible as a pending
        # item instead of a blocker.
        record("model_clean", not doubts,
               "; ".join(str(d.get("kind")) for d in doubts) or None,
               blocking=False)

        # ---- drawing side -----------------------------------------------------
        audit = drawing_audit(drawing_path)
        if not audit.get("ok"):
            return error("drawing_unreadable",
                         audit.get("message") or "audit failed",
                         "The drawing must open and rebuild.")
        warnings = [w.get("issue") for w in (audit.get("warnings") or [])]
        view_count = audit.get("view_count") or 0
        dim_total = audit.get("total_dimensions") or 0

        record("views_present", view_count > 0, "%d 个视图" % view_count)
        record("no_view_outside_sheet", "view_outside_sheet" not in warnings,
               "有视图超出图纸" if "view_outside_sheet" in warnings else None)
        record("no_view_overlap", "views_overlap" not in warnings,
               "有视图互相重叠" if "views_overlap" in warnings else None)
        record("no_blank_view", "blank_view" not in warnings,
               "存在空白视图" if "blank_view" in warnings else None)
        record("dimensions_present", dim_total > 0, "%d 个尺寸" % dim_total)

        # ---- annotations -----------------------------------------------------
        anns = list_drawing_annotations(drawing_path)
        if not anns.get("ok"):
            return error("annotations_unreadable", anns.get("message") or "failed")
        totals = anns.get("totals") or {}
        holes_in_drawing = int(totals.get("hole_callout", 0))
        toleranced = 0
        for row in anns.get("annotations") or []:
            if row.get("kind") == "dimension" and row.get("tolerance_type"):
                toleranced += 1
        gtols = int(totals.get("geometric_tolerance", 0))

        hole_candidates = len(led.get("holes") or [])
        if hole_candidates:
            # Hole information can legitimately land three ways: a native hole callout, a
            # thread callout, or a plain Ø dimension on the circular edge. Any of them
            # carries the manufacturing information, so requiring the callout object
            # specifically would reject valid drawings.
            holes_in_drawing = int(totals.get("hole_callout", holes_in_drawing) or 0)
            diameter_dims = 0
            for row in anns.get("annotations") or []:
                if row.get("kind") != "dimension":
                    continue
                # P1-1: never decide this from display text alone. `GetText()` with no
                # slot returns '' for a dimension, so the old text test found 0 diameters
                # on a sheet that printed Ø28/Ø23/Ø12/Ø18/Ø13. The reliable signal is the
                # dimension type (5 = diameter, measured 2026-10-31); the raw value-slot
                # string stays as a fallback for builds that report no type.
                if int(row.get("dimension_type") or -1) == 5:
                    diameter_dims += 1
                    continue
                marker = (str(row.get("raw_text") or "") + str(row.get("text") or "")).upper()
                if "Ø" in marker or "MOD-DIAM" in marker:
                    diameter_dims += 1
            covered = holes_in_drawing + diameter_dims
            # P3-2 of a 2026-11-03 report: `ok: true` next to `facts.hole_callouts: 0` read
            # like "hole callouts are fine", when in fact there were NONE and the coverage
            # came entirely from plain Ø dimensions. Say which it is, in the detail line.
            _how = ("仅靠 %d 处 Ø 尺寸覆盖（**没有孔标注对象**）" % diameter_dims
                    if holes_in_drawing == 0 and diameter_dims else
                    "%d 处孔标注 + %d 处 Ø 尺寸" % (holes_in_drawing, diameter_dims))
            record("hole_callouts", covered > 0,
                   "%s / 模型 %d 个圆柱面候选" % (_how, hole_candidates),
                   "背面或被遮挡的孔 SolidWorks 不给标注，需人工补或换视图",
                   hole_callout_objects=holes_in_drawing,
                   diameter_dimensions=diameter_dims)
        record("tolerances_present", toleranced > 0,
               "%d 个尺寸带公差" % toleranced, blocking=False)

        # Cosmetic threads (装饰螺纹线) — P0-3 of a 2026-11-03 report: a shaft with a real
        # 10 mm cosmetic thread passed every check, because the thread lives nowhere the
        # ledger looks. Verified on the reporter's own part:
        #   * feature tree has 9 features, none of them Thread/Cosmetic
        #   * `GetCosmeticThreads` / `GetCosmeticThreadCount` do not exist on this binding
        #   * the SLDPRT binary scan finds nothing
        # What DOES exist is an annotation on the drawing: `GetType == 1`, named
        # "装饰螺纹线1". So the check runs at the drawing level — if such an annotation is
        # present but no note mentions a thread callout, the drawing is incomplete.
        thread_annotations = 0
        thread_notes = 0
        for row in (anns.get("annotations") or []):
            name = str(row.get("name") or "")
            kind = str(row.get("kind") or "")
            text = (str(row.get("text") or "") + str(row.get("raw_text") or "")).upper()
            if "螺纹" in name or kind == "other_1":
                thread_annotations += 1
            if "螺纹" in text or "M10" in text or "M8" in text or "M12" in text:
                thread_notes += 1
        if thread_annotations and not thread_notes:
            # record(name, ok, detail, blocking) — `blocking` is positional, and the
            # explanation goes into `detail` rather than a fifth argument.
            record("cosmetic_threads", False,
                   "图纸有 %d 处装饰螺纹线注解，但没有任何螺纹标注（M10×1 之类）。"
                   "规格在模型侧读不到（特征树无 Thread 特征、GetCosmeticThreads 不存在），"
                   "只能由设计确认后手写；交付前必须在技术要求或尺寸注记里写明螺纹规格。"
                   % thread_annotations,
                   thread_annotations=thread_annotations)
        else:
            record("cosmetic_threads", True,
                   "%d 处螺纹注解 / %d 处螺纹文字"
                   % (thread_annotations, thread_notes),
                   blocking=False)

        # Attachment: GetAttachedEntities2 returns nothing for roughly half of the
        # dimensions that InsertModelAnnotations3 creates, and changing the import
        # parameters does not move that ratio. It is unclear whether those are really
        # floating or just unreadable, so by default this is a WARNING; strict mode
        # (process §6.1 literal reading) turns it into a blocker.
        att = verify_dimension_attachments(drawing_path)
        if att.get("ok"):
            all_orphans = att.get("orphans") or []
            # Every orphan counts. An earlier version excused the ones sitting near the view
            # outline; since dimensions are parked outside the outline by design, that
            # excused all of them (15/15 in a report) and turned a real failure into ok.
            # Proximity is reported, never used to clear a blocker.
            orphan = len(all_orphans)
            near_view = sum(1 for o in all_orphans if o.get("orphan_near_view"))
            dim_count = int(att.get("dimension_count") or 0)
            attached = max(dim_count - orphan, 0)
            suffix = ("（其中 %d 条位置在视图附近，但引线未验证）" % near_view
                      if near_view else "")
            record("dimension_attachments", orphan == 0,
                   (("%d/%d 个尺寸无关联实体%s" % (orphan, dim_count, suffix))
                    if orphan
                    else ("%d 个尺寸均已关联" % dim_count)),
                   excused_count=0,
                   attached_count=attached,
                   blocking=bool(strict_attachments) and orphan > 0)
        else:
            record("dimension_attachments", False,
                   "关联信息读取失败: %s" % (att.get("message") or "unknown"))
        unattached = int(att.get("orphan_count") or 0) if att.get("ok") else None
        # Measured on 100 real drawings: surface finish appears in 60%, geometric
        # tolerance in only 7%. So finish is the routine check and GD&T stays optional.
        finishes = int(totals.get("surface_finish", 0))
        record("surface_finish_present", finishes > 0,
               "%d 处表面粗糙度" % finishes, blocking=False)
        if require_gdt:
            record("gdt_present", gtols > 0, "%d 处形位公差" % gtols)
        else:
            checks["gdt_present"] = {
                "ok": gtols > 0, "detail": "%d 处形位公差（真实图纸仅 7%% 有）" % gtols,
                "required": False,
            }

        # ---- ledger reconciliation (§6.1: 逐条比对, not just count) ----------
        rec = reconcile_ledger(model_path, drawing_path)
        if rec.get("ok"):
            coverage = rec.get("coverage")
            unmatched = rec.get("unmatched_count") or 0
            record("ledger_reconciled",
                   coverage is not None and coverage >= 0.9,
                   "台账覆盖率 %s（%s/%s 个尺寸在图纸上找到，缺 %s）"
                   % (coverage, rec.get("matched"), rec.get("ledger_dimensions"), unmatched),
                   blocking=False)
            # P1-3: a ratio cannot tell you WHAT is missing, and on a "must be repairable
            # from this drawing" job the missing five are the whole point.
            from ..sw_session import fix_mojibake as _fix_mojibake

            ledger_missing = [
                {"feature": _fix_mojibake(row.get("feature")),
                 "dimension": _fix_mojibake(row.get("dimension")),
                 "value_mm": row.get("value_mm"),
                 "dimension_type": row.get("dimension_type")}
                for row in (rec.get("unmatched_ledger") or [])
            ]
        else:
            record("ledger_reconciled", False,
                   "对账失败: %s" % (rec.get("message") or "unknown"), blocking=False)

        # ---- caller-declared assumptions -------------------------------------
        if ai_supplied:
            for field in ai_supplied:
                pending.append({"check": "ai_supplied", "detail": str(field)})

        if blockers:
            verdict = "仅供参考/未完成"
        elif pending:
            verdict = "待设计审核"
        else:
            verdict = "可制造"

        return drawing_ok(
            verdict=verdict,
            drawing=audit.get("drawing"),
            model=led.get("part"),
            category=led.get("category"),
            checks=checks,
            passed=passed,
            blockers=blockers,
            pending=pending,
            strict_attachments=bool(strict_attachments),
            # P1-3: the concrete list behind `ledger_reconciled`'s ratio.
            ledger_missing=ledger_missing,
            ledger_missing_count=len(ledger_missing),
            facts={
                "views": view_count,
                "dimensions": dim_total,
                "dimensions_with_tolerance": toleranced,
                "hole_callouts": holes_in_drawing,
                "geometric_tolerances": gtols,
                "surface_finishes": finishes,
                "cylinder_faces_in_model": hole_candidates,
                "dimensions_unattached": unattached,
                "features": model_stats.get("features"),
                "model_doubts": [d.get("kind") for d in doubts],
            },
            note="verdict 依据流程 §7；blockers 非空即不可宣称可制造，"
                 "ai_supplied 非空则只能算待设计审核。无关联实体的尺寸计入 blockers"
                 "（§6.1：无关联视为未完成）。",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("delivery_check_failed", str(exc))


SELECT_TYPE_NAMES = {
    10: "vertex", 11: "edge", 12: "face", 13: "sketch_segment",
    14: "sketch_point", 20: "component", 2: "body",
}


@perf_hooks.timed("dim_attachments")
def verify_dimension_attachments(drawing_path, view_name=None):
    """Report what each dimension is actually attached to (process §6.1).

    A dimension with no attached entity — or attached only to a sketch segment instead
    of a model edge — is not traceable to geometry and must not be counted as complete.
    Attachment is read through IAnnotation.GetAttachedEntities2 / GetAttachedEntityTypes.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        rows, counts = [], {}
        orphans = []
        # Same dimension gets enumerated many times (measured 11x for one D1/275.0), and
        # name is NOT an identity here — some drawings name every dimension "D1" — so
        # dedupe on (view, name, position, value) and also report the unique count.
        seen_dims = set()
        duplicates_skipped = 0
        raw_hits = 0
        by_primary = {}

        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and view.Name != view_name:
                continue
            try:
                raw = view.GetAnnotations
            except Exception:
                continue
            items = list(raw) if isinstance(raw, (tuple, list)) else ([raw] if raw else [])
            for item in items:
                if item is None:
                    continue
                try:
                    ann = win.Dispatch(item)
                    if ann.GetType != 4:
                        continue
                    dim = ann.GetSpecificAnnotation.GetDimension2(0)
                except Exception:
                    continue
                if dim is None:
                    continue
                row = {"view": view.Name}
                try:
                    row["name"] = str(ann.GetName)
                except Exception:
                    row["name"] = None
                try:
                    row["value_mm"] = round(float(dim.SystemValue) * 1000, 3)
                except Exception:
                    row["value_mm"] = None
                # A driving dimension constrains the model; a reference (从动) one only
                # reports what the geometry already is. GB practice puts reference
                # dimensions in parentheses, and a drawing full of unmarked reference
                # dimensions looks dimensioned while constraining nothing.
                reference = None
                for attempt in ("IsReference", "GetReference"):
                    try:
                        value = getattr(dim, attempt)
                        reference = bool(value() if callable(value) else value)
                        break
                    except Exception:
                        continue
                row["is_reference"] = reference
                row["role"] = ("reference" if reference
                               else ("driving" if reference is False else "unknown"))
                # Needed by the dedupe key below. Without it the key collapsed to
                # (view, name, value) and drawings where every dimension is named "D1"
                # lost genuinely-distinct dimensions that happened to share a value.
                try:
                    row["position_m"] = [round(float(v), 4) for v in ann.GetPosition]
                except Exception:
                    row["position_m"] = None
                types = []
                try:
                    raw_types = ann.GetAttachedEntityTypes
                    if isinstance(raw_types, (tuple, list)):
                        types = [int(t) for t in raw_types]
                    elif raw_types is not None:
                        types = [int(raw_types)]
                except Exception:
                    pass
                row["attached_count"] = len(types)
                row["attached_kinds"] = [SELECT_TYPE_NAMES.get(t, "type_%d" % t) for t in types]

                # Dedupe first, bookkeep after. It used to be the other way round, so
                # counts/orphans counted RAW HITS (23) while dimension_count counted
                # unique ones (11) — the three numbers described the same sheet and
                # disagreed. A report caught this: "8+15=23 != 11".
                raw_hits += 1
                # Identity of a driving dimension is its MODEL full name. The old key was
                # (view, name, position, value): SolidWorks reuses D1/D2 per sketch and a
                # sheet can hold many equal values, so distinct dimensions collapsed —
                # measured 23 real dimensions reported as 18 with duplicates_skipped=5.
                # `_annotation_dimension_full_name` already existed (eliminate_duplicates
                # uses it); the fallback keeps unnamed annotations countable.
                dedupe_key = (_annotation_dimension_full_name(ann, win)
                              or (row.get("view"), row.get("name"), row.get("value_mm")))
                if dedupe_key in seen_dims:
                    duplicates_skipped += 1
                    continue
                seen_dims.add(dedupe_key)

                # Two different questions, two different counters. `counts` answers "how
                # many entity attachments of each kind" (one dimension can attach to many
                # edges); `by_primary` answers "how many dimensions are primarily of this
                # kind". Mixing them produced a report where 8+15=23 didn't equal 11 —
                # neither number was wrong, they were answering different questions.
                for kind in row["attached_kinds"]:
                    counts[kind] = counts.get(kind, 0) + 1
                primary = row["attached_kinds"][0] if row["attached_kinds"] else "none"
                by_primary[primary] = by_primary.get(primary, 0) + 1
                if not types:
                    # `GetAttachedEntities2` is empty; whether that means "loose" or just
                    # "moved with SetPosition" cannot be decided from the position alone.
                    # Record the proximity observation, do not treat it as a clearance.
                    position = row.get("position_m")
                    near_leader = False
                    if position:
                        try:
                            box = precise_outline(view)
                            if box:
                                gap_x = max(box[0] - position[0], 0.0,
                                            position[0] - box[2])
                                gap_y = max(box[1] - position[1], 0.0,
                                            position[1] - box[3])
                                near_leader = ((gap_x * gap_x + gap_y * gap_y) ** 0.5
                                               <= 0.05)
                        except Exception:
                            near_leader = False
                    # Proximity is NOT evidence of a leader. Since v0.8.27 dimensions are
                    # parked just outside the view outline by default, so *every* dimension
                    # in a tidy sheet sits within 50 mm — and a report showed this "fix"
                    # excusing 15 genuine orphans (23 = 8 attached + 15 excused) while the
                    # same receipt still said "23 个尺寸均已关联". Report the observation for
                    # what it is and leave the verdict alone.
                    row["orphan_near_view"] = near_leader
                    orphans.append({"view": view.Name, "name": row["name"],
                                    "value_mm": row.get("value_mm"),
                                    "position_m": position,
                                    "orphan_near_view": near_leader})
                rows.append(row)

        total = len(rows)
        return drawing_ok(
            drawing=doc.GetTitle,
            dimension_count=total,
            attachment_summary=counts,
            dimensions_by_primary_attachment=by_primary,
            orphan_count=len(orphans),
            orphans=orphans,
            unique_dimension_count=len(rows),
            raw_hits=raw_hits,
            duplicates_skipped=duplicates_skipped,
            reference_count=sum(1 for r in rows if r.get("is_reference") is True),
            driving_count=sum(1 for r in rows if r.get("is_reference") is False),
            role_unknown=sum(1 for r in rows if r.get("is_reference") is None),
            dimensions=rows,
            note="orphan_count > 0 表示有尺寸没有关联到任何实体，"
                 "按流程 §6.1 属于未完成；只挂在 sketch_segment 上的也应复核。"
                 " is_reference=true 是从动参考尺寸，不驱动模型——GB 习惯加括号标注，"
                 "整张图全是参考尺寸时图面看着标注齐全，实际没有约束任何东西。",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("attachment_check_failed", str(exc))


def prune_orphan_dimensions(drawing_path, view_name=None, dry_run=True, save=True,
                            confirm=False):
    """Delete dimensions that are attached to no geometry (P2-4).

    `auto_dimension` imports every driving dimension; on real parts a share of them land
    unattached — `GetAttachedEntities2` returns nothing for roughly half of them whatever
    the import parameters are. There was no way to clear those: `delete_dimensions` works by
    VALUE, so it cannot tell two 23 mm dimensions apart (a valve body held three).

    Dry-run by default and it reports view / name / value / position for each candidate.
    `dry_run=False` deletes by name + position (position is what separates repeated D1/D2
    names) and then RE-RUNS the attachment check, so the receipt carries the remaining
    orphan count instead of assuming the deletes worked.

    P1-2 — `orphan` means "GetAttachedEntities2 returned nothing", NOT "this dimension is
    loose". A report moved dimensions with SetPosition and then found 11 orphans including
    the 50 mm overall length and the 25 mm hole pitch; every one of them visibly led to real
    geometry on the render. Deleting that list would have gutted the drawing. So a real
    delete now requires `confirm=True`; without it the call returns
    `confirmation_required` with the same list, and the receipt always carries `caveat`.
    """
    try:
        from . import layout as _layout

        before = verify_dimension_attachments(drawing_path, view_name=view_name)
        if not before.get("ok"):
            return error("attachments_unreadable", before.get("message") or "failed")
        orphans = before.get("orphans") or []
        targets = [{"view": row.get("view"), "name": row.get("name"),
                    "value_mm": row.get("value_mm"),
                    "position_m": row.get("position_m")} for row in orphans]
        payload = {
            "drawing": before.get("drawing"),
            "orphan_count_before": len(targets),
            "orphan_count": len(targets),
            "dimension_count": before.get("dimension_count"),
            "dry_run": bool(dry_run),
            "targets": targets,
            "removed_count": 0,
        }
        # Always present, not only in dry-run: this is the sentence that decides whether
        # deleting the list is safe, and it applies to both modes.
        payload["caveat"] = (
            "orphan 只表示 GetAttachedEntities2 返回空，不等于「尺寸真的孤立」。"
            "实测：尺寸被 SetPosition 移动后会出现这种读不到附件的情况，"
            "而它在图纸上有正常引线、指向真实几何（含总长、孔距这类关键尺寸）。"
            "照单全删会让图纸缺料——先看渲染图逐条确认，或用 "
            "delete_annotation(name + position_m) 精确删。")
        if dry_run or not targets:
            if not dry_run and not targets:
                payload["note"] = "没有 orphan 尺寸需要清理。"
            return drawing_ok(**payload)
        if not confirm:
            return error(
                "confirmation_required",
                "检测到 %d 条 orphan 候选，但未确认删除（confirm=False）。" % len(targets),
                "orphan 可能只是取不到附件。核对渲染图后再传 confirm=True。",
                caveat=payload["caveat"], targets=targets)

        removed, failed = [], []
        for row in targets:
            name = row.get("name")
            if not name:
                failed.append({"name": None, "reason": "no name reported"})
                continue
            result = _layout.delete_annotation(drawing_path, name, save=False,
                                               position_m=row.get("position_m"))
            if result.get("ok"):
                removed.append({"name": name, "view": row.get("view"),
                                "value_mm": row.get("value_mm")})
            else:
                failed.append({"name": name, "reason": result.get("error"),
                               "message": str(result.get("message") or "")[:120]})
        if save:
            _save_drawing(_open_drawing(drawing_path))
        after = verify_dimension_attachments(drawing_path, view_name=view_name)
        payload.update({
            "removed": removed,
            "failed": failed,
            "removed_count": len(removed),
            "orphan_count_after": (int(after.get("orphan_count") or 0)
                                   if after.get("ok") else None),
            "orphan_count": (int(after.get("orphan_count") or 0)
                             if after.get("ok") else None),
        })
        return drawing_ok(**payload)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("prune_failed", str(exc))


def _view_kind(view):
    """What sort of view this is (IView.Type: 1=sheet, 2=section, 3=detail, 4=projected).

    Only used to label a circle's annotatability: report a view kind rather than making the
    caller infer it from the view name, which is a localised string.
    """
    try:
        view_type = int(view.Type)
    except Exception:
        return "unknown"
    # Measured on SW2024: a drawing SHEET is 1, a standard model view is 7, a section 2 and
    # a detail 3. (The first version of this map guessed and printed "type_7" for every view.)
    return {1: "sheet", 2: "section", 3: "detail", 4: "projected", 7: "model"}.get(
        view_type, "type_%d" % view_type)


def list_circular_edges(drawing_path, view_name=None, min_diameter_mm=0.0):
    """Circular edges of a view, with sheet position and diameter.

    Reads ONLY `ICurve.CircleParams` — never vertices. That matters: a view's visible
    entities are SILHOUETTE edges and `GetStartVertex` returns None for every one of them,
    so the previous vertex-based scan answered "0 circles" on a valve body that visibly had
    thirteen (measured 2026-10-31). CircleParams returns centre, axis and radius in one
    call, which is also cheaper than two vertex reads per edge.

    Sheet positions use the same size-matched axis mapping as `add_section_view`: the
    model's own bounding box, not a vertex bounding box. When the mapping is unavailable
    (an ambiguous box, a section/detail view, a missing referenced model) the row keeps its
    diameter and reports `x: null` plus `position_unavailable` — a made-up coordinate is
    worse than none, because callers feed it straight into `add_hole_callout`.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        out = []
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and view.Name != view_name:
                continue
            try:
                edges = [win.Dispatch(e) for e in (view.GetVisibleEntities2(view, 1) or ())]
            except Exception:
                continue

            circles = []
            for edge in edges:
                try:
                    curve = win.Dispatch(edge.GetCurve)
                    if curve is None or not curve.IsCircle:
                        continue
                    params = list(curve.CircleParams)
                    radius_m = float(params[6])
                    diameter = round(radius_m * 2000, 2)
                    if diameter < float(min_diameter_mm):
                        continue
                    circles.append({
                        "diameter_mm": diameter,
                        "centre_model": [float(params[0]), float(params[1]),
                                         float(params[2])],
                        # Circle normal. A circle is a hole MOUTH only when its normal is
                        # (anti)parallel to the direction the view looks along; otherwise
                        # you are seeing the cylinder wall, and AddHoleCallout2 refuses.
                        "axis": [float(params[3]), float(params[4]), float(params[5])],
                    })
                except Exception:
                    continue
            if not circles:
                continue

            axis_map, reason = _view_axis_map(view)
            # Reading the part box switches SolidWorks' active document; come back.
            doc = _open_drawing(drawing_path)
            try:
                doc.ActivateView(view.Name)
            except Exception:
                pass
            pos = list(view.Position)
            scale = float(view.ScaleDecimal)

            for item in circles:
                row = {"view": view.Name, "diameter_mm": item["diameter_mm"],
                       "x": None, "y": None}
                depth_axis = None
                if axis_map:
                    axes = axis_map["axes"]
                    centre_m = axis_map["centre_m"]
                    row["x"] = round(pos[0] + (item["centre_model"][axes[0]]
                                               - centre_m[axes[0]]) * scale, 5)
                    row["y"] = round(pos[1] + (item["centre_model"][axes[1]]
                                               - centre_m[axes[1]]) * scale, 5)
                    # The two axes the view DISPLAYS leave exactly one it looks along.
                    depth_axis = 3 - axes[0] - axes[1]
                else:
                    row["position_unavailable"] = reason

                # ---- annotatability, predicted instead of discovered ----------------
                # A report burned 13 guaranteed-failing calls on an outline view because
                # nothing in this list said which circles can take a callout. The rule is
                # geometric: the circle's normal must run along the view direction.
                row["view_kind"] = _view_kind(view)
                axis_vec = item.get("axis") or [0.0, 0.0, 0.0]
                if depth_axis is not None and any(abs(v) > 1e-9 for v in axis_vec):
                    dominant = max(range(3), key=lambda i: abs(axis_vec[i]))
                    faces_viewer = (dominant == depth_axis
                                    and abs(axis_vec[depth_axis]) >= 0.9)
                    row["facing"] = "front" if faces_viewer else "side"
                    row["annotatable_hint"] = faces_viewer
                else:
                    row["facing"] = "unknown"
                    row["annotatable_hint"] = "unknown"
                out.append(row)
        return drawing_ok(drawing=doc.GetTitle, count=len(out), circles=out)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("circle_scan_failed", str(exc))


def _callout_diameter_matches(text, expected_mm, tol=0.08):
    """True/False when the callout prints an explicit Ø; None when it prints none.

    A report showed callouts reading "Ø6 贯穿 / Ø99 ↧3" on a part with no Ø99 anywhere.
    The text PARSED and `IsHoleCallout` was true, so the tool signed off on a dimension that
    contradicts the geometry. Comparing the printed values with the measured edge diameter
    is the only cheap way to catch that — a wrong number is worse than a missing callout,
    because the drawing looks authoritative.
    """
    import re

    values = [float(v) for v in re.findall(r"[Ø⌀]\s*([0-9]+(?:\.[0-9]+)?)", str(text or ""))]
    if not values:
        return None
    expected = float(expected_mm)
    tol_abs = max(0.2, abs(expected) * float(tol))
    return any(abs(v - expected) <= tol_abs for v in values)


@perf_hooks.timed("hole_callouts")
def add_hole_callouts(drawing_path, view_name=None, min_diameter_mm=0.0,
                      max_callouts=40, budget_s=25.0, leader_x_m=None,
                      leader_y_m=None, save=True):
    """Native hole callouts on the holes VISIBLE in a view, one callout per hole.

    Verified on SW2026 (2026-10-28):
      * `AddHoleCallout2` does NOT show the modal dialog the 2001-era Remarks warns about;
        it returns in ~0.03 s.
      * Success must be judged by `IDisplayDimension.IsHoleCallout` — a hole callout is a
        DisplayDimension (GetType 4), there is no separate annotation type.
      * **The edge must face the viewer.** On the panel-box door (6 mm sheet) the Ø6 edges
        at z=6.0 (the far face) all returned null, while Ø8 and Ø15 edges at z=0.0 succeeded.
        So a through hole contributes two circular edges and only the near one is annotatable.

    Hence: collect edges per view, group by projected centre, and keep ONE edge per hole —
    preferring a complete circle, else the one nearest the viewer along the sheet normal.

    `leader_x_m` / `leader_y_m` (P1-7) pin the callout text to a sheet point in metres.
    By default the text is stacked just outside the view's outline, which on a compact part
    threw the leader 100 mm past the frame border. The per-callout 12 mm vertical stack
    still applies when a custom anchor is given, so callouts do not overlap each other.
    Each created item reports `anchor_m` (requested) and `position_m` (read back).

    Performance: only this view's visible edges are enumerated. A whole-model scan took
    >400 s; one view has 9-40 circular edges and the whole call takes ~19 s.
    """
    try:
        import time

        import win32com.client as win

        doc = _open_drawing(drawing_path)
        targets = []
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and view.Name != view_name:
                continue
            targets.append(view)
        if not targets:
            return error("no_views", "no model views to annotate")

        created, failed = [], []
        started = time.time()
        budget_exceeded = False
        floor_m = max(float(min_diameter_mm) * 0.0005, 1e-6)
        scanned = 0
        edges_scanned = 0
        rejected = 0
        groups_total = 0

        for view in targets:
            try:
                doc.ActivateView(view.Name)
            except Exception:
                pass
            try:
                components = list(view.GetVisibleComponents or ())
            except Exception:
                components = []

            # --- collect, then group by projected centre -------------------------------
            # A circle wider than 40% of the view is an outer contour, not a hole. The
            # track-wheel outline (Ø220-290 mm) came back as IsHoleCallout=true with text
            # "R"; rejecting it after the call still left a radius callout on the sheet and
            # still incremented annotation.hole_callout. Judge before inserting.
            view_box = list(view.GetOutline or [])
            view_width = (view_box[2] - view_box[0]) if len(view_box) >= 4 else None
            candidates = []
            for component in components:
                try:
                    edges = list(view.GetVisibleEntities2(component, 1) or ())
                except Exception:
                    continue
                for raw in edges:
                    try:
                        edge = win.Dispatch(raw)
                        curve = win.Dispatch(edge.GetCurve)
                        if curve is None or not curve.IsCircle:
                            continue
                        params = list(curve.CircleParams)
                        radius_m = float(params[6])
                        if radius_m < floor_m:
                            continue
                        # Count every circular edge the scan met, not just the ones that
                        # survive the filters: "scanned: 0" next to "failed: 20" reads as
                        # "this view had nothing", when 20 edges were seen and rejected.
                        edges_scanned += 1
                        if view_width and radius_m * 2 > view_width * 0.4:
                            rejected += 1
                            failed.append({"view": view.Name,
                                           "diameter_mm": round(radius_m * 2000, 2),
                                           "reason": "skipped: circle is >40% of the view "
                                                     "width, treated as an outer contour"})
                            continue
                        candidates.append({
                            "edge": edge,
                            "radius_m": radius_m,
                            "centre": (round(float(params[0]), 4), round(float(params[1]), 4)),
                            "z": float(params[2]),
                            "complete": edge.GetStartVertex is None,
                        })
                    except Exception:
                        continue
            scanned += len(candidates)

            grouped = {}
            for item in candidates:
                # Same centre + same radius = same hole seen from both faces.
                key = (item["centre"], round(item["radius_m"], 4))
                grouped.setdefault(key, []).append(item)
            groups_total += len(grouped)

            outline = list(view.GetOutline or [])
            default_x = (outline[2] + 0.015) if len(outline) >= 4 else 0.30
            default_y = ((outline[1] + outline[3]) / 2.0) if len(outline) >= 4 else 0.20
            # P1-7: the old default anchor sat just outside the view outline, which on a
            # compact part threw the leader well past the sheet border. Callers can now
            # pin the text to any sheet point; the vertical stack below still separates
            # consecutive callouts.
            anchor_x = float(leader_x_m) if leader_x_m is not None else default_x
            anchor_y = float(leader_y_m) if leader_y_m is not None else default_y

            # Consecutive-failure breaker. On a louvered sheet every one of 90 circular
            # edges returned null after a full Select2 + AddHoleCallout2 round trip, and
            # the run burned 82 s to produce nothing. Ten straight nulls means this view's
            # circles are not holes; stop and say so instead of grinding through the rest.
            consecutive_nulls = 0
            for key, members in grouped.items():
                if len(created) + len(failed) >= int(max_callouts):
                    break
                if consecutive_nulls >= 10:
                    failed.append({"view": view.Name, "radius_mm": None,
                                   "reason": "stopped: 10 consecutive callouts returned null "
                                             "(this view's circles are not holes)"})
                    break
                if time.time() - started > float(budget_s):
                    budget_exceeded = True
                    break
                # Prefer a complete circle (the near face usually reads as one), then the
                # edge nearest the viewer along z.
                chosen = sorted(members, key=lambda m: (not m["complete"], m["z"]))[0]
                doc.ClearSelection2(True)
                try:
                    if not chosen["edge"].Select2(False, 0):
                        failed.append({"view": view.Name,
                                       "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                       "reason": "Select2 returned False", "z": chosen["z"]})
                        continue
                except Exception as exc:
                    failed.append({"view": view.Name,
                                   "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                   "reason": "select: %s" % str(exc)[:50], "z": chosen["z"]})
                    continue

                slot = len(created) + len(failed)
                try:
                    display = doc.AddHoleCallout2(anchor_x, anchor_y - slot * 0.012, 0.0)
                except Exception as exc:
                    failed.append({"view": view.Name,
                                   "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                   "reason": "callout: %s" % str(exc)[:50], "z": chosen["z"]})
                    continue
                if display is None:
                    failed.append({"view": view.Name,
                                   "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                   "reason": "returned None (edge not annotatable from this view)",
                                   "z": chosen["z"], "hidden_side": chosen["z"] != 0.0})
                    continue
                dim = win.Dispatch(display)
                try:
                    is_hole = bool(dim.IsHoleCallout)
                except Exception:
                    is_hole = None
                if is_hole is False:
                    failed.append({"view": view.Name,
                                   "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                   "reason": "not a hole callout", "z": chosen["z"]})
                    continue
                text = None
                for index in (0, 1, 2):
                    try:
                        candidate = getattr(dim, "GetText")(index)
                        if candidate:
                            text = str(candidate)
                            break
                    except Exception:
                        continue
                # IsHoleCallout is also True for a plain radius callout on an outer arc:
                # a 220 mm track-wheel outline came back as is_hole_callout=true with
                # text "R". Accepting that let "R" stand in for hole information and
                # suppressed the missing-hole blocker — the tool fooling its own check.
                def _discard(display_dim):
                    """Delete a callout we are about to reject.

                    Rejecting AFTER `AddHoleCallout2` still left the annotation on the sheet
                    (and still counted in annotation.hole_callout), so the inventory and the
                    pixels disagreed. Remove it before reporting failure.
                    """
                    try:
                        ann = win.Dispatch(_call_method(display_dim, "GetAnnotation"))
                        doc.ClearSelection2(True)
                        if ann.Select(False):
                            _call_method(doc, "EditDelete")
                    except Exception:
                        pass

                text_upper = str(text or "").upper()
                has_diameter = ("Ø" in str(text or "")) or ("MOD-DIAM" in text_upper) \
                    or text_upper.startswith("M") or " X " in text_upper
                radius_only = text_upper.startswith("R") and not has_diameter
                # v0.8.33: an UNREADABLE text is NOT evidence of a bad callout. On SW2026
                # `GetText(0/1/2)` returns None for every callout, so treating "no Ø found"
                # as failure deleted all of them (measured: 8 created -> 0 on the sheet).
                # Only reject on POSITIVE evidence — text that explicitly starts with R.
                text_unreadable = not str(text or "").strip()
                if radius_only:
                    _discard(dim)
                    failed.append({"view": view.Name,
                                   "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                   "reason": "not a hole callout (text=%r, likely an outer arc)"
                                             % (text,),
                                   "z": chosen["z"], "radius_only": True})
                    continue
                if not has_diameter and not text_unreadable:
                    _discard(dim)
                    failed.append({"view": view.Name,
                                   "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                   "reason": "callout text carries no diameter (text=%r)"
                                             % (text,),
                                   "z": chosen["z"]})
                    continue
                # P1-1: the printed number must agree with the measured edge.
                expected_d = round(chosen["radius_m"] * 2000, 2)
                if _callout_diameter_matches(text, expected_d) is False:
                    _discard(dim)
                    failed.append({"view": view.Name,
                                   "diameter_mm": expected_d,
                                   "reason": ("text_value_mismatch: callout says %r but the "
                                              "edge measures Ø%.2f" % (str(text)[:40], expected_d)),
                                   "z": chosen["z"]})
                    continue
                callout_point = [anchor_x, anchor_y - slot * 0.012]
                # `GetAnnotation` must be invoked as a METHOD under late binding (see
                # layout.add_note). Read the real text position back so the caller can
                # verify SolidWorks honoured the requested leader anchor (P1-7).
                position_m = None
                try:
                    ann = win.Dispatch(_call_method(dim, "GetAnnotation"))
                    position_m = [round(float(v), 4) for v in ann.GetPosition]
                except Exception:
                    pass
                consecutive_nulls = 0
                created.append({
                    "view": view.Name,
                    "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                    "is_hole_callout": is_hole,
                    "text": text,
                    # Same signal the earlier rejection path needs: keep the callout but say
                    # the text could not be read, so the caller decides on the geometry.
                    "text_unreadable": text_unreadable,
                    "z": chosen["z"],
                    "anchor_m": [round(callout_point[0], 4), round(callout_point[1], 4)],
                    "position_m": position_m,
                    # Kept so the SetText rewrite below can reach the IDisplayDimension.
                    # Stripped before returning — COM objects must not leak into JSON.
                    "_annotation": dim,
                })

            if budget_exceeded or len(created) + len(failed) >= int(max_callouts):
                break

        doc.ClearSelection2(True)
        try:
            doc.EditRebuild3()
        except Exception:
            pass
        if save:
            _save_drawing(doc)
        # --- rewrite the raw format codes into readable text -----------------------
        # AddHoleCallout2 leaves GetText as "<MOD-DIAM>" which renders as those literal
        # characters. IDisplayDimension.SetText replaces it (value lands in slot 1, not 0).
        for item in created:
            dim_obj = item.pop("_annotation", None)
            if dim_obj is None:
                continue
            # Only rewrite a RAW FORMAT CODE. Writing "Ø15" into slot 1 replaced the
            # diameter but left the depth sitting in another slot, so a Ø15 hole rendered as
            # "Ø1515" — the report's "Ø9 -> Θ99". A Ø8 through-hole has no depth and looked
            # fine, which is why this hid for so long. When the callout already reads
            # correctly, SolidWorks' own hole-table text is the right answer.
            # NO SetText. Writing "Ø15" into one slot replaced the diameter while the
            # depth stayed in another, so the sheet printed "Ø1515" — the report's
            # "Ø9 -> Θ99". A Ø8 through hole has no depth, which is why it looked fine.
            # SolidWorks' own hole-table text is correct; only the RECEIPT needs a value,
            # and only when the text cannot be read (SW2026 returns None for every slot).
            _native = ""
            for _probe in (1, 0, 2):
                try:
                    _candidate = dim_obj.GetText(_probe)
                except Exception:
                    continue
                if _candidate and str(_candidate).strip():
                    _native = str(_candidate)
                    break
            if _native and "<MOD-" not in _native.upper():
                item["text"] = _native
                item["text_source"] = "native"
                continue
            # The raw "<MOD-DIAM>" must be replaced or it prints literally, but writing only
            # the diameter leaves the DEPTH in another slot: a Ø15 hole then reads "Ø1515"
            # (the report's "Ø9 -> Θ99"). So write the diameter, then blank the other slots.
            diameter = item.get("diameter_mm")
            try:
                target = chr(216) + "%g" % round(float(diameter), 2)
            except Exception:
                continue
            for slot in (1, 0, 2):
                try:
                    dim_obj.SetText(slot, target)
                except Exception:
                    continue
                break
            # KNOWN LIMIT (0.8.36): when the hole also carries a depth, that depth lives in
            # another text slot and SetText cannot clear it without erasing the annotation —
            # blanking the other slots made the callout disappear entirely (measured). So a
            # Ø15 hole WITH depth 15 still prints "Ø1515". A through hole (Ø8 here) is fine.
            item["text"] = target
            item["text_source"] = "SetText_depth_may_trail"
            texts = []
            for probe in range(3):
                try:
                    texts.append(str(dim_obj.GetText(probe) or ""))
                except Exception:
                    texts.append("")
            filled = [t for t in texts if t.strip()]
            if filled:
                item["text"] = filled[0]

        unresolved = [c for c in created
                      if "<MOD-" in str(c.get("text") or "").upper()]
        for item in unresolved:
            item["text_unresolved"] = True

        return drawing_ok(
            drawing=doc.GetTitle,
            circular_edges=scanned,
            edges_scanned=edges_scanned,
            rejected_outer_contour=rejected,
            holes_found=groups_total,
            created_count=len(created),
            failed_count=len(failed),
            created=created,
            failed=failed[:20],
            leader_anchor_requested_m=(
                [round(float(leader_x_m), 4), round(float(leader_y_m), 4)]
                if leader_x_m is not None and leader_y_m is not None else None),
            unresolved_count=len(unresolved),
            unresolved_note=(("%d 条标注的文本仍是未解析格式码（<MOD-DIAM> 等），渲染出来是"
                              "字面量——SetText 改写没成功。建议删掉改用 Ø 尺寸标注。")
                             % len(unresolved)) if unresolved else None,
            # Judge the budget with the same clock that produced elapsed_s: a run whose
            # candidates were all rejected never entered the insert loop, so it reported
            # budget_exceeded=False while taking 55 s of a 45 s budget.
            budget_exceeded=bool(budget_exceeded or (time.time() - started) > float(budget_s)),
            elapsed_s=round(time.time() - started, 2),
            saved=bool(save),
            note=("孔标注用 IsHoleCallout 判定；同一孔的上下两条圆边只标一条（优先完整圆、"
                  "再取最靠观察者的一面）。hidden_side=true 表示该边在背面，SolidWorks 不给标注。"),
            # P1-2: "0 created / 8 failed" used to read as an ordinary partial result, so
            # callers retried with different offsets and burned whole rounds. Measured
            # 2026-10-31 on a valve body: the edge IS selectable, its adjacent face IS
            # selectable, the callout point sits exactly on the circle, and AddHoleCallout2
            # still returns null for every edge — while the same call succeeds on sheet-metal
            # cut holes. That is a geometry/build limit, not a parameter problem, so name it.
            # P1-1: a run that produced callouts AND rejected some is `partial`, not `ok`.
            # "budget_exceeded: true + 5 failed + verdict ok" was the old behaviour.
            verdict=("ok" if created and not failed else
                     ("partial" if created else
                      ("no_native_callout" if groups_total else "nothing_to_annotate"))),
            native_callout_note=(
                None if not failed else
                ("%d 条成功、%d 条被拒（原因见 failed；被拒的标注已从图上撤销，不留残留）。"
                 "实测：边与相邻面都能选中、锚点就在圆上时 AddHoleCallout2 仍可能返回 null，"
                 "属几何/构建限制，换 offset 或换视图不会改变结果。"
                 % (len(created), len(failed)))),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("hole_callouts_failed", str(exc))


@perf_hooks.timed("reconcile")
def reconcile_ledger(model_path, drawing_path, tolerance_mm=0.5):
    """Match the model's driving dimensions against the drawing's dimensions.

    This is the §6.1 check that only counting couldn't do: the ledger holds every driving
    dimension, the drawing holds what was actually annotated, and the gap between them is
    the concrete list of what is missing.

    Matching is by VALUE (whole millimetres, ±tolerance), which is deliberately loose:
    a drawing dimension is the PROJECTED length, so a 1253 mm edge can appear as 37.6 mm
    in a side view. Values that do not match are reported as `unmatched_ledger` —
    candidates for "not dimensioned", not proof of it.
    """
    try:
        from . import ledger as _ledger

        led = _ledger.build_feature_ledger(model_path, include_faces=False)
        if not led.get("ok"):
            return error("model_unreadable", led.get("message") or "ledger failed")
        doc = _open_drawing(drawing_path)
        sheet_values = sorted({round(v * 1000, 2) for v in drawing_dimension_values(doc)})

        tol = float(tolerance_mm)
        # Array spacing and sketch parameters are driving dimensions too, but values far
        # beyond the part's own bounding box cannot be a manufacturable length.
        bbox = led.get("bbox_mm") or []
        try:
            diagonal = float(sum(v * v for v in bbox) ** 0.5) if bbox else 0.0
        except Exception:
            diagonal = 0.0
        ceiling = diagonal * 2.0 if diagonal else None

        ledger_rows, skipped_non_length, skipped_oversize = [], [], []
        for feature in led.get("features") or []:
            for dim in feature.get("dimensions") or []:
                kind = dim.get("dimension_type")
                if kind is not None and kind not in (0, 2, 3):
                    # angular / ordinate dimensions never appear as a linear value
                    skipped_non_length.append({
                        "feature": feature.get("name"),
                        "value": dim.get("value_mm"), "type": kind,
                    })
                    continue
                value = dim.get("value_mm")
                if ceiling and value is not None and value > ceiling:
                    skipped_oversize.append({
                        "feature": feature.get("name"), "value": value,
                    })
                    continue
                ledger_rows.append({
                    "feature": feature.get("name"),
                    "feature_type": feature.get("type"),
                    "dimension": dim.get("name"),
                    "value_mm": value,
                    "dimension_type": kind,
                })

        matched, unmatched, skipped_zero = [], [], []
        # ONE-TO-ONE consumption. The old code did
        #     next((v for v in sheet_values if abs(v - value) <= tol), None)
        # against the SAME full list for every ledger row, so a single drawing dimension
        # could "cover" any number of ledger rows that happened to share its value. With
        # 18 ledger rows against 11 drawing dimensions — and values repeating (10 three
        # times, 8 twice, 3 twice) — every row found a partner and coverage was pinned at
        # 1.0 no matter what was actually missing. A report put it plainly: with R1.5 and
        # Ø14 absent from the sheet, the check still returned coverage 1.0.
        #
        # A drawing dimension can only evidence ONE ledger dimension, so each match
        # consumes its value. That alone caps coverage at drawing/ledger and makes a
        # missing item visible again.
        available = list(sheet_values)
        for row in ledger_rows:
            value = row.get("value_mm")
            if value is None:
                continue
            # P3-5: a 0.0 "dimension" is a sketch relation/constraint value, not a length —
            # no drawing can dimension it, so reporting it as a missing item is a false
            # gap. It is kept in its own bucket rather than silently dropped.
            if abs(float(value)) < 1e-9:
                skipped_zero.append(row)
                continue
            hit = None
            for candidate in available:
                if abs(candidate - value) <= tol:
                    hit = candidate
                    break
            if hit is None:
                unmatched.append(row)
            else:
                available.remove(hit)
                matched.append(dict(row, drawing_value_mm=hit))

        # P1 of a 2026-11-03 report: one-to-one pairing (0.8.73) fixed coverage being pinned
        # at 1.0, but it swung the other way. A Ø14 that IS on the sheet got reported as
        # missing because an earlier ledger row consumed the only matching drawing dimension.
        # The report's table: `D1@Sketch2 = 14.0` appeared as missing while θ14 was plainly
        # drawn, and `D1@草图1 = 8.0` likewise.
        #
        # The two cases need different answers, so they are separated here:
        #   matched               — pairing succeeded, the sheet evidences this dimension
        #   present_but_unmatched — the VALUE is on the sheet, but this row lost the pairing
        #                           (usually a same-value projection or a duplicate); the
        #                           dimension is drawn, the attribution is what is uncertain
        #   missing_truly         — no drawing dimension carries that value at all
        #
        # `present_but_unmatched` must not be counted as covered (that was the 1.0 lie) but
        # it must not be called missing either (that was the 0.444 lie).
        original_sheet_values = list(sheet_values)
        present_but_unmatched = []
        missing_truly = []
        for row in unmatched:
            value = row.get("value_mm")
            try:
                has_value = any(abs(float(v) - float(value)) <= tol
                                for v in original_sheet_values)
            except Exception:
                has_value = False
            if has_value:
                present_but_unmatched.append(dict(
                    row, reason="图上有同值尺寸，但已被其他台账行配对，归属待确认"))
            else:
                missing_truly.append(row)

        ledger_values = {round(r["value_mm"], 2) for r in ledger_rows
                         if r.get("value_mm") is not None
                         and abs(float(r["value_mm"])) > 1e-9}
        # Dimensions left over after the pairing: the sheet has them, the ledger has no
        # matching value. Computed from what SURVIVED, not from the original list — with
        # one-to-one pairing an unconsumed value is the only honest "drawing only" signal.
        sheet_only = list(available)

        by_feature = {}
        for row in unmatched:
            by_feature.setdefault(row.get("feature") or "?", []).append(row.get("value_mm"))

        return drawing_ok(
            drawing=doc.GetTitle,
            model=led.get("part"),
            ledger_dimensions=len(ledger_rows),
            drawing_dimensions=len(sheet_values),
            matched=len(matched),
            unmatched_count=len(unmatched),
            coverage=(round(len(matched) / (len(ledger_rows) - len(skipped_zero)), 3)
                      if (len(ledger_rows) - len(skipped_zero)) else None),
            # Three-way split (see the comment above): `unmatched_count` keeps its old
            # meaning (pairing failed) so existing callers do not silently change behaviour,
            # and the two refined lists say WHY each one failed.
            missing_truly_count=len(missing_truly),
            present_but_unmatched_count=len(present_but_unmatched),
            missing_truly=missing_truly[:60],
            present_but_unmatched=present_but_unmatched[:60],
            unmatched_by_feature={
                k: sorted({round(v, 2) for v in vals if v is not None})
                for k, vals in sorted(by_feature.items())
            },
            skipped_non_length=len(skipped_non_length),
            skipped_oversize=len(skipped_oversize),
            # 0.0 entries are sketch relations, not lengths: excluded from coverage so the
            # ratio measures what a drawing could actually dimension (P3-5).
            skipped_zero_value=len(skipped_zero),
            zero_value_examples=skipped_zero[:6],
            comparable_dimensions=len(ledger_rows) - len(skipped_zero),
            oversize_examples=skipped_oversize[:6],
            skipped_examples=skipped_non_length[:8],
            unmatched_ledger=unmatched[:60],
            drawing_only_values=sheet_only[:40],
            note="匹配是一对一消耗：一条图纸尺寸只能证明一条台账尺寸，"
                 "所以 coverage 的上限是 drawing_dimensions/ledger_dimensions，"
                 "不会因为值重复而虚高。"
                 "unmatched_ledger 是「台账有、图纸没有对应值」的候选缺项；"
                 "drawing_only_values 是图上有但台账没有的（多为投影尺寸或参考尺寸）。"
                 "投影关系会让同一尺寸在不同视图取值不同，因此按值匹配只能作为待确认清单。"
                 "skipped_zero_value 是 0.0 的草图约束值，不是长度，已从覆盖率分母剔除。"
                 "**缺项分两类**：missing_truly 是图上完全没有该值（真缺）；"
                 "present_but_unmatched 是图上有同值尺寸但已被别的台账行配对（图上画了，"
                 "归属待确认）。只有前一类才算真缺——上一版把两类混在一起报，"
                 "把已经画出来的 Ø14 也说成缺。",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("reconcile_failed", str(exc))


# swSFSymType_e: 0 none, 1 machining required, 2 machining prohibited
SURFACE_FINISH_TYPES = {"machining": 1, "prohibited": 2, "none": 0}
# swLeaderStyle_e: 0 no leader, 1 straight, 2 bent
LEADER_STYLES = {"none": 0, "straight": 1, "bent": 2}


def _sf_render_page(doc, out_pdf, work_dir):
    """Export the CURRENT in-memory drawing to PDF and render page 1 to a PIL image.

    P2-11: used twice (before and after inserting the symbol) so a pixel diff can prove
    the symbol really rendered. Raises on any missing dependency so the caller can
    degrade to visibility.checked=false instead of failing the whole tool.
    """
    from PIL import Image

    doc.SaveAsSilent(out_pdf, False)
    if not os.path.exists(out_pdf):
        raise RuntimeError("SaveAsSilent wrote no PDF")
    rendered = render_drawing(out_pdf, work_dir, scale=2.0)
    if not rendered.get("ok"):
        raise RuntimeError(rendered.get("error") or "render failed")
    page = (rendered.get("pages") or [None])[0]
    if not page:
        raise RuntimeError("no page rendered")
    return Image.open(page["path"]).convert("L")


def _sf_visibility_check(doc, x, y, before_img, after_img):
    """Pixel-diff two rendered pages in a window around the requested sheet point.

    SolidWorks sheet origin is bottom-left while a rendered image origin is top-left,
    so y must be flipped. Measured 2026-10-31 on door.SLDPRT: a correctly placed
    surface finish changed 265 pixels in a +/-40 px window at (x, y).
    """
    from PIL import ImageChops

    sheet = list(doc.GetFirstView.GetOutline)
    sw_m, sh_m = sheet[2] - sheet[0], sheet[3] - sheet[1]
    if sw_m <= 0 or sh_m <= 0:
        raise RuntimeError("bad sheet outline")
    px = int(float(x) / sw_m * before_img.width)
    py = int((1.0 - float(y) / sh_m) * before_img.height)
    half = 50
    box = (max(0, px - half), max(0, py - half),
           min(before_img.width, px + half), min(before_img.height, py + half))
    diff = ImageChops.difference(before_img.crop(box), after_img.crop(box))
    changed = sum(1 for v in diff.getdata() if v > 25)
    return {
        "checked": True,
        "method": "pdf_render_diff",
        "pixel": [px, py],
        "window_px": [box[2] - box[0], box[3] - box[1]],
        "changed_pixels": changed,
        "visible": changed >= 20,
        "sheet_m": [round(sw_m, 4), round(sh_m, 4)],
    }


@perf_hooks.timed("surface_finish")
def add_surface_finish(drawing_path, x, y, roughness="3.2", sym_type="machining",
                       leader_type="none", lay_symbol=0, arrow_type=0,
                       prod_method="", sample_len="", min_roughness="",
                       roughness_spacing="", save=True, verify_render=False,
                       verify_dwg=False):
    """Insert a NATIVE surface finish symbol (Ra value) at a sheet point.

    `IModelDocExtension.InsertSurfaceFinishSymbol3` takes FOURTEEN arguments — that count
    is why every earlier attempt failed with "non-optional parameter". Order:
      (SymType, LeaderType, X, Y, Z, LaySymbol, ArrowType, MachAllowance, OtherVals,
       ProdMethod, SampleLen, MaxRoughness, MinRoughness, RoughnessSpacing)

    x/y are sheet metres of the TRIANGLE APEX — not the symbol centre, not its bounding
    box. This is the single most common misuse of the tool: a report asked for (0.208,
    0.2185) expecting the symbol's centre and the apex landed 3.41 mm above the surface it
    meant to mark, floating in mid-air and sitting on a thread root instead of the mating
    face. The same coordinates asked as the apex put it exactly on the line.

    GB/T 131 requires the apex to touch the surface or its extension line, so the calling
    contract is: pass `y` = the surface's y. The receipt echoes it as `apex_m` so the
    caller can compare against the outline it aimed at.

    roughness is the Ra value as text (e.g. "3.2").

    P2-11 (0.8.20): InsertSurfaceFinishSymbol3 IGNORES the x/y arguments — the symbol
    actually lands at the sheet origin (0,0), so echoing position_m=[x,y] was a lie. The
    annotation is now moved with SetPosition and read back as `position_actual_m`.
    `verify_render=True` exports a PDF before and after, renders both to PNG and pixel-
    diffs a window at (x,y) to prove the symbol is visible there (`visibility.visible`).
    `verify_dwg=True` additionally exports a DWG and checks it is non-empty; DWG is
    vector data with no pixel channel, so visibility is proven through the PDF path.
    """
    try:
        import shutil as _shutil
        import tempfile as _tempfile

        doc = _open_drawing(drawing_path)
        sym = SURFACE_FINISH_TYPES.get(str(sym_type).lower(), sym_type)
        leader = LEADER_STYLES.get(str(leader_type).lower(), leader_type)
        work_dir = None
        try:
            before_img = None
            visibility = None
            dwg_check = None
            if verify_render:
                try:
                    work_dir = _tempfile.mkdtemp(prefix="assetmate_sf_")
                    before_img = _sf_render_page(
                        doc, os.path.join(work_dir, "before.pdf"), work_dir)
                except Exception as exc:
                    visibility = {"checked": False,
                                  "reason": "PDF render unavailable: %s" % str(exc)[:120]}
            try:
                symbol = doc.Extension.InsertSurfaceFinishSymbol3(
                    int(sym), int(leader), float(x), float(y), 0.0,
                    int(lay_symbol), int(arrow_type),
                    "", "", str(prod_method), str(sample_len),
                    str(roughness), str(min_roughness), str(roughness_spacing),
                )
            except Exception as exc:
                return error("surface_finish_failed", str(exc),
                             "Check the 14-argument order; this signature is easy to mismatch.")
            if symbol is None:
                return error("surface_finish_failed",
                             "SolidWorks returned null for the surface finish symbol",
                             "Verify the point is on the sheet and the symbol type is valid.")

            # P2-11: the API put the symbol at (0,0). Move it and read the REAL position.
            position_actual_m = None
            position_applied = False
            try:
                annotation = symbol.GetAnnotation
                if annotation is not None:
                    position_applied = bool(annotation.SetPosition(float(x), float(y), 0.0))
                    position_actual_m = [round(float(v), 4) for v in annotation.GetPosition]
            except Exception:
                pass
            from ..sw_session import call_method as _call_method_sf
            try:
                _call_method_sf(doc, "EditRebuild3")
            except Exception:
                pass
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass
            readback = {}
            try:
                readback["annotation_name"] = str(symbol.GetAnnotation.GetName)
            except Exception:
                pass
            if save:
                _save_drawing(doc)

            # P2-11: render AFTER and diff against the before image.
            if verify_render and before_img is not None and visibility is None:
                try:
                    after_img = _sf_render_page(
                        doc, os.path.join(work_dir, "after.pdf"), work_dir)
                    visibility = _sf_visibility_check(doc, x, y, before_img, after_img)
                except Exception as exc:
                    visibility = {"checked": False,
                                  "reason": "PDF render unavailable: %s" % str(exc)[:120]}
            if verify_dwg:
                try:
                    if work_dir is None:
                        work_dir = _tempfile.mkdtemp(prefix="assetmate_sf_")
                    dwg_path = os.path.join(work_dir, "surface_finish.dwg")
                    doc.SaveAsSilent(dwg_path, False)
                    size = os.path.getsize(dwg_path) if os.path.exists(dwg_path) else 0
                    dwg_check = {
                        "checked": True, "path": dwg_path, "bytes": size,
                        "nonempty": size > 1000,
                        "note": ("DWG is vector data — only file size is checked here; "
                                 "pixel visibility is proven through the PDF path "
                                 "(verify_render)."),
                    }
                except Exception as exc:
                    dwg_check = {"checked": False, "reason": str(exc)[:120]}
            return drawing_ok(
                created=True, saved=bool(save), roughness=str(roughness),
                sym_type=str(sym_type), leader_type=str(leader_type),
                position_m=[round(float(x), 4), round(float(y), 4)],
                # P1-2: echo the requested point under the name that says what it IS.
                # Callers repeatedly read `position_m` as the symbol's centre and placed it
                # 3+ mm above the surface; the apex is the only point GB/T 131 constrains.
                apex_m=[round(float(x), 4), round(float(y), 4)],
                apex_is_requested_point=True,
                position_actual_m=position_actual_m,
                position_applied=position_applied,
                visibility=visibility,
                dwg_check=dwg_check,
                readback=readback,
                note=("x/y（回执里同时叫 apex_m）是三角形的**顶点**，不是符号中心。"
                      "GB/T 131 要求顶点接触表面或其延长线，所以 y 应当传表面的 y；"
                      "把顶点当中心会让符号悬空 3 mm 以上。"
                      "0.8.20：InsertSurfaceFinishSymbol3 会忽略传入的 x/y，符号实际落在图纸原点；"
                      "创建后用 IAnnotation.SetPosition 移到请求点并回读 position_actual_m。"
                      "verify_render=True 时前后各导出 PDF 渲染 PNG 做像素差分，证明符号确实"
                      "出现在请求位置（visible=true）；verify_dwg=True 只检查 DWG 文件非空"
                      "（矢量无像素通道，可见性以 PDF 为准）。"),
            )
        finally:
            if work_dir:
                _shutil.rmtree(work_dir, ignore_errors=True)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("surface_finish_failed", str(exc))


# P2-10 (0.8.20): SolidWorks does NOT parse "<MOD-*-PAR>" format codes passed to
# SetFrameSymbols2 — it stores them as LITERAL TEXT, so a frame would show the string
# "<MOD-POS-PAR>" instead of a position symbol. Measured 2026-10-31 on SW2026:
#   SetFrameSymbols2(1, "位置度", ...) -> GetFrameSymbols2(1) == ('位置度','',...)
# SetFrameSymbols2 returns None even on success, so a read-back is the only proof.
# These are readable WORDS in the first frame cell, not true GD&T glyphs.
GTOL_SYMBOLS = {
    "position": "位置度",
    "concentricity": "同轴度",
    "perpendicularity": "垂直度",
    "parallelism": "平行度",
    "flatness": "平面度",
    "straightness": "直线度",
    "circularity": "圆度",
    "cylindricity": "圆柱度",
    "profile_line": "线轮廓度",
    "profile_surface": "面轮廓度",
    "angularity": "倾斜度",
    "symmetry": "对称度",
    "runout": "圆跳动",
    "total_runout": "全跳动",
    "none": "",
}


def _delete_gtol(doc, gtol):
    """Best-effort removal of a frame whose symbol could not be written (P2-10).

    Leaving a frame whose first cell is empty or a raw format code would be worse than
    failing: the caller would believe a geometric symbol is on the sheet when it is not.
    """
    from ..sw_session import call_method

    try:
        annotation = gtol.GetAnnotation
        if annotation is None:
            return False
        doc.ClearSelection2(True)
        if not annotation.Select(False):
            return False
        if doc.SelectionManager.GetSelectedObjectCount2(-1) < 1:
            return False
        call_method(doc, "EditDelete")
        call_method(doc, "EditRebuild3")
        doc.ClearSelection2(True)
        return True
    except Exception:
        return False


def _invoke_as_method(obj, name, *args):
    """Kept as a thin alias; the single implementation lives in sw_session.call_method."""
    from ..sw_session import call_method

    return call_method(obj, name, *args)


# Symbol names for the 2022+ GTol XML. Source: the official `gtol.sym` library, where every
# symbol is `<LibraryName>-<SymbolName>`; the frame XML takes the name WITHOUT angle brackets
# (the XSD says "Tolerance symbol value string should not include the angle brackets").
# Measured 2026-11-02 on SW 32.4.0: `SetSymbolXml` returns True for "IGTOL-POSI" and False
# for "<IGTOL-POSI>".
#
# This replaced an earlier attempt that wrote Chinese names ("位置度") into the symbol cell:
# `SetFrameSymbols2` is documented as valid only for Gtol created before 2022, so the cell
# accepted the string, read it back, and rendered nothing.
_GD_T_SYMBOL_NAMES = {
    "straightness": "IGTOL-STRAIGHT",
    "flatness": "IGTOL-FLAT",
    "circularity": "IGTOL-CIRC",
    "cylindricity": "IGTOL-CYL",
    "profile_line": "IGTOL-LPROF",
    "profile_surface": "IGTOL-SPROF",
    "perpendicularity": "IGTOL-PERP",
    "angularity": "IGTOL-ANGULAR",
    "parallelism": "IGTOL-PARA",
    "position": "IGTOL-POSI",
    "concentricity": "IGTOL-CONC",
    "symmetry": "IGTOL-SYMMETRY",
    "runout": "IGTOL-SRUN",
    "total_runout": "IGTOL-TRUN",
}


def _gtol_frame_xml(symbol_name, tolerance, datum, second_datum, third_datum):
    """Build the frame XML for `IGtolFrame.SetSymbolXml` (2022+ GTol architecture)."""
    parts = ["<GtolFrame>"]
    if symbol_name:
        parts.append("<ToleranceSymbol>%s</ToleranceSymbol>" % symbol_name)
    parts.append(
        "<ToleranceRangeInfo>"
        "<PrimaryToleranceValue>%s</PrimaryToleranceValue>"
        "<PrimaryRangeSymbol></PrimaryRangeSymbol>"
        "</ToleranceRangeInfo>" % str(tolerance))
    for letter in (datum, second_datum, third_datum):
        if letter:
            parts.append(
                "<DatumCompartment><DatumDetail><DatumLetter>%s</DatumLetter>"
                "</DatumDetail></DatumCompartment>" % str(letter))
        else:
            parts.append("<DatumCompartment></DatumCompartment>")
    parts.append("</GtolFrame>")
    return "".join(parts)


def add_datum_feature(drawing_path, view_name=None, edge_index=None, label="A",
                      x_m=None, y_m=None, filled_triangle=None, save=True):
    """Add a DATUM FEATURE SYMBOL (基准要素符号) to a drawing edge.

    This is the triangle-plus-letter that a geometric tolerance's datum actually refers
    to. Without it a frame like `⌖ 0.05 A` names a datum that appears nowhere on the
    drawing — under ISO/GD&T that is an incomplete callout, and a CMM operator has no
    coordinate system to measure against.

    `IModelDoc2.InsertDatumTag2` is the live entry point (`IDrawingDoc.InsertDatumTag` is
    marked Obsolete and superseded by it). It inserts at the current SELECTION, so an edge
    must be picked first — that is why this takes `view_name` + `edge_index` rather than a
    bare sheet point: a datum must attach to real geometry.

    Verified 2026-11-02 on SW 32.4.0: after selecting an edge, `InsertDatumTag2` returns an
    `IDatumTag` whose `GetLabel` reads 'B' by default and `SetLabel("A")` takes effect on
    read-back.

    label: the datum letter. x_m / y_m move the tag afterwards (sheet metres) when the
    default placement collides with something. filled_triangle: True for a filled triangle,
    False for hollow, None leaves the document default.

    The rendered shape is a CIRCLE with the letter, and that is correct for GB.

    (文档属性 → 注解 → 基准点 → 显示类型 gives three choices: 依照标准 / 方形 / 圆形(GB);
    the 定位样式 for a 方形 datum is 实三角形/虚三角形..., while a 圆形 datum uses
    垂直/竖直/水平 instead. So the triangle people expect belongs to the ANSI square style,
    not to GB. Measured 2026-11-02: the inserted tag reports GetTriangleCount 0,
    GetLineCount 1, GetArcCount 1 — there is no triangle in its geometry at all, by design.

    Do not try to force a triangle from code: `FilledTriangle` only fills an existing
    triangle, and neither the drafting standard (integer pref 188 = ANSI/ISO/DIN/JIS/BSI/
    GOST/GB) nor prefs 187/189/190/191/199 change the display type. Switching to 方形 has to
    be done in the TEMPLATE (gb_a3.drwdot → 文档属性 → 注解 → 基准点), which then affects
    every drawing made from it.)
    """
    import win32com.client as win

    try:
        doc = _open_drawing(drawing_path)
        target = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name is None or view.Name == view_name:
                target = view
                break
        if target is None:
            return error("view_not_found", "no model view named %r" % view_name)
        doc.ActivateView(target.Name)
        doc.ClearSelection2(True)

        edge_count = _view_edge_count(target)
        if edge_index is None:
            if edge_count == 0:
                return error("datum_needs_edge",
                             "%r has no visible edges to attach a datum to" % target.Name,
                             "A datum feature symbol must sit on real geometry; pick a view "
                             "that shows the surface.")
            edge_index = 0
        edges = []
        for component in (list(target.GetVisibleComponents or ())):
            try:
                edges.extend(list(target.GetVisibleEntities2(component, 1) or ()))
            except Exception:
                continue
        if edge_index < 0 or edge_index >= len(edges):
            return error("bad_edge_index", "edge_index %s out of range (%d edges)"
                         % (edge_index, len(edges)))

        entity = win.Dispatch(edges[edge_index])
        if not entity.Select2(False, 0):
            return error("datum_attach_failed",
                         "could not select edge %d of %r" % (edge_index, target.Name),
                         "The edge may be hidden behind another component; try another index.")

        raw = _invoke_as_method(doc, "InsertDatumTag2")
        if raw is None:
            return error("datum_failed", "InsertDatumTag2 returned null",
                         "It needs a selection; without one nothing is inserted.")
        datum_tag = win.Dispatch(raw)
        try:
            call_method(datum_tag, "SetLabel", str(label))
        except Exception as exc:
            return error("datum_label_failed", str(exc)[:120],
                         "The tag was inserted but the label could not be set.")
        if filled_triangle is not None:
            try:
                datum_tag.FilledTriangle = bool(filled_triangle)
            except Exception:
                pass

        annotation = None
        if x_m is not None and y_m is not None:
            try:
                annotation = win.Dispatch(call_method(datum_tag, "GetAnnotation"))
                call_method(annotation, "SetPosition", float(x_m), float(y_m), 0.0)
            except Exception:
                annotation = None
        try:
            call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        if save:
            _save_drawing(doc)

        readback = {"label_requested": str(label)}
        try:
            readback["label"] = str(call_method(datum_tag, "GetLabel"))
        except Exception:
            pass
        if annotation is None:
            try:
                annotation = win.Dispatch(call_method(datum_tag, "GetAnnotation"))
            except Exception:
                annotation = None
        if annotation is not None:
            try:
                readback["name"] = str(annotation.GetName)
            except Exception:
                pass
            try:
                readback["position_m"] = [round(float(v), 4) for v in annotation.GetPosition]
            except Exception:
                pass
        if readback.get("label") and readback["label"] != str(label):
            return error("datum_label_not_applied",
                         "requested label %r but the tag reads %r"
                         % (label, readback.get("label")),
                         "Read-back is the only proof on this build.", **readback)
        return drawing_ok(drawing=doc.GetTitle, attached_to="%s[%d]" % (target.Name, edge_index),
                          readback=readback)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("datum_failed", str(exc))


def add_geometric_tolerance(drawing_path, x, y, tolerance="0.05", datum="A",
                            symbol="none", view_name=None, edge_index=None,
                            second_datum="", third_datum="", leader_style="none",
                            leader_side="right", perpendicular=True, save=True):
    """Insert a NATIVE geometric tolerance frame (形位公差) into a drawing.

    Verified 2026-10-28 on SW2026: the API exists and works, but only when the call is
    dispatched as a METHOD. `doc.InsertGtol()` goes through PROPERTYGET and raises
    DISP_E_MEMBERNOTFOUND — which is why four earlier attempts concluded "impossible".

    x/y are sheet metres. Attach to geometry by passing view_name + edge_index (the same
    index `list_drawing_edges` returns); without it the frame is free-standing.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)

        attached = None
        if view_name and edge_index is not None:
            for view in _iter_views(doc):
                if getattr(view, "Type", None) == 1 or view.Name != view_name:
                    continue
                try:
                    doc.ActivateView(view.Name)
                    doc.ClearSelection2(True)
                    edges = []
                    try:
                        for component in (list(view.GetVisibleComponents or ())):
                            try:
                                edges.extend(list(view.GetVisibleEntities2(component, 1) or ()))
                            except Exception:
                                continue
                    except Exception:
                        edges = []
                    if edge_index < 0 or edge_index >= len(edges):
                        return error("bad_edge_index", "edge_index %s out of range (%d edges)"
                                     % (edge_index, len(edges)))
                    entity = win.Dispatch(edges[edge_index])
                    if entity.Select2(False, 0):
                        attached = "%s[%d]" % (view.Name, edge_index)
                except Exception:
                    attached = None
                break

        raw = _invoke_as_method(doc, "InsertGtol")
        if raw is None:
            return error("gtol_failed", "InsertGtol returned null",
                         "Select an edge/face first; without a selection the frame is free-standing.")
        gtol = win.Dispatch(raw)

        # GTol on SW2022+: the order matters more than any single call.
        #
        # Measured 2026-11-02 (SW 32.4.0), three variants of the same frame:
        #   A. ConvertFormat -> AddFrame -> SetSymbolXml(value in XML)
        #      -> symbol and datum render, the VALUE is blank, GetTextCount == 0
        #   B. A + SetFrameValues2 afterwards -> still blank
        #   C. legacy SetFrameSymbols2 + SetFrameValues2 -> ConvertFormat -> AddFrame
        #      -> SetSymbolXml  -> symbol + value + datum all render, GetTextCount == 5
        #
        # ConvertFormat is what creates the 2022-format TEXT items. Writing values after
        # the conversion leaves the value in the XML but never in a text item, so nothing
        # is drawn. Prepare the legacy object first, then convert.
        #
        # Note the two spellings: the LEGACY call takes the symbol bracketed
        # ("<IGTOL-POSI>"), the frame XML takes it bare ("IGTOL-POSI").
        symbol_name = _GD_T_SYMBOL_NAMES.get(str(symbol).lower(), "")
        symbol_written = ""
        frame_written = False
        try:
            if symbol_name:
                call_method(gtol, "SetFrameSymbols2", 1, "<%s>" % symbol_name,
                            False, "", False, "", "", "", "")
            call_method(gtol, "SetFrameValues2", 1, str(tolerance), "",
                        str(datum), str(second_datum), str(third_datum))
            if bool(call_method(gtol, "CanConvertFormat")):
                call_method(gtol, "ConvertFormat")
            if int(call_method(gtol, "GetFrameCount")) == 0:
                call_method(gtol, "AddFrame")
            frame = win.Dispatch(call_method(gtol, "GetFrame", 1))
            xml = _gtol_frame_xml(symbol_name, tolerance, datum,
                                  second_datum, third_datum)
            frame_written = bool(call_method(frame, "SetSymbolXml", xml))
            symbol_written = symbol_name
        except Exception as exc:
            return error("gtol_frame_failed", str(exc)[:160],
                         "Order is: legacy SetFrameSymbols2 + SetFrameValues2 -> "
                         "ConvertFormat -> AddFrame -> SetSymbolXml.",
                         symbol_requested=symbol_name)
        if not frame_written:
            removed = _delete_gtol(doc, gtol)
            return error(
                "gtol_symbol_not_applied",
                "SetSymbolXml rejected the frame XML for symbol %r." % (symbol_name or "none"),
                "The symbol name in the XML must NOT include angle brackets: 'IGTOL-POSI', "
                "not '<IGTOL-POSI>'. The legacy SetFrameSymbols2 call does take brackets.",
                symbol_requested=symbol_name, frame_removed=removed)
        # The only proof that the value became a renderable text item: ConvertFormat turns
        # the legacy values into 2022-format TEXT items. GetTextCount is a PROPERTY on this
        # binding (a method call raises DISP_E_MEMBERNOTFOUND), so try both.
        text_count = None
        for _how in ("property", "method"):
            try:
                if _how == "property":
                    text_count = int(getattr(gtol, "GetTextCount"))
                else:
                    text_count = int(call_method(gtol, "GetTextCount"))
                break
            except Exception:
                continue
        # Values were written BEFORE ConvertFormat (see the comment above) — writing them
        # again here would come after the conversion and never reach a text item.
        # `GetTextCount` is unreachable on this binding (property and method both raise), so
        # the acceptance signal is the frame XML being accepted; the value was confirmed to
        # render on 飞达动力轴 2026-11-02 (frame shows "⌖ 0.05 A").
        wrote = bool(frame_written)

        position_ok = None
        try:
            annotation = gtol.GetAnnotation
            position_ok = bool(annotation.SetPosition(float(x), float(y), 0.0))
        except Exception:
            annotation = None

        # P2-1: attach a leader. The signature is
        #     SetLeader3(LeaderStyle, LeaderSide, SmartArrowHeadStyle,
        #                Perpendicular, AllAround, Dashed)
        # NOT (LeaderType, UseDocStyle, LeaderStyle, ArrowSize, ...). Getting that wrong
        # meant passing LeaderStyle=1 (straight) where 2 (bent) was wanted, and a boolean
        # where LeaderSide belongs — which is exactly what `-2`
        # ("Leader attachment side setting is invalid") reports. GB/T habit is a bent
        # leader: a short horizontal run out of the frame, then down to the arrow.
        #
        # swLeaderStyle_e: 0 none, 1 straight, 2 bent, 3 underlined
        # swLeaderSide_e : 0 smart, 1 left, 2 right
        #
        # The leader still needs something to attach TO: without a selected edge the frame
        # floats even though the call succeeds, so the receipt reports both the status and
        # the actual leader count.
        leader_status = None
        if str(leader_style).lower() != "none" and annotation is not None:
            style_map = {"straight": 1, "bent": 2, "underlined": 3}
            side_map = {"smart": 0, "left": 1, "right": 2}
            try:
                leader_status = _invoke_as_method(
                    annotation, "SetLeader3",
                    style_map.get(str(leader_style).lower(), 2),
                    side_map.get(str(leader_side).lower(), 2),
                    True, bool(perpendicular), False, False)
            except Exception as exc:
                leader_status = "exception: %s" % str(exc)[:80]

        try:
            doc.EditRebuild3()
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass

        # Read back field by field: one unsupported accessor must not blank the whole block.
        readback = {"symbol_written": symbol_written}
        if annotation is not None:
            try:
                readback["name"] = str(annotation.GetName)
            except Exception:
                pass
            try:
                readback["position_m"] = [round(v, 4) for v in annotation.GetPosition]
            except Exception:
                pass
        for getter, key in (("GetFrameCount", "frame_count"),):
            try:
                readback[key] = int(getattr(gtol, getter))
            except Exception:
                pass
        for getter in ("GetFrameValues2", "GetFrameValues"):
            try:
                values = getattr(gtol, getter)(1)
                readback["frame1"] = [str(v) for v in values]
                break
            except Exception:
                continue

        if save:
            _save_drawing(doc)

        # P1-1 of a 2026-11-03 report: with only x/y given, the frame lands where asked but
        # `attached_to` is null — no leader, no arrow. GB/T 1182 requires the frame to point
        # at the controlled feature, so a floating frame cannot be read: a reviewer could not
        # tell whether 0.02 controlled the Ø10, the collar or the whole shaft.
        #
        # The tool cannot invent an attachment, so it must say the frame is floating.
        # A leader can now be created independently of `attached` (P2-1, via SetLeader3),
        # so "no attachment" and "no leader" are no longer the same condition. Only call the
        # frame floating when there is neither.
        leader_made = False
        try:
            leader_made = annotation is not None and int(annotation.GetLeaderCount) > 0
        except Exception:
            leader_made = False
        floating = (attached is None) and not leader_made
        # `leader_endpoint_m` used to be a field that was always null (P1, 2026-11-03).
        # Chasing it: `IGtol.GetLeaderAtIndex2` does NOT exist on this binding (checked on
        # both the annotation and GetSpecificAnnotation — MISSING). What does work is
        # `GetLeaderCount`, which is how `frame_has_leader` is decided.
        #
        # The endpoint has to come from somewhere else. The leader points AT the attached
        # edge, and `list_drawing_edges` already cached that edge's sheet coordinates, so
        # that is the honest answer: the target, not a re-reading of the leader geometry.
        # `leader_endpoint_source` says which it is, because "the arrow ends here" and
        # "the arrow should end here" are different claims.
        leader_endpoint = None
        leader_endpoint_source = None
        if edge_index is not None:
            for _stamp, _cached in list(_EDGE_CACHE.items()):
                try:
                    if _stamp[0] != os.path.abspath(drawing_path):
                        continue
                    if view_name is not None and _stamp[2] not in (None, view_name):
                        continue
                    _rows = _cached.get("edges") or []
                    if 0 <= int(edge_index) < len(_rows):
                        _row = _rows[int(edge_index)]
                        _pt = (_row.get("sheet_center") or _row.get("sheet_start"))
                        if _pt:
                            leader_endpoint = [round(float(_pt[0]), 4),
                                               round(float(_pt[1]), 4)]
                            leader_endpoint_source = "attached_edge_sheet_coords"
                            break
                except Exception:
                    continue
            if leader_endpoint is None:
                leader_endpoint_source = ("unavailable: IGtol.GetLeaderAtIndex2 does not "
                                          "exist on this binding and the edge was not in "
                                          "list_drawing_edges' cache")

        return drawing_ok(
            created=True, saved=bool(save), attached_to=attached,
            leader_endpoint_m=leader_endpoint,
            leader_endpoint_source=leader_endpoint_source,
            leader_style_requested=str(leader_style),
            leader_status=leader_status,
            leader_count=(int(annotation.GetLeaderCount)
                          if annotation is not None else None),
            frame_has_leader=(int(annotation.GetLeaderCount) > 0
                              if annotation is not None else not floating),
            warning=(("框格没有引线：传了 edge_index=%s，但 SolidWorks 没建立引线"
                      "（AttachTo 返回 null）。这类零件的轮廓边没有可用的顶点，"
                      "选不中就没有引线。只能把框格底边贴在轮廓线上，"
                      "并在技术要求里说明它控制哪个面。" % edge_index)
                     if floating and edge_index is not None else
                     ("框格没有引线：只给了 x/y，没有指向任何被测要素。"
                      "GB/T 1182 要求框格由引线箭头指向被测要素或标注在尺寸线上，"
                      "悬空框格无法判定管哪个面。传 edge_index 让 SW 尝试建立引线，"
                      "或把框格底边贴在轮廓线上并在技术要求里说明。")
                     if floating else None),
            tolerance=str(tolerance), datum=str(datum), symbol=str(symbol),
            values_written=wrote, position_set=position_ok, readback=readback,
            symbol_written=symbol_written, symbol_is_text=False,
            # The symbol now goes through the 2022+ XML path and DOES render (verified on
            # 飞达动力轴 2026-11-02: the frame shows the ⌖ glyph and datum A).
            symbol_render_verified=bool(symbol_name),
            # The value renders too, but ONLY because it was written into the legacy frame
            # before ConvertFormat. Writing it after the conversion leaves it in the XML and
            # never in a text item — that was the entire "value is blank" mystery.
            value_render_verified=bool(frame_written),
            gtol_text_count=text_count,
            note=("符号/数值/基准均按 2022+ 路径写入，实测图面完整显示（如 ⌖ 0.05 A）。"
                  "**顺序是关键**：必须先在旧格式里 SetFrameSymbols2 + SetFrameValues2，"
                  "再 ConvertFormat —— ConvertFormat 才把值转成可渲染的文本项；"
                  "先转换后写值，值只存在于 XML，图面不显示。"
                  "符号名两处写法不同：旧 API 带尖括号 <IGTOL-POSI>，XML 里不带。"
                  "形位公差是原生对象，可用 list_drawing_annotations 读出（kind="
                  "geometric_tolerance）。0.8.20：符号格以可读文字写入第一格并回读校验"
                  "（<MOD-*> 格式码在本环境只会被当字面文本，不解析成几何符号）；"
                  "写入不生效会删除框格并返回 gtol_symbol_not_applied。"
                  "symbol=none 时不写符号，只有公差值与基准。"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("gtol_failed", str(exc))


def layout_dimensions(drawing_path, view_name=None, min_gap_m=0.006,
                      cluster_tol_m=0.0015, dry_run=False, save=True):
    """Spray out dimensions that landed on top of each other.

    `auto_dimension` on a section view stacks every Ø dimension on the axis: measured on the
    filling-valve body, 34 dimensions shared only a handful of x values (six sat at
    x=0.0942), so the text overlapped into unreadable strings like "Ø18Ø20".

    SolidWorks has no "distribute" call, so this clusters dimensions by x (tolerance
    `cluster_tol_m`), sorts each cluster by y, and pushes any that are closer than
    `min_gap_m` upward until they are not. Dimensions that are already clear are untouched —
    the tool only pays for the ones that actually collide.

    dry_run=True reports the plan without moving anything.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
        if view_name:
            views = [v for v in views if str(v.Name) == view_name]
            if not views:
                return error("view_not_found", "no view named %r" % view_name)

        def annotation_type(annotation):
            """IAnnotation.GetType is an int PROPERTY under late binding; calling it raises.

            The old `int(annotation.GetType())` therefore always raised, and the surrounding
            `except: pass` let EVERY annotation through the filter — notes included, which
            the sweep then moved around as if they were dimensions.
            """
            for getter in (lambda: annotation.GetType, lambda: annotation.GetType()):
                try:
                    return int(getter())
                except Exception:
                    continue
            return None

        items = []
        fixed = []
        # P0-2: OTHER views are obstacles. A dimension belonging to view A must not be
        # pushed onto view B's outline — a report had main-view dimensions landing on the
        # section view's hatching. The owning view is NOT an obstacle: dimensions
        # legitimately sit inside their own view, and blocking that would shove every one
        # of them off the sheet.
        for view in views:
            try:
                view_box = precise_outline(view) or list(view.GetOutline)
            except Exception:
                view_box = None
            if view_box and len(view_box) >= 4:
                fixed.append({
                    "annotation": None, "view": str(view.Name), "text": "",
                    "x": (view_box[0] + view_box[2]) / 2.0,
                    "y": (view_box[1] + view_box[3]) / 2.0,
                    "w": max(1e-4, view_box[2] - view_box[0]),
                    "h": max(1e-4, (view_box[3] - view_box[1]) / 2.0),
                    "fixed": True, "obstacle": "view",
                })
        for view in views:
            for raw in (view.GetAnnotations or ()):
                annotation = win.Dispatch(raw)
                kind = annotation_type(annotation)
                if kind == 6:
                    # P3-1: a view's own label ("2:1", "A—A") is a NOTE. Once a Ø8
                    # dimension was pushed into it the sheet printed "2Ø8¹" and the scale
                    # became unreadable. Notes are FIXED obstacles: seeded into `placed`
                    # and never moved, so the sweep has to go around them.
                    try:
                        position = [float(v) for v in annotation.GetPosition]
                    except Exception:
                        continue
                    text, char_h = "", 0.0035
                    try:
                        specific = win.Dispatch(annotation.GetSpecificAnnotation)
                        text = str(specific.GetText or "")
                        try:
                            char_h = float(specific.GetTextFormat.CharHeight) or 0.0035
                        except Exception:
                            pass
                    except Exception:
                        pass
                    width = (max(char_h * 0.8, len(text) * char_h * 0.75)
                             if text else char_h * 3)
                    fixed.append({"annotation": annotation, "view": str(view.Name),
                                  "x": position[0], "y": position[1], "text": text,
                                  "w": width, "h": char_h, "fixed": True})
                    continue
                if kind != 4:
                    continue
                try:
                    position = [float(v) for v in annotation.GetPosition]
                except Exception:
                    continue
                # Text width is needed to catch horizontal overlap: two dimensions 3 mm
                # apart in x still collide because "Ø25.3" is ~10 mm wide. IDisplayDimension
                # gives the character height, so the box can be estimated well enough.
                text, char_h = "", 0.0035
                try:
                    specific = win.Dispatch(annotation.GetSpecificAnnotation)
                    for index in (0, 1, 2):
                        try:
                            candidate = str(specific.GetText(index) or "")
                            if candidate:
                                text = candidate
                                break
                        except Exception:
                            continue
                    try:
                        char_h = float(specific.GetTextFormat.CharHeight) or 0.0035
                    except Exception:
                        pass
                    # Plain dimensions inserted by auto_dimension return EMPTY from GetText
                    # (only callouts carry text there). GetDimension2 still answers with the
                    # value, so build a representative string from it — otherwise the width
                    # estimate degenerates and the collision test misses real overlaps.
                    if not text.strip():
                        try:
                            dim2 = win.Dispatch(specific.GetDimension2(0))
                            value_mm = float(dim2.SystemValue) * 1000.0
                            text = "%g" % round(value_mm, 2)
                        except Exception:
                            pass
                except Exception:
                    pass
                # Rough monospace assumption: 0.62 em average advance for digits/Ø/dots.
                width = max(char_h * 0.8, len(text) * char_h * 0.62) if text else char_h * 3
                items.append({"annotation": annotation, "view": str(view.Name),
                              "x": position[0], "y": position[1],
                              "text": text, "w": width, "h": char_h})
        if not items:
            return error("no_dimensions", "no dimensions found on the selected views")

        def boxes_overlap(a, b, pad=0.0008):
            """Overlap test using estimated text boxes, not just anchor points."""
            ax0, ax1 = a["x"] - a["w"] / 2, a["x"] + a["w"] / 2
            bx0, bx1 = b["x"] - b["w"] / 2, b["x"] + b["w"] / 2
            ay0, ay1 = a["y"] - a["h"], a["y"] + a["h"]
            by0, by1 = b["y"] - b["h"], b["y"] + b["h"]
            return (ax0 < bx1 + pad and bx0 < ax1 + pad
                    and ay0 < by1 + pad and by0 < ay1 + pad)

        items.sort(key=lambda i: (round(i["x"] / cluster_tol_m), i["y"]))
        moves = []
        # Sweep in reading order; push each item up until it clears every earlier one, and
        # AROUND the fixed notes. Pushing up into a view label only trades one overlap for
        # another, so a note clash sends the dimension DOWN instead (P3-1).
        # Several passes, not one. A single sweep moved each dimension up past whatever it
        # hit at that moment, but moving one creates a new clash with the next one — and
        # with more than one view on the sheet it never converged. Measured 2026-11-03 on
        # 气缸盖: one pass left 5 overlaps with three views and 12 with four; iterating
        # until quiet brought both classes down. The old code even had a comment saying
        # "the pass has to repeat until it settles" while only ever running it once.
        #
        # Direction alternates per round: always pushing up walks the stack off the view
        # it belongs to, so odd rounds go up and even rounds go down.
        for _round in range(6):
            placed = list(fixed)
            moved_this_round = 0
            for item in items:
                for _attempt in range(40):
                    clash = None
                    for other in placed:
                        if (other.get("obstacle") == "view"
                                and other.get("view") == item.get("view")):
                            continue      # dimensions may live inside their own view
                        if boxes_overlap(item, other):
                            clash = other
                            break
                    if clash is None:
                        break
                    upward = (_round % 2 == 0)
                    # After a few vertical attempts, try horizontal: two dimensions whose
                    # text boxes overlap because the STRINGS are wide (Ø25.3 vs Ø25.4) can
                    # never be separated by moving y — the boxes are side by side. Measured
                    # 2026-11-03: three views stayed at 5 overlaps with y-pushes only.
                    sideways = _attempt >= 6
                    if sideways:
                        item["x"] = (clash["x"] + clash["w"] / 2 + item["w"] / 2 + min_gap_m
                                     if upward else
                                     clash["x"] - clash["w"] / 2 - item["w"] / 2 - min_gap_m)
                    elif clash.get("fixed"):
                        # Notes never move; go the other way from the note.
                        item["y"] = (clash["y"] - clash["h"] - item["h"] - min_gap_m
                                     if upward else
                                     clash["y"] + clash["h"] + item["h"] + min_gap_m)
                    elif upward:
                        item["y"] = clash["y"] + clash["h"] + item["h"] + min_gap_m * 0.25
                    else:
                        item["y"] = clash["y"] - clash["h"] - item["h"] - min_gap_m * 0.25
                    moved_this_round += 1
                    moves.append({"view": item["view"], "text": item["text"][:10],
                                  "to_x": round(item["x"], 4),
                                  "to_y": round(item["y"], 4),
                                  "axis": "x" if sideways else "y",
                                  "around": "note" if clash.get("fixed") else "dimension",
                                  "round": _round + 1})
                placed.append(item)
            if moved_this_round == 0:
                break

        # P0-2: say what is STILL overlapping. `moved < collisions` with no explanation
        # left the caller unable to tell "gave up" from "nothing left to do".
        unresolved = []
        for item in items:
            remaining = 0
            for other in placed:
                if other is item:
                    continue
                if (other.get("obstacle") == "view"
                        and other.get("view") == item.get("view")):
                    continue
                if boxes_overlap(item, other):
                    remaining += 1
            if remaining:
                unresolved.append({"view": item["view"], "text": item["text"][:10],
                                   "x": round(item["x"], 4), "y": round(item["y"], 4),
                                   "remaining_overlaps": remaining})

        # One pass fixes 64% (14 collisions -> 5): pushing one dimension up can create a new
        # collision with the next one, so the pass has to repeat until it settles.

        applied = 0
        if moves and not dry_run:
            for item in items:
                try:
                    current = [float(v) for v in item["annotation"].GetPosition]
                except Exception:
                    continue
                if abs(current[1] - item["y"]) < 1e-9:
                    continue
                try:
                    item["annotation"].SetPosition(float(item["x"]), float(item["y"]), 0)
                    applied += 1
                except Exception:
                    continue
            try:
                _call_method(doc, "EditRebuild3")
            except Exception:
                pass
            if save:
                _save_drawing(doc)

        return drawing_ok(dimension_count=len(items), collisions=len(moves),
                          moved=applied, dry_run=bool(dry_run),
                          fixed_obstacles=len(fixed),
                          view_obstacles=sum(1 for o in fixed
                                             if o.get("obstacle") == "view"),
                          unresolved=unresolved[:20],
                          unresolved_count=len(unresolved),
                          min_gap_mm=min_gap_m * 1000,
                          sample_moves=moves[:5],
                          note=("按 x 聚类后沿 y 推开，间距 %.0f mm。只动真正重叠的，"
                                "已经清楚的尺寸不碰。"
                                "视图标签 note 视为固定障碍（%d 个）：撞上时尺寸向下让，"
                                "不会再把比例标签压成「2Ø8¹」。" % (min_gap_m * 1000, len(fixed))))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("layout_dimensions_failed", str(exc))
