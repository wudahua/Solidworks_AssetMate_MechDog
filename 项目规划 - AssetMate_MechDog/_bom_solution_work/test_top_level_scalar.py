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

cases = [
    ("scalar-true-one", True, True, ["Default"]),
    ("scalar-true-all", True, True, ["Default", "UP"]),
    ("false-scalar-visible", False, True, ["Default", "UP"]),
]
for label, only_visible, visible, names in cases:
    table = view.InsertBomTable4(False, 0.24, 0.19, 1, 2, "", tpl, False, 0, False)
    tb = win.Dispatch(table)
    print("\n", label, "created", tb.RowCount, tb.ColumnCount)
    try:
        result = tb.BomFeature.SetConfigurations(only_visible, visible, names)
        print("SetConfigurations =>", result)
    except Exception as exc:
        print("SetConfigurations ERR", repr(exc))
    print("rebuild", call_method(doc, "ForceRebuild3", False))
    print("after", tb.RowCount, tb.ColumnCount,
          [tb.GetColumnTitle2(i, True) for i in range(int(tb.ColumnCount))])
    if int(tb.RowCount) > 1:
        for r in range(min(3, int(tb.RowCount))):
            print(" row", r, [tb.Text(r, c) for c in range(int(tb.ColumnCount))])
        break
