"""D group: a multi-model performance matrix (the spec asks for 10-20 representative models).

Run:  python tools/perf_matrix.py [--runs 1] [--no-close]

Why a separate script from perf_baseline.py: that one owns the A/B machinery for a SINGLE
model (ledger cache on/off, interleaved). This one answers a different question — "what does
a run cost across the kinds of part we actually meet" — and deliberately does NOT compare
variants. Mixing the two would invite reading a model-to-model difference as a regression.

Model choice is by CONSTRUCTION TYPE, not by size alone: sheet metal, turned parts, a casting,
a thin shell, a rebuilt part, a weldment rail, and both ends of the sheet-metal size range.
Size is a poor proxy — the largest file here is a sheet-metal panel with a few hundred
features, while the casting is smaller but far denser.

Honest limits, stated up front:
  * ONE run per model by default. This machine varies ~30% between runs of the same code
    (measured 82 -> 162 s on 旋转轴 in one session), so a single sample per model cannot
    support a claim like "part X is slower than part Y". It CAN support "here is the range
    and where the time goes".
  * A failed model is recorded as failed, not skipped. A matrix that quietly drops the parts
    it cannot draw would look like good news.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from solidworks_assetmate_mechdog import perf_hooks                # noqa: E402
from solidworks_assetmate_mechdog.sw_session import get_sw        # noqa: E402
from solidworks_assetmate_mechdog.tools import drawing, pipeline  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

#: (label, relative path, why this one is in the matrix)
MODELS = [
    ("sheetmetal_large", "测试用例/工程图测试用例/GGD配电柜/KSHE-016.015.SLDPRT",
     "largest sheet-metal panel in the corpus"),
    ("sheetmetal_simple", "测试用例/工程图测试用例/GGD配电柜/立柱.SLDPRT",
     "small sheet-metal post — the fast end of the range"),
    ("sheetmetal_door", "测试用例/工程图测试用例/GGD配电柜/内门/KSHE-016.013.SLDPRT",
     "door panel: many cutouts, several bends"),
    ("track_complex", "测试用例/原履带/履带.SLDPRT",
     "complex track — the heaviest geometry in the corpus"),
    ("track_rebuilt", "测试用例/履带-rebuild/P05_karetku.SLDPRT",
     "rebuilt track part — different feature history from source"),
    ("turned_shaft", "测试用例/旋转轴-工程图/旋转轴.SLDPRT",
     "turned shaft — the model every earlier measurement used"),
    ("turned_flyshaft", "测试用例/飞达动力轴/飞达动力轴.SLDPRT",
     "turned shaft with the 2 truly-missing dimensions"),
    ("casting_cylhead", "测试用例/气缸盖/气缸盖.SLDPRT",
     "casting — dense face count, no sheet-metal features"),
    ("thin_shell", "测试用例/简易护卫舰/baquebo (2).SLDPRT",
     "thin shell — geometry unlike any mechanical part here"),
    ("bracket_rail", "测试用例/监视器显示器固定支架-rebuild/P01_底座横梁.SLDPRT",
     "rebuilt bracket rail — weldment-style part"),
]


def doc_count():
    try:
        return len(list(get_sw().GetDocuments or ()))
    except Exception:
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1, help="runs per model")
    ap.add_argument("--paper", default="A3")
    ap.add_argument("--json", default="logs/perf_matrix.json", help="where to write the matrix")
    ap.add_argument("--cold", action="store_true",
                    help="disable the ledger and annotation caches. Both read their env "
                         "var at call time, so setting it here is enough.")
    args = ap.parse_args()

    if args.cold:
        os.environ["ASSETMATE_LEDGER_CACHE"] = "0"
        os.environ["ASSETMATE_ANNOT_CACHE"] = "0"
        print("cold path: ledger + annotation caches disabled")
    sw = get_sw()
    print("SolidWorks %s | docs %d | models %d | runs/model %d"
          % (sw.RevisionNumber, doc_count(), len(MODELS), args.runs))
    print("-" * 96)
    print("%-18s %7s %6s %5s %6s %8s  %s" %
          ("model", "wall_s", "dims", "views", "ok", "docs", "note"))

    rows = []
    work_root = tempfile.mkdtemp(prefix="matrix_")
    try:
        for label, rel, why in MODELS:
            src = os.path.join(ROOT, rel)
            if not os.path.exists(src):
                rows.append({"model": label, "ok": False, "error": "file_missing",
                             "why": why, "path": rel})
                print("%-18s %7s %6s %5s %6s %8s  %s" %
                      (label, "-", "-", "-", "MISS", "-", "file not found"))
                continue
            for run in range(args.runs):
                tag = uuid.uuid4().hex[:6]
                part = os.path.join(work_root, "m_%s_%d.SLDPRT" % (tag, run))
                drw = os.path.join(work_root, "d_%s_%d.SLDDRW" % (tag, run))
                shutil.copy2(src, part)
                perf_hooks.start("matrix", {"model_path": os.path.basename(rel),
                                            "model_class": "part", "paper": args.paper})
                wall = time.perf_counter()
                payload, error = {}, None
                try:
                    payload = pipeline.produce_drawing(part, output_path=drw,
                                                       paper=args.paper)
                except Exception as exc:                            # noqa: BLE001
                    error = "%s: %s" % (type(exc).__name__, str(exc)[:120])
                wall_ms = round((time.perf_counter() - wall) * 1000.0, 1)
                ok = bool(payload.get("ok")) and error is None
                # Spec-required metadata that only exists AFTER the run: view_count and
                # dimension_count are outputs, not inputs.
                perf_hooks.note_meta(
                    sw_revision=str(sw.RevisionNumber),
                    view_count=len(payload.get("views") or []),
                    dimension_count=(payload.get("dimension_gap") or {}).get("inserted"),
                    model_path=os.path.basename(rel),
                    model_class="part")
                rec = perf_hooks.stop(success=ok, correctness_pass=ok)
                rec["wall_ms"] = wall_ms
                rec["error"] = error
                rec["model_label"] = label
                rec["why"] = why
                perf_hooks.append("logs/perf_matrix.jsonl", rec)

                dims = (payload.get("dimension_gap") or {}).get("inserted")
                views = len(payload.get("views") or [])
                note = error or (payload.get("error") or "")
                rows.append({"model": label, "ok": ok, "wall_ms": wall_ms, "why": why,
                             "dims": dims, "views": views, "error": note or None,
                             "cold": bool(args.cold),
                             "timings_ms": rec.get("timings_ms"),
                             "counts": rec.get("counts")})
                print("%-18s %7.1f %6s %5s %6s %8s  %s" %
                      (label, wall_ms / 1000.0, dims, views,
                       "ok" if ok else "FAIL", doc_count(), note[:34]))

                try:
                    drawing.close_document(drw)
                    drawing.close_document(part)
                except Exception:
                    pass
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    good = [r for r in rows if r.get("ok")]
    print("-" * 96)
    print("ok %d / %d" % (len(good), len(rows)))
    # Per-model statistics, NOT pooled across models. The spec asks "每组记录至少 5 次",
    # and a "group" is one model: pooling ten different parts into one P95 answers a question
    # nobody asked, because the models differ by 12x in cost (52.9 s vs 661.5 s).
    by_model = {}
    for r in good:
        by_model.setdefault(r["model"], []).append(r["wall_ms"] / 1000.0)

    def p95(values):
        """Nearest-rank P95. Below n=20 this is the slowest sample and is labelled as such."""
        if not values:
            return float("nan")
        idx = max(0, min(len(values) - 1, int(round(0.95 * (len(values) - 1)))))
        return values[idx]

    print("per-model (spec: >=5 runs per group):")
    print("  %-18s %4s %8s %8s %8s %8s  %s" %
          ("model", "n", "median", "P95", "min", "max", "rebuilds/redraws"))
    thin = []
    for label in [m[0] for m in MODELS]:
        vals = sorted(by_model.get(label) or [])
        if not vals:
            continue
        first = next(r for r in good if r["model"] == label)
        counts = first.get("counts") or {}
        rc = counts.get("rebuild_calls")
        dc = counts.get("redraw_calls")
        print("  %-18s %4d %7.1fs %7.1fs %7.1fs %7.1fs  %s/%s" %
              (label, len(vals), statistics.median(vals), p95(vals), vals[0], vals[-1],
               rc, dc))
        if len(vals) < 5:
            thin.append("%s(n=%d)" % (label, len(vals)))
    if thin:
        print("  NOTE: below the spec's 5-run minimum: %s" % ", ".join(thin))
        print("        A single sample per model supports 'here is the range', "
              "not 'this model is faster'.")

    # Pooled figures kept only as a headline, explicitly labelled as not a per-group stat.
    all_walls = sorted(r["wall_ms"] / 1000.0 for r in good)
    print("all samples pooled (headline only, not a per-model percentile): "
          "n=%d median %.1fs min %.1fs max %.1fs"
          % (len(all_walls), statistics.median(all_walls), all_walls[0], all_walls[-1]))

    # Rebuild / redraw counts across all runs: the spec asks these be recorded, and they
    # explain part of the spread - a run that rebuilds 6 times costs more than one doing 2.
    ics = [r.get("counts") or {} for r in good]
    for key in ("rebuild_calls", "redraw_calls", "save", "audit"):
        vals = [c.get(key) for c in ics if c.get(key) is not None]
        if vals:
            print("  %-14s n=%d  median %s  range %s-%s"
                  % (key, len(vals), statistics.median(vals), min(vals), max(vals)))

    failed = [r["model"] for r in rows if not r.get("ok")]
    if failed:
        print("FAILED: %s" % ", ".join(failed))

    os.makedirs(os.path.dirname(os.path.join(ROOT, args.json)) or ".", exist_ok=True)
    with open(os.path.join(ROOT, args.json), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print("matrix written to %s" % args.json)
    return 0 if len(failed) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
