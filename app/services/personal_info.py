"""
Grado / cargo del personal y filtro de mandos (Oficiales y Suboficiales).

- El grado sale de `Personal.jerarquia` (editable en "Personal y Unidades").
- El cargo sale del padrón oficial de RUBA (columna "Cargo": Jefe de Cuerpo,
  Sub-Jefe, Director de Capacitaciones...).

"Autorizó la salida" solo ofrece mandos: grados de Suboficial u Oficial, o
quien tenga un cargo de conducción en el padrón.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, Iterable, List, Optional

from app.core.catalogos import Bombero
from app.db import get_session
from app.models import Personal

# Palabras que identifican grados de Suboficial y de Oficial en los cuerpos
# de bomberos voluntarios (Cabo, Sargento, Suboficial..., Ayudante, Teniente,
# Capitán, Oficial..., Comandante...).
PALABRAS_MANDO = ("cabo", "sargento", "suboficial", "oficial", "ayudante", "subayudante",
                  "teniente", "capitan", "comandante", "jefe", "director")


def _normalizar(texto: Optional[str]) -> str:
    sin_acentos = "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))
    return " ".join(sin_acentos.lower().split())


def es_mando(jerarquia: Optional[str], cargo: Optional[str] = None) -> bool:
    if cargo:
        return True
    grado = _normalizar(jerarquia)
    return any(palabra in grado.split() or grado.startswith(palabra) for palabra in PALABRAS_MANDO)


def grado_y_cargo(jerarquia: Optional[str], cargo: Optional[str]) -> str:
    """'Sargento — Jefe de Cuerpo Activo', 'Bombero', o '' si no hay datos."""
    return " — ".join(x for x in ((jerarquia or "").strip(), (cargo or "").strip()) if x)


def jerarquias_por_id_ruba(ids: Iterable[int]) -> Dict[int, Optional[str]]:
    ids = [i for i in ids if i is not None]
    if not ids:
        return {}
    with get_session() as session:
        return {p.id_ruba: p.jerarquia for p in session.query(Personal).filter(Personal.id_ruba.in_(ids))}


def grado_de(bombero: Optional[Bombero], jerarquias: Optional[Dict[int, Optional[str]]] = None) -> str:
    if bombero is None:
        return ""
    jerarquia = (jerarquias or jerarquias_por_id_ruba([bombero.id_ruba])).get(bombero.id_ruba)
    return grado_y_cargo(jerarquia or bombero.clasificacion, bombero.cargo)


def mandos_del_padron(padron: List[Bombero]) -> List[Bombero]:
    jerarquias = jerarquias_por_id_ruba(b.id_ruba for b in padron)
    return [b for b in padron if es_mando(jerarquias.get(b.id_ruba), b.cargo)]


def _es_activo(persona: Personal) -> bool:
    return bool(persona.activo) and (persona.estado or "Activo") == "Activo"


def padron_activo(padron: Iterable[Bombero]) -> List[Bombero]:
    """Padrón que se OFRECE en los selectores de la planilla: excluye a quien
    en la base local está de baja / licencia / reserva (aunque el Excel de
    RUBA todavía lo liste como activo). Quien no tiene legajo local se
    mantiene: el Excel es la única fuente sobre esa persona."""
    padron = list(padron)
    ids = [b.id_ruba for b in padron if b.id_ruba is not None]
    if not ids:
        return padron
    with get_session() as session:
        inactivos = {p.id_ruba for p in session.query(Personal).filter(Personal.id_ruba.in_(ids))
                     if not _es_activo(p)}
    return [b for b in padron if b.id_ruba not in inactivos]


def bombero_historico(id_ruba: int) -> Optional[Bombero]:
    """Persona que ya no está en el padrón ofrecido (baja, inactiva o fuera
    del Excel) pero figura en un parte guardado: se arma desde la base local
    para poder mostrarla en ESE parte sin ofrecerla como opción nueva."""
    with get_session() as session:
        p = session.query(Personal).filter(Personal.id_ruba == id_ruba).first()
        if p is None:
            return None
        return Bombero(id_ruba=p.id_ruba, apellido=p.apellido, nombre=p.nombre, dni=p.dni or "",
                       legajo=p.legajo, cargo=None, clasificacion=p.jerarquia or "", formacion="")
