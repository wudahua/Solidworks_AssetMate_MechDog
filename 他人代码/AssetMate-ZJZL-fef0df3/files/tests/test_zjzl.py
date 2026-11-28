"""ZJZL offline tests. Run with python tests/test_zjzl.py."""
import asyncio
import math
import os
import unittest
from unittest.mock import Mock, patch

from solidworks_assetmate_mechdog import server
from solidworks_assetmate_mechdog.tools import (
    capabilities, zjzl_balloon, zjzl_custom_view, zjzl_geometry, zjzl_mates, zjzl_orthographic,
)
from solidworks_assetmate_mechdog.tools.zjzl_native import ZJZLError, read_reference


class ZJZLTests(unittest.TestCase):
    def test_projection_positions_follow_sheet_convention(self):
        sides = ("top", "right", "bottom", "left")
        first = zjzl_orthographic.projection_positions((0.20, 0.15), sides, 0.05, True)
        third = zjzl_orthographic.projection_positions((0.20, 0.15), sides, 0.05, False)
        self.assertAlmostEqual(first["top"][1], 0.10)
        self.assertAlmostEqual(first["right"][0], 0.15)
        self.assertAlmostEqual(third["top"][1], 0.20)
        self.assertAlmostEqual(third["right"][0], 0.25)
        self.assertEqual(first["top"], third["bottom"])
        self.assertEqual(first["right"], third["left"])

    def test_projection_rejects_repeated_sides_or_nonfinite_offsets(self):
        for sides, offset in [(("top", "top"), 0.05), (("rear",), 0.05), ((), 0.05), (("top",), math.nan)]:
            with self.subTest(sides=sides, offset=offset), self.assertRaises(ZJZLError):
                zjzl_orthographic.projection_positions((0.20, 0.15), sides, offset, True)

    def test_view_batch_rolls_back_in_reverse_creation_order(self):
        native, doc, drawing, sheet, ext = (Mock() for _ in range(5))
        native.cast.side_effect = lambda entity, _: entity
        first, second = Mock(), Mock()
        first.GetName2.return_value, second.GetName2.return_value = "First", "Second"
        ext.DeleteSelection2.return_value = True
        with patch.object(zjzl_orthographic, "activate_sheet"), patch.object(
            zjzl_orthographic, "select_view", return_value=True
        ) as select, patch.object(zjzl_orthographic, "model_views", return_value=[]):
            with self.assertRaisesRegex(ValueError, "failed"):
                with zjzl_orthographic.view_changes(native, "drawing", doc, drawing, sheet, ext) as created:
                    created.extend([first, second])
                    raise ValueError("failed")
        self.assertEqual([call.args[1] for call in select.call_args_list], ["Second", "First"])
        self.assertEqual(ext.DeleteSelection2.call_count, 2)

    def test_rotation_orthogonalizes_up_and_keeps_handedness(self):
        matrix = zjzl_custom_view.rotation((3, 0, 4), (0, 1, 0.5))
        axes = [zjzl_orthographic.axis_vector(matrix, axis) for axis in range(3)]
        self.assertEqual(axes[2], (0.6, 0.0, 0.8))
        for axis in axes:
            self.assertAlmostEqual(math.hypot(*axis), 1)
        self.assertAlmostEqual(sum(a * b for a, b in zip(axes[1], axes[2])), 0)
        for actual, expected in zip(zjzl_custom_view._cross(*axes[:2]), axes[2]):
            self.assertAlmostEqual(actual, expected)

    def test_orientation_rejects_degenerate_vectors(self):
        for normal, up in [((0, 0, 0), (0, 1, 0)), ((0, 0, 1), (0, 0, 2)), ((0, 1), (0, 0, 1)),
                           ((math.inf, 0, 1), (0, 1, 0))]:
            with self.subTest(normal=normal, up=up), self.assertRaises(ZJZLError):
                zjzl_custom_view.rotation(normal, up)

    def test_geometry_resolves_only_in_exact_view_scope(self):
        path = os.path.abspath("test.SLDDRW")
        reference = zjzl_geometry.GeometryReference(drawing_path=path, sheet="S", view="V", kind="edge", id="AQI=")
        visible, raw = object(), object()
        native, ext, view = Mock(), Mock(), Mock()
        ext.GetObjectByPersistReference3.return_value = raw, 0
        native.app.IsSame.return_value = 1
        view.GetVisibleEntities2.return_value = [visible]
        components = {None: (None, None)}
        with self.assertRaisesRegex(ZJZLError, "different drawing"):
            zjzl_geometry.resolve_visible(native, ext, view, components, reference, path, "Wrong", "V")
        ext.GetObjectByPersistReference3.assert_not_called()
        self.assertIs(zjzl_geometry.resolve_visible(native, ext, view, components, reference, path, "S", "V"), visible)
        ext.GetObjectByPersistReference3.assert_called_once_with(bytes([1, 2]))
        native.app.IsSame.return_value = 0
        with self.assertRaisesRegex(ZJZLError, "no longer visible"):
            zjzl_geometry.resolve_visible(native, ext, view, components, reference, path, "S", "V")
        ext.GetObjectByPersistReference3.return_value = None, 3
        with self.assertRaisesRegex(ZJZLError, "state 3"):
            zjzl_geometry.resolve_visible(native, ext, view, components, reference, path, "S", "V")

    def test_reference_decoder_rejects_invalid_data(self):
        for identifier in ("", "A!!=", "x" * 65_537):
            with self.subTest(size=len(identifier)), self.assertRaises(ZJZLError):
                read_reference(identifier)

    def test_projected_geometry_uses_native_scale_and_translation(self):
        matrix = (0, -1, 0, 1, 0, 0, 0, 0, 1, .1, .2, 0, 2)
        actual = zjzl_geometry.point_through(matrix, (.01, .02, .03))
        for point, expected in zip(actual, (.14, .18, .06)):
            self.assertAlmostEqual(point, expected)

    def test_suppressed_mate_is_reported_when_native_details_are_unavailable(self):
        feature = Mock(Name="SuppressedMate")
        feature.GetTypeName2.return_value = "MateDistanceDim"
        feature.IsSuppressed.return_value = True
        feature.GetErrorCode2.return_value = (0, False)
        feature.GetSpecificFeature2.return_value = None
        native = Mock()
        native.cast.side_effect = lambda entity, _: entity
        with patch.object(zjzl_mates, "optional", side_effect=lambda call: call()):
            mate = zjzl_mates._mate_record(native, feature, Mock())
        self.assertTrue(mate["suppressed"])
        self.assertIsNone(mate["value"])
        self.assertEqual(mate["issues"], ["mate_details_unavailable"])

    def test_bom_resolves_exact_occurrence_and_refuses_hidden_row(self):
        native, drawing, view, surface, raw, table, bom, feature = (Mock() for _ in range(8))
        first, other = object(), object()
        model_path = os.path.abspath("test.SLDASM")
        native.app.IsSame.side_effect = lambda a, b: int(a is b)
        conversions = {"IView": surface, "ITableAnnotation": table,
                       "IBomTableAnnotation": bom, "IBomFeature": feature, "IFeature": feature}
        native.cast.side_effect = lambda value, kind: conversions.get(kind, value)
        drawing.GetViews.return_value = [(surface, view)]
        view.GetKeepLinkedToBOM.return_value = False
        view.GetReferencedModelName.return_value = model_path
        view.ReferencedConfiguration = "Default"
        surface.GetName2.return_value = "S"
        surface.GetTableAnnotations.return_value = [raw]
        table.Type, table.RowCount = 2, 2
        table.RowHidden.return_value = False
        bom.GetComponents2.side_effect = lambda row, _: (other,) if row == 0 else (first,)
        bom.GetComponentsCount2.return_value = (1, "2", "part number")
        feature.GetReferencedModelName.return_value = model_path
        feature.Name = "BOM"
        self.assertEqual(zjzl_balloon._row_for_component(native, drawing, "S", view, first), (1, "2", "BOM", "S"))
        table.RowHidden.return_value = True
        with self.assertRaisesRegex(ZJZLError, "visible BOM row"):
            zjzl_balloon._row_for_component(native, drawing, "S", view, first)

    def test_all_tools_are_registered_and_capability_discoverable(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        names = {"zjzl_create_orthographic_views", "zjzl_create_custom_view", "zjzl_probe_geometry",
                 "zjzl_add_reference_dimension", "zjzl_inspect_mates", "zjzl_add_bom_balloon"}
        self.assertEqual({name for name in tools if name.startswith("zjzl_")}, names)
        with patch("solidworks_assetmate_mechdog.sw_session.sw_version", return_value=None):
            for name in names:
                self.assertEqual(capabilities.check_capabilities(name)["checked"], 1)
                schema = tools[name].inputSchema
                self.assertFalse(schema["additionalProperties"])
                if "save" in schema["properties"]:
                    self.assertFalse(schema["properties"]["save"]["default"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
