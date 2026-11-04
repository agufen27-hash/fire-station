"""
Sincronización automática de la cola offline (app/services/cache_offline.py).

Cada INTERVALO_MS revisa la cola local; si hay operaciones pendientes, las
envía a Supabase en un TareaSupabase (QThread), en orden, sin interrumpir al
operador. Sin red se corta y reintenta en el próximo ciclo. `estado_cambiado`
alimenta el chip de la barra superior; `sincronizado` avisa lo enviado para
refrescar la página visible.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from app.services import cache_offline
from app.workers.supabase_tarea import TareaSupabase

log = logging.getLogger(__name__)

INTERVALO_MS = 30 * 1000


class SincronizadorOffline(QObject):
    estado_cambiado = Signal(int, int, bool)        # pendientes, errores, en línea
    sincronizado = Signal(object)                    # cache_offline.ResultadoSincronizacion

    def __init__(self, parent: Optional[QObject] = None, cliente_factory: Optional[Callable] = None,
                 intervalo_ms: int = INTERVALO_MS) -> None:
        super().__init__(parent)
        self._cliente_factory = cliente_factory
        self._tarea: Optional[TareaSupabase] = None
        self._timer = QTimer(self)
        self._timer.setInterval(intervalo_ms)
        self._timer.timeout.connect(self.revisar)

    def iniciar(self) -> None:
        self._timer.start()
        QTimer.singleShot(3000, self.revisar)

    @property
    def ocupado(self) -> bool:
        return self._tarea is not None

    def revisar(self) -> None:
        """Publica el estado y, si hay pendientes, sincroniza."""
        conteo = cache_offline.contar()
        pendientes = conteo.get(cache_offline.ESTADO_PENDIENTE, 0)
        self.estado_cambiado.emit(pendientes, conteo.get(cache_offline.ESTADO_ERROR, 0),
                                  cache_offline.estado().en_linea)
        if pendientes and self._tarea is None:
            self._sincronizar()

    def _sincronizar(self) -> None:
        from app.services.supabase_rest import ClienteSupabaseRest, SupabaseSinConexion

        fabrica = self._cliente_factory or ClienteSupabaseRest

        def trabajo():
            cliente = fabrica()
            return cache_offline.sincronizar(cliente.enviar_operacion,
                                             lambda e: isinstance(e, SupabaseSinConexion))

        tarea = self._tarea = TareaSupabase(trabajo, self)
        tarea.terminado.connect(self._al_terminar)
        tarea.error.connect(lambda msg: log.warning("Sincronización offline: %s", msg))
        tarea.finished.connect(self._liberar)
        tarea.start()

    def _al_terminar(self, resultado: cache_offline.ResultadoSincronizacion) -> None:
        if resultado.enviadas or resultado.rechazadas:
            log.info("Cola offline: %d enviadas, %d rechazadas, %d restantes",
                     resultado.enviadas, resultado.rechazadas, resultado.restantes)
            self.sincronizado.emit(resultado)

    def _liberar(self) -> None:
        if self._tarea is not None:
            self._tarea.deleteLater()
            self._tarea = None
        conteo = cache_offline.contar()
        self.estado_cambiado.emit(conteo.get(cache_offline.ESTADO_PENDIENTE, 0),
                                  conteo.get(cache_offline.ESTADO_ERROR, 0), cache_offline.estado().en_linea)

    def detener(self, espera_ms: int = 5000) -> None:
        self._timer.stop()
        if self._tarea is not None:
            self._tarea.wait(espera_ms)
