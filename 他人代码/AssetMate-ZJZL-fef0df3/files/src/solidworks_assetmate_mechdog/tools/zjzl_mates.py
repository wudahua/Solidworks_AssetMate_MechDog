"""Read the active configuration's assembly-level Mate features."""
from __future__ import annotations

import math

from ..sw_session import call_method
from .zjzl_native import Native, execute, optional, persist, source_path


def _cast_optional(native, read, interface):
    return optional(lambda: native.cast(read(), interface))


def _mate_record(native, feature, extension):
    kind = feature.GetTypeName2()
    suppressed = optional(feature.IsSuppressed)
    feature_error = optional(feature.GetErrorCode2)
    record = {"name": feature.Name, "feature_type": kind, "suppressed": suppressed,
              "feature_code": feature_error[0] if feature_error is not None else None,
              "feature_warning": feature_error[1] if feature_error is not None else None,
              "mate_type": None, "alignment": None, "flipped": None, "value": None,
              "unit": None, "entities": [], "issues": []}
    issues = record["issues"]
    mate = _cast_optional(native, feature.GetSpecificFeature2, "IMate2")
    if mate is None:
        issues.append("mate_details_unavailable")
        return record
    record.update(mate_type=optional(lambda: mate.Type), alignment=optional(lambda: mate.Alignment),
                  flipped=optional(lambda: mate.Flipped))
    count = optional(mate.GetMateEntityCount)
    if count is None or count == 0:
        issues.append("mate_entities_unavailable")
    for index in range(count or 0):
        entity = _cast_optional(native, lambda: mate.MateEntity(index), "IMateEntity2")
        if entity is None:
            issues.append(f"entity_{index}_unavailable")
            record["entities"].append({"index": index, "status": "unavailable"})
            continue
        component = _cast_optional(native, lambda: entity.ReferenceComponent, "IComponent2")
        reference = optional(lambda: entity.Reference)
        record["entities"].append({
            "index": index, "entity_type": optional(lambda: entity.ReferenceType2),
            "component": optional(lambda: component.Name2) if component is not None else None,
            "component_path": optional(component.GetPathName) if component is not None else None,
            "persistent_id": persist(extension, reference) if reference is not None else None,
            "status": "read" if reference is not None else "unavailable",
        })
        if reference is None:
            issues.append(f"entity_{index}_reference_unavailable")
    # Other mate types can carry multiple parameters. Avoid treating one as their value.
    if kind in ("MateDistanceDim", "MateAngleDim"):
        display = _cast_optional(native, lambda: mate.DisplayDimension, "IDisplayDimension")
        dimension = _cast_optional(native, lambda: display.GetDimension2(0), "IDimension") if display is not None else None
        value = optional(lambda: dimension.SystemValue) if dimension is not None else None
        if value is None or not math.isfinite(value):
            issues.append("value_unavailable")
        elif kind == "MateDistanceDim":
            record.update(value=value * 1000, unit="mm")
        else:
            record.update(value=math.degrees(value), unit="deg")
    return record


def _inspect(assembly_path):
    path = source_path(assembly_path, (".sldasm",))
    native = Native()
    doc = native.document(path, 2)
    manager = native.cast(doc.ConfigurationManager, "IConfigurationManager")
    configuration = native.cast(manager.ActiveConfiguration, "IConfiguration")
    extension = native.cast(doc.Extension, "IModelDocExtension")
    groups, mates = [], []
    raw = call_method(doc, "FirstFeature")
    while raw is not None:
        feature = native.cast(raw, "IFeature")
        if feature.GetTypeName2() == "MateGroup":
            groups.append(feature.Name)
            current = feature.GetFirstSubFeature()
            while current is not None:
                child = native.cast(current, "IFeature")
                if child.GetTypeName2().startswith("Mate"):
                    mates.append(_mate_record(native, child, extension))
                current = child.GetNextSubFeature()
        raw = call_method(feature, "GetNextFeature")
    return {"assembly_path": path, "configuration": configuration.Name, "mate_groups": groups,
            "mates": mates, "status": "partial" if any(row["issues"] for row in mates) else "complete"}


def inspect_mates(assembly_path):
    return execute(_inspect, assembly_path)
