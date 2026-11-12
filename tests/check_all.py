"""One command that runs every check that does not need SolidWorks.

    python tests/check_all.py

Three layers, cheapest first:

    1. static_check.py   — structure: imported names, README sync, duplicate defs, property
                           traps, stale roadmap, wrapper signatures
    2. regressions.py    — one assertion per shipped bug; the exact count is printed
                          below rather than written here, because a hard-coded
                          number drifts on every release (measured this session:/n                          134 -> 135 -> 136 -> 166 -> 170, each already stale)
    3. compileall        — syntax (fails fast if a file is broken at all)

The behavioural layer (tests/smoke.py) needs a live SolidWorks and is NOT run here — it
takes ~6 minutes and depends on local-only model files.

Exit code 0 = every check passed.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

STEPS = [
    ("syntax", [sys.executable, "-m", "compileall", "-q", os.path.join(ROOT, "src"),
                os.path.join(ROOT, "tests")]),
    ("structure", [sys.executable, os.path.join(HERE, "static_check.py")]),
    ("regressions", [sys.executable, os.path.join(HERE, "regressions.py")]),
]


def _regression_count():
    """How many entries `regressions.py` registers, without importing it."""
    try:
        with open(os.path.join(HERE, "regressions.py"), encoding="utf-8") as fh:
            text = fh.read()
        head = text.split("STATIC_REGRESSIONS = [", 1)[1]
        body = head.split("\n]", 1)[0]
        return body.count('\n    ("')
    except Exception:
        return 0


def _smoke_check_count():
    """How many `r.check(...)` calls `smoke.py` makes, without running it."""
    try:
        with open(os.path.join(HERE, "smoke.py"), encoding="utf-8") as fh:
            return fh.read().count("r.check(")
    except Exception:
        return 0


def main():
    failures = []
    for name, cmd in STEPS:
        print("=" * 78)
        print(">> %s" % name)
        print("=" * 78)
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        output = (result.stdout or "").strip()
        if output:
            print(output)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if stderr:
                print(stderr)
            failures.append(name)
        print()

    print("=" * 78)
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    # Two very different things were being reported as one. Report 2026-11-09 (core
    # problem C): every entry in `regressions.py` inspects source text; none of them drives
    # SolidWorks. Printing both counts side by side stops "all offline checks passed" from
    # implying the drawing itself was verified.
    print("all offline checks passed")
    print("  source-level : %d assertions (text/AST only)"
          % _regression_count())
    # 0.8.305 (report 2026-11-12 §4): this number is a STRING COUNT, not a runnable
    # suite. Calling them "checks" made "64 on-machine checks" read as coverage when
    # nothing had executed — and on a standard install most of them cannot run at all.
    print("  on-machine   : %d r.check() calls counted, 0 executed here "
          "(needs local fixtures + live SolidWorks) — `python tests/smoke.py`"
          % _smoke_check_count())
    return 0


if __name__ == "__main__":
    sys.exit(main())
