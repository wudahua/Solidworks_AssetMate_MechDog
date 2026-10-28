"""Declare capability limits BEFORE the work starts.

The failure this prevents: `create_drawing` runs for 400 seconds on a hole-heavy sheet,
then `delivery_check` reports "0 hole callouts / 80 cylindrical faces". The caller spent the
time and got nothing. A preflight answer costs nothing.

Every entry states what is unsupported, why (with evidence), and what to do instead. This
is deliberately a hard-coded table rather than a probe: probing SolidWorks for a capability
it silently ignores is exactly how the 400-second wait happened.
"""

# status: "supported" | "partial" | "unsupported"
CAPABILITIES = [
    {
        "id": "hole_callout",
        "status": "unsupported",
        "summary": "原生孔标注",
        "why": ("InsertModelAnnotations3 的孔标注位掩码无效：在含 M6 螺纹孔(Hole Wizard)的零件上，"
                "传 32768|0x100000 与只传 32768 得到的标注类型完全相同（{4:12, 6:82}）；"
                "按圆边定位的路线在 260 条边的视图上超过 400 秒"),
        "workaround": "用 add_drawing_dimension 在圆边上标 Ø 尺寸，或界面手工标注",
        "evidence": "2026-10-28 实测，配电箱 + SAB1_0101_002(M6 螺纹孔)",
    },
    {
        "id": "geometric_tolerance",
        "status": "unsupported",
        "summary": "形位公差框格",
        "why": ("ModelDoc2.InsertGtol 报 DISP_E_MEMBERNOTFOUND；"
                "IModelDocExtension.CreateGtolData / InsertGtol 成员不存在（SW2024）"),
        "workaround": "界面手工添加；读取不受影响（list_drawing_annotations 可读）",
        "evidence": "2026-10-28 实测，IDrawingDoc/ISwDrawingDoc QueryInterface 两条路径均失败",
        "note": "DISP_E_MEMBERNOTFOUND 只说明分派层找不到成员，不等于 API 必然不存在——"
                "早绑定诊断仍未做，结论保留",
    },
    {
        "id": "detail_view_scale",
        "status": "partial",
        "summary": "局部放大比例",
        "why": "局部视图默认 UseParentScale，创建时设置 ScaleDecimal 会被吞掉",
        "workaround": "返回值带 scale_locked 标记；需要非父视图比例时保存重开后再设一次",
        "evidence": "2026-10-28 实测",
    },
    {
        "id": "assembly_bounding_box",
        "status": "partial",
        "summary": "装配体包围盒",
        "why": "IAssemblyDoc 没有 GetPartBox；组件 GetBox 在轻化状态下返回 None",
        "workaround": "比例由实测安全区推算，建图后测量越界再降档",
        "evidence": "2026-10-28 实测，1.6m 条码设备靠该方法得到 1:20",
    },
    {
        "id": "dxf_import",
        "status": "unsupported",
        "summary": "DXF/DWG 逆向建模",
        "why": "未实现，且与建模类工具重叠",
        "workaround": "用 SolidPilot 的 analyze_drawing",
        "evidence": "设计决策",
    },
    {
        "id": "geometry_creation",
        "status": "unsupported",
        "summary": "建模/特征创建",
        "why": "本工具定位是出图与自检，不做几何创建",
        "workaround": "用建模类工具（如 solidpilot）",
        "evidence": "设计决策",
    },
]

STATUS_LABEL = {"supported": "可做", "partial": "有限支持", "unsupported": "做不了"}


def check_capabilities(operation=None):
    """Return what this toolkit can and cannot do, optionally filtered to one operation.

    Call this before a long pipeline. It answers in milliseconds what would otherwise cost
    a full SolidWorks run to discover.
    """
    from ..sw_session import error, ok

    entries = CAPABILITIES
    if operation:
        key = str(operation).strip().lower()
        entries = [c for c in CAPABILITIES
                   if key in c["id"].lower() or key in c["summary"]]
        if not entries:
            return error("unknown_operation",
                         "no capability entry matches %r" % operation,
                         "Known ids: %s" % ", ".join(c["id"] for c in CAPABILITIES))

    blocked = [c for c in entries if c["status"] == "unsupported"]
    partial = [c for c in entries if c["status"] == "partial"]
    payload = []
    for item in entries:
        row = dict(item)
        row["status_label"] = STATUS_LABEL.get(item["status"], item["status"])
        payload.append(row)

    return ok(
        operation=operation,
        checked=len(entries),
        blocked=[c["id"] for c in blocked],
        partial=[c["id"] for c in partial],
        capabilities=payload,
        advice=("有 %d 项做不了。调用前请先看 workaround，不要跑完流程才发现。"
                % len(blocked)) if blocked else "全部可做。",
    )
