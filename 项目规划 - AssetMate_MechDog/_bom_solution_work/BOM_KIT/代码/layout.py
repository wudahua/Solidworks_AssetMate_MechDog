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


def _call_method(obj, name, *args):
    """Thin alias; the single implementation lives in sw_session.call_method."""
    from ..sw_session import call_method

    return call_method(obj, name, *args)


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
            doc.EditRebuild3
        except Exception:
            pass
        doc.GraphicsRedraw2
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
            doc.EditRebuild3
        except Exception:
            pass
        doc.GraphicsRedraw2
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
            doc.EditRebuild3
        except Exception:
            pass
        doc.GraphicsRedraw2
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
        ballon = None
        if item_number is not None:
            try:
                ballon = doc.InsertBOMBalloon2(item_number, 0, 0, 0, 0.0)
            except Exception:
                ballon = None
        if ballon is None:
            ballon = doc.InsertBalloon(0, 0, 0, float(x_m), float(y_m), float(leader_x_m or x_m),
                                       float(leader_y_m or y_m))
        if ballon is None:
            return error("balloon_failed", "InsertBalloon returned null",
                         "Balloons need a view with components; activate the view first.")
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
            doc.EditRebuild3
        except Exception:
            pass
        doc.GraphicsRedraw2
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
            doc.EditRebuild3
        except Exception:
            pass
        doc.GraphicsRedraw2
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


def add_bom_table(drawing_path, view_name=None, bom_type=2, anchor_type=1,
                  configuration="", template=None, save=True):
    """Insert a native BOM (materials list) table into an assembly drawing.

    Two findings make this work, both verified on 2026-10-29:
      * `anchor_type` must be 1 (TopLeft). Passing 0 — an invalid member — made every
        call return None. This single value was the difference between "impossible" and
        "works".
      * `bom_type` must be 2 (TopLevelOnly). `swBomType_PartsOnly = 1`, which is what I
        passed for hours while believing it meant TopLevelOnly.

    With those corrected the call returns a BomTableAnnotation, the sheet gains one table
    and `BomFeature` is readable. What it still does NOT do is populate the rows: the
    table arrives with its header only (`RowCount == 1`). `BomFeature.SetConfigurations`
    — the documented way to attach configurations for a top-level-only BOM — rejects every
    argument form (list / tuple / str / VARIANT arrays) with DISP_E_TYPEMISMATCH, and
    `Configuration` assignment does not trigger a re-read either.

    So: the table object is real and native, but empty. Reported honestly rather than
    "success".
    """
    try:
        import win32com.client as win

        from ..sw_session import call_method, get_sw

        doc = _open_drawing(drawing_path)
        sw = get_sw()
        if template is None:
            # GetExecutablePath is a PROPERTY (returns str); calling it raises
            # "'str' object is not callable".
            template = os.path.join(str(sw.GetExecutablePath), "lang",
                                    "chinese-simplified", "bom-standard.sldbomtbt")
        if not os.path.exists(template):
            return error("template_missing", "BOM template not found: %s" % template)

        views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
        if not views:
            return error("no_views", "drawing has no model views")
        target = views[0]
        if view_name:
            for candidate in views:
                if candidate.Name == view_name:
                    target = candidate
                    break

        def table_rows():
            rows = []
            try:
                for raw in (doc.GetFirstView.GetTableAnnotations or ()):
                    table = win.Dispatch(raw)
                    rows.append(int(table.RowCount))
            except Exception:
                pass
            return rows

        # The referenced model must be present, BUT opening it steals the active document.
        # Inserting afterwards with a stale `view` returns None silently — that is exactly
        # why this wrapper failed while the same code in a script succeeded (the script
        # happened to finish with the drawing active).
        modeled = None
        try:
            referenced = call_method(target, "ReferencedDocument")
            if referenced is not None:
                import win32com.client as _win
                from ..sw_session import open_document
                modeled = str(_win.Dispatch(referenced).GetPathName)
                open_document(modeled)
        except Exception:
            pass

        # Re-establish the drawing context AFTER any model access, then re-acquire the
        # view from the sheet instead of reusing the pre-activation object.
        doc = _open_drawing(drawing_path)
        try:
            sheet = doc.GetCurrentSheet
            doc.ActivateSheet(str(sheet.GetName))
            views = [v for v in (sheet.GetViews or ())
                     if getattr(v, "Type", None) != 1]
        except Exception:
            views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
        if not views:
            return error("no_views", "drawing has no model views after reactivation")
        target = views[0]
        if view_name:
            for candidate in views:
                if candidate.Name == view_name:
                    target = candidate
                    break
        try:
            doc.ActivateView(target.Name)
        except Exception:
            pass
        doc.ClearSelection2(True)

        # Hard assertion: the drawing must be the ACTIVE document, not merely open.
        active_title = None
        try:
            active = sw.ActiveDoc
            active_title = str(active.GetTitle) if active is not None else None
        except Exception:
            pass
        doc_title = str(doc.GetTitle)
        if active_title and active_title != doc_title:
            return error("drawing_not_active",
                         "active document is %r, expected %r" % (active_title, doc_title),
                         "Inserting into a non-active drawing returns None silently.")

        before = table_rows()

        table = target.InsertBomTable4(
            False,                       # UseAnchorPoint: place at X/Y
            0.24, 0.19,
            int(anchor_type),            # 1 = TopLeft (0 is invalid and returns None)
            int(bom_type),               # 2 = TopLevelOnly (1 is PartsOnly)
            str(configuration),          # TopLevelOnly takes '' here
            str(template),
            False,                       # Hidden
            0,                           # IndentedNumberingType
            False,                       # DetailedCutList
        )
        # EditRebuild3 is a property on this binding: `doc.EditRebuild3()` raises
        # "'bool' object is not callable". Call it through call_method instead.
        try:
            call_method(doc, "ForceRebuild3", False)
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass

        after = table_rows()
        created = len(after) > len(before)

        feature_ok = None
        rows = None
        if table is not None:
            try:
                rows = int(win.Dispatch(table).RowCount)
                feature_ok = win.Dispatch(table).BomFeature is not None
            except Exception:
                pass
        if save:
            _save_drawing(doc)

        if not created:
            return error("bom_table_not_created",
                         "InsertBomTable4 returned %s and the sheet gained no table"
                         % ("None" if table is None else "an object"),
                         "anchor_type=%s bom_type=%s configuration=%r"
                         % (anchor_type, bom_type, configuration))
        return drawing_ok(
            drawing=doc.GetTitle, created=True, rows=rows,
            has_bom_feature=feature_ok, table_count=len(after), saved=bool(save),
            populated=bool(rows and rows > 1),
            note=("表格是原生 BOM（BomFeature 可取），但**行数据未填充**（RowCount=1 只有表头）。"
                  "SetConfigurations 各种参数形式均类型不匹配，Configuration 赋值也不触发重读。"
                  "anchor_type 必须为 1、bom_type 必须为 2，否则调用直接返回 None。"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("bom_table_failed", str(exc))


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
            doc.EditRebuild3
        except Exception:
            pass
        doc.GraphicsRedraw2
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

