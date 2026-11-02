"""Add a `description` to every MCP tool parameter.

Why this exists: `inputSchema.properties[*].description` was empty for all 287
parameters across 59 tools. The only place a parameter could be explained was the
tool's prose docstring, so any new argument that was not mentioned there was, to a
model reading `tools/list`, indistinguishable from a parameter that does not exist.
A report found three such cases in one release (`sheet_start`, `cut_line_ratio`,
`hatch_missing`).

This script is a one-off rewrite, kept in the repo so the mapping is reviewable and
re-runnable. It splices `Annotated[T, Field(description=...)]` around every annotated
parameter, using AST node offsets so nothing else in the file moves.

Run from the repo root:

    python tools/annotate_tool_params.py          # rewrite src/.../server.py
    python tools/annotate_tool_params.py --check   # exit 1 if anything is missing

After it runs, `tests/static_check.py` enforces that no parameter goes without a
description again.
"""

import argparse
import ast
import io
import os
import sys

TARGET = os.path.join("src", "solidworks_assetmate_mechdog", "server.py")

# One entry per unique parameter name. Where the same name means different things in
# different tools, the wording is deliberately general enough to be true for all of them.
DESCRIPTIONS = {
    # ---- paths -------------------------------------------------------------
    "drawing_path": "Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.",
    "model_path": "Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.",
    "part_path": "Single part file (.SLDPRT), absolute path.",
    "part_paths": "List of part files to rename; each is opened in turn.",
    "assembly_path": "Assembly file (.SLDASM), absolute path.",
    "output_path": "Where to write the result. Extension decides the format when fmt is omitted.",
    "output_dir": "Directory for generated files; created if missing.",
    "pdf_path": "PDF to rasterise into PNG pages.",
    "csv_path": "Where to write the BOM as CSV.",
    "html_path": "Where to write the survey report (HTML).",
    "paths": "Part file paths, one entry per item; each is opened and written.",

    # ---- common switches ---------------------------------------------------
    "save": "Save the document after the change (default True). False leaves it in memory for inspection.",
    "apply": "False (default) is a dry run that reports what WOULD change; True actually writes.",
    "dry_run": "True reports the plan without touching the document.",
    "confirm": "Must be True to actually delete; without it the call only reports what it would delete.",
    "refresh": "Re-read the model from disk instead of using cached data.",
    "resolve_lightweight": "Load lightweight components before reading the tree (slower, complete).",
    "config": "Configuration name; null means the active configuration.",
    "config_name": "Configuration to use; null means the active one.",
    "configuration": "BOM configuration name; null means the active configuration.",
    "database": "Material database to read from (e.g. solidworks materials); null uses the default.",

    # ---- views -------------------------------------------------------------
    "view_name": "Model view to target. null means the first model view (the sheet itself is skipped).",
    "views": "Which standard views to create, in order, e.g. ['front', 'right', 'isometric'].",
    "position": "Sheet position [x, y] in metres for the new view; null lets the tool choose.",
    "view_positions": "Per-view sheet positions [x, y] in metres, keyed by view name.",
    "view_scales": "Per-view scale, keyed by view name; overrides the sheet scale.",
    "position_m": "Exact sheet position [x, y] in metres. Required when several annotations share a name (SolidWorks reuses D1/D2).",
    "placements": "Explicit [x, y] positions, one per view, in sheet metres.",
    "offset_m": "Shift the cut line off the view centre, in sheet metres.",
    "offset_x": "Offset applied to the dimension text, in metres (positive = right).",
    "offset_y": "Offset applied to the dimension text, in metres (negative = below the line).",

    # ---- sizes and coordinates --------------------------------------------
    "x_m": "Sheet X in metres.",
    "y_m": "Sheet Y in metres.",
    "x": "Sheet X in metres.",
    "y": "Sheet Y in metres.",
    "x1_m": "Sheet X of the line start, in metres.",
    "y1_m": "Sheet Y of the line start, in metres.",
    "x2_m": "Sheet X of the line end, in metres.",
    "y2_m": "Sheet Y of the line end, in metres.",
    "leader_x_m": "Sheet X of the leader tip, in metres.",
    "leader_y_m": "Sheet Y of the leader tip, in metres.",
    "edge_index": "Index into list_drawing_edges for this view; lets you attach to an edge without sheet coordinates.",
    "center": "Sheet centre [x, y] of the detail boundary, in metres.",
    "radius_m": "Radius of the detail boundary, in metres.",
    "height_mm": "Note text height in millimetres (3.5 is the GB default).",
    "size": "Output image size in pixels, e.g. [1600, 1200].",
    "scale": "View or sheet scale as a decimal (2.0 means 2:1).",
    "paper": "Sheet size, e.g. 'A3' or 'A4'; drives the template and the usable area.",
    "template": "Drawing template path; null uses the built-in GB template.",

    # ---- tolerances --------------------------------------------------------
    "tolerance": "Tolerance value. For add_drawing_dimension it is a length in metres; for add_geometric_tolerance it is the frame value as text.",
    "tolerance_mm": "Matching tolerance in millimetres when reconciling ledger values.",
    "tolerance_type": "SolidWorks tolerance type code (e.g. 4 = symmetric/bilateral).",
    "upper": "Upper deviation, in metres.",
    "lower": "Lower deviation, in metres.",
    "value_mm": "Driving dimension value in millimetres to match against.",
    "value": "Explicit dimension value in metres. Omit to keep the measured value (safer: a value here drives the model).",
    "keep_tolerance_mm": "Two values closer than this are treated as the same dimension.",
    "keep_values": "Values to keep when deleting; everything else matching the filter goes.",

    # ---- annotations -------------------------------------------------------
    "text": "Note body. Ø ± ° are rewritten to SolidWorks format codes so DWG/DXF keeps them legible.",
    "name": "Annotation name exactly as reported by list_drawing_annotations.",
    "label": "Letter identifying this item: the view letter for a section/detail (e.g. 'A' in A-A) or the datum letter for a datum feature symbol.",
    "filled_triangle": "True draws a filled datum triangle, False hollow, null keeps the document default.",
    "symbol": "Geometric characteristic name, e.g. 'position', 'cylindricity'. Requires the 2022+ frame path.",
    "datum": "Primary datum letter referenced by the frame.",
    "second_datum": "Secondary datum letter, or '' for none.",
    "third_datum": "Tertiary datum letter, or '' for none.",
    "bold": "Render the note in bold.",
    "avoid_collisions": "Walk the note down until it clears every model view.",

    # ---- section / detail --------------------------------------------------
    "orientation": "'horizontal' cuts left-right, 'vertical' cuts top-bottom.",
    "flip": "Reverse the viewing direction of the section.",
    "depth": "Partial-section depth in metres; 0 gives a full section.",
    "cut_plane_normal": "Model-space plane normal; pass together with cut_plane_origin to cut an explicit plane.",
    "cut_plane_origin": "A point on the cut plane, in model metres.",
    "cut_line_ratio": "Cut-line length as a fraction of the view; clamped to half the SHORT edge so arrows stay on the geometry. Lower it for very flat views.",
    "verify_cut": "Check the result and return section_empty / section_suspicious / hatch_missing instead of a silent wrong cut.",
    "style": "Detail-view boundary style code.",
    "shape": "Detail-view boundary shape (0 = circle).",
    "show_label": "Show the detail label next to the view.",
    "circle_segments": "Segments used to approximate a circular boundary; higher is smoother.",
    "hide_bend_lines": "Hide bend lines in the flat-pattern view.",

    # ---- dimensions --------------------------------------------------------
    "all_views": "Insert into every model view. False inserts into the ACTIVE view only and activates one for you.",
    "include_unmarked": "Also bring in dimensions not marked for drawing (more complete, noisier).",
    "eliminate_duplicates": "Drop duplicate dimensions by full name after insertion.",
    "dimension_filter": "Only insert dimensions whose full name appears here; typically plan_drawing's keep_dimensions.",
    "place_outside": "After inserting, push every dimension just outside its view instead of leaving them inside the geometry.",
    "outside_gap_m": "Distance from the view outline to the first dimension, in metres.",
    "outside_step_m": "Spacing between stacked dimensions, in metres.",
    "prune_to_plan": "Delete inserted dimensions that the plan did not ask for.",
    "layout_dimensions": "Reposition dimensions to avoid overlaps after insertion.",
    "min_gap_m": "Minimum gap to keep between dimension texts, in metres.",
    "cluster_tol_m": "Dimensions within this distance are treated as one cluster, in metres.",

    # ---- surface finish ----------------------------------------------------
    "roughness": "Ra value as text, e.g. '1.6'.",
    "roughness_spacing": "Spacing between multiple roughness symbols on the same edge, in metres.",
    "sym_type": "Surface-finish symbol type code.",
    "prod_method": "Production method code (machined, cast, etc.).",
    "lay_symbol": "Lay (texture direction) symbol code.",
    "leader_type": "Leader style code.",
    "arrow_type": "Arrowhead style code.",
    "min_roughness": "Lowest Ra value to keep when auditing.",
    "sample_len": "Sampling length shown with the symbol.",
    "verify_render": "Export and rasterise to prove the symbol actually appears on the sheet.",
    "verify_dwg": "Also export DWG and check it is non-empty.",

    # ---- holes / centrelines ----------------------------------------------
    "min_diameter_mm": "Ignore circular edges smaller than this, in millimetres.",
    "max_callouts": "Upper bound on how many callouts to create in one call.",
    "do_hole_callouts": "Include hole callouts in the one-shot pipeline.",
    "do_surface_finish": "Include surface-finish symbols in the one-shot pipeline.",
    "budget_s": "Soft time budget in seconds; the tool stops before overrunning it.",
    "extended_lines": "Draw the centre-mark cross lines beyond the hole edge.",
    "include_slots": "Also mark slots, not just round holes.",
    "include_cylinders": "Include cylindrical faces in the quality report.",
    "include_faces": "Read per-face data too (slower, needed for face-level checks).",
    "include_points": "Include start/end coordinates. False returns indices only and is much faster.",
    "include_mass": "Include mass in the BOM rows.",
    "include_material": "Include material in the BOM rows.",
    "include_reversed": "Also update the 180-degree-rotated title-block copy in the sheet corner.",

    # ---- misc --------------------------------------------------------------
    "targets": "Files to export, one entry per input.",
    "fmt": "Output format, e.g. 'pdf', 'dwg', 'dxf', 'step'.",
    "output": "Where to write the exported file.",
    "mode": "Operating mode for the rename pass.",
    "mapping": "How to map old names to new ones.",
    "prefix": "Prefix prepended to every renamed item.",
    "suffix": "Suffix appended to every renamed item.",
    "palette": "Colour palette to apply; names are matched case-insensitively.",
    "view": "Named model view to capture, e.g. '*Front'. null uses the current view.",
    "keep_bmp": "Keep the intermediate BMP next to the PNG (debugging only).",
    "code": "Drawing number to write into the title block.",
    "material": "Material text for the title block.",
    "material_name": "Material to assign, e.g. '普通碳钢'.",
    "rules": "Per-part material assignment rules.",
    "category": "Part category used to pick views and the dimension strategy, e.g. '轴类'.",
    "auto_fit": "Shrink the view scale until every view fits the sheet inner box.",
    "ai_supplied": "Values you decided yourself (not derivable from the model); listed back so the receipt separates fact from judgement.",
    "require_gdt": "Treat a missing geometric tolerance as a pending item.",
    "strict_attachments": "Count every detached dimension as pending, without the orphan-suspected excuse.",
    "break_alignment": "Break the alignment to the parent view so this view can move freely.",
    "folder": "Directory of drawings to survey.",
    "sample_size": "How many drawings to include in the survey sample.",
    "sample_len": "Sampling length shown with the surface-finish symbol.",
    "seed": "Random seed, so a sample can be reproduced.",
    "deep": "Include the deeper diagnostic probes (slower).",
    "tail": "How many recent log entries to return.",
    "operation": "Which capability slice to report; null returns the whole table.",
    "budget_s": "Soft time budget in seconds.",
    "max_depth": "How many levels of the assembly tree to descend; null means all.",
}


def collect(text):
    """Return every (tool, arg, annotation span) that still needs wrapping."""
    tree = ast.parse(text)
    pending = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
            for d in node.decorator_list
        ):
            continue
        for arg in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs):
            if arg.arg in ("self", "cls"):
                continue
            ann = arg.annotation
            if ann is None:
                pending.append((node.name, arg.arg, None))
                continue
            # already wrapped?
            if (
                isinstance(ann, ast.Subscript)
                and isinstance(ann.value, ast.Name)
                and ann.value.id == "Annotated"
            ):
                continue
            pending.append((node.name, arg.arg, (ann.lineno, ann.col_offset,
                                                 ann.end_lineno, ann.end_col_offset)))
    return tree, pending


def apply(text, pending):
    """Splice Annotated[...] around each annotation, last position first."""
    lines = text.splitlines(keepends=True)

    def offset(lineno, col):
        return sum(len(x) for x in lines[: lineno - 1]) + col

    edits = []
    for tool, arg, span in pending:
        desc = DESCRIPTIONS.get(arg)
        if desc is None:
            raise SystemExit("no description for %r (used by %s)" % (arg, tool))
        if span is None:
            raise SystemExit("parameter %r of %s has no type annotation" % (arg, tool))
        start, end = offset(span[0], span[1]), offset(span[2], span[3])
        inner = text[start:end]
        literal = desc.replace("\\", "\\\\").replace('"', '\\"')
        edits.append((start, end, 'Annotated[%s, Field(description="%s")]' % (inner, literal)))

    for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
        text = text[:start] + replacement + text[end:]
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    path = TARGET if os.path.exists(TARGET) else os.path.join("..", TARGET)
    text = io.open(path, encoding="utf-8").read()

    tree, pending = collect(text)
    if pending:
        names = sorted({p[1] for p in pending})
        missing = [n for n in names if n not in DESCRIPTIONS]
        if missing:
            print("missing descriptions: %s" % ", ".join(missing))
        print("%d parameters still unannotated across %d tools"
              % (len(pending), len({p[0] for p in pending})))
        if args.check:
            return 1
        text = apply(text, pending)
        if "from typing import Annotated" not in text:
            text = text.replace("from typing import", "from typing import Annotated,", 1) \
                if "from typing import" in text else text
        if "Annotated," not in text and "import Annotated" not in text:
            text = text.replace("from typing import ", "from typing import Annotated, ", 1)
        if "from pydantic import Field" not in text:
            anchor = "from mcp.server.fastmcp import FastMCP"
            text = text.replace(anchor, "from pydantic import Field\n" + anchor, 1)
        io.open(path, "w", encoding="utf-8", newline="").write(text)
        print("rewritten")
    else:
        print("all parameters documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
