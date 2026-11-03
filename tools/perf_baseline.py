"""PR0 baseline runner — measures the current pipeline, changes nothing.

Usage:
    python tools/perf_baseline.py --model 测试用例/旋转轴-工程图/旋转轴.SLDPRT --runs 5
    python tools/perf_baseline.py --model X.SLDPRT --variant save_batch --runs 10

Writes one JSONL record per run to `logs/perf.jsonl` (override with ASSETMATE_PERF_LOG)
and prints median / P95 per timed phase. P95 is computed WITHOUT dropping slow samples,
per the spec: "P95 不排除慢样本".

Correctness gate: a run whose `produce_drawing` returned ok=False is recorded with
`correctness_pass=False` and is excluded from the timing percentiles — a failed run that
happened to be fast is not a performance result.
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from solidworks_assetmate_mechdog import perf_hooks           # noqa: E402
from solidworks_assetmate_mechdog.sw_session import get_sw     # noqa: E402
from solidworks_assetmate_mechdog.tools import drawing, pipeline  # noqa: E402


def doc_count():
    try:
        return len(list(get_sw().GetDocuments or ()))
    except Exception:
        return -1


def p95(values):
    """Nearest-rank P95, no outlier removal."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--paper", default="A3")
    ap.add_argument("--keep", action="store_true", help="keep the temp drawings")
    ap.add_argument("--interleave", action="store_true",
                    help="alternate cache ON/OFF every run (A,B,A,B,...) instead of one "
                         "variant per invocation. Required for a defensible A/B: within-"
                         "session drift then hits both arms equally. With this flag "
                         "--runs N means N pairs.")
    args = ap.parse_args()

    model = os.path.abspath(args.model)
    if not os.path.exists(model):
        raise SystemExit("model not found: %s" % model)

    log_path = perf_hooks.default_log_path()
    work = tempfile.mkdtemp(prefix="assetmate_perf_")
    rows = []
    before_docs = doc_count()
    print("model   : %s" % model)
    print("variant : %s   runs: %d" % (args.variant, args.runs))
    print("log     : %s" % log_path)
    print("-" * 78)

    # A/B plan. Without --interleave, every run uses the same variant (whatever the caller
    # exported) — that is what produced the "B leaked 23 s into the un-instrumented
    # remainder" reading, which may just have been drift between blocks.
    if args.interleave:
        plan = [("A_no_cache", "0"), ("B_cache", "1")] * args.runs
    else:
        plan = [(args.variant, None)] * args.runs

    try:
        for index, (variant, cache_env) in enumerate(plan):
            if cache_env is not None:
                os.environ["ASSETMATE_LEDGER_CACHE"] = cache_env
            tag = uuid.uuid4().hex[:6]
            part = os.path.join(work, "p_%s_%d.SLDPRT" % (tag, index))
            out = os.path.join(work, "d_%s_%d.SLDDRW" % (tag, index))
            shutil.copy2(model, part)

            perf_hooks.start(variant, {
                "model_path": os.path.basename(model),
                "model_class": "part" if model.lower().endswith(".sldprt") else "assembly",
                "paper": args.paper,
                "run_index": index,
                "sw_revision": str(getattr(get_sw(), "RevisionNumber", "") or ""),
                "docs_before": before_docs,
            })
            wall = time.perf_counter()
            payload = {}
            error = None
            try:
                payload = pipeline.produce_drawing(part, output_path=out, paper=args.paper)
            except Exception as exc:                        # noqa: BLE001
                error = "%s: %s" % (type(exc).__name__, exc)
                perf_hooks.fail("exception", error)
            wall_ms = round((time.perf_counter() - wall) * 1000.0, 3)

            ok = bool(payload.get("ok"))
            result = perf_hooks.stop(success=ok and error is None,
                                     correctness_pass=ok and error is None)
            result["wall_ms"] = wall_ms
            result["view_count"] = len(payload.get("views") or [])
            result["dimension_count"] = (payload.get("dimension_gap") or {}).get("inserted")
            if error:
                result["error"] = error

            # Close BEFORE sampling the document count. Reading it first reported the
            # in-flight state (part + drawing + the previous run's leftovers) and made a
            # clean run look like it leaked — the acceptance matrix cares about the count
            # after the run, not during it.
            try:
                drawing.close_document(out)
                drawing.close_document(part)
            except Exception:
                pass
            result["docs_after"] = doc_count()
            perf_hooks.append(log_path, result)
            rows.append(result)

            print("run %-2d %-12s %7.1f ms  ok=%-5s dims=%-4s docs_after=%s"
                  % (index + 1, variant, wall_ms, ok,
                     result.get("dimension_count"), result.get("docs_after")))
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    good = [r for r in rows if r.get("correctness_pass")]
    print("-" * 78)
    print("correct runs: %d / %d" % (len(good), len(rows)))
    if not good:
        print("no correct sample — refusing to report timings")
        return 1

    groups = []
    for r in good:
        if r.get("variant") not in [g for g, _ in groups]:
            groups.append((r.get("variant"), []))
    for name, _ in groups:
        pass
    buckets = {name: [] for name, _ in groups}
    for r in good:
        buckets[r.get("variant")].append(r)

    for name, sample in buckets.items():
        print("\n== %s   n=%d" % (name, len(sample)))
        walls = [r["wall_ms"] for r in sample]
        print("%-22s %10s %10s %10s" % ("phase", "median", "p95", "calls"))
        phases = sorted({k for r in sample for k in (r.get("timings_ms") or {})})
        for phase in phases:
            values = [(r["timings_ms"] or {}).get(phase) for r in sample]
            values = [v for v in values if isinstance(v, (int, float))]
            if not values:
                continue
            calls = [((r.get("counts") or {}).get(phase)) for r in sample]
            calls = [c for c in calls if isinstance(c, int)]
            print("%-22s %10.1f %10.1f %10s"
                  % (phase, statistics.median(values), p95(values),
                     statistics.median(calls) if calls else "-"))
        measured = [((r.get("timings_ms") or {}).get(p) or 0) for r in sample
                    for p in (r.get("timings_ms") or {})]
        # The un-instrumented remainder is the number that matters more than any phase:
        # if it moves between arms, the comparison is confounded.
        remainders = []
        for r in sample:
            spent = sum((r.get("timings_ms") or {}).values())
            remainders.append(max(0.0, r["wall_ms"] - spent))
        print("%-22s %10.1f %10.1f" % ("WALL", statistics.median(walls), p95(walls)))
        print("%-22s %10.1f %10.1f" % ("UNINSTRUMENTED", statistics.median(remainders),
                                       p95(remainders)))

    print("\nrecords appended to %s" % log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
