"""Sheet-layout operations: place views, edit dimensions, add notes/balloons/BOM.

Every one of these was previously "not available" and had to be written as raw COM by the
caller, which cost hours: `IView.Position` silently mis-writes unless dispatched as
PROPERTYPUT with a VARIANT array, `InsertNote` needs GetAnnotation called as a method, and
deleting a dimension needs SelectByID2 by position plus EditDelete.

Kept in a separate module because these are layout concerns, not geometry.
"""
from __future__ import annotations

import os

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


def set_view(drawing_path, view_name, x_m=None, y_m=None, scale=None, save=True):
    """Move and/or rescale one view. Coordinates are sheet metres.

    Position is written with DISPATCH_PROPERTYPUT + VARIANT(VT_ARRAY|VT_R8); a plain
    assignment silently lands the view somewhere else.
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

        applied = {"view": view_name, "position_m": None, "scale": None}
        if x_m is not None and y_m is not None:
            _prop_put(target, "Position",
                      VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                              [float(x_m), float(y_m)]))
            try:
                applied["position_m"] = [round(v, 4) for v in target.Position]
            except Exception:
                pass
        if scale is not None:
            _prop_put(target, "ScaleDecimal", float(scale))
            try:
                applied["scale"] = float(target.ScaleDecimal)
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
        return drawing_ok(drawing=doc.GetTitle, **applied)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("set_view_failed", str(exc))


def place_views(drawing_path, placements, save=True):
    """Place several views at once: [{"view": name, "x": , "y": , "scale": }] (metres)."""
    results = []
    for item in placements or []:
        results.append(set_view(drawing_path, item.get("view"),
                                x_m=item.get("x"), y_m=item.get("y"),
                                scale=item.get("scale"), save=False))
    if save:
        try:
            _save_drawing(_open_drawing(drawing_path))
        except Exception:
            pass
    ok_count = sum(1 for r in results if r.get("ok"))
    return drawing_ok(placed=ok_count, total=len(results), results=results)


def delete_dimensions(drawing_path, keep_values=None, view_name=None,
                      keep_tolerance_mm=0.05, save=True):
    """Delete dimensions whose value is not in keep_values.

    SolidWorks has no bulk delete: each dimension is selected by position and removed with
    EditDelete, which costs roughly 15 s per dimension. This batches the loop and reports
    what was removed, so a 49-dimension sheet can be reduced to the planned shortlist
    without hand-written COM.
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
                    specific = ann.GetSpecificAnnotation
                    dim = specific.GetDimension2(0)
                    if dim is None:
                        continue
                    value = round(float(dim.SystemValue) * 1000, 3)
                    rows.append((view, ann, value))
                except Exception:
                    continue

        for view, ann, value in rows:
            if keep and any(abs(value - target) <= keep_tolerance_mm for target in keep):
                kept.append({"value_mm": value, "name": str(ann.GetName)})
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
        return drawing_ok(
            drawing=doc.GetTitle,
            removed=len(removed), kept=len(kept), failed=len(failed),
            removed_items=removed[:40], kept_values=[k["value_mm"] for k in kept][:40],
            failed_items=failed[:10],
            note="按值保留（默认容差 0.05 mm）。SolidWorks 无批量删除接口，逐条约 15 s。",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("delete_dimensions_failed", str(exc))


def add_note(drawing_path, text, x_m, y_m, height_mm=None, bold=False, save=True):
    """Add a drawing note (技术要求 lines). x/y are sheet metres.

    `InsertNote` returns an INote; positioning it needs GetAnnotation invoked as a METHOD —
    pywin32 otherwise dispatches it as PROPERTYGET and the call fails.
    """
    try:
        doc = _open_drawing(drawing_path)
        doc.ClearSelection2(True)
        created = doc.InsertNote(str(text))
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
        return drawing_ok(drawing=doc.GetTitle, note=name, text=str(text),
                          position_m=[round(float(x_m), 4), round(float(y_m), 4)])
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


def add_sheet_line(drawing_path, x1_m, y1_m, x2_m, y2_m, save=True):
    """Draw a line in TRUE sheet space.

    `SketchManager.CreateLine` on a drawing interprets coordinates as model space and
    scales them by the view scale: asking for a line at (236, 62) produced a fan of
    diagonals across the sheet. This activates the sheet view first, then converts the
    requested sheet point through the sheet's own transform, which is the only way to get
    the line where it was asked for.
    """
    try:
        doc = _open_drawing(drawing_path)
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
        # Sheet space -> sketch space: a drawing sketch is placed at the sheet origin, so
        # sheet metres are already the right unit. Passing millimetres is what produced the
        # scaled fan of lines.
        sketch.CreateLine(float(x1_m), float(y1_m), 0.0,
                          float(x2_m), float(y2_m), 0.0)
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
        return drawing_ok(drawing=doc.GetTitle,
                          line_m=[[round(float(x1_m), 4), round(float(y1_m), 4)],
                                  [round(float(x2_m), 4), round(float(y2_m), 4)]],
                          note="坐标为图纸空间米；图纸草图不接受毫米。")
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("sheet_line_failed", str(exc))


def add_bom_table(drawing_path, view_name=None, configuration="Default",
                  template=None, save=True):
    """Insert a POPULATED native TopLevelOnly BOM into an assembly drawing.

    Verified working 2026-10-29 on SW2024 SP4: 10 rows x 4 columns with a quantity
    column, stable across save/reopen.

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

        configuration = configuration or getattr(target, "ReferencedConfiguration", "Default")

        result = populate_top_level_bom(
            doc, target, template,
            configuration=configuration or "Default",
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


def _find_annotation_by_name(doc, name):
    """Return (annotation, position_m) for an annotation with this reported name."""
    import win32com.client
    try:
        sheet = doc.GetFirstView
        for raw in (call_method(sheet, "GetAnnotations") or ()):
            annotation = win32com.client.Dispatch(raw)
            try:
                if str(annotation.GetName) == name:
                    position = [float(v) for v in annotation.GetPosition]
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
        return drawing_ok(deleted=view_name, views_before=before, views_after=after,
                          saved=bool(save))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("delete_view_failed", str(exc))


def delete_annotation(drawing_path, name, save=True):
    """Delete one annotation by name (broken hole callout, unwanted GT, stray note)."""
    try:
        from ..sw_session import byref_dispatch_null, error

        doc = _open_drawing(drawing_path)
        doc.ClearSelection2(True)

        # Strategy 1: select at the annotation's own position. This is what works —
        # SelectByID2 with the reported name fails for notes/GT/surface symbols.
        annotation, position = _find_annotation_by_name(doc, name)
        selected_type = None
        if position is not None:
            try:
                hit = doc.Extension.SelectByRay(position[0], position[1], position[2],
                                                0.0, 0.0, -1.0, 0.002, 2,
                                                False, 0, 0)
                if hit and doc.SelectionManager.GetSelectedObjectCount2(-1) > 0:
                    selected_type = "by_position"
            except Exception:
                pass
        if selected_type is None:
            # Strategy 2: fall back to type-based ID selection for dimensions.
            for candidate in DELETABLE_SELECT_TYPES:
                if _select_by_name(doc, name, candidate, byref_dispatch_null):
                    selected_type = candidate
                    break
        if selected_type is None:
            return error("annotation_not_found",
                         "no annotation named %r could be selected" % name,
                         "tried selecting at its reported position and by ID with types: %s. "
                         "List names first with list_drawing_annotations."
                         % ", ".join(DELETABLE_SELECT_TYPES))
        _call_method(doc, "EditDelete")
        try:
            _call_method(doc, "EditRebuild3")
        except Exception:
            pass
        doc.ClearSelection2(True)
        # EditDelete returns None on success, so there is nothing to trust; the caller can
        # re-run list_drawing_annotations to confirm the name is gone.
        if save:
            _save_drawing(doc)
        return drawing_ok(deleted=name, selected_as=selected_type, saved=bool(save))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("delete_annotation_failed", str(exc))


def move_annotation(drawing_path, name, x_m, y_m, save=True):
    """Move an annotation or dimension to a sheet position (metres).

    Dimensions pile up on the centre line when auto_dimension runs on a section view; there
    was no way to pull them apart, which made the output unreadable.
    """
    try:
        from ..sw_session import byref_dispatch_null, error

        doc = _open_drawing(drawing_path)
        doc.ClearSelection2(True)
        annotation, before = _find_annotation_by_name(doc, name)
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
        return drawing_ok(moved=name, position_before_m=before, position_after_m=after,
                          requested=[round(float(x_m), 4), round(float(y_m), 4)],
                          applied=after != before, saved=bool(save))
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("move_annotation_failed", str(exc))
