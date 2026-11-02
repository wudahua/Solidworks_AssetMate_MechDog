import os
import pythoncom
import win32com.client as win

MODEL = r"D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\P0-剖视无剖面线\证据\axis\model.SLDPRT"
OUT = r"D:\WorkBuddy_space\_assetmate_test_run\weld_test_WELDFILL.SLDDRW"
PDF = r"D:\WorkBuddy_space\_assetmate_test_run\weld_test_WELDFILL.pdf"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
base = str(sw.GetExecutablePath)
template = os.path.join(base, "data", "templates", "gb.drwdot")
# Open/reuse model
model = next((d for d in list(sw.GetDocuments or ()) if str(d.GetPathName) == MODEL), None)
if model is None:
    errors = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    model = sw.OpenDoc6(MODEL, 1, 1, "", errors, warnings)
print("model", model.GetTitle if model else None)
drw = sw.NewDocument(template, 9, 0, 0)
view = None
for name in ("*Front", "*前视", "前视", "*前視"):
    try:
        view = drw.CreateDrawViewFromModelView3(MODEL, name, 0.15, 0.15, 0)
        if view is not None:
            break
    except Exception:
        pass
print("view", view.Name if view else None)
drw.SaveAs3(OUT, 0, 0)
drw.ActivateView(view.Name)
drw.ClearSelection2(True)
selected = False
for comp in list(view.GetVisibleComponents or ()):
    for edge in list(view.GetVisibleEntities2(comp, 1) or ()):
        try:
            selected = bool(win.Dispatch(edge).Select2(False, 0))
        except Exception:
            try:
                selected = bool(win.Dispatch(edge).Select4(False, None))
            except Exception:
                selected = False
        if selected:
            break
    if selected:
        break
print("selected", selected)

# 10 arguments: Dim1, Symbol, Dim2, Symmetric, FieldWeld, ShowOtherSide,
# DashOnTop, Peripheral, HasProcess, ProcessValue
drw.InsertWeldSymbol("", "<WELD-FILL>", "6", False, False, False, False, False, False, "")
call_method(drw, "ForceRebuild3", False)
view = drw.GetFirstView
found = False
while view is not None:
    for raw in list(view.GetAnnotations or ()):
        ann = win.Dispatch(raw)
        try:
            if int(ann.GetType) != 16:  # weld symbol may use another annotation type; dump candidates below
                continue
            ws = win.Dispatch(ann.GetSpecificAnnotation)
            print("weld", ann.GetName, "lines", ws.GetLineCount, "triangles", ws.GetTriangleCount)
            for i in range(6):
                try:
                    print("text", i, call_method(ws, "GetText", i))
                except Exception:
                    pass
            found = True
        except Exception:
            pass
    view = view.GetNextView
print("found", found)
print("pdf", drw.SaveAs3(PDF, 0, 0))
import fitz
p = fitz.open(PDF)
print("PDF TEXT:", p[0].get_text())
