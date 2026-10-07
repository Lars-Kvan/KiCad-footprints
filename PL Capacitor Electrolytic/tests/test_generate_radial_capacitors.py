"""Regression tests for the radial capacitor generator."""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import generate_radial_capacitors as generator


class RadialCapacitorGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = generator.CapacitorSpec.from_values("8.0", "10.5", "3.5")

    def test_name_uses_one_decimal_place(self) -> None:
        self.assertEqual(
            self.spec.name, "CP_Radial_D8.0mm_L10.5mm_P3.5mm"
        )
        whole_numbers = generator.CapacitorSpec.from_values(10, 16, 5)
        self.assertEqual(
            whole_numbers.name, "CP_Radial_D10.0mm_L16.0mm_P5.0mm"
        )

    def test_default_and_custom_pad_dimensions(self) -> None:
        self.assertEqual(self.spec.drill_mm, Decimal("1.0"))
        self.assertEqual(self.spec.pad_diameter_mm, Decimal("2.0"))
        custom = generator.CapacitorSpec.from_values(16, 25, 7.5, 1.2, 2.4)
        self.assertEqual(custom.drill_mm, Decimal("1.2"))
        self.assertEqual(custom.pad_diameter_mm, Decimal("2.4"))

    def test_invalid_dimensions_are_rejected(self) -> None:
        with self.assertRaises(generator.SpecificationError):
            generator.CapacitorSpec.from_values(8, 10.5, 3.5, 2.0, 2.0)
        with self.assertRaises(generator.SpecificationError):
            generator.CapacitorSpec.from_values(8, 10.5, 2.0, 1.0, 2.0)
        with self.assertRaises(generator.SpecificationError):
            generator.CapacitorSpec.from_values(8.05, 10.5, 3.5)

    def test_generated_assets_have_expected_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            outputs = generator.generate_specs([self.spec], output_root)
            footprint_path, model_path = outputs[0]
            footprint = footprint_path.read_text(encoding="utf-8")
            model = model_path.read_text(encoding="utf-8")

        self.assertEqual(footprint.count("("), footprint.count(")"))
        self.assertIn('(footprint "CP_Radial_D8.0mm_L10.5mm_P3.5mm"', footprint)
        self.assertIn('(at -1.75 0.0)', footprint)
        self.assertIn('(at 1.75 0.0)', footprint)
        self.assertIn('CP_Radial_D8.0mm_L10.5mm_P3.5mm.wrl', footprint)
        self.assertIn("#VRML V2.0 utf8", model)
        self.assertIn("Top rolled metal rim", model)
        self.assertIn("Stamped vent horizontal groove", model)
        self.assertIn("Polarity stripe", model)

    def test_sample_csv_generates_the_selected_test_footprint(self) -> None:
        sample_csv = Path(__file__).parents[1] / "radial_capacitor_specs.csv"
        specifications = generator.read_csv_specs(sample_csv)
        self.assertEqual([spec.name for spec in specifications], [self.spec.name])

    def test_existing_outputs_require_explicit_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory)
            generator.generate_specs([self.spec], output_root)
            with self.assertRaises(FileExistsError):
                generator.generate_specs([self.spec], output_root)
            generator.generate_specs([self.spec], output_root, overwrite=True)


if __name__ == "__main__":
    unittest.main()
