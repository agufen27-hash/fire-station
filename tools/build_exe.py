"""
Empaqueta Fire Station como ejecutable de Windows con PyInstaller, para que
un guardia pueda hacer doble clic en `dist/FireStation/FireStation.exe` en
una máquina SIN Python ni Node.js instalados y la app funcione de punta a
punta (incluida la sincronización silenciosa con RUBA, que depende del
Chromium de Playwright).

Qué se embebe en el bundle (read-only, ver app/paths.py -> get_resource_path):
  - templates/PCS.xlsx, templates/PCD2.xlsx  (las plantillas oficiales)
  - data/config.example.json                 (esqueleto de config, SIN
                                                credenciales reales)
  - resources/app_icon.ico y resources/logo.png (ícono del .exe y de las
    ventanas, logo del encabezado). Solo esos dos: el resto de `resources/`
    (la imagen del mapa operativo) es un dato escribible del cuartel.
  - (el mapa táctico es nativo, QGraphicsView: no necesita archivos
    embebidos. Su imagen base se carga desde Configuración y vive en
    `resources/` AL LADO del .exe, ver app/services/cartografia.py)
  - el navegador Chromium de Playwright ya instalado en esta máquina
    (`ms-playwright/chromium-*` y `ms-playwright/chromium_headless_shell-*`,
    la versión más nueva de cada uno si hay varias) + el driver Node interno
    del paquete `playwright` (via --collect-all playwright)

Qué NO se embebe (a propósito):
  - `data/config.json` real (tiene el usuario/clave reales de RUBA -- si
    viajara adentro del .exe, cualquiera que lo abra con un descompresor de
    PyInstaller lo vería en texto plano). En el primer arranque en la
    máquina destino, si no existe `data/config.json` al lado del .exe, se
    copia el esqueleto de `config.example.json` para que el usuario cargue
    ahí las credenciales reales a mano.
  - `config/ruba_campos_detectados.json` / `ruba_formularios_por_tipo.json`:
    son salidas de las herramientas de exploración (tools/inspect_ruba.py,
    tools/deep_scan_ruba.py), no los usa la app en producción, y
    `ruba_campos_detectados.json` pesa ~1.8 MB de DOM escaneado del portal
    real sin ningún propósito en el .exe final.
    (`config/ruba_mapping.json` SÍ se embebe: `app/services/ruba_helpers.py`
    lee de ahí las URLs y los selectores oficiales de RUBA.)

Requisito previo (una sola vez, en ESTA máquina, antes de empaquetar):
    pip install pyinstaller
    playwright install chromium

Uso:
    python tools/build_exe.py

Resultado:
    dist/FireStation/FireStation.exe
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
DATA_DIR = BASE_DIR / "data"
RESOURCES_DIR = BASE_DIR / "resources"
ICONO_APP = RESOURCES_DIR / "app_icon.ico"
LOGO_INSTITUCIONAL = RESOURCES_DIR / "logo.png"
DIST_DIR = BASE_DIR / "dist"
BUILD_DIR = BASE_DIR / "build"
NOMBRE_APP = "FireStation"

# Prefijos de carpeta de Playwright que puede necesitar `chromium.launch()`
# según la versión instalada (algunas versiones de Playwright enrutan
# headless=True al "headless shell" en vez del chromium normal).
PREFIJOS_CHROMIUM = ("chromium-", "chromium_headless_shell-")


def _detectar_carpeta_ms_playwright() -> Optional[Path]:
    candidatos = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        candidatos.append(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]))
    if os.environ.get("LOCALAPPDATA"):
        candidatos.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
    for candidato in candidatos:
        if candidato.is_dir():
            return candidato
    return None


def _elegir_versiones_mas_nuevas(carpeta_ms_playwright: Path) -> List[Path]:
    """De cada prefijo (chromium-, chromium_headless_shell-) elige la
    subcarpeta con el número de versión más alto, para no embeber
    instalaciones viejas duplicadas."""
    mejores: Dict[str, Path] = {}
    for entrada in carpeta_ms_playwright.iterdir():
        if not entrada.is_dir():
            continue
        for prefijo in PREFIJOS_CHROMIUM:
            m = re.fullmatch(rf"{re.escape(prefijo)}(\d+)", entrada.name)
            if not m:
                continue
            version = int(m.group(1))
            actual = mejores.get(prefijo)
            if actual is None or version > int(re.search(r"(\d+)$", actual.name).group(1)):
                mejores[prefijo] = entrada
    return list(mejores.values())


def _preparar_config_example() -> Path:
    """Genera data/config.example.json (esqueleto SIN credenciales) a partir
    de la forma real de data/config.json, si existe, o con un esqueleto fijo
    si todavía no hay ningún config.json en este checkout."""
    destino = DATA_DIR / "config.example.json"
    origen = DATA_DIR / "config.json"

    esqueleto = {
        "ruba": {
            "usuario": "",
            "clave": "",
            "url_login": "https://www.gestionbomberos.org/login",
            "url_incidentes": "https://www.gestionbomberos.org/estructura/incidente/",
        },
        "cuartel": {
            "nombre": "",
            "localidad": "Adelia María",
            "codigo": "C 59 / R 3",
        },
    }
    if origen.exists():
        try:
            real = json.loads(origen.read_text(encoding="utf-8"))
            if "cuartel" in real:
                esqueleto["cuartel"] = {**esqueleto["cuartel"], **{
                    k: v for k, v in real["cuartel"].items() if k != "clave"
                }}
        except (json.JSONDecodeError, OSError):
            pass  # si no se puede leer, seguimos con el esqueleto fijo

    destino.write_text(json.dumps(esqueleto, indent=2, ensure_ascii=False), encoding="utf-8")
    return destino


def main() -> None:
    if shutil.which("pyinstaller") is None:
        sys.exit("Falta PyInstaller. Instalalo con:  pip install pyinstaller")

    for plantilla in ("PCS.xlsx", "PCD2.xlsx"):
        if not (TEMPLATES_DIR / plantilla).exists():
            sys.exit(f"Falta '{TEMPLATES_DIR / plantilla}'. Sin las plantillas no se puede empaquetar.")

    for recurso in (ICONO_APP, LOGO_INSTITUCIONAL):
        if not recurso.is_file():
            sys.exit(f"Falta '{recurso}' (identidad visual de la app).")

    carpeta_ms_playwright = _detectar_carpeta_ms_playwright()
    if carpeta_ms_playwright is None:
        sys.exit(
            "No se encontró el navegador Chromium de Playwright instalado en esta máquina.\n"
            "Corré primero:  playwright install chromium\n"
            "y volvé a intentar -- sin esto, la sincronización con RUBA no va a funcionar "
            "en una máquina destino sin Python/Node."
        )
    carpetas_chromium = _elegir_versiones_mas_nuevas(carpeta_ms_playwright)
    if not carpetas_chromium:
        sys.exit(
            f"'{carpeta_ms_playwright}' existe pero no tiene ninguna carpeta chromium-* / "
            "chromium_headless_shell-* adentro. Corré:  playwright install chromium"
        )

    config_example = _preparar_config_example()

    print("Se van a embeber estas versiones de Chromium:")
    for carpeta in carpetas_chromium:
        print(f"  - {carpeta}")

    sep = ";" if os.name == "nt" else ":"
    comando = [
        "pyinstaller",
        "--name", NOMBRE_APP,
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--collect-all", "playwright",
        "--hidden-import", "sqlalchemy.dialects.sqlite",
        "--icon", str(ICONO_APP),
        "--add-data", f"{ICONO_APP}{sep}resources",
        "--add-data", f"{LOGO_INSTITUCIONAL}{sep}resources",
        "--add-data", f"{TEMPLATES_DIR}{sep}templates",
        "--add-data", f"{config_example}{sep}data",
        "--add-data", f"{BASE_DIR / 'config' / 'ruba_mapping.json'}{sep}config",
    ]
    for carpeta in carpetas_chromium:
        comando += ["--add-data", f"{carpeta}{sep}ms-playwright/{carpeta.name}"]
    comando.append(str(BASE_DIR / "run.py"))

    print("\nComando de PyInstaller:")
    print(" ", " ".join(comando))
    print()

    resultado = subprocess.run(comando, cwd=BASE_DIR)
    if resultado.returncode != 0:
        sys.exit(resultado.returncode)

    print()
    print(f"Listo: {DIST_DIR / NOMBRE_APP / (NOMBRE_APP + '.exe')}")
    print(
        "Copiá TODA la carpeta 'dist/FireStation/' a la máquina destino (no solo el .exe: "
        "PyInstaller --onedir deja las librerías al lado). En el primer arranque ahí, la "
        "app va a crear su propia carpeta 'data/' al lado del .exe (base SQLite + "
        "config.json) -- hay que completar 'data/config.json' con el usuario/clave reales "
        "de RUBA antes de que la sincronización funcione."
    )


if __name__ == "__main__":
    main()
