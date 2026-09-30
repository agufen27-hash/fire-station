"""
Sincronización en segundo plano de `Incidente`s de Fire Station hacia el
portal RUBA: corre en un QThread aparte, con Chromium `headless=True` (nunca
abre una ventana), y nunca congela la interfaz.

Carga EN LOTE (Historial -> "🚀 Cargar Seleccionados a RUBA"): un único
`RubaLoteWorker` procesa los partes de a uno, en orden de fecha, reusando
el MISMO navegador y la misma sesión de RUBA (se loguea una sola vez). Si un
parte falla se marca ERROR y se sigue con el próximo; cancelar detiene la
cola al terminar el parte en curso.

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

import logging
import traceback
from datetime import date, datetime, time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from app.core.semilla import denunciante_por_defecto
from app.db import get_session
from app.models import EstadoRuba, Incidente
from app.services.ruba_automation import (
    EventoProgreso, NavegadorNoDisponibleError, PasoRuba, RubaAutomationError, RubaServiceAutomation,
)
from app.services.ruba_payload import payload_desde_incidente, validar_payload


log = logging.getLogger(__name__)

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
    # El seguro NO se completa acá: si el parte no tiene, "Datos del Seguro"
    # queda en blanco en RUBA (ruba_automation._cargar_seguro).


def _ruba_id_guardado(incidente_id: int) -> Optional[str]:
    """ID remoto a retomar, leído en cada corrida (nunca cacheado): si el
    usuario lo desvinculó o quedó vacío/en blanco, se crea desde cero."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        ruba_id = (incidente.ruba_id_remoto or "").strip() if incidente else ""
        if ruba_id == ID_REMOTO_MANUAL:  # marcador de carga manual, no un incidente de RUBA
            return None
        return ruba_id or None


def _descartar_id_remoto(incidente_id: int, id_viejo: str) -> None:
    """El incidente viejo no abrió en RUBA (500, timeout, eliminado): se
    olvida su ID para que ni esta corrida ni un reintento vuelvan a él."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is not None and incidente.ruba_id_remoto == id_viejo:
            incidente.ruba_id_remoto = None
            incidente.actualizado_en = datetime.now()


def _guardar_id_remoto(incidente_id: int, ruba_id_remoto: str) -> None:
    """Se persiste apenas RUBA crea el incidente: si un paso posterior
    falla, el reintento retoma ese mismo incidente en vez de duplicarlo."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is not None:
            incidente.ruba_id_remoto = ruba_id_remoto
            incidente.actualizado_en = datetime.now()


def preparar_payload(incidente_id: int) -> Dict[str, Any]:
    """Payload listo para RUBA, o ValueError con el motivo (servicio en
    curso, datos faltantes). No abre el navegador: en un lote, un parte
    incompleto falla al instante sin gastar una sesión de Chromium."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            raise ValueError("El parte ya no existe en la base local.")
        if incidente.en_curso:
            raise ValueError("El servicio sigue EN CURSO: cerralo (con los horarios de regreso) antes de cargarlo a RUBA.")
    payload = payload_desde_incidente(incidente_id)
    _completar_denunciante(payload)
    errores = validar_payload(payload)
    if errores:
        raise ValueError("El incidente no está listo para RUBA: " + " ".join(errores))
    return payload


class NavegadorLote:
    """Un navegador de Playwright compartido por todos los partes de un lote.
    Se abre recién cuando el primer parte válido lo necesita, y si se cae a
    mitad del lote (Chromium cerrado, sin red) se vuelve a abrir para el
    próximo. Solo se usa desde el hilo del worker (Playwright sync no se
    comparte entre hilos)."""

    def __init__(self) -> None:
        self._gestor = None
        self._contexto = None
        self._propio = True

    def contexto_para(self, automatizacion: RubaServiceAutomation) -> Tuple[Any, bool]:
        if self._contexto is not None and not self._vivo():
            log.warning("RUBA: el navegador del lote se cerró; se abre otro para el próximo parte.")
            self.cerrar()
        if self._contexto is None:
            gestor = automatizacion.abrir_navegador()
            self._contexto, self._propio = gestor.__enter__()
            self._gestor = gestor
        return self._contexto, self._propio

    def _vivo(self) -> bool:
        try:
            navegador = self._contexto.browser
            return navegador is None or navegador.is_connected()
        except Exception:  # noqa: BLE001
            return False

    def cerrar(self) -> None:
        gestor, self._gestor, self._contexto = self._gestor, None, None
        if gestor is not None:
            try:
                gestor.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - cerrar un navegador caído puede fallar: no importa
                log.exception("RUBA: error al cerrar el navegador del lote")

    def __enter__(self) -> "NavegadorLote":
        return self

    def __exit__(self, *exc) -> None:
        self.cerrar()


def sincronizar_incidente(
    incidente_id: int, on_progreso: Optional[Callable[[EventoProgreso], None]] = None,
    navegador: Optional[NavegadorLote] = None,
) -> Dict[str, Any]:
    """Carga el incidente en RUBA y devuelve {'ruba_id_remoto', 'url_final',
    'advertencias', 'ruba_id_descartado', 'aviso_recreado'}. Lanza una excepción con un mensaje claro si algo falla;
    no toca el estado en la base (eso lo hace el worker). Con `navegador`
    reusa ese navegador (lote); sin él abre y cierra uno propio."""
    payload = preparar_payload(incidente_id)
    automatizacion = RubaServiceAutomation(
        payload,
        on_progreso=on_progreso,
        ruba_id_existente=_ruba_id_guardado(incidente_id),
        on_id_remoto=lambda ruba_id: _guardar_id_remoto(incidente_id, ruba_id),
        on_id_descartado=lambda id_viejo: _descartar_id_remoto(incidente_id, id_viejo),
    )
    if navegador is None:
        resultado = automatizacion.ejecutar()
    else:
        contexto, propio = navegador.contexto_para(automatizacion)
        resultado = automatizacion.ejecutar_en_contexto(contexto, guardar_sesion=propio)
    return {
        "ruba_id_remoto": resultado.ruba_id_remoto,
        "url_final": resultado.url_final,
        "advertencias": resultado.advertencias,
        "ruba_id_descartado": resultado.ruba_id_descartado,
        "aviso_recreado": resultado.aviso_recreado,
    }


# ---------------------------------------------------------------------------
# Persistencia del resultado
# ---------------------------------------------------------------------------

def _marcar_sincronizado(incidente_id: int, ruba_id_remoto: Optional[str]) -> None:
    """Cargado en RUBA: estado SINCRONIZADO (= parte cerrado: el Historial ya
    no permite editarlo ni eliminarlo), con su ID remoto y la fecha."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            return
        ahora = datetime.now()
        incidente.estado_ruba = EstadoRuba.SINCRONIZADO.value
        if ruba_id_remoto:
            incidente.ruba_id_remoto = ruba_id_remoto
        incidente.ruba_error_log = None
        incidente.ruba_sincronizado_en = ahora
        incidente.actualizado_en = ahora


ID_REMOTO_MANUAL = "MANUAL"


def marcar_sincronizado_manual(incidente_ids: List[int]) -> List[int]:
    """"Ya cargado en RUBA manualmente": SINCRONIZADO sin pasar por la
    automatización (el lote lo omite como cualquier parte cargado). Conserva
    el ID remoto real si una corrida anterior lo había creado; si no, queda
    "MANUAL". No toca los EN CURSO (hay que cerrarlos antes). Devuelve los
    ids marcados."""
    marcados: List[int] = []
    ahora = datetime.now()
    with get_session() as session:
        for incidente_id in incidente_ids:
            incidente = session.get(Incidente, incidente_id)
            if incidente is None or incidente.en_curso or incidente.estado_ruba == EstadoRuba.SINCRONIZADO.value:
                continue
            incidente.estado_ruba = EstadoRuba.SINCRONIZADO.value
            incidente.ruba_carga_manual = True
            incidente.ruba_id_remoto = (incidente.ruba_id_remoto or "").strip() or ID_REMOTO_MANUAL
            incidente.ruba_error_log = None
            incidente.ruba_sincronizado_en = ahora
            incidente.actualizado_en = ahora
            marcados.append(incidente_id)
    return marcados


def desmarcar_sincronizado_manual(incidente_id: int) -> bool:
    """Deshace un marcado manual (error del operador): vuelve a PENDIENTE y
    olvida el ID "MANUAL". Los cargados por la automatización no se tocan."""
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None or not incidente.ruba_carga_manual:
            return False
        incidente.estado_ruba = EstadoRuba.PENDIENTE.value
        incidente.ruba_carga_manual = False
        if incidente.ruba_id_remoto == ID_REMOTO_MANUAL:
            incidente.ruba_id_remoto = None
        incidente.ruba_sincronizado_en = None
        incidente.actualizado_en = datetime.now()
        return True


def _marcar_error(incidente_id: int, mensaje: str) -> None:
    with get_session() as session:
        incidente = session.get(Incidente, incidente_id)
        if incidente is None:
            return
        incidente.estado_ruba = EstadoRuba.ERROR.value
        incidente.ruba_error_log = mensaje[:4000]
        incidente.actualizado_en = datetime.now()


def _describir_error(e: BaseException) -> Tuple[str, str]:
    """(mensaje corto para la UI, ruta de la captura o "")."""
    if isinstance(e, RubaAutomationError):
        return f"[{e.paso.value}] {e.detalle}", (str(e.captura) if e.captura else "")
    if isinstance(e, ValueError):
        return str(e), ""
    return f"{type(e).__name__}: {e}", ""


# ---------------------------------------------------------------------------
# Lote: orden y worker Qt (QThread)
# ---------------------------------------------------------------------------

TEXTO_PASO = {
    PasoRuba.SESION.name: "Conectando con RUBA…",
    PasoRuba.INICIALIZACION.name: "Creando el incidente…",
    PasoRuba.GENERAL.name: "Cargando datos generales…",
    PasoRuba.DAMNIFICADOS.name: "Cargando damnificados…",
    PasoRuba.PARTICIPACION.name: "Cargando participación…",
    PasoRuba.BOMBEROS.name: "Cargando dotación (bomberos)…",
    PasoRuba.VEHICULOS.name: "Cargando vehículos y guardando…",
}


def ordenar_lote(incidente_ids: List[int]) -> List[Tuple[int, str, Optional[str]]]:
    """[(id, N° de parte, motivo_para_omitir | None)] en orden cronológico
    (fecha, hora del llamado, N° de parte). Se omiten -- sin abrir RUBA --
    los que ya están cargados (evita duplicarlos), los EN CURSO y los que
    ya no existen."""
    vistos = set()
    filas = []
    with get_session() as session:
        for incidente_id in incidente_ids:
            if incidente_id in vistos:
                continue
            vistos.add(incidente_id)
            inc = session.get(Incidente, incidente_id)
            if inc is None:
                filas.append(((date.max, time.max, ""), incidente_id, f"#{incidente_id}",
                              "ya no existe en la base local"))
                continue
            motivo = None
            if inc.estado_ruba == EstadoRuba.SINCRONIZADO.value:
                motivo = "ya estaba cargado en RUBA"
            elif inc.en_curso:
                motivo = "el servicio sigue EN CURSO"
            clave = (inc.fecha or date.max, inc.hora_llamado or time.max, inc.numero_parte or "")
            filas.append((clave, inc.id, inc.numero_parte, motivo))
    filas.sort(key=lambda f: f[0])
    return [(i, numero, motivo) for _, i, numero, motivo in filas]


class RubaLoteWorker(QObject):
    """Procesa una lista de partes en un QThread aparte, de a uno y en orden.
    `run()` nunca deja escapar una excepción: cada falla queda en
    estado_ruba/ruba_error_log de ESE parte y el lote sigue. Las señales
    llegan a la UI por conexión encolada (el receptor vive en el hilo de Qt)."""

    item_iniciado = Signal(int, int, int, str)   # índice (1..N), total, incidente_id, numero_parte
    detalle = Signal(int, str)                   # incidente_id, texto ("Parte N° X: Conectando…")
    item_ok = Signal(int, str, str, str)         # incidente_id, numero_parte, ruba_id_remoto, url_final
    item_error = Signal(int, str, str, str)      # incidente_id, numero_parte, mensaje, ruta_captura
    item_omitido = Signal(int, str, str)         # incidente_id, numero_parte, motivo
    item_advertencia = Signal(int, str, str)     # incidente_id, numero_parte, aviso (tras item_ok)
    navegador_faltante = Signal(str)             # sin Chrome/Edge/Chromium: mensaje con instrucciones
    terminado = Signal(int, int, int, int)       # ok, errores, omitidos, sin_procesar (por cancelación)

    def __init__(self, incidente_ids: List[int]) -> None:
        super().__init__()
        self.incidente_ids = list(incidente_ids)
        self.incidente_actual: Optional[int] = None
        self._cancelado = False
        self._sin_navegador = False

    def cancelar(self) -> None:
        """Pedido desde la UI: se respeta al terminar el parte en curso."""
        self._cancelado = True

    @property
    def cancelado(self) -> bool:
        return self._cancelado

    def run(self) -> None:
        ok = errores = omitidos = procesados = total = 0
        try:
            lote = ordenar_lote(self.incidente_ids)
            total = len(lote)
            with NavegadorLote() as navegador:
                for indice, (incidente_id, numero, motivo) in enumerate(lote, start=1):
                    if self._cancelado or self._sin_navegador:
                        break
                    procesados += 1
                    self.incidente_actual = incidente_id
                    self.item_iniciado.emit(indice, total, incidente_id, numero)
                    if motivo:
                        omitidos += 1
                        self.item_omitido.emit(incidente_id, numero, motivo)
                        continue
                    if self._procesar(incidente_id, numero, navegador):
                        ok += 1
                    else:
                        errores += 1
        except Exception as e:  # noqa: BLE001 - nunca tirar hacia afuera del hilo de fondo
            log.exception("RUBA: el lote se interrumpió")
            self.detalle.emit(-1, f"El lote se interrumpió: {type(e).__name__}: {e}")
        finally:
            self.incidente_actual = None
            self.terminado.emit(ok, errores, omitidos, max(total - procesados, 0))

    def _procesar(self, incidente_id: int, numero: str, navegador: NavegadorLote) -> bool:
        prefijo = f"Parte N° {numero}: "
        self.detalle.emit(incidente_id, prefijo + "Validando datos…")

        def progreso(evento: EventoProgreso) -> None:
            if evento.estado == "inicio":
                self.detalle.emit(incidente_id, prefijo + TEXTO_PASO.get(evento.paso.name, evento.mensaje))
            elif evento.estado in ("omitido", "aviso"):
                self.detalle.emit(incidente_id, prefijo + evento.mensaje)

        try:
            resultado = sincronizar_incidente(incidente_id, on_progreso=progreso, navegador=navegador)
        except NavegadorNoDisponibleError as e:
            # No es culpa del parte (queda PENDIENTE) y los siguientes fallarían
            # igual: se corta el lote y la UI muestra cómo resolverlo.
            self._sin_navegador = True
            self.item_error.emit(incidente_id, numero,
                                 "No hay un navegador disponible para RUBA (Chrome, Edge o Chromium).", "")
            self.navegador_faltante.emit(str(e))
            return False
        except Exception as e:  # noqa: BLE001 - la falla de un parte no corta el lote
            mensaje, captura = _describir_error(e)
            try:
                _marcar_error(incidente_id, f"{mensaje}\n{traceback.format_exc(limit=5)}")
            except Exception:  # noqa: BLE001 - igual se avisa por señal
                log.exception("RUBA: no se pudo registrar el error del parte %s", numero)
            self.item_error.emit(incidente_id, numero, mensaje, captura)
            return False

        try:
            _marcar_sincronizado(incidente_id, resultado.get("ruba_id_remoto"))
        except Exception as e:  # noqa: BLE001
            self.item_error.emit(incidente_id, numero,
                                 f"Se cargó en RUBA pero no se pudo guardar el resultado local: {e}", "")
            return False
        self.detalle.emit(incidente_id, prefijo + "Guardado exitoso.")
        self.item_ok.emit(incidente_id, numero, resultado.get("ruba_id_remoto") or "",
                          resultado.get("url_final") or "")
        if resultado.get("aviso_recreado"):
            self.item_advertencia.emit(incidente_id, numero, resultado["aviso_recreado"])
        return True


def lanzar_lote(
    incidente_ids: List[int], conectar: Optional[Callable[[RubaLoteWorker], None]] = None,
) -> Tuple[QThread, RubaLoteWorker]:
    """Crea y arranca el hilo del lote. `conectar(worker)` se llama ANTES de
    arrancarlo, para no perder las primeras señales. El llamador (MainWindow)
    DEBE conservar la tupla mientras el hilo viva -- si Python junta basura
    el QThread mientras corre, Qt puede crashear la app."""
    hilo = QThread()
    worker = RubaLoteWorker(incidente_ids)
    worker.moveToThread(hilo)
    if conectar is not None:
        conectar(worker)

    hilo.started.connect(worker.run)
    worker.terminado.connect(hilo.quit)
    worker.terminado.connect(worker.deleteLater)
    hilo.finished.connect(hilo.deleteLater)

    hilo.start()
    return hilo, worker
