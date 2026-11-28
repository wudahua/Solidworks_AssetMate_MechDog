"""Read the active configuration's assembly-level mates.

Why this exists (ZJZL adoption batch 4, 2026-11-28)
--------------------------------------------------
The toolkit could read an assembly tree and its mass properties, but not a single mate. So a
drawing could claim "assembly per model" while the model's mating scheme — the thing that
actually defines the fit — was invisible to any check.

Each mate reports what SolidWorks will answer and NAMES what it would not: a suppressed mate,
an unreadable entity or a missing value lands in `issues` instead of becoming a silent null.
"""
from __future__ import annotations

import math

from .. import sw_session
from .native_com import (
    ComScopeError, Native, execute, optional, path_key, persist, source_path, value_of,
)


def _cast_optional(native, read, interface):
    return optional(lambda: native.cast(read(), interface))


def mate_record(native, feature, extension):
    kind = feature.GetTypeName2()
    # `GetErrorCode2` may be a METHOD that returns a (code, warning) tuple, or a property.
    # Reading the attribute without calling it handed back a bound method, and `error[0]`
    # then raised "'method' object is not subscriptable" (measured 2026-11-28 on a cabinet
    # assembly). Read through `value_of`, then check the shape before subscripting.
    error = optional(lambda: value_of(feature, "GetErrorCode2"))
    if not isinstance(error, (tuple, list)) or len(error) < 2:
        error = None
    record = {
        "name": feature.Name, "feature_type": kind,
        "suppressed": optional(lambda: value_of(feature, "IsSuppressed")),
        "feature_code": error[0] if error is not None else None,
        "feature_warning": error[1] if error is not None else None,
        "mate_type": None, "alignment": None, "flipped": None,
        "value": None, "unit": None, "entities": [], "issues": [],
    }
    issues = record["issues"]
    mate = _cast_optional(native, lambda: value_of(feature, "GetSpecificFeature2"), "IMate2")
    if mate is None:
        issues.append("mate_details_unavailable")
        return record
    record.update(mate_type=optional(lambda: value_of(mate, "Type")),
                  alignment=optional(lambda: value_of(mate, "Alignment")),
                  flipped=optional(lambda: value_of(mate, "Flipped")))
    count = optional(lambda: value_of(mate, "GetMateEntityCount"))
    if count is None or count == 0:
        issues.append("mate_entities_unavailable")
    for index in range(count or 0):
        entity = _cast_optional(native, lambda: value_of(mate, "MateEntity", index), "IMateEntity2")
        if entity is None:
            issues.append("entity_%d_unavailable" % index)
            record["entities"].append({"index": index, "status": "unavailable"})
            continue
        component = _cast_optional(native, lambda: value_of(entity, "ReferenceComponent"),
                                   "IComponent2")
        reference = optional(lambda: value_of(entity, "Reference"))
        record["entities"].append({
            "index": index,
            "entity_type": optional(lambda: value_of(entity, "ReferenceType2")),
            "component": optional(lambda: value_of(component, "Name2")) if component is not None else None,
            "component_path": (optional(lambda: value_of(component, "GetPathName"))
                               if component is not None else None),
            "persistent_id": persist(extension, reference) if reference is not None else None,
            "status": "read" if reference is not None else "unavailable",
        })
        if reference is None:
            issues.append("entity_%d_reference_unavailable" % index)
    # A mate may carry several parameters. Only the distance/angle kinds have one value, and
    # guessing for the others would invent a number.
    if kind in ("MateDistanceDim", "MateAngleDim"):
        display = _cast_optional(native, lambda: value_of(mate, "DisplayDimension"),
                                 "IDisplayDimension")
        dimension = (_cast_optional(native, lambda: display.GetDimension2(0), "IDimension")
                     if display is not None else None)
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
    doc = native.document(path, sw_session.SW_DOC_ASSEMBLY)
    manager = native.cast(value_of(doc, "ConfigurationManager"), "IConfigurationManager")
    configuration = native.cast(value_of(manager, "ActiveConfiguration"), "IConfiguration")
    extension = native.cast(doc.Extension, "IModelDocExtension")
    groups, mates = [], []
    raw = sw_session.call_method(doc, "FirstFeature")
    while raw is not None:
        feature = native.cast(raw, "IFeature")
        if feature is not None and feature.GetTypeName2() == "MateGroup":
            groups.append(feature.Name)
            current = optional(lambda: feature.GetFirstSubFeature())
            while current is not None:
                child = native.cast(current, "IFeature")
                if child is not None and child.GetTypeName2().startswith("Mate"):
                    mates.append(mate_record(native, child, extension))
                current = child.GetNextSubFeature() if child is not None else None
        if feature is None:
            break
        raw = sw_session.call_method(feature, "GetNextFeature")
    return {"assembly_path": path,
            "configuration": (optional(lambda: value_of(configuration, "Name"))
                              if configuration is not None else None),
            "mate_groups": groups, "mates": mates,
            "status": "partial" if any(row["issues"] for row in mates) else "complete"}


def inspect_mates(assembly_path):
    """Active configuration's assembly-level Mate features, with per-field honesty."""
    return execute(_inspect, assembly_path)
