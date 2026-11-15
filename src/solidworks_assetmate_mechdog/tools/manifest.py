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

    cov = _as_dict(payload.get("dimension_coverage"))
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
                           dimension_coverage=None):
    """Write the manifest and return its path plus the verdict it carries.

    Read-only with respect to the drawing: nothing is opened, saved or modified — the
    numbers come from `delivery_check`, which already reads both files.
    """
    from . import drawing as drawing_tools

    drawing_abs = os.path.abspath(drawing_path)
    model_abs = os.path.abspath(model_path)
    if not os.path.exists(drawing_abs):
        return error("bad_input", "drawing not found: %s" % drawing_abs)
    if not os.path.exists(model_abs):
        return error("bad_input", "model not found: %s" % model_abs)

    check = drawing_tools.delivery_check(model_abs, drawing_abs,
                                         include_faces=bool(include_faces))
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
        "dimension_coverage": dimension_coverage,
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
        note=("清单一页：结论与检查项来自 delivery_check，不新增判断；"
              "「尺寸覆盖（计划）」只在调用方传入 dimension_coverage 时有数字，"
              "否则明写「无」；「未检查」不等于通过。"),
    )
