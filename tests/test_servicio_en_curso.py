"""Ciclo de vida 'Emergencia en curso': borrador sin regreso, tarjeta con
cronómetro en Inicio, reapertura precargada y cierre sobre el MISMO
incidente. Base SQLite temporal."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date, datetime, time

import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
from app.core.catalogos import obtener_padron
from app.models import DamnificadoCivil, DotacionSalida, EstadoOperativo, EstadoRuba, Incidente, SalidaUnidad
from app.services import ruba_service
from app.ui.damnificados_widgets import COL_C_NOMBRE
from app.ui.servicio_en_curso import formatear_transcurrido
from tests.ayudas import despachar, elegir_categoria


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def padron():
    return obtener_padron().bomberos


@pytest.fixture
def ventana(qapp, tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()
    from app.ui import main_window as mw

    avisos = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: avisos.append(a[2]))
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: avisos.append(a[2]))
    monkeypatch.setattr(mw, "generar_e_imprimir_pcs", lambda i: f"PCS-{i}")
    monkeypatch.setattr(mw, "generar_e_imprimir_pcd2", lambda i: f"PCD2-{i}")
    v = mw.MainWindow()
    v._avisos = avisos
    yield v
    v.close()


def _salida_en_curso(ventana, padron):
    """Lo que se sabe al despachar: tipo, dirección, unidad con chofer y
    dotación, hora de salida. Sin regreso, sin Encargado, sin reseña."""
    from app.ui import main_window as mw

    elegir_categoria(ventana, mw, 3, "19")
    pagina = ventana.panel_datos_especificos.pagina("incendio_forestal")
    pagina.widget("causa").setCurrentIndex(pagina.widget("causa").findData("4"))
    ventana.entry_calle.setText("Ruta 8 km 603")
    ventana.campo_hora_llamado.set_value(time(8, 15))
    ventana.campo_fecha_salida.set_value(date(2026, 9, 23))
    ventana.campo_hora_salida.set_value(time(8, 20))
    ventana._on_horario_general_cambiado()
    despachar(ventana, 4326, padron[0], (padron[1], padron[2]))
    despachar(ventana, 9906, padron[3], (padron[4],))
    ventana.panel_damnificados.tarjeta_civiles.set_activa(True)
    ventana.panel_damnificados.grilla_civiles.campo(0, COL_C_NOMBRE).setText("Ana")


def test_boton_existe_con_su_object_name(ventana):
    assert ventana.boton_borrador.objectName() == "botonBorradorCurso"
    assert "Salida en Curso" in ventana.boton_borrador.text()


def test_borrador_sin_regreso_ni_encargado_queda_en_curso(ventana, padron):
    from app.ui import main_window as mw

    _salida_en_curso(ventana, padron)
    assert ventana._validar(), "el cierre SÍ tiene que exigir regreso, Encargado y reseña"
    numero = ventana.label_numero_parte.text()

    ventana.boton_borrador.click()
    with db.get_session() as session:
        incidente = session.query(Incidente).one()
        assert incidente.numero_parte == numero
        assert incidente.estado_operativo == EstadoOperativo.EN_CURSO.value
        assert incidente.estado_ruba == EstadoRuba.PENDIENTE.value
        assert incidente.hora_regreso is None
        assert len(incidente.salidas_unidad) == 2

    # Vuelve a Inicio con la tarjeta destacada y el formulario listo para otro servicio.
    assert ventana._stack.currentIndex() == mw.IDX_DASHBOARD
    (tarjeta,) = ventana._tarjetas_en_curso
    assert numero in tarjeta.label_parte.text() and "Forestal" in tarjeta.label_parte.text()
    assert "Rojo 18" in tarjeta.label_detalle.text() and "Rojo 23" in tarjeta.label_detalle.text()
    assert "23/09 08:20" in tarjeta.label_detalle.text()
    assert tarjeta.resumen.inicio == datetime(2026, 9, 23, 8, 20)
    tarjeta.tick(datetime(2026, 9, 23, 9, 5, 7))
    assert tarjeta.label_cronometro.text() == "00:45:07"
    assert not ventana._chip_en_curso.isHidden() and "1 servicio" in ventana._chip_en_curso.text()
    assert ventana.label_numero_parte.text() != numero


def test_continuar_precarga_y_cerrar_actualiza_el_mismo_incidente(ventana, padron):
    _salida_en_curso(ventana, padron)
    ventana.boton_borrador.click()
    with db.get_session() as session:
        incidente_id = session.query(Incidente).one().id

    ventana._tarjetas_en_curso[0].boton_continuar.click()
    assert ventana._incidente_en_edicion == incidente_id
    assert "En curso" in ventana._chip_borrador.text()

    # Precargado: tipo, datos específicos, horario, unidades con su dotación, damnificados.
    assert ventana.entry_calle.text() == "Ruta 8 km 603"
    assert ventana.panel_datos_especificos.datos()["campos"] == {"causa": "4"}
    assert ventana.campo_hora_salida.value() == time(8, 20) and ventana.campo_hora_llegada.value() is None
    unidades = ventana.panel_dotaciones.datos()
    assert [u["movil_id_ruba"] for u in unidades] == [4326, 9906]
    assert [b["id_ruba"] for b in unidades[0]["bomberos"]] == [padron[1].id_ruba, padron[2].id_ruba]
    assert ventana.panel_damnificados.civiles()[0]["nombre"] == "Ana"

    # Regresan: se completa el cierre.
    ventana.campo_fecha_llegada.set_value(date(2026, 9, 23))
    ventana.campo_hora_llegada.set_value(time(11, 40))
    ventana._on_horario_general_cambiado()
    ventana.panel_dotaciones.unidades()[0].selector_jefe.set_id_ruba(padron[5].id_ruba)
    ventana.panel_dotaciones.unidades()[1].selector_jefe.set_id_ruba(padron[6].id_ruba)
    ventana.texto_resena.setPlainText("Incendio de pastizal controlado.")
    assert ventana._validar() == []

    ventana.boton_guardar_local.click()
    with db.get_session() as session:
        (incidente,) = session.query(Incidente).all()  # el mismo, no uno nuevo
        assert incidente.id == incidente_id
        assert incidente.estado_operativo == EstadoOperativo.CERRADO.value
        assert incidente.hora_regreso == time(11, 40)
        assert [su.hora_regreso for su in incidente.salidas_unidad] == [time(11, 40), time(11, 40)]
        assert session.query(SalidaUnidad).count() == 2          # rehechas, no duplicadas
        assert session.query(DotacionSalida).count() == 5          # 3 embarcados + 2 Jefes
        assert session.query(DamnificadoCivil).count() == 1
    assert ventana._tarjetas_en_curso == [] and ventana._chip_en_curso.isHidden()
    assert ventana._incidente_en_edicion is None


def test_borrador_bloquea_datos_inconsistentes(ventana, padron):
    unidad = ventana.panel_dotaciones.agregar_unidad()           # sin móvil
    unidad.selector_chofer.setText("Nadie Conocido")
    ventana.boton_borrador.click()
    assert "Dotación N° 1: elegí el móvil." in ventana._avisos[-1]
    assert "no coincide con nadie" in ventana._avisos[-1]
    with db.get_session() as session:
        assert session.query(Incidente).count() == 0


def test_en_curso_no_se_sincroniza_ni_cuenta_como_pendiente(ventana, padron):
    _salida_en_curso(ventana, padron)
    ventana.boton_borrador.click()
    with db.get_session() as session:
        incidente_id = session.query(Incidente).one().id

    with pytest.raises(ValueError, match="EN CURSO"):
        ruba_service.sincronizar_incidente(incidente_id)

    encolados = []
    ventana._encolar_sincronizacion = lambda *a, **k: encolados.append(a)
    ventana._sincronizar_todos_pendientes()
    assert encolados == []

    ventana._pagina_historial.refrescar()
    tabla = ventana._pagina_historial.tabla
    assert tabla.item(0, 5).text() == "⏱️ En curso"
    textos = [b.text() for b in tabla.cellWidget(0, 6).findChildren(type(ventana.boton_limpiar))]
    assert "✏️ Continuar" in textos
    boton_ruba = next(b for b in tabla.cellWidget(0, 6).findChildren(type(ventana.boton_limpiar)) if "RUBA" in b.text())
    assert not boton_ruba.isEnabled()


def test_formato_del_cronometro():
    inicio = datetime(2026, 9, 23, 8, 0, 0)
    assert formatear_transcurrido(inicio, datetime(2026, 9, 23, 10, 3, 9)) == "02:03:09"
    assert formatear_transcurrido(inicio, datetime(2026, 9, 24, 8, 0, 0)) == "24:00:00"
    assert formatear_transcurrido(None) == "--:--:--"
