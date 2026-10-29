import json
import os
import pythoncom
import win32com.client as win

PATH = r"D:\WorkBuddy_space\_assetmate_test_run\bom_fix\BOM_SOLVED.SLDDRW"
REPORT = r"D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\项目规划 - AssetMate_MechDog\_bom_solution_work\solution\复开验证结果.json"

pythoncom.CoInitialize()
sw = win.dynamic.Dispatch(win.GetActiveObject("SldWorks.Application"))
doc = next((d for d in list(sw.GetDocuments or ()) if os.path.normcase(d.GetPathName) == os.path.normcase(PATH)), None)
if doc is not None:
    print("close", sw.CloseDoc(doc.GetTitle))
errors = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
warnings = win.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
doc = sw.OpenDoc6(PATH, 3, 1, "", errors, warnings)
assert doc is not None, (errors.value, warnings.value)
tables = list(doc.GetFirstView.GetTableAnnotations or ())
assert len(tables) == 1, len(tables)
tb = win.Dispatch(tables[0])
result = {
    "rows": int(tb.RowCount),
    "columns": int(tb.ColumnCount),
    "total_columns": int(tb.TotalColumnCount),
    "headers": [tb.GetColumnTitle2(i, True) for i in range(int(tb.ColumnCount))],
    "first_data_row": [tb.Text(1, c) for c in range(int(tb.ColumnCount))],
    "table_type": int(tb.BomFeature.TableType),
    "native_bom": tb.BomFeature is not None,
}
result["ok"] = result["rows"] == 10 and result["columns"] == 4 and result["native_bom"]
with open(REPORT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
assert result["ok"]
