"""
Legajo digital de un bombero (Fase 8): ficha táctica, gestión de firma
electrónica y PIN personal, historial automático de salidas y gestor
documental tipado. `main_window.py` lo embebe en la página Documentación,
mostrándolo recién después de validar el PIN del bombero elegido.
"""

from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import joinedload

from app.db import get_session
from app.models import (
    TIPOS_DOCUMENTO,
    DocumentoPersonal,
    DotacionSalida,
    Incidente,
    Personal,
    RolDotacion,
    SalidaUnidad,
    normalizar_tipo_documento,
)
from app.paths import get_writable_dir
from app.reports.excel_generator import abrir_para_impresion
from app.ui import theme
from app.ui.widgets import FirmaPad

ROL_ETIQUETA = {
    RolDotacion.A_CARGO.value: "Jefe de Dotación",
    RolDotacion.CHOFER.value: "Chofer",
    RolDotacion.BOMBERO.value: "Bombero",
}


def ruta_firma_legajo(numero_legajo: str) -> Path:
    return get_writable_dir("data") / "firmas" / "legajos" / f"{numero_legajo}.png"


def carpeta_legajo(numero_legajo: str) -> Path:
    carpeta = get_writable_dir("data") / "legajos" / numero_legajo
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


class LegajoPrivadoWidget(QWidget):
    """Vista privada de un legajo -- se muestra recién tras validar el PIN
    del bombero elegido (ver `main_window.py`)."""

    volver_solicitado = Signal()
    editar_datos_solicitado = Signal(int)  # personal_id: MainWindow abre el diálogo de edición

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._personal_id: Optional[int] = None
        self._numero_legajo: Optional[str] = None
        self._construir_ui()

    def _construir_ui(self) -> None:
        # Con scroll, como el resto de las páginas: sin él, en pantallas bajas
        # (notebook, zoom de Windows al 125 %) Qt aplastaba la ficha y los
        # textos -- "N° de Legajo" incluido -- quedaban cortados arriba y abajo.
        exterior = QVBoxLayout(self)
        exterior.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        exterior.addWidget(scroll)
        contenido = QWidget(scroll)
        scroll.setWidget(contenido)

        layout = QVBoxLayout(contenido)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)

        fila_acciones = QHBoxLayout()
        boton_volver = QPushButton("← Volver", self)
        boton_volver.clicked.connect(self.volver_solicitado.emit)
        fila_acciones.addWidget(boton_volver)
        fila_acciones.addStretch(1)
        boton_editar = QPushButton("✏️ Editar datos personales", self)
        boton_editar.setObjectName("botonAhora")
        boton_editar.setToolTip("DNI, teléfono, jerarquía, grupo sanguíneo, antigüedad y estado")
        boton_editar.clicked.connect(
            lambda: self._personal_id is not None and self.editar_datos_solicitado.emit(self._personal_id)
        )
        fila_acciones.addWidget(boton_editar)
        layout.addLayout(fila_acciones)

        fila_superior = QHBoxLayout()
        fila_superior.setSpacing(16)

        caja_ficha = QGroupBox("Ficha de Legajo", self)
        form = QFormLayout(caja_ficha)
        self._label_legajo = QLabel(caja_ficha)
        self._label_legajo.setObjectName("pageTitle")
        # Título grande (22 px): alto mínimo propio + aire arriba/abajo, para que
        # "Legajo" (con la g y la j que bajan) nunca quede recortado.
        self._label_legajo.setContentsMargins(0, 4, 0, 6)
        self._label_legajo.setMinimumHeight(self._label_legajo.fontMetrics().height() + 14)
        self._label_nombre = QLabel(caja_ficha)
        self._label_dni = QLabel(caja_ficha)
        self._label_telefono = QLabel(caja_ficha)
        self._label_grupo_sanguineo = QLabel(caja_ficha)
        self._label_jerarquia = QLabel(caja_ficha)
        self._label_antiguedad = QLabel(caja_ficha)
        self._label_estado = QLabel(caja_ficha)
        form.addRow(self._label_legajo)
        form.addRow("Apellido y Nombre", self._label_nombre)
        form.addRow("DNI", self._label_dni)
        form.addRow("Teléfono", self._label_telefono)
        form.addRow("Grupo Sanguíneo", self._label_grupo_sanguineo)
        form.addRow("Jerarquía", self._label_jerarquia)
        form.addRow("Antigüedad", self._label_antiguedad)
        form.addRow("Estado", self._label_estado)

        caja_firma = QGroupBox("Firma Digital", self)
        layout_firma = QVBoxLayout(caja_firma)
        self._label_firma_actual = QLabel(caja_firma)
        self._label_firma_actual.setMinimumHeight(80)
        self._label_firma_actual.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # La firma se muestra sobre "papel" blanco en ambos temas (legibilidad del trazo).
        self._label_firma_actual.setStyleSheet(
            "background:#ffffff;border:1px solid #cfd6e2;border-radius:8px;color:#8a94a6;"
        )
        layout_firma.addWidget(self._label_firma_actual, 1)
        fila_botones_firma = QHBoxLayout()
        boton_firmar = QPushButton("Firmar / Re-firmar", caja_firma)
        boton_firmar.setObjectName("botonAhora")
        boton_firmar.clicked.connect(self._abrir_dialogo_firma)
        boton_pin = QPushButton("Cambiar PIN", caja_firma)
        boton_pin.clicked.connect(self._abrir_dialogo_pin)
        fila_botones_firma.addWidget(boton_firmar)
        fila_botones_firma.addWidget(boton_pin)
        layout_firma.addLayout(fila_botones_firma)

        fila_superior.addWidget(caja_ficha, 2)
        fila_superior.addWidget(caja_firma, 1)
        layout.addLayout(fila_superior)

        caja_historial = QGroupBox("Historial de Salidas", self)
        layout_historial = QVBoxLayout(caja_historial)
        self._tabla_historial = QTableWidget(caja_historial)
        self._tabla_historial.setColumnCount(6)
        self._tabla_historial.setHorizontalHeaderLabels(
            ["N° Parte", "Fecha", "Tipo de Siniestro", "Unidad", "Rol", "Horas de servicio"]
        )
        self._tabla_historial.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tabla_historial.verticalHeader().setVisible(False)
        self._tabla_historial.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._tabla_historial.setMinimumHeight(160)
        theme.estilizar_tabla(self._tabla_historial)
        layout_historial.addWidget(self._tabla_historial)
        self._label_totales_historial = QLabel(caja_historial)
        self._label_totales_historial.setProperty("muted", True)
        layout_historial.addWidget(self._label_totales_historial)
        layout.addWidget(caja_historial)

        caja_documentos = QGroupBox("Documentos del Legajo", self)
        layout_documentos = QVBoxLayout(caja_documentos)
        self._tabla_documentos = QTableWidget(caja_documentos)
        self._tabla_documentos.setColumnCount(4)
        self._tabla_documentos.setHorizontalHeaderLabels(["Tipo", "Archivo", "Fecha", "Acciones"])
        self._tabla_documentos.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tabla_documentos.verticalHeader().setVisible(False)
        self._tabla_documentos.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tabla_documentos.setMinimumHeight(120)
        theme.estilizar_tabla(self._tabla_documentos)
        layout_documentos.addWidget(self._tabla_documentos)
        boton_agregar_doc = QPushButton("+ Agregar Documento", caja_documentos)
        boton_agregar_doc.setObjectName("botonAhora")
        boton_agregar_doc.clicked.connect(self._agregar_documento)
        layout_documentos.addWidget(boton_agregar_doc)
        layout.addWidget(caja_documentos)

    # -- Carga de la ficha ------------------------------------------------------

    def mostrar(self, personal_id: int) -> None:
        self._personal_id = personal_id
        with get_session() as session:
            p = session.get(Personal, personal_id)
            self._numero_legajo = p.legajo_display()
            ruta_firma = p.ruta_firma
            self._label_legajo.setText(f"N° de Legajo: {self._numero_legajo}")
            self._label_legajo.ensurePolished()  # fuente real del tema (22 px) antes de medir
            self._label_legajo.setMinimumHeight(self._label_legajo.fontMetrics().height() + 14)
            self._label_nombre.setText(p.nombre_completo())
            self._label_dni.setText(p.dni)
            self._label_telefono.setText(p.telefono or "—")
            self._label_grupo_sanguineo.setText(p.grupo_sanguineo or "—")
            self._label_jerarquia.setText(p.jerarquia or "—")
            self._label_antiguedad.setText(self._texto_antiguedad(p.antiguedad_fecha))
            self._label_estado.setText(p.estado)

        self._actualizar_vista_firma(ruta_firma)
        self._cargar_historial(personal_id)
        self._cargar_documentos(personal_id)

    @staticmethod
    def _texto_antiguedad(fecha_ingreso: Optional[date]) -> str:
        if not fecha_ingreso:
            return "—"
        anios = (date.today() - fecha_ingreso).days // 365
        return f"{fecha_ingreso.strftime('%d/%m/%Y')} ({anios} año{'s' if anios != 1 else ''} en servicio)"

    def _actualizar_vista_firma(self, ruta_firma: Optional[str]) -> None:
        if ruta_firma and Path(ruta_firma).exists():
            pixmap = QPixmap(ruta_firma)
            self._label_firma_actual.setPixmap(
                pixmap.scaled(
                    220, 70, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
            )
            self._label_firma_actual.setText("")
        else:
            self._label_firma_actual.setPixmap(QPixmap())
            self._label_firma_actual.setText("Sin firma registrada")

    # -- Historial automático de salidas -----------------------------------------

    def _cargar_historial(self, personal_id: int) -> None:
        with get_session() as session:
            filas = (
                session.query(DotacionSalida)
                .options(
                    joinedload(DotacionSalida.incidente).joinedload(Incidente.tipo),
                    joinedload(DotacionSalida.movil),
                    joinedload(DotacionSalida.salida_unidad),
                )
                .filter(DotacionSalida.personal_id == personal_id)
                .join(Incidente, DotacionSalida.incidente_id == Incidente.id)
                .order_by(Incidente.fecha.desc())
                .all()
            )
            datos = []
            total_horas = 0.0
            for f in filas:
                inc = f.incidente
                tipo = inc.tipo.nombre if inc.tipo else "—"
                movil_nombre = f.movil.nombre_identificador if f.movil else "—"
                horas = self._horas_servicio(f.salida_unidad)
                if horas is not None:
                    total_horas += horas
                datos.append((
                    inc.numero_parte,
                    inc.fecha.strftime("%d/%m/%Y") if inc.fecha else "—",
                    tipo,
                    movil_nombre,
                    ROL_ETIQUETA.get(f.rol, f.rol),
                    f"{horas:.1f} h" if horas is not None else "—",
                ))

        self._tabla_historial.setRowCount(len(datos))
        for fila_idx, valores in enumerate(datos):
            for col, valor in enumerate(valores):
                self._tabla_historial.setItem(fila_idx, col, QTableWidgetItem(valor))
        self._tabla_historial.resizeRowsToContents()
        self._label_totales_historial.setText(
            f"Total: {len(datos)} salida(s) — {total_horas:.1f} horas de servicio acumuladas"
        )

    @staticmethod
    def _horas_servicio(su: Optional[SalidaUnidad]) -> Optional[float]:
        if su is None or su.hora_salida is None or su.hora_regreso is None:
            return None
        inicio = su.hora_salida.hour * 60 + su.hora_salida.minute
        fin = su.hora_regreso.hour * 60 + su.hora_regreso.minute
        minutos = fin - inicio
        if minutos < 0:
            minutos += 24 * 60  # la salida cruzó la medianoche
        return minutos / 60

    # -- Gestor documental tipado -------------------------------------------------

    def _cargar_documentos(self, personal_id: int) -> None:
        with get_session() as session:
            docs = (
                session.query(DocumentoPersonal)
                .filter(DocumentoPersonal.personal_id == personal_id)
                .order_by(DocumentoPersonal.fecha_subida.desc())
                .all()
            )
            datos = [(d.id, d.tipo_documento, d.nombre_archivo, d.ruta_archivo, d.fecha_subida) for d in docs]

        self._tabla_documentos.setRowCount(len(datos))
        for fila_idx, (doc_id, tipo, nombre, ruta, fecha) in enumerate(datos):
            self._tabla_documentos.setItem(fila_idx, 0, QTableWidgetItem(normalizar_tipo_documento(tipo)))
            self._tabla_documentos.setItem(fila_idx, 1, QTableWidgetItem(nombre))
            self._tabla_documentos.setItem(fila_idx, 2, QTableWidgetItem(fecha.strftime("%d/%m/%Y %H:%M")))

            widget = QWidget()
            fila_botones = QHBoxLayout(widget)
            fila_botones.setContentsMargins(2, 2, 2, 2)
            fila_botones.setSpacing(4)
            boton_abrir = QPushButton("Abrir", widget)
            boton_abrir.clicked.connect(lambda _=False, r=ruta: abrir_para_impresion(Path(r)))
            boton_descargar = QPushButton("Descargar", widget)
            boton_descargar.clicked.connect(lambda _=False, r=ruta, n=nombre: self._descargar_documento(Path(r), n))
            boton_eliminar = QPushButton("Eliminar", widget)
            boton_eliminar.clicked.connect(lambda _=False, did=doc_id, r=ruta: self._eliminar_documento(did, Path(r)))
            fila_botones.addWidget(boton_abrir)
            fila_botones.addWidget(boton_descargar)
            fila_botones.addWidget(boton_eliminar)
            self._tabla_documentos.setCellWidget(fila_idx, 3, widget)
        self._tabla_documentos.resizeRowsToContents()

    def _agregar_documento(self) -> None:
        if self._personal_id is None or self._numero_legajo is None:
            return

        dialogo = QDialog(self)
        dialogo.setWindowTitle("Agregar Documento")
        layout = QFormLayout(dialogo)
        combo_tipo = QComboBox(dialogo)
        combo_tipo.addItems(TIPOS_DOCUMENTO)
        layout.addRow("Tipo de documento", combo_tipo)

        fila_botones = QHBoxLayout()
        boton_cancelar = QPushButton("Cancelar", dialogo)
        boton_continuar = QPushButton("Seleccionar archivo…", dialogo)
        boton_continuar.setObjectName("botonGuardar")
        boton_continuar.clicked.connect(dialogo.accept)
        boton_cancelar.clicked.connect(dialogo.reject)
        fila_botones.addWidget(boton_cancelar)
        fila_botones.addWidget(boton_continuar)
        layout.addRow(fila_botones)

        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        tipo_elegido = combo_tipo.currentText()

        ruta_texto, _ = QFileDialog.getOpenFileName(
            self, "Seleccionar documento", "", "Documentos (*.pdf *.png *.jpg *.jpeg)"
        )
        if not ruta_texto:
            return

        origen = Path(ruta_texto)
        tipo_limpio = tipo_elegido.replace("°", "").replace(" ", "_")
        fecha_texto = datetime.now().strftime("%Y%m%d_%H%M%S")
        destino = carpeta_legajo(self._numero_legajo) / f"{tipo_limpio}_{fecha_texto}_{origen.name}"

        try:
            shutil.copy(origen, destino)
        except OSError as e:
            QMessageBox.critical(self, "Error al adjuntar", str(e))
            return

        with get_session() as session:
            session.add(DocumentoPersonal(
                personal_id=self._personal_id, tipo_documento=tipo_elegido,
                nombre_archivo=origen.name, ruta_archivo=str(destino),
            ))

        self._cargar_documentos(self._personal_id)

    def _descargar_documento(self, ruta: Path, nombre_sugerido: str) -> None:
        destino_texto, _ = QFileDialog.getSaveFileName(self, "Descargar documento como…", nombre_sugerido)
        if not destino_texto:
            return
        try:
            shutil.copy(ruta, destino_texto)
        except OSError as e:
            QMessageBox.critical(self, "Error al descargar", str(e))
            return
        QMessageBox.information(self, "Descargado", f"Se guardó en:\n{destino_texto}")

    def _eliminar_documento(self, documento_id: int, ruta: Path) -> None:
        respuesta = QMessageBox.question(
            self, "Eliminar documento",
            "¿Eliminar este documento del legajo? Esta acción no se puede deshacer.",
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return
        with get_session() as session:
            doc = session.get(DocumentoPersonal, documento_id)
            if doc is not None:
                session.delete(doc)
        ruta.unlink(missing_ok=True)
        if self._personal_id is not None:
            self._cargar_documentos(self._personal_id)

    # -- Firma digital y PIN -------------------------------------------------------

    def _abrir_dialogo_firma(self) -> None:
        if self._personal_id is None or self._numero_legajo is None:
            return

        dialogo = QDialog(self)
        dialogo.setWindowTitle("Registrar Firma")
        dialogo.resize(420, 280)
        layout = QVBoxLayout(dialogo)
        layout.addWidget(QLabel("Dibujá la firma, o subí una imagen PNG con fondo transparente:", dialogo))

        pad = FirmaPad(dialogo)
        layout.addWidget(pad, 1)

        fila_pad = QHBoxLayout()
        boton_limpiar = QPushButton("Limpiar", dialogo)
        boton_limpiar.clicked.connect(pad.limpiar)
        boton_subir = QPushButton("Subir imagen…", dialogo)

        def _subir_imagen() -> None:
            ruta_texto, _ = QFileDialog.getOpenFileName(dialogo, "Subir firma", "", "Imágenes (*.png *.jpg *.jpeg)")
            if ruta_texto and not pad.cargar_desde_archivo(ruta_texto):
                QMessageBox.warning(dialogo, "Archivo inválido", "No se pudo leer esa imagen.")

        boton_subir.clicked.connect(_subir_imagen)
        fila_pad.addWidget(boton_limpiar)
        fila_pad.addWidget(boton_subir)
        layout.addLayout(fila_pad)

        fila_botones = QHBoxLayout()
        boton_cancelar = QPushButton("Cancelar", dialogo)
        boton_guardar = QPushButton("Guardar Firma", dialogo)
        boton_guardar.setObjectName("botonGuardar")
        boton_guardar.clicked.connect(dialogo.accept)
        boton_cancelar.clicked.connect(dialogo.reject)
        fila_botones.addWidget(boton_cancelar)
        fila_botones.addWidget(boton_guardar)
        layout.addLayout(fila_botones)

        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        if not pad.tiene_firma():
            QMessageBox.warning(self, "Sin firma", "No se registró ningún trazo ni imagen para guardar.")
            return

        ruta_destino = ruta_firma_legajo(self._numero_legajo)
        pad.guardar_png(ruta_destino)
        with get_session() as session:
            p = session.get(Personal, self._personal_id)
            p.ruta_firma = str(ruta_destino)

        self._actualizar_vista_firma(str(ruta_destino))
        QMessageBox.information(self, "Firma registrada", "La firma se guardó correctamente en el legajo.")

    def _abrir_dialogo_pin(self) -> None:
        if self._personal_id is None:
            return

        dialogo = QDialog(self)
        dialogo.setWindowTitle("Cambiar PIN de Seguridad")
        layout = QFormLayout(dialogo)
        entry_nuevo = QLineEdit(dialogo)
        entry_nuevo.setEchoMode(QLineEdit.EchoMode.Password)
        entry_nuevo.setMaxLength(10)
        entry_repetir = QLineEdit(dialogo)
        entry_repetir.setEchoMode(QLineEdit.EchoMode.Password)
        entry_repetir.setMaxLength(10)
        layout.addRow("PIN nuevo", entry_nuevo)
        layout.addRow("Repetir PIN", entry_repetir)

        fila_botones = QHBoxLayout()
        boton_cancelar = QPushButton("Cancelar", dialogo)
        boton_guardar = QPushButton("Guardar", dialogo)
        boton_guardar.setObjectName("botonGuardar")
        boton_guardar.clicked.connect(dialogo.accept)
        boton_cancelar.clicked.connect(dialogo.reject)
        fila_botones.addWidget(boton_cancelar)
        fila_botones.addWidget(boton_guardar)
        layout.addRow(fila_botones)

        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return

        nuevo = entry_nuevo.text().strip()
        if not nuevo or not nuevo.isdigit():
            QMessageBox.warning(self, "PIN inválido", "El PIN tiene que ser numérico.")
            return
        if nuevo != entry_repetir.text().strip():
            QMessageBox.warning(self, "No coincide", "Los dos PIN ingresados no coinciden.")
            return

        with get_session() as session:
            p = session.get(Personal, self._personal_id)
            p.pin = nuevo

        QMessageBox.information(self, "PIN actualizado", "El PIN de seguridad se actualizó correctamente.")
