# BOM 明细栏纯 Python 直译：失败证据与未解矛盾

> 日期：2026-10-29
> 环境：Windows / SOLIDWORKS **2024 SP4**（32.4.0）/ Python 3.11 / pywin32 晚绑定
> 针对：《BOM明细栏_免宏解决方案_SelectByRay与宏序列直译_20261029.md》
> 结论：**方案中 `SelectByRay` 的修正成立并已验证；但纯 Python 插入 BOM 仍然全部失败，宏方案也只在一张特定图纸上成立。**

---

## 一、结论速查

| 项 | 方案主张 | 实测结果 |
|---|---|---|
| `SelectByRay` 是 11 参、无 Callout | 删掉 `byref_dispatch_null()` 即可 | ✅ **成立**，返回 `True`、选中 2 个对象 |
| 宏用 `doc.InsertBomTable4`（8 参） | 这是此前未测的入口 | ❌ `GetIDsOfNames` 返回 **False**，该成员不存在 |
| 改用 `doc.Extension.InsertBomTable4`（9 参） | 模型侧入口 | ❌ 三种配置名全部返回 `None`，表格 0→0 |
| 按宏顺序直译即可成功 | ActivateView→SelectByRay→Clear→Insert | ❌ 五组对照全部 `None` |
| 不必须用宏 | 宏只是调用宿主 | ⚠️ **方向正确但未验证**：直译未成功，宏仍不可替代 |

**一句话**：`SelectByRay` 这个卡点解开了，但 BOM 创建仍无路径。

---

## 二、已确认成立的修正（先记推进的部分）

### 2.1 `SelectByRay` 确实是 11 参，没有 Callout

此前我按 `SelectByID2` 的习惯传了 12 个参数，末尾多一个 `byref_dispatch_null()`：

```python
# 错误（12 参）
doc.Extension.SelectByRay(cx, cy, 0.0, 0.0, 0.0, 0.0, 0.002, 2,
                          False, 0, byref_dispatch_null(), 0)
# → DISP_E_TYPEMISMATCH（报在第 11 个参数位置）
```

改为方案给出的 11 参：

```python
# 正确（11 参）
doc.Extension.SelectByRay(cx, cy, 0.0,  0.0, 0.0, -1.0,  0.002, 2, False, 0, 0)
# → True, SelectionManager.GetSelectedObjectCount2(-1) == 2
```

**这是本次唯一可确认的技术推进**，也是我此前"`SelectByRay` 无法复现"这一结论的错误源头。

两个方法的末尾结构确实不同：

| 方法 | 参数尾部 |
|---|---|
| `SelectByID2` | `Append, Mark, **Callout**, Option` |
| `SelectByRay` | `Append, Mark, Option` |

### 2.2 但选中了 2 个对象，不是 1 个

```
SelectByRay: True | count: 2
after ClearSelection2(True): count: 0
```

宏录制那一瞬间是「某特定射线打到某特定面」。我用「视图中心 + 向下方向」是猜的，选到 2 个对象说明**命中范围与录制时不同**。

这一差异可能不重要（宏里紧接着 `ClearSelection2`），也可能重要——见 §6。

---

## 三、失败证据矩阵

### 3.1 实验条件（严格控制）

| 项 | 值 |
|---|---|
| 图纸 | 新建 A3，3 视图（前视/右视/轴测） |
| 模型 | `SAB1_0000_000.SLDASM`（9 行含 5 子装配） |
| 模板 | `D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang\chinese-simplified\bom-standard.sldbomtbt`（`os.path.exists` = True） |
| 表格计数 | `len(doc.GetFirstView.GetTableAnnotations)`，插入前后各取一次 |
| 判定标准 | **表格数增加** 才算成功（不看返回值，因为已知返回值不可信） |

### 3.2 五组对照（严格按宏顺序）

执行序列（与宏完全一致）：

```
doc.ActivateView("工程图视图1")
→ ext.SelectByRay(cx, cy, 0, 0, 0, -1, 0.002, 2, False, 0, 0)   → True, count=2
→ doc.ClearSelection2(True)                                      → count=0
→ InsertBomTable4(...)
→ doc.EditRebuild3
→ 回读表格数
```

| # | 入口 | 参数数 | Configuration | 返回值 | 表格数 |
|---|---|---|---:|---|---|
| 1 | `ext.InsertBomTable4` | 9 | `'Default'` | `None` | 0 → 0 |
| 2 | `ext.InsertBomTable4` | 9 | `''` | `None` | 0 → 0 |
| 3 | `ext.InsertBomTable4` | 9 | `'UP'` | `None` | 0 → 0 |
| 4 | `view.InsertBomTable4` | 10 | `'Default'` | `None` | 0 → 0 |
| 5 | `view.InsertBomTable4` | 10 | `''` | `None` | 0 → 0 |

**五组全部失败，无一次表格数增加。**

### 3.3 参数个数探底

| 调用 | 结果 |
|---|---|
| `ext.InsertBomTable4` 8 参 | `「非选择性的参数」`（参数太少） |
| `ext.InsertBomTable4` 9 参 | `None`（调用被接受，创建被拒） |

9 参是正确个数（与 `IModelDocExtension.InsertBomTable4` 文档一致）。

### 3.4 此前已排除的（累计）

| 维度 | 取值 | 结果 |
|---|---|---|
| `BomType` | 0 / 1 / 2 / 3 / 4 | 全 `None` |
| `TableTemplate` | `''` / 真实路径 | 全 `None` |
| `UseAnchorPoint` | `True` / `False` | 全 `None` |
| 上下文 | `ActivateView` / `EditSheet` / `ActivateSheet` / `SaveAs3` 后 | 无差别 |
| 装配状态 | 关闭 / 保持打开 | 无差别 |
| 配置名 | `''` / `'Default'` / `'UP'`（真实名，取自 `GetConfigurationNames`） | 全 `None` |
| 选择状态 | 无选择 / 选中视图对象 / 选中可见边 / `SelectByRay` 选中后清空 | 全 `None` |
| 参数封送 | 裸值 / 显式 `VARIANT` / 手动 `ole.Invoke` | 无改善 |

**累计：8 条 API × 30+ 参数组合 × 6 类上下文，0 成功。**

---

## 四、宏方案的真实边界（新证据）

上一份记录写「宏能成」，这个说法需要收窄。

### 4.1 对比实验

| 图纸 | 宏录制于 | `RunMacro` 结果 |
|---|---|---|
| `BOM_TEST.SLDDRW` | ✅ 就是这张 | ✅ 表格出现，10 行 × 4 列 |
| 新建 A3 图纸（3 视图） | ❌ 不是这张 | ❌ `bom_table_not_created` |

回读成功案例的实际内容：

```
rows=10  cols=4
r0: ['项', '零', '说', '数']
r1: ['1', 'SAB1_0100_000', '框架模块', '1']
r2: ['2', 'SAB1_0200_000', '扫描笔固定模块', '1']
```

### 4.2 结论修正

> **不是「宏能成」，是「宏在它被录制的那张图纸那个状态下能成」。**

宏里的三个硬编码：

```vba
ActivateView("工程图视图2")                    ' 视图名
SelectByRay(0.1957, 0.3566, 0.031, ...)        ' 射线起点/方向（屏幕坐标）
InsertBomTable4(0.24, 0.19, ...)               ' 表格落点
```

新图纸的视图名是 `工程图视图1`，射线坐标也没意义——**宏的执行前提不成立**。

---

## 五、未解矛盾：宏里的 `swModel.InsertBomTable4` 在 Python 里不存在

这是本次最重要的一条待查证据。

### 5.1 事实一：Python 侧该成员不存在

```python
def has_member(obj, name):
    try:
        obj._oleobj_.GetIDsOfNames(name); return True
    except Exception: return False

has_member(doc, "InsertBomTable4")             # → False
has_member(doc, "InsertBomTable5")             # → False
has_member(doc.Extension, "InsertBomTable4")   # → True
has_member(doc.Extension, "InsertBomTable3")   # → True
```

`IDrawingDoc`（`doc`）上没有 `InsertBomTable*`，`IModelDocExtension`（`doc.Extension`）上有。

### 5.2 事实二：宏里写的是 `swModel.InsertBomTable4`

从 `.swp` 解压出的代码片段：

```vba
Set swModel = swApp.ActiveDoc
...
swModel.InsertBomTable4(0.24, 0.19, swBOMConfigurationAnchorType_TopLeft,
                        swBomType_TopLevelOnly, "...\bom-standard.sldbomtbt",
                        False, 0, False)
```

**8 个参数**：`X, Y, AnchorType, BomType, Template, Hidden, Numbering, CutList`
—— 没有 `UseAnchorPoint`，没有 `Configuration`。

### 5.3 矛盾点

- VBA 是**早绑定**，`ModelDoc2` 上没有的方法会**编译失败**。宏能跑，说明 VBA 认为它存在。
- Python 是**晚绑定**，`GetIDsOfNames` 说 `doc` 上没有。

**三种可能**（都未排除）：

| # | 可能 | 怎么验证 |
|---|---|---|
| 1 | 宏录制器把 `swModel.Extension.InsertBomTable4` 写成了 `swModel.InsertBomTable4`（VBA 的对象变量是 `Object` 类型，不做静态检查） | 解压代码里查 `swModel` 的 `Dim` 声明类型 |
| 2 | 该版本 `IModelDoc2` 确实有该方法，但 pywin32 的类型库映射缺项 | 读本机 `SolidWorks.Interop.sldworks.dll` 的方法表 |
| 3 | `swApp.ActiveDoc` 返回的对象实际是 `IModelDocExtension` 包装 | 打印 `type(doc)` 与接口 IID |

**可能 1 概率最高**：VBA 里 `Set swModel = swApp.ActiveDoc` 若 `swModel` 声明为 `Object`，编译器不校验成员，运行时靠 `IDispatch` 解析——而 `IDispatch` 解析失败会抛错，不会静默成功。

所以可能 1 也不完全解释得通，**这条矛盾需要类型库级别查证**。

### 5.4 已尝试的查证手段与阻塞

| 手段 | 结果 |
|---|---|
| `GetIDsOfNames` | ✅ 已做，结论见 5.1 |
| PowerShell + `Assembly.LoadFrom` 反射 | ❌ **被安全策略阻止**：「Reflection.Assembly load APIs execute arbitrary .NET code」 |
| `oleview` | 未安装 |
| `comtypes.client.GetModule` | 未尝试（可生成类型库封装后 `dir()`） |

**下一步最可行的是 `comtypes.client.GetModule`** 或读 `swconst` 类型库。

---

## 六、对方案的逐节评价

| 节 | 主张 | 评价 |
|---|---|---|
| §1 结论「不必须用宏」 | 宏只是调用宿主 | ⚠️ **方向正确，未被证实**。直译 5 组全失败，宏仍不可替代 |
| §2.1 宏给出新入口 | `doc.InsertBomTable4` 8 参未测 | ⚠️ 该入口在 Python 侧**不存在**；`Extension` 版本 9 参已测，失败 |
| §2.2 / §3 `SelectByRay` 11 参 | 删掉 Callout | ✅ **完全成立**，已验证 |
| §4 参数表不能混用 | 文档级 vs 视图级 | ✅ 结论正确，但文档级入口名需修正为 `Extension` |
| §5 方案 A 逐句直译 | 最高保真 | ❌ **已执行，失败**（见 §3.2） |
| §6 方案 B 几何对象选择 | 用可见边替代射线 | 未单独验证（因 §3.2 已失败，B 只是换选择方式，入口问题仍在） |
| §9 一次一变量 | 实验纪律 | ✅ **方法论正确**，我确实犯过同时改多个变量的错 |
| §12 修订我的表述 | 措辞收窄 | ✅ 接受。「必须走宏」应改为「目前只有宏路径被验证」 |
| §11.1 绑定方式对照 | 早绑定/vtable | 有价值但成本高，且 §5 的矛盾未解前无法定位 |

### 6.1 方案最有价值的三点

1. **`SelectByRay` 11 参**——直接解开我卡了 5 小时的类型不匹配
2. **「宏只是宿主」的判断**——纠正了我「必须用宏」的过强结论
3. **实验纪律（一次一变量）**——我此前确实同时改了「选择方式」和「BOM 入口」

### 6.2 方案未能解决的核心

**它假设「直译宏序列即可成功」，但实测五组全失败。**

失败原因未定位，因为存在 §5 那个矛盾（宏的入口在 Python 侧不存在）。

---

## 七、剩余路径与代价

| # | 路径 | 能回答什么 | 成本 | 风险 |
|---|---|---|---|---|
| 1 | `comtypes.client.GetModule` 查 `IModelDoc2` 方法表 | **宏的入口到底是哪个接口**（解 §5 矛盾） | 30 分钟 | 低 |
| 2 | 在新图纸上**重新录一次宏** | BOM 成功需要哪些前置（对比两份宏的差异） | 5 分钟（需人工） | 低，但依赖人 |
| 3 | `gencache.EnsureDispatch` + `CastTo` 早绑定重测 | 是否 IDispatch 派发问题 | 1 小时 | 中 |
| 4 | `RunCommand` 走菜单命令 | 绕过 API 直接触发命令 | 2 小时 | 高（异步 + 需界面） |
| 5 | 接受现状：`add_bom_table` 保持 pilot | — | 0 | 交付时需披露限制 |

**建议顺序：1 → 2 → （视结果决定 3/4/5）**

理由：
- 路径 1 能定性「纯 Python 到底有没有可能」，是最根本的一问
- 路径 2 只需人花 5 分钟，能提供第二份宏做差异对比
- 若 1 和 2 都指向「必须依赖特定上下文」，则路径 5 是理性选择

---

## 八、方法论反思（这次我错在哪）

### 8.1 把「同类 API 全失败」当成了「环境问题」，而不是「缺前置条件」

`InsertTableAnnotation2`（通用表格）也返回 `None`，我当时判定「环境拒绝一切表格插入」。

**实际是缺 `SelectByRay` 建立的上下文**——方案指出这条线索时我才意识到，它是「最强诊断信号」，不是「又一个失败」。

### 8.2 参数表靠猜而不是靠查，浪费了整整一晚

`InsertBomTable4` 是 10 参（视图入口）/ 9 参（Extension 入口），我按 4/5/6/7 猜了一晚。

**COM 方法签名是确切值，不是区间。** 猜参数个数这件事本身就不该做。

### 8.3 「宏能成」这个结论下得太早

只在一张图上验证就写成「破局」，第二天发现新图纸失败。

**「能跑通一次」和「能用」之间隔着一整轮可移植性验证。**

### 8.4 但这次也有一处做对了

**坚持用「表格数增量」而不是「返回值」判定成败。**

```
ext9 cfg=Default   ret=None       tables 0->0
```

如果看返回值，`None` 和「插入成功但没返回对象」无法区分。增量判定让五组失败一目了然，也让更早那次 `RunMacro` 返回 `False` 却成功的事实暴露出来。

---

## 九、复现材料

### 9.1 完整调用（可直接复制）

```python
import os, win32com.client as win
from solidworks_assetmate_mechdog.sw_session import get_sw
from solidworks_assetmate_mechdog.tools import drawing

sw = get_sw()
tpl = os.path.join(str(sw.GetExecutablePath),
                   'lang', 'chinese-simplified', 'bom-standard.sldbomtbt')

doc = drawing._open_drawing(drw_path)
ext = doc.Extension
view = [v for v in drawing._iter_views(doc) if getattr(v, 'Type', None) != 1][0]

def table_count():
    return len(list(doc.GetFirstView.GetTableAnnotations or ()))

# 1) 严格按宏顺序
doc.ActivateView(view.Name)
o = list(view.GetOutline)
cx, cy = (o[0] + o[2]) / 2, (o[1] + o[3]) / 2
assert ext.SelectByRay(cx, cy, 0.0, 0.0, 0.0, -1.0, 0.002, 2, False, 0, 0) is True
doc.ClearSelection2(True)

# 2) 三种入口，全部失败
before = table_count()
ext.InsertBomTable4(tpl, 0.24, 0.19, 1, 'Default', False, 0, False, False)
doc.EditRebuild3
print('ext9  :', before, '->', table_count())      # 0 -> 0

view.InsertBomTable4(False, 0.24, 0.19, 0, 1, 'Default', tpl, False, 0, False)
doc.EditRebuild3
print('view10:', before, '->', table_count())      # 0 -> 0
```

### 9.2 宏解压（如需复核宏的原意）

```python
import olefile, struct
# 流名：apc/The VBA Project/_VBA_Project/VBA/Macro11
# 首字节 0x01 为压缩签名，其后为 MS-OVBA 2.4.1 压缩数据
# 解压后 latin1 解码，中文乱码但标识符可读
```

完整解压函数见《BOM明细栏_宏驱动破局_完整记录_20261029.md》§4。

### 9.3 测试文件

- 装配：`测试用例/工程图测试用例/条码扫描设备/SAB1_0000_000.SLDASM`
- 宏：`src/solidworks_assetmate_mechdog/resources/insert_bom.swp`
- 成功案例图纸：`D:/WorkBuddy_space/_assetmate_test_run/bom_test/BOM_TEST.SLDDRW`

---

## 十、给规划者的一句话

**`SelectByRay` 这个卡点解决了，但 BOM 创建还在原地。**

方案的方法论（一次一变量、查签名不猜、宏只是宿主）都对，我也确实按它做了——**五组对照、严格照宏顺序、增量判定**。

结果是：**选择上下文建立成功了，插入仍然被拒。**

现在最该做的不是继续试参数，而是**查清 §5 那个矛盾**（宏的 `swModel.InsertBomTable4` 到底属于哪个接口）。这一点不搞清楚，任何纯 Python 尝试都是盲试。

**建议给这条设时间盒**：`comtypes` 查方法表 30 分钟 + 重新录一次宏 5 分钟，合计不到 1 小时。超时即定 `pilot`，转向几何测距与其余交付项。
