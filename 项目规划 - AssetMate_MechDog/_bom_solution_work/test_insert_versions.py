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
base = str(sw.GetExecutablePath)
tpl = os.path.join(base, "lang", "chinese-simplified", "bom-standard.sldbomtbt")
doc.ActivateView(view.Name)
doc.ClearSelection2(True)

cases = [
    ("v2-top", 2, 2, ""),
    ("v2-indented", 2, 3, "Default"),
    ("v3-top", 3, 2, ""),
    ("v3-indented", 3, 3, "Default"),
    ("v4-parts", 4, 1, "Default"),
    ("v4-indented", 4, 3, "Default"),
]
for idx, (label, version, bom_type, config) in enumerate(cases):
    try:
        if version == 2:
            table = view.InsertBomTable2(False, 0.24, 0.19, 1, bom_type, config, tpl)
        elif version == 3:
            table = view.InsertBomTable3(False, 0.24, 0.19, 1, bom_type, config, tpl, False)
        else:
            table = view.InsertBomTable4(False, 0.24, 0.19, 1, bom_type, config, tpl, False, 0, False)
        call_method(doc, "ForceRebuild3", False)
        if table is None:
            print(label, "None")
            continue
        tb = win.Dispatch(table)
        print(label, "rows", tb.RowCount, "cols", tb.ColumnCount,
              [tb.GetColumnTitle2(i, True) for i in range(int(tb.ColumnCount))])
    except Exception as exc:
        print(label, "ERR", repr(exc))
