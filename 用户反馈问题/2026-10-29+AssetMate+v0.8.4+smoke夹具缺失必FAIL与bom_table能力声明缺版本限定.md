# AssetMate v0.8.4 验收报告 — smoke 新增项依赖仓库外夹具必 FAIL；bom_table 能力声明缺版本限定

- **验收日期**：2026-10-29
- **版本跨度**：v0.8.2 → **v0.8.4**（2 个提交，无新增工具，仍 48 个）
- **验收环境**：Windows + SolidWorks 2026（Revision 34.0.0）+ Python 3.13.12 x64 + mcp 1.30.0
- **验收方式**：stdio JSON-RPC 全链路探针 + COM 级验证 + 官方 static_check / smoke 真机回归

---

## 一、总体结论

**上轮 2 个 P0 反馈全部高质量修复**（含采纳建议新增签名守护），`add_balloon` 如实降级 unsupported。本轮**无新代码缺陷**，但 smoke 新增 3 项因依赖仓库外私有夹具在任何 clone 上必 FAIL（12/15），且 `bom_table` 能力声明未带 SW 版本限定——SW2026 用户会被 "verified" 误导（balloon 降级前的同款模式）。

---

## 二、上轮反馈修复核验（全部通过）

| 上轮发现 | v0.8.4 修复 | 实测 |
|---|---|---|
| `add_bom_table` 包装层签名断链（P0，调用 100% 必炸） | 包装器删除 `bom_type`/`anchor_type` 透传，docstring 记录事故经过 | 实测工具正常执行，返回结构化 `bom_table_failed`（不再是 TypeError）✅ |
| 建议新增包装器↔实现签名校验 | `static_check.py` 新增 `check_wrapper_signatures`（AST 校验每个 `_json(module.func(...))` 的 kwargs 与实现签名匹配，docstring 直接引用本次事故） | static_check OK 且新守护生效 ✅ |
| `add_balloon` SW2026 失败应披露 | `check_capabilities` balloon=**unsupported**（why/why 证据含三入口错误码 + 引用"用户反馈在 SW2026 上 InsertBalloon 同样是 UNKNOWNNAME"）；README 表标注"本机不可用"；workaround 给 `add_note`+`add_sheet_line` 替代；`layout.py` 改走 `_call_method` 并按多签名逐次尝试、聚合错误 | 能力表实测 balloon=unsupported ✅ |
| 建议统一 `call_method` 双份定义 | **未处理**（P2，作者聚焦 P0 可理解） | bom_native.py:32 与 sw_session.py:85 仍各一份 |

**作者自行修复的 P0**（CONTRIBUTING 列为三起"静态全绿但代码坏掉"事故的防线）：
- `row["text"]` 先用后赋的排序崩溃 —— 本机对真实图纸实测 `list_drawing_annotations` ok ✅
- 尺寸去重（`unique_dimension_count`/`duplicates_skipped` 字段）—— 本机实测字段存在、口径一致 ✅
- MOD-DIAM 解码、README ROADMAP 过期守护（`check_stale_roadmap`）、smoke 末尾自清理已开文档（采纳环境备注）✅
- 新增 **CONTRIBUTING.md**：固化"改完必须真实调用"规则，附三起事故表——这是继"接口报错≠API 不存在"后又一条通用工程规则

---

## 三、新发现 1（中等严重性）：smoke 3 个新增项依赖仓库外私有夹具，任何 clone 必 FAIL

官方 smoke 本机实测 **12/15**：原有 12 项全 PASS，**新增 3 项全部 FAIL（0.0s，FileNotFoundError）**：

| 失败项 | 原因 |
|---|---|
| `list_drawing_annotations with hole callouts (P0)` | 依赖 `测试用例/` 目录下的 door/夹具（仓库外） |
| `verify_dimension_attachments dedupe` | `shutil.copy2(_model_dir()/door.SLDPRT)` —— door.SLDPRT 不在仓库 |
| `add_bom_table (native, populated)` | 显式断言 `"BOM fixture SAB1_0000_000.SLDASM not found"` —— glob `测试用例/工程图测试用例/**/SAB1_0000_000*.SLDASM` |

这与 v0.4.5 那轮"smoke 默认路径指向仓库外"**同类复发**：仓库不含 `测试用例/`，clone 后这三个新项永远 FAIL，且失败状态是 FAIL 而非 SKIP——无法区分"代码坏了"和"夹具没有"。另外这三项都是绘图域测试，却**不受 `--skip-drawing` 门控**（传了照样执行并失败）。

**修复建议**：夹具缺失时报 `SKIP (fixture missing)` 而非 FAIL（与 `--skip-*` 语义对齐）；或将极小夹具（door.SLDPRT / SAB1_0000_000）脱敏入库；或将三项纳入 `--skip-drawing` 门控。CONTRIBUTING 写的"smoke 16 项"在 clone 环境实际只能通过 12 项，README 的 smoke 说明宜同步。

---

## 四、新发现 2（中等严重性）：`bom_table` 能力声明缺 SW 版本限定，SW2026 实测失败

- `check_capabilities` 报 `bom_table: status="verified"`，why/evidence 只写"在本环境""SAB1_0000_000 实测"——**未写明是 SW2024 SP4**（layout.py docstring 有写：*"Verified working 2026-10-29 on SW2024 SP4"*）；
- 本机 **SW2026 实测**：`add_bom_table` 返回 `{"error":"bom_table_failed","message":"InsertBomTable4(Indented) returned None"}` —— 两步法第一步在 SW2026 就拿不到表；
- 这正是 balloon 降级前的同款模式：**"verified" 无版本限定 = 跨版本误导**。balloon 本轮已带版本限定（"在 SW2024 SP4 实测…用户反馈在 SW2026…"），bom_table 应同样处理（如 status 加 `verified_sw2024` 语义，或 why/evidence 补 SW 版本 + SW2026 失败记录）。

**顺带**：`bom_table_failed` 的 message 是裸 `str(exc)`，无排查指引——对比 `open_document` 的错误提示升级（带 `errors=` 错误码 + doc_type 提示），建议补充"已在 SW2024 SP4 验证；其他版本返回 None 时请核对配置名与 BOM 模板，可用 check_capabilities 查看证据"类 hint。

---

## 五、验证通过清单

| 验证项 | 结果 |
|---|---|
| 握手 `serverInfo.version` | 0.8.4 ✅ |
| tools/list | 48 个 ✅ |
| static_check | OK（`check_wrapper_signatures` + `check_stale_roadmap` + README 同步 + 属性陷阱 + dup-def 五道守护全部生效）✅ |
| smoke | 原有 12 项全 PASS（5m18s）；新增 3 项因夹具缺失 FAIL（见第三节）⚠️ |
| 七项历史回归（mass_basis、not-found、dry-run 探索性、no_match 守卫等） | 全 PASS ✅ |
| add_bom_table | 不再 TypeError；SW2026 下结构化失败（InsertBomTable4 返回 None）✅/⚠️ |
| add_balloon | 按声明如实 unsupported（能力表/README/工具三处口径一致）✅ |
| list_drawing_annotations / verify_dimension_attachments（P0 修复路径） | 实测 ok，去重字段存在 ✅ |
| CONTRIBUTING.md 入库 | "改完必须真实调用"规则 + 三起事故复盘 ✅ |

---

## 六、给作者的优先级排序

1. **P1**：smoke 夹具缺失项改 SKIP 语义（或入库小夹具）——当前 clone 环境永远 12/15，掩盖真实回归信号；
2. **P1**：`bom_table` 能力声明补 SW 版本限定（verified 仅限 SW2024 SP4），并在失败 error 中附版本上下文提示；
3. **P2**：smoke 新增项纳入 `--skip-drawing` 门控；`call_method` 统一到 sw_session 单份；
4. **P1（功能）**：`InsertBomTable4(Indented)` 在 SW2026 返回 None 的原因排查（可与 balloon 一并作 SW 版本差异专项）。

---

## 七、验收轨迹备注

v0.1.3 → v0.8.4 共十一轮验收。上轮（v0.8.2）两个 P0 + 签名守护建议全部闭环，`call_method` 双份定义挂账 P2。本轮无代码缺陷，两个新发现均为测试基础设施与能力声明口径问题。
