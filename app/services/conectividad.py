"""
Chequeo de conectividad a internet, para el aviso de "hay salidas
pendientes de sincronizar con RUBA" al arrancar la app (ver
app/ui/main_window.py). Corre en un QThread aparte -- igual que el
sincronizador de RUBA (Fase 3) -- para no bloquear el arranque de la UI con
un intento de conexión de red que puede tardar hasta `TIMEOUT_SEG`.
"""

from __future__ import annotations

import socket
from typing import Tuple

from PySide6.QtCore import QObject, QThread, Signal

TIMEOUT_SEG = 2.0
# 8.8.8.8:53 (DNS público de Google) se usa solo por su altísima
# disponibilidad para chequear "¿hay salida a internet?" -- no tiene
# relación con RUBA ni con ningún dato del cuartel.
HOST_CHEQUEO = "8.8.8.8"
PUERTO_CHEQUEO = 53


def hay_conexion_internet() -> bool:
    try:
        with socket.create_connection((HOST_CHEQUEO, PUERTO_CHEQUEO), timeout=TIMEOUT_SEG):
            return True
    except OSError:
        return False


class ConectividadWorker(QObject):
    resultado = Signal(bool)
    terminado = Signal()

    def run(self) -> None:
        try:
            self.resultado.emit(hay_conexion_internet())
        finally:
            self.terminado.emit()


def lanzar_chequeo_conectividad() -> Tuple[QThread, ConectividadWorker]:
    hilo = QThread()
    worker = ConectividadWorker()
    worker.moveToThread(hilo)

    hilo.started.connect(worker.run)
    worker.terminado.connect(hilo.quit)
    worker.terminado.connect(worker.deleteLater)
    hilo.finished.connect(hilo.deleteLater)

    hilo.start()
    return hilo, worker
