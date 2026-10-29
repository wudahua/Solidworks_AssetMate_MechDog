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
# use latest populated 5-column table
for raw in reversed(list(doc.GetFirstView.GetTableAnnotations or ())):
    tb = win.Dispatch(raw)
    if int(tb.RowCount) == 10 and int(tb.ColumnCount) == 5:
        break
else:
    raise RuntimeError("no 10x5 table")
print("before", tb.RowCount, tb.ColumnCount)
ole = tb._oleobj_
dispid = ole.GetIDsOfNames("ColumnHidden")
for i in range(5):
    try:
        print("hidden", i, ole.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYGET, True, i))
    except Exception as exc:
        print("hidden", i, "ERR", repr(exc))
print("put col4 hidden=True", ole.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUT, True, 4, True))
print("rebuild", call_method(doc, "ForceRebuild3", False))
print("after", tb.RowCount, tb.ColumnCount, tb.TotalColumnCount)
for i in range(int(tb.ColumnCount)):
    print("col", i, tb.GetColumnTitle2(i, True), tb.GetColumnType(i))
