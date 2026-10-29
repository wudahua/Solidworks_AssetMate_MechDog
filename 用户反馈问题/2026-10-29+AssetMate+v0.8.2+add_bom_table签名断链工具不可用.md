# AssetMate v0.8.2 验收报告 — add_bom_table 签名断链（工具完全不可用）

- **验收日期**：2026-10-29
- **版本跨度**：v0.6.1 → **v0.8.2**（3 个 minor 版本，工具 39 → 48）
- **验收环境**：Windows + SolidWorks 2026（Revision 34.0.0）+ Python 3.13.12 x64 + mcp 1.30.0
- **验收方式**：stdio JSON-RPC 全链路探针 + COM 级根因复现 + 官方 static_check / smoke 真机回归

---

## 一、总体结论

**基本可用，但本版头条功能 `add_bom_table` 因一处包装层断链完全不可用**；另有 `add_balloon` 在 SW 2026 上两条实现路径全失败。其余全部通过：48 工具注册、官方 smoke 12/12、静态检查（新增 README 同步守护与 duplicate-def 检测）、七项历史回归全绿、边枚举缓存生效。

---

## 二、核心问题 1（高严重性）：`add_bom_table` 任何调用 100% 必炸

**现象**：只传 `drawing_path`（schema 要求的唯一必填参数），返回：

```
Error executing tool add_bom_table: add_bom_table() got an unexpected keyword argument 'bom_type'
```

**根因**（代码级确认）：

- `server.py` 包装器（约 L255-271）：签名保留 `bom_type=2, anchor_type=1`，并**显式透传**给实现层；
- `layout.py` 实现（L362）：`def add_bom_table(drawing_path, view_name=None, configuration="Default", template=None, save=True)` —— 重构为固定 Indented→TopLevelOnly 两步法后**已删除**这两个参数（docstring 明确 "Do NOT call SetConfigurations in this flow"）；
- 因为包装器总是显式传 `bom_type=bom_type`（有默认值也照样传），**客户端无从绕过**；MCP schema 由包装器生成（schema 里 `bom_type` 默认 2），调用方甚至无法"不传"。

**三层自相矛盾**（也是定位线索）：

1. `check_capabilities` 对 `bom_table` 报 **status="verified"**——能力表与实际状态矛盾；
2. server 层 docstring 写 *"arrives with its header only: row population is not solved yet"*，layout 层 docstring 写 *"POPULATED native TopLevelOnly BOM … Verified working 2026-10-29 on SW2024 SP4: 10 rows x 4 columns"*——包装器是重构前残留，两层文档互相冲突；
3. 本版刚新增 duplicate-def 静态检查（commit *"test: detect duplicate def within a scope - the shadowing that cost 40 minutes"*），但**签名漂移**（包装器 kwargs 与实现签名不匹配）是另一类问题，现有静态检查（名字可解析、重复定义、属性陷阱）都覆盖不到。

**修复建议**：

- 包装器删除 `bom_type`/`anchor_type` 透传与参数（实现层已固定流程），同步两侧 docstring；
- 静态检查加一层**签名兼容校验**：对 server.py 里每个 `_json(module.func(...))` 调用，用 `inspect.signature` 校验传入 kwargs 是否都被实现层接受（AST 级亦可）——这是duplicate-def 检测的自然延伸，能把"重构断链"整类拦在发布前。

---

## 三、核心问题 2（中等严重性）：`add_balloon` 在 SW 2026 上两条路径全失败

**现象**：用 `drawing_audit` 查到的真实视图名（`工程图视图1`）调用，返回：

```json
{"ok": false, "error": "balloon_failed", "message": "<unknown>.InsertBalloon"}
```

**COM 级复现**（本机 SW 2026，晚绑定 CDispatch，同 server 完全一致的对象类型）：

| 调用方式 | 结果 |
|---|---|
| `doc.InsertBOMBalloon2(1, 0, 0, 0, 0.0)`（代码现状，5 参） | `com_error -2147352561（DISP_E_PARAMNOTOPTIONAL）`——参数个数不符 |
| `doc.InsertBalloon(...)`（fallback，属性派发） | `AttributeError: <unknown>.InsertBalloon` |
| `call_method(doc, 'InsertBalloon', ...)`（DISPATCH_METHOD 直呼） | `com_error -2147352570（DISP_E_UNKNOWNNAME）`——**名字都解析不到** |

第三行值得注意：连 `call_method` 都查无此名，说明 SW 2026 的 dispatch 接口上 `InsertBalloon` 已不存在（移除或改名），不只是属性派发陷阱。作者开发机是 **SW 2024 SP4**（layout.py docstring 记录），本机为 SW 2026——**无法断言工具写错，但"balloon 未经 SW 2026 验证且失败"应披露**。

**辅助观察**：

- `InsertBalloon`/`InsertBOMBalloon2` 不在 static_check 的 `PROPERTY_TRAPS`（9 项）名单里，`layout.py` L282/L286 的裸调用未被拦截（`InsertBOMBalloon2` 外层 try/except 还把真实错误吞掉静默 fallback）；
- `layout.py` L303/L306 的 `doc.EditRebuild3` / `doc.GraphicsRedraw2` 无括号裸属性调用，同属应走 `call_method` 的风格风险。

**修复建议**：SW 2026 上验证 `IBalloonParams`/`InsertBOMBalloonN` 系列的正确签名；至少在 com-quirks / check_capabilities 记录"balloon 在 SW2026 失败（UNKNOWNNAME），仅 SW2024 验证过"；把两个 Insert 调用改走 `call_method` 并把 `InsertBalloon` 加入陷阱名单（若确认是陷阱）。

---

## 四、次要问题：`call_method` helper 双份定义

`sw_session.py:85`（完整版，含 `_FlagAsMethod` 处理与陷阱名单共享）与 `bom_native.py:32`（精简版）各有一份。本版刚修过同 scope 重复定义（"the shadowing that cost 40 minutes"）并加了检测，但**跨模块 helper 重复**不在检测范围。建议统一 import sw_session 的版本，避免两处行为漂移。

---

## 五、验证通过的部分（证据清单）

| 验证项 | 结果 |
|---|---|
| 握手 `serverInfo.version` | 0.8.2 ✅ |
| tools/list | 48 个（13 装配 + 25 工程图 + 10 规划自检，与 README 表一致，static_check 已守护 README 同步）✅ |
| 官方 smoke（--skip-export --skip-drawing） | **12/12 PASS**（1m55s）✅ |
| static_check | OK（新增 duplicate-def 检测 + README 同步检查生效）✅ |
| 七项历史回归（mass_basis 披露、not-found 带原因、dry-run 探索性 available_parts、apply 零匹配 no_match 守卫等） | 全 PASS ✅ |
| 装配体建图 → drawing_audit | 布局警告 0（auto_fit 修复保持），视图 `工程图视图1/2` scale 0.1 ✅ |
| `add_surface_finish` / `add_sheet_line` / `add_note` | 实测 ok，annotations 计数正确增长 ✅ |
| 边枚举 mtime 缓存 | 397 边，首调 2.0s，二调 `cached=true` 瞬回 ✅（335s→47s 的绝对值为作者在配电箱大图上的数据，本机图小无法复测绝对值） |
| 能力表 14 项 | 状态诚实（`dxf_import`/`geometry_creation` 如实报 unsupported）✅ |
| `open_document` 错误提示升级 | 带 `errors=` 错误码与 doc_type 不匹配提示，doc_type 按扩展名推断 ✅ |
| `项目规划 - AssetMate_MechDog/`（19 份 BOM 攻坚过程记录）入库 | 过程透明度高 ✅ |

---

## 六、验收环境备注（非缺陷）

- **smoke 重跑前必须清掉上次中断的残留**：首次 smoke 超时被杀后遗留的 `smoke_asm.SLDASM`（smoke 自建副本）留在 SolidWorks 中，重跑 9 项报 "same name already open" FAIL；关闭残留后同参数重跑即 12/12。建议 smoke 启动时自动 CloseDoc 清理自己的产物名。
- 上轮 v0.6.1 无遗留待办；v0.1.x→v0.6.x 九轮反馈全部闭环。

---

## 七、给作者的优先级排序

1. **P0**：修 `add_bom_table` 包装层断链（删 bom_type/anchor_type 透传 + 同步 docstring）——头条功能当前调用即错，且与 capability 表 "verified" 矛盾；
2. **P0**：static_check 增加包装器↔实现签名兼容校验（防整类"重构断链"，与上轮 duplicate-def 检测互补）；
3. **P1**：`add_balloon` 在 SW 2026 的失败记录进 com-quirks / check_capabilities（UNKNOWNNAME 证据已附），验证替代 API；
4. **P2**：统一 `call_method` 到 sw_session 单份；`InsertBalloon*` 裸调用改走 call_method；smoke 自清理残留文档。
