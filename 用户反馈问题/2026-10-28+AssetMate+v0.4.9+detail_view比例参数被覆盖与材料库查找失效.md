# 2026-10-28 AssetMate v0.4.9 验收：上轮 7 缺陷全部修复 ✅；本轮新增 add_detail_view 比例参数被源码覆盖（2:1/3:1 局部放大图不可用）、中文版材料库默认路径 100% 失效、BOM 总质量父子双算

> 验收性质：v0.4.9 全量回归（35 工具全覆盖）+ v0.4.6 报告（2026-10-28《6工具NameError与hole_callouts单位错配》）重点复核
> 测试环境：Windows + SolidWorks 2026 + Python 3.13.12（WorkBuddy 内置）+ AssetMate v0.4.9（editable 安装指向 src，MCP 服务器进程 11:20:24 启动，晚于 11:00 源码更新，代码一致性经 pyc mtime + 字符串表双重核验）
> 测试模型：焊接夹具装配体 `3C01400300A0治具用.SLDASM`（7 组件，含 1 子装配）、自制副本零件 `T_定位块.SLDPRT`（含异型孔向导 M6 螺纹孔、圆角、切除拉伸）及由此生成的 GB A3 工程图（8 视图 25 尺寸 84 note，端到端落盘）
> 结论：**上轮 6 个调用即崩工具全部修复、单位错配修复，v0.4.9 实测 35/35 可调用（0 崩溃）**；本轮发现 1 个 P1（detail view scale 参数被覆盖）、2 个 P2（材料库路径、BOM 总质量）、4 个 P3（报错文案/COM 回读/rename 死锁/视图越界联动）。

---

## 一、上轮问题复核结果（重点核查项，全部修复 ✅）

| 上轮问题 | v0.4.9 实测 | 证据 |
|---|---|---|
| server.py import 缺 `material`/`ledger`/`survey` 模块（4 工具 NameError） | ✅ 已修复 | import 行已含全部模块；4 工具 MCP 实测全部正常返回 |
| `set_part_material` 参数 `material` 遮蔽模块名 | ✅ 已修复 | 参数改名 `material_name`（server.py:299-306），dry-run/回读正常 |
| `add_hole_callout` wrapper 签名缺 `model_path`/`save` | ✅ 已修复 | 签名补齐（server.py:536-538），实测创建成功 |
| `list_circular_edges`/`add_hole_callouts` 的 min_diameter_mm 毫米当米用（floor=20m 误杀全部圆） | ✅ 已修复 | drawing.py:2468 `floor = max(float(min_diameter_mm) * 0.0005, 1e-4)`；实测 min=8 正确过滤出 8 个 Ø10 圆，min=0 返回全部 18 圆 |

**上轮 7 个缺陷工具逐个复测**：`material_library`、`assign_materials`、`set_part_material`、`build_feature_ledger`、`survey_drawings`、`add_hole_callout`、`add_hole_callouts` —— 全部可调用、返回结构化结果，0 崩溃。v0.4.6 的修复建议（一行 import + 两处改名/补参 + 单位换算）被完整采纳。

## 二、本轮新发现

### P1-1 `add_detail_view` 的 scale 参数在函数体内被无条件覆盖——局部放大图永远是父视图比例

**现象**：`scale` 显式传 3（或默认 2）时，返回 `requested_scale=1.0, scale=1.0, scale_locked=false`——注意 `requested_scale` 来自 Python 变量 `float(scale)`，**说明参数在到达 CreateDetailViewAt4 之前就变成了 1.0**，不是 SolidWorks 拒绝设置。三次实测（默认、scale=2、scale=3）全部如此。

**根因（源码定位）**：`tools/drawing.py` `add_detail_view` 内：

```python
def add_detail_view(drawing_path, view_name=None, center=None, radius_m=0.02,
                    position=None, scale=2.0, ...):
    ...
    doc.ActivateView(target.Name)
    pos = list(target.Position)
    scale = float(target.ScaleDecimal)        # ← 第 1847 行：把「源视图比例」回写进参数 scale
    ...
    sketch_x = (sheet_x - pos[0]) / scale     # 这里本意是用「父视图比例」换算 sketch 坐标
    ...
    detail = doc.CreateDetailViewAt4(..., float(scale), float(scale), ...)
    applied = _apply_detail_scale(doc, detail, scale)   # ← 用被覆盖后的值去设置目标比例
```

参数名与「父视图比例」临时变量同名，`scale = float(target.ScaleDecimal)` 把用户请求的目标比例**冲掉**。后续 `_apply_detail_scale` 与 `requested_scale` 全部基于被覆盖值，因此永远"成功"且等于父视图比例——`scale_locked` 永远 false，缺陷完全静默。

**修复建议**：临时变量改名（如 `parent_scale`），或参数改名 `target_scale`，两处用途分开：

```python
parent_scale = float(target.ScaleDecimal)
sketch_x = (sheet_x - pos[0]) / parent_scale
...
applied = _apply_detail_scale(doc, detail, scale)   # scale 保持用户请求值
```

**影响**：2:1 局部放大图是 GB 图纸的标准需求，当前 API 路径完全无法获得；且因 `scale_locked=false`，调用方无法察觉。附：同文件 `add_section_view` 也把 `scale = float(target.ScaleDecimal)`（第 1692 行附近），但该函数没有 scale 参数，不存在覆盖，仅命名易混淆，建议顺手区分。

### P1-2 `add_detail_view` 不传 center 时，第二次起 boundary 中心解析到图纸外（y=-0.3558m）

**实测序列**（同一张图、同一个源视图 `工程图视图1`）：

| 调用 | center 参数 | 返回 boundary_center_m | 落点 |
|---|---|---|---|
| ① 创建局部视图 B | 未传 | (0.2214, 0.2576) | 视图1 中心，正确 |
| ② 创建局部视图 C | 未传 | (0.0974, **-0.3558**) | A3 图纸外（y∈[0,0.297]） |
| ③ 创建局部视图 D | 未传 | (0.0974, **-0.3558**) | 同上，可复现 |
| ④ 创建局部视图 E | **显式 (0.2214, 0.2576)** | (0.2214, 0.2576) | **回显正确** |

**隔离结论**：显式 center 完全正常，缺陷只发生在「不传 center 的自动路径」——即 `sheet_x = (outline[0]+outline[2])/2, sheet_y = (outline[1]+outline[3])/2` 基于 `target.GetOutline` 的取值在第二次调用起漂移到 (0.0974, -0.3558)。两次调用之间 `source_view` 报告同名（工程图视图1），说明要么 `GetOutline` 在图中已有 detail/flat-pattern 视图后对同名视图返回了不同值，要么 `_iter_views` 的 Name 匹配命中了错误对象。请作者在「create_drawing → add_flat_pattern_view → add_detail_view × N」序列下复测视图枚举。

**连带影响**：该图 6 个视图被 `drawing_audit` 标记 `view_outside_sheet`（survey 池 3 份同图合计 18 条），并使 `delivery_check` 判入 blockers。异常落点的视图叠加了 flat-pattern 视图（工程图视图6，高 0.3159m > 0.297m，`layout.fits=false`），布局器见「观察项 3」。

### P2-1 `material_library` 默认材料库在中文版 SolidWorks 上 100% 找不到，且 `database` 传绝对路径无效

**现象 1**：默认参数调用返回 `library_not_found: No SolidWorks Materials.sldmat found under PROGRAMDATA/APPDATA`。本机（简体中文版 SW2026）材料库实际位于：

```
C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang\chinese-simplified\sldmaterials\solidworks materials.sldmat
C:\ProgramData\SolidWorks\SOLIDWORKS 2026\自定义材料\自定义材料.sldmat
```

实现只在 `PROGRAMDATA\SOLIDWORKS` 与 `APPDATA\SOLIDWORKS` 两个根下 os.walk 并按**文件名全等**比较（material.py:184-196），上述两处均不覆盖。

**现象 2**：把完整绝对路径传入 `database` 参数，返回同样错误——hint 写着 "Pass an explicit database"，但实现从未支持路径输入（`filename.lower() == database.lower()` 对路径永假）。hint 与实现自相矛盾，AI 调用方按 hint 传路径必失败。

**修复建议**：① 搜索根补 `os.environ.get("PROGRAMFILES", r"C:\Program Files") + r"\SOLIDWORKS Corp\SOLIDWORKS\lang"`（顺带覆盖 english/其他语言目录）；② `database` 参数兼容绝对路径：`if os.path.isfile(database): 直接使用`。

### P2-2 `export_bom` 的 total_mass_kg 把子装配行与其子件行重复计入

**实测**（`3C01400300A0治具用.SLDASM`，7 组件含 1 子装配 `3C01400500A0治具用` 及其 2 个子件）：

| 来源 | 数值 |
|---|---|
| `get_mass_properties`（装配总质量，权威） | **0.295478 kg** |
| `export_bom.total_mass_kg` | **0.330515 kg**（偏大 11.9%） |

差值恰等于子装配行 0.035038（其两个子件行 0.003967+0.031070=0.035037）。**闭环验证**：total_mass_kg − 子装配行 = 0.295477，与 `get_mass_properties` 的装配总质量 0.29547794918… 仅差 9.5e-7（纯舍入误差）——父子双算定性。行级数据本身没丢（qty/material 均正确），但 `total_mass_kg` 不能直接用于称重核对。建议：汇总时跳过 `is_subassembly=true` 的行（或在响应中提供 `total_mass_leaf_only`）。

### P3-1 `add_drawing_dimension` 对「点在视图外」给出误导性报错

x/y=(0.16, 0.22)（该点不在任何视图 outline 内）时返回 `segment_not_found: No edge selected and no sheet point given`——实际上 x/y 都给了。实现在 `_view_at_point` 返回 None 且无 edge_index 时落入统一报错分支，不区分「没传点」与「点没落在任何视图内」。建议后者单独报 `point_outside_views` 并附各视图 outline，帮助调用方修正坐标。

### P3-2 `add_hole_callout` 的 readback text 是 COM 方法 repr 而非文本

实测回读：`"text": "<bound method GetText of <COMObject <unknown>>>"`——取值时调用了 `GetText` 属性访问但没带参数调用（应 `GetText()` / `win32com` 的 `Call()` 或改用 `GetText2`）。value_mm 5.0 正常，仅文本字段损坏。

### P3-3 `add_hole_callout`（单数版）按 x/y 创建的标注落到了别的视图

把 `list_circular_edges` 报告的**工程图视图2** 的 Ø10 圆坐标 (0.1879, 0.1892) 传给单数版，返回 `"view": "工程图视图1"`——标注创建在 front 视图而非坐标所属视图；回读值 5.0mm（M6 底孔值）与目标 Ø10 不符却报 `model_match.ok=true`（最近孔径匹配到模型里恰好存在的 Ø5）。批量版 `add_hole_callouts` 在同一坐标上行为正确（创建于视图2）。建议单数版显式按 x/y 调用 `_view_at_point` 并把 view 写进回读断言。

### P3-4 `rename_components` 对「本 MCP 自己读取过的文件」必然 WinError 32，形成工作流死锁

对 sandbox 副本零件执行 rename（apply=true）报 `另一个程序正在使用此文件`——该文件此前被 `set_part_material`/`build_feature_ledger` 等只读工具打开后留在 SW 会话中，MCP 自身没有 close 工具，调用方无从解锁；hint「Close the file in SolidWorks first」对纯 MCP 调用方不可执行。建议：rename 前自动 `doc.Close` 目标文件（或提供 `close_document` 辅助工具）。

## 三、实测通过的工具（35/35 可调用；以下 27 个通过验收）

**工程图端到端工作流（全部真实落盘）**：create_drawing（GB A3 模板，front+top+iso，safe_area）→ drawing_audit（evidence-only）→ auto_dimension（22 个模型驱动尺寸）→ verify_dimension_attachments（23 尺寸逐个报挂接，5 orphan 如实列出）→ add_center_marks（3 视图）→ add_section_view（剖面 A-A）→ add_detail_view（视图创建本身成功，比例缺陷见 P1）→ add_dimension_tolerance（dry-run→apply→readback type=2）→ add_drawing_dimension（edge_index 路径，90mm 底边命中）→ add_surface_finish（Ra3.2 原生符号）→ set_title_block（图样名称/代号×2/材质 4 字段写入+回读）→ list_drawing_annotations（101 项全量清点）→ list_drawing_edges（10 边含模型坐标）→ export_drawing（PDF 77KB）→ render_drawing（2382×1684 PNG，非空白校验通过）。

**模型/装配/交付**：build_feature_ledger（7 特征 41 驱动尺寸 8 孔候选，notes 披露口径）、reconcile_ledger（覆盖率 0.9，4 缺项按特征分组）、delivery_check（结构化 verdict「仅供参考/未完成」+ blockers/pending 分级）、get_assembly_tree（7 组件含子装配递归）、get_mass_properties（默认密度 1000 kg/m³ 如实披露）、export_bom（7 行 + 缺材料 warnings，总质量问题见 P2-2）、recolor_assembly（dry-run→apply，回读 [0.847,0.2,0.2] 匹配）、export_preview（1280×960 PNG，图像像素统计确认红/蓝上色生效）、batch_export（dry-run→apply，STEP 872KB/65KB 落盘）、list_bodies、survey_drawings（3 图纸池 10.4s，分布+占比+警告统计）、rename_components（dry-run 正常，apply 见 P3-4）、material_library/assign_materials/set_part_material（见 P2-1）。

## 四、观察项（不影响验收，供作者参考）

1. **中文特征名 mojibake（编码根因已定位）**：`build_feature_ledger`/`reconcile_ledger` 返回 `ÂÝÎÆ¿××êÍ·Éî¶È`。本机复现：`'螺纹孔钻头深度'.encode('gb2312').decode('latin-1')` 输出与 API 返回逐字符一致——SW API 返回的 GB2312 字节被按 Latin-1 解码。数值正确，仅名称列乱码。修复方向：按 SW 会话代码页（0x804）解码，或 `bytes.encode('latin-1').decode('gb2312', errors='replace')` 兜底。
2. **非钣金件也能创建 flat pattern 视图**：对无 Flat-Pattern 特征的 T_L块 调用 `add_flat_pattern_view` 返回 ok=true（视图 6，scale 2.0，高 0.3159m 超出 A3 且 fits=false）。schema 承诺"part must have a Flat-Pattern feature"，建议前置校验；注意该视图的 ScaleDecimal=2.0 设置成功，反衬普通 detail view 的比例锁死确为 add_detail_view 自身缺陷而非 SW 绑定限制。
3. **布局器不重排已存在视图**：create_drawing 的 auto_fit 只管自己创建的三视图；后续 detail/section/flat pattern 落位由固定偏移决定，板上是 view_outside_sheet 的主要来源（本轮 18 条警告、delivery_check 1 个 blocker 全部源于此）。
4. **版本管理**：本机 0.4.9 修复了 0.4.6 报告全部问题，迭代质量高；建议后续在 serverInfo 或 __init__ 暴露运行时版本号，便于验收方确认「所测即所写」。

---

*证据文件：`sandbox_v049/T_DWB.SLDDRW`（8 视图端到端产物）、`sandbox_v049/T_DWB.pdf` + `sandbox_v049/drw_png_v049/T_DWB_p1.png`、`sandbox_v049/step_out_v049/*.STEP`、`sandbox_v049/asm_preview.png`、`sandbox_v049/survey_pool/`（survey 用例）；全部报错为 MCP 通道原文。P1-1 的根因行号：src/solidworks_assetmate_mechdog/tools/drawing.py add_detail_view 函数体第 34 行（`scale = float(target.ScaleDecimal)`）。*

---

### 附：验收结论速览（供作者排期参考）

| 级别 | 数量 | 一句话 |
|---|---|---|
| 上轮回归 | 7/7 ✅ | v0.4.6 报告全部缺陷已修复，0 崩溃 |
| P1 | 2 | detail view 比例参数被覆盖（源码 1 行改名可修）；不传 center 的自动落点第二次起飞出图纸（显式传参可绕过） |
| P2 | 2 | 中文版材料库默认路径失效 + database 路径参数不生效；BOM total 父子双算（+11.9%） |
| P3 | 4 | 报错文案误导 / COM GetText 未调用 / 单数版 callout 视图归属错 / rename 自锁死锁 |
| 观察项 | 4 | GB2312 mojibake / 非钣金可建展开视图 / 布局器不重排 / 版本号披露 |
