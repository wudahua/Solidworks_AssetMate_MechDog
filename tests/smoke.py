"""Live smoke test against a running SolidWorks instance.

Runs the whole chain once and prints a PASS/FAIL table, so a change to one tool
cannot silently break another. Read-only except for files written to a temp dir:
nothing is saved back into the test models.

Usage (SolidWorks must be running):

    python tests/smoke.py
    python tests/smoke.py --assembly "D:/.../asm.SLDASM" --part "D:/.../p.SLDPRT"
    python tests/smoke.py --keep          # keep the temp output directory

Exit codes: 0 all pass, 1 some check failed, 2 environment problem.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

DEFAULT_ASSEMBLY = os.path.join(
    ROOT, "测试用例", "监视器显示器固定支架-rebuild", "装配体_显示器固定支架.SLDASM"
)
DEFAULT_PART = os.path.join(ROOT, "测试用例", "履带-rebuild", "P05_karetku.SLDPRT")
SHEET_METAL_DIR = os.path.join(ROOT, "测试用例",
                               "electric panel box配电箱3D数模图纸 Solidworks设计")


def _model_dir():
    """Directory holding the sheet-metal fixtures (door.SLDPRT / panel box.SLDPRT)."""
    return SHEET_METAL_DIR
# Real parts from four industries; the drawing chain is exercised against these because a
# single hard-coded part cannot represent sheet metal, turned parts and fixtures at once.
DEFAULT_ENG_CASES = os.path.join(ROOT, "测试用例", "工程图测试用例")


class FixtureMissing(Exception):
    """Raised when a test needs a model file that is not in the repository.

    The `测试用例/` tree is local-only (see .gitignore: *.SLDPRT / *.SLDASM), so a fresh
    clone has none of it. Reporting those cases as FAIL made 12/15 look like a code
    regression when it was only a missing fixture — SKIP keeps the two distinguishable.
    """


class Runner:
    def __init__(self):
        self.results = []

    def check(self, name, fn):
        start = time.time()
        status = "PASS"
        try:
            detail = fn() or ""
        except FixtureMissing as exc:
            status, detail = "SKIP", "fixture missing: %s" % exc
        except AssertionError as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:
            status, detail = "FAIL", "%s: %s" % (type(exc).__name__, exc)
            if os.environ.get("SMOKE_TRACE"):
                traceback.print_exc()
        elapsed = time.time() - start
        self.results.append((name, status, elapsed, detail))
        print("%-42s %s  %6.1fs  %s" % (name, status, elapsed, detail), flush=True)
        return status == "PASS"

    def summary(self):
        failed = [r for r in self.results if r[1] == "FAIL"]
        skipped = [r for r in self.results if r[1] == "SKIP"]
        passed = [r for r in self.results if r[1] == "PASS"]
        print("-" * 78)
        line = "%d passed" % len(passed)
        if skipped:
            line += ", %d skipped (fixture missing)" % len(skipped)
        if failed:
            line += ", %d FAILED" % len(failed)
        print(line + "  / %d total" % len(self.results))
        for name, _, _, detail in failed:
            print("  FAIL %s -> %s" % (name, detail))
        for name, _, _, detail in skipped:
            print("  SKIP %s -> %s" % (name, detail))
        return 0 if not failed else 1


def require(payload, key=None, value=None):
    assert isinstance(payload, dict), "expected dict, got %r" % type(payload)
    assert payload.get("ok") is True, "call failed: %s %s" % (payload.get("error"), payload.get("message"))
    if key is not None:
        assert payload.get(key) == value, "%s = %r, expected %r" % (key, payload.get(key), value)
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assembly", default=DEFAULT_ASSEMBLY)
    parser.add_argument("--part", default=DEFAULT_PART)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--skip-export", action="store_true", help="skip STEP export (slow)")
    parser.add_argument("--skip-drawing", action="store_true", help="skip drawing chain (slow)")
    parser.add_argument(
        "--eng-cases", nargs="?", const=DEFAULT_ENG_CASES, default=None,
        help="also run the real-part chain (ledger -> drawing -> annotations -> verdict) "
             "on this folder; omit the value to use the bundled 工程图测试用例 sample",
    )
    parser.add_argument("--eng-sample", type=int, default=2,
                        help="how many real parts to test with --eng-cases (default 2)")
    args = parser.parse_args()

    for path in (args.assembly, args.part):
        if not os.path.exists(path):
            print("missing test model: %s" % path)
            return 2

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    import pythoncom

    pythoncom.CoInitialize()

    from solidworks_assetmate_mechdog import __version__
    from solidworks_assetmate_mechdog import sw_session
    from solidworks_assetmate_mechdog.sw_session import get_sw
    from solidworks_assetmate_mechdog.tools import (assembly, bodies, bom, drawing, export,
                                                    layout, ledger, recolor, capabilities,
                                                    sheet_metal, planner, rename, material)

    try:
        get_sw()
    except Exception as exc:
        print("cannot attach to SolidWorks: %s" % exc)
        return 2

    # A run killed by a timeout (SIGTERM) never reaches the cleanup at the bottom, so its
    # documents stay open in SolidWorks. The next run then fails every model check with
    # OpenDoc6 errors=65536: SolidWorks refuses a second file with the same name
    # (com-quirks section 2). Self-heal — close any document left in an earlier
    # assetmate_smoke_* temp dir before we copy our own fixtures in.
    sw = get_sw()
    stale = []
    for doc in (sw.GetDocuments or ()):
        try:
            if "assetmate_smoke_" in str(doc.GetPathName or ""):
                stale.append(str(doc.GetTitle))
        except Exception:
            continue
    for title in stale:
        try:
            sw.CloseDoc(title)
        except Exception:
            pass
    if stale:
        print("closed %d stale smoke document(s) left by a previous run" % len(stale))

    workdir = tempfile.mkdtemp(prefix="assetmate_smoke_")
    # Copy under new names: SolidWorks refuses to open a second file with the same
    # name as an already-open document (quirks section 2). Copies also guarantee the
    # repo's test models are never written to.
    asm_copy = os.path.join(workdir, "smoke_asm.SLDASM")
    part_copy = os.path.join(workdir, "smoke_part.SLDPRT")
    shutil.copy2(args.assembly, asm_copy)
    shutil.copy2(args.part, part_copy)

    print("AssetMate smoke test  v%s" % __version__)
    print("assembly: %s" % args.assembly)
    print("part    : %s" % args.part)
    print("output  : %s" % workdir)
    print("-" * 78)

    r = Runner()

    def _static():
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import static_check

        tools, problems = static_check.check()
        assert not problems, "; ".join(problems[:3])
        return "%d tools, every module reference resolves" % len(tools)

    r.check("static check (undefined names)", _static)

    r.check("attach + version", lambda: "v" + __version__)

    tree = {}

    def _tree():
        tree.update(require(assembly.assembly_tree(asm_copy)))
        assert tree["components"] > 0, "no components"
        tree["parts"] = sorted({node["part"] for node in tree.get("tree") or [] if node.get("part")})
        assert tree["parts"], "no part names in tree"
        return "%d components / %d part types" % (tree["components"], len(tree["parts"]))

    r.check("get_assembly_tree", _tree)

    def _bad_input():
        payload = assembly.assembly_tree(os.path.join(workdir, "does_not_exist.SLDASM"))
        assert payload.get("ok") is False and payload.get("error") == "bad_input", payload
        assert "file not found" in payload.get("message", ""), payload.get("message")
        return "structured error with reason"

    r.check("bad_input has a reason", _bad_input)

    bom_data = {}

    def _bom():
        bom_data.update(require(bom.export_bom(asm_copy, csv_path=os.path.join(workdir, "bom.csv"))))
        assert bom_data["part_types"] > 0
        assert os.path.exists(os.path.join(workdir, "bom.csv")), "csv not written"
        assert bom_data.get("mass_complete") is True, "mass incomplete: %s" % (
            bom_data.get("warnings") or [])[:3]
        basis = "default-density" if "default density" in (bom_data.get("mass_basis") or "") else "material"
        return "%d rows, mass=%s (%s), warnings=%d" % (
            bom_data["part_types"], bom_data.get("total_mass_kg"), basis, len(bom_data.get("warnings") or []))

    r.check("export_bom + csv", _bom)

    def _mass():
        payload = require(assembly.mass_properties(asm_copy))
        assert payload["mass"] > 0 and payload["volume"] > 0
        assert "mass_basis" in payload, "mass_basis missing"
        return "mass=%s kg, density=%s" % (round(payload["mass"], 6), payload.get("average_density_kg_m3"))

    r.check("get_mass_properties", _mass)

    def _recolor_dry():
        parts = tree["parts"][:2]
        palette = {parts[0]: [0.15, 0.35, 0.8]}
        payload = require(recolor.recolor_assembly(asm_copy, palette, apply=False))
        assert payload.get("dry_run") is True
        assert payload["matched_components"] >= 1
        return "%d matched, dry-run only" % payload["matched_components"]

    r.check("recolor_assembly dry-run", _recolor_dry)

    def _recolor_no_match():
        payload = recolor.recolor_assembly(asm_copy, {"__NO_SUCH_PART__": [1, 0, 0]}, apply=False)
        assert payload.get("ok") is True and payload.get("matched_components") == 0, payload
        assert payload.get("available_parts"), "available_parts missing"
        applied = recolor.recolor_assembly(asm_copy, {"__NO_SUCH_PART__": [1, 0, 0]}, apply=True)
        assert applied.get("ok") is False and applied.get("error") == "no_match", applied
        return "dry-run exploratory, apply refused"

    r.check("zero-match guard", _recolor_no_match)

    def _apply_no_save():
        parts = tree["parts"][:2]
        palette = {parts[0]: [0.2, 0.4, 0.9], parts[1]: [0.9, 0.6, 0.1]}
        payload = require(recolor.recolor_assembly(asm_copy, palette, apply=True, save=False))
        assert payload["painted_components"] >= 1
        assert payload.get("readback_verified") is True, "readback mismatch: %s" % payload.get("readback_sample")
        return "%d painted (not saved), readback verified" % payload["painted_components"]

    r.check("recolor_assembly apply (in memory)", _apply_no_save)

    def _bodies():
        payload = require(bodies.list_bodies(part_copy))
        assert payload["body_count"] >= 1
        return "%d bodies" % payload["body_count"]

    r.check("list_bodies", _bodies)

    def _bodies_dry():
        payload = require(bodies.recolor_bodies(part_copy, {"__NO_SUCH_BODY__": [1, 0, 0]}))
        return "dry-run, %d bodies listed" % payload["body_count"]

    r.check("recolor_bodies dry-run", _bodies_dry)

    def _preview():
        target = os.path.join(workdir, "preview.png")
        payload = require(assembly.export_preview(asm_copy, target, "1280x960"))
        assert payload.get("format") == "png" and os.path.exists(target), payload
        assert not os.path.exists(target[:-4] + ".bmp"), "intermediate BMP left behind"
        return "%d KB png, no bmp residue" % (os.path.getsize(target) // 1024)

    r.check("export_preview png", _preview)

    if not args.skip_export:
        def _step():
            dry = require(export.batch_export([asm_copy], workdir, fmt="step", apply=False))
            assert dry.get("planned"), dry
            applied = require(export.batch_export([asm_copy], workdir, fmt="step", apply=True))
            exported = applied.get("exported") or []
            assert exported, applied
            assert exported[0]["bytes"] > 1024, "step file too small"
            return "%.1f MB step" % (exported[0]["bytes"] / 1024 / 1024)

        r.check("batch_export step", _step)

    if not args.skip_drawing:
        def _drawing():
            drw = os.path.join(workdir, "smoke.SLDDRW")
            pdf = os.path.join(workdir, "smoke.pdf")
            created = require(drawing.create_drawing(
                part_copy, drw, views=["front", "top", "isometric"]))
            assert created.get("saved") and len(created.get("views") or []) == 3, created
            exported = require(drawing.export_drawing(drw, pdf))
            assert exported["bytes"] > 1000, exported
            rendered = require(drawing.render_drawing(pdf, workdir))
            assert rendered["page_count"] >= 1, rendered
            dimensioned = require(drawing.auto_dimension(drw))
            marked = require(drawing.add_center_marks(drw))
            audited = require(drawing.drawing_audit(drw))
            assert audited["view_count"] == 3, audited
            bad = [w for w in (audited.get("warnings") or [])
                   if w.get("issue") in ("view_outside_sheet", "views_overlap")]
            assert not bad, "layout problems: %s" % bad
            return "3 views, %d dims, %d/%d views marked, %d KB pdf, scale %s" % (
                dimensioned.get("total_dimensions") or 0,
                marked.get("views_marked") or 0, marked.get("views_processed") or 0,
                exported["bytes"] // 1024, created["views"][0]["scale"])

        def _drawing_chain_guarded():
            try:
                return _drawing()
            except AssertionError as exc:
                if "pypdfium2" in str(exc):
                    raise FixtureMissing("pypdfium2 not installed (PDF render needs it)")
                raise

        r.check("drawing chain (create/pdf/render/audit)", _drawing_chain_guarded)

    if args.eng_cases:
        def _eng_cases():
            folder = args.eng_cases
            assert os.path.isdir(folder), "eng-cases folder not found: %s" % folder
            pool = glob.glob(os.path.join(folder, "**", "*.SLDPRT"), recursive=True)
            pool += glob.glob(os.path.join(folder, "**", "*.sldprt"), recursive=True)
            assert pool, "no .SLDPRT under %s" % folder
            # one part per industry folder, deterministic pick so runs are comparable
            by_folder = {}
            for item in sorted(pool):
                by_folder.setdefault(os.path.basename(os.path.dirname(item)), []).append(item)
            picks = []
            for name in sorted(by_folder):
                if len(picks) >= args.eng_sample:
                    break
                picks.append(by_folder[name][0])
            assert picks, "no parts selected"

            lines = []
            for src in picks:
                part = os.path.join(workdir, "eng.SLDPRT")
                drw = os.path.join(workdir, "eng.SLDDRW")
                shutil.copy2(src, part)
                led = require(ledger.build_feature_ledger(part, include_faces=False))
                assert led.get("bbox_mm"), "ledger missing bbox"
                created = require(drawing.create_drawing(part, drw, views=["front", "right"]))
                assert created["views"], "no views created"
                dimensioned = require(drawing.auto_dimension(drw, all_views=True))
                marked = require(drawing.add_center_marks(drw))
                audited = require(drawing.drawing_audit(drw))
                bad = [w for w in (audited.get("warnings") or [])
                       if w.get("issue") in ("views_overlap", "view_outside_sheet")]
                assert not bad, "layout problems on %s: %s" % (os.path.basename(src), bad)
                rec = require(drawing.reconcile_ledger(part, drw))
                verdict = require(drawing.delivery_check(part, drw))
                assert verdict.get("verdict"), "delivery_check returned no verdict"
                lines.append("%s: %s/%sf dims, cov %s, %s" % (
                    os.path.basename(src)[:18], dimensioned.get("total_dimensions"),
                    (led.get("stats") or {}).get("driving_dimensions"),
                    rec.get("coverage"), verdict.get("verdict")))
                for name in ("eng.SLDDRW", "eng.SLDPRT"):
                    try:
                        get_sw().CloseDoc(name)
                    except Exception:
                        pass
                for stale in (part, drw):
                    try:
                        os.remove(stale)
                    except Exception:
                        pass
            return " | ".join(lines)

        r.check("eng cases (ledger/draw/annotate/verdict)", _eng_cases)

    # --- v0.8.3 P0 regression: annotations on a drawing that HAS hole callouts ---------
    # list_drawing_annotations wrote row["text"] before creating `row`, so the first
    # callout scanned raised UnboundLocalError. A drawing without callouts never hit it,
    # which is why it shipped — and why this case must create callouts first.
    def _require_fixture(path, label):
        if not os.path.exists(path):
            raise FixtureMissing("%s (expected at %s; 测试用例/ is gitignored)" % (label, path))
        return path

    def _annotations_with_callouts():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_anno.SLDPRT")
        drw = os.path.join(workdir, "smoke_anno.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front", "right"], paper="A3"))
        made = drawing.add_hole_callouts(drw, budget_s=40)
        total = made.get("created_count") or 0
        listed = require(drawing.list_drawing_annotations(drw))
        totals = listed.get("totals") or {}
        callouts = [a for a in (listed.get("annotations") or [])
                    if "callout" in str(a.get("kind"))]
        assert callouts, ("callouts created (%d) but none listed — the text/row ordering "
                          "may have regressed" % total)
        for row_ in callouts:
            assert row_.get("kind"), "callout row missing kind: %r" % (row_,)
            # 0.8.13: AddHoleCallout2 leaves '<MOD-DIAM>' in the text. The SetText
            # rewrite must have turned it into a real diameter before it is listed.
            text = str(row_.get("text") or "")
            assert "<MOD-" not in text.upper(), (
                "callout text is still a raw format code: %r" % text)
        return "created %d, listed %d rows, totals=%s" % (total, len(callouts),
                                                          server_sorted(totals))

    def server_sorted(d):
        return "{%s}" % ", ".join("%s:%s" % (k, d[k]) for k in sorted(d))

    def _dimension_dedupe():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_dedupe.SLDPRT")
        drw = os.path.join(workdir, "smoke_dedupe.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))
        drawing.auto_dimension(drw, all_views=True)
        checked = require(drawing.verify_dimension_attachments(drw))
        unique = checked.get("unique_dimension_count")
        dupes = checked.get("duplicates_skipped")
        assert unique is not None, "unique_dimension_count missing"
        assert unique == len(checked.get("dimensions") or []), (
            "unique_dimension_count=%r but len(dimensions)=%r"
            % (unique, len(checked.get("dimensions") or [])))
        return "unique=%s duplicates_skipped=%s" % (unique, dupes)

    def _bom_table():
        # Use the sheet-metal assembly, not --assembly: the BOM path goes through
        # InsertBomTable4(Indented) and not every assembly reports configurations in a way
        # that populates it. SAB1_0000_000 is the fixture verified for 10x4.
        bom_assembly = [p for p in glob.glob(
            os.path.join(ROOT, "测试用例", "工程图测试用例", "**", "*.SLDASM"), recursive=True)
            if "SAB1_0000_000" in p]
        if not bom_assembly:
            raise FixtureMissing("SAB1_0000_000.SLDASM (测试用例/ is gitignored)")
        drw = os.path.join(workdir, "smoke_bom.SLDDRW")
        require(drawing.create_drawing(bom_assembly[0], drw, views=["front"], paper="A3"))
        made = layout.add_bom_table(drw)
        require(made)
        assert made.get("rows", 0) > 1, "BOM has no data rows (rows=%r)" % made.get("rows")
        assert made.get("columns") == 4, "BOM has %r columns, expected 4" % made.get("columns")
        assert made.get("native_bom"), "not a native BOM"
        return "rows=%s columns=%s headers=%s" % (made.get("rows"), made.get("columns"),
                                                  made.get("headers"))

    r.check("list_drawing_annotations with hole callouts (P0)", _annotations_with_callouts)
    r.check("verify_dimension_attachments dedupe", _dimension_dedupe)
    r.check("add_bom_table (native, populated)", _bom_table)

    # --- editing tools: create → move → delete, all by name -----------------------------
    # None of these were in the smoke run before. delete_view / delete_annotation /
    # move_annotation / layout_dimensions each shipped in a separate release and were only
    # ever verified by hand, once, by me. A tool nobody calls is a tool nobody knows is
    # broken.
    def _editing_by_name():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_edit.SLDPRT")
        drw = os.path.join(workdir, "smoke_edit.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front", "right"], paper="A3"))

        # add_note + add_sheet_line: cheap writes that should show up in the listing
        require(layout.add_note(drw, "SMOKE_NOTE", 0.10, 0.10))
        listed = require(drawing.list_drawing_annotations(drw))
        notes = [a for a in (listed.get("annotations") or []) if a.get("kind") == "note"]
        assert notes, "add_note succeeded but the sheet lists no note"

        # Names are `细节项目N` and the N changes on every rebuild — never hard-code one.
        # (Hard-coding "细节项目256" is exactly what made me chase a phantom bug.)
        target = notes[0]["name"]
        moved = require(layout.move_annotation(drw, target, 0.14, 0.14))
        assert moved.get("applied") is True, "move_annotation did not apply: %r" % moved
        assert moved.get("position_before_m") != moved.get("position_after_m"), (
            "move applied but the readback position did not change: %r" % moved)

        deleted = require(layout.delete_annotation(drw, target))
        assert deleted.get("removed") is True, (
            "delete_annotation reported ok but removed is not true: %r" % deleted)
        assert deleted.get("annotations_after") is not None, (
            "delete_annotation omitted the after-count readback")
        after = require(drawing.list_drawing_annotations(drw))
        still = [a for a in (after.get("annotations") or []) if a.get("name") == target]
        assert not still, "annotation %s still listed after delete" % target

        # delete_dimensions lives in layout, not drawing. A real prune removes each
        # dimension at ~15 s, so scan a view name that matches nothing: the tool
        # still reports removed/kept/failed without a 10-minute teardown.
        require(drawing.auto_dimension(drw))
        pruned = require(layout.delete_dimensions(drw, view_name="__SMOKE_NO_VIEW__"))
        for key in ("removed", "kept", "failed"):
            assert key in pruned, "delete_dimensions missing %r bucket" % key

        # set_view: position readback must reflect the request (or say why it did not)
        views = [v for v in drawing._iter_views(drawing._open_drawing(drw))
                 if getattr(v, "Type", None) != 1]
        assert views, "no model views to reposition"
        res = require(layout.set_view(drw, str(views[0].Name), x_m=0.08, y_m=0.28))
        # P2-12: the docstring promises an alignment receipt; guard all three keys so
        # a future refactor cannot quietly drop them again.
        for key in ("requested", "position_after_m", "aligned_to_parent"):
            assert key in res, "set_view missing %r (P2-12 alignment receipt)" % key

        # layout_dimensions: must run and report counts
        laid = require(drawing.layout_dimensions(drw, dry_run=True))
        for key in ("dimension_count", "collisions", "dry_run"):
            assert key in laid, "layout_dimensions missing %r" % key

        # delete_view last: it removes a view the other checks may have touched
        if len(views) > 1:
            drop = str(views[1].Name)
            gone = require(layout.delete_view(drw, drop))
            assert gone.get("views_after") is not None, "delete_view returned no view list"

        for name in ("smoke_edit.SLDDRW", "smoke_edit.SLDPRT"):
            try:
                get_sw().CloseDoc(name)
            except Exception:
                pass
        return "note add/move/delete + dims prune + set_view + layout + view delete"

    def _health():
        payload = sw_session.health(deep=True)
        assert payload.get("sw_version"), "health() reports no sw_version"
        assert payload.get("error") is None, "health() reports %r" % payload.get("error")
        return "sw %s, active doc %r" % (payload.get("sw_version"), payload.get("active_doc"))

    r.check("edit tools by name (move/delete/layout)", _editing_by_name)
    r.check("health()", _health)

    # --- annotation and layout writers ----------------------------------------------
    def _annotation_writers():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_anno2.SLDPRT")
        drw = os.path.join(workdir, "smoke_anno2.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))

        # sheet sketch primitives — unit trap: sheet coordinates are METRES
        require(layout.add_sheet_line(drw, 0.02, 0.02, 0.06, 0.02))

        # place_views with an explicit order; must return a plan or an error with a reason
        placed = layout.place_views(drw, placements={})
        assert placed.get("ok") is not None, "place_views returned no verdict"

        # circular edges feed hole callouts; must list something or say why not
        edges = drawing.list_circular_edges(drw)
        assert edges.get("ok") is not None, "list_circular_edges returned no verdict"

        # GT frame: symbol may fail, but the call must be structured, not raise
        gt = drawing.add_geometric_tolerance(drw, 0.10, 0.10, "0.05", "A", symbol="position")
        assert gt.get("ok") is not None, "add_geometric_tolerance returned no verdict"
        if gt.get("ok"):
            # The tool reports `symbol` and `values_written` (not a guessed
            # `symbol_written`); a missing readback would hide a failed symbol write.
            assert "symbol" in gt and "values_written" in gt, (
                "GT result omits the symbol/values readback")

        # surface finish: created flag must be explicit
        sf = drawing.add_surface_finish(drw, 0.12, 0.10, "3.2")
        assert sf.get("ok") is not None, "add_surface_finish returned no verdict"

        # dimension tolerance on an existing dimension
        require(drawing.auto_dimension(drw))
        tol = drawing.add_dimension_tolerance(drw, 0.05, upper=0.05, lower=0.05,
                                             apply=False)
        assert tol.get("ok") is not None, "add_dimension_tolerance returned no verdict"

        for name in ("smoke_anno2.SLDDRW", "smoke_anno2.SLDPRT"):
            try:
                get_sw().CloseDoc(name)
            except Exception:
                pass
        return "sheet_line + place_views + circular_edges + GT + finish + tolerance"

    # --- planning and inspection entry points ----------------------------------------
    def _planning():
        caps = require(capabilities.check_capabilities())
        assert caps.get("server_version"), "check_capabilities has no server_version"
        assert caps.get("sw_version"), "check_capabilities has no sw_version"

        plan = planner.plan_drawing(args.part)
        assert plan.get("ok") is not None, "plan_drawing returned no verdict"

        # Do not name this local `ledger`: it shadows the imported module and the
        # signature-guard assertion in regressions.py would read `ledger.get(...)` as a
        # module call that does not exist.
        led = require(ledger.build_feature_ledger(args.part, include_faces=False))
        assert led.get("features"), "ledger returned no features"

        # sheet_metal_qc must reject a non-sheet-metal part cleanly, not guess.
        # `"sheet_metal" in dir()` used to make this a silent no-op: dir() sees only
        # locals, while sheet_metal is a closure variable imported in main().
        qc = sheet_metal.sheet_metal_qc(args.part)
        assert qc.get("ok") is not None, "sheet_metal_qc returned no verdict"
        return "capabilities + plan + ledger + qc(is_sheet_metal=%s)" % qc.get("is_sheet_metal")

    r.check("annotation writers (sheet/GT/finish/tolerance)", _annotation_writers)
    r.check("planning entry points", _planning)

    # --- views, dimensions and title block ------------------------------------------
    def _views_and_dims():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_views.SLDPRT")
        drw = os.path.join(workdir, "smoke_views.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front", "top"], paper="A3"))

        title = drawing.set_title_block(drw, scale="1:2")
        assert title.get("ok") is not None, "set_title_block returned no verdict"

        # section through the middle: must either produce a view or explain the miss
        section = drawing.add_section_view(drw, orientation="vertical", offset_m=0.0,
                                          label="S")
        assert section.get("ok") is not None, "add_section_view returned no verdict"
        # The offset-sign rule is guarded statically by
        # regressions.assert_section_offset_sign. A successful cut must name the
        # created view; a structured failure is acceptable on a fixture with no
        # sectionable view.
        assert "section" in section or section.get("ok") is False, (
            "section result neither warns nor fails — the sanity check may be gone")

        # detail view: scale may be rejected by the parent, but must be reported
        detail = drawing.add_detail_view(drw, center=[0.10, 0.15], radius_m=0.008,
                                        position=[0.28, 0.10], scale=2, label="D")
        assert detail.get("ok") is not None, "add_detail_view returned no verdict"
        if detail.get("ok"):
            assert "scale" in detail and "scale_locked" in detail, (
                "detail result omits the scale readback")

        # edges + edge-anchored dimension: the only manual placement path
        edges = require(drawing.list_drawing_edges(drw, include_points=False))
        assert edges.get("edge_count") is not None, "list_drawing_edges returned no count"
        if edges.get("edge_count"):
            dim = drawing.add_drawing_dimension(drw, edge_index=0, value=None,
                                               offset_x=0.0, offset_y=-0.012)
            assert dim.get("ok") is not None, "add_drawing_dimension returned no verdict"

        for name in ("smoke_views.SLDDRW", "smoke_views.SLDPRT"):
            try:
                get_sw().CloseDoc(name)
            except Exception:
                pass
        return "title block + section + detail + edges + edge dimension"

    # --- part-level tools ------------------------------------------------------------
    def _part_tools():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_part_%s.SLDPRT" % uuid.uuid4().hex[:6])
        shutil.copy2(src, part)

        lib = material.material_library()
        assert lib.get("ok") is not None, "material_library returned no verdict"

        # dry-run first: never mutate the fixture without a dry run in between
        dry = material.set_part_material([part], "Plain Carbon Steel")
        assert dry.get("dry_run") is True, "set_part_material default is not dry-run"

        smart = material.assign_materials([part], apply=False)
        assert smart.get("ok") is not None, "assign_materials returned no verdict"

        fp = sheet_metal.flat_pattern_report(part)
        assert fp.get("ok") is not None, "flat_pattern_report returned no verdict"

        try:
            get_sw().CloseDoc(os.path.basename(part))
        except Exception:
            pass
        return "material library/set/assign + flat pattern report"

    def _hole_callout_single():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_hole.SLDPRT")
        drw = os.path.join(workdir, "smoke_hole.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))
        result = drawing.add_hole_callout(drw, 0.10, 0.10)
        # A miss is fine — what matters is a structured verdict, not an exception
        assert result.get("ok") is not None, "add_hole_callout returned no verdict"
        for name in ("smoke_hole.SLDDRW", "smoke_hole.SLDPRT"):
            try:
                get_sw().CloseDoc(name)
            except Exception:
                pass
        return "structured verdict: ok=%r" % result.get("ok")

    def _rename():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_rename_src.SLDPRT")
        shutil.copy2(src, part)
        result = rename.rename_components(
            part_paths=[part], prefix="SMOKE_", apply=False,
            assembly_path=args.assembly, mode="file")
        assert result.get("ok") is not None, "rename_components returned no verdict"
        assert result.get("dry_run") is True, "rename_components is not dry-run"
        return "dry-run ok=%r, planned=%d" % (
            result.get("ok"), len(result.get("planned") or []))

    # add_flat_pattern_view needs a sheet-metal part with a flat pattern
    def _flat_pattern_view():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_fp.SLDPRT")
        drw = os.path.join(workdir, "smoke_fp.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))
        result = drawing.add_flat_pattern_view(drw, part, position=[0.28, 0.22])
        assert result.get("ok") is not None, "add_flat_pattern_view returned no verdict"
        for name in ("smoke_fp.SLDDRW", "smoke_fp.SLDPRT"):
            try:
                get_sw().CloseDoc(name)
            except Exception:
                pass
        return "verdict ok=%r" % result.get("ok")

    # --- 0.8.6 behavioural guard: total material failure must not report ok:true --
    def _material_write_honesty():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_mat_write.SLDPRT")
        shutil.copy2(src, part)
        # The material name may or may not exist in the library. What matters is the
        # top-level verdict: 0.8.6 always answered ok:true even when every part failed.
        result = material.set_part_material([part], "Plain Carbon Steel",
                                            apply=True, save=False)
        assert result.get("ok") is not None, "set_part_material returned no verdict"
        assert "succeeded" in result and "failed" in result, (
            "set_part_material omits succeeded/failed counts")
        if result.get("failed") and not result.get("succeeded"):
            assert result.get("ok") is False, (
                "every part failed but the top level still says ok:true (0.8.6)")
        try:
            get_sw().CloseDoc(os.path.basename(part))
        except Exception:
            pass
        return "ok=%r succeeded=%s failed=%s" % (
            result.get("ok"), result.get("succeeded"), result.get("failed"))

    # --- reconciliation + delivery verdict, previously only under --eng-cases -----
    def _reconcile_delivery():
        part = os.path.join(workdir, "smoke_part.SLDPRT")
        drw = os.path.join(workdir, "smoke.SLDDRW")
        if not os.path.exists(drw):
            raise FixtureMissing("drawing chain output smoke.SLDDRW (needs drawing chain)")
        rec = drawing.reconcile_ledger(part, drw)
        assert rec.get("ok") is not None, "reconcile_ledger returned no verdict"
        verdict = drawing.delivery_check(part, drw)
        assert verdict.get("ok") is not None, "delivery_check returned no verdict"
        return "reconcile ok=%r, delivery ok=%r, verdict=%r" % (
            rec.get("ok"), verdict.get("ok"), verdict.get("verdict"))

    # --- server wrapper layer: smoke calls tools directly, so check forwarders -----
    def _server_wrapper_contract():
        # server.py is not importable here (mcp is a server-only dependency) and smoke
        # deliberately exercises the tools layer. Verify the wrapper layer statically:
        # every @mcp.tool() forwarder must bind to the real tools-layer signature.
        # 0.8.4 shipped add_bom_table forwarding a kwarg the implementation had dropped.
        import ast as _ast
        import importlib as _importlib
        import inspect as _inspect

        src = open(os.path.join(ROOT, "src", "solidworks_assetmate_mechdog", "server.py"),
                   encoding="utf-8").read()
        tree = _ast.parse(src)
        wrappers = [n for n in tree.body if isinstance(n, _ast.FunctionDef) and any(
            isinstance(d, _ast.Call) and isinstance(d.func, _ast.Attribute)
            and d.func.attr == "tool" for d in n.decorator_list)]
        names = {n.name for n in wrappers}
        for required in ("get_assembly_tree", "get_mass_properties", "health_check",
                         "recent_failures", "produce_drawing", "survey_drawings"):
            assert required in names, "server wrapper %r is missing" % required

        checked, problems = 0, []
        for node in wrappers:
            for call in _ast.walk(node):
                if not isinstance(call, _ast.Call):
                    continue
                f = call.func
                if not (isinstance(f, _ast.Attribute) and isinstance(f.value, _ast.Name)):
                    continue
                if f.value.id in ("sw_session", "json", "sys", "os", "platform"):
                    continue
                try:
                    mod = _importlib.import_module(
                        "solidworks_assetmate_mechdog.tools.%s" % f.value.id)
                    sig = _inspect.signature(getattr(mod, f.attr))
                except (ImportError, AttributeError, TypeError, ValueError):
                    continue
                if any(kw.arg is None for kw in call.keywords):
                    continue
                checked += 1
                try:
                    sig.bind(*([None] * len(call.args)),
                             **{kw.arg: None for kw in call.keywords if kw.arg})
                except TypeError as exc:
                    problems.append("%s -> %s: %s" % (node.name, f.attr, exc))
        assert checked >= 30, "only %d wrapper calls inspected" % checked
        assert not problems, "server wrappers with broken call shapes: %s" % problems[:4]
        return "%d wrappers, %d forwarding calls bind" % (len(wrappers), checked)

    r.check("views, dimensions, title block", _views_and_dims)
    r.check("part tools (material/flat pattern)", _part_tools)
    r.check("single hole callout", _hole_callout_single)
    r.check("rename components (dry-run)", _rename)
    r.check("flat pattern view", _flat_pattern_view)
    r.check("material write honesty (0.8.6)", _material_write_honesty)
    r.check("reconcile ledger + delivery verdict", _reconcile_delivery)
    r.check("server wrapper contract", _server_wrapper_contract)

    code = r.summary()

    # Close the documents this run opened. Otherwise the next run hits the same-name
    # lock ("another file with the same name is already open") and everything fails.
    try:
        sw = get_sw()
        for doc in (sw.GetDocuments or ()):
            try:
                if os.path.normcase(os.path.dirname(doc.GetPathName)) == os.path.normcase(workdir):
                    if doc.GetSaveFlag:
                        doc.SaveSilent
                    sw.CloseDoc(doc.GetTitle)
            except Exception:
                continue
    except Exception:
        pass

    if args.keep:
        print("kept: %s" % workdir)
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
