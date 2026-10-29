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


class Runner:
    def __init__(self):
        self.results = []

    def check(self, name, fn):
        start = time.time()
        try:
            detail = fn() or ""
            ok = True
        except AssertionError as exc:
            ok, detail = False, str(exc)
        except Exception as exc:
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
            if os.environ.get("SMOKE_TRACE"):
                traceback.print_exc()
        elapsed = time.time() - start
        self.results.append((name, ok, elapsed, detail))
        print("%-42s %s  %6.1fs  %s" % (name, "PASS" if ok else "FAIL", elapsed, detail), flush=True)
        return ok

    def summary(self):
        failed = [r for r in self.results if not r[1]]
        print("-" * 78)
        print("%d/%d passed" % (len(self.results) - len(failed), len(self.results)))
        for name, _, _, detail in failed:
            print("  FAIL %s -> %s" % (name, detail))
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
    from solidworks_assetmate_mechdog.sw_session import get_sw
    from solidworks_assetmate_mechdog.tools import (assembly, bodies, bom, drawing, export,
                                                    layout, ledger, recolor)

    try:
        get_sw()
    except Exception as exc:
        print("cannot attach to SolidWorks: %s" % exc)
        return 2

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

        r.check("drawing chain (create/pdf/render/audit)", _drawing)

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
    def _annotations_with_callouts():
        src = os.path.join(_model_dir(), "door.SLDPRT")
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
        return "created %d, listed %d rows, totals=%s" % (total, len(callouts),
                                                          server_sorted(totals))

    def server_sorted(d):
        return "{%s}" % ", ".join("%s:%s" % (k, d[k]) for k in sorted(d))

    def _dimension_dedupe():
        src = os.path.join(_model_dir(), "door.SLDPRT")
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
        assert bom_assembly, "BOM fixture SAB1_0000_000.SLDASM not found"
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
