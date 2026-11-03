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


def fail(code, detail=None):
    if _REC is not None:
        _REC.fail(code, detail)


def stop(success=True, correctness_pass=False):
    """Finish the run and return the receipt dict (None if never started)."""
    global _REC
    if _REC is None:
        return None
    result = _REC.finish(success=success, correctness_pass=correctness_pass)
    result["counts_scope"] = COUNTS_SCOPE
    _REC = None
    return result


def append(path, result):
    """Append one JSONL line. No-op for a None result."""
    if result:
        PerfRecorder.append_jsonl(path, result)


def default_log_path():
    """Where the baseline JSONL goes. Overridable so CI does not write into the repo."""
    return os.environ.get("ASSETMATE_PERF_LOG", "logs/perf.jsonl")
