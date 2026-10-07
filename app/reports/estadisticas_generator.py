"""
Reporte gerencial en Excel (openpyxl) a partir de
app/services/statistics_service.calcular_estadisticas.

    ruta = generar_reporte_estadisticas(date(2026, 1, 1), date(2026, 3, 31))

Libro con tres hojas, con la tipografía oficial de las planillas
(app/reports/tipografia.py: Arial 10 pt):
  1. "Resumen General": período, servicios, horas, salidas de móviles,
     horas-hombre y servicios por tipo de siniestro.
  2. "Estadísticas por Bombero": Legajo, Nombre, Servicios (escena / base /
     total), Horas Cuartel, Horas en Escena, Horas Totales.
  3. "Estadísticas por Vehículo": Móvil, Tipo, Cantidad de Salidas, Horas de
     Servicio, Km recorridos ("sin dato" mientras no se registre odómetro).

Encabezados en negrita sobre fondo institucional, columnas autoajustadas,
encabezado inmovilizado y filas de totales en negrita con fórmulas SUM (si
alguien corrige una celda en Excel, el total se recalcula).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.paths import get_writable_dir
from app.reports.tipografia import FUENTE_PLANILLAS, TAMANO_PT
from app.services.statistics_service import ReporteEstadisticas, calcular_estadisticas

OUTPUT_DIR = get_writable_dir("output") / "reportes"
INSTITUCION = 'Sociedad de Bomberos Voluntarios "Osvaldo R. Rossi" — Adelia María (C 59 / R 3)'

FORMATO_HORAS = "#,##0.00"
FORMATO_ENTERO = "#,##0"
COLOR_ENCABEZADO = "B71C1C"   # rojo institucional
COLOR_TOTALES = "F2F2F2"
ANCHO_MIN, ANCHO_MAX = 8, 60

_FUENTE = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT)
_FUENTE_NEGRITA = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT, bold=True)
_FUENTE_ENCABEZADO = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT, bold=True, color="FFFFFF")
_FUENTE_TITULO = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT + 2, bold=True)
_FUENTE_SUBTITULO = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT, italic=True, color="595959")
_FONDO_ENCABEZADO = PatternFill("solid", fgColor=COLOR_ENCABEZADO)
_FONDO_TOTALES = PatternFill("solid", fgColor=COLOR_TOTALES)
_LINEA = Side(style="thin", color="BFBFBF")
_BORDE = Border(left=_LINEA, right=_LINEA, top=_LINEA, bottom=_LINEA)
_BORDE_TOTAL = Border(left=_LINEA, right=_LINEA, top=Side(style="medium", color="000000"), bottom=_LINEA)


# ---------------------------------------------------------------------------
# Helpers de estilo
# ---------------------------------------------------------------------------

def _encabezado_hoja(hoja: Worksheet, titulo: str, reporte: ReporteEstadisticas, columnas: int) -> int:
    """Título, institución y período. Devuelve la próxima fila libre."""
    hoja.cell(row=1, column=1, value=titulo).font = _FUENTE_TITULO
    hoja.cell(row=2, column=1, value=INSTITUCION).font = _FUENTE
    hoja.cell(row=3, column=1, value=(
        f"Período: {reporte.fecha_desde:%d/%m/%Y} al {reporte.fecha_hasta:%d/%m/%Y}  ·  "
        f"Generado el {reporte.generado_en:%d/%m/%Y %H:%M}")).font = _FUENTE_SUBTITULO
    # Sin combinar celdas: el texto largo se extiende sobre las vecinas vacías
    # (una celda combinada lo recortaría al ancho de la tabla).
    return 5


def _escribir_tabla(hoja: Worksheet, fila_inicio: int, encabezados: Sequence[str], filas: List[Sequence[Any]],
                    formatos: Sequence[Optional[str]], totales: Optional[Sequence[Any]] = None) -> int:
    """Encabezado + filas + (opcional) fila de totales. En `totales`, "SUM"
    pone la fórmula de suma de la columna. Devuelve la última fila escrita."""
    for col, texto in enumerate(encabezados, start=1):
        celda = hoja.cell(row=fila_inicio, column=col, value=texto)
        celda.font = _FUENTE_ENCABEZADO
        celda.fill = _FONDO_ENCABEZADO
        celda.border = _BORDE
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    primera = fila_inicio + 1
    for i, valores in enumerate(filas):
        for col, valor in enumerate(valores, start=1):
            celda = hoja.cell(row=primera + i, column=col, value=valor)
            celda.font = _FUENTE
            celda.border = _BORDE
            formato = formatos[col - 1] if col - 1 < len(formatos) else None
            if formato and isinstance(valor, (int, float)):
                celda.number_format = formato
            numerico = isinstance(valor, (int, float))
            celda.alignment = Alignment(horizontal="right" if numerico else "left", vertical="center")
    ultima = primera + len(filas) - 1
    if totales is not None:
        fila_total = ultima + 1
        for col, valor in enumerate(totales, start=1):
            if valor == "SUM":
                letra = get_column_letter(col)
                valor = f"=SUM({letra}{primera}:{letra}{ultima})" if filas else 0
            celda = hoja.cell(row=fila_total, column=col, value=valor)
            celda.font = _FUENTE_NEGRITA
            celda.fill = _FONDO_TOTALES
            celda.border = _BORDE_TOTAL
            formato = formatos[col - 1] if col - 1 < len(formatos) else None
            if formato and valor not in (None, ""):
                celda.number_format = formato
            celda.alignment = Alignment(horizontal="left" if col == 1 else "right", vertical="center")
        ultima = fila_total
    return ultima


def _texto_visible(valor: Any, formato: Optional[str]) -> str:
    if valor is None:
        return ""
    if isinstance(valor, str) and valor.startswith("="):
        return "0000000.00"  # fórmula: se estima el ancho de un total
    if isinstance(valor, float):
        return f"{valor:,.2f}"
    return str(valor)


def _autoajustar(hoja: Worksheet, desde_fila: int) -> None:
    """Ancho de cada columna según su contenido (sin contar el título combinado)."""
    anchos: dict = {}
    for fila in hoja.iter_rows(min_row=desde_fila):
        for celda in fila:
            texto = _texto_visible(celda.value, celda.number_format)
            if texto:
                anchos[celda.column_letter] = max(anchos.get(celda.column_letter, 0), max(map(len, texto.split("\n"))))
    for letra, ancho in anchos.items():
        hoja.column_dimensions[letra].width = min(max(ancho + 3, ANCHO_MIN), ANCHO_MAX)


def _config_impresion(hoja: Worksheet, fila_encabezado: int) -> None:
    hoja.freeze_panes = hoja.cell(row=fila_encabezado + 1, column=1)
    hoja.print_title_rows = f"{fila_encabezado}:{fila_encabezado}"
    hoja.page_setup.orientation = "landscape"
    hoja.page_setup.fitToWidth = 1
    hoja.page_setup.fitToHeight = 0
    hoja.sheet_properties.pageSetUpPr.fitToPage = True
    hoja.oddFooter.left.text = "Fire Station — Reporte gerencial"
    hoja.oddFooter.right.text = "Página &P de &N"
    for item in (hoja.oddFooter.left, hoja.oddFooter.right):
        item.font = f"{FUENTE_PLANILLAS},Regular"
        item.size = TAMANO_PT


# ---------------------------------------------------------------------------
# Hojas
# ---------------------------------------------------------------------------

def _hoja_resumen(hoja: Worksheet, r: ReporteEstadisticas) -> None:
    hoja.title = "Resumen General"
    fila = _encabezado_hoja(hoja, "Reporte gerencial — Resumen General", r, 2)
    indicadores = [
        ("Servicios cerrados en el período", r.servicios),
        ("Servicios en curso (no incluidos)", r.servicios_en_curso),
        ("Horas de servicio (suma de cada parte)", round(r.horas_servicio, 2)),
        ("Salidas de móviles", r.salidas_moviles),
        ("Horas operativas de móviles", round(sum(m.horas for m in r.moviles), 2)),
        ("Bomberos que participaron", r.bomberos_participantes),
        ("Horas-hombre (cuartel + escena)", round(r.horas_hombre, 2)),
    ]
    encabezado_ind = fila
    fila = _escribir_tabla(hoja, fila, ["Indicador", "Valor"], indicadores, [None, FORMATO_HORAS])
    for i, (_, valor) in enumerate(indicadores, start=encabezado_ind + 1):
        if isinstance(valor, int):
            hoja.cell(row=i, column=2).number_format = FORMATO_ENTERO

    fila += 2
    filas_tipo = [(tipo, cantidad) for tipo, cantidad in r.por_tipo] or [("Sin servicios en el período", 0)]
    _escribir_tabla(hoja, fila, ["Servicios por tipo de siniestro", "Cantidad"], filas_tipo,
                    [None, FORMATO_ENTERO], totales=["Total", "SUM"])
    _autoajustar(hoja, encabezado_ind)
    _config_impresion(hoja, encabezado_ind)


def _hoja_bomberos(hoja: Worksheet, r: ReporteEstadisticas) -> None:
    encabezados = ["Legajo", "Apellido y Nombre", "Servicios en Escena", "Servicios en Base",
                   "Total Servicios", "Horas Cuartel", "Horas en Escena", "Horas Totales"]
    fila = _encabezado_hoja(hoja, "Estadísticas por Bombero", r, len(encabezados))
    filas = [
        (b.legajo, b.nombre, b.servicios_escena, b.servicios_cuartel, b.total_servicios,
         round(b.horas_cuartel, 2), round(b.horas_escena, 2), round(b.horas_totales, 2))
        for b in r.bomberos
    ]
    formatos = [None, None, FORMATO_ENTERO, FORMATO_ENTERO, FORMATO_ENTERO, FORMATO_HORAS, FORMATO_HORAS, FORMATO_HORAS]
    totales = ["Totales", f"{len(filas)} bombero(s)", "SUM", "SUM", "SUM", "SUM", "SUM", "SUM"]
    _escribir_tabla(hoja, fila, encabezados, filas, formatos, totales)
    _autoajustar(hoja, fila)
    _config_impresion(hoja, fila)


def _hoja_vehiculos(hoja: Worksheet, r: ReporteEstadisticas) -> None:
    encabezados = ["Móvil", "Tipo", "Cantidad de Salidas", "Horas de Servicio", "Km recorridos"]
    fila = _encabezado_hoja(hoja, "Estadísticas por Vehículo", r, len(encabezados))
    filas = [
        (m.nombre, m.tipo, m.salidas, round(m.horas, 2),
         round(m.km_recorridos, 1) if m.km_recorridos is not None else "sin dato")
        for m in r.moviles
    ]
    hay_km = any(m.km_recorridos is not None for m in r.moviles)
    totales = ["Totales", f"{len(filas)} móvil(es)", "SUM", "SUM", "SUM" if hay_km else "—"]
    ultima = _escribir_tabla(hoja, fila, encabezados, filas,
                             [None, None, FORMATO_ENTERO, FORMATO_HORAS, "#,##0.0"], totales)
    if not hay_km:
        nota = hoja.cell(row=ultima + 2, column=1, value=(
            "Km recorridos: la base todavía no registra el odómetro de salida / regreso de los móviles."))
        nota.font = _FUENTE_SUBTITULO
    _autoajustar(hoja, fila)
    _config_impresion(hoja, fila)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def construir_libro(reporte: ReporteEstadisticas) -> Workbook:
    wb = Workbook()
    _hoja_resumen(wb.active, reporte)
    _hoja_bomberos(wb.create_sheet("Estadísticas por Bombero"), reporte)
    _hoja_vehiculos(wb.create_sheet("Estadísticas por Vehículo"), reporte)
    wb.properties.title = "Reporte gerencial Fire Station"
    wb.properties.creator = "Fire Station"
    return wb


def ruta_reporte(fecha_desde: date, fecha_hasta: date, carpeta: Optional[Path] = None) -> Path:
    carpeta = carpeta or OUTPUT_DIR
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / f"Reporte_gerencial_{fecha_desde:%Y%m%d}_{fecha_hasta:%Y%m%d}.xlsx"


def guardar_libro(wb: Workbook, ruta: Path) -> Path:
    """Guarda; si el archivo está abierto en Excel (bloqueado), usa un nombre
    con la hora para no perder el reporte."""
    try:
        wb.save(ruta)
        return ruta
    except PermissionError:
        alternativa = ruta.with_name(f"{ruta.stem}_{datetime.now():%H%M%S}{ruta.suffix}")
        wb.save(alternativa)
        return alternativa


def generar_reporte_estadisticas(fecha_desde: date, fecha_hasta: date,
                                 carpeta: Optional[Path] = None) -> tuple:
    """Calcula y exporta. Devuelve (ruta del .xlsx, ReporteEstadisticas)."""
    reporte = calcular_estadisticas(fecha_desde, fecha_hasta)
    ruta = guardar_libro(construir_libro(reporte), ruta_reporte(fecha_desde, fecha_hasta, carpeta))
    return ruta, reporte
