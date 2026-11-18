"""
Datos semilla del cuartel que NO van al repositorio: el personal de fábrica
que `init_db()` carga en una base nueva y el denunciante por defecto que la
carga en RUBA usa cuando el parte no tiene uno.

Viven en `data/seed_bomberos.json` (carpeta excluida por .gitignore), con
esta forma:

    {
      "personal": [
        {"nombre": "Nombre", "apellido": "Apellido", "dni": "12345678", "telefono": "3585000000"}
      ],
      "denunciante_por_defecto": {
        "nombre": "Nombre", "apellido": "Apellido", "dni": "12345678", "telefono": "3585000000"
      }
    }

Sin el archivo (instalación limpia): la base arranca SIN personal -- nada
de fichas de ejemplo -- y el padrón se importa del 'Reporte de bomberos' de
RUBA. Tampoco hay denunciante por defecto: a RUBA (registro oficial) nunca
se le mandan datos inventados.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.paths import get_writable_dir

log = logging.getLogger(__name__)

NOMBRE_ARCHIVO = "seed_bomberos.json"
CAMPOS_PERSONA = ("nombre", "apellido", "dni", "telefono")



def ruta_semilla() -> Path:
    return get_writable_dir("data") / NOMBRE_ARCHIVO


def _leer() -> Dict[str, Any]:
    ruta = ruta_semilla()
    if not ruta.is_file():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("No se pudo leer %s (%s): se usan los valores genéricos.", ruta, e)
        return {}
    return datos if isinstance(datos, dict) else {}


def _persona(valor: Any) -> Optional[Dict[str, str]]:
    if not isinstance(valor, dict) or not str(valor.get("apellido") or "").strip():
        return None
    return {campo: str(valor.get(campo) or "").strip() for campo in CAMPOS_PERSONA}


def personal_inicial() -> List[Dict[str, str]]:
    """Personal para una base nueva: solo el de la semilla local del cuartel
    (data/seed_bomberos.json). Sin semilla, lista vacía: instalación limpia."""
    return [p for p in map(_persona, _leer().get("personal") or []) if p]


def denunciante_por_defecto() -> Optional[Dict[str, str]]:
    """Denunciante para RUBA cuando el parte no trae uno, o None si la semilla
    no lo define (no se inventa una identidad para un registro oficial)."""
    return _persona(_leer().get("denunciante_por_defecto"))
