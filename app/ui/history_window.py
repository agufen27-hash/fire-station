"""
Historial de Salidas: lista todos los incidentes guardados, con filtros
rápidos y acciones por fila (editar, eliminar, reimprimir planillas,
reintentar la carga en RUBA, ver el log de error), también desde el menú
contextual (clic derecho). Se embebe como página del dashboard en
`main_window.py` ("📋 Historial de Salidas" en la sidebar).

Carga a RUBA en lote: la primera columna es un checkbox por parte (con
"Seleccionar todos") y "🚀 Cargar Seleccionados a RUBA (X)" pide a
MainWindow que los procese en secuencia (app/services/ruba_service.py,
RubaLoteWorker). Los partes ya cargados en RUBA y los EN CURSO no se pueden
tildar: evita duplicarlos en el portal.

Regla con RUBA (`permisos_servicio`): un parte ya SINCRONIZADO está cerrado
-- no se edita ni se elimina, porque existe en el portal nacional (se puede
reimprimir). Pendientes / con error / en curso se editan y eliminan
libremente, aunque una carga fallida haya dejado un ID de RUBA a medio
crear: justamente hay que poder corregirlos y reintentar. Editar y eliminar los resuelve
`MainWindow` (conoce el formulario abierto y la cola de sincronización).
"""

from __future__ import annotations

from typing import List, Optional, Set

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.orm import joinedload

from app.db import get_session
from app.models import DotacionSalida, EstadoRuba, Incidente, TipoIncidente
from app.reports.excel_generator import generar_e_imprimir_pcd2, generar_e_imprimir_pcs, resumen_advertencias
from app.reports.pdf_generator import generar_e_imprimir_informe_pdf
from app.ui import theme


ESTADO_FILTRO_TODOS = "Todos"
ESTADO_FILTRO_SINCRONIZADOS = "Sincronizados"
ESTADO_FILTRO_PENDIENTES = "Pendientes / Error"

TIPO_FILTRO_TODOS = "Todos"

COL_SELECCION, COL_NUMERO_PARTE, COL_FECHA, COL_TIPO, COL_MOVIL, COL_DIRECCION, COL_ESTADO, COL_ACCIONES = range(8)
ENCABEZADOS = ["", "N° Parte", "Fecha", "Tipo y Categoría", "Móvil Principal", "Dirección", "Estado RUBA", "Acciones"]

TEXTO_BOTON_LOTE = "🚀 Cargar Seleccionados a RUBA ({})"

MOTIVO_PARTE_CERRADO = (
    "Parte cerrado: ya está cargado en RUBA (portal nacional), no se puede editar ni eliminar "
    "desde Fire Station. Las planillas e informes se pueden reimprimir."
)


def permisos_servicio(estado_ruba: str) -> tuple:
    """(puede_editar, puede_eliminar, motivo del bloqueo o "")."""
    if estado_ruba == EstadoRuba.SINCRONIZADO.value:
        return False, False, MOTIVO_PARTE_CERRADO
    return True, True, ""


class HistoryWindow(QWidget):
    """Página embebible (ver docstring del módulo). El nombre de la clase
    se mantiene por compatibilidad con el resto del código -- ya no es una
    ventana/diálogo aparte, sino un QWidget que main_window.py agrega
    directamente al QStackedWidget del dashboard."""

    carga_lote_solicitada = Signal(list)     # [incidente_id, ...] a cargar en RUBA, en secuencia
    continuar_solicitado = Signal(int)       # incidente_id de un servicio en curso
    editar_solicitado = Signal(int)          # incidente_id (cualquier estado)
    eliminar_solicitado = Signal(int)        # incidente_id (MainWindow confirma y borra)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._seleccionados: Set[int] = set()   # sobrevive a refrescar() y a los filtros
        self._carga_en_curso = False

        self._construir_ui()
        self._cargar_tipos_filtro()
        self.refrescar()

    # -- Construcción de la UI ------------------------------------------------

    def _construir_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)

        layout.addLayout(self._crear_filtros())
        layout.addLayout(self._crear_barra_lote())

        self.tabla = QTableWidget(self)
        self.tabla.setColumnCount(len(ENCABEZADOS))
        self.tabla.setHorizontalHeaderLabels(ENCABEZADOS)
        self.tabla.horizontalHeaderItem(COL_SELECCION).setToolTip("Tildá los partes a cargar en RUBA")
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla.verticalHeader().setVisible(False)
        header = self.tabla.horizontalHeader()
        header.setSectionResizeMode(COL_TIPO, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_DIRECCION, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_ACCIONES, QHeaderView.ResizeMode.ResizeToContents)
        for columna in (COL_SELECCION, COL_NUMERO_PARTE, COL_FECHA, COL_MOVIL, COL_ESTADO):
            header.setSectionResizeMode(columna, QHeaderView.ResizeMode.ResizeToContents)
        theme.estilizar_tabla(self.tabla, alto_fila=44)
        self.tabla.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabla.customContextMenuRequested.connect(self._mostrar_menu_contextual)
        self.tabla.doubleClicked.connect(self._on_doble_clic)
        self.tabla.itemChanged.connect(self._on_item_cambiado)
        layout.addWidget(self.tabla, 1)

        self.label_estado = QLabel("")
        self.label_estado.setProperty("muted", True)
        layout.addWidget(self.label_estado)

    def _crear_filtros(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        fila.setSpacing(10)

        fila.addWidget(QLabel("Buscar:"))
        self.entry_busqueda = QLineEdit(self)
        self.entry_busqueda.setPlaceholderText("N° parte, calle o apellido (bombero/denunciante)…")
        self.entry_busqueda.textChanged.connect(self.refrescar)
        fila.addWidget(self.entry_busqueda, 1)

        fila.addWidget(QLabel("Estado RUBA:"))
        self.combo_estado = QComboBox(self)
        self.combo_estado.addItems([ESTADO_FILTRO_TODOS, ESTADO_FILTRO_SINCRONIZADOS, ESTADO_FILTRO_PENDIENTES])
        self.combo_estado.currentIndexChanged.connect(self.refrescar)
        fila.addWidget(self.combo_estado)

        fila.addWidget(QLabel("Tipo:"))
        self.combo_tipo_filtro = QComboBox(self)
        self.combo_tipo_filtro.currentIndexChanged.connect(self.refrescar)
        fila.addWidget(self.combo_tipo_filtro)

        boton_actualizar = QPushButton("Actualizar", self)
        boton_actualizar.clicked.connect(self.refrescar)
        fila.addWidget(boton_actualizar)

        return fila

    def _crear_barra_lote(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        fila.setSpacing(10)
        self.check_todos = QCheckBox("Seleccionar todos (pendientes / con error)", self)
        self.check_todos.setToolTip("Tilda o destilda todos los partes visibles que se pueden cargar en RUBA")
        self.check_todos.clicked.connect(self._seleccionar_todos)
        fila.addWidget(self.check_todos)
        fila.addStretch(1)
        self.boton_cargar_lote = QPushButton(TEXTO_BOTON_LOTE.format(0), self)
        self.boton_cargar_lote.setObjectName("botonPrimarioRojo")
        self.boton_cargar_lote.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boton_cargar_lote.setToolTip("Carga en RUBA los partes tildados, de a uno y en orden de fecha")
        self.boton_cargar_lote.clicked.connect(self._cargar_seleccionados)
        theme.aplicar_sombra(self.boton_cargar_lote, "rojo")
        fila.addWidget(self.boton_cargar_lote)
        return fila

    def _cargar_tipos_filtro(self) -> None:
        self.combo_tipo_filtro.blockSignals(True)
        self.combo_tipo_filtro.clear()
        self.combo_tipo_filtro.addItem(TIPO_FILTRO_TODOS, None)
        with get_session() as session:
            for tipo in session.query(TipoIncidente).order_by(TipoIncidente.nombre).all():
                self.combo_tipo_filtro.addItem(tipo.nombre, tipo.id)
        self.combo_tipo_filtro.blockSignals(False)

    # -- Carga y filtrado de datos --------------------------------------------

    def refrescar(self) -> None:
        texto = self.entry_busqueda.text().strip().lower()
        filtro_estado = self.combo_estado.currentText()
        filtro_tipo_id = self.combo_tipo_filtro.currentData()

        with get_session() as session:
            incidentes = (
                session.query(Incidente)
                .options(
                    joinedload(Incidente.tipo),
                    joinedload(Incidente.categoria),
                    joinedload(Incidente.dotacion).joinedload(DotacionSalida.movil),
                    joinedload(Incidente.dotacion).joinedload(DotacionSalida.personal),
                )
                .order_by(Incidente.creado_en.desc(), Incidente.id.desc())
                .all()
            )
            filas = [
                self._extraer_fila(inc)
                for inc in incidentes
                if self._incidente_coincide(inc, texto, filtro_estado, filtro_tipo_id)
            ]

        self._poblar_tabla(filas)
        self.label_estado.setText(f"{len(filas)} siniestro(s) encontrados.")

    @staticmethod
    def _incidente_coincide(inc: Incidente, texto: str, filtro_estado: str, filtro_tipo_id) -> bool:
        if filtro_tipo_id is not None and inc.tipo_incidente_id != filtro_tipo_id:
            return False

        if filtro_estado == ESTADO_FILTRO_SINCRONIZADOS and inc.estado_ruba != EstadoRuba.SINCRONIZADO.value:
            return False
        if filtro_estado == ESTADO_FILTRO_PENDIENTES and inc.estado_ruba == EstadoRuba.SINCRONIZADO.value:
            return False

        if texto:
            campos = [inc.numero_parte or "", inc.calle_altura or "", inc.denunciante_apellido or ""]
            for d in inc.dotacion:
                if d.personal:
                    campos.append(d.personal.apellido or "")
            if not any(texto in campo.lower() for campo in campos):
                return False

        return True

    @staticmethod
    def _extraer_fila(inc: Incidente) -> dict:
        tipo_categoria = "—"
        if inc.tipo:
            tipo_categoria = inc.tipo.nombre
            if inc.categoria:
                tipo_categoria += f" — {inc.categoria.nombre}"

        movil_principal = "—"
        if inc.dotacion and inc.dotacion[0].movil:
            movil_principal = inc.dotacion[0].movil.nombre_identificador

        return {
            "id": inc.id,
            "numero_parte": inc.numero_parte,
            "fecha": inc.fecha.strftime("%d/%m/%Y") if inc.fecha else "—",
            "tipo_categoria": tipo_categoria,
            "movil_principal": movil_principal,
            "direccion": inc.calle_altura or "—",
            "estado_ruba": inc.estado_ruba,
            "ruba_error_log": inc.ruba_error_log,
            "ruba_id_remoto": inc.ruba_id_remoto,
            "ruba_sincronizado_en": inc.ruba_sincronizado_en,
            "en_curso": inc.en_curso,
        }

    @staticmethod
    def seleccionable(datos: dict) -> bool:
        """Solo lo que todavía no está en RUBA y ya se cerró."""
        return datos["estado_ruba"] != EstadoRuba.SINCRONIZADO.value and not datos["en_curso"]

    def _poblar_tabla(self, filas: List[dict]) -> None:
        self.tabla.blockSignals(True)
        self.tabla.setRowCount(0)
        self.tabla.setRowCount(len(filas))
        # Lo tildado que ya no se puede cargar (se sincronizó, se borró) se olvida.
        existentes = {d["id"]: d for d in filas}
        self._seleccionados = {i for i in self._seleccionados
                               if i not in existentes or self.seleccionable(existentes[i])}
        for fila_idx, datos in enumerate(filas):
            self.tabla.setItem(fila_idx, COL_SELECCION, self._crear_item_seleccion(datos))
            item_numero = QTableWidgetItem(datos["numero_parte"])
            item_numero.setData(Qt.ItemDataRole.UserRole, datos)  # para el menú contextual
            self.tabla.setItem(fila_idx, COL_NUMERO_PARTE, item_numero)
            self.tabla.setItem(fila_idx, COL_FECHA, QTableWidgetItem(datos["fecha"]))
            self.tabla.setItem(fila_idx, COL_TIPO, QTableWidgetItem(datos["tipo_categoria"]))
            self.tabla.setItem(fila_idx, COL_MOVIL, QTableWidgetItem(datos["movil_principal"]))
            self.tabla.setItem(fila_idx, COL_DIRECCION, QTableWidgetItem(datos["direccion"]))

            if datos["en_curso"]:
                item_estado = QTableWidgetItem("⏱️ En curso")
                item_estado.setForeground(QColor(theme.color("rojo_hover")))
                item_estado.setToolTip("Cerralo (con los horarios de regreso) para poder cargarlo en RUBA")
            else:
                item_estado = QTableWidgetItem(self._texto_estado(datos["estado_ruba"]))
                item_estado.setForeground(self._color_estado(datos["estado_ruba"]))
                item_estado.setToolTip(self._tooltip_estado(datos))
            self.tabla.setItem(fila_idx, COL_ESTADO, item_estado)

            self.tabla.setCellWidget(fila_idx, COL_ACCIONES, self._crear_widget_acciones(datos))

        self.tabla.resizeRowsToContents()
        self.tabla.blockSignals(False)
        self._actualizar_boton_lote()

    def _crear_item_seleccion(self, datos: dict) -> QTableWidgetItem:
        item = QTableWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, datos["id"])
        if self.seleccionable(datos):
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            marcado = datos["id"] in self._seleccionados
            item.setCheckState(Qt.CheckState.Checked if marcado else Qt.CheckState.Unchecked)
            item.setToolTip("Tildar para cargarlo en RUBA")
        else:
            # Bloqueado: sin ItemIsEnabled el checkbox se ve gris y no se puede tildar.
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip("🔒 Ya está cargado en RUBA" if not datos["en_curso"]
                            else "Servicio en curso: cerralo antes de cargarlo en RUBA")
        return item

    # -- Selección y carga en lote ------------------------------------------------

    def _on_item_cambiado(self, item: QTableWidgetItem) -> None:
        if item.column() != COL_SELECCION:
            return
        incidente_id = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self._seleccionados.add(incidente_id)
        else:
            self._seleccionados.discard(incidente_id)
        self._actualizar_boton_lote()

    def _items_seleccionables(self) -> List[QTableWidgetItem]:
        items = (self.tabla.item(fila, COL_SELECCION) for fila in range(self.tabla.rowCount()))
        return [i for i in items if i is not None and i.flags() & Qt.ItemFlag.ItemIsEnabled]

    def _seleccionar_todos(self, marcado: bool) -> None:
        estado = Qt.CheckState.Checked if marcado else Qt.CheckState.Unchecked
        self.tabla.blockSignals(True)
        for item in self._items_seleccionables():
            item.setCheckState(estado)
            (self._seleccionados.add if marcado else self._seleccionados.discard)(item.data(Qt.ItemDataRole.UserRole))
        self.tabla.blockSignals(False)
        self._actualizar_boton_lote()

    def _actualizar_boton_lote(self) -> None:
        self.boton_cargar_lote.setText(TEXTO_BOTON_LOTE.format(len(self._seleccionados)))
        self.boton_cargar_lote.setEnabled(not self._carga_en_curso)
        visibles = self._items_seleccionables()
        self.check_todos.blockSignals(True)
        self.check_todos.setChecked(bool(visibles) and all(
            i.checkState() == Qt.CheckState.Checked for i in visibles))
        self.check_todos.setEnabled(bool(visibles))
        self.check_todos.blockSignals(False)

    def seleccionados(self) -> List[int]:
        return sorted(self._seleccionados)

    def set_carga_en_curso(self, en_curso: bool) -> None:
        """MainWindow: mientras corre un lote no se puede lanzar otro."""
        self._carga_en_curso = en_curso
        self.boton_cargar_lote.setToolTip(
            "Hay una carga en RUBA en curso: esperá a que termine." if en_curso
            else "Carga en RUBA los partes tildados, de a uno y en orden de fecha")
        self._actualizar_boton_lote()

    def _cargar_seleccionados(self) -> None:
        if not self._seleccionados:
            QMessageBox.warning(
                self, "No hay partes seleccionados",
                "Tildá en la primera columna los partes que querés cargar en RUBA "
                "(o usá \"Seleccionar todos\").",
            )
            return
        # La selección NO se limpia: al refrescar, los que quedaron "Cargado en
        # RUBA" se destildan solos y los que fallaron siguen tildados para reintentar.
        self.carga_lote_solicitada.emit(self.seleccionados())

    @staticmethod
    def _texto_estado(estado: str) -> str:
        return {
            EstadoRuba.SINCRONIZADO.value: "✅ Cargado en RUBA",
            EstadoRuba.PENDIENTE.value: "🕒 Pendiente",
            EstadoRuba.ERROR.value: "❌ Error",
        }.get(estado, estado)

    @staticmethod
    def _color_estado(estado: str) -> QColor:
        return QColor(theme.color({
            EstadoRuba.SINCRONIZADO.value: "verde_texto",
            EstadoRuba.PENDIENTE.value: "ambar",
            EstadoRuba.ERROR.value: "rojo",
        }.get(estado, "texto_secundario")))

    @staticmethod
    def _tooltip_estado(datos: dict) -> str:
        estado = datos["estado_ruba"]
        if estado == EstadoRuba.SINCRONIZADO.value:
            cuando = datos.get("ruba_sincronizado_en")
            return ("Cargado en RUBA" + (f" el {cuando:%d/%m/%Y %H:%M}" if cuando else "")
                    + (f" (ID {datos['ruba_id_remoto']})" if datos.get("ruba_id_remoto") else "")
                    + ". Parte cerrado: no se edita ni se elimina.")
        if estado == EstadoRuba.ERROR.value:
            log_error = (datos.get("ruba_error_log") or "").strip()
            return "Falló la carga en RUBA:\n" + (log_error.splitlines()[0] if log_error else "(sin detalle)") \
                + "\n\nBotón ⚠ Log: detalle completo."
        return "Pendiente de carga en RUBA: tildalo y usá 🚀 Cargar Seleccionados."

    # -- Acciones por fila ------------------------------------------------------

    def _crear_widget_acciones(self, datos: dict) -> QWidget:
        contenedor = QWidget()
        fila = QHBoxLayout(contenedor)
        fila.setContentsMargins(4, 2, 4, 2)
        fila.setSpacing(4)

        incidente_id = datos["id"]
        numero_parte = datos["numero_parte"]

        puede_editar, puede_eliminar, motivo = permisos_servicio(datos["estado_ruba"])
        if datos["en_curso"]:
            boton_continuar = QPushButton("✏️ Continuar", contenedor)
            boton_continuar.setObjectName("botonAhora")
            boton_continuar.setToolTip("Abrir el servicio en curso para registrar el regreso y cerrarlo")
            boton_continuar.clicked.connect(lambda: self.continuar_solicitado.emit(incidente_id))
            fila.addWidget(boton_continuar)
        else:
            boton_editar = QPushButton("✏️ Editar" if puede_editar else "🔒 Cerrado", contenedor)
            boton_editar.setToolTip(motivo or "Cargar el servicio en el formulario para corregirlo")
            boton_editar.setEnabled(puede_editar)
            boton_editar.clicked.connect(lambda: self.editar_solicitado.emit(incidente_id))
            fila.addWidget(boton_editar)

        boton_eliminar = QPushButton("🗑️", contenedor)
        boton_eliminar.setObjectName("botonQuitarFila")
        boton_eliminar.setToolTip(motivo or "Eliminar el servicio de la base local")
        boton_eliminar.setEnabled(puede_eliminar)
        boton_eliminar.clicked.connect(lambda: self.eliminar_solicitado.emit(incidente_id))

        boton_pcs = QPushButton("🖨 PCS", contenedor)
        boton_pcs.setToolTip("Reimprimir la planilla PCS")
        boton_pcs.clicked.connect(lambda: self._reimprimir(incidente_id, numero_parte, generar_e_imprimir_pcs, "PCS"))
        fila.addWidget(boton_pcs)

        boton_pcd2 = QPushButton("🖨 PCD2", contenedor)
        boton_pcd2.setToolTip("Reimprimir la planilla PCD2")
        boton_pcd2.clicked.connect(
            lambda: self._reimprimir(incidente_id, numero_parte, generar_e_imprimir_pcd2, "PCD2")
        )
        fila.addWidget(boton_pcd2)

        boton_pdf = QPushButton("📄 Informe", contenedor)
        boton_pdf.setToolTip("Generar el informe PDF del siniestro")
        boton_pdf.clicked.connect(lambda: self._generar_informe_pdf(incidente_id, numero_parte))
        fila.addWidget(boton_pdf)

        boton_reintentar = QPushButton("↻ RUBA", contenedor)
        boton_reintentar.setToolTip("Cargar solo este parte en RUBA")
        boton_reintentar.setEnabled(self.seleccionable(datos) and not self._carga_en_curso)
        boton_reintentar.clicked.connect(lambda: self._reintentar_ruba(incidente_id, numero_parte))
        fila.addWidget(boton_reintentar)

        boton_log = QPushButton("⚠ Log", contenedor)
        boton_log.setToolTip("Ver el log del último error de RUBA")
        boton_log.setEnabled(bool(datos["ruba_error_log"]))
        boton_log.clicked.connect(lambda: self._ver_log_error(numero_parte, datos["ruba_error_log"]))
        fila.addWidget(boton_log)

        fila.addWidget(boton_eliminar)  # al final: lejos de los botones de uso diario
        return contenedor

    def _datos_de_fila(self, fila: int) -> Optional[dict]:
        item = self.tabla.item(fila, COL_NUMERO_PARTE)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _mostrar_menu_contextual(self, posicion: QPoint) -> None:
        indice = self.tabla.indexAt(posicion)
        datos = self._datos_de_fila(indice.row()) if indice.isValid() else None
        if not datos:
            return
        self.tabla.selectRow(indice.row())
        puede_editar, puede_eliminar, motivo = permisos_servicio(datos["estado_ruba"])

        menu = QMenu(self.tabla)
        if datos["en_curso"]:
            accion_editar = menu.addAction("✏️ Continuar servicio en curso")
            accion_editar.triggered.connect(lambda: self.continuar_solicitado.emit(datos["id"]))
        else:
            accion_editar = menu.addAction("✏️ Editar Servicio" if puede_editar
                                           else "🔒 Editar Servicio (parte cerrado: está en RUBA)")
            accion_editar.triggered.connect(lambda: self.editar_solicitado.emit(datos["id"]))
        accion_editar.setEnabled(puede_editar)
        if motivo:
            accion_editar.setToolTip(motivo)

        accion_eliminar = menu.addAction("🗑️ Eliminar Servicio")
        accion_eliminar.setEnabled(puede_eliminar)
        if motivo:
            accion_eliminar.setText("🗑️ Eliminar Servicio (parte cerrado: está en RUBA)")
            accion_eliminar.setToolTip(motivo)
        accion_eliminar.triggered.connect(lambda: self.eliminar_solicitado.emit(datos["id"]))
        menu.setToolTipsVisible(True)
        menu.exec(self.tabla.viewport().mapToGlobal(posicion))

    def _on_doble_clic(self, indice) -> None:
        datos = self._datos_de_fila(indice.row())
        if not datos:
            return
        if datos["en_curso"]:
            self.continuar_solicitado.emit(datos["id"])
            return
        puede_editar, _, motivo = permisos_servicio(datos["estado_ruba"])
        if not puede_editar:
            QMessageBox.information(self, f"Parte N° {datos['numero_parte']} cerrado", motivo)
            return
        self.editar_solicitado.emit(datos["id"])

    def _reimprimir(self, incidente_id: int, numero_parte: str, generar_func, nombre_planilla: str) -> None:
        try:
            ruta = generar_func(incidente_id)
        except FileNotFoundError as e:
            QMessageBox.warning(self, f"Falta la plantilla {nombre_planilla}", str(e))
            return
        except Exception as e:  # noqa: BLE001 - error inesperado generando/abriendo el xlsx
            QMessageBox.critical(
                self, f"Error al generar {nombre_planilla}",
                f"No se pudo generar {nombre_planilla} para el siniestro N° {numero_parte}:\n{e}",
            )
            return

        self.label_estado.setText(f"Se regeneró {nombre_planilla} del siniestro N° {numero_parte}: {ruta}")
        aviso = resumen_advertencias(nombre_planilla, ruta)
        if aviso:
            QMessageBox.warning(self, f"{nombre_planilla}: plantilla distinta al mapeo", aviso)

    def _generar_informe_pdf(self, incidente_id: int, numero_parte: str) -> None:
        try:
            ruta = generar_e_imprimir_informe_pdf(incidente_id)
        except Exception as e:  # noqa: BLE001 - error inesperado generando/abriendo el PDF
            QMessageBox.critical(
                self, "Error al generar el Informe PDF",
                f"No se pudo generar el Informe Técnico del siniestro N° {numero_parte}:\n{e}",
            )
            return
        self.label_estado.setText(f"Se generó el Informe Técnico del siniestro N° {numero_parte}: {ruta}")

    def _reintentar_ruba(self, incidente_id: int, numero_parte: str) -> None:
        """Un lote de un solo parte: mismo motor y mismo diálogo de progreso."""
        self.label_estado.setText(f"Carga del siniestro N° {numero_parte} en RUBA…")
        self.carga_lote_solicitada.emit([incidente_id])

    def _ver_log_error(self, numero_parte: str, log_error: Optional[str]) -> None:
        dialogo = QDialog(self)
        dialogo.setWindowTitle(f"Log de error — Siniestro N° {numero_parte}")
        dialogo.resize(560, 360)
        layout = QVBoxLayout(dialogo)

        texto = QTextEdit(dialogo)
        texto.setReadOnly(True)
        texto.setPlainText(log_error or "Sin errores registrados.")
        layout.addWidget(texto)

        boton_cerrar = QPushButton("Cerrar", dialogo)
        boton_cerrar.clicked.connect(dialogo.accept)
        layout.addWidget(boton_cerrar)

        dialogo.exec()
