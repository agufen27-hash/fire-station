"""
Ventana principal de Fire Station: dashboard con sidebar lateral (7 módulos),
barra institucional superior con el nombre de la sección activa, y un
QStackedWidget de páginas -- Inicio, Nueva Salida (formulario operativo de
guardia, con dotaciones dinámicas y firma electrónica), Historial de
Salidas, Cartografía Táctica, Personal y Unidades (CRUD), Documentación
(legajos con acceso por PIN) y Configuración (Fase 6 y Fase 7).
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QThread, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QIntValidator, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.catalogos import obtener_padron
from app.core.horarios import fecha_fin_ajustada
from app import __version__
from app.paths import ICONO_APP, LOGO_INSTITUCIONAL, ruta_recurso_existente
from app.db import DATA_DIR, get_session, moviles_para_despacho, reiniciar_partes, siguiente_numero_parte
from app.models import (
    MEDIOS_CONTACTO,
    MEDIOS_TELEFONICOS,
    DocumentoPersonal,
    BienAfectado,
    BomberoDamnificado,
    CategoriaIncidente,
    DamnificadoCivil,
    DotacionSalida,
    EstadoOperativo,
    EstadoRuba,
    FuncionBase,
    Incidente,
    Movil,
    Personal,
    PersonalBase,
    RolDotacion,
    SalidaUnidad,
    TipoIncidente,
)
from app.reports.excel_generator import (
    OUTPUT_DIR,
    TEMPLATES_DIR,
    generar_e_imprimir_pcd2,
    generar_e_imprimir_pcs,
    resumen_advertencias,
)
from app.services.conectividad import ConectividadWorker, lanzar_chequeo_conectividad
from app.services.ruba_payload import (
    DatosServicio,
    UnidadServicio,
    aplanar_unidades,
    construir_payload,
    numero_parte_ruba,
    persona_desde_personal,
)
from app.services.ruba_helpers import cargar_credenciales
from app.services.ruba_service import RubaLoteWorker, lanzar_lote
from app.ui.history_window import MOTIVO_PARTE_CERRADO, HistoryWindow
from app.ui.autor_dialog import pedir_autor
from app.services import cartografia
from app.ui.widgets.map_widget import MapWidget, OperationsMapWindow
from app.ui.damnificados_widgets import PanelDamnificados
from app.services.personal_info import mandos_del_padron, padron_activo
from app.ui.dotaciones_widgets import PanelDotaciones, PanelPersonalBase, personas_repetidas
from app.ui.participacion_widgets import HorarioServicio, SelectorBombero
from app.ui.bomberos_view import importar_bomberos_desde_excel
from app.ui.personnel_window import LegajoPrivadoWidget
from app.ui import theme
from app.ui.map_dialog import DialogoMarcarMapa, parsear_par
from app.ui.ruba_progreso_dialog import DialogoLoteRuba
from app.ui.servicio_en_curso import TarjetaServicioEnCurso, resumen_de
from app.ui.siniestro_widgets import FORM_ACCIDENTE, PanelDatosEspecificos
from app.ui.unidades_view import PanelUnidades
from app.services.ruba_importer import (
    ESTADO_MOVIL_EN_SERVICIO, ESTADO_MOVIL_FUERA_DE_SERVICIO, clave_movil, normalizar_dominio,
)
from app.ui.widgets import DateField, TarjetaKPI, TimeField
from app.ui.widgets.phonebook_widget import PhonebookWidget
from app.ui.widgets.weather_widget import WeatherWidget

ANCHO_VENTANA = 1280
ALTO_VENTANA = 860
ANCHO_SIDEBAR = 250

MEDIOS_ANTERIORES = {"Personal": "Presencial", "Radial": "Frecuencia Radial"}  # valores de antes de la Fase 15

# Rol extra de los ítems de combo_categoria: el código RUBA del subtipo
# (el UserRole normal guarda el id local de CategoriaIncidente).
ROL_CODIGO_RUBA = Qt.ItemDataRole.UserRole + 1


# Índices de página del QStackedWidget, en el mismo orden que la sidebar.
IDX_DASHBOARD = 0
IDX_FORMULARIO = 1
IDX_HISTORIAL = 2
IDX_MAPA = 3
IDX_DOTACIONES = 4
IDX_DOCUMENTACION = 5
IDX_CONFIGURACION = 6

ITEMS_NAV = [
    (IDX_DASHBOARD, "📊  Inicio"),
    (IDX_FORMULARIO, "📝  Nueva Planilla"),
    (IDX_HISTORIAL, "📈  Estadísticas"),
    (IDX_MAPA, "🗺️  Cartografía Táctica"),
    (IDX_DOTACIONES, "🚒  Personal y Unidades"),
    (IDX_DOCUMENTACION, "📁  Documentación"),
    (IDX_CONFIGURACION, "⚙️  Configuración"),
]

# Sugerencias del diálogo de unidad (combos editables: se puede escribir otro).
TIPOS_UNIDAD_SUGERIDOS = ["Autobomba", "Cisterna", "Forestal", "Rescate", "Transporte de personal",
                          "Ambulancia", "Camioneta", "Utilitario", "Unidad de comando", "Hidroelevador"]
MARCAS_UNIDAD_SUGERIDAS = ["Man", "Ford", "Daf", "Mercedes-Benz", "Iveco", "Scania", "Volkswagen",
                           "Chevrolet", "Toyota", "Hyundai", "Renault", "Fiat"]
ANIO_UNIDAD_MINIMO = 1950

COLUMNAS_PERSONAL = ["Nombre", "Legajo", "DNI", "Jerarquía", "Estado", "Acciones"]
COL_PERSONAL_ACCIONES = len(COLUMNAS_PERSONAL) - 1


def filtrar_tabla(tabla: QTableWidget, texto: str, col_excluida: int = -1) -> None:
    """Oculta las filas que no contienen TODAS las palabras buscadas en alguna
    de sus celdas de texto (sin distinguir mayúsculas; DNI con o sin puntos)."""
    palabras = [p for p in texto.lower().replace(".", "").split() if p]
    for fila in range(tabla.rowCount()):
        contenido = " ".join(
            (tabla.item(fila, c).text() if tabla.item(fila, c) else "")
            for c in range(tabla.columnCount()) if c != col_excluida
        ).lower().replace(".", "")
        tabla.setRowHidden(fila, not all(p in contenido for p in palabras))


NOMBRES_SECCION = {
    IDX_DASHBOARD: "INICIO",
    IDX_FORMULARIO: "NUEVA PLANILLA",
    IDX_HISTORIAL: "ESTADÍSTICAS",
    IDX_MAPA: "CARTOGRAFÍA TÁCTICA",
    IDX_DOTACIONES: "PERSONAL Y UNIDADES",
    IDX_DOCUMENTACION: "DOCUMENTACIÓN",
    IDX_CONFIGURACION: "CONFIGURACIÓN",
}




def _sincronizar_estado_movil(movil: Movil) -> None:
    """Tras dar de baja / reactivar a mano una unidad importada de RUBA, que
    el estado mostrado no contradiga a `activo` (las cargadas a mano, sin
    estado de RUBA, lo siguen sin tener)."""
    if movil.estado is None:
        return
    if movil.activo:
        movil.estado = ESTADO_MOVIL_EN_SERVICIO
    elif movil.estado == ESTADO_MOVIL_EN_SERVICIO:
        movil.estado = ESTADO_MOVIL_FUERA_DE_SERVICIO


class MainWindow(QMainWindow):
    # Lo asigna run.py (app/ui/actualizaciones.py); None en tests.
    controlador_actualizaciones = None

    def __init__(self) -> None:
        super().__init__()
        icono = ruta_recurso_existente(ICONO_APP)
        if icono is not None:
            self.setWindowIcon(QIcon(str(icono)))
        self.setWindowTitle(f'Fire Station {__version__} — Bomberos Voluntarios "Osvaldo R. Rossi" (C 59 / R 3)')
        self.resize(ANCHO_VENTANA, ALTO_VENTANA)
        self.setMinimumSize(1000, 640)

        # Carga en lote a RUBA en curso (hilo, worker, diálogo): hay que
        # mantener vivas las referencias mientras el hilo corre, o Qt puede
        # destruir el QThread en medio de la carga y crashear la app. Hay un
        # solo lote a la vez (una sola sesión de navegador).
        self._lote_ruba: Optional[Tuple[QThread, RubaLoteWorker, DialogoLoteRuba]] = None
        self._hilo_conectividad: Optional[Tuple[QThread, ConectividadWorker]] = None
        self._cantidad_pendientes_inicio = 0
        # Servicio EN_CURSO reabierto en el formulario (None = alta nueva).
        self._incidente_en_edicion: Optional[int] = None
        # True si lo que está en el formulario es un servicio CERRADO abierto
        # desde el Historial para corregirlo (no uno en curso).
        self._edicion_de_cerrado = False
        self._edicion_de_sincronizado = False
        self._tarjetas_en_curso: List[TarjetaServicioEnCurso] = []
        # Campos del horario general que el operador ajustó a mano (esos ya
        # no se recalculan solos desde el llamado / las llegadas).
        self._horario_manual: set = set()
        self._cargar_catalogos_oficiales()

        self._construir_ui()
        self._cargar_catalogos()
        self._limpiar_formulario()
        self.statusBar().showMessage(" ".join(self._avisos_catalogos) or "Listo.")
        self._actualizar_chips()

        # Aviso discreto (oculto salvo que haga falta) de "hay salidas
        # pendientes de sincronizar con RUBA y hay internet" -- ver
        # _verificar_pendientes_ruba_al_inicio().
        self._boton_sync_pendientes = QPushButton(self)
        self._boton_sync_pendientes.setVisible(False)
        self._boton_sync_pendientes.clicked.connect(self._sincronizar_todos_pendientes)
        self.statusBar().addPermanentWidget(self._boton_sync_pendientes)

        self._actualizar_dashboard()

        QTimer.singleShot(800, self._verificar_pendientes_ruba_al_inicio)

        # Cronómetro de los servicios en curso (una sola señal por segundo).
        self._reloj = QTimer(self)
        self._reloj.setInterval(1000)
        self._reloj.timeout.connect(self._tick_servicios_en_curso)
        self._reloj.start()

    # -- Construcción del shell: sidebar + barra institucional + stack ---------

    def _construir_ui(self) -> None:
        central = QWidget(self)
        layout_central = QHBoxLayout(central)
        layout_central.setContentsMargins(0, 0, 0, 0)
        layout_central.setSpacing(0)
        self.setCentralWidget(central)

        layout_central.addWidget(self._crear_sidebar())

        columna_derecha = QWidget(central)
        layout_derecha = QVBoxLayout(columna_derecha)
        layout_derecha.setContentsMargins(0, 0, 0, 0)
        layout_derecha.setSpacing(0)
        layout_derecha.addWidget(self._crear_barra_institucional())

        self._stack = QStackedWidget(columna_derecha)
        layout_derecha.addWidget(self._stack, 1)

        layout_central.addWidget(columna_derecha, 1)

        self._pagina_dashboard = self._crear_pagina_dashboard()
        self._pagina_formulario = self._crear_pagina_formulario()
        self._pagina_historial = HistoryWindow(self)
        self._pagina_historial.carga_lote_solicitada.connect(self._cargar_lote_ruba)
        self._pagina_historial.continuar_solicitado.connect(self.continuar_servicio_en_curso)
        self._pagina_historial.editar_solicitado.connect(self.editar_servicio)
        self._pagina_historial.eliminar_solicitado.connect(self.eliminar_servicio)
        self._pagina_historial.estados_cambiados.connect(self._actualizar_dashboard)
        self._pagina_mapa = OperationsMapWindow(self)
        self._pagina_dotaciones = self._crear_pagina_dotaciones()
        self._pagina_documentacion = self._crear_pagina_documentacion()
        self._pagina_configuracion = self._crear_pagina_configuracion()

        for pagina in (
            self._pagina_dashboard,
            self._pagina_formulario,
            self._pagina_historial,
            self._pagina_mapa,
            self._pagina_dotaciones,
            self._pagina_documentacion,
            self._pagina_configuracion,
        ):
            self._stack.addWidget(pagina)

        self._stack.currentChanged.connect(self._on_pagina_cambiada)

    def _crear_barra_institucional(self) -> QFrame:
        barra = QFrame(self)
        barra.setObjectName("barraInstitucional")
        barra.setFixedHeight(52)
        layout = QHBoxLayout(barra)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(10)

        escudo = QLabel("🛡️", barra)
        escudo.setObjectName("barraEscudo")
        layout.addWidget(escudo)

        self._label_seccion_actual = QLabel(NOMBRES_SECCION[IDX_DASHBOARD], barra)
        self._label_seccion_actual.setObjectName("barraTitulo")
        layout.addWidget(self._label_seccion_actual)
        layout.addStretch(1)

        # Chips de estado: borrador en curso, padrón y conexión con RUBA.
        self._chip_en_curso = self._crear_chip("", "error", barra)
        self._chip_en_curso.setVisible(False)
        layout.addWidget(self._chip_en_curso, 0, Qt.AlignmentFlag.AlignVCenter)
        self._chip_borrador = self._crear_chip("", "info", barra)
        self._chip_padron = self._crear_chip("", "neutro", barra)
        self._chip_ruba = self._crear_chip("RUBA · verificando…", "neutro", barra)
        for chip in (self._chip_borrador, self._chip_padron, self._chip_ruba):
            layout.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self._chip_borrador.setVisible(False)
        return barra

    @staticmethod
    def _crear_chip(texto: str, tono: str, parent: QWidget) -> QLabel:
        chip = QLabel(texto, parent)
        chip.setObjectName("chip")
        chip.setFixedHeight(24)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.set_tono(chip, tono)
        return chip

    def _actualizar_chips(self) -> None:
        en_formulario = self._stack.currentIndex() == IDX_FORMULARIO
        self._chip_borrador.setVisible(en_formulario)
        if en_formulario:
            if self._incidente_en_edicion is not None and self._edicion_de_cerrado:
                self._chip_borrador.setText(f"✏️ Modo edición · N° {self.label_numero_parte.text()}")
                theme.set_tono(self._chip_borrador, "alerta")
            elif self._incidente_en_edicion is not None:
                self._chip_borrador.setText(f"⏱️ En curso · N° {self.label_numero_parte.text()} (editando)")
                theme.set_tono(self._chip_borrador, "alerta")
            else:
                self._chip_borrador.setText(f"Nuevo · N° {self.label_numero_parte.text()}")
                theme.set_tono(self._chip_borrador, "info")
        if self._padron:
            self._chip_padron.setText(f"Padrón · {len(self._padron)} activos")
            theme.set_tono(self._chip_padron, "ok")
        else:
            self._chip_padron.setText("Padrón · no disponible")
            theme.set_tono(self._chip_padron, "alerta")
        self._chip_padron.setToolTip("\n".join(self._avisos_catalogos) or "data/Reporte de bomberos.xlsx")
        for chip in (self._chip_borrador, self._chip_padron):
            self._ajustar_chip(chip)

    def _set_chip_ruba(self, texto: str, tono: str, tooltip: str = "") -> None:
        self._chip_ruba.setText(f"RUBA · {texto}")
        self._chip_ruba.setToolTip(tooltip)
        theme.set_tono(self._chip_ruba, tono)
        self._ajustar_chip(self._chip_ruba)

    @staticmethod
    def _ajustar_chip(chip: QLabel) -> None:
        """Que el chip nunca recorte su texto cuando cambia."""
        chip.setMinimumWidth(chip.sizeHint().width())

    ALTO_LOGO_CABECERA = 56

    def _crear_logo_cabecera(self, parent: QWidget) -> QLabel:
        """Logo institucional (resources/logo.png) escalado a una altura fija;
        si no está o no se puede leer, el emoji de siempre."""
        etiqueta = QLabel(parent)
        etiqueta.setObjectName("sidebarEscudo")
        ruta = ruta_recurso_existente(LOGO_INSTITUCIONAL)
        pixmap = QPixmap(str(ruta)) if ruta else QPixmap()
        if pixmap.isNull():
            etiqueta.setText("🚒")
            return etiqueta
        # Escalado a la densidad real de la pantalla: nítido en monitores con
        # zoom de Windows al 125/150 %.
        dpr = self.devicePixelRatioF() or 1.0
        escalado = pixmap.scaledToHeight(round(self.ALTO_LOGO_CABECERA * dpr),
                                         Qt.TransformationMode.SmoothTransformation)
        escalado.setDevicePixelRatio(dpr)
        etiqueta.setPixmap(escalado)
        etiqueta.setFixedHeight(self.ALTO_LOGO_CABECERA)
        etiqueta.setToolTip("Sociedad de Bomberos Voluntarios de Adelia María")
        return etiqueta

    def _crear_sidebar(self) -> QWidget:
        sidebar = QWidget(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(ANCHO_SIDEBAR)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        cabecera = QWidget(sidebar)
        cabecera.setObjectName("sidebarCabecera")
        layout_cabecera = QVBoxLayout(cabecera)
        layout_cabecera.setContentsMargins(20, 24, 20, 18)
        layout_cabecera.setSpacing(4)

        layout_cabecera.addWidget(self._crear_logo_cabecera(cabecera))

        titulo = QLabel("Cuerpo Activo", cabecera)
        titulo.setObjectName("sidebarTitulo")
        layout_cabecera.addWidget(titulo)

        subtitulo = QLabel("Sociedad BV Adelia María", cabecera)
        subtitulo.setObjectName("sidebarSubtitulo")
        subtitulo.setWordWrap(True)
        layout_cabecera.addWidget(subtitulo)

        layout.addWidget(cabecera)

        separador = QFrame(sidebar)
        separador.setObjectName("sidebarSeparador")
        separador.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separador)

        layout_nav = QVBoxLayout()
        layout_nav.setContentsMargins(12, 16, 12, 12)
        layout_nav.setSpacing(4)
        etiqueta_nav = QLabel("NAVEGACIÓN", sidebar)
        etiqueta_nav.setObjectName("sidebarSeccion")
        layout_nav.addWidget(etiqueta_nav)
        layout_nav.addSpacing(4)

        self._grupo_nav = QButtonGroup(sidebar)
        self._grupo_nav.setExclusive(True)
        self._botones_nav: List[QPushButton] = []

        for indice, etiqueta in ITEMS_NAV:
            boton = QPushButton(etiqueta, sidebar)
            boton.setObjectName("navButton")
            boton.setCheckable(True)
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            boton.clicked.connect(lambda _=False, i=indice: self._ir_a_pagina(i))
            self._grupo_nav.addButton(boton)
            self._botones_nav.append(boton)
            layout_nav.addWidget(boton)

        self._botones_nav[IDX_DASHBOARD].setChecked(True)
        layout_nav.addStretch(1)
        layout.addLayout(layout_nav)

        return sidebar

    def _ir_a_pagina(self, indice: int) -> None:
        self._botones_nav[indice].setChecked(True)
        self._stack.setCurrentIndex(indice)

    def _on_pagina_cambiada(self, indice: int) -> None:
        self._label_seccion_actual.setText(NOMBRES_SECCION.get(indice, ""))
        self._actualizar_chips()
        if indice == IDX_DASHBOARD:
            self._actualizar_dashboard()
        elif indice == IDX_HISTORIAL:
            self._pagina_historial.refrescar()
        elif indice == IDX_MAPA:
            self._pagina_mapa.refrescar()
        elif indice == IDX_DOTACIONES:
            self._cargar_pagina_dotaciones()
        elif indice == IDX_DOCUMENTACION:
            self._cargar_pagina_documentacion()
        elif indice == IDX_CONFIGURACION:
            self._cargar_pagina_configuracion()

    def _crear_contenedor_seccion(self, texto_banner: str) -> Tuple[QFrame, QVBoxLayout]:
        """Una tarjeta del formulario: cabecera con el número de paso en un
        badge y el título ("1. PEDIDO DE SOCORRO" -> [1] PEDIDO DE SOCORRO),
        y debajo el contenido con márgenes y espaciado uniformes."""
        marco = QFrame()
        marco.setObjectName("seccionFormulario")
        layout_marco = QVBoxLayout(marco)
        layout_marco.setContentsMargins(0, 0, 0, 0)
        layout_marco.setSpacing(0)

        numero, _, titulo = texto_banner.partition(". ")
        if not numero.isdigit():
            numero, titulo = "", texto_banner
        cabecera = QFrame(marco)
        cabecera.setObjectName("cabeceraSeccion")
        fila = QHBoxLayout(cabecera)
        fila.setContentsMargins(16, 12, 16, 12)
        fila.setSpacing(10)
        if numero:
            badge = QLabel(numero, cabecera)
            badge.setObjectName("badgeSeccion")
            fila.addWidget(badge)
        etiqueta = QLabel(titulo, cabecera)
        etiqueta.setObjectName("tituloSeccion")
        fila.addWidget(etiqueta)
        fila.addStretch(1)
        layout_marco.addWidget(cabecera)

        contenido = QWidget(marco)
        layout_contenido = QVBoxLayout(contenido)
        layout_contenido.setContentsMargins(16, 16, 16, 16)
        layout_contenido.setSpacing(12)
        layout_marco.addWidget(contenido)

        return marco, layout_contenido

    def _crear_subtitulo(self, texto: str) -> QLabel:
        label = QLabel(texto)
        label.setObjectName("subtituloBloque")
        return label

    # -- Página: Inicio (Dashboard) ---------------------------------------------

    def _crear_pagina_dashboard(self) -> QWidget:
        # La página scrollea: con servicios en curso + clima + guía puede no
        # entrar entera en pantallas chicas.
        pagina_externa = QWidget()
        layout_externo = QVBoxLayout(pagina_externa)
        layout_externo.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(pagina_externa)
        scroll.setWidgetResizable(True)
        layout_externo.addWidget(scroll)
        pagina = QWidget()
        scroll.setWidget(pagina)
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._label_saludo = QLabel("", pagina)
        self._label_saludo.setObjectName("pageTitle")
        self._label_fecha_dashboard = QLabel("", pagina)
        self._label_fecha_dashboard.setObjectName("pageSubtitle")

        contenedor_titulo = QWidget(pagina)
        layout_titulo = QVBoxLayout(contenedor_titulo)
        layout_titulo.setContentsMargins(28, 24, 28, 8)
        layout_titulo.setSpacing(2)
        layout_titulo.addWidget(self._label_saludo)
        layout_titulo.addWidget(self._label_fecha_dashboard)
        layout.addWidget(contenedor_titulo)

        # Servicios en curso: arriba de todo, uno por borrador activo.
        self._layout_en_curso = QVBoxLayout()
        self._layout_en_curso.setContentsMargins(28, 8, 28, 4)
        self._layout_en_curso.setSpacing(12)
        layout.addLayout(self._layout_en_curso)

        grid = QGridLayout()
        grid.setContentsMargins(28, 8, 28, 16)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)

        self._tarjeta_siniestros = TarjetaKPI("Siniestros del Mes", "🚨", pagina)
        self._tarjeta_hectareas = TarjetaKPI("Hectáreas Quemadas (año)", "🔥", pagina)
        self._tarjeta_moviles = TarjetaKPI("Móviles Operativos", "🚒", pagina)
        self._tarjeta_cuerpo_activo = TarjetaKPI("Cuerpo Activo", "👥", pagina)
        self._tarjeta_ruba = TarjetaKPI("Estado RUBA", "🔄", pagina)

        self._tarjeta_siniestros.clicked.connect(lambda: self._ir_a_pagina(IDX_HISTORIAL))
        self._tarjeta_hectareas.clicked.connect(lambda: self._ir_a_pagina(IDX_MAPA))
        self._tarjeta_moviles.clicked.connect(lambda: self._ir_a_pagina(IDX_DOTACIONES))
        self._tarjeta_cuerpo_activo.clicked.connect(lambda: self._ir_a_pagina(IDX_DOTACIONES))
        self._tarjeta_ruba.clicked.connect(lambda: self._ir_a_pagina(IDX_HISTORIAL))

        tarjetas = [
            self._tarjeta_siniestros, self._tarjeta_hectareas, self._tarjeta_moviles,
            self._tarjeta_cuerpo_activo, self._tarjeta_ruba,
        ]
        for i, tarjeta in enumerate(tarjetas):
            grid.addWidget(tarjeta, 0, i)
        layout.addLayout(grid)

        # Herramientas de guardia: clima (riesgo de incendio) y guía telefónica.
        herramientas = QHBoxLayout()
        herramientas.setContentsMargins(28, 0, 28, 28)
        herramientas.setSpacing(16)
        self.widget_clima = WeatherWidget(pagina)
        self.widget_guia = PhonebookWidget(pagina)
        self.widget_guia.setMinimumHeight(470)
        herramientas.addWidget(self.widget_clima, 2, Qt.AlignmentFlag.AlignTop)
        herramientas.addWidget(self.widget_guia, 3)
        layout.addLayout(herramientas)
        layout.addStretch(1)
        return pagina_externa

    def _saludo_institucional(self) -> str:
        hora = datetime.now().hour
        if hora < 12:
            saludo = "Buen día"
        elif hora < 20:
            saludo = "Buenas tardes"
        else:
            saludo = "Buenas noches"
        return f"{saludo}, guardia 👋"

    def _actualizar_dashboard(self) -> None:
        hoy = date.today()
        self._label_saludo.setText(self._saludo_institucional())
        self._label_fecha_dashboard.setText(hoy.strftime("%d/%m/%Y"))

        with get_session() as session:
            inicio_mes = date(hoy.year, hoy.month, 1)
            siniestros_mes = session.query(Incidente).filter(Incidente.fecha >= inicio_mes).count()

            ha_anio = (
                session.query(func.coalesce(func.sum(Incidente.superficie_ha), 0.0))
                .join(TipoIncidente, Incidente.tipo_incidente_id == TipoIncidente.id)
                .filter(
                    TipoIncidente.nombre == "Incendios",
                    Incidente.zona == "Rural",
                    Incidente.fecha >= date(hoy.year, 1, 1),
                )
                .scalar()
            ) or 0.0

            moviles_activos = session.query(Movil).filter(Movil.activo.is_(True)).count()
            moviles_total = session.query(Movil).count()
            personal_activo = session.query(Personal).filter(Personal.activo.is_(True)).count()

            sincronizados = session.query(Incidente).filter(
                Incidente.estado_ruba == EstadoRuba.SINCRONIZADO.value
            ).count()
            pendientes = session.query(Incidente).filter(
                Incidente.estado_ruba.in_([EstadoRuba.PENDIENTE.value, EstadoRuba.ERROR.value]),
                Incidente.estado_operativo == EstadoOperativo.CERRADO.value,
            ).count()
            resumenes_en_curso = [
                resumen_de(inc) for inc in session.query(Incidente)
                .filter(Incidente.estado_operativo == EstadoOperativo.EN_CURSO.value)
                .order_by(Incidente.creado_en)
            ]

        self._tarjeta_siniestros.set_valor(str(siniestros_mes))
        self._tarjeta_hectareas.set_valor(f"{ha_anio:.2f} ha")
        self._tarjeta_moviles.set_valor(f"{moviles_activos} / {moviles_total}")
        self._tarjeta_cuerpo_activo.set_valor(str(personal_activo))
        self._tarjeta_ruba.set_valor(f"{sincronizados} ✓ / {pendientes} ⏳")
        self._mostrar_servicios_en_curso(resumenes_en_curso)

    def _mostrar_servicios_en_curso(self, resumenes) -> None:
        for tarjeta in self._tarjetas_en_curso:
            tarjeta.setParent(None)
            tarjeta.deleteLater()
        self._tarjetas_en_curso = []
        for resumen in resumenes:
            tarjeta = TarjetaServicioEnCurso(resumen, self._pagina_dashboard)
            tarjeta.continuar.connect(self.continuar_servicio_en_curso)
            self._layout_en_curso.addWidget(tarjeta)
            self._tarjetas_en_curso.append(tarjeta)
        cantidad = len(resumenes)
        self._chip_en_curso.setVisible(bool(cantidad))
        self._chip_en_curso.setText(f"🚨 {cantidad} servicio(s) en curso")
        self._ajustar_chip(self._chip_en_curso)

    def _tick_servicios_en_curso(self) -> None:
        for tarjeta in self._tarjetas_en_curso:
            tarjeta.tick()

    # -- Página: Nueva Salida (formulario operativo de guardia) ------------------

    def _crear_pagina_formulario(self) -> QWidget:
        pagina = QWidget()
        layout_pagina = QVBoxLayout(pagina)
        layout_pagina.setContentsMargins(0, 0, 0, 0)
        layout_pagina.setSpacing(0)

        scroll = QScrollArea(pagina)
        scroll.setWidgetResizable(True)
        layout_pagina.addWidget(scroll, 1)

        contenido = QWidget()
        self._layout_contenido = QVBoxLayout(contenido)
        self._layout_contenido.setContentsMargins(28, 20, 28, 28)
        self._layout_contenido.setSpacing(20)
        scroll.setWidget(contenido)

        # Mismo orden que la carga en RUBA: General -> datos condicionales
        # del tipo de siniestro -> Damnificados -> Participación, esta última
        # agrupada por unidad como la planilla PCD2.
        self._layout_contenido.addWidget(self._crear_seccion_pedido_socorro())
        self._layout_contenido.addWidget(self._crear_seccion_datos_especificos())
        self._layout_contenido.addWidget(self._crear_seccion_damnificados())
        self._layout_contenido.addWidget(self._crear_seccion_dotaciones())
        self._layout_contenido.addWidget(self._crear_seccion_base())
        self._layout_contenido.addLayout(self._crear_acciones())
        self._layout_contenido.addStretch(1)

        return pagina

    # -- 1. Pedido de Socorro (Datos del Servicio + Tiempos de Alarma +
    #    Ubicación + Denunciante) -----------------------------------------------

    def _crear_seccion_pedido_socorro(self) -> QFrame:
        marco, layout = self._crear_contenedor_seccion("1. PEDIDO DE SOCORRO")

        layout.addWidget(self._crear_subtitulo("Datos del Servicio"))
        grid_servicio = QGridLayout()
        grid_servicio.setColumnStretch(1, 1)
        grid_servicio.setColumnStretch(3, 1)

        grid_servicio.addWidget(QLabel("N° Parte"), 0, 0)
        self.label_numero_parte = QLineEdit(marco)
        self.label_numero_parte.setReadOnly(True)
        grid_servicio.addWidget(self.label_numero_parte, 0, 1)

        grid_servicio.addWidget(QLabel("Fecha"), 0, 2)
        self.campo_fecha = DateField(marco)
        grid_servicio.addWidget(self.campo_fecha, 0, 3)

        grid_servicio.addWidget(QLabel(theme.etiqueta_requerida("Tipo de incidente")), 1, 0)
        self.combo_tipo = QComboBox(marco)
        self.combo_tipo.currentIndexChanged.connect(self._on_tipo_cambiado)
        grid_servicio.addWidget(self.combo_tipo, 1, 1)

        grid_servicio.addWidget(QLabel(theme.etiqueta_requerida("Categoría")), 1, 2)
        self.combo_categoria = QComboBox(marco)
        self.combo_categoria.currentIndexChanged.connect(self._on_categoria_cambiada)
        grid_servicio.addWidget(self.combo_categoria, 1, 3)
        layout.addLayout(grid_servicio)

        layout.addWidget(self._crear_subtitulo("Tiempos de Alarma"))
        grid_alarma = QGridLayout()
        grid_alarma.setColumnStretch(0, 1)
        grid_alarma.setColumnStretch(1, 1)
        grid_alarma.addWidget(QLabel("Hora del Llamado"), 0, 0)
        grid_alarma.addWidget(QLabel("Hora de la Sirena / Alarma"), 0, 1)
        self.campo_hora_llamado = TimeField(marco)
        self.campo_hora_toque = TimeField(marco)
        self.campo_hora_llamado.valorCambiado.connect(self._on_hora_inicio_cambiada)
        self.campo_hora_toque.valorCambiado.connect(self._on_hora_inicio_cambiada)
        grid_alarma.addWidget(self.campo_hora_llamado, 1, 0)
        grid_alarma.addWidget(self.campo_hora_toque, 1, 1)
        layout.addLayout(grid_alarma)

        layout.addWidget(self._crear_subtitulo("Datos del Denunciante / Comunicación"))
        grid_denunciante = QGridLayout()
        for col in range(4):
            grid_denunciante.setColumnStretch(col, 1)
        for col, texto in enumerate(("Nombre", "Apellido", "DNI", "Domicilio del denunciante")):
            grid_denunciante.addWidget(QLabel(texto), 0, col)
        self.entry_den_nombre = QLineEdit(marco)
        self.entry_den_apellido = QLineEdit(marco)
        self.entry_den_dni = QLineEdit(marco)
        self.entry_den_domicilio = QLineEdit(marco)
        for col, campo in enumerate((self.entry_den_nombre, self.entry_den_apellido,
                                     self.entry_den_dni, self.entry_den_domicilio)):
            grid_denunciante.addWidget(campo, 1, col)

        # Un único dato de contacto: el medio + su número/frecuencia/detalle
        # (el teléfono del denunciante sale de acá, no de un campo aparte).
        grid_denunciante.addWidget(QLabel("Medio de contacto"), 2, 0)
        grid_denunciante.addWidget(QLabel("Número / Detalle"), 2, 1, 1, 3)
        self.combo_via_comunicacion = QComboBox(marco)
        self.combo_via_comunicacion.addItems(list(MEDIOS_CONTACTO))
        self.entry_contacto_detalle = QLineEdit(marco)
        self.entry_contacto_detalle.setPlaceholderText("Ej: 3584123456 · 154.200 MHz · se presentó en el cuartel")
        grid_denunciante.addWidget(self.combo_via_comunicacion, 3, 0)
        grid_denunciante.addWidget(self.entry_contacto_detalle, 3, 1, 1, 3)
        layout.addLayout(grid_denunciante)

        layout.addWidget(self._crear_subtitulo("Alerta y autorización"))
        grid_alerta = QGridLayout()
        grid_alerta.setColumnStretch(0, 2)
        grid_alerta.setColumnStretch(1, 1)
        grid_alerta.setColumnStretch(2, 2)
        grid_alerta.addWidget(QLabel("Recibió el llamado"), 0, 0)
        grid_alerta.addWidget(QLabel("Alarma general"), 0, 1)
        grid_alerta.addWidget(QLabel("Autorizó la salida (Oficial / Suboficial)"), 0, 2)
        self.selector_recibio = SelectorBombero(self._padron, marco)
        self.radio_alarma_si = QRadioButton("Sí", marco)
        self.radio_alarma_no = QRadioButton("No", marco)
        self.grupo_alarma = QButtonGroup(marco)
        self.grupo_alarma.addButton(self.radio_alarma_si)
        self.grupo_alarma.addButton(self.radio_alarma_no)
        fila_alarma = QHBoxLayout()
        fila_alarma.addWidget(self.radio_alarma_si)
        fila_alarma.addWidget(self.radio_alarma_no)
        fila_alarma.addStretch(1)
        self._mandos = mandos_del_padron(self._padron) if self._padron else []
        self.selector_autorizo = SelectorBombero(self._mandos, marco)
        if not self._mandos:
            self.selector_autorizo.setPlaceholderText("Sin mandos: cargá el grado en Personal y Unidades")
        self.selector_autorizo.setToolTip(
            "Solo personal con grado de Suboficial u Oficial (o cargo de conducción en el padrón)."
        )
        grid_alerta.addWidget(self.selector_recibio, 1, 0)
        grid_alerta.addLayout(fila_alarma, 1, 1)
        grid_alerta.addWidget(self.selector_autorizo, 1, 2)
        layout.addLayout(grid_alerta)

        layout.addWidget(self._crear_subtitulo("Ubicación y Cartografía"))
        grid_ubicacion = QGridLayout()
        grid_ubicacion.setColumnStretch(0, 2)
        grid_ubicacion.setColumnStretch(1, 1)
        grid_ubicacion.setColumnStretch(2, 1)
        grid_ubicacion.addWidget(QLabel("Localidad"), 0, 0)
        grid_ubicacion.addWidget(QLabel(theme.etiqueta_requerida("Dirección / Lugar del hecho")), 0, 1)
        grid_ubicacion.addWidget(QLabel("Zona"), 0, 2)

        self.entry_localidad = QLineEdit(marco)
        self.entry_localidad.setText("Adelia María")
        self.entry_calle = QLineEdit(marco)
        self.entry_calle.setPlaceholderText("Ej: Belgrano 58 · Ruta 8 km 603 (Enter: ubicar en el mapa)")
        self.entry_calle.textChanged.connect(lambda t: theme.marcar_invalido(self.entry_calle, False) if t else None)

        self.radio_urbana = QRadioButton("Urbana", marco)
        self.radio_rural = QRadioButton("Rural", marco)
        self.radio_urbana.setChecked(True)
        self.grupo_zona = QButtonGroup(marco)
        self.grupo_zona.addButton(self.radio_urbana)
        self.grupo_zona.addButton(self.radio_rural)

        fila_zona = QHBoxLayout()
        fila_zona.addWidget(self.radio_urbana)
        fila_zona.addWidget(self.radio_rural)
        fila_zona.addStretch(1)

        grid_ubicacion.addWidget(self.entry_localidad, 1, 0)
        grid_ubicacion.addWidget(self.entry_calle, 1, 1)
        grid_ubicacion.addLayout(fila_zona, 1, 2)
        grid_ubicacion.addWidget(QLabel("Coordenadas / Referencia rural (opcional)"), 2, 0, 1, 3)
        self.entry_referencia = QLineEdit(marco)
        self.entry_referencia.setPlaceholderText("Ej: -33.6315, -64.0212 · campo de Pérez, 2 km al norte de la ruta")
        self.entry_referencia.setMaxLength(200)
        grid_ubicacion.addWidget(self.entry_referencia, 3, 0, 1, 2)
        self.boton_marcar_mapa = QPushButton("📍 Marcar en Mapa", marco)
        self.boton_marcar_mapa.setObjectName("botonAhora")
        self.boton_marcar_mapa.setToolTip(
            "Abre el mapa para fijar el punto (clic o coordenadas) y guarda la imagen para el parte")
        self.boton_marcar_mapa.clicked.connect(self._abrir_marcar_en_mapa)
        grid_ubicacion.addWidget(self.boton_marcar_mapa, 3, 2)
        self.label_imagen_mapa = QLabel("", marco)
        self.label_imagen_mapa.setProperty("muted", True)
        self.label_imagen_mapa.setOpenExternalLinks(True)
        self.label_imagen_mapa.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.label_imagen_mapa.setVisible(False)
        grid_ubicacion.addWidget(self.label_imagen_mapa, 4, 0, 1, 3)
        layout.addLayout(grid_ubicacion)

        # El visor geográfico va AL FINAL de la sección: así no corta el
        # flujo de carga de datos (denunciante, dirección) con un mapa de
        # más de 300px de alto en el medio del formulario.
        self.mapa = MapWidget(
            marco, fuente_direccion=lambda: (self.entry_calle.text(), self.entry_localidad.text()),
        )
        self.mapa.setMinimumHeight(380)
        layout.addWidget(self.mapa)
        self.entry_calle.returnPressed.connect(self.mapa._buscar_direccion)
        self.mapa.coordenadas_cambiadas.connect(self._on_coordenadas_mapa)

        return marco

    # Un par "lat, lon" al principio del campo (escrito a mano o por el mapa).
    _PATRON_COORDS_INICIALES = re.compile(r"^\s*-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+\s*[·,;-]?\s*")

    def _on_coordenadas_mapa(self, lat: float, lng: float) -> None:
        """Al marcar el siniestro en el mapa, las coordenadas encabezan el
        campo "Coordenadas / Referencia rural" y se conserva la referencia
        escrita a mano (ej. "campo de Pérez") que viniera después."""
        coordenadas = f"{lat:.5f}, {lng:.5f}"
        resto = self._PATRON_COORDS_INICIALES.sub("", self.entry_referencia.text().strip(), count=1)
        self.entry_referencia.setText(f"{coordenadas} · {resto}" if resto else coordenadas)
        self._actualizar_label_imagen_mapa()

    # -- "📍 Marcar en Mapa" ------------------------------------------------------------------

    def _abrir_marcar_en_mapa(self) -> None:
        """Abre el selector centrado en el punto ya cargado (mapa del formulario
        o coordenadas escritas en "Referencia"); si no hay, en Adelia María."""
        datos = self.mapa.obtener_datos()
        lat, lon = datos["latitud"], datos["longitud"]
        if lat is None or lon is None:
            escrito = self._PATRON_COORDS_INICIALES.match(self.entry_referencia.text() or "")
            par = parsear_par(escrito.group(0).rstrip(" ·,;-")) if escrito else None
            if par is not None:
                lat, lon = par
        dialogo = DialogoMarcarMapa(
            self.label_numero_parte.text(), lat, lon, datos["superficie_ha"], datos["geometria_geojson"],
            fuente_direccion=lambda: (self.entry_calle.text(), self.entry_localidad.text()), parent=self,
        )
        if dialogo.exec() != QDialog.DialogCode.Accepted or dialogo.resultado is None:
            return
        r = dialogo.resultado
        mapa = r["mapa"]
        # El mapa del formulario toma el punto (y el polígono si se dibujó en el visor).
        self.mapa.cargar_incidente(self.label_numero_parte.text(), r["latitud"], r["longitud"],
                                   mapa["superficie_ha"], mapa["geometria_geojson"])
        self._ruta_imagen_mapa = r["ruta_imagen"]
        self._coords_imagen_mapa = (r["latitud"], r["longitud"])
        self._on_coordenadas_mapa(r["latitud"], r["longitud"])  # "Coordenadas / Referencia" + etiqueta
        theme.set_tono(self.statusBar(), "ok")
        self.statusBar().showMessage(f"Imagen del mapa guardada: {r['ruta_imagen']}", 10000)

    def _actualizar_label_imagen_mapa(self) -> None:
        ruta = getattr(self, "_ruta_imagen_mapa", None)
        if not ruta:
            self.label_imagen_mapa.setVisible(False)
            return
        uri = Path(ruta).resolve().as_uri()
        texto = f"🗺 Imagen del mapa: <a href='{uri}'>{Path(ruta).name}</a>"
        punto = self.mapa.obtener_datos()
        coords = getattr(self, "_coords_imagen_mapa", None)
        if coords and (punto["latitud"], punto["longitud"]) != coords:
            texto += " &nbsp;⚠ el punto cambió: volvé a \"📍 Marcar en Mapa\" para actualizar la imagen"
        elif not Path(ruta).is_file():
            texto += " &nbsp;⚠ el archivo ya no existe"
        self.label_imagen_mapa.setText(texto)
        self.label_imagen_mapa.setVisible(True)

    # -- 2. Datos específicos del siniestro (según Tipo/Subtipo) ------------------

    def _crear_seccion_datos_especificos(self) -> QFrame:
        marco, layout = self._crear_contenedor_seccion("2. DATOS ESPECÍFICOS DEL SINIESTRO")
        self.panel_datos_especificos = PanelDatosEspecificos(marco)
        layout.addWidget(self.panel_datos_especificos)
        return marco

    # -- Catálogos oficiales (padrón, móviles, tipos de tarea) --------------------

    def _cargar_catalogos_oficiales(self) -> None:
        """Padrón de bomberos (Excel) y móviles oficiales (ruba_mapping.json).
        Si alguno falta, la app arranca igual y se avisa en la barra de estado."""
        self._avisos_catalogos: List[str] = []
        try:
            # Solo se ofrece el personal Activo en la base (una baja cargada en
            # la app pesa más que el Excel de RUBA, que puede estar desactualizado).
            self._padron = padron_activo(obtener_padron().bomberos)
        except FileNotFoundError:
            self._padron = []
            self._avisos_catalogos.append(
                "Padrón de bomberos sin cargar: importalo desde Personal y Unidades → 📥 Importar padrón.")
        except Exception as e:  # noqa: BLE001 - un Excel roto nunca debe impedir que la app arranque
            self._padron = []
            self._avisos_catalogos.append(f"No se pudo leer el padrón de bomberos ({e}).")
        # Móviles de las dotaciones: los de la BASE (activos y con Id de RUBA),
        # ya no los de ruba_mapping.json -- así una unidad eliminada o dada de
        # baja no se sigue ofreciendo (ni rompe el guardado del parte).
        try:
            self._moviles_ruba = moviles_para_despacho()
        except Exception as e:  # noqa: BLE001 - la app arranca igual
            self._moviles_ruba = []
            self._avisos_catalogos.append(f"No se pudieron leer las unidades de la base ({e}).")
        if not self._moviles_ruba:
            self._avisos_catalogos.append(
                "No hay unidades activas con Id de RUBA: importalas desde Personal y Unidades "
                "→ 📥 Importar Unidades desde Excel (RUBA), o cargá el Id RUBA en cada unidad.")

    # -- 3. Damnificados, seguro y reseña ------------------------------------------

    def _crear_seccion_damnificados(self) -> QFrame:
        marco, layout = self._crear_contenedor_seccion("3. DAMNIFICADOS, SEGURO Y RESEÑA")
        self.panel_damnificados = PanelDamnificados(self._padron, marco)
        layout.addWidget(self.panel_damnificados)

        layout.addWidget(self._crear_subtitulo("Seguro del siniestro y reseña"))
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.addWidget(QLabel("Compañía de seguros (si se conoce)"), 0, 0)
        grid.addWidget(QLabel("N° de póliza (si se conoce)"), 0, 1)
        self.entry_seguro_compania = QLineEdit(marco)
        self.entry_seguro_poliza = QLineEdit(marco)
        grid.addWidget(self.entry_seguro_compania, 1, 0)
        grid.addWidget(self.entry_seguro_poliza, 1, 1)

        etiqueta_resena = QLabel(theme.etiqueta_requerida("Reseña operativa"))
        etiqueta_resena.setToolTip("RUBA exige la descripción del incidente para poder cargarlo.")
        grid.addWidget(etiqueta_resena, 2, 0, 1, 2)
        self.texto_resena = QTextEdit(marco)
        self.texto_resena.setFixedHeight(90)
        grid.addWidget(self.texto_resena, 3, 0, 1, 2)
        layout.addLayout(grid)
        return marco

    # -- 4. Dotaciones por Unidad (formato PCD2) ----------------------------------

    def _crear_seccion_dotaciones(self) -> QFrame:
        marco, layout = self._crear_contenedor_seccion("4. DOTACIONES MÓVILES (UNIDADES AL LUGAR)")

        layout.addWidget(self._crear_subtitulo(
            "Horario general del servicio · se calcula solo (llamado → última llegada), ajustable a mano"
        ))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        for col in range(4):
            grid.setColumnStretch(col, 1)
        for col, texto in enumerate(("Fecha inicio", "Hora inicio (llamado)", "Fecha fin", "Hora fin (última llegada)")):
            grid.addWidget(QLabel(texto), 0, col)
        self.campo_fecha_salida = DateField(marco)
        self.campo_hora_salida = TimeField(marco)
        self.campo_fecha_llegada = DateField(marco)
        self.campo_hora_llegada = TimeField(marco)
        for col, (nombre, campo) in enumerate((
            ("fecha_salida", self.campo_fecha_salida), ("hora_salida", self.campo_hora_salida),
            ("fecha_llegada", self.campo_fecha_llegada), ("hora_llegada", self.campo_hora_llegada),
        )):
            grid.addWidget(campo, 1, col)
            campo.valorCambiado.connect(lambda n=nombre: self._on_horario_general_editado(n))
        layout.addLayout(grid)

        fila_subtitulo = QHBoxLayout()
        fila_subtitulo.addWidget(self._crear_subtitulo("Dotaciones (cada horario sigue al general hasta editarlo)"), 1)
        fila_subtitulo.addWidget(self._crear_boton_refrescar(marco))
        layout.addLayout(fila_subtitulo)
        self.panel_dotaciones = PanelDotaciones(self._moviles_ruba, self._padron, marco)
        self.panel_dotaciones.llegadas_cambiadas.connect(self._recalcular_fin_servicio)
        layout.addWidget(self.panel_dotaciones)
        return marco

    def _crear_seccion_base(self) -> QFrame:
        marco, layout = self._crear_contenedor_seccion("5. PERSONAL EN BASE (GUARDIA / RESERVA)")
        self.panel_base = PanelPersonalBase(self._padron, marco)
        layout.addWidget(self.panel_base)
        return marco

    def _horario_general(self) -> HorarioServicio:
        return HorarioServicio(
            fecha_salida=self.campo_fecha_salida.value(), hora_salida=self.campo_hora_salida.value(),
            fecha_llegada=self.campo_fecha_llegada.value(), hora_llegada=self.campo_hora_llegada.value(),
        )

    def _on_horario_general_cambiado(self) -> None:
        self.panel_dotaciones.set_horario_general(self._horario_general())

    def _on_horario_general_editado(self, campo: str) -> None:
        """Ajuste manual: ese campo deja de recalcularse solo."""
        self._horario_manual.add(campo)
        self._ajustar_fecha_fin_general()
        self._on_horario_general_cambiado()

    def _ajustar_fecha_fin_general(self) -> None:
        """Fecha fin = fecha inicio, o el día siguiente si la hora fin es
        anterior a la de inicio (el servicio cruzó la medianoche). No pisa una
        fecha fin elegida a mano ni la que viene de la llegada de una dotación."""
        if "fecha_llegada" in self._horario_manual:
            return
        panel = getattr(self, "panel_dotaciones", None)
        if "hora_llegada" not in self._horario_manual and panel is not None and panel.llegada_mas_tardia() is not None:
            return
        nueva = fecha_fin_ajustada(self.campo_fecha_salida.value(), self.campo_hora_salida.value(),
                                   None, self.campo_hora_llegada.value())
        if nueva is not None and self.campo_hora_llegada.value() is not None:
            self.campo_fecha_llegada.set_value(nueva)

    def _on_hora_inicio_cambiada(self) -> None:
        """Inicio del servicio = hora del llamado (o de la alarma, si no hay llamado)."""
        if "hora_salida" in self._horario_manual:
            return
        inicio = self.campo_hora_llamado.value() or self.campo_hora_toque.value()
        if inicio is None:
            return
        self.campo_hora_salida.set_value(inicio)
        self._ajustar_fecha_fin_general()
        self._on_horario_general_cambiado()

    def _recalcular_fin_servicio(self) -> None:
        """Fin del servicio = la llegada más tardía cargada en las dotaciones."""
        if {"fecha_llegada", "hora_llegada"} & self._horario_manual:
            return
        ultima = self.panel_dotaciones.llegada_mas_tardia()
        if ultima is None:
            return
        fecha, hora = ultima
        self.campo_fecha_llegada.set_value(fecha)
        self.campo_hora_llegada.set_value(hora)
        self._on_horario_general_cambiado()

    def _telefono_denunciante(self) -> Optional[str]:
        """El teléfono del denunciante es el "Número / Detalle" cuando el
        medio es telefónico (va a RUBA como teléfono del solicitante)."""
        if self.combo_via_comunicacion.currentText() in MEDIOS_TELEFONICOS:
            return self.entry_contacto_detalle.text().strip() or None
        return None

    # -- Acciones -----------------------------------------------------------

    def _crear_acciones(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        self.boton_limpiar = QPushButton("Limpiar Formulario", self)
        self.boton_limpiar.setObjectName("botonLimpiar")
        self.boton_limpiar.clicked.connect(self._limpiar_formulario)

        self.boton_borrador = QPushButton("⏱️ Salida en Curso (Guardar Borrador)", self)
        self.boton_borrador.setObjectName("botonBorradorCurso")
        self.boton_borrador.setToolTip(
            "Guarda el parte mientras la dotación sigue en el lugar: no pide regreso ni datos de cierre, "
            "no imprime planillas ni carga a RUBA. Se retoma desde Inicio."
        )
        self.boton_borrador.clicked.connect(self._guardar_borrador)

        self.boton_guardar_local = QPushButton("💾 Guardar e Imprimir Planillas", self)
        self.boton_guardar_local.setObjectName("botonPrimarioAzul")
        self.boton_guardar_local.setToolTip(
            "Guarda en la base local (queda PENDIENTE de RUBA) y abre las planillas PCS/PCD2. "
            "La carga en RUBA se hace desde Estadísticas (🚀 Cargar Seleccionados a RUBA)."
        )
        self.boton_guardar_local.clicked.connect(self._guardar_y_generar_planillas)

        for boton in (self.boton_limpiar, self.boton_borrador, self.boton_guardar_local):
            boton.setCursor(Qt.CursorShape.PointingHandCursor)

        layout.setSpacing(12)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(self.boton_limpiar, 1)
        layout.addWidget(self.boton_borrador, 2)
        layout.addWidget(self.boton_guardar_local, 2)
        return layout

    # -- Carga de catálogos ---------------------------------------------------

    def _cargar_catalogos(self) -> None:
        with get_session() as session:
            tipos = session.query(TipoIncidente).order_by(TipoIncidente.id).all()
            self.combo_tipo.clear()
            for tipo in tipos:
                self.combo_tipo.addItem(tipo.nombre, tipo.id)
        self._on_tipo_cambiado()

    def _on_tipo_cambiado(self, *_args) -> None:
        tipo_id = self.combo_tipo.currentData()
        self.combo_categoria.blockSignals(True)
        self.combo_categoria.clear()
        self.combo_categoria.blockSignals(False)
        if tipo_id is None:
            self._on_categoria_cambiada()
            return
        with get_session() as session:
            categorias = (
                session.query(CategoriaIncidente)
                .filter(CategoriaIncidente.tipo_incidente_id == tipo_id)
                .order_by(CategoriaIncidente.nombre)
                .all()
            )
            self.combo_categoria.blockSignals(True)
            for cat in categorias:
                self.combo_categoria.addItem(cat.nombre, cat.id)
                self.combo_categoria.setItemData(self.combo_categoria.count() - 1, cat.codigo_ruba, ROL_CODIGO_RUBA)
            self.combo_categoria.blockSignals(False)
        self._on_categoria_cambiada()

    def _on_categoria_cambiada(self, *_args) -> None:
        codigo_ruba = self.combo_categoria.currentData(ROL_CODIGO_RUBA)
        self.panel_datos_especificos.actualizar(self.combo_tipo.currentData(), codigo_ruba)
        self.panel_damnificados.set_es_accidente(self.panel_datos_especificos.formulario_activo() == FORM_ACCIDENTE)

    def _datos_especificos(self) -> Optional[Dict[str, Any]]:
        """Bloque condicional del siniestro + (en Accidentes) los vehículos
        involucrados, que se guardan en el mismo JSON (datos_especificos)."""
        datos = self.panel_datos_especificos.datos()
        vehiculos = self.panel_damnificados.vehiculos()
        if datos is not None and vehiculos:
            datos = {**datos, "vehiculos": vehiculos}
        return datos

    # -- Validación y guardado -------------------------------------------------

    def _validar(self) -> list[str]:
        errores = []
        if not numero_parte_ruba(self.label_numero_parte.text()):
            errores.append("El N° de Parte no es válido (RUBA necesita dígitos).")
        falta_tipo = self.combo_tipo.currentData() is None
        falta_categoria = self.combo_categoria.currentData() is None
        falta_calle = not self.entry_calle.text().strip()
        theme.marcar_invalido(self.combo_tipo, falta_tipo)
        theme.marcar_invalido(self.combo_categoria, falta_categoria)
        theme.marcar_invalido(self.entry_calle, falta_calle)
        if falta_tipo:
            errores.append("Seleccioná el Tipo de incidente.")
        if falta_categoria:
            errores.append("Seleccioná la Categoría del incidente.")
        if falta_calle:
            errores.append("Completá la Calle/Altura del siniestro.")
        errores.extend(self._validar_participacion())
        return errores

    def _validar_borrador(self) -> List[str]:
        """Servicio en curso: solo lo que impide guardar (N° de parte, personas
        fuera del padrón o repetidas, unidades sin móvil). Sin regreso, sin
        Encargado y sin datos de cierre está permitido."""
        errores = []
        if not numero_parte_ruba(self.label_numero_parte.text()):
            errores.append("El N° de Parte no es válido (RUBA necesita dígitos).")
        errores.extend(self.panel_damnificados.validar(parcial=True))
        errores.extend(self.panel_dotaciones.validar(parcial=True))
        errores.extend(self._validar_personas_del_servicio())
        return errores

    def _validar_personas_del_servicio(self) -> List[str]:
        """Selectores de despacho/base con texto que no está en el padrón, y
        nadie repetido entre dotaciones y base."""
        errores = [
            f"{rotulo}: '{selector.text().strip()}' no coincide con nadie del padrón."
            for rotulo, selector in (("Recibió el llamado", self.selector_recibio),
                                     ("Autorizó la salida", self.selector_autorizo))
            if selector.text().strip() and selector.id_ruba() is None
        ]
        errores.extend(self.panel_base.validar())
        errores.extend(personas_repetidas(self.panel_dotaciones.datos(), self.panel_base.datos()))
        return errores

    def _validar_participacion(self) -> List[str]:
        errores: List[str] = []
        horario = self._horario_general()
        if horario.hora_salida and horario.hora_llegada and horario.fecha_salida and horario.fecha_llegada:
            if (horario.fecha_llegada, horario.hora_llegada) < (horario.fecha_salida, horario.hora_salida):
                errores.append("El regreso del servicio es anterior a la salida (se comparan fecha y hora: "
                               "si cruzó la medianoche, la fecha fin es la del día siguiente).")
        errores.extend(self.panel_damnificados.validar())
        errores.extend(self.panel_dotaciones.validar())
        errores.extend(self._validar_personas_del_servicio())
        return errores

    def _persistir_incidente(self, estado: str = EstadoOperativo.CERRADO.value) -> Optional[Tuple[int, str]]:
        """Valida y guarda el incidente + sus dotaciones (SalidaUnidad +
        DotacionSalida por cada una) en la DB. Devuelve (id, numero_parte),
        o None si la validación falló (y ya mostró el aviso correspondiente).

        `estado`: CERRADO (validación completa) o EN_CURSO (borrador, validación
        parcial). Si el formulario tiene abierto un servicio en curso, se
        ACTUALIZA ese mismo incidente (mismo N° y mismo id de RUBA) y se
        rehacen sus unidades y damnificados."""
        en_curso = estado == EstadoOperativo.EN_CURSO.value
        errores = self._validar_borrador() if en_curso else self._validar()
        errores += self._moviles_inexistentes()
        if errores:
            QMessageBox.warning(self, "Revisá el formulario", "\n".join(f"• {e}" for e in errores))
            return None

        # Finalizar la planilla exige confirmar la autoría con el PIN del
        # responsable (los borradores EN CURSO no: se guardan en plena salida).
        autor_id = None
        if not en_curso:
            autor_id = pedir_autor(self.label_numero_parte.text(), self._autor_sugerido(), self)
            if autor_id is None:
                self.statusBar().showMessage("Guardado cancelado: falta confirmar la autoría con PIN.", 8000)
                return None

        zona = "Urbana" if self.radio_urbana.isChecked() else "Rural"
        datos_especificos = self._datos_especificos()
        horario = self._horario_general()
        conteos = self.panel_damnificados.conteos()

        with get_session() as session:
            campos = dict(
                estado_operativo=estado,
                tipo_incidente_id=self.combo_tipo.currentData(),
                categoria_id=self.combo_categoria.currentData(),
                fecha=self.campo_fecha.value(),
                hora_llamado=self.campo_hora_llamado.value(),
                hora_toque=self.campo_hora_toque.value(),
                fecha_salida=horario.fecha_salida,
                hora_salida=horario.hora_salida,
                fecha_llegada=horario.fecha_llegada,
                hora_regreso=horario.hora_llegada,
                calle_altura=self.entry_calle.text().strip() or None,
                localidad=self.entry_localidad.text().strip() or "Adelia María",
                zona=zona,
                denunciante_nombre=self.entry_den_nombre.text().strip() or None,
                denunciante_apellido=self.entry_den_apellido.text().strip() or None,
                denunciante_dni=self.entry_den_dni.text().strip() or None,
                denunciante_telefono=self._telefono_denunciante(),
                referencia_ubicacion=self.entry_referencia.text().strip() or None,
                via_comunicacion=self.combo_via_comunicacion.currentText(),
                contacto_detalle=self.entry_contacto_detalle.text().strip() or None,
                denunciante_domicilio=self.entry_den_domicilio.text().strip() or None,
                alarma_general=(True if self.radio_alarma_si.isChecked()
                                else False if self.radio_alarma_no.isChecked() else None),
                damnificados_heridos=conteos["heridos"],
                damnificados_muertos=conteos["fallecidos"],
                damnificados_desaparecidos=conteos["desaparecidos"],
                bomberos_lesionados=conteos["bomberos"],
                seguro_compania=self.entry_seguro_compania.text().strip() or None,
                seguro_poliza=self.entry_seguro_poliza.text().strip() or None,
                resena_operativa=self.texto_resena.toPlainText().strip() or None,
                datos_especificos_json=json.dumps(datos_especificos, ensure_ascii=False) if datos_especificos else None,
                **self.mapa.obtener_datos(),
                ruta_imagen_mapa=getattr(self, "_ruta_imagen_mapa", None),
            )
            if autor_id is not None:
                campos.update(confecciono_personal_id=autor_id, confeccionado_en=datetime.now())
            incidente = session.get(Incidente, self._incidente_en_edicion) if self._incidente_en_edicion else None
            if self._incidente_en_edicion is not None and incidente is None:
                QMessageBox.warning(
                    self, "El servicio ya no existe",
                    "El servicio que estabas editando se eliminó del historial. Se guardará como un servicio nuevo.",
                )
                self._incidente_en_edicion = None
                self.label_numero_parte.setText(siguiente_numero_parte(session))
            if incidente is not None:
                for campo, valor in campos.items():
                    setattr(incidente, campo, valor)
                # Corregido tras un error: vuelve a PENDIENTE para cargarlo desde el
                # Historial (conserva ruba_id_remoto: el reintento retoma ese incidente).
                if incidente.estado_ruba != EstadoRuba.SINCRONIZADO.value:
                    incidente.estado_ruba = EstadoRuba.PENDIENTE.value
                incidente.actualizado_en = datetime.now()
                # Unidades, dotación y damnificados se rehacen desde el formulario.
                for relacion in (incidente.dotacion, incidente.salidas_unidad, incidente.damnificados_civiles,
                                 incidente.bienes_afectados, incidente.bomberos_damnificados,
                                 incidente.personal_base):
                    relacion.clear()
                session.flush()
            else:
                incidente = Incidente(numero_parte=self.label_numero_parte.text(),
                                      estado_ruba=EstadoRuba.PENDIENTE.value, **campos)
                session.add(incidente)
                session.flush()  # asigna incidente.id

            _, personal_por_id_ruba = self._mapas_id_ruba(session)
            incidente.recibio_personal_id = personal_por_id_ruba.get(self.selector_recibio.id_ruba())
            incidente.autorizo_personal_id = personal_por_id_ruba.get(self.selector_autorizo.id_ruba())
            self._persistir_damnificados(session, incidente)
            self._persistir_participacion(session, incidente)
            self._persistir_base(session, incidente, personal_por_id_ruba)

            incidente_id = incidente.id
            numero_parte = incidente.numero_parte

        return incidente_id, numero_parte

    def _autor_sugerido(self) -> Optional[int]:
        """Operador 1 de la guardia (id local), para preseleccionarlo en el PIN."""
        id_ruba = self.panel_base.datos().get("operador_1")
        if id_ruba is None:
            return None
        with get_session() as session:
            persona = session.query(Personal).filter(Personal.id_ruba == id_ruba).first()
            return persona.id if persona else None

    def _moviles_inexistentes(self) -> List[str]:
        """Dotaciones cuyo móvil ya no está en la base (eliminado mientras el
        formulario estaba abierto): se avisa en vez de fallar al guardar."""
        with get_session() as session:
            existentes = {i for (i,) in session.query(Movil.id_ruba).filter(Movil.id_ruba.isnot(None))}
        return [
            f"Dotación N° {d['numero']}: la unidad elegida (Id RUBA {d['movil_id_ruba']}) ya no existe; elegí otra."
            for d in self.panel_dotaciones.datos()
            if d["movil_id_ruba"] is not None and d["movil_id_ruba"] not in existentes
        ]

    def _refrescar_padron_despacho(self, releer_excel: bool = False) -> None:
        """Vuelve a leer el padrón (filtrando al personal inactivo en la base)
        y lo reparte a todos los selectores de bomberos de la planilla, sin
        perder a quien ya estaba elegido en el parte abierto."""
        if releer_excel:
            obtener_padron.cache_clear()
        try:
            padron = padron_activo(obtener_padron().bomberos)
        except FileNotFoundError:
            padron = []
        except Exception:  # noqa: BLE001 - se queda con el padrón anterior
            return
        self._padron = padron
        if padron:
            self._avisos_catalogos = [a for a in self._avisos_catalogos if "padrón" not in a.lower()]
        if getattr(self, "panel_dotaciones", None) is None:
            return
        self.panel_dotaciones.actualizar_padron(padron)
        self.panel_base.set_padron(padron)
        self.panel_damnificados.set_padron(padron)
        self.selector_recibio.set_padron(padron)
        self._mandos = mandos_del_padron(padron) if padron else []
        self.selector_autorizo.set_padron(self._mandos)
        self._actualizar_chips()

    def _crear_boton_refrescar(self, parent: QWidget) -> QPushButton:
        boton = QPushButton("🔄 Refrescar", parent)
        boton.setObjectName("botonAhora")
        boton.setToolTip("Recarga el padrón de bomberos y las unidades desde la base, sin reiniciar la app")
        boton.setCursor(Qt.CursorShape.PointingHandCursor)
        boton.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        boton.clicked.connect(self._refrescar_padron_y_unidades)
        return boton

    def _refrescar_padron_y_unidades(self) -> None:
        """Botón 🔄 Refrescar: recarga bomberos y unidades desde la base sin
        reiniciar la app."""
        self._refrescar_padron_despacho(releer_excel=True)
        if hasattr(self, "_tabla_personal_dotacion"):
            self._cargar_pagina_dotaciones()
        else:
            self._refrescar_moviles_despacho()
        self.statusBar().showMessage(
            f"Padrón y unidades actualizados: {len(self._padron)} bombero(s) activo(s), "
            f"{len(self._moviles_ruba)} unidad(es) para despacho.", 6000)

    def _refrescar_moviles_despacho(self) -> None:
        """Tras importar / editar / eliminar / dar de baja unidades."""
        panel = getattr(self, "panel_dotaciones", None)
        if panel is None:
            return
        try:
            self._moviles_ruba = moviles_para_despacho()
        except Exception:  # noqa: BLE001 - se queda con la lista anterior
            return
        panel.actualizar_moviles(self._moviles_ruba)

    def _mapas_id_ruba(self, session) -> Tuple[Dict[int, int], Dict[int, int]]:
        moviles = {m.id_ruba: m.id for m in session.query(Movil).filter(Movil.id_ruba.isnot(None))}
        personal = {p.id_ruba: p.id for p in session.query(Personal).filter(Personal.id_ruba.isnot(None))}
        return moviles, personal

    def _persistir_damnificados(self, session, incidente: Incidente) -> None:
        """Solo lo de las tarjetas en "Sí": civiles (todas las filas, con su
        lesión), bienes y bomberos damnificados."""
        _, personal_por_id_ruba = self._mapas_id_ruba(session)
        for civil in self.panel_damnificados.civiles():
            session.add(DamnificadoCivil(incidente_id=incidente.id, **civil))
        for bien in self.panel_damnificados.bienes():
            session.add(BienAfectado(incidente_id=incidente.id, **bien))
        for bombero in self.panel_damnificados.bomberos():
            session.add(BomberoDamnificado(
                incidente_id=incidente.id, personal_id=personal_por_id_ruba[bombero["id_ruba"]],
                detalle_atencion=bombero["detalle"],
            ))

    def _persistir_participacion(self, session, incidente: Incidente) -> None:
        """Una SalidaUnidad por dotación (móvil, chofer, horarios, arribo a QTH,
        grado del Jefe y su firma validada por PIN) y una DotacionSalida por
        el Jefe (rol A_CARGO) y por cada embarcado; todos Intervinientes."""
        movil_por_id_ruba, personal_por_id_ruba = self._mapas_id_ruba(session)
        for dotacion in self.panel_dotaciones.datos():
            salida = SalidaUnidad(
                incidente_id=incidente.id,
                movil_id=movil_por_id_ruba[dotacion["movil_id_ruba"]],
                chofer_personal_id=personal_por_id_ruba.get(dotacion["chofer_id_ruba"]),
                fecha_salida=dotacion["fecha_salida"], hora_salida=dotacion["hora_salida"],
                hora_arribo=dotacion["hora_arribo"],
                fecha_llegada=dotacion["fecha_llegada"], hora_regreso=dotacion["hora_llegada"],
                jefe_grado=dotacion["jefe_grado"],
            )
            session.add(salida)
            session.flush()  # asigna salida.id

            jefe_id = personal_por_id_ruba.get(dotacion["jefe_id_ruba"])
            filas = [(jefe_id, RolDotacion.A_CARGO.value)] if jefe_id else []
            filas += [(personal_por_id_ruba[b["id_ruba"]], RolDotacion.BOMBERO.value)
                      for b in dotacion["bomberos"] if b["id_ruba"] is not None]
            for personal_id, rol in filas:
                session.add(DotacionSalida(
                    incidente_id=incidente.id, movil_id=salida.movil_id, personal_id=personal_id,
                    rol=rol, salida_unidad_id=salida.id, tipo_tarea="1",
                ))

            if dotacion["firmada"] and jefe_id:
                salida.firma_validada_en = dotacion["firma_validada_en"]
                salida.ruta_firma_auditoria = self._copiar_firma_auditoria(
                    session, incidente.numero_parte, incidente.fecha, dotacion["numero"], jefe_id,
                )

    def _persistir_base(self, session, incidente: Incidente, personal_por_id_ruba: Dict[int, int]) -> None:
        base = self.panel_base.datos()
        filas = [(base["operador_1"], FuncionBase.OPERADOR_1.value), (base["operador_2"], FuncionBase.OPERADOR_2.value)]
        filas += [(id_ruba, FuncionBase.APRESTO.value) for id_ruba in base["apresto"]]
        for orden, (id_ruba, funcion) in enumerate(filas):
            if id_ruba is not None:
                session.add(PersonalBase(incidente_id=incidente.id, personal_id=personal_por_id_ruba[id_ruba],
                                         funcion=funcion, orden=orden))

    # -- Payload para la automatización de RUBA --------------------------------------

    def obtener_payload_servicio(self) -> Dict[str, Any]:
        """Todo el formulario en la estructura que consume la automatización
        de RUBA (ver app/services/ruba_payload.py). Las personas se resuelven
        contra el legajo local vinculado al padrón (por id de RUBA), igual
        que cuando el payload se arma desde un incidente guardado."""
        horario = self._horario_general()
        unidades_ui = self.panel_dotaciones.datos()
        base_ui = self.panel_base.datos()
        ids_base = [base_ui["operador_1"], base_ui["operador_2"], *base_ui["apresto"]]
        bomberos_damnificados_ui = self.panel_damnificados.bomberos()
        ids_personas = {u["chofer_id_ruba"] for u in unidades_ui} | {u["jefe_id_ruba"] for u in unidades_ui}
        ids_personas |= {b["id_ruba"] for u in unidades_ui for b in u["bomberos"]}
        ids_personas |= {b["id_ruba"] for b in bomberos_damnificados_ui} | set(ids_base)
        ids_personas.discard(None)
        with get_session() as session:
            personas = {
                p.id_ruba: persona_desde_personal(p)
                for p in session.query(Personal).filter(Personal.id_ruba.in_(ids_personas))
            }

        unidades = [
            UnidadServicio(
                movil_id_ruba=u["movil_id_ruba"], chofer=personas.get(u["chofer_id_ruba"]),
                jefe=personas.get(u["jefe_id_ruba"]),
                fecha_salida=u["fecha_salida"], hora_salida=u["hora_salida"],
                fecha_llegada=u["fecha_llegada"], hora_llegada=u["hora_llegada"],
                embarcados=[personas[b["id_ruba"]] for b in u["bomberos"] if b["id_ruba"] in personas],
            )
            for u in unidades_ui
        ]
        base = [personas[i] for i in ids_base if i in personas]
        vehiculos, bomberos = aplanar_unidades(unidades, base)
        conteos = self.panel_damnificados.conteos()
        datos = DatosServicio(
            numero_parte=self.label_numero_parte.text(),
            tipo_id=self.combo_tipo.currentData(),
            categoria_codigo=self.combo_categoria.currentData(ROL_CODIGO_RUBA),
            fecha=self.campo_fecha.value(),
            hora_llamado=self.campo_hora_llamado.value(),
            hora_toque=self.campo_hora_toque.value(),
            calle_altura=self.entry_calle.text().strip() or None,
            localidad=self.entry_localidad.text().strip() or "Adelia María",
            zona="Urbana" if self.radio_urbana.isChecked() else "Rural",
            denunciante_nombre=self.entry_den_nombre.text().strip() or None,
            denunciante_apellido=self.entry_den_apellido.text().strip() or None,
            denunciante_dni=self.entry_den_dni.text().strip() or None,
            denunciante_telefono=self._telefono_denunciante(),
            descripcion=self.texto_resena.toPlainText().strip() or None,
            civiles_heridos=conteos["heridos"],
            civiles_fallecidos=conteos["fallecidos"],
            civiles_desaparecidos=conteos["desaparecidos"],
            seguro_compania=self.entry_seguro_compania.text().strip() or None,
            seguro_poliza=self.entry_seguro_poliza.text().strip() or None,
            datos_especificos=self._datos_especificos() or None,
            latitud=self.mapa.obtener_datos()["latitud"],
            longitud=self.mapa.obtener_datos()["longitud"],
            damnificados=self.panel_damnificados.civiles(),
            bienes=self.panel_damnificados.bienes(),
            bomberos_damnificados=[
                {"persona": personas[b["id_ruba"]], "detalle": b["detalle"]}
                for b in bomberos_damnificados_ui if b["id_ruba"] in personas
            ],
            fecha_salida=horario.fecha_salida, hora_salida=horario.hora_salida,
            fecha_llegada=horario.fecha_llegada, hora_llegada=horario.hora_llegada,
            bomberos_lesionados=conteos["bomberos"],
            vehiculos=vehiculos,
            bomberos=bomberos,
        )
        return construir_payload(datos)

    @staticmethod
    def _limpiar_para_archivo(texto: str) -> str:
        """Deja un string apto para nombre de archivo/carpeta en Windows
        (misma regla que `app.reports.excel_generator._limpiar_para_archivo`)."""
        limpio = texto.replace("/", "-").replace("\\", "-").strip()
        for caracter in '<>:"|?*':
            limpio = limpio.replace(caracter, "")
        return limpio.replace(" ", "_")

    def _copiar_firma_auditoria(
        self, session, numero_parte: str, fecha: Optional[date], numero_dotacion: int, jefe_personal_id: int,
    ) -> Optional[str]:
        """Copia la firma ya registrada en el legajo del Jefe de Dotación a
        data/firmas/{AÑO}/{NUMERO_PARTE_LIMPIO}_DOTACION_{N}_JEFE.png, como
        copia de auditoría de esta intervención puntual (Fase 8). Devuelve
        None (sin cortar el guardado) si esa persona no tiene firma
        registrada -- no debería pasar porque _firmar_intervencion() ya lo
        valida antes de dejar firmar, pero por las dudas no revienta acá."""
        persona = session.get(Personal, jefe_personal_id)
        if persona is None or not persona.ruta_firma or not Path(persona.ruta_firma).exists():
            return None

        anio = fecha.year if fecha else datetime.now().year
        numero_limpio = self._limpiar_para_archivo(numero_parte)
        carpeta = DATA_DIR / "firmas" / str(anio)
        carpeta.mkdir(parents=True, exist_ok=True)
        destino = carpeta / f"{numero_limpio}_DOTACION_{numero_dotacion}_JEFE.png"
        shutil.copy(persona.ruta_firma, destino)
        return str(destino)

    def _guardar_y_generar_planillas(self) -> None:
        """"💾 Guardar e Imprimir Planillas": validar, guardar en la base local
        (estado RUBA = PENDIENTE) y generar/abrir las planillas PCS y PCD2 (PDF
        si hay Excel en la máquina). La carga en RUBA ya no se hace desde el
        formulario: se hace en lote desde el Historial de Salidas."""
        resultado = self._persistir_incidente()
        if resultado is None:
            return
        incidente_id, numero_parte = resultado

        generadas, problemas = self._generar_planillas(incidente_id)
        resumen = f"Siniestro N° {numero_parte} guardado en el histórico local."
        if generadas:
            resumen += "\n\nPlanillas generadas:\n" + "\n".join(generadas)
        if problemas:
            resumen += "\n\nProblemas al generar planillas:\n" + "\n".join(problemas)

        resumen += ("\n\nQuedó PENDIENTE de carga en RUBA: cargalo desde Estadísticas "
                    "(🚀 Cargar Seleccionados a RUBA).")
        (QMessageBox.warning if problemas else QMessageBox.information)(self, "Siniestro guardado", resumen)

        self._limpiar_formulario()
        self._actualizar_dashboard()

    def _guardar_borrador(self) -> None:
        """⏱️ Salida en Curso: guarda sin exigir regreso ni datos de cierre y
        vuelve a Inicio, donde el servicio queda destacado con su cronómetro."""
        resultado = self._persistir_incidente(EstadoOperativo.EN_CURSO.value)
        if resultado is None:
            return
        _, numero_parte = resultado
        self._limpiar_formulario()
        self._actualizar_dashboard()
        self._ir_a_pagina(IDX_DASHBOARD)
        theme.set_tono(self.statusBar(), "neutro")
        self.statusBar().showMessage(
            f"Servicio N° {numero_parte} guardado EN CURSO. Retomalo desde Inicio para registrar el regreso.", 10000
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - override de Qt
        """Si hay una actualización descargada y verificada, se instala al
        cerrar (el instalador espera a que este proceso termine)."""
        if self.controlador_actualizaciones is not None:
            self.controlador_actualizaciones.al_cerrar()
        super().closeEvent(event)

    # -- Editar / eliminar desde el Historial ----------------------------------

    def _incidente_en_sincronizacion(self, incidente_id: int) -> bool:
        """True si ese servicio se está cargando en RUBA o espera en la cola."""
        return self._lote_ruba is not None and incidente_id in self._lote_ruba[1].incidente_ids

    def carga_ruba_en_curso(self) -> bool:
        """True mientras corre un lote de carga a RUBA (lo consulta, p. ej., el
        auto-updater antes de cerrar la app para instalar)."""
        return self._lote_ruba is not None

    def _formulario_con_datos_sin_guardar(self) -> bool:
        """Heurística: el formulario tiene un servicio NUEVO a medio cargar."""
        if self._incidente_en_edicion is not None:
            return False
        return bool(self.entry_calle.text().strip() or self.entry_den_apellido.text().strip()
                    or self.texto_resena.toPlainText().strip() or self.panel_dotaciones.datos())

    def _confirmar_descartar_formulario(self, incidente_id: int) -> bool:
        if self._incidente_en_edicion is not None and self._incidente_en_edicion != incidente_id:
            texto = f"El formulario tiene abierto el servicio N° {self.label_numero_parte.text()}. " \
                    "Se descartan los cambios que no guardaste. ¿Continuar?"
        elif self._formulario_con_datos_sin_guardar():
            texto = "El formulario tiene un servicio nuevo sin guardar. Si seguís, se descarta. ¿Continuar?"
        else:
            return True
        respuesta = QMessageBox.question(
            self, "Descartar cambios", texto,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        return respuesta == QMessageBox.StandardButton.Yes

    def editar_servicio(self, incidente_id: int) -> None:
        """Carga un servicio del Historial en el formulario en MODO EDICIÓN:
        al guardar se actualiza ese mismo registro (mismo N° de parte y mismo
        ID de RUBA, si RUBA ya lo había creado) en vez de crear uno nuevo, y
        se puede reintentar la carga en RUBA con los datos corregidos."""
        if self._incidente_en_sincronizacion(incidente_id):
            QMessageBox.information(self, "Sincronización en curso",
                                    "Ese servicio se está cargando en RUBA. Esperá a que termine para editarlo.")
            return
        with get_session() as session:
            incidente = session.get(Incidente, incidente_id)
            if incidente is None:
                self._pagina_historial.refrescar()
                return
            if incidente.en_curso:
                en_curso = True
            else:
                en_curso = False
                numero_parte = incidente.numero_parte
                sincronizado = incidente.estado_ruba == EstadoRuba.SINCRONIZADO.value
                datos = self._leer_incidente_para_formulario(incidente)
        if en_curso:
            self.continuar_servicio_en_curso(incidente_id)
            return
        if sincronizado:
            QMessageBox.information(self, f"Parte N° {numero_parte} cerrado", MOTIVO_PARTE_CERRADO)
            return
        if not self._confirmar_descartar_formulario(incidente_id):
            return
        self._limpiar_formulario()
        self._cargar_formulario(datos)
        self._incidente_en_edicion = incidente_id
        self._edicion_de_cerrado = True
        self._edicion_de_sincronizado = sincronizado
        self._ir_a_pagina(IDX_FORMULARIO)
        self._actualizar_chips()
        theme.set_tono(self.statusBar(), "neutro")
        self.statusBar().showMessage(
            f"Editando el Parte N° {numero_parte}: al guardar se actualiza ese mismo registro.", 10000
        )

    def eliminar_servicio(self, incidente_id: int) -> None:
        """Borra un servicio NO sincronizado (y en cascada sus unidades,
        dotación, personal de base y damnificados). Uno ya cargado en RUBA
        no se borra: existe en el portal nacional."""
        with get_session() as session:
            incidente = session.get(Incidente, incidente_id)
            if incidente is None:
                self._pagina_historial.refrescar()
                return
            numero_parte = incidente.numero_parte
            estado_ruba = incidente.estado_ruba
            ruba_id_remoto = incidente.ruba_id_remoto

        if estado_ruba == EstadoRuba.SINCRONIZADO.value:
            QMessageBox.warning(
                self, "No se puede eliminar",
                f"El Parte N° {numero_parte} ya está sincronizado con RUBA (ID {ruba_id_remoto or '—'}): "
                "existe en el portal nacional y no se puede eliminar desde Fire Station.",
            )
            return
        if self._incidente_en_sincronizacion(incidente_id):
            QMessageBox.information(self, "Sincronización en curso",
                                    "Ese servicio se está cargando en RUBA. Esperá a que termine para eliminarlo.")
            return

        texto = f"¿Está seguro de que desea eliminar el Parte N° {numero_parte}? Esta acción no se puede deshacer."
        if ruba_id_remoto:
            texto += (f"\n\nAtención: una carga anterior llegó a crear el incidente en RUBA (ID {ruba_id_remoto}) "
                      "sin terminarla. Eliminarlo acá NO lo borra del portal: revisalo en RUBA.")
        if self._incidente_en_edicion == incidente_id:
            texto += "\n\nEste servicio está abierto en el formulario: se va a cerrar sin guardar."
        respuesta = QMessageBox.question(
            self, "Eliminar servicio", texto,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return

        try:
            with get_session() as session:
                incidente = session.get(Incidente, incidente_id)
                if incidente is not None:
                    session.delete(incidente)  # cascade ORM: unidades, dotación, base, damnificados
        except Exception as e:  # noqa: BLE001 - se informa, la base queda como estaba (rollback)
            QMessageBox.critical(self, "No se pudo eliminar", f"No se pudo eliminar el Parte N° {numero_parte}:\n{e}")
            return

        if self._incidente_en_edicion == incidente_id:
            self._limpiar_formulario()
        self._pagina_historial.refrescar()
        self._actualizar_dashboard()
        theme.set_tono(self.statusBar(), "neutro")
        self.statusBar().showMessage(f"Parte N° {numero_parte} eliminado del historial local.", 10000)

    def continuar_servicio_en_curso(self, incidente_id: int) -> None:
        """Abre el formulario con el servicio en curso precargado."""
        with get_session() as session:
            incidente = session.get(Incidente, incidente_id)
            if incidente is None or not incidente.en_curso:
                self._actualizar_dashboard()
                return
            datos = self._leer_incidente_para_formulario(incidente)
        if not self._confirmar_descartar_formulario(incidente_id):
            return
        self._limpiar_formulario()
        self._cargar_formulario(datos)
        self._incidente_en_edicion = incidente_id
        self._ir_a_pagina(IDX_FORMULARIO)
        self._actualizar_chips()

    @staticmethod
    def _leer_incidente_para_formulario(inc: Incidente) -> Dict[str, Any]:
        """Todo lo que hace falta para reponer el formulario, en tipos planos
        (se lee con la sesión abierta y se carga después)."""
        return {
            "numero_parte": inc.numero_parte, "tipo_id": inc.tipo_incidente_id, "categoria_id": inc.categoria_id,
            "fecha": inc.fecha, "hora_llamado": inc.hora_llamado, "hora_toque": inc.hora_toque,
            "calle_altura": inc.calle_altura, "localidad": inc.localidad, "zona": inc.zona,
            "referencia_ubicacion": inc.referencia_ubicacion,
            "denunciante": (inc.denunciante_nombre, inc.denunciante_apellido, inc.denunciante_telefono, inc.denunciante_dni),
            "via_comunicacion": inc.via_comunicacion,
            "contacto_detalle": inc.contacto_detalle, "denunciante_domicilio": inc.denunciante_domicilio,
            "recibio": inc.recibio.id_ruba if inc.recibio else None,
            "autorizo": inc.autorizo.id_ruba if inc.autorizo else None,
            "alarma_general": inc.alarma_general,
            "base": {
                "operador_1": next((b.personal.id_ruba for b in inc.personal_base
                                    if b.funcion == FuncionBase.OPERADOR_1.value), None),
                "operador_2": next((b.personal.id_ruba for b in inc.personal_base
                                    if b.funcion == FuncionBase.OPERADOR_2.value), None),
                "apresto": [b.personal.id_ruba for b in inc.personal_base if b.funcion == FuncionBase.APRESTO.value],
            },
            "mapa": (inc.latitud, inc.longitud, inc.superficie_ha, inc.geometria_geojson),
            "ruta_imagen_mapa": inc.ruta_imagen_mapa,
            "datos_especificos": inc.datos_especificos,
            "seguro": (inc.seguro_compania, inc.seguro_poliza), "resena": inc.resena_operativa,
            "horario": HorarioServicio(inc.fecha_salida, inc.hora_salida, inc.fecha_llegada, inc.hora_regreso),
            "civiles": [
                {"nombre": d.nombre, "apellido": d.apellido, "dni": d.dni, "genero": d.genero,
                 "lesion": d.lesion, "condicion": d.condicion}
                for d in inc.damnificados_civiles
            ],
            "bienes": [
                {"tipo": b.tipo, "descripcion": b.descripcion, "titular": b.titular, "seguro": b.seguro}
                for b in inc.bienes_afectados
            ],
            "bomberos_damnificados": [
                {"id_ruba": b.personal.id_ruba, "detalle": b.detalle_atencion} for b in inc.bomberos_damnificados
            ],
            "unidades": [
                {
                    "movil_id_ruba": su.movil.id_ruba if su.movil else None,
                    "chofer_id_ruba": su.chofer.id_ruba if su.chofer else None,
                    "jefe_id_ruba": next((f.personal.id_ruba for f in su.dotacion
                                          if f.rol == RolDotacion.A_CARGO.value), None),
                    "fecha_salida": su.fecha_salida, "hora_salida": su.hora_salida, "hora_arribo": su.hora_arribo,
                    "fecha_llegada": su.fecha_llegada, "hora_llegada": su.hora_regreso,
                    "firmada": bool(su.ruta_firma_auditoria), "firma_validada_en": su.firma_validada_en,
                    "bomberos": [
                        {"id_ruba": f.personal.id_ruba}
                        for f in sorted(su.dotacion, key=lambda f: f.id) if f.rol == RolDotacion.BOMBERO.value
                    ],
                }
                for su in inc.salidas_unidad
            ],
        }

    def _cargar_formulario(self, d: Dict[str, Any]) -> None:
        self.label_numero_parte.setText(d["numero_parte"])
        if d["fecha"]:
            self.campo_fecha.set_value(d["fecha"])
        if d["tipo_id"] is not None:
            self.combo_tipo.setCurrentIndex(max(0, self.combo_tipo.findData(d["tipo_id"])))
            self._on_tipo_cambiado()
        if d["categoria_id"] is not None:
            self.combo_categoria.setCurrentIndex(max(0, self.combo_categoria.findData(d["categoria_id"])))
        self.panel_datos_especificos.cargar(d["datos_especificos"])
        self.campo_hora_llamado.set_value(d["hora_llamado"])
        self.campo_hora_toque.set_value(d["hora_toque"])

        nombre, apellido, telefono, dni = d["denunciante"]
        for campo, valor in ((self.entry_den_nombre, nombre), (self.entry_den_apellido, apellido),
                             (self.entry_den_dni, dni)):
            campo.setText(valor or "")
        medio = MEDIOS_ANTERIORES.get(d["via_comunicacion"], d["via_comunicacion"])
        indice = self.combo_via_comunicacion.findText(medio or "")
        if indice >= 0:
            self.combo_via_comunicacion.setCurrentIndex(indice)
        detalle = d["contacto_detalle"]
        if not detalle and telefono:  # servicios cargados con el campo Teléfono aparte
            detalle = telefono
            if self.combo_via_comunicacion.currentText() not in MEDIOS_TELEFONICOS:
                self.combo_via_comunicacion.setCurrentText("Teléfono")
        self.entry_contacto_detalle.setText(detalle or "")
        self.entry_den_domicilio.setText(d["denunciante_domicilio"] or "")
        self.selector_recibio.set_id_ruba(d["recibio"])
        self.selector_autorizo.set_id_ruba(d["autorizo"])
        if d["alarma_general"] is not None:
            (self.radio_alarma_si if d["alarma_general"] else self.radio_alarma_no).setChecked(True)

        self.entry_calle.setText(d["calle_altura"] or "")
        self.entry_localidad.setText(d["localidad"] or "Adelia María")
        self.entry_referencia.setText(d["referencia_ubicacion"] or "")
        (self.radio_rural if d["zona"] == "Rural" else self.radio_urbana).setChecked(True)
        self.mapa.cargar_incidente(d["numero_parte"], *d["mapa"])
        self._ruta_imagen_mapa = d.get("ruta_imagen_mapa")
        self._coords_imagen_mapa = (d["mapa"][0], d["mapa"][1]) if self._ruta_imagen_mapa else None
        self._actualizar_label_imagen_mapa()

        self.panel_damnificados.cargar(d["civiles"], d["bienes"], d["bomberos_damnificados"],
                                       (d["datos_especificos"] or {}).get("vehiculos"))
        compania, poliza = d["seguro"]
        self.entry_seguro_compania.setText(compania or "")
        self.entry_seguro_poliza.setText(poliza or "")
        self.texto_resena.setPlainText(d["resena"] or "")

        horario: HorarioServicio = d["horario"]
        if horario.fecha_salida:
            self.campo_fecha_salida.set_value(horario.fecha_salida)
        if horario.fecha_llegada:
            self.campo_fecha_llegada.set_value(horario.fecha_llegada)
        self.campo_hora_salida.set_value(horario.hora_salida)
        self.campo_hora_llegada.set_value(horario.hora_llegada)
        self._horario_manual.clear()
        if horario.hora_salida is not None and horario.hora_salida not in (d["hora_llamado"], d["hora_toque"]):
            self._horario_manual.add("hora_salida")  # se había ajustado a mano: se respeta
        self.panel_dotaciones.cargar(d["unidades"], self._horario_general())
        self.panel_base.cargar(d["base"])

    def _generar_planillas(self, incidente_id: int) -> Tuple[List[str], List[str]]:
        """PCS y PCD2 (exportadas a PDF con Excel si está disponible, que
        tarda unos segundos: se muestra el cursor de espera)."""
        generadas: List[str] = []
        problemas: List[str] = []
        self.statusBar().showMessage("Generando planillas PCS y PCD2…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            for generar_func, nombre_planilla in (
                (generar_e_imprimir_pcs, "PCS"), (generar_e_imprimir_pcd2, "PCD2"),
            ):
                try:
                    ruta = generar_func(incidente_id)
                    generadas.append(f"{nombre_planilla}: {ruta}")
                    aviso = resumen_advertencias(nombre_planilla, ruta)
                    if aviso:
                        problemas.append(aviso)
                except FileNotFoundError as e:
                    problemas.append(f"{nombre_planilla}: falta la plantilla ({e})")
                except Exception as e:  # noqa: BLE001 - error inesperado generando/abriendo la planilla
                    problemas.append(f"{nombre_planilla}: {e}")
        finally:
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
        return generadas, problemas

    # -- Limpiar / valores por defecto ------------------------------------------

    def _limpiar_formulario(self) -> None:
        self._incidente_en_edicion = None  # lo próximo que se guarde es un servicio nuevo
        self._edicion_de_cerrado = False
        self._edicion_de_sincronizado = False
        with get_session() as session:
            self.label_numero_parte.setText(siguiente_numero_parte(session))

        self.campo_fecha.set_hoy()
        self.combo_tipo.setCurrentIndex(0 if self.combo_tipo.count() else -1)
        self._on_tipo_cambiado()

        self.campo_hora_llamado.clear()
        self.campo_hora_toque.clear()

        self.entry_calle.clear()
        self.entry_localidad.setText("Adelia María")
        self.entry_referencia.clear()
        self.radio_urbana.setChecked(True)

        self.entry_den_nombre.clear()
        self.entry_den_apellido.clear()
        self.entry_den_dni.clear()
        self.entry_den_domicilio.clear()
        self.combo_via_comunicacion.setCurrentIndex(0)
        self.entry_contacto_detalle.clear()
        self.selector_recibio.clear()
        self.selector_autorizo.clear()
        self.grupo_alarma.setExclusive(False)
        self.radio_alarma_si.setChecked(False)
        self.radio_alarma_no.setChecked(False)
        self.grupo_alarma.setExclusive(True)

        hoy = date.today()
        self.campo_fecha_salida.set_value(hoy)
        self.campo_fecha_llegada.set_value(hoy)
        self.campo_hora_salida.clear()
        self.campo_hora_llegada.clear()
        self._horario_manual.clear()
        self.panel_dotaciones.limpiar()
        self.panel_dotaciones.set_horario_general(self._horario_general())
        self.panel_base.limpiar()

        self.panel_damnificados.limpiar()
        self.panel_datos_especificos.limpiar()
        self.entry_seguro_compania.clear()
        self.entry_seguro_poliza.clear()
        self.texto_resena.clear()

        self.mapa.limpiar()
        self._ruta_imagen_mapa = None
        self._coords_imagen_mapa = None
        self._actualizar_label_imagen_mapa()
        for campo in (self.combo_tipo, self.combo_categoria, self.entry_calle):
            theme.marcar_invalido(campo, False)
        self._actualizar_chips()

        self.entry_calle.setFocus()

    # -- Sincronización con RUBA en segundo plano (Fase 3) ----------------------

    def _cargar_lote_ruba(self, incidente_ids: List[int]) -> None:
        """Carga secuencial en RUBA (Historial -> 🚀, ↻ RUBA de una fila, o el
        aviso de pendientes al arrancar). Un QThread con UN navegador para
        todo el lote; diálogo modal con progreso "k de N" y Cancelar."""
        if not incidente_ids:
            return
        if self._lote_ruba is not None:
            QMessageBox.information(self, "Carga en RUBA en curso",
                                    "Ya hay una carga en RUBA en curso. Esperá a que termine.")
            return
        dialogo = DialogoLoteRuba(len(incidente_ids), self)

        def conectar(worker: RubaLoteWorker) -> None:
            # Slots de QObjects del hilo de la UI (nunca lambdas sueltas).
            dialogo.conectar(worker)
            worker.item_ok.connect(self._on_lote_item_ok)
            worker.item_error.connect(self._on_lote_item_error)
            worker.terminado.connect(self._on_lote_terminado)

        hilo, worker = lanzar_lote(list(incidente_ids), conectar)
        self._lote_ruba = (hilo, worker, dialogo)
        hilo.finished.connect(self._olvidar_lote_ruba)
        self._pagina_historial.set_carga_en_curso(True)
        theme.set_tono(self.statusBar(), "neutro")
        self.statusBar().showMessage(f"Cargando {len(incidente_ids)} parte(s) en RUBA…")
        self._set_chip_ruba("cargando…", "info", f"{len(incidente_ids)} parte(s) en cola")
        dialogo.show()  # modal (ApplicationModal) sin bloquear el event loop: la carga sigue en su hilo

    def _on_lote_item_ok(self, incidente_id: int, numero_parte: str, ruba_id_remoto: str, url_final: str) -> None:
        self.statusBar().showMessage(f"Siniestro N° {numero_parte} cargado en RUBA (ID {ruba_id_remoto or '—'}).")
        self._pagina_historial.refrescar()

    def _on_lote_item_error(self, incidente_id: int, numero_parte: str, mensaje_error: str, captura: str) -> None:
        # El detalle completo ya quedó en incidentes.ruba_error_log (y la captura
        # en logs/screenshots/); acá solo se deja constancia en consola.
        print(f"[RUBA] Falló la carga del siniestro {numero_parte}: {mensaje_error} {captura}")
        self._pagina_historial.refrescar()

    def _on_lote_terminado(self, ok: int, errores: int, omitidos: int, sin_procesar: int) -> None:
        tono = "error" if errores else ("alerta" if sin_procesar else "ok")
        theme.set_tono(self.statusBar(), tono)
        texto = f"RUBA: {ok} cargado(s), {errores} con error, {omitidos} omitido(s)"
        if sin_procesar:
            texto += f", {sin_procesar} sin procesar (cancelado)"
        self.statusBar().showMessage(texto + ".", 15000)
        self._set_chip_ruba("error de carga" if errores else "sincronizado", "error" if errores else "ok", texto)

    def _olvidar_lote_ruba(self) -> None:
        self._lote_ruba = None
        self._pagina_historial.set_carga_en_curso(False)
        self._pagina_historial.refrescar()
        self._actualizar_dashboard()

    # -- Página: Personal y Unidades (CRUD completo: alta, edición, baja y eliminación) --

    def _crear_pagina_dotaciones(self) -> QWidget:
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(12)

        fila_botones = QHBoxLayout()
        boton_nuevo_bombero = QPushButton("+ Nuevo Bombero", pagina)
        boton_nuevo_bombero.setObjectName("botonAhora")
        boton_nuevo_bombero.clicked.connect(lambda: self._dialogo_bombero())
        boton_nueva_unidad = QPushButton("+ Nueva Unidad", pagina)
        boton_nueva_unidad.setObjectName("botonAhora")
        boton_nueva_unidad.clicked.connect(lambda: self._dialogo_unidad())
        boton_importar_padron = QPushButton("📥 Importar Bomberos desde Excel (RUBA)", pagina)
        boton_importar_padron.setToolTip("Elegí el 'Reporte de bomberos' exportado de RUBA (.xlsx) desde cualquier carpeta")
        boton_importar_padron.clicked.connect(self._importar_padron_bomberos)
        boton_importar_unidades = QPushButton("📥 Importar Unidades desde Excel (RUBA)", pagina)
        boton_importar_unidades.setToolTip(
            "Elegí el 'Reporte de vehiculos' exportado de RUBA (.xlsx) desde cualquier carpeta")
        boton_importar_unidades.clicked.connect(lambda: self._panel_unidades.importar_desde_excel())
        fila_botones.addWidget(boton_nuevo_bombero)
        fila_botones.addWidget(boton_nueva_unidad)
        fila_botones.addWidget(self._crear_boton_refrescar(pagina))
        fila_botones.addStretch(1)
        fila_botones.addWidget(boton_importar_padron)
        fila_botones.addWidget(boton_importar_unidades)
        layout.addLayout(fila_botones)

        # Una tabla a la vez, a todo el ancho (antes iban lado a lado y se
        # cortaban las columnas): selector "Personal | Unidades / Móviles".
        fila_selector = QHBoxLayout()
        fila_selector.setSpacing(0)
        self._grupo_vista_dotaciones = QButtonGroup(pagina)
        self._grupo_vista_dotaciones.setExclusive(True)
        for indice, texto in enumerate(("👥  Personal", "🚒  Unidades / Móviles")):
            boton = QPushButton(texto, pagina)
            boton.setCheckable(True)
            boton.setObjectName("toggleVista")
            boton.setCursor(Qt.CursorShape.PointingHandCursor)
            boton.setMinimumWidth(190)
            self._grupo_vista_dotaciones.addButton(boton, indice)
            fila_selector.addWidget(boton)
        fila_selector.addSpacing(16)
        self._entry_buscar_dotaciones = QLineEdit(pagina)
        self._entry_buscar_dotaciones.setPlaceholderText("Buscar por nombre, legajo, DNI o móvil...")
        self._entry_buscar_dotaciones.setClearButtonEnabled(True)
        self._entry_buscar_dotaciones.textChanged.connect(self._filtrar_dotaciones)
        fila_selector.addWidget(self._entry_buscar_dotaciones, 1)
        layout.addLayout(fila_selector)

        self._stack_dotaciones = QStackedWidget(pagina)

        caja_personal = QGroupBox("Personal")
        layout_personal = QVBoxLayout(caja_personal)
        self._tabla_personal_dotacion = QTableWidget(caja_personal)
        self._tabla_personal_dotacion.setColumnCount(len(COLUMNAS_PERSONAL))
        self._tabla_personal_dotacion.setHorizontalHeaderLabels(COLUMNAS_PERSONAL)
        self._tabla_personal_dotacion.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tabla_personal_dotacion.verticalHeader().setVisible(False)
        cabecera = self._tabla_personal_dotacion.horizontalHeader()
        cabecera.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cabecera.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        cabecera.setSectionResizeMode(COL_PERSONAL_ACCIONES, QHeaderView.ResizeMode.Fixed)
        self._tabla_personal_dotacion.setColumnWidth(COL_PERSONAL_ACCIONES, 270)
        theme.estilizar_tabla(self._tabla_personal_dotacion)
        layout_personal.addWidget(self._tabla_personal_dotacion)
        self._stack_dotaciones.addWidget(caja_personal)

        # Tabla de unidades + importador del 'Reporte de vehiculos' (app/ui/unidades_view.py).
        self._panel_unidades = PanelUnidades(self._acciones_movil, pagina)
        self._panel_unidades.unidades_cambiadas.connect(self._refrescar_moviles_despacho)
        self._panel_unidades.recargada.connect(self._filtrar_dotaciones)
        self._stack_dotaciones.addWidget(self._panel_unidades)

        self._grupo_vista_dotaciones.idClicked.connect(self._stack_dotaciones.setCurrentIndex)
        self._grupo_vista_dotaciones.button(0).setChecked(True)
        layout.addWidget(self._stack_dotaciones, 1)
        return pagina

    def _filtrar_dotaciones(self, *_args) -> None:
        """Filtro en tiempo real sobre las dos tablas (la visible y la otra,
        así al cambiar de vista el filtro ya está aplicado)."""
        texto = self._entry_buscar_dotaciones.text()
        filtrar_tabla(self._tabla_personal_dotacion, texto, COL_PERSONAL_ACCIONES)
        filtrar_tabla(self._panel_unidades.tabla, texto, self._panel_unidades.col_acciones)

    def _crear_widget_acciones_tabla(self, etiqueta_editar: str, fn_editar, etiqueta_estado: str, fn_estado,
                                     fn_eliminar=None) -> QWidget:
        contenedor = QWidget()
        fila = QHBoxLayout(contenedor)
        fila.setContentsMargins(2, 2, 2, 2)
        fila.setSpacing(4)
        boton_editar = QPushButton(etiqueta_editar, contenedor)
        boton_editar.clicked.connect(fn_editar)
        boton_estado = QPushButton(etiqueta_estado, contenedor)
        boton_estado.clicked.connect(fn_estado)
        fila.addWidget(boton_editar)
        fila.addWidget(boton_estado)
        if fn_eliminar is not None:
            boton_eliminar = QPushButton("Eliminar", contenedor)
            boton_eliminar.setObjectName("botonQuitarFila")
            boton_eliminar.clicked.connect(fn_eliminar)
            fila.addWidget(boton_eliminar)
        return contenedor

    def _cargar_pagina_dotaciones(self) -> None:
        self._panel_unidades.recargar()
        self._refrescar_moviles_despacho()
        self._refrescar_padron_despacho()
        with get_session() as session:
            personal = session.query(Personal).order_by(Personal.apellido, Personal.nombre).all()
            datos_personal = [(p.id, p.nombre_completo(), p.legajo or "—", p.dni or "—", p.jerarquia or "—",
                               p.activo) for p in personal]

        self._tabla_personal_dotacion.setRowCount(len(datos_personal))
        for fila, (personal_id, nombre, legajo, dni, jerarquia, activo) in enumerate(datos_personal):
            self._tabla_personal_dotacion.setItem(fila, 0, QTableWidgetItem(nombre))
            for columna, valor in ((1, legajo), (2, dni)):
                item = QTableWidgetItem(valor)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._tabla_personal_dotacion.setItem(fila, columna, item)
            self._tabla_personal_dotacion.setItem(fila, 3, QTableWidgetItem(jerarquia))
            item_estado = QTableWidgetItem("Activo" if activo else "Inactivo")
            item_estado.setForeground(QColor(theme.color("verde_texto" if activo else "ambar")))
            self._tabla_personal_dotacion.setItem(fila, 4, item_estado)
            widget = self._crear_widget_acciones_tabla(
                "Editar", lambda _=False, pid=personal_id: self._dialogo_bombero(pid),
                "Dar de baja" if activo else "Reactivar",
                lambda _=False, pid=personal_id: self._alternar_personal(pid),
                lambda _=False, pid=personal_id: self._eliminar_personal(pid),
            )
            self._tabla_personal_dotacion.setCellWidget(fila, COL_PERSONAL_ACCIONES, widget)
        self._tabla_personal_dotacion.resizeRowsToContents()
        self._filtrar_dotaciones()

    def _acciones_movil(self, movil_id: int, activo: bool) -> QWidget:
        return self._crear_widget_acciones_tabla(
            "Editar", lambda _=False, mid=movil_id: self._dialogo_unidad(mid),
            "Dar de baja" if activo else "Reactivar",
            lambda _=False, mid=movil_id: self._alternar_movil(mid),
            lambda _=False, mid=movil_id: self._panel_unidades.eliminar(mid),
        )

    def _importar_padron_bomberos(self) -> None:
        """Diálogo + importación en app/ui/bomberos_view.py; acá solo se
        refrescan la tabla de personal y el chip del padrón."""
        if not importar_bomberos_desde_excel(self):
            return
        # _cargar_pagina_dotaciones -> _refrescar_padron_despacho reparte el
        # padrón nuevo (ya filtrado) a los selectores de la planilla.
        self._cargar_pagina_dotaciones()

    def _alternar_movil(self, movil_id: int) -> None:
        with get_session() as session:
            movil = session.get(Movil, movil_id)
            movil.activo = not movil.activo
            _sincronizar_estado_movil(movil)
        self._cargar_pagina_dotaciones()

    def _alternar_personal(self, personal_id: int) -> None:
        with get_session() as session:
            persona = session.get(Personal, personal_id)
            persona.activo = not persona.activo
            persona.estado = "Activo" if persona.activo else "Baja"
        self._cargar_pagina_dotaciones()

    def _confirmar_eliminacion(self, titulo: str, texto: str) -> bool:
        respuesta = QMessageBox.question(
            self, titulo, texto,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        return respuesta == QMessageBox.StandardButton.Yes

    def _eliminar_personal(self, personal_id: int) -> None:
        """Solo se elimina quien no figura en ningún servicio ni legajo: si
        no, se perdería historia (planillas, RUBA) -> se ofrece la baja."""
        with get_session() as session:
            persona = session.get(Personal, personal_id)
            if persona is None:
                return
            nombre, id_ruba = persona.nombre_completo(), persona.id_ruba
            usos = sum(session.query(modelo).filter(condicion).count() for modelo, condicion in (
                (DotacionSalida, DotacionSalida.personal_id == personal_id),
                (SalidaUnidad, SalidaUnidad.chofer_personal_id == personal_id),
                (Incidente, (Incidente.recibio_personal_id == personal_id)
                 | (Incidente.autorizo_personal_id == personal_id)),
                (PersonalBase, PersonalBase.personal_id == personal_id),
                (BomberoDamnificado, BomberoDamnificado.personal_id == personal_id),
                (DocumentoPersonal, DocumentoPersonal.personal_id == personal_id),
            ))
        if usos:
            QMessageBox.warning(
                self, "No se puede eliminar",
                f"{nombre} figura en {usos} registro(s) (servicios o documentos del legajo).\n"
                "Para conservar el historial usá \"Dar de baja\".",
            )
            return
        aviso_padron = ("\n\nOjo: figura en el padrón oficial de RUBA, así que se volverá a crear "
                        "al reiniciar la app." if id_ruba is not None else "")
        if not self._confirmar_eliminacion("Eliminar bombero",
                                           f"¿Eliminar definitivamente a {nombre}?{aviso_padron}"):
            return
        with get_session() as session:
            session.delete(session.get(Personal, personal_id))
        self._cargar_pagina_dotaciones()

    @staticmethod
    def _campo_id_ruba(valor: Optional[int], parent: QWidget) -> QLineEdit:
        campo = QLineEdit(str(valor) if valor is not None else "", parent)
        campo.setValidator(QIntValidator(1, 999999999, campo))
        campo.setPlaceholderText("Obligatorio: Id de RUBA (sin él no se sincroniza)")
        return campo

    def _editar_datos_desde_legajo(self, personal_id: int) -> None:
        """"Editar datos personales" en la Ficha de Legajo: mismo diálogo que
        Personal y Unidades; al guardar se refrescan la ficha, la tabla de
        personal y el buscador de legajos (por si cambió el nombre)."""
        if self._dialogo_bombero(personal_id):
            self._legajo_privado.mostrar(personal_id)
            self._cargar_pagina_documentacion()
            theme.set_tono(self.statusBar(), "ok")
            self.statusBar().showMessage("Datos del legajo actualizados.", 6000)

    def _dialogo_bombero(self, personal_id: Optional[int] = None) -> bool:
        """Alta o edición de un bombero. True si se guardaron cambios."""
        datos_previos = None
        if personal_id is not None:
            with get_session() as session:
                p = session.get(Personal, personal_id)
                datos_previos = {
                    "legajo": p.legajo or "", "nombre": p.nombre, "apellido": p.apellido, "dni": p.dni,
                    "telefono": p.telefono or "", "jerarquia": p.jerarquia or "Bombero",
                    "grupo_sanguineo": p.grupo_sanguineo or "", "antiguedad_fecha": p.antiguedad_fecha,
                    "estado": p.estado, "id_ruba": p.id_ruba,
                }

        dialogo = QDialog(self)
        dialogo.setWindowTitle("Editar Bombero" if personal_id else "Nuevo Bombero")
        form = QFormLayout(dialogo)

        entry_id_ruba = self._campo_id_ruba(datos_previos["id_ruba"] if datos_previos else None, dialogo)
        entry_legajo = QLineEdit(datos_previos["legajo"] if datos_previos else "", dialogo)
        entry_legajo.setPlaceholderText("(opcional -- si se deja vacío, se usa el ID interno)")
        entry_nombre = QLineEdit(datos_previos["nombre"] if datos_previos else "", dialogo)
        entry_apellido = QLineEdit(datos_previos["apellido"] if datos_previos else "", dialogo)
        entry_dni = QLineEdit(datos_previos["dni"] if datos_previos else "", dialogo)
        entry_telefono = QLineEdit(datos_previos["telefono"] if datos_previos else "", dialogo)
        entry_jerarquia = QLineEdit(datos_previos["jerarquia"] if datos_previos else "Bombero", dialogo)
        entry_grupo_sanguineo = QLineEdit(datos_previos["grupo_sanguineo"] if datos_previos else "", dialogo)
        entry_grupo_sanguineo.setPlaceholderText("Ej: 0+, A-, AB+…")

        campo_antiguedad = DateField(dialogo)
        if datos_previos and datos_previos["antiguedad_fecha"]:
            campo_antiguedad.set_value(datos_previos["antiguedad_fecha"])

        combo_estado = QComboBox(dialogo)
        combo_estado.addItems(["Activo", "Licencia", "Reserva", "Baja"])
        combo_estado.setCurrentText(datos_previos["estado"] if datos_previos else "Activo")

        form.addRow(theme.etiqueta_requerida("ID RUBA"), entry_id_ruba)
        form.addRow("N° de Legajo", entry_legajo)
        form.addRow("Nombre", entry_nombre)
        form.addRow("Apellido", entry_apellido)
        form.addRow("DNI", entry_dni)
        form.addRow("Teléfono", entry_telefono)
        form.addRow("Jerarquía", entry_jerarquia)
        form.addRow("Grupo Sanguíneo", entry_grupo_sanguineo)
        form.addRow("Antigüedad (ingreso)", campo_antiguedad)
        form.addRow("Estado", combo_estado)

        fila_botones = QHBoxLayout()
        boton_guardar = QPushButton("Guardar", dialogo)
        boton_guardar.setObjectName("botonGuardar")
        boton_cancelar = QPushButton("Cancelar", dialogo)
        boton_guardar.clicked.connect(dialogo.accept)
        boton_cancelar.clicked.connect(dialogo.reject)
        fila_botones.addWidget(boton_cancelar)
        fila_botones.addWidget(boton_guardar)
        form.addRow(fila_botones)

        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return False

        nombre, apellido, dni = entry_nombre.text().strip(), entry_apellido.text().strip(), entry_dni.text().strip()
        if not entry_id_ruba.text().strip():
            QMessageBox.warning(self, "Falta el ID RUBA",
                                "El ID RUBA es obligatorio: sin él el bombero no se puede sincronizar con RUBA.")
            return False
        if not nombre or not apellido or not dni:
            QMessageBox.warning(self, "Datos incompletos", "Nombre, Apellido y DNI son obligatorios.")
            return False
        id_ruba = int(entry_id_ruba.text())
        with get_session() as session:
            duplicado = session.query(Personal).filter(Personal.id_ruba == id_ruba, Personal.id != personal_id).first()
            if duplicado is not None:
                QMessageBox.warning(self, "ID RUBA duplicado",
                                    f"El ID RUBA {id_ruba} ya es de {duplicado.nombre_completo()}.")
                return False

        estado = combo_estado.currentText()
        with get_session() as session:
            persona = session.get(Personal, personal_id) if personal_id is not None else Personal(dni=dni)
            persona.id_ruba = id_ruba
            persona.legajo = entry_legajo.text().strip() or None
            persona.nombre = nombre
            persona.apellido = apellido
            persona.dni = dni
            persona.telefono = entry_telefono.text().strip() or None
            persona.jerarquia = entry_jerarquia.text().strip() or None
            persona.grupo_sanguineo = entry_grupo_sanguineo.text().strip() or None
            persona.antiguedad_fecha = campo_antiguedad.value()
            persona.estado = estado
            persona.activo = estado == "Activo"
            if personal_id is None:
                session.add(persona)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                QMessageBox.warning(
                    self, "Datos duplicados",
                    "Ya existe un bombero cargado con ese DNI o ese N° de Legajo.",
                )
                return False

        self._cargar_pagina_dotaciones()
        return True

    def _dialogo_unidad(self, movil_id: Optional[int] = None) -> None:
        """Alta / edición de una unidad con todos los datos del 'Reporte de
        vehiculos' de RUBA: ID RUBA, identificador, Nº Móvil, tipo, marca,
        modelo, año y dominio."""
        previo: Dict[str, Any] = {}
        with get_session() as session:
            if movil_id is not None:
                m = session.get(Movil, movil_id)
                previo = {"nombre": m.nombre_identificador, "activo": m.activo, "id_ruba": m.id_ruba,
                          "numero_movil": m.numero_movil, "tipo": m.tipo, "marca": m.marca,
                          "modelo": m.modelo, "anio": m.anio, "dominio": m.dominio}
            tipos_usados = [t for (t,) in session.query(Movil.tipo).distinct() if t]
            marcas_usadas = [x for (x,) in session.query(Movil.marca).distinct() if x]

        dialogo = QDialog(self)
        dialogo.setWindowTitle("Editar Unidad" if movil_id else "Nueva Unidad")
        dialogo.setMinimumWidth(460)
        form = QFormLayout(dialogo)

        def combo_sugerencias(sugeridos: List[str], actual: Optional[str]) -> QComboBox:
            combo = QComboBox(dialogo)
            combo.setEditable(True)  # sugerencias, pero se puede escribir cualquier valor
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.addItems(sorted(set(sugeridos), key=str.lower))
            combo.setEditText(actual or "")
            return combo

        entry_id_ruba = self._campo_id_ruba(previo.get("id_ruba"), dialogo)
        entry_nombre = QLineEdit(previo.get("nombre") or "", dialogo)
        entry_nombre.setPlaceholderText("Ej: Móvil 24, Rojo 24")
        entry_numero = QLineEdit(previo.get("numero_movil") or "", dialogo)
        entry_numero.setPlaceholderText("Como figura en RUBA, ej: Rojo 24")
        entry_numero.setToolTip("Nº Móvil del Reporte de vehiculos: con él la carga en RUBA elige el vehículo")
        combo_tipo = combo_sugerencias(TIPOS_UNIDAD_SUGERIDOS + tipos_usados, previo.get("tipo"))
        combo_marca = combo_sugerencias(MARCAS_UNIDAD_SUGERIDAS + marcas_usadas, previo.get("marca"))
        entry_modelo = QLineEdit(previo.get("modelo") or "", dialogo)
        entry_modelo.setPlaceholderText("Ej: TGM-13.250, F-100 4x4")
        spin_anio = QSpinBox(dialogo)
        spin_anio.setRange(ANIO_UNIDAD_MINIMO - 1, date.today().year + 1)
        spin_anio.setSpecialValueText("—")  # el mínimo representa "sin año"
        spin_anio.setValue(previo.get("anio") or ANIO_UNIDAD_MINIMO - 1)
        entry_dominio = QLineEdit(previo.get("dominio") or "", dialogo)
        entry_dominio.setPlaceholderText("Ej: AB123CD (opcional)")
        entry_dominio.setMaxLength(15)
        check_activo = QCheckBox("Activa", dialogo)
        check_activo.setChecked(previo.get("activo", True))

        form.addRow(theme.etiqueta_requerida("ID RUBA"), entry_id_ruba)
        form.addRow(theme.etiqueta_requerida("Nombre / Identificador"), entry_nombre)
        form.addRow("Nº Móvil (RUBA)", entry_numero)
        form.addRow("Tipo de unidad", combo_tipo)
        form.addRow("Marca", combo_marca)
        form.addRow("Modelo", entry_modelo)
        form.addRow("Año de fabricación", spin_anio)
        form.addRow("Dominio / Patente", entry_dominio)
        form.addRow("", check_activo)

        fila_botones = QHBoxLayout()
        boton_guardar = QPushButton("Guardar", dialogo)
        boton_guardar.setObjectName("botonGuardar")
        boton_cancelar = QPushButton("Cancelar", dialogo)
        boton_guardar.clicked.connect(dialogo.accept)
        boton_cancelar.clicked.connect(dialogo.reject)
        fila_botones.addWidget(boton_cancelar)
        fila_botones.addWidget(boton_guardar)
        form.addRow(fila_botones)

        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return

        nombre = entry_nombre.text().strip()
        if not entry_id_ruba.text().strip():
            QMessageBox.warning(self, "Falta el ID RUBA",
                                "El ID RUBA es obligatorio: sin él la unidad no se puede sincronizar con RUBA.")
            return
        if not nombre:
            QMessageBox.warning(self, "Datos incompletos", "El nombre/identificador es obligatorio.")
            return
        id_ruba = int(entry_id_ruba.text())
        numero_movil = entry_numero.text().strip() or None
        anio = spin_anio.value() if spin_anio.value() >= ANIO_UNIDAD_MINIMO else None

        with get_session() as session:
            duplicado = session.query(Movil).filter(Movil.id_ruba == id_ruba, Movil.id != movil_id).first()
            if duplicado is not None:
                QMessageBox.warning(self, "ID RUBA duplicado",
                                    f"El ID RUBA {id_ruba} ya es de la unidad {duplicado.nombre_identificador}.")
                return
            if numero_movil:
                otro = next((m for m in session.query(Movil).filter(Movil.id != movil_id, Movil.numero_movil.isnot(None))
                             if clave_movil(m.numero_movil) == clave_movil(numero_movil)), None)
                if otro is not None:
                    QMessageBox.warning(self, "Nº Móvil duplicado",
                                        f"El Nº Móvil {numero_movil} ya es de la unidad {otro.nombre_identificador}.")
                    return
            movil = session.get(Movil, movil_id) if movil_id is not None else Movil(nombre_identificador=nombre)
            movil.id_ruba = id_ruba
            movil.nombre_identificador = nombre
            movil.numero_movil = numero_movil
            movil.tipo = combo_tipo.currentText().strip() or None
            movil.marca = combo_marca.currentText().strip() or None
            movil.modelo = entry_modelo.text().strip() or None
            movil.anio = anio
            movil.dominio = normalizar_dominio(entry_dominio.text())
            movil.activo = check_activo.isChecked()
            _sincronizar_estado_movil(movil)
            if movil_id is None:
                session.add(movil)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                QMessageBox.warning(self, "Nombre duplicado", "Ya existe una unidad con ese nombre/identificador.")
                return

        self._cargar_pagina_dotaciones()
        self._refrescar_moviles_despacho()

    # -- Página: Documentación (legajos con acceso por PIN personal, Fase 8) ------

    def _crear_pagina_documentacion(self) -> QWidget:
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack_documentacion = QStackedWidget(pagina)
        layout.addWidget(self._stack_documentacion, 1)

        self._stack_documentacion.addWidget(self._crear_subpagina_acceso_legajo())

        self._legajo_privado = LegajoPrivadoWidget(pagina)
        self._legajo_privado.volver_solicitado.connect(lambda: self._stack_documentacion.setCurrentIndex(0))
        self._legajo_privado.editar_datos_solicitado.connect(self._editar_datos_desde_legajo)
        self._stack_documentacion.addWidget(self._legajo_privado)

        return pagina

    def _crear_subpagina_acceso_legajo(self) -> QWidget:
        pagina = QWidget()
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(28, 40, 28, 28)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        caja = QGroupBox("Acceso a Legajo", pagina)
        caja.setMaximumWidth(420)
        form = QFormLayout(caja)

        self._combo_legajo_bombero = QComboBox(caja)
        self._combo_legajo_bombero.setEditable(True)
        self._combo_legajo_bombero.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form.addRow("Bombero", self._combo_legajo_bombero)

        self._entry_legajo_pin = QLineEdit(caja)
        self._entry_legajo_pin.setEchoMode(QLineEdit.EchoMode.Password)
        self._entry_legajo_pin.setMaxLength(10)
        self._entry_legajo_pin.returnPressed.connect(self._intentar_ingresar_legajo)
        form.addRow("PIN de Seguridad", self._entry_legajo_pin)

        boton_ingresar = QPushButton("Ingresar a Legajo", caja)
        boton_ingresar.setObjectName("botonGuardar")
        boton_ingresar.clicked.connect(self._intentar_ingresar_legajo)
        form.addRow(boton_ingresar)

        layout.addWidget(caja)
        return pagina

    def _cargar_pagina_documentacion(self) -> None:
        # Volver siempre a la pantalla de acceso: navegar afuera del legajo
        # y volver exige el PIN de nuevo (acceso privado, no queda abierto).
        self._stack_documentacion.setCurrentIndex(0)
        self._entry_legajo_pin.clear()

        with get_session() as session:
            personal = session.query(Personal).order_by(Personal.apellido, Personal.nombre).all()
            datos = [(p.id, p.nombre_completo(), p.pin) for p in personal]

        self._combo_legajo_bombero.blockSignals(True)
        self._combo_legajo_bombero.clear()
        for personal_id, texto, pin in datos:
            self._combo_legajo_bombero.addItem(texto, (personal_id, pin))
        self._combo_legajo_bombero.setCurrentIndex(-1)
        self._combo_legajo_bombero.blockSignals(False)

        completer = QCompleter([texto for _, texto, _ in datos], self._combo_legajo_bombero)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._combo_legajo_bombero.setCompleter(completer)

    def _intentar_ingresar_legajo(self) -> None:
        indice = self._combo_legajo_bombero.currentIndex()
        if indice < 0:
            QMessageBox.warning(self, "Seleccioná un bombero", "Elegí un bombero de la lista antes de ingresar.")
            return

        personal_id, pin_real = self._combo_legajo_bombero.itemData(indice)
        if self._entry_legajo_pin.text() != pin_real:
            QMessageBox.warning(self, "PIN incorrecto", "El PIN de seguridad no es correcto.")
            return

        self._legajo_privado.mostrar(personal_id)
        self._stack_documentacion.setCurrentIndex(1)

    # -- Página: Configuración ------------------------------------------------------

    def _crear_pagina_configuracion(self) -> QWidget:
        pagina = QWidget()
        layout_pagina = QVBoxLayout(pagina)
        layout_pagina.setContentsMargins(0, 0, 0, 0)
        layout_pagina.setSpacing(0)

        scroll = QScrollArea(pagina)
        scroll.setWidgetResizable(True)
        layout_pagina.addWidget(scroll, 1)

        contenido = QWidget()
        layout = QVBoxLayout(contenido)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)
        scroll.setWidget(contenido)

        caja_ruba = QGroupBox("Credenciales RUBA")
        form_ruba = QFormLayout(caja_ruba)
        self._entry_ruba_usuario = QLineEdit(caja_ruba)
        self._entry_ruba_clave = QLineEdit(caja_ruba)
        self._entry_ruba_clave.setEchoMode(QLineEdit.EchoMode.Password)
        self._entry_ruba_url_login = QLineEdit(caja_ruba)
        self._entry_ruba_url_incidentes = QLineEdit(caja_ruba)
        form_ruba.addRow("Usuario", self._entry_ruba_usuario)
        form_ruba.addRow("Clave", self._entry_ruba_clave)
        form_ruba.addRow("URL de login", self._entry_ruba_url_login)
        form_ruba.addRow("URL de incidentes", self._entry_ruba_url_incidentes)

        caja_cuartel = QGroupBox("Datos del Cuartel")
        form_cuartel = QFormLayout(caja_cuartel)
        self._entry_cuartel_nombre = QLineEdit(caja_cuartel)
        self._entry_cuartel_localidad = QLineEdit(caja_cuartel)
        self._entry_cuartel_codigo = QLineEdit(caja_cuartel)
        form_cuartel.addRow("Nombre", self._entry_cuartel_nombre)
        form_cuartel.addRow("Localidad", self._entry_cuartel_localidad)
        form_cuartel.addRow("Código", self._entry_cuartel_codigo)

        caja_rutas = QGroupBox("Rutas (solo lectura)")
        form_rutas = QFormLayout(caja_rutas)
        self._label_ruta_datos = QLabel(caja_rutas)
        self._label_ruta_plantillas = QLabel(caja_rutas)
        self._label_ruta_salida = QLabel(caja_rutas)
        for lbl in (self._label_ruta_datos, self._label_ruta_plantillas, self._label_ruta_salida):
            lbl.setWordWrap(True)
            lbl.setProperty("muted", True)
        form_rutas.addRow("Base de datos / config.json", self._label_ruta_datos)
        form_rutas.addRow("Plantillas PCS / PCD2", self._label_ruta_plantillas)
        form_rutas.addRow("Planillas generadas", self._label_ruta_salida)

        caja_mapa = self._crear_caja_config_mapa()

        caja_apariencia = QGroupBox("Apariencia")
        form_apariencia = QFormLayout(caja_apariencia)
        self._combo_tema = QComboBox(caja_apariencia)
        self._combo_tema.addItem("Oscuro — centro de despacho", "oscuro")
        self._combo_tema.addItem("Claro — institucional", "claro")
        self._combo_tema.setCurrentIndex(max(0, self._combo_tema.findData(theme.tema_guardado())))
        self._combo_tema.currentIndexChanged.connect(self._on_tema_cambiado)
        form_apariencia.addRow("Tema", self._combo_tema)

        caja_acerca = QGroupBox("Acerca de / Actualizaciones")
        form_acerca = QFormLayout(caja_acerca)
        form_acerca.addRow("Versión instalada", QLabel(f"v{__version__}", caja_acerca))
        self._boton_buscar_actualizaciones = QPushButton("🔄 Buscar actualizaciones ahora", caja_acerca)
        self._boton_buscar_actualizaciones.setToolTip(
            "Consulta el último release en GitHub (en desarrollo: origin/main vía git)")
        self._boton_buscar_actualizaciones.clicked.connect(self._buscar_actualizaciones_ahora)
        form_acerca.addRow(self._boton_buscar_actualizaciones)

        boton_guardar_config = QPushButton("Guardar Configuración", contenido)
        boton_guardar_config.setObjectName("botonGuardar")
        boton_guardar_config.clicked.connect(self._guardar_configuracion)

        layout.addWidget(caja_ruba)
        layout.addWidget(caja_cuartel)
        layout.addWidget(caja_rutas)
        layout.addWidget(caja_mapa)
        layout.addWidget(caja_apariencia)
        layout.addWidget(caja_acerca)
        layout.addWidget(self._crear_caja_mantenimiento(contenido))
        layout.addWidget(boton_guardar_config, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)

        return pagina

    def _crear_caja_mantenimiento(self, parent: QWidget) -> QGroupBox:
        caja = QGroupBox("Mantenimiento", parent)
        layout = QVBoxLayout(caja)
        texto = QLabel(
            "Reiniciar partes: borra TODOS los partes/incidentes para empezar la carga limpia del año. "
            "Se conservan el personal, las unidades, los contactos y la configuración del cuartel. "
            "Antes se guarda un respaldo completo de la base en data/respaldos/.", caja)
        texto.setWordWrap(True)
        texto.setProperty("muted", True)
        layout.addWidget(texto)
        boton = QPushButton("🗑️ Reiniciar partes (dejar en cero)", caja)
        boton.setObjectName("botonQuitarFila")
        boton.clicked.connect(self._reiniciar_partes)
        layout.addWidget(boton, 0, Qt.AlignmentFlag.AlignLeft)
        return caja

    def _reiniciar_partes(self) -> None:
        if self._lote_ruba is not None:
            QMessageBox.information(self, "Carga en RUBA en curso", "Esperá a que termine la carga en RUBA.")
            return
        with get_session() as session:
            cantidad = session.query(Incidente).count()
        if cantidad == 0:
            QMessageBox.information(self, "Reiniciar partes", "No hay partes cargados: la base ya está en cero.")
            return
        dialogo = QDialog(self)
        dialogo.setWindowTitle("Reiniciar partes")
        layout = QVBoxLayout(dialogo)
        aviso = QLabel(
            f"Se van a borrar los {cantidad} partes cargados (con sus dotaciones, damnificados y estados "
            "de RUBA). El personal, las unidades y la configuración se conservan y se guarda un respaldo "
            "de la base antes de borrar.\n\nPara confirmar escribí BORRAR:", dialogo)
        aviso.setWordWrap(True)
        layout.addWidget(aviso)
        entrada = QLineEdit(dialogo)
        layout.addWidget(entrada)
        fila = QHBoxLayout()
        fila.addStretch(1)
        boton_cancelar = QPushButton("Cancelar", dialogo)
        boton_cancelar.clicked.connect(dialogo.reject)
        boton_borrar = QPushButton("Borrar todos los partes", dialogo)
        boton_borrar.setObjectName("botonQuitarFila")
        boton_borrar.setEnabled(False)
        boton_borrar.clicked.connect(dialogo.accept)
        entrada.textChanged.connect(lambda t: boton_borrar.setEnabled(t.strip() == "BORRAR"))
        fila.addWidget(boton_cancelar)
        fila.addWidget(boton_borrar)
        layout.addLayout(fila)
        if dialogo.exec() != QDialog.DialogCode.Accepted:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            borrados, respaldo = reiniciar_partes()
        except Exception as e:  # noqa: BLE001 - base bloqueada / disco lleno: no se borró nada
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "No se pudo reiniciar", f"{type(e).__name__}: {e}")
            return
        QApplication.restoreOverrideCursor()
        self._incidente_en_edicion = None
        self._limpiar_formulario()
        self._pagina_historial.refrescar()
        self._actualizar_dashboard()
        QMessageBox.information(self, "Partes reiniciados",
                                f"Se borraron {borrados} parte(s).\nRespaldo previo: {respaldo}")

    def _buscar_actualizaciones_ahora(self) -> None:
        """Búsqueda manual: funciona aunque el modo sea "desactivado" o la
        ventana se haya creado sin controlador (tests / arranque sin run.py)."""
        if self.controlador_actualizaciones is None:
            from app.ui.actualizaciones import ControladorActualizaciones

            self.controlador_actualizaciones = ControladorActualizaciones(self)
        controlador = self.controlador_actualizaciones
        if not getattr(self, "_boton_actualizaciones_conectado", False):
            boton = self._boton_buscar_actualizaciones
            controlador.busqueda_en_curso.connect(
                lambda activa: (boton.setEnabled(not activa),
                                boton.setText("⏳ Buscando actualizaciones…" if activa
                                              else "🔄 Buscar actualizaciones ahora")))
            self._boton_actualizaciones_conectado = True
        controlador.buscar_manual()

    def _crear_caja_config_mapa(self) -> QGroupBox:
        """Mapa Operativo: imagen base (carta topográfica/satelital) y los
        límites geográficos de sus bordes (calibración estilo Avenza)."""
        caja = QGroupBox("Mapa Operativo")
        form = QFormLayout(caja)

        fila_imagen = QHBoxLayout()
        self._label_imagen_mapa = QLabel(caja)
        self._label_imagen_mapa.setWordWrap(True)
        self._label_imagen_mapa.setProperty("muted", True)
        boton_imagen = QPushButton("Cargar nueva imagen de mapa (.png, .jpg)", caja)
        boton_imagen.setObjectName("botonHerramienta")
        boton_imagen.clicked.connect(self._cargar_imagen_mapa)
        fila_imagen.addWidget(self._label_imagen_mapa, 1)
        fila_imagen.addWidget(boton_imagen)
        form.addRow("Imagen base", fila_imagen)

        def spin(minimo: float, maximo: float) -> QDoubleSpinBox:
            s = QDoubleSpinBox(caja)
            s.setRange(minimo, maximo)
            s.setDecimals(6)
            s.setSingleStep(0.01)
            return s

        self._spin_mapa_norte = spin(-90, 90)
        self._spin_mapa_sur = spin(-90, 90)
        self._spin_mapa_oeste = spin(-180, 180)
        self._spin_mapa_este = spin(-180, 180)
        self._spin_cuartel_lat = spin(-90, 90)
        self._spin_cuartel_lon = spin(-180, 180)
        form.addRow("Latitud borde norte (arriba)", self._spin_mapa_norte)
        form.addRow("Latitud borde sur (abajo)", self._spin_mapa_sur)
        form.addRow("Longitud borde oeste (izquierda)", self._spin_mapa_oeste)
        form.addRow("Longitud borde este (derecha)", self._spin_mapa_este)
        form.addRow("Cuartel: latitud", self._spin_cuartel_lat)
        form.addRow("Cuartel: longitud", self._spin_cuartel_lon)
        ayuda = QLabel(
            "La imagen tiene que estar orientada al norte y sin rotar. Los límites son las "
            "coordenadas exactas de sus bordes (se leen en Google Earth / QGIS o en la carta).", caja,
        )
        ayuda.setWordWrap(True)
        ayuda.setProperty("muted", True)
        form.addRow(ayuda)
        return caja

    def _cargar_config_mapa(self) -> None:
        calibracion = cartografia.cargar_calibracion()
        self._spin_mapa_norte.setValue(calibracion.lat_norte)
        self._spin_mapa_sur.setValue(calibracion.lat_sur)
        self._spin_mapa_oeste.setValue(calibracion.lon_oeste)
        self._spin_mapa_este.setValue(calibracion.lon_este)
        self._spin_cuartel_lat.setValue(calibracion.cuartel_lat)
        self._spin_cuartel_lon.setValue(calibracion.cuartel_lon)
        ruta = cartografia.ruta_imagen_base()
        self._label_imagen_mapa.setText(
            str(ruta) if ruta else f"(sin imagen: se muestra la cuadrícula) -> {cartografia.carpeta_imagen()}"
        )

    def _calibracion_de_formulario(self) -> "cartografia.CalibracionMapa":
        return cartografia.CalibracionMapa(
            lat_norte=self._spin_mapa_norte.value(), lat_sur=self._spin_mapa_sur.value(),
            lon_oeste=self._spin_mapa_oeste.value(), lon_este=self._spin_mapa_este.value(),
            cuartel_lat=self._spin_cuartel_lat.value(), cuartel_lon=self._spin_cuartel_lon.value(),
        )

    def _cargar_imagen_mapa(self) -> None:
        ruta_texto, _ = QFileDialog.getOpenFileName(
            self, "Cargar nueva imagen de mapa", "", "Imágenes (*.png *.jpg *.jpeg)"
        )
        if not ruta_texto:
            return
        try:
            destino = cartografia.instalar_imagen_base(Path(ruta_texto))
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "No se pudo cargar la imagen", str(e))
            return
        self._recargar_mapas()
        self._cargar_config_mapa()
        QMessageBox.information(
            self, "Imagen de mapa cargada",
            f"Se copió a:\n{destino}\n\nRevisá que los límites norte/sur/oeste/este de abajo "
            "correspondan a los bordes de ESTA imagen y guardá la configuración.",
        )

    def _recargar_mapas(self) -> None:
        for mapa in (getattr(self, "mapa", None), getattr(getattr(self, "_pagina_mapa", None), "mapa", None)):
            if mapa is not None:
                mapa.recargar_mapa()

    def _on_tema_cambiado(self) -> None:
        """Cambia el tema en caliente y lo recuerda en config.json."""
        nombre = self._combo_tema.currentData()
        theme.aplicar_tema(QApplication.instance(), nombre)
        theme.guardar_tema(nombre)
        theme.aplicar_sombra(self._pagina_historial.boton_cargar_lote, "rojo")
        self.statusBar().showMessage(f"Tema {self._combo_tema.currentText().split(' —')[0].lower()} aplicado.", 4000)

    def _ruta_config_json(self) -> Path:
        return DATA_DIR / "config.json"

    def _cargar_pagina_configuracion(self) -> None:
        ruta = self._ruta_config_json()
        datos: dict = {}
        if ruta.exists():
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                datos = {}

        ruba = datos.get("ruba", {})
        cuartel = datos.get("cuartel", {})
        self._entry_ruba_usuario.setText(ruba.get("usuario", ""))
        self._entry_ruba_clave.setText(ruba.get("clave", ""))
        self._entry_ruba_url_login.setText(ruba.get("url_login", ""))
        self._entry_ruba_url_incidentes.setText(ruba.get("url_incidentes", ""))
        self._entry_cuartel_nombre.setText(cuartel.get("nombre", ""))
        self._entry_cuartel_localidad.setText(cuartel.get("localidad", ""))
        self._entry_cuartel_codigo.setText(cuartel.get("codigo", ""))

        self._label_ruta_datos.setText(str(ruta))
        self._label_ruta_plantillas.setText(str(TEMPLATES_DIR))
        self._label_ruta_salida.setText(str(OUTPUT_DIR))
        self._cargar_config_mapa()

    def _guardar_configuracion(self) -> None:
        calibracion = self._calibracion_de_formulario()
        if not calibracion.es_valida():
            QMessageBox.warning(
                self, "Límites del mapa inválidos",
                "El borde norte tiene que tener mayor latitud que el sur (ej. -33.55 > -33.72) "
                "y el este mayor longitud que el oeste (ej. -63.92 > -64.12).",
            )
            return
        try:
            datos = json.loads(self._ruta_config_json().read_text(encoding="utf-8"))
            if not isinstance(datos, dict):
                datos = {}
        except (OSError, ValueError):
            datos = {}
        datos.update({
            "ruba": {
                "usuario": self._entry_ruba_usuario.text().strip(),
                "clave": self._entry_ruba_clave.text(),
                "url_login": self._entry_ruba_url_login.text().strip(),
                "url_incidentes": self._entry_ruba_url_incidentes.text().strip(),
            },
            "cuartel": {
                "nombre": self._entry_cuartel_nombre.text().strip(),
                "localidad": self._entry_cuartel_localidad.text().strip(),
                "codigo": self._entry_cuartel_codigo.text().strip(),
            },
            "mapa": dataclasses.asdict(calibracion),
        })
        try:
            self._ruta_config_json().write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Error al guardar", str(e))
            return
        self._recargar_mapas()
        QMessageBox.information(self, "Configuración guardada", "Los cambios se guardaron correctamente.")

    # -- Aviso de sincronización pendiente al arranque (Fase 4) ------------------

    def _verificar_pendientes_ruba_al_inicio(self) -> None:
        """Chequea la conexión (para el chip de RUBA) y, si hay salidas
        pendientes y hay internet, ofrece sincronizarlas."""
        with get_session() as session:
            pendientes = (
                session.query(Incidente)
                .filter(Incidente.estado_ruba.in_([EstadoRuba.PENDIENTE.value, EstadoRuba.ERROR.value]))
                .filter(Incidente.estado_operativo == EstadoOperativo.CERRADO.value)
                .count()
            )
        self._cantidad_pendientes_inicio = pendientes
        hilo, worker = lanzar_chequeo_conectividad()
        self._hilo_conectividad = (hilo, worker)
        worker.resultado.connect(self._on_resultado_conectividad)
        hilo.finished.connect(lambda: setattr(self, "_hilo_conectividad", None))

    def _on_resultado_conectividad(self, hay_internet: bool) -> None:
        credenciales = cargar_credenciales()
        if not hay_internet:
            self._set_chip_ruba("sin conexión", "error", "No hay salida a internet: las cargas quedan pendientes.")
            return
        if not (credenciales.get("usuario") and credenciales.get("clave")):
            self._set_chip_ruba("sin credenciales", "alerta", "Cargá usuario y clave en Configuración.")
        else:
            pendientes = self._cantidad_pendientes_inicio
            self._set_chip_ruba(
                f"en línea · {pendientes} pendiente(s)" if pendientes else "en línea",
                "alerta" if pendientes else "ok",
            )
        if not self._cantidad_pendientes_inicio:
            return
        self._boton_sync_pendientes.setText(
            f"⇪ Sincronizar {self._cantidad_pendientes_inicio} pendiente(s) con RUBA"
        )
        self._boton_sync_pendientes.setVisible(True)

    def _sincronizar_todos_pendientes(self) -> None:
        self._boton_sync_pendientes.setVisible(False)
        with get_session() as session:
            pendientes = (
                session.query(Incidente)
                .filter(Incidente.estado_ruba.in_([EstadoRuba.PENDIENTE.value, EstadoRuba.ERROR.value]))
                .filter(Incidente.estado_operativo == EstadoOperativo.CERRADO.value)
                .all()
            )
            ids = [inc.id for inc in pendientes]
        self._cargar_lote_ruba(ids)
