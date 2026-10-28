"""Pre-drawing planning: what to draw, at what scale, and WHICH dimensions to keep.

The problem this solves: `auto_dimension(include_unmarked=True)` pushes every driving
dimension onto the sheet (44 on the panel box), while 100 real drawings average **14**
(median 11). Drafting is selection, not transcription.

This module produces a plan — it does not create the drawing. The caller can inspect the
selection before committing to a 400-second build, and can override it.

Honest scope: the selection rules are heuristics derived from the real-drawing baseline
(external envelope first, then one representative per feature, deduplicated holes). They are
not a substitute for a draughtsman deciding what the shop floor needs.
"""

import os

from ..sw_session import error, ok

# How many dimensions to aim for, from the 100-drawing baseline (mean 14.2, p75 17).
TARGET_DIMENSIONS = 18

# Feature types that carry the envelope rather than detail.
_ENVELOPE_TYPES = ("extrude", "revolve", "sweep", "loft", "thin", "boundary")


def _rank_dimensions(ledger_rows, holes, box):
    """Pick a defensible subset: envelope, then holes, then per-feature representatives."""
    chosen, seen = [], set()
    # Array spacing and sketch parameters are driving dimensions too, but a value far
    # beyond the part cannot be a manufacturable length (same guard as reconcile_ledger).
    try:
        ceiling = float(sum(v * v for v in box) ** 0.5) * 2.0 if box else None
    except Exception:
        ceiling = None

    def add(entry):
        value = entry.get("value_mm")
        if ceiling and value and float(value) > ceiling:
            return  # 6000 mm array spacing on a 200 mm part is not a length to dimension
        # Deduplicate by value: Sketch1 and Base-Flange1 both report 232.0, and printing
        # the same number twice wastes one of the ~14 slots a real drawing has.
        key = round(float(value or 0), 1)
        if key in seen:
            return
        seen.add(key)
        chosen.append(entry)

    # 1) Overall envelope: the three box measurements are what every drawing must state.
    for axis, value in zip(("长", "宽", "高"), box or []):
        add({"feature": "外形", "dimension": "总%s" % axis, "value_mm": value,
             "reason": "外形尺寸，任何图纸都必须标注"})

    # 2) Holes: one entry per distinct diameter, carrying the count.
    by_diameter = {}
    for hole in holes or []:
        diameter = hole.get("diameter_mm")
        if diameter:
            by_diameter.setdefault(round(float(diameter), 1), []).append(hole)
    for diameter in sorted(by_diameter, reverse=True):
        group = by_diameter[diameter]
        add({"feature": "孔", "dimension": "Ø%.1f × %d" % (diameter, len(group)),
             "value_mm": diameter,
             "reason": "%d 个同径孔，标一次即可" % len(group)})

    # 3) Per feature: the largest dimension is usually the defining one.
    per_feature = {}
    for row in ledger_rows:
        per_feature.setdefault(row.get("feature") or "?", []).append(row)
    for feature, rows in per_feature.items():
        usable = [r for r in rows if r.get("value_mm")]
        if not usable:
            continue
        usable.sort(key=lambda r: -float(r["value_mm"]))
        for row in usable[:1]:
            add({"feature": feature, "dimension": row.get("dimension"),
                 "value_mm": row.get("value_mm"),
                 "reason": "该特征的最大尺寸（被引用最多的定位依据）"})

    return chosen


def plan_drawing(model_path, paper="A3", views=("front", "right"), include_unmarked=False):
    """Produce a drawing plan: scale, views, and a short list of dimensions worth keeping.

    Read-only. Nothing is created. Use the output to decide, then call create_drawing /
    auto_dimension with `dimension_filter` derived from `keep_dimensions`.
    """
    from . import drawing as drawing_tools
    from . import ledger as ledger_tools

    path = os.path.abspath(model_path)
    if not os.path.exists(path):
        return error("bad_input", "model not found: %s" % path)

    led = ledger_tools.build_feature_ledger(path, include_faces=False)
    if not led.get("ok"):
        return error("ledger_failed", led.get("message") or "feature ledger failed")

    box = led.get("bbox_mm") or []
    stats = led.get("stats") or {}
    ledger_rows = []
    for feature in led.get("features") or []:
        for dim in feature.get("dimensions") or []:
            kind = dim.get("dimension_type")
            if kind is not None and kind not in (0, 2, 3):
                continue  # angles and ordinates are not lengths
            ledger_rows.append({
                "feature": feature.get("name"), "type": feature.get("type"),
                "dimension": dim.get("name"), "value_mm": dim.get("value_mm"),
            })

    holes = led.get("holes") or []
    keep = _rank_dimensions(ledger_rows, holes, box)[:TARGET_DIMENSIONS]

    total_driving = stats.get("driving_dimensions") or len(ledger_rows)
    ratio = round(len(keep) / total_driving, 3) if total_driving else None

    # Sheet-splitting advice, from the measured A3 3-view ceiling.
    sheet_plan = [{"sheet": 1, "content": "结构图：主视 + 侧视",
                   "views": ["front", "right"]}]
    if led.get("category") == "钣金":
        sheet_plan.append({"sheet": 2, "content": "展开图：钣金展开 + 成型对照",
                           "views": ["flat_pattern"]})

    return ok(
        model=led.get("part"),
        category=led.get("category"),
        box_mm=box,
        paper=paper,
        suggested_views=list(views),
        driving_dimensions=total_driving,
        keep_count=len(keep),
        keep_ratio=ratio,
        keep_dimensions=keep,
        sheets=sheet_plan,
        baseline_note=("100 张真实工程图实测：尺寸中位 11、均值 14.2。"
                       "本方案推荐 %d 个，为全量导入（%d 个）的 %.0f%%。"
                       % (len(keep), total_driving, (ratio or 0) * 100)),
        advisory=[
            "这是启发式筛选（外形 → 孔 → 每特征最大尺寸），不是制图规划。",
            "调用方可按车间需要增减；`keep_dimensions` 就是给 auto_dimension 的筛选依据。",
        ],
    )
