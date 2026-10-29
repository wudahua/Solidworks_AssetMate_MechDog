"""最简可复现：用纯 pywin32 创建原生 BOM 表（已验证能成功创建）。

结果：rows=1, cols=3, BomFeature 可取 —— 有表头和表格对象，但没有数据行与数量列。

运行前改两处路径：
    ASSEMBLY —— 装配体路径（测试用 SAB1_0000_000.SLDASM）
    OUT_DRW  —— 生成的图纸路径

依赖：pywin32。不依赖项目包，可直接 python 运行。
"""
import os
import sys
import tempfile

import pythoncom
import win32com.client as win


# ==== 改这里 ====
ASSEMBLY = r"D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\测试用例\工程图测试用例\条码扫描设备\SAB1_0000_000.SLDASM"
OUT_DRW = os.path.join(tempfile.mkdtemp(prefix="bom_"), "BOM_TEST.SLDDRW")
TEMPLATE_NAME = "bom-standard.sldbomtbt"     # 或 bom-all / bom-material
# ================


def call_method(obj, name, *args):
    """SolidWorks 有些方法被 pywin32 当成属性派发，会报 DISP_E_MEMBERNOTFOUND。
    用 DISPATCH_METHOD 才能真正调到。FirstFeature / GetAnnotation / InsertGtol 都是这类。"""
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    flag = getattr(obj, "_FlagAsMethod", None)
    if flag is not None:
        try:
            flag(name)
        except Exception:
            pass
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


def main():
    pythoncom.CoInitialize()
    sw = win.Dispatch("SldWorks.Application")
    sw.Visible = True

    base = str(sw.GetExecutablePath)          # 注意：这是属性，不是方法
    template = os.path.join(base, "lang", "chinese-simplified", TEMPLATE_NAME)
    if not os.path.exists(template):
        print("模板不存在:", template)
        return 1

    # 1) 打开装配（必须，否则 BOM 没有数据源）
    print("打开装配:", ASSEMBLY)
    assy = sw.OpenDoc6(ASSEMBLY, 2, 1, "", 0, 0)
    if assy is None:
        print("装配打开失败")
        return 1

    # 2) 从装配建工程图
    print("建工程图:", OUT_DRW)
    drw = sw.NewDocument(
        os.path.join(base, "lang", "chinese-simplified", "Tutorial", "gb_a3.drwdot"),
        0, 0, 0)
    if drw is None:
        # 模板路径可能不同，退化为用模板参数直接建
        drw = sw.NewDocument("", 0, 0, 0)
    sw.ActivateDoc3(os.path.basename(OUT_DRW), False, 0, 0) if False else None

    # 3) 放一个前视图
    view = drw.CreateDrawViewFromModelView3(ASSEMBLY, "*Front", 0.15, 0.15, 0)
    if view is None:
        view = drw.CreateDrawViewFromModelView3(ASSEMBLY, "前视", 0.15, 0.15, 0)
    print("视图:", getattr(view, "Name", None))
    drw.EditRebuild3
    drw.GraphicsRedraw2

    # 4) 关键：插入 BOM。AnchorType 必须 1、BomType 必须 2
    drw.ActivateView(view.Name)
    drw.ClearSelection2(True)
    table = view.InsertBomTable4(
        False,          # UseAnchorPoint
        0.24, 0.19,     # X, Y（米）
        1,              # AnchorType = TopLeft       ← 传 0 会直接返回 None
        2,              # BomType = TopLevelOnly     ← 传 1 是 PartsOnly
        "",             # Configuration（TopLevelOnly 空串）
        template,       # TableTemplate
        False,          # Hidden
        0,              # IndentedNumberingType
        False,          # DetailedCutList
    )
    print("InsertBomTable4 ->", "None" if table is None else type(table).__name__)
    if table is None:
        return 1

    # 5) 重建（EditRebuild3 是属性，不能加括号；用 call_method 才会真正执行）
    try:
        call_method(drw, "ForceRebuild3", False)
    except Exception:
        pass
    drw.GraphicsRedraw2

    tb = win.Dispatch(table)
    print()
    print("=== 结果 ===")
    print("RowCount        :", tb.RowCount)
    print("ColumnCount     :", tb.ColumnCount)
    print("TotalColumnCount:", tb.TotalColumnCount, "   <- 注意这里是 5，而 ColumnCount 是 3")
    for i in range(int(tb.ColumnCount)):
        try:
            title = str(tb.GetColumnTitle2(i, True)).replace("<FONT size=3.5>", "")
        except Exception as exc:
            title = "ERR:" + str(exc)[:30]
        print("  col%d: %s" % (i, title))
    bf = tb.BomFeature
    for attr in ("TableType", "GetReferencedModelName", "KeepMissingItems"):
        try:
            print("%-22s %s" % (attr, getattr(bf, attr)))
        except Exception as exc:
            print("%-22s ERR %s" % (attr, str(exc)[:40]))

    # 6) 存盘
    drw.SaveAs3(OUT_DRW, 0, 0)
    print()
    print("图纸已存:", OUT_DRW)
    print()
    print("现状：有表头和表格对象，但 RowCount=1、ColumnCount=3（缺数量列、缺数据行）。")
    print("对照：用宏插入的那张是 RowCount=10、ColumnCount=4。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
