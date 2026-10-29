# 2026-10-27 AssetMate v0.1.5 组件颜色读回失真（MaterialPropertyValues 返回白色，渲染实为涂色）

> 验收性质：v0.1.5 更新验收（针对 v0.1.4 报告的 gen_py 早绑定崩溃修复 + 次要问题修复的复验）
> 测试环境：Windows + SolidWorks 2026 + Python 3.13.14 + pywin32 + AssetMate v0.1.5
> 测试模型：多连杆双曲柄滑块机构沙箱副本（7 零件 + 1 总装 + 上轮重命名的 T1_Pieza1 / 新增 P2_1）
> 验收通道：全部通过 MCP 工具调用完成（v0.1.4 验收中必崩的场景本次全部经 MCP 实测）

---

## 一、v0.1.5 修复验收结论：全部生效 ✅

| v0.1.4 报告的问题 | v0.1.5 修复情况 | MCP 实测结果 |
|---|---|---|
| P0：gen_py 早绑定缓存下全工具崩溃（int() ... not 'VARIANT'） | `get_sw()` 强制 `win32com.client.dynamic.Dispatch` 晚绑定 | ✅ 修复生效。同一台机器（gen_py 缓存仍在 `%TEMP%\gen_py\3.13\`）+ MCP 通道，7 个工具全部正常 |
| `get_mass_properties` 外层异常未包结构化信封 | 外层加 `mass_failed` 信封 | ✅ 源码确认（assembly.py:98），正常路径 mass=4.248158 kg 与 BOM 总和六位小数吻合 |
| preview 转 PNG 残留中间 BMP | 新增 `keep_bmp=False` 默认清理，可选保留 | ✅ 两种模式实测：默认输出 PNG 且无 BMP 残留；`keep_bmp=true` 时 BMP 保留（3.6MB） |
| 材质未设时 BOM 质量可能误导 | 响应新增 `mass_basis` 字段，明确注明默认密度 1000 kg/m³ 及涉及零件 | ✅ 实测字段存在且列出全部 7 个未设材质零件 |
| FileNotFoundError 报错只有裸路径 | 改为 "file not found: <path> (check drive, folder and extension ...)" | ✅ bad_input 信封实测，信息可操作 |

全工具回归（MCP 通道，7/7 通过）：`get_assembly_tree`（10 组件 + 深度递归）、`export_bom`（qty 合计 10、CSV 落盘、7 条 material_not_set warning 不瞎猜）、`get_mass_properties`、`list_bodies`（232 面/644 边/416 顶点）、`recolor_bodies`（干跑→实跑 232 面全涂 + 保存）、`recolor_assembly`（干跑→实跑 3 组件 + previous_colors）、`rename_components`（dry-run 规划 → auto apply 引用重定向，`verified_components` 确认实例名跟随新文件名 P2_1-1）。

## 二、新发现的问题（建议 v0.1.6 处理）

### 问题 1（主要）：`recolor_assembly` 涂色后，`MaterialPropertyValues` 读回失真为白色

复现步骤：

1. `recolor_assembly` `palette={"2":[0.15,0.35,0.8], "3":[1,0.8,0]}`，`apply=true` → 返回 `painted_components: 3, saved: true`。
2. `get_assembly_tree`（含 `refresh=true`）读回这 3 个组件的 `rgb` 均为 `[1.0, 1.0, 1.0]`（白色），而非涂色的蓝/黄。
3. 决定性对照：`export_preview` 渲染像素分析蓝 `(24,64,195)`、黄 `(251,181,5)` 真实存在；关闭文档重开、`ResolveAllLightweightComponents(True)` 后读回**依旧** `[1,1,1]`，渲染**依旧**蓝/黄。

结论：**写入正确、渲染正确、保存持久化正确，只有读回（读路径）失真**。影响：

- `get_assembly_tree` / `describe_parts` 返回的 `rgb` 对被涂色组件不可信（显示为白/None，与实际不符）；
- `recolor_assembly` 返回的 `previous_colors` 在"恢复原色"场景下可能拿到 `[1,1,1]` 这类失真值，用它回写会把组件改成白色而非恢复默认；
- 同会话内第一次读（涂色前 `rgb=null`）与涂色后读（`[1,1,1]`）还不一致，说明失真与写入动作本身相关（疑与 late-bound 传参下属性 getter 的 VARIANT 解组有关，值 0..1 的数组被读成 1,1,1）。

建议：在 `rgb_of()` / `describe_parts` 读回处对 `[1.0,1.0,1.0]` 与"从未涂色"做区分标注（如 `rgb_readback: "suspect"`），或改用 `GetMaterialPropertyValues2`/配置级 API 交叉验证；至少在 docs/com-quirks.md 记录该读回失真，避免用户把 tree 的 rgb 当作视觉真相。

### 问题 2（次要）：`recolor_bodies`（面级涂色）在装配上下文中被组件级颜色遮盖，且无提示

复现：对 T1_Pieza1 的唯一实体涂绿色 `[0.2,0.65,0.3]`（applied=true，232 面全涂、part 文档保存成功，重开后零件文件内面颜色读回 `[0.2,0.647,0.298]` 持久化正确），但装配预览图中**绿色像素为 0**——装配给该组件设过组件级颜色（红色），SolidWorks 显示优先级为组件级 > 面级，零件文档里的绿色在装配中不可见。

影响：用户对"在装配中显示的零件"做 recolor_bodies 会以为失败。建议在 recolor_bodies 响应中加一条提示：若该零件被某装配以组件级颜色引用，装配视图中组件级颜色优先。

## 三、证据清单

- MCP 通道产物：`sandbox/bom_v015.csv`、`sandbox/preview_v015.png`（红/蓝/黄三色像素定位+均值验证）、`sandbox/preview_after_reload.png`（重载后渲染不变）、`sandbox/preview_keepbmp.png` + `preview_keepbmp.bmp`（keep_bmp 生效）
- 诊断脚本（只读）：`sandbox/verify_colors_fresh_reload.py`（关闭重开 + 面级颜色读回）
- 关键数据：涂色请求蓝 [0.15,0.35,0.8] → 渲染实测均值 (24,64,195)≈[0.09,0.25,0.76]（sRGB/Gamma 差异，色相吻合）；黄 [1,0.8,0] → (251,181,5)；绿色涂到零件面（part 内持久化 ✅）但装配渲染 0 像素（组件级遮盖）

---

*报告基于 2026-10-27 MCP 通道实机验收，所有结论均有落盘证据。v0.1.5 的 P0 修复质量很高；新发现问题均不影响工具的写路径正确性，属读回/提示层面。*
