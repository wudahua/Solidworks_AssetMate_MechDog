# 交付清单

**结论：待设计审核**

| 项目 | 值 |
|---|---|
| 模型 | D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\logs\sample_probe\sample.SLDPRT |
| 图纸 | D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\logs\sample_probe\sample.SLDDRW |
| 生成时间 | 2026-11-16 16:39:20 |
| 工具版本 | 0.8.379 |
| 回执 ok | true |

## 阻断项

无（没有阻断交付的问题）

## 待确认（不阻断，但需人看一眼）

| 检查 | 说明 |
|---|---|
| dimension_attachments | 3/15 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认） |
| surface_finish_present | 0 处表面粗糙度 |
| ledger_reconciled | 台账覆盖率 0.4（12/30 个尺寸在图纸上找到，缺 18） |

## 检查项

| 检查 | 结果 | 说明 |
|---|---|---|
| model_readable | 通过 | 类别 一般零件 |
| model_clean | 通过 |  |
| views_present | 通过 | 3 个视图 / 1 页 |
| every_sheet_has_views | 通过 |  |
| no_view_outside_sheet | 通过 |  |
| no_view_overlap | 通过 |  |
| no_blank_view | 通过 |  |
| dimensions_present | 通过 | 15 个尺寸 |
| hole_callouts | 未检查 | 孔标注未核对：`include_faces=False`，台账没有圆柱面计数。要跑这项检查请传 `include_faces=True`（约 10 倍耗时）。 |
| tolerances_present | 通过 | 1 个尺寸带公差 |
| overall_dimension | 通过 | 图上最大尺寸 80.00 mm 对应模型最大外形 65.00 mm |
| cosmetic_threads | 通过 | 0 处螺纹注解 / 0 处螺纹文字 |
| dimension_attachments | 未通过 | 3/15 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认） |
| surface_finish_present | 未通过 | 0 处表面粗糙度 |
| gdt_present | 未通过 | 0 处形位公差（真实图纸仅 7% 有） |
| ledger_reconciled | 未通过 | 台账覆盖率 0.4（12/30 个尺寸在图纸上找到，缺 18） |

## 实测数据

| 指标 | 值 |
|---|---|
| views | 3 |
| dimensions | 15 |
| dimensions_with_tolerance | 1 |
| hole_callouts | 0 |
| geometric_tolerances | 0 |
| surface_finishes | 0 |
| cylinder_faces_in_model |  |
| cylinder_faces_status | not_checked |
| dimensions_unattached | 3 |
| features | 20 |
| model_doubts | [] |

## 尺寸覆盖（计划）

计划 16 条，落到图上 12 条（覆盖 75%）。

| 未落图的尺寸 | 数值 mm | 原因 | 补标路径 |
|---|---|---|---|
| 总长 | 55.0 | bbox_derived | 外形尺寸不是模型尺寸，导入永远拿不到；用 list_drawing_edges + add_distance_dimension 从两条边标注 |
| D2@草图6@sample.Part | 31.0 | not_inserted | 该驱动尺寸没有被导入（特征不在任何已激活视图里、被压缩，或属于孔向导草图）；补标用 add_drawing_dimension(view_name=...) |
| D1@切除-拉伸4@sample.Part | 30.0 | not_inserted | 该驱动尺寸没有被导入（特征不在任何已激活视图里、被压缩，或属于孔向导草图）；补标用 add_drawing_dimension(view_name=...) |
| D1@倒角2@sample.Part | 1.5 | not_inserted | 该驱动尺寸没有被导入（特征不在任何已激活视图里、被压缩，或属于孔向导草图）；补标用 add_drawing_dimension(view_name=...) |

## 页面

| 页 | 图幅 mm | 视图 | 尺寸 | 问题 |
|---|---|---|---|---|
| 图纸1 | 420×297 | 3 | 15 |  |

## 钣金工艺

无 —— 本次未要求（`include_sheet_metal=True` 才会跑，80 面零件约 150–300 s）

## 口径

本清单只复述 `delivery_check`（以及可选 `sheet_metal_qc`）的结论，不新增判断。`未检查` 表示该评价没有做，**不等于通过**。「待设计审核」表示图纸对象正确但含需人确认的值。

会话：读取模型与图纸时会**打开**这两个文档；返回前关闭本次打开的（2 个：sample.SLDDRW、sample.SLDPRT）；调用前就已打开的（0 个：无）不动。未保存、未修改任何文件。

verdict 依据流程 §7；blockers 非空即不可宣称可制造，ai_supplied 非空则只能算待设计审核。无关联实体的尺寸计入 **pending**（不是 blockers）——它表示「图纸还不能交付审核」，与「不可制造」是两个等级。当前：blockers 0 项、pending 3 项、ai_supplied 0 项。

## 原始回执

```json
{
 "dimension_coverage_status": "accepted",
 "generated_at": "2026-11-16 16:39:20",
 "tool_version": "0.8.379",
 "model": "D:\\WorkBuddy_space\\Solidworks_AssetMate_MechDog\\logs\\sample_probe\\sample.SLDPRT",
 "drawing": "D:\\WorkBuddy_space\\Solidworks_AssetMate_MechDog\\logs\\sample_probe\\sample.SLDDRW",
 "verdict": "待设计审核",
 "ok": true,
 "blockers": [],
 "pending": [
  {
   "check": "dimension_attachments",
   "detail": "3/15 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认）"
  },
  {
   "check": "surface_finish_present",
   "detail": "0 处表面粗糙度"
  },
  {
   "check": "ledger_reconciled",
   "detail": "台账覆盖率 0.4（12/30 个尺寸在图纸上找到，缺 18）"
  }
 ],
 "checks": {
  "model_readable": {
   "ok": true,
   "detail": "类别 一般零件"
  },
  "model_clean": {
   "ok": true,
   "detail": null
  },
  "views_present": {
   "ok": true,
   "detail": "3 个视图 / 1 页"
  },
  "every_sheet_has_views": {
   "ok": true,
   "detail": null,
   "sheets": [
    {
     "index": 1,
     "name": "图纸1",
     "size_m": [
      0.42,
      0.297
     ],
     "view_count": 3,
     "dimensions": 15,
     "issues": []
    }
   ]
  },
  "no_view_outside_sheet": {
   "ok": true,
   "detail": null
  },
  "no_view_overlap": {
   "ok": true,
   "detail": null
  },
  "no_blank_view": {
   "ok": true,
   "detail": null
  },
  "dimensions_present": {
   "ok": true,
   "detail": "15 个尺寸",
   "per_sheet": [
    {
     "sheet": "图纸1",
     "dimensions": 15
    }
   ]
  },
  "hole_callouts": {
   "ok": true,
   "detail": "孔标注未核对：`include_faces=False`，台账没有圆柱面计数。要跑这项检查请传 `include_faces=True`（约 10 倍耗时）。",
   "status": "not_checked",
   "hole_callout_objects": 0,
   "diameter_dimensions": null,
   "diameter_dimensions_status": "not_checked"
  },
  "tolerances_present": {
   "ok": true,
   "detail": "1 个尺寸带公差"
  },
  "overall_dimension": {
   "ok": true,
   "detail": "图上最大尺寸 80.00 mm 对应模型最大外形 65.00 mm",
   "model_max_mm": 65.0,
   "sheet_max_mm": 80.0
  },
  "cosmetic_threads": {
   "ok": true,
   "detail": "0 处螺纹注解 / 0 处螺纹文字"
  },
  "dimension_attachments": {
   "ok": false,
   "detail": "3/15 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认）",
   "excused_count": 0,
   "attached_count": 12,
   "near_view_orphans": 1,
   "far_from_view_orphans": 2
  },
  "surface_finish_present": {
   "ok": false,
   "detail": "0 处表面粗糙度"
  },
  "gdt_present": {
   "ok": false,
   "detail": "0 处形位公差（真实图纸仅 7% 有）",
   "required": false
  },
  "ledger_reconciled": {
   "ok": false,
   "detail": "台账覆盖率 0.4（12/30 个尺寸在图纸上找到，缺 18）",
   "status": "checked",
   "ledger_dimensions": 30
  }
 },
 "facts": {
  "views": 3,
  "dimensions": 15,
  "dimensions_with_tolerance": 1,
  "hole_callouts": 0,
  "geometric_tolerances": 0,
  "surface_finishes": 0,
  "cylinder_faces_in_model": null,
  "cylinder_faces_status": "not_checked",
  "dimensions_unattached": 3,
  "features": 20,
  "model_doubts": []
 },
 "sheets": [
  {
   "index": 1,
   "name": "图纸1",
   "size_m": [
    0.42,
    0.297
   ],
   "view_count": 3,
   "dimensions": 15,
   "issues": []
  }
 ],
 "note": "verdict 依据流程 §7；blockers 非空即不可宣称可制造，ai_supplied 非空则只能算待设计审核。无关联实体的尺寸计入 **pending**（不是 blockers）——它表示「图纸还不能交付审核」，与「不可制造」是两个等级。当前：blockers 0 项、pending 3 项、ai_supplied 0 项。",
 "sheet_metal": null,
 "dimension_coverage": {
  "planned": 16,
  "matched_by_name": 10,
  "matched_by_value": 2,
  "coverage_ratio": 0.75,
  "missing": [
   {
    "dimension": "总长",
    "value_mm": 55.0,
    "kind": "derived",
    "reason": "bbox_derived",
    "hint": "外形尺寸不是模型尺寸，导入永远拿不到；用 list_drawing_edges + add_distance_dimension 从两条边标注"
   },
   {
    "dimension": "D2@草图6@sample.Part",
    "value_mm": 31.0,
    "kind": "driving",
    "reason": "not_inserted",
    "hint": "该驱动尺寸没有被导入（特征不在任何已激活视图里、被压缩，或属于孔向导草图）；补标用 add_drawing_dimension(view_name=...)"
   },
   {
    "dimension": "D1@切除-拉伸4@sample.Part",
    "value_mm": 30.0,
    "kind": "driving",
    "reason": "not_inserted",
    "hint": "该驱动尺寸没有被导入（特征不在任何已激活视图里、被压缩，或属于孔向导草图）；补标用 add_drawing_dimension(view_name=...)"
   },
   {
    "dimension": "D1@倒角2@sample.Part",
    "value_mm": 1.5,
    "kind": "driving",
    "reason": "not_inserted",
    "hint": "该驱动尺寸没有被导入（特征不在任何已激活视图里、被压缩，或属于孔向导草图）；补标用 add_drawing_dimension(view_name=...)"
   }
  ]
 },
 "documents_closed": [
  "D:\\WorkBuddy_space\\Solidworks_AssetMate_MechDog\\logs\\sample_probe\\sample.SLDDRW",
  "D:\\WorkBuddy_space\\Solidworks_AssetMate_MechDog\\logs\\sample_probe\\sample.SLDPRT"
 ],
 "documents_closed_titles": [
  "sample - 图纸1"
 ],
 "documents_left_open": [],
 "documents_not_closed_newly_opened": [],
 "documents_close_status": "ok"
}
```
