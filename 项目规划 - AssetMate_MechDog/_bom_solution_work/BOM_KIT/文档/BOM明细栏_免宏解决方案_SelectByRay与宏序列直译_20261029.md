# BOM 明细栏免宏解决方案：直译宏序列 + 修正 SelectByRay 签名

> 针对《BOM明细栏_宏驱动破局_完整记录_20261029.md》。
> 编写日期：2026-10-29。环境：SOLIDWORKS 2024 SP4 · Python 3.11 · pywin32 晚绑定。
> 目标：**不运行 `.swp`、不依赖 `RunMacro`，用纯 Python COM 创建原生 BOM 明细栏。**
> 本文只提供解决方案与参考实现，不修改项目代码。
> 证据等级：【官方】= SOLIDWORKS API Help；【宏实测】= 本次录制宏与运行结果；【待测】= 参考实现，需真机验证。

---

## 目录

1. [结论：不必须用宏](#1-结论不必须用宏)
2. [这份记录里最重要的两个新发现](#2-这份记录里最重要的两个新发现)
3. [修正一：SelectByRay 是 11 个参数，没有 Callout](#3-修正一selectbyray-是-11-个参数没有-callout)
4. [修正二：宏用的是 doc.InsertBomTable4，不是 view.InsertBomTable4/5](#4-修正二宏用的是-docinsertbomtable4不是-viewinsertbomtable45)
5. [方案 A：逐句直译宏（最高保真，优先验证）](#5-方案-a逐句直译宏最高保真优先验证)
6. [方案 B：用几何对象选择替代 SelectByRay（推荐的最终形态）](#6-方案-b用几何对象选择替代-selectbyray推荐的最终形态)
7. [方案 C：不选择几何，直接调用文档级 InsertBomTable4](#7-方案-c不选择几何直接调用文档级-insertbomtable4)
8. [完整参考实现](#8-完整参考实现)
9. [实验矩阵：一次只改一个变量](#9-实验矩阵一次只改一个变量)
10. [回读、幂等与交付判定](#10-回读幂等与交付判定)
11. [如果纯 Python 仍失败](#11-如果纯-python-仍失败)
12. [对现有文档与代码的修改建议](#12-对现有文档与代码的修改建议)
13. [附录](#13-附录)

---

## 1. 结论：不必须用宏

### 1.1 直接回答

**不必须。**

要区分两件事：

| 概念 | 是否必须 | 作用 |
|---|---|---|
| **宏录制** | 已经完成，不再是依赖 | 用来发现 SolidWorks 真实调用序列 |
| **`RunMacro` 运行 `.swp`** | **不必须** | 只是重放调用序列的一种方式 |

宏本身没有特殊能力。它仍然通过同一套 SolidWorks COM API 工作：

```text
ActivateView
→ SelectByRay
→ ClearSelection2
→ InsertBomTable4
→ EditRebuild3
```

这段序列可以在 Python 中逐句执行。**只要接口、参数个数、参数顺序和调用上下文完全一致，就不需要宏文件。**

### 1.2 当前真正卡住的不是“宏能力”，而是两个签名问题

1. `SelectByRay` 调用时**多传了一个 `Callout` 参数**，导致 `DISP_E_TYPEMISMATCH`。
2. 宏最后实际调用的是 **`swModel.InsertBomTable4` 的 8 参数形式**，而此前 Python 主要尝试的是 `view.InsertBomTable4/5` 的 10/11 参数形式。**这是不同入口，不能混用参数表。**

因此下一步不是参数化 `.swp`，而是**把宏精确翻译成 Python**。

---

## 2. 这份记录里最重要的两个新发现

### 2.1 宏给出了一个此前没有测试的入口

宏代码【宏实测】：

```vba
swModel.InsertBomTable4( _
    0.24..., 0.19..., _
    swBOMConfigurationAnchorType_TopLeft, _
    swBomType_TopLevelOnly, _
    "...\bom-standard.sldbomtbt", _
    False, 0, False)
```

与此前的调用对比：

| 入口 | 参数数 | 是否含 `UseAnchorPoint` | 是否含 `Configuration` | 本次是否已试 |
|---|---:|---|---|---|
| `view.InsertBomTable4` | 10 | 有 | 有 | 已试，None |
| `view.InsertBomTable5` | 11 | 有 | 有 | 已试，None |
| **`doc.InsertBomTable4`（宏中的 `swModel`）** | **8** | **无** | **无** | **未见明确测试记录** |

这意味着此前“7 条 API × 20+ 组合全部失败”**并没有覆盖宏实际使用的那个调用形式**。

> 注意：当前官方在线文档主要列出 `IView.InsertBomTable4/5` 与 `IModelDocExtension.InsertBomTable4`。宏中的文档级 8 参入口应以本机类型库和 `GetIDsOfNames` 复核，不要再用其他入口的参数表套它。

### 2.2 `SelectByRay` 的官方签名与本次 Python 调用不一致

官方 `IModelDocExtension.SelectByRay`【官方】：

```text
SelectByRay(
  WorldX, WorldY, WorldZ,
  RayVecX, RayVecY, RayVecZ,
  RayRadius,
  TypeWanted,
  Append,
  Mark,
  Option
) → bool
```

**总共 11 个参数，没有 `Callout`。**

本次 Python 调用【宏实测】：

```python
doc.Extension.SelectByRay(
    cx, cy, 0.0,
    0.0, 0.0, 0.0,
    0.002, 2, False, 0,
    byref_dispatch_null(), 0)
#   ↑ 多出来的 Callout
```

**删掉 `byref_dispatch_null()`，就是官方的 11 参形式。**

```python
doc.Extension.SelectByRay(
    cx, cy, cz,
    dx, dy, dz,
    radius, type_wanted,
    append, mark, option)
```

这很可能直接解决当前 `TYPEMISMATCH`，无需尝试 `VT_ARRAY`、手动 `Invoke` 或全部参数逐个封送。

---

## 3. 修正一：SelectByRay 是 11 个参数，没有 Callout

### 3.1 不要与 SelectByID2 混淆

| 方法 | 是否有 `Callout` | 末尾结构 |
|---|---|---|
| `SelectByID2` | **有** | `Append, Mark, Callout, Option` |
| `SelectByRay`（当前 `IModelDocExtension`） | **没有** | `Append, Mark, Option` |

本次错误正是把 `SelectByID2` 的参数习惯套到了 `SelectByRay` 上。

### 3.2 正确封装【待测】

```python
def select_by_ray(doc, start, direction, radius,
                  type_wanted=2, append=False, mark=0, option=0):
    """IModelDocExtension.SelectByRay，严格 11 参。"""
    x, y, z = map(float, start)
    dx, dy, dz = map(float, direction)
    return doc.Extension.SelectByRay(
        float(x), float(y), float(z),
        float(dx), float(dy), float(dz),
        float(radius),
        int(type_wanted),
        bool(append),
        int(mark),
        int(option),
    )
```

### 3.3 调用后必须验证，而不是只看异常

```python
ok = select_by_ray(doc, start, direction, radius=0.002)
print("SelectByRay returned:", ok)

sel = doc.SelectionManager
print("selected count:", sel.GetSelectedObjectCount)
```

判读：

| 结果 | 含义 | 下一步 |
|---|---|---|
| 不再报 `TYPEMISMATCH`，返回 `True` | 签名问题已解决 | 继续调用 BOM 插入 |
| 不再报错，但返回 `False` | 调用成功，射线没打到目标 | 调坐标/方向/半径，或改方案 B |
| 仍报参数错误 | 本机签名与当前官方页面不一致 | 用本机类型库复核，不再盲目加参数 |
| 返回 `True`，但选择集为 0 | 上下文或选择状态异常 | 打印活动文档、视图、选择管理器状态 |

### 3.4 坐标的可移植性限制

官方 Remarks 提醒【官方】：

- 射线只选择面、边、顶点等几何，不选择草图实体、中心线等。
- 图纸中的射线方向需要按模型空间视图确定。
- 依赖坐标的选择在视图变化后可能失效。

所以：

- **方案 A** 适合先证明“宏可以直译为 Python”。
- **方案 B** 才适合任意图纸的产品化，不要长期依赖录制下来的固定射线参数。

---

## 4. 修正二：宏用的是 doc.InsertBomTable4，不是 view.InsertBomTable4/5

### 4.1 参数表不能混用

宏中的形式【宏实测】：

```text
doc.InsertBomTable4(
  X, Y,
  AnchorType,
  BomType,
  TableTemplate,
  Hidden,
  IndentedNumberingType,
  DetailedCutList
)
```

它是 **8 个参数**：

- 没有 `UseAnchorPoint`
- 没有 `Configuration`

而 `IView.InsertBomTable4` 是 10 个参数，包含上述两项。此前如果把 8 参形式当成视图入口的简化版，就会继续得到错误结论。

### 4.2 先在本机确认成员与真实调用形式【待测】

```python
def has_member(obj, name):
    try:
        obj._oleobj_.GetIDsOfNames(name)
        return True
    except Exception:
        return False

print("doc.InsertBomTable4:", has_member(doc, "InsertBomTable4"))
print("view.InsertBomTable4:", has_member(view, "InsertBomTable4"))
print("view.InsertBomTable5:", has_member(view, "InsertBomTable5"))
```

然后**优先按宏原样调用文档级 8 参入口**，不要再加 `UseAnchorPoint` 或 `Configuration`。

### 4.3 一个必须保持的实验纪律

不要在一次实验里同时改以下三项：

```text
SelectByRay 参数个数
+ BOM 入口（doc/view）
+ 是否清空选择集
```

否则即使成功也不知道是哪一项起作用，失败也无法归因。按 §9 的顺序做。

---

## 5. 方案 A：逐句直译宏（最高保真，优先验证）

### 5.1 目标

先在**与宏录制相同的图纸、相同的视图**上复现。此时不改坐标、不改入口、不清除任何宏中的步骤。

### 5.2 调用顺序

```text
1. 确保目标图纸是活动文档
2. doc.ActivateView(宏中的视图名)
3. doc.Extension.SelectByRay(宏中的起点、方向、半径、类型；严格 11 参)
4. 回读 SelectByRay 返回值和选择集数量
5. doc.ClearSelection2(True)              ← 宏里确实有，先照抄
6. doc.InsertBomTable4(8 参，按宏顺序)
7. doc.EditRebuild3()
8. 回读图纸表格数量、行数、列数
```

### 5.3 参考代码【待测】

```python
def insert_bom_exact_macro(doc, view_name,
                           ray_start, ray_direction, ray_radius,
                           table_x, table_y, table_template,
                           anchor_type=1, bom_type=1,
                           numbering_type=0):
    doc.ActivateView(view_name)

    selected = select_by_ray(
        doc,
        start=ray_start,
        direction=ray_direction,
        radius=ray_radius,
        type_wanted=2,
        append=False,
        mark=0,
        option=0,
    )
    print("ray selected:", selected)

    # 宏在插入前清空了选择集，第一轮严格照抄
    doc.ClearSelection2(True)

    table = doc.InsertBomTable4(
        float(table_x), float(table_y),
        int(anchor_type),
        int(bom_type),
        str(table_template),
        False,
        int(numbering_type),
        False,
    )
    doc.EditRebuild3()
    return table
```

### 5.4 第一轮不要做的“优化”

- 不要把 `doc.InsertBomTable4` 换成 `view.InsertBomTable5`。
- 不要把射线坐标改成视图中心。
- 不要删掉 `ClearSelection2`。
- 不要换模板、BomType 或 AnchorType。
- 不要把 `Configuration` 塞进这个 8 参入口。

**先证明直译可行，再逐项简化。**

---

## 6. 方案 B：用几何对象选择替代 SelectByRay（推荐的最终形态）

### 6.1 为什么推荐

`SelectByRay` 的坐标和方向是从一次具体 UI 操作中录出来的，天然依赖：

- 视图位置与比例
- 模型方向
- 当前图形区状态
- 射线是否恰好打到有效面/边

而项目已经有稳定的可见几何枚举能力，可以直接拿到视图内的边对象，再调用：

```python
view.SelectEntity(edge, False)
```

或：

```python
entity.Select4(False, None)
```

这样不需要射线，也不需要屏幕坐标。

### 6.2 推荐流程

```text
激活图纸与目标视图
→ view.GetVisibleComponents
→ view.GetVisibleEntities2(comp, 1) 取可见边
→ 选第一条有效边（或按几何规则选边）
→ view.SelectEntity(edge, False) / entity.Select4(False, None)
→ 回读选择集
→ 按宏顺序 ClearSelection2(True)
→ doc.InsertBomTable4(8 参)
→ EditRebuild3 + 回读表格
```

### 6.3 参考代码【待测】

```python
import win32com.client


def select_first_visible_edge(view):
    comps = view.GetVisibleComponents or []
    for comp in comps:
        try:
            edges = view.GetVisibleEntities2(comp, 1)  # 1 = edge
        except Exception:
            continue
        for edge in edges or []:
            # 优先使用 IView.SelectEntity；失败退化 IEntity.Select4
            try:
                if view.SelectEntity(edge, False):
                    return edge
            except Exception:
                pass
            try:
                entity = win32com.client.Dispatch(edge)
                if entity.Select4(False, None):
                    return edge
            except Exception:
                pass
    return None


def insert_bom_with_geometry_context(doc, view,
                                     table_x, table_y, table_template,
                                     anchor_type=1, bom_type=1,
                                     numbering_type=0,
                                     clear_before_insert=True):
    doc.ActivateView(view.Name)
    doc.ClearSelection2(True)

    edge = select_first_visible_edge(view)
    if edge is None:
        raise RuntimeError("未能选中视图内几何，无法复现宏的选择上下文")

    print("selected count:", doc.SelectionManager.GetSelectedObjectCount)

    if clear_before_insert:
        doc.ClearSelection2(True)

    table = doc.InsertBomTable4(
        float(table_x), float(table_y),
        int(anchor_type), int(bom_type),
        str(table_template), False,
        int(numbering_type), False,
    )
    doc.EditRebuild3()
    return table
```

### 6.4 选择后的 ClearSelection2 要单独做对照

宏里是“选择几何 → 清空选择 → 插入”。但其他入口可能要求“视图仍处于选中状态”。因此方案 B 要跑两组：

| 组 | 插入前选择状态 | 目的 |
|---|---|---|
| B1 | 按宏执行 `ClearSelection2(True)` | 验证是否只需要“曾经建立过视图上下文” |
| B2 | 保持几何/视图选中，不清空 | 验证插入是否要求当前选择 |

**不要默认宏里的 ClearSelection2 对所有入口都适用。**

---

## 7. 方案 C：不选择几何，直接调用文档级 InsertBomTable4

### 7.1 目的

验证 `SelectByRay` 到底是**必要条件**，还是只是宏录制顺带记录下来的 UI 动作。

当前记录把“宏能成、API 不能”主要归因于 `SelectByRay`，但这个结论还没有完全证明，因为宏同时使用了**不同的 BOM 入口**。

所以必须做一个最小对照：

```python
doc.ActivateView(view.Name)
doc.ClearSelection2(True)
table = doc.InsertBomTable4(x, y, anchor, bom_type, template, False, 0, False)
doc.EditRebuild3()
```

### 7.2 判读

| 结果 | 结论 |
|---|---|
| 成功 | **SelectByRay 不是必要条件**；真正差异是文档级 8 参入口 |
| 返回 None，方案 A 成功 | SelectByRay 建立的上下文确实必要 |
| 返回 None，方案 B 成功 | 不一定需要射线，只需要几何/视图上下文 |
| 三者都失败 | 继续检查活动文档、视图来源、选择状态及接口绑定方式 |

这个实验成本最低，但它不能替代方案 A；建议顺序仍是“先保真复现，再做减法”。

---

## 8. 完整参考实现

> 【待测】以下代码强调调用结构与证据采集。项目接入时替换为自己的 `sw_session`、视图枚举和表格回读函数。

```python
import time
import win32com.client


def select_by_ray_11(doc, start, direction, radius,
                     type_wanted=2, append=False, mark=0, option=0):
    """严格按 IModelDocExtension.SelectByRay 当前官方 11 参签名。"""
    sx, sy, sz = [float(v) for v in start]
    dx, dy, dz = [float(v) for v in direction]
    return doc.Extension.SelectByRay(
        sx, sy, sz,
        dx, dy, dz,
        float(radius),
        int(type_wanted),
        bool(append),
        int(mark),
        int(option),
    )


def insert_doc_bom8(doc, x, y, anchor_type, bom_type,
                    template, hidden=False,
                    numbering_type=0, detailed_cut_list=False):
    """按录制宏的文档级 8 参形式调用，不添加 Configuration/UseAnchorPoint。"""
    return doc.InsertBomTable4(
        float(x), float(y),
        int(anchor_type),
        int(bom_type),
        str(template),
        bool(hidden),
        int(numbering_type),
        bool(detailed_cut_list),
    )


def insert_bom_python_only(doc, view,
                           ray_start, ray_direction, ray_radius,
                           x, y, template,
                           anchor_type=1, bom_type=1,
                           clear_after_ray=True):
    log = {}

    doc.ActivateView(view.Name)
    log["view"] = view.Name
    log["active_doc"] = doc.GetTitle

    selected = select_by_ray_11(
        doc, ray_start, ray_direction, ray_radius,
        type_wanted=2, append=False, mark=0, option=0)
    log["ray_selected"] = bool(selected)
    log["selected_count"] = doc.SelectionManager.GetSelectedObjectCount

    if clear_after_ray:
        doc.ClearSelection2(True)
        log["cleared"] = True

    table = insert_doc_bom8(
        doc, x, y, anchor_type, bom_type, template,
        hidden=False, numbering_type=0, detailed_cut_list=False)
    log["table_returned"] = table is not None

    doc.EditRebuild3()
    log["rebuilt"] = True
    return table, log
```

### 8.1 接入时必须保留的日志

```json
{
  "active_doc": "...SLDDRW",
  "sheet": "图纸1",
  "view": "工程图视图2",
  "view_type": 7,
  "ray_selected": true,
  "selected_count": 1,
  "cleared": true,
  "entry": "doc.InsertBomTable4",
  "arg_count": 8,
  "table_returned": true,
  "table_rows": 10,
  "table_columns": 4
}
```

没有这些字段，就不要写“上下文已排除”。

---

## 9. 实验矩阵：一次只改一个变量

### 9.1 推荐顺序

| 编号 | 选择方式 | BOM 入口 | 插入前清空 | 目的 |
|---|---|---|---|---|
| E1 | 宏原始 `SelectByRay`（11 参） | `doc.InsertBomTable4`（8 参） | 是 | **完整直译宏，最高保真** |
| E2 | 不选择 | 同上 | 是 | 验证射线是否必要 |
| E3 | 可见边 `SelectEntity/Select4` | 同上 | 是 | 验证几何上下文能否替代射线 |
| E4 | 可见边 `SelectEntity/Select4` | 同上 | 否 | 验证清空选择的影响 |
| E5 | 宏原始 `SelectByRay` | `view.InsertBomTable4`（10 参） | 是 | 验证视图入口在正确上下文下是否可用 |
| E6 | 可见边选择 | `view.InsertBomTable4`（10 参） | 否 | 产品化候选 |
| E7 | 可见边选择 | `view.InsertBomTable5`（11 参） | 否 | 新 API 候选 |
| E8 | E1 成功序列 | 同 E1，但换一张新图纸/新视图名 | 同 E1 | 验证是否摆脱录制图纸依赖 |

### 9.2 通过标准

每一组都必须同时满足：

1. COM 调用无异常。
2. 返回表格对象非 `None`（若该入口返回对象）。
3. 图纸表格集合实际新增 1 项。
4. `RowCount` / `ColumnCount` 与装配和模板一致。
5. 表格内容可读，且不是空表。
6. `BomFeature` 可取到，证明是原生 BOM，而非静态表格。

### 9.3 预期结论形式

不要只写“成功/失败”，要写成：

```text
文档级 8 参入口 + 几何上下文 + 插入前清空 = 成功
文档级 8 参入口 + 无几何上下文 = 失败
=> 必要条件 = 几何上下文；非必要条件 = 射线本身
```

---

## 10. 回读、幂等与交付判定

### 10.1 不信任返回值

本次已经证明：

- `RunMacro` 返回 `False`，表格却真实生成。
- 某些插入 API 返回 `None`，但不代表调用未进入 SolidWorks。

所以最终判定统一为：

```text
调用前记录表格 ID/数量
→ 执行插入
→ EditRebuild3
→ 调用后枚举表格
→ 找到新增表格
→ 回读行列数、表头、数据行、BomFeature
```

### 10.2 幂等

插入前检查当前图纸是否已有 BOM：

```text
已有且配置/视图/模板一致 → 返回 existing，不重复插入
已有但配置不同           → 更新现有 BomFeature 或明确新建
没有                     → 创建
```

禁止重复调用后无条件生成第二张明细栏。

### 10.3 交付判定

只有同时满足以下条件，才能写 `native_bom=true`：

- 表格对象是 `BomTableAnnotation` / 可取到 `BomFeature`
- 行数与预期 BOM 聚合结果一致
- 零件号、数量、配置与 `export_bom` 对账通过
- 不是 `add_sheet_line + add_note` 或静态通用表格

---

## 11. 如果纯 Python 仍失败

### 11.1 第一优先：绑定方式对照，而不是回到宏

在与宏完全相同的图纸和上下文下，分别执行：

| 方式 | 目的 |
|---|---|
| pywin32 晚绑定 | 当前基线 |
| `gencache.EnsureDispatch` + `CastTo` | 排除 IDispatch 派发问题 |
| comtypes vtable | 进一步排除接口/绑定差异 |

判读：

- **VBA 成功 + pywin32 晚绑定失败 + vtable 成功** → 是绑定/接口问题，仍然不需要宏。
- **VBA 成功 + 所有 Python 绑定都失败** → 比较每一步返回状态，找出 Python 与 VBA 上下文差异。

### 11.2 第二优先：RunCommand，不是 RunMacro

`ISldWorks.RunCommand` 也是纯 COM 调用，不需要 `.swp` 文件。

适合：

- 调用菜单“插入材料明细表”命令。
- 有界面、本机、允许命令上下文依赖的场景。

限制：

- 异步，需要等待命令完成并设置超时。
- 依赖活动文档、视图和选择状态。
- 能力标 `pilot`，不作为无人值守首选。

### 11.3 最后才保留 RunMacro 作为兼容后端

如果短期内确实无法直译，可保留：

```text
后端优先级：
纯 Python API（目标）
→ RunCommand（有界面 pilot）
→ RunMacro（临时兼容，pilot）
→ 静态表格/CSV（交付降级，明确披露）
```

**不要把临时的 `.swp` 后端当成“这个问题必须用宏”的证据。**

---

## 12. 对现有文档与代码的修改建议

### 12.1 对《宏驱动破局_完整记录》的修订

保留成功事实，但修改结论表述：

| 原表述 | 建议改为 |
|---|---|
| “BOM 表能插入，但必须走录制宏 + RunMacro” | “录制宏 + RunMacro 已证明可行；宏对应 COM 序列尚未完成 Python 直译验证” |
| “SelectByRay 11 个参数，含 byref Callout” | “当前官方 `IModelDocExtension.SelectByRay` 为 11 参，不含 Callout；本次多传 Callout 导致类型不匹配” |
| “没有必要继续攻 SelectByRay” | “先删除多余 Callout 后重试，成本极低” |
| “路径 A：生成参数化宏（推荐）” | “优先纯 Python 直译；参数化宏仅作临时备选” |

### 12.2 `com-quirks.md` 建议新增

| # | 条目 | 内容 |
|---|---|---|
| BOM-11 | `SelectByRay` 没有 Callout | 与 `SelectByID2` 区分；当前官方签名为 11 参 |
| BOM-12 | 宏的 BOM 入口不是此前视图入口 | 宏为 `swModel.InsertBomTable4` 8 参，不能套 `IView` 的 10/11 参 |
| BOM-13 | 宏可直译为 Python | `RunMacro` 不是原生能力来源，只是调用宿主 |
| BOM-14 | 射线选择不是通用方案 | 产品化优先 `SelectEntity/Select4` 建立几何上下文 |
| BOM-15 | 宏差异分析必须控制变量 | 本次同时存在“选择方式不同”和“BOM 入口不同”，不能直接把成功全部归因于 SelectByRay |

### 12.3 能力声明

当前阶段建议：

```text
bom_table_native_macro   = pilot（已验证原图，依赖 .swp）
bom_table_native_python  = investigating（优先直译，不再判定 unsupported）
bom_table_static         = fallback（明确不随装配更新）
```

纯 Python 在新图纸、新装配、重复执行均通过后，再把 `bom_table_native_python` 升为 `verified`，并移除宏后端。

---

## 13. 附录

### 13.1 SelectByRay 当前官方参数表【官方】

| 顺序 | 参数 | 类型 |
|---:|---|---|
| 1–3 | `WorldX, WorldY, WorldZ` | double |
| 4–6 | `RayVecX, RayVecY, RayVecZ` | double |
| 7 | `RayRadius` | double |
| 8 | `TypeWanted` | int (`swSelectType_e`) |
| 9 | `Append` | bool |
| 10 | `Mark` | int |
| 11 | `Option` | int (`swSelectOption_e`) |

**没有第 12 个参数，也没有 `Callout`。**

### 13.2 宏直译核对清单

```text
[ ] 图纸是活动文档
[ ] 使用宏中的视图名
[ ] SelectByRay 严格 11 参
[ ] 不多传 byref_dispatch_null()
[ ] 回读 SelectByRay 返回值
[ ] 回读选择集数量
[ ] 按宏顺序 ClearSelection2(True)
[ ] 使用 doc.InsertBomTable4 的 8 参形式
[ ] 不添加 UseAnchorPoint
[ ] 不添加 Configuration
[ ] EditRebuild3
[ ] 回读表格对象、行数、列数、BomFeature
[ ] 在新图纸上做可移植性复测
```

### 13.3 参考来源

- `IModelDocExtension.SelectByRay`：SOLIDWORKS API Help 2024（11 参签名、返回值、几何选择限制）
- `IView.InsertBomTable4/5`：SOLIDWORKS API Help（视图入口的 10/11 参形式）
- `Insert BOM Table and Get Data Example (VBA)`：SOLIDWORKS API Help（视图选择与表格回读示例）
- 原始记录：《BOM明细栏_宏驱动破局_完整记录_20261029.md》（宏调用序列、`.swp` 运行结果、当前局限）
