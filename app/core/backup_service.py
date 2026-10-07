"""
Respaldo automático de la base del cuartel al cerrar Fire Station.

    realizar_backup_cierre(db_path)            # -> True / False, nunca lanza

Qué hace:
  1. Snapshot consistente de la base con la API nativa de SQLite
     (`sqlite3.Connection.backup`): copia página por página dentro de una
     transacción de lectura, así el resultado es una base íntegra aunque haya
     escrituras recientes (también en modo WAL, donde copiar el .db a mano
     dejaría afuera lo que todavía está en el -wal). Se verifica con
     `PRAGMA integrity_check` antes de darlo por bueno.
  2. Lo guarda como backup_firestation_YYYYMMDD_HHMMSS.db; si la base supera
     UMBRAL_ZIP_BYTES se guarda comprimido (.zip con ese .db adentro).
  3. Archivos críticos chicos que no viven en la base (configuración del
     cuartel, padrón, firmas): backup_firestation_YYYYMMDD_HHMMSS_archivos.zip,
     con el mismo sello de tiempo.
  4. Rotación: conserva los últimos `max_backups` respaldos (cada uno = su
     .db/.zip + su _archivos.zip) y borra los más viejos.

Todo se escribe primero a un archivo temporal y se renombra al final: un
cierre abrupto nunca deja un respaldo a medias con nombre válido (los
temporales huérfanos se limpian en la próxima corrida).

Los errores se registran con detalle en logs/app.log y la función devuelve
False: un fallo del respaldo nunca debe trabar el cierre de la app.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from app.core.app_log import asegurar_log_app

log = logging.getLogger(__name__)

PREFIJO = "backup_firestation_"
SUFIJO_ARCHIVOS = "_archivos"
SUFIJO_TEMPORAL = ".tmp"
UMBRAL_ZIP_BYTES = 25 * 1024 * 1024   # bases de más de 25 MB se guardan comprimidas
PAGINAS_POR_PASO = 1024               # backup por tramos: no bloquea la base de una sola vez
MAX_BACKUPS_DEFECTO = 15

# Relativos a la carpeta data/ (la de la base). Chicos y sin reemplazo si se pierden.
ARCHIVOS_CRITICOS = ("config.json", "cuerpo_activo.json", "seed_bomberos.json", "Reporte de bomberos.xlsx")
CARPETAS_CRITICAS = ("firmas",)

_RE_SELLO = re.compile(rf"^{PREFIJO}(\d{{8}}_\d{{6}})")


# ---------------------------------------------------------------------------
# Log a archivo (la app no configura logging global; el .exe no tiene consola)
# ---------------------------------------------------------------------------

def _asegurar_log_archivo() -> None:
    asegurar_log_app()  # logs/app.log compartido (app/core/app_log.py)


# ---------------------------------------------------------------------------
# Pasos
# ---------------------------------------------------------------------------

def _snapshot_sqlite(origen: Path, destino: Path) -> None:
    """Copia consistente con la API de backup de SQLite + verificación."""
    # Solo lectura: el respaldo jamás puede modificar la base original. as_uri()
    # codifica espacios y la unidad de Windows ("Fire Station" -> Fire%20Station).
    fuente = sqlite3.connect(f"{origen.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        copia = sqlite3.connect(str(destino))
        try:
            fuente.backup(copia, pages=PAGINAS_POR_PASO)
            resultado = copia.execute("PRAGMA integrity_check").fetchone()
            if not resultado or resultado[0] != "ok":
                raise sqlite3.DatabaseError(f"integrity_check del respaldo: {resultado}")
        finally:
            copia.close()
    finally:
        fuente.close()


def _comprimir(archivos: Sequence[tuple], destino: Path) -> None:
    """[(ruta en disco, nombre dentro del zip)] -> destino.zip (vía temporal)."""
    temporal = destino.with_name(destino.name + SUFIJO_TEMPORAL)
    with zipfile.ZipFile(temporal, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for ruta, nombre in archivos:
            z.write(ruta, nombre)
    os.replace(temporal, destino)


def _archivos_criticos(carpeta_datos: Path) -> List[tuple]:
    encontrados: List[tuple] = []
    for nombre in ARCHIVOS_CRITICOS:
        ruta = carpeta_datos / nombre
        if ruta.is_file():
            encontrados.append((ruta, nombre))
    for nombre in CARPETAS_CRITICAS:
        carpeta = carpeta_datos / nombre
        if carpeta.is_dir():
            for ruta in sorted(carpeta.rglob("*")):
                if ruta.is_file():
                    encontrados.append((ruta, ruta.relative_to(carpeta_datos).as_posix()))
    return encontrados


def _sello(ruta: Path) -> Optional[str]:
    coincidencia = _RE_SELLO.match(ruta.name)
    return coincidencia.group(1) if coincidencia else None


def _rotar(carpeta: Path, max_backups: int) -> List[Path]:
    """Conserva los últimos `max_backups` sellos de tiempo; borra el resto
    (y los temporales huérfanos de cierres abruptos). Devuelve lo borrado."""
    borrados: List[Path] = []
    for temporal in carpeta.glob(f"{PREFIJO}*{SUFIJO_TEMPORAL}"):
        try:
            temporal.unlink()
            borrados.append(temporal)
        except OSError:
            pass
    sellos = sorted({s for s in (_sello(r) for r in carpeta.glob(f"{PREFIJO}*")) if s}, reverse=True)
    for viejo in sellos[max(max_backups, 1):]:
        for ruta in carpeta.glob(f"{PREFIJO}{viejo}*"):
            try:
                ruta.unlink()
                borrados.append(ruta)
            except OSError as e:
                log.warning("Respaldo: no se pudo borrar %s en la rotación: %s", ruta.name, e)
    return borrados


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def realizar_backup_cierre(db_path: str, backup_dir: Optional[str] = None,
                           max_backups: int = MAX_BACKUPS_DEFECTO,
                           incluir_archivos: bool = True) -> bool:
    """Respalda la base (y los archivos críticos de su carpeta data/) en
    `backup_dir` y rota. Sin `backup_dir`, usa la carpeta "backups" al lado
    de la base (data/backups para la base real; así una base de prueba
    nunca rota los respaldos del cuartel). Devuelve True si el respaldo de
    la base quedó hecho; nunca lanza."""
    _asegurar_log_archivo()
    inicio = datetime.now()
    try:
        origen = Path(db_path)
        if not origen.is_file():
            log.warning("Respaldo omitido: no existe la base %s", origen)
            return False
        carpeta = Path(backup_dir) if backup_dir else origen.parent / "backups"
        if not carpeta.is_absolute():
            carpeta = origen.parent.parent / carpeta  # "data/backups" relativo a la raíz de la app
        carpeta.mkdir(parents=True, exist_ok=True)

        sello = inicio.strftime("%Y%m%d_%H%M%S")
        nombre_db = f"{PREFIJO}{sello}.db"
        temporal_db = carpeta / (nombre_db + SUFIJO_TEMPORAL)
        _snapshot_sqlite(origen, temporal_db)

        tamano = temporal_db.stat().st_size
        if tamano > UMBRAL_ZIP_BYTES:
            destino = carpeta / f"{PREFIJO}{sello}.zip"
            _comprimir([(temporal_db, nombre_db)], destino)
            temporal_db.unlink(missing_ok=True)
        else:
            destino = carpeta / nombre_db
            os.replace(temporal_db, destino)

        archivos_zip = None
        if incluir_archivos:
            criticos = _archivos_criticos(origen.parent)
            if criticos:
                archivos_zip = carpeta / f"{PREFIJO}{sello}{SUFIJO_ARCHIVOS}.zip"
                try:
                    _comprimir(criticos, archivos_zip)
                except OSError as e:  # un archivo bloqueado no invalida el respaldo de la base
                    log.warning("Respaldo: no se pudieron empaquetar los archivos críticos: %s", e)
                    archivos_zip = None

        borrados = _rotar(carpeta, max_backups)
        demora = (datetime.now() - inicio).total_seconds()
        log.info("Respaldo de cierre OK: %s (%.1f MB%s)%s en %.2f s; rotación: %s borrado(s).",
                 destino.name, tamano / 1_048_576, ", comprimido" if destino.suffix == ".zip" else "",
                 f" + {archivos_zip.name}" if archivos_zip else "", demora, len(borrados))
        return True
    except Exception:  # noqa: BLE001 - el respaldo nunca traba el cierre
        log.exception("Respaldo de cierre FALLÓ (base %s, carpeta %s)", db_path, backup_dir)
        return False


def listar_backups(backup_dir: str) -> List[Path]:
    """Respaldos de la base (más nuevo primero), sin los zips de archivos."""
    carpeta = Path(backup_dir)
    if not carpeta.is_dir():
        return []
    rutas: Iterable[Path] = (r for r in carpeta.glob(f"{PREFIJO}*")
                             if r.suffix in (".db", ".zip") and SUFIJO_ARCHIVOS not in r.stem)
    return sorted(rutas, key=lambda r: r.name, reverse=True)
