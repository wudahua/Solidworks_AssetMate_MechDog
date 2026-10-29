# AssetMate v0.6.3 更新实测问题汇总

- **版本**：solidworks-assetmate-mechdog **0.6.3**（tag `v0.6.3`，commit `b698e5d`，`fix: hole callout budget transparency, template text exclusion, outer-arc rejection`）
- **上一轮被测**：v0.6.2（同日，报告已提交）
- **环境**：Windows / SolidWorks `RevisionNumber 32.4.0`（2025 SP4）/ Python 3.11.5 64-bit venv / pywin32 312 / mcp 1.30.0 / Pillow 13.0.0
- **更新方式**：`git fetch --tags` → `git checkout v0.6.3` → `pip install -e ".[png]" --no-build-isolation`（依赖未变）
- **结论**：上轮三条问题（预算透明、模板文字、轮廓弧误判）**都已按建议修好并有实测证据**；但同一处判定换了条路径仍有漏洞——**创建侧已经拒掉半径标注，审计侧读不回来，旧图纸上的 `R` 仍被当成孔标注**，`delivery_check` 的缺孔阻塞项照旧会被掩盖。

> 环境备注（与代码无关）：本机 `http_proxy/http_ssl_proxy` 指向 WorkBuddy 内部代理 `127.0.0.1:49933`，`git fetch` / `curl` 访问 github 一律 `schannel: failed to receive handshake`；`env -u http_proxy -u https_proxy git fetch` 或 `curl --noproxy '*'` 才能连通。若作者复现本报告需要拉代码，先排除这一项。

---

## 一、核心问题（P1）：半径标注在审计侧仍被计为孔标注

### 现象

一张由 **v0.6.2** 生成的图纸（`AM_TEST_03.SLDDRW`，履带轮，图面 8 个 `R` 半径标注），在 **v0.6.3** 下审阅：

```json
{ "totals": { "note": 7, "title_block_text": 27, "template_text": 48,
              "hole_callout": 8, "surface_finish": 1, "dimension": 15 } }
```

有 8 个 `hole_callout`，但每个行记录的 `text` 都是 `null`，`name` 是 `RD1…RD8`。

### 根因：文本读不出来，判定就静默通过

`drawing.py` 新增的判定写的是：

```python
callout_text = str(ann.GetSpecificAnnotation.GetText(0) or "")   # → 抛异常
...
upper = callout_text.upper()
if upper.startswith("R") and ...:      # "" 不以 R 开头 → 不归为 radius_callout
```

而这批对象的 `GetSpecificAnnotation` 返回的其实是**尺寸对象**（名字前缀 `RD`＝半径尺寸），上面根本没有 `GetText`。COM 侧实测：

| 调用 | 结果 |
|---|---|
| `ann.GetSpecificAnnotation.IsHoleCallout` | `True`（这就是误判源头） |
| `ann.GetText` / `GetText(0)` / `GetText(1)` | 全部 `AttributeError` |
| `ann.GetName` | `RD1`、`RD2`… |

`try/except: callout_text = ""` 把异常吃掉，于是"读不到文本"被当成"文本不是 R"，原样计入 `hole_callout`。

### 影响

- **创建侧已修、审计侧没修**：v0.6.3 自己新出的图纸不会再产生 `R` 标注（下面通过项已验证），但**任何在 0.6.3 之前生成的图纸、或别人给的图纸**，`R` 标注仍然冒充孔标注。
- `delivery_check` 读的是 `totals["hole_callout"]`（`drawing.py:2426`），只要它 >0 就认为"图纸有孔信息"，**缺孔这项阻塞项因此失效**——这正是 v0.6.2 报告里那条"工具绕过自己的检查"，换了个入口又出现。

### 建议

1. 判定不要只依赖 `GetText`。`ann.GetName` 前缀足够可靠且零成本：`RD*` → `radius_callout`，`D*` → `dimension`，`H*`/含 `Ø` → `hole_callout`。
2. 文本必须读不到时，标 `text_unreadable: true` 并**保守不计入** `hole_callout`（宁可报"孔标注存疑"，也不要让它冒充成功）。
3. 兜底再加一层几何条件（直径 ≤ 视图宽度的 40%，与创建侧的拒绝阈值保持一致），两侧同一套判据，避免再次出现"写的按 A 判、读的按 B 判"。

---

## 二、上轮问题的修复验证（均通过）

| 上轮问题 | v0.6.3 状态 | 证据 |
|---|---|---|
| 孔标注预算静默归零 | ✅ 已修 | `hole_budget_s` 20 → **45**；汇总新增 `holes_found` / `budget_exceeded` / `elapsed_s`；新增 `callout_warning` 文案区分"没孔"与"超时"。履带轮实测返回 `elapsed_s: 55.88`、`failed: 20`，门板实测 `elapsed_s: 2.76` |
| 模板文字污染注释统计 | ✅ 已修 | 新增 `TEMPLATE_TEXT` 词表 + 1~2 字符规则 + `template_text` 类别；`annotation_count` 改为排除 `title_block_text` / `template_text`。履带轮图纸：**91 → 8**（title_block 27 + template_text 48 已剥离） |
| 轮廓圆弧被当成孔标注（`R`） | ✅ 创建侧已修 | 直径 > 视图宽度 40% 的圆在插入前就拒掉：履带轮 20 条轮廓弧全部 `skipped: circle is >40% of the view width`，不再落图 |
| 该规则会不会误杀真孔 | ✅ 未误杀 | 门板（钣金，Ø8/Ø15 真孔）实测 `holes_found: 13`，`created: 3`，`failed: 10`（背面边 null），`elapsed 2.76 s` |

---

## 三、本轮新发现

### 3.1 `annotation_count`、`totals`、行级 `annotations` 三方对不上（P2）

| 图纸 | `totals.dimension` | 行级 `dimension` 行数 | `annotation_count` | totals 真实合计（dim+hole+center+surface+note） |
|---|---|---|---|---|
| 履带轮（AM_V063） | 15 | **1**（只有 `D1`） | **8** | 23 |
| 门板（AM_DOOR） | 24 | **6** | **23** | 42 |

机制在 `list_drawing_annotations` 里：`counts[kind] += 1` 在**按 `ann.GetName` 去重之前**执行，而行是在去重 `continue` **之后**才 append。同一次扫描中多个尺寸返回同名（`D1` 被重复命中），于是行丢掉了、计数留下了。

`annotation_count` 是这张图对外的"证据条数"，现在它既不等于行数也不等于 totals：91 → 8 这个变化让"虚高"变成了"漏报"，两种都不可用。

**建议**：一次循环同时产出 rows 与 totals（`totals = Counter(row["kind"] for row in rows)`）；去重键改成 `(scope, name, position_m)`，不要只按 name。

### 3.2 全部候选被拒时，`scanned: 0` 与 `failed: 20`、`budget_exceeded: false` 与 `elapsed 55.88 s` 互相打架（P2）

履带轮返回：

```json
{ "created": 0, "failed": 20, "scanned": 0,
  "holes_found": 0, "budget_exceeded": false, "elapsed_s": 55.88 }
```

- 明明扫到并拒绝了 20 条圆边，`scanned` 却是 0（该字段直接取 `circular_edges`，而它只在候选通过筛选后才累加）——读者第一反应会是"这条视图没扫到东西"。
- `elapsed_s 55.88` 已经超过 45 s 预算，`budget_exceeded` 却是 `false`。原因是预算时钟只在插入循环里检查，候选全被拒时循环根本没进；耗时都花在前面的扫边上。

**建议**：`scanned` 用"扫到的圆边总数"，另加 `rejected` 细分；`elapsed_s` 拆成 `scan_s` / `insert_s`；`budget_exceeded` 用同一时钟判定（`elapsed > budget`），否则数字自相矛盾。

### 3.3 仍未修（沿用上轮编号）

| 问题 | 现状 |
|---|---|
| 工具跑完不关文档 | 本轮 2 次出图后残留 `~$AM_DOOR.SLDDRW`、`~$AM_V063.SLDDRW`，SolidWorks 文档数 5，需手工 `CloseDoc` |
| 参数/键名 `assembly_path` / `"assembly"` 用于零件 | 未改 |
| `plan` keep 5 / 实际插入 15 | 已在 README 边界表披露为设计接受项，`dimension_gap` 保留说明 |
| PyPI 仍无包（404） | 未改 |

---

## 四、通过项

| 检查 | 结果 |
|---|---|
| `tests/static_check.py` | PASS：39 tools，README 同步校验通过 |
| 零件 `get_mass_properties`（履带轮副本） | PASS：23.12 kg / 0.0231 m³，`mass_basis` 正确告警 1000 kg/m³ 为占位密度 |
| 零件 `export_preview` → PNG | PASS：1600×1200 |
| `produce_drawing`（履带轮，A3） | 10 步全 `ok: true`，`layout_warnings []`，`coverage 0.733` |
| `produce_drawing`（门板，钣金，A3） | 分类正确识别"钣金"，`verdict 仅供参考/未完成`，blocker `suppressed_features`（Flat-Pattern 被抑制，属实） |
| 孔标注（真孔门板） | `holes_found 13 / created 3 / failed 10 / elapsed 2.76 s`，与能力表"背面边不可标"一致 |
| 轮廓弧拒绝 | 履带轮 20/20 拒绝，理由明确 |
| 模板文字剥离 | `title_block_text 27` + `template_text 48` 单独归类，不再进入真实注释 |
| 中文 / 空格路径 | 无影响 |

---

## 五、优先级建议

1. **半径标注的审计侧判定（问题一）**——`GetName` 前缀两行就能修，但它决定 `delivery_check` 会不会漏报缺孔，属于"判定可信度"级别。
2. **计数与行级统一（3.1）**——一次循环产出，去重键带上位置，顺手把 `annotation_count` 的可信度修回来。
3. **`scanned` / `budget_exceeded` 语义（3.2）**——纯返回体口径，改起来最小，但对读返回体的人影响直接。
4. 关文档、命名、PyPI 三条仍可随手带上。
