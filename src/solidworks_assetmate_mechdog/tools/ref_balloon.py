"""BOM-linked balloons: the item number comes from the BOM row, not from the caller.

Why this exists (ZJZL adoption batch 4, 2026-11-28)
--------------------------------------------------
`add_balloon` (v0.8.364) can place a native balloon, but it cannot tell you what number the
balloon SHOULD carry: with no BOM table the text reads `?`, and the caller has to supply the
item. That is fine for a one-off, wrong for a real assembly drawing — the item number is a
property of the BOM row, and it can change.

This module resolves the number first and then verifies the balloon against it:

    ITableAnnotation.Type == 2 (swTableAnnotation_BillOfMaterials)
      -> IBomTableAnnotation.BomFeature -> IFeature.Name  (the view's linked BOM, if any)
      -> GetComponents2(row, configuration) must contain exactly this occurrence
      -> GetComponentsCount2(row, configuration) must agree, and the row must be visible
      -> the row must be UNIQUE for this occurrence

Only then is the balloon inserted (`IModelDocExtension.InsertBOMBalloon2(options)`), and it
is checked with `INote.IsBomBalloon`, the item text, dangling/visibility, the edge attachment
and the position. A failed check deletes the balloon instead of leaving a wrong number on
the sheet.
"""
from __future__ import annotations

import math

from .. import sw_session
from .native_com import (
    ComScopeError, Native, activate_sheet, drawing_scope, execute, named_view, optional,
    path_key, rollback_annotation, save_drawing, sheet_point, source_path, value_of,
)
from .ref_dimension import attachments_match
from .ref_geometry import reference_from, resolve_visible, visible_components


def bom_row_for(native, drawing, sheet_name, view, owner):
    """(row index, item text, bom feature name, bom sheet) for exactly one visible row."""
    configuration = view.ReferencedConfiguration
    linked = (value_of(view, "GetKeepLinkedToBOMName")
              if optional(lambda: value_of(view, "GetKeepLinkedToBOM")) else None)
    model_path = path_key(view.GetReferencedModelName())
    tables = []
    for views in (drawing.GetViews() or ()):
        if not views:
            continue
        surface = native.cast(views[0], "IView")
        bom_sheet = value_of(surface, "GetName2")
        if linked is None and bom_sheet != sheet_name:
            continue
        for raw in (value_of(surface, "GetTableAnnotations") or ()):
            table = native.cast(raw, "ITableAnnotation")
            if table is None or value_of(table, "Type") != 2:
                continue
            bom = native.cast(raw, "IBomTableAnnotation")
            bom_feature = native.cast(value_of(bom, "BomFeature"), "IBomFeature")
            if bom_feature is None:
                continue
            feature = native.cast(value_of(bom_feature, "GetFeature"), "IFeature")
            if feature is None:
                continue
            if linked is not None and feature.Name != linked:
                continue
            if path_key(value_of(bom_feature, "GetReferencedModelName")) == model_path:
                tables.append((table, bom, feature.Name, bom_sheet))
    if len(tables) != 1:
        raise ComScopeError(
            "bom_scope_unavailable",
            "Expected one linked BOM table or one same-sheet BOM for this assembly")
    table, bom, feature_name, bom_sheet = tables[0]
    rows = []
    for index in range(value_of(table, "RowCount")):
        components = [native.cast(raw, "IComponent2")
                      for raw in (value_of(bom, "GetComponents2", index, configuration) or ())]
        if not any(native.app.IsSame(candidate, owner) == 1 for candidate in components):
            continue
        count, item_number, _ = value_of(bom, "GetComponentsCount2", index, configuration)
        if count != len(components) or not item_number or value_of(table, "RowHidden", index):
            raise ComScopeError("bom_row_invalid",
                                "Could not verify a visible BOM row and its item number")
        rows.append((index, str(item_number).strip()))
    if len(rows) != 1:
        raise ComScopeError("bom_row_not_unique",
                            "The exact occurrence must appear in one visible BOM row")
    row, item = rows[0]
    return row, item, feature_name, bom_sheet


def existing_balloon(native, view, occurrence):
    """Is there already a BOM balloon pointing at this occurrence in the view?"""
    raw = optional(lambda: value_of(view, "GetFirstAnnotation3"))
    while raw is not None:
        annotation = native.cast(raw, "IAnnotation")
        if annotation is None:
            return False
        if value_of(annotation, "GetType") == 6:  # swNote
            note = native.cast(value_of(annotation, "GetSpecificAnnotation"), "INote")
            if note is not None and optional(lambda: value_of(note, "IsBomBalloon")):
                attached = annotation.GetAttachedEntities3() or ()
                if len(attached) == 1:
                    entity = native.cast(attached[0], "IEntity")
                    component = optional(lambda: value_of(entity, "GetComponent"))
                    if component is not None and native.app.IsSame(component, occurrence) == 1:
                        return True
        raw = optional(lambda: value_of(annotation, "GetNext3"))
    return False


def _add(drawing_path, view_name, reference, position_m, expected_item, sheet_name, save):
    ref = reference_from(reference)
    if ref.kind != "edge" or ref.component is None:
        raise ComScopeError("bad_input",
                            "Select a scoped edge reference from an assembly occurrence")
    path = source_path(drawing_path, (".slddrw",))
    native = Native()
    doc, drawing, sheet, extension = drawing_scope(native, path, sheet_name)
    view = named_view(native, sheet, view_name)
    sheet_name = sheet.GetName()
    point = sheet_point(sheet, position_m)
    components = visible_components(native, view)
    entity = resolve_visible(native, extension, view, components, ref, path,
                             sheet_name, view_name)
    occurrence, owner = components[ref.component]
    row, item, bom_feature, bom_sheet = bom_row_for(native, drawing, sheet_name, view, owner)
    if expected_item is not None and str(expected_item).strip() != item:
        raise ComScopeError("bom_item_mismatch",
                            "Native BOM item is %r, expected %r" % (item, expected_item))
    if existing_balloon(native, view, occurrence):
        raise ComScopeError("balloon_exists",
                            "A native BOM balloon already points to this occurrence")

    options = native.cast(value_of(extension, "CreateBalloonOptions"), "IBalloonOptions")
    if options is None:
        raise ComScopeError("balloon_options_unavailable",
                            "SolidWorks did not provide native BOM balloon options")
    options.Style = 1          # swBS_Circular
    options.UpperTextContent = 1   # swBalloonTextItemNumber

    activate_sheet(native, drawing, sheet)
    doc.ClearSelection2(True)
    annotation = None
    try:
        if not drawing.ActivateView(view_name) or not view.SelectEntity(entity, False):
            raise ComScopeError("entity_selection_failed",
                                "Could not select the occurrence edge in the target view")
        raw = value_of(extension, "InsertBOMBalloon2", options)
        if raw is None:
            raise ComScopeError("balloon_creation_failed",
                                "SolidWorks did not create a native BOM balloon")
        note = native.cast(raw, "INote")
        annotation = native.cast(sw_session.call_method(note, "GetAnnotation"), "IAnnotation")
        if annotation is None or not sw_session.call_method(annotation, "SetPosition2", *point):
            raise ComScopeError("balloon_position_failed",
                                "SolidWorks did not place the balloon")
        actual = [round(float(v), 6) for v in annotation.GetPosition()[:2]]
        native_item = str(value_of(note, "GetText")).strip()
        checks = {
            "native_bom_balloon": bool(optional(lambda: value_of(note, "IsBomBalloon"))),
            # INote uses swDetailingNoteTextContent_e (2 = item number), unlike the options
            # object which uses swBalloonTextContent_e (1 = item number).
            "linked_item_text": value_of(note, "GetBomBalloonTextStyle", True) == 2,
            "item_number": native_item == item,
            "not_dangling": not annotation.IsDangling(),
            "visible": value_of(annotation, "Visible") == 1,
            "edge_attachment": attachments_match(native, annotation, [entity]),
            "position": math.dist(actual, point[:2]) <= 0.001,
        }
        failed = [key for key, passed in checks.items() if not passed]
        if failed:
            raise ComScopeError("balloon_verification_failed",
                                "BOM item %r failed native readback: %s"
                                % (item, ", ".join(failed)))
        record = {"drawing_path": path, "sheet": sheet_name, "view": view_name,
                  "component": ref.component, "bom_row": row, "bom_feature": bom_feature,
                  "bom_sheet": bom_sheet, "item_number": native_item,
                  "annotation_name": annotation.GetName(), "position_m": actual,
                  "native_bom_balloon": True, "readback_verified": True,
                  "checks": checks, "saved": bool(save)}
        if save:
            record["save_warnings"] = save_drawing(doc)
        return record
    except Exception as exc:
        if annotation is not None:
            rollback_annotation(doc, extension, annotation, exc)
        raise
    finally:
        doc.ClearSelection2(True)


def add_bom_balloon(drawing_path, view_name, reference, position_m, expected_item=None,
                    sheet=None, save=False):
    """A native balloon whose item number is verified against the BOM row it came from."""
    return execute(_add, drawing_path, view_name, reference, position_m, expected_item,
                   sheet, save)


def resolve_reference(payload):
    """Public alias so callers do not import the pydantic model directly."""
    return reference_from(payload)
