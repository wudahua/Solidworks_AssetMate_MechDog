# SolidWorks 工程图能力借鉴与 v0.5 实施方案

> 用途：指导 `wudahua/Solidworks_AssetMate_MechDog` 工程图方向的完善。
> 调研对象：`wzyn20051216/solidworks-automation-skill`、`haunchen/solidworks-mcp`。
> 调研方式：阅读两仓库源码与文档（非二手介绍），本文所有 API 名、常量、容差、错误码均出自源码。
> 调研日期：2026-10-28。本文只做方案，不含对本项目的代码改动。

---

## 目录

1. [一页结论](#1-一页结论)
2. [本项目现状与差距](#2-本项目现状与差距)
3. [借鉴资产 A：PDF 矢量文字证据链（最高价值）](#3-借鉴资产-apdf-矢量文字证据链最高价值)
4. [借鉴资产 B：规格驱动 + 显式能力阻断](#4-借鉴资产-b规格驱动--显式能力阻断)
5. [借鉴资产 C：跨版本真机矩阵与坑清单](#5-借鉴资产-c跨版本真机矩阵与坑清单)
6. [借鉴资产 D：haunchen 的 COM 调用写法](#6-借鉴资产-dhaunchen-的-com-调用写法)
7. [v0.5 专项方案一：孔标注定位（性能）](#7-v05-专项方案一孔标注定位性能)
8. [v0.5 专项方案二：形位公差（API 限制）](#8-v05-专项方案二形位公差api-限制)
9. [v0.5 专项方案三：出图前规划工作流](#9-v05-专项方案三出图前规划工作流)
10. [落地路线图](#10-落地路线图)
11. [不借鉴清单](#11-不借鉴清单)
12. [附录](#12-附录)

---

## 1. 一页结论

### 1.1 总体判断

| 维度 | 领先者 | 说明 |
|---|---|---|
| 出图功能广度（公差 / 粗糙度 / 展开 / 标题栏） | **本项目** | 另外两家基本没有 |
| 图纸正确性判定、模型↔图纸对账 | **本项目** | 另外两家完全没有 |
| 判定依据客观性（真实图纸基线） | **本项目** | 唯一有 100 张实测基线 |
| 证据链的机器可验证性 | wzyn | COM 位置 ↔ PDF 矢量文字框一一关联 |
| 能力边界的工程化表达 | wzyn | 声明期显式阻断，不静默降级 |
| 远程调用与客户端兼容 | haunchen | 但带严重安全隐患，不借鉴部署方式 |

**结论：不扩功能广度，把 v0.5 的重心放在"证据链 + 声明期阻断 + 规划层"，同时用更低成本的路径解掉孔标注。**

### 1.2 v0.5 三项解法速查

| v0.5 待做 | 根因 | 推荐解法 | 预期收益 |
|---|---|---|---|
| 孔标注定位（性能） | 全模型扫边 260 条 > 400 s | **主路径改 `InsertModelAnnotations3` 位掩码 `0x100000`，零扫边**；辅路径用 `GetVisibleEntities2` 只扫视图可见边 | 400 s → 秒级 |
| 形位公差（API 限制） | `DISP_E_MEMBERNOTFOUND` 疑似晚期绑定问题，未证实是 API 不存在 | 先做**早绑定五步诊断**再定性；不通则走**占位模板改值**；产品策略保持"可选 + 只读核验"（真实图纸仅 7% 需要） | 避免误判为"永远做不了" |
| 出图前规划工作流 | 缺"标什么、怎么排"的决策层 | 新增 `plan_drawing`，由 `build_feature_ledger` + 实测基线驱动，产出 DrawingPlan JSON，串起现有全部工具 | 一次通过率提升，尺寸数落入 11–14 区间 |

### 1.3 六项可借鉴资产

| # | 资产 | 来源 | 优先级 |
|---|---|---|---|
| A | PDF 矢量文字证据链（坐标变换 + 一一匹配 + 碰撞检测 + 状态机） | wzyn | P0 |
| B | DrawingSpec 规格驱动 + `DRAWING_SPEC_CAPABILITY_UNSUPPORTED` 显式阻断 | wzyn | P0 |
| C | 跨版本真机矩阵 + 4 条已踩过的坑 | wzyn | P1 |
| D | 一批正确的 COM 调用写法（选边、圆边参数、剖视/局部/自定义视图、气泡、BOM） | haunchen | P0（孔标注依赖） |
| E | 错误码 + 结构化报告的文件级规范 | wzyn | P1 |
| F | 模型↔PDF 双向坐标回写（用真实渲染框替换 COM 估算框再做布局检查） | wzyn | P1 |

---

## 2. 本项目现状与差距

### 2.1 已有资产（不要重做）

- 34 工具 / 三域：装配零件 13、工程图 15、自检判定 6。
- 闭环：`特征台账 → 出图 → 标注 → 自检 → 交付判定`。
- `delivery_check` 三态：可制造 / 待设计审核 / 仅供参考未完成。
- 基线：142 套模板 6346 张真实 SLDDRW 抽样 100 张 → 平均 14.2 尺寸（中位 11）、注释 46.5、中心标记 4.9；粗糙度 60%、形位公差 7%、零件序号 3%。
- 设计原则已正确：每步回读、统一 `sw_session`、写操作 dry-run、不编造结果、判定要有依据、每工具配坑笔记。

### 2.2 差距清单（本次要补的）

| # | 差距 | 影响 |
|---|---|---|
| G1 | `render_drawing` 只出 PNG 供目视，没有机器可判的渲染证据 | `delivery_check` 最关键一环仍依赖人 |
| G2 | 尺寸边界靠 COM 估算，未与最终 PDF 实际渲染框对齐 | 重叠/越界判断可能漏报或误报 |
| G3 | 不支持的能力是"做完再判仅供参考"，没有声明期阻断 | 在已知做不好的事上烧 COM 时间（孔标注 400 s 即为例） |
| G4 | 版本口径靠文字描述（2024 SP4 开发 / 2026 SP0 用户实测），无可复跑矩阵 | 回归结论不可复现 |
| G5 | 孔标注路径选错（全模型扫边 + `SelectByID2`） | 性能不可接受，功能实际不可用 |
| G6 | 缺出图前规划层，`auto_dimension` 只能"超 30 提示冗余" | 与"制图是筛选不是搬运"的洞察不一致 |

---

## 3. 借鉴资产 A：PDF 矢量文字证据链（最高价值）

来源：`subskills/solidworks-engineering-drawing/scripts/drawing_review.py` + 根 `scripts/sw_review.py`。

### 3.1 它解决了什么

把"图纸上看着对不对"从**人工目视**变成**机器可判**：强制导出 PDF，把每个 COM 尺寸的位置与 PDF 中**实际渲染出来的矢量文字框**一一关联，再做碰撞检测；关联不全或有碰撞就不放行。

其真机案例（NIST Additive Manufacturing Test Artifact Ver3B，SW2026 SP01.1，GB/T 第一角 A1）交付物为：原生 SLDPRT + SLDDRW、三视图 + 等轴测 + A-A 剖视、10 个必需尺寸、PDF、BMP、`drawing_evidence.json`、`drawing_review_report.json`，定向测试 70 项通过、PDF 文字边界 0 重叠。

### 3.2 四层证据结构

```
① 规格语义核验   视图/尺寸/孔/专业标注/BOM/标题栏/注释是否满足规格
② COM 结构证据   structure：sheet_size、sheets、dimensions[].box_evidence.position_m、注释位置
③ PDF 矢量文字   COM 位置 ↔ PDF 文字框一一匹配 + 文字框碰撞检测   ← 核心，我们最缺
④ 布局与预览     review_drawing_layout()，用③替换后的真实框再判
        ↓
   pass / review_required / blocked
```

### 3.3 依赖与入口保护

- PDF 解析库为 **PyMuPDF**（`import fitz`），封装在 `_import_pdf_parser()`：
  1. 直接 `import fitz`；
  2. 失败则读环境变量 `CADSTUDIO_PYMUPDF_PATH`，展开为绝对路径，`sys.path.insert(0, ...)` 后重试；
  3. 仍失败则抛原始 `ImportError`。
- 缺依赖时返回 `DRAWING_PDF_TEXT_PARSER_MISSING`，提示安装 `requirements-pdf.txt`。
- 其他入口错误码：

| 错误码 | 触发条件 |
|---|---|
| `DRAWING_PDF_INVALID` | 非 `.pdf`、文件不存在、为空、或超过 `256 * 1024 * 1024` 字节 |
| `DRAWING_PDF_TEXT_PARSE_FAILED` | 打开/解析失败，含 span 超上限；`retryable=True` |
| `DRAWING_PDF_VECTOR_TEXT_MISSING` | 解析成功但没有任何矢量文字 |
| `DRAWING_PDF_TEXT_OVERLAP_RISK` | 检测到至少一对文字框重叠 |

**移植建议**：本项目已有 `pip install ".[png]"` 的 extra 模式，照搬新增 `.[pdf]`（PyMuPDF），缺依赖时 `delivery_check` 降级为"仅供参考"并明确说明缺证据，而不是假装通过。

### 3.4 文字 span 提取规则

```python
maximum_spans = 20000
minimum_overlap_area_pt2 = 0.5
with fitz.open(source) as document:
    payload = page.get_text("dict")   # blocks → lines → spans
```

每个 span 必须：`text.strip()` 非空、`bbox` 恰好 4 个值、4 个坐标均可转有限浮点（`math.isfinite`）。产出：

```json
{ "page": 1, "text": "...", "bboxPt": [x0,y0,x1,y1],
  "font": "...", "fontSizePt": 10.5, "dimensionCandidate": true }
```

每页另记 `widthPt = page.rect.width`、`heightPt = page.rect.height`、`textSpans`。总 span 超 20000 直接报错（防内存/耗时失控）。统计 `text_span_count` 与 `numeric_text_span_count`。

### 3.5 尺寸文字候选正则（可直接照抄）

```python
_PDF_DIMENSION_TEXT = re.compile(
    r"(?:^\s*\d+(?:[.,]\d+)?\s*$|[Ø⌀Rr]\s*\d|\bM\d"
    r"|\d+(?:[.,]\d+)?\s*(?:mm|°|deg)\b|\d+(?:[.,]\d+)?\s*[±]\s*\d)",
    re.IGNORECASE,
)
```

覆盖五类：纯数字（含小数逗号）、`Ø/⌀/R/r` 前缀、公制螺纹 `M\d`、带单位 `mm/°/deg`、公差 `±`。
注意：该标记只用于候选筛选与统计，**重叠检测作用于全页所有 span**，不过滤。

### 3.6 坐标变换（关键公式）

COM 图纸坐标：**左下角原点、Y 向上、单位米**。PDF 文字框：**左上角原点、Y 向下、单位 pt**。

```python
expected_x = position[0] / width_m * page_width
expected_y = page_height - position[1] / height_m * page_height
```

匹配成功后**反向回写**，用真实渲染框替换 COM 估算框：

```python
box_m = {
  "left":   left / page_width * width_m,
  "bottom": (page_height - bottom) / page_height * height_m,
  "right":  right / page_width * width_m,
  "top":    (page_height - top) / page_height * height_m,
}
dimensions[index]["box"] = box_m
dimensions[index]["box_source"] = "pdf_vector_text"
dimensions[index]["box_confidence"] = "high"
```

这一步是资产 F：后续所有布局/越界/重叠判断都建立在**真实渲染框**上，而非 COM 估算值。

### 3.7 一一匹配算法

```python
def inspect_pdf_dimension_rendering(pdf_path, structure, *, match_tolerance_m=0.012):
```

- 默认容差 **12 mm（图纸空间中心距离）**。
- 换算成 pt：`tolerance_pt = tolerance_m / min(width_m/page_width, height_m/page_height)`（取较小者 = 取较大 pt 容差）。
- 候选 span 必须：未被占用、`dimensionCandidate` 为真、在该尺寸对应页。
- 距离为文字框中心到 COM 期望位置的欧氏距离，`<= tolerance_pt` 才入候选，取**最近者**。
- `used.add((page_index, candidate_index))` 保证一个 span 不被多个尺寸复用 → 真正的一一对应。
- 位置不足两个坐标、页码不存在、坐标无法转 float → 进 `unmatched_dimension_indexes`。
- 注意：此函数**只按位置匹配**，不校验文字内容相等；语义核验由 `_required_dimension_report()` 另行负责（按精确 ID / 视图 / 种类 / 文字 / 数值，`valueMm` 容差 0.01）。

### 3.8 碰撞检测（两套，别混淆）

**① 已匹配尺寸框之间**：同页两两比较 O(n²)，严格正面积交集才算：

```python
min(r1,r2) > max(l1,l2) and min(b1,b2) > max(t1,t2)
```

边缘/角点接触（严格 `>`）不算。错误码 `DRAWING_RENDERED_DIMENSION_TEXT_OVERLAP`。

**② PDF 全页文字重叠**：按 `bboxPt[0]` 排序 + `active` 列表（只保留 `bboxPt[2] > x0` 者），交集面积 `> 0.5 Pt²` 才记录；并用 `_pdf_text_overlap_candidate()` 排除"去空白后长度为 1 的字母或数字"，避免图框分区字母、页码造成伪重叠。错误码 `DRAWING_PDF_TEXT_OVERLAP_RISK`，且会使总状态 `blocked`。

每条证据带：

```json
{ "confirmedGeometryOverlap": true, "confirmedVisualDefect": false,
  "evidenceSource": "pdf_vector_text_bbox" }
```

**这是必须保留的诚实口径**：只能证明文字包围盒几何相交，不能证明视觉缺陷，也不能证明文字与尺寸线/几何线/标题栏边框无碰撞。

### 3.9 布局检查错误码全集（`review_drawing_layout`，`padding_m=0.001`）

| 检查 | 错误码 / 检查项 | 级别 |
|---|---|---|
| 视图数 < 3 | `DRAWING_STANDARD_VIEWS_MISSING` | blocked |
| 视图缺边界框 | `drawing-view-boxes` | 证据不完整 |
| 视图互相重叠 | `DRAWING_VIEW_OVERLAP` | fail |
| 视图侵入标题栏 | `DRAWING_VIEW_TITLE_BLOCK_INTRUSION` | fail |
| 尺寸文字互相重叠 | `DRAWING_DIMENSION_TEXT_OVERLAP` | 原生框 fail / 估算框 warning |
| 尺寸侵入标题栏 | `DRAWING_DIMENSION_TITLE_BLOCK_INTRUSION` | — |
| 尺寸侵入非所属视图 | `DRAWING_DIMENSION_OTHER_VIEW_INTRUSION` | 固定 warning（同 `view` 名跳过） |
| 注释侵入标题栏 / 视图 | `DRAWING_NOTE_TITLE_BLOCK_INTRUSION` / `DRAWING_NOTE_VIEW_INTRUSION` | — |
| 注释与尺寸重叠 / 注释互叠 | `DRAWING_NOTE_DIMENSION_OVERLAP` / `DRAWING_NOTE_TEXT_OVERLAP` | — |
| 缺标题栏区域 | `drawing-title-block-box` | warning |
| 预览存在且非空白 | `drawing-pixel-preview` | pass 条件 |

豁免规则：注释仅在 `same_sheet()` 时检查（任一方缺 `sheet` 仍保守检查）；剖视/局部视图的所属标签豁免（`note_kind == "view_label"` 且 `owner_view` 匹配）。

**已知缺口（我们不要照抄）**：它**没有图纸页边距 / 对象越界检查**，`widthPt/heightPt` 只记录不参与判定。本项目的 `drawing_audit` 已有越界检查，这是我们的优势，合并时保留。

### 3.10 总状态判定优先级

`blocked`（任一）：layout blocked、PDF 尺寸渲染 blocked、注释核验 blocked、PDF 全页文字 overlaps。
`review_required`（未被阻断且任一）：有 `fail` 级 findings、BOM/标题栏需语义人工复核、layout 非 pass、PDF 尺寸渲染非 pass、规格验证状态为 `pilot`。
`pass`：以上皆无。`manual_review_required = (status != "pass")`，返回值固定带 `capability_level: "pilot"`。

顶层 `error_code` 优先级：`DRAWING_REVIEW_FINDINGS` → `DRAWING_SEMANTIC_EVIDENCE_INCOMPLETE` → 沿用 PDF 尺寸渲染错误码 → 布局错误码。缺 `pdf_path` 或非空 `structure` 时直接 `DRAWING_FINAL_PDF_REQUIRED`。

### 3.11 报告字段（供我们对齐命名）

```
status, stage="review", standard, projection, document_type,
checks[], findings[], layout, view_evidence, dimension_evidence,
hole_evidence, professional_annotation_evidence, bom_evidence,
title_block_evidence, note_evidence, pdf_evidence,
pdf_dimension_rendering{ status, matched[], unmatched_dimension_indexes[],
                         collisions[], error_code, pdf },
manual_review_required, error_code, capability_level
```

`matched[]` 每项：`dimension_index, dimension_name, page, text, bbox_pt[4], box_m{left,bottom,right,top}, distance_to_com_position_pt`。

### 3.12 其他语义核验的容差（可借鉴进 `reconcile_ledger`）

- 必需尺寸：精确 ID + 视图 + 种类 + 文字 + 数值，`valueMm` 容差 **0.01**。
- 孔：规格 + 数量 + 逐孔位置，位置容差 **0.1 mm**，直径容差 **0.05 mm**。
- 专业标注：只接受 SolidWorks 专用实体回读，**普通 Note 文字不算通过证据**。
- 注释：必须同时具备 COM 文字 + 位置 + 最终 PDF 精确文字框三重证据。

### 3.13 落地到本项目的改造方案

| 动作 | 说明 |
|---|---|
| 新增工具 `verify_drawing_rendering` | 输入 `drawing_path`（自动找同名 PDF 或先调 `export_drawing`），输出上述 `pdf_dimension_rendering` 结构 |
| 扩展 `list_drawing_annotations` | 每个尺寸补 `box_evidence.position_m`（COM 文字位置，米）与 `sheet`，作为匹配输入 |
| 扩展 `drawing_audit` | 增加"基于 PDF 真实渲染框"的重叠/侵入检查，与现有越界检查合并；标注 `box_source`（`native` / `pdf_vector_text` / `estimated`） |
| 扩展 `delivery_check` | 新增 blocker：`DRAWING_FINAL_PDF_REQUIRED`、`DRAWING_PDF_DIMENSION_MATCH_INCOMPLETE`、`DRAWING_RENDERED_DIMENSION_TEXT_OVERLAP`；缺 PyMuPDF 时降级为"仅供参考"并注明缺证据 |
| 新增 extra `.[pdf]` | PyMuPDF；`requirements-pdf.txt` |
| 文档 | `docs/com-quirks.md` 增"PDF 证据链"条目，写清几何相交 ≠ 视觉缺陷 |

**口径警示**：wzyn 自己的 `references/drawing.md` 写"PDF 文字边界只能用于风险筛查"，但代码里把匹配后的框标成 `box_confidence: "high"` 并作为主证据。两处口径不一致。我们采用**保守口径**：PDF 框用于提升重叠/越界判断可信度，但不单独作为"可制造"的充分条件，仍需 `verify_dimension_attachments` + `reconcile_ledger` 通过。

**验收标准**：同一零件出图后，`verify_drawing_rendering` 返回匹配率 100%、0 碰撞时，`drawing_audit` 与人工目视结论一致；人为把两个尺寸拉到一起，必须报 `DRAWING_RENDERED_DIMENSION_TEXT_OVERLAP`。

---

## 4. 借鉴资产 B：规格驱动 + 显式能力阻断

### 4.1 DrawingSpec v1 字段（`schemas/drawing_spec.schema.json`）

`additionalProperties: false`，必填：`schemaVersion, sourceModel, documentType, standard, projection, paperSize, modelSizeMm, views, outputs`。

| 字段 | 约束 |
|---|---|
| `schemaVersion` | const `"1.0"` |
| `documentType` | `part` / `assembly` / `sheet_metal` |
| `standard` | `GB_T` / `ISO` |
| `projection` | `first_angle` / `third_angle` |
| `paperSize` | `A4`–`A0` |
| `modelSizeMm` | 3 个正数（包围盒） |
| `scale` | 正数，或 `"1:2"` 形式字符串（正则 `^\s*[0-9]+(\.[0-9]+)?\s*:\s*[0-9]+(\.[0-9]+)?\s*$`） |
| `views` | 必填 `front`；另有 `top/bottom/left/right/isometric`、`sections[]`、`details[]` |
| `requiredDimensions[]` | 必填 `id, kind, view`；`kind ∈ overall/linear/angular/radius/diameter/tolerance/datum/gdt`；可带 `valueMm/text/tolerance/datum` |
| `holeRequirements[]` | 必填 `id, specification, count, locationsMm`（2–3 维坐标数组）；可带 `view/datum` |
| `professionalAnnotations` | `centerMarks[]`（必填 `id/view/count/targets`，`targets ⊆ {holes,fillets,slots}` 且唯一）、`centerLines[]`（`id/view/count`）、`holeCallouts[]/datums[]/geometricTolerances[]/surfaceFinishSymbols[]/weldSymbols[]`（`id/view/text`，可带 `count`） |
| `outputs` | 必填 `slddrw, pdf, report`；另有 `bmp/png` |
| 其他 | `insertModelDimensions`、`templateCandidates[]`、`titleBlock{}`、`notes[]`、`bom{}`、`sheetMetal{}`、`modelEvidence` |

### 4.2 前置规则（值得照搬）

- `standard=GB_T` 默认 `projection=first_angle`；**GB/T + 第三角直接被前置检查阻断**，不进入执行。
- 所有孔、槽、接口必须有规格、数量、定位信息，否则不通过校验。
- 工程图交付**必须导出 PDF** 并完成 COM↔PDF 关联，否则阻断或要求复核。
- 不暴露任意 Python/VBA 执行入口（安全底线）。

### 4.3 显式能力阻断机制

通用生成器只可靠执行：空配置 `front/top/right` 三视图、显式且可容纳的比例、模型尺寸导入、注释、模板化 BOM。
以下一律返回 **`DRAWING_SPEC_CAPABILITY_UNSUPPORTED`**，且**必须在修改文档之前返回**：轴测、剖视、局部视图、指定 ID 尺寸、孔标注/孔表、标题栏业务字段、钣金专用选项。
钣金图只有在本机存在可靠展开图证据时才继续，否则 `blocked`，不宣称无人值守完成。

### 4.4 与本项目 `delivery_check` 的关系（互补，不冲突）

```
声明期（改文档之前）          交付期（做完之后）
能力预检 + 规格校验      →     delivery_check 三态判定
不支持就直接拒绝              缺什么、能不能交
```

现在本项目只有右半边。补左半边的直接收益：孔标注这类已知不可用的请求，在 400 秒之前就被拦下并给出替代建议（`add_drawing_dimension` 标 Ø），而不是跑完再判"仅供参考"。

### 4.5 落地建议

1. 新增 `capabilities.yaml`（或 `drawing_capabilities.json`）：每个工程图能力标 `verified / pilot / reference_only / not_implemented`，与 `docs/com-quirks.md` 编号互链。
2. 所有写类工具入口先过 `assert_capability()`，不支持即返回结构化错误 + 替代方案 + 坑文档编号。
3. `plan_drawing`（见第 9 节）输出即为本项目的 DrawingSpec 等价物，字段命名可直接对齐上表，减少自造概念。
4. 标准/投影前置校验：`create_drawing` 增加 `standard` 与 `projection` 参数，GB/T + 第三角直接拒绝。

---

## 5. 借鉴资产 C：跨版本真机矩阵与坑清单

### 5.1 版本矩阵做法

```powershell
$env:CAD_STUDIO_VISIBLE='false'
python tests/solidworks_drawing_version_matrix.py --years 2024 2025 2026
```

- 只启动**已注册的精确版本 ProgID**，串行执行。
- **核对实际回读年份与请求年份一致才记 `pass`**；未安装记 `unavailable`。
- 明确禁止用默认 ProgID 的结果顶替指定版本。
- 输出每版本独立报告 + 总矩阵 JSON。

**落地**：本项目 `tests/smoke.py` 已有 13 项，加 `--years` 参数与 ProgID 精确分派即可，把 README 里"2024 SP4 开发验证 / 2026 SP0 用户实测"变成可复跑结论。

### 5.2 四条已踩过的坑（直接进 `docs/com-quirks.md`）

| # | 坑 | 后果 | 对策 |
|---|---|---|---|
| C1 | **SW2026 新建工程图必须先首次保存再插入注解** | 未保存时插入 API 返回 `True`，但实体暂时回读不到 | 封装在回读不到时保持失败；建图后立即保存一次再继续 |
| C2 | `InsertModelAnnotations3/4` 返回值不能作为尺寸已插入的证据 | 假完成 | 必须再读真实 `DisplayDimension` |
| C3 | `SetupSheet6` / `NewSheet4` 的投影参数必须与布局一致 | 第一角布局 + 第三角 sheet 设置 = 视图位置全错 | 生成后回读投影法；GB/T 默认第一角（俯视在主视下方、右视在主视左侧） |
| C4 | 图框文件名只代表候选证据 | 名为 GB/T 的模板内容未必合规 | 模板内容需独立核验，不能凭文件名通过 |

---

## 6. 借鉴资产 D：haunchen 的 COM 调用写法

来源：`src/tools/annotation.py`、`src/tools/drawing.py`。该项目工程图判定能力为零，但**底层 COM 写法有多处比我们当前文档记录的更稳**。

### 6.1 线程模型

```
MCP client ◄─ Streamable HTTP ─► FastMCP（async handler）
                                     │ sw.execute(func) → asyncio + Future
                                     ▼
                          COM Worker Thread（STA，singleton）
                                     │ pywin32 COM → SolidWorks
```

SolidWorks COM 必须单 STA 线程驱动，所有 async handler 把 COM callable 派发到专用 worker，用 `concurrent.futures.Future` 桥回。
**落地检查项**：本项目 `sw_session` 统一了会话，但需确认是否同时保证了**所有 COM 调用落在同一 STA 线程**（含批量/并发场景）。若目前靠"实际都串行"隐式成立，建议显式化，避免日后加并发时踩 `RPC_E_WRONG_THREAD`。

### 6.2 选取：不要用 `SelectByID2`

| 目的 | 推荐写法 | 备注 |
|---|---|---|
| 选边/面 | `view.SelectEntity(edge, append)` | 全库零 `SelectByID2`，注释明确 late-binding 下有类型问题 |
| 选视图 | `drawing.SelectByID(name, "DRAWINGVIEW", 0, 0, 0)` | 5 参数版 |
| 选注释（删除用） | `drawing.SelectByID(name, "NOTE", 0, 0, 0)` | 可试 `f"{name}@{view_name}"` 再退化 `name` |
| 选后清场 | `drawing.ClearSelection2(True)` | 每次选取前 |

### 6.3 可见边枚举与圆边参数（孔标注辅路径的核心）

```python
comps = view.GetVisibleComponents                    # 属性，不是方法
edges = view.GetVisibleEntities2(comp, 1)            # 1 = swViewEntityType_Edge
```

- **必须传 component**，传 `None` 会 `DISP_E_TYPEMISMATCH`。
- 只返回该视图**可见**边，数量远小于全模型边。
- 返回值兼容 tuple/list 与 SAFEARRAY（后者按下标循环）。

```python
curve = edge.GetCurve
if curve.IsCircle:
    if edge.GetStartVertex is None:                  # 无起点 = 完整圆；有起点 = 圆弧
        cp = curve.CircleParams                      # (cx,cy,cz, ax,ay,az, radius)
        center = (cp[0], cp[1], cp[2]); radius = cp[6]
```

- `CircleParams` 单位是**米**，转 mm 乘 1000；半径在索引 `[6]`。
- 直线用 `curve.LineParams`，方向向量在 `[3][4][5]`，需归一化。
- 同半径去重容差 `_RADIUS_TOL = 1e-4`（0.1 mm），每组保留离所有圆几何重心最远者。

### 6.4 边定位的双层匹配（比我们"边索引/坐标"更稳）

```python
_resolve_edge(): 先 index 精确匹配（中点距离 <= 0.5 mm），失败退化 proximity（<= 2.0 mm，否则抛错）
_check_edge_type(): diameter 需要 "circle"，radius 需要 "arc"，角度需要 "line"，不符直接报清楚
```

建尺寸时方向遍历 + 退化：

```python
for d in range(4):
    disp_dim = ext.AddDimension(text_x, text_y, 0, d)
    if disp_dim is not None: break
if disp_dim is None:
    disp_dim = drawing.AddDimension2(text_x, text_y, 0)
```

文字位置：视图右边界 + 偏移（`_DIM_TEXT_OFFSET_MM = 15.0` mm，`DIM_DIAMETER_OFFSET = 0.008` m），多颗圆纵向堆叠 `DIM_OFFSET_STACK = 0.010` m；视图边界取 `view.GetOutline`（`[xMin,yMin,xMax,yMax]`，米）。

### 6.5 第一角对齐三视图

```python
result = drawing.Create1stAngleViews2(os.path.basename(source_doc))
# 抛异常则退化完整路径
result = drawing.Create1stAngleViews2(source_doc)
drawing.ViewZoomtofit2()
```

位置、比例、显示模式全由 SolidWorks 自动处理，**自动建立有投影关联的三视图**。
**对本项目的价值**：当前剖视图 x 与父视图锁定、A3 最多 3 视图的布局约束，部分来自手工摆视图。用 `Create1stAngleViews2` 生成基础三视图再补剖视/局部，可能减少互挤。值得做一次对照实验并写入坑文档。

### 6.6 独立标准视图（含中文视图名与布局比例）

```python
view = drawing.CreateDrawViewFromModelView3(source_doc, "*前視", x, y, 0)
view.ScaleRatio = (1.0, scale)          # scale=2 表示 1:2
view.SetDisplayMode3(False, 6, False, False)   # 6 = swHIDDEN_LINES_REMOVED
```

中文版 SW2021 视图名映射：`front→*前視`、`back→*後視`、`top→*上視`、`bottom→*下視`、`right→*右視`、`left→*左視`、`isometric→*等角視`、`trimetric→*不等角視圖`、`dimetric→*二等角視圖`。未知名只警告跳过，不中断。

第一角布局用纸张宽高的比例坐标（本项目做自动布局可参考）：

| 视图 | 位置比例 | | 视图 | 位置比例 |
|---|---|---|---|---|
| front | (0.40, 0.55) | | left | (0.65, 0.55) |
| top | (0.40, 0.25) | | bottom | (0.40, 0.85) |
| right | (0.15, 0.55) | | back | (0.90, 0.55) |
| isometric | (0.75, 0.25) | | 未定义 | (0.5, 0.5) |

纸张 COM 常量：`A4=8, A3=9, A2=10, A1=11, A0=12`；对应 mm：297×210 / 420×297 / 594×420 / 841×594 / 1189×841。
`NewDocument(template_path, paper_const, 0, 0)`，未知纸张回退默认再回退 A3。
注意：它**硬编码纸张尺寸**是因为 pywin32 late-binding 调不动 `IDrawingDoc.GetCurrentSheet` —— 这也是一条坑，我们若已解决应在坑文档里记下解法。

### 6.7 剖视图精确参数

```python
drawing.ActivateView(parent_view); drawing.ClearSelection2(True)
drawing.SketchManager.CreateLine(sx, sy, 0, ex, ey, 0)      # sheet 绝对坐标，米
section_view = drawing.CreateSectionViewAt5(
    pos_x, pos_y, 0, label, 0, None, 0)                     # options / excludedComponents / sectionDepth
section_view.ScaleRatio = (1.0, scale)
drawing.EditRebuild3()
```

默认位置：父视图右侧 50 mm（`outline[2] + 0.05`），垂直居中。

### 6.8 局部放大精确参数

```python
drawing.SketchManager.CreateCircle(cx, cy, 0, cx + r, cy, 0)
detail = drawing.CreateDetailViewAt4(
    pos_x, pos_y, 0,
    0,            # style = swDetViewSTANDARD
    scale, 1.0,   # 比例直接在这里传，不是事后 ScaleRatio
    label,
    1,            # showtype = 圆+圆
    True, False, False, 5)   # fullOutline / jaggedOutline / noOutline / shapeIntensity
drawing.EditRebuild3()
```

默认位置：父视图右侧 50 mm、与父视图上缘齐平。默认 `scale=2.0`（2:1）。

### 6.9 自定义方向视图（任意 XYZ 旋转）

1. `app.ActivateDoc(basename)`（失败退化完整路径）；`model.ShowNamedView2("", 1)` 复位前视。
2. `mv = model.ActiveView; t = mv.Orientation3; t.ArrayData = arr; mv.Orientation3 = t`。
3. `model.NameView("_mcp_custom_temp")` → 切回图纸 → `CreateDrawViewFromModelView3(..., "_mcp_custom_temp", ...)`。
4. `finally` 中一律 `DeleteNamedView` 并激活回图纸。
5. `ArrayData` 赋值失败时改用 `win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, arr)`。

旋转为 XYZ 外旋 `R = Rz·Ry·Rx`，`MathTransform` 16 元素布局 `[R00,R01,R02,0, R10,R11,R12,0, R20,R21,R22,0, Tx,Ty,Tz,Scale]`，平移 0、缩放 1。
显示模式常量：`wireframe=1`、`shaded=3`、`hidden_lines_removed=6`。

### 6.10 气泡与 BOM（本项目目前缺）

```python
drawing.SelectByID(view_name, "DRAWINGVIEW", 0, 0, 0)
opts = drawing.CreateAutoBalloonOptions()
opts.Layout = 4                 # 右侧布局
opts.Style = 1                  # circular:1 / triangle:2 / hexagon:4
opts.Size = 0                   # SW_BALLOON_FIT_TIGHTEST
opts.UpperTextContent = ...     # ITEM_NUM
opts.InsertMagneticLine = False
drawing.AutoBalloon5(opts)      # 退化 AutoBalloon() / AutoBalloon2()
```

- 用**属性赋值**而非位置参数，避开 positional `VARIANT_BOOL` 的 `DISP_E_TYPEMISMATCH`。
- 零件关联靠 BOM item number：`note.GetBomBalloonTexts(True)` → `GetText()` / `GetComponent().Name2`（带实例后缀全名，过滤用名称包含比对）。
- BOM 表用 `InsertBomTable4`，需处理返回 `None` 的退化路径。
- `SW_BOM_TOP_LEVEL_ONLY = 1`。

**是否要做的判断**：真实图纸基线里零件序号只有 3%，所以**优先级低**；但如果 v0.5 之后要支持装配图交付，气泡 + 图内 BOM 是硬需求，届时直接照此实现，不必重新摸 API。

### 6.11 模型尺寸导入的位掩码常量

| 常量 | 值 |
|---|---|
| `SW_INSERT_DIMENSIONS` | `8` (0x8) |
| `SW_INSERT_DIMS_MARKED_FOR_DRAWING` | `32768` (0x8000) |
| `SW_INSERT_DIMS_NOT_MARKED_FOR_DRAWING` | `524288` (0x80000) |
| `SW_INSERT_HOLE_WIZARD_PROFILE` | `65536` (0x10000) |
| `SW_INSERT_HOLE_WIZARD_LOCATION` | `131072` (0x20000) |
| **`SW_INSERT_HOLE_CALLOUT`** | **`1048576` (0x100000)** |
| `SW_UNIT_MM` | `0` |
| `SW_INPUT_DIM_VAL_ON_CREATE` | `220`（创建时关闭尺寸值输入框） |

---

## 7. v0.5 专项方案一：孔标注定位（性能）

### 7.1 根因

现路径 = 全模型扫边（260 条，>400 s）拿圆边精确点位 → `SelectByID2` 选边 → `AddHoleCallout2`。两个问题叠加：**扫描范围错了**（全模型而非视图可见边），**选取 API 错了**（`SelectByID2` 在 late-binding 下不稳）。功能已实现但不可用。

### 7.2 方案 1（首选）：位掩码批量导入，零扫边

```python
InsertModelAnnotations3(..., SW_INSERT_HOLE_CALLOUT /* 0x100000 */, ...)
```

由 SolidWorks 自己生成孔标注，完全不需要定位圆边。haunchen 全库没有独立孔标注函数，走的就是这条路。

- 优点：秒级、无定位误差、原生实体。
- 限制：按视图/按孔筛选与样式控制弱，导入数量取决于模型与视图。
- **强制配套**：按坑 C2，返回值不算证据，必须回读 `DisplayDimension` / 注解实体，并把孔径与模型孔径比对（本项目 `add_hole_callout` 已有"回读 + 模型孔径比对"设计，保留）。

### 7.3 方案 2（辅路径）：只扫视图可见边

用于需要精确控制"哪个视图、哪个孔、标注文字位置"的场景。

```
view.GetVisibleComponents → 逐 comp 调 view.GetVisibleEntities2(comp, 1)
  → IsCircle + GetStartVertex is None 过滤完整圆
  → CircleParams 取圆心/半径（米→mm）
  → 与模型孔清单按半径/位置匹配（复用 build_feature_ledger 的孔候选）
  → view.SelectEntity(edge, False)     ← 不是 SelectByID2
  → AddHoleCallout2 → 回读孔径与文本
```

- 扫描量从"全模型 260 条"降为"该视图可见边"，且圆过滤在 COM 往返最少的顺序下做。
- 加**硬预算**：单视图扫边超时（建议 20–30 s）即中止并返回结构化错误 + 建议改走方案 1 或手工，**不再出现 400 s 无反馈**。
- 选取后必须 `ClearSelection2(True)` 清场。

### 7.4 方案 3（可选）：模型侧驱动，进一步缩小候选

`build_feature_ledger` 已产出孔候选与圆柱面信息。先用模型侧的孔径/轴线筛掉绝大多数边，只对匹配半径的可见圆边做选取。孔数量大（如配电箱 80 个圆柱面）时，方案 1 + 方案 3 组合比纯方案 2 更稳。

### 7.5 决策树

```
需要孔标注？
 ├─ 只要"图上有原生孔标注"      → 方案 1（位掩码）
 ├─ 要指定视图/指定孔/指定位置  → 方案 2（可见边 + SelectEntity）
 │    └─ 孔很多或扫边超预算      → 方案 3 预筛，或退回方案 1 + 手工微调
 └─ 模型无可用孔特征/回读失败   → 显式阻断，给 add_drawing_dimension 标 Ø 的替代建议
```

### 7.6 验收指标

| 指标 | 目标 |
|---|---|
| 单视图孔标注耗时 | < 10 s（方案 1），< 30 s（方案 2 含扫边） |
| 回读孔径与模型孔径一致率 | 100%，不一致即失败不放行 |
| 超预算行为 | 返回结构化错误 + 替代建议，绝不长时间挂起 |
| `delivery_check` | 原"0 处孔标注 / 模型 80 个圆柱面"类 blocker 能正常解除 |
| 坑文档 | 新增条目：位掩码常量、可见边枚举、`SelectEntity` 替代 `SelectByID2`、超时预算 |

---

## 8. v0.5 专项方案二：形位公差（API 限制）

### 8.1 先纠正一个可能的误判

当前记录：`InsertGtol` 报 `DISP_E_MEMBERNOTFOUND`，`IModelDocExtension.CreateGtolData` 成员不存在（SW2024），结论为"两条官方路径都不可用，C# 插件也绕不过"。

`DISP_E_MEMBERNOTFOUND` 是 **COM 分派层找不到成员**，常见原因是晚期绑定类型库不匹配、接口对象拿错、或前置选中状态缺失，**不等于"该 API 在此版本不存在"**。"C# 插件也绕不过"这一推论证据不足，建议在文档里降级为"当前 Python COM 路径未走通"。

### 8.2 早绑定五步诊断（成本极低，先做）

```python
import win32com.client
app = win32com.client.gencache.EnsureDispatch("SldWorks.Application")  # 早绑定，生成类型缓存
doc = app.ActiveDoc
print([m for m in dir(doc) if "gtol" in m.lower()])        # ① 真实暴露了哪些成员
print([m for m in dir(doc.Extension) if "gtol" in m.lower()])  # ② 接口归属是否在 Extension
```

1. 早绑定后枚举 `doc` 与 `doc.Extension` 上所有含 `gtol` 的成员，确认真实签名。
2. 确认调用对象是否正确（`IDrawingDoc` 与 `IModelDocExtension` 不是同一接口，晚期绑定下容易拿错）。
3. 补前置状态：先 `ActivateView` + `SelectByID(view_name, "DRAWINGVIEW", 0, 0, 0)` 选中目标视图，再调插入。
4. 按坑 C1，新建图纸先保存一次再插注解。
5. 用第 5 节的版本矩阵在 2024/2025/2026 各跑一遍，结论按版本记录（可能只某些版本可用）。

### 8.3 若仍不通：占位模板改值路线

"改"通常比"建"容易，且跨版本更稳：

1. 在 `.drwdot` 模板中预置一个占位 Gtol（或预置一个含 Gtol 的注释样例）。
2. 出图后选中该占位实体，编辑其公差类型、公差值、基准引用。
3. 回读实体属性确认改成功；改不了就明确报"仅供参考"，不写 Note 冒充。

另一条并行路径：若模型侧本就带形位公差注解，可尝试经 `InsertModelAnnotations3` 从模型导入图纸（与孔标注同一机制），由 SolidWorks 负责实体创建。

### 8.4 产品策略（重要：不要过度投入）

- 真实图纸基线：形位公差出现率 **7%**，粗糙度 **60%**。本项目已把 Gtol 降为可选、粗糙度升为常规，这个判断是对的，坚持。
- wzyn 同样只做"要求是否存在并可追溯"，明确**完整 GD&T 语义求解不在范围内**。我们也照此：不语义求解、不判定公差合理性，只核验"声明的 Gtol 是否有对应原生实体"。
- `delivery_check` 中：Gtol 缺失不 blocker；若调用方声明需要 Gtol 而图上无原生实体，则进 `pending` 并注明"AI 未写入，需人工补"，绝不以 Note 文字充当通过证据。

### 8.5 验收

- 五步诊断结论（每版本可用/不可用 + 证据）写入 `docs/com-quirks.md`。
- 若占位模板路线走通：新增 `add_geometric_tolerance`（pilot），只支持有限类型，回读校验。
- 若走不通：文档保留"不支持 + 替代操作建议"，`assert_capability()` 声明期阻断。

---

## 9. v0.5 专项方案三：出图前规划工作流

### 9.1 定位

现有闭环是 `台账 → 出图 → 标注 → 自检 → 判定`，缺"**出图之前的决策**"：用几张图、几个视图、什么比例、标哪些尺寸。这导致 `auto_dimension` 只能事后提示"超 30 个冗余"，而 README 的洞察"制图是筛选不是搬运"没落到代码。

wzyn 用 DrawingSpec 让**调用方**声明规格；我们更进一步：由 `build_feature_ledger` + 实测基线**自动产出**规格草案，调用方只需确认或微调。

### 9.2 新工具 `plan_drawing`

**输入**：`model_path`、可选 `document_type`（part/assembly/sheet_metal）、`paper_preference`、`target_dim_count`（默认按基线）、`required`（调用方必标清单）。

**输出**：`DrawingPlan` JSON，字段对齐第 4.1 节，便于日后兼容。

```json
{
  "sourceModel": "D:/parts/P05.SLDPRT",
  "documentType": "sheet_metal",
  "standard": "GB_T", "projection": "first_angle",
  "modelSizeMm": [420, 300, 180],
  "sheets": [
    { "paperSize": "A3", "role": "structure",
      "views": ["front", "right"], "sections": [{"label": "A", "parent": "front"}],
      "view_scales": {"right": 0.5} },
    { "paperSize": "A3", "role": "flat_pattern", "views": ["flat_pattern", "formed"] }
  ],
  "dimensionPlan": {
    "targetTotal": 14,
    "tiers": { "datum": 3, "mating": 4, "overall": 3, "hole": 4 },
    "items": [ {"id": "D1", "kind": "overall", "view": "front", "valueMm": 420} ]
  },
  "holeRequirements": [ {"id": "H1", "specification": "Ø5 thru", "count": 4, "view": "front"} ],
  "professionalAnnotations": {
    "centerMarks": [{"id": "CM1", "view": "front", "count": 4, "targets": ["holes"]}],
    "surfaceFinishSymbols": [{"id": "SF1", "view": "front", "text": "Ra3.2"}],
    "geometricTolerances": []
  },
  "capabilityPrecheck": [
    {"capability": "holeCallout", "level": "pilot", "route": "bitmask_import"},
    {"capability": "geometricTolerance", "level": "not_implemented",
     "action": "block", "fallback": "人工补标"}
  ],
  "risks": ["模型 Flat-Pattern1 被抑制，展开图不可用"]
}
```

### 9.3 规划规则（每条都要有依据）

| 规则 | 依据 |
|---|---|
| 图幅按模型包围盒 + 视图数 + 标注密度选，不默认 A3 | 实测 A3/1:2 带标注 2–3 视图零警告、4 视图互挤、5 视图不可用 |
| A3 视图数 > 3 时**直接拆图**，而不是缩小比例硬塞 | 配电箱实测：5 视图一张 = 4 越界 + 1 重叠；拆两张 = 双零警告 |
| 钣金件固定拆"结构图 + 展开图"两张 | 同上；且剖视图 x 与父视图锁定，只能上下排 |
| 展开图前先查 Flat-Pattern 是否被抑制 | 实测 blocker 案例 |
| 尺寸总量目标 **11–14**（可配），超过则按分层优先级裁剪 | 基线：平均 14.2、中位 11 |
| 尺寸分四层配额：`基准 / 配合 / 外形 / 孔位`，优先级从低到高裁剪 | 工程惯例 + 覆盖率判定需要 |
| 跨视图重复尺寸只保留信息量大的一处 | 现有"跨视图去重需 known_values"约束，规划期就先规避 |
| 粗糙度默认给常规值并标注"AI 拟定"；Gtol 默认空 | 基线 60% vs 7% |
| 视图无尺寸不报警，仅整图无尺寸才报警 | 基线误报从 79/100 降到 6 的那次修正 |
| 所有不支持能力在 plan 阶段就列出并给 fallback | 资产 B，声明期阻断 |

### 9.4 与现有工具的串联

```
build_feature_ledger(model)            ← 已有
   ↓
plan_drawing(model, ledger)            ← 新增：产出 DrawingPlan + 能力预检 + 拆图
   ↓ （人工或 Agent 确认/微调）
create_drawing(plan.sheets[i])         ← 已有，建议补 standard/projection 前置校验
   ↓
add_section_view / add_detail_view / add_flat_pattern_view
   ↓
auto_dimension(按 dimensionPlan.items 筛选，而非全量导入)   ← 需加 plan 入参
add_center_marks / add_hole_callout / add_surface_finish / add_dimension_tolerance
set_title_block
   ↓
export_drawing(PDF) → verify_drawing_rendering      ← 新增（资产 A）
   ↓
drawing_audit + verify_dimension_attachments + reconcile_ledger
   ↓
delivery_check
```

### 9.5 验收指标

- 同一批测试件，走 plan 流程后 `delivery_check` 一次通过率较当前提升（记录前后对比）。
- 出图尺寸数落入 11–20 区间的比例；超 30 个的情况应基本消失。
- `drawing_audit` 越界 + 重叠告警数下降；A3 超 3 视图的请求被规划期拆图拦截。
- plan 阶段能提前报出的能力阻断，不再出现在执行期长耗时之后。

---

## 10. 落地路线图

| 优先级 | 事项 | 依赖 | 验收 |
|---|---|---|---|
| **P0-1** | 孔标注改位掩码主路径 + 可见边辅路径 + 超时预算 | 6.3 / 6.11 / 坑 C2 | 7.6 指标 |
| **P0-2** | `verify_drawing_rendering`（PyMuPDF + 坐标变换 + 一一匹配 + 碰撞） | `.[pdf]` extra、尺寸 `position_m` 采集 | 3.13 验收 |
| **P0-3** | `capabilities.yaml` + `assert_capability()` 声明期阻断 | — | 不支持能力在改文档前返回结构化错误 |
| **P1-1** | `delivery_check` 接入 PDF 证据与阻断码 | P0-2 | 缺 PDF/缺依赖时正确降级 |
| **P1-2** | `plan_drawing` + `auto_dimension` 接 plan | `build_feature_ledger`、9.3 规则 | 9.5 指标 |
| **P1-3** | 版本矩阵 `--years` + 4 条坑入文档 | `tests/smoke.py` | 可复跑的版本报告 |
| **P1-4** | Gtol 早绑定五步诊断，按结论决定实现或阻断 | 8.2 | 8.5 |
| **P2-1** | `drawing_audit` 用真实渲染框重判（保留现有越界检查） | P0-2 | 误报/漏报对比记录 |
| **P2-2** | `Create1stAngleViews2` 布局对照实验 | 6.5 | 是否缓解 A3 互挤，结论入坑文档 |
| **P2-3** | 气泡 + 图内 BOM（仅当支持装配图交付） | 6.10 | 装配图 BOM/序号回读通过 |

**建议节奏**：P0-1 最快见效且能立刻写进 README（把"孔标注不可用"从边界清单里删掉），P0-2 是差异化护城河，两者先做。

---

## 11. 不借鉴清单

| 不借鉴 | 原因 |
|---|---|
| haunchen 的无鉴权 HTTP 服务、默认绑 `0.0.0.0:8080` | 能连端口即可驱动 SolidWorks 并读写文件，公开资料已被点名；本项目保持本地 stdio |
| haunchen 的 base64 图片回传 | MCP tool result 有 25K token 上限，图片按文字计会超限；它自己也改成了 SMB 路径 + 客户端 Read |
| wzyn 的 Execution Core / Backend Router / OCCT 无头后端 | 本项目只做 SolidWorks 原生出图，引入多后端会稀释投入 |
| wzyn 的 GUI（CAD Studio） | 与 MCP 定位无关 |
| GD&T 语义求解、公差合理性判定 | 两家都不做，且超出"图纸正确性"范畴，属于设计责任 |
| 仅按 PDF 文字框就宣称"可制造" | 几何相交 ≠ 视觉缺陷；且 wzyn 自己文档与代码口径不一致，采用保守口径 |
| wzyn 布局检查缺页边距/越界 | 我们已有，合并时保留，不要倒退 |

---

## 12. 附录

### 12.1 建议新增错误码（与现有风格统一）

```
DRAWING_CAPABILITY_UNSUPPORTED          声明期能力阻断（含 fallback 字段）
DRAWING_FINAL_PDF_REQUIRED              缺 PDF 或缺结构证据
DRAWING_PDF_TEXT_PARSER_MISSING         缺 PyMuPDF
DRAWING_PDF_INVALID / DRAWING_PDF_TEXT_PARSE_FAILED / DRAWING_PDF_VECTOR_TEXT_MISSING
DRAWING_PDF_DIMENSION_MATCH_INCOMPLETE  COM 尺寸与 PDF 文字框未一一对应
DRAWING_RENDERED_DIMENSION_TEXT_OVERLAP 尺寸文字框互撞
DRAWING_PDF_TEXT_OVERLAP_RISK           全页文字重叠风险
HOLE_CALLOUT_SCAN_BUDGET_EXCEEDED       孔标注扫边超预算（含替代建议）
```

### 12.2 常量速查

```
图幅 COM：A4=8 A3=9 A2=10 A1=11 A0=12
显示模式：wireframe=1 shaded=3 hidden_lines_removed=6
视图实体类型：swViewEntityType_Edge=1
孔标注位掩码：SW_INSERT_HOLE_CALLOUT=0x100000 (1048576)
尺寸导入位掩码：0x8 / 0x8000 / 0x80000 / 0x10000 / 0x20000
PDF 匹配容差：12 mm（图纸空间）；文字重叠面积阈值 0.5 Pt²；span 上限 20000
尺寸语义容差：valueMm 0.01；孔位置 0.1 mm；孔径 0.05 mm
边匹配：index 0.5 mm，proximity 2.0 mm；同半径去重 1e-4 m
布局碰撞 padding：0.001 m
```

### 12.3 参考链接

- wzyn 工程图子技能：`subskills/solidworks-engineering-drawing/`（`SKILL.md`、`README.md`、`schemas/drawing_spec.schema.json`、`scripts/drawing_review.py`、`references/drawing.md`）
- wzyn PDF/布局实现：根 `scripts/sw_review.py`（`inspect_pdf_text_layout`、`review_drawing_layout`）
- wzyn 版本矩阵：`tests/solidworks_drawing_version_matrix.py`
- haunchen：`src/tools/annotation.py`（选边、圆边参数、尺寸、气泡、位掩码）、`src/tools/drawing.py`（视图、剖视、局部、自定义方向）、`src/sw_connection.py`（STA worker）
