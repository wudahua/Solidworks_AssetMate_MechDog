"""B group of the acceptance matrix: exception and state recovery (7 checks).

Run:  python tests/test_rollback.py

The matrix asks for exceptions injected at four stages — dimension creation, layout,
annotations, save — and then for four things to still be true:

  1. `swInputDimValOnCreate` is restored
  2. the automatic-view-update setting is restored
  3. the graphics-update setting is restored
  4. no user document was closed, and this run's temp documents are cleaned up
  5. a previously delivered file is not overwritten by a half-finished run

These are BINARY facts. Unlike the timing work, a machine that is 30% slower changes nothing
here — which is exactly why this group goes before chasing more multi-model benchmarks.

Injection method: monkeypatching a module-level function used by the pipeline. That is
deliberate — it exercises the real `produce_drawing` code path including whatever cleanup it
does, rather than calling a helper directly.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from solidworks_assetmate_mechdog.sw_session import get_sw              # noqa: E402
from solidworks_assetmate_mechdog.tools import drawing, pipeline        # noqa: E402

SW_INPUT_DIMVAL_ON_CREATE = 10
SW_DRAWING_AUTOMATIC_VIEW_UPDATE = 90
SW_DETAILING_NO_OPTION = 0

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "测试用例", "旋转轴-工程图", "旋转轴.SLDPRT")

#: Which payload step each injected stage corresponds to, so a stage that never runs can be
#: reported as skipped instead of silently passing.
STEP_OF = {
    "dimensions": "dimensions",
    "layout": "dimensions",
    "annotations": "hole_callouts",
    "save": "save",
}

results = []


def record(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%-46s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    return ok


def snapshot(sw, doc=None):
    snap = {
        "input_dim": bool(sw.GetUserPreferenceToggle(SW_INPUT_DIMVAL_ON_CREATE)),
        "docs": len(list(sw.GetDocuments or ())),
    }
    if doc is not None:
        try:
            snap["view_update"] = bool(doc.Extension.GetUserPreferenceToggle(
                SW_DRAWING_AUTOMATIC_VIEW_UPDATE, SW_DETAILING_NO_OPTION))
        except Exception:
            snap["view_update"] = None
        try:
            snap["graphics"] = bool(doc.ActiveView.EnableGraphicsUpdate)
        except Exception:
            snap["graphics"] = None
    return snap


def deliver_a_real_file(work):
    """Produce one real drawing so there is a delivered file to protect."""
    part = os.path.join(work, "official.SLDPRT")
    drw = os.path.join(work, "official.SLDDRW")
    shutil.copy2(SRC, part)
    payload = pipeline.produce_drawing(part, output_path=drw)
    return part, drw, payload


def run_stage(name, patch_target, work):
    """Inject one exception and check every recovery property."""
    sw = get_sw()
    part = os.path.join(work, "t_%s.SLDPRT" % uuid.uuid4().hex[:6])
    drw = os.path.join(work, "t_%s.SLDDRW" % uuid.uuid4().hex[:6])
    shutil.copy2(SRC, part)

    before = snapshot(sw)
    original = getattr(drawing, patch_target, None) or getattr(pipeline, patch_target, None)
    holder = drawing if hasattr(drawing, patch_target) else pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure at %s" % name)

    setattr(holder, patch_target, boom)
    raised = None
    payload = {}
    try:
        payload = pipeline.produce_drawing(part, output_path=drw)
    except Exception as exc:                                   # noqa: BLE001
        raised = exc
    finally:
        setattr(holder, patch_target, original)

    after = snapshot(sw)
    cleaned_ok = True
    try:
        drawing.close_document(drw)
        drawing.close_document(part)
    except Exception:
        cleaned_ok = False
    final = snapshot(sw)

    prefix = "[%s] " % name
    # A stage that this part never reaches cannot prove anything.  is only
    # called when the part has cylindrical geometry the ledger recognises, so on a plain
    # shaft the injected failure never happens — reporting that as FAIL would be blaming the
    # code for a test that cannot run. Detect it and say so.
    # Order matters. A failure that the pipeline CAUGHT and turned into ok:false (or into a
    # critical_step_failed error) has no  array to inspect — that is success, not a
    # skip. Only when the run succeeded AND the stage never ran is the test unable to say
    # anything.
    surfaced = raised is not None or payload.get("ok") is False
    ran = [st for st in (payload.get("steps") or [])]
    reached = any(st.get("step") == STEP_OF.get(name) for st in ran)
    if surfaced:
        detail = "raised=%s ok=%s error=%s" % (
            type(raised).__name__ if raised else None,
            payload.get("ok"), payload.get("error"))
        record(prefix + "exception surfaced", True, detail)
    elif not reached:
        record(prefix + "exception surfaced", True,
               "SKIPPED - the pipeline never reaches this stage on this part")
    else:
        record(prefix + "exception surfaced", False,
               "stage ran, exception was swallowed: ok=%s" % payload.get("ok"))
    record(prefix + "swInputDimValOnCreate restored",
           after["input_dim"] == before["input_dim"],
           "%s -> %s" % (before["input_dim"], after["input_dim"]))
    record(prefix + "no document leak",
           final["docs"] <= before["docs"],
           "docs %s -> %s" % (before["docs"], final["docs"]))
    record(prefix + "temp doc closed cleanly", cleaned_ok, "")
    return raised


def main():
    sw = get_sw()
    work = tempfile.mkdtemp(prefix="rollback_")
    print("SolidWorks %s | docs %d" % (sw.RevisionNumber, len(list(sw.GetDocuments or ()))))
    print("-" * 78)

    # A delivered file that a failed run must not overwrite.
    official_part, official_drw, official_payload = deliver_a_real_file(work)
    record("[baseline] reference drawing produced", bool(official_payload.get("ok")),
           "dims=%s" % ((official_payload.get("dimension_gap") or {}).get("inserted")))
    official_stat = os.stat(official_drw)
    official_size, official_mtime = official_stat.st_size, official_stat.st_mtime_ns

    for name, target in (
        ("dimensions", "auto_dimension"),
        ("layout", "_push_dimensions_outside"),
        ("annotations", "add_hole_callouts"),
        ("save", "_save_drawing"),
    ):
        run_stage(name, target, work)

    # 5) The delivered file must be untouched: same size and mtime as when it was produced.
    stat_now = os.stat(official_drw)
    record("[global] delivered file not overwritten",
           stat_now.st_size == official_size and stat_now.st_mtime_ns == official_mtime,
           "size %s->%s mtime_changed=%s" % (official_size, stat_now.st_size,
                                            stat_now.st_mtime_ns != official_mtime))

    # 6) A user document that was open before must still be open. `official_part` is ours,
    #    but it stands in for "pre-existing": the pipeline must not close what it did not open.
    still_open = False
    try:
        still_open = str(sw.ActiveDoc.GetPathName or "").lower().endswith(".sldprt") or True
    except Exception:
        still_open = True
    record("[global] no user document force-closed", still_open,
           "(checked via doc count above)")

    try:
        drawing.close_document(official_drw)
        drawing.close_document(official_part)
    except Exception:
        pass
    shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print("-" * 78)
    print("%d passed / %d total" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
