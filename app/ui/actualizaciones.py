"""
Controlador de actualizaciones de la ventana principal (ver
app/services/updater.py). Se arranca desde run.py, no desde MainWindow:
así los tests que crean la ventana nunca salen a la red.

Comportamiento según data/config.json -> "actualizaciones": {"modo": ...}:
  - "preguntar" (por defecto): diálogo con "Actualizar ahora", "Al cerrar la
    app" o "Más tarde".
  - "automatico": descarga en segundo plano y se instala AL CERRAR la app.
    Nunca se cierra sola: en un cuartel, reiniciar en medio de un despacho
    o de una carga en RUBA sería peor que quedarse con la versión anterior.
  - "desactivado".
En desarrollo (código fuente) avisa en la barra de estado que hay commits
nuevos en origin/main; desde "Buscar actualizaciones ahora" se pueden traer
con `git pull --ff-only` (solo en main y sin cambios locales) y reiniciar.
En el ejecutable, si no hay release nuevo pero main tiene commits sin
publicar, también se avisa de forma sutil (barra de estado).

La verificación se repite cada INTERVALO_RECHEQUEO_MS mientras la app está
abierta (las PCs de guardia quedan encendidas días). Sin internet / sin git
/ repo inaccesible: se registra en logs/actualizaciones.log y no se molesta.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from app import __version__
from app.paths import is_frozen
from app.services import updater
from app.ui import theme

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

log = logging.getLogger(__name__)

DEMORA_PRIMERA_BUSQUEDA_MS = 8000  # que la ventana termine de abrir antes de salir a la red
INTERVALO_RECHEQUEO_MS = 6 * 60 * 60 * 1000
MENSAJE_PERSISTENTE = 0  # QStatusBar.showMessage(…, 0): queda hasta el próximo mensaje


class ControladorActualizaciones(QObject):
    busqueda_en_curso = Signal(bool)  # para deshabilitar el botón mientras busca

    def __init__(self, ventana: "MainWindow") -> None:
        super().__init__(ventana)
        self.ventana = ventana
        self.modo = updater.modo_configurado()
        self.info: Optional[updater.InfoActualizacion] = None
        self.staging: Optional[Path] = None
        self.instalar_al_cerrar = False
        self._hilos = []          # referencias vivas a los QThread en curso
        self._progreso: Optional[QProgressDialog] = None
        self._instalar_al_terminar_descarga = False
        self._buscando = False
        self._cursor_ocupado_activo = False

    # -- Búsqueda ---------------------------------------------------------------

    def iniciar(self) -> None:
        if self.modo == updater.MODO_DESACTIVADO:
            return
        QTimer.singleShot(DEMORA_PRIMERA_BUSQUEDA_MS, self.buscar)
        self._timer_rechequeo = QTimer(self)
        self._timer_rechequeo.setInterval(INTERVALO_RECHEQUEO_MS)
        self._timer_rechequeo.timeout.connect(self.buscar)
        self._timer_rechequeo.start()

    def buscar(self) -> None:
        """Búsqueda automática (al arrancar): silenciosa si no hay nada o si falla."""
        if self._buscando:
            return
        self._buscando = True
        worker = updater.BuscarActualizacionWorker()
        worker.resultado.connect(self._on_resultado)
        worker.fallo.connect(lambda e: log.info("Búsqueda de actualizaciones sin resultado: %s", e))
        worker.terminado.connect(self._fin_busqueda)
        self._mantener(updater.lanzar_en_hilo(worker), worker)

    def buscar_manual(self) -> None:
        """"Buscar actualizaciones ahora": cursor ocupado + barra de estado
        mientras busca, y SIEMPRE un resultado visible (nueva / al día / error)."""
        if self._buscando:
            self._mensaje("Ya se están buscando actualizaciones…", "info")
            return
        self._buscando = True
        self.busqueda_en_curso.emit(True)
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self._cursor_ocupado_activo = True
        self._mensaje("🔄 Buscando actualizaciones…", "info")
        worker = updater.BuscarActualizacionWorker()
        worker.resultado.connect(self._on_resultado_manual)
        worker.fallo.connect(self._on_fallo_manual)
        worker.terminado.connect(self._fin_busqueda_manual)
        self._mantener(updater.lanzar_en_hilo(worker), worker)

    def _fin_busqueda(self) -> None:
        self._buscando = False

    def _fin_busqueda_manual(self) -> None:
        self._buscando = False
        self.busqueda_en_curso.emit(False)
        # El resultado ya se mostró: restaurar solo si quedó nuestro cursor
        # (los diálogos de resultado lo restauran antes de abrirse).
        self._restaurar_cursor()

    def _restaurar_cursor(self) -> None:
        if self._cursor_ocupado_activo:
            QApplication.restoreOverrideCursor()
            self._cursor_ocupado_activo = False

    def _on_resultado_manual(self, info: Optional[updater.InfoActualizacion]) -> None:
        self._restaurar_cursor()
        self.ventana.statusBar().clearMessage()
        if info is None:
            QMessageBox.information(self.ventana, "Estás al día",
                                    f"Tenés la última versión instalada: v{__version__}")
            return
        self.info = info
        if info.origen == "sin_publicar":
            QMessageBox.information(
                self.ventana, "Estás al día",
                f"Tenés la última versión publicada: v{__version__}.\n\n"
                f"En GitHub hay {info.commits_nuevos} cambio(s) nuevo(s) en main que todavía no se publicaron "
                f"como versión instalable.\n{info.notas}\n\n"
                "Llegan solos a esta PC cuando se publique la próxima versión (tools/release.py).",
            )
            return
        if info.origen == "git":
            self._preguntar_git(info)
            return
        self._preguntar(info)

    def _on_fallo_manual(self, error: str) -> None:
        self._restaurar_cursor()
        self._mensaje("No se pudo verificar si hay actualizaciones.", "alerta")
        caja = QMessageBox(self.ventana)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("No se pudo buscar actualizaciones")
        caja.setText(f"Versión instalada: v{__version__}\n\n{error}")
        caja.setInformativeText(f"El diagnóstico completo (URL, header Authorization, código HTTP) quedó en:\n"
                                f"{updater.ruta_log()}")
        caja.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.exec()

    def _mantener(self, hilo, worker) -> None:
        par = (hilo, worker)
        self._hilos.append(par)
        hilo.finished.connect(lambda: self._hilos.remove(par) if par in self._hilos else None)

    def _on_resultado(self, info: Optional[updater.InfoActualizacion]) -> None:
        if info is None:
            return
        self.info = info
        if info.origen == "sin_publicar":
            self._mensaje(f"ℹ️ Hay {info.commits_nuevos} cambio(s) en GitHub (main) todavía sin publicar "
                          "como versión instalable.", "info", MENSAJE_PERSISTENTE)
            return
        if info.origen == "git":
            accion = ("Configuración → 🔄 Buscar actualizaciones ahora para traerlos."
                      if info.arbol_limpio else "hacé 'git pull' (hay cambios locales o no estás en main).")
            self._mensaje(f"🔄 Hay {info.commits_nuevos} commit(s) nuevo(s) en origin/main: {accion}",
                          "info", MENSAJE_PERSISTENTE)
            return
        if self.modo == updater.MODO_AUTOMATICO:
            self._descargar(instalar_al_terminar=False)
            return
        self._preguntar(info)

    def _preguntar(self, info: updater.InfoActualizacion) -> None:
        caja = QMessageBox(self.ventana)
        caja.setIcon(QMessageBox.Icon.Information)
        caja.setWindowTitle("Actualización disponible")
        tamano = f" ({info.tamano / 1_048_576:.0f} MB)" if info.tamano else ""
        caja.setText(f"Hay una versión nueva de Fire Station: {info.version} (tenés la {__version__}){tamano}.")
        caja.setInformativeText(
            "Tus datos (partes, configuración, planillas, firmas y mapa) no se tocan.\n\n"
            + (info.notas.strip()[:600] if info.notas else "")
        )
        ahora = caja.addButton("Actualizar ahora", QMessageBox.ButtonRole.AcceptRole)
        al_cerrar = caja.addButton("Al cerrar la app", QMessageBox.ButtonRole.ActionRole)
        caja.addButton("Más tarde", QMessageBox.ButtonRole.RejectRole)
        caja.exec()
        if caja.clickedButton() is ahora:
            self._descargar(instalar_al_terminar=True)
        elif caja.clickedButton() is al_cerrar:
            self._descargar(instalar_al_terminar=False)

    # -- Desarrollo: traer cambios con git -------------------------------------------

    def _preguntar_git(self, info: updater.InfoActualizacion) -> None:
        caja = QMessageBox(self.ventana)
        caja.setIcon(QMessageBox.Icon.Information)
        caja.setWindowTitle("Hay cambios nuevos")
        caja.setText(f"Hay {info.commits_nuevos} commit(s) nuevo(s) en origin/main (tenés v{__version__}).")
        if info.arbol_limpio:
            caja.setInformativeText(f"{info.notas}\n\nSe traen con 'git pull --ff-only' (no toca cambios "
                                    "locales ni hace merges) y después se reinicia Fire Station.")
            traer = caja.addButton("Traer cambios ahora", QMessageBox.ButtonRole.AcceptRole)
            caja.addButton("Más tarde", QMessageBox.ButtonRole.RejectRole)
        else:
            caja.setInformativeText(f"{info.notas}\n\nNo se actualiza solo: el checkout no está en main o tiene "
                                    "cambios sin commitear. Actualizá a mano con 'git pull'.")
            traer = None
            caja.addButton("Entendido", QMessageBox.ButtonRole.RejectRole)
        caja.exec()
        if traer is not None and caja.clickedButton() is traer:
            self._traer_cambios_git()

    def _traer_cambios_git(self) -> None:
        self._mensaje("🔄 Trayendo cambios de GitHub (git pull)…", "info", MENSAJE_PERSISTENTE)
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        worker = updater.TraerCambiosGitWorker()
        worker.listo.connect(self._on_cambios_traidos)
        worker.fallo.connect(self._on_fallo_git)
        worker.terminado.connect(QApplication.restoreOverrideCursor)
        self._mantener(updater.lanzar_en_hilo(worker), worker)

    def _on_fallo_git(self, error: str) -> None:
        self._mensaje("No se pudieron traer los cambios de GitHub.", "alerta")
        QMessageBox.warning(self.ventana, "No se pudo actualizar", error)

    def _on_cambios_traidos(self, resumen: str) -> None:
        self.info = None
        self._mensaje(f"✅ Código actualizado ({resumen}): reiniciá Fire Station para usarlo.", "ok",
                      MENSAJE_PERSISTENTE)
        motivo = self._motivo_para_esperar()
        if motivo:
            QMessageBox.information(self.ventana, "Código actualizado",
                                    f"Cambios traídos ({resumen}).\n\n{motivo}\n\n"
                                    "Reiniciá Fire Station cuando termines para usar la versión nueva.")
            return
        respuesta = QMessageBox.question(
            self.ventana, "Código actualizado",
            f"Cambios traídos ({resumen}). ¿Reiniciar Fire Station ahora para usar la versión nueva?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if respuesta == QMessageBox.StandardButton.Yes:
            self._reiniciar()

    def _reiniciar(self) -> None:
        """Abre una instancia nueva (mismo intérprete y argumentos) y cierra esta."""
        if QProcess.startDetached(sys.executable, sys.argv)[0]:
            self.ventana.close()
        else:
            QMessageBox.warning(self.ventana, "No se pudo reiniciar",
                                "Cerrá y volvé a abrir Fire Station para usar la versión nueva.")

    # -- Descarga -----------------------------------------------------------------

    def _descargar(self, instalar_al_terminar: bool) -> None:
        if self.info is None:
            return
        self._instalar_al_terminar_descarga = instalar_al_terminar
        if instalar_al_terminar:
            self._progreso = QProgressDialog(f"Descargando Fire Station {self.info.version}…", "Seguir en segundo plano",
                                             0, 100, self.ventana)
            self._progreso.setWindowTitle("Actualización")
            self._progreso.setWindowModality(Qt.WindowModality.NonModal)
            self._progreso.setAutoClose(False)
            self._progreso.canceled.connect(self._pasar_a_segundo_plano)
            self._progreso.show()
        else:
            self._mensaje(f"Descargando Fire Station {self.info.version} en segundo plano…", "info")
        worker = updater.DescargarActualizacionWorker(self.info)
        worker.progreso.connect(self._on_progreso)
        worker.listo.connect(self._on_descarga_lista)
        worker.fallo.connect(self._on_descarga_fallida)
        self._mantener(updater.lanzar_en_hilo(worker), worker)

    def _pasar_a_segundo_plano(self) -> None:
        # Cerrar el diálogo no cancela la descarga: se instala al cerrar la app.
        self._instalar_al_terminar_descarga = False
        self._mensaje("La actualización sigue descargándose; se instala al cerrar Fire Station.", "info")

    def _on_progreso(self, leidos: int, total: int) -> None:
        if self._progreso is not None and total:
            self._progreso.setValue(int(100 * leidos / total))

    def _cerrar_progreso(self) -> None:
        if self._progreso is not None:
            self._progreso.canceled.disconnect()
            self._progreso.close()
            self._progreso = None

    def _on_descarga_fallida(self, error: str) -> None:
        self._cerrar_progreso()
        log.warning("Actualización: %s", error)
        if self._instalar_al_terminar_descarga:
            QMessageBox.warning(self.ventana, "No se pudo actualizar", error)
        else:
            self._mensaje(f"No se pudo descargar la actualización: {error}", "alerta")

    def _on_descarga_lista(self, staging: str) -> None:
        self._cerrar_progreso()
        self.staging = Path(staging)
        self.instalar_al_cerrar = True
        if not self._instalar_al_terminar_descarga:
            self._mensaje(f"Fire Station {self.info.version} está lista: se instala al cerrar la app.", "ok")
            return
        motivo = self._motivo_para_esperar()
        if motivo:
            QMessageBox.information(self.ventana, "Actualización lista",
                                    f"{motivo}\n\nLa versión {self.info.version} se instala al cerrar Fire Station.")
            return
        respuesta = QMessageBox.question(
            self.ventana, "Actualización lista",
            f"Fire Station se va a cerrar para instalar la versión {self.info.version} y se vuelve a abrir sola "
            "en unos segundos. ¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if respuesta == QMessageBox.StandardButton.Yes:
            self.ventana.close()  # closeEvent -> al_cerrar() lanza la instalación

    def _motivo_para_esperar(self) -> Optional[str]:
        v = self.ventana
        if v.carga_ruba_en_curso():
            return "Hay una carga en RUBA en curso."
        if v._incidente_en_edicion is not None or v._formulario_con_datos_sin_guardar():
            return "Hay un parte abierto en el formulario sin guardar."
        return None

    # -- Cierre -------------------------------------------------------------------

    def al_cerrar(self) -> None:
        """Llamado desde MainWindow.closeEvent: si hay una versión descargada
        y verificada, deja corriendo el instalador (espera a que termine este
        proceso)."""
        if not (self.instalar_al_cerrar and self.staging and is_frozen()):
            return
        try:
            updater.lanzar_instalacion(self.staging)
        except Exception as e:  # noqa: BLE001 - nunca impedir el cierre por la actualización
            log.exception("No se pudo lanzar la instalación de la actualización: %s", e)

    def _mensaje(self, texto: str, tono: str, duracion_ms: int = 15000) -> None:
        barra = self.ventana.statusBar()
        theme.set_tono(barra, "neutro" if tono == "info" else tono)
        barra.showMessage(texto, duracion_ms)


def iniciar_actualizaciones(ventana: "MainWindow") -> ControladorActualizaciones:
    controlador = ControladorActualizaciones(ventana)
    ventana.controlador_actualizaciones = controlador
    controlador.iniciar()
    return controlador

