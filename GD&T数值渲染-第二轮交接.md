# GD&T 数值渲染：现状与卡点（第二轮交接）

> 承接《GD&T符号渲染_复核与IGtolFrame解决方案_20261102.md》。
> **符号已解决并发布（v0.8.58）**，本文只讲剩下的那一项：**公差数值不渲染**。
>
> 日期：2026-11-02 · 环境：SOLIDWORKS 2024 SP4（32.4.0）· pywin32

---

## 一、已经解决的（不用再看）

按你文档的三步序列，**符号和基准都渲染出来了**：

```python
call_method(gtol, "ConvertFormat")     # GetFormat 1 → 2
call_method(gtol, "AddFrame")          # ConvertFormat 清空了帧，必须补
frame = win.Dispatch(call_method(gtol, "GetFrame", 1))
call_method(frame, "SetSymbolXml", xml)   # → True
```

**图面实测**：框格里显示 **`⌖ A`** —— 位置度符号 + 基准字母，都是原生的。

你文档里的两个关键点全部验证正确：

| 你说的 | 实测 |
|---|---|
| 先 `ConvertFormat` 再 `AddFrame`，否则 `GetFrame` 返回 None | ✅ 完全一致 |
| XML 符号名不能带尖括号，用 `IGTOL-POSI` | ✅ 带括号返回 False，不带返回 True |

已发布 v0.8.58，离线 101/101、真机 smoke 35/35。

---

## 二、没解决的：数值 `0.05` 不显示

### 2.1 现象

框格最终只显示 **`⌖ A`**：

```
[ ⌖ ] [    ] [ A ]
  ↑     ↑     ↑
 有   空!    有
```

第二格（数值格）是空的。

### 2.2 两个 API 都说成功

**XML 路径**：

```xml
<GtolFrame>
  <ToleranceSymbol>IGTOL-POSI</ToleranceSymbol>
  <ToleranceRangeInfo>
    <PrimaryToleranceValue>0.05</PrimaryToleranceValue>
    <PrimaryRangeSymbol></PrimaryRangeSymbol>
  </ToleranceRangeInfo>
  <DatumCompartment><DatumDetail><DatumLetter>A</DatumLetter></DatumDetail></DatumCompartment>
  ...
</GtolFrame>
```

- `SetSymbolXml(xml)` → `True`
- `GetSymbolXml()` → **完整读回上面的 XML，`0.05` 在里面**

**旧 values API**：

```python
call_method(gtol, "SetFrameValues2", 1, "0.05", "", "A", "", "")
# → True
```

### 2.3 试过的两种组合，都不显示

| 组合 | 结果 |
|---|---|
| **只写 XML**（不调 `SetFrameValues2`） | 符号 ✅ 基准 ✅ **数值 ❌** |
| **XML + `SetFrameValues2`** | 符号 ✅ 基准 ✅ **数值 ❌** |

**两种都一样：`⌖ A`。**

### 2.4 关键矛盾

`GetSymbolXml` 读回的内容**和我们写进去的一模一样**——说明：

- **XML 被接受了、存住了**
- **渲染时数值字段被忽略**

这不是"数据没进去"，是"数据进去了但不画"。

---

## 三、我最怀疑的三件事

### 怀疑 1：`PrimaryToleranceValue` 不是渲染用的字段

我用的 XML 是**照你文档的骨架**写的，元素名 `PrimaryToleranceValue` 是对的（`SetSymbolXml` 接受它）。

但"接受"和"渲染"是两回事。可能：

- 真正的显示值在另一个元素（如 `ToleranceValueDisplay`、`ValueText`）
- 或者需要 `PrimaryRangeSymbol` 非空才触发数值渲染
- 或者需要 `<ToleranceZone>` / `<ToleranceType>` 之类的兄弟节点

**验证方法**：**读一个 UI 手工建的正确框格的 XML**（见第四节）。

### 怀疑 2：`SetFrameValues2` 与 XML 互相覆盖

`SetFrameValues2` 是**旧格式 API**。在一个已转到 2022 格式（`GetFormat = 2`）的对象上调用它，可能：

- 静默失败（返回 `True` 但不生效）
- 或把帧改回旧格式，与 XML 冲突

**我试过"只 XML"**，数值仍不显示——所以**不是覆盖问题**，至少不全是。

### 怀疑 3：SW2024 SP4 的渲染缺陷

`GetFormat = 2` 的对象在 SW2024 上**数值不渲染**，可能是这个版本的 bug。

**验证方法**：换一台 SW2025/2026 跑同一个脚本。反馈方的机器是 SW2026（34.0.0），如果那边显示正常，就是这个版本的缺陷。

---

## 四、请你帮的两件事（按性价比排序）

### ① 给我一个 UI 手工建的框格 XML（最有效）

在 SW 界面里：

1. **插入 → 注解 → 形位公差**
2. 选 **位置度** 符号
3. 数值填 **0.05**
4. 基准填 **A**
5. 放到图纸上、确定
6. 然后用这段代码把这个框格的 XML 读出来：

```python
import sys, win32com.client as win
sys.path.insert(0, "src")
from solidworks_assetmate_mechdog.tools import drawing
from solidworks_assetmate_mechdog.sw_session import call_method

doc = drawing._open_drawing("你的图纸.SLDDRW")
for view in drawing._iter_views(doc):
    for raw in (view.GetAnnotations or ()):
        ann = win.Dispatch(raw)
        if int(ann.GetType) != 5:      # 5 == gtol annotation
            continue
        gtol = win.Dispatch(ann.GetSpecificAnnotation)
        frame = win.Dispatch(call_method(gtol, "GetFrame", 1))
        print(ann.GetName)
        print(call_method(frame, "GetSymbolXml"))
```

**把打印出来的 XML 贴回来。** 对比它和我上面那份，差异就是答案。

**这一步比我猜十个字段名都快。**

### ② 换 SW 版本试一次

同一份脚本在 SW2025/2026 上跑一次。如果那边数值显示正常 → 是 SW2024 的 bug，可以只写文档说明。

---

## 五、复现脚本

```python
import sys, os, shutil, tempfile
sys.path.insert(0, "src")
import win32com.client as win
from solidworks_assetmate_mechdog.tools import drawing
from solidworks_assetmate_mechdog.sw_session import call_method

SRC = "测试用例/飞达动力轴/飞达动力轴.SLDPRT"
work = tempfile.mkdtemp(prefix="gtol_value_")
part = os.path.join(work, "m.SLDPRT"); drw = os.path.join(work, "d.SLDDRW")
shutil.copy2(SRC, part)

try:
    drawing.create_drawing(part, output_path=drw, views=["front"], paper="A3")
    doc = drawing._open_drawing(drw)

    gtol = win.Dispatch(drawing._invoke_as_method(doc, "InsertGtol"))
    call_method(gtol, "ConvertFormat")
    if int(call_method(gtol, "GetFrameCount")) == 0:
        call_method(gtol, "AddFrame")
    frame = win.Dispatch(call_method(gtol, "GetFrame", 1))

    xml = (
        "<GtolFrame><ToleranceSymbol>IGTOL-POSI</ToleranceSymbol>"
        "<ToleranceRangeInfo>"
        "<PrimaryToleranceValue>0.05</PrimaryToleranceValue>"
        "<PrimaryRangeSymbol></PrimaryRangeSymbol>"
        "</ToleranceRangeInfo>"
        "<DatumCompartment><DatumDetail><DatumLetter>A</DatumLetter></DatumDetail></DatumCompartment>"
        "<DatumCompartment></DatumCompartment>"
        "<DatumCompartment></DatumCompartment>"
        "</GtolFrame>"
    )
    print("SetSymbolXml:", call_method(frame, "SetSymbolXml", xml))
    print("readback   :", call_method(frame, "GetSymbolXml"))

    ann = win.Dispatch(call_method(gtol, "GetAnnotation"))
    call_method(ann, "SetPosition", 0.10, 0.10, 0.0)
    call_method(doc, "ForceRebuild3", False)
    doc.GraphicsRedraw2()

    pdf = os.path.join(work, "out.pdf")
    if drawing.export_drawing(drw, pdf).get("ok"):
        drawing.render_drawing(pdf, output_dir=work, scale=4.0)
    print("WORKDIR:", work)
finally:
    drawing.close_document(drw); drawing.close_document(part)
```

**预期输出**：`SetSymbolXml: True`、`readback` 里含 `0.05`、**渲染图里框格只有 `⌖ A`**。

---

## 六、当前工具行为（v0.8.58 已发布）

```json
{
  "ok": true,
  "symbol_written": "IGTOL-POSI",
  "symbol_is_text": false,
  "symbol_render_verified": true,     // 符号确实画出来了
  "value_render_unverified": true,    // 数值没画出来（如实标记）
  "note": "... 但公差数值当前不渲染 ..."
}
```

**没有谎报。** 调用方看到 `value_render_unverified: true` 就知道数值要手工补。

**但这仍然不够**——一张 `⌖` 后面没有数值的形位公差，在车间是**不完整的**。
所以这一项我按 P0 对待，不当作"已知限制"收尾。

---

## 七、我的判断

**最可能的答案在第四节①**：UI 手工框格的 XML 会直接暴露正确的数值字段。

如果那份 XML 和我这份**结构一致**、只是数值不显示 → **SW2024 渲染缺陷**，写文档即可。

如果那份 XML **结构不同**（多了/换了个元素）→ **改一行 XML 就解决**。

**两种情况都只需要一次 30 秒的界面操作。** 请优先做这个。
