"""
Historial de Salidas: lista todos los incidentes guardados, con filtros
rápidos y acciones por fila (editar, eliminar, reimprimir planillas,
reintentar la sincronización con RUBA, ver el log de error), también desde
el menú contextual (clic derecho). Se embebe como página del dashboard en
`main_window.py` ("📋 Historial de Salidas" en la sidebar).

Regla con RUBA (`permisos_servicio`): un parte ya SINCRONIZADO está cerrado
-- no se edita ni se elimina, porque existe en el portal nacional (se puede
reimprimir). Pendientes / con error / en curso se editan y eliminan
libremente, aunque una carga fallida haya dejado un ID de RUBA a medio
crear: justamente hay que poder corregirlos y reintentar. Editar y eliminar los resuelve
`MainWindow` (conoce el formulario abierto y la cola de sincronización).
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
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

COL_NUMERO_PARTE, COL_FECHA, COL_TIPO, COL_MOVIL, COL_DIRECCION, COL_ESTADO, COL_ACCIONES = range(7)

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

    reintento_solicitado = Signal(int, str)  # incidente_id, numero_parte
    continuar_solicitado = Signal(int)       # incidente_id de un servicio en curso
    editar_solicitado = Signal(int)          # incidente_id (cualquier estado)
    eliminar_solicitado = Signal(int)        # incidente_id (MainWindow confirma y borra)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._construir_ui()
        self._cargar_tipos_filtro()
        self.refrescar()

    # -- Construcción de la UI ------------------------------------------------

    def _construir_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 16)
        layout.setSpacing(12)

        layout.addLayout(self._crear_filtros())

        self.tabla = QTableWidget(self)
        self.tabla.setColumnCount(7)
        self.tabla.setHorizontalHeaderLabels(
            ["N° Parte", "Fecha", "Tipo y Categoría", "Móvil Principal", "Dirección", "Estado RUBA", "Acciones"]
        )
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla.verticalHeader().setVisible(False)
        header = self.tabla.horizontalHeader()
        header.setSectionResizeMode(COL_TIPO, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_DIRECCION, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_ACCIONES, QHeaderView.ResizeMode.ResizeToContents)
        for columna in (COL_NUMERO_PARTE, COL_FECHA, COL_MOVIL, COL_ESTADO):
            header.setSectionResizeMode(columna, QHeaderView.ResizeMode.ResizeToContents)
        theme.estilizar_tabla(self.tabla, alto_fila=44)
        self.tabla.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabla.customContextMenuRequested.connect(self._mostrar_menu_contextual)
        self.tabla.doubleClicked.connect(self._on_doble_clic)
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
            "en_curso": inc.en_curso,
        }

    def _poblar_tabla(self, filas: List[dict]) -> None:
        self.tabla.setRowCount(0)
        self.tabla.setRowCount(len(filas))
        for fila_idx, datos in enumerate(filas):
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
            else:
                item_estado = QTableWidgetItem(self._texto_estado(datos["estado_ruba"]))
                item_estado.setForeground(self._color_estado(datos["estado_ruba"]))
            self.tabla.setItem(fila_idx, COL_ESTADO, item_estado)

            self.tabla.setCellWidget(fila_idx, COL_ACCIONES, self._crear_widget_acciones(datos))

        self.tabla.resizeRowsToContents()

    @staticmethod
    def _texto_estado(estado: str) -> str:
        return {
            EstadoRuba.SINCRONIZADO.value: "Sincronizado",
            EstadoRuba.PENDIENTE.value: "Pendiente",
            EstadoRuba.ERROR.value: "Error",
        }.get(estado, estado)

    @staticmethod
    def _color_estado(estado: str) -> QColor:
        return QColor(theme.color({
            EstadoRuba.SINCRONIZADO.value: "verde_texto",
            EstadoRuba.PENDIENTE.value: "texto_secundario",
            EstadoRuba.ERROR.value: "ambar",
        }.get(estado, "texto_secundario")))

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
        boton_reintentar.setToolTip("Reintentar la carga en RUBA")
        boton_reintentar.setEnabled(datos["estado_ruba"] != EstadoRuba.SINCRONIZADO.value and not datos["en_curso"])
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
        """La ventana principal encola la carga (una a la vez) y muestra el
        diálogo de progreso; al terminar refresca esta tabla."""
        self.label_estado.setText(f"Reintento del siniestro N° {numero_parte} encolado para RUBA…")
        self.reintento_solicitado.emit(incidente_id, numero_parte)

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
