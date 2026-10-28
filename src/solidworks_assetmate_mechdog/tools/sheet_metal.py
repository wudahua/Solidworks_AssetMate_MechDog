"""Sheet-metal checks and the flat-pattern boundary statement.

Two jobs, both about honesty rather than new geometry:

1. `sheet_metal_qc` — checks that can be answered from the feature tree (thickness,
   bend count, bend relief presence). Checks that need solid-geometry distance
   computation are reported as `not_checked`, never silently passed.

2. `flat_pattern_report` — states WHERE the blank size came from. AssetMate does not
   compute the flat pattern: SolidWorks' SheetMetal engine does, from the model's bend
   radius and K-factor. A caller who reads "blank 387.17 x 434.17" and assumes the tool
   calculated and verified it is making a decision on a false premise — that is the exact
   failure mode reported on 2026-10-28, where an AI-written drawing note claimed
   "K=0.44" while the model used SolidWorks' default.

What this module refuses to do:
  * invent a K-factor, bend deduction, or blank size
  * claim a relief exists when no relief feature is found
  * report a check as passed when it was not evaluated
"""

from __future__ import annotations

import os

from ..sw_session import error, ok, open_document

# Feature types that constitute a bend.
BEND_TYPES = ("EdgeFlange", "SketchBend", "MiterFlange", "Hem", "Jog", "ProcessBends")

# Feature types / name fragments that indicate a bend relief was modelled.
RELIEF_HINTS = ("Relief", "止裂", "BendRelief", "CornerRelief")


def _call(obj, name, *args):
    """Invoke a method even when pywin32 decides the name is a property.

    FirstFeature / GetNextFeature / GetDefinition are all dispatched as PROPERTYGET by
    win32com and raise DISP_E_MEMBERNOTFOUND; calling the DISPID as DISPATCH_METHOD works.
    """
    import pythoncom

    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    try:
        obj._FlagAsMethod(name)
    except Exception:
        pass
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


def _walk_features(model, limit=400):
    import win32com.client as win

    rows = []
    try:
        feature = win.Dispatch(_call(model, "FirstFeature"))
    except Exception:
        return rows
    seen = 0
    while feature is not None and seen < limit:
        try:
            rows.append({"name": str(feature.Name), "type": str(feature.GetTypeName2)})
        except Exception:
            break
        try:
            feature = win.Dispatch(_call(feature, "GetNextFeature"))
        except Exception:
            break
        seen += 1
    return rows


def _thickness_from_ledger(ledger_result):
    """Board thickness as a named parameter, if the feature tree exposes one.

    Prefer the explicit `Thickness@...` dimension; fall back to any dimension carrying
    'thickness' in its name. Returns (value_mm, source) or (None, reason).
    """
    for feature in ledger_result.get("features") or []:
        for dim in feature.get("dimensions") or []:
            label = str(dim.get("name") or "")
            if "thickness" in label.lower() or "板厚" in label:
                value = dim.get("value_mm")
                if value:
                    return float(value), label
    return None, "no Thickness parameter found in the feature tree"


def sheet_metal_qc(model_path):
    """Sheet-metal manufacturability checks that the feature tree can actually answer.

    Returns per-check status: "pass" | "fail" | "warn" | "not_checked". `not_checked`
    entries carry the reason — a green tick on an evaluation that never ran is worse than
    admitting the gap.
    """
    from . import ledger as ledger_tools

    path = os.path.abspath(model_path)
    if not os.path.exists(path):
        return error("bad_input", "model not found: %s" % path)
    if not path.lower().endswith(".sldprt"):
        return error("bad_input", "sheet-metal QC needs a .SLDPRT",
                     "Assemblies have no thickness; run this per part.")

    try:
        model = open_document(path)
        features = _walk_features(model)
    except Exception as exc:
        return error("sheet_metal_qc_failed", str(exc))

    types = [f["type"] for f in features]
    names = [f["name"] for f in features]
    is_sheet_metal = "SheetMetal" in types or any(t in types for t in BEND_TYPES)
    if not is_sheet_metal:
        return error("not_sheet_metal", "%s has no SheetMetal feature" % os.path.basename(path),
                     "Checks for bend relief / bend edges only apply to sheet-metal parts.")

    bends = [f for f in features if f["type"] in BEND_TYPES]
    reliefs = [n for n in names if any(hint.lower() in n.lower() for hint in RELIEF_HINTS)]

    led = ledger_tools.build_feature_ledger(path, include_faces=False)
    thickness, thickness_source = (None, "ledger failed")
    if led.get("ok"):
        thickness, thickness_source = _thickness_from_ledger(led)

    checks = []

    def add(item, status, detail, blocking=False):
        checks.append({"check": item, "status": status, "detail": detail,
                       "blocking": blocking})

    add("is_sheet_metal", "pass", "SheetMetal 特征存在，%d 道弯折" % len(bends))
    if thickness:
        add("thickness", "pass", "板厚 %.3f mm（来源：%s）" % (thickness, thickness_source))
    else:
        add("thickness", "warn", "未读到板厚参数：%s" % thickness_source)

    if bends and not reliefs:
        add("bend_relief", "fail",
            "有 %d 道弯折但未找到止裂槽特征。折弯交角处建议 R≥t、槽宽≥1.2t、槽长≥R+2t" % len(bends),
            blocking=True)
    elif reliefs:
        add("bend_relief", "pass", "找到止裂槽特征：%s" % ", ".join(reliefs[:3]))
    else:
        add("bend_relief", "not_checked", "无弯折特征，不适用")

    # These need solid geometry distance maths that this build does not do yet. Say so
    # instead of returning a green tick.
    for item, why in (
        ("min_flange_length", "需测量折弯边实际长度（4t + R），当前未实现几何测量"),
        ("hole_to_bend_distance", "需计算孔心到折弯线的三维距离（≥2t + R），当前未实现"),
        ("bend_radius_vs_thickness", "需读折弯圆角半径并与 0.5t 比对；Feature 定义接口在本版本返回空"),
        ("thickness_consistency", "需逐特征比对板厚，当前只读到全局板厚"),
        ("k_factor_source", "见 flat_pattern_report：K 因子由 SolidWorks 决定，工具不读取也不计算"),
    ):
        add(item, "not_checked", why)

    failed = [c for c in checks if c["status"] == "fail" and c["blocking"]]
    not_checked = [c["check"] for c in checks if c["status"] == "not_checked"]
    return ok(
        model=os.path.basename(path),
        is_sheet_metal=True,
        thickness_mm=thickness,
        thickness_source=thickness_source,
        bends=[{"name": b["name"], "type": b["type"]} for b in bends],
        bend_count=len(bends),
        relief_features=reliefs,
        checks=checks,
        blockers=failed,
        not_checked=not_checked,
        verdict=("需回改" if failed else ("部分未检查" if not_checked else "通过")),
        note=("not_checked 的项没有评价，不代表通过。"
              "止裂槽判定基于特征名/类型，未做几何确认；如模型用切除代替，会漏判。"),
    )


def flat_pattern_report(model_path):
    """Where the flat-pattern size comes from, and what this toolkit did NOT do.

    AssetMate projects SolidWorks' already-computed flat state into a view. It does not
    solve the unfold, does not read the K-factor or bend table, and cannot verify the
    blank. Any drawing note quoting a K-factor or bend deduction must come from the shop,
    never from this tool.
    """
    from . import ledger as ledger_tools

    path = os.path.abspath(model_path)
    if not os.path.exists(path):
        return error("bad_input", "model not found: %s" % path)
    if not path.lower().endswith(".sldprt"):
        return error("bad_input", "flat pattern needs a sheet-metal .SLDPRT",
                     "Assemblies cannot be unfolded.")

    try:
        model = open_document(path)
        features = _walk_features(model)
    except Exception as exc:
        return error("flat_pattern_report_failed", str(exc))

    types = [f["type"] for f in features]
    if "SheetMetal" not in types and not any(t in types for t in BEND_TYPES):
        return error("not_sheet_metal", "%s has no SheetMetal feature" % os.path.basename(path))

    bends = [f for f in features if f["type"] in BEND_TYPES]
    has_flat_feature = any(t in ("FlatPattern", "SheetMetal") for t in types)

    led = ledger_tools.build_feature_ledger(path, include_faces=False)
    thickness, thickness_source = (None, "ledger failed")
    if led.get("ok"):
        thickness, thickness_source = _thickness_from_ledger(led)

    return ok(
        model=os.path.basename(path),
        solver="solidworks_sheet_metal_engine",
        computed_by_tool=False,
        k_factor=None,
        k_factor_source="not_read",
        bend_table=None,
        bend_table_source="not_read",
        thickness_mm=thickness,
        thickness_source=thickness_source,
        bend_count=len(bends),
        bends=[{"name": b["name"], "type": b["type"]} for b in bends],
        flat_feature_present=has_flat_feature,
        statement=(
            "展开尺寸由 SolidWorks 钣金引擎按其内部折弯参数计算，AssetMate 只把展平状态"
            "投影成视图，未参与计算、未读取 K 因子或折弯表、未校验毛坯尺寸。"
        ),
        must_not_write=[
            "不得在图纸技术要求里写 K 因子、折弯扣除（BD）等工艺参数——工具不掌握这些值",
            "不得把展开毛坯尺寸当作「已校验」；应注明由厂内折弯表复核、首件试折修正",
        ],
        expected_note=(
            "展开尺寸按 SolidWorks 展平结果，工艺参数（K 因子/折弯扣除）以厂内折弯表为准，"
            "首件试折修正。"
        ),
        not_implemented=[
            "读取 K 因子与折弯表（ISheetMetalFeatureData.GetCustomBendAllowance 在本版本返回空）",
            "按道次输出角度/半径/BD 的折弯表",
            "校验毛坯尺寸与模型展开是否一致",
        ],
        verdict="展开计算不在本工具职责内，图纸注记请使用 expected_note 的措辞",
    )
