"""MCP entry point. Registered name: Solidworks_AssetMate_MechDog.

Every tool returns a JSON string (never prose) so agents can keep reasoning.
"""
from __future__ import annotations

import json
import os

# A report had `add_section_view` take the whole server process with it: "Not connected",
# no traceback, no error payload, and the log stopped mid-run. A Python exception cannot do
# that — only a fault in native code (COM/pywin32) can. faulthandler writes the C-level
# stack to logs/crash.log so the next occurrence is diagnosable instead of silent.
import faulthandler

try:
    _CRASH_DIR = os.path.join(os.getcwd(), "logs")
    os.makedirs(_CRASH_DIR, exist_ok=True)
    _CRASH_FILE = open(os.path.join(_CRASH_DIR, "crash.log"), "a", buffering=1,
                       encoding="utf-8")
    faulthandler.enable(file=_CRASH_FILE, all_threads=True)
except Exception:  # a read-only cwd must not stop the server from starting
    faulthandler.enable(all_threads=True)

from typing import Annotated

from pydantic import Field
from mcp.server.fastmcp import FastMCP

from . import SERVER_NAME, __version__
from . import sw_session
from .tools import (assembly, bodies, bom, capabilities, drawing, export as export_tools,
                    layout, ledger, material as material_tools, pipeline, planner, recolor,
                    rename, sheet_metal, survey)

mcp = FastMCP(SERVER_NAME)
# FastMCP (mcp 1.x) has no `version` argument: the low-level server falls back to
# the mcp package version, so serverInfo.version reported 1.30.0 instead of this
# toolkit's version. Set it explicitly when the attribute exists.
if getattr(mcp, "_mcp_server", None) is not None:
    mcp._mcp_server.version = __version__


def _reject_unknown_arguments(fn):
    """Refuse a call that passes an argument this tool does not declare.

    Report 2026-11-08 (P2-5, carried since round 4): `health_check(bogus_param=123)`
    returned `isError=False` and a normal payload. FastMCP fills missing arguments with
    defaults and DROPS anything it does not recognise — `extra=ignore` is the pydantic
    default and there is no per-call override at this layer.

    That makes a typo invisible: `drw_path=` instead of `drawing_path=` silently calls the
    tool with the real parameter at its default, and the receipt looks like a normal run.
    For a tool that writes to a drawing, "it ran with the wrong arguments and said ok" is
    the worst possible outcome.

    `functools.wraps` keeps `__wrapped__`, and `inspect.signature` follows it, so FastMCP
    still generates the same inputSchema from the original signature. Only the CALL is
    intercepted.
    """
    import functools
    import inspect
    import json as _jsonlib

    try:
        accepted = set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return fn                      # builtins / C functions: nothing to check

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        unknown = sorted(k for k in kwargs if k not in accepted)
        if unknown:
            return _jsonlib.dumps({
                "ok": False,
                "error": "unknown_argument",
                "message": "unknown argument(s): %s" % ", ".join(unknown),
                "unknown": unknown,
                "accepted": sorted(accepted),
                "hint": ("This tool does not have those parameters. The call was refused "
                         "rather than run with them dropped — a mistyped name would "
                         "otherwise fall back to the default and the receipt would look "
                         "normal."),
            }, ensure_ascii=False)
        return fn(*args, **kwargs)

    return wrapper


def _json(payload):
    """Serialise a tool result, and log it when it reports failure.

    Hooking here instead of in 51 individual wrappers keeps the coverage complete without
    touching every tool: every result passes through this one function. The tool name is
    taken from the caller's frame, which is the `@mcp.tool()` function itself.
    """
    import sys

    tool = None
    try:
        tool = sys._getframe(1).f_code.co_name
    except Exception:
        pass
    try:
        if isinstance(payload, dict):
            sw_session.note_last_call(tool=tool, args=None)
            if payload.get("ok") is False:
                sw_session.log_failure(tool, payload)
    except Exception:
        # Diagnostics must never break a tool.
        pass
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _install_excepthook():
    """Flush unhandled tracebacks to stderr AND the failure log (P0-1, suggestion 4).

    The report noted there was no error output at all before the outage, so the user could
    not tell whether the MCP process or SolidWorks had broken. A bare traceback on stderr
    is also invisible inside most MCP hosts, so it is mirrored into
    logs/mcp-YYYYMMDD.log where `recent_failures` can read it back after the fact.
    """
    import sys
    import traceback

    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        try:
            sys.stderr.write("".join(traceback.format_exception(exc_type, exc, tb)))
            sys.stderr.flush()
        except Exception:
            pass
        try:
            sw_session.log_failure(
                "unhandled", {"stage": "excepthook"},
                exc=exc if isinstance(exc, BaseException) else None,
                extra={"exc_type": getattr(exc_type, "__name__", None)})
        except Exception:
            pass
        try:
            previous(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = hook


_install_excepthook()


@mcp.tool()
@_reject_unknown_arguments
def health_check(deep: Annotated[bool, Field(description="Include the deeper diagnostic probes (slower).")] = False) -> str:
    """Report whether the MCP bridge and SolidWorks are both alive.

    Tells apart three states that used to look identical: the bridge is dead (a stale COM
    pointer — `reconnect()` retries), SolidWorks is not responding, or everything is fine.
    `deep=True` also reads ActiveDoc, which is where a stale pointer usually surfaces.
    """
    return json.dumps(sw_session.health(deep=deep), ensure_ascii=False, indent=2)


@mcp.tool()
@_reject_unknown_arguments
def ping() -> str:
    """Cheapest possible "is it alive" probe (P0-1).

    Answers in ~18 ms and never touches ActiveDoc, so it cannot hang on a modal dialog.
    Returns the SolidWorks revision, the round-trip latency, the last tool that ran, and
    the self-healing counters (`bridge.stale_drops` / `bridge.reconnects`). Use it to tell
    "alive but busy" from "dead"; use health(deep=True) when you also need ActiveDoc.
    """
    return _json(sw_session.ping())


@mcp.tool()
@_reject_unknown_arguments
def recent_failures(tail: Annotated[int, Field(description="How many recent log entries to return.")] = 20) -> str:
    """Read back the last N failures from logs/mcp-YYYYMMDD.log.

    A mid-run disconnect used to leave no trace at all. Every failing tool call now appends
    one JSON line (tool, args, payload, SW version, health snapshot, traceback).
    """
    import os

    path = os.path.join(sw_session.log_dir(), "mcp-%s.log" % __import__("datetime").datetime.now().strftime("%Y%m%d"))
    if not os.path.exists(path):
        return json.dumps({"ok": True, "log": path, "entries": [], "note": "no failures today"},
                          ensure_ascii=False, indent=2)
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    entries = []
    for line in lines[-max(1, int(tail)):]:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return json.dumps({"ok": True, "log": path, "count": len(entries), "entries": entries},
                      ensure_ascii=False, indent=2)


@mcp.tool()
@_reject_unknown_arguments
def produce_drawing(
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    output_path: Annotated[str | None, Field(description="Where to write the result. Extension decides the format when fmt is omitted.")] = None,
    paper: Annotated[str, Field(description="Sheet size, e.g. 'A3' or 'A4'; drives the template and the usable area.")] = "A3",
    views: Annotated[list | None, Field(description="Which standard views to create, in order, e.g. ['front', 'right', 'isometric'].")] = None,
    category: Annotated[str | None, Field(description="Part category used to pick views and the dimension strategy, e.g. '轴类'.")] = None,
    do_hole_callouts: Annotated[bool | None, Field(description="Include hole callouts in the one-shot pipeline.")] = None,
    do_surface_finish: Annotated[bool | None, Field(description="Include surface-finish symbols in the one-shot pipeline.")] = None,
    prune_to_plan: Annotated[bool, Field(description="Delete inserted dimensions that the plan did not ask for.")] = True,
    overwrite: Annotated[bool, Field(description="Rebuild even if output_path already exists. Without it an existing drawing is refused (output_exists) instead of having a second full set of dimensions appended to it.")] = False,
    projection: Annotated[str, Field(description="Projection convention for the sheet: 'first' (GB/T 14692, default) or 'third' (ISO/US). Chooses where the top view and the side view are placed.")] = "first",
    close_when_done: Annotated[bool, Field(description="Close the part and drawing this run opened. False keeps them open for inspection — only useful when a human is watching the SolidWorks window.")] = True,
    do_derived_check: Annotated[bool, Field(description="Reconcile the model's driving dimensions against the sheet and report coverage. Costs one ledger read; turn off for a faster run.")] = True,
    hole_budget_s: Annotated[float, Field(description="Seconds allowed for hole callouts before that step gives up. Raise it for parts with many holes; the step reports budget_exceeded when it runs out.")] = 45.0,
) -> str:
    """Model in, annotated and judged drawing out — the whole chain in one call.

    ledger → plan → views → dimensions → center marks → surface finish (category default)
    → hole callouts → audit → reconciliation → delivery verdict.

    Defaults come from the part category (钣金 / 一般零件 / 轴类 / 装配体) and are echoed in
    `defaults_applied`. A failing step is recorded and the chain continues.

    prune_to_plan=True makes the plan binding: dimensions the plan did not keep are deleted
    right after insertion. Set False for the old "import everything" behaviour.
    """
    return _json(pipeline.produce_drawing(
        model_path, output_path=output_path, paper=paper,
        views=tuple(views) if views else None, category=category,
        do_hole_callouts=do_hole_callouts, do_surface_finish=do_surface_finish,
        prune_to_plan=prune_to_plan,
        overwrite=overwrite,
        projection=projection,
        close_when_done=close_when_done,
        do_derived_check=do_derived_check,
        hole_budget_s=hole_budget_s))


@mcp.tool()
@_reject_unknown_arguments
def plan_drawing(
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    paper: Annotated[str, Field(description="Sheet size, e.g. 'A3' or 'A4'; drives the template and the usable area.")] = "A3",
    views: Annotated[list | None, Field(description="Which standard views to create, in order, e.g. ['front', 'right', 'isometric'].")] = None,
    include_unmarked: Annotated[bool, Field(description="Also bring in dimensions not marked for drawing (more complete, noisier).")] = False,
) -> str:
    """Decide before drawing: which dimensions to keep, how many sheets, what views.

    Read-only plan (nothing is created). Real drawings average 14 dimensions; importing
    every driving dimension puts ~44 on the sheet, which is transcription, not drafting.
    `keep_dimensions` is the shortlist to feed auto_dimension.
    """
    return _json(planner.plan_drawing(
        model_path, paper=paper,
        views=tuple(views) if views else ("front", "right"),
        include_unmarked=include_unmarked))


@mcp.tool()
@_reject_unknown_arguments
def check_capabilities(operation: Annotated[str | None, Field(description="Which capability slice to report; null returns the whole table.")] = None) -> str:
    """What this toolkit can and cannot do — answer before a long pipeline, not after.

    Each entry states the limit, the evidence behind it, and the workaround. Use it to
    avoid a 400-second run that ends in "0 hole callouts".
    """
    return _json(capabilities.check_capabilities(operation=operation))


@mcp.tool()
@_reject_unknown_arguments
def recolor_assembly(
    assembly_path: Annotated[str, Field(description="Assembly file (.SLDASM), absolute path.")],
    palette: Annotated[dict, Field(description="Colour palette to apply; names are matched case-insensitively.")],
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
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
@_reject_unknown_arguments
def list_bodies(part_path: Annotated[str, Field(description="Single part file (.SLDPRT), absolute path.")]) -> str:
    """List solid bodies in a part: name, faces, edges, vertices.

    Use body names as palette keys — the returned order can change between calls.
    """
    return _json(bodies.list_bodies(part_path))


@mcp.tool()
@_reject_unknown_arguments
def recolor_bodies(
    part_path: Annotated[str, Field(description="Single part file (.SLDPRT), absolute path.")],
    palette: Annotated[dict, Field(description="Colour palette to apply; names are matched case-insensitively.")],
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
) -> str:
    """Recolor each solid body of a multibody PART a different color.

    Palette keys are body names from list_bodies, e.g. {"Hull": [0.8,0.2,0.2]}.
    SAFE DEFAULT: apply=false lists bodies and matched names without changing
    anything. Each paint is applied only after verifying exactly one body is
    selected (guards against a document-wide recolor).
    """
    return _json(bodies.recolor_bodies(part_path, palette, apply=apply, save=save))


@mcp.tool()
@_reject_unknown_arguments
def export_bom(
    assembly_path: Annotated[str, Field(description="Assembly file (.SLDASM), absolute path.")],
    include_mass: Annotated[bool, Field(description="Include mass in the BOM rows.")] = True,
    include_material: Annotated[bool, Field(description="Include material in the BOM rows.")] = True,
    csv_path: Annotated[str | None, Field(description="Where to write the BOM as CSV.")] = None,
    refresh: Annotated[bool, Field(description="Re-read the model from disk instead of using cached data.")] = False,
    resolve_lightweight: Annotated[bool, Field(description="Load lightweight components before reading the tree (slower, complete).")] = True,
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
@_reject_unknown_arguments
def rename_components(
    part_paths: Annotated[list[str], Field(description="List of part files to rename; each is opened in turn.")],
    mapping: Annotated[dict | None, Field(description="How to map old names to new ones.")] = None,
    prefix: Annotated[str | None, Field(description="Prefix prepended to every renamed item.")] = None,
    suffix: Annotated[str | None, Field(description="Suffix appended to every renamed item.")] = None,
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
    assembly_path: Annotated[str | None, Field(description="Assembly file (.SLDASM), absolute path.")] = None,
    mode: Annotated[str, Field(description="Operating mode for the rename pass.")] = "file",
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
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
@_reject_unknown_arguments
def batch_export(
    targets: Annotated[list[str], Field(description="Files to export, one entry per input.")],
    output_dir: Annotated[str, Field(description="Directory for generated files; created if missing.")],
    fmt: Annotated[str, Field(description="Output format, e.g. 'pdf', 'dwg', 'dxf', 'step'.")] = "step",
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
) -> str:
    """Export parts/assemblies to STEP in one pass (dry-run by default).

    Read-only on the model: SaveAsSilent() behaves as an export and leaves the
    document path unchanged. Existing targets are reported as conflicts, never
    overwritten. PDF needs a .SLDDRW and DXF a flat pattern — those are rejected
    with a hint rather than guessed at.
    """
    return _json(export_tools.batch_export(targets, output_dir, fmt=fmt, apply=apply))


@mcp.tool()
@_reject_unknown_arguments
def set_view(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    view_name: Annotated[str, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")],
    x_m: Annotated[float | None, Field(description="Sheet X in metres.")] = None,
    y_m: Annotated[float | None, Field(description="Sheet Y in metres.")] = None,
    scale: Annotated[float | None, Field(description="View or sheet scale as a decimal (2.0 means 2:1).")] = None,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
    break_alignment: Annotated[bool, Field(description="Break the alignment to the parent view so this view can move freely.")] = False,
) -> str:
    """Move and/or rescale one view (sheet metres).

    Writes IView.Position through DISPATCH_PROPERTYPUT with a VARIANT array — a plain
    assignment silently lands the view elsewhere.

    `break_alignment=True` calls IView.RemoveAlignment first (the UI's 解除对齐), so a section
    view can leave the strip beside its parent. It clears THIS view's alignment only; views
    that inherit from it stay as they are.
    """
    return _json(layout.set_view(drawing_path, view_name, x_m=x_m, y_m=y_m,
                                 scale=scale, save=save,
                                 break_alignment=break_alignment))


@mcp.tool()
@_reject_unknown_arguments
def place_views(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], placements: Annotated[list, Field(description="Explicit [x, y] positions, one per view, in sheet metres.")], save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Place several views at once: [{view, x, y, scale}] in sheet metres."""
    return _json(layout.place_views(drawing_path, placements, save=save))


@mcp.tool()
@_reject_unknown_arguments
def delete_dimensions(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    keep_values: Annotated[list | None, Field(description="Values to keep when deleting; everything else matching the filter goes.")] = None,
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    keep_tolerance_mm: Annotated[float, Field(description="Two values closer than this are treated as the same dimension.")] = 0.05,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
) -> str:
    """Delete dimensions whose value is not in keep_values (the plan_drawing shortlist).

    SolidWorks has no bulk delete; each dimension is selected by position and removed with
    EditDelete. Measured 2026-10-29: 14 dimensions in ONE call took < 5 s.
    """
    return _json(layout.delete_dimensions(drawing_path, keep_values=keep_values,
                                          view_name=view_name,
                                          keep_tolerance_mm=keep_tolerance_mm, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_weld_symbol(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], weld_type: Annotated[str, Field(description="Weld symbol kind. Only 'fillet' is implemented; groove/plug/spot are refused rather than approximated.")] = "fillet", size_mm: Annotated[float, Field(description="Weld leg size in millimetres; the whole symbol scales with it.")] = 6.0,
                    x_m: Annotated[float, Field(description="Sheet X in metres.")] = 0.0, y_m: Annotated[float, Field(description="Sheet Y in metres.")] = 0.0,
                    target_x_m: Annotated[float | None, Field(description="Sheet X the leader points at, in metres; omit for no leader.")] = None, target_y_m: Annotated[float | None, Field(description="Sheet Y the leader points at, in metres.")] = None,
                    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Draw a fillet-weld symbol as native SKETCH LINES (reference line + triangle + size).

    **Read `representation` in the receipt: it is `sketch_block`, not a native weld
    annotation.** On this build `IWeldSymbol` stores the symbol name as text but never
    instantiates the geometry — `SetText` returns True, `GetText(2)` reads back "FILL", and
    the sheet shows only a leader and the number.

    WARNING — the sheet-line fallback DOES render, but it is not a weld feature. Measured
    2026-11-06 with a LINES-ONLY diff (the sheet is rendered after the sketch lines and
    BEFORE the size note is written, so the changed pixels can only be the geometry):
    `triangle_verified: true`, `lines_only_visibility {checked: true, changed_pixels: 2134}`.
    The previous version of this docstring said the opposite, and cited an error code
    (`weld_not_rendered`) that never existed in this package — the measurement behind that
    claim was a whole-page diff taken AFTER the note was added, so it could not separate the
    triangle from the note, and "the triangle is not drawn" was inferred from a test that
    could not show it either way.

    What is still true: this is sketch geometry on the sheet, not an `IWeldSymbol`. It does
    not update with the weldment feature and cannot be queried as a weld annotation. Read
    `triangle_verified` (a real measurement now) and `lines_only_visibility.checked` —
    `checked: false` means nothing was verified, which is not the same as "not drawn".
    Capability status: `weld_symbol: partial`.

    Only `weld_type="fillet"` is supported. A groove/plug/spot symbol is a different shape
    and is refused with `weld_type_unsupported` rather than approximated — a wrong weld
    callout is worse than a missing one.

    Geometry scales with `size_mm` (the weld leg): reference line 3.3x, triangle legs 1x,
    size text above-left. `target_x_m`/`target_y_m` add a leader to the joint. Verify by
    exporting a PDF — do not trust the receipt alone.
    """
    return _json(layout.add_weld_symbol(
        drawing_path, weld_type=weld_type, size_mm=size_mm, x_m=x_m, y_m=y_m,
        target_x_m=target_x_m, target_y_m=target_y_m, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_technical_requirements(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], lines: Annotated[list[str], Field(description="The requirement lines, one entry each. Lines already starting with '1.' or '1、' keep their number; the rest get ordinals.")],
                               x_m: Annotated[float | None, Field(description="Sheet X in metres.")] = None, y_m: Annotated[float | None, Field(description="Sheet Y in metres.")] = None,
                               height_mm: Annotated[float, Field(description="Note text height in millimetres (3.5 is the GB default).")] = 3.5, title: Annotated[str, Field(description="Heading placed above the list; empty string writes the lines without a heading.")] = "技术要求",
                               auto_place: Annotated[bool, Field(description="True walks the block up the left margin until it clears every view; False uses x_m/y_m as given.")] = True, save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Write a 技术要求 block (10-30 lines) as ONE note instead of N `add_note` calls.

    A note holds newlines, so the whole block goes in with a single InsertNote — that is
    what makes this fast. Numbering is automatic: lines already starting with `1.` or `1、`
    are kept as written, the rest get ordinals.

    `auto_place` (default) walks the block up the left margin until it clears every model
    view, using the real (deflated) outlines; pass `x_m`/`y_m` with `auto_place=False` to
    pin it. Errors: `bad_input` (empty lines) / `no_room_for_requirements`.

    Verified 2026-11-02: a 4-line block reported a 22.4 mm bbox and rendered as 4 lines.
    """
    return _json(layout.add_technical_requirements(
        drawing_path, lines, x_m=x_m, y_m=y_m, height_mm=height_mm, title=title,
        auto_place=auto_place, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_note(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], text: Annotated[str, Field(description="Note body. Ø ± ° are rewritten to SolidWorks format codes so DWG/DXF keeps them legible.")], x_m: Annotated[float, Field(description="Sheet X in metres.")], y_m: Annotated[float, Field(description="Sheet Y in metres.")],
    height_mm: Annotated[float | None, Field(description="Note text height in millimetres (3.5 is the GB default).")] = None, bold: Annotated[bool, Field(description="Render the note in bold.")] = False, save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
    avoid_collisions: Annotated[bool, Field(description="Walk the note down until it clears every model view.")] = False,
) -> str:
    """Add a drawing note (技术要求). x/y are sheet metres.

    `avoid_collisions=True` walks the note DOWN until it clears every model view, so the
    first line is not overprinted by a section arrow. The receipt reports
    `position_requested_m` vs `position_m` plus `collisions_avoided`.
    """
    return _json(layout.add_note(drawing_path, text, x_m, y_m,
                                 height_mm=height_mm, bold=bold, save=save,
                                 avoid_collisions=avoid_collisions))


@mcp.tool()
@_reject_unknown_arguments
def layout_dimensions(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    min_gap_m: Annotated[float, Field(description="Minimum gap to keep between dimension texts, in metres.")] = 0.006,
    cluster_tol_m: Annotated[float, Field(description="Dimensions within this distance are treated as one cluster, in metres.")] = 0.0015,
    dry_run: Annotated[bool, Field(description="True reports the plan without touching the document.")] = False,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
) -> str:
    """Pull apart dimensions that auto_dimension stacked on the same axis.

    Clusters by x (tolerance `cluster_tol_m`), sorts each cluster by y, pushes any closer
    than `min_gap_m` upward. Already-clear dimensions are not touched.
    Use dry_run=True first to see how many collisions it would fix.
    """
    return _json(drawing.layout_dimensions(drawing_path, view_name=view_name,
                                           min_gap_m=min_gap_m,
                                           cluster_tol_m=cluster_tol_m,
                                           dry_run=dry_run, save=save))


@mcp.tool()
@_reject_unknown_arguments
def delete_view(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")], save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Delete a drawing view by name.

    Create-only APIs meant one wrong `add_section_view` forced rebuilding the whole sheet.
    `view_name` comes from `drawing_audit` or `view_positions`.
    """
    return _json(layout.delete_view(drawing_path, view_name, save=save))


@mcp.tool()
@_reject_unknown_arguments
def delete_annotation(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], name: Annotated[str, Field(description="Annotation name exactly as reported by list_drawing_annotations.")], save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
                      position_m: Annotated[list[float] | None, Field(description="Exact sheet position [x, y] in metres. Required when several annotations share a name (SolidWorks reuses D1/D2).")] = None) -> str:
    """Delete one annotation (hole callout / GT / surface finish / note / dimension) by name.

    Names come from `list_drawing_annotations` (e.g. "细节项目272"). All four kinds are
    verified: note, geometric tolerance, surface finish and dimension. Success is measured
    across every view, because deleting a dimension does not change the sheet's own count.

    `position_m` disambiguates repeated names — SolidWorks reuses D1/D2/D3 per sketch, so
    one sheet can hold eight annotations called "D1"; pass the position the listing reported.
    """
    return _json(layout.delete_annotation(drawing_path, name, save=save,
                                          position_m=position_m))


@mcp.tool()
@_reject_unknown_arguments
def move_annotation(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], name: Annotated[str, Field(description="Annotation name exactly as reported by list_drawing_annotations.")], x_m: Annotated[float, Field(description="Sheet X in metres.")], y_m: Annotated[float, Field(description="Sheet Y in metres.")],
                    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True, position_m: Annotated[list[float] | None, Field(description="Exact sheet position [x, y] in metres. Required when several annotations share a name (SolidWorks reuses D1/D2).")] = None) -> str:
    """Move an annotation or dimension to a sheet position in metres.

    Returns `requested` vs `position_after_m` separately — section and detail views keep
    projection alignment and may refuse the requested spot.

    `position_m` disambiguates repeated names (see delete_annotation).
    """
    return _json(layout.move_annotation(drawing_path, name, x_m, y_m, save=save,
                                        position_m=position_m))


@mcp.tool()
@_reject_unknown_arguments
def set_dimension_position(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], name: Annotated[str, Field(description="Annotation name exactly as reported by list_drawing_annotations.")], x_m: Annotated[float, Field(description="Sheet X in metres.")], y_m: Annotated[float, Field(description="Sheet Y in metres.")],
                           save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
                           position_m: Annotated[list[float] | None, Field(description="Exact sheet position [x, y] in metres. Required when several annotations share a name (SolidWorks reuses D1/D2).")] = None) -> str:
    """Move ONE dimension to a sheet position in metres, by its reported name.

    P0-2: single-dimension counterpart to layout_dimensions' bulk auto-placement. Verifies
    the target is a dimension (not a note/GTOL), and reports value plus before/after
    positions. Use list_drawing_annotations first to get the `name`.

    `position_m` disambiguates repeated names (a sheet with 19 dimensions all called D1 is
    real). The implementation always supported it; the wrapper not forwarding it is what
    made "move exactly one D1" impossible from the MCP side.
    """
    return _json(layout.set_dimension_position(drawing_path, name, x_m, y_m,
                                             save=save, position_m=position_m))


@mcp.tool()
@_reject_unknown_arguments
def add_bom_table(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    configuration: Annotated[str, Field(description="BOM configuration name; null means the active configuration.")] = "Default",
    template: Annotated[str | None, Field(description="Drawing template path; null uses the built-in GB template.")] = None,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
) -> str:
    """Insert a POPULATED native BOM (materials list) into an assembly drawing.

    Returns 10 rows x 4 columns with a quantity column on the reference assembly.

    Verified on SW2024 SP4 only. On other builds `InsertBomTable4` may return None;
    `check_capabilities` reports the version-qualified status and `bom_native` includes
    the SW version in the failure message. Wrapper/implementation signature drift is
    guarded by `static_check.check_wrapper_signatures` (a regression assertion, not a
    runtime concern — see CONTRIBUTING).
    """
    return _json(layout.add_bom_table(drawing_path, view_name=view_name,
                                      configuration=configuration,
                                      template=template, save=save))


@mcp.tool()
@_reject_unknown_arguments
def list_sheets(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW. Absolute path.")],
) -> str:
    """List every sheet in a drawing: name, size, views, and which one is active.

    Only the ACTIVE sheet is edited by the other tools, so read this before a write when
    the document has more than one page. `add_sheet` makes the new page active, and that
    page has no views — which is how "dimension page 2" used to become a silent no-op.
    """
    return _json(layout.list_sheets(drawing_path))


@mcp.tool()
@_reject_unknown_arguments
def activate_sheet(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW. Absolute path.")],
    name: Annotated[str | None, Field(description="Sheet name to activate, e.g. '图纸2'. Give this or index.")] = None,
    index: Annotated[int | None, Field(description="1-based sheet index. Give this or name.")] = None,
    save: Annotated[bool, Field(description="Save the document after switching (default True).")] = True,
) -> str:
    """Make one sheet current. Every other tool edits the active sheet.

    `add_sheet` leaves the NEW page active; call this to go back to a page that has views
    before dimensioning, or to pick the page a view should be created on.
    """
    return _json(layout.activate_sheet(drawing_path, name=name, index=index, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_view_to_sheet(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW. Absolute path.")],
    sheet: Annotated[str | int | None, Field(description="Target sheet name or 1-based index. Omit to use the active sheet.")] = None,
    model_path: Annotated[str | None, Field(description="Model to derive the view from. Omit to reuse the model the drawing already references.")] = None,
    view_name: Annotated[str, Field(description="Standard view: '*前视' / '*上视' / '*右视' / '*左视' / '*后视' / '*等轴测'.")] = "*前视",
    x_m: Annotated[float, Field(description="Sheet X for the view centre, in metres.")] = 0.15,
    y_m: Annotated[float, Field(description="Sheet Y for the view centre, in metres.")] = 0.15,
    scale: Annotated[float | None, Field(description="View scale, e.g. 2.0 for 2:1. Omit to inherit the sheet scale.")] = None,
    save: Annotated[bool, Field(description="Save after creating the view (default True).")] = True,
) -> str:
    """Put a model view ON A NAMED SHEET (activating that sheet first).

    This is the missing half of multi-page output: `add_sheet` creates the page, this puts
    something on it. `CreateDrawViewFromModelView3` always draws on the active sheet, so
    without sheet activation a new page could never receive a view.
    """
    return _json(layout.add_view_to_sheet(drawing_path, sheet=sheet, model_path=model_path,
                                          view_name=view_name, x_m=x_m, y_m=y_m,
                                          scale=scale, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_sheet(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to add a sheet to. Absolute path.")], name: Annotated[str, Field(description="Name for the new sheet, e.g. '图纸2'. Omit to auto-name from the current sheet count.")] = None, paper: Annotated[str, Field(description="Paper size: A0/A1/A2/A3/A4 (default A3).")] = None, scale: Annotated[float, Field(description="Sheet scale, e.g. 1.0 for 1:1, 0.5 for 1:2.")] = 1.0, save: Annotated[bool, Field(description="Save the document after adding the sheet (default True).")] = True, first_angle: Annotated[bool, Field(description="First-angle projection for the new sheet (default True, matching GB).")] = True,
) -> str:
    """Add another sheet to an existing drawing, so one .SLDDRW can carry several pages.

    **Works.** I spent 14 versions (0.8.180–0.8.195) documenting this as impossible:
    "`IDrawingDoc.NewSheet4` is late-bound and pywin32 cannot infer its signature."
    Both halves of that were wrong — the type library was already generated on this
    machine, and I had guessed the argument ORDER. `TemplateIn` is the THIRD argument
    and it is an I4; I was passing `None`/`''` there, so COM rejected the type at the
    dispatch layer and SolidWorks never saw the call.

    Real signature:
        NewSheet4(Name, PaperSize, TemplateIn, Scale1, Scale2, FirstAngle,
                  TemplateName, Width, Height, PropertyViewName,
                  ZoneLeftMargin, ZoneRightMargin, ZoneTopMargin, ZoneBottomMargin,
                  ZoneRow, ZoneCol)

    Verified 2026-11-07 on 飞达动力轴: `('图纸1',)` -> `['图纸1', '图纸2']`.
    Falls back to `sheet_creation_blocked` only when the pywin32 cache is genuinely absent.
    """
    return _json(layout.add_sheet(drawing_path, name=name, paper=paper,
                                  scale=scale, save=save, first_angle=first_angle))


@mcp.tool()
@_reject_unknown_arguments
def measure_layout(
    drawing_path: Annotated[str, Field(description="Path to the .SLDDRW to measure.")],
    sheet: Annotated[str | None, Field(description="Sheet name to measure. Omit for the active sheet.")] = None,
) -> str:
    """Measure what the sheet actually uses: per-view boxes, the group, and the margins.

    Answers, on an EXISTING drawing: how much of the sheet the views occupy, whether they
    tile their bounding box or are scattered, and how much room is left between the
    outermost geometry and the frame — that last number is the budget the next dimension
    would have had.

    Read-only. Every box comes from `precise_outline` (constant 11.9 mm deflation), which
    this project measured to be the only reliable source; vertex-walking is both wrong and
    slow on real parts.
    """
    return _json(layout.measure_layout(drawing_path, sheet=sheet))


@mcp.tool()
@_reject_unknown_arguments
def add_sheet_line(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], x1_m: Annotated[float, Field(description="Sheet X of the line start, in metres.")], y1_m: Annotated[float, Field(description="Sheet Y of the line start, in metres.")], x2_m: Annotated[float, Field(description="Sheet X of the line end, in metres.")], y2_m: Annotated[float, Field(description="Sheet Y of the line end, in metres.")], save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,    verify_render: Annotated[bool, Field(description="Prove the line rendered by exporting the page before and after and diffing pixels (default True). Costs two PDF exports. Read the result: visibility.checked=true and changed_pixels>0 means it drew; changed_pixels=0 returns line_not_rendered; visibility.checked=false means the renderer was unavailable, which is NOT the same as 'not drawn'.")] = True,
    line_style: Annotated[str, Field(description="'solid' (default), 'dashed', 'dashdot' (GB fine dash-dot, use this for an axis / centre line), 'dashdotdot', or 'phantom'. The style is set on the sketch segment via late-bound IDispatch — the type library does not declare it, so it is easy to conclude wrongly that it does not exist.")] = "solid",
) -> str:
    """Draw a line in true sheet space (metres).

    Sheet sketches DO render — but only with `SketchManager.AddToDB = True` set before
    `CreateLine`. AddToDB disables snapping and inference; without it the stroke snaps to
    nearby geometry (or does not appear at all). Measured 2026-11-06 on SW2024 SP4: with it,
    `add_sheet_line(0.30,0.24 -> 0.35,0.24)` reports `changed_pixels: 572, visible: true`;
    the identical call before the change reported 0.

    (Earlier versions of this docstring said sheet sketches never render, based on a 0-pixel
    diff taken before AddToDB was set. That conclusion was wrong; the fault was the missing
    switch, not the capability.)

    Read `visibility`: `checked: true` with `changed_pixels > 0` means it drew;
    `changed_pixels: 0` returns `line_not_rendered`; **`checked: false` means the renderer
    was unavailable — that is "not verified", not "not drawn"**.

    Historical note (the unit trap that is still true):

    Drawing sketches take sheet metres, not millimetres: passing 236/62 produced a scaled
    fan of diagonals instead of a horizontal line.
    """
    return _json(layout.add_sheet_line(drawing_path, x1_m, y1_m, x2_m, y2_m, save=save, verify_render=verify_render, line_style=line_style))


@mcp.tool()
@_reject_unknown_arguments
def sheet_metal_qc(model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")], include_cylinders: Annotated[bool, Field(description="Include cylindrical faces in the quality report.")] = False) -> str:
    """Sheet-metal manufacturability checks: thickness, bend count, bend relief.

    Each check returns pass / fail / warn / not_checked. `not_checked` means it was NOT
    evaluated — geometry-distance checks (min flange, hole-to-bend) are not implemented yet
    and say so instead of returning a green tick.

    include_cylinders=True adds a cylinder radius inventory (every hole and bend fillet,
    grouped by radius). Costs about 90 extra seconds on a 556-face part, hence opt-in.
    """
    return _json(sheet_metal.sheet_metal_qc(model_path, include_cylinders=include_cylinders))


@mcp.tool()
@_reject_unknown_arguments
def flat_pattern_report(model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")]) -> str:
    """Where the flat-pattern size comes from — and what this toolkit did not do.

    The blank is computed by SolidWorks' SheetMetal engine. AssetMate only projects the
    flattened state into a view; it does not read the K-factor or bend table, does not
    verify the blank, and must not have process parameters invented on the drawing.
    """
    return _json(sheet_metal.flat_pattern_report(model_path))


@mcp.tool()
@_reject_unknown_arguments
def add_geometric_tolerance(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    x: Annotated[float, Field(description="Sheet X in metres.")],
    y: Annotated[float, Field(description="Sheet Y in metres.")],
    tolerance: Annotated[str, Field(description="Tolerance value. For add_drawing_dimension it is a length in metres; for add_geometric_tolerance it is the frame value as text.")] = "0.05",
    datum: Annotated[str, Field(description="Primary datum letter referenced by the frame.")] = "A",
    symbol: Annotated[str, Field(description="Geometric characteristic name, e.g. 'position', 'cylindricity'. Requires the 2022+ frame path.")] = "none",
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    edge_index: Annotated[int | None, Field(description="Index into list_drawing_edges for this view; lets you attach to an edge without sheet coordinates.")] = None,
    second_datum: Annotated[str, Field(description="Secondary datum letter, or '' for none.")] = "",
    third_datum: Annotated[str, Field(description="Tertiary datum letter, or '' for none.")] = "",
    leader_style: Annotated[str, Field(description="Leader style: 'none' (default, frame floats), 'straight', or 'bent' (GB/T habit: a short horizontal run out of the frame, then down to the arrow).")] = "none",
    leader_side: Annotated[str, Field(description="Which side the leader leaves the frame from: 'right' (default), 'left', or 'smart'.")] = "right",
    perpendicular: Annotated[bool, Field(description="Turn the leader perpendicular to the frame — this is what makes the GB/T elbow.")] = True,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
) -> str:
    """Insert a NATIVE geometric tolerance frame (形位公差).

    The API works on SW2026 — it only fails when pywin32 dispatches it as a property. This
    calls the same DISPID as DISPATCH_METHOD, which returns the Gtol object.

    symbol: none / position / flatness / perpendicularity / parallelism / concentricity /
    circularity / cylindricity / profile_line / profile_surface / angularity / symmetry /
    runout / total_runout. On SW2023+ this goes through the NATIVE GD&T path: the frame
    carries a real geometric symbol (measured `symbol_is_text: false`, `symbol_written:
    'IGTOL-PERP'`, PDF renders ⊥ / ⌖ / ▱). The older Chinese-text fallback is only used
    when the native write fails, and that case returns `gtol_symbol_not_applied` rather
    than a frame with no symbol.

    An earlier version of this docstring described the symbol as text, which contradicted
    its own first sentence and the `geometric_tolerance: verified` capability entry.
    Pass view_name + edge_index to attach the leader to geometry.
    """
    return _json(drawing.add_geometric_tolerance(
        drawing_path, x, y, tolerance=tolerance, datum=datum, symbol=symbol,
        view_name=view_name, edge_index=edge_index, second_datum=second_datum,
        third_datum=third_datum, leader_style=leader_style,
        leader_side=leader_side, perpendicular=perpendicular, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_datum_feature(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
                      edge_index: Annotated[int | None, Field(description="Index into list_drawing_edges for this view; lets you attach to an edge without sheet coordinates.")] = None, label: Annotated[str, Field(description="Letter identifying this item: the view letter for a section/detail (e.g. 'A' in A-A) or the datum letter for a datum feature symbol.")] = "A",
                      x_m: Annotated[float | None, Field(description="Sheet X in metres.")] = None, y_m: Annotated[float | None, Field(description="Sheet Y in metres.")] = None,
                      filled_triangle: Annotated[bool | None, Field(description="True draws a filled datum triangle, False hollow, null keeps the document default.")] = None, save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Add a DATUM FEATURE SYMBOL (基准要素符号) — the triangle plus letter that a
    geometric tolerance's datum actually points at.

    Why it matters: `add_geometric_tolerance(datum="A")` only writes the letter inside the
    frame. Without this tool that letter refers to nothing on the drawing, which under
    ISO/GD&T is an incomplete callout — a CMM operator has no datum to build a coordinate
    system from.

    It attaches to an EDGE, not to a free sheet point: `IModelDoc2.InsertDatumTag2` inserts
    at the current selection. Pass `view_name` + `edge_index` (indices come from
    `list_drawing_edges`); with `edge_index` omitted the first visible edge is used.

    Verified on SW 32.4.0: the tag reads back label 'B' by default and `SetLabel` takes
    effect. Errors: `view_not_found` / `datum_needs_edge` / `bad_edge_index` /
    `datum_attach_failed` / `datum_label_failed` / `datum_label_not_applied`.
    """
    return _json(drawing.add_datum_feature(
        drawing_path, view_name=view_name, edge_index=edge_index, label=label,
        x_m=x_m, y_m=y_m, filled_triangle=filled_triangle, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_surface_finish(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    x: Annotated[float, Field(description="Sheet X in metres.")],
    y: Annotated[float, Field(description="Sheet Y in metres.")],
    roughness: Annotated[str, Field(description="Ra value as text, e.g. '1.6'.")] = "3.2",
    sym_type: Annotated[str, Field(description="Surface-finish symbol type code.")] = "machining",
    leader_type: Annotated[str, Field(description="Leader style code.")] = "none",
    lay_symbol: Annotated[int, Field(description="Lay (texture direction) symbol code.")] = 0,
    arrow_type: Annotated[int, Field(description="Arrowhead style code.")] = 0,
    prod_method: Annotated[str, Field(description="Production method code (machined, cast, etc.).")] = "",
    sample_len: Annotated[str, Field(description="Sampling length shown with the surface-finish symbol.")] = "",
    min_roughness: Annotated[str, Field(description="Lowest Ra value to keep when auditing.")] = "",
    roughness_spacing: Annotated[str, Field(description="Spacing between multiple roughness symbols on the same edge, in metres.")] = "",
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
    verify_render: Annotated[bool, Field(description="Export and rasterise to prove the symbol actually appears on the sheet.")] = False,
    verify_dwg: Annotated[bool, Field(description="Also export DWG and check it is non-empty.")] = False,
) -> str:
    """Insert a NATIVE surface finish symbol (Ra value) at a sheet point.

    x/y are sheet metres; roughness is text like "3.2". Needs InsertSurfaceFinishSymbol3's
    full 14 arguments — a shorter call fails with "non-optional parameter".

    The API ignores x/y and drops the symbol at the sheet origin, so the tool moves it
    with SetPosition and reports the real `position_actual_m`. Set verify_render=True to
    diff a before/after PDF render at (x,y) and prove the symbol is visible on the sheet
    (`visibility.visible`); verify_dwg=True additionally checks a DWG export is non-empty
    (DWG is vector data, so visibility itself is proven through the PDF).
    """
    return _json(drawing.add_surface_finish(
        drawing_path, x, y, roughness=roughness, sym_type=sym_type,
        leader_type=leader_type, lay_symbol=lay_symbol, arrow_type=arrow_type,
        prod_method=prod_method, sample_len=sample_len,
        min_roughness=min_roughness, roughness_spacing=roughness_spacing, save=save,
        verify_render=verify_render, verify_dwg=verify_dwg,
    ))


@mcp.tool()
@_reject_unknown_arguments
def verify_dimension_attachments(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None) -> str:
    """Report what each dimension is attached to (edge / face / sketch segment / nothing).

    Dimensions with no attachment are untraceable and count as incomplete (process §6.1).
    """
    return _json(drawing.verify_dimension_attachments(drawing_path, view_name=view_name))


@mcp.tool()
@_reject_unknown_arguments
def prune_orphan_dimensions(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    dry_run: Annotated[bool, Field(description="True reports the plan without touching the document.")] = True, save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True, confirm: Annotated[bool, Field(description="Must be True to actually delete; without it the call only reports what it would delete.")] = False,
) -> str:
    """Delete dimensions attached to no geometry, which `delete_dimensions` cannot target.

    `auto_dimension` imports every driving dimension and some land unattached. Clearing them
    used to be impossible: `delete_dimensions` works by VALUE, so it cannot tell three 23 mm
    dimensions apart. This deletes by name + position instead.

    Dry-run by default — returns view / name / value / position per candidate. With
    `dry_run=False` it deletes them and RE-RUNS the attachment check, so `orphan_count_after`
    is measured, not assumed.

    `confirm=True` is required for an actual delete: `orphan` only means
    `GetAttachedEntities2` returned nothing, and dimensions moved with SetPosition can read
    as orphan while still leading to real geometry (a report found the 50 mm overall length
    in that list).
    """
    return _json(drawing.prune_orphan_dimensions(drawing_path, view_name=view_name,
                                                 dry_run=dry_run, save=save,
                                                 confirm=confirm))


@mcp.tool()
@_reject_unknown_arguments
def list_circular_edges(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    min_diameter_mm: Annotated[float, Field(description="Ignore circular edges smaller than this, in millimetres.")] = 0.0,
) -> str:
    """List circular edges of a view: sheet position + diameter. Use before callouts."""
    return _json(drawing.list_circular_edges(
        drawing_path, view_name=view_name, min_diameter_mm=min_diameter_mm))


@mcp.tool()
@_reject_unknown_arguments
def add_hole_callouts(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    min_diameter_mm: Annotated[float, Field(description="Ignore circular edges smaller than this, in millimetres.")] = 0.0,
    max_callouts: Annotated[int, Field(description="Upper bound on how many callouts to create in one call.")] = 40,
    budget_s: Annotated[float, Field(description="Soft time budget in seconds.")] = 25.0,
    leader_x_m: Annotated[float | None, Field(description="Sheet X of the leader tip, in metres.")] = None,
    leader_y_m: Annotated[float | None, Field(description="Sheet Y of the leader tip, in metres.")] = None,
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
) -> str:
    """Native hole callouts on a view's visible circular edges, verified by IsHoleCallout.

    Only this view's visible edges are enumerated (a whole-model scan took >400 s; one view
    has ~9 circular edges). Callouts are stacked to the right of the view by default; pass
    leader_x_m / leader_y_m (sheet metres) to pin the text inside the frame (P1-7).
    """
    return _json(drawing.add_hole_callouts(
        drawing_path, view_name=view_name, min_diameter_mm=min_diameter_mm,
        max_callouts=max_callouts, budget_s=budget_s,
        leader_x_m=leader_x_m, leader_y_m=leader_y_m, save=save))


@mcp.tool()
@_reject_unknown_arguments
def reconcile_ledger(
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    tolerance_mm: Annotated[float, Field(description="Matching tolerance in millimetres when reconciling ledger values.")] = 0.5,
) -> str:
    """Match the model's driving dimensions against the drawing's, and list what is missing.

    The §6.1 check that counting cannot do: returns coverage, unmatched ledger dimensions
    grouped by feature, and drawing-only values. Projection means the same edge dimensions
    differently per view, so treat the unmatched list as candidates to confirm.
    """
    return _json(drawing.reconcile_ledger(
        model_path, drawing_path, tolerance_mm=tolerance_mm))


@mcp.tool()
@_reject_unknown_arguments
def plan_dimension_gaps(
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to inspect and optionally modify. Absolute path; the file is opened and saved in place when apply_gaps=True.")],
    tolerance_mm: Annotated[float, Field(description="How close an edge-pair distance must be to a missing value to count as a candidate (millimetres).")] = 0.05,
    max_candidates_per_gap: Annotated[int, Field(description="How many matching edge pairs to report per gap (nearest first).")] = 3,
    apply_gaps: Annotated[bool, Field(description="Fill the `fillable` gaps (exactly one APPLICABLE candidate each). Default False returns a PLAN. Ambiguous gaps are never auto-filled — name them in apply_indices instead. Note apply_indices fills gaps WITHOUT this flag.")] = False,
    apply_indices: Annotated[list | None, Field(description="Explicit gaps to fill, 0-based indices into `gaps`. Use this for `ambiguous` entries: the tool will not choose between equally valid edge pairs on its own, but it will place the one you name.")] = None,
    apply_candidate: Annotated[int, Field(description="Which candidate to use, 0-based into `candidates` (nearest first). The DEFAULT 0 may be `applicable: false` (a circle centre) — check that gap's `applicable_candidates` first, or the call returns circle_centre_not_applicable. One index is shared by every gap in apply_indices.")] = 0,
) -> str:
    """PR4: turn truly-missing dimensions into edge-pair candidates, optionally fill them.

    Only `reconcile_ledger`'s `missing_truly` list is considered — values that are drawn but
    unattributed (`present_but_unmatched`) are NOT gaps and are not touched. `AddDimension2`
    takes only (x, y, z) and measures from the CURRENT SELECTION, so a gap (a value) has to
    be paired with geometry before anything can be drawn.

    Each gap lists its matching edge pairs and a status:
      * `fillable`            — exactly ONE applicable candidate; `apply_gaps=True` fills it
      * `ambiguous`           — two or more applicable candidates; nothing is chosen
      * `low_confidence`      — more than 50 edge pairs match this value, so a distance
                                test alone cannot tell the real one from a coincidence
                                (measured: a 50 mm gap matched 696 pairs). Never filled by
                                `apply_gaps`; use `apply_indices` after checking.
      * `no_applicable_candidate` — matches exist but none can be passed to
                                `add_distance_dimension` (see below)
      * `no_length_pair`      — the matching edge pair is degenerate (start == end) or not
                                parallel, so `AddDimension2` would build an ANGLE whose
                                radians read as millimetres (the 1570.796 phantom). Nothing
                                is inserted, and no other edge pair in this view fits either.
      * `no_geometry`         — no edge pair matches; that value needs a section or detail view
      * `no_value`            — the ledger entry carries no numeric value at all, so there
                                is nothing to search for

    Candidate applicability: a gap whose value is met by a CIRCLE CENTRE cannot be filled.
    Circle centres carry synthetic negative indices, while `add_distance_dimension` indexes a
    real edge array; passing one used to fail with a misleading `bad_edge_index`. Such
    candidates are now tagged `candidate_kind: "circle_centre"` and `applicable: false`, and
    each gap reports `applicable_candidates` — the indices that WILL work.

    Filling:
      * `apply_gaps=True` fills only the `fillable` ones.
      * `apply_indices=[n, ...]` fills the gaps you name, including `ambiguous` ones, and
        does NOT require `apply_gaps=True`. Selecting an `applicable: false` candidate
        returns `circle_centre_not_applicable` with that gap's `applicable_candidates`.
      * `apply_candidate` is a SINGLE index applied to every gap in `apply_indices`; the
        candidates differ per gap, so multi-gap calls with different needs take more than
        one call.

    Candidate fields: `measured_mm` is the SHEET distance the match was made on, and
    `model_span_mm` is a 3D model distance between the pair's vertices — a different
    quantity, because a drawing is a projection. `model_check` says which case you have:
    `approximate_3d_distance` (the figure exists, treat it as a hint), or `unavailable`
    (the model point could not be projected, so the candidate carries NO model evidence).
    **`model_span_mm` is never used to filter** — an earlier version did, and it dropped
    correct candidates on every part (a 3.05 mm axial gap can be tens of mm apart in the
    model's depth direction). The real value is checked after insertion instead: see
    `actual_value_mm` below.

    Top-level counters: `candidates_total`, `candidates_applicable`,
    `candidates_circle_centre`, `candidates_skipped`. Note that `max_candidates_per_gap`
    truncates the list BEFORE these are counted — each gap reports
    `candidates_matched_before_truncation` and `candidates_truncated`, so a gap showing
    `no_applicable_candidate` with `candidates_truncated: true` may still be solvable by
    raising `max_candidates_per_gap`. `candidates_skipped` counts only candidates that an
    APPLY attempt refused — in the default plan-only mode it stays 0.
    """
    return _json(drawing.plan_dimension_gaps(
        model_path, drawing_path, tolerance_mm=tolerance_mm,
        max_candidates_per_gap=max_candidates_per_gap, apply_gaps=apply_gaps,
        apply_indices=apply_indices, apply_candidate=apply_candidate))


@mcp.tool()
@_reject_unknown_arguments
def survey_drawings(
    folder: Annotated[str, Field(description="Directory of drawings to survey.")],
    sample_size: Annotated[int, Field(description="How many drawings to include in the survey sample.")] = 100,
    seed: Annotated[int, Field(description="Random seed, so a sample can be reproduced.")] = 28,
    html_path: Annotated[str | None, Field(description="Where to write the survey report (HTML).")] = None,
    pattern: Annotated[str, Field(description="Glob pattern used to find drawings under the survey root, e.g. '*.SLDDRW'.")] = "*.SLDDRW",
) -> str:
    """Audit a random sample of real drawings and report the statistical baseline.

    Returns distributions (views / dimensions / notes / center marks) and the share of
    drawings carrying surface finish, geometric tolerance and balloons — the numbers every
    verdict threshold should be based on. Pass html_path to also write a standalone page.

    Each drawing is closed right after it is read: SolidWorks refuses to open anything once
    ~40 documents are loaded, and one drawing takes 4-11 s, so sampling beats full scans.
    """
    return _json(survey.survey_drawings(
        folder, sample_size=sample_size, seed=seed, html_path=html_path,
        pattern=pattern,
    ))


@mcp.tool()
@_reject_unknown_arguments
def delivery_check(
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    ai_supplied: Annotated[list[str] | None, Field(description="Values you decided yourself (not derivable from the model); listed back so the receipt separates fact from judgement.")] = None,
    require_gdt: Annotated[bool, Field(description="Treat a missing geometric tolerance as a pending item.")] = False,
    strict_attachments: Annotated[bool, Field(description="Count every detached dimension as pending, without the orphan-suspected excuse.")] = False,
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
@_reject_unknown_arguments
def list_drawing_annotations(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None) -> str:
    """Inventory every annotation: dimensions (with value + tolerance), notes,
    geometric tolerances (frame content), center marks, hole callouts.

    Use it to prove what a drawing does or does not carry — e.g. "no geometric
    tolerance anywhere" — which the CAD API cannot create but can read.
    """
    return _json(drawing.list_drawing_annotations(drawing_path, view_name=view_name))


@mcp.tool()
@_reject_unknown_arguments
def add_dimension_tolerance(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    value_mm: Annotated[float, Field(description="Driving dimension value in millimetres to match against.")],
    # Enum names, not codes — the old text said "4 = symmetric/bilateral", which mixes two
    # different swTolType_e values and invites a caller to pass a raw number. Codes are
    # swTolNone 0 / BASIC 1 / BILAT 2 / LIMIT 3 / SYMMETRIC 4.
    tolerance_type: Annotated[str, Field(description="Tolerance style: none / basic / bilateral (two deviations, most common) / limit (two limits) / symmetric (±) / min / max / fit / fit_with_tolerance.")] = "bilateral",
    # Report 2026-11-07 (P2): these said "in metres" while the top-level docstring says
    # millimetres. The implementation divides by 1000, so MILLIMETRES is correct — the
    # parameter text was the stale one. A caller who believed it would pass 0.05 for 50 µm.
    upper: Annotated[float, Field(description="Upper deviation, in MILLIMETRES (e.g. 0.05 for +0.05).")] = 0.1,
    lower: Annotated[float, Field(description="Lower deviation, in MILLIMETRES (e.g. 0.05 for -0.05).")] = 0.1, verify_render: Annotated[bool, Field(description="Export the sheet to PDF and read the text layer, reporting what the tolerance ACTUALLY prints as (adds ~2 s). Use this to catch a readback that is right while the figure is wrong.")] = False, tolerance_precision: Annotated[int, Field(description="Decimal places for the tolerance only (default 3). Metric fits need three: H7 +0.018 and H8 +0.033 both print as '0.02' / '0.03' at two, which is unusable. The dimension VALUE precision is left untouched.")] = 3,
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
) -> str:
    """Apply a NATIVE tolerance to an existing drawing dimension, located by its value in mm.

    tolerance_type: bilateral | symmetric | limit | basic | min | max | fit |
    fit_with_tolerance | none. upper/lower in millimetres. Dry-run by default.
    """
    return _json(drawing.add_dimension_tolerance(
        drawing_path, value_mm, tolerance_type=tolerance_type, upper=upper,
        lower=lower, view_name=view_name, apply=apply, verify_render=verify_render,
        tolerance_precision=tolerance_precision,
    ))


@mcp.tool()
@_reject_unknown_arguments
def material_library(database: Annotated[str, Field(description="Material database to read from (e.g. solidworks materials); null uses the default.")] = "SolidWorks Materials.sldmat") -> str:
    """List material names available in the installed SolidWorks material library."""
    return _json(material_tools.material_library(database=database))


@mcp.tool()
@_reject_unknown_arguments
def set_part_material(
    paths: Annotated[list[str], Field(description="Part file paths, one entry per item; each is opened and written.")],
    material_name: Annotated[str, Field(description="Material to assign, e.g. '普通碳钢'.")],
    database: Annotated[str, Field(description="Material database to read from (e.g. solidworks materials); null uses the default.")] = "SolidWorks Materials.sldmat",
    config: Annotated[str, Field(description="Configuration name; null means the active configuration.")] = "",
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
) -> str:
    """Assign a material to one or more parts; returns previous material and mass.

    Dry-run by default. Without a material SolidWorks uses 1000 kg/m³, so BOM masses are
    wrong until this is set.
    """
    return _json(material_tools.set_part_material(
        paths, material_name, database=database, config=config, save=save, apply=apply,
    ))


@mcp.tool()
@_reject_unknown_arguments
def assign_materials(
    paths: Annotated[list[str], Field(description="Part file paths, one entry per item; each is opened and written.")],
    rules: Annotated[list | None, Field(description="Per-part material assignment rules.")] = None,
    database: Annotated[str, Field(description="Material database to read from (e.g. solidworks materials); null uses the default.")] = "SolidWorks Materials.sldmat",
    config: Annotated[str, Field(description="Configuration name; null means the active configuration.")] = "",
    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True,
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
) -> str:
    """Pick a material per part from name keywords (标准件→碳钢, 结构件→铝合金), then assign.

    The chosen material is an AI-supplied value: it is reported per part with its reason
    and should be listed as pending confirmation in the delivery record.
    """
    return _json(material_tools.assign_materials(
        paths, rules=rules, database=database, config=config, save=save, apply=apply,
    ))


@mcp.tool()
@_reject_unknown_arguments
def build_feature_ledger(
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    include_faces: Annotated[bool, Field(description="Read per-face data too (slower, needed for face-level checks).")] = True,
    config: Annotated[str | None, Field(description="Configuration name; null means the active configuration.")] = None,
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
@_reject_unknown_arguments
def create_drawing(
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    output_path: Annotated[str | None, Field(description="Where to write the result. Extension decides the format when fmt is omitted.")] = None,
    template: Annotated[str | None, Field(description="Drawing template path; null uses the built-in GB template.")] = None,
    views: Annotated[list[str] | None, Field(description="Which standard views to create, in order, e.g. ['front', 'right', 'isometric'].")] = None,
    scale: Annotated[float | None, Field(description="View or sheet scale as a decimal (2.0 means 2:1).")] = None,
    auto_fit: Annotated[bool, Field(description="Shrink the view scale until every view fits the sheet inner box.")] = True,
    view_scales: Annotated[dict | None, Field(description="Per-view scale, keyed by view name; overrides the sheet scale.")] = None,
    paper: Annotated[str, Field(description="Sheet size, e.g. 'A3' or 'A4'; drives the template and the usable area.")] = "A3",
    view_positions: Annotated[list | None, Field(description="Per-view sheet positions [x, y] in metres, keyed by view name.")] = None,
    projection: Annotated[str, Field(description="Projection convention: 'first' (GB/T 14692, default) or 'third' (ISO/US). Chooses where the top view and the side view are placed.")] = "first",
) -> str:
    """Create a native .SLDDRW from a part/assembly, add model views, optionally save.

    views: front/back/left/right/top/bottom/isometric (default front+top+isometric).
    View names are resolved against the model, so Chinese (`*前视`) and English
    (`*Front`) SolidWorks both work. Units are meters; an A3 sheet is 0.42 x 0.297.
    view_positions: one sheet position per view, in metres, e.g.
    [[0.10, 0.20], [0.28, 0.20]] — same order as `views`. Omit to let auto_fit place them.
    """
    return _json(drawing.create_drawing(
        model_path, output_path=output_path, template=template,
        views=views or ["front", "top", "isometric"], scale=scale, auto_fit=auto_fit,
        view_scales=view_scales, paper=paper, view_positions=view_positions,
        projection=projection,
    ))


@mcp.tool()
@_reject_unknown_arguments
def export_drawing(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], output_path: Annotated[str, Field(description="Where to write the result. Extension decides the format when fmt is omitted.")], fmt: Annotated[str | None, Field(description="Output format, e.g. 'pdf', 'dwg', 'dxf', 'step'.")] = None) -> str:
    """Export a .SLDDRW to PDF / DWG / DXF.

    PDF can only come from a drawing — for STEP/STL of models use batch_export.
    """
    return _json(drawing.export_drawing(drawing_path, output_path, fmt=fmt))


@mcp.tool()
@_reject_unknown_arguments
def render_drawing(pdf_path: Annotated[str, Field(description="PDF to rasterise into PNG pages.")], output_dir: Annotated[str | None, Field(description="Directory for generated files; created if missing.")] = None, scale: Annotated[float, Field(description="View or sheet scale as a decimal (2.0 means 2:1).")] = 2.0) -> str:
    """Render each PDF page to PNG so the drawing can be visually checked.

    Requires pypdfium2 (pip install pypdfium2). Returns one PNG per page.
    """
    return _json(drawing.render_drawing(pdf_path, output_dir=output_dir, scale=scale))


@mcp.tool()
@_reject_unknown_arguments
def add_section_view(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    orientation: Annotated[str, Field(description="'horizontal' cuts left-right, 'vertical' cuts top-bottom.")] = "horizontal",
    offset_m: Annotated[float, Field(description="Shift the cut line off the view centre, in sheet metres.")] = 0.0,
    position: Annotated[list[float] | None, Field(description="Sheet position [x, y] in metres for the new view; null lets the tool choose.")] = None,
    label: Annotated[str, Field(description="View letter, e.g. 'A' for A-A.")] = "A",
    flip: Annotated[bool, Field(description="Reverse the viewing direction of the section.")] = False,
    depth: Annotated[float, Field(description="Partial-section depth in metres; 0 gives a full section.")] = 0.0,
    cut_plane_normal: Annotated[list[float] | None, Field(description="Model-space plane normal; pass together with cut_plane_origin to cut an explicit plane.")] = None,
    cut_plane_origin: Annotated[list[float] | None, Field(description="A point on the cut plane, in model metres.")] = None,
    verify_cut: Annotated[bool, Field(description="Check the result and return section_empty / section_suspicious / hatch_missing instead of a silent wrong cut.")] = True,
    cut_line_ratio: Annotated[float, Field(description="Cut-line length as a fraction of the view; clamped to half the SHORT edge so arrows stay on the geometry. Lower it for very flat views.")] = 0.55,
    align_to_parent: Annotated[bool, Field(description="Keep the section in projection with its parent view (default True — correct drawing practice). A section is created aligned, so a requested y is silently rewritten to stay on the parent's axis; set False to break the alignment and honour the position you asked for. The receipt shows alignment_kept / position_axis_ignored / alignment_broken either way.")] = True,
) -> str:
    """Create a SECTION VIEW through a model view (exposes internal features).

    The cut line is drawn in the view's sketch automatically. orientation is
    "horizontal" or "vertical"; offset_m shifts it off the view centre; flip reverses
    the viewing direction; depth > 0 gives a partial section.

    cut_plane_normal / cut_plane_origin (MODEL metres, pass together) override
    orientation + offset_m with an explicit axis-aligned plane; unmappable or oblique
    planes are refused with a structured error instead of cutting the wrong plane.

    cut_line_ratio (default 0.55) sizes the cut line: it is clamped to half the view's
    SHORT edge as well, so the arrows stay inside the view instead of spilling onto the
    technical-requirements block. Shrink it further for very flat views.

    verify_cut (default True) checks the result. Structured errors, all of which leave the
    view on the sheet for inspection:
      `section_empty`       - the cut produced 0 visible edges (line missed the geometry)
      `section_suspicious`  - kept <10% of the source view's width/height
      `hatch_missing`       - the cut face has NO hatching (GetFaceHatchCount == 0); a
                              section without hatching is unusable on a shop floor, so
                              0.8.56 turned this from ok:true into an error.
    """
    return _json(drawing.add_section_view(
        drawing_path, view_name=view_name, orientation=orientation,
        offset_m=offset_m, position=position, label=label, flip=flip, depth=depth,
        cut_plane_normal=cut_plane_normal, cut_plane_origin=cut_plane_origin,
        verify_cut=verify_cut, cut_line_ratio=cut_line_ratio,
        align_to_parent=align_to_parent))


@mcp.tool()
@_reject_unknown_arguments
def add_detail_view(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    center: Annotated[list[float] | None, Field(description="Sheet centre [x, y] of the detail boundary, in metres.")] = None,
    radius_m: Annotated[float, Field(description="Radius of the detail boundary, in metres.")] = 0.02,
    position: Annotated[list[float] | None, Field(description="Sheet position [x, y] in metres for the new view; null lets the tool choose.")] = None,
    scale: Annotated[float, Field(description="View or sheet scale as a decimal (2.0 means 2:1).")] = 2.0,
    label: Annotated[str, Field(description="View letter, e.g. 'A' for A-A.")] = "A",
    style: Annotated[int, Field(description="Detail-view boundary style code.")] = 0,
    shape: Annotated[int, Field(description="Detail-view boundary shape (0 = circle).")] = 0,
    flip: Annotated[bool, Field(description="Reverse the viewing direction of the section.")] = False,
    show_label: Annotated[bool, Field(description="Show the detail label next to the view.")] = True,
    circle_segments: Annotated[int, Field(description="Segments used to approximate a circular boundary; higher is smoother.")] = 32,
) -> str:
    """Add a DETAIL (enlarged) view around a circular area of a model view.

    center/radius_m are sheet metres (default: the view centre, 20 mm radius).
    scale is the REQUESTED detail scale (2.0 = 2:1); it is no longer overwritten by the
    parent view's scale. circle_segments must be > 0 — passing 0 is what makes
    SolidWorks reject the call. Returns error(detail_empty) if the new view has zero
    visible edges, and error(detail_scale_locked) if SolidWorks refuses the requested
    scale — neither is reported as a silent ok:true anymore.
    """
    return _json(drawing.add_detail_view(
        drawing_path, view_name=view_name, center=center, radius_m=radius_m,
        position=position, scale=scale, label=label, style=style, shape=shape,
        flip=flip, show_label=show_label, circle_segments=circle_segments,
    ))


@mcp.tool()
@_reject_unknown_arguments
def add_flat_pattern_view(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    model_path: Annotated[str, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")],
    config_name: Annotated[str | None, Field(description="Configuration to use; null means the active one.")] = None,
    position: Annotated[list[float] | None, Field(description="Sheet position [x, y] in metres for the new view; null lets the tool choose.")] = None,
    scale: Annotated[float | None, Field(description="View or sheet scale as a decimal (2.0 means 2:1).")] = None,
    hide_bend_lines: Annotated[bool, Field(description="Hide bend lines in the flat-pattern view.")] = False,
    flip: Annotated[bool, Field(description="Reverse the viewing direction of the section.")] = False,
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
@_reject_unknown_arguments
def survey_dimensions_by_view(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")]) -> str:
    """Report how dimensions are spread across the model views — READ ONLY, changes nothing.

    Use it to answer "why is the right view empty": it lists each view with its dimension
    count and names the busiest one and the empty ones.

    Measured on 气缸盖 2026-11-03, a claim that every dimension piles into the first view
    does NOT reproduce — `auto_dimension(all_views=True)` gives 20/10/9 across three views
    (and 20/10/9/0 with an isometric added). The first view carries the most because
    SolidWorks puts each dimension in a view that can show it. An isometric view having
    none is normal; a right/top view having none usually means the sheet lacks the
    geometry that dimension needs.

    **There is no API to re-assign a dimension to a different view.** An annotation belongs
    to its view; `SetPosition` only moves it on the sheet. `all_views=False` inserts only
    what the ACTIVE view can show, so it cannot redistribute either. This tool diagnoses;
    it does not redistribute.
    """
    return _json(drawing.survey_dimensions_by_view(drawing_path))


@mcp.tool()
@_reject_unknown_arguments
def auto_dimension(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    all_views: Annotated[bool, Field(description="Insert into every model view. False inserts into the ACTIVE view only and activates one for you.")] = True,
    include_unmarked: Annotated[bool, Field(description="Also bring in dimensions not marked for drawing (more complete, noisier).")] = False,
    eliminate_duplicates: Annotated[bool, Field(description="Drop duplicate dimensions by full name after insertion.")] = True,
    dimension_filter: Annotated[list | None, Field(description="Keep only these dimensions. Pass plan_drawing()['keep_dimensions'] AS-IS (a list of dicts with feature/value_mm) — mapping it to bare full-name strings loses the value, and model vs sheet feature names differ (Sketch1 vs 草图2).")] = None,
    place_outside: Annotated[bool, Field(description="Push every inserted dimension just outside its view. Defaults to TRUE — leaving them inside covers the part with dimension lines (measured 2026-11-03: 18 dimensions all landed inside the outline, sheet unusable). Pass False for the old behaviour.")] = True,
    outside_gap_m: Annotated[float, Field(description="Distance from the view outline to the first dimension, in metres.")] = 0.012,
    outside_step_m: Annotated[float, Field(description="Spacing between stacked dimensions, in metres.")] = 0.008,
    prefer_view: Annotated[str, Field(description="Which view KEEPS a dimension that appears in several views. Default keeps the first inserted (usually the main view), which strips a section view — the only place a depth dimension can live — of its copies. Pass the section view's name to keep them there.")] = None,
    audit: Annotated[bool, Field(description="Run the post-insert audit (overlaps, off-sheet, unresolved text) as part of this call. Costs a second pass over every annotation.")] = True,
    detect_same_value: Annotated[bool, Field(description="Group dimensions by (value, type) to flag same-value duplicates. OFF by default: it reads every annotation on every view and measured 18.4 s of auto_dimension's 27.0 s (68%) on 旋转轴. It only reports — it never deletes. Turn on when a sheet shows two identical values and you need the candidates.")] = False,
    sheet: Annotated[str | int | None, Field(description="Target sheet: name (e.g. '图纸2') or 1-based index. Omit to use whatever page is active. Every write edits the ACTIVE page, so pass this rather than relying on a previous activate_sheet call.")] = None,
) -> str:
    """Insert the model's driving dimensions into the drawing (Insert Model Items).

    Marked-for-drawing dimensions only by default; include_unmarked=True brings in
    every driving dimension. Returns inserted count and the post-insert audit.

    dimension_filter: `plan_drawing()["keep_dimensions"]`. Everything inserted that the
    plan did not keep is deleted afterwards, so "keep 8 of 41" really ends with ~8
    dimensions. Aborts without deleting if nothing matches the filter.

    place_outside=True moves everything this call inserted to just outside its view, stacked
    by side (gap + n × step). Use it instead of scaling the view up to hide overprinting:
    SolidWorks' own ~10 mm offset does not scale with the part.
    """
    return _json(drawing.auto_dimension(
        drawing_path, all_views=all_views, include_unmarked=include_unmarked,
        eliminate_duplicates=eliminate_duplicates, dimension_filter=dimension_filter,
        place_outside=place_outside, outside_gap_m=outside_gap_m,
        outside_step_m=outside_step_m, audit=audit,
        detect_same_value=detect_same_value, prefer_view=prefer_view, sheet=sheet))


@mcp.tool()
@_reject_unknown_arguments
def list_drawing_edges(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
                       include_points: Annotated[bool, Field(description="Include start/end coordinates. False returns indices only and is much faster.")] = True,
    flip_x: Annotated[bool, Field(description="Mirror every sheet X about the view centre. Use when a side view comes back left-right reversed — verify by exporting a PDF and measuring one known feature, then pass true. Measured 2026-11-03 on 旋转轴 at 2:1: unflipped reported the Ø14 collar at x=0.1288 while the page has it at 0.0487; flipped matches.")] = False,
) -> str:
    """List a view's visible edges with stable indices, model coordinates AND sheet coordinates.

    Since 0.8.52 each edge carries `sheet_start` / `sheet_end` in SHEET METRES, projected
    through the view, so they can be fed straight to add_drawing_dimension(x=, y=) — the
    old model-only output could not be aimed at the sheet. The top-level `sheet_projected`
    says whether that projection happened; when it is false, `sheet_projection_reason`
    names the cause (typically `axes_ambiguous` on a part whose two projected axes are
    equal length, e.g. a turned shaft).

    `include_points=False` returns indices only (much faster); coordinates are what makes
    a large view slow.
    """
    return _json(drawing.list_drawing_edges(drawing_path, view_name=view_name,
                                        include_points=include_points, flip_x=flip_x))


@mcp.tool()
@_reject_unknown_arguments
def add_distance_dimension(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], edge_a: Annotated[int, Field(description="Index of the FIRST edge (from list_drawing_edges) used to measure the gap.")], edge_b: Annotated[int, Field(description="Index of the SECOND edge; the dimension measures the distance between the two.")],
                           x_m: Annotated[float, Field(description="Sheet X in metres.")], y_m: Annotated[float, Field(description="Sheet Y in metres.")],
                           view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
                           save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = False) -> str:
    """Dimension the distance BETWEEN two edges — how you get an overall length.

    `add_drawing_dimension` hits ONE edge and reports that edge's own length, so a shaft
    made of 18+29+3+10 mm segments could never be given its 60 mm total: no single edge in
    the view is 60 mm long. This picks two edges and dimensions the gap between them.

    Verified 2026-11-03 on 旋转轴 (right view, 7 edges): edge 3 with edge 6 yields exactly
    **60.0 mm**; other pairs give 18/42/47/8 as expected.

    `edge_a` / `edge_b` are indices from `list_drawing_edges`. **Which pair spans the
    overall length depends on the view** — there is no way to guess it, so read the edge
    list, pick the two outermost parallel ones, and check `value_mm` against what you
    expect. The receipt returns the value it actually created rather than assuming.
    """
    return _json(drawing.add_distance_dimension(
        drawing_path, edge_a, edge_b, x_m, y_m, view_name=view_name, save=save))


@mcp.tool()
@_reject_unknown_arguments
def add_drawing_dimension(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    x: Annotated[float, Field(description="Sheet X in metres.")],
    y: Annotated[float, Field(description="Sheet Y in metres.")],
    value: Annotated[float | None, Field(description="Explicit dimension value in metres. Omit to keep the measured value (safer: a value here drives the model).")] = None,
    offset_x: Annotated[float, Field(description="Offset applied to the dimension text, in metres (positive = right).")] = 0.0,
    offset_y: Annotated[float, Field(description="Offset applied to the dimension text, in metres (negative = below the line).")] = -0.015,
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
    tolerance: Annotated[float, Field(description="Tolerance value. For add_drawing_dimension it is a length in metres; for add_geometric_tolerance it is the frame value as text.")] = 0.01,
    edge_index: Annotated[int | None, Field(description="Index into list_drawing_edges for this view; lets you attach to an edge without sheet coordinates.")] = None,
    skip_duplicates: Annotated[bool, Field(description="Refuse to add a dimension that duplicates an existing one on the sheet (same value, same view). Default True — a second identical dimension reads as a drafting error.")] = True,
    style: Annotated[str, Field(description="Callout form for an arc or circle: 'radius' (R), 'diameter' (Ø), 'linear' (plain), or 'auto' to let SolidWorks decide. A semicircular groove projects as an ARC and 'auto' measures it as a DIAMETER, which is a meaningless callout on a turned part — pass 'radius' for R. The receipt's `style_used` says which one was actually created; if SolidWorks refuses the radial call it falls back to linear and says so.")] = "auto",
) -> str:
    """Add ONE dimension on a projected edge at sheet point (x, y) in meters.

    Manual dimensioning path: InsertModelItems can only carry dimensions the model
    already has, so IR-rebuilt parts need this. value is meters; omit to keep the
    computed value. Selection falls back EDGE -> SILHOUETTE -> SKETCHSEGMENT.
    """
    return _json(drawing.add_drawing_dimension(
        drawing_path, x, y, value=value, offset_x=offset_x, offset_y=offset_y,
        view_name=view_name, tolerance=tolerance, edge_index=edge_index,
        skip_duplicates=skip_duplicates, style=style,
    ))


@mcp.tool()
@_reject_unknown_arguments
def set_view_scale(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")], scale: Annotated[float, Field(description="View or sheet scale as a decimal (2.0 means 2:1).")],
                   save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Change one view's scale on an existing drawing (no need to rebuild the sheet)."""
    return _json(drawing.set_view_scale(drawing_path, view_name, scale, save=save))


@mcp.tool()
@_reject_unknown_arguments
def move_view(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], view_name: Annotated[str, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")], x_m: Annotated[float, Field(description="Sheet X in metres.")], y_m: Annotated[float, Field(description="Sheet Y in metres.")],
              save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Move one existing view to a sheet point (metres)."""
    return _json(drawing.move_view(drawing_path, view_name, x_m, y_m, save=save))


@mcp.tool()
@_reject_unknown_arguments
def close_document(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")]) -> str:
    """Close a document SolidWorks still holds open (frees the file for delete/replace).

    Tools here open documents and never closed them; a report ended up with two abandoned
    drafts it could not delete ("Device or resource busy").
    """
    return _json(drawing.close_document(drawing_path))


@mcp.tool()
@_reject_unknown_arguments
def add_center_marks(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    include_slots: Annotated[bool, Field(description="Also mark slots, not just round holes.")] = True,
    extended_lines: Annotated[bool, Field(description="Draw the centre-mark cross lines beyond the hole edge.")] = True,
    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
) -> str:
    """Auto-insert center marks and centerlines on every hole/slot in every view.

    Returns views_marked (reliable) rather than a mark count, which under-reports
    on this binding.
    """
    return _json(drawing.add_center_marks(drawing_path, include_slots=include_slots, extended_lines=extended_lines, view_name=view_name))


@mcp.tool()
@_reject_unknown_arguments
def add_hole_callout(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")], x: Annotated[float, Field(description="Sheet X in metres.")], y: Annotated[float, Field(description="Sheet Y in metres.")],
                    view_name: Annotated[str | None, Field(description="Model view to target. null means the first model view (the sheet itself is skipped).")] = None,
                    model_path: Annotated[str | None, Field(description="Part or assembly to work on (.SLDPRT / .SLDASM), absolute path.")] = None,
                    save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True) -> str:
    """Insert a native hole callout at a sheet point (meters) on a hole edge.

    Returns callout_failed when the point is not on a hole's projected circle —
    a missing callout is reported, never faked.
    """
    return _json(drawing.add_hole_callout(
        drawing_path, x, y, view_name=view_name, model_path=model_path, save=save,
    ))


@mcp.tool()
@_reject_unknown_arguments
def set_title_block(
    drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")],
    name: Annotated[str | None, Field(description="Annotation name exactly as reported by list_drawing_annotations.")] = None,
    code: Annotated[str | None, Field(description="Drawing number to write into the title block.")] = None,
    material: Annotated[str | None, Field(description="Material text for the title block.")] = None,
    apply: Annotated[bool, Field(description="False (default) is a dry run that reports what WOULD change; True actually writes.")] = False,
    include_reversed: Annotated[bool, Field(description="Also update the 180-degree-rotated title-block copy in the sheet corner.")] = True,
    scale: Annotated[float | None, Field(description="Sheet scale to write into the 比例 cell (2.0 means 2:1). Omit to leave the cell alone; the sheet's own ScaleDecimal is the usual source.")] = None,
    sheet: Annotated[str | int | None, Field(description="Target sheet: name or 1-based index. Omit to use the active page. A title block usually exists on every page, so be explicit when the drawing has several.")] = None,
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
        include_reversed=include_reversed, scale=scale, sheet=sheet,
    ))


@mcp.tool()
@_reject_unknown_arguments
def drawing_audit(drawing_path: Annotated[str, Field(description="Existing .SLDDRW to modify. Absolute path; the file is opened and saved in place unless save=False.")]) -> str:
    """Audit a drawing: views, scales, blank views, dimension and annotation counts.

    Evidence only — it never judges view choice, tolerances or manufacturability.
    Blank views and zero dimensions are reported as warnings, not silently passed.
    """
    return _json(drawing.drawing_audit(drawing_path))


@mcp.tool()
@_reject_unknown_arguments
def get_assembly_tree(assembly_path: Annotated[str, Field(description="Assembly file (.SLDASM), absolute path.")], max_depth: Annotated[int, Field(description="How many levels of the assembly tree to descend; null means all.")] = 1, refresh: Annotated[bool, Field(description="Re-read the model from disk instead of using cached data.")] = False) -> str:
    """List assembly components with part names, paths, colors and instance info.

    max_depth=1 is flat and fast; higher values recurse into subassemblies.
    The component list is cached per document; set refresh=true after editing
    the assembly's structure outside this server.
    """
    return _json(assembly.assembly_tree(assembly_path, max_depth=max_depth, refresh=refresh))


@mcp.tool()
@_reject_unknown_arguments
def get_mass_properties(assembly_path: Annotated[str, Field(description="Assembly file (.SLDASM), absolute path.")]) -> str:
    """Read whole-assembly mass, volume, surface area and center of mass (SI units).

    Returns a structured error when CreateMassProperty is unavailable on the
    current binding (see docs/com-quirks.md section 5) instead of a guess.
    """
    return _json(assembly.mass_properties(assembly_path))


@mcp.tool()
@_reject_unknown_arguments
def export_preview(
    assembly_path: Annotated[str, Field(description="Assembly file (.SLDASM), absolute path.")],
    output_path: Annotated[str, Field(description="Where to write the result. Extension decides the format when fmt is omitted.")],
    size: Annotated[str, Field(description="Output image size in pixels, e.g. [1600, 1200].")] = "1600x1200",
    view: Annotated[str, Field(description="Named model view to capture, e.g. '*Front'. null uses the current view.")] = "*Isometric",
    keep_bmp: Annotated[bool, Field(description="Keep the intermediate BMP next to the PNG (debugging only).")] = False,
) -> str:
    """Export the current view to an image, e.g. 'D:/out/assembly.bmp' or '.png'.

    size is 'WxH'. SolidWorks writes BMP natively; PNG requires Pillow and the
    intermediate BMP is deleted unless keep_bmp=true.
    """
    return _json(assembly.export_preview(
        assembly_path, output_path, size=size, view=view, keep_bmp=keep_bmp,
    ))


def _install_argument_guard(app):
    """Reject unknown keys at the MCP request entry, BEFORE pydantic drops them.

    v0.8.233 put `_reject_unknown_arguments` on the tool functions. Report 2026-11-08
    measured that this changes nothing over stdio: FastMCP validates `arguments` against
    the generated inputSchema first, and pydantic's default `extra="ignore"` DISCARDS
    undeclared keys. By the time the wrapper runs, `kwargs` contains only the declared
    ones — the guard's `unknown` set is always empty. It only ever fired on direct Python
    calls, i.e. exactly where the tests were looking.

    So the check moves to the only place that can still see the raw payload:
    `Server.request_handlers[CallToolRequest]`. That handler receives `params.arguments`
    as it came off the wire.

    Returns True when the guard is installed. A False return is not fatal — the server
    still runs, just without the extra check — because this is the ONLY thing that touches
    mcp's private `_mcp_server`, and losing it must never take the whole server down.
    """
    try:
        import json as _jsonlib
        from mcp import types as _mt

        low = getattr(app, "_mcp_server", None)
        handlers = getattr(low, "request_handlers", None) if low is not None else None
        if not handlers:
            return False
        key = _mt.CallToolRequest
        original = handlers.get(key)
        if original is None:
            return False

        manager = getattr(app, "_tool_manager", None)
        known = getattr(manager, "_tools", None) or {}

        async def _guarded(request):
            try:
                name = request.params.name
                raw = request.params.arguments or {}
                tool = known.get(name)
                if tool is not None:
                    declared = set((tool.parameters or {}).get("properties") or {})
                    if declared:
                        unknown = sorted(k for k in raw if k not in declared)
                        if unknown:
                            payload = {
                                "ok": False,
                                "error": "unknown_argument",
                                "message": "unknown argument(s): %s" % ", ".join(unknown),
                                "unknown": unknown,
                                "accepted": sorted(declared),
                                "hint": ("This tool has no such parameter. The call was "
                                         "refused instead of run with it dropped — a "
                                         "mistyped name would otherwise fall back to the "
                                         "default and the receipt would look normal."),
                            }
                            return _mt.ServerResult(_mt.CallToolResult(
                                content=[_mt.TextContent(type="text",
                                                         text=_jsonlib.dumps(
                                                             payload, ensure_ascii=False))],
                                isError=True))
            except Exception:
                # Never let the guard itself break a call it does not understand.
                pass
            return await original(request)

        handlers[key] = _guarded
        return True
    except Exception:
        return False


def main():
    _install_argument_guard(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
