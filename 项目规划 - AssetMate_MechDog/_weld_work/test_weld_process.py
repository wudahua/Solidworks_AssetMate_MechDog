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
doc = next(d for d in list(sw.GetDocuments or ()) if str(d.GetTitle).startswith("weld_test_WELDFILL"))
view = doc.GetFirstView
ws = None
while view is not None and ws is None:
    for raw in list(view.GetAnnotations or ()):
        ann = win.Dispatch(raw)
        try:
            if int(ann.GetType) == 8:
                ws = win.Dispatch(ann.GetSpecificAnnotation)
                break
        except Exception:
            pass
    view = view.GetNextView
print("ws", ws)
for label, action in [
    ("spot", lambda: call_method(ws, "SetText", True, "", "SPOT", "6", "", 0)),
    ("process", lambda: call_method(ws, "SetProcess", True, "135", True)),
]:
    try:
        print(label, action())
    except Exception as exc:
        print(label, "ERR", repr(exc))
    print("rebuild", call_method(doc, "ForceRebuild3", False))
    try:
        print("lines", ws.GetLineCount, "tri", ws.GetTriangleCount)
    except Exception as exc:
        print("counts ERR", repr(exc))
    for i in range(4):
        try:
            print(" text", i, call_method(ws, "GetText", i))
        except Exception:
            pass
