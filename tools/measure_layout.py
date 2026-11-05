"""Measure what a produced drawing actually uses, before touching the layout.

Run:  python tools/measure_layout.py

Answers three questions, on real parts, without changing any code:

  1. How big is each view, and how big is the group? Is "40% of the sheet" caused by small
     views (correct, nothing to fix) or by the packer clustering them in a corner (a bug)?
  2. After `place_outside`, how much room does each side actually consume, and how much is
     left between the outermost dimension and the frame? That is the budget the next
     dimension would have had.
  3. Subtracting the view group from the safe area leaves four margins. Those margins are
     what a "dimension band" could be made of.

Nothing here writes to the repo; drawings go to a temp dir and are closed afterwards.
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from solidworks_assetmate_mechdog.tools import drawing, pipeline   # noqa: E402

PARTS = [
    ("接槽 (报告那个件)", r"D:\WorkBuddy Space Test\接槽-工程图\接槽.SLDPRT"),
    ("旋转轴", r"D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\测试用例\旋转轴-工程图\旋转轴.SLDPRT"),
]


def measure(label, src, views=("front", "top", "isometric")):
    work = tempfile.mkdtemp(prefix="meas_")
    token = uuid.uuid4().hex[:10]
    part = os.path.join(work, "m_%s.SLDPRT" % token)
    drw = os.path.join(work, "d_%s.SLDDRW" % token)
    shutil.copy2(src, part)
    try:
        payload = pipeline.produce_drawing(part, output_path=drw, views=list(views))
        if not payload.get("ok"):
            print("%s: FAILED %s" % (label, payload.get("message")))
            return

        doc = drawing._open_drawing(drw)
        sheet = [v for v in drawing._iter_views(doc) if getattr(v, "Type", None) == 1][0]
        frame = list(sheet.GetOutline)
        sw, sh = frame[2] - frame[0], frame[3] - frame[1]
        safe = drawing.template_safe_area(sheet, sw, sh)

        boxes = []
        for v in drawing._iter_views(doc):
            if getattr(v, "Type", None) == 1:
                continue
            b = drawing.precise_outline(v)
            if not b:
                continue
            orient = ""
            try:
                orient = str(drawing.call_method(v, "GetOrientationName") or "")
            except Exception:
                pass
            boxes.append({"view": str(v.Name), "orient": orient, "box": b,
                          "w_mm": (b[2] - b[0]) * 1000, "h_mm": (b[3] - b[1]) * 1000,
                          "scale": float(v.ScaleDecimal)})

        if not boxes:
            print("%s: no views" % label)
            return
        gx0 = min(b["box"][0] for b in boxes)
        gy0 = min(b["box"][1] for b in boxes)
        gx1 = max(b["box"][2] for b in boxes)
        gy1 = max(b["box"][3] for b in boxes)
        gw, gh = (gx1 - gx0) * 1000, (gy1 - gy0) * 1000

        area_w = sw - safe.get("left", 0) - safe.get("right", 0)
        area_h = sh - safe.get("top", 0) - safe.get("bottom", 0)

        print("=" * 78)
        print("%s  |  sheet %.0f x %.0f mm  |  safe area %.0f x %.0f mm" %
              (label, sw * 1000, sh * 1000, area_w * 1000, area_h * 1000))
        print("-" * 78)
        print("VIEWS")
        for b in boxes:
            print("  %-12s %-8s scale=%-5s %6.1f x %6.1f mm  x=[%.3f,%.3f] y=[%.3f,%.3f]" %
                  (b["view"], b["orient"], b["scale"], b["w_mm"], b["h_mm"],
                   b["box"][0], b["box"][2], b["box"][1], b["box"][3]))
        print("  GROUP        %6.1f x %6.1f mm  = %.0f%% of safe width, %.0f%% of safe height"
              % (gw, gh, gw / (area_w * 1000) * 100, gh / (area_h * 1000) * 100))

        # Where the group sits inside the safe area.
        left_m = (gx0 - safe.get("left", 0)) * 1000
        right_m = (sw - safe.get("right", 0) - gx1) * 1000
        top_m = (sh - safe.get("top", 0) - gy1) * 1000
        bottom_m = (gy0 - safe.get("bottom", 0)) * 1000
        print("MARGINS AROUND GROUP (mm): left %.1f  right %.1f  top %.1f  bottom %.1f"
              % (left_m, right_m, top_m, bottom_m))

        # Dimension spread: how far the annotations reach beyond the views.
        anns = drawing.list_drawing_annotations(drw)
        dims = [a for a in (anns.get("annotations") or [])
                if a.get("kind") == "dimension" and a.get("position_m")]
        if dims:
            ax0 = min(a["position_m"][0] for a in dims)
            ax1 = max(a["position_m"][0] for a in dims)
            ay0 = min(a["position_m"][1] for a in dims)
            ay1 = max(a["position_m"][1] for a in dims)
            print("DIMENSIONS   %d placed, reach x=[%.3f,%.3f] y=[%.3f,%.3f]" %
                  (len(dims), ax0, ax1, ay0, ay1))
            print("  beyond views (mm): left %.1f  right %.1f  top %.1f  bottom %.1f" %
                  ((gx0 - ax0) * 1000, (ax1 - gx1) * 1000,
                   (ay1 - gy1) * 1000, (gy0 - ay0) * 1000))
            print("  room left before frame (mm): left %.1f  right %.1f  top %.1f  bottom %.1f"
                  % ((ax0 - safe.get("left", 0)) * 1000,
                     (sw - safe.get("right", 0) - ax1) * 1000,
                     (sh - safe.get("top", 0) - ay1) * 1000,
                     (ay0 - safe.get("bottom", 0)) * 1000))
        drawing.close_document(drw)
    finally:
        try:
            drawing.close_document(drw)
        except Exception:
            pass
        try:
            drawing.close_document(part)
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)


def main():
    for label, src in PARTS:
        if not os.path.exists(src):
            print("%s: source missing (%s)" % (label, src))
            continue
        measure(label, src)


if __name__ == "__main__":
    main()
