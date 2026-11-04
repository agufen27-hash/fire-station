"""
Página "Mantenimiento" del escritorio: checklists preventivos por cadencia.

  * Pestañas Diarias / Semanales / Mensuales (botones conmutables).
  * Tarjetas KPI: al día, con observaciones, fuera de servicio y pendientes
    de la cadencia elegida.
  * Grilla de cumplimiento por ítem: estado de la cadencia (al día / por
    vencer / pendiente / vencida / con falla), última revisión, vencimiento
    y estado del ítem en el inventario (⚙️ = puesto fuera de servicio por el
    motor de mantenimiento).
  * "Completar revisión": formulario ágil (OK / Falla / N/A por tarea, "Marcar
    todo OK", observación obligatoria en cada falla) que anticipa, antes de
    firmar, si el ítem va a quedar fuera de servicio. Firma el operador
    identificado (chip 👤); sin operador no se puede registrar.
  * "Historial" (supervisión: quién revisó qué y cuándo, con el detalle) y
    "Plantillas" (las tareas; alta / edición / desactivación solo ADMIN).

Si falla la conexión al registrar, el formulario queda abierto con todo lo
cargado para reintentar. Toda llamada a la nube corre en un TareaSupabase.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
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
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.roles import ROL_ADMIN, normalizar_rol
from app.services.inventario_service import CATEGORIAS, ESTADOS as ESTADOS_INVENTARIO
from app.services.mantenimiento_service import (
    AYUDA_FRECUENCIA,
    DIARIA,
    ESTADOS_GENERALES,
    ESTADOS_TAREA,
    FALLA,
    FALLA_RESPUESTA,
    FRECUENCIAS,
    FUERA_DE_SERVICIO,
    NO_APLICA,
    OK,
    PENDIENTE,
    POR_VENCER,
    RESULTADOS,
    RESPUESTAS,
    TONO_ESTADO_GENERAL,
    TONO_ESTADO_TAREA,
    TONO_RESULTADO,
    VENCIDA,
    EstadoItem,
    FichaActivo,
    MantenimientoCliente,
    RegistroHistorial,
    ResultadoChecklist,
    TareaItem,
    TareaPlantilla,
    prever_estado_general,
    resultado_checklist,
    validar_respuestas,
)
from app.services.supabase_client import SupabaseConfigError, cargar_credenciales
from app.ui import theme
from app.ui.libro_guardia_view import DialogoCredencialesSupabase, _crear_chip, ahora_cuartel, formatear_fecha_hora
from app.ui.widgets import TarjetaKPI
from app.workers.supabase_tarea import TareaSupabase

INTERVALO_REFRESCO_MS = 2 * 60 * 1000
COLUMNAS = ["Ítem", "Código QR", "Categoría", "Ubicación", "Tareas", "Cumplimiento", "Última revisión",
            "Vence", "Estado del ítem"]
(COL_ITEM, COL_CODIGO, COL_CATEGORIA, COL_UBICACION, COL_TAREAS, COL_CUMPLIMIENTO, COL_ULTIMA,
 COL_VENCE, COL_ESTADO) = range(9)
COLUMNAS_CHIP = (COL_CUMPLIMIENTO, COL_ESTADO)
ICONOS_FRECUENCIA = {"DIARIA": "☀️", "SEMANAL": "📅", "MENSUAL": "🗓️"}
TONO_ESTADO_INVENTARIO = {"OPERATIVO": "ok", "EN_REPARACION": "alerta", "FUERA_DE_SERVICIO": "error"}
ESTADOS_ATENCION = (PENDIENTE, VENCIDA, FALLA, POR_VENCER)


def es_admin(rol: Optional[str]) -> bool:
    return normalizar_rol(rol) == ROL_ADMIN


def _celda_chip(texto: str, tono: str, tooltip: str = "") -> QWidget:
    contenedor = QWidget()
    fila = QHBoxLayout(contenedor)
    fila.setContentsMargins(6, 0, 6, 0)
    chip = _crear_chip(texto, tono)
    chip.setToolTip(tooltip)
    fila.addWidget(chip, 0, Qt.AlignmentFlag.AlignCenter)
    contenedor.setToolTip(tooltip)
    return contenedor


def _ajustar_columnas_chip(tabla: QTableWidget, columnas, minimo: int = 120) -> None:
    """Los chips son cellWidgets: resizeColumnsToContents no los mide."""
    for col in columnas:
        ancho = max((tabla.cellWidget(f, col).sizeHint().width()
                     for f in range(tabla.rowCount()) if tabla.cellWidget(f, col) is not None), default=0)
        tabla.setColumnWidth(col, max(minimo, ancho + 8))


class _ConTareas:
    """Mezcla: corre funciones bloqueantes en TareaSupabase y las mantiene vivas."""

    def _init_tareas(self) -> None:
        self._tareas: Set[TareaSupabase] = set()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._esperar_tareas)

    def _esperar_tareas(self) -> None:
        for tarea in list(self._tareas):
            tarea.wait(5000)

    def _ejecutar(self, funcion: Callable[[], Any], al_terminar: Callable[[Any], None],
                  al_fallar: Callable[[str], None]) -> None:
        tarea = TareaSupabase(funcion, self)  # type: ignore[arg-type]
        self._tareas.add(tarea)
        tarea.terminado.connect(al_terminar)
        tarea.error.connect(al_fallar)
        tarea.finished.connect(lambda t=tarea: (self._tareas.discard(t), t.deleteLater()))
        tarea.start()


# =============================================================================
# Formulario de checklist
# =============================================================================

class FilaTarea(QFrame):
    """Una tarea del checklist: título (+ CRÍTICA), OK / Falla / N/A y observación."""

    def __init__(self, tarea: TareaItem, al_cambiar: Callable[[], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tarea = tarea
        self.setObjectName("seccionFormulario")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        fila = QHBoxLayout()
        titulo = QLabel(tarea.titulo, self)
        titulo.setObjectName("preguntaConmutable")
        titulo.setWordWrap(True)
        fila.addWidget(titulo, 1)
        if tarea.critica:
            chip = _crear_chip("CRÍTICA", "error", self)
            chip.setToolTip("Si falla o se vence, el ítem queda FUERA DE SERVICIO automáticamente.")
            fila.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)
        self.grupo = QButtonGroup(self)
        self.botones: Dict[str, QPushButton] = {}
        for codigo, texto in ((OK, "✓ OK"), (FALLA_RESPUESTA, "✗ Falla"), (NO_APLICA, "N/A")):
            boton = QPushButton(texto, self)
            boton.setObjectName("toggleVista")
            boton.setCheckable(True)
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            boton.setMinimumWidth(74)
            self.grupo.addButton(boton)
            self.botones[codigo] = boton
            fila.addWidget(boton, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(fila)

        detalles = [tarea.descripcion or ""]
        if tarea.ultima_fecha:
            detalles.append(f"Última: {RESPUESTAS.get(tarea.ultimo_estado or '', '—')} el "
                            f"{formatear_fecha_hora(tarea.ultima_fecha)}"
                            + (f" · {tarea.ultimo_responsable}" if tarea.ultimo_responsable else "")
                            + (f" · «{tarea.ultima_observacion}»" if tarea.ultima_observacion else ""))
        texto_detalle = "\n".join(d for d in detalles if d)
        if texto_detalle:
            detalle = QLabel(texto_detalle, self)
            detalle.setObjectName("ayudaSeccion")
            detalle.setWordWrap(True)
            layout.addWidget(detalle)

        self.campo_observacion = QLineEdit(self)
        self.campo_observacion.setPlaceholderText("Observación (obligatoria si hay falla)")
        self.campo_observacion.setMaxLength(300)
        layout.addWidget(self.campo_observacion)

        self.grupo.buttonToggled.connect(lambda *_: (self._actualizar(), al_cambiar()))
        self.campo_observacion.textChanged.connect(lambda *_: (self._actualizar(), al_cambiar()))
        self._actualizar()

    def respuesta(self) -> Optional[str]:
        return next((codigo for codigo, b in self.botones.items() if b.isChecked()), None)

    def set_respuesta(self, codigo: str) -> None:
        self.botones[codigo].setChecked(True)

    def observacion(self) -> str:
        return self.campo_observacion.text().strip()

    def _actualizar(self) -> None:
        falla = self.respuesta() == FALLA_RESPUESTA
        theme.marcar_invalido(self.campo_observacion, falla and not self.observacion())


class DialogoChecklist(QDialog, _ConTareas):
    def __init__(self, item: EstadoItem, frecuencia: str, tareas_item: List[TareaItem], responsable: str,
                 enviar: Callable[[Dict[str, str], Dict[str, str], str], ResultadoChecklist],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._init_tareas()
        self.item = item
        self.frecuencia = frecuencia
        self.tareas_item = tareas_item
        self.tareas = [t for t in tareas_item if t.frecuencia == frecuencia]
        self._enviar = enviar
        self.resultado: Optional[ResultadoChecklist] = None
        self._enviando = False

        self.setWindowTitle(f"Revisión {frecuencia.lower()} · {item.nombre}")
        self.setMinimumSize(640, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        titulo = QLabel(f"{ICONOS_FRECUENCIA.get(frecuencia, '')} Revisión {frecuencia.lower()}: "
                        f"{item.nombre}", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)
        sub = QLabel(f"{item.codigo_qr} · {CATEGORIAS.get(item.categoria, item.categoria)} · {item.ubicacion}"
                     f"   —   Responsable: {responsable}", self)
        sub.setObjectName("ayudaSeccion")
        layout.addWidget(sub)

        barra = QHBoxLayout()
        self.boton_todo_ok = QPushButton("✓ Marcar todo OK", self)
        self.boton_todo_ok.setObjectName("botonHerramienta")
        self.boton_todo_ok.clicked.connect(self._todo_ok)
        barra.addWidget(self.boton_todo_ok)
        barra.addStretch(1)
        self.chip_prevision = _crear_chip("", "neutro", self)
        barra.addWidget(self.chip_prevision)
        layout.addLayout(barra)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        contenedor = QWidget()
        lista = QVBoxLayout(contenedor)
        lista.setContentsMargins(0, 0, 4, 0)
        lista.setSpacing(8)
        self.filas = [FilaTarea(t, self._actualizar, contenedor) for t in self.tareas]
        for fila in self.filas:
            lista.addWidget(fila)
        lista.addStretch(1)
        scroll.setWidget(contenedor)
        layout.addWidget(scroll, 1)

        self.campo_observaciones = QPlainTextEdit(self)
        self.campo_observaciones.setPlaceholderText("Observaciones generales de la revisión (opcional)")
        self.campo_observaciones.setFixedHeight(56)
        layout.addWidget(self.campo_observaciones)

        self.label_error = QLabel("", self)
        self.label_error.setObjectName("chip")
        self.label_error.setWordWrap(True)
        self.label_error.setVisible(False)
        layout.addWidget(self.label_error)

        self.botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.boton_ok = self.botones.button(QDialogButtonBox.StandardButton.Ok)
        self.boton_ok.setText("Firmar y registrar")
        self.botones.accepted.connect(self._firmar)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)
        self._actualizar()

    # -- Estado del formulario ----------------------------------------------------

    def respuestas(self) -> Dict[str, str]:
        return {f.tarea.tarea_id: r for f in self.filas if (r := f.respuesta()) is not None}

    def observaciones_tareas(self) -> Dict[str, str]:
        return {f.tarea.tarea_id: f.observacion() for f in self.filas if f.observacion()}

    def _todo_ok(self) -> None:
        for fila in self.filas:
            if fila.respuesta() is None:
                fila.set_respuesta(OK)

    def _actualizar(self) -> None:
        respuestas = self.respuestas()
        faltan = len(self.tareas) - len(respuestas)
        if faltan:
            self.chip_prevision.setText(f"Faltan {faltan} de {len(self.tareas)}")
            theme.set_tono(self.chip_prevision, "neutro")
        else:
            resultado = resultado_checklist(self.tareas, respuestas)
            general = prever_estado_general(self.tareas_item, respuestas)
            texto = f"Resultado: {RESULTADOS[resultado].upper()}"
            if general == FUERA_DE_SERVICIO:
                texto += " · el ítem queda FUERA DE SERVICIO"
            self.chip_prevision.setText(texto)
            theme.set_tono(self.chip_prevision, TONO_RESULTADO[resultado] if general != FUERA_DE_SERVICIO else "error")
        self.chip_prevision.setMinimumWidth(self.chip_prevision.sizeHint().width())
        self.boton_ok.setEnabled(not self._enviando and faltan == 0)

    def _error(self, texto: str) -> None:
        self.label_error.setText(texto)
        theme.set_tono(self.label_error, "error")
        self.label_error.setVisible(True)

    # -- Envío ------------------------------------------------------------------------

    def _firmar(self) -> None:
        respuestas, obs = self.respuestas(), self.observaciones_tareas()
        problema = validar_respuestas(self.tareas, respuestas, obs)
        if problema:
            self._error(problema)
            return
        if prever_estado_general(self.tareas_item, respuestas) == FUERA_DE_SERVICIO \
                and self.item.estado == "OPERATIVO":
            seguir = QMessageBox.question(
                self, "El ítem va a quedar fuera de servicio",
                f"Con estas respuestas «{self.item.nombre}» pasa automáticamente a FUERA DE SERVICIO "
                "(falla o vencimiento de una tarea crítica). ¿Registrar igual?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if seguir != QMessageBox.StandardButton.Yes:
                return
        self._enviando = True
        self._actualizar()
        self.label_error.setVisible(False)
        self.boton_ok.setText("Registrando…")
        general = self.campo_observaciones.toPlainText()
        self._ejecutar(lambda: self._enviar(respuestas, obs, general), self._al_registrar, self._al_fallar)

    def _al_registrar(self, resultado: ResultadoChecklist) -> None:
        self._enviando = False
        self.resultado = resultado
        self.accept()

    def _al_fallar(self, mensaje: str) -> None:
        self._enviando = False
        self.boton_ok.setText("Reintentar")
        self._actualizar()
        self._error(f"No se registró: {mensaje}\nLas respuestas siguen cargadas: podés reintentar.")

    def reject(self) -> None:
        if self._enviando:
            return  # no cerrar con el registro en curso
        if self.respuestas() and QMessageBox.question(
                self, "Descartar revisión", "¿Cerrar sin registrar? Se pierde lo cargado.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        super().reject()


# =============================================================================
# Historial (supervisión)
# =============================================================================

class DialogoHistorial(QDialog, _ConTareas):
    def __init__(self, cliente_factory: Callable[[], MantenimientoCliente], item: Optional[EstadoItem] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._init_tareas()
        self._cliente_factory = cliente_factory
        self._registros: List[RegistroHistorial] = []
        self.setWindowTitle("Historial de revisiones" + (f" · {item.nombre}" if item else ""))
        self.setMinimumSize(900, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        titulo = QLabel(f"Historial de revisiones — {item.nombre if item else 'todos los ítems'}", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)
        self.label_estado = QLabel("Cargando…", self)
        self.label_estado.setObjectName("ayudaSeccion")
        layout.addWidget(self.label_estado)

        self.tabla = QTableWidget(self)
        self.tabla.setColumnCount(7)
        self.tabla.setHorizontalHeaderLabels(["Fecha y hora", "Ítem", "Cadencia", "Responsable", "Resultado",
                                              "Fallas", "Observaciones"])
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tabla.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        theme.estilizar_tabla(self.tabla)
        self.tabla.itemSelectionChanged.connect(self._cargar_detalle)
        layout.addWidget(self.tabla, 3)

        sub = QLabel("DETALLE DE LA REVISIÓN SELECCIONADA", self)
        sub.setObjectName("subtituloBloque")
        layout.addWidget(sub)
        self.tabla_detalle = QTableWidget(self)
        self.tabla_detalle.setColumnCount(4)
        self.tabla_detalle.setHorizontalHeaderLabels(["Tarea", "Crítica", "Estado", "Observación"])
        self.tabla_detalle.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla_detalle.verticalHeader().setVisible(False)
        self.tabla_detalle.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tabla_detalle.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        theme.estilizar_tabla(self.tabla_detalle, alto_fila=32)
        layout.addWidget(self.tabla_detalle, 2)

        botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        botones.rejected.connect(self.reject)
        layout.addWidget(botones)

        item_id = item.id if item else None
        self._ejecutar(lambda: self._cliente_factory().historial(item_id), self._mostrar,
                       lambda msg: self.label_estado.setText(f"No se pudo leer el historial: {msg}"))

    def _mostrar(self, registros: List[RegistroHistorial]) -> None:
        self._registros = registros
        self.label_estado.setText(f"{len(registros)} revisión(es), más recientes primero." if registros
                                  else "Todavía no hay revisiones registradas.")
        self.tabla.setRowCount(len(registros))
        for fila, r in enumerate(registros):
            valores = [formatear_fecha_hora(r.fecha_hora), r.item, FRECUENCIAS.get(r.frecuencia, r.frecuencia),
                       r.responsable or "—", None, f"{r.fallas} de {r.tareas}", r.observaciones or ""]
            for col, valor in enumerate(valores):
                if valor is not None:
                    self.tabla.setItem(fila, col, QTableWidgetItem(valor))
            self.tabla.setCellWidget(fila, 4, _celda_chip(RESULTADOS.get(r.resultado, r.resultado).upper(),
                                                          TONO_RESULTADO.get(r.resultado, "neutro")))
        self.tabla.resizeColumnsToContents()
        self.tabla.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        _ajustar_columnas_chip(self.tabla, (4,))
        if registros:
            self.tabla.selectRow(0)

    def _cargar_detalle(self) -> None:
        filas = self.tabla.selectionModel().selectedRows()
        if not filas or filas[0].row() >= len(self._registros):
            return
        registro_id = self._registros[filas[0].row()].id
        self._ejecutar(lambda: self._cliente_factory().detalle_registro(registro_id), self._mostrar_detalle,
                       lambda msg: self.label_estado.setText(f"No se pudo leer el detalle: {msg}"))

    def _mostrar_detalle(self, detalle: List[Dict[str, Any]]) -> None:
        self.tabla_detalle.setRowCount(len(detalle))
        for fila, d in enumerate(detalle):
            self.tabla_detalle.setItem(fila, 0, QTableWidgetItem(d.get("titulo") or ""))
            self.tabla_detalle.setItem(fila, 1, QTableWidgetItem("Sí" if d.get("critica") else ""))
            estado = d.get("estado") or ""
            self.tabla_detalle.setCellWidget(fila, 2, _celda_chip(
                RESPUESTAS.get(estado, estado).upper(), {"OK": "ok", "FALLA": "error"}.get(estado, "neutro")))
            self.tabla_detalle.setItem(fila, 3, QTableWidgetItem(d.get("observacion") or ""))
        _ajustar_columnas_chip(self.tabla_detalle, (2,), minimo=80)


# =============================================================================
# Plantillas (tareas)
# =============================================================================

class DialogoEditarTarea(QDialog):
    def __init__(self, items: List[tuple], tarea: Optional[TareaPlantilla] = None,
                 parent: Optional[QWidget] = None) -> None:
        """`items`: [(item_id, "Nombre · QR")] del inventario activo."""
        super().__init__(parent)
        self.setWindowTitle("Editar tarea" if tarea else "Nueva tarea de mantenimiento")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        form = QFormLayout()
        form.setSpacing(10)
        self.campo_titulo = QLineEdit(self)
        self.campo_titulo.setMaxLength(120)
        self.campo_descripcion = QPlainTextEdit(self)
        self.campo_descripcion.setFixedHeight(64)
        self.campo_descripcion.setPlaceholderText("Cómo se verifica (aparece en el checklist)")
        self.combo_frecuencia = QComboBox(self)
        for codigo, texto in FRECUENCIAS.items():
            self.combo_frecuencia.addItem(f"{texto} — {AYUDA_FRECUENCIA[codigo]}", codigo)
        self.check_critica = QCheckBox("Crítica: si falla o se vence, el ítem queda FUERA DE SERVICIO", self)
        self.radio_categoria = QRadioButton("Toda una categoría", self)
        self.radio_item = QRadioButton("Un ítem puntual", self)
        self.combo_categoria = QComboBox(self)
        for codigo, texto in CATEGORIAS.items():
            self.combo_categoria.addItem(texto, codigo)
        self.combo_item = QComboBox(self)
        for item_id, texto in items:
            self.combo_item.addItem(texto, item_id)
        self.spin_orden = QSpinBox(self)
        self.spin_orden.setRange(0, 999)
        self.spin_orden.setValue(100)
        self.spin_orden.setToolTip("Orden dentro del checklist (menor = primero)")
        alcance = QHBoxLayout()
        alcance.addWidget(self.radio_categoria)
        alcance.addWidget(self.radio_item)
        alcance.addStretch(1)
        form.addRow(theme.etiqueta_requerida("Título"), self.campo_titulo)
        form.addRow("Instrucciones", self.campo_descripcion)
        form.addRow(theme.etiqueta_requerida("Cadencia"), self.combo_frecuencia)
        form.addRow("", self.check_critica)
        form.addRow(theme.etiqueta_requerida("Aplica a"), alcance)
        form.addRow("Categoría", self.combo_categoria)
        form.addRow("Ítem", self.combo_item)
        form.addRow("Orden", self.spin_orden)
        layout.addLayout(form)
        self.botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                                        self)
        self.botones.accepted.connect(self.accept)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)

        self.radio_categoria.toggled.connect(self._actualizar)
        self.campo_titulo.textChanged.connect(self._actualizar)
        self.radio_item.setEnabled(bool(items))
        if tarea is not None:
            self.campo_titulo.setText(tarea.titulo)
            self.campo_descripcion.setPlainText(tarea.descripcion or "")
            self.combo_frecuencia.setCurrentIndex(max(0, self.combo_frecuencia.findData(tarea.frecuencia)))
            self.check_critica.setChecked(tarea.critica)
            self.spin_orden.setValue(tarea.orden)
            if tarea.item_id:
                self.radio_item.setChecked(True)
                self.combo_item.setCurrentIndex(max(0, self.combo_item.findData(tarea.item_id)))
            else:
                self.radio_categoria.setChecked(True)
                self.combo_categoria.setCurrentIndex(max(0, self.combo_categoria.findData(tarea.categoria)))
        else:
            self.radio_categoria.setChecked(True)
        self._actualizar()

    def _actualizar(self) -> None:
        por_categoria = self.radio_categoria.isChecked()
        self.combo_categoria.setEnabled(por_categoria)
        self.combo_item.setEnabled(not por_categoria)
        self.botones.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.campo_titulo.text().strip()))

    def datos(self) -> dict:
        por_categoria = self.radio_categoria.isChecked()
        return {
            "titulo": self.campo_titulo.text().strip(),
            "descripcion": self.campo_descripcion.toPlainText().strip(),
            "frecuencia": self.combo_frecuencia.currentData(),
            "critica": self.check_critica.isChecked(),
            "categoria": self.combo_categoria.currentData() if por_categoria else None,
            "item_id": None if por_categoria else self.combo_item.currentData(),
            "orden": self.spin_orden.value(),
        }


class DialogoPlantillas(QDialog, _ConTareas):
    def __init__(self, cliente_factory: Callable[[], MantenimientoCliente], operador_id: Optional[int],
                 rol: Optional[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._init_tareas()
        self._cliente_factory = cliente_factory
        self._operador_id, self._rol = operador_id, rol
        self._plantillas: List[TareaPlantilla] = []
        self._items: List[tuple] = []
        self.cambio = False
        admin = operador_id is not None and es_admin(rol)

        self.setWindowTitle("Plantillas de mantenimiento")
        self.setMinimumSize(860, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        titulo = QLabel("Tareas de mantenimiento preventivo", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)
        self.label_estado = QLabel("Cargando…", self)
        self.label_estado.setObjectName("ayudaSeccion")
        self.label_estado.setWordWrap(True)
        layout.addWidget(self.label_estado)

        acciones = QHBoxLayout()
        self.boton_nueva = QPushButton("+ Nueva tarea", self)
        self.boton_nueva.setObjectName("botonPrimarioRojo")
        self.boton_editar = QPushButton("✏️ Editar", self)
        self.boton_editar.setObjectName("botonPrimarioAzul")
        self.boton_desactivar = QPushButton("⛔ Desactivar", self)
        self.boton_desactivar.setObjectName("botonLimpiar")
        for boton in (self.boton_nueva, self.boton_editar, self.boton_desactivar):
            boton.setVisible(admin)
            acciones.addWidget(boton)
        if not admin:
            candado = QLabel("🔒 Solo un Administrador identificado puede crear, editar o desactivar tareas.", self)
            candado.setObjectName("ayudaSeccion")
            acciones.addWidget(candado)
        acciones.addStretch(1)
        layout.addLayout(acciones)
        self.boton_nueva.clicked.connect(lambda: self._editar(None))
        self.boton_editar.clicked.connect(lambda: self._editar(self._seleccionada()))
        self.boton_desactivar.clicked.connect(self._desactivar)

        self.tabla = QTableWidget(self)
        self.tabla.setColumnCount(5)
        self.tabla.setHorizontalHeaderLabels(["Cadencia", "Tarea", "Crítica", "Aplica a", "Orden"])
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tabla.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        theme.estilizar_tabla(self.tabla)
        self.tabla.itemSelectionChanged.connect(self._actualizar_botones)
        if admin:
            self.tabla.cellDoubleClicked.connect(lambda *_: self._editar(self._seleccionada()))
        layout.addWidget(self.tabla, 1)
        botones = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        botones.rejected.connect(self.reject)
        layout.addWidget(botones)
        self._admin = admin
        self._actualizar_botones()
        self._cargar()

    def _cargar(self) -> None:
        def leer():
            from app.services.inventario_service import InventarioCliente

            plantillas = self._cliente_factory().plantillas()
            try:
                items = [(i.id, f"{i.nombre} · {i.codigo_qr}") for i in InventarioCliente().listar()]
            except Exception:  # noqa: BLE001 - sin inventario se pueden igual editar las de categoría
                items = []
            return plantillas, items

        self._ejecutar(leer, self._mostrar, lambda msg: self.label_estado.setText(f"No se pudo leer: {msg}"))

    def _mostrar(self, datos: tuple) -> None:
        self._plantillas, self._items = datos
        self.label_estado.setText(f"{len(self._plantillas)} tarea(s) activas. Las CRÍTICAS sacan de servicio "
                                  "el ítem si fallan o se vencen.")
        self.tabla.setRowCount(len(self._plantillas))
        for fila, t in enumerate(self._plantillas):
            celda = QTableWidgetItem(f"{ICONOS_FRECUENCIA.get(t.frecuencia, '')} {FRECUENCIAS[t.frecuencia]}")
            celda.setData(Qt.ItemDataRole.UserRole, t.id)
            self.tabla.setItem(fila, 0, celda)
            tarea = QTableWidgetItem(t.titulo)
            tarea.setToolTip(t.descripcion or "")
            self.tabla.setItem(fila, 1, tarea)
            self.tabla.setItem(fila, 2, QTableWidgetItem("⚠️ Sí" if t.critica else ""))
            self.tabla.setItem(fila, 3, QTableWidgetItem(t.alcance))
            self.tabla.setItem(fila, 4, QTableWidgetItem(str(t.orden)))
        self._actualizar_botones()

    def _seleccionada(self) -> Optional[TareaPlantilla]:
        filas = self.tabla.selectionModel().selectedRows()
        if not filas:
            return None
        tid = self.tabla.item(filas[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((t for t in self._plantillas if t.id == tid), None)

    def _actualizar_botones(self) -> None:
        seleccion = self._seleccionada() is not None
        self.boton_editar.setEnabled(self._admin and seleccion)
        self.boton_desactivar.setEnabled(self._admin and seleccion)

    def _editar(self, tarea: Optional[TareaPlantilla]) -> None:
        if not self._admin:
            return
        dialogo = DialogoEditarTarea(self._items, tarea, self)
        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        datos, op, rol = dialogo.datos(), self._operador_id, self._rol
        tid = tarea.id if tarea else None
        self._ejecutar(lambda: self._cliente_factory().guardar_plantilla(op, rol, datos, tid),
                       self._al_guardar, lambda msg: QMessageBox.warning(self, "No se pudo guardar", msg))

    def _desactivar(self) -> None:
        tarea = self._seleccionada()
        if tarea is None or not self._admin:
            return
        if QMessageBox.question(self, "Desactivar tarea",
                                f"¿Desactivar «{tarea.titulo}»? Deja de pedirse en los checklists; "
                                "el historial se conserva.") != QMessageBox.StandardButton.Yes:
            return
        op, rol = self._operador_id, self._rol
        self._ejecutar(lambda: self._cliente_factory().desactivar_plantilla(op, rol, tarea.id),
                       self._al_guardar, lambda msg: QMessageBox.warning(self, "No se pudo desactivar", msg))

    def _al_guardar(self, _resultado) -> None:
        self.cambio = True
        self._cargar()


# =============================================================================
# Ficha del activo (lo que se abre al escanear un QR)
# =============================================================================

class DialogoFichaActivo(QDialog, _ConTareas):
    """Datos del activo, su QR y una fila por cadencia con el estado y el botón
    para completar la revisión en el momento (la más urgente, resaltada)."""

    def __init__(self, ficha: FichaActivo, cliente_factory: Callable[[], MantenimientoCliente],
                 operador_id: Optional[int], operador_nombre: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._init_tareas()
        self._cliente_factory = cliente_factory
        self._operador_id = operador_id
        self._operador_nombre = operador_nombre
        self.hubo_cambios = False
        self.setWindowTitle(f"Ficha del activo · {ficha.item.codigo_qr}")
        self.setMinimumWidth(720)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 18, 20, 16)
        self._layout.setSpacing(12)
        self._contenido: Optional[QWidget] = None
        self._mostrar(ficha)

    def _mostrar(self, ficha: FichaActivo) -> None:
        from app.ui.qr_etiquetas import DialogoQrActivo, qr_pixmap

        self.ficha = ficha
        if self._contenido is not None:
            self._layout.removeWidget(self._contenido)
            self._contenido.deleteLater()
        contenido = self._contenido = QWidget(self)
        layout = QVBoxLayout(contenido)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        item = ficha.item

        cabecera = QHBoxLayout()
        qr = QPushButton(contenido)
        qr.setIcon(qr_pixmap(item.codigo_qr, 120))
        qr.setIconSize(qr_pixmap(item.codigo_qr, 120).size())
        qr.setFlat(True)
        qr.setCursor(Qt.CursorShape.PointingHandCursor)
        qr.setToolTip("Ver / imprimir el QR")
        qr.clicked.connect(lambda: DialogoQrActivo(item.codigo_qr, item.nombre, item.ubicacion, self).exec())
        cabecera.addWidget(qr, 0, Qt.AlignmentFlag.AlignTop)
        datos = QVBoxLayout()
        nombre = QLabel(item.nombre, contenido)
        nombre.setObjectName("pageTitle")
        nombre.setWordWrap(True)
        datos.addWidget(nombre)
        detalle = QLabel(" · ".join(filter(None, (item.codigo_qr, item.categoria_legible, item.ubicacion,
                                                  item.marca_modelo, f"Serie {item.numero_serie}"
                                                  if item.numero_serie else None))), contenido)
        detalle.setObjectName("pageSubtitle")
        detalle.setWordWrap(True)
        datos.addWidget(detalle)
        fila_estado = QHBoxLayout()
        texto_estado = ESTADOS_INVENTARIO.get(item.estado, item.estado).upper()
        if item.fuera_servicio_automatico:
            texto_estado = "⚙️ " + texto_estado + " (mantenimiento)"
        fila_estado.addWidget(_crear_chip(texto_estado, TONO_ESTADO_INVENTARIO.get(item.estado, "neutro"), contenido))
        if ficha.estado is not None and ficha.estado.estado_general not in ("OPERATIVO", item.estado):
            fila_estado.addWidget(_crear_chip(ESTADOS_GENERALES[ficha.estado.estado_general].upper(),
                                              TONO_ESTADO_GENERAL[ficha.estado.estado_general], contenido))
        fila_estado.addStretch(1)
        datos.addLayout(fila_estado)
        if item.motivo_estado or (ficha.estado and ficha.estado.motivos):
            motivos = item.motivo_estado or "; ".join(f"{m.get('titulo')} ({m.get('estado', '').lower()})"
                                                     for m in ficha.estado.motivos)
            label = QLabel(f"⚠️ {motivos}", contenido)
            label.setObjectName("ayudaSeccion")
            label.setWordWrap(True)
            datos.addWidget(label)
        datos.addStretch(1)
        cabecera.addLayout(datos, 1)
        layout.addLayout(cabecera)

        sub = QLabel("CHECKLISTS DE MANTENIMIENTO", contenido)
        sub.setObjectName("subtituloBloque")
        layout.addWidget(sub)
        urgente = ficha.frecuencia_mas_urgente
        self.botones_revision: Dict[str, QPushButton] = {}
        for frecuencia in FRECUENCIAS:
            layout.addWidget(self._fila_cadencia(ficha, frecuencia, urgente, contenido))
        if ficha.estado is None:
            vacio = QLabel("Este activo no tiene tareas de mantenimiento: un Administrador puede asignarlas en "
                           "Mantenimiento → ⚙️ Plantillas (por categoría o para este ítem).", contenido)
            vacio.setObjectName("ayudaSeccion")
            vacio.setWordWrap(True)
            layout.addWidget(vacio)
        if self._operador_id is None:
            aviso = QLabel("👤 Para firmar una revisión identificate con el chip de la barra superior.", contenido)
            aviso.setObjectName("ayudaSeccion")
            layout.addWidget(aviso)

        botones = QHBoxLayout()
        historial = QPushButton("📜 Historial", contenido)
        historial.setObjectName("botonLimpiar")
        historial.clicked.connect(lambda: DialogoHistorial(self._cliente_factory, ficha.estado, self).exec()
                                  if ficha.estado else None)
        historial.setEnabled(ficha.estado is not None)
        botones.addWidget(historial)
        botones.addStretch(1)
        cerrar = QPushButton("Cerrar", contenido)
        cerrar.clicked.connect(self.accept)
        botones.addWidget(cerrar)
        layout.addLayout(botones)
        self._layout.addWidget(contenido)

    def _fila_cadencia(self, ficha: FichaActivo, frecuencia: str, urgente: Optional[str], parent: QWidget) -> QFrame:
        marco = QFrame(parent)
        marco.setObjectName("seccionFormulario")
        fila = QHBoxLayout(marco)
        fila.setContentsMargins(12, 8, 12, 8)
        icono = QLabel(f"{ICONOS_FRECUENCIA[frecuencia]}  {FRECUENCIAS[frecuencia]}", marco)
        icono.setObjectName("preguntaConmutable")
        icono.setMinimumWidth(130)
        fila.addWidget(icono)
        resumen = ficha.resumen(frecuencia)
        if resumen is None:
            texto = QLabel("Sin tareas", marco)
            texto.setObjectName("ayudaSeccion")
            fila.addWidget(texto, 1)
            return marco
        fila.addWidget(_crear_chip(ESTADOS_TAREA[resumen.estado].upper(), TONO_ESTADO_TAREA[resumen.estado], marco))
        texto = QLabel(f"{resumen.total} tarea(s) · última {formatear_fecha_hora(resumen.ultima_revision)} · "
                       f"vence {formatear_fecha_hora(resumen.proximo_vencimiento)}", marco)
        texto.setObjectName("ayudaSeccion")
        texto.setWordWrap(True)
        fila.addWidget(texto, 1)
        boton = QPushButton("✅ Completar" if frecuencia == urgente else "Revisar", marco)
        boton.setObjectName("botonPrimarioRojo" if frecuencia == urgente else "botonLimpiar")
        boton.setCursor(Qt.CursorShape.PointingHandCursor)
        boton.setEnabled(self._operador_id is not None)
        boton.clicked.connect(lambda _=False, f=frecuencia: self.completar(f))
        self.botones_revision[frecuencia] = boton
        fila.addWidget(boton)
        return marco

    def completar(self, frecuencia: str) -> None:
        if self._operador_id is None or self.ficha.estado is None:
            return
        operador, item = self._operador_id, self.ficha.estado
        dialogo = DialogoChecklist(
            item, frecuencia, self.ficha.tareas, self._operador_nombre or f"operador {operador}",
            lambda resp, obs, general: self._cliente_factory().registrar(operador, item.id, frecuencia, resp, obs,
                                                                          general),
            self)
        if dialogo.exec() != QDialog.DialogCode.Accepted or dialogo.resultado is None:
            return
        self.hubo_cambios = True
        r = dialogo.resultado
        if r.cambio_estado:
            QMessageBox.warning(self, "Cambio de estado", f"«{item.nombre}» quedó "
                                f"{ESTADOS_INVENTARIO.get(r.estado_item, r.estado_item).upper()}.")
        codigo = self.ficha.item.codigo_qr
        self._ejecutar(lambda: self._cliente_factory().ficha_por_codigo(codigo), self._mostrar,
                       lambda msg: QMessageBox.warning(self, "No se pudo actualizar la ficha", msg))


# =============================================================================
# Página
# =============================================================================

class PaginaMantenimiento(QWidget, _ConTareas):
    def __init__(self, parent: Optional[QWidget] = None,
                 cliente_factory: Callable[[], MantenimientoCliente] = MantenimientoCliente) -> None:
        super().__init__(parent)
        self._init_tareas()
        self._cliente_factory = cliente_factory
        self._items: List[EstadoItem] = []
        self._frecuencia = DIARIA
        self._cargando = False
        self._sin_credenciales = False
        self._operador_id: Optional[int] = None
        self._operador_nombre = ""
        self._rol: Optional[str] = None
        self._abriendo = False

        self._timer = QTimer(self)
        self._timer.setInterval(INTERVALO_REFRESCO_MS)
        self._timer.timeout.connect(self.refrescar)
        self._construir()
        self.set_operador(None, None, "")

    # -- Construcción ------------------------------------------------------------

    def _construir(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        cabecera = QHBoxLayout()
        textos = QVBoxLayout()
        textos.setSpacing(2)
        titulo = QLabel("Checklists y Mantenimiento Preventivo", self)
        titulo.setObjectName("pageTitle")
        subtitulo = QLabel("Revisiones por cadencia. Una tarea crítica fallada o vencida saca el ítem de "
                           "servicio automáticamente.", self)
        subtitulo.setObjectName("pageSubtitle")
        textos.addWidget(titulo)
        textos.addWidget(subtitulo)
        cabecera.addLayout(textos, 1)
        self.chip_operador = _crear_chip("", "neutro", self)
        cabecera.addWidget(self.chip_operador, 0, Qt.AlignmentFlag.AlignVCenter)
        self.chip_sync = _crear_chip("☁️ Sin sincronizar", "neutro", self)
        cabecera.addWidget(self.chip_sync, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(cabecera)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.tarjeta_al_dia = TarjetaKPI("Ítems operativos", "✅", self)
        self.tarjeta_observaciones = TarjetaKPI("Con observaciones", "⚠️", self)
        self.tarjeta_fuera = TarjetaKPI("Fuera de servicio", "⛔", self)
        self.tarjeta_pendientes = TarjetaKPI("Revisiones pendientes", "📋", self)
        self.tarjeta_pendientes.clicked.connect(lambda: self.check_pendientes.setChecked(True))
        for i, tarjeta in enumerate((self.tarjeta_al_dia, self.tarjeta_observaciones, self.tarjeta_fuera,
                                     self.tarjeta_pendientes)):
            grid.addWidget(tarjeta, 0, i)
        layout.addLayout(grid)

        pestanas = QHBoxLayout()
        pestanas.setSpacing(8)
        self.grupo_frecuencia = QButtonGroup(self)
        self.botones_frecuencia: Dict[str, QPushButton] = {}
        for codigo, texto in FRECUENCIAS.items():
            boton = QPushButton(f"{ICONOS_FRECUENCIA[codigo]}  {texto}", self)
            boton.setObjectName("toggleVista")
            boton.setCheckable(True)
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            boton.setToolTip(AYUDA_FRECUENCIA[codigo])
            boton.clicked.connect(lambda _=False, c=codigo: self.set_frecuencia(c))
            self.grupo_frecuencia.addButton(boton)
            self.botones_frecuencia[codigo] = boton
            pestanas.addWidget(boton)
        self.botones_frecuencia[DIARIA].setChecked(True)
        self.label_ayuda_frecuencia = QLabel(AYUDA_FRECUENCIA[DIARIA], self)
        self.label_ayuda_frecuencia.setObjectName("ayudaSeccion")
        pestanas.addWidget(self.label_ayuda_frecuencia)
        pestanas.addStretch(1)
        layout.addLayout(pestanas)

        layout.addWidget(self._crear_tarjeta_grilla(), 1)

    def _crear_tarjeta_grilla(self) -> QFrame:
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
        self.label_titulo_grilla = QLabel("", cabecera)
        self.label_titulo_grilla.setObjectName("tituloSeccion")
        fila.addWidget(self.label_titulo_grilla)
        self.label_cantidad = QLabel("", cabecera)
        self.label_cantidad.setObjectName("ayudaSeccion")
        fila.addWidget(self.label_cantidad)
        fila.addStretch(1)
        self.campo_buscar = QLineEdit(cabecera)
        self.campo_buscar.setPlaceholderText("🔎 Buscar ítem, código QR o ubicación…")
        self.campo_buscar.setClearButtonEnabled(True)
        self.campo_buscar.setMinimumWidth(240)
        self.campo_buscar.textChanged.connect(self._pintar_tabla)
        fila.addWidget(self.campo_buscar)
        self.check_pendientes = QCheckBox("Solo lo que requiere atención", cabecera)
        self.check_pendientes.toggled.connect(self._pintar_tabla)
        fila.addWidget(self.check_pendientes)
        layout_marco.addWidget(cabecera)

        contenido = QWidget(marco)
        layout_contenido = QVBoxLayout(contenido)
        layout_contenido.setContentsMargins(16, 14, 16, 16)
        layout_contenido.setSpacing(12)

        acciones = QHBoxLayout()
        acciones.setSpacing(10)
        self.boton_completar = QPushButton("✅ Completar revisión", contenido)
        self.boton_completar.setObjectName("botonPrimarioRojo")
        self.boton_completar.clicked.connect(self.completar_revision)
        self.boton_historial = QPushButton("📜 Historial", contenido)
        self.boton_historial.setObjectName("botonLimpiar")
        self.boton_historial.clicked.connect(self._abrir_historial)
        self.boton_plantillas = QPushButton("⚙️ Plantillas", contenido)
        self.boton_plantillas.setObjectName("botonLimpiar")
        self.boton_plantillas.clicked.connect(self._abrir_plantillas)
        self.boton_configurar = QPushButton("🔑 Configurar conexión", contenido)
        self.boton_configurar.setObjectName("botonPrimarioAzul")
        self.boton_configurar.setVisible(False)
        self.boton_configurar.clicked.connect(self._configurar_conexion)
        self.boton_refrescar = QPushButton("⟳ Actualizar", contenido)
        self.boton_refrescar.setObjectName("botonHerramienta")
        self.boton_refrescar.clicked.connect(self.refrescar)
        self.boton_escanear = QPushButton("🔳 Escanear QR", contenido)
        self.boton_escanear.setObjectName("botonPrimarioAzul")
        self.boton_escanear.setToolTip("Pistola lectora, código a mano o cámara (Ctrl+K desde cualquier sección)")
        self.boton_escanear.clicked.connect(self.escanear)
        for boton in (self.boton_completar, self.boton_escanear, self.boton_historial, self.boton_plantillas):
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            acciones.addWidget(boton)
        acciones.addStretch(1)
        acciones.addWidget(self.boton_configurar)
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
        cab.setSectionResizeMode(COL_ITEM, QHeaderView.ResizeMode.Stretch)
        for col in COLUMNAS_CHIP:
            cab.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        theme.estilizar_tabla(self.tabla)
        self.tabla.itemSelectionChanged.connect(self._actualizar_botones)
        self.tabla.cellDoubleClicked.connect(lambda *_: self.completar_revision())
        layout_contenido.addWidget(self.tabla, 1)

        self.label_vacio = QLabel("", contenido)
        self.label_vacio.setObjectName("ayudaSeccion")
        self.label_vacio.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_vacio.setWordWrap(True)
        self.label_vacio.setVisible(False)
        layout_contenido.addWidget(self.label_vacio)
        layout_marco.addWidget(contenido, 1)
        self._actualizar_titulo_grilla()
        return marco

    # -- Sesión / cadencia --------------------------------------------------------

    def set_operador(self, operador_id: Optional[int], rol: Optional[str], nombre: str = "") -> None:
        self._operador_id, self._rol, self._operador_nombre = operador_id, rol, nombre or ""
        if operador_id is None:
            self.chip_operador.setText("👤 Identificate para firmar revisiones")
            theme.set_tono(self.chip_operador, "alerta")
        else:
            self.chip_operador.setText(f"👤 Firma: {self._operador_nombre or 'operador'}"
                                       + (" · Admin" if es_admin(rol) else ""))
            theme.set_tono(self.chip_operador, "ok")
        self.chip_operador.setMinimumWidth(self.chip_operador.sizeHint().width())
        self._actualizar_botones()

    def set_frecuencia(self, frecuencia: str) -> None:
        self._frecuencia = frecuencia
        self.botones_frecuencia[frecuencia].setChecked(True)
        self.label_ayuda_frecuencia.setText(AYUDA_FRECUENCIA[frecuencia])
        self._actualizar_titulo_grilla()
        self._actualizar_kpis()
        self._pintar_tabla()

    def _actualizar_titulo_grilla(self) -> None:
        self.label_titulo_grilla.setText(f"CUMPLIMIENTO — {FRECUENCIAS[self._frecuencia].upper()}")

    # -- Ciclo de vida ------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().showEvent(event)
        if not self._sin_credenciales:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().hideEvent(event)
        self._timer.stop()

    # -- Carga ----------------------------------------------------------------------

    def _faltan_credenciales(self) -> Optional[str]:
        if self._cliente_factory is not MantenimientoCliente:
            return None
        try:
            cargar_credenciales()
        except SupabaseConfigError as e:
            return str(e)
        return None

    def refrescar(self) -> None:
        if self._cargando:
            return
        faltan = self._faltan_credenciales()
        self._sin_credenciales = bool(faltan)
        self.boton_configurar.setVisible(bool(faltan))
        if faltan:
            self._timer.stop()
            self._set_chip("🔑 Conexión sin configurar", "alerta", faltan)
            self.label_vacio.setText("Esta PC todavía no tiene la conexión con la nube.\n"
                                     "Tocá \"🔑 Configurar conexión\" y pegá la service key de Supabase.")
            self.label_vacio.setVisible(True)
            self._actualizar_botones()
            return
        self._cargando = True
        self.boton_refrescar.setEnabled(False)
        self._set_chip("☁️ Sincronizando…", "neutro")
        self._ejecutar(lambda: self._cliente_factory().items(), self._al_cargar, self._al_fallar_carga)

    def _configurar_conexion(self) -> None:
        if DialogoCredencialesSupabase(self).exec() == QDialog.DialogCode.Accepted:
            self.refrescar()

    def _al_cargar(self, items: List[EstadoItem]) -> None:
        self._cargando = False
        self.boton_refrescar.setEnabled(True)
        self._items = items
        self._actualizar_kpis()
        self._pintar_tabla()
        self._set_chip(f"☁️ Sincronizado {ahora_cuartel():%H:%M}", "ok")

    def _al_fallar_carga(self, mensaje: str) -> None:
        self._cargando = False
        self.boton_refrescar.setEnabled(True)
        self._set_chip("☁️ Sin conexión", "error", f"{mensaje}\nSe muestran los últimos datos leídos.")
        if not self._items:
            self.label_vacio.setText(f"No se pudo leer el estado de mantenimiento.\n{mensaje}")
            self.label_vacio.setVisible(True)

    def _set_chip(self, texto: str, tono: str, tooltip: str = "") -> None:
        self.chip_sync.setText(texto)
        self.chip_sync.setToolTip(tooltip)
        theme.set_tono(self.chip_sync, tono)
        self.chip_sync.setMinimumWidth(self.chip_sync.sizeHint().width())

    # -- KPIs y grilla ----------------------------------------------------------------

    def _actualizar_kpis(self) -> None:
        fuera = sum(1 for i in self._items if i.estado == "FUERA_DE_SERVICIO" or i.estado_general == FUERA_DE_SERVICIO)
        observaciones = sum(1 for i in self._items if i.estado_general == "CON_OBSERVACIONES")
        self.tarjeta_al_dia.set_valor(str(len(self._items) - fuera - observaciones))
        self.tarjeta_observaciones.set_valor(str(observaciones))
        self.tarjeta_fuera.set_valor(str(fuera))
        pendientes = sum(1 for i in self._items
                         if (r := i.resumen(self._frecuencia)) and r.estado in (PENDIENTE, VENCIDA, FALLA))
        self.tarjeta_pendientes.set_valor(str(pendientes))

    def items_visibles(self) -> List[EstadoItem]:
        palabras = self.campo_buscar.text().lower().split()
        solo_atencion = self.check_pendientes.isChecked()
        visibles = []
        for i in self._items:
            resumen = i.resumen(self._frecuencia)
            if resumen is None:
                continue
            if solo_atencion and resumen.estado not in ESTADOS_ATENCION:
                continue
            texto = f"{i.nombre} {i.codigo_qr} {i.ubicacion} {CATEGORIAS.get(i.categoria, '')}".lower()
            if all(p in texto for p in palabras):
                visibles.append(i)
        # Lo más urgente arriba.
        gravedad = {FALLA: 0, VENCIDA: 1, PENDIENTE: 2, POR_VENCER: 3}
        return sorted(visibles, key=lambda i: (gravedad.get(i.resumen(self._frecuencia).estado, 9), i.nombre.lower()))

    def _pintar_tabla(self) -> None:
        seleccionado = self.item_seleccionado()
        visibles = self.items_visibles()
        self.tabla.setRowCount(len(visibles))
        for fila, i in enumerate(visibles):
            r = i.resumen(self._frecuencia)
            celda = QTableWidgetItem(i.nombre)
            celda.setData(Qt.ItemDataRole.UserRole, i.id)
            self.tabla.setItem(fila, COL_ITEM, celda)
            for col, texto in ((COL_CODIGO, i.codigo_qr), (COL_CATEGORIA, CATEGORIAS.get(i.categoria, i.categoria)),
                               (COL_UBICACION, i.ubicacion), (COL_TAREAS, str(r.total)),
                               (COL_ULTIMA, formatear_fecha_hora(r.ultima_revision)),
                               (COL_VENCE, formatear_fecha_hora(r.proximo_vencimiento))):
                celda = QTableWidgetItem(texto)
                if col in (COL_TAREAS, COL_ULTIMA, COL_VENCE):
                    celda.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.tabla.setItem(fila, col, celda)
            detalle = (f"{r.al_dia} al día · {r.por_vencer} por vencer · {r.pendientes} pendientes · "
                       f"{r.vencidas} vencidas · {r.fallas} con falla")
            self.tabla.setCellWidget(fila, COL_CUMPLIMIENTO, _celda_chip(
                ESTADOS_TAREA[r.estado].upper(), TONO_ESTADO_TAREA[r.estado], detalle))
            texto_estado = ESTADOS_INVENTARIO.get(i.estado, i.estado).upper()
            if i.fuera_servicio_automatico:
                texto_estado = "⚙️ " + texto_estado
            tooltip = i.motivo_estado or (f"Evaluación: {ESTADOS_GENERALES.get(i.estado_general, i.estado_general)}"
                                          + "".join(f"\n• {m.get('titulo')} ({m.get('estado', '').lower()})"
                                                    for m in i.motivos))
            self.tabla.setCellWidget(fila, COL_ESTADO, _celda_chip(
                texto_estado, TONO_ESTADO_INVENTARIO.get(i.estado, TONO_ESTADO_GENERAL.get(i.estado_general, "neutro")),
                tooltip))
        self.tabla.resizeColumnsToContents()
        self.tabla.horizontalHeader().setSectionResizeMode(COL_ITEM, QHeaderView.ResizeMode.Stretch)
        _ajustar_columnas_chip(self.tabla, COLUMNAS_CHIP)

        con_tareas = sum(1 for i in self._items if i.resumen(self._frecuencia))
        self.label_cantidad.setText(f"{len(visibles)} de {con_tareas} ítem(s)" if con_tareas else "")
        if not self._items:
            self.label_vacio.setText("No hay ítems con tareas de mantenimiento. Cargá los activos en Inventario: "
                                     "las tareas se asignan solas según su categoría.")
        elif not con_tareas:
            self.label_vacio.setText(f"Ningún ítem tiene tareas {FRECUENCIAS[self._frecuencia].lower()}.")
        else:
            self.label_vacio.setText("Todo al día para esta cadencia ✅" if self.check_pendientes.isChecked()
                                     else "Ningún ítem coincide con la búsqueda.")
        self.label_vacio.setVisible(not visibles)
        if seleccionado is not None:
            for f in range(self.tabla.rowCount()):
                if self.tabla.item(f, COL_ITEM).data(Qt.ItemDataRole.UserRole) == seleccionado.id:
                    self.tabla.selectRow(f)
                    break
        self._actualizar_botones()

    def item_seleccionado(self) -> Optional[EstadoItem]:
        modelo = self.tabla.selectionModel()
        filas = modelo.selectedRows() if modelo else []
        if not filas:
            return None
        celda = self.tabla.item(filas[0].row(), COL_ITEM)
        item_id = celda.data(Qt.ItemDataRole.UserRole) if celda is not None else None
        return next((i for i in self._items if i.id == item_id), None)

    def _actualizar_botones(self) -> None:
        hay = self.item_seleccionado() is not None
        self.boton_completar.setEnabled(hay and not self._abriendo and not self._sin_credenciales)
        self.boton_completar.setToolTip("" if self._operador_id is not None else
                                        "Para firmar la revisión identificate con el chip 👤 de la barra superior.")
        self.boton_historial.setEnabled(not self._sin_credenciales)
        self.boton_plantillas.setEnabled(not self._sin_credenciales)

    # -- Acciones ---------------------------------------------------------------------

    def completar_revision(self) -> None:
        item = self.item_seleccionado()
        if item is None or self._abriendo:
            return
        if self._operador_id is None:
            QMessageBox.information(self, "Identificate para firmar",
                                    "La revisión queda firmada por el operador identificado. Identificate con "
                                    "el chip 👤 de la barra superior (bombero + PIN) y volvé a intentar.")
            return
        self._abriendo = True
        self._actualizar_botones()
        frecuencia = self._frecuencia
        self._ejecutar(lambda: self._cliente_factory().tareas_item(item.id),
                       lambda tareas: self._abrir_checklist(item, frecuencia, tareas),
                       self._al_fallar_apertura)

    def _al_fallar_apertura(self, mensaje: str) -> None:
        self._abriendo = False
        self._actualizar_botones()
        QMessageBox.warning(self, "No se pudo abrir la revisión", mensaje)

    def _abrir_checklist(self, item: EstadoItem, frecuencia: str, tareas: List[TareaItem]) -> None:
        self._abriendo = False
        self._actualizar_botones()
        if not any(t.frecuencia == frecuencia for t in tareas):
            QMessageBox.information(self, "Sin tareas", "Las tareas de este ítem cambiaron. Actualizá la lista.")
            self.refrescar()
            return
        operador = self._operador_id
        dialogo = DialogoChecklist(
            item, frecuencia, tareas, self._operador_nombre or f"operador {operador}",
            lambda resp, obs, general: self._cliente_factory().registrar(operador, item.id, frecuencia, resp, obs,
                                                                          general),
            self)
        if dialogo.exec() != QDialog.DialogCode.Accepted or dialogo.resultado is None:
            return
        r = dialogo.resultado
        texto = f"Revisión registrada: {RESULTADOS.get(r.resultado, r.resultado)}."
        if r.cambio_estado and r.estado_item == FUERA_DE_SERVICIO:
            QMessageBox.warning(self, "Ítem fuera de servicio",
                                f"{texto}\n\n«{item.nombre}» pasó a FUERA DE SERVICIO por mantenimiento:\n"
                                + "\n".join(f"• {m.get('titulo')} ({m.get('estado', '').lower()})"
                                            for m in r.motivos if m.get('critica')))
        elif r.cambio_estado:
            QMessageBox.information(self, "Ítem operativo", f"{texto}\n\n«{item.nombre}» volvió a OPERATIVO.")
        else:
            self._set_chip(f"✅ {texto}", "ok")
        self.refrescar()

    # -- QR ---------------------------------------------------------------------------

    def escanear(self) -> None:
        from app.ui.qr_escaner import pedir_codigo_qr

        codigo = pedir_codigo_qr(self)
        if codigo:
            self.abrir_por_codigo(codigo)

    def abrir_por_codigo(self, codigo: str) -> None:
        """Busca el activo del QR y abre su ficha con los checklists (MainWindow lo
        llama también al leer una pistola desde cualquier sección)."""
        if self._sin_credenciales or self._faltan_credenciales():
            QMessageBox.information(self, "Sin conexión con la nube",
                                    "Configurá la conexión con Supabase para buscar activos por QR.")
            return
        self._set_chip(f"🔳 Buscando {codigo}…", "neutro")
        self._ejecutar(lambda: self._cliente_factory().ficha_por_codigo(codigo), self._abrir_ficha,
                       lambda msg: (self._set_chip("🔳 Código no encontrado", "alerta", msg),
                                    QMessageBox.warning(self, f"Código {codigo}", msg)))

    def _abrir_ficha(self, ficha: FichaActivo) -> None:
        self._set_chip(f"🔳 {ficha.item.codigo_qr}", "ok")
        dialogo = DialogoFichaActivo(ficha, self._cliente_factory, self._operador_id, self._operador_nombre, self)
        dialogo.exec()
        if dialogo.hubo_cambios:
            self.refrescar()

    def _abrir_historial(self) -> None:
        DialogoHistorial(self._cliente_factory, self.item_seleccionado(), self).exec()

    def _abrir_plantillas(self) -> None:
        dialogo = DialogoPlantillas(self._cliente_factory, self._operador_id, self._rol, self)
        dialogo.exec()
        if dialogo.cambio:
            self.refrescar()
