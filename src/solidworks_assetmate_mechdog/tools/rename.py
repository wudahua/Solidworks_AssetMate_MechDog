"""Component / part renaming.

Two very different operations, with different levels of API support:

1. File rename (mode="file") — implemented with the filesystem. SolidWorks'
   Extension.RenameDocument returns 2 and does nothing outside a PDM workspace,
   so the OS rename is the only reliable path. Every rename is dry-run first,
   refuses to overwrite, and rolls back on failure.

2. Instance rename (mode="instance") — NOT supported by the API on this build.
   IComponent2.Name is read-only ("Property can not be set") and Name2 silently
   ignores assignment. SolidWorks only exposes this through the FeatureManager UI.
   Returning a fake success would be worse than saying so.

Renaming a file breaks assembly references that point at the old name; pass
assembly_path to get a report of affected components before applying.
"""
from __future__ import annotations

import os

from ..sw_session import (
    SW_DOC_ASSEMBLY,
    error,
    get_components,
    ok,
    open_document,
)

ALLOWED_EXT = {".sldprt", ".sldasm", ".slddrw"}
ILLEGAL = set('<>:"/\\|?*')


def _target_name(path, mapping, prefix, suffix):
    base = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1]
    new_base = mapping.get(base, mapping.get(os.path.basename(path), base)) if mapping else base
    new_base = f"{prefix or ''}{new_base}{suffix or ''}"
    return os.path.join(os.path.dirname(path), new_base + ext)


def _reference_report(assembly_path, old_names):
    """Which components in the assembly reference the files being renamed."""
    doc = open_document(assembly_path, SW_DOC_ASSEMBLY)
    affected = []
    for comp in get_components(doc):
        try:
            current = comp.GetPathName
        except Exception:
            continue
        if current and os.path.basename(current).lower() in old_names:
            affected.append({"component": comp.Name2, "references": current})
    return affected


def rename_components(
    part_paths=None,
    mapping=None,
    prefix=None,
    suffix=None,
    apply=False,
    assembly_path=None,
    mode="file",
):
    """Rename part/assembly FILES by mapping or prefix/suffix. Dry-run by default.

    mode="instance" reports that the SolidWorks API cannot rename component
    instances (UI-only operation) instead of pretending it worked.
    """
    if mode != "file":
        return error(
            "instance_rename_unsupported",
            "SolidWorks exposes no writable API for component instance names on this build: "
            "IComponent2.Name is read-only and Name2 ignores assignment.",
            "Rename instances in the FeatureManager tree (slow double-click), or use "
            "mode='file' to rename the underlying part files.",
        )

    if not part_paths:
        return error("bad_input", "part_paths is required, e.g. ['D:/parts/P01.SLDPRT']")
    if not mapping and not prefix and not suffix:
        return error("bad_input", "Provide mapping, prefix, or suffix — otherwise nothing changes")

    try:
        changes, conflicts = [], []
        for raw in part_paths:
            path = os.path.abspath(raw)
            if not os.path.exists(path):
                conflicts.append({"path": path, "reason": "not_found"})
                continue
            if os.path.splitext(path)[1].lower() not in ALLOWED_EXT:
                conflicts.append({"path": path, "reason": "unsupported_extension"})
                continue
            target = _target_name(path, mapping or {}, prefix, suffix)
            if os.path.normcase(target) == os.path.normcase(path):
                conflicts.append({"path": path, "reason": "name_unchanged"})
                continue
            if any(ch in ILLEGAL for ch in os.path.basename(target)):
                conflicts.append({"path": path, "reason": "illegal_characters", "target": target})
                continue
            if os.path.exists(target):
                conflicts.append({"path": path, "reason": "target_exists", "target": target})
                continue
            changes.append({"old": path, "new": target})

        result = {
            "mode": "file",
            "planned": changes,
            "conflicts": conflicts,
            "applied": False,
        }
        if assembly_path and changes:
            old_names = {os.path.basename(c["old"]).lower() for c in changes}
            try:
                result["affected_components"] = _reference_report(assembly_path, old_names)
            except Exception as exc:
                result["reference_check_error"] = str(exc)

        if not apply:
            result["dry_run"] = True
            result["message"] = "Nothing renamed. apply=true performs the file renames."
            return ok(**result)

        done = []
        try:
            for change in changes:
                os.rename(change["old"], change["new"])
                done.append(change)
        except Exception as exc:
            for change in reversed(done):
                try:
                    os.rename(change["new"], change["old"])
                except Exception:
                    pass
            return error(
                "rename_failed",
                "%s; rolled back %d completed rename(s)" % (exc, len(done)),
                "Close the file in SolidWorks first — an open document cannot be renamed.",
            )

        result.update(applied=True, renamed=done)
        if result.get("affected_components"):
            result["warning"] = (
                "Renamed files are referenced by %d component(s); reopen the assembly or "
                "re-point the references, otherwise it will show missing components."
                % len(result["affected_components"])
            )
        return ok(**result)
    except Exception as exc:
        return error("rename_failed", str(exc))
