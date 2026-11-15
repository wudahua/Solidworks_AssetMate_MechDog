# 交付清单

**结论：待设计审核**

| 项目 | 值 |
|---|---|
| 模型 | D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\logs\callstatus_probe\csdoor.SLDPRT |
| 图纸 | D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\logs\callstatus_probe\csdoor.SLDDRW |
| 生成时间 | 2026-11-15 18:10:51 |
| 工具版本 | 0.8.342 |
| 回执 ok | true |

## 阻断项

无（没有阻断交付的问题）

## 待确认（不阻断，但需人看一眼）

| 检查 | 说明 |
|---|---|
| dimension_attachments | 3/21 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认） |
| ledger_reconciled | 台账覆盖率 0.433（13/30 个尺寸在图纸上找到，缺 17） |

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
| dimensions_present | 通过 | 21 个尺寸 |
| hole_callouts | 未检查 | 孔标注未核对：`include_faces=False`，台账没有圆柱面计数。要跑这项检查请传 `include_faces=True`（约 10 倍耗时）。 |
| tolerances_present | 通过 | 1 个尺寸带公差 |
| overall_dimension | 通过 | 图上最大尺寸 80.00 mm 对应模型最大外形 65.00 mm |
| cosmetic_threads | 通过 | 0 处螺纹注解 / 0 处螺纹文字 |
| dimension_attachments | 未通过 | 3/21 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认） |
| surface_finish_present | 通过 | 1 处表面粗糙度 |
| gdt_present | 未通过 | 0 处形位公差（真实图纸仅 7% 有） |
| ledger_reconciled | 未通过 | 台账覆盖率 0.433（13/30 个尺寸在图纸上找到，缺 17） |

## 实测数据

| 指标 | 值 |
|---|---|
| views | 3 |
| dimensions | 21 |
| dimensions_with_tolerance | 1 |
| hole_callouts | 4 |
| geometric_tolerances | 0 |
| surface_finishes | 1 |
| cylinder_faces_in_model |  |
| cylinder_faces_status | not_checked |
| dimensions_unattached | 3 |
| features | 20 |
| model_doubts | [] |

## 页面

| 页 | 图幅 mm | 视图 | 尺寸 | 问题 |
|---|---|---|---|---|
| 图纸1 | 420×297 | 3 | 21 |  |

## 口径

本清单只复述 `delivery_check`（以及可选 `sheet_metal_qc`）的结论，不新增判断。`未检查` 表示该评价没有做，**不等于通过**。「待设计审核」表示图纸对象正确但含需人确认的值。

verdict 依据流程 §7；blockers 非空即不可宣称可制造，ai_supplied 非空则只能算待设计审核。无关联实体的尺寸计入 **pending**（不是 blockers）——它表示「图纸还不能交付审核」，与「不可制造」是两个等级。当前：blockers 0 项、pending 2 项、ai_supplied 0 项。

## 原始回执

```json
{
 "generated_at": "2026-11-15 18:10:51",
 "tool_version": "0.8.342",
 "model": "D:\\WorkBuddy_space\\Solidworks_AssetMate_MechDog\\logs\\callstatus_probe\\csdoor.SLDPRT",
 "drawing": "D:\\WorkBuddy_space\\Solidworks_AssetMate_MechDog\\logs\\callstatus_probe\\csdoor.SLDDRW",
 "verdict": "待设计审核",
 "ok": true,
 "blockers": [],
 "pending": [
  {
   "check": "dimension_attachments",
   "detail": "3/21 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认）"
  },
  {
   "check": "ledger_reconciled",
   "detail": "台账覆盖率 0.433（13/30 个尺寸在图纸上找到，缺 17）"
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
     "dimensions": 21,
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
   "detail": "21 个尺寸",
   "per_sheet": [
    {
     "sheet": "图纸1",
     "dimensions": 21
    }
   ]
  },
  "hole_callouts": {
   "ok": true,
   "detail": "孔标注未核对：`include_faces=False`，台账没有圆柱面计数。要跑这项检查请传 `include_faces=True`（约 10 倍耗时）。",
   "status": "not_checked",
   "hole_callout_objects": 4,
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
   "detail": "3/21 个尺寸无关联实体（其中 1 条位置在视图附近、引线未验证 —— 若 PDF 上引线正常，这些多为 place_outside 移动过的尺寸，不必删；远离视图的才需人工确认）",
   "excused_count": 0,
   "attached_count": 18,
   "near_view_orphans": 1,
   "far_from_view_orphans": 2
  },
  "surface_finish_present": {
   "ok": true,
   "detail": "1 处表面粗糙度"
  },
  "gdt_present": {
   "ok": false,
   "detail": "0 处形位公差（真实图纸仅 7% 有）",
   "required": false
  },
  "ledger_reconciled": {
   "ok": false,
   "detail": "台账覆盖率 0.433（13/30 个尺寸在图纸上找到，缺 17）",
   "status": "checked",
   "ledger_dimensions": 30
  }
 },
 "facts": {
  "views": 3,
  "dimensions": 21,
  "dimensions_with_tolerance": 1,
  "hole_callouts": 4,
  "geometric_tolerances": 0,
  "surface_finishes": 1,
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
   "dimensions": 21,
   "issues": []
  }
 ],
 "note": "verdict 依据流程 §7；blockers 非空即不可宣称可制造，ai_supplied 非空则只能算待设计审核。无关联实体的尺寸计入 **pending**（不是 blockers）——它表示「图纸还不能交付审核」，与「不可制造」是两个等级。当前：blockers 0 项、pending 2 项、ai_supplied 0 项。",
 "sheet_metal": null
}
```
