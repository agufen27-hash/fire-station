"""
Motor de base de datos y siembra de catálogos para Fire Station.

`init_db()` crea las tablas si no existen y precarga:
  - Tipos y Categorías de incidente: una categoría base por Tipo, y en cada
    arranque se completan los subtipos que falten con el catálogo oficial
    de `config/ruba_mapping.json` (ver _sincronizar_categorias_desde_mapping).
  - Personal y Móviles del cuartel. El personal de fábrica sale de
    `data/seed_bomberos.json` (fuera del repo: tiene nombres y DNI reales);
    sin ese archivo se cargan bomberos genéricos de ejemplo
    (ver app/core/semilla.py).

La siembra es idempotente: cada catálogo se carga solo si la tabla
correspondiente está vacía, así correr `init_db()` de nuevo no duplica nada.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CategoriaIncidente, Contacto, Incidente, Movil, Personal, TipoIncidente
from app.paths import get_writable_dir

# data/ es escribible (la base y config.json con credenciales reales de RUBA
# viven ahí): en modo empaquetado se resuelve al lado del .exe, nunca dentro
# de la carpeta temporal de solo lectura de PyInstaller. Ver app/paths.py.
DATA_DIR = get_writable_dir("data")
DB_PATH = DATA_DIR / "fire_station.db"

# check_same_thread=False: la sincronización con RUBA (Fase 3) corre en un
# QThread de fondo y necesita poder abrir su propia sesión ahí -- cada
# get_session() sigue creando una Session propia (nunca se comparte una
# misma Session/conexión entre hilos), esto solo le saca a sqlite3 la
# restricción de "un connection object solo se usa desde su hilo de origen".
engine = create_engine(
    f"sqlite:///{DB_PATH}", echo=False, future=True,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def get_session() -> Iterator[Session]:
    """Sesión con commit/rollback automático:
        with get_session() as session:
            session.add(...)
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Catálogos base
# ---------------------------------------------------------------------------

TIPOS_INCIDENTE: dict[int, str] = {
    1: "Accidentes",
    2: "Factores Climáticos",
    3: "Incendios",
    4: "Materiales Peligrosos",
    5: "Rescates",
    6: "Servicios Especiales",
}

# tipo_id -> [(codigo_ruba, nombre), ...]. Ver docstring del módulo.
CATEGORIAS_RUBA: dict[int, list[tuple[str, str]]] = {
    1: [("3", "Tránsito")],
    2: [("9", "Tormentas")],
    3: [("23", "Vivienda")],
    4: [("25", "Escape o Fuga")],
    5: [("28", "Personas")],
    6: [("32", "Servicios Especiales")],
}

def _sembrar_tipos_y_categorias(session: Session) -> None:
    for tipo_id, nombre in TIPOS_INCIDENTE.items():
        session.add(TipoIncidente(id=tipo_id, nombre=nombre))
    session.flush()
    for tipo_id, categorias in CATEGORIAS_RUBA.items():
        for codigo_ruba, nombre_cat in categorias:
            session.add(
                CategoriaIncidente(tipo_incidente_id=tipo_id, codigo_ruba=codigo_ruba, nombre=nombre_cat)
            )


def _sincronizar_categorias_desde_mapping(session: Session) -> None:
    """Completa tipos y categorías (subtipos) con el catálogo oficial de
    `config/ruba_mapping.json`. Idempotente: solo agrega lo que falta
    (por tipo + código RUBA), nunca borra ni renombra -- los incidentes ya
    guardados siguen apuntando a sus categorías."""
    from app.core.catalogos import cargar_tipos_incidente  # import diferido: evita ciclo en el arranque

    try:
        tipos = cargar_tipos_incidente()
    except (OSError, ValueError) as e:  # json.JSONDecodeError es ValueError
        logging.getLogger(__name__).warning("No se pudo leer el catálogo de ruba_mapping.json: %s", e)
        return

    session.flush()  # la sesión no hace autoflush: sin esto no ve lo recién sembrado y lo duplica
    existentes = {(c.tipo_incidente_id, c.codigo_ruba) for c in session.query(CategoriaIncidente).all()}
    for tipo in tipos:
        if session.get(TipoIncidente, tipo.id_ruba) is None:
            session.add(TipoIncidente(id=tipo.id_ruba, nombre=tipo.nombre))
            session.flush()
        for codigo, nombre in tipo.subtipos.items():
            if (tipo.id_ruba, str(codigo)) not in existentes:
                session.add(CategoriaIncidente(tipo_incidente_id=tipo.id_ruba, codigo_ruba=str(codigo), nombre=nombre))


def cargar_moviles_desde_mapping(session: Session) -> None:
    """Da de alta (o vincula por nombre) los móviles de `vehiculos_cuartel`
    (config/ruba_mapping.json) con su `id_ruba`. Idempotente.

    YA NO se llama en init_db(): recrearlos en cada arranque hacía imposible
    eliminar una unidad. Queda solo como carga explícita (p. ej. las pruebas
    automáticas, que arman una base vacía)."""
    from app.core.catalogos import cargar_moviles  # import diferido: evita ciclo en el arranque

    try:
        moviles = cargar_moviles()
    except (OSError, ValueError) as e:
        logging.getLogger(__name__).warning("No se pudieron leer los móviles de ruba_mapping.json: %s", e)
        return

    session.flush()
    for movil in moviles:
        if session.query(Movil).filter(Movil.id_ruba == movil.id_ruba).first() is not None:
            continue
        nombre = f"Móvil {movil.numero}" if movil.numero.isdigit() else movil.numero
        homonimo = session.query(Movil).filter(Movil.nombre_identificador == nombre).first()
        if homonimo is not None and homonimo.id_ruba is None:
            homonimo.id_ruba = movil.id_ruba
            continue
        if homonimo is not None:
            nombre = f"{nombre} ({movil.id_ruba})"
        session.add(Movil(nombre_identificador=nombre, activo=True, id_ruba=movil.id_ruba))


def _sincronizar_personal_desde_padron(session: Session, padron=None) -> int:
    """Vincula/da de alta al personal activo del padrón oficial
    (data/Reporte de bomberos.xlsx, o el `padron` ya leído que se pase).
    Se cruza por Id de RUBA y, si no, por DNI -- así el personal ya cargado
    conserva su PIN y su firma. Si el Excel no está, no hace nada (la app
    sigue funcionando con lo que ya hay en la base). Devuelve cuántas
    personas se dieron de alta."""
    from app.core.catalogos import leer_padron_personal

    if padron is None:
        try:
            padron = leer_padron_personal()
        except FileNotFoundError:
            logging.getLogger(__name__).info(
                "Padrón de bomberos no cargado todavía (se importa desde Personal y Unidades).")
            return 0
        except (OSError, ValueError) as e:
            logging.getLogger(__name__).warning("No se pudo leer el padrón de personal: %s", e)
            return 0

    altas = 0
    session.flush()
    legajos_usados = {p.legajo for p in session.query(Personal).filter(Personal.legajo.isnot(None)).all()}
    for bombero in padron:
        persona = (
            session.query(Personal).filter(Personal.id_ruba == bombero.id_ruba).first()
            or session.query(Personal).filter(Personal.dni == bombero.dni).first()
        )
        if persona is None:
            legajo = bombero.legajo if bombero.legajo and bombero.legajo not in legajos_usados else None
            session.add(Personal(
                nombre=bombero.nombre, apellido=bombero.apellido, dni=bombero.dni, legajo=legajo,
                jerarquia=bombero.clasificacion, activo=True, estado="Activo", id_ruba=bombero.id_ruba,
            ))
            altas += 1
            if legajo:
                legajos_usados.add(legajo)
        elif persona.id_ruba is None:
            persona.id_ruba = bombero.id_ruba
    return altas


# ---------------------------------------------------------------------------
# Unidades: uso en partes, baja lógica y eliminación
# ---------------------------------------------------------------------------

def usos_movil(session: Session, movil_id: int) -> int:
    """Cuántos registros de partes referencian la unidad (salidas y
    dotaciones). Con alguno, borrarla rompería esos partes."""
    from app.models import DotacionSalida, SalidaUnidad

    return (session.query(SalidaUnidad).filter(SalidaUnidad.movil_id == movil_id).count()
            + session.query(DotacionSalida).filter(DotacionSalida.movil_id == movil_id).count())


def dar_de_baja_movil(movil_id: int) -> bool:
    """Baja lógica: queda en la base (los partes históricos la siguen
    mostrando) pero deja de ofrecerse para nuevas salidas."""
    from app.services.ruba_importer import ESTADO_MOVIL_BAJA

    with get_session() as session:
        movil = session.get(Movil, movil_id)
        if movil is None:
            return False
        movil.activo = False
        movil.estado = ESTADO_MOVIL_BAJA
        return True


def eliminar_movil(movil_id: int) -> int:
    """DELETE físico, solo si ningún parte usa la unidad. Devuelve 0 si se
    borró (o ya no existía) y, si no, la cantidad de registros que la usan
    (no se toca nada). Como init_db() ya no siembra móviles, no reaparece."""
    with get_session() as session:
        movil = session.get(Movil, movil_id)
        if movil is None:
            return 0
        usos = usos_movil(session, movil_id)
        if usos:
            return usos
        session.delete(movil)
        return 0


def moviles_para_despacho(incluir_id_ruba: tuple = ()):
    """Unidades que se ofrecen en las dotaciones del parte: las de la base
    que están activas y tienen Id de RUBA (sin él no se pueden cargar en
    RUBA), más las de `incluir_id_ruba` aunque estén de baja (para poder
    reabrir un parte viejo). Lista de catalogos.Movil, como antes."""
    from app.core.catalogos import Movil as MovilRuba

    with get_session() as session:
        consulta = session.query(Movil).filter(Movil.id_ruba.isnot(None))
        moviles = [m for m in consulta if m.activo or m.id_ruba in set(incluir_id_ruba)]
        resultado = [
            MovilRuba(
                id_ruba=m.id_ruba,
                numero=(m.numero_movil or m.nombre_identificador) + ("" if m.activo else " (fuera de servicio)"),
                marca=m.marca, modelo=m.modelo, descripcion=m.nombre_identificador,
            )
            for m in moviles
        ]
    return sorted(resultado, key=lambda m: (m.numero.endswith("(fuera de servicio)"), m.numero))


def importar_vehiculos_excel(ruta: Path):
    """Upsert de `moviles` desde el 'Reporte de vehiculos' de RUBA elegido
    por el usuario (clave: 'Nº Móvil'), en una transacción. Devuelve un
    ruba_importer.ResumenImportacion. Ver app/services/ruba_importer.py."""
    from app.services.ruba_importer import importar_vehiculos_desde_excel

    return importar_vehiculos_desde_excel(Path(ruta))


def importar_bomberos_excel(ruta: Path):
    """Upsert de `personal` desde el 'Reporte de bomberos' de RUBA (clave:
    Id de RUBA, luego DNI) + copia local del padrón. Devuelve
    (ResumenImportacion, activos_en_padron | None)."""
    from app.services.ruba_importer import importar_bomberos_desde_excel

    return importar_bomberos_desde_excel(Path(ruta))


# Guía telefónica: SOLO números oficiales de emergencia, de alcance nacional o
# provincial. Los contactos locales (cuarteles vecinos, hospital, cooperativa,
# municipio, productores) los carga la guardia desde la app: un número
# inventado en esta guía es peor que ninguno.
CONTACTOS_INICIALES: list[dict] = [
    {"categoria": "Servicios de Emergencia", "nombre": "Emergencias 911", "entidad": "Policía de Córdoba",
     "rubro": "Policía / emergencias", "localidad": "Provincia de Córdoba", "telefono": "911"},
    {"categoria": "Servicios de Emergencia", "nombre": "Bomberos", "entidad": "Línea nacional",
     "rubro": "Bomberos", "localidad": "Todo el país", "telefono": "100"},
    {"categoria": "Servicios de Emergencia", "nombre": "Emergencias médicas", "entidad": "Línea nacional",
     "rubro": "Ambulancia / salud", "localidad": "Todo el país", "telefono": "107"},
    {"categoria": "Defensa Civil", "nombre": "Defensa Civil", "entidad": "Línea nacional",
     "rubro": "Defensa Civil", "localidad": "Todo el país", "telefono": "103"},
]


def _sembrar_contactos(session: Session) -> None:
    for datos in CONTACTOS_INICIALES:
        session.add(Contacto(**datos))


def _sembrar_personal(session: Session) -> None:
    from app.core.semilla import personal_inicial

    for datos in personal_inicial():
        session.add(Personal(**datos, jerarquia="Bombero", activo=True))


# Columnas de cartografía táctica agregadas en Fase 5 (ver
# app/ui/widgets/map_widget.py): `Base.metadata.create_all()` solo crea tablas que
# todavía no existen, nunca agrega columnas nuevas a una tabla existente --
# en una base de un instalación previa a la Fase 5, `incidentes` ya existe
# sin estas columnas, así que hace falta un ALTER TABLE explícito. SQLite
# soporta `ADD COLUMN` de forma segura (no toca las filas existentes, los
# valores nuevos quedan NULL) siempre que la columna sea nullable, como es
# el caso acá.
COLUMNAS_NUEVAS_INCIDENTES = {
    "ruba_sincronizado_en": "DATETIME",  # carga en lote a RUBA desde el Historial
    "latitud": "REAL",
    "longitud": "REAL",
    "superficie_ha": "REAL",
    "geometria_geojson": "TEXT",
    "via_comunicacion": "VARCHAR(20)",
    # Fase 10: datos específicos por tipo de siniestro y civiles desaparecidos.
    "datos_especificos_json": "TEXT",
    "damnificados_desaparecidos": "INTEGER NOT NULL DEFAULT 0",
    # Fase 11: horario general del servicio.
    "fecha_salida": "DATE",
    "fecha_llegada": "DATE",
    # Fase 13: ciclo de vida del servicio (lo ya cargado quedó cerrado).
    "estado_operativo": "VARCHAR(12) NOT NULL DEFAULT 'CERRADO'",
    # Fase 15: despacho y alerta (planilla PCS).
    "denunciante_domicilio": "VARCHAR(200)",
    "referencia_ubicacion": "VARCHAR(200)",
    "contacto_detalle": "VARCHAR(80)",
    "recibio_personal_id": "INTEGER REFERENCES personal(id)",
    "alarma_general": "BOOLEAN",
    "autorizo_personal_id": "INTEGER REFERENCES personal(id)",
}

# Fase 7: "Dotaciones y Unidades" dinámicas -- dotacion_salida ahora se
# agrupa por SalidaUnidad (ver app/models.py). La tabla salidas_unidad es
# nueva, así que create_all() ya la crea completa; dotacion_salida en
# cambio ya existía desde la Fase 1, así que necesita el ALTER TABLE.
COLUMNAS_NUEVAS_DOTACION_SALIDA = {
    "salida_unidad_id": "INTEGER",
    "tipo_tarea": "VARCHAR(5)",
}

# Fase 8: legajo digital, firma electrónica y PIN personal. `pin` y `estado`
# son NOT NULL en el modelo -> el ALTER TABLE necesita un DEFAULT explícito
# (SQLite lo exige para agregar una columna NOT NULL a una tabla que ya
# tiene filas) para que las filas de `personal` cargadas antes de la Fase 8
# queden con un valor válido en vez de romper la migración.
COLUMNAS_NUEVAS_PERSONAL = {
    "pin": "VARCHAR(10) NOT NULL DEFAULT '5903'",
    "grupo_sanguineo": "VARCHAR(10)",
    "antiguedad_fecha": "DATE",
    "ruta_firma": "VARCHAR(255)",
    "estado": "VARCHAR(20) NOT NULL DEFAULT 'Activo'",
    "id_ruba": "INTEGER",
}

COLUMNAS_NUEVAS_MOVILES = {
    "id_ruba": "INTEGER",
    # Importador del 'Reporte de vehiculos' de RUBA (app/services/ruba_importer.py).
    "numero_movil": "VARCHAR(30)",
    "tipo": "VARCHAR(80)",
    "marca": "VARCHAR(60)",
    "modelo": "VARCHAR(80)",
    "anio": "INTEGER",
    "estado": "VARCHAR(20)",
}

# Fase 12: lesión de cada civil damnificado.
COLUMNAS_NUEVAS_DAMNIFICADOS_CIVILES = {
    "lesion": "VARCHAR(30)",
}

# Firma electrónica de la dotación (copia de auditoría). salidas_unidad ya
# existía desde la Fase 7.
COLUMNAS_NUEVAS_SALIDA_UNIDAD = {
    "ruta_firma_auditoria": "VARCHAR(500)",
    "fecha_salida": "DATE",
    "fecha_llegada": "DATE",
    "chofer_personal_id": "INTEGER REFERENCES personal(id)",
    "jefe_grado": "VARCHAR(120)",
    "firma_validada_en": "DATETIME",
}


def _migrar_columnas(nombre_tabla: str, columnas_nuevas: dict[str, str]) -> None:
    inspector = inspect(engine)
    if nombre_tabla not in inspector.get_table_names():
        return  # tabla recién creada por create_all(), ya tiene todas las columnas del modelo actual

    columnas_existentes = {col["name"] for col in inspector.get_columns(nombre_tabla)}
    faltantes = {
        nombre: tipo_sql for nombre, tipo_sql in columnas_nuevas.items()
        if nombre not in columnas_existentes
    }
    if not faltantes:
        return

    with engine.begin() as conexion:
        for nombre, tipo_sql in faltantes.items():
            conexion.execute(text(f"ALTER TABLE {nombre_tabla} ADD COLUMN {nombre} {tipo_sql}"))


def init_db() -> None:
    Base.metadata.create_all(engine)  # crea salidas_unidad, documentos_personal, damnificados_civiles si faltan
    _migrar_columnas("incidentes", COLUMNAS_NUEVAS_INCIDENTES)
    _migrar_columnas("dotacion_salida", COLUMNAS_NUEVAS_DOTACION_SALIDA)
    _migrar_columnas("personal", COLUMNAS_NUEVAS_PERSONAL)
    _migrar_columnas("salidas_unidad", COLUMNAS_NUEVAS_SALIDA_UNIDAD)
    _migrar_columnas("moviles", COLUMNAS_NUEVAS_MOVILES)
    _migrar_columnas("damnificados_civiles", COLUMNAS_NUEVAS_DAMNIFICADOS_CIVILES)
    with get_session() as session:
        if session.query(TipoIncidente).count() == 0:
            _sembrar_tipos_y_categorias(session)
        _sincronizar_categorias_desde_mapping(session)
        if session.query(Personal).count() == 0:
            _sembrar_personal(session)
        # Móviles: NO se siembran ni se sincronizan desde ruba_mapping.json.
        # El parque se administra solo desde la base (alta a mano o
        # importación del 'Reporte de vehiculos'), así una unidad eliminada
        # no reaparece al reiniciar.
        if session.query(Contacto).count() == 0:
            _sembrar_contactos(session)
        _sincronizar_personal_desde_padron(session)


# ---------------------------------------------------------------------------
# Numeración automática de N° de Parte
# ---------------------------------------------------------------------------

def siguiente_numero_parte(session: Session, anio: Optional[int] = None) -> str:
    """Sugiere el próximo N° de parte con formato 'NNN/AAAA', reiniciando
    el correlativo cada año calendario (mismo criterio que core/historial_store.py)."""
    anio = anio or date.today().year
    sufijo = f"/{anio}"

    max_correlativo = 0
    numeros = session.query(Incidente.numero_parte).filter(Incidente.numero_parte.like(f"%{sufijo}")).all()
    for (numero_parte,) in numeros:
        numero, _, anio_str = numero_parte.partition("/")
        if anio_str.strip() == str(anio) and numero.strip().isdigit():
            max_correlativo = max(max_correlativo, int(numero.strip()))

    return f"{max_correlativo + 1:03d}/{anio}"
