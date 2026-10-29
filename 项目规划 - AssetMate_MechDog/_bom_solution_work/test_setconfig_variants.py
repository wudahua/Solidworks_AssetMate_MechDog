import pythoncom
import win32com.client as win

CASES = {
    "h - ": (False, [True, False], ["Default", "UP"]),
    "ok - ": (True, [True, False], ["Default", "UP"]),
    "rb - ": (False, [True], ["Default"]),
    "c3 - ": (True, [True], ["Default"]),
}


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
for doc in list(sw.GetDocuments or ()):
    title = str(doc.GetTitle)
    case = next((v for k, v in CASES.items() if title.startswith(k)), None)
    if case is None:
        continue
    tb = win.Dispatch(list(doc.GetFirstView.GetTableAnnotations or ())[0])
    bf = tb.BomFeature
    print("\n", title, "before", tb.RowCount, tb.ColumnCount)
    try:
        print("config count", call_method(bf, "GetConfigurationCount"))
    except Exception as exc:
        print("config count ERR", repr(exc))
    only_visible, visible, names = case
    try:
        result = bf.SetConfigurations(only_visible, visible, names)
        print("set", case, "=>", result)
    except Exception as exc:
        print("set ERR", repr(exc))
    print("rebuild", call_method(doc, "ForceRebuild3", False))
    # Reacquire the table through the feature to avoid a stale wrapper
    try:
        tb2 = win.Dispatch(bf.GetTableAnnotations[0])
    except Exception:
        tb2 = tb
    print("after", tb2.RowCount, tb2.ColumnCount, [tb2.GetColumnTitle2(i, True) for i in range(int(tb2.ColumnCount))])
