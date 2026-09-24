import sys
from pathlib import Path

# Permite `import app...` corriendo pytest desde cualquier carpeta.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _sin_clima_automatico(monkeypatch):
    """Ningún test consulta Open-Meteo de verdad (ni escribe su cache)."""
    from app.ui.widgets import weather_widget

    monkeypatch.setattr(weather_widget.WeatherWidget, "AUTOMATICO_POR_DEFECTO", False)
    monkeypatch.setattr(weather_widget.WeatherWidget, "_guardar_cache", lambda self, datos: None)


@pytest.fixture(autouse=True)
def _cerrar_ventanas_sobrantes():
    """Destruye las ventanas que un test deja vivas: si se acumulan, cada
    cambio de tema posterior tiene que repulir cientos de widgets."""
    yield
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    for ventana in app.topLevelWidgets():
        ventana.close()
        ventana.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
