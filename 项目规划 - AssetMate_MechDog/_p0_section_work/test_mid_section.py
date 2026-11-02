import os
import pythoncom
import win32com.client as win

PATH = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\axis.SLDDRW"
PDF = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\axis_mid.pdf"
PNG = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\axis_mid.png"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
doc = next(d for d in list(sw.GetDocuments or ()) if str(d.GetTitle).startswith("axis - "))
parent = None
view = doc.GetFirstView
while view is not None:
    if getattr(view, "Type", None) not in (1, 2):
        parent = view
        break
    view = view.GetNextView
print("parent", parent.Name)
o = list(parent.GetOutline)
mid_y = (o[1] + o[3]) / 2
x1, x2 = o[0] - 0.01, o[2] + 0.01
print("line", x1, mid_y, x2, mid_y)
doc.ActivateView(parent.Name)
doc.ClearSelection2(True)
doc.SketchManager.CreateLine(x1, mid_y, 0.0, x2, mid_y, 0.0)
sec = doc.CreateSectionViewAt5(o[2] + 0.05, mid_y, 0.0, "B", 0, None, 0.00635)
print("section", sec.Name if sec else None)
print("rebuild", call_method(doc, "ForceRebuild3", False))
print("pdf", doc.SaveAs3(PDF, 0, 0))
import fitz
pdf = fitz.open(PDF)
pix = pdf[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
pix.save(PNG)
print(PNG)
