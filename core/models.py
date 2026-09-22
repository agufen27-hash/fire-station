"""
Modelo de datos unificado para el Cuartel de Bomberos Voluntarios "Osvaldo R. Rossi"
(Adelia María, Córdoba - C 59 / R 3).

Un mismo registro (RegistroServicio) alimenta:
  - La Planilla de Control de Siniestros (PCS.xlsx)  -> carátula / base de comunicaciones.
  - La Planilla de Control de Dotaciones (PCD2.xlsx)  -> móviles, roles y firmas.
  - El bot de Playwright que carga el siniestro en el portal RUBA.

Todas las clases son dataclasses serializables a dict/JSON (fechas y horas en
formato ISO, enums por su .value) para poder persistir en SQLite y para
alimentar tanto OpenPyXL (mapeo por celda) como Playwright (mapeo por
selector, ver ruba_mapping.json).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, time, datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumeraciones / catálogos
# ---------------------------------------------------------------------------

class MedioContacto(str, Enum):
    PERSONAL = "Personal"
    RADIO = "Radio"
    TELEFONO = "Teléfono"


class TipoSiniestro(str, Enum):
    ACCIDENTE = "Accidente"
    FACTOR_CLIMATICO = "Factor Climático"
    INCENDIO = "Incendio"
    RESCATE = "Rescate"
    MATERIALES_PELIGROSOS = "Mat-Pel"
    OTRO = "Otro"


# Subtipos sugeridos por tipo, para poblar combobox dependientes en la GUI.
# El campo `subtipo` del modelo se guarda como texto libre para no atar el
# dato a que la lista quede 100% completa/actualizada.
SUBTIPOS_POR_TIPO: Dict[TipoSiniestro, List[str]] = {
    TipoSiniestro.ACCIDENTE: ["Colisión", "Vuelco", "Despiste", "Atropello", "Otro"],
    TipoSiniestro.FACTOR_CLIMATICO: ["Inundación", "Viento/Tormenta", "Granizo", "Otro"],
    TipoSiniestro.INCENDIO: ["Estructural", "Forestal", "Vehicular", "Pastizal", "Otro"],
    TipoSiniestro.RESCATE: ["Animal", "Personas atrapadas", "Altura", "Otro"],
    TipoSiniestro.MATERIALES_PELIGROSOS: ["Derrame", "Fuga", "Otro"],
    TipoSiniestro.OTRO: ["Otro"],
}


class Magnitud(int, Enum):
    BAJA = 1
    MEDIA = 2
    ALTA = 3


class CausaSiniestro(str, Enum):
    NEGLIGENCIA = "Negligencia"
    INTENCIONAL = "Intencional"
    DESCONOCIDA = "Desconocida"
    NATURAL = "Natural"


# ---------------------------------------------------------------------------
# Bloques reutilizables
# ---------------------------------------------------------------------------

@dataclass
class PersonaGrado:
    """Un bombero identificado por nombre y grado (Bombero, Cabo, Sargento, etc.)."""
    nombre: str = ""
    grado: str = ""

    def esta_vacio(self) -> bool:
        return not self.nombre.strip()

    def nombre_completo(self) -> str:
        if self.esta_vacio():
            return ""
        return f"{self.grado} {self.nombre}".strip()


# ---------------------------------------------------------------------------
# PCS - Planilla de Control de Siniestros
# ---------------------------------------------------------------------------

@dataclass
class PedidoSocorro:
    """Carátula / base de comunicaciones (sección superior de la PCS)."""
    n_siniestro: str = ""
    dia: str = ""                      # día de la semana (se autocompleta desde `fecha`)
    fecha: date = field(default_factory=date.today)
    hora_llamado: time = field(default_factory=lambda: datetime.now().time().replace(microsecond=0, second=0))

    comunico: str = ""                 # nombre de quien da aviso (denunciante)
    domicilio_denunciante: str = ""

    medio_contacto: MedioContacto = MedioContacto.TELEFONO
    medio_contacto_detalle: str = ""   # n° de teléfono / canal de radio usado

    recibio_aviso: PersonaGrado = field(default_factory=PersonaGrado)

    alarma_activada: bool = False
    sab: str = ""                      # código de Sistema de Alarma de Bomberos, si aplica
    hora_alarma: Optional[time] = None

    autorizo_salida: PersonaGrado = field(default_factory=PersonaGrado)

    def dia_semana_texto(self) -> str:
        dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
        return dias[self.fecha.weekday()]


@dataclass
class DatosSiniestro:
    """Tipificación y ubicación del hecho (sección media de la PCS)."""
    tipo: TipoSiniestro = TipoSiniestro.OTRO
    subtipo: str = ""
    magnitud: Magnitud = Magnitud.BAJA

    localidad: str = "Adelia María"
    calle_lugar: str = ""
    zona_barrio: str = ""
    descripcion: str = ""


@dataclass
class PersonalCuartel:
    """Personal presente/disponible en cuartel al momento del llamado (pie de la PCS)."""
    operador_guardia_1: PersonaGrado = field(default_factory=PersonaGrado)
    operador_guardia_2: PersonaGrado = field(default_factory=PersonaGrado)
    personal_reserva: List[PersonaGrado] = field(default_factory=list)  # máx. 20
    confecciono_planilla: PersonaGrado = field(default_factory=PersonaGrado)

    MAX_RESERVA = 20

    def agregar_reserva(self, persona: PersonaGrado) -> None:
        if len(self.personal_reserva) >= self.MAX_RESERVA:
            raise ValueError(f"El personal de reserva no puede superar {self.MAX_RESERVA} bomberos.")
        self.personal_reserva.append(persona)

    def total_cuartel(self) -> int:
        """Operadores de guardia + reserva presentes (no vacíos)."""
        base = [self.operador_guardia_1, self.operador_guardia_2, *self.personal_reserva]
        return sum(1 for p in base if not p.esta_vacio())


# ---------------------------------------------------------------------------
# PCD - Planilla de Control de Dotaciones
# ---------------------------------------------------------------------------

@dataclass
class PersonalDotacion:
    """Roles a bordo de un móvil: Jefe de Dotación, Jefe de Seguridad, Chofer y
    Bomberos 4 al 12 (hasta 9 bomberos adicionales)."""
    jefe_dotacion: PersonaGrado = field(default_factory=PersonaGrado)
    jefe_seguridad: PersonaGrado = field(default_factory=PersonaGrado)
    chofer: PersonaGrado = field(default_factory=PersonaGrado)
    bomberos: List[PersonaGrado] = field(default_factory=list)  # roles 4 a 12 -> máx 9

    MAX_BOMBEROS = 9

    def agregar_bombero(self, persona: PersonaGrado) -> None:
        if len(self.bomberos) >= self.MAX_BOMBEROS:
            raise ValueError(f"No puede haber más de {self.MAX_BOMBEROS} bomberos adicionales por dotación.")
        self.bomberos.append(persona)

    def cantidad(self) -> int:
        base = [self.jefe_dotacion, self.jefe_seguridad, self.chofer, *self.bomberos]
        return sum(1 for p in base if not p.esta_vacio())


@dataclass
class Dotacion:
    """Una dotación despachada (móvil 1 o móvil 2) con su cronología y roles."""
    numero: int = 1                    # 1 o 2 (Dotación 1 / Dotación 2)
    movil: str = ""                    # unidad asignada, ej. "Autobomba 1"

    fecha_hora_salida: Optional[datetime] = None
    hora_arribo_qth: Optional[time] = None          # llegada al lugar del siniestro
    fecha_hora_regreso: Optional[datetime] = None   # regreso a cuartel

    personal: PersonalDotacion = field(default_factory=PersonalDotacion)

    informe_tactico: str = ""

    damnificados_personas: bool = False
    cantidad_damnificados_personas: int = 0
    damnificados_bomberos: bool = False

    superficie_afectada: str = ""      # ej. "150 m2", "2 ha"
    causa: Optional[CausaSiniestro] = None
    coordenadas_gps: str = ""          # "lat,lon"

    firma_jefe_dotacion: bool = False  # True una vez firmada en papel/tablet

    def tiempo_respuesta_minutos(self) -> Optional[float]:
        """Minutos entre salida y arribo a QTH, si ambos datos están cargados."""
        if not self.fecha_hora_salida or not self.hora_arribo_qth:
            return None
        arribo = datetime.combine(self.fecha_hora_salida.date(), self.hora_arribo_qth)
        delta = (arribo - self.fecha_hora_salida).total_seconds() / 60
        return delta if delta >= 0 else None


# ---------------------------------------------------------------------------
# Registro consolidado (PCS + PCD + control RUBA)
# ---------------------------------------------------------------------------

@dataclass
class RegistroServicio:
    """Registro completo de un servicio/siniestro: une PCS + PCD y agrega los
    metadatos necesarios para persistencia local y para la carga en RUBA."""

    id: Optional[int] = None
    creado_en: datetime = field(default_factory=datetime.now)
    actualizado_en: datetime = field(default_factory=datetime.now)

    pedido_socorro: PedidoSocorro = field(default_factory=PedidoSocorro)
    datos_siniestro: DatosSiniestro = field(default_factory=DatosSiniestro)
    personal_cuartel: PersonalCuartel = field(default_factory=PersonalCuartel)
    dotaciones: List[Dotacion] = field(default_factory=lambda: [Dotacion(numero=1)])

    # Control de integración con el portal oficial
    numero_ruba: Optional[str] = None
    cargado_ruba: bool = False
    fecha_carga_ruba: Optional[datetime] = None

    MAX_DOTACIONES = 2

    # -- Totales calculados ------------------------------------------------

    def total_cuartel(self) -> int:
        return self.personal_cuartel.total_cuartel()

    def total_servicio(self) -> int:
        return sum(d.personal.cantidad() for d in self.dotaciones)

    def total_general(self) -> int:
        return self.total_cuartel() + self.total_servicio()

    # -- Validación mínima ---------------------------------------------------

    def validar(self) -> List[str]:
        errores: List[str] = []
        if not self.pedido_socorro.n_siniestro.strip():
            errores.append("Falta el N° de siniestro.")
        if not self.datos_siniestro.calle_lugar.strip():
            errores.append("Falta la calle/lugar del siniestro.")
        if len(self.dotaciones) == 0:
            errores.append("Debe cargarse al menos una dotación.")
        if len(self.dotaciones) > self.MAX_DOTACIONES:
            errores.append(f"No puede haber más de {self.MAX_DOTACIONES} dotaciones (Dotación 1 y Dotación 2).")
        for d in self.dotaciones:
            if not d.movil.strip():
                errores.append(f"Falta el móvil asignado a la Dotación {d.numero}.")
        return errores

    # -- Serialización JSON / SQLite ----------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Dict 100% JSON-serializable (fechas/horas en ISO, enums por .value)."""
        return _serializar(asdict(self))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RegistroServicio":
        data = dict(data)

        data["creado_en"] = _parse_datetime(data.get("creado_en"))
        data["actualizado_en"] = _parse_datetime(data.get("actualizado_en"))
        data["fecha_carga_ruba"] = _parse_datetime(data.get("fecha_carga_ruba"))

        ps = data.get("pedido_socorro", {}) or {}
        ps["fecha"] = _parse_date(ps.get("fecha")) or date.today()
        ps["hora_llamado"] = _parse_time(ps.get("hora_llamado")) or time(0, 0)
        ps["hora_alarma"] = _parse_time(ps.get("hora_alarma"))
        ps["medio_contacto"] = MedioContacto(ps.get("medio_contacto", MedioContacto.TELEFONO.value))
        ps["recibio_aviso"] = PersonaGrado(**(ps.get("recibio_aviso") or {}))
        ps["autorizo_salida"] = PersonaGrado(**(ps.get("autorizo_salida") or {}))
        data["pedido_socorro"] = PedidoSocorro(**ps)

        ds = data.get("datos_siniestro", {}) or {}
        ds["tipo"] = TipoSiniestro(ds.get("tipo", TipoSiniestro.OTRO.value))
        ds["magnitud"] = Magnitud(ds.get("magnitud", Magnitud.BAJA.value))
        data["datos_siniestro"] = DatosSiniestro(**ds)

        pc = data.get("personal_cuartel", {}) or {}
        pc["operador_guardia_1"] = PersonaGrado(**(pc.get("operador_guardia_1") or {}))
        pc["operador_guardia_2"] = PersonaGrado(**(pc.get("operador_guardia_2") or {}))
        pc["personal_reserva"] = [PersonaGrado(**p) for p in (pc.get("personal_reserva") or [])]
        pc["confecciono_planilla"] = PersonaGrado(**(pc.get("confecciono_planilla") or {}))
        data["personal_cuartel"] = PersonalCuartel(**pc)

        dotaciones = []
        for d in data.get("dotaciones", []) or []:
            d = dict(d)
            d["fecha_hora_salida"] = _parse_datetime(d.get("fecha_hora_salida"))
            d["hora_arribo_qth"] = _parse_time(d.get("hora_arribo_qth"))
            d["fecha_hora_regreso"] = _parse_datetime(d.get("fecha_hora_regreso"))
            d["causa"] = CausaSiniestro(d["causa"]) if d.get("causa") else None

            personal = d.get("personal", {}) or {}
            personal["jefe_dotacion"] = PersonaGrado(**(personal.get("jefe_dotacion") or {}))
            personal["jefe_seguridad"] = PersonaGrado(**(personal.get("jefe_seguridad") or {}))
            personal["chofer"] = PersonaGrado(**(personal.get("chofer") or {}))
            personal["bomberos"] = [PersonaGrado(**p) for p in (personal.get("bomberos") or [])]
            d["personal"] = PersonalDotacion(**personal)

            dotaciones.append(Dotacion(**d))
        data["dotaciones"] = dotaciones

        return cls(**data)

    # -- Vista plana para OpenPyXL / Playwright ------------------------------

    def to_flat_dict(self) -> Dict[str, Any]:
        """Aplana el registro con claves 'punto' (ver ruba_mapping.json y las
        rutinas de relleno de PCS.xlsx / PCD2.xlsx). Las dotaciones usan un
        índice 1-based: 'dotaciones.1.movil', 'dotaciones.2.personal.chofer.nombre', etc."""
        flat: Dict[str, Any] = {}

        def volcar(prefijo: str, obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    volcar(f"{prefijo}.{k}" if prefijo else k, v)
            elif isinstance(obj, list):
                for i, v in enumerate(obj, start=1):
                    volcar(f"{prefijo}.{i}", v)
            else:
                flat[prefijo] = obj

        volcar("", self.to_dict())
        flat["totales.total_cuartel"] = self.total_cuartel()
        flat["totales.total_servicio"] = self.total_servicio()
        flat["totales.total_general"] = self.total_general()
        flat["pedido_socorro.dia"] = self.pedido_socorro.dia or self.pedido_socorro.dia_semana_texto()
        return flat


# ---------------------------------------------------------------------------
# Helpers de (de)serialización
# ---------------------------------------------------------------------------

def _serializar(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _serializar(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serializar(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    return obj


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _parse_time(value: Any) -> Optional[time]:
    if not value:
        return None
    if isinstance(value, time):
        return value
    return time.fromisoformat(value)
