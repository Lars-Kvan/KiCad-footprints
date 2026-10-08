#!/usr/bin/env python3
"""Generate documented two-lead boxed film capacitors for the PL KiCad libraries.

The supplier CSV is only a selection/index: every included part must name a
datasheet. Unverified package styles and incomplete mechanics are reported,
not silently given an invented footprint.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlparse
import uuid

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "Capacitor_Kicad_dataset"
FILM_FOOTPRINTS = ROOT / "footprints" / "PL Capacitor Film"
FILM_SYMBOLS = ROOT / "symbols" / "PL Capacitor Film"
LIBRARY = FILM_SYMBOLS / "PL Capacitor Film.kicad_sym"
FOOTPRINT_DIR = FILM_FOOTPRINTS / "PL Capacitor Film.pretty"
MODEL_DIR = FILM_FOOTPRINTS / "3D Model"
MANIFEST = FILM_FOOTPRINTS / "generated_film_capacitor_manifest.csv"
REPORT = FILM_FOOTPRINTS / "generated_film_capacitor_report.md"


def clean(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value in ("-", "N/A", "NA") else value


def number(text: str) -> float:
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"missing number: {text!r}")
    return float(match.group())


def dimension(text: str) -> float:
    match = re.findall(r"(\d+(?:\.\d+)?)\s*mm", text, flags=re.I)
    if not match:
        raise ValueError(f"missing mm dimension: {text!r}")
    return float(match[0])


def size(text: str) -> tuple[float, float]:
    match = re.search(r"\((\d+(?:\.\d+)?)mm\s*x\s*(\d+(?:\.\d+)?)mm\)", text, re.I)
    if not match:
        raise ValueError(f"missing L × W: {text!r}")
    return float(match.group(1)), float(match.group(2))


def fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def dim_fmt(value: float) -> str:
    result = fmt(value)
    return result + ".0" if "." not in result else result


def cap(text: str) -> str:
    value = number(text)
    low = text.lower()
    if "uf" in low or "µf" in low or "\ufffdf" in low:
        pf = value * 1_000_000
    elif "nf" in low:
        pf = value * 1000
    elif "pf" in low:
        pf = value
    else:
        raise ValueError(f"unknown capacitance unit: {text!r}")
    if pf >= 1_000_000:
        return f"{fmt(pf / 1_000_000)}uF"
    if pf >= 1000:
        return f"{fmt(pf / 1000)}nF"
    return f"{fmt(pf)}pF"


def voltage(row: dict[str, str]) -> str:
    ac = clean(row.get("Voltage Rating - AC"))
    dc = clean(row.get("Voltage Rating - DC"))
    if ac and ("X1" in row["Series"] or "X2" in row["Series"] or "Y1" in row["Series"] or "Y2" in row["Series"]):
        return f"{fmt(number(ac))}VAC"
    if dc:
        return f"{fmt(number(dc))}VDC"
    if ac:
        return f"{fmt(number(ac))}VAC"
    raise ValueError("missing rated voltage")


def quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def uid(name: str, part: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pl-film/{name}/{part}"))


def sexpr_end(text: str, start: int) -> int:
    depth = 0
    quoted = escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unbalanced KiCad expression")


def blocks(text: str) -> dict[str, str]:
    return {match.group(1): text[match.start():sexpr_end(text, match.start())]
            for match in re.finditer(r'^\t\(symbol "([^"]+)"', text, re.M)}


def property_value(block: str, key: str, value: str) -> str:
    pattern = re.compile(r'(\(property "' + re.escape(key) + r'" ")([^"]*)(")')
    result, count = pattern.subn(lambda m: m.group(1) + quote(value) + m.group(3), block, count=1)
    if count != 1:
        raise ValueError(f"missing template property {key}")
    return result


def hidden_property(key: str, value: str) -> str:
    return (f'\t\t(property "{quote(key)}" "{quote(value)}"\n'
            '\t\t\t(at 0 0 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n'
            '\t\t\t(hide yes)\n\t\t\t(effects (font (size 1.27 1.27)))\n\t\t)')


def pdf_reference(url: str, pdfs: dict[str, Path]) -> tuple[str, Path | None]:
    name = Path(unquote(urlparse(url).path)).name
    for candidate in (name, name + ".pdf"):
        pdf = pdfs.get(candidate.casefold())
        if pdf:
            return f"${{PL_SYMBOL_DIR}}/PL Capacitor Film/Datasheets/{pdf.name}", pdf
    return url, None


def model_name(geometry: tuple[float, float, float, float]) -> str:
    length, width, height, pitch = geometry
    return f"C_Film_Box_L{dim_fmt(length)}mm_W{dim_fmt(width)}mm_H{dim_fmt(height)}mm_P{dim_fmt(pitch)}mm"


def normalise(row: dict[str, str]) -> dict[str, object]:
    mpn = clean(row["Mfr Part #"])
    if not mpn:
        raise ValueError("missing MPN")
    if not clean(row["Datasheet"]):
        raise ValueError("missing datasheet")
    if row["Mounting Type"] != "Through Hole" or row["Package / Case"] != "Radial":
        raise ValueError("unsupported mounting/package")
    if clean(row["Termination"]) not in ("PC Pins", "PC Pins - Long Leads"):
        raise ValueError("unsupported termination")
    if row["Mfr"] == "Panasonic Industry":
        raise ValueError("epoxy-coated/dipped Panasonic case: not boxed")
    geom = (*size(clean(row["Size / Dimension"])), dimension(clean(row["Height - Seated (Max)"])), dimension(clean(row["Lead Spacing"])))
    length, width, height, pitch = geom
    if min(geom) <= 0 or length < pitch + 0.6 or width < 1.5 or height < 2:
        raise ValueError("invalid or insufficient mechanical dimensions")
    value, rated = cap(clean(row["Capacitance"])), voltage(row)
    symbol = f"{value}_{rated}_{mpn}"
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", symbol):
        raise ValueError("unsafe symbol name")
    return {"row": row, "mpn": mpn, "geometry": geom, "footprint": model_name(geom),
            "symbol": symbol, "value": value, "rated": rated}


def symbol_block(part: dict[str, object], template: str, datasheet: str) -> str:
    row = part["row"]
    name = str(part["symbol"])
    block = template.replace("Capacitor_Template", name)
    values = {"Value": str(part["value"]), "Rated Voltage": str(part["rated"]),
              "Footprint": "PL Capacitor Film:" + str(part["footprint"]),
              "Datasheet": datasheet, "Description": clean(row["Description"]),
              "ki_keywords": "film capacitor unpolarized radial through-hole",
              "ki_fp_filters": "C_Film_Box_*"}
    for key, value in values.items():
        block = property_value(block, key, value)
    manufacturer=row["Mfr"].replace("W\ufffdrth", "Würth")
    tolerance=clean(row["Tolerance"]).replace("\ufffd", "±")
    temperature=clean(row["Operating Temperature"]).replace("\ufffdC", "°C")
    optional = {"MPN": part["mpn"], "Manufacturer": manufacturer, "Series": row["Series"],
                "Tolerance": tolerance, "Dielectric Material": row["Dielectric Material"],
                "Voltage Rating - AC": row["Voltage Rating - AC"],
                "Voltage Rating - DC": row["Voltage Rating - DC"],
                "Operating Temperature": temperature,
                "Applications": row["Applications"], "Ratings": row["Ratings"],
                "Features": row["Features"]}
    props = "\n".join(hidden_property(key, clean(str(value))) for key, value in optional.items() if clean(str(value)))
    marker = "\n\t\t(symbol "
    index = block.find(marker)
    if index < 0:
        raise ValueError("template graphics missing")
    return block[:index] + "\n" + props + block[index:]


def fp_line(name: str, key: str, x1: float, y1: float, x2: float, y2: float, layer: str, width: float) -> str:
    return (f'\t(fp_line (start {x1:.3f} {y1:.3f}) (end {x2:.3f} {y2:.3f}) '
            f'(stroke (width {width}) (type solid)) (layer "{layer}") (uuid "{uid(name,key)}"))')


def rectangle(name: str, label: str, half_x: float, half_y: float, layer: str, width: float) -> list[str]:
    points = [(-half_x,-half_y),(half_x,-half_y),(half_x,half_y),(-half_x,half_y)]
    return [fp_line(name,f"{label}-{i}",*points[i],*points[(i+1)%4],layer,width) for i in range(4)]


def footprint(geometry: tuple[float,float,float,float]) -> str:
    length,width,height,pitch = geometry
    name = model_name(geometry)
    drill = 0.8 if pitch <= 7.5 else 1.0 if pitch <= 15 else 1.2
    pad = min(drill + 0.8, pitch - 0.3)
    if pad - drill < 0.5:
        raise ValueError(f"insufficient annular ring: {name}")
    hx,hy=length/2,width/2
    # Leave ≥0.2 mm between silkscreen and through-hole pad copper.
    silk_x=max(hx+0.12,pitch/2+pad/2+0.25)
    silk_y=max(hy+0.12,pad/2+0.25)
    court_x=max(hx+0.50,pitch/2+pad/2+0.50)
    court_y=max(hy+0.50,pad/2+0.50)
    lines=[f'(footprint "{name}"', '\t(version 20251024)', '\t(generator "pl_film_generator")',
           '\t(generator_version "1.0")', '\t(layer "F.Cu")',
           f'\t(descr "Film capacitor, boxed radial, L={fmt(length)}mm W={fmt(width)}mm H={fmt(height)}mm, pitch={fmt(pitch)}mm")',
           f'\t(tags "film capacitor radial boxed pitch {fmt(pitch)}mm")',
           f'\t(property "Reference" "REF**" (at 0 {-silk_y-1.4:.3f} 0) (layer "F.SilkS") (uuid "{uid(name,"reference")}") (effects (font (size 1 1) (thickness 0.15))))',
           f'\t(property "Value" "{name}" (at 0 {silk_y+1.4:.3f} 0) (layer "F.Fab") (uuid "{uid(name,"value")}") (effects (font (size 1 1) (thickness 0.15))))',
           '\t(attr through_hole)']
    lines+=rectangle(name,"silk",silk_x,silk_y,"F.SilkS",0.12)
    lines+=rectangle(name,"fab",hx,hy,"F.Fab",0.10)
    lines+=rectangle(name,"court",court_x,court_y,"F.CrtYd",0.05)
    for index,x in enumerate((-pitch/2,pitch/2),1):
        lines.append(f'\t(pad "{index}" thru_hole circle (at {x:.3f} 0) (size {pad:.3f} {pad:.3f}) (drill {drill:.3f}) (layers "*.Cu" "*.Mask") (uuid "{uid(name,f"pad-{index}")}"))')
    lines.append(f'\t(model "${{PL_FOOTPRINT_DIR}}/PL Capacitor Film/3D Model/{name}.wrl" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))')
    return "\n".join(lines+[ ")", ""])


def mesh(label: str, vertices: list[tuple[float,float,float]], faces: list[tuple[int,...]], material: str) -> str:
    points=", ".join(f"{x/2.54:.5f} {y/2.54:.5f} {z/2.54:.5f}" for x,y,z in vertices)
    indices=", ".join(", ".join(map(str,face))+", -1" for face in faces)
    return f"# {label}\nShape {{ appearance USE {material} geometry IndexedFaceSet {{ coord Coordinate {{ point [ {points} ] }} coordIndex [ {indices} ] creaseAngle 0.6 solid TRUE }} }}"


def rounded_rectangle(hx: float,hy: float,r: float) -> list[tuple[float,float]]:
    points=[]
    for cx,cy,start in ((hx-r,hy-r,0),(-hx+r,hy-r,90),(-hx+r,-hy+r,180),(hx-r,-hy+r,270)):
        for i in range(7):
            a=math.radians(start+90*i/6)
            points.append((cx+r*math.cos(a),cy+r*math.sin(a)))
    return points


def rounded_box(label: str, length: float,width: float, z0: float,z1: float,radius: float,mat: str) -> str:
    hx,hy=length/2,width/2
    radius=min(radius,hx*0.35,hy*0.35,(z1-z0)*0.4)
    profile=[(z0,radius),(z0+radius*0.25,radius*0.65),(z0+radius*0.65,radius*0.25),
             (z0+radius,0),(z1-radius,0),(z1-radius*0.65,radius*0.25),
             (z1-radius*0.25,radius*0.65),(z1,radius)]
    vertices=[]
    for z,inset in profile:
        vertices += [(x,y,z) for x,y in rounded_rectangle(hx-inset,hy-inset,max(radius-inset,0.015))]
    ring=28
    faces=[]
    for j in range(len(profile)-1):
        for i in range(ring):
            a=j*ring+i;b=j*ring+(i+1)%ring
            faces.append((a,b,b+ring,a+ring))
    faces.append(tuple(reversed(range(ring))))
    faces.append(tuple((len(profile)-1)*ring+i for i in range(ring)))
    return mesh(label,vertices,faces,mat)


def pin(label: str,x: float,r: float,z0: float,z1: float) -> str:
    n=20
    vertices=[(x+r*math.cos(2*math.pi*i/n),r*math.sin(2*math.pi*i/n),z) for z in (z0,z1) for i in range(n)]
    faces=[(i,(i+1)%n,(i+1)%n+n,i+n) for i in range(n)]
    faces += [tuple(reversed(range(n))),tuple(range(n,2*n))]
    return mesh(label,vertices,faces,"TIN")


def model(geometry: tuple[float,float,float,float]) -> str:
    length,width,height,pitch=geometry
    r=min(0.42,width*0.12,height*0.08)
    lead_radius=(0.55 if pitch<=7.5 else 0.75 if pitch<=15 else 0.9)/2
    # Body is seated at z=0; leads project below the PCB for a credible viewer model.
    pieces=["#VRML V2.0 utf8",
            "DEF CASE Appearance { material Material { diffuseColor 0.025 0.32 0.67 specularColor 0.12 0.18 0.24 shininess 0.42 } }",
            "DEF LID Appearance { material Material { diffuseColor 0.035 0.37 0.72 specularColor 0.12 0.19 0.27 shininess 0.45 } }",
            "DEF SEAM Appearance { material Material { diffuseColor 0.012 0.19 0.43 specularColor 0.06 0.10 0.15 shininess 0.24 } }",
            "DEF TIN Appearance { material Material { diffuseColor 0.68 0.71 0.73 specularColor 0.38 0.39 0.41 shininess 0.72 } }",
            rounded_box("moulded blue case",length,width,0,height-r*0.28,r,"CASE"),
            rounded_box("fine lid seam",length-0.11,width-0.11,height-r*0.64,height-r*0.34,min(r*0.3,0.09),"SEAM"),
            rounded_box("integral rounded lid",length-0.06,width-0.06,height-r*0.53,height,r*0.55,"LID")]
    for i,x in enumerate((-pitch/2,pitch/2),1):
        pieces.append(pin(f"tinned lead {i}",x,lead_radius,-3.5,0.35))
    return "\n".join(pieces)+"\n"


def atomic_install(files: list[tuple[Path,Path]]) -> None:
    with tempfile.TemporaryDirectory(prefix="pl-film-backup-") as backup_text:
        backup=Path(backup_text)
        installed=[]
        try:
            for i,(source,dest) in enumerate(files):
                dest.parent.mkdir(parents=True,exist_ok=True)
                old=backup/str(i)
                if dest.exists():
                    shutil.copy2(dest,old)
                temp=dest.with_name(dest.name+".new")
                shutil.copy2(source,temp)
                os.replace(temp,dest)
                installed.append((dest,old))
        except Exception:
            for dest,old in reversed(installed):
                if old.exists():
                    shutil.copy2(old,dest)
                else:
                    dest.unlink(missing_ok=True)
            raise


def generate(stage: Path, install: bool=False) -> dict[str,int]:
    stage.mkdir(parents=True,exist_ok=True)
    rows=list(csv.DictReader((DATASET/"film_capacitors.csv").open(encoding="utf-8-sig",newline="")))
    pdfs={p.name.casefold():p for p in DATASET.glob("*.pdf")}
    selected=[]; rejected=[]; mpns={}
    for row in rows:
        mpn=clean(row["Mfr Part #"])
        if mpn in mpns:
            if row!=mpns[mpn]:
                raise ValueError(f"conflicting duplicate MPN {mpn}")
            continue
        mpns[mpn]=row
        try:
            selected.append(normalise(row))
        except ValueError as error:
            rejected.append((row,str(error)))
    geometries={p["geometry"] for p in selected}
    names=[p["symbol"] for p in selected]
    if len(names)!=len(set(names)):
        raise ValueError("symbol-name collision")
    original=LIBRARY.read_text(encoding="utf-8")
    existing=blocks(original)
    template=existing["Capacitor_Template"]
    existing_mpns={match.group(1): name for name,block in existing.items()
                   if (match:=re.search(r'\(property "MPN" "([^"]+)"',block))}
    append=[]; copies={}; skipped=0
    for part in sorted(selected,key=lambda p:str(p["symbol"]).casefold()):
        url=clean(part["row"]["Datasheet"])
        ref,pdf=pdf_reference(url,pdfs)
        generated=symbol_block(part,template,ref)
        name=str(part["symbol"])
        if str(part["mpn"]) in existing_mpns and existing_mpns[str(part["mpn"])] != name:
            part["existing_symbol"]=existing_mpns[str(part["mpn"])]
            skipped+=1
        elif name in existing:
            if existing[name]!=generated:
                raise ValueError(f"conflicting existing symbol {name}")
            skipped+=1
        else:
            append.append(generated)
        if pdf:
            copies[pdf.name]=pdf
    if append:
        close=len(original.rstrip())-1
        library=original[:close].rstrip()+"\n"+"\n".join(append)+"\n)\n"
    else:
        library=original
    if sexpr_end(library,library.index("("))!=len(library.rstrip()):
        raise ValueError("generated symbol library unbalanced")
    files=[]
    def prepare(relative: str,contents: str,dest: Path):
        local=stage/relative
        local.parent.mkdir(parents=True,exist_ok=True)
        local.write_text(contents,encoding="utf-8",newline="\n")
        if not dest.exists() or dest.read_bytes()!=local.read_bytes():
            files.append((local,dest))
    prepare("PL Capacitor Film.kicad_sym",library,LIBRARY)
    for geom in sorted(geometries):
        name=model_name(geom)
        fp=footprint(geom)
        if sexpr_end(fp,fp.index("("))!=len(fp.rstrip()):
            raise ValueError(f"unbalanced footprint {name}")
        prepare("pretty/"+name+".kicad_mod",fp,FOOTPRINT_DIR/(name+".kicad_mod"))
        prepare("models/"+name+".wrl",model(geom),MODEL_DIR/(name+".wrl"))
    manifest=stage/"manifest.csv"
    with manifest.open("w",encoding="utf-8-sig",newline="") as output:
        fields=["status","reason","symbol","manufacturer","mpn","series","capacitance","rated_voltage","footprint","model","datasheet","source","dimensions_mm"]
        writer=csv.DictWriter(output,fieldnames=fields)
        writer.writeheader()
        for part in selected:
            row=part["row"];ref,pdf=pdf_reference(clean(row["Datasheet"]),pdfs)
            l,w,h,p=part["geometry"]
            existing_name=part.get("existing_symbol")
            if existing_name:
                existing_block=existing[str(existing_name)]
                current_fp=re.search(r'\(property "Footprint" "([^"]+)"',existing_block)
                footprint_name=current_fp.group(1) if current_fp else ""
                model_file=""
            else:
                footprint_name=str(part["footprint"])
                model_file=footprint_name+".wrl"
            writer.writerow(dict(status="existing_symbol" if existing_name else "generated",reason="pre-existing MPN preserved; original footprint retained" if existing_name else "",symbol=existing_name or part["symbol"],manufacturer=row["Mfr"],mpn=part["mpn"],series=row["Series"],capacitance=part["value"],rated_voltage=part["rated"],footprint=footprint_name,model=model_file,datasheet=ref,source="local PDF" if pdf else "datasheet URL",dimensions_mm=f"L{l} W{w} H{h} P{p}"))
        for row,reason in rejected:
            writer.writerow(dict(status="excluded",reason=reason,manufacturer=row["Mfr"],mpn=row["Mfr Part #"],series=row["Series"],datasheet=clean(row["Datasheet"])))
    if not MANIFEST.exists() or MANIFEST.read_bytes()!=manifest.read_bytes():
        files.append((manifest,MANIFEST))
    report=("# Boxed radial film capacitor generation\n\n"
            f"- Source rows: {len(rows)}\n- Generated symbols: {sum(not p.get('existing_symbol') for p in selected)}\n"
            f"- Shared footprints and 3D models: {len(geometries)} each\n"
            f"- Excluded rows: {len(rejected)}\n- Pre-existing MPNs preserved: {sum(bool(p.get('existing_symbol')) for p in selected)}\n"
            f"- Local datasheets: {len(copies)} distinct PDFs\n\n"
            "Exclusions and datasheet sources are listed per MPN in the manifest. "
            "The 3D model is a neutral unmarked blue moulded box. Pin diameter and drill follow a "
            "conservative pitch-based fallback (0.55/0.75/0.9 mm leads, drill 0.8/1.0/1.2 mm); "
            "verify against the manufacturer drawing before production. No non-boxed, SMD, "
            "multi-lead, or undimensioned packages were assigned a boxed footprint.\n")
    prepare("report.md",report,REPORT)
    for name,pdf in copies.items():
        dest=FILM_SYMBOLS/"Datasheets"/name
        if not dest.exists():
            files.append((pdf,dest))
    if install:
        atomic_install(files)
    return {"rows":len(rows),"symbols":len(selected),"geometries":len(geometries),"excluded":len(rejected),"changed_files":len(files)}


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage",type=Path,required=True)
    parser.add_argument("--install",action="store_true")
    args=parser.parse_args()
    print(generate(args.stage,args.install))


if __name__=="__main__":
    main()
