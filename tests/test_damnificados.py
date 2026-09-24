"""Sección Damnificados (tres tarjetas No / Sí): datos, validación,
guardado, payload de RUBA y planilla PCD2. Base SQLite temporal."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
import app.reports.excel_generator as eg
from app.core.catalogos import obtener_padron
from app.models import Personal, BienAfectado, BomberoDamnificado, DamnificadoCivil, Incidente
from app.services.ruba_payload import payload_desde_incidente
from app.ui.damnificados_widgets import COL_C_APELLIDO, COL_C_LESION, COL_C_NOMBRE, PanelDamnificados
from tests.ayudas import cargar_servicio_basico, despachar


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def padron():
    return obtener_padron().bomberos


@pytest.fixture
def base_temporal(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()


def _civil(panel, fila, nombre, apellido, lesion):
    grilla = panel.grilla_civiles
    grilla.campo(fila, COL_C_NOMBRE).setText(nombre)
    grilla.campo(fila, COL_C_APELLIDO).setText(apellido)
    combo = grilla.campo(fila, COL_C_LESION)
    combo.setCurrentIndex(combo.findText(lesion))


def test_tarjetas_arrancan_en_no_y_no_aportan_datos(qapp, padron):
    panel = PanelDamnificados(padron, mapping={})
    for tarjeta in (panel.tarjeta_civiles, panel.tarjeta_bienes, panel.tarjeta_bomberos):
        assert not tarjeta.activa() and tarjeta.contenido.isHidden()
    assert panel.civiles() == [] and panel.bienes() == [] and panel.bomberos() == []
    assert panel.conteos() == {"heridos": 0, "fallecidos": 0, "desaparecidos": 0, "bomberos": 0}
    assert panel.validar() == []


def test_civiles_cantidad_lesion_y_conteos(qapp, padron):
    panel = PanelDamnificados(padron, mapping={})
    panel.tarjeta_civiles.set_activa(True)
    assert not panel.tarjeta_civiles.contenido.isHidden()
    assert panel.grilla_civiles.tabla.rowCount() == 1  # al activar arranca con una fila

    panel.grilla_civiles.set_cantidad(3)
    _civil(panel, 0, "Ana", "Gómez", "Herido grave")
    _civil(panel, 1, "Luis", "Pérez", "Fallecido")
    _civil(panel, 2, "", "", "Desaparecido")
    assert panel.conteos() == {"heridos": 1, "fallecidos": 1, "desaparecidos": 1, "bomberos": 0}

    panel.grilla_civiles.set_cantidad(2)  # conserva las primeras filas
    assert [c["nombre"] for c in panel.civiles()] == ["Ana", "Luis"]
    assert panel.civiles()[0]["lesion"] == "Herido grave" and panel.civiles()[0]["condicion"] == "Herido"

    panel.tarjeta_civiles.set_activa(False)  # en "No" no se guarda nada, pero no se pierde lo tipeado
    assert panel.civiles() == []
    panel.tarjeta_civiles.set_activa(True)
    assert panel.civiles()[0]["nombre"] == "Ana"


def test_validacion_de_bienes_y_bomberos(qapp, padron):
    panel = PanelDamnificados(padron, mapping={})
    panel.tarjeta_bienes.set_activa(True)
    panel.tarjeta_bomberos.set_activa(True)
    errores = panel.validar()
    assert "Bien 1: describí el bien afectado." in errores
    assert "Bombero damnificado 1: elegí un bombero del padrón." in errores

    panel.grilla_bienes.tabla.cellWidget(0, 1).setText("Camioneta del propietario")
    selector = panel.grilla_bomberos.tabla.cellWidget(0, 0)
    selector.set_id_ruba(padron[1].id_ruba)
    otro = panel.grilla_bomberos.agregar_fila()
    otro.set_id_ruba(padron[1].id_ruba)
    assert panel.validar() == ["Bombero damnificado 2: está repetido."]


def test_guardado_payload_y_pcd2(qapp, base_temporal, padron, monkeypatch, tmp_path):
    from app.ui import main_window as mw

    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    ventana = mw.MainWindow()
    cargar_servicio_basico(ventana, mw, padron)          # Dotación 1: chofer p0, embarcado p1, Jefe p2
    despachar(ventana, 9906, padron[3], (padron[4],), jefe=padron[5])  # Dotación 2: chofer p3, embarcado p4, Jefe p5

    danos = ventana.panel_damnificados
    danos.tarjeta_civiles.set_activa(True)
    danos.grilla_civiles.set_cantidad(2)
    _civil(danos, 0, "Ana", "Gómez", "Herido leve")
    _civil(danos, 1, "Luis", "Pérez", "Desaparecido")
    danos.tarjeta_bienes.set_activa(True)
    fila_bien = danos.grilla_bienes.tabla
    fila_bien.cellWidget(0, 0).setCurrentText("Rodado")
    fila_bien.cellWidget(0, 1).setText("Pick-up Ford, frente quemado")
    fila_bien.cellWidget(0, 2).setText("Juan Pérez")
    danos.tarjeta_bomberos.set_activa(True)
    danos.grilla_bomberos.tabla.cellWidget(0, 0).set_id_ruba(padron[4].id_ruba)   # de la Unidad 2
    danos.grilla_bomberos.tabla.cellWidget(0, 1).setText("Esguince, atendido por SEM")

    payload = ventana.obtener_payload_servicio()
    general = payload["editar_general"]
    assert (general["civiles_heridos"], general["civiles_fallecidos"], general["civiles_desaparecidos"]) == (1, 0, 1)
    assert [h["nombre"] for h in payload["damnificados"]["heridos"]] == ["Ana"]
    assert payload["damnificados"]["bienes"][0]["tipo"] == "Rodado"
    assert payload["damnificados"]["bomberos"][0]["bombero"]["id_ruba"] == padron[4].id_ruba
    assert payload["participacion"]["bomberos_heridos"] == 1

    resultado = ventana._persistir_incidente()
    assert resultado is not None
    assert payload_desde_incidente(resultado[0]) == payload
    with db.get_session() as session:
        incidente = session.get(Incidente, resultado[0])
        assert (incidente.damnificados_heridos, incidente.damnificados_desaparecidos, incidente.bomberos_lesionados) == (1, 1, 1)
        civiles = session.query(DamnificadoCivil).filter_by(incidente_id=incidente.id).order_by(DamnificadoCivil.id).all()
        assert [(c.lesion, c.condicion) for c in civiles] == [("Herido leve", "Herido"), ("Desaparecido", "Desaparecido")]
        (bien,) = session.query(BienAfectado).all()
        assert (bien.tipo, bien.titular) == ("Rodado", "Juan Pérez")
        (bombero,) = session.query(BomberoDamnificado).all()
        assert bombero.detalle_atencion == "Esguince, atendido por SEM"

    monkeypatch.setattr(eg, "OUTPUT_DIR", tmp_path)
    hoja = openpyxl.load_workbook(eg.generar_pcd2(resultado[0])).active
    with db.get_session() as session:
        nombre = {p.id_ruba: p.nombre_completo() for p in session.query(Personal)}
    assert (hoja["A9"].value, hoja["B11"].value, hoja["E15"].value) == (1, nombre[padron[2].id_ruba], nombre[padron[1].id_ruba])
    assert (hoja["A32"].value, hoja["B34"].value, hoja["E38"].value) == (2, nombre[padron[5].id_ruba], nombre[padron[4].id_ruba])
    ventana.close()
