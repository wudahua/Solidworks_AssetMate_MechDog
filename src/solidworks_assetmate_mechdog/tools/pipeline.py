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
                    do_derived_check=True, hole_budget_s=45.0, prune_to_plan=True,
                    overwrite=False, projection="first"):
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
    from ..drawing_batch import BatchOptions, DrawingBatch
    from . import drawing as drawing_tools
    from . import ledger as ledger_tools
    from . import planner

    path = os.path.abspath(model_path)
    if not os.path.exists(path):
        return error("bad_input", "model not found: %s" % path)

    # P1, third report running (2026-11-03): this pipeline is NOT idempotent. Running it
    # twice on the same output_path opens the existing drawing and inserts everything again:
    # dimensions 8 -> 24, hole callouts 5 -> 10, centre marks 5 -> 8, surface finishes
    # 1 -> 2, and both runs report ok:true with blockers: []. There was no
    # output_exists / overwrite / preexisting handling of any kind.
    #
    # Overwriting is the sane default for a "produce this drawing" call, but silently
    # DOUBLING an existing sheet is not — so an existing file is refused unless the caller
    # says overwrite=True, and overwriting really does start from an empty drawing.
    if output_path:
        _exists_at = os.path.abspath(output_path)
        if os.path.splitext(_exists_at)[1].lower() != ".slddrw":
            _exists_at += ".SLDDRW"
        if os.path.exists(_exists_at):
            if not overwrite:
                return error(
                    "output_exists",
                    "output already exists: %s" % _exists_at,
                    "Pass overwrite=True to rebuild it from scratch, or choose another "
                    "output_path. Running again without this used to APPEND a second full "
                    "set of dimensions to the existing sheet (measured 8 -> 24 dimensions).",
                    path=_exists_at, overwrite=False)
            # Close any open copy first, then remove it so create_drawing starts clean.
            try:
                from ..sw_session import get_sw
                for _doc in (get_sw().GetDocuments or ()):
                    try:
                        if os.path.normcase(str(_doc.GetPathName)) == os.path.normcase(_exists_at):
                            get_sw().CloseDoc(str(_doc.GetTitle))
                    except Exception:
                        continue
            except Exception:
                pass
            try:
                os.remove(_exists_at)
            except OSError as exc:
                return error("output_locked", "cannot overwrite %s: %s" % (_exists_at, exc),
                             "Close the drawing in SolidWorks and retry.")

    steps = []

    def record(name, payload, detail=""):
        steps.append({
            "step": name,
            "ok": bool(payload.get("ok")) if isinstance(payload, dict) else bool(payload),
            "detail": detail or (payload.get("error") if isinstance(payload, dict) else ""),
        })
        return payload if isinstance(payload, dict) else {}

    # 0.8.120: outline reads are COUNTERS, not a cache. Reset per run so the number in the
    # receipt describes this drawing, and report it so the "should we cache" question is
    # answered from data (see docs/PR3-布局缓存-不建议实施说明.md).
    drawing_tools.reset_outline_stats()

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
                                                  paper=paper, projection=projection),
                     "")
    if not created.get("ok"):
        return error("create_failed", created.get("message") or "view creation failed",
                     "Drawing not produced; see create_drawing errors.")

    # Section 3 of the spec puts every write inside a drawing_batch scope, so that preference
    # changes are undone even when the body raises. Options are the defaults, which switch
    # NOTHING: the A/B in v0.8.112 found no measurable gain from either switch, so enabling
    # them here would buy nothing and cost a preference write. What we get is the scope.
    #
    # Entered by hand rather than `with` on purpose: the body is ~100 lines with a single
    # exit point, and re-indenting it to gain `with` would be a large diff for a smaller
    # guarantee (a stray early `return` would skip __exit__ anyway if I got the indent wrong).
    _batch = None
    try:
        from ..sw_session import get_sw
        _batch = DrawingBatch(drawing_tools._open_drawing(target), get_sw(),
                              BatchOptions()).__enter__()
    except Exception as _exc:                                   # noqa: BLE001
        record("drawing_batch", {"ok": False, "error": str(_exc)[:120]}, "")
    else:
        record("drawing_batch", {"ok": True,
                                 "switches": _batch.switch_report,
                                 "options": "defaults: no switch is changed"}, "")

    # 4) dimensions — N3: feed the plan back in, so "keep N" is what lands on the sheet.
    keep_list = plan_raw.get("keep_dimensions") or []
    if prune_to_plan and keep_list:
        dims = record("dimensions",
                      drawing_tools.auto_dimension(target, all_views=True,
                                                   dimension_filter=keep_list,
                                                   save=False, audit=False),
                      "")
    else:
        dims = record("dimensions", drawing_tools.auto_dimension(target, all_views=True, save=False, audit=False), "")

    # 5) center marks
    if defaults["center_marks"]:
        record("center_marks", drawing_tools.add_center_marks(target, save=False), "")

    # 6) surface finish — one symbol is what real drawings do ("其余 Ra3.2")
    roughness = defaults["roughness"]
    if (do_surface_finish if do_surface_finish is not None else bool(roughness)) and roughness:
        record("surface_finish",
               drawing_tools.add_surface_finish(target, 0.36, 0.06, roughness=roughness,
                                                leader_type="none", save=False),
               "Ra%s（类别默认）" % roughness)

    # 7) hole callouts — only where the ledger sees cylindrical faces
    # `include_faces=False` never counts holes, so do not gate on it — add_hole_callouts
    # enumerates the view's circular edges itself and reports what it found.
    want_callouts = do_hole_callouts if do_hole_callouts is not None else defaults["hole_callouts"]
    callouts = {}
    if want_callouts:
        callouts = record("hole_callouts",
                          drawing_tools.add_hole_callouts(target, budget_s=hole_budget_s, save=False),
                          "")

    # PR1: the sub-tools no longer save. Pipeline saves ONCE here, after every writer has
    # run and before anything reads the sheet back. Previously auto_dimension alone saved
    # twice (mid-function and in the place_outside branch) and audited once, then the
    # pipeline audited again — four saves and three audits per run.
    # Every sub-tool now honours save=False (center marks, surface finish, hole callouts,
    # notes) — verified by the 0.8.111 assertion. This is the only save in a pipeline run.
    try:
        # Last thing before the sheet is frozen: no dimension may be left off the paper.
        # Report 2026-11-04 found eight reference dimensions at y = -0.068 .. -0.200 on a
        # 297 mm sheet. Running this HERE, not inside auto_dimension, is deliberate — three
        # attempts to clamp inside that function missed because they executed before
        # `place_outside` had finished pushing dimensions around.
        _clamp = drawing_tools.clamp_dimensions_into_safe_area(target)
        record("clamp_dimensions", _clamp,
               _clamp.get("note") or ("clamped=%s" % _clamp.get("clamped_count")))
        _doc = drawing_tools._open_drawing(target)
        drawing_tools._save_drawing(_doc)
        record("save", {"ok": True}, "one save for the whole run")
    except Exception as exc:
        record("save", {"ok": False, "error": "save_failed", "message": str(exc)}, "")

    # The sheet is final from here on: nothing below writes to it. Serve the pipeline
    # summary and delivery_check's own annotation read from ONE pass instead of two.
    # Measured 2026-11-03: that read is 13.4 s, and delivery_check spent 38% of its 34.9 s
    # in it. Enabled/disabled rather than a context manager so the call sites do not need
    # re-indenting; a stale cache is impossible because enable() resets it and the sheet
    # cannot change in between.
    drawing_tools.annotations_cache_enable()
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
    drawing_tools.annotations_cache_disable()

    # Spec section 13: do NOT return ok:true when the final file was not actually updated,
    # or when the sheet never got its dimensions. Until 0.8.114 every step's ok was recorded
    # in  and then ignored — a run whose save raised still reported success, and the
    # only evidence was a nested field a caller had to know to read.
    #
    # Not every step is critical: hole callouts legitimately report ok=False on geometry that
    # has no native callout, and a missing surface finish is a documentation choice, not a
    # failure. Only the steps that decide whether the file is a usable drawing are fatal.
    if _batch is not None:
        # Restore any preference the scope touched. Raised as a restore failure rather than
        # swallowed: a failed restore leaves the user's SolidWorks in a state they did not
        # choose, and section 13 lists that as a must-not-report-ok condition.
        try:
            _batch.__exit__(None, None, None)
        except Exception as _exc:                               # noqa: BLE001
            return error("restore_failed", str(_exc)[:160],
                         "SolidWorks preferences may not have been restored.")

    _CRITICAL = ("create_drawing", "dimensions", "save")
    _failed_critical = [st["step"] for st in steps
                        if st["step"] in _CRITICAL and not st["ok"]]
    if _failed_critical:
        return error(
            "critical_step_failed",
            "%s failed; the drawing is not usable" % ", ".join(_failed_critical),
            "steps: %s" % "; ".join("%s=%s" % (st["step"], st["ok"]) for st in steps))

    return ok(
        model=path,
        drawing=target,
        category=resolved_category,
        paper=paper,
        # The audit is already run for its own step; its detail was previously thrown away, so
        # a caller could not see which views exist or what the audit warned about without
        # running drawing_audit again. None of that is free (2 view reads), so surface it.
        outline_reads=drawing_tools.outline_stats(),
        views=audit.get("views"),
        warnings=audit.get("warnings"),
        view_count=audit.get("view_count"),
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
