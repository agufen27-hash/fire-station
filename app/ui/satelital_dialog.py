"""
Diálogo "🛰️ Descargar imagen satelital" (Configuración → Mapa Operativo).

1. Área: los límites del mapa (precargados con los de Configuración) o un
   cuadrado de N km alrededor del cuartel.
2. Período y nubosidad máxima para el compuesto Sentinel-2.
3. Resolución (10 / 20 / 30 m): se muestra el tamaño estimado y, si el área
   no entra en los límites de Earth Engine, la resolución real que se usará.
4. "Descargar": la llamada a Earth Engine corre en un QThread
   (app/workers/tarea_fondo.py); la ventana no se congela.
5. Vista previa y "Usar como mapa base": instala la imagen y su calibración.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.services import imagen_satelital as sat
from app.services.cartografia import CalibracionMapa
from app.workers.tarea_fondo import TareaFondo

MESES_POR_DEFECTO = 3


def _a_qdate(d: date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _de_qdate(q: QDate) -> date:
    return date(q.year(), q.month(), q.day())


class DescargaSatelitalDialog(QDialog):
    def __init__(self, calibracion: CalibracionMapa, parent: Optional[QWidget] = None,
                 descargar=sat.descargar, instalar=sat.instalar) -> None:
        """`descargar` / `instalar` se inyectan en las pruebas."""
        super().__init__(parent)
        self.setWindowTitle("Descargar imagen satelital (Google Earth Engine)")
        self.resize(720, 640)
        self._calibracion = calibracion
        self._descargar = descargar
        self._instalar = instalar
        self._tarea: Optional[TareaFondo] = None
        self.imagen: Optional[sat.ImagenDescargada] = None
        self.instalada = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Se descarga un compuesto <b>Sentinel-2</b> sin nubes (10 m por píxel) de la zona y queda "
            "como imagen de fondo del mapa, calibrada automáticamente. Se baja una sola vez: después el "
            "mapa funciona sin internet.<br><i>No es la imagen de Google Earth / Google Maps.</i>", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._crear_caja_area())
        layout.addWidget(self._crear_caja_parametros())

        self.label_estimacion = QLabel(self)
        self.label_estimacion.setWordWrap(True)
        self.label_estimacion.setProperty("muted", True)
        layout.addWidget(self.label_estimacion)

        self.label_preview = QLabel("(la vista previa aparece al terminar la descarga)", self)
        self.label_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_preview.setMinimumHeight(220)
        self.label_preview.setProperty("muted", True)
        layout.addWidget(self.label_preview, 1)

        self.label_estado = QLabel("", self)
        self.label_estado.setWordWrap(True)
        layout.addWidget(self.label_estado)

        self.botones = QDialogButtonBox(self)
        self.boton_descargar = self.botones.addButton("🛰️ Descargar", QDialogButtonBox.ButtonRole.ActionRole)
        self.boton_usar = self.botones.addButton("✅ Usar como mapa base", QDialogButtonBox.ButtonRole.AcceptRole)
        self.boton_usar.setEnabled(False)
        self.botones.addButton(QDialogButtonBox.StandardButton.Close)
        self.boton_descargar.clicked.connect(self._iniciar_descarga)
        self.boton_usar.clicked.connect(self._usar_como_base)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)

        for spin in (self.spin_norte, self.spin_sur, self.spin_oeste, self.spin_este):
            spin.valueChanged.connect(self._actualizar_estimacion)
        self.combo_resolucion.currentIndexChanged.connect(self._actualizar_estimacion)
        self._actualizar_estimacion()

    # -- Construcción -------------------------------------------------------------------

    def _spin(self, minimo: float, maximo: float, valor: float) -> QDoubleSpinBox:
        s = QDoubleSpinBox(self)
        s.setRange(minimo, maximo)
        s.setDecimals(6)
        s.setSingleStep(0.01)
        s.setValue(valor)
        return s

    def _crear_caja_area(self) -> QGroupBox:
        c = self._calibracion
        caja = QGroupBox("Área a descargar", self)
        form = QFormLayout(caja)
        self.spin_norte = self._spin(-90, 90, c.lat_norte)
        self.spin_sur = self._spin(-90, 90, c.lat_sur)
        self.spin_oeste = self._spin(-180, 180, c.lon_oeste)
        self.spin_este = self._spin(-180, 180, c.lon_este)
        form.addRow("Latitud norte", self.spin_norte)
        form.addRow("Latitud sur", self.spin_sur)
        form.addRow("Longitud oeste", self.spin_oeste)
        form.addRow("Longitud este", self.spin_este)

        fila = QHBoxLayout()
        self.spin_radio = QDoubleSpinBox(self)
        self.spin_radio.setRange(1, 100)
        self.spin_radio.setDecimals(1)
        self.spin_radio.setSuffix(" km")
        self.spin_radio.setValue(10)
        boton_centrar = QPushButton("🚒 Centrar en el cuartel", self)
        boton_centrar.setToolTip("Reemplaza los límites por un cuadrado con ese radio alrededor del cuartel.")
        boton_centrar.clicked.connect(self._centrar_en_cuartel)
        fila.addWidget(QLabel("Radio:", self))
        fila.addWidget(self.spin_radio)
        fila.addWidget(boton_centrar)
        fila.addStretch(1)
        form.addRow(fila)
        return caja

    def _crear_caja_parametros(self) -> QGroupBox:
        caja = QGroupBox("Imagen", self)
        form = QFormLayout(caja)
        hoy = date.today()
        self.fecha_desde = QDateEdit(_a_qdate(hoy - timedelta(days=30 * MESES_POR_DEFECTO)), self)
        self.fecha_hasta = QDateEdit(_a_qdate(hoy), self)
        for f in (self.fecha_desde, self.fecha_hasta):
            f.setCalendarPopup(True)
            f.setDisplayFormat("dd/MM/yyyy")
            f.setMinimumDate(QDate(2017, 3, 28))
            f.setMaximumDate(_a_qdate(hoy + timedelta(days=1)))
        form.addRow("Desde", self.fecha_desde)
        form.addRow("Hasta", self.fecha_hasta)
        self.spin_nubes = QSpinBox(self)
        self.spin_nubes.setRange(1, 100)
        self.spin_nubes.setSuffix(" %")
        self.spin_nubes.setValue(20)
        self.spin_nubes.setToolTip("Se descartan las pasadas con más nubosidad que esto.")
        form.addRow("Nubosidad máxima", self.spin_nubes)
        self.combo_resolucion = QComboBox(self)
        for metros, texto in zip(sat.RESOLUCIONES_M, sat.nombres_para_ui()):
            self.combo_resolucion.addItem(texto, metros)
        form.addRow("Resolución", self.combo_resolucion)
        return caja

    # -- Lógica de la UI ----------------------------------------------------------------

    def pedido(self) -> sat.PedidoImagen:
        return sat.PedidoImagen(
            lat_norte=self.spin_norte.value(), lat_sur=self.spin_sur.value(),
            lon_oeste=self.spin_oeste.value(), lon_este=self.spin_este.value(),
            fecha_desde=_de_qdate(self.fecha_desde.date()),
            # filterDate es exclusivo en el extremo final: +1 día incluye el "hasta".
            fecha_hasta=_de_qdate(self.fecha_hasta.date()) + timedelta(days=1),
            nubosidad_max=float(self.spin_nubes.value()),
            resolucion_m=float(self.combo_resolucion.currentData()),
        )

    def _centrar_en_cuartel(self) -> None:
        norte, sur, oeste, este = sat.limites_alrededor(
            self._calibracion.cuartel_lat, self._calibracion.cuartel_lon, self.spin_radio.value())
        for spin, valor in ((self.spin_norte, norte), (self.spin_sur, sur),
                            (self.spin_oeste, oeste), (self.spin_este, este)):
            spin.setValue(valor)

    def _actualizar_estimacion(self) -> None:
        p = self.pedido()
        try:
            p.validar()
        except sat.ImagenSatelitalError as e:
            self.label_estimacion.setText(f"⚠️ {e}")
            self.boton_descargar.setEnabled(False)
            return
        grilla = sat.calcular_grilla(p)
        ancho_km, alto_km = sat.area_km(p)
        texto = (f"Área ≈ {ancho_km:.1f} x {alto_km:.1f} km → {grilla.ancho} x {grilla.alto} px "
                 f"(~{grilla.megabytes:.0f} MB sin comprimir).")
        if grilla.resolucion_m > p.resolucion_m + 0.05:
            texto += (f" El área es grande para {p.resolucion_m:g} m: se descargará a "
                      f"{grilla.resolucion_m:g} m/px (límite de Earth Engine).")
        self.label_estimacion.setText(texto)
        self.boton_descargar.setEnabled(self._tarea is None)

    def _ocupado(self, ocupado: bool, texto: str = "") -> None:
        self.boton_descargar.setEnabled(not ocupado)
        self.boton_usar.setEnabled(not ocupado and self.imagen is not None)
        for w in self.findChildren(QGroupBox):
            w.setEnabled(not ocupado)
        self.label_estado.setText(texto)

    def _iniciar_descarga(self) -> None:
        if self._tarea is not None:
            return
        pedido = self.pedido()
        try:
            pedido.validar()
        except sat.ImagenSatelitalError as e:
            QMessageBox.warning(self, "Datos inválidos", str(e))
            return
        self.imagen = None
        self._tarea = TareaFondo(lambda: self._descargar(pedido), "descarga de imagen satelital", self)
        self._tarea.terminado.connect(self._on_descargada)
        self._tarea.error.connect(self._on_error)
        self._tarea.finished.connect(self._on_tarea_finalizada)
        self._ocupado(True, "⏳ Earth Engine está armando el compuesto Sentinel-2… (puede tardar 1-3 minutos)")
        self._tarea.start()

    def _on_descargada(self, imagen: sat.ImagenDescargada) -> None:
        self.imagen = imagen
        pixmap = QPixmap(str(imagen.ruta))
        if not pixmap.isNull():
            self.label_preview.setPixmap(pixmap.scaled(
                self.label_preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        self._ocupado(False, f"✅ Imagen descargada: {imagen.grilla.ancho} x {imagen.grilla.alto} px, "
                             f"{imagen.grilla.resolucion_m:g} m/px. {imagen.atribucion}.")

    def _on_error(self, mensaje: str) -> None:
        self._ocupado(False, "")
        QMessageBox.critical(self, "No se pudo descargar la imagen", mensaje)

    def _on_tarea_finalizada(self) -> None:
        if self._tarea is not None:
            self._tarea.deleteLater()
        self._tarea = None
        self._actualizar_estimacion()
        self.boton_usar.setEnabled(self.imagen is not None)

    def _usar_como_base(self) -> None:
        if self.imagen is None:
            return
        try:
            destino, _ = self._instalar(self.imagen, self._calibracion.cuartel_lat, self._calibracion.cuartel_lon)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "No se pudo instalar la imagen", str(e))
            return
        self.instalada = True
        QMessageBox.information(
            self, "Imagen satelital instalada",
            f"La imagen quedó como mapa base:\n{destino}\n\nLos límites del mapa se actualizaron solos.\n\n"
            f"{self.imagen.atribucion}.")
        self.accept()

    # -- No cerrar con la descarga en curso -------------------------------------------

    def reject(self) -> None:
        if self._tarea is not None and self._tarea.isRunning():
            self.label_estado.setText("⏳ Esperá a que termine la descarga…")
            return
        super().reject()

    def closeEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        if self._tarea is not None and self._tarea.isRunning():
            evento.ignore()
            return
        super().closeEvent(evento)

