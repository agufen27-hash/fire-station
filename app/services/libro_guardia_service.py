"""
Libro de Guardia y Bitácora: lectura / escritura en Supabase (PostgREST) para
la página "Libro de Guardia" del escritorio.

Objetos de Supabase que usa (ver supabase/migrations/*libro_guardia*):
  * vista  libro_guardia_bitacora      viajes + asistencias en una sola grilla
  * rpc    rpc_resumen_libro_guardia   KPIs (viajes activos, en cuartel, guardias hoy)
  * rpc    rpc_registrar_viaje         alta de salida (dispara los avisos de WhatsApp)
  * rpc    rpc_cerrar_viaje_manual     regreso con fecha y hora completas

Mismas credenciales que el sync del padrón (app/services/supabase_client.py):
env vars SUPABASE_URL / SUPABASE_SERVICE_KEY o data/config.json. Todas las
llamadas son bloqueantes: la UI las corre en un QThread
(app/workers/libro_guardia_worker.py).

Los IDs de bombero son los de la base local: el sync del padrón sube
`personal.id` como `personal_cloud.id`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services.supabase_rest import ClienteSupabaseRest, SupabaseRechazo

log = logging.getLogger(__name__)

VISTA_BITACORA = "libro_guardia_bitacora"
# Hora del cuartel (America/Argentina/Cordoba): UTC-3 fijo, sin horario de verano.
TZ_CUARTEL = timezone(timedelta(hours=-3), "ART")
LIMITE_FILAS = 1000

ESTADO_EN_CURSO = "EN_CURSO"
ESTADO_FINALIZADO = "FINALIZADO"
ESTADO_SIN_EGRESO = "SIN_EGRESO"

METODOS = {
    "UBICACION": "GPS (bot)",
    "TOKEN": "QR / código (bot)",
    "BOT": "WhatsApp",
    "ESCRITORIO": "Escritorio",
    "SQL": "Manual",
}

MOTIVOS = {
    "BOMBERO_NO_REGISTRADO_O_INACTIVO":
        "El bombero no figura (o está inactivo) en la nube. Sincronizá el padrón "
        "desde Personal y Unidades → ☁️ Sincronizar a Cloud.",
    "DATOS_INCOMPLETOS": "Faltan el destino o el vehículo.",
    "YA_TIENE_VIAJE_ABIERTO": "Ese bombero ya tiene un viaje abierto: marcá primero su regreso.",
    "VIAJE_INEXISTENTE": "El viaje ya no existe (¿lo borraron?). Actualizá la lista.",
    "VIAJE_YA_FINALIZADO": "Ese viaje ya estaba finalizado. Actualizá la lista.",
    "FECHA_INVALIDA": "La fecha de regreso no es válida.",
    "LLEGADA_ANTERIOR_A_SALIDA": "El regreso no puede ser anterior a la salida.",
    "LLEGADA_EN_EL_FUTURO": "El regreso no puede ser posterior al momento actual.",
}


class LibroGuardiaError(SupabaseRechazo):
    """Rechazo de negocio (motivo de la RPC) con un texto listo para mostrar."""


@dataclass(frozen=True)
class ResumenGuardia:
    viajes_activos: int
    personal_en_cuartel: int
    guardias_del_dia: int


@dataclass(frozen=True)
class RegistroBitacora:
    id: str
    tipo_registro: str          # VIAJE | ASISTENCIA
    bombero_id: int
    bombero: str
    legajo: Optional[str]
    jerarquia: Optional[str]
    actividad: str
    vehiculo: Optional[str]
    inicio: datetime            # hora del cuartel
    fin: Optional[datetime]
    minutos: Optional[int]
    estado: str
    metodo_registro: str

    @property
    def es_viaje(self) -> bool:
        return self.tipo_registro == "VIAJE"

    @property
    def abierto(self) -> bool:
        return self.estado == ESTADO_EN_CURSO

    @property
    def metodo_legible(self) -> str:
        return METODOS.get(self.metodo_registro, self.metodo_registro.title())


def _a_hora_cuartel(texto: Optional[str]) -> Optional[datetime]:
    if not texto:
        return None
    return datetime.fromisoformat(texto).astimezone(TZ_CUARTEL)


def registro_desde_fila(fila: Dict[str, Any]) -> RegistroBitacora:
    return RegistroBitacora(
        id=str(fila["id"]),
        tipo_registro=fila.get("tipo_registro") or "ASISTENCIA",
        bombero_id=int(fila["bombero_id"]),
        bombero=fila.get("bombero") or "—",
        legajo=fila.get("legajo"),
        jerarquia=fila.get("jerarquia"),
        actividad=fila.get("actividad") or "—",
        vehiculo=fila.get("vehiculo"),
        inicio=_a_hora_cuartel(fila["inicio"]),
        fin=_a_hora_cuartel(fila.get("fin")),
        minutos=fila.get("minutos"),
        estado=fila.get("estado") or ESTADO_FINALIZADO,
        metodo_registro=fila.get("metodo_registro") or "",
    )


def inicio_de_mes(anio: int, mes: int) -> datetime:
    return datetime(anio, mes, 1, tzinfo=TZ_CUARTEL)


def fin_de_mes(anio: int, mes: int) -> datetime:
    """Primer instante del mes siguiente (límite exclusivo)."""
    return inicio_de_mes(anio + (mes == 12), mes % 12 + 1)


class LibroGuardiaCliente(ClienteSupabaseRest):
    def _exigir_ok(self, resultado: Any) -> Dict[str, Any]:  # type: ignore[override]
        return ClienteSupabaseRest._exigir_ok(resultado, MOTIVOS, LibroGuardiaError)

    # -- Lecturas -----------------------------------------------------------------

    def resumen(self) -> ResumenGuardia:
        r = self._rpc("rpc_resumen_libro_guardia") or {}
        return ResumenGuardia(
            viajes_activos=int(r.get("viajes_activos") or 0),
            personal_en_cuartel=int(r.get("personal_en_cuartel") or 0),
            guardias_del_dia=int(r.get("guardias_del_dia") or 0),
        )

    def bitacora(self, desde: datetime, hasta: Optional[datetime] = None,
                 limite: int = LIMITE_FILAS) -> List[RegistroBitacora]:
        """Registros con inicio en [desde, hasta), más recientes primero.
        Los EN_CURSO se incluyen siempre aunque hayan empezado antes de `desde`."""
        filtro_rango = f"inicio.gte.{desde.isoformat()}"
        if hasta is not None:
            filtro_rango = f"and(inicio.gte.{desde.isoformat()},inicio.lt.{hasta.isoformat()})"
        params = {
            "select": "*",
            "or": f"({filtro_rango},estado.eq.{ESTADO_EN_CURSO})",
            "order": "inicio.desc",
            "limit": str(limite),
        }
        filas = self._pedir("GET", VISTA_BITACORA, params=params) or []
        return [registro_desde_fila(f) for f in filas]

    def bitacora_mes(self, anio: int, mes: int) -> List[RegistroBitacora]:
        """Solo lo iniciado dentro del mes (para la planilla del seguro / ART)."""
        params = {
            "select": "*",
            "and": f"(inicio.gte.{inicio_de_mes(anio, mes).isoformat()},"
                   f"inicio.lt.{fin_de_mes(anio, mes).isoformat()})",
            "order": "inicio.asc",
            "limit": "10000",
        }
        filas = self._pedir("GET", VISTA_BITACORA, params=params) or []
        return [registro_desde_fila(f) for f in filas]

    # -- Escrituras -----------------------------------------------------------------

    def registrar_salida(self, bombero_id: int, destino: str, vehiculo: str) -> Dict[str, Any]:
        return self._exigir_ok(self._rpc("rpc_registrar_viaje", {
            "p_bombero_id": bombero_id,
            "p_destino": destino.strip(),
            "p_vehiculo": vehiculo.strip(),
            "p_origen": "ESCRITORIO",
        }))

    def marcar_regreso(self, viaje_id: str, fecha_llegada: datetime) -> Dict[str, Any]:
        if fecha_llegada.tzinfo is None:
            fecha_llegada = fecha_llegada.replace(tzinfo=TZ_CUARTEL)
        return self._exigir_ok(self._rpc("rpc_cerrar_viaje_manual", {
            "p_viaje_id": viaje_id,
            "p_fecha_llegada": fecha_llegada.isoformat(),
        }))


def mes_anterior(hoy: Optional[date] = None) -> tuple[int, int]:
    hoy = hoy or date.today()
    return (hoy.year - 1, 12) if hoy.month == 1 else (hoy.year, hoy.month - 1)
