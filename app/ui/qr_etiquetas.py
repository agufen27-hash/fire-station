"""
Visualización, exportación e impresión de los QR de los activos (rotulado de
equipos, herramientas y vehículos). Ver app/services/qr_service.py.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services import qr_service

Activo = Tuple[str, str, str]   # (código, nombre, ubicación)


@lru_cache(maxsize=512)
def qr_pixmap(codigo: str, lado: int = 220) -> QPixmap:
    return QPixmap.fromImage(qr_service.qr_qimage(codigo, lado))


def qr_icono(codigo: str) -> QIcon:
    """Miniatura para la grilla del inventario."""
    return QIcon(qr_pixmap(codigo, 64))


def _carpeta_salida() -> Path:
    from app.paths import get_writable_dir

    carpeta = get_writable_dir("output") / "etiquetas_qr"
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def imprimir_etiquetas(parent: Optional[QWidget], activos: Sequence[Activo]) -> bool:
    """Diálogo de impresión del sistema; misma grilla de etiquetas que el PDF."""
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter

    impresora = QPrinter(QPrinter.PrinterMode.HighResolution)
    dialogo = QPrintDialog(impresora, parent)
    dialogo.setWindowTitle("Imprimir etiquetas QR")
    if dialogo.exec() != QDialog.DialogCode.Accepted:
        return False
    qr_service.dibujar_etiquetas(impresora, list(activos))
    return True


def exportar_etiquetas(parent: Optional[QWidget], activos: Sequence[Activo], nombre: str = "Etiquetas_QR") -> None:
    """Pide dónde guardar, genera el PDF A4 (3 x 7 etiquetas) y ofrece abrirlo."""
    activos = list(activos)
    if not activos:
        QMessageBox.information(parent, "Etiquetas QR", "No hay activos para etiquetar con el filtro actual.")
        return
    ruta, _ = QFileDialog.getSaveFileName(parent, "Exportar etiquetas QR", str(_carpeta_salida() / f"{nombre}.pdf"),
                                          "PDF (*.pdf)")
    if not ruta:
        return
    try:
        ruta_pdf, paginas = qr_service.exportar_etiquetas_pdf(activos, Path(ruta))
    except (OSError, ValueError) as e:
        QMessageBox.warning(parent, "No se pudieron exportar las etiquetas", str(e))
        return
    respuesta = QMessageBox.question(
        parent, "Etiquetas exportadas",
        f"{len(activos)} etiqueta(s) en {paginas} hoja(s) A4:\n{ruta_pdf}\n\n¿Abrir el PDF?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
    if respuesta == QMessageBox.StandardButton.Yes:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ruta_pdf)))


class DialogoQrActivo(QDialog):
    """QR grande de un activo + guardar (PNG/SVG), imprimir etiqueta o PDF."""

    def __init__(self, codigo: str, nombre: str, ubicacion: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._activo: Activo = (codigo, nombre, ubicacion)
        self.setWindowTitle(f"Código QR · {codigo}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(8)

        imagen = QLabel(self)
        imagen.setPixmap(qr_pixmap(codigo, 300))
        imagen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        imagen.setStyleSheet("background: white; border-radius: 8px; padding: 8px;")
        layout.addWidget(imagen)
        etiqueta = QLabel(codigo, self)
        etiqueta.setObjectName("pageTitle")
        etiqueta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        etiqueta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(etiqueta)
        detalle = QLabel(f"{nombre}" + (f" · {ubicacion}" if ubicacion else ""), self)
        detalle.setObjectName("pageSubtitle")
        detalle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detalle.setWordWrap(True)
        layout.addWidget(detalle)

        fila = QHBoxLayout()
        for texto, accion in (("💾 Guardar imagen", self._guardar), ("🖨️ Imprimir etiqueta", self._imprimir),
                              ("📄 Etiqueta en PDF", self._pdf)):
            boton = QPushButton(texto, self)
            boton.setObjectName("botonHerramienta")
            boton.clicked.connect(accion)
            fila.addWidget(boton)
        layout.addLayout(fila)
        botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        botones.rejected.connect(self.reject)
        layout.addWidget(botones)

    def _guardar(self) -> None:
        codigo = self._activo[0]
        ruta, _ = QFileDialog.getSaveFileName(self, "Guardar QR", str(_carpeta_salida() / f"{codigo}.png"),
                                              "Imagen PNG (*.png);;Vectorial SVG (*.svg)")
        if ruta:
            try:
                qr_service.guardar_qr(codigo, Path(ruta))
            except OSError as e:
                QMessageBox.warning(self, "No se pudo guardar", str(e))

    def _imprimir(self) -> None:
        imprimir_etiquetas(self, [self._activo])

    def _pdf(self) -> None:
        exportar_etiquetas(self, [self._activo], nombre=f"Etiqueta_{self._activo[0]}")


def activos_para_etiquetas(items) -> List[Activo]:
    """ItemInventario -> (código, nombre, ubicación)."""
    return [(i.codigo_qr, i.nombre, i.ubicacion) for i in items]
