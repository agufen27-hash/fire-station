"""Dotaciones por Unidad (formato PCD2), sincronización de catálogos
oficiales y payload aplanado para RUBA. Base SQLite temporal."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date, datetime, time

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
import app.reports.excel_generator as eg
from app.core.semilla import personal_inicial
from app.core.catalogos import leer_mapping, obtener_catalogos_ruba, obtener_padron
from app.models import DotacionSalida, Incidente, Movil, Personal, PersonalBase, RolDotacion, SalidaUnidad
from app.services.ruba_payload import (
    numero_parte_ruba,
    payload_desde_incidente,
    separar_calle_altura,
    validar_payload,
)
from app.ui.dotaciones_widgets import PanelDotaciones, personas_repetidas
from app.ui.participacion_widgets import HorarioServicio, SelectorBombero, texto_bombero
from tests.ayudas import despachar, elegir_categoria


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


@pytest.fixture(scope="module")
def padron():
    return obtener_padron().bomberos


@pytest.fixture
def panel(qapp, padron):
    return PanelDotaciones(obtener_catalogos_ruba().moviles, padron)


# -- Catálogos en la base -------------------------------------------------------

def test_init_db_vincula_moviles_y_personal_oficiales(base_temporal, padron):
    with db.get_session() as session:
        assert session.query(Movil).filter(Movil.id_ruba.isnot(None)).count() == 10
        assert session.query(Personal).filter(Personal.id_ruba.isnot(None)).count() == len(padron) == 48
        # Cada persona sembrada que también está en el padrón quedó UNA sola
        # vez, vinculada a su ID oficial (sin duplicarse al fusionar).
        for persona in personal_inicial():
            oficial = next((b for b in padron if b.dni == persona["dni"]), None)
            if oficial is None:
                continue
            filas = session.query(Personal).filter(Personal.dni == persona["dni"]).all()
            assert len(filas) == 1 and filas[0].id_ruba == oficial.id_ruba
    db.init_db()
    with db.get_session() as session:
        assert session.query(Personal).filter(Personal.id_ruba.isnot(None)).count() == 48


def test_selector_bombero_resuelve_solo_textos_del_padron(qapp, padron):
    selector = SelectorBombero(padron)
    bombero = padron[0]
    selector.setText(texto_bombero(bombero).lower())
    assert selector.id_ruba() == bombero.id_ruba
    selector.setText("NADIE, Inexistente")
    assert selector.id_ruba() is None and selector.property("invalido") is True


# -- Panel de dotaciones ---------------------------------------------------------

def test_unidades_con_su_dotacion_y_horario_propio(panel, padron):
    panel.set_horario_general(HorarioServicio(date(2026, 9, 23), time(8, 20), date(2026, 9, 23), time(9, 45)))
    u1 = panel.agregar_unidad()
    u2 = panel.agregar_unidad()
    assert [u.numero for u in panel.unidades()] == [1, 2]
    assert u1.combo_movil.count() == 11  # "Seleccionar" + 10 oficiales
    assert u2.hora_salida.value() == time(8, 20)

    u2.hora_salida.time_edit.setTime(u2.hora_salida.time_edit.time().addSecs(10 * 60))  # sale 10' después
    panel.set_horario_general(HorarioServicio(date(2026, 9, 23), time(8, 30), date(2026, 9, 23), time(10, 0)))
    assert u1.hora_salida.value() == time(8, 30)
    assert u2.hora_salida.value() == time(8, 30)  # 08:20 + 10' = 08:30 propio, ya no sigue al general
    assert u2.hora_regreso.value() == time(10, 0)

    panel.quitar_unidad(u1)
    assert [u.numero for u in panel.unidades()] == [1]


def test_jefe_de_dotacion_grado_y_firma(base_temporal, panel, padron):
    u1 = panel.agregar_unidad()
    u1.selector_jefe.set_id_ruba(padron[2].id_ruba)
    assert u1.label_grado.text() != "—"                     # grado/cargo automático del padrón
    assert u1.validar_pin("0000") == "No tiene firma registrada en su legajo (Documentación)."

    u1._marcar_firmado(padron[2].id_ruba, datetime(2026, 9, 23, 10, 0))
    assert u1.esta_firmada() and u1.datos()["firmada"]
    u1.selector_jefe.set_id_ruba(padron[3].id_ruba)         # cambiar de Jefe invalida la firma
    assert not u1.esta_firmada()


def test_validacion_de_dotaciones(panel, padron):
    assert "Agregá al menos una Dotación." in panel.validar()
    u1 = panel.agregar_unidad()
    u1.combo_movil.setCurrentIndex(u1.combo_movil.findData(4326))
    u1.selector_chofer.set_id_ruba(padron[0].id_ruba)
    fila = u1.agregar_bombero()
    fila.selector.set_id_ruba(padron[0].id_ruba)  # el chofer repetido como embarcado
    u2 = panel.agregar_unidad()
    u2.combo_movil.setCurrentIndex(u2.combo_movil.findData(4326))
    u2.selector_chofer.setText("Cualquiera")
    errores = panel.validar()
    assert "Dotación N° 2: el móvil ya está en otra dotación." in errores
    assert "Dotación N° 1: elegí el Jefe de Dotación del padrón." in errores
    assert any("'Cualquiera' no coincide" in e for e in errores)
    assert not any("Jefe" in e for e in panel.validar(parcial=True))
    repetidos = personas_repetidas(panel.datos(), {"operador_1": padron[0].id_ruba, "apresto": []})
    assert repetidos == ["Una misma persona figura dos veces "
                         "(chofer de la Dotación N° 1 y embarcado en la Dotación N° 1 y Operador 1)."]


# -- Payload, persistencia y planillas -------------------------------------------

def test_helpers_de_formato():
    assert numero_parte_ruba("004/2026") == "0042026"
    assert separar_calle_altura("Belgrano 58") == ("Belgrano", "58")
    assert separar_calle_altura("Ruta 8 km 603") == ("Ruta 8 km", "603")
    assert separar_calle_altura("Zona rural") == ("Zona rural", None)


def _cargar_dos_unidades(ventana, mw, padron):
    """Dotación 1: chofer p0, Jefe p2, embarcado p1. Dotación 2 (sale 08:35):
    chofer p3, Jefe p5, embarcado p4. En base: Operador 1 p6, apresto p7."""
    elegir_categoria(ventana, mw, 3, "19")
    pagina = ventana.panel_datos_especificos.pagina("incendio_forestal")
    pagina.widget("tipo_lugar").setCurrentIndex(pagina.widget("tipo_lugar").findData("7"))
    pagina.widget("causa").setCurrentIndex(pagina.widget("causa").findData("2"))  # intencional
    ventana.campo_hora_llamado.set_value(time(8, 15))
    ventana.entry_calle.setText("Belgrano 58")
    ventana.texto_resena.setPlainText("Quema de pastizal en banquina.")
    ventana.campo_fecha_salida.set_value(date(2026, 9, 23))
    ventana.campo_fecha_llegada.set_value(date(2026, 9, 23))
    ventana.campo_hora_salida.set_value(time(8, 20))
    ventana.campo_hora_llegada.set_value(time(9, 45))
    ventana._on_horario_general_cambiado()
    despachar(ventana, 4326, padron[0], (padron[1],), jefe=padron[2])
    u2 = despachar(ventana, 9906, padron[3], (padron[4],), jefe=padron[5])
    u2.hora_salida.time_edit.setTime(u2.hora_salida.time_edit.time().fromString("08:35", "HH:mm"))
    ventana.panel_base.selector_operador_1.set_id_ruba(padron[6].id_ruba)
    ventana.panel_base.agregar_apresto().set_id_ruba(padron[7].id_ruba)


def test_payload_aplanado_y_persistencia_por_unidad(qapp, base_temporal, padron, monkeypatch, tmp_path):
    from app.ui import main_window as mw

    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: None)
    ventana = mw.MainWindow()
    _cargar_dos_unidades(ventana, mw, padron)

    payload = ventana.obtener_payload_servicio()
    json.dumps(payload)
    assert validar_payload(payload) == []

    vehiculos = payload["intervencion_vehiculos"]["vehiculos"]
    assert [v["select_vehiculo"] for v in vehiculos] == ["4326", "9906"]
    assert [v["hora_salida"] for v in vehiculos] == ["08:20", "08:35"]

    # RUBA: chofer, Jefe y embarcados de cada dotación (Intervinientes, con el
    # horario de SU dotación); el Jefe de la Dotación 1 es el Encargado; el
    # personal en base va como Apresto.
    bomberos = payload["intervencion_bomberos"]["bomberos"]
    orden = [padron[i].id_ruba for i in (0, 2, 1, 3, 5, 4, 6, 7)]
    assert [b["autocomplete_nombre"]["id_ruba"] for b in bomberos] == orden
    assert [b["is_encargado"] for b in bomberos] == [False, True] + [False] * 6
    assert [b["tipo_tarea"] for b in bomberos] == ["1"] * 6 + ["2"] * 2
    assert [b["hora_inicio"] for b in bomberos][:6] == ["08:20"] * 3 + ["08:35"] * 3
    assert payload["intervencion_bomberos"]["cantidad_bomberos"] == 8

    resultado = ventana._persistir_incidente()
    assert resultado is not None
    assert payload_desde_incidente(resultado[0]) == payload  # mismo payload desde la base

    with db.get_session() as session:
        salidas = session.query(SalidaUnidad).filter_by(incidente_id=resultado[0]).order_by(SalidaUnidad.id).all()
        assert [s.movil.id_ruba for s in salidas] == [4326, 9906]
        assert [s.chofer.id_ruba for s in salidas] == [padron[0].id_ruba, padron[3].id_ruba]
        assert [s.hora_salida for s in salidas] == [time(8, 20), time(8, 35)]
        assert all(s.jefe_grado for s in salidas)
        dotacion = {s.movil.id_ruba: sorted(f.personal.id_ruba for f in s.dotacion) for s in salidas}
        assert dotacion == {4326: sorted([padron[1].id_ruba, padron[2].id_ruba]),
                            9906: sorted([padron[4].id_ruba, padron[5].id_ruba])}
        filas = session.query(DotacionSalida).filter_by(incidente_id=resultado[0]).all()
        assert sum(f.rol == RolDotacion.A_CARGO.value for f in filas) == 2  # un Jefe por dotación
        assert {f.tipo_tarea for f in filas} == {"1"}
        base = session.query(PersonalBase).filter_by(incidente_id=resultado[0]).order_by(PersonalBase.orden).all()
        assert sorted((b.funcion, b.personal.id_ruba) for b in base) == sorted([
            ("OPERADOR_1", padron[6].id_ruba), ("APRESTO", padron[7].id_ruba)])

    # PCD2 alimentada por la estructura jerárquica
    monkeypatch.setattr(eg, "OUTPUT_DIR", tmp_path)
    hoja = openpyxl.load_workbook(eg.generar_pcd2(resultado[0])).active
    with db.get_session() as session:  # la planilla imprime el legajo local (conserva sus acentos)
        nombres = {p.id_ruba: p.nombre_completo() for p in session.query(Personal).filter(Personal.id_ruba.isnot(None))}
    nombre = lambda b: nombres[b.id_ruba]  # noqa: E731
    assert (hoja["A9"].value, hoja["C9"].value, hoja["L9"].value) == (1, "Rojo 18", "08:20")
    assert (hoja["B11"].value, hoja["E14"].value, hoja["E15"].value) == (nombre(padron[2]), nombre(padron[0]), nombre(padron[1]))
    assert (hoja["A32"].value, hoja["C32"].value, hoja["L32"].value) == (2, "Rojo 23", "08:35")
    assert (hoja["B34"].value, hoja["E37"].value, hoja["E38"].value) == (nombre(padron[5]), nombre(padron[3]), nombre(padron[4]))
    assert (hoja["S6"].value, hoja["V6"].value) == (1, 1)
    ventana.close()


def test_payload_respeta_las_claves_del_mapping(qapp, base_temporal, padron):
    """Contrato con la automatización: cada campo del payload se llama igual
    que su selector en ruba_mapping.json."""
    from app.ui import main_window as mw

    ventana = mw.MainWindow()
    _cargar_dos_unidades(ventana, mw, padron)
    payload = ventana.obtener_payload_servicio()
    sel = leer_mapping()["selectores"]
    assert set(payload["inicializacion"]) <= set(sel["inicializacion"])
    assert set(payload["editar_general"]) - {"condicionales"} <= set(sel["editar_general"])
    assert set(payload["participacion"]) <= set(sel["participacion"])
    for b in payload["intervencion_bomberos"]["bomberos"]:
        assert set(b) <= set(sel["intervencion_bomberos"]["fila_bombero"])
    for v in payload["intervencion_vehiculos"]["vehiculos"]:
        assert set(v) <= set(sel["intervencion_vehiculos"]["fila_vehiculo"])
    ventana.close()
