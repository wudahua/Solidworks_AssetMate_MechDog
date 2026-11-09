"""Bump the version in both places, and fail loudly when nothing changed.

Why this exists (report 2026-11-09, core problem A)
--------------------------------------------------
Three releases were tagged without their version actually moving:

    v0.8.134-136  stuck at 0.8.133
    v0.8.264-265  stuck at 0.8.263
    v0.8.272      stuck at 0.8.271   <- and it shares a commit with v0.8.271

Each time the cause was the same: a shell `sed` with a hard-coded literal. When the file
already held a different version, the pattern matched nothing, `sed` exited 0, the commit
and tag were created, and the only symptom was a third party diffing the tarball.

`sed -i 's/0.8.271/0.8.272/'` on a file that says 0.8.263 is a no-op that looks like success.
This script cannot be a no-op: it reads the current version first, computes the next one,
writes, then **re-reads and asserts the file changed**.

Usage
-----
    python tools/bump_version.py            # 0.8.273 -> 0.8.274
    python tools/bump_version.py --to 0.9.0
    python tools/bump_version.py --check    # print state, change nothing; exit 1 if inconsistent

Exit codes: 0 ok, 1 inconsistent input, 2 write failed to take effect.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "solidworks_assetmate_mechdog" / "__init__.py"
META = ROOT / "src" / "solidworks_assetmate_mechdog" / "_build_meta.json"


def _read(path: Path, pattern: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text, re.M)
    if not match:
        raise SystemExit("no version line in %s (pattern %r)" % (path, pattern))
    return match.group(1)


def current_versions() -> tuple[str, str]:
    return (
        _read(PYPROJECT, r'^version\s*=\s*"([^"]+)"'),
        _read(INIT, r'__version__\s*=\s*"([^"]+)"'),
    )


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts[:3])


def _rewrite(path: Path, pattern: str, new_version: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, lambda m: m.group(1) + new_version + m.group(3),
                              text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit("refusing to write %s: pattern matched %d times" % (path, count))
    path.write_text(new_text, encoding="utf-8", newline="")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                             timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def check_only() -> int:
    declared, dunder = current_versions()
    print("pyproject : %s" % declared)
    print("__init__  : %s" % dunder)
    if declared != dunder:
        print("MISMATCH — run this script without --check to fix")
        return 1

    head = _git("rev-parse", "HEAD")
    prev_tag = _git("describe", "--tags", "--abbrev=0", "HEAD^") or _git(
        "describe", "--tags", "--abbrev=0", "HEAD")
    prev_commit = _git("rev-parse", prev_tag) if prev_tag else ""
    if head and prev_commit and head == prev_commit:
        print("HEAD (%s) is the SAME commit as %s — tagging again here would create a "
              "duplicate release, which is how v0.8.272 shipped." % (head[:12], prev_tag))
        return 1
    print("HEAD %s is ahead of %s — safe to tag" % (head[:12] or "?", prev_tag or "(no tag)"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", help="exact target version, e.g. 0.9.0")
    parser.add_argument("--check", action="store_true",
                        help="report state and exit; write nothing")
    parser.add_argument("--no-meta", action="store_true",
                        help="skip writing _build_meta.json")
    args = parser.parse_args()

    if args.check:
        return check_only()

    declared, dunder = current_versions()
    if declared != dunder:
        print("refusing to start: pyproject %s vs __init__ %s already disagree"
              % (declared, dunder), file=sys.stderr)
        return 1

    target = args.to or _bump_patch(declared)
    if target == declared:
        print("target equals current (%s) — nothing to do" % target, file=sys.stderr)
        return 1

    _rewrite(PYPROJECT, r'^(version\s*=\s*")([^"]+)(")', target)
    _rewrite(INIT, r'^(__version__\s*=\s*")([^"]+)(")', target)

    # The whole point: prove the files actually changed. A silent no-op is what shipped
    # three times.
    after_declared, after_dunder = current_versions()
    if after_declared != target or after_dunder != target:
        print("WRITE DID NOT TAKE EFFECT: pyproject=%s __init__=%s, expected %s"
              % (after_declared, after_dunder, target), file=sys.stderr)
        return 2

    if not args.no_meta:
        import json
        META.write_text(json.dumps({"tag": "v" + target, "version": target}, indent=2) + "\n",
                        encoding="utf-8", newline="")

    print("%s -> %s  (pyproject + __init__%s)"
          % (declared, target, "" if args.no_meta else " + _build_meta.json"))
    print("next: git add -A && git commit && git tag v%s && git push" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
