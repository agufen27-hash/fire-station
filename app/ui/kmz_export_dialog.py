"""
Diálogo "📦 Exportar zonas a KMZ" de la Cartografía Táctica.

Muestra las zonas disponibles (polígonos de los partes guardados, o el
polígono que se está dibujando en el formulario), deja elegir cuáles
exportar, con qué tipo de zona (perímetro, despliegue, búsqueda, exclusión)
y nombre, y escribe el .kmz en un hilo aparte (app/workers/tarea_fondo.py):
la ventana principal no se congela aunque el archivo vaya a un pendrive o
a una carpeta de red lenta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.paths import get_writable_dir
from app.utils.kmz_exporter import ESTILOS, ExportadorKMZ, TipoZona
from app.workers.tarea_fondo import TareaFondo


@dataclass
class ZonaExportable:
    """Una zona candidata. `anillo`: [(lat, lon), ...] sin cerrar."""
    nombre: str
    anillo: List[Tuple[float, float]]
    descripcion: str = ""
    tipo: TipoZona = TipoZona.PERIMETRO
    punto: Optional[Tuple[float, float]] = None   # (lat, lon) del siniestro, si hay


COL_INCLUIR, COL_NOMBRE, COL_TIPO, COL_DETALLE = range(4)


def construir_kmz(ruta: Path, zonas: Sequence[ZonaExportable], incluir_puntos: bool,
                  nombre_documento: str) -> Path:
    """Trabajo pesado (sin Qt): corre en el hilo de fondo."""
    exp = ExportadorKMZ(nombre_documento)
    for z in zonas:
        exp.agregar_zona(z.nombre, z.anillo, z.tipo, z.descripcion)
        if incluir_puntos and z.punto is not None:
            exp.agregar_punto(z.nombre, z.punto[0], z.punto[1], z.descripcion)
    return exp.guardar(ruta)


class ExportarZonasKmzDialog(QDialog):
    def __init__(self, zonas: Sequence[ZonaExportable], parent: Optional[QWidget] = None,
                 nombre_documento: str = "Fire Station - Zonas operativas",
                 nombre_archivo: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exportar zonas a KMZ")
        self.resize(760, 440)
        self._zonas = list(zonas)
        self._nombre_documento = nombre_documento
        self._nombre_archivo = nombre_archivo or f"zonas_operativas_{date.today():%Y%m%d}.kmz"
        self._tarea: Optional[TareaFondo] = None
        self.ruta_exportada: Optional[Path] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Elegí las zonas a exportar y su tipo. Se guardan como polígonos semitransparentes "
            "(Google Earth, Avenza Maps, Gaia GPS, ArcGIS Field Maps).", self))

        self.tabla = QTableWidget(len(self._zonas), 4, self)
        self.tabla.setHorizontalHeaderLabels(["", "Nombre", "Tipo de zona", "Detalle"])
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        cab = self.tabla.horizontalHeader()
        cab.setSectionResizeMode(COL_INCLUIR, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(COL_NOMBRE, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(COL_TIPO, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(COL_DETALLE, QHeaderView.ResizeMode.Stretch)
        for fila, zona in enumerate(self._zonas):
            incluir = QTableWidgetItem()
            incluir.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            incluir.setCheckState(Qt.CheckState.Checked)
            self.tabla.setItem(fila, COL_INCLUIR, incluir)
            self.tabla.setItem(fila, COL_NOMBRE, QTableWidgetItem(zona.nombre))
            combo = QComboBox(self.tabla)
            for tipo in TipoZona:
                combo.addItem(ESTILOS[tipo].etiqueta, tipo)
            combo.setCurrentIndex(list(TipoZona).index(zona.tipo))
            self.tabla.setCellWidget(fila, COL_TIPO, combo)
            detalle = QTableWidgetItem(zona.descripcion)
            detalle.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tabla.setItem(fila, COL_DETALLE, detalle)
        layout.addWidget(self.tabla, 1)

        fila_opciones = QHBoxLayout()
        boton_todas = QPushButton("Marcar todas", self)
        boton_todas.clicked.connect(lambda: self._marcar_todas(True))
        boton_ninguna = QPushButton("Desmarcar todas", self)
        boton_ninguna.clicked.connect(lambda: self._marcar_todas(False))
        self.check_puntos = QCheckBox("Incluir marcadores de los siniestros", self)
        self.check_puntos.setChecked(True)
        fila_opciones.addWidget(boton_todas)
        fila_opciones.addWidget(boton_ninguna)
        fila_opciones.addStretch(1)
        fila_opciones.addWidget(self.check_puntos)
        layout.addLayout(fila_opciones)

        self.label_estado = QLabel("", self)
        layout.addWidget(self.label_estado)

        self.botones = QDialogButtonBox(self)
        self.boton_exportar = self.botones.addButton("💾 Exportar KMZ…", QDialogButtonBox.ButtonRole.AcceptRole)
        self.botones.addButton(QDialogButtonBox.StandardButton.Close)
        self.boton_exportar.clicked.connect(self._exportar)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)

    # -- Lectura de la tabla ------------------------------------------------------------

    def _marcar_todas(self, marcar: bool) -> None:
        estado = Qt.CheckState.Checked if marcar else Qt.CheckState.Unchecked
        for fila in range(self.tabla.rowCount()):
            self.tabla.item(fila, COL_INCLUIR).setCheckState(estado)

    def zonas_elegidas(self) -> List[ZonaExportable]:
        elegidas: List[ZonaExportable] = []
        for fila, zona in enumerate(self._zonas):
            if self.tabla.item(fila, COL_INCLUIR).checkState() != Qt.CheckState.Checked:
                continue
            nombre = (self.tabla.item(fila, COL_NOMBRE).text() or "").strip() or zona.nombre
            tipo = self.tabla.cellWidget(fila, COL_TIPO).currentData()
            elegidas.append(ZonaExportable(nombre, zona.anillo, zona.descripcion, tipo, zona.punto))
        return elegidas

    # -- Exportación en segundo plano --------------------------------------------------

    def _exportar(self) -> None:
        if self._tarea is not None:
            return
        zonas = self.zonas_elegidas()
        if not zonas:
            QMessageBox.information(self, "Nada para exportar", "Marcá al menos una zona.")
            return
        carpeta = get_writable_dir("output")
        carpeta.mkdir(parents=True, exist_ok=True)
        ruta_texto, _ = QFileDialog.getSaveFileName(
            self, "Exportar zonas a KMZ", str(carpeta / self._nombre_archivo), "Google Earth KMZ (*.kmz)")
        if not ruta_texto:
            return
        ruta = Path(ruta_texto)
        if ruta.suffix.lower() != ".kmz":
            ruta = ruta.with_suffix(".kmz")

        incluir_puntos = self.check_puntos.isChecked()
        self._tarea = TareaFondo(
            lambda: construir_kmz(ruta, zonas, incluir_puntos, self._nombre_documento),
            descripcion="exportación de zonas a KMZ", parent=self)
        self._tarea.terminado.connect(self._on_exportado)
        self._tarea.error.connect(self._on_error)
        self._tarea.finished.connect(self._on_tarea_finalizada)
        self._ocupado(True, f"Exportando {len(zonas)} zona(s)…")
        self._tarea.start()

    def _ocupado(self, ocupado: bool, texto: str = "") -> None:
        self.boton_exportar.setEnabled(not ocupado)
        self.tabla.setEnabled(not ocupado)
        self.label_estado.setText(texto)

    def _on_exportado(self, ruta: Path) -> None:
        self.ruta_exportada = Path(ruta)
        self._ocupado(False, f"✅ Guardado en {ruta}")
        QMessageBox.information(self, "KMZ exportado", f"Se guardó en:\n{ruta}")

    def _on_error(self, mensaje: str) -> None:
        self._ocupado(False, "")
        QMessageBox.critical(self, "No se pudo exportar el KMZ", mensaje)

    def _on_tarea_finalizada(self) -> None:
        if self._tarea is not None:
            self._tarea.deleteLater()
        self._tarea = None

    # -- No cerrar con la escritura a medias ---------------------------------------------

    def reject(self) -> None:
        if self._tarea is not None and self._tarea.isRunning():
            self.label_estado.setText("⏳ Esperá a que termine la exportación…")
            return
        super().reject()

    def closeEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        if self._tarea is not None and self._tarea.isRunning():
            evento.ignore()
            return
        super().closeEvent(evento)
