"""MCP entry point. Registered name: Solidworks_AssetMate_MechDog.

Every tool returns a JSON string (never prose) so agents can keep reasoning.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import SERVER_NAME
from .tools import assembly, bodies, bom, recolor, rename

mcp = FastMCP(SERVER_NAME)


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
    matched components. Set apply=true to paint. Changes component appearance
    only, not material/density. Returns previous colors for undo.
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
) -> str:
    """Export a BOM: one row per part type with qty, material, unit and total mass.

    Read-only. Lightweight components are resolved first, otherwise mass/material
    come back null. Missing values are reported in `warnings`, never guessed.
    Optionally writes a UTF-8 CSV for Excel.
    """
    return _json(bom.export_bom(
        assembly_path, include_mass=include_mass, include_material=include_material,
        csv_path=csv_path, refresh=refresh,
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
) -> str:
    """Rename part/assembly FILES by mapping or prefix/suffix. Dry-run by default.

    mode="file" is supported and rolls back on failure. mode="instance" returns
    instance_rename_unsupported: SolidWorks has no writable instance-name API
    (IComponent2.Name is read-only, Name2 ignores assignment).
    Pass assembly_path to get a report of components that still reference old names.
    """
    return _json(rename.rename_components(
        part_paths=part_paths, mapping=mapping, prefix=prefix, suffix=suffix,
        apply=apply, assembly_path=assembly_path, mode=mode,
    ))


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

    Experimental: returns a structured error on pywin32 bindings where
    CreateMassProperty is unavailable (see docs/com-quirks.md section 5).
    """
    return _json(assembly.mass_properties(assembly_path))


@mcp.tool()
def export_preview(
    assembly_path: str,
    output_path: str,
    size: str = "1600x1200",
    view: str = "*Isometric",
) -> str:
    """Export the current view to an image, e.g. 'D:/out/assembly.bmp' or '.png'.

    size is 'WxH'. SolidWorks writes BMP natively; PNG requires Pillow.
    """
    return _json(assembly.export_preview(assembly_path, output_path, size=size, view=view))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
