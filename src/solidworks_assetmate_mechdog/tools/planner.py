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

from .. import perf_hooks
from ..sw_session import error, ok

# How many dimensions to aim for, from the 100-drawing baseline (mean 14.2, p75 17).
TARGET_DIMENSIONS = 18

# Feature types that carry the envelope rather than detail.
_ENVELOPE_TYPES = ("extrude", "revolve", "sweep", "loft", "thin", "boundary")


def _rank_dimensions(ledger_rows, holes, box, category=None):
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
    #
    # For a turned part the box order is arbitrary — a Ø14x60 shaft reports [14, 14, 60],
    # so `总长` came out as 14.0 while the real length is 60, and a caller using this list
    # as a dimension_filter lost the overall length of the part. P2-6, 2026-11-03.
    # Sorting descending for 轴类 makes 总长 the LONGEST extent, which is what the word
    # means to a draughtsman. Other categories keep X/Y/Z, where the axes are meaningful.
    box_values = list(box or [])
    if str(category) == "轴类":
        box_values = sorted(box_values, reverse=True)
    for axis, value in zip(("长", "宽", "高"), box_values):
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


@perf_hooks.timed("plan")
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
    arc_skipped = []
    for feature in led.get("features") or []:
        for dim in feature.get("dimensions") or []:
            kind = dim.get("dimension_type")
            if kind is not None and kind not in (0, 2, 3):
                continue  # angles and ordinates are not lengths
            # P3, 2026-11-03: arc-length dimensions showed up in the shortlist — 785.398 mm
            # and 771.436 mm, which are the CIRCUMFERENCES of the chamfer circles. No shop
            # inspects a chamfer by its perimeter, and they inflated the ledger coverage.
            # Kept out of the plan by default; `arc_lengths` still reports how many were
            # skipped so this is not a silent drop.
            if kind == 3:
                arc_skipped.append({
                    "feature": feature.get("name"), "dimension": dim.get("name"),
                    "value_mm": dim.get("value_mm"),
                })
                continue
            ledger_rows.append({
                "feature": feature.get("name"), "type": feature.get("type"),
                "dimension": dim.get("name"), "value_mm": dim.get("value_mm"),
            })

    holes = led.get("holes") or []
    keep = _rank_dimensions(ledger_rows, holes, box,
                            category=led.get("category"))[:TARGET_DIMENSIONS]
    # Tag every entry as driving / derived. The 外形 rows carry BOUNDING-BOX values
    # (210 / 204.85 / 98.31 in a report) — there is no such driving dimension in the model,
    # so auto_dimension can never insert them. Without this tag the caller watches
    # "filter_kept: 20" and wonders why three headline dimensions are missing from the sheet.
    for entry in keep:
        label = str(entry.get("feature") or "") + str(entry.get("dimension") or "")
        entry["kind"] = ("derived"
                         if any(word in label for word in ("外形", "总长", "总宽", "总高"))
                         else "driving")

    total_driving = stats.get("driving_dimensions") or len(ledger_rows)
    ratio = round(len(keep) / total_driving, 3) if total_driving else None

    # Sheet-splitting advice. 0.8.311 (report 2026-11-12 §7 N-3): this used to hard-code
    # ["front", "right"] while `suggested_views` echoed the caller's list — so a caller who
    # passed four views got a two-view plan with no explanation, and `delivery_check`
    # echoed the label. The plan now uses what was ASKED for.
    # Report 2026-11-12 (plan_drawing §4): `views_omitted` used to be computed as
    # "requested minus planned" where planned WAS requested, so it could never be anything
    # but `[]`. A field that cannot report anything is worse than no field: it reads as
    # "nothing was dropped" for every input. The list is now built by VALIDATING each
    # requested name against the SAME alias table `create_drawing` uses — a second
    # whitelist here would drift from it, which is how 0.8.310's problems started.
    # 0.8.318 (report 2026-11-12 v0.8.317 §6.1): `flat_pattern` is NOT in `VIEW_ALIASES`
    # (that table drives `create_drawing`'s view creation and has no flat-pattern entry),
    # yet this planner APPENDS a flat-pattern page for sheet metal. The result was one
    # receipt that both rejected `flat_pattern` as unknown and planned it. The plan layer
    # accepts it; `create_drawing` reached it through its own path.
    _valid_views = set(drawing_tools.VIEW_ALIASES) | {"flat_pattern"}
    _requested_views = [str(v).strip().lower() for v in (views or ())]
    _seen_views, _planned_views, _omitted_views = set(), [], []
    _omitted_reasons = {}
    for _v in _requested_views:
        if _v not in _valid_views:
            _omitted_views.append(_v)
            _omitted_reasons[_v] = "unknown_view"
            continue
        if _v in _seen_views:
            _omitted_views.append(_v)
            _omitted_reasons[_v] = "duplicate"
            continue
        _seen_views.add(_v)
        _planned_views.append(_v)
    if not _planned_views:
        # Nothing usable was asked for: fall back, and say so through the omitted list
        # rather than silently pretending the caller wanted the default.
        _planned_views = ["front", "right"]
    sheet_plan = [{"sheet": 1,
                   "content": "结构图：%s" % " + ".join(_planned_views),
                   "views": list(_planned_views)}]
    if led.get("category") == "钣金":
        sheet_plan.append({"sheet": 2, "content": "展开图：钣金展开 + 成型对照",
                           "views": ["flat_pattern"]})

    return ok(
        model=led.get("part"),
        category=led.get("category"),
        category_reason=led.get("category_reason"),
        # "low" = inferred from three bbox numbers, not from geometry. Treat as a hint.
        category_confidence=led.get("category_confidence"),
        box_mm=box,
        paper=paper,
        # Report 2026-11-12 (plan_drawing §5/§6): `list(views)` raised TypeError for
        # `views=None` and produced `suggested_views: []` beside `views_planned: [...]`
        # for `views=[]`. All three fields now come from the same validated list.
        suggested_views=list(_planned_views),
        views_requested=(_requested_views or None),
        views_planned=list(_planned_views),
        views_omitted=_omitted_views,
        # WHY each one was dropped: "unknown_view" or "duplicate". Without this the list
        # mixes two different problems into one bucket.
        views_omitted_reasons=_omitted_reasons,
        # 0.8.318 (report 2026-11-12 v0.8.317 §6.2): a dict keyed by view name collapses
        # repeats — `("bogus","bogus")` gave 2 omitted entries but 1 reason key. The pair
        # below keeps the count honest without breaking the dict shape existing callers use.
        views_omitted_detail=[{"view": _v, "reason": _omitted_reasons.get(_v)}
                              for _v in _omitted_views],
        views_omitted_count=len(_omitted_views),
        driving_dimensions=total_driving,
        keep_count=len(keep),
        arc_lengths_skipped=len(arc_skipped),
        arc_lengths=arc_skipped[:10],
        keep_ratio=ratio,
        keep_dimensions=keep,
        keep_driving_count=sum(1 for e in keep if e.get("kind") == "driving"),
        keep_derived_count=sum(1 for e in keep if e.get("kind") == "derived"),
        sheets=sheet_plan,
        baseline_note=("100 张真实工程图实测：尺寸中位 11、均值 14.2。"
                       "本方案推荐 %d 个，为全量导入（%d 个）的 %.0f%%。"
                       % (len(keep), total_driving, (ratio or 0) * 100)),
        advisory=[
            "这是启发式筛选（外形 → 孔 → 每特征最大尺寸），不是制图规划。",
            "调用方可按车间需要增减；`keep_dimensions` 就是给 auto_dimension 的筛选依据。",
        ],
    )
