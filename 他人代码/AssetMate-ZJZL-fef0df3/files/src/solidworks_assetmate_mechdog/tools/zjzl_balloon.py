"""Insert a native BOM-linked balloon onto an exact visible occurrence edge."""
from __future__ import annotations

import math

from ..sw_session import call_method
from .zjzl_geometry import resolve_visible, visible_components
from .zjzl_native import (
    Native, ZJZLError, activate_sheet, drawing_scope, execute, named_view, path_key,
    rollback_annotation, save_drawing, sheet_point, source_path,
)
from .zjzl_reference_dimension import attachments_match


def _row_for_component(native, drawing, sheet_name, view, component):
    configuration = view.ReferencedConfiguration
    linked_name = view.GetKeepLinkedToBOMName() if view.GetKeepLinkedToBOM() else None
    model_path = path_key(view.GetReferencedModelName())
    tables = []
    for views in drawing.GetViews() or ():
        if not views:
            continue
        surface = native.cast(views[0], "IView")
        bom_sheet = surface.GetName2()
        if linked_name is None and bom_sheet != sheet_name:
            continue
        for raw in surface.GetTableAnnotations() or ():
            table = native.cast(raw, "ITableAnnotation")
            if table.Type != 2:  # swTableAnnotation_BillOfMaterials
                continue
            bom = native.cast(raw, "IBomTableAnnotation")
            bom_feature = native.cast(bom.BomFeature, "IBomFeature")
            feature = native.cast(bom_feature.GetFeature(), "IFeature")
            if linked_name is not None and feature.Name != linked_name:
                continue
            if path_key(bom_feature.GetReferencedModelName()) == model_path:
                tables.append((table, bom, feature.Name, bom_sheet))
    if len(tables) != 1:
        raise ZJZLError("bom_scope_unavailable", "Expected one linked BOM table or one same-sheet BOM for the referenced assembly")
    table, bom, feature_name, bom_sheet = tables[0]
    rows = []
    for index in range(table.RowCount):
        components = [native.cast(raw, "IComponent2") for raw in bom.GetComponents2(index, configuration) or ()]
        if not any(native.app.IsSame(owner, component) == 1 for owner in components):
            continue
        count, item_number, _ = bom.GetComponentsCount2(index, configuration)
        if count != len(components) or not item_number or table.RowHidden(index):
            raise ZJZLError("bom_row_invalid", "Could not verify a visible BOM row and its item number")
        rows.append((index, item_number.strip()))
    if len(rows) != 1:
        raise ZJZLError("bom_row_not_unique", "The exact occurrence must appear in one visible BOM row")
    row, item = rows[0]
    return row, item, feature_name, bom_sheet


def _existing_balloon(native, view, occurrence):
    raw = view.GetFirstAnnotation3()
    while raw is not None:
        annotation = native.cast(raw, "IAnnotation")
        if annotation.GetType() == 6:  # swNote
            note = native.cast(annotation.GetSpecificAnnotation(), "INote")
            if note is not None and note.IsBomBalloon():
                attached = annotation.GetAttachedEntities3() or ()
                if len(attached) == 1:
                    entity = native.cast(attached[0], "IEntity")
                    component = entity.GetComponent()
                    if component is not None and native.app.IsSame(component, occurrence) == 1:
                        return True
        raw = annotation.GetNext3()
    return False


def _add(drawing_path, view_name, reference, position_m, expected_item, sheet_name, save):
    if reference.kind != "edge" or reference.component is None:
        raise ZJZLError("bad_input", "Select a scoped edge reference from an assembly occurrence")
    path = source_path(drawing_path, (".slddrw",))
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    view = named_view(native, sheet, view_name)
    sheet_name = sheet.GetName()
    point = sheet_point(sheet, position_m)
    components = visible_components(native, view)
    entity = resolve_visible(native, extension, view, components, reference, path, sheet_name, view_name)
    occurrence, owner = components[reference.component]
    row, item, bom_feature, bom_sheet = _row_for_component(native, drawing, sheet_name, view, owner)
    if expected_item is not None and expected_item.strip() != item:
        raise ZJZLError("bom_item_mismatch", f"Native BOM item is {item!r}, expected {expected_item!r}")
    if _existing_balloon(native, view, occurrence):
        raise ZJZLError("balloon_exists", "A native BOM balloon already points to this occurrence in the view")
    options = native.cast(extension.CreateBalloonOptions(), "IBalloonOptions")
    if options is None:
        raise ZJZLError("balloon_options_unavailable", "SolidWorks did not provide native BOM balloon options")
    options.Style = 1  # swBS_Circular
    options.UpperTextContent = 1  # swBalloonTextItemNumber

    activate_sheet(native, drawing, sheet)
    doc.ClearSelection2(True)
    annotation = None
    try:
        if not drawing.ActivateView(view_name) or not view.SelectEntity(entity, False):
            raise ZJZLError("entity_selection_failed", "Could not select the occurrence edge in the target view")
        raw = extension.InsertBOMBalloon2(options)
        if raw is None:
            raise ZJZLError("balloon_creation_failed", "SolidWorks did not create a native BOM balloon")
        note = native.cast(raw, "INote")
        annotation = native.cast(call_method(note, "GetAnnotation"), "IAnnotation")
        if not call_method(annotation, "SetPosition2", *point):
            raise ZJZLError("balloon_position_failed", "SolidWorks did not place the balloon")
        actual_position = list(annotation.GetPosition()[:2])
        native_item = note.GetText().strip()
        checks = {"native_bom_balloon": note.IsBomBalloon(),
                  # INote uses swDetailingNoteTextContent_e, unlike IBalloonOptions.
                  "linked_item_text": note.GetBomBalloonTextStyle(True) == 2,
                  "item_number": native_item == item, "not_dangling": not annotation.IsDangling(),
                  "visible": annotation.Visible == 1, "edge_attachment": attachments_match(native, annotation, [entity]),
                  "position": math.dist(actual_position, point[:2]) <= 0.001}
        failed = [key for key, passed in checks.items() if not passed]
        if failed:
            raise ZJZLError("balloon_verification_failed", f"BOM item {item!r} failed native readback: {', '.join(failed)}")
        record = {"drawing_path": path, "sheet": sheet_name, "view": view_name,
                  "component": reference.component, "bom_row": row, "bom_feature": bom_feature,
                  "bom_sheet": bom_sheet, "item_number": native_item,
                  "annotation_name": annotation.GetName(), "position_m": actual_position,
                  "native_bom_balloon": True, "readback_verified": True}
        warnings = save_drawing(doc) if save else None
        return {**record, "saved": save, "save_warnings": warnings}
    except Exception as exc:
        if annotation is not None:
            rollback_annotation(doc, extension, annotation, exc)
        raise
    finally:
        doc.ClearSelection2(True)


def add_bom_balloon(drawing_path, view_name, reference, position_m, expected_item, sheet, save):
    return execute(_add, drawing_path, view_name, reference, position_m, expected_item, sheet, save)
