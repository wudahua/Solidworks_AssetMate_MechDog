# smoke 耗时：21 分钟 → 72 分钟（2026-11-09 排查）

**结论：没有代码级退化。单次调用全部正常。剩下一个我解释不了的 4 倍差异。**

## 数据

两次全量、同机、同零件、同样 46 条：

| 用例 | 0.8.250 | 0.8.264 | 变化 |
|---|---|---|---|
| paper scaling keeps views inside | 71.1s | 472.8s | **6.7x** |
| section + detail after produce_drawing | 104.3s | 498.7s | 4.8x |
| **reconcile ledger + delivery verdict** | **16.0s** | **383.2s** | **24x** |
| assembly drawing not an empty shell | 43.3s | 356.8s | 8.2x |
| edit tools by name | 70.1s | 312.1s | 4.5x |
| export_bom + csv | 73.1s | 244.3s | 3.3x |
| BOM quantity column adds up | 89.8s | 203.5s | 2.3x |
| a refused note leaves nothing | 21.4s | 100.5s | 4.7x |
| **合计** | **1220s** | **4133s** | **3.4x** |

`reconcile ledger` 最刺眼 —— 它**只读图不写图**，16 → 383 秒。

## 单次实测（同零件、19 条注解的图）

```
produce_drawing 234.4s
  reconcile     9.8s
  gaps         43.2s
  delivery     38.2s
```

`reconcile` **9.8 秒**，比 smoke 里的 383 秒快 **39 倍**。三个工具合计 **91 秒**，
而 smoke 里同一条 **383 秒**。

## 排除过的怀疑

| 怀疑 | 实测 | 结论 |
|---|---|---|
| `verify_dimension_attachments` 的 `GetAttachedEntityCount3`（0.8.263 加） | 8 尺寸 5.2s | 正常 |
| `reconcile_ledger` 本身 | 9.8s | 正常 |
| `delivery_check` | 38.2s | 正常 |
| `plan_dimension_gaps` | 43.2s | 正常 |
| `create_drawing` | 28.2s | 正常 |
| `produce_drawing` | 234.4s | 0.8.235 报告是 182s，**慢 29%**，但同一量级 |

## 没解释的

**同一条用例，单次 91 秒，smoke 里 383 秒。**

两者用同一张规模的图（都是 `produce_drawing` 建的 19 条注解），**所以不是规模差异**。

剩余可能，均无证据：

- 46 条**顺序执行**，前面的用例把 SolidWorks 状态堆住（本机同时 **140 个 UNKNOWN 文档**，
  `cleanup_temp_docs --execute` 只关得掉 2 个）
- 某处 COM 调用在「文档数量多」时退化

**不编原因。**

## 下次跑之前

1. **先关文档**。`python tools/cleanup_temp_docs.py --execute` 只处理回执里有路径的；
   本机 140 个 UNKNOWN 关不掉，只能手动在 SolidWorks 里关。
2. **`tests/smoke.py` 目前只能全跑**，不支持单条。改一个工具也要等一小时。
3. **基线**：0.8.250 = 46 条 / 1220s；0.8.264 = 46 条 / 4133s。

## 若要继续查

下一步是把 smoke 改成**能单跑一条**（`python tests/smoke.py --only "reconcile ledger"`），
这样：
- 每次改一个工具只跑相关 2-3 条，几分钟出结果
- 也顺便能定位「顺序执行 vs 单独执行」的 4 倍差异 —— 跑单条和跑全量各测一次，对比同一条的耗时

**这本身是一次测试框架改动，需要单独确认。**

---

## 补充（同日，加 `--only` 之后）

### `--only` 已实现

```bash
python tests/smoke.py --only "revolved part section"          # 47.7s（全量里 64.8s）
python tests/smoke.py --only "drawing chain" --only "reconcile ledger"
```

不匹配的用例**完全不执行**，summary 报「N passed / N run (M not selected)」。
依赖前面用例建图的那些，单跑会 `SKIP` 并说明缺什么 —— 这是正确答案，不是失败。

### 那 4 倍不是「顺序累积」

以为 46 条顺序跑把 SolidWorks 状态堆住了。**带上依赖单跑同一条：**

```
drawing chain (create/pdf/render/audit)  PASS    73.0s
reconcile ledger + delivery verdict      PASS   356.7s
```

**356.7 秒 —— 和全量里的 383 秒几乎一样。** 不是累积。

### 是「零件」的差别

| 零件 | 尺寸数 | `reconcile` 耗时 |
|---|---|---|
| `限位导向套.SLDPRT`（我手测） | 19 | **9.8s** |
| `P05_karetku.SLDPRT`（smoke 的 `drawing chain`） | 12 | **356.7s** |

**尺寸更少，却慢 36 倍。** 履带零件的可见边数量远多于法兰套，而 `reconcile` /
`delivery_check` / `plan_dimension_gaps` 都要在这一层枚举。

### 仍未解释

0.8.250 的 `reconcile ledger` 用**同一个零件**只要 **16 秒**，现在 **356 秒**。

**所以确实有退化，只是它不在单个工具的代码里，而在「对边多零件的枚举成本」上。**

下一步该做的：拿 `P05_karetku.SLDPRT` 单测 `list_drawing_edges` 和
`verify_dimension_attachments`，看哪个环节从秒级变成分钟级。
**现在有 `--only` 了，这类定位每轮只要一两分钟。**
