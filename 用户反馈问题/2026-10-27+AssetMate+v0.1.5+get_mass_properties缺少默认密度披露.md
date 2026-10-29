# 2026-10-27 AssetMate v0.1.5 验收反馈：get_mass_properties 缺少默认密度披露，与 BOM 的 mass_basis 不一致

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0，验收时正在运行）· Python 3.13.12 (64-bit) · mcp 1.30.0 · WorkBuddy MCP 客户端
> 验收对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（a3887b7，v0.1.5）
> 结论：**v0.1.5 验收通过**。上轮反馈修复到位；晚绑定修复在本机 gen_py 缓存真实存在的条件下通过端到端实战验证。本轮发现 1 个一致性问题（建议修复）+ 1 个文档小疏漏。

---

## 一、上轮反馈修复核验 + v0.1.5 新修复实测（全部通过 ✅）

| 修复项 | 来源 | 本机实测结果 |
|---|---|---|
| not-found 错误信息带原因 | 上轮 #v0.1.4 反馈 | `bad_input` 返回 `file not found: ... (check drive, folder and extension .SLDASM/.SLDPRT)` ✅ |
| **强制晚绑定**（防 gen_py 早绑定崩溃） | v0.1.5 新增（commit a3887b7） | **本机正是崩溃条件**：`%TEMP%\gen_py` 存在 SldWorks makepy 缓存，`GetActiveObject` 返回早绑定 wrapper（`win32com.gen_py.83A33D31-...`）。v0.1.5 用 `dyn.Dispatch` 包装为 `CDispatch` 后，byref VARIANT 的 `OpenDoc6` 正常工作，端到端 `get_assembly_tree`（10 组件、max_depth 1）完整返回 ✅ |
| `export_preview` 新增 `keep_bmp=false` | v0.1.5 新增 | PNG 正常生成（800×600，71 KB），BMP 中间文件默认已删除 ✅；tools/list 的 inputSchema 正确暴露新参数 ✅ |
| `export_bom` 新增 `mass_basis` 披露 | v0.1.5 新增 | 实测返回 `SolidWorks default density 1000 kg/m3 for 7 part(s) without material: 1, 2, 3, 4, 5, 6, T1_Pieza1` ✅（正是本轮发现问题的对照，见第二节） |
| `get_mass_properties` 转正（去掉实验标签） | v0.1.5 | 功能正常，但文档同步有疏漏，见第三节 |

**回归验证**：握手 `serverInfo.version == "0.1.5"` ✅，8/8 工具注册 ✅，editable 元数据 0.1.5 ✅，错误路径不崩溃 ✅。

**关于晚绑定修复的补充说明**：这是本轮最有价值的修复。验收时本机 `GetActiveObject` 返回的正是 `win32com.gen_py.83A33D31-27C5-11CE-BFD4-00400513BB57x0x34x0.SldWorks` 早绑定 wrapper——即作者描述的"gen_py 缓存存在时 7 个工具全灭"的条件在本机真实成立。v0.1.5 的 `dyn.Dispatch(obj)` 包装后走 `win32com.client.dynamic.CDispatch`，`OpenDoc6` 的 byref VARIANT 参数正常传递（实测 `errors.value == 2` 正确回填）。`EnsureDispatch` 无法用于 SldWorks（报 "can not automate the makepy process"）的备注也与本机行为一致。

---

## 二、本轮核心发现：`get_mass_properties` 无默认密度披露，与 `export_bom` 的 `mass_basis` 口径不一致

### 现象

对同一装配（7 个零件未指定材料）调用两个工具：

- `export_bom` → `total_mass_kg: 4.248157`，且 `mass_basis` 明确披露：`"SolidWorks default density 1000 kg/m3 for 7 part(s) without material: 1, 2, 3, ..."`
- `get_mass_properties` → `mass: 4.248157961146637`，**无任何披露字段**

数值完全一致（BOM 逐件求和 ≈ 整机 CreateMassProperty），但这恰恰证实了 `get_mass_properties` 的质量数字**同样来自 SolidWorks 对无材料零件的 1000 kg/m³ 默认密度回退**——`mass / volume = 4.248157961 / 0.004248157961 = 1000.0 kg/m³` 精确成立。

### 风险

同一装配，BOM 说"这个数基于默认密度、7 个零件没材料"，`get_mass_properties` 却只给一个光秃秃的 `4.248 kg`。AI 智能体或用户若只调后者，会把一个**实质上是占位值的质量**当成真实工程数据用于决策（选型、配重、运输估算等）。v0.1.5 在 BOM 侧已经迈出了正确的一步，两个工具应同口径。

### 建议修复

`get_mass_properties` 返回中增加与 BOM 同源的质量基准字段，例如：

```python
return ok(
    assembly=doc.GetTitle,
    units={...},
    mass=..., volume=..., surface_area=..., center_of_mass=...,
    mass_basis="SolidWorks default density 1000 kg/m3 for N part(s) without material: ..."
               if parts_without_material else "material density",
)
```

实现上可以遍历组件读 `GetMaterialPropertyName2`（该调用同样受 gen_py 早绑定影响，晚绑定下正常）；或至少在 docstring/返回中固定加上一句"未设材料的零件按 SW 默认 1000 kg/m³ 计"的静态说明。

### 严重性

中等偏低——数值本身没有错（SolidWorks 就是这么算的），问题在于**信息不对称**：BOM 披露了、质量工具没披露，违反了项目自己的"不编造结果"原则的精神（数值精确但语义不完整）。

---

## 三、小疏漏：`assembly.py` 中 `mass_properties` 的 docstring 仍写 "Experimental"

`tools/assembly.py` 的 `mass_properties()` docstring：`"Experimental: CreateMassProperty is unavailable under some pywin32 bindings..."`——而 v0.1.5 的 README（去掉"实验性"标注）、ROADMAP（"已转正（0.1.5）"）、`server.py` 工具 docstring（"Returns a structured error when CreateMassProperty is unavailable..."）都已按转正口径更新。函数体没变，仅 docstring 一行没跟上，建议顺手对齐，避免下一轮维护误判该工具的地位。

---

## 四、观察与建议（不计为问题）

1. **晚绑定方案的边界**：`dyn.Dispatch(early_bound_obj)` 依赖 `win32com.client.dynamic` 的 `CDispatch`，它在 pywin32 中是稳定 API，但作者若未来升级 pywin32 大版本，建议保留 com-quirks 第十节的回归测试（模拟 gen_py 缓存存在）。本次验收已验证该路径在"缓存存在"条件下工作正常。
2. **`get_sw` 的 last-resort 分支**：`except Exception: _SW = obj` 保留裸对象兜底——若 `dyn.Dispatch` 失败（极少见），这个裸对象可能正是早绑定 wrapper，会退回崩溃路径。建议兜底时打一行 warning 或在返回错误里提示，便于定位"为什么只有这台机器全灭"。
3. **`export_bom` 的 `mass_basis` 设计很好**，建议作为项目惯例推广到所有输出质量数的工具（即第二节的核心诉求）。

---

## 五、验收记录（本机，v0.1.5 @ a3887b7）

| 验证项 | 结果 |
|---|---|
| `serverInfo.version` | `0.1.5` ✅ |
| tools/list | 8/8 ✅，`export_preview` schema 含 `keep_bmp` ✅ |
| not-found 错误信息 | 带原因与检查建议 ✅（上轮修复） |
| gen_py 缓存条件下的端到端 `get_assembly_tree` | 10 组件 / max_depth 1 / 0.8s ✅（晚绑定修复实战验证） |
| `get_mass_properties` | mass 4.248157961146637 kg，volume 0.004248157961 m³，表面积 0.63673197 m²，质心 3 分量正常 ✅（披露缺失见第二节） |
| `export_bom` | total 4.248157 kg，`mass_basis` 披露 7 个无材料零件 ✅ |
| `export_preview`（800×600 PNG） | 生成成功，BMP 中间文件默认清理 ✅ |
| 早/晚绑定行为对比 | 早绑定 wrapper + byref VARIANT 在晚绑定包装下正常回填（errors.value=2）✅ |

*以上基于 main 分支 a3887b7 实测。测试装配为 10 组件的 Ensamblaje1.SLDASM（7 零件无材料，恰好构成默认密度回退的验证场景）。*
