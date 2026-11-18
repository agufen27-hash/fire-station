"""Importación del 'Reporte de bomberos' y del 'Reporte de vehiculos' de RUBA
(app/services/ruba_importer.py) con planillas sintéticas que copian los
encabezados del export real: legajo, apellido y nombre, DNI, jerarquía
(desde 'Cargo' / 'Formación'), teléfono y contacto de emergencia
('Familiares')."""

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.models import Movil, Personal
from app.services import ruba_importer as ri
from app.services.personal_info import grado_y_cargo

pytestmark = pytest.mark.rapido

# Mismo orden de columnas que el export real de RUBA (A..AC).
ENCABEZADO_BOMBEROS = [
    "Id", "Activo", "Apellido", "Nombre", "DNI", "Fecha de nacimiento", "Legajo", "Altura", "Peso", "Sexo",
    "Provincia", "Localidad", "Domicilio", "Email", "Telefono", "Grupo Sanguineo", "Factor", "Hijos",
    "Organización", "Federación", "Cuerpo", "Fecha Ingreso", "Formación", "Cargo", "Nivel Educativo",
    "Título obtenido", "Estado", "Fecha de Estado", "Familiares",
]


def _fila(**datos):
    fila = [None] * len(ENCABEZADO_BOMBEROS)
    for clave, valor in datos.items():
        fila[ENCABEZADO_BOMBEROS.index(clave)] = valor
    return fila


@pytest.fixture
def reporte_bomberos(tmp_path):
    wb = Workbook()
    hoja = wb.active
    hoja.append(ENCABEZADO_BOMBEROS)
    hoja.append(_fila(Id=901, Activo="Si", Apellido="Pruebaz", Nombre="Ana", DNI="30.111.222", Legajo="L-17",
                      Telefono="358 4111222", Formación="Bombero", Cargo="Sargento", Estado="Activo",
                      Familiares="PRUEBAZ,JUAN CARLOS | DNI:20333444"))
    hoja.append(_fila(Id=902, Activo="Si", Apellido="Ejemplo", Nombre="Beto", DNI=31222333, Legajo=None,
                      Telefono=None, Formación="Aspirante", Cargo=None, Estado="Activo", Familiares=None))
    ruta = tmp_path / "Reporte de bomberos.xlsx"
    wb.save(ruta)
    return ruta


def test_lee_los_campos_clave_del_legajo(reporte_bomberos):
    lectura = ri.leer_reporte_bomberos(reporte_bomberos)
    ana, beto = lectura.bomberos
    assert (ana.legajo, ana.apellido, ana.nombre, ana.dni) == ("L-17", "PRUEBAZ", "Ana", "30111222")
    assert ana.jerarquia == "Sargento" and ana.jerarquia_es_oficial  # grado de 'Cargo'
    assert ana.telefono == "358 4111222"
    assert ana.contacto_emergencia == "PRUEBAZ, Juan Carlos (DNI 20333444)"
    assert beto.jerarquia == "Aspirante" and not beto.jerarquia_es_oficial  # sin cargo: 'Formación'
    assert beto.contacto_emergencia is None and beto.legajo is None
    assert not any("Jerarquía" in a for a in lectura.avisos)


@pytest.mark.parametrize("crudo, esperado", [
    ("PEREZ,JUAN | DNI:12345678", "PEREZ, Juan (DNI 12345678)"),
    ("PEREZ,JUAN | DNI:12.345.678; GOMEZ,ANA | DNI:23456789",
     "PEREZ, Juan (DNI 12345678) · GOMEZ, Ana (DNI 23456789)"),
    ("Madre: 358-4000000", "Madre: 358-4000000"),
    ("", None), (None, None),
])
def test_contacto_desde_familiares(crudo, esperado):
    assert ri.contacto_desde_familiares(crudo) == esperado


def test_grado_y_cargo_no_repite():
    assert grado_y_cargo("Sargento", "Sargento") == "Sargento"
    assert grado_y_cargo("Sargento", "Jefe de Cuerpo") == "Sargento — Jefe de Cuerpo"


@pytest.fixture
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()
    yield
    engine.dispose()


def test_upsert_guarda_los_campos_y_no_borra_lo_cargado_a_mano(base, reporte_bomberos):
    lectura = ri.leer_reporte_bomberos(reporte_bomberos)
    with db.get_session() as s:
        ri.upsert_bomberos(s, lectura.bomberos)
    with db.get_session() as s:
        ana = s.query(Personal).filter_by(id_ruba=901).one()
        assert (ana.legajo, ana.dni, ana.jerarquia, ana.telefono) == ("L-17", "30111222", "Sargento", "358 4111222")
        assert ana.contacto_emergencia == "PRUEBAZ, Juan Carlos (DNI 20333444)"
        beto = s.query(Personal).filter_by(id_ruba=902).one()
        beto.contacto_emergencia = "Hermana 358-4999999"   # cargado a mano
        beto.telefono = "358 4888888"
    with db.get_session() as s:
        ri.upsert_bomberos(s, lectura.bomberos)            # RUBA trae esos datos vacíos
    with db.get_session() as s:
        beto = s.query(Personal).filter_by(id_ruba=902).one()
        assert (beto.contacto_emergencia, beto.telefono) == ("Hermana 358-4999999", "358 4888888")


def test_importa_vehiculos(base, tmp_path):
    wb = Workbook()
    hoja = wb.active
    hoja.append(["Nº Móvil", "Marca", "Modelo", "Tipo", "Año", "Servicio", "Baja", "Dominio"])
    hoja.append(["Rojo  99", "Iveco", "Eurocargo", "Autobomba", 2015, "Si", "No", "ab 123 cd"])
    ruta = tmp_path / "Reporte de vehiculos.xlsx"
    wb.save(ruta)
    resumen = ri.importar_vehiculos_desde_excel(ruta)
    assert resumen.nuevas == 1
    with db.get_session() as s:
        m = s.query(Movil).filter_by(numero_movil="Rojo 99").one()
        assert (m.marca, m.modelo, m.tipo, m.anio, m.dominio, m.activo) == (
            "Iveco", "Eurocargo", "Autobomba", 2015, "AB123CD", True)


def test_instalacion_limpia_arranca_sin_personal_ni_unidades(tmp_path, monkeypatch):
    """Primer arranque sin semilla del cuartel ni padrón: base con las tablas
    y el catálogo oficial de tipos, pero SIN personal ni unidades de ejemplo."""
    from app.core import catalogos, semilla
    from app.models import Contacto, TipoIncidente

    engine = create_engine(f"sqlite:///{tmp_path / 'nueva.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(semilla, "ruta_semilla", lambda: tmp_path / "no_existe_seed.json")
    monkeypatch.setattr(catalogos, "ruta_padron_por_defecto", lambda: tmp_path / "no_existe_padron.xlsx")
    # conftest envuelve init_db para sumar móviles de prueba: acá se usa el original.
    init_original = next(c.cell_contents for c in db.init_db.__closure__
                         if callable(c.cell_contents) and c.cell_contents.__name__ == "init_db")
    init_original()
    init_original()  # segundo arranque: sigue vacío
    with db.get_session() as s:
        assert s.query(Personal).count() == 0
        assert s.query(Movil).count() == 0
        assert s.query(TipoIncidente).count() > 0      # catálogo oficial de RUBA (necesario para cargar partes)
        assert s.query(Contacto).count() == 4          # solo 911 / 100 / 107 / 103
    assert semilla.personal_inicial() == []
    engine.dispose()
