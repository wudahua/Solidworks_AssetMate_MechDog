# 2026-10-27 AssetMate v0.1.6 验收：外部关闭重开后组件缓存静默返回空数据（BOM 塌缩为 1 空行）

> 验收性质：v0.1.6 第三轮验收（重点：质量属性披露、recolor 读回校验机制、零匹配边界）
> 测试环境：Windows + SolidWorks 2026 + Python 3.13.14 + pywin32 + AssetMate v0.1.6（MCP 服务器进程 09:54:44 启动，晚于 09:21 源码更新，确认加载新代码）
> 测试通道：全部经 MCP 工具调用；辅助诊断脚本仅做只读探测与外部关闭/重开文档
> 测试模型：多连杆双曲柄滑块机构沙箱（7 种零件 10 实例；含前两轮的 T1_Pieza1、P2_1 重定向副本）

---

## 一、v0.1.6 修复验收：通过 ✅

| 验收项 | MCP 实测结果 |
|---|---|
| `get_mass_properties` 默认密度披露 | ✅ `average_density_kg_m3: 1000.0` + `mass_basis: "suspected default density: mass/volume = 1000.0 kg/m3 … verify material assignments"`，质量 4.248158 kg 与 BOM 六位小数吻合 |
| `recolor_assembly` 读回校验机制 | ✅ `readback_sample`/`readback_verified` 字段生效。涂 4/5/T1_Pieza1 三种零件：读回 `[0.549,0.2,0.749]`/`[0.2,0.698,0.698]`/`[0.898,0.4471,0.098]` 均与请求值吻合且为**真实匹配**——关闭重开后渲染像素聚类确认紫/青/橙区域全部存在（0.092%/0.040%/0.013%），涂色写入与持久化正确 |
| `recolor_bodies` 装配遮盖提示 | ✅ 响应 note 明确"component level overrides face level in the viewport"（上轮次要问题 2 采纳） |
| com-quirks 坑文档第十一节 | ✅ 已记载读回失真（读回恒白）与 readback 机制 |
| 工具数量 | ✅ 8/8 工具（`resolve_lightweight` 后端参数不暴露于 MCP schema，见第三节 3） |
| 回归 | ✅ rename auto（6→P3_6 双实例重定向 verified）、recolor_bodies apply（232 面）、export_preview（PNG 正常、默认无 BMP 残留、keep_bmp 可保留）、list_bodies、错误信封均正常 |

## 二、核心发现（P0）：外部关闭重开文档后，组件缓存静默返回空数据

**复现序列（全部实测）**：

1. MCP 正常调用若干轮（缓存已建立）；
2. 任意外部进程关闭并重开该装配（诊断脚本 `CloseDoc` + `OpenDoc6`，或用户在 SW UI 中手动关闭再打开）；
3. MCP `export_bom`（默认 refresh=false）→ **返回 1 个空行**：`part: ""`、`qty: 10`、`path: null`、全部 mass/material null、`document_not_loaded` warning；`total_mass_kg: null`；
4. MCP `get_assembly_tree` → **10 个组件的 `component`/`part`/`path` 全为空串**，`ok: true`；
5. 加 `refresh: true` 后立即恢复正常（7 行齐全）。

**根因**：`sw_session.get_components()` 的缓存有效性校验是 `cached[0].Name2`（期待陈旧指针抛异常）。但文档关闭重开后，late-bound 下的陈旧 `IComponent2` 指针**不抛异常，而是静默返回空串**（`Name2=""`、`GetPathName=""`、`GetModelDoc2=None`）——校验形同虚设，垃圾数据以 `ok:true` 信用背书输出。缓存键是 `doc.GetPathName`，新文档路径相同直接命中陈旧条目。

**影响**：多智能体/人机混合作业是常态场景（用户在 SW 里关了文档再打开，或两个 MCP 客户端交替操作），此后所有依赖组件列表的读操作（tree/bom/recolor dry-run/rename 规划）都产出看似成功实则全空的响应，`ok:true` 还会误导调用方"装配就是空的"。

**建议修复**（任一即可，建议组合）：

```python
# 1. 缓存校验同时拒绝空名（一行，防全部静默空数据）
if cached and str(cached[0].Name2 or "").strip():
    return cached

# 2. 缓存条目记录 doc 指针，get_components 校验 doc 身份一致（防跨文档实例复用）
# 3. open_document 返回的 doc 与缓存 doc 不是同一实例时主动 invalidate
```

严重性：P0（数据静默失真 + 误信用背书），触发条件常见。

## 三、其他发现

1. **零匹配 + apply=true 仍保存文档且 `readback_verified=true` 误报（已实机坐实）**：palette 键全部拼错时 MCP 返回 `applied=true, painted_components=0, saved=true, readback_verified=true, readback_sample=[]`，且装配文件 mtime 实测被刷新（09:59:02）——空样本 `all([])` 恒真。此前评审已从源码推演过此缺陷，本轮补齐 MCP 实机证据。建议：零匹配提前返回 `no_match` 错误；`readback_ok = bool(readback) and all(...)`；`if painted and save:`。
2. **读回失真为非确定性**：同一文档中，v0.1.5 会话涂的 2/3（蓝/黄）重载后读回仍恒白 `[1,1,1]`，而本会话涂的 4/5/T1 读回正常——失真按组件/会话随机分布，非全文档属性。支持保留 readback 机制；但 `matched[:3]` 抽样可能恰好全落在正常子集而漏检（建议按 palette 每键至少抽 1 实例，并保持"读回恒白/None 均视为可疑"口径，`None` 失真模式建议补进 com-quirks 第十一节）。
3. **次要**：`bom.export_bom` 后端有 `resolve_lightweight` 参数，但 MCP server 注册的 schema 未暴露（始终按 True 执行）。行为无碍，仅参数不可达；若刻意隐藏建议在 docstring 说明。

## 四、验收记录

| 验证项 | 结果 |
|---|---|
| mass 披露 / readback 机制 / 遮盖提示 / 8 工具 | ✅ 全部通过 |
| 零匹配保存 + readback_verified 误报 | ⚠️ 实机坐实（源码推演 + mtime 证据） |
| 外部关闭重开后缓存空数据 | ❌ P0，MCP 实测复现（BOM 1 空行 + tree 10 空组件，refresh 可恢复） |
| 涂色写入/持久化/渲染 | ✅ 紫/青/橙重载渲染确认（0.092%/0.040%/0.013% 像素） |

*证据：`sandbox/bom_v016.csv`（塌缩现场未覆盖，refresh 版已另存）、`sandbox/preview_v016_final.png`、`sandbox/render_check.bmp`（重载后三色渲染）、MCP 响应全文见会话记录。外部关闭重开为实测触发序列，非推测。*
