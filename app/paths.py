"""
Resolución de rutas para Fire Station en modo desarrollo (corriendo desde el
código fuente) y en modo empaquetado (ejecutable único generado con
PyInstaller, ver tools/build_exe.py).

Dos categorías de ruta, con reglas distintas:

  - RECURSOS DE SOLO LECTURA que vienen con la app (plantillas PCS/PCD2, el
    esquema base de config/data, el navegador Chromium de Playwright): en
    modo congelado viven dentro del bundle de PyInstaller
    (`sys._MEIPASS`, una carpeta temporal de solo lectura que se borra al
    cerrar la app) -> `get_resource_path()`.

  - DATOS ESCRIBIBLES que la app genera o modifica en uso (la base SQLite,
    las planillas generadas en output/, config.json con las credenciales
    reales de RUBA que el usuario puede necesitar editar): NUNCA deben ir
    dentro de `_MEIPASS`, porque esa carpeta es temporal y de solo lectura.
    Van en una carpeta al lado del .exe, para que sobrevivan entre
    ejecuciones y sean fáciles de encontrar/respaldar -> `get_writable_dir()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Raíz del proyecto en modo desarrollo: .../Fire Station/
_DEV_BASE_DIR = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """True cuando corre como ejecutable empaquetado por PyInstaller."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def get_resource_path(relative_path: str) -> Path:
    """Ruta a un recurso de solo lectura embebido en la app (plantillas,
    esquema base de config/data, navegador de Playwright).

    En desarrollo resuelve contra la raíz del proyecto; empaquetado,
    contra la carpeta temporal donde PyInstaller descomprime `--add-data`.
    """
    base = Path(sys._MEIPASS) if is_frozen() else _DEV_BASE_DIR  # type: ignore[attr-defined]
    return base / relative_path


# Identidad visual embebida en el .exe (ver tools/build_exe.py). OJO: la
# imagen del mapa operativo NO va acá -- es un dato que el cuartel cambia
# desde Configuración y vive en la carpeta escribible (app/services/cartografia.py).
ICONO_APP = "resources/app_icon.ico"
LOGO_INSTITUCIONAL = "resources/logo.png"


def ruta_recurso_existente(relative_path: str) -> "Path | None":
    """`get_resource_path` si el archivo existe; None si no (el que llama
    decide el fallback, p. ej. el emoji del encabezado)."""
    ruta = get_resource_path(relative_path)
    return ruta if ruta.is_file() else None


def get_writable_dir(relative_subdir: str) -> Path:
    """Carpeta escribible para datos que la app genera o modifica
    (`data/`, `output/`). En desarrollo es la carpeta del proyecto;
    empaquetado, la carpeta donde vive el .exe (no `_MEIPASS`)."""
    base = Path(sys.executable).resolve().parent if is_frozen() else _DEV_BASE_DIR
    destino = base / relative_subdir
    destino.mkdir(parents=True, exist_ok=True)
    return destino


# Secciones que toda config.json debe tener (se agregan si faltan). La URL del
# proyecto Supabase no es secreta; la service key SÍ y nunca viaja en el bundle
# ni en el repo: se carga una vez por PC (diálogo del Libro de Guardia o a mano).
URL_SUPABASE_PREDETERMINADA = "https://uzwegoetekpdommmrnrv.supabase.co"
SECCIONES_PREDETERMINADAS = {
    "supabase": {"url": URL_SUPABASE_PREDETERMINADA, "service_key": ""},
}


def leer_config_json(ruta: "Path | None" = None) -> dict:
    """data/config.json como dict ({} si no existe o es ilegible). Tolera BOM
    (Bloc de notas lo agrega al guardar en UTF-8)."""
    import json

    ruta = ruta or get_writable_dir("data") / "config.json"
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def completar_config(existente: dict, plantilla: dict) -> tuple[dict, bool]:
    """Agrega a `existente` las secciones / claves de `plantilla` que falten.
    Nunca pisa un valor ya cargado (ni siquiera uno vacío). Devuelve
    (config completada, hubo_cambios)."""
    resultado = dict(existente)
    cambio = False
    for seccion, valores in plantilla.items():
        actual = resultado.get(seccion)
        if seccion not in resultado:
            resultado[seccion] = dict(valores) if isinstance(valores, dict) else valores
            cambio = True
        elif isinstance(actual, dict) and isinstance(valores, dict):
            faltantes = {k: v for k, v in valores.items() if k not in actual}
            if faltantes:
                resultado[seccion] = {**actual, **faltantes}
                cambio = True
    return resultado, cambio


def asegurar_config_inicial() -> None:
    """Primer arranque en una máquina nueva: crea `data/config.json` desde el
    esqueleto embebido `data/config.example.json` (sin credenciales reales,
    ver tools/build_exe.py). Si ya existe, le AGREGA las secciones que
    versiones nuevas necesitan y le faltan (p. ej. "supabase" en una
    instalación anterior a la nube), sin tocar nada de lo cargado; antes de
    reescribirlo deja una copia en config.json.bak."""
    import json
    import shutil

    destino = get_writable_dir("data") / "config.json"
    ejemplo = leer_config_json(get_resource_path("data/config.example.json"))
    # Esqueleto embebido + valores por defecto (los de SECCIONES_PREDETERMINADAS ganan:
    # el esqueleto trae la URL vacía).
    plantilla = dict(ejemplo)
    for seccion, valores in SECCIONES_PREDETERMINADAS.items():
        base = ejemplo.get(seccion)
        plantilla[seccion] = {**base, **valores} if isinstance(base, dict) else dict(valores)

    if not destino.exists():
        try:
            destino.write_text(json.dumps(plantilla, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return

    existente = leer_config_json(destino)
    if not existente and destino.stat().st_size > 0:
        return  # JSON roto: mejor no reescribirlo y perder lo que el usuario tenga
    completa, cambio = completar_config(existente, plantilla)
    if not cambio:
        return
    try:
        shutil.copy(destino, destino.with_name("config.json.bak"))
        destino.write_text(json.dumps(completa, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # sin permisos de escritura: las credenciales se piden igual desde la UI


def configurar_entorno_playwright() -> None:
    """Si el navegador Chromium de Playwright viene embebido en el bundle
    (carpeta `ms-playwright/`, ver tools/build_exe.py), apunta
    PLAYWRIGHT_BROWSERS_PATH ahí para que no intente descargarlo en una
    máquina sin Node/Python. En modo desarrollo no hace nada: se usa el
    caché normal de Playwright (`playwright install chromium`)."""
    if not is_frozen():
        return

    import os

    carpeta_navegadores = get_resource_path("ms-playwright")
    if carpeta_navegadores.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(carpeta_navegadores)
