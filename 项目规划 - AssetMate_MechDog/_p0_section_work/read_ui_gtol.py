import pythoncom
import win32com.client as win

PATH = r"D:\142套solidworks工程图图纸模板\solidworks工程图图纸2\铝合金下料切割机\2016-8-6开料\同步输送带\PF160401-04-09同步带电机座.SLDDRW"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
errors = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
warnings = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
doc = sw.OpenDoc6(PATH, 3, 1, "", errors, warnings)
print("doc", doc.GetTitle if doc else None, errors.value, warnings.value)
for view in [doc.GetFirstView] + []:
    pass
view = doc.GetFirstView
while view is not None:
    try:
        annotations = list(view.GetAnnotations or ())
    except Exception:
        annotations = []
    for raw in annotations:
        try:
            ann = win.Dispatch(raw)
            if int(ann.GetType) != 5:
                continue
            gtol = win.Dispatch(ann.GetSpecificAnnotation)
            print("ANNOTATION", ann.GetName)
            try:
                print("format", call_method(gtol, "GetFormat"))
            except Exception as exc:
                print("format ERR", repr(exc))
            try:
                print("can convert", call_method(gtol, "CanConvertFormat"))
                print("convert", call_method(gtol, "ConvertFormat"))
                print("format after", call_method(gtol, "GetFormat"))
            except Exception as exc:
                print("convert ERR", repr(exc))
            try:
                print("frame count", call_method(gtol, "GetFrameCount"))
            except Exception as exc:
                print("count ERR", repr(exc))
            frame = call_method(gtol, "GetFrame", 1)
            print("frame", frame)
            if frame is not None:
                frame = win.Dispatch(frame)
                print("XML", call_method(frame, "GetSymbolXml"))
        except Exception as exc:
            print("item ERR", repr(exc))
    view = view.GetNextView
