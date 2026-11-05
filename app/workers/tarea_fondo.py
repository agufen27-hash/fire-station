"""
QThread genérico: corre una función bloqueante (archivo, red) fuera del hilo
de la UI y devuelve el resultado o el error por señal. Lo usan la
exportación de zonas a KMZ y el cliente de Google Earth Engine.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class TareaFondo(QThread):
    terminado = Signal(object)   # lo que devolvió la función
    error = Signal(str)          # texto listo para mostrar

    def __init__(self, funcion: Callable[[], Any], descripcion: str = "tarea en segundo plano",
                 parent=None) -> None:
        super().__init__(parent)
        self._funcion = funcion
        self._descripcion = descripcion

    def run(self) -> None:
        try:
            resultado = self._funcion()
        except Exception as exc:  # noqa: BLE001 -- todo error va a la UI
            log.exception("Falló: %s", self._descripcion)
            self.error.emit(str(exc) or exc.__class__.__name__)
            return
        self.terminado.emit(resultado)
