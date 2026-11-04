"""
Entrada de códigos QR en el escritorio.

  * LectorPistolaQr: filtro de eventos de la aplicación que reconoce una
    pistola lectora USB (actúa como teclado: tipea el código muy rápido y
    termina con Enter) cuando el foco NO está en un campo de texto. Si el
    foco está en un campo, el código entra ahí como texto normal (sirve para
    los buscadores). Emite `codigo_leido` con el código normalizado.
  * DialogoEscanearQr: búsqueda rápida — campo con foco listo para la
    pistola o para tipear, y lectura por cámara (QtMultimedia + zxing-cpp)
    si hay cámara y decodificador.
"""

from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.services.qr_service import decodificador_disponible, decodificar_qimage, normalizar_codigo
from app.ui import theme

# Una persona tipea a ~100-250 ms por tecla; una pistola, a < 30 ms.
MAX_MS_ENTRE_TECLAS = 45
MAX_MS_PROMEDIO = 30
MIN_CARACTERES = 4
INTERVALO_DECODIFICACION_MS = 250


def _es_campo_de_texto(widget: Optional[QWidget]) -> bool:
    if widget is None:
        return False
    if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return not getattr(widget, "isReadOnly", lambda: False)()
    if isinstance(widget, QComboBox):
        return widget.isEditable()
    return False


class LectorPistolaQr(QObject):
    """Instalar con `QApplication.instance().installEventFilter(lector)`."""

    codigo_leido = Signal(str)

    def __init__(self, parent: Optional[QObject] = None, reloj=time.monotonic) -> None:
        super().__init__(parent)
        self._reloj = reloj
        self._buffer: List[str] = []
        self._tiempos: List[float] = []
        self.activo = True

    def reiniciar(self) -> None:
        self._buffer.clear()
        self._tiempos.clear()

    def eventFilter(self, objeto, evento) -> bool:  # noqa: N802 - override de Qt
        if not self.activo or evento.type() != QEvent.Type.KeyPress:
            return False
        app = QApplication.instance()
        if app is not None and (app.activeModalWidget() is not None or _es_campo_de_texto(app.focusWidget())):
            self.reiniciar()
            return False
        return self.procesar_tecla(evento.key(), evento.text())

    def procesar_tecla(self, tecla: int, texto: str) -> bool:
        """True si consume la tecla (el Enter que cierra un código leído)."""
        ahora = self._reloj()
        if tecla in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            codigo = self._codigo_si_fue_pistola(ahora)
            self.reiniciar()
            if codigo:
                self.codigo_leido.emit(codigo)
                return True
            return False
        if not texto or not texto.isprintable():
            return False
        if self._tiempos and (ahora - self._tiempos[-1]) * 1000 > MAX_MS_ENTRE_TECLAS:
            self.reiniciar()
        self._buffer.append(texto)
        self._tiempos.append(ahora)
        return False

    def _codigo_si_fue_pistola(self, ahora: float) -> Optional[str]:
        if len(self._buffer) < MIN_CARACTERES:
            return None
        if (ahora - self._tiempos[-1]) * 1000 > MAX_MS_ENTRE_TECLAS:
            return None
        intervalos = [b - a for a, b in zip(self._tiempos, self._tiempos[1:])]
        if intervalos and sum(intervalos) / len(intervalos) * 1000 > MAX_MS_PROMEDIO:
            return None
        return normalizar_codigo("".join(self._buffer))


class DialogoEscanearQr(QDialog):
    """Devuelve el código en `codigo` (accept) o se cancela."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.codigo: Optional[str] = None
        self._camara = None
        self._sesion = None
        self._sink = None
        self._ultimo_intento = 0.0
        self.setWindowTitle("Escanear / buscar por QR")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        titulo = QLabel("🔳 Escanear un activo", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)
        ayuda = QLabel("Apuntá la pistola lectora al QR de la etiqueta, o escribí el código (ej. FS-9A1B2C3D) "
                       "y Enter. Se abre la ficha del activo con sus checklists pendientes.", self)
        ayuda.setObjectName("ayudaSeccion")
        ayuda.setWordWrap(True)
        layout.addWidget(ayuda)

        self.campo = QLineEdit(self)
        self.campo.setPlaceholderText("Código QR del activo…")
        self.campo.setMinimumHeight(40)
        fuente = self.campo.font()
        fuente.setPointSize(fuente.pointSize() + 4)
        self.campo.setFont(fuente)
        self.campo.returnPressed.connect(self._aceptar_texto)
        layout.addWidget(self.campo)

        self.vista_camara = QLabel(self)
        self.vista_camara.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vista_camara.setMinimumHeight(260)
        self.vista_camara.setVisible(False)
        layout.addWidget(self.vista_camara)

        self.label_estado = QLabel("", self)
        self.label_estado.setObjectName("chip")
        self.label_estado.setWordWrap(True)
        self.label_estado.setVisible(False)
        layout.addWidget(self.label_estado)

        fila = QHBoxLayout()
        self.boton_camara = QPushButton("📷 Usar cámara", self)
        self.boton_camara.setObjectName("botonHerramienta")
        self.boton_camara.clicked.connect(self._alternar_camara)
        disponible, motivo = self._camara_disponible()
        self.boton_camara.setEnabled(disponible)
        self.boton_camara.setToolTip("" if disponible else motivo)
        fila.addWidget(self.boton_camara)
        fila.addStretch(1)
        botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        botones.button(QDialogButtonBox.StandardButton.Ok).setText("Buscar")
        botones.accepted.connect(self._aceptar_texto)
        botones.rejected.connect(self.reject)
        fila.addWidget(botones)
        layout.addLayout(fila)
        QTimer.singleShot(0, self.campo.setFocus)

    # -- Texto / pistola ----------------------------------------------------------

    def _aceptar_texto(self) -> None:
        codigo = normalizar_codigo(self.campo.text())
        if not codigo:
            self._estado("Eso no parece un código de activo (letras, números y guiones; ej. FS-9A1B2C3D).", "error")
            self.campo.selectAll()
            return
        self._aceptar(codigo)

    def _aceptar(self, codigo: str) -> None:
        self.codigo = codigo
        self._detener_camara()
        self.accept()

    def _estado(self, texto: str, tono: str) -> None:
        self.label_estado.setText(texto)
        theme.set_tono(self.label_estado, tono)
        self.label_estado.setVisible(True)

    # -- Cámara -------------------------------------------------------------------

    @staticmethod
    def _camara_disponible() -> tuple:
        if not decodificador_disponible():
            return False, "Falta el decodificador (pip install zxing-cpp numpy)."
        try:
            from PySide6.QtMultimedia import QMediaDevices
        except ImportError:
            return False, "Qt Multimedia no está disponible."
        if not QMediaDevices.videoInputs():
            return False, "No se detectó ninguna cámara."
        return True, ""

    def _alternar_camara(self) -> None:
        if self._camara is not None:
            self._detener_camara()
            return
        from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink

        self._camara = QCamera(QMediaDevices.defaultVideoInput(), self)
        self._sesion = QMediaCaptureSession(self)
        self._sink = QVideoSink(self)
        self._sesion.setCamera(self._camara)
        self._sesion.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_cuadro)
        self._camara.errorOccurred.connect(lambda _e, texto: self._estado(f"Cámara: {texto}", "error"))
        self._camara.start()
        self.vista_camara.setVisible(True)
        self.boton_camara.setText("⏹ Detener cámara")
        self._estado("Mostrá el QR frente a la cámara…", "info")

    def _on_cuadro(self, cuadro) -> None:
        imagen: QImage = cuadro.toImage()
        if imagen.isNull():
            return
        self.vista_camara.setPixmap(QPixmap.fromImage(imagen).scaled(
            self.vista_camara.width(), 260, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        ahora = time.monotonic()
        if (ahora - self._ultimo_intento) * 1000 < INTERVALO_DECODIFICACION_MS:
            return
        self._ultimo_intento = ahora
        try:
            textos = decodificar_qimage(imagen)
        except Exception as e:  # noqa: BLE001 - nunca cortar la vista por un cuadro malo
            self._estado(f"No se pudo decodificar: {e}", "error")
            return
        for texto in textos:
            codigo = normalizar_codigo(texto)
            if codigo:
                self._aceptar(codigo)
                return

    def _detener_camara(self) -> None:
        if self._camara is not None:
            self._camara.stop()
            self._camara.deleteLater()
            self._camara = None
        self.vista_camara.setVisible(False)
        self.boton_camara.setText("📷 Usar cámara")

    def done(self, resultado: int) -> None:
        self._detener_camara()
        super().done(resultado)


def pedir_codigo_qr(parent: Optional[QWidget] = None) -> Optional[str]:
    dialogo = DialogoEscanearQr(parent)
    return dialogo.codigo if dialogo.exec() == QDialog.DialogCode.Accepted else None
