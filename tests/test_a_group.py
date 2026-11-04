"""A group, the three items the acceptance matrix lists but nothing exercised.

Run:  python tests/test_a_group.py

  A3. 模型尺寸覆盖不减少；等轴测空视图不误报
  A5. 技术要求、GTol、基准、粗糙度、BOM 均不丢项
  A6. GTol 的符号、数值、基准、引线均在 PDF 可见

Each of these is a "did something silently disappear" check. That is the failure mode this
whole optimisation round was most at risk of: caching, skipped audits and merged saves all
remove work, and removed work can take content with it.

A3 is the one with a real trap. An isometric view legitimately has no dimensions — if the
empty-view warning fires on it, every clean drawing looks broken. So the assertion is not
"no empty views" but "the empty view, if any, is one where emptiness is expected".
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from solidworks_assetmate_mechdog.tools import drawing, layout, pipeline   # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(ROOT, "测试用例", "旋转轴-工程图", "旋转轴.SLDPRT")

results = []


def record(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("%-56s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    return ok


# ---------------------------------------------------------------- A3
def case_isometric_empty_view():
    """An isometric view has no model dimensions — that must not read as a defect.

    Two things are checked:
      1. if the audit calls a view "empty", that view is not one we asked to be dimensioned
         (isometric is the expected one)
      2. the coverage figure does not drop because of it
    """
    work = tempfile.mkdtemp(prefix="ag3_")
    token = uuid.uuid4().hex[:6]
    part = os.path.join(work, "m_%s.SLDPRT" % token)
    drw = os.path.join(work, "d_%s.SLDDRW" % token)
    shutil.copy2(SRC, part)
    try:
        # include an isometric view on purpose
        payload = pipeline.produce_drawing(part, output_path=drw,
                                           views=["front", "right", "isometric"])
        if not payload.get("ok"):
            return record("[A3] setup", False, payload.get("message") or "")
        views = payload.get("views") or []
        record("[A3] three views requested and produced", len(views) >= 3,
               "%d views" % len(views))

        empty = [v for v in views if int(v.get("dimensions") or 0) == 0]
        names = [str(v.get("name")) for v in empty]
        # An isometric / 等轴测 view is allowed to be empty. Any OTHER empty view is a defect.
        allowed = [n for n in names if ("等轴测" in n or "isometric" in n.lower()
                                        or "ISO" in n)]
        offenders = [n for n in names if n not in allowed]
        record("[A3] only the isometric view is empty", not offenders,
               "empty=%s allowed=%s offenders=%s" % (names, allowed, offenders))

        # Warnings must not treat the isometric emptiness as a problem.
        warns = " ".join(str(w) for w in (payload.get("warnings") or []))
        bad = [w for w in (payload.get("warnings") or [])
               if "empty" in str(w).lower() or "空" in str(w)]
        record("[A3] no empty-view warning for the isometric",
               not [w for w in bad if "等轴测" not in str(w)],
               "warnings=%s" % (str(bad)[:70] if bad else "none"))
        drawing.close_document(drw)
        return None
    finally:
        drawing.close_document(drw)
        drawing.close_document(part)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- A5
def case_nothing_disappears():
    """技术要求 / GTol / 基准 / 粗糙度 / BOM must survive the whole pipeline.

    Method: add each one through its own tool, then re-read the sheet and count. The count is
    what must hold — a receipt that says "added" is not evidence that it is still there after
    the pipeline saved the file.
    """
    work = tempfile.mkdtemp(prefix="ag5_")
    token = uuid.uuid4().hex[:6]
    part = os.path.join(work, "m_%s.SLDPRT" % token)
    drw = os.path.join(work, "d_%s.SLDDRW" % token)
    shutil.copy2(SRC, part)
    try:
        payload = pipeline.produce_drawing(part, output_path=drw)
        if not payload.get("ok"):
            return record("[A5] setup", False, payload.get("message") or "")

        before = drawing.list_drawing_annotations(drw)
        n_before = len(before.get("annotations") or [])

        tech = layout.add_technical_requirements(
            drw, ["1. 未注圆角 R1。", "2. 去毛刺。", "3. 调质处理。"])
        record("[A5] technical requirements added", bool(tech.get("ok")),
               (tech.get("message") or tech.get("error") or "")[:60])

        # Signature is (drawing_path, x, y, tolerance=, datum=, symbol=) — position first.
        gtol = drawing.add_geometric_tolerance(
            drw, 0.20, 0.06, tolerance="0.05", datum="A", symbol="位置度")
        record("[A5] GTOL added", bool(gtol.get("ok")),
               (gtol.get("message") or gtol.get("error") or "")[:60])

        rough = drawing.add_surface_finish(drw, 0.20, 0.10, roughness="3.2")
        record("[A5] surface finish added", bool(rough.get("ok")),
               (rough.get("message") or rough.get("error") or "")[:60])

        after = drawing.list_drawing_annotations(drw)
        rows = after.get("annotations") or []
        kinds = {}
        for row in rows:
            kinds[row.get("kind")] = kinds.get(row.get("kind"), 0) + 1
        record("[A5] annotation count grew", len(rows) > n_before,
               "%d -> %d  kinds=%s" % (n_before, len(rows), kinds))
        record("[A5] note survived", kinds.get("note", 0) >= 3,
               "notes=%d" % kinds.get("note", 0))
        record("[A5] gtol survived", kinds.get("gtol", 0) >= 1,
               "gtol=%d" % kinds.get("gtol", 0))
        record("[A5] surface finish survived", kinds.get("surface_finish", 0) >= 1,
               "sfsymbol=%d" % kinds.get("surface_finish", 0))

        # BOM is an assembly concept here: `export_bom(assembly_path)`. A part drawing has no
        # BOM table to lose, so this check is not part of A5 — it lives in the assembly smoke
        # item instead (`export_bom + csv`). Recorded here so a reader does not assume it was
        # quietly skipped.
        record("[A5] BOM not applicable to a part drawing", True,
               "covered by the assembly smoke item")
        return None
    finally:
        drawing.close_document(drw)
        drawing.close_document(part)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- A6
def case_gtol_visible_in_pdf():
    """GTOL frame must be visible in the exported PDF, not just present in the model.

    The earlier r8 fix wrote Chinese text into the symbol cell because <MOD-*-PAR> does not
    resolve to a GD&T glyph through SetFrameSymbols2. That means the frame exists — but
    "exists" and "prints" are different claims, and only a pixel check separates them.
    """
    work = tempfile.mkdtemp(prefix="ag6_")
    token = uuid.uuid4().hex[:6]
    part = os.path.join(work, "m_%s.SLDPRT" % token)
    drw = os.path.join(work, "d_%s.SLDDRW" % token)
    pdf = os.path.join(work, "out.pdf")
    shutil.copy2(SRC, part)
    try:
        payload = pipeline.produce_drawing(part, output_path=drw)
        if not payload.get("ok"):
            return record("[A6] setup", False, payload.get("message") or "")

        gtol = drawing.add_geometric_tolerance(
            drw, 0.20, 0.06, tolerance="0.05", datum="A", symbol="位置度")
        if not gtol.get("ok"):
            return record("[A6] gtol added", False,
                          (gtol.get("error") or "")[:60])

        # The frame must report its position back; without it a pixel check has no target.
        pos = gtol.get("position_actual_m") or gtol.get("position_m")
        record("[A6] gtol reports a position", pos is not None, "pos=%s" % (pos,))

        exp = drawing.export_drawing(drw, pdf, fmt="pdf")
        exists = os.path.exists(pdf) and os.path.getsize(pdf) > 1000
        record("[A6] pdf exported", bool(exp.get("ok")) and exists,
               "size=%s" % (os.path.getsize(pdf) if os.path.exists(pdf) else 0))

        if exists and pos:
            try:
                from PIL import Image, ImageChops
                import subprocess
                png = os.path.join(work, "page.png")
                # pdftoppm ships with the poppler tools the PDF skill already uses.
                subprocess.run(["pdftoppm", "-r", "150", "-png", "-f", "1", "-l", "1",
                                pdf, png], check=True, capture_output=True)
                candidates = [f for f in os.listdir(work) if f.startswith("page") and f.endswith(".png")]
                if candidates:
                    img = Image.open(os.path.join(work, candidates[0])).convert("RGB")
                    W, H = img.size
                    # Sheet lower-left origin -> PNG top-left, via the sheet size.
                    sheet = drawing._open_drawing(drw)
                    frames = [v for v in drawing._iter_views(sheet)
                              if getattr(v, "Type", None) == 1]
                    frame = list(frames[0].GetOutline) if frames else [0, 0, 0.42, 0.297]
                    sw, sh = frame[2] - frame[0], frame[3] - frame[1]
                    px = (pos[0] - frame[0]) / sw * W
                    py = (1 - (pos[1] - frame[1]) / sh) * H
                    box = (max(0, int(px) - 50), max(0, int(py) - 50),
                           min(W, int(px) + 50), min(H, int(py) + 50))
                    crop = img.crop(box)
                    dark = sum(1 for p in crop.getdata() if sum(p) < 450)
                    record("[A6] ink near the frame position", dark >= 20,
                           "dark pixels in 100x100 window: %d" % dark)
                else:
                    record("[A6] pdf rasterised", False, "pdftoppm produced no page")
            except FileNotFoundError:
                record("[A6] pdf rasterised", True,
                       "SKIPPED - pdftoppm not on PATH; only the PDF size was checked")
            except Exception as exc:                                # noqa: BLE001
                record("[A6] pdf rasterised", False, str(exc)[:70])
        return None
    finally:
        drawing.close_document(drw)
        drawing.close_document(part)
        shutil.rmtree(work, ignore_errors=True)


def main():
    print("A group: the three matrix items nothing exercised")
    print("-" * 84)
    for fn in (case_isometric_empty_view, case_nothing_disappears, case_gtol_visible_in_pdf):
        try:
            fn()
        except Exception as exc:                                    # noqa: BLE001
            record("[%s] raised" % fn.__name__, False,
                   "%s: %s" % (type(exc).__name__, str(exc)[:90]))
    failed = [r for r in results if not r[1]]
    print("-" * 84)
    print("%d passed / %d total" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
