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

import glob
import os
import time

from ..sw_session import (
    SW_DOC_DRAWING,
    SW_DOC_PART,
    error,
    ok,
    open_document,
    variant,
)

# `ok` is also used as a local variable name in some functions; keep the helper
# reachable under an unambiguous alias.
drawing_ok = ok

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


def _place_views(views, sheet_w, sheet_h, gap=0.022, safety=None, pad=0.018):
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

    # Reserve room for dimension text: IView.GetOutline covers geometry only, so a view
    # placed flush against the parts list has its dimensions drawn over the border.
    boxes = []
    for view in views:
        try:
            o = list(view.GetOutline or [])
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
    for col in columns:
        col_w = max((b[1] for b in col), default=0.0)
        y = sheet_h - top - pad / 2
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


def _standard_scale(fit):
    """Nearest standard drawing scale not larger than the available fit."""
    for candidate in (1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002):
        if candidate <= fit + 1e-9:
            return candidate
    return 0.002


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
):
    """Create a native .SLDDRW from a part/assembly, add model views, optionally save.

    views: any of front/back/left/right/top/bottom/isometric. View names are
    resolved against the model, so Chinese and English SolidWorks both work.
    scale=None with auto_fit picks a standard scale from the model's projected size
    (1:1, 1:2, 1:5 ...); pass an explicit scale to override.
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
        model = open_document(model_path, SW_DOC_PART if model_path.lower().endswith(".sldprt") else 2)

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
        box = None
        try:
            b = list(model.GetPartBox(True) or [])
            if len(b) >= 6:
                box = (abs(b[3] - b[0]), abs(b[4] - b[1]), abs(b[5] - b[2]))
        except Exception:
            box = None

        chosen_scale = float(scale) if scale else None
        if chosen_scale is None and auto_fit and box and box[0] > 0 and box[1] > 0:
            # Width must hold BOTH columns (front + side view) or the right column
            # lands outside the sheet; height must hold front + top views.
            fit_w = (sheet_w - 0.12) / max(box[0] + box[2] + 0.03, 1e-6)
            fit_h = (sheet_h - 0.10) / max(box[1] + box[2] + 0.03, 1e-6)
            fit = min(fit_w, fit_h, 1.0)
            chosen_scale = _standard_scale(fit)
        elif chosen_scale is None:
            chosen_scale = 1.0

        w, h, d = (box or (0.15, 0.08, 0.08))
        w, h, d = w * chosen_scale, h * chosen_scale, d * chosen_scale
        col1 = 0.05 + w / 2
        col2 = min(0.05 + w + 0.04 + d / 2, sheet_w - 0.03 - d / 2)
        front_y = 0.245 - h / 2
        top_y = max(0.055 + d / 2, front_y - h / 2 - 0.025 - d / 2)
        positions = {
            "front": (col1, front_y), "back": (col1, front_y),
            "top": (col1, top_y), "bottom": (col1, top_y),
            "right": (col2, front_y), "left": (col2, front_y),
            "isometric": (col2, top_y),
        }

        created, skipped, view_objects = [], [], []
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
            try:
                o = list(view.GetOutline or [])
                box_m = {"width_m": round(o[2] - o[0], 4), "height_m": round(o[3] - o[1], 4)}
            except Exception:
                box_m = None
            created.append({"view": key, "name": view.Name,
                            "scale": per_view_scale, "outline": box_m})
            view_objects.append(view)

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
            return error("no_views_created", "No drawing view could be created",
                         "Check the model is saved, not read-only, and has valid named views.",
                         skipped_views=skipped)

        if save and output_path:
            out = os.path.abspath(output_path)
            if os.path.splitext(out)[1].lower() != ".slddrw":
                out += ".SLDDRW"
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            drw.SaveAsSilent(out, False)
            result.update(saved=os.path.exists(out), drawing_path=out,
                          bytes=os.path.getsize(out) if os.path.exists(out) else 0)
        return ok(**result)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("create_drawing_failed", str(exc))


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
            return error("missing_dependency", "pypdfium2 is required to render PDF pages",
                         "Install with: pip install pypdfium2")
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
            row = {
                "name": name,
                "scale": scale,
                "width_m": round(width, 4) if width is not None else None,
                "height_m": round(height, 4) if height is not None else None,
                "dimensions": dims,
                "annotations": notes,
            }
            rows.append(row)
            if width is not None and (width < 0.001 or height < 0.001):
                warnings.append({"view": name, "issue": "blank_view"})
            if dims == 0:
                warnings.append({"view": name, "issue": "no_dimensions"})
            if width is not None and height is not None:
                try:
                    boxes.append((name, list(view.GetOutline)))
                except Exception:
                    pass
            if sheet_box and width is not None:
                try:
                    o = list(view.GetOutline)
                    if o[0] < sheet_box[0] or o[1] < sheet_box[1] or o[2] > sheet_box[2] or o[3] > sheet_box[3]:
                        warnings.append({
                            "view": name, "issue": "view_outside_sheet",
                            "hint": "reduce scale or move the view",
                        })
                except Exception:
                    pass

        if not model_views:
            warnings.append({"view": None, "issue": "no_model_views"})

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
            warnings=warnings,
            note="Evidence only: view choice, tolerances and manufacturability are not judged here.",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("audit_failed", str(exc))


def auto_dimension(drawing_path, all_views=True, include_unmarked=False, eliminate_duplicates=True):
    """Insert the model's driving dimensions into the drawing views.

    Equivalent to SolidWorks "Insert Model Items > Dimensions". Marked-for-drawing
    dimensions only by default; include_unmarked=True brings in every driving dim
    (more complete, noisier). Returns how many annotations were inserted.
    """
    try:
        doc = _open_drawing(drawing_path)
        # swImportModelItemsSource_e.FromEntireModel = 0
        # swInsertAnnotation_e: MarkedForDrawing = 32768, NotMarked = 524288
        types = 32768 | (524288 if include_unmarked else 0)
        doc.ClearSelection2(True)
        inserted = doc.InsertModelAnnotations3(
            0, types, bool(all_views), not bool(eliminate_duplicates), False, True
        )
        try:
            doc.EditRebuild3
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        count = len(inserted) if isinstance(inserted, (tuple, list)) else (1 if inserted else 0)
        # No repack: a section view is x-aligned with its parent and cannot be moved
        # sideways, so repacking would stack it on top of a neighbour.
        layout = None
        audit = drawing_audit(drawing_path)
        return ok(
            drawing=doc.GetTitle,
            inserted=count,
            layout=layout,
            include_unmarked=bool(include_unmarked),
            total_dimensions=audit.get("total_dimensions"),
            views=audit.get("views"),
            warnings=audit.get("warnings"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("auto_dimension_failed", str(exc))


def add_center_marks(drawing_path, include_slots=True, extended_lines=True):
    """Auto-insert center marks and centerlines on every hole/slot in every view.

    Requires activating each view first — drawing annotation ops act on the active
    view. GetCenterMarkCount under-reports on this binding, so `views_marked` (the
    API's own success count) is the reliable signal, not the count.
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


def add_hole_callout(drawing_path, x, y, view_name=None):
    """Insert a native hole callout at a sheet point (meters) that lies on a hole edge.

    Returns created + the annotation count delta in the target view. If the point is
    not on a hole's projected circle, SolidWorks returns null — we report that
    instead of pretending a callout exists.
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
        callout = doc.AddHoleCallout2(float(x), float(y), 0.0)
        if callout is None:
            return error(
                "callout_failed",
                "No hole edge found at (%.3f, %.3f) on the sheet" % (x, y),
                "Point at a hole's projected circle in a model view; use drawing_audit "
                "view outlines to locate one, or pass view_name.",
            )
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
        return ok(
            drawing=doc.GetTitle,
            created=True,
            view=target.Name if target is not None else None,
            annotations_before=before,
            annotations_after=after,
            note="Read the resulting text in the view to confirm diameter/depth/quantity.",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("hole_callout_failed", str(exc))


def ann_position(note):
    """Position of a note's annotation, best effort (None when unavailable)."""
    try:
        return list(note.GetAnnotation.GetPosition)
    except Exception:
        return []


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


def set_title_block(drawing_path, name=None, code=None, material=None, apply=False,
                   include_reversed=True):
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
        planned, unmatched, skipped_reversed = [], [], []

        wants = []
        if name is not None:
            wants.append(("name", "图样名称", str(name)))
        if code is not None:
            wants.append(("code", "图样代号", str(code)))
        if material is not None:
            wants.append(("material", "材质", str(material)))

        for field, marker, new_value in wants:
            hits = 0
            for note in notes:
                try:
                    text = note.GetText
                except Exception:
                    continue
                if not isinstance(text, str) or marker not in text:
                    continue
                angle = 0.0
                position = None
                try:
                    position = [round(v, 4) for v in ann_position(note)]
                except Exception:
                    position = None
                try:
                    angle = float(note.Angle)
                except Exception:
                    angle = 0.0
                if not include_reversed and abs(angle) > 0.01:
                    skipped_reversed.append({
                        "field": field, "text": text, "angle_deg": round(angle * 180 / 3.141592653589793, 1),
                    })
                    continue
                if field == "material":
                    before = text
                    after = text.replace("<未指定>", new_value).replace("<未指定 >", new_value)
                    if after == before:
                        after = "材质 %s" % new_value if "<" not in text else text.replace(
                            text[text.find("<"):text.find(">") + 1], new_value)
                else:
                    before = text
                    after = new_value
                planned.append({
                    "field": field, "before": before, "after": after, "note": note,
                    "angle_deg": round(angle * 180 / 3.141592653589793, 1),
                    "position": position,
                })
                hits += 1
            if hits == 0:
                # Fall back to the slot position: many GB templates leave these cells
                # with empty text, so there is nothing to match on.
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
                else:
                    unmatched.append({"field": field, "marker": marker})

        result = {
            "drawing": doc.GetTitle,
            "planned": [{k: v for k, v in p.items() if k != "note"} for p in planned],
            "unmatched": unmatched,
            "skipped_reversed": skipped_reversed,
            "applied": False,
        }
        if not apply:
            result["dry_run"] = True
            result["message"] = "Nothing changed. apply=true writes these title-block fields."
            return ok(**result)

        written = []
        for item in planned:
            try:
                item["note"].SetText(item["after"])
                written.append({"field": item["field"], "before": item["before"],
                                "after": item["after"], "ok": True})
            except Exception as exc:
                written.append({"field": item["field"], "ok": False, "error": str(exc)})
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
    """
    try:
        outline = list(view.GetOutline)
        width, height = outline[2] - outline[0], outline[3] - outline[1]
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
    if best is None or best[0] > tolerance:
        return None
    return best


def _view_at_point(doc, x, y):
    for view in _iter_views(doc):
        if getattr(view, "Type", None) == 1:
            continue
        try:
            o = list(view.GetOutline)
        except Exception:
            continue
        if o[0] <= x <= o[2] and o[1] <= y <= o[3]:
            return view
    return None


def list_drawing_edges(drawing_path, view_name=None):
    """List a view's visible edges with stable indices, so dimensions can be added
    without guessing sheet coordinates (which need a model->sheet transform this
    binding exposes unreliably).
    """
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
            return error("view_not_found", "No model view found (view_name=%r)" % view_name)
        import win32com.client as win

        rows = []
        for index, raw in enumerate(target.GetVisibleEntities2(target, 1) or ()):
            edge = win.Dispatch(raw)
            item = {"index": index}
            try:
                p1 = edge.GetStartVertex.GetPoint
                p2 = edge.GetEndVertex.GetPoint
                item["model_start"] = [round(v, 4) for v in p1]
                item["model_end"] = [round(v, 4) for v in p2]
            except Exception:
                pass
            rows.append(item)
        return ok(drawing=doc.GetTitle, view=target.Name, edge_count=len(rows), edges=rows)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("list_edges_failed", str(exc))


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
            if view is None or x is None or y is None:
                return error(
                    "segment_not_found",
                    "No edge selected and no sheet point given",
                    "Pass edge_index (+view_name) or sheet x/y in metres.",
                )
            # Fall back to projecting every visible edge's midpoint onto the sheet and
            # taking the nearest one — SelectByID2 cannot hit drawing edges by coordinates.
            hit = _nearest_edge_at_sheet_point(view, float(x), float(y), float(tolerance))
            if hit is None:
                return error(
                    "segment_not_found",
                    "No visible edge within %.1f mm of (%.4f, %.4f)"
                    % (float(tolerance) * 1000, float(x), float(y)),
                    "Increase tolerance or pass edge_index from list_drawing_edges.",
                )
            import win32com.client as win

            distance, midpoints = hit
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
    return {
        "left": frame_left + left_band,
        "right": sheet_w - frame_right + margin,
        "top": sheet_h - frame_top + margin,
        "bottom": bottom_band,
        "source": "template",
        "title_block_top": round(title_top, 4),
        "parts_list_right": round(list_right, 4),
    }


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


def add_section_view(drawing_path, view_name=None, orientation="horizontal",
                     offset_m=0.0, position=None, label="A", flip=False, depth=0.0):
    """Create a SECTION VIEW through a model view.

    The cut line is drawn in the view's own sketch — the view sketch coordinate system is
    `sketch = (paper - view.Position) / view.ScaleDecimal` (calibrated with Convert
    Entities). SolidWorks requires the line to live in that sketch and to be selected
    before CreateSectionViewAt5, otherwise it returns null.

    orientation: "horizontal" cuts left-right (shows the vertical section), "vertical"
    cuts top-bottom. offset_m shifts the line off the view centre (sheet metres).
    flip reverses the viewing direction (Options = 4). depth > 0 gives a partial section.
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
        half = (max(width, height) * 0.62) / scale
        offset = float(offset_m) / scale

        sm = doc.SketchManager
        sm.InsertSketch(True)
        try:
            if str(orientation).lower() == "vertical":
                sm.CreateLine(offset, -half, 0.0, offset, half, 0.0)
            else:
                sm.CreateLine(-half, offset, 0.0, half, offset, 0.0)
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
            )
        try:
            section.ScaleDecimal = scale
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        # Deliberately no repack here: repacking moves the section back into the parts
        # list whenever the sheet is tight. Call drawing_audit to check the result and
        # pass `position` to place it precisely.
        layout = None
        try:
            so = list(section.GetOutline)
            box = {"width_m": round(so[2] - so[0], 4), "height_m": round(so[3] - so[1], 4)}
        except Exception:
            box = None
        return drawing_ok(
            drawing=doc.GetTitle, section=section.Name, source_view=target.Name,
            label=str(label), orientation=str(orientation), flip=bool(flip),
            outline=box, layout=layout,
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("section_failed", str(exc))


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
    """
    try:
        import win32com.client as win

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
        scale = float(target.ScaleDecimal)
        outline = list(target.GetOutline)
        if center:
            sheet_x, sheet_y = float(center[0]), float(center[1])
        else:
            sheet_x = (outline[0] + outline[2]) / 2
            sheet_y = (outline[1] + outline[3]) / 2
        sketch_x = (sheet_x - pos[0]) / scale
        sketch_y = (sheet_y - pos[1]) / scale
        sketch_r = float(radius_m) / scale

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

        if position:
            place_x, place_y = float(position[0]), float(position[1])
        else:
            sheet_box = list(doc.GetFirstView.GetOutline)
            sheet_w, sheet_h = sheet_box[2] - sheet_box[0], sheet_box[3] - sheet_box[1]
            place_x = min(sheet_w - 0.09, outline[2] + 0.08)
            place_y = max(0.09, sheet_h * 0.30)

        detail = doc.CreateDetailViewAt4(
            place_x, place_y, 0.0, int(style), float(scale), float(scale), str(label),
            bool(show_label), bool(flip), False, int(shape), int(circle_segments),
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
        # after creation can be swallowed, so retry until the read-back matches.
        applied = _apply_detail_scale(doc, detail, scale)
        if applied is None or abs(applied - float(scale)) > 0.01:
            # A freshly created detail view refuses the scale until it is committed once.
            try:
                doc.SaveSilent
            except Exception:
                pass
            applied = _apply_detail_scale(doc, detail, scale)
            try:
                doc.SaveSilent
            except Exception:
                pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass
        try:
            d_o = list(detail.GetOutline)
            box = {"width_m": round(d_o[2] - d_o[0], 4), "height_m": round(d_o[3] - d_o[1], 4)}
        except Exception:
            box = None
        return drawing_ok(
            drawing=doc.GetTitle, detail=detail.Name, source_view=target.Name,
            label=str(label), boundary_center_m=[round(sheet_x, 4), round(sheet_y, 4)],
            boundary_radius_m=float(radius_m),
            scale=applied if applied is not None else float(detail.ScaleDecimal), outline=box,
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("detail_failed", str(exc))
