"""Seguridad local: reseteo de PIN con clave maestra en caliente (cierre de
planilla y firma del Jefe de Dotación), columna `rol` con migración suave,
RBAC (Bombero / Jefatura / Admin) y autorización de cambios de rol con la
cuenta de RUBA o la clave maestra. Base SQLite temporal."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QDialog
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import app.db as db
from app.core import roles
from app.core import security
from app.models import Personal
from app.services import personal_service
from app.services.personal_service import (
    asignar_roles,
    autorizar_cambio_roles,
    personal_con_roles,
    rol_de,
    verificar_pin_personal,
)

CLAVE_MAESTRA = "maestra-de-prueba"
PIN_TEMPORAL = "4321"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()
    # Clave maestra de prueba (sin tocar data/config.json) y contador de intentos limpio.
    monkeypatch.setattr(personal_service, "verificar_clave_maestra", lambda c: c == CLAVE_MAESTRA)
    monkeypatch.setitem(security._intentos_maestra, "fallidos", 0)
    monkeypatch.setitem(security._intentos_maestra, "bloqueado_hasta", 0.0)
    # Sin cuenta de RUBA guardada salvo que el test la inyecte.
    monkeypatch.setattr(personal_service, "_credenciales_ruba", lambda: {})
    return engine


@pytest.fixture
def personas(base):
    """(ana, beto, ceci_inactiva) ids, con PIN de fábrica."""
    with db.get_session() as s:
        a = Personal(nombre="Ana", apellido="Zzprueba", dni="90000001")
        b = Personal(nombre="Beto", apellido="Aaprueba", dni="90000002")
        c = Personal(nombre="Ceci", apellido="Bbprueba", dni="90000003", activo=False)
        s.add_all([a, b, c])
        s.flush()
        return a.id, b.id, c.id


@pytest.fixture
def reset_simulado(monkeypatch):
    """DialogoResetPinMaestra.exec sin bloquear: completa clave + PIN temporal y confirma."""
    from app.ui.pin_dialogs import DialogoResetPinMaestra

    abiertos = []

    def exec_simulado(self):
        abiertos.append(self)
        self.entry_maestra.setText(CLAVE_MAESTRA)
        self.entry_temporal.setText(PIN_TEMPORAL)
        self.entry_repetir.setText(PIN_TEMPORAL)
        self._confirmar()
        return self.result()

    monkeypatch.setattr(DialogoResetPinMaestra, "exec", exec_simulado)
    return abiertos


# ---------------------------------------------------------------------------
# DialogoResetPinMaestra
# ---------------------------------------------------------------------------

def test_reset_sin_preseleccion_combo_libre_y_exige_elegir(qapp, personas):
    from app.ui.pin_dialogs import DialogoResetPinMaestra

    d = DialogoResetPinMaestra()
    assert d.combo_bombero.isEnabled() and d.personal_id is None
    d.entry_maestra.setText(CLAVE_MAESTRA)
    d._confirmar()
    assert d.result() != QDialog.DialogCode.Accepted
    assert "Elegí" in d.label_error.text()


def test_reset_con_preseleccion_bloquea_el_bombero(qapp, personas):
    from app.ui.pin_dialogs import DialogoResetPinMaestra

    _ana, beto, ceci = personas
    d = DialogoResetPinMaestra(beto, "Aaprueba, Beto")
    assert not d.combo_bombero.isEnabled() and d.personal_id == beto
    # Legajo inactivo (fuera del combo de activos): igual queda preseleccionado.
    assert DialogoResetPinMaestra(ceci, "Bbprueba, Ceci").personal_id == ceci


def test_reset_llamada_posicional_existente(qapp, personas):
    from app.ui.pin_dialogs import DialogoResetPinMaestra

    ana, _beto, _ceci = personas
    assert DialogoResetPinMaestra(ana, "Zzprueba, Ana", None).personal_id == ana


def test_reset_clave_maestra_incorrecta_no_cambia_pin(qapp, personas):
    from app.ui.pin_dialogs import DialogoResetPinMaestra

    ana, _beto, _ceci = personas
    d = DialogoResetPinMaestra(ana, "Zzprueba, Ana")
    d.entry_maestra.setText("otra")
    d.entry_temporal.setText(PIN_TEMPORAL)
    d.entry_repetir.setText(PIN_TEMPORAL)
    d._confirmar()
    assert d.result() != QDialog.DialogCode.Accepted
    assert not verificar_pin_personal(ana, PIN_TEMPORAL)


# ---------------------------------------------------------------------------
# Flujo en caliente: cierre de planilla (DialogoAutorPin)
# ---------------------------------------------------------------------------

def test_cierre_planilla_resetea_pin_sin_cerrar_y_firma(qapp, personas, reset_simulado):
    from app.ui.autor_dialog import DialogoAutorPin

    ana, beto, _ceci = personas
    autor = DialogoAutorPin("123/2026", preseleccion=beto)
    autor.show()
    qapp.processEvents()
    autor.entry_pin.setText("9999")
    autor._resetear_pin()

    assert reset_simulado and reset_simulado[0].personal_id == beto
    assert not reset_simulado[0].combo_bombero.isEnabled()
    assert autor.isVisible() and autor.entry_pin.text() == ""
    assert "PIN actualizado" in autor.label_error.text()
    assert autor.label_error.property("tono") == "ok"
    assert autor.focusWidget() is autor.entry_pin
    assert verificar_pin_personal(beto, PIN_TEMPORAL) and not verificar_pin_personal(ana, PIN_TEMPORAL)

    autor.entry_pin.setText(PIN_TEMPORAL)
    autor._validar()
    assert autor.result() == QDialog.DialogCode.Accepted and autor.personal_id == beto


def test_cierre_planilla_sin_responsable_no_abre_reset(qapp, personas, reset_simulado):
    from app.ui.autor_dialog import DialogoAutorPin

    autor = DialogoAutorPin("124/2026")
    autor._resetear_pin()
    assert not reset_simulado
    assert "Elegí primero" in autor.label_error.text()
    assert autor.label_error.property("tono") == "error"


# ---------------------------------------------------------------------------
# Flujo en caliente: firma del Jefe de Dotación (TarjetaUnidad)
# ---------------------------------------------------------------------------

def _tarjeta(personal_id):
    from app.core.catalogos import obtener_padron
    from app.ui.dotaciones_widgets import TarjetaUnidad

    padron = obtener_padron().bomberos
    tarjeta = TarjetaUnidad(1, [], padron, resolver_personal=lambda _id: personal_id)
    return tarjeta, padron[0].id_ruba


def test_firma_jefe_resetea_pin_y_queda_lista_para_firmar(qapp, personas, reset_simulado):
    _ana, beto, _ceci = personas
    tarjeta, id_ruba = _tarjeta(beto)
    tarjeta.selector_jefe.set_id_ruba(id_ruba)
    tarjeta.show()
    qapp.processEvents()
    tarjeta.entry_pin.setText("9999")
    tarjeta._resetear_pin_jefe()

    assert reset_simulado and reset_simulado[0].personal_id == beto
    assert not reset_simulado[0].combo_bombero.isEnabled()
    assert tarjeta.entry_pin.text() == ""
    assert "PIN actualizado" in tarjeta.label_firma.text()
    assert not tarjeta.esta_firmada()
    assert tarjeta.focusWidget() is tarjeta.entry_pin
    assert verificar_pin_personal(beto, PIN_TEMPORAL)


def test_firma_jefe_sin_jefe_no_abre_reset(qapp, personas, reset_simulado):
    tarjeta, _id_ruba = _tarjeta(None)
    tarjeta._resetear_pin_jefe()
    assert not reset_simulado
    assert "Jefe de Dotación" in tarjeta.label_firma.text()


# ---------------------------------------------------------------------------
# Modelo: columna `rol` y migración suave
# ---------------------------------------------------------------------------

def test_rol_default_bombero_y_validacion(personas):
    ana, _beto, _ceci = personas
    assert rol_de(ana) == roles.ROL_BOMBERO
    assert rol_de(None) == roles.ROL_BOMBERO
    with pytest.raises(ValueError):
        Personal(nombre="X", apellido="Y", dni="1", rol="SUPERUSER")
    assert Personal(nombre="X", apellido="Y", dni="2", rol="admin").rol == roles.ROL_ADMIN


def test_migracion_agrega_rol_a_base_existente(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'vieja.db'}", future=True)
    with engine.begin() as c:  # tabla `personal` previa al RBAC, con una fila
        c.execute(text("CREATE TABLE personal (id INTEGER PRIMARY KEY, nombre VARCHAR(80) NOT NULL, "
                       "apellido VARCHAR(80) NOT NULL, dni VARCHAR(15) NOT NULL, activo BOOLEAN NOT NULL)"))
        c.execute(text("INSERT INTO personal (id, nombre, apellido, dni, activo) VALUES (1, 'Vieja', 'Base', '1', 1)"))
    monkeypatch.setattr(db, "engine", engine)
    db._migrar_columnas("personal", db.COLUMNAS_NUEVAS_PERSONAL)
    assert "rol" in {col["name"] for col in inspect(engine).get_columns("personal")}
    with engine.connect() as c:
        assert c.execute(text("SELECT rol FROM personal WHERE id = 1")).scalar() == "BOMBERO"
    db._migrar_columnas("personal", db.COLUMNAS_NUEVAS_PERSONAL)  # idempotente


# ---------------------------------------------------------------------------
# Permisos y servicio de roles
# ---------------------------------------------------------------------------

def test_matriz_de_permisos():
    b, j, a = (roles.permisos_de(r) for r in roles.ROLES)
    assert not (b.editar_padron or b.ver_legajos_ajenos or b.configurar or b.resetear_pin_ajeno)
    assert j.ver_legajos_ajenos and not (j.editar_padron or j.configurar or j.resetear_pin_ajeno)
    assert a.editar_padron and a.ver_legajos_ajenos and a.configurar and a.resetear_pin_ajeno
    assert roles.permisos_de("desconocido") == b and roles.permisos_de(None) == b


def test_asignar_roles_todo_o_nada(personas):
    ana, beto, _ceci = personas
    assert asignar_roles({ana: "ADMIN", beto: "JEFATURA"}) == 2
    assert (rol_de(ana), rol_de(beto)) == ("ADMIN", "JEFATURA")
    with pytest.raises(ValueError):
        asignar_roles({ana: "BOMBERO", beto: "SUPERUSER"})
    assert rol_de(ana) == "ADMIN"  # nada se guardó
    assert asignar_roles({ana: "ADMIN"}) == 0
    activos = {pid for pid, *_ in personal_con_roles()}
    assert ana in activos and personas[2] not in activos


def test_autorizacion_con_clave_maestra_si_no_hay_cuenta_ruba(base):
    assert autorizar_cambio_roles("", CLAVE_MAESTRA)
    assert not autorizar_cambio_roles("", "otra")


def test_autorizacion_con_cuenta_ruba_guardada(base):
    cred = {"usuario": "Cuartel59", "clave": "S3creta!"}
    assert autorizar_cambio_roles("cuartel59 ", "S3creta!", cred)
    assert not autorizar_cambio_roles("cuartel59", "s3creta!", cred)
    assert not autorizar_cambio_roles("otro", "S3creta!", cred)
    # Con cuenta RUBA guardada, la clave maestra ya no alcanza.
    assert not autorizar_cambio_roles("", CLAVE_MAESTRA, cred)


def test_panel_roles_exige_autorizacion_para_guardar(qapp, personas, monkeypatch):
    from app.ui import roles_widgets

    ana, _beto, _ceci = personas
    panel = roles_widgets.PanelUsuariosRoles()
    panel.refrescar()
    fila = next(i for i in range(panel.tabla.rowCount())
                if panel.tabla.cellWidget(i, roles_widgets.COL_ROL).property("personal_id") == ana)
    combo = panel.tabla.cellWidget(fila, roles_widgets.COL_ROL)
    combo.setCurrentIndex(combo.findData("JEFATURA"))
    assert panel.cambios_pendientes() == {ana: "JEFATURA"} and panel.boton_guardar.isEnabled()

    monkeypatch.setattr(roles_widgets.DialogoAutorizacionRoles, "exec", lambda self: QDialog.DialogCode.Rejected)
    assert not panel.guardar() and rol_de(ana) == "BOMBERO"

    def autorizar(self):
        self.entry_clave.setText(CLAVE_MAESTRA)
        self._confirmar()
        return self.result()

    monkeypatch.setattr(roles_widgets.DialogoAutorizacionRoles, "exec", autorizar)
    emitidos = []
    panel.roles_cambiados.connect(lambda: emitidos.append(True))
    assert panel.guardar() and rol_de(ana) == "JEFATURA" and emitidos


def test_dialogo_autorizacion_pide_usuario_ruba_si_hay_cuenta(qapp, base, monkeypatch):
    from app.ui import roles_widgets

    monkeypatch.setattr(personal_service, "_credenciales_ruba", lambda: {"usuario": "c59", "clave": "x1"})
    d = roles_widgets.DialogoAutorizacionRoles()
    assert d.usa_ruba and d.entry_usuario is not None
    d.entry_usuario.setText("c59")
    d.entry_clave.setText("mal")
    d._confirmar()
    assert d.result() != QDialog.DialogCode.Accepted and "RUBA" in d.label_error.text()
    d.entry_clave.setText("x1")
    d._confirmar()
    assert d.result() == QDialog.DialogCode.Accepted


# ---------------------------------------------------------------------------
# Legajo en solo lectura
# ---------------------------------------------------------------------------

def test_legajo_solo_lectura_oculta_alta_baja_edicion_y_reseteo(qapp, personas):
    from app.ui.personnel_window import LegajoPrivadoWidget

    ana, _beto, _ceci = personas
    legajo = LegajoPrivadoWidget()
    legajo.set_permisos(solo_lectura=True, editar_datos=True)
    legajo.mostrar(ana)
    for boton in (legajo.boton_editar, legajo.boton_firmar, legajo.boton_pin,
                  legajo.boton_reset_pin, legajo.boton_agregar_doc):
        assert boton.isHidden()
    assert not legajo.label_solo_lectura.isHidden()

    legajo.set_permisos(solo_lectura=False, editar_datos=False)  # propio, sin ser ADMIN
    assert legajo.boton_editar.isHidden() and not legajo.boton_reset_pin.isHidden()
    legajo.set_permisos(solo_lectura=False, editar_datos=True)   # ADMIN
    assert not legajo.boton_editar.isHidden()


# ---------------------------------------------------------------------------
# RBAC aplicado en MainWindow
# ---------------------------------------------------------------------------

@pytest.fixture
def ventana(qapp, personas, monkeypatch):
    from app.ui import main_window as mw

    avisos = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: avisos.append(a[2]))
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: None)
    v = mw.MainWindow()
    yield v, avisos, mw
    v._timer_sesion_operador.stop()
    v.close()
    QCoreApplication.processEvents()


def test_sin_operador_rige_bombero(ventana):
    v, avisos, _mw = ventana
    assert v._permisos == roles.permisos_de(roles.ROL_BOMBERO)
    assert all(not b.isEnabled() for b in v._botones_abm)
    assert all(not w.isEnabled() for w in v._widgets_config_criticos)
    assert not v._label_config_bloqueada.isHidden()
    assert v._panel_roles.isEnabled()  # roles: autorizado por RUBA / clave maestra, no por rol
    assert v._dialogo_bombero() is False and avisos
    v._guardar_configuracion()
    assert len(avisos) == 2


def test_operador_admin_habilita_todo_y_la_sesion_se_cierra(ventana, personas):
    v, _avisos, _mw = ventana
    ana, _beto, _ceci = personas
    asignar_roles({ana: "ADMIN"})
    v._iniciar_sesion_operador(ana)
    assert v._permisos.configurar and "Administrador" in v._chip_operador.text()
    assert all(b.isEnabled() for b in v._botones_abm)
    assert all(w.isEnabled() for w in v._widgets_config_criticos)
    assert v._timer_sesion_operador.isActive()
    v._cerrar_sesion_operador()
    assert not v._permisos.configurar and v._chip_operador.text() == "👤 Identificarse"


def test_jefatura_abre_legajo_ajeno_en_solo_lectura(ventana, personas):
    v, avisos, mw = ventana
    ana, beto, _ceci = personas
    asignar_roles({ana: "JEFATURA"})
    v._cargar_pagina_documentacion()

    # Sin operador, PIN vacío = PIN incorrecto (comportamiento de siempre).
    v._combo_legajo_bombero.setCurrentIndex(v._combo_legajo_bombero.findData(beto))
    v._intentar_ingresar_legajo()
    assert v._stack_documentacion.currentIndex() == 0 and avisos

    v._iniciar_sesion_operador(ana)
    assert not v._permisos.editar_padron and v._permisos.ver_legajos_ajenos
    v._combo_legajo_bombero.setCurrentIndex(v._combo_legajo_bombero.findData(beto))
    v._intentar_ingresar_legajo()
    assert v._stack_documentacion.currentIndex() == 1
    assert v._legajo_privado.solo_lectura and v._legajo_privado.boton_reset_pin.isHidden()
    assert all(not b.isEnabled() for b in v._botones_abm)

    v._cerrar_sesion_operador()  # el legajo ajeno no queda a la vista
    assert v._stack_documentacion.currentIndex() == 0


def test_bombero_con_su_pin_ve_su_legajo_editable_sin_editar_padron(ventana, personas):
    v, _avisos, _mw = ventana
    _ana, beto, _ceci = personas
    v._cargar_pagina_documentacion()
    v._combo_legajo_bombero.setCurrentIndex(v._combo_legajo_bombero.findData(beto))
    v._entry_legajo_pin.setText(security.PIN_DEFAULT)
    v._intentar_ingresar_legajo()
    assert v._stack_documentacion.currentIndex() == 1
    assert not v._legajo_privado.solo_lectura
    assert not v._legajo_privado.boton_reset_pin.isHidden()
    assert v._legajo_privado.boton_editar.isHidden()
