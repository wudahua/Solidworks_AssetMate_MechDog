# AssetMate v0.6.2 更新实测问题汇总

- **版本**：solidworks-assetmate-mechdog **0.6.2**（tag `v0.6.2`，commit `42e6614`，`fix: part/assembly doc_type mismatch, title-block text counted as notes; sync README and guard it in static check`）
- **上一轮被测**：v0.6.1（2026-10-28 上午，报告已提交）
- **测试环境**：Windows / SolidWorks `RevisionNumber 32.4.0`（2025 SP4）/ Python 3.11.5 64-bit venv / pywin32 312 / mcp 1.30.0 / Pillow 13.0.0
- **更新方式**：`git fetch --tags` → `git checkout v0.6.2` → `pip install -e ".[png]" --no-build-isolation`（依赖未变，重装仅刷新元数据）
- **结论**：**上轮 P0 已彻底修好并验证通过**；但暴露出一个更要紧的问题——**孔标注步骤在 `produce_drawing` 里拿到的是 20 s 预算，实测需要 35 s，超时后静默归零，直接把交付判定从「待设计审核」打到「仅供参考/未完成」**，且返回体里看不到任何超时痕迹。

---

## 一、核心问题（P1，建议优先）：孔标注超预算静默归零 → 交付判定翻转

### 现象：同一零件，结果在「有孔标注」与「没孔标注」之间摇摆

对同一零件（`P05_karetku.SLDPRT` 副本，35 特征）跑 `produce_drawing`（A3，默认参数）：

| 版本 | callouts | annotations | verdict |
|---|---|---|---|
| v0.6.1（上午首跑） | `created 8 / failed 0 / scanned 20` | note 82, dim 15 | 待设计审核 |
| v0.6.2（连跑 2 次） | `created 0 / failed 0 / scanned 12` | note 41 + title_block 41, dim 15 | 仅供参考/未完成 |
| **v0.6.1 回退对照**（为排除版本回归而回退重跑） | `created 0 / failed 0 / scanned 12` | note 82, dim 15 | 仅供参考/未完成 |

第三行是关键：**回退到 v0.6.1 也复现 0/12**，说明这不是 0.6.2 引入的回归，而是"孔标注是否在预算内跑完"本身不稳定——同一版本同一个零件，一次 8/20、一次 0/12。

结论翻转的**唯一**差别就是孔标注有没有在预算内跑完；`blockers` 随之从 `[]` 变成 `["图纸 0 处孔标注 + 0 处 Ø 尺寸 / 模型 8 个圆柱面候选"]`。

### 根因（两处）

1. **预算给低了**：`tools/pipeline.py:60` 默认 `hole_budget_s=20.0`；而 `tools/drawing.py:2665` 里独立调用 `add_hole_callouts` 的默认预算是 `budget_s=25.0`。也就是说"一条命令跑全流程"比"手动分步调"预算更紧。
2. **超时不留痕**：`tools/pipeline.py:173-175` 组装汇总时只取三个字段

```python
callouts={"created": callouts.get("created_count"),
          "failed":  callouts.get("failed_count"),
          "scanned": callouts.get("circular_edges")},
```

`budget_exceeded` 与 `elapsed_s` 被丢掉。于是"预算不够、只扫了 12 条边就收工"在下游看起来和"这个零件没有孔"完全一样——`failed 0` 更像成功。

### 证据

同一张图纸（`AM_TEST_03.SLDDRW`，produce_drawing 产出后）单独调 `add_hole_callouts`，让超时如实报出来：

```json
{ "circular_edges": 20, "holes_found": 10,
  "created_count": 8, "failed_count": 0,
  "budget_exceeded": true, "elapsed_s": 35.08, "saved": true, "ok": true }
```

35.08 s > 25 s > 20 s。同一张图、同一台机，独立调用能标 8 个，链式调用 0 个。

### 修复建议

1. `pipeline` 汇总里透传 `budget_exceeded` / `elapsed_s` / `holes_found`；
2. `budget_exceeded=True` 且 `created_count==0` 时，`blockers` 文案改为"孔标注因预算超时未完成（35 s / 预算 20 s）"，不要写成"图纸 0 处孔标注"——这两件事对使用者的行动指引正好相反；
3. 默认预算从 20 s 提到 ≥40 s，或按 `circular_edges` 线性给预算（实测约 1.7 s/条边，12 条 20 s 正好卡死）；
4. `hole_budget_s` 建议暴露到 `produce_drawing` 的入参里。

---

## 二、上轮问题的修复验证

| 上轮编号 | 问题 | v0.6.2 状态 | 验证方式 |
|---|---|---|---|
| P0 | `get_mass_properties` / `export_preview` 硬编码 `SW_DOC_ASSEMBLY`，零件全失败 | ✅ **已修** | `open_document` 改为按扩展名推断（`_DOC_TYPE_BY_EXT`），失败时回传 `errors/doc_type`。实测零件：`get_mass_properties` 返回 mass 23.12 kg / volume 0.0231 m³ / density 1000（占位密度有告警）；`export_preview` 正常出 1600×1200 PNG |
| P2 | README 工具数 35 vs 实际 39 | ✅ 已修 | `tests/static_check.py` 输出 `static check OK (39 tools, every module reference resolves, README in sync)`，README 已加 39 与三个新工具，`static_check` 现在会校验 README 同步 |
| P2 | README 能力边界表过时 | ✅ 已修 | 边界表已按 `check_capabilities` 重写（形位公差符号、孔标注背面、material_library、produce_drawing 多标尺寸都已如实披露） |
| P1 | 标题栏模板文字被算成 `note` | ⚠️ **只修了一半**，见下 | `totals` 多了 `title_block_text`，但 `annotation_count` 仍是 91 |
| P1 | `produce_drawing` 的 plan 不被 dimensions 采用 | ➖ 设计上接受 | README 边界表已明确写出"规划给保留 N 个，实际导入模型全部标记尺寸（无按值筛接口）"，`dimension_gap` 保留说明。**仍建议**在 `plan` 输出里就写明"仅供参考"，否则 `keep 5 / inserted 15` 依然会让人以为规划生效了 |
| P2 | 工具跑完不关文档、残留 `~$*.SLDDRW` | ❌ 未修 | 本轮 4 次出图后 `testdata/out/` 残留 3 个 `~$AM_TEST_0x.SLDDRW`，SolidWorks 文档数 4，需手工 `CloseDoc` 才清掉 |
| P3 | 零件被叫 `assembly` | ❌ 未修 | 零件调 `get_mass_properties` 返回体首字段仍是 `"assembly": "AM_TEST_01.SLDPRT"`；参数名仍是 `assembly_path` |

---

## 三、本轮新发现

### 3.1 模板文字过滤只覆盖了标题栏右下角（P1，延续上轮问题 3）

过滤条件是"`y < 页高×0.13` 且 `x > 页宽×0.5`"，只能命中标题栏下半部分。新建一张未加任何文字的 A3 图纸，`list_drawing_annotations` 结果：

| 类别 | 数量 | 实际内容 |
|---|---|---|
| `title_block_text` | 41 | 审核/校核/设计/标记/处数/签名… |
| `note` | 40 | `1` `2` `3` `4` `A` `B` `C` `D`（图框分区号，各 2 条）、`分区`、`更改文件号`、`签名`、`年 月 日`、`阶 段 标 记` |
| `dimension` | 15 | 真实标注 |
| `hole_callout` | 8 | 真实标注（但有误判，见 3.2） |
| `surface_finish` | 1 | 真实标注 |
| **`annotation_count`** | **91** | **真实标注只有 24 个，虚高 3.8 倍** |

对 `delivery_check` 的影响是实打实的：README 基线"真实图纸注释均值 46.5"，而一张**空**图纸在这里就能报 81 条模板文字 + 24 条真标注。

**建议**：别再靠坐标启发式，改为按内容与来源判定——① 文本命中模板词表（标记/处数/分区/签名/日期/阶段标记/单字母分区号/单数字分区号）直接归 `template_text`；② 尝试 `IsTitleBlock` / Figure 归属；③ 至少要保证 `annotation_count` 只统计 `dimension + hole_callout + surface_finish + geometric_tolerance + 用户 note`，把 `title_block_text` / `template_text` 排除在总数之外。

### 3.2 轮廓圆弧被当成孔标注，标注文本只有 "R"（P2）

`add_hole_callouts` 的 `created` 8 条，直径分别是 220 / 250 / 290 / 177 / 137 mm，`text` 全部是 `"R"`：

```json
{ "view": "工程图视图1", "diameter_mm": 220.0, "is_hole_callout": true, "text": "R" }
```

这些是履带轮的外轮廓圆弧，不是孔。`IsHoleCallout` 对普通圆弧也返回 true，于是：

- 图纸上得到的是 8 个"R"半径标注，不是 `n × Ø` 孔标注；
- `delivery_check` 会因此认为"图纸有孔标注"，把本该报的缺孔项放过去（设计原则第一条"工具返回成功 ≠ 图纸正确"在这里被自己的判定绕过了）。

**建议**：`IsHoleCallout` 之外再加两道闸——直径上限（如 ≤ 零件包围盒短边/4）或"该圆边是否为完整圆"；或要求回读文本含 `Ø` 才计入 `created`，`R` 文本单独归 `radius_callout` 并排除出孔标注统计。

### 3.3 其余（P3）

| 问题 | 说明 |
|---|---|
| 包仍未发布 PyPI | `https://pypi.org/pypi/solidworks-assetmate-mechdog/json` → 404，README 安装章节第一步仍是 `pip install .`，隐含"先 clone" |
| `smoke.py` 裸跑直接退出 | 默认路径指向未入库的 `测试用例/`，仓内无模型 |
| 响应键名残留 | 零件结果里叫 `assembly`，零件参数叫 `assembly_path` |

---

## 四、通过项（说明不是环境问题）

| 检查 | 结果 |
|---|---|
| `tests/static_check.py` | PASS：39 tools，README 同步校验也通过 |
| 装配体 `get_assembly_tree` | PASS：46 组件 |
| 装配体 `export_preview` → PNG | PASS：1600×1200 |
| 零件 `get_mass_properties` | PASS（上轮 P0 修复验证点） |
| 零件 `export_preview` → PNG | PASS（同上） |
| `produce_drawing` 10 步 | 全部 `ok: true`，`layout_warnings []`，`coverage 0.733` 稳定复现 |
| `drawing_audit` | PASS：2 视图、23 尺寸、`warnings []` |
| `list_circular_edges` | PASS：20 条圆边，两版图纸坐标与直径逐一一致（排除几何差异） |
| 中文 / 空格路径 | 无影响 |

---

## 五、优先级建议

1. **孔标注预算与透传（问题一）**——直接影响 `delivery_check` 的可信度，而这是项目的主打卖点。改动集中在 `pipeline.py` 3 行 + 1 个默认值。
2. **模板文字彻底过滤（3.1）**——`annotation_count` 91 vs 真实 24，基线全部不可比，建议和上轮思路一起收口。
3. **假孔标注（3.2）**——`R` 文本混进 `hole_callout` 会让"缺孔"这类阻塞项失效。
4. 关文档、命名、PyPI 三条随手改。
