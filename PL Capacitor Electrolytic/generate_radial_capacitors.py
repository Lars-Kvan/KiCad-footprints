#!/usr/bin/env python3
"""Generate radial electrolytic-capacitor footprints and 3D models.

The generator intentionally uses only the Python standard library.  It writes
KiCad 9 footprint files and VRML 2.0 models that can be viewed in KiCad's 3D
viewer without needing FreeCAD, CadQuery, or OpenSCAD.

Examples:
    python generate_radial_capacitors.py --csv radial_capacitor_specs.csv
    python generate_radial_capacitors.py --diameter 8 --height 10.5 --pitch 3.5
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_DRILL_MM = Decimal("1.0")
DEFAULT_PAD_DIAMETER_MM = Decimal("2.0")
DIMENSION_QUANTUM = Decimal("0.1")
UUID_NAMESPACE = uuid.UUID("7791d488-0638-4b7c-9ebc-82c39010c2f7")


class SpecificationError(ValueError):
    """Raised when a capacitor specification cannot produce a safe footprint."""


def _decimal(value: object, field_name: str) -> Decimal:
    """Parse a millimetre value and require at most one decimal place."""

    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as error:
        raise SpecificationError(f"{field_name} must be a number") from error

    if not result.is_finite():
        raise SpecificationError(f"{field_name} must be finite")
    if result.quantize(DIMENSION_QUANTUM) != result:
        raise SpecificationError(
            f"{field_name} must use increments of 0.1 mm or coarser"
        )
    return result.quantize(DIMENSION_QUANTUM)


def _mm(value: Decimal | float) -> str:
    """Format KiCad geometry compactly while preserving a decimal point."""

    decimal_value = Decimal(str(value)).quantize(Decimal("0.0001"))
    if decimal_value == 0:
        decimal_value = Decimal("0")
    text = format(decimal_value, "f").rstrip("0").rstrip(".")
    if text in ("", "-"):
        text = "0"
    return text if "." in text else f"{text}.0"


def _uuid(footprint_name: str, element_name: str) -> str:
    """Return stable UUIDs so a repeated generation has a clean diff."""

    return str(uuid.uuid5(UUID_NAMESPACE, f"{footprint_name}:{element_name}"))


@dataclass(frozen=True)
class CapacitorSpec:
    """The mechanical dimensions required to create one footprint."""

    diameter_mm: Decimal
    height_mm: Decimal
    pitch_mm: Decimal
    drill_mm: Decimal = DEFAULT_DRILL_MM
    pad_diameter_mm: Decimal = DEFAULT_PAD_DIAMETER_MM

    @classmethod
    def from_values(
        cls,
        diameter_mm: object,
        height_mm: object,
        pitch_mm: object,
        drill_mm: object | None = None,
        pad_diameter_mm: object | None = None,
    ) -> "CapacitorSpec":
        return cls(
            diameter_mm=_decimal(diameter_mm, "diameter_mm"),
            height_mm=_decimal(height_mm, "height_mm"),
            pitch_mm=_decimal(pitch_mm, "pitch_mm"),
            drill_mm=(
                DEFAULT_DRILL_MM
                if drill_mm in (None, "")
                else _decimal(drill_mm, "drill_mm")
            ),
            pad_diameter_mm=(
                DEFAULT_PAD_DIAMETER_MM
                if pad_diameter_mm in (None, "")
                else _decimal(pad_diameter_mm, "pad_diameter_mm")
            ),
        )

    def __post_init__(self) -> None:
        for field_name, value in (
            ("diameter_mm", self.diameter_mm),
            ("height_mm", self.height_mm),
            ("pitch_mm", self.pitch_mm),
            ("drill_mm", self.drill_mm),
            ("pad_diameter_mm", self.pad_diameter_mm),
        ):
            if value <= 0:
                raise SpecificationError(f"{field_name} must be greater than zero")

        if self.pad_diameter_mm <= self.drill_mm:
            raise SpecificationError("pad_diameter_mm must be larger than drill_mm")
        if self.pad_diameter_mm >= self.pitch_mm:
            raise SpecificationError(
                "pad_diameter_mm must be smaller than pitch_mm so pads do not touch"
            )

    @property
    def name(self) -> str:
        return (
            f"CP_Radial_D{self.diameter_mm:.1f}mm"
            f"_L{self.height_mm:.1f}mm"
            f"_P{self.pitch_mm:.1f}mm"
        )


def read_csv_specs(csv_path: Path) -> list[CapacitorSpec]:
    """Read a batch CSV, accepting blank drill/pad cells as default values."""

    required_columns = {"diameter_mm", "height_mm", "pitch_mm"}
    with csv_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise SpecificationError(f"{csv_path} has no header row")
        missing = required_columns.difference(reader.fieldnames)
        if missing:
            formatted = ", ".join(sorted(missing))
            raise SpecificationError(f"{csv_path} is missing column(s): {formatted}")

        specifications: list[CapacitorSpec] = []
        for line_number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                specifications.append(
                    CapacitorSpec.from_values(
                        row["diameter_mm"],
                        row["height_mm"],
                        row["pitch_mm"],
                        row.get("drill_mm"),
                        row.get("pad_diameter_mm"),
                    )
                )
            except SpecificationError as error:
                raise SpecificationError(f"{csv_path}:{line_number}: {error}") from error

    if not specifications:
        raise SpecificationError(f"{csv_path} contains no capacitor rows")
    return specifications


def _stroke(width: float | Decimal, line_type: str = "solid") -> list[str]:
    return ["\t\t(stroke", f"\t\t\t(width {_mm(width)})", f"\t\t\t(type {line_type})", "\t\t)"]


def _line(
    name: str,
    token: str,
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    layer: str,
) -> list[str]:
    return [
        "\t(fp_line",
        f"\t\t(start {_mm(start[0])} {_mm(start[1])})",
        f"\t\t(end {_mm(end[0])} {_mm(end[1])})",
        *_stroke(width),
        f"\t\t(layer \"{layer}\")",
        f"\t\t(uuid \"{_uuid(name, token)}\")",
        "\t)",
    ]


def _circle(
    name: str,
    token: str,
    radius: float,
    width: float,
    layer: str,
) -> list[str]:
    return [
        "\t(fp_circle",
        "\t\t(center 0.0 0.0)",
        f"\t\t(end {_mm(radius)} 0.0)",
        *_stroke(width),
        "\t\t(fill no)",
        f"\t\t(layer \"{layer}\")",
        f"\t\t(uuid \"{_uuid(name, token)}\")",
        "\t)",
    ]


def _property(
    name: str,
    token: str,
    property_name: str,
    value: str,
    at: tuple[float, float],
    layer: str,
    hidden: bool = False,
) -> list[str]:
    result = [
        f"\t(property \"{property_name}\" \"{value}\"",
        f"\t\t(at {_mm(at[0])} {_mm(at[1])} 0)",
        f"\t\t(layer \"{layer}\")",
    ]
    if hidden:
        result.append("\t\t(hide yes)")
    result.extend(
        [
            f"\t\t(uuid \"{_uuid(name, token)}\")",
            "\t\t(effects",
            "\t\t\t(font",
            "\t\t\t\t(size 1.0 1.0)",
            "\t\t\t\t(thickness 0.15)",
            "\t\t\t)",
            "\t\t)",
            "\t)",
        ]
    )
    return result


def _subtract_circle_from_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
    radius: float,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Return the pieces of a line segment that remain outside a keep-out."""

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    fx = start[0] - center[0]
    fy = start[1] - center[1]
    a = dx * dx + dy * dy
    b = 2.0 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - radius * radius
    discriminant = b * b - 4.0 * a * c
    if discriminant <= 0 or a == 0:
        return [(start, end)]

    root = math.sqrt(discriminant)
    t0 = (-b - root) / (2.0 * a)
    t1 = (-b + root) / (2.0 * a)
    lo, hi = sorted((t0, t1))
    lo = max(0.0, lo)
    hi = min(1.0, hi)
    if lo >= hi:
        return [(start, end)]

    def point(t: float) -> tuple[float, float]:
        return start[0] + dx * t, start[1] + dy * t

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    if lo > 0:
        segments.append((start, point(lo)))
    if hi < 1:
        segments.append((point(hi), end))
    return segments


def _hatch_segments(spec: CapacitorSpec) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Create clipped 45-degree silk hatching in the positive-x half of the body."""

    radius = float(spec.diameter_mm / 2 - Decimal("0.35"))
    if radius <= 0.25:
        return []
    spacing = max(0.8, min(1.2, radius / 3.5))
    hatch_offset = -radius
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    keep_out_radius = float(spec.pad_diameter_mm / 2 + Decimal("0.25"))
    pad_two = (float(spec.pitch_mm / 2), 0.0)

    while hatch_offset <= radius + 0.0001:
        # y = -x + hatch_offset intersects x^2 + y^2 = radius^2.
        determinant = 2.0 * radius * radius - hatch_offset * hatch_offset
        if determinant > 0:
            spread = math.sqrt(determinant)
            x_start = max(0.0, (hatch_offset - spread) / 2.0)
            x_end = (hatch_offset + spread) / 2.0
            if x_end - x_start > 0.12:
                start = (x_start, -x_start + hatch_offset)
                end = (x_end, -x_end + hatch_offset)
                for segment in _subtract_circle_from_segment(
                    start, end, pad_two, keep_out_radius
                ):
                    if math.dist(*segment) > 0.12:
                        segments.append(segment)
        hatch_offset += spacing
    return segments


def build_footprint(spec: CapacitorSpec) -> str:
    """Build a KiCad 9 footprint as S-expression text."""

    name = spec.name
    radius = float(spec.diameter_mm / 2)
    pad_x = float(spec.pitch_mm / 2)
    ref_y = -radius - 1.25
    value_y = radius + 1.25
    plus_x = -radius + min(0.35, radius * 0.12)
    plus_y = -radius * 0.60
    plus_half = 0.50
    description = (
        "CP, Radial electrolytic capacitor, "
        f"pin pitch={spec.pitch_mm:.1f}mm, diameter={spec.diameter_mm:.1f}mm, "
        f"height={spec.height_mm:.1f}mm"
    )
    tags = (
        "CP Radial Electrolytic "
        f"pitch {spec.pitch_mm:.1f}mm diameter {spec.diameter_mm:.1f}mm "
        f"height {spec.height_mm:.1f}mm"
    )

    lines = [
        f"(footprint \"{name}\"",
        "\t(version 20241229)",
        "\t(generator \"pcbnew\")",
        "\t(generator_version \"9.0\")",
        "\t(layer \"F.Cu\")",
        f"\t(descr \"{description}\")",
        f"\t(tags \"{tags}\")",
        *_property(name, "reference", "Reference", "REF**", (0.0, ref_y), "F.SilkS"),
        *_property(name, "value", "Value", name, (0.0, value_y), "F.Fab", True),
        *_property(name, "datasheet", "Datasheet", "", (0.0, 0.0), "F.Fab", True),
        *_property(name, "description", "Description", description, (0.0, 0.0), "F.Fab", True),
        "\t(attr through_hole)",
        *_line(
            name,
            "silk-plus-horizontal",
            (plus_x - plus_half, plus_y),
            (plus_x + plus_half, plus_y),
            0.15,
            "F.SilkS",
        ),
        *_line(
            name,
            "silk-plus-vertical",
            (plus_x, plus_y - plus_half),
            (plus_x, plus_y + plus_half),
            0.15,
            "F.SilkS",
        ),
        *_circle(name, "silk-body", radius + 0.12, 0.15, "F.SilkS"),
    ]

    for index, (start, end) in enumerate(_hatch_segments(spec)):
        lines.extend(_line(name, f"silk-hatch-{index}", start, end, 0.15, "F.SilkS"))

    lines.extend(
        [
            *_circle(name, "courtyard", radius + 0.25, 0.05, "F.CrtYd"),
            *_circle(name, "fab-body", radius, 0.10, "F.Fab"),
            *_line(
                name,
                "fab-plus-horizontal",
                (plus_x - plus_half, plus_y),
                (plus_x + plus_half, plus_y),
                0.10,
                "F.Fab",
            ),
            *_line(
                name,
                "fab-plus-vertical",
                (plus_x, plus_y - plus_half),
                (plus_x, plus_y + plus_half),
                0.10,
                "F.Fab",
            ),
            "\t(fp_text user \"${REFERENCE}\"",
            "\t\t(at 0.0 0.0 0)",
            "\t\t(layer \"F.Fab\")",
            f"\t\t(uuid \"{_uuid(name, 'fab-reference')}\")",
            "\t\t(effects",
            "\t\t\t(font",
            "\t\t\t\t(size 1.0 1.0)",
            "\t\t\t\t(thickness 0.15)",
            "\t\t\t)",
            "\t\t)",
            "\t)",
            "\t(pad \"1\" thru_hole roundrect",
            f"\t\t(at {_mm(-pad_x)} 0.0)",
            f"\t\t(size {_mm(spec.pad_diameter_mm)} {_mm(spec.pad_diameter_mm)})",
            f"\t\t(drill {_mm(spec.drill_mm)})",
            "\t\t(layers \"*.Cu\" \"*.Mask\")",
            "\t\t(remove_unused_layers no)",
            "\t\t(roundrect_rratio 0.125)",
            f"\t\t(uuid \"{_uuid(name, 'pad-1')}\")",
            "\t)",
            "\t(pad \"2\" thru_hole circle",
            f"\t\t(at {_mm(pad_x)} 0.0)",
            f"\t\t(size {_mm(spec.pad_diameter_mm)} {_mm(spec.pad_diameter_mm)})",
            f"\t\t(drill {_mm(spec.drill_mm)})",
            "\t\t(layers \"*.Cu\" \"*.Mask\")",
            "\t\t(remove_unused_layers no)",
            f"\t\t(uuid \"{_uuid(name, 'pad-2')}\")",
            "\t)",
            "\t(embedded_fonts no)",
            f"\t(model \"${{PL}}/footprints/PL Capacitor Electrolytic/3D Model/{name}.wrl\"",
            "\t\t(offset",
            "\t\t\t(xyz 0.0 0.0 0.0)",
            "\t\t)",
            "\t\t(scale",
            "\t\t\t(xyz 1.0 1.0 1.0)",
            "\t\t)",
            "\t\t(rotate",
            "\t\t\t(xyz 0.0 0.0 0.0)",
            "\t\t)",
            "\t)",
            ")",
            "",
        ]
    )
    return "\n".join(lines)


def _material(name: str, color: tuple[float, float, float], shininess: float) -> str:
    return (
        f"DEF {name} Appearance {{ material Material {{ diffuseColor {color[0]:.3f} "
        f"{color[1]:.3f} {color[2]:.3f} specularColor 0.600 0.600 0.600 "
        f"shininess {shininess:.3f} }} }}"
    )


def _cylinder(
    label: str,
    radius: float,
    height: float,
    center_z: float,
    material: str,
    x: float = 0.0,
    y: float = 0.0,
) -> str:
    return (
        f"# {label}\n"
        f"Transform {{ translation {x:.4f} {y:.4f} {center_z:.4f} children [\n"
        f"  Shape {{ appearance USE {material} geometry Cylinder {{ radius {radius:.4f} "
        f"height {height:.4f} }} }}\n] }}"
    )


def _box(
    label: str,
    size: tuple[float, float, float],
    center: tuple[float, float, float],
    material: str,
) -> str:
    return (
        f"# {label}\n"
        f"Transform {{ translation {center[0]:.4f} {center[1]:.4f} {center[2]:.4f} children [\n"
        f"  Shape {{ appearance USE {material} geometry Box {{ size {size[0]:.4f} "
        f"{size[1]:.4f} {size[2]:.4f} }} }}\n] }}"
    )


def _polarity_stripe(radius: float, z_min: float, z_max: float) -> str:
    """Create a slightly proud curved stripe on the negative (pad 2) side."""

    segments = 10
    start_angle = math.radians(-24.0)
    end_angle = math.radians(24.0)
    vertices: list[tuple[float, float, float]] = []
    for index in range(segments + 1):
        angle = start_angle + (end_angle - start_angle) * index / segments
        x = (radius + 0.02) * math.cos(angle)
        y = (radius + 0.02) * math.sin(angle)
        vertices.extend([(x, y, z_min), (x, y, z_max)])

    points = ",\n      ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in vertices)
    faces = []
    for index in range(segments):
        bottom_a = 2 * index
        top_a = bottom_a + 1
        bottom_b = bottom_a + 2
        top_b = bottom_a + 3
        faces.append(f"{bottom_a}, {bottom_b}, {top_b}, {top_a}, -1")
    return (
        "# Polarity stripe, facing pad 2\n"
        "Shape { appearance USE POLARITY_STRIPE geometry IndexedFaceSet {\n"
        f"  coord Coordinate {{ point [\n      {points}\n  ] }}\n"
        f"  coordIndex [\n    {',\n    '.join(faces)}\n  ]\n"
        "  solid FALSE\n} }"
    )


def build_vrml(spec: CapacitorSpec) -> str:
    """Build a detailed VRML 2.0 radial-capacitor model in millimetres."""

    body_radius = float(spec.diameter_mm / 2)
    body_height = float(spec.height_mm)
    lower_rim_height = min(0.35, body_height * 0.08)
    upper_rim_height = min(0.32, body_height * 0.08)
    bung_height = min(0.38, body_height * 0.10)
    sleeve_bottom = bung_height
    sleeve_top = max(sleeve_bottom + 0.2, body_height - upper_rim_height)
    sleeve_height = sleeve_top - sleeve_bottom
    lead_radius = min(float(spec.drill_mm) * 0.32, 0.34)
    lead_height = 3.2
    lead_center_z = sleeve_bottom / 2 - lead_height / 2
    pitch_half = float(spec.pitch_mm / 2)
    vent_length = max(1.2, min(body_radius * 1.35, body_radius * 2 - 0.8))

    return "\n".join(
        [
            "#VRML V2.0 utf8",
            f"# Generated radial electrolytic capacitor: {spec.name}",
            "# Units are millimetres; KiCad applies the model at the footprint origin.",
            _material("SLEEVE", (0.035, 0.090, 0.145), 0.28),
            _material("POLARITY_STRIPE", (0.820, 0.860, 0.900), 0.15),
            _material("METAL", (0.600, 0.620, 0.650), 0.72),
            _material("VENT", (0.180, 0.200, 0.225), 0.36),
            _material("BUNG", (0.050, 0.050, 0.055), 0.12),
            _material("LEAD", (0.760, 0.770, 0.780), 0.82),
            _cylinder(
                "Bottom rubber bung",
                body_radius * 0.72,
                bung_height,
                bung_height / 2,
                "BUNG",
            ),
            _cylinder(
                "Lower crimped metal rim",
                body_radius,
                lower_rim_height,
                sleeve_bottom + lower_rim_height / 2,
                "METAL",
            ),
            _cylinder(
                "Coloured shrink sleeve",
                body_radius - 0.10,
                sleeve_height,
                sleeve_bottom + sleeve_height / 2,
                "SLEEVE",
            ),
            _polarity_stripe(body_radius - 0.08, sleeve_bottom + 0.10, sleeve_top - 0.08),
            _cylinder(
                "Top rolled metal rim",
                body_radius,
                upper_rim_height,
                sleeve_top + upper_rim_height / 2,
                "METAL",
            ),
            _cylinder(
                "Top aluminium cap",
                max(0.20, body_radius - 0.38),
                0.10,
                body_height - 0.09,
                "METAL",
            ),
            _box(
                "Stamped vent horizontal groove",
                (vent_length, 0.18, 0.045),
                (0.0, 0.0, body_height - 0.023),
                "VENT",
            ),
            _box(
                "Stamped vent vertical groove",
                (0.18, vent_length, 0.045),
                (0.0, 0.0, body_height - 0.022),
                "VENT",
            ),
            _cylinder(
                "Positive lead",
                lead_radius,
                lead_height,
                lead_center_z,
                "LEAD",
                -pitch_half,
            ),
            _cylinder(
                "Negative lead",
                lead_radius,
                lead_height,
                lead_center_z,
                "LEAD",
                pitch_half,
            ),
            "",
        ]
    )


def generate_specs(
    specifications: Iterable[CapacitorSpec],
    output_root: Path,
    overwrite: bool = False,
) -> list[tuple[Path, Path]]:
    """Write model/footprint pairs and return their paths."""

    specifications = list(specifications)
    if not specifications:
        raise SpecificationError("no capacitor specifications were supplied")
    names = [spec.name for spec in specifications]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SpecificationError(f"duplicate footprint name(s): {', '.join(duplicates)}")

    footprint_directory = output_root / "PL Capacitor Electrolytic.pretty"
    model_directory = output_root / "3D Model"
    destinations = [
        (footprint_directory / f"{spec.name}.kicad_mod", model_directory / f"{spec.name}.wrl")
        for spec in specifications
    ]
    existing = [path for pair in destinations for path in pair if path.exists()]
    if existing and not overwrite:
        listing = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"refusing to overwrite existing file(s): {listing}")

    footprint_directory.mkdir(parents=True, exist_ok=True)
    model_directory.mkdir(parents=True, exist_ok=True)
    for spec, (footprint_path, model_path) in zip(specifications, destinations):
        footprint_path.write_text(build_footprint(spec), encoding="utf-8", newline="\n")
        model_path.write_text(build_vrml(spec), encoding="utf-8", newline="\n")
    return destinations


def _parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="CSV file containing capacitor dimensions")
    source.add_argument("--diameter", help="capacitor diameter in mm")
    parser.add_argument("--height", help="capacitor height in mm (required with --diameter)")
    parser.add_argument("--pitch", help="lead pitch in mm (required with --diameter)")
    parser.add_argument("--drill", help="optional drill diameter in mm; default: 1.0")
    parser.add_argument(
        "--pad-diameter", help="optional circular pad diameter in mm; default: 2.0"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory that contains the .pretty and 3D Model folders",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="replace matching generated files"
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parse_arguments(arguments)
    try:
        if args.csv:
            if args.height or args.pitch or args.drill or args.pad_diameter:
                raise SpecificationError(
                    "--height, --pitch, --drill, and --pad-diameter cannot be used with --csv"
                )
            specifications = read_csv_specs(args.csv)
        else:
            if args.height is None or args.pitch is None:
                raise SpecificationError("--diameter requires both --height and --pitch")
            specifications = [
                CapacitorSpec.from_values(
                    args.diameter,
                    args.height,
                    args.pitch,
                    args.drill,
                    args.pad_diameter,
                )
            ]
        outputs = generate_specs(specifications, args.output_root, args.overwrite)
    except (FileNotFoundError, FileExistsError, OSError, SpecificationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for footprint_path, model_path in outputs:
        print(f"generated {footprint_path}")
        print(f"generated {model_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
