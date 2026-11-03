# PR0 交付：性能采集与基线

> 只加测量，**不改行为**。这是规格里明确的第一步。
> 日期：2026-11-03 · 版本 v0.8.101

---

## 一、做了什么

| 文件 | 内容 |
|---|---|
| `src/solidworks_assetmate_mechdog/perf.py` | 规格包里的 `PerfRecorder` 原样落地（无依赖、span/count/fail/finish/append_jsonl） |
| `src/solidworks_assetmate_mechdog/perf_hooks.py` | 薄封装：`start/stop/span/bump/timed/append`，**未启动时全部是 no-op** |
| `src/solidworks_assetmate_mechdog/tools/drawing.py` | 6 处 `@perf_hooks.timed(...)` |
| `src/solidworks_assetmate_mechdog/tools/ledger.py` | 1 处 |
| `src/solidworks_assetmate_mechdog/tools/planner.py` | 1 处 |
| `tools/perf_baseline.py` | 基线跑批：N 轮、中位数 + P95、写 JSONL |

**埋点方式**：装饰器，不改函数体。这样 PR0 在代码层面**不可能改变语义**——
唯一风险是装饰器本身写错，而 132 条离线断言与 35 项 smoke 都会覆盖到。

**无行为变化验证**：

```
python tests/static_check.py   -> 64 tools, every parameter documented
python tests/check_all.py      -> 132/132 passed
```

---

## 二、基线（旋转轴，A3，5 轮）

```
run 1   81.3 s      run 4   88.7 s
run 2   82.1 s      run 5   75.4 s
run 3  100.8 s
中位数 82.1 s      P95 100.8 s      正确 5/5
```

| 相位 | 中位数 | P95 | 调用次数 |
|---|---:|---:|---:|
| **delivery_check** | **27.7 s** | 34.0 s | 1 |
| **ledger** | **22.6 s** | 31.7 s | **5** |
| **dimension_total** | **20.6 s** | 24.7 s | 1 |
| plan | 5.6 s | 7.8 s | 1 |
| drawing_create | 2.3 s | 2.6 s | 1 |
| save | 2.0 s | 2.2 s | 4 |
| audit | 0.6 s | 1.2 s | 3 |

**相位合计 ≈ 81.4 s，墙钟 82.1 s** —— 覆盖率接近 100%，没有大块"消失在空里"的耗时。

（第一轮埋点只覆盖 31 s / 98.5 s，补了 `ledger`/`plan`/`delivery_check`/`hole_callouts`
之后才补齐。**这个缺口本身是 PR0 的第一个产出。**）

---

## 三、基线指出的第一靶子：`build_feature_ledger` 一次出图调 5 次

| 调用点 | `include_faces` |
|---|---|
| `pipeline.py:161`（produce_drawing 第一步） | False |
| `planner.py:106`（plan 内部） | False |
| `drawing.py:6288` | False |
| **`drawing.py:2341`**（`add_hole_callouts`） | **True** |
| **`drawing.py:5240`**（`delivery_check`） | **True** |

**同一个模型、同一份特征树，读了五遍。其中两遍带圆柱面扫描。**

`ledger` 中位数 22.6 s ÷ 5 次 ≈ **4.5 s/次**；
两遍 `include_faces=True` 的那两次明显更贵（面扫描要遍历实体）。

**这是"重复工作"最典型的一例，也正是规格 §2.1 说的**
「缓存模型包围盒 / 避免同一视图反复读取」。

---

## 四、还没做的（按规格顺序）

| 阶段 | 内容 | 前置 |
|---|---|---|
| **PR1** | 合并保存与最终审计（`save` 现在 4 次、`audit` 3 次） | 需要先定"哪几次是不能省的" |
| **PR2** | `DrawingBatch` + `swDrawingAutomaticViewUpdate` A/B | 规格包已给参考实现，但要先确认 `GetUserPreferenceToggle` 在本 binding 是属性还是方法 |
| **PR3** | 布局缓存与位置一次性写入 | 依赖 PR2 的显式同步点 |
| **PR4** | 尺寸覆盖补偿（`AddDimension2` 只传三参） | 依赖 PR3 |
| **PR5** | C# Shim | 默认不做 |

**PR0 到此为止，不改任何行为。**

---

## 五、复现

```bash
python tools/perf_baseline.py --model 测试用例/旋转轴-工程图/旋转轴.SLDPRT --runs 5
```

输出：`logs/perf.jsonl`（可用 `ASSETMATE_PERF_LOG` 覆盖），每轮一条 JSON。
`counts_scope` 字段说明计数只覆盖被埋点的调用点，**不是全局 COM 计数**。

---

## 六、基线数据的局限（如实写）

- **只有一个零件**（旋转轴）。规格要求 10~20 个代表模型，目前是 1 个。
- **冷启动与热缓存没有分开**——5 轮都在同一 SolidWorks 实例里，run 1 偏慢（81.3 s）但
  run 3 反而最慢（100.8 s），说明**进程内波动大于冷热差异**，样本还不够。
- **`ledger` 的 5 次是计数，不是 5 次独立的耗时分解**——装饰器累加同一名字。
  要拆成 `ledger#1..#5` 需要给每次调用传不同 name，PR3 再做。
- 文档数稳定在 2（零件 + 图纸），**没有泄漏**。
