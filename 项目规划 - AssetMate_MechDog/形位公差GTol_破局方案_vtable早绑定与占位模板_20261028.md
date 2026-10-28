# 形位公差（GTol）破局方案：vtable 早绑定、NewGtol、RunCommand 与占位模板

> 针对《形位公差不可创建_四条路径验证记录_20261028.md》的复核与后续方案。
> 编写日期：2026-10-28。目标环境：Windows 10/11 x64 · SOLIDWORKS 2024 SP4 / 2026 SP0（中文版）· Python 3.10+ · pywin32 / comtypes。
> 本文只做方案与参考实现，不含对本项目的代码改动。
> 证据等级：【官方】= SOLIDWORKS API Help 原文；【推断】= 由事实推导，需实验确认；【待测】= 参考实现，须真机跑通后才可标 verified。

---

## 目录

1. [结论速查](#1-结论速查)
2. [对原报告的三处复核（重要）](#2-对原报告的三处复核重要)
3. [官方依据：InsertGtol 真实存在且有完整用法](#3-官方依据insertgtol-真实存在且有完整用法)
4. [路径 5：vtable 早绑定（最高优先级，未试过的独立路径）](#4-路径-5vtable-早绑定最高优先级未试过的独立路径)
5. [路径 6：IDrawingDoc.NewGtol / INewGtol（完全没试过的官方入口）](#5-路径-6idrawingdocnewgtol--inewgtol完全没试过的官方入口)
6. [路径 7：ISldWorks.RunCommand 走 UI 命令通道](#6-路径-7isldworksruncommand-走-ui-命令通道)
7. [路径 8：占位模板 + 修改既有 GTol（最稳的交付路径）](#7-路径-8占位模板--修改既有-gtol最稳的交付路径)
8. [前置条件修正：选面/选边必须真正成功](#8-前置条件修正选面选边必须真正成功)
9. [诊断工具箱（可复用脚本）](#9-诊断工具箱可复用脚本)
10. [执行顺序与判定树](#10-执行顺序与判定树)
11. [若最终仍不可创建：诚实降级与交付策略](#11-若最终仍不可创建诚实降级与交付策略)
12. [测试计划与验收](#12-测试计划与验收)
13. [对现有文档与代码的修改清单](#13-对现有文档与代码的修改清单)
14. [附录](#14-附录)

---

## 1. 结论速查

### 1.1 核心判断

**"四条路径全部失败 ⇒ 形位公差在本环境无法创建"这个结论下早了。**

- 原报告实际只试了 **1 个真实 API 入口**（`InsertGtol`，在两种文档类型上各调一次，本质同一路径），加上 1 个**官方文档里不存在的方法名**（`CreateGtolData`），加上 1 条**依赖前者的搬运路径**。
- 官方还存在 **`IDrawingDoc.NewGtol` / `INewGtol`** 与 **`ISldWorks.RunCommand(插入 GTol 命令)`** 两条入口，均未测试。
- 更关键：**"Invoke 阶段失败 ⇒ 运行时没有实现 ⇒ 早绑定也会失败"这个推理不成立**。vtable 早绑定**根本不走 `IDispatch::Invoke`**，它是独立的验证路径，不是同一路径的重复。

### 1.2 新路径速查

| # | 路径 | 是否走过 | 成本 | 优先级 |
|---|---|---|---|---|
| 5 | **vtable 早绑定**（makepy/gencache + `CastTo`，或 comtypes `QueryInterface`） | 否 | 30–60 分钟 | **P0** |
| 6 | **`IDrawingDoc.NewGtol` / `INewGtol`** | 否 | 10 分钟（可并入路径 5） | **P0** |
| 7 | **`RunCommand` 调用插入 GTol 命令** + `IGtol` 改值 | 否 | 1–2 小时 | P1 |
| 8 | **占位模板 + 修改既有 GTol** | 否 | 半天 | P1（交付兜底） |
| 9 | DimXpert | 部分（对象拿不到） | 高 | P3 |
| 10 | 宏录制取证 | 否 | 5 分钟 | **P0（最先做）** |

### 1.3 不变的部分

- 真实图纸基线：形位公差出现率 **7%**。**无论技术能否突破，`delivery_check` 都不把 Gtol 缺失列为 blocker**。
- 禁止用 Note 文本伪造 Gtol 框格充当通过证据。
- `list_drawing_annotations` 读取外部图纸已有 Gtol 的能力保留并加强（审核场景不依赖"创建"）。

---

## 2. 对原报告的三处复核（重要）

### 2.1 复核一：`CreateGtolData` 不是官方 API，路径 3 不能算作"官方路径被堵"

原报告路径 3 使用：

```csharp
IModelDocExtension ext = doc.Extension;
IGtolData data = ext.CreateGtolData();      // ← 官方文档中不存在此方法
ext.InsertGtol(data, x, y, z, 0, 0, 0, 0.0); // ← 该签名也不存在
```

官方实际提供的是【官方】：

```text
IModelDoc2.InsertGtol()   → Gtol
IModelDoc2.IInsertGtol()  → Gtol
IDrawingDoc.NewGtol()     → Gtol
IDrawingDoc.INewGtol()    → Gtol
```

**创建时不传任何参数**，返回空的 GTol 对象，再用 `IGtol` 的 Set 系列方法填内容。官方对 `IInsertGtol` 的 Remarks 明确写着【官方】：

> This method creates an **empty symbol**. To fill in the text and symbols of this GTol, use the pointer returned by this method to access the various get and set methods of the **IGTOL** interface, such as `IGtol::SetFrameSymbols2` and `IGtol::SetFrameValues`.

**结论**：路径 3 测的是一个不存在的方法名，`GetIDsOfNames` 失败是必然结果，**它不构成"官方新路径也被堵"的证据**。真正该测的是"拿到 GTol 对象后调用 `SetFrameSymbols2` / `SetFrameValues2`"这一段，而这一段从未被执行到。

> 方法论提醒：`GetIDsOfNames` 失败只说明"这个名字不在该接口上"，**不能推出"这个功能不存在"**。在否定一条路径前，必须先确认方法名来自官方文档而非转述。

### 2.2 复核二："Invoke 失败 ⇒ 运行时没实现 ⇒ 早绑定也失败"推理链断裂

原报告第五节的推理：

```
GetIDsOfNames 成功 + Invoke 返回 MEMBERNOTFOUND
  ⇒ 接口声明在类型库，运行时没有实现
  ⇒ 早绑定（C# interop）同样失败
```

问题在第二步到第三步。两条调用链完全不同：

```
晚期绑定（本次所有实验）
  Python → IDispatch::GetIDsOfNames → IDispatch::Invoke → SolidWorks 的 IDispatch 实现
                                                        ↑ 失败发生在这里

vtable 早绑定（未做过）
  Python/C# → 接口 vtable 直接调用 → SolidWorks 的方法实现
              ↑ 不经过 IDispatch，不经过 GetIDsOfNames/Invoke
```

`DISP_E_MEMBERNOTFOUND` 出现在 `Invoke` 层，除了"方法没实现"，还可能来自【推断】：

| 可能原因 | 说明 | vtable 是否可绕过 |
|---|---|---|
| A. 该方法确实未实现 | 原报告的结论 | 否 |
| B. **SolidWorks 的 IDispatch 实现缺陷** | 某些接口的 dispatch 映射不全，vtable 正常 | **是** |
| C. **接口指针不对**（拿到的是 `IModelDoc2` 的 dispatch，方法在 `IDrawingDoc` 上） | 晚期绑定按名字派发，容易掩盖接口归属问题 | **是**（QueryInterface 后正确） |
| D. **类型库与运行时版本不匹配** | 多版本共存、tlb 注册指向旧版本，DISPID 对不上 | **部分是**（重新生成/修复后） |

B/C/D 都没有被排除。**在这些可能性排除之前，不能写"早绑定也会失败"，更不能写"C# 插件也绕不过"。**

> 一个旁证：`InsertGtol` 自 SOLIDWORKS 2001Plus 就存在，官方至今（2027 版文档）仍提供完整 VBA 示例与 C# 示例，且社区大量在用。"该版本运行时整体移除了这个 25 年历史的方法"是可能性最低的解释，应当最后考虑。

### 2.3 复核三："必须先选中面"这个前置条件**没有真正被满足**

原报告第四节自己承认了：

> 本次选面本身也失败了（`face selected: False`），因此"选中面"这个前置条件**严格来说没有被完整满足**。

而官方 `IInsertGtol` 的 Remarks 明确【官方】：

> The leader attachment points for the newly created GTol object **come from the selections made before calling this method**. The initial location of the symbol is near the selection location. If there are no selections, then the GTol does not have a leader, is free standing, and is initially at the origin.

即：**选中状态是该方法的输入之一**。在输入未准备好的情况下得到的失败，不能记为"已排除前置条件因素"。

同时报告里还有两个具体的代码错误需要修正：

- `body.GetFaces` 是**方法**，要写成 `body.GetFaces()` 才能遍历。
- 选面失败说明选取 API 用错了，应改用 `IEntity.Select4(False, None)`（本项目在孔标注路径上已验证的写法），而不是继续用 `face.Select2`。

**结论**：原报告"已排除四个假设"中，第 3 条（前置选中）实际上**未排除**。需要在修正选面代码后重测。

### 2.4 复核小结：原结论应改写为

> 在 **pywin32 晚期绑定 + 未成功选中几何** 的上下文中，`IModelDoc2.InsertGtol()` 的 `IDispatch::Invoke` 返回 `DISP_E_MEMBERNOTFOUND`。
> 该现象**尚不能区分**"方法未实现"与"IDispatch 层/接口指针/前置状态问题"。
> `NewGtol`、`RunCommand`、vtable 早绑定、占位模板修改四条路径**尚未验证**。

---

## 3. 官方依据：InsertGtol 真实存在且有完整用法

### 3.1 官方 VBA 示例（Insert GTol Example）摘录【官方】

```vba
Set swDrawing = swApp.OpenDoc6(fileName, swDocDRAWING, swOpenDocOptions_Silent, "", errors, warnings)
Set swModel = swDrawing                       ' ModelDoc2，来自 DrawingDoc

Set swGtol = swModel.InsertGtol()             ' 创建空 GTol，无参数
If Not swGtol Is Nothing Then
    swGtol.SetFrameSymbols2 1, "", False, "", False, "", "", "", ""
    status = swGtol.SetFrameValues2(1, "0.4", "", "B-A-C", "B-C", "C-A")
    status = swGtol.SetPTZHeight2(1, 1, True, "50")
    swGtol.SetBetweenTwoPoints False, "", ""

    Set swAnno = swGtol.GetAnnotation()
    If Not swAnno Is Nothing Then
        status = swAnno.SetPosition(0.319315975204224, 0.12666668401487, 0)   ' 米
        status = swAnno.SetLeader3(swBENT, swLS_LEFT, True, False, False, False)
        swAnno.BentLeaderLength = 0.05
    End If
End If
swModel.WindowRedraw
```

可提炼的调用模型：

```
InsertGtol()                     → 空 GTol 对象（创建）
  ├─ SetFrameSymbols2(...)       → 每框的符号（位置度/平面度/垂直度…）
  ├─ SetFrameValues2(...)        → 每框的公差值与基准
  ├─ SetPTZHeight2(...)          → 投影公差带（可选）
  ├─ SetBetweenTwoPoints(...)    → 两点间标注（可选）
  └─ GetAnnotation()             → IAnnotation
        ├─ SetPosition(x,y,z)    → 图纸坐标，米
        └─ SetLeader3(...)       → 引线样式
```

**这正好补齐了原报告缺失的"创建之后怎么填内容"那一半。** 即使 `InsertGtol` 能返回对象，不调用这些 Set 方法，图上也只是一个空框——容易被误判为"创建失败"。

### 3.2 相关接口成员清单【官方】

| 成员 | 所属接口 | 作用 |
|---|---|---|
| `InsertGtol()` | `IModelDoc2` | 创建空 GTol |
| `IInsertGtol()` | `IModelDoc2` | 同上（dispatch 变体） |
| `NewGtol()` | `IDrawingDoc` | 创建新 GTol 对象 |
| `INewGtol()` | `IDrawingDoc` | 同上（dispatch 变体） |
| `SetFrameSymbols2` / `SetFrameValues2` | `IGtol` | 填符号与公差值/基准 |
| `SetPTZHeight2` / `GetPTZHeight2` | `IGtol` | 投影公差带 |
| `SetBetweenTwoPoints` | `IGtol` | 两点间标注 |
| `GetAnnotation()` | `IGtol` | 取注解对象用于定位与引线 |
| `InsertDatumTag` | `IDrawingDoc` | 基准符号（配套需求） |

> 注意：`IDrawingDoc` 同时有 `NewGtol` 和 `INewGtol`，`IModelDoc2` 同时有 `InsertGtol` 和 `IInsertGtol`。
> 原报告只试了 `InsertGtol` / `InsertGTol` 两种拼写，**`IInsertGtol`、`NewGtol`、`INewGtol` 三个名字都没试**。

---

## 4. 路径 5：vtable 早绑定（最高优先级，未试过的独立路径）

### 4.1 目的

一次性区分 §2.2 的 A 与 B/C/D：**如果 vtable 调用成功，说明问题在 IDispatch 层或接口指针，原报告"运行时未实现"的结论被推翻；如果 vtable 也失败，才接近"方法未实现"的结论。**

### 4.2 方案 5a：pywin32 makepy + CastTo【待测】

`CastTo` 能把一个晚期绑定对象**转成指定接口的早绑定对象**，正好对症"接口指针不对"。

```python
import win32com.client
from win32com.client import gencache

# 1) 生成 SolidWorks 类型库的 Python 封装（只需一次，会写入 gen_py 缓存）
app = gencache.EnsureDispatch("SldWorks.Application")
# 若 EnsureDispatch 因类型库过大失败，改用：
#   from win32com.client import makepy
#   makepy.GenerateFromTypeLibSpec("SldWorks.Application")   # 或指定 tlb 路径

doc = app.ActiveDoc                      # 已打开目标图纸
print("before CastTo:", type(doc))

# 2) 转成明确的接口早绑定对象
draw = win32com.client.CastTo(doc, "IDrawingDoc")
model = win32com.client.CastTo(doc, "IModelDoc2")

# 3) 逐个入口试，打印原始异常
for name, obj in [
    ("IModelDoc2.InsertGtol", model),
    ("IModelDoc2.IInsertGtol", model),
    ("IDrawingDoc.NewGtol", draw),
    ("IDrawingDoc.INewGtol", draw),
]:
    try:
        gtol = getattr(obj, name.split(".")[1])()
        print(name, "->", "OK" if gtol is not None else "returned None")
        if gtol is not None:
            break
    except Exception as exc:
        print(name, "->", repr(exc))
```

要点：

- `CastTo` 依赖 makepy 生成的接口定义；接口名拼写必须与类型库一致。
- 四个入口在同一个脚本里轮试，一次运行即可得到完整对照表。
- **先按 §8 修正选中状态**，再跑本脚本；并额外跑一组"无任何选中"的对照，观察行为差异（官方说无选中时会生成在原点的自由 GTol）。

### 4.3 方案 5b：comtypes QueryInterface【待测】

comtypes 天然走 vtable，且能显式 `QueryInterface`，是更彻底的验证。

```python
from comtypes.client import GetModule, CreateObject

TLB = r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\sldworks.tlb"
mod = GetModule(TLB)                      # 生成并加载类型库封装
# 生成的模块名以实际为准，可用 print(mod) 与 dir(mod) 查看

app = CreateObject("SldWorks.Application")
doc = app.ActiveDoc

# 在生成模块中找到接口定义后：
#   draw = doc.QueryInterface(mod.IDrawingDoc)
#   gtol = draw.NewGtol()
#   或 gtol = doc.InsertGtol()
print(type(doc))
```

要点：

- tlb 路径以本机实际安装为准；若存在多个 SOLIDWORKS 版本目录，**逐个试**，这同时能验证"类型库版本错配"假说。
- comtypes 与 pywin32 不要混用同一个 COM 对象指针；本脚本独立运行。

### 4.4 方案 5c：环境排查（若 5a/5b 仍失败）

| 检查项 | 方法 | 异常含义 |
|---|---|---|
| 是否多版本共存 | 列出 `C:\Program Files\SOLIDWORKS Corp\` 下版本目录 | 类型库可能指向旧版本 |
| tlb 注册 | `oleview` 打开 `sldworks.tlb`，查 `IModelDoc2.InsertGtol` 的 DISPID | 与实际 Invoke 的 DISPID 对比 |
| 以管理员身份重注册/修复 | SOLIDWORKS 安装管理器 → 修复 | 修复 dispatch 映射异常 |
| 换版本复测 | SW2024 SP4 / 2025 各跑一次同脚本 | 区分"2026 回归"与"本机环境" |
| 换一台机器复测 | 同脚本在另一台 SW2026 机器跑 | 区分"本机损坏"与"版本共性" |

### 4.5 成功后必须接着做的事

**拿到 GTol 对象只是开始。** 必须按 §3.1 继续：

```python
gtol.SetFrameSymbols2(...)          # 符号
gtol.SetFrameValues2(...)           # 公差值与基准
anno = gtol.GetAnnotation()
anno.SetPosition(x, y, 0)           # 米，图纸坐标
doc.WindowRedraw()                  # 或 EditRebuild3
# 回读：list_drawing_annotations 必须能看到该 Gtol，且符号/数值/基准与设定一致
```

**回读不过 = 不算成功。** 空框、符号缺失、基准缺失都要记为 fail，而不是"创建成功"。

---

## 5. 路径 6：IDrawingDoc.NewGtol / INewGtol（完全没试过的官方入口）

### 5.1 为什么单独列出来

- 它在 **`IDrawingDoc`** 上，而原报告主要围绕 `IModelDoc2` 打转。
- 晚期绑定下"方法属于另一个接口"正是 §2.2 的 C 类原因。**换接口 + 换方法名**可能直接解决问题，不需要先搞早绑定。
- 成本极低：两行代码。

### 5.2 最小验证脚本【待测】

```python
import win32com.client

app = win32com.client.Dispatch("SldWorks.Application")
doc = app.ActiveDoc                       # 图纸

def has_member(obj, name):
    try:
        obj._oleobj_.GetIDsOfNames(name)
        return True
    except Exception:
        return False

for name in ("NewGtol", "INewGtol", "InsertGtol", "IInsertGtol",
             "InsertDatumTag", "InsertSurfaceFinishSymbol3"):
    print(name, "member:", has_member(doc, name))

for name in ("NewGtol", "INewGtol"):
    if not has_member(doc, name):
        continue
    try:
        gtol = getattr(doc, name)()
        print(name, "->", "OK" if gtol is not None else "None")
        if gtol is not None:
            # 立刻补内容，避免"空框被误判为失败"
            # gtol.SetFrameSymbols2(...) / SetFrameValues2(...)
            break
    except Exception as exc:
        print(name, "->", repr(exc))
```

判读：

| 结果 | 含义 | 下一步 |
|---|---|---|
| `NewGtol` 成功 | 问题就是接口归属（C 类） | 用 `IDrawingDoc` 作为 Gtol 专用入口，封装完成 |
| `NewGtol` 也 MEMBERNOTFOUND | 更像 B 或 A | 上路径 5 的 vtable 验证 |
| `NewGtol` 返回 None | 创建被条件阻止 | 检查选中状态、文档类型、是否处于编辑状态 |

---

## 6. 路径 7：ISldWorks.RunCommand 走 UI 命令通道

### 6.1 原理

`ISldWorks.RunCommand(CommandID, NewTitle)` 可以执行 3000+ 条 SolidWorks 命令，包括**没有对应专用 API 的操作**【官方机制 + 社区实践】。官方 C# 示例文档的命令列表中存在 **`Insert_GTol_`** 命令条目【官方文档列表】。

这条路径的本质：**不依赖 `InsertGtol` 这个 API 是否可用，而是让 SolidWorks 自己执行菜单命令**，然后再用 `IGtol` 修改新建对象。

### 6.2 流程

```
1. 激活图纸与目标视图，按 §8 选中附着几何（面/边）
2. 解析"插入 GTol"的命令 ID（swCommands_e 中 Insert_GTol_ 对应值）
3. app.RunCommand(cmd_id, "")            → 打开 GTol PropertyManager / 生成 GTol
4. 同步等待命令结束（见 6.3）
5. 通过选择集或注解遍历拿到新建 GTol 对象
6. IGtol.SetFrameSymbols2 / SetFrameValues2 填内容
7. Annotation.SetPosition 定位，回读校验
```

### 6.3 必须处理的两个坑【官方机制 + 社区实践】

1. **`RunCommand` 是异步的**：命令启动就返回，不代表命令完成。
   - 同步方式：订阅 `CommandCloseNotify` 事件，或用 `ISldWorks.GetRunningCommandInfo` 轮询。
   - 简单场景可用"轮询直到出现新的 GTol 注解或超时"，但必须设硬超时（建议 10–15 s），防止挂死。
2. **命令依赖 UI 上下文**：需要 SolidWorks 可见、文档激活、正确的选中状态；锁屏/最小化/远程无人值守场景不可靠。

### 6.4 命令 ID 的获取办法

| 方法 | 说明 |
|---|---|
| 查 `swCommands_e` 枚举文档 | 找 `Insert_GTol_` 对应数值 |
| **宏录制**（推荐） | 手工执行一次菜单插入，录制脚本里会出现真实命令或真实 API |
| 遍历枚举试错 | 成本最高，不建议 |

### 6.5 定位

- 若路径 5/6 成功：不需要 RunCommand，优先纯 API（更稳、可无人值守）。
- 若路径 5/6 失败：RunCommand 是**比 pywinauto 坐标点击更稳**的 UI 通道（命令 ID 不依赖窗口位置与分辨率）。
- 能力等级：标 `pilot`，明确"需要界面在场"。

---

## 7. 路径 8：占位模板 + 修改既有 GTol（最稳的交付路径）

### 7.1 思路

**绕开"创建"，只做"修改"。** 原报告所有实验都聚焦"从无到有创建"，从未验证"修改一个已存在的 GTol"是否可用。而修改走的是另一组 API（`IGtol` 的 Set 方法 + `IAnnotation`），不依赖 `InsertGtol`。

### 7.2 实施步骤

1. **做模板**：在 `.drwdot` 中预置若干占位 GTol，覆盖常用类型（位置度、平面度、直线度、垂直度、平行度、同轴度、对称度、圆跳动、全跳动、线/面轮廓度）。
   - 占位内容用明显标记（如公差值 `0.01`、基准 `A`），便于识别与替换。
   - 每个占位 GTol 放在图纸固定区域（如"技术要求"旁的暂存区），并记录名称/位置清单。
2. **出图时**：按出图规划（plan_drawing）决定需要哪几个 Gtol。
3. **修改**：选中对应占位 GTol → `SetFrameSymbols2` / `SetFrameValues2` 写入真实值 → `GetAnnotation().SetPosition` 移到目标位置 → 按 §8 选中附着几何后设置引线。
4. **清理**：未用到的占位 GTol 一律删除，避免残留被误认为真实标注。
5. **回读**：符号、公差值、基准、位置、附着关系全部回读；占位标记若仍存在则判 fail。

### 7.3 关键验证点（先做 10 分钟实验）

```text
在现有图纸上手工加一个 GTol → 用 API 读出 → 用 SetFrameValues2 改公差值
→ WindowRedraw → 再读一次，确认值已变化
```

- **改得动** ⇒ 路径 8 成立，立即可交付（`pilot` → 稳定后 `verified`）。
- **改不动** ⇒ 说明 `IGtol` 的 Set 方法也受影响，问题范围比"创建"更大，需回到路径 5 做 vtable 验证。

### 7.4 优缺点

| 优点 | 缺点 |
|---|---|
| 不依赖创建类 API | 需要维护模板与占位清单 |
| 修改类 API 通常比创建类稳定 | 占位清理漏了会污染图纸（必须回读） |
| 可覆盖常用 Gtol 类型 | 非常用类型仍需人工 |
| 与现有"模板安全区实测"能力契合 | 附着几何仍需正确选中 |

### 7.5 与交付判定的配合

- 只有"占位 GTol 被替换成真实值且回读通过"才计入 Gtol 覆盖。
- 占位残留 = `pending`，不得放行。
- 需要的 Gtol 类型模板里没有 → 声明期就报 `unsupported` 并给人工补标建议，不要出了图再说。

---

## 8. 前置条件修正：选面/选边必须真正成功

原报告选面失败，导致前置条件未满足。修正要点：

```python
# 1) GetFaces 是方法，必须加括号
bodies = part.GetBodies2(0, True)          # 按实际接口调整
faces = bodies[0].GetFaces()               # 不是 .GetFaces

# 2) 选取统一用 IEntity.Select4（孔标注路径已验证的写法）
entity = win32com.client.Dispatch(faces[0])
ok = entity.Select4(False, None)           # False = 单选；True = 追加
print("face selected:", ok)
assert ok, "选中失败，前置条件未满足，后续结论无效"

# 3) 图纸侧：先激活视图，再选边
drawing.ActivateView(view_name)
drawing.ClearSelection2(True)
edge_entity = win32com.client.Dispatch(edge)
assert edge_entity.Select4(False, None)
```

纪律：

- **任何"前置条件已排除"的结论，都必须有 `Select4` 返回 True 的日志支撑。**
- 选中后用选择集回读确认数量与类型（`SelectionMgr.GetSelectedObjectCount` / `GetSelectedObjectType`），不要只看布尔返回。
- 对照组必须包含"有选中"与"无选中"两种，因为官方明确两种情况下 GTol 形态不同（有引线 vs 原点自由框）。

---

## 9. 诊断工具箱（可复用脚本）

### 9.1 成员探测（保留原报告方法，但限定解释范围）

```python
def has_member(obj, name):
    """True = 该名字可解析为 DISPID；False = 该接口上没有这个名字。
    注意：False 不能推出功能不存在，先核对方法名是否来自官方文档。"""
    try:
        obj._oleobj_.GetIDsOfNames(name)
        return True
    except Exception:
        return False
```

### 9.2 统一调用探针

```python
def probe(obj, name, *args):
    """返回 (stage, detail)。stage ∈ member_missing / invoke_failed / returned_none / ok"""
    if not has_member(obj, name):
        return ("member_missing", name)
    try:
        res = getattr(obj, name)(*args)
    except Exception as exc:
        return ("invoke_failed", repr(exc))     # 记录原始 HRESULT，勿只记文字
    return ("ok", res) if res is not None else ("returned_none", name)
```

### 9.3 错误码判读表

| 错误 | 含义 | 下一步 |
|---|---|---|
| `DISP_E_MEMBERNOTFOUND`（GetIDsOfNames 阶段） | 接口上没这个名字 | 核对官方方法名与接口归属 |
| `DISP_E_MEMBERNOTFOUND`（Invoke 阶段） | dispatch 层未处理该调用 | **上 vtable 早绑定**，不要直接判死刑 |
| `DISP_E_TYPEMISMATCH` | 参数类型不对（常见于 VARIANT_BOOL / 数组封送） | 改参数封送方式 |
| `DISP_E_BADPARAMCOUNT` / "非选择性的参数" | 参数个数不对 | 核对签名版本 |
| `E_NOINTERFACE` | 对象不支持该接口 | 换接口 / QueryInterface |
| 返回 `None` 但无异常 | 创建被条件阻止 | 查选中状态、文档状态、编辑模式 |

### 9.4 一次跑完的对照矩阵脚本结构

```text
for bind in (late_binding, early_binding_castto, comtypes_vtable):
    for iface in (IModelDoc2, IDrawingDoc):
        for method in (InsertGtol, IInsertGtol, NewGtol, INewGtol):
            for selection in (none, face_selected, edge_selected):
                probe(...) → 记录 stage + HRESULT + 是否产生空框
```

产出一张 3×2×4×3 的结果表，**一次性把"绑定方式 / 接口 / 方法名 / 选中状态"四个变量全部固定下来**，避免再出现"只测一个变量就下结论"。

---

## 10. 执行顺序与判定树

### 10.1 执行顺序

```
P0-1  宏录制（5 分钟）
      手工插入一个 GTol，看录制结果里出现的是 InsertGtol / NewGtol / RunCommand / 其他
      ↓
P0-2  修正选面代码（§8），重测 InsertGtol，含"有选中/无选中"对照
      ↓
P0-3  测 NewGtol / INewGtol / IInsertGtol（§5.2，10 分钟）
      ↓
P0-4  vtable 早绑定：CastTo → comtypes（§4，30–60 分钟）
      ↓
P1-1  修改既有 GTol 是否可行（§7.3，10 分钟）   ← 与上述并行可做
      ↓
P1-2  RunCommand 通道（§6）
      ↓
P1-3  占位模板工程化（§7）
      ↓
P3    DimXpert、oleview DISPID 比对、跨机跨版本复测
```

### 10.2 判定树

```
宏录制里出现了什么？
├─ InsertGtol / NewGtol      → 说明 API 路线可行，问题在我们调用方式 → 走 P0-2/P0-3/P0-4
├─ RunCommand / 菜单命令     → 走路径 7
└─ 什么都没录到（界面操作不落 API）
                              → 走路径 7 或路径 8

vtable 早绑定结果？
├─ 成功                      → 定型：早绑定 + IGtol 填值 + 回读，标 pilot→verified
└─ 失败
   ├─ 修改既有 GTol 成功      → 占位模板路径 8 作为主交付路径
   └─ 修改也失败              → RunCommand + UI 自动化；无人值守则声明 unsupported
```

---

## 11. 若最终仍不可创建：诚实降级与交付策略

### 11.1 能力声明

| 能力 | 级别 | 说明 |
|---|---|---|
| `gtol_create`（纯 API 创建） | `unsupported` / `pilot` | 以最终矩阵结果为准，附完整证据表 |
| `gtol_modify`（修改既有 GTol） | 待测 | 若可用，占位模板路径即可交付 |
| `gtol_read`（读取外部图纸 Gtol） | `verified` | `list_drawing_annotations` 保留 |
| `gtol_via_ui`（RunCommand / UI 自动化） | `pilot` | 需界面在场 |

### 11.2 交付判定口径

- **Gtol 缺失永远不是 blocker**（真实图纸仅 7% 带 Gtol）。
- 调用方声明需要 Gtol 而未能生成：结论最高"待设计审核"，`pending` 里写明"Gtol 未生成，需人工补标 + 具体类型/公差值/基准"。
- 禁止 Note 伪造框格；禁止把"调用了 InsertGtol"记为"已创建"。
- 空框、缺符号、缺基准、占位残留，一律记 fail。

### 11.3 出图规划（plan_drawing）的配套

- 规划期就列出所需 Gtol 清单（类型/数值/基准/附着特征），做能力预检。
- 不支持的类型在**改文档之前**返回 `unsupported` + 人工补标建议。
- 支持的类型若走占位模板，规划期检查模板占位是否足够。

### 11.4 投入上限建议

- 该能力只影响 7% 的真实图纸。**建议给技术突破设时间盒**（例如 P0 四项合计不超过半天）。
- 超时即定型为"占位模板 + 人工补标"，把精力投回 93% 的主线：尺寸、公差、粗糙度、孔标注、自检与交付判定。

---

## 12. 测试计划与验收

| # | 用例 | 通过标准 |
|---|---|---|
| G1 | 宏录制取证 | 得到真实调用序列，明确走 API 还是命令 |
| G2 | 修正选面后重测 `InsertGtol` | `Select4` 返回 True 有日志；记录有/无选中两组结果 |
| G3 | `NewGtol` / `INewGtol` / `IInsertGtol` | 每个入口都有 stage + HRESULT 记录 |
| G4 | vtable 早绑定（CastTo / comtypes） | 明确"vtable 是否也失败"，结论二分 |
| G5 | 创建后填值 | `SetFrameSymbols2` / `SetFrameValues2` 调用成功，图上不是空框 |
| G6 | 定位与引线 | `SetPosition` 生效，引线附着正确 |
| G7 | 回读 | `list_drawing_annotations` 能读到该 Gtol，符号/数值/基准一致 |
| G8 | 修改既有 GTol | 改公差值后回读到新值 |
| G9 | 占位模板全流程 | 替换 + 移动 + 清理 + 回读全通过，无占位残留 |
| G10 | RunCommand | 命令 ID 正确，同步等待生效，无挂死 |
| G11 | 重复执行幂等 | 不重复生成同一 Gtol |
| G12 | 版本矩阵 | 2024 SP4 / 2026 SP0 分别记录 pass/fail/unavailable |
| G13 | 反伪造 | 空框/缺基准/Note 伪造均被 `delivery_check` 拦下 |
| G14 | 交付判定 | 无 Gtol 的图纸不被误判为不可交付；声明需要却未生成时进 pending |

---

## 13. 对现有文档与代码的修改清单

### 13.1 《形位公差不可创建_四条路径验证记录_20261028.md》

- 标题与结论改为"晚期绑定下 InsertGtol 调用失败（四条路径待补）"，保留全部原始实验数据。
- 第五节"早绑定也会失败"标注为**推断，未验证**，并指向本文 §2.2。
- 路径 3 标注为"使用了非官方方法名，不构成官方路径证据"。
- 第四节"已排除选中面假设"改为"未排除（选面未成功）"。
- 补充本文路径 5–8 为后续待办。

### 13.2 《SolidWorks工程图_竞品借鉴与v0.5实施方案.md》§8

- 原"早绑定五步诊断"保留，但升级为本文 §9.4 的四变量对照矩阵。
- 删除"C# 插件也绕不过"的绝对表述。
- 增加 `NewGtol`、`RunCommand`、占位模板三条路径。
- 保留"Gtol 降为可选、不做语义求解、时间盒控制"的产品策略。

### 13.3 `docs/com-quirks.md` 新增条目

| # | 条目 | 要点 |
|---|---|---|
| Gtol-1 | 创建 GTol 不传参数 | `InsertGtol()` 返回空框，必须再 `SetFrameSymbols2` / `SetFrameValues2`，否则误判为失败 |
| Gtol-2 | 四个官方入口名 | `IModelDoc2.InsertGtol/IInsertGtol`、`IDrawingDoc.NewGtol/INewGtol` |
| Gtol-3 | Invoke 失败 ≠ 方法未实现 | 需 vtable 早绑定复核；`CastTo` 可解决接口归属问题 |
| Gtol-4 | 选中状态是输入 | 官方 Remarks：引线附着点来自调用前的 selections；`Select4` 必须返回 True |
| Gtol-5 | `CreateGtolData` 不是官方 API | 不要再用它做验证依据 |
| Gtol-6 | `body.GetFaces` 是方法 | 必须 `GetFaces()` 才能遍历 |
| Gtol-7 | `RunCommand` 异步 | 需 `CommandCloseNotify` / `GetRunningCommandInfo` 同步，且依赖 UI 上下文 |
| Gtol-8 | 占位模板必须清场 | 未替换的占位 GTol 要删除并回读，残留即 fail |

### 13.4 代码侧

- `check_capabilities`：Gtol 由单一 `unsupported` 改为 §11.1 的四档结构，每条带证据链接。
- 新增内部诊断脚本 `tests/probe_gtol.py`：实现 §9.4 矩阵，输出 JSON 结果表，供版本矩阵复用。
- 若路径 8 可行：新增 `add_geometric_tolerance`（基于占位替换，pilot），含占位清理与回读。
- `delivery_check`：按 §11.2 口径实现，不因 Gtol 缺失阻断交付。

---

## 14. 附录

### 14.1 关键 API 速查

```text
IModelDoc2.InsertGtol()          → Gtol（空框，需后续填值）
IModelDoc2.IInsertGtol()         → Gtol
IDrawingDoc.NewGtol()            → Gtol
IDrawingDoc.INewGtol()           → Gtol
IGtol.SetFrameSymbols2(...)      填符号
IGtol.SetFrameValues2(...)       填公差值与基准
IGtol.SetPTZHeight2(...)         投影公差带
IGtol.SetBetweenTwoPoints(...)   两点间标注
IGtol.GetAnnotation()            → IAnnotation
IAnnotation.SetPosition(x,y,z)   图纸坐标，单位米
IAnnotation.SetLeader3(...)      引线样式
IDrawingDoc.InsertDatumTag       基准符号（配套）
ISldWorks.RunCommand(cmd,title)  UI 命令通道（异步）
IEntity.Select4(append,data)     选中几何（必须回读 True）
```

### 14.2 绑定方式对照

| 方式 | 调用链 | 适用判断 |
|---|---|---|
| pywin32 晚期绑定 `Dispatch` | `GetIDsOfNames` + `Invoke` | 方便快捷，但易受 dispatch 实现影响 |
| pywin32 早绑定 `EnsureDispatch` / `CastTo` | makepy 生成 + vtable | **验证 Invoke 层问题的首选** |
| comtypes `GetModule` + `QueryInterface` | 纯 vtable，接口明确 | 最彻底，适合定位接口归属 |
| C# interop | vtable | 与 comtypes 等价，非必要不引入 |

### 14.3 参考来源

- `Insert GTol Example (VBA)`：SOLIDWORKS API Help（完整创建 + 填值 + 定位 + 引线流程）
- `IModelDoc2.IInsertGtol` / `IModelDoc2.InsertGtol`：SOLIDWORKS API Help（含"创建空框""选中决定引线"两条 Remarks）
- `IDrawingDoc.INewGtol` / `IDrawingDoc.NewGtol`：SOLIDWORKS API Help
- `ISldWorks.RunCommand` 与 `swCommands_e`：SOLIDWORKS API Help；命令列表中含 `Insert_GTol_` 条目
- `RunCommand` 异步与同步等待：CodeStack《How to run commands synchronously using SOLIDWORKS API》、CADSharp《RunCommand: The Swiss-Army Knife of the API》
- 原始实验数据：《形位公差不可创建_四条路径验证记录_20261028.md》
