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


def _counts_from_regressions(output):
    """The numbers `regressions.py` computed from its own lists. No recounting here.

    Report 2026-11-14 (v0.8.327) §P1-1: this file used to count list entries with a string
    split AND a regex over docstrings, while `regressions.py` counted with a different
    regex — three numbers for one fact (5 / 6 / 15). The suite now prints one machine-
    readable line and this file only parses it, so there is exactly one place that can be
    wrong.
    """
    for line in (output or "").splitlines():
        if not line.startswith("counts:"):
            continue
        got = {}
        for token in line.split()[1:]:
            key, _, value = token.partition("=")
            try:
                got[key] = int(value)
            except ValueError:
                continue
        if {"total", "behavioural", "text_ast"} <= set(got):
            return got
    return {}


def _smoke_check_count():
    """How many `r.check(...)` calls `smoke.py` makes, without running it."""
    try:
        with open(os.path.join(HERE, "smoke.py"), encoding="utf-8") as fh:
            return fh.read().count("r.check(")
    except Exception:
        return 0


def main():
    failures = []
    outputs = {}
    for name, cmd in STEPS:
        print("=" * 78)
        print(">> %s" % name)
        print("=" * 78)
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        output = (result.stdout or "").strip()
        outputs[name] = output
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
    # 0.8.328 (report 2026-11-14 v0.8.327 §P1-1): ONE source of these numbers. The suite
    # prints `counts: total=… behavioural=… text_ast=…` from `len()` of its two lists; this
    # file parses that line. Earlier versions recounted independently (list-split vs
    # docstring regex) and printed 5 while the suite said 6 and the AST said 15.
    _counts = _counts_from_regressions(outputs.get("regressions"))
    if _counts:
        # 0.8.330 (report 2026-11-14 v0.8.329 §5.3): "behavioural" hides that one of them
        # exercises the smoke harness rather than the product. Name the split.
        # 0.8.345 (report 2026-11-15 v0.8.344 §P2): the suite now splits the subprocess
        # launch out of "call the package" — it executes package code, but out of process.
        print("  source-level : %d assertions (%d behavioural = %d package + %d subprocess "
              "+ %d harness%s; %d text/AST)"
              % (_counts["total"], _counts["behavioural"],
                 _counts.get("package", _counts["behavioural"]),
                 _counts.get("subprocess", 0), _counts.get("harness", 0),
                 (" + %d unknown" % _counts["unknown"]) if _counts.get("unknown") else "",
                 _counts["text_ast"]))
    else:
        print("  source-level : UNKNOWN — `regressions.py` printed no `counts:` line "
              "(fix that before trusting any coverage number)")
    # 0.8.305 (report 2026-11-12 §4): this number is a STRING COUNT, not a runnable
    # suite. Calling them "checks" made "64 on-machine checks" read as coverage when
    # nothing had executed — and on a standard install most of them cannot run at all.
    # 0.8.330 (report 2026-11-14 v0.8.329 §5.2): "0 executed here" reads like a footnote.
    # It is the single largest gap in this report, so it is printed as NOT RUN plus the exact
    # commands that would close it. There is no CI wiring in this repo — saying so is cheaper
    # than pretending the 69 checks ran.
    print("  on-machine   : NOT RUN — %d r.check() cases need local fixtures + live "
          "SolidWorks" % _smoke_check_count())
    print("                 at minimum run: python tests/smoke.py "
          "--only \"assembly drawing not an empty shell\"")
    print("                                  python tests/smoke.py --part "
          "<有驱动尺寸的零件> --only \"a tolerance render never passes on a bare number\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
