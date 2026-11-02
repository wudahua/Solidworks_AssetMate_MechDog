# GD&T 符号渲染：真实性复核与 IGtolFrame 解决方案

> 针对《GD&T符号渲染-现状与卡点.md》。
> 日期：2026-11-02。实测环境：SOLIDWORKS 2024 SP4（32.4.0）· Python + pywin32。
> 本文包含本机实机验证结果，明确区分“已解决”与“尚未闭合”。

---

## 1. 问题真实，但“三条路径全部堵死”不成立

原现象成立：

- `SetFrameSymbols2` 能回读中文或 Unicode 文本，但图面不显示真正的 GD&T 符号；
- `IGtol.GetFrame(1)` 返回 `None`；
- Unicode 字形路径依赖字体，不应作为原生 GD&T 方案。

但本机已验证：**2022+ 的 `IGtolFrame + XML` 路径可用，位置度符号和基准 A 已实际渲染到 PDF。**

因此不应再把 B 路径判为不可用，也不必继续走中文文字或 Unicode 兜底。

---

## 2. 已实测成功的核心序列

### 2.1 `GetFrame(1)=None` 的真正原因

新建 GTol 时：

```text
GetFormat = 1（旧格式）
CanConvertFormat = True
GetFrame(1) = None
```

执行 `ConvertFormat` 后：

```text
ConvertFormat 返回 0（成功）
GetFormat = 2（2022 格式）
但 GetFrameCount = 0
```

所以后续必须：

```text
AddFrame → GetFrame(1)
```

实测 `AddFrame=True` 后，`GetFrame(1)` 返回真正的 `IGtolFrame` 对象。

**关键点：不是索引错误，也不是单纯绑定问题，而是格式转换后帧数为 0，必须先补帧。**

### 2.2 `SetSymbolXml` 的符号名不能带尖括号

本机 XSD：`data/xmlschema/swGtolFrameXmlSchema-public-2.xsd` 第 325 行明确写着：

> Tolerance symbol value string should not include the angle brackets.

正确：

```xml
<ToleranceSymbol>IGTOL-POSI</ToleranceSymbol>
```

错误：

```xml
<ToleranceSymbol><IGTOL-POSI></ToleranceSymbol>
<ToleranceSymbol>&lt;IGTOL-POSI&gt;</ToleranceSymbol>
```

`SetSymbolXml` 对正确形式返回 `True`，对错误形式返回 `False`。

### 2.3 已验证的图面结果

本机生成并放大检查后：

- 位置度符号 **⌖** 已显示；
- 基准 **A** 已显示。

这证明：**原生 GD&T 符号渲染路径已打通，不需要 Unicode 字符代替。**

---

## 3. 推荐实现

### 3.1 最小可用代码骨架

```python
import win32com.client as win


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


# 1) 创建 GTol 并转换到 2022 格式
gtol = win.Dispatch(call_method(doc, "InsertGtol"))

# 老格式对象先转换；已转换文档可按版本情况跳过
if call_method(gtol, "CanConvertFormat"):
    call_method(gtol, "ConvertFormat")

# 转换后原帧可能被清空，必须补帧
if int(call_method(gtol, "GetFrameCount")) == 0:
    call_method(gtol, "AddFrame")

frame = win.Dispatch(call_method(gtol, "GetFrame", 1))

# 2) 构造完整 XML：符号名不带尖括号
xml = (
    "<GtolFrame>"
    "<ToleranceSymbol>IGTOL-POSI</ToleranceSymbol>"
    "<ToleranceRangeInfo>"
    "<PrimaryToleranceValue>0.05</PrimaryToleranceValue>"
    "<PrimaryRangeSymbol></PrimaryRangeSymbol>"
    "</ToleranceRangeInfo>"
    "<DatumCompartment>"
    "<DatumDetail><DatumLetter>A</DatumLetter></DatumDetail>"
    "</DatumCompartment>"
    "<DatumCompartment></DatumCompartment>"
    "<DatumCompartment></DatumCompartment>"
    "</GtolFrame>"
)

ok = call_method(frame, "SetSymbolXml", xml)
assert ok, "SetSymbolXml rejected the frame XML"

# 3) 定位并重建
ann = win.Dispatch(call_method(gtol, "GetAnnotation"))
call_method(ann, "SetPosition", 0.12, 0.10, 0.0)
call_method(doc, "ForceRebuild3", False)
```

### 3.2 符号名映射

官方 `gtol.sym` 使用 `LibraryName-SymbolName`，XML 中**原样写入但不加尖括号**：

| 语义 | XML 中的 ToleranceSymbol |
|---|---|
| 位置度 | `IGTOL-POSI` |
| 圆柱度 | `IGTOL-CYL` |
| 垂直度 | `IGTOL-PERP` |
| 平行度 | `IGTOL-PARA` |
| 同轴度 | `IGTOL-CONC` |
| 平面度 | `IGTOL-FLAT` |
| 圆度 | `IGTOL-ROUND` |

其他符号按官方 14 个名称使用：`ANGULAR / CIRC / CONC / CYL / FLAT / LPROF / PARA / PERP / POSI / SPROF / SRUN / STRAIGHT / SYMMETRY / TRUN`。

---

## 4. 原三条路径如何处理

### A. `SetFrameSymbols2`：停止用于 2022+

官方限制成立，不再投入。
不要用中文、`<MOD-*>` 或 Unicode 往旧接口里塞符号。

### B. `IGtolFrame`：采用，但必须补齐三步

```text
ConvertFormat → AddFrame → 正确的 XML
```

只做 `GetFrame` 而不做格式转换和补帧，仍会得到 `None`。

### C. Unicode 字体：不再作为主路径

已有原生 XML 符号方案，不需要改模板字体，也不需要把 `⌖` 当普通文本写入。

---

## 5. 尚未完全闭合的部分：公差数值显示

本机同时发现：

- `SetSymbolXml` 返回 `True`；
- XML 中存在 `<PrimaryToleranceValue>0.05</PrimaryToleranceValue>`；
- 但当前 PDF 中仍未显示 `0.05`，只显示了符号与基准 A。

因此目前只能宣称：

> **GD&T 符号渲染已解决；完整“符号 + 数值 + 基准”框格还需增加数值渲染验证，不能提前标记全部成功。**

建议下一步集中检查：

1. `SetFrameValues2` 与 `SetSymbolXml` 的调用顺序，避免新旧 API 混写同一帧；
2. 使用单一完整 XML 写入符号、数值和基准，不再调用旧 `SetFrameSymbols2`；
3. 检查 SW2024 SP4 对新 GTol 框架数值字段的显示行为，必要时对照 UI 手工框格 XML；
4. 导出 PDF 后同时断言符号、数值、基准三项，而不是只回读 XML。

---

## 6. 交付门禁必须同步修改

当前 `ok:true + symbol_written` 不能证明图面正确。
建议 `add_geometric_tolerance` 最后增加：

```text
符号渲染检查
数值渲染检查
基准渲染检查
```

失败时分别返回：

```text
gtol_symbol_missing
gtol_value_missing
gtol_datum_missing
```

只有 PDF 中三项都可见，才能返回 `ok:true`。
`GetSymbolXml` 回读只能证明数据写入，不能证明渲染完成。

---

## 7. 最终结论

- 问题真实，且会直接导致图纸不可用。
- **B 路径并未堵死**：`IGtolFrame + XML` 已在 SW2024 SP4 实测画出位置度符号。
- `GetFrame=None` 的解法是 **先转换格式，再 AddFrame**。
- XML 符号名必须写成 `IGTOL-POSI`，**不能带 `< >`**。
- 不要再使用中文/Unicode 替代原生符号。
- 数值 `0.05` 的渲染仍需单独验收，不能仅凭 XML 回读或 `ok:true` 判定完整框格成功。
