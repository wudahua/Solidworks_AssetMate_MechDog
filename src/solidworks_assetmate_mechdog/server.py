"""MCP entry point. Registered name: Solidworks_AssetMate_MechDog.

Every tool returns a JSON string (never prose) so agents can keep reasoning.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import SERVER_NAME, __version__
from .tools import assembly, bodies, bom, drawing, export as export_tools, recolor, rename

mcp = FastMCP(SERVER_NAME)
# FastMCP (mcp 1.x) has no `version` argument: the low-level server falls back to
# the mcp package version, so serverInfo.version reported 1.30.0 instead of this
# toolkit's version. Set it explicitly when the attribute exists.
if getattr(mcp, "_mcp_server", None) is not None:
    mcp._mcp_server.version = __version__


def _json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def recolor_assembly(
    assembly_path: str,
    palette: dict,
    apply: bool = False,
    save: bool = True,
) -> str:
    """Recolor a SolidWorks assembly by part type, e.g. {"Crank": [0.88,0.2,0.18]}.

    Palette keys are component names without the '-N' instance suffix; RGB is 0..1.
    SAFE DEFAULT: apply=false is a dry run listing parts, current colors and
    matched components — with zero matches it still returns the part list so you
    can fix the keys. apply=true with zero matches returns no_match plus
    available_parts. Changes component appearance only, not material/density.
    Returns previous colors for undo; readback covers at most 8 part types
    (1 instance per palette key) and flags when the getter is unreliable.
    """
    return _json(recolor.recolor_assembly(assembly_path, palette, apply=apply, save=save))


@mcp.tool()
def list_bodies(part_path: str) -> str:
    """List solid bodies in a part: name, faces, edges, vertices.

    Use body names as palette keys — the returned order can change between calls.
    """
    return _json(bodies.list_bodies(part_path))


@mcp.tool()
def recolor_bodies(
    part_path: str,
    palette: dict,
    apply: bool = False,
    save: bool = True,
) -> str:
    """Recolor each solid body of a multibody PART a different color.

    Palette keys are body names from list_bodies, e.g. {"Hull": [0.8,0.2,0.2]}.
    SAFE DEFAULT: apply=false lists bodies and matched names without changing
    anything. Each paint is applied only after verifying exactly one body is
    selected (guards against a document-wide recolor).
    """
    return _json(bodies.recolor_bodies(part_path, palette, apply=apply, save=save))


@mcp.tool()
def export_bom(
    assembly_path: str,
    include_mass: bool = True,
    include_material: bool = True,
    csv_path: str | None = None,
    refresh: bool = False,
    resolve_lightweight: bool = True,
) -> str:
    """Export a BOM: one row per part type with qty, material, unit and total mass.

    Read-only. Lightweight components are resolved first (resolve_lightweight=false
    skips that, at the cost of null mass/material). Missing values are reported in
    `warnings`, never guessed. Optionally writes a UTF-8 CSV for Excel.
    """
    return _json(bom.export_bom(
        assembly_path, include_mass=include_mass, include_material=include_material,
        csv_path=csv_path, refresh=refresh, resolve_lightweight=resolve_lightweight,
    ))


@mcp.tool()
def rename_components(
    part_paths: list[str],
    mapping: dict | None = None,
    prefix: str | None = None,
    suffix: str | None = None,
    apply: bool = False,
    assembly_path: str | None = None,
    mode: str = "file",
    save: bool = True,
) -> str:
    """Rename part/assembly FILES by mapping or prefix/suffix. Dry-run by default.

    mode="file"  rename on disk only.
    mode="auto"  rename + retarget assembly references (ReplaceReference) + save.
                 Requires assembly_path. Instance names follow the new file name.
    mode="instance" is unsupported: SolidWorks has no writable instance-name API.
    """
    return _json(rename.rename_components(
        part_paths=part_paths, mapping=mapping, prefix=prefix, suffix=suffix,
        apply=apply, assembly_path=assembly_path, mode=mode, save=save,
    ))


@mcp.tool()
def batch_export(
    targets: list[str],
    output_dir: str,
    fmt: str = "step",
    apply: bool = False,
) -> str:
    """Export parts/assemblies to STEP in one pass (dry-run by default).

    Read-only on the model: SaveAsSilent() behaves as an export and leaves the
    document path unchanged. Existing targets are reported as conflicts, never
    overwritten. PDF needs a .SLDDRW and DXF a flat pattern — those are rejected
    with a hint rather than guessed at.
    """
    return _json(export_tools.batch_export(targets, output_dir, fmt=fmt, apply=apply))


@mcp.tool()
def verify_dimension_attachments(drawing_path: str, view_name: str | None = None) -> str:
    """Report what each dimension is attached to (edge / face / sketch segment / nothing).

    Dimensions with no attachment are untraceable and count as incomplete (process §6.1).
    """
    return _json(drawing.verify_dimension_attachments(drawing_path, view_name=view_name))


@mcp.tool()
def list_circular_edges(
    drawing_path: str,
    view_name: str | None = None,
    min_diameter_mm: float = 0.0,
) -> str:
    """List circular edges of a view: sheet position + diameter. Use before callouts."""
    return _json(drawing.list_circular_edges(
        drawing_path, view_name=view_name, min_diameter_mm=min_diameter_mm))


@mcp.tool()
def add_hole_callouts(
    drawing_path: str,
    view_name: str | None = None,
    min_diameter_mm: float = 0.0,
    one_per_diameter: bool = True,
    model_path: str | None = None,
    save: bool = True,
) -> str:
    """Batch native hole callouts on circular edges, reading each one back.

    one_per_diameter=True follows the drafting convention for repeated holes.
    model_path cross-checks the read-back diameter against the model.
    """
    return _json(drawing.add_hole_callouts(
        drawing_path, view_name=view_name, min_diameter_mm=min_diameter_mm,
        one_per_diameter=one_per_diameter, model_path=model_path, save=save))


@mcp.tool()
def reconcile_ledger(
    model_path: str,
    drawing_path: str,
    tolerance_mm: float = 0.5,
) -> str:
    """Match the model's driving dimensions against the drawing's, and list what is missing.

    The §6.1 check that counting cannot do: returns coverage, unmatched ledger dimensions
    grouped by feature, and drawing-only values. Projection means the same edge dimensions
    differently per view, so treat the unmatched list as candidates to confirm.
    """
    return _json(drawing.reconcile_ledger(
        model_path, drawing_path, tolerance_mm=tolerance_mm))


@mcp.tool()
def delivery_check(
    model_path: str,
    drawing_path: str,
    ai_supplied: list[str] | None = None,
    require_gdt: bool = False,
    strict_attachments: bool = False,
) -> str:
    """Aggregate ledger + audit + annotations into a delivery verdict (process §7).

    Returns 可制造 / 待设计审核 / 仅供参考·未完成 plus the blockers and open items.
    ai_supplied lists values the caller supplied (material, tolerance, fit) — anything
    listed downgrades the verdict to 待设计审核.
    """
    return _json(drawing.delivery_check(
        model_path, drawing_path, ai_supplied=ai_supplied, require_gdt=require_gdt,
        strict_attachments=strict_attachments,
    ))


@mcp.tool()
def list_drawing_annotations(drawing_path: str, view_name: str | None = None) -> str:
    """Inventory every annotation: dimensions (with value + tolerance), notes,
    geometric tolerances (frame content), center marks, hole callouts.

    Use it to prove what a drawing does or does not carry — e.g. "no geometric
    tolerance anywhere" — which the CAD API cannot create but can read.
    """
    return _json(drawing.list_drawing_annotations(drawing_path, view_name=view_name))


@mcp.tool()
def add_dimension_tolerance(
    drawing_path: str,
    value_mm: float,
    tolerance_type: str = "bilateral",
    upper: float = 0.1,
    lower: float = 0.1,
    view_name: str | None = None,
    apply: bool = False,
) -> str:
    """Apply a NATIVE tolerance to an existing drawing dimension, located by its value in mm.

    tolerance_type: bilateral | symmetric | limit | basic | min | max | fit |
    fit_with_tolerance | none. upper/lower in millimetres. Dry-run by default.
    """
    return _json(drawing.add_dimension_tolerance(
        drawing_path, value_mm, tolerance_type=tolerance_type, upper=upper,
        lower=lower, view_name=view_name, apply=apply,
    ))


@mcp.tool()
def material_library(database: str = "SolidWorks Materials.sldmat") -> str:
    """List material names available in the installed SolidWorks material library."""
    return _json(material.material_library(database=database))


@mcp.tool()
def set_part_material(
    paths: list[str],
    material: str,
    database: str = "SolidWorks Materials.sldmat",
    config: str = "",
    save: bool = True,
    apply: bool = False,
) -> str:
    """Assign a material to one or more parts; returns previous material and mass.

    Dry-run by default. Without a material SolidWorks uses 1000 kg/m³, so BOM masses are
    wrong until this is set.
    """
    return _json(material.set_part_material(
        paths, material, database=database, config=config, save=save, apply=apply,
    ))


@mcp.tool()
def assign_materials(
    paths: list[str],
    rules: list | None = None,
    database: str = "SolidWorks Materials.sldmat",
    config: str = "",
    save: bool = True,
    apply: bool = False,
) -> str:
    """Pick a material per part from name keywords (标准件→碳钢, 结构件→铝合金), then assign.

    The chosen material is an AI-supplied value: it is reported per part with its reason
    and should be listed as pending confirmation in the delivery record.
    """
    return _json(material.assign_materials(
        paths, rules=rules, database=database, config=config, save=save, apply=apply,
    ))


@mcp.tool()
def build_feature_ledger(
    model_path: str,
    include_faces: bool = True,
    config: str | None = None,
) -> str:
    """Build a feature ledger: every feature, its driving dimensions, hole candidates
    and model doubts (process §2). This is the basis for annotation planning and
    self-check — read-only, the model is never modified.

    include_faces=False skips cylindrical-face scanning (faster on large models).
    """
    return _json(ledger.build_feature_ledger(
        model_path, include_faces=include_faces, config=config,
    ))


@mcp.tool()
def create_drawing(
    model_path: str,
    output_path: str | None = None,
    template: str | None = None,
    views: list[str] | None = None,
    scale: float | None = None,
    auto_fit: bool = True,
    view_scales: dict | None = None,
    paper: str = "A3",
) -> str:
    """Create a native .SLDDRW from a part/assembly, add model views, optionally save.

    views: front/back/left/right/top/bottom/isometric (default front+top+isometric).
    View names are resolved against the model, so Chinese (`*前视`) and English
    (`*Front`) SolidWorks both work. Units are meters; an A3 sheet is 0.42 x 0.297.
    """
    return _json(drawing.create_drawing(
        model_path, output_path=output_path, template=template,
        views=views or ["front", "top", "isometric"], scale=scale, auto_fit=auto_fit,
        view_scales=view_scales, paper=paper,
    ))


@mcp.tool()
def export_drawing(drawing_path: str, output_path: str, fmt: str | None = None) -> str:
    """Export a .SLDDRW to PDF / DWG / DXF.

    PDF can only come from a drawing — for STEP/STL of models use batch_export.
    """
    return _json(drawing.export_drawing(drawing_path, output_path, fmt=fmt))


@mcp.tool()
def render_drawing(pdf_path: str, output_dir: str | None = None, scale: float = 2.0) -> str:
    """Render each PDF page to PNG so the drawing can be visually checked.

    Requires pypdfium2 (pip install pypdfium2). Returns one PNG per page.
    """
    return _json(drawing.render_drawing(pdf_path, output_dir=output_dir, scale=scale))


@mcp.tool()
def add_section_view(
    drawing_path: str,
    view_name: str | None = None,
    orientation: str = "horizontal",
    offset_m: float = 0.0,
    position: list[float] | None = None,
    label: str = "A",
    flip: bool = False,
    depth: float = 0.0,
) -> str:
    """Create a SECTION VIEW through a model view (exposes internal features).

    The cut line is drawn in the view's sketch automatically. orientation is
    "horizontal" or "vertical"; offset_m shifts it off the view centre; flip reverses
    the viewing direction; depth > 0 gives a partial section.
    """
    return _json(drawing.add_section_view(
        drawing_path, view_name=view_name, orientation=orientation,
        offset_m=offset_m, position=position, label=label, flip=flip, depth=depth,
    ))


@mcp.tool()
def add_detail_view(
    drawing_path: str,
    view_name: str | None = None,
    center: list[float] | None = None,
    radius_m: float = 0.02,
    position: list[float] | None = None,
    scale: float = 2.0,
    label: str = "A",
    style: int = 0,
    shape: int = 0,
    flip: bool = False,
    show_label: bool = True,
    circle_segments: int = 32,
) -> str:
    """Add a DETAIL (enlarged) view around a circular area of a model view.

    center/radius_m are sheet metres (default: the view centre, 20 mm radius).
    scale is the detail's own scale (2.0 = 2:1). circle_segments must be > 0 —
    passing 0 is what makes SolidWorks reject the call.
    """
    return _json(drawing.add_detail_view(
        drawing_path, view_name=view_name, center=center, radius_m=radius_m,
        position=position, scale=scale, label=label, style=style, shape=shape,
        flip=flip, show_label=show_label, circle_segments=circle_segments,
    ))


@mcp.tool()
def add_flat_pattern_view(
    drawing_path: str,
    model_path: str,
    config_name: str | None = None,
    position: list[float] | None = None,
    scale: float | None = None,
    hide_bend_lines: bool = False,
    flip: bool = False,
) -> str:
    """Add a sheet-metal FLAT PATTERN view (unfolded blank with bend lines).

    The part must have a Flat-Pattern feature. Dimension sheet metal on the flat
    pattern, not the folded view.
    """
    return _json(drawing.add_flat_pattern_view(
        drawing_path, model_path, config_name=config_name, position=position,
        scale=scale, hide_bend_lines=hide_bend_lines, flip=flip,
    ))


@mcp.tool()
def auto_dimension(
    drawing_path: str,
    all_views: bool = True,
    include_unmarked: bool = False,
    eliminate_duplicates: bool = True,
) -> str:
    """Insert the model's driving dimensions into the drawing (Insert Model Items).

    Marked-for-drawing dimensions only by default; include_unmarked=True brings in
    every driving dimension. Returns inserted count and the post-insert audit.
    """
    return _json(drawing.auto_dimension(
        drawing_path, all_views=all_views, include_unmarked=include_unmarked,
        eliminate_duplicates=eliminate_duplicates,
    ))


@mcp.tool()
def list_drawing_edges(drawing_path: str, view_name: str | None = None) -> str:
    """List a view's visible edges with stable indices and model coordinates.

    Use the index with add_drawing_dimension(edge_index=...) when sheet coordinates
    are inconvenient (drawing edges report MODEL coordinates under this binding).
    """
    return _json(drawing.list_drawing_edges(drawing_path, view_name=view_name))


@mcp.tool()
def add_drawing_dimension(
    drawing_path: str,
    x: float,
    y: float,
    value: float | None = None,
    offset_x: float = 0.0,
    offset_y: float = -0.015,
    view_name: str | None = None,
    tolerance: float = 0.01,
    edge_index: int | None = None,
) -> str:
    """Add ONE dimension on a projected edge at sheet point (x, y) in meters.

    Manual dimensioning path: InsertModelItems can only carry dimensions the model
    already has, so IR-rebuilt parts need this. value is meters; omit to keep the
    computed value. Selection falls back EDGE -> SILHOUETTE -> SKETCHSEGMENT.
    """
    return _json(drawing.add_drawing_dimension(
        drawing_path, x, y, value=value, offset_x=offset_x, offset_y=offset_y,
        view_name=view_name, tolerance=tolerance, edge_index=edge_index,
    ))


@mcp.tool()
def add_center_marks(
    drawing_path: str,
    include_slots: bool = True,
    extended_lines: bool = True,
) -> str:
    """Auto-insert center marks and centerlines on every hole/slot in every view.

    Returns views_marked (reliable) rather than a mark count, which under-reports
    on this binding.
    """
    return _json(drawing.add_center_marks(
        drawing_path, include_slots=include_slots, extended_lines=extended_lines,
    ))


@mcp.tool()
def add_hole_callout(drawing_path: str, x: float, y: float, view_name: str | None = None) -> str:
    """Insert a native hole callout at a sheet point (meters) on a hole edge.

    Returns callout_failed when the point is not on a hole's projected circle —
    a missing callout is reported, never faked.
    """
    return _json(drawing.add_hole_callout(
        drawing_path, x, y, view_name=view_name, model_path=model_path, save=save,
    ))


@mcp.tool()
def set_title_block(
    drawing_path: str,
    name: str | None = None,
    code: str | None = None,
    material: str | None = None,
    apply: bool = False,
    include_reversed: bool = True,
) -> str:
    """Fill title-block placeholders (图样名称 / 图样代号 / 材质) in a drawing.

    `图样代号` also appears rotated 180 deg at the sheet's top-left corner (GB
    folding convention). include_reversed=False leaves that one untouched.

    GB templates store these as literal quoted text, so nothing is linked to the
    model. Dry-run by default; only the fields you pass are changed, and fields
    that cannot be found are reported instead of invented.
    """
    return _json(drawing.set_title_block(
        drawing_path, name=name, code=code, material=material, apply=apply,
        include_reversed=include_reversed,
    ))


@mcp.tool()
def drawing_audit(drawing_path: str) -> str:
    """Audit a drawing: views, scales, blank views, dimension and annotation counts.

    Evidence only — it never judges view choice, tolerances or manufacturability.
    Blank views and zero dimensions are reported as warnings, not silently passed.
    """
    return _json(drawing.drawing_audit(drawing_path))


@mcp.tool()
def get_assembly_tree(assembly_path: str, max_depth: int = 1, refresh: bool = False) -> str:
    """List assembly components with part names, paths, colors and instance info.

    max_depth=1 is flat and fast; higher values recurse into subassemblies.
    The component list is cached per document; set refresh=true after editing
    the assembly's structure outside this server.
    """
    return _json(assembly.assembly_tree(assembly_path, max_depth=max_depth, refresh=refresh))


@mcp.tool()
def get_mass_properties(assembly_path: str) -> str:
    """Read whole-assembly mass, volume, surface area and center of mass (SI units).

    Returns a structured error when CreateMassProperty is unavailable on the
    current binding (see docs/com-quirks.md section 5) instead of a guess.
    """
    return _json(assembly.mass_properties(assembly_path))


@mcp.tool()
def export_preview(
    assembly_path: str,
    output_path: str,
    size: str = "1600x1200",
    view: str = "*Isometric",
    keep_bmp: bool = False,
) -> str:
    """Export the current view to an image, e.g. 'D:/out/assembly.bmp' or '.png'.

    size is 'WxH'. SolidWorks writes BMP natively; PNG requires Pillow and the
    intermediate BMP is deleted unless keep_bmp=true.
    """
    return _json(assembly.export_preview(
        assembly_path, output_path, size=size, view=view, keep_bmp=keep_bmp,
    ))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
