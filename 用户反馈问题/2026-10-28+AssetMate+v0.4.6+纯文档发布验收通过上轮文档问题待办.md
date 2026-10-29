# 2026-10-28 AssetMate v0.4.6 验收反馈：纯文档发布（ROADMAP 重写），验收通过；上轮 2 处文档问题仍在待办

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0）· Python 3.13.12 (64-bit) · mcp 1.30.0 · WorkBuddy MCP 客户端
> 验收对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（1d7067b，v0.4.6）
> 结论：**v0.4.6 验收通过**。本版为纯文档发布（ROADMAP 重写 + 版本号，零代码变更），回归全部保持；无新问题。上轮（#v0.4.5）反馈的 2 处文档问题本轮未处理，见第二节催办清单。

---

## 一、本版变更与验收结果

### 1.1 变更范围（与 v0.4.5 @ 5d26393 对比）

| 文件 | 变更 |
|---|---|
| `docs/ROADMAP.md` | 全面重写：现状对齐 v0.4.5（35 工具三能力域）；纳入真实图纸基线数据（6346 张抽样 100 张）；v0.5 规划定为"孔标注 + 规划 + 检查单"；新增风险项"判定标准失准——阈值必须来自实测基线，不确定的检测降级为 warning 而非 blocker"；明确不做 DXF/DWG 逆向（SolidPilot 已覆盖） |
| `pyproject.toml` / `__init__.py` | 版本号 0.4.5 → 0.4.6 |
| 其余全部代码 / README / tests | **无变更** |

### 1.2 回归验证（全部保持 ✅）

| 验证项 | 结果 |
|---|---|
| `serverInfo.version` | `0.4.6` ✅ |
| tools/list | 35 工具 ✅ |
| dry-run 零匹配 | `ok=true` + `available_parts`（探索性）✅ |
| apply 零匹配守卫 | `no_match` 结构化错误 ✅ |
| `get_mass_properties` 的 `mass_basis` | 保持披露 ✅ |
| not-found 错误信息 | 保持带原因 ✅ |
| editable 安装 | 0.4.6 ✅ |

ROADMAP 质量点评：新增的"判定标准失准"风险项（"工具给出错误结论，比没有更危险"）与对应的缓解策略（阈值实测化、不确定检测降级为 warning）是本版最有价值的 additions，与该项目"不编造结果"的一贯原则一致。v0.5 三条待决问题（形位公差是否走 C# interop、孔标注是否放弃自动化、判定标准是否按零件类型分档）边界清晰，期待下版。

---

## 二、催办：上轮（#v0.4.5）反馈的 2 处文档问题尚未处理

均为主 README / tests 层面的轻微问题，代码无涉，修复成本极低，建议随 v0.4.7 一并处理：

### 2.1 README 工具清单漏列 2 个工具（34 vs 实际 35）

- README 声称 13 + 15 + 6 = **34** 个工具，`tools/list` 实际注册 **35** 个；
- 工程图表只列 14 项（表头写 15），**漏列 `add_hole_callouts`（批量孔标注）与 `list_circular_edges`（列圆边）**；
- 这两个恰是孔标注工作流（`list_circular_edges` → `add_hole_callouts`）的入口，漏列容易让使用者错过。

修复：补两行表格、表头 15 → 16、总数 34 → 35。

### 2.2 `tests/smoke.py` 默认模型路径指向仓库外

`DEFAULT_ASSEMBLY / DEFAULT_PART` 引用 `测试用例/` 目录，该目录**不在仓库中**（zipball 无此目录）。README「真机回归」写的裸命令 `python tests/smoke.py` 在 clone/下载的仓库里直接失败（exit 2，环境问题）。

修复（任选其一）：
- README 该节补一句：「仓库不含测试模型，请用 `--assembly <装配> --part <零件>` 指定自己的模型」；
- 或入库一个极小示例装配让默认路径可跑。

（本机验证均以 `--assembly/--part` 显式指定模型完成，v0.4.5 时 smoke 11/11 通过；v0.4.6 代码未变，无需重跑。）

---

## 三、六轮验收轨迹（v0.1.3 → v0.4.6）

| 版本 | 反馈核心问题 | 状态 |
|---|---|---|
| v0.1.3 | serverInfo.version 显示 mcp 库版本 | v0.1.4 修复 ✅ |
| v0.1.4 | bad_input 错误信息裸路径 | v0.1.5 修复 ✅ |
| v0.1.5 | get_mass_properties 缺密度披露 | v0.1.6 修复 ✅ |
| v0.1.6 | 零匹配仍保存 + readback 误报 | v0.1.7 修复 ✅ |
| v0.1.7 | （观察项）no_match 对 dry-run 生效、readback 上限未标注 | v0.4.5 落实 ✅ |
| v0.4.5 | README 漏列 2 工具；smoke 默认路径失效 | **待办（本版未处理）** |

*以上基于 main 分支 1d7067b 实测。v0.4.6 零代码变更，回归探针覆盖所有既往修复点，全部保持。*
