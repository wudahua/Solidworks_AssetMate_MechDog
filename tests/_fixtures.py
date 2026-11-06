"""One place that decides where test fixtures live, and what to do when they are gone.

WHY THIS FILE EXISTS (2026-11-05, fourth report)

`tests/test_a_group.py`, `tests/test_rollback.py` and `tests/test_save_paths.py` each
hardcoded

    ROOT / "测试用例" / "旋转轴-工程图" / "旋转轴.SLDPRT"

while `.gitignore` excludes `测试用例/` as a whole tree:

    # Whole tree on purpose: nothing under 测试用例/ belongs in the package.
    测试用例/

So that part is never in a release tarball. Measured on a clean install: **0 of 7 cases
ran** — four raised `FileNotFoundError` before their first assertion, one module crashed
during import, and none of them said why.

The same mistake had already been reported once (v0.8.24, smoke pointing at a gitignored
fixture) and fixed with `FixtureMissing` + an actionable message. Three new files then
reintroduced it. Copy-pasting the pattern three more times would only delay the next one,
so the resolution lives here and all three import it.

CONTRACT
  resolve_part(argv) -> path            raises FixtureMissing with the full guidance
  add_part_argument(parser, help)       so each script can offer --part

A missing fixture is NOT a test failure: the code under test was never reached. The three
scripts exit 2 (the conventional "skipped for environmental reasons") and print how to
point them at a part.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_PART = os.path.join(ROOT, "测试用例", "旋转轴-工程图", "旋转轴.SLDPRT")

_GUIDANCE = """\
夹具缺失：{path}

这三个测试需要一个带键槽的零件，默认取 测试用例/旋转轴-工程图/旋转轴.SLDPRT，
而 测试用例/ 在 .gitignore 里被整树排除 —— 从发布包安装的机器上不会有它。

三条出路，任选其一：
  1. 指定自己的零件：  python {script} --part "D:\\path\\to\\some_part.SLDPRT"
  2. 把该零件放回 {expected_dir}
  3. 只跑不依赖夹具的部分：python tests/check_all.py   （离线断言，不需要 SolidWorks）

零件要求：任意 SLDPRT 均可（测试只关心"保存/覆盖/回滚/不丢内容"，
不关心具体几何）。"""


class FixtureMissing(Exception):
    """A test fixture is absent. Not a failure of the code under test."""


def resolve_part(argv=None, script=None):
    """Return an existing .SLDPRT, or raise FixtureMissing with the guidance text."""
    argv = sys.argv[1:] if argv is None else argv
    script = script or os.path.basename(sys.argv[0] or "test")

    chosen = DEFAULT_PART
    for i, token in enumerate(argv):
        if token == "--part" and i + 1 < len(argv):
            chosen = argv[i + 1]
            break
        if token.startswith("--part="):
            chosen = token.split("=", 1)[1]
            break

    if os.path.exists(chosen):
        return chosen
    raise FixtureMissing(_GUIDANCE.format(
        path=chosen, script=script, expected_dir=os.path.dirname(DEFAULT_PART)))


def add_part_argument(parser, help_text=None):
    """Register --part on an argparse parser with the same wording everywhere."""
    parser.add_argument(
        "--part", metavar="SLDPRT", default=None,
        help=help_text or (
            "零件路径（默认 测试用例/旋转轴-工程图/旋转轴.SLDPRT，"
            "但该目录被 gitignore，发布包里没有）"))


def exit_missing(exc):
    """Print the guidance and exit 2 — 'skipped', not 'failed'."""
    sys.stderr.write("\n%s\n" % exc)
    raise SystemExit(2)
