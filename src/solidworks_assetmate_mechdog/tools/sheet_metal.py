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
    """Thin alias; the single implementation lives in sw_session.call_method."""
    from ..sw_session import call_method

    return call_method(obj, name, *args)


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
    # Match the Chinese label too. The ledger reports this dimension as `厚度@钣金1`, and
    # matching only "板厚" meant sheet_metal_qc answered "no Thickness parameter" for a part
    # whose thickness build_feature_ledger had just read as 3.0 — same tree, two answers.
    needles = ("thickness", "板厚", "厚度")
    for feature in ledger_result.get("features") or []:
        for dim in feature.get("dimensions") or []:
            label = str(dim.get("name") or "")
            low = label.lower()
            if any(n in low if n.isascii() else n in label for n in needles):
                value = dim.get("value_mm")
                if value:
                    return float(value), label
    # Nothing matched by name: fall back to the sheet-metal feature's own thickness value.
    for feature in ledger_result.get("features") or []:
        if "SheetMetal" in str(feature.get("type_name") or ""):
            value = feature.get("thickness_mm")
            if value:
                return float(value), "%s.thickness_mm" % (feature.get("name") or "SheetMetal")
    return None, "no Thickness parameter found in the feature tree"


def sheet_metal_qc(model_path, include_cylinders=False):
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

    # Cylinder inventory: fast (single GetFaces pass) and reliable. Radius groups are what
    # a fabricator needs to sanity-check the model ("12x Ø7 and 12x Ø12 — is that right?").
    # Bend/hole separation is NOT attempted here — see the note in not_checked below.
    cylinders = []
    cylinder_error = None
    if include_cylinders:
      try:
        import win32com.client as win

        from . import geometry as geo

        # `_call` is this module's local alias for sw_session.call_method; using the wrong
        # name raised NameError which the broad except then swallowed into "0 cylinders".
        bodies = list(_call(model, "GetBodies2", 0, True) or ())
        if bodies:
            body = win.Dispatch(bodies[0])
            cylinders = geo.extract_cylinders(body, _call, include_face=True)
      except Exception as exc:
        cylinder_error = str(exc)[:120]
    radius_groups = {}
    for c in cylinders:
        key = round(c["radius_mm"], 2)
        radius_groups[key] = radius_groups.get(key, 0) + 1

    # 0.8.340 (roadmap v0.9 step 1): classify every cylindrical face by its OWNER FEATURE.
    # This is the discriminator the radius heuristic could not replace: on the panel box a
    # Ø12/Ø14 concentric hole pair differs by exactly the thickness and was paired as a
    # "bend" (80 cylinders → 36 fake bends, 8 holes). `IFace2.GetFeature.GetTypeName2`
    # answers it directly — `OneBend` = bend fillet, `ICE`/`HoleWzd` = hole, `Fillet` =
    # ordinary round, `LPattern` = patterned source (resolved one level up).
    face_kinds = {"bend": [], "hole": [], "fillet": [], "pattern": [], "other": []}
    for c in cylinders:
        face_kinds.setdefault(c.get("kind") or "other", []).append(c)

    def _summ(rows, limit=12):
        seen, out = set(), []
        for r in rows:
            key = (r.get("owner_name"), round(r.get("radius_mm") or 0, 3))
            if key in seen:
                continue
            seen.add(key)
            out.append({"feature": r.get("owner_name"),
                        "feature_type": r.get("owner_type"),
                        "radius_mm": round(r.get("radius_mm") or 0, 3),
                        "diameter_mm": round((r.get("radius_mm") or 0) * 2, 3),
                        "axis": [round(float(v), 4) for v in (r.get("axis") or ())][:3],
                        "via_pattern": bool(r.get("via_pattern"))})
            if len(out) >= limit:
                break
        return out

    # Pattern-owned faces: `GetParents` returns TOPOLOGICAL parents (bends/flange/extrude),
    # not the pattern's seed, so the source cannot be read directly. Instead of guessing a
    # kind, list the base features that could be the seed — same radius, parallel axis —
    # and let the caller decide. `confidence` stays "low" by construction.
    def _axis_dot(a, b):
        try:
            return abs(sum(float(x) * float(y) for x, y in zip(a[:3], b[:3])))
        except Exception:
            return 0.0

    _base = [c for c in cylinders if (c.get("kind") or "") in ("bend", "hole", "fillet")]
    pattern_hints = []
    for c in face_kinds.get("pattern", []):
        cands = [b for b in _base
                 if abs((b.get("radius_mm") or 0) - (c.get("radius_mm") or 0)) <= 0.02
                 and _axis_dot(b.get("axis"), c.get("axis")) >= 0.999]
        pattern_hints.append({
            "feature": c.get("owner_name"),
            "feature_type": c.get("owner_type"),
            "radius_mm": round(c.get("radius_mm") or 0, 3),
            "candidate_source_features": sorted({b.get("owner_name") for b in cands}),
            "confidence": "low",
            "why": "阵列的 GetParents 给的是拓扑父级（折弯/法兰/拉伸），不是阵列源；"
                   "候选按「同半径 + 轴平行」列出，需人确认",
        })

    classification = {
        "basis": "IFace2.GetFeature → IFeature.GetTypeName2（不依赖半径）",
        "checked": bool(include_cylinders) and not cylinder_error,
        "counts": {k: len(v) for k, v in face_kinds.items()},
        "bends": _summ(face_kinds["bend"]),
        "holes": _summ(face_kinds["hole"]),
        "fillets": _summ(face_kinds["fillet"]),
        "unresolved": _summ(face_kinds["pattern"] + face_kinds["other"]),
        "pattern_hints": pattern_hints,
        "note": ("这解决的是「哪个圆柱面是孔、哪个是折弯圆角」；两段距离"
                 "（最小折弯边 ≥4t+R、孔到折弯线 ≥2t+R）仍未计算，"
                 "所以那两项继续保持 not_checked。"),
    } if include_cylinders else None

    # ── 0.8.341 (v0.9 step 2a): hole-to-bend clearance ──────────────────────────────────
    # Both sides are cylinders with an axis, so this is a pure 3D skew-line distance:
    #   edge_clearance = |axis_hole × axis_bend| distance − R_bend − R_hole
    # compared with the shop convention 2t + R. `min_flange_length` stays not_checked:
    # it needs the FLANGE outline (tangent line → outer edge), which is a different walk.
    from . import geometry as _geo

    bend_groups = {}
    for c in (face_kinds.get("bend") or []):
        key = c.get("owner_name") or "?"
        g = bend_groups.setdefault(key, {"axis": c.get("axis"), "point": c.get("point"),
                                         "radii": []})
        if c.get("radius_mm"):
            g["radii"].append(float(c["radius_mm"]))
    bends_geom = []
    for name, g in bend_groups.items():
        if not g["radii"]:
            continue
        bends_geom.append({"feature": name,
                           "r_inner_mm": round(min(g["radii"]), 3),
                           "r_outer_mm": round(max(g["radii"]), 3),
                           "point": g["point"], "axis": g["axis"]})

    # ── 0.8.342 (v0.9 step 2b): min flange length, 4t + R ──────────────────────────────
    # For each bend take the INNER face, walk its two tangent lines to the planar face on
    # the other side, and take the farthest straight edge in that plane. Approximate by
    # construction (`confidence: "approx"`): a notched flange reports its widest point.
    flange_rows = []
    if thickness:
        for b in bends_geom:
            face = next((c.get("_face") for c in (face_kinds.get("bend") or [])
                         if c.get("owner_name") == b["feature"]
                         and abs((c.get("radius_mm") or 0) - b["r_inner_mm"]) <= 0.01), None)
            if face is None:
                continue
            import win32com.client as _win

            info = _geo.bend_flange_lengths(face, _win.Dispatch)
            sides = [s["length_mm"] for s in (info.get("sides") or [])]
            if not sides:
                flange_rows.append({"bend_feature": b["feature"],
                                    "r_inner_mm": b["r_inner_mm"],
                                    "sides_mm": [], "min_side_mm": None,
                                    "threshold_mm": round(4.0 * float(thickness)
                                                          + b["r_inner_mm"], 2),
                                    "status": "unknown",
                                    "reason": info.get("reason")})
                continue
            shortest = min(sides)
            threshold = 4.0 * float(thickness) + b["r_inner_mm"]
            # The measurement is an upper bound taken from face edges (see
            # `bend_flange_lengths`), so a millimetre-level difference is noise. Treat
            # |margin| <= tol as `borderline` instead of calling a 0.08 mm gap "too short".
            tol = 0.2
            margin = shortest - threshold
            status = ("pass" if margin > tol
                      else "borderline" if margin >= -tol else "too_short")
            flange_rows.append({
                "bend_feature": b["feature"], "r_inner_mm": b["r_inner_mm"],
                "sides_mm": sides, "min_side_mm": round(shortest, 2),
                "threshold_mm": round(threshold, 2),
                "margin_mm": round(margin, 2),
                "tolerance_mm": tol,
                "rule": "min_side >= 4t + R（厂内口径）",
                "confidence": info.get("confidence"),
                "status": status,
            })
    _too_short = [r for r in flange_rows if r["status"] == "too_short"]
    _borderline = [r for r in flange_rows if r["status"] == "borderline"]
    _flange_unknown = [r for r in flange_rows if r["status"] == "unknown"]

    hole_to_bend = []
    if thickness and bends_geom:
        for h in (face_kinds.get("hole") or []):
            best = None
            for b in bends_geom:
                d_mm, how = _geo.skew_line_distance(h.get("point"), h.get("axis"),
                                                    b["point"], b["axis"])
                if d_mm is None:
                    continue
                d_mm *= 1000.0
                if best is None or d_mm < best["axis_distance_mm"]:
                    best = {"axis_distance_mm": round(d_mm, 2), "how": how, "bend": b}
            if best is None:
                continue
            b = best["bend"]
            clearance = best["axis_distance_mm"] - b["r_inner_mm"] - float(h["radius_mm"])
            threshold = 2.0 * float(thickness) + b["r_inner_mm"]
            hole_to_bend.append({
                "hole_feature": h.get("owner_name"),
                "hole_diameter_mm": round(float(h["radius_mm"]) * 2.0, 2),
                "bend_feature": b["feature"],
                "bend_r_inner_mm": b["r_inner_mm"],
                "axis_distance_mm": best["axis_distance_mm"],
                "edge_clearance_mm": round(clearance, 2),
                "threshold_mm": round(threshold, 2),
                "rule": "edge_clearance >= 2t + R（厂内口径，可按需覆盖）",
                "status": "pass" if clearance >= threshold else "too_close",
            })
    # A through hole owns TWO cylindrical faces (one per wall), so the same hole would be
    # listed twice with identical numbers. Collapse by (hole, diameter, bend) keeping the
    # tighter clearance, and keep the face count so the dedupe is visible rather than silent.
    _collapsed = {}
    for r in hole_to_bend:
        key = (r["hole_feature"], r["hole_diameter_mm"], r["bend_feature"])
        prev = _collapsed.get(key)
        if prev is None or r["edge_clearance_mm"] < prev["edge_clearance_mm"]:
            r["faces"] = (prev or {}).get("faces", 1) + 1 if prev else 1
            _collapsed[key] = r
        else:
            prev["faces"] = prev.get("faces", 1) + 1
    hole_to_bend = list(_collapsed.values())
    _too_close = [r for r in hole_to_bend if r["status"] == "too_close"]

    checks = []

    def add(item, status, detail, blocking=False):
        checks.append({"check": item, "status": status, "detail": detail,
                       "blocking": blocking})

    add("is_sheet_metal", "pass", "SheetMetal 特征存在，%d 道弯折" % len(bends))
    if not thickness:
        add("min_flange_length", "not_checked", "未读到板厚，无法套用 4t + R 口径")
    elif not flange_rows:
        add("min_flange_length", "not_checked", "没有可测的折弯面")
    elif _too_short:
        add("min_flange_length", "warn",
            "%d 道折弯的短边小于 4t+R：%s" % (
                len(_too_short),
                "；".join("%s 最短 %.2f < %.2f mm" % (
                    r["bend_feature"], r["min_side_mm"], r["threshold_mm"])
                    for r in _too_short[:3])))
    elif _borderline:
        add("min_flange_length", "warn",
            "%d 道折弯的短边贴着 4t+R 临界（测量为近似值，±0.2 mm）：%s" % (
                len(_borderline),
                "；".join("%s 最短 %.2f vs %.2f mm（差 %.2f）" % (
                    r["bend_feature"], r["min_side_mm"], r["threshold_mm"],
                    r["margin_mm"]) for r in _borderline[:3])))
    elif _flange_unknown:
        add("min_flange_length", "not_checked",
            "%d 道折弯的相邻平面读不到边（%s）" % (
                len(_flange_unknown),
                (_flange_unknown[0].get("reason") or "")[:60]))
    else:
        add("min_flange_length", "pass",
            "%d 道折弯的短边均 ≥ 4t+R（最短 %.2f mm，口径 4t+R，近似测量）" % (
                len(flange_rows), min(r["min_side_mm"] for r in flange_rows)))

    # hole-to-bend is measured now (v0.9 step 2a). Three states stay distinguishable:
    # not measurable (no thickness / no bends / no holes), measured-and-clear, measured
    # and too close. "to close" is `warn`, not `fail`: the 2t+R rule is shop convention,
    # not a GB clause, so it must not block a drawing on its own.
    if not thickness:
        add("hole_to_bend_distance", "not_checked", "未读到板厚，无法套用 2t + R 口径")
    elif not bends_geom:
        add("hole_to_bend_distance", "not_checked", "没有可识别的折弯面（OneBend）")
    elif not hole_to_bend:
        add("hole_to_bend_distance", "not_checked", "没有可识别的孔面或轴距不可算")
    elif _too_close:
        add("hole_to_bend_distance", "warn",
            "%d 处孔离折弯线过近（口径 2t+R）：%s" % (
                len(_too_close),
                "；".join("%s→%s 余量 %.2f < %.2f mm" % (
                    r["hole_feature"], r["bend_feature"],
                    r["edge_clearance_mm"], r["threshold_mm"]) for r in _too_close[:3])))
    else:
        add("hole_to_bend_distance", "pass",
            "%d 个孔到折弯线余量均 ≥ 2t+R（最小 %.2f mm）" % (
                len(hole_to_bend),
                min(r["edge_clearance_mm"] for r in hole_to_bend)))
    if include_cylinders:
        if cylinder_error:
            add("face_classification", "not_checked",
                "圆柱面读取失败，无法分类：%s" % cylinder_error)
        else:
            _k = classification["counts"]
            add("face_classification", "pass",
                "圆柱面按所属特征分类：折弯 %d / 孔 %d / 普通圆角 %d / 未解析 %d"
                "（依据 %s）" % (_k["bend"], _k["hole"], _k["fillet"],
                                _k["pattern"] + _k["other"], classification["basis"]))
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
        # Three states must be distinguishable: not requested / requested but failed /
        # requested and empty. Returning null for all of them left the reader guessing.
        cylinders_not_requested=(not include_cylinders),
        cylinder_count=len(cylinders) if include_cylinders else None,
        cylinder_error=cylinder_error,
        cylinder_classification=classification,
        # Per-hole numbers, not just a verdict: a shop can re-check the arithmetic and
        # override the 2t+R convention without re-deriving anything.
        hole_to_bend=hole_to_bend,
        min_flange=flange_rows,
        cylinder_radius_groups_mm=sorted(
            ({"radius_mm": r, "diameter_mm": round(r * 2, 2), "count": n}
             for r, n in radius_groups.items()),
            key=lambda x: x["radius_mm"]) if include_cylinders else None,
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
