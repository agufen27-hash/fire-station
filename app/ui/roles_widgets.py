"""
Configuración → "Usuarios y Roles": lista el personal activo con un combo
de rol (Bombero / Jefatura / Administrador) por fila.

Guardar exige autorización institucional, no el rol del operador: la
cuenta de RUBA guardada en Configuración (usuario + clave) o, si todavía no
hay credenciales RUBA, la clave maestra del cuartel. Así el primer ADMIN se
puede nombrar en una instalación nueva y un ADMIN no puede escalar a otro
sin la credencial del cuartel. La validación vive en personal_service.
"""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.roles import NOMBRES_ROL, ROLES
from app.core.security import segundos_bloqueo_maestra
from app.services.personal_service import (
    asignar_roles,
    autorizacion_roles_usa_ruba,
    autorizar_cambio_roles,
    personal_con_roles,
)
from app.ui import theme

COL_NOMBRE, COL_LEGAJO, COL_ROL = range(3)


class DialogoAutorizacionRoles(QDialog):
    """Pide la cuenta institucional de RUBA (o la clave maestra si no hay
    cuenta guardada). Se cierra aceptado solo con la credencial correcta."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Autorizar cambio de roles")
        self.setModal(True)
        self.setMinimumWidth(400)
        self.usa_ruba = autorizacion_roles_usa_ruba()
        layout = QVBoxLayout(self)
        texto = ("Para cambiar roles ingresá la cuenta institucional de RUBA configurada en el sistema."
                 if self.usa_ruba else
                 "Todavía no hay credenciales de RUBA guardadas: autorizá con la clave maestra del cuartel.")
        explicacion = QLabel(texto, self)
        explicacion.setWordWrap(True)
        layout.addWidget(explicacion)
        form = QFormLayout()
        self.entry_usuario: Optional[QLineEdit] = None
        if self.usa_ruba:
            self.entry_usuario = QLineEdit(self)
            self.entry_usuario.setPlaceholderText("Usuario de RUBA")
            form.addRow("Usuario RUBA", self.entry_usuario)
        self.entry_clave = QLineEdit(self)
        self.entry_clave.setEchoMode(QLineEdit.EchoMode.Password)
        self.entry_clave.setMaxLength(128)
        form.addRow("Clave RUBA" if self.usa_ruba else "Clave maestra", self.entry_clave)
        layout.addLayout(form)
        self.label_error = QLabel("", self)
        self.label_error.setWordWrap(True)
        theme.set_tono(self.label_error, "error")
        self.label_error.hide()
        layout.addWidget(self.label_error)
        botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        botones.button(QDialogButtonBox.StandardButton.Ok).setText("Autorizar y guardar")
        botones.accepted.connect(self._confirmar)
        botones.rejected.connect(self.reject)
        layout.addWidget(botones)
        (self.entry_usuario or self.entry_clave).setFocus()

    def _confirmar(self) -> None:
        if not self.usa_ruba:
            espera = segundos_bloqueo_maestra()
            if espera:
                self._error(f"Demasiados intentos fallidos. Esperá {espera} s y probá de nuevo.")
                return
        usuario = self.entry_usuario.text() if self.entry_usuario is not None else ""
        if not autorizar_cambio_roles(usuario, self.entry_clave.text()):
            self._error("Credencial de RUBA incorrecta." if self.usa_ruba else "Clave maestra incorrecta.")
            return
        self.accept()

    def _error(self, texto: str) -> None:
        self.entry_clave.clear()
        self.entry_clave.setFocus()
        self.label_error.setText(texto)
        self.label_error.show()


class PanelUsuariosRoles(QGroupBox):
    """Tabla personal activo × rol. `roles_cambiados` se emite tras guardar."""

    roles_cambiados = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Usuarios y Roles", parent)
        self._roles_guardados: Dict[int, str] = {}
        layout = QVBoxLayout(self)
        ayuda = QLabel(
            "Bombero: planillas, firmas y estadísticas. Jefatura: además ve todos los legajos en solo lectura. "
            "Administrador: control total (personal, unidades y configuración). Guardar pide la cuenta "
            "de RUBA del cuartel (o la clave maestra si no hay una guardada).", self)
        ayuda.setWordWrap(True)
        ayuda.setProperty("muted", True)
        layout.addWidget(ayuda)

        self.tabla = QTableWidget(self)
        self.tabla.setColumnCount(3)
        self.tabla.setHorizontalHeaderLabels(["Bombero", "Legajo", "Rol"])
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.horizontalHeader().setSectionResizeMode(COL_NOMBRE, QHeaderView.ResizeMode.Stretch)
        self.tabla.setColumnWidth(COL_ROL, 260)
        self.tabla.setMinimumHeight(220)
        theme.estilizar_tabla(self.tabla)
        layout.addWidget(self.tabla)

        fila = QHBoxLayout()
        self.label_estado = QLabel("", self)
        self.label_estado.setWordWrap(True)
        fila.addWidget(self.label_estado, 1)
        self.boton_guardar = QPushButton("🔐 Guardar roles", self)
        self.boton_guardar.setObjectName("botonGuardar")
        self.boton_guardar.clicked.connect(self.guardar)
        fila.addWidget(self.boton_guardar)
        layout.addLayout(fila)

    def refrescar(self) -> None:
        filas = personal_con_roles()
        self._roles_guardados = {pid: rol for pid, _n, _l, rol in filas}
        self.tabla.setRowCount(len(filas))
        for i, (personal_id, nombre, legajo, rol) in enumerate(filas):
            self.tabla.setItem(i, COL_NOMBRE, QTableWidgetItem(nombre))
            self.tabla.setItem(i, COL_LEGAJO, QTableWidgetItem(legajo))
            combo = QComboBox(self.tabla)
            for valor in ROLES:
                combo.addItem(NOMBRES_ROL[valor], valor)
            combo.setCurrentIndex(combo.findData(rol))
            combo.setProperty("personal_id", personal_id)
            combo.currentIndexChanged.connect(self._actualizar_estado)
            self.tabla.setCellWidget(i, COL_ROL, combo)
        self.tabla.resizeRowsToContents()
        self._actualizar_estado()

    def cambios_pendientes(self) -> Dict[int, str]:
        cambios: Dict[int, str] = {}
        for i in range(self.tabla.rowCount()):
            combo = self.tabla.cellWidget(i, COL_ROL)
            personal_id = combo.property("personal_id")
            if combo.currentData() != self._roles_guardados.get(personal_id):
                cambios[personal_id] = combo.currentData()
        return cambios

    def _actualizar_estado(self, *_args) -> None:
        pendientes = len(self.cambios_pendientes())
        self.boton_guardar.setEnabled(bool(pendientes))
        self.label_estado.setText(f"{pendientes} cambio(s) sin guardar." if pendientes else "")
        theme.set_tono(self.label_estado, "alerta")

    def guardar(self) -> bool:
        cambios = self.cambios_pendientes()
        if not cambios:
            return False
        if DialogoAutorizacionRoles(self).exec() != QDialog.DialogCode.Accepted:
            return False
        try:
            cantidad = asignar_roles(cambios)
        except ValueError as e:
            self.label_estado.setText(f"✖ {e}")
            theme.set_tono(self.label_estado, "error")
            return False
        self.refrescar()
        self.label_estado.setText(f"✅ {cantidad} rol(es) actualizados.")
        theme.set_tono(self.label_estado, "ok")
        self.roles_cambiados.emit()
        return True
