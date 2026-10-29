# 2026-10-27 AssetMate v0.1.7 验收反馈：三项修复全部到位，验收通过；附 2 条观察项

> 反馈人环境：Windows 10 x64 · SolidWorks 2026 SP0（RevisionNumber 34.0.0，验收时正在运行）· Python 3.13.12 (64-bit) · mcp 1.30.0 · WorkBuddy MCP 客户端
> 验收对象：`wudahua/Solidworks_AssetMate_MechDog` @ main（85bbbd8，v0.1.7）
> 结论：**v0.1.7 验收通过，未发现需要修复的缺陷**。上一轮（#v0.1.6）的全部发现按建议落实，另附加固了组件缓存校验。文末附 2 条低优先级观察项，供后续版本参考。

---

## 一、上轮反馈修复核验（全部按建议落实 ✅）

| 上轮发现 | v0.1.7 修复方式 | 本机实测结果 |
|---|---|---|
| **核心**：零匹配时仍保存文档 + `readback_verified` 误报 `true` | ① 新增 `no_match` 守卫（零匹配提前返回结构化错误，附 dry-run 提示）；② `readback_ok = bool(readback) and all(...)`；③ `if painted and save` 才保存，`saved` 字段如实反映 | 实测零匹配 + `apply=true`：返回 `{"ok": false, "error": "no_match", "message": "No component matches palette keys: NoSuchPartAAA", "hint": "Run a dry-run first..."}`，无 `applied` 字段、不保存 ✅ |
| `readback_sample` 只抽前 3 个组件，可能漏检其他键的失真 | 按 palette **每个零件键各抽 1 个实例**（上限 8） | 代码确认 `seen_keys` 去重采样 ✅ |
| com-quirks 未记载 `None` 读回模式 | 第十一节更新：明确"失真有两种形态：恒白 `[1,1,1]` 与 `None`；`None` 不能当作无颜色"；并补充"失真按组件/会话随机分布，抽样必须覆盖每个零件类型" | 文档与此前实测（3 白 + 6 None）吻合 ✅ |

**额外加固（作者自行发现）**：`get_components` 缓存校验从"读属性抛异常才失效"升级为双重校验——`doc._oleobj_` 身份比对 + 首组件 `Name2` 非空。原因实测很典型：文档外部关闭重开后，陈旧 `IComponent2` **不抛异常而是静默返回空串**，旧校验会漏掉。`get_assembly_tree` 二次调用（缓存命中路径）回归正常（10 组件）✅。

## 二、验收明细（本机，gen_py 缓存条件）

| 验证项 | 结果 |
|---|---|
| `serverInfo.version` | `0.1.7` ✅ |
| tools/list | 8/8 ✅ |
| `no_match` 守卫（apply=true + 不存在的键） | 结构化错误、不保存、无假 applied ✅（v0.1.6 核心缺陷已闭合） |
| 部分匹配 dry-run（1 有效键 + 1 无效键） | `matched: 1`、`palette_keys_not_found: ['BogusKey']`、dry_run 正常 ✅（守卫不误伤部分匹配） |
| `get_mass_properties` 的 `mass_basis` | 保持披露（suspected default density 1000.0）✅ 回归通过 |
| not-found 错误信息 | 保持带原因与扩展名提示 ✅ 回归通过 |
| 缓存命中路径（第二次 get_assembly_tree） | 10 组件正常 ✅ |
| editable 安装元数据 | 0.1.7 ✅ |

另核对：`server.py` 为 `export_bom` 透传的 `resolve_lightweight` 参数在 v0.1.6 的 `bom.py` 中已存在（默认 `True`），签名匹配，无断链。

## 三、观察项（低优先级，不计为缺陷）

### 3.1 `no_match` 守卫对 dry-run 也生效，丢失了探索性信息

实测 dry-run + 全部无效键时同样返回 `no_match` 错误——而 dry-run 本来是只读探索调用，此前的行为是返回 `ok=true` + `parts` 全列表 + `palette_keys_not_found`，智能体可以据此**自行纠正键名**再发起 apply。现在这个场景要额外调一次 `get_assembly_tree` 才能拿到零件名清单。

建议（可选二选一）：
- dry-run 模式下保持旧行为（`ok=true` + 列表），仅对 `apply=true` 启用 `no_match` 守卫；守卫的本意是防误写，dry-run 无写风险；
- 或在 `no_match` 错误的 payload 中附带 `parts`（或 `available_parts`）列表，一次返回可用键名，省一次往返。

### 3.2 `readback_sample` 上限 8 的边界

palette 键超过 8 个时，第 9 个之后的零件类型无读回校验。当前装配规模（≤19 种零件）下影响有限，建议在 README 或工具 docstring 标注该上限即可。

---

## 四、四轮验收轨迹回顾（v0.1.3 → v0.1.7，供作者参考）

| 版本 | 反馈核心问题 | 状态 |
|---|---|---|
| v0.1.3 | serverInfo.version 显示 mcp 库版本 | v0.1.4 修复 ✅ |
| v0.1.4 | bad_input 错误信息裸路径 | v0.1.5 修复 ✅ |
| v0.1.5 | get_mass_properties 缺密度披露 | v0.1.6 修复 ✅ |
| v0.1.6 | 零匹配仍保存 + readback 误报 | v0.1.7 修复 ✅ |

*以上基于 main 分支 85bbbd8 实测。测试装配为 10 组件 Ensamblaje1.SLDASM（T1_Pieza1 / P2_1 / P3_6 / 1–6 等零件名）。*
