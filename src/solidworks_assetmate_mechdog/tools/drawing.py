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


def _place_views(views, sheet_w, sheet_h, gap=0.022,
                 left=0.034, right=0.020, top=0.020, bottom=0.060):
    """Repack views using their REAL projected outlines so nothing overlaps.

    The bounding-box estimate used before creation is only good enough to pick a
    scale; an isometric view projects wider than the box, which is how views ended
    up overlapping. Here we read each view's actual outline and pack them into one
    or two columns, then move them with IView.Position (needs a VT_ARRAY|VT_R8
    VARIANT — a plain tuple is silently misread as (0, y)).
    """
    import pythoncom
    from win32com.client import VARIANT

    boxes = []
    for view in views:
        try:
            o = list(view.GetOutline or [])
            boxes.append([view, o[2] - o[0], o[3] - o[1]])
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
    x = left
    for col in columns:
        col_w = max((b[1] for b in col), default=0.0)
        y = sheet_h - top
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
        result = _place_views(views, o[2] - o[0], o[3] - o[1])
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

        template = os.path.abspath(template) if template else _find_template("A3")
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
            try:
                view.ScaleDecimal = float(chosen_scale)
            except Exception:
                pass
            try:
                o = list(view.GetOutline or [])
                box_m = {"width_m": round(o[2] - o[0], 4), "height_m": round(o[3] - o[1], 4)}
            except Exception:
                box_m = None
            created.append({"view": key, "name": view.Name,
                            "scale": float(chosen_scale), "outline": box_m})
            view_objects.append(view)

        layout = _place_views(view_objects, sheet_w, sheet_h)
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
        layout = _relayout(doc)
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
        layout = _relayout(doc)
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
