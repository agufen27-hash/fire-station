"""
Diálogo MODAL que acompaña la carga en lote a RUBA (Historial -> "🚀 Cargar
Seleccionados a RUBA"): barra general "k de N", el paso en curso en tiempo
real ("Parte N° 012/2026: Cargando dotación (bomberos)…") y la lista de
resultados por parte (✅ cargado, ❌ error con su detalle, ⏭ omitido).

La carga corre en un QThread (`RubaLoteWorker`): la interfaz no se congela.
"Cancelar" no corta el parte que se está cargando (dejarlo a medias en RUBA
sería peor): detiene la cola al terminarlo. Mientras corre no se puede
cerrar el diálogo; al terminar, "Cerrar".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

ROL_CAPTURA = Qt.ItemDataRole.UserRole


class DialogoLoteRuba(QDialog):
    def __init__(self, total: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Carga en RUBA")
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(560)
        self.total = total
        self.estado_final: Optional[str] = None     # "ok" | "con_errores" | "cancelado" al terminar
        self._worker = None
        self._items: Dict[int, QListWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        titulo = QLabel(f"Cargando {total} parte(s) en RUBA", self)
        titulo.setObjectName("pageTitle")
        layout.addWidget(titulo)

        fila_barra = QHBoxLayout()
        self.barra = QProgressBar(self)
        self.barra.setRange(0, max(total, 1))
        self.barra.setValue(0)
        self.barra.setFormat("%v de %m")
        self.label_contador = QLabel(f"0 de {total}", self)
        self.label_contador.setMinimumWidth(64)
        self.label_contador.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fila_barra.addWidget(self.barra, 1)
        fila_barra.addWidget(self.label_contador)
        layout.addLayout(fila_barra)

        self.label_detalle = QLabel("Preparando la carga en segundo plano…", self)
        self.label_detalle.setObjectName("mensajeProgreso")
        self.label_detalle.setWordWrap(True)
        self.label_detalle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label_detalle)

        self.lista = QListWidget(self)
        self.lista.setMinimumHeight(160)
        self.lista.setWordWrap(True)
        self.lista.itemDoubleClicked.connect(self._abrir_captura)
        layout.addWidget(self.lista, 1)

        self.label_resumen = QLabel("", self)
        self.label_resumen.setWordWrap(True)
        self.label_resumen.setVisible(False)
        layout.addWidget(self.label_resumen)

        botones = QHBoxLayout()
        botones.addStretch(1)
        self.boton_cancelar = QPushButton("Cancelar", self)
        self.boton_cancelar.setToolTip("Detiene la cola al terminar el parte que se está cargando")
        self.boton_cancelar.clicked.connect(self._cancelar)
        botones.addWidget(self.boton_cancelar)
        self.boton_cerrar = QPushButton("Cerrar", self)
        self.boton_cerrar.setEnabled(False)
        self.boton_cerrar.clicked.connect(self.accept)
        botones.addWidget(self.boton_cerrar)
        layout.addLayout(botones)

    # -- Conexión con el worker -------------------------------------------------

    def conectar(self, worker) -> None:
        """Slots de ESTE QObject (hilo de la UI): Qt encola las señales del
        worker. Nunca lambdas sueltas (correrían en el hilo del worker)."""
        self._worker = worker
        worker.item_iniciado.connect(self.on_item_iniciado)
        worker.detalle.connect(self.on_detalle)
        worker.item_ok.connect(self.on_item_ok)
        worker.item_error.connect(self.on_item_error)
        worker.item_omitido.connect(self.on_item_omitido)
        worker.navegador_faltante.connect(self.on_navegador_faltante)
        worker.terminado.connect(self.on_terminado)

    def on_item_iniciado(self, indice: int, total: int, incidente_id: int, numero_parte: str) -> None:
        self.label_contador.setText(f"{indice} de {total}")
        item = QListWidgetItem(f"⏳ Parte N° {numero_parte}: en curso…")
        self.lista.addItem(item)
        self.lista.scrollToItem(item)
        self._items[incidente_id] = item

    def on_detalle(self, incidente_id: int, texto: str) -> None:
        self.label_detalle.setText(texto)

    def _completar(self, incidente_id: int, texto: str, color: str, tooltip: str = "") -> None:
        item = self._items.get(incidente_id)
        if item is None:
            item = QListWidgetItem()
            self.lista.addItem(item)
        item.setText(texto)
        item.setForeground(QColor(theme.color(color)))
        if tooltip:
            item.setToolTip(tooltip)
        self.barra.setValue(self.barra.value() + 1)

    def on_item_ok(self, incidente_id: int, numero_parte: str, ruba_id: str, url: str) -> None:
        self._completar(incidente_id, f"✅ Parte N° {numero_parte}: cargado en RUBA (ID {ruba_id or '—'})",
                        "verde_texto", url)

    def on_item_error(self, incidente_id: int, numero_parte: str, mensaje: str, captura: str) -> None:
        self._completar(incidente_id, f"❌ Parte N° {numero_parte}: {mensaje}", "rojo",
                        mensaje + ("\n\nDoble clic: ver la captura del error." if captura else ""))
        item = self._items.get(incidente_id)
        if item is not None and captura:
            item.setData(ROL_CAPTURA, captura)

    def on_item_omitido(self, incidente_id: int, numero_parte: str, motivo: str) -> None:
        self._completar(incidente_id, f"⏭ Parte N° {numero_parte}: omitido ({motivo})", "texto_secundario")

    def on_navegador_faltante(self, mensaje: str) -> None:
        """Aviso visual con las instrucciones (Chrome / Edge / playwright install)."""
        self.label_detalle.setText("❌ No hay un navegador disponible para conectarse a RUBA.")
        theme.set_tono(self.label_detalle, "error")
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("Falta un navegador para RUBA")
        texto, _, detalle = mensaje.partition("\n\nDetalle técnico:\n")
        caja.setText(texto)
        if detalle:
            caja.setDetailedText(detalle)
        caja.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        caja.open()  # no bloquea: el lote termina de cerrarse detrás

    def on_terminado(self, ok: int, errores: int, omitidos: int, sin_procesar: int) -> None:
        if sin_procesar:
            self.estado_final = "cancelado"
        else:
            self.estado_final = "con_errores" if errores else "ok"
        partes = [f"✅ {ok} cargado(s)"]
        if errores:
            partes.append(f"❌ {errores} con error (quedan como Error: corregilos y volvé a intentar)")
        if omitidos:
            partes.append(f"⏭ {omitidos} omitido(s)")
        if sin_procesar:
            partes.append(f"⏹ {sin_procesar} sin procesar (cancelado)")
        self.label_detalle.setText("Carga finalizada." if not sin_procesar else "Carga cancelada.")
        self.label_resumen.setText(" · ".join(partes))
        theme.set_tono(self.label_resumen, "error" if errores else ("alerta" if sin_procesar else "ok"))
        self.label_resumen.setVisible(True)
        self.boton_cancelar.setEnabled(False)
        self.boton_cerrar.setEnabled(True)
        self.boton_cerrar.setFocus()
        self._worker = None

    # -- Acciones ---------------------------------------------------------------

    def _cancelar(self) -> None:
        if self._worker is not None:
            self._worker.cancelar()
        self.boton_cancelar.setEnabled(False)
        self.boton_cancelar.setText("Cancelando…")
        self.label_detalle.setText(self.label_detalle.text() + "\n⏹ Cancelación pedida: se detiene al terminar el parte en curso.")

    def _abrir_captura(self, item: QListWidgetItem) -> None:
        ruta = item.data(ROL_CAPTURA)
        if ruta and Path(ruta).exists() and hasattr(os, "startfile"):
            os.startfile(str(ruta))  # noqa: S606 - abrir la captura es la acción pedida

    def en_curso(self) -> bool:
        return self.estado_final is None

    # Mientras corre, ni Esc ni la X cierran el diálogo (quedaría el lote huérfano).
    def reject(self) -> None:  # noqa: D401 - override de Qt
        if self.en_curso():
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - override de Qt
        if self.en_curso():
            event.ignore()
            return
        super().closeEvent(event)
