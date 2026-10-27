# SolidWorks COM 踩坑清单（pywin32 实测，SW 2024 / 32.4.0）

> 来源：2026-10-26 给风力仿生兽装配体按件配色时的真实调试过程。
> 标 ✅ 的是验证过能用的写法，❌ 的是"不报错但没效果 / 报类型不匹配"的写法。

## 一、外观（颜色）

### 外观有四层，别改错层

| 层 | 设置方式 | 影响范围 |
|---|---|---|
| 材质外观 | `set_part_material` / 材质库 | 零件 + 密度（会改质量属性） |
| 文档默认外观 | `doc.MaterialPropertyValues` | 该文档全部实体 |
| **组件外观** | **`comp.MaterialPropertyValues`** | **该实例（装配里按件上色就用这层）** |
| 面外观 | `Extension.SetMaterialPropertyValues`（选中面后） | 选中项 |

```python
# ✅ 逐组件
comp.MaterialPropertyValues = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
        [r, g, b, 1.0, 1.0, 0.88, 0.21, 0.0, 0.21])

# ❌ 文档级：会把装配里所有组件刷成同一色，且不报错
doc.Extension.SetMaterialPropertyValues(color_variant, 1)
```

### 9 个分量

```
[R, G, B, ambient, diffuse, specular, shininess, transparency, emission]
```
后 6 个**沿用默认** `[1, 1, 0.88, 0.21, 0, 0.21]`。把 ambient 降到 0.5 会让正红渲染成 `(72, 0, 0)` 的暗红——看起来像"没生效"。

### 数组必须封成 VARIANT

```python
comp.MaterialPropertyValues = [1, 0, 0, 1, 1, .88, .21, 0, 0]      # ❌ 静默失败
comp.MaterialPropertyValues = VARIANT(VT_ARRAY | VT_R8, [...])      # ✅
```

### 读回值 ≠ 文件里的值

装配引用的零件文档常驻内存，`CloseDoc` 后 `OpenDoc6` 会复用内存副本。判断"存没存进去"必须先清干净：

```python
sw.CloseAllDocuments(True)   # 再 OpenDoc6 才是从磁盘读
```

这一条害我误判了 20 分钟：文件明明是蓝的，读出来一直是白的。

## 二、打开文档

```python
# ❌ 参数不够 → "非选择性的参数"；Errors/Warnings 是 [out]
sw.OpenDoc6(path, 1, 1, "")

# ✅ byref VARIANT 占位
e = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
w = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
doc = sw.OpenDoc6(path, 1, 1, "", e, w)      # 1=part, 2=assembly；Options 1 = silent
```

注意：**SolidWorks 不允许同时打开两个同名文档**。若 `OutTXT\C Linkage.SLDPRT` 已开，再开 `D:\src\C Linkage.SLDPRT` 会返回 `None`——先把副本改名再开。

## 三、选择集

```python
comp.Select2(False, 0)          # ✅ 选组件
comp.Select4(False, None, False)  # ❌ 类型不匹配（Data 是 byref 指针）
ext.SelectByID2(name, "COMPONENT", 0,0,0, False, 0, None, 0)  # ❌ 末位参数 byref 报错
```

要改组件属性时，**不要走选择集**，直接拿 `IComponent2` 对象设属性最稳。

## 四、性能

- `asm.GetComponents(False)` 在 80 组件装配上要**数秒**；2026-10-26 在 46 组件的监视器支架上实测单次 **约 130 秒**（含首次打开零件的加载）。放进循环（每组调一次）会把会话拖到工具超时被杀。**只调一次，在内存里分组**。
- 本项目的做法：按文档路径缓存组件列表，`tree → dry-run → apply → tree` 只付一次 `GetComponents` 成本；用户外部增删组件后传 `refresh=true`。
- `SaveBMP(path, w, h)` 在 1400×1000 下约 5–10 s。
- 重开 80 组件装配 1–3 分钟，验证要挑时机。
- **关闭文档前必须先保存**。对未保存文档调 `CloseDoc` 会弹模态保存提示，自动化进程会被永久阻塞（无 UI 可点）。判断用 `GetSaveFlag`，保存用裸访问 `doc.SaveSilent`。
- **缓存不能只靠"读属性会抛异常"来判失效**。文档被外部关闭重开后，陈旧的 `IComponent2` 指针**不抛异常**，而是静默返回空串（`Name2=""`、`GetPathName=""`、`GetModelDoc2=None`）。此时任何依赖组件列表的读操作都会返回"成功但全空"的数据。校验必须同时满足：
  1. 缓存的 COM 文档身份与当前一致（`cached_ole == doc._oleobj_`，实测同对象返回 `True`、重开后为 `False`）；
  2. 首个组件的 `Name2` 非空。
  只做其中一条都会漏。

## 五、几个不可用的成员（本机实测）

这些在 pywin32 动态绑定下报"找不到成员"，不要浪费时间：

| 成员 | 现象 | 替代 |
|---|---|---|
| `SldWorks.GetMathUtility()` | 找不到成员 | 用组件现有 `Transform2` 的 `ArrayData` |
| `doc.Extension.CreateMassProperty` | 找不到成员 | 逐件用 MCP `analyze_model` 读质量属性，或手算合并 |
| `doc.SaveAs4(...)` | 非选择性的参数 | 用 MCP 的 save_document，或 `doc.SaveSilent` |
| `doc.Extension.GetMassProperties2(1, False)` | 类型不匹配 | 同上 |
| `SwApp.GetDocuments()` | 有时返回 `None` | 逐次 `OpenDoc6`，或先 `CloseAllDocuments` |

## 六、图像导出

```python
doc.SaveBMP(path, w, h)   # ✅ 但无论扩展名写 .png 还是 .bmp，实际都写 BMP（文件头 "BM"）
```

要 PNG：改名成 .bmp 再用图像库转，或用纯 Python 读 BMP + `zlib` 手写 PNG（无 Pillow 环境下的可行路径，约 30 行）。

## 七、保存文档（2026-10-26 实测新增）

`SaveSilent` 与 `GetSaveFlag` 在 pywin32 动态绑定下都是**属性**，不是可调用方法：

```python
doc.SaveSilent          # ✅ 访问即执行保存，返回 0/1（返回值不代表成功）
doc.SaveSilent()        # ❌ TypeError: 'int' object is not callable
doc.GetSaveFlag         # ✅ bool：True = 有未保存修改
doc.GetSaveFlag()       # ❌ TypeError: 'bool' object is not callable
```

返回值不可靠：实测保存成功后 `SaveSilent` 仍返回 `0`，应以 `GetSaveFlag` 在保存后变为 `False` 作为判据。

**重开后组件顺序会变。** 验证"颜色是否落盘"不能比对 `GetComponents()[0]`，必须按组件名（如 `P19-3`）查找同一实例。只关闭当前文档用 `sw.CloseDoc(title)`，不要为了验证而 `CloseAllDocuments`——会关掉用户正在编辑的其他文档。

## 八、实体级外观（2026-10-26 实测新增）

多实体零件按实体分色，官方文档推荐的两种写法在本机都不可靠：

| 写法 | 结果 |
|---|---|
| `body.MaterialPropertyValues = VARIANT(...)` | ❌ 不报错，回读永远是 `None`，颜色不变 |
| 选中实体 + `Extension.SetMaterialPropertyValues(color, 1)` | ⚠️ 小实体生效，758 面的主壳无效 |
| **逐面 `face.MaterialPropertyValues = VARIANT(...)`** | ✅ 全部生效，可回读，可靠 |

选择实体本身也有坑：`IBody2.Select2` 在晚绑定下报 `Python instance can not be converted to a COM object`，`SelectByID2` 的 Callout 参数必须传 `VARIANT(VT_BYREF | VT_DISPATCH, None)`：

```python
callout = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_DISPATCH, None)
doc.Extension.SelectByID2(name, "SOLIDBODY", 0, 0, 0, False, 0, callout, 0)
```

**结论：实体分色走面级。** 代价与注意点：

- 758 面约 12 秒；899 面的零件整体 apply 约 76 秒（含重建与保存）。
- 面级外观优先级最高，会覆盖原有的面外观/贴图，操作前务必用副本。
- `GetBodies2` 返回的实体**顺序不稳定**，跨调用必须按实体名匹配，不能用索引。

## 九、BOM 与重命名（2026-10-26 实测新增）

### 轻化组件读不到质量

装配默认轻化加载时，`comp.GetModelDoc2` 返回 `None`，`CreateMassProperty` 无从调用，19 个零件只能读到 1 个的质量。先一次性解析：

```python
doc.ResolveAllLightweightComponents(True)   # 返回值可能是 0，但文档确实加载了
```

之后 46/46 组件全部有 `GetModelDoc2`，总质量 0.126567 kg，与整机 `GetMassProperties` 一致。`CreateMassProperty` 只对**已加载**的文档有效，对未加载文档不会自动加载。

材料用 `GetMaterialPropertyName2("", VARIANT(VT_BYREF | VT_BSTR, ""))` 读取；未指定材料时返回**空字符串**，不是 `None`——判断时要 `strip()`。

### 重命名：实例名改不了，文件名可以

| 目标 | 结果 |
|---|---|
| `comp.Name = "x"` | ❌ `Property 'Name' can not be set` |
| `comp.Name2 = "x"` | ❌ 不报错，读回仍是旧名，`GetSaveFlag` 不变 |
| `doc.Extension.RenameDocument("x.SLDPRT")` | ❌ 返回 `2`，文件未改名（非 PDM 工作区） |
| **文件系统 `os.rename`** | ✅ 可用，但打开中的文件会失败 |

`IAssemblyDoc` 没有 `RenameComponent` / `RenameComponent2` 成员（用 `_oleobj_.GetIDsOfNames` 可直接探测成员是否存在，比 `dir()` 可靠，`dir()` 在 SldWorks 对象上会抛"找不到元素"）。

**结论：实例名只能 UI 改；工具用「另存 + 重定向」实现文件级重命名，并明确返回 `instance_rename_unsupported`，不造假。** `mode="file"` 会断装配引用，`mode="auto"` 会同步重定向引用。

### 装配打开时改不了零件文件名

装配会锁住组件文件，`os.rename` 直接报 `WinError 32 另一个程序正在使用此文件`。而"先关装配再改名"走不通——改名后装配找不到旧文件，直接断链。

可行路径是**另存 + 重定向**：

```python
part = open_document(old_path, SW_DOC_PART)
part.SaveAsSilent(new_path, False)      # ✅ 静默生成新文件（返回 0，文件已存在）
comp.ReplaceReference(new_path)         # ✅ 返回 1，单个组件立即改指向
doc.SaveSilent                          # 保存装配
```

实测：

- `SaveAsSilent` 会顺带更新已打开装配里指向该文档的引用；`ReplaceReference` 用于显式补刀，多实例要逐个调用。
- 保存并重开后路径与实例名都已更新，实例名按新文件名重排（`P01-1` → `RN_P01-1`）。
- 原文件保留，回退只需把引用改回去并删掉新文件。
- 关闭被装配引用的零件文档再改名无效（文件锁不释放），不要在这条路上浪费时间。

## 十、早绑定 gen_py 缓存让 byref VARIANT 全面崩溃（2026-10-27 用户实测）

现象：同一份代码，某些机器上 7 个工具全部失败：

```
TypeError: int() argument must be a string, a bytes-like object or a real number, not 'VARIANT'
```

根因：`%TEMP%\gen_py` 里存在 SldWorks 类型库的 makepy 缓存时，`GetActiveObject` 返回**早绑定** wrapper。它把 `OpenDoc6` 的 `Errors`/`Warnings` 声明为 byref int，在 `InvokeTypes` 里对传入的 `VARIANT` 调 `int()` → 直接崩。所有工具都要经过 `open_document`，所以全灭；只有纯文件系统的路径（`rename_components` 的 file 模式）幸存。

修复——拿到对象后强制晚绑定：

```python
import win32com.client.dynamic as dyn
_SW = dyn.Dispatch(win.GetActiveObject("SldWorks.Application"))
```

晚绑定路径下 `VARIANT(VT_BYREF | VT_I4)` 正常工作，且不依赖 gen_py 是否被别的程序生成过。

要点：

- **不要靠删 `gen_py` 缓解**——其他程序（含 makepy、其他 CAD 脚本）会再生成，问题必然复发。
- `EnsureDispatch` 对 SldWorks 会报 "This COM object can not automate the makepy process"，所以也没法用早绑定统一解决。
- 同类风险适用于所有自定义 `byref VARIANT` 调用（`SelectByID2` 的 Callout、`GetMaterialPropertyName2` 的 BSTR）。统一走晚绑定最省事。

## 十一、组件颜色读回失真（2026-10-27 用户实测）

在某些机器上，`recolor_assembly` 写入蓝/黄后：

- 渲染正确（预览图取色 `(24,64,195)` / `(251,181,5)`）
- 保存正确（关闭重开、`ResolveAllLightweightComponents` 后渲染依旧）
- 但 `comp.MaterialPropertyValues` 读回 `[1.0, 1.0, 1.0]`

同一台机器上面级 `IFace2.MaterialPropertyValues` 读回正常。出现条件与存在 gen_py 缓存、晚绑定包装早绑定对象一致，判断为**读路径的 VARIANT 解组问题，不是写入问题**。

对策：

- `recolor_assembly` 会**按 palette 每个零件键各抽 1 个实例**（最多 8 个）做读回校验，返回 `readback_sample` 与 `readback_verified`；为 `false` 时附 warning。
- 失真有两种形态：读回恒白 `[1, 1, 1]`，以及读回 `None`。**`None` 不能当作"该组件没有颜色"**，也不代表未设置外观。
- 失真按组件/会话随机分布，不是全文档属性，所以抽样必须覆盖每个零件类型，不能只取前 3 个组件。
- 此时**不要用 `previous_colors` 回退**——拿到可能是白色或 `None`。验收请用 `export_preview` 看图。
- `get_assembly_tree` 的 `rgb` 在这种环境下不可信，只看结构、路径、实例数。

另：`recolor_bodies` 的面级颜色在装配中会被**组件级颜色覆盖**（视口优先级：组件级 > 面级）。零件文件内颜色正确，但装配里看到的是组件色；工具已在 `note` 中提示。

## 十二、导出 STEP（2026-10-27 实测）

`doc.SaveAsSilent("x.STEP", False)` 可以直接写出有效的 STEP，而且**不会把文档路径改成 .STEP**——它按导出处理，不是真正的另存为：

- 46 组件装配 → 1.1 MB，约 2 秒
- `doc.GetPathName` 导出前后不变，可以连续导出、无需重开原文件
- 目标文件已存在时工具直接报冲突，不覆盖

PDF 必须来自 `.SLDDRW` 工程图，DXF 一般来自钣金平板型式或工程图视图；都不能从任意零件导出。`batch_export` 只接受 `step`，其余格式明确拒绝并给提示，不猜。

## 十三、工程图（2026-10-27 实测）

这一节是 4 个工程图工具的来源，每条都实机踩过。

### 模板路径必须是反斜杠

```python
sw.NewDocument(r"C:/.../gb_a3.drwdot", 0, 0, 0)   # ❌ 静默返回 None
sw.NewDocument(r"C://...//gb_a3.drwdot", 0, 0, 0)  # ✅
```

正斜杠不报错、不抛异常，只返回 None——是最容易误判成"模板坏了"的坑。模板本身可从
`C://ProgramData//SOLIDWORKS//<版本>//templates//*.drwdot` 找（GB 模板通常叫 `gb_a3.drwdot`）。

### 命名视图是本地化的

中文版 SolidWorks 的命名视图是 `*前视`、`*上视`、`*等轴测`，传 `*Front` 同样静默失败：

```python
names = list(model.GetModelViewNames)   # ('*正视于','*前视',...,'*等轴测',...)
```

正确做法是从模型读出真实名称再匹配，不要硬编码英文常量。

### 一批方法在晚绑定下是属性

| 写成方法 | 实际 |
|---|---|
| `doc.GetFirstView()` | `doc.GetFirstView`（属性，返回 sheet 的 IView） |
| `doc.GetViews()` | `doc.GetViews`（属性，返回**嵌套 tuple**） |
| `view.GetOutline()` | `view.GetOutline`（属性，返回 4 元素数组） |
| `view.GetDimensionCount()` | `view.GetDimensionCount`（属性） |

`GetViews` 的结构是 `((PyIDispatch, PyIDispatch, ...),)`，元素是裸指针，必须
`win32com.client.Dispatch(v)` 包装后才能读 `Name` / `Type` / `ScaleDecimal`。

### 视图插入前必须先开模型

`CreateDrawViewFromModelView3` 需要模型已加载；并且模型**必须先存盘**。工程图文档本身
可以在未保存状态下加视图，但模型不行。

### 新建视图读不到 outline

刚建好的视图 `GetOutline` 可能为空，设置 `ScaleDecimal` 之后才有值。因此比例不能靠"先建后量"，
要用 `model.GetPartBox(True)` 先估包围盒、选标准比例（1:1、1:2、1:5…），再一次性建视图并排版。

### 尺寸标注的注意事项

- 图纸坐标单位是米，A3 = 0.420 × 0.297。
- 标注必须点在投影边（`EDGE`）上，选不中时依次退化到 `SILHOUETTE`、`SKETCHSEGMENT`。
- 调用 `AddDimension2` 前要临时关闭 `swInputDimValOnCreate`，否则弹出数值输入框会卡死自动化。
- 剖视图必须先 `ActivateView` 目标视图，再选/画剖切线；否则 `CreateSectionViewAt5` 返回 null，
  At2 兜底甚至会让 SolidWorks 崩溃（RPC_E_SERVERFAULT）。

### 自动标注的三个 API（2026-10-27 实测）

```python
doc.InsertModelAnnotations3(0, 32768, True, True, False, True)   # 插入模型尺寸
view.AutoInsertCenterMarks2(1|4, 2, True, True, True, 0.0025, 0.0, True, True, 0.0)
doc.AddHoleCallout2(x, y, 0)                                     # 孔标注
```

- `InsertModelAnnotations3`：Option=0（FromEntireModel），Types 位掩码 32768（标记为工程图的尺寸）
  | 524288（全部驱动尺寸）；返回数组，长度即插入数量。P01 支架实测插入 6 个。
- `AutoInsertCenterMarks2` 是 **10 个参数**：InsertType(1=孔|4=槽)、Style(2=Single)、
  LinearSlotCenter、ArcSlotCenter、UseDocumentDefaults(True)、Size、Gap、ExtendedLines、
  CenterLineFont、Angle。**必须先 `ActivateView`**，否则不生效。返回 True 的视图数才是可靠信号，
  `GetCenterMarkCount` 在这个绑定下会低估（常读 0，但图上确实有标记）。
- `AddHoleCallout2` 必须点在孔的投影圆上，否则返回 None——工具据此返回 `callout_failed`，
  不伪造标注。
- `IModelDoc2.EditRebuild3` 是**属性**（bool），写 `EditRebuild3()` 会报 `'bool' object is not callable`。

### GB 模板的标题栏是写死的占位文字

新建的 GB 图纸标题栏显示 `"图样名称"`、`"图样代号"`（连引号一起），材料是 `<未指定>`。
这些**不是属性链接**，就是普通的 Note 文本，所以每张图都会原样显示占位词。

替换路径：

```python
sheet = doc.GetFirstView           # 属性
for ann in sheet.GetAnnotations:   # 属性，嵌套 tuple，需 Dispatch 包装
    if ann.GetType == 6:           # swAnnotationType_Note
        note = ann.GetSpecificAnnotation
        text = note.GetText        # 属性
        note.SetText("履带轮")      # 方法，返回 True
```

实测：一张 A3 图里 `图样代号` 出现 **2 次**：

| 位置 (m) | 角度 | 含义 |
|---|---|---|
| (0.391, 0.022) | 0° | 标题栏内的图样代号 |
| (0.053, 0.285) | **180°** | 图框左上角的倒置图号 |

左上角那处是 GB 折叠惯例：图纸对折后从背面能看到图号，所以模板里就是倒排的。
替换文字不会改变角度，因此它显示为倒着的编号——**这是标准，不是写错**。
`set_title_block(include_reversed=False)` 可以只改标题栏、保留倒置标记原样。

### 补标：按边索引选，不要指望坐标

`InsertModelAnnotations3` 只能搬模型已有的驱动尺寸。IR 重建的零件往往尺寸不全
（实测 P05：模型 38 个驱动尺寸，工程图只能拿到 27 个，缺的 7 个是基准面偏移、
7 个是草图内部尺寸，这两类不随模型项目导入），必须补标。

在工程图里补一个尺寸的可行顺序：

```python
# 1) 列边，拿索引
edges = view.GetVisibleEntities2(view, 1)        # 147 条边
edge  = win32com.client.Dispatch(edges[n])
# 2) 选边（Select2 可用，Select4 的 callout 参数会被拒）
edge.Select2(False, 0)
# 3) 加尺寸，坐标是图纸米
doc.AddDimension2(x, y - 0.015, 0).GetDimension2(0).SystemValue = 0.11
```

要点：

- **`edge.GetStartVertex.GetPoint` 返回的是模型坐标**，不是图纸坐标（P05 实测 x 为 -0.86），
  所以「按图纸坐标就近选边」是行不通的；要么按索引，要么让用户点在真实线上。
- `SelectByID2("", "EDGE", x, y, ...)` 在图纸中心这类空白处一定失败；点在真实投影线上才有机会命中。
  实测 `(0.1, 0.1)` 能命中，视图中心不能。所以工具先试坐标、失败再用 `edge_index`。
- `IEdge.Select4` 传 callout 会报"无效的参数数目"，用 `Select2(False, 0)`。
- `AddDimension2` 的 x/y 只是**尺寸文字位置**，与选中哪条边无关。

### 按图纸坐标补标（可用，2026-10-27 实测）

坐标选边本身行不通（`SelectByID2` 只认 VIEW），但可以**反过来投影**：
把每条可见边的中点从模型坐标换算到图纸坐标，再取最近的边选中。

```
paper = view.Position + (model - model_centre) * view.ScaleDecimal
```

三个前提：

1. `model_centre` 用该视图全部可见边端点的包围盒中心（不是零件 bbox）。
2. 要知道投影面：用视图 outline 的长宽比去匹配模型三组尺寸的长宽比
   （xy = 前视、xz = 俯视、zy = 侧视），最接近的一组即为投影面。
3. 换算只覆盖投影平面内的两轴，第三轴（深度）不参与比较。

实测：侧视图里取一条 275 mm 的边，按公式算出图纸点后补标成功（50 → 51 个尺寸）。

局限：投影面靠长宽比推断，**长宽比接近的视图可能判错**。优先用 `edge_index`
（`list_drawing_edges` 给出索引，稳定可靠），坐标路径作为便利入口。

### 尺寸去重怎么读、怎么比（2026-10-27 修正）

早前结论"读不回图纸尺寸"是错的，错在调用方式：

- `IView.GetDisplayDimensions` 确实只给 13 个 —— **不要用它**。
- 正确路径是 `IView.GetAnnotations` → `GetType() == 4` → `GetSpecificAnnotation.GetDimension2(0).SystemValue`，
  实测一张 50 尺寸的图能读到 **50 个**值。之前只看到 15 个，是因为值大量重复（5 mm 出现 5 次），
  不同值只有 11 种。
- 尺寸值是**投影长度**，不是三维边长：1253 mm 的边在侧视图里标注出来是 37.6 mm。
- 比较要用**整毫米**（`round(v*1000)`）：同一个尺寸在两个视图里最后几位浮点不同。
- 新增尺寸后 `doc.EditRebuild3` 才刷新注释列表，否则刚加的尺寸读不到。
- **`add_drawing_dimension` 必须保存**：只加尺寸不 `SaveSilent`，重开图纸就没了（这解释了
  为什么之前"补了尺寸却读不回来"）。

批量补标时仍建议同时传 `known_values`（由调用方记账），读值作为兜底。

### 模板安全区要从模板自己量

把边距写死成 A3 数值（左 34mm / 下 60mm）在 A2 上会直接失败，不同模板的标题栏、
明细栏尺寸也不一样。正确做法是在**加任何模型视图之前**，从图纸的 `GetLines3` 量出来：

```python
sheet = doc.GetFirstView
segments = _template_lines(sheet, w, h)   # 必须在加视图前，否则会混入视图线
```

两个必须知道的细节：

1. **`GetLines3` 每条线是 12 个数**：前 6 个是元数据，后 6 个才是 `x1,y1,z1,x2,y2,z2`。
   按 stride 6 读，每隔一条就会读到 `(0, 2, 1, 1)` 这种垃圾，坐标范围会跑到几十万。
   用「坐标必须落在图纸范围内」过滤比猜偏移量可靠。
2. **明细栏右边界会把图框的行列编号带进来**（A3 量出 0.12 m），按它留边会吃掉 30% 宽度。
   实测 A3 合理值是左 0.0588 / 下 0.065 / 右 0.004 / 上 0.004，所以量到的带宽要按比例截断
   （左 ≤ 14% 纸宽、下 ≤ 24% 纸高）。

量出的安全区会随结果返回（`safe_area.source` 为 `template` 表示实测，`ratio` 表示回退）。

### 局部放大视图：12 个参数，最后一个不能是 0（2026-10-27 实测）

```python
doc.CreateDetailViewAt4(X, Y, Z, Style, Scale1, Scale2, Label, ShowLabel, Flip,
                        NoOutline, Shape, NumCircleSegments)
```

- **必须传满 12 个参数**，第 12 个是边界圆的分段数。传 0 会报 `非选择性的参数`
  （DISP_E_PARAMNOTOPTIONAL），不创建任何东西——这就是它一开始看起来"不支持"的原因。传 32 正常。
- 和剖视一样，要先在**源视图草图**里画边界圆并选中：
  `sk → CreateCircleByRadius(sx, sy, 0, r)`，坐标用 `(paper - view.Position) / scale`。
- 圆心必须落在**有几何的地方**（角部孔、折弯、圆角）；落在空白处会得到一个空的局部视图。
- **比例锁在父视图**：新建的局部视图 `UseParentScale = True`，此时写 `ScaleDecimal` 无效
  （读回仍是父比例），`Scale1/Scale2` 参数也不生效。顺序必须是
  `UseParentScale = False` → `ScaleDecimal = 2.0`，而且**新建视图要保存一次之后才吃得进去**。
  实测：创建后立即设置无效；`SaveSilent` 之后再设，`ScaleRatio` 从 `(1,2)` 变成 `(2,1)`。

### 剖视图：剖切线必须画在视图草图里（2026-10-27 实测）

`CreateSectionViewAt5(X, Y, Z, Label, Options, ExcludedComponents, Depth)` —— **7 个参数**，
但光调用没用，必须先有一条**画在该视图草图里的剖切线**并处于选中状态，否则返回 null。

可行流程（实测通过，得到"剖面视图 A-A"）：

```python
doc.ActivateView(view.Name)
sm = doc.SketchManager
sm.InsertSketch(True)
# 草图坐标 = (图纸点 - view.Position) / view.ScaleDecimal  ← 用转换实体标定过
sm.CreateLine(-half, 0, 0, half, 0, 0)      # 水平剖切线
sm.ActiveSketch.GetSketchSegments → segment.Select2(False, 0)
sm.InsertSketch(True)                        # 退出，选择会保留
section = doc.CreateSectionViewAt5(x, y, 0, "A", 4, None, 0)
```

要点：

- `Options = 4` 是 `swCreateSectionView_ChangeDirection`（翻转观察方向），0 为默认。
- **剖视图与父视图 x 对齐锁定**：`view.Position = (x, y)` 只有 y 生效（写 x 不报错但读回不变），
  所以剖视只能放在源视图的正上/正下方。布局时把它当"父视图的附属视图"，不要当独立视图排。
- 剖切线要**穿过几何**；画在视图外或空白处会返回 null（而且失败几次后视图状态会变差，建议重开图）。
- `Position` 决定局部视图放哪，与剖切线无关。
- 实测配电箱箱体：3 视图 → 剖视 A-A，尺寸从 50 涨到 68 个。
  但**4 个视图（主+俯+侧+剖视）在 A3 / 1:2 下空间不够**，会互相挤压或压到明细栏；
  改成「主视 + 侧视 + 剖视」3 个视图后 `drawing_audit` 零警告。视图多时优先减视图或换 A2。

### 钣金展开视图（2026-10-27 实测）

```python
view = doc.CreateFlatPatternViewFromModelView3(
    model_path, config_name, x, y, 0, hide_bend_lines, flip)
```

- **7 个参数**，没有"自动配置"：不传 config 就读模型的 `ConfigurationManager.ActiveConfiguration.Name`。
- 零件必须有 `FlatPattern` 特征（钣金件）；装配体不能展开，传装配会返回 null。
- 返回的 IView 与普通视图一样可以 `ScaleDecimal`、可以重排。
- 实测：配电箱门板（163×215×10 mm）展开后 97×127 mm @1:2，折弯线标注"上 90° R2.54"正确显示。

钣金件要标在**展开图**上（下料尺寸、孔位、折弯方向），不是折弯后的形状视图。

### PDF 只能来自工程图

`export_drawing` 的 PDF/DWG/DXF 都以 `.SLDDRW` 为输入；`batch_export` 的 STEP 以模型为输入。
两者不要混用——PDF 从零件导出会得到空壳或直接失败。

## 十四、一条通用原则

> **先读现值，再改。**
> 凡是"多层属性"（外观、显示状态、配置、材质），第一步读一次目标对象的现值，它会直接告诉你该改哪一层。跳过这一步，靠猜 API 语义，返工成本是它的 10 倍。
