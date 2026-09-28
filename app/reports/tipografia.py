"""
Tipografía unificada de las planillas (PCS / PCD2 / Informe PDF):

- Texto y descripciones: 'Aptos' 12 pt, alineado a la IZQUIERDA.
- Números, fechas y horas: 'Aptos' 12 pt, CENTRADOS.

Aptos viene con Office 365 reciente; si no está instalada se usa Segoe UI
(Windows) y, como último recurso, Helvetica. La detección mira las carpetas
de fuentes del sistema y del usuario, así no depende de que exista una
QApplication (los generadores también corren desde scripts).
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any

TAMANO_PT = 12
PREFERIDAS = (("Aptos", "aptos"), ("Segoe UI", "segoeui"), ("Helvetica", "helvetica"))
FALLBACK_FINAL = "Helvetica"

# "12/03/2026", "08:45", "003/2026", "12", "4,5 ha", "-31.5, -64.2"
_ES_NUMERICO = re.compile(
    r"^\s*(\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}:\d{2}(:\d{2})?|\d+/\d{4}|[-+]?\d+([.,]\d+)?(\s*(ha|h|min))?"
    r"|[-+]?\d+[.,]\d+\s*,\s*[-+]?\d+[.,]\d+)\s*$"
)


def _carpetas_fuentes() -> list:
    carpetas = []
    windir = os.environ.get("WINDIR")
    if windir:
        carpetas.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        carpetas.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    carpetas += [Path("/usr/share/fonts"), Path.home() / ".fonts", Path("/Library/Fonts"),
                 Path("/System/Library/Fonts")]
    return [c for c in carpetas if c.is_dir()]


@lru_cache(maxsize=1)
def fuente_planillas() -> str:
    """Primera fuente instalada de PREFERIDAS (Aptos -> Segoe UI -> Helvetica)."""
    archivos = set()
    for carpeta in _carpetas_fuentes():
        try:
            archivos.update(f.name.lower() for f in carpeta.rglob("*") if f.suffix.lower() in (".ttf", ".otf", ".ttc"))
        except OSError:
            continue
    for familia, prefijo in PREFERIDAS:
        if any(nombre.startswith(prefijo) for nombre in archivos):
            return familia
    return FALLBACK_FINAL


def familia_css() -> str:
    """Pila CSS para QTextDocument: la elegida y los fallbacks."""
    familias = dict.fromkeys([fuente_planillas(), *(f for f, _ in PREFERIDAS)])
    return ", ".join(f"'{f}'" for f in familias) + ", sans-serif"


def es_numerico(valor: Any) -> bool:
    """Números, fechas y horas (van centrados); el resto es texto (izquierda)."""
    if isinstance(valor, bool):
        return False
    if isinstance(valor, (int, float, date, datetime, time)):
        return True
    return bool(_ES_NUMERICO.match(str(valor))) if valor not in (None, "") else False
