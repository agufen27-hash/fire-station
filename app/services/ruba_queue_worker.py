"""
Cola de sincronización con RUBA en segundo plano, resistente a cortes de red.

La UI solo ENCOLA (estado EN_COLA) y sigue trabajando: un QThread con un
`RubaColaWorker` toma los partes de a uno, en orden de llegada, reusando el
mismo navegador mientras haya trabajo (NavegadorLote) y cerrándolo cuando la
cola se vacía. Cada parte pasa por:

  EN_COLA -> SINCRONIZANDO -> SINCRONIZADO
                           -> ERROR_REINTENTO  (caída de red / timeout de carga)
                           -> ERROR            (datos faltantes o rechazo de RUBA)

Antes de abrir Chromium se corre el Semáforo Pre-RUBA
(app/services/ruba_validator.py): un parte incompleto pasa a ERROR al
instante, sin gastar una sesión de navegador.

Reintentos: un ERROR_REINTENTO guarda `proximo_reintento_ruba` con retroceso
exponencial (1, 2, 4, 8, 16 y 30 min; tope MAX_REINTENTOS) y el worker lo
vuelve a encolar cuando vence Y hay internet. Si el monitor de red de la UI
detecta que la conexión volvió, `reintentar_ahora()` adelanta los vencimientos.
Los estados viven en la base: si la app se cierra, al abrirla de nuevo la
cola se retoma (app/db.py devuelve los SINCRONIZANDO huérfanos a EN_COLA).

Toda la comunicación con la UI es por señales (conexión encolada al hilo de
Qt): nunca se toca un widget desde acá.
"""

from __future__ import annotations

import logging
import queue
import socket
import traceback
import urllib.error
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Set, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from app.db import get_session
from app.models import ESTADOS_RUBA_EN_PROCESO, EstadoRuba, Incidente
from app.services import ruba_service
from app.services.conectividad import hay_conexion_internet
from app.services.ruba_automation import EventoProgreso, NavegadorNoDisponibleError, RubaAutomationError
from app.services.ruba_validator import validar_incidente_para_ruba

log = logging.getLogger(__name__)

# Retroceso exponencial entre reintentos automáticos (minutos).
ESPERAS_REINTENTO_MIN = (1, 2, 4, 8, 16, 30)
MAX_REINTENTOS = len(ESPERAS_REINTENTO_MIN)
# Cada cuánto el worker ocioso revisa reintentos vencidos (y la red).
INTERVALO_REVISION_SEG = 15
ESPERA_COLA_SEG = 1.0

_MARCAS_RED = (
    "net::err_", "err_internet_disconnected", "err_name_not_resolved", "err_connection",
    "err_network", "err_timed_out", "err_address_unreachable", "timeout", "timed out",
    "target page, context or browser has been closed", "browser has been closed",
    "connection refused", "connection reset",
)


def espera_reintento(reintento: int) -> timedelta:
    """Espera antes del reintento N (1 = primer reintento)."""
    indice = min(max(reintento, 1), len(ESPERAS_REINTENTO_MIN)) - 1
    return timedelta(minutes=ESPERAS_REINTENTO_MIN[indice])


def es_error_de_red(e: BaseException) -> bool:
    """¿La falla es de conexión (vale reintentar sola) y no de datos?
    Recorre la cadena de causas: RubaAutomationError envuelve el error real
    de Playwright (`raise ... from e`)."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    vistos: Set[int] = set()
    actual: Optional[BaseException] = e
    while actual is not None and id(actual) not in vistos:
        vistos.add(id(actual))
        if isinstance(actual, (PlaywrightTimeoutError, socket.timeout, ConnectionError, urllib.error.URLError)):
            return True
        if isinstance(actual, OSError) and not isinstance(actual, FileNotFoundError):
            return True
        texto = (actual.detalle if isinstance(actual, RubaAutomationError) else str(actual)).lower()
        if any(marca in texto for marca in _MARCAS_RED):
            return True
        actual = actual.__cause__ or actual.__context__
    return False


def _numero_parte(incidente_id: int) -> str:
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        return incidente.numero_parte if incidente else f"#{incidente_id}"


def _set_estado(incidente_id: int, estado: str, **campos) -> None:
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            return
        incidente.estado_ruba = estado
        for nombre, valor in campos.items():
            setattr(incidente, nombre, valor)
        incidente.actualizado_en = datetime.now()


def ids_para_retomar() -> List[int]:
    """EN_COLA que quedaron en la base (app cerrada con trabajo pendiente)."""
    with get_session() as session:
        return [i for (i,) in session.query(Incidente.id)
                .filter(Incidente.estado_ruba == EstadoRuba.EN_COLA.value)
                .order_by(Incidente.actualizado_en, Incidente.id)]


def hay_reintentos_pendientes() -> bool:
    with get_session() as session:
        return session.query(Incidente.id).filter(
            Incidente.estado_ruba == EstadoRuba.ERROR_REINTENTO.value).first() is not None


def reintentos_vencidos(ahora: Optional[datetime] = None) -> List[int]:
    ahora = ahora or datetime.now()
    with get_session() as session:
        return [i for (i,) in session.query(Incidente.id).filter(
            Incidente.estado_ruba == EstadoRuba.ERROR_REINTENTO.value,
            (Incidente.proximo_reintento_ruba.is_(None)) | (Incidente.proximo_reintento_ruba <= ahora),
        ).order_by(Incidente.proximo_reintento_ruba, Incidente.id)]


def adelantar_reintentos() -> int:
    """La red volvió: todos los ERROR_REINTENTO vencen ya. Devuelve cuántos."""
    ahora = datetime.now()
    with get_session() as session:
        filas = session.query(Incidente).filter(Incidente.estado_ruba == EstadoRuba.ERROR_REINTENTO.value).all()
        for incidente in filas:
            incidente.proximo_reintento_ruba = ahora
        return len(filas)


class RubaColaWorker(QObject):
    """Vive en un QThread propio mientras haya trabajo (cola o reintentos
    pendientes); cuando no queda nada, termina y el hilo se libera."""

    encolado = Signal(int, str)                    # incidente_id, numero_parte
    iniciado = Signal(int, str)                    # empezó a cargarse (SINCRONIZANDO)
    detalle = Signal(int, str)                     # "Parte N° X: Cargando datos generales…"
    sincronizado = Signal(int, str, str)           # incidente_id, numero_parte, ruba_id_remoto
    advertencia = Signal(int, str, str)            # incidente_id, numero_parte, aviso
    error = Signal(int, str, str, str)             # incidente_id, numero_parte, mensaje, captura (ERROR definitivo)
    reintento_programado = Signal(int, str, int, str)  # incidente_id, numero_parte, segundos, motivo
    navegador_faltante = Signal(str)               # sin Chrome/Edge/Chromium: instrucciones
    estados_cambiados = Signal()                   # algo cambió en la base: refrescar contadores
    terminado = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._cola: "queue.Queue[int]" = queue.Queue()
        self._en_cola: Set[int] = set()
        self._detener = False
        self.incidente_actual: Optional[int] = None

    # -- API thread-safe (se llama desde la UI) ----------------------------------

    def encolar(self, incidente_ids: Iterable[int], reiniciar_reintentos: bool = True) -> List[int]:
        """Pone en cola (EN_COLA) los partes cerrados que no estén ya en RUBA
        ni en la cola. Un encolado del operador arranca los reintentos de
        cero; uno automático (reintento vencido / retomar) los conserva para
        el retroceso exponencial. Devuelve los ids encolados."""
        encolados: List[int] = []
        with get_session() as session:
            for incidente_id in incidente_ids:
                if incidente_id in self._en_cola or incidente_id == self.incidente_actual:
                    continue
                incidente = session.get(Incidente, incidente_id)
                if incidente is None or incidente.en_curso or incidente.estado_ruba == EstadoRuba.SINCRONIZADO.value:
                    continue
                incidente.estado_ruba = EstadoRuba.EN_COLA.value
                incidente.proximo_reintento_ruba = None
                if reiniciar_reintentos:
                    incidente.reintentos_ruba = 0
                incidente.actualizado_en = datetime.now()
                encolados.append(incidente_id)
        for incidente_id in encolados:
            self._en_cola.add(incidente_id)
            self._cola.put(incidente_id)
        return encolados

    def quitar(self, incidente_id: int) -> bool:
        """Saca de la cola un parte que todavía no empezó (para editarlo).
        False si se está cargando ahora mismo."""
        if incidente_id == self.incidente_actual:
            return False
        if incidente_id in self._en_cola:
            self._en_cola.discard(incidente_id)  # al salir de la cola se descarta
            _set_estado(incidente_id, EstadoRuba.NO_SINCRONIZADO.value)
        return True

    def contiene(self, incidente_id: int) -> bool:
        return incidente_id == self.incidente_actual or incidente_id in self._en_cola

    @property
    def pendientes(self) -> int:
        return len(self._en_cola) + (1 if self.incidente_actual is not None else 0)

    def detener(self) -> None:
        """Termina al finalizar el parte en curso (los demás quedan EN_COLA en
        la base y se retoman la próxima vez)."""
        self._detener = True

    # -- Hilo de fondo ---------------------------------------------------------------

    def run(self) -> None:
        proxima_revision = datetime.min
        try:
            with ruba_service.NavegadorLote() as navegador:
                while not self._detener:
                    try:
                        incidente_id = self._cola.get(timeout=ESPERA_COLA_SEG)
                    except queue.Empty:
                        navegador.cerrar()  # sin trabajo: se libera Chromium
                        if datetime.now() >= proxima_revision:
                            proxima_revision = datetime.now() + timedelta(seconds=INTERVALO_REVISION_SEG)
                            if self._encolar_reintentos_vencidos():
                                continue
                            if not hay_reintentos_pendientes():
                                break  # nada en cola ni por reintentar: el hilo termina
                        continue
                    if incidente_id not in self._en_cola:
                        continue  # lo sacaron de la cola (se va a editar)
                    self._en_cola.discard(incidente_id)
                    self.incidente_actual = incidente_id
                    try:
                        self._procesar(incidente_id, navegador)
                    finally:
                        self.incidente_actual = None
                        self.estados_cambiados.emit()
        except Exception as e:  # noqa: BLE001 - nunca tirar hacia afuera del hilo de fondo
            log.exception("RUBA: la cola se interrumpió")
            self.detalle.emit(-1, f"La cola de RUBA se interrumpió: {type(e).__name__}: {e}")
        finally:
            self.terminado.emit()

    def _encolar_reintentos_vencidos(self) -> bool:
        vencidos = [i for i in reintentos_vencidos() if i not in self._en_cola]
        if not vencidos:
            return False
        if not hay_conexion_internet():
            log.info("RUBA: %s reintento(s) vencido(s) pero sigue sin internet.", len(vencidos))
            return False
        encolados = self.encolar(vencidos, reiniciar_reintentos=False)
        for incidente_id in encolados:
            self.encolado.emit(incidente_id, _numero_parte(incidente_id))
        if encolados:
            self.estados_cambiados.emit()
        return bool(encolados)

    def _procesar(self, incidente_id: int, navegador: "ruba_service.NavegadorLote") -> None:
        numero = _numero_parte(incidente_id)
        prefijo = f"Parte N° {numero}: "

        # Semáforo Pre-RUBA: sin datos completos no se abre Chromium.
        semaforo = validar_incidente_para_ruba(incidente_id)
        if not semaforo.listo:
            mensaje = "Faltan datos para RUBA: " + "; ".join(semaforo.faltantes)
            self._marcar_error_definitivo(incidente_id, numero, mensaje, "")
            return

        # Sin internet no tiene sentido abrir el navegador: directo a reintento.
        if not hay_conexion_internet():
            self._programar_reintento(incidente_id, numero, "Sin conexión a internet.")
            return

        _set_estado(incidente_id, EstadoRuba.SINCRONIZANDO.value)
        self.iniciado.emit(incidente_id, numero)
        self.estados_cambiados.emit()

        def progreso(evento: EventoProgreso) -> None:
            if evento.estado == "inicio":
                self.detalle.emit(incidente_id, prefijo + ruba_service.TEXTO_PASO.get(evento.paso.name, evento.mensaje))
            elif evento.estado in ("omitido", "aviso"):
                self.detalle.emit(incidente_id, prefijo + evento.mensaje)

        try:
            resultado = ruba_service.sincronizar_incidente(incidente_id, on_progreso=progreso, navegador=navegador)
        except NavegadorNoDisponibleError as e:
            # No es culpa del parte: vuelve a NO_SINCRONIZADO y se avisa cómo resolverlo.
            _set_estado(incidente_id, EstadoRuba.NO_SINCRONIZADO.value,
                        ultimo_error_ruba="No hay un navegador disponible para RUBA.")
            self.navegador_faltante.emit(str(e))
            self._vaciar_cola(EstadoRuba.NO_SINCRONIZADO.value)
            return
        except Exception as e:  # noqa: BLE001 - la falla de un parte no corta la cola
            mensaje, captura = ruba_service._describir_error(e)
            navegador.cerrar()  # tras una falla se arranca limpio con el próximo
            if es_error_de_red(e):
                self._programar_reintento(incidente_id, numero, mensaje, traceback.format_exc(limit=5))
            else:
                self._marcar_error_definitivo(incidente_id, numero, mensaje, captura,
                                              traceback.format_exc(limit=5))
            return

        try:
            ruba_service._marcar_sincronizado(incidente_id, resultado.get("ruba_id_remoto"))
            _set_estado(incidente_id, EstadoRuba.SINCRONIZADO.value, reintentos_ruba=0,
                        ultimo_error_ruba=None, proximo_reintento_ruba=None)
        except Exception as e:  # noqa: BLE001
            self._marcar_error_definitivo(incidente_id, numero,
                                          f"Se cargó en RUBA pero no se pudo guardar el resultado local: {e}", "")
            return
        self.detalle.emit(incidente_id, prefijo + "Sincronizado con éxito.")
        self.sincronizado.emit(incidente_id, numero, resultado.get("ruba_id_remoto") or "")
        for aviso in filter(None, (resultado.get("aviso_recreado"),)):
            self.advertencia.emit(incidente_id, numero, aviso)

    def _programar_reintento(self, incidente_id: int, numero: str, motivo: str, detalle: str = "") -> None:
        with get_session() as session:
            incidente = session.get(Incidente, incidente_id)
            if incidente is None:
                return
            reintento = (incidente.reintentos_ruba or 0) + 1
            ahora = datetime.now()
            if reintento > MAX_REINTENTOS:
                incidente.estado_ruba = EstadoRuba.ERROR.value
                incidente.ultimo_error_ruba = f"Sin conexión con RUBA tras {MAX_REINTENTOS} reintentos: {motivo}"[:500]
                incidente.ruba_error_log = f"{incidente.ultimo_error_ruba}\n{detalle}"[:4000]
                incidente.proximo_reintento_ruba = None
                incidente.actualizado_en = ahora
                definitivo = incidente.ultimo_error_ruba
            else:
                espera = espera_reintento(reintento)
                incidente.estado_ruba = EstadoRuba.ERROR_REINTENTO.value
                incidente.reintentos_ruba = reintento
                incidente.ultimo_error_ruba = motivo[:500]
                incidente.ruba_error_log = f"{motivo}\n{detalle}"[:4000]
                incidente.proximo_reintento_ruba = ahora + espera
                incidente.actualizado_en = ahora
                definitivo = None
        if definitivo:
            self.error.emit(incidente_id, numero, definitivo, "")
        else:
            self.reintento_programado.emit(incidente_id, numero, int(espera.total_seconds()), motivo)

    def _marcar_error_definitivo(self, incidente_id: int, numero: str, mensaje: str, captura: str,
                                 detalle: str = "") -> None:
        try:
            _set_estado(incidente_id, EstadoRuba.ERROR.value, ultimo_error_ruba=mensaje[:500],
                        ruba_error_log=f"{mensaje}\n{detalle}"[:4000], proximo_reintento_ruba=None)
        except Exception:  # noqa: BLE001 - igual se avisa por señal
            log.exception("RUBA: no se pudo registrar el error del parte %s", numero)
        self.error.emit(incidente_id, numero, mensaje, captura)

    def _vaciar_cola(self, estado: str) -> None:
        while True:
            try:
                incidente_id = self._cola.get_nowait()
            except queue.Empty:
                break
            if incidente_id in self._en_cola:
                self._en_cola.discard(incidente_id)
                _set_estado(incidente_id, estado)


class ColaRuba(QObject):
    """Dueña del worker y de su QThread (los arranca cuando hace falta y los
    suelta cuando el worker termina). MainWindow conserva esta instancia: sus
    señales re-emiten las del worker de turno."""

    encolado = Signal(int, str)
    iniciado = Signal(int, str)
    detalle = Signal(int, str)
    sincronizado = Signal(int, str, str)
    advertencia = Signal(int, str, str)
    error = Signal(int, str, str, str)
    reintento_programado = Signal(int, str, int, str)
    navegador_faltante = Signal(str)
    estados_cambiados = Signal()
    inactiva = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._hilo: Optional[QThread] = None
        self._worker: Optional[RubaColaWorker] = None
        self._deteniendo = False  # cierre de la app: no se arranca otro worker

    # -- Estado -----------------------------------------------------------------

    @property
    def activa(self) -> bool:
        return self._worker is not None

    @property
    def cargando(self) -> Optional[int]:
        """incidente_id que se está cargando ahora mismo (o None)."""
        return self._worker.incidente_actual if self._worker else None

    @property
    def pendientes(self) -> int:
        return self._worker.pendientes if self._worker else 0

    def contiene(self, incidente_id: int) -> bool:
        return bool(self._worker and self._worker.contiene(incidente_id))

    # -- Acciones ----------------------------------------------------------------

    def encolar(self, incidente_ids: Iterable[int], reiniciar_reintentos: bool = True) -> List[int]:
        worker = self._asegurar_worker()
        encolados = worker.encolar(list(incidente_ids), reiniciar_reintentos)
        for incidente_id in encolados:
            self.encolado.emit(incidente_id, _numero_parte(incidente_id))
        if encolados:
            self.estados_cambiados.emit()
        return encolados

    def retomar(self) -> Tuple[int, bool]:
        """Al abrir la app: re-encola los EN_COLA y arranca el worker si hay
        reintentos pendientes. Devuelve (retomados, hay_reintentos)."""
        retomar = ids_para_retomar()
        reintentos = hay_reintentos_pendientes()
        if retomar:
            self.encolar(retomar, reiniciar_reintentos=False)
        elif reintentos:
            self._asegurar_worker()
        return len(retomar), reintentos

    def reintentar_ahora(self) -> int:
        """Monitor de red: volvió la conexión -> los reintentos vencen ya."""
        adelantados = adelantar_reintentos()
        if adelantados:
            self._asegurar_worker()
        return adelantados

    def quitar(self, incidente_id: int) -> bool:
        return self._worker.quitar(incidente_id) if self._worker else True

    def detener(self, espera_ms: int = 0) -> bool:
        """Pide terminar (al finalizar el parte en curso). Con `espera_ms`
        espera al hilo; devuelve True si quedó terminado."""
        self._deteniendo = True
        if self._worker is None or self._hilo is None:
            return True
        self._worker.detener()
        return self._hilo.wait(espera_ms) if espera_ms else False

    # -- Interno -----------------------------------------------------------------

    def _asegurar_worker(self) -> RubaColaWorker:
        if self._worker is not None:
            return self._worker
        self._deteniendo = False
        hilo = QThread()
        worker = RubaColaWorker()
        worker.moveToThread(hilo)
        for nombre in ("encolado", "iniciado", "detalle", "sincronizado", "advertencia", "error",
                       "reintento_programado", "navegador_faltante", "estados_cambiados"):
            getattr(worker, nombre).connect(getattr(self, nombre))
        hilo.started.connect(worker.run)
        worker.terminado.connect(hilo.quit)
        worker.terminado.connect(worker.deleteLater)
        hilo.finished.connect(self._on_hilo_terminado)
        hilo.finished.connect(hilo.deleteLater)
        self._hilo, self._worker = hilo, worker
        hilo.start()
        return worker

    def _on_hilo_terminado(self) -> None:
        worker = self._worker
        self._hilo, self._worker = None, None
        # Algo encolado justo mientras el worker terminaba: arrancar otro.
        if worker is not None and worker._en_cola and not self._deteniendo:
            pendientes = list(worker._en_cola)
            self._asegurar_worker().encolar(pendientes, reiniciar_reintentos=False)
            return
        self.inactiva.emit()
        self.estados_cambiados.emit()
