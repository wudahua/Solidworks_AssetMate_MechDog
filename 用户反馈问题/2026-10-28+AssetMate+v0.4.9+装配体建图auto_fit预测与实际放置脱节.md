# 2026-10-28 AssetMate v0.4.9 验收反馈：create_drawing 的 auto_fit 预测模型与实际放置脱节，装配体图纸仍越界

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0，验收时正在运行）· Python 3.13.12 (64-bit) · mcp 1.30.0 · WorkBuddy MCP 客户端
> 验收对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（352e272，v0.4.9，含 v0.4.7–v0.4.9 四个提交）
> 结论：**v0.4.9 验收基本通过**。上轮 2 处文档问题已修复；`static_check.py` 是重要补强（并如实披露了 v0.4.6 曾带 6 个调用即崩工具发布）。本轮发现 1 个中等缺陷：**装配体建图的 auto_fit 预测用硬编码边距，与模板实测安全区/实际放置脱节，产出的图纸仍被自家 `drawing_audit` 判 `view_outside_sheet`**。

---

## 一、上轮反馈修复核验 + 本版新能力（✅）

| 上轮问题 / 本版新特性 | 实测结果 |
|---|---|
| **README 漏列 2 工具** | 已补：工程图域表头改为 16，`list_circular_edges`、`add_hole_callouts` 均已列入，13+16+6=35 与 `tools/list` 一致 ✅ |
| **smoke 默认路径指向仓库外** | README 已注明"仓库不含测试模型"，给出 `--assembly/--part` 示例与 `--skip-export --skip-drawing` 快跑方式 ✅ |
| **新增 `tests/static_check.py`** | 本机运行：`static check OK (35 tools, every module reference resolves)`，秒级、零依赖 ✅。其 docstring 如实披露 v0.4.6 曾带 6 个"调用即崩"工具发布（`material`/`ledger`/`survey` 未 import、一处 wrapper 引用未声明参数）——该缺陷 `py_compile` 查不出，此守卫正是对症 |
| **smoke 新增静态检查项 + `--eng-cases` 真实零件链路入口** | smoke 升至 12 项，本机 12/12 PASS（1m47s，静态检查 0.0s 位列第一）✅ |
| **`set_part_material` 参数改名 `material` → `material_name`** | tools/list schema 确认改名生效 ✅（注意：这是 breaking rename，调用方需同步） |
| **`add_hole_callout` 补 `model_path`/`save` 参数** | schema 确认 ✅ |

**回归全绿**：握手 0.4.9 / 35 工具 / dry-run 零匹配探索性 / no_match 守卫 / mass_basis / not-found 信息，全部保持 ✅。

---

## 二、本轮核心发现：装配体建图 `auto_fit` 预测模型与实际放置脱节，产出越界图纸

### 现象（本机 10 组件装配 Ensamblaje1 实测）

```
create_drawing(assembly, views=[front,right], paper=A3, auto_fit 默认)
  → ok=true, 2 视图, scale=0.2, layout.fits=false
drawing_audit(同一张图)
  → warning: view_outside_sheet (工程图视图1)
```

**建图宣称成功、自家审计立刻判越界**——与项目"工具返回成功 ≠ 图纸正确"的设计原则直接冲突。这条链路本应闭环：v0.4.9 的 `_model_box` 修复注释里写的正是"旧代码对 1.69 m 机器静默用 1.0 比例（reported as D-3）"，但实测本机装配（包围盒 1080.6 × 571.1 × 140.0 mm，`_model_box` 已正确取到）依然越界。

### 根因（离线复现 + 源码对照）

`create_drawing` 的自动比例分两步，两步用的**不是同一套几何**：

1. **初次估比例**（`fit_w/fit_h` 公式）：用模板实测安全区口径（`sheet_w-0.12` / `sheet_h-0.10`），算出 fit≈0.24 → 选 0.2；
2. **越界预测**（`_layout()`，新增的降档循环用）：却用**硬编码常量**——`0.05` 左边距、`0.03` 右边距、`0.245` 前视图中心锚点、`0.005` 容差——完全没用到 `template_safe_area()` 已经量出来的模板安全区（本机 GB 模板实测：left **0.0588**、bottom **0.065**、parts_list_right **0.12**、title_block_top 0.061）；
3. 预测结果 `over=False`（`fy+hh/2=0.245 ≤ 0.292`），降档循环不触发；
4. 但实际放置后视图 outline 底边进入模板底框区（bottom 安全区 0.065 + 明细栏），`drawing_audit` 按**真实 outline vs 模板 sheet_box** 判越界。

换言之：**修 D-3 时给预测器换了对的包围盒，却仍用错的边距模型**。`layout.fits=false` 在响应里已经如实算出，却没有与预测联动（fits=false 时既不强制降档也不加 warning），矛盾被静默返回给调用方。

### 建议修复（按优先级）

1. **`_layout()` 改吃 `template_safe_area()` 的实测值**：锚点与边距从安全区推导（`fy_center = (safe.bottom + (sheet_h - safe.top)) / 2` 之类），替换全部硬编码常量；函数已在作用域内（`safety` 变量 453 行就有），改造成本低；
2. **`layout.fits=false` 时联动**：要么强制再降一档标准比例，要么在返回中加 `warning`（与 `drawing_audit` 的 issue 命名对齐），不要静默返回 `ok=true`；
3. （加固）smoke 或 eng-cases 中加一条"建图后必须 `drawing_audit` 零布局警告"的断言，让这类回归在真机上必现。

### 严重性

中等——零件级小模型（默认 0.15×0.08×0.08 兜底盒）通常不触发；装配体/大件正是该功能的目标场景，且失败模式是"宣称成功 + 自家审计打脸"，损害交付判定链路的公信力。

---

## 三、次要观察：`list_circular_edges` 单位修复的验证局限

`min_diameter_mm` 单位修复（`*0.5` → `*0.0005`，mm→m 换算，修复注释清晰）在代码层面正确，但本机 sandbox 零件均为平板/折弯件，**前/右/顶视图均无圆边**（count=0），无法构造行为级对照。建议作者在 eng-cases 里挑一个带孔零件覆盖此断言（README 提到 1460 个零件的测试集里有配电箱 80 个圆柱面，是理想样本）。

---

## 四、验收记录（本机，v0.4.9 @ 352e272）

| 验证项 | 结果 |
|---|---|
| `serverInfo.version` / tools/list | `0.4.9` / 35 ✅ |
| `tests/static_check.py` | OK，35 工具引用全解析 ✅ |
| smoke（本机模型，跳过慢项） | **12/12 PASS**（含新静态检查项）✅ |
| set_part_material 改名 / add_hole_callout 扩参 | schema 确认 ✅ |
| 装配体 `_model_box` | 正确取到 1080.6×571.1×140.0 mm（v0.4.6 会拿零）✅ |
| 装配体建图 auto_fit → audit | **越界仍在**（见第二节）⚠️ 本轮核心发现 |
| `min_diameter_mm` 单位修复 | 代码正确；本机无带孔零件，行为级未验证（见第三节）|
| 回归六项（零匹配/守卫/mass_basis/not-found 等） | 全部保持 ✅ |

*以上基于 main 分支 352e272 实测。越界复现数据：box=(1.0806, 0.5711, 0.1400) m，A3 横向，模板安全区 left 0.0588 / bottom 0.065 / parts_list_right 0.12，`_layout(0.2)` 预测 over=False vs audit 实判 view_outside_sheet。*
