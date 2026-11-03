# 验收说明 · 2026-11-03 性能优化轮（v0.8.101 → v0.8.110）

> 依据：`项目规划 - AssetMate_MechDog/工程图性能优化_实施包/`
> 规格：`AssetMate_工程图性能优化_实施规格_20261103.md`

---

## 一、当前状态

| 项 | 值 |
|---|---|
| 版本 | **v0.8.110**（`pyproject.toml` 与 `__init__.py` 一致） |
| 工具数 | 64 |
| 离线断言 | **133 / 133** |
| 静态检查 | 64 tools，参数全文档化 |
| 真机 smoke | **35 / 35**（最近一次 12m59s） |
| 工作区 | 干净（0 uncommitted） |
| HEAD | 见 `git log -1` |

### 一条命令验证

```bash
cd "D:/WorkBuddy_space/Solidworks_AssetMate_MechDog"
python tests/static_check.py && python tests/check_all.py
```

---

## 二、本轮做了什么

按规格的 **PR0 → PR3 提前 → PR3 余项** 顺序，**没有改任何出图逻辑**，
只做「消除重复工作」和「诊断功能默认关」。

| PR | 版本 | 内容 |
|---|---|---|
| **PR0** | v0.8.101 | `PerfRecorder` + `perf_hooks` + 9+ 处埋点 + `tools/perf_baseline.py` |
| **PR3 提前** | v0.8.102 | `build_feature_ledger` 缓存 |
| — | v0.8.103 / .104 | A/B 方法论（交错采样、`UNINSTRUMENTED` 余量） |
| — | v0.8.105 | `list_drawing_annotations` 作用域缓存 |
| — | v0.8.106 | `detect_same_value` 默认关 |
| — | v0.8.107–.109 | `dim_outside` 全拆解（1.4 s 硬成本，无可省） |
| — | v0.8.110 | 版本号修正（见第五节） |

### 实测收益

| 相位 | 之前 | 现在 | 手段 |
|---|---:|---:|---|
| `ledger` | 34.8 s | **~16 s** | 缓存（一次出图 5 次 → 2 次真读） |
| `plan` | 7.7 s | **~0** | 同上，完全命中 |
| `delivery_check` | 42.6 s | **~19 s** | 注解缓存 |
| `dimension_total` | 29.0 s | **~6.8 s** | `detect_same_value` 默认关 |
| `dim_outside` | 4.0 s | ~2.9 s | 拆明，**无可省** |

**端到端 A/B（n=4 每组，逐轮交错）：122.2 s → 101.9 s，-16.6%。**

> ⚠️ **这台机器的波动大于收益本身**：同一份代码在 6 小时内从 82 s 变到 162 s 再回到 106 s。
> 所有百分比只在**同一轮交错采样内**可信，跨时段对比不成立。

### 附带收益

**smoke 从 22m10s 降到 12m59s（-41%）。** 回归测试自己变快，以后每轮验证成本都更低。

---

## 三、改了两个默认值（行为变更，请确认）

| 参数 | 旧 | 新 | 理由 |
|---|---|---|---|
| `auto_dimension.place_outside` | False | **True** | 18 条尺寸全落在轮廓内，图面不可用 |
| `auto_dimension.detect_same_value` | True | **False** | 只用于诊断，却占 68% 时间（18.4 s / 27.0 s） |

**两者都保留旧行为的开关**，且都在工具描述里写明了实测数据。

---

## 四、明确没做的

| 项 | 原因 |
|---|---|
| **PR1 合并保存/审计** | 基线显示 `save`+`audit` 合计只占 3%，**调序后先做了收益更大的 PR3** |
| **PR2 `DrawingBatch`** | 未做。规格包给了参考实现，但要先确认 `GetUserPreferenceToggle` 在本 binding 是属性还是方法 |
| **PR4 尺寸覆盖补偿** | 未做。依赖 PR3 完成后的覆盖清单 |
| **PR5 C# Shim** | 规格要求「测量证明 Python 遍历是主要瓶颈」才做，**未满足** |
| **`dim_outside` 的 1.4 s 取值** | 三条替代接口全部验证不可用（见 v0.8.108/109） |
| **`delivery_check` 剩余 19 s** | 未拆。其中 `annotations` 已缓存，剩 `dim_attachments`(4.1 s) + 自身 |

---

## 五、我搞错的地方（都留在 release note 里）

这一轮有 6 次「我以为对了，实际错了」：

| # | 错在哪 | 怎么发现的 |
|---|---|---|
| 1 | **分块采样**得出「B 组泄漏 23 s」 | 改成逐轮交错后 **23 s 消失，变成 0** |
| 2 | **`docs=3` 当成文档泄漏** | 采样点在 `close_document` **之前**，实际是 1 |
| 3 | **`flip_x` 加了但没生效** | `_EDGE_CACHE` 的键不含它，第二次返回未翻转的缓存 |
| 4 | **`_t_outline` 定义在循环之后** | `auto_dimension` 整个返回 `auto_dimension_failed`。**离线 132 条全绿，只有真机跑才暴露** |
| 5 | **断言解析人类中文** | 改了措辞就挂，报出来像"代码有问题" |
| 6 | **版本号 19 个版本没动** | `sed` 匹配上一个版本号，一次失配后全失配，**且退出码 0** |

**共性**：
- **1、2 是测量方法错**，不是代码错——**方法论错时，数据会一本正经地骗人**
- **3、4 是"改了但没验证是否生效"**
- **5、6 是"验证手段本身失效"**

**新加的断言**：`assert_no_undefined_bare_calls`、`version consistent`、
`smoke reads diameter_dimensions`（不解析散文）。

---

## 六、验收建议顺序

1. **跑两条命令**（第一节）——30 秒
2. **看 `docs/2026-11-03-pr0-baseline.md`** ——基线数据与局限
3. **看 `logs/release-v0.8.10[1-9].md` / `v0.8.110.md`** ——每版的实测数字
4. **真机抽查一版**（推荐 v0.8.106，收益最大且最直观）：

```python
auto_dimension(drw, all_views=True)                       # 默认不检测同值
auto_dimension(drw, all_views=True, detect_same_value=True)  # 对比，慢 ~11 s
```

5. **`logs/perf_ab.jsonl`** ——交错 A/B 的原始 16 条记录

---

## 七、一句话总结

**没有让出图变快 5 倍。** 做到的是：**把四个占大头的重复工作去掉，端到端 -16.6%，
回归测试快 41%，并且把每个相位的成本变成了可查的数字。**

**`dim_outside` 那 1.4 s 我查到最后发现没有可省的**——
**知道哪里省不了，和知道哪里能省一样有用。**
