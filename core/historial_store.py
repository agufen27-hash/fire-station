"""
Persistencia local del histórico de servicios.

Implementación mínima en JSON (data/historial.json) para que la GUI funcione
de punta a punta desde el primer arranque. La interfaz pública (guardar,
listar, siguiente_n_siniestro) es la que va a exponer más adelante el módulo
de base de datos SQLite, así que la pestaña "Historial de Salidas" no debería
necesitar cambios cuando se haga ese reemplazo.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import List

from core.models import RegistroServicio

BASE_DIR = Path(__file__).resolve().parent.parent
HISTORIAL_PATH = BASE_DIR / "data" / "historial.json"


def _leer_todos() -> List[dict]:
    if not HISTORIAL_PATH.exists():
        return []
    with HISTORIAL_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _escribir_todos(registros: List[dict]) -> None:
    HISTORIAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORIAL_PATH.open("w", encoding="utf-8") as f:
        json.dump(registros, f, ensure_ascii=False, indent=2)


def listar_registros() -> List[RegistroServicio]:
    return [RegistroServicio.from_dict(d) for d in _leer_todos()]


def guardar_registro(registro: RegistroServicio) -> RegistroServicio:
    """Inserta (id nuevo) o actualiza (id existente) un registro y lo persiste."""
    registros = _leer_todos()

    if registro.id is None:
        registro.id = max([r.get("id", 0) for r in registros], default=0) + 1
        registros.append(registro.to_dict())
    else:
        for i, r in enumerate(registros):
            if r.get("id") == registro.id:
                registros[i] = registro.to_dict()
                break
        else:
            registros.append(registro.to_dict())

    _escribir_todos(registros)
    return registro


def siguiente_n_siniestro(anio: int | None = None) -> str:
    """Sugiere el próximo N° de siniestro con formato 'NNN/AAAA', reiniciando
    el correlativo cada año calendario."""
    anio = anio or date.today().year
    registros = _leer_todos()

    max_correlativo = 0
    for r in registros:
        n = r.get("pedido_socorro", {}).get("n_siniestro", "")
        if "/" not in n:
            continue
        numero, _, anio_registro = n.partition("/")
        if anio_registro.strip() == str(anio) and numero.strip().isdigit():
            max_correlativo = max(max_correlativo, int(numero.strip()))

    return f"{max_correlativo + 1:03d}/{anio}"
