import pythoncom
import win32com.client as win


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


def dump(gtol, label):
    print("\n===", label, "===")
    for name in ("GetHeight", "GetTextFont", "GetTextCount"):
        try:
            print(name, call_method(gtol, name))
        except Exception as exc:
            print(name, "ERR", repr(exc))
    for i in range(12):
        try:
            text = call_method(gtol, "GetTextAtIndex", i)
            if text:
                print("text", i, repr(text))
                try:
                    print("  height", call_method(gtol, "GetTextHeightAtIndex", i))
                except Exception as exc:
                    print("  height ERR", repr(exc))
        except Exception:
            pass


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
for doc in list(sw.GetDocuments or ()):
    title = str(doc.GetTitle)
    if not (title.startswith("PF160401") or title.startswith("axis - ")):
        continue
    try:
        if int(doc.GetType) != 3:
            continue
        view = win.Dispatch(call_method(doc, "GetFirstView"))
    except Exception:
        continue
    while view is not None:
        for raw in list(view.GetAnnotations or ()):
            try:
                ann = win.Dispatch(raw)
                if int(ann.GetType) != 5:
                    continue
                gtol = win.Dispatch(ann.GetSpecificAnnotation)
                dump(gtol, title + " / " + ann.GetName)
                break
            except Exception:
                pass
        view = view.GetNextView
