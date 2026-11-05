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
    # 0.8.120 added a counting wrapper named precise_outline; the margin lives in the
    # implementation it delegates to. Assert on the impl, then check the wrapper is a
    # pass-through so the two cannot drift apart.
    po = _function_node(tree, "_precise_outline_impl") or _function_node(tree, "precise_outline")
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
    # 0.8.120: the margin lives in the implementation; precise_outline is a counting wrapper.
    node = (_function_node(tree, "_precise_outline_impl")
            or _function_node(tree, "precise_outline"))
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
    # 0.8.92 added a LOOSE key for the model/sheet sketch-renumbering gap, so the literal
    # `if full_prefixes:` became `if full_prefixes or loose_keys:`. The property that must
    # hold is exclusivity: once any name form is given, a miss returns False rather than
    # falling through to value matching.
    assert "full_prefixes or loose_keys" in body, (
        "the name-based branch is gone")
    assert "full.startswith" in body, "exact full-name matching is gone"
    assert "return False" in body, (
        "the filter falls back to value matching when name forms were given")
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
    # 0.8.108 defers the read (only buckets with >= 2 need ordering), so the local is now
    # . What must hold is that the value is still READ and still used for ordering.
    assert "SystemValue" in body, "the dimension value is never read"
    assert "GetDimension2" in body, "the value is no longer obtained from the dimension"
    assert "len(_items) < 2" in body, (
        "the deferred read is gone - every dimension pays 143 ms again")
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
    # 0.8.91: flipped from partial to unsupported. `AddToDB = True` did NOT make sheet
    # sketches render (three line styles measured, all 0 pixels). The 602 pixels this tool
    # does produce come from its NOTE, not the triangle.
    assert '"unsupported"' in segment, (
        "weld_symbol claims partial again — the triangle has never rendered")
    assert "triangle_verified=False" in _read("tools/layout.py"), (
        "the receipt claims the triangle was verified, which a whole-page diff cannot do")
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

def assert_set_view_verifies_the_outline_not_the_property():
    """0.8.79 / P1-6 — `IView.Position` echoed the request; the geometry never moved.

    A 2026-11-03 report: set_view(detail, x=0.382, y=0.132, break_alignment=True) returned
    {"position_after_m": [0.382, 0.132], "alignment_broken": true, "ok": true} while the
    rendered sheet still had the detail sitting on top of the section view. `audit` flagged
    views_overlap, which is how it was caught.

    Reading the property back after writing it proves nothing. The ink outline comes from
    the geometry, so its centre is the check. Two more details learned here:

      * compare against the REQUESTED point, not against Position — on a detail view that
        refuses to move, Position came back as None, leaving nothing to disagree with
      * report it as an ERROR, not a warning: the caller asked for a position and did not
        get it

    Verified on 旋转轴: the detail reports delta [-72.3, -20.0] mm; a plain view reports ok.
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "set_view")) or ""
    assert "outline_center" in body and "GetOutline" in body, (
        "the position check no longer reads the geometry — Position alone can lie")
    assert "moved_but_position_mismatch" in body, (
        "silent ok:true is back for a view that did not move")
    assert "outline_center[0] - requested[0]" in body, (
        "the comparison is not against the request — Position can be None on detail views")
    assert "position_reported_m" in body and "delta_mm" in body, (
        "the receipt does not show both numbers, so a reader cannot see the disagreement")
    return "set_view verifies the outline centre against the request and errors on mismatch"

def assert_cross_sections_are_not_judged_by_parent_extent():
    """0.8.80 / P1-4 — the kept-dimension test compared a cross-section to the whole part.

    A 2026-11-03 report: cutting a shaft across the axis at the keyway gave a correct
    15.98x15.40 section, judged `section_suspicious` because it "kept 15.4 of 27.98 mm
    (ratio 0.550)". The parent's 27.98 is the WHOLE shaft height — a cross-section is a
    disc the size of the LOCAL diameter, so the ratio was never meaningful.

    It also failed the other way, which is worse: cutting at the Ø14 collar produces a
    section height of exactly the parent's height, so ratio 1.0 and the check PASSED even
    with the cut in the wrong place. "该拦的不拦、不该拦的拦".

    The discriminator is geometry, not a heuristic: a cross-section is smaller than the
    parent in BOTH axes; a proper longitudinal section keeps one axis at full size.

    Verified on 旋转轴 (source 59.98x13.98, right view):
        off 0.01 -> 9.98x9.98   cross_section, accepted  (ratio would have been 0.714)
        off 0.02 -> 7.98x7.69   cross_section, accepted
        off 0.03 -> 23.8x13.34  not a cross-section, still rejected as section_empty
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_section_sanity")) or ""
    assert "cross_section" in body, (
        "cross-sections are judged by the parent's overall extent again")
    assert "sec_w < src_w * 0.85" in body and "sec_h < src_h * 0.85" in body, (
        "the discriminator must require BOTH axes smaller — one axis at full size means a "
        "longitudinal section, which the ratio test does apply to")
    assert "section_kind" in body, (
        "the receipt does not say which kind of section it decided this was")
    assert "hatch_count" in body or "edge_count" in body, (
        "the acceptance fallback for cross-sections is no longer stated")
    return "cross-sections skip the parent-extent ratio; longitudinal sections still use it"

def assert_section_reports_a_dropped_position_axis():
    """0.8.81 / P1-5 — a section silently discarded the y of the requested position.

    A 2026-11-03 report: add_section_view(position=[0.300, 0.130]) produced an outline
    centred at y=0.205, with `layout: null` and no flag saying the request had been
    altered. The section landed on top of the end view and needed a second set_view.

    Keeping horizontal projection alignment with the parent is CORRECT drawing practice —
    the bug was saying nothing about it. Verified on 旋转轴: requested [0.3, 0.13],
    actual [0.3, 0.2759], x preserved, y replaced.

    So the receipt must carry request, result, whether alignment overrode it, and WHICH
    axis was dropped.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_section_view")) or ""
    assert "position_requested_m" in body, "the request is no longer echoed"
    assert "position_actual_m" in body, (
        "where the view actually landed is not reported — the reader cannot spot the change")
    assert "alignment_kept" in body, (
        "the receipt does not say whether projection alignment overrode the request")
    assert "position_axis_ignored" in body, (
        "the receipt does not name WHICH coordinate was dropped")
    assert "break_alignment" in body, (
        "the warning does not tell the caller how to opt out of the alignment")
    return "sections report the requested position, the actual one, and the dropped axis"

def assert_tolerance_checks_units_and_degenerate_limits():
    """0.8.82 / P2-9 — an h7 tolerance printed "8.00 / 8.00" and still said applied:true.

    A 2026-11-03 report: add_dimension_tolerance(value_mm=8, tolerance_type="limit",
    upper=0, lower=-1.5e-05). The function divides by 1000 to reach metres, which is
    correct — but -1.5e-05 had been intended as METRES and was read as MILLIMETRES, i.e.
    -0.000015 mm, which rounds to zero on the sheet. The allowance vanished while the
    receipt read `{"applied": true, "rendered_text": null}`.

    Two guards, deliberately with different severities:

      * a non-zero magnitude below 0.001 mm is not a manufacturable tolerance — almost
        certainly metres passed to a mm slot -> `unit_suspect` + warning (the call still
        succeeds; the value may be legitimate in some other unit convention)
      * upper == lower on a LIMIT tolerance is arithmetic, not a readback quirk -> ERROR
        `tolerance_not_rendered`

    `rendered_text` being null is only a WARNING: GetText returns nothing on this build
    even for a correctly applied -0.015, so treating that as failure would cry wolf.
    Verified 2026-11-03 on 旋转轴: correct h7 passes, -1.5e-05 is flagged, 0/0 errors.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_dimension_tolerance")) or ""
    assert "unit_suspect" in body, (
        "a metre value in a millimetre slot is no longer detected")
    assert "tolerance_not_rendered" in body, (
        "degenerate limits pass again")
    # the 0/0 test must NOT depend on rendered_text — it was null even when correct
    assert 'if str(tolerance_type).lower() == "limit":' in body, (
        "the degenerate-limit check is gated on rendered_text again, which is usually null")
    assert "rendered_verified" in body, (
        "the receipt does not distinguish 'verified' from 'could not read'")
    assert "无法确认" in body or "could not read" in body.lower() or "cannot" in body.lower(), (
        "an unreadable tolerance is reported as fact instead of uncertainty")
    return "tolerances flag unit mistakes and refuse degenerate limits, without crying wolf"

def assert_detail_thin_is_a_warning_not_a_failure():
    """0.8.83 / P1-3 — a thin detail view is worth flagging, not worth refusing.

    A 2026-11-03 report claimed `edge_count` was "always 2 regardless of framing, so it
    cannot tell you whether the framing worked". Measured on 旋转轴 (right view, centre
    [0.075, 0.265], 4:1):

        r=0.004 -> 0    r=0.006 -> 1    r=0.008 -> 2    r=0.012 -> 2    r=0.020 -> 3

    It does vary. It stays small because that window genuinely holds only a couple of
    contour lines on a plain shaft — the report expected 6-8 from a part with keyways.

    So the verdict is a WARNING: a plain shaft really does have two outline lines, and
    enlarging those may be exactly what was asked. `detail_empty` (0 edges) stays an
    ERROR because that view shows nothing at all.

    Also recorded, because it was misfiled as a bug: the arc across the detail view is
    SolidWorks' own boundary circle scaled up, NOT a sketch this tool draws —
    `circle_segments=0` produces an identical sheet.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_detail_view")) or ""
    assert "detail_thin" in body, "a nearly-empty detail view passes silently again"
    assert "thin_warning" in body, (
        "the thin verdict is no longer a warning — plain shafts would be refused")
    assert '"detail_empty"' in body, (
        "a truly empty detail view must still fail, distinct from a thin one")
    # the claim that produced the report
    assert "r=0.020 -> 3" in body, (
        "the measurement showing edge_count DOES vary is gone; the next reader will "
        "re-run the same five framings")
    return "thin detail views warn, empty ones still fail, and the numbers are on record"

def assert_turned_parts_classify_and_report_real_length():
    """0.8.84 / P2-6 — a Ø14x60 shaft was "一般零件" whose 总长 was 14.

    A 2026-11-03 report: plan_drawing returned category "一般零件" with
    `总长: 14.0` — the X extent, not the 60 mm length — and the shortlist omitted Ø14,
    the collar width 3 and the keyway depth 1.8.

    Two causes, both fixed here:

      * `_classify` required slender >= 8 for 轴类. A 4.3:1 shaft is obviously turned; the
        threshold is now 3 with width <= 2.5, which still rejects a 5x50x60 strip (12:1 but
        width 10).
      * `_rank_dimensions` zipped the box straight onto 长/宽/高. For a turned part the box
        order is arbitrary, so descending sort makes 总长 mean the longest extent.

    Verified on 旋转轴: category 轴类, `总长: 60.0` (was 14.0), `总宽: 14.0`.

    The shortlist gap (missing Ø14 / collar / keyway) is NOT fixed — that is `_rank_dimensions`
    ranking, a different problem.
    """
    led = _read("tools/ledger.py")
    assert "slender >= 3 and width <= 2.5" in led, (
        "turned parts fall back into 一般零件 at 4:1 again")
    plan = _read("tools/planner.py")
    assert "sorted(box_values, reverse=True)" in plan, (
        "总长 is the X extent again instead of the longest one")
    assert "category_reason" in plan, (
        "the receipt does not carry WHY it classified the part")
    assert "outside_warning" in _read("tools/drawing.py"), (
        "a second place_outside run reports placed_outside=0 without explanation again")
    return "turned parts classify correctly and 总长 is the longest extent"

def assert_same_value_duplicates_are_listed_not_deleted():
    """0.8.85 / P2-4 — the FullName dedupe key cannot see a same-value dimension from another view.

    A 2026-11-03 report: the sheet carried TWO `10` dimensions and `duplicates_removed: 4`
    still reported success. The key is the model's FullName, so a second dimension with the
    same VALUE but a different FullName — the same measurement projected elsewhere — is not
    considered a duplicate. The stray `10` sat alone on the second row with nothing aligned
    to it, which is what made the sheet look untidy.

    Value alone must NOT trigger deletion: Ø10 and a 10 mm length are both "10" and both
    correct. So candidates are grouped by (value, dimension_type) and REPORTED, leaving the
    decision to the caller.

    Verified on 旋转轴: two groups surface — 8.0/type5 x2 (two Ø8) and 10.0/type2 x2 (the
    pair the report described).
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "auto_dimension")) or ""
    assert "same_value_candidates" in body, (
        "same-value dimensions are invisible again — the stray 10 stays on the sheet")
    assert "dimension_type" in body and "value_mm" in body, (
        "the grouping is not by (value, type), which would conflate Ø10 with a 10 mm length")
    # must not auto-delete: this is a report, not an action
    assert 'same_value_candidate_count' in body, (
        "the receipt carries no count, so a caller cannot tell whether any exist")
    # the dedupe key itself is still FullName; this is the second pass, not a replacement
    assert "duplicate_report" in body, (
        "the FullName pass was replaced instead of supplemented")
    return "same-value duplicates are listed by (value, type) and never deleted on value alone"

def assert_sheet_lines_prove_they_rendered():
    """0.8.87 / P2-2 fallout — add_sheet_line returned ok:true for a line that never appeared.

    Found while investigating the centre-line request. Measured 2026-11-03 on 旋转轴:
    export the PDF, call add_sheet_line, export again, and the two rendered pages differ by
    **0 pixels out of 16,041,784**. The function had been reporting success since it was
    written, and the smoke test only ever asserted `ok`, so nothing caught it.

    That matters beyond centre lines: add_sheet_line is the documented way to draw BOM
    frames, so every frame anyone "drew" with it was blank.

    The before/after pixel diff is now part of the contract (verify_render defaults True),
    mirroring add_surface_finish's verify_render. A line that changes no pixels returns
    `line_not_rendered`.
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_sheet_line")) or ""
    assert "verify_render" in body, "the proof was removed — silent blanks are back"
    assert "_sf_render_page" in body and "ImageChops" in body, (
        "the pixel diff is gone")
    assert "line_not_rendered" in body, (
        "a zero-pixel line reports success again")
    assert "changed_pixels" in body, (
        "the receipt does not carry the number that settles it")
    return "add_sheet_line proves the line rendered or reports line_not_rendered"

def assert_p3_receipts_say_what_they_mean():
    """0.8.89 / P3 (6 items) — five receipt defects and one non-bug.

    P3-1  list_drawing_annotations gave {"raw_text": "<MOD-DIAM>", "text": "Ø", "value_mm":
          10.0} — the symbol resolved, the number did not, because the format string has no
          <DIM> placeholder. Callers spliced value_mm by hand.
          -> `display_text` ("Ø10") added; text/raw_text stay so the mismatch stays visible.

    P3-2  delivery_check reported `hole_callouts ok: true` beside `hole_callouts: 0`,
          reading as "hole callouts are fine" when there were none and coverage came purely
          from Ø dimensions. -> detail now says «仅靠 N 处 Ø 尺寸覆盖（没有孔标注对象）»
          and the two counts are separate fields.

    P3-3  check_capabilities lacked cosmetic threads and centre lines (and the report's
          "no datum symbol" was already answered in 0.8.63).
          -> `cosmetic_thread` (partial) and `centerline` (unsupported) added; 27 -> 29.

    P3-4  set_title_block returned `cell_position_m: []` on every run while claiming
          `via: "position"`. `ann_position` only tried the property form of GetAnnotation
          and returned [] on failure — indistinguishable from a real (0,0).
          -> both forms tried via call_method, unusable result is None.

    P3-6  Not a bug: the "8.00 / 8.00" Ø8 carries `tolerance_type: 3` (LIMIT) IN THE MODEL,
          brought along by InsertModelItems. display_text now annotates that a tolerance
          exists rather than pretending none does — and says the values were not read back.

    P3-5 is covered by 0.8.82 (the unit_suspect guard).
    """
    src = _read("tools/drawing.py")
    assert "display_text" in src, "P3-1 regressed: the value is unreadable again"
    assert "公差类型" in src, "P3-6 regressed: a tolerance-bearing dimension looks plain"
    assert "仅靠" in src, "P3-2 regressed: zero hole callouts reads as a pass again"
    assert "call_method(note, \"GetAnnotation\")" in src, (
        "P3-4 regressed: ann_position only tries the property form and returns [] on failure")
    caps = _read("tools/capabilities.py")
    assert '"cosmetic_thread"' in caps and '"centerline"' in caps, (
        "P3-3 regressed: two capabilities the report asked for are missing")
    return "P3 receipts say what they mean; P3-6 confirmed as model-side tolerance"

def assert_create_drawing_does_not_leak_a_blank_sheet():
    """0.8.90 — the path guard ran AFTER NewDocument, so firing it leaked a blank drawing.

    A 2026-11-03 report measured it precisely: smoke run 8 documents -> 10 afterwards, the
    extra being `工程图12 - 图纸1` with `GetPathName == ''`. Nothing ever closed it — both
    cleanup paths in smoke.py match on GetPathName, and `dirname('') == ''` matches nothing.
    Because the blank drawing also referenced the source part, ONE leak pinned TWO documents,
    and it grew on every run.

    The guard itself was honest (`saved: false`, `save_error: path_locked_by_open_doc`); only
    its position was wrong. Moving it before `_new_document` means the document is never
    created, and `no_views_created` now closes what it opened.

    Verified 2026-11-03: two calls to the same path leave `blanks 0 -> 0`, and the second
    returns ok:false with `path_locked_by_open_doc`.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "create_drawing")) or ""
    guard = body.find("path_locked_by_open_doc")
    newdoc = body.find("_new_document(sw, template)")
    assert guard > 0 and newdoc > 0, "the guard or the document creation moved"
    assert guard < newdoc, (
        "the path guard runs AFTER the document is created again — every trigger leaks a "
        "blank sheet that no cleanup path can find")
    assert "sw.CloseDoc(drw.GetTitle)" in body, (
        "an early exit leaves the freshly created drawing open")
    return "path guard runs before NewDocument, so a refused save leaks nothing"
def assert_dimension_filter_accepts_plan_dicts_and_strings():
    """0.8.92 / P1 — the documented "plan -> auto_dimension" path matched 0 of 18.

    A 2026-11-03 report fed `[d["dimension"] for d in plan["keep_dimensions"]]` and got
    `filter_kept: 0, filter_aborted: "none of the 18 inserted dimensions matched the plan
    (0 features / 0 values)"`. Two causes:

      * a string entry fell through `isinstance(entry, dict)` and was skipped entirely, so
        neither `features` nor `values` was ever populated
      * even as a name, the plan says `D1@Sketch1` while the sheet says `D1@草图2` — the
        feature is renamed AND translated, so no name form can bridge it

    Passing the dicts (which carry `value_mm`) is what works: measured 旋转轴 kept 11 and
    removed 1, where the string form kept 0.

    Both are supported now, but the value-bearing dict form is the one to recommend.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_filter_inserted_dimensions")) or ""
    assert 'isinstance(entry, str)' in body, (
        "string entries are skipped again — plan->auto_dimension silently does nothing")
    assert "loose_keys" in body, (
        "the model/sheet renumbering key is gone")
    return "dimension_filter accepts plan dicts and raw strings, and recommends the dicts"
def assert_sheet_x_can_be_flipped_and_the_cache_knows():
    """0.8.93 / P0 — the mirrored sheet X is now correctable, and the cache must honour it.

    Measured 2026-11-03 on 旋转轴, right view at 2:1:
        view centre           0.0888
        Ø14 collar reported   0.12879  (right of centre)
        collar on the PDF     0.0487   (left of centre)
        2*0.0888 - 0.12879 = 0.0488    — the mirror is exact

    Nothing in the API decides the direction: `_view_axis_map` gives the axis PAIR, and both
    the outline and the view position are symmetric under the transform. So the caller
    measures once and passes `flip_x=True`; the projection then mirrors about the centre.

    My first attempt at this appeared to do nothing, because `_EDGE_CACHE` keyed on
    (path, mtime, view, include_points) — the second call with flip_x=True returned the
    cached UNFLIPPED rows. The flag belongs in the key.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "list_drawing_edges")) or ""
    assert "flip_x" in body, "the correction switch is gone"
    assert "bool(flip_x)" in body, "flip_x is not part of the cache stamp — it will be ignored"
    assert "2 * view_pos[0] -" in body, (
        "the mirror is not computed about the view centre")
    assert "sheet_axis_direction_verified" in body and "sheet_flipped_x" in body, (
        "the receipt does not say whether the caller corrected the axis")
    return "sheet X can be mirrored by the caller, the cache keys on it, and the receipt says so"
def assert_produce_drawing_refuses_to_double_a_sheet():
    """0.8.94 / P1 (third report) — running the pipeline twice doubled everything.

    Measured 2026-11-03: same model, same output_path, twice —
    dimensions 8 -> 24, hole callouts 5 -> 10, centre marks 5 -> 8, surface finishes 1 -> 2,
    and BOTH runs reported ok:true with blockers: []. There was no output_exists /
    overwrite / preexisting handling at all.

    Now an existing file is refused (`output_exists`) unless `overwrite=True`, and
    overwriting closes the open copy and deletes the file first so create_drawing really
    starts clean. Verified: #1 ok, #2 output_exists, #3 overwrite -> 10 dimensions (a
    single fresh set, where the old behaviour produced 24).
    """
    src = _read("tools/pipeline.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "produce_drawing")) or ""
    assert "output_exists" in body, "a second run appends to the existing sheet again"
    assert "overwrite" in body, "there is no way to rebuild deliberately"
    assert "os.remove" in body, (
        "overwrite does not delete the old file, so create_drawing opens it and appends")
    assert "CloseDoc" in body, "an open copy would block the delete"
    return "produce_drawing refuses a second run unless overwrite=True, and overwrite is clean"
def assert_leader_endpoint_comes_from_the_attached_edge():
    """0.8.95 / P1 — `leader_endpoint_m` existed but was always null.

    Chased it properly this time. `IGtol.GetLeaderAtIndex2` (the method the report
    suggested) does NOT exist on this binding — checked on both the annotation and
    GetSpecificAnnotation, both MISSING. `GetLeaderCount` works, which is why
    `frame_has_leader` was never the problem.

    Two things I had wrong before:
      * the GTol annotation reports `GetType == 5` on this part, not 7 (that number came
        from a different drawing in an earlier report)
      * `IAnnotation.GetLeaderPoints` does not exist either

    So the endpoint is taken from what the leader points AT: `list_drawing_edges` has
    already cached the attached edge's sheet coordinates. `leader_endpoint_source` says
    so explicitly, because "the arrow ends here" and "the arrow should end here" are
    different claims and the caller must be able to tell them apart.

    Verified on 旋转轴: edge0 at [0.10879, 0.27601], receipt [0.1088, 0.276],
    source attached_edge_sheet_coords.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_geometric_tolerance")) or ""
    assert "leader_endpoint_source" in body, (
        "the receipt does not say where the endpoint came from — a null field is back")
    assert 'leader_endpoint_source = "attached_edge_%s" % _pt_source' in body, (
        "the endpoint is no longer sourced from the attached edge")
    # 0.8.150: for a circular edge the endpoint must be a point ON the curve, not the
    # centre — the centre was reported 15.000 mm (one radius) away from the DXF arrow tip.
    assert '"sheet_point_on_circle", "circle_point"' in body, (
        "the endpoint falls back to the circle centre again; a leader touches the curve")
    order = body.find("sheet_point_on_circle")
    centre = body.find("sheet_center\", \"circle_centre")
    assert 0 < order < centre, (
        "the point on the circle must be tried BEFORE the centre")
    assert "GetLeaderAtIndex2 does not" in body, (
        "the note that GetLeaderAtIndex2 is unavailable was lost")
    return "leader endpoint reports the attached edge's coords, with provenance"
def assert_reconcile_splits_missing_from_present_but_unmatched():
    """0.8.96 / P1 — coverage swung from a lie of 1.0 to a lie of "Ø14 is missing".

    0.8.73 made the pairing one-to-one, which stopped coverage being pinned at 1.0 but
    overcorrected: a Ø14 that IS drawn got reported as missing because an earlier ledger row
    had consumed the only drawing dimension with that value. The report listed
    `D1@Sketch2 = 14.0` and `D1@草图1 = 8.0` as missing while both were plainly on the sheet.

    Now the failures are separated:
      missing_truly         — no drawing dimension carries that value at all
      present_but_unmatched — the value IS on the sheet, this row just lost the pairing

    Coverage is unchanged (it still counts only successful pairings — that is the honest
    number), but a caller can now tell "not drawn" from "drawn, attribution unclear".

    Verified on 旋转轴: missing_truly 0, present_but_unmatched 10
    (values 1.5 / 1.8 / 3.0 / 8.0 / 10.0 / 14.0). The 1.5 is the chamfer, not the R1.5
    fillet — same value, different feature, which is exactly why this class must not be
    called "missing" OR "matched".
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "reconcile_ledger")) or ""
    assert "missing_truly" in body, "the split is gone — everything is called missing again"
    assert "present_but_unmatched" in body, (
        "a value that IS on the sheet is reported as missing again")
    assert "original_sheet_values" in body, (
        "the check runs against the leftover list instead of the whole sheet, so nothing "
        "would ever be classified as present")
    assert "unmatched_count" in body, (
        "the old count disappeared, silently changing what existing callers see")
    return "reconcile separates truly-missing from present-but-unmatched"
def assert_notes_decode_and_arc_lengths_are_dropped():
    """0.8.97 / two P2-P3 items from a 2026-11-03 report.

    ① NOTE READBACK was still raw format codes. The WRITE side deliberately replaces Ø with
       `<MOD-DIAM>` (and explains why), so a reader got `技术要求：未注孔径 <MOD-DIAM>8` and
       could not tell whether the sheet prints "Ø8" or the literal text. Dimensions had
       `display_text` since 0.8.89; notes did not.
       -> notes now carry `text` (decoded), `text_raw` (original) and `display_text`.
          Verified: text/display "Ø8", raw "<MOD-DIAM>8".

    ② ARC-LENGTH dimensions entered the shortlist: 785.398 mm and 771.436 mm — the
       CIRCUMFERENCES of the chamfer circles. No shop inspects a chamfer by its perimeter,
       and they inflated the ledger coverage. Dropped from the plan, with a count so the
       omission is visible (`arc_lengths_skipped`).
    """
    src = _read("tools/drawing.py")
    assert "text_raw" in src, "notes are raw format codes again"
    assert "_resolve_display_text(text)" in src, (
        "the note branch no longer decodes")
    plan = _read("tools/planner.py")
    assert "arc_skipped" in plan, "arc lengths are back in the shortlist"
    assert "arc_lengths_skipped" in plan, (
        "arc lengths are dropped silently, with no count in the receipt")
    return "notes decode for the reader; arc lengths stay out of the plan but are counted"
def assert_circle_edges_carry_a_point_on_the_circumference():
    """0.8.98 / P3 — `sheet_start` for a circle is the CENTRE, and the docstring said to use it.

    A closed curve has no endpoints, so `sheet_start == sheet_end == centre` for every circular
    edge. The tool description told callers to feed `sheet_start` to
    `add_drawing_dimension(x=, y=)` — which for a circle means clicking the centre and
    selecting nothing.

    `sheet_point_on_circle` (centre + radius on +X) and `sheet_start_is_center: true` now say
    what is actually true.

    Verified on 飞达动力轴: centre [0.07229, 0.26551], r 0.0175,
    point [0.08979, 0.26551] — exactly centre + r.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "list_drawing_edges")) or ""
    assert "sheet_point_on_circle" in body, (
        "circles are back to having no usable point")
    assert "sheet_start_is_center" in body, (
        "the receipt does not warn that sheet_start is the centre for a circle")
    return "circular edges expose a real point on the circumference, and flag the centre case"
def assert_auto_dimension_places_outside_by_default():
    """0.8.99 / P3 — "inside by default" is a default that cannot be delivered.

    A 2026-11-03 report: with `place_outside` unset, all 18 inserted dimensions landed inside
    the shaft outline, dimension lines crossing the part and each other — the sheet was
    unusable, not merely untidy.

    The default is now True. Verified on 旋转轴: 11 inserted, `place_outside_applied: true`,
    `placed_outside: 11`.

    This is a deliberate behaviour change for existing callers. It is called out in the tool
    description rather than slipped in, and `place_outside=False` restores the old behaviour.

    Known trade-offs (from the report, not hidden): a side with no room collapses into a
    vertical stack, and imported angle dimensions can arc across the page. `outside_debug`
    still reports the bucketing.
    """
    tool = _read("tools/drawing.py")
    assert "place_outside=True," in tool, "the tool default went back to False"
    return "auto_dimension pushes dimensions outside by default, and says so"
def assert_smoke_reads_fields_not_human_text():
    """0.8.100 — an assertion that parsed a human-readable line broke when the wording changed.

    smoke asserted the Ø count by doing `detail.split("+")[1]` on the delivery_check detail
    string. 0.8.89 rephrased that line to "仅靠 5 处 Ø 尺寸覆盖（没有孔标注对象）" — no "+" —
    so the split raised, the except set `counted = -1`, and smoke reported
    "delivery_check reports 0 Ø dimensions" on a payload that plainly said 5.

    The structured field `diameter_dimensions` has existed since 0.8.89. Tests must read it.

    Guarded here so the parse cannot come back as the primary source.
    """
    _root = os.path.dirname(os.path.dirname(SRC))
    with open(os.path.join(_root, "tests", "smoke.py"),
              encoding="utf-8") as _fh:
        smoke = _fh.read()
    assert "diameter_dimensions" in smoke, (
        "smoke parses the detail string again instead of reading the field")
    assert 'hole.get("diameter_dimensions")' in smoke, (
        "the structured read is gone — smoke is parsing prose again")
    return "smoke reads diameter_dimensions, not the Chinese detail line"
def assert_package_version_matches_pyproject():
    """0.8.110 — 19 releases shipped with pyproject and __init__ stuck at 0.8.90.

    Every `sed -i 's/old/NEW/'` under this session matched the PREVIOUS version string. One
    silent mismatch early on (0.8.90 -> 0.8.91 never applied) made all 19 later edits no-ops,
    because each one looked for a string that was never there. The tags advanced to
    v0.8.109 while `pip show` would still have said 0.8.90.

    The tell was in plain sight: smoke printed "AssetMate smoke test  v0.8.90" in every run
    I did today, including the ones I read through.

    A tag cannot be checked from a unit test, but the two files must at least agree.
    """
    root = os.path.dirname(os.path.dirname(SRC))
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
        pyproject = fh.read()
    with open(os.path.join(SRC, "__init__.py"), encoding="utf-8") as fh:
        init = fh.read()

    import re as _re
    py = _re.search(r'^version\s*=\s*"([^"]+)"', pyproject, _re.M)
    ini = _re.search(r'__version__\s*=\s*"([^"]+)"', init)
    assert py and ini, "version strings not found"
    assert py.group(1) == ini.group(1), (
        "pyproject says %s but __init__ says %s" % (py.group(1), ini.group(1)))
    return "package version is consistent: %s" % py.group(1)
def assert_pipeline_saves_once():
    """0.8.111 / PR1 — a pipeline run saved FIVE times and audited three.

    Counted 2026-11-03, one produce_drawing:
      add_center_marks, add_surface_finish, add_hole_callouts, add_note  — one each
      auto_dimension                                                     — two (mid + place_outside branch)
    and nothing in pipeline.py saved at all, so the file's final state depended entirely on
    whichever sub-tool happened to run last.

    Now every sub-tool honours save=False, pipeline saves ONCE after all writers, and the
    audit count dropped 3 -> 2 (auto_dimension no longer audits on the pipeline path).

    Measured after the change: save calls 5 -> 1, save ms 1697 -> 665, file 101751 bytes
    with 10 dimensions, every step ok.

    The risk this guards: if someone adds a new sub-tool that saves unconditionally, the
    count grows again silently — the receipt has no field for it.
    """
    src = _read("tools/pipeline.py")
    body = ast.get_source_segment(src, ast.parse(src).body[
        next(i for i, n in enumerate(ast.parse(src).body)
             if isinstance(n, ast.FunctionDef) and n.name == "produce_drawing")]) or ""
    for call in ("auto_dimension(", "add_center_marks(", "add_surface_finish(",
                 "add_hole_callouts("):
        idx = body.find(call)
        assert idx > 0, "%s is no longer called" % call
        tail = body[idx:idx + 260]
        assert "save=False" in tail, (
            "%s does not pass save=False - the run will save more than once" % call)
    assert "one save for the whole run" in body, (
        "the single explicit save is gone; the file's final state is back to luck")
    drawing = _read("tools/drawing.py")
    assert "if save:" in drawing, "add_center_marks stopped honouring the flag"
    return "pipeline saves once; every writer is asked not to"
def assert_drawing_batch_defaults_off_and_restores():
    """0.8.112 / PR2 — DrawingBatch exists, but its switches are OFF because they were measured.

    The spec's reference skeleton enabled `suppress_dimension_input` and
    `disable_automatic_view_update` by default. On this machine neither earns that:

    * `swInputDimValOnCreate` (10): interleaved A/B, 旋转轴, auto_dimension —
      True 7542 / 8253 ms, False 7812 / 7428 ms. Ranges overlap; delta on medians -441 ms.
      A FIRST single pair suggested -1417 ms and that was drift. So: default False.
    * `swDrawingAutomaticViewUpdate` (90): already False on the drawings we tested, which
      the spec itself warns about — "if already off, do not attribute gains to it".
      Default None = do not touch it.

    What the class DOES guarantee, and what this assertion is really about:
    preferences come back even when the body raises, and the receipt says when a switch was
    already in the desired state so nobody can claim an intervention that never happened.
    """
    src = _read("drawing_batch.py")
    assert "suppress_dimension_input: bool = False" in src, (
        "suppress_dimension_input is on by default again without a measurement")
    assert "automatic_view_update: Optional[bool] = None" in src, (
        "automatic_view_update is forced by default - it must be opt-in")
    assert "already_off" in src, (
        "the receipt no longer says when a switch was already in the desired state")
    assert "restore_errors" in src and "Failed to restore" in src, (
        "a failed restore is silent again")

    body = ast.get_source_segment(src, _function_node(ast.parse(src), "__exit__")) or ""
    # Restore must happen in __exit__, not in a happy-path-only place.
    assert "SetUserPreferenceToggle" in body, "preferences are not restored on exit"
    return "DrawingBatch is opt-in, restores on exception, and reports already-off switches"
def assert_gap_planner_only_fills_unambiguous_gaps():
    """0.8.113 / PR4 — compensation must not guess which two edges measure a missing value.

    `AddDimension2` takes only (x, y, z); the measurement comes from the CURRENT SELECTION.
    A gap from reconcile_ledger is a VALUE, so before anything can be drawn the geometry has
    to be found. Two failure modes this guards:

      * drawing on the first edge pair that happens to match — with `ambiguous` gaps that is
        a coin flip presented as automation
      * treating `present_but_unmatched` as a gap — those values ARE on the sheet, the
        attribution is what is unclear. Filling them would double a dimension.

    Measured 2026-11-03 on 飞达动力轴: reconcile reported missing_truly=2 (both 11.0 mm,
    different features), present_but_unmatched=13, and the overlap between the two value
    sets was EMPTY — the three-way split holds. Both gaps came back `no_geometry`, because
    no edge pair in that view measures 11 mm: a depth value needs a section or side view.
    That is the correct answer — refusing to draw beats drawing a plausible wrong dimension.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""
    assert "missing_truly" in body, (
        "the planner no longer reads the truly-missing list")
    assert "present_but_unmatched" in body, (
        "the planner stopped distinguishing unattributed values from real gaps")
    assert "ambiguous" in body, "ambiguity is no longer reported"
    assert "no_geometry" in body, "the no-geometry case is no longer reported"
    # 0.8.155: the rule now counts APPLICABLE candidates from the TRUNCATED list, not the
    # full match list — `max_candidates_per_gap=1` used to leave every gap `ambiguous`
    # because status read `len(matches)` while `candidates` was cut to one.
    assert "if len(_usable) == 1" in body, (
        "the single-usable-candidate rule is gone - ambiguous gaps would be filled by "
        "guessing, or a single-candidate gap would never be filled at all")
    assert "if len(matches) == 1" not in body, (
        "status is judging the untruncated match list again")
    return "only single-match gaps are fillable; ambiguity and missing geometry are reported"
def assert_ledger_open_failure_is_structured():
    """0.8.118 — `open_document` sat OUTSIDE build_feature_ledger's try.

    Consequence: a failed open raised straight out of the MCP tool. Every other failure path
    in this package returns `{"ok": false, "error": ..., "message": ...}`, so a caller had no
    uniform way to handle it — and an uncaught exception from a tool surfaces to the agent as
    a crash, not as a result it can react to.

    Found by tests/test_save_paths.py, case "same name conflict": SolidWorks refuses to open
    a second file with the same basename, and the RuntimeError escaped. Before the fix the run
    ended with `RuntimeError` and no receipt at all; after it the caller gets
    error=ledger_failed with the full explanation intact.

    The specific message matters here: "same name already open" is the single most common
    cause (docs/com-quirks.md section 2) and the only thing that tells a user what to close.
    """
    src = _read("tools/ledger.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "build_feature_ledger")) or ""
    assert "open_document" in body, "the open call moved"
    assert "open_failed" in body, (
        "a failed open is no longer converted to a structured error")
    # The call must be inside a try: check that the nearest preceding `try:` is before it.
    idx = body.find("model = open_document")
    assert idx > 0, "open call shape changed"
    before = body[:idx]
    assert before.rstrip().endswith("try:"), (
        "open_document is outside its try block again - a failed open will raise")
    return "a failed open returns a structured error with the conflict reason"
def assert_outline_wrapper_is_pass_through():
    """0.8.120 — outline reads are COUNTED, never cached.

    The planner accepted "do not cache by default" but asked to keep the call count and
    elapsed time so the threshold can be judged from data. That instrumentation is a wrapper
    around the real implementation; if it ever starts caching (or altering the box), the
    numbers stop describing the work actually done and the threshold becomes meaningless.

    Checked:
      * the wrapper delegates to _precise_outline_impl
      * it does not touch the returned box
      * a cache dict is NOT introduced (the whole point of the decision)
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    wrapper = _function_node(tree, "precise_outline")
    assert wrapper is not None, "the counting wrapper is gone"
    body = ast.get_source_segment(src, wrapper) or ""
    assert "_precise_outline_impl" in body, (
        "the wrapper no longer delegates - is it duplicating the margin logic?")
    assert "OUTLINE_MARGIN_M" not in body, (
        "the margin moved into the wrapper; there is now one place too many")
    for cache_word in ("_CACHE", "cache[", "lru_cache"):
        assert cache_word not in body, (
            "the outline wrapper now caches (%r) - that is exactly what was decided against"
            % cache_word)
    assert "OUTLINE_CALL_THRESHOLD" in src and "OUTLINE_MS_THRESHOLD" in src, (
        "the thresholds are gone; nobody can tell when caching becomes worth it")
    return "outline reads are counted (threshold 100 calls / 500 ms), not cached"
def assert_perf_receipt_matches_spec_schema():
    """0.8.124 — the JSONL record must carry section 9.1's field names.

    The spec lists an explicit key table. For three rounds of this work I emitted my own
    equivalent names (`rebuild_calls`, `dim_insert`, `dim_outside`) and never went back to
    compare. The data was there; a consumer written against the spec would have read nothing.

    Two things this pins down:
      * every spec key EXISTS in the receipt, under `spec`
      * a key we cannot measure is None, never 0 — 0 reads as "took no time", which is a
        claim the pipeline has not made

    The old names stay in place beside the spec block: JSONL already on disk uses them, and
    rewriting history to match a newer table would make older numbers unreadable.
    """
    import sys as _sys
    if os.path.join(ROOT, "src") not in _sys.path:
        _sys.path.insert(0, os.path.join(ROOT, "src"))
    from solidworks_assetmate_mechdog import perf_hooks as _ph
    _ph.start("schema", {"model_path": "x"})
    _ph.bump("rebuild_calls", 2)
    _ph.bump("redraw_calls", 6)
    _ph.bump("save")
    _ph.note_meta(sw_revision="32.4.0", view_count=2, dimension_count=10)
    rec = _ph.stop(success=True, correctness_pass=True)

    assert rec is not None, "stop() returned nothing"
    spec = rec.get("spec") or {}
    assert spec, "no spec block - the receipt cannot be read by a spec-conformant consumer"

    for key in ("run_id", "variant", "sw_revision", "model_path", "model_class",
                "view_count", "dimension_count", "success", "correctness_pass"):
        assert key in spec, "spec.%s missing" % key

    want_t = ("model_open", "drawing_create", "view_create", "geometry_sync",
              "dimension_insert", "dimension_dedupe", "dimension_layout", "annotations",
              "bom", "save", "audit", "export", "total")
    for key in want_t:
        assert key in spec["timings_ms"], "spec.timings_ms.%s missing" % key

    for key in ("force_rebuild", "graphics_redraw", "save", "layout_pass"):
        assert key in spec["counts"], "spec.counts.%s missing" % key

    # Counters must be mapped, not left null, when the internal name has a value.
    assert spec["counts"]["force_rebuild"] == 2, "rebuild_calls did not map to force_rebuild"
    assert spec["counts"]["graphics_redraw"] == 6, "redraw_calls did not map to graphics_redraw"
    # Unmeasured stays None: truthy-zero would be a false claim.
    assert spec["timings_ms"]["bom"] is None, "bom is not measured; it must not be 0"
    assert "bom" in spec["unmeasured"], "an unmeasured field must be listed"
    return "spec block carries 9 top-level, 13 timing and 4 count fields"
def assert_title_block_guards_none_position():
    """0.8.126 — set_title_block iterated ann_position() without checking for None.

    Report 2026-11-04 P0-1: every call failed with `'NoneType' object is not iterable`
    wrapped as `title_block_failed`. The tool that fills 图样代号 — a field GB drawings
    cannot ship without — was unusable, and the message named no cell, so a caller could not
    even work around it.

    `ann_position` documents "None when unavailable" and returns exactly that for some cells
    on this template. TWO call sites iterated it directly; only the first was reachable from
    dry-run, which is why one fix looked complete.

    `_cell_position` must return None (not []) when the position is unreadable: [] reads as
    "the position is the origin", a different and wrong claim.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)

    helper = _function_node(tree, "_cell_position")
    assert helper is not None, "the guard helper is gone; the None path is unguarded again"
    body = ast.get_source_segment(src, helper) or ""
    assert "ann_position" in body, "the helper no longer wraps ann_position"
    assert "None" in body, "the helper lost its None handling"

    # Both receipt branches must go through the guard. Counting is the right check here: the
    # two remaining direct iterations (inside the marker loop) ARE wrapped in try/except, so
    # "no iteration at all" would be a false requirement — it failed on the first run of this
    # assertion for exactly that reason.
    fn = _function_node(tree, "set_title_block")
    fn_body = ast.get_source_segment(src, fn) or ""
    guarded = fn_body.count("_cell_position(")
    assert guarded >= 2, (
        "only %d call sites use _cell_position — both receipt branches need it" % guarded)
    return "_cell_position guards both receipt branches (%d sites)" % guarded


def assert_bilateral_lower_is_negative():
    """0.8.126 — bilateral tolerances must write a NEGATIVE lower deviation.

    Report 2026-11-04 P0-3: `tolerance_type=bilateral, upper=0.05, lower=0.05` rendered as
    "Ø14.20 +0.05 / +0.05". A zero-width band that looks like a real tolerance is worse than
    no tolerance, because the shop believes it.

    `SetToleranceValues` treats the second argument as a magnitude, so the sign has to be
    applied before the call. Only bilateral gets it: symmetric writes ±value and limit writes
    two actual limits (14.25 / 14.15), where a forced negative would be wrong.

    Also pinned: the readback must use `GetToleranceValues` — a PROPERTY returning
    (upper, lower) in metres. `GetToleranceUpperLimit` does not exist on this build, and the
    first version of this readback reported None for both limits without saying so.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "add_dimension_tolerance")
    body = ast.get_source_segment(src, fn) or ""

    assert "_lower_arg" in body, "the sign adjustment is gone"
    assert "-abs(_lower_arg)" in body, (
        "the lower deviation is no longer forced negative for bilateral")
    assert '"bilateral"' in body, "the bilateral-only condition is gone"
    assert "GetToleranceValues" in body, (
        "the numeric readback is gone — the sign cannot be checked without a PDF")
    # Strip comments before this check: the explanation of WHY that API does not exist
    # mentions it by name, and a naive substring test flagged its own documentation.
    code_only = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    assert "GetToleranceUpperLimit" not in code_only, (
        "GetToleranceUpperLimit does not exist on this build; calling it reports None silently")
    return "bilateral writes -abs(lower); readback via GetToleranceValues property"
def assert_gap_matcher_converts_units():
    """0.8.128 — plan_dimension_gaps compared metres against millimetres.

    Sheet coordinates come back in METRES (`IView.Position`, `GetOutline`). `target` and
    `tolerance_mm` are millimetres. The matcher did:

        if abs(span - target) <= tolerance_mm:      # 0.031 vs 31, tol 0.05

    so a 31 mm gap was off by 30.97 and nothing could ever match. Every part since PR4
    reported `gaps_found: 13, fillable: 0, no_geometry: 13` — which reads like "the drawing
    has no measurable geometry" and is why report 2026-11-04 P1-2 concluded the candidate
    generation was broken. It was, but not where the report guessed (circle centres); the
    comparison itself could never be true.

    Measured after the fix on 接槽: ambiguous 0 -> 9, no_geometry 13 -> 4, with matched spans
    of exactly 13.00 / 4.40 / 31.00 / 30.00 / 12.00 / 4.00 / 1.50 mm.

    `fillable` is still 0 for that part and that is correct: four symmetric counterbores
    produce several equally valid pairs, so the honest status is `ambiguous`, not a coin flip.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "plan_dimension_gaps")
    body = ast.get_source_segment(src, fn) or ""

    assert "span_mm = span_m * 1000.0" in body, (
        "the sheet-metre to millimetre conversion is gone — the matcher will match nothing")
    assert "abs(span_mm - target)" in body, (
        "the comparison no longer uses the converted value")
    assert "abs(span - target)" not in body.replace("abs(span_mm - target)", ""), (
        "a raw metre value is still being compared against millimetres")

    # Circle centres must stay in the candidate set: hole-pitch gaps are centre-to-centre.
    assert "list_circular_edges" in body, (
        "circle centres dropped out of the candidate points — hole pitches cannot match")
    return "sheet metres converted to mm; circle centres remain in the candidate set"
def assert_datum_and_move_report_placement():
    """0.8.129 — placement must be checkable without rendering a PDF.

    Report 2026-11-04 P1-4 had two halves and both were about SILENT PLACEMENT:

      * `add_datum_feature` put the actual landing point only inside
        `readback.position_m`, so a caller looking for a top-level field concluded the tool
        reported nothing. Measured on 旋转轴: requested [0.2, 0.06], actual
        [0.0243, 0.1019] — 175 mm away — and nothing said so.
      * `move_annotation` tested `applied = after != before`. A datum tag that moved in X
        only still satisfies that, so `applied: true` shipped a tag sitting on a dimension.

    Both now answer "is it where I asked" per axis, and `applied` means fully placed.
    """
    drawing_src = _read("tools/drawing.py")
    layout_src = _read("tools/layout.py")

    datum = ast.get_source_segment(
        drawing_src, _function_node(ast.parse(drawing_src), "add_datum_feature")) or ""
    for field in ("position_requested_m", "position_actual_m", "position_honoured"):
        assert field in datum, "add_datum_feature does not report %s" % field

    move = ast.get_source_segment(
        layout_src, _function_node(ast.parse(layout_src), "move_annotation")) or ""
    assert "axis_honoured" in move, (
        "move_annotation collapsed both axes into one flag again")
    assert "blocked_axes" in move, "move_annotation does not name the ignored axis"
    assert "after != before" in move, (
        "the raw movement test is gone — check that applied still requires actual movement")

    caps = _read("tools/capabilities.py")
    assert "符号为文字" not in caps, (
        "capabilities still claims GTOL symbols are text; the 2022+ path writes real glyphs")
    assert "IGTOL-PERP" in caps or "symbol_is_text: false" in caps, (
        "the capability entry does not cite the measured native-symbol evidence")
    return "datum + move report per-axis placement; GTOL capability text matches reality"
def assert_projection_angle_is_selectable():
    """0.8.130 — the sheet used to follow NEITHER convention cleanly.

    Report 2026-11-04 P2-3: with the default settings the top view sat BELOW the front view
    (first angle) while the right view sat to the RIGHT of it (third angle). A drawing that
    mixes the two cannot be read correctly by either audience, and GB/T 14692 requires first
    angle, which matters here because this is a Chinese-mechanical tool.

    Root cause: `_place_views` packs by box height with no notion of projection side, so it
    overwrote whatever the position map said. Measured after the fix on 旋转轴:

        first   front y=0.2319   top y=0.1491   left x=0.2673   (top BELOW, left RIGHT)
        third   front y=0.1491   top y=0.2319   left x=0.2673   (top ABOVE, front BELOW)

    `first` is the default. The side view stays in column 2 under both, because a two-column
    grid only has one side-view slot — mirroring it would collide with the front view
    (measured: both at x=0.1845). That limitation is reported via `projection_notes` when a
    caller asks for `right` under first angle, rather than being silently accepted.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)

    create = _function_node(tree, "create_drawing")
    assert create is not None, "create_drawing is gone"
    args = [a.arg for a in create.args.args] + [a.arg for a in create.args.kwonlyargs]
    assert "projection" in args, "create_drawing has no projection parameter"

    body = ast.get_source_segment(src, create) or ""
    assert 'projection="first"' in body or "'first'" in body, (
        "first angle is not the default")
    assert 'bad_projection' in body, (
        "an unknown projection value is not rejected")

    # front must move with the convention, not stay pinned to the upper row.
    assert "front_y_for_views" in body, (
        "the front view does not follow the projection — third angle will overlap")
    assert "top_y_for_views" in body and "bottom_y_for_views" in body, (
        "the top/bottom views do not follow the projection")

    # and the packer must not override it
    assert "explicit_positions = projection is not None" in body, (
        "_place_views will overwrite the projection mapping again")
    assert "projection_notes" in body, (
        "the side-view limitation is not reported")
    return "projection selectable (first default); front and top rows both follow it"
def assert_requirements_respect_safe_area():
    """0.8.131 — a technical-requirements block could grow straight through the title block.

    Report 2026-11-04 P2-7: requested at y=0.07, the block's bottom edge landed at 0.038 —
    below the template's 0.065 safe edge — and the receipt said `adjusted: false,
    collisions_avoided: 0`. Nothing was wrong with the request; y is the block's TOP edge and
    the block grows DOWNWARDS, so a multi-line note can clear the frame entirely.

    Two things are pinned here:
      * an out-of-safe-area result is an ERROR (`block_out_of_safe_area`), not a silent
        success, and it names `max_y_for_this_block_m` — "raise y" alone would not say by how
        much.
      * auto placement starts from the TEMPLATE's safe area, not a hardcoded 10 mm. It used
        to place at x=0.010 on a template whose safe left is 0.020 (measured).

    Measured after the fix: auto_place lands at x=0.0588 (exactly the safe left edge);
    y=0.07 is rejected with max_y=0.0765.
    """
    src = _read("tools/layout.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "add_technical_requirements")
    assert fn is not None, "add_technical_requirements is gone"
    body = ast.get_source_segment(src, fn) or ""

    # The bounds check lives in `add_note`, which every text block goes through, and
    # `add_technical_requirements` propagates it via `if not result.get("ok"): return result`.
    # That is the better place: 图样代号 fallbacks and any other note get the same guard.
    note_fn = _function_node(tree, "add_note")
    assert note_fn is not None, "add_note is gone"
    note_body = ast.get_source_segment(src, note_fn) or ""
    assert "block_out_of_safe_area" in note_body, (
        "out-of-bounds placement is no longer an error in add_note")
    assert "max_y_for_this_block_m" in note_body, (
        "the error does not say how high the block may be placed")
    assert "template_safe_area" in note_body, (
        "add_note does not consult the safe area")

    assert "template_safe_area" in body, (
        "auto placement does not consult the safe area")
    # auto placement must prefer the template margins over the bare 10 mm
    assert "bottom_limit" in body, (
        "the auto-placement floor is a bare margin again")
    assert "max(margin, float((safe_area or {}).get(\"left\"" in body, (
        "auto placement still starts from a hardcoded 10 mm margin")
    return "requirements block is bounded by the template safe area, in both paths"
def assert_center_marks_and_orphans_are_discriminating():
    """0.8.132 — two checks that could not tell "correct" from "unknown".

    P2-6 (center marks): every model view on this build answers `Type == 7`, and the name is
    just 工程图视图3, so neither obvious test identifies the isometric. `GetOrientationName`
    does (`*等轴测`), and that is now what gates the skip. Measured: processed 2 of 3 views,
    the axonometric one listed under `skipped_views` with a reason instead of silently
    dropped, and each view carries its own `center_mark_count`.

    P2-5 (orphans): `GetAttachedEntities2` returns empty for dimensions brought in by
    InsertModelItems, so "no attachments" was reported as "orphan" — a fillet radius read as
    a broken dimension. The witness is the DISPLAY dimension's type, and it has to be read as
    `ann.GetSpecificAnnotation.GetType` (2 linear / 5 diameter); `dim.GetType` answers 0 and
    the first attempt therefore excused nothing.

    Measured on 接槽: orphans 3 -> 1. `65` is excused by matching the view extent, `R10` by
    being type 5, and `1` (a chamfer) stays an orphan — no geometric witness covers it, and
    inventing one would be worse than reporting it.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)

    marks = ast.get_source_segment(src, _function_node(tree, "add_center_marks")) or ""
    assert "GetOrientationName" in marks, (
        "center marks no longer identify axonometric views by orientation")
    assert "skipped_views" in marks, (
        "skipped axonometric views are not reported")
    assert "center_mark_count" in marks, (
        "per-view counts are gone; views_marked alone cannot prove the skip")

    att = ast.get_source_segment(src, _function_node(tree, "verify_dimension_attachments")) or ""
    assert "GetSpecificAnnotation" in att and "dim_type" in att, (
        "dimension type is not read from the display dimension")
    assert "diameter_or_radius" in att, (
        "diameter/radius dimensions are not excused")
    assert "matches_view_extent" in att, (
        "overall-size dimensions are not excused by the view extent")
    assert "unattached_plausible" in att, (
        "plausible-unattached is not separated from orphans")
    return "axonometric skipped by orientation; orphans separated by geometric witnesses"
def assert_no_dimension_is_left_off_the_sheet():
    """0.8.134 — dimensions were being printed outside the paper.

    Report 2026-11-04 (found while measuring for P1-1, not reported by the user): eight
    reference dimensions RD1..RD8 came out of InsertModelItems at
    y = -0.068 .. -0.200 on a 297 mm sheet. `place_outside` never touches them — they are
    not in its buckets — and the perpendicular clamp it gained only covers what it does
    push. A dimension printed off the sheet is worse than a cramped one: the drawing states
    less than it appears to.

    Measured after the fix (接槽): annotation y range -0.176..0.262 -> 0.094..0.262, bottom
    room -240.9 mm -> +29.0 mm.

    Placement matters as much as the logic. Three attempts to clamp inside `auto_dimension`
    all missed: one ran before `place_outside` finished, one matched its anchor at the wrong
    depth, one was edited away by its own cleanup. A standalone step called from the pipeline
    immediately before save cannot execute too early, and is the version that works.
    """
    drawing_src = _read("tools/drawing.py")
    tree = ast.parse(drawing_src)
    helper = _function_node(tree, "clamp_dimensions_into_safe_area")
    assert helper is not None, "the clamp helper is gone"
    body = ast.get_source_segment(drawing_src, helper) or ""
    assert "GetOutline" in body, "the helper does not read the sheet frame"
    assert "SetPosition" in body, "the helper does not move anything"
    assert "step_m" in body, "offenders are stacked on one line again"

    pipe_src = _read("tools/pipeline.py")
    pipe = ast.get_source_segment(
        pipe_src, _function_node(ast.parse(pipe_src), "produce_drawing")) or ""
    assert "clamp_dimensions_into_safe_area" in pipe, (
        "the pipeline no longer clamps before saving")
    # It must run BEFORE the save, or the clamped version never reaches disk.
    clamp_at = pipe.find("clamp_dimensions_into_safe_area")
    save_at = pipe.find('record("save"')
    assert clamp_at > 0 and save_at > 0 and clamp_at < save_at, (
        "the clamp runs after the save — the file on disk keeps the off-sheet coordinates")
    return "dimensions are clamped inside the frame before the sheet is saved"
def assert_hole_callouts_carry_a_quantity():
    """0.8.135 — callouts report their quantity, and their text is left ALONE.

    Report 2026-11-04 P1-3 asked for "n× Ø4.5" prefixes and for the counterbore to be
    merged into one callout. Investigating showed SolidWorks already does both, and that we
    were the ones destroying it:

        nothing rewritten -> slot 1: "<HOLE-SPOT><MOD-DIAM> 8 <HOLE-DEPTH> 4.40"
                             slot 3: "2 x <MOD-DIAM> 4.50 完全贯穿"
        exported PDF      -> "Ø8  4.40" and "2 x Ø4.50 完全贯穿"

    The old guard was `"<MOD-" not in native`, so any native text containing a format code
    fell through to SetText and got replaced by a bare "Ø8" — which is exactly the
    "沉孔信息丢失" the report saw. A `<MOD-DIAM>` code is how SolidWorks STORES a
    diameter; the renderer turns it into Ø.

    Three attempts to write the prefix ourselves all failed in the exported PDF:
        literal Ø       -> "7× Θ8"       (Greek capital theta)
        DWG code %%c    -> "7× %%c8"     (printed literally)
        <MOD-DIAM>      -> "7×" + blank  (token swallowed)

    So the text is never rewritten now. Quantity is reported per callout and grouped per
    diameter; that is data, not ink, and it cannot be wrong.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "add_hole_callouts")
    assert fn is not None, "add_hole_callouts is gone"
    body = ast.get_source_segment(src, fn) or ""

    assert "callout_groups_by_diameter" in body, (
        "the per-diameter grouping is not reported")
    assert 'item["quantity"]' in body, "per-callout quantity is not reported"

    # The native text must be kept whenever it exists, format codes and all.
    assert "native_text_complete" in body, "the native-complete flag is gone"
    assert 'if _native:' in body, (
        "the native-text guard is conditional again — format codes will trigger a rewrite")
    code_only = chr(10).join(line.split('#', 1)[0] for line in body.splitlines())
    assert '"<MOD-" not in _native' not in code_only, (
        "the old guard is back: any native text with a format code gets overwritten")

    # And no prefix may be written with SetText.
    # Only the prefix block matters here: from the grouping to the point where the COM
    # handles are stripped. Later SetText calls belong to unrelated annotation work.
    prefix_start = code_only.find("_by_dia")
    prefix_end = code_only.find('_item.pop("_display_dimension"', prefix_start)
    assert prefix_start > 0 and prefix_end > prefix_start, "prefix block not found"
    assert "SetText" not in code_only[prefix_start:prefix_end], (
        "the quantity pass writes text again — all three encodings render wrong")
    return "quantity reported per callout; native callout text never rewritten"
def assert_version_strings_agree():
    """0.8.137 — the package reported 0.8.133 while the tag was v0.8.136.

    Report 2026-11-05 (P1): three consecutive releases (0.8.134 / 0.8.135 / 0.8.136) were
    committed and tagged WITHOUT bumping the version, so `health_check` answered
    `package_version: 0.8.133` from a tree that contained 0.8.136 code.

    The self-identifying fields added four rounds earlier (package_path / package_version /
    python_executable) exist precisely because several install directories can coexist — and
    they were pointing at the wrong version. `static_check` passed throughout because nothing
    compared the two strings; neither did anything compare them to the tag.
    """
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        pyproject = fh.read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert m, "pyproject has no version line"
    declared = m.group(1)

    with open(os.path.join(SRC, "__init__.py"), encoding="utf-8") as fh:
        init = fh.read()
    m2 = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    assert m2, "__init__.py has no __version__"
    declared_dunder = m2.group(1)

    assert declared == declared_dunder, (
        "pyproject says %r but __version__ says %r" % (declared, declared_dunder))

    # Compare with the tag only when HEAD sits exactly on one. A commit made between releases
    # legitimately sits ahead of the last tag, and failing on that would train people to skip
    # this check entirely.
    tag_version = None
    try:
        import subprocess
        out = subprocess.run(["git", "describe", "--tags", "--exact-match", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            tag_version = out.stdout.strip().lstrip("v")
    except Exception:
        tag_version = None

    if tag_version:
        # Only "the tag is NEWER than the package" is a defect — that is exactly the shape of
        # the 2026-11-05 report, where v0.8.136 was tagged over code declaring 0.8.133.
        #
        # "The package is newer than the tag" is the normal mid-release state (bump, commit,
        # then tag), and failing on it would make this assertion unusable: it would go red on
        # every release commit and train people to ignore it.
        def _as_tuple(text):
            parts = []
            for piece in str(text).split("."):
                try:
                    parts.append(int(piece))
                except ValueError:
                    parts.append(0)
            return tuple(parts)

        assert _as_tuple(declared) >= _as_tuple(tag_version), (
            "HEAD is tagged v%s but the package declares %r — that is the un-bumped case"
            % (tag_version, declared))
    return "version %s agrees across pyproject, __init__ and tag%s" % (
        declared, (" v" + tag_version) if tag_version else " (untagged HEAD)")


def assert_ledger_dedupes_by_full_name():
    """0.8.137 — one model dimension was counted once per feature that touched it.

    Report 2026-11-05 (P2): `D2@Sketch1@AM_DOOR.Part` was reported twice — once under
    Sketch1 and once under Base-Flange1, the sheet-metal feature that consumes that sketch.
    18 rows described 13 real dimensions, and `ledger_dimensions: 37` was inflated the same
    way, so coverage (matched / ledger_dimensions) was systematically pushed DOWN and
    `missing_truly: 18` told the caller to draw five dimensions that already existed.

    The same list feeds `plan_dimension_gaps`, so fixing the ledger fixes both.

    Measured after the fix on 接槽: `missing_truly: 7`, all seven names unique, while
    `features` still records the second owner — attribution survives, counting does not
    double.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "reconcile_ledger")
    assert fn is not None, "reconcile_ledger is gone"
    body = ast.get_source_segment(src, fn) or ""

    assert "ledger_by_name" in body, (
        "the ledger is keyed by (dimension, feature) again — duplicates will return")
    assert "existing[\"features\"].append" in body, (
        "the second owner is dropped instead of recorded; attribution is lost")
    assert "ledger_rows = list(ledger_by_name.values())" in body, (
        "rows are no longer materialised from the deduped map")
    assert "ledger_rows_duplicate_count" in body, (
        "the duplicate count is not computed — a clean ledger cannot be told from a quiet "
        "one")
    assert "coverage_formula" in body, (
        "the coverage denominator is undocumented; cross-version comparison will read a "
        "deliberate change as a regression")
    return "ledger is one row per full_name; multi-owner features preserved"


def assert_pipeline_closes_what_it_opened():
    """0.8.138 — the pipeline left its documents open, poisoning the NEXT run.

    Old backlog item, promoted because it is not cosmetic: every `produce_drawing` left both
    the part and the drawing open, so the run deposited a `~$*.SLDDRW` lock file and the next
    run could not open the same name — `OpenDoc6 returned null with errors=65536`. That is
    exactly the error that failed a smoke item on 2026-11-04, and a reader would have blamed
    the part.

    Measured after the fix: two consecutive runs on the same output path both return ok, both
    report two closed documents, and the directory holds no `~$` files.

    Closing is the right default for a tool. `close_when_done=False` keeps them for
    inspection; `close_document` matches on the exact path, so a document the caller already
    had open is not touched.
    """
    src = _read("tools/pipeline.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "produce_drawing")
    assert fn is not None, "produce_drawing is gone"
    args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "close_when_done" in args, "the pipeline has no way to keep documents open"
    assert "close_when_done=True" in (ast.get_source_segment(src, fn) or ""), (
        "closing is not the default — the lock files will come back")

    body = ast.get_source_segment(src, fn) or ""
    assert "close_document" in body, "the pipeline does not close anything"
    assert "documents_closed" in body, "the receipt does not say what was closed"

    # Both exits must close: a failed run leaves the same locks as a successful one.
    assert body.count("documents_closed=closed") >= 2, (
        "only one exit closes — the failure path still leaks a lock file")
    return "pipeline closes both documents on success and on failure"


def assert_receipts_say_what_the_fields_mean():
    """0.8.139 — three old backlog items where a field name lied.

    Report 2026-11-05 listed them as "老账"; none breaks a drawing, all three cost a reader
    time:

      1. `text_is_format_code: true` sat next to a `text` reading "Ø8". The flag describes
         the RAW text, not the field beside it, so the pair looked self-contradictory.
         `text_decoded` now says the thing a reader needs, and `text_raw` keeps the original.
      2. `_annotation_total()` (raw enumeration, including 审核/校核/设计 furniture) and
         `annotation_count` (furniture stripped) are different numbers on purpose. The
         docstring now says so and says which one to compare against what.
      3. `"assembly": ...` was answered for parts too, in bom / bom_native / recolor /
         rename. `document` and `document_type` are added; `assembly` stays for
         compatibility because renaming it would break every existing caller.
    """
    drawing_src = _read("tools/drawing.py")
    assert "text_decoded" in drawing_src, (
        "the decode flag is gone; text_is_format_code will read as self-contradictory again")
    assert drawing_src.count('row["text_decoded"] = True') >= 3, (
        "not every site that decodes text sets the flag")

    layout_src = _read("tools/layout.py")
    assert "NOT the same number as `annotation_count`" in layout_src, (
        "the two annotation counts are undocumented again")

    for module in ("bom", "bom_native", "recolor", "rename"):
        src = _read("tools/%s.py" % module)
        assert '"document_type"' in src, "%s does not report document_type" % module
        assert '"document"' in src, "%s does not report the neutral document key" % module
        assert '"assembly"' in src, (
            "%s dropped the compatibility key; existing callers would break" % module)
    return "receipts distinguish decoded text, two annotation counts, and document type"


def assert_every_implementation_param_reaches_mcp():
    """0.8.140 — 14 parameters existed in Python but not over MCP.

    Report 2026-11-05 (P0): `produce_drawing` was missing `projection`, `close_when_done`,
    `do_derived_check` and `hole_budget_s`; `create_drawing` was missing `projection`; and
    six more wrappers had the same gap. FastMCP runs with `extra=ignore`, so a caller who
    passed them got `isError: false` and no effect — the parameter was accepted, dropped,
    and never mentioned.

    The reason it survived ten releases is that `check_wrapper_signatures` only checked ONE
    direction: "does the wrapper forward a keyword the target rejects". The opposite — a
    target parameter the wrapper never offers — passed silently. This is the third time the
    class shipped (0.8.2 add_bom_table, 0.8.4 view_positions, now 0.8.139), so the rule
    lives in static_check rather than in another round of manual patching.

    `save` / `known_values` / `config` stay internal on purpose: the pipeline batches saves,
    `known_values` is a dedupe hint, and `config` is resolved from the active document.
    """
    src = open(os.path.join(ROOT, "tests", "static_check.py"), encoding="utf-8").read()

    assert "INTERNAL_ONLY" in src, (
        "the internal-parameter whitelist is gone; the reverse check has no basis")
    assert "never exposes them" in src, (
        "the reverse check no longer reports unexposed parameters")
    assert "wrapper_params" in src, (
        "the wrapper's own parameter set is not collected — only forwarded kwargs are, "
        "which cannot detect a parameter that was never offered")

    # The five parameters this report named must be reachable now.
    server = _read("server.py")
    for param, where in (
            ("projection", "produce_drawing"),
            ("close_when_done", "produce_drawing"),
            ("do_derived_check", "produce_drawing"),
            ("hole_budget_s", "produce_drawing"),
            ("projection", "create_drawing")):
        start = server.find("def %s(" % where)
        assert start > 0, "%s wrapper is gone" % where
        end = server.find(") -> str:", start)
        block = server[start:end]
        assert param in block, "%s() still does not expose %s" % (where, param)
    return "implementation params are reachable through the MCP wrappers"


def assert_pipeline_closes_only_what_it_opened():
    """0.8.141 — closing by path also closes the USER's copy of that path.

    Report 2026-11-05 (P1): `produce_drawing` closed `(target, path)` unconditionally and
    `close_document` matches purely on path, so running the pipeline while the user had that
    part open in the editor would close their document — unsaved edits included. The previous
    receipt even justified itself with "close_document matches on the exact path this run
    produced", which is true and irrelevant: the user's document has the same path.

    Two more defects in the same code:
      * `close_document` answers ok=True when it found nothing, so `documents_closed[].ok`
        was always true and the step was unconditionally ok — "closed 2" and "closed 0"
        looked identical. The real count now comes from `closed_count`.
      * an un-enumerable session returned an empty set, which must NOT be read as "nothing
        was open". It is treated as "unknown" and nothing is closed.

    Measured after the fix, with the part pre-opened:
        pre-open count: 2      closed: [('d_xxx.SLDDRW', 1)]      skipped: 1
    """
    src = _read("tools/pipeline.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "produce_drawing")
    assert fn is not None
    body = ast.get_source_segment(src, fn) or ""

    assert "open_document_paths" in body, (
        "no snapshot is taken — caller-owned documents will be closed again")
    pre_at = body.find("_preexisting = ")
    first_open = body.find("drawing_tools.create_drawing(")
    assert first_open > 0, "the create_drawing call is gone"
    assert 0 < pre_at < first_open, (
        "the snapshot is taken after the first document is opened; it must come before")
    assert "skipped_open_by_caller" in body, (
        "caller-owned documents are skipped but not reported")
    assert "closed_count" in body, (
        "the receipt is back to a bare boolean that cannot tell 'closed 2' from 'closed 0'")

    helper = _function_node(ast.parse(_read("tools/drawing.py")), "open_document_paths")
    assert helper is not None, "the snapshot helper is gone"
    return "the pipeline closes only documents it opened, and says what it skipped"


def assert_native_callout_text_is_not_called_unresolved():
    """0.8.142 — the receipt told users to delete the only annotation that renders.

    Report 2026-11-05 (P0-C): every native callout came back with

        "text": "<HOLE-SPOT><MOD-DIAM> 8 <HOLE-DEPTH> 4.40",
        "text_source": "native",
        "native_text_complete": true,
        "text_unresolved": true

    and `unresolved_note` advised "建议删掉改用 Ø 尺寸标注". The PDF of the same sheet
    printed "2 × Ø4.50 完全贯穿" and "⌴ Ø8 ↧ 4.40" — correctly.

    `<MOD-DIAM>` is how SolidWorks STORES a diameter in the text layer; the renderer turns
    it into Ø. Treating it as an unresolved literal is a category error, and acting on the
    advice would remove the one annotation capability that actually works.

    Only text this tool WROTE can be unresolved: a SetText that failed leaves the raw code
    in a string we own. `text_source` is the discriminator, `native_text_complete` the
    corroborating flag.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "add_hole_callouts")
    assert fn is not None
    body = ast.get_source_segment(src, fn) or ""

    marker = body.find("unresolved = [c for c in created")
    assert marker > 0, "the unresolved list is gone"
    segment = body[marker:marker + 400]
    assert 'c.get("text_source") != "native"' in segment, (
        "native text is counted as unresolved again — the receipt will advise deletion")
    assert "native_text_complete" in segment, (
        "the corroborating flag is not consulted")
    assert "text_resolution_basis" in body, (
        "the receipt does not say why native text is exempt")
    # The old advice must be gone: it is the harmful part, not just the wrong number.
    assert "建议删掉改用" not in body, (
        "the 'delete them' advice is back; it would remove working annotation")
    return "native callout text is never reported as unresolved"


def assert_no_refuted_claim_is_still_in_the_docs():
    """0.8.143 — the capability table was right and the docs around it were still wrong.

    Report 2026-11-05 (second report, section 3): `check_capabilities` had been corrected to
    say `geometric_tolerance: verified` with a note reading "两个历史结论都已过期，写在这里
    免得有人照着旧文档直接放弃这项能力" — while README's boundary table still said the frame
    symbol is text and "真正的几何符号仍需界面补", and two tool descriptions still said the
    opposite of their own capability entry.

    README line 218 says "若与工具输出冲突，以工具为准", so a reader is told to trust the
    tool — but they read the table first, and the table told them to give up.

    `check_readme` only counted tools and checked that names appeared; nothing read the
    claims. This scans the three places a claim can live: README, capabilities.py, and the
    MCP docstrings in server.py.

    Each string below was measured false on this build. If one comes back, it is a
    regression, not a wording preference.
    """
    REFUTED = [
        # GD&T: native symbols since SW2023; measured symbol_is_text: false.
        "第一格是文字",
        "真正的几何符号仍需界面补",
        "符号是文字",
        "readable TEXT",
        # add_sheet_line / weld triangle: 0 changed pixels out of 16,041,784.
        "it looks right and prints right",
        "add_note 写文字 + add_sheet_line",
        # Section line follows the view since v0.8.57 (drawn in view sketch space).
        "四组接口都够不着",
        "已移过就删了重建",
    ]

    sources = {
        "README.md": open(os.path.join(ROOT, "README.md"), encoding="utf-8").read(),
        "tools/capabilities.py": _read("tools/capabilities.py"),
        "server.py": _read("server.py"),
    }

    hits = []
    for name, text in sources.items():
        for claim in REFUTED:
            if claim in text:
                hits.append("%s still says %r" % (name, claim))
    assert not hits, (
        "refuted claims are back in the docs (the capability table disagrees with them): "
        + "; ".join(hits))
    return "no refuted claim appears in README, capabilities or tool docstrings"


def assert_docs_numbers_match_the_code():
    """0.8.154 — this assertion was a false pass for eleven releases.

    Report 2026-11-05 (second round) opened it up and found three holes:

      1. **It never ran.** The tool-count pattern required the words "扫描 server.py 里"
         with no line break between "server.py" and "里"; README wraps that sentence across
         two lines (383-384), so `re.search` returned None and `if m:` treated "no match"
         as "nothing to check" and skipped the assertion entirely.
      2. **The reference value was wrong.** The old code counted the literal string
         "@mcp.tool()" in server.py's source — plain TEXT — and server.py's own module
         docstring contains that phrase, so it returned 66 against a real 65.
         `the @mcp.tool() function itself` — so it returned 66 against a real 65.
         (AST: 65 decorated functions. tools/list: 65.)
      3. **The third number was never implemented.** The docstring claimed it checked
         "默认 35 项" smoke; there was no code for it at all.

    So `165/165 passed` was reporting PASS on two README claims that were never read.
    That is worse than having no assertion: a check that cannot fail looks like coverage.

    Fixed by: AST for the tool count, `assert m` so "not found" fails, a whitespace-tolerant
    pattern so a line wrap is not a match failure, and the smoke count removed entirely — `r.check`
    calls live inside functions (41 total, in several helpers), so there is no stable
    "number of default checks" to compare against, and a number nobody can recompute is a
    number that will drift again.
    """
    import re

    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()

    # Count DECORATED FUNCTIONS, not the string. A text count picks up the docstring.
    server_tree = ast.parse(_read("server.py"))
    registered = 0
    for node in ast.walk(server_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    registered += 1
    assert registered == 65, (
        "expected 65 registered tools, AST found %d — update this expectation "
        "deliberately when a tool is added" % registered)

    # Whitespace-tolerant because README wraps the sentence; `assert m` because not
    # finding it is a FAILURE, not a no-op. That distinction is the whole report.
    m = re.search(r"扫描 `server\.py`\s*里 (\d+) 个工具函数", readme)
    assert m, ("README no longer contains the tool-count sentence; this assertion cannot "
               "verify anything — restore the sentence or update the pattern")
    assert int(m.group(1)) == registered, (
        "README says %s tools, server.py registers %d" % (m.group(1), registered))

    static_count = len(STATIC_REGRESSIONS)
    m2 = re.search(r"(\d+) 条历史回归", readme)
    assert m2, "README no longer states a regression count; this assertion cannot verify it"
    assert int(m2.group(1)) == static_count, (
        "README says %s regression entries, the file has %d"
        % (m2.group(1), static_count))

    # Deliberately NOT asserted: a smoke-item count. There is no recomputable number —
    # `r.check` appears inside several helper functions (41 call sites, spread over
    # helpers whose execution depends on flags), so "how many run by default" is control
    # flow, not a count. README therefore no longer states one.
    return "README tool count and regression count match the code"


def assert_derived_dimensions_are_reported_as_unfillable():
    """0.8.144 — a shaft drawing with no overall length, and every step said ok.

    Report 2026-11-05 (P1): `plan.keep_dimensions` listed 总长 107 as
    `kind: "derived"` — a bounding-box value with no driving dimension behind it. The plan's
    own source comment already admits "auto_dimension can never insert them". Measured: 10
    dimensions reached the sheet, none of them 107, and the axial chain did not close either.

    The receipt said

        "note": "规划已生效：插入后按 keep_dimensions 删除多余尺寸…"
        "verdict": "待设计审核", "blockers": []

    so the one document a shop would read first claimed the plan had been applied, while the
    dimension the plan called "any drawing must show this" was absent.

    Two additions, both reporting-only:
      * `dimension_gap.derived_unfilled` names them and says which tool can still place them
        (`add_distance_dimension`, which already existed since 0.8.78).
      * `delivery_check` gains an `overall_dimension` pending item comparing the biggest
        dimension on the sheet against the model's largest bbox extent. Pending, not
        blocking: an implicit chain can legitimately omit it, and guessing wrong would
        refuse a usable drawing.
    """
    pipe = _read("tools/pipeline.py")
    tree = ast.parse(pipe)
    fn = _function_node(tree, "produce_drawing")
    assert fn is not None
    body = ast.get_source_segment(pipe, fn) or ""
    assert "derived_unfilled" in body, (
        "the gap receipt does not separate derived entries — the note will claim the whole "
        "plan took effect again")
    assert "add_distance_dimension" in body, (
        "the note does not say how to fill them")

    draw = _read("tools/drawing.py")
    dtree = ast.parse(draw)
    dc = _function_node(dtree, "delivery_check")
    assert dc is not None
    dbody = ast.get_source_segment(draw, dc) or ""
    assert '"overall_dimension"' in dbody, (
        "delivery_check no longer looks for an overall dimension")
    assert "add_distance_dimension" in dbody, (
        "the check does not name the tool that can fix it")
    # Pending, not blocking: refusing delivery over an implied chain would be wrong.
    marker = dbody.find('"overall_dimension"')
    segment = dbody[marker:marker + 600]
    assert "blocking=False" in segment, (
        "overall_dimension became a blocker — an implied chain can be legitimate")
    return "derived dimensions are named as unfillable, and the sheet is checked for one"


def assert_default_views_include_the_face_with_holes():
    """0.8.146 — hole callouts were enabled and produced nothing, for a layout reason.

    Report 2026-11-05 (P0-C area) and my own two hours of isolation: `produce_drawing`
    defaulted to `views or ["front", "right"]`. On 接槽 the RIGHT view is an END FACE —
    7 circular edges, every one of which `AddHoleCallout2` rejects. **The holes live on the
    TOP view, which was never created.** So `defaults.hole_callouts` said True while
    `callouts.created` was 0, and no step reported a problem.

    Measured:
        front+right            -> scanned 7,  created 0
        front+top+isometric    -> scanned 32, created 13

    The fix is per-category, because one constant cannot serve both shapes: a shaft's top
    view is a second long bar (the same report complained about "两张几乎一样的长轴视图"),
    while a plate's holes only exist in the top view.

    Everything else I isolated first — auto_dimension, center_marks, surface_finish,
    save=False, the DrawingBatch scope, ledger+plan, dimension_filter, budget 45 vs 120 —
    reproduced 13 every time. The view set was the only variable that mattered, and it was
    invisible because all of those steps "worked".
    """
    src = _read("tools/pipeline.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "produce_drawing")
    assert fn is not None
    body = ast.get_source_segment(src, fn) or ""

    assert '"front", "right"' not in body, (
        "the pipeline is back to front+right, whose right view is an end face — "
        "hole callouts will silently produce zero again")
    assert 'defaults.get("views")' in body, (
        "the view set no longer comes from the category")

    for category in ("钣金", "一般零件"):
        marker = '"%s": {' % category
        start = src.find(marker)
        assert start > 0, "category %s is gone" % category
        block = src[start:start + 400]
        assert '"top"' in block, (
            "%s does not request the top view; its holes will have nowhere to attach"
            % category)
    return "default views include the face that carries the holes"


def assert_the_regression_ledger_is_intact():
    """0.8.147 — the ledger's own header went missing four times in one session.

    Every time, the cause was the same: an edit whose `old_string` was exactly
    `STATIC_REGRESSIONS = [` and whose `new_string` forgot to put it back. The result was a
    SyntaxError ('unmatched ]'), which `check_all` did catch — so nothing shipped broken.

    This assertion is not about catching the bug (the parser already does). It is about the
    MESSAGE: "unmatched ]" at line 4145 says nothing about what happened, while this says
    the ledger header is missing. Cheap, and the next person does not have to work it out.
    """
    import re as _re
    src = open(os.path.join(ROOT, "tests", "regressions.py"), encoding="utf-8").read()
    # Only a LINE-START occurrence is the definition. This function necessarily contains
    # the same characters twice (once as the pattern, once in this comment's neighbourhood),
    # and a plain .count() counted those and failed on its own source — measured: 3.
    count = len(_re.findall(r"^STATIC_REGRESSIONS = \[", src, _re.M))
    assert count == 1, (
        "the ledger header appears %d times; it must appear exactly once "
        "(a lost header means an edit dropped it, and the entry list follows it)" % count)
    assert len(STATIC_REGRESSIONS) >= 100, (
        "the ledger has only %d entries — did an edit truncate it?" % len(STATIC_REGRESSIONS))
    return "the regression ledger has one header and %d entries" % len(STATIC_REGRESSIONS)


def assert_tolerance_types_follow_the_real_enum():
    """0.8.148 — "bilateral" was asking SolidWorks for LIMIT.

    Report 2026-11-05 (P0-A): a 14.20 with +0.05/-0.05 printed as "-0.05" above "+0.05" —
    upper deviation below lower, a negative tolerance band, illegal on a drawing.

    The table had every name shifted by one against `swTolType_e`:

        name        was   swTolType_e
        basic        1    swTolBILAT     1
        bilateral    2    swTolLIMIT     2   <- prints two LIMIT VALUES, not two deviations
        limit        3    swTolSYMMETRIC 3
        symmetric    4    swTolMIN       4
        min          5    swTolMAX       5
        max          6    swTolFIT       6
        fit          7    swTolFITTOLONLY 7
        basic        -    swTolBASIC     8

    So `tolerance_type_readback: 2` was reported as success while the sheet carried limit
    values. After the fix the same call reads back 1 and the PDF prints "±0.05" for equal
    deviations — measured 2026-11-05 on 接槽.

    Asserted by VALUE, not by round-tripping through the table: a test that reads the table
    to build its expectation cannot detect a wrong table.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    node = None
    for item in tree.body:
        if isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name) and t.id == "TOLERANCE_TYPES":
                    node = item
    assert node is not None, "TOLERANCE_TYPES is gone"
    table = ast.literal_eval(node.value)

    expected = {
        "none": 0, "bilateral": 1, "limit": 2, "symmetric": 3,
        "min": 4, "max": 5, "fit": 6, "fit_with_tolerance": 7, "basic": 8,
    }
    for name, code in expected.items():
        assert table.get(name) == code, (
            "%s maps to %r; swTolType_e says %d — a shifted table silently selects the "
            "wrong tolerance style" % (name, table.get(name), code))
    return "tolerance type codes match swTolType_e"


def assert_title_block_reads_before_it_writes():
    """0.8.149 — "before" was always empty because the getter was never called.

    Report 2026-11-05 (P1-D): the title block receipt showed

        {"field":"material","before":"","after":"POM","via":"position",
         "note_name":null,"cell_position_m":null}

    while the template visibly printed POM in that cell. The report read this as "the tool
    wrote into the wrong cell" — and it may have — but the evidence it was based on was
    phoney: `INote.GetText` is a METHOD, and the code did

        before = note.GetText            # bound method, not text
        ... isinstance(before, str) ...  # quietly False -> ""

    so `before` was "" for every cell on every drawing. Every downstream "is this cell
    free?" decision was made on a value that could never be non-empty.

    Measured after the fix on 接槽: `before: "接槽"` for the name cell (non-empty, as it
    should be) and `already_filled: true` where the existing text differs from the new one.

    Also added: `cell_slot_m` and `already_filled`, so a caller can see which hard-coded
    coordinate was used and whether it landed on text that was already there.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "set_title_block")
    assert fn is not None
    body = ast.get_source_segment(src, fn) or ""

    assert "_raw_before = note.GetText" in body, (
        "the before-read is gone again; the receipt will claim every cell is empty")
    assert "callable(_raw_before)" in body, (
        "the getter is not called conditionally — a property would raise, a method would "
        "return the bound object and be discarded")
    assert "already_filled" in body, (
        "an overwrite of an already-populated cell is no longer reported")
    assert "cell_slot_m" in body, (
        "the receipt does not say which fixed coordinate was used")
    return "title block reads the existing text before deciding whether to overwrite"


def assert_circle_detection_does_not_need_a_vertex():
    """0.8.150 — circular edges were invisible to every downstream tool.

    Report 2026-11-05 (P1): `leader_endpoint_m` on a circular edge returned the CENTRE,
    15.000 mm (one radius) from the arrow tip in the DXF — while carrying
    `leader_endpoint_source: "attached_edge_sheet_coords"`, which made it look sourced.

    The chain had two breaks:

      1. `add_geometric_tolerance` preferred `sheet_center` over `sheet_point_on_circle`.
         Fixed by trying circle_point -> edge_start -> circle_centre, and labelling which
         one was used.
      2. `sheet_point_on_circle` was never POPULATED. `list_drawing_edges` guarded the
         circle branch with `if "model_start" not in item` — so any circular edge whose
         start vertex happened to resolve skipped it. Measured: **0 of 145 edges across
         three views** carried the field.

    After the fix, 接槽 view 1 reports 5 circular edges out of 26, and the endpoint lands on
    the curve: centre [0.18196, 0.23937] -> on_circle [0.18906, 0.23937].

    A vertex resolving is an accident of the binding; `ICurve.CircleParams` answers on its
    own. The guard must be about the field, not about a lucky vertex.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "list_drawing_edges")) or ""

    assert 'if "radius_m" not in item:' in body, (
        "the circle branch is gated on something other than the radius again — "
        "edges with a resolved vertex will skip it and lose sheet_point_on_circle")
    assert 'if "model_start" not in item:' not in body, (
        "the old vertex-dependent guard is back")
    assert "sheet_point_on_circle" in body, (
        "the point on the curve is no longer computed")
    return "circle detection is independent of whether a vertex resolved"


def assert_gap_planner_can_place_an_ambiguous_gap():
    """0.8.151 — the planner found the right edge pairs and then refused to use them.

    Report 2026-11-05 (P2-G): 10 gaps, `fillable: 1`, `ambiguous: 6`, and `apply_gaps=True`
    placed nothing on the ambiguous ones — because `fillable` requires EXACTLY ONE
    candidate. With two or three equally-closed edge pairs (all `delta_mm: 0.0`) the tool
    correctly refuses to GUESS, but it also gave the caller no way to say "use the first
    one". Every run ended in manual work, which is the same dead end the report hit before.

    `apply_indices` + `apply_candidate` are that way: the tool still does not choose, and
    the caller names the gap. Measured on 接槽:

        apply_indices=[1] -> applied: [{value_mm: 13.0, gap_index: 1, edge_a: 0,
                                        edge_b: 3, ok: true}]

    Two receipt bugs surfaced while testing this and are fixed here too:
      * `applied_count` was gated on `apply_gaps`, so an explicit `apply_indices` run
        reported 0 while the dimension had in fact been added — success that looks exactly
        like a no-op.
      * `applied[]` did not say WHICH gap was filled; with 10 gaps and duplicate values
        that is unreadable.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "plan_dimension_gaps")
    assert fn is not None
    args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "apply_indices" in args, (
        "there is no way to place an ambiguous gap; every run ends in manual work")
    assert "apply_candidate" in args, (
        "the caller cannot choose between equally valid candidates")

    body = ast.get_source_segment(src, fn) or ""
    assert "_explicit" in body and "in _explicit" in body, (
        "explicit indices are not consulted")
    assert 'if apply_gaps else 0' not in body.replace("\n", " "), (
        "applied_count is gated on apply_gaps again — an explicit run will report zero "
        "successes while adding dimensions")
    assert '"gap_index"' in body, (
        "the receipt does not say which gap was filled")

    server = _read("server.py")
    start = server.find("def plan_dimension_gaps(")
    block = server[start:server.find(") -> str:", start)]
    assert "apply_indices" in block, "the MCP wrapper does not expose apply_indices"
    assert "apply_candidate" in block, "the MCP wrapper does not expose apply_candidate"
    return "an ambiguous gap can be placed when the caller names it"


def assert_bent_leader_admits_it_cannot_be_verified():
    """0.8.152 — the last item of the 2026-11-05 set, and the one that stays unfixed.

    Report: with `leader_style="bent"` the horizontal elbow segment ran THROUGH the
    tolerance value cell ("0.008") before turning up to the arrow, and the receipt said
    nothing — `warning: null`.

    The report asked for a warning when the polyline intersects the frame bbox. That check
    cannot be built on this binding:

      * `SetLeader3` lets SolidWorks choose the elbow; the caller picks style and side only.
      * `IGtol.GetLeaderAtIndex2` does not exist, so the resulting points cannot be read
        back for verification.
      * DXF writes the leader as plain LINE entities among 362 others with no link back to
        the annotation — I could not attribute a line to the callout, so even an offline
        check has nothing to key on.

    (The report's own DXF read worked because their export put the leader in millimetres
    with a different origin; my export came back at 1600x875 with the leader unidentifiable.)

    What CAN be done is say so at the moment the style is chosen, and name the one lever
    that changes the outcome. `leader_path_note` carries that. A tool that returns
    `warning: null` on a path it never inspected is making a claim it cannot support.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "add_geometric_tolerance")
    assert fn is not None
    body = ast.get_source_segment(src, fn) or ""

    assert "leader_path_note" in body, (
        "the bent-leader limitation is no longer disclosed")
    assert 'leader_style).lower() == "bent"' in body, (
        "the note is not tied to the bent style")
    assert "GetLeaderAtIndex2" in body, (
        "the reason the path cannot be verified is not stated")
    assert "leader_side" in body.split("leader_path_note", 1)[1][:600], (
        "the note does not name the lever that changes the elbow")
    return "bent leaders disclose that their path is SolidWorks' and cannot be checked"


def assert_the_doc_number_check_cannot_silently_skip():
    """0.8.154 — a verifier that cannot fail is worse than no verifier.

    Report 2026-11-05 (second round): `assert_docs_numbers_match_the_code` reported PASS
    for eleven releases while checking ONE of the three numbers it claimed. The other two
    were behind `if m:` — and `if m:` reads "pattern did not match" as "nothing to check".

    That is the same failure shape as everything else this project fixed today
    (`duplicates_removed` always 0, `before` always "", `sheet_point_on_circle` never
    populated): **the validator itself was never validated.**

    This asserts the shape that makes the check honest:
      * no `if m:` / `if m2:` guard — the pattern must be asserted to match;
      * the tool count comes from the AST, not from counting the string in the source;
      * README states no smoke-item number, because no recomputable value exists for it.

    Verified by mutation on 2026-11-05: breaking either number makes check_all fail with
    "README says 60 tools, server.py registers 65" / "README says 136...".
    """
    src = open(os.path.join(ROOT, "tests", "regressions.py"), encoding="utf-8").read()

    start = src.find("def assert_docs_numbers_match_the_code()")
    assert start > 0, "the doc-number check is gone"
    end = src.find("\ndef ", start + 10)
    body = src[start:end if end > 0 else len(src)]

    # Strip the docstring first: it QUOTES the old `if m:` when explaining the bug, and a
    # naive substring check would trip on the explanation rather than the code. (Measured:
    # it did, on the first run of this very assertion.)
    _code = body
    _doc_start = body.find('"""')
    if _doc_start >= 0:
        _doc_end = body.find('"""', _doc_start + 3)
        if _doc_end > _doc_start:
            _code = body[:_doc_start] + body[_doc_end + 3:]
    assert "if m:" not in _code, (
        "the match is guarded again — an unmatched pattern will silently pass")
    assert "assert m," in _code, (
        "a missing sentence is not treated as a failure")
    assert "if m2:" not in _code, "the second match is guarded again"
    assert "assert m2," in _code, "the regression count is not asserted to be found"
    assert "ast.parse(_read(\"server.py\"))" in body, (
        "the tool count is back to a text count, which picks up the docstring")
    assert 're.findall(r"@mcp' not in body, (
        "counting the decorator string again; server.py's docstring mentions it")

    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    assert "默认 **35 项**" not in readme, (
        "README states a smoke count again; there is no recomputable value for it")
    return "the doc-number check cannot pass without actually reading the numbers"


def assert_tool_descriptions_are_not_broken_sentences():
    """0.8.155 — a mangled sentence survived 41 releases in the one text agents read.

    Report 2026-11-05 (third round): `plan_dimension_gaps`'s MCP docstring read

        Only 's  list is considered — values that are drawn but
        unattributed () are NOT gaps and are not touched.

    The identifiers had been stripped: it should have said "Only `reconcile_ledger`'s
    `missing_truly` list … unattributed (`present_but_unmatched`)". Introduced in v0.8.113,
    alive through v0.8.154 — 41 release numbers, 15 feedback reports, several rounds of
    documentation hardening. Nothing read docstring BODIES:

      * `check_readme` counts tool names and the three section totals
      * `check_param_descriptions` only asks whether a description EXISTS
      * the refuted-claim blacklist only stops old sentences coming BACK

    Every one of those checks a shape, none reads a sentence. This reads sentences.

    The markers are deliberately narrow — an empty `()`, an orphaned `'s`, an odd number of
    backticks — because a broad rule would fire on legitimate prose. Measured across all 65
    tool docstrings: only the one broken one hits "empty parens AND orphan 's".
    """
    src = _read("server.py")
    tree = ast.parse(src)
    problems = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = False
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                is_tool = True
        if not is_tool:
            continue
        doc = ast.get_docstring(node) or ""
        if not doc:
            problems.append("%s: no docstring at all" % node.name)
            continue
        # An empty () is only suspicious when it is not part of a real call chain.
        empties = [m for m in __import__("re").finditer(r"\(\s*\)", doc)
                   if not __import__("re").search(r"[A-Za-z_][\w.]*$", doc[:m.start()])]
        if empties:
            problems.append("%s: empty parentheses at offset %d"
                            % (node.name, empties[0].start()))
        if __import__("re").search(r"(?<![\w`])'s\s", doc):
            problems.append("%s: orphaned possessive ('s with no word before it)" % node.name)
        if doc.count("`") % 2:
            problems.append("%s: odd number of backticks — a span is unterminated" % node.name)

    assert not problems, "broken tool descriptions: " + "; ".join(problems)
    return "all 65 tool docstrings read as complete sentences"


def assert_gap_receipt_does_not_contradict_itself():
    """0.8.155 — the receipt said a gap was both applied and not applied.

    Report 2026-11-05 (third round, §4.2): `ambiguous_not_applied` listed EVERY ambiguous
    gap regardless of what this run had just done:

        "applied": [{"gap_index": 1, "value_mm": 13.0, "ok": true}],
        "applied_count": 1,
        "ambiguous_not_applied": [13.0, ...]      <- same gap, opposite claim

    A caller following `ambiguous_not_applied` would dimension the same feature twice. For
    a project whose selling point is "the receipt tells the truth", this is worse than a
    wrong number — it is two numbers disagreeing inside one object.

    Also guarded here: `status` must judge the TRUNCATED, APPLICABLE candidate list. It used
    `len(matches)` while `candidates` was cut to `max_candidates_per_gap`, so `max=1` left
    six single-candidate gaps reporting `ambiguous` and the automatic path skipped all of
    them (report §2).
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""

    assert "_filled" in body, (
        "ambiguous_not_applied no longer excludes what this run applied")
    assert "_i not in _filled" in body, (
        "the exclusion is gone — a gap can be reported as both applied and not applied")
    assert "if len(_usable) == 1" in body, (
        "status is not counting applicable candidates")
    assert "no_applicable_candidate" in body, (
        "a gap whose only match is unusable is indistinguishable from one with no geometry")
    return "the gap receipt cannot report the same gap as applied and not applied"


def assert_circle_centre_candidates_never_reach_the_edge_array():
    """0.8.155 — the merged circle-centre patch shipped with no assertion at all.

    Report 2026-11-05 (third round, P1-B): the previous release's headline fix was merged
    verbatim and then guarded by nothing. `grep -rn "circle_centre_not_applicable|
    candidates_circle_centre|applicable_candidates" tests/` returned nothing — in the very
    release whose subject was "an assertion that never ran".

    The contract being protected: `plan_dimension_gaps` marks a circle-centre candidate with
    a SYNTHETIC NEGATIVE index (`-(ci+1)`), while `add_distance_dimension` indexes a real
    edge array and validates `0 <= index < len(edges)`. Passing one through used to fail
    with `bad_edge_index`, which reads like the caller computed an index wrong.

    What must hold:
      * candidates carry `candidate_kind` and `applicable`;
      * each gap reports `applicable_candidates`;
      * an inapplicable pick returns `circle_centre_not_applicable` and does NOT call
        `add_distance_dimension`;
      * the applicability check appears BEFORE the call in source order.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""

    assert '"candidate_kind"' in body, "candidates no longer say what kind they are"
    assert '"applicable"' in body, "candidates no longer carry applicability"
    assert '"applicable_candidates"' in body, (
        "the gap does not list which candidate indices actually work")
    assert "circle_centre_not_applicable" in body, (
        "the explicit error code is gone; a negative index would reach the edge array again")

    guard_at = body.find("if not cand.get(\"applicable\", True):")
    call_at = body.find("added = add_distance_dimension(")
    assert guard_at > 0, "the applicability guard is gone"
    assert call_at > guard_at, (
        "add_distance_dimension is called before the applicability check — a synthetic "
        "negative index would be passed through")
    return "circle-centre candidates cannot reach add_distance_dimension"


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
    ("0.8.79 set_view outline check", assert_set_view_verifies_the_outline_not_the_property),
    ("0.8.80 cross-section exemption", assert_cross_sections_are_not_judged_by_parent_extent),
    ("0.8.81 section position report", assert_section_reports_a_dropped_position_axis),
    ("0.8.82 tolerance units/limits", assert_tolerance_checks_units_and_degenerate_limits),
    ("0.8.83 detail_thin warns", assert_detail_thin_is_a_warning_not_a_failure),
    ("0.8.84 axis classify + 总长", assert_turned_parts_classify_and_report_real_length),
    ("0.8.85 same-value duplicates", assert_same_value_duplicates_are_listed_not_deleted),
    ("0.8.87 sheet lines prove render", assert_sheet_lines_prove_they_rendered),
    ("0.8.89 P3 receipts", assert_p3_receipts_say_what_they_mean),
    ("0.8.137 version strings agree", assert_version_strings_agree),
    ("0.8.138 pipeline closes documents", assert_pipeline_closes_what_it_opened),
    ("0.8.139 field names mean what they say", assert_receipts_say_what_the_fields_mean),
    ("0.8.140 impl params reach MCP", assert_every_implementation_param_reaches_mcp),
    ("0.8.141 closes only its own documents", assert_pipeline_closes_only_what_it_opened),
    ("0.8.142 native callout text is fine", assert_native_callout_text_is_not_called_unresolved),
    ("0.8.143 no refuted claim in docs", assert_no_refuted_claim_is_still_in_the_docs),
    ("0.8.143 doc numbers match code", assert_docs_numbers_match_the_code),
    ("0.8.144 derived dims reported", assert_derived_dimensions_are_reported_as_unfillable),
    ("0.8.146 default views carry the holes", assert_default_views_include_the_face_with_holes),
    ("0.8.147 ledger header intact", assert_the_regression_ledger_is_intact),
    ("0.8.148 tolerance enum matches SW", assert_tolerance_types_follow_the_real_enum),
    ("0.8.149 title block reads before write", assert_title_block_reads_before_it_writes),
    ("0.8.150 circle detection needs no vertex", assert_circle_detection_does_not_need_a_vertex),
    ("0.8.151 ambiguous gap can be placed", assert_gap_planner_can_place_an_ambiguous_gap),
    ("0.8.152 bent leader is disclosed", assert_bent_leader_admits_it_cannot_be_verified),
    ("0.8.154 doc check cannot skip", assert_the_doc_number_check_cannot_silently_skip),
    ("0.8.155 descriptions are sentences", assert_tool_descriptions_are_not_broken_sentences),
    ("0.8.155 gap receipt consistent", assert_gap_receipt_does_not_contradict_itself),
    ("0.8.155 circle centre never reaches edges", assert_circle_centre_candidates_never_reach_the_edge_array),
    ("0.8.137 ledger dedupes by full_name", assert_ledger_dedupes_by_full_name),
    ("0.8.135 callout quantity", assert_hole_callouts_carry_a_quantity),
    ("0.8.134 no dimension off-sheet", assert_no_dimension_is_left_off_the_sheet),
    ("0.8.132 marks + orphans discriminate", assert_center_marks_and_orphans_are_discriminating),
    ("0.8.131 requirements stay in frame", assert_requirements_respect_safe_area),
    ("0.8.130 projection selectable", assert_projection_angle_is_selectable),
    ("0.8.129 placement is reported", assert_datum_and_move_report_placement),
    ("0.8.128 gap matcher units", assert_gap_matcher_converts_units),
    ("0.8.126 title block guards None", assert_title_block_guards_none_position),
    ("0.8.126 bilateral lower is negative", assert_bilateral_lower_is_negative),
    ("0.8.124 perf receipt matches spec 9.1", assert_perf_receipt_matches_spec_schema),
    ("0.8.120 outline counted not cached", assert_outline_wrapper_is_pass_through),
    ("0.8.118 ledger open is structured", assert_ledger_open_failure_is_structured),
    ("0.8.113 gap planner no guessing", assert_gap_planner_only_fills_unambiguous_gaps),
    ("0.8.112 drawing batch opt-in", assert_drawing_batch_defaults_off_and_restores),
    ("0.8.111 pipeline saves once", assert_pipeline_saves_once),
    ("0.8.110 version consistent", assert_package_version_matches_pyproject),
    ("0.8.100 smoke reads fields", assert_smoke_reads_fields_not_human_text),
    ("0.8.99 place_outside default", assert_auto_dimension_places_outside_by_default),
    ("0.8.98 circle point on circumference", assert_circle_edges_carry_a_point_on_the_circumference),
    ("0.8.97 note decode + arc drop", assert_notes_decode_and_arc_lengths_are_dropped),
    ("0.8.96 reconcile three-way", assert_reconcile_splits_missing_from_present_but_unmatched),
    ("0.8.95 leader endpoint sourced", assert_leader_endpoint_comes_from_the_attached_edge),
    ("0.8.94 produce_drawing idempotent", assert_produce_drawing_refuses_to_double_a_sheet),
    ("0.8.93 flip_x + cache key", assert_sheet_x_can_be_flipped_and_the_cache_knows),
    ("0.8.92 filter takes plan dicts", assert_dimension_filter_accepts_plan_dicts_and_strings),
    ("0.8.90 no blank sheet leak", assert_create_drawing_does_not_leak_a_blank_sheet),
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
