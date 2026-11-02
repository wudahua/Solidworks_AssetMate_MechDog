import pythoncom
import win32com.client as win

PDF = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\gtol_xml_only.pdf"
PNG = r"D:\WorkBuddy_space\_assetmate_test_run\section_fix\gtol_xml_only.png"


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
print("convert", call_method(gtol, "ConvertFormat"))
print("add frame", call_method(gtol, "AddFrame"))
frame = win.Dispatch(call_method(gtol, "GetFrame", 1))
xml = call_method(frame, "GetSymbolXml")
print("template", xml)
if not xml:
    xml = "<GtolFrame><ToleranceSymbol></ToleranceSymbol><ToleranceRangeInfo><PrimaryToleranceValue>0.05</PrimaryToleranceValue><PrimaryRangeSymbol></PrimaryRangeSymbol><ToleranceZone>0.05</ToleranceZone></ToleranceRangeInfo><DatumCompartment><DatumDetail><DatumLetter>A</DatumLetter></DatumDetail></DatumCompartment><DatumCompartment></DatumCompartment><DatumCompartment></DatumCompartment></GtolFrame>"
xml = xml.replace("<ToleranceSymbol></ToleranceSymbol>", "<ToleranceSymbol>IGTOL-POSI</ToleranceSymbol>")
if "<PrimaryToleranceValue>" in xml:
    import re
    xml = re.sub(r"<PrimaryToleranceValue>[^<]*</PrimaryToleranceValue>", "<PrimaryToleranceValue>0.05</PrimaryToleranceValue>", xml)
else:
    xml = xml.replace("<ToleranceRangeInfo>", "<ToleranceRangeInfo><PrimaryToleranceValue>0.05</PrimaryToleranceValue>")
if "<DatumLetter>" in xml:
    import re
    xml = re.sub(r"<DatumLetter>[^<]*</DatumLetter>", "<DatumLetter>A</DatumLetter>", xml, count=1)
else:
    xml = xml.replace("<DatumCompartment>", "<DatumCompartment><DatumDetail><DatumLetter>A</DatumLetter></DatumDetail>", 1)
print("set xml", call_method(frame, "SetSymbolXml", xml))
print("final xml", call_method(frame, "GetSymbolXml"))
ann = win.Dispatch(call_method(gtol, "GetAnnotation"))
print("position", call_method(ann, "SetPosition", 0.12, 0.10, 0.0))
print("rebuild", call_method(doc, "ForceRebuild3", False))
print("pdf", doc.SaveAs3(PDF, 0, 0))
import fitz
pdf = fitz.open(PDF)
pix = pdf[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
pix.save(PNG)
print(PNG)
