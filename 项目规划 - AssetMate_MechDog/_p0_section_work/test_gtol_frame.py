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
doc = next((d for d in list(sw.GetDocuments or ()) if str(d.GetTitle).startswith("axis - ")), None)
if doc is None:
    errors = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\axis.SLDDRW", 3, 1, "", errors, warnings)
print("doc", doc.GetTitle if doc else None)
gtol = win.Dispatch(call_method(doc, "InsertGtol"))
print("gtol", gtol)
for name in ("GetFormat", "CanConvertFormat", "GetFrameCount"):
    try:
        print(name, call_method(gtol, name))
    except Exception as exc:
        print(name, "ERR", repr(exc))
try:
    frame = call_method(gtol, "GetFrame", 1)
    print("frame before convert", frame)
except Exception as exc:
    print("frame before convert ERR", repr(exc))
try:
    print("ConvertFormat", call_method(gtol, "ConvertFormat"))
except Exception as exc:
    print("ConvertFormat ERR", repr(exc))
try:
    print("format after", call_method(gtol, "GetFormat"))
except Exception as exc:
    print("format after ERR", repr(exc))
try:
    frame = call_method(gtol, "GetFrame", 1)
    print("frame after convert", frame)
    if frame is not None:
        frame = win.Dispatch(frame)
        print("xml", call_method(frame, "GetSymbolXml"))
except Exception as exc:
    print("frame after convert ERR", repr(exc))
try:
    print("count after", call_method(gtol, "GetFrameCount"))
except Exception as exc:
    print("count after ERR", repr(exc))
try:
    ann = win.Dispatch(call_method(gtol, "GetAnnotation"))
    print("select annotation", ann.Select4(False, None))
except Exception as exc:
    print("select annotation ERR", repr(exc))
try:
    print("AddFrame", call_method(gtol, "AddFrame"))
except Exception as exc:
    print("AddFrame ERR", repr(exc))
for mode in ("direct", "method"):
    try:
        frame = gtol.GetFrame(1) if mode == "direct" else call_method(gtol, "GetFrame", 1)
        print("frame after select", mode, frame)
        if frame is not None:
            frame = win.Dispatch(frame)
            print("xml", call_method(frame, "GetSymbolXml"))
    except Exception as exc:
        print("frame after select", mode, "ERR", repr(exc))
