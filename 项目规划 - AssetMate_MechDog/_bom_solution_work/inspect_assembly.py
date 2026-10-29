import pythoncom
import win32com.client as win

pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
assy = next(d for d in list(sw.GetDocuments or ()) if str(d.GetPathName).upper().endswith("SAB1_0000_000.SLDASM"))
print("assembly", assy.GetTitle)
for name in ("GetActiveConfigurationName", "GetConfigurationNames"):
    try:
        print(name, getattr(assy, name))
    except Exception as exc:
        print(name, "ERR", repr(exc))
try:
    print("IGetActiveConfigurationName", assy.GetActiveConfigurationName)
except Exception as exc:
    print("active ERR", repr(exc))
components = list(assy.GetComponents(False) or [])
print("components", len(components))
for comp in components:
    try:
        print(comp.Name2, "suppression", comp.GetSuppression, "visible", comp.Visible)
    except Exception as exc:
        print("comp ERR", repr(exc))
