"""
Página "Inventario" del escritorio: ABM de los activos del cuartel en
Supabase (vehículos, herramientas, equipos de protección respiratoria, EPP...).

  * Tarjetas KPI por estado (Total / Operativos / En reparación / Fuera de
    servicio): clic = filtro rápido.
  * Grilla con buscador (nombre, código QR, marca, serie, ubicación) y filtros
    por estado, ubicación y categoría; opción de ver los dados de baja.
  * Gestión (Nuevo / Editar / Dar de baja): SOLO para el rol ADMIN de la
    sesión de operador. Para el resto los controles quedan OCULTOS y se
    muestra la página en modo lectura. `set_operador` la llama MainWindow
    cada vez que cambia la sesión (chip 👤). El servicio y la base vuelven a
    verificar el rol (ver app/services/inventario_service.py).

Códigos QR: miniatura en la grilla, "🔳 Ver QR" (guardar / imprimir la
etiqueta), "🏷️ Etiquetas QR" (PDF A4 con todos los ítems filtrados, para
rotular) y "📋 Ficha y checklists" / "🔳 Escanear QR", que abren la ficha del
activo en Mantenimiento (señal `ficha_solicitada`, la conecta MainWindow).

Toda llamada a la nube corre en un TareaSupabase (QThread).
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Set

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.roles import ROL_ADMIN, normalizar_rol
from app.services.inventario_service import (
    CATEGORIAS,
    ESTADO_EN_REPARACION,
    ESTADO_FUERA_DE_SERVICIO,
    ESTADO_OPERATIVO,
    ESTADOS,
    InventarioCliente,
    ItemInventario,
)
from app.services.supabase_client import SupabaseConfigError, cargar_credenciales
from app.ui import theme
from app.ui.libro_guardia_view import DialogoCredencialesSupabase, _crear_chip, ahora_cuartel, formatear_fecha_hora
from app.ui.qr_etiquetas import DialogoQrActivo, activos_para_etiquetas, exportar_etiquetas, qr_icono
from app.ui.widgets import TarjetaKPI
from app.workers.supabase_tarea import TareaSupabase

INTERVALO_REFRESCO_MS = 2 * 60 * 1000
COLUMNAS = ["Código QR", "Nombre", "Categoría", "Ubicación", "Estado", "Marca / Modelo", "N° de serie",
            "Última modificación"]
COL_CODIGO, COL_NOMBRE, COL_CATEGORIA, COL_UBICACION, COL_ESTADO, COL_MARCA, COL_SERIE, COL_MODIF = range(8)
ANCHO_MIN_ESTADO = 140
TODOS = "__todos__"

TONO_ESTADO = {ESTADO_OPERATIVO: "ok", ESTADO_EN_REPARACION: "alerta", ESTADO_FUERA_DE_SERVICIO: "error"}
UBICACIONES_SUGERIDAS = ["Pañol", "Sala de máquinas", "Depósito", "Taller", "Guardia", "Oficina",
                         "Sala de capacitación"]


def es_admin(rol: Optional[str]) -> bool:
    return normalizar_rol(rol) == ROL_ADMIN


# =============================================================================
# Diálogo de alta / edición
# =============================================================================

class DialogoItemInventario(QDialog):
    def __init__(self, ubicaciones: List[str], item: Optional[ItemInventario] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._item = item
        self.setWindowTitle("Editar ítem del inventario" if item else "Nuevo ítem del inventario")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        titulo = QLabel(f"Editar: {item.nombre}" if item else "Alta de un activo del cuartel", self)
        titulo.setObjectName("tituloSeccion")
        layout.addWidget(titulo)

        form = QFormLayout()
        form.setSpacing(10)
        self.campo_codigo = QLineEdit(self)
        self.campo_codigo.setMaxLength(40)
        self.campo_codigo.setPlaceholderText("Vacío = se genera solo (FS-XXXXXXXX)")
        self.campo_nombre = QLineEdit(self)
        self.campo_nombre.setMaxLength(120)
        self.campo_nombre.setPlaceholderText("Ej. Equipo ERA MSA G1 N° 3")
        self.combo_categoria = QComboBox(self)
        for codigo, texto in CATEGORIAS.items():
            self.combo_categoria.addItem(texto, codigo)
        self.combo_ubicacion = QComboBox(self)
        self.combo_ubicacion.setEditable(True)
        self.combo_ubicacion.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_ubicacion.addItems(sorted({*ubicaciones, *UBICACIONES_SUGERIDAS}, key=str.lower))
        self.combo_ubicacion.setCurrentIndex(-1)
        self.combo_ubicacion.lineEdit().setPlaceholderText("Dónde está físicamente (pañol, móvil, taller…)")
        self.combo_estado = QComboBox(self)
        for codigo, texto in ESTADOS.items():
            self.combo_estado.addItem(texto, codigo)
        self.campo_marca = QLineEdit(self)
        self.campo_serie = QLineEdit(self)
        self.campo_observaciones = QPlainTextEdit(self)
        self.campo_observaciones.setFixedHeight(70)

        form.addRow("Código QR", self.campo_codigo)
        form.addRow(theme.etiqueta_requerida("Nombre"), self.campo_nombre)
        form.addRow(theme.etiqueta_requerida("Categoría"), self.combo_categoria)
        form.addRow(theme.etiqueta_requerida("Ubicación física"), self.combo_ubicacion)
        form.addRow(theme.etiqueta_requerida("Estado"), self.combo_estado)
        form.addRow("Marca / Modelo", self.campo_marca)
        form.addRow("N° de serie", self.campo_serie)
        form.addRow("Observaciones", self.campo_observaciones)
        layout.addLayout(form)

        self.botones = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self.botones.button(QDialogButtonBox.StandardButton.Ok).setText("Guardar cambios" if item else "Dar de alta")
        self.botones.accepted.connect(self.accept)
        self.botones.rejected.connect(self.reject)
        layout.addWidget(self.botones)

        if item is not None:
            self._precargar(item)
        for senal in (self.campo_nombre.textChanged, self.combo_ubicacion.currentTextChanged):
            senal.connect(self._validar)
        self._validar()

    def _precargar(self, item: ItemInventario) -> None:
        self.campo_codigo.setText(item.codigo_qr)
        self.campo_nombre.setText(item.nombre)
        self.combo_categoria.setCurrentIndex(max(0, self.combo_categoria.findData(item.categoria)))
        self.combo_ubicacion.setCurrentText(item.ubicacion)
        self.combo_estado.setCurrentIndex(max(0, self.combo_estado.findData(item.estado)))
        self.campo_marca.setText(item.marca_modelo or "")
        self.campo_serie.setText(item.numero_serie or "")
        self.campo_observaciones.setPlainText(item.observaciones or "")

    def _validar(self) -> None:
        self.botones.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.campo_nombre.text().strip()) and bool(self.combo_ubicacion.currentText().strip()))

    def datos(self) -> dict:
        return {
            "codigo_qr": self.campo_codigo.text().strip().upper(),
            "nombre": self.campo_nombre.text().strip(),
            "categoria": self.combo_categoria.currentData(),
            "ubicacion": self.combo_ubicacion.currentText().strip(),
            "estado": self.combo_estado.currentData(),
            "marca_modelo": self.campo_marca.text().strip(),
            "numero_serie": self.campo_serie.text().strip(),
            "observaciones": self.campo_observaciones.toPlainText().strip(),
        }


# =============================================================================
# Página
# =============================================================================

class PaginaInventario(QWidget):
    # Código QR del activo cuya ficha (con checklists) hay que abrir.
    ficha_solicitada = Signal(str)
    escaneo_solicitado = Signal()

    def __init__(self, parent: Optional[QWidget] = None,
                 cliente_factory: Callable[[], InventarioCliente] = InventarioCliente) -> None:
        super().__init__(parent)
        self._cliente_factory = cliente_factory
        self._items: List[ItemInventario] = []
        self._tareas: Set[TareaSupabase] = set()
        self._cargando = False
        self._operador_id: Optional[int] = None
        self._rol: Optional[str] = None
        self._sin_credenciales = False
        self._aviso_pendiente: Optional[str] = None
        self._seleccionar_tras_carga: Optional[str] = None

        self._timer = QTimer(self)
        self._timer.setInterval(INTERVALO_REFRESCO_MS)
        self._timer.timeout.connect(self.refrescar)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._esperar_tareas)

        self._construir()
        self.set_operador(None, None)

    # -- Construcción ------------------------------------------------------------

    def _construir(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        cabecera = QHBoxLayout()
        textos = QVBoxLayout()
        textos.setSpacing(2)
        titulo = QLabel("Inventario del Cuartel", self)
        titulo.setObjectName("pageTitle")
        subtitulo = QLabel("Vehículos, herramientas, equipos de protección respiratoria y demás activos.", self)
        subtitulo.setObjectName("pageSubtitle")
        textos.addWidget(titulo)
        textos.addWidget(subtitulo)
        cabecera.addLayout(textos, 1)
        self.chip_rol = _crear_chip("", "neutro", self)
        cabecera.addWidget(self.chip_rol, 0, Qt.AlignmentFlag.AlignVCenter)
        self.chip_sync = _crear_chip("☁️ Sin sincronizar", "neutro", self)
        cabecera.addWidget(self.chip_sync, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(cabecera)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.tarjeta_total = TarjetaKPI("Activos", "📦", self)
        self.tarjeta_operativos = TarjetaKPI("Operativos", "✅", self)
        self.tarjeta_reparacion = TarjetaKPI("En reparación", "🔧", self)
        self.tarjeta_fuera = TarjetaKPI("Fuera de servicio", "⛔", self)
        for i, (tarjeta, estado) in enumerate(((self.tarjeta_total, TODOS),
                                               (self.tarjeta_operativos, ESTADO_OPERATIVO),
                                               (self.tarjeta_reparacion, ESTADO_EN_REPARACION),
                                               (self.tarjeta_fuera, ESTADO_FUERA_DE_SERVICIO))):
            tarjeta.clicked.connect(lambda e=estado: self._filtrar_estado(e))
            grid.addWidget(tarjeta, 0, i)
        layout.addLayout(grid)
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
        etiqueta = QLabel("ACTIVOS DEL CUARTEL", cabecera)
        etiqueta.setObjectName("tituloSeccion")
        fila.addWidget(etiqueta)
        self.label_cantidad = QLabel("", cabecera)
        self.label_cantidad.setObjectName("ayudaSeccion")
        fila.addWidget(self.label_cantidad)
        fila.addStretch(1)
        self.campo_buscar = QLineEdit(cabecera)
        self.campo_buscar.setPlaceholderText("🔎 Buscar nombre, código QR, marca o serie…")
        self.campo_buscar.setClearButtonEnabled(True)
        self.campo_buscar.setMinimumWidth(240)
        self.campo_buscar.textChanged.connect(self._pintar_tabla)
        fila.addWidget(self.campo_buscar)
        self.combo_estado = QComboBox(cabecera)
        self.combo_estado.addItem("Todos los estados", TODOS)
        for codigo, texto in ESTADOS.items():
            self.combo_estado.addItem(texto, codigo)
        self.combo_ubicacion = QComboBox(cabecera)
        self.combo_ubicacion.addItem("Todas las ubicaciones", TODOS)
        self.combo_categoria = QComboBox(cabecera)
        self.combo_categoria.addItem("Todas las categorías", TODOS)
        for codigo, texto in CATEGORIAS.items():
            self.combo_categoria.addItem(texto, codigo)
        for combo in (self.combo_estado, self.combo_ubicacion, self.combo_categoria):
            combo.currentIndexChanged.connect(self._pintar_tabla)
            fila.addWidget(combo)
        layout_marco.addWidget(cabecera)

        contenido = QWidget(marco)
        layout_contenido = QVBoxLayout(contenido)
        layout_contenido.setContentsMargins(16, 14, 16, 16)
        layout_contenido.setSpacing(12)

        acciones = QHBoxLayout()
        acciones.setSpacing(10)
        self.boton_nuevo = QPushButton("+ Nuevo ítem", contenido)
        self.boton_nuevo.setObjectName("botonPrimarioRojo")
        self.boton_nuevo.clicked.connect(self._nuevo)
        self.boton_editar = QPushButton("✏️ Editar", contenido)
        self.boton_editar.setObjectName("botonPrimarioAzul")
        self.boton_editar.clicked.connect(self._editar)
        self.boton_baja = QPushButton("⛔ Dar de baja", contenido)
        self.boton_baja.setObjectName("botonLimpiar")
        self.boton_baja.clicked.connect(self._dar_de_baja)
        self.label_solo_lectura = QLabel(
            "🔒 Modo consulta. Para dar de alta, editar o dar de baja, identificate como Administrador "
            "(chip 👤 de la barra superior).", contenido)
        self.label_solo_lectura.setObjectName("ayudaSeccion")
        self.label_solo_lectura.setWordWrap(True)
        self.check_bajas = QCheckBox("Mostrar dados de baja", contenido)
        self.check_bajas.toggled.connect(lambda _=False: self.refrescar())
        self.boton_configurar = QPushButton("🔑 Configurar conexión", contenido)
        self.boton_configurar.setObjectName("botonPrimarioAzul")
        self.boton_configurar.setVisible(False)
        self.boton_configurar.clicked.connect(self._configurar_conexion)
        self.boton_refrescar = QPushButton("⟳ Actualizar", contenido)
        self.boton_refrescar.setObjectName("botonHerramienta")
        self.boton_refrescar.clicked.connect(self.refrescar)
        for boton in (self.boton_nuevo, self.boton_editar, self.boton_baja):
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            acciones.addWidget(boton)
        self.boton_ficha = QPushButton("📋 Ficha y checklists", contenido)
        self.boton_ficha.setObjectName("botonLimpiar")
        self.boton_ficha.clicked.connect(self._pedir_ficha)
        self.boton_qr = QPushButton("🔳 Ver QR", contenido)
        self.boton_qr.setObjectName("botonLimpiar")
        self.boton_qr.clicked.connect(self._ver_qr)
        self.boton_etiquetas = QPushButton("🏷️ Etiquetas QR", contenido)
        self.boton_etiquetas.setObjectName("botonLimpiar")
        self.boton_etiquetas.setToolTip("PDF A4 con las etiquetas de los ítems que muestra la grilla (filtros incluidos)")
        self.boton_etiquetas.clicked.connect(self._exportar_etiquetas)
        self.boton_escanear = QPushButton("🔳 Escanear", contenido)
        self.boton_escanear.setObjectName("botonHerramienta")
        self.boton_escanear.setToolTip("Pistola lectora, código a mano o cámara (Ctrl+K)")
        self.boton_escanear.clicked.connect(self.escaneo_solicitado.emit)
        for boton in (self.boton_ficha, self.boton_qr, self.boton_etiquetas):
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            acciones.addWidget(boton)
        acciones.addStretch(1)
        acciones.addWidget(self.check_bajas)
        acciones.addWidget(self.boton_configurar)
        acciones.addWidget(self.boton_escanear)
        acciones.addWidget(self.boton_refrescar)
        layout_contenido.addLayout(acciones)
        layout_contenido.addWidget(self.label_solo_lectura)

        self.tabla = QTableWidget(contenido)
        self.tabla.setColumnCount(len(COLUMNAS))
        self.tabla.setHorizontalHeaderLabels(COLUMNAS)
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla.verticalHeader().setVisible(False)
        cab = self.tabla.horizontalHeader()
        cab.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(COL_NOMBRE, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(COL_ESTADO, QHeaderView.ResizeMode.Fixed)
        theme.estilizar_tabla(self.tabla)
        self.tabla.itemSelectionChanged.connect(self._actualizar_botones)
        self.tabla.setIconSize(QSize(30, 30))
        # Doble clic: el Administrador edita; el resto abre la ficha con los checklists.
        self.tabla.cellDoubleClicked.connect(lambda *_: self._editar() if self.puede_editar else self._pedir_ficha())
        layout_contenido.addWidget(self.tabla, 1)

        self.label_vacio = QLabel("", contenido)
        self.label_vacio.setObjectName("ayudaSeccion")
        self.label_vacio.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_vacio.setVisible(False)
        layout_contenido.addWidget(self.label_vacio)
        layout_marco.addWidget(contenido, 1)
        return marco

    # -- Permisos -----------------------------------------------------------------

    @property
    def puede_editar(self) -> bool:
        return self._operador_id is not None and es_admin(self._rol)

    def set_operador(self, operador_id: Optional[int], rol: Optional[str]) -> None:
        """Sesión de operador actual (MainWindow._aplicar_permisos)."""
        self._operador_id, self._rol = operador_id, rol
        admin = self.puede_editar
        for boton in (self.boton_nuevo, self.boton_editar, self.boton_baja):
            boton.setVisible(admin)
        self.label_solo_lectura.setVisible(not admin)
        if admin:
            self.chip_rol.setText("🛠️ Edición habilitada · Administrador")
            theme.set_tono(self.chip_rol, "ok")
        else:
            self.chip_rol.setText("🔒 Solo lectura")
            theme.set_tono(self.chip_rol, "neutro")
        self.chip_rol.setMinimumWidth(self.chip_rol.sizeHint().width())
        self._actualizar_botones()

    # -- Ciclo de vida ------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().showEvent(event)
        if not self._sin_credenciales:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - override de Qt
        super().hideEvent(event)
        self._timer.stop()

    def _esperar_tareas(self) -> None:
        for tarea in list(self._tareas):
            tarea.wait(5000)

    def _ejecutar(self, funcion: Callable[[], Any], al_terminar: Callable[[Any], None],
                  al_fallar: Callable[[str], None]) -> None:
        tarea = TareaSupabase(funcion, self)
        self._tareas.add(tarea)
        tarea.terminado.connect(al_terminar)
        tarea.error.connect(al_fallar)
        tarea.finished.connect(lambda t=tarea: (self._tareas.discard(t), t.deleteLater()))
        tarea.start()

    # -- Carga ----------------------------------------------------------------------

    def _faltan_credenciales(self) -> Optional[str]:
        if self._cliente_factory is not InventarioCliente:
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
            return
        self._cargando = True
        self.boton_refrescar.setEnabled(False)
        self._set_chip("☁️ Sincronizando…", "neutro")
        incluir_bajas = self.check_bajas.isChecked()
        self._ejecutar(lambda: self._cliente_factory().listar(incluir_bajas), self._al_cargar, self._al_fallar_carga)

    def _configurar_conexion(self) -> None:
        if DialogoCredencialesSupabase(self).exec() == QDialog.DialogCode.Accepted:
            self.refrescar()

    def _al_cargar(self, items: List[ItemInventario]) -> None:
        self._cargando = False
        self.boton_refrescar.setEnabled(True)
        self._items = items
        self._actualizar_kpis()
        self._actualizar_combo_ubicaciones()
        self._pintar_tabla()
        if self._seleccionar_tras_carga:
            self._seleccionar(self._seleccionar_tras_carga)
            self._seleccionar_tras_carga = None
        if self._aviso_pendiente:
            self._set_chip("✅ " + self._aviso_pendiente, "ok")
            self._aviso_pendiente = None
        else:
            self._set_chip(f"☁️ Sincronizado {ahora_cuartel():%H:%M}", "ok")

    def _al_fallar_carga(self, mensaje: str) -> None:
        self._cargando = False
        self.boton_refrescar.setEnabled(True)
        self._set_chip("☁️ Sin conexión", "error", mensaje)
        if not self._items:
            self.label_vacio.setText(f"No se pudo leer el inventario de la nube.\n{mensaje}")
            self.label_vacio.setVisible(True)

    def _set_chip(self, texto: str, tono: str, tooltip: str = "") -> None:
        self.chip_sync.setText(texto)
        self.chip_sync.setToolTip(tooltip)
        theme.set_tono(self.chip_sync, tono)
        self.chip_sync.setMinimumWidth(self.chip_sync.sizeHint().width())

    def _actualizar_kpis(self) -> None:
        activos = [i for i in self._items if i.activo]
        self.tarjeta_total.set_valor(str(len(activos)))
        for tarjeta, estado in ((self.tarjeta_operativos, ESTADO_OPERATIVO),
                                (self.tarjeta_reparacion, ESTADO_EN_REPARACION),
                                (self.tarjeta_fuera, ESTADO_FUERA_DE_SERVICIO)):
            tarjeta.set_valor(str(sum(1 for i in activos if i.estado == estado)))

    def ubicaciones(self) -> List[str]:
        return sorted({i.ubicacion for i in self._items if i.ubicacion}, key=str.lower)

    def _actualizar_combo_ubicaciones(self) -> None:
        actual = self.combo_ubicacion.currentData()
        self.combo_ubicacion.blockSignals(True)
        self.combo_ubicacion.clear()
        self.combo_ubicacion.addItem("Todas las ubicaciones", TODOS)
        for ubicacion in self.ubicaciones():
            self.combo_ubicacion.addItem(ubicacion, ubicacion)
        self.combo_ubicacion.setCurrentIndex(max(0, self.combo_ubicacion.findData(actual)))
        self.combo_ubicacion.blockSignals(False)

    # -- Grilla -----------------------------------------------------------------------

    def _filtrar_estado(self, estado: str) -> None:
        self.combo_estado.setCurrentIndex(max(0, self.combo_estado.findData(estado)))

    def items_visibles(self) -> List[ItemInventario]:
        estado = self.combo_estado.currentData()
        ubicacion = self.combo_ubicacion.currentData()
        categoria = self.combo_categoria.currentData()
        palabras = self.campo_buscar.text().lower().split()

        def coincide(i: ItemInventario) -> bool:
            if estado not in (None, TODOS) and i.estado != estado:
                return False
            if ubicacion not in (None, TODOS) and i.ubicacion != ubicacion:
                return False
            if categoria not in (None, TODOS) and i.categoria != categoria:
                return False
            texto = " ".join(filter(None, (i.nombre, i.codigo_qr, i.marca_modelo, i.numero_serie,
                                           i.ubicacion, i.categoria_legible))).lower()
            return all(p in texto for p in palabras)

        return [i for i in self._items if coincide(i)]

    def _pintar_tabla(self) -> None:
        seleccionado = self.item_seleccionado()
        visibles = self.items_visibles()
        self.tabla.setRowCount(len(visibles))
        for fila, i in enumerate(visibles):
            celda_codigo = QTableWidgetItem(qr_icono(i.codigo_qr), i.codigo_qr)
            celda_codigo.setData(Qt.ItemDataRole.UserRole, i.id)
            celda_codigo.setToolTip("Doble clic: ficha y checklists · 🔳 Ver QR: imprimir la etiqueta")
            self.tabla.setItem(fila, COL_CODIGO, celda_codigo)
            celda_nombre = QTableWidgetItem(i.nombre if i.activo else f"{i.nombre}  (BAJA)")
            celda_nombre.setToolTip("\n".join(filter(None, (
                i.observaciones, f"Baja: {i.motivo_baja}" if i.motivo_baja else None,
                f"⚙️ {i.motivo_estado}" if i.motivo_estado else None,
                f"Alta: {formatear_fecha_hora(i.fecha_alta)}" + (f" por {i.creado_por}" if i.creado_por else "")))))
            if not i.activo:
                celda_nombre.setForeground(theme_color("texto_tenue"))
            self.tabla.setItem(fila, COL_NOMBRE, celda_nombre)
            modificacion = formatear_fecha_hora(i.updated_at) + (f" · {i.modificado_por}" if i.modificado_por else "")
            for columna, texto in ((COL_CATEGORIA, i.categoria_legible), (COL_UBICACION, i.ubicacion),
                                   (COL_MARCA, i.marca_modelo or "—"), (COL_SERIE, i.numero_serie or "—"),
                                   (COL_MODIF, modificacion)):
                self.tabla.setItem(fila, columna, QTableWidgetItem(texto))
            self.tabla.setCellWidget(fila, COL_ESTADO, self._celda_estado(i))
        self.tabla.resizeColumnsToContents()
        self.tabla.horizontalHeader().setSectionResizeMode(COL_NOMBRE, QHeaderView.ResizeMode.Stretch)
        ancho = max((self.tabla.cellWidget(f, COL_ESTADO).sizeHint().width()
                     for f in range(self.tabla.rowCount())), default=0)
        self.tabla.setColumnWidth(COL_ESTADO, max(ANCHO_MIN_ESTADO, ancho + 8))

        total = len(self._items)
        self.label_cantidad.setText(f"{len(visibles)} de {total} ítem(s)" if total else "")
        if total:
            self.label_vacio.setText("Ningún ítem coincide con la búsqueda o los filtros.")
        else:
            self.label_vacio.setText("El inventario está vacío." +
                                     (" Cargá el primer activo con \"+ Nuevo ítem\"." if self.puede_editar else ""))
        self.label_vacio.setVisible(not visibles)
        if seleccionado is not None:
            self._seleccionar(seleccionado.id)
        self._actualizar_botones()

    @staticmethod
    def _celda_estado(item: ItemInventario) -> QWidget:
        contenedor = QWidget()
        fila = QHBoxLayout(contenedor)
        fila.setContentsMargins(6, 0, 6, 0)
        if item.activo:
            texto = item.estado_legible.upper()
            chip = _crear_chip(("⚙️ " + texto) if item.fuera_servicio_automatico else texto,
                               TONO_ESTADO.get(item.estado, "neutro"))
            if item.motivo_estado:
                chip.setToolTip(item.motivo_estado)
        else:
            chip = _crear_chip("DADO DE BAJA", "neutro")
        fila.addWidget(chip, 0, Qt.AlignmentFlag.AlignCenter)
        return contenedor

    def _seleccionar(self, item_id: str) -> None:
        for fila in range(self.tabla.rowCount()):
            celda = self.tabla.item(fila, COL_CODIGO)
            if celda is not None and celda.data(Qt.ItemDataRole.UserRole) == item_id:
                self.tabla.selectRow(fila)
                return

    def item_seleccionado(self) -> Optional[ItemInventario]:
        modelo = self.tabla.selectionModel()
        filas = modelo.selectedRows() if modelo else []
        if not filas:
            return None
        celda = self.tabla.item(filas[0].row(), COL_CODIGO)
        item_id = celda.data(Qt.ItemDataRole.UserRole) if celda is not None else None
        return next((i for i in self._items if i.id == item_id), None)

    def _actualizar_botones(self) -> None:
        item = self.item_seleccionado()
        editable = self.puede_editar and item is not None and item.activo
        self.boton_ficha.setEnabled(item is not None and item.activo)
        self.boton_qr.setEnabled(item is not None)
        self.boton_etiquetas.setEnabled(any(i.activo for i in self.items_visibles()))
        self.boton_editar.setEnabled(editable)
        self.boton_baja.setEnabled(editable)
        self.boton_nuevo.setEnabled(self.puede_editar and not self._sin_credenciales)

    # -- Gestión (solo ADMIN) -------------------------------------------------------

    def _exigir_admin(self) -> bool:
        """Guarda de cada acción (además de los botones ocultos)."""
        if self.puede_editar:
            return True
        QMessageBox.warning(self, "Permiso insuficiente",
                            "Modificar el inventario requiere rol Administrador. Identificate como operador "
                            "desde el chip 👤 de la barra superior.")
        return False

    def _nuevo(self) -> None:
        if not self._exigir_admin():
            return
        dialogo = DialogoItemInventario(self.ubicaciones(), None, self)
        if dialogo.exec() == QDialog.DialogCode.Accepted:
            self._guardar(dialogo.datos(), None)

    def _editar(self) -> None:
        item = self.item_seleccionado()
        if item is None or not item.activo or not self.puede_editar:
            return
        dialogo = DialogoItemInventario(self.ubicaciones(), item, self)
        if dialogo.exec() == QDialog.DialogCode.Accepted:
            self._guardar(dialogo.datos(), item.id)

    def _guardar(self, datos: dict, item_id: Optional[str]) -> None:
        operador, rol = self._operador_id, self._rol
        self.boton_nuevo.setEnabled(False)
        self._ejecutar(
            lambda: self._cliente_factory().guardar(operador, rol, datos, item_id),
            lambda item: self._al_terminar(f"«{item.nombre}» guardado (código {item.codigo_qr}).", item.id),
            lambda msg: self._al_fallar("No se pudo guardar el ítem", msg),
        )

    def _dar_de_baja(self) -> None:
        item = self.item_seleccionado()
        if item is None or not item.activo or not self._exigir_admin():
            return
        motivo, ok = QInputDialog.getText(
            self, "Dar de baja", f"Motivo de la baja de «{item.nombre}» ({item.codigo_qr}):\n"
            "(queda en el historial; el ítem deja de figurar en el inventario activo)")
        if not ok or not motivo.strip():
            return
        operador, rol = self._operador_id, self._rol
        self._ejecutar(
            lambda: self._cliente_factory().dar_de_baja(operador, rol, item.id, motivo),
            lambda _r: self._al_terminar(f"«{item.nombre}» dado de baja.", None),
            lambda msg: self._al_fallar("No se pudo dar de baja", msg),
        )

    # -- QR -------------------------------------------------------------------------

    def _pedir_ficha(self) -> None:
        item = self.item_seleccionado()
        if item is not None and item.activo:
            self.ficha_solicitada.emit(item.codigo_qr)

    def _ver_qr(self) -> None:
        item = self.item_seleccionado()
        if item is not None:
            DialogoQrActivo(item.codigo_qr, item.nombre, item.ubicacion, self).exec()

    def _exportar_etiquetas(self) -> None:
        exportar_etiquetas(self, activos_para_etiquetas(i for i in self.items_visibles() if i.activo))

    def _al_terminar(self, texto: str, seleccionar: Optional[str]) -> None:
        self._aviso_pendiente = texto[:70]
        self._seleccionar_tras_carga = seleccionar
        self._actualizar_botones()
        self.refrescar()

    def _al_fallar(self, titulo: str, mensaje: str) -> None:
        self._actualizar_botones()
        QMessageBox.warning(self, titulo, mensaje)


def theme_color(token: str):
    from PySide6.QtGui import QColor

    return QColor(theme.color(token))
