"""Replacement for tools/layout.py::add_bom_table.

Integration:
1. Copy bom_native.py into the same tools package directory.
2. Replace the old add_bom_table function with this one (ensure no duplicate defs).
3. Restart the MCP server after replacing the file.
"""
from __future__ import annotations

import os

from .bom_native import default_template, populate_top_level_bom


def add_bom_table(drawing_path, view_name=None, configuration="Default",
                  template=None, save=True):
    """Insert a populated native TopLevelOnly BOM into an existing assembly drawing."""
    try:
        from ..sw_session import error, get_sw
        from .drawing import _open_drawing, _iter_views, _save_drawing, drawing_ok

        doc = _open_drawing(drawing_path)
        sw = get_sw()
        template = template or default_template(sw)
        if not os.path.exists(template):
            return error("template_missing", "BOM template not found: %s" % template)

        views = [v for v in _iter_views(doc) if getattr(v, "Type", None) != 1]
        if not views:
            return error("no_views", "drawing has no model views")
        target = views[0]
        if view_name:
            target = next((v for v in views if v.Name == view_name), None)
            if target is None:
                return error("view_not_found", "no view named %r" % view_name)

        # Indented needs a valid configuration to populate; TopLevelOnly is assigned
        # only after rows exist (see bom_native.populate_top_level_bom).
        configuration = configuration or getattr(target, "ReferencedConfiguration", "Default")

        result = populate_top_level_bom(
            doc, target, template,
            configuration=configuration or "Default",
        )
        if save:
            _save_drawing(doc)

        return drawing_ok(
            drawing=doc.GetTitle,
            created=True,
            native_bom=True,
            populated=True,
            rows=result["rows"],
            columns=result["columns"],
            data_rows=result["data_rows"],
            headers=result["headers"],
            table_type=result["table_type"],
            referenced_model=result["referenced_model"],
            sample_data=result["data"][:3],
            saved=bool(save),
        )
    except (FileNotFoundError, ValueError) as exc:
        from ..sw_session import error
        return error("bad_input", str(exc))
    except Exception as exc:
        from ..sw_session import error
        return error("bom_table_failed", str(exc))
