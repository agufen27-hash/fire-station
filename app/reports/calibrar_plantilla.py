"""
Herramienta de calibración: vuelca celda por celda el contenido de una
plantilla .xlsx (y sus celdas combinadas) para poder ubicar rápido dónde va
cada dato y ajustar EXCEL_MAPPING_PCS / EXCEL_MAPPING_PCD2 en excel_generator.py.

Uso:
    python -m app.reports.calibrar_plantilla templates/PCS.xlsx
    python -m app.reports.calibrar_plantilla templates/PCD2.xlsx --hoja "Hoja1"
"""

from __future__ import annotations

import argparse
from pathlib import Path

from openpyxl import load_workbook


def calibrar(ruta: Path, nombre_hoja: str | None = None) -> None:
    wb = load_workbook(ruta, data_only=True)
    hoja = wb[nombre_hoja] if nombre_hoja else wb.active

    print(f"Archivo: {ruta}")
    print(f"Hojas disponibles: {wb.sheetnames}")
    print(f"Usando hoja: '{hoja.title}'  (dimensiones: {hoja.dimensions})")

    print("\n-- Celdas combinadas (merged) --")
    for rango in hoja.merged_cells.ranges:
        print(f"  {rango}")

    print("\n-- Celdas con contenido --")
    for fila in hoja.iter_rows():
        for celda in fila:
            if celda.value not in (None, ""):
                print(f"  {celda.coordinate}: {celda.value!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ruta", help="Ruta al .xlsx a inspeccionar (ej. templates/PCS.xlsx)")
    parser.add_argument("--hoja", default=None, help="Nombre de hoja a inspeccionar (por defecto, la primera)")
    args = parser.parse_args()

    ruta = Path(args.ruta)
    if not ruta.exists():
        raise SystemExit(f"No existe el archivo: {ruta}")

    calibrar(ruta, args.hoja)


if __name__ == "__main__":
    main()
