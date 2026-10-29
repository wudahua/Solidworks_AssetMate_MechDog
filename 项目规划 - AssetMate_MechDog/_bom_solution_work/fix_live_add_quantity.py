import os
import pythoncom
import win32com.client as win

OUT = r"D:\WorkBuddy_space\_assetmate_test_run\bom_fix\u_fixed.SLDDRW"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


def dump(tb, label):
    print(label, "rows", tb.RowCount, "cols", tb.ColumnCount, "total", tb.TotalColumnCount)
    for i in range(int(tb.ColumnCount)):
        print(" ", i, tb.GetColumnTitle2(i, True), "type", tb.GetColumnType(i))
    for r in range(min(int(tb.RowCount), 3)):
        print("  row", r, [tb.Text(r, c) for c in range(int(tb.ColumnCount))])


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
doc = next(d for d in list(sw.GetDocuments or ()) if str(d.GetTitle).startswith("u - "))
print("before saveas", doc.GetPathName)
print("saveas", doc.SaveAs3(OUT, 0, 0))

tables = list(doc.GetFirstView.GetTableAnnotations or ())
tb = win.Dispatch(tables[0])
dump(tb, "BEFORE")

print("InsertColumn2:", tb.InsertColumn2(4, 0, "数量", 0))
idx = int(tb.ColumnCount) - 1
print("new index", idx)
print("SetColumnType2:", tb.SetColumnType2(idx, 203, True))
print("ForceRebuild3:", call_method(doc, "ForceRebuild3", False))
dump(tb, "AFTER")
print("saved", doc.SaveAs3(OUT, 0, 0), OUT)
