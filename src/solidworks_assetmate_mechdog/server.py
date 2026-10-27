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
def create_drawing(
    model_path: str,
    output_path: str | None = None,
    template: str | None = None,
    views: list[str] | None = None,
    scale: float | None = None,
    auto_fit: bool = True,
) -> str:
    """Create a native .SLDDRW from a part/assembly, add model views, optionally save.

    views: front/back/left/right/top/bottom/isometric (default front+top+isometric).
    View names are resolved against the model, so Chinese (`*前视`) and English
    (`*Front`) SolidWorks both work. Units are meters; an A3 sheet is 0.42 x 0.297.
    """
    return _json(drawing.create_drawing(
        model_path, output_path=output_path, template=template,
        views=views or ["front", "top", "isometric"], scale=scale, auto_fit=auto_fit,
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
    return _json(drawing.add_hole_callout(drawing_path, x, y, view_name=view_name))


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
