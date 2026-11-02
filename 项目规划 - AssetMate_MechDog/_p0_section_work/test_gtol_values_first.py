import pythoncom
import win32com.client as win

PDF = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\gtol_values_first.pdf"


def call_method(obj, name, *args):
    ole = obj._oleobj_
    dispid = ole.GetIDsOfNames(name)
    if args:
        return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True, *args)
    return ole.Invoke(dispid, 0, pythoncom.DISPATCH_METHOD, True)


pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
doc = next(d for d in list(sw.GetDocuments or ()) if str(d.GetTitle).startswith("axis - "))
gtol = win.Dispatch(call_method(doc, "InsertGtol"))

# Write symbol, tolerance and datum BEFORE converting to 2022 format
print("set symbol old", call_method(gtol, "SetFrameSymbols2", 1, "<IGTOL-POSI>", False, "", False, "", "", "", ""))
print("set values old", call_method(gtol, "SetFrameValues2", 1, "0.05", "", "A", "", ""))
try:
    print("text count old", call_method(gtol, "GetTextCount"))
except Exception as exc:
    print("text count old ERR", repr(exc))
print("convert", call_method(gtol, "ConvertFormat"))
print("add frame", call_method(gtol, "AddFrame"))
frame = win.Dispatch(call_method(gtol, "GetFrame", 1))
xml = (
    "<GtolFrame><ToleranceSymbol>IGTOL-POSI</ToleranceSymbol>"
    "<ToleranceRangeInfo><PrimaryToleranceValue>0.05</PrimaryToleranceValue>"
    "<PrimaryRangeSymbol></PrimaryRangeSymbol></ToleranceRangeInfo>"
    "<DatumCompartment><DatumDetail><DatumLetter>A</DatumLetter></DatumDetail></DatumCompartment>"
    "<DatumCompartment></DatumCompartment><DatumCompartment></DatumCompartment></GtolFrame>"
)
print("set xml", call_method(frame, "SetSymbolXml", xml))
try:
    print("text count new", call_method(gtol, "GetTextCount"))
except Exception as exc:
    print("text count new ERR", repr(exc))
for i in range(4):
    try:
        print("text", i, call_method(gtol, "GetTextAtIndex", i))
    except Exception:
        pass
ann = win.Dispatch(call_method(gtol, "GetAnnotation"))
call_method(ann, "SetPosition", 0.14, 0.10, 0.0)
call_method(doc, "ForceRebuild3", False)
print("pdf", doc.SaveAs3(PDF, 0, 0))
import fitz
p = fitz.open(PDF)
print("PDF TEXT:", p[0].get_text())
