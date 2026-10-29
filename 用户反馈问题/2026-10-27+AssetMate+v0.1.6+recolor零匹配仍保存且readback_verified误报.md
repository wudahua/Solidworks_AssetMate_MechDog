# 2026-10-27 AssetMate v0.1.6 验收反馈：recolor 零匹配时仍保存文档且 readback_verified 误报 true

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0，验收时正在运行）· Python 3.13.12 (64-bit) · mcp 1.30.0 · WorkBuddy MCP 客户端
> 验收对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（2ccfcac，v0.1.6）
> 结论：**v0.1.6 验收通过**。上轮三项反馈全部修复到位；本轮发现 1 个边界逻辑缺陷（建议修复）+ 1 个坑文档补充观察。

---

## 一、上轮反馈修复核验（全部通过 ✅）

| 上轮问题 | v0.1.6 修复方式 | 本机实测结果 |
|---|---|---|
| **核心**：`get_mass_properties` 无默认密度披露，与 BOM 口径不一致 | 新增 `mass_basis` + `average_density_kg_m3` 字段；mass/volume ≈ 1000 kg/m³ 时明确标记 `suspected default density` 并提示核实材料分配 | 本机实测（7 无材料零件装配）：`average_density_kg_m3: 1000.0`，`mass_basis: "suspected default density: mass/volume = 1000.0 kg/m3, which matches SolidWorks' fallback for parts without material — verify material assignments before using this mass"` ✅ 正是建议方案 |
| `assembly.py` docstring 仍写 "Experimental" | docstring 已按转正口径重写 | ✅ |
| `get_sw` 裸对象兜底无提示 | 兜底时 `logging.warning` 说明早绑定风险与症状特征 | 代码确认 ✅（本机 `dyn.Dispatch` 正常，未触发该路径） |

**回归验证**：握手 `serverInfo.version == "0.1.6"` ✅，8/8 工具 ✅，not-found 错误信息保持带原因 ✅，editable 元数据 0.1.6 ✅。

## 二、v0.1.6 新功能在本机（= 失真条件现场）的验证

com-quirks 第十一节描述的颜色读回失真条件（gen_py 缓存 + SW 2026）**正是本机环境**。只读探测打开中的装配（10 组件）：

| 组件 | MaterialPropertyValues 读回 |
|---|---|
| T1_Pieza1-1 | `[0.847, 0.2, 0.2]`（红，读回正常） |
| 2-1 / 2-2 / 3-2 | `[1.0, 1.0, 1.0]`（白，但渲染图中蓝/黄零件确被正确渲染） |
| 6-2 / P2_1-1 / 6-1 / 5-1 / 4-2 / 4-1 | **`None`** |

对 `preview_after_reload.png` 的像素聚类分析确认渲染中存在 ~RGB(24,64,195) 蓝、~RGB(240,168,0) 黄、~RGB(240,48,48) 红等饱和色块——**"渲染正确但读回失真"在本机真实成立**，v0.1.6 的 `readback_verified` 机制针对的问题是真实存在的，机制方向正确 ✅。

（副本 apply 实测因"同名文档已在 SolidWorks 中打开"被 `open_document` 正确拒绝——com-quirks 第二节行为，未动用户会话；读回失真的验证以只读方式完成。）

---

## 三、本轮核心发现：`recolor_assembly` 零匹配时仍保存文档，且 `readback_verified` 误报 `true`

### 现象（源码逻辑推演，`tools/recolor.py`）

当 palette 的键全部未命中任何组件（`matched == []`）时，例如调用方拼错零件名，执行流为：

1. `previous = {}`、`painted = 0`——循环体不执行，**没有任何修改**；
2. 但 `doc.ForceRebuild3(False)` 仍然执行，且 `save=True`（默认）时 **`doc.SaveSilent` 仍然执行——把用户文档保存了一遍**（时间戳/磁盘状态被改动，虽然内容无变化）；
3. 读回校验：`readback = []`，`readback_ok = all([])`——Python 中 `all(空列表)` 为 **`True`**；
4. 最终返回 `ok=True, applied=True, painted_components=0, readback_verified=True`——**声称"已应用且读回验证通过"，实际什么都没画**。

调用方（尤其是 AI 智能体）看到 `applied=true` + `readback_verified=true` 会认为上色成功，配合 `palette_keys_not_found` 字段虽然能发现线索，但两个肯定性字段与事实矛盾，且文档已被无意义保存。

### 建议修复（三选一或组合）

```python
# 1. 零匹配提前返回（推荐，dry-run/apply 都适用）
if not matched:
    return error(
        "no_match",
        "No component matches palette keys: %s" % ", ".join(sorted(palette)),
        "Run a dry-run first and use part names without the '-N' instance suffix.",
    )

# 2. 读回空样本时的语义修正
readback_ok = bool(readback) and all(item["matches"] for item in readback)
# 空样本不再误报 verified；零匹配时自然落到 warning 或 error

# 3. 无修改时不保存
if painted and save:
    doc.SaveSilent
```

### 严重性

中等——正常路径不受影响；触发条件是"palette 拼错 + apply=true"，但该场景在智能体工作流中并不罕见（LLM 生成的键名与实际零件名不一致），而"零修改却保存 + 肯定性验证字段"的组合会放大误导。

---

## 四、坑文档补充观察：读回失真实际有两种模式，`None` 未被记载

com-quirks 第十一节记载的失真模式是"读回白色 `[1.0, 1.0, 1.0]`"。本机实测还有第二种模式：**6/10 个组件的 `MaterialPropertyValues` 直接返回 `None`**（非 lightweight 疑问——组件已解析；也未做过任何写操作）。对 v0.1.6 机制的影响：

- `rgb_of(None)` 返回 `None` → `readback` 项 `matches=False` → 会正确触发 warning，**不会误报**，方向安全；
- 但 `describe_parts` 的 `rgb` 与 `previous_colors` 中这些组件会显示 `null`，调用方若不理解 `None` 也是失真模式之一，可能误判为"组件无颜色"而覆盖原始外观。

建议第十一节补充："失真模式有两种——读回恒白 `[1,1,1]` 与读回 `None`；`None` 同样不可作为'该组件无色'的依据。"另外 `readback_sample` 只抽前 3 个匹配组件（`matched[:3]`），若前 3 个恰好都在正常子集（如本机的红色件），失真可能漏检——可考虑按 palette 键各抽 1 个实例，保证每个被上色的零件类型都被抽查。

---

## 五、验收记录（本机，v0.1.6 @ 2ccfcac）

| 验证项 | 结果 |
|---|---|
| `serverInfo.version` | `0.1.6` ✅ |
| tools/list | 8/8 ✅ |
| `get_mass_properties` | mass 4.248157961 kg + `average_density_kg_m3: 1000.0` + `mass_basis` 疑似默认密度警示 ✅（上轮核心修复） |
| not-found 错误信息 | 带原因 ✅（回归通过） |
| 读回失真条件实测（只读） | 3 组件恒白、6 组件 None、渲染正确——第十一节描述的条件真实存在，v0.1.6 机制针对性强 ✅ |
| 副本 apply 路径 | 被 `open_document` 以"同名文件已打开"正确拒绝（符合坑文档第二节），未动用户会话 ✅ |
| 零匹配边界 | 源码审查发现"仍保存 + readback_verified 误报"（第三节，建议修复）⚠️ |

*以上基于 main 分支 2ccfcac 实测。读回失真的对照证据：`preview_after_reload.png` 像素聚类 vs `GetComponents` 逐件 `MaterialPropertyValues` 只读探测。*
