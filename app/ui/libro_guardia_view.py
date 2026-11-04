"""
Página "Libro de Guardia" del escritorio: bitácora de viajes y asistencias
sincronizada con Supabase (vista libro_guardia_bitacora).

  * Tarjetas KPI: Viajes Activos, Personal en Cuartel, Guardias del Día.
  * Tabla Bitácora y Viajes: bombero, actividad / destino, vehículo,
    ingreso / salida, egreso / regreso, duración, estado (chip) y registro.
  * Acciones: "+ Registrar Salida / Viaje" (dispara los avisos automáticos de
    WhatsApp desde la base), "Marcar Regreso" (fecha y hora completas: admite
    viajes de varios días) y "Exportar Planilla Mensual" (Excel para ART).

Toda llamada a la nube corre en un TareaLibroGuardia (QThread): la UI nunca se
congela aunque no haya internet. Con la página visible se refresca sola cada
minuto. Mismos componentes del resto de la app (TarjetaKPI, tarjetas
#seccionFormulario, chips con tono, tablas de theme.estilizar_tabla).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, List, Optional, Set

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.libro_guardia_service import (
    ESTADO_EN_CURSO,
    ESTADO_FINALIZADO,
    ESTADO_SIN_EGRESO,
    TZ_CUARTEL,
    LibroGuardiaCliente,
    RegistroBitacora,
    ResumenGuardia,
)
from app.ui import theme
from app.ui.widgets import DateField, TarjetaKPI, TimeField
from app.workers.libro_guardia_worker import TareaLibroGuardia

log = logging.getLogger(__name__)

INTERVALO_REFRESCO_MS = 60 * 1000
DIAS_BITACORA = 30
ANCHO_MIN_ESTADO = 110
COLUMNAS = ["Bombero", "Actividad / Destino", "Vehículo", "Ingreso / Salida", "Egreso / Regreso",
            "Duración", "Estado", "Registro"]
COL_BOMBERO, COL_ACTIVIDAD, COL_VEHICULO, COL_INICIO, COL_FIN, COL_DURACION, COL_ESTADO, COL_REGISTRO = range(8)

ETIQUETA_ESTADO = {ESTADO_EN_CURSO: "EN_CURSO", ESTADO_FINALIZADO: "FINALIZADO", ESTADO_SIN_EGRESO: "SIN EGRESO"}
TONO_ESTADO = {ESTADO_EN_CURSO: "info", ESTADO_FINALIZADO: "ok", ESTADO_SIN_EGRESO: "alerta"}

FILTROS = [
    ("Todos los registros", lambda r: True),
    ("En curso", lambda r: r.estado == ESTADO_EN_CURSO),
    ("Viajes", lambda r: r.es_viaje),
    ("Viajes en curso", lambda r: r.es_viaje and r.abierto),
    ("Asistencias al cuartel", lambda r: not r.es_viaje),
]
FILTRO_VIAJES_EN_CURSO, FILTRO_EN_CURSO = 3, 1

MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre"]
VEHICULO_PARTICULAR = "Vehículo particular"


def formatear_fecha_hora(valor: Optional[datetime]) -> str:
    return f"{valor:%d/%m/%Y %H:%M}" if valor else "—"


def formatear_duracion(minutos: Optional[int]) -> str:
    if minutos is None:
        return "—"
    horas, resto = divmod(max(0, int(minutos)), 60)
    if horas >= 24:
        dias, horas = divmod(horas, 24)
        return f"{dias} d {horas} h"
    return f"{horas} h {resto:02d} min" if horas else f"{resto} min"


def ahora_cuartel() -> datetime:
    return datetime.now(TZ_CUARTEL)


def _crear_chip(texto: str, tono: str, parent: Optional[QWidget] = None) -> QLabel:
    chip = QLabel(texto, parent)
    chip.setObjectName("chip")
    chip.setFixedHeight(24)
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    theme.set_tono(chip, tono)
    chip.ensurePolished()  # el padding del QSS entra en el sizeHint recién al pulir
    chip.setMinimumWidth(chip.sizeHint().width())  # que nunca recorte su texto
    return chip


# =============================================================================
# Diálogos
# =============================================================================

class DialogoRegistrarSalida(QDialog):
    """Bombero (padrón local activo), vehículo (unidades activas o particular)
    y destino."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Registrar salida / viaje")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        titulo = QLabel("Nueva salida del libro de guardia", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)
        ayuda = QLabel("Al confirmar se avisa automáticamente al grupo operativo por WhatsApp.", self)
        ayuda.setObjectName("ayudaSeccion")
        ayuda.setWordWrap(True)
        layout.addWidget(ayuda)

        form = QFormLayout()
        form.setSpacing(10)
        self.combo_bombero = QComboBox(self)
        self.combo_bombero.setEditable(True)
        self.combo_bombero.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_bombero.lineEdit().setPlaceholderText("Escribí apellido, nombre o legajo…")
        completer = self.combo_bombero.completer()
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.combo_vehiculo = QComboBox(self)
        self.combo_vehiculo.setEditable(True)
        self.combo_vehiculo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_vehiculo.lineEdit().setPlaceholderText("Unidad del cuartel o vehículo particular")

        self.campo_destino = QLineEdit(self)
        self.campo_destino.setPlaceholderText("Ej. Río Cuarto — Hospital Regional")
        self.campo_destino.setMaxLength(120)

        form.addRow(theme.etiqueta_requerida("Bombero"), self.combo_bombero)
        form.addRow(theme.etiqueta_requerida("Vehículo"), self.combo_vehiculo)
        form.addRow(theme.etiqueta_requerida("Destino"), self.campo_destino)
        layout.addLayout(form)

        self.botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.botones.button(QDialogButtonBox.StandardButton.Ok).setText("Registrar salida")
        self.botones.accepted.connect(self.accept)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)

        self._cargar_opciones()
        for senal in (self.combo_bombero.currentTextChanged, self.combo_vehiculo.currentTextChanged,
                      self.campo_destino.textChanged):
            senal.connect(self._validar)
        self._validar()

    def _cargar_opciones(self) -> None:
        from app.db import get_session
        from app.models import Movil, Personal

        with get_session() as session:
            personas = [
                (p.id, f"{p.nombre_completo()}" + (f" · Leg. {p.legajo}" if p.legajo else ""))
                for p in session.query(Personal).filter(Personal.activo.is_(True))
                .order_by(Personal.apellido, Personal.nombre)
            ]
            moviles = [m.nombre_identificador for m in
                       session.query(Movil).filter(Movil.activo.is_(True)).order_by(Movil.nombre_identificador)]
        for persona_id, texto in personas:
            self.combo_bombero.addItem(texto, persona_id)
        self.combo_bombero.setCurrentIndex(-1)
        self.combo_vehiculo.addItems([*moviles, VEHICULO_PARTICULAR])
        self.combo_vehiculo.setCurrentIndex(-1)

    def bombero_id(self) -> Optional[int]:
        indice = self.combo_bombero.findText(self.combo_bombero.currentText().strip(),
                                             Qt.MatchFlag.MatchFixedString)
        return self.combo_bombero.itemData(indice) if indice >= 0 else None

    def vehiculo(self) -> str:
        return self.combo_vehiculo.currentText().strip()

    def destino(self) -> str:
        return self.campo_destino.text().strip()

    def _validar(self) -> None:
        bombero_ok = self.bombero_id() is not None
        theme.marcar_invalido(self.combo_bombero, bool(self.combo_bombero.currentText().strip()) and not bombero_ok)
        self.botones.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bombero_ok and bool(self.vehiculo()) and bool(self.destino()))


class DialogoMarcarRegreso(QDialog):
    """Fecha y hora del regreso (por defecto: ahora). Debe quedar entre la
    salida y el momento actual; la base lo vuelve a validar."""

    def __init__(self, registro: RegistroBitacora, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._registro = registro
        self.setWindowTitle("Marcar regreso")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        titulo = QLabel(f"Regreso de {registro.bombero}", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)
        detalle = QLabel(
            f"{registro.actividad} · {registro.vehiculo or 'sin vehículo'}\n"
            f"Salida: {formatear_fecha_hora(registro.inicio)}", self)
        detalle.setObjectName("ayudaSeccion")
        layout.addWidget(detalle)

        form = QFormLayout()
        form.setSpacing(10)
        self.campo_fecha = DateField(self)
        self.campo_fecha.set_hoy()
        self.campo_hora = TimeField(self)
        self.campo_hora.set_ahora()
        form.addRow(theme.etiqueta_requerida("Fecha de regreso"), self.campo_fecha)
        form.addRow(theme.etiqueta_requerida("Hora de regreso"), self.campo_hora)
        layout.addLayout(form)

        self.label_error = QLabel("", self)
        self.label_error.setObjectName("chip")
        theme.set_tono(self.label_error, "error")
        self.label_error.setWordWrap(True)
        self.label_error.setVisible(False)
        layout.addWidget(self.label_error)

        self.botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.botones.button(QDialogButtonBox.StandardButton.Ok).setText("Confirmar regreso")
        self.botones.accepted.connect(self._aceptar)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)

    def fecha_llegada(self) -> Optional[datetime]:
        hora = self.campo_hora.value()
        if hora is None:
            return None
        return datetime.combine(self.campo_fecha.value(), hora).replace(tzinfo=TZ_CUARTEL)

    def _aceptar(self) -> None:
        llegada = self.fecha_llegada()
        error = None
        if llegada is None:
            error = "Indicá la hora de regreso."
        elif llegada < self._registro.inicio.replace(second=0, microsecond=0):
            error = f"El regreso no puede ser anterior a la salida ({formatear_fecha_hora(self._registro.inicio)})."
        elif llegada > ahora_cuartel():
            error = "El regreso no puede ser posterior al momento actual."
        if error:
            self.label_error.setText(error)
            self.label_error.setVisible(True)
            return
        self.accept()


class DialogoExportarMes(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exportar planilla mensual")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        titulo = QLabel("Planilla de presencia para Seguro / ART", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)

        fila = QHBoxLayout()
        self.combo_mes = QComboBox(self)
        self.combo_mes.addItems(MESES)
        self.spin_anio = QSpinBox(self)
        self.spin_anio.setRange(2020, 2100)
        hoy = date.today()
        self.combo_mes.setCurrentIndex(hoy.month - 1)
        self.spin_anio.setValue(hoy.year)
        fila.addWidget(QLabel("Mes:", self))
        fila.addWidget(self.combo_mes, 1)
        fila.addWidget(QLabel("Año:", self))
        fila.addWidget(self.spin_anio)
        layout.addLayout(fila)

        botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        botones.button(QDialogButtonBox.StandardButton.Ok).setText("Exportar Excel")
        botones.accepted.connect(self.accept)
        botones.rejected.connect(self.reject)
        layout.addWidget(botones)

    def periodo(self) -> tuple[int, int]:
        return self.spin_anio.value(), self.combo_mes.currentIndex() + 1


# =============================================================================
# Página
# =============================================================================

class PaginaLibroGuardia(QWidget):
    def __init__(self, parent: Optional[QWidget] = None,
                 cliente_factory: Callable[[], LibroGuardiaCliente] = LibroGuardiaCliente) -> None:
        super().__init__(parent)
        self._cliente_factory = cliente_factory
        self._registros: List[RegistroBitacora] = []
        self._tareas: Set[TareaLibroGuardia] = set()
        self._cargando = False

        self._timer = QTimer(self)
        self._timer.setInterval(INTERVALO_REFRESCO_MS)
        self._timer.timeout.connect(self.refrescar)

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._esperar_tareas)

        self._construir()

    # -- Construcción ------------------------------------------------------------

    def _construir(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        cabecera = QHBoxLayout()
        textos = QVBoxLayout()
        textos.setSpacing(2)
        titulo = QLabel("Libro de Guardia y Bitácora", self)
        titulo.setObjectName("pageTitle")
        subtitulo = QLabel("Viajes, comisiones y asistencias al cuartel registradas por el bot y por la guardia.",
                           self)
        subtitulo.setObjectName("pageSubtitle")
        textos.addWidget(titulo)
        textos.addWidget(subtitulo)
        cabecera.addLayout(textos, 1)
        self.chip_sync = _crear_chip("☁️ Sin sincronizar", "neutro", self)
        cabecera.addWidget(self.chip_sync, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(cabecera)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.tarjeta_viajes = TarjetaKPI("Viajes Activos", "🚐", self)
        self.tarjeta_cuartel = TarjetaKPI("Personal en Cuartel", "🧑‍🚒", self)
        self.tarjeta_guardias = TarjetaKPI("Guardias del Día", "🛡️", self)
        self.tarjeta_viajes.clicked.connect(lambda: self.combo_filtro.setCurrentIndex(FILTRO_VIAJES_EN_CURSO))
        self.tarjeta_cuartel.clicked.connect(lambda: self.combo_filtro.setCurrentIndex(FILTRO_EN_CURSO))
        for i, tarjeta in enumerate((self.tarjeta_viajes, self.tarjeta_cuartel, self.tarjeta_guardias)):
            grid.addWidget(tarjeta, 0, i)
        layout.addLayout(grid)

        layout.addWidget(self._crear_tarjeta_bitacora(), 1)

    def _crear_tarjeta_bitacora(self) -> QFrame:
        marco = QFrame(self)
        marco.setObjectName("seccionFormulario")
        layout_marco = QVBoxLayout(marco)
        layout_marco.setContentsMargins(0, 0, 0, 0)
        layout_marco.setSpacing(0)

        cabecera = QFrame(marco)
        cabecera.setObjectName("cabeceraSeccion")
        fila = QHBoxLayout(cabecera)
        fila.setContentsMargins(16, 10, 16, 10)
        fila.setSpacing(10)
        etiqueta = QLabel("BITÁCORA Y VIAJES", cabecera)
        etiqueta.setObjectName("tituloSeccion")
        fila.addWidget(etiqueta)
        self.label_cantidad = QLabel("", cabecera)
        self.label_cantidad.setObjectName("ayudaSeccion")
        fila.addWidget(self.label_cantidad)
        fila.addStretch(1)
        self.campo_buscar = QLineEdit(cabecera)
        self.campo_buscar.setPlaceholderText("🔎 Buscar bombero, destino o vehículo…")
        self.campo_buscar.setClearButtonEnabled(True)
        self.campo_buscar.setMinimumWidth(260)
        self.campo_buscar.textChanged.connect(self._pintar_tabla)
        fila.addWidget(self.campo_buscar)
        self.combo_filtro = QComboBox(cabecera)
        for texto, _ in FILTROS:
            self.combo_filtro.addItem(texto)
        self.combo_filtro.currentIndexChanged.connect(self._pintar_tabla)
        fila.addWidget(self.combo_filtro)
        layout_marco.addWidget(cabecera)

        contenido = QWidget(marco)
        layout_contenido = QVBoxLayout(contenido)
        layout_contenido.setContentsMargins(16, 14, 16, 16)
        layout_contenido.setSpacing(12)

        acciones = QHBoxLayout()
        acciones.setSpacing(10)
        self.boton_registrar = QPushButton("+ Registrar Salida / Viaje", contenido)
        self.boton_registrar.setObjectName("botonPrimarioRojo")
        self.boton_registrar.clicked.connect(self._registrar_salida)
        self.boton_regreso = QPushButton("🏁 Marcar Regreso", contenido)
        self.boton_regreso.setObjectName("botonPrimarioAzul")
        self.boton_regreso.setEnabled(False)
        self.boton_regreso.setToolTip("Seleccioná un viaje EN_CURSO de la tabla.")
        self.boton_regreso.clicked.connect(self._marcar_regreso)
        self.boton_exportar = QPushButton("📊 Exportar Planilla Mensual", contenido)
        self.boton_exportar.setObjectName("botonLimpiar")
        self.boton_exportar.setToolTip("Excel con la presencia del personal del mes (Seguro / ART).")
        self.boton_exportar.clicked.connect(self._exportar_planilla)
        self.boton_refrescar = QPushButton("⟳ Actualizar", contenido)
        self.boton_refrescar.setObjectName("botonHerramienta")
        self.boton_refrescar.clicked.connect(self.refrescar)
        for boton in (self.boton_registrar, self.boton_regreso, self.boton_exportar):
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            acciones.addWidget(boton)
        acciones.addStretch(1)
        acciones.addWidget(self.boton_refrescar)
        layout_contenido.addLayout(acciones)

        self.tabla = QTableWidget(contenido)
        self.tabla.setColumnCount(len(COLUMNAS))
        self.tabla.setHorizontalHeaderLabels(COLUMNAS)
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla.verticalHeader().setVisible(False)
        cab = self.tabla.horizontalHeader()
        cab.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(COL_ACTIVIDAD, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(COL_ESTADO, QHeaderView.ResizeMode.Fixed)  # chips: ancho en _pintar_tabla
        theme.estilizar_tabla(self.tabla)
        self.tabla.itemSelectionChanged.connect(self._actualizar_botones)
        self.tabla.cellDoubleClicked.connect(lambda *_: self._marcar_regreso())
        layout_contenido.addWidget(self.tabla, 1)

        self.label_vacio = QLabel("", contenido)
        self.label_vacio.setObjectName("ayudaSeccion")
        self.label_vacio.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_vacio.setVisible(False)
        layout_contenido.addWidget(self.label_vacio)

        layout_marco.addWidget(contenido, 1)
        return marco

    # -- Ciclo de vida ------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().hideEvent(event)
        self._timer.stop()

    def _esperar_tareas(self) -> None:
        for tarea in list(self._tareas):
            tarea.wait(5000)

    def _ejecutar(self, funcion: Callable[[], Any], al_terminar: Callable[[Any], None],
                  al_fallar: Callable[[str], None]) -> None:
        tarea = TareaLibroGuardia(funcion, self)
        self._tareas.add(tarea)
        tarea.terminado.connect(al_terminar)
        tarea.error.connect(al_fallar)
        tarea.finished.connect(lambda t=tarea: self._liberar(t))
        tarea.start()

    def _liberar(self, tarea: TareaLibroGuardia) -> None:
        self._tareas.discard(tarea)
        tarea.deleteLater()

    # -- Carga ----------------------------------------------------------------------

    def refrescar(self) -> None:
        if self._cargando:
            return
        self._cargando = True
        self.boton_refrescar.setEnabled(False)
        self._set_chip("☁️ Sincronizando…", "neutro")
        desde = ahora_cuartel() - timedelta(days=DIAS_BITACORA)

        def cargar():
            cliente = self._cliente_factory()
            return cliente.resumen(), cliente.bitacora(desde)

        self._ejecutar(cargar, self._al_cargar, self._al_fallar_carga)

    def _al_cargar(self, resultado: tuple) -> None:
        resumen, registros = resultado
        self._cargando = False
        self.boton_refrescar.setEnabled(True)
        self._mostrar_resumen(resumen)
        self._registros = registros
        self._pintar_tabla()
        self._set_chip(f"☁️ Sincronizado {ahora_cuartel():%H:%M}", "ok",
                       f"Últimos {DIAS_BITACORA} días + todo lo que sigue en curso. Se actualiza cada minuto.")

    def _al_fallar_carga(self, mensaje: str) -> None:
        self._cargando = False
        self.boton_refrescar.setEnabled(True)
        self._set_chip("☁️ Sin conexión", "error", mensaje)
        if not self._registros:
            for tarjeta in (self.tarjeta_viajes, self.tarjeta_cuartel, self.tarjeta_guardias):
                tarjeta.set_valor("—")
            self.label_vacio.setText(f"No se pudo leer el libro de guardia de la nube.\n{mensaje}")
            self.label_vacio.setVisible(True)

    def _mostrar_resumen(self, resumen: ResumenGuardia) -> None:
        self.tarjeta_viajes.set_valor(str(resumen.viajes_activos))
        self.tarjeta_cuartel.set_valor(str(resumen.personal_en_cuartel))
        self.tarjeta_guardias.set_valor(str(resumen.guardias_del_dia))

    def _set_chip(self, texto: str, tono: str, tooltip: str = "") -> None:
        self.chip_sync.setText(texto)
        self.chip_sync.setToolTip(tooltip)
        theme.set_tono(self.chip_sync, tono)
        self.chip_sync.setMinimumWidth(self.chip_sync.sizeHint().width())

    # -- Tabla ------------------------------------------------------------------------

    def registros_visibles(self) -> List[RegistroBitacora]:
        _, criterio = FILTROS[max(0, self.combo_filtro.currentIndex())]
        palabras = self.campo_buscar.text().lower().split()

        def coincide(r: RegistroBitacora) -> bool:
            texto = " ".join(filter(None, (r.bombero, r.legajo, r.actividad, r.vehiculo))).lower()
            return all(p in texto for p in palabras)

        return [r for r in self._registros if criterio(r) and coincide(r)]

    def _pintar_tabla(self) -> None:
        seleccionado = self.registro_seleccionado()
        visibles = self.registros_visibles()
        self.tabla.setRowCount(len(visibles))
        for fila, r in enumerate(visibles):
            item_bombero = QTableWidgetItem(r.bombero)
            item_bombero.setData(Qt.ItemDataRole.UserRole, r.id)
            item_bombero.setToolTip(" · ".join(filter(None, (r.jerarquia, f"Leg. {r.legajo}" if r.legajo else None))))
            self.tabla.setItem(fila, COL_BOMBERO, item_bombero)
            item_actividad = QTableWidgetItem(("🚐 " if r.es_viaje else "🏠 ") + r.actividad)
            item_actividad.setToolTip(r.actividad)
            self.tabla.setItem(fila, COL_ACTIVIDAD, item_actividad)
            for columna, texto in ((COL_VEHICULO, r.vehiculo or "—"),
                                   (COL_INICIO, formatear_fecha_hora(r.inicio)),
                                   (COL_FIN, formatear_fecha_hora(r.fin)),
                                   (COL_DURACION, formatear_duracion(r.minutos) if r.fin else "—"),
                                   (COL_REGISTRO, r.metodo_legible)):
                item = QTableWidgetItem(texto)
                if columna != COL_VEHICULO:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.tabla.setItem(fila, columna, item)
            self.tabla.setCellWidget(fila, COL_ESTADO, self._celda_estado(r.estado))
        self.tabla.resizeColumnsToContents()
        self.tabla.horizontalHeader().setSectionResizeMode(COL_ACTIVIDAD, QHeaderView.ResizeMode.Stretch)
        # Los chips son cellWidgets: resizeColumnsToContents no los mide.
        ancho_chips = max((self.tabla.cellWidget(f, COL_ESTADO).sizeHint().width()
                           for f in range(self.tabla.rowCount())), default=0)
        self.tabla.setColumnWidth(COL_ESTADO, max(ANCHO_MIN_ESTADO, ancho_chips + 8))

        total = len(self._registros)
        self.label_cantidad.setText(f"{len(visibles)} de {total} registro(s)" if total else "")
        self.label_vacio.setText("No hay registros para este filtro." if total else
                                 f"Sin viajes ni asistencias en los últimos {DIAS_BITACORA} días.")
        self.label_vacio.setVisible(not visibles)
        if seleccionado is not None:
            self._seleccionar(seleccionado.id)
        self._actualizar_botones()

    @staticmethod
    def _celda_estado(estado: str) -> QWidget:
        contenedor = QWidget()
        fila = QHBoxLayout(contenedor)
        fila.setContentsMargins(6, 0, 6, 0)
        fila.addWidget(_crear_chip(ETIQUETA_ESTADO.get(estado, estado), TONO_ESTADO.get(estado, "neutro")),
                       0, Qt.AlignmentFlag.AlignCenter)
        return contenedor

    def _seleccionar(self, registro_id: str) -> None:
        for fila in range(self.tabla.rowCount()):
            item = self.tabla.item(fila, COL_BOMBERO)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == registro_id:
                self.tabla.selectRow(fila)
                return

    def registro_seleccionado(self) -> Optional[RegistroBitacora]:
        filas = self.tabla.selectionModel().selectedRows() if self.tabla.selectionModel() else []
        if not filas:
            return None
        item = self.tabla.item(filas[0].row(), COL_BOMBERO)
        registro_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return next((r for r in self._registros if r.id == registro_id), None)

    def _actualizar_botones(self) -> None:
        r = self.registro_seleccionado()
        self.boton_regreso.setEnabled(r is not None and r.es_viaje and r.abierto)

    # -- Acciones ---------------------------------------------------------------------

    def _registrar_salida(self) -> None:
        dialogo = DialogoRegistrarSalida(self)
        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        bombero_id, destino, vehiculo = dialogo.bombero_id(), dialogo.destino(), dialogo.vehiculo()
        self.boton_registrar.setEnabled(False)
        self._ejecutar(
            lambda: self._cliente_factory().registrar_salida(bombero_id, destino, vehiculo),
            lambda _r: self._al_terminar_accion(self.boton_registrar, "Salida registrada",
                                                f"Viaje a {destino} registrado. El aviso al grupo operativo "
                                                "sale automáticamente por WhatsApp."),
            lambda msg: self._al_fallar_accion(self.boton_registrar, "No se pudo registrar la salida", msg),
        )

    def _marcar_regreso(self) -> None:
        registro = self.registro_seleccionado()
        if registro is None or not (registro.es_viaje and registro.abierto):
            return
        dialogo = DialogoMarcarRegreso(registro, self)
        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        llegada = dialogo.fecha_llegada()
        self.boton_regreso.setEnabled(False)
        self._ejecutar(
            lambda: self._cliente_factory().marcar_regreso(registro.id, llegada),
            lambda r: self._al_terminar_accion(
                self.boton_regreso, "Regreso registrado",
                f"{registro.bombero}: regreso {llegada:%d/%m/%Y %H:%M} "
                f"({formatear_duracion(r.get('minutos'))}). Viaje FINALIZADO."),
            lambda msg: self._al_fallar_accion(self.boton_regreso, "No se pudo marcar el regreso", msg),
        )

    def _al_terminar_accion(self, boton: QPushButton, titulo: str, texto: str) -> None:
        boton.setEnabled(True)
        QMessageBox.information(self, titulo, texto)
        self.refrescar()

    def _al_fallar_accion(self, boton: QPushButton, titulo: str, mensaje: str) -> None:
        boton.setEnabled(True)
        self._actualizar_botones()
        QMessageBox.warning(self, titulo, mensaje)

    def _exportar_planilla(self) -> None:
        dialogo = DialogoExportarMes(self)
        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        anio, mes = dialogo.periodo()
        self.boton_exportar.setEnabled(False)
        self.boton_exportar.setText("Exportando…")

        def generar():
            from app.reports.libro_guardia_generator import generar_planilla_mensual

            registros = self._cliente_factory().bitacora_mes(anio, mes)
            return generar_planilla_mensual(anio, mes, registros), len(registros)

        self._ejecutar(generar, self._al_exportar, self._al_fallar_exportar)

    def _restaurar_boton_exportar(self) -> None:
        self.boton_exportar.setEnabled(True)
        self.boton_exportar.setText("📊 Exportar Planilla Mensual")

    def _al_exportar(self, resultado: tuple) -> None:
        ruta, cantidad = resultado
        self._restaurar_boton_exportar()
        caja = QMessageBox(self)
        caja.setIcon(QMessageBox.Icon.Information)
        caja.setWindowTitle("Planilla exportada")
        caja.setText(f"Planilla de presencia generada con {cantidad} registro(s).")
        caja.setInformativeText(str(ruta))
        boton_abrir = caja.addButton("Abrir", QMessageBox.ButtonRole.AcceptRole)
        caja.addButton("Cerrar", QMessageBox.ButtonRole.RejectRole)
        caja.exec()
        if caja.clickedButton() is boton_abrir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(ruta)))

    def _al_fallar_exportar(self, mensaje: str) -> None:
        self._restaurar_boton_exportar()
        QMessageBox.warning(self, "No se pudo exportar la planilla", mensaje)
