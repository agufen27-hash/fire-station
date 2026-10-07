"""
Tipografía unificada de las planillas oficiales (PCS / PCD2 en Excel) y del
Informe Técnico en PDF:

- Fuente: Arial 10 pt (la de las plantillas oficiales del cuerpo). Excel
  la toma de FUENTE_PLANILLAS / TAMANO_PT a través de
  app/reports/excel_generator.py (FUENTE_EXCEL, TAMANO_EXCEL_PT) y el PDF
  de app/reports/pdf_generator.py, así ambos documentos coinciden.
- Texto y descripciones: alineado a la IZQUIERDA.
- Números, fechas y horas: CENTRADOS (`es_numerico` decide qué es qué).

Arial viene con Windows; en el PDF (QTextDocument) la pila CSS cae en
Helvetica / sans-serif si no estuviera instalada.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any

FUENTE_PLANILLAS = "Arial"
TAMANO_PT = 10
FALLBACKS_CSS = ("Helvetica",)

# "12/03/2026", "08:45", "003/2026", "12", "4,5 ha", "-31.5, -64.2"
_ES_NUMERICO = re.compile(
    r"^\s*(\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}:\d{2}(:\d{2})?|\d+/\d{4}|[-+]?\d+([.,]\d+)?(\s*(ha|h|min))?"
    r"|[-+]?\d+[.,]\d+\s*,\s*[-+]?\d+[.,]\d+)\s*$"
)


def fuente_planillas() -> str:
    """Familia de las planillas y del PDF (Arial)."""
    return FUENTE_PLANILLAS


def familia_css() -> str:
    """Pila CSS para QTextDocument: Arial y sus fallbacks."""
    return ", ".join(f"'{f}'" for f in (FUENTE_PLANILLAS, *FALLBACKS_CSS)) + ", sans-serif"


def es_numerico(valor: Any) -> bool:
    """Números, fechas y horas (van centrados); el resto es texto (izquierda)."""
    if isinstance(valor, bool):
        return False
    if isinstance(valor, (int, float, date, datetime, time)):
        return True
    return bool(_ES_NUMERICO.match(str(valor))) if valor not in (None, "") else False
