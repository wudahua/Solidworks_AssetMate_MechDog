"""Run from the solution package. Override paths with env vars if needed."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "代码"))
from bom_native import create_drawing_with_bom  # noqa: E402

ASSEMBLY = os.environ.get(
    "BOM_ASSEMBLY",
    r"D:\WorkBuddy_space\Solidworks_AssetMate_MechDog\测试用例\工程图测试用例\条码扫描设备\SAB1_0000_000.SLDASM",
)
OUT = os.environ.get("BOM_OUT", os.path.join(ROOT, "成功案例", "BOM_VERIFY.SLDDRW"))
REPORT = os.path.join(ROOT, "验证结果", "端到端结果_复测.json")

if __name__ == "__main__":
    result = create_drawing_with_bom(ASSEMBLY, OUT, configuration="Default")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "data"}, ensure_ascii=False, indent=2))
    print("first rows:", result["data"][:3])
