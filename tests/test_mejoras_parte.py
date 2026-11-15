"""Pruebas rápidas (sin MainWindow) de: N° de parte editable, fecha combinada
en la PCS y reinicio de partes. Corren en segundos."""

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db
from app.models import Incidente
from app.reports import excel_generator as eg
from app.services.ruba_payload import numero_parte_ruba

pytestmark = pytest.mark.rapido


# -- N° de parte editable: lo que valida el formulario -------------------------

@pytest.mark.parametrize("numero, esperado", [
    ("004/2026", "0042026"), (" 12/2026 ", "122026"), ("abc", ""), ("", ""),
])
def test_numero_parte_editado_se_valida_por_digitos(numero, esperado):
    assert numero_parte_ruba(numero) == esperado


# -- PCS: fecha combinada --------------------------------------------------------

def _rangos(hoja):
    return {r.coord for r in hoja.merged_cells.ranges}


def test_plantilla_pcs_trae_la_fecha_combinada():
    hoja = load_workbook(eg._ubicar_plantilla(eg.PCS_TEMPLATE_NOMBRE)).active
    assert eg.MAPEO_PCS["rango_fecha"] in _rangos(hoja)
    assert eg.MAPEO_PCS["fecha"] == eg.MAPEO_PCS["rango_fecha"].split(":")[0]


def test_combina_la_fecha_si_la_plantilla_no_la_trae():
    hoja = Workbook().active
    eg._asegurar_combinada(hoja, "T6:Y6")
    assert _rangos(hoja) == {"T6:Y6"}
    assert hoja["T6"].alignment.horizontal == "center"


def test_reemplaza_una_combinacion_vieja_superpuesta():
    hoja = Workbook().active
    hoja.merge_cells("T6:U6")
    hoja.merge_cells("A1:B1")
    eg._asegurar_combinada(hoja, "T6:Y6")
    assert _rangos(hoja) == {"T6:Y6", "A1:B1"}


def test_no_toca_la_combinacion_si_ya_esta():
    hoja = Workbook().active
    hoja.merge_cells("T6:Y6")
    eg._asegurar_combinada(hoja, "T6:Y6")
    assert _rangos(hoja) == {"T6:Y6"}


# -- Reinicio de partes ------------------------------------------------------------

@pytest.fixture
def base_temporal(tmp_path, monkeypatch):
    ruta = tmp_path / "fire_station.db"
    engine = create_engine(f"sqlite:///{ruta}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(db, "DB_PATH", ruta)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    db.init_db()
    yield tmp_path
    engine.dispose()


def test_reiniciar_partes_vacia_la_base_y_deja_respaldo(base_temporal):
    with db.get_session() as session:
        session.add_all([Incidente(numero_parte="001/2026"), Incidente(numero_parte="002/2026")])

    borrados, respaldo = db.reiniciar_partes()

    assert borrados == 2
    assert respaldo.exists() and respaldo.parent == base_temporal / "respaldos"
    with db.get_session() as session:
        assert session.query(Incidente).count() == 0
        assert db.siguiente_numero_parte(session, 2026) == "001/2026"


CUENTA_RUBA = {"usuario": "Cuartel.Adelia", "clave": "S3creta!"}


def test_reinicio_requiere_cuenta_de_ruba_cargada():
    from app.services.personal_service import autorizacion_roles_usa_ruba

    assert autorizacion_roles_usa_ruba(CUENTA_RUBA) is True
    assert autorizacion_roles_usa_ruba({"usuario": "Cuartel.Adelia", "clave": ""}) is False
    assert autorizacion_roles_usa_ruba({}) is False


@pytest.mark.parametrize("usuario, clave, ok", [
    ("cuartel.adelia", "S3creta!", True),     # usuario sin distinguir mayúsculas
    (" Cuartel.Adelia ", "S3creta!", True),
    ("Cuartel.Adelia", "s3creta!", False),    # clave exacta
    ("otro", "S3creta!", False),
    ("", "", False),
])
def test_autorizacion_con_usuario_ruba(usuario, clave, ok):
    from app.services.personal_service import autorizar_cambio_roles

    assert autorizar_cambio_roles(usuario, clave, CUENTA_RUBA) is ok
