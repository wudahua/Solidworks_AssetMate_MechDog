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
        "id": "outline_precision",
        "status": "verified",
        "summary": "视图 outline 恒定虚胖 11.9 mm，已用常数内缩统一修正",
        "why": ("`IView.GetOutline` 对每个视图的宽高各多报 11.9 mm（1:1 与 2:1 实测相同，"
                "是固定偏移而非投影误差）。所有判断——打包、重叠、出界、投影轴推断、"
                "点命中——统一走 `precise_outline()` 常数内缩，实测与真实投影吻合："
                "三视图 59.0×28.0 / 59.0×80.0 / 80.0×28.0（误差 <0.1 mm），"
                "detail 视图比虚胖值窄 11.92 mm。**没有逐顶点版本**："
                "`GetVisibleEntities2` 返回的是轮廓边（`GetStartVertex` 为 None）、"
                "`IBody2.GetVertices` 对全圆边实体返回 None、`ICurve` 在该版本没有 "
                "GetEndPoint/GetTessPoints，三条取点路均已实测排除。"),
        "workaround": ("需要原始值时仍读 `outline`，工具同时返回 `precise_outline_m`；"
                       "图纸尺寸（`ISheet`/`GetFirstView.GetOutline`）是精确的，"
                       "实测 420×297 mm，不受该偏移影响"),
        "evidence": ("2026-10-31 实测 灌装阀阀体 59×28×80：raw 70.88×39.88 → precise "
                     "58.98×27.98；drawing_audit 每行带 precise_width_m/precise_height_m；"
                     "detail 11.92 mm 差值"),
    },
    {
        "id": "bridge_resilience",
        "status": "verified",
        "summary": "COM 桥接自愈：存活探针 + 忙时有限重试 + 断线重连 + ping",
        "why": ("曾出现「连续约 50 分钟后所有调用报 Not connected，而 SolidWorks 本身正常」："
                "缓存的 dispatcher 变成死指针却永不刷新。现在 `get_sw()` 用 `RevisionNumber`"
                "（实测 18 ms）做存活探针，最多每 2 s 一次，失败立即丢缓存重新 GetActiveObject；"
                "`call_method()` 区分两类失败——忙（RPC_E_CALL_REJECTED）退避重试最多 2 次，"
                "断线（CO_E_OBJNOTCONNECTED 等）**不重试**而是丢缓存并抛 `BridgeLost` "
                "提示重试该工具；`ping()` 做真实往返，失败即重连重试一次并报 `recovered`。"
                "未处理的异常会 flush 到 stderr 并写入 logs/mcp-YYYYMMDD.log。"),
        "workaround": ("没有线程级硬超时：pywin32 在拥有对象的 STA 线程上派发，把调用丢到工作线程"
                       "再超时放弃会破坏 COM 公寓而不是取消调用。单次真正挂死仍会阻塞，"
                       "属 SolidWorks 侧状态，客户端无法安全中断"),
        "evidence": ("2026-10-31 实测：ping ok / 32.4.0 / 239.5 ms；污染缓存后 get_sw 自动丢弃"
                     "（stale_drops 0→1）并返回活连接；ping 报 recovered=true（reconnects 1→2）；"
                     "错误分类三例（busy / not connected / member not found）判定正确"),
    },
    {
        "id": "dimension_edit",
        "status": "partial",
        "summary": "尺寸删除、去重与单个移动",
        "why": ("`delete_dimensions` 按值保留/删除；`auto_dimension(eliminate_duplicates=True)` "
                "（0.8.19）按模型 full name 自动删除多视图重复副本，只动本次插入的尺寸、"
                "不碰用户已有尺寸；`set_dimension_position` 可把单个尺寸移到图纸坐标并回读数值。"
                "SolidWorks 无批量删除接口，实测 14 条一次调用 < 5 s（早期文档高估约 100 倍）；"
                "`prune_orphan_dimensions`（0.8.24）按 name + position 清掉挂不到实体的尺寸，"
                "`delete_dimensions` 按值删无法区分同值不同位置（一张阀体图有三个 23 mm）"),
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
        # Report 2026-11-07 (C7): this said "do not pair it with add_sheet_line, that tool
        # renders 0 pixels" — while `sheet_sketch_space` in the SAME table says `verified`
        # with 571 changed pixels. The capability table must not contradict itself; the
        # leader can be drawn, only the balloon object itself is unavailable.
        "workaround": ("件号用 add_note 写数字 + `add_sheet_line` 画引线"
                       "（`AddToDB` 已修，实测 571 像素可见）；"
                       "或界面手工插（明细栏行号本身已经正确）"),
        "evidence": ("2026-10-29 实测 SW2024 SP4：InsertBOMBalloon2 → 非选择性的参数；"
                     "InsertBOMBalloon → 无效的参数数目；InsertBalloon → 未知名称。"
                     "用户反馈 SW2026 同款失败。"),
    },
    {
        "id": "delete_annotation",
        "status": "verified",
        "summary": "删除/移动标注（按 name；可用 position_m 区分重名）",
        "why": ("四类标注均已实测可删：note / 形位公差 / 表面粗糙度 / 尺寸，note 也已实测可移。"
                "关键是用 `IAnnotation.Select(False)` 选中——`Select3` 返回 False、"
                "`SelectByID2` 对「细节项目34」这类名字根本解析不到。"
                "**成功判定必须跨全部视图计数**：删除尺寸不改变图纸视图自身的注解数"
                "（实测图纸恒 83、工程图视图1 从 8→7、总数 122→121），只看 `GetFirstView` "
                "会把成功的删除误报成 `delete_failed`。"
                "SolidWorks 按草图复用 D1/D2/D3，一张图可能有 19 个同名 `D1`，"
                "这时传 `position_m` 指定具体那一个。"),
        "workaround": ("按 name 操作前先用 `list_drawing_annotations` 取名与 `position_m`——"
                       "名字是 `细节项目N` 形式，每次建图编号不同，不能硬编码；"
                       "重名尺寸务必带 `position_m`"),
        "evidence": ("2026-10-31 实测：note 移到 [0.045,0.15] 生效；GTOL 删除 125→124、"
                     "SFSYMBOL 删除、DIMENSION 删除 123→122 全部 removed=True；"
                     "重名 D1 用两组 position 分别定位到不同对象。"),
    },
    {
        "id": "dimension_display_text",
        "status": "verified",
        "summary": "尺寸的显示文本与类型（值槽是 GetText(1)，不是 GetText()）",
        "why": ("`IDisplayDimension.GetText()` **不带槽号返回空串**——尺寸的值与格式码在槽 1："
                "实测 Ø20 尺寸 `GetText(0)=''`、`GetText(1)='<MOD-DIAM>'`、"
                "`GetType=5`（直径；线性尺寸为 2）。0.8.23 起 `list_drawing_annotations` "
                "对尺寸返回 `dimension_type`、`raw_text`（原始槽 1）与解析后的 `text`"
                "（`<MOD-DIAM>`→`Ø`、`<DIM>`→数值）；`delivery_check` 的 Ø 判定改用 "
                "`dimension_type`，不再依赖显示文本。此前正因只读 `GetText()`，"
                "一张印着 Ø28/Ø23/Ø12/Ø18/Ø13 的图被判「0 处 Ø 尺寸」并降级 verdict。"),
        "workaround": ("判断是否直径看 `dimension_type == 5`；要比对文字用 `text`；"
                       "要核对原始编码用 `raw_text`"),
        "evidence": ("2026-10-31 实测：38 个尺寸里 18 个 `dimension_type==5`，解析后 text 为 Ø；"
                     "`delivery_check` 的 hole_callouts 由「0 处 Ø 尺寸」变为「17 处 Ø 尺寸」"),
    },
    {
        "id": "annotation_write",
        "status": "partial",
        "summary": "注记 / 图纸直线（件号见 balloon 条目）",
        "why": ("`add_note` 可用；`add_sheet_line` 也**可用**（0.8.173 起：先设 "
                "`AddToDB = True` 再 `CreateLine`，草图线就会渲染——实测 571 像素可见）；"
                "图纸草图单位是**米**不是毫米。"
                "件号请单独看 `balloon` 条目——它是 unsupported，不要在这里当成可用"),
        "workaround": "传毫米会得到满屏放射斜线（坐标被当模型空间并乘视图比例）",
        "evidence": "2026-10-28 实测 add_note / add_sheet_line 均 ok；add_balloon 见 balloon 条目",
    },
    {
        "id": "bom_table",
        "status": "verified",
        "summary": "装配明细栏（原生 BOM）",
        "why": ("两步法：`InsertBomTable4(BomType=Indented=3)` 建表（能出行），"
                "再把 `BomFeature.TableType` 切到 2（TopLevelOnly）。"
                "直接建 TopLevelOnly 只出表头且无数量列，`SetConfigurations` 各种形式都修不好。"
                "切完会多一个重复数量列，隐藏最后一列即得目标形态。\n\n"
                "**配置名必须传真实值**（2026-11-06 修正）：早先这里写「仅 SW2024 SP4 验证、"
                "SW2026 返回 None」，把一次**配置名不匹配**误诊成了版本限制 —— 中文 GB 模板的"
                "配置叫「默认」，而代码默认传 `\"Default\"`，于是 `InsertBomTable4(Indented)` "
                "返回 None，回执还把责任推给版本（当时 `sw_version` 正是它声称支持的那个）。"
                "0.8.166 起按 调用方 → 视图 `ReferencedConfiguration` → 引用装配的 "
                "`ActiveConfiguration` → `\"Default\"` 逐层下探，SW2024 SP4 与 SW2026 都通过。"),
        "workaround": ("用 add_bom_table，`configuration` 留空即可（会取真实配置名）。"
                       "不要在该流程里调 SetConfigurations（会把表清回表头），"
                       "也不要用 SetColumnType 补 Quantity 列（SolidWorks 禁止普通列改数量列，返回 False）。"),
        "evidence": ("两版并列实测："
                     "SW2024 SP4 + SAB1_0000_000.SLDASM → rows=10 cols=4 data_rows=9，"
                     "headers=['项目号','零件号','说明','数量']，保存重开保持（2026-10-29）；"
                     "SW2026 34.0.0 + 自造两零件装配体 → rows=3 cols=4 native=True"
                     "（v0.8.168 smoke，2026-11-06，同一构建 `add_bom_table` 项 PASS）。"),
    },
    {
        "id": "multi_sheet",
        "status": "verified",
        "summary": "一个 .SLDDRW 内建多张图纸页 · 可用（0.8.196 起）",
        "why": ("**我错了 14 个版本。** 从 0.8.180 到 0.8.195 我一直写「`NewSheet4` 是 "
                "late-bound，pywin32 推不出 11–13 个参数的签名」，还把它记成了"
                "「客户端环境限制，不是功能缺失」。\n\n"
                "真实情况两条都错：\n\n"
                "1. **类型库早就生成了** —— `%TEMP%/gen_py/3.13/"
                "83A33D31-27C5-11CE-BFD4-00400513BB57x0x32x0.py` 一直都在；\n"
                "2. **我猜错了参数顺序**。真实签名是\n\n"
                "```\n"
                "NewSheet4(Name, PaperSize, TemplateIn, Scale1, Scale2, FirstAngle,\n"
                "          TemplateName, Width, Height, PropertyViewName,\n"
                "          ZoneLeftMargin, ZoneRightMargin, ZoneTopMargin, ZoneBottomMargin,\n"
                "          ZoneRow, ZoneCol)\n"
                "```\n\n"
                "`TemplateIn` 是**第三个**参数、类型 I4，我却一直把 `None` / `''` 塞在那里，"
                "所以每种 arity 都在 `类型不匹配 arg 6` 上失败 —— 那是 COM 在派发层就把类型"
                "挡了，SolidWorks 根本没收到调用。"),
        "workaround": ("不需要。`add_sheet` 直接用 `gencache` 缓存里的 `IDrawingDoc` 包装"
                       "late-bound 对象；缓存不存在时才退回 `sheet_creation_blocked` 并给"
                       "makepy 命令。"),
        "evidence": ("SW2024 SP4 实测（2026-11-07 07:53，飞达动力轴）：`('图纸1',)` → "
                     "`['图纸1', '图纸2']`，`ok: true`。"),
    },
    {
        "id": "sheet_sketch_space",
        "status": "verified",
        "summary": ("图纸草图坐标系 · 草图线能渲染（需要 `AddToDB`）"
                    "· 实测于 SW2024 SP4 + 飞达动力轴 A3"),
        "why": ("早先这里写「图纸草图不渲染，调用返回 `line_not_rendered`」—— **2026-11-06 更正**："
                "缺的是 `SketchManager.AddToDB = True`。同包的 `add_weld_symbol` 一直设它，"
                "它的几何能渲染；`add_sheet_line` 没设，于是 0 像素。补上之后实测 "
                "`changed_pixels: 572, visible: true`。\n\n"
                "`AddToDB` 的作用是关闭捕捉与推断，线才会落 在调用方给的坐标上"
                "（否则会吸附到邻近视图几何）。"),
        "workaround": ("先算好每段线的米坐标，批量调用。若 `visibility.changed_pixels` 为 0，"
                       "先确认模板没有锁住图纸草图，而不是直接判「画不出来」。\n\n"
                       "**证据面**：只测过 SW2024 SP4 + 一个零件一次。这正是当年出错的原因之一 ——"
                       "旧结论「不渲染」声称覆盖 SW2024/2026 两个版本，而它也只在一个环境下测过。"
                       "所以这里不重复那个错误：`line_not_rendered` 分支保留，它是真实路径。"),
        "evidence": ("SW2024 SP4 实测（2026-11-06，飞达动力轴 A3）：`add_sheet_line(0.30,0.24 → "
                     "0.35,0.24)` → `ok:true`，`visibility {checked:true, changed_pixels:572, "
                     "visible:true}`。补 `AddToDB=True` 之前同一调用是 0 像素。"),
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
        "id": "dimension_per_view",
        "status": "verified",
        "summary": "尺寸跨视图分布可诊断，但**不能改派**——尺寸属于所在视图",
        "why": ("反馈称「文档级幂等，尺寸全集中到首个视图」。**实测不复现**："
                "气缸盖 `auto_dimension(all_views=True)` 在 3 视图上是 "
                "**20 / 10 / 9**，4 视图是 **20 / 10 / 9 / 0**（等轴测恒空），"
                "2 视图是 20 / 10。首视图确实最多——那是 SW 把每条尺寸放进"
                "**能显示它的**视图，不是缺陷。"
                "**API 无法改派**：`IAnnotation` 属于它的视图，`SetPosition` 只改图纸位置；"
                "`all_views=False` 只插当前视图能显示的部分，插不到时返回 "
                "`no_dimensions_inserted`。所以「按语义分配」（直径→主视图、深度→侧视图）"
                "在本 binding 下没有入口。"),
        "workaround": ("用 `survey_dimensions_by_view` 看实际分布："
                       "**空视图才是真问题**——等轴测没有尺寸正常，"
                       "右视/俯视没有尺寸通常说明图纸缺对应几何。"
                       "需要特定分配时，只能在界面手工移动（同视图内改位置）或补建视图。"),
    },
    {
        "id": "cosmetic_thread",
        "status": "partial",
        "summary": "装饰螺纹线：图纸层能检出并拦截漏标，但**模型侧读不到规格**",
        "why": ("模型侧四条路全失败（2026-11-03，旋转轴 Ø10 段带 10 mm 装饰螺纹线）："
                "① 特征树 9 个特征里没有 Thread/Cosmetic；② `GetCosmeticThreads` / "
                "`GetCosmeticThreadCount` **方法不存在**；③ `GetFirstAnnotation2` 报 "
                "`DISP_E_UNKNOWNNAME`；④ SLDPRT 二进制扫描 0 命中。"
                "**但图纸上有**：`IAnnotation.GetType == 1`、名字「装饰螺纹线N」，"
                "`list_drawing_annotations` 早就报成 `kind: other_1`，只是没人去看。"),
        "workaround": ("`delivery_check` 的 `cosmetic_threads` 检查：图纸有螺纹注解但全图"
                       "没有任何螺纹文字（「螺纹」/M10/M8/M12）→ **blocker**，verdict 降级。"
                       "**规格（M10×1）不编**——反馈靠像素反推得出，工具不把它当事实输出，"
                       "只要求「设计确认后手写」。"),
    },
    {
        "id": "centerline",
        "status": "partial",
        "summary": "轴线中心线 · 前提已推翻（草图能渲染），本身未重测",
        "why": ("反馈试了五条路全失败。本轮追到根因，比中心线本身更底层："
                "`InsertSketch` + `CreateLine` **能把线建出来**"
                "（`GetSketchSegments` 数到 1 段），`SketchSegment.Style` 也能写，"
                "**但图纸上不显示、导出 PDF 也不含它**（前后像素差分 **0 / 16,041,784**）。"
                "线默认落在 `Layer = '10'`，而文档 12 个图层**一个都读不到**："
                "`GetLayerManager`/`GetCount` 是属性（加括号报错），"
                "`GetLayerAtIndex` 不存在。`PrintSetup` 是方法且要参数，猜不出形态。"),
        "workaround": ("**轴线能画，但枚举顺序和文档不一样**（2026-11-08 实测）。"
                       "`add_sheet_line(line_style=...)` 可以设线型 —— "
                       "`ISketchSegment.Style` 是**后期绑定**属性，`dir()` 列的 32 个属性里"
                       "查不到它，不等于它不存在（这一点我错过一次，已更正）。"
                       "本 build（SW2024 SP4）逐值实测的「覆盖率 / 笔画数」："
                       "1→0.69/69（细虚线）、2→0.71/31、3→0.77/25、"
                       "4→0.94/7（长划、窄间隙）、5→0.11/194（极稀的点）、"
                       "6→0.57/20（更粗）、**7→1.00/1（唯一的连续线）**。"
                       "**`style=1` 名义上是 solid，印出来是细虚线；要连续线传 7，"
                       "要细点划线传 4。** 回执里的 `line_style_measured` 会带回该值的"
                       "实测覆盖率与笔画数，不用再翻文档猜。"
                       "孔中心符号仍用 `add_center_marks`（已加幂等：已标过的视图返回 "
                       "`skipped: already_marked`）。"
                       "**但 `GetCenterMarkCount` 在本机（SW2024 SP4）恒读 0，即使确实"
                       "画上了东西** —— 2026-11-08 实测：`气缸盖` 三视图 count 全 0，"
                       "而前后 PDF 差 **5253 像素**；`小车底部滚轮`（导入实体）、`飞达动力轴`、"
                       "`baquebo`、`P01_底座横梁` 同样全 0。所以："
                       "① `views_marked: 0` **不代表没画**，要另导 PDF 比对；"
                       "② `skipped: already_marked` 在本机**永远不触发**（分不出「已标过」），"
                       "该分支只在 count 能正常读出的环境里生效。"
                       "**注意 `ok` 的语义**：它由 `ok()` 统一返回 `true`，所以"
                       "`ok:true + views_marked:0` 是**零标注但没报错**，不是「标注成功」。"
                       "判断有没有真的画上，只能看 `views_marked` 或另导 PDF 比对，别看 `ok`。"
                       "**想过用前后像素差兜底，做不到**（2026-11-08，但原因已修正）。"
                       "`add_center_marks` 内部做「改前/改后」页面差量，三次都得到 "
                       "`changed_pixels: 0`。当时以为是 `SaveAsSilent` 破坏文档，"
                       "**报告复测未能重现**：导出之后 `add_note` 仍能写入"
                       "（`annotation_count 10 → 11`），`GetPathName` 也仍指向 `.SLDDRW`。"
                       "所以「导出会破坏文档」这条**不成立**，那次失败更可能是我实验代码"
                       "自己的缺陷。**当前状态：差量为 0 的真实原因未知，不再归因于导出。**"
                       "**轴线跟随视图：能**（2026-11-08 更正，第七条「做不到」被推翻）。"
                       "草图归谁取决于 `InsertSketch` 之前激活的是谁：`ActivateSheet` → "
                       "图纸草图（`草图1`，不跟随）；`ActivateView` → **视图草图**"
                       "（`草图3`，跟随）。所以 `add_sheet_line(view_name='工程图视图1')` "
                       "画的线**会跟着视图移动和改比例走**；不传 `view_name` 就仍在图纸上。"
                       "回执的 `attached_to` / `follows_view` 说明线落在哪个草图。"
                       "~~视图移动或改比例时轴线不会跟随，必须重画~~（此结论已推翻）。"
                       "`views_marked` 从 0.8.228 起只在 `center_mark_count > 0` 时计数，"
                       "不会再对「返回 True 但插入 0 个」的视图报成功。"
                       "~~点划线样的回转轴线目前没有可靠入口~~（此结论已推翻，2026-11-08）"),
    },
    {
        "id": "weld_symbol",
        "status": "partial",
        "summary": "焊接符号（`sketch_block` 实现）：线、三角、尺寸文字都渲染，但不是原生对象",
        "why": ("`IWeldSymbol` 只存符号名不实例化几何（SW32.4.0，两个零件实测：`SetText` 返回 True、"
                "`GetText(2)` 读回 \"FILL\"、图上只有引线和尺寸数字），所以改用图纸草图线画"
                "（`InsertSketch` + `CreateLine` + `AddToDB=True`）。\n\n"
                "**2026-11-06 更正**：早先这里写「仍然 0 像素、三角形从来没出现过」，并据此把"
                "状态定为 `unsupported` —— **那是错的**。当时用的是整页差分，而本函数同时写了"
                "note（note 是原生注解，能渲染），两者混在一起无法分离；作者由此推断三角形没画出来，"
                "把「直线画不出来」的结论外推到了焊缝。\n\n"
                "现在的判据是 **lines-only 差分**：写完线、**在写 note 之前**渲染一次，差值只可能"
                "来自草图几何。实测 2134 像素（整页 2230，差 96 = note 文字），三角形、参考线、"
                "引线都在。`triangle_verified` 因此是真测量值，不再是常量 False。"),
        "workaround": ("调用后看 `triangle_verified` 与 `lines_only_visibility`：`checked:true` 且"
                       "`changed_pixels>0` 表示几何确实渲染了；`checked:false` 表示渲染器不可用、"
                       "两种情况都没验证，不代表画不出来。"
                       "**局限**：不是 `IWeldSymbol` 原生对象，不随焊件特征更新、无法按焊接注解查询；"
                       "`weld_type=\"butt\"` 仍是明确拒绝，不做近似。"),
        "evidence": ("SW2024 SP4 实测（2026-11-06，飞达动力轴 A3）：`ok:true`，"
                     "`triangle_verified:true`，"
                     "`lines_only_visibility {checked:true, changed_pixels:2134, visible:true}`，"
                     "`visibility {changed_pixels:2230}`；PDF 目视可见完整角焊缝符号"
                     "（水平参考线 + 直角三角形 + 「6.0」+ 斜引线）。"),
    },
    {"id": "datum_feature_symbol",
        "status": "verified",
        "summary": "基准要素符号：能插入、能指定字母、能挂在指定边上；形状是圆形，这是 GB 的正确样式",
        "why": ("`add_datum_feature` 走 `IModelDoc2.InsertDatumTag2`（`IDrawingDoc.InsertDatumTag` "
                "已标 Obsolete），**在当前位置的选择集上插入**，所以必须挂到边上——参数是 "
                "`view_name` + `edge_index` 而不是自由图纸点，因为基准必须指向真实几何。"
                "实测 2026-11-02：`ok: True`、`attached_to: 工程图视图1[0]`、"
                "`GetLabel` 默认是 'B'、`SetLabel('A')` 读回一致。"
                "**形状是圆形里带字母，这对 GB 是正确的**——文档属性里「显示类型」三选一"
                "（依照标准 / 方形 / 圆形(GB)），三角形的「定位样式」属于**方形**基准点"
                "（ANSI / 部分企业标准），圆形基准点用的是垂直/竖直/水平，没有三角。"
                "实测该符号 `GetTriangleCount` 为 0、`GetLineCount` 1、`GetArcCount` 1，"
                "几何上本就没有三角。"),
        "workaround": ("不需要 workaround。**不要试图从代码里逼出三角形**：`FilledTriangle` "
                       "只填充已有三角；制图标准（整数偏好 188）与 187/189/190/191/199 "
                       "都改不了显示类型。真要方形三角样式，得改模板 "
                       "`gb_a3.drwdot` → 文档属性 → 注解 → 基准点 → 显示类型 → 方形，"
                       "那是模板改动，会影响所有用该模板新建的图纸。"),
    },
    {
        "id": "section_line_follows_view",
        "status": "verified",
        "summary": ("移动父视图时剖面切割线跟着走（0.8.57 起；此前线画在图纸坐标里，"
                    "移了就断联）· 实测于 SW2024 SP4 + 飞达动力轴"),
        "why": ("**限定**：`verified` 的依据是 2026-11-02 在 **SW2024 SP4 + 飞达动力轴**"
                "上的位移比对（父视图与剖视位移完全一致）。**没有在其他版本或其他零件上复测**——"
                "2026-11-05 的核对在 SW2026 + 另一夹具上得到 `GetFaceHatchCount() == 0`，"
                "那只说明切不出剖面线（见 section_view 的限定），但足以说明本项的证据面很窄。\n\n"
                "**0.8.57 之前做不到，之后自然成立。** 关键在于线画在哪个坐标系："
                "旧实现按**图纸坐标**画（`pos[0] ± half`），线是相对图纸原点的绝对位置，"
                "视图一移动就与几何脱开；而且那条线其实压根没落在视图草图里，"
                "剖视因此静默退化成投影（`GetFaceHatchCount() == 0`）。"
                "0.8.57 起线画在**视图草图坐标系**（原点 = 视图中心、单位 = 模型空间），"
                "草图属于视图，视图移动时草图随行，线自然跟随。"
                "**实测 2026-11-02**（飞达动力轴，A3 1:1）：父视图从 (0.07629, 0.26551) "
                "移到 (0.05, 0.05)，位移 (-0.02629, -0.21551)；剖视位移完全相同，"
                "两者相对距离恒为 (0, -0.08344)。剖切面 hatch 正常（edges 37）。"
                "**注意**：`IDrSection.GetLineInfo` 仍返回模型坐标、`SetLineInfo` 仍会"
                "把多段线重排成折返——但既然线已随视图走，就不再需要碰这两个接口。"),
        "workaround": "无需 workaround：正常摆视图即可，剖切线会跟着。",
    },
    {
        "id": "hole_callout_text_slots",
        "status": "partial",
        "summary": "孔标注文字：带深度的孔会连成 Ø1515",
        "why": ("`AddHoleCallout2` 产出的文字是 `<MOD-DIAM>` 格式码，必须用 `SetText` 改写，"
                "但**直径和深度在不同文字槽里**：只写直径时深度留在另一槽，Ø15 深15 渲染成 "
                "`Ø1515`（反馈里的 `Ø9 → Θ99` 就是同一件事）。三条路实测都不通——"
                "① 只写直径 → 深度粘连；② 写直径后清空其余槽 → **标注整条消失**；"
                "③ 完全不写 → 印出字面量 `<MOD-DIAM>`。"
                "通孔（无深度，如门板 Ø8）正常，只有带深度的孔受影响。"),
        "workaround": ("对带深度的孔改用 `add_drawing_dimension`（普通尺寸，文字可控），"
                       "或建完标注后人工改一次文字；`add_hole_callouts` 的回执里 "
                       "`text_source=SetText_depth_may_trail` 就是这条限制的标记"),
        "evidence": ("2026-11-01 门板 A3：Ø8×2 渲染正常（`Ø8`），Ø15×3 渲染成 `Ø1515`；"
                     "PDF 文本提取 + 渲染图双向确认"),
    },
    {
        "id": "duplicate_dimension_to_view",
        "status": "unsupported",
        "summary": "把同一条驱动尺寸补插到第二个视图（局部放大）",
        "why": ("**API 层面做不到**（2026-11-01 实测）。`InsertModelAnnotations3` 是文档级幂等："
                "同一模型尺寸一旦插入过，再次调用一律返回 None——`all_views=True/False`、"
                "`duplicateDims=True/False`、激活目标视图，全部组合都试过，第二个视图始终 0 条。"
                "具体实测：pass1 `all_views=True` 插 12 条（全在视图1）；"
                "pass2 激活视图2 + `all_views=False` + `duplicateDims=True` → 返回 None；"
                "pass3 `all_views=True` + `duplicateDims=True` → 也返回 None。"),
        "workaround": ("在目标视图**重新标注**，不要想复制：先 `list_drawing_edges(view_name=...)` "
                       "拿到该视图的边，再用 `add_drawing_dimension(view_name=..., edge_index=...)` "
                       "建一条新尺寸。它引用的是同一个模型几何，数值必然一致，只是多一条标注"),
        "evidence": ("2026-11-01 履带轮 P05：两视图实验，三组参数组合，第二视图尺寸数恒为 0；"
                     "`InsertModelAnnotations3` 返回值恒为 None"),
    },
    {
        "id": "hole_callout_batch",
        "status": "pilot",
        "summary": "批量孔标注（带 verdict：能否标注由几何决定）",
        "why": ("按孔去重（同圆心同径只标一次），单视图 25 秒预算，超时返回 budget_exceeded。"
                "0.8.24 起返回 `verdict`：`ok` / `no_native_callout` / `nothing_to_annotate`。"
                "`no_native_callout` 表示找到了孔但 `AddHoleCallout2` 全部返回 null——"
                "实测（阀体）边与相邻面都能选中、锚点就在圆上仍为 null，而同一调用在"
                "钣金拉伸孔上可用，属几何/构建限制，换 offset 或换视图不会改变，"
                "`native_callout_note` 会把这一点写清楚。"),
        "workaround": ("看到 `no_native_callout` 就不要再调参数重试：改用 Ø 尺寸或手动补标；"
                       "`list_circular_edges`（0.8.24 修好，改用 CircleParams 而非顶点）"
                       "可先列出圆边判断值不值得试"),
        "evidence": ("2026-10-28 实测钣金门板 Ø8/Ø15 成功；2026-10-31 实测阀体 8/8 全 null"
                     "（边/面均选中），`list_circular_edges` 由 0 条修正为 23 条"),
    },
    {
        "id": "geometric_tolerance",
        "status": "verified",
        "summary": "形位公差框格（原生 GD&T 符号，位置可控）",
        "why": ("**可以做，而且是原生符号**。两个历史结论都已过期，写在这里免得有人"
                "照着旧文档直接放弃这项能力：\n"
                "1) 「不可创建」是调用方式错误——pywin32 把 InsertGtol 当属性派发"
                "（PROPERTYGET）必然报 DISP_E_MEMBERNOTFOUND，改用 DISPATCH_METHOD "
                "即可拿到 Gtol 对象。\n"
                "2) 0.8.20 曾把符号格写成**可读文字**（因为 `<MOD-*-PAR>` 格式码在旧路径"
                "上只被当字面文本）。**2026-11-04 在 0.8.124 + SW32.4.0 上复测：** "
                "`symbol='position'/'perpendicularity'/...` 走 2022+ 原生路径，回执为 "
                "`symbol_written: 'IGTOL-PERP'`、`symbol_is_text: false`、"
                "`symbol_render_verified: true`、`gtol_text_count: 0`，PDF 渲染出来是"
                "真正的 ⊥ / ⌖ / ▱ 几何符号。\n"
                "框格坐标 `x/y` 也被如实采纳（`position_set: true`），可以精确摆放。\n"
                "**仍然受限的是基准符号**，不是框格：`add_datum_feature` 的落点由所选边"
                "推导，传入坐标被忽略，详见 `datum_feature`。"),
        "workaround": ("框格可直接用于正式图纸。若需要基准符号落在指定位置，"
                       "用 `add_datum_feature` 选边后回读 `position_actual_m` 判断落点，"
                       "不合适就换一条边重试（或用 add_note 手工标，但那就不是原生基准了）"),
        "evidence": ("2026-11-04 实测 接槽.SLDPRT（v0.8.124）：三处框格 ▱0.1 / ⊥0.1 A / "
                     "⌖0.2 A B 在 PDF 里是真符号，symbol_is_text=false；"
                     "2026-10-28 实测 door.SLDPRT frame1 回读正确"),
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
        "id": "section_view",
        "status": "partial",
        "summary": ("剖视图（模型坐标剖切面 + 剖切 sanity 校验）· `offset_m=0` 可切；"
                    "剖面线取决于**剖切线是否伸出轮廓**与**零件是否赋材质**，不是「看运气」"),
        # Report 2026-11-07 (P0-1): a revolved part (wheel / shaft / sleeve / flange) needs
        # an AXIAL full section, and taking it from the front view silently produced an
        # uncut projection — `edge_count` equal to the parent's, `hatch_count 0`, and
        # `sanity.verdict: "ok"`. The working recipe is to cut the END view vertically.
        # `sanity` now reports `no_hatch` / `identical_to_source` instead of `ok`.
        "workaround": ("**`cut_line_overshoot_m`（默认 4 mm）—— 2026-11-08 修掉根因**。"
                       "「`offset_m=0` 切不开」不是偏置问题，是**剖切线长度被 clamp 到"
                       "「短边一半」，端点正好落在轮廓圆上**（65×65 视图的半长 = 半径），"
                       "SolidWorks 把这种退化线当成「没穿过实体」，返回普通投影。"
                       "现在线会伸出轮廓，`offset_m=0` 直接可切：实测 `限位导向套` 前视 "
                       "46 边 / hatch 4 / ratio 1.000，`小车底部滚轮`（单实体导入件）"
                       "前视 28 边 / hatch 2 / ratio 1.000。"
                       "仍然切不开时按顺序试：①`cut_line_overshoot_m=0.008`；"
                       "②检查 `sanity.verdict` —— 报 `identical_to_source` 是线没穿过实体，"
                       "报 `no_hatch` 才去看剖面线设置；"
                       "③手工做。"
                       "**关于材质（历史备注，已不成立）**：0.8.25x 时期记录过"
                       "「未赋材质时 hatch 恒 0」，2026-11-09 在同一零件（未赋材质、"
                       "`get_mass_properties` 报 1000 kg/m³ 回退密度）上实测 `hatch_count 4`、"
                       "渲染图剖面线清晰 —— **这条结论作废**，它当时很可能只是"
                       "「端点压在轮廓圆上」那条退化路径的伴生现象。"
                       "**不要用 `offset_m` 去凑** —— 回转体上偏 5 mm，Ø16 内孔剖面变成 "
                       "2·√(8²−5²)=12.5 mm，标出来的直径是错的；`section_off_axis` 会拦"
                       "「剖面比源视图窄 0.5% 以上」的情况，但它拦不住所有偏心。"
                       "**精度分层**：`edge_count == source_edge_count` 返回 "
                       "`section_not_cut`（根本没剖开）；`hatch_count == 0` 返回 "
                       "`hatch_missing`（剖开了没画线）；两者 hint 不共用。"
                       "**失败视图**：`keep_on_failure='auto'` 默认删掉 `section_not_cut` / "
                       "`section_empty` 的空剖视，保留 `hatch_missing`（能看出剖切面落在哪）。"
                       "删除能清掉 84% 的污染，但**父视图草图里的剖切线清不掉**"
                       "（`EnumSectionLines` 在本 binding 抛错），回执用 "
                       "`section_line_remains: true` 如实说明。"),
        "why": ("0.8.21 起 `cut_plane_normal` / `cut_plane_origin` 可用**模型坐标**指定剖切面；"
                "内部按「视图 deflated outline 与零件包围盒的尺寸匹配」确定图纸轴 ↔ 模型轴"
                "（`GetViewPlaneNormal` 不存在、`ModelToViewTransform` 的 16 个数不是行主序 "
                "4x4、`IMathPoint` 没有 MultiplyTransform，三者均已实测排除）。"
                "法向必须轴对齐（主分量 ≥0.9），斜切 / 平行图面 / 映射歧义分别返回 "
                "`cut_plane_oblique` / `cut_plane_not_in_view_plane` / `cut_plane_unmapped`。"
                "`verify_cut=True` 默认开启：垂直于图纸水平轴的剖切不改变宽度，故剖视宽必须"
                "等于源视图宽（竖切比高），容差 10%；超出返回 `section_suspicious`，"
                "`edge_count==0` 返回 `section_empty`，两者都保留视图供检查。\n\n"
                "**限定（2026-11-05）**：剖切面切出来有没有剖面线（`GetFaceHatchCount`）"
                "**取决于零件几何**，不是代码问题。同一台 SW2026 上，飞达动力轴回调 "
                "`hatch_count: 1`，而另一夹具 `hatch_count: 0` → `hatch_missing`。"
                "0.8.56 起这种情况**如实返回 `hatch_missing` 而不是 `ok:true`**，"
                "能力状态因此是 `partial`：视图能建、剖切面能定位，但**剖面线不保证**。"
                "需要剖面线时请换一个能切出实体截面的视图或零件。"),
        "workaround": ("映射靠尺寸匹配，正立方体这类各向等长的零件会 `axes_ambiguous`，"
                       "此时改传 orientation + offset_m；部分剖（depth>0）尺寸本就缩小，"
                       "sanity 记 checked=false 不判定"),
        "evidence": ("2026-10-31 实测 灌装阀阀体 59×28×80：法向 +Y 过中心 → 轴映射 [0,1]、"
                     "horizontal、剖视 58.98×79.98、ratio 1.000；已知错位 offset +0.012 → "
                     "截面 14.4×49.98、ratio 0.244，被 `section_suspicious` 拦下；"
                     "法向 +X → vertical、ratio 1.04；法向平行视线 / 斜切均结构化拒绝"),
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
    from ..sw_session import error, ok, runtime_info, sw_version

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

    # Version context belongs in the answer: "verified" means verified on one SolidWorks
    # build, and each entry names which builds it was measured on. (2026-11-06: this comment
    # used to claim "bom_table is SW2024 SP4 only" — that was a misdiagnosis. The tool failed
    # because a Chinese GB template names its configuration 默认 while the code passed
    # "Default"; it has since passed on both SW2024 SP4 and SW2026.) The PATH matters just
    # as much — several copies of this package can coexist on one machine, and an editable
    # install pinned to an old checkout is exactly what once sent a report after fixes that
    # were already in the repo.
    runtime = runtime_info()
    return ok(
        server_name="Solidworks_AssetMate_MechDog",
        server_version=runtime.get("server_version"),
        server_path=runtime.get("server_path"),
        python_executable=runtime.get("python_executable"),
        python_version=runtime.get("python_version"),
        pid=runtime.get("pid"),
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
