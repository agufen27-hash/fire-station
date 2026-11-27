"""Clave maestra de fábrica (5903) y su reemplazo al primer inicio.

Los archivos de configuración se redirigen a tmp_path: nunca se toca el
data/config.json real."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from app.core import security
from app.ui import pin_dialogs


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Ruta del data/config.json simulado (sin app_config con clave)."""
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({"supabase": {"url": "x"}}), encoding="utf-8")
    monkeypatch.setattr(security, "_ruta_config_escribible", lambda: ruta)
    monkeypatch.setattr(security, "get_resource_path", lambda rel: tmp_path / "no_existe.json")
    monkeypatch.setitem(security._intentos_maestra, "fallidos", 0)
    monkeypatch.setitem(security._intentos_maestra, "bloqueado_hasta", 0.0)
    return ruta


def test_instalacion_nueva_acepta_5903(config):
    assert security.clave_maestra_es_default()
    assert security.verificar_clave_maestra("5903")
    assert not security.verificar_clave_maestra("1234")


def test_clave_propia_reemplaza_a_la_de_fabrica(config):
    security.establecer_clave_maestra("cuartel-2026")
    assert not security.clave_maestra_es_default()
    assert security.verificar_clave_maestra("cuartel-2026")
    assert not security.verificar_clave_maestra("5903")
    # El resto del archivo se conserva.
    datos = json.loads(config.read_text(encoding="utf-8"))
    assert datos["supabase"] == {"url": "x"}
    assert security.es_hash(datos["seguridad"]["clave_maestra_hash"])


@pytest.mark.parametrize("clave", ["", "corta", "5903"])
def test_clave_nueva_invalida(config, clave):
    with pytest.raises(ValueError):
        security.establecer_clave_maestra(clave)
    assert security.clave_maestra_es_default()


def test_bloqueo_tras_intentos_fallidos_tambien_con_la_de_fabrica(config):
    for _ in range(security.MAX_INTENTOS_MAESTRA):
        assert not security.verificar_clave_maestra("mal")
    assert not security.verificar_clave_maestra("5903")  # bloqueada aunque sea la correcta


def test_dialogo_inicial_guarda_la_clave(qapp, config):
    d = pin_dialogs.DialogoClaveMaestraInicial()
    d.entry_nueva.setText("cuartel-2026")
    d.entry_repetir.setText("otra-cosa")
    d._confirmar()
    assert d.result() != QDialog.DialogCode.Accepted
    assert security.clave_maestra_es_default()

    d.entry_nueva.setText("cuartel-2026")
    d.entry_repetir.setText("cuartel-2026")
    d._confirmar()
    assert d.result() == QDialog.DialogCode.Accepted
    assert security.verificar_clave_maestra("cuartel-2026")


def test_inicio_no_pregunta_si_ya_hay_clave_propia(qapp, config, monkeypatch):
    abiertos = []
    monkeypatch.setattr(pin_dialogs.DialogoClaveMaestraInicial, "exec",
                        lambda self: abiertos.append(self) or QDialog.DialogCode.Rejected)
    assert not pin_dialogs.pedir_clave_maestra_si_es_default()
    assert len(abiertos) == 1  # de fábrica: pregunta ("Más tarde")

    security.establecer_clave_maestra("cuartel-2026")
    assert not pin_dialogs.pedir_clave_maestra_si_es_default()
    assert len(abiertos) == 1  # ya no vuelve a preguntar
