# 2026-10-28 AssetMate v0.4.6 验收：6 个新工具调用即崩（模块未导入/参数遮蔽），hole_callouts 尺寸过滤单位错配

> 验收性质：v0.4.6 大版本验收（0.1.7 → 0.4.6，工具 8 → 35 个，新增工程图全链路/材料/台账/批量导出五大模块）
> 测试环境：Windows + SolidWorks 2026 + Python 3.13.14 + pywin32 + AssetMate v0.4.6（MCP 服务器进程 2026-10-28 09:51:47 启动，晚于 09:48 源码更新）
> 测试通道：全部经 MCP 工具调用实测；静态 AST 分析交叉定位根因
> 结论：**27 个新工具中 20 个实测通过（含完整工程图工作流端到端跑通），7 个存在缺陷，其中 6 个调用即崩**——均为 server.py 注册层问题，模块实现本身完好，修复成本极低。

---

## 一、P0：6 个工具调用即崩（server.py 注册层缺陷）

### 1.1 根因一：import 行缺模块（4 个工具崩）

`server.py` 第 12 行：

```python
from .tools import assembly, bodies, bom, drawing, export as export_tools, recolor, rename
```

缺 `material`、`ledger`、`survey` 三个模块。AST 静态分析 + MCP 实测双确认：

| 工具 | MCP 实测报错 | 后端实现 |
|---|---|---|
| `material_library` | `Error: name 'material' is not defined` | material.py:175 存在 ✅ |
| `assign_materials` | `Error: name 'material' is not defined` | material.py:127 存在 ✅ |
| `build_feature_ledger` | `Error: name 'ledger' is not defined` | ledger.py:204 存在 ✅ |
| `survey_drawings` | `Error: name 'survey' is not defined` | survey.py:47 存在 ✅ |

### 1.2 根因二：参数名遮蔽模块名（1 个工具崩，import 修好也仍崩）

`set_part_material` 的 wrapper 签名是 `def set_part_material(paths, material: str, ...)`——参数 `material: str` 在函数作用域内**遮蔽**了 `material` 模块名，`material.set_part_material(...)` 实为在字符串上找属性：

```
MCP 实测：Error executing tool set_part_material: 'str' object has no attribute 'set_part_material'
```

修复需两步：import 补上 `material` **且** wrapper 参数改名（如 `material_name`）或用别名导入（`from .tools import material as material_tools`）。tools/material.py 中的 `set_part_material(paths, material, ...)` 实现无此问题（不在同一名字空间）。

### 1.3 根因三：wrapper 签名与函数体脱节（1 个工具崩）

`add_hole_callout`（单数版）：

```python
def add_hole_callout(drawing_path: str, x: float, y: float, view_name: str | None = None) -> str:
    ...
    return _json(drawing.add_hole_callout(
        drawing_path, x, y, view_name=view_name, model_path=model_path, save=save,
    ))
```

函数体引用 `model_path`/`save`，但签名里没有——`NameError: name 'model_path' is not defined`（实测复现）。后端 `drawing.py:710` 的实现签名是 `(drawing_path, x, y, view_name=None, model_path=None, save=True)`，wrapper 照抄参数即可。**注意 `add_hole_callouts`（复数版）wrapper 签名正确、可正常注册，只是被下述 P1 挡住。**

**修复汇总（一行 import + 两处改名/补参）**：

```python
from .tools import (assembly, bodies, bom, drawing, export as export_tools,
                    ledger, material as material_tools, recolor, rename, survey)
# set_part_material 的 material 参数改名为 material_name，调用改为 material_tools.set_part_material(...)
# add_hole_callout 签名补 model_path: str | None = None, save: bool = True
```

## 二、P1：`list_circular_edges` / `add_hole_callouts` 的 min_diameter_mm 过滤单位错配

**现象**：`min_diameter_mm=0` 找到 142 个圆（Ø2~Ø70）；`min_diameter_mm=40` 反而返回 **0 个**——门槛越高结果越少是正常的，但降到 0 都没有就是过滤逻辑坏了。连锁后果：`add_hole_callouts(min_diameter_mm=40)` 返回 `no_circles`，批量孔标注工作流（`list_circular_edges` → `add_hole_callouts`）在带过滤的场景下不可用。

**根因（实测定量坐实）**：`drawing.py` 的预过滤门槛

```python
floor = max(float(min_diameter_mm) * 0.5, 1e-4)   # 本意：40mm 半径 = 20mm
```

`min_diameter_mm` 是**毫米**，`floor` 却直接与**米**制弦长比较（SW API 返回米）。`min=40` → floor=20.0 **米**；实测单视图 Ø60 圆的弦长 0.0599 m、共 63 个圆全部被 `chord < floor` 误杀。`min=0` 时 floor 兜底 1e-4 m=0.1mm，恰好放行全部圆，所以默认参数看不出问题。

**修复**：`floor = max(float(min_diameter_mm) * 0.0005, 1e-4)`（毫米转米）。

**附带观察**：min=0 时该过滤仍会把"闭合整圆（起点=终点，弦长=0）"跳过吗？实测没有——Ø60 整圆弦长 59.99mm 正常入列。因为视图边里的整圆实测带非零弦（圆被剖面线打断成弧段），当前数据集未触发，但纯整圆模型（无剖切）建议作者补一个用例确认。

## 三、实测通过的工具（27 个新工具中 20 个 ✅ + 旧 8 工具全部回归 ✅)

**工程图端到端工作流（全部真实落盘，可复现）**：

| 步骤 | 工具 | 实测要点 |
|---|---|---|
| 建图 | `create_drawing` | 自动挂 GB A3 模板（gb_a3.drwdot），front+top+iso 三视图，safe_area 标注标题栏/明细栏边界，266KB 落盘 |
| 审计 | `drawing_audit` | evidence-only 口径，0 尺寸给 warning 不装通过 |
| 自动标注 | `auto_dimension` | 插入 29 个模型驱动尺寸 |
| 附件核查 | `verify_dimension_attachments` | 29 尺寸逐个报 vertex/edge 挂接，9 个 orphan 如实列出（§6.1 口径） |
| 列圆 | `list_circular_edges`（min=0） | 142 圆含直径/视图/坐标 |
| 中心线 | `add_center_marks` | 3 视图标记，如实说明 GetCenterMarkCount 低报 |
| 剖视 | `add_section_view` | 剖面 A-A 生成 |
| 局部放大 | `add_detail_view` | 局部视图 B (1:5) 生成 |
| 公差 | `add_dimension_tolerance` | dry-run → apply → readback（tolerance_type=4） |
| 手工尺寸 | `add_drawing_dimension` | EDGE 选择命中，值 56.96mm |
| 粗糙度 | `add_surface_finish` | 原生 Ra3.2 符号 + 回读 |
| 标题栏 | `set_title_block` | 端盖 / CLM-T1-001 / 合金钢 三字段写入 ✅（dry-run 与 apply 双确认） |
| 清单 | `list_drawing_annotations` | 94 项注解全量清点（29 尺寸+84 note+6 中心线+1 粗糙度） |
| 导出 | `export_drawing` | PDF 74KB |
| 渲染 | `render_drawing` | 装 pypdfium2 后 2382×1684 PNG，内容非空 |

**其他**：`reconcile_ledger`（35/35 覆盖率 1.0，投影口径说明清晰）、`delivery_check`（结构化 verdict「仅供参考/未完成」+ blockers/pending 分级，与 §7 流程一致）、`batch_export`（dry-run → apply，STEP AP203 真实文件 21KB/50KB，冲突不覆盖）、`list_drawing_edges`（189 边含模型坐标）。

**旧 8 工具回归**：assembly_tree（10 组件）、mass_properties（4.248158 kg + 1000.0 kg/m³ 披露）、export_bom（7 行 + CSV）、rename/preview/recolor/list_bodies 全部保持 v0.1.7 验收水平。

## 四、版本管理提醒

本机源码目录的 0.4.6（09:48，含 5 个新模块的大功能版）与作者仓库已验收的 0.4.6（1d7067b，纯文档发布）**版本号相同但内容不同**。若两者都要发布，建议本机这版改用 0.4.7+，避免同版本号两个内容造成交付歧义。

---

*证据：`sandbox/T1_drawing.SLDDRW`（端到端工作流产物）、`sandbox/T1_drawing.pdf` + `sandbox/drw_png/T1_drawing_p1.png`、`sandbox/step_out/*.STEP`、`sandbox/bom_v046.csv`；全部报错为 MCP 通道原文。6 个崩溃工具的修复仅涉及 server.py 注册层（一行 import + 两处参数），后端模块实现经 import 直接调用验证结构完整。*
