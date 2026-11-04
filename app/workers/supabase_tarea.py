"""
QThread genérico para las llamadas a Supabase de la app de escritorio (Libro
de Guardia, Inventario): corre una función bloqueante fuera del hilo de la UI
y devuelve el resultado o el error por señal. La UI nunca se congela aunque
no haya internet.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class TareaSupabase(QThread):
    terminado = Signal(object)   # lo que devolvió la función
    error = Signal(str)          # texto listo para mostrar

    def __init__(self, funcion: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._funcion = funcion

    def run(self) -> None:
        try:
            resultado = self._funcion()
        except Exception as exc:  # noqa: BLE001 -- todo error va a la UI
            from app.services.supabase_rest import SupabaseRechazo

            if not isinstance(exc, SupabaseRechazo):  # un rechazo de negocio no es un fallo
                log.exception("Falló una operación con Supabase")
            self.error.emit(str(exc) or exc.__class__.__name__)
            return
        self.terminado.emit(resultado)
