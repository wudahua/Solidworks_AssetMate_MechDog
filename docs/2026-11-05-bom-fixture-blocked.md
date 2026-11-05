# 自造 BOM 夹具：装配体通了，`add_bom_table` 仍然失败

**日期**：2026-11-05（第二轮）
**状态**：**夹具已能造出来**；`add_bom_table` 在它上面**仍然失败**，原因未定

## 一、突破了什么

`AddComponent5` 一直返回 `False`，`ActivateDoc3` / `ActivateDoc2` 报「类型不匹配」。
试过路径、两种配置选项、`InsertComponent`（方法根本不存在）。

**真正的原因是两个，都不是参数**：

### ① 零件必须先 `OpenDoc`，再传**文档标题**

```python
pa = sw.OpenDoc(path_a, 1)                      # 必须先打开
asm = sw.NewDocument(TPL_ASM, 0, 0, 0)
sw.ActivateDoc(asm.GetTitle)                    # 旧版 ActivateDoc，不要 ActivateDoc2/3
act.AddComponent5(pa.GetTitle, 0, "", False, "", 0, 0, 0)   # ← 标题，不是路径
```

- 传**路径** → `False`
- 传**已打开文档的标题** → `True`，组件数 +1

实测：

```
add block_a.SLDPRT -> True
add block_b.SLDPRT -> True
comps: 2 | saved: True
```

### ② `ActivateDoc2` / `ActivateDoc3` 在 pywin32 上类型不匹配

```
ActivateDoc    -> True
ActivateDoc2   -> ERR (-2147352571, '类型不匹配。', None, 3)
ActivateDoc3   -> ERR (-2147352571, '类型不匹配。', None, 4)
```

参数里需要 `VARIANT`（`Errors`/`Warnings` 之类），`None` 不行。
**直接用旧版 `ActivateDoc(title)`** 就够了。

### ③ `EditRebuild3` 在这个绑定上是属性

```python
act.EditRebuild3()      # TypeError: 'bool' object is not callable
```

去掉括号即可。`com-quirks.md` 已记过同类陷阱。

## 二、零件构建（同样已验证）

```python
TPL_PART = r'C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\gb_part.prtdot'
NULL = sw_session.byref_dispatch_null()      # ← Callout 必须是 byref dispatch

doc = sw.NewDocument(TPL_PART, 0, 0, 0)
doc.Extension.SelectByID2("前视基准面", "PLANE", 0, 0, 0, False, 0, NULL, 0)
doc.InsertSketch2(True)
doc.SketchManager.CreateCornerRectangle(-w/2000, -h/2000, 0, w/2000, h/2000, 0)
doc.ClearSelection2(True)
doc.FeatureManager.FeatureExtrusion2(
    True, False, False, 0, 0, 0.005, 0.005, False, False, False, False,
    0.0174532925199433, 0.0174532925199433, False, False, False, False,
    True, True, True, 0, 0, False)
doc.SaveAs(path)
```

```
part A: plane=True  extrude=True  bodies=1 saved=True
part B: plane=True  extrude=True  bodies=1 saved=True
```

**模板路径不能靠 `GetUserPreferenceStringValue(196/197/198)`** —— 本机三个全返回空字符串，
必须自己找 `.prtdot` / `.asmdot`。

## 三、现在卡在哪

夹具齐了，链路走到最后一步：

```
comps: 2 | saved: True
drawing ok=True | views: 1（工程图视图1，非空）
bom ok=False
```

```
error: bom_table_failed
message: InsertBomTable4(Indented) returned None [sw_version=32.4.0]
```

**`sw_version = 32.4.0` 就是 SW2024 SP4** —— 也就是能力表里写着「仅 SW2024 SP4 验证」的那个版本。
**所以这不是"版本不对"**，而是别的原因。

## 四、下一步查什么

1. **装配体是否需要在保存前 `Rebuild`** —— 我只 `SaveAs` 了，没重建过。视图能看到两个零件
   （`views: 1` 非空），但 BOM 读的是配置/属性树，可能需要一次真实重建。
2. **`InsertBomTable4` 的第一个参数** —— 代码传的是 `Indented`；能力表提到
   「两步法：Indented → TopLevelOnly」。也许 `Indented` 在**没有子装配**时会返回 None，
   应该先试 `TopLevelOnly`。
3. **视图的 `ReferencedConfiguration`** —— 若为空，BOM 可能拿不到配置名。
4. **零件是否需要 `Description` / `PartNumber` 自定义属性** —— 空属性可能让行生成为 0，
   但那应该返回一个空表而不是 None。

## 五、为什么还没写进 smoke

**夹具已经能造出来了，但 BOM 用例现在会 FAIL。**

写进 smoke 的后果：`add_bom_table (native, populated)` 从 SKIP 变成 **FAIL**，
而 **FAIL 的真实含义（"工具在这个装配体上确实不行"还是"夹具缺了某个前置"）还没查清**。

**在查清之前写进去，等于用一个不确定的红换掉一个诚实的灰。**

先把夹具构建能力记在这里：**它已经可用**，上面三段代码可以直接搬进 smoke，
剩下的只是 BOM 那一步。

## 六、相关

- `tests/smoke.py:529` `_bom_table`
- `docs/com-quirks.md` 第三节（`SelectByID2` 的 Callout）
- `src/.../tools/capabilities.py` `bom_table` 条目（「仅 SW2024 SP4 验证」）
