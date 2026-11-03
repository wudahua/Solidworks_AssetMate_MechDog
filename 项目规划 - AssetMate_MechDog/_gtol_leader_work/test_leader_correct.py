import os
import pythoncom
import win32com.client as win

PDF = r"D:\WorkBuddy_space\_assetmate_test_run\leader_correct.pdf"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
doc = next((d for d in list(sw.GetDocuments or ()) if str(d.GetTitle).startswith("PF160401") and int(d.GetType) == 3), None)
if doc is None:
    errors = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc = sw.OpenDoc6(r"D:\142套solidworks工程图图纸模板\solidworks工程图图纸2\铝合金下料切割机\2016-8-6开料\同步输送带\PF160401-04-09同步带电机座.SLDDRW", 3, 1, "", errors, warnings)
print("doc", doc.GetTitle if doc else None)
view = doc.GetFirstView
gt = None
while view is not None and gt is None:
    for raw in list(view.GetAnnotations or ()):
        try:
            ann = win.Dispatch(raw)
            if int(ann.GetType) == 5:
                gt = ann
                break
        except Exception:
            pass
    view = view.GetNextView
print("gt", gt.GetName if gt else None)
# Select an edge before setting the leader
selected = False
for comp in list(view.GetVisibleComponents or ()):
    for edge in list(view.GetVisibleEntities2(comp, 1) or ()):
        try:
            selected = bool(win.Dispatch(edge).Select2(False, 0))
        except Exception:
            selected = False
        if selected:
            print("select edge", selected)
            break
    if selected:
        break

# BENT=2, RIGHT=2, smart arrow=True, perpendicular=True, allAround=False, dashed=False
print("SetLeader3", call_method(gt, "SetLeader3", 2, 2, True, True, False, False))
print("leader count", gt.GetLeaderCount)
print("rebuild", call_method(doc, "ForceRebuild3", False))
print("pdf", doc.SaveAs3(PDF, 0, 0))
