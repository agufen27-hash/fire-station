import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.ui import personnel_window  # noqa: E402
from app.ui.personnel_window import BotonSyncCloud  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _esperar_fin(boton, segundos=5.0):
    import time
    limite = time.monotonic() + segundos
    while boton._worker is not None and time.monotonic() < limite:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    assert boton._worker is None, "el worker no terminó"


@pytest.mark.parametrize("rol, visible", [(None, False), ("BOMBERO", False), ("JEFATURA", False), ("ADMIN", True)])
def test_visible_solo_para_admin(qapp, rol, visible):
    padre = QWidget()
    boton = BotonSyncCloud(padre)
    padre.show()
    boton.set_rol(rol)
    assert boton.isVisible() is visible
    assert boton.isEnabled() is visible


def _correr(qapp, monkeypatch, bridge_mock):
    padre = QWidget()
    boton = BotonSyncCloud(padre)
    padre.show()
    boton.set_rol("ADMIN")
    info, critico = mock.Mock(), mock.Mock()
    monkeypatch.setattr(personnel_window.QMessageBox, "information", info)
    monkeypatch.setattr(personnel_window.QMessageBox, "critical", critico)
    with mock.patch("app.services.supabase_client.SupabaseBridge", return_value=bridge_mock):
        boton.click()
        assert boton.text() == BotonSyncCloud.TEXTO_OCUPADO and not boton.isEnabled()
        _esperar_fin(boton)
    QCoreApplication.processEvents()
    assert boton.text() == BotonSyncCloud.TEXTO and boton.isEnabled()
    return info, critico


def test_exito_muestra_cantidad(qapp, monkeypatch):
    bridge = mock.Mock()
    bridge.sincronizar_padron_completo.return_value = 50
    info, critico = _correr(qapp, monkeypatch, bridge)
    assert "Se sincronizaron 50 bomberos correctamente en la nube." in info.call_args.args[2]
    critico.assert_not_called()


def test_error_muestra_mensaje(qapp, monkeypatch):
    bridge = mock.Mock()
    bridge.sincronizar_padron_completo.side_effect = RuntimeError("Supabase respondió 401")
    info, critico = _correr(qapp, monkeypatch, bridge)
    assert "Supabase respondió 401" in critico.call_args.args[2]
    info.assert_not_called()
