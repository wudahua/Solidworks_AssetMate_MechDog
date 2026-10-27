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

        created, skipped = [], []
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
