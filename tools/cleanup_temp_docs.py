"""Close drawings THIS TOOLKIT leaked, and never a document the user is working on.

Why this exists: on 2026-11-07 I cleaned up leftover test documents with this predicate —

    if (p and tempdir in p.lower()) or p == '':
        sw.CloseDoc(title)

`p == ''` is wrong. An empty path is not only a leaked blank drawing; **a document the
user just created and has not saved yet also has an empty path.** That run closed 13
documents and I could not tell whether any of them were the user's. This file exists so
that mistake cannot be repeated by hand.

Design rules, all of them learned the hard way:

  1. DRY RUN BY DEFAULT. You must pass --execute, and you see the list first.
  2. Three buckets, and the third is untouchable:
       safe     path is inside the system temp directory -> this toolkit made it
       likely   empty path AND the title matches a drawing our tools create
                (工程图<N> - 图纸<M>, AM_*, smoke_*, m_*, d_*)
       unknown  empty path AND anything else -> LISTED, NEVER CLOSED.
                That is exactly the bucket I closed by accident.
  3. `--only <pattern>` narrows to matching titles, for when you want to be surgical.
  4. Nothing is closed unless it is in `safe` or `likely`. There is no flag that widens
     this to `unknown`; if you truly need to close one, close it in SolidWorks.

Usage:
    python tools/cleanup_temp_docs.py                 # dry run, shows all three buckets
    python tools/cleanup_temp_docs.py --execute       # close safe + likely only
    python tools/cleanup_temp_docs.py --execute --only smoke_   # narrower still
"""

import argparse
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src"))

# Titles produced by this toolkit's own test and pipeline runs. Kept deliberately narrow:
# widening this list is how the `unknown` bucket would slowly become closable.
_LIKELY_TITLE = re.compile(
    r"^(工程图\d+\s*-\s*图纸\d+"      # blank .SLDDRW created by create_drawing
    r"|AM_[\w.-]+"                    # pipeline prefix for copied fixtures
    r"|smoke_[\w.-]+"                 # smoke fixtures
    r"|m_[0-9a-f]{6}[\w.-]*"          # ad-hoc probe parts (uuid suffix)
    r"|d_[0-9a-f]{6}[\w.-]*)"         # ad-hoc probe drawings
)


def classify(path, title, temp_root):
    """('safe' | 'likely' | 'unknown', reason) — never guesses in the caller's favour."""
    p = (path or "").strip()
    t = (title or "").strip()
    if p:
        try:
            norm = os.path.normcase(os.path.abspath(p))
            root = os.path.normcase(os.path.abspath(temp_root))
            if norm.startswith(root + os.sep) or norm == root:
                return "safe", "inside the temp directory"
        except Exception:
            pass
        return "unknown", "has a path outside the temp directory — possibly the user's"
    # empty path from here on
    if _LIKELY_TITLE.match(t):
        return "likely", "empty path but the title matches a drawing this toolkit creates"
    return "unknown", ("empty path and an unrecognised title — this is the bucket I "
                       "closed by accident on 2026-11-07; it is never touched here")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="Actually close the safe/likely documents. Without this it is a "
                         "dry run.")
    ap.add_argument("--only", default=None,
                    help="Only consider documents whose title contains this substring.")
    args = ap.parse_args()

    from solidworks_assetmate_mechdog import sw_session

    try:
        app = sw_session.get_sw()
    except Exception as exc:                                        # noqa: BLE001
        print("cannot reach SolidWorks: %s" % exc)
        return 2

    temp_root = tempfile.gettempdir()
    docs = list(getattr(app, "GetDocuments", None) or ())
    if not docs:
        print("no documents open")
        return 0

    buckets = {"safe": [], "likely": [], "unknown": []}
    for item in docs:
        try:
            title = str(item.GetTitle)
            path = str(item.GetPathName or "")
        except Exception:
            continue
        if args.only and args.only not in title:
            continue
        kind, why = classify(path, title, temp_root)
        buckets[kind].append((title, path, why))

    print("temp root : %s" % temp_root)
    print("documents : %d open, %d matched\n" % (len(docs), sum(len(v) for v in buckets.values())))
    for kind in ("safe", "likely", "unknown"):
        rows = buckets[kind]
        print("%-8s %d" % (kind.upper(), len(rows)))
        for title, path, why in rows[:40]:
            print("   %-40s %s" % (title[:40], why))
            if path:
                print("       %s" % path)
        if len(rows) > 40:
            print("   ... %d more" % (len(rows) - 40))
        print()

    closable = buckets["safe"] + buckets["likely"]
    if not closable:
        print("nothing to close. %d document(s) are in UNKNOWN and stay open."
              % len(buckets["unknown"]))
        return 0

    if not args.execute:
        print("DRY RUN — %d document(s) would be closed. Re-run with --execute."
              % len(closable))
        print("The %d UNKNOWN document(s) are never closed by this script."
              % len(buckets["unknown"]))
        return 0

    closed, failed, still_open = [], [], []
    for title, path, _why in closable:
        try:
            app.CloseDoc(title)
            closed.append(title)
        except Exception as exc:                                    # noqa: BLE001
            failed.append((title, str(exc)[:80]))

    # `CloseDoc` returns without raising even when nothing was closed — measured
    # 2026-11-07 on a title containing `:`, which Windows cannot store as a filename, so
    # the document could never be matched. Re-enumerate; do not trust the call.
    try:
        remaining = {str(d.GetTitle) for d in (getattr(app, "GetDocuments", None) or ())}
        still_open = [t for t in closed if t in remaining]
    except Exception:
        still_open = []

    print("closed %d, failed %d, STILL OPEN after CloseDoc %d, left %d UNKNOWN untouched"
          % (len(closed), len(failed), len(still_open), len(buckets["unknown"])))
    for title in still_open:
        print("   STILL OPEN %-38s the title could not be matched (illegal character?)"
              % title[:38])
    for title, err in failed:
        print("   FAILED %-40s %s" % (title[:40], err))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
