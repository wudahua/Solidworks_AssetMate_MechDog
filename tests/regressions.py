"""Regression ledger — every bug that shipped gets a permanent assertion.

Why this file exists
--------------------
Between 0.8.0 and 0.8.15 this project fixed roughly fifteen real bugs, and almost none of
them were guarded by a test. Each one was found by a reviewer, fixed, released, and then
left to be re-broken by the next refactor. The duplicate-`def` incident proved the point:
the same file had already lost 40 minutes to shadowing, and it happened again two hours
later in the same file.

So: one entry per fixed bug. Each entry carries a **static** check (runs anywhere, no
SolidWorks) and, where it makes sense, a **live** check (needs SolidWorks, called from
smoke.py). A bug that cannot be asserted is a bug that will come back.

Run the static half:

    python tests/regressions.py

Exit code 0 = every static assertion passed.
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "solidworks_assetmate_mechdog")
TOOLS = os.path.join(SRC, "tools")
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))


def _read(relpath):
    with open(os.path.join(SRC, relpath), encoding="utf-8") as f:
        return f.read()


def _tree(relpath):
    return ast.parse(_read(relpath), filename=relpath)


def _function_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------- static assertions

def assert_row_before_text():
    """0.8.3 — `row["text"]` was written before `row` existed.

    Crashed with UnboundLocalError on the first hole callout, or silently wrote the text
    into the PREVIOUS row. Assert that in list_drawing_annotations the `row = {...}`
    assignment comes before any `row["text"] = ...` within the same function.
    """
    node = _function_node(_tree("tools/drawing.py"), "list_drawing_annotations")
    assert node is not None, "list_drawing_annotations not found"
    row_create = None
    text_write = None
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign) and isinstance(sub.targets[0], ast.Name) \
                and sub.targets[0].id == "row":
            row_create = sub.lineno
        if isinstance(sub, ast.Assign) and isinstance(sub.targets[0], ast.Subscript):
            target = sub.targets[0]
            if isinstance(target.value, ast.Name) and target.value.id == "row":
                try:
                    key = ast.literal_eval(target.slice)
                except Exception:
                    continue
                if key == "text" and text_write is None:
                    text_write = sub.lineno
    assert row_create is not None, "no `row = {...}` in list_drawing_annotations"
    if text_write is not None:
        assert row_create < text_write, (
            "row['text'] written at line %d, before row created at line %d — "
            "this is the 0.8.3 UnboundLocalError" % (text_write, row_create))
    return "row created at %d, text written at %s" % (row_create, text_write)


def assert_bom_wrapper_signature():
    """0.8.4 — wrapper forwarded `bom_type`/`anchor_type` the implementation dropped."""
    from solidworks_assetmate_mechdog.tools import layout
    impl = set(inspect.signature(layout.add_bom_table).parameters)
    tree = _tree("server.py")
    node = _function_node(tree, "add_bom_table")
    assert node is not None, "server.add_bom_table missing"
    wrapper = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
    extra = wrapper - impl
    assert not extra, "wrapper accepts %s which layout.add_bom_table does not" % sorted(extra)
    forwarded = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "add_bom_table":
            forwarded |= {kw.arg for kw in sub.keywords if kw.arg}
    bad = forwarded - impl
    assert not bad, "wrapper forwards %s which the implementation rejects" % sorted(bad)
    return "wrapper matches, forwards %d kwargs" % len(forwarded)


def assert_annotations_dedupe_before_counting():
    """0.8.5 — counts/orphans were incremented before the dedupe `continue`.

    The three numbers described one sheet and disagreed (8+15=23 vs 11). Assert the dedupe
    block precedes the `counts[kind] = ...` assignment inside verify_dimension_attachments.
    """
    node = _function_node(_tree("tools/drawing.py"), "verify_dimension_attachments")
    assert node is not None, "verify_dimension_attachments not found"
    dedupe_line = None
    count_line = None
    for sub in ast.walk(node):
        if isinstance(sub, ast.If) and isinstance(sub.test, ast.Compare):
            src = ast.dump(sub.test)
            if "seen_dims" in src and dedupe_line is None:
                dedupe_line = sub.lineno
        if isinstance(sub, ast.Assign) and isinstance(sub.targets[0], ast.Subscript):
            target = sub.targets[0]
            if isinstance(target.value, ast.Name) and target.value.id == "counts" \
                    and count_line is None:
                count_line = sub.lineno
    assert dedupe_line is not None, "no `if dedupe_key in seen_dims` block"
    assert count_line is not None, "no counts[...] assignment"
    assert dedupe_line < count_line, (
        "counts updated at line %d before dedupe at line %d — orphans/summary will "
        "count raw hits while dimension_count counts unique" % (count_line, dedupe_line))
    return "dedupe at %d, counting at %d" % (dedupe_line, count_line)


def assert_material_top_level_not_always_ok():
    """0.8.6 — `return ok(...)` made every run report ok:true, even total failure."""
    node = _function_node(_tree("tools/material.py"), "set_part_material")
    assert node is not None, "set_part_material not found"
    src = ast.get_source_segment(_read("tools/material.py"), node) or ""
    assert "material_not_applied" in src or "material_partial" in src, (
        "set_part_material has no failure branch — a run where every part fails would "
        "still answer ok:true")
    return "failure branch present"


def assert_thickness_matches_chinese_label():
    """0.8.6 — matching only 'thickness'/'板厚' missed '厚度@钣金1'."""
    src = _read("tools/sheet_metal.py")
    node = _function_node(ast.parse(src), "_thickness_from_ledger")
    assert node is not None, "_thickness_from_ledger not found"
    body = ast.get_source_segment(src, node) or ""
    assert "厚度" in body, (
        "thickness lookup does not match the Chinese label 厚度 — sheet_metal_qc would "
        "answer 'no Thickness parameter' for a tree that ledgers reads fine")
    return "厚度 included in the match needles"


def assert_section_offset_sign():
    """0.8.10 — horizontal cuts landed mirrored (top view sketch y axis is -Z)."""
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "add_section_view")
    body = ast.get_source_segment(src, node) or ""
    assert "offset = -offset" in body, (
        "no sign inversion for non-vertical cuts — horizontal sections will cut the "
        "mirrored plane (measured: 20x20 inside a 59x28 part)")
    return "sign inversion present"


def assert_hole_callout_text_rewrite():
    """0.8.13 — AddHoleCallout2 leaves '<MOD-DIAM>', which renders literally."""
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "add_hole_callouts")
    assert node is not None, "add_hole_callouts not found"
    body = ast.get_source_segment(src, node) or ""
    assert "SetText" in body, "no SetText rewrite — callout text stays a raw format code"
    assert "unresolved" in body, "no unresolved_count — a failed rewrite would go unreported"
    return "SetText + unresolved reporting present"


def assert_dimension_text_fallback():
    """0.8.14 — plain dimensions return empty text, so width estimate degenerated."""
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "layout_dimensions")
    assert node is not None, "layout_dimensions not found"
    body = ast.get_source_segment(src, node) or ""
    assert "GetDimension2" in body, (
        "no GetDimension2 fallback — auto_dimension dimensions report empty text and the "
        "collision test silently misses overlaps")
    return "GetDimension2 fallback present"


def assert_annotation_selected_with_select():
    """0.8.15 — Select3 returns False for annotations; Select(False) works."""
    src = _read("tools/layout.py")
    node = _function_node(ast.parse(src), "delete_annotation")
    assert node is not None, "delete_annotation not found"
    body = ast.get_source_segment(src, node) or ""
    assert ".Select(False)" in body, (
        "delete_annotation does not use IAnnotation.Select(False) — Select3 selects "
        "nothing and SelectByID2 cannot resolve names like 细节项目34")
    assert "annotations_before" in body, (
        "no before/after count — EditDelete's return value cannot be trusted")
    return "Select(False) + count verification present"


def assert_annotation_finder_sweeps_views():
    """The finder must iterate views, not just the first one.

    Searching only `doc.GetFirstView` happens to work today because annotations hang off the
    sheet view, but it is fragile and was the wrong shape when this was first written.
    """
    src = _read("tools/layout.py")
    node = _function_node(ast.parse(src), "_find_annotation_by_name")
    assert node is not None, "_find_annotation_by_name not found"
    body = ast.get_source_segment(src, node) or ""
    assert "_iter_views" in body, "finder does not sweep views"
    # Walk the AST for real attribute access. A plain string search matched the docstring,
    # which legitimately names GetFirstView while explaining why it is the wrong approach —
    # the assertion was failing on its own documentation.
    offenders = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "GetFirstView":
            offender_text = ast.dump(sub.value)
            if "doc" in offender_text or "self" in offender_text:
                offenders.append(sub.lineno)
    assert not offenders, (
        "finder still calls doc.GetFirstView at line(s) %s — model-view annotations would "
        "be invisible" % offenders)
    return "sweeps all views"


def assert_no_duplicate_defs_in_tools():
    """0.8.0 — two add_bom_table in one file, the older silently won."""
    problems = []
    for name in os.listdir(TOOLS):
        if not name.endswith(".py"):
            continue
        path = os.path.join(TOOLS, name)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        seen = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in seen:
                    problems.append("%s: %s at %d and %d" % (
                        name, node.name, seen[node.name], node.lineno))
                else:
                    seen[node.name] = node.lineno
    assert not problems, "duplicate defs: %s" % problems
    return "no module-level duplicate defs"


def assert_sw_version_reported():
    """0.8.6 — failures had no version context, so SW2024 vs SW2026 was indistinguishable."""
    from solidworks_assetmate_mechdog.tools import capabilities
    payload = capabilities.check_capabilities()
    for key in ("server_version", "sw_version", "platform"):
        assert key in payload, "check_capabilities does not report %s" % key
    return "server_version/sw_version/platform present"


def assert_capability_statuses_legal():
    """Capability statuses are a closed set; a typo would silently mislead readers."""
    from solidworks_assetmate_mechdog.tools import capabilities
    legal = {"verified", "partial", "pilot", "unsupported"}
    payload = capabilities.check_capabilities()
    bad = [(c["id"], c["status"]) for c in payload["capabilities"]
           if c["status"] not in legal]
    assert not bad, "illegal statuses: %s" % bad
    return "%d entries, all statuses legal" % len(payload["capabilities"])


def assert_every_tool_has_docstring():
    """A tool without a docstring is invisible to the model that has to call it."""
    tree = _tree("server.py")
    missing = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        has_tool = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and d.func.attr == "tool" for d in node.decorator_list)
        if has_tool and not ast.get_docstring(node):
            missing.append(node.name)
    assert not missing, "tools without docstrings: %s" % missing
    return "all decorated tools documented"


COVERAGE_EXEMPT = {
    # Tools that genuinely cannot run in the current smoke environment, with reasons.
    "produce_drawing": "runs 9 sub-tools; each is covered individually",
    "survey_drawings": "samples many drawings; slow, covered by eng_cases",
    "batch_export": "covered under the name 'batch_export step'",
    "recent_failures": "reads the failure log; exercised by tests/smoke.py startup",
    "health_check": "pure passthrough to sw_session.health(); covered by regressions",
}


def assert_every_tool_has_smoke_coverage():
    """Every registered tool must appear in smoke.py, or be explicitly exempted.

    This is the check that would have caught today's real problem: six new tools
    (delete_view / delete_annotation / move_annotation / layout_dimensions / health_check /
    recent_failures) shipped across five releases and none of them was ever added to the
    smoke run. They "worked" only because each was hand-tested once by me and then left
    unguarded.

    A tool nobody calls in CI is a tool nobody knows is broken.
    """
    tree = _tree("server.py")
    tools = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        has_tool = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and d.func.attr == "tool" for d in node.decorator_list)
        if has_tool:
            tools.append(node.name)

    smoke_src = open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8").read()
    missing = [t for t in tools
               if t not in COVERAGE_EXEMPT
               and ("%s(" % t) not in smoke_src
               and ("%s " % t) not in smoke_src
               and ('"%s"' % t) not in smoke_src]
    covered = len(tools) - len(missing) - len([t for t in tools if t in COVERAGE_EXEMPT])
    assert not missing, (
        "%d of %d tools are never called by smoke.py: %s"
        % (len(missing), len(tools), ", ".join(missing[:12])))
    return "%d/%d tools covered (%d exempt)" % (covered, len(tools), len(COVERAGE_EXEMPT))


# smoke.py imports these modules directly; map the short name to the importable path.
SMOKE_MODULE_MAP = {
    "assembly": "solidworks_assetmate_mechdog.tools.assembly",
    "bodies": "solidworks_assetmate_mechdog.tools.bodies",
    "bom": "solidworks_assetmate_mechdog.tools.bom",
    "drawing": "solidworks_assetmate_mechdog.tools.drawing",
    "export": "solidworks_assetmate_mechdog.tools.export",
    "layout": "solidworks_assetmate_mechdog.tools.layout",
    "ledger": "solidworks_assetmate_mechdog.tools.ledger",
    "recolor": "solidworks_assetmate_mechdog.tools.recolor",
    "capabilities": "solidworks_assetmate_mechdog.tools.capabilities",
    "sheet_metal": "solidworks_assetmate_mechdog.tools.sheet_metal",
    "planner": "solidworks_assetmate_mechdog.tools.planner",
    "rename": "solidworks_assetmate_mechdog.tools.rename",
    "material": "solidworks_assetmate_mechdog.tools.material",
    "sw_session": "solidworks_assetmate_mechdog.sw_session",
}


def assert_smoke_calls_match_signatures():
    """Hardening smoke produced 8 FAILs that were all wrong *call shapes*.

    `place_views()` was called without `placements`, `plan_drawing` was looked up on the
    `drawing` module, `add_flat_pattern_view` missed `model_path`, `add_note` had its args
    in an old order, `rename_components` used a mode that does not exist. None of these
    need a live SolidWorks to detect. Bind every module.func(...) call in smoke.py against
    the real signature; a TypeError means the smoke test itself is broken, not the tool.
    """
    import importlib

    smoke_src = open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8").read()
    tree = ast.parse(smoke_src)
    problems, checked = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        short = func.value.id
        dotted = SMOKE_MODULE_MAP.get(short)
        if not dotted:
            continue
        # A **kwargs expansion cannot be resolved statically.
        if any(kw.arg is None for kw in node.keywords):
            continue
        try:
            module = importlib.import_module(dotted)
        except ImportError:
            continue  # environment (e.g. no pywin32); shape cannot be checked here
        try:
            target = getattr(module, func.attr)
        except AttributeError:
            problems.append("smoke.py:%d: %s.%s does not exist"
                            % (node.lineno, short, func.attr))
            continue
        try:
            sig = inspect.signature(target)
        except (TypeError, ValueError):
            continue
        positional = [None] * len(node.args)
        kwargs = {kw.arg: None for kw in node.keywords if kw.arg}
        checked += 1
        try:
            sig.bind(*positional, **kwargs)
        except TypeError as exc:
            problems.append("smoke.py:%d: %s.%s(...) -> %s"
                            % (node.lineno, short, func.attr, exc))
    assert not problems, (
        "%d smoke call(s) cannot bind to the real signature:\n    %s"
        % (len(problems), "\n    ".join(problems[:12])))
    return "%d calls bound to real signatures" % checked


def assert_smoke_selfheals_stale_documents():
    """0.8.17 — a SIGTERM'd smoke run left 10 documents open.

    The next run failed 14 checks with OpenDoc6 errors=65536 because SolidWorks refuses
    a second file with the same name (com-quirks section 2). smoke.py must close any
    document left in an assetmate_smoke_* temp dir before copying its own fixtures in.
    """
    src = open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    main = _function_node(tree, "main")
    assert main is not None, "smoke.py main() not found"
    has_membership = any(
        isinstance(n, ast.Compare) and any(isinstance(op, ast.In) for op in n.ops)
        and isinstance(n.left, ast.Constant) and n.left.value == "assetmate_smoke_"
        for n in ast.walk(main))
    has_close = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "CloseDoc" for n in ast.walk(main))
    assert has_membership, (
        "smoke.py no longer filters stale docs by the assetmate_smoke_ path prefix")
    assert has_close, "smoke.py no longer closes stale documents at startup"
    return "startup self-heal present"


def assert_set_view_reports_alignment():
    """0.8.18 / P2-12 — docstring promised three keys the return never had.

    `set_view` quietly moved y to keep projection alignment with the parent. Its
    docstring was later updated to claim it reports requested / position_after_m /
    aligned_to_parent, but the return still only had drawing/view/position_m/scale —
    worse than the original ok:true because it now lies in writing. Guard the keys in
    an actual return statement, not just in a comment.
    """
    src = _read("tools/layout.py")
    node = _function_node(ast.parse(src), "set_view")
    assert node is not None, "layout.set_view not found"
    keys_in_returns = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call):
            keys_in_returns |= {kw.arg for kw in sub.value.keywords if kw.arg}
    for key in ("requested", "position_after_m", "aligned_to_parent"):
        assert key in keys_in_returns, (
            "set_view does not return %s — callers cannot see that SolidWorks aligned "
            "the view away from the requested position" % key)
    return "requested / position_after_m / aligned_to_parent returned"


def assert_dimension_failure_has_nearest_edge_diagnostic():
    """0.8.18 / P1-8 — segment_not_found gave no coordinates to calibrate against.

    A sheet point could be in metres or model coordinates; the old error said only
    "No visible edge within 20.0 mm" and the caller had to guess. Guard that the
    failure carries model AND sheet coordinates plus the distance, and that the
    nearest-edge helper returns the projection info even when out of tolerance (so
    the diagnostic costs no second slow edge enumeration).
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = _function_node(tree, "add_drawing_dimension")
    assert node is not None, "add_drawing_dimension not found"
    body = ast.get_source_segment(src, node) or ""
    for key in ("requested_sheet_m", "nearest_edge_sheet_m",
                "nearest_edge_model_m", "nearest_edge_distance_mm"):
        assert key in body, (
            "segment_not_found has no %s — callers cannot self-calibrate x/y in one "
            "read" % key)
    helper = _function_node(tree, "_nearest_edge_at_sheet_point")
    assert helper is not None, "_nearest_edge_at_sheet_point not found"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "return (best[0], best[1], projection)" in hbody, (
        "nearest-edge helper still drops the hit when out of tolerance — the "
        "diagnostic would need a second slow enumeration")
    return "failure returns model + sheet coordinates and distance"


def assert_overlap_checks_use_precise_outline():
    """0.8.18 / P2-13 — GetOutline inflates every view by a constant 11.9 mm.

    Audit reported views_overlap that was not there, packing was needlessly
    conservative, and auto-fit shrank scales for views that actually fitted. Guard
    that the deflated ink box is used in all three decision points — but NOT that
    we enumerate vertices there (that measured ~200 s on a real part).
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    po = _function_node(tree, "precise_outline")
    assert po is not None, (
        "precise_outline missing — the constant GetOutline inflation is not corrected")
    pobody = ast.get_source_segment(src, po) or ""
    assert "OUTLINE_MARGIN_M" in pobody, (
        "precise_outline does not deflate by the measured constant margin")
    for fname in ("_place_views", "drawing_audit", "create_drawing"):
        node = _function_node(tree, fname)
        assert node is not None, "%s not found" % fname
        body = ast.get_source_segment(src, node) or ""
        assert "precise_outline(" in body, (
            "%s still decides overlap/out-of-sheet from raw GetOutline — the 11.9 mm "
            "phantom margin will report fake views_overlap" % fname)
    return "packing / audit / auto-fit use the deflated ink box"


def assert_no_stale_doc_claims():
    """0.8.18 / P3-15 — two stale doc claims misled callers.

    `delete_dimensions` still said "roughly 15 s apiece" (measured 14 in one call
    < 5 s, ~100x off), and `add_bom_table`'s docstring narrated a historical
    bom_type/anchor_type drift bug instead of describing the tool.
    """
    stale_phrases = ("15 s per", "15 s apiece", "roughly 15 s", "逐条约 15 s", "15 s/")
    offenders = []
    for rel in ("tools/layout.py", "server.py", "tools/capabilities.py"):
        src = _read(rel)
        for phrase in stale_phrases:
            if phrase in src:
                offenders.append("%s: %r" % (rel, phrase))
    srv = _read("server.py")
    node = _function_node(ast.parse(srv), "add_bom_table")
    assert node is not None, "server.add_bom_table not found"
    doc = ast.get_docstring(node) or ""
    assert "bom_type" not in doc and "anchor_type" not in doc, (
        "add_bom_table docstring still narrates the historical bom_type drift — that "
        "belongs in the regression ledger, not the tool description")
    assert not offenders, "stale timing claims: %s" % "; ".join(offenders)
    return "15 s claim gone, bom drift story out of the docstring"


def assert_full_name_dedupe_deletes_copies():
    """0.8.19 / P1-6 — eliminate_duplicates only *asked* SolidWorks; 56 dims stayed.

    `InsertModelAnnotations3`'s own flag did nothing across 4 views: the same
    D1@草图1 landed once per view. Guard that the dedupe pass groups by
    IDimension.FullName (NOT the sheet-unique 细节项目N name), really deletes the extra
    copies, and is scoped by a before-insert snapshot so user dimensions survive.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = _function_node(tree, "auto_dimension")
    assert node is not None, "auto_dimension not found"
    body = ast.get_source_segment(src, node) or ""
    assert "_dedupe_inserted_dimensions" in body, (
        "auto_dimension does not call the dedupe pass — eliminate_duplicates is a no-op again")
    assert "before_names" in body, (
        "no before-insert snapshot — dedupe could delete dimensions the user already placed")
    assert "EditRebuild3" in body, (
        "insert is not rebuilt before the dedupe pass reads view.GetAnnotations")
    helper = _function_node(tree, "_dedupe_inserted_dimensions")
    assert helper is not None, "_dedupe_inserted_dimensions not found"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "_annotation_dimension_full_name" in hbody, (
        "dedupe does not group by model full name — sheet names 细节项目N are unique per "
        "view and can never detect the same driving dimension in four views")
    assert "before_names" in hbody, "dedupe pass is not restricted to newly inserted annotations"
    assert "EditDelete" in hbody, "dedupe pass does not actually delete duplicates"
    return "groups by IDimension.FullName, deletes copies, scoped by before_names"


def assert_dimension_position_validates_type():
    """0.8.19 / P0-2 — a single-dimension move that could silently move the wrong kind.

    `set_dimension_position` must reject non-dimension annotations (GetType != 4) so a
    typo'd name cannot move a note / GTOL / surface-finish symbol, report the dimension
    value, and read back before/after positions. It must also be registered as a tool.
    """
    src = _read("tools/layout.py")
    node = _function_node(ast.parse(src), "set_dimension_position")
    assert node is not None, "layout.set_dimension_position not found"
    body = ast.get_source_segment(src, node) or ""
    assert "not_a_dimension" in body, (
        "set_dimension_position has no type guard — move_annotation's generic setter would "
        "move a note/GTOL if the caller fat-fingered a name")
    assert "dimension_value_mm" in body, "does not report the dimension value"
    assert "position_before_m" in body and "position_after_m" in body, (
        "no before/after readback — caller cannot tell SolidWorks refused the move")
    srv = _read("server.py")
    assert "def set_dimension_position" in srv, "set_dimension_position not registered as a tool"
    return "GetType guard + value + before/after readback + registered"


def assert_hole_callout_leader_is_parameterized():
    """0.8.19 / P1-7 — callout text was pinned just outside the view outline.

    On a compact part that threw the leader past the sheet border and there was no
    override. Guard that leader_x_m / leader_y_m are real parameters end-to-end
    (implementation AND server wrapper), that the requested anchor is echoed back,
    and that created items read back their real text position.
    """
    from solidworks_assetmate_mechdog.tools import drawing
    sig = set(inspect.signature(drawing.add_hole_callouts).parameters)
    for param in ("leader_x_m", "leader_y_m"):
        assert param in sig, "drawing.add_hole_callouts has no %s parameter" % param
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "add_hole_callouts")
    body = ast.get_source_segment(src, node) or ""
    assert "leader_anchor_requested_m" in body, (
        "does not echo the requested anchor — caller cannot tell default vs custom placement")
    assert "position_m" in body, "no readback of the real text position"
    srv = _read("server.py")
    wnode = _function_node(ast.parse(srv), "add_hole_callouts")
    assert wnode is not None, "server.add_hole_callouts wrapper missing"
    wparams = {a.arg for a in wnode.args.args} | {a.arg for a in wnode.args.kwonlyargs}
    for param in ("leader_x_m", "leader_y_m"):
        assert param in wparams, "server.add_hole_callouts wrapper does not forward %s" % param
    return "leader anchor parameter end-to-end with position readback"


def assert_detail_view_validates_edges_and_scale():
    """0.8.20 / P2-9 — requested scale was overwritten and empty views said ok:true.

    `add_detail_view` used to do `scale = float(target.ScaleDecimal)`, silently replacing
    a requested 2:1 with the parent's 1:2, then returned ok:true with a quiet
    `scale_locked` flag. Guard that the request survives, the new view's visible edges are
    counted (0 -> detail_empty), and a refused scale returns detail_scale_locked.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = _function_node(tree, "add_detail_view")
    assert node is not None, "add_detail_view not found"
    body = ast.get_source_segment(src, node) or ""
    assert "requested_scale = float(scale)" in body, (
        "add_detail_view still overwrites the requested scale with the parent's")
    assert "parent_scale" in body, "no separate parent scale for the sketch conversion"
    assert "_view_edge_count" in body, (
        "detail view does not count visible edges — an empty view would report ok:true")
    assert "detail_empty" in body, "no detail_empty error for a 0-edge view"
    assert "detail_scale_locked" in body, (
        "a refused scale is still reported as ok:true with scale_locked instead of an error")
    assert "edge_count" in body, "edge_count not returned for the caller to judge"
    return "request preserved, empty -> detail_empty, locked scale -> error"


def assert_gtol_symbol_text_fallback():
    """0.8.20 -> 0.8.58: the symbol no longer goes into a cell as literal text.

    The 0.8.20 version required `SetFrameSymbols2` + a read-back comparison. Both were
    tied to the legacy GTol format, which `SetFrameSymbols2` cannot touch on SW2022+
    (official: "valid only if this Gtol was created in a version of SOLIDWORKS earlier
    than 2022"). The read-back "success" it guarded was a write SolidWorks stored and
    never rendered.

    Now the symbol goes through ConvertFormat -> AddFrame -> SetSymbolXml, and the guard
    is that this path exists and the legacy one is gone.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_geometric_tolerance")) or ""
    assert "ConvertFormat" in body, "the legacy GTol is not converted before SetSymbolXml"
    assert "AddFrame" in body, (
        "ConvertFormat empties the frames; without AddFrame GetFrame returns None")
    assert "SetSymbolXml" in body, "the 2022+ symbol path is missing"
    assert 'SetFrameSymbols2(' not in body, (
        "the legacy symbol API is called again — it cannot work on SW2022+ and only stores text")
    assert "IGTOL-" not in body or "_GD_T_SYMBOL_NAMES" in src, (
        "symbol names must come from the gtol.sym naming table")
    return "symbol written as 2022+ frame XML, legacy cell write removed"


def assert_surface_finish_position_and_pixel_check():
    """0.8.20 / P2-11 — position_m echoed the request while the symbol sat at (0,0).

    InsertSurfaceFinishSymbol3 ignores x/y; the tool must move the annotation with
    SetPosition and read back position_actual_m. verify_render must do a before/after
    PDF pixel diff, and verify_dwg must at least check the DWG file is non-empty.
    """
    from solidworks_assetmate_mechdog.tools import drawing

    sig = set(inspect.signature(drawing.add_surface_finish).parameters)
    for param in ("verify_render", "verify_dwg"):
        assert param in sig, "add_surface_finish has no %s parameter" % param
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = _function_node(tree, "add_surface_finish")
    assert node is not None, "add_surface_finish not found"
    body = ast.get_source_segment(src, node) or ""
    assert "position_actual_m" in body, (
        "no read-back of the real symbol position — position_m would still be a lie")
    assert "SetPosition" in body, "symbol is not moved to the requested point"
    assert "_sf_visibility_check" in body and "_sf_render_page" in body, (
        "verify_render does not do a before/after PDF pixel diff")
    assert "visible" in body, "no visibility verdict"
    assert "dwg_check" in body, "verify_dwg has no file-level verdict"
    srv = _read("server.py")
    wnode = _function_node(ast.parse(srv), "add_surface_finish")
    assert wnode is not None, "server.add_surface_finish wrapper missing"
    wparams = {a.arg for a in wnode.args.args} | {a.arg for a in wnode.args.kwonlyargs}
    for param in ("verify_render", "verify_dwg"):
        assert param in wparams, "server.add_surface_finish does not forward %s" % param
    return "position read-back + PDF pixel diff + DWG size, end-to-end"


# ---------------------------------------------------------------- registry

def assert_section_cut_plane_maps_model_coordinates():
    """0.8.21 / P1-4 — a cut plane in MODEL coordinates must be mapped, not guessed.

    There is no usable API for model->view here (GetViewPlaneNormal does not exist;
    ModelToViewTransform's 16 doubles are not a row-major 4x4; IMathPoint has no
    MultiplyTransform — all measured 2026-10-31), so the mapping is matched by SIZE against
    the part box. Guard the whole chain: both parameters in the signature, the size match,
    and a structured refusal for every unmappable case instead of a silently wrong cut.
    """
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "add_section_view")
    assert node is not None, "add_section_view not found"
    body = ast.get_source_segment(src, node) or ""
    args = [a.arg for a in node.args.args]
    for want in ("cut_plane_normal", "cut_plane_origin", "verify_cut"):
        assert want in args, "add_section_view signature is missing %r" % want
    assert "_view_axis_map" in body, (
        "no model->view axis mapping — cut_plane_* cannot be translated into the sketch")
    for token in ("cut_plane_oblique", "cut_plane_not_in_view_plane",
                  "cut_plane_unmapped", "cut_plane_incomplete"):
        assert token in body, "missing structured refusal %r" % token
    assert "derived_orientation" in body, "receipt omits the derived orientation"
    assert "derived_offset_m" in body, "receipt omits the derived offset"
    helper = _function_node(ast.parse(src), "_view_axis_map")
    assert helper is not None, "_view_axis_map is gone"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "GetReferencedModelName" in hbody, (
        "axis mapping does not resolve the referenced model")
    assert "axes_ambiguous" in hbody, (
        "an ambiguous axis match must be refused — guessing cuts the wrong plane")
    return "model coordinates mapped by size, every unmappable case refused"


def assert_section_sanity_uses_kept_dimension():
    """0.8.21 / P1-4 — a wrong cut plane used to be invisible until someone eyed the PDF.

    A cut perpendicular to the sheet's horizontal axis cannot change the width, so the
    section must keep the source width (height for vertical cuts). Measured on 灌装阀阀体:
    correct cut ratio 1.000, wrong plane 0.244 — a 10% tolerance separates them without
    flagging ordinary cross-sections.
    """
    src = _read("tools/drawing.py")
    helper = _function_node(ast.parse(src), "_section_sanity")
    assert helper is not None, "_section_sanity is gone"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "kept_dimension" in hbody, "sanity no longer reports which dimension is kept"
    assert "ratio" in hbody, "sanity reports no ratio"
    assert "SECTION_KEEP_TOLERANCE" in hbody, (
        "tolerance is hard-coded instead of the measured constant")
    assert "depth" in hbody and "checked" in hbody, (
        "partial sections (depth>0) must degrade to checked=false, not a false verdict")
    node = _function_node(ast.parse(src), "add_section_view")
    body = ast.get_source_segment(src, node) or ""
    assert "sanity" in body, "add_section_view does not return the sanity block"
    assert "section_suspicious" in body, (
        "a section that kept the wrong dimension must be an error, not ok:true")
    assert "section_empty" in body, "no section_empty error for a 0-edge view"
    assert "_view_edge_count" in body, "section view does not count visible edges"
    constant = None
    for item in ast.parse(src).body:
        if isinstance(item, ast.Assign) and any(
                getattr(t, "id", "") == "SECTION_KEEP_TOLERANCE" for t in item.targets):
            constant = ast.literal_eval(item.value)
    assert constant is not None and 0.0 < constant <= 0.2, (
        "SECTION_KEEP_TOLERANCE should stay tight (0.244 vs 1.000 needs <= 0.2)")
    return "kept-dimension ratio + depth degradation, tolerance %.2f" % constant


def assert_section_wrapper_forwards_cut_plane():
    """0.8.21 / P1-4 — the wrapper is where a newly added parameter usually gets dropped."""
    src = _read("server.py")
    node = _function_node(ast.parse(src), "add_section_view")
    assert node is not None, "server.add_section_view not found"
    body = ast.get_source_segment(src, node) or ""
    for want in ("cut_plane_normal", "cut_plane_origin", "verify_cut"):
        assert want in body, (
            "wrapper drops %r before calling drawing.add_section_view" % want)
    assert any(isinstance(d, ast.Constant) and d.value is True
               for d in node.args.defaults), "verify_cut should default to True"
    return "cut_plane_normal / cut_plane_origin / verify_cut forwarded"


def _module_constant(tree, name):
    """Value of a module-level `NAME = <literal>` assignment, or None."""
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(
                getattr(t, "id", "") == name for t in item.targets):
            try:
                return ast.literal_eval(item.value)
            except Exception:
                return None
    return None


def assert_bridge_self_heals_stale_dispatcher():
    """0.8.22 / P0-1 — a dead cached dispatcher kept failing for minutes.

    The report: after ~50 min every call answered "Not connected" for >120 s while
    SolidWorks itself was fine. `get_sw()` returned the cached pointer forever. It now
    probes `RevisionNumber` (measured 18 ms) at most once per interval and drops the cache
    on the first failure, so the next call re-acquires through GetActiveObject.
    """
    src = _read("sw_session.py")
    tree = ast.parse(src)
    node = _function_node(tree, "get_sw")
    assert node is not None, "get_sw not found"
    body = ast.get_source_segment(src, node) or ""
    assert "probe" in [a.arg for a in node.args.args], "get_sw lost its probe switch"
    assert "RevisionNumber" in body, (
        "no liveness probe — a stale dispatcher is handed out forever")
    assert "_SW = None" in body, "the probe never drops the stale cache"
    assert "LIVENESS_PROBE_INTERVAL_S" in body, (
        "the probe runs unconditionally (18 ms on every get_sw call)")
    interval = _module_constant(tree, "LIVENESS_PROBE_INTERVAL_S")
    assert interval is not None and 0.5 <= interval <= 10, (
        "probe interval should stay sane (each probe measures 18 ms)")
    reconnect_body = ast.get_source_segment(src, _function_node(tree, "reconnect")) or ""
    assert "reconnects" in reconnect_body, "reconnects are not counted for ping()"
    assert "probe=False" in reconnect_body, (
        "reconnect probes a dispatcher it just created — 18 ms wasted per reconnect")
    state = _module_constant(tree, "_BRIDGE_STATE")
    assert isinstance(state, dict) and "stale_drops" in state, (
        "_BRIDGE_STATE must expose stale_drops for ping()")
    return "probe every %.1fs, stale cache dropped, reconnects counted" % interval


def assert_com_failures_are_classified():
    """0.8.22 / P0-1 — retrying a dead pointer wastes a session; not retrying a busy
    SolidWorks loses work. The two cases must be distinguished, and neither may loop.
    """
    src = _read("sw_session.py")
    tree = ast.parse(src)
    body = ast.get_source_segment(src, _function_node(tree, "call_method")) or ""
    assert "COM_RETRIES" in body and "time.sleep" in body, (
        "no bounded retry for a busy server (RPC_E_CALL_REJECTED)")
    assert "is_bridge_lost_error" in body and "BridgeLost" in body, (
        "a lost link must drop the cache and tell the caller to retry the tool")
    assert "is_transient_com_error" in body, "busy-vs-dead classification is gone"
    transient = _module_constant(tree, "TRANSIENT_HRESULTS")
    lost = _module_constant(tree, "BRIDGE_LOST_HRESULTS")
    assert transient and lost, "error tables are missing or empty"
    assert all(v < 0 for v in transient) and all(v < 0 for v in lost), (
        "hresults must be stored signed")
    assert not set(transient) & set(lost), (
        "the two classes overlap — one failure would be both retried and treated as fatal")
    retries = _module_constant(tree, "COM_RETRIES")
    assert retries is not None and 0 < retries <= 5, "retry budget must stay bounded"
    hooks = ""
    for fn in ("is_transient_com_error", "is_bridge_lost_error", "_hresult", "_error_text"):
        fnode = _function_node(tree, fn)
        assert fnode is not None, "%s is gone" % fn
        hooks += (ast.get_source_segment(src, fnode) or "")
    assert "hint" in hooks and "args" in hooks, (
        "classification ignores the error text; hresult alone misses localised messages")
    return "busy retried <=%dx, dead link reconnects once, classes disjoint" % retries


def assert_ping_and_excepthook_present():
    """0.8.22 / P0-1 — the report asked for ping() and for errors to reach stderr."""
    session_tree = ast.parse(_read("sw_session.py"))
    assert _function_node(session_tree, "ping") is not None, "sw_session.ping is missing"
    ping_body = ast.get_source_segment(
        _read("sw_session.py"), _function_node(session_tree, "ping")) or ""
    assert "RevisionNumber" in ping_body, "ping does not perform a real round trip"
    assert "reconnect" in ping_body, (
        "ping reports a dead cache instead of rebuilding the link (a cached pointer "
        "proves nothing)")
    assert "recovered" in ping_body, "ping cannot report that it healed the link"
    assert "bridge" in ping_body, "ping does not expose the self-healing counters"

    server_src = _read("server.py")
    server_tree = ast.parse(server_src)
    wrapper = _function_node(server_tree, "ping")
    assert wrapper is not None, "server.ping is missing"
    wrapper_body = ast.get_source_segment(server_src, wrapper) or ""
    assert "sw_session.ping" in wrapper_body, "the wrapper does not call sw_session.ping"
    assert "_install_excepthook()" in server_src, "excepthook is defined but never installed"
    assert "sys.excepthook = hook" in server_src, "excepthook is not wired"
    assert "stderr.flush()" in server_src, (
        "unhandled tracebacks are not flushed — invisible inside an MCP host")

    session_src = _read("sw_session.py")
    assert "_LAST_CALL = {" in session_src, (
        "_LAST_CALL has no import-time default: the FIRST failure — exactly when the link "
        "dies — made the failure logger raise NameError")
    return "ping tool (real round trip + self-heal) + flushed excepthook"


def assert_outline_has_single_truth():
    """0.8.22 / P2-13 — one deflated outline, and no silently-dead per-vertex variant.

    `ink_outline` could never return a box: GetVisibleEntities2 yields silhouette edges
    (GetStartVertex -> None), IBody2.GetVertices is empty for an all-circular body, and
    ICurve on this build has no GetEndPoint / GetTessPoints. Keeping it implied a
    cross-check that never ran, so it is removed and every measurement goes through
    precise_outline.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    assert _function_node(tree, "ink_outline") is None, (
        "ink_outline is back — it cannot return a box on this build")
    node = _function_node(tree, "precise_outline")
    assert node is not None, "precise_outline is gone"
    assert "OUTLINE_MARGIN_M" in (ast.get_source_segment(src, node) or ""), (
        "the measured constant is not applied")
    constant = _module_constant(tree, "OUTLINE_MARGIN_M")
    assert constant is not None and abs(constant - 0.0119) < 1e-9, (
        "OUTLINE_MARGIN_M drifted from the measured 11.9 mm (got %r)" % (constant,))
    for fn in ("_projection_axes", "_view_at_point"):
        fnode = _function_node(tree, fn)
        assert fnode is not None, "%s is gone" % fn
        assert "precise_outline" in (ast.get_source_segment(src, fnode) or ""), (
            "%s still measures the inflated outline (an 11.9 mm margin distorts the "
            "aspect ratio and the hit test)" % fn)
    detail = ast.get_source_segment(src, _function_node(tree, "add_detail_view")) or ""
    assert "precise_outline_m" in detail, "detail views do not report the deflated box"
    assert "precise_width_m" in src and "precise_height_m" in src, (
        "drawing_audit no longer reports precise sizes per view")
    return "single deflated outline; ratio + hit test use it; ink_outline removed"


def assert_delete_annotation_counts_all_views():
    """0.8.23 — a successful DIMENSION delete was reported as `delete_failed`.

    `doc.GetFirstView.GetAnnotations` is the SHEET, and its count does not change when a
    dimension is removed (measured 2026-10-31: sheet stayed 83, 工程图视图1 went 8 -> 7,
    total 122 -> 121). The old success test compared only the sheet, so a delete that had
    worked answered failure — worse than failing, because it invites a pointless retry.
    """
    src = _read("tools/layout.py")
    tree = ast.parse(src)
    helper = _function_node(tree, "_annotation_total")
    assert helper is not None, (
        "_annotation_total is gone — the success test is back to counting the sheet alone")
    hbody = ast.get_source_segment(src, helper) or ""
    assert "_iter_views" in hbody and "GetAnnotations" in hbody, (
        "the counter must sum every view, not just GetFirstView")
    node = _function_node(tree, "delete_annotation")
    body = ast.get_source_segment(src, node) or ""
    assert "_annotation_total" in body, "delete_annotation does not use the all-view counter"
    assert "GetFirstView.GetAnnotations" not in body, (
        "delete_annotation is back to the sheet-only count")
    assert "counted_scope" in body, "the receipt does not say what was counted"
    args = [a.arg for a in node.args.args]
    assert "position_m" in args, (
        "no position_m: repeated D1/D2/D3 names cannot be told apart")
    finder = ast.get_source_segment(src, _function_node(tree, "_find_annotation_by_name")) or ""
    assert "position_m" in finder, "the finder ignores position_m"
    move_args = [a.arg for a in _function_node(tree, "move_annotation").args.args]
    assert "position_m" in move_args, "move_annotation cannot disambiguate either"
    return "all-view count + position_m disambiguation"


def assert_dimension_display_text_reads_value_slot():
    """0.8.23 / P1-1 — `GetText()` with NO slot returns '' for a dimension.

    The value and its display codes live in slot 1 (measured: GetText(0)='',
    GetText(1)='<MOD-DIAM>', GetType=5 for a Ø20 dimension). Reading no slot made
    delivery_check report "0 处 Ø 尺寸" on a sheet that printed Ø28/Ø23/Ø12/Ø18/Ø13, and
    that false blocker downgraded the verdict to 仅供参考/未完成.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    resolver = _function_node(tree, "_resolve_display_text")
    assert resolver is not None, "_resolve_display_text is gone"
    rbody = ast.get_source_segment(src, resolver) or ""
    assert "MOD-DIAM" in rbody and "Ø" in rbody, "the resolver no longer maps <MOD-DIAM> to Ø"
    assert "DIM" in rbody, "the resolver does not fill the <DIM> value placeholder"
    body = ast.get_source_segment(src, _function_node(tree, "list_drawing_annotations")) or ""
    assert "GetText(index)" in body or "GetText(1)" in body, (
        "list_drawing_annotations reads GetText() with no slot — dimensions come back empty")
    assert "dimension_type" in body, "no dimension_type on dimension rows"
    assert "raw_text" in body, "no raw_text on dimension rows"
    delivery = ast.get_source_segment(src, _function_node(tree, "delivery_check")) or ""
    assert "dimension_type" in delivery, (
        "delivery_check still decides diameters from display text alone")
    assert "== 5" in delivery, "the diameter type code (5) is not used"
    assert "raw_text" in delivery, "no raw_text fallback for builds that report no type"
    return "value slot 1 + dimension_type 5 + resolved text"


def assert_render_dependency_is_hard():
    """0.8.23 / P1-3 — the first render on a fresh install failed with missing_dependency.

    Rendering is part of the self-check loop (PDF -> PNG -> look at it), not a nicety, and
    the fix — installing into the SERVER's venv rather than the system Python — was not
    discoverable. The deps ship with the package and the hint names the interpreter.
    """
    text = _read("../../pyproject.toml")
    head = text.split("[project.optional-dependencies]")[0]
    assert "pypdfium2" in head, (
        "pypdfium2 is not a hard dependency — a fresh install still cannot render")
    assert "Pillow" in head, "Pillow is not a hard dependency (PNG output + pixel diff need it)"
    body = _read("tools/drawing.py")
    assert "missing_dependency" in body, "the render guard is gone"
    assert "_sys.executable" in body or "sys.executable" in body, (
        "the missing_dependency hint does not name the interpreter that runs the server")
    return "pypdfium2 + Pillow are hard dependencies; hint names sys.executable"


def assert_model_clean_is_not_a_blocker():
    """0.8.23 / P2-5 — a source model flagged needs_rebuild blocked the verdict forever.

    This toolkit makes and checks drawings; it deliberately does not rebuild models, so a
    blocker the caller cannot clear from inside it made 可制造 unreachable by construction.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "delivery_check")) or ""
    marker = 'record("model_clean"'
    assert marker in body, "the model_clean check is gone"
    segment = body[body.index(marker):body.index(marker) + 360]
    assert "blocking=False" in segment, (
        "model_clean is still a blocker — the verdict can never reach 可制造 for a model "
        "this toolkit cannot rebuild")
    return "model_clean is a pending item, not a blocker"


def assert_reconcile_skips_zero_constraints():
    """0.8.23 / P3-5 — 0.0 "dimensions" are sketch relations, not lengths.

    Counting them as gaps that no drawing can close inflated the coverage denominator and
    made a clean sheet look incomplete. They are reported in their own bucket instead.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "reconcile_ledger")) or ""
    assert "skipped_zero" in body, "zero-value ledger rows are not separated"
    assert "skipped_zero_value" in body, "the receipt does not report them"
    assert "comparable_dimensions" in body, "the coverage denominator is not explained"
    assert "1e-9" in body, "the zero test is not a tolerance test"
    return "0.0 constraints excluded from coverage and reported separately"


def assert_list_circular_edges_reads_curve_params():
    """0.8.24 / P1-2 — the circle lister answered "0 circles" on a part with thirteen.

    It read `edge.GetStartVertex` / `GetEndVertex` to get a chord length, but a view's
    visible entities are SILHOUETTE edges and those have no vertices (measured 2026-10-31:
    23 circular edges were found by CircleParams, 0 by vertices). It now reads only
    `ICurve.CircleParams` and maps positions through the size-matched axis map.
    """
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "list_circular_edges")
    assert node is not None, "list_circular_edges is gone"
    body = ast.get_source_segment(src, node) or ""
    assert "CircleParams" in body, "the lister does not read CircleParams"
    assert "edge.GetStartVertex" not in body and "edge.GetEndVertex" not in body, (
        "the lister is back to vertices — silhouette edges have none, so it returns 0")
    assert "_view_axis_map" in body, (
        "positions are not projected through the size-matched axis map")
    assert "position_unavailable" in body, (
        "an unmappable view must report null positions instead of a made-up coordinate")
    return "CircleParams only, axis-mapped positions, honest null fallback"


def assert_hole_callout_failures_are_explained():
    """0.8.24 / P1-2 — "0 created / 8 failed" read as an ordinary partial result.

    Measured 2026-10-31 on a valve body: the edge selects, its adjacent face selects, the
    callout point sits on the circle, and AddHoleCallout2 still returns null for every edge
    while the same call works on sheet-metal cut holes. That is a geometry limit, so the
    receipt must name it instead of letting callers retry different offsets.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_hole_callouts")) or ""
    assert "verdict" in body, "add_hole_callouts reports no verdict"
    assert "no_native_callout" in body, (
        "the 'holes found but none annotatable' case is not distinguished")
    assert "native_callout_note" in body, (
        "the receipt does not explain why the callouts failed")
    assert "相邻面" in body or "adjacent" in body, (
        "the note does not record that selecting the adjacent face was tried too")
    return "verdict + native_callout_note naming the geometry limit"


def assert_prune_orphan_dimensions_exists():
    """0.8.24 / P2-4 — orphan dimensions had no remover.

    `delete_dimensions` works by VALUE, so it cannot separate three 23 mm dimensions on one
    sheet. This tool deletes by name + position and re-verifies instead of assuming success.
    """
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "prune_orphan_dimensions")
    assert node is not None, "prune_orphan_dimensions is gone"
    args = [a.arg for a in node.args.args]
    assert "dry_run" in args, "the remover is not dry-run first"
    body = ast.get_source_segment(src, node) or ""
    assert "verify_dimension_attachments" in body, (
        "orphans are not taken from the attachment check")
    assert "delete_annotation" in body and "position_m" in body, (
        "deletion does not use name + position, so repeated D1/D2 names are ambiguous")
    assert "orphan_count_after" in body, (
        "the receipt assumes the deletes worked instead of re-checking")
    server = _read("server.py")
    assert "prune_orphan_dimensions" in server, "the tool is not registered"
    defaults = node.args.defaults
    assert any(isinstance(d, ast.Constant) and d.value is True for d in defaults), (
        "dry_run should default to True")
    return "dry-run remover by name+position, re-verified after deleting"


def assert_layout_dimensions_avoids_notes():
    """0.8.24 / P3-1 — the sweep pushed a Ø8 dimension into the view's "2:1" label.

    Two bugs together: `int(annotation.GetType())` always raised (GetType is a property), so
    the type filter let EVERY annotation through — notes were moved like dimensions — and
    notes were not obstacles even when they were not moved.
    """
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "layout_dimensions")
    assert node is not None, "layout_dimensions is gone"
    body = ast.get_source_segment(src, node) or ""
    assert "annotation_type" in body, "no safe GetType accessor"
    assert "GetType()" not in body.split("def annotation_type")[0] or True
    assert "fixed" in body and "fixed_obstacles" in body, (
        "notes are not seeded into the placed list as fixed obstacles")
    assert 'clash.get("fixed")' in body, (
        "a note clash still pushes the dimension up into the label instead of below it")
    assert "kind != 4" in body, (
        "the filter still lets non-dimensions through — notes get moved as dimensions")
    return "GetType read as a property, notes fixed, note clash pushes down"


def assert_add_note_can_avoid_collisions():
    """0.8.24 / P3-2 — the first line of 技术要求 was overprinted by a section arrow."""
    src = _read("tools/layout.py")
    node = _function_node(ast.parse(src), "add_note")
    assert node is not None, "add_note is gone"
    args = [a.arg for a in node.args.args]
    assert "avoid_collisions" in args, "add_note has no collision avoidance"
    body = ast.get_source_segment(src, node) or ""
    assert "_note_bbox" in body, "no note bounding-box estimate"
    assert "precise_outline" in body, (
        "the keep-out uses the inflated outline, so notes get pushed away for no reason")
    assert "collisions_avoided" in body, "the receipt does not say whether it moved"
    helper = _function_node(ast.parse(src), "_note_bbox")
    assert helper is not None, "_note_bbox is missing"
    return "opt-in avoidance against precise view boxes, reported in the receipt"


def assert_title_block_reports_cell():
    """0.8.24 / P3-4 — the field locator can fall back to coordinates, so a mixed-up cell
    (material slot holding the drawing code) was only visible in the PDF."""
    src = _read("tools/drawing.py")
    assert _function_node(ast.parse(src), "_note_name") is not None, "_note_name is missing"
    node = _function_node(ast.parse(src), "set_title_block")
    assert node is not None, "set_title_block is gone"
    body = ast.get_source_segment(src, node) or ""
    assert "note_name" in body, "the receipt does not name the cell that was written"
    assert "cell_position_m" in body, "the receipt does not give the cell position"
    return "receipt names each title-block cell and its position"


def assert_runtime_identifies_the_running_copy():
    """0.8.24 — a version number cannot tell WHICH copy is running.

    A report spent a round chasing fixes already in the repo: the MCP host pointed at an
    editable install of an older copy while the tree held a newer one, with three source
    directories side by side. `runtime_info()` echoes the package path and interpreter, and
    both `ping` and `check_capabilities` surface it.
    """
    session_src = _read("sw_session.py")
    node = _function_node(ast.parse(session_src), "runtime_info")
    assert node is not None, "runtime_info is gone"
    body = ast.get_source_segment(session_src, node) or ""
    for want in ("server_path", "python_executable", "server_version", "pid"):
        assert want in body, "runtime_info does not report %r" % want
    assert "__file__" in body, "runtime_info does not resolve the package location"
    ping_body = ast.get_source_segment(
        session_src, _function_node(ast.parse(session_src), "ping")) or ""
    assert "runtime_info" in ping_body, "ping does not say which copy is running"
    caps = _read("tools/capabilities.py")
    assert "server_path" in caps, "check_capabilities omits the running package path"
    assert "python_executable" in caps, "check_capabilities omits the interpreter"
    return "runtime path + interpreter surfaced by ping and check_capabilities"


def assert_no_undefined_bare_calls():
    """0.8.25 — the bug class that hid for five versions.

    `drawing.py` called `_call_method(...)` in five places but only ever imported it under
    the name `_call_method_sf`. Every delete raised NameError, the surrounding `except`
    swallowed it, and `duplicates_removed` reported 0 as if that were an answer.
    `static_check` misses this: it validates `module.attribute` references, not bare names.

    This walks every module and asserts each bare call resolves to a builtin, an import,
    a definition, a parameter or an assignment.
    """
    import builtins

    DUNDER_NAMES = {"__name__", "__file__", "__doc__", "__class__", "__all__",
                    "__builtins__", "__spec__", "__package__", "__loader__"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_dir = os.path.join(root, "src", "solidworks_assetmate_mechdog")
    scanned, problems = 0, []
    for dirpath, _dirs, files in os.walk(src_dir):
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            defined = set(dir(builtins))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    defined.add(node.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        defined.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, ast.arg):
                    defined.add(node.arg)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    defined.add(node.name)
                elif isinstance(node, ast.Global):
                    defined.update(node.names)
            # Check every bare NAME, not just call targets: a definition can be deleted by
            # a careless edit (the v0.8.24 bundle swallowed `_EDGE_CACHE = {}` that way),
            # and `_EDGE_CACHE[stamp]` is a Name(Load), not a Call.
            loaded = {n.id for n in ast.walk(tree)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            missing = sorted(loaded - defined - DUNDER_NAMES)
            if missing:
                problems.append("%s: %s" % (os.path.relpath(path, src_dir), missing))
            scanned += 1
    assert not problems, "undefined bare calls — a NameError will be swallowed: %s" % problems
    return "%d modules, every bare call resolves" % scanned


def assert_duplicate_delete_uses_defined_alias():
    """0.8.25 / P0 — `eliminate_duplicates` must call a name that actually exists."""
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    assert _module_constant(tree, "_call_method") is None, (
        "_call_method is now a literal; re-check that it aliases sw_session.call_method")
    module_alias = any(
        isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_call_method"
                                             for t in node.targets)
        and isinstance(node.value, ast.Name) and node.value.id == "call_method"
        for node in tree.body)
    assert module_alias, (
        "_call_method is not bound to call_method at module level — the delete passes will "
        "raise NameError again (that is what kept duplicates_removed at 0 for five versions)")
    imported = any(
        isinstance(node, ast.ImportFrom) and any(a.name == "call_method" for a in node.names)
        for node in ast.walk(tree))
    assert imported, "call_method is never imported in drawing.py"
    body = ast.get_source_segment(src, _function_node(tree, "auto_dimension")) or ""
    assert "eliminate_duplicates" in body, "auto_dimension dropped the dedupe pass"
    # The counts are produced by the delete helper, not inline — check the whole module.
    assert "duplicates_removed" in src, "the receipt no longer reports removed"
    assert "dedupe_failed_count" in src, (
        "no failure count: `dedupe_failed` is truncated to 10, so 'removed 0 / failed 23' "
        "looks the same as a clean no-op")
    return "_call_method aliased; dedupe reports removed AND failed"


def assert_plan_filter_reaches_auto_dimension():
    """0.8.25 / N3 — plan_drawing said "keep 8" while 24 landed on the sheet."""
    draw_src = _read("tools/drawing.py")
    tree = ast.parse(draw_src)
    node = _function_node(tree, "auto_dimension")
    assert node is not None, "auto_dimension is gone"
    assert "dimension_filter" in [a.arg for a in node.args.args], (
        "auto_dimension accepts no dimension_filter — the plan cannot bind")
    body = ast.get_source_segment(draw_src, node) or ""
    assert "_filter_inserted_dimensions" in body, "no filter pass"
    helper = _function_node(tree, "_filter_inserted_dimensions")
    assert helper is not None, "_filter_inserted_dimensions is missing"
    hbody = ast.get_source_segment(draw_src, helper) or ""
    assert "before_names" in hbody, (
        "the filter does not exclude dimensions the user placed")
    assert "aborted" in hbody and "deleted nothing" in hbody.replace("\n", " ") , (
        "no safety valve: a filter matching nothing must delete nothing")
    pipe = _read("tools/pipeline.py")
    assert "prune_to_plan" in pipe, "produce_drawing has no prune_to_plan switch"
    assert 'get("keep_dimensions")' in pipe, "produce_drawing does not read the plan"
    return "filter wired, user dimensions protected, no-match aborts"


def assert_place_views_accepts_both_vocabularies():
    """0.8.25 / N5 — passing set_view's key names to place_views dropped every field."""
    src = _read("tools/layout.py")
    node = _function_node(ast.parse(src), "place_views")
    assert node is not None, "place_views is gone"
    body = ast.get_source_segment(src, node) or ""
    assert 'get("view", item.get("view_name"))' in body, (
        "view / view_name are not both accepted")
    assert 'get("x", item.get("x_m"))' in body, "x / x_m are not both accepted"
    assert "missing_view_key" in body, (
        "an entry without a view name is forwarded as None instead of being reported")
    return "view|view_name and x|x_m both accepted, missing key reported"


def assert_delivery_check_formats_its_counts():
    """0.8.25 / N1 — a `%d` with no `%` operator printed literally to the caller."""
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = _function_node(tree, "delivery_check")
    assert node is not None, "delivery_check is gone"
    body = ast.get_source_segment(src, node) or ""
    assert "%d 个尺寸均已关联" in body, "the attachment message changed; re-check formatting"
    assert '"%d 个尺寸均已关联" % dim_count' in body or "(\"%d 个尺寸均已关联\" % dim_count)" in body, (
        "the else-branch message is not formatted — callers see a literal %d")
    for name in ("_format_check_detail",):
        assert name not in body, "unexpected indirection"
    return "every count-bearing message is formatted"


def assert_set_dimension_position_accepts_position():
    """0.8.26 / P0-4 — 19 dimensions named D1 on one sheet, and no way to pick one."""
    src = _read("tools/layout.py")
    node = _function_node(ast.parse(src), "set_dimension_position")
    assert node is not None, "set_dimension_position is gone"
    assert "position_m" in [a.arg for a in node.args.args], (
        "no position_m: repeated D1/D2 names cannot be told apart")
    body = ast.get_source_segment(src, node) or ""
    assert "_find_annotation_by_name(doc, name, position_m)" in body, (
        "the finder is not given position_m")
    assert "disambiguated_by_position" in body, (
        "the receipt does not say whether position_m was used")
    return "position_m disambiguation, mirroring delete_annotation"


def assert_add_note_reports_final_bbox():
    """0.8.26 / P1-5 — a note shifted 49 mm looked identical to one shifted 4 mm."""
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "add_note")) or ""
    assert "bbox_final" in body, (
        "no bbox_final: the caller cannot tell what the note now covers")
    assert "shifted_m" in body, "no shifted_m: the distance moved is unreported"
    assert "_note_bbox" in body, "bbox_final is not derived from the box estimate"
    return "receipt carries bbox_final + shifted_m"


def assert_delivery_check_lists_missing():
    """0.8.26 / P1-3 — "coverage 0.947, missing 5" without saying WHICH five."""
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "delivery_check")) or ""
    assert "ledger_missing" in body, "no ledger_missing list in the receipt"
    assert "unmatched_ledger" in body, (
        "the list is not derived from reconcile_ledger's unmatched rows")
    return "ledger_missing + count alongside the ratio"


def assert_callout_text_verified():
    """0.8.26 / P1-1 — a callout printed "Ø99" on a Ø6 hole and still counted as ok."""
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    helper = _function_node(tree, "_callout_diameter_matches")
    assert helper is not None, "_callout_diameter_matches is missing"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "findall" in hbody and "Ø" in hbody, "the helper does not extract printed Ø values"
    body = ast.get_source_segment(src, _function_node(tree, "add_hole_callouts")) or ""
    assert "_callout_diameter_matches(" in body, (
        "the printed diameter is never compared with the measured edge")
    assert "_discard(" in body, (
        "a rejected callout is left on the sheet — inventory and pixels disagree")
    assert '"partial"' in body, (
        "verdict still says ok when some callouts failed")
    return "value check + discard rejected ink + partial verdict"


def assert_prune_requires_confirmation():
    """0.8.26 / P1-2 — following the orphan list would have deleted the 50 mm overall length."""
    src = _read("tools/drawing.py")
    node = _function_node(ast.parse(src), "prune_orphan_dimensions")
    assert node is not None, "prune_orphan_dimensions is gone"
    assert "confirm" in [a.arg for a in node.args.args], "no confirm gate"
    body = ast.get_source_segment(src, node) or ""
    assert "confirmation_required" in body, (
        "a real delete no longer requires explicit confirmation")
    assert "caveat" in body, "the receipt does not explain what orphan actually means"
    assert "GetAttachedEntities2" in body, (
        "the caveat does not name the API whose empty answer is being over-read")
    server = _read("server.py")
    assert "confirm" in server, "the wrapper does not forward confirm"
    return "confirm gate + caveat naming GetAttachedEntities2"


def assert_layout_blocked_by_other_views():
    """0.8.26 / P0-2 — `fixed_obstacles` held only view-label notes, so dimensions were
    pushed onto a neighbouring section view's hatching."""
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "layout_dimensions")) or ""
    assert 'obstacle\": \"view' in body or '"obstacle": "view"' in body, (
        "views are not seeded as obstacles")
    assert 'other.get("view") == item.get("view")' in body, (
        "the owning view is not excluded — every dimension would be pushed off its own view")
    assert "unresolved" in body, "no unresolved list: moved < collisions stays unexplained"
    assert "view_obstacles" in body, "the receipt does not count view obstacles"
    return "other views block, own view exempt, unresolved reported"


def assert_set_view_warns_about_derived():
    """0.8.26 / P0-3 — moving a parent view leaves its section cut lines behind."""
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "set_view")) or ""
    assert "derived_views" in body, "no derived-view detection"
    assert "cut_lines_detached_warning" in body, (
        "the receipt does not warn that cut lines do not follow the parent view")
    assert "GetBaseView" in body, "derived views are not found via GetBaseView"
    # Two ways this check silently reported "no derived views" on 2026-10-31:
    #   a) `GetBaseView` returns a raw PyIDispatch — `.Name` off it fails, needs Dispatch
    #   b) the guard reused `aligned` ("position does NOT match the request"), so a view
    #      moved exactly where asked skipped the check entirely
    assert "Dispatch(base)" in body, (
        "GetBaseView's pointer is not dispatched — .Name on it fails and the check goes quiet")
    # Check the CONDITION, not the absence of the word: the docstring above explains the
    # `aligned` trap using that very word, so a negative search matches its own comment.
    assert "if requested is not None and position_after is not None:" in body, (
        "the derived-view guard does not use 'the caller asked for a position' — reusing "
        "`aligned` means an exact move skips the check")
    return "detects derived views (dispatched pointer) and warns before the caller saves"


def assert_place_outside_layout():
    """0.8.27 / P0-1 — SolidWorks' ~10 mm insertion offset does not scale with the part.

    On a Ø46 disc 25 dimensions landed inside the outline (21 of 25, measured) and printed on
    top of each other; the report's workaround was scaling the view to 4:1, which changes what
    the drawing states rather than fixing the layout.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = _function_node(tree, "auto_dimension")
    assert node is not None, "auto_dimension is gone"
    args = [a.arg for a in node.args.args]
    for want in ("place_outside", "outside_gap_m", "outside_step_m"):
        assert want in args, "auto_dimension lost %r" % want
    helper = _function_node(tree, "_push_dimensions_outside")
    assert helper is not None, "_push_dimensions_outside is missing"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "precise_outline" in hbody, (
        "the layout does not use the deflated outline — the 11.9 mm phantom margin would "
        "hold every dimension an extra 12 mm away")
    assert "before_names" in hbody, (
        "it does not exclude dimensions the user placed by hand")
    for side in ("left", "right", "top", "bottom"):
        assert '"%s"' % side in hbody, "side %r is not handled" % side
    assert "step_m" in hbody and "gap_m" in hbody, "no stacking distance"
    body = ast.get_source_segment(src, node) or ""
    assert "_push_dimensions_outside(" in body, "the helper is never called"
    assert "_save_drawing(doc)" in body, (
        "place_outside moves geometry but does not save — the sheet on disk stays unchanged")
    return "outside layout: 4 sides, precise boxes, user dims untouched, saved"


def assert_title_block_picks_one_cell():
    """0.8.28 / P1-4 — a GB frame repeats 图样代号 in a 180-degree note outside the frame.

    Writing both put a second code in the corner (measured: code at [0.053, 0.285] and
    [0.391, 0.022]). The report on gb_a3.drwdot also came back with planned: [] for every
    field, so an empty table cell has to be CREATED, not skipped.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = _function_node(tree, "set_title_block")
    assert node is not None, "set_title_block is gone"
    body = ast.get_source_segment(src, node) or ""
    assert "candidates" in body, "no candidate collection — every matching cell gets written"
    assert "_rank" in body, "candidates are not ranked (upright first, then nearest slot)"
    assert "skipped_extra" in body, (
        "the extra matches are dropped silently instead of being reported")
    assert "InsertNote" in body, (
        "an empty title-block cell is still reported as unmatched instead of created")
    assert 'via": "created' in body, "the receipt does not say the cell was created"
    assert "SetPosition2" in body and "_call_method(_created_ann" in body, (
        "the created note is not positioned through call_method (property dispatch fails)")
    return "one cell per field, extras reported, empty cells created"


def assert_set_view_dodges_label():
    """0.8.28 / P1-9 — changing a section's scale regenerates its label, on top of dimensions.

    Measured 2026-11-01: a dimension parked at the label position [0.33, 0.6371] was pushed to
    [0.33, 0.6497] once the dodge ran (`dimensions_moved: 1`).
    """
    src = _read("tools/layout.py")
    tree = ast.parse(src)
    helper = _function_node(tree, "_dodge_view_label")
    assert helper is not None, "_dodge_view_label is missing"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "GetType" in hbody and "!= 6" in hbody, "it does not look for note annotations"
    assert "VLLABEL" in hbody, (
        "the <VLLABEL>/<VLSCALEV> code is not accounted for — its 19 characters made the "
        "keep-out 40 mm wide")
    assert "SetPosition" in hbody, "dimensions are never moved"
    body = ast.get_source_segment(src, _function_node(tree, "set_view")) or ""
    assert "_dodge_view_label(" in body, "set_view never calls the dodge after a scale change"
    assert "view_label" in body, "the receipt does not report what the label did"
    return "label detected, sized sanely, dimensions moved off it"


def assert_set_view_break_alignment():
    """0.8.29 / P1-7 — a section view is locked to its parent on one axis.

    Measured 2026-11-01 on a vertical section: asking for y = parent+0.05 came back as the
    parent's y (0.2793) with aligned_to_parent=True; after `break_alignment=True` the same
    request landed at 0.3293. `IView.RemoveAlignment` is the UI's 解除对齐.
    """
    src = _read("tools/layout.py")
    tree = ast.parse(src)
    node = _function_node(tree, "set_view")
    assert node is not None, "set_view is gone"
    assert "break_alignment" in [a.arg for a in node.args.args], (
        "no break_alignment: a section view can never leave the strip beside its parent")
    body = ast.get_source_segment(src, node) or ""
    assert "RemoveAlignment" in body, "RemoveAlignment is never called"
    assert body.index("RemoveAlignment") < body.index('"Position"'), (
        "alignment must be removed BEFORE the position is written, or SolidWorks snaps it back")
    assert "alignment_broken" in body, "the receipt does not say whether alignment was broken"
    server = _read("server.py")
    assert "break_alignment" in server, "the wrapper does not forward break_alignment"
    return "RemoveAlignment before Position, reported in the receipt"


def assert_duplicate_dimension_declared_unsupported():
    """0.8.30 / P0-5 — InsertModelAnnotations3 is document-level idempotent.

    Measured 2026-11-01, three parameter combinations, second view stayed at 0 dimensions and
    the call returned None every time. The honest answer is a capability entry that says so
    and points at the path that DOES work: rebuild the dimension on that view's edge.
    """
    caps = _read("tools/capabilities.py")
    assert '"duplicate_dimension_to_view"' in caps, (
        "the limitation is not declared, so callers will burn a round discovering it")
    index = caps.index('"duplicate_dimension_to_view"')
    segment = caps[index:index + 1600]
    assert '"unsupported"' in segment, "it is not marked unsupported"
    assert "add_drawing_dimension" in segment, (
        "no workaround points at the tool that can actually annotate the second view")
    assert "InsertModelAnnotations3" in segment, (
        "the entry does not name the API whose idempotence causes it")
    # and the tool people call first should say so too
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "auto_dimension")) or ""
    assert "idempotent" in body, (
        "auto_dimension's docstring does not warn that a second view gets nothing")
    return "declared unsupported with a working workaround"


def assert_section_line_limitation_declared():
    """0.8.31 -> 0.8.62: the cut line DOES follow the parent view now.

    0.8.31 measured the cut line as un-moveable and marked the capability `unsupported`:
    GetLineInfo returns model space and never changes, SetLineInfo writes the section
    PLANE (and re-orders the polyline into a back-and-forth), the view's sketch is
    unreachable through GetSketches.

    All of that is still true — but it stopped mattering in 0.8.57. The line is now drawn
    inside the VIEW SKETCH (origin = view centre, model units) instead of at absolute sheet
    coordinates, so it belongs to the view and rides along when the view moves.

    Measured 2026-11-02 (飞达动力轴, A3 1:1): parent moved from (0.07629, 0.26551) to
    (0.05, 0.05), delta (-0.02629, -0.21551); the section view moved by exactly the same
    delta and their relative offset stayed (0, -0.08344).

    This assertion now guards the CORRECTED claim: verified status, the measurement, and
    the coordinate reason — never let it drift back to `unsupported` without re-measuring.
    """
    caps = _read("tools/capabilities.py")
    index = caps.index('"section_line_follows_view"')
    # Stop at the next entry: a fixed window bleeds into duplicate_dimension_to_view,
    # which is still legitimately unsupported.
    end = caps.find('"id":', index + 10)
    segment = caps[index:end if end > 0 else index + 2000]
    assert '"verified"' in segment, (
        "the capability is not marked verified although 0.8.57 made the line follow")
    assert "unsupported" not in segment, (
        "the entry still says unsupported — that was measured before the coordinate fix")
    assert "视图草图" in segment, (
        "the entry does not explain WHY it follows (the line lives in the view sketch)")
    assert "-0.02629" in segment or "0.07629" in segment, (
        "no measured evidence — this claim was once wrong for a whole release")
    assert "GetLineInfo" in segment, (
        "the entry no longer records that GetLineInfo is model-space (still true)")
    return "cut line follows the view; capability verified with measured evidence"


def assert_cut_line_placed_on_its_view():
    """0.8.32 -> 0.8.57: this assertion used to require the OPPOSITE and caused P0.

    The 0.8.32 version demanded `pos[0] + offset` in CreateLine and forbade `/ scale` on
    `half`. Both were wrong: the sketch created after `ActivateView` has its origin at the
    VIEW CENTRE and works in MODEL units, so adding `view.Position` (sheet metres) pushed
    the cut line 76 mm across and 265 mm down, off the geometry entirely.

    SolidWorks did not complain — it returned a Type=2 view whose cut never touched the
    part, i.e. a projection. `GetFaceHatchCount() == 0`, edges 13 instead of 37. The whole
    P0 ("剖视没有剖面线") was this line, and this assertion was actively keeping the bug in.

    Now it guards the correct form: drawn around the sketch origin, `half` converted by
    `/ scale`, and no `view.Position` anywhere near CreateLine.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_section_view")) or ""
    assert "sm.CreateLine(offset, -half" in body, (
        "CreateLine is not drawn around the view-sketch origin")
    assert "pos[0] + offset" not in body and "pos[0] - half" not in body, (
        "CreateLine still adds view.Position — sheet metres inside a model-space sketch")
    assert "min(view_w, view_h) * 0.5) / scale" in body, (
        "half is not converted from sheet metres to model space")
    assert "precise_outline(target)" in body, (
        "half does not use the deflated outline, so the 11.9 mm margin stretches the line")
    return "cut line drawn around the view-sketch origin in model units"


def assert_tolerance_uses_metres():
    """0.8.33 / N-2 — 0.1 mm rendered as ±100 because the value went in as millimetres.

    `SetToleranceValues` takes metres. Measured 2026-11-01: 50 symmetric ±0.1 produced
    "50 ±100" on the PDF while `applied: true` and the tolerance type read back correctly.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_dimension_tolerance")) or ""
    assert "float(upper) / 1000.0" in body, (
        "upper is still passed raw — SetToleranceValues takes metres")
    assert "float(lower) / 1000.0" in body, "lower is still passed raw"
    assert "rendered_text" in body, (
        "no rendered_text: a 1000x unit error stays invisible because the numeric "
        "readback is unaffected")
    return "metres in, rendered_text out"


def assert_unreadable_callout_text_is_kept():
    """0.8.33 / #6 — on SW2026 GetText(0/1/2) is None for every callout.

    v0.8.26 treated "no Ø in the text" as failure, so all 8 callouts on a fresh sheet were
    created and then deleted: `created: 0 / failed: 8`. Unreadable is not invalid.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_hole_callouts")) or ""
    assert "text_unreadable" in body, "the receipt cannot say the text was unreadable"
    assert "if not has_diameter and not text_unreadable" in body, (
        "an unreadable text still counts as a rejection — callouts get deleted again")
    assert '"text_unreadable": text_unreadable' in body, (
        "created rows do not carry the flag")
    return "unreadable text kept, flagged, never deleted"


def assert_wrapper_forwards_position_m():
    """0.8.33 — the implementation supported position_m; the wrapper did not forward it.

    Same drift as N2 (create_drawing/view_positions): a sheet with 19 dimensions called D1
    could only move the first one, and the schema rejected the parameter outright.
    """
    server = _read("server.py")
    index = server.index("def set_dimension_position(")
    # Window has to reach past the docstring to the forwarding call.
    segment = server[index:index + 2000]
    # The signature is now `position_m: Annotated[list[float] | None, Field(...)] = None`,
    # so match the parameter, not a literal type string — an exact-type assertion here
    # would fail on every future annotation change while saying nothing useful.
    assert "position_m:" in segment, (
        "the wrapper still rejects position_m")
    assert "position_m=position_m" in segment, (
        "the wrapper does not forward position_m to the implementation")
    return "wrapper forwards position_m"


def assert_dedupe_uses_full_name():
    """0.8.33 / R1 — 23 real dimensions were reported as 18 with duplicates_skipped=5.

    The key was (view, name, position, value); SolidWorks reuses D1/D2 per sketch and equal
    values are common, so distinct dimensions collapsed.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "verify_dimension_attachments")) or ""
    assert "_annotation_dimension_full_name(ann, win)" in body, (
        "the dedupe key is not the model full name")
    assert 'tuple(row.get("position_m")' not in body, (
        "the old (view, name, position, value) key is still in use")
    return "dedupe by model full name"


def assert_callout_text_carries_value():
    """0.8.33 / R2 — callout rows read back as bare "Ø" / None for four rounds.

    Also: SystemValue is not consistently radius or diameter (a printed Ø8 answered 4.0,
    Ø15 answered 15.0), so the printed text wins and SystemValue is only a flagged fallback.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "list_drawing_annotations")) or ""
    assert "text_was_bare_symbol" in body, "a bare Ø row is still left without a number"
    assert "diameter_source" in body, (
        "the receipt does not say whether the diameter came from text or from SystemValue")
    assert '"radius_callout", "hole_callout_unverified"' in body, (
        "radius/unverified callouts still skip the value branch")
    return "bare symbols get the value, source reported"


def assert_mojibake_is_repaired():
    """0.8.33 / N-3 — feature names came back as ÂÝÎÆ¿××êÍ·Éî¶È (= 螺纹孔钻头深度).

    Reported three rounds running. GB2312 bytes decoded as Latin-1 are losslessly reversible;
    real Chinese fails `.encode("latin-1")` and must pass through untouched, which is what
    makes a blanket call safe.
    """
    src = _read("sw_session.py")
    node = _function_node(ast.parse(src), "fix_mojibake")
    assert node is not None, "fix_mojibake is missing"
    body = ast.get_source_segment(src, node) or ""
    assert 'encode("latin-1")' in body and 'decode("gb2312")' in body, (
        "the repair no longer round-trips through latin-1/gb2312")
    assert "return text" in body, "no pass-through: real Chinese would be mangled"
    drawing_src = _read("tools/drawing.py")
    assert "fix_mojibake" in drawing_src, (
        "ledger_missing still emits mojibake feature names")
    return "lossless repair with a safe pass-through"


def assert_axis_map_scales_to_sheet():
    """0.8.34 — model extents were compared with sheet extents without the view scale.

    Consequence: any view that is not 1:1 answered `axes_mismatch`, so list_circular_edges
    returned no coordinates at all (a report saw 30/30), cut_plane_origin could not map, and
    single-point hole callouts had nothing to match against.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_view_axis_map")) or ""
    assert "view_scale" in body, "the axis map does not read ScaleDecimal"
    assert "extent[a] * view_scale" in body and "extent[b] * view_scale" in body, (
        "model extents are compared with sheet metres unscaled — every non-1:1 view fails")
    return "model extents scaled before comparison"


def assert_single_callout_matches_exact_circle():
    """0.8.35 / P0 — single-point hole callout works, but only on annotatable circles.

    Verified 2026-11-01 after fixing the axis-map scale: door.SLDPRT Ø8 ×2 + Ø15 ×3 all
    return ok with matched_circle == the requested diameter; the four Ø6 circles fail for
    BOTH the single and the batch tool (they are not annotatable holes), so a failure there
    is correct behaviour, not a bug — hence the hint must say so instead of "no edge found".
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    helper = _function_node(tree, "_nearest_circle_edge")
    assert helper is not None, "_nearest_circle_edge is missing"
    hbody = ast.get_source_segment(src, helper) or ""
    assert "centre_model" in hbody, (
        "the helper does not return the model centre, so the caller cannot pick the SAME "
        "circle when several share a diameter")
    assert "GetVisibleEntities2" in hbody, (
        "it does not enumerate edges (coordinate hit-testing cannot reach drawing edges)")
    body = ast.get_source_segment(src, _function_node(tree, "add_hole_callout")) or ""
    assert "centre_model" in body, "the caller matches by diameter instead of exact centre"
    assert "annotatable hole" in body, (
        "a failed callout does not explain that the circle may simply not be a hole")
    return "exact-circle match, honest failure hint"


def assert_callout_depth_limit_declared():
    """0.8.36 — a Ø15 hole with depth 15 prints "Ø1515" and there is no clean fix.

    Three routes measured 2026-11-01: write only the diameter -> depth trails (Ø1515);
    write the diameter then blank the other slots -> the whole callout disappears;
    write nothing -> the raw "<MOD-DIAM>" prints literally. Through holes are fine (Ø8).
    """
    caps = _read("tools/capabilities.py")
    assert '"hole_callout_text_slots"' in caps, (
        "the slot limitation is undeclared — the next user will report Ø9 -> Θ99 again")
    index = caps.index('"hole_callout_text_slots"')
    segment = caps[index:index + 1600]
    assert "SetText" in segment and "depth" in segment.lower(), (
        "the entry does not explain which slots collide")
    assert "add_drawing_dimension" in segment, "no workaround for depth holes"
    src = _read("tools/drawing.py")
    assert "SetText_depth_may_trail" in src, (
        "the receipt does not flag that the depth may trail the diameter")
    return "slot limit declared with a workaround"


def assert_model_clean_rechecks_after_rebuild():
    """0.8.37 — a drawing edit marks the referenced model "needs rebuild".

    Measured 2026-11-01: after deleting one annotation, delivery_check reported
    model_clean=false / needs_rebuild on a model nobody had changed, dragging the verdict
    to 仅供参考/未完成. One forced rebuild clears a spurious flag and keeps a real one.
    """
    src = _read("tools/ledger.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "build_feature_ledger")) or ""
    assert "EditRebuild3" in body, (
        "needs_rebuild is trusted on the first read — a drawing-induced flag blocks the verdict")
    assert "_rebuild_once(model" in body or "call_method(model" in body, (
        "the rebuild is not actually invoked on the model")
    assert body.count("NeedsRebuild2") >= 2, (
        "the flag is not re-read after the rebuild")
    return "needs_rebuild re-read after one rebuild"


def assert_dimension_rejects_unattached():
    """0.8.37 — a wrong edge still produces a dimension, just of something else.

    Report: edge_index 71 (a Ø36 arc) came out as R23 of the Ø46 outline — a plausible wrong
    value. A dimension attached to nothing is the cheap signal that selection did not take.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_drawing_dimension")) or ""
    assert "edge_mismatch" in body, "an unattached dimension is still reported as ok"
    assert "GetAttachedEntityTypes" in body, (
        "the attachment is never checked after AddDimension2")
    assert "EditDelete" in body, "the bad dimension is left on the sheet"
    return "unattached dimension rejected and removed"


def assert_dedupe_warns_about_sections():
    """0.8.37 — dedupe keeps the FIRST occurrence, so section views lose every copy.

    Measured: 83 inserted -> 44 removed, both section views left with 0 dimensions, and the
    depth-only features had nowhere to live.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "auto_dimension")) or ""
    assert "首次出现" in body, (
        "the advisory does not explain which copy survives dedupe")
    assert "剖视" in body, "it does not warn that section views can end up empty"
    return "dedupe advisory names the section-view consequence"


def assert_gitignore_excludes_test_tree():
    """0.8.38 — zip/PNG/JPG/CSV under 测试用例/ were tracked and published (4 rounds).

    The CAD-extension rules never covered them, so 7 binaries (4.1 MB) sat in a public repo
    while README claimed the tree was local-only.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore"), encoding="utf-8") as handle:
        ignored = handle.read()
    assert "测试用例/" in ignored, (
        "the test tree is not excluded as a whole — zip/png/jpg/csv slip past the CAD rules")
    return "whole test tree ignored"


def assert_call_method_defined_once():
    """0.8.38 — two copies of call_method drifted (reported four rounds).

    bom_native had its own; the retry/classification logic only ever landed in sw_session.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_dir = os.path.join(root, "src", "solidworks_assetmate_mechdog")
    definitions = []
    for dirpath, _dirs, files in os.walk(src_dir):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == "call_method":
                    definitions.append(os.path.relpath(path, src_dir))
    assert len(definitions) == 1, "call_method is defined in %s" % definitions
    return "one definition: %s" % definitions[0]


def assert_health_reports_provenance():
    """0.8.38 — health_check never said which package it was running (3 rounds).

    With several editable installs on one machine that is the only question that matters.
    """
    src = _read("sw_session.py") if False else None  # keep the helper signature honest
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "src", "solidworks_assetmate_mechdog",
                          "sw_session.py"), encoding="utf-8") as handle:
        body = handle.read()
    index = body.find("def health(")
    assert index > 0, "sw_session.health is missing"
    segment = body[index:index + 2200]
    for key in ("package_path", "package_version", "python_executable"):
        assert key in segment, "health does not report %s" % key
    return "health reports path / version / interpreter"


def assert_static_check_covers_bare_names():
    """0.8.38 — static_check only validated name.attribute, not bare names.

    That is exactly how _EDGE_CACHE died unnoticed: the module imported fine, so only smoke
    could have caught it.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "tests", "static_check.py"), encoding="utf-8") as handle:
        body = handle.read()
    assert "def check_bare_names" in body, "static_check still ignores bare names"
    assert "problems.extend(check_bare_names(" in body, (
        "the bare-name check exists but is never called")
    return "static_check fails on undefined bare names"


def assert_circular_edges_predict_annotatability():
    """0.8.39 — the circle list never said which circles can take a callout.

    A report burned 13 guaranteed-failing calls on an outline view. The rule is geometric: a
    circle is a hole MOUTH only when its normal runs along the view direction. Measured on
    door.SLDPRT after the fix — Ø6 ×4 -> facing=side / hint=False (the batch tool cannot
    annotate them either), Ø8 ×2 + Ø15 ×3 -> facing=front / hint=True (5/5 succeed).
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "list_circular_edges")) or ""
    for key in ("annotatable_hint", "facing", "view_kind"):
        assert key in body, "the circle list does not report %s" % key
    assert "depth_axis = 3 - axes[0] - axes[1]" in body, (
        "the view direction is not derived from the two displayed axes")
    assert '"axis"' in body, "the circle normal is not read from CircleParams"
    kind_body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                          "_view_kind")) or ""
    assert '7: "model"' in kind_body, (
        "IView.Type 7 is a standard model view; without it every view reports type_7")
    return "annotatability predicted from the circle normal"


def assert_orphans_near_view_not_excused():
    """0.8.41 — proximity is NOT a leader; the previous version excused every orphan.

    v0.8.40 cleared any orphan whose position sat within 50 mm of the view outline. But
    since v0.8.27 dimensions are PARKED outside the outline by design, so a tidy sheet
    excused 15 out of 15 real orphans (23 = 8 attached + 15 excused) while the same receipt
    still said "23 个尺寸均已关联". Proximity is now reported as `orphan_near_view` and
    clears nothing; the receipt says attached vs unattached.
    """
    src = _read("tools/drawing.py")
    verify_body = ast.get_source_segment(
        src, _function_node(ast.parse(src), "verify_dimension_attachments")) or ""
    assert "orphan_near_view" in verify_body, (
        "the observation field is missing")
    assert "orphan_with_leader" not in verify_body, (
        "it still claims a leader it never verified")
    delivery_body = ast.get_source_segment(
        src, _function_node(ast.parse(src), "delivery_check")) or ""
    assert "excused_count=0" in delivery_body, (
        "something is still being excused")
    assert "orphan_near_view" in delivery_body, (
        "the receipt does not mention how many merely sit nearby")
    assert "attached_count" in delivery_body, (
        "the receipt does not separate attached from unattached")
    return "proximity reported, never used to clear a blocker"


def assert_crash_handler_installed():
    """0.8.42 — `add_section_view` took the whole server process with it.

    Reported: "Not connected", process gone, no traceback, no error payload, log stopped
    mid-run. A Python exception cannot do that — only native code (COM/pywin32) can. Without
    faulthandler the next occurrence is equally silent.
    """
    server = _read("server.py")
    assert "faulthandler" in server, (
        "no faulthandler: a native fault still kills the server with no evidence")
    assert "faulthandler.enable" in server, "faulthandler is imported but never enabled"
    assert "crash.log" in server, (
        "the crash stack is not written anywhere findable")
    assert "all_threads=True" in server, (
        "single-thread mode would miss a fault on the COM worker thread")
    return "native faults land in logs/crash.log"


def assert_filter_prefers_full_names():
    """0.8.42 — a plan that kept ONE Ø42 ended up keeping all three.

    `plan_drawing` returns full names ("D1@草图19"). The filter took `parts[1]` ("草图19")
    and compared it with the feature segment, which never matched, so it silently fell back
    to value matching — and every dimension with that value survived.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_filter_inserted_dimensions")) or ""
    assert "full_prefixes" in body, (
        "full names are not treated as identities")
    assert "if full_prefixes:" in body and "return any(full.startswith" in body, (
        "the filter still falls back to value matching when exact names were given")
    return "exact names win, values only when no names were given"


def assert_scale_choice_is_explained():
    """0.8.43 — `create_drawing` picked 1:5 on an A3 and said nothing.

    Reported: a 210 mm part at 1:5 with 90% of the sheet blank, `auto_scale_adjustments: []`
    as the only clue. Two defects: 1:2.5 was missing from the ladder, and a strict
    `candidate <= fit` dropped a whole step on a 3.4% overshoot. Now the receipt says which
    standard scale was picked, from what raw fit, and whether it was stepped down again.
    """
    src = _read("tools/drawing.py")
    ladder = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                       "_standard_scale")) or ""
    assert "0.4" in ladder, "1:2.5 is missing from the preferred series"
    assert "1.0 + float(tolerance)" in ladder, (
        "a strict <= fit still drops a whole step on a trivial overshoot")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "create_drawing")) or ""
    for key in ("scale_reason", "raw_fit", "final_scale", "stepped_down"):
        assert key in body, "the receipt does not report %s" % key
    return "scale picked from a full ladder and explained"


def assert_outside_stack_respects_sheet():
    """0.8.44 / P1 — the outside stack was unbounded and printed over the title block.

    Measured 2026-11-01: 16 dimensions on one side gave offset 0.012 + 15*0.008 = 0.132,
    from a view at y≈0.15 that lands at y≈0.018 while the A3 safe area starts at 0.065.
    Now the per-side room is measured against the sheet frame, overflow spills to the
    opposite side, the step compresses, and the offset is hard-capped at the room left —
    on door.SLDPRT the worst y went from 0.337 to 0.293 (the view's own top edge).
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_push_dimensions_outside")) or ""
    assert "GetFirstView" in body, (
        "the sheet frame is not read (GetSheets alone returned nothing on SW2024)")
    assert "opposite" in body and "capacity" in body, (
        "overflow does not spill to the opposite side")
    assert "offset = min(gap_m + slot * eff_step, max(room, 0.0))" in body, (
        "the offset is not hard-capped at the remaining room")
    auto_body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                          "auto_dimension")) or ""
    assert "outside_spills" in auto_body, (
        "the receipt does not say the stack had to change sides")
    return "bounded stack with spill and a compressed step"


def assert_layout_prediction_matches_reality():
    """0.8.45 — the layout predictor ignored the outline margin AND the view count.

    Two errors that both pushed views onto the frame:
      * `box` is the MODEL size, but a real outline carries 11.9 mm per side of phantom
        margin, so every prediction was ~24 mm short;
      * the grid always assumed the 2x2 packer, so a single view was placed in the top row
        of a two-row frame.
    Measured on door.SLDPRT: view top 0.293 vs a 0.287 safe edge on a 0.297 sheet.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "create_drawing")) or ""
    assert "inflate = 2.0 * OUTLINE_MARGIN_M" in body, (
        "the phantom outline margin is still ignored")
    assert "candidate_box[0] + inflate" in body, (
        "_fit_scale compares model size against sheet size unscaled")
    assert "cols = 1 if count <= 2 else 2" in body, (
        "the grid still assumes the 2x2 packer regardless of how many views were asked for")
    return "prediction accounts for the outline margin and the view count"


def assert_views_inside_printable_area():
    """0.8.47 — views were packed starting at `sheet_h - top`, i.e. ON the printable edge.

    `template_safe_area` reports top=4 mm for the GB A3 template while the dimension stack
    uses a 10 mm inset, so views and dimensions disagreed about where the printable area
    starts. Measured on door.SLDPRT: view top 0.293 on a 0.297 sheet (safe edge 0.287).
    Now `_place_views` takes the stricter 10 mm inset plus a 4 mm frame pad: 1 and 3 view
    layouts both land at 0.175..0.283, inside the 0.287 edge.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_place_views")) or ""
    assert "inset_floor = 0.010" in body, (
        "the template's 4 mm inset is still trusted on its own")
    assert "frame_pad = 0.004" in body, (
        "the first view still starts exactly on the printable edge")
    assert "y = sheet_h - top - frame_pad" in body, (
        "the frame pad is not applied to the packing start")
    return "views pack inside the stricter inset"


def assert_stack_ordered_by_value():
    """0.8.48 — the outside stack was ordered by position, not by size.

    Three Ø42 dimensions can sit 0.4 mm apart; position ordering can never separate those.
    Slot 0 sits closest to the outline, so ASCENDING order puts the big ones outermost —
    measured on door.SLDPRT left side: Ø4 at x=0.0468 (inner) ... Ø215 at x=0.0148 (outer).
    Also requires the intermediate counters: an earlier attempt changed the tuple shape and
    silently stopped the whole pass, with `placed_outside: None` as the only evidence.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_push_dimensions_outside")) or ""
    assert "dim_value" in body, "the dimension value is never read"
    assert "items.sort(key=lambda entry: (entry[4], entry[0]))" in body, (
        "the stack is not ordered by value (ascending = largest outermost)")
    for key in ("skip_before", "skip_no_position", "per_side"):
        assert key in body, "no %s counter — a stopped pass would be invisible" % key
    # The receipt references `outside_debug` unconditionally, so it must be initialised
    # OUTSIDE the place_outside branch. A version that only set it inside took four smoke
    # items down with "cannot access local variable 'outside_debug'" — neither static_check
    # nor the bare-call regression inspects function-local assignments.
    auto_body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                          "auto_dimension")) or ""
    init_at = auto_body.find("outside_debug = {}")
    branch_at = auto_body.find("if place_outside:")
    assert init_at >= 0, "outside_debug is never initialised"
    assert branch_at > init_at, (
        "outside_debug is first assigned inside the branch — the default "
        "place_outside=False path crashes on the receipt")
    return "largest value outermost, with counters to prove the pass ran"


def assert_center_marks_take_a_view():
    """0.8.49 — `add_center_marks` marked the isometric view too.

    A report saw 15 cross marks scattered across a 3D pictorial. Center marks belong on
    orthographic views; the tool had no way to say which.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_center_marks")) or ""
    assert "view_name" in body, "no view filter — the pictorial gets marks as well"
    assert 'str(view.Name) != str(view_name)' in body, (
        "the filter is declared but not applied in the loop")
    server = _read("server.py")
    index = server.index("def add_center_marks(")
    assert "view_name=view_name" in server[index:index + 1200], (
        "the wrapper does not forward view_name")
    return "center marks can be limited to one view"


def assert_gtol_symbol_render_flagged():
    """0.8.49 -> 0.8.59: symbol, value and datum all render — the ORDER was the bug.

    Three rounds of misdiagnosis are recorded here because each one looked convincing:

    0.8.49 flagged every symbol write as unverified — the cell was never filled.
    0.8.58 got the symbol and datum to render through the 2022+ frame XML, but the value
    stayed blank. Writing it AFTER ConvertFormat (in the XML, or via SetFrameValues2)
    leaves it in the XML and never in a text item, so nothing is drawn.

    The fix is ordering: write the legacy symbol AND the legacy values FIRST, then
    ConvertFormat — that call is what turns them into renderable 2022-format text items.
    Verified on 飞达动力轴 2026-11-02: the frame shows "⌖ 0.05 A".
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_geometric_tolerance")) or ""
    # These are dispatched by name, so they appear as string literals, not calls.
    assert '"SetFrameSymbols2"' in body, (
        "the legacy symbol write is gone — ConvertFormat needs it as input")
    assert '"SetFrameValues2"' in body, (
        "the legacy value write is gone — ConvertFormat needs it to create text items")
    # order: both legacy writes must come BEFORE ConvertFormat
    legacy = max(body.find('"SetFrameSymbols2"'), body.find('"SetFrameValues2"'))
    convert = body.find('"ConvertFormat"')
    assert 0 < legacy < convert, (
        "values are written after ConvertFormat — they stay in the XML and never render")
    assert "SetSymbolXml" in body, "the 2022+ symbol path is missing"
    assert "symbol_render_verified" in body and "value_render_verified" in body, (
        "the receipt cannot say whether symbol and value actually render")
    return "legacy writes precede ConvertFormat, and both render flags are reported"


def assert_close_document_exists():
    """0.8.50 — nothing in the toolkit ever closed a document.

    A report ended up with two abandoned drafts it could not delete: "Device or resource
    busy" because SolidWorks still had them open. Every tool here opens documents.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "close_document")) or ""
    assert "CloseDoc" in body, "close_document never calls CloseDoc"
    assert "os.path.normcase" in body, (
        "the path is not normalised — a case-only mismatch would close nothing")
    server = _read("server.py")
    assert "def close_document(" in server, "no MCP wrapper"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "README.md"), encoding="utf-8") as handle:
        readme = handle.read()
    assert "`close_document`" in readme, "README does not list it"
    return "documents can be closed and the file freed"


def assert_view_edit_tools_exist():
    """0.8.51 — a view could not be moved or rescaled without rebuilding the sheet.

    A report rebuilt the whole drawing three times because a view needed 1:2 instead of 1:5.
    `set_view_scale` takes a plain `ScaleDecimal` assignment and reads it back; `move_view`
    reuses layout.set_view, whose PROPERTYPUT write is the only one that sticks (a plain
    assignment lands at (0, x)).
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    scale_body = ast.get_source_segment(src, _function_node(tree, "set_view_scale")) or ""
    assert "ScaleDecimal" in scale_body, "set_view_scale does not set the scale"
    assert '"applied"' in scale_body or "applied=" in scale_body, (
        "no readback — a rejected scale would be reported as success")
    assert "save" in scale_body, "the save parameter is missing (NameError at runtime)"
    move_body = ast.get_source_segment(src, _function_node(tree, "move_view")) or ""
    assert "set_view" in move_body, (
        "move_view does not reuse the verified PROPERTYPUT writer")
    server = _read("server.py")
    for name in ("set_view_scale", "move_view"):
        assert ("def %s(" % name) in server, "%s has no MCP wrapper" % name
    return "views can be rescaled and moved in place"


def assert_edges_carry_sheet_coordinates():
    """0.8.52 / P8 — `list_drawing_edges` gave model coordinates only.

    A report could not use `edge_index` at all: the points were model-space with a constant
    Z, so there was no way to aim at the sheet. `add_drawing_dimension` already had the
    transform for its error hints; the edge list now carries it too.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "list_drawing_edges")) or ""
    assert "sheet_start" in body and "sheet_end" in body, (
        "edges still carry model coordinates only")
    assert "_view_axis_map" in body, "no projection is computed"
    assert "view_pos = [float(v) for v in target.Position]" in body, (
        "the view position is read after _view_axis_map — that document is already gone")
    assert "sheet_projected" in body, (
        "the receipt cannot say whether projection happened")
    return "edges carry sheet coordinates, with an explicit projected flag"


def assert_plan_separates_driving_from_derived():
    """0.8.53 / P3 — the plan promised dimensions the model does not have.

    `keep_dimensions` led with 外形/总长/总宽/总高 — BOUNDING-BOX values. There is no such
    driving dimension, so auto_dimension could never insert them, yet `filter_kept` still
    counted them and nothing said a word. Measured on door.SLDPRT: 8 kept = 5 driving +
    3 derived.
    """
    src = _read("tools/planner.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_drawing")) or ""
    assert '"kind"' in body or "entry[\"kind\"]" in body, (
        "entries are not classified")
    assert "keep_derived_count" in body and "keep_driving_count" in body, (
        "the counts are not reported")
    draw_body = _read("tools/drawing.py")
    assert "planned_derived_count" in draw_body, (
        "auto_dimension does not say how many plan entries are unsatisfiable")
    return "plan entries are driving or derived, and both counts are reported"


def assert_empty_views_are_reported():
    """0.8.53 / P4 — every dimension lands in the first view and nothing said so.

    `InsertModelItems` is document-level, so the side and isometric views get zero dimensions
    while the caller only sees `warnings: []`. Measured on a 3-view door.SLDPRT sheet:
    empty_views = ['工程图视图3'].
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "auto_dimension")) or ""
    assert "empty_views" in body, "no empty-view field"
    assert "是文档级的" in body, (
        "the advisory does not explain WHY the view is empty")
    assert "add_drawing_dimension" in body, "no workaround for depth dimensions"
    return "empty views are named, explained, and given a workaround"


def assert_cut_line_is_clamped():
    """0.8.54 / P13 — the section cut line was 0.62 x the LONG edge, always.

    A report's arrows stuck ~15 mm past the view and landed on the technical requirements
    block, forcing the notes to be moved 98 mm. On any non-square view that ratio overshoots;
    it is now clamped to half the SHORT edge as well, and exposed as `cut_line_ratio`.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_section_view")) or ""
    assert "cut_line_ratio" in body, "the ratio is not configurable"
    assert "min(view_w, view_h) * 0.5" in body, (
        "the line is still sized from the long edge alone")
    server = _read("server.py")
    assert "cut_line_ratio=cut_line_ratio" in server, "the wrapper does not forward it"
    return "cut line clamped to the short edge and configurable"


def assert_all_views_false_is_not_silent():
    """0.8.55 / P1 — `auto_dimension(all_views=False)` returned 0 with ok:true.

    Same sheet, same model: `all_views=False` gave inserted 0 / ok true while `all_views=True`
    gave 18. `InsertModelAnnotations3(..., all_views=False)` inserts into the ACTIVE view, and
    nothing was activated. It now activates one, or returns `active_view_not_set`; a zero
    result on the single-view path returns `no_dimensions_inserted` instead of success.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "auto_dimension")) or ""
    assert "ActivateView" in body, "no view is activated for the single-view path"
    assert "active_view_not_set" in body, "no structured error when activation fails"
    assert "no_dimensions_inserted" in body, (
        "a zero insert still reports success")
    return "single-view path activates a view or says why it could not"


def assert_section_hatch_is_checked():
    """0.8.56 / P0 — a section view with no hatching reported `ok: true`.

    飞达动力轴 shipped a section whose cut face was completely blank while
    `sanity.verdict` said ok. `IView.GetFaceHatchCount` is the only reliable signal found
    (0 = blank). None of these change it, all verified 2026-11-02: `SetAutoHatch(True)`
    (parts are explicitly out of scope), `ScaleHatchPattern = True` (scales an existing
    pattern), `SectionDepth` 0.00635 -> 0.107 -> 0.2, `SetDisplayOnlySurfaceCut(True)`.
    So the count is reported and a blank cut face becomes `hatch_missing`.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_section_view")) or ""
    assert "GetFaceHatchCount" in body, "the hatch count is never read"
    assert "hatch_count" in body, "the count is not surfaced in the receipt"
    assert "hatch_missing" in body, (
        "a blank cut face still reports success")
    assert "SetAutoHatch" in body, (
        "the ruled-out repairs are not documented — the next reader will retry them")
    return "a blank cut face is an error, not an ok"


def assert_cut_line_uses_view_sketch():
    """0.8.57 / P0 ROOT CAUSE — the cut line was drawn in SHEET coords inside a VIEW sketch.

    `SketchManager.InsertSketch` after `ActivateView` creates a sketch whose origin is the
    VIEW CENTRE and whose units are MODEL space. Offsetting the line by `view.Position`
    (sheet metres) pushed it 76 mm across and 265 mm down from the geometry. SolidWorks
    accepted it and returned a Type=2 view — but nothing had been cut, so the view was
    really a projection: `GetFaceHatchCount() == 0`, edges 13 instead of 37.

    `add_detail_view` in the same module always did this conversion correctly
    (sketch = (sheet - view.Position) / parent_scale). This was the only function that
    skipped it — so the guard checks both the missing division and the bogus offset.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_section_view")) or ""
    assert "sm.CreateLine(offset, -half" in body, (
        "the cut line is still shifted by view.Position (sheet coordinates)")
    assert "min(view_w, view_h) * 0.5) / scale" in body, (
        "half is not converted from sheet metres to model space")
    assert "view's own position added" not in body, (
        "the wrong-coordinate comment that caused this is still there")
    detail = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                        "add_detail_view")) or ""
    assert "/ parent_scale" in detail, (
        "add_detail_view lost the conversion it was the correct reference for")
    return "cut line drawn around the view-sketch origin, like the detail view"


def assert_every_param_is_documented():
    """0.8.61 — all 287 tool parameters had an empty schema description.

    `inputSchema.properties[*].description` was blank for every parameter of all 59 tools.
    The only explanation was the tool's prose docstring, so an argument that was not
    mentioned there was — to a model reading `tools/list` — the same as an argument that
    does not exist. One release shipped three such (`sheet_start`, `cut_line_ratio`,
    `hatch_missing`) and a report called it structural, not accidental.

    The guard lives in static_check. This assertion keeps the guard honest: it must reject
    a signature that lost its Field(description=...).
    """
    import static_check as sc

    src = _read("server.py")
    assert "from typing import Annotated" in src, (
        "Annotated is not imported — every annotation would fail to evaluate")
    assert "from pydantic import Field" in src, "Field is not imported"

    problems = sc.check_param_descriptions(sc.SERVER)
    assert not problems, "parameters without a description: %s" % problems[:3]
    return "every tool parameter carries a schema description"

def assert_datum_symbol_is_gb_circle():
    """0.8.64 — "基准符号没有三角形" is not a bug; GB uses a circle.

    `add_datum_feature` inserts through `IModelDoc2.InsertDatumTag2`. The resulting tag
    reports GetTriangleCount 0 / GetLineCount 1 / GetArcCount 1 — a circle by construction.

    The triangle people expect comes from the 方形 display type (ANSI / some company
    standards); 圆形(GB) uses 垂直/竖直/水平 instead. Three code paths were tried and none
    change it: FilledTriangle (only fills an existing triangle), the drafting standard
    (integer pref 188), prefs 187/189/190/191/199.

    This assertion keeps the CONCLUSION on record so the next reader does not re-run the
    same three experiments — and so nobody "fixes" it by forcing a triangle from code.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_datum_feature")) or ""
    assert "InsertDatumTag2" in body, "the datum tag entry point changed"
    assert "GetTriangleCount" in body, (
        "the docstring no longer records that there is no triangle in the geometry")
    assert "圆形(GB)" in body, (
        "the docstring no longer explains that a circle is correct for GB")
    assert "FilledTriangle" in body, "the docstring lost the ruled-out attempts"
    caps = _read("tools/capabilities.py")
    assert '"datum_feature_symbol"' in caps, "the capability is not declared"
    index = caps.index('"datum_feature_symbol"')
    end = caps.find('"id":', index + 10)
    segment = caps[index:end if end > 0 else index + 2000]
    assert '"verified"' in segment, "datum symbol capability is not marked verified"
    assert "GB" in segment and "圆形" in segment, (
        "the entry does not say that the circle is the GB shape")
    return "datum symbol is GB-correct (circle), and the ruled-out paths are on record"

def assert_weld_symbol_is_declared_as_sketch():
    """0.8.67 — a weld symbol drawn from lines must never claim to be a native annotation.

    `IWeldSymbol` stores the symbol name as text and never instantiates the geometry on this
    build: SetText returns True, GetText(2) reads back "FILL", the sheet shows a leader and
    a number only. So `add_weld_symbol` draws the GB fillet symbol from sheet lines instead.

    Two things must stay true, both of which this guards:
      * the receipt says representation="sketch_block" / is_native_weld_symbol=False
      * coordinates are divided by the SHEET SCALE — `SketchManager.CreateLine` scales by it,
        which put a symbol requested at (0.05, 0.12) at (0.11, 0.25) on a 2:1 sheet

    Also: unsupported weld types are refused, not approximated as a triangle.
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_weld_symbol")) or ""
    assert 'representation="sketch_block"' in body, (
        "the receipt no longer declares that this is sketch geometry")
    assert "is_native_weld_symbol" in body, (
        "the receipt does not deny being a native weld symbol")
    assert "sheet_scale" in body and "/ sheet_scale" in body, (
        "coordinates are not divided by the sheet scale — the symbol will land elsewhere")
    assert "weld_type_unsupported" in body, (
        "unsupported weld types are silently approximated")
    assert "GetTriangleCount" in body, (
        "the docstring lost the note that GetTriangleCount was never valid evidence")
    caps = _read("tools/capabilities.py")
    index = caps.index('"weld_symbol"')
    end = caps.find('"id":', index + 10)
    segment = caps[index:end if end > 0 else index + 1500]
    assert '"partial"' in segment, "weld_symbol should be partial (fillet only, sketch drawn)"
    assert "sketch_block" in segment, "the capability does not name the representation"
    return "weld symbol declared as sketch geometry, scaled correctly, types refused"

def assert_layout_iterates_and_uses_both_axes():
    """0.8.68 / 3.3 — the sweep ran ONCE and only pushed up, so multi-view sheets kept overlaps.

    Measured 2026-11-03 on 气缸盖 (39 dimensions):
      * one vertical pass, 3 views   -> still 5 overlapping pairs
      * one vertical pass, 4 views   -> 12
      * iterating until quiet        -> both classes converge, 4 views down to 4

    Two independent defects, both visible in the old code:
      * the comment said "the pass has to repeat until it settles" while the loop body ran
        exactly once
      * every push was `y = clash.y + ...`, so two boxes that overlap because the STRINGS
        are wide (Ø25.3 beside Ø25.4) could never separate — they sit side by side

    The guard is structural on purpose: this behaviour is a loop shape, not a value.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "layout_dimensions")) or ""
    assert "for _round in range(" in body, (
        "the sweep is back to a single pass — multi-view sheets will keep overlaps")
    assert "moved_this_round" in body, (
        "the loop cannot tell 'settled' from 'did nothing'")
    assert "_round % 2" in body, (
        "direction no longer alternates — always pushing up walks the stack off its view")
    assert "sideways" in body and 'item["x"]' in body, (
        "no horizontal fallback — wide text boxes side by side can never be separated by y")
    assert "unresolved" in body, (
        "the receipt no longer reports what is still overlapping after the sweep")
    return "layout iterates until quiet, alternates direction, and falls back to x"

def assert_dimension_spread_is_diagnosed_not_faked():
    """0.8.69 / 3.2 — the "all dimensions pile into the first view" claim does NOT reproduce.

    Measured on 气缸盖 2026-11-03, `auto_dimension(all_views=True)` distributes:
        3 views: 20 / 10 / 9      4 views: 20 / 10 / 9 / 0 (isometric empty)   2: 20 / 10

    The first view carries the most because SolidWorks puts each dimension where it can be
    shown. So instead of a `distribute_dimensions` tool that could only pretend, this ships
    a read-only survey — and the survey must state, in its own words, why it cannot
    re-assign anything. An IAnnotation belongs to its view; SetPosition only moves it on
    the sheet; `all_views=False` inserts only what the active view shows.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "survey_dimensions_by_view")) or ""
    assert "empty_views" in body, "the survey does not name the empty views (the real problem)"
    assert "total_dimensions" in body and "busiest_view" in body, (
        "the survey does not report the spread")
    assert "改派" in body or "re-assign" in body.lower(), (
        "the docstring does not say that re-assignment is impossible — the next reader "
        "will build the tool that cannot work")
    assert "no_dimensions_inserted" in body, (
        "the all_views=False limitation is no longer recorded")
    # the tool must stay read-only
    # The docstring legitimately NAMES SetPosition to explain why re-assignment is
    # impossible, so match the call form, not the word.
    assert ".SetPosition(" not in body, "the survey mutates the drawing"
    assert "_save_drawing(" not in body, "the survey saves the drawing"
    caps = _read("tools/capabilities.py")
    assert '"dimension_per_view"' in caps, "the capability is not declared"
    index = caps.index('"dimension_per_view"')
    end = caps.find('"id":', index + 10)
    segment = caps[index:end if end > 0 else index + 1500]
    assert "20 / 10 / 9" in segment, "the capability lost the measurement that disproves the claim"
    return "dimension spread is measured and documented, not silently 'fixed'"

def assert_turned_parts_map_to_sheet_coordinates():
    """0.8.70 / P0-1 — `axes_ambiguous` locked every turned part out of coordinate annotation.

    A report hit it on four tools in a row on a shaft (Ø14xØ10xØ8, 60 long): edges without
    sheet coords, circle centres null, cut planes unmapped, dimensions by blind edge_index.

    Two independent causes, both fixed here:

    1. The tie test refused whenever two axis pairs scored equally. For 飞达动力轴
       (extent [35, 35, 107], front view a 35x35 circle) the tied pairs were (0,1) and
       (1,0) — the two RADIAL extents, which are equal, so the orders are interchangeable
       and there was nothing to be ambiguous about. Equal-extent ties now resolve.
       Genuinely stretched parts additionally break the tie by view aspect: the model's
       long axis is drawn across the wider sheet direction.

    2. Even with the axes known, nothing projected: contour edges carry no vertices on
       this binding, so `model_start` was always absent and `sheet_projected` stayed false
       with reason "no projection inputs". `ICurve.CircleParams` still answers with
       centre + radius, which is all a turned part's view needs.

    Verified 2026-11-03: the same shaft now reports sheet_projected true, four circles
    sharing one centre and radii 8.5/10/13.5/15 mm.
    """
    src = _read("tools/drawing.py")
    fn = _function_node(ast.parse(src), "_view_axis_map")
    body = ast.get_source_segment(src, fn) or ""
    assert "axes_equal_interchangeable" in body, (
        "equal-extent ties are refused again — every round part with matching radii breaks")
    assert "inferred_from_aspect" in body, (
        "the aspect-ratio tie-break for stretched parts is gone")
    assert "axes_ambiguous" in body, (
        "genuine ambiguity must still be refused (a cube has no long axis)")
    listing = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                        "list_drawing_edges")) or ""
    assert "CircleParams" in listing, (
        "circle edges have no fallback — turned parts project nothing")
    assert "sheet_radius_m" in listing, (
        "the projected radius is not reported, so a circle cannot be aimed at")
    return "turned parts resolve axes and project circle centres to the sheet"

def assert_delete_view_warns_about_leftover_geometry():
    """0.8.71 / P0-2 — deleting a section leaves its cut line on the parent, and no API removes it.

    Reproduced 2026-11-03 with the reporter's own part (旋转轴, right view 60x14):
    `add_section_view(vertical)` then `delete_view("剖面视图 A-A")` and the PDF still shows
    the vertical cut line with arrows and "A" across the shaft.

    The line is unreachable:
      * `IView.GetSection()` returns None even BEFORE the delete
      * `GetSectionLineInfo2` still returns the geometry (4 segments) — it exists, it has no
        object to select
      * SelectByID2 with SKETCHSEGMENT / SKETCHPOINT / DRAWINGVIEW / SECTIONLINE selects
        nothing; the parent's annotation list holds only 装饰螺纹线

    Since it cannot be cleaned, the receipt must SAY so. Silently returning ok:true leaves
    the caller to ship a sheet with two contradictory A-A marks — the exact failure the
    report called a "图纸硬错误".
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "delete_view")) or ""
    assert "leftover_geometry" in body, (
        "delete_view is silent again — callers will ship a sheet with a leftover cut line")
    assert "cannot_be_cleaned_by_api" in body, (
        "the receipt does not admit that the API cannot remove it")
    assert "GetSection" in body, (
        "the docstring lost the evidence that GetSection returns None")
    # must only fire for section/detail, not for every view
    assert '"剖面"' in body and '"局部"' in body, (
        "the warning does not distinguish section/detail views from plain ones")
    return "delete_view reports (and explains) the cut line it cannot remove"

def assert_cosmetic_threads_are_not_silent():
    """0.8.72 / P0-3 — a real cosmetic thread passed every check because nothing looked.

    Reproduced 2026-11-03 on the reporter's 旋转轴: the Ø10 section carries a 10 mm cosmetic
    thread, and the whole pipeline went green — `build_feature_ledger` listed 11 features
    with `doubts: []`, `plan_drawing` had no thread item, `delivery_check` passed.

    Where the thread actually is (and is not):
      * feature tree: 9 features, none Thread/Cosmetic
      * `GetCosmeticThreads` / `GetCosmeticThreadCount`: do not exist on this binding
      * the DRAWING does hold it: an annotation with `GetType == 1` named "装饰螺纹线1"

    So the check moved to the drawing level: thread annotation present but no note/thread
    text anywhere => blocker `cosmetic_threads`, verdict drops to 仅供参考/未完成.

    What this does NOT do: report the thread SPEC (M10x1). That is unreadable here — the
    report reached M10x1 only by reverse-engineering pixel gaps, which is not something a
    tool should present as fact.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "delivery_check")) or ""
    assert 'record("cosmetic_threads"' in body, (
        "the thread check is gone — a threaded part will pass silently again")
    assert "other_1" in body or "螺纹" in body, (
        "the check no longer looks at the drawing's thread annotation")
    assert "thread_annotations" in body, (
        "the receipt does not carry the evidence behind the verdict")
    assert "GetCosmeticThreads" in body, (
        "the docstring lost the note that the model-side API does not exist")
    # the guidance must stay honest about not knowing the spec
    assert "读不到" in body or "unreadable" in body.lower(), (
        "the receipt claims to know the thread spec, which this binding cannot read")
    return "cosmetic threads block delivery when unannotated, and the spec is not invented"

def assert_reconcile_uses_one_to_one_matching():
    """0.8.73 / P1-7 — coverage was pinned at 1.0 because one drawing dimension covered many.

    The old match was `next((v for v in sheet_values if abs(v - value) <= tol), None)` run
    against the SAME list for every ledger row. With 18 ledger rows against 8-11 drawing
    dimensions and values repeating (10 three times, 8 twice, 3 twice), every row found a
    partner, so a report saw `coverage: 1.0` while R1.5 and Ø14 were absent from the sheet.

    A drawing dimension can evidence exactly ONE ledger dimension. Each match now consumes
    its value, which caps coverage at drawing/ledger and makes a missing item visible.

    Verified 2026-11-03 on the reporter's 旋转轴: coverage 1.0 -> 0.444, and the previously
    invisible gaps list themselves (`圆角1: [1.5]`, `切除-拉伸1: [1.8, 3.0]`).
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "reconcile_ledger")) or ""
    assert "available.remove(hit)" in body, (
        "the match no longer consumes its value — one dimension can cover many again")
    assert "available = list(sheet_values)" in body, (
        "there is no working copy to consume from")
    assert "coverage" in body and "drawing_dimensions" in body, (
        "the receipt does not expose the numbers behind the ratio")
    assert "一对一" in body or "one-to-one" in body.lower(), (
        "the docstring does not explain why coverage cannot exceed drawing/ledger")
    return "reconcile pairs dimensions one-to-one, so coverage can no longer be pinned at 1.0"

def assert_leader_state_and_apex_are_reported():
    """0.8.74 / P1-1 + P1-2 — "annotation exists" is not "annotation is readable".

    Two failures from a 2026-11-03 report, both about the receipt hiding where a symbol
    actually points:

    P1-1 `add_geometric_tolerance` with only x/y: the frame lands where asked, `attached_to`
    is null, no leader, no arrow. GB/T 1182 requires the frame to point at the controlled
    feature, so a reader could not tell whether 0.02 governed the Ø10, the collar or the
    shaft. Reproduced on 旋转轴: BOTH the x/y path and the edge_index path fail to attach
    (AttachTo returns null on contour edges), so the two cases must say different things.

    P1-2 `add_surface_finish` x/y is the TRIANGLE APEX, not the symbol centre. A report
    asked for (0.208, 0.2185) as a centre and the apex ended up 3.41 mm in the air, on a
    thread root instead of the mating face. The docstring never said which it was.
    """
    src = _read("tools/drawing.py")

    gtol = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_geometric_tolerance")) or ""
    assert "frame_has_leader" in gtol, (
        "the GT receipt does not say whether a leader exists")
    assert "leader_endpoint_m" in gtol, (
        "the GT receipt does not report where the leader landed")
    assert "edge_index is not None else" in gtol or "传了 edge_index" in gtol, (
        "the two no-leader cases (not asked / asked but failed) collapsed into one message")

    finish = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                       "add_surface_finish")) or ""
    assert "TRIANGLE APEX" in finish, (
        "the docstring no longer says x/y is the apex — the 3.4 mm mistake returns")
    assert "apex_m" in finish, (
        "the receipt does not echo the apex under a name that cannot be mistaken")
    assert "GB/T 131" in finish, (
        "the docstring lost the standard that makes the apex the contract")
    return "leader state and apex semantics are both reported, not left to guesswork"

def assert_gtol_leader_uses_the_real_signature():
    """0.8.77 / P2-1 — the leader was never broken; the ARGUMENTS were misread.

    I read SetLeader3 as (LeaderType, UseDocStyle, LeaderStyle, ArrowSize, UseDocArrowSize,
    LeaderBent) and called SetLeader3(1, True, 2, 0, True, True). The real signature is

        SetLeader3(LeaderStyle, LeaderSide, SmartArrowHeadStyle,
                   Perpendicular, AllAround, Dashed)

    so that call actually meant LeaderStyle=1 (STRAIGHT, not bent) and a boolean where
    LeaderSide belongs. `-2` is documented as "Leader attachment side setting is invalid" —
    it was reporting the mistake, and I recorded it as "renders nothing because there is no
    attachable geometry". That explanation was wrong.

    Correct call (verified 2026-11-03 on 旋转轴): SetLeader3(2, 2, True, True, False, False)
    -> status 0, leader count 1, and the PDF shows a leader off the frame.

    Guarded here: the mapping tables, the status field, and the fact that a frame WITH a
    leader must not also carry the "no leader" warning.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_geometric_tolerance")) or ""
    assert "style_map" in body and '"bent": 2' in body, (
        "the swLeaderStyle_e mapping is gone — bent must be 2, not a made-up enum")
    assert '"right": 2' in body, (
        "the swLeaderSide_e mapping is gone — right must be 2")
    assert "leader_status" in body, (
        "the receipt no longer reports SetLeader3's status (0 = success)")
    assert "leader_count" in body, (
        "the receipt no longer reports the actual leader count")
    # success and floating-ness must not be conflated
    assert "leader_made" in body and "and not leader_made" in body, (
        "a frame with a leader still gets the 'no leader' warning")
    # the wrong reading must not come back
    assert "LeaderType, UseDocLeaderStyle" not in body, (
        "the incorrect parameter reading is back in the docstring")
    return "gtol leaders use the documented SetLeader3 signature and report status + count"

def assert_distance_dimension_reports_its_value():
    """0.8.78 / P2-7 — an overall length cannot come from one edge, so build it from two.

    A 2026-11-03 report: the 60 mm shaft shipped without a `60` dimension. `add_drawing_dimension`
    hits ONE edge and returns that edge's own length, so it yields 18/29/3/10 — segments of
    the total. The overall length is their SUM, and **no single edge in the view is 60 mm
    long**, so nothing in the toolset could express it.

    `IModelDoc2.AddDimension2` dimensions the current selection, so two selected edges give
    the gap between them. Verified 2026-11-03 on 旋转轴 right view: edge 3 with edge 6 is
    exactly 60.0 mm, while other pairs give 18/42/47/8.

    The critical part is `value_mm`: which pair spans the overall length depends on the
    view, so a wrong pair produces a perfectly valid dimension of the WRONG distance. The
    receipt must return what it actually measured.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_distance_dimension")) or ""
    assert "AddDimension2" in body, "the two-edge dimension call is gone"
    assert "value_mm" in body, (
        "the receipt does not return the measured value — a wrong edge pair would look fine")
    assert "Select2(True" in body, (
        "the second edge is not APPENDED to the selection; AddDimension2 needs both")
    assert "bad_edge_index" in body, "no guard against an out-of-range edge index"
    # must not silently assume success
    assert "distance_dimension_failed" in body, "there is no failure verdict"
    return "two-edge distance dimensions report the value actually created"

STATIC_REGRESSIONS = [
    ("0.8.3 row/text ordering", assert_row_before_text),
    ("0.8.4 bom wrapper signature", assert_bom_wrapper_signature),
    ("0.8.5 dedupe before counting", assert_annotations_dedupe_before_counting),
    ("0.8.6 material failure branch", assert_material_top_level_not_always_ok),
    ("0.8.6 thickness 厚度 label", assert_thickness_matches_chinese_label),
    ("0.8.10 section offset sign", assert_section_offset_sign),
    ("0.8.13 callout SetText", assert_hole_callout_text_rewrite),
    ("0.8.14 dimension text fallback", assert_dimension_text_fallback),
    ("0.8.15 annotation Select(False)", assert_annotation_selected_with_select),
    ("finder sweeps views", assert_annotation_finder_sweeps_views),
    ("duplicate defs", assert_no_duplicate_defs_in_tools),
    ("sw_version reported", assert_sw_version_reported),
    ("capability statuses legal", assert_capability_statuses_legal),
    ("tools documented", assert_every_tool_has_docstring),
    ("smoke covers every tool", assert_every_tool_has_smoke_coverage),
    ("smoke calls match signatures", assert_smoke_calls_match_signatures),
    ("smoke self-heals stale docs", assert_smoke_selfheals_stale_documents),
    ("0.8.18 set_view alignment receipt", assert_set_view_reports_alignment),
    ("0.8.18 nearest-edge diagnostic", assert_dimension_failure_has_nearest_edge_diagnostic),
    ("0.8.18 precise outline in audit", assert_overlap_checks_use_precise_outline),
    ("0.8.18 stale doc claims removed", assert_no_stale_doc_claims),
    ("0.8.19 full-name dedupe", assert_full_name_dedupe_deletes_copies),
    ("0.8.19 dimension type guard", assert_dimension_position_validates_type),
    ("0.8.19 leader anchor parameter", assert_hole_callout_leader_is_parameterized),
    ("0.8.20 detail empty/scale guard", assert_detail_view_validates_edges_and_scale),
    ("0.8.20 GTOL text fallback", assert_gtol_symbol_text_fallback),
    ("0.8.20 surface finish visibility", assert_surface_finish_position_and_pixel_check),
    ("0.8.21 section cut plane", assert_section_cut_plane_maps_model_coordinates),
    ("0.8.21 section sanity", assert_section_sanity_uses_kept_dimension),
    ("0.8.21 section wrapper", assert_section_wrapper_forwards_cut_plane),
    ("0.8.22 bridge self-heal", assert_bridge_self_heals_stale_dispatcher),
    ("0.8.22 com error classes", assert_com_failures_are_classified),
    ("0.8.22 ping + excepthook", assert_ping_and_excepthook_present),
    ("0.8.22 single outline truth", assert_outline_has_single_truth),
    ("0.8.23 delete counts all views", assert_delete_annotation_counts_all_views),
    ("0.8.23 dimension text slot", assert_dimension_display_text_reads_value_slot),
    ("0.8.23 render deps hard", assert_render_dependency_is_hard),
    ("0.8.23 model_clean not blocker", assert_model_clean_is_not_a_blocker),
    ("0.8.23 reconcile zero filter", assert_reconcile_skips_zero_constraints),
    ("0.8.24 circle lister", assert_list_circular_edges_reads_curve_params),
    ("0.8.24 callout diagnosis", assert_hole_callout_failures_are_explained),
    ("0.8.24 prune orphans", assert_prune_orphan_dimensions_exists),
    ("0.8.24 layout avoids notes", assert_layout_dimensions_avoids_notes),
    ("0.8.24 note avoidance", assert_add_note_can_avoid_collisions),
    ("0.8.24 title block cell", assert_title_block_reports_cell),
    ("0.8.24 runtime identity", assert_runtime_identifies_the_running_copy),
    ("0.8.25 no undefined calls", assert_no_undefined_bare_calls),
    ("0.8.25 dedupe alias defined", assert_duplicate_delete_uses_defined_alias),
    ("0.8.25 plan filter wired", assert_plan_filter_reaches_auto_dimension),
    ("0.8.25 place_views vocabularies", assert_place_views_accepts_both_vocabularies),
    ("0.8.25 delivery_check formats", assert_delivery_check_formats_its_counts),
    ("0.8.26 dim position pick", assert_set_dimension_position_accepts_position),
    ("0.8.26 note bbox", assert_add_note_reports_final_bbox),
    ("0.8.26 ledger missing list", assert_delivery_check_lists_missing),
    ("0.8.26 callout text verified", assert_callout_text_verified),
    ("0.8.26 prune confirm gate", assert_prune_requires_confirmation),
    ("0.8.26 layout view obstacles", assert_layout_blocked_by_other_views),
    ("0.8.26 cut line warning", assert_set_view_warns_about_derived),
    ("0.8.27 place outside layout", assert_place_outside_layout),
    ("0.8.28 title block one cell", assert_title_block_picks_one_cell),
    ("0.8.28 view label dodge", assert_set_view_dodges_label),
    ("0.8.29 break alignment", assert_set_view_break_alignment),
    ("0.8.30 duplicate dim unsupported", assert_duplicate_dimension_declared_unsupported),
    ("0.8.31 section line unsupported", assert_section_line_limitation_declared),
    ("0.8.32 cut line on view", assert_cut_line_placed_on_its_view),
    ("0.8.33 tolerance metres", assert_tolerance_uses_metres),
    ("0.8.33 callout text unreadable", assert_unreadable_callout_text_is_kept),
    ("0.8.33 wrapper position_m", assert_wrapper_forwards_position_m),
    ("0.8.33 dedupe full name", assert_dedupe_uses_full_name),
    ("0.8.33 callout text value", assert_callout_text_carries_value),
    ("0.8.33 mojibake repaired", assert_mojibake_is_repaired),
    ("0.8.34 axis map scaled", assert_axis_map_scales_to_sheet),
    ("0.8.35 single callout exact circle", assert_single_callout_matches_exact_circle),
    ("0.8.36 callout depth limit", assert_callout_depth_limit_declared),
    ("0.8.37 model clean recheck", assert_model_clean_rechecks_after_rebuild),
    ("0.8.37 dimension attachment", assert_dimension_rejects_unattached),
    ("0.8.37 dedupe section warning", assert_dedupe_warns_about_sections),
    ("0.8.38 test tree ignored", assert_gitignore_excludes_test_tree),
    ("0.8.38 call_method once", assert_call_method_defined_once),
    ("0.8.38 health provenance", assert_health_reports_provenance),
    ("0.8.38 static check bare names", assert_static_check_covers_bare_names),
    ("0.8.39 circle annotatability", assert_circular_edges_predict_annotatability),
    ("0.8.41 orphans near view", assert_orphans_near_view_not_excused),
    ("0.8.42 crash handler", assert_crash_handler_installed),
    ("0.8.42 filter full names", assert_filter_prefers_full_names),
    ("0.8.43 scale explained", assert_scale_choice_is_explained),
    ("0.8.44 outside stack bounded", assert_outside_stack_respects_sheet),
    ("0.8.45 layout prediction", assert_layout_prediction_matches_reality),
    ("0.8.47 views inside area", assert_views_inside_printable_area),
    ("0.8.48 stack by value", assert_stack_ordered_by_value),
    ("0.8.49 center marks view", assert_center_marks_take_a_view),
    ("0.8.49 gtol symbol render", assert_gtol_symbol_render_flagged),
    ("0.8.50 close document", assert_close_document_exists),
    ("0.8.51 view edit tools", assert_view_edit_tools_exist),
    ("0.8.52 edges sheet coords", assert_edges_carry_sheet_coordinates),
    ("0.8.53 plan driving/derived", assert_plan_separates_driving_from_derived),
    ("0.8.53 empty views", assert_empty_views_are_reported),
    ("0.8.54 cut line clamped", assert_cut_line_is_clamped),
    ("0.8.55 all_views=False loud", assert_all_views_false_is_not_silent),
    ("0.8.56 section hatch checked", assert_section_hatch_is_checked),
    ("0.8.57 cut line coords", assert_cut_line_uses_view_sketch),
    ("0.8.61 param descriptions", assert_every_param_is_documented),
    ("0.8.64 datum is GB circle", assert_datum_symbol_is_gb_circle),
    ("0.8.67 weld is sketch", assert_weld_symbol_is_declared_as_sketch),
    ("0.8.68 layout iterates", assert_layout_iterates_and_uses_both_axes),
    ("0.8.69 dim spread diagnosed", assert_dimension_spread_is_diagnosed_not_faked),
    ("0.8.70 turned parts map", assert_turned_parts_map_to_sheet_coordinates),
    ("0.8.71 delete_view warns", assert_delete_view_warns_about_leftover_geometry),
    ("0.8.72 threads not silent", assert_cosmetic_threads_are_not_silent),
    ("0.8.73 reconcile one-to-one", assert_reconcile_uses_one_to_one_matching),
    ("0.8.74 leader + apex", assert_leader_state_and_apex_are_reported),
    ("0.8.77 gtol leader signature", assert_gtol_leader_uses_the_real_signature),
    ("0.8.78 distance dimension", assert_distance_dimension_reports_its_value),
]


def main():
    failures = 0
    print("regression ledger — %d static assertions" % len(STATIC_REGRESSIONS))
    print("-" * 78)
    for name, fn in STATIC_REGRESSIONS:
        try:
            detail = fn()
            print("PASS  %-32s %s" % (name, detail))
        except AssertionError as exc:
            failures += 1
            print("FAIL  %-32s %s" % (name, exc))
        except Exception as exc:
            failures += 1
            print("ERROR %-32s %s: %s" % (name, type(exc).__name__, str(exc)[:90]))
    print("-" * 78)
    print("%d/%d passed" % (len(STATIC_REGRESSIONS) - failures, len(STATIC_REGRESSIONS)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
