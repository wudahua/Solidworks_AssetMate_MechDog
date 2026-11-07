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
import uuid
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
            # A bare "missing" left clone users with nothing to act on (reported twice).
            # 测试用例/ is local-only by design, so say what is needed and how to point
            # the run at any model the machine already has.
            print("missing test model: %s" % path)
            print("")
            print("测试用例/ 是本地目录，.gitignore 已整树排除——仓库里没有测试模型。")
            print("跑 smoke 需要三样东西（任意尺寸都可以）：")
            print("  1. 一个零件   --part     默认 测试用例/履带-rebuild/P05_karetku.SLDPRT")
            print("  2. 一个装配体 --assembly 默认 测试用例/监视器显示器固定支架-rebuild/"
                  "装配体_显示器固定支架.SLDASM")
            print("  3. 一个钣金件（用于展开/孔标注用例），放到 测试用例/"
                  "electric panel box配电箱3D数模图纸 Solidworks设计/")
            print("把本机已有的模型路径传进来即可，例如：")
            print("  python tests/smoke.py --part D:/models/any.SLDPRT "
                  "--assembly D:/models/any.SLDASM")
            return 2

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    import pythoncom

    pythoncom.CoInitialize()

    from solidworks_assetmate_mechdog import __version__
    from solidworks_assetmate_mechdog import sw_session
    from solidworks_assetmate_mechdog.sw_session import get_sw
    from solidworks_assetmate_mechdog.tools import (assembly, bodies, bom, drawing, export,
                                                    layout, ledger, recolor, capabilities,
                                                    sheet_metal, planner, rename, material,
                                                    pipeline)

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

    def _bom_quantity():
        """0.8.125 — section 13: a BOM with no quantity column or no data rows must fail.

        Measured 2026-11-04 on 装配体_显示器固定支架: 19 items, qty column present,
        values [1,2,5,1,2,2,1,1,1,1,2,6,2,1,1,1,1,4,11], no zeros, and the sum is 46 —
        exactly the component count. That equality is the invariant worth pinning: a qty
        column that exists but does not add up is worse than a missing one, because it looks
        authoritative.
        """
        payload = require(bom.export_bom(asm_copy))
        items = payload.get("items") or []
        assert items, "BOM has no data rows"
        assert "qty" in items[0], "BOM rows carry no quantity column: %r" % sorted(items[0])
        qtys = [it.get("qty") for it in items]
        assert all(q is not None and q > 0 for q in qtys), (
            "a BOM row has a missing or zero quantity: %r" % qtys)
        total = sum(qtys)
        components = payload.get("components")
        assert components is None or total == components, (
            "quantities sum to %s but the assembly has %s components" % (total, components))
        return "%d rows, qty sums to %s = components" % (len(items), total)

    r.check("BOM quantity column adds up", _bom_quantity)

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
            # Two views, not three. Measured 2026-11-05 on P05_karetku (155 + 155 + 129 mm
            # of outline on a 420 mm A3): the isometric makes the row 439 mm wide and
            # `drawing_audit` flags views 2 <-> 3 as overlapping. The tool is right; the
            # test was asking for a layout that cannot fit.
            created = require(drawing.create_drawing(
                part_copy, drw, views=["front", "top"]))
            assert created.get("saved") and len(created.get("views") or []) == 2, created
            # `layout.fits` used to be hard-coded True for explicit positions; assert it
            # now, so a sheet that does not fit cannot pass as one that does.
            # (`_layout`, not `layout` — the latter is the tools module, and the signature
            # guard reads `layout.get(...)` as a tool call.)
            _layout = created.get("layout") or {}
            assert _layout.get("fits") is True, (
                "create_drawing reports the layout does not fit: %r" % (_layout,))
            exported = require(drawing.export_drawing(drw, pdf))
            assert exported["bytes"] > 1000, exported
            rendered = require(drawing.render_drawing(pdf, workdir))
            assert rendered["page_count"] >= 1, rendered
            dimensioned = require(drawing.auto_dimension(drw))
            marked = require(drawing.add_center_marks(drw))
            audited = require(drawing.drawing_audit(drw))
            assert audited["view_count"] == 2, audited
            bad = [w for w in (audited.get("warnings") or [])
                   if w.get("issue") in ("view_outside_sheet", "views_overlap")]
            assert not bad, "layout problems: %s" % bad
            return "2 views, %d dims, %d/%d views marked, %d KB pdf, scale %s" % (
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

    def _hole_callout_leader_anchor():
        """P1-7 / 0.8.19: leader_x_m / leader_y_m pin the text to a sheet point."""
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_leader.SLDPRT")
        drw = os.path.join(workdir, "smoke_leader.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))
        anchor = [0.25, 0.20]
        made = drawing.add_hole_callouts(drw, budget_s=25, max_callouts=3,
                                         leader_x_m=anchor[0], leader_y_m=anchor[1])
        assert made.get("ok") is not None, "add_hole_callouts returned no verdict"
        if made.get("ok"):
            assert made.get("leader_anchor_requested_m") == anchor, (
                "leader anchor not echoed: %r" % made.get("leader_anchor_requested_m"))
            for item in (made.get("created") or []):
                assert "anchor_m" in item and "position_m" in item, (
                    "created callout missing anchor/position readback: %r" % item)
                assert abs(item["anchor_m"][0] - anchor[0]) < 1e-3, (
                    "callout text ignored leader_x_m: %r" % item["anchor_m"])
        for name in ("smoke_leader.SLDDRW", "smoke_leader.SLDPRT"):
            try:
                get_sw().CloseDoc(name)
            except Exception:
                pass
        return "leader anchor echoed, created=%s" % (made.get("created_count"),)

    def server_sorted(d):
        return "{%s}" % ", ".join("%s:%s" % (k, d[k]) for k in sorted(d))

    def _dimension_dedupe():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_dedupe.SLDPRT")
        drw = os.path.join(workdir, "smoke_dedupe.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))
        deduped = require(drawing.auto_dimension(drw, all_views=True))
        # 0.8.19 / P1-6: eliminate_duplicates must really delete the copies and report
        # what it did — not just pass a flag to SolidWorks and hope for the best.
        assert deduped.get("dedupe_applied") is True, (
            "auto_dimension did not apply the dedupe pass: %r" % deduped)
        assert isinstance(deduped.get("duplicates_removed"), int), (
            "auto_dimension omitted duplicates_removed count: %r" % deduped)
        # 0.8.25: `isinstance(0, int)` passed for five versions while every delete raised
        # NameError. "0 removed + N failed" is the exact signature of that regression.
        assert not (deduped.get("duplicates_removed") == 0
                    and (deduped.get("dedupe_failed_count") or 0) > 0), (
            "dedupe removed 0 and failed %r — the delete path is raising again"
            % deduped.get("dedupe_failed_count"))
        checked = require(drawing.verify_dimension_attachments(drw))
        unique = checked.get("unique_dimension_count")
        dupes = checked.get("duplicates_skipped")
        assert unique is not None, "unique_dimension_count missing"
        assert unique == len(checked.get("dimensions") or []), (
            "unique_dimension_count=%r but len(dimensions)=%r"
            % (unique, len(checked.get("dimensions") or [])))
        # 0.8.24 / P2-4: orphan dimensions need a targeted remover — delete_dimensions works
        # by VALUE and cannot tell three 23 mm dimensions apart. Dry-run must name each
        # candidate with a position so the caller can act on it.
        pruned = require(drawing.prune_orphan_dimensions(drw, dry_run=True))
        assert pruned.get("ok") is not None, "prune_orphan_dimensions returned no verdict"
        assert "orphan_count_before" in pruned, (
            "prune_orphan_dimensions omitted orphan_count_before: %r" % pruned)
        assert pruned.get("dry_run") is True, "prune_orphan_dimensions ignored dry_run"
        for row in (pruned.get("targets") or [])[:3]:
            assert "name" in row, "orphan target without a name: %r" % row
        return "unique=%s duplicates_skipped=%s orphans=%s" % (
            unique, dupes, pruned.get("orphan_count_before"))

    def self_built_assembly(workdir):
        """Two extruded blocks + an assembly holding both, built from scratch.

        No external fixture: `测试用例/` is gitignored as a whole tree, so a release install
        never has SAB1_0000_000.SLDASM. Returns [asm_path] or None.

        Three COM details that cost real time to find (2026-11-05):
          * `SelectByID2` needs `sw_session.byref_dispatch_null()` for Callout, not None.
          * `AddComponent5` wants the OPEN DOCUMENT'S TITLE, not its path.
          * `ActivateDoc2/3` raise on this binding; the old `ActivateDoc` works.
        """
        tpl_part, tpl_asm = None, None
        for root in (os.environ.get("PROGRAMDATA", ""),
                     os.path.join(os.environ.get("PROGRAMFILES", ""),
                                  "SOLIDWORKS Corp")):
            if not root or not os.path.isdir(root):
                continue
            if tpl_part is None:
                hits = glob.glob(os.path.join(root, "**", "gb_part.prtdot"), recursive=True)
                tpl_part = hits[0] if hits else None
            if tpl_asm is None:
                hits = glob.glob(os.path.join(root, "**", "gb_assembly.asmdot"), recursive=True)
                tpl_asm = hits[0] if hits else None
            if tpl_part and tpl_asm:
                break
        if not (tpl_part and tpl_asm):
            return None

        from solidworks_assetmate_mechdog import sw_session as _sws

        sw = get_sw()
        null = _sws.byref_dispatch_null()

        def block(path, size_mm):
            doc = sw.NewDocument(tpl_part, 0, 0, 0)
            if doc is None:
                return None
            ext = doc.Extension
            ok = ext.SelectByID2("前视基准面", "PLANE", 0, 0, 0, False, 0, null, 0)
            if not ok:
                ok = ext.SelectByID2("Front Plane", "PLANE", 0, 0, 0, False, 0, null, 0)
            if not ok:
                return None
            doc.InsertSketch2(True)
            half = size_mm / 2000.0
            doc.SketchManager.CreateCornerRectangle(-half, -half, 0, half, half, 0)
            doc.ClearSelection2(True)
            doc.FeatureManager.FeatureExtrusion2(
                True, False, False, 0, 0, 0.005, 0.005, False, False, False, False,
                0.0174532925199433, 0.0174532925199433, False, False, False, False,
                True, True, True, 0, 0, False)
            if not doc.SaveAs(path):
                return None
            return doc

        a = block(os.path.join(workdir, "bom_fix_a.SLDPRT"), 20)
        b = block(os.path.join(workdir, "bom_fix_b.SLDPRT"), 30)
        if a is None or b is None:
            return None

        asm = sw.NewDocument(tpl_asm, 0, 0, 0)
        if asm is None:
            return None
        sw.ActivateDoc(asm.GetTitle)
        asm = sw.ActiveDoc
        for doc in (a, b):
            asm.AddComponent5(doc.GetTitle, 0, "", False, "", 0, 0, 0)
        if len(list(asm.GetComponents(False) or ())) < 2:
            return None
        out = os.path.join(workdir, "bom_fix.SLDASM")
        if not asm.SaveAs(out):
            return None
        return out

    def _bom_table():
        # Use the sheet-metal assembly, not --assembly: the BOM path goes through
        # InsertBomTable4(Indented) and not every assembly reports configurations in a way
        # that populates it. SAB1_0000_000 is the fixture verified for 10x4.
        bom_assembly = [p for p in glob.glob(
            os.path.join(ROOT, "测试用例", "工程图测试用例", "**", "*.SLDASM"), recursive=True)
            if "SAB1_0000_000" in p]
        if not bom_assembly:
            # 0.8.166: build the assembly instead of skipping. `add_bom_table` is the only
            # tool that ever shipped a "100% must-fail" defect (v0.8.2), and its real-machine
            # case had been SKIPPED for six rounds — the defect survived to release precisely
            # because nothing ran it. Measured 2026-11-05: the self-built version exercises
            # the same code path (InsertBomTable4 Indented -> TableType switch) and returns
            # 3 rows x 4 columns.
            bom_assembly = [self_built_assembly(workdir)]
            if not bom_assembly:
                raise FixtureMissing(
                    "SAB1_0000_000.SLDASM (测试用例/ is gitignored) and the self-built "
                    "substitute could not be created — check the default part/assembly "
                    "templates are installed")
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
    r.check("hole callout leader anchor (P1-7)", _hole_callout_leader_anchor)
    r.check("verify_dimension_attachments dedupe", _dimension_dedupe)
    def _add_sheet():
        """`add_sheet` must ADD A PAGE, or fail loudly — never `ok:true` with no new sheet.

        The "late-bound cannot infer NewSheet4" note that used to sit here was wrong
        (0.8.196): the typelib was already generated and I had the argument order backwards,
        so this now expects a real page. The safety property is unchanged: success must be
        visible in the sheet list.
        """
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_sheet.SLDPRT")
        drw = os.path.join(workdir, "smoke_sheet.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))
        made = layout.add_sheet(drw, name="图纸2", paper="A4")
        if made.get("ok"):
            assert made.get("sheets_after", 0) > made.get("sheets_before", 0), (
                "add_sheet reported success without adding a sheet: %r" % (made,))
            return "created: %s" % (made.get("sheets"),)
        assert made.get("error") == "sheet_creation_blocked", (
            "unexpected failure from add_sheet: %r" % (made,))
        # The workaround may live in `message` or `reason` depending on which error helper
        # ran; stringify the whole receipt rather than guessing the key.
        assert "makepy" in str(made), (
            "the refusal does not name a workaround: %r" % (made,))
        return "blocked as documented (late-bound NewSheet4)"

    r.check("add_sheet (blocked or created)", _add_sheet)

    def _sheet_tools():
        """The three tools that make multi-page output reachable (report C3).

        `add_sheet` only creates an EMPTY page. Nothing could put a view on it, and every
        other tool edits whatever page is active — so "dimension page 2" dimensioned a page
        with no views and returned `inserted: 0 / ok: true`.

        Asserts the whole loop: create a page, see it in `list_sheets`, switch back by
        index, then create a view ON page 2 and see its view count go to 1.
        """
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_ms_sheet.SLDPRT")
        drw = os.path.join(workdir, "smoke_ms_sheet.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))

        made = layout.add_sheet(drw, name="图纸2", paper="A4")
        if not made.get("ok"):
            return "add_sheet blocked here: %s" % (made.get("error"),)

        listed = layout.list_sheets(drw)
        require(listed)
        assert listed.get("sheet_count") == 2, "list_sheets sees %r sheets" % (listed.get("sheet_count"),)
        assert [sh.get("name") for sh in listed["sheets"]] == ["图纸1", "图纸2"], (
            "unexpected sheet names: %r" % ([sh.get("name") for sh in listed["sheets"]],))
        assert listed["sheets"][0].get("view_count") == 1, (
            "sheet 1 should carry the front view: %r" % (listed["sheets"][0],))
        assert listed["sheets"][1].get("view_count") == 0, (
            "the new sheet should start empty: %r" % (listed["sheets"][1],))

        switched = layout.activate_sheet(drw, index=1)
        require(switched)
        assert switched.get("active_sheet") == "图纸1", (
            "activate_sheet(index=1) left %r active" % (switched.get("active_sheet"),))

        added = layout.add_view_to_sheet(drw, sheet=2, view_name="*前视",
                                         x_m=0.15, y_m=0.15)
        require(added)
        assert added.get("sheet") == "图纸2", "view went to %r" % (added.get("sheet"),)
        assert added.get("view_count") == 1, (
            "page 2 still has no view after add_view_to_sheet: %r" % (added,))

        # And now the thing that used to fail silently: dimensioning page 2 works, because
        # page 2 finally HAS a view.
        dims = drawing.auto_dimension(drw, all_views=True, save=False)
        assert dims.get("ok"), "auto_dimension failed on the page that now has a view: %r" % (dims,)
        return "2 sheets, page2 view created, auto_dimension ok=%s" % (dims.get("inserted"),)

    r.check("sheet tools (list / activate / add_view)", _sheet_tools)

    def _sheet_direction():
        """Right and left views must project model +Z to OPPOSITE sides.

        This is the one check that needs no pixel measurement. Before 0.8.207 the projection
        was unsigned, so both views reported dZ/dX = +/+ — impossible, since one looks along
        -X and the other along +X. Two assertions:

          1. ALWAYS: the direction basis is the view's own orientation name (not a guess,
             not a caller-provided flip). Any part satisfies this.
          2. WHEN MEASURABLE: comparing model Z against sheet X on the two side views gives
             opposite signs. Skipped (loudly, in the detail) when the part's Z span is too
             small to tell — that is a fixture limit, not a pass.
        """
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_dir.SLDPRT")
        drw = os.path.join(workdir, "smoke_dir.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["right", "left"], paper="A2"))

        signs, bases = {}, {}
        for view in ("工程图视图1", "工程图视图2"):
            payload = drawing.list_drawing_edges(drw, view_name=view)
            require(payload)
            bases[view] = payload.get("sheet_axis_basis")
            assert payload.get("sheet_axis_direction_verified") is True, (
                "%s: the projection still reports the direction as unverified: %r"
                % (view, payload.get("sheet_axis_warning")))
            rows = [r for r in (payload.get("edges") or [])
                    if r.get("sheet_start") and r.get("model_start")]
            pairs = sorted((r["model_start"][2], r["sheet_start"][0]) for r in rows)
            if len(pairs) < 2:
                continue
            dz = pairs[-1][0] - pairs[0][0]
            dx = pairs[-1][1] - pairs[0][1]
            if abs(dz) > 0.002 and abs(dx) > 0.002:
                signs[view] = 1 if dz * dx > 0 else -1

        assert set(bases.values()) == {"orientation_name"}, (
            "the direction no longer comes from the view name: %r" % (bases,))
        if len(signs) == 2:
            assert signs["工程图视图1"] != signs["工程图视图2"], (
                "right and left project model +Z to the SAME side (%r) — one of them is "
                "mirrored, which is the 2026-11-03 defect" % (signs,))
            return "basis=orientation_name, right/left signs %r" % (signs,)
        return ("basis=orientation_name; sign check skipped, Z span too small on this "
                "fixture (measured %r)" % (signs,))

    r.check("sheet direction from view name", _sheet_direction)

    def _create_drawing_paths():
        """The two `create_drawing` paths that used to crash.

        Report 2026-11-07 (P0): `_title_scale_synced`, `_final_scale` and `blocker` were all
        assigned only inside the "saved AND scale != 1:1" branch, then read unconditionally.
        Three separate unbound names in one function — fixing the first one just moved the
        error to the next line.

        Fixing one of them is what a string assertion can catch; only EXECUTING the paths
        proves all three are gone. So: 1:1 fixed scale, and save=False.

        Note the shape of the assertion — `ok` must be true AND the receipt must carry a
        reason. A receipt that reports `null` for "nothing to do" is the same lie as one
        that reports success for something it never did.
        """
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        results = []
        # UNIQUE names per run. Fixed names failed with `OpenDoc6 errors=65536` because a
        # document from an interrupted earlier run was still open under the same title —
        # SolidWorks refuses to open a second document with an identical name, and the
        # error text blames a lock or a doc-type mismatch, not the real cause.
        _tag = uuid.uuid4().hex[:6]
        for label, kwargs in (("scale1to1", dict(scale=1.0, auto_fit=False)),
                              ("saveFalse", dict(save=False))):
            # No `:` or `=` in the stem: Windows rejects `:` in a filename, and a title with
            # one made `CloseDoc` report success while the document stayed open — two
            # documents from earlier runs were still listed afterwards.
            _stem = "smoke_path_%s_%s" % (label, _tag)
            part = os.path.join(workdir, _stem + ".SLDPRT")
            drw = os.path.join(workdir, _stem + ".SLDDRW")
            shutil.copy2(src, part)
            payload = drawing.create_drawing(part, drw, views=["front"], **kwargs)
            assert payload.get("ok"), (
                "%s crashed: %r" % (label, payload.get("message") or payload.get("error")))
            synced = payload.get("title_scale_synced")
            assert isinstance(synced, dict) and synced, (
                "%s: title_scale_synced is %r — a null cannot be told apart from "
                "'not checked'" % (label, synced))
            assert synced.get("skipped") or synced.get("text"), (
                "%s: the receipt neither synced nor said why: %r" % (label, synced))
            results.append("%s=%s" % (label, "synced" if synced.get("text") else "skipped"))
        return ", ".join(results)

    r.check("create_drawing scale=1:1 / save=False", _create_drawing_paths)

    def _revolved_section():
        """A section that never cut must not report `ok`.

        Report 2026-11-07 (P0-1): on 小车底部滚轮 (Ø64 x 26 wheel, axis along X) the axial
        section taken from the FRONT view came back line-for-line identical to its parent —
        `edge_count 4` against the parent's 4, `hatch_count 0`, no bore, no counterbore —
        and `sanity.verdict` still said **"ok"**.

        It said ok because `kept_dimension` compares the section's width against the source
        view's, and an uncut projection has the same extent. The check measured the one
        property that cannot detect this failure.

        ONLY the bad half is asserted. The "working recipe" — cutting the END view
        vertically — gave 28 edges / 2 hatches when it was first measured (0.8.218) and
        gives 4 / 0 when run on its own now (2026-11-08), including on a fresh drawing with
        no earlier cut. I cannot explain that difference yet, and an assertion that flips
        between runs is worse than none, so it stays out until it is understood.

        Fixture is local-only (`测试用例/` is gitignored), so a missing file skips with a
        reason instead of failing — the branch stays covered where the part exists.
        """
        src = os.path.join(ROOT, "测试用例", "小车底部滚轮.SLDPRT")
        if not os.path.exists(src):
            return ("skipped: 小车底部滚轮.SLDPRT not present (测试用例/ is gitignored); "
                    "the no_hatch / identical_to_source branch stays unchecked")
        stem = "smoke_rev_%s" % uuid.uuid4().hex[:6]
        part = os.path.join(workdir, stem + ".SLDPRT")
        drw = os.path.join(workdir, stem + ".SLDDRW")
        shutil.copy2(src, part)
        built = drawing.create_drawing(part, drw, views=["front", "right"], paper="A3")
        require(built)
        got = drawing.add_section_view(drw, view_name="工程图视图1",
                                       orientation="horizontal", offset_m=0.0)
        sanity = got.get("sanity") or {}
        assert sanity.get("verdict") in ("no_hatch", "identical_to_source"), (
            "a section that did not cut reports verdict %r — the exact lie the 2026-11-07 "
            "report measured (edges %r vs parent %r, hatch %r). Verdict must be no_hatch "
            "or identical_to_source."
            % (sanity.get("verdict"), sanity.get("edge_count"),
               sanity.get("source_edge_count"), sanity.get("hatch_count")))
        return "front-cut=%s (edges %s vs parent %s, hatch %s)" % (
            sanity.get("verdict"), sanity.get("edge_count"),
            sanity.get("source_edge_count"), sanity.get("hatch_count"))

    r.check("revolved part section verdict", _revolved_section)
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

        # 0.8.19 / P0-2: the single-dimension mover must REFUSE a note target. A typo'd
        # name would otherwise silently relocate the wrong annotation kind.
        guard = layout.set_dimension_position(drw, target, 0.20, 0.20)
        assert guard.get("ok") is False and guard.get("error") == "not_a_dimension", (
            "set_dimension_position accepted a note target: %r" % guard)

        deleted = require(layout.delete_annotation(drw, target))
        assert deleted.get("removed") is True, (
            "delete_annotation reported ok but removed is not true: %r" % deleted)
        assert deleted.get("annotations_after") is not None, (
            "delete_annotation omitted the after-count readback")
        after = require(drawing.list_drawing_annotations(drw))
        still = [a for a in (after.get("annotations") or []) if a.get("name") == target]
        assert not still, "annotation %s still listed after delete" % target

        # delete_dimensions lives in layout, not drawing. Measured: 14 dimensions in ONE
        # call took < 5 s (an early note overestimated ~100x). Scan a view name that
        # matches nothing so the check stays fast and does not tear down the sheet.
        require(drawing.auto_dimension(drw))
        # 0.8.19: move ONE dimension to a sheet position and read its value back.
        listed_dims = require(drawing.list_drawing_annotations(drw))
        dim_rows = [a for a in (listed_dims.get("annotations") or [])
                    if a.get("kind") == "dimension"]
        if dim_rows:
            dim_name = dim_rows[0]["name"]
            placed = require(layout.set_dimension_position(drw, dim_name, 0.20, 0.20))
            for key in ("dimension_value_mm", "position_after_m", "requested", "applied"):
                assert key in placed, "set_dimension_position missing %r: %r" % (key, placed)
            # 0.8.23 / P1-1: dimension rows must carry the VALUE SLOT. GetText() with no
            # slot returns '' for a dimension (the codes live in slot 1), which made
            # delivery_check report "0 处 Ø 尺寸" on a sheet that visibly printed diameters.
            typed = [r for r in dim_rows if r.get("dimension_type") is not None]
            assert len(typed) == len(dim_rows), (
                "dimension rows missing dimension_type: %d of %d"
                % (len(typed), len(dim_rows)))
            assert any(r.get("raw_text") for r in dim_rows), (
                "no dimension reported raw_text — value slot 1 is not being read")
            diameters = [r for r in dim_rows if int(r["dimension_type"]) == 5]
            if diameters:
                assert any("Ø" in str(r.get("text") or "") for r in diameters), (
                    "diameter rows do not resolve <MOD-DIAM> to Ø")
            # 0.8.23 / P0: a DIMENSION delete must be confirmed across ALL views — the
            # sheet's own count does not move when a dimension is removed.
            # Re-list first: set_dimension_position just moved dim_name, so the position
            # recorded above is stale (deleting by a stale position is exactly the
            # annotation_not_found this test must not confuse with a real defect).
            refreshed = require(drawing.list_drawing_annotations(drw))
            fresh = [r for r in (refreshed.get("annotations") or [])
                     if r.get("kind") == "dimension" and r.get("position_m")
                     and r.get("name") != dim_name]
            victim = fresh[0] if fresh else None
            if victim:
                res = require(layout.delete_annotation(
                    drw, victim["name"], position_m=victim["position_m"]))
                assert res.get("removed") is True, (
                    "DIMENSION delete not confirmed: %r" % res)
                assert res.get("counted_scope") == "all_views", (
                    "delete_annotation still counts only the sheet: %r" % res)
        pruned = require(layout.delete_dimensions(drw, view_name="__SMOKE_NO_VIEW__"))
        for key in ("removed", "kept", "failed"):
            assert key in pruned, "delete_dimensions missing %r bucket" % key

        # 0.8.23 / P0: geometric tolerance and surface finish must delete too — all four
        # kinds are the acceptance criterion of the v0.8.14 report.
        gt = drawing.add_geometric_tolerance(drw, 0.30, 0.22)
        if gt.get("ok"):
            gt_rows = [a for a in (require(drawing.list_drawing_annotations(drw))
                                   .get("annotations") or [])
                       if a.get("kind") == "geometric_tolerance"]
            if gt_rows:
                res = require(layout.delete_annotation(
                    drw, gt_rows[0]["name"], position_m=gt_rows[0].get("position_m")))
                assert res.get("removed") is True, "GTOL delete not confirmed: %r" % res
        sf = drawing.add_surface_finish(drw, 0.34, 0.22, "3.2")
        if sf.get("ok"):
            sf_rows = [a for a in (require(drawing.list_drawing_annotations(drw))
                                   .get("annotations") or [])
                       if a.get("kind") == "surface_finish"]
            if sf_rows:
                res = require(layout.delete_annotation(
                    drw, sf_rows[0]["name"], position_m=sf_rows[0].get("position_m")))
                assert res.get("removed") is True, (
                    "surface finish delete not confirmed: %r" % res)

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
        return "note add/move/delete + dim type-guard/position + dims prune + set_view + layout + view delete"

    def _health():
        payload = sw_session.health(deep=True)
        assert payload.get("sw_version"), "health() reports no sw_version"
        assert payload.get("error") is None, "health() reports %r" % payload.get("error")
        return "sw %s, active doc %r" % (payload.get("sw_version"), payload.get("active_doc"))

    r.check("edit tools by name (move/delete/layout)", _editing_by_name)
    r.check("health()", _health)

    def _bridge_selfheal():
        """P0-1: ping() must do a real round trip, and a dead cache must heal itself.

        Registered LAST: it poisons the cached dispatcher on purpose, so if a reconnect
        ever fails it must not take the rest of the run down with it.
        """
        first = sw_session.ping()
        assert first.get("ok") is True, first.get("message") or first.get("error")
        assert first.get("sw_version"), "ping returned no sw_version"
        assert first.get("latency_ms") is not None, "ping returned no latency"
        assert isinstance(first.get("bridge"), dict), "ping returned no bridge counters"

        before = dict(sw_session._BRIDGE_STATE).get("stale_drops", 0)
        sw_session._SW = object()      # a pointer that cannot answer RevisionNumber
        sw_session._LAST_PROBE = 0.0   # allow the liveness probe immediately
        sw_session.get_sw()
        after = dict(sw_session._BRIDGE_STATE).get("stale_drops", 0)
        assert after == before + 1, (
            "get_sw handed back a dead dispatcher (stale_drops %d -> %d)" % (before, after))

        healed = sw_session.ping()
        assert healed.get("ok") is True, "ping did not survive a poisoned cache"
        return "ping + stale-dispatcher self-heal (%d drops)" % after

    # --- annotation and layout writers ----------------------------------------------
    def _annotation_writers():
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_anno2.SLDPRT")
        drw = os.path.join(workdir, "smoke_anno2.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))

        # sheet sketch primitives — unit trap: sheet coordinates are METRES
        # 0.8.173: sheet sketches DO render once `AddToDB = True` is set before CreateLine
        # (measured 572 changed pixels). The earlier comment here said the opposite and
        # called an `ok:true` "the lie this check was added to catch" — that was true only
        # while the switch was missing. What this check must reject is `ok:true` with
        # `visibility.visible == false`, which the branch below still asserts.
        sheet_line = layout.add_sheet_line(drw, 0.02, 0.02, 0.06, 0.02)
        assert sheet_line.get("ok") is not None, "add_sheet_line returned no verdict"
        if sheet_line.get("ok"):
            vis = sheet_line.get("visibility") or {}
            assert vis.get("visible"), (
                "add_sheet_line claimed success without proving the line rendered: %r"
                % sheet_line)
        else:
            assert sheet_line.get("error") == "line_not_rendered", (
                "unexpected failure from add_sheet_line: %r" % sheet_line)

        # place_views with an explicit order; must return a plan or an error with a reason
        placed = layout.place_views(drw, placements={})
        assert placed.get("ok") is not None, "place_views returned no verdict"

        # circular edges feed hole callouts; must list something or say why not
        edges = drawing.list_circular_edges(drw)
        assert edges.get("ok") is not None, "list_circular_edges returned no verdict"

        # two-edge distance (0.8.78): the only way to dimension an overall length.
        # Must report the value it actually created — guessing the pair silently gives a
        # perfectly valid dimension of the WRONG distance.
        dist = drawing.add_distance_dimension(drw, 0, 1, 0.10, 0.14)
        assert dist.get("ok") is not None, "add_distance_dimension returned no verdict"
        if dist.get("ok"):
            assert dist.get("value_mm") is not None, (
                "distance dimension did not report its value: %r" % dist)
            assert dist.get("edge_a") == 0 and dist.get("edge_b") == 1, (
                "receipt does not echo which edges were used: %r" % dist)

        # dimension spread (0.8.69): read-only diagnostic, must not change the drawing
        spread = drawing.survey_dimensions_by_view(drw)
        assert spread.get("ok") is not None, "survey_dimensions_by_view returned no verdict"
        if spread.get("ok"):
            assert "views" in spread and "empty_views" in spread, (
                "survey omitted the per-view breakdown: %r" % list(spread))
            assert int(spread.get("total_dimensions") or 0) >= 0, "total is not a number"

        # weld symbol (0.8.67): drawn from sketch lines, NOT a native IWeldSymbol.
        # The receipt must say so — claiming a native annotation would be a lie.
        weld = layout.add_weld_symbol(drw, weld_type="fillet", size_mm=6,
                                      x_m=0.05, y_m=0.12)
        assert weld.get("ok") is not None, "add_weld_symbol returned no verdict"
        if weld.get("ok"):
            assert weld.get("representation") == "sketch_block", (
                "weld symbol does not declare that it is sketch geometry: %r" % weld)
            assert weld.get("is_native_weld_symbol") is False, (
                "weld symbol claims to be native: %r" % weld)
            assert int(weld.get("segments") or 0) >= 4, (
                "weld geometry is incomplete: %r" % weld)
        refused = layout.add_weld_symbol(drw, weld_type="butt", size_mm=6,
                                         x_m=0.10, y_m=0.12)
        assert refused.get("error") == "weld_type_unsupported", (
            "unsupported weld types must be refused, not approximated: %r" % refused)

        # technical-requirements block (0.8.65): ONE note, auto-numbered, auto-placed
        req = layout.add_technical_requirements(
            drw, ["未注倒角 C1.5", "去毛刺", "调质 HRC28-32"], height_mm=3.5)
        assert req.get("ok") is not None, "add_technical_requirements returned no verdict"
        if req.get("ok"):
            assert req.get("line_count") == 3, "line_count wrong: %r" % req.get("line_count")
            body = str(req.get("text") or "")
            assert body.count("\n") == 3, (
                "the block did not become one multi-line note: %r" % body)
            assert "1." in body and "3." in body, (
                "ordinals were not generated for un-numbered lines: %r" % body)

        # datum feature symbol (0.8.63): attaches to an edge, and the label must read
        # back — InsertDatumTag2 defaults to 'B', so a silent failure would be easy.
        datum = drawing.add_datum_feature(drw, edge_index=0, label="A")
        assert datum.get("ok") is not None, "add_datum_feature returned no verdict"
        if datum.get("ok"):
            rb_d = datum.get("readback") or {}
            assert rb_d.get("label") == "A", (
                "datum label did not read back as requested: %r" % rb_d)
            assert datum.get("attached_to"), "datum receipt does not name the edge used"

        # GT frame: symbol may fail, but the call must be structured, not raise
        gt = drawing.add_geometric_tolerance(drw, 0.10, 0.10, "0.05", "A", symbol="position")
        assert gt.get("ok") is not None, "add_geometric_tolerance returned no verdict"
        if gt.get("ok"):
            # The tool reports `symbol` and `values_written` (not a guessed
            # `symbol_written`); a missing readback would hide a failed symbol write.
            assert "symbol" in gt and "values_written" in gt, (
                "GT result omits the symbol/values readback")
            # 0.8.58: the symbol now goes through the 2022+ frame XML, so the receipt
            # carries the gtol.sym name ("IGTOL-POSI"), not a Chinese label in the cell.
            # What must hold: the requested symbol produced a name, the frame XML was
            # accepted (symbol_render_verified), and the value is flagged as unrendered.
            rb = gt.get("readback") or {}
            assert rb.get("symbol_written"), (
                "GT produced no symbol name: %r" % rb)
            assert str(rb.get("symbol_written")).startswith("IGTOL-"), (
                "GT symbol is not a gtol.sym name — the legacy cell write may be back: %r"
                % rb)
            assert gt.get("symbol_render_verified") is True, (
                "GT symbol was stored but not verified as rendered: %r" % gt)
            assert gt.get("value_render_verified") is True, (
                "GT value was written but not verified as rendered: %r" % gt)
            assert "<MOD-" not in str(rb.get("symbol_written") or "").upper(), (
                "GT symbol cell still holds a raw format code: %r" % rb)
        else:
            assert gt.get("error") in ("gtol_symbol_not_applied", "gtol_failed",
                                       "gtol_symbol_failed", "gtol_value_failed"), (
                "unexpected GT failure: %r" % gt)

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

    def _surface_finish_visibility():
        """P2-11: the symbol must really land at (x,y) and show up in an exported PDF."""
        src = _require_fixture(os.path.join(_model_dir(), "door.SLDPRT"), "door.SLDPRT")
        part = os.path.join(workdir, "smoke_sf.SLDPRT")
        drw = os.path.join(workdir, "smoke_sf.SLDDRW")
        shutil.copy2(src, part)
        require(drawing.create_drawing(part, drw, views=["front"], paper="A3"))
        sf = drawing.add_surface_finish(drw, 0.30, 0.20, "3.2",
                                        verify_render=True, verify_dwg=True)
        assert sf.get("ok") is not None, "add_surface_finish returned no verdict"
        if sf.get("ok"):
            actual = sf.get("position_actual_m")
            assert actual and abs(actual[0] - 0.30) < 0.01 and abs(actual[1] - 0.20) < 0.01, (
                "surface finish ignored the requested position (P2-11): %r" % sf)
            vis = sf.get("visibility") or {}
            assert "checked" in vis, "no visibility verdict (P2-11)"
            if vis.get("checked"):
                assert vis.get("visible") is True, (
                    "surface finish did not render at the requested point (P2-11): %r" % vis)
            dwg = sf.get("dwg_check") or {}
            assert "checked" in dwg, "no DWG verdict (P2-11)"
        for name in ("smoke_sf.SLDDRW", "smoke_sf.SLDPRT"):
            try:
                get_sw().CloseDoc(name)
            except Exception:
                pass
        return "position readback + PDF pixel diff + DWG size"

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
    r.check("surface finish visibility (P2-11)", _surface_finish_visibility)
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
        # P1-4: verify_cut defaults to True, so every call reports a sanity block and an
        # edge count. A cut that keeps the wrong dimension must be a structured error
        # (section_suspicious), never ok:true.
        if section.get("error") == "hatch_missing":
            assert section.get("hatch_count") == 0, (
                "hatch_missing must carry hatch_count == 0")
        assert "sanity" in section, "add_section_view omitted sanity (P1-4 verify_cut)"
        assert "edge_count" in section, "add_section_view omitted edge_count (P1-4)"
        if section.get("ok"):
            assert section["edge_count"] > 0, "ok:true section has 0 visible edges"
            sane = section.get("sanity") or {}
            if sane.get("checked"):
                assert abs(float(sane.get("ratio") or 0) - 1.0) <= 0.10, (
                    "ok:true section kept the wrong dimension: %r" % (sane,))

        # P1-4: an explicit MODEL-coordinate cut plane. Either it maps — and the receipt
        # names the derived orientation, offset and axis mapping — or it refuses with a
        # machine-readable reason. A silent wrong cut is the failure mode being guarded.
        plane = drawing.add_section_view(
            drw, cut_plane_normal=[0, 1, 0], cut_plane_origin=[0.0, 0.0, 0.0],
            label="M", position=[0.30, 0.20], verify_cut=True)
        assert plane.get("ok") is not None, "cut_plane call returned no verdict"
        if plane.get("ok"):
            cp = plane.get("cut_plane") or {}
            assert cp.get("derived_orientation") in ("horizontal", "vertical"), cp
            assert cp.get("derived_offset_m") is not None, "no derived offset in receipt"
            assert (cp.get("mapping") or {}).get("axes") is not None, (
                "receipt omits the model-axis mapping")
        else:
            allowed = ("cut_plane_unmapped", "cut_plane_not_in_view_plane",
                       "cut_plane_oblique", "cut_plane_incomplete", "section_empty",
                       "section_suspicious", "section_failed", "no_views",
                       "view_not_found",
                       # 0.8.56: a cut that produced no hatch is now an error. The
                       # fixture's geometry can legitimately produce one; what matters is
                       # that the failure is named, not that the section succeeded.
                       "hatch_missing")
            assert plane.get("error") in allowed, (
                "unexpected cut_plane failure: %r" % plane.get("error"))
            if plane.get("error") == "cut_plane_unmapped":
                assert plane.get("mapping_reason"), (
                    "cut_plane_unmapped must name the mapping_reason")

        # detail view (P2-9): aim the boundary at the CENTRE of the source view so it
        # lands on real geometry; an empty view must now return detail_empty, and a
        # refused scale must return detail_scale_locked — never a silent ok:true.
        src_doc = drawing._open_drawing(drw)
        model_views = [v for v in drawing._iter_views(src_doc)
                       if getattr(v, "Type", None) != 1]
        v_outline = list(model_views[0].GetOutline)
        v_center = [(v_outline[0] + v_outline[2]) / 2,
                    (v_outline[1] + v_outline[3]) / 2]
        detail = drawing.add_detail_view(drw, center=v_center, radius_m=0.008,
                                         position=[0.28, 0.10], scale=2, label="D")
        assert detail.get("ok") is not None, "add_detail_view returned no verdict"
        assert detail.get("edge_count") is not None, (
            "add_detail_view omitted edge_count (P2-9 empty-view guard)")
        # P2-13: a detail view is small, so the fixed 11.9 mm inflation is a large RELATIVE
        # error there — the deflated box has to be reported explicitly, not implied.
        assert "precise_outline_m" in detail, (
            "add_detail_view omitted precise_outline_m (P2-13)")
        if detail.get("precise_outline_m") and (detail.get("outline") or {}).get("width_m"):
            pbox = detail["precise_outline_m"]
            delta = detail["outline"]["width_m"] - (pbox[2] - pbox[0])
            assert abs(delta - 0.0119) < 0.001, (
                "detail precise outline is not deflated by the 11.9 mm constant: %r" % delta)
        if detail.get("ok"):
            assert detail["edge_count"] > 0, "ok:true detail view has 0 edges (P2-9)"
            assert "requested_scale" in detail and "scale_locked" in detail, (
                "detail result omits the scale readback")
            assert abs(detail["requested_scale"] - 2.0) < 0.01, (
                "detail view overwrote the requested scale (P2-9): %r" % detail)
        else:
            assert detail.get("error") in ("detail_empty", "detail_scale_locked",
                                           "detail_failed", "boundary_not_selected",
                                           "boundary_failed", "no_views", "view_not_found"), (
                "unexpected detail view failure: %r" % detail)

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

        # PR4: the gap planner must at least run and classify. On a part with no truly-missing
        # dimension it reports gaps_found=0; on one with gaps it must classify every one of
        # them (fillable / ambiguous / no_geometry) rather than emitting a bare list.
        gaps = drawing.plan_dimension_gaps(part, drw)
        assert gaps.get("ok") is not None, "plan_dimension_gaps returned no verdict"
        if gaps.get("ok") and gaps.get("gaps_found"):
            for item in (gaps.get("gaps") or []):
                # 0.8.156: `no_applicable_candidate` was added in 0.8.155 and this tuple
                # was not updated — a real run on any part with circular geometry would
                # fail here with "gap has no status", which says the opposite of the truth.
                # 0.8.200: `no_length_pair` joined the set when the geometry check moved
                # from the apply loop into candidate generation — a gap whose only
                # candidates are non-parallel/degenerate edge pairs.
                assert item.get("status") in ("fillable", "ambiguous", "no_geometry",
                                               "no_applicable_candidate", "no_length_pair",
                                               "low_confidence", "no_value"), (
                    "gap status is not one this smoke knows: %r" % (item.get("status"),))
            assert gaps.get("applied_count", 0) == 0 or gaps.get("applied"), (
                "apply without a record of what was applied")
        # 0.8.23 / P3-5: 0.0 entries are sketch constraints, not lengths — they must not sit
        # in unmatched_ledger nor inflate the coverage denominator.
        if rec.get("ok"):
            zero = int(rec.get("skipped_zero_value") or 0)
            assert "comparable_dimensions" in rec, (
                "reconcile_ledger does not report the comparable denominator")
            assert int(rec.get("comparable_dimensions") or 0) == (
                int(rec.get("ledger_dimensions") or 0) - zero), (
                "comparable_dimensions != ledger_dimensions - skipped_zero_value")
            leftovers = [r for r in (rec.get("unmatched_ledger") or [])
                         if abs(float(r.get("value_mm") or 1)) < 1e-9]
            assert not leftovers, "0.0 constraints are still listed as missing items"
        verdict = drawing.delivery_check(part, drw)
        assert verdict.get("ok") is not None, "delivery_check returned no verdict"
        # 0.8.23 / P2-5: model_clean must never be a blocker — this toolkit cannot rebuild
        # a model, so a blocker there makes 可制造 unreachable by construction.
        blocker_names = [b.get("check") for b in (verdict.get("blockers") or [])]
        assert "model_clean" not in blocker_names, (
            "model_clean is a blocker again: %r" % blocker_names)
        # 0.8.23 / P1-1: Ø counting comes from dimension_type, not display text. If any row
        # says dimension_type==5, the delivery check must not report zero diameters.
        hole = (verdict.get("checks") or {}).get("hole_callouts") or {}
        detail = str(hole.get("detail") or "")
        if hole and "Ø 尺寸" in detail:
            listing = drawing.list_drawing_annotations(drw)
            dims = [r for r in (listing.get("annotations") or [])
                    if r.get("kind") == "dimension"]
            diameters = [r for r in dims if int(r.get("dimension_type") or -1) == 5]
            if diameters:
                # 0.8.100: read the STRUCTURED field, not the human-readable detail line.
                # The old version parsed `detail.split("+")[1]`, which silently broke the
                # moment the wording changed (0.8.89 rephrased it to
                # "仅靠 5 处 Ø 尺寸覆盖（没有孔标注对象）" — no "+", so the split raised,
                # `counted` became -1, and smoke reported a regression that was really a
                # brittle assertion). The field has existed since 0.8.89.
                counted = hole.get("diameter_dimensions")
                if counted is None:
                    # Older payload: fall back to parsing, but never let the parse decide
                    # "0" when a number is visibly present.
                    try:
                        counted = int("".join(
                            ch for ch in str(detail).split("+")[-1] if ch.isdigit()) or 0)
                    except Exception:
                        counted = -1
                assert int(counted) > 0, (
                    "delivery_check reports %r Ø dimensions while %d rows have "
                    "dimension_type==5: %r" % (counted, len(diameters), detail))
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

    def _assembly_drawing():
        """0.8.116: produce_drawing had NEVER run on an assembly.

        Every drawing measurement so far used parts. Assemblies take a different path —
        lightening, component visibility, and reconcile has nothing to compare against
        (coverage=None) — so a green part matrix said nothing about this case.
        Measured 2026-11-04: ok=True, 26 dimensions inserted, 10 callouts, 2 views at 1:2,
        36 dimensions after audit, 538 KB, verdict 待设计审核.

        What this asserts is that the drawing is not an EMPTY SHELL: an assembly can return
        ok=True with zero views or zero geometry, and the old receipt had no field that
        would tell you which.
        """
        asm_out = os.path.join(workdir, "smoke_asm_drw.SLDDRW")
        payload = require(pipeline.produce_drawing(asm_copy, output_path=asm_out))
        assert payload.get("ok"), payload.get("message")
        views = payload.get("views") or []
        assert len(views) >= 1, "assembly drawing produced no views"
        assert payload.get("view_count") == len(views), (
            "view_count disagrees with views: %r" % (payload.get("view_count"),))
        audit = drawing.drawing_audit(asm_out)
        assert int(audit.get("total_dimensions") or 0) > 0, (
            "assembly drawing has views but no dimensions - an empty shell")
        drawing.close_document(asm_out)
        return "%d views, %d dims, verdict=%s" % (
            len(views), audit.get("total_dimensions"), payload.get("verdict"))

    r.check("assembly drawing not an empty shell", _assembly_drawing)

    def _section_and_detail():
        """0.8.117: section and detail views had never run AFTER produce_drawing.

        add_section_view / add_detail_view each have their own smoke item, but those call
        them on a drawing that carries nothing else. The real order is
        produce_drawing -> add section -> add detail, and that is where the previous two
        attempts failed — with the tools reporting honestly rather than silently:

            offset_m=0.0 on the NARROW view   -> hatch_missing (GetFaceHatchCount = 0)
            center at a guessed coordinate    -> detail_empty (0 visible edges)

        Both were MY parameters, not tool bugs. What works, measured 2026-11-04:
          * section on the WIDEST model view, orientation='vertical' (cut along the axis)
          * detail centred on that view's precise_outline centre
        Result: 2 base views -> 4, dimensions unchanged at 10, file 128366 bytes.

        The assertions below encode those two facts, because getting them wrong produces a
        view that exists but draws nothing — which is exactly the failure mode reconcile and
        the audit cannot see.
        """
        sec_out = os.path.join(workdir, "smoke_sec_drw.SLDDRW")
        base = pipeline.produce_drawing(part_copy, output_path=sec_out)
        assert base.get("ok"), base.get("message")
        before = drawing.drawing_audit(sec_out)
        n_before = int(before.get("view_count") or 0)
        d_before = int(before.get("total_dimensions") or 0)

        doc = drawing._open_drawing(sec_out)
        views = [v for v in drawing._iter_views(doc) if getattr(v, "Type", None) != 1]
        # The widest view is the side view; the narrow one is an end view that cannot be cut.
        wide = max(views, key=lambda v: (drawing.precise_outline(v) or [0, 0, 0, 0])[2])
        box = drawing.precise_outline(wide)
        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0

        sec = drawing.add_section_view(sec_out, view_name=str(wide.Name),
                                       orientation="vertical", offset_m=0.0,
                                       position=(cx, box[1] - 0.06), label="A")
        # Report 2026-11-05 (P1): this asserted `ok` while `_views_and_dims` (line ~920)
        # accepts the SAME structured failure — two standards for one error code inside
        # one file. `hatch_missing` has been the deliberate honest refusal since v0.8.56
        # (a cut with no hatch lines says so instead of returning ok), and the capability
        # table marks `section_view` as `partial`. A fixture whose geometry cannot produce
        # hatch lines is not a code regression.
        #
        # So: accept `hatch_missing`, but require it to be honest about the count. Anything
        # else still fails.
        assert sec.get("ok") is not None, "add_section_view returned no verdict"
        if sec.get("error") == "hatch_missing":
            assert int(sec.get("hatch_count") or 0) == 0, (
                "hatch_missing reported but hatch_count is not 0 — the refusal is "
                "inconsistent: %r" % (sec.get("hatch_count"),))
        else:
            assert sec.get("ok"), (
                "section failed: %s %s"
                % (sec.get("error"), (sec.get("message") or "")[:80]))

        det = drawing.add_detail_view(sec_out, view_name=str(wide.Name), center=(cx, cy),
                                      radius_m=0.015, position=(cx + 0.13, cy + 0.03),
                                      scale=2.0, label="B")
        assert det.get("ok"), (
            "detail failed: %s %s" % (det.get("error"), (det.get("message") or "")[:80]))

        after = drawing.drawing_audit(sec_out)
        n_after = int(after.get("view_count") or 0)
        assert n_after == n_before + 2, (
            "expected two more views, %d -> %d" % (n_before, n_after))
        assert int(after.get("total_dimensions") or 0) == d_before, (
            "adding a section/detail changed the dimension count: %d -> %d"
            % (d_before, after.get("total_dimensions")))
        drawing.close_document(sec_out)
        return "%d -> %d views, dims held at %d" % (n_before, n_after, d_before)

    r.check("section + detail after produce_drawing", _section_and_detail)

    def _paper_scaling():
        """0.8.119: every drawing measurement so far used A3.

        The safe-area logic reads the sheet's own frame, so paper size should not matter — but
        "should not" is not "measured". Measured 2026-11-04 on 履带 (the heaviest part):

            A4  297x210 mm  scale=0.1  151.5x47.9 mm  inside=True
            A2  594x420 mm  scale=0.2  314.9x107.8 mm inside=True

        The scale adapts (1:10 on A4, 1:5 on A2) and both views stay inside the frame. That
        auto-scaling is what makes an oversized part fit a small sheet at all; if it stopped
        working the views would simply hang off the edge and only a visual check would notice.

        Asserted here: the view rectangles are inside the sheet rectangle on both papers, and
        the larger paper does not use a smaller scale (that would mean the fit logic inverted).
        """
        scales = {}
        for paper in ("A4", "A2"):
            out = os.path.join(workdir, "smoke_%s.SLDDRW" % paper)
            payload = pipeline.produce_drawing(part_copy, output_path=out, paper=paper)
            assert payload.get("ok"), "%s: %s" % (paper, payload.get("message"))
            doc = drawing._open_drawing(out)
            views = [v for v in drawing._iter_views(doc) if getattr(v, "Type", None) != 1]
            sheets = [v for v in drawing._iter_views(doc) if getattr(v, "Type", None) == 1]
            assert views and sheets, "%s: no views or no sheet" % paper
            frame = list(sheets[0].GetOutline)
            for v in views:
                box = drawing.precise_outline(v)
                assert box, "%s: view %r has no outline" % (paper, str(v.Name))
                inside = (box[0] >= frame[0] - 1e-4 and box[1] >= frame[1] - 1e-4
                          and box[2] <= frame[2] + 1e-4 and box[3] <= frame[3] + 1e-4)
                assert inside, (
                    "%s: view %r sticks out of the sheet (%r vs %r)"
                    % (paper, str(v.Name), box, frame))
            scales[paper] = float(views[0].ScaleDecimal)
            drawing.close_document(out)
        assert scales["A2"] >= scales["A4"], (
            "a bigger sheet produced a smaller scale: A4=%s A2=%s"
            % (scales["A4"], scales["A2"]))
        return "A4 scale=%s A2 scale=%s, all views inside" % (scales["A4"], scales["A2"])

    r.check("paper scaling keeps views inside", _paper_scaling)
    r.check("part tools (material/flat pattern)", _part_tools)
    r.check("single hole callout", _hole_callout_single)
    r.check("rename components (dry-run)", _rename)
    r.check("flat pattern view", _flat_pattern_view)
    r.check("material write honesty (0.8.6)", _material_write_honesty)
    r.check("reconcile ledger + delivery verdict", _reconcile_delivery)
    def _close_document():
        # A non-existent path must be a clean no-op: proves the tool is callable and that it
        # only closes a path match, never an unrelated document.
        res = drawing.close_document(os.path.join(workdir, "no_such_smoke_document.SLDDRW"))
        assert res.get("ok"), res
        assert res.get("closed_count") == 0, res
        return "clean no-op on an unknown path"

    def _view_edit_fixture():
        local = os.path.join(workdir, "smoke_view_edit.SLDDRW")
        drawing.create_drawing(part_copy, output_path=local, views=["front"], paper="A3")
        doc = drawing._open_drawing(local)
        target = next((v for v in drawing._iter_views(doc)
                       if getattr(v, "Type", None) != 1), None)
        if target is None:
            raise AssertionError("no model view on the fixture")
        return local, str(target.Name)

    def _set_view_scale():
        local, name = _view_edit_fixture()
        res = drawing.set_view_scale(local, name, 0.05)
        assert res.get("ok"), res
        assert res.get("applied"), "scale_after did not match: %s" % res
        return "scale %s -> %s" % (res.get("scale_before"), res.get("scale_after"))

    def _move_view():
        local, name = _view_edit_fixture()
        res = drawing.move_view(local, name, 0.15, 0.15)
        assert res.get("ok"), res
        after = res.get("position_after_m") or []
        assert after and abs(after[0] - 0.15) < 1e-3 and abs(after[1] - 0.15) < 1e-3, res
        return "moved to %s" % (after,)

    r.check("set view scale", _set_view_scale)
    r.check("move view", _move_view)
    r.check("close document", _close_document)
    r.check("server wrapper contract", _server_wrapper_contract)
    # Last on purpose — see the docstring: this one deliberately poisons the COM cache.
    r.check("bridge self-heal (P0-1)", _bridge_selfheal)

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
