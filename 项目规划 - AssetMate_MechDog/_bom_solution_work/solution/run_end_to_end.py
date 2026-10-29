import json
import os
import sys

from bom_native import create_drawing_with_bom

ASSEMBLY = r"D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\测试用例\工程图测试用例\条码扫描设备\SAB1_0000_000.SLDASM"
OUT = r"D:\WorkBuddy_space\_assetmate_test_run\bom_fix\BOM_SOLVED.SLDDRW"
REPORT = r"D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\项目规划 - AssetMate_MechDog\_bom_solution_work\solution\验证结果.json"

if __name__ == "__main__":
    result = create_drawing_with_bom(ASSEMBLY, OUT, configuration="Default")
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "data"}, ensure_ascii=False, indent=2))
    print("first rows:", result["data"][:3])
