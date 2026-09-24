"""Sistema de diseño: temas, helpers de estado y que el restyling no rompa
objectNames funcionales ni el reintento de RUBA desde el historial."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
from app.ui import theme


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def base_temporal(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()


@pytest.mark.parametrize("nombre", ["oscuro", "claro"])
def test_temas_generan_qss_e_iconos(qapp, nombre):
    paleta = theme.aplicar_tema(qapp, nombre)
    assert theme.PALETA is paleta and paleta.nombre == nombre
    qss = qapp.styleSheet()
    for selector in ("#botonLimpiar", "#botonPrimarioAzul", "#botonPrimarioRojo", "#navButton",
                     "#seccionFormulario", "QHeaderView::section", "QLabel#chip[tono=\"ok\"]"):
        assert selector in qss
    for ruta in (linea.split("url(")[1].split(")")[0] for linea in qss.splitlines() if "url(" in linea):
        assert Path(ruta).exists(), ruta
    # Con QSS activo Qt envuelve el estilo (QStyleSheetStyle); el base se verifica
    # aplicando el tema sin hoja de estilos encima.
    qapp.setStyleSheet("")
    assert qapp.style().name().lower() == "fusion"
    theme.aplicar_tema(qapp, nombre)


def test_helpers_de_estado(qapp):
    theme.aplicar_tema(qapp, "oscuro")
    campo = QLineEdit()
    theme.marcar_invalido(campo, True)
    assert campo.property("invalido") is True
    theme.marcar_invalido(campo, False)
    assert campo.property("invalido") is False
    chip = QLabel()
    theme.set_tono(chip, "alerta")
    assert chip.property("tono") == "alerta"
    assert theme.color("rojo") == "#E53935"
    assert "*" in theme.etiqueta_requerida("Calle")


def test_preferencia_de_tema_persistente(tmp_path, monkeypatch):
    monkeypatch.setattr(theme, "_ruta_config", lambda: tmp_path / "config.json")
    assert theme.tema_guardado() == "oscuro"
    theme.guardar_tema("claro")
    assert theme.tema_guardado() == "claro"


def test_ventana_conserva_object_names_y_valida_en_linea(qapp, base_temporal, monkeypatch):
    from app.ui import main_window as mw

    theme.aplicar_tema(qapp, "oscuro")
    ventana = mw.MainWindow()
    assert ventana.boton_limpiar.objectName() == "botonLimpiar"
    assert ventana.boton_guardar_local.objectName() == "botonPrimarioAzul"
    assert ventana.boton_guardar_ruba.objectName() == "botonPrimarioRojo"

    ventana.entry_calle.clear()
    ventana._validar()
    assert ventana.entry_calle.property("invalido") is True
    ventana.entry_calle.setText("Belgrano 58")
    assert ventana.entry_calle.property("invalido") is False

    ventana._ir_a_pagina(mw.IDX_FORMULARIO)
    assert not ventana._chip_borrador.isHidden()
    assert ventana.label_numero_parte.text() in ventana._chip_borrador.text()
    assert "48 activos" in ventana._chip_padron.text()

    # Cambio de tema en caliente desde Configuración
    monkeypatch.setattr(theme, "guardar_tema", lambda nombre: None)
    ventana._combo_tema.setCurrentIndex(ventana._combo_tema.findData("claro"))
    assert theme.PALETA.nombre == "claro"
    ventana.close()


def test_reintento_desde_historial_usa_la_cola(qapp, base_temporal, monkeypatch):
    from app.ui import main_window as mw

    ventana = mw.MainWindow()
    encolados = []
    monkeypatch.setattr(ventana, "_encolar_sincronizacion", lambda i, n, d=None: encolados.append((i, n, d)))
    ventana._pagina_historial._reintentar_ruba(7, "007/2026")
    assert len(encolados) == 1 and encolados[0][:2] == (7, "007/2026")
    assert encolados[0][2] is not None  # con diálogo de progreso
    encolados[0][2].close()
    ventana.close()
