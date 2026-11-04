"""
Modo offline del escritorio: caché local de lecturas + cola de escrituras.

Archivo: data/cache_nube.db (SQLite propio, separado de la base de partes).

  * CACHÉ DE LECTURAS: cada GET exitoso a Supabase (Libro de Guardia,
    bitácora, inventario, mantenimiento, fichas por QR) guarda la respuesta
    JSON con su hora. Sin red, ClienteSupabaseRest devuelve lo último guardado
    y marca `estado().en_linea = False` (la UI muestra "📴 datos de HH:MM").
  * COLA DE ESCRITURAS: alta de viaje, regreso y checklist hechos sin red se
    guardan con la hora real y un `cliente_id` (uuid) y se envían en orden al
    volver la conexión (app/workers/sincronizador_offline.py). La base usa el
    `cliente_id` para no duplicar si un envío llegó pero se perdió la
    respuesta; un rechazo de negocio (ej. "ya tiene un viaje abierto") queda
    como ERROR visible para revisarlo, sin trabar al resto.

Solo se encolan las escrituras de la guardia (viaje, regreso, revisión). Las
de administración (inventario, plantillas) requieren conexión: modificarlas a
ciegas podría pisar cambios de otra PC.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ESTADO_PENDIENTE = "PENDIENTE"
ESTADO_ERROR = "ERROR"

# RPC que se pueden encolar sin red (todas idempotentes por cliente_id o fecha).
OPERACIONES_ENCOLABLES = {
    "rpc_registrar_viaje": "Salida / viaje",
    "rpc_cerrar_viaje_manual": "Regreso de viaje",
    "rpc_mantenimiento_registrar": "Revisión de mantenimiento",
}

_LOCK = threading.RLock()
_ruta_db: Optional[Path] = None


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def configurar_ruta(ruta: Optional[Path]) -> None:
    """Para los tests (None = la ruta por defecto en data/)."""
    global _ruta_db
    _ruta_db = Path(ruta) if ruta else None
    _ESTADO.reiniciar()


def ruta_db() -> Path:
    if _ruta_db is not None:
        return _ruta_db
    from app.paths import get_writable_dir

    return get_writable_dir("data") / "cache_nube.db"


def _conectar() -> sqlite3.Connection:
    conexion = sqlite3.connect(ruta_db(), timeout=10)
    conexion.row_factory = sqlite3.Row
    conexion.executescript("""
        create table if not exists cache_lecturas (
            clave text primary key,
            datos text not null,
            guardado_at text not null
        );
        create table if not exists cola_escrituras (
            id integer primary key autoincrement,
            operacion text not null,
            argumentos text not null,
            descripcion text,
            creado_at text not null,
            intentos integer not null default 0,
            estado text not null default 'PENDIENTE',
            ultimo_error text
        );
    """)
    return conexion


@contextmanager
def _sesion():
    """Conexión que confirma al salir sin error y SIEMPRE se cierra."""
    conexion = _conectar()
    try:
        yield conexion
        conexion.commit()
    finally:
        conexion.close()


# -----------------------------------------------------------------------------
# Estado de la conexión (lo actualiza cada llamada real a Supabase)
# -----------------------------------------------------------------------------

@dataclass
class EstadoConexion:
    en_linea: bool = True
    ultimo_ok_at: Optional[datetime] = None
    ultimo_fallo_at: Optional[datetime] = None
    ultimo_error: str = ""
    dato_cacheado_at: Optional[datetime] = None   # hora del último dato servido desde la caché

    def reiniciar(self) -> None:
        self.__init__()


_ESTADO = EstadoConexion()


def estado() -> EstadoConexion:
    return _ESTADO


def marcar_en_linea() -> None:
    _ESTADO.en_linea = True
    _ESTADO.ultimo_ok_at = datetime.now(timezone.utc)
    _ESTADO.dato_cacheado_at = None


def marcar_sin_conexion(error: str) -> None:
    _ESTADO.en_linea = False
    _ESTADO.ultimo_fallo_at = datetime.now(timezone.utc)
    _ESTADO.ultimo_error = error


# -----------------------------------------------------------------------------
# Caché de lecturas
# -----------------------------------------------------------------------------

def clave_lectura(ruta: str, params: Optional[Dict[str, Any]] = None) -> str:
    return ruta + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))


def guardar_lectura(clave: str, datos: Any) -> None:
    with _LOCK, _sesion() as conexion:
        conexion.execute("insert or replace into cache_lecturas (clave, datos, guardado_at) values (?, ?, ?)",
                         (clave, json.dumps(datos, ensure_ascii=False), _ahora()))


def leer_lectura(clave: str) -> Optional[tuple]:
    """(datos, guardado_at) o None si nunca se leyó con conexión."""
    with _LOCK, _sesion() as conexion:
        fila = conexion.execute("select datos, guardado_at from cache_lecturas where clave = ?", (clave,)).fetchone()
    if fila is None:
        return None
    return json.loads(fila["datos"]), datetime.fromisoformat(fila["guardado_at"])


# -----------------------------------------------------------------------------
# Cola de escrituras
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class OperacionPendiente:
    id: int
    operacion: str
    argumentos: Dict[str, Any]
    descripcion: str
    creado_at: datetime
    intentos: int
    estado: str
    ultimo_error: Optional[str]

    @property
    def nombre(self) -> str:
        return OPERACIONES_ENCOLABLES.get(self.operacion, self.operacion)


def encolar(operacion: str, argumentos: Dict[str, Any], descripcion: str = "") -> int:
    if operacion not in OPERACIONES_ENCOLABLES:
        raise ValueError(f"{operacion} no se puede guardar sin conexión.")
    with _LOCK, _sesion() as conexion:
        cursor = conexion.execute(
            "insert into cola_escrituras (operacion, argumentos, descripcion, creado_at) values (?, ?, ?, ?)",
            (operacion, json.dumps(argumentos, ensure_ascii=False, default=str), descripcion, _ahora()))
        return int(cursor.lastrowid)


def pendientes(incluir_errores: bool = True) -> List[OperacionPendiente]:
    estados = (ESTADO_PENDIENTE, ESTADO_ERROR) if incluir_errores else (ESTADO_PENDIENTE,)
    with _LOCK, _sesion() as conexion:
        filas = conexion.execute(
            f"select * from cola_escrituras where estado in ({','.join('?' * len(estados))}) order by id",
            estados).fetchall()
    return [OperacionPendiente(
        id=f["id"], operacion=f["operacion"], argumentos=json.loads(f["argumentos"]),
        descripcion=f["descripcion"] or "", creado_at=datetime.fromisoformat(f["creado_at"]),
        intentos=f["intentos"], estado=f["estado"], ultimo_error=f["ultimo_error"]) for f in filas]


def contar() -> Dict[str, int]:
    with _LOCK, _sesion() as conexion:
        filas = conexion.execute("select estado, count(*) n from cola_escrituras group by estado").fetchall()
    return {f["estado"]: f["n"] for f in filas}


def _cerrar(op_id: int) -> None:
    with _LOCK, _sesion() as conexion:
        conexion.execute("delete from cola_escrituras where id = ?", (op_id,))


def _marcar(op_id: int, estado: str, error: str) -> None:
    with _LOCK, _sesion() as conexion:
        conexion.execute("update cola_escrituras set estado = ?, ultimo_error = ?, intentos = intentos + 1 "
                         "where id = ?", (estado, error[:500], op_id))


def descartar(op_id: int) -> None:
    """El operador decide no reintentar una operación en ERROR."""
    _cerrar(op_id)


def reintentar_errores() -> None:
    with _LOCK, _sesion() as conexion:
        conexion.execute("update cola_escrituras set estado = ? where estado = ?", (ESTADO_PENDIENTE, ESTADO_ERROR))


@dataclass(frozen=True)
class ResultadoSincronizacion:
    enviadas: int = 0
    rechazadas: int = 0
    restantes: int = 0
    sin_conexion: bool = False
    detalle_rechazos: tuple = ()


def sincronizar(enviar: Callable[[str, Dict[str, Any]], Any],
                es_sin_conexion: Callable[[Exception], bool]) -> ResultadoSincronizacion:
    """Envía la cola en orden. `enviar(operacion, argumentos)` devuelve el JSON
    de la RPC ({"ok": ...}). Sin conexión se corta y lo demás queda para la
    próxima; un rechazo de negocio queda en ERROR y se sigue con el resto."""
    enviadas = rechazadas = 0
    rechazos = []
    for op in pendientes(incluir_errores=False):
        try:
            respuesta = enviar(op.operacion, op.argumentos)
        except Exception as exc:  # noqa: BLE001 - la red o el servidor
            if es_sin_conexion(exc):
                marcar_sin_conexion(str(exc))
                return ResultadoSincronizacion(enviadas, rechazadas, len(pendientes(False)), True, tuple(rechazos))
            _marcar(op.id, ESTADO_ERROR, str(exc))
            rechazadas += 1
            rechazos.append(f"{op.nombre}: {exc}")
            continue
        marcar_en_linea()
        if isinstance(respuesta, dict) and respuesta.get("ok"):
            _cerrar(op.id)
            enviadas += 1
        else:
            motivo = respuesta.get("motivo") if isinstance(respuesta, dict) else repr(respuesta)
            _marcar(op.id, ESTADO_ERROR, str(motivo))
            rechazadas += 1
            rechazos.append(f"{op.nombre} ({op.descripcion}): {motivo}")
    return ResultadoSincronizacion(enviadas, rechazadas, 0, False, tuple(rechazos))
