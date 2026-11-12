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


def _find_sheet_format(paper, doc=None):
    """Locate the GB drawing FORMAT (`.slddrt`) for `paper`, or None.

    Report 2026-11-12 D1, measured root cause: `NewSheet4`'s `TemplateName` argument takes
    a drawing FORMAT, not a document TEMPLATE. Passing a `.drwdot` made SolidWorks record
    `...\\sheetformat\\a2` — a path with no extension and no file — so the new page got the
    right PAPER SIZE (the `paper` code still applies) and **no frame, no title block**,
    while the receipt said `template_applied: true`.

    A page that HAS a frame reports `GetTemplateName() == "a2 - gb.slddrt"`. So: prefer the
    format sitting next to the current sheet's own format (same machine, same convention),
    then fall back to scanning the installed templates.
    """
    want = str(paper or "A3").strip().lower()
    names = ("%s - gb.slddrt" % want, "%s.slddrt" % want)

    if doc is not None:
        try:
            current = str(doc.GetCurrentSheet.GetTemplateName or "")
        except Exception:
            current = ""
        if current.lower().endswith(".slddrt"):
            folder = os.path.dirname(current)
            for name in names:
                candidate = os.path.join(folder, name)
                if os.path.exists(candidate):
                    return candidate

    roots = [r"C:\ProgramData\SOLIDWORKS", r"C:\ProgramData\SolidWorks",
             r"C:\Program Files\SOLIDWORKS Corp"]
    for root in roots:
        for name in names:
            hits = glob.glob(os.path.join(root, "**", "sheetformat", name),
                             recursive=True)
            if hits:
                return sorted(hits)[0]
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
            perf_hooks.bump("redraw_calls")
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


#: Outline-read instrumentation (0.8.120). Deliberately NOT a cache.
#:
#: The planner's decision on PR3 was "do not cache by default, but keep the call count and
#: elapsed time so the threshold can be evaluated from data". These counters are that. The
#: thresholds below mark where caching stops being premature; crossing them is REPORTED,
#: never acted on, because the decision needs a human who knows the drawing.
OUTLINE_CALL_THRESHOLD = 100
OUTLINE_MS_THRESHOLD = 500.0

_OUTLINE_STATS = {"calls": 0, "ms": 0.0}


def reset_outline_stats():
    """The pipeline calls this before a run so the numbers describe THAT run only."""
    _OUTLINE_STATS["calls"] = 0
    _OUTLINE_STATS["ms"] = 0.0


def outline_stats():
    """Snapshot of outline reads: count, elapsed, and whether the cache threshold was crossed."""
    calls = _OUTLINE_STATS["calls"]
    ms = round(_OUTLINE_STATS["ms"], 1)
    return {
        "calls": calls,
        "ms": ms,
        "call_threshold": OUTLINE_CALL_THRESHOLD,
        "ms_threshold": OUTLINE_MS_THRESHOLD,
        "over_threshold": bool(calls > OUTLINE_CALL_THRESHOLD
                               or ms > OUTLINE_MS_THRESHOLD),
        "note": ("Outline reads are counted, not cached. Past %d calls or %.0f ms a per-run "
                 "cache starts to be worth its invalidation risk - see "
                 "docs/PR3-布局缓存-不建议实施说明.md, section 7."
                 % (OUTLINE_CALL_THRESHOLD, OUTLINE_MS_THRESHOLD)),
    }


def precise_outline(view):
    """Outline of a view inset by the constant 11.9 mm (see docs/com-quirks.md).

    Counted, not cached. The numbers surface through outline_stats() and in the pipeline
    receipt, so "should this be cached" is answered from the drawing in front of us rather
    than from a general assumption about how expensive GetOutline is.
    """
    import time as _time
    started = _time.perf_counter()
    try:
        return _precise_outline_impl(view)
    finally:
        _OUTLINE_STATS["calls"] += 1
        _OUTLINE_STATS["ms"] += (_time.perf_counter() - started) * 1000.0


def _precise_outline_impl(view):
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
def _ratio_text(scale):
    """`0.5` -> "1:2", `2.0` -> "2:1". None when the scale is unknown."""
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return None
    if abs(value) < 1e-12:
        return None
    if value < 1:
        return "1:%g" % round(1.0 / value, 4)
    return "%g:1" % round(value, 4)


def _corrected_scale_reason(reason, final_scale, adjustments):
    """Overwrite the scale fields that auto_fit invalidated.

    Report 2026-11-06 (P1): after the views stepped from 1:1 to 1:2, `scale_reason` still
    said `final_scale: 1.0` / `final_ratio: "1:1"` — and it is the field NAMED "final". A
    caller filling a process sheet from it would record a ratio the drawing does not have.
    """
    merged = dict(reason or {})
    if final_scale is None:
        return merged
    merged["final_scale"] = final_scale
    merged["final_ratio"] = _ratio_text(final_scale)
    if adjustments:
        merged["corrected_after_auto_fit"] = True
    return merged


def create_drawing(
    model_path,
    output_path=None,
    template=None,
    views=("front", "top", "isometric"),
    projection="first",
    scale=None,
    auto_fit=True,
    save=True,
    view_scales=None,
    paper="A3",
    view_positions=None,
    annotation_band_m=0.018,
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
    # Report 2026-11-07 (P0-3): the outermost `except` returned an error without closing
    # the drawing this call had just created. A probe measured "docs after: 17 (leaked 5)",
    # including a blank drawing at path '' that no path-based cleanup can ever find.
    # Bound before the `try` so the handler can reach it.
    _created_drawing = None
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
                    perf_hooks.bump("rebuild_calls")
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
        # `blocker` is read at the end of the function (`if blocker:` closes the drawing and
        # returns an error), but it was only assigned INSIDE this branch. With `save=False`
        # the name was never bound and the whole call raised UnboundLocalError — the third
        # such name in `create_drawing` after `_title_scale_synced` and `_final_scale`.
        # All three are the same defect: a receipt field that is only set on the success
        # path, then read unconditionally.
        blocker = None
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
        _created_drawing = drw
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

        # ── Room for dimensions (report 2026-11-08, P3-1b) ──────────────────────────────
        # The safe area says where DRAWING is allowed, not where a dimension fits. Packing
        # views flush to it left the isometric view 4.5 mm from the frame
        # (`min_margin_m: 0.0045`) with nowhere to put a leader.
        #
        # v0.8.262 tried this on `_place_views` and changed nothing measurable — that
        # function only REPACKS views after they exist. The box that decides the initial
        # positions is `safe_x0..safe_y1` right here, so the inset belongs here.
        # Report 2026-11-12 §5 (P1-2): `fits` was judged against the BAND-INSET box while
        # `measure_layout` judges against the raw safe area, so on 边肋连接件 one tool said
        # `fits:false / out_of_bounds:[视图2,视图4]` and the other said every view was
        # inside with `min_margin_m +0.010`. Same sheet, two verdicts, both "measured".
        # The band exists to reserve room for DIMENSIONS, not to make a view "outside the
        # sheet" — so keep the raw rectangle for `fits` and report the inset one separately.
        _base_rect = (safe_x0, safe_y0, safe_x1, safe_y1)
        _band = max(0.0, float(annotation_band_m))
        if _band:
            safe_x0 += _band
            safe_x1 -= _band
            safe_y0 += _band
            safe_y1 -= _band
            # Never invert the box on a small sheet: keep at least 40% of each dimension.
            if safe_x1 - safe_x0 < sheet_w * 0.4:
                _mid = (safe_x0 + safe_x1) / 2.0
                safe_x0 = _mid - sheet_w * 0.2
                safe_x1 = _mid + sheet_w * 0.2
            if safe_y1 - safe_y0 < sheet_h * 0.4:
                _mid = (safe_y0 + safe_y1) / 2.0
                safe_y0 = _mid - sheet_h * 0.2
                safe_y1 = _mid + sheet_h * 0.2
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
        # Projection convention. Report 2026-11-04 P2-3 found the sheet was a MIXTURE:
        # `top` sat below the front view (first angle) while `right` sat to the right of it
        # (third angle). A reader cannot tell which convention a drawing follows, and
        # GB/T 14692 requires first angle, so a mixed sheet is wrong for the shop.
        #
        #   first  (GB, default): the view is pushed AWAY from the observer —
        #                         top view BELOW, left view to the RIGHT
        #   third  (ISO/US):      the view stays where it was taken from —
        #                         top view ABOVE, right view to the right
        #
        # The choice is reported as `projection` so a sheet can be checked without reading
        # the picture. `isometric` keeps its corner: an axonometric view has no projection
        # side to respect.
        _proj = str(projection or "first").strip().lower()
        if _proj not in ("first", "third"):
            return error("bad_projection",
                         "projection must be 'first' or 'third', got %r" % (projection,),
                         "GB/T 14692 uses first angle; ISO/US drawings commonly use third.")
        # VERTICAL: this is the part of the convention a reader actually checks, and the one
        # that was mixed before.
        # The grid has exactly two rows, so the two views must occupy different ones:
        #   first  — front on top, top view BELOW it
        #   third  — top view on top, front BELOW it
        # The first attempt only swapped `top` and left `front` pinned to the upper row, which
        # put both views at y=0.2319 under third angle (measured, they overlap).
        if _proj == "first":
            front_y_for_views = front_y
            top_y_for_views, bottom_y_for_views = top_y, front_y
        else:
            front_y_for_views = top_y
            top_y_for_views, bottom_y_for_views = front_y, top_y

        # HORIZONTAL: a two-column grid only has ONE side-view slot, so the convention cannot
        # be expressed by mirroring columns — putting `right` in column 1 collided with the
        # front view (measured: both centred at x=0.1845). The side view therefore always
        # takes column 2. In first angle that slot holds the LEFT view (GB: 左视图放在右边),
        # so a caller who asks for `right` under `projection="first"` gets a placement the
        # convention does not endorse — reported via `projection_notes` rather than silently
        # accepted or silently moved.
        projection_notes = []
        if _proj == "first" and any(str(v).strip().lower() == "right" for v in views):
            projection_notes.append(
                "projection='first' 但请求了 right 视图：GB/T 14692 第一角里右侧位置应放"
                "**左视图**。当前把 right 放在第二列（几何有效但不符合第一角惯例）。"
                "需要严格合规就用 views=['front','top','left']。")

        positions = {
            "front": (col1, front_y_for_views), "back": (col1, front_y_for_views),
            "top": (col1, top_y_for_views), "bottom": (col1, bottom_y_for_views),
            "right": (col2, front_y_for_views), "left": (col2, front_y_for_views),
            "isometric": (col2, top_y),
        }

        # Explicit sheet centres win over the automatic packer. Callers were forced to
        # re-place views with raw COM (DISPATCH_PROPERTYPUT + VARIANT array) because a
        # 4-view sheet collapsed into one column; passing positions avoids that entirely.
        # `_place_views` packs by box height and has no notion of projection side, so it
        # would overwrite the mapping above and silently restore the mixed behaviour. When a
        # projection is stated, the mapping IS the layout: mark it explicit so the packer
        # leaves it alone. Scale selection still ran before this point, so auto-fit and the
        # step-down for oversized parts are unaffected.
        explicit_positions = projection is not None

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
                perf_hooks.bump("rebuild_calls")
            except Exception:
                pass
            try:
                doc.GraphicsRedraw2()
                perf_hooks.bump("redraw_calls")
            except Exception:
                pass

        if explicit_positions:
            # Caller placed the views; repacking would throw that away. But `fits` must
            # still be TRUE only if it is true.
            #
            # Report 2026-11-05 (§3.2 / this smoke run): this branch hard-coded
            # `"fits": True` without measuring anything, so a 3-column A3 layout whose
            # outlines summed to 439 mm on a 420 mm sheet reported fits=True, and
            # `drawing_audit` immediately flagged `views_overlap` between views 2 and 3.
            # The caller follows `fits`; the two must agree.
            _boxes = []
            for _v in view_objects:
                try:
                    _b = precise_outline(_v)
                except Exception:
                    _b = None
                # precise_outline returns [x1, y1, x2, y2] in sheet metres.
                if _b and len(_b) >= 4:
                    _boxes.append((str(getattr(_v, "Name", "?")), _b))
            def _outside(rect):
                return [_n for _n, _b in _boxes
                        if (_b[0] < rect[0] - 1e-6 or _b[1] < rect[1] - 1e-6
                            or _b[2] > rect[2] + 1e-6 or _b[3] > rect[3] + 1e-6)]

            # `fits` answers "is the view outside the SHEET" — the same question
            # `measure_layout` / `drawing_audit` / `delivery_check` answer. The band-inset
            # verdict is a different, stricter one and gets its own field (P1-2).
            _out_of_bounds = _outside(_base_rect)
            _out_of_band = _outside((safe_x0, safe_y0, safe_x1, safe_y1))
            _overlaps = []
            for _i in range(len(_boxes)):
                for _j in range(_i + 1, len(_boxes)):
                    _a, _c = _boxes[_i][1], _boxes[_j][1]
                    if min(_a[2], _c[2]) - max(_a[0], _c[0]) > 1e-6 and                             min(_a[3], _c[3]) - max(_a[1], _c[1]) > 1e-6:
                        _overlaps.append("%s<->%s" % (_boxes[_i][0], _boxes[_j][0]))
            # Report 2026-11-12 §5 (P1-2): a 2x2 layout reported `columns: 4` because every
            # VIEW was counted as a column, and `fits:false` disagreed with `measure_layout`
            # / `drawing_audit` / `delivery_check` on the same sheet. `columns` now counts
            # distinct left edges, and the receipt says this is a PREDICTION against the
            # annotation-band box — not the measurement `measure_layout` performs. Exposing
            # `bounds_m` lets the caller see which box produced the verdict instead of
            # trusting a bare boolean.
            _columns = sorted({round(_b[0], 4) for _, _b in _boxes})
            layout = {"fits": not _out_of_bounds and not _overlaps,
                      "columns": len(_columns) or len(views),
                      "explicit": True,
                      "measured": bool(_boxes),
                      "predicted": True,
                      # The rectangle `fits` was judged against — the RAW safe area, so it
                      # matches `measure_layout` (P1-2).
                      "bounds_m": [round(_base_rect[0], 4), round(_base_rect[1], 4),
                                   round(_base_rect[2], 4), round(_base_rect[3], 4)],
                      "out_of_bounds": _out_of_bounds,
                      "overlaps": _overlaps,
                      # Stricter, separate question: did the packing leave the
                      # `annotation_band_m` inset free for dimensions? A view can be
                      # `fits:true` and `fits_annotation_band:false` — that is a placement
                      # choice, not a defect.
                      "fits_annotation_band": not _out_of_band and not _overlaps,
                      "annotation_band_out_of_bounds": _out_of_band,
                      "annotation_band_bounds_m": [round(safe_x0, 4), round(safe_y0, 4),
                                                   round(safe_x1, 4), round(safe_y1, 4)]}
        else:
            layout = _place_views(view_objects, sheet_w, sheet_h, safety=safety)
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
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
                    perf_hooks.bump("redraw_calls")
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
            perf_hooks.bump("redraw_calls")
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
            perf_hooks.bump("redraw_calls")
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
            # Which convention this sheet follows. Reported so a drawing can be checked
            # against GB/T 14692 without reading the picture, and so a caller can tell that
            # the old mixed placement is gone rather than assume it.
            "projection": _proj,
            "projection_notes": projection_notes,
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
        # Report 2026-11-06 (P0, second attempt): the first version of this ran BEFORE the
        # save. `set_title_block` works by PATH — it reopens the file on disk, which at that
        # moment is either absent (new path) or the previous version (overwrite) — and the
        # subsequent `SaveAsSilent` wrote the in-memory document over it, discarding the
        # edit. The receipt still reported `title_scale_synced: {text: "1:2"}`, because the
        # return value was thrown away and `set_title_block` signals failure with an error
        # dict rather than an exception.
        #
        # Measured by a report: title block read back as 1:1 while the views were 1:2, with
        # `ok: true` and `title_scale_synced: {"from": 1.0, "to": 0.5, "text": "1:2"}`.
        #
        # Now: run AFTER the save, check `ok` and every `written[].ok`, and trigger whenever
        # the final scale is not 1:1 — not only when auto_fit changed it. Manually passing
        # `scale=0.5` produced no sync at all before.
        # MUST be bound before the branch: the sync only runs when the final scale is not
        # 1:1, so a drawing that stays 1:1 reached `result.update(...)` with this name
        # unbound and raised NameError — reported as `create_drawing_failed`, with no hint
        # that the scale was fine. Measured 2026-11-07 on a right+left pair that auto-fit
        # kept at 1:1; the front-view fixtures always stepped to 0.5 and never hit it.
        # Every branch writes a REASON, never a bare null. "Nothing to do" and "I could not
        # find out" are different answers, and a receipt that reports null for both lets a
        # caller assume the title block matches when it was never checked.
        _title_scale_synced = {"skipped": "save=False：没有保存，标题栏也就没有可同步的对象"}
        # Hoisted OUT of the `if saved` branch: `_final_scale` is also read by the receipt
        # below (`chosen_scale` / `scale_reason`), so `save=False` crashed on IT next, after
        # `_title_scale_synced` was fixed. Both names had the same defect; fixing one just
        # moved the error one line down. The views exist either way, so reading the scale
        # costs nothing here.
        _final_scale = None
        for _v in view_objects:
            try:
                _final_scale = float(_v.ScaleDecimal)
                break
            except Exception:
                continue
        if result.get("saved"):
            try:
                if _final_scale is None:
                    _title_scale_synced = {"skipped": "读不到视图比例（IView.ScaleDecimal）"}
                elif abs(_final_scale - 1.0) <= 1e-9:
                    # report 2026-11-07 (P0): this branch used to write NOTHING, then the
                    # receipt read the name unconditionally. 1:1 is auto_fit's default for
                    # small models, so every clean-install run crashed here.
                    _title_scale_synced = {"skipped": "视图是 1:1，标题栏无需改动",
                                           "scale": 1.0}
                if _final_scale is not None and abs(_final_scale - 1.0) > 1e-9:
                    # Keep `views[].scale` in step with reality. It was filled when the views
                    # were created, before auto_fit stepped them down, so it said 1.0 while
                    # every view was at 0.5. (2026-11-06 report, P1.)
                    for _item in created:
                        try:
                            _item["scale"] = _final_scale
                        except Exception:
                            continue
                    _ratio = ("1:%g" % round(1.0 / _final_scale, 4)) if _final_scale < 1 \
                        else ("%g:1" % round(_final_scale, 4))
                    _tb = set_title_block(out, scale=_ratio, apply=True)
                    _written = _tb.get("written") or []
                    _all_ok = bool(_written) and all(w.get("ok") for w in _written)
                    if _tb.get("ok") and _all_ok:
                        _title_scale_synced = {"from": 1.0, "to": _final_scale,
                                               "text": _ratio,
                                               "verified_by": "written[].ok"}
                    else:
                        # Do not claim a sync that did not happen.
                        _title_scale_synced = {
                            "text": _ratio,
                            "error": _tb.get("error") or (result.get("message")
                                                          if isinstance(result, dict) else None)
                            or "set_title_block did not confirm the write",
                            "written": _written}
            except Exception as _exc:                               # noqa: BLE001
                _title_scale_synced = {"error": str(_exc)[:100]}
        result.update(
            safe_area_used={
                "x": [round(safe_x0, 4), round(safe_x1, 4)],
                "y": [round(safe_y0, 4), round(safe_y1, 4)],
                "source": safety.get("source"),
            },
            box_used_m=[round(v, 4) for v in box] if box else None,
            auto_scale_adjustments=auto_scale_adjustments,
            title_scale_synced=_title_scale_synced,
            placement_deltas=placement_deltas,
            # Report 2026-11-06 (P1): four fields disagreed with the geometry after auto_fit
            # stepped the views down — `views[].scale` read 1.0 while the views were 0.5,
            # `chosen_scale` 1.0, `scale_reason.final_scale` 1.0 / `final_ratio` "1:1". A
            # caller filling a process sheet from `chosen_scale` got a ratio the drawing did
            # not have. All of them are corrected from the views themselves, at the end.
            chosen_scale=_final_scale if _final_scale is not None else chosen_scale,
            scale_reason=_corrected_scale_reason(scale_reason, _final_scale,
                                                 auto_scale_adjustments),
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
        # Report 2026-11-05 (P1): v0.8.90 moved the path guard BEFORE `_new_document()`, so
        # the common "target already open" case no longer leaks. But three blockers remain
        # AFTER the document is created — a second path_locked guard, `saveas_failed`, and
        # `save_not_written` — and they did:
        #
        #     result.update(saved=False, ...)
        #     return ok(**result)          # top-level ok True, document never closed
        #
        # Measured by pointing the output at a directory: `ok=True, saved=False,
        # save_error=save_not_written`, docs 35 -> 37 — a blank drawing left open at an
        # EMPTY path (invisible to both cleanup passes) plus its locked source part.
        #
        # A tool whose top-level `ok` is true while `saved` is false is the same lie the
        # rest of this project spent the day removing. If the file was not written, the
        # call failed, and whatever was created for it must not survive.
        if blocker:
            try:
                sw.CloseDoc(drw.GetTitle)
            except Exception:
                pass
            return error(blocker[0], blocker[1],
                         "The drawing document was closed; nothing was written to %s."
                         % out, **result)
        return ok(**result)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        # Close only what THIS call opened. A blank drawing carries an empty path, so
        # matching by path is useless — hold the object and close it by title.
        _cleaned, _cleanup_failed = [], []
        if _created_drawing is not None:
            try:
                from ..sw_session import get_sw as _get_sw
                _title = str(_created_drawing.GetTitle)
                _get_sw().CloseDoc(_title)
                _cleaned.append(_title)
            except Exception as _close_exc:                         # noqa: BLE001
                _cleanup_failed.append(str(_close_exc)[:80])
        return error("create_drawing_failed", str(exc),
                     ("本次调用创建的图纸已关闭：%s" % ", ".join(_cleaned)) if _cleaned
                     else ("图纸未能关闭：%s" % ", ".join(_cleanup_failed)) if _cleanup_failed
                     else "本次调用没有创建图纸，无需清理",
                     closed_on_failure=_cleaned,
                     cleanup_failed=_cleanup_failed or None)


@perf_hooks.timed("save")
def _save_drawing(doc):
    """Persist the drawing. Annotation steps otherwise live only in memory and the
    .SLDDRW handed to the user has views but none of the dimensions."""
    try:
        doc.SaveSilent
        return True
    except Exception:
        return False


def _active_sheet_views(doc):
    """Model views on the ACTIVE sheet only.

    Third occurrence of the same mistake (2026-11-07): `_iter_views(doc)` walks every view
    in the DOCUMENT, so a two-sheet drawing reported the same view list for both pages.
    That made the empty-sheet guard pass on a page that had nothing on it, because the
    other page's view was still in the list.

    `IView` has no `Sheet` property; `ISheet.GetViews` is the per-sheet reader.
    """
    try:
        sheet = doc.GetCurrentSheet
        raw = list(sheet.GetViews or ())
    except Exception:
        return []
    views = []
    for item in raw:
        try:
            import win32com.client as _win
            view = _win.Dispatch(item)
            if getattr(view, "Type", None) == 1:
                continue
            views.append(view)
        except Exception:
            continue
    return views


def _activate_sheet_if_given(doc, sheet):
    """Switch to `sheet` (name or 1-based index) before a write. Returns (name, error).

    Report 2026-11-07 (C3): every write tool edits whatever page is current, so
    "dimension page 2" only worked if the caller remembered to `activate_sheet` first.
    Passing `sheet=` makes the intent part of the call instead of a state the caller has to
    maintain.

    `sheet=None` means "do not touch the active page" — the old behaviour, still the
    default so nothing that worked before changes meaning.
    """
    if sheet is None:
        return None, None
    try:
        names = [str(n) for n in (doc.GetSheetNames or ())]
    except Exception as exc:                                        # noqa: BLE001
        return None, error("sheet_lookup_failed", str(exc)[:120])
    if not names:
        return None, error("no_sheets", "the drawing reports zero sheets")
    target = None
    # Report 2026-11-12 §5 (P1-1): the MCP schema declares `string | integer` and Pydantic
    # coerced `1` to `"1"` before it arrived, so the int branch never ran and a perfectly
    # valid index failed with `no sheet named '1'`. Resolve by NAME first (a sheet really
    # named "1" must win), then fall back to a digit string as a 1-based index.
    _as_index = None
    if isinstance(sheet, int) and not isinstance(sheet, bool):
        _as_index = sheet
    elif str(sheet) not in names and str(sheet).strip().isdigit():
        _as_index = int(str(sheet).strip())
    if str(sheet) in names:
        target = str(sheet)
    elif _as_index is not None:
        try:
            target = names[_as_index - 1]
        except Exception:
            return None, error("sheet_not_found",
                               "index %r is outside 1..%d" % (_as_index, len(names)),
                               "existing: %s" % ", ".join(names), sheets=names)
    else:
        return None, error("sheet_not_found", "no sheet named %r" % sheet,
                           "existing: %s" % ", ".join(names), sheets=names)
    try:
        ok = bool(doc.ActivateSheet(target))
    except Exception as exc:                                        # noqa: BLE001
        return None, error("activate_sheet_failed", str(exc)[:120], sheets=names)
    # READ BACK. Report 2026-11-07 (P0-2): on SW2024 `ActivateSheet("图纸2")` returns True
    # while `GetCurrentSheet` still answers 图纸1, so every write tool kept editing page 1
    # and reported `ok: true`. The return value alone cannot be trusted; the only evidence
    # that the page changed is what the document says afterwards.
    current = None
    try:
        current = str(doc.GetCurrentSheet.GetName)
    except Exception:
        current = None
    if not ok or (current is not None and current != target):
        return None, error(
            "sheet_activation_not_effective",
            "请求切到 %r，但当前页仍是 %r" % (target, current),
            ("SolidWorks 报告 ActivateSheet 成功，实际没有切过去。"
             "继续写入会改到错误的图纸页，所以这次调用被拒绝。"
             "已知 SW2024 存在该行为。" if ok else
             "ActivateSheet 返回 False。"),
            requested=target, current=current, sheets=names,
            sw_version=_sw_version_string())
    return target, None


def _sw_version_string():
    """SolidWorks version as a string, for receipts that may depend on it."""
    try:
        from ..sw_session import get_sw
        app = get_sw()
        return str(getattr(app, "RevisionNumber", "") or "") or None
    except Exception:
        return None


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
    """Export an existing .SLDDRW to PDF / DWG / DXF. PDF comes from a drawing only.

    **What is actually known about exporting mid-pipeline (corrected 2026-11-08).**

    I had written that "`SaveAsSilent` corrupts the live document — export is
    end-of-pipeline only". A report re-ran that scenario and could NOT reproduce it, and
    this module's own `batch_export` docstring says the opposite ("behaves as an export and
    leaves the document path unchanged").

    The evidence, separated:

    * `add_note` AFTER an export — **works**. The report wrote notes before and after an
      export and both landed (`annotation_count` 10 -> 11), with `GetPathName` still
      pointing at the `.SLDDRW`.
    * `add_center_marks` AFTER an export — **failed once in my test**
      (`ok:false, views_marked=None`). That was in a build where I had just added a
      before/after render inside the same function; the failure is at least as likely to
      have been my own code as the export.
    * `batch_export`'s docstring — says `SaveAsSilent` leaves the path unchanged.

    So: **export-last is a convention, not a proven constraint.** Do not cite this
    function as evidence that mid-pipeline export breaks a document. If a future failure
    appears right after an export, test `add_note` as a control before blaming the export.
    """
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
    """Structural audit, PER SHEET: views, scales, blank views, counts, out-of-frame.

    Evidence only — it does not judge whether the chosen views or tolerances are
    manufacturable. Blank views and missing dimensions are the checks that catch
    silent failures.

    Report 2026-11-07 (C4): a two-sheet drawing reported `sheet_count: 2` but a single
    `sheet_size_m` (the first sheet's), and judged EVERY view against that one frame. On a
    document whose page 2 is Letter while page 1 is A2, page 1's perfectly placed view was
    flagged `view_outside_sheet`, because Letter is smaller than where the view sits.

    So the audit now walks each sheet, uses THAT sheet's outline, and returns
    `sheets: [{name, size_m, view_count, dimensions, annotations, views[], warnings[]}]`.
    Overlap is only checked WITHIN a sheet — two views on different pages cannot overlap,
    and the old document-wide loop would have compared their coordinates blindly.
    """
    try:
        doc = _open_drawing(drawing_path)
        names = [str(n) for n in (doc.GetSheetNames or ())]
        try:
            original_active = str(doc.GetCurrentSheet.GetName)
        except Exception:
            original_active = None

        sheet_rows, all_views, warnings, boxes_by_sheet = [], [], [], []
        total_dims = 0
        sheet_annotations = None

        for sheet_index, sheet_name in enumerate(names, start=1):
            try:
                if not doc.ActivateSheet(sheet_name):
                    continue
            except Exception:
                continue
            try:
                sheet_obj = doc.GetCurrentSheet
            except Exception:
                continue
            # `ISheet.GetOutline` does NOT exist (measured 2026-11-07:
            # "<unknown>.GetOutline"). The sheet's frame comes from its SHEET VIEW
            # (`IView` with Type == 1), which is one of `sheet.GetViews`. The size comes
            # from `ISheet.GetProperties`, whose last two entries are width/height in
            # metres — `GetSize` is unusable because it wants two byref parameters.
            size_m, frame = None, None
            try:
                props = list(sheet_obj.GetProperties or ())
                if len(props) >= 2:
                    size_m = [round(float(props[-2]), 5), round(float(props[-1]), 5)]
            except Exception:
                pass

            views_here = []
            try:
                raw = list(sheet_obj.GetViews or ())
            except Exception:
                raw = []
            for item in raw:
                try:
                    import win32com.client as _win0
                    _v0 = _win0.Dispatch(item)
                    if getattr(_v0, "Type", None) != 1:
                        continue
                    _o = list(_v0.GetOutline or ())
                    if len(_o) >= 4:
                        frame = _o
                    break
                except Exception:
                    continue
            sheet_warnings = []
            local_boxes = []
            dims_here = 0
            for item in raw:
                try:
                    import win32com.client as _win
                    view = _win.Dispatch(item)
                except Exception:
                    continue
                if getattr(view, "Type", None) == 1:
                    # The sheet view carries the sheet-level annotation count.
                    try:
                        sheet_annotations = int(view.GetAnnotationCount)
                    except Exception:
                        pass
                    continue
                try:
                    name = str(view.Name)
                except Exception:
                    continue
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
                    dims_here += dims
                pbox = precise_outline(view)
                row = {
                    "name": name,
                    "sheet": sheet_name,
                    "sheet_index": sheet_index,
                    "scale": scale,
                    "width_m": round(width, 4) if width is not None else None,
                    "height_m": round(height, 4) if height is not None else None,
                    "precise_width_m": round(pbox[2] - pbox[0], 4) if pbox else None,
                    "precise_height_m": round(pbox[3] - pbox[1], 4) if pbox else None,
                    "dimensions": dims,
                    "annotations": notes,
                }
                views_here.append(row)
                all_views.append(row)
                if width is not None and (width < 0.001 or height < 0.001):
                    sheet_warnings.append({"view": name, "issue": "blank_view",
                                           "sheet": sheet_name})

                check_box = pbox
                if check_box is None:
                    try:
                        check_box = list(view.GetOutline)
                    except Exception:
                        check_box = None
                if check_box:
                    local_boxes.append((name, check_box))
                # Out-of-frame uses THIS sheet's outline. Comparing a page-2 view against
                # page 1's frame is what produced the false `view_outside_sheet`.
                if frame and check_box:
                    if (check_box[0] < frame[0] or check_box[1] < frame[1]
                            or check_box[2] > frame[2] or check_box[3] > frame[3]):
                        sheet_warnings.append({
                            "view": name, "issue": "view_outside_sheet",
                            "sheet": sheet_name,
                            "hint": "reduce scale or move the view",
                        })

            # Overlap is meaningful only within one sheet.
            for i in range(len(local_boxes)):
                for j in range(i + 1, len(local_boxes)):
                    a, b = local_boxes[i][1], local_boxes[j][1]
                    if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                        sheet_warnings.append({
                            "view": "%s <-> %s" % (local_boxes[i][0], local_boxes[j][0]),
                            "issue": "views_overlap",
                            "sheet": sheet_name,
                            "hint": "reduce scale or use fewer views per sheet",
                        })

            if not views_here:
                sheet_warnings.append({"view": None, "issue": "no_model_views",
                                       "sheet": sheet_name})
            elif dims_here == 0:
                sheet_warnings.append({
                    "view": None, "issue": "no_dimensions_anywhere",
                    "sheet": sheet_name,
                    "hint": "this sheet carries no dimensions at all",
                })

            total_dims += dims_here
            warnings.extend(sheet_warnings)
            sheet_rows.append({
                "index": sheet_index,
                "name": sheet_name,
                "size_m": size_m,
                "view_count": len(views_here),
                "dimensions": dims_here,
                "views": views_here,
                "warnings": sheet_warnings,
                "active": sheet_name == original_active,
            })

        if original_active:
            try:
                doc.ActivateSheet(original_active)
            except Exception:
                pass

        return ok(
            drawing=doc.GetTitle,
            path=doc.GetPathName,
            sheet_count=len(names),
            # Kept for callers that only read the flat field; it is the FIRST sheet's size,
            # and `sheets[].size_m` is the authoritative per-page value.
            sheet_size_m=(sheet_rows[0]["size_m"] if sheet_rows else None),
            view_count=len(all_views),
            total_dimensions=total_dims,
            views=all_views,
            sheets=sheet_rows,
            sheet_annotations=sheet_annotations,
            warnings=warnings,
            note=("Evidence only: view choice, tolerances and manufacturability are not "
                  "judged here. Per-sheet results are in `sheets`; `sheet_size_m` at the "
                  "top level is the FIRST sheet only. A view's `annotations` count may "
                  "include sheet-level notes (技术要求) — `sheet_annotations` reports the "
                  "sheet's own count separately."),
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
    # Repair at the read: mangled names break every downstream comparison, not just the
    # one place that used to call fix_mojibake (2026-11-06 report, fourth round).
    try:
        from ..sw_session import fix_mojibake as _fix
    except Exception:
        _fix = None
    for attr in ("FullName", "Name"):
        try:
            value = str(getattr(dim, attr))
        except Exception:
            continue
        if value:
            return _fix(value) if _fix is not None else value
    return None


@perf_hooks.timed("dim_dedupe")
def _dedupe_inserted_dimensions(doc, before_names, win, prefer_view=None):
    """Delete duplicate driving dimensions among the annotations JUST inserted (P1-6).

    SolidWorks' own `eliminate_duplicates` flag on InsertModelAnnotations3 did not stop the
    same D1@草图1 from landing once per view (56 dims across 4 views). Keep ONE occurrence
    of each model full name and delete the rest. Only annotations whose sheet name was
    absent before the insert are eligible, so dimensions the user already placed are never
    touched. Returns (duplicate_groups, removed_names, failures).

    `prefer_view` decides WHICH copy survives. Report 2026-11-06 (P1, B3): the default
    "keep the first" meant the main view always won, so a section view — created later, and
    the only place a depth dimension can live — had its copies deleted and ended up with
    zero dimensions. Deletion order is insertion order, and insertion order is view creation
    order, which has nothing to do with where a dimension belongs.
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
                groups.setdefault(full, []).append(
                    (sheet_name, annotation, str(getattr(view, "Name", ""))))
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    removed, failed = [], []
    for full, entries in duplicates.items():
        if prefer_view:
            # Keep the copy in the preferred view (first match); fall back to the first
            # entry when that view has none.
            preferred = [e for e in entries if e[2] == prefer_view]
            if preferred:
                winner = preferred[0]
                ordered = [e for e in entries if e is not winner]
            else:
                ordered = entries[1:]
        else:
            ordered = entries[1:]
        for sheet_name, annotation, _view_name in ordered:
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
            perf_hooks.bump("rebuild_calls")
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
            perf_hooks.bump("rebuild_calls")
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
             "skip_no_position": 0, "skip_no_owner": 0, "buckets": 0, "per_side": {},
             # 0.8.316 (report 2026-11-12 D3): anges are deliberately NOT pushed outside.
             "skipped_angular": []}
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

            # ── 0.8.316 (report 2026-11-12 D3): never push an ANGLE outward ────────────
            # An angular dimension's leader arcs around its vertex at that angle's own
            # radius. Moving its text outward STRETCHES that arc, and on a large radius the
            # arc crosses the sheet frame — the "big arc + slanted leader entering from
            # outside the frame" defect that survived three releases. SolidWorks' own
            # placement keeps the arc next to the geometry, which is strictly safer; an
            # angular dimension that is not moved cannot be moved outside.
            try:
                if _is_angular_dimension_type(
                        _annotation_dimension_type(annotation, win)):
                    debug["skipped_angular"].append(
                        {"name": name,
                         "position_m": [round(float(position[0]), 4),
                                        round(float(position[1]), 4)]})
                    continue
            except Exception:
                pass

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
                # Report 2026-11-06 (P1, B2): this used to be "sheet minus 10 mm", which on
                # A3 gives [0.010, 0.410] x [0.010, 0.287]. The real usable area inside the
                # frame is [0.0588, 0.416] x [0.065, 0.293] — the bottom edge of the TITLE
                # BLOCK sits at 0.065. So eleven stacked dimensions were pushed down to
                # y = 0.0595, which "fits" by the old test and prints on top of the title
                # block.
                #
                # Use the template's actual interior. Fall back to the old inset only if the
                # template cannot be read.
                _safe = None
                try:
                    _m = template_safe_area(candidate, frame[2] - frame[0],
                                            frame[3] - frame[1])
                    if _m:
                        safe = (frame[0] + _m.get("left", 0.0),
                                frame[1] + _m.get("bottom", 0.0),
                                frame[2] - _m.get("right", 0.0),
                                frame[3] - _m.get("top", 0.0))
                        _safe = safe
                except Exception:
                    _safe = None
                if _safe is None:
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
            # Clamp the PERPENDICULAR axis too. `place_outside` used to move only the
            # stacking direction and keep the other coordinate untouched, on the assumption
            # that SolidWorks had placed the dimension somewhere sensible along it. It has
            # not: measured on 接槽, an 8.6 mm dimension arrived at y = -0.176 — 176 mm BELOW
            # a 297 mm sheet — and pushing it to the right side of its view preserved that y,
            # so the "fixed" dimension still printed outside the frame.
            #
            # A dimension is only placed when it is on the sheet in BOTH axes.
            cross = position[1] if side in ("left", "right") else position[0]
            if safe is not None:
                if side in ("left", "right"):
                    cross = min(max(cross, safe[1]), safe[3])
                else:
                    cross = min(max(cross, safe[0]), safe[2])
            if side == "left":
                target = (box[0] - offset, cross)
            elif side == "right":
                target = (box[2] + offset, cross)
            elif side == "bottom":
                target = (cross, box[1] - offset)
            else:
                target = (cross, box[3] + offset)
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
def clamp_dimensions_into_safe_area(drawing_path, inset_m=0.010, step_m=0.014):
    """Pull any dimension that sits off the sheet back inside, stacked along the bottom.

    Report 2026-11-04: eight reference dimensions (RD1..RD8) came out of InsertModelItems at
    y = -0.068 .. -0.200 on a 297 mm sheet — 68 to 200 mm BELOW the paper. `place_outside`
    never sees them (they are not in its buckets), and the perpendicular clamp it applies
    only covers what it does push.

    Standalone on purpose. Three earlier attempts embedded this inside `auto_dimension` and
    all three missed for the same class of reason: the code ran before `place_outside` had
    finished, or the anchor was matched at the wrong depth. A function called explicitly at
    the END of the pipeline cannot be executed too early.

    Stacking: dimensions are spread by `step_m` along the bottom margin. Snapping them all
    to one y puts nine dimensions in an unreadable pile — only marginally better than being
    off-sheet.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        frame = list(doc.GetFirstView.GetOutline or ())
        if len(frame) < 4 or (frame[2] - frame[0]) <= 0.05:
            return drawing_ok(clamped=[], clamped_count=0,
                              note="sheet frame unavailable; nothing to clamp against")
        lo_x, lo_y = frame[0] + inset_m, frame[1] + inset_m
        hi_x, hi_y = frame[2] - inset_m, frame[3] - inset_m
        # Report 2026-11-06 (P2): the function is named `..._into_safe_area` and its
        # criterion was the SHEET, so three dimensions at x = 0.0308 / 0.0468 / 0.0388
        # (frame interior starts at 0.0588) were reported as "no dimension was off the
        # sheet" while printing outside the frame. Use the template's interior; the inset
        # stays only as a fallback when the template cannot be read.
        _safe_source = "sheet_inset"
        try:
            _m = template_safe_area(doc.GetFirstView, frame[2] - frame[0], frame[3] - frame[1])
            if _m:
                lo_x, lo_y = frame[0] + _m.get("left", inset_m), frame[1] + _m.get("bottom", inset_m)
                hi_x, hi_y = frame[2] - _m.get("right", inset_m), frame[3] - _m.get("top", inset_m)
                _safe_source = _m.get("source") or "template"
        except Exception:
            pass

        offenders = []
        scanned = 0
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
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
                    if int(ann.GetType) != 4:
                        continue
                    pos = [float(v) for v in ann.GetPosition]
                    name = str(ann.GetName)
                except Exception:
                    continue
                scanned += 1
                if lo_x <= pos[0] <= hi_x and lo_y <= pos[1] <= hi_y:
                    continue
                offenders.append((ann, name, pos))

        offenders.sort(key=lambda entry: entry[2][0])
        clamped = []
        for i, (ann, name, pos) in enumerate(offenders):
            nx = min(max(pos[0], lo_x), hi_x)
            ny = min(lo_y + i * step_m, hi_y)
            try:
                ann.SetPosition(nx, ny, 0.0)
                clamped.append({"name": name,
                                "from_m": [round(pos[0], 4), round(pos[1], 4)],
                                "to_m": [round(nx, 4), round(ny, 4)]})
            except Exception as exc:
                clamped.append({"name": name, "error": str(exc)[:80]})
        if clamped:
            try:
                _call_method(doc, "EditRebuild3")
            except Exception:
                pass
            try:
                doc.GraphicsRedraw2()
                perf_hooks.bump("redraw_calls")
            except Exception:
                pass
            _save_drawing(doc)
        return drawing_ok(clamped=clamped, clamped_count=len(clamped),
                          safe_area_source=_safe_source,
                          safe_area_used={"x": [round(lo_x, 4), round(hi_x, 4)],
                                          "y": [round(lo_y, 4), round(hi_y, 4)]},
                          dimensions_scanned=scanned,
                          safe_area_m=[round(lo_x, 4), round(lo_y, 4),
                                       round(hi_x, 4), round(hi_y, 4)],
                          note=("no dimension was off the sheet" if not clamped else
                                "%d 条尺寸原本在图框外，已拉回并按 %.0f mm 错开"
                                % (len(clamped), step_m * 1000)))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("clamp_failed", str(exc))


def auto_dimension(drawing_path, all_views=True, include_unmarked=False, eliminate_duplicates=True,
                   dimension_filter=None, place_outside=True, detect_same_value=False,
                   save=True, audit=True,
                   outside_gap_m=0.012, outside_step_m=0.008, prefer_view=None,
                   sheet=None, max_clamp_travel_m=0.030):
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
        _sheet_target, _sheet_err = _activate_sheet_if_given(doc, sheet)
        if _sheet_err is not None:
            return _sheet_err
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
        # Report 2026-11-07 (C3): after `add_sheet` the new page becomes active, and it has
        # NO views on it. `auto_dimension(all_views=True, include_unmarked=True)` then
        # returned `inserted: 0 / total_dimensions: 0 / ok: true` — a silent no-op that the
        # caller cannot distinguish from "this part has no dimensions". The same arguments
        # on the single-sheet control returned `inserted: 28`.
        #
        # An empty active sheet is a caller error (dimension a sheet that has views), so say
        # it instead of reporting success. Count only MODEL views: the sheet view (Type 1)
        # is always present and would make every page look non-empty.
        _active_views = []
        try:
            _sheet_name = str(doc.GetCurrentSheet.GetName)
        except Exception:
            _sheet_name = None
        # Per-SHEET, not per-document: `_iter_views(doc)` includes the other pages, which
        # made this guard pass on an empty page (measured 2026-11-07).
        for _v in _active_sheet_views(doc):
            _active_views.append(str(_v.Name))
        if not _active_views:
            return error(
                "active_sheet_has_no_views",
                ("当前活动图纸页（%s）上没有任何模型视图，无法插入尺寸"
                 % (_sheet_name or "unknown")),
                ("`add_sheet` 之后新页会成为活动页且它是空的。先在该页建视图"
                 "（`create_drawing` 的 views 参数，或后续的视图工具），"
                 "或用 `ActivateSheet` 切回有视图的那一页。"
                 "这条以前返回 `ok:true` + `inserted:0`，和「零件本来就没尺寸」无法区分。"))

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
            perf_hooks.bump("rebuild_calls")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
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
                    doc, before_names, _win_auto, prefer_view=prefer_view)
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
        # Report 2026-11-06 (P1, B4): `total_dimensions` fell back to 0 when `audit=False`,
        # so a receipt read `inserted: 12 / filter_kept: 9 / total_dimensions: 0`. Zero is a
        # measurement; "not measured" is not. Use None and say so.
        total = audit.get("total_dimensions")
        _total_measured = total is not None
        if total is None:
            total = None

        # P1-6: report the model full names that HAD duplicates and what the automatic
        # pass actually removed. The old code only reported and told the caller to delete
        # by hand — the duplicates stayed on the sheet.
        duplicate_report = {
            # Entries became 3-tuples when `prefer_view` needed to know which view each
            # copy lives in. Unpacking two here raised `too many values to unpack` and took
            # down `auto_dimension(eliminate_duplicates=True)` entirely — measured 2026-11-06
            # on two smoke items ("edit tools by name", "assembly drawing").
            full: [entry[0] for entry in entries]
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
        # `total` is None when audit=False ("not measured"), so it must not be compared.
        # 0.8.190 made it None to stop `inserted: 12 / total_dimensions: 0` reading as a
        # contradiction, and this comparison then raised TypeError on every such call —
        # which the pipeline turned into `dimensions failed; the drawing is not usable`
        # for three smoke items, with no way to see the cause.
        if total is not None and total > 30:
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
                perf_hooks.bump("rebuild_calls")
            except Exception:
                pass
            try:
                doc.GraphicsRedraw2()
                perf_hooks.bump("redraw_calls")
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
            _ang = (outside_debug or {}).get("skipped_angular") or []
            notes.append(
                "place_outside：%d 条尺寸已推到视图外按侧分层（外扩 %.0f mm + n×%.0f mm），"
                "失败 %d 条。" % (outside_placed, float(outside_gap_m) * 1000,
                                 float(outside_step_m) * 1000, len(outside_failed))
                + (" %d 条角度/弧长尺寸**未移动** —— 外移会把引线拉长、越过图框"
                   "（见 `outside_debug.skipped_angular`）。" % len(_ang) if _ang else ""))
            if outside_spills:
                # Say when the stack had to change sides or compress — otherwise the caller
                # sees a layout it did not ask for and cannot tell why.
                notes.append("分层受安全区限制：%s。" % "；".join(outside_spills))

        # Final gate, placed AFTER place_outside and immediately before the receipt.
        #
        # Report 2026-11-04: eight reference dimensions (RD1..RD8) came out of
        # InsertModelItems at y = -0.068 .. -0.200 on a 297 mm sheet — 68 to 200 mm BELOW
        # the paper. `place_outside` does not touch them (they are not in its buckets) and
        # the perpendicular-axis clamp only applies to what it does push. A dimension that
        # prints off the sheet is worse than a cramped one: the drawing states less than it
        # appears to.
        #
        # The previous attempt put this gate BEFORE `place_outside`, so it clamped correct
        # coordinates and place_outside then pushed them off again — `clamped=0` while nine
        # dimensions stayed at y<0. Order is the whole point here.
        clamped = []
        try:
            frame = list(doc.GetFirstView.GetOutline or ())
            if len(frame) >= 4 and (frame[2] - frame[0]) > 0.05:
                # Report 2026-11-07 (C5): `clamped_into_safe_area` moved a 45° dimension
                # from [0.1129, 0.4104] to **[0.1129, 0.01]** and called that "into the
                # safe area". 0.01 is the paper edge minus 10 mm, while the template's real
                # safe area starts at y = 0.0924 — the clamp pushed the annotation OUTSIDE
                # the frame it was supposed to pull back in.
                #
                # Same defect as `clamp_dimensions_into_safe_area` had before 0.8.183: the
                # name says safe area, the code used the sheet. Use the template.
                inset = 0.010
                lo_x, lo_y = frame[0] + inset, frame[1] + inset
                hi_x, hi_y = frame[2] - inset, frame[3] - inset
                _safe_source = "sheet_inset"
                try:
                    _m = template_safe_area(doc.GetFirstView,
                                            frame[2] - frame[0], frame[3] - frame[1])
                    if _m:
                        lo_x = frame[0] + _m.get("left", inset)
                        lo_y = frame[1] + _m.get("bottom", inset)
                        hi_x = frame[2] - _m.get("right", inset)
                        hi_y = frame[3] - _m.get("top", inset)
                        _safe_source = _m.get("source") or "template"
                except Exception:
                    pass
                off_sheet = []
                for view in _iter_views(doc):
                    if getattr(view, "Type", None) == 1:
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
                            ann = _win_auto.Dispatch(item)
                            if ann.GetType != 4:
                                continue
                            pos = [float(v) for v in ann.GetPosition]
                        except Exception:
                            continue
                        if lo_x <= pos[0] <= hi_x and lo_y <= pos[1] <= hi_y:
                            continue
                        off_sheet.append((ann, str(ann.GetName), pos))
                # Place them along the bottom margin, stacked so they do not land on one
                # line — nine dimensions snapped to a single y overlap into an unreadable
                # pile, which is only marginally better than being off-sheet.
                off_sheet.sort(key=lambda entry: entry[2][0])
                # Lay them along the bottom of the SAFE AREA, inside it on both ends.
                # `lo_y` is the safe-area floor (0.0924 on A3), so y can never come out
                # below the frame; when the band fills up, wrap into a second column
                # instead of saturating everything onto `hi_y` (which stacks the overflow
                # into one unreadable point).
                step = 0.014
                _col = max(1, int((hi_y - lo_y) / step) + 1)
                for i, (ann, name, pos) in enumerate(off_sheet):
                    _band = i % _col
                    _column = i // _col
                    nx = min(lo_x + _column * 0.045, hi_x)
                    ny = min(lo_y + _band * step, hi_y)
                    # ── How far is too far? (report 2026-11-08, P0-3) ────────────────
                    # On a sheet whose views had been placed at x=0 (the P0-2 bug) every
                    # dimension sat outside the frame, and this clamp dragged all eight of
                    # them to the left edge of the safe area — leaders crossing the whole
                    # A3. The report's ask: give the move a distance budget and refuse
                    # beyond it, rather than relocate an annotation to the other side of
                    # the sheet.
                    #
                    # 30 mm is roughly a dimension line plus text plus gap. Beyond that the
                    # annotation no longer points at what it measures, which is worse than
                    # leaving it where it was: a leader you cannot follow at least shows
                    # where the number belongs.
                    _travel = ((nx - pos[0]) ** 2 + (ny - pos[1]) ** 2) ** 0.5
                    if _travel > float(max_clamp_travel_m):
                        clamped.append({"name": name,
                                        "from_m": [round(pos[0], 4), round(pos[1], 4)],
                                        "error": "travel_exceeds_limit",
                                        "travel_mm": round(_travel * 1000, 1),
                                        "limit_mm": round(float(max_clamp_travel_m) * 1000, 1),
                                        "note": "尺寸离安全区太远，多半是父视图没摆进安全区。"
                                                "先修视图位置（set_view / place_views）再标尺寸，"
                                                "不要靠 clamp 把标注拖回图框。"})
                        continue
                    if not (lo_x <= nx <= hi_x and lo_y <= ny <= hi_y):
                        clamped.append({"name": name,
                                        "from_m": [round(pos[0], 4), round(pos[1], 4)],
                                        "error": "no_room_in_safe_area",
                                        "safe_area_m": [round(lo_x, 4), round(lo_y, 4),
                                                        round(hi_x, 4), round(hi_y, 4),
                                                        _safe_source]})
                        continue
                    try:
                        ann.SetPosition(nx, ny, 0.0)
                        clamped.append({"name": name,
                                        "from_m": [round(pos[0], 4), round(pos[1], 4)],
                                        "to_m": [round(nx, 4), round(ny, 4)]})
                    except Exception:
                        continue
        except Exception:
            clamped = []
        if clamped:
            try:
                _call_method(doc, "EditRebuild3")
                perf_hooks.bump("rebuild_calls")
            except Exception:
                pass
            # Report 2026-11-09 (Q-5): this said "已拉回安全区" while BOTH entries in
            # `clamped` carried `error: travel_exceeds_limit` — i.e. nothing was moved. The
            # sentence counted attempts and called them successes.
            _moved = [c for c in clamped if "to_m" in c]
            _refused = [c for c in clamped if c.get("error") == "travel_exceeds_limit"]
            if _moved:
                notes.append("有 %d 条尺寸原本在图框外，已拉回安全区并按 %.0f mm 错开。"
                             % (len(_moved), 14))
            if _refused:
                notes.append("有 %d 条尺寸超出移动上限（%s mm > %.0f mm）未拉回 —— "
                             "它们多半是因为父视图没摆进安全区。先 `set_view` 修视图位置，"
                             "不要靠 clamp 把标注拖回图框。"
                             % (len(_refused),
                                "、".join("%.0f" % c.get("travel_mm", 0) for c in _refused),
                                float(max_clamp_travel_m) * 1000))

        return ok(
            drawing=doc.GetTitle,
            inserted=count,
            sheet=_sheet_target,
            layout=layout,
            include_unmarked=bool(include_unmarked),
            # None means "not measured" (audit=False), not "zero". A receipt that says
            # `inserted: 12 / total_dimensions: 0` is self-contradictory.
            total_dimensions=total,
            total_dimensions_measured=_total_measured,
            total_dimensions_note=(None if _total_measured else
                                   "audit=False, so the sheet was not re-read; this call "
                                   "reports only what it inserted (see `inserted`)"),
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
            clamped_into_safe_area=clamped,
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
            perf_hooks.bump("rebuild_calls")
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
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


def open_document_paths():
    """Normalised paths of every document currently open in the SolidWorks session.

    Exists so a caller can snapshot BEFORE doing work and close only what it opened.
    Report 2026-11-05: `produce_drawing` closed `(target, path)` unconditionally, and
    `close_document` matches purely on path — so running the pipeline while the user had
    that same part open in the editor would close THEIR document, discarding unsaved edits.
    "Matches the exact path this run produced" was true and irrelevant: the user's document
    has the same path.

    Returns a TRI-STATE, because "nothing is open" and "I cannot tell" must not look alike:

        set()          the session enumerated fine and nothing is open  -> safe to close
        set(paths)     the session enumerated; these were already open   -> skip these
        None           enumeration FAILED                                -> close NOTHING

    Report 2026-11-05 (third round, P2-1): the old version returned `set()` for both the
    empty session and the failure, so `produce_drawing` could not tell them apart and did
    the worst of both:

        documents_skipped_open_by_caller: ["会话内的文档无法枚举，未关闭任何文档……"]
        documents_closed: [{"closed_count": 1}, {"closed_count": 1}]
        close_documents: "closed 2 document(s); 1 skipped as caller-owned"

    Three statements, three different stories: the message said nothing was closed, the
    data said two were, and the detail line counted the enumeration failure as a
    caller-owned skip. The comment in the caller claimed "nothing is closed when the
    session cannot be enumerated" — that was true of the sentence, not of the code.
    """
    try:
        from ..sw_session import get_sw
        sw = get_sw()
        documents = sw.GetDocuments
        if documents is None:
            # Measured 2026-11-05: a LIVE session with nothing open returns `None` here, not
            # an empty tuple. So `None` alone cannot mean "enumeration failed" — that would
            # make the empty-session case look like a bridge failure and skip closing
            # forever. Ask a property that answers regardless of what is open.
            try:
                _ = sw.RevisionNumber
                return set()          # session alive, nothing open: safe to close
            except Exception:
                return None           # the property itself is dead: do not touch anything
        paths = set()
        for document in documents:
            try:
                name = document.GetPathName or ""
            except Exception:
                continue
            if name:
                paths.add(os.path.normcase(os.path.normpath(str(name))))
        return paths
    except Exception:
        return None


def close_document(drawing_path):
    """Close a document SolidWorks still holds open.

    A report's `out/` kept two abandoned drafts it could not delete: "Device or resource
    busy" because SolidWorks still had them open, and the toolkit had no way to close
    anything. Every tool here opens documents; none of them ever let go.
    """
    try:
        from ..sw_session import get_sw

        # The safe-area cache is keyed by (sheet.Name, w, h) — it cannot tell two openings
        # of the same file apart, so a re-opened drawing kept serving the previous
        # opening's measurement. Report 2026-11-08 (P0-A) hit exactly that: the same sheet
        # measured `bottom 0.065` when freshly created and `0.004` after close+reopen.
        # Closing is the one moment we know the old measurement is stale.
        try:
            _SAFE_AREA_CACHE.clear()
        except Exception:
            pass

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

        # Report 2026-11-06 (P2, B13): three `~$xxx.SLDDRW` owner files (6 bytes each)
        # survived a clean close, so a delivery directory looked dirty even though every
        # document was gone. `CloseDoc` releases the document; it does not always remove
        # the lock file. Delete only the lock that belongs to THIS path — `~$` + basename
        # sits next to the file.
        locks_removed, locks_failed = [], []
        if closed:
            target = str(drawing_path)
            lock = os.path.join(os.path.dirname(os.path.abspath(target)),
                                "~$" + os.path.basename(target))
            if os.path.exists(lock):
                try:
                    os.remove(lock)
                    locks_removed.append(os.path.basename(lock))
                except Exception as exc:                            # noqa: BLE001
                    locks_failed.append({"file": os.path.basename(lock),
                                         "reason": str(exc)[:80]})

        return drawing_ok(
            drawing=os.path.basename(str(drawing_path)),
            closed=closed, closed_count=len(closed),
            owner_files_removed=locks_removed,
            owner_files_failed=locks_failed,
            note=("未找到已打开的该文档（可能本来就没开）。" if not closed
                  else "已关闭 %d 个%s。" % (len(closed),
                                            "，清理 %d 个锁文件" % len(locks_removed)
                                            if locks_removed else "")),
        )
    except Exception as exc:
        return error("close_failed", str(exc)[:120],
                     "Check that SolidWorks is reachable; a busy dialog blocks CloseDoc.")


@perf_hooks.timed("center_marks")
def _count_center_marks(doc):
    """How many centre-mark annotations the drawing actually holds.

    Report 2026-11-08 (P2-1): `IView.GetCenterMarkCount` reads 0 on this binding even when
    marks exist — the tool's own note said so, yet 0.8.228 changed the success test to
    `count > 0`. That turned a false PASS into a false FAIL: a view that already carried
    two centre marks, and a view where the insert worked, both reported `ok: false`.

    Counting the annotations is the only signal that does not depend on
    `GetCenterMarkCount`. `IAnnotation.GetType == 13` is `swAnnotationType_CenterMark`,
    the same code `list_drawing_annotations` maps to "center_mark".
    """
    try:
        import win32com.client as win
    except Exception:
        return None
    total = 0
    for view in _iter_views(doc):
        try:
            items = list(view.GetAnnotations or ())
        except Exception:
            continue
        for raw in items:
            try:
                if int(win.Dispatch(raw).GetType) == 13:
                    total += 1
            except Exception:
                continue
    return total


def _coincident_mark_groups(rows, tol=1e-4):
    """Split centre-mark rows into keepers and coincident duplicates. PURE.

    `rows` is a sequence of `(position, payload)`. Two marks are duplicates when ALL THREE
    coordinates are within `tol` (0.1 mm) — real marks on distinct holes are never that
    close. The FIRST of each cluster is kept, so a stable order gives a stable result.

    Report 2026-11-10 (P2-4) measured three marks on one point [0.2157, 0.1481, 0.026],
    left by an older non-idempotent version. Extracted so smoke can replay that geometry
    without COM.
    """
    keepers, duplicates, groups = [], [], []
    for index, (pos, _payload) in enumerate(rows):
        match = next(
            (k for k in keepers
             if all(abs(float(a) - float(b)) <= tol
                    for a, b in zip(pos, rows[k][0]))),
            None)
        if match is None:
            keepers.append(index)
            continue
        duplicates.append(index)
        groups.append({"position": [round(float(v), 6) for v in pos],
                       "kept_index": match,
                       "kept_at": [round(float(v), 6) for v in rows[match][0]]})
    return {"keepers": keepers, "duplicates": duplicates, "groups": groups}


def _dedupe_view_center_marks(doc, view):
    """Delete COINCIDENT centre marks in ONE view, keeping the first of each cluster.

    Report 2026-11-10 (P2-4): a view carried three `center_mark` annotations at the SAME
    point [0.2157, 0.1481, 0.026] — leftovers from a non-idempotent older version. They
    draw a visibly thicker line and read like a rendering artifact, and the existing
    `already_marked` guard cannot help: it only refuses to ADD more.

    Only markers within 0.1 mm of each other in SHEET space count as duplicates — two
    real marks on different holes are never that close. `GetPosition` is called as a
    METHOD; reading it as a property raises on this binding.
    """
    import win32com.client as _wc

    rows = []
    for _raw in (view.GetAnnotations or ()):
        try:
            _ann = _wc.Dispatch(_raw)
            if int(_ann.GetType) != 13:
                continue
        except Exception:
            continue
        try:
            _pos = [round(float(v), 6) for v in call_method(_ann, "GetPosition")]
        except Exception:
            continue
        rows.append((_pos, _ann))

    plan = _coincident_mark_groups(rows)
    removed, outcomes = 0, []
    for index, group in zip(plan["duplicates"], plan["groups"]):
        _pos, _ann = rows[index]
        try:
            call_method(_ann, "Select", False)
            call_method(doc, "EditDelete")
        except Exception as _exc:                                    # noqa: BLE001
            outcomes.append({**group, "removed": False, "error": str(_exc)[:80]})
            continue
        removed += 1
        outcomes.append({**group, "removed": True})
    return {"checked": len(rows), "removed": removed,
            "kept": len(plan["keepers"]), "groups": outcomes[:10]}


def add_center_marks(drawing_path, include_slots=True, extended_lines=True, view_name=None,
                     save=True, dedupe_existing=False):
    """Auto-insert center marks and centerlines on every hole/slot in every view.

    Requires activating each view first — drawing annotation ops act on the active
    view. GetCenterMarkCount under-reports on this binding, so `views_marked` (the
    API's own success count) is the reliable signal, not the count.

    `view_name` restricts it to one view. Without it the isometric view got marks too —
    a report saw 15 cross marks scattered across a 3D pictorial, which is simply wrong
    drafting.

    `dedupe_existing=True` FIRST deletes centre marks that sit on the same point (report
    2026-11-10 P2-4). The `already_marked` guard only stops NEW duplicates, so a sheet
    that already carries three coincident marks stays wrong forever; those have to be
    deleted by position. Off by default because it deletes annotations the caller may
    have placed on purpose — read `center_marks_removed` / `dedupe` in the receipt.
    """
    try:
        doc = _open_drawing(drawing_path)
        # Before/after annotation counts — the honest signal, see `_count_center_marks`.
        _cm_before = _count_center_marks(doc)
        insert_type = 1 | (4 if include_slots else 0)  # Hole = 1, Slots = 4
        insert_option = 2  # swCenterMarkStyle_e.swCenterMark_Single
        processed = marked = 0
        results = []
        # Report 2026-11-04 P2-6: the isometric view got center marks again (a prior report
        # saw 15 crosses scattered across a 3D pictorial). `IView.Type` cannot identify it —
        # every model view on this build answers 7 — and the name is just 工程图视图3, so
        # neither of the obvious tests works.
        #
        # `GetOrientationName` DOES name the base orientation: measured `*前视`, `*上视`,
        # `*等轴测`. That is the only reliable signal here, and it is also the one that
        # composes with a custom-named view.
        skipped_views = []
        _dedupe_results = []
        _dedupe_removed = 0
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and str(view.Name) != str(view_name):
                continue
            orientation = ""
            try:
                orientation = str(call_method(view, "GetOrientationName") or "")
            except Exception:
                orientation = ""
            lowered = orientation.lower()
            if ("等轴测" in orientation or "isometric" in lowered
                    or "trimetric" in lowered or "dimetric" in lowered):
                # Axonometric views are pictorials: a centerline across one is a drafting
                # error, not a convenience. Recorded so a caller can see it was skipped on
                # purpose rather than silently dropped.
                skipped_views.append({"view": str(view.Name), "reason": "axonometric",
                                      "orientation": orientation})
                continue
            processed += 1
            try:
                doc.ActivateView(view.Name)
            except Exception:
                pass
            # ── Delete pre-existing duplicates first (report 2026-11-10, P2-4) ─────
            # The idempotency guard below only stops NEW marks; a view that already has
            # three marks on one point keeps them. Do this BEFORE counting, so the count
            # and the `already_marked` decision describe the CLEANED state.
            if dedupe_existing:
                try:
                    _dd = _dedupe_view_center_marks(doc, view)
                except Exception as _exc:                            # noqa: BLE001
                    _dd = {"checked": 0, "removed": 0, "kept": 0,
                           "error": str(_exc)[:120]}
                _dedupe_removed += int(_dd.get("removed") or 0)
                if _dd.get("removed") or _dd.get("error"):
                    _dedupe_results.append({"view": str(view.Name), **_dd})
            # ── Idempotent (report 2026-11-08, P2-6) ──────────────────────────────
            # Called twice on the same view this left TWO center marks at the SAME point
            # (`细节项目254` / `256`, both [0.2157, 0.1481]). SolidWorks does not
            # deduplicate them, and two coincident marks draw a visibly thicker line — a
            # drafting defect that reads as a rendering artifact and is hard to find.
            #
            # So read the count BEFORE the call and leave an already-marked view alone.
            # `GetCenterMarkCount` reads 0 on this binding even when the view HAS marks,
            # so counting annotations in this view is the only way to know it is already
            # marked. Measured 2026-11-08 on 限位导向套: with the old check, a second call
            # added TWO MORE marks (before 2, after 4) instead of skipping.
            _view_marks = 0
            try:
                import win32com.client as _win
                for _raw in (view.GetAnnotations or ()):
                    try:
                        if int(_win.Dispatch(_raw).GetType) == 13:
                            _view_marks += 1
                    except Exception:
                        continue
            except Exception:
                _view_marks = 0
            if _view_marks > 0:
                results.append({"view": view.Name, "ok": True,
                                "skipped": "already_marked",
                                "center_mark_count": _view_marks,
                                "center_mark_source": "annotations",
                                "note": "This view already carries %d centre mark(s); "
                                        "nothing was inserted. Delete them first if you "
                                        "want a fresh pass." % _view_marks})
                continue
            try:
                ok_view = view.AutoInsertCenterMarks2(
                    insert_type, insert_option, bool(include_slots), bool(include_slots),
                    True, 0.0025, 0.0, bool(extended_lines), True, 0.0,
                )
            except Exception as exc:
                results.append({"view": view.Name, "ok": False, "error": str(exc)})
                continue
            # Per-view count FIRST, then decide whether anything was marked.
            #
            # Report 2026-11-07 (P1-3): on a section view `AutoInsertCenterMarks2` returned
            # True while `GetCenterMarkCount` stayed 0 and NOTHING appeared on the sheet —
            # `views_marked: 1` with `center_mark_count: 0`. The docstring here claimed the
            # API's return value was "the reliable signal, not the count"; the report shows
            # the return value is just as unreliable. A view with no hole/slot geometry
            # returns True and inserts nothing.
            #
            # So a view counts as marked only when the count moved. `ok_view` alone is
            # reported separately as `api_returned_true` so the difference stays visible.
            per_view = None
            # Count this view's centre-mark ANNOTATIONS instead of asking
            # `GetCenterMarkCount`, which reads 0 here even when marks were just created.
            try:
                import win32com.client as _wc
                _n = 0
                for _raw in (view.GetAnnotations or ()):
                    try:
                        if int(_wc.Dispatch(_raw).GetType) == 13:
                            _n += 1
                    except Exception:
                        continue
                per_view = _n
            except Exception:
                per_view = None
            _inserted = bool(per_view and per_view > 0)
            if _inserted:
                marked += 1
            results.append({"view": view.Name, "ok": bool(_inserted),
                            "api_returned_true": bool(ok_view),
                            "orientation": orientation,
                            "center_mark_count": per_view,
                            "note": (None if _inserted else
                                     "AutoInsertCenterMarks2 returned %r but the view still "
                                     "has 0 center marks — no hole/slot geometry to mark, "
                                     "or the call is a no-op on this view type"
                                     % (bool(ok_view),))})
        try:
            doc.EditRebuild3
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
        except Exception:
            pass
        _cm_after = _count_center_marks(doc)
        _cm_added = (None if _cm_before is None or _cm_after is None
                     else _cm_after - _cm_before)
        layout = None
        # PR1: honour save=False so a pipeline run saves once at the end. This was the last
        # of the five sub-tools still saving unconditionally.
        if save:
            _save_drawing(doc)
        # ── What "marked" means (report 2026-11-08, P2-1) ───────────────────────────
        # `views_marked` used to come from `GetCenterMarkCount`, which reads 0 on this
        # binding even when marks exist. 0.8.228 then made `count > 0` the ONLY proof of
        # success, so a view that already had marks — or one where the insert worked —
        # returned `ok: false`. The report calls it exactly right: a false PASS became a
        # false FAIL.
        #
        # `_cm_added` is the change in centre-mark ANNOTATIONS across the whole drawing.
        # It does not consult `GetCenterMarkCount` at all.
        _marked_by_count = marked > 0
        _marked_by_annotations = bool(_cm_added and _cm_added > 0)
        _api_said_yes = any(v.get("api_returned_true") for v in results)
        _skipped_any = any(v.get("skipped") for v in results)
        # FOUR outcomes, not two:
        #   added > 0                       -> marked
        #   every view already had marks    -> already_marked (a success, not a failure —
        #                                      the sheet ends up correct either way)
        #   added == 0, api said yes        -> unverified (this binding lies about counts)
        #   added == 0, api said no         -> genuinely nothing to mark
        #
        # Measured 2026-11-08: without the `already_marked` branch, three consecutive calls
        # on a view that came with two marks all returned `nothing_to_mark`, which reads as
        # "this tool cannot mark anything" when in fact the sheet was already correct.
        _verdict = ("marked" if _marked_by_annotations else
                    "already_marked" if _skipped_any else
                    "unverified" if _api_said_yes else "nothing_to_mark")
        _receipt = dict(
            drawing=doc.GetTitle,
            layout=layout,
            views_processed=processed,
            # The annotation delta is the number a caller can trust.
            views_marked=(_cm_added if _marked_by_annotations else 0),
            views_marked_by_count=marked,
            center_marks_before=_cm_before,
            center_marks_after=_cm_after,
            center_marks_added=_cm_added,
            verdict=_verdict,
            skipped_views=skipped_views,
            # 0.8.297: how many coincident marks were DELETE, and per view where. A non-zero
            # value proves the cleanup ran; 0 with `dedupe_existing:true` means there was
            # nothing to clean, which is a different fact from "not attempted".
            dedupe_existing=bool(dedupe_existing),
            center_marks_removed=_dedupe_removed,
            dedupe=_dedupe_results,
            views=results,
        )
        if _verdict == "nothing_to_mark":
            return error(
                "center_marks_not_drawn",
                "no centre-mark annotation was created and AutoInsertCenterMarks2 "
                "returned false for every view",
                "The views have no hole/slot geometry this build can mark. If you expected "
                "marks, check the view actually shows holes.",
                **{k: v for k, v in _receipt.items() if k != "drawing"})
        return ok(
            **(_receipt | {"note": (
                "`views_marked` is the change in centre-mark ANNOTATIONS, not "
                "`GetCenterMarkCount` (which reads 0 on this binding even when marks "
                "exist). `verdict: 'unverified'` means nothing new was added but "
                "AutoInsertCenterMarks2 reported success — the marks may already have been "
                "there, or the insert was a no-op; look at the sheet before trusting "
                "either.")}))
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


# ── Coaxial merge policy (report 2026-11-09 §4, fixed 0.8.294) ─────────────────────
# One radial tolerance used to do TWO unrelated jobs. The chamfer filter used
# "radius gap <= 1.5 mm" to find a chamfer mouth, and the coaxial merge used
# "radius gap > 1.5 mm" to avoid folding that same pair into a counterbore. The
# second gate silently refused EVERY shallow counterbore: Ø11 over Ø9.8 is a 0.6 mm
# radial gap, so both circles survived and the sheet got two word-for-word identical
# callouts. Chamfer MEMBERSHIP — not a radius magnitude — is the discriminator, so
# the filter now publishes the keys it adjudicated and the merge consumes that set.
CHAMFER_RADIUS_GAP_M = 0.0015


def _coaxial_merge_plan(axis_keys, chamfer_pair_keys):
    """Decide which keys of one co-axial set lose their hole callout.

    PURE: both arguments are plain `(centre, radius)` grouping keys, so smoke can
    replay report 2026-11-09 §4 without SolidWorks.

    `axis_keys` are the keys still alive in one X/Y axis group. `chamfer_pair_keys`
    are the keys the chamfer filter already adjudicated: those are NOT counterbores.
    That filter removed them or reported them in `ambiguous_chamfer_pairs` for manual
    annotation. Folding one into the widest circle here would delete a real bore
    (Ø16 bore vs its Ø18 chamfer mouth) and repeat report 2026-11-09 N-3.

    A counterbore and its through hole are ONE feature: SolidWorks' native hole
    callout on the wider circle already prints both diameters, so keeping both draws
    two identical annotations (Q-2). Before 0.8.294 the merge required
    `keep_r - r > CHAMFER_RADIUS_GAP_M`; there is NO radius test now. Keys of equal
    radius are different holes at different centres and are never dropped.
    """
    clean = [k for k in axis_keys if k not in chamfer_pair_keys]
    protected = len(axis_keys) - len(clean)
    if len(clean) < 2:
        return {"keep": None, "drop": [], "chamfer_protected": protected}
    keep = max(clean, key=lambda k: k[1])
    drop = [k for k in clean if k[1] != keep[1]]
    return {"keep": keep, "drop": drop, "chamfer_protected": protected}


def _read_native_callout_text(readers):
    """Read every reachable `GetText` slot without inventing a private `text` dialect.

    PURE apart from the COM `GetText` calls, so a fake reader can replay report strings in
    smoke. `readers` is an ordered iterable of `(label, IDisplayDimension-like object)`;
    the singular tool passes both the Dispatch'd callout and `GetDimension2(0)`, the batch
    tool passes one.

    Four report-driven rules (2026-11-09 0.8.278 §4 / 0.8.281 §6, 2026-11-10 0.8.290 S-5):

    1. `text` stays ONE native string. Joining the slots with `" | "` made the same field
       mean different things in `add_hole_callouts` and `list_drawing_annotations`: the
       latter's `text_is_format_code` / `text_decoded` parse a single native string and
       silently mis-parse a composite. Every other line goes into `text_lines` (an ARRAY).
    2. `native_text_complete` is ALWAYS None (unknown), never true/false. Six probed slots
       cannot PROVE SolidWorks has no seventh line. `len(parts) > 1` produced both false
       negatives (a correct single-line callout flagged `text_is_partial`) and false
       positives (two of three lines read -> "complete").
    3. `<hw-*>` Hole Wizard placeholders are stripped from rendered strings but kept in
       `text_slots_raw`. Measured: `<HOLE-SPOT><MOD-DIAM> <hw-cbdia> <HOLE-DEPTH>
       <hw-cbdepth>` in a slot, with nothing for those two tokens on the PDF. They are
       NOT format codes: `<MOD-DIAM>` is how SolidWorks STORES a diameter and renders as
       Ø, while `<hw-*>` has no value bound and never renders. Leaving them in `text`
       makes every "unresolved format code" audit fire on a correct sheet.
    4. Nothing is silently dropped: `text_slots_raw` holds the unmodified slot text, so a
       caller can always reconstruct what the binding returned.

    Callers that need to JUDGE the text (e.g. the depth gate) must use `text_lines`, not
    `text`: on 限位导向套 the depth lives in another slot and taking only the first line
    would let `Ø16 ↧ 68` through again — the exact P0 fixed in 0.8.291.
    """
    import re as _re

    hw = _re.compile(r"<hw-[^>]*>", _re.I)
    lines, raw_rows, slots_read, placeholders, used = [], [], [], [], []
    for slot in (1, 0, 2, 3, 4, 5):
        for label, reader in readers:
            try:
                value = reader.GetText(slot)
            except Exception:
                continue
            text = str(value).strip() if value else ""
            if not text:
                continue
            raw_rows.append({"slot": slot, "reader": label, "text": text})
            found = hw.findall(text)
            placeholders.extend(found)
            rendered = " ".join(hw.sub("", text).split()).strip()
            if rendered and rendered not in lines:
                lines.append(rendered)
                slots_read.append(slot)
                if label not in used:
                    used.append(label)
            break                      # one reader answered this slot; do not duplicate it
    return {
        "text": lines[0] if lines else None,
        "text_lines": lines,
        "text_slots_read": slots_read,
        "text_slots_raw": raw_rows,
        "hw_placeholders_stripped": sorted({p.lower() for p in placeholders}),
        "native_text_complete": None,
        "text_completeness": "unknown",
        "text_completeness_reason": (
            "GetText(slot) was probed over 6 slots, but this binding cannot prove that "
            "SolidWorks holds no further text line; completeness is unknown rather than "
            "true or false. Read `text_lines` for everything that WAS read."),
        "text_reader": (", ".join(used) if used
                        else "no slot returned content on any reader"),
    }


def _hole_callout_verification(text, model_match_ok=None):
    """May a freshly created hole callout stay on the sheet? PURE — no COM (0.8.291).

    Report 2026-11-10 (S-1) measured `Ø16 ↧ 68` on a THROUGH bore while the receipt said
    `created:true / ok:true / model_match.ok:true / delta_mm:0.0`. Three separate gates all
    missed it: `ok` was hard-coded, `model_match` compared only the diameter, and the text
    reader looked at the wrong object. The rule below is the whole contract in one place,
    so smoke.py can replay the report's strings without SolidWorks:

        "<MOD-DIAM> 16 <HOLE-DEPTH> 68"  -> unverified / unsupported_through_bore
        None                             -> unverified / hole_callout_unverifiable
        "Ø16 完全贯穿"                    -> verified
        any text + model_match_ok=False  -> unverified / diameter_mismatch

    We do NOT try to decide whether the hole is really through: the drawing cannot prove
    that. A depth symbol is therefore refused rather than judged.
    """
    joined = str(text or "")
    has_depth = ("\u21a7" in joined) or ("HOLE-DEPTH" in joined)
    readable = bool(joined.strip())
    verified = readable and not has_depth and model_match_ok is not False
    if verified:
        return {"verified": True, "verdict": "verified", "error": None}
    if has_depth:
        code = "unsupported_through_bore"
    elif model_match_ok is False:
        code = "diameter_mismatch"
    else:
        code = "hole_callout_unverifiable"
    return {"verified": False, "verdict": "unverified", "error": code}


def _reject_unverified_callout(doc, callout):
    """Remove a just-created hole callout whose rendered text cannot be trusted.

    Report 2026-11-10 (S-1): `add_hole_callout` created `Ø16 ↧ 68` on a THROUGH bore and
    returned `created:true / ok:true`. The batch tool's `unannotated_note` warned about it,
    but a caller using the singular tool never saw that note — the wrong annotation stayed
    on the sheet. "Put up a sign" was not a fix.

    Deleting is the same verified path `add_hole_callouts._discard` uses: get the
    IAnnotation through a late-bound `GetAnnotation` CALL, select it, `EditDelete`.
    Returns a dict — a failed revert must be reported, never swallowed into `reverted:True`.
    """
    try:
        import win32com.client as _win_revert

        dim = _win_revert.Dispatch(callout)
        ann = _win_revert.Dispatch(_call_method(dim, "GetAnnotation"))
        doc.ClearSelection2(True)
        if not ann.Select(False):
            return {"reverted": False,
                    "reason": "IAnnotation.Select(False) returned False"}
        _call_method(doc, "EditDelete")
        # `view.GetAnnotations` / GetAnnotationCount do not refresh until a real rebuild —
        # the same trap as `auto_dimension`'s duplicate removal (0.8.19).
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        return {"reverted": True}
    except Exception as exc:
        return {"reverted": False, "reason": "%s: %s" % (type(exc).__name__, str(exc)[:140])}


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
                matched_circle=circle_matched,
                # The callout text can say `↧ 68` on a hole that goes through the part.
                # `value_mm` is still 16, so a number-only check never sees it. Surface the
                # contradiction instead of letting `ok: true` imply the text is right.
                selected_before_call=_sel_count)

        readback = {}
        # ── Read the text off the DISPLAY DIMENSION, every slot ─────────────────────────
        # 2026-11-09 (R-1): this used `callout.GetText()` with no argument on the
        # IAnnotation — no such method, so `text` was always None while the receipt said
        # `ok:true`. That null hid the real output, measured on 限位导向套:
        #
        #     Ø16 ↧ 68          <- a blind 68 mm hole, on a bore that goes THROUGH
        #
        # `value_mm` was 16 (correct), so any number-only check passed.
        #
        # 2026-11-10 (S-1, the actual root cause): the 0.8.279 "fix" switched to
        # `callout.GetDimension2(0).GetText(slot)`, which ALSO returns nothing here. The
        # batch tool reads the SAME object differently and succeeds:
        #
        #     dim = win32com.client.Dispatch(display)   # AddHoleCallout2 return value
        #     dim.GetText(slot)                         # -> "<MOD-DIAM> 16 <HOLE-DEPTH> 68"
        #
        # So `text_slots: []` was a READER bug, not a SolidWorks limitation. The depth
        # symbol was readable all along; the singular tool just never looked at it. Use the
        # proven Dispatch path first, keep GetDimension2 as a fallback.
        _readers = []
        try:
            import win32com.client as _win_read

            _readers.append(("IDisplayDimension via Dispatch",
                             _win_read.Dispatch(callout)))
        except Exception:
            pass
        try:
            _dim = callout.GetDimension2(0)
            if _dim is not None:
                _readers.append(("GetDimension2(0)", _dim))
        except Exception:
            pass
        # `text` is ONE native line again; `text_lines` carries the rest. The old
        # composite string leaked into `list_drawing_annotations`, whose
        # `text_is_format_code` / `text_decoded` parse a single native string.
        readback.update(_read_native_callout_text(_readers))
        # Keep the old key for callers, but with the same single-line semantics per entry.
        readback["text_slots"] = list(readback["text_lines"])

        # ── Flag a bore that the callout calls blind (report 2026-11-09, R-1) ────────────
        # The Ø16 bore goes through the part (a full section shows an open band end to end;
        # volume 70 671 mm³ agrees). SolidWorks' own callout still renders it as
        # `Ø16 ↧ 68` — it takes the depth from the cut feature, not from whether the cut
        # exits the far face.
        #
        # Rewriting that text through `SetText` is what produced "Ø1515" in 0.8.x: a
        # dimension's display text lives in several slots and writing one destroys the
        # others. So do not rewrite — say it.
        _text_flags = []
        # Judge over EVERY line, not `text`. `text` is one native line again (0.8.293);
        # on 限位导向套 the depth `<HOLE-DEPTH>68` / `↧` lives in a DIFFERENT slot. Reading
        # only `text` would let `Ø16 ↧ 68` on a through bore return ok:true again — the
        # exact P0 that 0.8.291 reverted. `text_lines` is the full read.
        _joined = " ".join(readback.get("text_lines") or [])
        _has_depth = ("\u21a7" in _joined) or ("HOLE-DEPTH" in _joined)
        if _has_depth:
            _text_flags.append({
                "flag": "depth_on_a_through_bore_suspect",
                "detail": "the callout carries a depth symbol (`↧` / <HOLE-DEPTH>). "
                          "SolidWorks takes it from the cut feature, not from whether the "
                          "cut exits the far face; on 限位导向套 a Ø16 THROUGH bore rendered "
                          "as `Ø16 ↧ 68`. This tool cannot prove through/blind from the "
                          "drawing, so the callout is reverted and `ok:false` is returned. "
                          "Do NOT retry via `add_drawing_dimension(style='diameter')` — "
                          "that path's dimension text does not render either (com-quirks.md).",
            })
        if not _joined:
            _text_flags.append({
                "flag": "text_unreadable",
                "detail": "no text slot returned content on either reader; the rendered "
                          "annotation was not verified. Returns `ok:false` and reverts the "
                          "callout — an unreadable annotation must not be left on a sheet.",
            })
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

        # `match` used to stay None when `model_path` was not supplied — while the note
        # told the caller to "compare model_match.ok". A null that the note treats as a
        # verdict is the same failure this project keeps fixing elsewhere: report it as
        # "not checked", with a reason, instead of handing back nothing.
        # Report 2026-11-10 (S-1, suggestions 3/4): `model_match.ok:true` was being read as
        # "the callout is correct" when all it proved was 16.0 == 16.0. The depth `↧ 68`
        # sailed through with `delta_mm: 0.0`, making the diameter match a FALSE SAFETY
        # SIGNAL. `build_feature_ledger` inventories CYLINDRICAL FACES only (diameter /
        # axis / centre) — there is no through/blind flag, so the through state genuinely
        # cannot be derived here. Say `unknown` instead of inventing a verdict, and state
        # in the receipt exactly what `ok` means.
        _through_unknown = {
            "through_state": "unknown",
            "through_state_reason": (
                "build_feature_ledger() inventories cylindrical faces only; it has no "
                "through/blind flag, so a depth symbol cannot be checked against the model"),
            "depth_text_present": _has_depth,
            "depth_text_consistent": None,
            "depth_text_consistent_reason": (
                "through_state is unknown — consistency is NOT judged; `null` is not `true`"),
            "ok_means": ("diameter match ONLY — never evidence that the rendered text, "
                         "depth or through state is correct"),
        }
        match = dict(_through_unknown, ok=None, checked=False,
                     reason="model_path not supplied — diameter/through state not verified")
        if model_path:
            try:
                from . import ledger as _ledger

                led = _ledger.build_feature_ledger(model_path, include_faces=True)
                values = sorted({round(h["diameter_mm"], 1) for h in (led.get("holes") or [])})
                got = readback.get("value_mm")
                if got is not None and values:
                    nearest = min(values, key=lambda v: abs(v - got))
                    match = dict(
                        _through_unknown,
                        read_mm=got,
                        nearest_model_diameter_mm=nearest,
                        delta_mm=round(got - nearest, 3),
                        ok=abs(got - nearest) <= max(0.2, nearest * 0.02),
                        checked=True,
                        model_diameters_mm=values[:20])
            except Exception as exc:
                match = dict(_through_unknown, error=str(exc)[:120])

        # ── Verification gate (report 2026-11-10, S-1 — the core fix) ────────────────
        # Before this version EVERY path returned `ok:true`: the one whose rendered text
        # was `Ø16 ↧ 68` on a through bore, AND the one where no text could be read at all.
        # `text_flags` even said "do not treat ok:true as evidence the text is right" while
        # the tool kept handing out `ok:true`. A warning no batch pipeline can act on is
        # not a fix — the report called it "坑没被堵，只是被贴了告示".
        #
        # Now anything unverifiable is REVERTED and reported as `ok:false` / `verified:false`.
        # This is deliberately stricter than "keep it but warn": both automatic paths for
        # the main bore were measured and retired (com-quirks.md), so a "created but wrong"
        # path is the bug, not a capability worth preserving.
        _verification = _hole_callout_verification(_joined, match.get("ok"))
        _verified = _verification["verified"]
        if _verification["error"] == "diameter_mismatch":
            _text_flags.append({
                "flag": "diameter_mismatch",
                "detail": "model_match.ok is False — the callout's value does not match any "
                          "model diameter. Reverted.",
            })
        _revert = None
        if not _verified:
            _revert = _reject_unverified_callout(doc, callout)
        _reverted = bool(_revert and _revert.get("reverted"))

        after = 0
        if target is not None:
            try:
                after = int(target.GetAnnotationCount)
            except Exception:
                after = 0
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
        except Exception:
            pass
        if save:
            _save_drawing(doc)

        _extra = {}
        if not _verified:
            _extra["error"] = _verification["error"]
            _extra["hint"] = (
                "SolidWorks 的 HoleCallout 会把贯通孔写成带深度的盲孔（实测 "
                "`Ø16 ↧ 68`），而 `SetText` 改写文字会毁掉其它槽位（历史 `Ø1515`），"
                "`add_drawing_dimension(style='diameter')` 的尺寸文字也渲染不出来。"
                "本工具不再把无法验证的标注留在图纸上：请在 SolidWorks 界面手工标该孔。"
                "复数工具 `add_hole_callouts.unannotated_note` 给出同样结论。")
        return drawing_ok(
            ok=_verified,
            # `created` reflects the SHEET, not the API call: a reverted callout was never
            # really delivered. A failed revert leaves `created:true` with `ok:false` and
            # `drawing_safe:false`, so nobody can mistake it for a good annotation.
            created=(not _reverted),
            view=target.Name if target is not None else None,
            annotations_before=before,
            annotations_after=after,
            readback=readback,
            model_match=match,
            # Which circle the point resolved to. Without it a caller cannot tell "the hole
            # I meant" from "the nearest hole", and this tool used to fail outright instead.
            matched_circle=circle_matched,
            text_flags=_text_flags,
            verification=_verification,
            verified=_verified,
            verdict=_verification["verdict"],
            drawing_safe=_verified,
            callout_revert=_revert,
            saved=bool(save),
            note=("model_match.ok：null=未校验（未传 model_path）；true=仅直径匹配；"
                  "false=直径不匹配。三者都不代表图面文字/深度/贯通状态正确"
                  "（见 model_match.ok_means，through_state 恒为 unknown）。"
                  "v0.8.291 起：文字不可读或带深度符号时撤销标注并返回 "
                  "ok:false / verified:false；贯通主孔请在 SolidWorks 里手工标。"),
            **_extra
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("hole_callout_failed", str(exc))


def _cell_position(note):
    """Sheet position of a planned title-block cell, or None when it cannot be read.

    `ann_position` documents "None when unavailable" — and returns None on this template for
    some cells. The caller used to iterate the result directly:

        [round(v, 4) for v in ann_position(p["note"])]

    which is exactly the `'NoneType' object is not iterable` that made `set_title_block`
    fail on every call (report 2026-11-04, P0-1). The tool was unusable, and the error
    surfaced as a Python message rather than a structured one, so a caller had no way to
    know which cell broke.

    Returning None (not []) is deliberate: [] would read as "the position is the origin",
    which is a different and wrong claim.
    """
    if note is None:
        return None
    position = ann_position(note)
    if position is None:
        return None
    try:
        return [round(v, 4) for v in position]
    except TypeError:
        return None


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
# Cell anchors for a GB A3 title block (`gb_a3.drwdot`), in sheet metres.
#
# Report 2026-11-08 (P2-2): `material` used to point at (0.344, 0.021), which is the
# "主管设计" row — the y was the same as `code`'s. The template's own Q235 sample sits at
# (0.3415, 0.055), so writing material there overwrote the wrong cell. The dry-run receipt
# even showed the coordinates, but without the text that was already in that cell there was
# no way to notice.
#
# These anchors are TEMPLATE SPECIFIC. `set_title_block` now echoes what is already at the
# target cell so a wrong slot shows up before anything is written.
TITLE_SLOTS = {
    "name": (0.391, 0.036),
    "code": (0.391, 0.022),
    "material": (0.344, 0.055),
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


def _note_text(annotation):
    """The visible text of a note annotation, or "" if it cannot be read.

    `IAnnotation` has NO `GetText` at all — the method lives on `INote`, and there it takes
    NO arguments:

        def GetText(self)          # INote
        def GetText(self, slot)    # IDisplayDimension — a DIFFERENT interface

    Calling `annotation.GetText(1)` raised, the `except` swallowed it, and every note read
    back as `""`. That empty string is why `set_title_block` could never see that the
    material cell already said POM, so `already_filled` never fired and each run wrote a
    second copy on top of the first (report 2026-11-07, P1-D).

    Measured 2026-11-07 on a GB A3 template: with `INote.GetText()` the title block reads
    back correctly — `飞达动力轴` at (0.3907, 0.0363), `1:1` at (0.3591, 0.0304),
    `重量` / `比例` as labels. With `GetText(1)` every one of them was `''`.
    """
    try:
        import win32com.client as win
        raw = annotation
        try:
            raw = annotation.GetSpecificAnnotation
        except Exception:
            pass
        specific = win.Dispatch(raw)
    except Exception:
        return ""
    # Typed route first (the only one that resolves GetText with no arguments), then two
    # untyped fallbacks for builds where the generated cache is missing.
    try:
        import importlib
        import sys as _sys
        try:
            import win32com.client.gencache as _gc
            _path = _gc.GetGeneratePath()
            if _path and _path not in _sys.path:
                _sys.path.insert(0, _path)
        except Exception:
            pass
        mod = importlib.import_module("83A33D31-27C5-11CE-BFD4-00400513BB57x0x32x0")
        note = mod.INote(specific._oleobj_)
        return str(note.GetText() or "")
    except Exception:
        pass
    for call in (lambda: specific.GetText(), lambda: specific.Text):
        try:
            value = call()
            if isinstance(value, str):
                return value
        except Exception:
            continue
    return ""


def set_title_block(drawing_path, name=None, code=None, material=None, apply=False,
                   include_reversed=True, scale=None, sheet=None):
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
        _sheet_target, _sheet_err = _activate_sheet_if_given(doc, sheet)
        if _sheet_err is not None:
            return _sheet_err
        _sheet_obj = doc.GetFirstView
        # THIRD site of the same miss (report 2026-11-07, P0-2). `sheet` is the PARAMETER
        # (a page name or None) since 0.8.204; the local Sheet object is `_sheet_obj`.
        # `_sheet_notes(None)` returned [] and `_sheet_notes("图纸2")` raised, so the
        # marker loop below never ran once — which is why `scale` was reported as
        # "title block has no scale value in the bottom band" while `1:1` sat at
        # [0.3591, 0.0304]. `_note_at` was fixed in 0.8.215; this call was not.
        notes = _sheet_notes(_sheet_obj)
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
                sheet_box = list(_sheet_obj.GetOutline or [])
                sheet_h = (sheet_box[3] - sheet_box[1]) if len(sheet_box) >= 4 else 0.297
            except Exception:
                sheet_h = 0.297
            for note in notes:
                # SECOND occurrence of the same defect (report 2026-11-07, P0-2). The
                # main marker loop was fixed to use `_note_text`; this one still did
                # `text = note.GetText`, which yields a bound method (or raises), so
                # `isinstance(text, str)` was always False and EVERY note was skipped.
                # `scale: 1:1` sat at [0.3591, 0.0304] — inside the band, matching the
                # pattern — and the receipt still said "no scale value in the bottom band".
                text = _note_text(note)
                if not isinstance(text, str) or not scale_pattern.match(text):
                    continue
                try:
                    position = [round(v, 4) for v in ann_position(note)]
                except Exception:
                    position = None
                if position and position[1] > sheet_h * 0.15:
                    continue  # outside the title-block band
                # `str(scale)` wrote "2.0" into a cell that reads "1:1" — a scale cell holds
                # a RATIO, not a decimal. `create_drawing` builds `1:2` / `2:1` from the same
                # number with this formula; using it here keeps the two paths identical.
                # Report 2026-11-08 (P2-8): `create_drawing` calls this with a RATIO
                # STRING ("1:2"), the manual callers pass a float (2.0), and only the float
                # path worked — `float("1:2")` raised ValueError, which the outer
                # `except (FileNotFoundError, ValueError)` turned into `bad_input`. So the
                # same feature failed from the pipeline and succeeded by hand, and the
                # receipt blamed the input rather than this line.
                _ratio_value = scale
                if isinstance(_ratio_value, str) and ":" in _ratio_value:
                    _lhs, _rhs = _ratio_value.split(":", 1)
                    _ratio_value = float(_lhs) / float(_rhs)
                else:
                    _ratio_value = float(_ratio_value)
                if _ratio_value <= 0:
                    return error("bad_input",
                                 "scale must be positive, got %r" % (scale,))
                _scale_text = (("1:%g" % round(1.0 / _ratio_value, 4))
                               if _ratio_value < 1
                               else ("%g:1" % round(_ratio_value, 4)))
                planned.append({
                    "field": "scale", "before": text, "after": _scale_text, "note": note,
                    "angle_deg": 0.0, "position": position,
                })
            if not any(p["field"] == "scale" for p in planned):
                unmatched.append({"field": "scale", "marker": "n:m",
                                  "reason": "title block has no scale value in the bottom band"})

        for field, marker, new_value in wants:
            candidates = []
            for note in notes:
                # FOURTH site of the same bad read, and the most consequential: this is the
                # `marker not in text` test that decides whether the name / code / material
                # cells are FOUND at all. `note.GetText` is a bound method, so
                # `isinstance(text, str)` was always False and every cell fell through to
                # the `via: "created"` branch — which ADDS a note instead of rewriting the
                # existing one. That is the "两处 POM / 两处 JT-001" in the 2026-11-07
                # report; 0.8.215 only fixed the `before` read further down, not this test.
                text = _note_text(note)
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
                # Report 2026-11-06 (P1, B7): the parameter is called `include_reversed`
                # and passing True still left the 180-degree 图样代号 as a placeholder —
                # `_rank` sorts reversed cells last, so the upright one won and the reversed
                # copy went to `skipped_extra_matches`. A caller who asked for reversed cells
                # to be written got the opposite, with no way to tell.
                #
                # Now the flag decides: True writes every cell that carries the marker,
                # False keeps only the upright one.
                _take = candidates if include_reversed else candidates[:1]
                for entry in _take:
                    entry.pop("_reversed", None)
                    planned.append(entry)
                for entry in candidates[len(_take):]:
                    skipped_extra.append({
                        "field": field, "text": entry.get("before"),
                        "position": entry.get("position"),
                        "angle_deg": entry.get("angle_deg"),
                        "reason": "another cell carries the same marker; kept the upright "
                                  "one nearest the configured slot"})
            else:
                slot = TITLE_SLOTS.get(field)
                # `sheet` is the PARAMETER (a page name or None) since 0.8.204 — the
                # local Sheet object was renamed to `_sheet_obj` and this call site was
                # missed. `sheet.GetAnnotations` therefore raised on None/str, `_note_at`
                # returned None, and EVERY field took the `via: "created"` path: instead of
                # rewriting the existing cell it added a second note on top of it. That is
                # the "two POMs / two JT-001" in the 2026-11-07 report, and it is mine.
                note = _note_at(_sheet_obj, slot) if slot else None
                if note is not None:
                    # `INote.GetText` is a METHOD; this read the bound method object and
                    # then `isinstance(before, str)` quietly turned it into "". That is why
                    # the 2026-11-05 report saw `"before": ""` for `material` while the
                    # template visibly printed POM in that cell — the cell was NOT empty,
                    # the read never happened. Every "is this cell free?" decision downstream
                    # was therefore made on a value that was always "".
                    # `_note_text` goes through INote.GetText(), which takes NO arguments.
                    # The previous version called `note.GetText()` on the IAnnotation
                    # dispatch, where that method does not exist — it raised, the except
                    # returned "", and every cell looked empty forever.
                    before = _note_text(note)
                    _entry = {
                        "field": field, "before": before,
                        "after": new_value, "note": note, "via": "position",
                    }
                    if slot:
                        _entry["cell_slot_m"] = [round(float(v), 4) for v in slot]
                    _before = _entry["before"]
                    if _before.strip() and _before.strip() != str(new_value):
                        # The slot is a hard-coded coordinate measured on one template.
                        # Report 2026-11-05: `material` resolved to the strip above
                        # 共 张 第 张 / 版本 while the template's own POM cell already said
                        # POM — two POMs on the sheet, both reported ok.
                        _entry["already_filled"] = True
                        _entry["warning"] = (
                            "该格已有内容 %r，与要写入的 %r 不同。可能是模板静态文字，"
                            "也可能是同名字段的另一处副本——cell_slot_m 是本次使用的"
                            "固定坐标，请核对它是否落在你以为的格子里。"
                            % (_before, new_value))
                    planned.append(_entry)
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
                             # Never iterate ann_position() directly: it returns None when
                             # the cell has no readable position (P0-1, 2026-11-04).
                             cell_position_m=_cell_position(p.get("note")))
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
                # Same guard as the dry-run branch. Two call sites iterated
                # ann_position() directly; P0-1 only surfaced the first one because
                # dry_run returns before reaching this loop.
                "cell_position_m": _cell_position(item.get("note")),
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
            perf_hooks.bump("redraw_calls")
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


# Report 2026-11-12 §4 (P0): an ANGULAR dimension's `SystemValue` is RADIANS, and every
# reader multiplied it by 1000 as if it were millimetres. 45° = 0.785398 rad became
# `785.398 "mm"`, which then printed as `785.4`, entered a length comparison as
# `drawing_only_values: [785.4]`, dropped `coverage` to 0.7, and made the angle count as
# an orphan. Both codes below are angular on this binding — `add_drawing_dimension`
# already reported `value_is_angular=(type in (1, 3))`; the other readers used one code
# and disagreed with it. ONE predicate now, so they cannot drift again.
ANGULAR_DIMENSION_TYPES = (1, 3)


def _is_angular_dimension_type(dim_type):
    """Is this `GetType` code angular? PURE. `None` / junk -> False (never guess)."""
    try:
        return int(dim_type) in ANGULAR_DIMENSION_TYPES
    except (TypeError, ValueError):
        return False


def _radians_to_deg(value_rad):
    """Radians -> degrees, 3 decimals. PURE."""
    return round(float(value_rad) * 180.0 / 3.141592653589793, 3)


def _annotation_dimension_type(annotation, win=None):
    """`GetType` of a dimension's SPECIFIC annotation, as an int (or None)."""
    if win is None:
        import win32com.client as win
    try:
        spec = win.Dispatch(annotation.GetSpecificAnnotation)
    except Exception:
        return None
    try:
        raw = spec.GetType
        return int(raw() if callable(raw) else raw)
    except Exception:
        return None


def drawing_dimension_values(doc):
    """All LINEAR dimension values already on the sheet, in MODEL MILLIMETRES.

    IView.GetDisplayDimensions only returns a subset (13 of 56 measured), so read the
    view's annotation list instead: dimension annotations have GetType() == 4, and the
    value comes from GetSpecificAnnotation().GetDimension2(0).SystemValue.

    Angular rows are SKIPPED. Both callers compare against lengths, and a radian value
    masquerading as millimetres is what produced `785.4` (report 2026-11-12 §4).
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
                if _is_angular_dimension_type(
                        _annotation_dimension_type(ann, win)):
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


def _edge_kind(row):
    """Classify an edge from what the binding actually returned. PURE (0.8.293).

    `GetVisibleEntities2` returns CONTOUR edges: `GetStartVertex` is None on this build,
    so a straight silhouette often has no endpoints at all. An arc cannot be told apart
    from such an unreadable straight edge, so the honest answer is `unknown` rather than
    a guessed `arc`. Callers must not treat `unknown` as a line they can dimension.
    """
    if row.get("is_circle") is True or row.get("radius_m"):
        return "circle"
    start, end = row.get("model_start"), row.get("model_end")
    if start and end and start != end:
        return "line"
    return "unknown"


def _edge_projection_scope(rows):
    """PURE: are there projected EDGE ENDPOINTS a caller can pick by index? (0.8.293)

    Four consecutive reports (2026-11-08 P1-3, 2026-11-09 Q-6, 2026-11-10 S-6) measured
    a right-side view with five edges, ALL circles, `sheet_start == sheet_end ==
    sheet_center`. The envelope still said `sheet_projected: true` because the CENTRES had
    been projected. A caller read that as "the edges are usable", tried
    `add_distance_dimension(edge_a=..., edge_b=...)` for the 70 mm overall length, and
    could not: no straight edge exists in the payload.

    So `sheet_projected` now means "there is at least one projected edge with DISTINCT
    start/end points", not "some coordinate was transformed". Circle centres remain
    available under `sheet_center` / `sheet_point_on_circle`; `sheet_coordinates_present`
    says they are still there, and `sheet_projection_reason: circular_model_edges_only`
    explains why selection is not possible. Kept as a pure function so smoke can replay
    the report's five measured circles without SolidWorks.
    """
    kinds = {}
    for row in rows:
        kind = _edge_kind(row)
        kinds[kind] = kinds.get(kind, 0) + 1
    usable = [
        row for row in rows
        if _edge_kind(row) == "line"
        and row.get("sheet_start") and row.get("sheet_end")
        and row["sheet_start"] != row["sheet_end"]
    ]
    return {
        "edge_kinds": kinds,
        "usable_edge_count": len(usable),
        "usable_for_edge_selection": bool(usable),
        "circular_model_edges_only": bool(rows) and not usable,
        "sheet_coordinates_present": any(
            row.get("sheet_start") or row.get("sheet_center") for row in rows),
    }


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
            # `index` AND `edge_index` — report 2026-11-08 (P1-3). `add_distance_dimension`
            # documents its `edge_a` as "Index of the FIRST edge (from
            # list_drawing_edges)", but this row only carried `index`. Every caller reading
            # `edge_index` got None, concluded the view could not be selected by index, and
            # fell back to hand-placed sheet coordinates. Both keys now exist so neither
            # naming breaks.
            item = {"index": index, "edge_index": index}
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
                # Report 2026-11-05: this used to run only `if "model_start" not in item`.
                # A circular edge whose start vertex DID resolve therefore skipped the
                # branch entirely, so `radius_m` / `sheet_point_on_circle` were never set —
                # measured: 0 of 145 edges across three views carried the field, which is
                # why `add_geometric_tolerance` fell back to `sheet_center` and reported a
                # leader endpoint one radius (15 mm) away from where DXF puts the arrow.
                #
                # Circle detection must not depend on whether a vertex happened to resolve;
                # `CircleParams` answers independently. Still guarded, so a real straight
                # edge is unaffected: an exception leaves the item untouched.
                if "radius_m" not in item:
                    try:
                        edge = win.Dispatch(raw)
                        curve = win.Dispatch(call_method(edge, "GetCurve"))
                        # Cheap gate before the expensive call. `CircleParams` allocates a
                        # 7+ element array through COM for every edge that asks; on the track
                        # part (147 edges, 12 of them circles) that was 15.3 s of the 76 s
                        # total. `IsCircle` reads one boolean and skips the other 135.
                        _is_circle = True
                        try:
                            _raw_flag = curve.IsCircle
                            _is_circle = bool(_raw_flag() if callable(_raw_flag) else _raw_flag)
                        except Exception:
                            _is_circle = True     # unknown -> keep the old behaviour
                        if not _is_circle:
                            raise StopIteration
                        params = list(curve.CircleParams or ())
                        if len(params) >= 7:
                            centre_pt = [float(params[0]), float(params[1]), float(params[2])]
                            radius = float(params[6])
                            item["model_center"] = [round(v, 4) for v in centre_pt]
                            item["radius_m"] = round(radius, 6)
                            item["is_circle"] = True
                            item["model_start"] = [round(v, 4) for v in centre_pt]
                            item["model_end"] = [round(v, 4) for v in centre_pt]
                    except StopIteration:
                        item["is_circle"] = False
                    except Exception:
                        pass
            rows.append(item)

        # Project to sheet coordinates. `add_drawing_dimension` already does this for error
        # messages, so the transform exists — `model_*` alone cannot be used to aim at the
        # sheet, which is what a report hit ("用 index 定位这条路实际走不通").
        axis_map, projection_reason = _view_axis_map(target)
        projected = False
        # Direction from the view's own name. Before 0.8.207 this was an unchecked `flip_x`
        # argument because "the API cannot tell direction" — but `GetOrientationName` says
        # exactly where the eye is, and the sign follows from the handedness. The manual
        # argument still works and still overrides, so an unusual view can be corrected.
        _auto_flip_x = bool((axis_map or {}).get("flip_x"))
        _auto_flip_y = bool((axis_map or {}).get("flip_y"))
        effective_flip_x = bool(flip_x) != _auto_flip_x
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
        if projected and (effective_flip_x or _auto_flip_y):
            try:
                for _item in rows:
                    for _key in ("sheet_start", "sheet_end", "sheet_center"):
                        if not _item.get(_key):
                            continue
                        if effective_flip_x:
                            _item[_key][0] = round(2 * view_pos[0] - _item[_key][0], 5)
                        if _auto_flip_y:
                            _item[_key][1] = round(2 * view_pos[1] - _item[_key][1], 5)
            except Exception:
                pass

        # So this does NOT claim the direction is right. It says the direction is UNVERIFIED
        # and that the only trustworthy check is a pixel measurement of the exported page.
        # Callers that place things by sheet coordinate (dimensions, leaders, datum tags)
        # should confirm one known feature against the PDF first.
        # Same flag on every row as on the envelope: a caller that filters edges wants to
        # know per-edge whether the sheet coordinates are real, without cross-referencing
        # the payload. Report 2026-11-08 (P1-3) read `sheet_projected` per edge and got
        # None on every one.
        #
        # 0.8.293: "real" now means USABLE FOR SELECTION, not merely "a centre was
        # transformed". Four reports in a row measured a side view of five CIRCLES with
        # `sheet_start == sheet_end == sheet_center` while this flag said true. Circle
        # centres are still projected and still useful — see `sheet_coordinates_present` —
        # but `add_distance_dimension` cannot pick an edge with no distinct endpoints.
        _scope = _edge_projection_scope(rows)
        _effective_projected = bool(projected) and _scope["usable_for_edge_selection"]
        if not projected:
            _reason = projection_reason or "no projection inputs"
        elif _scope["circular_model_edges_only"]:
            _reason = "circular_model_edges_only"
        else:
            _reason = None
        for _row in rows:
            _row["edge_kind"] = _edge_kind(_row)
            _row["sheet_projected"] = _effective_projected
            if not _effective_projected:
                _row["sheet_projection_reason"] = _reason

        payload = {"drawing": doc_title, "view": target_name,
                   "edge_count": len(rows), "edges": rows,
                   "sheet_projected": _effective_projected,
                   "sheet_coordinates_present": _scope["sheet_coordinates_present"],
                   "edge_kinds": _scope["edge_kinds"],
                   "usable_edge_count": _scope["usable_edge_count"],
                   "usable_for_edge_selection": _scope["usable_for_edge_selection"],
                   # Verified when the direction came from the view's orientation name
                   # (or the caller forced it). Only sizes matched by outline are unverified.
                   "sheet_axis_direction_verified": bool(
                       _auto_flip_x or _auto_flip_y or flip_x
                       or (axis_map or {}).get("source") == "orientation_name"),
                   "sheet_flipped_x": bool(effective_flip_x),
                   "sheet_flipped_y": bool(_auto_flip_y),
                   "sheet_axis_basis": ((axis_map or {}).get("source")
                                        or ("caller_flip_x" if flip_x else None)),
                   "sheet_axis_warning": (
                       "sheet 坐标方向来自视图名（%s），不是像素核对。标准视图按右手系"
                       "推导：*右视 看 -X、屏幕向右是 +Z；*左视 看 +X、屏幕向右是 -Z。"
                       "如果这张图在 SolidWorks 里被手工旋转过，方向可能不符，"
                       "届时传 flip_x=True 覆盖。" % ((axis_map or {}).get("orientation") or "未知")
                       if projected and not bool(flip_x) and
                       (axis_map or {}).get("source") != "orientation_name" else None)}
        if not _effective_projected:
            payload["sheet_projection_reason"] = _reason
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
                          save=True, prefix=""):
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
        # ── Ø / R prefix (report 2026-11-08, P2-3) ──────────────────────────────────
        # A groove's mouth is a real diameter but between two arcs `AddDimension2` yields a
        # plain length, so a turned part ended up with a bare `58` where the drawing needs
        # `Ø58`. `IDisplayDimension.SetText(1, prefix)` sets the prefix — measured
        # 2026-11-08: SetText(1..4) each read back through GetText(1..4).
        #
        # `dir(IDisplayDimension)` does not list `SetText` either; it is late-bound, the
        # same blind spot that hid `ISketchSegment.Style`. Tried first, not assumed absent.
        prefix_applied = None
        _want_prefix = str(prefix or "")
        if _want_prefix:
            try:
                display.SetText(1, _want_prefix)
                prefix_applied = str(display.GetText(1) or "")
            except Exception as _pfx_exc:                            # noqa: BLE001
                return error("prefix_failed",
                             "could not set the dimension prefix %r: %s"
                             % (_want_prefix, str(_pfx_exc)[:80]),
                             "The dimension was created WITHOUT the prefix — check the "
                             "figure before shipping, or delete it and retry.")
        value_mm = None
        value_deg = None
        dimension_type = None
        try:
            dim2 = win.Dispatch(call_method(display, "GetDimension2", 0))
            value_mm = round(float(dim2.SystemValue) * 1000.0, 3)
            # Callers need to know whether this is a LENGTH. Between two non-parallel edges
            # `AddDimension2` creates an angular dimension whose SystemValue is radians;
            # ×1000 that becomes 1570.796 for a right angle, which reads as a length and
            # sent this project chasing a phantom for 35 versions.
            try:
                dimension_type = int(dim2.GetType)
            except Exception:
                try:
                    dimension_type = int(dim2.Type)
                except Exception:
                    dimension_type = None
            # 0.8.308 (report 2026-11-12 §4): between two NON-PARALLEL edges the very same
            # call yields an ANGULAR dimension, whose SystemValue is radians. Reporting
            # `value_mm: 785.398` for 45° is the bug this whole release line started from.
            if _is_angular_dimension_type(dimension_type):
                value_deg = _radians_to_deg(dim2.SystemValue)
                value_mm = None
        except Exception:
            pass
        try:
            _call_method(doc, "EditRebuild3")
            perf_hooks.bump("rebuild_calls")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
        except Exception:
            pass
        if save:
            _save_drawing(doc)

        return drawing_ok(
            drawing=doc.GetTitle, view=str(target.Name),
            edge_a=int(edge_a), edge_b=int(edge_b),
            value_mm=value_mm,
            # 0.8.308: angular results carry `value_deg` and NO `value_mm`; a field named
            # `_mm` must never hold degrees (report 2026-11-12 §4).
            value_deg=value_deg,
            dimension_type=dimension_type,
            # Requested vs applied: `SetText` can fail on some builds, and a Ø that did not
            # land reads as a plain length to anyone checking the sheet.
            prefix_requested=str(prefix or "") or None,
            prefix_applied=prefix_applied,
            value_is_angular=(dimension_type in (1, 3)) if dimension_type is not None else None,
            position_m=[round(float(x_m), 4), round(float(y_m), 4)],
            note=("这是两条边之间的距离，不是任何一条边的长度。"
                  "总长这类「由多段相加得到」的尺寸只能这样标。"
                  "value_mm 是实际生成的值——请与期望总长核对，不要假定选对了边。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("distance_dimension_failed", str(exc))


def add_angular_dimension(drawing_path, edge_a, edge_b, x_m, y_m, view_name=None,
                          expect_deg=None, tolerance_deg=0.5, save=True):
    """Create a NATIVE, associative angular dimension between two edges.

    Report 2026-11-12 §4.4: the only way to get 45° onto the sheet was to DELETE the
    imported annotation and type the text, which loses the association — edit the model and
    the sheet stops following. This selects two edges and calls `AddDimension2` (the same
    mechanism as `add_distance_dimension`), then CHECKS the result is angular before
    reporting success: between two parallel edges that identical call yields a LENGTH, and
    shipping one labelled "angle" is worse than refusing.

    `expect_deg` is optional and strongly recommended for a chamfer: `verified` says
    whether the created value matches it within `tolerance_deg`. A silently wrong angle is
    invisible on the sheet.

    Angles are the ONE dimension kind whose raw `SystemValue` is RADIANS, so this returns
    `value_deg` and always `value_mm: null` (report 2026-11-12 §4).
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
                "angular_dimension_failed", "AddDimension2 returned null",
                "It needs exactly two compatible entities selected. Pick the two edges "
                "that actually bound the angle you want.")
        display = win.Dispatch(raw)

        dim_type = None
        value_deg = None
        try:
            dim2 = win.Dispatch(call_method(display, "GetDimension2", 0))
            try:
                dim_type = int(dim2.GetType)
            except Exception:
                try:
                    dim_type = int(dim2.Type)
                except Exception:
                    dim_type = None
            if _is_angular_dimension_type(dim_type):
                value_deg = _radians_to_deg(dim2.SystemValue)
        except Exception:
            pass

        if not _is_angular_dimension_type(dim_type):
            # Wrong KIND: undo it. A stray length dimension on the sheet reads as a real
            # measurement and is hard to attribute later.
            reverted = False
            try:
                ann = win.Dispatch(call_method(display, "GetAnnotation"))
                ann.Select(False)
                _call_method(doc, "EditDelete")
                _call_method(doc, "EditRebuild3")
                reverted = True
            except Exception:
                reverted = False
            try:
                doc.GraphicsRedraw2()
            except Exception:
                pass
            if save:
                _save_drawing(doc)
            return error(
                "not_angular",
                "AddDimension2 created a dimension of type %r, not an angle" % (dim_type,),
                "The two edges are parallel (or the selection degenerated), so SolidWorks "
                "produced a LENGTH. Pick the two lines that bound the angle.",
                created=False, reverted=reverted, dimension_type=dim_type,
                edge_a=int(edge_a), edge_b=int(edge_b))

        verified = None
        if expect_deg is not None and value_deg is not None:
            verified = abs(float(value_deg) - float(expect_deg)) <= float(tolerance_deg)

        try:
            _call_method(doc, "EditRebuild3")
            perf_hooks.bump("rebuild_calls")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
        except Exception:
            pass
        if save:
            _save_drawing(doc)

        return drawing_ok(
            drawing=doc.GetTitle, view=str(target.Name),
            edge_a=int(edge_a), edge_b=int(edge_b),
            created=True, reverted=False,
            # `value_deg` is the whole point: 45°, not 785.4.
            value_deg=value_deg,
            value_mm=None,
            value_is_angular=True,
            dimension_type=dim_type,
            expect_deg=(None if expect_deg is None else round(float(expect_deg), 3)),
            tolerance_deg=float(tolerance_deg),
            verified=verified,
            position_m=[round(float(x_m), 4), round(float(y_m), 4)],
            prefix_requested=None, prefix_applied=None,
            ok=(verified is not False),
            note=("原生关联角度尺寸 —— 编辑模型后图面会跟着更新。角度是唯一"
                  "`SystemValue` 用弧度的尺寸类型，所以 `value_mm` 恒为 null、角度在 "
                  "`value_deg`。`verified: null` 表示没传 `expect_deg`，不是验证通过；"
                  "`not_angular` 表示两条边平行、已被撤销。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("angular_dimension_failed", str(exc))


def add_drawing_dimension(drawing_path, x=None, y=None, value=None, offset_x=0.0,
                          offset_y=-0.015, view_name=None, tolerance=0.01,
                          edge_index=None, skip_duplicates=False, known_values=None,
                          save=True, style="auto"):
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
        # Identity snapshot, kept only when the duplicate check is on. `IDisplayDimension`
        # has no way to be selected or deleted — `GetAnnotation` is not in the type library
        # and `Select` is not on it either (measured 2026-11-08) — so the just-inserted
        # duplicate has to be found by DIFF against a snapshot taken before the insert.
        # That is the same mechanism 0.8.216 verified for the rollback path.
        _dims_before = None
        if skip_duplicates:
            try:
                _dims_before = {n for n, _ in (_dimension_selection_names(doc) or [])}
            except Exception:
                _dims_before = None
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
                # ── "view has no edges" is NOT the only reason (report 2026-11-08, P2-1) ─
                # `_nearest_edge_at_sheet_point` returns the nearest edge even when it is
                # outside tolerance, so None means one of exactly two things: the view
                # enumerates no edges, or its scale reads 0. The report saw this message on
                # a view whose `list_drawing_edges` answered 14 edges — the point sat on a
                # CENTRE LINE, which `GetVisibleEntities2(view, 1)` does not enumerate
                # (that type only covers edges). The caller then concluded the view was
                # empty and gave up on dimensions for it.
                #
                # Count the edges here so the two cases stop sharing one sentence.
                _edge_n = None
                try:
                    import win32com.client as _wc
                    _edge_n = len([_wc.Dispatch(e)
                                   for e in (view.GetVisibleEntities2(view, 1) or ())])
                except Exception:
                    _edge_n = None
                _scale_n = None
                try:
                    _scale_n = float(view.ScaleDecimal)
                except Exception:
                    _scale_n = None
                if _edge_n:
                    return error(
                        "point_missed_every_edge",
                        "视图里有 %d 条可见边，但点 (%.4f, %.4f) 离它们都超过 %.1f mm"
                        % (_edge_n, float(x), float(y), float(tolerance) * 1000),
                        "这个点可能落在**中心线、构造圆、或空白的视图范围内**——"
                        "`GetVisibleEntities2(view, 1)` 只枚举「边」，不枚举中心线和草图段，"
                        "所以落在中心线圆上的点永远配不中边。"
                        "改用 `edge_index`（`list_drawing_edges` 每条边都带 `edge_index`），"
                        "或把点移到某条边附近。视图本身没问题，边数已回传。",
                        visible_edges=_edge_n, scale=_scale_n,
                        point=[round(float(x), 4), round(float(y), 4)],
                        tolerance_m=float(tolerance))
                return error(
                    "segment_not_found",
                    "No visible edges could be enumerated in this view "
                    "(edges=%r, scale=%r)" % (_edge_n, _scale_n),
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
        # ── R / Ø (report 2026-11-07, P1-2) ─────────────────────────────────────────
        # A semicircular groove projects as an ARC, and `AddDimension2` measures it as a
        # diameter — `Ø20` for an R10 groove, which is a meaningless callout on a turned
        # part (the report's exact case). `AddRadialDimension2` exists and works; it was
        # simply not wrapped.
        #
        # Measured 2026-11-07 on 飞达动力轴 (Ø35 shaft): selecting the outline and calling
        # `AddRadialDimension2` returned 17.50 mm — the radius, not the diameter.
        #
        # Falls back to `AddDimension2` when `style` is "auto" (previous behaviour) or when
        # the radial call returns null, and the receipt says which one happened so a caller
        # never has to guess whether the R prefix is real.
        _style = str(style or "auto").lower()
        if _style not in ("auto", "radius", "diameter", "linear"):
            return error("bad_style", "style must be auto / radius / diameter / linear",
                         "Got %r" % (style,))
        _dim_x = base_x + float(offset_x)
        _dim_y = base_y + float(offset_y)
        display_dim = None
        _style_used = "linear"
        if _style == "radius":
            try:
                display_dim = doc.AddRadialDimension2(_dim_x, _dim_y, 0)
                if display_dim is not None:
                    _style_used = "radius"
            except Exception:
                display_dim = None
        elif _style == "diameter":
            try:
                display_dim = doc.AddDiameterDimension2(_dim_x, _dim_y, 0)
                if display_dim is not None:
                    _style_used = "diameter"
            except Exception:
                display_dim = None
        if display_dim is None:
            display_dim = doc.AddDimension2(_dim_x, _dim_y, 0)
            _style_used = "linear"
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
        #
        # Report 2026-11-08 (P0-2), TWO defects in this one check:
        #
        # 1. It compared `picked_length` — the EDGE's projected length — against the sheet.
        #    With `style="radius"` on an arc, the edge measures Ø20 but the dimension
        #    created is R10, so the test said "20 already exists" (the groove WIDTH is 20)
        #    while the thing actually being added was a different value. The dimension that
        #    landed was therefore not a duplicate at all, and it was reported as skipped.
        #
        # 2. The dimension had ALREADY been created by this point, and the "skip" path did
        #    not remove it. `created: false, skipped: "duplicate_value"` while a second R10
        #    sits on the sheet next to the first — the report had to delete it by hand.
        #
        # So: compare the value that ACTUALLY landed, and if the verdict is "duplicate",
        # undo the insert before returning.
        _landed_value = None
        try:
            _landed_value = float(dim.SystemValue)
        except Exception:
            _landed_value = picked_length
        if (skip_duplicates and _landed_value is not None
                and round(_landed_value * 1000) in existing):
            _undone = False
            try:
                _snap_now = _dimension_selection_names(doc) or []
                _fresh = [(n, a) for n, a in _snap_now
                          if _dims_before is not None and n not in _dims_before]
                for _nm, _ann in _fresh:
                    try:
                        doc.ClearSelection2(True)
                        if _ann.Select(False):
                            _call_method(doc, "EditDelete")
                            _undone = True
                    except Exception:
                        continue
            except Exception:
                _undone = False
            doc.ClearSelection2(True)
            try:
                _call_method(doc, "EditRebuild3")
            except Exception:
                pass
            return drawing_ok(
                drawing=doc.GetTitle, created=False, selected=selected,
                view=view.Name if view is not None else None,
                value_m=float(_landed_value), skipped="duplicate_value",
                # Whether the just-created duplicate was actually removed. A False here
                # means the sheet now carries a duplicate that this call created — the
                # caller must clean it up, and must not be told "skipped" without that.
                duplicate_removed=_undone,
                note=("The same dimension already exists on this sheet."
                      if _undone else
                      "The same dimension already exists AND the duplicate this call "
                      "created could not be removed — delete it by hand."),
            )

        doc.ClearSelection2(True)
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
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
            # Which callout form was actually created. `style="radius"` can fall back to a
            # linear dimension when SolidWorks refuses the radial call, and a caller that
            # assumes the R prefix landed would ship a wrong callout.
            style_requested=_style,
            style_used=_style_used,
            note=("Verify the dimension text and its attachment, then export a PDF."
                  + (" SolidWorks refused the radial call and a linear dimension was "
                     "created instead — check the figure before shipping."
                     if _style in ("radius", "diameter") and _style_used == "linear"
                     else "")),
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


_SAFE_AREA_CACHE = {}


def template_safe_area(sheet, sheet_w, sheet_h):
    """Usable region of a sheet: inside the frame, clear of the title block / parts list.

    GB templates vary with paper size, so the margins are measured from the template's
    own lines instead of being hard-coded for A3 (which is why A2 layouts overflowed).

    Memoised per sheet identity: `_template_lines` walks every sketch segment on the sheet
    (measured as the dominant cost of `template_safe_area`), and callers such as
    `_push_dimensions_outside` ask once per candidate frame. Measured 2026-11-06: an
    assembly smoke item went from 41 s to 316 s after the safe-area clamp was switched from
    the paper inset to this function — the geometry never changes within a call, so the read
    does not need to repeat.
    """
    try:
        _key = (str(getattr(sheet, "Name", "") or ""), round(float(sheet_w), 6),
                round(float(sheet_h), 6))
    except Exception:
        _key = None
    if _key is not None and _key in _SAFE_AREA_CACHE:
        return _SAFE_AREA_CACHE[_key]
    _result = _template_safe_area_impl(sheet, sheet_w, sheet_h)
    if _key is not None:
        _SAFE_AREA_CACHE[_key] = _result
        if len(_SAFE_AREA_CACHE) > 32:                  # a long session must not grow
            _SAFE_AREA_CACHE.pop(next(iter(_SAFE_AREA_CACHE)))
    return _result


def _template_safe_area_impl(sheet, sheet_w, sheet_h):
    """Uncached body of `template_safe_area`. See that function for the contract."""
    segments = _template_lines(sheet, sheet_w, sheet_h)
    fallback = {
        "left": sheet_w * 0.14, "right": sheet_w * 0.01,
        "top": sheet_h * 0.012, "bottom": sheet_h * 0.22,
        "source": "ratio",
        # A ratio guess is not a measurement. Saying so lets `add_note` / `measure_layout`
        # show the caller that the layout check ran against a guess.
        "degraded": True,
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
        "degraded": False,
    }

    # ── Reject a degenerate scan (report 2026-11-08, P0-A) ──────────────────────────
    # Re-opening the SAME drawing made this return `bottom 0.065 -> 0.004`,
    # `right 0.004 -> -0.016`, `title_block_top 0.061 -> 0.0` — while still saying
    # `source: "template"`. So the caller could not tell a good measurement from a
    # collapsed one, and `add_note` at the same coordinates went from refused to accepted
    # depending on whether the process had created the drawing or merely re-opened it.
    #
    # The three signatures below are physically impossible for a real GB template:
    #   right < 0                    the frame is wider than the sheet
    #   title_block_top == frame_bottom   no title block was found at all
    #   bottom < 2 mm                the reserved band vanished
    # Any one of them means the scan failed, so fall back to ratios and SAY SO.
    _degenerate = []
    if measured["right"] < 0:
        _degenerate.append("right=%.4f<0" % measured["right"])
    if abs(title_top - frame_bottom) < 1e-6:
        _degenerate.append("title_block_top==frame_bottom (not found)")
    if measured["bottom"] < 0.002:
        _degenerate.append("bottom=%.4f<0.002" % measured["bottom"])
    if _degenerate:
        _fb = dict(fallback)
        _fb["source"] = ("ratio (template scan degenerate: %s)"
                         % "; ".join(_degenerate))
        _fb["degraded"] = True
        _fb["degenerate_reasons"] = _degenerate
        return _fb

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
            perf_hooks.bump("redraw_calls")
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

    **CORRECTION 2026-11-07**: the paragraph above is wrong. `IView.GetOrientationName`
    exists and answers exactly this question:

        工程图视图1  '*前视'    -> the view shows Y-Z
        工程图视图2  '*上视'    -> the view shows X-Z
        工程图视图3  '*右视'    -> the view shows Y-Z  (mirrored)

    It returns `''` for anything that is not a standard view (a rotated or custom view), so
    the size match below is kept as the FALLBACK, not as the primary path. That removes the
    `axes_mismatch` refusal for every standard view — including the cube that made the size
    match ambiguous, because a cube's front view is still named `*前视`.

    So: read the orientation name first; match by size only when it is empty.
    Returns (info, reason); info carries axes / centre / extent / path.
    An ambiguous size match (e.g. a cube with no standard orientation) is still refused.

    The caller must re-activate the DRAWING afterwards: reading the part box goes through
    open_document(model_path), which switches SolidWorks' active document.
    """
    from ..sw_session import call_method

    # Fast path: standard views name their own projection.
    #
    # `axes` are MODEL INDEXES (0=X, 1=Y, 2=Z), matching the `pairs` table below — not
    # letters. Front looks down -Z so the sheet shows X-Y; top looks down -Y so it shows
    # X-Z; right looks down -X so it shows Z-Y (that is why the third pair is (2,1) and not
    # (1,2): the model's Z runs across the sheet).
    _std_axes = None
    try:
        _name = str(call_method(view, "GetOrientationName") or "")
    except Exception:
        try:
            _name = str(view.GetOrientationName or "")
        except Exception:
            _name = ""
    if _name:
        _std = {"*前视": (0, 1), "*后视": (0, 1),
                "*上视": (0, 2), "*下视": (0, 2),
                "*右视": (2, 1), "*左视": (2, 1)}
        _std_axes = _std.get(_name)
        # ── Direction, not just which axes ──────────────────────────────────────────
        # The old comment below claimed the direction was unknowable from the API. It is
        # not: the view's own name says where the eye is, and the handedness follows.
        #
        # Measured 2026-11-07 (飞达动力轴, front + right + left): comparing model Z against
        # sheet X gave dZ/dX = +/+ for BOTH the right view and the left view. That cannot
        # be physical — one looks along -X, the other along +X, so the model's +Z must fall
        # on opposite sides. Both were using the same unsigned projection.
        #
        # Right-handed derivation, up = +Y throughout:
        #   *右视  forward = -X  ->  screen right = up × forward = +Z   (no flip)
        #   *左视  forward = +X  ->  screen right = -Z                 (flip X)
        #   *前视  forward = -Z  ->  screen right = +X                 (no flip)
        #   *后视  forward = +Z  ->  screen right = -X                 (flip X)
        #   *上视  forward = -Y  ->  screen right = +X, screen up = -Z (flip Y)
        #   *下视  forward = +Y  ->  screen right = +X, screen up = +Z (no flip)
        _std_flip = {"*前视": (False, False), "*后视": (True, False),
                     "*上视": (False, True), "*下视": (False, False),
                     "*右视": (False, False), "*左视": (True, False)}
        _std_flip_x, _std_flip_y = _std_flip.get(_name, (False, False))
    else:
        _std_flip_x = _std_flip_y = False

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

    if _std_axes is not None:
        # The view named its own projection, so the size match below is unnecessary — but
        # the receipt must still carry the model box, because `_circular_edge_in_view`
        # subtracts `centre_m` to place a circle centre on the sheet.
        return {
            "axes": [int(_std_axes[0]), int(_std_axes[1])],
            "centre_m": centre,
            "extent_m": extent,
            "model_path": model_path,
            "outline_m": [width, height],
            "orientation": _name,
            "source": "orientation_name",
            "flip_x": bool(_std_flip_x),
            "flip_y": bool(_std_flip_y),
        }, None

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


# Above this many edge pairs matching one gap value, a distance-only match has no
# discriminating power — see the `low_confidence` branch in `plan_dimension_gaps`
# (report 2026-11-07, C8: a 50 mm gap on 接槽 matched 696 pairs).
LOW_CONFIDENCE_MATCH_LIMIT = 50

SECTION_KEEP_TOLERANCE = 0.10

# Report 2026-11-10 (P0): an eccentric chord of a revolved part shrinks the dimension
# that `kept_dimension` THROWS AWAY. Measured on 凹槽滚轮 (Ø64×26, front view 2:1):
#
#     offset_m=0     source [51.98, 127.98]  section [51.98, 127.98]  ratio 1.000
#     offset_m=0.004 source [51.98, 127.98]  section [51.98, 127.73]  ratio 1.000
#
# `horizontal` keeps the width (51.98, constant by construction) and discards the height,
# where the 0.25 mm chord loss lives. A PROPORTIONAL threshold cannot catch this:
# 0.25 / 127.98 = 0.195%, well under the 0.5% the old check advertised. Hence an absolute
# millimetre threshold on the non-kept axis. A correct through-axis cut matches to
# rounding (0.00 mm measured on both parts), so 0.1 mm leaves real headroom.
SECTION_OFF_AXIS_TOLERANCE_MM = 0.1

# Report 2026-11-12 §2 (P0 regression): a cut plane THROUGH the axis cannot produce a
# chord, so a measured eccentricity at or below this gate must never be reported as one.
# 0.5 mm on the model: at Ø64 a 0.5 mm chord shortens the diameter by 0.004 mm, far below
# `SECTION_OFF_AXIS_TOLERANCE_MM` anyway — this gate only removes false positives, it
# cannot hide a real eccentric chord.
SECTION_ECCENTRICITY_GATE_MM = 0.5


def _section_sanity(source_view, section, orientation, depth,
                    hatch_count=None, edge_count=None, source_edge_count=None):
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
        "hatch_count": hatch_count,
        "edge_count": edge_count,
        "source_edge_count": source_edge_count,
    }
    # Report 2026-11-07 (P0-1): `kept_dimension` is TRUE BY CONSTRUCTION for a view that
    # never cut anything. An uncut projection has the source view's exact extent, so the
    # width test passes trivially and the receipt said `verdict: "ok"` on a section that
    # was visually identical to its parent (`edge_count 4` vs the parent's 4, no hatching
    # at all). Two independent signals were available and neither was consulted:
    #
    #   hatch_count == 0        the cut region produced no hatching
    #   edge_count == source's  nothing was cut open — the same edges came through
    #
    # Both now downgrade the verdict. They are reported, not refused: a section can
    # legitimately look like its parent in degenerate cases, and the caller is better
    # served by a named doubt than by a hard error.
    if hatch_count == 0:
        info["verdict"] = "no_hatch"
        info["verdict_reason"] = (
            "剖面线数量为 0：剖切区域没有产生剖面线。未真正剖开的投影视图也满足"
            "上面的宽度判据，所以这一项必须单独报告。")
    elif (edge_count is not None and source_edge_count is not None
          and edge_count > 0 and edge_count == source_edge_count):
        info["verdict"] = "identical_to_source"
        info["verdict_reason"] = (
            "剖视图的边数（%d）与父视图相同：内腔/孔没有剖出来，多半是一个未剖切的"
            "投影。回转体请改从端视图用 orientation='vertical' 剖切。" % edge_count)
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
    # Report 2026-11-10 (P0): measure the OTHER axis too. `section_off_axis` was named
    # after this failure but only ever compared `kept_dimension` — on a revolved part an
    # eccentric chord leaves that axis untouched and shortens the discarded one, so the
    # check was structurally blind to the exact case it was written for. Both axes are now
    # in the receipt; the caller (add_section_view) decides on the non-kept one.
    #
    # This is reached only for a full-depth longitudinal section: `depth>0` (partial
    # section, legitimately smaller) and `cross_section` (smaller in BOTH axes) returned
    # above with `checked=False`, so a chord is not confused with either of those.
    if str(orientation) == "vertical":
        off_src, off_sec, off_which = src_w, sec_w, "width"
    else:
        off_src, off_sec, off_which = src_h, sec_h, "height"
    info["off_axis_dimension"] = off_which
    info["source_off_axis_mm"] = round(off_src * 1000, 2)
    info["section_off_axis_mm"] = round(off_sec * 1000, 2)
    # `-0.0` appears when the two values are equal (report 2026-11-12 §3-6) and reads as a
    # negative measurement. Clamp the sign of zero; a real shrink keeps its value.
    _off_shrink = round((off_src - off_sec) * 1000, 3)
    info["off_axis_shrink_mm"] = 0.0 if _off_shrink == 0 else _off_shrink
    # Only when nothing more specific was found above. This line runs AFTER the hatch and
    # edge-count checks, and used to overwrite them unconditionally — `no_hatch` came out
    # of the function as `ok`, which is exactly the failure the check was added to name.
    # Measured 2026-11-07 on 小车底部滚轮: hatch_count 0, edge_count 4 (parent 4), and the
    # receipt still said verdict "ok" until this guard was added.
    if "verdict" not in info:
        info["verdict"] = ("ok" if abs(ratio - 1.0) <= SECTION_KEEP_TOLERANCE
                           else "suspicious")
    return info


def _position_deviation(requested, actual, tol=1e-3):
    """(rewritten, axis) between a REQUESTED sheet position and the ACTUAL one.

    PURE, so smoke can replay report 2026-11-10 P2-1: a call used to report
    `position_axis_ignored: "x"` beside two IDENTICAL coordinates, because the axis was
    computed before an alignment break while `position_actual_m` was re-read after it.
    Both numbers were true at different moments; the receipt cannot say that, so every
    position field is now derived from the final placement by this one function.

    Returns `(None, None)` when either side is unknown — `null` means "not compared",
    never "matched".
    """
    if requested is None or actual is None:
        return None, None
    dx = abs(float(actual[0]) - float(requested[0]))
    dy = abs(float(actual[1]) - float(requested[1]))
    if dy > tol:
        return True, "y"
    if dx > tol:
        return True, "x"
    return False, None


def _section_off_axis_from_sanity(sanity, eccentricity_mm=None):
    """Decide whether a finished section is an eccentric chord. PURE — no COM.

    Extracted from `add_section_view` (report 2026-11-10, P0) so the decision can be
    exercised without SolidWorks. `sanity` is exactly the dict `_section_sanity` returns,
    which makes this a real unit of the geometry rule rather than a string in a receipt.

    Three guards, in this order:

      0. `eccentricity_mm` — measured distance from the cut plane to the axis. A plane
         THROUGH the axis cannot produce a chord, so a known value at or below the gate
         returns "not eccentric" immediately. Without this, report 2026-11-12 §2 measured
         a CORRECT end-view vertical section being rejected: the source view is a Ø64
         circle (`source_size_mm [127.98, 127.98]`) and the section is the axial slice
         (`[51.98, 127.98]`), so "width shrank by 76 mm" was the diameter being replaced
         by the part's length — while the SAME receipt carried `eccentricity_mm: 0.0`,
         `ratio: 1.0` and 28 edges / hatch 2.
      1. `kept_dimension` — the axis a perpendicular cut CANNOT change. A > 0.5% shrink
         here means the plane missed the intended cross-section (the 2026-11-08 case).
         On a CIRCULAR source this is the axis an eccentric chord actually shortens.
      2. `off_axis_dimension` — the axis the cut CAN shorten. Meaningful only when both
         axes keep their meaning across the cut, so it is SKIPPED for a square source view
         (circle / square profile), where one axis becomes the part's axial length.

    Only `checked=True` full sections are judged: partial sections (depth>0) shrink by
    design and cross-sections are smaller in both axes by definition — both return from
    `_section_sanity` with `checked=False`.
    """
    if not isinstance(sanity, dict) or not sanity.get("checked"):
        return None

    # Guard 0: measured eccentricity, not a guess. `null` means "not known" and falls
    # through to the geometry tests below — absence must never be read as zero.
    try:
        if eccentricity_mm is not None and \
                abs(float(eccentricity_mm)) <= SECTION_ECCENTRICITY_GATE_MM:
            return None
    except (TypeError, ValueError):
        pass

    kept = sanity.get("section_kept_mm")
    src = sanity.get("source_kept_mm")
    try:
        if kept is not None and src:
            shrunk = float(src) - float(kept)
            if shrunk > max(0.005 * float(src), 0.2):
                return {
                    "shrunk_mm": round(shrunk, 3),
                    "source_mm": round(float(src), 3),
                    "section_mm": round(float(kept), 3),
                    "ratio": round(float(kept) / float(src), 4),
                    "dimension": sanity.get("kept_dimension"),
                    "detected_on": "kept_dimension",
                }
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    # Guard 1: a square source view replaces one dimension with the axial length. The
    # kept-axis test above still catches a chord on a circular source, because there the
    # chord shortens the KEPT axis — so skipping this pair loses nothing.
    _src_size = sanity.get("source_size_mm") or ()
    try:
        _square_source = (len(_src_size) >= 2 and _src_size[0] and _src_size[1]
                          and abs(float(_src_size[0]) - float(_src_size[1])) <= 1e-6)
    except (TypeError, ValueError, IndexError):
        _square_source = False
    if _square_source:
        return None

    raw = sanity.get("off_axis_shrink_mm")
    try:
        shrink = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        shrink = None
    o_src = sanity.get("source_off_axis_mm")
    o_sec = sanity.get("section_off_axis_mm")
    if (shrink is not None and shrink > SECTION_OFF_AXIS_TOLERANCE_MM
            and o_src and o_sec is not None):
        return {
            "shrunk_mm": round(shrink, 3),
            # Report 2026-11-12 §3-2: these used to be called `source_kept_mm` /
            # `section_kept_mm` while holding the OFF-AXIS values, so the receipt named
            # the wrong dimension. Neutral names now; `sanity` keeps its own pair.
            "source_mm": o_src,
            "section_mm": o_sec,
            "ratio": round(float(o_sec) / float(o_src), 4),
            "dimension": sanity.get("off_axis_dimension"),
            "detected_on": "off_axis_dimension",
            "tolerance_mm": SECTION_OFF_AXIS_TOLERANCE_MM,
        }
    return None


def add_section_view(drawing_path, view_name=None, orientation="horizontal",
                     offset_m=0.0, position=None, label="A", flip=False, depth=0.0,
                     cut_plane_normal=None, cut_plane_origin=None, verify_cut=True,
                     cut_line_ratio=0.55, align_to_parent=True,
                     auto_nudge_zero_offset=False, keep_on_failure="auto",
                     cut_line_overshoot_m=0.004):
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
            # What the caller asked for vs what was actually cut. `offset_m=0` cannot be
            # cut on a symmetric view (see the note where the line is drawn), so the tool
            # nudges it to 1 mm and says so here rather than silently cutting a section at
            # a different place than requested.
            "offset_requested_m": round(_offset_requested, 6),
            "offset_used_m": round(float(offset_m), 6),
            "offset_nudged": bool(_offset_nudged),
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
        # ── The cut line MUST overshoot the outline (report 2026-11-08, P0-1) ────────
        # `min(view_w, view_h) * 0.5` puts the line's endpoints EXACTLY on the outline of a
        # symmetric view: a 65x65 view gets a half-length of 32.5 mm, which is precisely the
        # radius. SolidWorks treats that as a line that does not cross the solid and returns
        # a plain projection — 5 contour edges, no hatching, `section_not_cut`.
        #
        # That is why `offset=0` was a dead point on 限位导向套 (Ø65 circle) and why nudging
        # the offset "worked": it moved the endpoints off the circle. The offset was never
        # the problem, so nudging it was treating a symptom — and on a revolved part it
        # produces a CHORD (offset 5 mm on a Ø16 bore gives a 12.5 mm section).
        #
        # A few millimetres of overshoot fixes the cause: the line then crosses the outline
        # on both sides no matter where the centre is, and `offset=0` becomes a normal cut.
        _overshoot = max(0.0, float(cut_line_overshoot_m))
        half = (min(max(view_w, view_h) * float(cut_line_ratio),
                    min(view_w, view_h) * 0.5) + _overshoot) / scale
        # ── Zero offset cannot cut (measured 2026-11-08) ────────────────────────────
        # `offset_m=0` puts the cut line exactly on the view centre. Measured on
        # 限位导向套, front view, horizontal cut:
        #
        #     offset 0 / 1e-6 / 1e-5 / 1e-4   hatch_missing  edges 5  hatch 0
        #     offset 1e-3                     ok  edges 46  hatch 4  ratio 1.000
        #     offset 4e-3                     ok  edges 34  hatch 2  ratio 0.992
        #
        # So it is neither a zero-length line (the segment spans the view) nor a
        # floating-point equality problem (1e-4 fails too). SolidWorks does not cut when
        # the line sits on the centre line of a symmetric view.
        #
        # `1e-3` sheet metres is 0.25-0.5 mm on the model at 2:1-4:1 — the section came back
        # 64.95 wide against a 64.98 source, i.e. through the axis to drawing tolerance.
        #
        # Nudge rather than refuse: asking for the axis cut is the normal request, so
        # refusing would break every caller. The receipt says which offset was used.
        _offset_requested = float(offset_m)
        _offset_nudged = False
        if abs(_offset_requested) < 1e-9 and bool(auto_nudge_zero_offset):
            offset_m = 1e-3
            _offset_nudged = True
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
        # Named once, so every failure branch below can tell the caller WHICH view it left
        # on the sheet. `section_not_cut` used to say "the view was created but nothing was
        # cut" and stop there — the reader then found an empty `A-A` sitting in the layout
        # with no way to know it was this call that put it there.
        _new_view_name = str(getattr(section, "Name", "") or "")
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
            perf_hooks.bump("redraw_calls")
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

        # P0-2 of a 2026-11-04 report asked for enough context to tell THREE different
        # causes apart, which the single count cannot:
        #
        #   a) the template has region hatching switched off globally
        #   b) the cut face has no closed region (a grazing cut through a wall)
        #   c) the view is displaying as a projection rather than a true section
        #
        # None of these is repairable through the API (see the note above about
        # SetAutoHatch / ScaleHatchPattern / SectionDepth all leaving the count at 0), so
        # the value here is diagnosis, not repair. Collected only when hatching is absent:
        # these are extra COM reads and this function already runs on every section.
        hatch_diag = None
        if hatch_count == 0:
            hatch_diag = {}
            for label_key, getter in (
                    ("template", lambda: str(doc.GetPathName)),
                    ("display_mode", lambda: int(section.GetDisplayMode2)),
                    ("style", lambda: int(section.GetDisplayMode)),
                    ("use_section_hatch", lambda: bool(section.GetUseSectionHatch)),
                    ("auto_hatch", lambda: bool(section.GetAutoHatch)),
                    ("scale_hatch", lambda: bool(section.GetScaleHatchPattern)),
                    ("section_depth_m", lambda: round(float(section.GetSectionDepth), 6)),
            ):
                try:
                    hatch_diag[label_key] = getter()
                except Exception as exc:
                    hatch_diag[label_key] = "unavailable: %s" % str(exc)[:40]
            # Per-face counts: a section through several bodies should hatch each cut face.
            # All zero means the pattern is off; SOME zero and some not means geometry.
            try:
                faces = section.GetSectionedFaces
                counts = []
                if faces:
                    for face in (faces if isinstance(faces, (tuple, list)) else [faces]):
                        try:
                            counts.append(int(face.GetFaceHatchCount))
                        except Exception:
                            counts.append(None)
                hatch_diag["per_face_hatch"] = counts
                hatch_diag["face_count"] = len(counts)
            except Exception as exc:
                hatch_diag["per_face_hatch"] = "unavailable: %s" % str(exc)[:40]

        # `_section_sanity` needs the hatch and edge counts now: an uncut projection
        # reproduces the parent's edge count exactly and produces no hatching, and neither
        # signal was consulted before (report 2026-11-07, P0-1).
        try:
            _source_edge_count = _view_edge_count(target)
        except Exception:
            _source_edge_count = None
        sanity = (_section_sanity(target, section, orientation, depth,
                                  hatch_count=hatch_count,
                                  edge_count=edge_count,
                                  source_edge_count=_source_edge_count)
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
        # Snapshot BEFORE any break: this decides whether a break is needed at all.
        alignment_rewrote_request, _ = _position_deviation(position_requested,
                                                          position_actual)
        alignment_kept = None
        dropped_axis = None

        # Report 2026-11-06 (P1, B1): a section view is created in projection with its
        # parent, so the requested y is silently rewritten to stay on the parent's axis.
        # A caller who asked for (0.335, 0.17) got (0.335, 0.3018) — and had to call
        # `set_view(break_alignment=True)` afterwards to get what they asked for. Two calls
        # for one intent is a trap, and the first call looks like a bug.
        #
        # `align_to_parent=False` breaks the alignment here and re-applies the position.
        alignment_broken = None
        if not align_to_parent and position_requested is not None and alignment_rewrote_request:
            try:
                from . import layout as _layout_mod
                alignment_broken = _layout_mod.set_view(
                    drawing_path, section.Name, break_alignment=True,
                    x_m=position_requested[0], y_m=position_requested[1], save=False)
                try:
                    position_actual = [round(float(v), 4) for v in section.Position]
                except Exception:
                    pass
            except Exception as _exc:                               # noqa: BLE001
                alignment_broken = {"ok": False, "error": str(_exc)[:100]}

        # Report 2026-11-10 (P2-1): every position field is recomputed from the FINAL
        # placement. The old code read `alignment_kept` / `position_axis_ignored` BEFORE
        # the break and `position_actual` AFTER it — so one receipt could say "x was
        # dropped" next to two identical coordinates, and `alignment_kept: true` could sit
        # beside a SUCCESSFUL `alignment_broken`. Those were two different moments in time,
        # not two contradictory facts, but the caller cannot tell that from the JSON.
        alignment_kept, dropped_axis = _position_deviation(position_requested,
                                                          position_actual)
        position_fulfilled = (None if position_requested is None or position_actual is None
                              else not alignment_kept)

        # ── Should a failed section stay on the sheet? (report 2026-11-08, P2-2) ──────
        # A deflated empty section pollutes the layout; measured 2026-11-08 on
        # 小车底部滚轮, a failed cut changed 11127 pixels. `layout.delete_view` gets that
        # down to 1829 — it removes the view, NOT the section line left in the parent's
        # sketch (`EnumSectionLines` raises on this binding, so the line cannot be reached
        # and there is no way to finish the job from here).
        #
        # So: drop the view when it carries no information (`section_not_cut`,
        # `section_empty`), keep it when it does (`hatch_missing` on a real cut shows WHERE
        # the plane landed). "auto" is the default because the empty case is strictly
        # better deleted — 84% less pollution — and the receipt says what remains either
        # way, instead of implying a clean sheet.
        def _maybe_drop_failed_view(_reason_code):
            _keep = keep_on_failure
            # `keep_on_failure` arrives as a STRING from MCP ("auto" / "keep" / "drop"),
            # so `is False` never matched and "drop" silently behaved like "keep".
            _mode = str(_keep).lower()
            _drop = (_mode in ("false", "drop", "delete", "remove") or
                     (_mode == "auto" and _reason_code in
                      ("section_not_cut", "section_empty")))
            if not _drop or not _new_view_name:
                return {"view_removed": False,
                        "view_kept": bool(_new_view_name),
                        "kept_because": ("keep_on_failure=%r" % (_keep,))}
            try:
                from .layout import delete_view as _dv
                # `add_section_view` has no `save` switch — it always persists, so the
                # cleanup does too.
                _r = _dv(drawing_path, _new_view_name, save=True)
                _ok_del = bool(_r.get("ok"))
            except Exception as _exc:                               # noqa: BLE001
                return {"view_removed": False, "view_kept": True,
                        "delete_error": str(_exc)[:80]}
            return {"view_removed": _ok_del,
                    "view_kept": not _ok_del,
                    # Even a successful delete leaves the parent's section line behind.
                    "section_line_remains": True,
                    "note": ("视图已删除，但父视图草图里的剖切线删不掉 —— "
                             "`EnumSectionLines` 在这个 binding 上会抛错。"
                             "残留约 1.8k 像素（实测：删前 11127，删后 1829）。"
                             if _ok_del else
                             "delete_view 没能删掉这个空剖视图，需要手工处理。")}


        diag = {
            "drawing": doc.GetTitle, "section": section.Name, "source_view": target.Name,
            "label": str(label), "orientation": str(orientation), "flip": bool(flip),
            "offset_m": round(float(offset_m), 6), "outline": box,
            "precise_outline_m": ([round(v, 5) for v in precise] if precise else None),
            "edge_count": edge_count, "sanity": sanity, "cut_plane": cut_plane,
            "layout": layout, "hatch_count": hatch_count,
            "hatch_diagnostics": hatch_diag,
            "position_requested_m": position_requested,
            "position_actual_m": position_actual,
            "alignment_kept": alignment_kept,
            # NEW (0.8.297): `true` = the FINAL position equals the request. Its negation of
            # `alignment_kept` makes the pair self-checking: exactly one of them is true
            # whenever a position was requested.
            "position_fulfilled": position_fulfilled,
            # What the alignment did BEFORE the optional break. `true` + `position_fulfilled`
            # = the break worked; `true` + `false` = this build refused to break.
            "alignment_rewrote_request": alignment_rewrote_request,
            "align_to_parent": bool(align_to_parent),
            "alignment_broken": alignment_broken,
            "position_axis_ignored": dropped_axis,
            "warning": ("最终位置 %r 与请求 %r 在 %s 上不一致：剖面视图与父视图 %r 保持"
                        "投影对齐。这是 FINAL 结果 —— 若 `align_to_parent=False` 已尝试打断"
                        "仍如此，说明本 build 拒绝打断，需要在界面里手工移动。"
                        % (position_actual, position_requested, dropped_axis, str(target.Name))
                        if dropped_axis else None),
        }
        # Report 2026-11-10 (P0, suggestion 2): put the eccentricity implied by the
        # requested offset in the receipt, in MODEL millimetres. `offset_m` is sheet metres
        # and the sign convention above gives `offset_m = model_offset * scale`, so
        # eccentricity = |offset_m| / scale. Measured on 凹槽滚轮 2:1: 0.004 m sheet ->
        # 2 mm model, chord 2*sqrt(32^2-2^2) = 63.875 mm -> 127.75 mm at 2:1, matching the
        # reported 127.73.
        #
        # DIAGNOSTIC ONLY. The `section_off_axis` rejection rests on MEASURED geometry,
        # never on this conversion: an unavailable/wrong scale must not be able to argue a
        # bad cut back into `ok:true`.
        try:
            _sec_scale = float(target.ScaleDecimal) or 1.0
        except Exception:
            _sec_scale = 1.0
        try:
            diag["eccentricity_mm"] = round(
                abs(float(offset_m)) * 1000.0 / _sec_scale, 3)
            diag["eccentricity_basis"] = "|offset_m| / view.ScaleDecimal, model mm"
        except (TypeError, ValueError, ZeroDivisionError):
            diag["eccentricity_mm"] = None
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
            #
            # Report 2026-11-08 (P0-1): the hint below used to send everyone to the hatch
            # SETTINGS, and for a revolved part that is the wrong room entirely — the view
            # was never cut. `hatch_diagnostics` showed `display_mode 2 / style 2` (normal)
            # while `edge_count == source_edge_count == 4`: no new edge, no cut.
            #
            # Two different failures, two different hints:
            #   same edge count as the parent -> the cut never happened
            #   more edges, still no hatch   -> the cut happened but hatching is off
            _same_edges = (sanity.get("edge_count") is not None
                           and sanity.get("source_edge_count") is not None
                           and sanity.get("edge_count") == sanity.get("source_edge_count"))
            if _same_edges:
                return error(
                    "section_not_cut",
                    "剖面视图已创建，但它和父视图的边数相同（%s vs %s）——**没有剖开**。"
                    % (sanity.get("edge_count"), sanity.get("source_edge_count")),
                    "剖切面没有生效：视图里没有出现任何内腔或孔的边。"
                    # Measured 2026-11-08 on 小车底部滚轮 (a revolved wheel), sweeping every
                    # combination of view x orientation x offset:
                    #
                    #   front + horizontal  off=0        section_not_cut   <- the dead spot
                    #   front + horizontal  off=-0.020   ok  edges 16  hatch 2
                    #   front + horizontal  off=-0.005   ok  edges 32  hatch 2
                    #   front + horizontal  off=+0.005   ok  edges 28  hatch 2
                    #   front + vertical    any offset   hatch_missing
                    #   end   + vertical    off=-0.005   ok  edges 32  hatch 2
                    #   end   + horizontal  off=-0.005   ok  edges 32  hatch 2
                    #
                    # So the earlier advice ("cut the end view vertically") was right but
                    # incomplete, and `offset_m=0` is a genuine dead spot on the front view:
                    # the cut plane sits exactly on the symmetry plane and no edge appears.
                    "**`offset_m=0` 现在可以切了**（0.8.255 修掉根因）。之前切不开不是偏置问题，是**剖切线长度被 clamp 到「短边一半」，端点正好落在轮廓圆上**，SolidWorks 把这种退化线当成「没穿过实体」，返回一个普通投影。现在线会**外扩 `cut_line_overshoot_m`（默认 4 mm）**，端点一定在轮廓外。\n"
                    "实测（2026-11-08，`offset=0`）：`限位导向套` 前视 46 边 / hatch 4 / ratio 1.000；`小车底部滚轮`（单实体导入件）前视 28 边 / hatch 2 / ratio 1.000 —— **两种零件都过轴**。\n"
                    "**如果仍然切不开**：① 把 `cut_line_overshoot_m` 加到 0.008；② 看 `sanity.verdict`：`identical_to_source` 说明线没穿过实体，`no_hatch` 才去查剖面线设置（**别再怀疑材质** —— 2026-11-09 实测未赋材质也能 hatch 4，旧结论「未赋材质时 hatch 恒 0」已作废）；③ 还不行就把剖视留给 SolidWorks 手工做，**不要用 `offset_m` 去凑** —— 回转体上偏 2 mm，Ø64 外圆的弦长是 2·√(32²−2²) = 63.875 mm（旧文案误写 63.5，那对应约偏 4 mm），标出来的直径是错的。",
                    # Report 2026-11-08 (P0-1, suggestion 3): a failed section used to be
                    # left on the sheet with no mention in the receipt, so the caller found
                    # an empty `A-A` polluting the layout later. It is still kept — deleting
                    # automatically would also delete the evidence that shows WHERE the cut
                    # plane landed — but the receipt now says so and names the view.
                    view_to_delete=_new_view_name,
                    **_maybe_drop_failed_view("section_not_cut"),
                    **diag)
            return error(
                "hatch_missing",
                "剖面视图已创建，但剖切面没有任何剖面线（GetFaceHatchCount = 0）。",
                "GB 要求剖切面必须画剖面线；这种视图在生产上不能用。"
                "请在 SolidWorks 界面手工检查该视图的区域剖面线属性（是否选了 Hatch / "
                "Material crosshatch），或换一个能正常显示剖面线的模板/零件做对照。",
                view_to_delete=_new_view_name,
                # `auto` keeps this one: the cut DID happen, so the view shows where the
                # plane landed — that is diagnostic value the empty case does not have.
                **_maybe_drop_failed_view("hatch_missing"),
                **diag)
        # ── Off-axis / eccentric chord (2026-11-08 suggestion 2, fixed 2026-11-10) ────
        # The decision now lives in `_section_off_axis_from_sanity` — pure geometry, no
        # COM, so smoke.py can exercise it with a synthetic `sanity` dict. It checks BOTH
        # axes: `kept_dimension` (a > 0.5% shrink means the plane missed the cross-section)
        # and `off_axis_dimension` (where an eccentric chord of a revolved part actually
        # shows up). Report 2026-11-10 proved the old one-axis check was structurally
        # blind: `ratio 1.000` next to `127.98 -> 127.73`, and still `ok: true`.
        _off_axis = _section_off_axis_from_sanity(sanity, diag.get("eccentricity_mm"))
        if _off_axis:
            # Report 2026-11-12 §3-1: the nested `sanity` still said `verdict: "ok"` next
            # to a top-level rejection, so a caller reading only `sanity` saw a pass.
            sanity["verdict"] = "section_off_axis"
            sanity["off_axis"] = _off_axis
            _ecc = diag.get("eccentricity_mm")
            _chord = None
            if _ecc and _off_axis.get("detected_on") == "off_axis_dimension":
                try:
                    # Chord of the SOURCE diameter at that offset, in MODEL mm:
                    # 2*sqrt(r^2 - e^2). Computed, never a constant — report 2026-11-12
                    # §3-4 caught two releases in a row quoting a hand-written 63.5 /
                    # 63.48 that matched neither the model nor the sheet.
                    _r_model = (float(_off_axis["source_mm"]) / _sec_scale) / 2.0
                    _chord = round(2.0 * ((_r_model ** 2 - float(_ecc) ** 2) ** 0.5), 3)
                except (TypeError, ValueError, KeyError, ZeroDivisionError):
                    _chord = None
            _share = 0.0
            try:
                _share = (float(_off_axis["shrunk_mm"]) / float(_off_axis["source_mm"])
                          * 100.0)
            except (TypeError, ValueError, KeyError, ZeroDivisionError):
                _share = 0.0
            return error(
                "section_off_axis",
                "剖视图的 %s 维比源视图短了 %s mm（%.3f → %.3f）—— "
                "**这不是过轴全剖，是一条偏心弦**。"
                % (_off_axis.get("dimension") or "尺寸", _off_axis["shrunk_mm"],
                   _off_axis["source_mm"], _off_axis["section_mm"]),
                ("判据来自 `%s`：该维偏差超过 %s mm 绝对值即拒绝 —— 用绝对值而不是百分比，"
                 "因为本例只掉 %.2f%%，任何按比例的门都会放行。"
                 "回转体的轴向全剖必须过轴线：偏心会让外圆与孔都读小，图上却看不出异常。"
                 "%s"
                 "把 offset_m 调回 0 附近重切。视图保留了，供你判断偏心方向。"
                 % (_off_axis.get("detected_on") or "kept_dimension",
                    SECTION_OFF_AXIS_TOLERANCE_MM, _share,
                    ("按 `eccentricity_mm` 算出该处弦长 %.3f mm（模型 mm）。" % _chord)
                    if _chord else "")),
                off_axis=_off_axis,
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
        # What the caller asked for vs what was actually cut. `offset_m=0` cannot be cut
        # on a symmetric view, so the tool nudges it to 1 mm — the receipt must not let
        # that pass silently.
        diag.update(offset_requested_m=round(_offset_requested, 6),
                    offset_used_m=round(float(offset_m), 6),
                    offset_nudged=bool(_offset_nudged))
        # Report 2026-11-10 (P1): `offset_nudged` used to be a silent leaf field while the
        # same tool's own hint says "不要用 offset_m 去凑". A request for a through-axis
        # cut must not quietly become a 0.5 mm eccentric one. The default is now False
        # (cut_line_overshoot_m fixed the root cause), but an explicit opt-in still deserves
        # a TOP-LEVEL warning so no pipeline can miss it.
        if _offset_nudged:
            _nudge_warning = (
                "请求的 offset_m=0 被内部改写为 %.3f m（约 %.2f mm 模型偏心）：这是"
                "「零偏置切不开」时期遗留的 workaround，`cut_line_overshoot_m` 已修掉根因，"
                "`auto_nudge_zero_offset` 默认已改为 False。请显式传 0 复核，"
                "不要把这张偏心剖面当成过轴全剖。"
                % (float(offset_m), diag.get("eccentricity_mm") or 0.0))
            diag["warning"] = " | ".join(
                part for part in (diag.get("warning"), _nudge_warning) if part)
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
            perf_hooks.bump("redraw_calls")
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
            perf_hooks.bump("redraw_calls")
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


# SolidWorks `swTolType_e`, in ITS order — not in the order the names read nicely.
#
# ⚠️ THIS TABLE WAS "FIXED" INTO A WORSE STATE ON 2026-11-05, AND CORRECTED BACK
#    2026-11-07. Read this before touching it again.
#
# 2026-11-05 (P0-A): a "bilateral" 14.20 +0.05/-0.05 printed "-0.05" ABOVE "+0.05".
# I diagnosed it as a shifted table and moved `bilateral` from 2 to 1.
#
# That diagnosis was wrong, and the "fix" broke the most common tolerance in machining:
#
#   2026-11-07 measured, `bilateral` with upper=0.05 lower=0.05  -> readback 1
#     PDF: ┌────────┐
#          │ Ø14.20 │      <- a BOX. That is swTolBASIC, i.e. a theoretically exact
#          └────────┘         dimension with NO tolerance band at all.
#   `symmetric` with the same values -> readback 3
#     PDF: Ø14.25 / 14.25  <- two identical limits, band width ZERO. That is swTolLIMIT.
#
# The real enum (SolidWorks API `swTolType_e`) is:
#     swTolNone = 0   swTolBASIC = 1   swTolBILAT = 2   swTolLIMIT = 3
#     swTolSYMMETRIC = 4   swTolMIN = 5   swTolMAX = 6   swTolFIT = 7
#     swTolFITTOLONLY = 8
#
# So the 2026-11-04 table was ALREADY CORRECT (`bilateral: 2`). The inverted pair came
# from the ARGUMENT ORDER of `SetToleranceValues`, not from this table — see the call site
# for the measured signature.
#
# Historical evidence that settles it: v0.8.139 with `bilateral = 2` printed the two
# DEVIATIONS (+0.05 / -0.05), which only swTolBILAT does. With `bilateral = 1` we got a
# box, which only swTolBASIC does. Two independent observations, same conclusion.
TOLERANCE_TYPES = {
    "none": 0,                # swTolNone
    "basic": 1,               # swTolBASIC      <- 方框，无公差带
    "bilateral": 2,           # swTolBILAT      <- 上下两个偏差（机加工最常用）
    "limit": 3,               # swTolLIMIT      <- 两个极限值
    "symmetric": 4,           # swTolSYMMETRIC  <- ±
    "min": 5,                 # swTolMIN
    "max": 6,                 # swTolMAX
    "fit": 7,                 # swTolFIT
    "fit_with_tolerance": 8,  # swTolFITTOLONLY
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
                    # Return the ANNOTATION as well: `IDisplayDimension` (which carries
                    # `SetPrecision3`) is the annotation's specific object, and `IDimension`
                    # has no property that leads back to it (checked: no GetAnnotation, no
                    # GetDisplayDimension). Constructing IDisplayDimension from the
                    # dimension's own `_oleobj_` raises, which is how the decimal-place
                    # setting silently became a no-op.
                    return view, dim, ann
            except Exception:
                continue
    return None, None, None


def _tolerance_text_on_pdf(pdf_path, nominal_mm, tol, upper_mm, lower_mm):
    """What the PDF text layer actually shows around a toleranced dimension.

    Report 2026-11-07 (P0), final line: "readback 读的是你写进去的值，它证明不了图面上是
    什么" — and it was right. The 2026-11-05 "fix" passed every readback and still printed
    a BASIC box. This is the check that would have caught it.

    Measured 2026-11-07 on 飞达动力轴, one fresh drawing per type, upper=lower=0.05:

        bilateral  ['35', '0.05', '0.05']         two deviations, side by side
        symmetric  ['35 ±0.05']                   one ± value
        basic      ['35']                         no deviation at all
        limit      ['35.05', '35.05']             two LIMITS (nominal±dev), not deviations

    Those four shapes are distinguishable, so that is what gets asserted. Note what is NOT
    asserted: that bilateral stacks vertically. On this build the two deviations come out
    side by side at the same y (measured 545.4 for both), and no `IDisplayDimension`
    property controls it — the shape is still bilateral, just laid out inline.

    Returns a dict with `lines` (text containing the nominal or the deviation),
    `shape` (one of the four names, or `unrecognised`) and `evidence`.
    """
    try:
        import pypdfium2 as pdfium
    except Exception:
        return {"checked": False, "reason": "pypdfium2_missing"}

    try:
        page = pdfium.PdfDocument(pdf_path)[0]
        textpage = page.get_textpage()
        raw = textpage.get_text_range()
    except Exception as exc:                                        # noqa: BLE001
        return {"checked": False, "reason": "pdf_unreadable: %s" % str(exc)[:60]}

    # Report 2026-11-07 (P1-1): `get_text_range()` flattened the page into one string and
    # the deviation stack was simply missing from it — the report measured
    # `"lines": ["0 18", ...]` with `deviation_count: 0` while `+0.03 / 0` was plainly
    # printed under the Ø30. Both the `<MOD-DIAM>` glyph and the tolerance stack are
    # SEPARATE text objects, and a flat dump does not keep them next to their nominal.
    #
    # So read per rect and keep only a WINDOW around the nominal: the tolerance lives
    # underneath (or beside) its dimension, and pulling the whole page in lets the
    # technical-requirements block — which mentions "H7" and "H8" in prose — answer for
    # the dimension.
    window_text = []
    # `boxes` must live OUTSIDE the try as well: the numeric fallback below needs every
    # text box on the page when the window does not contain the deviations
    # (report 2026-11-12 §2).
    boxes = []
    try:
        n_rects = textpage.count_rects()
        boxes = []
        for _i in range(n_rects):
            try:
                _r = textpage.get_rect(_i)
                _t = textpage.get_text_bounded(left=_r[0], bottom=_r[1],
                                               right=_r[2], top=_r[3])
            except Exception:
                continue
            _t = ( _t or "" ).strip()
            if _t:
                boxes.append((float(_r[0]), float(_r[1]), float(_r[2]), float(_r[3]), _t))
        # locate the nominal's own box
        nominal_precise = None
        try:
            nominal_precise = float(nominal_mm)
        except Exception:
            nominal_precise = None
        anchor = None
        for _b in boxes:
            try:
                if abs(float(_b[4].replace("Ø", "").replace("φ", "")) - nominal_precise) < 1e-6:
                    anchor = _b
                    break
            except Exception:
                continue
        if anchor is not None:
            # Text height gives the scale: a tolerance stack sits within ~3 line heights.
            _h = max(1.0, anchor[3] - anchor[1])
            _cx = (anchor[0] + anchor[2]) / 2.0
            _cy = (anchor[1] + anchor[3]) / 2.0
            for _b in boxes:
                _bx = (_b[0] + _b[2]) / 2.0
                _by = (_b[1] + _b[3]) / 2.0
                # 0.8.312: 3 line heights was too tight — a tolerance stack two rows below
                # its nominal fell outside the window, and the probe then reported "no
                # deviation" for a perfectly correct sheet (report 2026-11-12 §2).
                if abs(_bx - _cx) <= 6.0 * _h and abs(_by - _cy) <= 6.0 * _h:
                    window_text.append(_b[4])
        elif boxes:
            # No exact nominal found: fall back to everything, which is what the old code
            # did. Better a false positive than a silent "no deviation" when the parser
            # simply did not recognise the nominal.
            window_text = [b[4] for b in boxes]
    except Exception:
        window_text = []
    # REPLACE, do not append. Appending the window to the full-page text double-counts
    # every deviation it contains, so a bilateral tolerance would report `deviation_count: 4`.
    _window_used = bool(window_text)
    if _window_used:
        raw = "\n".join(window_text)

    nominal = ("%g" % round(float(nominal_mm), 3))
    dev = ("%g" % round(float(upper_mm), 3)) if upper_mm is not None else None
    hi = ("%g" % round(float(nominal_mm) + float(upper_mm), 3)) if upper_mm is not None else None

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    hits = [ln for ln in lines
            if nominal in ln or (dev and dev in ln) or (hi and hi in ln)]

    joined = " ".join(hits)
    dev_count = joined.count(dev) if dev else 0
    hi_count = joined.count(hi) if hi else 0

    # ── Report 2026-11-08 (P2-2) ──────────────────────────────────────────────────
    # `shape` counted how many times ONE string appeared and required >= 2 for
    # "bilateral". A `+0.018/0` tolerance only ever prints ONE deviation — the lower limit
    # is zero and zero is not drawn as a second number. So a perfectly correct bilateral
    # stack came back `deviation_count: 1` and fell through to `unrecognised`.
    #
    # The count was never the right question. A shape is determined by WHICH numbers are
    # present, not by how many times one of them repeats:
    #
    #   upper present, lower is 0      -> bilateral (one printed deviation is correct)
    #   upper and lower both present    -> bilateral
    #   only the summed limit present  -> limit_values
    #   neither                        -> basic
    _lo_text = None
    _lo_value = None
    try:
        if lower_mm is not None:
            _lo_value = float(lower_mm)
            _lo_text = "%g" % round(abs(_lo_value), 3)
    except Exception:
        _lo_text = None
        _lo_value = None
    _has_up = bool(dev) and dev in joined and (upper_mm is None or float(upper_mm) != 0)
    # A zero lower limit is legitimately absent from the sheet; a non-zero one must be
    # there, and its sign may or may not print, so match the magnitude.
    _has_lo = (bool(_lo_text) and _lo_value not in (None, 0.0)
               and (_lo_text in joined or ("-%s" % _lo_text) in joined))

    # ── Report 2026-11-12 §2 (P0): the sign and the number are SEPARATE text objects ──
    # SolidWorks writes a tolerance's `+`/`-` apart from its magnitude, so a CORRECT
    # `Ø14.20 +0.050 / -0.050` arrived here as three bare numbers. Requiring the sign on
    # the same line produced `basic_no_deviation` plus "readback is right but the sheet is
    # not what you asked for" — a FALSE NEGATIVE that invites the caller to "fix" a
    # correct drawing, which is worse than not checking at all.
    #
    # The shape test is NUMERIC now: parse each line as a magnitude and compare it with the
    # requested deviations. The sign-text test stays only as corroboration.
    def _magnitudes_of(text_lines):
        out = []
        for _ln in (text_lines or []):
            _s = str(_ln).strip().lstrip("+-\u00b1").strip()
            try:
                out.append(abs(float(_s)))
            except Exception:
                continue
        return out

    _up_mag = abs(float(upper_mm)) if upper_mm is not None else None
    _lo_mag = abs(float(lower_mm)) if lower_mm is not None else None

    def _found(mags, target):
        return bool(target) and any(abs(_m - target) <= 1e-6 for _m in mags)

    _magnitudes = _magnitudes_of(lines)
    _num_up = _found(_magnitudes, _up_mag)
    _num_lo = _found(_magnitudes, _lo_mag)
    _basis = "magnitudes_in_window" if _window_used else "magnitudes_full_page"
    if _window_used and boxes and not (_num_up or _num_lo):
        # The window missed them: fall back to the whole page. Comparing MAGNITUDES keeps
        # the technical-requirements prose ("H7", "H8") from answering for a dimension —
        # those lines never parse as a bare float.
        _page_mags = _magnitudes_of([b[4] for b in boxes])
        _page_up = _found(_page_mags, _up_mag)
        _page_lo = _found(_page_mags, _lo_mag)
        if _page_up or _page_lo:
            _magnitudes, _num_up, _num_lo = _page_mags, _page_up, _page_lo
            _basis = "magnitudes_full_page_fallback"
            _window_used = False
    _num_count = ((int(_num_up) + int(_num_lo))
                  if (_up_mag or 0) != (_lo_mag or 0) else int(_num_up))

    _tol_name = str(tol or "").lower().split("(")[0].strip()
    if "±" in joined and dev and dev in joined:
        shape, _evidence = "symmetric_plus_minus", "sign_text"
    elif hi and hi != nominal and hi_count >= 1 and not _num_up and not _num_lo:
        shape, _evidence = "limit_values", "sign_text"
    elif _num_up or _num_lo:
        # One magnitude is enough: a zero lower limit prints no second number.
        shape, _evidence = "bilateral_two_deviations", "numeric_magnitude"
    elif _has_up or _has_lo:
        shape, _evidence = "bilateral_two_deviations", "sign_text"
    elif _tol_name and _tol_name not in ("none", "basic") and dev_count == 0:
        shape, _evidence = "basic_no_deviation", "no_deviation_found"
    elif dev_count == 0:
        shape, _evidence = "basic_no_deviation", "no_deviation_found"
    else:
        shape, _evidence = "unrecognised", "ambiguous"

    return {"checked": True, "shape": shape, "lines": hits[:8],
            "windowed": _window_used,
            "deviation_count": dev_count, "limit_count": hi_count,
            # 0.8.312: how many requested magnitudes were found AS NUMBERS, independent of
            # whether their signs landed in the same text object.
            "numeric_deviation_count": _num_count,
            # What the verdict rests on, so the caller can weigh it (report §2.5 item 3).
            "basis": _basis,
            "evidence": _evidence,
            "upper_present": bool(_has_up or _num_up),
            "lower_present": bool(_has_lo or _num_lo),
            "lower_expected": _lo_value,
            "expected": tol}


def add_dimension_tolerance(drawing_path, value_mm, tolerance_type="bilateral",
                           upper=0.1, lower=0.1, view_name=None, apply=True,
                           verify_render=False, tolerance_precision=3):
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
        view, dim, _ann = _find_dimension(doc, value_mm, view_name)
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
            # Bilateral means "+upper / -lower". Both arguments are written as magnitudes, so
            # passing lower as a positive number produced "Ø14.20 +0.05 / +0.05" — a tolerance
            # band of zero, which is worse than no tolerance at all because it looks
            # authoritative (report 2026-11-04, P0-3). The negative is applied here rather
            # than left to the API, which treats the second argument as a magnitude.
            #
            # Only bilateral gets the sign. `symmetric` writes ±value and `limit` writes two
            # actual limits (e.g. 14.25 / 14.15), where a forced negative would be wrong.
            # GROUND TRUTH, read from the generated type library (2026-11-07):
            #
            #     def SetToleranceValues(self, TolMin, TolMax):
            #         'Set the tolerance min and max values'
            #
            # The parameters are (MIN, MAX) — I was passing (upper, lower), i.e. exactly
            # reversed. That is what printed "-0.05" above "+0.05" on 2026-11-05, and I
            # "fixed" it by shifting the TYPE table, which broke `bilateral` into a BASIC
            # box and `symmetric` into two identical limits.
            #
            # With (TolMin=0.05, TolMax=-0.05) SolidWorks drew max (-0.05) on top and min
            # (+0.05) below — precisely the reported symptom, and it confirms the order.
            _upper_m = float(upper) / 1000.0
            _lower_m = float(lower) / 1000.0
            if str(tolerance_type).lower() == "bilateral":
                _lower_m = -abs(_lower_m)
            try:
                dim.SetToleranceValues(_lower_m, _upper_m)
            except Exception:
                # Symmetric takes a single value on some builds.
                try:
                    dim.SetToleranceValues(_upper_m, _upper_m)
                except Exception:
                    pass

            # ── Decimal places (report 2026-11-07, P1-1) ────────────────────────────
            # The drawing defaulted to 2 places, so an H7 bore of +0.018 printed as
            # "+0.02" and an H8 of +0.033 printed as "+0.03". Metric fits need three;
            # two makes the tolerance unusable. `IDimensionTolerance` has no precision
            # property at all — it lives on `IDisplayDimension` as
            # `SetPrecision3(Primary, Dual, PrimaryTol, DualTol)`.
            #
            # Measured 2026-11-07 on 飞达动力轴: before `GetPrimaryPrecision()=2`,
            # `GetPrimaryTolPrecision()=2`, `GetUseDocPrecision()=True`; after
            # `SetPrecision3(3,3,3,3)` both read 3.
            #
            # The value precision is passed through unchanged — only the TOLERANCE
            # digits are forced, because "35" in a 35 mm dimension is correct and
            # "35.000" is not what GB drawings show.
            precision_applied = None
            try:
                import importlib
                import sys as _sys
                try:
                    import win32com.client.gencache as _gc2
                    _p2 = _gc2.GetGeneratePath()
                    if _p2 and _p2 not in _sys.path:
                        _sys.path.insert(0, _p2)
                except Exception:
                    pass
                _m2 = importlib.import_module(
                    "83A33D31-27C5-11CE-BFD4-00400513BB57x0x32x0")
                _dd = _m2.IDisplayDimension(_ann.GetSpecificAnnotation._oleobj_)
                _want = int(tolerance_precision)
                _val_prec = int(_dd.GetPrimaryPrecision() or 2)
                _dual_prec = int(_dd.GetAlternatePrecision() or 2)
                _dd.SetPrecision3(_val_prec, _dual_prec, _want, _want)
                precision_applied = int(_dd.GetPrimaryTolPrecision())
            except Exception as _prec_exc:                          # noqa: BLE001
                warnings.append("公差小数位数未设置成功：%s" % str(_prec_exc)[:80])
                precision_applied = None
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
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
        # Numeric readback of the limits themselves. `rendered_text` comes from GetText,
        # which returns nothing on some builds even for a correct tolerance (noted in the
        # P2-9 comment below), so it cannot be the only evidence. These two calls answer
        # "what did SolidWorks actually store" in metres, independent of rendering.
        #
        # Report 2026-11-04 P0-3: bilateral printed "+0.05 / +0.05" and there was no way
        # to see it without rendering a PDF. With these two fields the sign is checkable.
        # `GetToleranceValues` is a PROPERTY on this build and returns (upper, lower) in
        # metres — measured 2026-11-04: a bilateral 0.05/0.05 reads back (5e-05, -5e-05).
        # `GetToleranceUpperLimit` / `ToleranceUpperLimit` do not exist here at all, which is
        # why the first attempt at this readback silently reported None for both limits.
        #
        # The pair is the only way to check the SIGN without rendering a PDF, and the sign is
        # what P0-3 got wrong: "+0.05 / +0.05" is a zero-width band that looks correct.
        limits_stored_m = {"upper": None, "lower": None}
        try:
            _tv = dim.GetToleranceValues          # property, not a call
            if isinstance(_tv, (tuple, list)) and len(_tv) >= 2:
                # ORDER IS (LOWER, UPPER), same as `SetToleranceValues(TolMin, TolMax)`.
                # Measured 2026-11-07 on 飞达动力轴, requested upper +0.018 / lower -0.003:
                # `GetToleranceValues` returned (-3e-06, 1.8e-05) in METRES, i.e.
                # (-0.003 mm, +0.018 mm). The receipt had them swapped, and the swap then
                # tripped `lower_sign_wrong` — the tool reported its own reading error as a
                # writing error.
                limits_stored_m["lower"] = round(float(_tv[0]), 8)
                limits_stored_m["upper"] = round(float(_tv[1]), 8)
        except Exception:
            pass
        # A bilateral lower limit that is not negative means the band is one-sided; that is
        # the P0-3 defect, and it must not report ok next time it happens.
        lower_sign_wrong = None
        if str(tolerance_type).lower() == "bilateral":
            try:
                lower_sign_wrong = (limits_stored_m.get("lower") is not None
                                    and limits_stored_m["lower"] > 0)
            except Exception:
                lower_sign_wrong = None
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
        # ── Figure-level proof ───────────────────────────────────────────────────────
        # "readback 读的是你写进去的值，它证明不了图面上是什么" (report, 2026-11-07).
        # Export once and read the text layer, so the receipt carries what a person would
        # SEE rather than what COM stored. Costs one PDF export (~2 s) when asked for.
        pdf_probe = {"checked": False, "reason": "verify_render_false"}
        if verify_render:
            _tmp_pdf = None
            try:
                import tempfile as _tempfile
                _fd, _tmp_pdf = _tempfile.mkstemp(suffix=".pdf")
                os.close(_fd)
                drawing_ok_export = export_drawing(drawing_path, _tmp_pdf, fmt="pdf")
                if drawing_ok_export.get("ok"):
                    pdf_probe = _tolerance_text_on_pdf(_tmp_pdf, value_mm, tolerance_type,
                                                       float(upper), float(lower))
                else:
                    pdf_probe = {"checked": False,
                                 "reason": "export_failed: %s"
                                           % drawing_ok_export.get("error")}
            except Exception as exc:                                # noqa: BLE001
                pdf_probe = {"checked": False, "reason": str(exc)[:80]}
            finally:
                try:
                    if _tmp_pdf and os.path.exists(_tmp_pdf):
                        os.remove(_tmp_pdf)
                except Exception:
                    pass

        # Map the printed shape back to the requested type so a mismatch is a FAILURE,
        # not a note. This is the check that would have caught the 2026-11-05 regression:
        # `bilateral` reported readback 1 and printed a BASIC box, and every field said ok.
        _want = {"bilateral": "bilateral_two_deviations",
                 "symmetric": "symmetric_plus_minus",
                 "basic": "basic_no_deviation",
                 "limit": "limit_values"}.get(str(tolerance_type).lower())
        _shape = pdf_probe.get("shape")
        render_matches = (None if not pdf_probe.get("checked") or not _want
                          else _shape == _want)
        if render_matches is False:
            warnings.append(
                "图面与请求不符：要求 `%s`（应呈现为 %s），PDF 文本层实际是 `%s` —— "
                "readback 是对的，但印出来的不是这个公差。"
                % (tolerance_type, _want, _shape))

        return drawing_ok(applied=True, saved=True, planned=planned,
                          tolerance_type_readback=applied,
                          rendered_text=rendered_text,
                          limits_stored_m=limits_stored_m,
                          lower_sign_wrong=lower_sign_wrong,
                          rendered_verified=bool(rendered_text),
                          unit_suspect=unit_suspect,
                          tolerance_precision_requested=int(tolerance_precision),
                          tolerance_precision_applied=precision_applied,
                          pdf_render=pdf_probe,
                          pdf_render_expected=_want,
                          pdf_render_matches=render_matches,
                          warning=(" ".join(warnings) if warnings else None))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("tolerance_failed", str(exc))


ANNOTATION_KINDS = {
    # Report 2026-11-08 (P2-5): this map said `2: "centerline"`, and a DATUM FEATURE
    # SYMBOL came back as `kind: "centerline"`. Measured 2026-11-08 on 飞达动力轴 with
    # `InsertDatumTag2`: the annotation created by it reports `GetType == 2`, and on that
    # sheet type 2 was produced ONLY by the datum symbol — every hole centre mark was 13.
    #
    # I could not build a real centreline to compare against (`InsertCenterLine` is a
    # property returning bool, not a method, and `InsertCenterLine2` raises 找不到成员), so
    # I cannot prove that a centreline never reports 2. Both report
    # `GetAttachedEntityTypes == (1,)` as well, so that is not a discriminator either.
    #
    # Hence: type 2 is reported as `datum_tag` (the observed producer) and every such row
    # carries `kind_ambiguous` naming centreline as the other possibility. A caller that
    # must be certain has to look at the sheet; a caller that counted "centre lines" was
    # counting datum symbols.
    2: "datum_tag", 4: "dimension", 5: "geometric_tolerance", 6: "note",
    7: "surface_finish", 8: "datum_tag", 9: "datum_target", 11: "balloon",
    12: "weld_symbol", 13: "center_mark", 14: "hole_callout",
}

# Type codes that cannot be resolved to one kind by the API alone.
ANNOTATION_KIND_AMBIGUOUS = {
    2: ("datum_tag", "centerline"),
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


def _signed_mm(value):
    """Format a tolerance value in mm: `+0.033`, `-0.018`, or a plain `0`.

    `GetToleranceValues` can hand back a NEGATIVE ZERO, and `%+g` then prints `-0` while
    the sheet itself prints `0` — the receipt and the drawing disagreed on the same
    dimension (report 2026-11-10 P2-3). A bare `0` also matches GB practice:
    `Ø30 +0.033/0`, not `Ø30 +0.033/+0`.
    """
    value = float(value)
    if abs(value) < 5e-7:
        return "0"
    return "%+g" % value


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
                # Type codes the API cannot resolve on its own. Reported rather than
                # guessed: counting "centre lines" on a sheet full of datum symbols is the
                # failure this field exists to make visible.
                _ambiguous = ANNOTATION_KIND_AMBIGUOUS.get(kind_code)
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
                if _ambiguous:
                    # Keep `kind` as the observed producer so existing callers do not
                    # break, but say out loud that the type code is not exclusive.
                    row["kind_ambiguous"] = list(_ambiguous)
                    row["kind_note"] = (
                        "swAnnotationType %d is emitted by %s — the API does not "
                        "distinguish them (both report GetAttachedEntityTypes (1,)). "
                        "`kind` is the one observed here; verify on the sheet before "
                        "counting." % (kind_code, " and ".join(_ambiguous)))
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
                    row["text_raw"] = callout_text or None
                    if callout_text and "<MOD-" in callout_text.upper():
                        # Report 2026-11-05: `text_is_format_code: true` next to a `text`
                        # that reads "Ø8" looked self-contradictory — the flag describes the
                        # RAW text, not the field it sits next to. `text_decoded` says the
                        # thing a reader actually needs: "what you see here has been
                        # translated". The old flag is kept for compatibility; `text_raw`
                        # holds the untranslated original.
                        row["text_is_format_code"] = True
                        row["text_decoded"] = True
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
                            # Report 2026-11-06 (B5/B9): `SystemValue` is a LENGTH in metres
                            # for linear dimensions but an ANGLE IN RADIANS for angular ones.
                            # Multiplying 45 deg by 1000 gave `value_mm: 785.398` — a number
                            # that looks like a 785 mm length and made `delivery_check`
                            # announce an impossible overall size, while masking the fact
                            # that the annotation had left the sheet.
                            #
                            # Angular dimensions get `value_deg` and NO `value_mm`; a field
                            # named `value_mm` must never carry degrees.
                            _dim_type = 0
                            try:
                                _dim_type = int(dim.GetType)
                            except Exception:
                                try:
                                    _dim_type = int(dim.Type)
                                except Exception:
                                    _dim_type = 0
                            _raw = float(dim.SystemValue)
                            if _dim_type in (1, 3):          # angular / arc-length
                                row["value_deg"] = round(_raw * 180.0 / 3.141592653589793, 3)
                                row["value_is_angular"] = True
                                row["value_mm"] = None
                                dimension_value = None
                            else:
                                dimension_value = round(_raw * 1000, 3)
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
                                    # `dimension_value` is None for angular dimensions —
                                    # never format a callout from it without a guard.
                                    row["diameter_mm"] = (round(float(dimension_value), 3)
                                                          if dimension_value is not None
                                                          else None)
                                    row["diameter_source"] = "value_unverified"
                                    _prefix = ("R" if (_bare == "R"
                                                       or kind == "radius_callout")
                                               else "Ø")
                                    if dimension_value is not None:
                                        row["text"] = "%s%g" % (
                                            _prefix, round(float(dimension_value), 3))
                                        row["text_was_bare_symbol"] = True
                                # Report 2026-11-06 (P2, B12): the sheet prints `4-R7`,
                                # but this row carried `text: "4-R"` and `value_mm: 7` as
                                # two disconnected pieces. A reader (or a downstream
                                # consumer) has to know the drafting convention to join
                                # them. `full_text` does the join; `text` and `value_mm`
                                # stay as they were so nothing that parses them breaks.
                                _bare_full = str(row.get("text") or "").strip()
                                _diam = row.get("diameter_mm")
                                if _bare_full and _diam is not None \
                                        and "full_text" not in row:
                                    _num = "%g" % round(float(_diam), 3)
                                    if _num not in _bare_full:
                                        row["full_text"] = "%s%s" % (_bare_full, _num)
                                    else:
                                        row["full_text"] = _bare_full
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
                                    row["text_decoded"] = True
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
                                    # Report 2026-11-08 (P2-1): this used to append
                                    # "(公差类型 2，值未读回)" and stop — a reader could see
                                    # that a tolerance existed but not what it said, so the
                                    # only way to check the printed deviation was to look at
                                    # the sheet. `GetToleranceValues` CAN be read (0.8.222
                                    # fixed its order for `add_dimension_tolerance`), so
                                    # the numbers are recovered here.
                                    #
                                    # ORDER IS (LOWER, UPPER), in METRES — measured
                                    # 2026-11-08: requested +0.018/-0.003, got
                                    # (-3e-06, 1.8e-05).
                                    _tol_limits = None
                                    try:
                                        _tv = dim.GetToleranceValues
                                        if isinstance(_tv, (tuple, list)) and len(_tv) >= 2:
                                            _tol_limits = (round(float(_tv[0]) * 1000.0, 4),
                                                           round(float(_tv[1]) * 1000.0, 4))
                                    except Exception:
                                        _tol_limits = None
                                    row["tolerance_type_code"] = int(_tol)
                                    if _tol_limits is not None:
                                        _lo, _up = _tol_limits
                                        row["tolerance_values_mm"] = {"lower": _lo,
                                                                      "upper": _up}
                                        _code = int(_tol)
                                        if _code == 2:      # BILATERAL: +up / -|lo|
                                            row["display_text"] = "%s %s/%s" % (
                                                row["display_text"], _signed_mm(_up),
                                                _signed_mm(_lo))
                                        elif _code == 3:    # LIMIT: two stacked values
                                            _base = row.get("value_mm")
                                            if _base is not None:
                                                row["display_text"] = "%g / %g" % (
                                                    round(float(_base) + _up, 3),
                                                    round(float(_base) + _lo, 3))
                                            else:
                                                row["display_text"] = "%s (%s/%s)" % (
                                                    row["display_text"], _signed_mm(_up),
                                                    _signed_mm(_lo))
                                        else:               # SYMMETRIC and others
                                            row["display_text"] = "%s %s/%s" % (
                                                row["display_text"], _signed_mm(_up),
                                                _signed_mm(_lo))
                                    else:
                                        # Still honest when the read fails, and now the
                                        # receipt also says WHY it is missing.
                                        row["tolerance_values_mm"] = None
                                        row["display_text"] = "%s (公差值读回失败)" % (
                                            row["display_text"],)
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
                                row["text_decoded"] = True
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
                   strict_attachments=False, include_faces=False):
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
        # The other half of `unmatched_ledger`: values that ARE on the sheet but lost their
        # attribution. Defined here so a failed `reconcile_ledger` cannot NameError the
        # receipt (0.8.306).
        ledger_unmatched_but_present = []

        # `blocking` is KEYWORD-ONLY. It used to be the fourth positional parameter, so an
        # explanation passed in that slot silently became the blocking flag — and any
        # explicit `blocking=` then raised `got multiple values`. Keyword-only makes that
        # class of mistake a TypeError at the call site with a line number, instead of a
        # swallowed failure that empties the verdict. Asserted by
        # `assert_record_blocking_is_keyword_only`.
        def record(name, ok, detail=None, *, blocking=True, **extra):
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
        # Faces are expensive and only feed one number (2026-11-09).
        # `include_faces=True` walks every face: measured on the track part (P05_karetku,
        # 147 visible edges) it costs 209 s vs 19.7 s without — 10x. The ledger's `holes`
        # list is the only thing here that needs faces, and this function uses it for a
        # truthiness test plus one number in a message. Default False; pass True to pay
        # for the hole count.
        led = _ledger.build_feature_ledger(model_path, include_faces=bool(include_faces))
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

        # ---- per-sheet (report C4) -------------------------------------------
        # `drawing_audit` walks every page, so the detail is already there; the previous
        # version threw it away and only counted document-wide, which meant a two-page
        # drawing could pass with an entirely EMPTY second page — `views_present` saw page
        # 1's views and said yes.
        audit_sheets = audit.get("sheets") or []
        sheet_summary = [{
            "index": sh.get("index"),
            "name": sh.get("name"),
            "size_m": sh.get("size_m"),
            "view_count": sh.get("view_count"),
            "dimensions": sh.get("dimensions"),
            "issues": [w.get("issue") for w in (sh.get("warnings") or [])],
        } for sh in audit_sheets]
        empty_sheets = [sh["name"] for sh in sheet_summary if not sh.get("view_count")]
        sheet_issues = {sh["name"]: sh["issues"] for sh in sheet_summary if sh.get("issues")}

        record("views_present", view_count > 0, "%d 个视图 / %d 页"
               % (view_count, len(sheet_summary) or 1))
        # A page with no view is not a "drawing page" — it will export blank. Blocking,
        # because the whole point of adding a sheet is to put something on it.
        record("every_sheet_has_views", not empty_sheets,
               ("空页：%s" % "、".join(empty_sheets)) if empty_sheets else None,
               sheets=sheet_summary)
        record("no_view_outside_sheet", "view_outside_sheet" not in warnings,
               ("超出图纸：%s" % "；".join(
                   "%s（%s）" % (w.get("view"), w.get("sheet"))
                   for w in (audit.get("warnings") or [])
                   if w.get("issue") == "view_outside_sheet") or None)
               if "view_outside_sheet" in warnings else None)
        record("no_view_overlap", "views_overlap" not in warnings,
               "有视图互相重叠" if "views_overlap" in warnings else None)
        record("no_blank_view", "blank_view" not in warnings,
               "存在空白视图" if "blank_view" in warnings else None)
        record("dimensions_present", dim_total > 0, "%d 个尺寸" % dim_total,
               per_sheet=[{"sheet": sh["name"], "dimensions": sh.get("dimensions")}
                          for sh in sheet_summary])

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
            # Report 2026-11-07 (P2): `ok: true` next to `hole_callout_objects: 0` still
            # read as "hole callouts are fine". Coverage from plain Ø dimensions is a
            # DIFFERENT thing from a native hole callout (which carries depth, countersink,
            # thread) — calling both "ok" hides that the caller got the weaker one.
            #
            # `ok` now means "a native hole callout exists"; a sheet covered only by Ø
            # dimensions reports `ok: false` with `covered: true`, so the two facts can no
            # longer contradict each other.
            # P2-7 of a 2026-11-07 report: a plain through-bore with counterbores was a
            # BLOCKER because no native hole callout existed, while the same receipt said
            # `covered: true` (three Ø dimensions on the sheet). GB accepts a Ø dimension on
            # a plain hole; the native callout carries depth/countersink/thread, which is
            # required for THREADED and countersunk holes, not for a reamed bore.
            #
            # So it blocks only when NOTHING covers the holes: no callout AND no Ø
            # dimension. "Only Ø dimensions" is a `pending` item — visible, and it does not
            # downgrade a delivered drawing to 仅供参考/未完成.
            # The explanation is APPENDED to `detail`, not passed as a fourth positional
            # argument. Measured 2026-11-08 by a report: the extra argument bound to
            # `blocking`, and the `blocking=` keyword then raised
            #
            #   record() got multiple values for argument 'blocking'
            #
            # which killed `delivery_check` AFTER it had run most of its checks (51.5 s in),
            # so `verdict` / `blockers` / `pending` all came back empty while
            # `produce_drawing` still reported `ok: true`. The whole delivery judgement
            # disappeared and the receipt looked like a pass.
            #
            # This is the same lesson already written 80 lines below at `cosmetic_threads`:
            # "`blocking` is positional, and the explanation goes into `detail` rather than
            # a fifth argument" — I read it and then broke it here in 0.8.222.
            record("hole_callouts", holes_in_drawing > 0,
                   ("%s / 模型 %d 个圆柱面候选。"
                    % (_how, hole_candidates))
                   + ("没有任何原生孔标注对象；只有 Ø 尺寸覆盖。普通通孔/沉孔用 Ø 标注符合 GB，"
                      "螺纹孔与沉头孔才需要原生孔标注"
                      if holes_in_drawing == 0 and diameter_dims else
                      "孔既没有标注也没有 Ø 尺寸覆盖，孔信息完全缺失"
                      if holes_in_drawing == 0 else
                      "背面或被遮挡的孔 SolidWorks 不给标注，需人工补或换视图"),
                   blocking=(holes_in_drawing == 0 and diameter_dims == 0),
                   hole_callout_objects=holes_in_drawing,
                   diameter_dimensions=diameter_dims,
                   covered_by_diameter_only=(holes_in_drawing == 0 and diameter_dims > 0),
                   covered=covered > 0)
        else:
            # Report 2026-11-12 §4-4: with `include_faces=False` (the default, 10x cheaper)
            # the ledger never counts cylindrical faces, so `hole_candidates` is 0 and this
            # whole block was skipped — the check VANISHED from `checks`. A missing check
            # reads as "nothing to report", which is how "0 hole-callout objects" stopped
            # being judged at all. It now reports itself as not run.
            record("hole_callouts", True,
                   "孔标注未核对：`include_faces=False`，台账没有圆柱面计数。"
                   "要跑这项检查请传 `include_faces=True`（约 10 倍耗时）。",
                   blocking=False, status="not_checked",
                   hole_callout_objects=holes_in_drawing,
                   diameter_dimensions=diameter_dims)
        record("tolerances_present", toleranced > 0,
               "%d 个尺寸带公差" % toleranced, blocking=False)

        # Overall size (外形尺寸). Report 2026-11-05: the pipeline planned 总长 107 as
        # `kind: derived`, never inserted it (no driving dimension exists to import), and
        # `delivery_check` said 待设计审核 with 0 blockers. A shaft drawing with no overall
        # length cannot be cut — the shop has to add 56+21.5+1.5+1.5+10+17 by hand and hope
        # it closes.
        #
        # This is a PENDING item, not a blocker: a sheet can legitimately omit an overall
        # dimension when it is implied by a chain that does close, and the check cannot tell
        # which. Saying it out loud is the value; refusing delivery would be wrong.
        try:
            bbox_mm = led.get("bbox_mm") or []
            extents = [float(v) for v in bbox_mm if v]
            #  already came from list_drawing_annotations(drawing_path) — the doc
            # handle is not in scope here, and re-opening it would cost a second COM pass.
            sheet_vals = [float(a.get("value_mm")) / 1000.0
                          for a in (anns.get("annotations") or [])
                          if a.get("kind") == "dimension" and a.get("value_mm")
                          # Report 2026-11-06 (P0, B5/B9): an un-deleted 45 degree angle
                          # annotation reported `value_mm: 785.398` — the radian value
                          # 0.785398 scaled by 1000 and treated as millimetres. That made
                          # `delivery_check` announce "图上最大尺寸 785.40 mm 对应模型最大外形
                          # 60.00 mm", which is nonsense and also masked the real problem
                          # (the annotation had left the sheet). Angular and arc-length
                          # dimensions are not lengths; they cannot participate in an
                          # overall-extent comparison at all.
                          and int(a.get("dimension_type") or 0) not in (1, 3)]
            if extents and sheet_vals:
                biggest = max(extents)
                tolerance = max(biggest * 0.01, 0.2)
                has_overall = any(abs(v * 1000 - biggest) <= tolerance for v in sheet_vals
                                  if v)
                record("overall_dimension", has_overall,
                       ("图上最大尺寸 %.2f mm 对应模型最大外形 %.2f mm"
                        % (max(sheet_vals) * 1000, biggest)) if has_overall else
                       ("图上没有任何尺寸接近模型最大外形 %.2f mm（最大只有 %.2f mm）——"
                        "总长/总宽/总高未标注，车间无法下料。这类外形尺寸是包围盒值，"
                        "模型里没有驱动尺寸，auto_dimension 插不进去，"
                        "需用 add_distance_dimension 补。"
                        % (biggest, max(sheet_vals) * 1000)),
                       blocking=False, model_max_mm=round(biggest, 2),
                       sheet_max_mm=round(max(sheet_vals) * 1000, 2))
        except Exception:
            pass

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
            # Report 2026-11-12 §5 (P1-4) asked for `orphan` to be downgraded to a warning
            # whenever the dimension merely sits near its view. That test was measured and
            # REJECTED in 0.8.27: parked dimensions are always within 50 mm of the outline,
            # so proximity excused 15 genuine orphans while the same receipt still claimed
            # every dimension was attached. Proximity stays an OBSERVATION; what changed
            # here is that its distribution is published, and the hint says what to do.
            suffix = ("（其中 %d 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，"
                      "这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认）"
                      % near_view if near_view else "")
            record("dimension_attachments", orphan == 0,
                   (("%d/%d 个尺寸无关联实体%s" % (orphan, dim_count, suffix))
                    if orphan
                    else ("%d 个尺寸均已关联" % dim_count)),
                   excused_count=0,
                   attached_count=attached,
                   near_view_orphans=near_view,
                   far_from_view_orphans=orphan - near_view,
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
            _ledger_dims = rec.get("ledger_dimensions") or 0
            if _ledger_dims == 0:
                # Report 2026-11-10 (P2-5): an imported body has NO driving dimensions, so
                # "0/0 found" is not a coverage failure — it is not applicable. Reporting
                # it as `ok:false` put a permanent pending item on every imported part and
                # held the verdict at 待设计审核 forever, for something the caller cannot
                # fix from this toolkit.
                record("ledger_reconciled", True,
                       "台账 0 个尺寸（导入实体无驱动尺寸）—— 不适用，不是未覆盖",
                       blocking=False, status="not_applicable", ledger_dimensions=0)
            else:
                record("ledger_reconciled",
                       coverage is not None and coverage >= 0.9,
                       "台账覆盖率 %s（%s/%s 个尺寸在图纸上找到，缺 %s）"
                       % (coverage, rec.get("matched"), _ledger_dims, unmatched),
                       blocking=False, status="checked", ledger_dimensions=_ledger_dims)
            # P1-3: a ratio cannot tell you WHAT is missing, and on a "must be repairable
            # from this drawing" job the missing five are the whole point.
            from ..sw_session import fix_mojibake as _fix_mojibake

            # Report 2026-11-12 §5 (P1-5): `unmatched_ledger` mixes two different facts —
            # "no drawing dimension carries this value" and "the value IS on the sheet but
            # an earlier ledger row consumed it". Listing both under `ledger_missing` told
            # the caller to draw six dimensions that were already drawn (measured on
            # 边肋连接件: 30.0 x2 / 5.0 / 15.5 / 9.0 x2 / 15.0, all present).
            _missing_rows = rec.get("missing_truly") or []
            _present_rows = rec.get("present_but_unmatched") or []
            ledger_missing = [
                {"feature": _fix_mojibake(row.get("feature")),
                 "dimension": _fix_mojibake(row.get("dimension")),
                 "value_mm": row.get("value_mm"),
                 "dimension_type": row.get("dimension_type")}
                for row in _missing_rows
            ]
            ledger_unmatched_but_present = [
                {"feature": _fix_mojibake(row.get("feature")),
                 "dimension": _fix_mojibake(row.get("dimension")),
                 "value_mm": row.get("value_mm"),
                 "dimension_type": row.get("dimension_type"),
                 "reason": "图上有同值尺寸，归属待确认 —— 不是缺项"}
                for row in _present_rows
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
            # 0.8.306 (report 2026-11-12 §5 P1-5): the OTHER half of `unmatched_ledger`.
            # These values ARE on the sheet; only the attribution is uncertain, so they
            # must not be listed as missing.
            ledger_unmatched_but_present=ledger_unmatched_but_present,
            ledger_unmatched_but_present_count=len(ledger_unmatched_but_present),
            facts={
                "views": view_count,
                "dimensions": dim_total,
                "dimensions_with_tolerance": toleranced,
                "hole_callouts": holes_in_drawing,
                "geometric_tolerances": gtols,
                "surface_finishes": finishes,
                # Report 2026-11-12 D7: with `include_faces=False` (the default — walking
                # faces costs 10x) the ledger never touches faces, so this reported `0`.
                # On a real A2 part with 22 cylindrical faces that read as "no cylinders"
                # instead of "not counted", and every hole-related judgement inherited it.
                "cylinder_faces_in_model": (hole_candidates if include_faces else None),
                "cylinder_faces_status": ("counted" if include_faces else "not_checked"),
                "dimensions_unattached": unattached,
                "features": model_stats.get("features"),
                "model_doubts": [d.get("kind") for d in doubts],
            },
            # Report 2026-11-07 (P2): the note said orphan dimensions "count as blockers"
            # while `dimension_attachments` actually lands in `pending` — so a reader who
            # trusted the note read `blockers: []` as "no orphans" when the same receipt
            # listed them under `pending`. The note is now generated from where the items
            # really go.
            sheets=sheet_summary,
            sheet_issues=sheet_issues or None,
            note=("verdict 依据流程 §7；blockers 非空即不可宣称可制造，"
                  "ai_supplied 非空则只能算待设计审核。"
                  "无关联实体的尺寸计入 **pending**（不是 blockers）——"
                  "它表示「图纸还不能交付审核」，与「不可制造」是两个等级。"
                  "当前：blockers %d 项、pending %d 项、ai_supplied %d 项。"
                  # `ai_supplied` defaults to None — `len(None)` raised TypeError on every
                  # call that did not pass it, and the whole tool reported
                  # `delivery_check_failed: object of type 'NoneType' has no len()`.
                  # Introduced 0.8.201 when this note started counting items; smoke did not
                  # catch it because that case passes ai_supplied explicitly.
                  % (len(blockers), len(pending), len(ai_supplied or []))),
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
        unattached_plausible = []
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
                # ── `...2` is not the whole story (report 2026-11-08, P2-3) ─────────────
                # `GetAttachedEntityTypes` returns nothing for two dimensions this toolkit
                # created itself with `add_distance_dimension` (a 70 mm overall length and a
                # 48 mm hole spacing), while the rendered leaders plainly touched geometry —
                # `delivery_check` counted them as `2/7 无关联实体`.
                #
                # The `...3` family answers for those. Read the COUNT rather than the array:
                # it is one integer, it needs no VARIANT handling, and it is enough to
                # separate "attached" from "not".
                if not types:
                    try:
                        _n3 = ann.GetAttachedEntityCount3
                        _n3 = int(_n3() if callable(_n3) else _n3)
                        if _n3 > 0:
                            types = [-1] * _n3
                            row["attached_count_source"] = "GetAttachedEntityCount3"
                    except Exception:
                        pass
                row["attached_count"] = len(types)
                row["attached_kinds"] = [SELECT_TYPE_NAMES.get(t, "type_%d" % t) for t in types]
                # Report 2026-11-04 P2-5: three dimensions on a real sheet (65 overall
                # height, R10 fillet, 1 chamfer) were reported as orphans while plainly
                # pointing at geometry. `GetAttachedEntities2` returns empty for dimensions
                # brought in by InsertModelItems — a known behaviour — so "no attachments"
                # alone is not proof of an orphan.
                #
                # The witness is on the DISPLAY dimension, not on `IDimension`:
                # `ann.GetSpecificAnnotation.GetType` answers 2 for a linear dimension on
                # this build, while `dim.GetType` (the object returned by GetDimension2(0))
                # answers 0 — which is why the first version of this check classified every
                # dimension as "type 0" and excused nothing.
                dim_type = None
                try:
                    spec = win.Dispatch(ann.GetSpecificAnnotation)
                except Exception:
                    spec = None
                if spec is not None:
                    try:
                        raw_type = spec.GetType
                        dim_type = int(raw_type() if callable(raw_type) else raw_type)
                    except Exception:
                        dim_type = None
                row["dimension_type"] = dim_type
                # 0.8.303 (report 2026-11-12 §4): `SystemValue` is RADIANS for an angular
                # dimension, so `* 1000` above produced `value_mm: 785.398` for a 45°
                # chamfer — a field named `_mm` carrying degrees, which then fed the orphan
                # and ledger checks. Correct the unit once the type is known, exactly as
                # `list_drawing_annotations` already does for its own rows.
                if _is_angular_dimension_type(dim_type):
                    try:
                        row["value_deg"] = _radians_to_deg(dim.SystemValue)
                        row["value_is_angular"] = True
                    except Exception:
                        row["value_deg"] = None
                    row["value_mm"] = None

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
                    # Before calling this an orphan, check whether the dimension has a
                    # plausible reason to be unattached:
                    #   * diameter/radius dimensions (type 5) are frequently model-item
                    #     imports whose attachment the API does not report;
                    #   * a REFERENCE dimension constrains nothing by definition, so
                    #     "unattached" is expected rather than a defect.
                    # Only the remaining ones are counted as orphans; the rest carry
                    # `unattached_but_plausible` so the distinction is visible instead of
                    # being folded into one number.
                    plausible = None
                    if dim_type == 5:
                        plausible = "diameter_or_radius"
                    elif row.get("is_reference") is True:
                        plausible = "reference_dimension"
                    else:
                        # A linear dimension whose value equals the view's own width or
                        # height is the overall size — it necessarily points at the outer
                        # geometry, and model-item imports of it report no attachments on
                        # this build (measured: 65 mm overall height on a 65 mm tall view).
                        # Matching the OUTLINE, not the position, is what makes this a
                        # geometric check rather than a proximity guess — proximity was
                        # tried before and excused 15 genuine orphans.
                        value = row.get("value_mm")
                        if value:
                            try:
                                box = precise_outline(view)
                            except Exception:
                                box = None
                            if box:
                                extent_w = (box[2] - box[0]) * 1000.0
                                extent_h = (box[3] - box[1]) * 1000.0
                                if (abs(value - extent_w) <= 0.1
                                        or abs(value - extent_h) <= 0.1):
                                    plausible = "matches_view_extent"
                    if plausible:
                        row["unattached_but_plausible"] = plausible
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
                    # A dimension with a plausible reason to be unattached is reported in
                    # `unattached_plausible`, not in `orphans`. The two numbers answer
                    # different questions and were previously the same number, which is why
                    # a fillet radius read as a broken dimension.
                    if plausible:
                        unattached_plausible.append({
                            "view": view.Name, "name": row["name"],
                            "value_mm": row.get("value_mm"),
                            "dimension_type": dim_type,
                            "reason": plausible})
                    else:
                        orphans.append({"view": view.Name, "name": row["name"],
                                        "value_mm": row.get("value_mm"),
                                        "position_m": position,
                                        "dimension_type": dim_type,
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
            unattached_plausible=unattached_plausible,
            unattached_plausible_count=len(unattached_plausible),
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
                                # ── Compare in the same space (2026-11-09) ──────────────────────────────
                        # `view_width` is measured on the SHEET; `radius_m * 2` is the MODEL
                        # diameter. On a 1:2 sheet (scale 0.5) Ø18 is 0.018 model but only 0.009 on
                        # paper, so comparing them raw blocked Ø18 against a 0.0178 threshold and
                        # reported it as an outer contour — which is how the chamfer mouth kept
                        # disappearing from the callout list while Ø16 survived.
                        try:
                            _view_scale = float(view.ScaleDecimal)
                        except Exception:
                            _view_scale = 1.0
                        if not _view_scale:
                            _view_scale = 1.0
                        _dia_on_sheet = radius_m * 2 * _view_scale
                        if view_width and _dia_on_sheet > view_width * 0.4:
                            rejected += 1
                            failed.append({"view": view.Name,
                                           "diameter_mm": round(radius_m * 2000, 2),
                                           "reason": "skipped: circle is >40% of the view "
                                                     "width, treated as an outer contour"})
                            continue
                        candidates.append({
                            "edge": edge,
                            "radius_m": radius_m,
                            # Keep Z. Report 2026-11-09 (N-3 follow-up): dropping it made the
                        # two ends of a through bore, and the bore vs its own chamfer mouth,
                        # all look like "the same centre" — so the chamfer filter paired a
                        # 70 mm-long bore with a 1 mm chamfer edge on the opposite face.
                        "centre": (round(float(params[0]), 4), round(float(params[1]), 4),
                                   round(float(params[2]), 4)),
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

            # ── Drop chamfer edges (report 2026-11-08, P1-2) ────────────────────────
            # A 1x45° chamfer contributes a circle 2 mm smaller than the one it cuts
            # (Ø30 -> Ø28 on a boss, Ø16 -> Ø18 on a bore). Those circles are not holes,
            # but they are circular, concentric and big enough to pass the size filters, so
            # the report got a callout on Ø28 and Ø18.
            #
            # A chamfer is never alone: something coaxial sits within a couple of
            # millimetres of it. Holes are not like that — a counterbore differs from its
            # through hole by several millimetres (Ø11 vs Ø6.6 = 4.4 mm here), and separate
            # holes do not share a centre. So: same centre, radius difference <= 1.5 mm ->
            # the SMALLER one is the chamfer face and is dropped.
            # Two circles belong to the same chamfer only if they share a centre in 3D,
            # not just in projection. The grouping key carries all three coordinates now,
            # so a through bore (two circles 70 mm apart along the axis) can never pair with
            # itself, and a chamfer mouth (same face, 2 mm larger) still will.
            # Cluster centres by DISTANCE, not by exact tuple equality. A chamfer mouth
            # sits one chamfer-depth (about 1 mm) off the face it cuts, so an exact match
            # never paired it; a through bore's two ends sit 70 mm apart and must not pair.
            # 1.5 mm separates those two cases cleanly.
            def _near(a, b, tol=0.0015):
                return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))

            # Store the full KEYS, not just radii: exclusion has to drop every key in the
            # cluster. Members of a cluster have slightly different centre tuples (a chamfer
            # mouth sits ~1 mm off the face), so excluding "the smaller radius at THIS
            # centre" left the other one alive and Ø30 survived as a hole callout.
            _by_centre = {}
            for _k in grouped:
                _placed = False
                for _rep in _by_centre:
                    if _near(_rep, _k[0]):
                        _by_centre[_rep].append(_k)
                        _placed = True
                        break
                if not _placed:
                    _by_centre[tuple(_k[0])] = [_k]
            # Both circles of a close pair are excluded, never just the smaller one.
            #
            # Report 2026-11-09 (N-3): "discard the SMALLER" is right for an OUTER chamfer
            # (Ø30 boss vs Ø28 chamfer face -> drop Ø28) and exactly backwards for an INNER
            # one. On this part the bore wall is Ø16 (r=8) and its chamfer mouth is Ø18
            # (r=9), so the rule deleted the real bore and annotated the chamfer mouth as a
            # hole. A missing callout is recoverable by hand; a callout that says a chamfer
            # is a hole is a wrong part.
            #
            # Which circle is the chamfer depends on the feature, and `IEdge` does not
            # expose that here. So drop BOTH and say so — `ambiguous_chamfer_pairs` in the
            # receipt names them, so a caller can annotate by hand instead of guessing.
            _chamfers = set()
            _ambiguous = []
            # 0.8.294: publish EVERY key this filter adjudicated. The coaxial merge below
            # used to re-derive "is this a chamfer?" from the radius gap — but that same
            # gap describes a shallow counterbore (Ø11 over Ø9.8). Membership is the only
            # fact this filter actually established, so it is handed down explicitly.
            _chamfer_pair_keys = set()
            for _centre, _keys in _by_centre.items():
                if len(_keys) < 2:
                    continue
                _rs = sorted(_k[1] for _k in _keys)
                for _i in range(len(_rs) - 1):
                    if (_rs[_i + 1] - _rs[_i]) <= CHAMFER_RADIUS_GAP_M:
                        _pair_radii = {_rs[_i], _rs[_i + 1]}
                        _chamfer_pair_keys.update(
                            _k for _k in _keys if _k[1] in _pair_radii)
                        # A THROUGH bore shows the same diameter at two distant centres (both
                        # ends of the hole). A chamfer mouth shows up once. So before dropping
                        # the pair, check whether either diameter also appears somewhere else
                        # in this view — if it does, that one is the bore and must stay.
                        #
                        # Report 2026-11-09 (N-3): dropping both left the main Ø16 bore
                        # unannotated, which makes the drawing incomplete. This is the
                        # "is it a through cylinder" test the report asked for, done with
                        # circle centres instead of feature types.
                        _keep_at = set()
                        for _r_probe in (_rs[_i], _rs[_i + 1]):
                            _places = {_k2[0] for _k2 in grouped if _k2[1] == _r_probe}
                            if len(_places) >= 2:
                                _keep_at.add(_r_probe)
                        for _k in _keys:
                            if _k[1] not in _keep_at:
                                _chamfers.add(_k)
                        _ambiguous.append({
                            "centre": [round(float(v), 4) for v in _centre],
                            "narrower_dia_mm": round(_rs[_i] * 2000, 2),
                            "wider_dia_mm": round(_rs[_i + 1] * 2000, 2),
                            "gap_mm": round((_rs[_i + 1] - _rs[_i]) * 1000, 2),
                        })
            if _chamfers:
                grouped = {k: v for k, v in grouped.items() if k not in _chamfers}

            # ── Counterbore and through-hole are ONE feature (report 2026-11-09, Q-2) ────
            # A counterbored hole shows two concentric circles: Ø11 (the counterbore) and
            # Ø6.6 (the through hole). Both survived the radius merge because they are
            # different radii, so the sheet got TWO callouts that are word-for-word
            # identical — each carrying `4 ×`, which reads as eight holes:
            #
            #     4 × Ø 6.60 完全贯穿
            #        ⌴ Ø 11 ↧ 6.80        (twice, on two leaders in the same corner)
            #
            # SolidWorks' own hole callout already writes both lines in one annotation.
            # So keep the WIDER circle of a coaxial pair — its callout carries the
            # counterbore line and the through line — and drop the other.
            # Coaxial means "same axis", NOT "same centre in 3D". Measured on 限位导向套:
            #
            #     Ø11 counterbore  centre [-0.024, 0, 0.015 ]   <- on the face
            #     Ø6.6 through     centre [ 0.024, 0, 0.0082]   <- 6.8 mm down, the cb depth
            #
            # The 1.5 mm clustering used for chamfers therefore never sees them as one
            # feature. Here X/Y identity is what matters and Z is irrelevant.
            #
            # Report 2026-11-09 §4 (fixed 0.8.294): the old `> 0.0015` radius gate here
            # was a SECOND chamfer guess. It also rejected shallow counterbores — Ø11
            # over Ø9.8 is a 0.6 mm radial gap, so both circles survived and produced two
            # identical callouts. The chamfer filter above already decided which keys are
            # chamfer members; `_coaxial_merge_plan` consumes that decision and never
            # looks at a radius magnitude again.
            def _same_axis(_a, _b, tol=0.0005):
                return abs(float(_a[0]) - float(_b[0])) <= tol and \
                       abs(float(_a[1]) - float(_b[1])) <= tol

            _coaxial_dropped = []
            _coaxial_chamfer_protected = 0
            _axis_groups = []
            for _centre, _keys in _by_centre.items():
                _alive = [_k for _k in _keys if _k in grouped]
                if not _alive:
                    continue
                for _grp in _axis_groups:
                    if _same_axis(_grp[0], _centre):
                        _grp[1].extend(_alive)
                        break
                else:
                    _axis_groups.append([_centre, list(_alive)])

            for _centre, _alive in _axis_groups:
                _plan = _coaxial_merge_plan(_alive, _chamfer_pair_keys)
                _coaxial_chamfer_protected += _plan["chamfer_protected"]
                _keep = _plan["keep"]
                for _k in _plan["drop"]:
                    _coaxial_dropped.append({
                        "dropped_dia_mm": round(_k[1] * 2000, 2),
                        "kept_dia_mm": round(_keep[1] * 2000, 2),
                        "centre": [round(float(v), 4) for v in _centre],
                        "reason": "coaxial with a larger circle; its callout already "
                                  "names both diameters",
                    })
                    grouped.pop(_k, None)

            # ── One callout per hole SIZE, not per hole (report 2026-11-08, P1-2) ────
            # A pattern of four identical counterbores produced FOUR separate callouts,
            # each carrying `quantity: 4`. Four annotations all saying "4x Ø11" is worse
            # than one: it reads as sixteen holes, and it is what stacked twelve leaders
            # into one column on the sheet.
            #
            # GB practice is one callout with a count prefix, and `quantity` already holds
            # the number of holes at that size (`len(_group)`). So keep the first group per
            # radius and record how many groups were folded into it.
            _merged_by_radius = {}
            _keep = {}
            for _k in grouped:
                _r = _k[1]
                _merged_by_radius.setdefault(_r, []).append(_k)
                if _r not in _keep:
                    _keep[_r] = _k
            _dropped_groups = sum(len(v) - 1 for v in _merged_by_radius.values()
                                  if len(v) > 1)
            # The surviving group must carry the count of EVERY hole at that diameter,
            # not just the members of its own group. Measured 2026-11-08: four counterbores
            # collapsed to one callout that then said `quantity: 1` — the count was lost in
            # the merge, which is worse than four separate callouts because it under-reports.
            _quantity_by_radius = {}
            for _r, _keys in _merged_by_radius.items():
                _members_total = 0
                for _k in _keys:
                    _members_total += len(grouped[_k])
                # Two faces of one hole collapse into one group already, so the member
                # count IS the hole count when each hole shows one visible face. Cap at the
                # number of distinct centres to avoid double-counting a through hole seen
                # from both sides.
                _centres = len({_k[0] for _k in _keys})
                _quantity_by_radius[_r] = max(_centres, 1) if _members_total else 1
            grouped = {_k: grouped[_k] for _k in _keep.values()}
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
            # Keep a handle for the quantity-prefix pass below. The pop above removes the
            # COM object so it cannot leak into JSON, but that also made it unreachable
            # later — the prefix pass looked for `_annotation`, found None on every item,
            # and silently prefixed nothing (`prefixed: 0` while six Ø4.5 callouts existed).
            if dim_obj is not None:
                item["_display_dimension"] = dim_obj
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
            # ── Read EVERY slot, not just the first non-empty one ────────────────────
            # Report 2026-11-09 (Q-3): the sheet showed two lines —
            #
            #     4 × Ø 6.60 完全贯穿
            #        ⌴ Ø 11 ↧ 6.80
            #
            # but `text` came back as only `<HOLE-SPOT><MOD-DIAM> 11 <HOLE-DEPTH> 6.80`
            # while `native_text_complete` said True. Slot 3 held the through-hole line and
            # was never read, so two callouts of different diameters produced IDENTICAL
            # `text` — any pipeline auditing the drawing against the receipt would call a
            # correct sheet wrong.
            #
            # The sheet was right the whole time. Only the receipt was truncated.
            # 0.8.293: one shared reader for both callout tools. `text` stays a SINGLE
            # native line; every other line goes into `text_lines`. See
            # `_read_native_callout_text` for why the old `" | "` composite and the
            # `len(_parts) > 1` completeness claim were both wrong.
            _read = _read_native_callout_text([("IDisplayDimension", dim_obj)])
            _native = _read["text"]
            # Report 2026-11-04 P1-3, second pass: the native text ALREADY carries the
            # counterbore. Measured on 接槽 with NOTHING rewritten:
            #
            #   slot 1: "<HOLE-SPOT><MOD-DIAM> 8 <HOLE-DEPTH> 4.40"
            #   slot 3: "2 x <MOD-DIAM> 4.50 完全贯穿"
            #
            # That is the counterbore depth AND SolidWorks' own quantity count — everything
            # the report asked us to synthesise. We were destroying it: the old guard was
            # `"<MOD-" not in _native`, so ANY native text containing a format code fell
            # through to SetText and got replaced by a bare "Ø8".
            #
            # A `<MOD-DIAM>` format code is how SolidWorks STORES a diameter; the renderer
            # turns it into Ø. Discarding it because it "looks like a placeholder" is the
            # mistake. Keep the native text whenever there is any.
            if _native:
                item["text"] = _native
                item["text_source"] = "native"
                # Every line that answered, as an ARRAY — no synthetic composite string.
                item["text_lines"] = _read["text_lines"]
                item["text_slots_read"] = _read["text_slots_read"]
                item["text_slots_raw"] = _read["text_slots_raw"]
                item["hw_placeholders_stripped"] = _read["hw_placeholders_stripped"]
                item["text_reader"] = _read["text_reader"]
                # UNKNOWN, not false: six probed slots cannot prove there is no seventh
                # line. The old `len(_parts) > 1` flagged a correct single-line callout as
                # incomplete (false negative) AND 2-of-3 lines as complete (false positive).
                item["native_text_complete"] = None
                item["text_completeness"] = _read["text_completeness"]
                item["text_completeness_reason"] = _read["text_completeness_reason"]
                continue
            # The raw "<MOD-DIAM>" must be replaced or it prints literally, but writing only
            # the diameter leaves the DEPTH in another slot: a Ø15 hole then reads "Ø1515"
            # (the report's "Ø9 -> Θ99"). So write the diameter, then blank the other slots.
            diameter = item.get("diameter_mm")
            # KNOWN LIMIT (2026-11-05): this fallback still prints the diameter sign as the
            # Greek capital theta (Theta) instead of the diameter sign. It is reached ONLY
            # when the native text could not be read at all, which is rare — the native path
            # above is correct and keeps us out of here.
            #
            # Three encodings were measured in the exported PDF and all three fail:
            #   chr(216)      -> Greek theta + 8   (what this line does today)
            #   "%%c"         -> printed literally (a DWG export code, not an internal one)
            #   "<MOD-DIAM>"  -> blank             (the token is swallowed by SetText)
            # The native callouts prove <MOD-DIAM> renders correctly when SolidWorks writes
            # it itself, so the token is fine and SetText is the problem. Without a working
            # way to write the sign, an imperfect glyph beats another broken iteration.
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

        # Report 2026-11-04 P1-3: eight callouts for four M4 counterbores read
        # Ø8 / Ø8 / Ø8 / Ø8 / Ø4.5 / Ø4.5 / Ø4.5 / Ø4.5, with no quantity prefix.
        #
        # The quantity is now REPORTED per callout (and grouped per diameter), but the text
        # is NOT rewritten. Three rewrite attempts were measured in the exported PDF and all
        # three failed, each differently:
        #
        #   SetText with a literal Ø        -> "7× Θ8"        (Greek capital theta)
        #   SetText with the DWG code %%c   -> "7× %%c8"      (printed literally)
        #   SetText with <MOD-DIAM>         -> "7×" then blank (the token was swallowed)
        #
        # Meanwhile the native text needs no help at all. Measured with nothing rewritten:
        #
        #   slot 1: "<HOLE-SPOT><MOD-DIAM> 8 <HOLE-DEPTH> 4.40"   -> prints "Ø8  4.40"
        #   slot 3: "2 x <MOD-DIAM> 4.50 完全贯穿"                 -> prints "2 x Ø4.50 完全贯穿"
        #
        # SolidWorks already writes the counterbore depth AND its own quantity count. Every
        # prefix we added was strictly worse than leaving it alone, so the honest answer is
        # to stop touching the text and report the grouping instead.
        _by_dia = {}
        for _item in created:
            _dia = _item.get("diameter_mm")
            if _dia is not None:
                _by_dia.setdefault(round(float(_dia), 2), []).append(_item)
        for _dia, _group in _by_dia.items():
            # `len(_group)` counts CALLOUTS, not holes: after the same-diameter merge there
            # is exactly one, so every callout said `quantity: 1` even for a 4-hole pattern.
            # `_quantity_by_radius` was computed during the merge and holds the number of
            # distinct centres at that radius — that is the hole count.
            #
            # Key conversion is where the previous two attempts died: `_by_dia` is keyed in
            # MILLIMETRES (`diameter_mm`), `_quantity_by_radius` in METRES (the grouping key
            # is `round(radius_m, 4)`). `diameter_mm / 2000` puts them on the same scale.
            _r_m = round(float(_dia) / 2000.0, 4)
            _holes_at_this_dia = int(_quantity_by_radius.get(_r_m, 0) or 0)
            for _item in _group:
                _item["quantity"] = max(_holes_at_this_dia, 1)
                if _holes_at_this_dia > 1:
                    # Say where the number came from — a caller checking the sheet against
                    # the model wants to know this is a distinct-centre count, not a guess.
                    _item["quantity_source"] = "distinct_centres_at_this_diameter"

        for _item in created:
            _item.pop("_display_dimension", None)

        # Report 2026-11-05 (P0-C): this flagged every NATIVE callout as unresolved.
        #
        # `<MOD-DIAM>` is how SolidWorks STORES a diameter in the text layer; the renderer
        # turns it into Ø. The PDF of 接槽 printed "2 × Ø4.50 完全贯穿" and "⌴ Ø8 ↧ 4.40"
        # correctly while the receipt said `text_unresolved: true` and advised deleting the
        # callouts — advice that would have removed the only annotation capability that
        # actually renders.
        #
        # Only text this tool WROTE can be unresolved: a SetText that failed to take leaves
        # the raw code in a string we own. Native text is SolidWorks' own and always renders.
        # `text_source` is the discriminator; `native_text_complete` is the corroborating
        # flag, and the two must never disagree.
        unresolved = [c for c in created
                      if c.get("text_source") != "native"
                      # `native_text_complete` is UNKNOWN (None) even after a full read —
                      # it must never act as a verdict here. Only text this tool WROTE
                      # can be unresolved; native text always renders.
                      and c.get("native_text_complete") is not True
                      and "<MOD-" in str(c.get("text") or "").upper()]
        for item in unresolved:
            item["text_unresolved"] = True

        return drawing_ok(
            drawing=doc.GetTitle,
            circular_edges=scanned,
            # How many same-diameter groups were folded into one callout. A non-zero value
            # means the sheet has one leader per hole SIZE, not one per hole.
            duplicate_groups_merged=_dropped_groups,
            # Concentric circle pairs within 1.5 mm of each other. One of the two is a
            # chamfer face and the other is often the real hole, and which is which cannot
            # be told from here — so neither is annotated. Annotate these by hand.
            ambiguous_chamfer_pairs=_ambiguous[:10],
            # ── Say what was left out, and what it might be (report 2026-11-09, Q-1) ─────
            # The pairs above are skipped because "which of the two is the chamfer" cannot
            # be answered from an edge. On 限位导向套 that silently dropped Ø16 — the main
            # through bore — leaving `created_count: 2, ok: true`, which reads as "two holes,
            # done". The receipt named the pair but never said a real hole might be missing.
            #
            # These fields make the omission countable: every skipped diameter is listed,
            # with a flag when the pair is concentric with the view centre (the usual shape
            # of a main bore or its boss).
            coaxial_duplicates_dropped=_coaxial_dropped[:10],
            # 0.8.294: a NON-ZERO value means the chamfer filter, not a radius guess,
            # kept these circles out of the counterbore merge. Report §4 asked for
            # "is it a chamfer pair" as the discriminator; this makes the decision
            # countable in a real receipt instead of inferable from source comments.
            coaxial_chamfer_protected=_coaxial_chamfer_protected,
            # Partial work is not success. See the verdict comment above.
            ok=(not _ambiguous),
            # Report 2026-11-10 (S-3): `ok:false` here means "the drawing is INCOMPLETE",
            # not "the call failed" — the tool did run, created callouts and saved them.
            # A pipeline reading ok:false as a transport error will retry or roll back a
            # perfectly good partial result, which is a different problem from the one
            # `ok:false` was introduced to prevent. Keep `ok` as the completeness gate (it
            # is what stops a batch runner shipping a sheet with no main bore) but give the
            # execution status its own field so the two can be told apart.
            execution_ok=True,
            completeness=("incomplete" if _ambiguous else "complete"),
            excluded_diameters_mm=sorted(
                {a["narrower_dia_mm"] for a in _ambiguous}
                | {a["wider_dia_mm"] for a in _ambiguous}),
            unannotated_note=(
                ("%d 个直径因倒角配对无法判定而未标注：%s。其中可能有主孔。"
                 "**请在 SolidWorks 里手工标** —— 两条自动路径都已实测证伪："
                 "`add_hole_callout` 曾把贯通孔写成「深 68」的盲孔；v0.8.291 起它会"
                 "撤销该标注并返回 `ok:false / verified:false`，不再往图上留错字。"
                 "`add_drawing_dimension(style=\"diameter\")` 的尺寸文字渲染不出来"
                 "（写 `<MOD-DIAM><DIM>` 得到 `Ø<DIM>16`，写数值得到 `Ø1616`）。"
                 "位置见 `ambiguous_chamfer_pairs[].centre`（模型坐标，米）。"
                 % (len({a["narrower_dia_mm"] for a in _ambiguous}
                        | {a["wider_dia_mm"] for a in _ambiguous}),
                    "、".join("Ø%s" % d for d in sorted(
                        {a["narrower_dia_mm"] for a in _ambiguous}
                        | {a["wider_dia_mm"] for a in _ambiguous}))))
                if _ambiguous else None),
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
            callout_groups_by_diameter={("%g" % k): len(v) for k, v in
                                        sorted(_by_dia.items())},
            unresolved_note=(("%d 条标注由本工具改写文字后仍留有未解析格式码，渲染可能是"
                              "字面量。原生标注（text_source=native）不在此列——它们的 "
                              "<MOD-*> 是 SolidWorks 内部表示，渲染器会正确解析成 Ø。")
                             % len(unresolved)) if unresolved else None,
            text_resolution_basis=("native text is rendered by SolidWorks and is never "
                                   "counted as unresolved; only SetText-rewritten text can be"),
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
            # Report 2026-11-09 (R-2): `ok: true` sat next to `verdict: "partial"` and four
            # excluded diameters. On 限位导向套 that meant "one callout created, main Ø16 bore
            # skipped" was reported as success — a batch pipeline keyed on `ok` would ship
            # the sheet. Excluded diameters now count as partial, and `ok` follows `verdict`.
            verdict=("partial" if _ambiguous else
                     ("ok" if created and not failed else
                      ("partial" if created else
                       ("no_native_callout" if groups_total else "nothing_to_annotate")))),
            partial_reason=(
                ("%d 个直径因倒角配对无法判定而未标注（%s）。ok=false：按 ok 判断的批处理"
                 "不得把这张图当成完整。"
                 % (len({a["narrower_dia_mm"] for a in _ambiguous}
                        | {a["wider_dia_mm"] for a in _ambiguous}),
                    "、".join("Ø%s" % d for d in sorted(
                        {a["narrower_dia_mm"] for a in _ambiguous}
                        | {a["wider_dia_mm"] for a in _ambiguous}))))
                if _ambiguous else None),
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
        ledger_by_name = {}
        ledger_rows_duplicate_count = 0
        for feature in led.get("features") or []:
            for dim in feature.get("dimensions") or []:
                kind = dim.get("dimension_type")
                # 0.8.303 (report 2026-11-12 §4): `3` is ANGULAR on this binding, not
                # linear. Accepting it put a 45° chamfer into the ledger as `785.398 mm`,
                # which then surfaced as `drawing_only_values: [785.4]` and dragged
                # `coverage` down to 0.7 on a sheet where nothing was actually missing.
                # Only plain lengths (0) and diameters (2) may enter a length comparison.
                if kind is not None and kind not in (0, 2):
                    skipped_non_length.append({
                        "feature": feature.get("name"),
                        "value": dim.get("value_mm"), "type": kind,
                        "reason": ("angular" if _is_angular_dimension_type(kind)
                                   else "non_length"),
                    })
                    continue
                value = dim.get("value_mm")
                if ceiling and value is not None and value > ceiling:
                    skipped_oversize.append({
                        "feature": feature.get("name"), "value": value,
                    })
                    continue
                # ONE ROW PER MODEL DIMENSION, not per (dimension, feature) pair.
                #
                # Report 2026-11-05 (P2): `D2@Sketch1@AM_DOOR.Part` appeared twice — once
                # for Sketch1 and once for Base-Flange1, the sheet-metal feature that
                # consumes that sketch — so 18 rows described 13 real dimensions, and
                # `ledger_dimensions: 37` was inflated the same way. Every downstream number
                # inherited it: coverage (matched / ledger_dimensions) was systematically
                # pushed DOWN, and `missing_truly: 18` told callers to draw five dimensions
                # that already existed.
                #
                # The full name IS the model-level identity, so it is the only key. The
                # feature that "owns" the dimension is the first one seen; every other
                # consumer is kept in `features` so the attribution is not lost.
                name = dim.get("name")
                key = name or ("%s#%s" % (feature.get("name"), value))
                existing = ledger_by_name.get(key)
                if existing is None:
                    ledger_by_name[key] = {
                        "feature": feature.get("name"),
                        "feature_type": feature.get("type"),
                        "features": [feature.get("name")],
                        "dimension": name,
                        "value_mm": value,
                        "dimension_type": kind,
                    }
                else:
                    owner = feature.get("name")
                    if owner not in existing["features"]:
                        existing["features"].append(owner)

        ledger_rows = list(ledger_by_name.values())
        ledger_rows_duplicate_count = sum(
            max(len(r.get("features") or []) - 1, 0) for r in ledger_rows)

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
            # How many rows the old (dimension, feature) pairing would have produced.
            # Report 2026-11-05 P2 saw 18 rows for 13 dimensions; without this number a
            # reader cannot tell whether the ledger is clean or merely quiet.
            ledger_rows_duplicate_count=ledger_rows_duplicate_count,
            coverage_formula="matched / ledger_dimensions (one-to-one, unique names)",
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
    """Render the current page to a PIL image.

    Calls `SaveAsSilent`. Whether that leaves the document editable afterwards is NOT
    established — see the corrected note on `export_drawing`. A report measured `add_note`
    working fine after an export on SW2024 SP4.

    What IS established: this function was the "after" half of the before/after diff inside
    `add_center_marks`, and that diff came back as 0 changed pixels three times. The most
    likely explanation is a defect in that experiment rather than in `SaveAsSilent`, so no
    conclusion about mid-pipeline rendering is drawn here.
    """
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
@perf_hooks.timed("dimension_gaps")
def _dimension_full_names(drawing_path, doc=None):
    """Every dimension's `FullName` in the drawing, in view order.

    Used as an insert-snapshot: the difference before/after a call is exactly the set of
    annotations that call created. `FullName` is the only stable identity here —
    `IAnnotation.GetName` gives sheet-unique labels like 细节项目34 that are reused, and
    coordinates can collide with existing work. Report 2026-11-06 asked for precisely this
    so that a wrong insert can be undone without a fuzzy match.

    Pass the OPEN document when you have one. Report 2026-11-06: the rollback called this
    with a path while `add_distance_dimension(save=False)` had only touched the in-memory
    document, so `_open_drawing` re-read the file on DISK, saw the pre-insert state, and the
    difference came out empty — the wrong dimension was never deleted and the receipt said
    `rolled_back: false` with no reason. Measured on the AM_DOOR fixture: 1570.796 stayed on
    the sheet.
    """
    names = []
    import win32com.client as win
    if doc is None:
        try:
            doc = _open_drawing(drawing_path)
        except Exception:
            return names
    try:
        for view in _iter_views(doc):
            for annotation in (view.GetAnnotations or ()):
                # `annotation.GetDimension2(0)` does NOT exist on the annotation object
                # (measured: "未知名称" / "<unknown>.GetDimension2"). The project's own
                # reader goes through `GetSpecificAnnotation` first — reuse it rather than
                # re-deriving the chain.
                full = _annotation_dimension_full_name(annotation, win)
                if full:
                    names.append(full)
    except Exception:
        pass
    return names


def _edges_define_a_length(rows, edge_a, edge_b, parallel_tol=0.02):
    """Can these two edges produce a LENGTH dimension? Judge BEFORE calling AddDimension2.

    Report 2026-11-06 (root cause of the 1570.796 phantom): `AddDimension2` between two
    non-parallel edges creates an ANGULAR dimension, whose `SystemValue` is radians.
    Multiplied by 1000 a right angle becomes "1570.796 mm". No post-hoc value comparison
    can recover from that — the quantity is the wrong kind, not the wrong size.

    Two conditions must hold for a distance:
      * neither edge is degenerate (start == end) — a point defines no direction;
      * the two directions are parallel (or anti-parallel) within `parallel_tol` of the
        unit-vector cross product magnitude. Perpendicular edges give an angle instead.

    Returns (ok, reason). `reason` is a short code the caller puts in the receipt.
    """
    def _ends(idx):
        for row in rows:
            if row.get("index") == idx:
                a = row.get("model_start")
                b = row.get("model_end")
                if a and b and len(a) >= 3 and len(b) >= 3:
                    return ([float(a[0]), float(a[1]), float(a[2])],
                            [float(b[0]), float(b[1]), float(b[2])])
                return None
        return None

    pa, pb = _ends(edge_a), _ends(edge_b)
    if pa is None or pb is None:
        return False, "edge_endpoints_unavailable"
    va = [pa[1][i] - pa[0][i] for i in range(3)]
    vb = [pb[1][i] - pb[0][i] for i in range(3)]
    la = sum(v * v for v in va) ** 0.5
    lb = sum(v * v for v in vb) ** 0.5
    if la < 1e-9 or lb < 1e-9:
        return False, "degenerate_edge"
    ua = [v / la for v in va]
    ub = [v / lb for v in vb]
    cross = [ua[1] * ub[2] - ua[2] * ub[1],
             ua[2] * ub[0] - ua[0] * ub[2],
             ua[0] * ub[1] - ua[1] * ub[0]]
    if (sum(c * c for c in cross)) ** 0.5 > parallel_tol:
        # Angle between the directions, for the receipt.
        dot = max(-1.0, min(1.0, sum(ua[i] * ub[i] for i in range(3))))
        import math as _math
        return False, "not_parallel_%.1fdeg" % _math.degrees(_math.acos(dot))
    return True, None


def _dimension_selection_names(doc):
    """Full names that `IAnnotation.Select` actually matches on, with the annotation.

    CORRECTION 2026-11-07 — this closes the last "impossible" item from the v0.8.173
    reports. `com-quirks` 56 recorded that deleting a just-inserted dimension failed at
    four levels and could not be made to work. The fourth level was wrong: there are simply
    TWO name spaces, and I was comparing across them.

        IDimension.FullName            D1@Sketch1@AM_50ef1f.Part          <- model side
        IDisplayDimension.GetNameForSelection
                                       D1@Sketch1@AM_50ef1f-1@工程图视图1  <- sheet side

    `FullName` names the MODEL dimension; the sheet wrapper adds the view. A difference
    computed on `FullName` can never match anything `Select` accepts, so the rollback
    reported `rolled_back: false` with an empty list.

    Measured 2026-11-07 on the door fixture: snapshot 12 model names vs 12 selection names,
    `same set: False`; comparing on the selection name and then `Select(False)` +
    `EditDelete` took the sheet from 7 dimensions to 6.

    So: snapshot with THIS, diff with THIS, and delete by walking the annotations and
    comparing this name — `ModelDocExtension.SelectByID2` cannot be used here at all
    (it demands a non-null Callout, measured: None / 0 / Missing / Empty all raise
    类型不匹配 at arg 8).
    """
    import win32com.client as win

    try:
        import win32com.client.gencache as _gencache
        import importlib
        import sys as _sys
        cache = _gencache.GetGeneratePath()
        if cache and cache not in _sys.path:
            _sys.path.insert(0, cache)
        _mod = importlib.import_module(
            "83A33D31-27C5-11CE-BFD4-00400513BB57x0x32x0")
    except Exception:
        _mod = None

    # TRI-STATE (report 2026-11-07, P0-1) — the third state is the point:
    #     None   the read FAILED            -> nothing may be deleted
    #     []     the sheet truly has none   -> safe to delete what appeared
    #     [...]  the sheet's dimensions
    #
    # The caller got `[]` for BOTH "empty" and "reader failed", so its rollback guard
    # (`bool(_names_before)`) refused to delete on a sheet that started with ZERO
    # dimensions — exactly the first auto-dimension run. A wrong dimension (146.5 mm
    # for a 32.5 mm gap) then stayed on the sheet forever, and the next call rolled
    # back only its own.
    #
    # Same empty-collection ambiguity that `open_document_paths()` was fixed for in
    # 0.8.161; it came back here in a different form.
    found = []
    _views_read = 0
    _views_failed = 0
    try:
        _model_views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
    except Exception:
        return None
    if not _model_views:
        return []
    for view in _model_views:
        try:
            _raw_annotations = list(view.GetAnnotations or ())
            _views_read += 1
        except Exception:
            _views_failed += 1
            continue
            continue
        for raw in _raw_annotations:
            try:
                annotation = win.Dispatch(raw)
                if int(annotation.GetType) != 4:
                    continue
            except Exception:
                continue
            name = None
            if _mod is not None:
                try:
                    specific = annotation.GetSpecificAnnotation
                    name = _mod.IDisplayDimension(
                        specific._oleobj_).GetNameForSelection()
                except Exception:
                    name = None
            if not name:
                # Fall back to the model full name; it will not match a fresh selection,
                # but it lets the caller see something instead of an empty string.
                name = _annotation_dimension_full_name(annotation, win)
            if name:
                found.append((name, annotation))
    # Every view failed to enumerate: the reader is broken, not the sheet. Saying
    # "no dimensions" here would make the caller treat a full sheet as empty.
    if _views_failed and not _views_read and not found:
        return None
    return found


def plan_dimension_gaps(model_path, drawing_path, tolerance_mm=0.05,
                        max_candidates_per_gap=3, apply_gaps=False,
                        apply_indices=None, apply_candidate=0):
    """PR4: turn reconcile_ledger's `missing_truly` into actionable edge pairs.

    The spec asks for the missing-coverage list FIRST and manual dimensions only for real
    gaps. `reconcile_ledger` already separates `missing_truly` (no drawing dimension carries
    that value at all) from `present_but_unmatched` (the value is drawn, attribution is
    unclear). Only the first kind is a gap worth filling, so only that is considered here.

    Why this does not just call AddDimension2 blindly:

      * `AddDimension2` takes only (x, y, z) — the measurement comes from the CURRENT
        SELECTION. A gap is a VALUE, not a piece of geometry, so the geometry has to be
        found and selected first.
      * Guessing which two edges measure 14 mm is exactly the kind of inference that
        produces a plausible-looking wrong dimension. So a gap becomes a candidate list
        (edge pairs whose sheet distance matches within `tolerance_mm`), and nothing is
        drawn unless `apply_gaps=True`.
      * Even then the pair must be UNIQUE. Two pairs matching the same value is an
        ambiguity, and picking one would be a coin flip dressed as automation.

    Returns per-gap `candidates` plus a `status`:
      fillable          — exactly one matching edge pair, safe to apply
      ambiguous         — more than one pair matches; caller must choose
      no_geometry       — no edge pair measures that value (needs a section or detail view)
      already_present   — a dimension with that value appeared since the reconcile ran
    """
    try:
        rec = reconcile_ledger(model_path, drawing_path, tolerance_mm=tolerance_mm)
        if not rec.get("ok"):
            return error("reconcile_failed", rec.get("message") or "reconcile failed")

        gaps = rec.get("missing_truly") or []
        if not gaps:
            return drawing_ok(
                gaps_found=0, filled=0, results=[],
                note=("reconcile_ledger reports no truly-missing dimensions — every planned "
                      "value is drawn somewhere. Nothing to compensate."),
                coverage=rec.get("coverage"),
                present_but_unmatched=rec.get("present_but_unmatched_count"))

        edges_payload = list_drawing_edges(drawing_path)
        if not edges_payload.get("ok"):
            return error("edges_failed",
                         edges_payload.get("message") or "cannot enumerate drawing edges",
                         "Compensation needs the sheet's edges to measure between.")
        rows = edges_payload.get("edges") or []
        points = []
        # Report 2026-11-05 (P0): candidates were selected purely on SHEET distance —
        # `|paper span - target| <= tolerance` — while `add_distance_dimension` measures
        # MODEL geometry. On AM_DOOR the two disagreed by two orders of magnitude: a paper
        # span of 8 mm produced a 1570.796 mm dimension (the pair was an arc). Three calls
        # left 15 absurd dimensions because nothing compared the two.
        #
        # So carry BOTH coordinate systems. The sheet pair decides where the dimension can
        # sit; the model pair decides whether it will measure the right thing. A candidate
        # that matches on paper but not in the model is discarded before it is offered.
        #
        # For a straight edge the model point is its start vertex. For a circular edge the
        # only meaningful model point is the CENTRE — which is exactly right for the axial
        # gaps this is meant to serve (two end-face circles on a shaft are their centre
        # distance apart).
        points = []
        model_points = {}
        _model_fallback = []
        for idx, row in enumerate(rows):
            pt = row.get("sheet_center") or row.get("sheet_point_on_circle") or row.get("sheet_start")
            if pt:
                points.append((idx, float(pt[0]), float(pt[1])))
            else:
                # Report 2026-11-06 (P0, A2/A3): on a 60x60 part the axis mapping is
                # ambiguous, so `list_drawing_edges` reports `sheet_projected: false` and
                # NO row carries a sheet point. `points` came out empty, and the whole tool
                # returned `not_enough_edges / "found 0"` — every sheet-coordinate-based gap
                # finder died on exactly the parts that need it (a square is not exotic).
                #
                # The model coordinates are still there. Fall back to them: a distance is
                # still a distance, we just lose the ability to place the dimension nicely.
                # The receipt says which space was used, so a caller can tell.
                m = row.get("model_center") or row.get("model_start")
                if m and len(m) >= 2:
                    _model_fallback.append((idx, float(m[0]), float(m[1]),
                                            float(m[2]) if len(m) > 2 else 0.0))
            mpt = row.get("model_center") or row.get("model_start")
            if mpt and len(mpt) >= 2:
                model_points[idx] = (float(mpt[0]), float(mpt[1]),
                                     float(mpt[2]) if len(mpt) > 2 else 0.0)

        _coordinate_space = "sheet"
        if not points and _model_fallback:
            # Use the first two axes of the model; the matching loop below already tries
            # both axes of whatever pair it is given, and a third pass is not worth the
            # combinatorial cost for a fallback that only exists to avoid a hard zero.
            points = [(idx, x, y) for idx, x, y, _z in _model_fallback]
            _coordinate_space = "model_fallback"

        # Report 2026-11-04 P1-2: 13 gaps, all `no_geometry`, including values that are
        # plainly measurable — 31 (two counterbore centres), 12 (Ø5 spacing), 13 (Ø14.2
        # position). Those are CIRCLE-to-CIRCLE distances, and the point set above holds only
        # straight edges, so every hole-pitch gap was guaranteed to miss.
        #
        # Circle centres are appended with a synthetic negative index so they cannot collide
        # with edge indices, and tagged so a candidate can say which kind of pair it is.
        circle_points = []
        try:
            circles_payload = list_circular_edges(drawing_path)
            for ci, circle in enumerate(circles_payload.get("circles") or []):
                if circle.get("x") is None or circle.get("y") is None:
                    continue
                circle_points.append(
                    (-(ci + 1), float(circle["x"]), float(circle["y"]),
                     circle.get("diameter_mm")))
        except Exception as _exc:
            circle_points = []
            _circle_error = str(_exc)[:160]
        else:
            _circle_error = None
        # Circle candidates carry synthetic negative indices from `list_circular_edges`,
        # which reports SHEET x/y only. Match them back to the edge rows by sheet position
        # so they get a model point too — otherwise the model gate silently passes them
        # (`model_span_mm: None`) and the 1570.796 class of error survives for exactly the
        # pairs that produced it.
        for _pid, _cx, _cy, _d in circle_points:
            if _pid in model_points:
                continue
            for _idx, _row in enumerate(rows):
                _sc = _row.get("sheet_center")
                if not _sc:
                    continue
                if abs(float(_sc[0]) - _cx) < 1e-4 and abs(float(_sc[1]) - _cy) < 1e-4:
                    _m = _row.get("model_center")
                    if _m and len(_m) >= 2:
                        model_points[_pid] = (float(_m[0]), float(_m[1]),
                                              float(_m[2]) if len(_m) > 2 else 0.0)
                    break
        points = points + [(pid, x, y) for pid, x, y, _ in circle_points]
        if len(points) < 2:
            return error("not_enough_edges",
                         "need at least two edges with coordinates, found %d (space: %s)"
                         % (len(points), _coordinate_space),
                         ("The sheet projection was unavailable AND the model fallback found "
                          "nothing. On a part whose X/Y extents are equal the axis mapping is "
                          "ambiguous (`axes_mismatch`), so no sheet points exist — the model "
                          "coordinates are the only route, and they are missing here too. "
                          "Use add_distance_dimension(edge_a, edge_b) with explicit indices "
                          "from list_drawing_edges."))

        results = []
        for gap in gaps:
            value = gap.get("value_mm")
            entry = {
                "feature": gap.get("feature"),
                "full_name": gap.get("dimension_full_name") or gap.get("dimension"),
                "value_mm": value,
                "candidates": [],
                "status": "no_geometry",
            }
            if value is None:
                entry["status"] = "no_value"
                results.append(entry)
                continue
            target = float(value)
            matches = []
            for i in range(len(points)):
                for j in range(i + 1, len(points)):
                    _, x1, y1 = points[i]
                    _, x2, y2 = points[j]
                    dx, dy = abs(x2 - x1), abs(y2 - y1)
                    # A dimension measures along ONE axis for linear extents; accept a match
                    # on either axis and record which, rather than a euclidean distance that
                    # no linear dimension ever uses.
                    for axis, span_m in (("x", dx), ("y", dy)):
                        # Sheet coordinates are METRES; `target` is millimetres and
                        # `tolerance_mm` is millimetres. Comparing them directly made the
                        # tolerance 1000x too tight in practice: a 31 mm gap spans 0.031 in
                        # sheet units, so |0.031 - 31| = 30.97 never cleared 0.05. That is
                        # why every part since PR4 reported 13/13 `no_geometry` — the matcher
                        # could not match anything, at any tolerance.
                        span_mm = span_m * 1000.0
                        if abs(span_mm - target) <= tolerance_mm:
                            # Second gate: does the MODEL agree? A sheet-space match only
                            # says the dimension would LOOK right where it is placed. The
                            # value SolidWorks writes comes from the model. Report
                            # 2026-11-05: an 8.0 mm paper gap became a 1570.796 mm
                            # dimension because only the paper was consulted.
                            _ia, _ib = points[i][0], points[j][0]
                            _ma, _mb = model_points.get(_ia), model_points.get(_ib)
                            _model_span_mm = None
                            if _ma and _mb:
                                _model_span_mm = ((_ma[0] - _mb[0]) ** 2
                                                  + (_ma[1] - _mb[1]) ** 2
                                                  + (_ma[2] - _mb[2]) ** 2) ** 0.5 * 1000.0
                            # Report 2026-11-06 (P1): this used to `continue` when the model
                            # span missed the target. That was WRONG, and it was mine.
                            #
                            # The sheet comparison above uses a SINGLE AXIS (`abs(x2-x1)`
                            # or `abs(y2-y1)`) because a linear dimension measures along one
                            # axis. The model figure here is a 3D EUCLIDEAN distance between
                            # two vertices. On a drawing those are different quantities: the
                            # sheet is a projection, so a 3.05 mm axial gap can span tens of
                            # mm in the model's depth direction. Comparing them is an
                            # apples-to-oranges test that rejects correct candidates.
                            #
                            # Measured on AM_DOOR: 13 gaps, 5 paper matches on one of them,
                            # ALL dropped by this gate -> 12 `no_geometry` + 1
                            # `no_applicable_candidate`, and `apply_gaps` produced nothing.
                            # The v0.8.156 post-insert value check already catches a
                            # genuinely wrong pair (`actual_value_mm` vs planned), so the
                            # gate only cost candidates without adding safety.
                            #
                            # Kept as EVIDENCE (`model_span_mm`), not as a filter.
                            # `model_check` states whether the figure exists, so "cannot
                            # verify" never looks like "verified".
                            # Circle centres carry SYNTHETIC NEGATIVE indices (see above).
                            # `add_distance_dimension` indexes a REAL edge array, so such a
                            # pair can never be selected. Mark it here instead of letting
                            # the caller discover it as a misleading `bad_edge_index`.
                            _circle = _ia < 0 or _ib < 0
                            # Report 2026-11-07 (P1, filed independently): `fillable: 6` on
                            # AM_DOOR and `fillable: 1` on 接槽, then `apply_gaps` refused
                            # every one of them with `edge_pair_has_no_length` —
                            # `applied_count: 0`. Fourteen gaps, zero产出.
                            #
                            # The check was in the WRONG PHASE: `_edges_define_a_length` ran
                            # inside the apply loop, so the plan advertised candidates that
                            # could never land. Whether two edges define a length is a
                            # STATIC property of the geometry — it belongs here, while the
                            # candidate is being built, not at the moment of insertion.
                            #
                            # Now a non-length pair is marked `applicable: false` with a
                            # reason, so `fillable` only ever means "will land".
                            _length_ok, _length_why = (
                                (None, None) if _circle
                                else _edges_define_a_length(rows, _ia, _ib))
                            matches.append({
                                "edge_a": _ia, "edge_b": _ib,
                                "candidate_kind": ("circle_centre" if _circle else "edge"),
                                "applicable": (not _circle) and (_length_ok is not False),
                                "defines_length": _length_ok,
                                "length_check_reason": _length_why,
                                "axis": axis, "measured_mm": round(span_mm, 3),
                                "model_span_mm": (round(_model_span_mm, 3)
                                                  if _model_span_mm is not None else None),
                                "model_check": ("approximate_3d_distance"
                                                if _model_span_mm is not None
                                                else "unavailable"),
                                "delta_mm": round(span_mm - target, 4),
                                "centres": [[round(x1, 4), round(y1, 4)],
                                            [round(x2, 4), round(y2, 4)]],
                            })
            matches.sort(key=lambda m: abs(m["delta_mm"]))
            # Report 2026-11-05 (P1-A): `status` used `len(matches)` — the FULL match count —
            # while `candidates` was truncated to `max_candidates_per_gap`. With max=1 every
            # gap kept exactly one candidate and still reported `ambiguous`, so the
            # automatic path skipped all six. The two numbers must come from the same list.
            #
            # And `status` must count only APPLICABLE candidates: a gap whose single match
            # is a circle centre cannot be applied either, and calling that `fillable`
            # ("safe to apply") is the same misreport one level down.
            _kept = matches[:max_candidates_per_gap]
            entry["candidates"] = _kept
            _usable = [i for i, c in enumerate(_kept) if c.get("applicable", True)]
            entry["applicable_candidates"] = _usable
            entry["candidates_matched_before_truncation"] = len(matches)
            entry["candidates_truncated"] = len(matches) > len(_kept)
            # ── Confidence, from the match count (report 2026-11-07, C8) ────────────────
            # On 接槽 a 50 mm gap matched **696** edge pairs. Keeping three of them and
            # calling one "the" dimension is not a plan, it is a coincidence: with that many
            # pairs satisfying a pure distance test, at least one will look right by luck.
            #
            # The count itself is the signal. A real dimension lines up with a handful of
            # pairs; hundreds means the test has no discriminating power on this geometry.
            # LOW CONFIDENCE GAPS ARE NEVER `fillable`, so `apply_gaps` cannot land one by
            # accident — the caller has to name it with `apply_indices` on purpose.
            _n_matches = len(matches)
            if _n_matches > LOW_CONFIDENCE_MATCH_LIMIT:
                entry["confidence"] = "low"
                entry["confidence_reason"] = (
                    "%d 条边对都满足这个距离 —— 纯距离匹配在这份几何上没有区分度，"
                    "命中的多半是坐标巧合（实测接槽 50 mm 匹配 696 条）。"
                    "要落地请用 apply_indices 明确指定，并先核对候选。" % _n_matches)
            elif _n_matches > 1:
                entry["confidence"] = "medium"
                entry["confidence_reason"] = "%d 条边对匹配" % _n_matches
            elif _n_matches == 1:
                entry["confidence"] = "high"
                entry["confidence_reason"] = "唯一匹配"
            else:
                # ZERO matches used to fall into the `else` above and be reported as
                # "唯一匹配 / high" — so a `no_geometry` gap (no candidate pair at all)
                # read as a certain answer with `candidates: []` (report 2026-11-12 §5).
                # "No candidates" and "exactly one candidate" are opposite facts.
                entry["confidence"] = "none"
                entry["confidence_reason"] = "没有候选边对（与「唯一匹配」相反）"

            _low = entry.get("confidence") == "low"
            if _low:
                # Deliberately NOT fillable and NOT ambiguous: it is a different failure —
                # "too many answers" rather than "one/some answers".
                entry["status"] = "low_confidence"
            elif len(_usable) == 1:
                entry["status"] = "fillable"
            elif len(_usable) > 1:
                entry["status"] = "ambiguous"
            elif matches:
                # Geometry exists, but none of it can be handed to add_distance_dimension.
                # Split the reason: a circle centre is a selection problem (change
                # candidate), while a non-length edge pair is a geometry problem (this
                # gap cannot be dimensioned by ANY edge pair in this view).
                _reasons = {c.get("length_check_reason") for c in _kept
                            if c.get("length_check_reason")}
                if _reasons and all(not c.get("defines_length", True) for c in _kept):
                    entry["status"] = "no_length_pair"
                    entry["reason"] = "、".join(sorted(_reasons))
                else:
                    entry["status"] = "no_applicable_candidate"
            results.append(entry)

        fillable = [r for r in results if r["status"] == "fillable"]
        applied = []
        # Filled in after the apply loop: this must EXCLUDE gaps the same call just
        # filled. Report 2026-11-05: a receipt carrying both "applied 13.0" and
        # "13.0 not applied" invites the caller to dimension the same feature twice.
        _ambiguous_not_applied = []
        _all_cands = [c for r in results for c in (r.get("candidates") or [])]
        _candidates_skipped = 0
        # Report 2026-11-05: `apply_gaps=True` could only act on `status == "fillable"`,
        # which requires EXACTLY ONE candidate. The reported part had 7 gaps, all
        # `ambiguous` with 2-3 candidates each at delta_mm 0.0 — so the tool announced ten
        # precise edge pairs and then refused to place any of them. "Candidates are right
        # but nothing gets drawn" is the same dead end the report hit two versions ago.
        #
        # `apply_indices` lets the caller pick which gaps to fill (0-based, in `gaps`
        # order) and `apply_candidate` which candidate to use. That keeps the tool honest —
        # it still will not GUESS between equally valid pairs — while making the data it
        # already computed actually usable.
        _explicit = set()
        if apply_indices:
            for _i in apply_indices:
                try:
                    _explicit.add(int(_i))
                except (TypeError, ValueError):
                    continue
        _to_apply = []
        for _idx, item in enumerate(results):
            if _idx in _explicit:
                _to_apply.append((_idx, item, int(apply_candidate)))
            elif apply_gaps and item["status"] == "fillable":
                # Report 2026-11-05: `fillable` now means "exactly ONE APPLICABLE
                # candidate" — but that candidate is not necessarily index 0. When the
                # single usable one sat at index 1 and a circle centre sat at 0, the auto
                # path passed 0, hit the applicability guard, and drew nothing while the
                # gap was advertised as "safe to apply". Use the index the status was
                # computed from.
                _to_apply.append((_idx, item,
                                  (item.get("applicable_candidates") or [0])[0]))
        _mismatch_seen = 0
        _breaker_tripped = False
        # Report 2026-11-06 (P0, 3.4): repeated calls kept inserting. The ledger cannot help
        # (it never sees 1570.796), so the sheet is the memory: read the values already
        # present and skip any gap whose TARGET is already there.
        #
        # SCOPE — this makes the happy path idempotent, and NOTHING MORE. It cannot stop a
        # repeat of a WRONG insert: the sheet holds 1570.796, the gap asks for 8.0, and no
        # comparison of those two numbers can tell "already attempted and failed" from
        # "never attempted". Measured 2026-11-06 on the door fixture: call 1 -> 8→9, call 2
        # -> 9→10, `already_present: 0` both times.
        #
        # Closing that needs per-gap memory that survives the call: either a persisted
        # "attempted edge pairs" record, or a same-unit model prediction (3.5) so a wrong
        # candidate can be rejected BEFORE inserting. Neither is done.
        #
        # One read for the whole batch (measured ~12 s on 40 annotations), not per gap.
        _present_values = []
        try:
            _pre = list_drawing_annotations(drawing_path)
            _present_values = [float(a["value_mm"]) for a in (_pre.get("annotations") or [])
                               if a.get("kind") == "dimension" and a.get("value_mm")]
        except Exception:
            _present_values = []
        _already_present = []
        if _to_apply:
            # Report 2026-11-06 (P0): one call inserted SIX wrong dimensions because the
            # loop had no memory of a failure. A wrong insert means the sheet-vs-model
            # premise does not hold for this drawing at all — the remaining gaps come from
            # the same edge pool and will fail the same way. Stop at the first one.
            #
            # This is a circuit breaker, not a heuristic: after a `value_mismatch` the tool
            # knows the候选 selection is unreliable on this sheet, so continuing can only
            # add more damage.
            _kept = []
            for _idx, item, _cand_i in _to_apply:
                _target = float(item.get("value_mm") or 0.0)
                if _target and any(abs(v - _target) <= tolerance_mm for v in _present_values):
                    _already_present.append({"gap_index": _idx, "value_mm": _target,
                                             "reason": "a dimension with this value is already "
                                                       "on the sheet"})
                    continue
                _kept.append((_idx, item, _cand_i))
            _to_apply = _kept
            for _idx, item, _cand_i in _to_apply:
                if _mismatch_seen:
                    _breaker_tripped = True
                    applied.append({"value_mm": item["value_mm"],
                                    "gap_index": _idx,
                                    "ok": False,
                                    "error": "not_attempted_after_mismatch",
                                    "inserted_but_wrong": False,
                                    "message": ("本次调用已经插错一条尺寸（纸面距离不等于模型尺寸），"
                                                "后续 gap 不再尝试——同一批候选来自同一组边，"
                                                "会以同样方式失败。修正候选选择方式后再调用。")})
                    continue
                _cands = item.get("candidates") or []
                if not _cands:
                    continue
                _pick = min(max(_cand_i, 0), len(_cands) - 1)
                cand = _cands[_pick]
                if not cand.get("applicable", True):
                    # Never pass a synthetic negative index to a function that indexes a
                    # real edge array. Report an explicit limitation and tell the caller
                    # which candidate indices WOULD work for this gap — without guessing
                    # a different candidate on their behalf.
                    _candidates_skipped += 1
                    applied.append({"value_mm": item["value_mm"],
                                    "gap_index": _idx,
                                    "candidate_index": _pick,
                                    "candidate_kind": cand.get("candidate_kind"),
                                    "edge_a": cand.get("edge_a"),
                                    "edge_b": cand.get("edge_b"),
                                    "ok": False,
                                    "error": "circle_centre_not_applicable",
                                    "applicable_candidates": item.get(
                                        "applicable_candidates") or [],
                                    "message": ("candidate uses a circle-centre synthetic "
                                                "index; choose via applicable_candidates")})
                    continue
                pt = cand["centres"][0]
                # Report 2026-11-06 (P0): v0.8.156 made a wrong insert HONEST (`ok:false`
                # / `value_mismatch`) but left it on the sheet, and the ledger never
                # learned about the value — so every later call inserted another one.
                # Measured: 25 -> 31 -> 37 annotations, 1570.796 x5 -> x10.
                #
                # "Do not auto-delete: a position hit could take out a legitimate
                # dimension" was true, but position is not the only identity available.
                # `IDimension.FullName` is: snapshot the set before the insert, take the
                # difference after, and the new annotation is named exactly. No fuzzy
                # match, no risk to anything that was already there.
                # One document handle for the whole insert+rollback. `add_distance_dimension`
                # runs with save=False, so the insert lives in memory only; a snapshot that
                # reopens the path reads the file on disk and sees nothing new.
                try:
                    _doc_live = _open_drawing(drawing_path)
                except Exception:
                    _doc_live = None
                # Judge BEFORE inserting. `AddDimension2` between non-parallel edges makes
                # an ANGLE, and its radians become "1570.796 mm" downstream. Refusing here
                # is the only place the mistake can be prevented rather than reported.
                _defines_length, _why_not = _edges_define_a_length(
                    rows, cand.get("edge_a"), cand.get("edge_b"))
                if not _defines_length:
                    applied.append({"value_mm": item["value_mm"],
                                    "gap_index": _idx,
                                    "candidate_index": _pick,
                                    "candidate_kind": cand.get("candidate_kind"),
                                    "edge_a": cand.get("edge_a"),
                                    "edge_b": cand.get("edge_b"),
                                    "ok": False,
                                    "error": "edge_pair_has_no_length",
                                    "reason": _why_not,
                                    "inserted_but_wrong": False,
                                    "message": ("这对边在模型里不构成可测的长度（%s）。"
                                                "在两条不平行的边之间 AddDimension2 会建出**角度**"
                                                "尺寸，其 SystemValue 是弧度，乘 1000 就成了"
                                                "「1570.796 mm」这类假长度 —— 所以不插入，"
                                                "请换候选。" % _why_not)})
                    continue
                _snap_before = _dimension_selection_names(_doc_live) if _doc_live else []
                # `None` means the reader FAILED — it must survive to the decision below,
                # not be consumed here (report 2026-11-08, P2-3). Unpacking it directly
                # raises `TypeError: cannot unpack non-iterable NoneType`, so the tri-state
                # added in 0.8.216 protected nothing: the "safe refusal to delete" turned
                # into an uncaught exception instead.
                _names_before = (None if _snap_before is None
                                 else [n for n, _ in _snap_before])
                added = add_distance_dimension(
                    drawing_path, cand["edge_a"], cand["edge_b"],
                    x_m=pt[0], y_m=pt[1] - 0.015)
                # Report 2026-11-05 (P0): candidates are selected by SHEET distance —
                # |paper span - target| <= tolerance — but AddDimension2 measures MODEL
                # geometry. On AM_DOOR the two disagreed wildly: a gap of 8.0 mm was
                # "filled" with a 1570.796 mm dimension (500π — the edge pair was an arc).
                #
                # The old code wrote `"value_mm": item["value_mm"]` — the PLANNED value —
                # and read only `added.get("ok")`. `added["value_mm"]` was discarded, so the
                # receipt said "filled 8.0" while the sheet carried 1570.796, and neither
                # side raised. Worse, the ledger never saw 8.0 appear, so every later call
                # decided the gap was still missing and inserted another 1570.796: three
                # runs left 15 absurd dimensions on one sheet.
                #
                # A dimension that does not measure what we asked for is not a success.
                # Compare the actual value, undo the insert, and say so.
                _actual = added.get("value_mm")
                _expected = item["value_mm"]
                # ROOT CAUSE, found 2026-11-06 after 35 versions of chasing this:
                # `AddDimension2` between two edges that are NOT parallel creates an ANGULAR
                # dimension, and its `SystemValue` is radians. Multiplied by 1000 that is
                # 1570.796 for a right angle and 523.599 for 30 degrees — both read as
                # millimetres and both looked like absurd model geometry.
                #
                # The edges in this fixture: 14 degrades to a single point (start == end),
                # 44 is a 40 mm segment; there is no distance between them to measure.
                #
                # So the value is not "wrong by a tolerance" — it is the wrong KIND of
                # quantity, and no amount of prediction would make a length out of it.
                _wrong_dim_type = added.get("dimension_type")
                if _wrong_dim_type in (1, 3):
                    applied.append({"value_mm": _expected,
                                    "gap_index": _idx,
                                    "candidate_index": _pick,
                                    "candidate_kind": cand.get("candidate_kind"),
                                    "edge_a": cand.get("edge_a"),
                                    "edge_b": cand.get("edge_b"),
                                    "actual_value_mm": _actual,
                                    "dimension_type": _wrong_dim_type,
                                    "ok": False,
                                    "error": "angular_dimension_created",
                                    "inserted_but_wrong": True,
                                    "rolled_back": False,
                                    "rollback_skipped_reason":
                                        "an angular dimension cannot be corrected by deleting "
                                        "and retrying — the edge pair does not define a length",
                                    "message": ("这两条边之间建出来的是**角度**尺寸，不是长度"
                                                "（type=%s）。%s 是弧度值当 mm 用的结果 —— "
                                                "%0.4f rad = %.2f°。这条边对不构成可测的长度，"
                                                "请换候选。" % (
                                                    _wrong_dim_type, _actual,
                                                    (float(_actual) / 1000.0) if _actual else 0.0,
                                                    (float(_actual) / 1000.0 * 180.0
                                                     / 3.141592653589793) if _actual else 0.0))})
                    _mismatch_seen += 1
                    continue
                _mismatch = False
                if _actual is None:
                    _mismatch = bool(added.get("ok"))
                else:
                    try:
                        _mismatch = abs(float(_actual) - float(_expected)) > tolerance_mm
                    except (TypeError, ValueError):
                        _mismatch = True
                if _mismatch:
                    # Delete exactly what this call created — by identity, not position.
                    #
                    # SAFETY: a snapshot that came back EMPTY means the identity reader
                    # failed, not that the sheet had no dimensions. In that state every
                    # dimension would look "new" and the rollback would delete the user's
                    # whole drawing. Measured 2026-11-06: the reader returns [] on a sheet
                    # that visibly carries 10 dimensions, so this guard is load-bearing,
                    # not defensive decoration.
                    _snap_after = _dimension_selection_names(_doc_live) if _doc_live else []
                    _names_after = (None if _snap_after is None
                                    else [n for n, _ in _snap_after])
                    # `_names_after` is tri-state too. If either read failed the diff cannot
                    # be computed, and an empty list would be read downstream as "nothing new
                    # appeared" — the safest answer is an empty list PLUS a flag saying the
                    # diff was not computable.
                    _diff_computable = (_names_after is not None
                                        and _names_before is not None)
                    _new_names = ([n for n in _names_after if n not in _names_before]
                                  if _diff_computable else [])
                    # `bool(_names_before)` was the bug (report 2026-11-07, P0-1): "no
                    # dimensions before inserting" is a LEGAL state — it is the first
                    # auto-dimension run — but the guard treated it the same as "the reader
                    # failed", so the wrong dimension it had just inserted was never removed.
                    #
                    # `_names_before` is tri-state now: None means the read failed (do not
                    # delete anything), while an empty list means the sheet really was empty
                    # (delete exactly `_new_names`). `_new_names` is already computed, so the
                    # scope is precise and does not depend on the before-snapshot having
                    # content.
                    _rollback_possible = (_names_before is not None) and bool(_new_names)
                    if not _rollback_possible:
                        _rb_reason = (
                            "the dimension-identity read failed (not 'the sheet was empty'), "
                            "so a delete could not be scoped"
                            if _names_before is None else
                            "the insert produced no new annotation to undo")
                    else:
                        _rb_reason = None      # filled in below if the delete fails
                    _deleted = []
                    if _rollback_possible:
                        # Walk the annotations and delete by SELECTION name.
                        #
                        # `SelectByID2` cannot be used: its `Callout` parameter is a
                        # required IDispatch and every null variant (`None`, `0`,
                        # `python3com.Missing`, `python3com.Empty`) raises
                        # 类型不匹配 at arg 8 — measured 2026-11-07.
                        #
                        # But `IAnnotation.Select(False)` + `EditDelete` works, and the
                        # name that matches is `GetNameForSelection`, NOT
                        # `IDimension.FullName`. See `_dimension_selection_names`.
                        for _nm, _ann in _snap_after:
                            if _nm not in _new_names:
                                continue
                            try:
                                _doc_live.ClearSelection2(True)
                                if not _ann.Select(False):
                                    continue
                                _doc_live.EditDelete()
                                _deleted.append(_nm)
                            except Exception:
                                continue
                        if _deleted:
                            try:
                                _doc_live.EditRebuild3()
                                perf_hooks.bump("rebuild_calls")
                            except Exception:
                                pass
                            _rb_reason = None
                        else:
                            _rb_reason = ("scoped to %d new selection name(s) but Select(False)"
                                          " + EditDelete removed none"
                                          % len(_new_names))
                    applied.append({"value_mm": _expected,
                                    "gap_index": _idx,
                                    "candidate_index": _pick,
                                    "candidate_kind": cand.get("candidate_kind"),
                                    "edge_a": cand.get("edge_a"),
                                    "edge_b": cand.get("edge_b"),
                                    "actual_value_mm": _actual,
                                    "value_delta_mm": (round(float(_actual) - float(_expected), 3)
                                                       if _actual is not None else None),
                                    "ok": False,
                                    "error": "value_mismatch",
                                    "inserted_but_wrong": True,
                                    # `len(None)` raises, and this line runs on a path
                                    # that is already reporting a failure — a second
                                    # exception here would replace the real diagnosis.
                                    "snapshot_before": (None if _names_before is None
                                                        else len(_names_before)),
                                    "snapshot_after": (None if _names_after is None
                                                       else len(_names_after)),
                                    "diff_computable": _diff_computable,
                                    "new_names": _new_names[:6],
                                    "rolled_back": (_rollback_possible
                                                    and bool(_new_names)
                                                    and len(_deleted) == len(_new_names)),
                                    "rolled_back_names": _deleted,
                                    # Two different failures, two different messages.
                                    # `null` here used to mean both "the scoped delete
                                    # worked" and "it was attempted and failed" — the receipt
                                    # said `rolled_back: false` with no explanation at all.
                                    "rollback_skipped_reason": _rb_reason,
                                    "message": ("这一对边在图纸上相距接近 %s，但 SolidWorks 按模型几何"
                                                "量出的是 %s —— 纸面距离不等于模型尺寸。"
                                                "%s"
                                                % (_expected, _actual,
                                                   ("本次插入的标注已按 FullName 身份删除"
                                                    "（%d 条），图纸恢复原状。" % len(_deleted))
                                                   if _deleted else
                                                   "删除未能执行，这条错误尺寸仍在图上。"))})
                    _mismatch_seen += 1
                    continue
                applied.append({"value_mm": item["value_mm"],
                                "actual_value_mm": _actual,
                                "gap_index": _idx,
                                    "candidate_index": _pick,
                                    "candidate_kind": cand.get("candidate_kind"),
                                    "edge_a": cand.get("edge_a"),
                                "edge_b": cand.get("edge_b"),
                                "ok": bool(added.get("ok")),
                                "error": added.get("error"),
                                "message": (added.get("message") or "")[:160]})
            try:
                _save_drawing(_open_drawing(drawing_path))
            except Exception:
                pass
        # Every ambiguous gap that this run did NOT fill. Excluding the filled ones is the
        # whole point: a receipt saying "applied 13.0" and "13.0 not applied" tells the
        # caller to dimension the same feature twice.
        _filled = set(a.get("gap_index") for a in applied if a.get("ok"))
        _ambiguous_not_applied = [r["value_mm"] for _i, r in enumerate(results)
                                  if r["status"] == "ambiguous" and _i not in _filled]

        return drawing_ok(
            gaps_found=len(results),
            # Which space the candidates were matched in. `model_fallback` means the sheet
            # projection was unavailable (ambiguous axes on an equal-extent part) and the
            # distances come from model coordinates — the values are still comparable, but
            # the placement hint (`centres`) is in model space, not on the sheet.
            coordinate_space=_coordinate_space,
            fillable=len(fillable),
            ambiguous=sum(1 for r in results if r["status"] == "ambiguous"),
            no_geometry=sum(1 for r in results if r["status"] == "no_geometry"),
            applied=applied,
            apply_indices_used=sorted(_explicit),
            ambiguous_not_applied=_ambiguous_not_applied,
            # Report 2026-11-06 (P0): `applied_count` counted only successes, so a call
            # that inserted SIX wrong dimensions reported `applied_count: 0` next to
            # `applied[]` with six entries. A caller reading the counter concluded
            # "nothing happened" while the sheet had grown by six. The counter must not
            # be able to describe a side effect as a no-op.
            applied_count=len([a for a in applied if a["ok"]]),
            applied_failed=len([a for a in applied if not a["ok"]]),
            inserted_but_wrong=len([a for a in applied
                                    if a.get("inserted_but_wrong")]),
            # Report 2026-11-08 (P2-1): `sheet_modified` was computed as "did I insert a
            # wrong dimension at any point", so it stayed `true` even after every one of
            # them was rolled back and `annotation_count` returned to its baseline. Read
            # next to `rolled_back: 1`, that says "the sheet was damaged" when the sheet is
            # exactly as it was.
            #
            # Two separate facts, two separate fields:
            #   insert_attempted_wrong  a wrong dimension WAS created (historical)
            #   sheet_modified          the sheet is different NOW (residual work)
            # `sheet_modified` keeps its name because a caller may already branch on it —
            # but it now means what it says, and the old meaning is still available.
            insert_attempted_wrong=any(a.get("inserted_but_wrong") for a in applied),
            sheet_modified=any(a.get("inserted_but_wrong") and not a.get("rolled_back")
                               for a in applied),
            residual_inserted_but_wrong=len([a for a in applied
                                             if a.get("inserted_but_wrong")
                                             and not a.get("rolled_back")]),
            rolled_back=len([a for a in applied if a.get("rolled_back")]),
            breaker_tripped=_breaker_tripped,
            # Gaps whose value was already on the sheet: skipped, not attempted, not failed.
            already_present=_already_present[:20],
            already_present_count=len(_already_present),
            candidates_total=len(_all_cands),
            candidates_applicable=sum(1 for c in _all_cands
                                      if c.get("applicable", True)),
            candidates_circle_centre=sum(1 for c in _all_cands
                                         if c.get("candidate_kind") == "circle_centre"),
            candidates_skipped=_candidates_skipped,
            gaps=results,
            coverage=rec.get("coverage"),
            note=("This is a PLAN unless apply_gaps or apply_indices was given. Each gap "
                  "carries a status: `fillable` (exactly one APPLICABLE candidate), "
                  "`ambiguous` (several), `low_confidence` (more than %d edge pairs match "
                  "this value, so a distance-only test has no discriminating power — never "
                  "auto-applied; name it with apply_indices after checking), "
                  "`no_applicable_candidate` (matches exist but none "
                  "can be passed to add_distance_dimension), `no_length_pair` (the matching "
                  % LOW_CONFIDENCE_MATCH_LIMIT +
                  "edge pair is degenerate or not parallel, so AddDimension2 would create an "
                  "ANGLE rather than a length), `no_geometry` (that value needs "
                  "a section or detail view). apply_gaps=True fills only the `fillable` "
                  "ones; apply_indices fills the gaps you name, ambiguous or not, and does "
                  "not require apply_gaps. Candidates tagged `candidate_kind: circle_centre` "
                  "carry synthetic negative indices and are `applicable: false` — choose "
                  "from `applicable_candidates`. Every applied entry carries "
                  "`actual_value_mm`; when it differs from the planned value the entry is "
                  "`ok: false` with `error: value_mismatch`, because sheet distance is not "
                  "model geometry. Each candidate also carries `model_check`: "
                  "`approximate_3d_distance` (a 3D hint, not the same quantity as the sheet "
                  "span) or `unavailable` (no model point, so no model evidence at all). "
                  "Neither filters anything — the drawn value is verified after insertion. "
                  "A gap whose `candidates_truncated` is true may still be solvable: raise "
                  "`max_candidates_per_gap` and re-plan."),
        )
    except Exception as exc:
        return error("dimension_gaps_failed", str(exc))


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
                perf_hooks.bump("redraw_calls")
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
            perf_hooks.bump("redraw_calls")
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

        # Report 2026-11-04 P1-4: the actual landing point existed only inside
        # `readback.position_m`, so a caller looking for a top-level field concluded the tool
        # reported nothing and could only judge placement by rendering a PDF. Hoist it, and
        # say plainly when the requested coordinate was NOT honoured — the datum is attached
        # to an edge, so SolidWorks may place the tag itself and ignore SetPosition.
        position_requested = None
        if x_m is not None and y_m is not None:
            position_requested = [round(float(x_m), 4), round(float(y_m), 4)]
        position_actual = readback.get("position_m")
        position_delta = None
        position_honoured = None
        if position_requested and position_actual:
            dx = abs(position_actual[0] - position_requested[0])
            dy = abs(position_actual[1] - position_requested[1])
            position_delta = [round(dx, 4), round(dy, 4)]
            # 1 mm is generous on purpose: the tag's anchor is not its text centre, so a
            # small offset is expected and should not read as a failure.
            position_honoured = bool(dx <= 0.001 and dy <= 0.001)
        warning = None
        if position_requested and position_honoured is False:
            warning = ("请求的落点 %r 未被采纳，实际落在 %r。基准符号由所选边推导锚点，"
                       "坐标只是建议值；换一条边重试，或按实际落点调整其它标注。"
                       % (position_requested, position_actual))
        return drawing_ok(drawing=doc.GetTitle, attached_to="%s[%d]" % (target.Name, edge_index),
                          position_requested_m=position_requested,
                          position_actual_m=position_actual,
                          position_delta_m=position_delta,
                          position_honoured=position_honoured,
                          warning=warning,
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
        leader_path_note = None
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
            # Report 2026-11-05 (P1): with `leader_style="bent"` the elbow segment ran
            # horizontally THROUGH the tolerance value cell ("0.008") before turning up to
            # the arrow. The report asked for a warning when the polyline intersects the
            # frame bbox — that check cannot be built here: `SetLeader3` lets SolidWorks
            # choose the elbow, and nothing on this binding exposes the resulting points
            # (`IGtol.GetLeaderAtIndex2` does not exist; DXF writes the leader as plain
            # LINEs among 362 others with no link back to the annotation).
            #
            # So the honest thing is to say the path is not ours to verify, at the moment
            # the style is chosen, and name the one lever that changes it.
            if str(leader_style).lower() == "bent":
                leader_path_note = (
                    "折弯引线的拐点由 SolidWorks 自己决定，本绑定读不到折线的实际坐标"
                    "（`IGtol.GetLeaderAtIndex2` 不存在；DXF 里引线被拆成无归属的 LINE，"
                    "362 条中无法定位哪条属于本标注）。实测出现过水平段横穿公差值格的情况；"
                    "若发生，把 leader_side 换到另一侧（left/right）通常能改变拐点位置——"
                    "工具无法预判哪一侧不会穿格。")

        try:
            doc.EditRebuild3()
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
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
                        # Report 2026-11-05: on a circular edge this reported the CENTRE,
                        # 15.000 mm (one radius) from where the DXF shows the arrow tip.
                        # A leader touches the curve, not its centre — `sheet_center` is
                        # only correct for a point edge. Prefer the point ON the circle
                        # (added in 0.8.98 for exactly this), then the edge start, and use
                        # the centre only as a last resort.
                        _pt = None
                        _pt_source = None
                        for _key, _label in (("sheet_point_on_circle", "circle_point"),
                                             ("sheet_start", "edge_start"),
                                             ("sheet_center", "circle_centre")):
                            _candidate = _row.get(_key)
                            if _candidate:
                                _pt = _candidate
                                _pt_source = _label
                                break
                        if _pt:
                            leader_endpoint = [round(float(_pt[0]), 4),
                                               round(float(_pt[1]), 4)]
                            leader_endpoint_source = "attached_edge_%s" % _pt_source
                            if _pt_source == "circle_centre":
                                # Only reachable when the row carries no point on the
                                # curve; say so rather than passing a centre off as a tip.
                                leader_endpoint_source += "_no_circle_point_available"
                            break
                except Exception:
                    continue
            if leader_endpoint is None:
                leader_endpoint_source = ("unavailable: IGtol.GetLeaderAtIndex2 does not "
                                          "exist on this binding and the edge was not in "
                                          "list_drawing_edges' cache")

        return drawing_ok(
            created=True, saved=bool(save), attached_to=attached,
            # Report 2026-11-06 (P3): this is NOT where the leader's arrow lands. It is a
            # point computed on the attached edge's circle (centre + radius), and a report
            # measured 27.5 mm between it and the real DXF LEADER first point. The field name
            # promised a measurement and delivered a prediction.
            #
            # `leader_target_m` is the honest name. `leader_endpoint_m` is kept so existing
            # callers do not break, but it carries the same value and `leader_endpoint_m_is`
            # says what it actually is.
            leader_target_m=leader_endpoint,
            leader_target_source=leader_endpoint_source,
            leader_endpoint_m=leader_endpoint,
            leader_endpoint_m_is="computed_target_not_measured_arrow_position",
            leader_endpoint_source=leader_endpoint_source,
            leader_style_requested=str(leader_style),
            leader_path_note=leader_path_note,
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
                  "geometric_tolerance）。符号走 2022+ 原生路径：symbol='position' 等英文名"
                  "经 _GD_T_SYMBOL_NAMES 映射到 IGTOL-*，回执 symbol_is_text=false 且"
                  "PDF 渲染为真实几何符号（2026-11-04 复测，旧版本文档说的「符号是文字」"
                  "已过期）。<MOD-*> 格式码只在旧路径上会被当字面文本，故不再使用。"
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
                    # 0.8.303 (report 2026-11-12 §4): an ANGULAR dimension's
                    # `SystemValue` is radians, and `GetText` can hand back the
                    # radian-derived number — a 45° chamfer arrived here as `785.4` and
                    # was laid out (and reported) as a 785 mm length. The angle is
                    # authoritative, so label every angular row from it.
                    if _is_angular_dimension_type(
                            _annotation_dimension_type(annotation, win)):
                        try:
                            text = "%g°" % _radians_to_deg(
                                win.Dispatch(specific.GetDimension2(0)).SystemValue)
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
                perf_hooks.bump("rebuild_calls")
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


# ── Sketch geometry export (report 2026-11-12 §3, the last P0 gap) ───────────────────────
# "Rebuild this part in any CAD system" needs the sketch outline point by point, not just
# the driving dimensions. Probed on this binding before writing anything:
#
#   sketch.GetSketchSegments        OK   (list of ISketchSegment, late-bound)
#   sketch.GetSketchPoints          OK   (ISketchPoint with X/Y/Z, sketch coordinates)
#   sketch.GetSketchContours        OK   (contour.IsClosed)
#   sketch.ModelToSketchTransform   FAIL DISP_E_MEMBERNOTFOUND  -> no plane frame
#   sketch.GetReferenceEntity       FAIL needs an argument      -> no plane frame
#   seg.GetType                     OK   0 = line, 1 = arc
#   seg.GetLength                   OK   metres
#   seg.GetCurve                    OK
#   curve.LineParams                OK   [ox, oy, oz, dx, dy, dz]  (origin + unit dir)
#   curve.CircleParams              OK   [cx, cy, cz, ax, ay, az, radius]
#   ISketchLine.GetStartPoint2      FAIL the derived interface is invisible to late binding
#
# So a LINE gives exact endpoints (origin, origin + direction x length) and an ARC gives
# exact centre/axis/radius but NOT its endpoints. The endpoints are recovered from the
# sketch's own point set — and only when exactly one pair matches the arc's chord.

SKETCH_SEGMENT_LINE = 0
SKETCH_SEGMENT_ARC = 1


def _seg_point(value):
    """Three floats from an `ISketchPoint`-like object, or None."""
    try:
        return [round(float(value.X), 6), round(float(value.Y), 6), round(float(value.Z), 6)]
    except Exception:
        return None


def _dist3(a, b):
    try:
        return sum((float(x) - float(y)) ** 2 for x, y in zip(a[:3], b[:3])) ** 0.5
    except Exception:
        return None


def _arc_endpoints(center, radius, length, points, tol=1e-4):
    """(start, end, source) for an arc, from the sketch's point set. PURE.

    An arc reports centre/axis/radius and its LENGTH, not its endpoints (the derived
    `ISketchArc` interface is invisible to late binding). The sketch's point set contains
    them: the two points that lie ON this circle, at a chord distance of
    `2*r*sin(len/(2r))`.

    Returns `(None, None, reason)` instead of guessing when the pair is not unique — a
    wrong endpoint silently corrupts every downstream rebuild.
    """
    import math

    try:
        radius = float(radius)
        length = float(length)
    except (TypeError, ValueError):
        return None, None, "bad_arc_parameters"
    if radius <= 0 or length <= 0:
        return None, None, "bad_arc_parameters"
    chord = 2.0 * radius * math.sin(min(length / radius, math.pi) / 2.0)
    on_circle = [p for p in (points or [])
                 if p and _dist3(p, center) is not None
                 and abs(_dist3(p, center) - radius) <= max(tol, radius * 1e-3)]
    if len(on_circle) < 2:
        return None, None, "fewer than two sketch points lie on this circle"
    best = None
    for i in range(len(on_circle)):
        for j in range(i + 1, len(on_circle)):
            d = _dist3(on_circle[i], on_circle[j])
            if d is None:
                continue
            if best is None or abs(d - chord) < best[0]:
                best = (abs(d - chord), on_circle[i], on_circle[j], d)
    if best is None:
        return None, None, "no usable point pair"
    if best[0] > max(tol, chord * 0.02):
        return None, None, ("no point pair matches the arc chord (nearest is %.4f mm off)"
                            % (best[0] * 1000.0))
    return best[1], best[2], "matched_from_sketch_points"


def _sketch_segments(sketch):
    """Every segment of one sketch as plain dicts. Never invents a coordinate."""
    import win32com.client as win

    points = []
    for raw in (sketch.GetSketchPoints or ()):
        pt = _seg_point(win.Dispatch(raw))
        if pt:
            points.append(pt)

    segments = []
    for index, raw in enumerate(sketch.GetSketchSegments or ()):
        try:
            seg = win.Dispatch(raw)
        except Exception:
            continue
        try:
            kind_code = int(seg.GetType)
        except Exception:
            kind_code = None
        try:
            length_m = round(float(seg.GetLength), 6)
        except Exception:
            length_m = None
        entry = {"index": index, "type": kind_code, "length_m": length_m}
        curve = None
        try:
            curve = win.Dispatch(seg.GetCurve)
        except Exception:
            curve = None
        if kind_code == SKETCH_SEGMENT_LINE:
            entry["kind"] = "line"
            origin = direction = None
            if curve is not None:
                try:
                    vals = [float(v) for v in curve.LineParams]
                    if len(vals) >= 6:
                        origin = [round(v, 6) for v in vals[:3]]
                        direction = [round(v, 6) for v in vals[3:6]]
                except Exception:
                    pass
            entry["start_m"] = origin
            entry["direction"] = direction
            if origin and direction and length_m:
                entry["end_m"] = [round(o + d * length_m, 6)
                                  for o, d in zip(origin, direction)]
                entry["endpoints_source"] = "line_params"
            else:
                entry["end_m"] = None
                entry["endpoints_source"] = None
        elif kind_code == SKETCH_SEGMENT_ARC:
            entry["kind"] = "arc"
            center = axis = radius = None
            if curve is not None:
                try:
                    vals = [float(v) for v in curve.CircleParams]
                    if len(vals) >= 7:
                        center = [round(v, 6) for v in vals[:3]]
                        axis = [round(v, 6) for v in vals[3:6]]
                        radius = round(vals[6], 6)
                except Exception:
                    pass
            entry["center_m"] = center
            entry["axis"] = axis
            entry["radius_m"] = radius
            if radius and length_m:
                import math as _math
                entry["sweep_deg"] = round(_math.degrees(length_m / radius), 3)
            else:
                entry["sweep_deg"] = None
            start, end, source = _arc_endpoints(center, radius, length_m, points)
            entry["start_m"] = start
            entry["end_m"] = end
            entry["endpoints_source"] = source
        else:
            entry["kind"] = "other"
            entry["start_m"] = None
            entry["end_m"] = None
            entry["endpoints_source"] = "unsupported_segment_type"
        segments.append(entry)

    closed = None
    try:
        contours = list(sketch.GetSketchContours or ())
        if contours:
            closed = bool(win.Dispatch(contours[0]).IsClosed)
    except Exception:
        closed = None

    return {"segments": segments, "points": points, "closed": closed}


def export_sketch_geometry(model_path, include_points=True, max_sketches=200):
    """Read every sketch's segment geometry — the input a full rebuild needs.

    Report 2026-11-12 §3 (P0): the toolkit could describe a part's DRIVING DIMENSIONS but
    not its sketch OUTLINE, so "rebuild this in any CAD system" still needed an external
    MCP plus a hand-written document. This fills that gap for the geometry that is actually
    readable on this binding.

    What IS returned, per sketch:
      * `segments[]` — `kind` (line / arc / other), length, and for a line the exact
        `start_m` + `direction` + computed `end_m`; for an arc the exact `center_m` /
        `axis` / `radius_m` / `sweep_deg`, plus endpoints recovered from the sketch's own
        point set (`endpoints_source`).
      * `points[]` — every sketch point, in SKETCH coordinates (metres).
      * `closed` — from `GetSketchContours()[0].IsClosed`.

    What is NOT, and is reported as null rather than guessed:
      * `plane_frame` — `ModelToSketchTransform` answers DISP_E_MEMBERNOTFOUND and
        `GetReferenceEntity` needs an argument, so the sketch→model transform cannot be
        read here. Coordinates are SKETCH-local: Z is 0 for a planar sketch, and the
        plane's origin/directions must be confirmed in the UI.
      * an arc whose endpoints do not resolve to a unique point pair — `endpoints_source`
        carries the reason instead of a coordinate.

    Read-only: the document is opened, read and closed without saving.
    """
    try:
        import win32com.client as win

        path = os.path.abspath(model_path)
        if not os.path.exists(path):
            return error("bad_input", "model not found: %s" % path)
        doc = open_document(path, SW_DOC_PART)
        if doc is None:
            return error("model_unreadable", "could not open %s" % path)
        sketches = []
        skipped_unreadable = 0
        try:
            feature = doc.FirstFeature
            guard = 0
            while feature is not None and guard < 2000 and len(sketches) < int(max_sketches):
                guard += 1
                try:
                    ftype = str(feature.GetTypeName2)
                    fname = str(feature.Name)
                except Exception:
                    feature = feature.GetNextFeature
                    continue
                if ftype in ("Sketch", "ProfileFeature", "3DSketch"):
                    try:
                        sketch = win.Dispatch(feature.GetSpecificFeature2)
                        data = _sketch_segments(sketch)
                    except Exception:
                        skipped_unreadable += 1
                        data = None
                    if data is not None:
                        entry = {"name": fname, "feature_type": ftype,
                                 "segment_count": len(data["segments"]),
                                 "point_count": len(data["points"]),
                                 "closed": data["closed"],
                                 "segments": data["segments"],
                                 # `null` for a documented reason, never a guess.
                                 "plane_frame": None,
                                 "plane_frame_reason": (
                                     "ModelToSketchTransform is not available on this "
                                     "binding; coordinates are SKETCH-local (Z=0 for a "
                                     "planar sketch)")}
                        if include_points:
                            entry["points"] = data["points"]
                        sketches.append(entry)
                feature = feature.GetNextFeature
        finally:
            try:
                close_document(path)
            except Exception:
                pass

        return ok(
            model=path,
            units="m",
            sketch_count=len(sketches),
            sketches=sketches,
            skipped_unreadable=skipped_unreadable,
            geometry_available=("segments" if sketches else "none"),
            plane_frame_available=False,
            note=("坐标是**草图局部坐标**（米），Z 对平面草图为 0；基准面 origin/xdir/ydir "
                  "在本 binding 读不到（`ModelToSketchTransform` 报 MEMBERNOTFOUND），"
                  "`plane_frame` 因此是 null 而不是猜出来的值。直线端点由 "
                  "`LineParams` + `GetLength` 精确算出；圆弧给圆心/轴/半径/包角，"
                  "端点由草图点集反解，反解不唯一时 `endpoints_source` 写明原因。"
                  "本工具只读：打开、读取、关闭，不保存。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("sketch_geometry_failed", str(exc))
