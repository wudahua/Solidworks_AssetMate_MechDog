"""Live ZJZL acceptance on disposable fixture copies. See docs/zjzl-capabilities.md."""
import argparse
from pathlib import Path
import hashlib
import json
import math
import shutil
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solidworks_assetmate_mechdog import server
from solidworks_assetmate_mechdog.tools.drawing import VIEW_ALIASES, _new_document
from solidworks_assetmate_mechdog.tools.zjzl_native import Native, model_views, path_key, read_reference, save_drawing
from solidworks_assetmate_mechdog.tools.zjzl_orthographic import transform

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--part", type=Path, help="60 × 40 mm plate with a visible Ø5 mm hole in the front view")
parser.add_argument("--assembly", type=Path, help="Assembly with a 20 mm distance mate between two instances of one part")
parser.add_argument("--component-part", type=Path, help="The assembly's sole referenced part file")
parser.add_argument("--drawing-template", type=Path, required=True, help="A3 first-angle drawing template")
parser.add_argument("--bom-template", type=Path, help="Standard native BOM template")
parser.add_argument("--base-assembly-drawing", type=Path, help="Optional fallback drawing with one front view, no BOM or balloons")
parser.add_argument("--output-dir", type=Path, default=Path(tempfile.gettempdir()), help="Parent folder for the new test directory")
args = parser.parse_args()
if not (args.part or args.assembly):
    parser.error("Provide --part or --assembly")
if args.assembly and (args.component_part is None or args.bom_template is None):
    parser.error("--assembly requires --component-part and --bom-template")
for value in (args.part, args.assembly, args.component_part, args.drawing_template, args.bom_template, args.base_assembly_drawing):
    if value is not None and not value.is_file():
        parser.error(f"File missing: {value}")
ROOT = args.output_dir.resolve() / f"zjzl-live-{uuid4().hex[:8]}"
ROOT.mkdir(parents=True)

n = Native()
original = n.cast(n.app.ActiveDoc, "IModelDoc2")
original_path = original.GetPathName() if original is not None else ""
original_kind = original.GetType() if original is not None else 0
print("SolidWorks", n.dynamic.RevisionNumber, "fixture", ROOT, flush=True)


def run_tool(tool, expected=None, **kwargs):
    value = json.loads(getattr(server, tool)(**kwargs))
    if not value["ok"]:
        print(tool, value, flush=True)
    if expected is None:
        assert value["ok"], value
    else:
        assert value.get("error") == expected, value
    return value


def drawing(path, model_path, centre, fallback=None):
    raw = _new_document(n.dynamic, str(args.drawing_template.resolve()))
    if raw is None and fallback is not None:
        shutil.copyfile(fallback, path)
        deps = n.app.GetDocumentDependencies2(str(path), False, False, False)
        assert len(deps) == 2 and n.app.ReplaceReferencedDocument(str(path), deps[1], str(model_path))
        doc = n.document(str(path), 3)
        dim = n.cast(doc, "IDrawingDoc")
        sheet = n.cast(dim.GetCurrentSheet(), "ISheet")
        view, = model_views(n, sheet)
        return doc, dim, sheet, view
    assert raw is not None, "SolidWorks did not create a drawing from the supplied template"
    doc = n.cast(raw, "IModelDoc2")
    dim = n.cast(doc, "IDrawingDoc")
    ext = n.cast(doc.Extension, "IModelDocExtension")
    assert ext.SaveAs(str(path), 0, 1, None, 0, 0) == (True, 0, 0)
    model = n.cast(n.app.GetOpenDocumentByName(str(model_path)), "IModelDoc2")
    front = next(name for name in VIEW_ALIASES["front"] if name in model.GetModelViewNames())
    view = n.cast(dim.CreateDrawViewFromModelView3(str(model_path), front, *centre, 0), "IView")
    assert view is not None
    sheet = n.cast(dim.GetCurrentSheet(), "ISheet")
    doc.EditRebuild3()
    save_drawing(doc)
    return doc, dim, sheet, view


def part_case():
    path = ROOT / f"ZJZL_Plate_{ROOT.name}.SLDPRT"
    drw = path.with_suffix(".SLDDRW")
    shutil.copyfile(args.part, path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    part = n.document(str(path), 1)
    existing_names = part.GetModelViewNames()
    doc, draw, sheet, view = drawing(drw, path, (.14, .17))
    name = view.GetName2()
    scope = {"drawing_path": str(drw), "sheet": sheet.GetName()}
    run_tool("zjzl_create_orthographic_views", expected="view_overlap", **scope, base_view=name,
           directions=["right"], offset_m=.02)
    assert len(model_views(n, sheet)) == 1
    projections = run_tool("zjzl_create_orthographic_views", **scope, base_view=name,
                         directions=["top", "right"], offset_m=.09, save=True)
    assert projections["projection"] == "first"
    assert projections["views"]["top"]["position_m"][1] < .17
    assert projections["views"]["right"]["position_m"][0] < .14
    run_tool("zjzl_create_orthographic_views", expected="projection_exists", **scope, base_view=name,
           directions=["top"])
    custom = run_tool("zjzl_create_custom_view", **scope, model_path=str(path), normal=(.6, 0, .8),
                    up=(0, 1, 0), position_m=(.30, .23), save=True)
    assert math.dist(custom["rotation"], [.8, 0, .6, 0, 1, 0, -.6, 0, .8]) < 1e-6
    assert part.GetModelViewNames() == existing_names
    scope["view_name"] = name
    geometry = run_tool("zjzl_probe_geometry", **scope)
    print("edges", len(geometry["edges"]), "vertices", len(geometry["vertices"]), geometry["issues"], flush=True)
    straight = [e for e in geometry["edges"] if e["curve"] == "line" and e["reference"]]
    circle = min((e for e in geometry["edges"] if e["curve"] == "circle" and e["reference"]
                  and e["projected_axis_ratio"] > .99), key=lambda edge: edge["radius_m"])
    horizontal = max((e for e in straight if math.isclose(e["endpoints_sheet_m"][0][1],
                                                          e["endpoints_sheet_m"][1][1], abs_tol=1e-8)),
                     key=lambda edge: edge["length_m"])
    vertices = [v for v in geometry["vertices"] if v["reference"] and v["point_model_m"] is not None]
    at_ends = [min(vertices, key=lambda v: math.dist(v["point_model_m"], p))
               for p in horizontal["endpoints_model_m"]]
    reference_model = server.zjzl_geometry.GeometryReference
    vertex_refs = [reference_model(**v["reference"]) for v in at_ends]
    circular_ref = reference_model(**circle["reference"])
    run_tool("zjzl_add_reference_dimension", expected="dimension_value_mismatch", **scope, kind="diameter",
           references=[circular_ref], position_m=(.20, .22), expected_value=999)
    assert view.GetFirstDisplayDimension5() is None
    horizontal_record = run_tool("zjzl_add_reference_dimension", **scope, kind="horizontal", references=vertex_refs,
                               position_m=(.14, .236), expected_value=horizontal["length_m"] * 1000, save=True)
    diameter = run_tool("zjzl_add_reference_dimension", **scope, kind="diameter", references=[circular_ref],
                      position_m=(.20, .22), expected_value=circle["radius_m"] * 2000, save=True)
    run_tool("zjzl_add_reference_dimension", expected="dimension_exists", **scope, kind="diameter", references=[circular_ref],
           position_m=(.20, .22))
    run_tool("zjzl_add_reference_dimension", expected="reference_scope_mismatch", **scope, kind="diameter",
           references=[circular_ref.model_copy(update={"view": custom["view"]["name"]})], position_m=(.20, .22))
    n.app.CloseDoc(doc.GetTitle())
    doc = n.document(str(drw), 3)
    draw = n.cast(doc, "IDrawingDoc")
    sheet = n.cast(draw.GetCurrentSheet(), "ISheet")
    views = {item.GetName2(): item for item in model_views(n, sheet)}
    for item in projections["views"].values():
        assert n.cast(views[item["name"]].GetBaseView(), "IView").GetName2() == name
    c = views[custom["view"]["name"]]
    assert math.dist(transform(n, c)[:9], custom["rotation"]) < 1e-6
    ext = n.cast(doc.Extension, "IModelDocExtension")
    for receipt in (horizontal_record, diameter):
        raw, status = ext.GetObjectByPersistReference3(read_reference(receipt["dimension_id"]))
        assert status == 0 and raw is not None
        display = n.cast(raw, "IDisplayDimension")
        dimension = n.cast(display.GetDimension2(0), "IDimension")
        annotation = n.cast(display.GetAnnotation(), "IAnnotation")
        assert dimension.DrivenState == 1 and display.ShowParenthesis and not annotation.IsDangling()
        assert math.isclose(dimension.SystemValue * 1000, receipt["value"], abs_tol=1e-6)
        assert len(annotation.GetAttachedEntities3()) == len(receipt["attachments"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_hash
    print("PASS part projections, negative rollback, orientation, references and dimensions",
          horizontal_record["value"], diameter["value"], flush=True)


def assembly_case():
    path = ROOT / f"ZJZL_Gap_{ROOT.name}.SLDASM"
    drw = path.with_suffix(".SLDDRW")
    part_path = ROOT / f"ZJZL_MatePlate_{ROOT.name}.SLDPRT"
    shutil.copyfile(args.assembly, path)
    shutil.copyfile(args.component_part, part_path)
    dependencies = n.app.GetDocumentDependencies2(str(path), False, False, False)
    assert dependencies and len(dependencies) == 2, dependencies
    assert n.app.ReplaceReferencedDocument(str(path), dependencies[1], str(part_path))
    assembly_doc = n.document(str(path), 2)
    assembly = n.cast(assembly_doc, "IAssemblyDoc")
    for raw in assembly.GetComponents(False):
        component = n.cast(raw, "IComponent2")
        assert path_key(component.GetPathName()) == path_key(str(part_path)), component.GetPathName()
    mates = run_tool("zjzl_inspect_mates", assembly_path=str(path))
    print("mates", [{key: row[key] for key in ("name", "value", "unit", "issues")} for row in mates["mates"]], flush=True)
    assert any(row["feature_type"] == "MateDistanceDim" and row["unit"] == "mm" and len(row["entities"]) == 2
               and math.isclose(row["value"], 20.0, abs_tol=.001) for row in mates["mates"]), mates
    doc, draw, sheet, view = drawing(drw, path, (.155, .16), args.base_assembly_drawing)
    scope = dict(drawing_path=str(drw), view_name=view.GetName2(), sheet=sheet.GetName())
    geo = run_tool("zjzl_probe_geometry", **scope)
    reference = server.zjzl_geometry.GeometryReference(**next(e["reference"] for e in geo["edges"] if e["reference"]))
    run_tool("zjzl_add_bom_balloon", expected="bom_scope_unavailable", **scope, reference=reference, position_m=(.23, .20))
    assert view.InsertBomTable4(False, .29, .235, 1, 1, view.ReferencedConfiguration,
                                str(args.bom_template.resolve()), False, 0, False) is not None
    run_tool("zjzl_add_bom_balloon", expected="bom_item_mismatch", **scope, reference=reference, position_m=(.23, .20), expected_item="999")
    balloon = run_tool("zjzl_add_bom_balloon", **scope, reference=reference, position_m=(.23, .20),
                     expected_item="1", save=True)
    run_tool("zjzl_add_bom_balloon", expected="balloon_exists", **scope, reference=reference, position_m=(.23, .20))
    n.app.CloseDoc(doc.GetTitle())
    doc = n.document(str(drw), 3)
    draw = n.cast(doc, "IDrawingDoc")
    sheet = n.cast(draw.GetCurrentSheet(), "ISheet")
    view = next(v for v in model_views(n, sheet) if v.GetName2() == scope["view_name"])
    raw = view.GetFirstAnnotation3()
    while raw is not None:
        annotation = n.cast(raw, "IAnnotation")
        if annotation.GetName() == balloon["annotation_name"]:
            note = n.cast(annotation.GetSpecificAnnotation(), "INote")
            assert note.IsBomBalloon() and note.GetBomBalloonTextStyle(True) == 2
            assert note.GetText().strip() == "1" and not annotation.IsDangling()
            assert len(annotation.GetAttachedEntities3()) == 1
            break
        raw = annotation.GetNext3()
    else:
        raise AssertionError("Persisted BOM balloon missing")
    print("PASS assembly mates, duplicate/missing/mismatched BOM and persisted linked balloon", flush=True)


try:
    if args.part is not None:
        part_case()
    if args.assembly is not None:
        assembly_case()
finally:
    docs = [n.cast(raw, "IModelDoc2") for raw in (n.app.GetDocuments() or ())]
    opened = [(doc.GetType(), doc.GetPathName()) for doc in docs]
    for _, path in sorted(opened, reverse=True):
        if path and Path(path).is_relative_to(ROOT):
            doc = n.cast(n.app.GetOpenDocumentByName(path), "IModelDoc2")
            if doc is not None:
                n.app.CloseDoc(doc.GetTitle())
    if original_path and n.app.GetOpenDocumentByName(original_path) is not None:
        n.document(original_path, original_kind)
