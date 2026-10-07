"""
Diálogo "Reporte gerencial": elige un rango de fechas y exporta a Excel las
métricas de personal y vehículos (app/services/statistics_service.py +
app/reports/estadisticas_generator.py). Se abre desde la página
Estadísticas (Historial).

Atajos de período:
  - Mes actual: del 1° del mes a hoy.
  - Año actual: del 1° de enero a hoy.
  - Último trimestre: el último trimestre calendario COMPLETO (en octubre:
    1/7 al 30/9), el que se usa en los informes trimestrales.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.reports.estadisticas_generator import generar_reporte_estadisticas
from app.ui import theme


def rango_mes_actual(hoy: Optional[date] = None) -> Tuple[date, date]:
    hoy = hoy or date.today()
    return hoy.replace(day=1), hoy


def rango_anio_actual(hoy: Optional[date] = None) -> Tuple[date, date]:
    hoy = hoy or date.today()
    return date(hoy.year, 1, 1), hoy


def rango_ultimo_trimestre(hoy: Optional[date] = None) -> Tuple[date, date]:
    """Último trimestre calendario completo (ene-mar, abr-jun, jul-sep, oct-dic)."""
    hoy = hoy or date.today()
    inicio_actual = date(hoy.year, 3 * ((hoy.month - 1) // 3) + 1, 1)
    fin = inicio_actual - timedelta(days=1)
    return date(fin.year, 3 * ((fin.month - 1) // 3) + 1, 1), fin


def _a_qdate(valor: date) -> QDate:
    return QDate(valor.year, valor.month, valor.day)


def _a_date(valor: QDate) -> date:
    return date(valor.year(), valor.month(), valor.day())


class DialogoReporteGerencial(QDialog):
    """Rango de fechas + "Exportar Reporte a Excel"; ofrece abrir el archivo."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Reporte gerencial — Personal y Vehículos")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.ultima_ruta: Optional[Path] = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        explicacion = QLabel(
            "Servicios cerrados entre las dos fechas (inclusive): asistencias y horas por bombero "
            "(en escena y en cuartel) y salidas y horas por móvil. Se exporta a Excel con el formato "
            "de las planillas oficiales.", self)
        explicacion.setWordWrap(True)
        explicacion.setProperty("muted", True)
        layout.addWidget(explicacion)

        form = QFormLayout()
        self.fecha_desde = self._crear_fecha()
        self.fecha_hasta = self._crear_fecha()
        form.addRow("Desde", self.fecha_desde)
        form.addRow("Hasta", self.fecha_hasta)
        layout.addLayout(form)

        rapidos = QHBoxLayout()
        for texto, funcion, ayuda in (
            ("Mes actual", rango_mes_actual, "Del 1° de este mes a hoy"),
            ("Año actual", rango_anio_actual, "Del 1° de enero a hoy"),
            ("Último trimestre", rango_ultimo_trimestre, "El último trimestre calendario completo"),
        ):
            boton = QPushButton(texto, self)
            boton.setObjectName("botonAhora")
            boton.setToolTip(ayuda)
            boton.clicked.connect(lambda _=False, f=funcion: self.set_rango(*f()))
            rapidos.addWidget(boton)
        rapidos.addStretch(1)
        layout.addLayout(rapidos)

        self.label_estado = QLabel("", self)
        self.label_estado.setWordWrap(True)
        self.label_estado.hide()
        layout.addWidget(self.label_estado)

        botones = QHBoxLayout()
        botones.addStretch(1)
        boton_cerrar = QPushButton("Cerrar", self)
        boton_cerrar.clicked.connect(self.reject)
        self.boton_exportar = QPushButton("📊 Exportar Reporte a Excel", self)
        self.boton_exportar.setObjectName("botonGuardar")
        self.boton_exportar.setDefault(True)
        self.boton_exportar.clicked.connect(self.exportar)
        botones.addWidget(boton_cerrar)
        botones.addWidget(self.boton_exportar)
        layout.addLayout(botones)

        self.fecha_desde.dateChanged.connect(self._validar_rango)
        self.fecha_hasta.dateChanged.connect(self._validar_rango)
        self.set_rango(*rango_mes_actual())

    def _crear_fecha(self) -> QDateEdit:
        campo = QDateEdit(self)
        campo.setDisplayFormat("dd/MM/yyyy")
        campo.setCalendarPopup(True)
        campo.setMaximumDate(QDate.currentDate().addYears(1))
        return campo

    def set_rango(self, desde: date, hasta: date) -> None:
        self.fecha_desde.setDate(_a_qdate(desde))
        self.fecha_hasta.setDate(_a_qdate(hasta))

    def rango(self) -> Tuple[date, date]:
        return _a_date(self.fecha_desde.date()), _a_date(self.fecha_hasta.date())

    def _validar_rango(self) -> bool:
        desde, hasta = self.rango()
        invalido = hasta < desde
        theme.marcar_invalido(self.fecha_hasta, invalido)
        self.boton_exportar.setEnabled(not invalido)
        self._mostrar_estado("La fecha 'Hasta' es anterior a 'Desde'." if invalido else "", "error")
        return not invalido

    def _mostrar_estado(self, texto: str, tono: str) -> None:
        self.label_estado.setText(texto)
        theme.set_tono(self.label_estado, tono)
        self.label_estado.setVisible(bool(texto))

    def exportar(self) -> None:
        if not self._validar_rango():
            return
        desde, hasta = self.rango()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ruta, reporte = generar_reporte_estadisticas(desde, hasta)
        except Exception as e:  # noqa: BLE001 - se informa sin cerrar el diálogo
            QApplication.restoreOverrideCursor()
            self._mostrar_estado(f"No se pudo generar el reporte: {e}", "error")
            QMessageBox.critical(self, "Reporte gerencial", f"No se pudo generar el reporte:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        self.ultima_ruta = ruta
        resumen = (f"{reporte.servicios} servicio(s) · {reporte.bomberos_participantes} bombero(s) · "
                   f"{reporte.salidas_moviles} salida(s) de móviles · {reporte.horas_hombre:,.1f} horas-hombre")
        self._mostrar_estado(f"✅ Reporte guardado en {ruta}\n{resumen}", "ok")
        respuesta = QMessageBox.question(
            self, "Reporte gerencial generado",
            f"Se guardó el reporte:\n{ruta}\n\n{resumen}\n\n¿Abrirlo ahora?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if respuesta == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(ruta)))


def abrir_reporte_gerencial(parent: Optional[QWidget] = None) -> None:
    DialogoReporteGerencial(parent).exec()
