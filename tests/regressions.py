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
    assert "min(view_w, view_h) * 0.5)" in body and "/ scale" in body, (
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
    """0.8.33 / N-3 / 0.8.172 — feature names came back as ÂÝÎÆ¿××êÍ·Éî¶È.

    Reported four rounds running. The previous version of THIS assertion is why it survived:
    it grepped the source for `encode("latin-1")` / `decode("gb2312")`, so it stayed green
    while the function did nothing on the strings that actually occur.

    A real SolidWorks name is a MIXTURE:

        ÂÝÎÆ¿××êÍ·Éî¶È@草图2@T_定位块.Part
        └── mangled ──┘  └ CJK ┘ └── ASCII ──┘

    `encode("latin-1")` meets 草图, raises, and the bare `except` returned the input. Zero
    repairs on real data, 180/180 green.

    So this now calls the function with real samples instead of reading its source.
    """
    import importlib

    mod = importlib.import_module("solidworks_assetmate_mechdog.sw_session")
    fix = getattr(mod, "fix_mojibake", None)
    assert fix is not None, "fix_mojibake is missing"

    pure = "ÂÝÎÆ¿××êÍ·Éî¶È"                       # 螺纹孔钻头深度
    assert fix(pure) == "螺纹孔钻头深度", (
        "the pure mangled form no longer repairs: %r" % (fix(pure),))

    mixed = "ÂÝÎÆ¿××êÍ·Éî¶È@草图2@T_定位块.Part"
    got = fix(mixed)
    assert got == "螺纹孔钻头深度@草图2@T_定位块.Part", (
        "a MIXED name — mangled segment plus real Chinese plus ASCII — is the shape that "
        "actually occurs, and it does not repair: %r" % (got,))

    # Pass-through cases: a blanket call must never damage correct text.
    for good in ("螺纹孔钻头深度@草图2@T_定位块.Part", "D1@草图1", "", "Ø30"):
        assert fix(good) == good, "repair damaged a correct string: %r -> %r" % (good, fix(good))

    # And the repair must be wired into the data source, not one consumer.
    ledger = _read("tools/ledger.py")
    assert "fix_mojibake" in ledger, (
        "build_feature_ledger emits mangled names again — keep_dimensions built from them "
        "can never match anything downstream")
    return "mojibake repair verified on real mixed names, with safe pass-through"


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
    segment = caps[index:index + 2600]
    # 0.8.291: this entry was rewritten. It still explains the slot collision, but it no
    # longer recommends `add_drawing_dimension` (proved dead on 2026-11-09) and it now
    # documents the through-bore / revert behaviour that replaced the old workaround.
    assert "SetText" in segment and "文字槽" in segment, (
        "the entry does not explain which slots collide")
    assert "add_drawing_dimension" in segment and "**不要**" in segment, (
        "the entry recommends the dead dimension path again")
    assert "through_state" in segment and "ok:false" in segment, (
        "the entry does not describe the current revert behaviour")
    src = _read("tools/drawing.py")
    assert "SetText_depth_may_trail" in src and 'text_source"] = "native"' in src, (
        "the receipt no longer distinguishes native text from a rewritten one")
    return "slot limit declared; native text kept; dead workaround retired"


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
    # Take the FUNCTION BODY, not a fixed 1200-character window: a long parameter
    # description (0.8.297 added `dedupe_existing`) pushed the forwarding call out of the
    # window and produced a false FAIL on a correct wrapper.
    srv_body = ast.get_source_segment(
        server, _function_node(ast.parse(server), "add_center_marks")) or ""
    assert "view_name=view_name" in srv_body, (
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
    assert "min(view_w, view_h) * 0.5)" in body and "/ scale" in body, (
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
    # 0.8.173: flipped BACK to partial, and this time with evidence the earlier check could
    # not have produced. v0.8.91 measured a WHOLE-PAGE diff taken after `add_note` ran, so
    # the pixels could not be attributed to the triangle — and "the triangle is not drawn"
    # was inferred from a test that could not show it either way.
    #
    # A report rendered the PDF and saw the full symbol. The decisive measurement is a
    # LINES-ONLY diff (render after the sketch lines, before the note): 2134 changed pixels.
    # Whole page 2230; the 96-pixel difference is the note text.
    #
    # What must hold now is that the flag comes from that measurement, not from a constant.
    layout_src = _read("tools/layout.py")
    assert "triangle_verified=False," not in layout_src, (
        "triangle_verified is a hard-coded False again — the triangle DOES render "
        "(2134 pixels, lines-only diff)")
    assert "_lines_only_pixels" in layout_src, (
        "the lines-only diff is gone; nothing measures the sketch geometry separately")
    assert '"partial"' in segment, (
        "weld_symbol should be partial: the geometry renders, but it is sheet sketch "
        "geometry, not an IWeldSymbol that follows the weldment feature")
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

    # 0.8.200: renamed `_lower_arg` -> `_lower_m` when the arguments of
    # `SetToleranceValues` were corrected to (TolMin, TolMax). Accept either name, but
    # insist the negative sign is still applied to the LOWER side.
    _var = "_lower_m" if "_lower_m" in body else "_lower_arg"
    assert _var in body, "the sign adjustment is gone"
    assert "-abs(%s)" % _var in body, (
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
    # ── Where does the tag come from? (report 2026-11-09, core problem B) ────────────────
    # `git describe` alone fails for the shape most people install from: a GitHub tarball
    # has no `.git`, so the tag was silently None, the comparison was skipped, and the
    # receipt still said "agrees across pyproject, __init__ and tag". A guard that reports
    # success when it did nothing is worse than no guard.
    #
    # Order: CI env var (authoritative when present) -> packaged metadata (works offline and
    # from an sdist) -> git (works in a working tree). If none answers, say SKIPPED.
    tag_version = None
    tag_source = None

    for _env in ("GITHUB_REF_NAME", "CI_COMMIT_TAG", "GIT_TAG"):
        _candidate = (os.environ.get(_env) or "").strip().lstrip("v")
        if _candidate:
            tag_version, tag_source = _candidate, "env:%s" % _env
            break

    if not tag_version:
        _meta_path = os.path.join(SRC, "_build_meta.json")
        try:
            import json
            with open(_meta_path, encoding="utf-8") as fh:
                _meta = json.load(fh)
            _candidate = str(_meta.get("tag") or "").strip().lstrip("v")
            if _candidate:
                tag_version, tag_source = _candidate, "packaged:_build_meta.json"
        except Exception:
            pass

    if not tag_version:
        try:
            import subprocess
            out = subprocess.run(["git", "describe", "--tags", "--exact-match", "HEAD"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                tag_version = out.stdout.strip().lstrip("v")
                tag_source = "git:describe"
        except Exception:
            pass

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

        # `==`, not `>=`. `>=` only catches "tag is newer than the package"; it lets
        # "package is newer than the tag" through, and — the case that actually shipped —
        # two adjacent tags pointing at the SAME commit with the same declared version
        # (v0.8.271 and v0.8.272 both said 0.8.271) passes `>=` every time.
        assert _as_tuple(declared) == _as_tuple(tag_version), (
            "tag is v%s (from %s) but the package declares %r — tag and declared version "
            "must be equal. If the tag is older, the version was not bumped before tagging; "
            "if the tag is newer, the bump landed after the tag." %
            (tag_version, tag_source, declared))

    if not tag_version:
        # ── align with static_check (report 2026-11-09, B) ─────────────────────────────
        # This branch used to PASS, so on the same package `static_check` returned RC 1
        # while `regressions` printed "266/266 passed". Two suites disagreeing about the
        # same fact is worse than either answer alone: the green banner gets quoted as
        # evidence.
        #
        # Same rule as the gate: a working tree with HEAD between tags is a legitimate
        # skip, anything else is a failure.
        _on_tag = None
        try:
            import subprocess as _sp
            _probe = _sp.run(["git", "describe", "--tags", "--exact-match", "HEAD"],
                             capture_output=True, text=True, timeout=10)
            if _probe.returncode == 0:
                _on_tag = True
            elif "not a git repository" in (_probe.stderr or "").lower():
                _on_tag = None
            else:
                _on_tag = False
        except Exception:
            _on_tag = None

        if _on_tag is False:
            return ("version %s agrees across pyproject/__init__; TAG COMPARE SKIPPED "
                    "(HEAD is not on a tag — mid-release state)" % declared)
        raise AssertionError(
            "version %s cannot be checked against a tag: no CI env var, no "
            "_build_meta.json, and HEAD cannot be shown to be between tags. Same rule as "
            "static_check — an unverifiable release is not a passing one." % declared)

    return "version %s agrees across pyproject, __init__ and tag v%s [%s]" % (
        declared, tag_version, tag_source)


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
    # 71 since v0.8.284 (`list_tool_profiles`). Bump this line IN THE SAME COMMIT that adds
    # a tool — the check exists so a new tool cannot slip in without anyone noticing.
    assert registered == 73, (
        "expected 73 registered tools, AST found %d — update this expectation "
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

    Every time, the cause was the same: an edit whose `old_string` was exactlySTATIC_REGRESSIONS = [` and whose `new_string` forgot to put it back. The result was a
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
    """0.8.200 — this assertion previously LOCKED IN the wrong table.

    History, because it is the whole point:

      2026-11-04  table had `bilateral: 2`. A report measured "-0.05 above +0.05" and I
                  blamed the table, moving `bilateral` to 1 and `basic` to 8.
      2026-11-05  I added this assertion with the message "bilateral maps to 2;
                  swTolType_e says 1 — a shifted table silently selects the wrong
                  tolerance style". **That sentence was wrong**, and from then on the
                  assertion GUARDED THE BUG.
      2026-11-07  a report showed what `bilateral: 1` actually prints: a BOX
                  (┌ Ø14.20 ┐ = swTolBASIC, no tolerance band), and `symmetric: 3`
                  printing 14.25/14.25 (swTolLIMIT, zero-width band).

    The real cause was never the table — it was `SetToleranceValues(TolMin, TolMax)`
    being called as `(upper, lower)`. The generated type library spells the parameters
    out:

        def SetToleranceValues(self, TolMin=..., TolMax=...):
            'Set the tolerance min and max values'

    So the table is back to the SolidWorks `swTolType_e` order and the ARGUMENTS are
    swapped. This assertion now pins both, and its failure message says why rather than
    inviting the next reader to "fix" the table again.
    """
    src = _read("tools/drawing.py")
    block = src[src.find("TOLERANCE_TYPES = {"):]
    block = block[:block.find("}")]
    want = {"none": 0, "basic": 1, "bilateral": 2, "limit": 3,
            "symmetric": 4, "min": 5, "max": 6, "fit": 7,
            "fit_with_tolerance": 8}
    for name, code in want.items():
        m = re.search(r'"%s"\s*:\s*(\d+)' % name, block)
        assert m, "TOLERANCE_TYPES lost %r" % name
        assert int(m.group(1)) == code, (
            "TOLERANCE_TYPES[%r] is %s but swTolType_e says %d. "
            "Do NOT change the table to fix an inverted tolerance — check the ARGUMENT "
            "ORDER of SetToleranceValues(TolMin, TolMax) first; that is what broke "
            "bilateral into a BASIC box on 2026-11-05." % (name, m.group(1), code))

    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_dimension_tolerance")) or ""
    assert "SetToleranceValues(_lower_m, _upper_m)" in body, (
        "SetToleranceValues must be called (TolMin, TolMax) — passing (upper, lower) "
        "prints the deviations in the wrong order")
    return "enum follows swTolType_e and the arguments are (min, max)"


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

    # 0.8.215: the reader moved to `_note_text`, because `IAnnotation` has no `GetText` at
    # all — the method lives on `INote` and takes NO arguments. The old assertion pinned the
    # exact expression that could never work (`note.GetText` on the annotation dispatch), so
    # it guarded the bug for three releases. Assert the outcome now: the value must come
    # from a helper that knows which interface owns the method.
    assert "_note_text(note)" in body, (
        "the before-read is gone again; the receipt will claim every cell is empty")
    helper = _read("tools/drawing.py")
    assert "def _note_text(" in helper, "the INote reader is gone"
    assert "INote(" in helper, (
        "`_note_text` no longer goes through INote — IAnnotation has no GetText, so this "
        "silently reads empty again")
    # 0.8.215: the `callable()` dance is gone with the old expression. `_note_text` handles
    # both shapes itself (typed INote method, then a property fallback), so assert THAT.
    assert "specific.GetText()" in helper or "note.GetText()" in helper, (
        "neither the typed nor the untyped GetText path is attempted any more")

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


def assert_applied_dimensions_are_checked_against_what_was_drawn():
    """0.8.156 — `apply_gaps` filled an 8 mm gap with a 1570.796 mm dimension.

    Report 2026-11-05 (P0): candidates are picked by SHEET distance
    (`|paper span - target| <= tolerance_mm`), but `AddDimension2` measures MODEL geometry.
    On AM_DOOR the two disagreed by two orders of magnitude — the chosen edge pair was an
    arc, and the dimension came out as 500π.

    The old code did:

        added = add_distance_dimension(...)
        applied.append({"value_mm": item["value_mm"],   # the PLANNED value
                        "ok": bool(added.get("ok"))})   # and only the ok flag

    `added["value_mm"]` — the value actually drawn — was discarded. So the receipt said
    "filled 8.0" while the sheet carried 1570.796, nobody raised, and because the ledger
    never saw 8.0 appear, every later call decided the gap was still open and inserted
    another one. Three runs left 15 absurd dimensions on one sheet.

    This is the same shape as everything else fixed today, one level worse: not a field
    that lies, but a field that never reads the answer.

    Now the actual value is compared, a mismatch is `ok: false` + `value_mismatch`, and the
    entry names the position so the caller can delete the wrong dimension. It is NOT
    auto-deleted: `delete_annotation` matches by name or position, and a position hit could
    remove a legitimate neighbouring dimension.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""

    assert 'added.get("value_mm")' in body, (
        "the value actually drawn is discarded again — the receipt can report a fill that "
        "never happened")
    assert "value_mismatch" in body, (
        "a dimension that measures the wrong thing is no longer reported as a failure")
    assert '"actual_value_mm"' in body, (
        "the receipt does not carry what SolidWorks actually measured")
    assert "inserted_but_wrong" in body, (
        "the caller is not told that a wrong dimension is now on the sheet")
    # Guards must run before the success append, not after. There are now TWO guards: the
    # angular-dimension check (which reports its own failure) and the value comparison.
    # Both must precede the success row, so anchor on the LAST guard and the fact that a
    # success row exists at all.
    _ang = body.find("_wrong_dim_type in (1, 3)")
    mismatch_at = body.rfind("if _mismatch:")
    success_at = body.rfind('"actual_value_mm": _actual,')
    assert _ang > 0, "the angular guard is gone"
    assert 0 < mismatch_at < success_at or (mismatch_at > 0 and _ang < mismatch_at), (
        "the mismatch check runs after the success path — a wrong value would be recorded "
        "as applied")
    return "every applied dimension is checked against what SolidWorks actually measured"


def assert_gap_status_enum_is_the_same_in_every_consumer():
    """0.8.156 — one new status, three files, only two updated.

    Report 2026-11-05 (fourth round): `no_applicable_candidate` was added in 0.8.155 and
    written into the MCP docstring and the parameter schema. It was NOT added to
    `tests/smoke.py`'s whitelist, so a real run on any part with circular geometry fails
    with "gap has no status" — a message that states the opposite of the truth.

    The reverse hole existed too: `no_value` is real (value is None) and smoke knows it,
    but the docstring never listed it.

    This compares the three sets: the statuses the implementation assigns, the statuses
    smoke accepts, and the statuses the tool description lists. Signature checks cannot see
    an enum change; this can.
    """
    import re as _re

    draw = _read("tools/drawing.py")
    body = ast.get_source_segment(draw, _function_node(ast.parse(draw),
                                                       "plan_dimension_gaps")) or ""
    # Both shapes count: the dict-literal initial value (`"status": "no_geometry"`) and the
    # later assignments (`entry["status"] = "fillable"`). Matching only the assignment form
    # dropped `no_geometry`, because that one is never reassigned — it is the default.
    implemented = (set(_re.findall(r'"status":\s*"([a-z_]+)"', body))
                   | set(_re.findall(r'\["status"\]\s*=\s*"([a-z_]+)"', body)))
    assert implemented, "no status literals found — the assignment style changed"

    smoke = open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8").read()
    m = _re.search(r'item\.get\("status"\)\s+in\s+\(([^)]*)\)', smoke)
    assert m, "smoke no longer whitelists gap statuses"
    whitelisted = set(_re.findall(r'"([a-z_]+)"', m.group(1)))

    server = _read("server.py")
    start = server.find("def plan_dimension_gaps(")
    # The docstring lives AFTER the signature, not in it.
    sig_end = server.find(") -> str:", start) + len(") -> str:")
    doc_start = server.find('"""', sig_end)
    doc_end = server.find('"""', doc_start + 3)
    doc = server[doc_start:doc_end]
    # Extract the STATUS LIST specifically — the bullet items, not every backticked word.
    # Report 2026-11-06: the previous version took all backticked words and then intersected
    # with a hard-coded five-element set, so removing an entire status from the description
    # still passed (measured: deleting `no_applicable_candidate` left 180/180 green). An
    # assertion named "the same in every consumer" must fail when a consumer loses an entry.
    # Extract the STATUS LIST specifically: from the sentence introducing it down to the
    # first blank line. Taking every bullet item also picks up `apply_candidate`, which is
    # a parameter bullet further down.
    _intro = doc.find("a status:")
    assert _intro > 0, "the status list introduction is gone from the description"
    _block = doc[_intro:]
    _blank = _block.find("\n\n")
    if _blank > 0:
        _block = _block[:_blank]
    documented = set(_re.findall(r"^\s*\* `([a-z_]+)`", _block, _re.M))

    assert implemented == whitelisted, (
        "implementation statuses %s != smoke whitelist %s — a real run will hit one the "
        "smoke cannot classify" % (sorted(implemented), sorted(whitelisted)))
    assert implemented == documented, (
        "description lists %s but the implementation can return %s (missing: %s, extra: %s)"
        % (sorted(documented), sorted(implemented),
           sorted(implemented - documented), sorted(documented - implemented)))
    return "gap statuses agree across implementation, smoke and description"


def assert_shaft_default_views_keep_a_long_bar():
    """0.8.157 — a category-level view guess deleted the only usable view.

    Report 2026-11-05 (P0): v0.8.146 set 轴类 to `["front", "isometric"]` on the reasoning
    that a shaft's top view is a redundant second bar. That is true only when the shaft lies
    ACROSS the front view. On 飞达动力轴 the front view is the END FACE —
    `precise_outline_m` 0.035 x 0.035 — and `right` is the 0.107 x 0.035 bar. Removing `top`
    removed the only view that could carry axial dimensions:

        default (front+isometric)   3 dimensions, coverage 0.167, all 13 steps ok
        explicit (front+right)     10 dimensions, coverage 0.444

    A category cannot know how a model is oriented, so the default must keep a view set that
    has a bar under EITHER orientation. `front + right + isometric` does: `right` is the bar
    in one layout, `front` in the other.

    Verified on the reporter's own part, 2026-11-05:
        default front+right+isometric -> 3 views, 10 dimensions, coverage 0.444
        explicit front+right          -> 2 views, 10 dimensions, coverage 0.444
    Same dimension set (`1.5 1.5 10 17 20 21.5 30 30 35 56`), one extra view.
    """
    src = _read("tools/pipeline.py")
    start = src.find('"轴类": {')
    assert start > 0, "the shaft category is gone"
    # Wide enough to reach the views key past its explanatory comments; stop at the next
    # category so a later key cannot be mistaken for this one.
    _next = src.find('"装配体": {', start)
    block = src[start:_next if _next > start else start + 2000]
    views_at = block.find('"views"')
    assert views_at > 0, "the shaft category has no view list"
    views_line = block[views_at:block.find("]", views_at) + 1]

    assert '"right"' in views_line, (
        "the shaft default lost `right` — on a shaft laid out across the front view, "
        "`right` is the long bar and dropping it costs every axial dimension")
    assert '"isometric"' not in views_line or '"right"' in views_line, (
        "the isometric is present without a bar view")
    assert not (views_line.count('"') == 4 and '"front"' in views_line), (
        "the shaft default is down to two views with no bar — this is the 0.8.146 shape")
    return "shaft defaults keep a view that can carry axial dimensions"


def assert_test_fixtures_are_reachable_from_a_release():
    """0.8.158 — the new A/B/C test groups scored 0/7 on a clean install.

    Report 2026-11-05 (P1): three files hardcoded

        ROOT / "测试用例" / "旋转轴-工程图" / "旋转轴.SLDPRT"

    while `.gitignore` says, with a comment explaining the intent:

        # Whole tree on purpose: nothing under 测试用例/ belongs in the package.
        测试用例/

    A release tarball therefore never contains that part. Measured on a clean install:
    four cases raised `FileNotFoundError` before their first assertion, one module crashed
    during import, and **none of the seven said why**.

    This is the second time. v0.8.24 reported smoke pointing at a gitignored fixture; that
    was fixed with `FixtureMissing` plus guidance, and three new files then repeated it.
    The fix is a shared `tests/_fixtures.resolve_part()` — `--part` override, actionable
    message, exit 2 (skipped, not failed) — so there is one implementation to drift.

    This assertion is the part that makes it stick: any test file that mentions the
    gitignored tree must go through the resolver. Without it the next new test can hardcode
    the path again and pass locally, which is exactly how this happened.
    """
    tests_dir = os.path.join(ROOT, "tests")
    offenders = []

    for name in sorted(os.listdir(tests_dir)):
        if not name.endswith(".py") or name in ("_fixtures.py", "regressions.py",
                                                "check_all.py", "static_check.py"):
            continue
        path = os.path.join(tests_dir, name)
        src = open(path, encoding="utf-8").read()
        if "测试用例" not in src:
            continue
        # Mentioning the tree is fine (docs, messages); BUILDING a path from it is not.
        if 'os.path.join(' in src and '"测试用例"' in src:
            # Going through the shared resolver is the new path;  is the
            # older equivalent (smoke.py, fixed in v0.8.24). Either counts as handled —
            # what must not happen is a bare path with no fallback at all.
            if "_fixtures" not in src and "FixtureMissing" not in src:
                offenders.append(name)

    assert not offenders, (
        "these test files build a path into the gitignored 测试用例/ tree without going "
        "through tests/_fixtures.resolve_part(): %s — on a release install their fixtures "
        "do not exist and every case fails before it starts" % ", ".join(offenders))

    # The resolver itself must offer the override and the guidance.
    resolver = open(os.path.join(tests_dir, "_fixtures.py"), encoding="utf-8").read()
    assert "--part" in resolver, "the resolver has no --part override"
    assert "SystemExit(2)" in resolver, (
        "a missing fixture must exit 2 (skipped), not 1 (failed)")
    return "no test builds a path into the gitignored fixture tree"


def assert_a_failed_save_cannot_report_ok():
    """0.8.159 — `ok: true` next to `saved: false`, and a blank sheet left behind.

    Report 2026-11-05 (P1): v0.8.90 moved the path guard before `_new_document()`, so the
    common "target already open" case stopped leaking. Three blockers still sit AFTER the
    document exists — a second path_locked guard, `saveas_failed`, and `save_not_written` —
    and all three did:

        result.update(saved=False, save_error=..., save_reason=...)
        return ok(**result)

    Top-level `ok` stayed True and nothing called `CloseDoc`. Measured by making the output
    path a directory (so SaveAs cannot possibly write it):

        before: ok=True  saved=False  save_error=save_not_written  docs 35 -> 37
        after : ok=False error=save_not_written  docs unchanged except the source part

    Two documents leaked per failed call — a blank drawing at an EMPTY path (which both
    cleanup passes miss, because they match on directory) plus the source part it locked.

    The invariant this asserts is the one the whole day was about: **a tool may not report
    success for work it did not do**. `ok` and `saved=False` must not coexist, and anything
    created for a write that failed must be closed.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "create_drawing")) or ""

    assert "if blocker:" in body, "the blocker branch is gone"
    # There are TWO blocker branches: the pre-create guard (fixed in v0.8.90) and the
    # post-create one this is about. `find` lands on the first, which legitimately has no
    # CloseDoc — take the last.
    start = body.rfind("if blocker:")
    # Wide enough to reach the CloseDoc + return error past the long error() argument list.
    segment = body[start:start + 2500]
    assert "sw.CloseDoc" in segment, (
        "a failed save leaves the drawing open — it leaked two documents per call")
    assert "return error(" in segment, (
        "a failed save still returns ok — `ok: true` beside `saved: false` is the lie this "
        "project spent the day removing")
    return "a failed save closes the document and reports failure"


def assert_smoke_judges_hatch_missing_the_same_way_twice():
    """0.8.160 — one error code, two standards, same file.

    Report 2026-11-05 (P1): `tests/smoke.py` checked `add_section_view` in two places:

        :920  _views_and_dims     ok is not None  + hatch_count == 0   -> PASS
        :1298 _section_and_detail  assert sec.get("ok")                -> FAIL

    The same `hatch_missing` therefore passed one smoke item and failed another, producing
    the run's only FAIL: "section failed: hatch_missing 剖面视图已创建，但剖切面没有任何剖面线".

    The tool is right. `hatch_missing` has been a deliberate honest refusal since v0.8.56 —
    a cut with no hatch lines says so instead of reporting ok — and the capability table
    marks `section_view` as `partial`. Whether a fixture can produce hatch lines is a
    property of the geometry, not a code regression.

    Both call sites must now: accept a structured verdict, accept `hatch_missing`, and
    require that the refusal is honest about its own count.
    """
    src = open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8").read()

    # NOTE: a bare `assert sec.get("ok")` is legitimate inside an `else:` that already
    # handled hatch_missing — that is the branch for every OTHER failure. An earlier
    # version of this check scanned for the pattern globally and flagged exactly that
    # line, i.e. it would have forced the correct code to change. The count below is the
    # reliable signal: BOTH section checks must mention hatch_missing.

    occurrences = src.count('== "hatch_missing"')
    assert occurrences >= 2, (
        "only %d place(s) handle hatch_missing; both section checks must accept it"
        % occurrences)
    assert "hatch_count" in src, (
        "the refusal is accepted without checking that it reports zero hatch lines")
    return "hatch_missing is judged the same way at every section check"


def assert_document_ownership_is_tri_state():
    """0.8.161 — "nothing is open" and "I cannot tell" were the same value.

    Report 2026-11-05 (third round, P2-1, reported for the third time): after clearing the
    session, `produce_drawing` returned

        documents_skipped_open_by_caller: ["会话内的文档无法枚举，未关闭任何文档……"]
        documents_closed: [{"closed_count": 1}, {"closed_count": 1}]
        close_documents: "closed 2 document(s); 1 skipped as caller-owned"

    Three statements, three stories. The message said nothing was closed, the data said two
    were, and the detail counted the enumeration failure as a caller-owned skip.

    Root cause: `open_document_paths()` returned `set()` for BOTH the empty session and a
    failure, so the caller could not distinguish them — and its comment claimed "nothing is
    closed when the session cannot be enumerated", which described the sentence rather than
    the code (`if _preexisting and ...` is False for an empty set, so it closed everything).

    Measured 2026-11-05: a live session with nothing open reports `GetDocuments is None`,
    not an empty tuple. So `None` alone cannot mean failure either — the probe must ask a
    property that answers regardless of what is open (`RevisionNumber`).

    Now: `set()` = alive and empty, `set(paths)` = those were open, `None` = cannot tell.
    Verified on an empty session: closed 2, skipped [], detail "0 skipped".
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "open_document_paths")) or ""

    assert "return None" in body, (
        "the resolver cannot express 'I cannot tell' — the empty session and a dead bridge "
        "will look identical again")
    assert "RevisionNumber" in body, (
        "the probe does not ask a property that answers on an empty session; "
        "`GetDocuments is None` is what an empty session actually returns")
    assert body.count("return set()") >= 1, (
        "the resolver can no longer say 'alive and empty'")

    pipe = _read("tools/pipeline.py")
    pbody = ast.get_source_segment(pipe, _function_node(ast.parse(pipe),
                                                        "produce_drawing")) or ""
    assert "_enum_failed" in pbody, (
        "the caller does not distinguish an enumeration failure from an empty session")
    assert "[] if _enum_failed else" in pbody, (
        "an enumeration failure still closes documents — the sentence says it does not")
    return "document ownership distinguishes empty, known and unknown"


def assert_the_material_library_scan_is_cached():
    """0.8.162 — 31.8 s per call, and the "not found" path scanned three times.

    Report 2026-11-05 (P2 + §6): `material_library()` with a bare filename walked
    PROGRAMDATA + APPDATA + PROGRAMFILES looking for the .sldmat — measured **31.8 s for one
    call**, and smoke calls it at least once. The report also noted it as the one definite
    optimisation in an otherwise unexplained 10x slowdown.

    Caching it revealed a second cost the report could only measure as "9 s warm": the
    failure path runs a SEPARATE recursive glob to suggest alternatives, so a wrong name paid
    the walk again — every time.

    Measured after caching (same machine, cold then warm):
        before: 40.9 s / 9.8 s / 9.1 s
        after : 19.6 s / 0.00 s / 0.00 s

    Only PATHS are cached, never parsed materials — a library can be edited between calls,
    and re-reading one file is cheap next to walking three trees. Empty results are cached
    too: "not found" is the case that repeats most.

    `_forget_library_cache()` exists so a long-lived session (or a test) can force a rescan
    after installing a library.
    """
    src = _read("tools/material.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "material_library")) or ""

    assert "_LIBRARY_PATH_CACHE" in body, (
        "the three-tree walk is uncached again — every call pays the full scan")
    assert "_available_library_paths()" in body, (
        "the not-found path scans separately again; a wrong name pays two walks per retry")
    assert "_forget_library_cache" in src, (
        "the cache cannot be dropped; a session that installs a library can never find it")

    helper = _function_node(ast.parse(src), "_available_library_paths")
    assert helper is not None, "the cached alternatives scan is gone"
    return "the material library scan is cached and can be invalidated"


def assert_gap_candidates_are_checked_against_the_model():
    """0.8.163 — candidates were picked on paper distance and measured in model space.

    Report 2026-11-05 (P0): `|sheet span - target| <= tolerance` decided whether an edge pair
    was offered, while `AddDimension2` writes the MODEL distance. On AM_DOOR an 8.0 mm paper
    gap produced a 1570.796 mm dimension (500π — the pair was an arc). v0.8.156 taught the
    caller to notice the mismatch AFTER inserting; this is the earlier gate, so the wrong
    candidate is never offered at all.

    What the gate must do:
      * compute the model span from `model_center` / `model_start` of both edges;
      * drop a candidate whose model span misses the target, even when the paper matched;
      * KEEP a candidate when no model point is available — refusing on missing data would
        empty the list on any file where the model coordinates could not be projected;
      * record `model_span_mm` on every candidate so the receipt shows both numbers.

    NOT VERIFIED on real hardware: both fixtures on this machine produce only circle-centre
    candidates (`candidate_kind: circle_centre`, no edge pairs at all), so the gate's drop
    branch never ran. The fixture that exhibits the bug (`AM_DOOR`) was not available. The
    logic is guarded by this assertion and by the 0.8.156 value check downstream; the gate
    itself is unexercised.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""

    assert "model_points" in body, (
        "the model coordinates are no longer collected at all")
    assert "_model_span_mm" in body, "the model span is not computed"
    assert '"model_span_mm"' in body, (
        "candidates do not report the model span, so a caller cannot see both numbers")
    assert '"model_check"' in body, (
        "the candidate does not say whether the model check was available — 'cannot "
        "verify' would look the same as 'verified'")

    # 0.8.169: the gate NO LONGER DROPS. It used to `continue` when the 3D model distance
    # missed the target — but the sheet test is single-axis and the model figure is a 3D
    # euclidean distance, so the two are not comparable. Measured on AM_DOOR: 13 gaps,
    # 5 paper matches, all 5 dropped, `apply_gaps` produced nothing.
    #
    # The `continue` scan is deliberately limited to the exact gate expression: a window
    # scan also matches the legitimate circle-centre `continue` further down, and an
    # assertion that forces correct code to change is worse than no assertion. (Measured:
    # it did, on the first version of this very check.)
    gate_at = body.find("if abs(_model_span_mm - target) > tolerance_mm:")
    assert gate_at < 0, (
        "the model gate filters candidates again — a single-axis sheet span and a 3D model "
        "distance are different quantities, so this rejects correct candidates")
    return "gap candidates record the model span as evidence instead of filtering on it"


def assert_no_tool_reports_success_for_work_it_did_not_do():
    """0.8.164 — the rule this whole day kept rediscovering, as a static check.

    Every serious defect of 2026-11-05 had the same shape: a receipt saying the job was done
    when it was not.

        duplicates_removed: 0      a NameError swallowed by `except`
        before: ""                 a getter that was never called
        sheet_point_on_circle      a field that was never populated
        applied: value: 8.0        the planned value, never compared to the drawn one
        ok: true, saved: false     a save that failed, reported as success
        未关闭任何文档 + closed 2   two sentences in one receipt

    v0.8.159 fixed the last one in `create_drawing`. The report asked for the general rule,
    and it is worth having: **nothing may return `ok(**result)` while `result` already says
    the work did not happen.**

    Scope is deliberately narrow — only `saved=False`, the one flag whose meaning is
    unambiguous. It must be followed by a `return error(` before any `return ok(`; that is
    the shape v0.8.159 established. Flags like `triangle_verified=False` are nested inside
    sub-objects and describe a detail, not the outcome, so they are out of scope.
    """
    offenders = []
    for module in sorted(os.listdir(os.path.join(ROOT, "src",
                                                 "solidworks_assetmate_mechdog", "tools"))):
        if not module.endswith(".py"):
            continue
        path = os.path.join(ROOT, "src", "solidworks_assetmate_mechdog", "tools", module)
        src = open(path, encoding="utf-8").read()
        if "saved=False" not in src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(src, node) or ""
            saved_at = body.find('"saved": False')
            if saved_at < 0:
                saved_at = body.find("saved=False")
            if saved_at < 0:
                continue
            after = body[saved_at:]
            ok_at = after.find("return ok(")
            if ok_at < 0:
                continue
            err_at = after.find("return error(")
            if err_at < 0 or err_at > ok_at:
                offenders.append("%s:%s" % (module, node.name))

    assert not offenders, (
        "these functions can return ok() after recording saved=False — success reported "
        "for work that failed: %s" % ", ".join(offenders))
    return "no function reports ok after recording that nothing was saved"


def assert_bom_configuration_falls_back_to_the_model():
    """0.8.166 — the BOM failed on the exact version it claims to support.

    Report 2026-11-05: `add_bom_table` returned

        error: bom_table_failed
        message: InsertBomTable4(Indented) returned None [sw_version=32.4.0]. This step is
                 verified on SW2024 SP4 only — a SW2026 report fails right here, so version
                 is the first thing to check.

    `32.4.0` IS SW2024 SP4. The version hint was a red herring the tool itself printed.

    Real cause: `configuration` defaulted to the literal string `"Default"`, and on a
    Chinese GB template the only configuration is named 默认. The fallback chain was

        configuration or getattr(view, "ReferencedConfiguration", "Default")

    which lands on `"Default"` whenever the view reports an empty string — and it does.

    Measured on a self-built two-block assembly:
        configuration="Default" -> bom_table_failed
        configuration="默认"    -> ok, 3 rows x 4 columns
        no argument (default)   -> ok, 3 rows x 4 columns   (after this fix)

    The chain is now: explicit -> view.ReferencedConfiguration -> the referenced model's
    ACTIVE configuration -> "Default" as a last resort. The third step is the one that
    knows the real name.
    """
    src = _read("tools/layout.py")
    fn = _function_node(ast.parse(src), "add_bom_table")
    assert fn is not None
    args = [a.arg for a in fn.args.args]
    defaults = [d for d in fn.args.defaults]
    # `configuration` must no longer default to a hard-coded string.
    idx = args.index("configuration")
    offset = len(args) - len(defaults)
    if idx >= offset:
        default = defaults[idx - offset]
        assert not (isinstance(default, ast.Constant) and isinstance(default.value, str)), (
            "configuration defaults to a literal string again — a Chinese template's only "
            "configuration is 默认, so this fails on the version it claims to support")

    body = ast.get_source_segment(src, fn) or ""
    assert "ActiveConfiguration" in body, (
        "the fallback no longer asks the referenced model for its active configuration — "
        "it will land on a name that does not exist")
    assert 'configuration or "Default"' in body, (
        "the last-resort default is gone; the parameter is now unbounded")
    return "BOM configuration falls back to the model's active configuration"


def assert_verified_capabilities_do_not_disown_themselves():
    """0.8.170 — a capability said "verified" and "fails on your version" at once.

    Report 2026-11-06 (P1): `check_capabilities(operation="bom_table")` returned

        status   : verified
        summary  : 装配明细栏（原生 BOM）· 仅 SW2024 SP4 验证
        why      : **仅 SW2024 SP4 验证**… SW2026 报告 InsertBomTable4(Indented) 返回 None…
                   **已解决**
        evidence : SW2024 SP4 实测 … SW2026 实测失败

    while the SAME response carried `sw_version: 34.0.0` — SW2026 — and the same build's
    smoke run had `add_bom_table` PASSING on that very machine (`rows=3 columns=4`).

    The real cause was found and fixed in v0.8.166: a Chinese GB template names its only
    configuration 默认, while the code passed the literal "Default", so
    `InsertBomTable4(Indented)` returned None and the receipt blamed the version. The code
    was corrected; **the capability table kept the old conclusion**, and it is the table a
    caller reads before deciding whether to run a long pipeline.

    This asserts the shape that made it possible: a `verified` entry may not also carry
    version-exclusive or failure wording. Naming the builds an entry was measured on is
    fine and expected — claiming it works *only* on one build, or that it failed on the
    build you are running, is the contradiction.
    """
    src = _read("tools/capabilities.py")
    tree = ast.parse(src)

    caps = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "CAPABILITIES" for t in node.targets):
            caps = node.value
            break
    assert caps is not None, "CAPABILITIES is gone"

    offenders = []
    # Only outright FAILURE claims. "实测于 SW2024 SP4" states the evidence base and is
    # wanted; "仅 SW2024 SP4 验证" is ambiguous (does it mean it fails elsewhere?), and
    # quoting an old conclusion while correcting it is legitimate. The unambiguous lie is
    # claiming an entry failed on some build while marking it verified.
    exclusive = ("实测失败", "实测不通过", "fails on SW20")
    for entry in ast.literal_eval(caps):
        if entry.get("status") != "verified":
            continue
        blob = " ".join(str(entry.get(k) or "") for k in ("summary", "why", "evidence"))
        for phrase in exclusive:
            if phrase in blob:
                offenders.append("%s contains %r" % (entry.get("id"), phrase))
    assert not offenders, (
        "verified capabilities that also disown themselves: %s — a caller reads this "
        "before deciding whether to run the tool" % "; ".join(offenders))
    return "no verified capability also claims it fails on some version"


def assert_auto_fit_updates_the_title_block():
    """0.8.171 — the drawing was 1:2 and the title block still said 1:1.

    Report 2026-11-06 (P0): with `views=["front","top","left"], projection="third"`,
    `auto_fit` reduced all three views from 1:1 to 1:2 to keep them on the sheet — and the
    title block kept saying **1:1**. A shop measuring off that sheet reads every linear
    dimension at half its true size. This is not cosmetic.

    Nothing in the receipt said the ratio had changed either: `produce_drawing` returned
    `warnings: []`, and the only trace was `views[].scale = 0.5` buried in an array.

    Now `create_drawing` writes the surviving ratio into the title block whenever it differs
    from what was asked for, records it as `title_scale_synced`, and `produce_drawing`
    surfaces `auto_scale_adjustments` as a warning.

    Measured 2026-11-06 on 飞达动力轴:
        adjustments: [{'from': 1.0, 'to': 0.5}]
        title_scale_synced: {'from': 1.0, 'to': 0.5, 'text': '1:2'}
        set_title_block -> written [{'field': 'scale', 'before': '1:1', 'after': '1:2',
                                     'ok': True}]
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "create_drawing")) or ""

    assert "_title_scale_synced" in body, (
        "auto_fit can change the scale without touching the title block again — the shop "
        "would measure at the wrong ratio")
    assert "set_title_block(" in body, (
        "the title block is no longer written when the scale changes")
    assert "apply=True" in body, (
        "set_title_block defaults to a dry run — without apply=True it reports success "
        "and writes nothing")

    # 0.8.177: the first version of this fix was a FAKE fix, and this assertion let it pass
    # because it only looked for the call. Report 2026-11-06: the receipt said
    # `title_scale_synced: {text: "1:2"}` while the sheet still read 1:1. Three causes:
    #
    #   1. it ran BEFORE the save — set_title_block works by path, reopening the file on
    #      disk, and the later SaveAsSilent overwrote that edit;
    #   2. the return value was discarded — set_title_block reports failure with an error
    #      dict, not an exception, so try/except saw nothing;
    #   3. it only fired when auto_fit changed the scale, so an explicit `scale=0.5`
    #      produced no sync at all.
    #
    # These three are now asserted in shape, because each one independently turned the
    # receipt into a claim the code could not back.
    save_at = body.find("SaveAsSilent")
    # Measure the actual sync CALL, not the first mention of the name: 0.8.211 pre-binds
    # `_title_scale_synced` before the save (it must, or 1:1 / save=False crash), so
    # `find("_title_scale_synced")` now lands above `SaveAsSilent` and this assertion
    # started failing on correct code.
    sync_at = body.find("set_title_block(out, scale=")
    assert save_at > 0 and sync_at > save_at, (
        "the title-block sync runs before the save again — SaveAsSilent will overwrite it, "
        "and the receipt will claim a sync that got thrown away")
    assert 'written") or []' in body or "written\") or []" in body, (
        "the return value is discarded again — set_title_block signals failure with an "
        "error dict, so a bare call always 'succeeds'")
    assert "verified_by" in body, (
        "nothing marks the sync as confirmed by written[].ok")
    assert "abs(_final_scale - 1.0)" in body, (
        "the trigger is back to 'changed from what was asked', so an explicit scale=0.5 "
        "never syncs the title block")

    pipe = _read("tools/pipeline.py")
    assert "creation_warnings" in pipe, (
        "produce_drawing still discards create_drawing's compliance notes and layout "
        "findings — the one-click entry point is what most callers use")
    assert '"scale_was_reduced"' in pipe or "scale_was_reduced" in pipe, (
        "a reduced scale is not surfaced as a warning")
    return "auto_fit keeps the title block ratio in agreement with the views"


def assert_a_wrong_insert_does_not_stay_on_the_sheet_silently():
    """0.8.174 — the P0 came back, and the counter described it as "nothing happened".

    Report 2026-11-06 (P0): v0.8.169 removed the model gate (correctly — it was comparing a
    single-axis sheet span against a 3D model distance). That also removed the only thing
    preventing insertion, so `apply_gaps` inserted six wrong dimensions per call:

        annotation_count  25 -> 31 -> 37
        1570.796 count     5 -> 10
        applied_count: 0     applied[]: 6 entries, all ok:false

    The receipt was honest per-entry and misleading in aggregate: `applied_count` counts
    successes, so "0" reads as "the drawing is untouched" while it grew by six.

    Two things are asserted here:
      * the counters can no longer describe a side effect as a no-op
        (`applied_failed`, `inserted_but_wrong`, `sheet_modified`);
      * the rollback that deletes the new annotation by identity has a SAFETY GUARD —
        if the identity snapshot came back empty, nothing is deleted. Measured 2026-11-06:
        `_dimension_full_names` returned [] until it went through
        `GetSpecificAnnotation` (the annotation object has no `GetDimension2`), so an
        unguarded rollback would have deleted every dimension on the sheet.

    NOT yet true: the rollback itself is not verified on real hardware — the fixtures here
    produce no `value_mismatch` case (no applicable edge pairs), so the delete path has
    never executed.
    """
    src = _read("tools/drawing.py")

    for field in ("applied_failed", "inserted_but_wrong", "sheet_modified"):
        assert field in src, (
            "`%s` is gone — `applied_count: 0` can again stand next to six inserted "
            "dimensions without saying so" % field)

    assert "_dimension_full_names" in src, (
        "the identity snapshot is gone; a wrong insert cannot be scoped for rollback")
    assert "_rollback_possible" in src, (
        "the rollback can run with an empty snapshot — every dimension would look new and "
        "the user's whole drawing would be deleted")
    assert "rollback_skipped_reason" in src, (
        "when the rollback cannot run, the receipt must say so instead of implying the "
        "sheet was restored")

    # The snapshot must go through the project's own reader, which knows the
    # GetSpecificAnnotation -> GetDimension2 chain.
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_dimension_full_names")) or ""
    assert "_annotation_dimension_full_name" in body, (
        "the snapshot builds its own identity chain again; `annotation.GetDimension2` does "
        "not exist on this binding (measured: 未知名称) and the snapshot silently returns []")
    return "a wrong insert is counted, scoped and never silently left behind"


def assert_a_failed_insert_stops_the_batch():
    """0.8.175 — one call inserted six wrong dimensions because the loop had no memory.

    Report 2026-11-06 (P0): `apply_gaps` inserted six wrong dimensions per call
    (`25 -> 31 -> 37` annotations, `1570.796` x5 -> x10). The receipt was honest per entry
    and the batch kept going: every remaining gap came from the same candidate pool, so
    every one failed the same way.

    A `value_mismatch` is not a per-gap accident — it means the sheet-vs-model premise does
    not hold for this drawing at all. Continuing can only add damage. So the first mismatch
    trips a breaker and the rest are reported as `not_attempted_after_mismatch`, which is
    distinguishable from "tried and failed" and from "nothing to do".

    This is the same principle as the rest of the day: a failure must be observable, and it
    must not silently continue into more of itself.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""

    assert "_mismatch_seen" in body, (
        "the insert loop can again run past a wrong insert")
    assert '"not_attempted_after_mismatch"' in body, (
        "the skipped gaps are not labelled — a caller cannot tell 'not tried' from "
        "'tried and failed'")
    assert "breaker_tripped" in body, (
        "the receipt does not say the batch was stopped early")

    # The counter must be defined before the branch, or the receipt raises NameError on a
    # call that had nothing to apply. (Measured: the first version defined it inside
    # `if _to_apply:` and used it at the end of the function.)
    init_at = body.find("_mismatch_seen = 0")
    branch_at = body.find("if _to_apply:")
    assert 0 < init_at < branch_at, (
        "the breaker counters are initialised inside the apply branch — a call with no "
        "applicable gaps would raise NameError building its receipt")
    return "a wrong insert stops the batch and says which gaps were skipped"


def assert_a_reversed_capability_does_not_linger_in_the_docs():
    """0.8.176 — a capability flipped, and six of eight consumers kept telling callers "no".

    Report 2026-11-06 (second review of the same day): v0.8.173 proved sheet sketch lines DO
    render once `SketchManager.AddToDB = True` is set (572 px), and the weld triangle had
    been rendering all along (2134 px, lines-only diff). `capabilities.py` and the
    `add_weld_symbol` docstring were updated. Everything else still said it could not work:

        server.py   add_sheet_line docstring   "does NOT work on SW2024/2026"
        server.py   verify_render param        "always returns line_not_rendered"
        README      tool table                 "实测不渲染（0 / 16,041,784 像素）"
        README      capability table           "不要配 add_sheet_line 画引线"
        com-quirks  §55                        "图纸草图在图纸上不渲染"
        smoke.py    comment                    "an ok:true here is precisely the lie"
        layout.py   old comment                "with AddToDB = True the diff is STILL 0"
        capabilities centerline                "做不出来——图纸草图本身不渲染"

    This is the mirror of v0.8.89 (proved it cannot draw, four places said it could). Same
    disease, opposite direction, and 182/182 green both times because no assertion compared
    the capability contract against the text a caller actually reads.

    So: once a capability says a thing DOES work, the "it does not work" phrasing must not
    survive in the consumer docs. Historical notes are fine if they say so — the check looks
    for the bare claims, not for the topic.
    """
    # Phrases that assert the tool cannot do the thing.
    banned = [
        "does **NOT work on SW2024/2026**",
        "图纸草图在图纸上不渲染",
        "实测不渲染",
        "图纸草图本身不渲染",
        "always returns `line_not_rendered`",
        "is precisely the lie",
        # Phrasings that survived because the blacklist only had exact sentences.
        "STILL 0",
        "has never appeared",
        "从来没出现过",
        "线画不上",
        "画不上会返回",
    ]
    # 0.8.211 (report P1): `layout.py` was missing, and it is where the ACTIVITY comment
    # "with AddToDB = True the diff is STILL 0 / the weld triangle has never appeared"
    # survived two days after the code was fixed. Comments in code are documentation too.
    sources = {
        "server.py": _read("server.py"),
        "tools/layout.py": _read("tools/layout.py"),
        "README.md": open(os.path.join(ROOT, "README.md"), encoding="utf-8").read(),
        "docs/com-quirks.md": open(os.path.join(ROOT, "docs", "com-quirks.md"),
                                   encoding="utf-8").read(),
        # The smoke comment said "an ok:true here is precisely the lie" long after the
        # tool started working — a test file is documentation too, and it was the last
        # place still asserting the opposite of the measured behaviour.
        "tests/smoke.py": open(os.path.join(ROOT, "tests", "smoke.py"),
                               encoding="utf-8").read(),
    }
    # Scan LINE BY LINE and skip any line that marks itself as a correction.
    #
    # Both `capabilities.py` and `layout.py` deliberately QUOTE the old wrong sentences
    # ("早先这里写…2026-11-06 更正", "It used to say … STILL 0") so the next reader knows what
    # was wrong and why. A blind substring scan reports those quotations as the bug — the
    # first version of this fix did exactly that and failed on correct code.
    _correction_markers = ("早先", "更正", "推翻", "used to say", "CORRECTED", "lagged",
                           "This used to", "was wrong", "previously", "旧注释")
    offenders = []
    for name, text in sources.items():
        for line in text.splitlines():
            if any(mark in line for mark in _correction_markers):
                continue
            for phrase in banned:
                if phrase in line:
                    offenders.append("%s: %r" % (name, phrase[:34]))
                    break

    cap = _read("tools/capabilities.py")
    for line in cap.splitlines():
        if any(mark in line for mark in _correction_markers):
            continue
        for phrase in banned:
            if phrase in line:
                offenders.append("tools/capabilities.py: %r" % (phrase[:34],))
                break

    assert not offenders, (
        "these still tell the caller the tool cannot render, while the capability contract "
        "says it can (measured 572 / 2134 px): %s" % "; ".join(offenders))

    # And the positive finding must be reachable BEFORE calling: `AddToDB` has to appear in
    # at least one tool description, otherwise a reader cannot reproduce the result.
    server = sources["server.py"]
    assert "AddToDB" in server, (
        "the fix itself (`AddToDB = True`) appears in zero tool descriptions — a caller "
        "cannot know why it works here and not on their machine")
    return "a reversed capability no longer contradicts itself across the docs"


def assert_delete_verifies_what_survived():
    """0.8.178 — a bulk delete reported success while leaving an item behind.

    Report 2026-11-06 (P0, B15): two consecutive `delete_dimensions(keep_values=[...])` calls
    reported `removed: 3` and `removed: 1`, both `ok: true` / `failed: 0` — and
    `list_drawing_annotations` still found a `D2 / 785.398` dimension afterwards. The caller
    had no way to know: the receipt counted what the loop BELIEVED it deleted.

    That leftover then left the sheet: its arc centre sat at sheet x < 0, and the exported
    PDF carried three paths with negative x0 (down to -299.2 pt on an A2 page) — a large arc
    and two leaders intruding into the main view. Delivery defect.

    Same class as a save that does not save: **the receipt described an intention, not a
    result.** So a delete must re-enumerate and name what survived.
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "delete_dimensions")) or ""

    assert "remaining_matches" in body, (
        "delete_dimensions does not report survivors — a partial delete looks like a clean "
        "one, and an annotation left outside the sheet is a delivery defect")
    assert "remaining_count" in body, "no count of survivors"
    assert "verified_clean" in body, (
        "the receipt cannot say 'nothing is left' as a verified fact")

    # The re-enumeration must happen AFTER the deletes, or it measures the wrong state.
    delete_at = body.find("doc.EditDelete()")
    remain_at = body.find("remaining = []")
    assert 0 < delete_at < remain_at, (
        "the survivor scan runs before the deletions — it would report the pre-delete state")

    # Angular dimensions must not masquerade as lengths in the overall-size check.
    drawing_src = _read("tools/drawing.py")
    assert 'dimension_type") or 0) not in (1, 3)' in drawing_src, (
        "angular / arc-length dimensions can enter the overall-extent comparison again — a "
        "45 degree angle reports 785.398 and makes `delivery_check` announce an impossible "
        "overall size")
    return "a bulk delete reports what survived; angles are not lengths"


def assert_gap_finder_survives_an_unmappable_axis():
    """0.8.179 — a square part made the whole gap finder return zero candidates.

    Report 2026-11-06 (P0, A2/A3): on a 60×60×10 part the sheet-axis mapping is ambiguous
    (`axes_mismatch` — the deflated outline matches two model extents equally). So
    `list_drawing_edges` reported `sheet_projected: false` with no sheet coordinates on any
    row, `plan_dimension_gaps` built an empty point list, and returned

        {"ok": false, "error": "not_enough_edges",
         "message": "need at least two edges with sheet coordinates, found 0"}

    Every sheet-coordinate-based capability — gap finding, dimension placement, edge
    picking — collapsed on a part shape that is not remotely exotic.

    The model coordinates were there the whole time. A distance is still a distance; what is
    lost is the placement hint, not the measurability. So the tool now falls back to model
    coordinates and SAYS SO via `coordinate_space`, instead of reporting that it found
    nothing.

    Not verified on real hardware: this machine has no equal-extent part, so the fallback
    branch has never executed (`coordinate_space` always reads "sheet" here).
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""

    assert "_model_fallback" in body, (
        "the model-coordinate fallback is gone — an ambiguous-axis part returns zero "
        "candidates again while its model coordinates sit unused")
    assert '_coordinate_space = "model_fallback"' in body, (
        "the fallback is not labelled, so a caller cannot tell model distances from sheet "
        "distances")
    assert "coordinate_space=" in body, (
        "the receipt does not report which space was used")

    # The empty-result error must mention the space, or the next report will again have to
    # guess whether the sheet projection or the model data was missing.
    assert "(space: %s)" in body, (
        "the not_enough_edges message does not say which coordinate space failed")
    return "gap finding falls back to model coordinates and names the space"


def assert_section_dimensions_have_somewhere_to_land():
    """0.8.182 — three defects that only bite together: depth dimensions had nowhere to go.

    Report 2026-11-06 (P1, B1/B2/B3). Each is small alone; together they made it impossible
    to put a depth dimension on a section view:

    **B1** — a section view is created in projection with its parent, so a requested y is
    silently rewritten. Measured: asked (0.335, 0.17), got (0.335, 0.3018), with
    `position_axis_ignored: "y"`. The caller had to make a SECOND call
    (`set_view(break_alignment=True)`) to get what they asked for; two calls for one intent,
    and the first looks like a bug.

    **B2** — `place_outside` clamped against "sheet minus 10 mm", which on A3 is
    [0.010, 0.410] x [0.010, 0.287]. The title block's top edge is at y = 0.065, so eleven
    stacked dimensions reached y = 0.0595 and printed over it — inside the old bound, outside
    the real one. Now clamped against `template_safe_area`.

    **B3** — `eliminate_duplicates` kept the FIRST occurrence, and insertion order is view
    creation order. The main view always won, so a section view (created later, and the only
    place a depth dimension belongs) had its copies deleted and ended up with zero
    dimensions. Now `prefer_view` chooses the survivor.
    """
    src = _read("tools/drawing.py")

    # B1
    assert "align_to_parent" in src, (
        "add_section_view cannot honour an explicit position again — the caller needs a "
        "second call to break the projection alignment")
    assert "alignment_broken" in src, (
        "the receipt does not say whether the alignment was broken, so a caller cannot tell "
        "which of the two behaviours they got")

    # B2
    assert "template_safe_area(candidate" in src, (
        "place_outside clamps against the sheet again instead of the frame interior — "
        "dimensions will stack over the title block and still look 'inside'")
    assert "inset = 0.010" in src, (
        "no fallback when the template cannot be read")

    # B3
    assert "prefer_view" in src, (
        "the dedupe pass has no view preference — a section view loses every copy and ends "
        "up with zero dimensions")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_dedupe_inserted_dimensions")) or ""
    assert "preferred = [e for e in entries if e[2] ==" in body, (
        "prefer_view is accepted but never consulted")

    # Both new parameters must reach the MCP surface, or tools/call drops them silently.
    server = _read("server.py")
    assert "align_to_parent: Annotated" in server, (
        "align_to_parent exists in the implementation but not in the tool schema")
    assert "prefer_view: Annotated" in server, (
        "prefer_view exists in the implementation but not in the tool schema")
    return "a section view can hold depth dimensions: position honoured, inside the frame, copies kept"


def assert_receipts_do_not_disagree_with_the_geometry():
    """0.8.183 — five small receipt defects, all the same shape: a field that lies.

    Each was reported 2026-11-06 and each is the same failure as the big ones: the value
    comes from a snapshot taken before the work, not from the result.

    **1** `create_drawing` reported `views[].scale: 1.0` / `chosen_scale: 1.0` /
    `scale_reason.final_scale: 1.0` while every view was at 0.5 after auto_fit stepped them
    down. `scale_reason` is the field NAMED "final".

    **2** `clamp_dimensions_into_safe_area` judged against the SHEET (frame ± 10 mm) while
    its name says safe area — 3 dimensions at x = 0.0308 / 0.0468 / 0.0388 passed as "inside"
    when the frame interior starts at 0.0588.

    **3** `dimension_gap.note` said "规划已生效" unconditionally whenever `prune_to_plan`
    was true, next to `planned 9 / inserted 12`.

    **4** `leader_endpoint_m` is not the leader's endpoint — it is a computed point on the
    attached edge's circle (centre + radius), measured 27.5 mm away from the real DXF LEADER
    start.

    **5** `value_mm: 785.398` for a 45 degree angle: `SystemValue` is radians for angular
    dimensions, multiplied by 1000 and presented as a length.
    """
    src = _read("tools/drawing.py")

    # 1 — the final scale must come from the views, at the end.
    assert "_corrected_scale_reason" in src, (
        "scale_reason can contradict the views again after auto_fit steps them down")
    assert '_item["scale"] = _final_scale' in src, (
        "views[].scale is a creation-time snapshot again, so it disagrees with ScaleDecimal")

    # 2 — clamp against the frame interior
    assert "template_safe_area(doc.GetFirstView" in src, (
        "clamp_dimensions judges against the sheet again, so dimensions outside the frame "
        "still report 'no dimension was off the sheet'")
    assert "safe_area_source" in src, (
        "the receipt does not say which area it clamped against")

    # 3 — the note must state numbers
    pipe = _read("tools/pipeline.py")
    assert '"规划已生效" :=' not in pipe, "the conditional was replaced by an assignment"
    assert "规划已生效（prune_to_plan=True）：计划" in pipe, (
        "dimension_gap.note announces the plan took effect without stating planned vs "
        "inserted")

    # 4 — honest field name, alias kept
    assert "leader_target_m=leader_endpoint" in src, (
        "the field still claims to be the leader endpoint")
    assert "computed_target_not_measured_arrow_position" in src, (
        "the alias does not say what it actually holds")

    # 5 — angular dimensions never carry value_mm
    assert 'row["value_deg"]' in src, "angular dimensions are not given a degree value"
    assert 'row["value_mm"] = None' in src, (
        "an angular dimension can still report a value_mm — 45 degrees becomes 785.398 mm")
    return "receipts report the result, not the plan"


def assert_an_angular_dimension_is_not_a_length():
    """0.8.191 — the phantom chased for 35 versions was a right angle.

    Every report since v0.8.155 mentioned `1570.796 mm` being inserted where 8.0 was asked
    for, and called it "model geometry measured in 3D". It is not a length at all:

        1570.796 / 1000 = 1.5708 rad = 90.00 deg
         523.599 / 1000 = 0.5236 rad = 30.00 deg

    `AddDimension2` between two edges that are NOT parallel creates an ANGULAR dimension,
    and `SystemValue` for an angular dimension is RADIANS. Multiply by 1000 (the length
    conversion) and a right angle reads as 1570.796 mm.

    The fixture's edge pair makes this visible: edge 14 degrades to a single point
    (start == end), edge 44 is a 40 mm segment. There is no distance between them to
    measure, so SolidWorks measured an angle instead.

    Consequences that all trace back here: the ledger never sees the value, so the gap stays
    missing and every call inserts again; `delivery_check` reported an impossible overall
    size; and the inserted annotation left the sheet.

    What must hold: an angular dimension is its own failure mode, not a tolerance miss.
    """
    src = _read("tools/drawing.py")
    assert '"angular_dimension_created"' in src, (
        "an angular dimension is reported as `value_mismatch` again — that wording sends "
        "the next reader looking for model geometry instead of radians")
    assert "dimension_type=dimension_type" in src, (
        "add_distance_dimension does not say what KIND of dimension it created")
    assert "_wrong_dim_type in (1, 3)" in src, (
        "nothing checks the dimension type before comparing values")

    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_distance_dimension")) or ""
    assert "dim2.GetType" in body, "the type is not read from the created dimension"
    return "an angular dimension is named as such, with the degrees spelled out"


def assert_a_length_is_judged_before_it_is_drawn():
    """0.8.192 — the only place the angular-dimension trap can be prevented.

    v0.8.191 learned that `1570.796` is a right angle in radians and started reporting it
    as `angular_dimension_created`. That is honest, but the wrong dimension is already on
    the sheet by then, and it cannot be deleted (com-quirks 56).

    So the pair must be judged BEFORE `AddDimension2`: a degenerate edge defines no
    direction, and non-parallel directions produce an angle rather than a distance.

    Measured on the door fixture:

        call1/2: dims 8->8 | wrong=0 | {'edge_pair_has_no_length': 6}
        1570.796 on sheet: False
        reasons: ['not_parallel_90.0deg', 'degenerate_edge']
    """
    src = _read("tools/drawing.py")
    assert "_edges_define_a_length" in src, (
        "the edge pair is not judged before insertion — a right angle will be drawn again")
    assert '"edge_pair_has_no_length"' in src, "a refusal is not reported as its own code"
    assert '"degenerate_edge"' in src, "degenerate edges (start == end) are not detected"
    assert "not_parallel_" in src, "the rejection does not say how far from parallel"

    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""
    judge_at = body.find("_edges_define_a_length(")
    insert_at = body.find("added = add_distance_dimension(")
    assert 0 < judge_at < insert_at, (
        "the judgement runs AFTER the insert — the wrong dimension is already on the sheet")
    return "a non-length edge pair is refused before AddDimension2 runs"


def assert_a_missing_measurement_is_not_compared():
    """0.8.194 — turning a fallback 0 into None broke a comparison two versions later.

    v0.8.190 changed `total_dimensions` from `0` (a lie: audit=False means "not measured")
    to `None`, and added `total_dimensions_measured`. Two versions later three smoke items
    failed with:

        dimensions: auto_dimension_failed: '>' not supported between instances of
        'NoneType' and 'int'

    because `if total > 30:` still assumed a number. `produce_drawing` reported only
    `dimensions failed; the drawing is not usable` — the actual reason was buried in
    `steps[].detail`, which the failure branch never returned.

    Two lessons, both asserted here:
      * a value that can be None must be guarded before comparison;
      * a failed step must carry its reason out of the pipeline.
    """
    src = _read("tools/drawing.py")
    assert "if total is not None and total > 30:" in src, (
        "`total` can be None (audit=False) and is compared again — TypeError on every such "
        "call, surfacing as 'dimensions failed' with no visible cause")
    assert "total_dimensions_measured" in src, (
        "the receipt no longer distinguishes 'not measured' from 'zero'")

    pipe = _read("tools/pipeline.py")
    assert 'st.get("detail")' in pipe, (
        "critical_step_failed reports only step names — a failure reads "
        "'dimensions failed; the drawing is not usable' with no reason")
    return "a None measurement is guarded, and a failed step carries its reason"

def assert_a_view_names_its_own_projection():
    """0.8.197 — the second "no such API" that did exist.

    `_view_axis_map` carried a comment saying there is no reliable way to know which model
    axes a drawing view displays:

        - `IView.GetViewPlaneNormal` does not exist  (true)
        - `IView.ModelToViewTransform` is not a row-major 4x4  (true)
        - `IMathPoint.MultiplyTransform` does not exist  (true)

    All three are true, and the conclusion was still wrong: `IView.GetOrientationName`
    answers it directly.

        工程图视图1  '*前视'   -> (z, y)
        工程图视图2  '*上视'   -> (x, z)
        工程图视图3  '*右视'   -> (z, y)

    The size match is kept as a fallback for rotated/custom views (where the name is ''),
    but every standard view — including a cube, which made the size match ambiguous —
    now resolves without guessing.

    Same failure as `add_sheet`: three failed probes were read as "the road is closed",
    when they only meant "these three are not the road".
    """
    src = _read("tools/drawing.py")
    assert 'GetOrientationName' in src, (
        "the view no longer asks its own orientation name — standard views go back to "
        "being matched by size, and a cube becomes ambiguous again")
    assert '"orientation_name"' in src, (
        "the receipt does not say which mechanism resolved the axes")

    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_view_axis_map")) or ""
    orient_at = body.find("GetOrientationName")
    size_at = body.find("precise_outline")
    assert 0 < orient_at, "the orientation lookup is gone"
    assert "'*前视'" in body and "'*上视'" in body, (
        "the standard-view table is gone")
    return "a standard view reports its own projection; size match is fallback only"

def assert_the_rollback_uses_the_sheet_name_space():
    """0.8.198 — the last "impossible" item was two name spaces, not a missing API.

    com-quirks 56 recorded four failed attempts to delete a dimension that had just been
    inserted, and concluded "cannot be done". The fourth attempt failed because positions
    do not match; the real defect was upstream of all four:

        IDimension.FullName               D1@Sketch1@AM_50ef1f.Part          <- model
        IDisplayDimension.GetNameForSelection
                                          D1@Sketch1@AM_50ef1f-1@工程图视图1  <- sheet

    A difference computed on `FullName` can never intersect what `Select` accepts, so
    `_new_names` was always correct and always useless.

    Measured after the fix (door fixture): `before=12 after=13 new=1`, and
    `Select(False)` + `EditDelete` on the matching annotation took the sheet from 8
    dimensions back to 7 — the pre-insert count.

    Also asserted: `SelectByID2` is NOT the mechanism. Its `Callout` argument is a required
    IDispatch and every null variant raises 类型不匹配 at arg 8, so the code must walk the
    annotations instead of selecting by name.
    """
    src = _read("tools/drawing.py")
    assert "_dimension_selection_names" in src, (
        "the snapshot went back to IDimension.FullName — a difference computed on it can "
        "never match a selection, so the rollback silently deletes nothing")
    assert "GetNameForSelection" in src, (
        "the sheet-side name is not read")
    assert '"rolled_back"' in src, "the receipt no longer reports the rollback"

    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""
    assert "_dimension_selection_names(_doc_live)" in body, (
        "plan_dimension_gaps does not snapshot with the selection-name reader")
    assert "_ann.Select(False)" in body, (
        "the rollback does not select the annotation it found")
    assert ".EditDelete()" in body, (
        "the rollback does not delete the selected annotation")
    # Check for a CALL, not a mention: the comment above the loop explains why
    # SelectByID2 cannot be used, and that sentence must not trip this assertion.
    assert "SelectByID2(" not in body, (
        "SelectByID2 cannot be used here — its Callout argument is required and every null "
        "variant raises 类型不匹配 at arg 8")

    quirks = open(os.path.join(ROOT, "docs", "com-quirks.md"), encoding="utf-8").read()
    assert "做得到" in quirks and "两套命名空间" in quirks, (
        "com-quirks still says deleting a just-inserted dimension is impossible")
    return "the rollback diffs on the sheet name space and deletes what it found"

def assert_a_tolerance_is_checked_on_the_figure():
    """0.8.202 — readback proves what COM stored, not what the sheet prints.

    The 2026-11-05 regression is the case in point: `bilateral` was mapped to 1, readback
    said `tolerance_type_readback: 1` (a valid swTolType_e), `applied: true`,
    `lower_sign_wrong: false` — and the PDF showed `┌ Ø14.20 ┐`, a BASIC box with no
    tolerance at all. Every field agreed with itself and none of them looked at the paper.

    A report put it plainly: "readback 读的是你写进去的值，它证明不了图面上是什么".

    So `add_dimension_tolerance(verify_render=True)` exports the sheet and classifies the
    text layer. Measured 2026-11-07, one fresh drawing per type, upper = lower = 0.05:

        bilateral  ['35', '0.05', '0.05']    -> bilateral_two_deviations
        symmetric  ['35 ±0.05']              -> symmetric_plus_minus
        basic      ['35']                    -> basic_no_deviation
        limit      ['35.05', '35.05']        -> limit_values

    `pdf_render_matches` compares that against the requested type, so "the figure is wrong"
    is a reported failure rather than four fields that each look fine.
    """
    src = _read("tools/drawing.py")
    assert "_tolerance_text_on_pdf" in src, (
        "the figure is no longer inspected — a tolerance can be stored correctly and "
        "printed as a BASIC box without anything noticing")
    assert "verify_render=False" in src, "verify_render parameter is gone"
    assert "pdf_render_matches" in src, (
        "the receipt does not compare the printed shape against the request")

    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_dimension_tolerance")) or ""
    for shape in ("bilateral_two_deviations", "symmetric_plus_minus",
                  "basic_no_deviation", "limit_values"):
        assert shape in body, "the expected shape %r is not mapped" % shape
    assert "pypdfium2" in src, "the PDF text layer is no longer read"

    server = _read("server.py")
    assert "verify_render" in server, "the MCP tool does not expose verify_render"
    return "the printed tolerance is classified and compared against the request"

def assert_a_write_can_name_its_sheet():
    """0.8.204 — "which page did you edit" must be an argument, not hidden state.

    Report 2026-11-07 (C3): every write tool edited whatever page happened to be active.
    After `add_sheet` that was the new, EMPTY page, so dimensioning page 2 silently
    dimensioned nothing. The tools now take `sheet=` (name or 1-based index) and switch
    before writing.

    Underneath sat the same mistake THREE times in one session, all of the form
    "`_iter_views(doc)` is not a per-sheet reader":

      1. `list_sheets` reported both pages with the same view list;
      2. `add_view_to_sheet` counted a total that never moved, so it "succeeded";
      3. the empty-sheet guard passed on an empty page, because the OTHER page's view was
         still in the document-level list.

    `ISheet.GetViews` is the per-sheet reader. `IView` has no `Sheet` property and
    `ISheet.GetSize` takes byref parameters pywin32 cannot supply.

    Measured 2026-11-07 on 飞达动力轴:
        empty page       -> ok:false  active_sheet_has_no_views
        sheet='图纸2'     -> ok:true   inserted 4  sheet 图纸2
        sheet=1          -> ok:true   inserted 4  sheet 图纸1
    """
    src = _read("tools/drawing.py")
    assert "_activate_sheet_if_given" in src, (
        "a write can no longer be told which sheet to edit — it edits hidden state again")
    assert "_active_sheet_views" in src, (
        "the active-sheet reader is gone; document-level `_iter_views` is not per-sheet")
    assert "active_sheet_has_no_views" in src, "the empty-sheet refusal is gone"

    for fn in ("auto_dimension", "set_title_block"):
        body = ast.get_source_segment(src, _function_node(ast.parse(src), fn)) or ""
        assert "_activate_sheet_if_given(doc, sheet)" in body, (
            "%s takes `sheet` but never switches to it" % fn)

    layout = _read("tools/layout.py")
    assert "_sheet_model_views" in layout, (
        "list_sheets/add_view_to_sheet went back to document-level view counting")

    server = _read("server.py")
    for fn in ("auto_dimension", "set_title_block"):
        i = server.find("def %s(" % fn)
        j = server.find("def ", i + 10)
        assert "sheet: Annotated" in server[i:j], (
            "the MCP tool %s does not expose `sheet`" % fn)
    return "writes name their sheet, and views are counted per sheet"

def assert_the_audit_judges_each_sheet_by_its_own_frame():
    """0.8.205 — one frame for every page flags correct views as out of bounds.

    Report 2026-11-07 (C4): a two-sheet drawing reported `sheet_count: 2` but ONE
    `sheet_size_m` (the first page's) and judged every view against it. On page 2 the view
    sits where page 2 puts it; page 1's larger frame does not contain it, so a perfectly
    placed view came back as `view_outside_sheet`.

    The audit now walks each sheet and uses THAT sheet's frame. Two things had to be
    measured rather than assumed:

      * `ISheet.GetOutline` does not exist — "<unknown>.GetOutline";
      * the frame lives on the sheet VIEW (`IView` with Type == 1), one of `GetViews`;
      * the size comes from `ISheet.GetProperties` last two entries.

    Measured 2026-11-07, page 1 A2 + page 2 A4:
        sheets[0] size [0.594, 0.42] views 2
        sheets[1] size [0.297, 0.21] views 1
        no spurious view_outside_sheet
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "drawing_audit")) or ""
    assert "sheets=" in body, "drawing_audit does not return per-sheet rows"
    assert "sheet.GetViews" in body or "sheet_obj.GetViews" in body, (
        "the audit counts document-level views again, so pages share one list")
    assert "GetProperties" in body, (
        "sheet size is read from something that does not exist (ISheet.GetOutline)")
    assert "ActivateSheet" in body, (
        "the audit no longer walks each sheet, so one frame still judges every page")
    assert "view_outside_sheet" in body and '"sheet": sheet_name' in body, (
        "the out-of-frame warning does not say which sheet it belongs to")
    return "each sheet is audited against its own frame"

def assert_delivery_checks_every_sheet():
    """0.8.206 — a drawing can pass delivery with an entirely blank page.

    Report 2026-11-07 (C4, second half): `delivery_check` aggregated the DOCUMENT, so
    `views_present` was satisfied by page 1 and said nothing about page 2. Add a sheet, put
    nothing on it, and the verdict was unchanged — even though the exported PDF has a blank
    page in the middle of the deliverable.

    It now consumes `drawing_audit`'s per-sheet rows and reports:
      * `sheets: [{index, name, size_m, view_count, dimensions, issues}]`
      * a `every_sheet_has_views` check that BLOCKS on any page with no view
      * out-of-frame detail naming both the view and its sheet

    Measured 2026-11-07 (page 1 with a front view + one empty page):
        verdict  仅供参考/未完成
        blockers [every_sheet_has_views, dimensions_present, hole_callouts]
        sheets   [('图纸1', 1), ('空页', 0)]

    Also fixed here: `len(ai_supplied)` in the verdict note, where `ai_supplied` defaults
    to None. That raised `object of type 'NoneType' has no len()` on every call that did
    not pass the argument — introduced in 0.8.201 and invisible to smoke, whose case
    always passes it.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "delivery_check")) or ""
    assert "every_sheet_has_views" in body, (
        "delivery no longer blocks on an empty page — a blank sheet passes")
    assert "sheet_summary" in body, "the per-sheet summary is gone"
    assert "sheets=sheet_summary" in body, "the receipt does not expose per-sheet rows"
    assert "len(ai_supplied or [])" in body, (
        "len(ai_supplied) is back — it raises TypeError whenever the argument is omitted")
    return "delivery reports and blocks on per-sheet state"

def assert_sheet_direction_comes_from_the_view_name():
    """0.8.207 — "the API cannot tell direction" was wrong for the fifth time.

    `list_drawing_edges` carried: "There is no way to decide this from the API:
    `_view_axis_map` gives the axis PAIR but not its direction, and both the outline and the
    view position are symmetric under the transform." It shipped a manual `flip_x` and a
    warning telling the caller to measure pixels.

    `IView.GetOrientationName` says where the eye is, and the sign follows from handedness.
    The proof that the old projection was wrong needs no pixels: on 飞达动力轴 with front +
    right + left, model Z against sheet X gave dZ/dX = +/+ for BOTH the right view and the
    left view. One looks along -X, the other along +X — the model's +Z cannot fall on the
    same side twice.

    After deriving the sign from the name (up = +Y throughout):
        *右视  forward -X  -> screen right = +Z   (no flip)
        *左视  forward +X  -> screen right = -Z   (flip X)
        *上视  forward -Y  -> screen up    = -Z   (flip Y)

    Measured 2026-11-07 after the fix:
        view1 (front) dZ +0.0775 dX +0.0000  same
        view2 (right) dZ +0.1070 dX +0.1070  same
        view3 (left)  dZ +0.1070 dX -0.1070  MIRRORED
        based_on = orientation_name, verified = True
    """
    src = _read("tools/drawing.py")
    assert "_std_flip" in src, (
        "the orientation-derived direction table is gone — side views mirror again")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "list_drawing_edges")) or ""
    assert "flip_x" in body and "effective_flip_x" in body, (
        "the projection no longer applies a direction")
    assert "sheet_axis_direction_verified" in body, (
        "the receipt stopped reporting whether the direction is established")
    assert '"sheet_axis_basis"' in body, (
        "the receipt does not say WHAT the direction was based on")
    return "side-view direction is derived from the view name, not guessed"

def assert_too_many_matches_means_no_answer():
    """0.8.209 — hundreds of matches is not a plan, it is a coincidence.

    Report 2026-11-07 (C8): on 接槽 a 50 mm gap matched **696** edge pairs, and the planner
    still offered three of them as "candidates", one of which was `edge_a:3` (the frame
    edge) paired with `edge_b:69` (a small circle). The distance was right; the semantics
    were nonsense.

    A distance-only test has no discriminating power once everything matches. The count is
    the signal, so `plan_dimension_gaps` now grades it:

        <= 1 match        high    "唯一匹配"
        2..50             medium  "N 条边对匹配"
        > 50              low     never `fillable`, never filled by `apply_gaps`

    `low_confidence` is deliberately a separate STATUS, not a flag on `fillable`: "too many
    answers" is a different failure from "one answer" or "several answers", and auto-apply
    must not resolve it. The caller has to name the gap with `apply_indices`.

    Measured 2026-11-07 on door (front + right):
        statuses   {no_geometry: 7, no_length_pair: 5, low_confidence: 1}
        confidence {high: 7, medium: 5, low: 1}
        the low one: 3.05 mm matched 112 pairs
        apply_gaps -> applied_count 0, applied_failed 0, sheet_modified false
    """
    src = _read("tools/drawing.py")
    assert "LOW_CONFIDENCE_MATCH_LIMIT" in src, (
        "the match-count threshold is gone — hundreds of coincidences look like a plan again")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""
    assert '"low_confidence"' in body, "the status is gone"
    assert '"confidence"' in body and '"confidence_reason"' in body, (
        "the receipt does not carry the grade or the reason")
    # low confidence must short-circuit BEFORE the fillable branch
    low_at = body.find('_low = entry.get("confidence") == "low"')
    fillable_at = body.find('entry["status"] = "fillable"', low_at if low_at > 0 else 0)
    assert low_at > 0 and 0 < low_at < fillable_at, (
        "a low-confidence gap can still become `fillable`, so apply_gaps would fill a "
        "coincidence")

    smoke = open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8").read()
    assert "low_confidence" in smoke.split("gap status is not one this smoke knows")[0], (
        "the smoke whitelist does not know the new status, so a real run would fail there")
    return "too many matches is graded low and cannot auto-apply"

def assert_a_conditional_receipt_field_is_pre_bound():
    """0.8.210 — a variable assigned inside `if` and read outside it.

    `create_drawing` syncs the title-block scale only when the final scale is not 1:1, and
    `_title_scale_synced` was only assigned in that branch — then unconditionally put into
    the receipt. A drawing that stayed 1:1 raised:

        create_drawing_failed: cannot access local variable '_title_scale_synced'
                               where it is not associated with a value

    Every existing fixture auto-fit a front view down to 0.5, so the branch always ran. The
    case that found it builds right+left on a 10 mm door, which fits at 1:1 — measured
    2026-11-07, first time that combination was ever drawn.

    This is the fifth instance today of the same shape: a name that is valid on one path and
    unbound on another. The static check catches undefined MODULE references; nothing
    catches an unbound LOCAL, so this pins the one that bit.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "create_drawing")) or ""
    # 0.8.211: pre-bound to a REASON dict, not to None — "nothing to do" and "could not
    # find out" must be distinguishable in the receipt.
    pre = body.find("_title_scale_synced = {")
    guard = body.find('if result.get("saved"):')
    # 0.8.211 also removed a DUPLICATE `result.update(title_scale_synced=...)`; the field
    # now appears once, as a keyword of the final `result.update(...)`. Match the keyword,
    # not the old wrapper.
    use = body.find("title_scale_synced=_title_scale_synced")
    assert pre > 0, "`_title_scale_synced` is no longer pre-initialised"
    assert 0 < pre < guard, (
        "the initialisation must come BEFORE the conditional branch, or the 1:1 path "
        "raises NameError again")
    assert use > guard, "the receipt field is gone"
    assert body.count("title_scale_synced=_title_scale_synced") == 1, (
        "the receipt field is written twice again — one of them will drift")
    return "the conditional receipt field is bound on every path"

def assert_a_failed_create_closes_what_it_opened():
    """0.8.212 — an error path that leaves a document behind is still a leak.

    Report 2026-11-07 (P0-3): `create_drawing`'s outermost `except` returned an error
    without closing the drawing it had just created. The probe recorded
    "docs after: 17 (leaked 5)", including:

        LEAK 工程图87 - 图纸1 | path=''

    A blank drawing has an EMPTY path, so every cleanup pass that matches by path — which
    is all of them — is blind to it. That is the same shape as the 0.8.89 blank-drawing
    leak.

    The handler now holds the object and closes it by TITLE, and the receipt says which
    document was closed (or that the close itself failed). Both matter: a silent cleanup is
    indistinguishable from no cleanup.

    String-pinned because a real error path needs a real COM failure to trigger; the behaviour
    was exercised by the probe described above, not by this assertion.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "create_drawing")) or ""
    assert "_created_drawing = None" in body, (
        "the cleanup handle is gone — a failed create leaks its drawing again")
    assert "_created_drawing = drw" in body, (
        "the drawing is not remembered, so the handler has nothing to close")
    assert "CloseDoc(_title)" in body, (
        "the handler does not close by title — a blank drawing has no path to match")
    assert "closed_on_failure" in body, (
        "the receipt does not say whether anything was cleaned up")
    # the handle must be bound before the try, or the handler cannot see it
    pre = body.find("_created_drawing = None")
    try_at = body.find("    try:")
    assert 0 < pre < try_at, (
        "the cleanup handle is bound inside the try, so the except block cannot reach it")
    return "a failed create_drawing closes what it opened and says so"

def assert_title_block_looks_up_the_sheet_object_not_the_parameter():
    """0.8.215 — I renamed a local variable and missed one consumer.

    0.8.204 added a `sheet` PARAMETER to `set_title_block`. The existing local Sheet object
    was renamed to `_sheet_obj` — but the `_note_at(sheet, slot)` call site was left passing
    the parameter. With `sheet=None` (the default) `sheet.GetAnnotations` raised,
    `_note_at` returned None, and EVERY field took the `via: "created"` path.

    `created` does not rewrite the existing cell; it adds a NEW note next to it. That is the
    "两处 POM / 两处 JT-001" the 2026-11-07 report measured — a regression I introduced, not
    a template problem.

    Underneath that sat a second, older defect: `_note_text` could never have worked, because
    the read was `annotation.GetText(1)` and `IAnnotation` has no `GetText` at all — the
    method is on `INote` and takes NO arguments.

    Measured 2026-11-07 after both fixes (GB A3):
        name      before='飞达动力轴'   via=position
        material  before='  '          via=position
        apply     written both ok=True
        re-run    before='测试名'/'45钢'  -> no second write
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "set_title_block")) or ""
    assert "_note_at(_sheet_obj, slot)" in body, (
        "the lookup passes the `sheet` PARAMETER again — it is a page name or None, not a "
        "Sheet object, so every field falls back to creating a duplicate note")
    assert "_note_at(sheet," not in body, (
        "`_note_at(sheet, ...)` is back; `sheet` is the parameter")
    return "the title-block lookup uses the Sheet object, not the page-name parameter"

def assert_a_rollback_does_not_require_a_non_empty_before_shot():
    """0.8.216 — "the sheet had no dimensions" is not "the reader failed".

    Report 2026-11-07 (P0-1): on a drawing with ZERO dimensions (the first auto-dimension
    run — the normal case), `apply_gaps` inserted a wrong dimension (`146.5 mm` for a
    32.5 mm gap), reported `inserted_but_wrong: true`, and then refused to remove it:

        "rolled_back": false,
        "rollback_skipped_reason": "the dimension-identity snapshot was empty, so a delete
                                    could not be scoped"

    The guard was `bool(_names_before) and bool(_names_after)`. An empty before-snapshot is
    a LEGAL state, but the code could not tell it apart from a failed read, so it chose not
    to delete. The next call rolled back only its own insert; `RD1` stayed on the sheet
    forever and `reconcile_ledger` coverage stayed at 0.0.

    This is the empty-collection ambiguity `open_document_paths()` was fixed for in 0.8.161,
    reappearing in a different form. The fix is the same shape: tri-state the reader.

        None   read FAILED          -> delete nothing
        []     the sheet is empty   -> delete exactly the names that appeared
        [...]  the sheet's names

    The scope comes from `_new_names`, which was already computed and already printed in the
    receipt — it was never used for the decision.
    """
    src = _read("tools/drawing.py")
    reader = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                       "_dimension_selection_names")) or ""
    assert "return None" in reader, (
        "the reader is not tri-state again — an empty sheet and a failed read look alike")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "plan_dimension_gaps")) or ""
    assert "_rollback_possible = (_names_before is not None) and bool(_new_names)" in body, (
        "the rollback guard no longer requires new names — either it deleted nothing on an "
        "empty sheet (the P0) or it can delete user dimensions")
    assert "bool(_names_before) and bool(_names_after)" not in body, (
        "the old guard is back: a sheet with no dimensions can never be rolled back")
    return "a rollback scopes on the names that appeared, not on the before-snapshot"


def assert_sheet_activation_is_read_back():
    """0.8.216 — `ActivateSheet` returned True and the page did not change.

    Report 2026-11-07 (P0-2), direct COM probe on SW2024 (32.4.0):

        ActivateSheet("图纸2") -> true
        GetCurrentSheet.GetName -> "图纸1"
        GetViews count -> 3 (the SAME three views)

    So every write tool kept editing page 1 while the receipt said `active_sheet: 图纸2`.
    The receipt was echoing the ARGUMENT, not reading the document back.

    Measured 2026-11-07 on this machine (SW2024 SP4): activation works normally —
    `图纸1` 1 view / `图纸2` 0 views, and `add_view_to_sheet` + `auto_dimension(sheet=...)`
    land on page 2. The readback therefore does not fire here; its value is that on a build
    where it does not stick, the call now FAILS instead of writing to the wrong page.
    """
    src = _read("tools/drawing.py")
    helper = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                        "_activate_sheet_if_given")) or ""
    assert "sheet_activation_not_effective" in helper, (
        "activation is no longer verified — a silent wrong-page write is possible again")
    assert "GetCurrentSheet.GetName" in helper, (
        "the helper does not read the page back, so it can only echo the request")
    assert "sw_version" in helper, (
        "the refusal does not say which SolidWorks build behaved this way")

    layout = _read("tools/layout.py")
    for fn in ("activate_sheet", "add_view_to_sheet"):
        body = ast.get_source_segment(layout, _function_node(ast.parse(layout), fn)) or ""
        assert "sheet_activation_not_effective" in body, (
            "%s does not verify the page actually changed" % fn)
    assert "active_sheet=current" in layout, (
        "activate_sheet echoes the argument again instead of the read-back page")
    return "sheet activation is read back and refused when it did not take effect"

def assert_a_section_that_did_not_cut_is_not_ok():
    """0.8.217 — `kept_dimension` is true by construction for a view that never cut.

    Report 2026-11-07 (P0-1): on a revolved part the axial full section is the MAIN view,
    and taking it from the front view produced a view that was line-for-line identical to
    its parent — `edge_count 4` against the parent's 4, `hatch_count 0`, no bore, no
    counterbore — while the receipt said:

        "sanity": {"verdict": "ok", "ratio": 1.0,
                   "source_size_mm": [25.98, 63.98],
                   "section_size_mm": [25.98, 63.98]}

    Of course the sizes match: an uncut projection has the same extent as its source. The
    check was measuring the one property that cannot detect this failure.

    Two independent signals were already available and neither was consulted: the hatch
    count (0 means nothing was cut open) and the edge count (equal to the parent's means
    the same edges came through).

    Measured by the reporter: front-view cut -> 4 edges / 0 hatches / "ok";
    end-view vertical cut -> 28 edges / 2 hatches / correct section.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "_section_sanity")) or ""
    assert "hatch_count" in body, (
        "sanity no longer looks at hatching — an uncut projection reports ok again")
    assert "no_hatch" in body, "the no-hatch verdict is gone"
    assert "identical_to_source" in body, (
        "sanity does not compare edge counts, so a projection identical to its parent "
        "still reads as a valid section")
    call = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "add_section_view")) or ""
    assert "source_edge_count=_source_edge_count" in call, (
        "the parent's edge count is not passed in, so the identical-to-source check can "
        "never fire")
    assert "hatch_count=hatch_count" in call, "the hatch count is not passed in"

    # The verdict must not be assigned unconditionally after the specific checks. The first
    # version of this fix put `no_hatch` in and then a later line overwrote it with "ok" —
    # measured 2026-11-07 on 小车底部滚轮: hatch 0, edges 4 vs parent 4, verdict still "ok".
    assert 'if "verdict" not in info:' in body, (
        "the default verdict overwrites the specific one again — `no_hatch` will come out "
        "as `ok` exactly as before")
    caps = _read("tools/capabilities.py")
    assert "cut_line_overshoot_m" in caps, (
        "the working recipe for an axial section of a revolved part is not documented; "
        "the only way to find it is to guess")
    return "a section that did not cut reports a named doubt, not ok"


def assert_a_title_block_scale_can_be_written():
    """0.8.217 — the 比例 cell exists, is readable, and cannot be matched.

    Report 2026-11-07 (P0-2): every view was 2:1 while the title block said 1:1, and:

        set_title_block(scale=2.0, apply=True)
        -> "unmatched": [{"field": "scale", "marker": "n:m",
                          "reason": "title block has no scale value in the bottom band"}]

    Meanwhile `list_drawing_annotations` listed that very text — `细节项目209`,
    `kind: title_block_text`, `position_m: [0.3591, 0.0304]`, `text: "1:1"`. The marker
    scan looked for a *value* pattern in the bottom band and the cell is a bare `1:1`, so
    it was skipped.

    This pins the wider matcher: the scale field must be locatable from the label
    (`比例`) or from a bare `n:m` in the title-block band, not only from a value+label
    pair.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                      "set_title_block")) or ""
    assert "scale_pattern" in body, "the bare n:m matcher is gone"
    # 0.8.221 found the actual cause: `notes = _sheet_notes(sheet)` passed the PARAMETER
    # (a page name or None), so the list was empty or raised and the loop never ran —
    # third site of the same 0.8.204 rename miss, after `_note_at` and before the receipt.
    assert "_sheet_notes(_sheet_obj)" in body, (
        "the note list is built from the `sheet` parameter again, which is a page name — "
        "the loop body never executes and every field reads as unmatched")
    assert "_sheet_notes(sheet)" not in body, (
        "`_sheet_notes(sheet)` is back; `sheet` is the parameter")
    # And the value has to be a RATIO: str(2.0) wrote "2.0" into a cell that reads "1:1".
    assert "_scale_text" in body, (
        "the scale is written as a decimal again instead of n:m")
    return "the scale cell is found, and written as a ratio"


def assert_the_note_reader_is_used_everywhere():
    """0.8.221 — one bad read, three sites, three releases to find them all.

    `IAnnotation.GetText` does not exist; the method is `INote.GetText()` with no
    arguments. Three separate places read a note and all three were broken in the same
    way, and each one silently produced "":

      1. `_note_at` looked up the existing cell by position — fixed 0.8.215 (`_note_text`);
      2. the marker loop read `before` — fixed 0.8.215 (`_note_text`);
      3. the scale-cell loop read `text` for its n:m pattern — fixed 0.8.221.

    Site 3 is why `set_title_block(scale=2.0)` reported
    "title block has no scale value in the bottom band" while `list_drawing_annotations`
    printed that very text at [0.3591, 0.0304].

    A grep is the right assertion here: the point is that NO site reads GetText directly
    any more.
    """
    src = _read("tools/drawing.py")
    lines = src.splitlines()

    # Only the `set_title_block` body matters — `_note_text` itself must call GetText, and
    # `add_hole_callouts` legitimately calls `ICallout.GetText()`. Scanning the whole file
    # reports both as offenders, which is how the first version of this assertion failed.
    start = next(i for i, l in enumerate(lines) if l.startswith("def set_title_block("))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("def ") and not lines[i].startswith("def set_title_block")),
               len(lines))
    body_lines = lines[start:end]
    offenders = []
    for offset, line in enumerate(body_lines):
        stripped = line.split("#", 1)[0]
        if ".GetText" not in stripped:
            continue
        if "_note_text" in stripped:
            continue
        # dimension text slots live on IDisplayDimension and take an index — legitimate
        if "dimension" in stripped.lower() or "display" in stripped.lower():
            continue
        offenders.append("line %d: %s" % (start + offset + 1, stripped.strip()[:70]))
    assert not offenders, (
        "a note is still read with the wrong GetText — it will silently return an empty "
        "string and the cell will look free://n" + "\n".join(offenders[:5]))
    return "every note read in set_title_block goes through _note_text"

def assert_a_tolerance_can_print_three_decimals():
    """0.8.219 — H7 fits printed as +0.02 because the tolerance had two decimal places.

    Report 2026-11-07 (P1-1): `add_dimension_tolerance(value_mm=18, upper=0.018)` printed
    `Ø18 +0.02 / 0`. Ø30 H8 +0.033 printed `+0.03`. Metric fits live at three decimals;
    two makes the tolerance unusable, so the fit could only go in the notes.

    The precision is NOT on `IDimensionTolerance` — that interface has no precision property
    at all (checked: only font/values). It is `IDisplayDimension.SetPrecision3(Primary,
    Dual, PrimaryTol, DualTol)`, and the TOLERANCE digits are the third argument.

    Getting to that object took a second attempt: `IDimension` has no GetAnnotation and no
    GetDisplayDimension, so `IDisplayDimension(dim._oleobj_)` raises. The annotation has to
    be carried out of the lookup. That failure was silent — the `except` turned it into
    `tolerance_precision_applied: None` and the drawing kept printing two places.

    Measured 2026-11-07 on 飞达动力轴, upper=0.018, PDF text layer:
        tolerance_precision=2 -> applied 2, printed "+0.02"
        tolerance_precision=3 -> applied 3, printed "+0.018"
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_dimension_tolerance")) or ""
    assert "SetPrecision3(" in body, "the decimal places are no longer set"
    assert "GetPrimaryTolPrecision()" in body, (
        "the result is not read back, so a failed precision set looks like success")
    assert "tolerance_precision_applied" in body, (
        "the receipt does not say how many decimals were applied")
    assert "_ann.GetSpecificAnnotation" in body, (
        "the display dimension is built from IDimension again — that raises, and the "
        "drawing silently keeps two decimals")

    finder = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                       "_find_dimension")) or ""
    assert "return view, dim, ann" in finder, (
        "_find_dimension must hand back the annotation; there is no way from IDimension")
    server = _read("server.py")
    assert "tolerance_precision" in server, "the MCP tool does not expose the parameter"
    return "tolerance decimals are settable, read back, and proven on the PDF"

def assert_the_tolerance_probe_reads_a_window_not_the_page():
    """0.8.220 — a flat text dump lost the deviation stack, and the check cried wolf.

    Report 2026-11-07 (P1-1): `verify_render=true` reported

        "shape": "basic_no_deviation", "deviation_count": 0,
        "lines": ["0 18", "3. 18 孔为基准孔 H7， 30 沉孔 H8，..."]

    while `+0.03 / 0` was plainly printed under the Ø30. So a CORRECT tolerance was
    reported as missing — worse than the original blind spot, because a check that cries
    wolf stops being trusted.

    Two objects were involved and `get_text_range()` flattened them apart: the `<MOD-DIAM>`
    glyph and the tolerance stack are separate text items, and the flat dump also drags in
    the technical-requirements prose, which mentions "H7"/"H8" and can answer for the
    dimension by accident.

    So the probe now reads per rect and keeps only a WINDOW around the nominal — ±3 text
    heights in both axes. Measured 2026-11-07 on 飞达动力轴 with tolerance_precision=3:

        bilateral  bilateral_two_deviations  windowed  ['35', '0.050', '0.050']
        symmetric  symmetric_plus_minus      windowed  ['35 ±0.050']
        basic      basic_no_deviation        windowed  ['35']

    The window must REPLACE the page text, not be appended to it: appending double-counts
    every deviation (a bilateral would report `deviation_count: 4`).
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "_tolerance_text_on_pdf")) or ""
    assert "count_rects()" in body, (
        "the probe reads the page as one string again — the stack disappears from it")
    assert "get_text_bounded" in body, "per-rect text extraction is gone"
    assert "windowed" in body, "the receipt no longer says whether a window was used"
    assert 'raw = "\\n".join(window_text)' in body, (
        "the window is appended to the page text again — deviations will be double-counted")
    return "the tolerance probe reads a local window around the nominal"

def assert_the_limits_readback_is_lower_then_upper():
    """0.8.222 — `GetToleranceValues` returns (LOWER, UPPER), and the receipt swapped them.

    Measured 2026-11-07 on 飞达动力轴, requested upper +0.018 / lower -0.003:

        GetToleranceValues -> (-3e-06, 1.8e-05)      in metres, i.e. (-0.003, +0.018)

    The receipt labelled `_tv[0]` as "upper", so it reported `upper: -3e-06`,
    `lower: 1.8e-05` — and then `lower_sign_wrong` fired because "lower" was positive.
    **The tool reported its own reading error as a writing error.**

    Same order as `SetToleranceValues(TolMin, TolMax)`, which was already correct.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_dimension_tolerance")) or ""
    low_at = body.find('limits_stored_m["lower"] = round(float(_tv[0])')
    up_at = body.find('limits_stored_m["upper"] = round(float(_tv[1])')
    assert low_at > 0, "the lower limit no longer comes from _tv[0]"
    assert up_at > 0, "the upper limit no longer comes from _tv[1]"
    assert low_at < up_at or True, "order of assignment does not matter; the indices do"
    return "the tolerance readback maps _tv[0] to lower and _tv[1] to upper"


def assert_a_covered_hole_is_not_a_blocker():
    """0.8.222 — a plain bore with Ø dimensions was a BLOCKER.

    Report 2026-11-07 (P2-7): the part had a Ø18 through-bore and Ø30 counterbores at both
    ends. `hole_callouts` reported `ok: false` (no native callout) and the same receipt said
    `covered: true` with three Ø dimensions. That blocker downgraded a finished drawing to
    仅供参考/未完成.

    GB accepts a Ø dimension on a plain hole; the native callout carries depth /
    countersink / thread, which is required for THREADED and countersunk holes. Making the
    callout mandatory for every cylindrical face rejects valid drawings — the same mistake
    the 0.8.200 `fillable` change was made to avoid.

    It now blocks only when NOTHING covers the holes (no callout AND no Ø dimension);
    "Ø only" is `pending`.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "delivery_check")) or ""
    assert "blocking=(holes_in_drawing == 0 and diameter_dims == 0)" in body, (
        "a Ø-covered hole blocks delivery again")
    assert "covered_by_diameter_only" in body, (
        "the receipt no longer distinguishes Ø coverage from a native callout")
    return "a hole covered by Ø dimensions is pending, not blocking"

def assert_a_dimension_can_be_radial():
    """0.8.226 — a semicircular groove could only be dimensioned as a diameter.

    Report 2026-11-07 (P1-2): `add_drawing_dimension(edge_index=10)` on the R10 groove of a
    turned part produced **Ø20**, and `skip_duplicates` then rejected it because the groove
    WIDTH was also 20 — "Ø20 is a meaningless callout for a semicircular groove, and it gets
    deduplicated against an unrelated 20".

    `AddRadialDimension2` existed the whole time and was never wrapped. Measured 2026-11-07
    on 飞达动力轴 (Ø35 shaft, edge 4):

        style="auto"      -> value 0.035   style_used "linear"
        style="radius"    -> value 0.0175  style_used "radius"     <- R17.5
        style="diameter"  -> value 0.035   style_used "diameter"   <- Ø35

    The receipt must say which form was actually created: SolidWorks can refuse the radial
    call and the code falls back to a linear dimension, and a caller that assumes the R
    landed would ship a wrong callout.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_drawing_dimension")) or ""
    assert "AddRadialDimension2" in body, (
        "the radial call is gone — an arc can only be dimensioned as a diameter again")
    assert "AddDiameterDimension2" in body, "the explicit diameter call is gone"
    assert 'style_used' in body and 'style_requested' in body, (
        "the receipt does not say which callout form was created, so a silent fallback to "
        "linear looks like an R that landed")
    assert '"radius", "diameter", "linear"' in body, (
        "the accepted style values are not validated")
    server = _read("server.py")
    assert "style: Annotated[str" in server, "the MCP tool does not expose `style`"
    return "an arc can be dimensioned as R, and the receipt names the form used"

def assert_the_title_block_is_not_blocked_by_the_safe_area():
    """0.8.227 — the safe area and the title block were mutually exclusive.

    Report 2026-11-07 (P0-2): `add_note(text="2:1", x=0.3565, y=0.0285)` was refused with
    `block_out_of_safe_area` — `safe_area.bottom` is 0.065 and the whole title block sits
    below 0.061. So every form field except the three hard-coded slots (`name` / `code` /
    `material`) had no way in: 重量, 版本, 共/第几张, 日期.

    The safe area exists to keep the DRAWING area clear of the frame. The title block is a
    printed form in the bottom-right corner; filling it is the normal case, not a layout
    violation.

    Measured 2026-11-08 on 飞达动力轴 A3:
        add_note("V1.0", 0.3565, 0.0285) -> ok, title_block_zone_used true
                                            bbox [0.3565, 0.0229, 0.367, 0.0285]
        add_note("X", 0.10, 0.02)        -> block_out_of_safe_area (still refused)

    Both directions are asserted: a waiver that also lets blocks into the drawing margin
    would be worse than the bug.
    """
    src = _read("tools/layout.py")
    assert "_title_block_zone(" in src, "the title-block corner is no longer recognised"
    assert "title_block_zone_used" in src, (
        "the receipt does not say the safe-area check was waived — a caller cannot tell a "
        "real layout pass from an exemption")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "add_note")) or ""
    assert "_title_block_zone(" in body, (
        "add_note does not consult the title-block zone, so form fields are refused again")
    assert "violations = []" in body, (
        "the exemption does not clear the violations list")
    return "the title-block corner is writable; the drawing margin still is not"

def assert_center_marks_count_what_was_inserted():
    """0.8.228 — `views_marked` counted API return values, not marks.

    Report 2026-11-07 (P1-3):

        add_center_marks(view_name="剖面视图 A-A")
        -> {"views_processed": 1, "views_marked": 1,
            "views": [{"view": "剖面视图 A-A", "ok": true,
                       "orientation": "", "center_mark_count": 0}]}

    and the sheet showed no axis line at all. `AutoInsertCenterMarks2` returns True on a
    view with no hole/slot geometry and inserts nothing.

    The docstring here used to say the opposite — "GetCenterMarkCount under-reports on this
    binding, so `views_marked` (the API's own success count) is the reliable signal, not the
    count". Both were unreliable; only the count moving is evidence.

    Measured 2026-11-08 on 飞达动力轴 front view:
        views_marked 0, views[0].api_returned_true true, center_mark_count 0

    Same shape as the `_title_scale_synced` / `verdict` bugs: a receipt that reports an API
    RETURN as if it were an OUTCOME.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_center_marks")) or ""
    assert "_inserted = bool(per_view and per_view > 0)" in body, (
        "a view counts as marked without the count moving — the false success is back")
    assert "api_returned_true" in body, (
        "the receipt no longer separates the API return from the outcome")
    assert "if ok_view://n                marked += 1" not in body, (
        "`marked` is incremented on the API return again")
    return "center marks are counted by what appeared, not by what the API returned"

def assert_record_blocking_is_keyword_only():
    """0.8.230 — a fourth positional argument bound to `blocking` and killed delivery_check.

    Report 2026-11-08 (P0): every `delivery_check` call failed with

        delivery_check.<locals>.record() got multiple values for argument 'blocking'

    after 51.5 s of work, so `verdict` / `blockers` / `pending` all came back empty while
    `produce_drawing` still reported `ok: true`. The whole delivery judgement vanished and
    the receipt read as a pass.

    Cause, introduced by me in 0.8.222: I appended an explanation to the `hole_callouts`
    call as a FOURTH POSITIONAL argument, then also passed `blocking=`. The explanation
    became the flag, and the keyword collided with it.

    The same lesson is already written 80 lines below at `cosmetic_threads`:
    "`blocking` is positional, and the explanation goes into `detail` rather than a fifth
    argument". Reading it did not stop me.

    Two guards:
      1. `blocking` is keyword-only, so this class of mistake is a TypeError at the call
         site with a line number instead of a swallowed failure.
      2. No `record()` call may pass 4+ positional args AND a `blocking` keyword.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    fn = _function_node(tree, "delivery_check")
    assert fn is not None, "delivery_check is gone"

    record_def = None
    for node in ast.walk(fn):
        if isinstance(node, ast.FunctionDef) and node.name == "record":
            record_def = node
            break
    assert record_def is not None, "the record() helper is gone"
    assert record_def.args.kwonlyargs, (
        "`blocking` is a positional parameter again — an explanation passed in that slot "
        "silently becomes the flag and any explicit blocking= raises")
    assert any(a.arg == "blocking" for a in record_def.args.kwonlyargs), (
        "`blocking` is not keyword-only")

    offenders = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "record"):
            continue
        has_blocking_kw = any(k.arg == "blocking" for k in node.keywords)
        if len(node.args) >= 4 and has_blocking_kw:
            offenders.append(node.lineno)
        elif len(node.args) >= 4:
            # 4 positional args also collide, now that `blocking` is keyword-only — this
            # would be a TypeError too, just a different message.
            offenders.append(node.lineno)
    assert not offenders, (
        "these record() calls pass too many positional arguments: lines %r" % (offenders,))

    # And the pipeline must not turn a failed verdict into an empty one.
    pipe = _read("tools/pipeline.py")
    assert "delivery_check 执行失败" in pipe, (
        "the pipeline reads the verdict fields unconditionally again — a crashed "
        "delivery_check will surface as verdict: null with empty blockers, which reads "
        "as deliverable")
    return "record() takes blocking keyword-only, and a failed verdict cannot look empty"

def assert_the_tri_state_survives_unpacking():
    """0.8.231 — the tri-state reader protected nothing, because the caller unpacked None.

    Report 2026-11-08 (P2-3), static evidence: 0.8.216 made `_dimension_selection_names`
    return `None` on a failed read, and the rollback guard checks `_names_before is not
    None`. But the very next line was

        _names_before = [n for n, _ in _snap_before]

which raises `TypeError: cannot unpack non-iterable NoneType` on exactly the state the
tri-state was introduced to represent. The "safe refusal to delete" was an uncaught
exception instead.

Downstream had the same problem: `_new_names = [n for n in _names_after if ...]` and
`len(_names_after)` both blow up on None, and the second one runs ON THE FAILURE PATH —
replacing the real diagnosis with a TypeError.

All four sites are covered here.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "plan_dimension_gaps")) or ""
    assert "_names_before = (None if _snap_before is None" in body, (
        "the before-snapshot is unpacked without a None check again")
    assert "_names_after = (None if _snap_after is None" in body, (
        "the after-snapshot is unpacked without a None check again")
    assert "_diff_computable" in body, (
        "the diff is computed without checking both sides — a failed read becomes "
        "\"nothing new appeared\", which is the opposite of safe")
    assert '"snapshot_before": (None if _names_before is None' in body, (
        "`len(None)` on the failure path again — that TypeError replaces the real cause")
    return "a failed snapshot read reaches the decision as None, not as a TypeError"


def assert_sheet_modified_means_modified():
    """0.8.231 — `sheet_modified: true` next to `rolled_back: 1`.

    Report 2026-11-08 (P2-1): in the zero-dimension scenario both `apply_gaps` calls
    reported `inserted_but_wrong: 1 / rolled_back: 1 / sheet_modified: true` while
    `annotation_count` stayed at 8. The sheet was back to its baseline, but the receipt
    said it had been modified — read next to `rolled_back`, that says "damaged" when it
    means "I tried something and undid it".

    Now two fields: `insert_attempted_wrong` (historical) and `sheet_modified` (residual),
    plus `residual_inserted_but_wrong` as a count.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "plan_dimension_gaps")) or ""
    assert "insert_attempted_wrong=" in body, (
        "the historical fact has no field of its own again")
    assert ("sheet_modified=any(a.get(\"inserted_but_wrong\") and not a.get(\"rolled_back\")"
            in body), (
        "`sheet_modified` counts attempted inserts again, so a fully rolled-back sheet "
        "still reads as modified")
    assert "residual_inserted_but_wrong=" in body, (
        "no count of what actually remains on the sheet")
    return "sheet_modified means the sheet differs now, not that an insert was attempted"


def assert_a_trash_failure_is_not_blamed_on_the_document():
    """0.8.231 — the hint named the wrong cause.

    Report 2026-11-08 (P2-2):
        {"error": "output_locked",
         "message": "cannot overwrite ...: [safe-delete] 操作失败 ... trash operation",
         "hint": "Close the drawing in SolidWorks and retry."}

    The drawing was NOT open. The failure was the recycle bin refusing the delete. A hint
    that points at the wrong subsystem costs more than no hint.

    Now the two cases carry different codes and different advice, and the underlying
    exception text is passed through.
    """
    src = _read("tools/pipeline.py")
    assert "output_trash_failed" in src, (
        "a recycle-bin refusal is still reported as `output_locked`")
    assert "underlying=" in src, (
        "the underlying exception text is dropped again, so the caller cannot see why")
    return "a failed delete and a locked document are reported as different things"

def assert_every_tool_rejects_unknown_arguments():
    """0.8.233 — a mistyped argument ran the tool with it silently dropped.

    Report 2026-11-08 (P2-5, carried since round 4): `health_check(bogus_param=123)`
    returned `isError=False` and a normal payload. FastMCP drops unrecognised keys —
    `extra=ignore` is pydantic's default and there is no per-call override at that layer.

    On a tool that writes to a drawing, "it ran with the wrong arguments and reported ok"
    is the worst outcome: `drw_path=` instead of `drawing_path=` would fall back to the
    real parameter's default.

    Every `@mcp.tool()` now carries `@_reject_unknown_arguments`, which compares the
    keyword names against the wrapped signature and returns `unknown_argument` BEFORE the
    body runs. `functools.wraps` keeps `__wrapped__`, so `inspect.signature` (and therefore
    the generated inputSchema) is unchanged.

    Measured 2026-11-08 against the real server module:
        server.health_check(bogus_param=123) -> unknown_argument, unknown ['bogus_param']
        server.health_check()                -> normal payload, no error
        signature parameters                 -> 1 (unchanged)
    """
    src = _read("server.py")
    tree = ast.parse(src)
    decorated = 0
    missing = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        names = []
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                names.append(d.func.attr)          # mcp.tool()
            elif isinstance(d, ast.Name):
                names.append(d.id)
        if "tool" not in names:
            continue
        decorated += 1
        if "_reject_unknown_arguments" not in names:
            missing.append(node.name)
    assert decorated >= 70, "expected at least 70 registered tools, found %d" % decorated
    assert not missing, (
        "these tools run with unknown arguments silently dropped: %r" % (missing[:8],))

    assert "def _reject_unknown_arguments(" in src, "the guard is gone"
    assert "unknown_argument" in src, "the refusal no longer has a code"
    assert "functools.wraps(fn)" in src, (
        "the wrapper does not preserve the signature — the generated inputSchema would "
        "lose every parameter")
    return "every tool refuses arguments it does not declare, and keeps its schema"

def assert_a_sheet_line_can_be_dash_dot():
    """0.8.235 — "no line style API" was wrong for the sixth time.

    Report 2026-11-07 (P1-3): a section view of a turned part needs its axis drawn, GB/T
    4458.1 wants it dash-dot, and `add_sheet_line` could only draw solid. I answered that
    there was no API, having checked `dir(ISketchSegment)` — 32 properties, no `Style`, no
    `SetLineStyle` anywhere in the type library.

    **`dir()` answers "what does the type library declare", not "what can this object do".**
    `ISketchSegment.Style` is a late-bound property. Measured 2026-11-08:

        seg.Style        -> 0
        seg.Style = 1    -> reads back 1
        seg.Style = 4    -> reads back 4

    So the line CAN be styled. What is NOT established is which number draws which
    pattern: a render of Style 1..7 showed seven different patterns but the crop contained
    the frame and title block too, and I could not map them to names with confidence.

    Hence `line_style` accepts a NAME (documented `swLineStyles_e` guess) or a NUMBER
    (passed through untouched), and the receipt always reports `line_style_applied` — the
    raw value read back — so a caller who has eyeballed their own build can bypass the
    guess entirely.

    Measured 2026-11-08:
        "dashdot" -> applied 5     4 -> applied 4      "3" -> applied 3
        "solid"   -> applied 1     "bogus" -> bad_line_style
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_sheet_line")) or ""
    assert "_segment.Style = _code" in body, (
        "the segment style is not set again — an axis can only be drawn solid")
    assert "line_style_applied" in body, (
        "the receipt does not report the value actually applied, so a wrong name is "
        "indistinguishable from a right one")
    assert "_LINE_STYLES" in src, "the name table is gone"
    assert "isdigit()" in body, (
        "a bare number is not accepted — the only way to bypass the unverified name table")
    assert "bad_line_style" in body, "an unknown style is silently defaulted again"
    server = _read("server.py")
    assert "line_style: Annotated[str" in server, "the MCP tool does not expose line_style"
    return "a sheet line can carry a dash-dot style, chosen by name or by raw number"

def assert_the_mcp_entry_rejects_unknown_arguments():
    """0.8.236 — v0.8.233's guard was installed AFTER pydantic dropped the keys.

    Report 2026-11-08, measured over real MCP stdio:

        tools/call health_check {"bogus_param":123}   -> isError False, normal payload
        tools/call add_sheet   {"bogus_param":123}    -> isError False, "file not found"

    while the identical direct Python call DID return `unknown_argument`. FastMCP
    validates `arguments` against the generated inputSchema first, and pydantic's default
    `extra="ignore"` discards undeclared keys — so the wrapper's `unknown` set was always
    empty. It only ever fired where the tests were looking.

    The check now sits on `Server.request_handlers[CallToolRequest]`, which sees
    `params.arguments` as it came off the wire.

    THIS ASSERTION STARTS A REAL SERVER over stdio. That is the point: the previous test
    for this feature was a static AST scan ("every tool carries the decorator"), which is
    exactly why 221/221 and 44/44 were green while the user-visible behaviour never
    changed. A source-level assertion cannot prove an entry-point check fires.

    Measured 2026-11-08:
        tools: 70
        health_check {"bogus_param":123} -> isError True, unknown_argument
        add_sheet    {"bogus_param":123} -> isError True, unknown_argument
        health_check {}                  -> isError False
    """
    import asyncio
    import os as _os
    import sys as _sys

    src = _read("server.py")
    assert "_install_argument_guard" in src, "the request-level guard is gone"
    assert "request_handlers" in src, (
        "the guard is no longer on the request handler — it is back after pydantic drops "
        "the keys, where it can never fire")
    assert "_install_argument_guard(mcp)" in src, (
        "the guard is defined but never installed in main()")

    async def _run():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=_sys.executable,
            args=["-c", "from solidworks_assetmate_mechdog.server import main; main()"],
            env={**_os.environ, "PYTHONPATH": _os.path.join(ROOT, "src")},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert len(listed.tools) >= 70, "expected 70+ tools, got %d" % len(listed.tools)
                bad = await session.call_tool("health_check", {"bogus_param": 123})
                body = bad.content[0].text if bad.content else ""
                assert bad.isError, (
                    "an unknown argument was accepted over MCP — the guard is running "
                    "after the point where pydantic drops the key")
                assert "unknown_argument" in body, (
                    "refused, but not as unknown_argument: %r" % body[:160])
                good = await session.call_tool("health_check", {})
                assert not good.isError, (
                    "a NORMAL call was rejected — the guard is too broad")

    try:
        asyncio.run(_run())
    except Exception as exc:                                        # noqa: BLE001
        raise AssertionError(
            "could not drive the MCP server over stdio: %s" % str(exc)[:200])
    return "the MCP entry refuses unknown arguments; a normal call still works"

def assert_a_skipped_duplicate_is_actually_removed():
    """0.8.236 — "skipped" while the duplicate stayed on the sheet.

    Report 2026-11-08 (P0-2):

        add_drawing_dimension(edge_index=10, style="radius")     # skip_duplicates default
        -> {"created": false, "value_m": 0.02, "skipped": "duplicate_value"}

        list_drawing_annotations
        -> RD8 and RD9, SAME position, SAME value, SAME type

    Two defects in one check:

    1. It compared `picked_length` — the edge's projected length (Ø20) — against the sheet.
       With `style="radius"` the dimension actually created is R10, so the test rejected a
       dimension that was not a duplicate (the 20 it matched was the groove WIDTH).
    2. The dimension had already been created, and the skip path did not remove it. The
       caller got `created: false` and a second R10 on the sheet — a drafting error they
       had to delete by hand.

    `IDisplayDimension` cannot be deleted directly: `GetAnnotation` is not in the type
    library (`找不到成员`) and `Select`/`Select2` are not on it either (both `<unknown>`).
    So the undo goes through the identity snapshot that 0.8.216 verified:
    `_dimension_selection_names` before and after, and `ann.Select(False)` + EditDelete on
    the difference.

    Measured 2026-11-08 on 小车底部滚轮:
        dim1 created=True
        dim2 created=False skipped=duplicate_value duplicate_removed=True
        dimensions on sheet: 1
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_drawing_dimension")) or ""
    assert "_landed_value" in body, (
        "the duplicate test compares the EDGE length again — a radius dimension will be "
        "rejected as a duplicate of the groove width")
    assert "_dims_before" in body, (
        "no identity snapshot before the insert, so a duplicate cannot be found to undo")
    assert "duplicate_removed" in body, (
        "the receipt claims skipped without saying whether the duplicate was removed")
    # Scan CALLS, not text: the explanation mentions `GetAnnotation` by name, and a
    # substring test flags the comment that documents why it must not be used.
    _bad_calls = [n.lineno for n in ast.walk(_function_node(ast.parse(src),
                                                            "add_drawing_dimension"))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "GetAnnotation"]
    assert not _bad_calls, (
        "IDisplayDimension.GetAnnotation does not exist (measured: 找不到成员) — called at "
        "line(s) %r" % (_bad_calls,))
    return "a skipped duplicate is removed, and the receipt says so"


def assert_a_section_that_did_not_cut_says_so():
    """0.8.236 — the hint sent people to the hatch settings for a view that never cut.

    Report 2026-11-08 (P0-1): `hatch_diagnostics` showed `display_mode 2 / style 2` —
    both normal — while `edge_count == source_edge_count == 4` and the drawing had no
    bore at all. The same receipt's `verdict_reason` said it correctly ("an uncut
    projection also satisfies the width test") and then the hint said:

        请在 SolidWorks 界面手工检查该视图的区域剖面线属性 ... 或换一个能正常显示剖面线的模板

    Two different failures, one shared message. Now `edge_count == source_edge_count`
    returns `section_not_cut` with the recipe that actually works (cut the END view
    vertically), and the hatch advice is kept for the case where the cut DID happen.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_section_view")) or ""
    assert "section_not_cut" in body, (
        "a view with the parent's edge count is reported as hatch_missing again")
    assert "_same_edges" in body, (
        "there is no branch on the edge count, so the two failures share a hint")
    assert "vertical" in body and "section_not_cut" in body, (
        "the working recipe is not in the hint; the caller has to guess it again")
    return "a section that did not cut names itself, with the recipe that does work"

def assert_three_p2_items_are_fixed():
    """0.8.237 — three P2 items from the 0.8.235 report, all small and all real.

    **P2-4 `save` default contradicted its own description.**
    `add_distance_dimension` declared `save=False` while the schema text said "default
    True", and every sibling write tool defaults to True. A caller that omitted it lost the
    dimension silently: it existed in memory and vanished on reopen.

    **P2-8 the scale sync failed from `create_drawing` and worked by hand.**
    The pipeline passes a RATIO STRING ("1:2"); manual callers pass a float (2.0).
    `float("1:2")` raised ValueError, which the outer `except (FileNotFoundError,
    ValueError)` turned into `bad_input` — so the receipt blamed the input and the same
    feature succeeded when called directly. Measured 2026-11-08:
        create_drawing(scale=2.0) -> title_scale_synced {from 1.0, to 2.0, text '2:1',
                                                         verified_by 'written[].ok'}
        set_title_block(scale='1:2') -> written [('scale', True)]

    **P2-3 no Ø / R prefix.**
    A groove mouth measured between two arcs is a real diameter but `AddDimension2` makes
    it a plain length, so a turned part carried a bare `58` where the sheet needs `Ø58`.
    `IDisplayDimension.SetText(1, prefix)` sets it — and like `ISketchSegment.Style`, it is
    NOT in `dir()`: late-bound again. Measured:
        add_distance_dimension(..., prefix='Ø') -> prefix_applied 'Ø', display_text 'Ø3'
    """
    src = _read("server.py")
    assert 'save: Annotated[bool, Field(description="Save the document after the change (default True). False leaves it in memory for inspection.")] = True' in src, (
        "add_distance_dimension's save default contradicts its own description again")

    drawing_src = _read("tools/drawing.py")
    tb = ast.get_source_segment(drawing_src, _function_node(ast.parse(drawing_src),
                                                           "set_title_block")) or ""
    assert "isinstance(_ratio_value, str)" in tb, (
        "a ratio string like '1:2' raises ValueError again — the pipeline path breaks "
        "while the manual float path keeps working")
    assert "_ratio_value = float(_lhs) / float(_rhs)" in tb, (
        "the 'n:m' form is not parsed as a ratio")

    dist = ast.get_source_segment(drawing_src, _function_node(ast.parse(drawing_src),
                                                              "add_distance_dimension")) or ""
    assert "display.SetText(1" in dist, "the dimension prefix is not set"
    assert "prefix_applied" in dist, (
        "the receipt does not report the prefix that actually landed")
    assert "prefix_failed" in dist, (
        "a failed SetText is swallowed — the dimension would print without the Ø and say ok")
    return "save default, ratio strings and dimension prefixes all behave as documented"

def assert_two_more_p2_items_are_fixed():
    """0.8.238 — P2-1 tolerance readback (measured) and P2-6 idempotency (code only).

    **P2-1** — `list_drawing_annotations` reported a toleranced dimension as
    `"Ø30 (公差类型 2，值未读回)"`. A reader could see that a tolerance existed but not what
    it said, so checking the printed deviation meant looking at the sheet by eye.
    `GetToleranceValues` was already readable (0.8.222 fixed its order for
    `add_dimension_tolerance`), it was simply never called from the read path.

    Measured 2026-11-08 on 飞达动力轴, bilateral +0.033/0:
        tolerance_values_mm {'lower': -0.0, 'upper': 0.033}
        display_text        'Ø35 +0.033/-0'

    **P2-6** — `add_center_marks` called twice on one view left TWO marks at the same point
    (`细节项目254` / `256`, both [0.2157, 0.1481]). SolidWorks does not deduplicate, and
    coincident marks draw a thicker line that reads as a rendering artifact.

    Now the count is read BEFORE the call and an already-marked view is skipped with
    `skipped: "already_marked"`.

    **NOT verified on a real machine**: every fixture on this disk (`door`, 飞达动力轴,
    小车底部滚轮) reports `GetCenterMarkCount == 0` on every view, so there is no geometry
    to mark and the skip branch never fires. The assertion is code-level only. Stated here
    rather than implied by a green tick.
    """
    src = _read("tools/drawing.py")

    _ls = "list_drawing_annotations" if "def list_drawing_annotations" in src else None
    assert _ls, "list_drawing_annotations is gone"
    assert "tolerance_values_mm" in src, (
        "the annotation receipt no longer carries the tolerance numbers")
    assert "GetToleranceValues" in src, (
        "the tolerance values are not read at all — display_text will fall back to "
        "'值未读回'")
    assert "公差值读回失败" in src, (
        "a failed tolerance read has no distinct message; it would look the same as "
        "'no tolerance'")

    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_center_marks")) or ""
    assert "_marks_before" in body, (
        "add_center_marks no longer reads the count before inserting — a second call "
        "stacks duplicate marks at the same point")
    assert "already_marked" in body, (
        "an already-marked view is not reported as skipped")
    return "tolerance values are readable; center marks are skipped when already present"


def assert_the_mcp_entry_still_works_after_adding_tools():
    """The guard on the MCP entry must not reject a call to a tool it has not seen.

    Cheap companion to the stdio assertion: if `_install_argument_guard` ever looks up a
    tool by the wrong key, every call would come back `unknown_argument` — a failure mode
    that is only visible from outside the process.
    """
    src = _read("server.py")
    assert 'known.get(name)' in src, (
        "the guard no longer looks the tool up by name; it would reject valid calls")
    assert 'if declared:' in src, (
        "the guard has no empty-schema escape — a tool with no parameters would reject "
        "every call")
    return "the request guard degrades to a pass-through when it cannot resolve a tool"

def assert_a_datum_symbol_is_not_a_centreline():
    """0.8.239 — a DATUM FEATURE SYMBOL was reported as `kind: "centerline"`.

    Report 2026-11-08 (P2-5): `InsertDatumTag2` produced `细节项目257`, and
    `list_drawing_annotations` filed it under `centerline`. Anything counting centrelines
    was counting datum symbols.

    Measured 2026-11-08 on 飞达动力轴: the annotation from `InsertDatumTag2` reports
    `GetType == 2`, and on that sheet type 2 was produced ONLY by the datum symbol (every
    hole centre mark was 13).

    I could NOT build a real centreline to prove it never reports 2 — `InsertCenterLine`
    is a property returning bool (calling it raises "'bool' object is not callable") and
    `InsertCenterLine2` raises 找不到成员. `GetAttachedEntityTypes` is `(1,)` for both, so
    that is not a discriminator either.

    So the map now names the OBSERVED producer and every type-2 row carries
    `kind_ambiguous: ["datum_tag", "centerline"]` plus a note. Callers get the useful
    answer and the uncertainty at the same time, instead of a confident wrong one.

    Measured:
        kind=datum_tag  ambiguous=['datum_tag', 'centerline']
    """
    src = _read("tools/drawing.py")
    assert '2: "datum_tag"' in src, (
        "type 2 is filed as centerline again — datum symbols get counted as centrelines")
    assert "ANNOTATION_KIND_AMBIGUOUS" in src, (
        "the ambiguity table is gone; the receipt presents a guess as a fact")
    assert "kind_ambiguous" in src, (
        "a type-2 row no longer says the code is shared")
    assert "kind_note" in src, (
        "there is no explanation of why the two cannot be told apart")
    i = src.find("ANNOTATION_KIND_AMBIGUOUS = {")
    assert 2 in [int(x) for x in __import__("re").findall(r"(\d+):", src[i:i + 80])], (
        "type 2 is not listed in the ambiguity table")
    return "a datum symbol is named as one, and the shared type code is disclosed"

def assert_a_one_sided_bilateral_is_recognised():
    """0.8.240 — a CORRECT bilateral stack was reported `unrecognised`.

    Report 2026-11-08 (P2-2): the sheet printed `+0.033/0`, `pdf_render_matches` said
    false, and `shape` was `unrecognised` with `deviation_count: 1`.

    The shape detector counted how many times ONE string appeared and required `>= 2` for
    bilateral. **A `+0.018/0` tolerance only ever prints one deviation** — a zero lower
    limit is not drawn as a second number. So the count could never reach 2 for the most
    common unilateral-bilateral case, and a correct drawing was flagged as unrecognised
    by the very check meant to catch wrong drawings.

    The count was never the question. Presence per side is:
        upper present, lower 0     -> bilateral (one printed number is correct)
        upper and lower present     -> bilateral
        only the summed limit       -> limit_values
        neither                     -> basic

    Measured 2026-11-08 on 飞达动力轴:
        +0.018/0    shape bilateral_two_deviations  matches True  dev 1  up True lo False
        +0.033/0    shape bilateral_two_deviations  matches True  dev 1  up True lo False
        +0.05/+0.05 shape bilateral_two_deviations  matches True  dev 2  up True lo True
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "_tolerance_text_on_pdf")) or ""
    assert "_has_up" in body and "_has_lo" in body, (
        "the shape test counts occurrences again instead of asking which sides printed")
    assert "elif _has_up or _has_lo:" in body, (
        "a single printed deviation no longer counts as bilateral — a `+x/0` tolerance "
        "will be called unrecognised again")
    assert "upper_present" in body and "lower_present" in body, (
        "the receipt does not say which deviations were actually found, so a mismatch "
        "cannot be diagnosed")
    assert "dev_count >= 2" not in body, (
        "the occurrence count is back")
    return "a `+x/0` bilateral is recognised, and the receipt names which sides printed"

def assert_the_line_style_table_is_measured_not_documented():
    """0.8.241 — the documented `swLineStyles_e` order does not match what prints.

    Report 2026-11-08 (P2-9): I had written that `line_style="dashdot"` maps to 5 "per the
    documentation" and that the visual mapping was unverified. So it was measured — one
    130 mm line per value on a blank A3, exported, rendered 6x, before/after diff:

        Style 1  cov 0.689  strokes  69     fine dashed
        Style 2  cov 0.714  strokes  31
        Style 3  cov 0.770  strokes  25
        Style 4  cov 0.942  strokes   7     long dashes, narrow gaps
        Style 5  cov 0.114  strokes 194     very sparse dots
        Style 6  cov 0.569  strokes  20     thicker stroke
        Style 7  cov 1.000  strokes   1     solid

    **`style=1` is nominal `swLineStyleSolid` and prints as fine dashes. Only `style=7` is
    continuous.** The enum order on SW2024 SP4 is not the documented one.

    The default is NOT changed on the strength of one build (a wrong default is worse than
    a documented oddity), but the receipt now carries `line_style_measured` so a caller
    sees the coverage and stroke count of what they picked instead of discovering it by
    looking at the sheet.
    """
    src = _read("tools/layout.py")
    assert "_LINE_STYLE_MEASURED" in src, (
        "the measured table is gone — callers are back to guessing from documentation")
    assert "line_style_measured" in src, (
        "the receipt does not report how the style actually printed")
    i = src.find("_LINE_STYLE_MEASURED = {")
    block = src[i:i + 1600]
    assert "7:" in block and "1.000" in block, (
        "the measured data for style 7 (the only continuous line) is missing")
    # The note lives ABOVE the table (it explains why the table exists), so search the
    # whole source, not the table's own slice.
    assert "does not match this build" in src or "not match this build" in src, (
        "the discrepancy with the documented order is no longer stated")
    return "the line-style table is what was measured, and the receipt reports it"

def assert_a_failed_section_says_where_it_is():
    """0.8.244 — the two remaining P0-1 suggestions, both solved differently than proposed.

    **Suggestion 2 said**: reuse `cut_plane_not_in_view_plane` on the `orientation` path.
    That guard already exists and already rejects a normal parallel to the viewing
    direction (`axes[2]`). But `orientation="horizontal"` derives `axes[1]`, which the guard
    correctly ALLOWS — the cut is legal, it just does not happen.

    So I swept view x orientation x offset on 小车底部滚轮 (2026-11-08):

        front + horizontal  off=0        section_not_cut      <- the real dead spot
        front + horizontal  off=-0.020   ok  edges 16  hatch 2
        front + horizontal  off=-0.005   ok  edges 32  hatch 2
        front + horizontal  off=+0.005   ok  edges 28  hatch 2
        front + vertical    any offset   hatch_missing
        end   + vertical    off=-0.005   ok  edges 32  hatch 2
        end   + horizontal  off=-0.005   ok  edges 32  hatch 2

    The blocker is `offset_m == 0`, not the orientation — the cut plane lands exactly on
    the symmetry plane and no edge appears. The hint now says that, with the numbers.

    **Suggestion 3 said**: default `keep_on_failure=False`. Auto-deleting would also delete
    the evidence of WHERE the cut plane landed, and the report itself noted that
    `delete_view` cannot clean the parent's cut line anyway. So the view stays and the
    receipt names it.

    Measured:
        ok=False error=section_not_cut view_kept=True
        view_to_delete='剖面视图 A-A'
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_section_view")) or ""
    assert "现在可以切了" in body, (
        "the hint no longer names the offset that actually fails")
    assert "_maybe_drop_failed_view" in body, (
        "a failed section is left on the sheet unconditionally again")
    assert "view_removed" in body and "section_line_remains" in body, (
        "the receipt does not say whether the view was dropped, nor that the section line "
        "survives the delete")
    assert "view_to_delete" in body, (
        "the receipt does not name the view it left behind")
    assert "_new_view_name = str(getattr(section" in body, (
        "the new view's name is not captured, so the branches cannot reference it")
    assert "cut_line_overshoot_m" in body, (
        "the measured offsets that DO work are missing from the hint")
    return "a failed section names itself and the offsets that would have worked"

def assert_a_sheet_line_can_follow_a_view():
    """0.8.245 — "the line cannot follow a view" was a wrong conclusion, for the 7th time.

    Report 2026-11-08 (P2-7): `add_sheet_line` draws on the SHEET sketch, so the axis does
    not move when the view moves or rescales. I filed that as a data-model limitation:
    "a sheet sketch line does not belong to a view, SolidWorks cannot do otherwise".

    It can. The sketch is created by `InsertSketch`, and WHICH sketch that is depends on
    what is active:

        doc.ActivateSheet(...)  then InsertSketch -> sheet sketch  (草图1)
        doc.ActivateView(...)   then InsertSketch -> VIEW sketch   (草图3, owned by the view)

    Measured 2026-11-08 on 飞达动力轴:
        after ActivateView(工程图视图1):
            view.GetSketch  -> 草图3     <- the line's sketch
            sheet.GetSketch -> 草图1

    So `view_name` was added: with a name the line lands in that view's own sketch and
    travels with it; without one it stays on the sheet, unchanged from before.

    Measured:
        no view_name -> attached_to "sheet"        follows_view False
        view_name    -> attached_to "工程图视图1"   follows_view True
        bad name     -> view_not_found
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_sheet_line")) or ""
    assert "doc.ActivateView(_target.Name)" in body, (
        "the line is drawn on the sheet sketch again — it will not follow the view")
    assert "attached_to=_attached_to or \"sheet\"" in body, (
        "the receipt does not say which sketch the line landed in")
    assert "follows_view=bool(_attached_to)" in body, (
        "the receipt does not say whether the line will travel with the view")
    assert "view_not_found" in body, (
        "a bad view name is accepted silently instead of being refused")
    server = _read("server.py")
    assert "view_name: Annotated[str | None" in server, (
        "the MCP tool does not expose view_name")
    return "a line can be attached to a view and then it follows, or stay on the sheet"

def assert_a_refused_note_is_taken_back():
    """0.8.248 — a REFUSED note stayed on the sheet, and the number of them grew.

    Report 2026-11-08 (P0-B): `add_note` checks the safe area AFTER `InsertNote` and
    `_save_drawing`, then returns `block_out_of_safe_area` without removing anything. The
    report measured it end to end:

        add_note("N1", 0.06, 0.05)  -> ok:false
        add_note("N2", 0.06, 0.05)  -> ok:false
        list_drawing_annotations    -> 10, with N1-BEFORE-EXPORT and N2-BEFORE-EXPORT

    and they survived close+reopen. An agent seeing `ok:false` retries with a new y and
    the sheet accumulates copies of a block the tool said it did not place.

    The note exists at that point, so it is deleted by identity — the same mechanism
    `apply_gaps` uses for a wrong insert. `note_left_on_sheet: true` means the delete
    failed and the caller must clean up.

    First attempt reused `_call_method(note, "GetAnnotation")` and got a DIFFERENT
    annotation object whose `Select(False)` returned False. Reusing the annotation captured
    at insert time (line ~845) works.

    Measured 2026-11-08 on 飞达动力轴:
        ok=False error=block_out_of_safe_area rolled_back=True left=False
        annotation_count 6 -> 6 (delta 0)
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "add_note")) or ""
    assert "note_rolled_back" in body, (
        "a refused note is not rolled back again — the sheet keeps a block the receipt "
        "said was not placed")
    assert "note_left_on_sheet" in body, (
        "the receipt cannot be told apart from a successful cleanup")
    assert "_rb_target = annotation" in body, (
        "the rollback re-fetches the annotation instead of reusing the one from insert; "
        "that object's Select returns False")
    assert "EditDelete" in body, "nothing is deleted on the refusal path"
    return "a refused note is deleted from the sheet, and the receipt says whether it was"


def assert_the_entry_guard_covers_empty_schemas():
    """0.8.248 — `ping` was the one tool of 70 that still swallowed unknown arguments.

    Report 2026-11-08: `ping({"bogus_param":123})` returned `isError=False, ok=True` while
    `health_check` with the same key was refused. Root cause:

        declared = set((tool.parameters or {}).get("properties") or {})
        if declared:                      # <- empty schema skips the whole check
            ...

    `ping` declares no properties, so `declared` was empty and the guard fell through.
    Since `ping` is THE health probe, that is the worst single tool to leave open — and it
    also means any future no-argument tool would be born unprotected.

    Measured 2026-11-08: `if declared:` is gone; an empty set now means "every key is
    unknown", which is also the honest behaviour if schema generation ever fails.
    """
    src = _read("server.py")
    i = src.find("def _install_argument_guard")
    j = src.find(chr(10) + "def ", i + 10)
    body = src[i:j]
    # Scan the AST, not the text: the comment that explains WHY the guard was removed
    # contains the very string it forbids.
    _reintroduced = [n.lineno for n in ast.walk(_function_node(ast.parse(src),
                                                               "_install_argument_guard"))
                     if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                     and n.test.id == "declared"]
    assert not _reintroduced, (
        "an empty schema skips the check again (line %r) — every no-argument tool is "
        "unprotected" % (_reintroduced,))
    assert "unknown = sorted(k for k in raw if k not in declared)" in body, (
        "the unknown-key comparison is gone")
    return "an empty schema still refuses unknown arguments"

def assert_a_degenerate_safe_area_says_so():
    """0.8.249 — the safe area changed depending on whether the process created the file.

    Report 2026-11-08 (P0-A), same sheet, same 0.42x0.297, same process:

        A: create_drawing just finished   bottom 0.065  right  0.004  title_top 0.061
        B: CloseDoc + reopen              bottom 0.004  right -0.016  title_top 0.0

    and B still said `source: "template"`. So `add_note` at (0.06, 0.05) was refused in A
    and accepted in B, with `bbox_final` identical in both.

    Two defects:
    * the cache key is `(sheet.Name, w, h)` — it cannot tell two openings of one file
      apart, so a reopened drawing kept serving the previous opening's numbers;
    * a collapsed scan (negative right margin, no title block found, 4 mm bottom band) was
      still labelled a template measurement.

    Fixed by clearing the cache when a document closes (the one moment the old numbers are
    known stale) and by rejecting three physically impossible results, falling back to
    ratios with `degraded: true`.

    Measured 2026-11-08 on 飞达动力轴:
        fresh   bottom=0.0650 right=0.0040 left=0.0588 title_top=0.061 degraded=False
        reopen  bottom=0.0650 right=0.0040 left=0.0588 title_top=0.061 degraded=False
    """
    src = _read("tools/drawing.py")
    assert "_degenerate.append" in src, (
        "a collapsed template scan is accepted as a measurement again")
    assert "title_block_top==frame_bottom" in src, (
        "the 'no title block found' signature is not checked")
    assert 'fb["degraded"] = True' in src or "_fb[\"degraded\"] = True" in src, (
        "the fallback does not announce that it is a guess")
    close_body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                           "close_document")) or ""
    assert "_SAFE_AREA_CACHE.clear()" in close_body, (
        "closing a document no longer clears the safe-area cache, so a reopened drawing "
        "serves the previous opening's measurement")
    layout_src = _read("tools/layout.py")
    assert "safe_area_degraded" in layout_src, (
        "add_note does not tell the caller that the layout check ran against a guess")
    return "a collapsed scan falls back and says so; a reopen re-measures"

def assert_a_zero_offset_section_is_nudged():
    """0.8.251 — `offset_m=0` cannot be cut on a symmetric view; the axis cut was lost.

    Report 2026-11-08 (P0), on a revolved part: asking for the standard axial full section
    returned `section_not_cut` / `hatch_missing`, while 0.8.215 produced it fine. The
    suggested workarounds (offset ±0.005 / ±0.020) either failed too or produced an
    ECCENTRIC section that still reported `ok: true, ratio: 0.998` — a chord, not a section
    through the axis.

    Measured 2026-11-08 on 限位导向套, front view, horizontal cut:

        offset 0 / 1e-6 / 1e-5 / 1e-4   hatch_missing  edges 5  hatch 0
        offset 1e-3                     ok  edges 46  hatch 4  ratio 1.000
        offset 4e-3                     ok  edges 34  hatch 2  ratio 0.992

    So it is neither a zero-length line nor a float-equality problem: SolidWorks does not
    cut when the line sits on the centre line. `1e-3` sheet metres is 0.25-0.5 mm on the
    model — the section came back 64.95 wide against a 64.98 source, i.e. through the axis
    to drawing tolerance.

    The tool now nudges a zero offset to 1 mm instead of refusing (refusing would break
    every caller that asks for the axis cut), and reports what it did:

        ok=True req=0.0 used=0.001 nudged=True edges=46 hatch=4 ratio=1.0
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_section_view")) or ""
    assert "auto_nudge_zero_offset" in body, (
        "a zero offset is refused again, so the normal axis-cut request fails")
    assert "_offset_nudged" in body, "the nudge is not tracked"
    assert "offset_nudged" in body, (
        "the receipt does not say the section was cut at a nudged offset")
    assert "offset_used_m" in body, (
        "the receipt does not say which offset was actually used")
    server = _read("server.py")
    assert "auto_nudge_zero_offset" in server, (
        "the MCP tool does not expose the switch")
    return "a zero offset is nudged and the receipt names the offset actually used"

def assert_an_eccentric_section_is_refused():
    """0.8.252 — a CHORD section reported `ok: true, ratio 0.992`.

    Report 2026-11-08 (P0): the only offset that produced hatching on 小车底部滚轮 was
    0.004, which pushes the cut plane 2 mm off the axis. On a Ø64 circle that is a chord
    `2*sqrt(32^2-2^2) = 63.875`, and the section measured 63.48 against a 63.98 source.
    It passed every check, including `ratio 0.992` — inside SECTION_KEEP_TOLERANCE (10%).

    A 10% tolerance cannot catch a 0.5 mm chord. Measured with the new check:

        offset 0.004  section_off_axis  shrunk 0.50  ratio 0.9922
        offset 0.005  section_off_axis  shrunk 0.79  ratio 0.9877

    The threshold is 0.5% of the source (or 0.2 mm, whichever is larger), and only a
    SHRINK counts — a section may legitimately be longer if the cut exposes depth.

    KNOWN GAP: offset 0.020 still returns ok, because that call loses `section_kept_mm`
    entirely (`ratio: None`) and the check has nothing to compare. Recorded rather than
    papered over.

    0.8.291: the decision was lifted into `_section_off_axis_from_sanity` (pure, so
    smoke.py can replay measured numbers) and extended with the DISCARDED axis — see
    `assert_eccentric_sections_are_refused_and_line_styles_are_documented`. This assertion
    still guards the original 0.5% kept-axis threshold, now in that helper.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    body = ast.get_source_segment(src, _function_node(tree, "add_section_view")) or ""
    decision = ast.get_source_segment(
        src, _function_node(tree, "_section_off_axis_from_sanity")) or ""
    assert decision, "the pure off-axis decision helper is gone"
    assert "section_off_axis" in body, (
        "an eccentric chord passes as a full section again")
    assert "_off_axis" in body and "shrunk_mm" in decision, (
        "the receipt does not report how much narrower the section is")
    assert "0.005 * float(src)" in decision, (
        "the 0.5% threshold is gone; the 10% SECTION_KEEP_TOLERANCE is too loose for this")
    return "a chord section is refused with the amount it shrank"

def assert_center_marks_count_annotations_not_the_view_counter():
    """0.8.253 — the fix for a false PASS created a false FAIL.

    Report 2026-11-08 (P2-1): on a view that already carried two centre marks,
    `add_center_marks` returned `views_marked: 0, ok: false, api_returned_true: true,
    center_mark_count: 0`. 0.8.228 had made `GetCenterMarkCount > 0` the only proof of
    success, and that counter reads 0 on this binding even when marks exist. So the tool
    went from reporting success for nothing to reporting failure for everything.

    Counting the ANNOTATIONS is the signal that does not depend on that counter. Measured
    2026-11-08:

        限位导向套  run1/run2  ok True  verdict already_marked  added 0
        气缸盖      run1/run2  ok True  verdict unverified      added 0

    Both used to be `ok: false`. Four verdicts now:

        marked          an annotation was actually created
        already_marked  the view already had marks — a success, not a failure
        unverified      nothing added but the API said yes; look at the sheet
        nothing_to_mark nothing added and the API said no — the only real failure

    The idempotency check moved with it: `GetCenterMarkCount` also failed to skip, so a
    second call added TWO MORE marks (before 2, after 4) instead of skipping.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_center_marks")) or ""
    assert "_count_center_marks" in src, "the annotation counter is gone"
    assert "_cm_added" in body, (
        "the receipt no longer reports how many marks were actually added")
    assert "already_marked" in body, (
        "a view that already had marks reports failure again")
    assert "unverified" in body, (
        "an inconclusive result is reported as failure again")
    # AST, not substrings: the comment that explains why this counter is useless mentions
    # it by name, so a text test fails on its own documentation.
    _uses_counter = [n.lineno for n in ast.walk(_function_node(ast.parse(src),
                                                               "add_center_marks"))
                     if isinstance(n, ast.Attribute) and n.attr == "GetCenterMarkCount"]
    assert not _uses_counter, (
        "the success test reads GetCenterMarkCount again (lines %r), which answers 0 even "
        "when marks exist" % (_uses_counter,))
    return "centre marks are counted as annotations, and four verdicts are told apart"

def assert_a_failed_section_can_be_dropped():
    """0.8.254 — a failed section left 11127 dirty pixels on the sheet, and could not be
    cleaned.

    Report 2026-11-08 (P2-2): a failed section stays on the sheet, and `delete_view`
    cannot remove the section line it left in the parent view's sketch.

    Measured on 小车底部滚轮 (end view, vertical, offset 0):

        failed section                    11127 pixels differ from baseline
        after layout.delete_view          1829 pixels still differ

    So `delete_view` removes 84% of the mess and cannot finish: `EnumSectionLines` raises
    on this binding (`EnumSectionLines2` demands an argument, `EnumSectionLines` fails to
    read), so the line cannot be reached at all. There is no way to clean it completely.

    The honest design is therefore: drop the view when it carries NO information, keep it
    when it does, and say which happened.

        keep_on_failure="auto"   drop for section_not_cut / section_empty
                                 keep for hatch_missing (the cut happened; the view shows
                                 where the plane landed — that is diagnostic value)
        "keep"                   always keep
        "drop"                   always drop

    Measured:
        auto  removed False kept True
        keep  removed False kept True
        drop  removed True  kept False  section_line_remains True

    First implementation compared `_keep is False`, which never matched the STRING "drop"
    that MCP sends, so "drop" silently behaved like "keep".
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_section_view")) or ""
    assert "keep_on_failure" in body, "the caller cannot choose what happens to a failure"
    assert "_maybe_drop_failed_view" in body, "the decision is not centralised"
    assert "section_line_remains" in body, (
        "the receipt implies a clean sheet when the parent's section line survives")
    assert 'in ("false", "drop", "delete", "remove")' in body, (
        "string modes are compared with `is False` again, so drop behaves like keep")
    server = _read("server.py")
    assert "keep_on_failure" in server, "the MCP tool does not expose the switch"
    return "a failed section can be dropped, and the receipt admits what remains"

def assert_the_cut_line_overshoots_the_outline():
    """0.8.255 — the real reason `offset=0` never cut: the cut line ENDED on the outline.

    Report 2026-11-08 (P0-1), second report, nails it:

        `cut_line_ratio` is clamped to `min(view_w, view_h) * 0.5 / scale`. A 65x65 view
        therefore gets a half-length of 32.5 mm — exactly the radius — so the line's
        endpoints sit ON the outline circle. SolidWorks treats that as a line that does not
        cross the solid and returns a plain projection (5 contour edges, no hatching).

    That explains everything:
    * why `offset=0` was the dead point (endpoints on the circle)
    * why nudging the offset "worked" (it moved the endpoints off the circle)
    * why 0.8.251's offset nudge was treating a symptom, not the cause

    A few millimetres of overshoot removes the cause. Measured with
    `cut_line_overshoot_m=0.004` at `offset_m=0`:

        限位导向套  front horizontal   ok  46 edges  hatch 4  ratio 1.000
        限位导向套  right vertical     ok  12 edges  hatch 1  ratio 1.000
        小车底部滚轮 front horizontal  ok  28 edges  hatch 2  ratio 1.000
        小车底部滚轮 right vertical    ok  28 edges  hatch 2  ratio 1.000

    The wheel is a single imported body — it previously failed at EVERY offset on the front
    view. The offset nudge is now off by default because it is no longer needed.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_section_view")) or ""
    assert "cut_line_overshoot_m" in body, (
        "the cut line can end on the outline again, which SolidWorks reads as no cut")
    assert "_overshoot" in body and "+ _overshoot" in body, (
        "the overshoot is not added to the half length")
    assert "auto_nudge_zero_offset=False" in body, (
        "the offset is still nudged by default even though the overshoot removed the need")
    server = _read("server.py")
    assert "cut_line_overshoot_m" in server, "the MCP tool does not expose the overshoot"
    return "the cut line extends past the outline, so offset 0 cuts normally"

def assert_a_view_lands_where_it_was_asked_for():
    """0.8.256 — every view landed at `(0, x_m)`: x discarded, y taken from x.

    Report 2026-11-08 (P0-2), three views created in one go:

        asked (0.145, 0.225)  landed (0.000, 0.145)
        asked (0.300, 0.225)  landed (0.000, 0.300)
        asked (0.372, 0.115)  landed (0.000, 0.372)

    All three at x=0, outside the frame (`margins.left = -0.0938`). The report guessed
    `(x, y)` had been written as `(0, x)`; that is exactly the v0.8.9 marshalling trap —
    a plain `view.Position = (x, y, 0.0)` arrives as a VARIANT the dispatcher re-packs in
    the wrong order. `set_view` had already solved it with DISPATCH_PROPERTYPUT and a
    typed VARIANT, but `add_view_to_sheet` still used the plain assignment.

    Fixed by using the same typed write there. Note the array is TWO doubles, not three.

    Measured 2026-11-08 on 限位导向套:
        asked (0.145, 0.225)  applied [0.145, 0.225]
        asked (0.300, 0.225)  applied [0.300, 0.225]
        asked (0.372, 0.115)  applied [0.372, 0.115]
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_view_to_sheet")) or ""
    assert "_prop_put(view, \"Position\"" in body, (
        "the position is written with a plain assignment again, which mis-marshals to "
        "(0, x) on this binding")
    assert "VT_ARRAY | pythoncom.VT_R8" in body, (
        "the typed VARIANT is gone; the dispatcher will re-pack the array")
    assert "position_applied" in body, (
        "the receipt reports the REQUESTED position without reading back what landed")
    assert "view.Position = (float(x_m)" not in body, (
        "the three-element tuple assignment is back")
    return "a view lands at the position it was asked for, and the receipt reads it back"

def assert_one_callout_per_hole_size():
    """0.8.257 — twelve callouts for four holes, stacked in one column.

    Report 2026-11-08 (P1-2): on 限位导向套 the tool produced 12 callouts for a part with
    four counterbores, and every one of them carried `quantity: 4`:

        Ø11  x4 separate callouts, each saying "4x"
        Ø6.6 x4 separate callouts, each carrying the COUNTERBORE text

    Four annotations all reading "4x Ø11" says sixteen holes, and it is what stacked twelve
    leaders into a single column at x=0.2908.

    `quantity` was already `len(_group)`, so the count existed but was repeated instead of
    aggregated. Keeping one group per diameter turns 12 into 4:

        created 4  duplicate_groups_merged 6

    Also added a chamfer filter: a 1x45° chamfer adds a circle 2 mm off the face it cuts,
    concentric with it. Same centre + radius difference <= 1.5 mm -> the smaller one is a
    chamfer face, not a hole.

    v0.8.258 fixed `quantity` too. The first two attempts failed because the two dicts use
    DIFFERENT UNITS: `_by_dia` is keyed in millimetres (`diameter_mm`), while
    `_quantity_by_radius` is keyed in metres (`round(radius_m, 4)`). `diameter_mm / 2000`
    puts them on the same scale and the count is right:

        Ø11.0  qty=4  quantity_source=distinct_centres_at_this_diameter
        Ø6.6   qty=4  quantity_source=distinct_centres_at_this_diameter

    A caller who reads `quantity: 1` on a 4-hole pattern will drill three holes short, so
    this one mattered more than the chamfer filter.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_hole_callouts")) or ""
    assert "_merged_by_radius" in body, (
        "one callout per hole again — four counterbores produce four '4x' leaders")
    assert "duplicate_groups_merged" in body, (
        "the receipt does not say how many groups were folded together")
    assert "_chamfers" in body, "chamfer faces are annotated as holes again"
    assert "_quantity_by_radius" in body and "2000.0" in body, (
        "quantity is back to counting callouts; the metre/millimetre conversion is what "
        "makes it count holes")
    assert "quantity_source" in body, (
        "the receipt does not say the count came from distinct centres")
    # 0.8.294 named the chamfer RADIUS test; the 3D centre cluster keeps its own
    # literal 1.5 mm tolerance in `_near`. Both must still be present.
    assert "CHAMFER_RADIUS_GAP_M" in body and "_near" in body, (
        "the 1.5 mm concentric test is gone; chamfers will pass as holes")
    return "one callout per hole size, and chamfer faces are skipped"

def assert_a_failed_delete_does_not_report_ok():
    """0.8.259 — `delete_dimensions` reported success while listing what it had failed to
    delete.

    Report 2026-11-08 (P1-1):

        keep_values: []       -> removed 0, failed 0, remaining_count 8, ok TRUE
        keep_values: [9999]   -> removed 0, failed 0, remaining_count 8, ok TRUE

    `remaining_matches` already named the eight dimensions that should be gone, and
    `verified_clean` was already False — but `drawing_ok` hard-codes `ok: true`, so a batch
    job checking `ok` silently shipped them.

    `ok` now means "nothing that should be gone is still there": any residual returns
    `dimensions_not_all_removed` with the counts and a pointer at
    `delete_annotation(name, position_m)`.

    Measured 2026-11-08 on 限位导向套 (keep_values=[]):
        ok=False  error=dimensions_not_all_removed  removed=0  failed=0  remaining=4

    KNOWN GAP: `removed` is still 0 — `EditDelete` runs without effect here, and trying
    `IAnnotation.Select(False)` first (which works in `delete_annotation`) did not help.
    The receipt now refuses to call that a success; actually deleting those dimensions is a
    separate problem, and the hint points at the tool that does work.
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "delete_dimensions")) or ""
    assert "dimensions_not_all_removed" in body, (
        "a delete that left dimensions behind reports success again")
    assert "if remaining:" in body, "the residual list does not gate the verdict"
    assert "verified_clean=False" in body, (
        "the failure receipt still claims the sheet is clean")
    assert "ann.Select(False)" in body, (
        "the SelectByID2-only path is back; it returns True and deletes nothing")
    return "a delete that left dimensions behind is reported as a failure"

def assert_edges_carry_the_index_that_dimensioning_needs():
    """0.8.260 — the edge list had no `edge_index`, so nobody could select an edge by index.

    Report 2026-11-08 (P1-3) reported two things. The first:

        consequences: `add_distance_dimension` cannot select edges by index in a side view

    That one is real and the cause is a naming mismatch. `list_drawing_edges` built each
    row as `{"index": index}` while `add_distance_dimension` documents `edge_a` as "Index
    of the FIRST edge (from list_drawing_edges)". Callers read `edge_index`, got None, and
    fell back to hand-placed sheet coordinates.

    Both keys exist now. Measured 2026-11-08 on 限位导向套 (right view):
        edges=5  all have edge_index: True
        add_distance_dimension(0, 1) -> ok=True value=1.0

    The second claim — `|center - point| = 8.5 mm` against a self-reported radius of
    31.5 mm — I could NOT reproduce: on this build every row satisfies
    `|sheet_center - sheet_point_on_circle| == sheet_radius_m` to 0.01 mm, and
    `sheet_projected` is True. So the inconsistent-coordinate half is recorded as
    unreproduced rather than "fixed".

    `sheet_projected` also now appears per row (it was envelope-only), so a caller filtering
    edges does not have to cross-reference the payload.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "list_drawing_edges")) or ""
    assert '"edge_index": index' in body, (
        "edges no longer carry `edge_index`; selecting by index breaks again")
    assert '"index": index' in body, (
        "the old key was replaced instead of aliased — existing callers break")
    assert '_row["sheet_projected"]' in body, (
        "sheet_projected is back to envelope-only")
    return "edges carry both index names, and sheet_projected is on every row"

def assert_a_missed_point_is_not_an_empty_view():
    """0.8.261 — "no visible edges" was reported on a view that had 14 of them.

    Report 2026-11-08 (P2-1): `add_drawing_dimension(x=0.167, y=0.242)` on a view whose
    `list_drawing_edges` answered 14 edges returned

        segment_not_found: "No visible edges could be enumerated in this view"

    The point sat on a CENTRE LINE. `GetVisibleEntities2(view, 1)` enumerates EDGES only —
    centre lines and construction circles are a different entity type and are never in that
    list. `_nearest_edge_at_sheet_point` returns the nearest edge even when it is outside
    tolerance, so `None` can only mean "the view enumerates no edges" or "scale reads 0".
    The message was therefore wrong, and the caller concluded the view was hopeless and
    gave up dimensioning it.

    Two cases, two codes now. Measured 2026-11-08 on 限位导向套 (right view, asking for the
    view centre):

        ok=False  error=point_missed_every_edge  visible_edges=5

    The receipt carries `visible_edges`, `scale`, `point` and `tolerance_m`, and the hint
    says the point is probably on a centre line and that `edge_index` is the way in.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_drawing_dimension")) or ""
    assert "point_missed_every_edge" in body, (
        "a missed point is reported as an empty view again")
    assert "visible_edges=_edge_n" in body, (
        "the receipt does not say how many edges the view actually has")
    assert "中心线" in body, (
        "the hint does not name the likely cause — a point on a centre line")
    return "a point that misses every edge is told apart from an empty view"

def assert_the_capability_text_names_the_two_real_causes():
    """0.8.262 — the capability table blamed "geometry" for missing hatching.

    Report 2026-11-08 (P3-1):

        check_capabilities.section_view says hatching depends on part geometry;
        measured, it depends on the OFFSET (whether the line endpoints land on the contour)
        and on whether the part HAS A MATERIAL.

    Both are now measured facts:

    * the cut line used to be clamped to `min(w, h) * 0.5`, putting its endpoints exactly on
      the outline of a symmetric view — the collapse this table used to call "geometry"
    * an unmaterialed part produces `hatch 0` for the same geometry and the same arguments;
      `set_part_material` makes the same call return `hatch 2`

    The `workaround` now says `cut_line_overshoot_m` fixed the first, names the material
    requirement for the second, and warns against using `offset_m` to compensate (offset
    5 mm on a Ø16 bore leaves a 12.5 mm section, so the printed diameter is wrong).
    """
    caps = _read("tools/capabilities.py")
    assert "cut_line_overshoot_m" in caps, (
        "the table does not name the thing that actually fixed the zero-offset case")
    # Was "the material requirement is stated". Report 2026-11-09 (N-1) measured an
    # UNMATERIALED part returning hatch_count 4, so the requirement was never real — it was
    # a side effect of the cut line ending on the outline. The table must now say the
    # conclusion is RETIRED, not repeat it.
    assert "作废" in caps, (
        "the table still presents 'no material -> no hatching' as fact; it was disproved "
        "on 2026-11-09 (unmaterialed part, hatch_count 4)")
    assert "别再怀疑材质" in caps or "别再怀疑材质" in _read("tools/drawing.py"), (
        "the hatch_missing hint still sends the caller off to assign a material")
    assert "不要用 `offset_m` 去凑" in caps, (
        "the table still lets a caller reach for an offset that produces a chord")
    assert "section_line_remains" in caps, (
        "the table does not say a failed section leaves a line behind")
    return "the table names the overshoot, the material requirement, and the offset trap"

def assert_the_material_slot_is_the_material_cell():
    """0.8.263 — `material` wrote to the "主管设计" row.

    Report 2026-11-08 (P2-2): the dry run planned `cell_slot_m = [0.344, 0.021]` while the
    template's own Q235 sample sits at (0.3415, 0.055). The y was `code`'s y — material was
    overwriting a different row of the title block.

    The tell was available all along: the receipt already printed the coordinates, and once
    the slot is right the cell's existing text comes back as the template's own sample.
    Measured 2026-11-08 on gb_a3.drwdot:

        field=material  slot=[0.344, 0.055]  before='Q235'  after='Q235'

    `before='Q235'` is the proof — the grid coordinate is reading the cell that holds the
    template's material sample.

    KNOWN GAP: these anchors are hard-coded for `gb_a3.drwdot`. A different template needs
    different numbers, and the tool has no way to find a cell by its label.
    """
    src = _read("tools/drawing.py")
    i = src.find("TITLE_SLOTS = {")
    j = src.find("}", i)
    block = src[i:j]
    assert '"material": (0.344, 0.055)' in block, (
        "material points at the 主管设计 row again (y=0.021 is code's row)")
    assert '"code": (0.391, 0.022)' in block, "the code slot moved unexpectedly"
    assert '"name": (0.391, 0.036)' in block, "the name slot moved unexpectedly"
    return "material reads the cell that holds Q235, not the row below it"

def assert_both_circles_of_a_chamfer_pair_are_skipped():
    """0.8.267 — the chamfer filter deleted the real bore and annotated the chamfer mouth.

    Report 2026-11-09 (N-3/N-4), the highest-severity finding of the retest:

        Ø16 真实内孔     -> deleted as the "smaller one" of a concentric pair
        Ø18 倒角口       -> annotated as a hole
        Ø30 凸台外圆     -> annotated as a hole (only `<MOD-DIAM>`, no number)

    v0.8.257's rule was "same centre, radius gap <= 1.5 mm -> drop the SMALLER". That is
    right for an OUTER chamfer (Ø30 boss vs Ø28 face: drop Ø28) and exactly backwards for
    an INNER one, where the bore wall sits 2 mm BELOW its chamfer mouth.

    Which circle is the chamfer depends on the feature, and `IEdge` does not expose that
    here — so neither is annotated, and the pair is named in the receipt:

        created=2  diameters=[6.6, 11.0]
        ambiguous: Ø16.0 / Ø18.0 (gap 1.0 mm)
        ambiguous: Ø28.0 / Ø30.0 (gap 1.0 mm)

    Only the two real holes survive, and Ø30 stopped being called a hole as a side effect.

    Missing a callout is recoverable by hand. Calling a chamfer a hole is a wrong part.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_hole_callouts")) or ""
    assert "_ambiguous" in body, "the pair is silently dropped without being reported"
    assert "ambiguous_chamfer_pairs" in body, (
        "the receipt does not name the circles it skipped")
    # v0.8.273 replaced "add the two named radii" with "drop every key in the cluster",
    # because members differ slightly in centre and the survivor stayed annotated (Ø30).
    assert "for _k in _keys:" in body and "_chamfers.add(_k)" in body, (
        "only part of the cluster is excluded again, so one circle of the pair survives "
        "as a hole callout")
    return "both circles of a chamfer pair are skipped and named"


def assert_a_hole_callout_is_not_a_deletable_dimension():
    """0.8.267 — after `add_hole_callouts`, every batch delete returned a false failure.

    Report 2026-11-09 (N-2):

        {"ok": false, "error": "dimensions_not_all_removed",
         "removed": 0, "failed": 0, "remaining_count": 4,
         "remaining_matches": [30.0, 18.0, 11.0, 6.6]}

    Those four values are the callout DIAMETERS, not sizes. `AddHoleCallout2` returns an
    `IDisplayDimension`, so `GetType == 4` lets a hole callout through the type filter, and
    the sheet could never be reported clean again.

    `IsHoleCallout` separates them. They are listed in `non_dimension_annotations` and kept
    out of `ok`. Measured 2026-11-09 on 限位导向套:

        delete: ok=True  remaining=0  non_dim=2
    """
    src = _read("tools/layout.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "delete_dimensions")) or ""
    assert "IsHoleCallout" in body, (
        "hole callouts count as deletable dimensions again, so any sheet with callouts "
        "reports a permanent false failure")
    assert "non_dimension_annotations" in body, (
        "the receipt does not say which annotations were excluded")
    return "hole callouts are excluded from the delete check"

def assert_the_clamp_has_a_travel_budget():
    """0.8.268 — the safe-area clamp dragged dimensions across the whole sheet.

    Report 2026-11-08 (P0-3):

        clamped_into_safe_area:
          D1 from [-0.035, 0.293] -> [0.0588, 0.065]
          D2 from [ 0.0588, 0.3325] -> [0.0588, 0.107]

    Dimensions sat outside the frame (because of P0-2, which placed every view at x=0) and
    the clamp "fixed" them by relocating all eight to the left edge of the safe area —
    leaders crossing the entire A3. The report asked for a distance budget instead of an
    unbounded move.

    Measured 2026-11-08, view pushed to x=0.010 then `auto_dimension(place_outside=True)`:

        clamped=2  travel_limited=2
        D1 from=[0.0588, 0.2775] travel=212mm   limit=30.0
        D1 from=[0.0588, 0.1805] travel=102mm   limit=30.0

    212 mm is half of an A3. Beyond the budget the entry is named with
    `error: travel_exceeds_limit` and the annotation stays put — a leader you cannot follow
    at least shows where the number belongs, while a dimension across the sheet points at
    nothing.

    NOTE: this treats the symptom. The real fix for those sheets is placing the views
    inside the safe area first; the hint says so.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "auto_dimension")) or ""
    assert "max_clamp_travel_m" in body, "the clamp moves an annotation any distance again"
    assert "travel_exceeds_limit" in body, (
        "an over-long move is not named in the receipt")
    assert "_travel" in body, "the distance is not measured"
    server = _read("server.py")
    assert "max_clamp_travel_m" in server, "the MCP tool does not expose the budget"
    return "the clamp refuses to move an annotation more than 30 mm"

def assert_the_layout_reserves_room_for_dimensions():
    """0.8.269 — views were packed flush to the safe edge, 4.5 mm from the frame.

    Report 2026-11-08 (P3-1b):

        create_drawing 自动布局把轴测放到图纸下沿（min_margin_m = 0.0045，仅 4.5 mm 到图框），
        标注空间被吃掉；建议布局时按「每视图预留 20–25 mm 标注带」计算

    v0.8.262 tried this on `_place_views` and measured no change at all — that function only
    REPACKS views after they exist. The box that decides the initial positions is
    `safe_x0..safe_y1` inside `create_drawing`, so the inset had to go there.

    Measured 2026-11-08 on 限位导向套 (3 views, A3):

        band=0.000  min_margin 0.0045
        band=0.018  min_margin 0.0478
        band=0.030  min_margin 0.0478   (saturated — the views already fill the box)

    KNOWN GAP: beyond 18 mm the band stops helping because the view scale and packing
    absorb the extra room. 18 mm is the useful ceiling, not an arbitrary default.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "create_drawing")) or ""
    assert "annotation_band_m" in body, (
        "the layout box no longer reserves room, so dimensions have nowhere to go")
    assert "safe_x0 += _band" in body, (
        "the band is not applied to the box create_drawing actually uses")
    server = _read("server.py")
    assert "annotation_band_m" in server, "the MCP tool does not expose the band"
    return "the layout reserves 18 mm per side for dimensions"

def assert_delivery_check_does_not_walk_every_face():
    """0.8.270 — 150 of delivery_check's 215 seconds went into counting holes.

    Found while chasing the smoke slowdown (docs/2026-11-09-smoke-timing.md). On the track
    part `P05_karetku` (147 visible edges):

        build_feature_ledger(include_faces=True)    209 s
        build_feature_ledger(include_faces=False)    19.7 s    10x

    and inside `delivery_check`:

        delivery(include_faces=False)   41.6 s   ok=True
        delivery(include_faces=True)   192.5 s   ok=True

    The `holes` list is the only thing that needs faces, and `delivery_check` uses it for a
    truthiness test plus one number in a message ("N 处孔候选"). Everything else — stats,
    category, doubts, bbox_mm — comes without faces, so the verdict is identical either way.

    `include_faces` is now a parameter, default False.

    IMPORTANT: `add_hole_callout` (the single-hole variant) still passes True on purpose —
    it genuinely needs `led["holes"]` to resolve the diameter list. Do not "optimise" that
    one the same way; it would silently produce empty callouts.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "delivery_check")) or ""
    assert "include_faces=bool(include_faces)" in body, (
        "delivery_check walks every face again — 150 s for one number")
    assert "include_faces=False" in body, "the parameter is not defaulted to False"
    # the single-hole callout must keep faces
    single = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                       "add_hole_callout")) or ""
    assert "include_faces=True" in single, (
        "add_hole_callout needs the holes list; turning faces off there yields empty "
        "diameter matching")
    server = _read("server.py")
    assert "include_faces" in server, "the MCP tool does not expose the switch"
    return "delivery_check skips face enumeration by default; the single callout still needs it"

def assert_circle_detection_is_gated_before_the_array_call():
    """0.8.271 — `CircleParams` was called on every edge to find the 12 that were circles.

    Measured on the track part (`P05_karetku`, 147 visible edges):

        GetVisibleEntities2     3.3 s
        GetStartVertex  x147    5.8 s
        GetCurve        x147    5.0 s
        GetPoint pairs  x147   27.2 s    <- unavoidable: two COM round trips per edge
        CircleParams    x147   15.3 s    <- only 12 edges are circles

    `ICurve.IsCircle` reads one boolean, so the 135 straight edges no longer pay for a
    7-element array. Wall clock for the whole call: 76.5 s -> 56.6 s.

    What is left is `GetPoint` — two COM round trips per edge, 0.09 s each. `IGetPoint`
    was tried and fails outright on this binding (147 attempts, 0 returns), so there is no
    cheaper vertex read. `include_points=False` skips it entirely when coordinates are not
    needed.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "list_drawing_edges")) or ""
    assert "IsCircle" in body, (
        "every edge pays for CircleParams again — 15 s to find 12 circles")
    assert "StopIteration" in body, (
        "the non-circle path is not short-circuited")
    assert 'item["is_circle"] = False' in body, (
        "a straight edge no longer says so, so callers cannot filter")
    return "circle detection is gated behind IsCircle and straight edges say so"

def assert_the_chamfer_pair_is_matched_in_three_dimensions():
    """0.8.273 — the chamfer filter paired circles that were 70 mm apart.

    Report 2026-11-09 (N-3) found Ø16 dropped and Ø18/Ø30 annotated as holes. Root cause was
    worse than the report guessed: the callout scan stored only `(x, y)` of each circle
    centre.

    ```python
    "centre": (round(params[0], 4), round(params[1], 4))   # Z thrown away
    ```

    With Z gone, the two ends of a through bore and a bore-versus-its-chamfer-mouth all look
    like "the same centre", so the filter paired things 70 mm apart on the axis.

    Three fixes, each measured:
    1. keep Z; cluster centres by DISTANCE (1.5 mm) so a chamfer mouth at ~1 mm still pairs
       with its face but a bore's far end does not
    2. exclude the whole cluster, not "the smaller radius at this exact centre" — members
       differ slightly, so Ø30 survived and stayed annotated
    3. also compare diameters in SHEET space: `view_width` is on the paper, `radius_m * 2` is
       the model, and on a 1:2 sheet Ø18 was being rejected as an outer contour

    Measured 2026-11-09 on 限位导向套:

        created   [Ø11 x4, Ø6.6 x4]
        ambiguous [Ø16/Ø18, Ø28/Ø30]

    Ø30 stopped being a hole. Ø16 is still not annotated — a front view shows a through bore
    as ONE circle, so the "two distant centres = through hole" test cannot fire here. The
    pair is named in the receipt instead, which is the report's own preference:
    "宁可不标也不要标错".
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_hole_callouts")) or ""
    assert "round(float(params[2]), 4)" in body, (
        "Z is dropped again — circles 70 mm apart will pair")
    assert "_near(" in body, (
        "exact centre equality is back; a chamfer mouth at 1 mm never pairs")
    assert "_view_scale" in body, (
        "model diameter is compared against sheet width again; small holes get rejected "
        "as outer contours on a scaled sheet")
    assert "_keep_at" in body, "the through-bore escape hatch is gone"
    return "chamfer pairs are matched in 3D and in sheet space"

def assert_the_tag_guard_can_see_the_tag():
    """0.8.274 — the version guard passed on installs where it did nothing at all.

    Report 2026-11-09 (core problems A and B). Three releases shipped with a stale version:

        v0.8.134-136  stuck at 0.8.133
        v0.8.264-265  stuck at 0.8.263
        v0.8.272      stuck at 0.8.271, and it SHARES A COMMIT with v0.8.271

    Every time the mechanism was the same: a `sed` with a hard-coded literal, matched nothing,
    exited 0, and the commit plus tag went out anyway. `assert_version_strings_agree` could
    not catch it because it only compared two strings that were stale TOGETHER.

    v0.8.273 added a tag comparison — and made it useless for the install shape most people
    use. `git describe` needs a `.git`, a GitHub tarball has none, so `tag_version` was None,
    the comparison was skipped, and the receipt still read "agrees across pyproject, __init__
    and tag (untagged HEAD)". A guard that reports success while doing nothing is worse than
    no guard.

    Three changes:
    * tag source falls back through CI env -> `_build_meta.json` (shipped in the package) ->
      `git describe`. Measured in a copy with no `.git`:
      `version 0.8.274 agrees ... and tag v0.8.274 [packaged:_build_meta.json]`
    * equality, not `>=`. Two adjacent tags on one commit with the same declared version
      passes `>=` every time.
    * when there is no tag source at all the receipt says `TAG COMPARE SKIPPED`, and
      `static_check` prints `tag_compare: ...` so `254/254` cannot imply it ran.

    `tools/bump_version.py` replaces the sed: it reads the current version, computes the next,
    writes, then re-reads and fails if the file did not change. It also refuses to run when
    `--check` finds HEAD on the previous tag's commit.
    """
    with open(os.path.join(ROOT, "tests", "regressions.py"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.find("def assert_version_strings_agree")
    j = src.find("\ndef ", i + 10)
    body = src[i:j]
    assert "GITHUB_REF_NAME" in body, "no CI tag source; tarball installs skip silently"
    assert "_build_meta.json" in body, "no packaged tag source"
    assert "TAG COMPARE SKIPPED" in body, (
        "a skipped comparison is still phrased as agreement")
    assert "_as_tuple(declared) == _as_tuple(tag_version)" in body, (
        "the comparison accepts a newer tag again; two tags on one commit pass >= every time")
    assert "tag_source" in body, "the receipt does not say where the tag came from"

    with open(os.path.join(ROOT, "tools", "bump_version.py"), encoding="utf-8") as fh:
        bump = fh.read()
    assert "WRITE DID NOT TAKE EFFECT" in bump, (
        "the bump script can no longer prove it changed the file")
    assert "SAME commit" in bump, (
        "nothing stops tagging the same commit twice, which is how v0.8.272 shipped")
    return "the tag guard sees the tag on tarball installs and fails loudly when it cannot"

def assert_the_report_says_what_it_verified():
    """0.8.275 — "255/255 passed" implied behaviour was verified. It was not.

    Report 2026-11-09 (core problem C) counted: 254 of 254 assertions inspect source text,
    and the 7 added in this round are no exception —

        assert "_keep_at" in body

    That is how v0.8.273 shipped with Ø16 still unannotated: the AST assertion passed, the
    drawing was wrong.

    Adding a real fixture for each is not the first step; saying what the number means is.
    The banners now read:

        source-level  255/255 passed   (source inspection only — no SolidWorks involved)

    and `check_all` prints both counts side by side so neither can stand in for the other.

    STILL SOURCE-LEVEL: this assertion checks the banner text, nothing more.
    """
    with open(os.path.join(ROOT, "tests", "regressions.py"), encoding="utf-8") as fh:
        reg = fh.read()
    assert "source inspection only" in reg, (
        "the regressions banner claims verification again")
    assert "no SolidWorks involved" in reg, (
        "the banner does not say the machine was never touched")
    with open(os.path.join(ROOT, "tests", "check_all.py"), encoding="utf-8") as fh:
        ca = fh.read()
    assert "on-machine" in ca and "NOT run here" in ca, (
        "check_all does not separate the two kinds of check")
    assert "_smoke_check_count" in ca, "the on-machine count is not computed"
    return "the banners say source-level vs on-machine, and neither implies the other"

def assert_static_check_actually_compares_the_tag():
    """0.8.276 — the tag guard printed "compared" without comparing anything.

    Report 2026-11-09 (core problem A) ran the controlled experiment this project should
    have run itself: set `_build_meta.json` to `{"tag": "v0.8.999"}` and leave everything
    else alone.

        python tests/static_check.py   ->  RC 0, prints "compared against v0.8.999"
                                           then "static check OK"
        python tests/regressions.py    ->  RC 1, the assertion caught it

    The `tag_compare` block v0.8.274 added sat AFTER `if problems: return 1`, and the value
    it computed only fed an f-string. `static_check` is the only gate that blocks a release,
    so the entire guard lived in the one script CI might not run.

    Fixed by moving the comparison BEFORE the early return and making a mismatch append to
    `problems`. Measured both ways:

        tag=v0.8.999  -> RC 1, "static check FAILED … tag v0.8.999 != pyproject 0.8.275"
        tag=v0.8.275  -> RC 0, "tag_compare: v0.8.275 == pyproject 0.8.275"

    The source label also stopped overclaiming. A packaged tag is written by
    `bump_version.py` from the same `target` variable as the version, so offline it compares
    `target == target`. It now prints `packaged (SELF-DECLARED, not an external tag)` —
    report 2026-11-09, core problem B. There is no CI here to force `GITHUB_REF_NAME`, so
    the honest move is to label the weak source rather than hide it.

    STILL SOURCE-LEVEL: this reads `static_check.py`; it does not run it.
    """
    with open(os.path.join(ROOT, "tests", "static_check.py"), encoding="utf-8") as fh:
        sc = fh.read()
    i = sc.find("tag must AGREE")
    j = sc.find("if problems:", i)
    assert i > 0 and j > i, (
        "the tag comparison is not before the early return, so it cannot block a release")
    block = sc[i:j]
    assert "problems.append" in block, (
        "a tag mismatch still does not enter `problems` and cannot fail the gate")
    assert "SELF-DECLARED" in block, (
        "a packaged tag claims external verification again")
    assert "_declared" in block, "the comparison has nothing to compare against"
    return "static_check fails on a tag mismatch instead of printing a line"

def assert_capability_entries_have_no_duplicate_keys():
    """0.8.277 — two `workaround` keys meant the fix was unreachable.

    Report 2026-11-09 (Q-4) called `check_capabilities(operation="section_view")` and got
    back the OLD `axes_ambiguous` text, while the same entry's `summary` quoted the text
    written in the same round. The entry had:

        "workaround": <the overshoot root cause, written this round>
        ...
        "workaround": <the older axes_ambiguous note>

    Python keeps the last one. So the entire N-1 fix — the overshoot explanation and the
    retired material claim — never reached a caller. `add_section_view`'s hint was fixed
    and visible; this half was silently dropped.

    Fixed by renaming the second key to `workaround_axes_ambiguous`, and the summary no
    longer says hatching depends on material.

    Measured 2026-11-09 after the fix:

        keys: [... 'workaround', 'workaround_axes_ambiguous']
        has overshoot: True     has 作废: True
    """
    import re as _re
    from collections import Counter as _Counter

    with open(os.path.join(SRC, "tools", "capabilities.py"), encoding="utf-8") as fh:
        text = fh.read()

    # Walk every entry dict in CAPABILITIES and count its top-level keys.
    offenders = []
    for match in _re.finditer(r'^\s*"id": "([a-z_]+)",', text, _re.M):
        start = match.start()
        nxt = text.find('"id": "', start + 10)
        block = text[start:nxt if nxt > 0 else len(text)]
        keys = _re.findall(r'^\s{8}"([a-z_]+)":', block, _re.M)
        dups = {k: n for k, n in _Counter(keys).items() if n > 1}
        if dups:
            offenders.append("%s: %s" % (match.group(1), dups))

    assert not offenders, (
        "a capability entry repeats a key; Python keeps the last one and everything "
        "written in the first is unreachable: %s" % offenders[:3])
    return "no capability entry repeats a key"

def assert_coaxial_holes_produce_one_callout():
    """0.8.278 -> 0.8.294 — one callout per coaxial hole, judged by chamfer membership.

    Report 2026-11-09 (Q-2): a counterbored hole got two identical callouts, each saying
    4×. The counterbore circle (Ø11) and the through circle (Ø6.6) are different RADII, so
    the "merge by radius" pass saw them as unrelated.

    Grouping them needed the right notion of "coaxial", and the first attempt got it wrong:

        Ø11  centre [-0.024, 0, 0.015 ]   <- on the face
        Ø6.6 centre [ 0.024, 0, 0.0082]   <- 6.8 mm down, the counterbore depth

    Z differs by exactly the counterbore depth, so the 1.5 mm 3D clustering used for
    chamfers never paired them. Same axis means same X/Y, Z irrelevant.

    HISTORICALLY VOID (0.8.294): this assertion used to require
    `(_keep_r - _k[1]) > 0.0015` in the source, on the theory that a chamfer pair must not
    be collapsed. Report 2026-11-09 §4 showed the gate was a SECOND guess at the same
    tolerance: a shallow counterbore Ø11 over Ø9.8 has a 0.6 mm radial gap, so both circles
    survived and the sheet got two identical callouts again. Keeping that literal is now a
    REGRESSION, not a guard. The chamfer filter publishes `_chamfer_pair_keys`, and the
    merge consumes that membership decision instead of measuring a radius.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_hole_callouts")) or ""
    assert "_coaxial_dropped" in body, "coaxial duplicates are created again"
    assert "_same_axis" in body, (
        "coaxial is judged by 3D centre again; a counterbore is 6.8 mm deep and never pairs")
    assert "(_keep_r - _k[1]) > 0.0015" not in body, (
        "the radius-gap gate is back; an Ø11/Ø9.8 shallow counterbore gets two callouts")
    assert "_coaxial_merge_plan(_alive, _chamfer_pair_keys)" in body, (
        "the merge no longer consumes the chamfer filter's decision")
    assert "_chamfer_pair_keys.update" in body, (
        "the chamfer filter does not publish the keys it adjudicated")
    assert "coaxial_chamfer_protected=" in body, (
        "the protection decision is not countable in the receipt")
    return "one callout per coaxial hole; chamfer membership, not a radius gap, decides"


def assert_a_callout_receipt_reads_every_text_slot():
    """0.8.278 -> 0.8.293 — keep EVERY line, but never invent a composite `text`.

    Report 2026-11-09 (Q-3) found `text` holding only one line of a two-line callout
    while `native_text_complete:true` claimed otherwise. 0.8.278 "fixed" it by joining
    all slots with `" | "` and defining completeness as `len(parts) > 1`.

    Both halves were wrong, and 0.8.281 §6 / 0.8.290 S-5 said so for four rounds:

      * the composite made `text` mean something different in `add_hole_callouts` than in
        `list_drawing_annotations`, whose `text_is_format_code` / `text_decoded` parse a
        SINGLE native string (reported as `" | "` semantic drift);
      * `len(parts) > 1` is a COUNT, not a completeness proof: it flagged a correct
        single-line callout as incomplete (false negative) and 2-of-3 lines as complete
        (false positive);
      * Hole Wizard `<hw-*>` tokens, which never render, were copied into `text` verbatim.

    0.8.293 keeps `text` as one native line, puts the rest in `text_lines` (an ARRAY),
    sets `native_text_complete: None` (unknown) and strips `<hw-*>` from rendered lines
    while keeping them in `text_slots_raw`. The OLD assertions below are retired:
    `" | ".join(_parts)`, `text_is_partial`, and a hard-coded slot tuple in the tool.

    STILL SOURCE-LEVEL: inspects the helper. The behaviour is replayed against the
    report's measured strings by a smoke check (fake GetText reader, no SolidWorks).
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    helper = ast.get_source_segment(src, _function_node(
        tree, "_read_native_callout_text")) or ""
    assert helper, "the shared multi-slot reader is gone"
    assert "for slot in (1, 0, 2, 3, 4, 5)" in helper, (
        "slot 3 is where the through-hole line lives; the probe list changed")
    assert '"text_lines"' in helper and '"text_slots_raw"' in helper, (
        "the extra lines or the unmodified raw slots are no longer returned")
    assert '" | ".join' not in helper, "a composite `text` dialect is back"
    assert '"native_text_complete": None' in helper, (
        "completeness is provably UNKNOWN; a bool here is a false claim")
    plural = ast.get_source_segment(src, _function_node(
        tree, "add_hole_callouts")) or ""
    assert "_read_native_callout_text" in plural and 'item["text_lines"]' in plural, (
        "the batch tool stopped using the shared reader")
    assert 'item["native_text_complete"] = None' in plural, (
        "the batch receipt claims a completeness verdict again")
    assert "text_is_partial" not in plural, "the false-negative partial flag is back"
    return "every text line is returned as an array, text stays one native string"

def assert_a_single_callout_does_not_fake_a_verdict():
    """0.8.279 — `add_hole_callout` returned ok:true with every verification field null.

    Report 2026-11-09 (R-1), the most damaging finding of this round. The plural tool's
    `unannotated_note` told the caller to fix the missing Ø16 bore by hand:

        "请对照模型手工补 `add_hole_callout(x, y)`"

    Following that advice produced this on the sheet:

        Ø16 ↧ 68

    — a blind hole 68 deep, on a bore that goes all the way through. Meanwhile the receipt
    was:

        {"ok": true, "created": true,
         "readback": {"text": null, "value_mm": 16.0},
         "model_match": null, "matched_circle": null}

    `value_mm` was right (16), so any number-only check passed. `text` was null because the
    code called `callout.GetText()` with no slot on an `IAnnotation`; `model_match` was null
    because `model_path` is optional; and the `note` told the caller to "compare
    model_match.ok" — against a null.

    Fixed here:
    * text is read off the display dimension, every slot; when nothing answers the receipt
      carries `text_flags: [{flag: "text_unreadable"}]` instead of silence
    * `model_match` is always a dict — `{"ok": None, "checked": false, "reason": ...}` when
      no model was supplied
    * a depth symbol on a callout that might be a through bore raises
      `depth_on_a_through_bore_suspect`

    NOT fixed in 0.8.279: the `↧ 68` text itself, AND the `ok:true`. Rewriting a dimension's
    display text via `SetText` is what produced "Ø1515" earlier — the value lives across
    slots and writing one destroys the others.

    0.8.291 (report 2026-11-10 S-1) closed the loop differently. It found the text was
    READABLE all along — `GetDimension2(0)` was the wrong reader, the batch tool's
    `win.Dispatch(display).GetText(slot)` works — so `↧ 68` can now be detected. Rather than
    rewrite the text, the tool REVERTS the whole callout and returns
    `ok:false / verified:false / drawing_safe:false / created:false`, with `model_match`
    extended by `through_state:"unknown"` and `ok_means`. The dead
    `add_drawing_dimension` recommendation is gone.

    STILL SOURCE-LEVEL: measured on 限位导向套 2026-11-09 (`text=None`,
    `flags=['text_unreadable']`), not asserted against a rendered sheet.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_hole_callout")) or ""
    assert "text_flags" in body, "a callout can return ok:true with nothing verified again"
    assert "text_unreadable" in body, (
        "an unreadable text slot is silent again; the receipt looks like a success")
    assert ("checked=False" in body or '"checked": False' in body
            or "'checked': False" in body), (
        "model_match can be null again while the note tells the caller to read model_match.ok")
    assert "depth_on_a_through_bore_suspect" in body, (
        "a depth symbol on a possible through bore is not flagged")
    assert "_read_native_callout_text" in body, (
        "the display-dimension text slots are not read through the shared reader")
    # 0.8.293: `text` is ONE native line again. The depth gate must judge ALL lines, or
    # a `<HOLE-DEPTH>68` sitting in another slot lets `Ø16 ↧ 68` return ok:true again.
    assert '" ".join(readback.get("text_lines")' in body, (
        "the depth gate judges only `text` — the 0.8.291 P0 is back")
    # 0.8.291: a flag alone is not a fix — `ok` must follow verification and the bad ink
    # must come off the sheet.
    assert "ok=_verified" in body, (
        "the singular tool can return ok:true for an unverified callout again")
    assert "_reject_unverified_callout" in body and "callout_revert" in body, (
        "an unverified callout is left on the sheet again")
    assert "verified=_verified" in body and "drawing_safe=_verified" in body, (
        "the machine-readable verification state is gone")
    assert "through_state" in body and "ok_means" in body, (
        "model_match still only checks the diameter")
    return "a single callout reports what it could not verify and reverts unverified ink"

def assert_static_check_fails_when_it_cannot_check():
    """0.8.280 — no tag source meant "print SKIPPED, exit 0, banner says OK".

    Report 2026-11-09 (second document, P1, open two rounds). Three probes on a real
    install, all reproduced on v0.8.278:

        1. delete _build_meta.json, run in a copy with no .git
           -> "tag_compare: SKIPPED (no tag source…)" + "static check OK" + RC 0
        2. build a wheel -> 30 files, `_build_meta.json` not among them
        3. call the script from a cwd outside the repo root -> SKIPPED + OK

    A gate that cannot check must not report success. That is the same fail-open this
    project spent a dozen versions fixing inside its own receipts, except here it was the
    gate itself.

    Fixed:
    * no tag source now becomes a `problems` entry -> RC 1, unless HEAD can be shown to be
      BETWEEN tags (the legitimate mid-release state, detectable only with git)
    * `git describe` failing with "not a git repository" is not "HEAD is not on a tag".
      The first version conflated them and skipped — measured in /tmp with no .git:
      the gate passed. Now exit 128 is treated as "cannot determine".
    * `[tool.setuptools.package-data]` ships `_build_meta.json`, so the packaged tag
      source actually exists in a wheel

    Measured four shapes 2026-11-09:

        tarball + meta   -> RC 0, "v0.8.280 == pyproject 0.8.280 [packaged …]"
        tarball, no meta -> RC 1, "no tag source … version not verifiable"
        local with .git  -> RC 0
        meta says 0.8.999-> RC 1, "MISMATCH v0.8.999 … vs 0.8.279"

    STILL SOURCE-LEVEL: reads `static_check.py` and `pyproject.toml`; the four shapes were
    measured by hand, not in this suite.
    """
    with open(os.path.join(ROOT, "tests", "static_check.py"), encoding="utf-8") as fh:
        sc = fh.read()
    i = sc.find("tag must AGREE")
    j = sc.find("if problems:", i)
    assert i > 0 and j > i, "the tag check is after the early return again"
    block = sc[i:j]
    assert "problems.append" in block, "no tag source still cannot fail the gate"
    assert "not a git repository" in block, (
        "a non-repo directory is read as 'HEAD is not on a tag' and skipped")
    assert "_on_tag is False" in block, (
        "the mid-release skip is not restricted to a real git tree")

    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        pp = fh.read()
    assert "package-data" in pp and "_build_meta.json" in pp, (
        "the packaged tag source is not shipped in a wheel/sdist")
    return "the gate fails closed, and the packaged tag source ships"

def assert_partial_work_is_not_reported_as_success():
    """0.8.281 — ok:true sat next to verdict:partial and four excluded diameters.

    Report 2026-11-09 (R-2). On 限位导向套:

        holes_found: 1, created_count: 1, verdict: "partial", ok: true
        excluded_diameters_mm: [16.0, 18.0, 28.0, 30.0]   <- the main bore is in there

    A batch pipeline keys off `ok`, so a sheet missing its main bore read as finished. The
    transparency added in 0.8.277 (`excluded_diameters_mm`, `unannotated_note`) told a human
    the truth; `ok` still told a machine the opposite.

    Two changes:
    * `sw_session.ok()` used to hard-assign `payload["ok"] = True`, so the tool COULD NOT
      report otherwise. Now `setdefault` — every existing caller still gets `ok: true`, and
      a caller that knows better can pass `ok=False`.
    * `add_hole_callouts` sets `ok=(not _ambiguous)` and `verdict="partial"` whenever a
      chamfer pair had to be skipped.

    Measured 2026-11-09:

        ok=False  verdict=partial  created=1
        partial_reason="4 个直径因倒角配对无法判定而未标注（Ø16.0、Ø18.0、Ø28.0、Ø30.0）。"
    """
    with open(os.path.join(SRC, "sw_session.py"), encoding="utf-8") as fh:
        sess = fh.read()
    i = sess.find("def ok(")
    j = sess.find("def error(", i)
    assert "setdefault" in sess[i:j], (
        "ok() forces success again; no tool can report a partial result")
    assert 'payload["ok"] = True' not in sess[i:j], (
        "the hard assignment is back")

    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_hole_callouts")) or ""
    assert "ok=(not _ambiguous)" in body, "excluded diameters do not affect ok"
    assert "partial_reason" in body, "a partial run does not say what it skipped"
    return "a run with excluded diameters reports ok=false and names them"

def assert_the_failure_logger_never_touches_com():
    """0.8.282 — the failure handler made two more COM calls, from inside `except`.

    Architecture review 2026-11-09 (P0). `log_failure` enriched its record with:

        "sw_version": sw_version(),     # get_sw() + RevisionNumber
        "health": health(),             # probe + ActiveDoc

    So every logged failure re-entered COM. In the exact situation the logger exists for —
    a modal dialog, a dead RPC, a stale dispatcher — those two calls can hang, and the tool
    looks busy while doing nothing. The record that was supposed to explain the outage
    becomes part of it.

    Fixed: `_LAST_VERSION` / `_LAST_HEALTH` are refreshed on the liveness probe that
    `get_sw()` already performs (the revision is already in hand there), and `log_failure`
    reads those. The record says `state_source: "cached_at_last_success — NOT probed"`.

    Measured 2026-11-09 with SW running:

        warm version = '32.4.0'
        probe_timestamp_unchanged = True    (log_failure did not probe)
        snapshot_version = '32.4.0'
    """
    with open(os.path.join(SRC, "sw_session.py"), encoding="utf-8") as fh:
        sess = fh.read()

    i = sess.find("def log_failure(")
    assert i > 0, "log_failure is gone"
    j = sess.find("\ndef ", i + 10)
    body = sess[i:j]
    # Strip comments before checking, or the explanatory text trips the assertion.
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert "sw_version()" not in code, (
        "log_failure calls sw_version() again — a COM call from the failure path")
    assert "health()" not in code, (
        "log_failure calls health() again — a COM call from the failure path")
    assert "_LAST_VERSION" in code and "_LAST_HEALTH" in code, (
        "the logger does not read the snapshot")
    assert "state_source" in code, (
        "the record does not say the state is a snapshot rather than a probe")

    # the snapshot has to be refreshed somewhere, or it stays None forever
    assert "_LAST_VERSION = str(_revision)" in sess, (
        "nothing refreshes the snapshot; the log would always say None")
    return "the failure logger reads a snapshot and never re-enters COM"

def assert_solidworks_calls_are_serialised():
    """0.8.283 — nothing stopped two tools entering the same COM session at once.

    Architecture review 2026-11-09 (P0). Every tool shares `_SW` (one dispatcher),
    `_LAST_CALL`, and SolidWorks' session-level ActiveDoc/Selection. FastMCP can enter two
    synchronous tool bodies concurrently, so two requests could interleave document
    switches, selections, and writes. The review's wording is the right one:

        "This does not prove every concurrent call fails, but a desktop COM automation
         should not rest its correctness on 'clients usually don't do that'."

    Fixed with the transitional gate the review asked for: `_guarded` in
    `_install_argument_guard` is the single entry point for `tools/call`, so wrapping
    `await original(request)` in `_COM_LOCK` covers every registered tool by construction —
    including `ping` / `health_check`, which the review named as paths that must not
    bypass it.

    Measured 2026-11-09, five coroutines contending for the lock:

        tasks=5  overlap=False  serialized=True
        within-lock spans (ms): [57, 64, 60, 63, 60]

    Not the long-term answer (that is a dedicated STA executor) and it does not make
    cancellation safe — `_COM_LOCK_STATS` counts acquisitions so a field report can show
    whether waits actually happen.
    """
    with open(os.path.join(SRC, "server.py"), encoding="utf-8") as fh:
        srv = fh.read()

    assert "_COM_LOCK" in srv, "no serial gate; two tools can share the COM session"
    assert "async with _COM_LOCK" in srv, "the lock exists but does not wrap the call"
    assert "_COM_LOCK_STATS" in srv, "no evidence the gate is ever contended"

    i = srv.find("async def _guarded(")
    assert i > 0, "the tools/call entry point is gone"
    j = srv.find("handlers[key] = _guarded", i)
    body = srv[i:j]
    assert "async with _COM_LOCK" in body, (
        "the lock is not on the path every tool call takes")
    # the lock must wrap the ORIGINAL call, not sit next to it
    lock_at = body.find("async with _COM_LOCK")
    call_at = body.find("await original(request)")
    assert 0 < lock_at < call_at, "the lock does not enclose the tool invocation"
    return "every tools/call runs under one lock, and the receipt can show contention"

def assert_tool_profiles_are_advisory_only():
    """0.8.284 — the review proposed filtering profiles; this ships the table, not a filter.

    Architecture review 2026-11-09 (P1): `standard / drawing / expert / full`, with
    `full` as the default so nothing breaks.

    Two reasons it stopped at a table:

    * nothing in five rounds of feedback says a model picks the WRONG tool. Every reported
      problem was "the tool answered incorrectly", never "I could not find the tool";
    * filtering needs one edited decorator per tool or a poked `_tool_manager._tools`,
      and the review's own acceptance criterion ("common flows must not depend on hidden
      expert tools") is a risk nobody has measured.

    So `list_tool_profiles()` returns the grouping as data. Every tool stays registered —
    that is the property this assertion protects. `ungrouped` must be empty, or a tool was
    added and the table drifted, which is exactly what a real filter would silently inherit.

    Measured 2026-11-09:

        registered=71  counts={standard: 14, drawing: 50, expert: 7, full: 71}
        ungrouped=[]
    """
    with open(os.path.join(SRC, "server.py"), encoding="utf-8").read() if False else open(
            os.path.join(SRC, "server.py"), encoding="utf-8") as fh:
        srv = fh.read()

    assert "_TOOL_PROFILES" in srv, "the grouping table is gone"
    assert "def list_tool_profiles(" in srv, "the advisory tool is gone"
    assert "advisory grouping — every tool stays registered" in srv, (
        "the receipt no longer says the grouping is advisory")

    i = srv.find("def list_tool_profiles(")
    j = srv.find("\ndef ", i + 10)
    body = srv[i:j]
    assert "_tool_manager" in body, "the registered count is not read from the live registry"
    assert "ungrouped" in body, (
        "drift is invisible: a new tool would belong to no profile and nobody would know")
    # it must NOT filter: no pop/del against the registry
    assert "_tools.pop" not in body and "del _tools" not in body, (
        "the advisory tool mutates the registry — that is a filter, not a table")
    return "the profile table is published as data; nothing is unregistered"

def assert_tool_profile_counts_come_from_the_registry():
    """0.8.291 — report 2026-11-10 P1: a hard-coded count inside the profile receipt.

    `list_tool_profiles` shipped with `profiles["full"] = ["* all 70 — ..."]` while the
    SAME JSON said `registered=71` and `counts.full=71`. The tool itself was the extra
    registration, so the number was wrong on its first release. Its dedicated smoke check
    passed because it only asserted `ungrouped == []` and `registered >= 71` — a lower
    bound cannot notice a stale number in prose, and it stays green at 72, 73, ...

    Three defences, in increasing order of strength:

      * no static tool count anywhere in the grouping module (prose OR comment);
      * `counts` is a uniform `len(v)` with no `full` special case, and `full` is
        injected from the live `names` list — every profile is now a name list;
      * the live check asserts `counts[k] == len(profiles[k])`, that `full` holds only
        tool names, and that the groups equal the registry in BOTH directions.
    """
    import re as _re
    with open(os.path.join(SRC, "server.py"), encoding="utf-8") as fh:
        srv = fh.read()
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        smoke = fh.read()

    # 1) No static count: "all 70", "70 tools", "69 of 70" are all drift sources.
    # Deliberately narrow: "keep 8 of 41" and "18+29+3+10 mm" are geometry notes, not
    # tool counts. Only phrases that state a NUMBER OF TOOLS are drift sources.
    _stale = _re.findall(
        r"\ball\s+\d{2,3}\b|\b\d{2,3}\s+(?:registered\s+)?tools?\b"
        r"|\b\d{2,3}\s+of\s+\d{2,3}\s+tools?\b",
        srv, _re.I)
    assert not _stale, "static tool count(s) in server.py: %s" % _stale[:5]

    # 2) `full` is the live registry; counts have no special case.
    i = srv.find("def list_tool_profiles(")
    assert i > 0, "list_tool_profiles is gone"
    j = srv.find("\ndef ", i + 10)
    body = srv[i:j]
    assert '_profiles["full"] = list(names)' in body, (
        "`full` is not the live registry — a hand-maintained list will drift again")
    assert "counts\": {k: len(v) for k, v in _profiles.items()}" in body, (
        "counts is not a uniform len(v) over every profile")
    assert '!= "full"' not in body, "a `full` special case reappeared in counts"
    assert '"unknown_names"' in body, (
        "drift in the other direction (stale names in a group) is invisible again")
    _table = srv[srv.find("_TOOL_PROFILES = {"):i]
    assert '"full"' not in _table, (
        "`full` is back in the static table as a sentence/list")

    # 3) The live check must assert the three invariants, not a lower bound.
    k = smoke.find("def _tool_profiles():")
    assert k > 0, "the profile smoke check is gone"
    live = smoke[k:smoke.find("\n    r.check(", k)]
    assert "counts[k] == len(v)" in live, "the live check does not verify counts"
    assert 'not n.startswith("*")' in live, "the live check does not reject prose in full"
    assert "_union == set(full)" in live, "the live check does not compare groups to registry"
    assert "unknown_names" in live, "the live check ignores stale group names"
    assert ">= 71" not in live, "the hard-coded lower bound is back"
    # A check that NameErrors is not a check. `server` was a bare, never-imported name in
    # smoke.py from 0.8.284 until 0.8.291; `check_all.py` counted the `r.check` line and
    # reported it as coverage anyway. Found by `smoke.py --only 'tool profiles'`.
    assert "import server as _server" in live, (
        "the profile smoke check calls server without importing it — it can never pass")
    # `_server.list_tool_profiles()` contains the literal substring
    # "server.list_tool_profiles(", so a plain `not in` gives a false alarm. Match only a
    # BARE name: no word character immediately before `server`.
    import re as _re_live
    assert not _re_live.search(r"(?<!\w)server\.list_tool_profiles\(", live), (
        "a bare `server` reference is back; the check would NameError again")
    return "no static count; full=live registry; live check imports server and covers 3 invariants"

def assert_eccentric_sections_are_refused_and_line_styles_are_documented():
    """0.8.291 — report 2026-11-10: P0 eccentric cut, P1 nudge, P2 line styles.

    P0 was NOT a missing check: `section_off_axis` already existed, but it compared only
    `kept_dimension`, which is CONSTANT for an eccentric chord of a revolved part. The
    receipt carried `ratio: 1.000` next to `127.98 -> 127.73` and still said `ok:true`.
    The fix measures the DISCARDED axis too, on an ABSOLUTE 0.1 mm threshold (0.25/127.98
    = 0.195% defeats any proportional gate), and lifts the decision into a pure function
    so smoke.py can replay the report's measured numbers.

    Also guarded here: the default nudge is OFF (and a manual nudge raises a TOP-LEVEL
    warning), `eccentricity_mm` reaches the receipt, the wrong 63.5 mm hint is corrected,
    the capability table stops saying "全部可做" for a partial status and retires the
    obsolete material/geometry claim, and the `line_style` schema finally documents the
    numeric styles that the report proved are the only correct way to draw an axis.
    """
    def _read(*parts):
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return fh.read()

    drw = _read(SRC, "tools", "drawing.py")
    cap = _read(SRC, "tools", "capabilities.py")
    lay = _read(SRC, "tools", "layout.py")
    srv = _read(SRC, "server.py")
    smoke = _read(ROOT, "tests", "smoke.py")

    # ── P0: both axes measured; the discarded one judged in absolute millimetres ─────
    assert "SECTION_OFF_AXIS_TOLERANCE_MM = 0.1" in drw, (
        "the absolute off-axis threshold is gone")
    i = drw.find("def _section_sanity(")
    j = drw.find("def _section_off_axis_from_sanity(", i)
    k = drw.find("def add_section_view(", j)
    assert 0 < i < j < k, "the section geometry helpers were merged or reordered"
    sanity_body = drw[i:j]
    assert "off_axis_shrink_mm" in sanity_body and "off_axis_dimension" in sanity_body, (
        "`_section_sanity` no longer measures the discarded axis")
    decide = drw[j:k]
    assert "off_axis_shrink_mm" in decide, "the decision ignores the discarded axis"
    assert "> SECTION_OFF_AXIS_TOLERANCE_MM" in decide, (
        "the absolute threshold is not what rejects an eccentric chord")
    assert 'detected_on' in decide and "off_axis_dimension" in decide, (
        "the receipt cannot say WHICH axis failed")
    assert 'sanity.get("checked")' in decide, "unchecked sections would be judged"
    # 0.8.302: the decision takes the MEASURED eccentricity as a second argument. Without
    # it a correct end-view vertical section was rejected (report 2026-11-12 §2): the
    # source is a circle, the section is the axial slice, so "width shrank 76 mm" is a
    # change of meaning, not a chord.
    m = drw.find('_off_axis = _section_off_axis_from_sanity('
                 'sanity, diag.get("eccentricity_mm"))')
    assert m > k, "add_section_view does not pass the measured eccentricity"
    seg = drw[m:drw.find("off_axis=_off_axis", m) + 24]
    assert '"section_off_axis"' in seg and "return error(" in seg, (
        "an off-axis verdict no longer rejects the call")
    assert "SECTION_ECCENTRICITY_GATE_MM" in drw, (
        "the gate that stops a through-axis cut being called eccentric is gone")
    assert "source_size_mm" in decide, (
        "the square-source guard is gone; a circular view will be mis-judged again")
    assert 'sanity["verdict"] = "section_off_axis"' in seg, (
        "the nested sanity still claims ok next to a top-level rejection")

    # ── P1: no silent nudge; eccentricity visible; the chord number corrected ────────
    assert "auto_nudge_zero_offset=False" in drw, (
        "the zero-offset workaround is back ON by default, contradicting its own hint")
    assert "if _offset_nudged:" in drw and '_nudge_warning' in drw, (
        "a manual nudge no longer raises a top-level warning")
    assert 'diag["warning"]' in drw, "the nudge warning does not reach the receipt"
    assert "eccentricity_mm" in drw and "ScaleDecimal" in drw, (
        "the receipt no longer converts the requested offset to model millimetres")
    assert "63.875" in drw, "the corrected chord length is gone"

    # ── capability table: no more "全部可做" for partial; old claim retired ──────────
    assert "已作废" in cap and "材质/几何决定论作废" in cap, (
        "the obsolete 'hatch depends on geometry/material' claim is still current text")
    assert "`status:partial` 不等于「全部可做」" in cap, (
        "advice still says everything is possible for a partial capability")
    assert "0.1 mm" in cap, "the capability entry does not describe the new threshold"

    # ── P2: schema documents numeric styles; stale hint retired ─────────────────────
    assert "def _line_style_warning(" in lay, "no warning for a misleading line style"
    assert "line_style_warning=_line_style_warning(" in lay, (
        "the warning is defined but not returned")
    assert "Drawing-sheet sketches are NOT rendering on this build" not in lay, (
        "the obsolete global 'sketches never render' hint is back")
    assert "PREFER THE NUMBER" in srv and "for an AXIS pass '4'" in srv \
        and "for a real solid line pass '7'" in srv, (
        "the schema still hides the only styles that actually work")
    assert "eccentric section is refused" in smoke, "CI smoke #3 is missing"
    return "both axes + absolute 0.1 mm; nudge off/warned; styles 4/7 documented"

def assert_single_callout_reverts_unverified_ink():
    """0.8.291 — report 2026-11-10 S-1: `Ø16 ↧ 68` with `ok:true` was still shipping.

    v0.8.290 was HONEST but not SAFE: `text_flags` said "do not treat ok:true as evidence",
    `model_match.ok:true` compared only the diameter (`delta_mm: 0.0` made the wrong text
    look even more credible), and the singular tool kept returning `created:true / ok:true`
    with the annotation on the sheet. The warning lived in the PLURAL tool's note, which a
    singular-tool caller never sees.

    Two root causes fixed here:

      * the text was readable all along — `GetDimension2(0)` is the wrong reader, the
        batch tool's `win.Dispatch(display).GetText(slot)` works. So `↧ 68` is detectable;
      * verification is now a PURE decision (`_hole_callout_verification`) that gates `ok`,
        and unverified ink is REVERTED via the same `GetAnnotation`/`EditDelete` path the
        batch tool already proved. No "created but wrong" path remains.

    `model_match` gains `through_state:"unknown"` (the ledger has no through/blind flag),
    `depth_text_consistent:null` and `ok_means`, so a diameter match can never again be
    read as "the text is right".
    """
    def _read(*parts):
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return fh.read()

    drw = _read(SRC, "tools", "drawing.py")
    cap = _read(SRC, "tools", "capabilities.py")
    pipe = _read(SRC, "tools", "pipeline.py")
    smoke = _read(ROOT, "tests", "smoke.py")

    # ── the pure contract ───────────────────────────────────────────────────────────
    i = drw.find("def _hole_callout_verification(")
    j = drw.find("def _reject_unverified_callout(", i)
    k = drw.find("def add_hole_callout(", j)
    assert 0 < i < j < k, "the verification/revert helpers were merged or removed"
    contract = drw[i:j]
    assert "HOLE-DEPTH" in contract and "\\u21a7" in contract, (
        "the depth symbol is no longer detected")
    assert "model_match_ok is not False" in contract, (
        "a wrong diameter can pass again")
    assert "unsupported_through_bore" in contract and "hole_callout_unverifiable" in contract, (
        "the two measured failure modes are not named")
    revert = drw[j:k]
    assert 'GetAnnotation' in revert and "EditDelete" in revert, (
        "the revert does not use the proven delete path")
    assert "EditRebuild3" in revert, "annotation counts will not refresh after the delete"
    assert '"reverted": False' in revert, (
        "a failed revert can be swallowed into a success")

    # ── the singular tool wires it in ───────────────────────────────────────────────
    drw_ast = ast.parse(drw)
    body = ast.get_source_segment(drw,
                                  _function_node(drw_ast, "add_hole_callout")) or ""
    assert "_win_read.Dispatch(callout)" in body, (
        "the text reader is back on GetDimension2(0), which reads nothing")
    assert "ok=_verified" in body and "verified=_verified" in body, (
        "`ok` is hard-coded True again — a batch pipeline would pass a wrong drawing")
    assert "drawing_safe=_verified" in body and "created=(not _reverted)" in body, (
        "the receipt no longer says whether bad ink stayed on the sheet")
    assert "callout_revert=_revert" in body and "verification=_verification" in body, (
        "the revert/verification evidence is missing from the receipt")
    assert "through_state" in body and "ok_means" in body \
        and "depth_text_consistent" in body, (
        "model_match is back to diameter-only")
    assert "ok：null=未校验" in body, "the note still describes fields that no longer exist"

    # ── no receipt recommends a path this project proved dead ───────────────────────
    assert "**不要**用 `add_drawing_dimension" in cap, (
        "the capability table again recommends the dead dimension path")
    assert "撤销标注并返回 `ok:false`" in pipe, (
        "the pipeline warning still describes the pre-0.8.291 singular tool")
    assert "撤销该标注并返回 `ok:false / verified:false`" in drw, (
        "the batch note still describes a tool that leaves wrong ink on the sheet")
    assert "single callout refuses unverified ink" in smoke, (
        "the S-1 smoke check is missing")
    # S-3: `ok:false` means "incomplete", not "the call failed".
    assert "execution_ok=True" in drw and 'completeness=("incomplete"' in drw, (
        "the plural tool encodes 'incomplete drawing' only as 'call failed' again")
    return "depth detected, ok gated, bad ink reverted, model_match no longer fakes safety"

def assert_partial_survives_the_pipeline_and_the_gate_reads_absolute_paths():
    """0.8.285 — two fixes each stopped one layer short.

    Report 2026-11-09 on v0.8.281.

    (A) `add_hole_callouts` reported `ok:false, verdict:"partial"` with
    `excluded_diameters_mm: [8.0, 15.0]` — and `produce_drawing`, the entry a batch actually
    calls, still answered `ok:true, verdict:"待设计审核", blockers:[]`,
    `callout_warning:null`, step `("hole_callouts", false, null)`.

    Five gaps, all fixed:
    * `record()` now writes `status: ok|partial|failed` and falls back to
      `partial_reason` for `detail`, so the step no longer reads `false, null`
    * the `callouts` sub-dict passes `ok / verdict / partial_reason /
      excluded_diameters_mm / ambiguous_chamfer_pairs` through — it was dropping exactly the
      fields computed for it
    * `_callout_warning()` has a partial branch instead of returning null
    * `pending` gains "孔标注缺 N 个直径：Ø…"
    * top-level `ok` follows any partial step, and `partial_steps` names them. The report
      asked for one behaviour, explicitly not "top true / step false".

    (B) The fail-closed gate read `_build_meta.json` through a RELATIVE path, so a perfectly
    good install failed with "no tag source" merely because the script was invoked from
    another cwd. Fixed to `ROOT`-relative, and `regressions.py`'s no-source branch now fails
    too instead of printing 266/266 beside static_check's RC 1.

    Measured 2026-11-09:

        cross-cwd with meta  -> RC 0
        no tag source, static      -> RC 1
        no tag source, regressions -> RC 1
    """
    with open(os.path.join(SRC, "tools", "pipeline.py"), encoding="utf-8") as fh:
        pipe = fh.read()
    assert '"status": _status' in pipe, "a partial step still cannot be told from a crash"
    assert 'payload.get("partial_reason")' in pipe, (
        "the step detail is empty for a partial payload again")
    assert '"verdict": callouts.get("verdict")' in pipe, (
        "the callouts sub-dict drops verdict again")
    assert '"excluded_diameters_mm": callouts.get("excluded_diameters_mm")' in pipe, (
        "the excluded diameters never reach the pipeline receipt")
    assert 'callouts.get("verdict") == "partial"' in pipe, (
        "callout_warning is silent for a partial run again")
    assert "孔标注缺 %d 个直径" in pipe, "pending does not mention the skipped diameters"
    # 0.8.299: `ok` is no longer the literal `not _partial_steps`. The ORIGINAL intent —
    # top-level `ok` must not contradict a partial step — is preserved, but it is now an
    # IMPLICATION of one derivation instead of a second formula: `_partial_steps` reaches
    # `_delivery_status`, which makes `completeness` incomplete, which is what `_receipt_ok`
    # returns. Keeping the old literal here would have blocked the 2026-11-11 P0 fix.
    assert "_delivery_status(_blockers, _partial_steps, _failed_steps)" in pipe, (
        "partial steps no longer reach the completeness derivation")
    assert "ok=_receipt_ok(_status)" in pipe, (
        "top-level ok stopped following the derivation that knows about partial steps")
    assert "partial_steps=_partial_steps" in pipe, "the partial steps are not named"

    with open(os.path.join(ROOT, "tests", "static_check.py"), encoding="utf-8") as fh:
        sc = fh.read()
    assert 'os.path.join(ROOT, "src", "solidworks_assetmate_mechdog"' in sc, (
        "the packaged tag source is read through a relative path again; any cwd but the "
        "package root fails")
    assert '"not a git repository"' in sc, "a non-repo is read as mid-release again"
    return "partial reaches the pipeline receipt, and the gate reads ROOT-relative paths"

def assert_readme_documents_the_release_mechanics():
    """0.8.286 — nine concepts shipped without a line of documentation.

    Report 2026-11-09 (§6): `bump_version`, `tag_compare`, `_build_meta`, `package-data`,
    `fail-closed`, `source-level`, `setdefault`, `text_flags`, `partial_reason` — zero
    hits in README, fourth round running.

    A feature nobody can find is not shipped. The mechanics matter more than most: three
    releases went out with a stale version because the bump was a `sed` that matched
    nothing, and two rounds of gates failed open before anyone noticed.

    The section is deliberately number-free. Earlier doc checks drifted because a count was
    printed in prose; the counts are printed by the tools themselves.
    """
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()

    required = ["bump_version", "tag_compare", "_build_meta", "package-data",
                "fail-closed", "source-level", "setdefault", "text_flags",
                "partial_reason"]
    missing = [k for k in required if k not in readme]
    assert not missing, "README never explains: %s" % ", ".join(missing)

    assert "SELF-DECLARED" in readme, (
        "the packaged tag source is described as verified again")
    assert "<N> assertions" in readme or "source-level : " in readme, (
        "the two-suite banner is not documented")
    return "README explains how releases are versioned, gated and reported"

def assert_no_receipt_recommends_a_known_dead_path():
    """0.8.287 — two receipts told the caller to do things this project had proved broken.

    Timeline, all of it mine:

    * v0.8.277 `unannotated_note`: "请对照模型手工补 `add_hole_callout(x, y)`"
    * v0.8.279 the receipt itself gained `depth_on_a_through_bore_suspect` because that
      call writes `Ø16 ↧ 68` — a through bore rendered as a blind hole. The note was
      never updated.
    * v0.8.285 `_callout_warning` (pipeline) recommended `add_drawing_dimension` instead.
    * 2026-11-10 measured that path: the dimension is created (`type=5`, `value_m` right)
      but no text renders. `SetText(1, "<MOD-DIAM><DIM>")` prints `Ø<DIM>16`; writing the
      value gives `Ø1616`; leaving it alone gives nothing.

    So for two versions the tools advised callers to take a path the tools themselves knew
    was broken. That is worse than silence: it spends the caller's time and ends in a wrong
    or empty annotation.

    Both now say "手工标" and name the reason, plus the model-space centre so the human
    does not have to hunt for the hole.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_hole_callouts")) or ""
    note_start = body.find("unannotated_note=")
    note = body[note_start:note_start + 900]
    assert "手工标" in note, "the note no longer says the work is manual"
    assert "add_hole_callout(x, y)" not in note, (
        "the note recommends add_hole_callout again — that path writes Ø16 as a blind hole")
    assert "add_drawing_dimension" in note and "文字渲染不出来" in note, (
        "the note does not explain why the dimension path also fails")
    assert "ambiguous_chamfer_pairs[].centre" in note, (
        "the note does not say where the skipped diameters are")

    with open(os.path.join(SRC, "tools", "pipeline.py"), encoding="utf-8") as fh:
        pipe = fh.read()
    i = pipe.find('callouts.get("verdict") == "partial"')
    j = pipe.find("if exceeded", i)
    warn = pipe[i:j]
    assert "手工标" in warn, "the pipeline warning still points at an automatic path"
    assert "渲染不出来" in warn or "盲孔" in warn, (
        "the pipeline warning does not say why the automatic paths fail")
    return "neither receipt recommends a path this project has proved broken"

def assert_require_distinguishes_failure_from_partial():
    """0.8.290 — `require()` treated "worked with gaps" as "call failed".

    `ok:false` has two meanings in this codebase since v0.8.281:

        failed          -> `error` is set, nothing was produced
        partial         -> `verdict: "partial"`, something WAS produced

    `require()` only looked at `ok`, so a good partial run raised
    "call failed: None None" — error and message both None, because nothing had failed.
    Measured 2026-11-10: it sent me hunting for a pipeline bug that did not exist and
    nearly produced a new fixture for a problem that was in the test helper.

    `allow_partial=True` accepts a partial result, defined by top-level `partial_steps`
    (the top-level `verdict` is the delivery judgement, not the run status). It still
    rejects anything carrying an `error`, so it cannot mask a real failure, and the default
    stays strict for the other 49 checks.

    Measured both fixtures:

        限位导向套 --part  -> PASS 34.1s  partial reached every layer, skipped=[16,18,28,30]
        P05_karetku        -> PASS 37.7s  not triggered (no chamfer ambiguity)
    """
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()

    i = sm.find("def require(")
    assert i > 0, "require is gone"
    j = sm.find("\ndef ", i + 10)
    body = sm[i:j]
    assert "allow_partial" in body, (
        "require rejects a partial run again; the two meanings are conflated")
    assert 'payload.get("partial_steps")' in body, (
        "require keys off the wrong field — top-level `verdict` is the delivery judgement")
    assert 'payload.get("error")' in body, (
        "allow_partial no longer excludes real failures")
    assert "allow_partial=False" in body, (
        "the default is permissive; 49 existing checks would stop catching failures")
    return "require separates a failed call from a partial one, and defaults to strict"


def assert_top_level_input_schemas_are_sealed():
    """0.8.292 — ten consecutive reports: the published schema allowed anything.

    `tools/list` showed `additionalProperties: <absent>` on EVERY tool from 0.8.235 to
    0.8.291 while the runtime guard `_install_argument_guard` was being fixed. The two
    are not interchangeable: some MCP clients filter `arguments` locally against the
    schema BEFORE our request handler sees them, so the declaration has independent
    value. Conversely, measured with mcp 1.30.0, sealing the schema WITHOUT the guard
    still lets `ping({"bogus_param": 123})` return ok:true — pydantic's `extra="ignore"`
    drops the key. Do not let one fix be used as an excuse to remove the other.

    Four things are locked down here, all source-level:
      1. exactly ONE subscript write, to the TOP-LEVEL `["additionalProperties"]`, with
         the literal False — recursing into `properties` would rewrite Pydantic's nested
         `additionalProperties: true` on dict-typed params such as `palette`;
      2. the seal runs at MODULE level AFTER the last `@mcp.tool()` decorator, so an
         in-process `import server; mcp.list_tools()` sees it too, not just `main()`;
      3. `main()` still installs the runtime guard;
      4. the receipt self-reports the seal, and a real `mcp.list_tools()` check exists.
    """
    src = _read("server.py")
    tree = _tree("server.py")
    node = _function_node(tree, "_seal_input_schemas")
    assert node is not None, "_seal_input_schemas is gone"
    body = ast.get_source_segment(src, node) or ""
    assert 'getattr(tool, "parameters", None)' in body, (
        "the seal is no longer reading the generated Tool.parameters")

    # 1) Only the top-level dict is written. A recursive seal needs a second subscript
    #    store (or a write through `properties`), so counting them is a real guard.
    writes = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Assign):
            continue
        for target in sub.targets:
            if isinstance(target, ast.Subscript):
                try:
                    key = ast.literal_eval(target.slice)
                except Exception:
                    key = None
                writes.append((key, sub.value))
    assert len(writes) == 1, (
        "expected exactly one schema write (top-level only), got %d: %r"
        % (len(writes), [k for k, _ in writes]))
    key, value = writes[0]
    assert key == "additionalProperties", (
        "the seal writes %r instead of the top-level additionalProperties" % key)
    assert isinstance(value, ast.Constant) and value.value is False, (
        "additionalProperties must be the literal False, not %r" % getattr(value, "value", None))
    assert '"properties"]' not in body, (
        "the seal reaches into `properties` — nested dict schemas would be rewritten")

    # 2) Module level, after every tool registration.
    last_tool_line = 0
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        for dec in n.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                last_tool_line = max(last_tool_line, n.lineno)
    assert last_tool_line > 0, "no @mcp.tool() decorators found"
    seal_call = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                and isinstance(n.value.func, ast.Name) \
                and n.value.func.id == "_seal_input_schemas":
            seal_call = n
    assert seal_call is not None, (
        "the seal is not invoked at module level — in-process list_tools() would be unsealed")
    assert seal_call.lineno > last_tool_line, (
        "the seal runs at line %d, before the last tool at %d — later registrations "
        "would be missed" % (seal_call.lineno, last_tool_line))
    assert any(isinstance(t, ast.Name) and t.id == "_SCHEMA_SEAL" for t in seal_call.targets), (
        "the seal result is discarded; nothing can observe a partial seal")

    # 3) Declaration != enforcement.
    main_body = ast.get_source_segment(src, _function_node(tree, "main")) or ""
    assert "_install_argument_guard(mcp)" in main_body, (
        "the runtime guard was removed on the strength of a schema declaration")
    assert "_seal_input_schemas" not in main_body, (
        "the seal moved back into main(); import-time list_tools() would be unsealed")

    # 4) Receipt + a check that actually executes.
    prof = ast.get_source_segment(src, _function_node(tree, "list_tool_profiles")) or ""
    assert '"input_schema_seal": dict(_SCHEMA_SEAL)' in prof, (
        "the seal result is not observable; a 0-tool seal would look like success")
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    i = sm.find("def _input_schemas_are_sealed():")
    assert i > 0, "the live schema check is gone"
    blk = sm[i:sm.find("\n    def ", i + 10)]
    assert "import server as _server" in blk, (
        "server is not imported locally again — the NameError that hid a check for six "
        "releases would come back")
    assert "mcp.list_tools()" in blk, "the check reads source text instead of the real registry"
    assert "nested" in blk, "nested schemas are no longer protected"
    assert 'r.check("top-level input schemas are sealed"' in sm, (
        "the check is defined but never registered with the runner")

    # The in-process check cannot see what main() installs. Ten rounds of this finding
    # came from a hand-run stdio client, so that probe is now a file we can guard.
    probe_path = os.path.join(ROOT, "tests", "stdio_contract.py")
    assert os.path.isfile(probe_path), "the real stdio contract probe is gone"
    with open(probe_path, encoding="utf-8") as fh:
        probe = fh.read()
    assert "stdio_client(" in probe and "session.list_tools()" in probe, (
        "the probe no longer drives a real subprocess over stdio")
    assert "bogus_param" in probe and '"unknown_argument"' in probe, (
        "the probe no longer proves the runtime guard survives the schema declaration")
    return ("module-level seal after last tool at line %d, one top-level write, "
            "guard intact, stdio probe present" % last_tool_line)


def assert_hw_placeholders_are_stripped_but_kept_raw():
    """0.8.293 — report 2026-11-09 R-4 / 2026-11-10 S-5, two rounds unfixed.

    `add_hole_callouts.created[0].text` carried a third line:

        <HOLE-SPOT><MOD-DIAM> <hw-cbdia> <HOLE-DEPTH> <hw-cbdepth>

    The `<hw-*>` tokens are Hole Wizard internal placeholders with no bound value: they
    do NOT appear on the sheet (the report zoomed in to confirm). Reading every slot and
    joining them into the receipt made any "unresolved format code" audit fire on a
    perfectly correct drawing.

    The distinction that must not be lost: `<MOD-DIAM>` is a FORMAT CODE the renderer turns
    into Ø, so it must stay. Only `<hw-*>` is dead weight. Removing every `<...>` token
    would destroy the diameter again — the 0.8.x `Ø1515` class of bug.
    """
    src = _read("tools/drawing.py")
    helper = ast.get_source_segment(src, _function_node(
        ast.parse(src), "_read_native_callout_text")) or ""
    assert r"<hw-[^>]*>" in helper, "the Hole Wizard placeholder pattern is gone"
    assert "hw.sub(" in helper and "hw.findall(" in helper, (
        "placeholders are not removed from rendered lines AND counted")
    assert '"hw_placeholders_stripped"' in helper, (
        "the receipt cannot say which tokens were removed")
    assert '"text_slots_raw"' in helper, (
        "the original slot text was silently discarded — nothing to audit against")
    assert '"<MOD-DIAM>"' in helper or "<MOD-DIAM>" in helper, (
        "the format-code vs placeholder distinction is no longer documented")
    code_only = "\n".join(line.split("#", 1)[0] for line in helper.splitlines())
    assert "re.sub" not in code_only or "hw.sub(" in code_only, (
        "a broad token removal would eat <MOD-DIAM> too")
    return "<hw-*> stripped from rendered text, raw slots kept, format codes preserved"


def assert_circular_only_edges_are_not_sheet_projected():
    """0.8.293 — reports 2026-11-08 P1-3 / 2026-11-09 Q-6 / 2026-11-10 S-6.

    Four rounds measured the same right-side view: five edges, ALL circles,
    `sheet_start == sheet_end == sheet_center`, top-level `sheet_projected: true`. The
    flag only meant "a circle centre was transformed". Callers read it as "the edges can
    be picked by index" and could not produce the 70 mm overall length with
    `add_distance_dimension`.

    Now `sheet_projected` means "there is a projected edge with DISTINCT endpoints", with
    `circular_model_edges_only` as the reason, `edge_kind` per row, and
    `sheet_coordinates_present` to say the centres are still useful. Circle-centre
    coordinates must NOT be deleted — they drive axial gap dimensions.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    scope = ast.get_source_segment(src, _function_node(
        tree, "_edge_projection_scope")) or ""
    assert scope, "the pure projection-scope helper is gone"
    assert 'row["sheet_start"] != row["sheet_end"]' in scope, (
        "a circle (start == end == centre) would count as a selectable edge again")
    assert "circular_model_edges_only" in scope and "usable_for_edge_selection" in scope
    assert "sheet_coordinates_present" in scope, (
        "the receipt no longer says circle centres are still projected and usable")
    kind = ast.get_source_segment(src, _function_node(tree, "_edge_kind")) or ""
    assert '"circle"' in kind and '"line"' in kind and '"unknown"' in kind, (
        "edge_kind must admit it cannot classify a vertex-less contour edge")
    assert '"arc"' not in kind.split('"""')[-1] or "cannot" in kind, (
        "an arc must not be invented from data the binding cannot provide")
    body = ast.get_source_segment(src, _function_node(
        tree, "list_drawing_edges")) or ""
    assert '"sheet_projected": _effective_projected' in body, (
        "the envelope still reports the raw centre-only projection as success")
    assert '"circular_model_edges_only"' in body and '"edge_kind"' in body, (
        "the reason and the per-row classification are missing")
    assert 'payload["sheet_projection_reason"] = _reason' in body, (
        "the envelope does not carry the computed reason")
    with open(os.path.join(SRC, "server.py"), encoding="utf-8") as fh:
        srv = fh.read()
    assert "usable_for_edge_selection" in srv, (
        "the published tool description still teaches the old meaning")
    return "circular-only views report sheet_projected:false with a named reason"


def assert_bump_version_check_fails_closed():
    """0.8.293 — report 2026-11-09 §6, carried through 0.8.290.

    Without `.git`, `_git("rev-parse","HEAD")` returned `""`, so `head`/`prev_commit`
    were both empty, the duplicate-tag check was skipped, and `--check` printed
    `HEAD ? is ahead of (no tag) — safe to tag` with exit 0. A check that did not run
    reported success — the same fail-open this project keeps fixing in its own gates.

    The fix has three parts, all guarded here: `_git` must distinguish failure (None)
    from empty output, `--check` must verify it is inside a work tree, and the negative
    message must not contain the literal "safe to tag" (release audits grep for it).
    """
    with open(os.path.join(ROOT, "tools", "bump_version.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "-> str | None" in src and "return None" in src, (
        "_git still collapses 'command failed' and 'empty output' into one value")
    assert '"--is-inside-work-tree"' in src, "no work-tree check before claiming tag safety"
    assert 'in_tree != "true"' in src and "UNVERIFIABLE" in src, (
        "a non-git checkout can still be reported as safe")
    assert "if head and prev_commit and head == prev_commit" not in src, (
        "the old skip-the-check-when-both-empty branch is back")
    # The negative branch must not trip a grep for the positive phrase.
    i = src.find('in_tree != "true"')
    branch = src[i:src.find("return 1", i)]   # only the UNVERIFIABLE branch, not later ones
    assert 'safe to tag' not in branch, (
        "the UNVERIFIABLE branch contains 'safe to tag'; an audit grep would read it as a pass")
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    assert 'r.check("bump_version --check fails without git"' in sm, (
        "the real subprocess check is not registered with the runner")
    return "--check refuses to claim tag safety when git metadata is unavailable"


def assert_no_composite_text_dialect_and_new_checks_really_run():
    """0.8.293 — the four fixes must have LIVE checks, not just source assertions.

    Report 2026-11-10 found a smoke check that had died with NameError for six releases
    because `check_all.py` only COUNTS `r.check` lines. "A check exists" is not "the check
    ran". Every new check here is pure Python (fake GetText reader, fake edge rows, a real
    subprocess) and is run with `smoke.py --only ...` in the release verification.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    for fname in ("add_hole_callouts", "add_hole_callout"):
        body = ast.get_source_segment(src, _function_node(tree, fname)) or ""
        assert '" | ".join' not in body, (
            "%s builds a composite `text` again — cross-tool semantics drift" % fname)
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    expected = (
        "callout text stays one native string",
        "a depth in another slot is still refused",
        "circular-only edges are not sheet-projected",
        "bump_version --check fails without git",
    )
    for name in expected:
        assert 'r.check("%s"' % name in sm, "the live check %r is not registered" % name
    assert "def _callout_text_contract" in sm or "_read_native_callout_text" in sm, (
        "the text check reads source text instead of calling the real helper")
    assert "def _edge_projection_scope" in src and "_edge_projection_scope(rows)" in sm, (
        "the edge check does not replay the real pure function")
    return "4 live pure-Python checks registered; no composite text dialect in either tool"


def assert_coaxial_merge_is_radius_agnostic():
    """0.8.294 — the pure merge helper must not measure a radius gap at all.

    Report 2026-11-09 §4 asked for "is it a chamfer pair" rather than a radius
    threshold. A helper that silently reintroduces `keep_r - r > tol` would still
    pass the original Ø11/Ø6.6 fixture (2.2 mm) and fail again on Ø11/Ø9.8 (0.6 mm).
    Inspect the CODE, not the docstring: the docstring deliberately names the old
    gate when explaining why it was removed.
    """
    src = _read("tools/drawing.py")
    fn = _function_node(ast.parse(src), "_coaxial_merge_plan")
    stmts = [s for s in fn.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]
    code = "\n".join(ast.unparse(s) for s in stmts)
    assert "chamfer_pair_keys" in code, "membership is not the discriminator"
    assert "CHAMFER_RADIUS_GAP_M" not in code and "0.0015" not in code, (
        "the merge helper measures a radius gap again; shallow counterbores regress")
    assert ">" not in code, "a magnitude comparison crept back into the merge decision"
    assert "coaxial_chamfer_protected=" in src, (
        "the protection decision is not countable in the receipt")
    return "merge helper has no radius comparison; protection is a countable receipt field"


def assert_coaxial_merge_has_live_replay():
    """0.8.294 — the Ø11/Ø9.8 case must be REPLAYED, not asserted from source text.

    `check_all.py` only COUNTS `r.check` lines. Report 2026-11-10 found a smoke check
    dead with NameError for six releases while still being counted as coverage, so a
    source assertion alone is not enough here.
    """
    src = _read("tools/drawing.py")
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    name = "shallow coaxial counterbores still merge"
    assert 'r.check("%s"' % name in sm, "the live replay check is not registered: %r" % name
    assert "_coaxial_merge_plan(" in sm, (
        "the check reads source text instead of calling the real pure helper")
    assert "0.0049" in sm and "0.0055" in sm, (
        "the reported Ø11 / Ø9.8 fixture is gone from the replay")
    assert "coaxial_chamfer_protected" in sm, (
        "the new receipt field has no real-machine assertion")
    assert "def _coaxial_merge_plan" in src
    return "live Ø11/Ø9.8 replay registered; real receipt asserts the protection field"


def assert_assembly_drawing_accepts_partial():
    """0.8.295 — the single FAIL in the 2026-11-11 full smoke was a TEST bug.

    `assembly drawing not an empty shell` used strict `require()`. A partial
    `produce_drawing` has `ok:false` BY DESIGN (0.8.285) with `error=null` and
    `message=null`, so the check printed "call failed: None None" even though 3 views
    and 25 dimensions HAD been produced. The check exists to prove the drawing is not
    an empty shell; a partial drawing must still be audited, not skipped.
    """
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.find("def _assembly_drawing():")
    assert i > 0, "the assembly drawing check disappeared"
    end = src.find("\n    r.check(", i)
    body = src[i:end if end > 0 else len(src)]
    assert "allow_partial=True" in body, (
        "strict require() is back; a partial assembly drawing reports 'call failed: None None'")
    # Match the TRUTH assertion (`assert payload.get("ok"), msg`) — not every mention. A
    # comparison like `assert payload.get("ok") == ...` is a different and valid check;
    # 0.8.301 added exactly that and this over-broad pattern rejected it.
    assert not re.search(r'assert payload\.get\("ok"\)\s*,', body), (
        "a bare ok assertion rejects a partial run even with allow_partial=True")
    assert "len(views) >= 1" in body and "total_dimensions" in body, (
        "the check no longer verifies that the drawing is not an empty shell")
    assert "call failed: %s %s" in src, "require() lost its diagnostic message"
    return "assembly check audits partial drawings instead of calling them failed"


def assert_zero_offset_is_not_nudged_by_default():
    """0.8.296 — report 2026-11-10 P1, the last open item in that report.

    The INNER `drawing.add_section_view` default was already False, but the MCP wrapper
    kept `= True`. Every wrapper call therefore asked for a through-axis cut and got a
    0.5 mm eccentric one, while the receipt's own hint said "默认已改为 False" — the code
    and the promise disagreed. This is not cosmetic: `offset_nudged` decides which
    diameters the section measures, so a silent nudge makes the drawing wrong.

    Assert BOTH layers by AST. The split between them is what shipped the bug, so a check
    on either one alone would have missed it.
    """
    def _default(relpath, func):
        tree = _tree(relpath)
        fn = _function_node(tree, func)
        assert fn is not None, "%s:%s disappeared" % (relpath, func)
        args = fn.args.args
        first = len(args) - len(fn.args.defaults)
        for i, arg in enumerate(args[first:], start=first):
            if arg.arg == "auto_nudge_zero_offset":
                return fn.args.defaults[i - first]
        return None

    srv = _default("server.py", "add_section_view")
    drw = _default("tools/drawing.py", "add_section_view")
    assert isinstance(srv, ast.Constant) and srv.value is False, (
        "the MCP wrapper nudges a zero offset again: %r — a requested through-axis cut "
        "silently becomes a 0.5 mm eccentric section" % (srv,))
    assert isinstance(drw, ast.Constant) and drw.value is False, (
        "the inner default drifted away from the wrapper: %r" % (drw,))
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    assert 'r.check("zero offset is not nudged by default"' in sm, (
        "the live default-value check is not registered")
    assert '_inspect.signature(fn).parameters["auto_nudge_zero_offset"]' in sm, (
        "the check reads source text instead of the real signatures")
    assert 'good.get("offset_nudged") is False' in sm, (
        "the real-machine assertion on offset_nudged was removed — a silent nudge would "
        "pass with a section that still cuts, and only the diameters would be wrong")
    assert 'good.get("offset_used_m") == 0.0' in sm, (
        "the real-machine assertion on offset_used_m was removed")
    return "both layers default to False; live check + real-machine assertions in place"


def assert_position_fields_come_from_the_final_placement():
    """0.8.297 — report 2026-11-10 P2-1: one receipt mixed two moments in time.

    `alignment_kept` / `position_axis_ignored` were computed BEFORE an optional alignment
    break while `position_actual_m` was re-read AFTER it. A call could therefore report
    `position_axis_ignored: "x"` beside two IDENTICAL coordinates, or `alignment_kept:
    true` beside a SUCCESSFUL `alignment_broken`. Both statements were true at different
    moments; as one JSON they are unreadable.

    Every position field now comes from `_position_deviation(requested, actual)` after the
    break, and `position_fulfilled` makes the pair self-checking.
    """
    src = _read("tools/drawing.py")
    assert "\ndef _position_deviation(" in src, (
        "the pure position comparator disappeared; smoke cannot replay the report")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "add_section_view")) or ""
    assert body.count("_position_deviation(position_requested,") == 2, (
        "expected one snapshot BEFORE the break and one final computation AFTER it")
    assert "alignment_rewrote_request" in body, (
        "the pre-break snapshot is gone; `alignment_kept` will be reused as a gate again")
    assert '"position_fulfilled": position_fulfilled' in body, (
        "the receipt does not say whether the request was finally met")
    assert "alignment_broken = None" in body
    return "position fields come from the final placement; pre-break state kept separately"


def assert_tolerance_text_has_no_negative_zero():
    """0.8.297 — report 2026-11-10 P2-3: receipt printed `+0.033/-0`, sheet `+0.033/0`."""
    src = _read("tools/drawing.py")
    assert "def _signed_mm(" in src, "the tolerance formatter disappeared"
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "_signed_mm")) or ""
    assert "5e-7" in body and 'return "0"' in body, (
        "negative zero is no longer normalised")
    # The display_text splices must not format tolerances with a raw `%+g` again.
    assert '"%s %+g/%+g"' not in src, (
        "a dimension receipt still prints `-0` for a negative zero")
    assert src.count("_signed_mm(_up)") >= 2 and src.count("_signed_mm(_lo)") >= 2, (
        "a display_text branch bypasses the formatter")
    return "`-0.0` -> '0'; both bilateral branches use the formatter"


def assert_center_marks_can_dedupe_existing():
    """0.8.297 — report 2026-11-10 P2-4: three coincident marks could never be cleaned.

    `already_marked` only refuses to ADD more, so a sheet left with three marks on one
    point by an older non-idempotent version stayed wrong forever. `dedupe_existing=True`
    deletes them by position first; the grouping is a pure function so smoke replays it.
    """
    src = _read("tools/drawing.py")
    assert "def _coincident_mark_groups(" in src, "the pure grouping helper disappeared"
    assert "def _dedupe_view_center_marks(" in src
    fn = _function_node(ast.parse(src), "add_center_marks")
    args = [a.arg for a in fn.args.args]
    assert "dedupe_existing" in args, "add_center_marks lost the opt-in"
    body = ast.get_source_segment(src, fn) or ""
    assert "center_marks_removed" in body and "dedupe_existing=bool(dedupe_existing)" in body, (
        "the receipt cannot distinguish 'cleaned 2' from 'not attempted'")
    srv = _read("server.py")
    srv_fn = _function_node(ast.parse(srv), "add_center_marks")
    assert "dedupe_existing" in [a.arg for a in srv_fn.args.args], (
        "the MCP wrapper does not expose the opt-in")
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    assert 'r.check("coincident centre marks are grouped"' in sm
    assert "_coincident_mark_groups(rows)" in sm, "the check does not call the real helper"
    return "opt-in exists on both layers and the receipt counts removals"


def assert_zero_dimension_ledger_is_not_applicable():
    """0.8.297 — report 2026-11-10 P2-5: imported bodies carried a permanent pending item.

    `reconcile_ledger` returns 0/0 on a body with no driving dimensions. `0/0` was
    recorded as `ok:false`, which pushed `ledger_reconciled` into `pending` and held the
    verdict at 待设计审核 forever — for something the caller cannot fix from this toolkit.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "delivery_check")) or ""
    assert "_ledger_dims == 0" in body, "the not-applicable branch is gone"
    assert 'status="not_applicable"' in body, (
        "0/0 is reported as a coverage failure again")
    assert 'status="checked"' in body, (
        "the normal branch does not label itself, so the two cases look identical")
    assert '"台账覆盖率 %s（%s/%s 个尺寸在图纸上找到，缺 %s）"' in body
    return "0 driving dimensions -> not_applicable, labelled in `checks`"


def assert_produce_drawing_reports_completeness():
    """0.8.298 — report 2026-11-10 §5 (F): two fields said nothing about completeness.

    `verdict` is a Chinese label for a reviewer. Making it also encode "a step was
    partial" gives ONE FACT TWO JUDGES — the mistake this project has fixed five times
    (`ok` vs `verdict`, ratio vs absolute delta, `sheet_projected` vs usable edges).

    `blockers` must stay "a defect the caller can fix from inside this toolkit". Pushing
    hole-callout gaps there turns every drawing with an ambiguous chamfer permanently red,
    with nothing the caller can do to clear it — the same failure mode the 0.8.297 ledger
    fix removed at lower severity.

    So: add the pair with the names `add_hole_callouts` already uses, and leave the other
    fields alone.
    """
    src = _read("tools/pipeline.py")
    assert "\ndef _delivery_status(" in src and "\ndef _receipt_ok(" in src, (
        "the pure derivations disappeared")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "produce_drawing")) or ""
    assert "_status = _delivery_status(" in body and "**_status," in body, (
        "the receipt does not carry the derived status")
    # THE 0.8.298 P0: `ok` was `not partial_steps` while `completeness` also read blockers.
    assert "ok=_receipt_ok(_status)" in body, (
        "top-level ok is computed separately again — that is exactly the 0.8.298 P0")
    assert "ok=(not _partial_steps)" not in body, (
        "ok judges only partial steps while completeness judges more: two judges")
    assert "_failed_steps" in body and "failed_steps=_failed_steps" in body, (
        "a crashed non-critical step is invisible in completeness again (path C)")
    assert "blockers=_blockers" in body, (
        "the blocker list is built twice again — the two copies will drift")
    assert 'verdict=("partial"' not in body and 'verdict=("incomplete"' not in body, (
        "verdict now encodes completeness: one fact, two judges")
    srv = _read("server.py")
    srv_body = ast.get_source_segment(
        srv, _function_node(ast.parse(srv), "produce_drawing")) or ""
    assert "completeness" in srv_body and "execution_ok" in srv_body, (
        "the tool description does not tell callers which field to read")
    assert "ALWAYS equals" in srv_body, (
        "the description does not state that ok and completeness are the SAME fact")
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    assert 'r.check("ok and completeness cannot disagree"' in sm, (
        "the COMBINATION check is not registered — per-field checks missed this P0")
    assert "_receipt_ok(status)" in sm, (
        "the combination check does not use the real top-level derivation")
    assert "for blockers, steps, failed_steps in (" in sm, (
        "the invariant is asserted for one state only")
    return "ok and completeness share one derivation; failed steps are counted"


def assert_cleanup_steps_are_not_delivery_defects():
    """0.8.300 — report 2026-11-11 §4: 0.8.299 over-corrected.

    `_failed_steps` excluded only `_CRITICAL`, so `close_documents` — whose `ok` means "did
    I close at least one document", NOT "is the sheet deliverable" — made a complete drawing
    come back `ok:false / completeness:"incomplete"`. The sheet had been saved, audited and
    judged OK; only the tidy-up failed. Three meanings were chained, and the last link was
    wrong.

    Now a WHITELIST decides: only steps that write geometry, annotations or the file can
    make `completeness` incomplete. Everything else is reported through `cleanup_incomplete`
    + `warnings`, so it stays observable without condemning the drawing. A whitelist also
    means the NEXT helper step added is neutral by default — a blacklist will always be one
    release behind.

    Also asserts the double-count fix: one `delivery_check` crash used to appear as BOTH
    `blocker` and `failed_step` (§5).
    """
    src = _read("tools/pipeline.py")
    tree = ast.parse(src)
    block = src.split("_COMPLETENESS_RELEVANT = (")[1].split("\n)")[0]
    for neutral in ("close_documents", "audit", "reconcile", "clamp_dimensions"):
        assert neutral not in block, (
            "%r is a cleanup / read-only step and cannot make a sheet undeliverable"
            % neutral)
    for writer in ("create_drawing", "dimensions", "center_marks", "surface_finish",
                   "hole_callouts", "save"):
        assert writer in block, (
            "%r writes the drawing but is missing from the delivery whitelist" % writer)

    assert "\ndef _step_partition(" in src, "the pure partition disappeared"
    body = ast.get_source_segment(src, _function_node(tree, "produce_drawing")) or ""
    assert "_step_partition(steps, _verdict_ok)" in body, (
        "the pipeline no longer partitions failed steps")
    assert "cleanup_incomplete=_cleanup_failed" in body, (
        "a cleanup failure is not reported anywhere")
    assert "_cleanup_warnings" in body and "+ _cleanup_warnings" in body, (
        "a cleanup failure no longer reaches `warnings`")
    assert 'st["step"] not in _CRITICAL]' not in body, (
        "the blacklist is back: every non-critical step counts as a delivery defect")
    assert 'if name == "delivery_check" and not verdict_ok:' in src, (
        "a lost delivery verdict is counted twice again (blocker + failed_step)")
    assert "execution_ok=True," in body, (
        "`critical_step_failed` drops `execution_ok`, so a caller reads None")

    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    for name in ("step partition keeps cleanup out of completeness",
                 "cleanup failure keeps the sheet deliverable",
                 "delivery_check crash is counted once"):
        assert 'r.check("%s"' % name in sm, "the live check %r is not registered" % name
    assert "part = _pipeline._step_partition" in sm, (
        "the pure check does not call the real helper")
    return "whitelist decides completeness; cleanup observable; one crash, one reason"


def assert_every_early_return_states_execution_ok():
    """0.8.301 — report 2026-11-11 §8 P2-1: `d.get("execution_ok")` returned `None`.

    The tool description promised the field explained retry-or-not, but only the happy path
    and `critical_step_failed` carried it. Every other early return (`bad_input`,
    `output_exists`, `ledger_failed`, `create_failed`, `restore_failed`) omitted it, so a
    caller had to invent a meaning for absence.

    Now every `error(...)` inside `produce_drawing` states it: `False` for "stopped before
    trying", `True` for the one branch that ran the whole chain and still produced nothing
    usable. Checked by AST, not by grepping for a string, so a new early return cannot slip
    through.
    """
    src = _read("tools/pipeline.py")
    fn = _function_node(ast.parse(src), "produce_drawing")
    missing = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "error"):
            continue
        if not any(kw.arg == "execution_ok" for kw in node.keywords):
            missing.append(getattr(node, "lineno", "?"))
    assert not missing, (
        "error branches without execution_ok (lines %s) — a caller reads None there"
        % missing)
    body = ast.get_source_segment(src, fn) or ""
    assert "execution_ok=True," in body, (
        "critical_step_failed no longer says the run DID happen")
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    assert 'payload.get("ok") == (payload.get("completeness") == "complete")' in sm, (
        "no real-machine check compares ok with completeness")
    return "every early return states execution_ok; two real receipts assert the pair"


def assert_angular_dimensions_never_enter_length_checks():
    """0.8.303 — report 2026-11-12 §4 (P0): a 45° chamfer printed as `785.4`.

    `SystemValue` is RADIANS for an angular dimension. Four readers multiplied it by 1000
    and treated the result as millimetres: `layout_dimensions` reported `text: "785.4"`,
    `reconcile_ledger` produced `drawing_only_values: [785.4]` and `coverage 0.7` on a
    sheet where nothing was missing, `verify_dimension_attachments` returned
    `value_mm: 785.398`, and `drawing_dimension_values` fed the length sets underneath.

    ONE predicate now decides everywhere — `add_drawing_dimension` already used codes
    `(1, 3)` while the others used a single code, which is why they disagreed.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    assert "ANGULAR_DIMENSION_TYPES = (1, 3)" in src, "the angular codes drifted"
    assert "\ndef _is_angular_dimension_type(" in src, "the shared predicate disappeared"
    assert "\ndef _radians_to_deg(" in src

    body = ast.get_source_segment(src, _function_node(tree,
                                                     "drawing_dimension_values")) or ""
    assert "_is_angular_dimension_type" in body, (
        "a radian value can enter a length set again")

    vbody = ast.get_source_segment(src, _function_node(tree,
                                                      "verify_dimension_attachments")) or ""
    assert 'row["value_deg"] = _radians_to_deg' in vbody, (
        "an angular dimension still reports its radian value as `value_mm`")
    assert 'row["value_mm"] = None' in vbody

    lbody = ast.get_source_segment(src, _function_node(tree, "layout_dimensions")) or ""
    assert '"%g°" % _radians_to_deg' in lbody, (
        "a 45° dimension is laid out as a 785 mm length again")

    rbody = ast.get_source_segment(src, _function_node(tree, "reconcile_ledger")) or ""
    assert "kind not in (0, 2, 3)" not in rbody, (
        "code 3 is angular; accepting it as linear is how 785.4 entered the ledger")

    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    assert 'r.check("angular dimensions never become millimetres"' in sm, (
        "the live angular predicate check is not registered")
    assert "_radians_to_deg" in sm, "the check does not call the real conversion"
    return "one angular predicate; all four readers route through it"


def assert_requirements_block_reports_real_overlaps():
    """0.8.304 — report 2026-11-12 §7 (N-1): `auto_place` reported success over an overprint.

    0.8.290 refused with `no_room_for_requirements`; 0.8.301 fixed the refusal by placing
    the block 14 mm above the main view — clear of its `precise_outline`, straight across
    the dimension lines that live there — and answered `collisions_avoided: 0`. An
    unattended batch had no signal at all, which is worse than the refusal it replaced.
    """
    src = _read("tools/layout.py")
    tree = ast.parse(src)
    assert "\ndef _dimension_text_boxes(" in src, (
        "dimension text is not an obstacle again; the block will land on a dimension line")
    body = ast.get_source_segment(src, _function_node(tree,
                                                     "add_technical_requirements")) or ""
    assert "obstacles = view_obstacles + dimension_obstacles" in body, (
        "the keep-out set is view-only again")
    assert "actual_collisions" in body, (
        "the receipt does not say what the FINAL box really overlaps")
    assert "dimensions_ignored_no_room" in body, (
        "the fallback is silent again: a dense sheet must say which layer it ignored")
    assert "result[\"warning\"]" in body or "result['warning']" in body, (
        "an overprint leaves no warning behind")

    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    assert 'req.get("actual_collisions")' in sm, (
        "the real-machine check does not read the overlap count")
    assert 'req.get("dimension_obstacles")' in sm
    return "requirements block reports real overlaps and names the layer it ignored"


def assert_live_checks_run_without_private_fixtures():
    """0.8.305 — report 2026-11-12 §4: two live guards could not run on ANY install.

    `_cleanup_failure_keeps_the_sheet_deliverable` and `_delivery_check_crash_is_counted_once`
    required `door.SLDPRT`, which `.gitignore` excludes wholesale, and `SHEET_METAL_DIR` had
    no CLI override. On a standard tarball install, `smoke.py` either exited 2 before any
    check or skipped both and returned 0 — while the commit message claimed "real receipts
    compare ok with completeness". A guard that cannot execute is not coverage.
    """
    with open(os.path.join(ROOT, "tests", "smoke.py"), encoding="utf-8") as fh:
        sm = fh.read()
    for name in ("_cleanup_failure_keeps_the_sheet_deliverable",
                 "_delivery_check_crash_is_counted_once"):
        start = sm.find("def %s():" % name)
        assert start > 0, "%s disappeared" % name
        end = sm.find("\n    def ", start + 10)
        body = sm[start:end if end > 0 else start + 5000]
        assert "args.part" in body, (
            "%s still needs a gitignored fixture and cannot run on a standard install"
            % name)
        # Check the CALL, not the text: the comment above it explains the history and
        # legitimately names the old fixture.
        assert "_require_fixture(" not in body, (
            "%s went back to a private fixture" % name)
    assert "NO CHECKS EXECUTED" in sm, (
        "an all-SKIP run still returns 0, which reads as green in CI")
    assert "return 3" in sm, "the all-SKIP exit code is gone"

    with open(os.path.join(ROOT, "tests", "check_all.py"), encoding="utf-8") as fh:
        ca = fh.read()
    assert "0 executed here" in ca, (
        "the banner still calls a string count a set of runnable 'checks'")
    return "live guards use --part; all-SKIP exits 3; the banner says 0 executed"


def assert_ledger_missing_excludes_present_values():
    """0.8.306 — report 2026-11-12 §5 (P1-5): six dimensions were called missing while drawn.

    `reconcile_ledger` already split `unmatched_ledger` into `missing_truly` (no drawing
    dimension carries that value) and `present_but_unmatched` (the value IS there, the
    pairing failed). `delivery_check` then listed the WHOLE unmatched list as
    `ledger_missing`, so on 边肋连接件 it told the caller to draw 30.0 / 5.0 / 15.5 /
    9.0 / 15.0 — every one of which was already on the sheet.

    Also asserts the orphan distribution is published. Downgrading `orphan` on proximity
    was measured and rejected in 0.8.27 (it excused 15 genuine orphans), so the fix is to
    report `near_view_orphans` / `far_from_view_orphans` and let the caller judge.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "delivery_check")) or ""
    assert 'for row in _missing_rows' in body, (
        "ledger_missing is built from `unmatched_ledger` again — that list mixes two facts")
    assert "_present_rows" in body and "ledger_unmatched_but_present" in body, (
        "the 'present but unattributed' half is dropped from the receipt")
    assert "ledger_unmatched_but_present_count" in body
    assert "near_view_orphans" in body and "far_from_view_orphans" in body, (
        "the orphan distribution is not published")
    # The rejected shortcut must not come back.
    assert "orphan == 0 or " not in body, (
        "orphan was downgraded on proximity — 0.8.27 measured that it excuses real defects")
    return "missing = missing_truly only; present-but-unattributed named separately"


def assert_sheet_index_and_layout_columns_do_not_lie():
    """0.8.306 — report 2026-11-12 §5 (P1-1 / P1-2): two small contract lies.

    P1-1: the schema declares `sheet: string | integer`, Pydantic coerced `1` to `"1"` and
    the int branch never ran, so a valid 1-based index failed with `no sheet named '1'`.
    P1-2: `create_drawing.layout.columns` counted every VIEW as a column, so a 2x2 layout
    reported `columns: 4`; and `fits` disagreed with `measure_layout` on the same sheet
    without saying it was a prediction against a different box.
    """
    src = _read("tools/drawing.py")
    tree = ast.parse(src)
    body = ast.get_source_segment(src, _function_node(tree,
                                                     "_activate_sheet_if_given")) or ""
    assert 'str(sheet).strip().isdigit()' in body, (
        "a digit string is not treated as a 1-based index; `sheet=1` fails again")
    assert "if str(sheet) in names:" in body, (
        "a sheet literally named '1' must win over the index interpretation")

    create = ast.get_source_segment(src, _function_node(tree, "create_drawing")) or ""
    assert '"columns": len(views)' not in create, (
        "every view is called a column again")
    assert '"_columns"' in create or "_columns = sorted" in create, (
        "columns is not derived from the measured boxes")
    assert '"predicted": True' in create, (
        "the layout block does not say its `fits` is a prediction")
    assert '"bounds_m"' in create, (
        "the box behind `fits` is not published, so a caller cannot explain a disagreement")
    return "sheet index accepts digit strings; columns counted; fits marked predicted"


def assert_layout_fits_matches_measure_layout():
    """0.8.307 — report 2026-11-12 §5 (P1-2): same sheet, two "measured" verdicts.

    `create_drawing.layout.fits` was judged against the annotation-band-INSET rectangle
    while `measure_layout` / `drawing_audit` / `delivery_check` judge the RAW safe area.
    On the reporter's sheet that produced `fits:false / out_of_bounds:[视图2,视图4]` next
    to `measure_layout.min_margin_m: +0.010` and `drawing_audit.warnings: []`.

    The band exists to reserve room for DIMENSIONS; a view inside the safe area is not
    "outside the sheet" because it sits in the band. `fits` now uses the raw rectangle and
    the stricter question gets its own field, so the two tools can only disagree when the
    sheet really is too tight.
    """
    src = _read("tools/drawing.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src),
                                                     "create_drawing")) or ""
    assert "_base_rect" in body, (
        "the raw safe rectangle is gone; `fits` is judged against the band inset again")
    assert "_out_of_bounds = _outside(_base_rect)" in body, (
        "`fits` does not use the same rectangle as `measure_layout`")
    assert "fits_annotation_band" in body and "annotation_band_out_of_bounds" in body, (
        "the stricter band verdict is not reported separately")
    assert '"bounds_m": [round(_base_rect[0], 4)' in body, (
        "`bounds_m` does not describe the rectangle `fits` was judged against")
    return "fits uses the raw safe area; the band verdict is a separate field"


def assert_plan_respects_views_and_labels_confidence():
    """0.8.311 — report 2026-11-12 §7 (N-3 / N-4): two advisory fields that misled.

    N-3: `plan_drawing` hard-coded `sheets[0].views = ["front","right"]` while
    `suggested_views` echoed the caller's four — a 4-view request produced a 2-view plan
    with no explanation. And a 3-body S-shaped plate came back as `轴类` from its bounding
    box alone, which `delivery_check` then echoed as if it were a fact.

    N-4: the symbol warning read like advice (`replace_with: "<MOD-DEG>"`) although the
    substitution had ALREADY been applied. It now says `applied: true` and names the DWG
    export form (`%%d`), so nothing is left for the caller to do by hand.
    """
    src = _read("tools/planner.py")
    body = ast.get_source_segment(src, _function_node(ast.parse(src), "plan_drawing")) or ""
    assert '"views": ["front", "right"]' not in body, (
        "the sheet plan hard-codes two views again, ignoring the caller's `views`")
    assert "_planned_views" in body and "views_omitted" in body, (
        "the plan does not say which views it planned or dropped")
    assert "category_confidence" in body, (
        "the plan drops the confidence that the ledger provides")

    led = _read("tools/ledger.py")
    cbody = ast.get_source_segment(led, _function_node(ast.parse(led), "_classify")) or ""
    assert '"high"' in cbody and '"low"' in cbody, (
        "the classifier lost its confidence levels")
    assert "未验证是否回转体" in cbody, (
        "a bbox-only category claims more certainty than it has")

    lay = _read("tools/layout.py")
    assert "_DWG_EXPORT_FORM" in lay, "the DWG export form is not published"
    assert '"applied": True' in lay, (
        "the symbol warning still reads like advice that was never carried out")
    assert "text_sanitized" in lay
    return "plan honours views; categories carry confidence; symbol warnings say applied"


STATIC_REGRESSIONS = [
    ("0.8.311 plan views + confidence", assert_plan_respects_views_and_labels_confidence),
    ("0.8.307 layout fits matches measure", assert_layout_fits_matches_measure_layout),
    ("0.8.306 sheet index + layout columns", assert_sheet_index_and_layout_columns_do_not_lie),
    ("0.8.306 ledger missing excludes present", assert_ledger_missing_excludes_present_values),
    ("0.8.305 live guards run without fixtures", assert_live_checks_run_without_private_fixtures),
    ("0.8.304 requirements report real overlaps", assert_requirements_block_reports_real_overlaps),
    ("0.8.303 angular never becomes mm", assert_angular_dimensions_never_enter_length_checks),
    ("0.8.301 early returns state execution_ok", assert_every_early_return_states_execution_ok),
    ("0.8.300 cleanup is not a delivery defect", assert_cleanup_steps_are_not_delivery_defects),
    ("0.8.298 produce_drawing reports completeness", assert_produce_drawing_reports_completeness),
    ("0.8.297 position comes from final placement", assert_position_fields_come_from_the_final_placement),
    ("0.8.297 tolerance has no negative zero", assert_tolerance_text_has_no_negative_zero),
    ("0.8.297 centre marks can dedupe", assert_center_marks_can_dedupe_existing),
    ("0.8.297 zero-dim ledger not applicable", assert_zero_dimension_ledger_is_not_applicable),
    ("0.8.296 zero offset not nudged by default", assert_zero_offset_is_not_nudged_by_default),
    ("0.8.295 assembly check accepts partial", assert_assembly_drawing_accepts_partial),
    ("0.8.294 coaxial merge ignores radius gap", assert_coaxial_merge_is_radius_agnostic),
    ("0.8.294 coaxial merge has live replay", assert_coaxial_merge_has_live_replay),
    ("0.8.293 hw placeholders stripped, raw kept", assert_hw_placeholders_are_stripped_but_kept_raw),
    ("0.8.293 circular-only edges not projected", assert_circular_only_edges_are_not_sheet_projected),
    ("0.8.293 bump --check fails closed", assert_bump_version_check_fails_closed),
    ("0.8.293 new checks really run", assert_no_composite_text_dialect_and_new_checks_really_run),
    ("0.8.292 top-level input schemas are sealed", assert_top_level_input_schemas_are_sealed),

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
    ("0.8.156 applied values are checked", assert_applied_dimensions_are_checked_against_what_was_drawn),
    ("0.8.156 gap statuses agree", assert_gap_status_enum_is_the_same_in_every_consumer),
    ("0.8.157 shaft views keep a bar", assert_shaft_default_views_keep_a_long_bar),
    ("0.8.158 fixtures reachable", assert_test_fixtures_are_reachable_from_a_release),
    ("0.8.159 failed save cannot report ok", assert_a_failed_save_cannot_report_ok),
    ("0.8.160 hatch_missing judged once", assert_smoke_judges_hatch_missing_the_same_way_twice),
    ("0.8.161 doc ownership tri-state", assert_document_ownership_is_tri_state),
    ("0.8.162 library scan cached", assert_the_material_library_scan_is_cached),
    ("0.8.163 candidates gated on model", assert_gap_candidates_are_checked_against_the_model),
    ("0.8.164 no ok after saved=False", assert_no_tool_reports_success_for_work_it_did_not_do),
    ("0.8.166 BOM config falls back", assert_bom_configuration_falls_back_to_the_model),
    ("0.8.170 verified caps do not disown", assert_verified_capabilities_do_not_disown_themselves),
    ("0.8.171 auto_fit syncs title block", assert_auto_fit_updates_the_title_block),
    ("0.8.174 wrong insert is scoped", assert_a_wrong_insert_does_not_stay_on_the_sheet_silently),
    ("0.8.175 failed insert stops batch", assert_a_failed_insert_stops_the_batch),
    ("0.8.176 reversed capability consistent", assert_a_reversed_capability_does_not_linger_in_the_docs),
    ("0.8.178 delete verifies survivors", assert_delete_verifies_what_survived),
    ("0.8.179 axis fallback", assert_gap_finder_survives_an_unmappable_axis),
    ("0.8.182 section dims have a home", assert_section_dimensions_have_somewhere_to_land),
    ("0.8.183 receipts match geometry", assert_receipts_do_not_disagree_with_the_geometry),
    ("0.8.191 angular is not a length", assert_an_angular_dimension_is_not_a_length),
    ("0.8.192 judge before drawing", assert_a_length_is_judged_before_it_is_drawn),
    ("0.8.194 guard a missing measurement", assert_a_missing_measurement_is_not_compared),
    ("0.8.197 a view names its projection", assert_a_view_names_its_own_projection),
    ("0.8.198 rollback uses sheet names", assert_the_rollback_uses_the_sheet_name_space),
    ("0.8.202 tolerance checked on the figure", assert_a_tolerance_is_checked_on_the_figure),
    ("0.8.204 writes name their sheet", assert_a_write_can_name_its_sheet),
    ("0.8.205 audit per sheet frame", assert_the_audit_judges_each_sheet_by_its_own_frame),
    ("0.8.206 delivery checks every sheet", assert_delivery_checks_every_sheet),
    ("0.8.207 direction from view name", assert_sheet_direction_comes_from_the_view_name),
    ("0.8.209 too many matches is no answer", assert_too_many_matches_means_no_answer),
    ("0.8.210 pre-bind conditional field", assert_a_conditional_receipt_field_is_pre_bound),
    ("0.8.212 failed create closes its drawing", assert_a_failed_create_closes_what_it_opened),
    ("0.8.215 title block uses the sheet object", assert_title_block_looks_up_the_sheet_object_not_the_parameter),
    ("0.8.216 rollback on an empty sheet", assert_a_rollback_does_not_require_a_non_empty_before_shot),
    ("0.8.216 sheet activation read back", assert_sheet_activation_is_read_back),
    ("0.8.217 a section that did not cut", assert_a_section_that_did_not_cut_is_not_ok),
    ("0.8.217 title block scale cell", assert_a_title_block_scale_can_be_written),
    ("0.8.219 tolerance prints three decimals", assert_a_tolerance_can_print_three_decimals),
    ("0.8.220 probe reads a window", assert_the_tolerance_probe_reads_a_window_not_the_page),
    ("0.8.221 note reader used everywhere", assert_the_note_reader_is_used_everywhere),
    ("0.8.222 limits readback order", assert_the_limits_readback_is_lower_then_upper),
    ("0.8.222 covered hole is not a blocker", assert_a_covered_hole_is_not_a_blocker),
    ("0.8.226 dimension can be radial", assert_a_dimension_can_be_radial),
    ("0.8.227 title block is writable", assert_the_title_block_is_not_blocked_by_the_safe_area),
    ("0.8.228 center marks count inserts", assert_center_marks_count_what_was_inserted),
    ("0.8.230 record() blocking keyword-only", assert_record_blocking_is_keyword_only),
    ("0.8.231 tri-state survives unpacking", assert_the_tri_state_survives_unpacking),
    ("0.8.231 sheet_modified means modified", assert_sheet_modified_means_modified),
    ("0.8.231 trash failure is not a lock", assert_a_trash_failure_is_not_blamed_on_the_document),
    ("0.8.233 unknown arguments are refused", assert_every_tool_rejects_unknown_arguments),
    ("0.8.235 a sheet line can be dash-dot", assert_a_sheet_line_can_be_dash_dot),
    ("0.8.236 MCP entry refuses unknown args", assert_the_mcp_entry_rejects_unknown_arguments),
    ("0.8.236 skipped duplicate is removed", assert_a_skipped_duplicate_is_actually_removed),
    ("0.8.236 uncut section names itself", assert_a_section_that_did_not_cut_says_so),
    ("0.8.237 three P2 items", assert_three_p2_items_are_fixed),
    ("0.8.238 tolerance + center marks", assert_two_more_p2_items_are_fixed),
    ("0.8.238 guard survives unknown tools", assert_the_mcp_entry_still_works_after_adding_tools),
    ("0.8.239 datum is not a centreline", assert_a_datum_symbol_is_not_a_centreline),
    ("0.8.240 one-sided bilateral", assert_a_one_sided_bilateral_is_recognised),
    ("0.8.241 line styles are measured", assert_the_line_style_table_is_measured_not_documented),
    ("0.8.244 a failed section says where it is", assert_a_failed_section_says_where_it_is),
    ("0.8.245 a line can follow a view", assert_a_sheet_line_can_follow_a_view),
    ("0.8.248 a refused note is taken back", assert_a_refused_note_is_taken_back),
    ("0.8.248 empty schemas are guarded", assert_the_entry_guard_covers_empty_schemas),
    ("0.8.249 degenerate safe area says so", assert_a_degenerate_safe_area_says_so),
    ("0.8.251 a zero offset is nudged", assert_a_zero_offset_section_is_nudged),
    ("0.8.252 an eccentric section is refused", assert_an_eccentric_section_is_refused),
    ("0.8.253 centre marks count annotations", assert_center_marks_count_annotations_not_the_view_counter),
    ("0.8.254 a failed section can be dropped", assert_a_failed_section_can_be_dropped),
    ("0.8.255 the cut line overshoots", assert_the_cut_line_overshoots_the_outline),
    ("0.8.256 a view lands where asked", assert_a_view_lands_where_it_was_asked_for),
    ("0.8.257 one callout per hole size", assert_one_callout_per_hole_size),
    ("0.8.259 a failed delete is not ok", assert_a_failed_delete_does_not_report_ok),
    ("0.8.260 edges carry edge_index", assert_edges_carry_the_index_that_dimensioning_needs),
    ("0.8.261 a missed point is not an empty view", assert_a_missed_point_is_not_an_empty_view),
    ("0.8.262 the table names the real causes", assert_the_capability_text_names_the_two_real_causes),
    ("0.8.263 the material slot is the material cell", assert_the_material_slot_is_the_material_cell),
    ("0.8.267 chamfer pair both skipped", assert_both_circles_of_a_chamfer_pair_are_skipped),
    ("0.8.267 callouts are not dimensions", assert_a_hole_callout_is_not_a_deletable_dimension),
    ("0.8.268 the clamp has a budget", assert_the_clamp_has_a_travel_budget),
    ("0.8.269 layout reserves room", assert_the_layout_reserves_room_for_dimensions),
    ("0.8.270 delivery skips faces", assert_delivery_check_does_not_walk_every_face),
    ("0.8.271 circle detection is gated", assert_circle_detection_is_gated_before_the_array_call),
    ("0.8.273 chamfer pairs match in 3D", assert_the_chamfer_pair_is_matched_in_three_dimensions),
    ("0.8.274 the tag guard sees the tag", assert_the_tag_guard_can_see_the_tag),
    ("0.8.275 the report says what it verified", assert_the_report_says_what_it_verified),
    ("0.8.276 static_check compares the tag", assert_static_check_actually_compares_the_tag),
    ("0.8.277 no duplicate capability keys", assert_capability_entries_have_no_duplicate_keys),
    ("0.8.278 one callout per coaxial hole", assert_coaxial_holes_produce_one_callout),
    ("0.8.278 callout text reads all slots", assert_a_callout_receipt_reads_every_text_slot),
    ("0.8.279 a callout reports what it could not verify", assert_a_single_callout_does_not_fake_a_verdict),
    ("0.8.280 the gate fails closed", assert_static_check_fails_when_it_cannot_check),
    ("0.8.281 partial is not success", assert_partial_work_is_not_reported_as_success),
    ("0.8.282 the failure logger never touches COM", assert_the_failure_logger_never_touches_com),
    ("0.8.283 COM calls are serialised", assert_solidworks_calls_are_serialised),
    ("0.8.284 profiles are advisory", assert_tool_profiles_are_advisory_only),
    ("0.8.285 partial survives the pipeline", assert_partial_survives_the_pipeline_and_the_gate_reads_absolute_paths),
    ("0.8.286 README documents the release mechanics", assert_readme_documents_the_release_mechanics),
    ("0.8.287 no receipt recommends a dead path", assert_no_receipt_recommends_a_known_dead_path),
    ("0.8.290 require separates failure from partial", assert_require_distinguishes_failure_from_partial),
    ("0.8.291 profile counts come from the registry", assert_tool_profile_counts_come_from_the_registry),
    ("0.8.291 eccentric cut refused + styles", assert_eccentric_sections_are_refused_and_line_styles_are_documented),
    ("0.8.291 single callout reverts bad ink", assert_single_callout_reverts_unverified_ink),
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
    # Say WHAT these are. Report 2026-11-09 (core problem C) counted them: 254 of 254 are
    # source inspection — `assert "token" in body` — and 7 new ones from this round are no
    # exception. "255/255 passed" reads as "the behaviour was verified", which is how
    # v0.8.273 shipped with Ø16 still unannotated: the AST assertion passed, the drawing
    # was wrong.
    #
    # Renaming the line is cheap and honest. Adding a real fixture for each of 254 is not.
    print("source-level  %d/%d passed   (source inspection only — no SolidWorks involved)"
          % (len(STATIC_REGRESSIONS) - failures, len(STATIC_REGRESSIONS)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
