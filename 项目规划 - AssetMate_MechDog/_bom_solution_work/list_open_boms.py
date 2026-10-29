import pythoncom
import win32com.client as win

pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
for doc in list(sw.GetDocuments or ()):
    try:
        if int(doc.GetType) != 3:
            continue
        tables = list(doc.GetFirstView.GetTableAnnotations or ())
        for raw in tables:
            tb = win.Dispatch(raw)
            print(doc.GetTitle, doc.GetPathName, "rows", tb.RowCount, "cols", tb.ColumnCount, "total", tb.TotalColumnCount)
    except Exception as exc:
        print("ERR", doc.GetTitle, repr(exc))
