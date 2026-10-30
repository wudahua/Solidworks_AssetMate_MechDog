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


# ---------------------------------------------------------------- registry

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
