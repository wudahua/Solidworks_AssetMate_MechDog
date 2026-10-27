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

## 十三、一条通用原则

> **先读现值，再改。**
> 凡是"多层属性"（外观、显示状态、配置、材质），第一步读一次目标对象的现值，它会直接告诉你该改哪一层。跳过这一步，靠猜 API 语义，返工成本是它的 10 倍。
