# AssetMate v0.8.2 更新实测问题汇总

- **版本**：solidworks-assetmate-mechdog **0.8.2**（tag `v0.8.2` = `origin/main` = commit `e3aef6c`，`perf: edge enumeration 335s -> 47s (index-only) with mtime-keyed cache`）
- **上一轮被测**：v0.6.3（2026-10-28）——本次跨 **v0.6.3 → v0.8.2**，工具数 39 → **48**
- **环境**：Windows / SolidWorks `RevisionNumber 32.4.0`（2025 SP4）/ Python 3.11.5 64-bit venv / pywin32 312 / mcp 1.30.0 / Pillow 13.0.0
- **更新方式**：`git fetch --tags` → `git checkout v0.8.2` → `pip install -e ".[png]" --no-build-isolation`（依赖未变：mcp / pywin32 / Pillow）
- **结论**：上轮两条核心问题**都修了**（半径标注判定、totals 与行级统一），新增的 9 个工具（`place_views` / `delete_dimensions` / `add_note` / `add_balloon` / `add_bom_table` / `add_sheet_line` / `sheet_metal_qc` / `flat_pattern_report` / `set_view`）也在位；但 `list_drawing_annotations` 这次**直接崩了**——一次重构把 `row["text"]` 写到了 `row = {...}` 之前。

> 环境备注（与代码无关）：本机 `http_proxy/https_proxy` 指向 WorkBuddy 内部代理 `127.0.0.1:49933`，git/curl 访问 github 会 `schannel: failed to receive handshake`；用 `env -u http_proxy -u https_proxy git fetch` 或 `curl --noproxy '*'` 直连可通。

---

## 一、核心问题（P0）：`list_drawing_annotations` 在孔标注上抛 `UnboundLocalError`

### 现象

对一张含 8 个 `R` 标注的图纸（`AM_TEST_03.SLDDRW`，v0.6.2 生成的履带轮图纸）调用：

```json
{ "ok": false, "error": "annotations_failed",
  "message": "cannot access local variable 'row' where it is not associated with a value" }
```

同一台机、同一天，对另一张含 3 个孔标注的图纸（`AM_V082_DOOR.SLDDRW`）却返回正常。**能否崩，取决于扫描顺序。**

### 根因：`row` 在赋值之前被写

`src/solidworks_assetmate_mechdog/tools/drawing.py`：

```python
2431:                    row["text"] = callout_text or None        # ← 先在 row 上写
...
2443:                row = {"kind": kind, "name": name, "scope": scope}   # ← 才创建 row
```

Python 的 `row` 是函数局部变量，跨循环保留：

- **首次扫描对象就是孔标注** → `row` 从未赋值 → `UnboundLocalError` → 整个工具返回 `annotations_failed`（本次复现）；
- **之前已经 append 过一行** → `row` 还指向上一次的 dict → `text` 被写进**上一行**，本行的 `text` 丢失。门板图纸上看到 `RD3`/`RD4` 有 `<MOD-DIAM>`、`RD2`/`RD5` 为 `null`，与该错位一致。

所以这不是"某些图纸不支持"，而是**只要图纸里有孔标注就处在不确定状态**：要么崩，要么悄悄串行。

### 影响面

| 调用方 | 后果 |
|---|---|
| `pipeline.py:170`（`produce_drawing` 的 annotations 汇总） | 图纸一旦有孔标注，汇总可能整体失败或串行 |
| `survey.py:92`（`survey_drawings` 抽样画像） | 抽样遇到孔标注即中断/失真 |
| `drawing.py:2602`（自检项） | 直接返回 `annotations_unreadable` |
| `delivery_check` | **不受影响**（走自有扫描），实测门板仍正常出 10 项判定 |

### 建议

1. `row["text"]` 移到 `row = {...}` 之后，并按 kind 收口：

```python
row = {"kind": kind, "name": name, "scope": scope}
if kind in ("hole_callout", "radius_callout", "hole_callout_unverified"):
    row["text"] = callout_text or None
```

2. `callout_text` 在每轮循环开头显式置 `""`，避免跨轮残留（现在它是在 `if kind == "hole_callout" and name:` 分支里才初始化的）。
3. 加一条针对该函数的冒烟用例：拿一张**含孔标注**的图纸跑 `list_drawing_annotations`，断言 `ok: true`。这次的回归正是"被重构的代码没有覆盖到孔标注分支"。

---

## 二、本轮其他发现

### 2.1 尺寸被重复枚举，同名同值达 11 次（P1）

`verify_dimension_attachments`（`AM_TEST_03.SLDDRW`）：

```json
{ "dimension_count": 23, "attachment_summary": { "type_1": 8 }, "orphan_count": 15 }
```

15 个孤儿里**只有 3 个唯一**：

| 名称 | 值 | 出现次数 |
|---|---|---|
| `D1` | 275.0 | 11 |
| `D1` | 245.0 | 2 |
| `D1` | 215.0 | 2 |

两件事叠在一起：

1. **扫描重复**：同一条尺寸被反复命中（此前 v0.6.3 报的"totals 15 / 行 1"也是同一个病根）。
2. **名字不能当身份**：这张图纸上**所有尺寸都叫 `D1`**。于是"按 name 去重"会把 15 条误合并成 1 条（v0.6.3 的错），"不去重"就是现在的 11 倍膨胀——两头都不对。

**建议**：去重键用 `(view, position_m, value_mm)` 或直接持 COM 对象身份（`id(ann)` / `dispId`）；返回体里另给 `unique_dimension_count`，让使用者一眼看出 23 与 3 的差别。

### 2.2 `text` 返回未解析的格式码 `<MOD-DIAM>`（P2）

门板图纸的孔标注行：

```json
{ "kind": "hole_callout", "name": "RD3", "text": "<MOD-DIAM>" }
```

判定逻辑用 `"MOD-DIAM" in text` 认出这是直径标注（这点是对的），但把格式码原样抛给了使用者。要么解析成 `Ø8`（`GetText` 之外再读 `IDisplayDimension.GetDimension2` 的 `SystemValue`），要么至少在返回体里标明 `text_is_format_code: true`。

### 2.3 `sheet_metal_qc` 的圆柱清单：`null` 分不清"没请求"还是"取不到"（P2）

```json
{ "cylinder_count": null, "cylinder_error": null, "cylinder_radius_groups_mm": null }
```

`include_cylinders` 默认 `False`，所以三项都是 `null`——但 `cylinder_error` 也是 `null`，读者无法区分"你没让我查"和"我查失败了"。既然 v0.8 的 commit 把 cylinder inventory 当卖点（`feat: cylinder inventory for sheet-metal QC`），建议返回 `cylinders_not_requested: true` 或在 `note` 里写一句。

顺带：`min_flange_length` / `hole_to_bend_distance` / `bend_radius_vs_thickness` / `thickness_consistency` / `k_factor_source` 五项 `not_checked` 的说明写得很清楚（含"试过面相邻性判据"、"遍历 80 个圆柱面需 200 s"），这部分体验是好的。

### 2.4 仍未修（沿用前几轮）

| 问题 | 现状 |
|---|---|
| 工具跑完不关文档 | 本轮 4 次出图后残留 `~$AM_DOOR.SLDDRW`、`~$AM_TEST_03.SLDDRW`、`~$AM_V063.SLDDRW`、`~$AM_V082_DOOR.SLDDRW`，需手工 `CloseDoc` |
| 参数/键名 `assembly_path` / `"assembly"` 用于零件 | 未改：零件调 `get_mass_properties` 首字段仍是 `"assembly": "AM_TEST_01.SLDPRT"` |
| PyPI 无包（404） | 未改，README 仍是 `pip install .`（隐含先 clone） |
| `plan` keep N / 实际插入全部尺寸 | 已在 README 边界表披露为设计接受项 |

---

## 三、上轮问题的修复验证（均通过）

| 上轮问题 | v0.8.2 状态 | 证据 |
|---|---|---|
| 半径标注（`R`）在审计侧仍算孔标注 | ✅ 已修 | 新增 `radius_callout` / `hole_callout_unverified` 两个 kind；改为读 `IDisplayDimension.GetText(0..2)`，读不到再退回 `RD*` 名称前缀；门板实测 `radius_callout: 2` + `hole_callout: 3`，未再出现"R 冒充孔" |
| 计数与行级三方不一致 | ✅ 已修 | `totals` 改为从 rows 派生（`Counter(row["kind"] for row in rows)`）。履带轮图纸 `dimension 10` 与行数一致，`annotation_count 17 = 10+6+1`；门板 `41 = 24+2+3+5+1+6`，全部对得上 |
| 模板文字污染 | ✅ 保持 | `template_text` / `title_block_text` 单独归类，不再进 `annotation_count` |

---

## 四、通过项

| 检查 | 结果 |
|---|---|
| `tests/static_check.py` | PASS：**48 tools**，README 同步校验通过 |
| `tools/list` | 48 个工具，新增 9 个（`place_views`/`set_view`/`delete_dimensions`/`add_note`/`add_balloon`/`add_bom_table`/`add_sheet_line`/`sheet_metal_qc`/`flat_pattern_report`） |
| `produce_drawing`（门板，钣金，A3） | 10 步全 `ok: true`，`coverage 0.865`，`callouts created 3 / holes_found 13 / elapsed 21.35 s`，`annotations` 七类齐全，verdict `仅供参考/未完成`（blocker `suppressed_features` 属实） |
| `sheet_metal_qc`（门板） | PASS：`is_sheet_metal true`、`thickness 1.000 mm`（来源 Thickness@Cut-Extrude2）、1 道 `Edge-Flange1`、`bend_relief: fail` 并给出"R≥t、槽宽≥1.2t、槽长≥R+2t"的可执行建议 |
| `delivery_check`（门板） | PASS：10 项检查明细完整（`hole_callouts: 3 处 / 模型 17 个圆柱面候选`、`tolerances_present: fail 0 个带公差`、`dimension_attachments: fail 8/29 无关联`） |
| `get_mass_properties`（零件） | PASS：23.12 kg / 0.0231 m³，与 v0.6.x 结果逐位一致（无回归） |
| `verify_dimension_attachments` | 功能可用（`dimension_count 23` / `orphan 15`），问题见 2.1 |
| 中文 / 空格路径 | 无影响 |

---

## 五、优先级建议

1. **`row` 未定义（问题一）**——一行位置错误，P0 影响面：`produce_drawing` 的注释汇总、`survey_drawings`、自检项。建议同时补一条"含孔标注图纸"的冒烟用例，这类回归靠人眼很难发现。
2. **尺寸身份与重复枚举（2.1）**——它同时是"totals 忽大忽小"和"名字去重误合并"的病根，建议一次性收口。
3. **`<MOD-DIAM>` 解析（2.2）**、**cylinder 三字段口径（2.3）**——返回体可读性问题，改动小。
4. 关文档 / 命名 / PyPI 三条仍可随手带上。
