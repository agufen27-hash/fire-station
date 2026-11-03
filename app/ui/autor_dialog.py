"""
Confirmación de autoría de una planilla: al finalizar (guardar un servicio
cerrado) se elige el bombero/operador que la confeccionó y éste confirma con
su PIN personal (el mismo del legajo y de la firma de dotación). Sin PIN
correcto no se persiste nada. El nombre queda en el pie de las planillas.
Si el PIN validado es todavía el de fábrica, se obliga a definir uno propio
antes de continuar.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.security import es_pin_default
from app.db import get_session
from app.models import Personal
from app.services.personal_service import verificar_pin_personal
from app.ui import theme
from app.ui.pin_dialogs import DialogoCambioPin, DialogoResetPinMaestra


class DialogoAutorPin(QDialog):
    """Modal: responsable + PIN. `personal_id` queda con el autor validado."""

    def __init__(self, numero_parte: str, preseleccion: Optional[int] = None,
                 parent: Optional[QWidget] = None, *, titulo: str = "Confirmar autoría de la planilla",
                 explicacion: Optional[str] = None, texto_ok: str = "Confirmar y guardar") -> None:
        super().__init__(parent)
        self.setWindowTitle(titulo)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.personal_id: Optional[int] = None

        layout = QVBoxLayout(self)
        explicacion = QLabel(explicacion or (
            f"Parte N° {numero_parte}: elegí quién confeccionó la planilla y confirmá con su PIN personal. "
            "Su nombre queda en el pie de las planillas."), self)
        explicacion.setWordWrap(True)
        layout.addWidget(explicacion)

        form = QFormLayout()
        self.combo_responsable = QComboBox(self)
        self.combo_responsable.setEditable(True)  # tipeo con autocompletado
        self.combo_responsable.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        with get_session() as session:
            personas = (session.query(Personal).filter(Personal.activo.is_(True))
                        .order_by(Personal.apellido, Personal.nombre).all())
            for p in personas:
                self.combo_responsable.addItem(p.nombre_completo(), p.id)
        indice = self.combo_responsable.findData(preseleccion) if preseleccion is not None else -1
        self.combo_responsable.setCurrentIndex(indice)
        if indice < 0:
            self.combo_responsable.setEditText("")
        form.addRow("Responsable", self.combo_responsable)

        self.entry_pin = QLineEdit(self)
        self.entry_pin.setEchoMode(QLineEdit.EchoMode.Password)
        self.entry_pin.setMaxLength(10)
        self.entry_pin.setPlaceholderText("PIN personal")
        form.addRow("PIN", self.entry_pin)
        # PIN olvidado: se resetea acá mismo sin cerrar el diálogo, así no se
        # pierde lo cargado en la planilla.
        self.boton_reset_pin = QPushButton("¿Olvidó su PIN? (Clave Maestra)", self)
        self.boton_reset_pin.setFlat(True)
        self.boton_reset_pin.setAutoDefault(False)
        self.boton_reset_pin.clicked.connect(self._resetear_pin)
        form.addRow("", self.boton_reset_pin)
        layout.addLayout(form)

        self.label_error = QLabel("", self)
        self.label_error.setWordWrap(True)
        theme.set_tono(self.label_error, "error")
        self.label_error.hide()
        layout.addWidget(self.label_error)

        botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        botones.button(QDialogButtonBox.StandardButton.Ok).setText(texto_ok)
        botones.accepted.connect(self._validar)
        botones.rejected.connect(self.reject)
        layout.addWidget(botones)

        (self.entry_pin if indice >= 0 else self.combo_responsable).setFocus(Qt.FocusReason.OtherFocusReason)

    def _responsable(self) -> Optional[int]:
        texto = self.combo_responsable.currentText().strip()
        indice = self.combo_responsable.findText(texto, Qt.MatchFlag.MatchFixedString)
        return self.combo_responsable.itemData(indice) if indice >= 0 else None

    def _validar(self) -> None:
        personal_id = self._responsable()
        if personal_id is None:
            self._error("Elegí un responsable de la lista.")
            return
        pin = self.entry_pin.text()
        if not verificar_pin_personal(personal_id, pin):
            self.entry_pin.clear()
            self.entry_pin.setFocus()
            self._error("PIN incorrecto.")
            return
        if es_pin_default(pin):
            cambio = DialogoCambioPin(personal_id, self, pin_actual=pin, obligatorio=True)
            if cambio.exec() != QDialog.DialogCode.Accepted:
                self.entry_pin.clear()
                self._error("Tenés que definir un PIN propio para continuar.")
                return
        self.personal_id = personal_id
        self.accept()

    def _resetear_pin(self) -> None:
        """Reseteo con clave maestra para el responsable elegido; si sale bien
        el diálogo sigue abierto, con el PIN vacío y el foco listo para firmar."""
        personal_id = self._responsable()
        if personal_id is None:
            self._error("Elegí primero de la lista al responsable que olvidó su PIN.")
            return
        dialogo = DialogoResetPinMaestra(personal_id, self.combo_responsable.currentText().strip(), self)
        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        self.entry_pin.clear()
        self._error("PIN actualizado. Firmá ahora con el PIN temporal.", tono="ok")
        self.entry_pin.setFocus(Qt.FocusReason.OtherFocusReason)

    def _error(self, texto: str, tono: str = "error") -> None:
        theme.set_tono(self.label_error, tono)
        self.label_error.setText(texto)
        self.label_error.show()


def pedir_autor(numero_parte: str, preseleccion: Optional[int], parent: QWidget) -> Optional[int]:
    """personal_id del autor validado con PIN, o None si se canceló."""
    dialogo = DialogoAutorPin(numero_parte, preseleccion, parent)
    return dialogo.personal_id if dialogo.exec() == QDialog.DialogCode.Accepted else None


def pedir_operador(parent: QWidget) -> Optional[int]:
    """Identificación del operador (chip 👤 de la barra superior): mismo
    diálogo responsable + PIN, con reseteo por clave maestra incluido.
    personal_id validado, o None si se canceló."""
    dialogo = DialogoAutorPin(
        "", None, parent, titulo="Identificarse como operador",
        explicacion=("Elegí tu nombre y confirmá con tu PIN personal. Los permisos de tu rol "
                     "(Bombero, Jefatura o Administrador) quedan activos hasta que cierres la sesión."),
        texto_ok="Identificarme")
    return dialogo.personal_id if dialogo.exec() == QDialog.DialogCode.Accepted else None
