"""
Auto-actualización de Fire Station.

Dónde se busca:
  - Ejecutable (.exe, PyInstaller): el último GitHub Release del repo. Cada
    release publicado con `tools/release.py` trae dos assets:
      * `FireStation-update-X.Y.Z.zip`: FireStation.exe + _internal/ SIN el
        Chromium de Playwright (_internal/ms-playwright, ~700 MB que casi
        nunca cambia).
      * `update.json`: {"version", "paquete", "sha256", "tamano", "chromium", "notas"}.
  - Código fuente (desarrollo): `git fetch` + commits nuevos en origin/main.
    Solo se AVISA (hay que hacer `git pull`): nunca se toca el árbol de trabajo.

Por qué no se reemplaza solo FireStation.exe: el build es `--onedir`, y el
mapeo de RUBA, las plantillas PCS/PCD2, los íconos y las dependencias viven
en `_internal/`. Cambiar solo el .exe dejaría todo eso desfasado.

Seguridad de datos: la actualización escribe ÚNICAMENTE `FireStation.exe` y
`_internal/` (menos `ms-playwright`). Las carpetas de datos que la app crea
al lado del .exe -- data/ (base SQLite, config.json con la clave de RUBA,
semilla, firmas), output/ (planillas y PDF), logs/ y resources/ (imagen del
mapa operativo) -- quedan fuera del paquete, se rechaza cualquier paquete
que las nombre, y el script de instalación nunca las recorre.

Instalación (Windows): el .exe en uso no se puede sobrescribir, así que se
genera un `updater.bat` en %TEMP% que espera a que termine ESTE proceso
(por PID), respalda la versión actual, copia la nueva, y si algo falla
restaura el respaldo. En todos los casos vuelve a abrir la app.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from app import __version__
from app.paths import get_resource_path, get_writable_dir, is_frozen
from app.services.red import descargar_archivo, obtener_json_verificado

log = logging.getLogger(__name__)

REPO_GITHUB = "agufen27-hash/fire-station"
URL_ULTIMO_RELEASE = f"https://api.github.com/repos/{REPO_GITHUB}/releases/latest"
NOMBRE_MANIFIESTO = "update.json"
NOMBRE_EXE = "FireStation.exe"
CARPETA_INTERNA = "_internal"
CARPETA_CHROMIUM = "ms-playwright"  # dentro de _internal: nunca viaja en el paquete de actualización
# Carpetas de datos al lado del .exe (ver app/paths.get_writable_dir): intocables.
CARPETAS_PROTEGIDAS = ("data", "output", "logs", "resources")

TIMEOUT_CONSULTA_SEG = 10
TIMEOUT_DESCARGA_SEG = 60

MODO_PREGUNTAR = "preguntar"
MODO_AUTOMATICO = "automatico"      # descarga sola y se instala al cerrar la app
MODO_DESACTIVADO = "desactivado"
MODOS = (MODO_PREGUNTAR, MODO_AUTOMATICO, MODO_DESACTIVADO)


class ActualizacionError(Exception):
    """Paquete inválido, descarga corrupta o actualización no aplicable."""


@dataclass
class InfoActualizacion:
    version: str
    origen: str                      # "release" | "git"
    notas: str = ""
    url_paquete: str = ""
    nombre_paquete: str = ""
    sha256: str = ""
    tamano: int = 0
    chromium: List[str] = field(default_factory=list)
    commits_nuevos: int = 0


# ---------------------------------------------------------------------------
# Versiones y configuración
# ---------------------------------------------------------------------------

_RE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def version_tupla(texto: str) -> Tuple[int, int, int]:
    m = _RE_VERSION.match((texto or "").strip())
    if not m:
        raise ValueError(f"Versión inválida: '{texto}' (se espera X.Y.Z)")
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def es_mas_nueva(remota: str, local: str = __version__) -> bool:
    try:
        return version_tupla(remota) > version_tupla(local)
    except ValueError:
        return False


def modo_configurado() -> str:
    """data/config.json -> {"actualizaciones": {"modo": "preguntar"}} (por defecto)."""
    try:
        datos = json.loads((get_writable_dir("data") / "config.json").read_text(encoding="utf-8"))
        modo = (datos.get("actualizaciones") or {}).get("modo", MODO_PREGUNTAR)
    except (OSError, ValueError, AttributeError):
        modo = MODO_PREGUNTAR
    return modo if modo in MODOS else MODO_PREGUNTAR


def carpeta_instalacion() -> Path:
    """Carpeta del .exe en uso (solo tiene sentido empaquetado)."""
    return Path(sys.executable).resolve().parent


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------

def buscar_en_releases() -> Optional[InfoActualizacion]:
    """Último GitHub Release; None si no hay uno más nuevo que esta versión.
    Un repo privado (o inexistente) responde 404 sin token: se informa
    como excepción y el que llama lo loguea sin molestar al usuario."""
    cabeceras = {"Accept": "application/vnd.github+json"}
    release = obtener_json_verificado(URL_ULTIMO_RELEASE, TIMEOUT_CONSULTA_SEG, cabeceras)
    version = str(release.get("tag_name") or "").lstrip("v")
    if not es_mas_nueva(version):
        return None
    assets = {a.get("name"): a for a in release.get("assets") or []}
    if NOMBRE_MANIFIESTO not in assets:
        raise ActualizacionError(f"El release v{version} no trae '{NOMBRE_MANIFIESTO}' (¿publicado a mano?).")
    manifiesto = obtener_json_verificado(assets[NOMBRE_MANIFIESTO]["browser_download_url"], TIMEOUT_CONSULTA_SEG)
    if str(manifiesto.get("version")) != version:
        raise ActualizacionError(f"update.json dice v{manifiesto.get('version')} pero el release es v{version}.")
    paquete = assets.get(manifiesto.get("paquete"))
    if paquete is None or not re.fullmatch(r"[0-9a-f]{64}", str(manifiesto.get("sha256", ""))):
        raise ActualizacionError(f"El release v{version} tiene un update.json incompleto.")
    return InfoActualizacion(
        version=version, origen="release", notas=str(manifiesto.get("notas") or release.get("body") or ""),
        url_paquete=paquete["browser_download_url"], nombre_paquete=paquete["name"],
        sha256=manifiesto["sha256"], tamano=int(paquete.get("size") or 0),
        chromium=[str(c) for c in manifiesto.get("chromium") or []],
    )


def _git(*args: str, cwd: Path, timeout: float = 30) -> str:
    entorno = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    banderas = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    resultado = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                               env=entorno, creationflags=banderas, check=True)
    return resultado.stdout.strip()


def buscar_en_git(raiz: Optional[Path] = None) -> Optional[InfoActualizacion]:
    """Desarrollo: commits en origin/main que este checkout no tiene."""
    raiz = raiz or get_resource_path(".")
    _git("fetch", "--quiet", "origin", "main", cwd=raiz)
    nuevos = int(_git("rev-list", "--count", "HEAD..origin/main", cwd=raiz) or 0)
    if not nuevos:
        return None
    ultimo = _git("log", "-1", "--format=%s", "origin/main", cwd=raiz)
    return InfoActualizacion(version=f"origin/main (+{nuevos})", origen="git", commits_nuevos=nuevos,
                             notas=f"Último commit: {ultimo}")


def buscar_actualizacion() -> Optional[InfoActualizacion]:
    return buscar_en_releases() if is_frozen() else buscar_en_git()


# ---------------------------------------------------------------------------
# Descarga y validación del paquete
# ---------------------------------------------------------------------------

def carpeta_temporal(version: str) -> Path:
    return Path(tempfile.gettempdir()) / f"FireStation_update_{version}"


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def validar_entradas_paquete(nombres: List[str]) -> None:
    """El paquete solo puede traer FireStation.exe y _internal/ (sin Chromium).
    Cualquier otra ruta -- en particular data/, output/, logs/, resources/ al
    lado del .exe, rutas absolutas o con '..' -- invalida el paquete entero."""
    if NOMBRE_EXE not in nombres:
        raise ActualizacionError(f"El paquete no trae {NOMBRE_EXE}.")
    for nombre in nombres:
        partes = Path(nombre.replace("\\", "/")).parts
        if nombre.startswith(("/", "\\")) or ".." in partes or (partes and ":" in partes[0]):
            raise ActualizacionError(f"Ruta insegura en el paquete: {nombre}")
        if nombre == NOMBRE_EXE:
            continue
        if not partes or partes[0] != CARPETA_INTERNA:
            raise ActualizacionError(f"El paquete intenta escribir fuera de la app: {nombre}")
        if len(partes) > 1 and partes[1] == CARPETA_CHROMIUM:
            raise ActualizacionError(f"El paquete no debe traer Chromium: {nombre}")


def descargar_y_preparar(info: InfoActualizacion, instalacion: Optional[Path] = None,
                         on_progreso: Optional[Callable[[int, int], None]] = None) -> Path:
    """Descarga el paquete a %TEMP%, verifica SHA-256 y contenido, y lo
    descomprime en `<temp>/nuevo`. Devuelve la carpeta temporal (staging)."""
    instalacion = instalacion or carpeta_instalacion()
    chromium_actual = instalacion / CARPETA_INTERNA / CARPETA_CHROMIUM
    faltantes = [c for c in info.chromium if not (chromium_actual / c).is_dir()]
    if faltantes:
        raise ActualizacionError(
            f"La versión {info.version} necesita otro Chromium ({', '.join(faltantes)}): hay que "
            "reinstalar la carpeta completa de Fire Station (copiando data/ a la nueva)."
        )

    staging = carpeta_temporal(info.version)
    staging.mkdir(parents=True, exist_ok=True)
    zip_local = staging / info.nombre_paquete
    if not (zip_local.is_file() and _sha256(zip_local) == info.sha256):
        descargar_archivo(info.url_paquete, zip_local, TIMEOUT_DESCARGA_SEG, on_progreso)
    if _sha256(zip_local) != info.sha256:
        zip_local.unlink(missing_ok=True)
        raise ActualizacionError("El paquete descargado está dañado (SHA-256 no coincide). Se reintentará.")

    nuevo = staging / "nuevo"
    with zipfile.ZipFile(zip_local) as z:
        nombres = [n for n in z.namelist() if not n.endswith("/")]
        validar_entradas_paquete(nombres)
        if nuevo.exists():
            import shutil

            shutil.rmtree(nuevo)
        z.extractall(nuevo)
    return staging


# ---------------------------------------------------------------------------
# Instalación (script .bat)
# ---------------------------------------------------------------------------

def generar_script(staging: Path, instalacion: Path, pid: int) -> Path:
    """updater.bat: espera a que termine el PID, respalda, instala, y ante
    un error restaura. robocopy /MIR + /XD ms-playwright: refleja _internal
    sin tocar Chromium (los directorios excluidos tampoco se purgan)."""
    robo = "/R:3 /W:2 /NFL /NDL /NJH /NJS /NP"
    lineas = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        f'set "APP={instalacion}"',
        f'set "NUEVO={staging / "nuevo"}"',
        f'set "RESPALDO={staging / "respaldo"}"',
        f'set "LOG={staging / "actualizacion.log"}"',
        # Rutas completas a las herramientas de Windows: con Git (u otras
        # herramientas Unix) en el PATH, `find` sería el de Git, nunca
        # encontraría el PID y no se esperaría el cierre de la app.
        r'set "SYS=%SystemRoot%\System32"',
        'echo [%date% %time%] Esperando que se cierre Fire Station > "%LOG%"',
        ":esperar",
        f'"%SYS%\\tasklist.exe" /FI "PID eq {pid}" /NH 2>nul | "%SYS%\\find.exe" "{pid}" >nul',
        # `timeout` no funciona sin consola (el .bat corre oculto): ping como espera.
        r'if not errorlevel 1 ( "%SYS%\PING.EXE" -n 2 127.0.0.1 >nul & goto esperar )',
        'echo [%date% %time%] Respaldo de la version actual >> "%LOG%"',
        f'"%SYS%\\Robocopy.exe" "%APP%\\{CARPETA_INTERNA}" "%RESPALDO%\\{CARPETA_INTERNA}" /MIR /XD {CARPETA_CHROMIUM} {robo} >> "%LOG%"',
        "if errorlevel 8 goto sin_respaldo",
        f'copy /y "%APP%\\{NOMBRE_EXE}" "%RESPALDO%\\{NOMBRE_EXE}" >> "%LOG%" || goto sin_respaldo',
        'echo [%date% %time%] Instalando la version nueva >> "%LOG%"',
        f'"%SYS%\\Robocopy.exe" "%NUEVO%\\{CARPETA_INTERNA}" "%APP%\\{CARPETA_INTERNA}" /MIR /XD {CARPETA_CHROMIUM} {robo} >> "%LOG%"',
        "if errorlevel 8 goto restaurar",
        f'copy /y "%NUEVO%\\{NOMBRE_EXE}" "%APP%\\{NOMBRE_EXE}" >> "%LOG%" || goto restaurar',
        'echo [%date% %time%] OK >> "%LOG%"',
        "goto abrir",
        ":restaurar",
        'echo [%date% %time%] ERROR al instalar: se restaura la version anterior >> "%LOG%"',
        f'"%SYS%\\Robocopy.exe" "%RESPALDO%\\{CARPETA_INTERNA}" "%APP%\\{CARPETA_INTERNA}" /MIR /XD {CARPETA_CHROMIUM} {robo} >> "%LOG%"',
        f'copy /y "%RESPALDO%\\{NOMBRE_EXE}" "%APP%\\{NOMBRE_EXE}" >> "%LOG%"',
        "goto abrir",
        ":sin_respaldo",
        'echo [%date% %time%] ERROR: no se pudo respaldar; no se instalo nada >> "%LOG%"',
        ":abrir",
        f'start "" "%APP%\\{NOMBRE_EXE}"',
        'del "%~f0"',
    ]
    script = staging / "updater.bat"
    script.write_text("\r\n".join(lineas) + "\r\n", encoding="utf-8")
    return script


def lanzar_instalacion(staging: Path, instalacion: Optional[Path] = None) -> None:
    """Deja corriendo updater.bat (oculto, independiente de este proceso).
    El que llama tiene que cerrar la app enseguida: el script espera ese cierre."""
    if not is_frozen():
        raise ActualizacionError("La instalación automática solo aplica al ejecutable; en desarrollo usá git pull.")
    instalacion = instalacion or carpeta_instalacion()
    for protegida in CARPETAS_PROTEGIDAS:  # defensa extra: nunca instalar sobre una carpeta de datos
        if instalacion.name.lower() == protegida:
            raise ActualizacionError(f"Carpeta de instalación sospechosa: {instalacion}")
    script = generar_script(staging, instalacion, os.getpid())
    banderas = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=banderas, close_fds=True,
                     cwd=str(staging))
    log.info("Actualización lanzada: %s", script)


# ---------------------------------------------------------------------------
# Hilos (QThread) para la UI
# ---------------------------------------------------------------------------

class BuscarActualizacionWorker(QObject):
    resultado = Signal(object)   # InfoActualizacion | None
    fallo = Signal(str)
    terminado = Signal()

    def run(self) -> None:
        try:
            self.resultado.emit(buscar_actualizacion())
        except Exception as e:  # noqa: BLE001 - sin internet / repo privado / sin git: no molesta al operador
            self.fallo.emit(f"{type(e).__name__}: {e}")
        finally:
            self.terminado.emit()


class DescargarActualizacionWorker(QObject):
    progreso = Signal(int, int)  # bytes, total
    listo = Signal(str)          # carpeta staging
    fallo = Signal(str)
    terminado = Signal()

    def __init__(self, info: InfoActualizacion) -> None:
        super().__init__()
        self.info = info

    def run(self) -> None:
        try:
            staging = descargar_y_preparar(self.info, on_progreso=lambda a, b: self.progreso.emit(a, b))
            self.listo.emit(str(staging))
        except Exception as e:  # noqa: BLE001 - se informa en la UI
            self.fallo.emit(str(e) if isinstance(e, ActualizacionError) else f"{type(e).__name__}: {e}")
        finally:
            self.terminado.emit()


def lanzar_en_hilo(worker: QObject) -> QThread:
    hilo = QThread()
    worker.moveToThread(hilo)
    hilo.started.connect(worker.run)
    worker.terminado.connect(hilo.quit)
    worker.terminado.connect(worker.deleteLater)
    hilo.finished.connect(hilo.deleteLater)
    hilo.start()
    return hilo
