"""Botones inferiores del formulario (guardado local + planillas, Limpiar)
y carga EN LOTE a RUBA desde el Historial: QThread contra el RUBA falso, con
su diálogo modal de progreso. Base SQLite temporal."""

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
from app.models import EstadoRuba, Incidente, Personal
from app.services import ruba_automation as ra
from app.services import ruba_service
from app.ui.ruba_progreso_dialog import DialogoLoteRuba
from app.ui.siniestro_widgets import FORM_ESTRUCTURAL
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
    # Autoría con PIN al finalizar: el diálogo modal colgaría el test (offscreen).
    with db.get_session() as session:
        autor_id = session.query(Personal.id).order_by(Personal.id).first()[0]
    monkeypatch.setattr(mw, "pedir_autor", lambda *a, **k: autor_id)

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
    # Incendio de vivienda: sin "Tipo de lugar" el Semáforo Pre-RUBA no lo
    # deja encolar y _cargar_lote_ruba abriría un QMessageBox modal (cuelga).
    pagina = ventana.panel_datos_especificos.pagina(FORM_ESTRUCTURAL)
    pagina.widget("tipo_lugar").setCurrentIndex(pagina.widget("tipo_lugar").findData("1"))


def _esperar(condicion, segundos=90):
    limite = reloj.time() + segundos
    while reloj.time() < limite:
        QApplication.processEvents()
        if condicion():
            return True
        reloj.sleep(0.05)
    return False


def _con_internet(monkeypatch):
    """El worker de la cola consulta internet real antes de abrir Chromium:
    acá el RUBA es local (FakeRuba), así que se simula la conexión."""
    from app.services import ruba_queue_worker

    monkeypatch.setattr(ruba_queue_worker, "hay_conexion_internet", lambda *a, **k: True)


def _senales_cola(ventana):
    """Registra lo que emite la ColaRuba (corre en su QThread; las señales
    llegan al hilo de la UI mientras _esperar procesa eventos)."""
    eventos = {"sincronizado": [], "error": [], "inactiva": 0}
    cola = ventana._cola_ruba
    cola.sincronizado.connect(lambda i, n, r: eventos["sincronizado"].append(i))
    cola.error.connect(lambda i, n, m, c: eventos["error"].append((i, m)))
    cola.inactiva.connect(lambda: eventos.__setitem__("inactiva", eventos["inactiva"] + 1))
    return eventos


def _guardar_local(ventana, padron, calle="Belgrano 58"):
    """Guarda un parte con 💾 y devuelve su id (queda PENDIENTE de RUBA)."""
    _cargar(ventana, padron, calle=calle)
    numero = ventana.label_numero_parte.text()
    ventana.boton_guardar_local.click()
    with db.get_session() as session:
        return session.query(Incidente).filter_by(numero_parte=numero).one().id


def test_formulario_sin_boton_ruba_y_guardado_queda_pendiente(entorno, padron):
    ventana, avisos, _ = entorno
    assert not hasattr(ventana, "boton_guardar_ruba")
    incidente_id = _guardar_local(ventana, padron)
    with db.get_session() as session:
        assert session.get(Incidente, incidente_id).estado_ruba == EstadoRuba.PENDIENTE.value
    assert "Cargar Seleccionados a RUBA" in avisos[-1][1]  # el aviso indica dónde se carga a RUBA


def test_lote_carga_en_ruba_con_una_sola_sesion(entorno, padron, monkeypatch):
    """🚀 del Historial: los partes se encolan en la ColaRuba (segundo plano,
    sin diálogo modal) y se cargan con una sola sesión de navegador."""
    ventana, _, sitio = entorno
    _con_internet(monkeypatch)
    ids = [_guardar_local(ventana, padron), _guardar_local(ventana, padron, calle="San Martín 100")]
    eventos = _senales_cola(ventana)

    ventana._cargar_lote_ruba(ids)
    assert ventana._cola_ruba.activa  # el worker arrancó en su QThread
    assert not ventana.findChildren(DialogoLoteRuba)  # ya no hay modal: la UI queda libre
    with db.get_session() as session:  # encolados en el acto (o ya tomados por el worker)
        assert all(session.get(Incidente, i).estado_ruba in (EstadoRuba.EN_COLA.value,
                                                            EstadoRuba.SINCRONIZANDO.value,
                                                            EstadoRuba.SINCRONIZADO.value) for i in ids)

    assert _esperar(lambda: not ventana._cola_ruba.activa, 90), "la cola no terminó"
    assert sorted(eventos["sincronizado"]) == sorted(ids) and not eventos["error"]
    assert eventos["inactiva"] >= 1
    assert sitio.logins == 1  # mismo navegador y misma sesión para todo el lote
    assert not ventana.carga_ruba_en_curso()
    with db.get_session() as session:
        for incidente_id in ids:
            incidente = session.get(Incidente, incidente_id)
            assert incidente.estado_ruba == EstadoRuba.SINCRONIZADO.value
            assert incidente.ruba_id_remoto and incidente.ruba_sincronizado_en is not None
    assert ventana._chip_ruba.text().startswith("RUBA: Al día")


def test_lote_sigue_despues_de_un_parte_con_error(entorno, padron, monkeypatch):
    """Un parte con error de datos queda en ERROR (definitivo, sin reintento)
    y la cola sigue con el siguiente."""
    ventana, _, _ = entorno
    _con_internet(monkeypatch)
    malo = _guardar_local(ventana, padron)
    bueno = _guardar_local(ventana, padron, calle="San Martín 100")
    preparar_original = ruba_service.preparar_payload

    def preparar(incidente_id):
        if incidente_id == malo:
            raise ValueError("El incidente no está listo para RUBA: dato de prueba faltante.")
        return preparar_original(incidente_id)

    monkeypatch.setattr(ruba_service, "preparar_payload", preparar)
    eventos = _senales_cola(ventana)
    ventana._cargar_lote_ruba([malo, bueno])
    assert _esperar(lambda: not ventana._cola_ruba.activa, 90), "la cola no terminó"
    assert [i for i, _m in eventos["error"]] == [malo]
    assert eventos["sincronizado"] == [bueno]
    with db.get_session() as session:
        incidente_malo = session.get(Incidente, malo)
        assert incidente_malo.estado_ruba == EstadoRuba.ERROR.value
        assert "dato de prueba" in incidente_malo.ruba_error_log
        assert incidente_malo.proximo_reintento_ruba is None
        assert session.get(Incidente, bueno).estado_ruba == EstadoRuba.SINCRONIZADO.value
    assert ventana._chip_ruba.text().startswith("RUBA: 1 con error")


def test_worker_omite_cargados_y_respeta_cancelar(entorno, padron, monkeypatch):
    ventana, _, _ = entorno
    ids = [_guardar_local(ventana, padron), _guardar_local(ventana, padron, calle="San Martín 100"),
           _guardar_local(ventana, padron, calle="Mitre 7")]
    ruba_service._marcar_sincronizado(ids[0], "999")  # ya estaba en RUBA: no se vuelve a cargar

    cargados = []
    worker = ruba_service.RubaLoteWorker(ids)

    def sincronizar(incidente_id, on_progreso=None, navegador=None):
        cargados.append(incidente_id)
        worker.cancelar()  # se pide cancelar durante el primer parte: termina ese y corta
        return {"ruba_id_remoto": "1", "url_final": "", "advertencias": []}

    monkeypatch.setattr(ruba_service, "sincronizar_incidente", sincronizar)
    omitidos, resultado = [], []
    worker.item_omitido.connect(lambda i, n, motivo: omitidos.append(motivo))
    worker.terminado.connect(lambda *r: resultado.append(r))
    worker.run()  # en este hilo: sin QThread para el test
    assert omitidos == ["ya estaba cargado en RUBA"]
    assert cargados == [ids[1]]
    assert resultado == [(1, 0, 1, 1)]  # ok, errores, omitidos, sin procesar


def test_guardar_e_imprimir_y_limpiar(entorno, padron):
    ventana, avisos, _ = entorno
    _cargar(ventana, padron)
    ventana.boton_guardar_local.click()
    assert avisos[-1][0] == "info" and "PCS-" in avisos[-1][1] and "PCD2-" in avisos[-1][1]
    with db.get_session() as session:
        incidente = session.query(Incidente).one()
        assert incidente.estado_ruba == EstadoRuba.PENDIENTE.value  # no se envió a RUBA
    assert not ventana.findChildren(DialogoLoteRuba)

    _cargar(ventana, padron, calle="San Martín 100")
    ventana.panel_damnificados.tarjeta_civiles.set_activa(True)
    ventana.boton_limpiar.click()
    assert ventana.entry_calle.text() == "" and not ventana.panel_damnificados.tarjeta_civiles.activa()
    assert ventana.panel_dotaciones.unidades() == []
    assert ventana.combo_tipo.currentIndex() == 0 and ventana.campo_hora_salida.value() is None
    assert ventana.entry_localidad.text() == "Adelia María"
