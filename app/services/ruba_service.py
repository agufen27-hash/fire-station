"""
Sincronización en segundo plano de un `Incidente` de Fire Station hacia el
portal RUBA: corre en un QThread aparte, con Chromium `headless=True` (nunca
abre una ventana), y nunca congela la interfaz.

El flujo pantalla por pantalla vive en `app/services/ruba_automation.py`
(`RubaServiceAutomation`), alimentado con el payload del incidente guardado
(`app/services/ruba_payload.py`). Acá solo se orquesta: payload -> validación
-> automatización -> estado_ruba en la base, informando el avance por señal.

No se inventan datos: si falta algo que RUBA exige, la sincronización falla
con un error claro (`estado_ruba = 'ERROR'`, detalle en `ruba_error_log`). La
única excepción es el denunciante, que tiene un valor por defecto explícito
(pedido del requerimiento) cuando el formulario de guardia no cargó uno.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from app.core.semilla import denunciante_por_defecto
from app.db import get_session
from app.models import EstadoRuba, Incidente
from app.services.ruba_automation import EventoProgreso, RubaAutomationError, RubaServiceAutomation
from app.services.ruba_payload import payload_desde_incidente, validar_payload


# RUBA exige compañía y póliza (label "required"); sin seguro se declara así,
# con el mismo valor que usaba la carga anterior.
SEGURO_POR_DEFECTO: Dict[str, str] = {"compania_seguro": "Sin datos", "numero_poliza": "00000000"}


CAMPOS_DENUNCIANTE = (
    ("nombre", "nombre_solicitante"), ("apellido", "apellido_solicitante"),
    ("dni", "dni_solicitante"), ("telefono", "telefono_solicitante"),
)


def _completar_denunciante(payload: Dict[str, Any]) -> None:
    """Denunciante por defecto (pedido explícito del requerimiento: es la
    única identidad que se rellena sola) desde data/seed_bomberos.json --
    fuera del repo. Sin semilla no se inventa nada: RUBA es un registro
    oficial y el parte tiene que traer su denunciante."""
    general = payload["editar_general"]
    faltantes = [clave for _, clave in CAMPOS_DENUNCIANTE if not general.get(clave)]
    if faltantes:
        por_defecto = denunciante_por_defecto()
        if por_defecto is None:
            raise ValueError(
                "El parte no tiene denunciante completo y no hay uno por defecto configurado "
                f"({', '.join(faltantes)}). Cargalo en el formulario o definí "
                "'denunciante_por_defecto' en data/seed_bomberos.json."
            )
        for campo, clave in CAMPOS_DENUNCIANTE:
            if not general.get(clave):
                general[clave] = por_defecto[campo]
    for clave, valor in SEGURO_POR_DEFECTO.items():
        if not general.get(clave):
            general[clave] = valor


def _ruba_id_guardado(incidente_id: int) -> Optional[str]:
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        return incidente.ruba_id_remoto if incidente else None


def _guardar_id_remoto(incidente_id: int, ruba_id_remoto: str) -> None:
    """Se persiste apenas RUBA crea el incidente: si un paso posterior
    falla, el reintento retoma ese mismo incidente en vez de duplicarlo."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is not None:
            incidente.ruba_id_remoto = ruba_id_remoto
            incidente.actualizado_en = datetime.now()


def sincronizar_incidente(
    incidente_id: int, on_progreso: Optional[Callable[[EventoProgreso], None]] = None,
) -> Dict[str, Any]:
    """Carga el incidente en RUBA y devuelve {'ruba_id_remoto', 'url_final',
    'advertencias'}. Lanza una excepción con un mensaje claro si algo falla;
    no toca la base de datos (eso lo hace el `RubaSyncWorker`)."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is not None and incidente.en_curso:
            raise ValueError("El servicio sigue EN CURSO: cerralo (con los horarios de regreso) antes de cargarlo a RUBA.")
    payload = payload_desde_incidente(incidente_id)
    _completar_denunciante(payload)
    errores = validar_payload(payload)
    if errores:
        raise ValueError("El incidente no está listo para RUBA: " + " ".join(errores))

    resultado = RubaServiceAutomation(
        payload,
        on_progreso=on_progreso,
        ruba_id_existente=_ruba_id_guardado(incidente_id),
        on_id_remoto=lambda ruba_id: _guardar_id_remoto(incidente_id, ruba_id),
    ).ejecutar()
    return {
        "ruba_id_remoto": resultado.ruba_id_remoto,
        "url_final": resultado.url_final,
        "advertencias": resultado.advertencias,
    }


# ---------------------------------------------------------------------------
# Persistencia del resultado
# ---------------------------------------------------------------------------

def _marcar_sincronizado(incidente_id: int, ruba_id_remoto: Optional[str]) -> None:
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            return
        incidente.estado_ruba = EstadoRuba.SINCRONIZADO.value
        incidente.ruba_id_remoto = ruba_id_remoto
        incidente.ruba_error_log = None
        incidente.actualizado_en = datetime.now()


def _marcar_error(incidente_id: int, mensaje: str) -> None:
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            return
        incidente.estado_ruba = EstadoRuba.ERROR.value
        incidente.ruba_error_log = mensaje[:4000]
        incidente.actualizado_en = datetime.now()


# ---------------------------------------------------------------------------
# Worker Qt (QThread) — ejecución asíncrona, sin congelar la UI
# ---------------------------------------------------------------------------

class RubaSyncWorker(QObject):
    """Corre `sincronizar_incidente()` en un QThread aparte. `run()` nunca
    deja escapar una excepción: cualquier falla se captura, se persiste en
    estado_ruba/ruba_error_log, y se informa por señal Qt (entrega segura
    entre hilos vía conexión encolada, aunque el receptor viva en la UI)."""

    sincronizado = Signal(int, str, str, str)     # incidente_id, numero_parte, ruba_id_remoto, url_final
    fallo = Signal(int, str, str, str)            # incidente_id, numero_parte, mensaje_error, ruta_captura
    progreso = Signal(int, str, str, int, str)    # incidente_id, paso, estado, porcentaje, mensaje
    terminado = Signal()                     # siempre se emite al final, haya éxito o no

    def __init__(self, incidente_id: int, numero_parte: str) -> None:
        super().__init__()
        self.incidente_id = incidente_id
        self.numero_parte = numero_parte

    def _reenviar_progreso(self, evento: EventoProgreso) -> None:
        self.progreso.emit(self.incidente_id, evento.paso.name, evento.estado, evento.porcentaje, evento.mensaje)

    def run(self) -> None:
        try:
            resultado = sincronizar_incidente(self.incidente_id, on_progreso=self._reenviar_progreso)
        except Exception as e:  # noqa: BLE001 - nunca debe tirar hacia afuera del hilo de fondo
            if isinstance(e, RubaAutomationError):
                mensaje_corto = f"[{e.paso.value}] {e.detalle}"
                captura = str(e.captura) if e.captura else ""
            else:
                mensaje_corto, captura = f"{type(e).__name__}: {e}", ""
            try:
                _marcar_error(self.incidente_id, f"{mensaje_corto}\n{traceback.format_exc(limit=5)}")
            except Exception:
                pass  # si ni siquiera se pudo escribir en la DB, igual avisamos por señal
            self.fallo.emit(self.incidente_id, self.numero_parte, mensaje_corto, captura)
            self.terminado.emit()
            return

        try:
            _marcar_sincronizado(self.incidente_id, resultado.get("ruba_id_remoto"))
        except Exception as e:  # noqa: BLE001
            self.fallo.emit(
                self.incidente_id, self.numero_parte,
                f"Se sincronizó en RUBA pero no se pudo guardar el resultado local: {e}", "",
            )
            self.terminado.emit()
            return

        self.sincronizado.emit(
            self.incidente_id, self.numero_parte, resultado.get("ruba_id_remoto") or "", resultado.get("url_final") or "",
        )
        self.terminado.emit()


def lanzar_sincronizacion(incidente_id: int, numero_parte: str) -> Tuple[QThread, RubaSyncWorker]:
    """Crea y arranca el hilo de sincronización para un incidente. El
    llamador (MainWindow) DEBE guardar la tupla devuelta en una lista propia
    mientras el hilo esté vivo -- si Python junta basura el QThread mientras
    corre, Qt puede crashear la app."""
    hilo = QThread()
    worker = RubaSyncWorker(incidente_id, numero_parte)
    worker.moveToThread(hilo)

    hilo.started.connect(worker.run)
    worker.terminado.connect(hilo.quit)
    worker.terminado.connect(worker.deleteLater)
    hilo.finished.connect(hilo.deleteLater)

    hilo.start()
    return hilo, worker
