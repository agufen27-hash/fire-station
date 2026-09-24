"""
Acceso a los catálogos locales (personal, móviles, credenciales RUBA) usados
por la GUI y, más adelante, por el relleno de planillas y el bot de Playwright.

Todos los catálogos viven como JSON en `data/` para poder editarlos a mano o
desde la pestaña "Configuración y Catálogos" sin depender de la base de datos.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

CUERPO_ACTIVO_PATH = DATA_DIR / "cuerpo_activo.json"
PARQUE_AUTOMOTOR_PATH = DATA_DIR / "parque_automotor.json"
CONFIG_PATH = DATA_DIR / "config.json"

# Escala de grados sugerida para los combobox de la UI. Es una lista abierta:
# cualquier grado cargado a mano en un catálogo que no esté aquí igual se muestra.
GRADOS: List[str] = [
    "Aspirante",
    "Bombero",
    "Cabo",
    "Cabo 1ro",
    "Sargento",
    "Sargento Ayudante",
    "Suboficial Mayor",
    "Oficial",
    "Oficial Inspector",
    "Comandante",
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "ruba": {
        "usuario": "",
        "clave": "",
        "url_login": "https://www.gestionbomberos.org/login",
        "url_incidentes": "https://www.gestionbomberos.org/estructura/incidente/",
    },
    "cuartel": {
        "nombre": 'Bomberos Voluntarios "Osvaldo R. Rossi"',
        "localidad": "Adelia María",
        "codigo": "C 59 / R 3",
    },
}


def _leer_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _escribir_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Cuerpo activo (personal)
# ---------------------------------------------------------------------------

def cargar_cuerpo_activo(solo_activos: bool = True) -> List[Dict[str, Any]]:
    personal = _leer_json(CUERPO_ACTIVO_PATH, [])
    if solo_activos:
        personal = [p for p in personal if p.get("activo", True)]
    return sorted(personal, key=lambda p: (p.get("apellido", ""), p.get("nombre", "")))


def guardar_cuerpo_activo(personal: List[Dict[str, Any]]) -> None:
    _escribir_json(CUERPO_ACTIVO_PATH, personal)


def nombre_completo(persona: Dict[str, Any]) -> str:
    return f"{persona.get('apellido', '')}, {persona.get('nombre', '')}".strip(", ")


def etiqueta_combo(persona: Dict[str, Any]) -> str:
    """Texto mostrado en los combobox: 'Apellido, Nombre — Grado'."""
    return f"{nombre_completo(persona)} — {persona.get('grado', '')}".strip(" —")


# ---------------------------------------------------------------------------
# Parque automotor (móviles)
# ---------------------------------------------------------------------------

def cargar_parque_automotor(solo_activos: bool = True) -> List[Dict[str, Any]]:
    parque = _leer_json(PARQUE_AUTOMOTOR_PATH, [])
    if solo_activos:
        parque = [m for m in parque if m.get("activo", True)]
    return sorted(parque, key=lambda m: m.get("codigo", ""))


def guardar_parque_automotor(parque: List[Dict[str, Any]]) -> None:
    _escribir_json(PARQUE_AUTOMOTOR_PATH, parque)


def etiqueta_movil(movil: Dict[str, Any]) -> str:
    return f"{movil.get('codigo', '')} - {movil.get('nombre', '')}".strip(" -")


# ---------------------------------------------------------------------------
# Configuración general (credenciales RUBA, datos del cuartel)
# ---------------------------------------------------------------------------

def cargar_config() -> Dict[str, Any]:
    config = _leer_json(CONFIG_PATH, None)
    if config is None:
        config = json.loads(json.dumps(DEFAULT_CONFIG))  # copia profunda
        guardar_config(config)
    return config


def guardar_config(config: Dict[str, Any]) -> None:
    # NOTA: se guarda en texto plano en un JSON local. Es aceptable para un
    # equipo de escritorio de uso interno en LAN del cuartel, pero si la PC
    # es compartida conviene reforzarlo (ej. con `keyring`) más adelante.
    _escribir_json(CONFIG_PATH, config)
