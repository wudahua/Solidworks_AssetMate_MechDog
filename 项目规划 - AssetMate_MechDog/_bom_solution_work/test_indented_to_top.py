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

for initial in (3, 1):
    table = view.InsertBomTable4(False, 0.24, 0.19, 1, initial, "Default", tpl, False, 0, False)
    tb = win.Dispatch(table)
    bf = tb.BomFeature
    print("initial", initial, "rows", tb.RowCount, "cols", tb.ColumnCount)
    try:
        bf.TableType = 2
        print("set TableType=2 ok")
    except Exception as exc:
        print("set TableType ERR", repr(exc))
    print("rebuild", call_method(doc, "ForceRebuild3", False))
    print("after type", bf.TableType, "rows", tb.RowCount, "cols", tb.ColumnCount)
    for r in range(min(int(tb.RowCount), 4)):
        print(" row", r, [tb.Text(r, c) for c in range(int(tb.ColumnCount))])
