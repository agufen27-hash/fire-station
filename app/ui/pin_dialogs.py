"""
Diálogos de PIN: cambio por el propio bombero (pide el actual), reseteo
con la clave maestra del cuartel y cambio obligatorio del PIN de fábrica.

Toda la validación pasa por app/services/personal_service.py; los errores
se muestran dentro del diálogo, que no se cierra hasta que el cambio quedó
guardado o se cancela.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.security import LARGO_MAX_PIN, PIN_DEFAULT, segundos_bloqueo_maestra
from app.services.personal_service import PinError, cambiar_pin, resetear_pin_con_clave_maestra
from app.ui import theme


def _campo_secreto(parent: QWidget, largo: int = LARGO_MAX_PIN, ayuda: str = "") -> QLineEdit:
    campo = QLineEdit(parent)
    campo.setEchoMode(QLineEdit.EchoMode.Password)
    campo.setMaxLength(largo)
    if ayuda:
        campo.setPlaceholderText(ayuda)
    return campo


class _DialogoPinBase(QDialog):
    """Esqueleto común: explicación, formulario, línea de error y botones."""

    def __init__(self, titulo: str, explicacion: str, texto_ok: str, parent: Optional[QWidget]) -> None:
        super().__init__(parent)
        self.setWindowTitle(titulo)
        self.setModal(True)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        etiqueta = QLabel(explicacion, self)
        etiqueta.setWordWrap(True)
        layout.addWidget(etiqueta)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.label_error = QLabel("", self)
        self.label_error.setWordWrap(True)
        theme.set_tono(self.label_error, "error")
        self.label_error.hide()
        layout.addWidget(self.label_error)
        self.botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.botones.button(QDialogButtonBox.StandardButton.Ok).setText(texto_ok)
        self.botones.accepted.connect(self._confirmar)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)

    def _error(self, texto: str, foco: Optional[QLineEdit] = None) -> None:
        self.label_error.setText(texto)
        self.label_error.show()
        if foco is not None:
            foco.clear()
            foco.setFocus()

    def _confirmar(self) -> None:  # pragma: no cover - lo implementa cada diálogo
        raise NotImplementedError


class DialogoCambioPin(_DialogoPinBase):
    """Cambio de PIN por el propio bombero. Con `pin_actual` (ya validado
    por quien llama, p. ej. el PIN de fábrica recién tipeado) no lo vuelve a
    pedir; `obligatorio` cambia los textos y el botón Cancelar."""

    def __init__(self, personal_id: int, parent: Optional[QWidget] = None,
                 pin_actual: Optional[str] = None, obligatorio: bool = False) -> None:
        if obligatorio:
            titulo = "Cambio de PIN obligatorio"
            explicacion = (f"Tu PIN todavía es el de fábrica ({PIN_DEFAULT}). Por seguridad tenés que "
                           "definir un PIN propio antes de continuar.")
        else:
            titulo = "Cambiar PIN de Seguridad"
            explicacion = "Para cambiar el PIN confirmá primero el PIN actual."
        super().__init__(titulo, explicacion, "Guardar PIN", parent)
        if obligatorio:
            self.botones.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar (no continuar)")
        self._personal_id = personal_id
        self._pin_actual = pin_actual
        self.entry_actual: Optional[QLineEdit] = None
        if pin_actual is None:
            self.entry_actual = _campo_secreto(self, ayuda="PIN actual")
            self.form.addRow("PIN actual", self.entry_actual)
        self.entry_nuevo = _campo_secreto(self, ayuda="Solo números, 4 a 10 dígitos")
        self.entry_repetir = _campo_secreto(self)
        self.form.addRow("PIN nuevo", self.entry_nuevo)
        self.form.addRow("Repetir PIN", self.entry_repetir)
        (self.entry_actual or self.entry_nuevo).setFocus()

    def _confirmar(self) -> None:
        actual = self._pin_actual if self.entry_actual is None else self.entry_actual.text()
        nuevo = self.entry_nuevo.text().strip()
        if nuevo != self.entry_repetir.text().strip():
            self._error("Los dos PIN nuevos no coinciden.", self.entry_repetir)
            return
        try:
            cambiar_pin(self._personal_id, actual or "", nuevo)
        except PinError as e:
            campo = self.entry_actual if "actual" in str(e).lower() and self.entry_actual else self.entry_nuevo
            self._error(str(e), campo)
            return
        self.accept()


class DialogoResetPinMaestra(_DialogoPinBase):
    """Reseteo con la clave maestra del cuartel: fija un PIN temporal sin
    conocer el anterior. Vacío = PIN de fábrica (se fuerza a cambiarlo en el
    próximo uso). `pin_resultante` queda con el PIN fijado."""

    def __init__(self, personal_id: int, nombre: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Resetear PIN con Clave Maestra",
            f"Resetear el PIN de {nombre}. Ingresá la clave maestra del cuartel y un PIN temporal. "
            f"Si dejás el PIN temporal vacío vuelve al de fábrica ({PIN_DEFAULT}) y el bombero va a "
            "tener que cambiarlo la próxima vez que lo use.",
            "Resetear PIN", parent)
        self._personal_id = personal_id
        self.pin_resultante: Optional[str] = None
        self.entry_maestra = _campo_secreto(self, largo=128, ayuda="Clave maestra del cuartel")
        self.entry_temporal = _campo_secreto(self, ayuda=f"Vacío = {PIN_DEFAULT}")
        self.entry_repetir = _campo_secreto(self)
        self.form.addRow("Clave maestra", self.entry_maestra)
        self.form.addRow("PIN temporal", self.entry_temporal)
        self.form.addRow("Repetir PIN", self.entry_repetir)
        self.entry_maestra.setFocus()

    def _confirmar(self) -> None:
        espera = segundos_bloqueo_maestra()
        if espera:
            self._error(f"Demasiados intentos fallidos. Esperá {espera} s y probá de nuevo.")
            return
        temporal = self.entry_temporal.text().strip()
        if temporal != self.entry_repetir.text().strip():
            self._error("Los dos PIN temporales no coinciden.", self.entry_repetir)
            return
        try:
            self.pin_resultante = resetear_pin_con_clave_maestra(
                self._personal_id, self.entry_maestra.text(), temporal)
        except PinError as e:
            campo = self.entry_maestra if "maestra" in str(e).lower() else self.entry_temporal
            self._error(str(e), campo)
            return
        self.accept()
