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

    Includes `mass_basis`: average density derived from mass/volume. SolidWorks
    falls back to 1000 kg/m3 for parts without material, so a value of exactly
    1000 is reported as a suspected placeholder rather than real engineering data.
    """
    try:
        doc = open_document(assembly_path, SW_DOC_ASSEMBLY)
        try:
            mass = doc.Extension.CreateMassProperty
            center = mass.CenterOfMass
            mass_kg = float(mass.Mass)
            volume_m3 = float(mass.Volume)
            density = mass_kg / volume_m3 if volume_m3 else None
            if density is not None and abs(density - 1000.0) < 1.0:
                basis = (
                    "suspected default density: mass/volume = %.1f kg/m3, which matches "
                    "SolidWorks' fallback for parts without material — verify material "
                    "assignments before using this mass" % density
                )
            elif density is not None:
                basis = "average density %.1f kg/m3 from mass/volume" % density
            else:
                basis = "unknown (volume is zero)"
            return ok(
                assembly=doc.GetTitle,
                units={"mass": "kg", "volume": "m^3", "surface_area": "m^2", "center_of_mass": "m"},
                mass=mass_kg,
                volume=volume_m3,
                surface_area=float(mass.SurfaceArea),
                center_of_mass=[float(v) for v in center] if center else None,
                mass_basis=basis,
                average_density_kg_m3=round(density, 3) if density is not None else None,
            )
        except Exception as exc:
            return error(
                "mass_unavailable", str(exc),
                "CreateMassProperty failed on this binding; see docs/com-quirks.md "
                "section 5. Read per-part properties instead of guessing.",
            )
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("mass_failed", str(exc))


def export_preview(assembly_path, output_path, size="1600x1200", view="*Isometric", keep_bmp=False):
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
                if not keep_bmp:
                    try:
                        os.remove(bmp_path)  # avoid leaving a multi-MB intermediate
                    except OSError:
                        pass
                result.update(image=output_path, format="png")
            except ImportError:
                result["note"] = "Saved BMP instead. Install Pillow (pip install '.[png]') for PNG output."
        return result
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error("preview_failed", str(exc))
