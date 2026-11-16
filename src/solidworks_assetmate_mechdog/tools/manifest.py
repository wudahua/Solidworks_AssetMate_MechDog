"""Delivery manifest — the one page you can file with the drawing.

`delivery_check` answers "can this drawing be delivered" in a JSON receipt. A receipt is
fine for a program and useless in a document pack: a shop folder wants one page that says
what was checked, what is still open, and what the numbers were.

This module turns that receipt into Markdown. It deliberately adds NO new judgement — every
line comes from `delivery_check` (and optionally `sheet_metal_qc`). If the manifest and the
tool ever disagree, the tool is right; the renderer is pure so that can be tested.
"""
from __future__ import annotations

import json
import os
import time

from ..sw_session import error, ok


def _esc(value):
    """Markdown table cell: no pipes, no newlines."""
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_esc(c) for c in row) + " |")
    return "\n".join(out)


#: The shape `auto_dimension.dimension_coverage` really has (drawing.py). Anything else is
#: not "coverage" — it is a caller's guess, and a delivery page must not print guesses.
_COVERAGE_INT_FIELDS = ("matched_by_name", "matched_by_value")


def normalise_dimension_coverage(value):
    """`(clean, status, reasons)` for a caller-supplied coverage object. PURE.

    0.8.347 (report 2026-11-16 v0.8.346 §4, P0): `dimension_coverage` reached MCP with NO
    structural check. `render_manifest` then did `.get()` / `float()` on whatever arrived, so:

        {"foo": 1}                  -> "计划 None 条，落到图上 0 条（覆盖 0%）。"  (fabricated)
        {"missing": ["总长"]}        -> AttributeError: 'str' object has no attribute 'get'
        {"coverage_ratio": "75%"}   -> ValueError: could not convert string to float

    A receipt that says `ok: true` while the page carries invented numbers is the exact
    failure this project keeps removing, and an exception that escapes to MCP leaves the
    client with no `call_status` at all.

    Three outcomes, named:

      "absent"            nothing supplied (or an empty object) -> section says where the
                          number lives. This is the honest default, not an error.
      "rejected_invalid"  something was supplied and it does not fit the shape -> the
                          section states the input was rejected and why. NEVER numbers.
      "accepted"          the shape matches -> `clean` is a fresh dict with only the known
                          keys, so the page cannot be steered by extra ones.
    """
    if value is None:
        return None, "absent", []
    if not isinstance(value, dict):
        return None, "rejected_invalid", [
            "dimension_coverage 必须是对象（收到 %s）" % type(value).__name__]
    if not value:
        return None, "absent", []

    reasons = []
    planned = value.get("planned")
    if isinstance(planned, bool) or not isinstance(planned, int):
        reasons.append("planned 必须是整数（收到 %r）" % (planned,))
    elif planned < 0:
        reasons.append("planned 不能为负（收到 %r）" % (planned,))

    ratio = value.get("coverage_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        reasons.append("coverage_ratio 必须是 0–1 的数字（收到 %r）" % (ratio,))
    elif not (0.0 <= float(ratio) <= 1.0):
        # 75 is the percentage, not the ratio. Printing "覆盖 7500%" is not a rounding
        # problem, it is a unit error the caller can fix in one place.
        reasons.append("coverage_ratio 是 0–1 的比例，不是百分数（收到 %r）" % (ratio,))

    for key in _COVERAGE_INT_FIELDS:
        got = value.get(key, 0)
        if isinstance(got, bool) or not isinstance(got, int):
            reasons.append("%s 必须是整数（收到 %r）" % (key, got))

    missing = value.get("missing")
    if missing is None:
        missing = []
    if not isinstance(missing, (list, tuple)):
        reasons.append("missing 必须是数组（收到 %s）" % type(missing).__name__)
        missing = []
    else:
        bad = [i for i, row in enumerate(missing) if not isinstance(row, dict)]
        if bad:
            reasons.append("missing 的第 %s 项不是对象"
                           % "、".join(str(i) for i in bad[:3]))

    if reasons:
        return None, "rejected_invalid", reasons

    clean = {
        "planned": planned,
        "matched_by_name": value.get("matched_by_name", 0),
        "matched_by_value": value.get("matched_by_value", 0),
        "coverage_ratio": float(ratio),
        "missing": [dict(row) for row in missing],
    }
    return clean, "accepted", []


def render_manifest(payload):
    """Markdown for a delivery manifest. PURE — takes a dict, returns a string.

    Keys read (all optional except `verdict`): `generated_at`, `tool_version`, `model`,
    `drawing`, `verdict`, `ok`, `blockers`, `pending`, `checks`, `facts`, `sheets`,
    `note`, `sheet_metal`, `dimension_coverage`.

    Sections are fixed on purpose: a manifest that changes shape per drawing cannot be
    filed next to its predecessors. Empty sections still appear, with 「无」 — a missing
    section reads as "not checked", which is a different claim.
    """
    payload = payload if isinstance(payload, dict) else {}

    def _as_dict(value):
        # 0.8.345 (report 2026-11-15 v0.8.344 §P1-1/§P2): a public "PURE — takes a dict"
        # function used to raise AttributeError on `checks: list` or `blockers: ["str"]`.
        # Production passes dicts, but a manifest is exactly the thing third parties build
        # by hand, so coerce instead of exploding.
        return value if isinstance(value, dict) else {}

    def _as_rows(value):
        rows = value if isinstance(value, (list, tuple)) else []
        return [r if isinstance(r, dict) else {"detail": r} for r in rows]

    lines = []
    verdict = payload.get("verdict") or "未判定"
    lines.append("# 交付清单")
    lines.append("")
    lines.append("**结论：%s**" % verdict)
    lines.append("")
    lines.append(_table(
        ["项目", "值"],
        [("模型", payload.get("model") or "—"),
         ("图纸", payload.get("drawing") or "—"),
         ("生成时间", payload.get("generated_at") or "—"),
         ("工具版本", payload.get("tool_version") or "—"),
         ("回执 ok", "true" if payload.get("ok") else "false")]))
    lines.append("")

    def _section(title, body):
        lines.append("## %s" % title)
        lines.append("")
        lines.append(body if body else "无")
        lines.append("")

    blockers = _as_rows(payload.get("blockers"))
    _section("阻断项", _table(["检查", "说明"],
                              [(b.get("check") or b.get("name"), b.get("detail"))
                               for b in blockers]) if blockers else "无（没有阻断交付的问题）")

    pending = _as_rows(payload.get("pending"))
    _section("待确认（不阻断，但需人看一眼）",
             _table(["检查", "说明"],
                    [(p.get("check") or p.get("name"), p.get("detail"))
                     for p in pending]) if pending else "无")

    checks = _as_dict(payload.get("checks"))
    rows = []
    for name, item in checks.items():
        if not isinstance(item, dict):
            rows.append((name, "", item))
            continue
        # Order matters: a `not_checked` entry is `ok: true` (nothing failed), and calling
        # that "通过" is the exact lie this project keeps removing.
        if item.get("status") == "not_checked":
            state = "未检查"
        elif item.get("ok"):
            state = "通过"
        else:
            state = "未通过"
        rows.append((name, state, item.get("detail")))
    _section("检查项", _table(["检查", "结果", "说明"], rows) if rows else "无")

    facts = _as_dict(payload.get("facts"))
    _section("实测数据", _table(["指标", "值"],
                              [(k, json.dumps(v, ensure_ascii=False)
                                if isinstance(v, (list, dict)) else v)
                               for k, v in facts.items()]) if facts else "")

    # 0.8.347 (report 2026-11-16 v0.8.346 §4): validate, never trust. Every branch below
    # is reachable from `tools/call`, where the client writes the dict by hand.
    cov, cov_status, cov_reasons = normalise_dimension_coverage(
        payload.get("dimension_coverage"))
    if cov:
        body = ["计划 %s 条，落到图上 %s 条（覆盖 %s%%）。" % (
            cov.get("planned"), (cov.get("matched_by_name") or 0)
            + (cov.get("matched_by_value") or 0),
            round(float(cov.get("coverage_ratio") or 0) * 100))]
        missing = cov.get("missing") or []
        if missing:
            body.append("")
            body.append(_table(["未落图的尺寸", "数值 mm", "原因", "补标路径"],
                               [((m.get("dimension") or ""), m.get("value_mm"),
                                 m.get("reason"), m.get("hint")) for m in missing]))
        _section("尺寸覆盖（计划）", "\n".join(body))
    elif cov_status == "rejected_invalid":
        # Showing numbers here is how a delivery page lies. Say the input was refused and
        # list the reasons, so the caller can fix the object instead of trusting the page.
        _section("尺寸覆盖（计划）",
                 "无 —— 调用方传入的 `dimension_coverage` 结构不合法，已拒绝，"
                 "本页不输出任何覆盖率数字。\n\n"
                 + "\n".join("- %s" % r for r in cov_reasons)
                 + "\n\n正确来源：`auto_dimension` 回执的 `dimension_coverage`，"
                   "或 `produce_drawing` 的 `dimension_gap.coverage`。")
    else:
        # 0.8.345 (report §P1-1): this section used to vanish while the receipt's `sections`
        # claimed it. `delivery_check` has NO plan-level coverage — the number lives in
        # `auto_dimension.dimension_coverage` — so say that instead of implying a check
        # that never ran.
        _section("尺寸覆盖（计划）",
                 "无 —— `delivery_check` 不提供计划级覆盖；数字在 `auto_dimension` 的 "
                 "`dimension_coverage` 里（`produce_drawing` 的 `dimension_gap.coverage` 会带出）。"
                 "台账覆盖率见「待确认」中的 `ledger_reconciled`。")

    sheets = _as_rows(payload.get("sheets"))
    _section("页面", _table(["页", "图幅 mm", "视图", "尺寸", "问题"],
                           [(s.get("name"),
                             "×".join("%.0f" % (v * 1000) for v in (s.get("size_m") or []))
                             if s.get("size_m") else "",
                             s.get("view_count"), s.get("dimensions"),
                             "；".join(s.get("issues") or [])) for s in sheets])
             if sheets else "")

    sm = payload.get("sheet_metal")
    if not (isinstance(sm, dict) and sm.get("ok")):
        _section("钣金工艺", "无 —— 本次未要求（`include_sheet_metal=True` 才会跑，"
                              "80 面零件约 150–300 s）")
    if isinstance(sm, dict) and sm.get("ok"):
        body = [_table(["检查", "结果", "说明"],
                       [(c.get("check"),
                         {"pass": "通过", "warn": "注意", "fail": "不通过",
                          "not_checked": "未检查"}.get(c.get("status"), c.get("status")),
                         c.get("detail")) for c in (sm.get("checks") or [])])]
        holes = sm.get("hole_to_bend") or []
        if holes:
            body.append("")
            body.append("孔到折弯线（口径 2t+R）：")
            body.append(_table(["孔", "直径", "折弯", "余量 mm", "阈值 mm", "结论"],
                               [(h.get("hole_feature"), h.get("hole_diameter_mm"),
                                 h.get("bend_feature"), h.get("edge_clearance_mm"),
                                 h.get("threshold_mm"), h.get("status")) for h in holes]))
        flanges = sm.get("min_flange") or []
        if flanges:
            body.append("")
            body.append("最小折弯边（口径 4t+R，近似测量）：")
            body.append(_table(["折弯", "两侧 mm", "最短 mm", "阈值 mm", "结论"],
                               [(f.get("bend_feature"),
                                 "/".join(str(x) for x in (f.get("sides_mm") or [])),
                                 f.get("min_side_mm"), f.get("threshold_mm"),
                                 f.get("status")) for f in flanges]))
        _section("钣金工艺", "\n".join(body))

    lines.append("## 口径")
    lines.append("")
    lines.append("本清单只复述 `delivery_check`（以及可选 `sheet_metal_qc`）的结论，"
                 "不新增判断。`未检查` 表示该评价没有做，**不等于通过**。"
                 "「待设计审核」表示图纸对象正确但含需人确认的值。")
    lines.append("")
    _status = payload.get("documents_close_status")
    _closed = payload.get("documents_closed") or []
    _left = payload.get("documents_left_open") or []
    _unclosed = payload.get("documents_not_closed_newly_opened") or []

    def _names(paths):
        return "、".join(os.path.basename(str(p)) for p in paths) if paths else "无"

    lines.append(
        "会话：读取模型与图纸时会**打开**这两个文档；返回前关闭本次打开的（%d 个：%s）；"
        "调用前就已打开的（%d 个：%s）不动。%s"
        % (len(_closed), _names(_closed), len(_left), _names(_left),
           {"not_enumerated": "本次无法枚举会话文档，因此没有关闭任何文档"
                              "（不拿「看不到」当「没有」）。",
            "disabled": "调用方要求保留打开状态（`close_opened=False`）。",
            "close_failed": "关闭时出错，见 `documents_close_status`。",
            "skipped_caller_documents": "凡是有一个在调用前就已打开，本次就一个都不关 —— "
                                        "关闭其中一个会把它一起带走（实测 2026-11-16）；"
                                        "本次打开但未关闭的：%s。" % _names(_unclosed),
            "ok": "未保存、未修改任何文件。"}.get(_status, "")))
    lines.append("")
    if payload.get("note"):
        lines.append(str(payload["note"]))
        lines.append("")

    lines.append("## 原始回执")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def export_delivery_report(model_path, drawing_path, output_path=None,
                           include_faces=False, include_sheet_metal=False,
                           dimension_coverage=None, close_opened=True):
    """Write the manifest and return its path plus the verdict it carries.

    **Nothing is saved or modified.** The model and the drawing ARE opened — that is how
    `delivery_check` reads them — and closed again before returning, but only if they were
    not already open:
    `delivery_check` snapshots `open_document_paths()` before the read and closes the diff
    afterwards, so documents the user had open are never touched (closing those would
    discard unsaved edits, reported 2026-11-05).

    Report 2026-11-16 (v0.8.345 §7): the previous wording said "nothing is opened", and a
    clean session went from 0 to 2 open documents per manifest call.
    """
    from . import drawing as drawing_tools

    drawing_abs = os.path.abspath(drawing_path)
    model_abs = os.path.abspath(model_path)

    # Validate BEFORE the 30-90 s COM read: a malformed coverage object is a caller bug,
    # and failing fast keeps the receipt structured (`call_status` included) instead of
    # raising out of `render_manifest` after the drawing was already opened.
    cov, cov_status, cov_reasons = normalise_dimension_coverage(dimension_coverage)

    def _fail(code, message, hint=None, **extra):
        return error(code, message, hint,
                     documents_closed=[], documents_left_open=[],
                     documents_close_status="not_started",
                     dimension_coverage_status=cov_status, **extra)

    if cov_status == "rejected_invalid":
        return _fail("invalid_dimension_coverage",
                     "dimension_coverage 结构不合法，未写出清单：%s" % "；".join(cov_reasons),
                     "传 `auto_dimension` 回执里的 `dimension_coverage`，"
                     "或 `produce_drawing` 的 `dimension_gap.coverage`；"
                     "或干脆不传 —— 该段会写明「无」并指出数字在哪。")

    if not os.path.exists(drawing_abs):
        return _fail("bad_input", "drawing not found: %s" % drawing_abs)
    if not os.path.exists(model_abs):
        return _fail("bad_input", "model not found: %s" % model_abs)

    check = drawing_tools.delivery_check(model_abs, drawing_abs,
                                         include_faces=bool(include_faces),
                                         close_opened=bool(close_opened))
    if not check.get("ok") and check.get("error"):
        return error(check.get("error") or "delivery_check_failed",
                     check.get("message") or "delivery_check returned an error",
                     "清单没有写出来 —— 不能把一次失败的检查伪装成一份文档。",
                     delivery_check=check)

    sm = None
    if include_sheet_metal and model_abs.lower().endswith(".sldprt"):
        from . import sheet_metal as sm_tools

        sm = sm_tools.sheet_metal_qc(model_abs, include_cylinders=True)

    try:
        import solidworks_assetmate_mechdog as _pkg

        version = getattr(_pkg, "__version__", None)
    except Exception:
        version = None

    payload = {
        # 0.8.347 (report §4): the receipt says whether the numbers on the page came from a
        # valid object, were refused, or were never supplied. No need to read the Markdown.
        "dimension_coverage_status": cov_status,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool_version": version,
        "model": model_abs,
        "drawing": drawing_abs,
        "verdict": check.get("verdict"),
        "ok": check.get("ok"),
        "blockers": check.get("blockers") or [],
        "pending": check.get("pending") or [],
        "checks": check.get("checks") or {},
        "facts": check.get("facts") or {},
        "sheets": check.get("sheets") or [],
        "note": check.get("note"),
        "sheet_metal": sm,
        # Only when the caller has a plan-level number (it comes from `auto_dimension`, not
        # from `delivery_check`). Left out otherwise — the section then says so instead of
        # disappearing while the receipt claims it exists.
        "dimension_coverage": cov,
        # 0.8.346 (report 2026-11-16 v0.8.345 §7): "read-only" was true about saving and
        # false about opening. These three come straight from `delivery_check`, which now
        # closes what it opened — the manifest only reports it, so the page can be audited.
        "documents_closed": check.get("documents_closed") or [],
        "documents_closed_titles": check.get("documents_closed_titles") or [],
        "documents_left_open": check.get("documents_left_open") or [],
        # 0.8.347 (report §5.4): on `skipped_caller_documents` these are the documents this
        # call opened and did NOT close — the caller needs the list to clean up on its terms.
        "documents_not_closed_newly_opened":
            check.get("documents_not_closed_newly_opened") or [],
        "documents_close_status": check.get("documents_close_status"),
    }
    text = render_manifest(payload)

    if output_path is None:
        stem = os.path.splitext(drawing_abs)[0]
        output_path = stem + ".delivery.md"
    out = os.path.abspath(output_path)
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        return error("write_failed", str(exc)[:160], "清单未写入磁盘",
                     output_path=out)

    return ok(
        output_path=out,
        bytes=os.path.getsize(out),
        verdict=check.get("verdict"),
        delivery_ok=check.get("ok"),
        blockers=len(payload["blockers"]),
        pending=len(payload["pending"]),
        checks=len(payload["checks"]),
        includes_sheet_metal=bool(sm and sm.get("ok")),
        # 0.8.345 (report 2026-11-15 v0.8.344 §P1-1): `sections` used to be a hard-coded
        # list of ten names, two of which could never be rendered. Derive it from the text
        # we actually wrote — a receipt that describes a different file than it produced is
        # the same lie the rest of this project spent the day removing.
        sections=[ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")],
        # 0.8.346: the caller must be able to verify the session side effect without
        # opening SolidWorks. Same three fields as `delivery_check`.
        documents_closed=payload["documents_closed"],
        documents_closed_titles=payload["documents_closed_titles"],
        documents_left_open=payload["documents_left_open"],
        documents_not_closed_newly_opened=payload["documents_not_closed_newly_opened"],
        documents_close_status=payload["documents_close_status"],
        dimension_coverage_status=cov_status,
        note=("清单一页：结论与检查项来自 delivery_check，不新增判断；"
              "「尺寸覆盖（计划）」只在调用方传入 dimension_coverage 时有数字，"
              "否则明写「无」；「未检查」不等于通过。"
              "本次打开模型与图纸后已关闭（见 documents_closed），"
              "调用前就打开的文档不会被关闭。"),
    )
