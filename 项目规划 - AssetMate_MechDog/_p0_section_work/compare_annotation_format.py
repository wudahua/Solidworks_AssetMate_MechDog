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
for doc in list(sw.GetDocuments or ()):
    title = str(doc.GetTitle)
    if not (title.startswith("PF160401") or title.startswith("axis - ")):
        continue
    try:
        if int(doc.GetType) != 3:
            continue
    except Exception:
        continue
    view = win.Dispatch(call_method(doc, "GetFirstView"))
    while view is not None:
        for raw in list(view.GetAnnotations or ()):
            ann = None
            try:
                ann = win.Dispatch(raw)
                if int(ann.GetType) != 5:
                    continue
                print("\n", title, ann.GetName)
                for name in ("GetUseDocTextFormat", "GetTextFormat"):
                    try:
                        print(name, getattr(ann, name))
                    except Exception as exc:
                        print(name, "ERR", repr(exc))
                break
            except Exception as exc:
                print("item ERR", repr(exc))
        view = view.GetNextView
