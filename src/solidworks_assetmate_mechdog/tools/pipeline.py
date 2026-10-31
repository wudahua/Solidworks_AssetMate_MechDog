"""One-call drawing pipeline: model in, annotated & judged drawing out.

Why this exists: every capability (views, dimensions, tolerances, roughness, hole callouts,
Gtol, audit, verdict) was reachable only by calling it explicitly, in the right order, with
the right arguments. A real job is a sequence, not a menu — and the defaults were "write
nothing", which is safe but leaves the drawing incomplete.

Defaults are chosen per part category, taken from what 100 real drawings actually do:

| category | default annotations |
|---|---|
| 钣金 / 一般零件 (structural) | 未注公差 GB/T 1804-m, 其余 Ra3.2, 中心标记, 孔标注 |
| 轴类 (turned / mating) | 同上 + 关键尺寸公差留给调用方 |

Nothing here invents values: tolerances and roughness come from the category defaults,
which are stated in the result so they can be challenged.
"""

import os

from ..sw_session import error, ok

# Category defaults. `notes` become drawing notes; `roughness` becomes a surface finish
# symbol; both are stated in the result so the caller knows what was assumed.
CATEGORY_DEFAULTS = {
    "钣金": {
        "general_tolerance": "未注公差按 GB/T 1804-m",
        "roughness": "3.2",
        "hole_callouts": True,
        "center_marks": True,
    },
    "一般零件": {
        "general_tolerance": "未注公差按 GB/T 1804-m",
        "roughness": "3.2",
        "hole_callouts": True,
        "center_marks": True,
    },
    "轴类": {
        "general_tolerance": "未注公差按 GB/T 1804-m",
        "roughness": "1.6",
        "hole_callouts": False,
        "center_marks": True,
    },
    "装配体": {
        "general_tolerance": "未注公差按 GB/T 1804-m",
        "roughness": "",
        "hole_callouts": False,
        "center_marks": False,
    },
}
FALLBACK_DEFAULTS = CATEGORY_DEFAULTS["一般零件"]


def _defaults_for(category):
    return CATEGORY_DEFAULTS.get(str(category or ""), FALLBACK_DEFAULTS)


def _callout_warning(callouts):
    """Explain a hole-callout run that did not finish, instead of letting it look empty.

    Two very different situations produced identical output before: "no holes in this
    view" and "budget ran out after 12 of 20 edges". The user's next action differs — one
    means add Ø dimensions, the other means raise the budget — so the text must differ too.
    """
    if not callouts:
        return None
    exceeded = callouts.get("budget_exceeded")
    created = callouts.get("created_count") or 0
    elapsed = callouts.get("elapsed_s")
    scanned = callouts.get("circular_edges")
    if exceeded and created == 0:
        return ("孔标注因预算超时未完成（%.1f s 内只扫了 %s 条圆边，未生成任何标注）。"
                "这不是「图纸没有孔」，请提高 hole_budget_s 后重试。"
                % (float(elapsed or 0), scanned))
    if exceeded:
        return ("孔标注预算超时：已生成 %d 个，%s s 后中止（扫到 %s 条圆边）。"
                "结果不完整，提高 hole_budget_s 可继续。"
                % (created, elapsed, scanned))
    return None


def produce_drawing(model_path, output_path=None, paper="A3", views=None,
                    category=None, do_hole_callouts=None, do_surface_finish=None,
                    do_derived_check=True, hole_budget_s=45.0, prune_to_plan=True):
    """Run the whole chain and report each step plus the delivery verdict.

    Steps (each failure is recorded, not fatal — a partial drawing beats no drawing):
      1 ledger → 2 plan → 3 create views → 4 auto dimension → 5 center marks
      → 6 surface finish (category default) → 7 hole callouts (if holes exist)
      → 8 audit → 9 ledger reconciliation → 10 delivery verdict

    hole_budget_s defaults to 45 s, not 20: measured cost is ~1.7 s per circular edge, and
    a 20 s budget silently truncated a 35 s job to zero callouts — which then flipped the
    verdict to "仅供参考". A timed-out callout run must never look like "this part has no
    holes" (reported 2026-10-28).

    prune_to_plan=True (N3) feeds `plan.keep_dimensions` into auto_dimension's
    `dimension_filter`, so the sheet really ends up with ~keep_count dimensions instead of
    every dimension the model marks for drawing. Set False to get the old insert-everything
    behaviour; `dimension_gap` still reports planned vs actual either way.
    """
    from . import drawing as drawing_tools
    from . import ledger as ledger_tools
    from . import planner

    path = os.path.abspath(model_path)
    if not os.path.exists(path):
        return error("bad_input", "model not found: %s" % path)

    steps = []

    def record(name, payload, detail=""):
        steps.append({
            "step": name,
            "ok": bool(payload.get("ok")) if isinstance(payload, dict) else bool(payload),
            "detail": detail or (payload.get("error") if isinstance(payload, dict) else ""),
        })
        return payload if isinstance(payload, dict) else {}

    # 1) ledger — also tells us the category when the caller did not
    led_raw = ledger_tools.build_feature_ledger(path, include_faces=False)
    led = record("ledger", led_raw,
                 "features=%d" % len(led_raw.get("features") or []))
    if not led.get("ok"):
        return error("ledger_failed", led.get("message") or "feature ledger failed")
    resolved_category = category or led.get("category")
    defaults = _defaults_for(resolved_category)

    # 2) plan — how many dimensions are worth keeping
    plan_raw = planner.plan_drawing(path, paper=paper)
    plan = record("plan", plan_raw,
                  "keep %s of %s dimensions" % (plan_raw.get("keep_count"),
                                                plan_raw.get("driving_dimensions")))
    keep_dimensions = plan_raw.get("keep_count")
    driving = plan_raw.get("driving_dimensions")

    # 3) views
    target = output_path or os.path.splitext(path)[0] + ".SLDDRW"
    created = record("create_drawing",
                     drawing_tools.create_drawing(path, target, views=views or ["front", "right"],
                                                  paper=paper),
                     "")
    if not created.get("ok"):
        return error("create_failed", created.get("message") or "view creation failed",
                     "Drawing not produced; see create_drawing errors.")

    # 4) dimensions — N3: feed the plan back in, so "keep N" is what lands on the sheet.
    keep_list = plan_raw.get("keep_dimensions") or []
    if prune_to_plan and keep_list:
        dims = record("dimensions",
                      drawing_tools.auto_dimension(target, all_views=True,
                                                   dimension_filter=keep_list),
                      "")
    else:
        dims = record("dimensions", drawing_tools.auto_dimension(target, all_views=True), "")

    # 5) center marks
    if defaults["center_marks"]:
        record("center_marks", drawing_tools.add_center_marks(target), "")

    # 6) surface finish — one symbol is what real drawings do ("其余 Ra3.2")
    roughness = defaults["roughness"]
    if (do_surface_finish if do_surface_finish is not None else bool(roughness)) and roughness:
        record("surface_finish",
               drawing_tools.add_surface_finish(target, 0.36, 0.06, roughness=roughness,
                                                leader_type="none"),
               "Ra%s（类别默认）" % roughness)

    # 7) hole callouts — only where the ledger sees cylindrical faces
    # `include_faces=False` never counts holes, so do not gate on it — add_hole_callouts
    # enumerates the view's circular edges itself and reports what it found.
    want_callouts = do_hole_callouts if do_hole_callouts is not None else defaults["hole_callouts"]
    callouts = {}
    if want_callouts:
        callouts = record("hole_callouts",
                          drawing_tools.add_hole_callouts(target, budget_s=hole_budget_s),
                          "")

    summary = {}
    try:
        ann = drawing_tools.list_drawing_annotations(target)
        summary = ann.get("totals") or {}
    except Exception:
        ann = {}

    # 8) audit
    audit = record("audit", drawing_tools.drawing_audit(target), "")
    layout_warnings = [w.get("issue") for w in (audit.get("warnings") or [])
                       if w.get("issue") in ("views_overlap", "view_outside_sheet")]

    # 9) reconciliation
    recon = {}
    if do_derived_check:
        recon_raw = drawing_tools.reconcile_ledger(path, target)
        recon = record("reconcile", recon_raw, "coverage=%s" % recon_raw.get("coverage"))

    # 10) verdict
    verdict = record("delivery_check", drawing_tools.delivery_check(path, target), "")

    return ok(
        model=path,
        drawing=target,
        category=resolved_category,
        paper=paper,
        steps=steps,
        defaults_applied={
            "general_tolerance": defaults["general_tolerance"],
            "roughness": roughness,
            "hole_callouts": bool(want_callouts),
            "center_marks": defaults["center_marks"],
        },
        plan={"driving_dimensions": driving, "keep_dimensions": keep_dimensions},
        callouts={"created": callouts.get("created_count"),
                  "failed": callouts.get("failed_count"),
                  "scanned": callouts.get("circular_edges"),
                  "holes_found": callouts.get("holes_found"),
                  # Passing these through matters: a timed-out run used to be
                  # indistinguishable from a part with no holes, and the verdict flipped
                  # on that difference alone.
                  "budget_exceeded": callouts.get("budget_exceeded"),
                  "elapsed_s": callouts.get("elapsed_s")},
        callout_warning=_callout_warning(callouts),
        # Plan says "keep N"; auto_dimension inserts every dimension marked for drawing.
        # The gap is real and stated rather than silently reconciled — pruning inserted
        # dimensions would need delete APIs that are riskier than leaving them.
        dimension_gap=(None if keep_dimensions is None else {
            "planned": keep_dimensions,
            "inserted": (summary or {}).get("dimension"),
            "pruned_to_plan": bool(prune_to_plan),
            "note": ("规划已生效：插入后按 keep_dimensions 删除多余尺寸，"
                     if prune_to_plan else
                     "规划未生效（prune_to_plan=False）：实际导入的是模型全部标记为工程图用的尺寸，"
                     "需人工删减或按需补标。")
                    + ("filter 失败/未命中时不会删除，见 dimensions 步骤的 filter_aborted。"
                       if prune_to_plan else ""),
        }),
        annotations=summary,
        layout_warnings=layout_warnings,
        coverage=recon.get("coverage"),
        verdict=verdict.get("verdict"),
        blockers=[b.get("detail") for b in (verdict.get("blockers") or [])],
        pending=[p.get("detail") for p in (verdict.get("pending") or [])],
        note=("流水线按类别套默认值；defaults_applied 里写明了假定什么。"
              "某一步失败不会中断后续步骤，逐条看 steps。"),
    )
