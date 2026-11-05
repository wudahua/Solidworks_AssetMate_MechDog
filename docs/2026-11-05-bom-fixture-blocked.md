# 自造 BOM 夹具：走通了零件，卡在装配体

**日期**：2026-11-05
**目标**：让 `add_bom_table` 的真机用例不再 SKIP 已经 6 轮

## 为什么值得做

`add_bom_table` 是**唯一出过「签名断链、100% 必炸」事故的工具**（v0.8.2）。
那次事故能活到发布，正是因为它的真机用例一直是 SKIP（夹具 `SAB1_0000_000.SLDASM`
在 `测试用例/`，而该目录整树 gitignore）。修好之后**用例还是 SKIP** ——
等于修完之后仍然没有覆盖。

## 走通的部分（已验证）

```python
TPL_PART = r'C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\gb_part.prtdot'
NULL = sw_session.byref_dispatch_null()      # ← 关键

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

实测结果（两个不同尺寸的方块）：

```
part A: plane=True  extrude=True  bodies=1 saved=True
part B: plane=True  extrude=True  bodies=1 saved=True
```

**两个零件都能正常运行。**

### 两个坑（都已解决，值得记）

1. **模板路径**：`GetUserPreferenceStringValue(196/197/198)` 在本机**全返回空字符串** ——
   默认模板没设。必须自己找 `.prtdot`（本机在
   `C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates\gb_part.prtdot`）。
2. **`SelectByID2` 的 Callout 参数**：传 `None` 抛「类型不匹配」。
   必须用项目里的 `sw_session.byref_dispatch_null()`（`VT_BYREF | VT_DISPATCH`）——
   这个坑 `docs/com-quirks.md` 第三节早就写了，我照抄了文档才没再踩第二次。

## 卡住的部分

装配体里插零件，试了三条路都不通：

| 调用 | 结果 |
|---|---|
| `asm.AddComponent5(path, 0, "", False, "", 0, 0, 0)` | 返回 **False**，`GetComponents` = 0 |
| `asm.AddComponent(path, 0, 0, 0)` | 未走到（上一条没抛异常，只是返回 False） |
| `asm.ActivateDoc3(title, False, 0, 0)` | **类型不匹配**（参数类型问题） |
| `asm.InsertComponent(doc)` | 未测到（卡在 ActivateDoc3） |

`NewDocument(TPL_ASM)` 本身是成功的（拿到「装配体1」），`SaveAs` 也成功 ——
**卡在"往装配体里放组件"这一步**。

## 下一步怎么试（按可能性排序）

1. **`IAssemblyDoc.AddComponent5` 的 `ConfigOption` 不是 `0`** —— 查 `swAddComponentConfigOptions_e`，
   可能应该传 `1`（`swAddComponentConfigOptions_CurrentSelectedConfig`）而不是 `0`。
2. **先 `ActivateDoc`（旧版，非 `ActivateDoc3`）** —— `ActivateDoc3` 的第二个参数可能需要
   `VARIANT`，试 `sw.ActivateDoc(title)`。
3. **用 `InsertComponent` 但先 `SelectByID2` 一个基准面** —— 有些版本要求有插入点。
4. **完全换路子**：`sw.OpenDoc` 打开一个**已有的最小装配体**（项目 `测试用例/` 里有 140 个
   `.SLDASM`，虽然 gitignore，但**作者本机有**），在测试里断言「本机存在则用、不存在则 SKIP」——
   **但这只解决作者本机，不解决发布包**，等于没修。

## 为什么没有强行写进 smoke

写一半进主线的代价：**多一条可能自身就坏的用例**。
`_bom_table` 现在的 SKIP 是**诚实的**（`FixtureMissing` + 说明），
换成一条"造夹具也造不出来"的 FAIL 会更糟。

**零件构建部分可以随时捡起来用** —— 它在上面，已验证。
剩下的只有「装配体插组件」一步。

## 相关

- `tests/smoke.py:529` `_bom_table` 的夹具查找
- `tests/_fixtures.py` 夹具解析（0.8.158 加的）
- `docs/com-quirks.md` 第三节 `SelectByID2` 的 Callout 坑
