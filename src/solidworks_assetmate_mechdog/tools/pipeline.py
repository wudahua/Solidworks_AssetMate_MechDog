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
        # Holes live on the face, so the top view is where the callouts can attach.
        "views": ["front", "top", "isometric"],
    },
    "一般零件": {
        "general_tolerance": "未注公差按 GB/T 1804-m",
        "roughness": "3.2",
        "hole_callouts": True,
        "center_marks": True,
        "views": ["front", "top", "isometric"],
    },
    "轴类": {
        "general_tolerance": "未注公差按 GB/T 1804-m",
        "roughness": "1.6",
        "hole_callouts": False,
        "center_marks": True,
        # A shaft's top view is a second long bar — redundant on a turned part. The
        # isometric carries the keyway and cross-holes instead (report 2026-11-05 also
        # complained that front+top+left gave "两张几乎一样的长轴视图").
        # Report 2026-11-05 (P0): v0.8.146 changed this to ["front", "isometric"] on the
        # assumption that `front` is the long bar and `top` a redundant second one. That
        # holds only when the shaft is laid out ACROSS the front view. On 飞达动力轴 the
        # front view is the END FACE — `precise_outline_m` 0.035 x 0.035 — while `right`
        # is the 0.107 x 0.035 bar. Dropping `top` therefore dropped the ONLY view that
        # could carry axial dimensions: measured 10 dimensions -> 3 (diameters only),
        # coverage 0.444 -> 0.167, and all 13 steps still reported ok.
        #
        # `front + right + isometric` has a bar under EITHER orientation, and the isometric
        # still carries the keyway and cross-holes that 0.8.146 was after. It is one view
        # more than before, not one fewer — which is the correct direction for a shape
        # whose orientation the category cannot predict.
        "views": ["front", "right", "isometric"],
    },
    "装配体": {
        "general_tolerance": "未注公差按 GB/T 1804-m",
        "roughness": "",
        "hole_callouts": False,
        "center_marks": False,
        "views": ["front", "isometric"],
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
    # Report 2026-11-09 (A): a partial run had `ok:false, verdict:"partial"` and was still
    # described as nothing at all (`callout_warning: null`). The caller cannot fix what the
    # receipt does not mention.
    if callouts.get("verdict") == "partial":
        skipped = callouts.get("excluded_diameters_mm") or []
        return ("孔标注只完成了一部分：%d 条已创建、%d 条被拒；%s 因倒角配对无法判定而未标注。"
                "这些直径若含主孔，图纸不能直接交付 —— **请在 SolidWorks 里手工标**。"
                "两条自动路径都试过都不对：`add_hole_callout` 会把贯通孔写成盲孔，"
                "`add_drawing_dimension` 的尺寸文字渲染不出来。"
                % (created, callouts.get("failed_count") or 0,
                   "、".join("Ø%s" % d for d in skipped) or "部分直径"))
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
                    overwrite=False, projection="first", close_when_done=True):
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
                # Report 2026-11-08 (P2-2): the hint said "Close the drawing in SolidWorks
                # and retry" while the actual failure was
                #
                #   [safe-delete] 操作失败 ... Error during a `trash` operation
                #
                # — a policy refusal on the RECYCLE BIN, with the drawing not open at all.
                # A hint that names the wrong cause sends the caller to fix something that
                # is not broken, so the two cases are separated here and the underlying
                # text is passed through instead of being replaced by advice.
                _text = str(exc)
                _trash = ("trash" in _text.lower() or "safe-delete" in _text.lower()
                          or "recycle" in _text.lower())
                return error(
                    "output_trash_failed" if _trash else "output_locked",
                    "cannot overwrite %s: %s" % (_exists_at, exc),
                    ("This is the DELETE failing, not the drawing being open: whatever "
                     "removes files here (the recycle bin / a safe-delete policy) refused. "
                     "Remove the file yourself, or pass a different output_path."
                     if _trash else
                     "The drawing is open in SolidWorks. Close it and retry, or pass a "
                     "different output_path."),
                    underlying=str(exc)[:200])

    steps = []

    def record(name, payload, detail=""):
        # ── partial is its own state (report 2026-11-09, core problem A) ────────────────
        # `add_hole_callouts` reports `ok:false, verdict:"partial"` when it had to skip a
        # chamfer-ambiguous diameter. Recording that as a plain failure lost the reason
        # (`detail` came from `payload["error"]`, and a partial payload has none — the step
        # showed `ok:false, detail:null`), while the top level still said `ok:true`.
        #
        # Now the step carries `status`, and `detail` falls back to `partial_reason`, so a
        # reader can tell "6 of 7 callouts failed and 2 diameters were skipped" from
        # "this step crashed".
        _ok = bool(payload.get("ok")) if isinstance(payload, dict) else bool(payload)
        _verdict = payload.get("verdict") if isinstance(payload, dict) else None
        _status = "ok" if _ok else ("partial" if _verdict == "partial" else "failed")
        _detail = ""
        if isinstance(payload, dict):
            _detail = detail or payload.get("partial_reason") or payload.get("error") or ""
        steps.append({"step": name, "ok": _ok, "status": _status, "detail": _detail})
        return payload if isinstance(payload, dict) else {}

    # 0.8.120: outline reads are COUNTERS, not a cache. Reset per run so the number in the
    # receipt describes this drawing, and report it so the "should we cache" question is
    # answered from data (see docs/PR3-布局缓存-不建议实施说明.md).
    # Snapshot BEFORE anything is opened. `close_document` matches purely on path, so a run
    # that closes `(target, path)` unconditionally will also close the user's own copy of
    # that part if they happen to have it open in the editor — and their unsaved edits go
    # with it. Report 2026-11-05 flagged this as a data-loss risk; the previous receipt even
    # claimed "close_document matches on the exact path this run produced", which is true and
    # irrelevant because the user's document has the same path.
    #
    # An empty set means the session could not be enumerated. In that case nothing is closed:
    # leaving a lock file behind is annoying, silently closing someone's work is not
    # recoverable.
    _preexisting = drawing_tools.open_document_paths()
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
                     drawing_tools.create_drawing(
                         path, target,
                         # Default views come from the CATEGORY, not a constant.
                         # Report 2026-11-05: the pipeline built front+right, whose right
                         # view is an END FACE — 7 circular edges, all of which
                         # AddHoleCallout2 rejects. The holes live on the TOP view, which
                         # was never created, so `callouts.created` was 0 while
                         # `defaults.hole_callouts` said True. Measured on 接槽:
                         # front+right -> scanned 7, created 0;
                         # front+top+isometric -> scanned 30, created 13.
                         views=views or defaults.get("views") or ["front", "top", "isometric"],
                                                  paper=paper, projection=projection),
                     "")
    if not created.get("ok"):
        return error("create_failed", created.get("message") or "view creation failed",
                     "Drawing not produced; see create_drawing errors.")

    # Report 2026-11-06 (P1, second round on this): `create_drawing` already computes
    # compliance notes, a measured `layout`, the scale it chose and every auto-fit
    # adjustment — and this pipeline read ONE field of it (`ok`) and threw the rest away.
    # Measured: `create_drawing(views=["front","right"], projection="first")` returns a
    # projection_notes entry about GB/T 14692; `produce_drawing` with the same arguments
    # returns `warnings: []`. The one-click entry point is what most callers use, so the
    # compliance hint was 100% lost there.
    creation_warnings = []
    for _note in (created.get("projection_notes") or []):
        creation_warnings.append({"issue": "projection_note", "detail": str(_note)})
    _lay = created.get("layout") or {}
    if _lay.get("fits") is False:
        creation_warnings.append({
            "issue": "layout_does_not_fit",
            "detail": ("views leave the safe area or overlap: out_of_bounds=%s overlaps=%s"
                       % (_lay.get("out_of_bounds"), _lay.get("overlaps")))})
    if created.get("auto_scale_adjustments"):
        creation_warnings.append({
            "issue": "scale_was_reduced",
            "detail": ("auto_fit changed the scale: %s — the title block must agree, or the "
                       "shop measures at the wrong ratio"
                       % (created.get("auto_scale_adjustments"),))})

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
        _dims_res = drawing_tools.auto_dimension(target, all_views=True,
                                                 dimension_filter=keep_list,
                                                 save=False, audit=False)
        dims = record("dimensions", _dims_res,
                      # Carry the failure reason into the step. `critical_step_failed` only
                      # names the step, so a report read "dimensions failed; the drawing is
                      # not usable" with no way to tell why — three smoke items failed with
                      # that exact sentence and nothing else.
                      "" if _dims_res.get("ok") else
                      "%s: %s" % (_dims_res.get("error"),
                                  str(_dims_res.get("message"))[:160]))
    else:
        _dims_res = drawing_tools.auto_dimension(target, all_views=True, save=False, audit=False)
        dims = record("dimensions", _dims_res,
                      "" if _dims_res.get("ok") else
                      "%s: %s" % (_dims_res.get("error"),
                                  str(_dims_res.get("message"))[:160]))

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
    # Close what this run opened. Report 2026-11-05 (old backlog): the pipeline left the part
    # and the drawing open, so every run deposited a `~$*.SLDDRW` lock file and the NEXT run
    # could not open the same name — `OpenDoc6 returned null with errors=65536`, the exact
    # failure that made a smoke item fail on 2026-11-04 and that a reader would have blamed on
    # the part instead of on the previous run.
    #
    # Closing is the correct default for a tool: it should not leave documents behind for the
    # caller to find and clean up. `close_when_done=False` keeps them open for inspection.
    # Neither branch touches anything the caller already had open — `close_document` matches
    # on the exact path this run produced.
    closed = []
    skipped_open_by_caller = []
    if close_when_done:
        # Tri-state, and the three cases must read differently (report 2026-11-05, P2-1):
        #   None        enumeration FAILED -> close nothing, and say that we could not tell
        #   set()       empty session      -> nothing belonged to a caller, close freely
        #   set(paths)  those were open    -> skip exactly those
        #
        # The old code tested `if not _preexisting:` for both None and set(), printed
        # "未关闭任何文档" in BOTH cases, and then went on to close them anyway — the
        # sentence and the behaviour disagreed, and `close_documents` counted the
        # enumeration failure as "1 skipped as caller-owned".
        _enum_failed = _preexisting is None
        if _enum_failed:
            skipped_open_by_caller.append(
                "无法枚举会话内已打开的文档（GetDocuments 失败），因此本次不关闭任何文档"
                "——宁可留下锁文件，也不误关调用者正在编辑的图纸。")
        for _doc_path in ([] if _enum_failed else (target, path)):
            _norm = os.path.normcase(os.path.normpath(str(_doc_path)))
            if _preexisting and _norm in _preexisting:
                skipped_open_by_caller.append(
                    "%s 在本次运行开始前就已打开，判定属于调用者，不关闭。" % _doc_path)
                continue
            try:
                _res = drawing_tools.close_document(_doc_path)
                # `close_document` answers ok=True even when it found nothing, so ok alone
                # cannot distinguish "closed 2" from "closed 0". The real count comes from
                # `closed_count`, which the old receipt threw away.
                closed.append({"document": _doc_path,
                               "ok": bool(_res.get("ok", False)),
                               "closed_count": int(_res.get("closed_count") or 0),
                               "closed_titles": _res.get("closed") or []})
            except Exception as exc:
                closed.append({"document": _doc_path, "ok": False, "closed_count": 0,
                               "error": str(exc)[:80]})
        _actually_closed = sum(c.get("closed_count", 0) for c in closed)
        record("close_documents",
               {"ok": _actually_closed > 0 or not closed,
                "actually_closed": _actually_closed},
               "closed %d document(s); %d skipped as caller-owned"
               % (_actually_closed, len(skipped_open_by_caller)))

    if _failed_critical:
        return error(
            "critical_step_failed",
            "%s failed; the drawing is not usable" % ", ".join(_failed_critical),
            # Include the DETAIL of each failed step, not just its name: three smoke items
            # failed for two days with "dimensions failed; the drawing is not usable" and
            # nothing else, because the reason lived in `steps[].detail` which this branch
            # never returned.
            "steps: %s | %s" % (
                "; ".join("%s=%s" % (st["step"], st["ok"]) for st in steps),
                "; ".join("%s: %s" % (st["step"], st.get("detail"))
                          for st in steps if not st.get("ok") and st.get("detail"))
                or "no detail recorded"),
            documents_closed=closed)

    # ── top-level `ok` must not contradict a partial step (report 2026-11-09, A) ────────
    # `_CRITICAL` (create/dimensions/save) deliberately excludes hole callouts, so a sheet
    # with skipped diameters reported `ok:true` — and a batch pipeline keyed on `ok` shipped
    # it. The report asked for one of two behaviours, explicitly not "top true / step false".
    #
    # Chosen: `ok:false` whenever any step is partial, with `partial_steps` naming them.
    # `verdict` is untouched — that is the delivery judgement and it comes from
    # `delivery_check`, not from whether every callout landed.
    _partial_steps = [st["step"] for st in steps if st.get("status") == "partial"]
    return ok(
        ok=(not _partial_steps),
        partial_steps=_partial_steps,
        model=path,
        drawing=target,
        category=resolved_category,
        paper=paper,
        # The audit is already run for its own step; its detail was previously thrown away, so
        # a caller could not see which views exist or what the audit warned about without
        # running drawing_audit again. None of that is free (2 view reads), so surface it.
        outline_reads=drawing_tools.outline_stats(),
        views=audit.get("views"),
        # Both sources: what the audit measured on the finished sheet, and what
        # `create_drawing` reported while building it (projection compliance, layout fit,
        # auto-fit scale changes). Neither is complete alone.
        warnings=(audit.get("warnings") or []) + list(creation_warnings),
        creation_warnings=creation_warnings,
        view_count=audit.get("view_count"),
        steps=steps,
        documents_closed=closed,
        documents_skipped_open_by_caller=skipped_open_by_caller,
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
                  "elapsed_s": callouts.get("elapsed_s"),
                  # Report 2026-11-09 (A): `add_hole_callouts` computes `verdict`,
                  # `partial_reason` and `excluded_diameters_mm` for exactly this reader and
                  # all three were dropped here. The sub-dict is what a pipeline consumer
                  # actually sees, so the omission made the detailed receipt unreachable.
                  "ok": callouts.get("ok"),
                  "verdict": callouts.get("verdict"),
                  "partial_reason": callouts.get("partial_reason"),
                  "excluded_diameters_mm": callouts.get("excluded_diameters_mm"),
                  "ambiguous_chamfer_pairs": (callouts.get("ambiguous_chamfer_pairs") or [])[:5]},
        callout_warning=_callout_warning(callouts),
        # Plan says "keep N"; auto_dimension inserts every dimension marked for drawing.
        # The gap is real and stated rather than silently reconciled — pruning inserted
        # dimensions would need delete APIs that are riskier than leaving them.
        dimension_gap=(None if keep_dimensions is None else {
            "planned": keep_dimensions,
            "inserted": (summary or {}).get("dimension"),
            "pruned_to_plan": bool(prune_to_plan),
            # `kind == "derived"` entries are BOUNDING-BOX values (总长/总宽/总高). There is
            # no driving dimension behind them in the model, so `auto_dimension` — which
            # imports model dimensions — can never insert one. Report 2026-11-05: the gap
            # listed 总长 107 as "外形尺寸，任何图纸都必须标注", not one of them reached the
            # sheet, and the note still read "规划已生效" while every step said ok. The
            # drawing therefore had no overall length at all — unusable for cutting stock.
            #
            # They are separated out here because the fix is different: they need
            # `add_distance_dimension` between two edges, not a filter tweak. Saying "the
            # plan took effect" without that qualifier is the part that misled.
            # `keep_dimensions` above is the COUNT (planned: 9). The dicts are `keep_list`.
            "derived_unfilled": [d for d in keep_list
                                 if str(d.get("kind") or "").lower() == "derived"],
            "note": (
                # Report 2026-11-06 (P2): this sentence was unconditional — it said "the
                # plan took effect" whenever `prune_to_plan` was true, without looking at
                # what actually landed. Measured: `planned 9 / inserted 12`. A receipt that
                # announces success it has not checked is the same failure as a save that
                # did not save. State the numbers instead.
                (("规划已生效（prune_to_plan=True）：计划 %s 条，实际插入 %s 条%s。"
                  % (len(keep_list), ((summary or {}).get("dimension") or 0),
                     "（插入多于计划：`_filter_inserted_dimensions` 按值保留，"
                     "同一数值的多条尺寸会一起保留，这是有意的兜底）"
                     if ((summary or {}).get("dimension") or 0) > len(keep_list) else ""))
                 if prune_to_plan else
                 ("规划未生效（prune_to_plan=False）：实际导入的是模型全部标记为工程图用的尺寸"
                  "（%s 条），需人工删减或按需补标。" % ((summary or {}).get("dimension") or 0)))
                + ("filter 失败/未命中时不会删除，见 dimensions 步骤的 filter_aborted。"
                   if prune_to_plan else "")
                + ((" 另有 %d 条 derived 项（外形总长/总宽/总高）不在插入路径上——"
                    "模型里没有对应的驱动尺寸，auto_dimension 插不进去，"
                    "需用 add_distance_dimension 在两视图边之间补标：%s。"
                    % (len([d for d in keep_list
                            if str(d.get("kind") or "").lower() == "derived"]),
                       "、".join("%s=%s" % (d.get("dimension"), d.get("value_mm"))
                                for d in keep_list
                                if str(d.get("kind") or "").lower() == "derived")))
                   if any(str(d.get("kind") or "").lower() == "derived"
                          for d in keep_list) else "")
            ),
        }),
        annotations=summary,
        layout_warnings=layout_warnings,
        coverage=recon.get("coverage"),
        # Report 2026-11-08 (P1): `delivery_check` crashed (a bad `record()` call), returned
        # an ERROR dict, and this line read `verdict.get("verdict")` -> None, `blockers` ->
        # [], `pending` -> []. The receipt then said `ok: true` with empty blockers, which
        # reads as "deliverable" when in fact NO delivery judgement was ever made.
        #
        # A failed check is not a pass. If the step did not run, say so in the verdict and
        # put the reason where a reviewer looks. `ok` keeps its existing meaning (a failed
        # step does not abort the pipeline), but the three delivery fields can no longer be
        # silently empty.
        verdict=(
            verdict.get("verdict") if verdict.get("ok")
            else "未判定：delivery_check 执行失败（%s）"
            % (str(verdict.get("message") or verdict.get("error") or "unknown")[:120],)
        ),
        blockers=([b.get("detail") for b in (verdict.get("blockers") or [])]
                  if verdict.get("ok") else
                  ["delivery_check 未完成，交付判定丢失：%s"
                   % (str(verdict.get("message") or verdict.get("error") or "unknown")[:120],)]),
        pending=[p.get("detail") for p in (verdict.get("pending") or [])]
                # Report 2026-11-06 (P3): a projection that violates GB/T 14692 went into
                # `creation_warnings` only, so the verdict stayed "待设计审核" with an empty
                # `pending` list. A compliance defect is not a hint — it is a reason the
                # drawing is not ready. Surfaced here so it appears where a reviewer looks.
                + ["GB/T 14692 投影不合规：%s" % w.get("detail")
                   for w in creation_warnings if w.get("issue") == "projection_note"]
                # Report 2026-11-09 (A): the callout step already knew which diameters it
                # could not annotate; `pending` is where a reviewer looks, and it said
                # nothing about them.
                + (["孔标注缺 %d 个直径：%s（倒角配对无法判定；可能是主孔）"
                    % (len(callouts.get("excluded_diameters_mm") or []),
                       "、".join("Ø%s" % d
                                for d in (callouts.get("excluded_diameters_mm") or [])))]
                   if (callouts.get("verdict") == "partial"
                       and callouts.get("excluded_diameters_mm")) else []),
        note=("流水线按类别套默认值；defaults_applied 里写明了假定什么。"
              "某一步失败不会中断后续步骤，逐条看 steps。"),
    )
