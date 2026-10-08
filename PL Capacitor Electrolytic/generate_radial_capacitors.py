#!/usr/bin/env python3
"""Generate radial electrolytic-capacitor symbols, footprints, and 3D models.

The generator intentionally uses only the Python standard library.  It writes
KiCad footprint files, KiCad 10 symbol additions, and VRML 2.0 models that can
be viewed in KiCad's 3D viewer without FreeCAD, CadQuery, or OpenSCAD.

Examples:
    python generate_radial_capacitors.py --csv radial_capacitor_specs.csv
    python generate_radial_capacitors.py --diameter 8 --height 10.5 --pitch 3.5
    python generate_radial_capacitors.py --dataset-dir C:\\path\\to\\dataset \\
        --symbol-library C:\\path\\to\\PL Capacitor Electrolytic.kicad_sym
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlparse


DEFAULT_DRILL_MM = Decimal("1.0")
DEFAULT_PAD_DIAMETER_MM = Decimal("2.0")
DIMENSION_QUANTUM = Decimal("0.1")
UUID_NAMESPACE = uuid.UUID("7791d488-0638-4b7c-9ebc-82c39010c2f7")
VRML_UNITS_PER_MM = 1.0 / 2.54

DATASET_COLUMNS = (
    "Mfr Part #",
    "Mfr",
    "Series",
    "Capacitance",
    "Tolerance",
    "Voltage - Rated",
    "ESR (Equivalent Series Resistance)",
    "Lifetime @ Temp.",
    "Operating Temperature",
    "Polarization",
    "Ripple Current @ Low Frequency",
    "Ripple Current @ High Frequency",
    "Lead Spacing",
    "Size / Dimension",
    "Height - Seated (Max)",
    "Mounting Type",
    "Package / Case",
    "Description",
    "Datasheet",
)

LOCAL_DATASHEET_BY_SERIES = {
    "SU": "ABA0000C1053.pdf",
    "CKE": "CKH_CKE.pdf",
    "CKS": "CKR_CKS.pdf",
    "ESK": "KEM_A4004_ESK.pdf",
    "ESH": "KEM_A4005_ESH.pdf",
    "ESC": "KEM_A4006_ESC.pdf",
    "ESY": "KEM_A4007_ESY.pdf",
    "ESW": "KEM_A4009_ESW.pdf",
    "ESE": "KEM_A4055_ESE.pdf",
    "ESS": "KEM_A4057_ESS.pdf",
    "ESL": "KEM_A4074_ESL.pdf",
    "ZLJ": "ZLJ.pdf",
    "157 PUM-SI": "157pumsi.pdf",
    "159 PUL-SI": "159pulsi.pdf",
    "257 PRM-SI": "257prm-si.pdf",
    "259 PHM-SI": "259phmsi.pdf",
    "380LQ": "380LQ.pdf",
    "380LX": "381-383.pdf",
    "381LQ": "381LQ.pdf",
    "381LR": "381LR.pdf",
    "381LX": "381-383.pdf",
    "ALC10": "KEM_A4020_ALC10.pdf",
    "ALC70": "KEM_A4081_ALC70.pdf",
    "B43501": "B43501_Rev_Oct_2005.pdf",
    "B43544": "B43544.pdf",
    "B43547": "B43547.pdf",
    "B43548": "B43548.pdf",
    "B43630": "B43630.pdf",
    "B43644": "B43644.pdf",
    "B43647": "B43647.pdf",
    "BXW": "BXW.pdf",
    "CXW": "CXW.pdf",
    "ELH": "KEM_A4018_ELH.pdf",
    "ESG": "KEM_A4008_ESG.pdf",
    "HXG": "HXG.pdf",
    "HXW": "HXW.pdf",
    "KMQ": "KMQN-e.PDF",
    "KMR": "KMRN-e.PDF",
    "KMS": "KMSN-e.PDF",
    "KMZ": "KMZN-e.PDF",
    "LBA": "LBA.pdf",
    "LGG": "e-lgg.pdf",
    "LGL": "e-lgl.pdf",
    "LGN": "e-lgn.pdf",
    "LGU": "e-lgu.pdf",
    "LGW": "e-lgw.pdf",
    "LGX": "e-lgx.pdf",
    "LHS": "LHSN-e.PDF",
    "LLG": "e-llg.pdf",
    "LLS": "e-lls.pdf",
    "LMB": "LMB.pdf",
    "LXS": "LXSN-e.PDF",
    "LXW": "LXW.pdf",
    "MXG": "MXG.pdf",
    "MXK": "MXK.pdf",
    "PEH534": "KEM_A4024_PEH534.pdf",
    "QXW": "QXW.pdf",
    "SMQ": "SMQN-e.PDF",
    "SLPX": "SLPX.pdf",
    "UCP": "e-ucp.pdf",
    "UCY": "e-ucy.pdf",
    "UPZ": "e-upz.pdf",
    "VXH": "VXH.pdf",
}


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
    package_style: str = "radial"

    @classmethod
    def from_values(
        cls,
        diameter_mm: object,
        height_mm: object,
        pitch_mm: object,
        drill_mm: object | None = None,
        pad_diameter_mm: object | None = None,
        package_style: str = "radial",
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
            package_style=package_style,
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
        if self.package_style not in {"radial", "snap_in"}:
            raise SpecificationError(f"unsupported package_style: {self.package_style}")

    @property
    def name(self) -> str:
        base = (
            f"CP_Radial_D{self.diameter_mm:.1f}mm"
            f"_L{self.height_mm:.1f}mm"
            f"_P{self.pitch_mm:.1f}mm"
        )
        return f"{base}_SnapIn" if self.package_style == "snap_in" else base


@dataclass(frozen=True)
class DatasetPart:
    """One normalized, orderable capacitor from a supplier export."""

    mpn: str
    manufacturer: str
    series: str
    capacitance: str
    tolerance: str
    rated_voltage: str
    esr: str
    lifetime: str
    operating_temperature: str
    polarization: str
    package_case: str
    ripple_current: str
    description: str
    datasheet: str
    spec: CapacitorSpec | None
    lead_geometry_source: str
    source_rows: int = 1

    @property
    def symbol_name(self) -> str:
        return f"{self.capacitance}_{self.rated_voltage}_{self.mpn}"


@dataclass(frozen=True)
class DatasetLoadResult:
    """Normalized dataset plus rows intentionally not generated."""

    generated: tuple[DatasetPart, ...]
    excluded_bipolar: tuple[DatasetPart, ...]
    rejected: tuple[DatasetPart, ...]


def _clean_field(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text == "-" else re.sub(r"\s+", " ", text)


def _normalise_capacitance(value: object) -> str:
    text = _clean_field(value).replace("μ", "u").replace("µ", "u")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([unpmmk]?F)", text, re.I)
    if not match:
        raise SpecificationError(f"unsupported capacitance: {text!r}")
    number, unit = match.groups()
    unit = unit[0].lower() + "F" if len(unit) == 2 else "F"
    return f"{number}{unit}"


def _normalise_voltage(value: object) -> str:
    text = _clean_field(value)
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*V", text, re.I)
    if not match:
        raise SpecificationError(f"unsupported rated voltage: {text!r}")
    return f"{match.group(1)}V"


def _metric_dimension(value: object, field_name: str) -> Decimal:
    text = _clean_field(value)
    matches = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*mm", text, re.I)
    if not matches:
        raise SpecificationError(f"{field_name} has no metric dimension: {text!r}")
    return _decimal(matches[0], field_name)


def _lead_diameter_for_body(diameter_mm: Decimal) -> tuple[Decimal, str]:
    """Return the conservative straight-lead diameter documented by body class."""

    if diameter_mm <= Decimal("6.3"):
        return Decimal("0.5"), "manufacturer-table"
    if diameter_mm <= Decimal("10.0"):
        return Decimal("0.6"), "manufacturer-table"
    if diameter_mm <= Decimal("18.0"):
        return Decimal("0.8"), "manufacturer-table"
    if diameter_mm <= Decimal("22.0"):
        return Decimal("1.0"), "manufacturer-table"
    raise SpecificationError(
        f"no manufacturer or KiCad lead-diameter rule for D{diameter_mm:.1f}mm"
    )


def _mechanical_spec(row: Mapping[str, str]) -> tuple[CapacitorSpec, str]:
    diameter = _metric_dimension(row.get("Size / Dimension"), "diameter_mm")
    try:
        height = _metric_dimension(row.get("Height - Seated (Max)"), "height_mm")
    except SpecificationError:
        combined = re.findall(
            r"([0-9]+(?:\.[0-9]+)?)\s*mm",
            _clean_field(row.get("Size / Dimension")),
            re.I,
        )
        if len(combined) < 2:
            raise
        height = _decimal(combined[1], "height_mm")
    pitch = _metric_dimension(row.get("Lead Spacing"), "pitch_mm")
    package_case = _clean_field(row.get("Package / Case"))
    if package_case == "Radial, Can - Snap-In":
        if pitch != Decimal("10.0"):
            raise SpecificationError(
                f"unsupported snap-in pitch {pitch:.1f} mm; expected 10.0 mm"
            )
        return (
            CapacitorSpec(
                diameter,
                height,
                pitch,
                Decimal("2.0"),
                Decimal("4.0"),
                "snap_in",
            ),
            "kicad-standard-2pin-snapin",
        )
    if package_case != "Radial, Can":
        raise SpecificationError(f"unsupported package/case: {package_case or '-'}")
    lead, _ = _lead_diameter_for_body(diameter)
    source = (
        "supplied-manufacturer-table"
        if _clean_field(row.get("Series")) in LOCAL_DATASHEET_BY_SERIES
        else "kicad-standard-fallback"
    )
    drill = (lead + Decimal("0.2")).quantize(DIMENSION_QUANTUM)
    pad = min(drill + Decimal("0.8"), pitch - Decimal("0.2"))
    pad = pad.quantize(DIMENSION_QUANTUM)
    if (pad - drill) / 2 < Decimal("0.25"):
        raise SpecificationError(
            f"D{diameter:.1f} L{height:.1f} P{pitch:.1f}: less than 0.25 mm annular ring"
        )
    return CapacitorSpec(diameter, height, pitch, drill, pad), source


def _part_from_row(row: Mapping[str, str], source_rows: int = 1) -> DatasetPart:
    low_ripple = _clean_field(row.get("Ripple Current @ Low Frequency"))
    high_ripple = _clean_field(row.get("Ripple Current @ High Frequency"))
    ripple = "; ".join(value for value in (low_ripple, high_ripple) if value)
    try:
        spec, lead_geometry_source = _mechanical_spec(row)
    except SpecificationError:
        spec = None
        lead_geometry_source = ""
    return DatasetPart(
        mpn=_clean_field(row.get("Mfr Part #")),
        manufacturer=_clean_field(row.get("Mfr")),
        series=_clean_field(row.get("Series")),
        capacitance=_normalise_capacitance(row.get("Capacitance")),
        tolerance=_clean_field(row.get("Tolerance")),
        rated_voltage=_normalise_voltage(row.get("Voltage - Rated")),
        esr=_clean_field(row.get("ESR (Equivalent Series Resistance)")),
        lifetime=_clean_field(row.get("Lifetime @ Temp.")),
        operating_temperature=_clean_field(row.get("Operating Temperature")),
        polarization=_clean_field(row.get("Polarization")),
        package_case=_clean_field(row.get("Package / Case")),
        ripple_current=ripple,
        description=_clean_field(row.get("Description")),
        datasheet=_clean_field(row.get("Datasheet")),
        spec=spec,
        lead_geometry_source=lead_geometry_source,
        source_rows=source_rows,
    )


def load_digikey_dataset(dataset_dir: Path) -> DatasetLoadResult:
    """Load and de-duplicate the supplied DigiKey capacitor CSV exports."""

    csv_paths = sorted(dataset_dir.glob("aluminum_electrolytic_capacitors*.csv"))
    if not csv_paths:
        raise SpecificationError(f"no capacitor CSV exports found in {dataset_dir}")

    rows_by_mpn: dict[str, list[dict[str, str]]] = {}
    for csv_path in csv_paths:
        with csv_path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            missing = set(DATASET_COLUMNS).difference(reader.fieldnames or ())
            if missing:
                raise SpecificationError(
                    f"{csv_path} is missing column(s): {', '.join(sorted(missing))}"
                )
            for line_number, row in enumerate(reader, start=2):
                mpn = _clean_field(row.get("Mfr Part #"))
                if not mpn:
                    raise SpecificationError(f"{csv_path}:{line_number}: missing Mfr Part #")
                rows_by_mpn.setdefault(mpn, []).append(row)

    parts: list[DatasetPart] = []
    for mpn, duplicate_rows in sorted(rows_by_mpn.items()):
        baseline = tuple(_clean_field(duplicate_rows[0].get(key)) for key in DATASET_COLUMNS)
        for duplicate in duplicate_rows[1:]:
            candidate = tuple(_clean_field(duplicate.get(key)) for key in DATASET_COLUMNS)
            # Supplier descriptions and URLs can differ without changing the part.
            critical_indices = range(0, 15)
            if any(baseline[index] != candidate[index] for index in critical_indices):
                raise SpecificationError(f"conflicting duplicate dataset rows for {mpn}")
        parts.append(_part_from_row(duplicate_rows[0], len(duplicate_rows)))

    # A part without either a supplied local PDF or a source URL cannot be
    # checked against an authoritative source. Keep it in the manifest as
    # rejected instead of emitting a production symbol from supplier metadata.
    documented = {
        part.mpn for part in parts if _datasheet_reference(part, dataset_dir)[0]
    }
    generated = tuple(
        part
        for part in parts
        if part.polarization == "Polar" and part.spec and part.mpn in documented
    )
    excluded = tuple(
        part
        for part in parts
        if part.polarization == "Bi-Polar" and part.spec and part.mpn in documented
    )
    selected_mpns = {part.mpn for part in generated + excluded}
    rejected = tuple(part for part in parts if part.mpn not in selected_mpns)
    if not generated:
        raise SpecificationError("dataset contains no usable polarized capacitor rows")
    return DatasetLoadResult(generated, excluded, rejected)


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

    radius = float(spec.diameter_mm / 2 + Decimal("0.12"))
    if radius <= 0.25:
        return []
    spacing = max(0.55, min(0.80, radius / 5.5))
    hatch_offset = -radius
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    keep_out_radius = float(spec.pad_diameter_mm / 2 + Decimal("0.25"))
    pad_two = (float(spec.pitch_mm / 2), 0.0)

    # For y = -x + c, c reaches sqrt(2) * radius in the upper-right
    # quadrant.  Stopping at radius leaves that quadrant visibly bare.
    while hatch_offset <= math.sqrt(2.0) * radius + 0.0001:
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


def _negative_boundary_segments(
    spec: CapacitorSpec,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Create the centre divider while maintaining silk clearance to both pads."""

    radius = float(spec.diameter_mm / 2 + Decimal("0.12"))
    if radius <= 0.25:
        return []
    segments = [((0.0, -radius), (0.0, radius))]
    keep_out_radius = float(spec.pad_diameter_mm / 2 + Decimal("0.25"))
    pitch_half = float(spec.pitch_mm / 2)
    for pad_center in ((-pitch_half, 0.0), (pitch_half, 0.0)):
        clipped: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for start, end in segments:
            clipped.extend(
                _subtract_circle_from_segment(start, end, pad_center, keep_out_radius)
            )
        segments = clipped
    return [segment for segment in segments if math.dist(*segment) > 0.12]


def build_footprint(spec: CapacitorSpec) -> str:
    """Build a KiCad footprint as S-expression text."""

    name = spec.name
    radius = float(spec.diameter_mm / 2)
    pad_x = float(spec.pitch_mm / 2)
    ref_y = -radius - 1.25
    value_y = radius + 1.25
    courtyard_radius = radius + 0.50
    # Keep the silk polarity mark wholly outside the circular courtyard.  This
    # avoids body, hatch, and pad collisions while retaining a readable mark
    # that scales with the capacitor diameter.
    silk_plus_half = min(1.50, max(0.30, radius * 0.09))
    silk_plus_x = -(courtyard_radius + silk_plus_half + 0.20)
    silk_plus_y = 0.0
    silk_plus_stroke = min(0.25, max(0.12, silk_plus_half * 0.35))
    fab_plus_x = -radius * 0.60
    fab_plus_y = -radius * 0.60
    fab_plus_half = min(0.35, radius * 0.12)
    package_description = (
        "2-pin snap-in" if spec.package_style == "snap_in" else "radial leaded"
    )
    description = (
        f"CP, {package_description} electrolytic capacitor, "
        f"pin pitch={spec.pitch_mm:.1f}mm, diameter={spec.diameter_mm:.1f}mm, "
        f"height={spec.height_mm:.1f}mm"
    )
    tags = (
        f"CP Radial Electrolytic {package_description} "
        f"pitch {spec.pitch_mm:.1f}mm diameter {spec.diameter_mm:.1f}mm "
        f"height {spec.height_mm:.1f}mm"
    )

    lines = [
        f"(footprint \"{name}\"",
        "\t(version 20241229)",
        "\t(generator \"radial_capacitor_generator\")",
        "\t(generator_version \"1.1\")",
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
            (silk_plus_x - silk_plus_half, silk_plus_y),
            (silk_plus_x + silk_plus_half, silk_plus_y),
            silk_plus_stroke,
            "F.SilkS",
        ),
        *_line(
            name,
            "silk-plus-vertical",
            (silk_plus_x, silk_plus_y - silk_plus_half),
            (silk_plus_x, silk_plus_y + silk_plus_half),
            silk_plus_stroke,
            "F.SilkS",
        ),
        *_circle(name, "silk-body", radius + 0.12, 0.15, "F.SilkS"),
    ]

    for index, (start, end) in enumerate(_negative_boundary_segments(spec)):
        lines.extend(
            _line(name, f"silk-negative-boundary-{index}", start, end, 0.15, "F.SilkS")
        )

    for index, (start, end) in enumerate(_hatch_segments(spec)):
        lines.extend(_line(name, f"silk-hatch-{index}", start, end, 0.15, "F.SilkS"))

    lines.extend(
        [
            *_circle(name, "courtyard", courtyard_radius, 0.05, "F.CrtYd"),
            *_circle(name, "fab-body", radius, 0.10, "F.Fab"),
            *_line(
                name,
                "fab-plus-horizontal",
                (fab_plus_x - fab_plus_half, fab_plus_y),
                (fab_plus_x + fab_plus_half, fab_plus_y),
                0.10,
                "F.Fab",
            ),
            *_line(
                name,
                "fab-plus-vertical",
                (fab_plus_x, fab_plus_y - fab_plus_half),
                (fab_plus_x, fab_plus_y + fab_plus_half),
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
            f"\t(model \"${{PL_FOOTPRINT_DIR}}/PL Capacitor Electrolytic/3D Model/{name}.wrl\"",
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


def _material(
    name: str,
    color: tuple[float, float, float],
    shininess: float,
    specular: tuple[float, float, float] = (0.10, 0.10, 0.10),
    ambient: float = 0.20,
) -> str:
    return (
        f"DEF {name} Appearance {{ material Material {{ diffuseColor {color[0]:.3f} "
        f"{color[1]:.3f} {color[2]:.3f} specularColor {specular[0]:.3f} "
        f"{specular[1]:.3f} {specular[2]:.3f} ambientIntensity {ambient:.3f} "
        f"shininess {shininess:.3f} }} }}"
    )


def _vrml_units(value_mm: float) -> float:
    """Convert millimetres to KiCad's legacy external-VRML unit (0.1 inch)."""

    return value_mm * VRML_UNITS_PER_MM


def _cylinder(
    label: str,
    radius: float,
    height: float,
    center_z: float,
    material: str,
    x: float = 0.0,
    y: float = 0.0,
    segments: int = 48,
    edge_radius: float = 0.0,
) -> str:
    """Return a closed cylindrical mesh with optional rounded edge profiles."""

    z_bottom = -height / 2.0
    z_top = height / 2.0
    edge_radius = max(0.0, min(edge_radius, radius * 0.45, height * 0.45))
    if edge_radius > 0:
        curve_steps = 6
        profile: list[tuple[float, float]] = []
        for index in range(curve_steps + 1):
            angle = -math.pi / 2.0 + (math.pi / 2.0) * index / curve_steps
            profile.append(
                (
                    radius - edge_radius + edge_radius * math.cos(angle),
                    z_bottom + edge_radius + edge_radius * math.sin(angle),
                )
            )
        for index in range(curve_steps + 1):
            angle = (math.pi / 2.0) * index / curve_steps
            profile.append(
                (
                    radius - edge_radius + edge_radius * math.cos(angle),
                    z_top - edge_radius + edge_radius * math.sin(angle),
                )
            )
    else:
        profile = [(radius, z_bottom), (radius, z_top)]

    vertices: list[tuple[float, float, float]] = []
    for ring_radius, z in profile:
        for index in range(segments):
            angle = 2.0 * math.pi * index / segments
            vertices.append(
                (ring_radius * math.cos(angle), ring_radius * math.sin(angle), z)
            )
    bottom_center = len(vertices)
    vertices.append((0.0, 0.0, z_bottom))
    top_center = len(vertices)
    vertices.append((0.0, 0.0, z_top))

    faces: list[str] = []
    for ring_index in range(len(profile) - 1):
        lower_ring = ring_index * segments
        upper_ring = (ring_index + 1) * segments
        for index in range(segments):
            next_index = (index + 1) % segments
            faces.append(
                f"{lower_ring + index}, {lower_ring + next_index}, "
                f"{upper_ring + next_index}, {upper_ring + index}, -1"
            )

    top_ring = (len(profile) - 1) * segments
    for index in range(segments):
        next_index = (index + 1) % segments
        faces.append(f"{bottom_center}, {next_index}, {index}, -1")
        faces.append(f"{top_center}, {top_ring + index}, {top_ring + next_index}, -1")

    points = ",\n        ".join(
        f"{_vrml_units(point_x):.4f} {_vrml_units(point_y):.4f} "
        f"{_vrml_units(point_z):.4f}"
        for point_x, point_y, point_z in vertices
    )
    indices = ",\n        ".join(faces)
    return (
        f"# {label}\n"
        f"Transform {{ translation {_vrml_units(x):.4f} {_vrml_units(y):.4f} "
        f"{_vrml_units(center_z):.4f} children [\n"
        f"  Shape {{ appearance USE {material} geometry IndexedFaceSet {{\n"
        f"    coord Coordinate {{ point [\n        {points}\n    ] }}\n"
        f"    coordIndex [\n        {indices}\n    ]\n"
        "    creaseAngle 0.80\n"
        "    solid TRUE\n"
        "  } }\n"
        "] }"
    )


def _mesh(
    label: str,
    vertices: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    material: str,
    *,
    solid: bool = True,
) -> str:
    points = ",\n      ".join(
        f"{_vrml_units(x):.4f} {_vrml_units(y):.4f} {_vrml_units(z):.4f}"
        for x, y, z in vertices
    )
    indices = ",\n      ".join(
        ", ".join(str(index) for index in face) + ", -1" for face in faces
    )
    return (
        f"# {label}\n"
        f"Shape {{ appearance USE {material} geometry IndexedFaceSet {{\n"
        f"  coord Coordinate {{ point [\n      {points}\n  ] }}\n"
        f"  coordIndex [\n      {indices}\n  ]\n"
        "  creaseAngle 1.15\n"
        f"  solid {'TRUE' if solid else 'FALSE'}\n"
        "} }"
    )


def _lathed_can(
    label: str, profile: list[tuple[float, float]], material: str, segments: int = 72
) -> str:
    """Revolve the actual can outline, including its rolled-over top shoulder."""

    vertices = [
        (radius * math.cos(2 * math.pi * index / segments),
         radius * math.sin(2 * math.pi * index / segments), z)
        for radius, z in profile
        for index in range(segments)
    ]
    bottom_center = len(vertices)
    vertices.append((0.0, 0.0, profile[0][1]))
    top_center = len(vertices)
    vertices.append((0.0, 0.0, profile[-1][1]))
    faces: list[tuple[int, ...]] = []
    for ring in range(len(profile) - 1):
        for index in range(segments):
            following = (index + 1) % segments
            faces.append((ring * segments + index, ring * segments + following,
                          (ring + 1) * segments + following,
                          (ring + 1) * segments + index))
    for index in range(segments):
        following = (index + 1) % segments
        faces.append((bottom_center, following, index))
        faces.append((top_center, (len(profile) - 1) * segments + index,
                      (len(profile) - 1) * segments + following))
    return _mesh(label, vertices, faces, material)


def _surface_radius(z: float, profile: list[tuple[float, float]]) -> float:
    """Interpolate the outer can wall so printed graphics follow both shoulders."""

    for (lower_r, lower_z), (upper_r, upper_z) in zip(profile, profile[1:]):
        if lower_z <= z <= upper_z:
            fraction = (z - lower_z) / (upper_z - lower_z)
            return lower_r + fraction * (upper_r - lower_r)
    raise ValueError("printed graphic lies outside the can profile")


def _surface_point(
    angle: float, z: float, profile: list[tuple[float, float]], offset: float
) -> tuple[float, float, float]:
    radius = _surface_radius(z, profile) + offset
    return (radius * math.cos(angle), radius * math.sin(angle), z)


def _curved_band(
    profile: list[tuple[float, float]], z_bottom: float, z_top: float,
    half_angle: float, offset: float,
) -> str:
    """Wrap the negative sleeve print around the can and over its shoulder."""

    levels = [z_bottom, *[z for _, z in profile if z_bottom < z < z_top], z_top]
    columns = 16
    vertices = [
        _surface_point(-half_angle + 2 * half_angle * column / columns,
                       z, profile, offset)
        for z in levels for column in range(columns + 1)
    ]
    faces = []
    for row in range(len(levels) - 1):
        for column in range(columns):
            start = row * (columns + 1) + column
            faces.append((start, start + 1, start + columns + 2,
                          start + columns + 1))
    return _mesh("Curved negative polarity band", vertices, faces,
                 "POLARITY_STRIPE", solid=False)


def _curved_oval(
    label: str, profile: list[tuple[float, float]], z_center: float,
    half_height: float, half_angle: float, offset: float,
) -> str:
    """A dark oval printed on the gray band, like the sample sleeve artwork."""

    segments = 28
    vertices = []
    for index in range(segments):
        phase = 2 * math.pi * index / segments
        for inner in (False, True):
            angle_radius = half_angle * (0.72 if inner else 1.0)
            height_radius = half_height * (0.76 if inner else 1.0)
            vertices.append(_surface_point(angle_radius * math.cos(phase),
                                           z_center + height_radius * math.sin(phase),
                                           profile, offset))
    faces = [
        (2 * index, 2 * ((index + 1) % segments),
         2 * ((index + 1) % segments) + 1, 2 * index + 1)
        for index in range(segments)
    ]
    return _mesh(label, vertices, faces, "MARKING", solid=False)


def _curved_minus(
    label: str, profile: list[tuple[float, float]], z_center: float,
    half_height: float, half_angle: float, offset: float,
) -> str:
    """A compact minus sign wrapped flush to the polarity band."""

    columns = 12
    vertices = [
        _surface_point(
            -half_angle + 2 * half_angle * column / columns,
            z_center + vertical * half_height,
            profile,
            offset,
        )
        for vertical in (-1.0, 1.0)
        for column in range(columns + 1)
    ]
    faces = [
        (column, column + 1, columns + column + 2, columns + column + 1)
        for column in range(columns)
    ]
    return _mesh(label, vertices, faces, "MARKING", solid=False)


def _curved_chevron(
    label: str, profile: list[tuple[float, float]], z: float,
    half_angle: float, offset: float, scale: float,
) -> str:
    """A small downward chevron between the larger sleeve markings."""

    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, ...]] = []
    outer_angle = half_angle * 0.80
    steps = 8
    for start_angle, end_angle in ((-outer_angle, 0.0),
                                   (0.0, outer_angle)):
        first = len(vertices)
        for index in range(steps + 1):
            fraction = index / steps
            angle = start_angle + (end_angle - start_angle) * fraction
            height = z + (0.17 - 0.34 * (fraction if start_angle < 0
                                          else 1 - fraction)) * scale
            vertices.extend(
                (_surface_point(angle, height - 0.060 * scale,
                                profile, offset),
                 _surface_point(angle, height + 0.060 * scale,
                                profile, offset))
            )
            if index:
                previous = first + 2 * (index - 1)
                faces.append((previous, previous + 2, previous + 3,
                              previous + 1))
    return _mesh(label, vertices, faces, "MARKING", solid=False)


def _box(
    label: str,
    size: tuple[float, float, float],
    center: tuple[float, float, float],
    material: str,
    rotation_z: float = 0.0,
) -> str:
    rotation = f" rotation 0 0 1 {rotation_z:.6f}" if rotation_z else ""
    return (
        f"# {label}\n"
        f"Transform {{ translation {_vrml_units(center[0]):.4f} "
        f"{_vrml_units(center[1]):.4f} {_vrml_units(center[2]):.4f}"
        f"{rotation} children [\n"
        f"  Shape {{ appearance USE {material} geometry Box {{ size "
        f"{_vrml_units(size[0]):.4f} {_vrml_units(size[1]):.4f} "
        f"{_vrml_units(size[2]):.4f} }} }}\n] }}"
    )


def build_vrml(spec: CapacitorSpec) -> str:
    """Build a detailed VRML 2.0 radial-capacitor model for KiCad."""

    body_radius = float(spec.diameter_mm / 2)
    body_height = float(spec.height_mm)
    detail_scale = min(1.0, body_radius / 4.0, body_height / 10.5)
    can_radius = body_radius - 0.025 * detail_scale
    outer_profile = [
        (can_radius - 0.28 * detail_scale, 0.24 * detail_scale),
        (can_radius - 0.14 * detail_scale, 0.32 * detail_scale),
        (can_radius - 0.045 * detail_scale, 0.46 * detail_scale),
        (can_radius, 0.68 * detail_scale),
        (can_radius, body_height - 0.74 * detail_scale),
        (can_radius - 0.018 * detail_scale,
         body_height - 0.55 * detail_scale),
        (can_radius - 0.09 * detail_scale,
         body_height - 0.38 * detail_scale),
        (can_radius - 0.23 * detail_scale,
         body_height - 0.19 * detail_scale),
        (can_radius - 0.41 * detail_scale,
         body_height - 0.055 * detail_scale),
        (can_radius - 0.57 * detail_scale, body_height),
    ]
    sleeve_profile = outer_profile + [
        (can_radius - 0.69 * detail_scale,
         body_height - 0.035 * detail_scale),
        (can_radius - 0.72 * detail_scale,
         body_height - 0.14 * detail_scale),
        (can_radius - 0.72 * detail_scale,
         body_height - 0.27 * detail_scale),
    ]
    lower_crimp_profile = [
        (can_radius - 0.30 * detail_scale, 0.02 * detail_scale),
        (can_radius - 0.12 * detail_scale, 0.055 * detail_scale),
        (body_radius - 0.015 * detail_scale, 0.17 * detail_scale),
        (body_radius, 0.25 * detail_scale),
        (body_radius - 0.005 * detail_scale, 0.32 * detail_scale),
        (can_radius - 0.07 * detail_scale, 0.43 * detail_scale),
        (can_radius - 0.09 * detail_scale, 0.49 * detail_scale),
        (can_radius - 0.02 * detail_scale, 0.59 * detail_scale),
    ]
    bung_height = 0.16 * detail_scale
    top_cap_radius = can_radius - 0.725 * detail_scale
    top_cap_height = 0.07 * detail_scale
    top_cap_top_z = body_height - 0.20 * detail_scale
    top_cap_center_z = top_cap_top_z - top_cap_height / 2
    lead_radius = min(float(spec.drill_mm - Decimal("0.2")) / 2.0, 0.50)
    lead_height = 3.2
    lead_center_z = bung_height - lead_height / 2
    pitch_half = float(spec.pitch_mm / 2)
    band_offset = 0.019 * detail_scale
    marking_offset = 0.026 * detail_scale
    stripe_bottom = 0.59 * detail_scale
    stripe_top = body_height - 0.27 * detail_scale
    band_height = stripe_top - stripe_bottom
    half_band_angle = math.radians(18)
    stripe_markings = [
        _curved_minus(
            f"Polarity band minus marking {index + 1}", outer_profile,
            stripe_bottom + band_height * fraction,
            min(0.085 * detail_scale, band_height * 0.018),
            half_band_angle * 0.58, marking_offset,
        )
        for index, fraction in enumerate((0.19, 0.47, 0.75))
    ] + [
        _curved_chevron(
            f"Polarity band chevron {index + 1}", outer_profile,
            stripe_bottom + band_height * fraction,
            half_band_angle, marking_offset, detail_scale,
        )
        for index, fraction in enumerate((0.33, 0.61, 0.89))
    ]
    vent_cross = [
        _box(
            f"Stamped X vent groove {index + 1}",
            (2 * (top_cap_radius - 0.40 * detail_scale),
             0.065 * detail_scale, 0.008 * detail_scale),
            (0.0, 0.0, top_cap_top_z + 0.004 * detail_scale),
            "VENT",
            angle,
        )
        for index, angle in enumerate((math.pi / 4, -math.pi / 4))
    ]

    if spec.package_style == "snap_in":
        leads = [
            _box(
                "Positive snap-in terminal",
                (0.8, 1.6, lead_height),
                (-pitch_half, 0.0, lead_center_z),
                "LEAD",
            ),
            _box(
                "Negative snap-in terminal",
                (0.8, 1.6, lead_height),
                (pitch_half, 0.0, lead_center_z),
                "LEAD",
            ),
        ]
    else:
        leads = [
            _cylinder(
                "Positive lead",
                lead_radius,
                lead_height,
                lead_center_z,
                "LEAD",
                -pitch_half,
                edge_radius=min(0.04, lead_radius * 0.25),
            ),
            _cylinder(
                "Negative lead",
                lead_radius,
                lead_height,
                lead_center_z,
                "LEAD",
                pitch_half,
                edge_radius=min(0.04, lead_radius * 0.25),
            ),
        ]

    return "\n".join(
        [
            "#VRML V2.0 utf8",
            f"# Generated radial electrolytic capacitor: {spec.name}",
            f"# Nominal body envelope: D={body_radius * 2:.4f} mm, "
            f"H={body_height:.4f} mm; lead pitch={float(spec.pitch_mm):.4f} mm",
            "# Geometry inputs are millimetres, encoded as KiCad legacy VRML",
            "# units (0.1 inch / 2.54 mm) for correct footprint-model sizing.",
            _material(
                "SLEEVE",
                (0.022, 0.025, 0.029), 0.16, (0.10, 0.11, 0.12),
            ),
            _material(
                "POLARITY_STRIPE",
                (0.46, 0.51, 0.56), 0.14, (0.13, 0.14, 0.15),
            ),
            _material("MARKING", (0.025, 0.028, 0.032), 0.08,
                      (0.05, 0.05, 0.05)),
            _material(
                "CRIMP_METAL",
                (0.18, 0.19, 0.21), 0.36, (0.30, 0.31, 0.33),
            ),
            _material(
                "ALUMINIUM",
                (0.48, 0.49, 0.51), 0.32, (0.22, 0.23, 0.25),
            ),
            _material("VENT", (0.13, 0.14, 0.16), 0.12,
                      (0.07, 0.07, 0.08)),
            _material("BUNG", (0.025, 0.027, 0.030), 0.04, (0.03, 0.03, 0.03)),
            _material(
                "LEAD",
                (0.560, 0.575, 0.590),
                0.62,
                (0.480, 0.490, 0.500),
            ),
            "Transform {",
            "  scale 1.0 1.0 1.0",
            "  children [",
            _cylinder(
                "Bottom rubber bung",
                body_radius * 0.78,
                bung_height,
                bung_height / 2,
                "BUNG",
                edge_radius=0.025 * detail_scale,
            ),
            _lathed_can("Rolled lower crimp and retaining bead",
                        lower_crimp_profile, "CRIMP_METAL"),
            _lathed_can("Black sleeve and rounded top shoulder",
                        sleeve_profile, "SLEEVE"),
            _curved_band(outer_profile, stripe_bottom, stripe_top,
                         half_band_angle, band_offset),
            *stripe_markings,
            _cylinder(
                "Recessed top aluminium cap",
                top_cap_radius,
                top_cap_height,
                top_cap_center_z,
                "ALUMINIUM",
                edge_radius=0.008 * detail_scale,
            ),
            *vent_cross,
            *leads,
            "  ]",
            "}",
            "",
        ]
    )


def _sexpr_end(text: str, start: int) -> int:
    """Return the end of one balanced S-expression, ignoring quoted parentheses."""

    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                break
    raise SpecificationError("unbalanced KiCad S-expression")


def _sexpr_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _top_level_symbol_blocks(library_text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    pattern = re.compile(r'^\t\(symbol "((?:\\.|[^"\\])*)"', re.MULTILINE)
    for match in pattern.finditer(library_text):
        end = _sexpr_end(library_text, match.start() + 1)
        blocks[match.group(1)] = library_text[match.start():end]
    return blocks


def _replace_property_value(block: str, property_name: str, value: str) -> str:
    pattern = re.compile(
        r'(\(property\s+"' + re.escape(property_name) + r'"\s+")((?:\\.|[^"\\])*)(")'
    )
    replacement = lambda match: match.group(1) + _sexpr_quote(value) + match.group(3)
    block, count = pattern.subn(replacement, block, count=1)
    if count != 1:
        raise SpecificationError(f"template is missing property {property_name!r}")
    return block


def _hidden_symbol_property(name: str, value: str) -> str:
    return "\n".join(
        [
            f'\t\t(property "{_sexpr_quote(name)}" "{_sexpr_quote(value)}"',
            "\t\t\t(at 0 0 0)",
            "\t\t\t(show_name no)",
            "\t\t\t(do_not_autoplace no)",
            "\t\t\t(hide yes)",
            "\t\t\t(effects",
            "\t\t\t\t(font",
            "\t\t\t\t\t(size 1.27 1.27)",
            "\t\t\t\t)",
            "\t\t\t)",
            "\t\t)",
        ]
    )


def _datasheet_reference(part: DatasetPart, dataset_dir: Path) -> tuple[str, Path | None]:
    pdfs = {path.name.casefold(): path for path in dataset_dir.glob("*.pdf")}
    url_filename = Path(unquote(urlparse(part.datasheet).path)).name
    candidate_names = [
        f"{part.mpn}.pdf",
        LOCAL_DATASHEET_BY_SERIES.get(part.series, ""),
        url_filename,
        f"{part.series}.pdf",
        f"{part.series}N-e.PDF",
        f"e-{part.series.lower()}.pdf",
    ]
    local_pdf = next(
        (pdfs[name.casefold()] for name in candidate_names if name and name.casefold() in pdfs),
        None,
    )
    if local_pdf:
        reference = (
            "${PL_SYMBOL_DIR}/PL Capacitor Electrolytic/Datasheets/"
            f"{local_pdf.name}"
        )
        return reference, local_pdf
    return part.datasheet, None


def build_symbol_block(part: DatasetPart, template_block: str, dataset_dir: Path) -> str:
    """Clone the existing KiCad 10 template without altering its graphics or pins."""

    if part.spec is None:
        raise SpecificationError(f"cannot build symbol without mechanics: {part.mpn}")
    datasheet, _ = _datasheet_reference(part, dataset_dir)
    block = template_block
    replacements = {
        "Value": part.capacitance,
        "Footprint": f"PL Capacitor Electrolytic:{part.spec.name}",
        "Datasheet": datasheet,
        "Description": part.description,
        "Rated Voltage": part.rated_voltage,
        "ki_keywords": "capacitor electrolytic polarized radial through-hole",
        "ki_fp_filters": "CP_Radial_*",
    }
    for name, value in replacements.items():
        block = _replace_property_value(block, name, value)

    custom_values = (
        ("MPN", part.mpn),
        ("Manufacturer", part.manufacturer),
        ("Series", part.series),
        ("Tolerance", part.tolerance),
        ("ESR", part.esr),
        ("Ripple Current", part.ripple_current),
        ("Lifetime", part.lifetime),
        ("Operating Temperature", part.operating_temperature),
    )
    custom = "\n".join(
        _hidden_symbol_property(name, value)
        for name, value in custom_values
        if value
    )
    nested_symbol = block.find("\n\t\t(symbol ")
    if nested_symbol < 0:
        raise SpecificationError("polarized symbol template has no graphical units")
    block = block[:nested_symbol] + "\n" + custom + block[nested_symbol:]
    return block.replace("C_Polarized_Template", part.symbol_name)


def build_symbol_library(
    original_text: str,
    parts: Sequence[DatasetPart],
    dataset_dir: Path,
    preserve_existing: bool = False,
) -> tuple[str, int, int]:
    blocks = _top_level_symbol_blocks(original_text)
    template = blocks.get("C_Polarized_Template")
    if template is None:
        raise SpecificationError("symbol library has no C_Polarized_Template")

    additions: list[str] = []
    skipped = 0
    existing_mpn_names: dict[str, str] = {}
    mpn_pattern = re.compile(r'\(property\s+"MPN"\s+"((?:\\.|[^"\\])*)"')
    for existing_name, existing_block in blocks.items():
        match = mpn_pattern.search(existing_block)
        if match:
            existing_mpn_names[match.group(1)] = existing_name
    for part in sorted(parts, key=lambda item: item.symbol_name.casefold()):
        generated = build_symbol_block(part, template, dataset_dir)
        existing = blocks.get(part.symbol_name)
        if existing is not None:
            if existing != generated and not preserve_existing:
                raise SpecificationError(
                    f"existing symbol conflicts with generated data: {part.symbol_name}"
                )
            skipped += 1
            continue
        if part.mpn in existing_mpn_names:
            if not preserve_existing:
                raise SpecificationError(
                    f"MPN {part.mpn} already exists as {existing_mpn_names[part.mpn]}"
                )
            skipped += 1
            continue
        additions.append(generated)

    root_close = len(original_text.rstrip()) - 1
    if root_close < 0 or original_text[root_close] != ")":
        raise SpecificationError("symbol library root is not properly closed")
    if not additions:
        return original_text, 0, skipped
    prefix = original_text[:root_close].rstrip()
    result = prefix + "\n" + "\n".join(additions) + "\n)\n"
    return result, len(additions), skipped


def _validate_balanced_sexpr(text: str, label: str) -> None:
    try:
        end = _sexpr_end(text, text.index("("))
    except (ValueError, SpecificationError) as error:
        raise SpecificationError(f"{label}: {error}") from error
    if text[end:].strip():
        raise SpecificationError(f"{label}: trailing content after root expression")


def _write_manifest(
    path: Path,
    result: DatasetLoadResult,
    dataset_dir: Path,
) -> None:
    fields = [
        "status", "symbol", "manufacturer", "mpn", "series", "capacitance",
        "rated_voltage", "footprint", "model", "datasheet", "dimensions_mm",
        "drill_mm", "pad_diameter_mm", "lead_geometry_source", "source_rows",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        groups = (
            ("generated", result.generated),
            ("excluded_bipolar", result.excluded_bipolar),
            ("rejected_or_unsupported", result.rejected),
        )
        for status, parts in groups:
            for part in parts:
                spec = part.spec
                datasheet, _ = _datasheet_reference(part, dataset_dir)
                writer.writerow(
                    {
                        "status": status,
                        "symbol": part.symbol_name if status == "generated" else "",
                        "manufacturer": part.manufacturer,
                        "mpn": part.mpn,
                        "series": part.series,
                        "capacitance": part.capacitance,
                        "rated_voltage": part.rated_voltage,
                        "footprint": spec.name if spec and status == "generated" else "",
                        "model": f"{spec.name}.wrl" if spec and status == "generated" else "",
                        "datasheet": datasheet,
                        "dimensions_mm": (
                            f"D{spec.diameter_mm:.1f}xL{spec.height_mm:.1f};P{spec.pitch_mm:.1f}"
                            if spec else ""
                        ),
                        "drill_mm": f"{spec.drill_mm:.1f}" if spec else "",
                        "pad_diameter_mm": f"{spec.pad_diameter_mm:.1f}" if spec else "",
                        "lead_geometry_source": part.lead_geometry_source,
                        "source_rows": part.source_rows,
                    }
                )


def _write_report(
    path: Path,
    result: DatasetLoadResult,
    dataset_dir: Path,
    geometry_count: int,
) -> None:
    missing = {
        "ESR": sum(not part.esr for part in result.generated),
        "Ripple Current": sum(not part.ripple_current for part in result.generated),
        "Lifetime": sum(not part.lifetime for part in result.generated),
        "Operating Temperature": sum(not part.operating_temperature for part in result.generated),
        "Datasheet URL/local mapping": sum(
            not _datasheet_reference(part, dataset_dir)[0] for part in result.generated
        ),
    }
    lines = [
        "# Radial electrolytic capacitor generation report",
        "",
        f"- Generated polarized symbols: {len(result.generated)}",
        f"- Shared footprint/model geometries: {geometry_count}",
        f"- Excluded usable bi-polar parts: {len(result.excluded_bipolar)}",
        f"- Rejected unsupported/incomplete/undocumented rows: {len(result.rejected)}",
        f"- Standard two-terminal snap-in parts: "
        f"{sum(part.package_case == 'Radial, Can - Snap-In' for part in result.generated)}",
        f"- Parts using KiCad lead-geometry fallback: "
        f"{sum(part.lead_geometry_source == 'kicad-standard-fallback' for part in result.generated)}",
        "",
        "## Missing optional source fields",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in missing.items())
    lines.extend(
        [
            "",
            "Missing optional values were omitted from symbols; no values were invented.",
            "Rows without either a supplied PDF or a datasheet URL were not generated.",
            "Bi-polar parts, if present, are intentionally excluded from this batch.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _install_files_atomically(files: Sequence[tuple[Path, Path]], overwrite: bool) -> None:
    existing = [destination for _, destination in files if destination.exists()]
    generated_asset_existing = [
        path for path in existing if path.suffix.lower() in {".kicad_mod", ".wrl", ".csv", ".md"}
    ]
    if generated_asset_existing and not overwrite:
        raise FileExistsError(
            "refusing to overwrite generated file(s): "
            + ", ".join(str(path) for path in generated_asset_existing)
        )

    with tempfile.TemporaryDirectory(prefix="radial-capacitor-backup-") as backup_dir_text:
        backup_dir = Path(backup_dir_text)
        backups: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        try:
            for index, (source, destination) in enumerate(files):
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    backup = backup_dir / f"{index}-{destination.name}"
                    shutil.copy2(destination, backup)
                    backups.append((backup, destination))
                temporary = destination.with_name(destination.name + ".new")
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
                installed.append(destination)
        except Exception:
            for destination in reversed(installed):
                destination.unlink(missing_ok=True)
            for backup, destination in backups:
                shutil.copy2(backup, destination)
            raise


def generate_dataset_library(
    dataset_dir: Path,
    output_root: Path,
    symbol_library: Path,
    overwrite: bool = False,
) -> dict[str, int]:
    """Generate, validate, and atomically install the selected dataset batch."""

    result = load_digikey_dataset(dataset_dir)
    geometry: dict[str, CapacitorSpec] = {}
    for part in result.generated:
        assert part.spec is not None
        existing = geometry.get(part.spec.name)
        if existing and existing != part.spec:
            # Same body envelope: keep the safest verified hole/pad combination.
            drill = max(existing.drill_mm, part.spec.drill_mm)
            pad = min(existing.pitch_mm - Decimal("0.2"), drill + Decimal("0.8"))
            geometry[part.spec.name] = CapacitorSpec(
                existing.diameter_mm,
                existing.height_mm,
                existing.pitch_mm,
                drill,
                pad,
                existing.package_style,
            )
        else:
            geometry[part.spec.name] = part.spec
    original_symbols = symbol_library.read_text(encoding="utf-8-sig")
    new_symbols, added_symbols, skipped_symbols = build_symbol_library(
        original_symbols, result.generated, dataset_dir, preserve_existing=True
    )
    _validate_balanced_sexpr(new_symbols, "generated symbol library")

    with tempfile.TemporaryDirectory(
        prefix=".radial-capacitor-stage-", dir=output_root
    ) as staging_text:
        staging = Path(staging_text)
        generated_pairs = generate_specs(
            sorted(geometry.values(), key=lambda item: item.name), staging, overwrite=True
        )
        staged_symbol = staging / symbol_library.name
        staged_symbol.write_text(new_symbols, encoding="utf-8", newline="\n")
        manifest = staging / "generated_capacitor_manifest.csv"
        report = staging / "generated_capacitor_report.md"
        _write_manifest(manifest, result, dataset_dir)
        _write_report(report, result, dataset_dir, len(geometry))

        for footprint, model in generated_pairs:
            footprint_text = footprint.read_text(encoding="utf-8")
            _validate_balanced_sexpr(footprint_text, footprint.name)
            model_text = model.read_text(encoding="utf-8")
            if "#VRML V2.0 utf8" not in model_text or "Stamped X vent groove" not in model_text:
                raise SpecificationError(f"{model.name}: incomplete detailed VRML model")

        installs: list[tuple[Path, Path]] = []
        for footprint, model in generated_pairs:
            installs.append((footprint, output_root / "PL Capacitor Electrolytic.pretty" / footprint.name))
            installs.append((model, output_root / "3D Model" / model.name))
        installs.extend(
            [
                (staged_symbol, symbol_library),
                (manifest, output_root / manifest.name),
                (report, output_root / report.name),
            ]
        )
        datasheet_target = symbol_library.parent / "Datasheets"
        local_sources = {
            local.resolve()
            for part in result.generated
            for _, local in [_datasheet_reference(part, dataset_dir)]
            if local is not None
        }
        for local in sorted(local_sources):
            installs.append((local, datasheet_target / local.name))
        _install_files_atomically(installs, overwrite)

    return {
        "symbols": len(result.generated),
        "symbols_added": added_symbols,
        "symbols_skipped": skipped_symbols,
        "geometries": len(geometry),
        "excluded_bipolar": len(result.excluded_bipolar),
        "rejected": len(result.rejected),
    }


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
    source.add_argument(
        "--dataset-dir",
        type=Path,
        help="directory containing the DigiKey CSV exports and manufacturer PDFs",
    )
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
    parser.add_argument(
        "--symbol-library",
        type=Path,
        help="KiCad 10 PL Capacitor Electrolytic .kicad_sym (required with --dataset-dir)",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parse_arguments(arguments)
    try:
        if args.dataset_dir:
            if not args.symbol_library:
                raise SpecificationError("--dataset-dir requires --symbol-library")
            if args.height or args.pitch or args.drill or args.pad_diameter:
                raise SpecificationError(
                    "single-footprint dimensions cannot be used with --dataset-dir"
                )
            summary = generate_dataset_library(
                args.dataset_dir,
                args.output_root,
                args.symbol_library,
                args.overwrite,
            )
            print(
                "generated dataset library: "
                f"{summary['symbols']} symbols, {summary['geometries']} footprints/models, "
                f"{summary['excluded_bipolar']} bi-polar excluded, "
                f"{summary['rejected']} rejected"
            )
            return 0
        if args.symbol_library:
            raise SpecificationError("--symbol-library is only valid with --dataset-dir")
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
