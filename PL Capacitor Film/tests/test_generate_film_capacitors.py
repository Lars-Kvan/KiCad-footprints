"""Small, dependency-free invariants for the film-capacitor generator."""

import csv
import importlib.util
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "generate_film_capacitors.py"
spec = importlib.util.spec_from_file_location("film", SOURCE)
film = importlib.util.module_from_spec(spec)
spec.loader.exec_module(film)


class FilmGeneratorTest(unittest.TestCase):
    def test_capacitance_and_voltage(self):
        self.assertEqual(film.cap("0.022 \ufffdF"), "22nF")
        self.assertEqual(film.cap("4700 pF"), "4.7nF")
        self.assertEqual(film.cap("1.5 \ufffdF"), "1.5uF")
        self.assertEqual(film.voltage({"Series": "R82", "Voltage Rating - AC": "40V", "Voltage Rating - DC": "63V"}), "63VDC")

    def test_geometry_and_footprint(self):
        geometry = (7.2, 3.5, 7.6, 5.0)
        name = film.model_name(geometry)
        self.assertEqual(name, "C_Film_Box_L7.2mm_W3.5mm_H7.6mm_P5.0mm")
        footprint = film.footprint(geometry)
        self.assertIn('(pad "1" thru_hole circle (at -2.500 0)', footprint)
        self.assertIn('(pad "2" thru_hole circle (at 2.500 0)', footprint)
        self.assertEqual(footprint.count('(layer "F.CrtYd")'), 4)
        self.assertIn(name + ".wrl", footprint)
        self.assertEqual(film.sexpr_end(footprint, 0), len(footprint.rstrip()))

    def test_model_dimensions_and_details(self):
        geometry = (7.2, 3.5, 7.6, 5.0)
        model = film.model(geometry)
        self.assertIn("moulded blue case", model)
        self.assertIn("fine lid seam", model)
        self.assertIn("integral rounded lid", model)
        self.assertIn("tinned lead 1", model)
        self.assertIn("tinned lead 2", model)
        self.assertIn("0.025 0.32 0.67", model)
        for position in (-2.5, 2.5):
            self.assertIn(f"{position / 2.54:.5f}", model)
        self.assertIn(f"{7.6 / 2.54:.5f}", model)

    def test_selection_and_exclusions(self):
        with (film.DATASET / "film_capacitors.csv").open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        selected = []
        for row in rows:
            try:
                selected.append(film.normalise(row))
            except ValueError:
                pass
        self.assertEqual(len(rows), 500)
        self.assertEqual(len(selected), 344)
        self.assertEqual(len({p["geometry"] for p in selected}), 99)
        self.assertTrue(all(p["row"]["Datasheet"] != "-" for p in selected))


if __name__ == "__main__":
    unittest.main()
