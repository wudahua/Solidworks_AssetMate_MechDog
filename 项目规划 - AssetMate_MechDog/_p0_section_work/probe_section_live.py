import pythoncom
import win32com.client as win

PATH = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\axis.SLDDRW"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.Dispatch("SldWorks.Application"))
sw.Visible = True
errors = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
warnings = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
doc = sw.OpenDoc6(PATH, 3, 1, "", errors, warnings)
print("doc", doc.GetTitle if doc else None, errors.value, warnings.value)
view = doc.GetFirstView
while view is not None:
    if getattr(view, "Type", None) == 2:
        print("section view", view.Name)
        try:
            sec = win.Dispatch(call_method(view, "GetSection"))
            print("DrSection", sec)
            for name in ("GetAutoHatch", "GetDisplayOnlySurfaceCut", "GetLabel"):
                try:
                    print(name, call_method(sec, name))
                except Exception as exc:
                    print(name, "ERR", repr(exc))
            try:
                print("ScaleHatchPattern", sec.ScaleHatchPattern)
            except Exception as exc:
                print("Scale ERR", repr(exc))
            try:
                print("SectionDepth", sec.SectionDepth)
            except Exception as exc:
                print("Depth ERR", repr(exc))
            for name in ("GetLineInfo", "GetName"):
                try:
                    print(name, call_method(sec, name))
                except Exception as exc:
                    print(name, "ERR", repr(exc))
        except Exception as exc:
            print("GetSection ERR", repr(exc))
    view = view.GetNextView
