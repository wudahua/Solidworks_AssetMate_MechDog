# Solidworks_AssetMate_MechDog

![Before: all parts white / After: 19 part types in distinct colors](docs/images/recolor-before-after.png)

**一个会出图、也能证明这张图不能交的 SolidWorks MCP 服务。**

别的工具告诉你「生成了几张图」，它告诉你「53 个尺寸里有 7 个补不上、0 处孔标注、21 个尺寸挂不到几何上，所以这张图仅供参考」。

Windows + SolidWorks 2020+，纯 Python + COM，不需要编译 C# 插件。MCP 注册名：`Solidworks_AssetMate_MechDog`。

差异化不在"会调 API"，而在把 SolidWorks COM 的坑固化进代码与文档 → [docs/com-quirks.md](docs/com-quirks.md)。

---

## 三个能力域

### 一、装配与零件（13）

| 工具 | 作用 |
|---|---|
| `get_assembly_tree` | 组件树：零件名、路径、颜色、子装配递归 |
| `export_bom` | BOM：按零件聚合，数量/材料/单重/总重，可写 CSV |
| `get_mass_properties` | 整机质量/体积/表面积/质心 |
| `recolor_assembly` | 按零件类型批量上色（黄金比例配色，同零件同色） |
| `recolor_bodies` | 多实体零件逐实体分色 |
| `list_bodies` | 零件实体清单（面/边/顶点数） |
| `rename_components` | 重命名零件文件，可选重定向装配引用 |
| `export_preview` | 视图导出 BMP/PNG |
| `batch_export` | 批量 STEP（默认 dry-run，不覆盖） |
| `set_part_material` | 指定材质，回读材质与质量变化 |
| `assign_materials` | 按名称关键词智能分配（标准件→碳钢，结构件→铝） |
| `material_library` | 列本地材质库名称（解析受限，见边界） |
| `build_feature_ledger` | **特征台账**：特征/驱动尺寸/孔候选/模型疑点/零件类别 |

### 二、工程图（16）

| 工具 | 作用 |
|---|---|
| `create_drawing` | 建图：A0–A4、多视图、逐视图比例、模板安全区实测 |
| `add_section_view` | 剖视图（自动画剖切线） |
| `add_detail_view` | 局部放大（圆圈边界 + 独立比例） |
| `add_flat_pattern_view` | 钣金展开（下料尺寸 + 折弯线） |
| `auto_dimension` | 导入模型驱动尺寸，超 30 个会提示冗余 |
| `add_center_marks` | 孔/槽中心标记与中心线 |
| `list_circular_edges` | 列视图内圆边（图纸坐标 + 直径），孔标注的前置工具 |
| `add_hole_callout` | 原生孔标注（带回读 + 模型孔径比对） |
| `add_hole_callouts` | 批量孔标注（同直径标一次，**受扫描性能限制，见边界**） |
| `add_drawing_dimension` | 按边索引/坐标补标单个尺寸 |
| `add_dimension_tolerance` | 原生尺寸公差（双边/对称/极限/配合） |
| `add_surface_finish` | **原生表面粗糙度**（Ra 值，14 参数） |
| `set_title_block` | 标题栏：文字匹配 + 位置兜底两级策略 |
| `list_drawing_edges` | 视图可见边（索引 + 模型坐标） |
| `export_drawing` | PDF / DWG / DXF |
| `render_drawing` | PDF → PNG，供目视检查 |

### 三、自检与判定（6）

| 工具 | 回答什么问题 |
|---|---|
| `drawing_audit` | 有没有空视图、越界、互相重叠 |
| `list_drawing_annotations` | 图上到底有多少尺寸/公差/形位公差/粗糙度 |
| `verify_dimension_attachments` | 每个尺寸挂在边/面/草图段，还是根本没挂 |
| `reconcile_ledger` | **台账对账**：模型的 58 个尺寸，图纸上缺哪 7 个 |
| `delivery_check` | **这张图能不能交** |
| `survey_drawings` | 抽样真实图纸出统计基线 + HTML 画像报告 |

---

## 工程图闭环

```
特征台账 ──→ 出图 ──→ 标注 ──→ 自检 ──→ 交付判定
   │                             │           │
   │  有哪些特征/尺寸/孔          │  有没有  │  可制造
   └─────────────────────────────┴──────────→ 待设计审核
                                              仅供参考/未完成
```

`delivery_check` 的三种结论：

| 结论 | 触发条件 |
|---|---|
| **可制造** | 关键尺寸/孔/公差齐全，双重自检通过 |
| **待设计审核** | 图纸对象正确，但含调用方声明为「AI 拟定」的关键值 |
| **仅供参考/未完成** | 有越界/重叠/空视图、无尺寸、模型疑点、缺孔标注、尺寸无关联 |

实测配电箱箱体：

```
verdict: 仅供参考/未完成
blockers:  模型 Flat-Pattern1 被抑制
           图纸 0 处孔标注 / 模型 80 个圆柱面
pending:   27/53 尺寸读不到关联
           台账覆盖率 0.879（51/58，缺 7 个）
```

**这就是核心价值**：不是"生成成功"，而是"缺什么、能不能交"。

---

## 真实图纸基线

判定标准不是凭感觉定的。用 142 套 SolidWorks 工程图模板里 **6346 张真实 SLDDRW** 抽样 100 张实测：

| 指标 | 平均 | 中位 | 范围 |
|---|---|---|---|
| 视图 | 3.1 | 3 | 1–9 |
| **尺寸** | **14.2** | **11** | 0–58 |
| 注释 | 46.5 | 36 | 0–160 |
| 中心标记 | 4.9 | 3 | 0–59 |

**制造类标注的出现率**

| 标注 | 占比 |
|---|---|
| 表面粗糙度 | **60%** |
| 形位公差 | **7%** |
| 零件序号 | 3% |

据此改掉三条拍脑袋的标准：

| 原来 | 改后 | 依据 |
|---|---|---|
| 全量导入 44 个尺寸 | 提示冗余 | 真实 14 个，制图是筛选不是搬运 |
| 要求形位公差 | 降为可选、粗糙度升为常规 | 7% vs 60% |
| 视图无尺寸即报警 | 只在整张图无尺寸时报警 | 原判定 79/100 误报，改后 6 |

复跑基线（换新图纸后应重做）：

```json
{ "folder": "D:/142套solidworks工程图图纸模板",
  "sample_size": 100, "seed": 28,
  "html_path": "D:/out/survey_report.html" }
```

固定 `seed` 保证样本可复现，改阈值前后能直接对比。

---

## 能力边界（诚实披露）

| 限制 | 原因 | 影响 |
|---|---|---|
| **形位公差写不进去** | `InsertGtol` 报 `DISP_E_MEMBERNOTFOUND`；`IModelDocExtension.CreateGtolData` 成员不存在（SW2024）。两条官方路径都不可用，C# 插件也绕不过 | 只能读不能写；真实图纸仅 7% 需要 |
| **孔标注不可用** | 方法已查清（`SelectByID2` 选边 → `AddHoleCallout2`），但需要圆边精确点位，扫边 260 条超 400 秒 | 建议界面手工，或用 `add_drawing_dimension` 标 Ø |
| **`material_library` 失效** | `*.sldmat` 能定位但条目解析不出（格式/编码） | 材质名需自行确认后传入 |
| **A3 最多 3 个视图** | 剖视图 x 与父视图锁定，无法横向移开 | 钣金件拆两张图（结构 + 展开） |
| **尺寸跨视图去重需外部传值** | 图纸尺寸是投影长度，同一条边不同视图值不同 | 去重只能靠调用方给 `known_values` |
| **批量读图需逐张关闭** | SolidWorks 开 40+ 文档后任何新文件都打不开 | 批量脚本必须 `CloseDoc` |
| **DXF/DWG 逆向建模** | 未实现 | 只有 SolidWorks 原生图纸能读 |

设计原则第一条就是：**工具返回成功 ≠ 图纸正确**。所以每步都回读。

---

## 快速上手

**出一张图并自检**

```json
// 1. 建图
{ "model_path": "D:/parts/P05.SLDPRT", "output_path": "D:/out/P05.SLDDRW",
  "views": ["front", "right"], "paper": "A3", "view_scales": { "right": 0.2 } }

// 2. 标注（导入 + 中心标记 + 标题栏）
{ "drawing_path": "D:/out/P05.SLDDRW", "all_views": true, "include_unmarked": false }
{ "drawing_path": "D:/out/P05.SLDDRW", "name": "履带轮", "code": "P05", "apply": true }

// 3. 判定能不能交
{ "model_path": "D:/parts/P05.SLDPRT", "drawing_path": "D:/out/P05.SLDDRW" }
```

**查缺哪些尺寸**

```json
{ "model_path": "D:/parts/P05.SLDPRT", "drawing_path": "D:/out/P05.SLDDRW" }
→ coverage 0.879, unmatched_by_feature: { "Cut-Extrude1": [1.0, 30.0], "Vent1": [1.0] }
```

**装配上色**

```json
{ "assembly_path": "D:/models/beast.SLDASM",
  "palette": { "Crank": [0.88, 0.20, 0.18], "Frame": [0.36, 0.40, 0.45] },
  "apply": false }
```

dry-run 确认零件与匹配数无误后，再 `apply=true`。

![Body-level recolor: 8 solid bodies, 8 colors](docs/images/recolor-bodies.png)

---

## 视图数量与拆图策略

A3 / 1:2、带标注的情况下实测：

| 图幅 | 视图数 | 结果 |
|---|---|---|
| A3 | 2–3 | `drawing_audit` 零警告 |
| A3 | 4 | 互挤，常压到左侧明细栏 |
| A3 | 5 | 越界 + 重叠，不可用 |

**剖视图位置半锁**：与父视图 x 对齐（`Position` 只有 y 生效），只能放在源视图正上/正下方。

**拆图建议**（钣金件必用）：

1. **结构图**：主视 + 侧视 + 剖视（≤3 视图）
2. **展开图**：钣金展开 + 成型对照视图

实测配电箱：5 视图一张 = 4 处越界 + 1 重叠；拆成两张 = 两张都零警告。

---

## 环境与安装

- Windows + SolidWorks 2020+（开发验证 2024 SP4；用户实测 2026 SP0 可用）
- 64 位 Python 3.10+，与 SolidWorks 位数一致

```bash
pip install .
pip install ".[png]"        # 需要 PNG 输出时
```

| 现象 | 处理 |
|---|---|
| 隔离构建下载 `setuptools` 失败 | `pip install -e ".[png]" --no-build-isolation` |
| 镜像报 `from versions: none`（实为 403） | 换源 `-i https://mirrors.aliyun.com/pypi/simple/` |
| 找不到 `solidworks-assetmate-mechdog` 命令 | 入口在 `<python>\Scripts`，改用 `python -m` |
| 工程图视图创建返回空 | 0.3.0 已处理（中文版视图名 `*前视`） |
| 所有工具报 `int() argument must be ... VARIANT` | 0.1.5 已修复（强制晚绑定） |

MCP 注册（不依赖 PATH）：

```json
{
  "mcpServers": {
    "Solidworks_AssetMate_MechDog": {
      "command": "python",
      "args": ["-m", "solidworks_assetmate_mechdog.server"]
    }
  }
}
```

---

## 真机回归

```bash
python tests/smoke.py
```

13 项：连接/版本、组件树、BOM、质量属性、上色 dry-run、零匹配守卫、上色 apply、实体列表、实体分色、预览 PNG、STEP 导出、工程图链路（建图→PDF→渲染→审计）、错误信息。全部只读或写临时目录。

**仓库不含测试模型**（`测试用例/` 因体积与版权未入库），裸跑会因文件不存在直接退出。用 `--assembly` / `--part` 指定自己的模型：

```bash
python tests/smoke.py --assembly D:/models/your.SLDASM --part D:/parts/your.SLDPRT
python tests/smoke.py --skip-export --skip-drawing    # 只跑快项
```

首次约 5–8 分钟（解析轻化组件），之后 1–2 分钟。**跑前建议清理 SolidWorks 中堆积的文档**，否则会因同名文件打不开而全线失败。

---

## 性能

慢在 SolidWorks COM 往返和文件加载，不在 Python 侧。

| 操作 | 耗时 |
|---|---|
| `get_assembly_tree` 首次（46 组件） | 163 s |
| `recolor_assembly` apply（46 件） | 121 s |
| 工程图建图 + 3 视图 + 导出 | 12 s |
| `drawing_audit`（真实图纸） | 2–4 s |
| `survey_drawings`（100 张真实图） | 1916 s（19 s/张） |

首次操作单个装配请预留 2–3 分钟，不要用默认 30–60 秒超时。

---

## 设计原则

1. **工具返回成功 ≠ 图纸正确** — 每步回读（孔标注读孔径、尺寸读公差类型、剖视读实际切到的位置）
2. **统一会话层** — 所有工具共用 `sw_session`，慢调用只取一次
3. **写操作先快照** — 默认 dry-run，返回修改前的值
4. **不编造结果** — COM 不可用时返回结构化错误 + 坑文档编号，绝不假装成功
5. **判定要有依据** — 阈值由 100 张真实图纸实测，不是凭感觉
6. **每个工具配坑笔记** — 新增工具必须同步 `docs/com-quirks.md`

---

## 路线图

完整规划见 [docs/ROADMAP.md](docs/ROADMAP.md)。

- ~~v0.3 出图链路~~ ✅
- ~~v0.4 台账 / 公差 / 自检 / 交付判定~~ ✅
- **v0.5 待做**：孔标注定位（性能）、形位公差（受 API 限制）、出图前规划工作流

---

## License

MIT © 2026 wudahua
