"""
Diálogo NO modal que acompaña la carga en RUBA: muestra cada fase de
`RubaServiceAutomation` (Sesión, Inicialización, Datos generales,
Damnificados, Participación, Bomberos, Vehículos) con su estado, una barra
de progreso y, al final, el ID/URL generados en RUBA o el error con su
captura. La carga corre en un QThread: cerrar el diálogo no la cancela.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services.ruba_automation import PASOS
from app.ui import theme

ICONOS = {"pendiente": "○", "inicio": "⏳", "ok": "✅", "omitido": "⏭", "error": "❌"}


class DialogoProgresoRuba(QDialog):
    def __init__(self, numero_parte: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Carga en RUBA — Siniestro N° {numero_parte}")
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(520)
        self.estado_final: Optional[str] = None  # "ok" | "error" cuando termina

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        titulo = QLabel(f"Carga en RUBA · N° {numero_parte}", self)
        titulo.setObjectName("pageTitle")
        layout.addWidget(titulo)

        self.label_resumen_local = QLabel("", self)
        self.label_resumen_local.setWordWrap(True)
        self.label_resumen_local.setObjectName("pageSubtitle")
        layout.addWidget(self.label_resumen_local)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(8)
        self._iconos: Dict[str, QLabel] = {}
        self._detalles: Dict[str, QLabel] = {}
        for fila, paso in enumerate(PASOS):
            icono = QLabel(ICONOS["pendiente"], self)
            icono.setFixedWidth(24)
            nombre = QLabel(f"{fila + 1}. {paso.value}", self)
            detalle = QLabel("", self)
            detalle.setObjectName("pageSubtitle")
            grid.addWidget(icono, fila, 0)
            grid.addWidget(nombre, fila, 1)
            grid.addWidget(detalle, fila, 2)
            self._iconos[paso.name] = icono
            self._detalles[paso.name] = detalle
        layout.addLayout(grid)

        fila_barra = QHBoxLayout()
        self.barra = QProgressBar(self)
        self.barra.setRange(0, 100)
        self.barra.setValue(0)
        self.label_porcentaje = QLabel("0 %", self)
        self.label_porcentaje.setMinimumWidth(44)
        self.label_porcentaje.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fila_barra.addWidget(self.barra, 1)
        fila_barra.addWidget(self.label_porcentaje)
        layout.addLayout(fila_barra)

        self.label_mensaje = QLabel("Iniciando la carga en segundo plano…", self)
        self.label_mensaje.setObjectName("mensajeProgreso")
        self.label_mensaje.setWordWrap(True)
        self.label_mensaje.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label_mensaje)

        # Resultado (oculto hasta terminar)
        self.panel_resultado = QWidget(self)
        grid_res = QGridLayout(self.panel_resultado)
        grid_res.setContentsMargins(0, 0, 0, 0)
        grid_res.addWidget(QLabel("ID en RUBA"), 0, 0)
        self.entry_id = QLineEdit(self.panel_resultado)
        self.entry_id.setReadOnly(True)
        grid_res.addWidget(self.entry_id, 0, 1)
        grid_res.addWidget(QLabel("URL"), 1, 0)
        self.label_url = QLabel(self.panel_resultado)
        self.label_url.setOpenExternalLinks(True)
        self.label_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.label_url.setWordWrap(True)
        grid_res.addWidget(self.label_url, 1, 1)
        self.panel_resultado.setVisible(False)
        layout.addWidget(self.panel_resultado)

        botones = QHBoxLayout()
        self.boton_captura = QPushButton("Ver captura del error", self)
        self.boton_captura.setVisible(False)
        self.boton_captura.clicked.connect(self._abrir_captura)
        botones.addWidget(self.boton_captura)
        botones.addStretch(1)
        self.boton_cerrar = QPushButton("Cerrar (la carga sigue en segundo plano)", self)
        self.boton_cerrar.clicked.connect(self.close)
        botones.addWidget(self.boton_cerrar)
        layout.addLayout(botones)

        self._ruta_captura: Optional[Path] = None

    # -- Slots conectados al worker ---------------------------------------------

    def set_resumen_local(self, texto: str) -> None:
        self.label_resumen_local.setText(texto)

    def on_progreso(self, paso: str, estado: str, porcentaje: int, mensaje: str) -> None:
        if paso in self._iconos:
            self._iconos[paso].setText(ICONOS.get(estado, "•"))
            if estado in ("omitido", "error"):
                self._detalles[paso].setText(mensaje[:80])
        self._set_porcentaje(porcentaje)
        self.label_mensaje.setText(mensaje)

    def _set_porcentaje(self, porcentaje: int) -> None:
        self.barra.setValue(porcentaje)
        self.label_porcentaje.setText(f"{porcentaje} %")

    def on_exito(self, ruba_id: str, url: str) -> None:
        self.estado_final = "ok"
        self._set_porcentaje(100)
        self.label_mensaje.setText("✅ Servicio cargado en RUBA.")
        theme.set_tono(self.label_mensaje, "ok")
        self.entry_id.setText(ruba_id or "(RUBA no informó el ID)")
        self.label_url.setText(f'<a href="{url}">{url}</a>' if url else "—")
        self.panel_resultado.setVisible(True)
        self.boton_cerrar.setText("Cerrar")

    def on_fallo(self, mensaje: str, ruta_captura: str) -> None:
        self.estado_final = "error"
        self.label_mensaje.setText(
            f"❌ No se pudo completar la carga en RUBA:\n{mensaje}\n\n"
            "El servicio quedó guardado localmente como pendiente de sincronización."
        )
        theme.set_tono(self.label_mensaje, "error")
        self._ruta_captura = Path(ruta_captura) if ruta_captura else None
        self.boton_captura.setVisible(self._ruta_captura is not None and self._ruta_captura.exists())
        self.boton_cerrar.setText("Cerrar")

    def _abrir_captura(self) -> None:
        if self._ruta_captura and self._ruta_captura.exists() and hasattr(os, "startfile"):
            os.startfile(str(self._ruta_captura))  # noqa: S606 - abrir la captura es la acción pedida
