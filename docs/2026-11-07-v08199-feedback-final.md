# v0.8.199 三份反馈 · 完整账（v0.8.209 复核）

**复核时间**：2026-11-07 17:45
**范围**：`0.8.196 → 0.8.209`，14 个版本
**工具数**：66 → **69**
**断言**：189 → **200**
**smoke**：40 → **42**

---

## 一、报告 1（AM_DOOR）：`fillable` 与可落地性脱节

| 项 | 判定 | 版本 | 实测 |
|---|---|---|---|
| A1 `fillable: 6` 全被拒 | ✅ | 0.8.200 | `fillable 6 → 0`，6 条标 `no_length_pair`，`apply` 零尝试 |
| A2 `add_sheet` 类型库阻断 | ✅ | 0.8.196 / 0.8.200 | 自动 `EnsureModule`；`('图纸1',)` → `['图纸1','图纸2']` |
| 注释漂移 | ✅ | 0.8.200 | 已改 |
| 未知参数静默忽略 | ⚠️ 未做 | — | 属 FastMCP 框架行为 |
| `export_bom` 失败无 `document_type` | ⚠️ 未做 | — | 第六轮遗留 |
| `measure_layout` 未注册 | ⚠️ 未做 | — | 长期 |

---

## 二、报告 2（承载治具）：多页链路

| 项 | 判定 | 版本 | 实测 |
|---|---|---|---|
| **C1** 类型库缓存 | ✅ | 0.8.196 | `EnsureModule` 自愈 |
| **C2** 新页空白/Letter | ✅ | 0.8.200 | A4/A3/A2/A0 尺寸全对，`matches=True` |
| **C3** 无 sheet 维度工具 | ✅ | 0.8.203 / 0.8.204 | 三个新工具 + 两个写工具加 `sheet` |
| **C4** audit 只看当前页 | ✅ | 0.8.205 / 0.8.206 | `sheets[]` + `every_sheet_has_views` |
| **C5** clamp 推到框外 | ✅ | 0.8.201 | 改用 `template_safe_area` |
| **C5** 漏删角度 | ✅ | 0.8.201 | 一个 `_dimension_measure` 统一量纲 |
| **C6** ×1000 口径 | ✅ | 0.8.201 | 同上 |
| **C7** 能力表矛盾 | ✅ | 0.8.201 | 三处改「可用」 |
| **C8** 候选语义 | ⚠️ **部分** | 0.8.209 | 计数分级完成；**孔距语义匹配未做** |
| **C9** X 轴符号 | ✅ | 0.8.207 / 0.8.208 | 从视图名推导，右/左视符号相反 |

---

## 三、报告 3（接槽）：公差枚举

| 项 | 判定 | 版本 | 实测 |
|---|---|---|---|
| **P0** 枚举错位 | ✅ | 0.8.200 | 改回 `BASIC=1/BILAT=2/LIMIT=3/SYM=4` |
| **P0** 真根因 | ✅ | 0.8.200 | `SetToleranceValues(TolMin, TolMax)`，原来传反了 |
| **图面验证** | ✅ | 0.8.202 | `bilateral` → 两个偏差；`symmetric` → `±`；`basic` → 无 |
| `set_title_block` 落错格 | ⚠️ 未做 | — | `before.material` 仍空、两处 POM |
| `datum_feature` 交叉引用 | ⚠️ 未做 | — | 引用不存在的条目 |
| `delivery_check.note` 与 blockers | ✅ | 0.8.201 | 从实参生成 |
| `hole_callouts.ok` 与 0 对象 | ✅ | 0.8.201 | `ok:false` + `covered:true` |
| `add_dimension_tolerance` 单位 | ✅ | 0.8.201 | metres → MILLIMETRES |
| 圆边坐标退化自检 | ⚠️ 未做 | — | — |
| 孔标注引线同一 x | ⚠️ 未做 | — | — |

---

## 四、这一轮真正解决了什么

### 1. 三个「做不到」全部推翻

| 版本 | 我说做不到 | 实际 |
|---|---|---|
| 0.8.180–195 | `NewSheet4` 调不通 | **参数顺序错**，`TemplateIn` 是第三个且 type=int |
| 0.8.174–196 | 视图轴向没 API | **`GetOrientationName` 一直都在** |
| 0.8.186–197 | 删不掉插入的尺寸 | **两套命名空间**，`FullName` vs `GetNameForSelection` |

### 2. 又一个「做不到」：C9 方向

> There is no way to decide this from the API... Deriving it needs a pixel measurement.

**不用像素。** 反证：右视和左视都把模型 +Z 投到同一侧，物理上不可能。
`GetOrientationName` 说的就是「眼睛在哪」，右手系一推就有符号。

### 3. 一次真实的测量推翻了一条注释

`_view_axis_map` 的注释列了三条「API 不存在」（`GetViewPlaneNormal`、
`ModelToViewTransform` 非 4x4、`MultiplyTransform` 缺失）—— **三条全真，结论仍错**。

**注释写得越详细越像真的。** 它比正确代码更有说服力，因为带着日期和 API 名。

---

## 五、这一轮我自己引入的回归

| 版本 | 改动 | 破坏 | 修于 |
|---|---|---|---|
| 0.8.182 | 元组 2→3 | `duplicate_report` 解包 | 0.8.184 |
| 0.8.190 | `0` → `None` | `if total > 30` TypeError | 0.8.194 |
| 0.8.198 | info 加字段 | `centre_m` KeyError | 0.8.199 |
| 0.8.201 | note 加计数 | `len(ai_supplied)` TypeError | 0.8.206 |

**四次都在改返回值/文案之后，四次都是 smoke 抓到，没有一次是断言抓到的。**

### 一个认知错误，漏了三处

`_iter_views(doc)` 不是「当前页」的读取器：

1. `list_sheets` 两页报同一视图列表
2. `add_view_to_sheet` 数总量，数量「没变」也算成功
3. **空页检查用文档级视图 → 空页也是 `ok:true`**（0.8.201 以为修好了，其实从没生效）

---

## 六、验证状态

| | 数量 |
|---|---|
| 离线断言 | **200/200** |
| static check | **69 tools** |
| smoke | **42 项**（本次全量跑，结果见发布说明） |
| 真机逐条实测 | 见各节「实测」列 |

---

## 七、仍开着的

| 项 | 性质 | 优先级 |
|---|---|---|
| C8 孔距语义匹配（只匹配圆边中心距、同视图同轴） | 新算法 | P2 |
| `set_title_block` 落错格 | 要读模板表格结构 | P2 |
| `datum_feature` 交叉引用 | 文案 | P2 |
| 圆边坐标退化自检 | 小 | P2 |
| 孔标注引线同一 x | 小 | P2 |
| 十几个写工具加 `sheet` | 机械但量大 | P2 |
| `delivery_check` 速度（48 分钟 smoke） | 性能 | P3 |

**没有 P0/P1 开着。**
