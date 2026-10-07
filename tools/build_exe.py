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


# Nombre en browsers.json de Playwright -> prefijo de la carpeta en ms-playwright/.
NAVEGADORES_REQUERIDOS = {"chromium": "chromium-", "chromium-headless-shell": "chromium_headless_shell-"}


def revisiones_requeridas() -> Dict[str, str]:
    """Revisiones EXACTAS de Chromium que espera el paquete `playwright`
    instalado (driver/package/browsers.json), p. ej. {"chromium-": "1223",
    "chromium_headless_shell-": "1223"}.

    Antes se embebía la carpeta de número más alto que hubiera en
    %LOCALAPPDATA%\\ms-playwright: si quedaba una de otra versión de
    Playwright (1234 con Playwright 1.60, que pide 1223) el .exe instalado
    fallaba con "BrowserType.launch: Executable doesn't exist at
    ...\\_internal\\ms-playwright\\chromium_headless_shell-1223\\...".
    """
    import playwright

    ruta = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        navegadores = json.loads(ruta.read_text(encoding="utf-8"))["browsers"]
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"No se pudo leer {ruta} ({e}): ¿está bien instalado el paquete playwright?")
    revisiones = {}
    for navegador in navegadores:
        prefijo = NAVEGADORES_REQUERIDOS.get(navegador.get("name"))
        if prefijo:
            overrides = navegador.get("revisionOverrides") or {}
            revisiones[prefijo] = str(overrides.get("win64") or navegador["revision"])
    faltan = set(NAVEGADORES_REQUERIDOS.values()) - set(revisiones)
    if faltan:
        sys.exit(f"{ruta} no declara {sorted(faltan)}: versión de Playwright no soportada por este script.")
    return revisiones


def _carpetas_requeridas(carpeta_ms_playwright: Path) -> List[Path]:
    """Las carpetas de las revisiones que pide Playwright (no "la más nueva").
    Si falta alguna se corta con la instrucción para instalarla."""
    carpetas = [carpeta_ms_playwright / f"{prefijo}{revision}" for prefijo, revision in revisiones_requeridas().items()]
    faltantes = [c for c in carpetas if not (c / "INSTALLATION_COMPLETE").is_file()]
    if faltantes:
        sys.exit(
            "Falta el Chromium que necesita la versión de Playwright instalada:\n"
            + "\n".join(f"  - {c}" for c in faltantes)
            + "\nCorré:  playwright install chromium   (instala exactamente esas revisiones)"
        )
    otras = sorted(e.name for e in carpeta_ms_playwright.iterdir()
                   if e.is_dir() and e.name.startswith(PREFIJOS_CHROMIUM) and e not in carpetas)
    if otras:
        print(f"Aviso: se ignoran versiones de Chromium que esta Playwright no usa: {', '.join(otras)}")
    return carpetas


def verificar_chromium_embebido(dist_app: Path) -> None:
    """Después de PyInstaller: que _internal/ms-playwright tenga los
    ejecutables de las revisiones que va a buscar el Playwright embebido."""
    base = dist_app / "_internal" / "ms-playwright"
    ejecutables = {
        "chromium-": ("chrome-win64", "chrome.exe"),
        "chromium_headless_shell-": ("chrome-headless-shell-win64", "chrome-headless-shell.exe"),
    }
    faltantes = []
    for prefijo, revision in revisiones_requeridas().items():
        carpeta = base / f"{prefijo}{revision}"
        subcarpeta, exe = ejecutables[prefijo]
        if not (carpeta / subcarpeta / exe).is_file() and not any(carpeta.glob(f"*/{exe}")):
            faltantes.append(str(carpeta / subcarpeta / exe))
    if faltantes:
        sys.exit("El bundle quedó SIN el Chromium que pide Playwright:\n  " + "\n  ".join(faltantes))
    print(f"Chromium embebido OK en {base} ({', '.join(f'{p}{r}' for p, r in revisiones_requeridas().items())})")


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
    carpetas_chromium = _carpetas_requeridas(carpeta_ms_playwright)

    config_example = _preparar_config_example()

    print("Se van a embeber estas versiones de Chromium (las que pide Playwright):")
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
    # NOTA: NO pasamos carpetas_chromium a PyInstaller por --add-data
    # para evitar bloqueos de I/O masivos en COLLECT en Windows.
    # Las copiamos directamente abajo con shutil.copytree.
    comando.append(str(BASE_DIR / "run.py"))

    print("\nComando de PyInstaller:")
    print(" ", " ".join(comando))
    print()

    resultado = subprocess.run(comando, cwd=BASE_DIR)
    if resultado.returncode != 0:
        sys.exit(resultado.returncode)

    # Copia directa y limpia de Chromium a dist/FireStation/_internal/ms-playwright
    destino_pw = DIST_DIR / NOMBRE_APP / "_internal" / "ms-playwright"
    print(f"\nCopiando Chromium directamente a {destino_pw}...")
    for carpeta in carpetas_chromium:
        dest_carpeta = destino_pw / carpeta.name
        if dest_carpeta.exists():
            shutil.rmtree(dest_carpeta)
        shutil.copytree(carpeta, dest_carpeta)
        print(f"  OK: {carpeta.name}")

    verificar_chromium_embebido(DIST_DIR / NOMBRE_APP)

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