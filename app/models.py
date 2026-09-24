"""
Modelo de datos SQLAlchemy para Fire Station (`data/fire_station.db`).

Tablas:
    tipos_incidente, categorias_incidente  -> árbol de tipificación relevado del RUBA.
    personal, moviles                      -> catálogos del cuartel.
    incidentes                             -> un siniestro/servicio.
    dotacion_salida                        -> quién (personal) fue en qué móvil y con qué rol,
                                               para un incidente dado (relación ternaria).
    damnificados_civiles                   -> civiles damnificados individualizados de un incidente.
    bienes_afectados                       -> inmuebles / rodados / rastrojos afectados.
    bomberos_damnificados                  -> bomberos lesionados en el servicio, con su atención médica.
"""

from __future__ import annotations

import enum
import json
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums de referencia (la columna en sí se guarda como texto plano en
# SQLite; estos enums son para uso en la UI y en validaciones de negocio).
# ---------------------------------------------------------------------------

class EstadoRuba(str, enum.Enum):
    PENDIENTE = "PENDIENTE"
    SINCRONIZADO = "SINCRONIZADO"
    ERROR = "ERROR"


class EstadoOperativo(str, enum.Enum):
    """Ciclo de vida del servicio en el cuartel (independiente de RUBA)."""
    EN_CURSO = "EN_CURSO"   # borrador: dotación en el lugar, regreso pendiente
    CERRADO = "CERRADO"     # horarios completos: listo para planillas y RUBA


# Medios de contacto del pedido de socorro (planilla PCS) y cómo se rotulan
# en la celda ("WhatsApp: 3584123456", "Frecuencia: 154.200").
MEDIOS_CONTACTO = {"Teléfono": "Teléfono", "Celular": "Celular", "WhatsApp": "WhatsApp",
                   "Frecuencia Radial": "Frecuencia", "Presencial": "Presencial"}
# Medios cuyo "Número / Detalle" es un teléfono (va a RUBA como teléfono del solicitante).
MEDIOS_TELEFONICOS = ("Teléfono", "Celular", "WhatsApp")


class RolDotacion(str, enum.Enum):
    CHOFER = "CHOFER"
    A_CARGO = "A_CARGO"
    BOMBERO = "BOMBERO"


class Zona(str, enum.Enum):
    URBANA = "Urbana"
    RURAL = "Rural"


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------

class TipoIncidente(Base):
    __tablename__ = "tipos_incidente"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)

    categorias: Mapped[List["CategoriaIncidente"]] = relationship(
        back_populates="tipo", cascade="all, delete-orphan", order_by="CategoriaIncidente.nombre"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TipoIncidente id={self.id} {self.nombre!r}>"


class CategoriaIncidente(Base):
    __tablename__ = "categorias_incidente"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tipo_incidente_id: Mapped[int] = mapped_column(ForeignKey("tipos_incidente.id"), nullable=False)
    codigo_ruba: Mapped[str] = mapped_column(String(20), nullable=False)  # value real del <option> en el RUBA
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)

    tipo: Mapped["TipoIncidente"] = relationship(back_populates="categorias")

    __table_args__ = (
        UniqueConstraint("tipo_incidente_id", "codigo_ruba", name="uq_categoria_tipo_codigo"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CategoriaIncidente id={self.id} {self.nombre!r}>"


class Personal(Base):
    __tablename__ = "personal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # `legajo` = "N° de Legajo" (Fase 8): ya existía desde la Fase 1 con la
    # misma forma (String(20), único, nullable) que pedía la Fase 8 para
    # `numero_legajo` -- se reutiliza tal cual en vez de renombrar la
    # columna (evita un ALTER TABLE RENAME innecesario para un campo que ya
    # cumplía exactamente ese rol).
    legajo: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    apellido: Mapped[str] = mapped_column(String(80), nullable=False)
    dni: Mapped[str] = mapped_column(String(15), unique=True, nullable=False)
    telefono: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    jerarquia: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # -- Fase 8: legajo digital, firma electrónica y PIN personal -----------
    pin: Mapped[str] = mapped_column(String(10), default="5903", nullable=False)
    grupo_sanguineo: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    antiguedad_fecha: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    ruta_firma: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # "Activo" / "Licencia" / "Baja" -- más rico que el booleano `activo`
    # (no lo pedía explícito la lista de columnas de la Fase 8, pero la
    # ficha de legajo sí pide este estado de 3 valores). Se mantienen
    # sincronizados: Activo -> activo=True, Licencia/Baja -> activo=False,
    # así todo el código existente que filtra por `Personal.activo`
    # (selector de dotaciones, KPI "Cuerpo Activo", etc.) seguí funcionando
    # sin cambios.
    estado: Mapped[str] = mapped_column(String(20), default="Activo", nullable=False)

    # Fase 11: Id interno de RUBA (columna "Id" del Reporte de bomberos),
    # para cruzar con el padrón oficial. NULL para personal cargado a mano.
    id_ruba: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    def nombre_completo(self) -> str:
        return f"{self.apellido}, {self.nombre}"

    def legajo_display(self) -> str:
        """N° de Legajo para mostrar en la UI -- si todavía no se cargó
        ninguno, usa el id interno como fallback (Fase 8)."""
        return self.legajo or str(self.id)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Personal id={self.id} {self.nombre_completo()!r}>"


class Movil(Base):
    __tablename__ = "moviles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre_identificador: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Fase 11: value del <option> del vehículo en RUBA (`vehiculos_cuartel`
    # de config/ruba_mapping.json). NULL para móviles cargados a mano.
    id_ruba: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Movil id={self.id} {self.nombre_identificador!r}>"


# ---------------------------------------------------------------------------
# Incidente
# ---------------------------------------------------------------------------

class Incidente(Base):
    __tablename__ = "incidentes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    numero_parte: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)

    tipo_incidente_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tipos_incidente.id"), nullable=True)
    categoria_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categorias_incidente.id"), nullable=True)

    fecha: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Tiempos de Alarma (Fase 7: sección "1. Pedido de Socorro"): solo
    # llamado y sirena/toque son del incidente en sí. Salida/Arribo/Regreso
    # ahora son POR DOTACIÓN (cada unidad tiene su propio horario) -- ver
    # SalidaUnidad más abajo. hora_salida/hora_arribo/hora_regreso quedan
    # en desuso (nunca se escriben desde el formulario nuevo) pero no se
    # borran de la tabla para no perder los incidentes ya cargados antes
    # de la Fase 7.
    hora_llamado: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    hora_toque: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    hora_salida: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    hora_arribo: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    hora_regreso: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    # Fase 11: horario general del servicio (Participación en RUBA). La
    # "llegada" de RUBA es el regreso a base: su hora vive en `hora_regreso`.
    fecha_salida: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    fecha_llegada: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    calle_altura: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    localidad: Mapped[str] = mapped_column(String(80), nullable=False, default="Adelia María")
    zona: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # Zona.URBANA / Zona.RURAL
    # Opcional: coordenadas escritas o referencia rural ("campo de Pérez, 2 km al N de la ruta").
    referencia_ubicacion: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Cartografía táctica (Fase 5, ver app/ui/widgets/map_widget.py): punto del
    # incidente + polígono del área afectada. Todos opcionales -- un
    # incidente puede quedar sin marcar en el mapa (fallback manual, o
    # simplemente porque el operador no lo cargó).
    latitud: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitud: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    superficie_ha: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    geometria_geojson: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    denunciante_nombre: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    denunciante_apellido: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    denunciante_dni: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    denunciante_telefono: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # Fase 15 (planilla PCS): despacho y alerta.
    denunciante_domicilio: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    contacto_detalle: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)  # N°, frecuencia...
    recibio_personal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("personal.id"), nullable=True)
    alarma_general: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    autorizo_personal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("personal.id"), nullable=True)
    # "Personal" / "Radial" / "Teléfono" -- alimenta la marca de "Vía de
    # contacto" del PCS (D11/E11/F11, calibrada desde la Fase 2 pero sin
    # dato de origen hasta ahora).
    via_comunicacion: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    damnificados_heridos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    damnificados_muertos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    damnificados_desaparecidos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bomberos_lesionados: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    seguro_compania: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    seguro_poliza: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    resena_operativa: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Campos condicionales del panel "General" de RUBA según Tipo/Subtipo
    # (incendio forestal / estructural, accidente), como JSON:
    #   {"formulario": "incendio_forestal", "campos": {"tipo_lugar": "7", ...}}
    # Las claves de "campos" son las de `selectores.editar_general.condicionales.<formulario>`
    # en config/ruba_mapping.json, y los valores de los combos son los códigos
    # oficiales de RUBA -- ver app/ui/siniestro_widgets.py.
    datos_especificos_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Fase 13: "Emergencia en curso". Un servicio EN_CURSO no genera planillas
    # ni se sincroniza con RUBA hasta que se cierra.
    estado_operativo: Mapped[str] = mapped_column(
        String(12), default=EstadoOperativo.CERRADO.value, nullable=False
    )

    estado_ruba: Mapped[str] = mapped_column(String(20), default=EstadoRuba.PENDIENTE.value, nullable=False)
    ruba_id_remoto: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    ruba_error_log: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    tipo: Mapped[Optional["TipoIncidente"]] = relationship()
    categoria: Mapped[Optional["CategoriaIncidente"]] = relationship()
    recibio: Mapped[Optional["Personal"]] = relationship(foreign_keys=[recibio_personal_id])
    autorizo: Mapped[Optional["Personal"]] = relationship(foreign_keys=[autorizo_personal_id])
    dotacion: Mapped[List["DotacionSalida"]] = relationship(
        back_populates="incidente", cascade="all, delete-orphan"
    )
    salidas_unidad: Mapped[List["SalidaUnidad"]] = relationship(
        back_populates="incidente", cascade="all, delete-orphan", order_by="SalidaUnidad.id"
    )
    damnificados_civiles: Mapped[List["DamnificadoCivil"]] = relationship(
        back_populates="incidente", cascade="all, delete-orphan", order_by="DamnificadoCivil.id"
    )
    bienes_afectados: Mapped[List["BienAfectado"]] = relationship(
        back_populates="incidente", cascade="all, delete-orphan", order_by="BienAfectado.id"
    )
    bomberos_damnificados: Mapped[List["BomberoDamnificado"]] = relationship(
        back_populates="incidente", cascade="all, delete-orphan", order_by="BomberoDamnificado.id"
    )
    personal_base: Mapped[List["PersonalBase"]] = relationship(
        back_populates="incidente", cascade="all, delete-orphan", order_by="PersonalBase.orden"
    )

    @property
    def datos_especificos(self) -> Optional[Dict[str, Any]]:
        return json.loads(self.datos_especificos_json) if self.datos_especificos_json else None

    @property
    def en_curso(self) -> bool:
        return self.estado_operativo == EstadoOperativo.EN_CURSO.value

    def total_damnificados(self) -> int:
        return self.damnificados_heridos + self.damnificados_muertos

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Incidente id={self.id} numero_parte={self.numero_parte!r}>"


class CondicionDamnificado(str, enum.Enum):
    HERIDO = "Herido"
    FALLECIDO = "Fallecido"
    DESAPARECIDO = "Desaparecido"


class DamnificadoCivil(Base):
    """Un civil herido o fallecido individualizado (pantalla "Damnificados"
    de RUBA: nombre, apellido, DNI, género). `genero` guarda el código
    oficial de RUBA cuando el mapping lo define."""

    __tablename__ = "damnificados_civiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incidente_id: Mapped[int] = mapped_column(ForeignKey("incidentes.id"), nullable=False)
    condicion: Mapped[str] = mapped_column(String(20), nullable=False)  # CondicionDamnificado
    nombre: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    apellido: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    dni: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    genero: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Fase 12: "Herido leve" / "Herido grave" / "Fallecido" / "Desaparecido"
    # (la `condicion` para RUBA se deriva de acá).
    lesion: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    incidente: Mapped["Incidente"] = relationship(back_populates="damnificados_civiles")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DamnificadoCivil incidente={self.incidente_id} {self.condicion} {self.apellido!r}>"


class BienAfectado(Base):
    """Un bien afectado por el siniestro (inmueble, rodado, rastrojo...)."""

    __tablename__ = "bienes_afectados"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incidente_id: Mapped[int] = mapped_column(ForeignKey("incidentes.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)   # Inmueble | Rodado | Rastrojo | Otro
    descripcion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    titular: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    seguro: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)

    incidente: Mapped["Incidente"] = relationship(back_populates="bienes_afectados")


class BomberoDamnificado(Base):
    """Un bombero lesionado durante el servicio y la atención médica recibida."""

    __tablename__ = "bomberos_damnificados"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incidente_id: Mapped[int] = mapped_column(ForeignKey("incidentes.id"), nullable=False)
    personal_id: Mapped[int] = mapped_column(ForeignKey("personal.id"), nullable=False)
    detalle_atencion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    incidente: Mapped["Incidente"] = relationship(back_populates="bomberos_damnificados")
    personal: Mapped["Personal"] = relationship()


class Contacto(Base):
    """Guía telefónica operativa de la guardia (Fase 14)."""

    __tablename__ = "contactos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    categoria: Mapped[str] = mapped_column(String(40), nullable=False)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    entidad: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    rubro: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    localidad: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    telefono: Mapped[str] = mapped_column(String(40), nullable=False)
    telefono_alt: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Contacto {self.nombre!r} {self.telefono!r}>"


class FuncionBase(str, enum.Enum):
    OPERADOR_1 = "OPERADOR_1"
    OPERADOR_2 = "OPERADOR_2"
    APRESTO = "APRESTO"


class PersonalBase(Base):
    """Personal que quedó en el cuartel durante el servicio: operadores de
    guardia y reserva en apresto (para RUBA, tarea "Apresto")."""

    __tablename__ = "personal_base"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incidente_id: Mapped[int] = mapped_column(ForeignKey("incidentes.id"), nullable=False)
    personal_id: Mapped[int] = mapped_column(ForeignKey("personal.id"), nullable=False)
    funcion: Mapped[str] = mapped_column(String(12), nullable=False)  # FuncionBase
    orden: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    incidente: Mapped["Incidente"] = relationship(back_populates="personal_base")
    personal: Mapped["Personal"] = relationship()


class SalidaUnidad(Base):
    """Una unidad (Móvil) despachada para un Incidente, con sus propios
    horarios (Fase 7: "2. Dotaciones y Unidades" -- cada dotación tiene su
    salida/arribo/regreso independiente, a diferencia de los campos
    heredados a nivel Incidente que ahora quedan en desuso). Puede haber
    varias por incidente (Dotación 1, Dotación 2, ...)."""

    __tablename__ = "salidas_unidad"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incidente_id: Mapped[int] = mapped_column(ForeignKey("incidentes.id"), nullable=False)
    movil_id: Mapped[int] = mapped_column(ForeignKey("moviles.id"), nullable=False)

    hora_salida: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    hora_arribo: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    hora_regreso: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    # Fase 11 (Vehículos intervinientes de RUBA): fechas propias del móvil y
    # su chofer. `hora_regreso` guarda la hora de "llegada" de RUBA.
    fecha_salida: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    fecha_llegada: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    chofer_personal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("personal.id"), nullable=True)
    # Fase 15: grado/cargo del Jefe de Dotación al momento del servicio
    # (foto histórica para la PCD2) y cuándo validó su firma con el PIN.
    jefe_grado: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    firma_validada_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Firma dibujada a mano en el pad táctil (Fase 7, PNG en base64) --
    # reemplazada en la Fase 8 por la firma electrónica autorizada con PIN
    # (ver `ruta_firma_auditoria`). Se deja la columna por compatibilidad
    # con dotaciones ya guardadas antes de la Fase 8; el formulario nuevo
    # ya no la completa.
    firma_jefe_dotacion: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Fase 8: firma electrónica del Jefe de Dotación, autorizada con su PIN
    # personal al momento de cerrar la salida. `ruta_firma_auditoria` es la
    # copia de auditoría de esa firma en
    # data/firmas/{AÑO}/{NUMERO_PARTE_LIMPIO}_DOTACION_{N}_JEFE.png -- su
    # sola presencia (no NULL) indica que esta dotación quedó firmada.
    ruta_firma_auditoria: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    incidente: Mapped["Incidente"] = relationship(back_populates="salidas_unidad")
    movil: Mapped["Movil"] = relationship()
    chofer: Mapped[Optional["Personal"]] = relationship(foreign_keys=[chofer_personal_id])
    dotacion: Mapped[List["DotacionSalida"]] = relationship(
        back_populates="salida_unidad", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SalidaUnidad id={self.id} incidente_id={self.incidente_id} movil_id={self.movil_id}>"


class DotacionSalida(Base):
    """Quién (Personal) salió en qué Móvil, con qué rol, dentro de una
    SalidaUnidad (dotación) concreta de un Incidente."""

    __tablename__ = "dotacion_salida"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incidente_id: Mapped[int] = mapped_column(ForeignKey("incidentes.id"), nullable=False)
    movil_id: Mapped[int] = mapped_column(ForeignKey("moviles.id"), nullable=False)
    personal_id: Mapped[int] = mapped_column(ForeignKey("personal.id"), nullable=False)
    rol: Mapped[str] = mapped_column(String(20), nullable=False)  # RolDotacion.CHOFER / A_CARGO / BOMBERO
    # Nullable por compatibilidad con dotaciones cargadas antes de la Fase 7
    # (una sola unidad por incidente, sin este agrupador); todo lo cargado
    # desde el formulario nuevo siempre lo completa.
    salida_unidad_id: Mapped[Optional[int]] = mapped_column(ForeignKey("salidas_unidad.id"), nullable=True)
    # Fase 11: "Tipo de Tarea" de RUBA (código de
    # `intervencion_bomberos.tipo_tarea_opciones`: '1' Interviniente, '2' Apresto).
    tipo_tarea: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)

    incidente: Mapped["Incidente"] = relationship(back_populates="dotacion")
    movil: Mapped["Movil"] = relationship()
    personal: Mapped["Personal"] = relationship()
    salida_unidad: Mapped[Optional["SalidaUnidad"]] = relationship(back_populates="dotacion")

    __table_args__ = (
        UniqueConstraint("incidente_id", "movil_id", "personal_id", "rol", name="uq_dotacion_unica"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DotacionSalida incidente={self.incidente_id} personal={self.personal_id} rol={self.rol}>"


TIPOS_DOCUMENTO: list[str] = [
    "DNI",
    "Certificado de 1° Nivel",
    "Certificado de 2° Nivel",
    "Certificado de 3° Nivel",
    "Certificado de 4° Nivel",
    "Libreta Operativa",
    "Certificado de Curso Especial",
    "Sumario Administrativo",
    "Solicitud de Ingreso",
    "Otro Documento",
]

# Etiquetas usadas antes de la unificación -> etiqueta formal actual.
_TIPOS_DOCUMENTO_LEGADOS = {
    "CERTIFICADO DE PRIMER NIVEL": "Certificado de 1° Nivel",
    "CERTIFICADO DE SEGUNDO NIVEL": "Certificado de 2° Nivel",
    "CERTIFICADO DE TERCER NIVEL": "Certificado de 3° Nivel",
    "CERTIFICADO DE CUARTO NIVEL": "Certificado de 4° Nivel",
    "LIBRETA": "Libreta Operativa",
    "SUMARIO": "Sumario Administrativo",
    "OTRO": "Otro Documento",
}


def normalizar_tipo_documento(tipo: str) -> str:
    """Devuelve la etiqueta formal de un tipo de documento, tolerando valores
    guardados con el formato anterior (mayúsculas, nombres abreviados)."""
    texto = (tipo or "").strip()
    clave = texto.upper()
    for canonico in TIPOS_DOCUMENTO:
        if canonico.upper() == clave:
            return canonico
    return _TIPOS_DOCUMENTO_LEGADOS.get(clave, texto)


class DocumentoPersonal(Base):
    """Un documento adjunto al legajo de un bombero (Fase 8: DNI, carnets de
    curso, libreta, etc.) -- el archivo en sí vive en
    data/legajos/{numero_legajo}/, esta tabla solo indexa sus metadatos."""

    __tablename__ = "documentos_personal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    personal_id: Mapped[int] = mapped_column(ForeignKey("personal.id"), nullable=False)
    tipo_documento: Mapped[str] = mapped_column(String(60), nullable=False)
    nombre_archivo: Mapped[str] = mapped_column(String(255), nullable=False)
    ruta_archivo: Mapped[str] = mapped_column(String(500), nullable=False)
    fecha_subida: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)

    personal: Mapped["Personal"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DocumentoPersonal id={self.id} personal_id={self.personal_id} tipo={self.tipo_documento!r}>"
