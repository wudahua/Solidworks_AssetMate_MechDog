# ZJZL 增量包吸收评估与实施方案

- 评估对象：`他人代码/AssetMate-ZJZL-fef0df3/`（补丁 `AssetMate-ZJZL.patch` + `files/` 全量文件 + 交付清单 + 验收说明）
- 对方基线：AssetMate **v0.8.351**，提交 `fef0df3`；实测环境 **SolidWorks 2025 33.0.0**
- 本仓库基线：AssetMate **v0.8.373**，**80** 个 MCP 工具，**36** 条能力表项（15 verified / 16 partial / 2 pilot / 3 unsupported），离线断言 **352**（33 behavioural / 319 source-level）
- 本文结论基于：完整读码（7 个模块 1403 行 + 测试 369 行 + 补丁 1832 行）+ **本机 SW 32.4.0 真机探针**（只读，未改仓库代码）
- 探针与输出：`logs/probe_zjzl_claims.py`、`logs/probe_zjzl_claims2.py`、对应 `.out`

---

## 0. 一页结论

| 判断 | 内容 |
|---|---|
| 值不值得吸收 | **值得**。6 个工具中 4 个（正投影、任意方向、参考尺寸、BOM 气泡）在本项目里是空白或明显弱于对方；配合读取也是空白 |
| 能不能整体 `git apply` | **不能**。补丁上下文是 0.8.351，会与 `server.py` / `capabilities.py` / `check_all.py` / `regressions.py` 冲突，且其中一处会把 0.8.364 的能力结论改回去 |
| 最大不确定性 | `GetCurveParams3`（`ICurveParamData`）在**本机 late-bound 调用报「找不到成员」**；对方用 typed 接口绕过。这条决定了「能用」还是「只在 2025 上能用」 |
| 最大意外收益 | `IView.ModelToViewTransform` **本机可用** —— 我们在 0.8.x 明确写过「不可用 / 不是行主序 4×4」，实测是 16 个数、`[12]`=比例、`[9..11]`=平移、`[0..8]`=旋转。模型↔图纸映射这条路被重新打开 |
| 建议节奏 | 5 批：地基 → 尺寸 → 视图 → 装配 → 测试。**先做第 1 批并真机验 `GetCurveParams3`**，再决定后三批是否照搬 |
| 工作量 | 约 **3 个工作日**（含真机验收），不含与 0.8.37x 的语义对齐讨论 |

---

## 1. 对方交付物清单

| 文件 | 行数 | 作用 |
|---|---|---|
| `zjzl_native.py` | 173 | 地基：typed 接口、文档定位、事务性回滚、持久引用、保存 |
| `zjzl_orthographic.py` | 147 | 原生对齐正投影（读投影法、校验父子/法向/位置、失败整批撤回） |
| `zjzl_custom_view.py` | 104 | 任意方向视图（临时改模型视口 + 命名视图 + 恢复） |
| `zjzl_geometry.py` | 204 | 可见几何探测 + 作用域持久引用 + 投影跨度计算 |
| `zjzl_reference_dimension.py` | 147 | 括号从动参考尺寸（分类型 Add*Dimension2 + 附件精确校验） |
| `zjzl_mates.py` | 89 | 配合读取（类型/对齐/抑制/实体/距离角度值） |
| `zjzl_balloon.py` | 136 | BOM 联动件号气泡（解析 BOM 行取件号并逐项校验） |
| `tests/test_zjzl.py` | 150 | 11 条单测（纯逻辑 + mock COM） |
| `tests/zjzl_live.py` | 219 | 真机验收脚本（零件 + 装配两条） |
| `docs/zjzl-capabilities.md` | 34 | 使用边界说明 |

补丁另改：`server.py`（6 个工具注册 + profile 归类）、`capabilities.py`（4 条新条目 +
**改写 `balloon` 条目**）、`README.md`、`check_all.py`、`regressions.py`。

---

## 2. 能力对照（逐项）

### 2.1 正投影对齐视图

| 维度 | 本项目 v0.8.373 | ZJZL |
|---|---|---|
| 入口 | `place_views` / `add_view_to_sheet`（按 front/top/right/iso 摆放） | `zjzl_create_orthographic_views(drawing_path, base_view, directions, offset_m)` |
| 投影法 | 不读 | `sheet.GetProperties2[4]` 判第一/第三角法，由它决定上下左右 |
| 建视方法 | `CreateDrawViewFromModelView3` 等 | `CreateUnfoldedViewAt3`（原生投影，自动继承父视图比例/配置） |
| 校验 | 轮廓是否在页内 | 类型==4、`GetBaseView` 指回父视图、模型路径一致、配置一致、法向向量一致、比例一致、位置误差 ≤1 mm |
| 失败处理 | 单步返回错误 | **整批按逆序删除已建视图，并回读确认已删** |
| 已有同向投影 | 不判 | 遍历同父投影的法向，命中即 `projection_exists` |

**结论：强吸收。** 它把「摆视图」从「算坐标」变成「用 SolidWorks 自己的投影关系」，且失败可回滚。

### 2.2 任意方向视图

| 维度 | 本项目 | ZJZL |
|---|---|---|
| 入口 | 无（只有预设方向） | `zjzl_create_custom_view(model, normal, up, position)` |
| 机制 | — | 临时把模型视口转到目标姿态 → `NameView(临时名)` → `CreateDrawViewFromModelView3` → **恢复原视口** → 删临时命名视图 |
| 校验 | — | 视图 `ModelToViewTransform[:9]` 与请求矩阵差 ≤1e-6、位置误差 ≤1 mm、模型路径/配置一致；视口恢复也必须通过（`viewport_restore_failed`） |

**结论：强吸收。** 唯一能在 API 层做「任意方向」的路子；且它对「污染用户模型」有防护。

### 2.3 几何探测与参考尺寸

| 维度 | 本项目 | ZJZL |
|---|---|---|
| 几何清单 | `list_drawing_edges` / `list_circular_edges`（半径、中心、是否整圆） | `zjzl_probe_geometry`：线/弧/圆/其它曲线 + 可见顶点，模型坐标与纸面坐标**都给** |
| 稳定引用 | 无（只有下标/坐标） | `GetPersistReference3` → base64；`GetObjectByPersistReference3` 解析；作用域含图纸/页/视图/组件实例 |
| 尺寸创建 | `AddDimension2`（已知会产出错误类型，需要事后删除） | 分类型：`AddHorizontalDimension2` / `AddVerticalDimension2` / `AddDiameterDimension2` / `AddRadialDimension2` |
| 参考尺寸（括号） | **无** | `display.ShowParenthesis = True` + `dimension.DrivenState == 1` 校验 |
| 附件校验 | 位置邻近猜测（`verify_dimension_attachments`） | `GetAttachedEntities3` + `IsSame` 精确比对 |
| 重复检测 | 值 + 视图 | 类型 + 附件集合 + 实测值 |
| 失败处理 | 部分路径会残留标注 | 创建成功但校验失败 → `rollback_annotation` 删掉 |

**结论：强吸收。** 这一块同时解决我们两个长期缺陷：「尺寸类型不可控」与「附件靠猜」。

### 2.4 配合读取

| 维度 | 本项目 | ZJZL |
|---|---|---|
| 入口 | 只有 `get_assembly_tree`（层级 + 质量） | `zjzl_inspect_mates(assembly_path)` |
| 内容 | 无配合 | 配合组名、`IMate2.Type/Alignment/Flipped`、抑制、`GetErrorCode2`、每个 `IMateEntity2` 的组件与持久引用、`MateDistanceDim`/`MateAngleDim` 的数值与单位 |
| 缺数据处理 | — | 每个字段失败记账进 `issues`，`status` 标 `partial` |

**结论：强吸收（只读，风险低）。**

### 2.5 BOM 件号气泡（**唯一与我们正面重叠**）

| 维度 | 我们 `add_balloon`（v0.8.364，verified） | `zjzl_add_bom_balloon` |
|---|---|---|
| 建法 | `IModelDoc.InsertBOMBalloon2(style,size,0,text,0,"")`（6 参） | `extension.CreateBalloonOptions()` + `extension.InsertBOMBalloon2(options)` |
| 件号来源 | **调用方给**（`item="3"`）；无 BOM 时读 `?` | **从 BOM 行解析**：`ITableAnnotation.Type==2` → `IBomTableAnnotation.BomFeature` → `GetComponents2(index, configuration)` → `GetComponentsCount2`；行必须可见、必须唯一 |
| 校验 | 返回对象 + `GetText` | `note.IsBomBalloon()`、`GetBomBalloonTextStyle(True)==2`、件号文本等于 BOM 行、`IsDangling()`、`Visible==1`、附件是该边、位置误差 ≤1 mm |
| 幂等 | 无 | 同实例同视图已有气泡 → `balloon_exists` |
| 失败处理 | 无（失败只报错） | `rollback_annotation` |

**结论：用它替换我们的实现（保留我们的 `item` 手工能力作为降级路径）。** 只有它能把气泡和 BOM 真正连起来。

### 2.6 我们自己已有的、对方**没有**的（合并时不能丢）

| 项 | 我们 | 对方 |
|---|---|---|
| 件号手工编号（无 BOM） | ✅ `item=` | ❌ 必须有 BOM |
| 孔表 / 图纸草图线增删 | ✅ | ❌ |
| 剖视、局部放大 | ✅ | ❌ |
| 尺寸排版（`layout_dimensions`/`plan_dimension_gaps`） | ✅ | ❌ |
| 交付判定 / 台账 / 对账 / 能力表 | ✅ | 只加条目 |
| BOM 表导出、模板导出、批量导出 | ✅ | ❌ |

---

## 3. 技术底牌：本机实测结论

> 全部为 **SW 32.4.0**、A3 图、`door.SLDPRT`、`scale=0.5` 的只读探针。

### 3.1 `IView.ModelToViewTransform` 可用（推翻旧结论）

```
ArrayData(16) = [1,0,0, 0,1,0, 0,0,1, 0.2354, 0.179, -0.002, 0.5, 0, 0, 0]
                 └── 旋转 3×3（行主序）──┘ └── 平移(m) ──┘  └比例┘
[12] = 0.5 == view.ScaleDecimal
```

模型→纸面：`p_sheet = R·p_model·scale + t`（对方 `point_through` 完全一致）。

实测：圆孔中心 `(0.0825, -0.0700, 0.006)` → 纸面 `(0.2766, 0.1440)`，半径 `0.003 m × 0.5 = 0.0015 m`，
落在视图轮廓 `[0.1907, 0.1193, 0.2841, 0.2387]` 内 ✔

**意义**：`docs/com-quirks.md` 里「模型坐标→视图坐标没有可用 API」这条要改。
可解锁：精确防重叠（不再靠 1.2 mm 虚胖轮廓）、标注锚点、孔标注与模型孔的对应、
`axes_ambiguous` 的降级路径。

### 3.2 `GetProperties2` 是属性，8 个值

```
GetProperties2 (attr) len=8 [8.0, 12.0, 1.0, 2.0, 1.0, 0.42, 0.297, 1.0]
GetProperties  (attr) len=7 [8.0, 12.0, 1.0, 2.0, 1.0, 0.42, 0.297]
```

我们一直用 7 值版。第 8 个值疑似「投影法/首角标志」，与对方 `[4]` 用法需要对齐确认（见 §6 待验证项）。

### 3.3 `GetStartVertex/GetEndVertex` 对**模型视图的圆边**可用

```
GetStartVertex -> obj   vertex model=[0.0825,-0.07,0.003] -> sheet=(0.2766,0.1440)
GetEndVertex   -> obj   vertex model=[0.0825,-0.07,0.009] -> sheet=(0.2766,0.1440)
```

我们旧结论「恒 None」只对**轮廓边（silhouette）**成立。这条要按上下文写清楚，不能一句话概括。

### 3.4 `GetCurveParams3` 在本机 late-bound 下不可用

```
GetCurveParams3  FAIL (-2147352573, '找不到成员。')
```

但类型库里**确实声明**了 `GetCurveParams3` / `ICurveParamData`。
判断：这是我们反复踩过的「late binding 看不到、typed 接口能拿到」问题（本轮之前已在
`InsertBOMBalloon2`／`InsertWeldSymbol3`／`InsertModelAnnotations4` 上遇到三次同类）。
对方 `Native.cast()` 正是为此设计。

**这是第 1 批必须先验证的一步**：typed `IEdge.GetCurveParams3()` 在本机能否返回端点。

---

## 4. 冲突与风险

| # | 风险 | 具体 | 处理 |
|---|---|---|---|
| 1 | 补丁上下文过期 | 基线 0.8.351 → 现 0.8.373，`server.py`/`capabilities.py`/测试全变 | **不 apply**，手工按 §5 移植 |
| 2 | 能力条目被改回去 | 补丁把 `balloon` 从 `unsupported`（其基线）改为 `partial`，会覆盖我们 0.8.364 的 `verified` 措辞 | 合成一条：`verified`，两条路径都写，落 `tools` 字段 |
| 3 | 测试计数冲突 | 其 `check_all` 认 337 断言 / 4 套件；我们 352 / 4+stdio | 只取 `test_zjzl.py` 与 `zjzl_live.py`，接进我们的套件 |
| 4 | SW 版本差异 | 对方 33.0（2025）；本机 32.4 | 每个 COM 调用都要在本机复验，不能凭对方回执 |
| 5 | 命名风格 | 全部 `zjzl_` 前缀 | **保留前缀**（可追溯），在 README 里注明来源 |
| 6 | 默认 `save` 语义 | 对方默认 `save=False`；我们多数写工具默认 `True` | 新工具沿用对方 `False`（更安全），并在描述里显式说明 |
| 7 | 错误码口径 | 对方统一吞为 `zjzl_operation_failed` | 合并时映射到我们既有码（`bad_input`/`view_not_found`/`rollback_failed`…） |
| 8 | runtime guard | 我们的 `_reject_unknown_arguments` + pydantic 模型入参（`GeometryReference`）组合未测 | 第 2 批必须先跑一次 stdio 契约（含未知参数、错误类型） |

---

## 4.5 执行记录（按批更新）

### 批 1 · 地基 — 已完成（v0.8.374）

| 项 | 结果 |
|---|---|
| `native_com.py` | 已落地：`Native`/`cast`/`value_of`/`execute`/`drawing_scope`/`model_views`/`named_view`/`sheet_properties`/`sheet_point`/`save_drawing`/`persist`/`read_reference`/`rollback_annotation` |
| `geometry.view_frame` + `axis_vector` + `point_through` | 已落地并带 behavioural 断言 |
| **typed `IEdge.GetCurveParams3`** | ✅ **12/12 条边成功**（late-bound 下报「找不到成员」）→ 结论：typed 路线在本机成立，后面三批可以照搬 |
| `view_frame` 精度 | scale 精确 0.500；端点极值 0.1600 m → 纸面 0.0800 m；视图中点误差 0.8 mm（≤1 mm 判据通过） |
| `GetProperties2` | 属性访问，8 值；`[7]=1.0` → GB A3 判为**第一角法** |
| 视口 outline | 宽高总虚胖 **0.0119**（y 维精确：0.1075+0.0119=0.1194）；旧注记「各边 0.0119」应改为「总 0.0119」 |

批 1 结论：**地基可用，无需降级实现。** 下一批（几何探测 + 参考尺寸）按原计划推进。

### 批 2 · 几何探测 + 参考尺寸 — 已完成（v0.8.375）

| 项 | 结果 |
|---|---|
| `ref_geometry.py` | `probe_geometry`：51 条边 / 44 个顶点，每条带 `GetPersistReference3` 引用 |
| `ref_dimension.py` | `add_reference_dimension`：Ø15 参考尺寸 `reference:true` / `readback_verified:true`；水平 1.0 mm（door）、1.325 mm（P05）；重复调用 `dimension_exists` |
| 新工具 | 2 个（80 → 82），`stdio_contract` 10/10 |
| **新发现：坐标是三个空间** | `ICurveParamData` = **视图局部**（door 跨度 0.160×0.215 m 居中）、`IVertex.GetPoint` = **模型**、`ModelToViewTransform` 平移 = **视图中心**。第一版把端点当模型坐标 → 44 个顶点全部「位置不可验证」；现分三字段输出，并按包围盒中心反推视图原点 |
| 未验证 | 保存重开后 `dimension_id` 能否解析（列入批 5） |

### 批 3 · 视图 — 已完成（v0.8.376）

| 项 | 结果 |
|---|---|
| `create_orthographic_views` | 原生上视/右视：法向 (0,1,0)/(1,0,0)，`projection: first`（GB A3 `[7]=1`），重复请求 `projection_exists` |
| `create_custom_view` | `normal=[0.6,0,0.8]` → `rotation` 回读一致；模型 MD5 未变；无 `ZJZL_` 命名视图残留 |
| 新工具 | 2 个（82 → 84） |
| **新发现** | `IModelDoc2.ActiveView` 可用，但 `IActiveView` 是只写属性；`value_of` 不能把「属性返回的 CDispatch」当方法调用（否则报「找不到成员」） |
| 未强制触发 | 投影建到一半失败时的整批撤回（代码有，未造失败用例） |

### 批 4 · 装配 — 已完成（v0.8.377）

| 项 | 结果 |
|---|---|
| `inspect_mates` | GGD 配电柜装配：**9 个配合 / 1 组 / `complete`**；monitor 支架 rebuild 夹具只有**空配合组（0 配合）**，不能用来验收 |
| `add_bom_balloon` | 真机：20 行 BOM，165 条组件边，`item_number='10'`、`bom_row=10`、`readback_verified:true`；无 BOM 时 `bom_scope_unavailable` |
| 新工具 | 2 个（84 → 86） |
| **发现** | `GetErrorCode2` 在本 build 是**方法**，直接读属性得到 bound method，`error[0]` 报 `'method' object is not subscriptable` —— 这条错误掩盖了「装配其实能打开」 |
| **未进 smoke** | `add_bom_balloon`：在 20 件装配上插 BOM 会挂住会话（两次），已加豁免并写明原因，正路径留文档证据，批 5 重做 |

### 批 5 · 测试收口 — 已完成（v0.8.378）

| 项 | 结果 |
|---|---|
| **保存重开后引用** | ✅ 建 Ø/水平参考尺寸 → 保存 → 关图 → 重开 → `GetObjectByPersistReference3` 返回 `state=0`，与视图内 display dimension `IsSame` 一致，值 1.000 mm、`ShowParenthesis=True`、`DrivenState=1` |
| 单测移植 | 3 条 behavioural：投影摆位（第一/三角法 + 非法输入）、旋转正交性（含行列式 +1）、引用 id 编解码与拒绝非法数据 |
| `probe_geometry` 预算 | 新增 `max_edges`（默认 400）与 `budget_s`（默认 90），命中即截断并写 `truncated_reason` |
| **BOM 气泡仍不进 CI** | 小装配（3 行 BOM）能建图与建表，但 `probe_geometry` 在**第一条边**上卡了 **285 s**（40 s 预算只能在下一次循环前生效，拦不住单次 COM 阻塞）。夹具问题，非工具逻辑，豁免保留 |
| 断言总数 | 359（40 behavioural） |

### 补做 · 项 C：重叠判定改用内容框 — 已完成（v0.8.379）

| 项 | 结果 |
|---|---|
| 实测留白 | `GetOutline` 每边 **0.0059 m**（1:1 与 1:2 都是 L=B=T 精确；R 因右侧是圆弧切线而偏大） |
| 改法 | `content_box()` 去掉已知留白；重叠按内容框 + 0.2 mm 容差判；**页内边界仍用原始 outline** |
| 正向 | `native projections and custom view` PASS（含"太近必须拒绝"的负向断言） |
| 回归 | `views, dimensions, title block` PASS、`paper scaling keeps views inside` PASS |
| 断言 | 360（41 behavioural） |

> 注意：这一轮 smoke 的三条用例耗时异常（108 s / 274 s / 568 s，平时 16 / 25 / 77 s）。
> 会话已连续跑了一整天、建过上百张图纸，判断为**会话退化**而非本次改动；结论按"通过/不通过"采信，不采信耗时。






## 5. 吸收方案（5 批）

### 批 1 · 地基（0.5 天）— 决定后面三批是否成立

**取**：`zjzl_native.py` 全量（`Native`/`cast`/`execute`/`drawing_scope`/`save_drawing`/
`persist`/`read_reference`/`rollback_annotation`/`sheet_point`/`model_views`/`named_view`）。

**改**：
1. `execute()` 里的兜底错误码映射到我们的码表；
2. `Native.cast` 与现有 `_typed_drawing_doc` 合并成一个入口，避免两套 typed 绑定；**改造**：把 `ModelToViewTransform` 解析封成公共函数 `view_frame(doc, view) -> {rotation, translation, scale}`，并加进 `geometry.py` 供全项目复用；
3. 把 `point_through` 做成纯函数 + 单测（不碰 COM）。

**验收（真机）**：

| 项 | 判据 |
|---|---|
| typed `IEdge.GetCurveParams3()` | 返回对象，`StartPoint/EndPoint` 非空；若失败，走顶点回退并记 `curve_params_unavailable` |
| `view_frame` 精度 | 任取 2 个模型点，映射后与 `view.GetOutline` 及已知孔位误差 ≤0.1 mm |
| `GetProperties2[7]` 含义 | 用 A3 第一角模板 vs 手工第三角对照，确认哪一位是投影法 |
| 离线 | 新增断言全部进 behavioural 档；`check_all` 全绿 |

**这一批失败的后果**：`GetCurveParams3` 不可用 → 几何探测改走顶点回退（功能缩水但可用）；
`view_frame` 若漂移 → 只用于「相对比较」（防重叠），不用于绝对落点。

### 批 2 · 尺寸与几何（1 天）

**取**：`zjzl_geometry.py` + `zjzl_reference_dimension.py`。

**改**：
- 端点来源按批 1 结论二选一（`GetCurveParams3` 或顶点回退）；
- 尺寸类型映射表（`OPERATIONS`）与我们的 `add_drawing_dimension` 合并为一个实现，
  分类型 API 优先，`AddDimension2` 仅作降级；
- `GeometryReference` 的 pydantic 模型要过我们的 runtime guard（`_reject_unknown_arguments`）。

**验收**：
- 60 mm 水平尺寸、Ø5 直径尺寸，括号显示 + `DrivenState==1`；
- 错误期望值（如 61 mm）必须回滚并留 `dimension_value_mismatch`；
- 保存重开后 `dimension_id` 仍可解析；
- 重复调用同一几何 → `dimension_exists`。

### 批 3 · 视图（0.5 天）

**取**：`zjzl_orthographic.py` + `zjzl_custom_view.py`。

**改**：与现有 `place_views` 的关系写清楚（新工具建「原生关联投影」，旧工具做「自由摆放」），
`check_layout` 改用批 1 的 `view_frame` 做精确重叠判定。

**验收**：
- 第一角法：A3 模板上建上视/右视，位置在下/左；第三角法相反（模拟覆盖，真机需第三角模板）；
- 重叠必须整批撤回且回读确认视图已删除；
- 任意方向视图：源模型文件哈希不变、视口姿态已恢复、临时命名视图已删除。

### 批 4 · 装配（0.5 天）

**取**：`zjzl_mates.py` + `zjzl_balloon.py`。

**改**：
- `zjzl_add_bom_balloon` 与现有 `add_balloon` **并存**：新工具要求 BOM，老工具保留手工件号；
- 能力条目合成一条 `balloon`（`verified`），`tools` 列出两者，`why` 分两段写清差异；
- 无 BOM 时新工具必须**拒绝**（`bom_scope_unavailable`），不得退化成写 `?`。

**验收**：
- 距离配合读回 20 mm、两个实例引用正确；
- 件号与 BOM 行一致；件号不符返回 `bom_item_mismatch`；
- 同视图重复插同一实例 → `balloon_exists`。

### 批 5 · 测试与门禁（0.5 天）

**取**：`tests/test_zjzl.py`（11 条）+ `tests/zjzl_live.py`（真机）。

**改**：
- 单测并入 `regressions.py` 的 behavioural 档（或新建第 5 套件 `zjzl`，与现有 `check_all` 的一行汇总格式对齐）；
- `zjzl_live.py` 改成继承我们 smoke 的夹具解析（`--part` / `--assembly` / `--template`），
  并在缺夹具时 `SKIP` 而不是 FAIL；
- README 增加一行工具数与来源说明。

**验收**：`check_all` 全绿；`stdio_contract` 全绿（86 工具）；真机 live 两条通过。

---

## 6. 待验证清单（合并前必须逐条落到回执）

| # | 问题 | 判据 / 影响 |
|---|---|---|
| 1 | typed `IEdge.GetCurveParams3()` 在 32.4 是否可用 | 决定几何探测的实现分支 |
| 2 | `sheet.GetProperties2` 第 8 个值与投影法的关系 | 决定第一/第三角判定用哪个下标 |
| 3 | `extension.CreateBalloonOptions()` 在 32.4 是否存在 | 决定 BOM 气泡能否替换现实现 |
| 4 | `IBomTableAnnotation.GetComponents2/GetComponentsCount2` 在 32.4 的返回形状 | 决定件号解析是否可行 |
| 5 | `IMate2.GetMateEntityCount/MateEntity` 在 32.4 | 决定配合读取粒度 |
| 6 | `IDisplayDimension.ShowParenthesis` 的写形态（属性 or 方法） | 决定参考尺寸括号是否可信 |
| 7 | `GetPersistReference3` 跨保存重开是否稳定 | 决定「引用」能否作为长期契约 |
| 8 | `_reject_unknown_arguments` 对 pydantic 模型入参的行为 | 决定工具能否原样注册 |

---

## 7. 明确**不吸收**的部分

| 项 | 理由 |
|---|---|
| `README.md` 整段改写 | 我们的 README 已含 80 工具/能力表/门禁说明，改写会丢内容 |
| `capabilities.py` 的 `balloon` 回退 | 会覆盖 0.8.364 的 `verified` 结论 |
| `check_all.py` 的 337 断言计数与套件结构 | 与我们当前 352 / 4+stdio 冲突 |
| `zjzl_` 前缀改名 | 保留前缀可追溯来源，改名反而失去线索 |
| 其 `partial` 能力状态 | 逐项在本机真机验过后再定状态，不直接继承对方的声明 |

---

## 8. 工作量与顺序

| 批 | 内容 | 估时 | 阻塞关系 |
|---|---|---|---|
| 1 | 地基 + `view_frame` + 3 条真机探针 | 0.5 d | 无 |
| 2 | 几何探测 + 参考尺寸 | 1 d | 依赖批 1 结论 |
| 3 | 正投影 + 任意方向 | 0.5 d | 依赖批 1 `view_frame` |
| 4 | 配合 + BOM 气泡 | 0.5 d | 依赖批 2 的引用体系 |
| 5 | 测试与门禁 | 0.5 d | 依赖 1–4 |

合计约 **3 个工作日**（不含双方对语义口径的往返）。

---

## 9. 需要业主决策的三件事

| # | 问题 | 我的建议 | 备选 |
|---|---|---|---|
| 1 | 工具命名 | 保留 `zjzl_` 前缀 | 改为无前缀（统一但丢来源） |
| 2 | 件号气泡 | 新老并存，默认走 BOM 版 | 只留一个 |
| 3 | 合并门槛 | 按批次合并，每批真机验后发布 | 全部做完再一次验 |

---

## 10. 一句话风险提示

这个包的价值不在「6 个新工具」，而在**它证明了三件我们以为做不到的事**：
typed 接口能绕过 late-binding 的「找不到成员」、`ModelToViewTransform` 能算模型↔图纸、
参考尺寸可以带附件级校验。**把这三条吸收进公共层，比把 6 个工具搬过来更重要** ——
否则下一个类似增量包还要重新发现一遍。
