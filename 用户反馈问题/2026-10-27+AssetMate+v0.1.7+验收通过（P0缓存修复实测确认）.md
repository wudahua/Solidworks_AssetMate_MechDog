# 2026-10-27 AssetMate v0.1.7 验收记录：上轮 P0 缓存修复实测确认，验收通过

> 验收性质：v0.1.7 第四轮验收（独立复验，与作者仓库 85bbbd8 的验收报告互补）
> 测试环境：Windows + SolidWorks 2026 + Python 3.13.14 + pywin32 + AssetMate v0.1.7（MCP 服务器进程 10:37:47 启动，晚于 10:29 源码更新）
> 测试通道：全部经 MCP 工具调用；外部关闭/重开脚本复现上轮 P0 触发序列
> 结论：**验收通过，未发现新缺陷**。本轮价值在于实机复现了 v0.1.6 P0 的完整触发链并确认修复生效。

---

## 一、上轮 P0（外部关闭重开后缓存静默空数据）修复实测：通过 ✅

完整触发链复现（正是 v0.1.6 塌缩现场）：

1. MCP `get_assembly_tree` 建立缓存（10 组件齐全）；
2. 外部进程 `CloseDoc` + `OpenDoc6` 重开装配（上轮触发方式不变）；
3. MCP `export_bom`（**不刷 refresh**，走缓存路径）→ **立即返回完整 7 行**：零件名/路径/质量齐全，`total_mass_kg: 4.248157`，`mass_complete: true`。对比 v0.1.6 同序列返回 1 个空行（`part:""`, `qty:10`, 全 null）。

修复实现核验（`sw_session.get_components`）：双重校验——`doc._oleobj_` COM 身份比对 + 首组件 `Name2` 非空。本轮补充实测：晚绑定下不同 dynamic wrapper 包装的同一文档，`_oleobj_ ==` 比较为 `True`（`is` 为 `False`）——**身份比对在真实调用路径上有效，缓存命中不会被误杀**，无性能回退隐患。

## 二、其余修复项实测

| 验收项 | MCP 实测结果 |
|---|---|
| 零匹配 + apply=true 守卫 | ✅ 返回结构化 `{"ok": false, "error": "no_match", "message": "No component matches palette keys: AlsoFake, NotARealPart", "hint": ...}`；无 `applied` 字段；装配文件 mtime 保持 10:05:39 未被触碰（v0.1.6 的无意义保存已消除） |
| 读回抽样按 palette 每键 1 实例 | ✅ 6 键 palette → `readback_sample` 6 项（2-1/T1/4-2/P3_6-2/5-1/P2_1-1 各一），全部真实匹配；预览渲染像素确认六色（蓝 0.096%/绿 0.040%/紫 0.104%/青 0.053%/橙 0.030%/卡其 0.009%）全部真实存在 |
| `resolve_lightweight` 暴露到 MCP schema | ✅ `resolve_lightweight=false` 实测可用，BOM 数据完整（零件已解析场景） |
| 部分匹配不误伤 | ✅ dry-run + 1 有效键 + 1 无效键：`ok=true`、`matched_components: 2`、`palette_keys_not_found: ["BogusKey"]` |
| 回归 | ✅ rename auto（5→P4_5，verified_components 确认重定向）、BOM refresh 路径、list_bodies、export_preview（PNG 正常 + 默认无 BMP 残留）、错误信封 |

## 三、观察项确认（与作者仓库 v0.1.7 报告一致，非新发现）

1. **dry-run 零匹配同样返回 `no_match` 错误**（实测确认）：dry-run 本是只读探索，旧行为返回 `ok=true` + `parts` 全列表 + `palette_keys_not_found` 便于智能体自行纠正键名；现在需额外调一次 `get_assembly_tree` 才能拿到零件名清单。支持作者报告的建议：dry-run 保持旧行为，或 `no_match` payload 附带 `available_parts`。
2. `readback_sample` 上限 8 的边界：当前规模（7 种零件）未触及，仅需文档标注（作者报告已建议）。

## 四、验收明细

| 验证项 | 结果 |
|---|---|
| P0 缓存修复（BOM 路径，完整触发链） | ✅ 本轮核心确认 |
| `_oleobj_` 身份比对在晚绑定下有效 | ✅ 补充实测 |
| no_match 守卫 + 不保存 | ✅ mtime 证据 |
| 每键抽样 + 六色渲染验证 | ✅ |
| 8 工具回归 | ✅ |

*证据：`sandbox/bom_v017.csv`（P0 修复后完整 BOM）、`sandbox/preview_v017_allcolors.png`（六色渲染）、装配 mtime 10:05:39（零匹配未触碰）。v0.1.6 塌缩现场对照见上一轮报告。*

---

*四轮验收轨迹：v0.1.4（gen_py 全崩）→ v0.1.5（读回失真披露）→ v0.1.6（缓存空数据 P0）→ v0.1.7（全部闭合，验收通过）。*
