"""Opt-in instrumentation hooks for the drawing pipeline (PR0).

Design constraints from the spec:

* **No behaviour change.** With no recorder running every function here is a no-op,
  so the existing 35-check smoke and 132 regressions must pass identically.
* **Runtime counts, not static string assertions.** The spec explicitly forbids
  "does the source contain X" as a performance gate.
* **Honest about scope.** `bump()` only counts the call sites that were instrumented.
  It is not a whole-application counter, and the receipt says so via
  `counts_scope`.

Usage:

    from . import perf_hooks
    perf_hooks.start("baseline", {"model_path": path})
    try:
        ...  # run the pipeline
    finally:
        result = perf_hooks.stop()
        perf_hooks.append("perf.jsonl", result)
"""
from __future__ import annotations

import functools
import os
import time
from contextlib import contextmanager

from .perf import PerfRecorder

_REC = None

#: Which call sites feed `counts`. Stated in every receipt so a reader never assumes
#: these are global SolidWorks call counts.
COUNTS_SCOPE = (
    "instrumented call sites only: create_drawing, auto_dimension, _save_drawing, "
    "drawing_audit — not a global hook on IModelDoc2"
)


def start(variant, metadata=None):
    """Begin a recording run. Returns the recorder so a caller can attach metadata."""
    global _REC
    _REC = PerfRecorder(variant, metadata)
    return _REC


def running():
    return _REC is not None


@contextmanager
def span(name):
    """Time a block. No-op when not recording."""
    if _REC is None:
        yield
        return
    with _REC.span(name):
        yield


def timed(name):
    """Decorator: time one call and count it. Zero overhead when not recording.

    Preferred over editing function bodies — a decorator is one line at the definition
    and cannot change semantics, which is the whole point of PR0.
    """
    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            if _REC is None:
                return fn(*args, **kwargs)
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                elapsed = (time.perf_counter() - start) * 1000.0
                _REC.timings_ms[name] = round(
                    _REC.timings_ms.get(name, 0.0) + elapsed, 3)
                _REC.count(name)
        return _wrapper
    return _decorator


def mark(name, started_at):
    """Record the elapsed time since the given perf_counter() value.

    For blocks that cannot be wrapped in a decorator because they are inline in a long
    function - InsertModelAnnotations3 + its required rebuild/redraw, for example.
    """
    if _REC is None:
        return
    elapsed = (time.perf_counter() - started_at) * 1000.0
    _REC.timings_ms[name] = round(_REC.timings_ms.get(name, 0.0) + elapsed, 3)
    _REC.count(name)


def bump(name, amount=1):
    """Increment a counter. No-op when not recording."""
    if _REC is not None:
        _REC.count(name, amount)


#: Metadata keys the spec wants on every record but that only the caller knows about
#: (the pipeline does not measure its own output). Merged into the receipt verbatim.
SPEC_META = ("sw_revision", "view_count", "dimension_count", "model_path", "model_class")


def note_meta(**kw):
    """Attach spec-required metadata discovered outside the timed region.

    Called by the runner after the pipeline returns, because view_count and dimension_count
    are OUTPUTS of the run, not inputs. No-op when not recording.
    """
    if _REC is None:
        return
    for key, value in kw.items():
        if value is not None:
            _REC.metadata[key] = value   # PerfRecorder calls it metadata, not meta


def fail(code, detail=None):
    if _REC is not None:
        _REC.fail(code, detail)


#: Field names the spec (section 9.1) requires in every JSONL record. Anything in here that
#: the pipeline does not measure is emitted as None, NOT 0 - a zero would read as "this took
#: no time", which is a claim we have not made.
SPEC_TIMINGS = (
    "model_open", "drawing_create", "view_create", "geometry_sync", "dimension_insert",
    "dimension_dedupe", "dimension_layout", "annotations", "bom", "save", "audit",
    "export", "total",
)
SPEC_COUNTS = ("force_rebuild", "graphics_redraw", "save", "layout_pass")

#: Our internal name -> the spec's name. Kept as an explicit table so a future rename cannot
#: silently drop a field.
_TIMING_ALIASES = {
    "drawing_create": "drawing_create",
    "dimension_insert": "dim_insert",
    "dimension_dedupe": "dim_dedupe",
    "dimension_layout": "dim_outside",
    "dimension_total": None,          # not a spec field; kept under its own name below
    "annotations": "annotations",
    "save": "save",
    "audit": "audit",
}
_COUNT_ALIASES = {
    "force_rebuild": "rebuild_calls",
    "graphics_redraw": "redraw_calls",
    "save": "save",
}


def conform_to_spec(result):
    """Reshape a receipt so it carries the section 9.1 field names.

    The original keys stay in place. That is deliberate: the JSONL files already on disk use
    them, and rewriting history to fit a newer key table would make yesterday's numbers
    unreadable. The spec fields are added alongside.

    Unmeasured fields are None. `total` falls back to the summed timings when the caller has
    not supplied a wall-clock figure, and says so via `total_source`.
    """
    if not result:
        return result

    timings = result.get("timings_ms") or {}
    counts = result.get("counts") or {}

    spec_t = {}
    for spec_name in SPEC_TIMINGS:
        internal = _TIMING_ALIASES.get(spec_name)
        spec_t[spec_name] = timings.get(internal) if internal else timings.get(spec_name)
    spec_t["dimension_total"] = timings.get("dimension_total")

    if spec_t.get("total") is None:
        wall = result.get("wall_ms")
        if wall is not None:
            spec_t["total"] = round(float(wall), 3)
            result["total_source"] = "wall_ms"
        else:
            spec_t["total"] = round(sum(v for k, v in timings.items()
                                        if isinstance(v, (int, float))), 3)
            result["total_source"] = "sum_of_stages (no wall clock supplied)"

    spec_c = {}
    for spec_name in SPEC_COUNTS:
        internal = _COUNT_ALIASES.get(spec_name)
        spec_c[spec_name] = counts.get(internal) if internal else counts.get(spec_name)
    # `layout_pass` has no instrumented source yet. Counted from the layout stage only when
    # auto_dimension reports place_outside; None otherwise rather than a made-up number.
    if spec_c.get("layout_pass") is None and counts.get("dim_outside"):
        spec_c["layout_pass"] = counts.get("dim_outside")

    result["timings_ms"] = timings
    result["counts"] = counts
    result["spec"] = {
        "run_id": result.get("run_id"),
        "variant": result.get("variant"),
        "sw_revision": result.get("sw_revision"),
        "model_path": result.get("model_path"),
        "model_class": result.get("model_class"),
        "view_count": result.get("view_count"),
        "dimension_count": result.get("dimension_count"),
        "success": result.get("success"),
        "correctness_pass": result.get("correctness_pass"),
        "timings_ms": spec_t,
        "counts": spec_c,
    }
    result["spec"]["unmeasured"] = sorted(
        [k for k in SPEC_TIMINGS if spec_t.get(k) is None]
        + ["counts." + k for k in SPEC_COUNTS if spec_c.get(k) is None])
    return result


def stop(success=True, correctness_pass=False):
    """Finish the run and return the receipt dict (None if never started)."""
    global _REC
    if _REC is None:
        return None
    result = _REC.finish(success=success, correctness_pass=correctness_pass)
    result["counts_scope"] = COUNTS_SCOPE
    result = conform_to_spec(result)
    _REC = None
    return result


def append(path, result):
    """Append one JSONL line. No-op for a None result."""
    if result:
        PerfRecorder.append_jsonl(path, result)


def default_log_path():
    """Where the baseline JSONL goes. Overridable so CI does not write into the repo."""
    return os.environ.get("ASSETMATE_PERF_LOG", "logs/perf.jsonl")
