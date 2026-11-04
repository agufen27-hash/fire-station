"""
Planilla mensual de presencia del personal (Libro de Guardia) en Excel, para
justificar la presencia de los bomberos ante la Aseguradora / ART.

    ruta = generar_planilla_mensual(2026, 11, registros)

Mismo generador y estilo que el reporte gerencial
(app/reports/estadisticas_generator.py): tipografía oficial Arial 10 pt
(app/reports/tipografia.py), encabezados en negrita sobre rojo institucional,
bordes finos, totales con fórmulas SUM, encabezado inmovilizado y hoja
apaisada ajustada al ancho. Dos hojas:
  1. "Planilla de Presencia": un renglón por viaje / asistencia del mes, con
     bloque de firmas al pie.
  2. "Resumen por Bombero": registros, viajes, asistencias, días con
     presencia y horas computadas por persona.

Las horas solo se computan en registros cerrados (con egreso / regreso); los
abiertos figuran con su estado y sin horas.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.worksheet.worksheet import Worksheet

from app.reports.estadisticas_generator import (
    FORMATO_ENTERO,
    FORMATO_HORAS,
    INSTITUCION,
    OUTPUT_DIR,
    _autoajustar,
    _escribir_tabla,
    guardar_libro,
)
from app.reports.tipografia import FUENTE_PLANILLAS, TAMANO_PT
from app.services.libro_guardia_service import (
    ESTADO_EN_CURSO,
    ESTADO_SIN_EGRESO,
    RegistroBitacora,
    fin_de_mes,
    inicio_de_mes,
)

MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre"]
FORMATO_FECHA = "dd/mm/yyyy"
FORMATO_FECHA_HORA = "dd/mm/yyyy hh:mm"
ESTADOS_LEGIBLES = {ESTADO_EN_CURSO: "En curso", ESTADO_SIN_EGRESO: "Sin egreso", "FINALIZADO": "Finalizado"}

_FUENTE = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT)
_FUENTE_TITULO = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT + 2, bold=True)
_FUENTE_NEGRITA = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT, bold=True)
_FUENTE_SUBTITULO = Font(name=FUENTE_PLANILLAS, size=TAMANO_PT, italic=True, color="595959")
_LINEA_FIRMA = Border(top=Side(style="thin", color="000000"))

ENCABEZADOS_PRESENCIA = ["Fecha", "Legajo", "Bombero", "Jerarquía", "Actividad / Destino", "Vehículo",
                         "Ingreso / Salida", "Egreso / Regreso", "Horas", "Estado", "Registro"]
COLS_FECHA = {1: FORMATO_FECHA, 7: FORMATO_FECHA_HORA, 8: FORMATO_FECHA_HORA}
ENCABEZADOS_RESUMEN = ["Legajo", "Bombero", "Jerarquía", "Registros", "Viajes", "Asistencias",
                       "Días con presencia", "Horas computadas"]


def nombre_mes(anio: int, mes: int) -> str:
    return f"{MESES[mes - 1]} {anio}"


def _sin_tz(valor: Optional[datetime]) -> Optional[datetime]:
    """openpyxl no admite datetimes con zona: se escribe la hora del cuartel."""
    return valor.replace(tzinfo=None) if valor is not None else None


def _horas(r: RegistroBitacora) -> Optional[float]:
    return round(r.minutos / 60, 2) if r.fin is not None and r.minutos is not None else None


def _encabezado(hoja: Worksheet, titulo: str, anio: int, mes: int) -> int:
    desde = inicio_de_mes(anio, mes)
    ultimo_dia = fin_de_mes(anio, mes) - timedelta(days=1)
    hoja.cell(row=1, column=1, value=titulo).font = _FUENTE_TITULO
    hoja.cell(row=2, column=1, value=INSTITUCION).font = _FUENTE
    hoja.cell(row=3, column=1, value=(
        f"Período: {nombre_mes(anio, mes)} ({desde:%d/%m/%Y} al {ultimo_dia:%d/%m/%Y})  ·  "
        f"Generado el {datetime.now():%d/%m/%Y %H:%M}")).font = _FUENTE_SUBTITULO
    hoja.cell(row=4, column=1, value=(
        "Constancia de presencia del personal en actividades del cuerpo (cuartel, capacitaciones, "
        "guardias y comisiones / viajes), para su presentación ante la Aseguradora / ART.")).font = _FUENTE
    return 6


def _config_impresion(hoja: Worksheet, fila_encabezado: int, pie: str) -> None:
    hoja.freeze_panes = hoja.cell(row=fila_encabezado + 1, column=1)
    hoja.print_title_rows = f"{fila_encabezado}:{fila_encabezado}"
    hoja.page_setup.orientation = "landscape"
    hoja.page_setup.paperSize = hoja.PAPERSIZE_A4
    hoja.page_setup.fitToWidth = 1
    hoja.page_setup.fitToHeight = 0
    hoja.sheet_properties.pageSetUpPr.fitToPage = True
    hoja.oddFooter.left.text = pie
    hoja.oddFooter.right.text = "Página &P de &N"
    for item in (hoja.oddFooter.left, hoja.oddFooter.right):
        item.font = f"{FUENTE_PLANILLAS},Regular"
        item.size = TAMANO_PT


def _centrar_fechas(hoja: Worksheet, primera: int, ultima: int, columnas: Dict[int, str]) -> None:
    """Fechas y horas centradas (criterio de app/reports/tipografia.py)."""
    for fila in range(primera, ultima + 1):
        for col, formato in columnas.items():
            celda = hoja.cell(row=fila, column=col)
            if isinstance(celda.value, datetime):
                celda.number_format = formato
            celda.alignment = Alignment(horizontal="center", vertical="center")


def _bloque_firmas(hoja: Worksheet, fila: int) -> None:
    for col, texto in ((2, "Firma y sello — Jefe de Cuerpo"), (6, "Firma y sello — Presidente")):
        for c in range(col, col + 3):
            hoja.cell(row=fila, column=c).border = _LINEA_FIRMA
        celda = hoja.cell(row=fila + 1, column=col, value=texto)
        celda.font = _FUENTE_NEGRITA


def _hoja_presencia(hoja: Worksheet, registros: Sequence[RegistroBitacora], anio: int, mes: int) -> None:
    hoja.title = "Planilla de Presencia"
    fila = _encabezado(hoja, f"PLANILLA MENSUAL DE PRESENCIA DEL PERSONAL — {nombre_mes(anio, mes).upper()}",
                       anio, mes)
    filas = [
        (_sin_tz(r.inicio.replace(hour=0, minute=0, second=0, microsecond=0)), r.legajo or "—", r.bombero,
         r.jerarquia or "—", r.actividad, r.vehiculo or "—", _sin_tz(r.inicio), _sin_tz(r.fin),
         _horas(r), ESTADOS_LEGIBLES.get(r.estado, r.estado.title()), r.metodo_legible)
        for r in sorted(registros, key=lambda x: (x.inicio, x.bombero))
    ]
    formatos = [None] * 8 + [FORMATO_HORAS, None, None]
    totales = ["Totales", "", f"{len({r.bombero_id for r in registros})} bombero(s)", "",
               f"{len(filas)} registro(s)", "", "", "", "SUM", "", ""]
    ultima = _escribir_tabla(hoja, fila, ENCABEZADOS_PRESENCIA, filas, formatos, totales)
    if filas:
        _centrar_fechas(hoja, fila + 1, fila + len(filas), COLS_FECHA)
    _autoajustar(hoja, fila)
    for col, ancho in ((1, 12), (7, 17), (8, 17)):
        hoja.column_dimensions[hoja.cell(row=fila, column=col).column_letter].width = ancho

    nota = hoja.cell(row=ultima + 2, column=1, value=(
        "Horas: se computan solo en registros cerrados (con egreso / regreso). "
        "Registro: GPS = ubicación validada en el cuartel por el bot de WhatsApp; QR = código del evento; "
        "Escritorio = cargado por el operador de guardia."))
    nota.font = _FUENTE_SUBTITULO
    _bloque_firmas(hoja, ultima + 6)
    _config_impresion(hoja, fila, f"Fire Station — Planilla de presencia {nombre_mes(anio, mes)}")


def resumen_por_bombero(registros: Sequence[RegistroBitacora]) -> List[tuple]:
    grupos: Dict[int, List[RegistroBitacora]] = defaultdict(list)
    for r in registros:
        grupos[r.bombero_id].append(r)
    filas = []
    for regs in grupos.values():
        base = regs[0]
        filas.append((
            base.legajo or "—", base.bombero, base.jerarquia or "—", len(regs),
            sum(1 for r in regs if r.es_viaje), sum(1 for r in regs if not r.es_viaje),
            len({r.inicio.date() for r in regs}),
            round(sum(_horas(r) or 0 for r in regs), 2),
        ))
    return sorted(filas, key=lambda f: f[1])


def _hoja_resumen(hoja: Worksheet, registros: Sequence[RegistroBitacora], anio: int, mes: int) -> None:
    fila = _encabezado(hoja, f"RESUMEN DE PRESENCIA POR BOMBERO — {nombre_mes(anio, mes).upper()}", anio, mes)
    filas = resumen_por_bombero(registros)
    formatos = [None, None, None, FORMATO_ENTERO, FORMATO_ENTERO, FORMATO_ENTERO, FORMATO_ENTERO, FORMATO_HORAS]
    totales = ["Totales", f"{len(filas)} bombero(s)", "", "SUM", "SUM", "SUM", "", "SUM"]
    _escribir_tabla(hoja, fila, ENCABEZADOS_RESUMEN, filas, formatos, totales)
    _autoajustar(hoja, fila)
    _config_impresion(hoja, fila, f"Fire Station — Resumen de presencia {nombre_mes(anio, mes)}")


def construir_libro(registros: Sequence[RegistroBitacora], anio: int, mes: int) -> Workbook:
    wb = Workbook()
    _hoja_presencia(wb.active, registros, anio, mes)
    _hoja_resumen(wb.create_sheet("Resumen por Bombero"), registros, anio, mes)
    wb.properties.title = f"Planilla de presencia {nombre_mes(anio, mes)}"
    wb.properties.creator = "Fire Station"
    return wb


def ruta_planilla(anio: int, mes: int, carpeta: Optional[Path] = None) -> Path:
    carpeta = carpeta or OUTPUT_DIR
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / f"Planilla_presencia_ART_{anio}-{mes:02d}.xlsx"


def generar_planilla_mensual(anio: int, mes: int, registros: Sequence[RegistroBitacora],
                             carpeta: Optional[Path] = None) -> Path:
    return guardar_libro(construir_libro(registros, anio, mes), ruta_planilla(anio, mes, carpeta))
