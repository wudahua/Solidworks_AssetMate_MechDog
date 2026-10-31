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
        "summary": "尺寸删除、去重与单个移动",
        "why": ("`delete_dimensions` 按值保留/删除；`auto_dimension(eliminate_duplicates=True)` "
                "（0.8.19）按模型 full name 自动删除多视图重复副本，只动本次插入的尺寸、"
                "不碰用户已有尺寸；`set_dimension_position` 可把单个尺寸移到图纸坐标并回读数值。"
                "SolidWorks 无批量删除接口，实测 14 条一次调用 < 5 s（早期文档高估约 100 倍）"),
        "workaround": "先用 plan_drawing 拿短名单再删；单条微调尺寸位置用 set_dimension_position",
        "evidence": ("2026-10-29 实测 14 条一次调用 < 5 s；2026-10-28 实测 17 条 → 2 条，"
                     "removed 15 / failed 0；0.8.19 full-name dedupe + 单尺寸移动 smoke 覆盖"),
    },
    {
        "id": "balloon",
        "status": "unsupported",
        "summary": "件号圆圈（BOM balloon）",
        "why": ("`add_balloon` 的两条路径都不成立。在 SW2024 SP4 实测："
                "`InsertBOMBalloon2` 全部参数都是必需的（DISP_E_PARAMNOTOPTIONAL，"
                "传 3/4/5 参都不够，完整签名需要 9 参含 Style/Size/上标/下标/引线样式）；"
                "`InsertBOMBalloon` 参数数目无效；`InsertBalloon` **名字解析不到**"
                "（DISP_E_UNKNOWNNAME，GetIDsOfNames 直接 False）。"
                "用户反馈在 SW2026 上 `InsertBalloon` 同样是 UNKNOWNNAME，与本地一致。"),
        "workaround": ("件号暂时用 add_note 写文字 + add_sheet_line 画引线；"
                       "或界面手工插（明细栏行号本身已经正确）"),
        "evidence": ("2026-10-29 实测 SW2024 SP4：InsertBOMBalloon2 → 非选择性的参数；"
                     "InsertBOMBalloon → 无效的参数数目；InsertBalloon → 未知名称。"
                     "用户反馈 SW2026 同款失败。"),
    },
    {
        "id": "delete_annotation",
        "status": "verified",
        "summary": "删除/移动标注（按 name）",
        "why": ("`delete_view` 与 `delete_annotation` / `move_annotation` 均可用；"
                "`set_dimension_position`（0.8.19）只对尺寸生效（GetType==4），会拒绝"
                "note/GTOL/粗糙度，避免名字打错时移动错对象，并回读尺寸值与前后位置。"
                "关键是用 `IAnnotation.Select(False)` 选中——`Select3` 返回 False、"
                "`SelectByID2` 对「细节项目34」这类名字根本解析不到。"
                "删除后回读 annotation 计数确认（82 → 81），不只信 EditDelete 的返回值。"),
        "workaround": ("按 name 操作前先用 `list_drawing_annotations` 取名字——"
                       "名字是 `细节项目N` 形式，每次建图编号不同，不能硬编码。"),
        "evidence": ("2026-10-30 实测：delete 82→81 removed=True；"
                     "move [0.005,0.057]→[0.16,0.16] applied=True；"
                     "0.8.19 set_dimension_position 单尺寸移动 + 类型守卫 smoke 覆盖。"),
    },
    {
        "id": "annotation_write",
        "status": "partial",
        "summary": "注记 / 图纸直线（件号见 balloon 条目）",
        "why": ("`add_note` / `add_sheet_line` 可用；图纸草图单位是**米**不是毫米。"
                "件号请单独看 `balloon` 条目——它是 unsupported，不要在这里当成可用"),
        "workaround": "传毫米会得到满屏放射斜线（坐标被当模型空间并乘视图比例）",
        "evidence": "2026-10-28 实测 add_note / add_sheet_line 均 ok；add_balloon 见 balloon 条目",
    },
    {
        "id": "bom_table",
        "status": "verified",
        "summary": "装配明细栏（原生 BOM）· 仅 SW2024 SP4 验证",
        "why": ("**仅 SW2024 SP4 验证**（2026-10-29）。SW2026 报告 `InsertBomTable4(Indented)` "
                "返回 None，两步法第一步就拿不到表——与本项「verified」的口径不符，"
                "故 status 保持 verified 但限定版本。"
                "**已解决**（2026-10-29 实测）。直接 InsertBomTable4(BomType=TopLevelOnly=2) 在本环境"
                "只出表头且无数量列，SetConfigurations 各种形式都不能修复。可行路径是两步："
                "先用 Indented(3) + 有效配置建表（能出 120 行 4 列，证明装配/模板/引用都正常），"
                "再把 BomFeature.TableType 切到 2——得到 9 行模块 + 一个重复数量列（5 列），"
                "隐藏最后一列即得 10 行 4 列目标形态。"),
        "workaround": ("用 add_bom_table；配置名传真实值（空则取视图的 ReferencedConfiguration）。"
                       "不要在该流程里调 SetConfigurations（会把表清回表头），"
                       "也不要用 SetColumnType 补 Quantity 列（SolidWorks 禁止普通列改数量列，返回 False）。"),
        "evidence": ("SW2024 SP4 实测 SAB1_0000_000.SLDASM：rows=10 cols=4 data_rows=9，"
                     "headers=['项目号','零件号','说明','数量']，保存重开保持。"
                     "SW2026 实测失败：InsertBomTable4(Indented) 返回 None。"),
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
        "why": ("板厚 / 弯折数 / 止裂槽有无可判；`include_cylinders=True` 能给出圆柱面清单"
                "（按半径分组，孔与折弯圆角一并列出）。最小折弯边与孔到折弯线仍需判定"
                "「哪个圆柱面是孔、哪个是折弯圆角」——当前不可靠：半径差=板厚会把 Ø12/Ø14 "
                "同心孔误判为折弯（80 个圆柱面被配成 36 个折弯），面相邻性判据也无效"
                "（已知折弯的内外圆角同样不共享边），故这两项保持 not_checked"),
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
                "可用 leader_x_m/leader_y_m（0.8.19）把引线文字钉在图纸米坐标内，"
                "返回值 echo 请求落点并逐条回读真实 position_m；"
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
        "status": "partial",
        "summary": "形位公差框格（符号为文字）",
        "why": ("**可以做**（2026-10-28 突破）。此前判定「不可创建」是调用方式错误："
                "pywin32 把 InsertGtol 当作属性派发（PROPERTYGET），必然报 "
                "DISP_E_MEMBERNOTFOUND，改用 DISPATCH_METHOD 即可拿到 Gtol 对象，"
                "SetFrameValues2 写入公差与基准并回读确认。"
                "0.8.20：符号格改用**可读文字**（位置度/平面度…）写入第一格，"
                "再用 GetFrameSymbols2 回读校验；`<MOD-*-PAR>` 格式码实测只会被存成"
                "字面文本、不解析成几何符号，故已弃用。写入不生效会删除框格并返回 "
                "`gtol_symbol_not_applied`，不再谎报符号已标注。"),
        "workaround": ("符号是文字不是真正的 GD&T 几何符号，出正式图纸请在界面替换；"
                       "或用 symbol='none' 只写公差值与基准"),
        "evidence": ("2026-10-28 实测 door.SLDPRT：2 个框格 frame1 回读正确；"
                     "2026-10-31 实测 GetFrameSymbols2 回读 ('位置度','','','','','')，"
                     "SetFrameSymbols2 返回 None 必须以回读判定"),
    },
    {
        "id": "surface_finish",
        "status": "partial",
        "summary": "表面粗糙度符号（位置修正 + 像素可见性自检）",
        "why": ("`InsertSurfaceFinishSymbol3` 14 参可创建符号，但**忽略传入的 x/y**——"
                "符号实际落在图纸原点 (0,0)，早期只回显请求值 position_m 造成谎报。"
                "0.8.20 起创建后用 `IAnnotation.SetPosition` 移到请求点并回读 "
                "`position_actual_m`；`verify_render=True` 前后各导出 PDF 渲染 PNG，"
                "在 (x,y) 窗口做像素差分证明符号确实可见（`visibility.visible`）。"
                "`verify_dwg=True` 只检查 DWG 文件非空——矢量格式无像素通道。"),
        "workaround": ("要看符号是否真在图面上，传 verify_render=True；"
                       "像素检查依赖 pypdfium2 + Pillow，缺失时 visibility.checked=false 诚实降级"),
        "evidence": ("2026-10-31 实测 door.SLDPRT：未修位置前 PDF diff 只在原点 "
                     "(0,1626)-(34,1684)；SetPosition 后 diff bbox=(1684,493)-(1736,551) "
                     "落在目标像素 (1701,549)，窗口 changed=265"),
    },
    {
        "id": "detail_view_scale",
        "status": "partial",
        "summary": "局部放大视图：空视图与比例锁定诊断",
        "why": ("局部视图默认 UseParentScale，创建时设置 ScaleDecimal 会被吞掉。"
                "0.8.20 起 `scale` 不再被父视图比例覆盖，创建后重试设置并回读；"
                "仍被锁则返回 `detail_scale_locked` error 而非静默；`edge_count==0` "
                "返回 `detail_empty`（边界圈落在源视图几何之外，视图是空的）。"),
        "workaround": ("`center` 必须落在源视图 outline 内的真实几何上；返回 detail_scale_locked "
                       "时保存重开后在 UI 设比例，或让 scale 等于父视图比例"),
        "evidence": ("2026-10-31 实测 door.SLDPRT：边界在内 edges=1、在外 edges=0；"
                     "`_apply_detail_scale(2.0)` 生效，视图名变为 2 : 1"),
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
    from ..sw_session import error, ok, sw_version

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
        # Version context belongs in the answer: "verified" means verified on one SolidWorks
        # build, and bom_table is SW2024 SP4 only.
        server_name="Solidworks_AssetMate_MechDog",
        server_version=__import__("solidworks_assetmate_mechdog").__version__,
        sw_version=sw_version(),
        platform=__import__("platform").platform(),
        operation=operation,
        checked=len(entries),
        blocked=[c["id"] for c in blocked],
        partial=[c["id"] for c in partial],
        capabilities=payload,
        advice=("有 %d 项做不了。调用前请先看 workaround，不要跑完流程才发现。"
                % len(blocked)) if blocked else "全部可做。",
    )
