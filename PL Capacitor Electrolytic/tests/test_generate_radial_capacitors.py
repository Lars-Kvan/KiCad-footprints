"""Regression tests for the radial capacitor generator."""

from __future__ import annotations

import math
import re
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
        self.assertIn('(start -2.75 -2.4)', footprint)
        self.assertIn('(end -2.05 -2.4)', footprint)
        self.assertIn('(end 4.5 0.0)', footprint)
        self.assertEqual(
            len(re.findall(r'\(layer "F\.CrtYd"\)', footprint)), 1
        )
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
        self.assertIn("Black sleeve and rounded top shoulder", model)
        self.assertIn("Rolled lower crimp and retaining bead", model)
        self.assertIn("Curved negative polarity band", model)
        self.assertIn("Polarity band minus marking 3", model)
        self.assertIn("Polarity band chevron 3", model)
        self.assertIn("Stamped X vent groove 1", model)
        self.assertIn("Stamped X vent groove 2", model)
        self.assertNotIn("geometry Torus", model)

        body_vertices = model.split(
            "# Black sleeve and rounded top shoulder", 1
        )[1].split("coordIndex", 1)[0]
        coordinates = [
            tuple(map(float, match))
            for match in re.findall(
                r"^\s*(-?\d+\.\d+) (-?\d+\.\d+) (-?\d+\.\d+),?$",
                body_vertices,
                re.MULTILINE,
            )
        ]
        self.assertTrue(coordinates)
        self.assertAlmostEqual(max(z for _, _, z in coordinates) * 2.54, 10.5,
                               places=3)
        self.assertLessEqual(
            max(math.hypot(x, y) for x, y, _ in coordinates) * 2.54,
            4.001,
        )

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


class DatasetGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset_dir = Path(__file__).parents[3] / "Capacitor_Kicad_dataset"
        cls.symbol_library = (
            Path(__file__).parents[3]
            / "symbols"
            / "PL Capacitor Electrolytic"
            / "PL Capacitor Electrolytic.kicad_sym"
        )
        cls.result = generator.load_digikey_dataset(cls.dataset_dir)

    def test_dataset_partition_and_installed_batch_counts(self) -> None:
        import csv

        source_mpns = set()
        for path in self.dataset_dir.glob("aluminum_electrolytic_capacitors*.csv"):
            with path.open(newline="", encoding="utf-8-sig") as source:
                source_mpns.update(row["Mfr Part #"].strip() for row in csv.DictReader(source))
        self.assertEqual(
            len(self.result.generated)
            + len(self.result.excluded_bipolar)
            + len(self.result.rejected),
            len(source_mpns),
        )
        self.assertEqual(len(self.result.generated), 875)
        self.assertEqual(len(self.result.excluded_bipolar), 0)
        self.assertEqual(len(self.result.rejected), 21)
        self.assertTrue(
            all(
                generator._datasheet_reference(part, self.dataset_dir)[0]
                for part in self.result.generated
            )
        )
        self.assertEqual(
            len({part.spec.name for part in self.result.generated if part.spec}), 176
        )

    def test_normalization_and_exact_symbol_name(self) -> None:
        import csv

        self.assertEqual(generator._normalise_capacitance("470 µF"), "470uF")
        self.assertEqual(generator._normalise_voltage("35 V"), "35V")
        manifest = Path(__file__).parents[1] / "generated_capacitor_manifest.csv"
        with manifest.open(newline="", encoding="utf-8-sig") as source:
            part = next(row for row in csv.DictReader(source) if row["status"] == "generated")
        self.assertEqual(
            part["symbol"],
            f"{part['capacitance']}_{part['rated_voltage']}_{part['mpn']}",
        )
        self.assertRegex(
            part["footprint"],
            r"^CP_Radial_D\d+\.\dmm_L\d+\.\dmm_P\d+\.\dmm(?:_SnapIn)?$",
        )

    def test_generated_pad_geometry_is_safe(self) -> None:
        for part in self.result.generated:
            spec = part.spec
            self.assertIsNotNone(spec)
            self.assertGreaterEqual(
                (spec.pad_diameter_mm - spec.drill_mm) / 2, Decimal("0.25")
            )
            self.assertLess(spec.pad_diameter_mm, spec.pitch_mm)

    def test_snap_in_geometry_and_model(self) -> None:
        spec = generator.CapacitorSpec.from_values(
            35, 47, 10, 2, 4, package_style="snap_in"
        )
        self.assertEqual(
            spec.name, "CP_Radial_D35.0mm_L47.0mm_P10.0mm_SnapIn"
        )
        footprint = generator.build_footprint(spec)
        model = generator.build_vrml(spec)
        self.assertIn('(drill 2.0)', footprint)
        self.assertIn('(size 4.0 4.0)', footprint)
        self.assertIn('2-pin snap-in', footprint)
        self.assertIn('Positive snap-in terminal', model)
        self.assertIn('Negative snap-in terminal', model)

    def test_symbol_clone_preserves_original_library_and_is_idempotent(self) -> None:
        original = self.symbol_library.read_text(encoding="utf-8-sig")
        blocks = generator._top_level_symbol_blocks(original)
        template_start = original.index('\t(symbol "C_Polarized_Template"')
        original = (
            original[:template_start]
            + blocks["C_Polarized_Template"]
            + "\n)\n"
        )
        part = self.result.generated[0]
        updated, added, skipped = generator.build_symbol_library(
            original, [part], self.dataset_dir
        )
        self.assertEqual((added, skipped), (1, 0))
        self.assertTrue(updated.startswith(original.rstrip()[:-1]))
        self.assertIn(f'(symbol "{part.symbol_name}"', updated)
        self.assertIn(f'(property "Value" "{part.capacitance}"', updated)
        self.assertIn(f'(property "Rated Voltage" "{part.rated_voltage}"', updated)
        again, added, skipped = generator.build_symbol_library(
            updated, [part], self.dataset_dir
        )
        self.assertEqual((added, skipped), (0, 1))
        self.assertEqual(again, updated)

    def test_conflicting_duplicate_rows_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            header = list(generator.DATASET_COLUMNS)
            row = {field: "" for field in header}
            row.update(
                {
                    "Mfr Part #": "TEST-MPN",
                    "Mfr": "Example",
                    "Series": "X",
                    "Capacitance": "100 uF",
                    "Tolerance": "±20%",
                    "Voltage - Rated": "25 V",
                    "Polarization": "Polar",
                    "Lead Spacing": '0.197" (5.00mm)',
                    "Size / Dimension": '0.394" Dia (10.00mm)',
                    "Height - Seated (Max)": '0.630" (16.00mm)',
                }
            )
            for index, voltage in enumerate(("25 V", "35 V")):
                row["Voltage - Rated"] = voltage
                path = root / f"aluminum_electrolytic_capacitors{index}.csv"
                with path.open("w", newline="", encoding="utf-8") as destination:
                    writer = __import__("csv").DictWriter(destination, fieldnames=header)
                    writer.writeheader()
                    writer.writerow(row)
            with self.assertRaisesRegex(generator.SpecificationError, "conflicting"):
                generator.load_digikey_dataset(root)


if __name__ == "__main__":
    unittest.main()
