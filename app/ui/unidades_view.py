"""
Panel "Unidades" de la página Personal y Unidades: tabla de móviles con
los datos del 'Reporte de vehiculos' de RUBA (tipo, marca/modelo, año,
estado) y el importador desde Excel.

El Excel lo elige SIEMPRE el usuario con QFileDialog: no hay rutas fijas.
Eliminar: si la unidad figura en algún parte se ofrece la baja lógica
(estado "Baja", conserva los partes); si no, se borra de verdad y no
reaparece (el arranque ya no la recrea desde ruba_mapping.json).
Editar / dar de baja / reactivar siguen en MainWindow (callbacks).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.db import dar_de_baja_movil, eliminar_movil, get_session, importar_vehiculos_excel, usos_movil
from app.models import Movil
from app.services.ruba_importer import ESTADO_MOVIL_BAJA
from app.ui import theme

COLUMNAS = ["Unidad", "Tipo", "Marca / Modelo", "Año", "Estado", "Acciones"]
COL_ACCIONES = len(COLUMNAS) - 1

AccionMovil = Callable[[int], None]


class PanelUnidades(QGroupBox):
    """Tabla de unidades + "📥 Importar Unidades desde Excel (RUBA)"."""

    # Tras importar / eliminar / dar de baja: MainWindow refresca los combos
    # de móviles del formulario de parte.
    unidades_cambiadas = Signal()
    # Tras cada recarga de la tabla (MainWindow reaplica el filtro de búsqueda).
    recargada = Signal()

    def __init__(self, crear_acciones: Callable[[int, bool], QWidget], parent: Optional[QWidget] = None) -> None:
        """`crear_acciones(movil_id, activo)` arma los botones de la fila
        (Editar / Dar de baja / Eliminar) que ya define MainWindow."""
        super().__init__("Unidades / Móviles", parent)
        self._crear_acciones = crear_acciones
        self.col_acciones = COL_ACCIONES

        layout = QVBoxLayout(self)
        fila = QHBoxLayout()
        self.boton_importar = QPushButton("📥 Importar Unidades desde Excel (RUBA)", self)
        self.boton_importar.setToolTip("Elegí el 'Reporte de vehiculos' exportado de RUBA (.xlsx) desde cualquier carpeta")
        self.boton_importar.clicked.connect(self.importar_desde_excel)
        fila.addWidget(self.boton_importar)
        fila.addStretch(1)
        layout.addLayout(fila)

        self.tabla = QTableWidget(self)
        self.tabla.setColumnCount(len(COLUMNAS))
        self.tabla.setHorizontalHeaderLabels(COLUMNAS)
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.verticalHeader().setVisible(False)
        cabecera = self.tabla.horizontalHeader()
        cabecera.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        cabecera.setSectionResizeMode(COL_ACCIONES, QHeaderView.ResizeMode.Fixed)
        self.tabla.setColumnWidth(COL_ACCIONES, 270)
        theme.estilizar_tabla(self.tabla)
        layout.addWidget(self.tabla)

    # -- Tabla ------------------------------------------------------------------

    def recargar(self) -> None:
        with get_session() as session:
            filas = [
                (m.id, m.nombre_identificador, m.tipo, " ".join(x for x in (m.marca, m.modelo) if x),
                 m.anio, m.estado, m.activo)
                for m in session.query(Movil).order_by(Movil.nombre_identificador)
            ]
        self.tabla.setRowCount(len(filas))
        for i, (movil_id, nombre, tipo, marca_modelo, anio, estado, activo) in enumerate(filas):
            self.tabla.setItem(i, 0, QTableWidgetItem(nombre))
            item_tipo = QTableWidgetItem(tipo or "—")
            item_tipo.setToolTip(tipo or "")
            self.tabla.setItem(i, 1, item_tipo)
            self.tabla.setItem(i, 2, QTableWidgetItem(marca_modelo or "—"))
            item_anio = QTableWidgetItem(str(anio) if anio else "—")
            item_anio.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tabla.setItem(i, 3, item_anio)
            # Sin estado de RUBA (unidad cargada a mano): el texto de siempre.
            texto_estado = estado or ("Operativo" if activo else "Fuera de servicio")
            color = "verde_texto" if activo else ("rojo" if estado == ESTADO_MOVIL_BAJA else "ambar")
            item_estado = QTableWidgetItem(texto_estado)
            item_estado.setForeground(QColor(theme.color(color)))
            self.tabla.setItem(i, 4, item_estado)
            self.tabla.setCellWidget(i, COL_ACCIONES, self._crear_acciones(movil_id, activo))
        self.tabla.resizeRowsToContents()
        self.recargada.emit()

    # -- Importación ------------------------------------------------------------

    def importar_desde_excel(self) -> None:
        ruta_texto, _ = QFileDialog.getOpenFileName(
            self, "Importar unidades (Reporte de vehiculos de RUBA)", "", "Excel (*.xlsx *.xlsm)"
        )
        if not ruta_texto:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            resumen = importar_vehiculos_excel(Path(ruta_texto))
        except (OSError, ValueError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "No se pudo importar las unidades", str(e))
            return
        except Exception as e:  # noqa: BLE001 - base u openpyxl: la transacción ya se revirtió
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "No se pudo importar las unidades",
                                 f"{type(e).__name__}: {e}\n\nNo se modificó ninguna unidad.")
            return
        QApplication.restoreOverrideCursor()

        self.recargar()
        self.unidades_cambiadas.emit()
        texto = resumen.texto("unidades")
        if resumen.avisos:
            texto += "\n\nAvisos:\n• " + "\n• ".join(resumen.avisos)
        QMessageBox.information(self, "Unidades importadas", texto)

    # -- Eliminación ------------------------------------------------------------

    def eliminar(self, movil_id: int) -> None:
        with get_session() as session:
            movil = session.get(Movil, movil_id)
            if movil is None:
                return
            nombre, ya_de_baja = movil.nombre_identificador, movil.estado == ESTADO_MOVIL_BAJA and not movil.activo
            usos = usos_movil(session, movil_id)

        if usos:
            self._ofrecer_baja(movil_id, nombre, usos, ya_de_baja)
            return

        respuesta = QMessageBox.question(
            self, "Eliminar unidad",
            f"¿Eliminar definitivamente la unidad {nombre}?\n\n"
            "No figura en ningún parte. Se borra de la base y no vuelve a aparecer "
            "(salvo que la importes de nuevo desde el Reporte de vehiculos).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return
        restantes = eliminar_movil(movil_id)
        if restantes:  # alguien la usó en un parte mientras el diálogo estaba abierto
            self._ofrecer_baja(movil_id, nombre, restantes, ya_de_baja)
            return
        self.recargar()
        self.unidades_cambiadas.emit()

    def _ofrecer_baja(self, movil_id: int, nombre: str, usos: int, ya_de_baja: bool) -> None:
        """Con partes asociados no se borra (se romperían esos partes y las
        planillas): se sugiere la baja lógica."""
        if ya_de_baja:
            QMessageBox.information(
                self, "No se puede eliminar",
                f"La unidad {nombre} figura en {usos} registro(s) de partes, así que no se borra para no "
                "romper el historial.\n\nYa está dada de BAJA: no se ofrece para nuevas salidas.",
            )
            return
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Warning)
        caja.setWindowTitle("La unidad tiene partes asociados")
        caja.setText(f"La unidad {nombre} figura en {usos} registro(s) de partes (salidas / dotaciones).")
        caja.setInformativeText(
            "Borrarla rompería esos partes y sus planillas. En su lugar se puede dar de BAJA: "
            "queda en el historial pero deja de ofrecerse para nuevas salidas.\n\n¿Dar de baja la unidad?"
        )
        boton_baja = caja.addButton("Dar de baja", QMessageBox.ButtonRole.AcceptRole)
        caja.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        caja.setDefaultButton(boton_baja)
        caja.exec()
        if caja.clickedButton() is not boton_baja:
            return
        dar_de_baja_movil(movil_id)
        self.recargar()
        self.unidades_cambiadas.emit()
