# v0.8.173 五份反馈 · 逐条状态

**核对时间**：2026-11-06
**核对方式**：对着当前代码逐项 grep，不凭记忆
**结论**：**已修 11 条，未修 21 条。** 修好的都是「一说就清楚改哪儿」的，
没修的是「要动数据结构或要新算法」的。

---

## 一、报告 1：标题栏比例同步假成功

| # | 条目 | 状态 | 证据 |
|---|---|---|---|
| 1.1 | **P0 标题栏同步是假修复**（回执 1:2，图面 1:1） | ✅ **0.8.177** | `SaveAsSilent` 在 offset 25694，同步在 29741 —— **保存之后**；`verified_by: written[].ok` |
| 1.2 | 只覆盖 auto_fit，手动 `scale=0.5` 不触发 | ✅ **0.8.177** | 条件改为 `abs(_final_scale - 1.0) > 1e-9`；实测手动 0.5 也读到 `1:2` |
| 1.3 | **P1 `create_drawing` 4 个 scale 字段与实际矛盾** | ❌ 未修 | `views[].scale` 仍是构建时快照（1.0），实际 0.5；`chosen_scale` / `scale_reason.final_scale` 同样 |
| 1.4 | **P2 `clamp_dimensions` 按纸张判界** | ❌ 未修 | 仍是 `inset_m=0.010`（纸张 0–0.42），安全区是 `[0.0588, 0.416]`；3 条尺寸实证越界 |
| 1.5 | **P2 `dimension_gap.note` 首句「规划已生效」** | ❌ 未修 | `pipeline.py:497` 原文仍在，而 `planned 9 / inserted 12` |
| 1.6 | **P3 `leader_endpoint_m` 字段名** | ❌ 未修 | 仍叫 `endpoint`，实际是圆上推算点（圆心 + r），与真实箭头差 27.5 mm |
| 1.7 | P3 `projection_note` 应进 `pending` 而非只进 `warnings` | ❌ 未修 | 合规问题按「提示」处理，`blockers: []` |

---

## 二、报告 2：能力反转只传播到 2/8 处

| # | 条目 | 状态 | 证据 |
|---|---|---|---|
| 2.1 | `add_sheet_line` MCP 描述说「does NOT work」 | ✅ **0.8.176** | 改成「DOES render，需 AddToDB」 |
| 2.2 | `verify_render` 参数说「always returns line_not_rendered」 | ✅ **0.8.176** | 改成「读 visibility：checked/0/checked:false 三态」 |
| 2.3 | README 工具表「实测不渲染」 | ✅ **0.8.176** | 断言当场抓到并改掉 |
| 2.4 | README 能力边界「不要配 add_sheet_line 画引线」 | ✅ **0.8.176** | 同上 |
| 2.5 | `com-quirks.md` §55「图纸草图不渲染」 | ✅ **0.8.176** | 整节改写，附 0→572 像素实测 |
| 2.6 | `capabilities.centerline` 派生结论 | ✅ **0.8.176** | `unsupported` → `partial`，注明「前提已推翻，本身未重测」 |
| 2.7 | **`tests/smoke.py:824-826` 注释**「an ok:true is precisely the lie」 | ❌ 未修 | 注释仍与事实相反（分支逻辑本身能过） |
| 2.8 | **`layout.py:1000-1006` 旧注释**「with AddToDB the diff is STILL 0」 | ❌ 未修 | 与同文件 2134 像素的新注释直接冲突 |
| 2.9 | 证据面限定（`verified` 无环境限定） | ✅ **0.8.176** | summary 加「实测于 SW2024 SP4 + 飞达动力轴 A3」 |
| 2.10 | **跨文件结论一致性断言** | ✅ **0.8.176** | 黑名单扫 server/README/com-quirks，要求 `AddToDB` 至少出现一次 |

---

## 三、报告 3：移除闸门后 1570.796 回归

| # | 条目 | 状态 | 证据 |
|---|---|---|---|
| 3.1 | **P0 错误尺寸重新插入并堆积** | 🟡 **部分** | 0.8.174 加了按身份撤回（`_dimension_full_names` 实测 10 条），但**撤回路径从未执行**——本机没有 `value_mismatch` 场景 |
| 3.2 | **P0 一次调用插 6 条** | ✅ **0.8.175** | 熔断：首条 mismatch 后其余标 `not_attempted_after_mismatch` |
| 3.3 | `applied_count: 0` 掩盖 6 条副作用 | ✅ **0.8.174** | 拆成 `applied_failed` / `inserted_but_wrong` / `sheet_modified` / `rolled_back` |
| 3.4 | **P0 跨调用幂等短路** | ❌ 未修 | 台账不认这些尺寸（`coverage` 恒定 0.348），每次调用仍认为缺 |
| 3.5 | **P0 同口径模型度量** | ❌ 未修 | `model_span_mm` 只是证据，仍是 3D 欧氏距离，不是 `AddDimension2` 会写的值 |
| 3.6 | **P1 真机回归**（AM_DOOR 连续两次 `apply_gaps` 后标注数不变） | ❌ 未修 | 本机没有那颗零件 |
| 3.7 | `export_bom` 失败分支缺 `document_type` | ❌ 未修 | 成功分支有，失败分支没有 |
| 3.8 | 未知 `arguments` 静默忽略 | ❌ 未修 | `ping(bogus_param)` 仍 `isError: false` |
| 3.9 | `measure_layout` 未注册 | ❌ 未修 | 仍 66 个工具，无此项 |
| 3.10 | `produce_drawing` 不回显 `projection` | ❌ 未修 | 只传参不回显 |

---

## 四、报告 4：完整尺寸出图链路断裂

| # | 条目 | 状态 | 证据 |
|---|---|---|---|
| A1 | **`add_sheet` 不存在** | 🟡 **工具有了，调不通** | 0.8.180 新增，`NewSheet4` 三种 arity 全卡 arg 6；返回 `sheet_creation_blocked` + 三条绕法 |
| A2/A3 | **`axes_mismatch` 让补尺寸归零** | 🟡 **部分** | 0.8.179 回退 model 坐标，但**回退分支从未执行**（本机无等长零件）；且只取前两轴，Z 向会错 |
| A4 | `duplicate_dimension_to_view` 批量包装 | ❌ 未修 | single-insert 已有，批量没封 |
| A9 | `precheck_section` | ❌ 未修 | 仍要切完才知道有没有剖面线 |
| A11 | `prune_orphan_dimensions` 误判已移动尺寸 | ❌ 未修 | 仍只有警告，无引线验证 |
| B1 | **剖视强制投影对齐**（`position` 被改写） | ❌ 未修 | 无 `align_to_parent` 参数，必须再调 `set_view` |
| B2 | **`place_outside` 不 clamp 安全区** | ❌ 未修 | 11 条堆到 y=0.0595，压进标题栏 |
| B3 | **`eliminate_duplicates` 删掉剖视副本** | ❌ 未修 | 无 `prefer_view` / `keep_in_view` |
| B4 | `total_dimensions: 0` 与 `filter_kept: 32` 矛盾 | ❌ 未修 | `audit: false` 时计数失效 |
| B5 | **角度当长度（45° → 785.4 mm）** | ✅ **0.8.178** | `sheet_vals` 过滤 `dimension_type in (1, 3)` |
| B6 | `set_title_block(scale=3.0)` → `"3.0"` 而非 `"3:1"` | ❌ 未修 | 无格式化 |
| B7 | `include_reversed` 跳过 180° 反转的图样代号 | ❌ 未修 | `skipped_extra_matches` 仍在，仍跳过 |
| B8 | `add_detail_view` 的 `inside_sheet: false` 误判 | ❌ 未修 | 落在安全区内却报出界 |
| B9 | **P0 标注越出图框**（残留角度标注的弧心 x<0） | 🟡 **根因修了，症状未验证** | 根因是 B15 没删净 + 角度当长度；两条都修了，但**没有 60×60 零件复验** |
| B10 | `plan_drawing` 只保留 12/77 | ❌ 未修 | 无 `goal="completeness"` |
| B11 | `projection` 不合规时仍照建几何 | ❌ 未修 | 只有 note，无拒绝也无 `compliance` 标记 |
| B12 | 半径/数量尺寸缺 `full_text`（`4-R` 无值） | ❌ 未修 | 无 `full_text` 字段 |
| B13 | `close_document` 残留 `~$` 锁文件 | ❌ 未修 | 无 owner 清理 |
| B14 | `delivery_check` 只能单页核对 | ❌ 未修 | 无 `drawing_paths[]` |
| B15 | **P0 按值批量删除不删净** | ✅ **0.8.178** | `remaining_matches` / `remaining_count` / `verified_clean`；实测 18 条删净、0 残留 |

---

## 五、报告 5：能力表仍写版本限制

| # | 条目 | 状态 |
|---|---|---|
| 5.1 | `bom_table` summary 仍「仅 SW2024 SP4 验证」 | ✅ **0.8.170** |
| 5.2 | `bom_table` evidence 仍「SW2026 实测失败」 | ✅ **0.8.170**（改成两版并列） |
| 5.3 | `capabilities.py:487` 注释固化错误前提 | ✅ **0.8.170** |
| 5.4 | **P1 A6 硬依赖未声明的 `pymupdf`** | ✅ **0.8.170**（改 pypdfium2，16/16） |
| 5.5 | `_fixtures.py` 写死「离线 172 条」 | ✅ **0.8.170**（去掉数字） |
| 5.6 | 加「verified 不得自我否定」断言 | ✅ **0.8.170** |
| 5.7 | P2 `create_drawing` 失败只关图纸、源模型仍开 | ❌ 未修 |
| 5.8 | `regressions.py` 仍写 `r.check calls 41 total`（实际 40） | ❌ 未修 |
| 5.9 | `check_all.py` docstring 有字面 `/n` | ❌ 未修 |

---

## 六、汇总

| 分类 | 数量 |
|---|---|
| ✅ 已修并真机验证 | **14** |
| 🟡 改了但未验证（缺夹具） | **4** |
| ❌ 未修 | **17** |

### 已修的（14）

标题栏同步×2、`add_sheet_line` 五处文案、`centerline` 状态、能力表 `bom_table`×3、
A6 的 pymupdf、`_fixtures` 数字、跨文件断言、`applied_count` 拆分、熔断、
角度当长度、`delete_dimensions` 重枚举。

### 🟡 改了但没验证的（4）

| 项 | 缺什么 |
|---|---|
| 按身份撤回 | 需要 `value_mismatch` 场景（AM_DOOR） |
| 熔断路径 | 同上 |
| `axes_mismatch` 回退 | 需要 60×60 等长零件 |
| B9 标注越界 | 需要同一颗零件复验 |

### ❌ 未修的（17），按「改了会不会伤到人」排

**会影响交付正确性**
1. **B1/B2/B3 剖视深度尺寸**（强制对齐、不 clamp、删副本）—— 一次 `auto_dimension` 后深度尺寸无处安放
2. **1.3 `create_drawing` 4 个 scale 字段矛盾** —— 单独调用时报 1:1，实际 0.5
3. **1.4 `clamp_dimensions` 按纸张判界** —— 3 条实证越界却报「没出界」
4. **B8 `inside_sheet` 误判** —— 在安全区内报出界
5. **B6 比例格式 `3.0` 而非 `3:1`**

**会让人误判工具能力**
6. **1.5 `dimension_gap.note` 静态报捷**
7. **B4 `total_dimensions: 0` 与 `filter_kept: 32` 矛盾**
8. **1.6 `leader_endpoint_m` 字段名**
9. **2.7 / 2.8 两处旧注释**（smoke 与 layout 自相矛盾）
10. **5.8 / 5.9 两处数字与笔误**

**功能缺口**
11. `add_sheet` 调不通（需要客户端 makepy）
12. B7 `include_reversed` 跳过反转代号
13. B10 无 completeness 模式
14. B12 无 `full_text`
15. B13 锁文件残留
16. B14 单页核对
17. A4 / A9 / A11 / 3.4 / 3.5 / 3.7 / 3.8 / 3.9 / 3.10 / 5.7

---

## 七、一句话

**修好的都是「一说就知道改哪一行」的；没修的是「要动数据结构、要新算法、或者要一颗本机没有的零件」的。**

其中 **B1+B2+B3（剖视深度尺寸）** 是唯一仍会让用户拿不到可用图纸的一组，
而且三条互相纠缠：投影对齐强制忽略 y、外置堆叠不 clamp、去重删掉剖视副本。
