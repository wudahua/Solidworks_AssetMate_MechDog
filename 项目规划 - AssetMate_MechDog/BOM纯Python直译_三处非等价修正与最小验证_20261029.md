# BOM 纯 Python 直译：三处非等价修正与最小验证方案

> 针对《BOM纯Python直译_失败证据与未解矛盾_20261029.md》。
> 日期：2026-10-29。环境：SOLIDWORKS 2024 SP4 · Python 3.11 · pywin32 晚绑定。
> 目标：在不运行 `.swp` 的前提下，用 `IView.InsertBomTable4/5` 创建原生 BOM。
> 本文只给解决方案与参考代码，不修改项目文件。
> 说明：以下修正来自报告中的实际代码与官方枚举/示例对照；是否足以解决问题，需按本文最小脚本实测，**不预先宣称已成功**。

---

## 目录

1. [结论：当前实验并未严格复现宏](#1-结论当前实验并未严格复现宏)
2. [修正一：`doc.EditRebuild3` 没有执行](#2-修正一doceditrebuild3-没有执行)
3. [修正二：`AnchorType=0` 与宏不一致](#3-修正二anchortype0-与宏不一致)
4. [修正三：`BomType=1` 与宏不一致](#4-修正三bomtype1-与宏不一致)
5. [最小可行验证脚本](#5-最小可行验证脚本)
6. [Extension 入口为什么应从本轮排除](#6-extension-入口为什么应从本轮排除)
7. [“VBA 早绑定”前提需要修正](#7vba-早绑定前提需要修正)
8. [如果仍返回 None：按这个顺序排查](#8如果仍返回-none按这个顺序排查)
9. [表格回读也要修正：双通道判定](#9表格回读也要修正双通道判定)
10. [最终产品化调用结构](#10最终产品化调用结构)
11. [对现有报告与文档的修订建议](#11对现有报告与文档的修订建议)
12. [附录](#12附录)

---

## 1. 结论：当前实验并未严格复现宏

报告 §9.1 的“严格按宏顺序”代码，与宏至少有三处实质差异：

| 项 | 宏 | 当前 Python | 影响 |
|---|---|---|---|
| 重建 | VBA 中实际执行 `EditRebuild3` | `doc.EditRebuild3`（无括号） | **Python 没有调用方法，只取得了方法对象** |
| AnchorType | `swBOMConfigurationAnchor_TopLeft` | `0` | 官方示例中 TopLeft 为 **1**，0 不是宏的取值 |
| BomType | `swBomType_TopLevelOnly` | `1` | 官方 VBA 示例中 TopLevelOnly 为 **2**，1 是 PartsOnly |

因此目前不能据此得出：

- “纯 Python 直译不可行”；
- “SelectByRay 上下文建立成功但插入仍必被拒”；
- “必须先解决宏接口矛盾才能继续”。

**先把这三处改成与宏完全一致，再谈接口和绑定方式。**

---

## 2. 修正一：`doc.EditRebuild3` 没有执行

### 2.1 VBA 与 Python 的语法不同

VBA 允许无参数过程不写括号：

```vba
swModel.EditRebuild3
```

Python 不写括号不会调用方法：

```python
doc.EditRebuild3       # 只是方法对象/属性访问，不是执行重建
doc.EditRebuild3()     # 才真正执行
```

报告 §9.1 的可复现代码中，两次都是：

```python
doc.EditRebuild3
```

所以“插入后已重建”这一实验前提没有日志支持。

### 2.2 修正方式

```python
rebuild_ok = doc.EditRebuild3()
print("EditRebuild3:", rebuild_ok)

doc.GraphicsRedraw2()
```

如果当前封装里 `EditRebuild3` 是属性而不是方法，应以本机返回为准；但对 pywin32 的 COM 方法，标准写法是显式 `()`。

### 2.3 不能夸大它的作用

无括号问题**可能导致表格未刷新或未进入可回读状态**，但不一定能解释所有 `None`。
所以必须与下面两个参数修正一起测，不能单独改完就下新结论。

---

## 3. 修正二：`AnchorType=0` 与宏不一致

宏使用：

```vba
swBOMConfigurationAnchorType_TopLeft
```

官方 VBA 示例给出的枚举值：

```text
swBOMConfigurationAnchor_TopLeft     = 1
swBOMConfigurationAnchor_TopRight    = 2
swBOMConfigurationAnchor_BottomLeft  = 3
swBOMConfigurationAnchor_BottomRight = 4
```

当前 Python：

```python
view.InsertBomTable4(False, 0.24, 0.19, 0, 1, 'Default', tpl, False, 0, False)
#                                    ↑ AnchorType=0
```

应改为：

```python
anchor_type = 1  # TopLeft；最好使用本地常量而不是字面量
```

即使 `UseAnchorPoint=False`，也不应假设 `AnchorType` 可以传任意值。
**它是 API 的显式枚举参数，不是“被忽略参数就可以乱填”。**

---

## 4. 修正三：`BomType=1` 与宏不一致

### 4.1 官方示例的值

官方《Insert BOM Table and Get Data Example (VBA)》中列出：

```text
swBomType_PartsOnly    = 1
swBomType_TopLevelOnly = 2
swBomType_Indented     = 3
```

宏使用：

```vba
swBomType_TopLevelOnly
```

所以对应数值应为 **2**，不是 1。

当前 Python 传 `1`，实际是在请求 **PartsOnly**，不是宏的 TopLevelOnly。

### 4.2 为什么这个差异重要

- PartsOnly 与 TopLevelOnly 的装配展开行为不同。
- PartsOnly/Indented 对配置有效性有更明确要求。
- 当前实验声称“严格复现宏”，但 BOM 类型已经变了。

### 4.3 建议不要再用裸数字

```python
from win32com.client import gencache, constants

gencache.EnsureDispatch("SldWorks.Application")

anchor = constants.swBOMConfigurationAnchor_TopLeft
bom_type = constants.swBomType_TopLevelOnly
numbering = constants.swNumberingType_None
```

若本地常量名解析失败，先用本文数值完成最小验证，再查类型库；不要继续沿用“TopLevelOnly=1”的旧假设。

### 4.4 TopLevelOnly 时，Configuration 也不能照搬 'Default'

`IView.InsertBomTable2` 的官方 Remarks 明确说明：

> If BomType is `swBomType_TopLevelOnly`, then **do not specify Configuration**. Instead, use `IBomFeature::GetConfigurations` and `IBomFeature::SetConfigurations` to work with configurations in BOM tables.

因此，按宏的 **TopLevelOnly** 类型复现时，视图入口应传：

```python
Configuration = ''
```

这与 `IModelDocExtension`（模型侧）“必须指定配置”的规则不同，**两个入口的配置语义不能混用**。

修正后的核心组合应为：

```text
AnchorType = TopLeft(1)
BomType = TopLevelOnly(2)
Configuration = ''
```

---

## 5. 最小可行验证脚本

### 5.1 目的

只测文档明确的图纸入口 `IView.InsertBomTable4`，把宏的三个关键值修正，并真正执行重建。
**本轮不调用 `doc.Extension.InsertBomTable4`。**

### 5.2 代码【待测】

```python
import os
import win32com.client as win


def table_count(doc):
    tables = doc.GetFirstView.GetTableAnnotations
    return len(list(tables or ()))


def insert_bom_minimal(doc, view, template, configuration=""):
    # 与宏一致的关键取值
    USE_ANCHOR = False
    X, Y = 0.24, 0.19
    ANCHOR_TOP_LEFT = 1
    BOM_TOP_LEVEL_ONLY = 2
    NUMBERING_NONE = 0
    # TopLevelOnly 按 IView 官方规则不指定配置，因此默认 configuration=''

    doc.ActivateView(view.Name)
    doc.ClearSelection2(True)

    before = table_count(doc)

    table = view.InsertBomTable4(
        USE_ANCHOR,
        float(X), float(Y),
        ANCHOR_TOP_LEFT,
        BOM_TOP_LEVEL_ONLY,
        str(configuration),
        str(template),
        False,
        NUMBERING_NONE,
        False,
    )

    # 必须带括号，且记录返回值
    rebuild_ok = doc.EditRebuild3()
    doc.GraphicsRedraw2()

    after = table_count(doc)

    return {
        "table_returned": table is not None,
        "table_count_before": before,
        "table_count_after": after,
        "rebuild_ok": rebuild_ok,
        "table": table,
    }
```

### 5.3 调用后判读

| 结果 | 含义 | 下一步 |
|---|---|---|
| `table_returned=True` 或表格数增加 | 纯 Python 路径成立 | 做内容对账与新图纸复测 |
| 返回非 None，但表格数不变 | 回读路径不完整 | 用 §9 的 BomFeat 通道复核 |
| 返回 None 且表格数不变 | 继续单变量排查 | 先加选择上下文，再测 v5，不回到 Extension |
| 抛异常 | 记录原始 HRESULT | 修正签名/封送，不能归为静默拒绝 |

### 5.4 若需要 InsertBomTable5

仅在 v4 的修正版本仍失败时，保持其他参数不变，只换入口并追加最后一个参数：

```python
table = view.InsertBomTable5(
    False,
    0.24, 0.19,
    1,                 # TopLeft
    2,                 # TopLevelOnly
    "",                # TopLevelOnly 不指定 Configuration
    template,
    False,
    0,                 # NumberingNone
    False,             # DetailedCutList
    False,             # DissolvePartLevelRows
)
doc.EditRebuild3()
```

---

## 6. Extension 入口为什么应从本轮排除

报告继续使用：

```python
ext.InsertBomTable4(tpl, 0.24, 0.19, 1, 'Default', False, 0, False, False)
```

但官方对 `IModelDocExtension.InsertBomTable4` 的定义是：

> Inserts a bill of materials (BOM) table **in a part or assembly document**.

也就是**模型文档（零件/装配）里的 BOM**，不是图纸视图的明细栏。

当前 `doc` 是 `.SLDDRW`，所以：

- 该入口与目标文档类型不匹配；
- 返回 None 不能证明图纸 BOM 无法创建；
- 继续调整 Configuration 对当前问题没有诊断价值。

本轮只保留：

```text
IView.InsertBomTable4 / IView.InsertBomTable5
```

---

## 7. “VBA 早绑定”前提需要修正

报告 §5.3 写：

> VBA 是早绑定，ModelDoc2 上没有的方法会编译失败。

但宏代码是：

```vba
Dim swApp As Object
Set swApp = Application.SldWorks
Set swModel = swApp.ActiveDoc
```

这里：

- `swApp` 是 `Object`；
- `swModel` 没有显式声明为 `SldWorks.ModelDoc2`；
- 因此这是 **VBA 晚绑定**，编译器不会对 `swModel.InsertBomTable4` 做完整的接口成员校验。

所以“宏能编译”不能证明 `IModelDoc2` 上一定有这个成员。

不过，**宏运行时确实生成了表格**，仍说明宏中的某条实际调用路径有效。最可靠的查证方式不是继续猜，而是：

1. 在 SolidWorks 宏编辑器里直接打开 `.swp`，复制完整源码与所有 `Dim` 声明；
2. 核对插入那一行的接收对象到底是 `swModel`、`swView` 还是解压时识别错的标识符；
3. 若仍有矛盾，再用 comtypes 查接口。

**但在完成 §5 的最小修正实验之前，不必把类型库查证作为阻塞项。**

---

## 8. 如果仍返回 None：按这个顺序排查

### R1：修正参数 + 真正重建（先做）

即 §5，不添加射线。

目的：验证最简单的视图入口是否已经可用。

### R2：只增加有效几何上下文

保持 R1 的所有参数不变，仅加入：

```text
ActivateView
→ SelectByRay（11 参，返回 True）
→ 记录选择集
→ 按宏 ClearSelection2(True)
→ view.InsertBomTable4（同 R1）
→ EditRebuild3()
```

如果 R2 成功而 R1 失败，才能说“选择上下文是必要的”。

### R3：用几何对象替代射线

保持 R2，只把 `SelectByRay` 换成：

```python
view.SelectEntity(edge, False)
```

或：

```python
entity.Select4(False, None)
```

如果 R3 成功，则产品化不需要脆弱射线。

### R4：在原宏图纸上复现

若新图纸仍失败，回到 `BOM_TEST.SLDDRW`：

- 使用原视图名；
- 使用原射线参数；
- 使用修正后的 AnchorType/BomType；
- 真正执行重建。

用于区分“新图纸上下文”与“调用方式”。

### R5：最后才做绑定方式与接口考古

只有 R1–R4 都有完整日志且仍失败，才进入：

- `gencache.EnsureDispatch + CastTo`；
- comtypes vtable；
- 宏编辑器源码复核；
- RunCommand。

---

## 9. 表格回读也要修正：双通道判定

当前唯一判定：

```python
doc.GetFirstView.GetTableAnnotations
```

它可能漏掉“已创建在特征树、但尚未按预期挂到当前图纸视图”的对象。
建议同时走两条通道。

### 通道 A：图纸表格集合

```python
tables = doc.GetFirstView.GetTableAnnotations
count = len(list(tables or ()))
```

### 通道 B：特征树 BomFeat

```python
def bom_feature_count(doc):
    count = 0
    feat = doc.FirstFeature
    while feat is not None:
        if feat.GetTypeName2 == "BomFeat":
            count += 1
        feat = feat.GetNextFeature()
    return count
```

判读：

| 表格集合 | BomFeat | 含义 |
|---|---|---|
| 增加 | 增加 | 创建成功 |
| 不变 | 增加 | 已创建但回读位置不对，或尚未正确显示 |
| 不变 | 不变 | 确实未创建 |
| 增加 | 不变 | 可能是非 BOM 通用表格，不能算原生 BOM |

注意所有方法调用都要显式 `()`，属性则按本机 COM 映射处理。

---

## 10. 最终产品化调用结构

```text
1. 激活图纸文档与目标 sheet
2. 从 sheet.GetViews 取装配模型视图
3. 校验视图模型是装配，并取有效配置名
4. 按需建立几何/视图上下文（由 R1/R2 实验决定是否必须）
5. view.InsertBomTable4(
       UseAnchorPoint=False,
       X/Y 为图纸内米坐标,
       AnchorType=TopLeft(1),
       BomType=TopLevelOnly(2) 或调用方指定,
       Configuration=按 BomType 处理：TopLevelOnly 传 ''，PartsOnly/Indented 传真实配置名,
       TableTemplate=有效模板或 '',
       Hidden=False,
       IndentedNumberingType=None(0),
       DetailedCutList=False)
6. table 为 None 时，保持其他参数不变再退化/升级到 InsertBomTable5
7. doc.EditRebuild3() + doc.GraphicsRedraw2()
8. 双通道回读表格/BomFeat
9. 回读 RowCount、ColumnCount、逐行文本与 BomFeature
10. 与 export_bom 对账，做幂等处理
```

成功后再设置：

```python
table.BomFeature.FollowAssemblyOrder2 = True
```

---

## 11. 对现有报告与文档的修订建议

### 11.1 当前报告应收窄的结论

| 原结论 | 建议改为 |
|---|---|
| “五组严格按宏顺序全部失败” | “五组失败，但代码与宏在 AnchorType、BomType、重建调用上不等价” |
| “选择上下文建立成功，插入仍被拒” | “选择上下文已建立；插入参数与宏仍有差异，需修正后复测” |
| “VBA 是早绑定” | “该宏变量声明为 Object，属于晚绑定，编译期不能证明接口成员” |
| “先查接口矛盾，否则都是盲试” | “先完成最小等价修正；若仍失败，再查接口与绑定” |

### 11.2 `com-quirks.md` 应新增

| # | 条目 |
|---|---|
| BOM-16 | Python 调用 COM 方法必须写 `()`：`doc.EditRebuild3` 不执行，`doc.EditRebuild3()` 才执行 |
| BOM-17 | `swBOMConfigurationAnchor_TopLeft=1`，不要把 AnchorType 填 0 |
| BOM-18 | 官方 VBA 示例：`PartsOnly=1`、`TopLevelOnly=2`、`Indented=3`；不要沿用旧猜测 |
| BOM-19 | `IModelDocExtension.InsertBomTable4` 用于零件/装配文档，不用于图纸明细栏 |
| BOM-20 | 表格创建判定要同时查 `GetTableAnnotations` 与特征树 `BomFeat` |

---

## 12. 附录

### 12.1 宏与修正后 Python 对照

| 参数 | 宏 | 修正后 |
|---|---|---|
| 图纸入口 | 视图 BOM 插入 | `view.InsertBomTable4` |
| UseAnchorPoint | False | False |
| X/Y | 0.24 / 0.19（按录制值） | 同左 |
| AnchorType | TopLeft | **1** |
| BomType | TopLevelOnly | **2** |
| Configuration | TopLevelOnly 不指定 | `''` |
| Template | `bom-standard.sldbomtbt` | 同左 |
| Hidden | False | False |
| Numbering | 0 | 0 |
| DetailedCutList | False | False |
| Rebuild | 实际执行 | `doc.EditRebuild3()` |

### 12.2 参考来源

- SOLIDWORKS API Help《Insert BOM Table and Get Data Example (VBA)》：AnchorType 与 BomType 的枚举值、视图 BOM 回读方式。
- SOLIDWORKS API Help《IModelDocExtension.InsertBomTable4》：模型侧 BOM 入口及配置说明。
- SOLIDWORKS API Help《IView.InsertBomTable4/5》：图纸视图明细栏入口。
- 原始实验：《BOM纯Python直译_失败证据与未解矛盾_20261029.md》。
