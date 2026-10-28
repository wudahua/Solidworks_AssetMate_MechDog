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

        def _fit_scale(candidate_box):
            width_need = candidate_box[0] + gap + candidate_box[2]
            height_need = candidate_box[1] + gap + candidate_box[2]
            return min(avail_w / max(width_need, 1e-6),
                       avail_h / max(height_need, 1e-6), 1.0)

        if chosen_scale is None and auto_fit and box and box[0] > 0 and box[1] > 0:
            chosen_scale = _standard_scale(_fit_scale(box))
        elif chosen_scale is None:
            chosen_scale = 1.0

        def _layout(scale_value):
            ww, hh, dd = (box or (0.15, 0.08, 0.08))
            ww, hh, dd = ww * scale_value, hh * scale_value, dd * scale_value
            # Two columns (front+top on the left, right+iso on the right), centred inside
            # the measured safe area — so the prediction matches where views really land.
            total_w = ww + gap + dd
            total_h = hh + gap + dd
            over = (total_w > avail_w + 1e-6) or (total_h > avail_h + 1e-6)
            x_start = safe_x0 + (avail_w - total_w) / 2
            y_start = safe_y0 + (avail_h - total_h) / 2
            c1 = x_start + ww / 2
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
                        o = list(candidate.GetOutline or [])
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
        result.update(
            safe_area_used={
                "x": [round(safe_x0, 4), round(safe_x1, 4)],
                "y": [round(safe_y0, 4), round(safe_y1, 4)],
                "source": safety.get("source"),
            },
            box_used_m=[round(v, 4) for v in box] if box else None,
            auto_scale_adjustments=auto_scale_adjustments,
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
            # A view without dimensions is normal: axonometric and detail views often
            # carry none. Measured on 100 real drawings, 79 of them would have tripped
            # this warning, so it now only fires when the SHEET has no dimensions at all.

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
        _save_drawing(doc)
        audit = drawing_audit(drawing_path)
        total = audit.get("total_dimensions") or 0
        notes = []
        if total > 30:
            notes.append(
                "本图 %d 个尺寸；100 张真实工程图实测平均 14 个（中位 11）。"
                "全量导入（include_unmarked=True）会把模型的每个驱动尺寸都倒到图上，"
                "制图是筛选而非搬运，建议关闭它或事后删除非关键尺寸。" % total)
        return ok(
            drawing=doc.GetTitle,
            inserted=count,
            layout=layout,
            include_unmarked=bool(include_unmarked),
            total_dimensions=total,
            advisory=notes,
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
                         "No hole edge at or near (%.3f, %.3f)" % (x, y),
                         "Point at a projected circular edge; use list_circular_edges "
                         "to get exact positions.")
        callout = doc.AddHoleCallout2(float(x), float(y), 0.0)
        if callout is None:
            return error(
                "callout_failed",
                "No hole edge found at (%.3f, %.3f) on the sheet" % (x, y),
                "Point at a hole's projected circle in a model view; use drawing_audit "
                "view outlines to locate one, or pass view_name.",
            )

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
            saved=bool(save),
            note="比对 model_match.ok；false 表示标注落在别处或模型无对应孔径。",
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
        planned, unmatched, skipped_reversed = [], [], []

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
        _save_drawing(doc)
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
        _save_drawing(doc)
        try:
            d_o = list(detail.GetOutline)
            box = {"width_m": round(d_o[2] - d_o[0], 4), "height_m": round(d_o[3] - d_o[1], 4)}
        except Exception:
            box = None
        final_scale = applied if applied is not None else float(detail.ScaleDecimal)
        locked = abs(float(final_scale) - float(scale)) > 0.01
        return drawing_ok(
            drawing=doc.GetTitle, detail=detail.Name, source_view=target.Name,
            label=str(label), boundary_center_m=[round(sheet_x, 4), round(sheet_y, 4)],
            boundary_radius_m=float(radius_m),
            scale=final_scale,
            requested_scale=float(scale),
            scale_locked=locked,
            scale_note=("局部视图默认继承父视图比例（UseParentScale），ScaleDecimal 需在保存后"
                        "二次设置才生效；scale_locked=true 表示本次仍按父视图比例，"
                        "需要不同比例时在 UI 中改或先保存重开再调一次。") if locked else None,
            outline=box,
        )
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
            try:
                dim.SetToleranceValues(float(upper), float(lower))
            except Exception:
                # Symmetric takes a single value on some builds.
                try:
                    dim.SetToleranceValues(float(upper), float(upper))
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
        return drawing_ok(applied=True, saved=True, planned=planned,
                          tolerance_type_readback=applied)
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
                    row["text"] = callout_text or None
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
                if kind in ("dimension", "hole_callout"):
                    try:
                        dim = ann.GetSpecificAnnotation.GetDimension2(0)
                        if dim is not None:
                            row["value_mm"] = round(float(dim.SystemValue) * 1000, 3)
                            try:
                                row["tolerance_type"] = int(dim.GetToleranceType)
                            except Exception:
                                pass
                    except Exception:
                        pass
                if kind == "note":
                    try:
                        text = ann.GetSpecificAnnotation.GetText
                        if isinstance(text, str):
                            row["text"] = text
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

        return drawing_ok(
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
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("annotations_failed", str(exc))


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

        def record(name, ok, detail=None, blocking=True):
            checks[name] = {"ok": bool(ok), "detail": detail}
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
        record("model_clean", not doubts,
               "; ".join(str(d.get("kind")) for d in doubts) or None)

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
                text = str(row.get("text") or "")
                if row.get("kind") == "dimension" and ("Ø" in text or "MOD-DIAM" in text.upper()):
                    diameter_dims += 1
            covered = holes_in_drawing + diameter_dims
            record("hole_callouts", covered > 0,
                   "图纸 %d 处孔标注 + %d 处 Ø 尺寸 / 模型 %d 个圆柱面候选"
                   % (holes_in_drawing, diameter_dims, hole_candidates),
                   "背面或被遮挡的孔 SolidWorks 不给标注，需人工补或换视图")
        record("tolerances_present", toleranced > 0,
               "%d 个尺寸带公差" % toleranced, blocking=False)

        # Attachment: GetAttachedEntities2 returns nothing for roughly half of the
        # dimensions that InsertModelAnnotations3 creates, and changing the import
        # parameters does not move that ratio. It is unclear whether those are really
        # floating or just unreadable, so by default this is a WARNING; strict mode
        # (process §6.1 literal reading) turns it into a blocker.
        att = verify_dimension_attachments(drawing_path)
        if att.get("ok"):
            orphan = int(att.get("orphan_count") or 0)
            dim_count = int(att.get("dimension_count") or 0)
            record("dimension_attachments", orphan == 0,
                   "%d/%d 个尺寸无关联实体" % (orphan, dim_count) if orphan
                   else "%d 个尺寸均已关联",
                   blocking=bool(strict_attachments))
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
                for kind in row["attached_kinds"]:
                    counts[kind] = counts.get(kind, 0) + 1
                if not types:
                    orphans.append({"view": view.Name, "name": row["name"],
                                    "value_mm": row.get("value_mm")})
                rows.append(row)

        total = len(rows)
        return drawing_ok(
            drawing=doc.GetTitle,
            dimension_count=total,
            attachment_summary=counts,
            orphan_count=len(orphans),
            orphans=orphans,
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


def list_circular_edges(drawing_path, view_name=None, min_diameter_mm=0.0):
    """Circular edges of a view, with sheet position and diameter.

    Performance: reading `edge.GetCurve` on every visible edge is the expensive part
    (260 edges took over 300 s). The chord length between an edge's two vertices is free
    to compute and is an upper bound on nothing — but a short chord means a small circle,
    so edges shorter than `min_diameter_mm * 0.5` are skipped before any curve is read.
    Pass a min_diameter_mm to get a large speed-up on hole-heavy views.

    `edge.GetCurve` returns a dispatch pointer that must be wrapped before reading
    IsCircle — calling it on the raw pointer fails silently and made an earlier version
    report "no circles" on a view holding 82 of them.
    """
    try:
        import math

        import win32com.client as win

        doc = _open_drawing(drawing_path)
        out = []
        for view in _iter_views(doc):
            if getattr(view, "Type", None) == 1:
                continue
            if view_name and view.Name != view_name:
                continue
            edges = [win.Dispatch(e) for e in (view.GetVisibleEntities2(view, 1) or ())]

            # Cheap pass: vertex positions only (no curve, no surface).
            endpoints = []
            for edge in edges:
                try:
                    a, b = edge.GetStartVertex, edge.GetEndVertex
                    if a is None or b is None:
                        continue
                    pa, pb = a.GetPoint, b.GetPoint
                    chord = math.dist(pa, pb)
                    endpoints.append((edge, pa, pb, chord))
                except Exception:
                    continue

            # min_diameter_mm is in MILLIMETRES but chord length is in METRES: convert,
            # otherwise min_diameter_mm=40 becomes a 20 m threshold and kills every circle.
            floor = max(float(min_diameter_mm) * 0.0005, 1e-4)
            rows = []
            for edge, pa, pb, chord in endpoints:
                if chord < floor:
                    continue
                try:
                    curve = win.Dispatch(edge.GetCurve)
                    if curve is None or not curve.IsCircle:
                        continue
                    diameter = round(float(list(curve.CircleParams)[6]) * 2000, 2)
                    if diameter < float(min_diameter_mm):
                        continue
                    rows.append((edge, pa, diameter))
                except Exception:
                    continue
            if not rows:
                continue
            axes = _projection_axes(view, [r[0] for r in rows])
            pos = list(view.Position)
            scale = float(view.ScaleDecimal)
            all_pts = [p for _, pa, pb, _ in endpoints for p in (pa, pb)]
            lo = [min(p[i] for p in all_pts) for i in range(3)]
            hi = [max(p[i] for p in all_pts) for i in range(3)]
            for edge, point, diameter in rows:
                out.append({
                    "view": view.Name,
                    "diameter_mm": diameter,
                    "x": pos[0] + (point[axes[0]] - (lo[axes[0]] + hi[axes[0]]) / 2) * scale,
                    "y": pos[1] + (point[axes[1]] - (lo[axes[1]] + hi[axes[1]]) / 2) * scale,
                })
        return drawing_ok(drawing=doc.GetTitle, count=len(out), circles=out)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("circle_scan_failed", str(exc))


def add_hole_callouts(drawing_path, view_name=None, min_diameter_mm=0.0,
                      max_callouts=40, budget_s=25.0, save=True):
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
            anchor_x = (outline[2] + 0.015) if len(outline) >= 4 else 0.30
            anchor_y = ((outline[1] + outline[3]) / 2.0) if len(outline) >= 4 else 0.20

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
                text_upper = str(text or "").upper()
                has_diameter = ("Ø" in str(text or "")) or ("MOD-DIAM" in text_upper) \
                    or text_upper.startswith("M") or " X " in text_upper
                radius_only = text_upper.startswith("R") and not has_diameter
                if radius_only or not has_diameter:
                    failed.append({"view": view.Name,
                                   "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                                   "reason": "not a hole callout (text=%r, likely an outer arc)"
                                             % (text,),
                                   "z": chosen["z"], "radius_only": True})
                    continue
                consecutive_nulls = 0
                created.append({
                    "view": view.Name,
                    "diameter_mm": round(chosen["radius_m"] * 2000, 2),
                    "is_hole_callout": is_hole,
                    "text": text,
                    "z": chosen["z"],
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
            # Judge the budget with the same clock that produced elapsed_s: a run whose
            # candidates were all rejected never entered the insert loop, so it reported
            # budget_exceeded=False while taking 55 s of a 45 s budget.
            budget_exceeded=bool(budget_exceeded or (time.time() - started) > float(budget_s)),
            elapsed_s=round(time.time() - started, 2),
            saved=bool(save),
            note=("孔标注用 IsHoleCallout 判定；同一孔的上下两条圆边只标一条（优先完整圆、"
                  "再取最靠观察者的一面）。hidden_side=true 表示该边在背面，SolidWorks 不给标注。"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("hole_callouts_failed", str(exc))


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

        matched, unmatched = [], []
        for row in ledger_rows:
            value = row.get("value_mm")
            if value is None:
                continue
            hit = next((v for v in sheet_values if abs(v - value) <= tol), None)
            if hit is None:
                unmatched.append(row)
            else:
                matched.append(dict(row, drawing_value_mm=hit))

        ledger_values = {round(r["value_mm"], 2) for r in ledger_rows if r.get("value_mm") is not None}
        sheet_only = [v for v in sheet_values
                      if not any(abs(v - lv) <= tol for lv in ledger_values)]

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
            coverage=round(len(matched) / len(ledger_rows), 3) if ledger_rows else None,
            unmatched_by_feature={
                k: sorted({round(v, 2) for v in vals if v is not None})
                for k, vals in sorted(by_feature.items())
            },
            skipped_non_length=len(skipped_non_length),
            skipped_oversize=len(skipped_oversize),
            oversize_examples=skipped_oversize[:6],
            skipped_examples=skipped_non_length[:8],
            unmatched_ledger=unmatched[:60],
            drawing_only_values=sheet_only[:40],
            note="unmatched_ledger 是「台账有、图纸没有对应值」的候选缺项；"
                 "drawing_only_values 是图上有但台账没有的（多为投影尺寸或参考尺寸）。"
                 "投影关系会让同一尺寸在不同视图取值不同，因此按值匹配只能作为待确认清单。",
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("reconcile_failed", str(exc))


# swSFSymType_e: 0 none, 1 machining required, 2 machining prohibited
SURFACE_FINISH_TYPES = {"machining": 1, "prohibited": 2, "none": 0}
# swLeaderStyle_e: 0 no leader, 1 straight, 2 bent
LEADER_STYLES = {"none": 0, "straight": 1, "bent": 2}


def add_surface_finish(drawing_path, x, y, roughness="3.2", sym_type="machining",
                       leader_type="none", lay_symbol=0, arrow_type=0,
                       prod_method="", sample_len="", min_roughness="",
                       roughness_spacing="", save=True):
    """Insert a NATIVE surface finish symbol (Ra value) at a sheet point.

    `IModelDocExtension.InsertSurfaceFinishSymbol3` takes FOURTEEN arguments — that count
    is why every earlier attempt failed with "non-optional parameter". Order:
      (SymType, LeaderType, X, Y, Z, LaySymbol, ArrowType, MachAllowance, OtherVals,
       ProdMethod, SampleLen, MaxRoughness, MinRoughness, RoughnessSpacing)

    x/y are sheet metres. roughness is the Ra value as text (e.g. "3.2").
    """
    try:
        doc = _open_drawing(drawing_path)
        sym = SURFACE_FINISH_TYPES.get(str(sym_type).lower(), sym_type)
        leader = LEADER_STYLES.get(str(leader_type).lower(), leader_type)
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
        return drawing_ok(
            created=True, saved=bool(save), roughness=str(roughness),
            sym_type=str(sym_type), leader_type=str(leader_type),
            position_m=[round(float(x), 4), round(float(y), 4)],
            readback=readback,
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("surface_finish_failed", str(exc))


# swGdtSym_e is not reachable through late binding on this build either; the symbol inside
# the frame is written with SolidWorks format codes instead (same mechanism as <MOD-DIAM>).
GTOL_SYMBOLS = {
    "position": "<MOD-POS-PAR>",
    "concentricity": "<MOD-CONC-PAR>",
    "perpendicularity": "<MOD-PERP-PAR>",
    "parallelism": "<MOD-PAR-PAR>",
    "flatness": "<MOD-FLAT-PAR>",
    "straightness": "<MOD-STR-PAR>",
    "circularity": "<MOD-CIRC-PAR>",
    "cylindricity": "<MOD-CYL-PAR>",
    "profile_line": "<MOD-PROF-LINE-PAR>",
    "profile_surface": "<MOD-PROF-SURF-PAR>",
    "angularity": "<MOD-ANG-PAR>",
    "symmetry": "<MOD-SYM-PAR>",
    "runout": "<MOD-RUN-PAR>",
    "total_runout": "<MOD-TRUN-PAR>",
    "none": "",
}


def _invoke_as_method(obj, name, *args):
    """Kept as a thin alias; the single implementation lives in sw_session.call_method."""
    from ..sw_session import call_method

    return call_method(obj, name, *args)


def add_geometric_tolerance(drawing_path, x, y, tolerance="0.05", datum="A",
                            symbol="none", view_name=None, edge_index=None,
                            second_datum="", third_datum="", save=True):
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

        code = GTOL_SYMBOLS.get(str(symbol).lower(), "")
        try:
            gtol.SetFrameSymbols2(1, code, False, "", False, "", "", "", "")
        except Exception as exc:
            return error("gtol_symbol_failed", str(exc)[:120])
        try:
            wrote = bool(gtol.SetFrameValues2(1, str(tolerance), "", str(datum),
                                              str(second_datum), str(third_datum)))
        except Exception as exc:
            return error("gtol_value_failed", str(exc)[:120])

        position_ok = None
        try:
            annotation = gtol.GetAnnotation
            position_ok = bool(annotation.SetPosition(float(x), float(y), 0.0))
        except Exception:
            annotation = None

        try:
            doc.EditRebuild3()
        except Exception:
            pass
        try:
            doc.GraphicsRedraw2()
        except Exception:
            pass

        # Read back field by field: one unsupported accessor must not blank the whole block.
        readback = {}
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
        return drawing_ok(
            created=True, saved=bool(save), attached_to=attached,
            tolerance=str(tolerance), datum=str(datum), symbol=str(symbol),
            values_written=wrote, position_set=position_ok, readback=readback,
            note=("形位公差是原生对象，可用 list_drawing_annotations 读出（kind="
                  "geometric_tolerance）。符号用格式码写入，symbol 传 none 时只有公差值与基准。"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("gtol_failed", str(exc))
