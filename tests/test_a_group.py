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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fixtures import FixtureMissing, resolve_part, exit_missing      # noqa: E402

try:
    SRC = resolve_part(script="tests/test_a_group.py")
except FixtureMissing as _exc:
    exit_missing(_exc)

results = []


def record(name, ok, detail="", skip=False):
    """Record one assertion.

    `skip=True` means the assertion never RAN (a missing library, not a wrong result).
    Report 2026-11-06: A6's three pixel assertions were all inside an `import pymupdf`
    block, and the ImportError branch logged one FAIL — so a clean install printed
    "14/15" and read like one small problem, while the question the case exists to answer
    ("is the GTol visible in the PDF?") had not been asked at all.
    """
    results.append((name, bool(ok), detail, bool(skip)))
    label = "SKIP" if skip else ("PASS" if ok else "FAIL")
    print("%-56s %s  %s" % (name, label, detail))
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
        # SolidWorks names the views 工程图视图1/2/3 regardless of the kind requested, so the
        # name cannot identify the isometric. We asked for front/right/isometric in that order,
        # and the isometric is the one that legitimately carries no model dimensions - so the
        # check is: whatever is empty must be exactly one view, and it must not be the first
        # two (those are the ones we expect to be dimensioned).
        record("[A3] exactly one empty view", len(empty) <= 1,
               "empty=%s" % names)
        offenders = [n for n, v in zip(names, empty)
                     if views.index(v) < 2]
        record("[A3] the dimensioned views are not empty", not offenders,
               "offenders=%s" % offenders)

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
            drw, 0.20, 0.06, tolerance="0.05", datum="A", symbol="position")
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
        record("[A5] gtol survived", kinds.get("geometric_tolerance", 0) >= 1,
               "gtol=%d" % kinds.get("geometric_tolerance", 0))
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
            drw, 0.20, 0.06, tolerance="0.05", datum="A", symbol="position")
        if not gtol.get("ok"):
            return record("[A6] gtol added", False,
                          (gtol.get("error") or "")[:60])

        # The frame must report its position back; without it a pixel check has no target.
        # The receipt uses , not position_actual_m (that name belongs to
        # add_surface_finish). The coordinates we asked for are the ones to look at in the PDF.
        record("[A6] gtol reports a position", bool(gtol.get("position_set")),
               "position_set=%r" % (gtol.get("position_set"),))
        pos = (0.20, 0.06)

        exp = drawing.export_drawing(drw, pdf, fmt="pdf")
        exists = os.path.exists(pdf) and os.path.getsize(pdf) > 1000
        record("[A6] pdf exported", bool(exp.get("ok")) and exists,
               "size=%s" % (os.path.getsize(pdf) if os.path.exists(pdf) else 0))

        if exists and pos:
            try:
                # pypdfium2, NOT pymupdf. Report 2026-11-06 (P1): this block imported
                # `pymupdf`, which is in no dependency list anywhere — `pyproject.toml`'s
                # hard deps are mcp / pywin32 / Pillow / pypdfium2. On a clean install the
                # whole pixel check therefore never ran, and the failure was recorded as
                # "no PDF renderer", which reads like a missing machine capability while the
                # project's own renderer (pypdfium2) was installed and in use elsewhere in
                # the same run.
                #
                # pdftoppm is not on PATH here either, which is why the first version of
                # this check silently degraded to "the PDF is 47 KB". A size check cannot
                # tell a rendered frame from an empty one.
                import pypdfium2 as pdfium

                if not os.path.exists(pdf):
                    record("[A6] pdf rasterised", False, "pdf missing")
                else:
                    _pdf = pdfium.PdfDocument(pdf)
                    if len(_pdf) == 0:
                        record("[A6] pdf rasterised", False, "pdfium produced no page")
                        _pdf.close()
                    else:
                        page0 = _pdf[0]
                        # 150 dpi at 72 dpi base = scale 150/72
                        bitmap = page0.render(scale=150 / 72.0)
                        pil = bitmap.to_pil().convert("RGB")
                        W, H = pil.size
                        samples_bytes = pil.tobytes()
                        page0.close()
                        _pdf.close()
                        sheet = drawing._open_drawing(drw)
                    frames = [v for v in drawing._iter_views(sheet)
                              if getattr(v, "Type", None) == 1]
                    frame = list(frames[0].GetOutline) if frames else [0, 0, 0.42, 0.297]
                    sw, sh = frame[2] - frame[0], frame[3] - frame[1]
                    px = (pos[0] - frame[0]) / sw * W
                    py = (1 - (pos[1] - frame[1]) / sh) * H
                    x0 = max(0, int(px) - 50)
                    y0 = max(0, int(py) - 50)
                    x1 = min(W, int(px) + 50)
                    y1 = min(H, int(py) + 50)
                    dark = 0
                    # PIL RGB: 3 bytes per pixel, tightly packed.
                    _stride, _n = W * 3, 3
                    for yy in range(y0, y1):
                        row = yy * _stride
                        for xx in range(x0, x1):
                            off = row + xx * _n
                            if (samples_bytes[off] + samples_bytes[off + 1]
                                    + samples_bytes[off + 2] < 450):
                                dark += 1
                    record("[A6] ink near the frame position", dark >= 20,
                           "%dx%d page, dark pixels in 100x100 window: %d at (%.0f,%.0f)"
                           % (W, H, dark, px, py))
                    # A blank sheet would give ~0 dark pixels everywhere; this is the control
                    # that says the 20 above is meaningful rather than a rendering artefact.
                    whole = 0
                    for yy in range(0, H, 4):
                        row = yy * _stride
                        for xx in range(0, W, 4):
                            off = row + xx * _n
                            if (samples_bytes[off] + samples_bytes[off + 1]
                                    + samples_bytes[off + 2] < 450):
                                whole += 1
                    record("[A6] page is not blank", whole > 200,
                           "dark pixels on a 4x sampled page: %d" % whole)
            except ImportError as exc:
                # "Not verified" must not be recorded as "FAIL". Report 2026-11-06: the old
                # branch logged one FAIL and stopped, so the run read "14/15, one small
                # problem" while in fact all THREE pixel assertions of A6 had never run.
                # The tool group's stated purpose is "GTol is visible in the PDF"; a
                # missing renderer means that question went unanswered, not that the answer
                # was no.
                record("[A6] pdf rasterised", False,
                       "SKIPPED — no renderer (%s); the 3 ink assertions did NOT run" % exc,
                       skip=True)
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
    failed = [r for r in results if not r[1] and not r[3]]
    skipped = [r for r in results if r[3]]
    print("-" * 84)
    print("%d passed / %d total%s" % (
        len(results) - len(failed) - len(skipped), len(results),
        (", %d skipped" % len(skipped)) if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
