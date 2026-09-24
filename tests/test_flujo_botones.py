"""Botones inferiores del formulario, de punta a punta: guardado local +
planillas, carga en RUBA en un QThread (contra el RUBA falso) con su diálogo
de progreso, y Limpiar Formulario. Base SQLite temporal."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time as reloj
from datetime import date, time

import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
from app.core.catalogos import obtener_padron
from app.models import EstadoRuba, Incidente
from app.services import ruba_automation as ra
from app.services import ruba_service
from app.ui.ruba_progreso_dialog import DialogoProgresoRuba
from tests.ayudas import cargar_servicio_basico
from tests.fake_ruba import FakeRuba


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def padron():
    return obtener_padron().bomberos


@pytest.fixture
def entorno(qapp, tmp_path, monkeypatch, padron):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True,
                           connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()

    from app.ui import main_window as mw

    # Planillas: se generan de verdad pero sin abrir Excel/visor.
    monkeypatch.setattr(mw, "generar_e_imprimir_pcs", lambda i: f"PCS-{i}")
    monkeypatch.setattr(mw, "generar_e_imprimir_pcd2", lambda i: f"PCD2-{i}")
    avisos = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: avisos.append(("info", a[2])))
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: avisos.append(("warning", a[2])))

    # La automatización real, apuntada al RUBA falso.
    sitio = FakeRuba([(b.id_ruba, f"{b.apellido}, {b.nombre} (DNI {b.dni})") for b in padron]).iniciar()

    class AutomatizacionDePrueba(ra.RubaServiceAutomation):
        def __init__(self, payload, **kwargs):
            super().__init__(
                payload,
                credenciales={"usuario": "g", "clave": "x", "url_login": sitio.url_login,
                              "url_incidentes": sitio.url_incidentes},
                dir_capturas=tmp_path / "capturas", ruta_sesion=tmp_path / "sesion.json", timeout_ms=5000, **kwargs,
            )

    monkeypatch.setattr(ruba_service, "RubaServiceAutomation", AutomatizacionDePrueba)
    ventana = mw.MainWindow()
    yield ventana, avisos, sitio
    ventana.close()
    sitio.detener()


def _cargar(ventana, padron, calle="Belgrano 58"):
    from app.ui import main_window as mw

    cargar_servicio_basico(ventana, mw, padron, calle=calle)


def _esperar(condicion, segundos=90):
    limite = reloj.time() + segundos
    while reloj.time() < limite:
        QApplication.processEvents()
        if condicion():
            return True
        reloj.sleep(0.05)
    return False


def _dialogo(ventana):
    return next(w for w in ventana.findChildren(DialogoProgresoRuba))


def test_guardar_y_cargar_a_ruba_con_dialogo_de_progreso(entorno, padron):
    ventana, avisos, _ = entorno
    _cargar(ventana, padron)
    numero = ventana.label_numero_parte.text()

    ventana.boton_guardar_ruba.click()
    dialogo = _dialogo(ventana)
    assert dialogo.isVisible() and not dialogo.isModal()
    assert "guardado en el histórico local" in dialogo.label_resumen_local.text()
    assert avisos == []  # sin cuadros modales en el camino a RUBA

    # La UI sigue viva mientras el QThread trabaja.
    assert _esperar(lambda: dialogo.estado_final is not None), "la carga no terminó"
    assert dialogo.estado_final == "ok", dialogo.label_mensaje.text()
    assert dialogo.entry_id.text() == "555"
    assert "/estructura/incidente/555" in dialogo.label_url.text()
    assert dialogo.barra.value() == 100
    assert all(icono.text() in ("✅", "⏭") for icono in dialogo._iconos.values())

    assert _esperar(lambda: not ventana._hilos_sync, 10)
    with db.get_session() as session:
        incidente = session.query(Incidente).filter_by(numero_parte=numero).one()
        assert incidente.estado_ruba == EstadoRuba.SINCRONIZADO.value
        assert incidente.ruba_id_remoto == "555"
    assert ventana.panel_dotaciones.unidades() == []  # el formulario quedó limpio para el próximo servicio


def test_falla_en_ruba_muestra_error_y_captura(entorno, padron):
    ventana, _, sitio = entorno
    sitio.categorias = {"3": []}  # RUBA "ya no ofrece" la categoría: falla la inicialización
    _cargar(ventana, padron)
    numero = ventana.label_numero_parte.text()
    ventana.boton_guardar_ruba.click()
    dialogo = _dialogo(ventana)

    assert _esperar(lambda: dialogo.estado_final is not None)
    assert dialogo.estado_final == "error"
    assert "[Inicialización]" in dialogo.label_mensaje.text()
    assert not dialogo.boton_captura.isHidden()
    assert dialogo._iconos["INICIALIZACION"].text() == "❌"
    assert _esperar(lambda: not ventana._hilos_sync, 10)
    with db.get_session() as session:
        incidente = session.query(Incidente).filter_by(numero_parte=numero).one()
        assert incidente.estado_ruba == EstadoRuba.ERROR.value and "Inicialización" in incidente.ruba_error_log


def test_validacion_previa_no_guarda_nada(entorno, padron):
    ventana, avisos, _ = entorno
    _cargar(ventana, padron)
    ventana.panel_dotaciones.unidades()[0].selector_jefe.clear()  # sin Jefe (= sin Encargado)
    ventana.boton_guardar_ruba.click()
    assert avisos and avisos[-1][0] == "warning" and "Jefe de Dotación" in avisos[-1][1]
    assert not ventana.findChildren(DialogoProgresoRuba)
    with db.get_session() as session:
        assert session.query(Incidente).count() == 0


def test_guardar_e_imprimir_y_limpiar(entorno, padron):
    ventana, avisos, _ = entorno
    _cargar(ventana, padron)
    ventana.boton_guardar_local.click()
    assert avisos[-1][0] == "info" and "PCS-" in avisos[-1][1] and "PCD2-" in avisos[-1][1]
    with db.get_session() as session:
        incidente = session.query(Incidente).one()
        assert incidente.estado_ruba == EstadoRuba.PENDIENTE.value  # no se envió a RUBA
    assert not ventana.findChildren(DialogoProgresoRuba)

    _cargar(ventana, padron, calle="San Martín 100")
    ventana.panel_damnificados.tarjeta_civiles.set_activa(True)
    ventana.boton_limpiar.click()
    assert ventana.entry_calle.text() == "" and not ventana.panel_damnificados.tarjeta_civiles.activa()
    assert ventana.panel_dotaciones.unidades() == []
    assert ventana.combo_tipo.currentIndex() == 0 and ventana.campo_hora_salida.value() is None
    assert ventana.entry_localidad.text() == "Adelia María"
