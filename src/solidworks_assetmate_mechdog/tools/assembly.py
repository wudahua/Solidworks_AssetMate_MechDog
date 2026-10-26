"""Read-only assembly inspection and preview export."""
from __future__ import annotations

import os

from ..sw_session import (
    SW_DOC_ASSEMBLY,
    error,
    get_components,
    ok,
    open_document,
    part_name,
    rgb_of,
)


def _component_info(comp):
    try:
        path = comp.GetPathName
    except Exception:
        path = None
    return {
        "component": comp.Name2,
        "part": part_name(comp.Name2),
        "path": path,
        "is_subassembly": bool(path and path.lower().endswith(".sldasm")),
        "rgb": rgb_of(comp.MaterialPropertyValues),
    }


def assembly_tree(assembly_path, max_depth=1, refresh=False):
    """Component tree. max_depth=1 is flat (fast); deeper levels use GetChildren."""
    try:
        doc = open_document(assembly_path, SW_DOC_ASSEMBLY)
        components = get_components(doc, refresh=refresh)  # one slow call, then memory only
        nodes = []
        for comp in components:
            info = _component_info(comp)
            if max_depth > 1 and info["is_subassembly"]:
                info["children"] = _children(comp, max_depth - 1)
            nodes.append(info)
        return ok(
            assembly=doc.GetTitle,
            path=doc.GetPathName,
            components=len(components),
            max_depth=max_depth,
            tree=nodes,
        )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("tree_failed", str(exc))


def _children(comp, depth):
    try:
        children = comp.GetChildren or []
    except Exception:
        return []
    result = []
    for child in children:
        info = _component_info(child)
        if depth > 1 and info["is_subassembly"]:
            info["children"] = _children(child, depth - 1)
        result.append(info)
    return result


def mass_properties(assembly_path):
    """Whole-assembly mass/volume/area in SI units (kg, m^3, m^2).

    Experimental: CreateMassProperty is unavailable under some pywin32
    bindings (docs/com-quirks.md section 5); a structured error is returned
    instead of a fabricated result.
    """
    try:
        doc = open_document(assembly_path, SW_DOC_ASSEMBLY)
        try:
            mass = doc.Extension.CreateMassProperty
            center = mass.CenterOfMass
            return ok(
                assembly=doc.GetTitle,
                units={"mass": "kg", "volume": "m^3", "surface_area": "m^2", "center_of_mass": "m"},
                mass=float(mass.Mass),
                volume=float(mass.Volume),
                surface_area=float(mass.SurfaceArea),
                center_of_mass=[float(v) for v in center] if center else None,
            )
        except Exception as exc:
            return error(
                "mass_unavailable", str(exc),
                "CreateMassProperty failed on this binding; see docs/com-quirks.md "
                "section 5. Read per-part properties instead of guessing.",
            )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))


def export_preview(assembly_path, output_path, size="1600x1200", view="*Isometric"):
    """Save a view image. SolidWorks SaveBMP always writes BMP; PNG needs Pillow."""
    try:
        doc = open_document(assembly_path, SW_DOC_ASSEMBLY)
        try:
            width, height = (int(x) for x in size.lower().split("x"))
        except Exception:
            width, height = 1600, 1200
        try:
            doc.ShowNamedView2(view, 7)
        except Exception:
            pass  # keep the current view if the named view is missing
        doc.ViewZoomtofit2()

        output_path = os.path.abspath(output_path)
        wants_png = output_path.lower().endswith(".png")
        bmp_path = output_path[:-4] + ".bmp" if wants_png else output_path
        if not doc.SaveBMP(bmp_path, width, height):
            return error("preview_failed", "SaveBMP returned failure: %s" % bmp_path)

        result = ok(assembly=doc.GetTitle, image=bmp_path, width=width, height=height, format="bmp")
        if wants_png:
            try:
                from PIL import Image
                Image.open(bmp_path).save(output_path)
                result.update(image=output_path, format="png")
            except ImportError:
                result["note"] = "Saved BMP instead. Install Pillow (pip install '.[png]') for PNG output."
        return result
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("preview_failed", str(exc))
