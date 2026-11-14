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

### GetOutline 恒定虚胖 11.9 mm（2026-10-29 实测，P2-13）

`IView.GetOutline` 返回的包围盒**宽和高各比真实投影大 11.9 mm**，与视图、比例无关：

| 视图 | 真实投影 | GetOutline | 差 |
|---|---|---|---|
| front (X×Y) | 59 × 28 | 70.9 × 39.9 | +11.9 / +11.9 |
| top (X×Z) | 59 × 80 | 70.9 × 91.9 | +11.9 / +11.9 |
| right (Z×Y) | 80 × 28 | 91.9 × 39.9 | +11.9 / +11.9 |
| A-A 剖视 (Y×Z) | 28 × 80 | 39.9 × 91.9 | +11.9 / +11.9 |

2:1 后仍是同一常数，说明是**固定偏移而非投影误差**。后果：`drawing_audit` 会把只在这层
虚胖边距里"相交"的两个视图误报 `views_overlap`，`_place_views` 摆位偏保守，auto_fit 会把
本可放下的视图缩小。

修法（`drawing.precise_outline`）：宽高各减 `OUTLINE_MARGIN_M = 0.0119`、四边各内缩 5.95 mm，
O(1)、零额外 COM 调用，与真实 ink bbox 吻合。**不要**在 audit 里逐视图遍历可见边顶点重算：
`ink_outline()` 那样做在 25 特征零件上实测约 200 s（见「四、性能」），只适合一次性交叉校验。

另外 `ink_outline` 早期版本返回的是**模型坐标**的 x/y，对 top/right 视图用错了投影轴；现在
统一用 `paper = view.Position + (model - centre) * scale` 投影到图纸米，与 `GetOutline`
同一坐标系后才能直接比较。

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

有的模板连占位词都没有——`gb_a3.drwdot` 的图样名称/代号格是**空文本**，文字匹配必然落空。
`set_title_block` 因此带位置兜底：文字匹配失败时按 GB A3 的格子坐标（`TITLE_SLOTS`）找最近的
注释再写入，返回里用 `via: "position"` 标出来。实测配电箱箱体：`name`/`code` 两格均按位置写入成功。

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

**去重为什么只能靠调用方传值**：判断"这个尺寸是否已有"必须知道它在新视图里会被标成多少，
而尺寸值是**投影长度**——同一条 1253 mm 的三维边，在主视图标 1253、在侧视图标 37.6。
读图纸现有值只能确认"哪些数值已存在"，无法预测新边在新视图的投影值。所以
`skip_duplicates` 默认关，只认调用方按目标视图投影长度传入的 `known_values`。

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

### 注释类操作必须自己保存

`InsertModelAnnotations3`、`AddDimension2`、`InsertCenterMark`、剖视/局部/展开视图创建之后，
文档是 dirty 的但**没有落盘**。之前只有 `add_drawing_dimension` 做了保存，结果是：

- 内存里 44 个尺寸，导出的 PDF 正常
- 磁盘上的 .SLDDRW 打开后**一个尺寸都没有**

给调用方的文件是空的。现在 `auto_dimension` / `add_center_marks` / `add_section_view` /
`add_detail_view` / `add_flat_pattern_view` / `add_dimension_tolerance` 全部 `SaveSilent`。

判断方法：任何"创建了对象"的操作，之后用 `drawing_dimension_values` 读一遍；读不到就是没保存。

### 圆柱面 ≠ 孔：IBody2.GetFaces 是方法

钣金件上每个 R2.54 折弯都贡献一个 Ø5.1 的圆柱面，所以"圆柱面清单"远多于"孔清单"。

- `IBody2.GetFaces` 是**方法**（必须 `GetFaces()`）；`IFeature.GetFaces` / `IComponent2.GetFaces`
  是属性。按属性调 body 的会静默拿到 bound method，扫描结果为空——这个坑让配电箱的 80 个圆柱面
  一开始一个都没扫到。
- 试过用「面法向 vs 径向」判孔/凸台：80 个面全部被判成同一类，与已知的 Ø6 安装孔矛盾，
  判据在本环境不可靠，已放弃。台账改为输出**全部圆柱面 + 直径/轴线/中心**，由调用方判断。

### 形位公差 / 粗糙度：能读不能写（2026-10-27 实测）

**能读**：形位公差的注释类型是 **5**（`swAnnotationType_Gtol`），读内容是

```python
gtol = annotation.GetSpecificAnnotation      # IGtol
gtol.GetFrameCount                            # 框格数
gtol.GetFrameValues(1)                        # ('0.01','','','','') 公差值 + 三个基准 + 符号
```

**不能写**，三条路都堵：

| 尝试 | 结果 |
|---|---|
| `doc.InsertGtol(...)` 0–4 个参数 | 全部 `找不到成员`（DISP_E_MEMBERNOTFOUND） |
| `doc.InsertSurfaceFinishSymbol2(...)` 4–9 个参数 | 全部 `参数数目` 错误 |
| 宏录制插入动作 | 只录到 `SelectByID2("细节项目275@图纸1", "GTOL", ...)`，插入本身不被捕获 |
| 复制粘贴：`doc.EditCopy()` 成功，`doc.EditPaste` / `EditPaste2` | **两个方法都不存在** |
| `gencache.EnsureDispatch` 早绑定 | `This COM object can not automate the makepy process`（SolidWorks 无类型库） |

**QueryInterface 试探（2026-10-27，最终结论）**

已知 `IDrawingDoc = {83A33D33-27C5-11CE-BFD4-00400513BB57}`、
`ISwDrawingDoc = {67930141-0BAC-4518-A40E-30AB15D6E9B8}`（注册表 HKCR\Interface 可查）。
用 `doc._oleobj_.QueryInterface(pythoncom.IID_IDispatch, IID)` 能成功拿到接口，绕开默认接口限制：

| 接口 | InsertGtol | InsertSurfaceFinishSymbol2 | InsertDatumTag |
|---|---|---|---|
| IDrawingDoc | 名字存在，**Invoke 报「找不到成员」** | 名字存在，报「非选择性的参数」 | 调用返回 None |
| ISwDrawingDoc | 同上 | 同上 | 同上 |

关键点：`GetIDsOfNames` 能找到方法名（类型库里有），但 `Invoke` 报 `DISP_E_MEMBERNOTFOUND`
—— 说明该版本（SolidWorks 2024）**运行时没有实现这个方法**，不是绑定方式的问题。
写 C# 插件也绕不过去。

**表面粗糙度有解（2026-10-27 查官方文档后实测通过）**

正确入口是 `IModelDocExtension.InsertSurfaceFinishSymbol3`，而且**必须给满 14 个参数**
（之前试 4–9 个，所以一直报「非选择性的参数」）：

```python
doc.Extension.InsertSurfaceFinishSymbol3(
    SymType, LeaderType, X, Y, Z, LaySymbol, ArrowType,
    MachAllowance, OtherVals, ProdMethod, SampleLen,
    MaxRoughness, MinRoughness, RoughnessSpacing)
```

实测 `(1, 1, 0.30, 0.10, 0.0, 0, 0, "", "", "", "", "3.2", "", "")` 成功返回 SFSymbol，
落盘后能在 `list_drawing_annotations` 里读回（`surface_finish` 类型）。
坐标是**图纸坐标**，给错位置会插在空白处，所以要么手动指定，要么先算好表面位置。

**形位公差确认无解（两条官方路径都不可用）**

官方文档给了两条路：

1. 旧：`ModelDoc2.InsertGtol()`（2016/2017 示例）→ 本机报 `DISP_E_MEMBERNOTFOUND`
2. 新：`IModelDocExtension.CreateGtolData()` + `ext.InsertGtol(data, x, y, z, ...)`（2015+ 推荐）
   → **本机 `CreateGtolData` / `InsertGtol` 两个成员都不存在**

所以不是参数问题、不是绑定问题，是该版本没有实现。C# 插件也绕不过去。

**形位公差四条路全堵（2026-10-28 在 SW2026 上逐一实测）**

| # | 路径 | 结果 |
|---|---|---|
| 1 | 图纸 `doc.InsertGtol()` | `DISP_E_MEMBERNOTFOUND` |
| 2 | 零件 `part.InsertGtol()` | 同样 `MEMBERNOTFOUND` |
| 3 | `IModelDocExtension.CreateGtolData` / `InsertGtol` | 成员不存在 |
| 4 | `InsertModelAnnotations3(0, swInsertGTols=0x20)` | 只能**搬运**模型已有 Gtol；模型侧创建不出来 → 无源可搬 |

补充排除的假设：不是"要先选中面"（选中状态对 MEMBERNOTFOUND 无影响）、
不是大小写（`InsertGTol` 同样失败）、不是对象类型（零件和图纸都试过）。

**注意 `MEMBERNOTFOUND` 的含义**：`GetIDsOfNames` 能找到方法名（类型库里有），
`Invoke` 却失败——说明该版本运行时**没有实现**，不是参数或调用顺序问题。
早绑定（C#）在同样运行时也会失败，除非 SolidWorks 提供别的入口。

**形位公差是能创建的——卡在 pywin32 的调用方式上（2026-10-28 突破）**

前面四条路都失败，原因不是 API 缺失，而是 **`doc.InsertGtol()` 被 win32com 当成属性读取
（DISPATCH_PROPERTYGET）**，于是必然返回 `DISP_E_MEMBERNOTFOUND`。

正确写法——用同一个 DISPID，但显式声明是方法：

```python
import pythoncom, win32com.client as win
ole = doc._oleobj_
dispid = ole.GetIDsOfNames("InsertGtol")
raw = ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)   # ← 关键
gtol = win.Dispatch(raw)
gtol.SetFrameValues2(1, "0.05", "", "A", "", "")
anno = gtol.GetAnnotation
anno.SetPosition(0.30, 0.15, 0.0)
```

实测：插入 2 个框格，回读 `frame1=['0.05','','A','','']`、`['0.1','','B','C','']`，
`list_drawing_annotations` 计到 `geometric_tolerance: 2`，图上框格位置正确。

**留下的问题**：框格内的公差**符号**（平面度 ∇、位置度 ⌖）没写进去。
`SetFrameSymbols2` 的第一个字符串参数传 `<MOD-FLAT-PAR>` 这类格式码无效，传空得到空符号格。
需要找正确的符号写入方式（可能是 `swGdtSym_e` 枚举值，或特定的格式码语法）。

**方法论教训**：`DISP_E_MEMBERNOTFOUND` 有三种完全不同的来源——属性/方法派发方式错、
接口指针不对、真的没实现。只测一种调用方式就断言"API 不存在"，会把自己堵死两天。

**`IInsertGtol` / `INewGtol` 报的是另外的错误**（61836/61837 "无法读/写只读属性"），
说明这两个带 `I` 前缀的 dispatch 变体参数封送规则不同，暂未使用。

**结论**：表面粗糙度**可写**（`add_surface_finish`）；形位公差**可创建**（
`add_geometric_tolerance`），符号待补。

`list_drawing_annotations` 因此定位为**自检工具**：证明图上有没有形位公差、内容对不对。

### 读尺寸挂在什么上（§6.1 关联校验）

```python
ann.GetAttachedEntities2      # (entity, entity) —— 通常两条边
ann.GetAttachedEntityTypes    # (11, 11)  11 = EDGE
```

- 类型码：10 vertex / 11 edge / 12 face / 13 sketch_segment / 14 sketch_point。
- **没有关联实体的尺寸无法追溯**，按流程 §6.1 属于未完成；只挂在 sketch_segment 上的也要复核。
- 实测配电箱图 44 个尺寸：24 处挂边、7 处挂顶点、8 个 `type_1`、1 个 `type_0`、
  **21 个无关联**。孤儿比例这么高说明 `InsertModelAnnotations3` 导入的尺寸不一定绑到几何上，
  这正是「导入成功 ≠ 图纸正确」的实证。
- `delivery_check` 把「无关联尺寸」计入检查项，**默认按 warning**（`pending`），
  `strict_attachments=True` 才升级为 blocker（流程 §6.1 的字面读法）。

### 无关联尺寸是不是导入参数造成的？不是（2026-10-27 实测）

`InsertModelAnnotations3` 必须传 **6 个参数**（传 5 个报「非选择性的参数」）。逐一改参数试关联率：

| 调用 | 尺寸数 | 无关联 |
|---|---|---|
| `(0, T, True, False, False, True)` 现用 | 44 | 21（47.7%） |
| `(0, T, True, True, False, True)` 第4参数改 True | 36 | 17（47.2%） |
| `(0, T, True, True, True, True)` | 46 | 24（52%） |

**孤儿比例不随参数变化**，稳定在 47–52%。要么这些尺寸真的没绑几何，要么
`GetAttachedEntities2` 对它们就是不返回 —— 目前无法区分。

所以不能拿它一票否决：默认只报 warning，需要严格判定时传 `strict_attachments=True`。

### 装配体不能脱离引用复制（2026-10-28 教训）

把 `.SLDASM` 单独复制到临时目录去建图，组件引用全部断开，`CreateDrawViewFromModelView3`
会返回**没有几何的视图**：三个视图 outline 全是 0.0357 × 0.0252 m，`GetVisibleEntities2` 返回 0 条边，
看起来像"装配建图功能坏了"。

实际上原路径建图一切正常：`box = [0.8, 1.098, 0.8852]`、比例 1:20、视图有内容。

**结论**：测试装配必须保持引用完整——要么在原目录建图、要么整目录复制（含所有零件）。
只复制 `.SLDASM` 得到的"空白视图"不是产品缺陷。

### 材料：SetMaterialPropertyName2 与本地名称（2026-10-27 实测）

```python
part.SetMaterialPropertyName2("", "SolidWorks Materials.sldmat", "普通碳钢")
part.MaterialIdName     # -> 'solidworks materials|普通碳钢|9'
```

- 数据库名固定 `SolidWorks Materials.sldmat`；**材料名必须与本地材质库完全一致**。
  实测「普通碳钢」有效，「铝合金 6061」「不锈钢 304」写入后 `MaterialIdName` 为空 —— 名称不对。
- 写入后要 `EditRebuild3` 才会更新质量；没有材料时 SolidWorks 按 1000 kg/m³ 算，BOM 重量只是体积代理。
- 材质库 XML 在 `C:/ProgramData/SOLIDWORKS/SOLIDWORKS 2024\` 下能找到 `*.sldmat`，但
  `<material name="...">` 正则解析不出条目（格式/编码不符），自动列名称暂不可用。

### 真实图纸基线（100 张实测，2026-10-28）

用 `D:/142套solidworks工程图图纸模板` 里 1–3 号（6346 张 SLDDRW）随机抽 100 张跑
`drawing_audit` + `list_drawing_annotations`，**100/100 全部可读，单张约 7 秒**。结论：

| 指标 | 平均 | 中位 | 范围 |
|---|---|---|---|
| 视图 | 3.1 | 3 | 1–9 |
| **尺寸** | **14.2** | **11** | 0–58 |
| 注释 | 46.5 | 36 | 0–160 |
| 中心标记 | 4.9 | 3 | 0–59 |

制造类标注的出现率：

| 标注 | 有它的图纸 |
|---|---|
| **表面粗糙度** | **60 / 100** |
| **形位公差** | **7 / 100** |
| 零件序号 | 3 / 100 |

**据此改了三处判定**（原来都是我拍脑袋定的）：

1. `auto_dimension`：全量导入产生 44 个尺寸，是真实图纸（14 个）的 3 倍。现返回 `advisory` 提示，
   建议关闭 `include_unmarked` 或事后筛选。
2. `delivery_check`：**粗糙度升为常规检查项**（60% 的图有）、**形位公差降为可选**（仅 7%）。
   我们恰好是"粗糙度能写、形位公差写不了"，方向是对的。
3. `drawing_audit`：`no_dimensions` 原本按视图报警，**79/100 的真实图会误报**（轴测图、示意视图本来
   不带尺寸）。改为只在**整张图无任何尺寸**时报 `no_dimensions_anywhere`。复测 5 张真实图，误报归零。

**批量读图的硬性前提**：SolidWorks 同时开 40+ 文档后任何新文件都打不开
（"could not open the document"）。必须**每张读完就 `CloseDoc`**，否则第 2 张起全部失败。

### 台账对账：按值匹配要先过滤两类尺寸（2026-10-27 实测）

`reconcile_ledger` 把台账的驱动尺寸逐个到图纸上找对应值。直接比会有大量假缺失，两类要先剔掉：

| 类型 | 判断 | 例（配电箱） |
|---|---|---|
| 非长度尺寸 | `dim.GetType` 不是 0/2/3（线性/直径/半径） | 角度、坐标尺寸 8 个 |
| 超出包围盒 | 值 > 包围盒对角线 × 2 | 阵列间距 6000/8000，草图 2000/4000，共 4 个 |

过滤后：台账 58 个有效长度尺寸，图纸 22 个值，匹配 51，**覆盖率 87.9%，缺 7 个**。
缺的具体是 Ø30、Ø1、Ø4（沉孔）—— 这才是可执行的「补这些就齐了」清单。

注意匹配是按值且容差 0.5 mm，而图纸尺寸是投影长度，所以结果只能当待确认清单，不能当判决。

### AddHoleCallout2 必须先 SelectByID2 选边（2026-10-27 对照 SolidPilot 源码）

SolidPilot 的实现（`SolidWorksService.Drawing.cs`，AddHoleCallout）比我多做一步：

```csharp
modelDoc.ClearSelection2(true);
bool selected = modelDoc.Extension.SelectByID2("", "EDGE", px, py, 0, false, 0, null, 0);
if (!selected)
    selected = modelDoc.Extension.SelectByID2("", "SILHOUETTE", px, py, 0, false, 0, null, 0);
if (!selected) return "EDGE_NOT_FOUND";
object callout = drawingDoc.AddHoleCallout2(px, py, 0.0);
```

**选择集本身就是上下文**：先选中投影边，AddHoleCallout2 才知道标哪个孔、属于哪个视图。
只给坐标不选边，即使点位正确也返回 null —— 这就是我们之前全部失败的原因。

他们自己在注释里也承认这个方法的局限：
`coordinate selection is fragile for crowded geometry (KNOWN-LIMITATIONS #6)`。

**实测（配电箱结构图）**：按网格点试选 `EDGE`，15 个点里 12 个能选中 —— SelectByID2 本身有效；
但 `AddHoleCallout2` 在这些点上仍返回 null，因为网格落点命中的是**直线边**，不是孔的投影圆。
结论：方法正确，但必须先拿到**圆边上的精确点位**，而这需要扫边。

**扫边是硬瓶颈**：`GetVisibleEntities2(view, 1)` 取到 260 条边后，即使只用端点算弦长预筛
（不调 GetCurve），一个视图仍跑不完 400 秒。慢在实体枚举与逐边顶点读取，不在曲线判断。
要提速得换 API（如按视图分片、或用 `IView.GetPolylines7`），没在本次解决。

**当前结论**：孔标注在纯 Python 下可用性不足，建议界面手工或用 `add_drawing_dimension` 标 Ø 尺寸。

### 圆边识别：GetCurve 必须 Dispatch

```python
curve = win32com.client.Dispatch(edge.GetCurve)   # 少了这一步 IsCircle 恒失败
if curve.IsCircle: ...
```

配电箱侧视图 260 条可见边里有 **82 条是圆**，之前"找不到圆"纯粹是没包 Dispatch。
但逐边取曲线在大视图上很慢（260 边 > 300 s），批量孔标注要先按直径/位置过滤再取。

### PDF 只能来自工程图

`export_drawing` 的 PDF/DWG/DXF 都以 `.SLDDRW` 为输入；`batch_export` 的 STEP 以模型为输入。
两者不要混用——PDF 从零件导出会得到空壳或直接失败。

## 十四、一条通用原则

> **先读现值，再改。**
> 凡是"多层属性"（外观、显示状态、配置、材质），第一步读一次目标对象的现值，它会直接告诉你该改哪一层。跳过这一步，靠猜 API 语义，返工成本是它的 10 倍。

## 十五、标注身份、类型与位置（0.8.19，P1-6 / P0-2 / P1-7）

**尺寸身份是 `IDimension.FullName`，不是 `IAnnotation.GetName`。** `GetName` 只是图纸内
唯一的 `细节项目N` 标签；同一个驱动尺寸 `D1@草图1@零件.Part` 在 front / top / right
三个视图各插一次，会得到三个互不相同的 `细节项目N`。`auto_dimension` 靠
`InsertModelAnnotations3` 自带的 `eliminate_duplicates` 标志去重**没有用**（实测 56 个
尺寸散在 4 个视图）。可行做法：插入前快照所有 `GetName`，插入后 `EditRebuild3`，
再按 `IDimension.FullName`（回退 `Name`）分组，保留首次出现，其余用
`annotation.Select(False)` + `EditDelete` 删除。**只处理本次新增的标注**——
`before_names` 快照把用户先前放的尺寸全部排除，否则一次自动标注会删掉别人的成果。

**`IDisplayDimension.GetAnnotation` 必须按方法调用。** 晚绑定下写 `dim.GetAnnotation`
会被当 PROPERTYGET 派发，拿不到 `IAnnotation`；用 `call_method(dim, "GetAnnotation")`
再 `Dispatch` 才能读 `GetPosition`。这是 `add_hole_callouts` 回读引线真实落点
（`position_m`）的依据——请求落点 `anchor_m` 与真实落点可能不一致，必须都返回。

**`IAnnotation.GetType == 4` 才是尺寸。** note / GTOL / 粗糙度是别的类型。
`set_dimension_position` 先查类型，非尺寸直接回 `not_a_dimension`，避免名字打错时
静默移动错对象；`SetPosition(x, y, 0)` 之后必须 `EditRebuild3` 再回读
`GetPosition`（section / detail 视图会保持投影对齐而拒绝部分移动，所以要返回
`requested` 与 `position_after_m` 供调用方判断）。

**`EditRebuild3` 是方法不是属性。** `auto_dimension` 里曾写成裸 `doc.EditRebuild3`
属性访问，插入后 `view.GetAnnotations` 仍是旧集合，去重遍历扑空；必须
`call_method(doc, "EditRebuild3")` 真正调用才会刷新。

## 十六、局部视图、形位公差符号与表面粗糙度（0.8.20，P2-9 / P2-10 / P2-11）

### 局部视图：`scale` 参数曾被父视图比例覆盖

`add_detail_view` 里写过 `scale = float(target.ScaleDecimal)`，把用户请求的
2.0（2:1）直接换成父视图的 0.5，于是 `requested_scale` 也报 0.5、
`scale_locked` 恒为 False——一个永远抓不到的"锁"。修法：请求比例存
`requested_scale`，父视图比例单独存 `parent_scale`，**只**用来换算边界圈草图坐标
（`(paper - view.Position) / parent_scale`）；创建后 `_apply_detail_scale(doc,
detail, requested_scale)` 设 `UseParentScale=False` + `ScaleDecimal` 并回读。
实测 `_apply_detail_scale(2.0)` 生效，视图名从 `局部视图 A (1 : 2)` 变成
`局部视图 A (2 : 1)`。

**判空信号**：`IView.GetVisibleComponents` 在边界圈落在源视图几何之内时返回
1 个组件、`GetVisibleEntities2(component, 1)` 得到 1 条边；落在几何之外返回
`None`、0 条边。`GetVisibleEntities2(None, 1)` 会报"类型不匹配"，
`GetPolylines7` / `GetEdgeCount` / `IDisplayData.GetEdgeCount` 在这个晚绑定环境
不可用，别用它们判空。所以创建后数一次可见边，0 条就返回 `detail_empty`
（视图是空的，别报 ok:true）；比例被锁则返回 `detail_scale_locked` error。

### 形位公差：`<MOD-*-PAR>` 不是符号，是字面文本

`IGtol.SetFrameSymbols2(1, code, ...)` **返回 None**，成功失败都靠回读判定：
`GetFrameSymbols2(1)` 返回 6 元组，第一格就是刚写入的内容。实测
`SetFrameSymbols2(1, "<MOD-POS-PAR>", ...)` 回读 `('<MOD-POS-PAR>', ...)`——
格式码不会被解析成位置度符号，只会原样显示在框格里；而
`SetFrameSymbols2(1, "位置度", ...)` 回读 `('位置度', ...)`。
因此 0.8.20 起 `GTOL_SYMBOLS` 全部改成可读中文文字，写入后用
`GetFrameSymbols2` 校验，不一致就 `IAnnotation.Select(False)` + `EditDelete`
删掉刚建的框格并返回 `gtol_symbol_not_applied`。**这是文字标签，不是真正的
GD&T 几何符号**，出正式图纸仍需在界面替换。（另注：`GetFrameValues2` 在新建的
空 Gtol 上报成员找不到，回退 `GetFrameValues` 可用。）

### 表面粗糙度：`InsertSurfaceFinishSymbol3` 忽略 x/y

14 参调用能创建符号、`list_drawing_annotations` 也能列出 `surface_finish: 1`，
但 annotation 的 `GetPosition` 实测是 `[0.0, 0.0, 0.0]`（type 7）——
传入的 x/y 被忽略，符号落在图纸原点。早期只回显请求值
`position_m=[x,y]` 属于谎报。修法：创建后 `IAnnotation.SetPosition(x, y, 0)`
（实测返回 True、回读 `[0.3, 0.2, 0]`），再 `EditRebuild3`。

**像素可见性自检的坐标映射**：SolidWorks 图纸原点在**左下**，渲染出的 PNG
原点在**左上**，所以 y 要翻转：
`px = x / sheet_w * image.width`，`py = (1 - y / sheet_h) * image.height`。
在插入符号前后各 `SaveAsSilent` 一份 PDF、`render_drawing` 成 PNG，
用 `ImageChops.difference` 在 (px,py) 的 ±50 px 窗口统计变化像素。
实测修好位置后 diff bbox `(1684,493)-(1736,551)` 正好落在目标像素
`(1701,549)`，窗口 changed=265；未修位置前 diff 只在左下角原点。
DWG 是矢量、没有像素通道，只能检查文件非空，可见性以 PDF 为准。

## 十七、模型坐标 → 视图坐标：三条 API 路都堵死，最后靠尺寸匹配（0.8.21，P1-4）

要支持「用模型坐标指定剖切面」，必须先知道**图纸的两个轴分别对应模型的哪个轴**。
2026-10-31 在 SW2026 + 灌装阀阀体（59×28×80）上依次排除了三条路：

| 试的路 | 结果 |
|---|---|
| `IView.GetViewPlaneNormal` | 属性访问 `AttributeError`，按方法调用 `DISP_E_UNKNOWNNAME`（未知名称）——该版本不存在此 API |
| `IView.ModelToViewTransform` | **存在**，返回 16 个 double，但**不是行主序 4×4**：平移量与一个孤立的 `-1` 对不上，按任何常见分组都凑不出正交矩阵 |
| `IMathPoint.MultiplyTransform` | `DISP_E_MEMBERNOTFOUND`（找不到成员），无法让 SolidWorks 自己算变换 |

**还有一条更隐蔽的坑**：`IView.GetVisibleEntities2(component, 1)` 返回的是
**轮廓边（silhouette edge）**，不是 `IEdge`——`GetStartVertex` 返回 `None`，
只有 `GetCurve` 有值。实测 3 个视图各 10/14/11 个实体，取顶点全军覆没。
这直接意味着 `ink_outline()` 里的 `edge.GetStartVertex.GetPoint` **永远拿不到顶点**，
它只能返回 `None`；该函数保留为"理论上的精确交叉校验"，**不要依赖它出数**。
要数顶点只能走 `IBody2.GetEdges`（从零件本体拿），那是另一条更慢的路。

**最终方案：尺寸匹配（唯一可行且可解释的路）**

1. `IView.GetReferencedModelName` 拿模型路径（属性、方法都可用）；
2. `open_document` 打开零件，`GetPartBox(True)` 拿包围盒 → extent + centre；
3. 视图的 **deflated outline**（`GetOutline` 减 11.9 mm，见第十六节 P2-13）宽高，
   去和包围盒三轴 extent 的**两两组合**做匹配，误差最小且唯一者胜；
4. 误差超 `max(w,h)×5%` → `axes_mismatch`；两组误差相同（正立方体）→ `axes_ambiguous`，
   **拒绝而不是猜**——猜错就是切错平面，比报错严重得多。

**读 part box 会切换 ActiveDoc**：`open_document(model_path)` 之后必须重新
`_open_drawing(drawing_path)` + `Doc.ActivateView(name)` 才碰视图草图，
否则 `doc.SketchManager` 画到零件上去。

**offset 语义（实测锁定）**：`offset_m = (origin[dominant] - centre[dominant]) × scale`，
图纸米；`orientation != "vertical"` 时内部仍取反（0.8.10 的 top 视图草图 y 指向 -Z 那条）。
所以调用方始终按「模型坐标 − 零件中心」思考，不需要关心符号。

**剖切 sanity 的几何依据（不是启发式）**：剖切平面垂直于图纸**水平轴**时，
它**不可能**改变图纸水平方向的尺寸——剖视图的宽必须等于源视图的宽（竖切则比高）。
容差 10%，实测：

| 场景 | 源视图 deflated 宽 | 剖视 deflated 宽 | ratio | 判定 |
|---|---|---|---|---|
| 法向 +Y 过中心（正确） | 58.98 mm | 58.98 mm | 1.000 | ok |
| offset +0.012（切进 Ø20 孔，错） | 58.98 mm | 14.40 mm | 0.244 | `section_suspicious` |

`depth > 0` 的部分剖尺寸**本来就该缩小**，所以 sanity 记 `checked=false` 并写明原因，
不做判定——这与「宁可 not_checked 不要错数」一致。

**六场景真机结果（2026-10-31）**：法向 +Y → `horizontal` / 轴映射 `[0,1]` / ratio 1.000；
法向 +X → `vertical` / ratio 1.04；法向平行视线 → `cut_plane_not_in_view_plane`；
斜切 0.707 → `cut_plane_oblique`；错位 offset → `section_suspicious`；offset 0 → ok。
6/6 通过。

## 十八、COM 桥接自愈与 outline 单一真源（0.8.22，P0-1 / P2-13）

### 桥接：缓存的 dispatcher 会变成死指针

症状：连续约 50 分钟后所有调用报 `Not connected`，持续 120 s 以上不恢复，而 SolidWorks
本体完全正常（`GetActiveObject` 拿到的 `RevisionNumber` 仍是 32.4.0）。
根因：`get_sw()` 缓存 dispatcher 后**永不验证**，死指针一路复用。

三条实测支撑的实现决策：

1. **存活探针 18 ms**：`RevisionNumber` 一次往返实测 17.97 ms（20 次平均）。
   这个成本不能每次 `get_sw()` 都付（`open_document` 在一个工具里会调好几次），
   所以节流到 `LIVENESS_PROBE_INTERVAL_S = 2.0` 秒；探针失败立即丢缓存重新
   `GetActiveObject`。
2. **`pythoncom.CoRegisterMessageFilter` 在本机不存在**（`hasattr` 为 False），
   所以「SW 忙 → 拒绝调用」不能靠 COM message filter 自动重试，只能手工分类：
   - 忙（`RPC_E_CALL_REJECTED` 0x80010001 等）→ 退避 0.25 s / 0.5 s 重试，最多 2 次
   - 断线（`CO_E_OBJNOTCONNECTED` 0x800401FD 等）→ **不重试**，丢缓存 + 抛 `BridgeLost`
   - `DISP_E_MEMBERNOTFOUND`（0x80020003）两者都不属：既不重试也不重连
3. **不做线程级硬超时**：pywin32 在拥有对象的 STA 线程上派发，把调用丢到工作线程
   再「超时放弃」会破坏 COM 公寓，而不是取消调用。所以覆盖的是「失败后永不恢复」
   这个真实症状，而不是「单次调用挂死」（后者是 SolidWorks 侧状态，客户端无法安全中断）。

顺带修掉一个更早的坑：`_LAST_CALL` 只在第一次 `note_last_call()` 之后才存在，而
`log_failure()` 会读它——**最早那次失败**（正是断线时刻）会让日志函数自己抛
`NameError`，写进 `_logger_error.log` 而不是 `mcp-*.log`。现在模块级有默认值。

### outline：11.9 mm 是唯一真源，逐顶点路全部堵死

`IView.GetOutline` 宽高各多报 11.9 mm（固定偏移）。曾写过 `ink_outline()` 想用可见边
顶点算精确 bbox 做交叉校验，2026-10-31 实测**四条路全断**：

| 路 | 结果 |
|---|---|
| `IView.GetVisibleEntities2(comp, 1)` | 返回**轮廓边**，`GetStartVertex` → None，只有 `GetCurve` |
| `IBody2.GetVertices` | 该阀体 31 面 / 32 边全是闭合圆边，返回 **None** |
| `IEdge.GetStartVertex`（走 call_method） | 仍返回 None（32 条边、0 个顶点） |
| `ICurve.GetEndPoint` / `GetTessPoints` | 该版本**没有**这些成员（AttributeError / UNKNOWNNAME） |

所以 `ink_outline()` 从来只会返回 None——一个静默失效的「交叉校验」，已删除。
`precise_outline()` 常数内缩不是捷径，而是唯一可行解，精度实测三视图
59.0×28.0 / 59.0×80.0 / 80.0×28.0（真值 59×28 / 59×80 / 80×28，误差 <0.1 mm），
detail 视图差值 11.92 mm。

注意 `ISheet` / `GetFirstView.GetOutline` 是**精确的**（实测 420×297 mm），不受该偏移
影响——所以像素映射（`px = x / sheet_w * W`）不用改。

## 十九、标注删除的成功判定与尺寸显示文本（0.8.23，P0 / P1-1 / P3-3）

### 删除成功不能只看图纸视图的注解数

`doc.GetFirstView` 是**图纸**（Type 1），它的 `GetAnnotations` 数量在删除**尺寸**时
**不变**——尺寸属于模型视图。实测（2026-10-31，灌装阀阀体）：

| 时机 | 图纸1 | 工程图视图1 | 全视图合计 |
|---|---|---|---|
| 删除前 | 83 | 8 | 122 |
| 删掉一个 D1 后 | **83（不变）** | **7** | **121** |

所以 `removed = after < before` 若只比图纸，会把**成功的删除报成 `delete_failed`**——
比失败更糟：调用方会重试，然后误以为"这工具删不掉"。现在改为跨全部视图求和，
并在回执里写 `counted_scope: all_views`。

附带一个更隐蔽的事实：`IAnnotation.Select(False)` 对尺寸**是有效的**
（`GetSelectedObjectCount2` 返回 1），`EditDelete` **也确实删掉了**。v0.8.14 之所以
"四类全失效"，根因与类型无关——`_find_annotation_by_name` 里写了未导入的
`call_method`，`NameError` 被外层 `except` 吞成"没有候选"，于是对**刚被列出过的名字**
回 `annotation_not_found`。

**重名**：SolidWorks 按草图复用 `D1`/`D2`/`D3`，一张图实测有 **19 个 `D1`**。
`delete_annotation` / `move_annotation` 现在接受 `position_m`，用
`list_drawing_annotations` 报告的位置精确定位具体那一个。

### 尺寸的显示文本在槽 1，不是 `GetText()`

| 调用 | Ø20 直径尺寸 | 线性尺寸 |
|---|---|---|
| `IDisplayDimension.GetText()` | `''` | `''` |
| `GetText(0)` | `''` | `''` |
| `GetText(1)` | **`'<MOD-DIAM>'`** | `''` |
| `GetText(2)` | `''` | `''` |
| `GetType` | **5** | 2 |

`GetText()` 不带槽号对尺寸返回空串——尺寸的值与格式码都在**槽 1**。
`delivery_check` 之前用"文本里有没有 `Ø` / `MOD-DIAM`"判直径，于是恒为 0 处，
把一张印着 Ø28/Ø23/Ø12/Ø18/Ø13 的图判成「仅供参考/未完成」。

现在两条路并用：`dimension_type`（`IDisplayDimension.GetType`，5 = 直径）作主判据，
`raw_text`（槽 1 原文）+ 解析后 `text`（`<MOD-DIAM>`→`Ø`、`<DIM>`→数值）作兜底。
实测 38 个尺寸里 18 个 `dimension_type == 5`，`delivery_check` 从「0 处 Ø 尺寸」
变为「17 处 Ø 尺寸」。

### 0.0 不是长度

`reconcile_ledger` 会把值为 0.0 的台账项当成"图纸缺项"，但 0.0 是草图几何关系/约束值，
任何图纸都标不出来。现在分离到 `skipped_zero_value`，覆盖率分母改用
`comparable_dimensions`（实测 45 个台账项里 2 个是 0.0 → 43 个可比项）。

## 二十、圆边枚举、标注碰版与注记避让（0.8.24，P1-2 / P2-4 / P3-1 / P3-2 / P3-4）

### 视图里的"边"没有顶点，`GetStartVertex` 恒为 None

`list_circular_edges` 早期用 `edge.GetStartVertex` / `GetEndVertex` 算弦长做预筛，
在阀体上返回 **0 条**——因为视图可见实体是**轮廓边（silhouette edge）**，没有顶点
（与第十八节同源）。改用 `ICurve.CircleParams`（一次调用给出圆心/轴/半径）后同一张图
返回 **23 条**，坐标由 `_view_axis_map` 的尺寸匹配投影；映射不可用时给 `x: null`
而不是编一个坐标。

### `AddHoleCallout2` 的失败与选择方式无关

同一张图、同一批圆边，三种选择方式全部返回 null：

| 方式 | 选中 | `AddHoleCallout2` |
|---|---|---|
| `SelectByID2("", "EDGE", 边位置)` | ✅ | **null** |
| `edge.Select2(False, 0)` | ✅ | **null** |
| 相邻面 `face.Select2(False, 0)` | ✅ | **null** |

而同一调用在钣金拉伸孔（门板 Ø8/Ø15）上成功。所以判据不是"选没选中"，而是**几何类型**。
0.8.24 起返回 `verdict: no_native_callout` 并附 `native_callout_note`，
让调用方不要再换 offset 反复重试。

### 布局避让：标签 note 是障碍，不是尺寸

两个独立 bug 叠在一起：

1. `int(annotation.GetType())` —— `GetType` 在晚绑定下是**属性**，加括号必抛
   `TypeError`，而外层 `except: pass` 把它吞了 → **类型过滤从未生效**，note 被当成尺寸搬动。
2. 即便 note 不被移动，sweep 也只避让尺寸，把 Ø8 推进了 "2:1" 标签，渲染成 `2Ø8¹`。

修法：`annotation_type()` 属性优先 / 方法回退；`kind != 4` 才当尺寸；note（`kind == 6`）
作为 **fixed** 障碍塞进 `placed`，撞上时尺寸**向下**让（向上会撞回视图）。

### 注记避让的估计口径

`add_note(avoid_collisions=True)` 按「行数 × 字高 × 1.6 行距」估高，
按「最长行 × 字高 × 0.75」估宽（中文近方、拉丁约 0.55，取中偏保守）。
障碍用 `precise_outline`，所以 11.9 mm 虚胖不会把注记无故推走。
实测：注记放在视图中心会被下移 3 次（`0.253 → 0.2074`）落到视图下方。

### 版本号不能说明"跑的是哪一份代码"

一份反馈花了一整轮去追已经修好的问题：MCP 宿主指向的是**旧副本**的 editable 安装
（0.8.14），而工作区里已经是新版本；机器上同时存在三份源码目录。
版本号无法澄清这一点，**路径可以**。所以 `ping` 的 `runtime` 与 `check_capabilities`
都回显 `server_path` / `python_executable` / `python_version` / `pid`——
一次调用就能确认口径，不必再去翻 `.pth` 文件。

## 二十一、裸名字 NameError 被 `except` 吞掉：一个藏了五个版本的 P0（0.8.25）

### 现象

`auto_dimension(eliminate_duplicates=True)` 从 0.8.19 起一直报
`duplicates_removed: 0`，看起来像"这张图本来就没有重复尺寸"。
实际上每一次删除都抛 `NameError`，被外层 `except` 吞掉，一条也没删成。

### 根因

`drawing.py` 里有 5 处 `_call_method(doc, "EditDelete")`，但**模块级从未定义 `_call_method`**——
只有一处 `from ..sw_session import call_method as _call_method_sf`（别名不同）。
Python 在运行时才解析裸名字，于是：

- 删除 → `NameError: name '_call_method' is not defined`
- 外层 `try/except Exception` → 静默跳过
- 汇总 → `removed=0`，**看起来完全正常**

同一个坑在 `layout.py` 的 `_find_annotation_by_name` 上出现过一次（v0.8.14 的"四类标注全失效"），
这次是第二次。

### 为什么两道防线都没抓到

| 防线 | 为什么漏 |
|---|---|
| `static_check.py` | 只校验 `module.attribute` 形式（`name.attr` 的 `name` 有没有 import），**裸函数名不在范围内** |
| `regressions.py` 的 smoke 契约 | 断言写的是 `isinstance(duplicates_removed, int)` —— `isinstance(0, int)` 也是 True |

### 修法与新增防线

1. `drawing.py` 模块级 `_call_method = call_method`（并在 import 列表里加 `call_method`）。
2. `auto_dimension` 补 `dedupe_failed_count`（原来只有截断到 10 条的 `dedupe_failed` 列表，
   "删 0 失败 23"和"干净的空操作"无法区分）。
3. **新回归断言 `assert_no_undefined_bare_calls`**：遍历 `src/` 全部 21 个模块，收集每个
   `ast.Call` 的裸函数名，要求它能解析到内置函数 / import / 定义 / 参数 / 赋值 / `global`。
   这是**整类 bug 的通用防线**，不再依赖"记得给某个函数加断言"。
4. smoke 的断言从 `isinstance(...)` 改成「`removed == 0` 且 `failed > 0` 即失败」，
   把那次的签名直接写成判据。

### 同类第二次：定义被整段删掉

同一份 v0.8.24 补丁在插入两个新函数时，把 `_EDGE_CACHE = {}` / `EDGE_CACHE_LIMIT = 32`
当上下文行一起删了，`list_drawing_edges` 立刻报 `_EDGE_CACHE is not defined`，
整条边坐标读取失效。所以通用检查必须覆盖 **`ast.Name`（Load）**，不能只看 `Call`——
变量引用不会出现在 `Call` 里，定义被删时也一样静默到运行期才炸。

### 一条通用规则

**任何 `except` 吞掉的分支，都要有一个"结果不可能为 0"的断言。**
失败的删除和没有可删的对象，返回值必须能区分——否则静默失败会伪装成正常空操作。

## 二十二、尺寸摆位：~10 mm 是绝对值，不随零件大小变（0.8.27）

`InsertModelAnnotations3` 把每条尺寸放在离几何约 **10 mm** 的地方——这是**绝对距离**，
与零件尺寸无关。Ø46 的圆盘上塞 25 条尺寸，全部落在轮廓内互相压字；换成 4:1 只是把
拥挤稀释掉，改的是图纸对尺寸的表达，不是排版。

`auto_dimension(place_outside=True)` 的做法：

1. 只取**本次调用插入**的尺寸（`before_names` 快照之后出现的），手放的永不移动；
2. 按 `precise_outline` 判断属于哪个视图（在框内即归属，否则取中心最近的）；
3. 取**最近的一条边**作为方向（left / right / top / bottom）；
4. 同一侧按另一轴排序后依次外扩：距离 = `gap + slot × step`，
   所以同侧两条尺寸**不会共用一个距离**；
5. 结束时 `EditRebuild3` + 落盘——`auto_dimension` 其余分支都不保存，
   这个分支必须自己存，否则图纸上什么都没变。

气缸盖实测（25 条尺寸）：

| 指标 | 默认摆位 | `place_outside=True` |
|---|---|---|
| 落在视图框内 | 21 / 25 | **0 / 25** |
| 文本重叠对 | 17 对 | **6 对** |
| 四侧分布 | — | 上 8 / 下 6 / 左 7 / 右 4 |

剩下 6 对重叠说明分层只解决了"出框"，不解决"同侧相邻两条太近"——
需要在外面再跑一次 `layout_dimensions` 收尾。

## 二十三、标题栏一字段多格，视图标签藏在 `<VLLABEL>` 里（0.8.28）

### 同一个 marker 会命中多个单元格

GB 图框在**框外**还有一条 180° 倒置的「图样代号」（在 `[0.053, 0.285]`，图纸左上角），
标题栏里的正主在 `[0.391, 0.022]`。原实现 `marker in text` 就 `planned.append`，
于是两处都写——图上多出一个倒着的代号。

修法：收集候选，按 **(是否倒置, 距 `TITLE_SLOTS` 的距离)** 排序取第一个，
其余写进 `skipped_extra_matches` 供核对。

### 空单元格没有 note，位置兜底也找不到

`_note_at(sheet, slot)` 只能找"已经存在的 note"。GB 模板那几格是空表格（只有线），
根本没有 note 对象，于是 `planned: []`。第三级兜底改为 **`InsertNote` 在槽位新建**，
注意位置必须 `call_method(ann, "SetPosition2", ...)`——属性派发会抛 MEMBERNOTFOUND。

### 视图标签的 `GetText` 是 `<VLLABEL>/<VLSCALEV>`

v0.8.24 曾判定它"复现不出来"，因为当时只看 `list_drawing_annotations`（那条路径没返回它）。
直接读 `IAnnotation.GetSpecificAnnotation.GetText` 就能拿到。渲染后是「A—A」叠「2.5 : 1」，
所以**估算宽度必须按可见文本**：19 个码字符直接乘字宽会得到 40 mm 的禁区，
把无关尺寸推得很远。

另外：**标签 note 在改比例后才出现在 `view.GetAnnotations` 里**——所以避让必须挂在
`set_view` 改比例之后，而不是等 `layout_dimensions` 事后处理。
实测：停在标签 `[0.33, 0.6371]` 的尺寸被移到 `[0.33, 0.6497]`。

## 二十四、剖视对齐与切割线：一个能解除，一个只能告警（0.8.29）

### 解除对齐有正经 API

`IView.RemoveAlignment()` 就是 UI 的「解除对齐」，必须在写 `Position` **之前**调用——
顺序反了 SolidWorks 会把坐标改回去。文档明确：它只清当前视图的对齐，不影响子视图
继承来的对齐。

实测（垂直剖）：请求 `y = 父视图 y + 0.05`，不解除 → 回 `0.2793`（= 父视图 y）、
`aligned_to_parent: true`；`break_alignment=True` → 落到 `0.3293`。

### 切割线跟随：`IDrSection` 可读，写入方式未探明

`IView.GetSection` **只在剖视图上**返回 `IDrSection`（父视图返回 None）：
- `GetLineInfo` 可用，返回 12 个 double（切割线两端在模型坐标的点）
- `GetLabel` → 'A'、`GetView` → 父视图
- **`SetLineInfo` 存在但所有尝试都失败**：直接调用返回 False，
  按 6/7/8/10/12 个参数传都报「无效的参数数目」——多半需要 VARIANT 数组或
  额外的 line-count 参数，本轮没探明

所以 v0.8.29 仍然只是「移动父视图时告警」（`derived_views` + `cut_lines_detached_warning`，
0.8.26 已做），没有真正让线跟随。要真修，下一步得先确定 `SetLineInfo` 的签名，
或者改走「重建剖面」路线（代价是该视图上的尺寸会丢）。

## 二十五、驱动尺寸不能"补插到第二个视图"，但能在那重新标（0.8.30）

### 参数改不出"重复插入"

`InsertModelAnnotations3(source, types, allViews, duplicateDims, hidden, ...)` 的第 4 个参数
看着像"允许插重复"，实际**不是决定因素**——这个调用是**文档级幂等**的：同一模型尺寸
一旦插入过，第二次调用直接返回 `None`。

实测（履带轮 P05，两视图）：

| 组合 | 返回 | 视图1 | 视图2 |
|---|---|---|---|
| `all_views=True` 首次 | 12 | 12 | 0 |
| 激活视图2 + `all_views=False` + `duplicateDims=True` | **None** | 12 | 0 |
| 激活视图2 + `all_views=False` + `duplicateDims=False` | **None** | 12 | 0 |
| `all_views=True` + `duplicateDims=True` | **None** | 12 | 0 |

所以「局部放大视图永远 inserted: 0」不是 bug，是 API 的语义。

### 能做的：在同一几何上重新标一条

`add_drawing_dimension` 引用的是**视图里的边**，不是"某条模型驱动尺寸的副本"，
所以它在第二个视图里能正常建：

```
list_drawing_edges(view_name="工程图视图2")        # 147 条边
add_drawing_dimension(view_name="工程图视图2", edge_index=0)
→ {"created": true, "value_m": 0.11, "selected": "edge_index"}
```

视图2 的尺寸数从 0 变成 1。数值必然与主视图一致（同一个模型几何），
代价是它是一条**新标注**（`FullName` 不同），不是原尺寸的副本——
所以 `reconcile_ledger` 会看到"图上有两条同值尺寸"，这属于正常。

结论写进 `check_capabilities` 的 `duplicate_dimension_to_view`（`unsupported`），
避免下一个使用者再花一轮去试参数。

## 二十六、剖面切割线：三个接口都够不着那张图上的线（0.8.31）

想让"移动父视图时切割线跟着走"，试完四组接口，都不行：

| 接口 | 结果 |
|---|---|
| `IDrSection.GetLineInfo` | 12 个 double，**模型坐标**（±1.03 m，而 A3 只有 420 mm）。移动视图前后**完全相同** |
| `IDrSection.SetLineInfo(VARIANT 数组)` | 能写，返回 `True`——但改的是**模型空间的剖切平面**，等于把剖面切到别的地方，不是移动图上那条线 |
| `IView.GetSectionLineInfo2` | 只读，且同样返回模型坐标（值 -1.0347 与 `GetLineInfo` 一致） |
| `IView.GetSketchCount` / `GetSketches` | 晚绑定下**不存在**（`DISP_E_UNKNOWNNAME`），触达不了图纸上的草图 |

`SetLineInfo` 的正确签名在文档里：**只接受一个参数**，是 VARIANT 数组，
每段 6 个 double（起点 xyz + 终点 xyz），多段的公共点必须保持一致。
之前按 6/7/8/10/12 个散参数传，全都报「无效的参数数目」——那是参数形式错，不是方法不存在。

### 顺带发现更基本的问题

渲染核对（移动前后各导一次 PDF）显示：`add_section_view` 建出来的切割线
**在图纸上的位置与父视图并不重合**——父视图在左上角，线飘在图纸中部。
也就是说「移动后脱离」之前，它就已经没压对地方。

原因是切割线用 `ISketchManager.CreateLine` 画在草图里，而坐标基准（图纸绝对 vs 视图局部）
在晚绑定下无法验证（草图对象拿不到）。修它需要重定坐标基准，属独立一轮。

### 现在的建议流程

1. **先把视图摆到位**（`place_views` / `set_view`），最后一步才 `add_section_view`
2. 已经移过 → `delete_view` 删剖面重建（代价：该视图上的尺寸会丢）
3. 每次改完 `export_drawing` + `render_drawing` 目视核对线是否压在父视图上

## 二十七、切割线画错位置：一个表达式里两个错（0.8.32）

`add_section_view` 原来这样算半长：

```python
half = (max(width, height) * 0.62) / scale     # width 来自 GetOutline
sm.CreateLine(offset, -half, 0.0, offset, half, 0.0)
```

两处都错：

### ① `width` 已经是图纸米，不该再除 scale

`GetOutline` 返回的是**图纸坐标**，`width = outline[2] - outline[0]` 因此已经是图纸米。
除 scale 后：`0.1669 × 0.62 / 0.1 = 1.0347 m` —— **A3 只有 420 mm**，线长 2 米多。

这个 1.0347 就是 `GetLineInfo` 报出的 ± 值，之前误判成"模型坐标"，
其实是**自己画进去的错误长度**。

### ② `CreateLine` 用绝对坐标，没加视图位置

草图在**图纸层**，坐标是图纸绝对坐标。写成 `(offset, ±half)` 就是 `x = 0`
（图纸左边缘），只有线中段恰好扫过视图，看起来像"线在视图附近但错位"。

### 修法

```python
precise_box = precise_outline(target) or outline
half = max(precise_box[2] - precise_box[0], precise_box[3] - precise_box[1]) * 0.62
...
sm.CreateLine(pos[0] + offset, pos[1] - half, 0.0,
              pos[0] + offset, pos[1] + half, 0.0)      # 竖剖
```

用 `precise_outline` 而不是 `GetOutline`，顺带把 11.9 mm 虚胖排除掉（否则线再长 24 mm）。

实测（履带轮，A3，视图 scale 0.1）：

| 指标 | 修前 | 修后 |
|---|---|---|
| 半长 `half` | 1.0347 m | 0.1035 m |
| 线 x | 0（图纸边缘） | 0.3133（= 视图中心） |
| 线 y | -1.03 ~ +1.03（图纸外） | 0.1832 ~ 0.3754（视图 0.2596~0.299，上下各伸出 ~80 mm） |
| `sanity` | — | `ratio 1.0, verdict ok` |

**「移动父视图后跟随」仍不支持**：`SetLineInfo` 会把多段独立短线重排成一条折返线
（实测两段被连成往返），所以只能保证**初始位置正确**，移动后仍需重建。

## 二十八、公差单位 ×1000 与"读不到文字就删标注"（0.8.33）

### `SetToleranceValues` 收米，代码传了毫米

`add_dimension_tolerance(value_mm=50, tolerance_type="symmetric", upper=0.1)` 渲染成
**"50 ±100"**——`0.1` 被当成 **0.1 米**。而回执一切正常：`applied: true`、
`tolerance_type_readback: 4`。**数值对了、类型对了、只有图是错的。**

这类错误只有两条防线：知道 API 的单位契约，或者在返回值里给**渲染出来的文本**。
现在两条都有：传参 `/1000`，并新增 `rendered_text`（PDF 实测 `±0.10`）。

教训：`applied: true` 只证明"调用没抛异常"，不证明"图对了"。
凡是**写入数值**的工具，回执都该带一个从文档读回来的字符串。

### SW2026 上 `GetText` 对所有孔标注返回 None

v0.8.26 用「文本里有没有 Ø」作为孔标注的准入门槛。在 SW2026 上
`GetText(0/1/2)` 对每一条 callout 都返回 `None`，于是 `has_diameter=False` →
走 `not has_diameter` 分支 → `_discard()` 删除。

结果：全新图纸 8 个孔，`created: 0 / failed: 8 / no_native_callout`——
**能力从"标注不理想"退化成"标注不存在"**，而这恰好是 README 头部第一项能力。

修法不是"再加一层校验"，而是换判据：

```python
text_unreadable = not str(text or "").strip()
if radius_only:                                  # 文本明确以 R 开头才拒
    _discard(...); continue
if not has_diameter and not text_unreadable:     # 有文本但没 Ø 才拒
    _discard(...); continue
# 其余一律保留，created 行标 text_unreadable=True
```

**原则：缺证据 ≠ 反证据。** 读不到就打 `text_unreadable` 让调用方自己决定，
不要替用户删掉已经建出来的东西。

### 顺带：`SystemValue` 对 callout 不统一

同一次调用里，印着 `Ø8` 的 callout `SystemValue` 是 **4.0（半径）**，
印着 `Ø15` 的却是 **15.0（已经是直径）**。所以 `list_drawing_annotations` 现在
**以文本里的数字为准**，取不到才回退 `SystemValue` 并标 `diameter_source`。

## 二十九、`_view_axis_map` 漏乘比例：一条 bug 卡住三个工具（0.8.34）

```python
scored.append((abs(extent[a] - width) + abs(extent[b] - height), (a, b)))
```

`extent` 来自 `GetPartBox`，是**模型米**；`width/height` 来自 `precise_outline`，是**图纸米**。
没乘视图比例，所以只要视图不是 1:1 就匹配不上，直接返回 `axes_mismatch`。

连锁后果：

| 工具 | 症状 |
|---|---|
| `list_circular_edges` | 全长返回 `x: null / position_unavailable`（实测门板 0/9，修复后 9/9） |
| `add_section_view(cut_plane_origin=…)` | 无法把模型坐标映射进视图，返回 `cut_plane_unmapped` |
| `add_hole_callout`（单点） | 没有可匹配的圆，一切坐标都 `edge_not_found` |

一行 `extent * view_scale` 三个都通。**教训**：跨坐标系的比较，先问一句"这两边单位一样吗"。

### 单点孔标注：匹配修好了，调用还没通

改用「枚举圆 + 投影取最近」后：匹配距离 **0.0 m**、`Select2` 后选择计数 **1**，
但 `AddHoleCallout2` 仍返回 `None`。批量版在同一个视图能成功，差异未定位。

已把证据写进错误体（`matched_circle` + `selected_before_call`），下轮不必重跑盲猜。

另外两个已经踩过的坑，记在这里省得再踩：
- `_view_axis_map` 读零件包围盒会 `open_document(model)`，**把活动文档切成模型**，
  之前取的 `view` / `edge` 全部失效——`Select2` 在死句柄上会"成功"，`AddHoleCallout2` 随即返回 None；
- 孔有**两面同心圆**，只有近面可标，批量版按 `z` 最小取，单点版也照此。

## 三十、单点孔标注：匹配对了就通；`Ø9→Θ99` 是文字槽不是数值（0.8.36）

### 单点 `add_hole_callout` 为什么之前必失败

三个原因叠在一起：

1. **`_view_axis_map` 漏乘视图比例**（0.8.34 修）→ 拿不到圆心的图纸坐标；
2. **按坐标射线选边命不中**（v0.8.23 就发现过）→ 改成枚举圆 + 投影取最近；
3. **按直径匹配会选错孔**——同径多孔时取到的是别的孔，于是「选边成功、`AddHoleCallout2` 返回 None」。

改用**精确模型圆心**（`CircleParams[0..2]`）匹配后：门板 **Ø8×2 + Ø15×3 = 5/5 成功**。

### 那 4 个 Ø6 圆本来就标不出来

最初验证时取 `rows[:3]`，正好是 4 个 Ø6 —— **批量版对它们也是 0 产出**（非孔或背面）。
所以那不是 bug。错误提示已改成「这个圆可能不是可标注孔（外圆弧或背面）」，而不是
「找不到边」——后者会让人一直重试坐标。

### `Ø9 → Θ99`：文字槽冲突

渲染图实测：Ø8 正常（`Ø8`），**Ø15 印成 `Ø1515`**。`GetText` 回读始终是 `Ø15`，说明值没错。

原因是 `AddHoleCallout2` 的标注文字分槽：**直径一个槽、深度另一个槽**。
`SetText(slot, "Ø15")` 只替换直径槽，深度（15）仍在另一槽 → 拼成 `Ø1515`。
反馈里的 `Ø9 → Θ99` 是同一件事（`Ø` 在 PDF 里被提取成 `Θ`，纯字形映射）。

三条路都试过：

| 做法 | 结果 |
|---|---|
| 只写直径 | 深度粘连（`Ø1515`） |
| 写直径后清空其余槽 | **标注整条消失** |
| 完全不写 | 印出字面量 `<MOD-DIAM>` |

所以这不是「一行能修」的问题，登记为 `hole_callout_text_slots`（`partial`）：
通孔正常，带深度的孔会粘连，workaround 是改用 `add_drawing_dimension` 或人工改一次文字。
回执里 `text_source=SetText_depth_may_trail` 就是这条限制的标记。

## 三十一、假脏标记、假尺寸、真空隙（0.8.37）

### 工程图操作会给模型打 `NeedsRebuild2`

删一条标注，模型没动，`model.Extension.NeedsRebuild2` 却变成 `True`。
`delivery_check` 把它记成 `needs_rebuild`，verdict 从「待设计审核」掉到「仅供参考/未完成」——
一个调用方在这个工具集里**永远清不掉**的阻塞项。

修法：读到脏就**先强制重建一次再读**。真改动重建后仍在，工程图引起的会消失。
实测：删标注后 `needs_rebuild` 消失，剩下的 `suppressed_features` 是门板真有的压缩特征。

### 错的边也会给出"合理"的尺寸

`AddDimension2` 只要求"当前有选中的实体"，不校验**选中的是不是你以为的那条**。
反馈里 `edge_index=71`（Ø36 分度圆弧段）量出了 Ø46 外圆的 `R23` —— 一个看起来完全正常的错误值。

便宜的判据是**关联实体**：建完读 `IDisplayDimension.GetAnnotation` 的
`GetAttachedEntityTypes`，为空说明这次选择根本没落上，撤销并报 `edge_mismatch`。
比"比对数值"便宜，也不会被"巧合相等"骗过。

### 去重保留"首次出现"，剖视图因此永远是空的

`eliminate_duplicates` 按模型 `FullName` 保留第一条，而插入顺序是**主视图在前**，
所以两个剖视图上的副本全被删掉（实测 83 插入 → 删 44 → 剖视 0 尺寸）。
而深度、内腔这类尺寸**只有剖视能承载**，`ledger_missing` 随即报 7 条缺料。

现在的 advisory 把这件事写清楚了，并给出两条出路：
`eliminate_duplicates=False`（全留，自己删），或 `dimension_filter`（精确保留）。

## 三十二、仓库卫生：四轮挂账一次清（0.8.38）

### `.gitignore` 只挡 CAD 扩展名，挡不住 zip/png/jpg/csv

规则里列了 `*.SLDPRT` / `*.STEP` 等，于是 `测试用例/` 下的 **zip、PNG、JPG、CSV** 一路绿灯，
7 个文件 4.1 MB 进了公开仓库——而 README 写着"仓库不含测试模型"。

改成**整树排除** `测试用例/`，再用 `git rm --cached -r` 移出索引。
**不改写历史**（`filter-repo` 要 force push），本地文件一个没动。

### smoke 缺模型时只说"missing"

clone 下来跑 `python tests/smoke.py` 得到一行 `missing test model: ...` 和 `exit 2`，
没有任何下一步。现在会列出所需的三样东西（零件 / 装配体 / 钣金件）并给出
`--part` / `--assembly` 的用法示例——仓库本来就不带模型，能指到本机模型就够了。

### `call_method` 有两份

`bom_native.py` 自己写了一份，`sw_session.py` 一份。四轮反馈后才统一——
代价是**重试与错误分类逻辑只长在 `sw_session` 那份上**，`bom_native` 的那条路径
永远拿不到桥接自愈。现在只留一处。

### `health_check` 不说自己在跑哪个包

多份 editable 安装并存时，"MCP 连的是哪个版本"是唯一重要的信息。补
`package_path` / `package_version` / `python_executable`。

### `static_check` 只看 `name.attribute`

裸名字（模块级常量、缓存）不在它的视野里。`_EDGE_CACHE` 被一次合并吞掉后，
模块仍然 import 成功，只有跑那条路径才会炸——**秒级防线当时给的是绿灯**。

现在 `check_bare_names()` 遍历每个模块的顶层函数，把 `ast.Name`（Load）逐个对
`imports ∪ 模块级定义 ∪ 参数 ∪ 局部 ∪ 内建 ∪ dunder` 求解。第一版漏了**嵌套函数的参数**，
99 个误报；补上 `ast.arg` 与 `global` 后归零。

## 三十三、可标注性可以算出来，不用试出来（0.8.39）

反馈：外形视图 13 条圆边，`add_hole_callout` 3/3 全拒；端视图 2/10、剖视 4/9 可标。
规律很明显，但**清单里没有任何可标注性信号**，调用方只能逐条试。

### 判据是几何的

一个圆能不能打孔标注，取决于**它的法向是否沿视图的观察方向**：

```python
axis_vec = CircleParams[3:6]                    # 圆的法向
depth_axis = 3 - axes[0] - axes[1]              # 视图显示两个轴，剩下那个就是观察方向
dominant = max(range(3), key=lambda i: abs(axis_vec[i]))
faces_viewer = dominant == depth_axis and abs(axis_vec[depth_axis]) >= 0.9
```

`axes` 来自既有的 `_view_axis_map`，所以**零额外 API 成本**，不碰 COM。

### 实测完全对齐

门板 `door.SLDPRT`：

| 圆 | `facing` | `annotatable_hint` | 实际 |
|---|---|---|---|
| Ø6 × 4 | `side` | `false` | 单点与批量**都标不出** |
| Ø8 × 2 | `front` | `true` | 5/5 成功 |
| Ø15 × 3 | `front` | `true` | 5/5 成功 |

9 条圆边，预判 9 条全对。调用方现在可以在清单阶段直接过滤。

### 顺带：`IView.Type` 的 7 是标准模型视图

初版 `_view_kind` 按文档猜了 `{1: sheet, 2: section, 3: detail, 4: projected}`，
结果每条都显示 `type_7`——测下来**图纸是 1、标准模型视图是 7**。补上后正常。

猜枚举值之前先打一次日志，比事后看 `type_7` 猜半天快。

## 三十四、孤儿尺寸：`GetAttachedEntities2` 为空不等于没引线（0.8.40）

`SetPosition` 移过的尺寸，`GetAttachedEntities2` 返回空——被记成 orphan。
`prune_orphan_dimensions` 的 caveat 早就写明"这些其实还有引线，照单全删会缺料"，
但 `delivery_check` 照旧把它们计为 blocking，verdict 永远到不了「可制造」。

**同一张图，两个工具给出相反的处理指引**，用户无所适从。这是第三次报告。

### 用几何做仲裁

```python
gap_x = max(box[0] - position[0], 0.0, position[0] - box[2])
gap_y = max(box[1] - position[1], 0.0, position[1] - box[3])
near_leader = ((gap_x ** 2 + gap_y ** 2) ** 0.5) <= 0.05     # 50 mm
```

活着尺寸离它标注的视图不远（`layout_dimensions` 就停放在轮廓外侧），
真死的会飘得很远。50 mm 是经验阈值：够容纳 `outside_gap + slot × step` 的堆叠，
又远小于跨图纸的漂移。

实测门板：5 个 orphan **全部** `orphan_with_leader: true`，
`dimension_attachments ok:true`、`blockers: []`、verdict 回到「待设计审核」。

回执里会写明"另有 5 条移动过的尺寸仍有引线，不计入"——**豁免要说出来**，
否则下次没人知道为什么 5 个 orphan 没扣分。

## 三十五、一次"修好了"的自我推翻：50 mm 不等于有引线（0.8.41）

0.8.40 我加了「orphan 到视图 outline 距离 ≤50 mm 就算还有引线，不计入 blocker」。
理由看似充分：`prune_orphan_dimensions` 的 caveat 说这些尺寸其实还有引线。

**错在哪**：从 v0.8.27 起，尺寸**默认就被摆到视图外侧**（"place dimensions outside
their view"）。也就是说，一张排得越整齐的图，每个尺寸离 outline 都越近——
这个判据实际衡量的是「**尺寸放在哪**」，而不是「**尺寸有没有挂到几何**」。

结果在履带轮那张图上：**15/15 条真孤儿全部被豁免**，
`dimension_attachments` 从 fail 翻成 ok，而同一个返回体里还写着
「23 个尺寸均已关联」——实际是 8 条关联 + 15 条被豁免。

### 回退成诚实的计数

```python
orphan_near_view = ...          # 只作观测，不再叫 "with_leader"
orphan = len(all_orphans)       # 全部计入
suffix = "（其中 %d 条位置在视图附近，但引线未验证）" % near_view
```

`checks` 里同时给 `attached_count` 和 `excused_count(=0)`，让"N 个已关联"不再
和"另有 5 条不计入"打架。

实测门板：`ok: false`、`detail: 5/17 个尺寸无关联实体（其中 5 条位置在视图附近，但引线未验证）`、
`attached_count: 12`。

### 教训

**判据必须回答它声称回答的问题。**「位置在哪」和「挂没挂上几何」是两件事，
用一个便宜的相关量去替代，就会把失败的判定变成通过——这恰好是项目第一条原则
（"工具返回成功 ≠ 图纸正确"）要防的事。

真正的判据在 `IAnnotation.GetLeader` 那一侧（引线指向哪个实体），下一轮探。
探不到就老实标 `unknown`，**不要当成"有"**。

## 三十六、服务被一次 COM 调用带走（0.8.42）

反馈现场：调 `add_section_view` 返回 `Not connected`，随后所有工具同样，`Win32_Process`
里**服务进程已不存在**，`logs/mcp-*.log` 停在那一刻，**没有 traceback、没有 error payload**。

**Python 的异常做不到这件事**——它会被 FastMCP 包成 error payload。进程无声消失只有一种解释：
**原生代码（COM/pywin32）里发生了 fault**。

### 能做的第一件事：让下次有证据

```python
faulthandler.enable(file=open("logs/crash.log", "a", buffering=1), all_threads=True)
```

`all_threads=True` 是必须的——COM 调用跑在 STA 工作线程上，只看主线程等于不看。
写文件而不是 stderr，因为 MCP 走 stdio，stderr 常常没人接。

这不是修复，是**把"静默消失"变成"可诊断"**。真正的定位需要下一次复现时的 C 级调用栈。

### 附带：`dimension_filter` 的全名陷阱

`plan_drawing` 返回 `{"feature": "D1@草图19", "value_mm": 42}`，而 filter 里写的是
`parts = full.split("@"); feature = parts[1]` → `"草图19"`，拿它去比 `"D1@草图19"` 永远不中。

于是**静默退化**到按 `value_mm` 匹配：计划只留 1 条 Ø42，实际 3 条全留，
调用方以为"按特征精确裁剪"，其实是"按数值粗筛"。

现在的规则：**给了全名就只按全名**（`full.startswith(prefix)`），不退化到数值。
"精确指定"和"模糊兜底"不能同时生效——两者混在一起时，永远是模糊的那个赢。

## 三十七、比例选择：序列缺一档 + 严格判据 + 回执记错（0.8.43）

反馈：A3 上 210 mm 零件被排成 **1:5**，三视图挤在左上角，图纸 90% 空白，
`auto_scale_adjustments: []`——**没有任何解释**。

三个问题叠在一起：

### ① 标准序列缺 1:2.5

`(1.0, 0.5, 0.2, 0.1, ...)` 里没有 0.4。于是 1:2 与 1:5 之间的一切都掉到 1:5。
GB/T 14690 的首选系列本来就有 1:2.5。

### ② 判据太严：3.4% 的超出掉一整档

```python
if candidate <= fit + 1e-9:     # 严格
```
实测 `raw_fit = 0.1934`，对候选 0.2 只差 **3.4%**。而 `_fit_scale` 里 `gap`
**算了两遍**（宽、高各加一次），这点超出轻松吸收。改成 `candidate <= fit * 1.05`。

### ③ 回执记的是降档前的值

`_layout(chosen_scale)` 会做**预测性降档**：0.2 溢出 → 降到 0.1。
但 `scale_reason` 在降档**之前**就填好了，于是回执说「1:5」，实际比例是 1:10——
**自己和自己矛盾**。

现在 `scale_reason` 记全链路：

```json
{"raw_fit": 0.1934, "standard_scale": 0.2, "ratio": "1:5",
 "final_scale": 0.1, "final_ratio": "1:10", "stepped_down": true}
```

**教训**：回执里的每个数都要在**所有可能改动它之后**再取。中途快照会在
"后面还有一道降档"时变成假话。

### 还没解决

`_layout` 的溢出预测本身偏保守（1:5 实际放得下，它判溢出）。这属于尺寸布局那一轮
（`auto_dimension` 的 P1）一起处理——单改这一处，没有布局算法配合还是会反复。

## 三十八、外推分层：三个"看起来对"的细节全错（0.8.44）

反馈：16 条尺寸外推，**10 条挤在下方**，最远落到 y≈0.03，压进标题栏。

### ① 堆叠没有上限

```python
offset = gap_m + slot * step_m      # 无上限
```
16 条 → `0.012 + 15×0.008 = 0.132`。主视图 y≈0.15，减去 0.132 → **0.018**，
而 A3 安全区下限是 0.065。

### ② 读不到图纸框，限额等于没加

第一版加了容量限制，但用 `doc.GetSheets` 取图纸 outline——**在 SW2024 上返回不了可用框**，
`safe` 一直是 None，`_room()` 兜底返回 0.20 m，容量算成 24 条，等于没限。

改用 **Type 1 的图纸视图**（`doc.GetFirstView.GetOutline`），这个在本文件别处已对过
420×297 mm 实测。

### ③ `max(room, gap_m)` 兜底又把尺寸推出去

```python
offset = min(gap_m + slot * eff_step, max(room, gap_m))    # 错
```
视图顶部已经贴着安全区（`room = 0`）时，兜底给出 `gap_m = 0.012`，
于是尺寸被推到 `box[3] + 0.012 = 0.305`，**比安全区还高 18 mm**。
正确写法是硬上限：`min(..., max(room, 0.0))`——宁可贴着轮廓，也不能出界。

### 结果

门板实测：最坏 y **0.337 → 0.293**（= 视图自身顶边），四侧分布
`left 5 / top 1 / right 7 / bottom 4`，回执写明哪些条被换去了对面。

**残留 6 mm 超出不是这条的锅**——视图顶部本身就在 0.293，安全区是 0.287。
那是 `_layout` 把视图放太靠边，与 P5 的比例判断同一处代码。

### 教训

三个细节每一个单看都"合理"：无上限是"简单"，`GetSheets` 是"按文档写的"，
`max(room, gap)` 是"防止贴太近"。**但它们凑在一起，限额一次都没生效。**
加约束时，要验证约束**真的生效了**——不是写完代码就算。

## 三十九、布局预测的两个错，以及一个还没找到的第三方（0.8.45）

视图贴着图纸边（顶 0.293，安全区 0.287），两个预测层面的错：

### ① 拿模型尺寸和图纸尺寸直接比

`_fit_scale` / `_layout` 用 `box`（`GetPartBox`，模型米）去算布局，但真实视图轮廓
每条边都有恒定 **11.9 mm** 的虚胖（`OUTLINE_MARGIN_M`，P2-13 实测）。
于是预测每向少 24 mm——**图越规整，贴边越明显**。

修：两边都加 `inflate = 2 * OUTLINE_MARGIN_M`。

### ② 网格恒按 2×2 算

```python
total_w = ww + gap + dd      # 永远是两列
total_h = hh + gap + dd      # 永远是两行
```
只请求 1 个视图时，它被放进**两行框的顶行**，于是顶部天然贴边。

修：网格跟随 `views` 的数量——1 个用单格，2 个用单列，3 个以上才用 2×2。

### ③ 但视图位置还是没变

改完实测仍是 `0.1855..0.2930`。`positions` 确实取自 `_layout` 的 `c1/fy`：

```python
positions = {"front": (col1, front_y), ...}
CreateDrawViewFromModelView3(model, view_name, x, y, 0)
```

所以**还有第三方在覆盖它**——候选是 `CreateDrawViewFromModelView3` 自己按模板
重排，或者后面某一步动了视图。**没定位，不猜。**

**这条教训**：改完预测要**回读实际值**再收工。`fits: false` 和实际 outline 摆在一起时，
我第一版还以为改对了。

## 四十、视图为什么从不落在预测位置（0.8.46）

两轮"修预测"没有带来任何可测变化，因为**视图根本没被移动过**。

### 三个实验，三个事实

| 实验 | 结果 |
|---|---|
| `set_view(0.20, 0.15)` 移动一个**已存在**的视图 | ✅ 成功，outline `0.1855..0.2930 → 0.0963..0.2037` |
| `CreateDrawViewFromModelView3(model, name, x, y, 0)` 传 `x/y` | ❌ **被忽略**，视图落在模板给的位置 |
| 在**刚建出**的视图上写 `Position` | ❌ 请求 `(0.2374, 0.179)`，实际 `(0.0, 0.2374)` |

第三条正是 v0.8.9 记录过的 marshalling 陷阱——**新建视图上它又回来了一次**，
和"已存在视图"上的行为不同。

### 所以"预测"和"落地"之间有一条断链

`_layout` 算出 `c1/fy` → `positions["front"]` → `CreateDrawViewFromModelView3` **不理**。
链路前面再准，末端没接上。

### 为什么本轮不修

`set_view` 会 `_open_drawing`（重开文档），使建图循环里正在填的 `view_objects` 全部失效。
正确的做法是**把定位搬到循环之后**，重新枚举视图、按名字对应位置再移一遍——
结构改动，不是一行。

回执已如实写明：

```json
"placement_deltas": [{"view": "front", "placed": false,
                      "requested": [0.2374, 0.179],
                      "reason": "pending_post_create_placement"}]
```

### 顺带：裸名字防线第一次实战

我在这段里误用了 `layout._prop_put`（`drawing.py` 没有这个名字），
`static_check` 的 `check_bare_names`（0.8.38 加的）当场拦下：
`undefined bare calls: tools\drawing.py: ['_prop_put']`。

**如果不拦，它会变成 no-op**——`except` 一包，位置照旧不动，
和这两轮的"修了但没变化"一模一样。

## 四十一、视图贴边：两套边距口径打架（0.8.47）

0.8.46 我把根因归给 `CreateDrawViewFromModelView3` 忽略 x/y——**只对了一半**。
它后面还有 `_place_views`，用**真实 outline** 重排，那才是视图的实际位置。

真正的问题是 `_place_views` 的两处口径都太松：

### ① 起排点正好压在边界上

```python
y = sheet_h - top - pad / 2      # top 来自 template_safe_area
```
`template_safe_area` 对这个 GB A3 模板只给 **`top = 4 mm`**，而尺寸那边（`_push_dimensions_outside`）
用的是 **10 mm inset**。**两个模块对「可打印区从哪开始」意见不一**，
于是视图顶边落在 0.293，而尺寸那套认为安全线是 0.287。

### ② SW 的 outline 比墨迹大

`precise_outline` 已经内缩了 11.9 mm 的虚胖，但边界仍然紧贴。

### 修法

```python
inset_floor = 0.010                 # 四边统一取严格值
left = max(left, inset_floor)  ...
frame_pad = 0.004                   # 起排再让 4 mm
y = sheet_h - top - frame_pad - pad / 2
```

实测：1 视图 `y 0.1755..0.2830`，3 视图 `0.1750..0.2830`，全部在 0.2875 以内。

### 顺手删掉一条误报

0.8.46 在创建循环里记了 `pending_post_create_placement`，
可 `_place_views` 的重排**一直是好的**——那是一条给正常路径挂的警报。
现在的注释只说明事实，不再报"未放置"。

### 教训

**同一个概念在两处有不同定义时，先统一再修。** 这里"可打印区"有 4 mm 和 10 mm 两个版本，
两边各自都对，合起来就是视图压线。

## 四十二、按值排序：失败一次之后才做对（0.8.48）

### 为什么位置排序不行

3 条 Ø42 可以相距 **0.4 mm**。按位置排序时它们必然挤在同一槽位，
`gap + slot*step` 算出来的距离也一样——**排序键选错了，重排等于没重排**。

### 方向也踩了一次

`slot 0` 离轮廓最近，所以：
- **降序**（大值先）→ 大值贴视图、小值跑图框边 —— 反了；
- **升序** → 大值落在最外层，符合制图习惯。

实测门板左侧：Ø4 `x=0.0468`（内）… Ø215 `x=0.0148`（外）。

### 第一次改动把外推整个打没了

改 5 元组 + 换排序键，结果 `placed_outside` 从 17 变成 **`None`**，
`outside_sides` 变 null，尺寸全留在视图内。

**查了三个点都是对的**：`_room(items[0][3])` 仍取 `owner_box`、
`dim_value` 有 try/except 兜底、解包 5 元对齐。查不出原因 → **回退，不发布**。

### 第二轮：先装仪表，再动刀

在 `_push_dimensions_outside` 里加中间量：

```python
debug = {"views": ..., "annotations": ..., "skip_before": ...,
         "skip_no_position": ..., "skip_no_owner": ..., "buckets": ..., "per_side": {...}}
```

先把**未改动**的版本跑一遍，确认 `buckets: 17`、四侧 `5/1/7/4` 是基线；
再改同一处，`placed_outside` 立刻是 **17**，一次通过。

**同样的改动，第一次失败第二次成功，差别只在"有没有中间量"。**

### 教训

**改动前先装仪表。** 回执只给最终值时，一处静默断裂就等于黑盒——
这时回退是对的，但**回退之后要补的是可观测性，不是再猜一遍**。

## 四十三、回读通过 ≠ 图面可见（0.8.49）

### GT 符号格：字符串落进去了，但没画出来

`add_geometric_tolerance(symbol="cylindricity")` 返回
`symbol_written: "圆柱度"`、`symbol_is_text: true`、`ok: true`，
`GetFrameSymbols2` 回读也一致——**然后导出的 PDF 里符号格是空的**。

`<MOD-*>` 格式码在本环境不被解析（v0.8.20 已知），退化成写中文文字；
而 SolidWorks 的**符号格只认格式码**，写字面文字会被原样存回、却不在图面显示。
**回读校验把一次无效写入判成了成功。**

现在的成功路径带 `symbol_render_unverified: true`，note 里写明两条出路：
`symbol='none'` 把符号名并入 `tolerance`（`tolerance='圆柱度 0.02'`），或界面手补。

**这不是"修好了"，是"不再骗人"。** 真修需要渲染前后 diff，成本远高于收益。

### 中心标记不该打在立体图上

`add_center_marks` 原来无差别跑所有视图，反馈在**等轴测图上看到 15 个十字标记**。
立体图上的中心标记没有制图含义。现在可以传 `view_name` 限定。

### 一条通用的

**回读能证明的只是"数据进去了"，不是"用户能看到"。**
凡是把东西写进**渲染后才可见**的容器（符号格、字体格、模板占位符），
回读一致就应该配一句"渲染未验证"。

## 四十四、只开不关：`close_document`（0.8.50）

工具集里每个工具都 `_open_drawing`，**没有一个会关**。

后果在反馈里很具体：`out/` 里两个作废草稿删不掉——`Device or resource busy`，
因为 SolidWorks 还开着它们；重启 SW 才能删。

### 实现要点

```python
want = os.path.normcase(os.path.abspath(str(drawing_path)))
for document in (software.GetDocuments or ()):
    if os.path.normcase(document.GetPathName or "") != want:
        continue
    software.CloseDoc(str(document.GetTitle))
```

两个细节：

1. **`normcase` 两边都做**：Windows 路径大小写不敏感，只规范化一边就永远匹配不上。
2. **只关路径完全一致的那一个**。全局 `CloseAllDocuments` 会把用户正在编辑的东西一起关掉，
   那是不可接受的副作用。

未打开时返回 `closed_count: 0` 的干净空操作——smoke 就靠这点验证「不误关」：
传一个不存在的路径，断言 `ok: true` 且 `closed_count: 0`。

### 还没做的

`add_model_view` / `move_view` / `set_view_scale` 仍在账上。它们比 `close_document` 重，
而且 `move_view` 会撞上 0.8.46 记录的那个「新建视图写 Position 变成 (0, x)」的坑。

## 四十五、视图能改比例、能挪位了（0.8.51）

反馈为改一个视图比例**重建整张图三次**：`create_drawing` 定下比例后就再没有入口。

### `set_view_scale`：直接赋值就行

```python
target.ScaleDecimal = value      # 可以
```

与 `Position` **不同**——后者必须 `DISPATCH_PROPERTYPUT` + VARIANT 数组，
普通赋值会变成 `(0, x)`。同一个 `IView` 上两种属性、两种写法，别混。

回执给 `scale_before` / `scale_after` / `applied`：**写完要回读**，
否则被锁定的图纸会以"成功"的样子返回。

### `move_view`：复用已验证的写入器

```python
from . import layout as _layout
return _layout.set_view(drawing_path, view_name, x_m=..., y_m=...)
```

`layout.set_view` 内部会**重开文档**再写 `Position`，这是唯一能落地的路径
（0.8.46 实测：新建视图上普通赋值 / 刚建出时 PROPERTYPUT 都落在 `(0, x)`）。
这里不做第二套实现——**已验证的写入器只该有一份**。

### 工具数 57 → 59，`static_check` 三道都要过

1. `README section headers`：`### 二、工程图（30 → 32）`
2. `README never mentions`：工具表加两行
3. `smoke covers every tool`：每个新工具都要被 smoke 调用

smoke 里的图纸要**自己建**（顶层的 `drw` 是各函数内的局部变量，跨函数看不到）——
用 `part_copy` + `create_drawing` 造一张临时图，两个工具各测一次。

## 四十六、剖切线按长边算，非方视图必然溢出（0.8.54）

```python
half = max(view_w, view_h) * 0.62      # 一律按长边
```

一个 116.9 × 114.3 mm 的视图，半长算出 72.5 mm，而视图半高只有 57 mm——
**上下各溢出 ~15 mm**，下方箭头正好落在技术要求第 2 行上，只能把技术要求整体右移 98 mm。

修：

```python
half = min(max(view_w, view_h) * cut_line_ratio,   # 0.55
           min(view_w, view_h) * 0.5)              # 短边兜底
```

**按长边取比例，在任何一个非正方形视图上都会溢出**——这条本身没什么可争的。
补上短边约束后，视图越扁，线越短，箭头就待在图里。

同时暴露 `cut_line_ratio`：默认值只是合理起点，具体图纸还得调。

### 顺带记一次正则改签名的坑

用 `re.sub` 往 MCP wrapper 的多行签名里插参数，把逗号拼成了：

```python
    verify_cut: bool = True,
,                                  # ← 孤立的逗号
                        cut_line_ratio: float = 0.55) -> str:
```

`static_check` 没拦（它做 AST 解析，语法错就直接抛），是 import 时炸的。
**改多行签名别用正则**，直接锚定最后一行替换。

## 四十七、`all_views=False` 要激活视图，否则 0 条也报成功（0.8.55）

`InsertModelAnnotations3(..., all_views=False)` 插到**当前激活视图**。没有激活视图时，
它插到空处——**仍然返回成功**。

反馈同图同模型：`all_views=False` → 0 条 / `ok: true`，`all_views=True` → 18 条。

修：单视图路径先 `ActivateView`；激不活返回 `active_view_not_set`；
插了 0 条返回 `no_dimensions_inserted`。

**这就是 0.8.44 那条「失败与空操作必须可区分」的又一次翻版。**

## 四十八、剖面线：`GetFaceHatchCount` 是唯一可靠判据（0.8.56）

剖视图建出来、`edge_count` 正常、`sanity: ok`，**剖切面却一条剖面线都没有**。

试过并全部无效：

| 尝试 | 结果 |
|---|---|
| `GetDisplayMode2()` | 本来就返回 2（隐藏线） |
| `SetAutoHatch(True)` | 官方 Remarks：**只对装配体有效，零件无效** |
| `ScaleHatchPattern = True` | 只缩放**已有**图案 |
| `SectionDepth` 0.00635 → 0.107 → 0.2 | 无变化 |
| `SetDisplayOnlySurfaceCut(True)` | 无变化 |
| `GetHatchStyle` / `SetMaterialCrosshatch` | 方法**不存在** |
| 材质缺失说 | 有材质「普通碳钢」也一样 |

**唯一可靠的信号**：

```python
section.GetFaceHatchCount   # 0 == 剖切面是空白
```

不需要渲染、不需要导 PDF，直接读。0.8.56 起 `== 0` 返回 `hatch_missing`。

**注意**：`DrSection.GetAutoHatch` **不能**当判据——连剖面线完全正常的剖视它也是 `False`。

## 四十九、剖切线画在图纸坐标 → 剖视静默退化成投影（0.8.57）

`ActivateView()` 之后 `SketchManager.InsertSketch()` 创建的草图，**原点在视图中心、单位是模型空间**。

旧代码却按图纸坐标画线（`pos[0] ± half`），整条线被 `view.Position` 平移出去
（实测偏 **76 mm 横向、265 mm 纵向**）。

SolidWorks **不报错**：照样返回 `Type=2` 的剖面视图，`_section_sanity` 也过（它只比宽度），
但**一刀没切**——视图内容是"轮廓 + 隐藏线"，不是剖切。

```python
# 错
sm.CreateLine(pos[0] - half, pos[1] + offset, ...)
# 对：围绕草图原点，长度和偏移都 / scale
half = min(max(view_w, view_h) * ratio, min(view_w, view_h) * 0.5) / scale
sm.CreateLine(-half, offset, ...)
```

修复实测：`GetFaceHatchCount` 0 → 2，边数 **13 → 37**。

`add_detail_view` 一直是写对的（`sketch = (sheet - pos) / parent_scale`）——
**同一个模块里两个函数，一个对了一个错了整整一轮**，而 v0.8.32 的断言还在要求错的那种写法。

## 五十、GTol：符号/数值/基准的渲染顺序（0.8.58 / 0.8.59）

**符号格填不进去**（`SetFrameSymbols2` 官方只对 2022 前的 GTol 有效），2022+ 要走 `IGtolFrame` XML。
但 `GetFrame(1)` 直接调用返回 `None`——**必须先转换格式、再补帧**：

```python
call_method(gtol, "ConvertFormat")          # GetFormat 1 -> 2，但帧被清空
call_method(gtol, "AddFrame")               # GetFrameCount 0 -> 2
frame = win.Dispatch(call_method(gtol, "GetFrame", 1))
```

**数值不渲染的根因是顺序**，不是字段名。`ConvertFormat` 才是把旧格式转成**文本项**的那一步：

```python
# 对：旧格式先写符号和值 → 转换 → 补帧 → 最终 XML
call_method(gtol, "SetFrameSymbols2", 1, "<IGTOL-POSI>", ...)   # 带尖括号
call_method(gtol, "SetFrameValues2", 1, "0.05", "", "A", ...)
call_method(gtol, "ConvertFormat")

# 错：转换后再写值，值只存在于 XML，图面永远不显示
```

实测：只转换后写值 → `GetTextCount 0`，图面只有 `⌖ A`；旧格式先写 → `GetTextCount 5`，
图面 **`⌖ 0.05 A`** 完整。

**同一个符号名两处写法不同**：旧 API `"<IGTOL-POSI>"` 带尖括号，XML `"IGTOL-POSI"` 不带。

## 五十一、注记里的 Ø 导出 DWG 变方框（0.8.60）

`add_note` 直接写 `Ø`（U+00D8）时，SolidWorks 存的是**普通字符**。
导出 DWG/DXF 时按规范转义成 `\U+00D8`——AutoCAD 会解码，**部分国产 CAD 不解码或字体缺字形，画成方框**。

而**尺寸**里的 Ø 走的是格式代码 `<MOD-DIAM>`，导出成 `%%c`，所有 CAD 都认。

所以同一张图上**同一个 Ø 有两种编码**：尺寸 `%%c`（正常），注记 `\U+00D8`（方框）。

修：`add_note` 自动把 `Ø ⌀ ± °` 换成 `<MOD-DIAM>` / `<MOD-PM>` / `<MOD-DEG>`，
并在回执的 `text_warnings` 里说明替换了什么。

反馈实测：DXF 里 `\U+00D8` 出现次数 **1 → 0**，`%%c` **4 → 10**。

**这类"PDF 正常、DWG 不正常"的分裂最危险**——出图的人看 PDF 说没问题，干活的拿 DWG。

## 五十二、基准要素符号：GB 是圆形，三角形是 ANSI（0.8.63）

`IModelDoc2.InsertDatumTag2` 能插基准符号（`IDrawingDoc.InsertDatumTag` 已 Obsolete），
**在当前位置的选择集上插入**——所以必须挂到边上，参数是 `view_name` + `edge_index`，
不能给自由图纸点。基准必须指向真实几何。

**默认标签是 `'B'`**，不设就是 B。必须读回校验，所以回执带 `label`，不符报 `datum_label_not_applied`。

### 形状：不要试图逼出三角形

文档属性 → 注解 → 基准点，**显示类型三选一**：

| 选项 | 定位样式 |
|---|---|
| 依照标准 / **方形** | 实三角形 / 带肩角的实三角形 / 虚三角形 / 带肩角的虚三角形 |
| **圆形(GB)** | 垂直 / 竖直 / 水平 |

**三角形的"定位样式"属于方形基准点**（ANSI / 部分企业标准）。
**GB 用圆形，本来就没有三角。**

实测该符号的几何构成：

```
GetTriangleCount = 0
GetLineCount     = 1
GetArcCount      = 1      ← 一条线 + 一段弧 = 圆圈
```

**三条死路（都试过）**：

| 尝试 | 结果 |
|---|---|
| `FilledTriangle = True` | 只填充**已有**三角；没有三角时属性空转 |
| `SetUserPreferenceIntegerValue(188, 0/1/4/5/6)` 制图标准 | `GetTriangleCount` 恒 0 |
| pref 187 / 189 / 190 / 191 / 199 × 值 1/2 | **全部无命中** |

真要方形三角样式，只能改**模板**：`gb_a3.drwdot` → 文档属性 → 注解 → 基准点 → 显示类型 → 方形。
那是模板改动，影响所有用该模板新建的图纸，不是代码能做的。

**这一条容易反复被当成 bug 报**——"基准符号没有三角形"。**每次的回答都一样：GB 就是圆形。**

## 五十三、焊接符号：能插、能写文字，但焊缝图形符号画不出来（0.8.66 探测）

`InsertWeldSymbol3` 在 **late binding 下必须无参调用**：

```python
ws = Win.Dispatch(call_method2(doc, "InsertWeldSymbol3"))   # 无参 → 成功返回 IWeldSymbol
```

带任何参数都报 `DISP_E_MEMBERNOTFOUND`（"找不到成员"）——试过 1~6 个空串，
全部失败。**不是 arity 猜错，是这个接口只接受无参。**

### SetText 的签名（官方）

```
IWeldSymbol.SetText(Top, Left, Symbol, Right, Stagger, Contour)
```

官方支持的 ISO 焊缝符号名（`gtol.sym`）：

```
BUTT BUSQ BUSV BUSB BUSVBR BUSBR BUSU BUSJ BACK FILL PLUG SPOT SEAM SEAMC JSPT JSM
```

**注意：表里没有 `FILLET`**。角焊大概由"符号格留空 + 默认样式"表示。

### 卡点：符号格只存文本，不画

```
SetText(True, "", "FILL", "6", "", 0)   → True
GetText(2)                              → 'FILL'        （存进去了）
GetText(3)                              → '6'           （尺寸也进去了）
GetTriangleCount                        → 0             （恒 0）
GetLineCount                            → 3             （引线 + 水平线 + 一小段）
```

渲染出来是：**引线 + 水平参考线 + 数字 6，没有角焊三角形**。

试过 `FILL` / `FILLET` / 空串 / `<WELD-FILLET>`，`GetTriangleCount` **全是 0**。

**这和第四十八节的 GTol 符号格是同一个病**：SW 接受字符串、原样读回，但不渲染成图形。

区别在于 GTol 后来找到了 `IGtolFrame` XML 这条 2022+ 新路（第五十节）；
**焊接符号还没有找到等价的接口**——`IWeldSymbol` 的方法表里只有
`SetText / SetFieldWeld / SetProcess / SetPeripheral / SetStagger / SetSymmetric / SetContour(只读)`，
**没有任何一个能指定焊缝图形**。

### 下一步值得试的

1. `InsertWeldSymbol2` / `InsertWeldSymbol`（旧版接口，参数表可能不同）
2. `SetProcess(True, ...)` 会不会顺带把符号带出来
3. 文档属性 → 注解 → 焊接符号（是否存在"默认样式"影响图形）
4. 换 SW 版本对照（本机 32.4.0）

**在找到把图形画出来的方法之前，不要发布 `add_weld_symbol`** ——
一个只有引线和数字、没有焊缝类型的符号，在车间比没有更危险。

## 五十四、形位公差引线：`SetLeader3` 有入口但被拒（0.8.75）

反馈里"框格没有引线"这条，我一开始判断"做不了"——**没试就下结论，是错的**。实测后发现入口是有的。

### 入口确实存在

```python
annotation = win.Dispatch(call_method(gtol, "GetAnnotation"))
annotation.SetLeader2   # ✅ 存在
annotation.SetLeader3   # ✅ 存在
annotation.GetLeaderCount  # ✅ 存在，初始为 0
```

注意：这些在 **`IAnnotation`** 上，不在 `GetSpecificAnnotation` 返回的对象上——
后者只有 `GetLeaderCount` / `GetLeaderAtIndex`，没有 `SetLeader*`。

### 签名（实测 arity）

`SetLeader3` 与 `SetLeader2` **都接受 6 个参数**：

```
SetLeader3(LeaderType, UseDocLeaderStyle, LeaderStyle, ArrowSize, UseDocArrowSize, LeaderBent)
```

### 但调用被拒

```
SetLeader3(1, True, 1, 0, True, True)  -> -2 | leaders=0
SetLeader3(1, True, 2, 0, True, True)  -> -2 | leaders=0
SetLeader3(1, True, 2, 0, True, False) -> -2 | leaders=0
```

**全部返回 `-2`，`GetLeaderCount` 仍是 0。** 参数形态试了 `LeaderStyle` 1/2、
`LeaderBent` True/False，都不行。

### 结论

**`-2` 的含义没查到，但现象明确：接口接受调用、拒绝生效。**

真要做引线转折（国标"先出一小段水平线再折下打箭头"），得先搞清楚这个 `-2`：
可能是要求先有可附着的几何（轮廓边没有顶点，`AttachTo` 也是 null），
可能是 `LeaderType` 的枚举值不对（1 未必是"直线"）。

**这一条留作未解，不宣布"做不到"，只记录"当前参数形态下被拒"。**

### 五十四补充：追下去发现了什么（同日）

**第一层：对象选错了。** 我一开始查的是 `GetSpecificAnnotation` 返回的对象（`type=6` 的 note）。
真正的形位公差是 **`GetType == 7`**——在反馈方那张图上叫 `细节项目260`。

**第二层：参数对了，状态也变了。**

```python
SetLeader3(1, True, 2, 0, True, True)   # LeaderStyle=2 (bent), LeaderBent=True
-> 返回 -2，但 GetLeaderCount 从 0 变成 1
```

`-2` 是「有问题」不是「失败」——对象状态确实改了。

**第三层：图面还是空的。** 导出 PDF 渲染：`0.008` 框格出来了，**没有任何引线**。

### 准确的结论

```
SetLeader3 能改状态（leaders 0 -> 1）
但图面不画引线
```

**同源问题第三次出现**：GTol 符号格（存字符串不渲染）→ 焊接符号（存字符串不渲染）→ 引线（改计数不渲染）。

**共同点：缺少可附着的几何。** 这些零件的轮廓边没有顶点，
`AttachTo` 返回 null，`attached_to` 也是 null——没有附着点，引线退化成零长度。

**这不是参数能解决的，是「先有边、才有引线」的顺序问题。**

### 五十四更正：签名读错了，不是「没几何附着」（0.8.77）

我上面那段「没有可附着的边，引线退化」的解释——**是错的**。

规划者指出 `SetLeader3` 的真实签名是：

```
SetLeader3(LeaderStyle, LeaderSide, SmartArrowHeadStyle, Perpendicular, AllAround, Dashed)
```

不是我猜的 `(LeaderType, UseDocLeaderStyle, LeaderStyle, ArrowSize, ...)`。

所以 `SetLeader3(1, True, 2, 0, True, True)` 的实际含义是：

| 位置 | 我以为是 | 实际是 | 我传的 |
|---|---|---|---|
| 1 | LeaderType | **LeaderStyle** | `1` = swSTRAIGHT（**不是转折**）|
| 2 | UseDocLeaderStyle | **LeaderSide** | `True`（**不是枚举**）|
| 3 | LeaderStyle | SmartArrowHeadStyle | `2` |
| 4 | ArrowSize | **Perpendicular** | `0` = **关闭垂直转折** |

**`-2` 的官方含义是「Leader attachment side setting is invalid」**——
它在说「LeaderSide 参数不对」，我却记成「渲染失败」。

### 正确调用（实测通过）

```python
SetLeader3(2, 2, True, True, False, False)
#          LeaderStyle=swBENT, LeaderSide=swLS_RIGHT,
#          SmartArrowHeadStyle=True, Perpendicular=True,
#          AllAround=False, Dashed=False
-> 返回 0，GetLeaderCount=1，PDF 中显示引线
```

### 枚举（swconst.tlb 读取）

```
swLeaderStyle_e: swNO_LEADER=0  swSTRAIGHT=1  swBENT=2  swUNDERLINED=3
swLeaderSide_e : swLS_SMART=0   swLS_LEFT=1    swLS_RIGHT=2
```

### 返回值对照

| 值 | 含义 |
|---:|---|
| 0 | 成功 |
| -2 | LeaderSide 无效 |
| -3 | 注解不支持引线 |
| -4 | 不能禁用引线 |
| -5 | 不能禁用转折引线 |
| -6 | 不允许下划线引线 |

### 教训

**我没查文档就自己解释了参数，再用那个错误解释去推根因。**
`-2` 明明在说「LeaderSide 不对」，我却顺着「没有附着几何」的故事编下去，
还写进了交接文档——**规划者得先纠正我的签名，才能纠正我的结论**。

**下次遇到接口返回非零，先查文档的签名与返回码表，别猜。**

## 五十五、图纸草图：`CreateLine` 确实返回对象，但图上和 PDF 里都没有（0.8.86 探测）

P2-2 要画轴线中心线。反馈试了五条路，都失败，建议「退回 `CreateLine` 并设 `SketchSegment.Style` 为中心线线型」。

### 线型这一环是通的

```python
sk = doc.SketchManager
seg = win.Dispatch(sk.CreateLine(0.04, 0.25, 0, 0.12, 0.25, 0))
seg.Style   # -> 0   （可读）
seg.Style = 6        （可写，不报错）
```

`Style` / `Layer`（`'10'`）/ `Color` 都能读到，`Style` 也能写。

### 但线不出现——而且**连 `add_sheet_line` 也一样**

用项目里已验证过的 `add_sheet_line`（smoke 断言它 `ok: true`）画同一条线，
**导出 PDF 与不画时像素级相同**：

```
add_sheet_line(drw, 0.04, 0.25, 0.12, 0.25) -> ok: true
导出 PDF -> 和没有这条线时完全一致
```

**特征树里也找不到这个 Sketch**（遍历 `GetTypeName2` 只有 BlockFolder / DocsFolder /
DetailCabinet / InkMarkupFolder / MarkupCommentFolder / CommentsFolder 这些文件夹）。

### 所以卡点不在线型

`add_sheet_line` 的 smoke 只断言 `ok`，**从来没有断言线可见**。
这次探测第一次去看了 PDF，发现它在图纸上根本不出现。

**在解决「图纸草图为什么不可见」之前，设 `Style` 没有意义**——
画不出线，就没有线型可言。

### 下一步该查的

1. **图纸草图是否需要「显示草图」开关**（`GetUserPreferenceToggle` 存在，没试）
2. **PDF 导出是否包含草图**（导出设置里可能有「包含草图」选项）
3. **图纸草图到底建在哪**——`InsertSketch()` 在图纸上是否真的开了草图
4. 反馈说「`CreateLine` 能渲染（BOM 线框验证）」——**但 BOM 是 `ITable` 对象，不是草图线**，
   那次验证可能不适用于图纸草图

**注意**：`add_sheet_line` 是 60+ 个工具里少数几个「只报 ok 不验证结果」的，
它的 bug 会静默传播。**建议先给它加一个 PDF 像素验证**，再谈中心线。

### 五十五补充：草图建出来了，但渲染不出来（同日追查）

**第 3 条已确认：草图是真的建了。**

```python
sm = doc.SketchManager
doc.InsertSketch()
sm.CreateLine(0.04, 0.25, 0, 0.12, 0.25, 0)
win.Dispatch(sm.ActiveSketch).GetSketchSegments  ->  1 段  ✅
```

**所以问题不在「有没有画」，在「画了为什么看不见」。**

### 已排除的

| 猜测 | 结果 |
|---|---|
| 草图没建 | ❌ 有 1 段 |
| Style 设错 | ❌ 设 0（实线）也不显示 |
| 线被画到模型空间 | ❌ 坐标是图纸米，且在图纸草图里 |

### 卡住的：图层读不到

线默认落在 **Layer = '10'**。文档里有 **12 个图层**，但：

```python
doc.GetLayerManager       # 属性，不是方法（加括号报 DISP_E_MEMBERNOTFOUND）
lm.GetCount               # 属性，不是方法（加括号报 int object is not callable）
lm.GetLayerAtIndex(i)     # 不存在（<unknown>.GetLayerAtIndex）
```

**12 个图层一个都读不到，Visible 也就无从判断、无从修改。**

### 其他撞墙的

- doc.PrintSetup 是方法且要参数（直接调报「非选择性的参数」），猜不出参数形态，读不到「打印是否含草图」
- GetUserPreferenceToggle 存在，但 0~260 全扫一遍没有明确的「显示图纸草图」项

### 当前可下的结论

**图纸草图（InsertSketch + CreateLine）能渲染 —— 缺的是 `SketchManager.AddToDB = True`。**

**2026-11-06 更正**：本节原先的结论「在图纸上不渲染」是错的。缺的是那一行开关。
`AddToDB = True` 关闭捕捉与推断，线才会落在调用方给的坐标上；不设它时
`CreateLine` 返回对象、`GetSketchSegments` 能数到，但图上和 PDF 里都没有。

实测（SW2024 SP4 + 飞达动力轴 A3）：

```
不设 AddToDB：add_sheet_line(0.30,0.24 → 0.35,0.24) → 0 像素
设  AddToDB：同一次调用                             → 572 像素，visible: true
```

同一个开关也让 `add_weld_symbol` 的三角形渲染出来（lines-only 差分 2134 像素）。

**这一层是「图纸草图」这个能力的基础**——它通了，add_sheet_line、手画图框、
引线才都有可能。`add_sheet_line` 现在做像素验证：`changed_pixels > 0` 表示画上了，
`0` 返回 `line_not_rendered`，`checked: false` 表示渲染器不可用（**不等于没画出来**）。

**证据面**：只测过 SW2024 SP4 + 飞达动力轴 A3 一次。换模板 / 换零件仍可能失败，
所以保留了 `line_not_rendered` 分支 —— 它是真实存在的路径，不是历史遗留。

### 若要继续，该试的

1. doc.PrintSetup(...) 的正确参数形态（查 IModelDoc2.PrintSetup 文档）
2. 宏录制：界面手动画一条草图线并导出 PDF，看它调了什么
3. 换一个「没有图层」的新模板建图纸再试（排除模板自带图层 10 被隐藏）
4. 在界面里看图层 10 的可见性，确认是不是它

---

## 五十六、一个尺寸有三套身份，而且互不认账（0.8.186 实测）

> **结论先行（2026-11-07 更正）**：**做得到。** 我原来写「做不到」，
> 因为第四层判断错了 —— 不是三套标识互不认账，而是**我一直在跨两套命名空间做差集**。
>
> 正确做法：快照用 `IDisplayDimension.GetNameForSelection()`，差集用这个名，
> 删除时遍历注解比对同名后 `Select(False)` + `EditDelete`。
> 实测 `dims 8 -> 7`，回到插入前的数量。
>
> 下面保留四层的排查过程，但**第四层的结论已作废**。

### 场景

`plan_dimension_gaps(apply_gaps=True)` 插入一条尺寸，读到 `actual_value_mm = 1570.796`
（目标 8.0，模型按 3D 欧氏距离量出来的是这个数）。既然插错了，就要撤回。

### 四层，逐层失败

**① 快照读的是磁盘，不是内存**

`add_distance_dimension` 默认 `save=False`，插入只存在于内存文档。而快照函数
`_dimension_full_names` 内部 `_open_drawing(path)` —— 它**重新读磁盘文件**，
看到的是插入前的状态。

```
插入前 FullName 集合 = {13 个}
插入后（重新读盘）= {13 个}      ← 差集永远空
```

**修法**：把内存文档句柄传进去。`_open_drawing` 对已打开的文档返回同一个 COM 对象，
所以拿到的就是那份带新尺寸的内存文档。

**② 修完之后 `name 'doc' is not defined`**

`plan_dimension_gaps` **从不持有文档句柄**——它只在保存时 `_open_drawing` 一次。
第一次改完直接崩。

**修法**：插入前 `_doc_live = _open_drawing(drawing_path)`，两处快照共用。

**③ 差集对了，还是删不掉**

```
before = 13
after  = 14
new    = ['RD6@工程图视图1@d.Drawing']
```

拿到新名字了。但 `delete_annotation(name=...)` **匹配的不是 `FullName`**：

| 标识 | 形态 | 谁用 |
|---|---|---|
| `IDimension.FullName` | `RD6@工程图视图1@d.Drawing` | 模型层身份，跨视图唯一 |
| `IAnnotation.GetName` | `细节项目34` / `D1` | 图纸层标签，**同一张图上会重名**（19 个 `D1` 实测过） |

**两套身份，互相找不到。** 用 `RD6@...` 去查 `GetName` 永远是 0 命中。

**修法**：改用插入位置删——插入位置就是当初请求 `add_distance_dimension` 的那个 `pt`，
精确到 `[0.2512, 0.2269]`。

**④ 按位置删不掉 —— 这一层的结论是错的**

当时 `rolled_back` 仍然 `false`，我据此判定「这条路也不通」。

**真相是位置本来就对不上**：`add_distance_dimension(x_m=0.20, y_m=0.15)` 之后，
注解的实际位置不是 `(0.20, 0.135)`，而 `delete_annotation(position_m=...)` 又是
按「离给定点最近且在容差内」匹配的，于是 `annotation_not_found`。

**真正的问题从一开始就不是「删不掉」，是「找不到」。**

### ⑤ 正解：有两套命名空间，别跨着用

```
IDimension.FullName               D1@Sketch1@AM_50ef1f.Part          ← 模型侧
IDisplayDimension.GetNameForSelection
                                  D1@Sketch1@AM_50ef1f-1@工程图视图1  ← 图纸侧
```

**`FullName` 命名的是模型里的那个尺寸，图纸包装加了视图后缀。**
用 `FullName` 做出来的差集，永远不可能匹配上 `Select` 认的名字。

正确流程：

```python
def _dimension_selection_names(doc):
    # 遍历每个视图的注解，取 GetNameForSelection()
    ...

before = _dimension_selection_names(doc)          # [(name, annotation)]
insert(...)
after  = _dimension_selection_names(doc)
new    = [n for n in after_names if n not in before_names]

for name, ann in after:
    if name not in new:
        continue
    doc.ClearSelection2(True)
    if ann.Select(False):
        doc.EditDelete()
```

实测（door 夹具）：`before=12 after=13 new=1`，删除后 `dims 8 -> 7` —— 回到插入前。

### `SelectByID2` 为什么用不了

它的签名是 `SelectByID2(Name, Type, X, Y, Z, Append, Mark, Callout, SelectOption)`，
`Callout` 是**必填 IDispatch**：

```
None     → 类型不匹配 arg 8
0        → 类型不匹配 arg 8
Missing  → PyOleMissing 无法转换
Empty    → 类型不匹配 arg 8
```

**所以只能走「遍历 + 比对 + Select(False)」这条路，不能按名字直选。**

### 补充：`Select(False)` + `EditDelete` 一直有效

单独测过：随便挑一条尺寸 `Select(False)` 再 `EditDelete`，`dims 7 -> 6`。
`DeleteSelection(False)` 同样有效。

**两条路都能删 —— 前提是你手上拿着那个注解对象。**

### 为什么 `delete_annotation` 在这个场景失效（推测，未证实）

同批插入的 `RD` 系列是 `InsertModelItems` 产生的**参考尺寸**，而
`add_distance_dimension` 新建的是**驱动尺寸**。两者在 `IAnnotation` 上的
`GetType` 都返回 4（尺寸），但 `Select(False)` 的行为可能不同——
`delete_annotation` 依赖 `Select(False)` + `EditDelete`。

**没验证这条推测**，因为验证它需要单独构造一个只有一条 `RD` 尺寸的图纸，
再用 `Select` 逐个试。**下次谁要继续这条，从这里开始。**

### 现在能保证什么，不能保证什么

| | 保证 |
|---|---|
| **能** | 单次调用只插错 **1 条**（熔断：`breaker_tripped: true`，其余标 `not_attempted_after_mismatch`） |
| **能** | 回执说清楚发生了什么：`inserted_but_wrong` / `sheet_modified` / `rolled_back` / `rollback_skipped_reason` |
| **能** | 诊断字段 `snapshot_before` / `snapshot_after` / `new_names` 直接给出差集 |
| **不能** | 自动删掉那条错的 —— **必须人工用界面删** |

### 下次别再重走这四层

如果以后有人提「按 `FullName` 删尺寸」：

1. 先确认 `delete_annotation` 是否已支持 `FullName`（现在不支持，只支持 `GetName`）
2. 如果要做，**别用 `Select(False)`**——先试 `IDisplayDimension.SetPosition` 之类不依赖选中的路径
3. 验证时**必须**用 `add_distance_dimension(save=False)` 造场景，因为 `save=True` 会让
   第 ① 层的磁盘/内存差异消失，测试假通过

**第 3 条是这轮最大的教训**：如果当初用 `save=True` 测，就不会发现第 ① 层，
也就不会知道「快照和插入用的不是同一份文档」。

---

## `add_drawing_dimension(style="diameter")` 建得出尺寸，但图面没有文字

实测 2026-11-09，`限位导向套.SLDPRT` 前视图，`edge_index=3`（`list_drawing_edges` 里
`is_circle` 且 Ø16 的那条）：

```json
{"created": true, "selected": "edge_index", "style_requested": "diameter",
 "style_used": "diameter", "value_m": 0.016, "ok": true}
```

回读：

```
RD1 | type=5（直径）| text='Ø' | value=16.0
```

**`type=5` 是直径尺寸、`value_m=0.016` 是 Ø16、`style_used` 也对。**
导出 PDF 用 pypdfium2 按 6× 渲染成 7144×5052 PNG，裁中央视图放大：

**图上有引线和箭头，没有数字。`GetText` 给的 `'Ø'` 后面本该由渲染器补 `16`，实际是空的。**

### 这意味着什么

**这条路「能建但不能用」** —— 比 `add_hole_callout` 的 `Ø16 ↧ 68` 好（至少不误导成盲孔），
但**仍然不是一个可以交付的标注**。

**也说明「改用 `add_drawing_dimension` 补 Ø16」这个建议（报告 2026-11-09 §3.3 第 5 点）
没那么简单**：工具的 API 层是通的，断在渲染/文字这一层。

### 根因：槽里只有 `<MOD-DIAM>`，没有 `<DIM>`

2026-11-10 探测同一条尺寸的显示属性：

```
GetText(0) = ''
GetText(1) = '<MOD-DIAM>'      <- 只有 Ø 格式码
GetText(5) = '<MOD-DIAM>'
ShowDimensionValue = True      <- 显示值是开的，不是被关掉
IsHoleCallout = False
```

**`<DIM>` 是数值占位符。** 没有它，渲染器不知道该往 `Ø` 后面填什么，于是只画引线和箭头。

**不是直径特有的** —— 用 `style="linear"` 去标 Ø63 那条圆边，结果一样
（`type=5`、`text='Ø'`），因为边是圆时 SolidWorks 仍按直径建。

### 修法：`SetText` 补回 `<DIM>`

```python
disp.SetText(1, "<MOD-DIAM><DIM>")     # 返回 None（不是 False）
# 回读：GetText(1) == '<MOD-DIAM><DIM>'
# 渲染：圆下方出现 Ø 与 16
```

**`SetText` 返回 `None` 不等于失败** —— 必须回读确认，这是本项目反复踩的同一个坑。

### 补 `<DIM>` 是错的：它被当字面文本打印

`SetText(1, "<MOD-DIAM><DIM>")` 回读成功，**渲染出来是 `Ø<DIM>16`** —— `<DIM>` 不是占位符，
是普通文本，而 `16` 是系统自己补的数值。

### 硬写数值也不行：变成 `Ø1616`

`SetText(1, "<MOD-DIAM>16")`（数值取自 `value_m`）渲染成 **`Ø 1616`** ——
`ShowDimensionValue=True` 让系统在自定义文本之后再补一遍数值。

**同一个开关解释了两次失败**：

| 写法 | 渲染 | 原因 |
|---|---|---|
| 不动（`GetText(1)` = `<MOD-DIAM>`） | **空** | 自定义文本吃掉数值，系统不再补 |
| `<MOD-DIAM><DIM>` | `Ø<DIM>16` | `<DIM>` 是字面量，系统补了 `16` |
| `<MOD-DIAM>16` | `Ø1616` | 系统又补了一次 |

**三种写法各错一种，说明 `SetText` 与 `ShowDimensionValue` 在这里的合成规则不是
「自定义优先」也不是「系统优先」，而是二者叠加** —— 我没有找到能只留一次的写法。

### 顺带确认：`offset_x` 控制方向

同一批实验里 `offset_x=0.035` 的那条**是水平的**，`offset_y` 负值那两条是竖的。
**所以排版方向可以用 `offset` 调**，不是死路 —— 死的是文字内容。

### 结论：`add_drawing_dimension` 不适合自动补标 Ø16

**能建尺寸、能定位、能控制方向，但文字无法稳定输出「Ø16」。**

**当前处置（v0.8.291 起）**：不再是「贴个警告、错字留在图上」。`add_hole_callout`（单数）
在读到深度符号或读不到文字时**撤销刚创建的标注**，返回
`ok:false / verified:false / drawing_safe:false / created:false`，并给出 `callout_revert`；
`add_hole_callouts`（复数）对无法判定的直径返回 `ok:false / verdict:partial`，同时用
`execution_ok:true / completeness:incomplete` 区分「图纸不完整」与「调用失败」。
`model_match.through_state` 如实为 `unknown`（ledger 只有圆柱面清单，判不出贯通）。
**不要**自动用 `add_drawing_dimension` 补标 —— 会得到 `Ø1616` 或 `Ø<DIM>16`。
贯通主孔请在 SolidWorks 界面手工标。

### 补出来之后（旧记录，已作废）：文字是竖排的

渲染图上 `Ø` 和 `16` 旋转 90°、竖排在箭头下方，不是 GB 习惯的水平标注。

**没调**：可能是 `ArrowSide=3` 或标注方向继承自竖直的直径线。**下次改这里之前先试
`offset_x/offset_y` 把文字挪到视图外，再看是否需要旋转。**

### 对「补标 Ø16」的意义（旧结论，已作废，仅存档）

> ⚠️ 本节写于 `SetText` 实验早期，**结论已被上面「文字无法稳定输出 Ø16」推翻**。
> `SetText(1, "<MOD-DIAM><DIM>")` 渲染成 `Ø<DIM>16`，写数值渲染成 `Ø1616`，
> 不写则完全没有文字。路径**不通**。保留只为说明当时的判断轨迹。

`add_drawing_dimension` **能建、能补文字**，路径是通的：

1. `add_drawing_dimension(edge_index=..., style="diameter")` -> `type=5`、`value_m` 正确
2. `SetText(1, "<MOD-DIAM><DIM>")` -> 图面出现数字
3. 排版方向仍需调整

**比 `add_hole_callout` 的 `Ø16 ↧ 68` 好** —— 那个把通孔写成盲孔，这个是方向难看但数值对。

---

## 2026-11-10（v0.8.291）：三份验收反馈里三个「看起来修了其实没修」的根因

### 1. `list_tool_profiles` 自己就是第 71 个工具，却写着 `all 70`

`profiles.full` 曾是一句英文说明 `["* all 70 — this is what is registered today"]`，
而同一份回执里 `counts.full` 与 `registered` 都由运行时 registry 算出，等于 71。
新增的 smoke check 只断言 `ungrouped == []` 和 `registered >= 71` —— **下界永远不会发现
说明文字里的静态数字**。`full` 现在就是 `names` 本身（真工具名数组），`counts` 统一
`len(v)` 无特例，另加 `unknown_names` 检测分组里残留的不存在工具名。守护分两层：
源码级禁止 `all NN` / `NN tools` 这类短语，运行时断言 `counts[k] == len(profiles[k])`、
`_union == set(full)`。

### 2. `section_off_axis` 名不副实：偏心改的是被丢弃的那一维

`_section_sanity` 的 `kept_dimension` 判据基于几何事实：垂直于水平轴的剖切不能改变宽度。
但**回转体的偏心弦剖缩短的是高度**，恰好是被丢掉的那一维。实测（凹槽滚轮 2:1，
`offset_m=0.004`）：

```text
kept width   51.98 -> 51.98   ratio 1.000   （旧版甚至比上轮的 0.998 更像「正常」）
other height 127.98 -> 127.73  差 0.25 mm = 0.195%
```

任何按百分比的门（0.5%、1%、10%）都放行。现在两条轴都进回执，非 kept 维用
**绝对 0.1 mm** 阈值判定；判定逻辑抽成 `_section_off_axis_from_sanity(sanity)`（纯函数，
无 COM），smoke 用上面这组实测数字回放。`depth>0` 部分剖与 cross-section 仍是
`checked=false`，不参与判定。另给 `eccentricity_mm = |offset_m| / view.ScaleDecimal`
作**诊断**（不是判据 —— 错的 scale 不该把坏剖切说成好的）。

`auto_nudge_zero_offset` 默认已是 `False`（`cut_line_overshoot_m` 修掉根因后这个
workaround 就是冗余的）；显式开启时 `offset_nudged` 提升为顶层 `warning`。
旧 hint 里「偏 2 mm 从 64 掉到 63.5」也不准，正确弦长是 `2·√(32²−2²) = 63.875`。

### 3. `Ø16 ↧ 68` 一直能被读到，是读法选错了对象

复数工具 `add_hole_callouts` 这样读，能读到：

```python
dim = win32com.client.Dispatch(display)   # AddHoleCallout2 的返回值
dim.GetText(slot)                         # -> "<MOD-DIAM> 16 <HOLE-DEPTH> 68"
```

单数工具 `add_hole_callout` 却用了 `callout.GetDimension2(0).GetText(slot)`，
所有槽都空 → `text_slots: []` → `text_unreadable`。于是 0.8.279 的结论
「SolidWorks 没给出可读文字」是**读法 bug**，不是 API 限制。

v0.8.290 的问题不是不诚实：`text_flags` 明写「不要把 ok:true 当文字正确的证据」，
而工具仍然返回 `ok:true`；`model_match.ok:true / delta_mm:0.0` 只证明 16.0 == 16.0，
反而制造了更强的安全感；警告写在复数工具的 note 里，单数调用者根本看不到。
**贴告示不等于堵坑。**

0.8.291 的做法：

* 用复数工具已验证的 Dispatch 路径读文字；
* `_hole_callout_verification(text, model_match_ok)` 纯函数决定 `ok`，
  带深度符号 → `unsupported_through_bore`，读不到 → `hole_callout_unverifiable`；
* 未验证时用 `GetAnnotation` / `Select(False)` / `EditDelete` / `EditRebuild3`
  **撤销刚创建的标注**（与复数工具 `_discard` 同一条已验证路径），回执给
  `created:false / ok:false / verified:false / drawing_safe:false / callout_revert`；
* `model_match` 增 `through_state:"unknown"`、`depth_text_consistent:null`、
  `ok_means`，明确 `ok:true` 只表示直径匹配；`note` 同步解释 `ok:null` / `ok:true` / `ok:false`。

**仍不做**：改写文字（`SetText` 会毁掉其它槽，历史 `Ø1515`）；猜贯通状态
（图纸与 ledger 都判不出，猜了就是编数据）。主孔留给人手工标。

### 4. 顺带更正的旧结论

* `check_capabilities(section_view).why` 的「剖面线取决于零件几何、因此 partial」是
  2026-11-05 的误判，真根因是剖切线端点退化；已标为历史存档。
* `advice` 不再对 `status:partial` 说「全部可做」。
* `hole_callout_text_slots.workaround` 不再推荐已证伪的 `add_drawing_dimension`。
* `add_sheet_line` 的 `line_style` schema 补上数字码：`solid` 名义实线实测是 Style 1
  细虚线，`dashdot` 名义 GB 细点划线实测是 Style 5 的 194 个稀点；要轴线传 `"4"`，
  要连续实线传 `"7"`。映射**不改**（单一构建的证据不足以改默认值），但回执新增
  `line_style_warning`。过时的「图纸草图不渲染」hint 也已撤回。

### 5. 本机真机复核（2026-11-10，SW 32.4.0，`限位导向套.SLDPRT`）

按反馈文档的复现坐标 `create_drawing(views=["front"], scale=1, auto_fit=False,
view_positions=[[0.12,0.18]], paper="A3")` + `add_hole_callout(0.128, 0.18)`：

```json
"readback": {"text": "<MOD-DIAM> | <HOLE-DEPTH>68",
             "text_slots": ["<MOD-DIAM>", "<HOLE-DEPTH>68"],
             "text_reader": "IDisplayDimension via Dispatch", "value_mm": 16.0}
"ok": false, "created": false, "error": "unsupported_through_bore",
"verified": false, "verdict": "unverified", "drawing_safe": false,
"callout_revert": {"reverted": true},
"annotations_before": 2, "annotations_after": 2
```

`text` 能读出 `<HOLE-DEPTH>68` 是对「旧版读不到文字」的**直接反证**；
注解数 `2 → 2` 证明错误标注确实被撤销，没有留在图上。

带 `model_path` 再调一次（旧版 `model_match.ok:true / delta_mm:0.0` 的双重放行）：

```json
{"ok": false, "created": false, "verified": false,
 "model_match_ok": true, "model_match_delta": 0.0,
 "through_state": "unknown", "reverted": true, "annotations": [2, 2]}
```

`model_match` 仍然只证明直径匹配，但它不再能把 `ok` 拉回 `true`。

`add_sheet_line(line_style="dashdot")` 真机同样给出
`line_style_applied:5` / `very sparse dots` / 194 笔，并附顶层
`line_style_warning` 指向 `"4"` 与 `"7"`。

**另一处真机发现**：`smoke.py` 的 `tool profiles cover every tool` 从 v0.8.284 起
就因 `server` 从未导入而恒 `NameError`；`check_all.py` 只数 `r.check` 行、不执行，
所以一直被算作覆盖。用 `smoke.py --only 'tool profiles'` 才暴露。已改为局部导入
`server`，并加源码级断言防止重现。**「有个 check」不等于「check 跑过」。**

---

## 2026-11-10（v0.8.292）顶层 `inputSchema.additionalProperties:false`：声明与守卫不是一回事

连续 10 轮反馈（v0.8.235 → v0.8.290）都列了同一条 P3：`tools/list` 里所有 tool 的
顶层 `inputSchema` 都是 `additionalProperties: <absent>`，而 `server.py` 全文检索
`additionalProperties` 命中 0。期间修的一直是**运行时拒绝**
（`_install_argument_guard` 挂在 `request_handlers[CallToolRequest]`，7/7、12/12 实测
拒绝未知参数），始终没补**对外发布的契约**。这是两件事：

| | 作用位置 | 能挡住什么 |
|---|---|---|
| `_install_argument_guard` | 服务端 MCP 请求入口，看到**原始** `params.arguments` | 已发到服务端的未知键 → `isError:true / unknown_argument` |
| `additionalProperties:false` | `tools/list` 发布的 JSON Schema | 客户端**发送前**的本地校验；以及告诉调用方哪些参数真实存在 |

报告 v0.8.247 第四节指出：部分 MCP 客户端会先按 schema 在本地过滤未知键，
守卫根本看不到原始 `arguments`。所以声明覆盖的是守卫够不到的那一段，不是锦上添花。

### 实测：声明不是运行时守卫

mcp 1.30.0 的 `FastMCP.list_tools()` 直接把 `Tool.parameters` 作为 `inputSchema` 发布
（`inputSchema=info.parameters`），因此改 `Tool.parameters` 是最小注入点，
无需改动任何 decorator 或函数签名：

```python
# 装了 seal、但没装 _install_argument_guard
ping({})                      -> ok=true
ping({"bogus_param": 123})    -> ok=true   # 仍然静默丢弃！
```

FastMCP/pydantic 默认 `extra="ignore"`，`additionalProperties:false` 在这个版本
**不会**触发服务端校验错误。结论：**`main()` 里的 `_install_argument_guard(mcp)`
绝不能因为补了声明就删掉**；反过来，也不能因为守卫能拦就不发声明。

### 只改顶层，别碰嵌套

Pydantic 会对 dict 类型参数生成 `additionalProperties: true`（当前构建的
`recolor_assembly.palette` / `recolor_bodies.palette` 等；数量随 SDK 和工具集变化，
**不要写死数字**）。递归改成 false 会直接破坏这些参数。实现里只有一次下标赋值：
`params["additionalProperties"] = False`，`params` 取自
`getattr(tool, "parameters", None)`。回归断言锁死「恰好一个 Subscript 写、
slice 是 `additionalProperties`、值是字面量 `False`、函数体不出现 `properties"]`」。

### 模块级 seal，而不是放进 `main()`

`import server; mcp.list_tools()`（测试与进程内客户端走的路径）不经过 `main()`。
所以 `_SCHEMA_SEAL = _seal_input_schemas(mcp)` 放在所有 `@mcp.tool()` 装饰器之后的
模块级；`main()` 只保留 `_install_argument_guard(mcp)`。

### 失败必须可区分

seal 返回 `{registered, sealed, ok}`，而不是静默返回 `None` 或只丢一个计数；
`list_tool_profiles()` 回执新增 `input_schema_seal`。封了 0 个工具时 `ok=false`，
不会看起来像一次干净的空操作 —— 沿用 2026-11-10 的通用规则：
**任何可能被吞掉或空转的分支，失败与空操作必须可区分。**

### 验证

* `smoke.py --only "input schemas"` 真跑 `asyncio.run(mcp.list_tools())`：
  `71/71 schemas sealed, 2 nested declarations preserved`，并通过
  `mcp.call_tool("list_tool_profiles", {})` 确认正常调用的校验路径没被破坏。
  check 局部导入 `server`，避免重演上一轮「check 恒 NameError 却被算作覆盖」。
* 真实 stdio 进程 `tools/list` + 未知参数拒绝：见 v0.8.292 发布记录。
* 源码级断言 `0.8.292 top-level input schemas are sealed`：模块级调用位置、
  顶层唯一写入、`main()` 守卫仍在、回执自证、smoke check 已注册，一次全查。

---

## 2026-11-10（v0.8.293）四项长期遗留：回执语义 / 占位符 / 侧视图投影 / 版本门禁

这四项分别被 2 / 2 / 4 / 4 轮反馈点名，一直排在优先级表末尾。共同点都是
**「代码跑通了，但回执在说另一件事」**，所以修法都不是改 COM 行为，而是让字段说真话。

### 1. `native_text_complete` 与 `" | "` 合成串（0.8.278 §4 / 0.8.281 §6）

0.8.278 用 `text = " | ".join(_parts)` 把多槽文字拼成一串，并把
`native_text_complete` 定义成 `len(_parts) > 1`。两个判据都不成立：

| | 旧行为 | 0.8.293 |
|---|---|---|
| `text` | 自造的合成串，`list_drawing_annotations` 的 `text_is_format_code` / `text_decoded` 按单串解析 → 语义漂移 | **一条原生文字**；其余行放 `text_lines` 数组 |
| 单行标注 | 1 个槽 → `complete:false` + `text_is_partial:true`（假阴性，把正确图纸判成不完整） | `native_text_complete: null` |
| 只读到 2/3 行 | 2 个槽 → `complete:true`（假阳性） | 仍是 `null`；固定枚举 6 个槽**无法证明**没有第 7 行 |
| 完备性 | 一个 bool | `text_completeness: "unknown"` + `text_completeness_reason` |

新增共用纯函数 `_read_native_callout_text(readers)`，单/复数两个工具都走它；
`text_slots_read` / `text_slots_raw` / `text_reader` 让「读到了什么」完全可审计。

**⚠️ 最容易踩回去的坑**：单数工具 `add_hole_callout` 用文字判定深度符号（`↧` /
`<HOLE-DEPTH>`）来决定是否撤销标注。0.8.291 实测
`text_slots = ["<MOD-DIAM>", "<HOLE-DEPTH>68"]`——深度在**第二槽**。
把 `text` 收成单串后若仍用 `text` 判定，`Ø16 ↧ 68` 会重新拿到 `ok:true`，
把刚堵上的 P0 放回去。所以内部判据是 `_joined = " ".join(text_lines)`，
并用一条 smoke check 专门回放这个陷阱。**改 `text` 语义必须同步检查所有判据消费者。**

### 2. `<hw-*>` 未解析模板槽位（0.8.278 R-4 / 0.8.290 S-5）

实测第三段：`<HOLE-SPOT><MOD-DIAM> <hw-cbdia> <HOLE-DEPTH> <hw-cbdepth>`。
`<hw-cbdia>` / `<hw-cbdepth>` 是 Hole Wizard 内部占位符，**没有绑定值、图面不显示**，
拼进回执后自动核查会判成「文字未解析」。

关键区分：**`<MOD-DIAM>` 是格式码，渲染器会变成 Ø，必须保留**；`<hw-*>` 是死占位符。
所以只剥 `<hw-[^>]*>`，不做「删掉所有尖括号」——后者会重现 `Ø1515` 那一类事故。
剥离结果进 `text_lines` / `text`，原始逐槽文本完整留在 `text_slots_raw`，
并新增 `hw_placeholders_stripped` 明说去掉了哪些 token。

### 3. 侧视图 `sheet_projected:true`（0.8.266 P1-3 / 0.8.276 Q-6 / 0.8.290 S-6）

连续四轮实测同一个右视图：5 条边**全是圆**，
`sheet_start == sheet_end == sheet_center`、`sheet_start_is_center:true`，
顶层却写 `sheet_projected:true`。这个 `true` 只表示「圆心被投影过」，
而 `add_distance_dimension` 需要**两个不同端点**的直线边来按 `edge_index` 选边，
70 总长因此始终标不出来。

0.8.293 把口径改成「有没有可用于选边的投影边」：

* 新增纯函数 `_edge_kind`（`circle` / `line` / `unknown`）与
  `_edge_projection_scope(rows)`。顶点拿不到的轮廓边诚实返回 `unknown`——
  该 binding 无法分辨圆弧与无顶点的直线，**不编 `arc`**。
* 全圆视图：`sheet_projected:false`（顶层 + 每行）、
  `sheet_projection_reason:"circular_model_edges_only"`、
  `usable_for_edge_selection:false`、`usable_edge_count:0`、`edge_kinds`。
* 圆心坐标**不删**（轴向间距标注要用），新增 `sheet_coordinates_present:true` 明说还在。
* `axes_ambiguous` 等原有失败原因优先于 `circular_model_edges_only`，不互相覆盖。
* `server.py` 的 `list_drawing_edges` 描述同步改成新语义。

### 4. `bump_version.py --check` 无 `.git` 假安全（0.8.275 §6 / 0.8.290）

`_git()` 曾把「命令失败」与「成功但输出为空」都返回 `""`。在无 `.git` 的
tarball / wheel 里 `head` / `prev_commit` 皆空，重复 tag 检查被整段跳过，
却打印 `HEAD ? is ahead of (no tag) — safe to tag` 并返回 0。

* `_git()` 改为 `str | None`，失败返回 `None`；
* `--check` 先跑 `git rev-parse --is-inside-work-tree`，不是工作树或读不到 HEAD /
  解析不到上一 tag，一律打印 `UNVERIFIABLE — the duplicate-tag check was NOT run`
  并 `return 1`（fail-closed，与项目门禁同一口径）；
* 首次发版确实没有上一 tag，单独走「duplicate check not applicable」分支；
* **否定文案里也不能出现 "safe to tag" 字面量**——发布审计按短语 grep，
  一句「不能声称 safe to tag」会被读成通过。第一版就是这么被自己的断言抓到的。

### 验证

四项全部用**反馈里的实测数据**做纯 Python 回放，不依赖 SolidWorks：

| smoke check（`--only` 可单独跑） | 结果 |
|---|---|
| `callout text stays one native string` | 三行实测字符串：单串 `text`、`text_lines` 3 条、`<hw-*>` 剥离、原始槽保留、`<MOD-DIAM>` 保留、完备性 `null` |
| `a depth in another slot is still refused` | 复现 0.8.291 陷阱：只看 `text` 会 `verified:true`，按全部行判定 `unsupported_through_bore` |
| `circular-only edges are not sheet-projected` | 5 条实测圆边 → 不可选；加 1 条直线 → 可选；无顶点 → `unknown` |
| `bump_version --check fails without git` | 真起子进程：git 工作树 RC=0、无 `.git` RC=1 + `UNVERIFIABLE`、版本不一致 RC=1 |

`check_all.py` 278/278，`static_check` RC=0，`stdio_contract.py` 6/6。
**全量 `smoke.py` 本轮未跑**（历史 42–69 min）；本轮改动不触碰任何 COM 调用路径。

---

## 2026-11-10（v0.8.294）同轴沉孔合并不再拿半径阈值兼做倒角判定

### 1. 反馈与反例

报告 2026-11-09 §4：0.8.278 为「同轴沉孔产生两条逐字相同 callout」加了
`_same_axis`，但合并条件是 `(_keep_r - _k[1]) > 0.0015`（半径差 > 1.5 mm），
注释说是为避开 chamfer。**Ø11 沉孔配 Ø9.8 通孔径向差 0.6 mm 会绕过合并**，
图纸仍然出两条相同标注。

根因不是阈值取小了，而是**同一个 1.5 mm 被两个不同语义复用**：

| 位置 | 判定 | 语义 |
|---|---|---|
| `_chamfers` 过滤器 | `半径差 <= 1.5 mm`（且 3D 中心邻近） | 「这一对可能是倒角口」 |
| coaxial 合并 | `半径差 > 1.5 mm` | 「这一对不是倒角，可以合并」 |

第二条只是对第一条的反向猜测，不是独立事实。倒角对和浅沉孔在半径维度上
本来就重叠，用同一个数切不出两类。

### 2. 改法：把「是否 chamfer 对」从事实来源传下来

* 常量 `CHAMFER_RADIUS_GAP_M = 0.0015` **只**服务于 chamfer 过滤器；`_near` 的
  1.5 mm 是 3D 中心聚类，语义不同，保留自己的字面量。
* 过滤器判定一对时，把参与的两个半径对应的**全部 key** 写入
  `_chamfer_pair_keys`（被删的、被 through-bore 保护留下的都算「它判过」）。
* 新增纯函数 `_coaxial_merge_plan(axis_keys, chamfer_pair_keys)`：
  `clean = 不在集合里的 key`，**不做任何半径比较**；`clean` 中保留最大半径的 key，
  其余半径不同者 drop。等半径 key 是不同孔位，永不 drop。
* 回执新增 `coaxial_chamfer_protected`：被 chamfer 判定保护而没参与合并的 key 数。
  「保护生效」和「没有触发保护」必须可区分，不能都显示 0 而无人知道有没有跑过。

### 3. 为什么仍然保守（已知边界）

`_chamfer_pair_keys` 会保护同簇内被 `ambiguous_chamfer_pairs` 报告的圆对。
一个 3D 中心差 ≤1.5 mm 的**极浅沉孔**在几何上与 45° 倒角口不可分
（轴向深度 ≈ 径向增量），本实现宁可让它们继续走 `ambiguous_chamfer_pairs` +
`ok:false` 的人工标注路径，也不冒险把真实孔 Ø16 折进倒角口 Ø18 里删掉
（那正是 0.8.266 / N-3 的 P0：漏标可手工补，把倒角标成孔是错的零件）。
常规沉孔两端 Z 差等于沉孔深度（实测 6.8 mm），落在**不同** `_by_centre` 簇，
过滤器不会判过，直接按同轴合并 —— 这才是反馈反例的实际结构。

### 4. 验证

| check | 结果 |
|---|---|
| `check_all.py` | 280/280（新增 `coaxial merge ignores radius gap` / `coaxial merge has live replay`） |
| `smoke.py --only "shallow coaxial counterbores still merge"` | PASS：真调纯函数回放 Ø11/Ø9.8 = 0.6 mm、Ø11/Ø6.6 = 2.2 mm、Ø16/Ø18 保护、等半径不误删 |
| 真机 smoke（3 个针对性 check） | 3/3 PASS：`list_drawing_annotations with hole callouts (P0)` 47.3 s、`hole callout leader anchor (P1-7)` 38.8 s、纯 Python 回放 0.0 s；前者断言 `coaxial_chamfer_protected` 是**真回执里的整数**、`coaxial_duplicates_dropped` 是 list |
| `stdio_contract.py` | 6/6 |

旧断言 `0.8.278 one callout per coaxial hole` 曾要求源码里必须有
`(_keep_r - _k[1]) > 0.0015`；0.8.294 起该字面量**出现即为回归**，断言已重写
并标为历史作废。教训与 0.8.293 的 `text` 语义漂移同类：
**改动判据时必须同步枚举所有以旧判据为守卫的回归断言，否则旧断言会反过来锁死修复。**

---

## 2026-11-11（v0.8.295）全量 smoke 55/56，唯一 FAIL 是测试断言缺陷

### 全量结果

`python tests/smoke.py` 真机跑完，53 m 19 s，**55 passed / 1 FAILED / 56 run**。
0.8.294 新增的 `shallow coaxial counterbores still merge` PASS。
`partial reaches produce_drawing` 诚实标注
"not triggered — this fixture has no chamfer-ambiguous diameter"，不是通过。

### 唯一 FAIL 的定性

`assembly drawing not an empty shell` → `call failed: None None`。
单跑 `produce_drawing` 打印回执：

```text
ok=false, error=null, message=null
partial_steps=["hole_callouts"], view_count=3, 25 dims
callouts: created=1 failed=31 verdict=partial budget_exceeded=true
```

0.8.285 起 `produce_drawing` 的 partial 结果**故意**给 `ok:false`（孔标注有倒角歧义），
但视图和尺寸都已产出。该 check 用严格 `require()`，把 `ok:false` 当调用失败；
`error` / `message` 都是 null，于是打印出误导性的 `call failed: None None`。
`smoke.py:1128-1147` 早已记录同一坑并在 `partial reaches produce_drawing` 用了
`allow_partial=True`，只是 `_assembly_drawing` 漏了。

与 v0.8.294 的产品改动无关：`add_hole_callouts` 的 `ok` 只由 `_ambiguous` 决定，
0.8.294 只改了 coaxial 合并如何消费该集合，未改 `_ambiguous` 的生成。

### 修复与验证

* `_assembly_drawing` 改用 `require(..., allow_partial=True)`，删掉裸
  `assert payload.get("ok")`；后续 `views` / `view_count` / `drawing_audit` 断言不变 ——
  partial 图纸同样必须证明不是空壳。`allow_partial=True` 仍拒绝带 `error` 的回执，
  真实调用失败不可能被掩盖。
* 新增源码级断言 `0.8.295 assembly check accepts partial`（281 条），防止严格
  `require()` 回来。
* 复验：`check_all.py` 281/281、`stdio_contract.py` 6/6、
  `smoke.py --only "assembly drawing not an empty shell"` **PASS**（3 views, 28 dims）。
* **修复后未重跑全量**（53 min）；其余 55 项在全量里已全部 PASS，改动只影响这一项。

---

## 2026-11-11（v0.8.296）零偏置不再被静默改写成偏心剖（报告 2026-11-10 P1）

### 问题

`add_section_view` 的内层 `drawing.add_section_view` 的 `auto_nudge_zero_offset`
默认早已是 `False`（0.8.255 用 `cut_line_overshoot_m` 修掉「剖切线端点被 clamp」
根因后不再需要 nudge），但 **MCP 外壳 `server.add_section_view` 仍是 `= True`**。
于是每一次工具调用 `offset_m=0` 都被改写成 `0.001 m`（1:2 时 0.5 mm 模型偏心），
而同一份回执的 hint 已经写着「`auto_nudge_zero_offset` 默认已改为 False」——
代码与承诺正好相反。

影响不是观感问题：偏心剖量到的直径是错的（Ø64 → 弦长 63.875），
而剖视仍 `ok:true` + `hatch:2`，只有直径错。报告 §6 的 CI smoke #5 即此。

### 改法

* 外壳默认改 `False`，描述重写：说明根因已由 `cut_line_overshoot_m` 解决；
  显式传 `True` 时回执给 `offset_nudged:true` + 顶层 `warning`（点名偏心量）。
* 内层默认保持 `False`，不动。
* 新增纯 Python check `zero offset is not nudged by default`：用
  `inspect.signature` **同时**断言两层默认值是 `False` —— 只查一层正是漏掉本 bug 的原因。
* 真机 `revolved part section verdict` 增加两条断言：`offset_nudged is False`、
  `offset_used_m == 0.0`，且剖视仍 `ok / hatch 2`（证明不 nudge 也能过轴切）。
* 新增回归断言 `0.8.296 zero offset not nudged by default`（282 条），
  用 AST 读两处签名默认值，并守护上述真机断言存在。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 282/282 |
| `smoke.py --only "zero offset is not nudged by default"` | PASS |
| `smoke.py --only "revolved part section verdict"` | PASS，34.6 s，`default=ok edges 28 hatch 2`、`offset_nudged:false`、`offset_used_m:0.0` |

### 教训

**同名参数的默认值分散在两层时，必须断言两层一致。** 0.8.296 之前那条
`assert "auto_nudge_zero_offset=False" in body` 只查了内层函数体，
外壳那一行从未被任何断言看过 —— 于是「内层已修好」被当成了「已经修好」。

---

## 2026-11-11（v0.8.297）回执可读性四项（报告 2026-11-10 P2-1/3/4/5）

### P2-1 位置字段混了两个时刻

`add_section_view` 的 `alignment_kept` / `position_axis_ignored` 在**打断对齐之前**
计算，而 `position_actual_m` 在**打断之后**重读。于是同一份回执可以写
`position_axis_ignored: "x"` 却给出两个完全相同的坐标，或 `alignment_kept: true`
旁边躺着成功的 `alignment_broken`。两句话各自为真，只是不在同一时刻。

* 抽出纯函数 `_position_deviation(requested, actual, tol=1e-3)` → `(rewritten, axis)`。
* 打断前只取一次快照 `alignment_rewrote_request`（决定是否需要打断）。
* **所有**位置字段改为打断之后用同一函数重算；新增 `position_fulfilled`
  （最终位置是否等于请求），与 `alignment_kept` 互为否定，可自检。
* warning 文案改为「这是 FINAL 结果；若 `align_to_parent=False` 仍如此，说明本 build
  拒绝打断，需要界面手工移动」。

### P2-3 公差文字出 `-0`

`GetToleranceValues` 会给出 **-0.0**，`%+g` 打印 `-0`，而图面是 `0`
（GB 形式 `Ø30 +0.033/0`，`+0` 同样不对）。新增 `_signed_mm(value)`：
`|v| < 5e-7` → `"0"`，否则 `%+g`；三处 `display_text` 拼接全部改用它。

### P2-4 既有重复中心标记无法清理

`already_marked` 只阻止**继续新增**，一个被旧版本写成三个重叠标记的视图永远修不好。
新增 `dedupe_existing`（默认 `False`，因为会删标注）：

* 纯函数 `_coincident_mark_groups(rows, tol=1e-4)`：三轴都在 0.1 mm 内才算重复，
  保留每组第一个；相隔 5 mm 的两个真实孔位永不合并（只比 X/Y 会把纯 Z 差异误判）。
* `_dedupe_view_center_marks(doc, view)` 按位置 `Select(False)` + `EditDelete`。
* **先清理再统计**，`already_marked` 描述的是清理后的状态。
* 回执 `dedupe_existing` / `center_marks_removed` / `dedupe`：
  `removed:0` + `dedupe_existing:true` = 没有重复，与「没尝试」可区分。

### P2-5 0 个尺寸被判成覆盖率失败

`reconcile_ledger` 对导入实体返回 `0/0`，旧代码记 `ok:false` → 永久 pending，
verdict 永远停在「待设计审核」，而调用方在本工具集内无法修复它。
现在 `ledger_dimensions == 0` 记 `ok:true` + `status:"not_applicable"`；
正常分支标 `status:"checked"`，两者在 `checks` 里可区分。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 286/286（+4 条断言） |
| `smoke.py --only "position and tolerance receipts match the sheet"` | PASS |
| `smoke.py --only "coincident centre marks are grouped"` | PASS |
| `smoke.py --only "revolved part section verdict"` | PASS，20.6 s，`ledger_dims=0 status=not_applicable`（真机命中 not_applicable 分支） |
| `smoke.py --only "drawing chain" --only "reconcile ledger"` | 2/2 PASS（dedupe 回执 + delivery verdict 常态分支） |

### 附带修掉的测试缺陷

`0.8.49 center marks view` 用 `server[index:index + 1200]` 窗口查 `view_name=view_name`；
0.8.297 新增的长参数描述把转发调用挤出窗口，产生**假 FAIL**（代码是对的）。
改成按 AST 取函数体，不再依赖字符偏移 —— 与 0.8.294 的「断言不能锁死修复」同类：
**任何按字符窗口定位源码的断言，都会被无关的新增文本打破。**

---

## 2026-11-11（v0.8.298）`produce_drawing` 补 completeness，不给 verdict 加档（报告 §5 F）

### 问题

`produce_drawing` 的 `ok:false`（0.8.285 起表示「某步 partial」）只由
`partial_steps` 和 `callouts.verdict` 承载，顶层 `verdict` 仍是「待设计审核」、
`blockers: []`。报告 §5 认为只读这两项的调用者看不到「不可交付」，
建议把漏标塞进 `blockers`，或给 `verdict` 加 `partial` 档。

### 为什么两条建议都不采纳

* `verdict` 是**给人看的中文交付标签**，由 `delivery_check` 产生。让它同时编码
  「某步 partial」= 一个事实两个判据，项目已修过五次（`ok` vs `verdict`、
  ratio vs 绝对偏差、`sheet_projected` vs 可用边）。三档改四档还会打断所有下游。
* `blockers` 的语义是「**调用方在本工具集内能修的硬缺陷**」。孔标注漏标是
  COM/几何能力限制，塞进去会让每张有倒角歧义孔的图纸永久红灯，调用方无法通过
  任何操作清除 —— 与 0.8.297 刚修掉的「0/0 永久 pending」同型，只是更重。

### 改法

复用 `add_hole_callouts`（0.8.290）已有的词汇，新增三个字段，旧字段一字不改：

| 字段 | 值 | 含义 |
|---|---|---|
| `execution_ok` | `true` | 调用跑完了；`ok:false` + 它 `true` = 图纸不完整，**不要重试/回滚** |
| `completeness` | `complete` / `incomplete` | 与 `add_hole_callouts` 同名同义 |
| `incomplete_reasons` | `[{"kind":"blocker",…}]` / `[{"kind":"partial_step",…}]` | 为什么，且两类可区分 |

* 推导抽成纯函数 `_delivery_status(blockers, partial_steps)`，smoke 直接回放四分支。
* `blockers` 列表提到 `return` 之前只算一次（`_blockers`），**一份数据喂两处** ——
  两份必然漂移，B 就是这么坏的。
* `produce_drawing` 的工具描述写死字段分工：批处理读 `ok` / `completeness`；
  `verdict` 是给审核的人看的标签，不是机器信号。

### 验证

真机（装配体夹具）：

```text
ok = false
partial_steps = ["hole_callouts"]
execution_ok = true
completeness = "incomplete"
incomplete_reasons = [{"kind": "partial_step", "steps": ["hole_callouts"]}]
verdict = "待设计审核"      <- 未变
blockers = []               <- 未变
```

| check | 结果 |
|---|---|
| `check_all.py` | 287/287（+1 条断言） |
| `smoke.py --only "ok and completeness cannot disagree"` | PASS，五状态（0.8.299 起） |
| 真机 `pipeline.produce_drawing` | 上表：`completeness:"incomplete"`，`verdict`/`blockers` 未变 |

### 教训

**不要为了让一个字段「也说这件事」而改它的语义。** 补一个新字段的成本是一行回执；
改一个老字段的语义的成本是每一个调用方。

---

## 2026-11-11（v0.8.299）`ok` 与 `completeness` 同源（报告 2026-11-11 §5，P0）

### 问题：0.8.298 自己造了第六个「一个事实两个判官」

```python
ok=(not _partial_steps),                        # 只看 partial 步骤
**_delivery_status(_blockers, _partial_steps),  # completeness 还看 blockers
```

于是**有 blocker 但没有 partial 步骤**时，回执是
`ok:true + execution_ok:true + completeness:"incomplete"` —— 按 `ok` 判成败的批处理
会把「交付判定丢失」的图纸当成成功出图。四条可达状态里只有这一条不一致，
而真机恰好落在 `PARTIAL only`，矛盾被掩盖，287 条断言 + 61 条 check 全绿漏检。

三条真机可达路径：

| 路径 | 触发 | 0.8.298 结果 |
|---|---|---|
| A | `delivery_check` 返回 error dict（v0.8.229 的 `record()` 崩溃） | `ok:true` + `incomplete` |
| B | `do_hole_callouts=False` 且 `delivery_check` 报真实 blocker | 同上 |
| C | 任何**非 `_CRITICAL`** 步骤 `status="failed"` | 更糟：`ok:true` + `"complete"`（无人命名失败） |

### 改法

* 新纯函数 `_receipt_ok(status)`：`return status["completeness"] == "complete"`。
  **顶层 `ok` 不再是独立公式**，与 `completeness` 同一个来源，结构上无法分歧。
* `_delivery_status(blockers, partial_steps, failed_steps=())` 增加第三个 reason
  `kind:"failed_step"`，堵住路径 C；`_CRITICAL` 步骤不会进入（它们早退
  `critical_step_failed`）。
* 回执新增 `failed_steps`，让「哪个步骤崩了」可枚举。
* `produce_drawing` 的工具描述改写成字段优先级：`ok` **恒等于**
  `completeness == "complete"`（向后兼容别名）；新代码优先读 `completeness`；
  `incomplete_reasons[].kind` 区分 `blocker` / `partial_step` / `failed_step`；
  `execution_ok` 只管「要不要重试」。

### 验证

真机复现路径 A（monkeypatch `drawing.delivery_check` 返回 error dict）：

```text
ok = false
execution_ok = true
completeness = "incomplete"
incomplete_reasons = [
  {"kind": "blocker", "count": 1, "items": ["delivery_check 未完成，交付判定丢失：…"]},
  {"kind": "partial_step", "steps": ["hole_callouts"]},
  {"kind": "failed_step", "steps": ["delivery_check"]}]
failed_steps = ["delivery_check"]
verdict = "未判定：delivery_check 执行失败（…）"
```

| check | 结果 |
|---|---|
| `check_all.py` | 287/287（旧断言 `ok=(not _partial_steps)` 重写为「ok 跟随同一份推导」） |
| `smoke.py --only "ok and completeness cannot disagree"` | PASS：5 个状态逐一断言 `ok == (completeness=="complete")`，含 blocker-only 与 failed |

### 教训

**新增一个判据维度时，必须回头检查所有基于旧判据的字段。**
0.8.298 给 `completeness` 加 `blockers` 维度，却把 `ok` 留在只读 `partial_steps` 的
旧公式上 —— 两个字段各算各的，就是这一版自己 docstring 里写的 "one fact, two judges"。
守护也必须**把两个字段放进同一条断言**：分开断言时，两边各自都「正确」，组合却是错的。

---

## 2026-11-11（v0.8.300）清理步骤不是交付缺陷（报告 2026-11-11 §4/§5）

### 问题：0.8.299 的 over-correction

0.8.298 是「不完整却报成功」，0.8.299 补 `failed_step` 时只排除了 `_CRITICAL`，
于是**方向反过来**：一张已保存、审计通过、独立 `delivery_check` 也 `ok:true`、
有 17 条标注的完整图纸，仅因 `close_documents` 没关掉任何文档（`closed_count=0`）
就变成 `ok:false / completeness:"incomplete"`。调用者既不能交付、又被
`execution_ok:true` 告知「不要重试」—— 卡在无法收敛的死状态。

三个语义被串在一起，最后一环是错的：

| 层 | `close_documents` 的 ok 表示 |
|---|---|
| `close_document()` 返回 | 调用没抛异常 |
| `record()` 读成 | 这一步成功了吗（`closed_count > 0` 才算） |
| `_failed_steps` 再读成 | **图纸不完整吗** ← 错的 |

### 第二处：`delivery_check` 崩溃被双计

`verdict.get("ok")` 为假时，`_blockers` 合成一条「交付判定丢失」，同时
`record("delivery_check", …)` 记 `status="failed"`，`_failed_steps` 又收一次。
`incomplete_reasons` 里 `blocker` 和 `failed_step` 指向同一事件，按 `kind` 计数的
调用者会重复统计。

### 改法

* 新增 `_COMPLETENESS_RELEVANT` **白名单**（只列会写图纸的步骤）：
  `create_drawing` / `dimensions` / `center_marks` / `surface_finish` /
  `hole_callouts` / `save`。
  **用白名单不用黑名单**：以后每加一个辅助步骤都默认中性，不会再次漏掉。
* 新增纯函数 `_step_partition(steps, verdict_ok)` → `(relevant_failed, cleanup_failed)`。
  `delivery_check` 在「verdict 已丢失」时**只算 blocker**，不再进 `failed_step`。
* 回执新增 `cleanup_incomplete`，并把每条清理失败写进 `warnings`
  （`清理/辅助步骤失败（不影响图纸内容）：close_documents`）—— **可观测，但不污染
  `completeness`**。
* `critical_step_failed` 的 error 回执补 `execution_ok=True`：流水线确实跑完了，
  只是没产出可用图纸；字段缺失才表示「没跑到出回执」。工具描述同步写明这条。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 288/288（+1 条断言） |
| `smoke.py --only "step partition keeps cleanup out of completeness"` | PASS（纯函数：清理中性、写入步骤计数、`delivery_check` 只计一次、未知步骤默认中性） |
| `smoke.py --only "cleanup failure keeps the sheet deliverable"` | **PASS，99.2 s**（真机注入 `close_document` 返回 0 关闭）→ `ok:true / complete`，`cleanup_incomplete:["close_documents"]`，warnings 有记录，`drawing_audit` 仍 ok |
| `smoke.py --only "delivery_check crash is counted once"` | **PASS，107.7 s**（真机注入 error dict）→ `blocker` 1 条、`failed_step` 0 条、`verdict` 仍 `未判定…` |

### 教训

**「这一步失败了」和「图纸能不能交付」是两个问题。** 一个字段（`status`）承载
执行结果，另一个字段（`completeness`）承载交付判断；把前者直接喂给后者，
就必须先回答「这个步骤有没有资格影响图纸」。过滤条件要写成**白名单**：
黑名单永远落后于下一次新增的步骤。

---

## 2026-11-12（v0.8.301）补齐上一份反馈的两处边界（报告 2026-11-11 §6 / §8）

0.8.300 修了 P1-1 / P1-2 并加了两条真机 check，但逐条核对反馈时发现两处没做满。

### 1. `execution_ok` 只补了一个分支（§8 P2-1）

描述承诺「字段缺失 = 没跑到出回执」，实际只有 `critical_step_failed` 带
`execution_ok=True`；其余早退分支（`bad_input` / `output_exists` /
`output_trash_failed` / `ledger_failed` / `create_failed` / `restore_failed`）
没有任何 `execution_ok`，调用者 `d.get("execution_ok")` 仍是 `None`。

现在 `produce_drawing` 的**每一个** `error()` 都显式声明：

| 分支 | `execution_ok` |
|---|---|
| `critical_step_failed`（跑完 13 步但图纸不可用） | `True` |
| `bad_input` / `output_exists` / `output_trash_failed` / `ledger_failed` / `create_failed` / `restore_failed` | `False` |

字段从此永远存在；描述改成分支式说明，不再依赖「缺失的含义」。
回归断言用 AST 遍历 `produce_drawing` 内**所有** `error(...)` 调用，缺一个就红。

### 2. 真机 check 只看单字段（§6）

`partial reaches produce_drawing` 与 `assembly drawing not an empty shell` 都调
`produce_drawing`，却只断言 `ok` / `partial_steps`，从不比较 `ok` 与 `completeness` ——
这正是 0.8.298 那个 P0 能漏检的原因。两处都补上：

```python
_ok = payload.get("ok")
_complete = payload.get("completeness")
assert _ok == (_complete == "complete")
assert isinstance(payload.get("failed_steps"), list)
assert isinstance(payload.get("cleanup_incomplete"), list)
```

先绑定成变量再比较，避免触发「禁止裸 `assert payload.get("ok")`」的旧断言。

### 附带修掉一个过宽的断言

`0.8.295 assembly check accepts partial` 用子串
`'assert payload.get("ok")' not in body` 禁止真值断言，但任何**比较式**也会被它挡住
（0.8.301 新增的 `assert payload.get("ok") == ...` 就被判违规）。改成正则只匹配真值
断言 `assert payload.get("ok") <逗号>`。**过宽的模式和写错的正则一样会误杀正确代码。**

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 289/289（+1 条 AST 断言） |
| `assembly drawing not an empty shell`（真机） | PASS，250.0 s，3 views / 26 dims，新增组合断言已执行 |
| `partial reaches produce_drawing`（真机） | PASS，94.8 s，但 `not triggered`（默认夹具无歧义倒角）—— partial 分支断言仍未执行，需 `--part 限位导向套` |

---

## 2026-11-12（v0.8.302）正确的过轴全剖不再被判成偏心弦（报告 §2 P0）

### 问题

0.8.291 的偏心判据把「非 kept 维收缩」当**独立**拒绝条件。这对矩形源视图成立，
对**圆形源视图的竖剖**完全失效：

| 视图 | 源 `size_mm` | 剖视 `size_mm` | 判据结论 |
|---|---|---|---|
| 端视图（Ø64 圆）竖剖 offset=0 | `[127.98, 127.98]` | `[51.98, 127.98]` | ❌ 报 `section_off_axis`，`shrunk=76.0` |

宽度从 127.98 → 51.98 不是偏心，是**「圆的直径」被换成「零件的轴向长度」**。
同一份回执里 `eccentricity_mm: 0.0`、`kept ratio 1.0`、28 边 / hatch 2 都在说
「这是正确的过轴全剖」—— 判据却给出相反结论。

### 改法（三道守卫，按顺序）

1. **偏心量门控**：`SECTION_ECCENTRICITY_GATE_MM = 0.5`。
   `eccentricity_mm` 已知且 `<= 0.5` → 直接放行。过轴的平面物理上不可能产生弦剖。
   `null`（未知）不当作 0，继续走几何判据。
2. **kept 维**：不变（圆形源视图上，偏心缩短的正是这一维，所以这道守卫本来就有效）。
3. **off_axis 维**：新增「方形源视图跳过」——`source_size_mm` 两维相等（圆 / 正方形轮廓）
   时，这一对维度的语义在剖切后已改变，不可比。矩形源视图仍照旧比对。

附带修掉报告 §3 的五处回执缺陷：

| 项 | 改法 |
|---|---|
| 被拒时 `sanity.verdict` 仍 `"ok"` | 置为 `"section_off_axis"` 并附 `sanity.off_axis` |
| `off_axis.section_kept_mm` / `source_kept_mm` 装的是 **off-axis 维**的值 | 改中性名 `source_mm` / `section_mm` |
| 两个 `ratio` 同名不同义 | off-axis 分支的键改为 `ratio`，但只在 `off_axis` 对象内；`sanity.ratio` 不变 |
| hint 里手写弦长常数（63.5 → 63.48 两版都不准） | 由 `eccentricity_mm` + `source_mm / scale` **计算** `2·√(r²−e²)` |
| `off_axis_shrink_mm: -0.0` | 归零，不再出现负零 |

### 验证

真机（`小车底部滚轮`，`create_drawing(views=["right"])` + 竖剖 offset=0）：

```text
ok = true          (0.8.301: false / section_off_axis)
edge_count = 28    hatch_count = 2
eccentricity_mm = 0.0
sanity = {source_size_mm:[63.98,63.98], section_size_mm:[25.98,63.98],
          kept_dimension:"height", ratio:1.0, off_axis_shrink:38.0}
off_axis = null
```

| check | 结果 |
|---|---|
| `check_all.py` | 289/289 |
| `smoke.py --only "eccentric section is refused"` | PASS：过轴 + 圆视图放行、0.25 mm 弦仍拒、`ecc=0` 永不产生弦判定、未知偏心仍走几何判据 |

### 教训

**「这一维变短了」不等于「剖切面偏了」。** 判据必须区分「数值变化」与「语义变化」：
圆形视图竖剖后，一条直径被换成轴向长度，两维不再可比。
先用**测量得到**的偏心量做门控，再谈尺寸收缩 —— 把可靠的测量放在猜测前面。

---

## 2026-11-12（v0.8.303）角度尺寸不再被当成毫米（报告 §4 P0）

### 一个根因，三个症状

`IDimension.SystemValue` 对角度尺寸是**弧度**。四个读取点都乘 1000 当毫米：

| 读取点 | 0.8.302 输出 |
|---|---|
| `layout_dimensions` | `sample_moves[].text = "785.4"`（45° 被排版成 785 mm） |
| `reconcile_ledger` | `drawing_only_values: [785.4]` → `coverage 0.7`（图上其实一条不缺） |
| `verify_dimension_attachments` | `value_mm: 785.398, dimension_type: 3` |
| `drawing_dimension_values` | 把弧度值塞进「图上所有长度值」集合，喂给上面两个 |

`list_drawing_annotations` 早就正确（`value_deg: 45.0` + `value_mm: null`），
但它用的是自己那套 `(1, 3)` 判断；其他三处各写一个类型码，于是互相矛盾。

### 改法：一个谓词，四处共用

```python
ANGULAR_DIMENSION_TYPES = (1, 3)          # 这个 binding 上 1 和 3 都是角度

def _is_angular_dimension_type(dim_type):  # PURE
    ...

def _radians_to_deg(value_rad):            # PURE
    ...
```

* `drawing_dimension_values` **跳过**角度行 —— 它的两个调用者都在比长度。
* `verify_dimension_attachments`：类型已知后补 `value_deg`，并把 `value_mm` 置 `None`
  （名字带 `_mm` 的字段不该装度数）。
* `reconcile_ledger`：ledger 侧只接受 `kind in (0, 2)`；`3` 从「线性」移到
  `skipped_non_length` 并标 `reason: "angular"`。
* `layout_dimensions`：角度行的 `text` 由 `_radians_to_deg` 生成 `45°`，
  不再沿用弧度派生的 `785.4`，宽度估算也随之正确。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 290/290（+1 条断言） |
| `smoke.py --only "angular dimensions never become millimetres"` | PASS：`1`/`3` 判角度、`0.785398 rad → 45.0°`、`785.4` 不可作为长度泄漏 |
| 真机 `drawing chain` + `reconcile ledger` | 2/2 PASS（无角度尺寸的路径未被误伤） |

**未覆盖**：本机没有带角度尺寸的夹具（反馈者的 `边肋连接件` 不在仓库），
所以「45° 图面不再印 785.4」只在纯函数层验证。图面根治还需要
`add_angular_dimension`（创建/修正原生关联角度标注）—— 反馈列为 P1，本轮未做。

### 教训

**同一个事实的编码散在四处，就会有三处是错的。** 类型判断必须只有一个谓词，
而且这个谓词要被所有读者调用 —— 每处「顺手写一个 if」都是一次未来的分歧。

---

## 2026-11-12（v0.8.304）技术要求块不再静默压图（报告 §7 N-1）

### 问题：从「过保守拒绝」变成「静默压图」

0.8.290 是 `no_room_for_requirements`（称需 38 mm），调用方至少知道失败。
0.8.301 修好拒绝之后：

```json
{ "ok": true, "adjusted": false, "collisions_avoided": 0,
  "position_m": [0.0588, 0.287], "block_height_mm": 28.0 }
```

PDF 上标题与前两行正压在主视图的大圆弧和上方尺寸线上。原因是障碍集只有视图
`precise_outline`：块放在视图 bbox 上方 14 mm 就「合法」，而尺寸线、引出线、圆弧
恰好住在那条缝里。**从「明确失败」变成「成功但图面错误」，对无人值守批量出图更危险。**

### 改法

* 新增 `_dimension_text_boxes(doc)`：按 `layout_dimensions` 同一套估算
  （位置 ± 半宽 / 一个字高）为每个尺寸标注生成 bbox，**尺寸也算障碍**。
* 保持 `view_obstacles` 与 `dimension_obstacles` 两份：扫描时先试「视图 + 尺寸」，
  找不到再退化为「只避视图」，并回 `degraded_reason: "dimensions_ignored_no_room"`。
  这样既不退回 0.8.290 的硬报错，也不再静默压图。
* 回执新增三项：
  * `actual_collisions` —— 最终 box 真正交叠的障碍数（不是「挪了几次」）；
  * `overlap_with` —— 前 5 个交叠 box，便于定位；
  * `dimension_obstacles` —— 障碍集里有多少个尺寸文字框。
  `actual_collisions > 0` 时同时给 `warning`，`ok` 仍为 true（放置本身没失败，
  但调用方现在有信号）。

### 验证

真机（`door.SLDPRT`，front 视图 + 17 条尺寸 + 4 行技术要求）：

```text
ok = true
position_m = [0.0588, 0.287]
actual_collisions = 0
dimension_obstacles = 17      # 改前恒为 0：尺寸根本没进障碍集
degraded_reason = null
```

| check | 结果 |
|---|---|
| `check_all.py` | 291/291（+1 条断言） |
| `smoke.py`（`eng cases` 内的技术要求断言） | 新增 `actual_collisions` / `dimension_obstacles` 断言 |

### 教训

**「没有报错」不等于「放对了」。** 当避让的障碍集只覆盖一类对象时，另一类对象
必然会被压住，而回执仍然全绿。障碍集要按「图上所有会被文字遮住的东西」定义，
并且回执必须回报**实际交叠数**，而不是「挪动次数」。

---

## 2026-11-12（v0.8.305）守护能跑起来，白名单管住 partial（报告 §4 / §5 / §6）

### §4 两条真机守护在标准安装下零覆盖

`_cleanup_failure_keeps_the_sheet_deliverable` 与 `_delivery_check_crash_is_counted_once`
都用 `_require_fixture(_model_dir()/door.SLDPRT)`，而 `测试用例/` 整树被 `.gitignore`
排除、`SHEET_METAL_DIR` 没有 CLI 覆盖。标准 tarball 安装下：

* 直接跑 `smoke.py` → `missing test model` + `return 2`，**0 条 check 执行**；
* 传 `--part/--assembly` 让启动通过 → 两条全 `SKIP`，**退出码仍是 0**。

于是提交说明里的 "real receipts compare ok with completeness" 依赖的正是这两条，
而它们在用户机器上一条都跑不起来 —— 同一模式第七次出现。

改法：

* 两条 check 改用 `args.part`（启动时本来就必须提供），不再依赖钣金夹具。
* `Runner.summary()`：有 FAIL → 1；**有 run 但 0 passed → 3** + 醒目
  `NO CHECKS EXECUTED`；否则 0。让 CI 能区分「全 SKIP」与「全 PASS」。
* `check_all.py` 横幅改成 `N r.check() calls counted, 0 executed here` ——
  那是个字符串计数，不是可运行套件。

### §5 白名单只护 `failed`，`partial` 绕过

`_step_partition` 只处理 `status == "failed"`，而 `_partial_steps` 完全没过白名单。
同一个 `close_documents`：`failed` 态已被正确中性化，换成 `partial` 态就重新把完整
图纸判为 `incomplete`。反馈构造出 6 个这样的绕过（`close_documents` / `audit` /
`reconcile` / `clamp_dimensions` / `drawing_batch` / 未知辅助步骤）。

改法：一个分区同时处理两种状态，返回值改成 dict：

```python
{"failed": [...], "partial": [...], "cleanup": [...], "superseded": [...]}
```

只有白名单内的写入步骤进入 `failed` / `partial`；其余进 `cleanup`，仍进
`cleanup_incomplete` + `warnings`，但不再影响 `completeness`。

### §6 `delivery_check` 失败从两个列表里消失

0.8.300 为消除双计把它 `continue` 掉了，结果它既不在 `failed_steps` 也不在
`cleanup_incomplete` —— 想枚举「本次哪些步骤失败」的调用者必须去解析 14 项 `steps[]`
（含长 `detail`）。现在进 `superseded` 并在回执里给出：

```json
"superseded_steps": ["delivery_check"]
```

「只算一次」与「可以枚举」同时成立。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 292/292 |
| 全 SKIP 退出码（构造 1 SKIP + 1 not-selected） | `RC = 3` + `NO CHECKS EXECUTED`；有 PASS 时 `RC = 0` |
| 真机 `cleanup failure keeps the sheet deliverable` | PASS，69.9 s（现在走 `--part`） |
| 真机 `delivery_check crash is counted once` | PASS，64.8 s，`superseded_steps == ["delivery_check"]` |
| `smoke.py --only "step partition ..."` | PASS：`failed`/`partial` 两态都中性化，8 个未知辅助步骤全进 `cleanup` |

### 教训

**「守护存在」和「守护能跑」是两件事，而只有后者算覆盖。**
写 check 时要问「在一台没有我这些私有夹具的机器上，它会不会执行」；
再问「全 SKIP 时退出码是多少」—— 如果都是 0，那这个 check 在 CI 里等于不存在。

---

## 2026-11-12（v0.8.306）台账缺项不再点名已画出的尺寸（报告 §5 P1-1/2/4/5）

### P1-5 `ledger_missing` 把「归属不确定」当成「缺」

`reconcile_ledger` 早就把 `unmatched_ledger` 拆成两半：`missing_truly`（图上没有任何尺寸
带这个值）与 `present_but_unmatched`（值在图上，配对失败）。`delivery_check` 却把**整个**
unmatched 列表写成 `ledger_missing`，于是在 `边肋连接件` 上让调用方去补 30.0 ×2 / 5.0 /
15.5 / 9.0 ×2 / 15.0 —— 每一条都已经画在图上。

现在 `ledger_missing` 只来自 `missing_truly`，另一半独立发布：

```json
"ledger_unmatched_but_present": [{"value_mm": 30.0, "reason": "图上有同值尺寸，归属待确认 —— 不是缺项"}]
```

### P1-4 orphan：不降级，但把分布摊开

反馈建议「位置在视图附近的 orphan 降为 warning」。**这条不改** —— 0.8.27 已经实测过：
尺寸默认停靠在视图外 12 mm，所以几乎每个 orphan 都「在视图附近」，那次「修好」放过了
15 个真 orphan，同一份回执还声称「23 个尺寸均已关联」。判据本身已被证伪，不能重犯。

改的是可观测性：`dimension_attachments` 现在带
`near_view_orphans` / `far_from_view_orphans`，detail 里也说清「若 PDF 引线正常，
这些多为 `place_outside` 移动过的尺寸，不必删；远离视图的才需人工确认」。
orphan 仍是 pending —— 宁可让人看一眼，也不要让真缺陷悄悄通过。

### P1-1 `sheet=1` 报 `no sheet named '1'`

schema 声明 `string | integer`，Pydantic 把 `1` 转成 `"1"` 后 `isinstance(sheet, int)`
永远为假，于是合法的 1-based 索引被当成名字查。现在**先按名字**（真有一张叫 "1" 的图纸
就该赢），名字不存在且是纯数字才当索引。

### P1-2 `create_drawing.layout.columns` 数的是视图数

2×2 摆位被报成 `columns: 4`，`fits:false` 又与 `measure_layout` / `drawing_audit` /
`delivery_check` 三方矛盾。`columns` 改为按测量到的**不同左边缘**计数；`fits` 增加
`predicted: true` 与 `bounds_m`，让调用方看到判定用的是哪个框，而不是只给一个布尔值。

**未做**：让 `fits` 与 `measure_layout` 共用同一套判定仍是待办 —— 那需要把
「标注带」框与 `precise_outline` 的语义统一，改动面更大，留待下一轮。

### 验证

真机（`door.SLDPRT` 复制为 `sidxdoor`，避免同名文档冲突）：

```text
create: True
layout: {"fits": true, "columns": 2, "explicit": true, "measured": true,
         "predicted": true, "bounds_m": [0.0768, 0.083, 0.398, 0.275],
         "out_of_bounds": [], "overlaps": []}
add_sheet: True
sheet=1 -> ok | inserted: 24        # 0.8.305 报 no sheet named '1'
```

| check | 结果 |
|---|---|
| `check_all.py` | 294/294（+2 条断言） |
| 真机 `reconcile ledger` + `drawing chain` | 2/2 PASS |

### 教训

**「我没配上对」不等于「图上没有」。** 把「未匹配」直接翻译成「缺项」，会让调用方去补
已经画好的尺寸 —— 这类假阴性和假阳性一样贵，因为它消耗的是信任。

---

## 2026-11-12（v0.8.307）`layout.fits` 与 `measure_layout` 同源（报告 §5 P1-2）

### 同一张图，两个「已测量」的相反结论

| 工具 | 结论 |
|---|---|
| `create_drawing.layout` | `fits:false` / `out_of_bounds:[视图2, 视图4]` / `columns:4` |
| `measure_layout` | 4 视图全在框内，`min_margin_m: +0.010` |
| `drawing_audit` | `warnings: []` |
| `delivery_check` | `no_view_outside_sheet: ok` |

### 根因：两个工具用的不是同一个矩形

`create_drawing` 在放置前把安全区**内缩 `annotation_band_m`（默认 18 mm）**，然后用这个
更小的框判 `out_of_bounds`；`measure_layout` 用的是**未内缩**的模板安全区。

内缩本身是对的 —— 它给尺寸文字留空间。错的是把「视图落进了标注带」报成
「视图出界」。视图在安全区内就是没出界，只是没留够标注空间，这是两件事。

### 改法

* 内缩前保存 `_base_rect`（原始安全区），`fits` / `out_of_bounds` / `bounds_m` 全部改用它
  —— 与 `measure_layout` 同一个框。
* 更严格的那个问题独立成字段，不再混进 `fits`：
  `fits_annotation_band` / `annotation_band_out_of_bounds` / `annotation_band_bounds_m`。
* `columns` 上一版已改为按不同左边缘计数（2×2 报 2，不再是 4）。

### 验证

真机（`door.SLDPRT`，`view_positions=[[0.130,0.134],[0.320,0.134]]`，band 18 mm）：

```text
layout: {"fits": true, "fits_annotation_band": false,
         "out_of_bounds": [],
         "annotation_band_out_of_bounds": ["工程图视图1", "工程图视图2"]}
measure_layout.min_margin_m: 0.0153
```

`fits:true` 与 `min_margin_m:+0.0153` 一致（0.8.306 会报 `fits:false`）；
标注带不足由 `fits_annotation_band:false` 单独表达，调用方知道该留空间还是该改布局。

对照：把视图再往下放 6 mm（`y=0.095`）时两边同时报出界 ——
`fits:false` + `measure_layout.min_margin_m: -0.0237`，说明真出界时仍然一致。

| check | 结果 |
|---|---|
| `check_all.py` | 295/295（+1 条断言） |

### 教训

**两个工具都标着「measured」，不代表它们测的是同一件事。** 判定框不同时，布尔值必然
打架；要么共用同一个框，要么把更严格的那条判据拆成独立字段并说明它更严格。

---

## 2026-11-12（v0.8.308）新增 `add_angular_dimension`：原生关联角度尺寸（报告 §4.4）

### 之前只能「删掉再打字」

`add_drawing_dimension` 的文字槽写不出来，`auto_dimension` 只能搬运模型已有的尺寸。
所以想把 45° 标到图上，唯一办法是 `delete_annotation` 删掉原生标注、再加一条文字 ——
图面上有「45°」，但它不再与模型关联，改模型不会跟着变。

### 做法：`AddDimension2` + 类型校验 + 撤销

`add_distance_dimension` 早已证明「选两条边 → `AddDimension2`」可用。角度是同一机制，
区别只在**必须校验结果的类型**：两条边平行时，同一个调用会安静地产出一个**长度**尺寸。

```
edge_a.Select2(False, 0) → edge_b.Select2(True, 0) → AddDimension2(x, y, 0)
→ GetDimension2(0).GetType ∈ (1, 3)?  是 → value_deg；否 → 撤销并返回 not_angular
```

* 类型不对时**真的删掉**：`GetAnnotation` → `Select(False)` → `EditDelete` → `EditRebuild3`，
  回执 `created:false / reverted:true / dimension_type`。图上留一条「看起来像角度」的
  长度标注，比没有更糟。
* 回执 `value_deg`（度数）+ `value_mm: null` + `value_is_angular: true`。
* `expect_deg` 可选：给了就比对，`verified` 三态（`true` / `false` / `null`=未校验）。
  `ok` 跟随 `verified is not False` —— 创建成功但角度不对时 `ok:false`，调用方必须处理。

同时修掉 `add_distance_dimension` 的同类 bug：两条非平行边产生角度尺寸时，它原来报
`value_mm: 785.398`。现在给 `value_deg` 并把 `value_mm` 置 null —— 这正是
`layout_dimensions` 那条 `785.4` 的上游。

### 验证

| check | 结果 |
|---|---|
| `static_check` | `OK (72 tools, ... README in sync, every parameter documented)` |
| `check_all.py` | 295/295 |
| `stdio_contract.py` | 6/6，`seal={'registered': 72, 'sealed': 72, 'ok': True}` |
| 真机 `angular dimension is native or refused` | PASS，39.0 s，`refused: not_angular` |

真机这一次选中的两条边**平行**，所以命中拒绝路径：`not_angular` + `reverted:true`，
`value_mm` 为 null。**成功路径（`ok:true` + `value_deg`）本轮未在真机命中** ——
需要在有明确夹角的两条边上复测，已列为待办。

### 教训

**「能创建」和「创建对了」必须分开断言。** `AddDimension2` 在平行边之间不会报错，
它会给你一个长度；不校验类型就报成功，等于把「选错了边」伪装成「标好了角度」。

---

## 2026-11-12（v0.8.309）新增 `export_sketch_geometry`：草图逐点几何（报告 §3，最后一个 P0）

### 先探测，再写代码

这个 binding 上「草图几何」有一半能读、一半不能。动手前逐项实测：

| API | 结果 |
|---|---|
| `sketch.GetSketchSegments` | ✅ 段列表（late-bound 可读） |
| `sketch.GetSketchPoints` | ✅ 每个点 `X/Y/Z`（草图坐标） |
| `sketch.GetSketchContours` | ✅ `contour.IsClosed` |
| `seg.GetType` | ✅ `0` = 直线，`1` = 圆弧 |
| `seg.GetLength` | ✅ 米 |
| `seg.GetCurve` → `LineParams` | ✅ `[ox,oy,oz, dx,dy,dz]`（原点 + 单位方向） |
| `seg.GetCurve` → `CircleParams` | ✅ `[cx,cy,cz, ax,ay,az, radius]` |
| `sketch.ModelToSketchTransform` | ❌ `DISP_E_MEMBERNOTFOUND` |
| `sketch.GetReferenceEntity` | ❌ 需要参数 |
| `ISketchLine.GetStartPoint2` | ❌ 派生接口对 late binding 不可见 |

所以：**直线端点可精确算**（`origin`、`origin + direction × length`）；**圆弧没有端点**，
只有圆心/轴/半径/长度。

### 圆弧端点从点集反解，不唯一就报 null

弧长 `L` 与半径 `r` 定包角 `θ = L/r`，弦长 `2r·sin(θ/2)`。草图点集里同时满足
「到圆心距离 = r」且「两点距离 = 弦长」的那一对就是端点。纯几何、可复算。

反解不唯一（或最近候选差超过弦长 2%）时返回 `start_m: null` / `end_m: null`，
`endpoints_source` 写原因 —— **一个错的端点会让整份重建模型全错**，猜不得。

### 基准面 frame 明确 null

`plane_frame` 恒为 `null`，配 `plane_frame_reason` 与顶层
`plane_frame_available: false`：坐标是**草图局部坐标**（平面草图 Z 恒 0），
基准面 origin/xdir/ydir 必须回界面确认。这是「拿不到就说拿不到」，不是省略。

### 回执

```json
{"ok": true, "units": "m", "sketch_count": 17, "plane_frame_available": false,
 "sketches": [{"name": "草图1", "segment_count": 9, "point_count": 13, "closed": true,
   "segments": [
     {"index": 0, "kind": "arc", "type": 1, "length_m": 0.034502,
      "center_m": [0.11448, -0.08625, 0.0], "axis": [0.0, 0.0, 1.0],
      "radius_m": 0.0885, "sweep_deg": 22.336,
      "start_m": [0.113405, -0.174749, 0.0], "end_m": [0.147118, -0.168519, 0.0],
      "endpoints_source": "matched_from_sketch_points"},
     {"index": 4, "kind": "line", "type": 0, "length_m": 0.865091,
      "start_m": [-0.7517, -0.1748, 0.0], "direction": [1.0, 0.0, 0.0],
      "end_m": [-0.113391, -0.1748, 0.0], "endpoints_source": "line_params"}]}]}
```

### 验证

| check | 结果 |
|---|---|
| `static_check` | `OK (73 tools, ... README in sync, every parameter documented)` |
| `check_all.py` | 295/295 |
| 真机 `sketch geometry is exact and honest` | **PASS，97.6 s**：17 个草图、**235 条直线段逐条验证 `end == start + direction × length`**、8 条圆弧 `radius > 0` 且都有端点溯源、`plane_frame` 全为 null |

### 教训

**先探 API，再写功能。** 这个 binding 上「看起来该有」的方法有三分之二是
`MEMBERNOTFOUND`；凭文档写完再调试，等于把每一处不可用都变成一次静默失败。
探测脚本本身就是交付物的一部分 —— 它决定了回执里哪些字段能给值、哪些必须写 null。

---

## 2026-11-12（v0.8.310）两个「已知未覆盖」的收口

### `add_angular_dimension` 的成功路径：已真机验证

0.8.308 的真机只命中了拒绝分支（所选两条边平行），所以「能创建原生角度」这件事
其实**没有被验证过** —— 一个只有失败路径覆盖的新工具等于没覆盖。

在 `P05_karetku` 上逐对试边：

| 边对 | 结果 |
|---|---|
| (0, 1)（两条圆边） | `not_angular`，`reverted: true` |
| (2, 3)（两条直线） | **`ok`，`value_deg: 12.631`，`dimension_type: 1`，`reverted: false`** |

两条路径现在都被固定进 `smoke.py` 的 `angular dimension is native or refused`：
先试已知可成的 (2,3)，失败再退化到其它组合；**若该夹具一对都不成，返回
`the SUCCESS path was NOT exercised` 而不是绿行**。

实测回执：`native angle 12.631 deg (type 1)`，37.9 s。

### 基准面 `plane_frame`：补探 6 条路径后结案

初版只试了 3 个名字就说「读不到」，不够。补探结果：

| API | 结果 |
|---|---|
| `ModelToSketchTransform` | `DISP_E_MEMBERNOTFOUND` |
| `GetSketchTransform` / `Get3DSketchTransform` / `GetSketchPlane` | 成员不存在 |
| `GetReferenceEntity2` | 成员不存在 |
| `GetReferenceEntity(0/1/True)` | `DISP_E_TYPEMISMATCH` —— 需要 out 参数，pywin32 传不了 |
| `feature.GetDefinition` | 返回 `None` |
| `Extension.GetCoordinateSystemTransformByName("")` | 返回 `None`（模型无坐标系） |

**结论：这是 binding 的硬边界，不是实现缺陷。** `plane_frame` 保持 `null` +
`plane_frame_reason` + 顶层 `plane_frame_available: false`，坐标为草图局部坐标。
继续在上面投入只会把「拿不到」包装成「看起来拿到了」。

### 教训

**「未覆盖」分两种：实现漏了，和 API 拿不到。** 前者的正确动作是补测试，
后者的正确动作是**把边界写进回执然后停止投入**。混在一起谈会让后者永远挂在
待办列表上，看起来像欠债，实际是天花板。

---

## 2026-11-12（v0.8.311）最后两条 P2：分类置信度 + 符号编码已生效（报告 §7 N-3 / N-4）

### N-3 `plan_drawing` 忽略传入 `views`，分类过度自信

两个独立问题：

1. `sheets[0].views` 硬编码 `["front", "right"]`，而同一份回执的 `suggested_views`
   回显调用方传入的 4 个 —— 传 4 个得到 2 个的计划，且不解释。
2. 一个 3 实体 S 形弯板（46.5 × 30 × 120，全无圆柱面）被判成 `轴类`，
   `delivery_check` 又把这个标签当事实复述。

改法：

* 计划直接用调用方的 `views`；新增 `views_requested` / `views_planned` / `views_omitted`
  三个字段，让「要了什么 / 规划了什么 / 丢了什么」可对账。
* `_classify` 改为返回 `(category, reason, confidence)`：`FlatPattern` 命中是**事实**
  （`high`），其余全部来自三个包围盒数字（`low`），且 `axis` 分支的 reason 追加
  「仅基于包围盒长径比，未验证是否回转体 —— 结论仅供参考」。`plan_drawing` 与
  `build_feature_ledger` 都透出 `category_confidence`。

**没有**加「圆柱面占比」判据：`plan_drawing` 用 `include_faces=False` 是为了性能，
为此新开一次面扫描会把一个已知误标换成另一个未知误标。低置信度标签 + 说明，是这里
正确的成本点。

### N-4 `°` 的警告读起来像「还要你自己改」

`_encode_note_symbols` 其实**已经**把裸字符替换成 SolidWorks 格式码（`°` → `<MOD-DEG>`），
但 warning 写的是 `replace_with: "<MOD-DEG>"` + 「raw char becomes a \U+ escape」，
调用方读不出「已经改好了」还是「建议你改」。

现在每条 warning 带 `applied: true` 与 `dwg_export`（`%%c` / `%%p` / `%%d`），
reason 明确写「已自动替换……无需手工处理」；回执另给
`text_sanitized: true` / `text_sanitized_count`，一个布尔就够判断。

### 验证

真机：

```text
plan_drawing(P05, views=["top","front","right","isometric"])
  category=轴类  category_confidence=low
  reason=长径比 5.6、中/短比 1.9（仅基于包围盒长径比，未验证是否回转体 —— 结论仅供参考）
  views_requested=['top','front','right','isometric']
  views_planned  =['top','front','right','isometric']   omitted=[]
  sheets[0].views=['top','front','right','isometric']    # 0.8.310 是 ['front','right']

add_note("倒角 C2（2x45°），外径 Ø30 ±0.1")
  ok=True  text_sanitized=True  text_sanitized_count=3
  Ø -> <MOD-DIAM>  applied=True  dwg=%%c
  ± -> <MOD-PM>    applied=True  dwg=%%p
  ° -> <MOD-DEG>   applied=True  dwg=%%d
  实际写入：倒角 C2（2x45<MOD-DEG>），外径 <MOD-DIAM>30 <MOD-PM>0.1
```

| check | 结果 |
|---|---|
| `check_all.py` | 296/296（+1 条断言） |

### 教训

**「建议」和「已完成」必须用不同字段表达。** 一条 warning 如果既可能是待办也可能
是回执，调用方只能去猜；`applied: true` 比任何措辞都省事。

---

## 2026-11-12（v0.8.312）公差渲源自证不再假阴性 + `views` 三件套同源（报告 §2 / §4-§7）

### §2 P0：正确的双边公差被判成基本尺寸

`_tolerance_text_on_pdf` 用「带符号文本」判形状。但 SolidWorks 导出 PDF 时把公差的
`+`/`-` 写成**独立文本对象**，一份正确的 `Ø14.20 +0.050 / -0.050` 到解析器里是三行裸数字：

```
'14.20'
'0.050'
'0.050'
```

于是 `deviation_count: 0` → `basic_no_deviation` → `pdf_render_matches: false` +
警告「readback 是对的，但印出来的不是这个公差」。**假阴性会诱导调用方去"修"一张正确的图**，
方向比假阳性更危险。

改法：

* **按数值判形状**：把每行解析成量值（去掉 `+`/`-`/`±` 前缀后 `float()`），与请求的上下偏差
  比对。符号是否同行不再参与。
* **窗口从 3 行高放宽到 6 行高**，并在窗口内找不到偏差时**回退到全页**（`basis:
  "magnitudes_full_page_fallback"`）。回退也只比数值 —— 技术要求正文里的 `H7` 解析不出 float，
  不会顶替尺寸。
* 回执新增 `numeric_deviation_count` / `basis` / `evidence`（`numeric_magnitude` 或 `sign_text`），
  让调用方知道这个结论有多强。

真机（`door` 建图 + 0.20 mm 尺寸 + `bilateral ±0.05`）：

```text
shape = "bilateral_two_deviations"     # 修前 basic_no_deviation
basis = "magnitudes_in_window"
evidence = "numeric_magnitude"
numeric_deviation_count = 1
deviation_count = 2
lines = ["0.20", "0.050", "0.050"]     # 两个偏差现在都在窗口内
pdf_render_matches = True              # 修前 false
```

### §4-§7 `views` 三件套：一个字段三个答案

| 问题 | 改法 |
|---|---|
| `views_omitted` 按「请求 − 计划」算，而计划就是请求 → **恒空** | 逐个校验：非法名进 `omitted` 且标 `unknown_view`，重复标 `duplicate`，都不进 `sheets[].views` |
| `views=None` → `suggested_views=list(views)` 抛 **TypeError** | 三个字段全部取校验后的列表 |
| `views=[]` → `suggested_views: []` 与 `views_planned: [front,right]` 自相矛盾 | 同上，三者同源 |
| 只有 AST 断言，行为覆盖 0 | 新增**行为断言**：mock 掉 COM 台账读取，真调 `plan_drawing` 验 5 种输入 |

视图名白名单复用 `drawing.VIEW_ALIASES`（`create_drawing` 用的同一张表），**不另写一份** ——
第二份白名单一定会漂移。

行为断言实测覆盖：4 视图全采纳、`None` 不崩且回默认、`[]` 三字段一致、`bogus_view` 进
`omitted` 且带 `unknown_view`、`(front,front)` 去重且标 `duplicate`。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 298/298（+2 条断言，其中一条是行为断言） |
| 真机 `add_dimension_tolerance(verify_render=True)` | `shape: bilateral_two_deviations` / `matches: True` |

### 教训

**判据不能依赖「渲染器怎么摆字符」。** 符号、数字、单位分属不同文本对象是导出器的自由；
能稳定比较的是**数值**。同样，一个字段如果按构造永远只能是同一个值，那它不是字段，
是常数 —— 应该要么真算，要么删掉。

---

## 2026-11-12（v0.8.313）`delete_dimensions` 一条都删不掉（报告 D2 / D6 / D7）

### D2：`rows` 恒空，所以 `removed: 0 / failed: 0`

收集循环把 `IAnnotation` 传给 `_dimension_measure`，而后者第一句是
`display.GetDimension2(0)` —— **`GetDimension2` 在 `IDisplayDimension` 上，`IAnnotation` 没有**。
调用抛异常，被循环外层的 `except: continue` 吞掉，于是 `rows` 永远是空列表：

```
removed: 0    failed: 0    remaining_matches: [4 条角度尺寸]
```

用户看到的「一条都删不掉」，实际是**一条都没尝试**。而存活扫描用的是正确对象
（`ann.GetSpecificAnnotation`），所以它照常列出 4 条残留 —— 两个字段各说各的。

修法：收集时先 `win.Dispatch(ann.GetSpecificAnnotation)`，与存活扫描同一路径。
真机（`door` + 17 条尺寸，`keep_values=[]`）：

```text
ok = true    removed = 17    failed = 0    remaining_matches = []    verified_clean = true
```

（修前 `ok:false / removed:0 / failed:0 / remaining 4`。）

同时按反馈建议补两处：`remaining_matches` 现在带 `name` + `position_m` + `unit`，
可直接喂给 `delete_annotation(name, position_m)` 兜底；角度尺寸本来就走删除分支
（`unit == "deg"` 永不被 `keep_values` 保留），这一点无需改。

### D6：`add_view_to_sheet(sheet=2)` 报 `no sheet named '2'`

与 `_activate_sheet_if_given` 同一个成因：schema 声明 `string | integer`，Pydantic 把 `2`
转成 `"2"`，`isinstance(sheet, int)` 永不命中。改成「先按名字、再按纯数字索引」，并补上
原来缺失的 `else`（名字不存在且非数字时明确报 `sheet_not_found`）。

真机：`add_view_to_sheet(sheet=2)` → `ok`。

### D7：`cylinder_faces_in_model: 0` 让人以为模型没有圆柱面

`delivery_check` 默认 `include_faces=False`（走面要 10 倍时间），台账因此从不遍历面，
`holes` 为空，这个数就报 0。一个实测有 22 个圆柱面的零件被报成 0。

现在：`include_faces=False` 时该字段为 `null`，并给
`cylinder_faces_status: "not_checked"`；只有真数过才报数字。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 300/300（+2 条断言） |
| 真机 `delete_dimensions(keep_values=[])` | `removed: 17 / verified_clean: true` |
| 真机 `add_view_to_sheet(sheet=2)` | `ok` |

### 教训

**「计数为 0」有三种含义：没有对象、没有尝试、没有权限。** 一个 `except: continue`
就能把「没尝试」伪装成「没有」，而旁边那段用正确对象的扫描会照实报出残留 —— 两个字段
互相矛盾，却都「有数据」。计数器的分母必须和动作本身同源。

---

## 2026-11-12（v0.8.314）一批口径项：零候选不是唯一匹配、检查消失要说出来（报告 §3/§4-4/§5）

### §5 `no_geometry` 的 gap 报「唯一匹配 / high」

置信度阶梯的最后一级是裸 `else`：

```python
elif _n_matches > 1:   medium
else:                  high / "唯一匹配"     # ← 0 次匹配也落这里
```

于是 `candidates: []` 的 gap（根本没找到边对）被描述成「唯一、高置信度」。
**「没有候选」和「恰好一个候选」是相反的事实。**

改：`elif _n_matches == 1` 才 `high`；`0` 给 `confidence: "none"` +
`reason: "没有候选边对（与「唯一匹配」相反）"`。

### §4-4 `hole_callouts` 检查整项消失

`record("hole_callouts", ...)` 整块挂在 `if hole_candidates:` 下面。默认
`include_faces=False` → 台账不数圆柱面 → `hole_candidates == 0` → **整个检查不进 `checks`**。
上一轮「`ok:true` 但 0 个孔标注对象」的矛盾，以「删掉检查」的方式消失了。

现在补 `else` 分支：`ok: True` + `status: "not_checked"` + 明确写「传 `include_faces=True`
才会跑这项检查」。**检查可以没跑，但不能不在。**

### §3 能力表引用了不存在的 `datum_feature`

`geometric_tolerance.why` 连着三轮写「详见 `datum_feature`」，而能力表里只有
`datum_feature_symbol` —— 解释限制的那句话把读者指向了空处。

改：指向 `datum_feature_symbol`；该条目 `summary` / `why` / `workaround` 三处都写明
「`y_m` 生效、`x_m` 由所选边决定，先读 `position_honoured`」。
`status` 仍是 `verified`（符号本身确实可用、GB 圆形确已实测），因为误导来自那句
「不需要 workaround」，而不是来自 `status`。

新增断言：扫描能力表里所有 `详见 \`xxx\``，每个都必须是一个真实 `id` —— 这类悬空引用
不能再出现。

### `produce_drawing` 描述补齐四个桶

`partial_steps` / `failed_steps` / `cleanup_incomplete` / `superseded_steps` 的语义
原先只写在代码注释里，工具描述只提到 `incomplete_reasons`。现在描述里逐桶点名，
`superseded_steps` 特别说明「已折进 blocker，只算一次但仍可枚举」。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 303/303（+3 条断言） |
| `capability references resolve` | 全部 `详见` 引用可解析 |

### 教训

**「计数为 0」和「唯一」是同一条 `else` 的两端。** 只要置信度阶梯以裸 `else` 收尾，
零值就会继承最高的那一档结论。阶梯要逐值显式，末档要留给真正的「其他」。

---

## 2026-11-12（v0.8.315）新页图框：定位到根因，三次尝试无效，回执改诚实（报告 D1）

### 复现与定位（`door` 造两页 A2）

| 页 | 渲染结果 | `GetTemplateName()` |
|---|---|---|
| 第 1 页（`create_drawing`） | 图框 + 标题栏表格齐全 | `v:/updated cs\a2 - gb.slddrt` |
| 第 2 页（`add_sheet`） | **只有分区字母**，无图框无标题栏 | `...\sheetformat\a2`（无扩展名、文件不存在） |

第 2 页图幅是对的（`[0.594, 0.42]` = A2），所以 `paper` 代码生效、**模板没生效**。
`NewSheet4` 的 `TemplateName` 要的是**图纸格式 `.slddrt`**，而工具传的是**文档模板 `.drwdot`**
—— SolidWorks 拿它拼了个不存在的 `sheetformat\a2`。

### 三次尝试，全部无效

| 尝试 | 结果 |
|---|---|
| `TemplateName = *.drwdot`（原实现） | 尺寸对，**无图框** |
| `TemplateName = *.slddrt`（`a2 - gb.slddrt`，文件确实存在） | 尺寸对，**仍无图框** |
| `SetTemplateName(.slddrt)` + `ReloadTemplate(True)` | `ReloadTemplate` 返回 **0**，`GetTemplateName` 不变 |

**关键坑**：`GetTemplateName()` 回显的是**请求值**，不是实际加载的格式 ——
第三种尝试里 `SetTemplateName` 明明没生效，读回却像是生效了。所以这个 API **无法**回答
「图框到底套上没套上」。

### 结论与改法

这是 **API 边界**，不是实现缺陷：本 build 上 `NewSheet4` 创建的页拿不到图纸格式。

回执改为不撒谎：

```json
"template": "a2 - gb.slddrt",
"template_kind": "sheet_format",
"sheet_format_applied": null,
"template_applied": null,
"frame_and_titleblock_verified": null,
"note": "…图框与标题栏未验证，且本 build 上大概率没有…这页可以写视图和尺寸，
          但不能作为正式交付页 —— 交付前必须导出 PDF 目视，或继续用
          「多个 .SLDDRW + 外部合并」。"
```

旧版 `template_applied: true` 正是「图幅对就等于模板对」的误判来源。

### 验收口径建议（给调用方）

`add_sheet` 之后**不要**用 `template_applied` 判断能否交付。要么导出 PDF 目视，
要么继续沿用「多个 `.SLDDRW` + 外部合并」。`add_view_to_sheet` / `activate_sheet` /
`auto_dimension(sheet=…)` 这条多页链路本身是可用的（本机实测第 2 页写入 17 条尺寸、
`placed_outside: 17`）。

### 教训

**一个 API 如果「写进去什么、读出来就是什么」，它就不能用来验证自己有没有生效。**
`SetTemplateName` + `GetTemplateName` 正是这种自证循环；这种字段必须标 `null`，
或者用**外部证据**（导出 PDF）判定，绝不能拿它当 `ok`。

---

## 2026-11-12（v0.8.316）对外置的角度尺寸：不移动（报告 D3）

### 为什么「把尺寸挪开」会造出界弧线

`place_outside` 的逻辑是「找到所属视图 → 按最近边分层外推」。对**线性尺寸**这是对的：
文字往外挪，引线只是变长一点。

但对**角度尺寸**，引线是绕顶点、按该角自己的半径画的**弧**。文字外推会把弧拉长 ——
半径大的时候弧直接越过图框。反馈里那条「左下角大圆弧 + 斜引线 + 角度文字从图框外侵入」
就是这个：文字位置在框内，弧在框外。

`place_outside` 本身**已经有安全区约束**（`outside_spills` 记录「分层受安全区限制」），
所以这不是「没检查边界」，而是「被移动的对象本身就带着一段跨界的几何」。

### 改法：角度尺寸不参与外置

```python
if _is_angular_dimension_type(_annotation_dimension_type(annotation, win)):
    debug["skipped_angular"].append({"name": name, "position_m": [...]})
    continue
```

理由：SolidWorks 自己的放置会把弧贴在几何旁边，比外移安全得多。
**一个没被移动的尺寸，不可能被移动到图框外。**

回执新增 `outside_debug.skipped_angular`（每条含 `name` + `position_m`），
`notes` 里也会写「N 条角度/弧长尺寸未移动」。调用方据此知道哪些尺寸保持原位。

### 真机验证（`接槽.SLDPRT`，A2 四视图）

```text
create: True
auto_dimension(include_unmarked=True, place_outside=True)
  inserted: 33   total: 23   placed_outside: 21
  skipped_angular: [
    {"name": "D3", "position_m": [0.2822, 0.3116]},
    {"name": "D2", "position_m": [0.2599, 0.2620]}]
```

导出 PDF（54 506 B）→ `render_drawing(scale=2.0)` 目视：

* 图框 + 右下角标题栏齐全；
* **框外没有任何线条、圆弧、引线或文字**（四周干净，分区字母在框内）；
* 两处 45° 角度尺寸都留在原位，弧贴在几何旁，没有再被拉到远处。

修前这两条正是被外置推出去、弧跨出图框的那一类。**D3 关闭。**

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 305/305（+1 条断言） |
| 真机 `auto_dimension(place_outside=True)` | 走通，`outside_debug` 含 `skipped_angular`（空，无角度尺寸） |

### 教训

**「把东西挪到外面」只对点状标注成立。** 带自身几何的标注（角度弧、引线、符号）移动时，
几何会跟着形变 —— 位置合法不等于图元合法。判断这类改动要先问「它的几何会怎么变」。

---

## 2026-11-12（v0.8.317）最后三项口径收尾（报告 §4-1 / §5-2 / §5-3）

### §4-1 `drawing_audit.sheet_annotations` 变 `null`

多页改造后，这个字段从稳定的 `83` 变成 `null` —— 因为按页遍历的循环不再走到 sheet view，
没人给它赋值。现在直接从 `doc.GetFirstView` 读，不再依赖那个循环。

真机（接槽 A2 四视图）：`sheet_annotations: 94`、`view_count: 4`。

### §5-2 `set_title_block` 的 `note_name` / `cell_position_m` 恒 `null`

`_note_name` 里 `_call_method(note, "GetAnnotation").GetName` 没做 dispatch，读不到就返回
`None`，于是两个字段一起静默为空。改为先 `win.Dispatch` 再取 `GetName`（属性/方法两种形态都兼容）。

真机：

```text
field=material  before='Q235B'  note_name='细节项目256'  cell_position_m=None
cell_position_reason='该格子的注解位置读不到（模板限制），用 `cell_slot_m` 定位'
```

`cell_position_m` 在这个模板上确实读不到（注解位置 API 的已知边界），但它不再是一个
**没有解释的 null** —— `cell_position_reason` 说清原因，`cell_slot_m` 给出可用定位点。

顺带确认 `dry_run` **不会**改文件：调用前后 `mtime` 与字节数完全一致。第一次测到的
`before` 从空变成 `Q235B`，是**读取能力**被修好，不是 dry-run 泄漏。

### §5-3 `add_dimension_tolerance` 参数单位

参数级描述已是 `in MILLIMETRES (e.g. 0.05 for +0.05)`，全仓库无 `in metres` 残留。
本轮无需改动，记录在此以免下一轮重复核对。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 306/306（+1 条断言） |
| 真机 `drawing_audit` | `sheet_annotations: 94`（修前 null） |
| 真机 `set_title_block(apply=False)` | `note_name='细节项目256'`（修前 null），文件 mtime/大小不变 |

### 教训

**一个 `null` 至少要能回答「为什么是 null」。** 三处字段的修法不同 —— 一处是读错了对象、
一处是没 dispatch、一处是 API 边界 —— 但对调用方来说都是「空」。不给原因的空，
和撒谎只差一个字段名。

---

## 2026-11-12（v0.8.318）`delivery_check` 默认链路崩溃 + 公差护栏假阳性（报告 §4 / §5 / §6）

### §4 P0：默认路径 100% 崩溃，交付判定全丢

**这是 0.8.314 引入的回归。** 为修「孔标注检查消失」，我在 `else` 分支里写了
`diameter_dimensions=diameter_dims`，而 `diameter_dims` 只在 `if hole_candidates:` 内部赋值：

```python
if hole_candidates:
    diameter_dims = 0        # ← 唯一赋值点
    ...
else:                        # 0.8.314 新增
    record("hole_callouts", ..., diameter_dimensions=diameter_dims)   # UnboundLocalError
```

`include_faces=False` 是**默认值** → `hole_candidates == 0` → 必然走 `else` → 崩。
`delivery_check` 返回 `ok:false / verdict:null / checks:{}`，`produce_drawing` 花了 20 多秒
却什么都判定不了。

修法：`diameter_dims = 0` 提到 `if` 之前；`else` 分支的 `diameter_dimensions` 改为 `null` +
`diameter_dimensions_status: "not_checked"` —— 没数过就不能报 0。

真机：

```text
delivery_check(model, drawing)            # 默认 include_faces=False
  ok: true   verdict: 待设计审核   blockers: 0   pending: 3   checks: 16
  hole_callouts: {status: "not_checked", diameter_dimensions: null,
                  detail: "孔标注未核对：include_faces=False…"}
```

smoke 端到端（`drawing chain` + `reconcile ledger`，默认参数）：
`delivery ok=True, verdict='待设计审核'`，381.6 s。

**守护的教训**：0.8.314 为此加的那条断言只查源码里有没有 `status="not_checked"` 字符串 ——
AST 断言**不可能**发现同一分支里的 `UnboundLocalError`。新增的是**顺序断言**
（`diameter_dims = 0` 必须在 `if hole_candidates:` 之前）+ smoke 的行为覆盖。顺序断言仍是
静态检查，真正确认「默认路径不崩」的是 smoke 那次真机调用。

### §4.4 逃生开关：`produce_drawing(include_faces=False)`

原先 `produce_drawing` → `delivery_check` 是硬编码默认值，调用方唯一出路是绕开一站式出图。
现在 `produce_drawing` 有 `include_faces` 参数，直通 `delivery_check`。默认仍是 `False`
（便宜），需要孔覆盖判定时显式打开。

### §5 P1：公差护栏可被「页面别处的同值数字」满足

0.8.312 把形状判定改成按量值，解决了假阴性，但引入假阳性：任意一个等于偏差值的数字
（另一条尺寸、粗糙度说明、未注公差行）都能把 `basic_no_deviation` 抬成
`bilateral_two_deviations`，于是 `render_matches: true` —— 恰好放行了这道护栏要抓的那类事故。

两处收紧：

1. **x 带从 ±6 行高收到 ±2.5**：公差堆叠与它的主值在同一列，页面别处的 `0.05` 通常不在。
2. **整页回退时 `evidence = "uncertain"`**，且 `render_matches` 记 **`null`** 而不是 `true`：

```python
_uncertain = (pdf_probe.get("evidence") == "uncertain"
              or pdf_probe.get("basis") == "magnitudes_full_page_fallback")
render_matches = None if (not checked or not _want or _uncertain) else _shape == _want
```

`null` + warning（"图面公差形状未能确定…请导出 PDF 目视核对"）。**猜出来的形状不配当通过。**

### §6 P2：`flat_pattern` 与 `views_omitted_reasons`

* `flat_pattern` 不在 `VIEW_ALIASES`（那张表驱动 `create_drawing` 建视图形状），但这个 planner
  自己会给钣金件追加一页 `views: ["flat_pattern"]` —— 同一份回执既判它「未知视图」又把它
  写进计划。现在计划层用 `set(VIEW_ALIASES) | {"flat_pattern"}`，不动 `create_drawing` 那条路。
* `views_omitted_reasons` 以视图名为 key，`("bogus","bogus")` 会 2 条 omitted / 1 个 key。
  新增 `views_omitted_detail`（list of `{view, reason}`）与 `views_omitted_count`。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 309/309（+3 条断言） |
| 真机 `delivery_check`（默认参数） | `ok:true / verdict:待设计审核 / checks:16` |
| 真机 smoke `drawing chain` + `reconcile ledger` | 2/2 PASS，`delivery ok=True`，381.6 s |

### 教训

**「加个 else 分支」最容易踩的坑，是 else 里引用了 if 才有的东西。** 这条错误连
`static_check` / `regressions` / `check_all` 三套全绿都拦不住，因为静态检查看不到
「值是否被绑定」。任何跨分支共用的名字，必须在分支之前初始化 —— 这不是风格，是正确性。

---

## 2026-11-13（v0.8.319）标题栏比例没写进去，就不能说 ok（报告 §1 P0 / §2.2 / §2.3）

### §1 三层回执都说 ok，图面仍是 1:1

反馈在 SW2026 的 `gb_a3.drwdot` 上测到：视图 `scale=0.5`，标题栏仍写 1:1，
而 `create_drawing` / `set_title_block` / `produce_drawing` 全报 `ok: true`。

根因不是新 bug，而是**失败已经记录、结论没有跟着降级**：

```python
_title_scale_synced = {"text": "1:2",
                       "error": "set_title_block did not confirm the write",
                       "written": []}          # ← 失败在这
result.update(title_scale_synced=_title_scale_synced, ...)
return ok(**result)                             # ← ok 仍是 True
```

而这文件自己对 `save_not_written` 写过准则：「`ok:true` + `saved:false` 是同一个谎」并真的
`return error(...)`。标题栏同步走的是另一条路。

三处改：

| 位置 | 改法 |
|---|---|
| `set_title_block` | `apply=True` 且 `written == []` → `error("title_block_not_written", ...)`，带上 `planned` / `unmatched` |
| `create_drawing` | `title_scale_synced.error` 非空 → `ok=false` + `error: "title_scale_not_synced"` + `message` + 一条 `blockers`（图仍在，只是不能直接交付） |
| `produce_drawing` | `creation_warnings` 加 `{"issue": "title_scale_unsynced", ...}`，一键入口不再沉默 |

**真机（接槽 A3，`scale=0.5`）**：本机模板的比例格能被读到，同步**成功**
（`written: [{field: scale, before: "1:2", after: "1:9", ok: true}]`），所以降级分支没被触发。
单独验证零写入分支：`set_title_block(drw, apply=True)` 不传任何字段 →
**`ok:false / error: title_block_not_written`**（修前是 `ok:true`）。

**未在失败模板上复现**：反馈的 SW2026 模板取不到比例 note（82 个 note / 0 个 ratio candidate），
本机 SW2024 的 `gb_a3` 能取到。结论按反馈的 caveat 表述：**该能力依赖模板把比例存成 `INote`**，
已在 `check_capabilities` 新增 `title_block_scale: partial`。

### §2.2 `add_distance_dimension` 把角度当距离报成功

两条边互相垂直时，`AddDimension2` 生成的是**角度尺寸**，工具却 `ok:true` + `value_mm: null` +
`note` 自称「这是两条边之间的距离」。

现在：`_is_angular_dimension_type` 命中 → `error("angular_pair_not_a_distance", ...)`，
`value_deg` 一起给，并提示「换平行的两条边，或改用 `add_angular_dimension`」。
尺寸保留在图上（它是真实的），只是结论不再冒充距离。

### §2.3 `produce_drawing` 失败时什么都不解释

`ok:false` 回执没有 `message` / `error` / `hint`，调用方 `payload.get("message")` 得到 `None`。

现在失败时：`error: "delivery_incomplete"`、`message` = 第一条 blocker、`hint` 说明
`blockers` / `pending` / `warnings` 三者语义不同。成功时这三个字段为 `null`。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 312/312（+3 条断言） |
| 真机 `set_title_block(apply=True)` 零字段 | `ok:false / title_block_not_written` |
| 真机 `set_title_block(scale="1:9", apply=True)` | `ok:true`，`before 1:2 → after 1:9` |

### 教训

**「把失败记下来」和「让失败影响结论」是两件事。** 0.8.318 已经让
`title_scale_synced.error` 可见，但只要顶层 `ok` 不动，一键入口的调用方永远看不到它 ——
这就是「记录」和「判定」之间的那道缝。同一个文件里对 `save_not_written` 早就写对了，
这次只是把同一条规则用到隔壁分支。

---

## 2026-11-14（v0.8.320）一个字段删掉了整条流水线（报告 §4 P0 / §5 / §6）

### §4 P0：我上版把「比例没写进去」升级成 ok:false，忘了管道第一行就查 ok

0.8.319 的意图是对的（诚实地承认没写进去），但 `produce_drawing` 的第一道守卫是：

```python
if not created.get("ok"):          # ← 它问的是「图纸建出来了吗」
    return error("create_failed", ...)
```

`ok:false` 被当成了「什么都没产出」，于是在 GB 表格/块模板上（`title_block_scale: partial`
正是我上一版自己写的条目）：

```
produce_drawing → ok:false / error:create_failed / execution_ok:false
                  verdict:null / steps:null / warnings:null
                  两个已打开文档泄漏（提前 return 跳过 close_when_done）
```

**比我上一轮批评的那个 P0 更严重**：那次 13 步都跑完了，只是判定丢失；这次一步都没跑。

而且同一版为「转发真实原因」新加的 `title_scale_unsynced` 警告，写在那个 `return` **之后** ——
它只在「`ok:true` 且带 error」这种 0.8.319 刚刚消灭的旧形状下才执行。**修上一个版本的问题的代码，
防不住自己这一版新造的问题。**

修法三步：

1. `create_drawing` 在降级时补 `blocking: false` —— 「图纸建出来了，只是标签错了」和
   「什么都没产出」是两件事，一个布尔量说不清。
2. `pipeline` 把守卫抽成纯函数 `_creation_is_blocking(created)`：
   只有 `not ok` **且** `blocking` 不是 false 才短路；未知形状一律短路（不在「也许」上继续）。
3. `creation_warnings` 的收集（含 `title_scale_unsynced`）**提到守卫之前**；
   真短路时也补上 `close_when_done` 的清理，不再漏文档。

### §5 公差护栏又有两条路假通过

0.8.318 只把 **fallback** 那条路降级了，另两条仍判 `render_matches: true`：

| 路径 | 0.8.318 | 现在 |
|---|---|---|
| 无 charbox、整页直扫（`basis: magnitudes_full_page`） | `true` ❌ | `null` ✅ |
| 同列窗口内的无关 `0.05` | `true` ❌ | 窗口内仍可能 `true`，但这条只能靠「绑定到该尺寸的文本组」根治 |

改成**按证据来源判定**：只有 `basis == "magnitudes_in_window"` 才算确定，其余一律 `null` + warning。
「窗口内同列」那条挡不住，已如实记在上面的表里。

### §6 `message` 在最常见的失败形态下是占位串

0.8.319 的 `message` 只取 `_blockers[0]`，而最常见的失败是「某步骤 partial、无 blocker」——
于是回执写着「完整性不足，见 incomplete_reasons」，真正的原因（`hole_callouts` 因两个倒角配对
直径跳过）就在 `partial_steps` 里。抽出 `_failure_message()`，按
`blockers → failed_steps → partial_steps → incomplete_reasons` 依次取具名原因。

真机：`message: "步骤部分完成：hole_callouts（详见 partial_steps / pending）"`
（修前是「完整性不足，见 incomplete_reasons」）。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 314/314（+2 条**行为**断言） |
| 真机 `produce_drawing(接槽, A3)` | `execution_ok:true`、`verdict:待设计审核`、14 步全跑、`message` 具名 |
| `_creation_is_blocking` 六种输入 | 行为断言覆盖：`blocking:false` → 继续；真失败/未知 → 短路 |

### 未做（本轮 P2，记入待办）

* `angular_pair_not_a_distance` 回执不给尺寸 `name`/`FullName`，hint 里建议的
  `delete_annotation(name, ...)` 无法直接执行。
* `produce_drawing` 没把 `delivery_check` 的 `hole_callouts.status` 与生效的 `include_faces`
  透传到顶层 —— 开了开关也看不出跑没跑。
* `produce_drawing` 的 docstring 未补 `include_faces` / 比例语义。

### 教训

**「加一个失败标记」要先问「谁在读这个标记，它读得懂吗」。** `ok` 被三个地方当三种意思用：
`create_drawing` 用它说「全成了吗」，`pipeline` 用它说「还值得继续吗」，调用方用它说「能用吗」。
一个布尔量承担三种语义，改动任何一处都会在另外两处炸。分开之后（`ok` / `blocking` / `verdict`）
才各说各的。

---

## 2026-11-14（v0.8.321）三条 P2 收尾（报告 §7）

### §7.1 `angular_pair_not_a_distance` 的 hint 无法执行

回执写着「该角度尺寸已保留在图上，可直接用 `delete_annotation` 删掉」，而
`delete_annotation(drawing_path, name, ...)` 要的是 **name**，回执里一个都没有 —— 调用方只能重新
枚举全部注解找它。

现在错误回执带 `annotation_name`（读 `GetAnnotation().GetName`，属性/方法两种形态都兼容）。

真机：`ok:false / error:angular_pair_not_a_distance / annotation_name:"RD2" / value_deg:90.0`。

### §7.2 逃生开关开了也看不出来

`include_faces` 接通了，但 `produce_drawing` 的顶层回执在 `False` / `True` 两种配置下**完全一致** ——
调用方花了 10 倍时间，却无法确认孔覆盖判定到底跑没跑。

现在顶层带两个字段：

```json
"include_faces": false,
"hole_callouts_check": {"status": "not_checked", "hole_callout_objects": 4,
                        "diameter_dimensions": null,
                        "diameter_dimensions_status": "not_checked", "covered": null}
```

开 `include_faces=True` 时 `status` 会变成 `checked`、`diameter_dimensions` 有数字、
`covered` 有真假。

真机（默认）：`include_faces: false` + `status: "not_checked"`。

### §7.3 工具描述未同步两个新参数

`produce_drawing` 的 docstring 补了两段：`include_faces` 与孔覆盖判定的关系
（不传则 `not_checked`，「没有孔标注」= 「没测」而不是「测了是 0」），以及非 1:1 图纸的比例未写入
会以 `title_scale_unsynced` 出现在 `creation_warnings` 里、交付前必须手工补填。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 314/314 |
| 真机 `add_distance_dimension`（成角的边对） | `annotation_name: "RD2"` |
| 真机 `produce_drawing`（默认） | `include_faces:false`、`hole_callouts_check.status:"not_checked"` |

### 教训

**一条建议如果指向一个需要参数的动作，那个参数必须在回执里。** 否则「可直接删掉」只是一句话，
不是一条出路 —— 调用方还是得自己翻一遍。

---

## 2026-11-14（v0.8.322）第二道门：短路从「进门」挪到了「出门」（报告 §4 P0）

### 现象

0.8.320 修了第一道守卫（`_creation_is_blocking`），14 步真的全跑完了、文档也没泄漏 ——
但顶层仍是：

```
ok:false / error:critical_step_failed / "the drawing is not usable"
verdict:null / steps:null / creation_warnings:null / completeness:null
```

`message` 说「图纸不可用」，而 `hint` 里自己列着 14 步全跑完、`delivery_check=True`。

### 根因：`record()` 与 `_failed_critical` 不知道 `blocking`

```python
# 1) record() 只看 payload["ok"] -> step 记成 ok:False / status:"failed"
# 2) _CRITICAL = ("create_drawing", "dimensions", "save")
# 3) _failed_critical -> ["create_drawing"] -> :677 提前 return，判定全丢
```

`blocking:false` 只走到了第一道门。**同一个非阻断事件在流水线末尾被重新判成致命。**

### 改法（A + B + C 三条一起）

| # | 改法 |
|---|---|
| **A** | `record()` 抽成纯函数 `_step_state(payload)`：`blocking:false` → `status: "warning"` + `non_blocking: true`；`_failed_critical` 抽成 `_failed_critical_steps(steps, critical)`，跳过 `non_blocking` |
| **B** | 比例未同步**进 `_blockers`**：`ok:false` 由同一个 `_receipt_ok(_status)` 派生，`execution_ok` 保持 true，`verdict` / `completeness` / `incomplete_reasons` 全部保留 —— 缺陷反映到结论，但不删除结论 |
| **C** | `critical_step_failed` 的 return 补 `verdict` / `steps` / `creation_warnings` / `warnings` / `blockers` / `completeness` / `incomplete_reasons` —— 它在整条链**之后**执行，这些值确实存在，丢掉它们只是遗漏 |

C 的第一次插入我放错了位置（放在第 3 个位置参数 `hint` 之前），
`SyntaxError: positional argument follows keyword argument` 立刻暴露 —— 这类错误编译器会拦，
上一版的语义错误则不会。

### 顺带

* **§5**：`magnitudes_in_window` + `evidence="numeric_magnitude"` 也降为 `render_matches=None` ——
  窗口内同列的无关数字（粗糙度、另一条尺寸的偏差）仍然能凑出形状，只有**同窗口内的符号证据**
  才算确定。`None` 既不是通过也不是失败。
* **§6.1**：`hole_callouts` 在**已核对**时补 `status: "checked"` /
  `diameter_dimensions_status: "checked"` —— 之前已核对时这两个字段是 `null`，读起来像「没读到」。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 315/315（+1 条行为断言） |
| 真机 `produce_drawing(接槽, A3)` | `execution_ok:true`、`verdict:待设计审核`、**`steps:14`**、`blockers:[]`、`error:delivery_incomplete` |
| `_step_state` / `_failed_critical_steps` | 行为断言：非阻断 → `warning`/`non_blocking` 且不进 critical；真失败 → 进 |

### 教训

**一个标记要贯穿整条链路，而不是只喂给第一个读它的地方。** `blocking:false` 在第一道门被读懂、
在最后一道门被忽略，等于没写。这次的修法是把「这个失败严不严重」的判定抽成**两个纯函数**，
让两处读同一份逻辑 —— 只要有第二个地方自己写 `if not ok`，它就会再次分叉。

---

## 2026-11-14（v0.8.323）带符号的公差被当成裸数字，护栏恒 null（报告 §6）

### 现象

`add_dimension_tolerance(verify_render=True)` 对**印对了**的双边公差返回
`pdf_render_matches: null`：

```
shape: bilateral_two_deviations
evidence: numeric_magnitude        ← 明明是 +0.050 / -0.050
basis: magnitudes_in_window
pdf_render_matches: null           ← 应为 true
```

而 0.8.322 的 warning 还自相矛盾：「证据来自**整页文本**（basis=`magnitudes_in_window`）」。

### 根因：符号在分类前就被丢掉了

| 步 | 位置 | 发生了什么 |
|---|---|---|
| 1 | `_magnitudes_of` | `lstrip("+-\u00b1")` 把 `+0.05` 剥成 `0.05`，与粗糙度 `0.05`、比例值 `0.05` 在数值上完全一样 |
| 2 | `_num_up` | 于是裸数值匹配为真 |
| 3 | 判定顺序 | `elif _num_up:` 排在 `elif _has_up or _has_lo:` **之前** → `sign_text` 对双边公差**不可达** |
| 4 | 0.8.322 的 `_uncertain` | 一律拒绝 `numeric_magnitude` → 真公差也判 `null` |

护栏本身没错，错在它读的那个标签永远不会是 `sign_text`。

### 改法：幅值带上「原本有没有符号」，判定按证据强度排序

```python
def _magnitudes_of(lines):        # 返回 (值, 是否带符号)
    ...
    return out, signed

_signed_hit = any(flag and (值等于上偏差 或 下偏差) for 值, flag in ...)

elif _signed_hit or _has_up or _has_lo:      # 带符号 → 可信
    shape, _evidence = "bilateral_two_deviations", "sign_text"
elif _num_up or _num_lo:                     # 裸数字 → 仍不确定
    shape, _evidence = "bilateral_two_deviations", "numeric_magnitude"
```

`_uncertain` 一行没改：真带符号的走 `sign_text` → `matches=true`；裸数字、整页回退仍是 `null`。
**修好假通过和恢复真通过可以同时成立，不是二选一。**

warning 文案也改成按 `basis` 分支：窗口内写「尺寸窗口内的裸数字」，整页才写「整页文本」。

### 顺带（P2 / P3）

* `record()` 把 `payload["error"]` 存进 step；`_failed_critical_steps` 的 `non_blocking`
  豁免**收窄到 `create_drawing` + `error == "title_scale_not_synced"`** ——
  `save` 磁盘满、`dimensions` 无驱动尺寸即使标了 `blocking:false` 也照样阻断。
* `produce_drawing` 描述补上真实步数（14）与 `status="warning"` / `non_blocking` 的语义。

### 验证

| check | 结果 |
|---|---|
| `check_all.py` | 317/317（+2 条断言，含符号证据顺序检查） |
| 真机 `add_dimension_tolerance(80.0, bilateral ±0.05, verify_render=True)` | `evidence: sign_text`、`basis: magnitudes_in_window`、**`matches: True`** |

### 教训

**「降级到不确定」是最容易过头的修法。** 0.8.322 把一整类证据判死，连带把正确的也判死；
真正的修法是**把证据分级**（符号 > 裸数值 > 整页），而不是把不确定的档位连到可信档位上。
判据的分支顺序就是优先级本身 —— 写反了，最强的那条分支就永远走不到。
