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
while view is not None:
    for raw in list(view.GetAnnotations or ()):
        try:
            ann = win.Dispatch(raw)
            print("ann", ann.GetName, "type", ann.GetType)
            try:
                spec = win.Dispatch(ann.GetSpecificAnnotation)
                print("  spec", spec)
                for name in ("GetLineCount", "GetTriangleCount"):
                    try:
                        print(" ", name, getattr(spec, name))
                    except Exception:
                        pass
                for i in range(5):
                    try:
                        print("  text", i, call_method(spec, "GetText", i))
                    except Exception:
                        pass
            except Exception as exc:
                print("  spec ERR", repr(exc))
        except Exception as exc:
            print("ann ERR", repr(exc))
    view = view.GetNextView
