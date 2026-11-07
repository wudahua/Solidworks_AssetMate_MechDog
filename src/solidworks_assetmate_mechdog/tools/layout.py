"""Sheet-layout operations: place views, edit dimensions, add notes/balloons/BOM.

Every one of these was previously "not available" and had to be written as raw COM by the
caller, which cost hours: `IView.Position` silently mis-writes unless dispatched as
PROPERTYPUT with a VARIANT array, `InsertNote` needs GetAnnotation called as a method, and
deleting a dimension needs SelectByID2 by position plus EditDelete.

Kept in a separate module because these are layout concerns, not geometry.
"""
from __future__ import annotations

import os

from .. import perf_hooks

from ..sw_session import byref_dispatch_null, error, get_sw
from .bom_native import default_template, populate_top_level_bom
from .drawing import _open_drawing, _iter_views, drawing_ok, _save_drawing, GTOL_SYMBOLS  # noqa: F401


def _prop_put(obj, name, value):
    """Set a property through IDispatch::Invoke with DISPATCH_PROPERTYPUT.

    Plain `obj.X = value` goes through pywin32's own marshalling, which silently writes
    the wrong thing for array-valued properties: setting Position to (0.12, 0.20) produced
    (0, 0.12). Explicit PROPERTYPUT with a VARIANT array is the only reliable form.
    """
    import pythoncom

    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUT, True, value)


# call_method lives in sw_session; this module used to carry its own copy, which risked
# drifting from the real one. Keep the single import and drop the duplicate.
from ..sw_session import call_method as _call_method


def _dodge_view_label(doc, view, gap_m=0.006, max_steps=12):
    """Move this view's dimensions off its own scale / label note (P1-9).

    Changing a section view's scale makes SolidWorks regenerate the "A-A" / "2.5 : 1" label
    at a position of its own choosing, and that position ignores dimensions already sitting
    there — a report had 2.80 and the A-A tag printed on top of each other the moment a view
    went 4:1 -> 2.5:1. `layout_dimensions` only treats the label as an obstacle when it
    happens to run afterwards, which is not when the damage occurs.

    The label is any non-empty note on the view: `IView.GetLabel` is not reachable under
    late binding, and the ratio is not always a separate note. Overlapping dimensions
    are pushed up one line height at a time until they clear every label box.
    """
    import win32com.client as win

    try:
        raws = list(view.GetAnnotations or ())
    except Exception:
        return None

    # A view's own notes ARE its label ("A", "A-A", "2.5 : 1") — the ratio is not always a
    # separate note, and `IView.GetLabel` is unreachable under late binding, so take every
    # non-empty note on this view as a label candidate.
    labels = []
    for raw in raws:
        try:
            annotation = win.Dispatch(raw)
            if int(annotation.GetType) != 6:
                continue
            note = win.Dispatch(annotation.GetSpecificAnnotation)
            text = str(note.GetText or "")
            position = [float(v) for v in annotation.GetPosition]
        except Exception:
            continue
        if not text.strip():
            continue
        labels.append({"text": text[:20], "position": position})
    if not labels:
        return None

    char_h = 0.0035
    boxes = []
    for label in labels:
        # `<VLLABEL>/<VLSCALEV>` is what a view label's GetText really returns (confirmed
        # 2026-11-01 — v0.8.24 concluded it was unreproducible because
        # list_drawing_annotations happened not to surface it). It renders as "A—A" over
        # "2.5 : 1", so 19 code characters are about 8 visible ones; using the raw length
        # made the keep-out 40 mm wide and shoved dimensions away for no reason.
        shown = label["text"]
        if "<VL" in shown.upper():
            shown = (shown.upper().replace("<VLLABEL>", "A—A")
                     .replace("<VLSCALEV>", "2.5 : 1"))
        half_w = max(char_h, len(shown) * char_h * 0.6)
        boxes.append([label["position"][0] - half_w, label["position"][1] - char_h,
                      label["position"][0] + half_w, label["position"][1] + char_h])

    def _hits(candidate):
        for box in boxes:
            if (box[0] - gap_m < candidate[2] and candidate[0] < box[2] + gap_m
                    and box[1] - gap_m < candidate[3] and candidate[1] < box[3] + gap_m):
                return True
        return False

    moved = 0
    for raw in raws:
        try:
            annotation = win.Dispatch(raw)
            if int(annotation.GetType) != 4:
                continue
            position = [float(v) for v in annotation.GetPosition]
        except Exception:
            continue
        half_text = 0.006
        candidate = [position[0] - half_text, position[1] - char_h * 0.5,
                     position[0] + half_text, position[1] + char_h * 0.5]
        if not _hits(candidate):
            continue
        for _step in range(max_steps):
            position[1] += char_h * 1.8
            candidate = [position[0] - half_text, position[1] - char_h * 0.5,
                         position[0] + half_text, position[1] + char_h * 0.5]
            if not _hits(candidate):
                break
        try:
            annotation.SetPosition(float(position[0]), float(position[1]), 0)
            moved += 1
        except Exception:
            continue
    return {"labels": [{"text": entry["text"],
                        "position_m": [round(v, 4) for v in entry["position"]]}
                       for entry in labels],
            "dimensions_moved": moved}


def set_view(drawing_path, view_name, x_m=None, y_m=None, scale=None, save=True,
             break_alignment=False):
    """Move and/or rescale one view. Coordinates are sheet metres.

    Position is written with DISPATCH_PROPERTYPUT + VARIANT(VT_ARRAY|VT_R8); a plain
    assignment silently lands the view somewhere else.

    Section and detail views keep projection alignment with their parent, so the applied
    position may differ from the requested one. The result reports `requested`,
    `position_after_m` and `aligned_to_parent` separately — previously it just said ok:true
    while quietly moving y, and a reporter needed two rounds to notice.
    """
    try:
        import pythoncom
        from win32com.client import VARIANT

        doc = _open_drawing(drawing_path)
        target = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view.Name == view_name:
                target = view
                break
        if target is None:
            names = [v.Name for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
            return error("view_not_found", "no view named %r" % view_name,
                         "Available: %s" % ", ".join(names))

        # P1-7: a section view is locked to its parent along one axis, so the requested
        # coordinate comes back "corrected" and multi-view layout gets stuck in a narrow
        # strip beside the parent. IView.RemoveAlignment is the UI's 解除对齐; note it only
        # clears THIS view's alignment, not what its children inherit.
        alignment_broken = False
        if break_alignment:
            try:
                _call_method(target, "RemoveAlignment")
                alignment_broken = True
            except Exception:
                alignment_broken = False

        requested = None
        if x_m is not None and y_m is not None:
            requested = [round(float(x_m), 4), round(float(y_m), 4)]
            _prop_put(target, "Position",
                      VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                              [float(x_m), float(y_m)]))
        if scale is not None:
            _prop_put(target, "ScaleDecimal", float(scale))
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2
        except Exception:
            pass

        # Read back AFTER the rebuild. Section/detail views keep projection alignment
        # with their parent, so SolidWorks may have moved the view away from the
        # requested position. P2-12: report requested vs applied separately instead of
        # only saying ok:true while quietly changing y.
        applied = {"view": view_name, "position_m": None, "scale": None}
        position_after = None
        try:
            position_after = [round(v, 4) for v in target.Position]
        except Exception:
            pass
        if x_m is not None and y_m is not None:
            applied["position_m"] = position_after

        # P1-6: `IView.Position` is not proof the view moved. A report showed
        # set_view(detail, x, y, break_alignment=True) returning
        # {"position_after_m": [0.382, 0.132], "ok": true} while the rendered sheet still
        # had the detail sitting on top of the section view — the readback was just echoing
        # the value that had been written. The ink outline comes from the actual geometry,
        # so its centre is the only trustworthy check.
        outline_center = None
        try:
            box = [float(v) for v in target.GetOutline]
            outline_center = [round((box[0] + box[2]) / 2, 4),
                              round((box[1] + box[3]) / 2, 4)]
        except Exception:
            outline_center = None
        # Compare against the REQUESTED point, not against `IView.Position`. On a detail
        # view that refuses to move, Position came back as None entirely — comparing
        # Position-to-outline would have found nothing to disagree with. The caller asked
        # for a point; the outline says where the ink actually is.
        mismatch = None
        if (requested is not None and outline_center is not None
                and (abs(outline_center[0] - requested[0]) > 0.001
                     or abs(outline_center[1] - requested[1]) > 0.001)):
            mismatch = {
                "position_requested_m": [round(float(requested[0]), 4),
                                         round(float(requested[1]), 4)],
                "position_reported_m": position_after,
                "outline_center_m": outline_center,
                "delta_mm": [round((outline_center[0] - requested[0]) * 1000, 2),
                             round((outline_center[1] - requested[1]) * 1000, 2)],
            }
        if scale is not None:
            try:
                applied["scale"] = float(target.ScaleDecimal)
            except Exception:
                pass
        # P1-9: a scale change regenerates the view label at a SW-chosen spot; move this
        # view's dimensions off it before the caller sees the result.
        label_dodge = None
        if scale is not None:
            try:
                label_dodge = _dodge_view_label(doc, target)
                if label_dodge and label_dodge.get("dimensions_moved"):
                    _call_method(doc, "EditRebuild3")
                    try:
                        doc.GraphicsRedraw2
                    except Exception:
                        pass
            except Exception:
                label_dodge = None
        aligned = bool(
            requested is not None and position_after is not None
            and (abs(position_after[0] - requested[0]) > 1e-4
                 or abs(position_after[1] - requested[1]) > 1e-4)
        )
        if mismatch is not None and x_m is not None and y_m is not None:
            # Report the disagreement as an ERROR, not a warning: the caller asked for a
            # position and did not get it. Silently returning ok:true is what cost the
            # reporter a round.
            if save:
                _save_drawing(doc)
            return error(
                "moved_but_position_mismatch",
                "IView.Position reports %r but the geometry sits at %r"
                % (position_after, outline_center),
                "Detail views are tied to the parent's detail circle and often refuse to "
                "move; the printed sheet follows the OUTLINE, so the view is NOT where you "
                "asked. Rebuild the detail view instead of moving it.",
                **mismatch)
        if save:
            _save_drawing(doc)
        # P0-3: moving a PARENT view does not carry its section cut lines with it — the
        # cut line lives in the parent's sketch, and SolidWorks leaves it at the old sheet
        # coordinates, so the section loses its cutting basis while still looking valid.
        # The report paid for this twice (rebuild the section -> lose that view's
        # dimensions). We cannot move the sketch from here, so say it before saving.
        children = []
        # NOTE: this used to reuse `aligned`, which means "the applied position does NOT
        # match the request" — so a view that moved exactly where asked skipped the check
        # and reported no derived views. Ask instead: did the caller request a position?
        if requested is not None and position_after is not None:
            import win32com.client as _win

            for other in _iter_views(doc):
                if getattr(other, "Type", None) == 1:
                    continue
                try:
                    if str(other.Name) == str(view_name):
                        continue
                    # `GetBaseView` is a PROPERTY that returns a raw PyIDispatch: reading
                    # `.Name` straight off it fails and the exception was being swallowed,
                    # so this check silently always reported "no derived views" (measured
                    # 2026-10-31: a section view does answer 工程图视图1 once dispatched).
                    base = other.GetBaseView
                    if base is None:
                        continue
                    if str(_win.Dispatch(base).Name) == str(view_name):
                        children.append(str(other.Name))
                except Exception:
                    continue
        return drawing_ok(
            drawing=doc.GetTitle,
            requested=requested,
            position_after_m=position_after,
            aligned_to_parent=aligned,
            alignment_reason=("section/detail views keep projection alignment with the "
                              "parent view" if aligned else None),
            # P1-9: what the label did, and how many dimensions had to move off it.
            view_label=label_dodge,
            alignment_broken=alignment_broken,
            # P0-3: empty list means no derived views (detail/section) reference this one.
            derived_views=children,
            cut_lines_detached_warning=(
                "本视图有派生视图 %r：切割线/边界草图不会随父视图平移，"
                "请用 export_drawing + render_drawing 复核剖面位置，"
                "或重建剖面（重建会丢该视图上已插入的尺寸）。" % children
                if children else None),
            **applied,
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("set_view_failed", str(exc))


def place_views(drawing_path, placements, save=True):
    """Place several views at once: [{"view": name, "x": , "y": , "scale": }] (metres).

    Accepts `set_view`'s key names too (`view_name` / `x_m` / `y_m`), because passing one
    tool's vocabulary to the other used to drop every field and report "no view named
    None" (N5). An entry with neither key is reported as `missing_view_key` instead of
    being forwarded as None.

    A bare `[x, y]` pair is ALSO accepted (P1, 2026-11-03). The MCP schema advertises
    `[x, y] positions, one per view`, and a report followed it exactly — only to get
    `placed: 0, results[].error: "placement_not_object", ok: true`. The schema promised a
    shape the implementation rejected, and the top-level `ok` hid it. Pairs are matched to
    the views in sheet order; the view name is filled in from `_iter_views`.
    """
    view_names = None
    results = []
    for index, item in enumerate(placements or []):
        if isinstance(item, (list, tuple)):
            if view_names is None:
                try:
                    view_names = [str(v.Name) for v in _iter_views(_open_drawing(drawing_path))
                                  if getattr(v, "Type", None) != 1]
                except Exception:
                    view_names = []
            if index < len(view_names) and len(item) >= 2:
                # Positional pair — turn it into the dict form the rest of the loop expects.
                item = {"view": view_names[index], "x": item[0], "y": item[1]}
            else:
                results.append({
                    "ok": False, "index": index, "error": "no_view_for_position",
                    "message": "第 %d 条 [x, y] 没有对应的视图（本图 %d 个视图）"
                               % (index, len(view_names or [])),
                    "hint": "改用 [{\"view\": \"工程图视图1\", \"x\": 0.1, \"y\": 0.2}] 指定名字",
                })
                continue
        if not isinstance(item, dict):
            results.append({"ok": False, "index": index, "error": "placement_not_object"})
            continue
        name = item.get("view", item.get("view_name"))
        if name is None:
            results.append({
                "ok": False, "index": index, "error": "missing_view_key",
                "message": "这是第 %d 条 placement，缺视图名" % index,
                "hint": ("键名用 view 或 view_name 都可以；坐标用 x/y 或 x_m/y_m（单位：米）。"
                         "示例：[{\"view\": \"工程图视图1\", \"x\": 0.1, \"y\": 0.2}]"),
            })
            continue
        x = item.get("x", item.get("x_m"))
        y = item.get("y", item.get("y_m"))
        results.append(set_view(drawing_path, name,
                                x_m=x, y_m=y,
                                scale=item.get("scale"), save=False))
    if save:
        try:
            _save_drawing(_open_drawing(drawing_path))
        except Exception:
            pass
    ok_count = sum(1 for r in results if r.get("ok"))
    # P1: `ok: true` unconditionally was hiding total failure — a report got
    # `placed: 0, total: 2` and every entry `ok: false`, yet the top level said success and
    # only a per-entry read revealed nothing had moved. Same shape as the
    # `readback_verified` and `saved`-vs-`ok` bugs: a nested flag contradicting the verdict.
    failed = [r for r in results if not r.get("ok")]
    if failed:
        return error(
            "place_views_incomplete",
            "%d of %d placements failed" % (len(failed), len(results)),
            "Read `results[]` — each entry carries its own error. Nothing is assumed to have "
            "moved.",
            placed=ok_count, total=len(results), results=results)
    return drawing_ok(placed=ok_count, total=len(results), results=results)


def _dimension_measure(display):
    """(value, unit, dimension_type) for a display dimension, in its OWN unit.

    Report 2026-11-07 (C5/C6), two defects in one line each:

      * `delete_dimensions` compared `SystemValue * 1000` — for an ANGULAR dimension
        `SystemValue` is RADIANS, so a 45° dimension scored 785.398 and could never match
        the `keep_values` list, yet it stayed on the sheet because the delete path skipped
        it. The report found three such leftovers with negative-x paths in the PDF.
      * the post-delete `remaining` scan used `IDimension.Value * 1000`, but `Value` is
        already in document units (millimetres), so every survivor was reported 1000x too
        large — `60000.0` for a 60 mm dimension. The receipt then accused 23 legitimate
        dimensions of being leftovers.

    One reader, one unit convention: LENGTH -> (mm, "mm"), ANGLE -> (deg, "deg").
    Callers must only compare values that share a unit.
    """
    dim = display.GetDimension2(0)
    if dim is None:
        return None, None, None
    dim_type = None
    for getter in (lambda: int(dim.GetType), lambda: int(dim.Type)):
        try:
            dim_type = getter()
            break
        except Exception:
            continue
    raw = float(dim.SystemValue)          # metres for length, radians for angle
    if dim_type in (1, 3):
        return round(raw * 180.0 / 3.141592653589793, 4), "deg", dim_type
    return round(raw * 1000.0, 3), "mm", dim_type


def delete_dimensions(drawing_path, keep_values=None, view_name=None,
                      keep_tolerance_mm=0.05, save=True):
    """Delete dimensions whose value is not in keep_values.

    SolidWorks has no bulk delete: each dimension is selected by position and removed with
    EditDelete. Measured 2026-10-29: 14 dimensions in ONE call took < 5 s. An earlier
    timing note overestimated this by ~100x and made callers budget ten minutes for a
    job that takes seconds. This batches the loop and reports what was removed, so a
    49-dimension sheet can be reduced to the planned shortlist without hand-written COM.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        keep = sorted(float(v) for v in (keep_values or []))
        removed, kept, failed = [], [], []
        rows = []
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and view.Name != view_name:
                continue
            try:
                items = list(view.GetAnnotations or ())
            except Exception:
                continue
            for item in items:
                try:
                    ann = win.Dispatch(item)
                    if int(ann.GetType) != 4:
                        continue
                    _val, _unit, _type = _dimension_measure(ann)
                    if _val is None:
                        continue
                    rows.append((view, ann, _val, _unit, _type))
                except Exception:
                    continue

        for view, ann, value, unit, _type in rows:
            # Only LENGTHS are ever in `keep_values` (a mm list). An angular dimension is
            # never "kept" by that list — it is a leftover by definition and must go, or
            # the caller is told it survived when it did not.
            if unit == "mm" and keep and any(abs(value - target) <= keep_tolerance_mm
                                             for target in keep):
                kept.append({"value_mm": value, "unit": unit, "name": str(ann.GetName)})
                continue
            try:
                position = list(ann.GetPosition)
                name = str(ann.GetName)
                doc.ClearSelection2(True)
                try:
                    doc.ActivateView(view.Name)
                except Exception:
                    pass
                # SelectByID2 with an empty Name returns False for dimensions; the annotation
                # name is what actually selects it. Position is kept as a fallback for builds
                # where the name differs from the selection name.
                selected = False
                # The Callout argument must be a byref null VARIANT; passing None raises
                # 'type mismatch' and SelectByID2 returns False (quirk section 3).
                callout = byref_dispatch_null()
                for candidate, px, py, pz in ((name, 0.0, 0.0, 0.0),
                                              (name + "@" + view.Name, 0.0, 0.0, 0.0),
                                              ("", position[0], position[1], position[2])):
                    try:
                        if doc.Extension.SelectByID2(candidate, "DIMENSION", px, py, pz,
                                                     False, 0, callout, 0):
                            selected = True
                            break
                    except Exception:
                        continue
                if not selected:
                    failed.append({"value_mm": value, "reason": "select failed",
                                   "name": name})
                    continue
                doc.EditDelete()
                removed.append({"value_mm": value, "name": name})
            except Exception as exc:
                failed.append({"value_mm": value, "reason": str(exc)[:80]})

        doc.ClearSelection2(True)
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2
        except Exception:
            pass
        if save:
            _save_drawing(doc)

        # Report 2026-11-06 (P0, B15): two consecutive calls reported `removed: 3` and
        # `removed: 1`, both `ok: true` / `failed: 0` — and a `D2 / 785.398` dimension was
        # still on the sheet afterwards. The caller had no way to tell: the receipt only
        # counted what this loop thought it deleted. A delete that leaves items behind and
        # says nothing is the same class of failure as a save that did not save.
        #
        # So re-enumerate and report what SURVIVED. Only a value in `keep` is allowed to
        # remain; anything else is a real leftover and gets named.
        remaining = []
        try:
            for view in _iter_views(doc):
                if getattr(view, "Type", None) == 1:
                    continue
                try:
                    items = list(view.GetAnnotations or ())
                except Exception:
                    continue
                for item in items:
                    try:
                        ann = win.Dispatch(item)
                        if int(ann.GetType) != 4:
                            continue
                        display = win.Dispatch(ann.GetSpecificAnnotation)
                        value, unit, _t = _dimension_measure(display)
                        if value is None:
                            continue
                    except Exception:
                        continue
                    if unit == "mm" and any(abs(value - k) <= keep_tolerance_mm for k in keep):
                        continue
                    remaining.append({"value_mm": round(value, 3),
                                      "view": str(getattr(view, "Name", ""))})
        except Exception:
            remaining = []

        return drawing_ok(
            drawing=doc.GetTitle,
            removed=len(removed), kept=len(kept), failed=len(failed),
            removed_items=removed[:40], kept_values=[k["value_mm"] for k in kept][:40],
            failed_items=failed[:10],
            remaining_matches=remaining[:40],
            remaining_count=len(remaining),
            verified_clean=not remaining,
            note=("按值保留（默认容差 0.05 mm）。SolidWorks 无批量删除接口；实测 14 条一次调用 < 5 s。"
                  " `remaining_matches` 是删除后重新枚举的结果——**非空表示没删净**，"
                  "这些值不在 `keep_values` 里却还在图上，请用 `delete_annotation(name, position_m)` 精确删。"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("delete_dimensions_failed", str(exc))


def _note_bbox(text, x, y, height_m):
    """Conservative bounding box for a note anchored at (x, y) with its TOP-LEFT there.

    Chinese glyphs are roughly square, Latin roughly 0.55 em; 0.75 is the safe middle and
    over-estimating only costs a slightly larger keep-out, never a missed collision.
    """
    lines = str(text).splitlines() or [""]
    line_h = height_m * 1.6
    width = max(1, max(len(line) for line in lines)) * height_m * 0.75
    return [float(x), float(y) - line_h * len(lines), float(x) + width, float(y)]


def _boxes_overlap(a, b, pad=0.0):
    return (a[0] - pad < b[2] and b[0] - pad < a[2]
            and a[1] - pad < b[3] and b[1] - pad < a[3])


# Raw Unicode symbols that SolidWorks stores verbatim but exports to DWG/DXF as
# `\U+XXXX` escapes. AutoCAD decodes those; several Chinese CAD viewers do not, and fall
# back to a font without the glyph — the character becomes a box. A shipped drawing lost
# "自 Ø30 外圆面计" exactly this way, which matters because that clause defines the depth
# datum: read as a box, the shop either guesses or ignores it.
#
# Writing the format code instead makes SolidWorks export the CAD control code
# (`%%c` / `%%p` / `%%d`), which every viewer understands. Verified by a report:
# DXF `\U+00D8` count 1 -> 0, `%%c` count 4 -> 10.
_NOTE_SYMBOL_CODES = {
    "\u00d8": "<MOD-DIAM>",   # Ø
    "\u2300": "<MOD-DIAM>",   # ⌀ (diameter sign)
    "\u00b1": "<MOD-PM>",     # ±
    "\u00b0": "<MOD-DEG>",    # °
}


def _encode_note_symbols(text):
    """Replace raw Unicode technical symbols with SolidWorks format codes.

    Returns (encoded_text, warnings). A warning names the character and the code used, so
    the caller can see what changed instead of discovering it in the DXF.
    """
    warnings = []
    encoded = str(text)
    for char, code in _NOTE_SYMBOL_CODES.items():
        if char not in encoded:
            continue
        warnings.append({
            "char": char,
            "codepoint": "U+%04X" % ord(char),
            "replace_with": code,
            "reason": "raw char becomes a \\U+ escape in DWG/DXF; some CAD viewers draw a box",
        })
        encoded = encoded.replace(char, code)
    return encoded, warnings


def add_technical_requirements(drawing_path, lines, x_m=None, y_m=None, height_mm=3.5,
                               title="技术要求", auto_place=True, save=True):
    """Write a technical-requirements block as ONE note, not N notes.

    A drawing note holds newlines, so `InsertNote` takes the whole block at once. That is
    the entire reason this exists: writing 10-30 lines through `add_note` one call at a
    time is slow, and each call has to re-open, re-read the view outlines, and re-save.

    Numbering: a line that already starts with `N.` or `N、` is kept verbatim; anything else
    gets an ordinal. That way a caller can mix "1. 未注倒角 C1.5" with "去毛刺".

    auto_place walks the block UP the left margin until it clears every model view, then
    falls back to (x_m, y_m) if a caller insists on a position. Collision boxes come from
    `precise_outline`, so the 11.9 mm phantom margin does not push the block away for no
    reason.

    Multi-line rendering verified 2026-11-02: a 4-line note came back with a bbox height of
    22.4 mm (= 4 x 3.5 x 1.6) and rendered as 4 separate lines in the PDF.
    """
    try:
        if isinstance(lines, str):
            lines = [lines]
        entries = [str(x) for x in (lines or []) if str(x).strip()]
        if not entries:
            return error("bad_input", "lines is empty — nothing to write")

        # Numbering is all-or-nothing on purpose. Mixing kept and generated ordinals
        # produced "1. 倒角 / 2. 去毛刺 / 1. 调质 / 4. 公差" in testing — a caller who writes
        # one number clearly manages the numbering, so leave every line alone.
        def _has_ordinal(line):
            head = line[:3]
            return (len(line) > 2 and line[0].isdigit()
                    and len(head) > 1 and head[1] in ".,、")

        cleaned = [entry.strip() for entry in entries]
        if any(_has_ordinal(line) for line in cleaned):
            numbered = cleaned
        else:
            numbered = ["%d. %s" % (index, line)
                        for index, line in enumerate(cleaned, start=1)]
        body = "\n".join(([str(title)] if title else []) + numbered)

        doc = _open_drawing(drawing_path)
        sheet = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                sheet = view
                break
        height_m = float(height_mm) / 1000.0
        block_box = _note_bbox(body, 0.0, 0.0, height_m)
        block_h = -block_box[1]
        block_w = block_box[2]

        obstacles = []
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            try:
                from .drawing import precise_outline
                box = precise_outline(view)
            except Exception:
                box = None
            if box:
                obstacles.append([float(v) for v in box])

        sheet_w, sheet_h = 0.420, 0.297
        try:
            sheet = doc.GetFirstView
            outline = list(sheet.GetOutline)
            sheet_w = float(outline[2] - outline[0])
            sheet_h = float(outline[3] - outline[1])
        except Exception:
            pass

        chosen = None
        if x_m is not None and y_m is not None and not auto_place:
            chosen = (float(x_m), float(y_m))
        else:
            # The 10 mm margin is measured from the SHEET edge, not from the template's safe
            # area, so auto placement put the block at x=0.010 on a template whose safe left
            # edge is 0.020 — outside the frame (measured 2026-11-04, P2-7). Prefer the
            # template's own margins and fall back to 10 mm only when they cannot be read.
            margin = 0.010
            safe_area = None
            try:
                from .drawing import template_safe_area as _safe_area
                sheet_for_safe = doc.GetFirstView
                safe_area = _safe_area(sheet_for_safe, sheet_w, sheet_h)
            except Exception:
                safe_area = None
            left = max(margin, float((safe_area or {}).get("left", 0) or 0))
            top = sheet_h - max(margin, float((safe_area or {}).get("top", 0) or 0))
            bottom_limit = max(margin, float((safe_area or {}).get("bottom", 0) or 0))
            candidate = top
            while candidate - block_h > bottom_limit:
                box = [left, candidate - block_h, left + block_w, candidate]
                if not any(_boxes_overlap(box, ob, 0.004) for ob in obstacles):
                    chosen = (left, candidate)
                    break
                candidate -= block_h + 0.006
            if chosen is None and x_m is not None and y_m is not None:
                chosen = (float(x_m), float(y_m))
            if chosen is None:
                return error(
                    "no_room_for_requirements",
                    "%.0f mm of stacking space needed on a %.0f x %.0f mm sheet"
                    % (block_h * 1000, sheet_w * 1000, sheet_h * 1000),
                    "Pass x_m/y_m with auto_place=False, or delete a view.")

        result = add_note(drawing_path, body, chosen[0], chosen[1],
                          height_mm=height_mm, save=save, avoid_collisions=False)
        if not result.get("ok"):
            return result
        result["line_count"] = len(numbered)
        result["title"] = str(title) if title else None
        result["text"] = body
        result["block_height_mm"] = round(block_h * 1000, 1)
        return result
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("requirements_failed", str(exc))


def add_note(drawing_path, text, x_m, y_m, height_mm=None, bold=False, save=True,
             avoid_collisions=False):
    """Add a drawing note (技术要求 lines). x/y are sheet metres.

    `InsertNote` returns an INote; positioning it needs GetAnnotation invoked as a METHOD —
    pywin32 otherwise dispatches it as PROPERTYGET and the call fails.

    `avoid_collisions=True` (P3-2) walks the note DOWN until its estimated box clears every
    model view. A report showed the first line of a 技术要求 block being overprinted by a
    section view's arrow, and `move_annotation` was the only cure — which meant a rebuild if
    it failed. The note box is estimated from line count and height; views are taken from
    `precise_outline`, so the 11.9 mm phantom margin does not push notes away for no reason.
    """
    try:
        doc = _open_drawing(drawing_path)
        doc.ClearSelection2(True)

        collisions_avoided = 0
        position_requested = [round(float(x_m), 4), round(float(y_m), 4)]
        if avoid_collisions:
            from .drawing import precise_outline

            height_m = (float(height_mm) / 1000.0) if height_mm else 0.0035
            obstacles = []
            for view in _iter_views(doc):
                if getattr(view, "Type", None) == 1:
                    continue
                box = precise_outline(view)
                if box:
                    obstacles.append(box)
            while collisions_avoided < 24:
                box = _note_bbox(text, x_m, y_m, height_m)
                if not any(_boxes_overlap(box, obstacle, pad=0.002)
                           for obstacle in obstacles):
                    break
                step = (box[3] - box[1]) + 0.004
                if float(y_m) - step < 0.02:
                    break
                y_m = float(y_m) - step
                collisions_avoided += 1

        # Technical symbols go in as SolidWorks format codes, not raw Unicode: a raw "Ø"
        # is exported to DWG/DXF as "\U+00D8", which several Chinese CAD viewers render as
        # a box. The format code exports as "%%c" instead.
        encoded_text, text_warnings = _encode_note_symbols(text)
        created = doc.InsertNote(str(encoded_text))
        if created is None:
            return error("note_failed", "InsertNote returned null",
                         "Some templates refuse notes while a view is active; activate the sheet first.")
        note = created
        try:
            annotation = _call_method(note, "GetAnnotation")
            import win32com.client as win

            annotation = win.Dispatch(annotation)
            _call_method(annotation, "SetPosition2", float(x_m), float(y_m), 0.0)
        except Exception:
            pass
        if height_mm:
            try:
                note.SetHeight(float(height_mm) / 1000.0)
            except Exception:
                try:
                    _call_method(note, "SetHeight", float(height_mm) / 1000.0)
                except Exception:
                    pass
        if bold:
            try:
                note.SetBold(True)
            except Exception:
                pass
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2
        except Exception:
            pass
        if save:
            _save_drawing(doc)
        name = None
        try:
            name = str(_call_method(note, "GetAnnotation").GetName)
        except Exception:
            pass
        # P1-5: report the box the note actually occupies. A multi-line note shifted by
        # 49 mm looked identical to one shifted by 4 mm in the old receipt — only
        # `adjusted: true` — so the caller could not tell whether it now sat on a view or
        # the title block.
        final_bbox = _note_bbox(str(encoded_text), float(x_m), float(y_m),
                                (float(height_mm) / 1000.0) if height_mm else 0.0035)

        # Report 2026-11-04 P2-7: a block requested at y=0.07 grew DOWNWARDS to a bottom edge
        # of 0.038, which is below the template's 0.065 safe edge — it overlapped the title
        # block and the receipt said nothing (`adjusted: false, collisions_avoided: 0`).
        #
        # y is the TOP edge of the block (the same convention as add_note), so a tall block
        # silently grows into whatever is underneath. Checking the RESULT rather than the
        # request is the only way to catch that: the request was fine, the box was not.
        safe = None
        out_of_safe_area = None
        try:
            from .drawing import template_safe_area as _safe_area
            sheet = doc.GetFirstView
            outline = list(sheet.GetOutline)
            sheet_w = outline[2] - outline[0]
            sheet_h = outline[3] - outline[1]
            safe = _safe_area(sheet, sheet_w, sheet_h)
            if safe and final_bbox:
                violations = []
                if final_bbox[0] < safe.get("left", 0) - 1e-9:
                    violations.append("left")
                if final_bbox[1] < safe.get("bottom", 0) - 1e-9:
                    violations.append("bottom")
                if final_bbox[2] > sheet_w - safe.get("right", 0) + 1e-9:
                    violations.append("right")
                if final_bbox[3] > sheet_h - safe.get("top", 0) + 1e-9:
                    violations.append("top")
                if violations:
                    out_of_safe_area = {
                        "axes": violations,
                        "bbox_final": [round(v, 4) for v in final_bbox],
                        "safe_area": {k: round(float(v), 4) for k, v in safe.items()
                                      if isinstance(v, (int, float))},
                        # y is the block's TOP edge, so the usable y is the safe bottom plus
                        # the block height — saying "raise y" alone would not tell anyone by
                        # how much.
                        "max_y_for_this_block_m": round(
                            safe.get("bottom", 0) + (final_bbox[3] - final_bbox[1]), 4),
                    }
        except Exception:
            safe = None

        if out_of_safe_area:
            return error(
                "block_out_of_safe_area",
                "技术要求块落在安全区外（%s）。"
                % "、".join(out_of_safe_area["axes"]),
                "y 是文字块的**顶边**，块向下生长，所以字号越大越容易压到标题栏。"
                "该块底边 %.4f，安全区底边 %.4f；这个高度的块最多可用 y=%.4f。"
                % (final_bbox[1], safe.get("bottom", 0) if safe else 0,
                   out_of_safe_area["max_y_for_this_block_m"]),
                bbox_final=[round(v, 4) for v in final_bbox],
                safe_area=out_of_safe_area["safe_area"],
                max_y_for_this_block_m=out_of_safe_area["max_y_for_this_block_m"],
                position_m=[round(float(x_m), 4), round(float(y_m), 4)])

        return drawing_ok(drawing=doc.GetTitle, note=name, text=str(encoded_text),
                          text_requested=str(text), text_warnings=text_warnings,
                          position_m=[round(float(x_m), 4), round(float(y_m), 4)],
                          position_requested_m=position_requested,
                          adjusted=bool(collisions_avoided),
                          collisions_avoided=collisions_avoided,
                          bbox_final=[round(v, 4) for v in final_bbox],
                          shifted_m=([round(float(x_m) - position_requested[0], 4),
                                      round(float(y_m) - position_requested[1], 4)]
                                     if collisions_avoided else [0.0, 0.0]))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("note_failed", str(exc))


def add_balloon(drawing_path, view_name, x_m, y_m, text=None, item_number=None,
                leader_x_m=None, leader_y_m=None, save=True):
    """Add a BOM balloon to a view.

    Uses InsertBOMBalloon2 when an item number is known (attaches to the component under
    the point); otherwise inserts a plain balloon with the given text.
    """
    try:
        doc = _open_drawing(drawing_path)
        target = None
        for view in _iter_views(doc):
            if getattr(view, "Type", None) != 1 and view.Name == view_name:
                target = view
                break
        if target is None:
            return error("view_not_found", "no view named %r" % view_name)
        doc.ActivateView(target.Name)
        doc.ClearSelection2(True)
        # Both entry points go through call_method: direct attribute access on this binding
        # raises for some of them, and one user report on SW2026 shows `InsertBalloon`
        # resolving to nothing at all (DISP_E_UNKNOWNNAME) — i.e. renamed or removed in
        # that version. Reported honestly rather than hidden behind a bare except.
        balloon_error = None
        ballon = None
        if item_number is not None:
            for attempt in (5, 4, 3):
                try:
                    args = [item_number] + [0] * (attempt - 1)
                    if attempt == 5:
                        args[-1] = 0.0
                    ballon = _call_method(doc, "InsertBOMBalloon2", *args)
                    if ballon is not None:
                        break
                except Exception as exc:
                    balloon_error = "%s(%d args): %s" % ("InsertBOMBalloon2", attempt,
                                                         str(exc)[:60])
        if ballon is None:
            # `InsertBalloon` does not exist on this binding at all (GetIDsOfNames: False on
            # SW2024 SP4, DISP_E_UNKNOWNNAME on SW2026 per a user report). `InsertBOMBalloon`
            # and `InsertBOMBalloon2` do. Try the older entry point before giving up.
            for api in ("InsertBOMBalloon", "InsertBalloon"):
                try:
                    ballon = _call_method(doc, api, 0, 0, 0,
                                          float(x_m), float(y_m),
                                          float(leader_x_m or x_m),
                                          float(leader_y_m or y_m))
                    if ballon is not None:
                        balloon_error = None
                        break
                except Exception as exc:
                    balloon_error = "%s; %s: %s" % (balloon_error or "no callout",
                                                    api, str(exc)[:60])
        if ballon is None:
            return error("balloon_failed",
                         "no balloon could be created",
                         "%s | Verified on SW2024 SP4 only; a SW2026 report shows "
                         "InsertBalloon as DISP_E_UNKNOWNNAME (renamed or removed there)."
                         % (balloon_error or "both entry points returned null"))
        applied_text = None
        if text:
            try:
                ballon.SetText(str(text))
                applied_text = str(text)
            except Exception:
                try:
                    _call_method(ballon, "SetText", str(text))
                    applied_text = str(text)
                except Exception:
                    pass
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2
        except Exception:
            pass
        if save:
            _save_drawing(doc)
        return drawing_ok(drawing=doc.GetTitle, view=target.Name, text=applied_text,
                          item_number=item_number)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("balloon_failed", str(exc))


# Weld-symbol geometry, as fractions of the weld leg size. A fillet weld in GB/ISO is a
# right triangle sitting ON the reference line, leg length = the size called out.
_FILLET_REF_FACTOR = 3.3     # reference line length / leg size
_FILLET_INSET = 0.6          # distance from the reference line start to the triangle


def add_weld_symbol(drawing_path, weld_type="fillet", size_mm=6.0,
                    x_m=0.0, y_m=0.0, target_x_m=None, target_y_m=None,
                    save=True):
    """Draw a weld symbol as native SKETCH LINES — not as an IWeldSymbol.

    `representation` in the receipt says `sketch_block`, and that is deliberate: this is a
    faithful drawing of the standard symbol made from sheet lines plus a note, NOT a native
    SolidWorks weld annotation. It will not update with a weldment feature and it is not
    queryable as an IWeldSymbol.

    Why it exists: `IWeldSymbol` stores the symbol name as text but never instantiates the
    geometry on this build (measured 2026-11-02/03, SW 32.4.0, two parts). `SetText`
    returns True, `GetText(2)` reads back "FILL", and the sheet still shows only a leader
    and the size number. That is the same shape of failure as the GTol symbol cell, but
    without the 2022+ XML escape hatch.

    Important correction to the earlier investigation: `GetTriangleCount == 0` was NOT
    evidence of a missing triangle. In `gtol.sym` the fillet symbol is three LINEs, not a
    filled triangle object, so that counter would read 0 even when drawn correctly. The
    only valid check is the rendered sheet.

    Geometry (fillet), all scaled by the leg size `s = size_mm / 1000`:
        reference line   (x, y) -> (x + 3.3s, y)
        triangle         (x + 0.6s, y) -> (x + 0.6s, y + s) -> (x + 1.6s, y) -> close
        size text        left of the triangle, above the reference line
        leader           (x + 3.3s, y) -> (target_x_m, target_y_m) when a target is given

    `weld_type="butt"` is refused rather than approximated: a groove symbol is a different
    shape (V/U/J with its own root gap), and drawing it as a triangle would be worse than
    saying it is not supported.
    """
    try:
        kind = str(weld_type or "").lower()
        if kind not in ("fillet", "角焊"):
            return error(
                "weld_type_unsupported",
                "weld_type %r is not implemented" % weld_type,
                "Only 'fillet' is supported. A groove/plug/spot symbol has a different "
                "geometry and drawing it as a triangle would be a wrong callout.",
                supported=["fillet"])
        s = float(size_mm) / 1000.0
        if s <= 0:
            return error("bad_input", "size_mm must be positive")

        x = float(x_m)
        y = float(y_m)
        ref_len = s * _FILLET_REF_FACTOR
        gap = s * _FILLET_INSET
        segments = [
            (x, y, x + ref_len, y),                       # reference line
            (x + gap, y, x + gap, y + s),                 # triangle: vertical leg
            (x + gap, y + s, x + gap + s, y),             # hypotenuse
            (x + gap + s, y, x + gap, y),                 # base (closes the triangle)
        ]
        if target_x_m is not None and target_y_m is not None:
            segments.append((x + ref_len, y, float(target_x_m), float(target_y_m)))

        doc = _open_drawing(drawing_path)
        # A 2026-11-03 report caught the contradiction: this docstring says "the only valid
        # check is the rendered sheet", and then nothing checked it.
        #
        # ⚠️ CORRECTED 2026-11-06, and this comment lagged two days behind the code (flagged
        # again by a report on 2026-11-07). It used to claim that even with AddToDB set the
        # pixel diff stayed zero, i.e. that the weld triangle had never been drawn. That was
        # measured on `add_sheet_line` BEFORE `SketchManager.AddToDB = True` was actually
        # set, and I generalised it to this function without re-measuring.
        #
        # `add_sheet_line` now measures 571 changed pixels with AddToDB set, and this
        # function uses the same primitive plus the same pixel check — so the check below is
        # a real measurement, not a formality. `segments: N` counts CreateLine calls; only
        # `visibility.changed_pixels > 0` says anything rendered.
        _weld_before = None
        _weld_dir = None
        try:
            import tempfile as _tempfile
            from .drawing import _sf_render_page
            _weld_dir = _tempfile.mkdtemp(prefix="weld_")
            _weld_before = _sf_render_page(
                doc, os.path.join(_weld_dir, "before.pdf"), _weld_dir)
        except Exception:
            _weld_before = None
        sheet_obj = doc.GetCurrentSheet
        try:
            doc.ActivateSheet(sheet_obj.GetName)
        except Exception:
            pass
        # `SketchManager.CreateLine` on a drawing scales by the SHEET SCALE, it does not
        # take sheet metres directly. Measured 2026-11-03 on a 2:1 sheet: asking for
        # (0.05, 0.12) put the triangle at (0.11, 0.25) — exactly 2.1x. `add_sheet_line`'s
        # "sheet metres are already the right unit" note only holds at 1:1.
        sheet_scale = 1.0
        try:
            props = list(sheet_obj.GetProperties)
            num, den = float(props[2]), float(props[3])
            if num > 0 and den > 0:
                sheet_scale = num / den
        except Exception:
            sheet_scale = 1.0
        doc.ClearSelection2(True)
        try:
            doc.InsertSketch()
        except Exception:
            pass
        sketch = doc.SketchManager
        # AddToDB disables snapping and inference. Without it the first line snaps to the
        # nearest view geometry and the whole symbol lands somewhere else — measured
        # 2026-11-03: requested (0.05, 0.12), the triangle appeared next to the view at
        # roughly (0.11, 0.25) while the size text (added separately) stayed correct.
        try:
            sketch.AddToDB = True
        except Exception:
            pass
        drawn = 0
        for x1, y1, x2, y2 in segments:
            try:
                sketch.CreateLine(x1 / sheet_scale, y1 / sheet_scale, 0.0,
                                  x2 / sheet_scale, y2 / sheet_scale, 0.0)
                drawn += 1
            except Exception:
                continue
        sketch.InsertSketch(True)
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
            perf_hooks.bump("redraw_calls")
        except Exception:
            pass
        if drawn < len(segments):
            if save:
                _save_drawing(doc)
            return error("weld_geometry_incomplete",
                         "drew %d of %d segments" % (drawn, len(segments)),
                         "The sheet sketch may be locked; the partial geometry is left "
                         "on the sheet for inspection.",
                         representation="sketch_block")
        if save:
            _save_drawing(doc)

        # Report 2026-11-06 (P1): `triangle_verified` was the constant False, with a note
        # saying "the triangle probably is not drawn". A report rendered the PDF and SAW a
        # complete fillet symbol: reference line, right triangle, the "6.0" text and the
        # leader. The constant was the bug, and it also poisoned `capabilities`
        # (status: unsupported, "0 pixels") and the tool docstring (which cited an error
        # code, `weld_not_rendered`, that does not exist anywhere in the package).
        #
        # The earlier reasoning was right about ONE thing: a whole-page diff taken after
        # `add_note` cannot tell the triangle from the note. The fix is to take the diff
        # BEFORE the note exists. That isolates the sketch geometry exactly.
        _lines_only_pixels = 0
        _lines_visibility = {"checked": False, "reason": "render helper unavailable"}
        if _weld_before is not None:
            try:
                from .drawing import _sf_render_page
                from PIL import Image, ImageChops
                _lines_pdf = os.path.join(_weld_dir, "lines_only.pdf")
                _lines_img = _sf_render_page(doc, _lines_pdf, _weld_dir)
                _ld = ImageChops.difference(_weld_before.convert("RGB"),
                                            _lines_img.convert("RGB"))
                _lines_only_pixels = sum(1 for _p in _ld.get_flattened_data()
                                         if _p != (0, 0, 0))
                _lines_visibility = {"checked": True, "changed_pixels": _lines_only_pixels,
                                     "visible": _lines_only_pixels > 0}
            except Exception as _exc:
                _lines_visibility = {"checked": False, "reason": str(_exc)[:120]}

        text_result = add_note(drawing_path, str(size_mm),
                               x + gap * 0.2, y + s * 1.15,
                               height_mm=max(2.5, min(5.0, s * 1000 * 0.6)),
                               save=save)
        _weld_visibility = {"checked": False, "reason": "render helper unavailable"}
        if _weld_before is not None:
            try:
                from .drawing import _sf_render_page
                from PIL import Image, ImageChops
                _after = _sf_render_page(doc, os.path.join(_weld_dir, "after.pdf"), _weld_dir)
                _d = ImageChops.difference(_weld_before.convert("RGB"), _after.convert("RGB"))
                _changed = sum(1 for _p in _d.get_flattened_data() if _p != (0, 0, 0))
                _weld_visibility = {"checked": True, "changed_pixels": _changed,
                                    "visible": _changed > 0}
            except Exception as _exc:
                _weld_visibility = {"checked": False, "reason": str(_exc)[:120]}

        # 602 changed pixels were measured here on 2026-11-03 — but the same sheet drawn
        # WITHOUT this function changes 0, and a bare CreateLine changes 0 too, in all three
        # line styles (default / 6 centre / 0 solid). So those 602 pixels are the NOTE, not
        # the triangle: `add_weld_symbol` writes a note alongside the sketch, and notes render
        # natively.
        #
        # The pixel diff therefore proves "something was added to the sheet", never "the
        # triangle is there". Isolating it would need a lines-only variant that this function
        # does not have, so the receipt says exactly that instead of claiming either way.
        _triangle_isolated = False

        return drawing_ok(
            drawing=doc.GetTitle,
            weld_type="fillet",
            size_mm=float(size_mm),
            representation="sketch_block",
            is_native_weld_symbol=False,
            segments=len(segments),
            visibility=_weld_visibility,
            # Measured, not assumed: the diff was taken before the note existed, so these
            # pixels can only be the sketch geometry.
            triangle_verified=(_lines_only_pixels > 0),
            lines_only_visibility=_lines_visibility,
            triangle_note=(
                "`triangle_verified` comes from a lines-only diff: the sheet was rendered "
                "BEFORE `add_note` ran, so the changed pixels are the sketch geometry alone. "
                "A previous version hard-coded this to False and the capability table said "
                "\"0 pixels\"; a report rendered the PDF and saw the full symbol, which "
                "means the constant — not the drawing — was wrong. If `checked` is false "
                "the renderer was unavailable and nothing was verified either way."),
            size_text=str(size_mm),
            text_written=bool((text_result or {}).get("ok")),
            position_m=[round(x, 4), round(y, 4)],
            leader=([round(float(target_x_m), 4), round(float(target_y_m), 4)]
                    if target_x_m is not None and target_y_m is not None else None),
            note=("representation='sketch_block'：这是用图纸草图线画的角焊符号，"
                  "不是 IWeldSymbol 原生对象；不随焊件特征更新，也无法按焊接注解查询。"
                  "验收请导出 PDF 看图，不要只看回执。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("weld_symbol_failed", str(exc))


def _typed_drawing_doc(doc):
    """Wrap a late-bound drawing in its generated `IDrawingDoc`, if the type library exists.

    Report 2026-11-06 → corrected 2026-11-07 07:33: I spent 14 versions claiming
    `NewSheet4` was unreachable because pywin32 "cannot infer its signature". That was
    wrong on two counts:

      1. the type library IS already generated here —
         `%TEMP%/gen_py/3.13/83A33D31-27C5-11CE-BFD4-00400513BB57x0x32x0.py`;
      2. I guessed the argument ORDER. The real one is

             NewSheet4(Name, PaperSize, TemplateIn, Scale1, Scale2, FirstAngle,
                       TemplateName, Width, Height, PropertyViewName,
                       ZoneLeftMargin, ZoneRightMargin, ZoneTopMargin, ZoneBottomMargin,
                       ZoneRow, ZoneCol)

         `TemplateIn` is the THIRD argument and it is an I4, not a string. I was passing
         `None` / `''` there, which is why every arity failed with 类型不匹配 arg 6 — COM
         counts from 1 and the dispatch layer rejected the type before SolidWorks saw it.

    Measured 2026-11-07 on 飞达动力轴 A3: `('图纸1',)` -> `('图纸1', '图纸2')`.
    """
    import importlib
    import os as _os

    # `gencache.GetModuleForCLSID` returns None for this library even when the module is
    # present (measured 2026-11-07 — the CLSID is a *typelib* GUID, and GetModuleForCLSID
    # expects a coclass GUID). Import the generated file by its filename instead.
    try:
        import win32com.client.gencache as _gencache
        cache = _gencache.GetGeneratePath()
    except Exception:
        return None

    stem = "83A33D31-27C5-11CE-BFD4-00400513BB57"

    def _try_import():
        if _os.path.isdir(cache) and cache not in __import__("sys").path:
            __import__("sys").path.insert(0, cache)
        try:
            entries = _os.listdir(cache)
        except Exception:
            entries = []
        for entry in entries:
            if not (entry.lower().startswith(stem.lower()) and entry.endswith("x0.py")):
                continue
            try:
                mod = importlib.import_module(entry[:-3])
                return mod.IDrawingDoc(doc._oleobj_)
            except Exception:
                continue
        return None

    typed = _try_import()
    if typed is not None:
        return typed

    # ── Self-heal ────────────────────────────────────────────────────────────────────
    # Report 2026-11-07 (C1), filed independently by two testers on Python 3.11: the cache
    # only existed under `gen_py/3.13` (my interpreter), so `add_sheet` answered
    # `sheet_creation_blocked` on their machines and the capability table's `verified` was
    # therefore a claim about MY environment, not theirs.
    #
    # gencache is per Python version. Rather than telling the caller to run makepy, do it
    # here: `EnsureModule` writes `gen_py/<this pyver>/83A33D31-...x0x32x0.py`, and the
    # retry below then finds it. Measured 2026-11-07 on a 3.11 venv: one call, cache
    # appears, `add_sheet` goes from blocked to ok.
    try:
        _gencache.EnsureModule("{%s}" % stem, 0, 32, 0)
    except Exception:
        try:
            # Fall back to the dispatch route; harmless if it also fails.
            import win32com.client as _win
            _gencache.EnsureDispatch(_win.Dispatch("SldWorks.Application"))
        except Exception:
            pass
    return _try_import()


def add_sheet(drawing_path, name=None, paper=None, scale=1.0, save=True,
              first_angle=True):
    """Add another sheet to an existing .SLDDRW.

    Report 2026-11-06 (P0, A1): this capability did not exist at all — a user who needed
    "multi-page + full dimensioning in ONE native drawing" had to build three separate
    files and merge them with an external PDF tool, which breaks title-block page
    numbering and `delivery_check`'s coverage accounting.

    **Now it works.** The earlier claim that `NewSheet4` was unreachable was my own
    argument-order mistake; see `_typed_drawing_doc`. Verified: A3 sheet becomes
    `('图纸1', '图纸2')`.

    Falls back to the documented `sheet_creation_blocked` refusal only when the type
    library is genuinely missing (a fresh Python install with no `gen_py` cache and no
    SolidWorks type library registered).
    """
    # `swDwgPaperSizes_e` — measured 2026-11-07 by passing every candidate to NewSheet4
    # and reading `ISheet.GetProperties` back. The A series sits ONE SLOT HIGHER than the
    # published enumeration, so the docs are not usable here:
    #     6  -> 297x210   A4
    #     8  -> 420x297   A3
    #     9  -> 594x420   A2
    #     10 -> 841x594   A1
    #     11 -> 1189x841  A0
    #     0  -> 216x279   Letter      2 -> 432x279 (B)   3 -> 559x432 (C)
    # The old table was `A0:0 ... A4:4`, which is why every sheet came out Letter.
    #
    # `sheet_size_matches_request` in the receipt is the authority: if a value ever drifts
    # on another build, the caller sees `false` instead of a plausible-looking `ok: true`.
    paper_map = {"A4": 6, "A3": 8, "A2": 9, "A1": 10, "A0": 11,
                 "B": 2, "C": 3, "D": 4, "E": 5,
                 "LETTER": 0}
    try:
        doc = _open_drawing(drawing_path)
        try:
            existing = list(doc.GetSheetNames or ())
        except Exception:
            existing = []
        target_name = name or ("图纸%d" % (len(existing) + 1))
        paper_code = paper_map.get(str(paper or "").upper(), 8)   # 8 = A3

        typed = _typed_drawing_doc(doc)
        if typed is None:
            return error(
                "sheet_creation_blocked",
                "the SolidWorks type library is not available to pywin32",
                ("Run once, outside this tool: `python -m win32com.client.makepy "
                 "\"SldWorks 20xx Type Library\"` (or `win32com.client.gencache."
                 "EnsureDispatch` on any SldWorks object). That writes "
                 "%TEMP%/gen_py/<pyver>/83A33D31-27C5-11CE-BFD4-00400513BB57x0x32x0.py "
                 "and this tool picks it up automatically. Until then use a multi-sheet "
                 "template or merge PDFs."),
                sheets=existing)

        # ── Template, not a blank page ───────────────────────────────────────────────
        # Report 2026-11-07 (C2): `add_sheet(paper="A2")` returned ok:true but the new
        # sheet was 279.4 x 215.9 mm (Letter) with **no frame, no title block, no views** —
        # `get_drawings()` path count 0 on the exported page 2.
        #
        # Cause: `TemplateIn = 0` means "no template", so SolidWorks made an empty page at
        # the default paper, and the `PaperSize` argument only matters when a template is
        # not supplying the size. Reuse the same GB template `create_drawing` uses.
        try:
            from .drawing import _find_template as _find_drw_template
            _tpl = _find_drw_template(str(paper or "A3").upper())
        except Exception:
            _tpl = None
        _tpl_name = str(_tpl or "")
        # TemplateIn: 0 = none, 1 = the named template, 2 = system default.
        _tpl_flag = 1 if _tpl_name else 0

        try:
            # ARGUMENT ORDER, measured 2026-11-07: the SECOND argument is the template
            # flag and the THIRD is the paper size — the reverse of what the names suggest
            # when read as `NewSheet4(Name, PaperSize, TemplateIn, ...)`. Passing the size
            # in slot 2 gave a Letter page for every A-series request; passing it in slot 3
            # gives 297x210 for A4 and 841x594 for A0.
            made = typed.NewSheet4(target_name, _tpl_flag, paper_code,
                                   float(scale), float(scale), bool(first_angle),
                                   _tpl_name, 0.0, 0.0, "", 0.0, 0.0, 0.0, 0.0, 1, 1)
        except Exception as exc:                                    # noqa: BLE001
            return error("sheet_creation_failed", str(exc)[:200],
                         "NewSheet4(Name, PaperSize, TemplateIn, Scale1, Scale2, ...) — "
                         "TemplateIn is the THIRD argument: 0=none, 1=TemplateName, "
                         "2=default. With 0 the page is blank and `paper` is ignored.")

        after = list(doc.GetSheetNames or ())
        created = len(after) > len(existing)
        if created and save:
            _save_drawing(doc)
        if not created:
            return error("sheet_not_created",
                         "NewSheet4 returned %r but the sheet list is unchanged" % (made,),
                         sheets=after)
        # Report the size the sheet ACTUALLY got. `ok: true` on a Letter-sized blank page
        # is worse than a refusal — the caller plans the rest of the pipeline around it.
        # `ISheet.GetSize(Width, Height)` takes two byref in/out parameters and pywin32
        # cannot supply them (measured 2026-11-07: no-args -> 非选择性的参数,
        # (0.0, 0.0) -> 类型不匹配 arg 1). `GetProperties` returns a flat list whose LAST
        # TWO entries are the sheet width/height in metres — measured [8.0, 12.0, 1.0,
        # 1.0, 1.0, 0.42, 0.297] on an A3 sheet.
        size_actual = None
        try:
            sheet = doc.GetCurrentSheet
            props = list(sheet.GetProperties or ())
            if len(props) >= 2:
                size_actual = [round(float(props[-2]), 5), round(float(props[-1]), 5)]
        except Exception:
            size_actual = None
        expected = {"A0": [1.189, 0.841], "A1": [0.841, 0.594], "A2": [0.594, 0.42],
                    "A3": [0.420, 0.297], "A4": [0.297, 0.210]}
        want = expected.get(str(paper or "A3").upper())
        size_ok = None
        if size_actual and want:
            size_ok = (abs(size_actual[0] - want[0]) < 0.005
                       and abs(size_actual[1] - want[1]) < 0.005)

        return drawing_ok(
            drawing=doc.GetTitle, sheets=after,
            sheets_before=len(existing), sheets_after=len(after),
            created=True, sheet_name=target_name, paper=str(paper or "A3").upper(),
            scale=float(scale),
            template_applied=bool(_tpl_name), template=_tpl_name or None,
            sheet_size_m_actual=size_actual,
            sheet_size_matches_request=size_ok,
            note=("新图纸页已建立：%s（%d 页，%s）"
                  % (target_name, len(after),
                     "%.1f×%.1f mm" % (size_actual[0] * 1000, size_actual[1] * 1000)
                     if size_actual else "尺寸未读到")))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("sheet_creation_failed", str(exc))


def _sw_version():
    """SolidWorks build string, so a receipt can say which build behaved this way.

    Report 2026-11-07: one of the two P0s was purely an environment difference
    (`ActivateSheet` returns True without switching on SW2024). Without the version in the
    receipt, a caller cannot tell "the tool is broken" from "this build behaves differently".
    """
    try:
        from ..sw_session import get_sw
        return str(getattr(get_sw(), "RevisionNumber", "") or "") or None
    except Exception:
        return None


class _SheetNotActive(Exception):
    """The document is not on the page the caller asked for. Raised, never swallowed."""


def _sheet_model_views(doc, expected=None):
    """Model views on the ACTIVE sheet — not the whole document.

    Measured 2026-11-07: `_iter_views(doc)` walks every view on every page, so a two-sheet
    drawing reported the SAME view list for both pages, and `add_view_to_sheet` "succeeded"
    by looking at a count that had not moved. `IView` has no `Sheet` property and
    `ISheet.GetSize` takes byref parameters pywin32 cannot supply, but `ISheet.GetViews`
    returns exactly the views belonging to that sheet.
    """
    # Report 2026-11-07 (P0-2): on SW2024 `ActivateSheet` can return True without changing
    # the current page, so this reader silently returned page 1's views no matter which page
    # was asked for. `expected` makes that observable instead of silent: when the document
    # says it is on a different page, the caller must not treat the result as that page's.
    try:
        sheet = doc.GetCurrentSheet
        if expected is not None:
            _actual = str(sheet.GetName)
            if _actual != str(expected):
                raise _SheetNotActive("expected %r, document is on %r"
                                      % (expected, _actual))
    except _SheetNotActive:
        raise
    except Exception:
        return []
    views = []
    try:
        raw = list(sheet.GetViews or ())
    except Exception:
        return []
    for item in raw:
        try:
            import win32com.client as _win
            view = _win.Dispatch(item)
            if getattr(view, "Type", None) == 1:      # the sheet view itself
                continue
            views.append(view)
        except Exception:
            continue
    return views


def list_sheets(drawing_path):
    """Every sheet in the drawing, with its size, view count and whether it is active.

    Report 2026-11-07 (C3): after `add_sheet` there was **no way to ask which sheets exist
    or which one is active**. The caller had to infer it from `add_sheet`'s own reply, and
    every other tool silently operated on whatever page happened to be current — which is
    how "dimension page 2" became "dimension an empty page and report ok:true".

    `ISheet.GetProperties` returns a flat list whose LAST TWO entries are width/height in
    metres (measured [8.0, 12.0, 1.0, 1.0, 1.0, 0.42, 0.297] on A3). `GetSize` cannot be
    used: it takes two byref in/out parameters that pywin32 cannot supply.
    """
    try:
        doc = _open_drawing(drawing_path)
        names = list(doc.GetSheetNames or ())
        try:
            active = str(doc.GetCurrentSheet.GetName)
        except Exception:
            active = None
        sheets = []
        for index, name in enumerate(names, start=1):
            entry = {"index": index, "name": str(name), "active": str(name) == active}
            try:
                # Activate EVERY page, including the one that is already active.
                #
                # The previous version skipped the switch when `entry["active"]` was true,
                # assuming the current page was that page — but the loop had already moved
                # it on the previous iteration, so the "active" sheet was read while a
                # DIFFERENT page was current. Measured 2026-11-07: page 2 reported page 1's
                # view, and `add_view_to_sheet` then "succeeded" by comparing a count that
                # never moved.
                doc.ActivateSheet(str(name))
                props = list(doc.GetCurrentSheet.GetProperties or ())
                if len(props) >= 2:
                    entry["size_m"] = [round(float(props[-2]), 5),
                                       round(float(props[-1]), 5)]
            except Exception:
                pass
            # `ISheet.GetViews` — NOT `_iter_views(doc)`, which returns every view in the
            # document and made both pages report the same list (measured 2026-11-07).
            views = [str(v.Name) for v in _sheet_model_views(doc)]
            entry["views"] = views
            entry["view_count"] = len(views)
            sheets.append(entry)
        if active:
            try:
                doc.ActivateSheet(active)
            except Exception:
                pass
        return drawing_ok(
            drawing=doc.GetTitle, sheet_count=len(sheets), sheets=sheets,
            active_sheet=active,
            note=("按 `index`（1 起）或 `name` 切页：`activate_sheet`。"
                  "只有 `active: true` 的那一页会被其它工具修改。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("list_sheets_failed", str(exc))


def activate_sheet(drawing_path, name=None, index=None, save=True):
    """Make one sheet current. Every other tool edits the ACTIVE sheet.

    Report 2026-11-07 (C3): this did not exist, so after `add_sheet` the new page stayed
    active and `auto_dimension` dutifully dimensioned a page with no views on it.
    """
    try:
        doc = _open_drawing(drawing_path)
        names = [str(n) for n in (doc.GetSheetNames or ())]
        if not names:
            return error("no_sheets", "the drawing reports zero sheets")
        target = None
        if name:
            if name not in names:
                return error("sheet_not_found",
                             "no sheet named %r" % name,
                             "existing: %s" % ", ".join(names),
                             sheets=names)
            target = name
        elif index is not None:
            try:
                target = names[int(index) - 1]
            except Exception:
                return error("sheet_not_found",
                             "index %r is outside 1..%d" % (index, len(names)),
                             sheets=names)
        else:
            try:
                current = str(doc.GetCurrentSheet.GetName)
            except Exception:
                current = None
            return drawing_ok(drawing=doc.GetTitle, sheets=names,
                              active_sheet=current,
                              note="未指定 name/index，只报告当前活动页。")
        ok = bool(doc.ActivateSheet(target))
        # Report 2026-11-07 (P0-2): the receipt echoed the ARGUMENT. On SW2024 the call
        # returns True and the page does not change, so `active_sheet: "图纸2"` was a
        # straight lie. Read the page back and report that.
        current = None
        try:
            current = str(doc.GetCurrentSheet.GetName)
        except Exception:
            current = None
        effective = bool(ok) and (current is not None and current == target)
        if not effective:
            return error(
                "sheet_activation_not_effective",
                "请求切到 %r，但当前页仍是 %r" % (target, current),
                ("SolidWorks 报告 ActivateSheet 成功，实际没有切过去。"
                 "已知 SW2024 存在该行为。后续写操作会改到 %r，请勿继续。"
                 % current),
                requested=target, current=current, activated=bool(ok),
                sheets=names, sw_version=_sw_version())
        if save:
            try:
                _save_drawing(doc)
            except Exception:
                pass
        return drawing_ok(drawing=doc.GetTitle, sheets=names,
                          active_sheet=current, activated=True,
                          sheet_index=names.index(target) + 1)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("activate_sheet_failed", str(exc))


def add_view_to_sheet(drawing_path, sheet, model_path=None, view_name="*前视",
                      x_m=0.15, y_m=0.15, scale=None, save=True):
    """Create a model view ON A NAMED SHEET, activating that sheet first.

    Report 2026-11-07 (C3): `add_sheet` produced a blank page and nothing could put a view
    on it, so "multi-page in one .SLDDRW" stayed unreachable and callers kept merging
    separate files. `CreateDrawViewFromModelView3` always draws on the ACTIVE sheet, so the
    missing piece was never the view API — it was sheet activation.

    `model_path` defaults to the model the drawing already references.
    """
    try:
        doc = _open_drawing(drawing_path)
        names = [str(n) for n in (doc.GetSheetNames or ())]
        target = None
        if isinstance(sheet, int):
            try:
                target = names[sheet - 1]
            except Exception:
                return error("sheet_not_found", "index %r outside 1..%d"
                             % (sheet, len(names)), sheets=names)
        elif sheet:
            if str(sheet) not in names:
                return error("sheet_not_found", "no sheet named %r" % sheet,
                             "existing: %s" % ", ".join(names), sheets=names)
            target = str(sheet)
        else:
            try:
                target = str(doc.GetCurrentSheet.GetName)
            except Exception:
                return error("sheet_not_found", "no sheet specified and none is active")

        ok = bool(doc.ActivateSheet(target))
        _current = None
        try:
            _current = str(doc.GetCurrentSheet.GetName)
        except Exception:
            _current = None
        if not ok or (_current is not None and _current != target):
            return error(
                "sheet_activation_not_effective",
                "请求在 %r 上建视图，但当前页仍是 %r" % (target, _current),
                "这里拒绝继续：否则视图会建在错误的图纸页上，而回执会说是 %r。" % target,
                requested=target, current=_current, activated=bool(ok),
                sheets=names, sw_version=_sw_version())
        # From here the document IS on `target`; ask the reader to prove it rather than
        # relying on the check above having been enough.
        try:
            _sheet_model_views(doc, expected=target)
        except Exception as exc:                                    # noqa: BLE001
            return error("sheet_activation_not_effective", str(exc)[:120],
                         "the per-sheet view reader disagrees with the activation",
                         requested=target, sheets=names)

        model = model_path
        if not model:
            for view in _iter_views(doc):
                if getattr(view, "Type", None) == 1:
                    continue
                try:
                    model = str(view.GetReferencedModelName or "")
                except Exception:
                    model = ""
                if model:
                    break
        if not model or not os.path.exists(str(model)):
            return error("no_model_reference",
                         "the drawing has no model to derive a view from, and "
                         "`model_path` was not given",
                         "pass model_path explicitly for a drawing that has no views yet")

        before = len(_sheet_model_views(doc))
        try:
            view = doc.CreateDrawViewFromModelView3(str(model), str(view_name),
                                                    float(x_m), float(y_m), 0)
        except Exception as exc:                                    # noqa: BLE001
            return error("view_creation_failed", str(exc)[:200])
        if view is None:
            return error("view_creation_failed",
                         "CreateDrawViewFromModelView3 returned null",
                         "check that view_name is a standard view of %s"
                         % os.path.basename(str(model)))

        # The call ignores x/y on SW2024 (measured) — write the position explicitly.
        try:
            view.Position = (float(x_m), float(y_m), 0.0)
        except Exception:
            pass

        applied_scale = None
        if scale is not None:
            try:
                view.ScaleDecimal = float(scale)
                applied_scale = round(float(view.ScaleDecimal), 5)
            except Exception:
                applied_scale = None
        try:
            doc.EditRebuild3()
            perf_hooks.bump("rebuild_calls")
        except Exception:
            pass
        if save:
            try:
                _save_drawing(doc)
            except Exception:
                pass

        after_views = [str(v.Name) for v in _sheet_model_views(doc)]
        created = len(after_views) > before
        if not created:
            return error("view_not_created",
                         "CreateDrawViewFromModelView3 returned an object but the sheet's "
                         "view list did not grow",
                         sheets=names, views=after_views)
        return drawing_ok(drawing=doc.GetTitle, sheet=target, sheet_index=names.index(target) + 1,
                          view_name=str(view_name), model=os.path.basename(str(model)),
                          position_m=[round(float(x_m), 4), round(float(y_m), 4)],
                          scale=applied_scale, views=after_views,
                          view_count=len(after_views),
                          note="视图已建在「%s」上（该页现在是活动页）。" % target)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("add_view_to_sheet_failed", str(exc))


def add_sheet_line(drawing_path, x1_m, y1_m, x2_m, y2_m, save=True,
                   verify_render=True):
    """Draw a line in TRUE sheet space.

    `SketchManager.CreateLine` on a drawing interprets coordinates as model space and
    scales them by the view scale: asking for a line at (236, 62) produced a fan of
    diagonals across the sheet. This activates the sheet view first, then converts the
    requested sheet point through the sheet's own transform, which is the only way to get
    the line where it was asked for.
    """
    try:
        doc = _open_drawing(drawing_path)
        # verify_render: until 0.8.87 this function reported ok:true for a line that never
        # appeared. Measured 2026-11-03 on 旋转轴: export the PDF, draw, export again, and
        # the two rendered pages differ by **0 pixels out of 16,041,784**. Every caller that
        # trusted the receipt — including the BOM frame use it was written for — got nothing.
        #
        # So the before/after pixel diff is now part of the contract, the same way
        # `add_surface_finish` proves the symbol rendered. Cost: two PDF exports per call.
        before_page = None
        work_dir = None
        if verify_render:
            import tempfile as _tempfile
            try:
                from .drawing import _sf_render_page
                work_dir = _tempfile.mkdtemp(prefix="sheetline_")
                before_page = _sf_render_page(
                    doc, os.path.join(work_dir, "before.pdf"), work_dir)
            except Exception:
                before_page = None
        sheet = doc.GetFirstView
        try:
            doc.ActivateSheet(doc.GetCurrentSheet().GetName)
        except Exception:
            pass
        doc.ClearSelection2(True)
        try:
            doc.InsertSketch()
        except Exception:
            pass
        sketch = doc.SketchManager
        # Report 2026-11-06: `add_weld_symbol` sets `AddToDB` before drawing and its
        # geometry DOES appear in the PDF (2134 changed pixels, lines-only diff); this
        # function never set it and reported 0. That correlation is the strongest lead so
        # far, and the earlier "sheet sketch never renders" conclusion was drawn without it.
        #
        # AddToDB disables snapping and inference, so the stroke lands where the caller
        # asked instead of jumping to nearby geometry.
        try:
            sketch.AddToDB = True
        except Exception:
            pass
        # Sheet space -> sketch space: a drawing sketch is placed at the sheet origin, so
        # sheet metres are already the right unit. Passing millimetres is what produced the
        # scaled fan of lines.
        sketch.CreateLine(float(x1_m), float(y1_m), 0.0,
                          float(x2_m), float(y2_m), 0.0)
        try:
            sketch.AddToDB = False
        except Exception:
            pass
        sketch.InsertSketch(True)
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2
        except Exception:
            pass
        if save:
            _save_drawing(doc)

        visibility = {"checked": False, "reason": "verify_render=False"}
        if before_page is not None:
            try:
                from .drawing import _sf_render_page
                from PIL import Image, ImageChops
                after = _sf_render_page(doc, os.path.join(work_dir, "after.pdf"), work_dir)
                diff = ImageChops.difference(before_page.convert("RGB"),
                                             after.convert("RGB"))
                changed = sum(1 for p in diff.get_flattened_data() if p != (0, 0, 0))
                visibility = {"checked": True, "changed_pixels": changed,
                              "visible": changed > 0}
            except Exception as exc:
                visibility = {"checked": False, "reason": str(exc)[:120]}

        if visibility.get("checked") and not visibility.get("visible"):
            return error(
                "line_not_rendered",
                "the sheet line did not change a single pixel on the exported page",
                "Drawing-sheet sketches are NOT rendering on this build — `CreateLine` "
                "returns an object and `SketchSegment.Style` is writable, but nothing is "
                "drawn. Do not rely on add_sheet_line for frames or centre lines until "
                "this is solved; see docs/com-quirks.md section 55.",
                line_m=[[round(float(x1_m), 4), round(float(y1_m), 4)],
                        [round(float(x2_m), 4), round(float(y2_m), 4)]],
                visibility=visibility)

        return drawing_ok(drawing=doc.GetTitle,
                          line_m=[[round(float(x1_m), 4), round(float(y1_m), 4)],
                                  [round(float(x2_m), 4), round(float(y2_m), 4)]],
                          visibility=visibility,
                          note=("坐标为图纸空间米；图纸草图不接受毫米。"
                                "verify_render=True 时用前后 PDF 像素差分证明线真的出现了；"
                                "0 像素=线没画上，返回 line_not_rendered。"))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("sheet_line_failed", str(exc))


def add_bom_table(drawing_path, view_name=None, configuration=None,
                  template=None, save=True):
    """Insert a POPULATED native TopLevelOnly BOM into an assembly drawing.

    Verified working 2026-10-29 on SW2024 SP4: 10 rows x 4 columns with a quantity
    column, stable across save/reopen.

    `configuration` defaults to None, meaning "use the drawing's active configuration".
    Report 2026-11-05: the old default was the literal string "Default", and on a Chinese
    GB template the only configuration is named 默认 — so `InsertBomTable4(Indented)`
    returned None and the receipt blamed the SW version ("only verified on SW2024 SP4")
    while running ON SW2024 SP4. Measured: `configuration="Default"` -> bom_table_failed;
    `configuration="默认"` -> ok, 3 rows x 4 columns.

    Why the two-step sequence is required (and why everything simpler failed):
      * `InsertBomTable4(BomType=TopLevelOnly=2)` creates ONLY the header here, and
        `BomFeature.SetConfigurations` does not repair it (every marshalling form returns
        True or False but the column count never moves off 3).
      * `InsertBomTable4(BomType=Indented=3)` with a valid configuration DOES populate
        rows (120 rows, 4 columns) — proving assembly, template and model reference are all
        fine; only TopLevelOnly's configuration linkage is broken.
      * Converting the populated table with `BomFeature.TableType = 2` yields the 9 module
        rows plus ONE duplicate quantity column (5 columns); hiding that last column gives
        the target shape.

    Do NOT call SetConfigurations in this flow — it clears the table back to its header.
    Do NOT try to add a quantity column via SetColumnType — SOLIDWORKS forbids promoting a
    normal column to Quantity (SetColumnType2(..., 203, ...) returns False).
    """
    try:
        from ..sw_session import error, get_sw

        doc = _open_drawing(drawing_path)
        sw = get_sw()
        template = template or default_template(sw)
        if not os.path.exists(template):
            return error("template_missing", "BOM template not found: %s" % template)

        views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
        if not views:
            return error("no_views", "drawing has no model views")
        target = views[0]
        if view_name:
            target = next((v for v in views if v.Name == view_name), None)
            if target is None:
                return error("view_not_found", "no view named %r" % view_name)

        # Resolution order, because getting this wrong looks exactly like a version bug:
        #   1. what the caller passed
        #   2. the view's ReferencedConfiguration (may be an empty string)
        #   3. the referenced assembly's ACTIVE configuration
        #   4. only then the literal "Default"
        #
        # Report 2026-11-05: step 4 is where a Chinese GB template dies — its only
        # configuration is 默认, so "Default" matches nothing, InsertBomTable4(Indented)
        # returns None, and the receipt blames the SolidWorks version while running on the
        # exact version it claims to support.
        if not configuration:
            try:
                configuration = str(getattr(target, "ReferencedConfiguration", "") or "")
            except Exception:
                configuration = ""
        if not configuration:
            try:
                model_name = str(getattr(target, "GetReferencedModelName", "") or "")
                if model_name:
                    model_doc = _open_drawing(model_name) if model_name.lower().endswith(
                        ".slddrw") else None
                    if model_doc is None:
                        from ..sw_session import get_sw
                        model_doc = get_sw().OpenDoc(model_name, 2)   # 2 = assembly
                    if model_doc is not None:
                        configuration = str(
                            model_doc.ConfigurationManager.ActiveConfiguration.Name or "")
            except Exception:
                pass
        configuration = configuration or "Default"

        result = populate_top_level_bom(
            doc, target, template,
            configuration=configuration,
        )
        if save:
            _save_drawing(doc)

        return drawing_ok(
            drawing=doc.GetTitle,
            created=True,
            native_bom=True,
            populated=True,
            rows=result["rows"],
            columns=result["columns"],
            data_rows=result["data_rows"],
            headers=result["headers"],
            table_type=result["table_type"],
            referenced_model=result["referenced_model"],
            sample_data=result["data"][:3],
            saved=bool(save),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("bom_table_failed", str(exc))




# --- delete / move: the missing half of the API surface -------------------------------
#
# Every create-only design funnels into the same dead end: one wrong parameter means
# rebuilding the whole sheet, because nothing on it can be removed. A report measured this
# precisely — four full rebuilds for one drawing, three of them solely because the sheet
# carried leftovers (`delete_view` / `delete_annotation` absent). `name` was already exposed
# by list_drawing_annotations, so deletion was the cheapest possible fix.

# SelectByID2 does not accept a generic "ANNOTATION" — notes, GT frames and surface symbols
# each have their own type string, and the reported `name` ("细节项目256") is not resolvable
# by ID at all. Selecting by the annotation's own position is what actually works.
DELETABLE_SELECT_TYPES = ("NOTE", "GTOL", "SFSYMBOL", "DIMENSION", "ANNOTATION")


def _annotation_total(doc):
    """Annotation count summed over EVERY view.

    The v0.8.14 report's P0 was partly a measurement bug: `doc.GetFirstView.GetAnnotations`
    is the SHEET, and its count does not change when a DIMENSION is deleted, because
    dimensions belong to the model views. Comparing only the sheet made a successful
    delete answer `delete_failed`. Measured 2026-10-31 on a real sheet: the sheet stayed at
    83 while 工程图视图1 went 8 -> 7 and the total went 122 -> 121.

    IMPORTANT — this is NOT the same number as `annotation_count` in
    `list_drawing_annotations`. Report 2026-11-05 flagged the two as "两套口径" and it is
    right that they disagree:

      * this function counts EVERY annotation object SolidWorks hands back, including
        template furniture (审核/校核/设计/标记 — a fresh A3 sheet reports 82 of them);
      * `annotation_count` strips that furniture, because a user asking "how many
        annotations are on my drawing" does not mean "including the printed title block".

    They serve different purposes and must not be compared. This one is used only as a
    before/after DELTA around a single delete, where the constant furniture cancels out.
    """
    total = 0
    counted = False
    for view in _iter_views(doc):
        try:
            total += len(list(view.GetAnnotations or ()))
            counted = True
        except Exception:
            continue
    return total if counted else None


def _find_annotation_by_name(doc, name, position_m=None):
    """Return (annotation, position_m) for an annotation with this reported name.

    Iterates EVERY view, not just the first one. `doc.GetFirstView` is the sheet, and its
    GetAnnotations returns sheet furniture only — but in practice every annotation, including
    model-view ones, hangs off the sheet view too (the name reported by
    list_drawing_annotations, e.g. "细节项目256", does appear here). Sweeping all views is
    still the correct approach: it costs one extra loop and covers the case where an
    annotation belongs to a model view.

    `position_m` disambiguates repeated names: SolidWorks names dimensions D1/D2/D3 per
    sketch, so one sheet can hold eight annotations called "D1". Passing the position that
    list_drawing_annotations reported selects that specific one instead of the first hit.
    """
    import win32com.client
    try:
        candidates = []
        for view in _iter_views(doc):
            try:
                # _call_method is this module's alias for sw_session.call_method. Writing
                # `call_method` here raised NameError, which the surrounding except turned
                # into a silent "no candidates" — the finder then reported not_found for
                # names the listing tool had just printed.
                candidates.extend(list(_call_method(view, "GetAnnotations") or ()))
            except Exception:
                continue
        for raw in candidates:
            annotation = win32com.client.Dispatch(raw)
            try:
                if str(annotation.GetName) != name:
                    continue
                position = [float(v) for v in annotation.GetPosition]
                if position_m is not None:
                    try:
                        if not all(abs(position[i] - float(position_m[i])) <= 1e-3
                                   for i in range(2)):
                            continue
                    except Exception:
                        continue
                return annotation, position
            except Exception:
                continue
    except Exception:
        pass
    return None, None


def _select_by_name(doc, name, select_type, byref_dispatch_null):
    """Select one object by its reported name. Returns True when the selection took."""
    try:
        ok_selected = doc.Extension.SelectByID2(name, select_type, 0, 0, 0,
                                               False, 0, byref_dispatch_null(), 0)
    except Exception:
        return False
    if not ok_selected:
        return False
    try:
        return doc.SelectionManager.GetSelectedObjectCount2(-1) > 0
    except Exception:
        return ok_selected


def delete_view(drawing_path, view_name, save=True):
    """Delete a drawing view (wrong section, empty detail, stray projection).

    Without this, a single wrong `add_section_view` forces rebuilding the entire sheet.
    """
    try:
        from ..sw_session import byref_dispatch_null, error, get_sw

        doc = _open_drawing(drawing_path)
        before = [str(v.Name) for v in _iter_views(doc)
                  if getattr(v, "Type", None) != 1]
        if view_name not in before:
            return error("view_not_found",
                         "no model view named %r" % view_name,
                         "available: %s" % ", ".join(before))
        doc.ClearSelection2(True)
        if not _select_by_name(doc, view_name, "DRAWINGVIEW", byref_dispatch_null):
            return error("select_failed",
                         "SelectByID2 could not select %r as DRAWINGVIEW" % view_name)
        _call_method(doc, "EditDelete")
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        after = [str(v.Name) for v in _iter_views(doc)
                 if getattr(v, "Type", None) != 1]
        if len(after) >= len(before):
            return error("delete_failed",
                         "EditDelete did not remove the view",
                         "before=%s after=%s" % (before, after),
                         before=before, after=after)
        if save:
            _save_drawing(doc)
        # Deleting a section or detail view leaves its cut line / boundary circle on the
        # PARENT view as orphan geometry. Measured 2026-11-03 on 旋转轴 (the same part a
        # report used): after `delete_view("剖面视图 A-A")` the parent still shows the
        # vertical cut line with its arrows and "A", rendered in the PDF.
        #
        # It cannot be cleaned from the API here:
        #   * `IView.GetSection()` returns None even BEFORE the delete, so the object is
        #     never reachable
        #   * `GetSectionLineInfo2` still answers with the geometry (4 segments) — the line
        #     exists, it just has no object to select
        #   * SelectByID2 with SKETCHSEGMENT / SKETCHPOINT / DRAWINGVIEW / SECTIONLINE never
        #     selects anything, and the parent's annotation list holds only 装饰螺纹线
        #   * `IView.GetSectionLines` DOES return a tuple with one object (2026-11-04) — so
        #     the line is reachable in principle — but that object is an untyped CDispatch:
        #     every member answers `<unknown>.<name>` and `gencache.EnsureDispatch` refuses
        #     with "This COM object can not automate the makepy process". Reachable is not
        #     usable. This is the closest the API gets, and it is still not a delete path.
        #
        # So say it instead of pretending the sheet is clean. A leftover A-A next to a
        # deleted section is a drawing error a shop will reject, and the caller needs to
        # know it before sending the file.
        leftover = None
        lowered = str(view_name)
        if ("剖面" in lowered or "section" in lowered.lower()
                or "局部" in lowered or "detail" in lowered.lower()):
            leftover = {
                "kind": "section" if ("剖面" in lowered or "section" in lowered.lower())
                        else "detail",
                "parent_hint": "the view this one was cut/detailed from",
                "cannot_be_cleaned_by_api": True,
            }
        return drawing_ok(
            deleted=view_name, views_before=before, views_after=after,
            saved=bool(save),
            leftover_geometry=leftover,
            warning=("删除剖面/局部视图后，父视图上会残留剖切线/边界圈，"
                     "API 无法清除（IView.GetSection 恒 None；GetSectionLines 虽返回对象，"
                     "但该对象不可自动化；SelectByID2 选不中）。"
                     "交付前请在界面手工删除，或重建父视图。"
                     if leftover else None))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("delete_view_failed", str(exc))


def delete_annotation(drawing_path, name, save=True, position_m=None):
    """Delete one annotation by name (broken hole callout, unwanted GT, stray note).

    Uses `IAnnotation.Select(False)` then EditDelete. Measured on a real sheet:
    Select3 returned False and selected nothing; Select(False) selected exactly one object
    and EditDelete removed it. SelectByID2 with the reported name never worked either —
    `<unknown>`-style names like "细节项目34" are not resolvable ids.

    All four kinds were re-verified on 2026-10-31 against a report that claimed every type
    failed: note, geometric tolerance, surface finish and dimension all delete. Dimensions
    are the reason the success test now counts EVERY view instead of the sheet: deleting a
    dimension leaves the sheet's own annotation count untouched, so the old check answered
    `delete_failed` for a delete that had worked (sheet stayed 83, 工程图视图1 went 8 -> 7).

    `position_m` selects among repeated names — SolidWorks reuses D1/D2/D3 per sketch, so
    one sheet can hold eight annotations called "D1". Pass the `position_m` that
    `list_drawing_annotations` reported for the one you mean.
    """
    try:
        from ..sw_session import error

        doc = _open_drawing(drawing_path)
        doc.ClearSelection2(True)
        annotation, position = _find_annotation_by_name(doc, name, position_m)
        if annotation is None:
            return error("annotation_not_found",
                         "no annotation named %r%s on this sheet"
                         % (name, " at %r" % (position_m,) if position_m else ""),
                         "list names first with list_drawing_annotations")
        try:
            selected = annotation.Select(False)
        except Exception as exc:
            return error("select_failed", "IAnnotation.Select raised: %s" % str(exc)[:80])
        try:
            count = doc.SelectionManager.GetSelectedObjectCount2(-1)
        except Exception:
            count = None
        if not selected or count == 0:
            return error("select_failed",
                         "Select(False) did not select %r" % name,
                         "selected count=%r" % count)

        before = _annotation_total(doc)
        _call_method(doc, "EditDelete")
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        after = _annotation_total(doc)
        doc.ClearSelection2(True)
        removed = bool(before is not None and after is not None and after < before)
        if not removed and position is not None:
            # Fallback: a view can hand back a cached annotation list, leaving the total
            # unchanged. Re-find the exact object instead of trusting the count.
            again, again_position = _find_annotation_by_name(doc, name, position)
            if again is None or again_position is None:
                removed = True
            else:
                removed = not all(abs(again_position[i] - position[i]) <= 1e-3
                                  for i in range(2))
        if not removed and before is not None and after is not None:
            return error("delete_failed",
                         "EditDelete did not remove %r" % name,
                         "annotation count %s -> %s (all views)" % (before, after))
        if save:
            _save_drawing(doc)
        return drawing_ok(deleted=name, removed=removed,
                          annotations_before=before, annotations_after=after,
                          counted_scope="all_views",
                          position_m=[round(v, 4) for v in position] if position else None,
                          saved=bool(save))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("delete_annotation_failed", str(exc))


def move_annotation(drawing_path, name, x_m, y_m, save=True, position_m=None):
    """Move an annotation or dimension to a sheet position (metres).

    Dimensions pile up on the centre line when auto_dimension runs on a section view; there
    was no way to pull them apart, which made the output unreadable.

    `position_m` selects among repeated names (SolidWorks reuses D1/D2/D3 per sketch) —
    pass the position `list_drawing_annotations` reported. Verified on 2026-10-31 for a
    note: `move_annotation` returned the new position, which a v0.8.14 report said was
    impossible.
    """
    try:
        from ..sw_session import byref_dispatch_null, error

        doc = _open_drawing(drawing_path)
        doc.ClearSelection2(True)
        annotation, before = _find_annotation_by_name(doc, name, position_m)
        if annotation is None:
            return error("annotation_not_found",
                         "no annotation named %r on this sheet" % name,
                         "list names first with list_drawing_annotations")
        try:
            before = [round(float(v), 4) for v in annotation.GetPosition]
        except Exception:
            before = None
        try:
            annotation.SetPosition(float(x_m), float(y_m), 0)
        except Exception as exc:
            return error("move_failed", "SetPosition raised: %s" % str(exc)[:80],
                         "section/detail views keep projection alignment and refuse some moves")
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        after = None
        try:
            after = [round(float(v), 4) for v in annotation.GetPosition]
        except Exception:
            pass
        if save:
            _save_drawing(doc)
        # Report 2026-11-04 P1-4: a datum tag accepted only the X part of a move and the
        # receipt still said `applied: true`, because the test was `after != before` — the
        # annotation HAD moved (in X), so the flag was technically true and practically
        # useless. A caller reading `applied` concluded both axes landed and shipped a tag
        # sitting on top of a dimension.
        #
        # `applied` now means "the annotation is where you asked", which is what callers
        # actually mean. The per-axis detail says which half was ignored.
        requested = [round(float(x_m), 4), round(float(y_m), 4)]
        axis_honoured = None
        blocked_axes = []
        if after and len(after) >= 2:
            dx = abs(after[0] - requested[0])
            dy = abs(after[1] - requested[1])
            # 1 mm: the annotation's anchor is not its text centre, so a sub-millimetre
            # residual is expected and is not a refusal.
            axis_honoured = {"x": dx <= 0.001, "y": dy <= 0.001}
            blocked_axes = [axis for axis in ("x", "y") if not axis_honoured[axis]]
        applied = bool(after != before) and not blocked_axes if after else (after != before)
        warning = None
        if blocked_axes:
            warning = ("请求 %r，实际 %r — %s 方向未被采纳（基准/尺寸标签的锚点可能被几何"
                       "约束）。`applied` 只在两个方向都落地时才为 true。"
                       % (requested, after, "/".join(blocked_axes)))
        return drawing_ok(moved=name, position_before_m=before, position_after_m=after,
                          requested=requested,
                          axis_honoured=axis_honoured,
                          blocked_axes=blocked_axes,
                          applied=applied, saved=bool(save), warning=warning)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("move_annotation_failed", str(exc))


def set_dimension_position(drawing_path, name, x_m, y_m, save=True, position_m=None):
    """Move ONE dimension to a sheet position (metres), by its reported name.

    P0-2: `list_drawing_annotations` already gives every dimension a stable `name`; this is
    the single-dimension counterpart to `layout_dimensions`' bulk auto-placement. Unlike
    `move_annotation` it verifies the target really is a dimension (GetType 4), reports the
    dimension value, and refuses notes / GTOL / surface-finish symbols so a typo cannot
    silently move the wrong kind of annotation.

    `position_m` disambiguates repeated names — SolidWorks reuses D1/D2/D3 per sketch, and a
    40-dimension sheet can hold 19 of them called "D1". Pass the `position_m` that
    `list_drawing_annotations` reported. Without it the first match wins, which is exactly
    the wrong-dimension trap this tool otherwise exists to avoid.
    """
    try:
        import win32com.client as win

        doc = _open_drawing(drawing_path)
        doc.ClearSelection2(True)
        annotation, matched = _find_annotation_by_name(doc, name, position_m)
        if annotation is None:
            return error("annotation_not_found",
                         "no annotation named %r%s on this sheet"
                         % (name, " at %r" % (position_m,) if position_m else ""),
                         "List names first with list_drawing_annotations; a repeated name "
                         "needs position_m to tell the copies apart.")
        try:
            kind = int(annotation.GetType)
        except Exception:
            kind = None
        if kind != 4:
            return error("not_a_dimension",
                         "%r is annotation type %r, not a dimension (4)" % (name, kind),
                         "Use move_annotation for notes / GTOL / surface-finish symbols.")
        value_mm = None
        try:
            specific = win.Dispatch(annotation.GetSpecificAnnotation)
            dim = win.Dispatch(specific.GetDimension2(0))
            value_mm = round(float(dim.SystemValue) * 1000.0, 3)
        except Exception:
            pass
        try:
            before = [round(float(v), 4) for v in annotation.GetPosition]
        except Exception:
            before = None
        try:
            annotation.SetPosition(float(x_m), float(y_m), 0)
        except Exception as exc:
            return error("move_failed", "SetPosition raised: %s" % str(exc)[:80],
                         "Section/detail views keep projection alignment and refuse some moves.")
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        try:
            after = [round(float(v), 4) for v in annotation.GetPosition]
        except Exception:
            after = None
        if save:
            _save_drawing(doc)
        return drawing_ok(
            dimension=name, dimension_value_mm=value_mm,
            position_before_m=before, position_after_m=after,
            requested=[round(float(x_m), 4), round(float(y_m), 4)],
            matched_position_m=([round(v, 4) for v in matched] if matched else None),
            disambiguated_by_position=bool(position_m),
            applied=after != before, saved=bool(save))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("set_dimension_position_failed", str(exc))
