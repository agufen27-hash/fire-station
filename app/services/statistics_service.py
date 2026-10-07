"""
Estadísticas gerenciales de personal y vehículos entre dos fechas.

    reporte = calcular_estadisticas(date(2026, 1, 1), date(2026, 12, 31))

Alcance: partes CERRADOS cuya fecha cae en [fecha_desde, fecha_hasta]
(ambas inclusive). Los servicios EN CURSO no suman (todavía no tienen
regreso); se informan aparte en el resumen.

Horas (siempre con app/core/horarios.duracion: fecha + hora completas y
regla de la medianoche, 23:30 -> 01:15 = 1,75 h):
  - De una SALIDA (móvil): su salida -> regreso propio; lo que falte se toma
    del horario general del parte (mismo criterio que el Historial).
  - Del SERVICIO: el horario general del parte; si no lo tiene, desde la
    primera salida hasta el último regreso de sus móviles.
  - Duraciones negativas o mayores a MAX_HORAS_CREIBLES se descartan (dato
    mal cargado) y cuentan como 0 h.

Personal:
  - Horas en Escena: dotación embarcada (chofer, Jefe y embarcados): la
    duración de la salida de SU móvil.
  - Horas Cuartel: personal en base (operadores de guardia y apresto): la
    duración del servicio completo, que es lo que quedaron afectados.
  - Total Servicios: partes distintos en los que participó (una persona no
    figura dos veces en un mismo parte; si pasara, cuenta una vez).

Vehículos: salidas, horas operativas (suma de la duración de sus salidas) y
kilómetros. La base todavía NO registra odómetro de salida / regreso: los
km quedan en None ("sin dato") hasta que se carguen.

Todo sale de una sola consulta con carga anticipada (selectinload) de las
relaciones: sin consultas por fila.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import selectinload

from app.core.horarios import duracion, rango
from app.db import get_session
from app.models import (
    DotacionSalida,
    EstadoOperativo,
    Incidente,
    Movil,
    Personal,
    PersonalBase,
    SalidaUnidad,
)

MAX_HORAS_CREIBLES = 72


@dataclass
class EstadisticaBombero:
    personal_id: int
    legajo: str
    nombre: str
    servicios_escena: int = 0          # partes en los que salió embarcado (chofer / Jefe / embarcado)
    servicios_cuartel: int = 0         # partes en los que quedó en base (operador / apresto)
    horas_escena: float = 0.0
    horas_cuartel: float = 0.0
    _partes: Set[int] = field(default_factory=set, repr=False)

    @property
    def total_servicios(self) -> int:
        return len(self._partes)

    @property
    def horas_totales(self) -> float:
        return self.horas_escena + self.horas_cuartel


@dataclass
class EstadisticaMovil:
    movil_id: int
    nombre: str
    tipo: str
    salidas: int = 0
    horas: float = 0.0
    km_recorridos: Optional[float] = None   # sin odómetro en la base: None = sin dato


@dataclass
class ReporteEstadisticas:
    fecha_desde: date
    fecha_hasta: date
    generado_en: datetime
    servicios: int = 0                        # partes cerrados del período
    servicios_en_curso: int = 0               # excluidos (sin regreso)
    horas_servicio: float = 0.0               # suma de la duración de cada parte
    salidas_moviles: int = 0
    por_tipo: List[Tuple[str, int]] = field(default_factory=list)
    bomberos: List[EstadisticaBombero] = field(default_factory=list)
    moviles: List[EstadisticaMovil] = field(default_factory=list)

    @property
    def bomberos_participantes(self) -> int:
        return sum(1 for b in self.bomberos if b.total_servicios)

    @property
    def horas_hombre(self) -> float:
        return sum(b.horas_totales for b in self.bomberos)


# ---------------------------------------------------------------------------
# Duraciones
# ---------------------------------------------------------------------------

def _horas(fecha_ini: Optional[date], hora_ini: Optional[time],
           fecha_fin: Optional[date], hora_fin: Optional[time]) -> float:
    """Horas creíbles entre salida y regreso (0 si falta un dato o no cierra)."""
    if fecha_fin is not None and fecha_ini is not None and fecha_fin < fecha_ini:
        return 0.0  # regreso con fecha anterior a la salida: dato mal cargado
    delta = duracion(fecha_ini, hora_ini, fecha_fin, hora_fin)
    if delta is None:
        return 0.0
    horas = delta.total_seconds() / 3600
    return horas if 0 <= horas <= MAX_HORAS_CREIBLES else 0.0


def horas_salida(su: SalidaUnidad, inc: Incidente) -> float:
    """Duración de la salida de un móvil (lo que falte, del horario general)."""
    return _horas(su.fecha_salida or inc.fecha_salida or inc.fecha, su.hora_salida or inc.hora_salida,
                  su.fecha_llegada or inc.fecha_llegada, su.hora_regreso or inc.hora_regreso)


def horas_servicio(inc: Incidente) -> float:
    """Duración del servicio: horario general o, si no está, de la primera
    salida al último regreso de sus móviles."""
    general = _horas(inc.fecha_salida or inc.fecha, inc.hora_salida, inc.fecha_llegada, inc.hora_regreso)
    if general:
        return general
    inicios, fines = [], []
    for su in inc.salidas_unidad:
        inicio, fin = rango(su.fecha_salida or inc.fecha, su.hora_salida, su.fecha_llegada, su.hora_regreso)
        if inicio and fin and timedelta(0) <= fin - inicio <= timedelta(hours=MAX_HORAS_CREIBLES):
            inicios.append(inicio)
            fines.append(fin)
    if not inicios:
        return 0.0
    horas = (max(fines) - min(inicios)).total_seconds() / 3600
    return horas if 0 <= horas <= MAX_HORAS_CREIBLES else 0.0


# ---------------------------------------------------------------------------
# Cálculo
# ---------------------------------------------------------------------------

def _consultar_partes(session, fecha_desde: date, fecha_hasta: date) -> List[Incidente]:
    return (
        session.query(Incidente)
        .filter(Incidente.fecha >= fecha_desde, Incidente.fecha <= fecha_hasta)
        .options(
            selectinload(Incidente.tipo),
            selectinload(Incidente.salidas_unidad).selectinload(SalidaUnidad.movil),
            selectinload(Incidente.salidas_unidad).selectinload(SalidaUnidad.chofer),
            selectinload(Incidente.salidas_unidad).selectinload(SalidaUnidad.dotacion)
            .selectinload(DotacionSalida.personal),
            selectinload(Incidente.dotacion).selectinload(DotacionSalida.personal),
            selectinload(Incidente.personal_base).selectinload(PersonalBase.personal),
        )
        .order_by(Incidente.fecha, Incidente.id)
        .all()
    )


def _ficha(fichas: Dict[int, EstadisticaBombero], persona: Personal) -> EstadisticaBombero:
    ficha = fichas.get(persona.id)
    if ficha is None:
        ficha = EstadisticaBombero(persona.id, persona.legajo_display(), persona.nombre_completo())
        fichas[persona.id] = ficha
    return ficha


def _personas_embarcadas(su: SalidaUnidad) -> Iterable[Personal]:
    if su.chofer is not None:
        yield su.chofer
    for fila in su.dotacion:
        if fila.personal is not None:
            yield fila.personal


def calcular_estadisticas(fecha_desde: date, fecha_hasta: date,
                          incluir_moviles_inactivos: bool = True) -> ReporteEstadisticas:
    """Métricas de personal y vehículos de los partes del período. Todos los
    móviles del parque aparecen (también con 0 salidas, para ver la flota
    ociosa); sin `incluir_moviles_inactivos` se omiten los dados de baja que
    no salieron en el período."""
    if fecha_hasta < fecha_desde:
        raise ValueError("La fecha 'hasta' es anterior a la fecha 'desde'.")
    reporte = ReporteEstadisticas(fecha_desde, fecha_hasta, datetime.now())
    fichas: Dict[int, EstadisticaBombero] = {}
    moviles: Dict[int, EstadisticaMovil] = {}
    por_tipo: Counter = Counter()

    with get_session() as session:
        for movil in session.query(Movil).order_by(Movil.nombre_identificador):
            if movil.activo or incluir_moviles_inactivos:
                moviles[movil.id] = EstadisticaMovil(movil.id, movil.nombre_identificador, movil.tipo or "—")

        for inc in _consultar_partes(session, fecha_desde, fecha_hasta):
            if inc.estado_operativo != EstadoOperativo.CERRADO.value:
                reporte.servicios_en_curso += 1
                continue
            reporte.servicios += 1
            por_tipo[inc.tipo.nombre if inc.tipo else "Sin tipo"] += 1
            duracion_parte = horas_servicio(inc)
            reporte.horas_servicio += duracion_parte

            embarcados_en_parte: Set[int] = set()
            for su in inc.salidas_unidad:
                horas = horas_salida(su, inc)
                reporte.salidas_moviles += 1
                if su.movil is not None:
                    estadistica = moviles.get(su.movil_id) or moviles.setdefault(
                        su.movil_id, EstadisticaMovil(su.movil_id, su.movil.nombre_identificador, su.movil.tipo or "—"))
                    estadistica.salidas += 1
                    estadistica.horas += horas
                vistos_en_salida: Set[int] = set()
                for persona in _personas_embarcadas(su):
                    if persona.id in vistos_en_salida:
                        continue
                    vistos_en_salida.add(persona.id)
                    ficha = _ficha(fichas, persona)
                    ficha.horas_escena += horas
                    if persona.id not in embarcados_en_parte:
                        ficha.servicios_escena += 1
                        embarcados_en_parte.add(persona.id)
                    ficha._partes.add(inc.id)

            # Dotaciones de antes de la Fase 7 (sin SalidaUnidad): la duración del servicio.
            for fila in inc.dotacion:
                if fila.salida_unidad_id is not None or fila.personal is None:
                    continue
                if fila.personal_id in embarcados_en_parte:
                    continue
                ficha = _ficha(fichas, fila.personal)
                ficha.horas_escena += duracion_parte
                ficha.servicios_escena += 1
                ficha._partes.add(inc.id)
                embarcados_en_parte.add(fila.personal_id)

            en_base: Set[int] = set()
            for base in inc.personal_base:
                if base.personal is None or base.personal_id in en_base or base.personal_id in embarcados_en_parte:
                    continue
                en_base.add(base.personal_id)
                ficha = _ficha(fichas, base.personal)
                ficha.horas_cuartel += duracion_parte
                ficha.servicios_cuartel += 1
                ficha._partes.add(inc.id)

    reporte.por_tipo = sorted(por_tipo.items(), key=lambda par: (-par[1], par[0]))
    reporte.bomberos = sorted(fichas.values(), key=lambda b: (-b.horas_totales, b.nombre))
    reporte.moviles = sorted(moviles.values(), key=lambda m: (-m.salidas, -m.horas, m.nombre))
    return reporte
