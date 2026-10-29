import os
import pythoncom
import win32com.client as win

ROOT = r"D:/WorkBuddy_space/Solidworks_AssetMate_MechDog/项目规划 - AssetMate_MechDog/_bom_solution_work"
DRAWING = r"D:\WorkBuddy_space\_assetmate_test_run\bom_fix\BOM_TEST.SLDDRW"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


def show(tb, idx):
    print("TABLE", idx, "rows", tb.RowCount, "cols", tb.ColumnCount, "total", tb.TotalColumnCount)
    for i in range(int(tb.TotalColumnCount)):
        print(" COL", i, "title", tb.GetColumnTitle2(i, True))
        for name in ("GetColumnType", "GetColumnType2", "GetColumnType3"):
            try:
                if name == "GetColumnType":
                    result = tb.GetColumnType(i)
                elif name == "GetColumnType2":
                    result = call_method(tb, name, i, True)
                else:
                    result = call_method(tb, name, i, True, None, None)
                print("   ", name, "=>", result)
            except Exception as exc:
                print("   ", name, "ERR", repr(exc))
    try:
        bf = tb.BomFeature
        for attr in ("TableType", "Configuration", "GetReferencedModelName", "GetConfigurationCount", "KeepMissingItems"):
            try:
                print(" BF", attr, getattr(bf, attr))
            except Exception as exc:
                print(" BF", attr, "ERR", repr(exc))
        try:
            print(" BF GetConfigurations:", bf.GetConfigurations(False, None))
        except Exception as exc:
            print(" BF GetConfigurations ERR", repr(exc))
    except Exception as exc:
        print("BF ERR", repr(exc))
    for r in range(min(int(tb.RowCount), 3)):
        vals = []
        for c in range(int(tb.ColumnCount)):
            try:
                vals.append(str(tb.Text(r, c)))
            except Exception as exc:
                vals.append("ERR")
        print(" ROW", r, vals)


def main():
    pythoncom.CoInitialize()
    sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
    docs = list(sw.GetDocuments or ())
    doc = next((d for d in docs if str(d.GetTitle).startswith("BOM_TEST")), None)
    if doc is None:
        errors = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        doc = sw.OpenDoc6(DRAWING, 3, 1, "", errors, warnings)
    print("doc", doc.GetTitle if doc else None)
    sheet = doc.GetFirstView
    tables = list(sheet.GetTableAnnotations or ())
    print("tables", len(tables))
    for i, raw in enumerate(tables):
        show(win.Dispatch(raw), i)


if __name__ == "__main__":
    main()
