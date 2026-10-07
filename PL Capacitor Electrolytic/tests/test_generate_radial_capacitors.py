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
        self.assertIn('(generator "radial_capacitor_generator")', footprint)
        self.assertIn('${PL_FOOTPRINT_DIR}/PL Capacitor Electrolytic', footprint)
        self.assertIn('(start -4.4 -2.4)', footprint)
        self.assertIn('(end -3.6 -2.4)', footprint)
        self.assertGreater(len(generator._hatch_segments(self.spec)), 10)
        self.assertTrue(
            any(
                abs((end[0] * end[0] + end[1] * end[1]) ** 0.5 - 4.12) < 0.001
                for _, end in generator._hatch_segments(self.spec)
            )
        )
        self.assertIn('CP_Radial_D8.0mm_L10.5mm_P3.5mm.wrl', footprint)
        self.assertIn("#VRML V2.0 utf8", model)
        self.assertIn("scale 1.0 1.0 1.0", model)
        self.assertIn(
            "Nominal body envelope: D=8.0000 mm, H=10.5000 mm; "
            "lead pitch=3.5000 mm",
            model,
        )
        self.assertNotIn("0.3937007874", model)
        self.assertAlmostEqual(generator.VRML_UNITS_PER_MM * 2.54, 1.0)
        self.assertIn("translation -0.6890 0.0000", model)
        self.assertNotIn("geometry Cylinder", model)
        self.assertGreater(model.count("geometry IndexedFaceSet"), 7)
        self.assertIn("Matte shrink sleeve with filleted ends", model)
        self.assertIn("Subtle rolled top lip", model)
        self.assertIn("Stamped pressure-relief vent ray 1", model)
        self.assertIn("Polarity stripe", model)
        self.assertNotIn("geometry Torus", model)

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
