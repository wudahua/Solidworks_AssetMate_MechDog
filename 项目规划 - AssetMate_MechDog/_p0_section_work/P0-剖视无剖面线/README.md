# P0 交接：剖视图不生成剖面线

> 这份文档是为了把问题完整交给另一位规划者/工程师。
> 目标不是"我们试过什么"，而是**让接手的人不必重复已经排除的方向**。

---

## 一句话

`AssetMate_MechDog` 的 `add_section_view` 能建出剖视图（`ok: true`、`sanity: ok`、`edge_count` 正常），
但导出的 PDF 里**剖切面上一条剖面线都没有**，视图内部全白，只剩轮廓线。

**GB 规定剖视图必须在剖切面上画剖面线。没有剖面线的剖视，加工方分不清实体与空腔——比没有剖视图更糟，因为它看起来"画了剖面"。**

---

## 环境

| 项 | 值 |
|---|---|
| SolidWorks | `32.4.0`（SW2024 SP4） |
| Python | 3.13.x / pywin32 |
| 工具版本 | AssetMate_MechDog v0.8.55 |
| 反馈来源 | 另一台机器，SW `34.0.0`（SW2026），**同样现象、边数一致（13）** |

**两台机器、两个 SW 版本，行为一致**——所以这不是某台机器的偶发问题。

---

## 复现（可直接执行）

```python
import sys, os, shutil
sys.path.insert(0, "src")
from solidworks_assetmate_mechdog.tools import drawing

SRC = "测试用例/飞达动力轴/飞达动力轴.SLDPRT"   # 或任意带回转特征的零件
work = "P0-剖视无剖面线/证据/axis"
part = os.path.join(work, "model.SLDPRT")      # 已存在
drw  = os.path.join(work, "drawing.SLDDRW")    # 已存在

# 若从零重跑：
# shutil.copy2(SRC, part)
# drawing.create_drawing(part, output_path=drw, views=["front"], paper="A3")
# drawing.add_section_view(drw, view_name="工程图视图1",
#                          orientation="horizontal", label="A")

pdf = os.path.join(work, "drawing.pdf")
drawing.export_drawing(drw, pdf)
drawing.render_drawing(pdf, output_dir=work, scale=2.0)
```

**实际返回**（本机实测）：

```
section ok: True   edge_count: 13   sanity.verdict: ok   export ok: True
```

**渲染结果**：见 `证据/axis/drawing_p1.png` —— A-A 剖视内部**全白**。

---

## 已经排除的三个假设（都有实测证据，别再走）

### ① 显示模式是线框（隐藏线未开）

```python
sec.GetDisplayMode2()   →  2      # 2 == 隐藏线可见，本来就是对的值
```
`IView.DisplayMode` / `IView.ViewDisplayMode` **都不可写**（`<unknown>.DisplayMode`）。

**结论：不是显示模式。**

### ② `CreateSectionViewAt5` 的 options 少传了位

```python
options = 4 if flip else 0     # 只用了 swCreateSectionView_ChangeDirection
```

这是 SW 默认的剖视创建方式。

**结论：不是 options。**

### ③ 材质缺失 → 无剖面图案

SW 论坛与官方文档都确认：**剖面图案由零件材质决定**
（"The section property is controlled by the part material."）。

所以怀疑过"没材质就没图案"。用真实零件验证：

```
build_feature_ledger(飞达动力轴) → material: 普通碳钢   ← 有材质
```

**仍有问题，一条线都没有。**

**结论：不是材质。**

---

## API 探测矩阵（全部失败，附报错原文）

| 尝试 | 结果 |
|---|---|
| `IView.GetSection()` | `(-2147352573, '找不到成员。')` — **这个 binding 根本没有** |
| `IView.GetSectionLineInfo` / `GetSectionLineInfo2` | 存在，但返回**空 tuple**（`len: 0`） |
| `ISectionView.ScaleHatchPattern` | 官方文档明确是 `IView.GetSection()` 返回的 `DrSection` 上的属性——**入口拿不到，够不着** |
| `import swconst` | `ModuleNotFoundError: No module named 'swconst'` |
| `swUserPreferenceToggle_e` 里的 section hatch 开关 | **官方枚举里没有这一项**（查了 2010 / 2022 / 2024 / 2026 四个版本） |
| `swApp.GetUserPreferenceToggle(i)` 遍历 190–274 | 63 个 `False`，**无法区分哪个是 hatch** |
| `swApp.SetUserPreferenceToggle` | 存在，但不知道该设哪个值 |
| `IView.DisplayMode = 2` / `ViewDisplayMode = 2` | `<unknown>.xxx`，**不可写** |

**关键矛盾**：官方文档说剖面线由 `DrSection.ScaleHatchPattern` 控制，
但 `IView.GetSection()` 在这个 pywin32 late-binding 环境下**取不到**。

---

## 已查的官方文档

- [swUserPreferenceToggle_e（2026）](https://help.solidworks.com/2026/english/api/swconst/SolidWorks.Interop.swconst~SolidWorks.Interop.swconst.swUserPreferenceToggle_e.html) —— 确认**没有** section hatch 开关
- [IView.ScaleHatchPattern](https://help.solidworks.com/2027/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IView~ScaleHatchPattern.html) —— 属性属于 `DrSection`，需先 `GetSection()`
- [Scale Hatch Pattern to Section View Example](https://help.solidworks.com/2026/english/api/sldworksapi/Scale_Hatch_Pattern_With_Section_View_Example_CSharp.htm) —— 官方示例用 `swView.GetSection()` 拿 `DrSection`

---

## 想请规划者回答的三个问题

1. **`IView.GetSection()` 在 pywin32 late-binding 下有没有替代入口？**
   （`gencache.EnsureDispatch` 早绑定？`win32com.client.dynamic` 换 `Dispatch`？读 `.tlb`？）
   如果能拿到 `DrSection`，`ScaleHatchPattern = true` 可能就是解法。

2. **剖面线是不是"材质剖面图案"没映射到 GB 标准？**
   SW 的 `普通碳钢` 默认图案是什么？GB 模板（`gb_a3.drwdot`）是否要求不同的 hatch 映射？
   验证方法：手工在 UI 里把该视图的"材质剖面线"关掉、改手动样式，看有没有线。

3. **UI 手工建剖视有没有线？**
   这是**最省钱的判断**，30 秒：
   - UI 也没线 → 文档设置/模板问题，工具无能为力，只需加校验
   - UI 有线 → 工具少传了参数，继续挖

---

## 我倾向的兜底方案（不修根因，但不再骗人）

`add_section_view` 加 `verify_hatch=True`：

1. 建剖视 → 导出 PDF → 渲染 PNG
2. 取剖视图**内部一块窗口**（避开轮廓）
3. 统计非白像素比例
4. **低于阈值 → 返回 `hatch_missing` 结构化错误**，不再 `ok: true`

与 `add_surface_finish` 的 `verify_render` 同一套像素差分思路（那个已经跑通）。
**代价**：每次建剖视多一次 PDF 导出 + 渲染，约 10–20 秒。

---

## 包内文件

```
P0-剖视无剖面线/
├─ README.md                    ← 本文
├─ 复现脚本.py                  ← 可直接跑
└─ 证据/
   ├─ axis/                     ← 飞达动力轴（有材质，仍无线）
   │  ├─ model.SLDPRT
   │  ├─ drawing.SLDDRW
   │  ├─ drawing.pdf
   │  └─ drawing_p1.png         ← 关键：A-A 剖视内部全白
   └─ door/                     ← 门板（无材质，也无线；仅作对照）
      └─ drawing.SLDDRW
```

`证据/axis/drawing_p1.png` 是**最直接的证据**：左侧那个标 A-A 的细长视图就是剖视，
内部除轮廓外完全空白。
