"""Formulario de siniestro: bloque condicional por Tipo/Subtipo, opciones
desde ruba_mapping.json y guardado. Corre contra
una base SQLite temporal (nunca toca data/fire_station.db)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import time

import pytest
from PySide6.QtWidgets import QApplication, QComboBox
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
from app.core.catalogos import leer_mapping
from app.models import CategoriaIncidente, Incidente
from app.ui.siniestro_widgets import (
    FORM_ACCIDENTE,
    FORM_ESTRUCTURAL,
    FORM_FORESTAL,
    FORM_INCENDIO,
    TEXTO_SIN_OPCIONES,
    PanelDatosEspecificos,
    formulario_para,
)
from tests.ayudas import despachar


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def base_temporal(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()
    return engine


# -- Reglas Tipo/Subtipo ---------------------------------------------------------

@pytest.mark.parametrize("tipo, subtipo, esperado", [
    (3, "19", FORM_FORESTAL),
    (3, 15, FORM_ESTRUCTURAL),
    (3, "21", FORM_ESTRUCTURAL),
    (3, "23", FORM_ESTRUCTURAL),
    (3, "22", FORM_INCENDIO),  # Incendio vehicular: solo la causa
    (1, "3", FORM_ACCIDENTE),
    (1, None, FORM_ACCIDENTE),
    (5, "28", None),
    (None, None, None),
])
def test_formulario_para(tipo, subtipo, esperado):
    assert formulario_para(tipo, subtipo) == esperado


# -- Panel condicional ---------------------------------------------------------

def test_panel_cambia_de_pagina_y_usa_codigos_del_mapping(qapp):
    mapping = leer_mapping()
    panel = PanelDatosEspecificos(mapping=mapping)

    panel.actualizar(5, "28")
    assert panel.formulario_activo() is None and panel.datos() is None

    panel.actualizar(3, "19")
    assert panel.formulario_activo() == FORM_FORESTAL
    forestal = mapping["selectores"]["editar_general"]["condicionales"]["incendio_forestal"]
    combo_lugar: QComboBox = panel.pagina(FORM_FORESTAL).widget("tipo_lugar")
    codigos = [combo_lugar.itemData(i) for i in range(1, combo_lugar.count())]
    assert codigos == list(forestal["tipo_lugar_opciones"].values())

    combo_lugar.setCurrentIndex(combo_lugar.findData("7"))  # pastizal
    pagina = panel.pagina(FORM_FORESTAL)
    pagina.widget("unidad_superficie").setCurrentIndex(pagina.widget("unidad_superficie").findData("3"))
    pagina.widget("cantidad_superficie").setValue(12.5)
    pagina.widget("causa").setCurrentIndex(pagina.widget("causa").findData("4"))
    assert panel.datos() == {
        "formulario": FORM_FORESTAL,
        "campos": {"tipo_lugar": "7", "unidad_superficie": "3", "cantidad_superficie": 12.5, "causa": "4"},
    }

    panel.limpiar()
    assert panel.datos() == {"formulario": FORM_FORESTAL, "campos": {}}


def test_panel_estructural_radios_y_numeros(qapp):
    panel = PanelDatosEspecificos(mapping=leer_mapping())
    panel.actualizar(3, "23")
    pagina = panel.pagina(FORM_ESTRUCTURAL)

    grupo = pagina.widget("habia_extintores")
    assert [b.property("valor_ruba") for b in grupo.buttons()] == ["si", "no", "desconoce"]
    grupo.buttons()[2].setChecked(True)
    pagina.widget("numero_piso").setValue(0)       # planta baja es un dato válido
    pagina.widget("cantidad_pisos").setValue(2)
    pagina.widget("tipo_techo").setCurrentIndex(pagina.widget("tipo_techo").findData("4"))

    assert panel.datos()["campos"] == {
        "habia_extintores": "desconoce", "numero_piso": 0, "cantidad_pisos": 2, "tipo_techo": "4",
    }


def test_combo_sin_opciones_en_mapping_queda_deshabilitado(qapp):
    panel = PanelDatosEspecificos(mapping={})  # mapping sin opciones: no se inventan códigos
    panel.actualizar(1, "3")
    combo_clima = panel.pagina(FORM_ACCIDENTE).widget("clima")
    assert not combo_clima.isEnabled()
    assert combo_clima.currentText() == TEXTO_SIN_OPCIONES and combo_clima.currentData() is None
    assert panel.datos() == {"formulario": FORM_ACCIDENTE, "campos": {}}


def test_accidente_toma_opciones_cuando_el_mapping_las_define(qapp):
    mapping = {"selectores": {"editar_general": {"condicionales": {"accidente": {
        "clima_opciones": {"despejado": "1", "lluvia": "2"},
    }}}}}
    panel = PanelDatosEspecificos(mapping=mapping)
    combo = panel.pagina(FORM_ACCIDENTE).widget("clima")
    assert combo.isEnabled()
    assert [combo.itemText(i) for i in range(combo.count())] == ["— Seleccionar —", "Despejado", "Lluvia"]


# -- Catálogo completo en la base y guardado end-to-end --------------------------

def test_init_db_sincroniza_todos_los_subtipos_del_mapping(base_temporal):
    catalogo = leer_mapping()["catalogo"]
    esperado = sum(len(t["subtipos"]) for t in catalogo.values())
    with db.get_session() as session:
        assert session.query(CategoriaIncidente).count() == esperado
        db.init_db()  # idempotente
    with db.get_session() as session:
        assert session.query(CategoriaIncidente).count() == esperado


def test_main_window_reacciona_y_guarda(qapp, base_temporal, monkeypatch):
    from app.ui import main_window as mw

    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    ventana = mw.MainWindow()

    def elegir(tipo_id, codigo):
        ventana.combo_tipo.setCurrentIndex(ventana.combo_tipo.findData(tipo_id))
        for i in range(ventana.combo_categoria.count()):
            if ventana.combo_categoria.itemData(i, mw.ROL_CODIGO_RUBA) == codigo:
                ventana.combo_categoria.setCurrentIndex(i)
                return
        raise AssertionError(f"No está la categoría {codigo}")

    elegir(3, "19")
    assert ventana.panel_datos_especificos.formulario_activo() == FORM_FORESTAL
    elegir(3, "15")
    assert ventana.panel_datos_especificos.formulario_activo() == FORM_ESTRUCTURAL
    elegir(1, "3")
    assert ventana.panel_datos_especificos.formulario_activo() == FORM_ACCIDENTE
    elegir(6, "34")
    assert ventana.panel_datos_especificos.formulario_activo() is None

    # Guardado completo de un incendio estructural.
    elegir(3, "23")
    pagina = ventana.panel_datos_especificos.pagina(FORM_ESTRUCTURAL)
    pagina.widget("tipo_lugar").setCurrentIndex(pagina.widget("tipo_lugar").findData("1"))
    pagina.widget("habia_nichos").buttons()[1].setChecked(True)
    ventana.entry_calle.setText("Belgrano 58")
    ventana.texto_resena.setPlainText("Incendio de vivienda.")
    ventana.campo_hora_salida.set_value(time(8, 20))    # cerrar exige salida y regreso
    ventana.campo_hora_llegada.set_value(time(9, 45))
    ventana._on_horario_general_cambiado()
    padron = ventana._padron
    despachar(ventana, 4326, padron[0], (padron[1],), encargado_en=0)

    resultado = ventana._persistir_incidente()
    assert resultado is not None
    with db.get_session() as session:
        incidente = session.get(Incidente, resultado[0])
        assert incidente.datos_especificos == {
            "formulario": FORM_ESTRUCTURAL, "campos": {"tipo_lugar": "1", "habia_nichos": "no"},
        }

    ventana._limpiar_formulario()
    assert ventana.panel_dotaciones.unidades() == []
    ventana.close()
