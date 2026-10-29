import os
import pythoncom
import win32com.client as win


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
doc = next(d for d in list(sw.GetDocuments or ()) if str(d.GetTitle).startswith("h - "))
view = next(v for v in list(doc.GetCurrentSheet.GetViews or ()) if getattr(v, "Type", None) != 1)
tpl = os.path.join(str(sw.GetExecutablePath), "lang", "chinese-simplified", "bom-standard.sldbomtbt")
doc.ActivateView(view.Name)
doc.ClearSelection2(True)

table = view.InsertBomTable4(False, 0.24, 0.19, 1, 3, "Default", tpl, False, 0, False)
tb = win.Dispatch(table)
tb.BomFeature.TableType = 2
call_method(doc, "ForceRebuild3", False)
print("after switch", tb.RowCount, tb.ColumnCount)
try:
    print("DeleteColumn(4):", tb.DeleteColumn(4))
except Exception as exc:
    print("DeleteColumn ERR", repr(exc))
call_method(doc, "ForceRebuild3", False)
print("after delete", tb.RowCount, tb.ColumnCount)
for i in range(int(tb.ColumnCount)):
    print("col", i, tb.GetColumnTitle2(i, True), "type", tb.GetColumnType(i))
for r in range(min(4, int(tb.RowCount))):
    print("row", r, [tb.Text(r, c) for c in range(int(tb.ColumnCount))])
