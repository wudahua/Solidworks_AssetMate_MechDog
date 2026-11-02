import pythoncom
import win32com.client as win

PDF = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\gtol_position.pdf"
PNG = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\gtol_position.png"


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
print("format", call_method(gtol, "GetFormat"))
print("convert", call_method(gtol, "ConvertFormat"))
print("add frame", call_method(gtol, "AddFrame"))
frame = win.Dispatch(call_method(gtol, "GetFrame", 1))
print("set type position", call_method(frame, "SetFrameToleranceType", 23))
print("set values", call_method(gtol, "SetFrameValues2", 1, "0.05", "", "A", "", ""))
ann = win.Dispatch(call_method(gtol, "GetAnnotation"))
print("set position", call_method(ann, "SetPosition", 0.10, 0.10, 0.0))
print("rebuild", call_method(doc, "ForceRebuild3", False))
try:
    xml = call_method(frame, "GetSymbolXml")
    print("xml before", xml)
    for library in ("IGTOL-NO-BRACKETS", "IGTOL", "GTOL", "GGTOL", "GCS"):
        if library == "IGTOL-NO-BRACKETS":
            candidate = xml.replace(
                "<ToleranceSymbol></ToleranceSymbol>",
                "<ToleranceSymbol>IGTOL-POSI</ToleranceSymbol>",
            ).replace(
                "<PrimaryRangeSymbol></PrimaryRangeSymbol>",
                "<PrimaryRangeSymbol>phi</PrimaryRangeSymbol>",
            )
        else:
            candidate = xml.replace(
                "<ToleranceSymbol></ToleranceSymbol>",
                "<ToleranceSymbol>&lt;%s-POSI&gt;</ToleranceSymbol>" % library,
            )
        print("set xml", library, call_method(frame, "SetSymbolXml", candidate))
        print("xml after", call_method(frame, "GetSymbolXml"))
except Exception as exc:
    print("xml ERR", repr(exc))
call_method(doc, "ForceRebuild3", False)
print("set values again", call_method(gtol, "SetFrameValues2", 1, "0.05", "", "A", "", ""))
print("xml final", call_method(frame, "GetSymbolXml"))
call_method(doc, "ForceRebuild3", False)
print("pdf", doc.SaveAs3(PDF, 0, 0))
import fitz
pdf = fitz.open(PDF)
pix = pdf[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
pix.save(PNG)
print(PNG)
