"""Recolor assembly components by part type (component-level appearance only).

Ported from the validated sw_recolor.py CLI. The three critical rules:
1. Write comp.MaterialPropertyValues (component level), never the document level.
2. The 9-element array must be VARIANT(VT_ARRAY | VT_R8).
3. Keep SolidWorks' default lighting components; only replace RGB.
"""
from __future__ import annotations

from ..sw_session import (
    SW_DOC_ASSEMBLY,
    double_array,
    error,
    get_components,
    ok,
    open_document,
    part_name,
    rgb_of,
)

# ambient, diffuse, specular, shininess, transparency, emission (SW defaults)
DEFAULT_LIGHTING = [1.0, 1.0, 0.88, 0.21, 0.0, 0.21]


def normalize_palette(raw):
    if not isinstance(raw, dict) or not raw:
        raise ValueError("palette must be a non-empty object: {part_name: [r,g,b]}")
    palette = {}
    for key, rgb in raw.items():
        if not (isinstance(rgb, (list, tuple)) and len(rgb) == 3):
            raise ValueError("bad color for %r: expected [r,g,b] in 0..1" % key)
        palette[str(key)] = [max(0.0, min(1.0, float(c))) for c in rgb]
    return palette


def _color_array(rgb):
    return double_array([float(c) for c in rgb] + DEFAULT_LIGHTING)


def describe_parts(components):
    """Group components in memory; never re-query SolidWorks per group."""
    groups = {}
    for comp in components:
        name = part_name(comp.Name2)
        group = groups.setdefault(name, {"part": name, "instances": 0, "rgb": None})
        group["instances"] += 1
        if group["rgb"] is None:
            group["rgb"] = rgb_of(comp.MaterialPropertyValues)
    return [groups[name] for name in sorted(groups)]


def recolor_assembly(assembly_path, palette, apply=False, save=True):
    """Dry-run by default. With apply=True, paints and returns previous colors for undo."""
    try:
        palette = normalize_palette(palette)
        doc = open_document(assembly_path, SW_DOC_ASSEMBLY)
        components = get_components(doc)
        if not components:
            return error("empty_assembly", "No components found in %s" % doc.GetTitle)

        parts = describe_parts(components)
        present = {p["part"] for p in parts}
        missing = sorted(set(palette) - present)
        matched = [c for c in components if part_name(c.Name2) in palette]
        base = {
            "assembly": doc.GetTitle,
            "path": doc.GetPathName,
            "components": len(components),
            "parts": parts,
            "matched_components": len(matched),
            "palette_keys_not_found": missing,
            "applied": False,
        }
        if not apply:
            base["dry_run"] = True
            base["message"] = "Nothing changed. Call again with apply=true to paint."
            return ok(**base)

        previous = {}
        painted = 0
        for comp in matched:
            name = comp.Name2
            if name not in previous:
                previous[name] = comp.MaterialPropertyValues
            comp.MaterialPropertyValues = _color_array(palette[part_name(name)])
            painted += 1

        try:
            doc.ForceRebuild3(False)
        except Exception:
            doc.EditRebuild3()
        if save:
            # pywin32 exposes SaveSilent as a property: bare access performs the
            # save and returns 0/1. Calling it raises "'int' object is not callable".
            doc.SaveSilent

        # Read-back check on a small sample. On some bindings (SolidWorks 2026 with a
        # gen_py cache present) MaterialPropertyValues reads back as white even though
        # the render and the saved file are correct — do not let callers trust
        # previous_colors blindly in that case.
        readback = []
        for comp in matched[:3]:
            name = part_name(comp.Name2)
            expected = palette[name]
            actual = rgb_of(comp.MaterialPropertyValues)
            matched_ok = bool(actual) and all(
                abs(a - b) <= 0.02 for a, b in zip(actual[:3], expected[:3])
            )
            readback.append({
                "component": comp.Name2,
                "requested": expected,
                "readback": actual,
                "matches": matched_ok,
            })
        readback_ok = all(item["matches"] for item in readback)

        base.update({
            "applied": True,
            "painted_components": painted,
            "saved": bool(save),
            "previous_colors": {
                name: rgb_of(values) for name, values in previous.items()
            },
            "readback_sample": readback,
            "readback_verified": readback_ok,
        })
        if not readback_ok:
            base["warning"] = (
                "MaterialPropertyValues does not read back the color just written, although "
                "the model renders and saves correctly (known on some bindings). Treat "
                "get_assembly_tree rgb and previous_colors as unreliable here; verify visually "
                "with export_preview instead of restoring colors from previous_colors."
            )
        return ok(**base)
    except (FileNotFoundError, ValueError) as exc:
        return error("bad_input", str(exc))
    except Exception as exc:
        return error(
            "recolor_failed", str(exc),
            "See docs/com-quirks.md sections 1 and 4 for common COM failures.",
        )
