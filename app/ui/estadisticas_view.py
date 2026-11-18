"""
Estadísticas y KPIs del cuartel (sección propia de la sidebar, separada de
"Planillas", que es la lista operativa de partes).

Arriba, una fila de filtros (período, tipo de salida, vehículo, bombero);
abajo, tarjetas KPI, la estadística personal y los gráficos (servicios por
mes, por tipo, horas por móvil, ranking de bomberos) más la tabla detallada.
Todo sale de app/services/statistics_service.py (partes CERRADOS del
período; los en curso se informan aparte).

Permisos (app/core/roles.py, `ver_estadisticas_completas`):
  - Bombero (o sin operador identificado): KPIs generales del cuartel y SU
    estadística personal (asistencia a siniestros, horas en apresto, horas
    como interviniente y total). Para ver la suya se identifica con su PIN
    (chip 👤). No ve rankings ni datos de otros, ni filtra por vehículo o
    bombero, ni exporta el reporte gerencial.
  - Jefatura, Administrador y Estadística: toda la base -- filtros
    avanzados, ranking y tabla por bombero, reporte gerencial en Excel, y la
    ficha personal de cualquier bombero (filtro "Bombero").
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Tuple

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.roles import permisos_de
from app.db import get_session
from app.models import Movil, Personal, TipoIncidente
from app.services.statistics_service import (
    EstadisticaPersonal,
    ReporteEstadisticas,
    calcular_estadisticas,
    estadistica_personal,
)
from app.ui import theme
from app.ui.reports_window import rango_anio_actual, rango_mes_actual, rango_ultimo_trimestre
from app.ui.widgets import TarjetaKPI
from app.ui.widgets.graficos import GraficoBarras, GraficoColumnas, GraficoDona

PERIODO_MES = "Mes actual"
PERIODO_ANIO = "Año actual"
PERIODO_12_MESES = "Últimos 12 meses"
PERIODO_TRIMESTRE = "Último trimestre"
PERIODO_PERSONALIZADO = "Personalizado"
PERIODOS = [PERIODO_ANIO, PERIODO_MES, PERIODO_12_MESES, PERIODO_TRIMESTRE, PERIODO_PERSONALIZADO]

TODOS = "Todos"

COLUMNAS_BOMBEROS = ["Bombero", "Legajo", "Asistencias", "Como interviniente", "En apresto",
                     "Horas interviniente", "Horas apresto", "Horas totales"]


def formatear_horas(horas: float) -> str:
    total_min = int(round(horas * 60))
    return f"{total_min // 60} h {total_min % 60:02d} min"


def rango_ultimos_12_meses(hoy: Optional[date] = None) -> Tuple[date, date]:
    """Del 1° del mismo mes del año pasado (+1 mes) a hoy: 12 meses completos en el gráfico."""
    hoy = hoy or date.today()
    anio, mes = (hoy.year, hoy.month - 11) if hoy.month > 11 else (hoy.year - 1, hoy.month + 1)
    return date(anio, mes, 1), hoy


def rango_de_periodo(periodo: str, hoy: Optional[date] = None) -> Optional[Tuple[date, date]]:
    return {
        PERIODO_MES: rango_mes_actual,
        PERIODO_ANIO: rango_anio_actual,
        PERIODO_12_MESES: rango_ultimos_12_meses,
        PERIODO_TRIMESTRE: rango_ultimo_trimestre,
    }.get(periodo, lambda _h=None: None)(hoy)


def _a_qdate(valor: date) -> QDate:
    return QDate(valor.year, valor.month, valor.day)


def _a_date(valor: QDate) -> date:
    return date(valor.year(), valor.month(), valor.day())


class _ItemHoras(QTableWidgetItem):
    """Celda "3 h 05 min" que ordena por las horas, no por el texto."""

    def __init__(self, horas: float) -> None:
        super().__init__(formatear_horas(horas))
        self.horas = horas

    def __lt__(self, otro) -> bool:
        return self.horas < getattr(otro, "horas", 0.0)


class TarjetaDato(QFrame):
    """Dato de la estadística personal: valor grande + rótulo + detalle."""

    def __init__(self, titulo: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("kpiCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.label_valor = QLabel("—", self)
        self.label_valor.setObjectName("kpiValor")
        layout.addWidget(self.label_valor)
        label_titulo = QLabel(titulo.upper(), self)
        label_titulo.setObjectName("kpiTitulo")
        label_titulo.setWordWrap(True)
        layout.addWidget(label_titulo)
        self.label_detalle = QLabel("", self)
        self.label_detalle.setProperty("muted", True)
        self.label_detalle.setWordWrap(True)
        layout.addWidget(self.label_detalle)

    def set_valor(self, valor: str, detalle: str = "") -> None:
        self.label_valor.setText(valor)
        self.label_detalle.setText(detalle)
        self.label_detalle.setVisible(bool(detalle))


class PaginaEstadisticas(QWidget):
    """Página "📊 Estadísticas" (ver docstring del módulo)."""

    identificarse_solicitado = Signal()   # MainWindow abre el chip 👤 (PIN)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._operador_id: Optional[int] = None
        self._completa = False
        self.reporte: Optional[ReporteEstadisticas] = None
        self.personal: Optional[EstadisticaPersonal] = None
        self._construir_ui()
        self._cargar_combos()
        self._on_periodo_cambiado()
        self._aplicar_nivel_acceso()

    # -- Construcción de la UI ---------------------------------------------------

    def _construir_ui(self) -> None:
        externo = QVBoxLayout(self)
        externo.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        externo.addWidget(scroll)
        contenido = QWidget()
        scroll.setWidget(contenido)
        layout = QVBoxLayout(contenido)
        layout.setContentsMargins(28, 20, 28, 24)
        layout.setSpacing(16)

        layout.addLayout(self._crear_filtros())
        contenido.setMinimumWidth(0)

        self.label_acceso = QLabel("", contenido)
        self.label_acceso.setObjectName("chip")
        self.label_acceso.setWordWrap(True)
        layout.addWidget(self.label_acceso)

        # KPIs generales del cuartel (los ve todo el mundo).
        fila_kpi = QGridLayout()
        fila_kpi.setSpacing(14)
        self.kpi_servicios = TarjetaKPI("Servicios cerrados", "🚒", contenido)
        self.kpi_horas = TarjetaKPI("Horas de servicio", "⏱️", contenido)
        self.kpi_promedio = TarjetaKPI("Promedio por servicio", "📐", contenido)
        self.kpi_salidas = TarjetaKPI("Salidas de móviles", "🚐", contenido)
        self.kpi_participantes = TarjetaKPI("Bomberos participantes", "👥", contenido)
        self.kpi_horas_hombre = TarjetaKPI("Horas-hombre", "🧑‍🚒", contenido)
        for i, tarjeta in enumerate((self.kpi_servicios, self.kpi_horas, self.kpi_promedio,
                                     self.kpi_salidas, self.kpi_participantes, self.kpi_horas_hombre)):
            tarjeta.setCursor(Qt.CursorShape.ArrowCursor)
            fila_kpi.addWidget(tarjeta, i // 3, i % 3)
        layout.addLayout(fila_kpi)

        layout.addWidget(self._crear_bloque_personal(contenido))

        # Gráficos.
        grilla = QGridLayout()
        grilla.setSpacing(14)
        self.grafico_meses = GraficoColumnas("Servicios por mes", contenido)
        self.grafico_tipos = GraficoDona("Servicios por tipo de salida", contenido)
        self.grafico_moviles = GraficoBarras("Horas de servicio por móvil", contenido)
        self.grafico_bomberos = GraficoBarras("Bomberos con más horas dedicadas", contenido)
        for i, grafico in enumerate((self.grafico_meses, self.grafico_tipos, self.grafico_moviles,
                                     self.grafico_bomberos)):
            marco = QFrame(contenido)
            marco.setObjectName("kpiCard")
            interno = QVBoxLayout(marco)
            interno.setContentsMargins(12, 10, 12, 10)
            interno.addWidget(grafico)
            grilla.addWidget(marco, i // 2, i % 2)
            grafico.marco = marco
        layout.addLayout(grilla)

        # Tabla por bombero (solo vista completa).
        self.titulo_tabla = QLabel("Detalle por bombero", contenido)
        self.titulo_tabla.setObjectName("subtituloBloque")
        layout.addWidget(self.titulo_tabla)
        self.tabla_bomberos = QTableWidget(contenido)
        self.tabla_bomberos.setColumnCount(len(COLUMNAS_BOMBEROS))
        self.tabla_bomberos.setHorizontalHeaderLabels(COLUMNAS_BOMBEROS)
        self.tabla_bomberos.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla_bomberos.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla_bomberos.verticalHeader().setVisible(False)
        self.tabla_bomberos.setSortingEnabled(True)
        self.tabla_bomberos.setMinimumHeight(280)
        cabecera = self.tabla_bomberos.horizontalHeader()
        cabecera.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        theme.estilizar_tabla(self.tabla_bomberos)
        layout.addWidget(self.tabla_bomberos)

        self.label_estado = QLabel("", contenido)
        self.label_estado.setProperty("muted", True)
        layout.addWidget(self.label_estado)
        layout.addStretch(1)

    def _crear_filtros(self) -> QVBoxLayout:
        """Dos filas: período (con sus fechas) y acciones; debajo, filtros por dimensión."""
        bloque = QVBoxLayout()
        bloque.setSpacing(8)
        fila = QHBoxLayout()
        fila.setSpacing(8)
        bloque.addLayout(fila)
        fila.addWidget(QLabel("Período:"))
        self.combo_periodo = QComboBox(self)
        self.combo_periodo.addItems(PERIODOS)
        self.combo_periodo.currentIndexChanged.connect(self._on_periodo_cambiado)
        fila.addWidget(self.combo_periodo)

        self.fecha_desde = QDateEdit(self)
        self.fecha_hasta = QDateEdit(self)
        for campo in (self.fecha_desde, self.fecha_hasta):
            campo.setCalendarPopup(True)
            campo.setDisplayFormat("dd/MM/yyyy")
            campo.dateChanged.connect(self._on_fecha_editada)
        fila.addWidget(self.fecha_desde)
        fila.addWidget(QLabel("a"))
        fila.addWidget(self.fecha_hasta)
        fila.addStretch(1)
        boton_actualizar = QPushButton("Actualizar", self)
        boton_actualizar.clicked.connect(self.refrescar)
        fila.addWidget(boton_actualizar)
        self.boton_reporte = QPushButton("📑 Reporte gerencial", self)
        self.boton_reporte.setObjectName("botonAhora")
        self.boton_reporte.setToolTip("Exporta a Excel asistencias y horas por bombero y salidas y horas por "
                                      "móvil entre dos fechas")
        self.boton_reporte.clicked.connect(self._abrir_reporte_gerencial)
        fila.addWidget(self.boton_reporte)

        fila = QHBoxLayout()
        fila.setSpacing(8)
        bloque.addLayout(fila)
        fila.addWidget(QLabel("Tipo de salida:"))
        self.combo_tipo = QComboBox(self)
        self.combo_tipo.currentIndexChanged.connect(self.refrescar)
        fila.addWidget(self.combo_tipo)

        self.label_movil = QLabel("Vehículo:")
        fila.addWidget(self.label_movil)
        self.combo_movil = QComboBox(self)
        self.combo_movil.currentIndexChanged.connect(self.refrescar)
        fila.addWidget(self.combo_movil)

        self.label_bombero = QLabel("Bombero:")
        fila.addWidget(self.label_bombero)
        self.combo_bombero = QComboBox(self)
        self.combo_bombero.setMinimumWidth(180)
        for combo in (self.combo_tipo, self.combo_movil, self.combo_bombero):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(16)
        self.combo_bombero.currentIndexChanged.connect(self.refrescar)
        fila.addWidget(self.combo_bombero)
        fila.addStretch(1)
        return bloque

    def _crear_bloque_personal(self, parent: QWidget) -> QFrame:
        marco = QFrame(parent)
        marco.setObjectName("tarjetaUnidad")
        layout = QVBoxLayout(marco)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        cabecera = QHBoxLayout()
        self.titulo_personal = QLabel("👤 Mi estadística", marco)
        self.titulo_personal.setObjectName("tituloUnidad")
        cabecera.addWidget(self.titulo_personal)
        cabecera.addStretch(1)
        self.boton_identificarse = QPushButton("👤 Identificarme con mi PIN", marco)
        self.boton_identificarse.setObjectName("botonAhora")
        self.boton_identificarse.clicked.connect(self.identificarse_solicitado.emit)
        cabecera.addWidget(self.boton_identificarse)
        layout.addLayout(cabecera)

        self.label_personal_aviso = QLabel("", marco)
        self.label_personal_aviso.setProperty("muted", True)
        self.label_personal_aviso.setWordWrap(True)
        layout.addWidget(self.label_personal_aviso)

        self._contenedor_datos_personales = QWidget(marco)
        fila = QHBoxLayout(self._contenedor_datos_personales)
        fila.setContentsMargins(0, 0, 0, 0)
        fila.setSpacing(12)
        self.dato_asistencia = TarjetaDato("Asistencia a siniestros", marco)
        self.dato_apresto = TarjetaDato("Horas en apresto", marco)
        self.dato_interviniente = TarjetaDato("Horas como interviniente", marco)
        self.dato_total = TarjetaDato("Total de horas dedicadas", marco)
        for dato in (self.dato_asistencia, self.dato_apresto, self.dato_interviniente, self.dato_total):
            fila.addWidget(dato)
        layout.addWidget(self._contenedor_datos_personales)
        return marco

    def _cargar_combos(self) -> None:
        with get_session() as session:
            tipos = [(t.nombre, t.id) for t in session.query(TipoIncidente).order_by(TipoIncidente.nombre)]
            moviles = [(m.nombre_identificador, m.id)
                       for m in session.query(Movil).order_by(Movil.nombre_identificador)]
            personas = [(p.nombre_completo(), p.id)
                        for p in session.query(Personal).order_by(Personal.apellido, Personal.nombre)]
        for combo, items in ((self.combo_tipo, tipos), (self.combo_movil, moviles), (self.combo_bombero, personas)):
            elegido = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(TODOS, None)
            for texto, valor in items:
                combo.addItem(texto, valor)
            combo.setCurrentIndex(max(0, combo.findData(elegido)))
            combo.blockSignals(False)

    # -- Permisos ------------------------------------------------------------------

    def set_operador(self, personal_id: Optional[int], rol: Optional[str]) -> None:
        """MainWindow: operador identificado (o None) y su rol."""
        self._operador_id = personal_id
        self._completa = permisos_de(rol).ver_estadisticas_completas if personal_id is not None else False
        self._aplicar_nivel_acceso()
        if self.isVisible():
            self.refrescar()

    @property
    def vista_completa(self) -> bool:
        return self._completa

    def _aplicar_nivel_acceso(self) -> None:
        completa = self._completa
        for widget in (self.label_movil, self.combo_movil, self.label_bombero, self.combo_bombero,
                       self.boton_reporte, self.titulo_tabla, self.tabla_bomberos, self.kpi_participantes,
                       self.kpi_horas_hombre, self.grafico_bomberos.marco):
            widget.setVisible(completa)
        if not completa:
            for combo in (self.combo_movil, self.combo_bombero):
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
        if completa:
            self.label_acceso.setText("🔓 Vista completa: toda la base estadística del cuartel "
                                      "(Jefatura / Administración / Estadística).")
            theme.set_tono(self.label_acceso, "ok")
        else:
            self.label_acceso.setText("🔒 Vista de bombero: KPIs generales del cuartel y tu estadística personal. "
                                      "Jefatura, Administración y Estadística ven el detalle completo.")
            theme.set_tono(self.label_acceso, "info")
        self.boton_identificarse.setVisible(self._operador_id is None)

    # -- Período -------------------------------------------------------------------

    def _on_periodo_cambiado(self, *_args) -> None:
        rango = rango_de_periodo(self.combo_periodo.currentText())
        if rango is not None:
            for campo, valor in ((self.fecha_desde, rango[0]), (self.fecha_hasta, rango[1])):
                campo.blockSignals(True)
                campo.setDate(_a_qdate(valor))
                campo.blockSignals(False)
        self.refrescar()

    def _on_fecha_editada(self, *_args) -> None:
        indice = self.combo_periodo.findText(PERIODO_PERSONALIZADO)
        if self.combo_periodo.currentIndex() != indice:
            self.combo_periodo.blockSignals(True)
            self.combo_periodo.setCurrentIndex(indice)
            self.combo_periodo.blockSignals(False)
        self.refrescar()

    def rango(self) -> Tuple[date, date]:
        return _a_date(self.fecha_desde.date()), _a_date(self.fecha_hasta.date())

    # -- Datos ---------------------------------------------------------------------

    def refrescar(self, *_args) -> None:
        desde, hasta = self.rango()
        if hasta < desde:
            self.label_estado.setText("La fecha 'hasta' es anterior a la fecha 'desde'.")
            return
        tipo_id = self.combo_tipo.currentData()
        movil_id = self.combo_movil.currentData() if self._completa else None
        bombero_id = self.combo_bombero.currentData() if self._completa else None

        # KPIs generales: período + tipo (+ vehículo / bombero en la vista completa).
        self.reporte = calcular_estadisticas(desde, hasta, tipo_id=tipo_id, movil_id=movil_id,
                                             personal_id=bombero_id)
        # Ficha personal: la propia, o la del bombero filtrado en la vista completa.
        persona_id = bombero_id if bombero_id is not None else self._operador_id
        if persona_id is not None:
            base = (self.reporte if movil_id is None and bombero_id is None
                    else calcular_estadisticas(desde, hasta, tipo_id=tipo_id))
            self.personal = estadistica_personal(persona_id, desde, hasta, reporte=base)
        else:
            self.personal = None

        self._mostrar_kpis(self.reporte)
        self._mostrar_personal(bombero_id)
        self._mostrar_graficos(self.reporte)
        self._mostrar_tabla(self.reporte)
        en_curso = f" · {self.reporte.servicios_en_curso} en curso (no suman)" if self.reporte.servicios_en_curso else ""
        self.label_estado.setText(f"Período {desde:%d/%m/%Y} – {hasta:%d/%m/%Y}: "
                                  f"{self.reporte.servicios} servicio(s) cerrados{en_curso}.")

    def _mostrar_kpis(self, r: ReporteEstadisticas) -> None:
        self.kpi_servicios.set_valor(str(r.servicios))
        self.kpi_horas.set_valor(formatear_horas(r.horas_servicio))
        self.kpi_promedio.set_valor(formatear_horas(r.promedio_horas_servicio))
        self.kpi_salidas.set_valor(str(r.salidas_moviles))
        self.kpi_participantes.set_valor(str(r.bomberos_participantes))
        self.kpi_horas_hombre.set_valor(formatear_horas(r.horas_hombre))

    def _mostrar_personal(self, bombero_filtrado: Optional[int]) -> None:
        p = self.personal
        if p is None:
            self.titulo_personal.setText("👤 Mi estadística")
            self.label_personal_aviso.setText("Identificate con tu PIN (chip 👤 de la barra superior) para ver tu "
                                              "asistencia a siniestros y tus horas de apresto e interviniente.")
            self.label_personal_aviso.setVisible(True)
            self._contenedor_datos_personales.setVisible(False)
            return
        propio = bombero_filtrado is None or bombero_filtrado == self._operador_id
        self.titulo_personal.setText(f"👤 {'Mi estadística' if propio else 'Estadística de'} · {p.nombre}")
        self.label_personal_aviso.setVisible(False)
        self._contenedor_datos_personales.setVisible(True)
        self.dato_asistencia.set_valor(
            str(p.asistencias),
            f"{p.porcentaje_asistencia:.0f}% de los {p.servicios_cuartel_total} servicios del cuartel · "
            f"{p.servicios_interviniente} interviniente / {p.servicios_apresto} apresto")
        self.dato_apresto.set_valor(formatear_horas(p.horas_apresto), "En base: operador de radio o reserva")
        self.dato_interviniente.set_valor(formatear_horas(p.horas_interviniente), "Embarcado: chofer, Jefe o dotación")
        self.dato_total.set_valor(formatear_horas(p.horas_totales), "Apresto + interviniente")

    def _mostrar_graficos(self, r: ReporteEstadisticas) -> None:
        self.grafico_meses.set_datos([(mes, float(n), str(n)) for mes, n in r.por_mes])
        self.grafico_tipos.set_datos([(tipo, float(n), str(n)) for tipo, n in r.por_tipo])
        self.grafico_moviles.set_datos([(m.nombre, m.horas, formatear_horas(m.horas))
                                        for m in sorted(r.moviles, key=lambda m: -m.horas) if m.horas > 0])
        self.grafico_bomberos.set_datos([(b.nombre, b.horas_totales, formatear_horas(b.horas_totales))
                                         for b in r.bomberos if b.horas_totales > 0] if self._completa else [])

    def _mostrar_tabla(self, r: ReporteEstadisticas) -> None:
        tabla = self.tabla_bomberos
        tabla.setSortingEnabled(False)
        tabla.setRowCount(0)
        if not self._completa:
            return
        filas: List[tuple] = [
            (b.nombre, b.legajo, b.total_servicios, b.servicios_escena, b.servicios_cuartel,
             b.horas_escena, b.horas_cuartel, b.horas_totales)
            for b in r.bomberos if b.total_servicios
        ]
        tabla.setRowCount(len(filas))
        for i, fila in enumerate(filas):
            for j, valor in enumerate(fila):
                if isinstance(valor, float):
                    item = _ItemHoras(valor)
                elif isinstance(valor, int):
                    item = QTableWidgetItem()
                    item.setData(Qt.ItemDataRole.DisplayRole, valor)
                else:
                    item = QTableWidgetItem(str(valor or "—"))
                if j >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                tabla.setItem(i, j, item)
        tabla.setSortingEnabled(True)

    def _abrir_reporte_gerencial(self) -> None:
        if not self._completa:
            return
        from app.ui.reports_window import abrir_reporte_gerencial

        abrir_reporte_gerencial(self)

    def recargar_catalogos(self) -> None:
        """Tipos, móviles y personal pueden cambiar (padrón, ABM)."""
        self._cargar_combos()


