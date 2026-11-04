"""
QThread genérico para las llamadas a Supabase del Libro de Guardia: corre una
función bloqueante (app/services/libro_guardia_service.py) fuera del hilo de
la UI y devuelve el resultado o el error por señal.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class TareaLibroGuardia(QThread):
    terminado = Signal(object)   # lo que devolvió la función
    error = Signal(str)          # texto listo para mostrar

    def __init__(self, funcion: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._funcion = funcion

    def run(self) -> None:
        try:
            resultado = self._funcion()
        except Exception as exc:  # noqa: BLE001 -- todo error va a la UI
            from app.services.libro_guardia_service import LibroGuardiaError

            if not isinstance(exc, LibroGuardiaError):
                log.exception("Falló una operación del Libro de Guardia")
            self.error.emit(str(exc) or exc.__class__.__name__)
            return
        self.terminado.emit(resultado)
