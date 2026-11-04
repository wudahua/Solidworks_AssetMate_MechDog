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
    args = ap.parse_args()

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
                             "timings_ms": rec.get("timings_ms")})
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
    if good:
        walls = sorted(r["wall_ms"] / 1000.0 for r in good)
        print("wall: min %.1f s  median %.1f s  max %.1f s" %
              (walls[0], statistics.median(walls), walls[-1]))
        # Bucketed so a reader can see whether cost tracks construction type.
        for bucket in ("sheetmetal", "track", "turned", "casting", "thin", "bracket"):
            sub = [r["wall_ms"] / 1000.0 for r in good if bucket in r["model"]]
            if sub:
                print("  %-11s n=%d  median %.1f s  range %.1f-%.1f" %
                      (bucket, len(sub), statistics.median(sub), min(sub), max(sub)))
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
