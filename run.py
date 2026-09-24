"""
Punto de entrada de Fire Station (PySide6 + SQLite).

Ejecutar con:  python run.py
"""

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.db import init_db
from app.paths import ICONO_APP, asegurar_config_inicial, configurar_entorno_playwright, ruta_recurso_existente
from app.services.cartografia import asegurar_calibracion_en_config
from app.ui.main_window import MainWindow
from app.ui.theme import aplicar_tema


def _identificar_app_en_windows() -> None:
    """Sin un AppUserModelID propio, Windows muestra en la barra de tareas el
    ícono del proceso (python.exe al correr desde el código) en vez del de
    la ventana."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BVAdeliaMaria.FireStation")
    except (AttributeError, OSError):
        pass


def main() -> None:
    asegurar_config_inicial()
    asegurar_calibracion_en_config()
    configurar_entorno_playwright()
    init_db()

    _identificar_app_en_windows()
    app = QApplication(sys.argv)
    icono = ruta_recurso_existente(ICONO_APP)
    if icono is not None:
        app.setWindowIcon(QIcon(str(icono)))  # también lo heredan los diálogos
    aplicar_tema(app)

    ventana = MainWindow()
    ventana.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
