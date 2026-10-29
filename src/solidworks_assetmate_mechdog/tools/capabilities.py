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
        "id": "view_layout",
        "status": "partial",
        "summary": "视图摆位与比例",
        "why": ("`create_drawing(view_positions=...)` 可按图纸米坐标显式摆位，`set_view` / `place_views` "
                "可事后移动缩放；自动布局在 4 视图以上会退回单列"),
        "workaround": "多视图请显式给 view_positions；auto_fit 不再覆盖调用方指定的 scale",
        "evidence": "2026-10-28 实测 4 视图 2x2：layout {fits: True, columns: 4, explicit: True}",
    },
    {
        "id": "dimension_edit",
        "status": "partial",
        "summary": "尺寸删除与筛选",
        "why": "`delete_dimensions` 按值保留/删除；SolidWorks 无批量删除接口，逐条约 15 s",
        "workaround": "先用 plan_drawing 拿短名单再删；尺寸很多时分批做",
        "evidence": "2026-10-28 实测 17 条 → 2 条，removed 15 / failed 0",
    },
    {
        "id": "annotation_write",
        "status": "partial",
        "summary": "注记 / 件号 / 图纸直线",
        "why": "`add_note` / `add_balloon` / `add_sheet_line` 可用；图纸草图单位是**米**不是毫米",
        "workaround": "传毫米会得到满屏放射斜线（坐标被当模型空间并乘视图比例）",
        "evidence": "2026-10-28 实测三个工具均 ok",
    },
    {
        "id": "bom_table",
        "status": "partial",
        "summary": "装配明细栏（BOM 表）",
        "why": ("`IView.InsertBomTable4` 真实签名有 **10 个参数**（官方 2024 文档），"
                "第一个是 `UseAnchorPoint`(bool) 不是模板路径——按 6 参数调用必然 "
                "DISP_E_TYPEMISMATCH。改用正确签名后不再报错，但**返回 None**："
                "BomType=0/1/2、模板路径已确认存在（安装目录 lang\\chinese-simplified\\"
                "bom-standard.sldbomtbt）、保存图纸后重试，结果一致。"
                "`InsertBomTable5`（11 参数，2024 FCS）与 `InsertTableAnnotation2`（7 参数）"
                "同样全部返回 None；已试 BomType 0-4、空/真实模板、视图激活/EditSheet/"
                "ActivateSheet/存盘后插、UseAnchorPoint 两种取值，共 7 条 API × 20+ 组合，"
                "无一成功。返回 None 而非报错 = 调用被接受、创建被静默拒绝，"
                "最可能是文档属性禁用或模板格式不兼容。"),
        "workaround": ("用 add_bom_table（宏驱动）可生成原生 BOM；宏按固定视图名与位置录制，"
                       "视图名不符时可能不出现"),
        "evidence": "2026-10-28 实测 10 种参数组合全部类型不匹配",
    },
    {
        "id": "sheet_sketch_space",
        "status": "partial",
        "summary": "图纸草图坐标系",
        "why": "`add_sheet_line` 按米定位可用；细表格布线仍需多次调用",
        "workaround": "先算好每段线的米坐标，批量调用",
        "evidence": "2026-10-28 实测 ok（未做整表验证）",
    },
    {
        "id": "sheet_metal_qc",
        "status": "partial",
        "summary": "钣金工艺检查",
        "why": ("板厚 / 弯折数 / 止裂槽有无可判；最小折弯边、孔到折弯线、折弯半径需要实体几何"
                "距离计算，未实现，返回 not_checked"),
        "workaround": "not_checked 的项按 not_checked 处理，不要当作通过",
        "evidence": "2026-10-28 实测箱体 3 道弯折无止裂槽 → blocker",
    },
    {
        "id": "flat_pattern_calc",
        "status": "unsupported",
        "summary": "展开计算与校验",
        "why": ("展开由 SolidWorks 钣金引擎计算，工具只投影展平状态；K 因子与折弯表读不到"
                "（GetCustomBendAllowance 返回空）"),
        "workaround": "用 flat_pattern_report 拿 expected_note 的措辞，工艺参数由厂内折弯表定",
        "evidence": "2026-10-28 实测 computed_by_tool=false",
    },
    {
        "id": "hole_callout",
        "status": "pilot",
        "summary": "原生孔标注",
        "why": ("走「视图可见边 + Select2 + AddHoleCallout2」。SW2026 上**不弹模态对话框**"
                "（官方 Remarks 过时），单视图 13 条圆边 12 秒完成；"
                "但只有朝向观察者的孔边可标——同一孔的背面边返回 null"),
        "workaround": ("背面/被遮挡的孔如实记 failed 并由 Ø 尺寸补；"
                       "通风百叶等非孔圆不产生标注"),
        "evidence": ("2026-10-28 实测 door.SLDPRT：5/13 成功（8 X 18 ×2、Ø15 贯穿 ×3，文字完整、"
                     "引线自动引出），失败 8 个全为 Ø6 背面边（z=6.0）"),
    },
    {
        "id": "hole_callout_batch",
        "status": "pilot",
        "summary": "批量孔标注",
        "why": "按孔去重（同圆心同径只标一次），单视图 25 秒预算，超时返回 budget_exceeded",
        "workaround": "多孔零件按 plan_drawing 的配额标注，同规格孔只标一处写 n×",
        "evidence": "2026-10-28 实测",
    },
    {
        "id": "geometric_tolerance",
        "status": "pilot",
        "summary": "形位公差框格",
        "why": ("**可以做**（2026-10-28 突破）。此前判定「不可创建」是调用方式错误："
                "pywin32 把 InsertGtol 当作属性派发（PROPERTYGET），必然报 "
                "DISP_E_MEMBERNOTFOUND。改用 ole.Invoke(dispid, 0, DISPATCH_METHOD, True) "
                "即可拿到 Gtol 对象，SetFrameValues2 写入公差与基准，回读确认。"
                "局限：框架内的公差**符号**（平面度/位置度等）用格式码 <MOD-*-PAR> 未生效"),
        "workaround": ("用 add_geometric_tolerance 创建框格（公差值/基准可靠）；"
                       "符号需界面补，或后续找到正确的符号写入方式"),
        "evidence": ("2026-10-28 实测 door.SLDPRT：插入 2 个框格，回读 "
                     "frame1=['0.05','','A','',''] 与 ['0.1','','B','C','']，位置正确，"
                     "list_drawing_annotations 计到 geometric_tolerance: 2"),
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
