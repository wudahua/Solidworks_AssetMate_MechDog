# AssetMate v0.8.4 更新实测问题汇总

- **版本**：solidworks-assetmate-mechdog **0.8.4**（tag `v0.8.4` = `origin/main` = commit `f8bc23c`，`fix: P0 add_bom_table wrapper signature drift; add wrapper-vs-impl signature guard; downgrade add_balloon`）
- **上一轮被测**：v0.8.2（2026-10-29 上午）
- **环境**：Windows / SolidWorks `RevisionNumber 32.4.0`（2025 SP4）/ Python 3.11.5 64-bit venv / pywin32 312 / mcp 1.30.0 / Pillow 13.0.0
- **更新方式**：`git fetch --tags` → `git checkout v0.8.4` → `pip install -e ".[png]" --no-build-isolation`（依赖未变）
- **结论**：上轮 P0（`row`/`text` 顺序崩溃）、`MOD-DIAM` 解码、wrapper 签名漂移**都修了并验证通过**；`add_bom_table` 实测能出原生 BOM。但上轮 P1（尺寸重复枚举）**只修了一半**——`dimension_count` 去重了，`orphans` 和 `attachment_summary` 没有，三个数字现在互相打架。

> 环境备注（与代码无关）：本机 `http_proxy/https_proxy` 指向 WorkBuddy 内部代理 `127.0.0.1:49933`，git/curl 访问 github 会 `schannel: failed to receive handshake`；用 `env -u http_proxy -u https_proxy git fetch` 或 `curl --noproxy '*'` 直连可通。

---

## 一、核心问题（P1）：`verify_dimension_attachments` 去重只覆盖了计数，没覆盖孤儿清单

### 现象：同一张图纸，三个数字凑不上

`AM_TEST_03.SLDDRW`（履带轮，A3）：

| 指标 | v0.8.2 | v0.8.4 | 说明 |
|---|---|---|---|
| `dimension_count` | 23 | **11** | 已去重 |
| `orphan_count` | 15 | **15** | 未去重 |
| `orphans` 去重后唯一条数 | 3 | **3** | 仍是 3 |
| `attachment_summary`（type_1） | 8 | **8** | 未去重 |
| `type_1 + orphan_count` 与 `dimension_count` 对得上吗 | 8+15=23 ✓ | **8+15=23 ≠ 11 ✗** | |

`orphans` 里最夸张的一条：`D1 / 275.0` 出现 **11 次**，`D1 / 245.0` 2 次，`D1 / 215.0` 2 次——15 条里只有 3 个不同的东西。

### 根因（`drawing.py` `verify_dimension_attachments`）

去重逻辑加在了统计之后：

```python
counts[kind] = counts.get(kind, 0) + 1          # ← 先计数（原始 23 次）
if not types:
    orphans.append({...})                       # ← 先记孤儿（原始 15 次）
dedupe_key = (row.get("view"), row.get("name"),
              tuple(row.get("position_m") or ()),
              row.get("value_mm"))
if dedupe_key in seen_dims:
    duplicates_skipped += 1
    continue                                    # ← 只有这里才去重
```

只有 `rows`（`dimension_count`）走到了 `continue`，另外两处已经"记账"了。

另外 `dedupe_key` 里带了 `row.get("position_m")`，但这个函数**从未给 `row` 赋 `position_m`**，所以该项恒为 `()`，键实际退化成 `(view, name, value_mm)`。而这张图纸上所有尺寸都叫 `D1`/`D3`（`D1` 同名不同值：275.0 / 245.0 / 215.0 / 32.5 / 8.0 / 10.0），同名同值的真不同尺寸会被误合并——11 这个数也就不能当真。

### 建议

1. 把 `counts[kind] += 1` 和 `orphans.append(...)` 一起移到 `if dedupe_key in seen_dims: continue` **之后**，让三个数字同源。
2. 补 `row["position_m"] = list(ann.GetPosition)`（其他函数已有这个写法），去重键才有区分度。
3. 返回体里同时给 `raw_hits`（原始命中 23）和 `unique_dimension_count`（11），让使用者看得见"23 → 11"是被合并掉的，而不是数字凭空变化。
4. 顺带：`duplicates_skipped` 已经统计了却没返回，加上它。

---

## 二、上轮问题的修复验证（均通过）

| 上轮问题 | v0.8.4 状态 | 证据 |
|---|---|---|
| `list_drawing_annotations` 抛 `UnboundLocalError: row` | ✅ **已修** | 同一张 `AM_TEST_03.SLDDRW`：v0.8.2 返回 `annotations_failed / cannot access local variable 'row'`；v0.8.4 返回 `ok: true`，8 个 `R` 正确归入 `radius_callout`（`text: "R"`），`annotation_count 25 = 10 dim + 8 radius + 6 note + 1 surface` |
| 孔标注文本返回 `<MOD-DIAM>` | ✅ 已修（部分） | 现在返回 `"Ø"`；但仍未拼上数值，见 3.1 |
| 尺寸重复枚举（上轮 2.1） | ⚠️ 部分修 | `dimension_count` 23 → 11，但 `orphans` / `attachment_summary` 未去重，见问题一 |
| `add_bom_table` 包装签名漂移 | ✅ 已修，实测可用 | 对门板图纸调用：`created: true`、`native_bom: true`、`rows 2 / columns 4 / data_rows 1`、中文表头 `["项目号","零件号","说明","数量"]`、`sample_data [["1","AM_DOOR","","1"]]` |
| `static_check` 覆盖不足 | ✅ 已加强 | 新增 wrapper-vs-impl 签名守卫，输出 `static check OK (48 tools, every module reference resolves, README in sync)`；仓库同时加了 `CONTRIBUTING` 与 smoke 对 annotations / BOM / dedupe 的覆盖 |

---

## 三、本轮其他发现

### 3.1 孔标注文本解码只到符号，没有数值（P2）

```json
{ "kind": "hole_callout", "name": "RD3", "text": "Ø" }
```

从 `<MOD-DIAM>`（v0.8.2）到 `Ø`（v0.8.4）确实进了一步，但图纸上真正要看的是 `Ø8`。`row` 里已经有 `value_mm` 可读（`IDisplayDimension.GetDimension2(0).SystemValue`），拼接后返回 `"Ø8"` 成本极低；若一张标注里有数量前缀（`4 X Ø8`），可先只拼单值并在文档里说明解析边界。

### 3.2 仍未修（沿用前几轮）

| 问题 | 现状 |
|---|---|
| 工具跑完不关文档 | 本轮 3 次出图/审图后残留 `~$AM_TEST_03.SLDDRW`、`~$AM_V082_DOOR.SLDDRW`、`~$AM_V084_DOOR.SLDDRW`，需手工 `CloseDoc` |
| 参数/键名 `assembly_path` / `"assembly"` 用于零件 | 未改 |
| PyPI 无包（404） | 未改，README 仍是 `pip install .`（隐含先 clone） |
| `plan` keep N / 实际插入全部尺寸 | 已在 README 边界表披露为设计接受项 |

---

## 四、通过项

| 检查 | 结果 |
|---|---|
| `tests/static_check.py` | PASS：48 tools、README 同步、**wrapper-vs-impl 签名守卫**（本次新增） |
| `list_drawing_annotations`（含 8 个 R 标注） | PASS（上轮崩溃点） |
| `add_bom_table`（门板图纸） | PASS：原生 BOM，1 数据行，中文表头 |
| `produce_drawing`（门板，钣金，A3） | 10 步全 `ok: true`，`coverage 0.865`，`callouts created 3 / holes_found 13`，`radius_callout 2` + `hole_callout 3`，verdict `仅供参考/未完成`（blocker `suppressed_features` 属实） |
| 孔标注耗时 | `elapsed_s 6.38`（v0.8.2 同零件同参数为 21.35 s），边枚举缓存/索引优化生效 |
| 模板文字分离 | `template_text 47` + `title_block_text 29`，均不进 `annotation_count` |
| 中文 / 空格路径 | 无影响 |

---

## 五、优先级建议

1. **问题一（去重半成品）**——改动就是挪两行 + 补一个字段，但它决定"这张图纸有几个尺寸悬空"这个核心数字是否可信，建议优先。
2. **3.1 `Ø` 补数值**——一行拼接，直接影响 `delivery_check` 的孔标注明细可读性。
3. 关文档 / 命名 / PyPI 三条仍可随手带上。
