"""
QThread que sube el padrón local a Supabase (`personal_cloud`) sin
bloquear la UI. Ver app/services/supabase_client.py.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class SyncSupabaseWorker(QThread):
    terminado = Signal(int)   # filas enviadas (insertadas / actualizadas)
    error = Signal(str)

    def run(self) -> None:
        # Import diferido: si `requests` o las credenciales fallan, el error
        # viaja por la señal en vez de romper el arranque de la ventana.
        try:
            from app.services.supabase_client import SupabaseBridge

            enviadas = SupabaseBridge().sincronizar_padron_completo()
        except Exception as exc:  # noqa: BLE001 -- todo error va a la UI
            log.exception("Falló la sincronización con Supabase")
            self.error.emit(str(exc) or exc.__class__.__name__)
            return
        self.terminado.emit(enviadas)
