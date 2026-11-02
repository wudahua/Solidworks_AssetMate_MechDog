# GD&T 符号渲染：现状与卡点（交接给规划者）

> 目标：让形位公差（GTol）框格的**符号格**在图上画出真正的几何公差符号（⌖ ◎ ⊥ ∥），
> 而不是空着，也不是退化成中文文字（"位置度""圆柱度"）。
>
> 结论先说：**在本环境（SOLIDWORKS 2024 SP4，32.4.0，pywin32）上，三条已知路径全部堵死，
> 其中第一条是官方文档明确宣布的"结构性不可用"。** 下面给出全部实测证据，请帮忙判断还有没有第四条路。

---

## 一、问题现象

```python
add_geometric_tolerance(drawing_path, 0.10, 0.10, tolerance="0.05",
                        symbol="position", datum="A")
```

返回：

```json
{"ok": true,
 "symbol_written": "位置度",
 "symbol_is_text": true,
 "readback": {"frame1": ["位置度", "", "A", "", ""]}}
```

**回读一致、`ok: true`**。然后导出的 PDF 上：

- 框格里**只有 `A`**
- **符号格是空的**
- 数值 `0.05` 也没显示（因为数值被放在第二个值位，而第一个值位是"位置度"这个假符号）

**这就是"回读通过 ≠ 图面可见"的典型。**

---

## 二、机制：为什么会"回读一致但不画"

`GTOL_SYMBOLS` 是**我们自己写的中文字典**（`src/.../tools/drawing.py:5860`）：

```python
GTOL_SYMBOLS = {
    "position": "位置度",
    "concentricity": "同轴度",
    ...
}
```

`SetFrameSymbols2(1, "位置度", ...)` 把这串中文写进符号格。
SolidWorks **接受并原样存回**（所以回读一致），但**符号格只认"符号索引"，不认字面文字**，
所以渲染时画不出来。

**这不是 SolidWorks 的行为问题，是我们把文字塞进了符号格。**

---

## 三、三条路径，全部堵死

### 路径 A：`IGtol.SetFrameSymbols2` + `<MOD-*>` 格式码 —— **官方宣布对 2022+ 无效**

官方文档（2027 版，`IGtol~SetFrameSymbols2.html`）Remarks 原文：

> **This method is valid only if this Gtol was created in a version of SOLIDWORKS earlier than 2022.**
>
> GTol symbols are defined in `C:\ProgramData\SolidWorks\SolidWorks 20nn\lang\english\gtol.sym`.
> Reference this file to specify GCS, TolMC1, ... in `<LibraryName-SymbolName>` format.
> Examples: `GCS = "<IGTOL-POSI>"` (Position symbol in the ISO Geometric Tolerancing Symbols library)

**本机 `RevisionNumber = 32.4.0`（SW2024）→ 落在"无效"区间。**

这解释了 v0.6.0 ~ v0.8.20 为什么反复失败：**不是参数没调对，是这个方法在 2022+ 上根本不该生效。**

### 路径 B：`IGtol.GetFrame()` → `IGtolFrame.SetSymbolXml()` —— **返回 None**

2022+ 的替代架构是 `IGtolFrame`，用 **XML** 描述帧（`SetSymbolXml(xMLString)`），
入口是 `IGtol::GetFrame`。

实测（SW 32.4.0）：

```python
frame = call_method(gtol, "GetFrame", 1)
# → None
win.Dispatch(frame)   # → AttributeError: 'NoneType' object has no attribute 'GetTypeInfo'
```

先试过无参：`GetFrame()` → `DISP_E_NONAMEDARG`（"非选择性的参数"，即必须传 frame 号）。
传 1 之后返回 `None`。

**拿不到 `IGtolFrame`，就没有 `SetSymbolXml` 可用。**

### 路径 C：Unicode 字形写进公差文本 —— **数据进去了，字体缺字形**

思路：既然符号格画不出来，就把 Unicode 几何公差字符（`⌖` U+2316）写进**公差文本**，
让框格显示 `⌖ 0.05`。

实测：

```json
{"ok": true,
 "symbol_written": "⌖",
 "tolerance": "⌖ 0.05",
 "readback": {"frame1": ["⌖ 0.05", "", "A", "", ""]}}
```

`frame1` 里确实是 `⌖ 0.05`。**但渲染出来的 PDF 上，框格里只有一个 `A`——`⌖` 和 `0.05` 都没画。**

**原因**：GTol 使用的字体缺 `⌖`（U+2316）字形。GTol 的字体由**模板**（`gb_a3.drwdot`）的
"注释字体"设置决定，不是代码能改的。

**这一条比 A/B 有希望**：如果换一个含 GD&T 字形的字体（如 `Segoe UI Symbol`、`Arial Unicode MS`），
理论上能画出来。**但我没有验证，因为改模板字体超出了这一轮的范围。**

---

## 四、完整探测矩阵（报错原文）

| 尝试 | 结果 |
|---|---|
| `SetFrameSymbols2(1, "<IGTOL-POSI>", ...)` | 写入后回读为空 / 或存成字面文本 |
| `SetFrameSymbols2(1, "位置度", ...)` | 回读一致，**图面空白** |
| `SetFrameSymbols2(1, "⌖", ...)` | 同上报错或被存成文本 |
| `IGtol.GetFrame()` | `(-2147352561, '非选择性的参数。')` — 必须传 frame 号 |
| `IGtol.GetFrame(1)` | 返回 `None` |
| `IGtol.GetFrameCount` | `1`（正常） |
| `IGtol.GetFrameSymbols2(1)` | 返回 tuple，能读出写入的内容（回读有效，但不代表渲染） |
| `IGtol.GetFrameValues(1)` | 返回 tuple，值正常 |
| `tolerance="⌖ 0.05"` + `symbol="none"` | `frame1` 有值，**图面只显示 `A`** |

**注**：`GetFrameValues` / `GetFrameSymbols2` 都是**属性还是方法**在 late-binding 下要小心——
本项目里 `IGtol.GetFrameCount` 是属性（不带括号），`GetFrameSymbols2(1)` 是方法（带括号）。

---

## 五、当前代码状态（v0.8.57 之后，未提交）

`add_geometric_tolerance` 里新增了 `_GD_T_GLYPHS` 表：

```python
_GD_T_GLYPHS = {
    "position": "\u2316",      # ⌖
    "concentricity": "\u25CE", # ◎
    "cylindricity": "\u232D",  # ⌭
    "perpendicularity": "\u22A5",  # ⊥
    "parallelism": "\u2225",   # ∥
    ...
}
```

行为：`symbol="position"` 时**不调 `SetFrameSymbols2`**，改为
`tolerance = "⌖ " + tolerance`，返回 `symbol_written: "⌖"`、`symbol_is_text: false`。

**但图面仍然不显示**（路径 C 的结论）。

**所以这段改动当前是"半成品"**：它让 PDF 文本层可选中 `⌖ 0.05`，
但没解决"图面上要有符号"这个真实需求。

**我不建议保留它**——它给人"已经支持符号"的错觉。等规划者定方向后再动。

---

## 六、请规划者判断的四件事

### Q1：`IGtol.GetFrame(1)` 返回 `None`，是绑定问题还是 API 问题？

可能的方向：

- `gencache.EnsureDispatch` 早绑定（`sldworks.tlb` 的 IID 是
  `{83A33D31-27C5-11CE-BFD4-00400513BB57}`，末两位取 SW 主版本）
- `win32com.client.dynamic.Dispatch` 换 `win32com.client.Dispatch`
- 直接 `QueryInterface` 到 `IGtolFrame`

**如果能拿到 `IGtolFrame`，`SetSymbolXml` 就是正路**——XML 里可以精确指定符号。

### Q2：模板字体能不能改？

GTol 的字体由 `gb_a3.drwdot` 的注释字体设置决定。

- 如果把 GTol 字体改成 `Segoe UI Symbol` / `Arial Unicode MS`，`⌖` 能否渲染？
- 改模板是"一次性设置"还是"每张图都要改"？
- 有没有可能**在代码里**改（`IModelDocExtension.SetUserPreferenceStringValue` 的字体项）？

**这是唯一不需要新 API 的路，值得先试。**

### Q3：`gtol.sym` 文件能直接用吗？

官方说符号定义在：

```
C:\ProgramData\SolidWorks\SolidWorks 20nn\lang\english\gtol.sym
```

本机路径应该是 `...\lang\chinese-simplified\gtol.sym`（中文版）。

- 这个文件的格式是什么？
- 能否用它索引出"符号名 → 索引号"，然后用**索引**而不是字符串调 `SetFrameSymbols2`？
- 2022+ 的 `IGtolFrame` XML 是否也用这个文件的符号名？

### Q4：如果都不行，"伪 GD&T"做到什么程度才够用？

兜底方案：`add_note` 单独放一个符号 + 一个只有数值的框格。

- 供应商是否接受"框格在旁边、符号是独立文字"这种形式？
- 还是必须"符号在框格内"？
- 如果必须在内，那**只能改模板字体**（Q2）

---

## 七、复现脚本

```python
import sys, os, shutil, tempfile
sys.path.insert(0, "src")
from solidworks_assetmate_mechdog.tools import drawing

SRC = "测试用例/飞达动力轴/飞达动力轴.SLDPRT"
work = tempfile.mkdtemp(prefix="gtol_")
part = os.path.join(work, "m.SLDPRT"); drw = os.path.join(work, "d.SLDDRW")
shutil.copy2(SRC, part)

try:
    drawing.create_drawing(part, output_path=drw, views=["front"], paper="A3")

    # 1) 中文符号（旧行为，回读一致但图面空白）
    a = drawing.add_geometric_tolerance(drw, 0.10, 0.15, tolerance="0.05",
                                        symbol="cylindricity", datum="A")
    print("CN :", a.get("symbol_written"), (a.get("readback") or {}).get("frame1"))

    # 2) Unicode 字形（数据进去但字体缺字形）
    b = drawing.add_geometric_tolerance(drw, 0.10, 0.10, tolerance="0.05",
                                        symbol="position", datum="A")
    print("UNI:", b.get("symbol_written"), (b.get("readback") or {}).get("frame1"))

    pdf = os.path.join(work, "out.pdf")
    if drawing.export_drawing(drw, pdf).get("ok"):
        drawing.render_drawing(pdf, output_dir=work, scale=3.0)
    print("WORKDIR:", work)
finally:
    drawing.close_document(drw); drawing.close_document(part)
```

**看图**：两个框格里应该都只有 `A`，没有任何符号。

---

## 八、影响面（为什么这是 P0）

反馈原文（`AssetMate_工程图改进需求_v0.8.54.md` 第 51 行）：

> 质检部门、CMM 测量程序、供应链图纸交换，全部认 GD&T 符号（⌖ ◎ ⊥ ∥）。
> 中文文字在工业语境下等于**未标注**，供应商可以拒收图纸，导致返工或订单流失。

**这不是"不好看"，是"图纸不成立"。**

而当前工具会返回 `ok: true` + `symbol_is_text: true`——**调用方很容易误以为成功了**。

0.8.49 已经加了 `symbol_render_unverified: true` 做提醒，但那只是标签，没解决问题。

---

## 九、我这一轮的判断

**三条路都堵了，其中一条是官方宣布的。**

- A 路径（`SetFrameSymbols2`）**不必再试**——文档说 2022+ 无效，实测确认
- B 路径（`IGtolFrame`）**取决于能否拿到对象**，这是唯一值得投入的技术方向
- C 路径（Unicode）**取决于模板字体**，这是唯一不依赖新 API 的方向

**最省事的验证**：打开 `gb_a3.drwdot`，把 GTol 字体改成 `Arial Unicode MS`，
再跑一次上面的脚本——如果 `⌖ 0.05` 出现了，整件事就解决了，不用碰 API。

**这个试验 5 分钟就能做，建议先做这个。**
