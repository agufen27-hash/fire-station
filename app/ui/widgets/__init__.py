"""
Controles reutilizables de la UI de guardia: campos de hora/fecha con
captura de un toque ("Ahora"/"Hoy") y una lista de personal con checkboxes.
"""

from __future__ import annotations

import base64
from datetime import date as date_
from datetime import time as time_
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QBuffer, QDate, QEvent, QIODevice, QPoint, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateEdit,
    QDateTimeEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)


class EditorHoraRapido(QTimeEdit):
    """QTimeEdit para tipear de corrido con el teclado numérico: al entrar
    queda seleccionada la hora; "0815" -> 08:15 (Qt pasa solo a los minutos
    al completar la hora) y ":", "." o "," también saltan a los minutos."""

    tecleado = Signal()

    SEPARADORES = {":", ".", ",", "h", "H"}

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDisplayFormat("HH:mm")
        self.setKeyboardTracking(True)
        self.setCurrentSection(QDateTimeEdit.Section.HourSection)

    def _seleccionar(self, seccion: QDateTimeEdit.Section) -> None:
        self.setCurrentSection(seccion)
        self.setSelectedSection(seccion)

    def focusInEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().focusInEvent(event)
        # Diferido: el click del mouse reubica el cursor DESPUÉS del foco.
        QTimer.singleShot(0, self._seleccionar_hora)

    def _seleccionar_hora(self) -> None:
        self._seleccionar(QDateTimeEdit.Section.HourSection)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - override de Qt
        if event.text() in self.SEPARADORES and event.text():
            self._seleccionar(QDateTimeEdit.Section.MinuteSection)
            return
        if event.text().isdigit():
            self.tecleado.emit()
        super().keyPressEvent(event)


class TimeField(QWidget):
    """QTimeEdit (HH:mm) + botón "Ahora". Distingue "sin tocar" de "00:00
    real" bloqueando la señal mientras se resetea programáticamente. Sin
    valor se muestra "--:--" (no "00:00", que se confundiría con medianoche
    -- clave para un regreso todavía no registrado)."""

    valorCambiado = Signal()

    def __init__(self, parent: Optional[QWidget] = None, con_boton: bool = True) -> None:
        super().__init__(parent)
        self._tiene_valor = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.time_edit = EditorHoraRapido(self)
        self.time_edit.setTime(QTime(0, 0))
        self.time_edit.timeChanged.connect(self._on_time_changed)
        self.time_edit.tecleado.connect(self._on_tecleado)
        self.time_edit.editingFinished.connect(self._on_edicion_terminada)
        self.time_edit.installEventFilter(self)
        self._tecleado = False
        self._actualizar_placeholder()

        self.boton_ahora = QPushButton("Ahora", self)
        self.boton_ahora.setObjectName("botonAhora")
        self.boton_ahora.setFixedWidth(64)
        self.boton_ahora.clicked.connect(self.set_ahora)

        layout.addWidget(self.time_edit, 1)
        layout.addWidget(self.boton_ahora)
        self.boton_ahora.setVisible(con_boton)  # en celdas de tabla no hay lugar

    def _on_time_changed(self, _qtime: QTime) -> None:
        self._tiene_valor = True
        self._actualizar_placeholder()
        self.valorCambiado.emit()

    def eventFilter(self, objeto, evento) -> bool:  # noqa: N802 - override de Qt
        # Sin valor se muestra "--:--"; al entrar a escribir se ve "00:00" con
        # la hora seleccionada, para tipear directo encima.
        if objeto is self.time_edit and not self._tiene_valor:
            if evento.type() == QEvent.Type.FocusIn:
                self.time_edit.setSpecialValueText("")
            elif evento.type() == QEvent.Type.FocusOut:
                self._actualizar_placeholder()
        return super().eventFilter(objeto, evento)

    def _on_tecleado(self) -> None:
        self._tecleado = True

    def _on_edicion_terminada(self) -> None:
        # Un "00:00" tipeado a mano no cambia el valor (no emite timeChanged)
        # pero es un dato real: medianoche.
        if self._tecleado and not self._tiene_valor:
            self._tiene_valor = True
            self._actualizar_placeholder()
            self.valorCambiado.emit()
        self._tecleado = False

    def _actualizar_placeholder(self) -> None:
        # QTimeEdit muestra el specialValueText solo en su mínimo (00:00): se
        # usa únicamente mientras no hay valor, así un 00:00 real se ve como tal.
        self.time_edit.setSpecialValueText("" if self._tiene_valor else "--:--")

    def set_ahora(self) -> None:
        self.time_edit.setTime(QTime.currentTime())  # dispara timeChanged -> _tiene_valor = True

    def value(self) -> Optional[time_]:
        if not self._tiene_valor:
            return None
        qt = self.time_edit.time()
        return time_(qt.hour(), qt.minute())

    def set_value(self, valor: Optional[time_]) -> None:
        self.time_edit.blockSignals(True)
        if valor is None:
            self.time_edit.setTime(QTime(0, 0))
            self._tiene_valor = False
        else:
            self.time_edit.setTime(QTime(valor.hour, valor.minute))
            self._tiene_valor = True
        self._actualizar_placeholder()
        self.time_edit.blockSignals(False)

    def clear(self) -> None:
        self.set_value(None)


class DateField(QWidget):
    """QDateEdit (dd/MM/yyyy) + botón "Hoy". `valorCambiado` se emite solo
    por cambios del usuario (set_value() no la dispara)."""

    valorCambiado = Signal()

    def __init__(self, parent: Optional[QWidget] = None, con_boton: bool = True) -> None:
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.date_edit = QDateEdit(self)
        self.date_edit.setDisplayFormat("dd/MM/yyyy")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.dateChanged.connect(lambda _qd: self.valorCambiado.emit())

        self.boton_hoy = QPushButton("Hoy", self)
        self.boton_hoy.setObjectName("botonAhora")
        self.boton_hoy.setFixedWidth(64)
        self.boton_hoy.clicked.connect(self.set_hoy)

        layout.addWidget(self.date_edit, 1)
        layout.addWidget(self.boton_hoy)
        self.boton_hoy.setVisible(con_boton)

    def set_hoy(self) -> None:
        self.date_edit.setDate(QDate.currentDate())

    def value(self) -> date_:
        qd = self.date_edit.date()
        return date_(qd.year(), qd.month(), qd.day())

    def set_value(self, valor: date_) -> None:
        self.date_edit.blockSignals(True)
        self.date_edit.setDate(QDate(valor.year, valor.month, valor.day))
        self.date_edit.blockSignals(False)


class ListaPersonalCheckeable(QListWidget):
    """Lista de personal con checkbox, para armar la dotación de bomberos."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

    def cargar_personal(self, personal: List) -> None:
        """`personal`: lista de objetos con .id y .nombre_completo()."""
        self.clear()
        for p in personal:
            item = QListWidgetItem(f"{p.nombre_completo()}" + (f"  ({p.jerarquia})" if p.jerarquia else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            self.addItem(item)

    def ids_seleccionados(self) -> List[int]:
        seleccionados = []
        for i in range(self.count()):
            item = self.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                seleccionados.append(item.data(Qt.ItemDataRole.UserRole))
        return seleccionados

    def limpiar_seleccion(self) -> None:
        for i in range(self.count()):
            self.item(i).setCheckState(Qt.CheckState.Unchecked)


class TarjetaKPI(QFrame):
    """Tarjeta de acceso rápido del dashboard (Fase 6): ícono + valor grande
    + título, con sombra sutil, clickeable para navegar a la sección
    correspondiente."""

    clicked = Signal()

    def __init__(self, titulo: str, icono: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("kpiCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(110)

        from app.ui import theme  # import diferido: theme no depende de widgets

        theme.aplicar_sombra(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(4)

        label_icono = QLabel(icono, self)
        label_icono.setObjectName("kpiIcono")
        layout.addWidget(label_icono)

        self.label_valor = QLabel("…", self)
        self.label_valor.setObjectName("kpiValor")
        layout.addWidget(self.label_valor)

        label_titulo = QLabel(titulo.upper(), self)
        label_titulo.setObjectName("kpiTitulo")
        label_titulo.setWordWrap(True)
        layout.addWidget(label_titulo)

        layout.addStretch(1)

    def set_valor(self, texto: str) -> None:
        self.label_valor.setText(texto)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class FirmaPad(QWidget):
    """Pad de firma táctil/mouse: se traza con el dedo o el mouse sobre un
    lienzo transparente (Fase 8: el trazo va directo sobre fondo
    transparente, así el PNG final se puede estampar en Excel/PDF sin un
    recuadro blanco alrededor). Se usa para registrar la firma del legajo
    de cada bombero en `data/firmas/legajos/{legajo}.png` (ver
    `app/ui/main_window.py`, sección Documentación)."""

    COLOR_TRAZO = QColor("#0f172a")
    COLOR_PLACEHOLDER = QColor("#94a3b8")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setCursor(Qt.CursorShape.CrossCursor)
        # "Papel" blanco en ambos temas: el trazo de la firma tiene que contrastar.
        self.setStyleSheet("background-color: #ffffff; border: 1px solid #cfd6e2; border-radius: 8px;")

        self._imagen = QImage(max(self.width(), 1), max(self.height(), 1), QImage.Format.Format_ARGB32)
        self._imagen.fill(Qt.GlobalColor.transparent)
        self._hay_trazo = False
        self._dibujando = False
        self._ultimo_punto = QPoint()

    def resizeEvent(self, event) -> None:  # noqa: N802 - override de Qt
        nueva = QImage(max(self.width(), 1), max(self.height(), 1), QImage.Format.Format_ARGB32)
        nueva.fill(Qt.GlobalColor.transparent)
        pintor = QPainter(nueva)
        pintor.drawImage(0, 0, self._imagen)
        pintor.end()
        self._imagen = nueva
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - override de Qt
        pintor = QPainter(self)
        pintor.drawImage(0, 0, self._imagen)
        if not self._hay_trazo:
            pintor.setPen(self.COLOR_PLACEHOLDER)
            pintor.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Firme acá (mouse o pantalla táctil)")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - override de Qt
        if event.button() == Qt.MouseButton.LeftButton:
            self._dibujando = True
            self._hay_trazo = True
            self._ultimo_punto = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - override de Qt
        if not self._dibujando:
            return
        punto_actual = event.position().toPoint()
        pintor = QPainter(self._imagen)
        pintor.setPen(QPen(self.COLOR_TRAZO, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        pintor.drawLine(self._ultimo_punto, punto_actual)
        pintor.end()
        self._ultimo_punto = punto_actual
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - override de Qt
        self._dibujando = False

    def tiene_firma(self) -> bool:
        return self._hay_trazo

    def limpiar(self) -> None:
        self._imagen.fill(Qt.GlobalColor.transparent)
        self._hay_trazo = False
        self.update()

    def obtener_base64(self) -> Optional[str]:
        if not self._hay_trazo:
            return None
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self._imagen.save(buffer, "PNG")
        return base64.b64encode(bytes(buffer.data())).decode("ascii")

    def cargar_base64(self, texto: Optional[str]) -> None:
        self.limpiar()
        if not texto:
            return
        imagen = QImage.fromData(base64.b64decode(texto), "PNG")
        if not imagen.isNull():
            self._imagen = imagen.convertToFormat(QImage.Format.Format_ARGB32)
            self._hay_trazo = True
            self.update()

    def cargar_desde_archivo(self, ruta) -> bool:
        """Carga una imagen (PNG/JPG) subida a mano como firma -- Fase 8,
        alternativa a dibujarla. Devuelve False si el archivo no es una
        imagen válida (y no toca el estado del pad)."""
        imagen = QImage(str(ruta))
        if imagen.isNull():
            return False
        self._imagen = imagen.convertToFormat(QImage.Format.Format_ARGB32).scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        self._hay_trazo = True
        self.update()
        return True

    def guardar_png(self, ruta) -> bool:
        """Guarda la firma actual como PNG (fondo transparente) en `ruta`.
        Devuelve False si todavía no hay ningún trazo/imagen cargada."""
        if not self._hay_trazo:
            return False
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        return self._imagen.save(str(ruta), "PNG")
