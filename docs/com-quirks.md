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

## 八、一条通用原则

> **先读现值，再改。**
> 凡是"多层属性"（外观、显示状态、配置、材质），第一步读一次目标对象的现值，它会直接告诉你该改哪一层。跳过这一步，靠猜 API 语义，返工成本是它的 10 倍。
