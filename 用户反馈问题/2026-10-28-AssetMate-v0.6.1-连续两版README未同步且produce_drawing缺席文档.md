# 更新问题反馈：连续第二个版本 README 未同步——上版四项全未修，v0.6.1 主打功能 produce_drawing 又缺席文档

> 反馈日期：2026-10-28
> 项目版本：solidworks-assetmate-mechdog **v0.6.1**（commit 046e917，自 v0.6.0）
> 问题性质：**README 同步缺失已成模式**——v0.6.0 反馈的四项文档问题在 v0.6.1 中零修复，且本版新功能再次未入文档
> 更新方式：git pull（8242494 → 046e917，fast-forward）

---

## 一、更新验证基线（代码层全部通过）

| 检查项 | 结果 |
|--------|------|
| git pull | ✅ fast-forward，无冲突 |
| pyproject.toml | ✅ 仅版本号 0.6.0 → 0.6.1，依赖零变化，editable 安装自动生效 |
| MCP 握手 | ✅ `serverInfo: Solidworks_AssetMate_MechDog v0.6.1` |
| tools/list | ✅ **39 个工具**（新增 `produce_drawing`） |
| tests/static_check.py | ✅ `static check OK (39 tools, every module reference resolves)` |

`produce_drawing` 本身设计值得肯定：一次调用串起 `ledger → plan → views → dimensions → center marks → surface finish → hole callouts → audit → reconciliation → delivery verdict` 全链路，按零件类别（钣金/一般零件/轴类/装配体）给默认值并在 `defaults_applied` 回显，单步失败记录后继续——符合本项目「不编造结果」的原则。

---

## 二、核心问题：README 同步缺失已成模式

### 2.1 v0.6.0 反馈的四项问题，v0.6.1 逐项核对结果

| # | v0.6.0 反馈项 | v0.6.1 现状 |
|---|---|---|
| 1 | `add_geometric_tolerance` 缺席工程图工具表 | ❌ 仍未入表（全文仅能力边界表一处提及，且是错误结论） |
| 2 | 工具计数混乱（18/16/17/39/38/35 并存） | ❌ 未动，且随工具数增至 39 进一步过期 |
| 3 | 能力边界表仍写「形位公差写不进去…只能读不能写」，与「接口报错 ≠ API 不存在」专节矛盾 | ❌ 152 行原文未改 |
| 4 | 路线图仍为「v0.5 待做」，实际 v0.5/v0.6 均已发布 | ❌ 379 行未勾 |

### 2.2 新增问题：v0.6.1 主打功能 `produce_drawing` 同样未写进 README

v0.6.1 的 release 主题就是 "produce_drawing pipeline"，`tools/list` 实测存在且参数完整（`model_path / output_path / paper / views / category / do_hole_callouts / do_surface_finish`），但 README 全文检索不到 `produce_drawing`——三域工具表、快速上手、能力介绍均无。**连续两个版本，每个版本的主打新功能都没进 README。**

### 2.3 计数现状（截至 v0.6.1）

| 位置 | 数字 | 实测 |
|---|---|---|
| 装配标题（13）+ 工程图标题（18）+ 规划自检标题（8） | 39 | 巧合正确，但工程图表内 16 行、实际工程图类 17 个（+几何公差）+1 个 produce_drawing 归类待定 |
| 工程图表 | 16 行 | 少 `add_geometric_tolerance`，且无 `produce_drawing` |
| static_check 章节（294 行） | 「35 个工具函数」 | 实际 39 |
| `tests/static_check.py` 自己的输出 | 39 | 39 |

---

## 三、修复建议（比上版新增一条流程性建议）

**文档改动（同 v0.6.0 反馈，不再展开）：**
1. 工程图表补 `add_geometric_tolerance` 和 `produce_drawing` 两行
2. 294 行「35 个」→「39 个」
3. 能力边界表 152 行形位公差条目改为「v0.6.0 已解决，见专节」
4. 路线图勾掉 v0.5 / v0.6

**流程性建议（针对「连续两版都漏」的模式）：**

`tests/static_check.py` 已经掌握真实工具数和模块清单，建议让它顺带做两个零成本校验：
- **README 计数校验**：解析三个域标题括号里的数字之和 + 表内工具行数，与实际工具数比对，不一致即报警
- **工具名覆盖校验**：每个 `tools/list` 里的工具名必须在 README 出现至少一次（反引号包裹即可）

这样「加工具忘改文档」会在跑 smoke 的第一秒被挡住，与本项目「0.4.6 那种调用即崩的 NameError 会在这里被挡住」的思路一脉相承。另可在发布检查单里加一行：「release commit 必须包含 README diff」。

---

## 四、结论

代码层 v0.6.1 没有任何问题，`produce_drawing` 是把 34→39 个工具串成完整交付链的关键一步。唯一问题是 **README 已经落后代码两个版本**：上一版的主打功能（几何公差）和这一版的主打功能（produce_drawing）都不在文档里，能力边界表还在劝退一个已可用的能力。建议本次连同 v0.6.0 反馈的四项一起，做一次集中的文档同步，并用 static_check 建立防再犯机制。

— 反馈人：WorkBuddy 智能体（同一环境，Windows 10 / Python 3.13.14 64 位 / venv editable 安装 / SW2020+）
