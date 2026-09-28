"""
Sistema de diseño de Fire Station (PySide6).

Todo el aspecto visual sale de acá: una `Paleta` de tokens (fondos,
superficies, bordes, texto y acentos), una hoja de estilos QSS generada a
partir de esos tokens, íconos SVG (flechas, tilde) generados en el color del
tema, y helpers para el código que necesita colores en tiempo de ejecución
(estados, sombras, validación). La lógica de negocio no define colores.

Dos temas con los mismos tokens:
  - "oscuro" (por defecto): centro de despacho -- slate/charcoal profundo,
    rojo bomberil para lo urgente, azul comando para guardar/imprimir y
    verde operativo para confirmaciones.
  - "claro": institucional pulido, mismos acentos.

La preferencia se guarda en data/config.json -> {"ui": {"tema": "oscuro"}}.

Uso:  aplicar_tema(app)            # al arrancar (run.py)
      aplicar_tema(app, "claro")   # cambio en caliente (Configuración)
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget

TEMA_POR_DEFECTO = "oscuro"
FUENTE = "Segoe UI"
FUENTES_CSS = "'Segoe UI', 'Inter', 'Roboto', sans-serif"


@dataclass(frozen=True)
class Paleta:
    nombre: str
    # Fondos y superficies (de más profundo a más elevado)
    fondo_hundido: str      # sidebar, barra de estado
    fondo: str              # lienzo de las páginas
    superficie: str         # tarjetas
    panel: str              # cabeceras de tarjeta, encabezados de tabla, popups
    campo: str              # fondo de inputs
    campo_hover: str
    fila_alterna: str
    # Bordes
    borde: str
    borde_suave: str
    borde_fuerte: str
    # Texto
    texto: str
    texto_secundario: str
    texto_tenue: str
    texto_sobre_acento: str
    # Acentos
    rojo: str
    rojo_hover: str
    rojo_pressed: str
    azul: str
    azul_hover: str
    azul_pressed: str
    azul_texto: str         # azul legible como texto/ícono sobre el fondo
    verde: str
    verde_hover: str
    verde_texto: str
    ambar: str
    # Barra superior
    barra: str
    # Sombra de tarjetas (rgba)
    sombra: tuple


OSCURO = Paleta(
    nombre="oscuro",
    fondo_hundido="#15181E",
    fondo="#1A1D24",
    superficie="#222631",
    panel="#2A2F3D",
    campo="#1C2029",
    campo_hover="#20242E",
    fila_alterna="#262B37",
    borde="#3E4456",
    borde_suave="#30354A",
    borde_fuerte="#56607A",
    texto="#E8EBF1",
    texto_secundario="#A9B1C1",
    texto_tenue="#7B8397",
    texto_sobre_acento="#FFFFFF",
    rojo="#E53935",
    rojo_hover="#EF5350",
    rojo_pressed="#C62828",
    azul="#1976D2",
    azul_hover="#1E88E5",
    azul_pressed="#1565C0",
    azul_texto="#64B5F6",
    verde="#388E3C",
    verde_hover="#43A047",
    verde_texto="#66BB6A",
    ambar="#FFB300",
    barra="#1E222B",
    sombra=(0, 0, 0, 110),
)

CLARO = Paleta(
    nombre="claro",
    fondo_hundido="#EDF0F5",
    fondo="#F4F6FA",
    superficie="#FFFFFF",
    panel="#F7F9FC",
    campo="#FFFFFF",
    campo_hover="#FAFBFD",
    fila_alterna="#F8FAFD",
    borde="#D5DBE5",
    borde_suave="#E5E9F0",
    borde_fuerte="#AAB4C4",
    texto="#111827",
    texto_secundario="#4B5563",
    texto_tenue="#8A94A6",
    texto_sobre_acento="#FFFFFF",
    rojo="#D32F2F",
    rojo_hover="#E53935",
    rojo_pressed="#B71C1C",
    azul="#1976D2",
    azul_hover="#1E88E5",
    azul_pressed="#1565C0",
    azul_texto="#1565C0",
    verde="#388E3C",
    verde_hover="#43A047",
    verde_texto="#2E7D32",
    ambar="#B26A00",
    barra="#FFFFFF",
    sombra=(15, 23, 42, 38),
)

TEMAS = {"oscuro": OSCURO, "claro": CLARO}

# Paleta activa: la leen los helpers de abajo y el código que colorea en
# tiempo de ejecución (estados del historial, barra de estado, etc.).
PALETA: Paleta = OSCURO


def _rgba(hex_color: str, alfa: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alfa})"


# ---------------------------------------------------------------------------
# Íconos SVG en el color del tema (QSS solo acepta imágenes para flechas y tildes)
# ---------------------------------------------------------------------------

_SVG = {
    "chevron-down": '<path d="M4 6l4 4 4-4" fill="none" stroke="{c}" stroke-width="1.8" '
                    'stroke-linecap="round" stroke-linejoin="round"/>',
    "chevron-up": '<path d="M4 10l4-4 4 4" fill="none" stroke="{c}" stroke-width="1.8" '
                  'stroke-linecap="round" stroke-linejoin="round"/>',
    "check": '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{c}" stroke-width="2.2" '
             'stroke-linecap="round" stroke-linejoin="round"/>',
}


def _generar_iconos(p: Paleta) -> dict:
    carpeta = Path(tempfile.gettempdir()) / "fire_station_tema" / p.nombre
    carpeta.mkdir(parents=True, exist_ok=True)
    rutas = {}
    for nombre, cuerpo, color in (
        ("chevron-down", _SVG["chevron-down"], p.texto_secundario),
        ("chevron-up", _SVG["chevron-up"], p.texto_secundario),
        ("chevron-down-deshabilitado", _SVG["chevron-down"], p.texto_tenue),
        ("check", _SVG["check"], p.texto_sobre_acento),
    ):
        ruta = carpeta / f"{nombre}.svg"
        ruta.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f'{cuerpo.format(c=color)}</svg>',
            encoding="utf-8",
        )
        rutas[nombre] = ruta.as_posix()
    return rutas


# ---------------------------------------------------------------------------
# Hoja de estilos
# ---------------------------------------------------------------------------

def construir_qss(p: Paleta, iconos: Optional[dict] = None) -> str:
    i = iconos or _generar_iconos(p)
    foco_fondo = _rgba(p.azul, 0.10)
    seleccion = _rgba(p.azul, 0.28)
    hover_fila = _rgba(p.azul, 0.10)
    return f"""
/* ===== Base ============================================================== */
QWidget {{
    color: {p.texto};
    font-family: {FUENTES_CSS};
    font-size: 13px;
}}
QMainWindow, QDialog, QMessageBox, QStackedWidget {{
    background-color: {p.fondo};
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QLabel {{ background: transparent; }}
QLabel[muted="true"] {{ color: {p.texto_secundario}; font-size: 12px; }}
QLabel#pageTitle {{ font-size: 22px; font-weight: 700; color: {p.texto}; }}
QLabel#pageSubtitle {{ font-size: 13px; color: {p.texto_secundario}; }}
QToolTip {{
    background-color: {p.panel};
    color: {p.texto};
    border: 1px solid {p.borde};
    border-radius: 6px;
    padding: 6px 10px;
}}

/* ===== Sidebar =========================================================== */
QWidget#sidebar {{
    background-color: {p.fondo_hundido};
    border-right: 1px solid {p.borde_suave};
}}
QWidget#sidebarCabecera {{ background: transparent; }}
QLabel#sidebarEscudo {{ font-size: 30px; }}
QLabel#sidebarTitulo {{ color: {p.texto}; font-size: 16px; font-weight: 700; }}
QLabel#sidebarSubtitulo {{ color: {p.texto_tenue}; font-size: 11px; letter-spacing: 0.3px; }}
QLabel#sidebarSeccion {{
    color: {p.texto_tenue}; font-size: 10px; font-weight: 700;
    letter-spacing: 1.2px; padding: 0 14px;
}}
QFrame#sidebarSeparador {{ background-color: {p.borde_suave}; max-height: 1px; border: none; }}
QPushButton#navButton {{
    background-color: transparent;
    color: {p.texto_secundario};
    border: none;
    border-left: 3px solid transparent;
    border-radius: 6px;
    text-align: left;
    padding: 10px 14px 10px 13px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton#navButton:hover {{ background-color: {p.panel}; color: {p.texto}; }}
QPushButton#navButton:checked {{
    background-color: {_rgba(p.rojo, 0.14)};
    border-left: 3px solid {p.rojo};
    color: {p.texto};
    font-weight: 700;
}}

/* Selector de vista (Personal | Unidades / Móviles): pestañas tipo toggle. */
QPushButton#toggleVista {{
    background-color: {p.superficie};
    color: {p.texto_secundario};
    border: 1px solid {p.borde};
    border-radius: 0px;
    padding: 8px 18px;
}}
QPushButton#toggleVista:hover {{ color: {p.texto}; border-color: {p.borde_fuerte}; }}
QPushButton#toggleVista:checked {{
    background-color: {_rgba(p.rojo, 0.14)};
    border: 1px solid {p.rojo};
    color: {p.texto};
    font-weight: 700;
}}

/* ===== Barra superior (sección activa + estado) ========================= */
QFrame#barraInstitucional {{
    background-color: {p.barra};
    border-bottom: 2px solid {p.rojo};
}}
QLabel#barraEscudo {{ font-size: 20px; }}
QLabel#barraTitulo {{
    color: {p.texto}; font-size: 15px; font-weight: 700; letter-spacing: 1px;
}}
QLabel#chip {{
    border-radius: 11px;
    padding: 3px 11px;
    font-size: 11px;
    font-weight: 600;
    border: 1px solid {p.borde};
    background-color: {p.panel};
    color: {p.texto_secundario};
}}
QLabel#chip[tono="ok"] {{
    background-color: {_rgba(p.verde, 0.16)}; border-color: {_rgba(p.verde, 0.55)}; color: {p.verde_texto};
}}
QLabel#chip[tono="alerta"] {{
    background-color: {_rgba(p.ambar, 0.14)}; border-color: {_rgba(p.ambar, 0.55)}; color: {p.ambar};
}}
QLabel#chip[tono="error"] {{
    background-color: {_rgba(p.rojo, 0.14)}; border-color: {_rgba(p.rojo, 0.55)}; color: {p.rojo_hover};
}}
QLabel#chip[tono="info"] {{
    background-color: {_rgba(p.azul, 0.16)}; border-color: {_rgba(p.azul, 0.55)}; color: {p.azul_texto};
}}

/* ===== Tarjetas de sección del formulario =============================== */
QFrame#seccionFormulario {{
    background-color: {p.superficie};
    border: 1px solid {p.borde_suave};
    border-radius: 10px;
}}
QFrame#cabeceraSeccion {{
    background-color: {p.panel};
    border: none;
    border-bottom: 1px solid {p.borde_suave};
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
}}
QLabel#badgeSeccion {{
    background-color: {p.rojo};
    color: {p.texto_sobre_acento};
    border-radius: 12px;
    font-size: 12px;
    font-weight: 800;
    min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
    qproperty-alignment: AlignCenter;
}}
QLabel#tituloSeccion {{
    color: {p.texto}; font-size: 13px; font-weight: 700; letter-spacing: 0.8px;
}}
QLabel#ayudaSeccion {{ color: {p.texto_tenue}; font-size: 12px; }}
QLabel#subtituloBloque {{
    color: {p.texto_secundario};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.9px;
    padding: 6px 0 5px 0;
    border-bottom: 1px solid {p.borde_suave};
    margin-top: 4px;
}}

/* QGroupBox como tarjeta (páginas de gestión) */
QGroupBox {{
    background-color: {p.superficie};
    border: 1px solid {p.borde_suave};
    border-radius: 10px;
    margin-top: 0px;
    padding: 40px 14px 14px 14px;
    font-size: 13px;
    font-weight: 700;
}}
QGroupBox QWidget {{ font-weight: 400; }}
QGroupBox::title {{
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 16px;
    top: 12px;
    color: {p.texto};
    font-weight: 700;
    font-size: 13px;
}}

/* ===== Campos de entrada ================================================ */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QTimeEdit, QDateEdit, QListWidget {{
    background-color: {p.campo};
    border: 1px solid {p.borde};
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 20px;
    color: {p.texto};
    selection-background-color: {p.azul};
    selection-color: {p.texto_sobre_acento};
}}
QLineEdit:hover, QTextEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover,
QTimeEdit:hover, QDateEdit:hover {{
    border-color: {p.borde_fuerte};
    background-color: {p.campo_hover};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QTimeEdit:focus, QDateEdit:focus {{
    border: 1px solid {p.azul_hover};
    background-color: {foco_fondo};
}}
QLineEdit:read-only {{ color: {p.texto_secundario}; background-color: {p.panel}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QTimeEdit:disabled, QDateEdit:disabled, QTextEdit:disabled {{
    color: {p.texto_tenue};
    background-color: {p.panel};
    border-color: {p.borde_suave};
}}
QLineEdit[invalido="true"], QComboBox[invalido="true"] {{
    border: 1px solid {p.rojo};
    background-color: {_rgba(p.rojo, 0.08)};
}}

QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border: none;
}}
QComboBox::down-arrow {{ image: url({i["chevron-down"]}); width: 14px; height: 14px; }}
QComboBox::down-arrow:disabled {{ image: url({i["chevron-down-deshabilitado"]}); }}
QComboBox QAbstractItemView {{
    background-color: {p.panel};
    border: 1px solid {p.borde};
    border-radius: 6px;
    padding: 4px;
    outline: 0;
    selection-background-color: {seleccion};
    selection-color: {p.texto};
}}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 2px 8px; border-radius: 4px; }}

QSpinBox, QDoubleSpinBox, QTimeEdit, QDateEdit {{ padding-right: 24px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button, QDateEdit::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: 22px; border: none; background: transparent; margin-top: 2px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button, QDateEdit::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 22px; border: none; background: transparent; margin-bottom: 2px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QTimeEdit::up-arrow, QDateEdit::up-arrow {{
    image: url({i["chevron-up"]}); width: 10px; height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QTimeEdit::down-arrow, QDateEdit::down-arrow {{
    image: url({i["chevron-down"]}); width: 10px; height: 10px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover, QTimeEdit::up-button:hover, QTimeEdit::down-button:hover,
QDateEdit::up-button:hover, QDateEdit::down-button:hover {{
    background-color: {p.panel}; border-radius: 4px;
}}
QDateEdit::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none;
}}

/* Calendario desplegable */
QCalendarWidget QWidget {{ alternate-background-color: {p.panel}; }}
QCalendarWidget QWidget#qt_calendar_navigationbar {{ background-color: {p.panel}; }}
QCalendarWidget QToolButton {{
    color: {p.texto}; background: transparent; border: none; padding: 4px 8px; font-weight: 600;
}}
QCalendarWidget QToolButton:hover {{ background-color: {p.superficie}; border-radius: 4px; }}
QCalendarWidget QAbstractItemView {{
    background-color: {p.superficie};
    color: {p.texto};
    selection-background-color: {p.azul};
    selection-color: {p.texto_sobre_acento};
    outline: 0;
}}
QCalendarWidget QAbstractItemView:disabled {{ color: {p.texto_tenue}; }}

/* Casillas y radios */
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.borde_fuerte};
    background-color: {p.campo};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.azul_hover}; }}
QCheckBox::indicator:checked {{
    background-color: {p.azul}; border-color: {p.azul}; image: url({i["check"]});
}}
QRadioButton::indicator:checked {{
    border: 1px solid {p.azul};
    background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 {p.texto_sobre_acento}, stop:0.34 {p.texto_sobre_acento}, stop:0.42 {p.azul}, stop:1 {p.azul});
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background-color: {p.panel}; border-color: {p.borde_suave};
}}

/* ===== Tablas =========================================================== */
QTableView, QTableWidget, QListView {{
    background-color: {p.superficie};
    alternate-background-color: {p.fila_alterna};
    border: 1px solid {p.borde_suave};
    border-radius: 8px;
    gridline-color: {p.borde_suave};
    selection-background-color: {seleccion};
    selection-color: {p.texto};
    outline: 0;
}}
QTableView::item {{ padding: 4px 6px; border: none; }}
QTableView::item:hover {{ background-color: {hover_fila}; }}
QTableView::item:selected {{ background-color: {seleccion}; color: {p.texto}; }}
QHeaderView {{ background-color: {p.panel}; border: none; }}
QHeaderView::section {{
    background-color: {p.panel};
    color: {p.texto_secundario};
    font-weight: 600;
    font-size: 12px;
    min-height: 34px;
    padding: 0 10px;
    border: none;
    border-bottom: 1px solid {p.borde};
    border-right: 1px solid {p.borde_suave};
}}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background-color: {p.panel}; border: none; }}
/* Controles dentro de celdas: más compactos */
QTableView QLineEdit, QTableView QComboBox, QTableView QSpinBox,
QTableView QTimeEdit, QTableView QDateEdit {{
    border-radius: 5px; padding: 3px 8px; min-height: 18px;
}}
QTableView QComboBox, QTableView QTimeEdit, QTableView QDateEdit {{ padding-right: 24px; }}
QTableView QPushButton {{ padding: 4px 10px; font-size: 12px; min-height: 0; border-radius: 5px; }}

/* ===== Barras de desplazamiento ======================================== */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.borde}; border-radius: 3px; min-height: 36px; }}
QScrollBar::handle:horizontal {{ background: {p.borde}; border-radius: 3px; min-width: 36px; }}
QScrollBar::handle:hover {{ background: {p.borde_fuerte}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ===== Botones ========================================================== */
QPushButton {{
    background-color: {p.panel};
    border: 1px solid {p.borde};
    border-radius: 6px;
    padding: 7px 14px;
    color: {p.texto};
    font-weight: 500;
}}
QPushButton:hover {{ border-color: {p.borde_fuerte}; background-color: {p.campo_hover}; }}
QPushButton:pressed {{ background-color: {p.fondo_hundido}; }}
QPushButton:disabled {{ color: {p.texto_tenue}; border-color: {p.borde_suave}; background-color: {p.superficie}; }}
QPushButton:focus {{ outline: none; }}

/* Secundario de acento (Hoy / Ahora / + Agregar / Generar filas) */
QPushButton#botonAhora {{
    background-color: {_rgba(p.azul, 0.14)};
    border: 1px solid {_rgba(p.azul, 0.45)};
    color: {p.azul_texto};
    font-weight: 600;
    padding: 6px 12px;
}}
QPushButton#botonAhora:hover {{ background-color: {_rgba(p.azul, 0.24)}; border-color: {p.azul_hover}; }}
QPushButton#botonAhora:pressed {{ background-color: {_rgba(p.azul, 0.34)}; }}

/* Confirmación (diálogos, Guardar Configuración): verde operativo */
QPushButton#botonGuardar {{
    background-color: {p.verde};
    border: 1px solid {p.verde};
    color: {p.texto_sobre_acento};
    font-weight: 700;
    padding: 9px 18px;
}}
QPushButton#botonGuardar:hover {{ background-color: {p.verde_hover}; border-color: {p.verde_hover}; }}
QPushButton#botonGuardar:pressed {{ background-color: {p.verde}; padding-top: 10px; padding-bottom: 8px; }}

/* Barra de herramientas del mapa: compacta */
QPushButton#botonHerramienta {{ padding: 5px 10px; font-size: 12px; }}

/* Quitar fila (✖): discreto, rojo al pasar */
QPushButton#botonQuitarFila {{
    background: transparent; border: 1px solid transparent; color: {p.texto_tenue};
    padding: 2px; font-size: 12px;
}}
QPushButton#botonQuitarFila:hover {{
    color: {p.rojo_hover}; background-color: {_rgba(p.rojo, 0.12)}; border-color: {_rgba(p.rojo, 0.4)};
}}

/* Barra de acciones del formulario */
QPushButton#botonLimpiar, QPushButton#botonBorradorCurso, QPushButton#botonPrimarioAzul,
QPushButton#botonPrimarioRojo {{
    min-height: 46px;
    font-size: 14px;
    font-weight: 700;
    border-radius: 8px;
    padding: 10px 18px;
}}
QPushButton#botonLimpiar {{
    background-color: transparent;
    border: 1px solid {p.borde};
    color: {p.texto_secundario};
}}
QPushButton#botonLimpiar:hover {{ background-color: {p.panel}; color: {p.texto}; border-color: {p.borde_fuerte}; }}
QPushButton#botonLimpiar:pressed {{ background-color: {p.fondo_hundido}; }}
QPushButton#botonBorradorCurso {{
    background-color: {_rgba(p.ambar, 0.14)};
    border: 1px solid {_rgba(p.ambar, 0.6)};
    color: {p.ambar};
}}
QPushButton#botonBorradorCurso:hover {{ background-color: {_rgba(p.ambar, 0.24)}; border-color: {p.ambar}; }}
QPushButton#botonBorradorCurso:pressed {{ background-color: {_rgba(p.ambar, 0.34)}; padding-top: 11px; padding-bottom: 9px; }}
QPushButton#botonPrimarioAzul {{
    background-color: {p.azul};
    border: 1px solid {p.azul};
    color: {p.texto_sobre_acento};
}}
QPushButton#botonPrimarioAzul:hover {{ background-color: {p.azul_hover}; border-color: {p.azul_hover}; }}
QPushButton#botonPrimarioAzul:pressed {{
    background-color: {p.azul_pressed}; border-color: {p.azul_pressed}; padding-top: 11px; padding-bottom: 9px;
}}
QPushButton#botonPrimarioRojo {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.rojo_hover}, stop:1 {p.rojo});
    border: 1px solid {p.rojo_pressed};
    color: {p.texto_sobre_acento};
}}
QPushButton#botonPrimarioRojo:hover {{
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F26A67, stop:1 {p.rojo_hover});
}}
QPushButton#botonPrimarioRojo:pressed {{
    background-color: {p.rojo_pressed}; padding-top: 11px; padding-bottom: 9px;
}}

/* ===== Damnificados: tarjetas con conmutador No / Sí ==================== */
QFrame#tarjetaConmutable {{
    background-color: {p.panel};
    border: 1px solid {p.borde_suave};
    border-radius: 10px;
}}
QFrame#tarjetaConmutable[activa="true"] {{
    border: 1px solid {_rgba(p.rojo, 0.55)};
    background-color: {_rgba(p.rojo, 0.05)};
}}
QLabel#iconoConmutable {{ font-size: 18px; }}
QLabel#preguntaConmutable {{ font-size: 13px; font-weight: 700; color: {p.texto}; }}
QPushButton#segmento {{
    min-width: 54px;
    padding: 5px 12px;
    border: 1px solid {p.borde};
    background-color: {p.campo};
    color: {p.texto_secundario};
    font-weight: 600;
}}
QPushButton#segmento[lado="izq"] {{
    border-top-left-radius: 6px; border-bottom-left-radius: 6px;
    border-top-right-radius: 0; border-bottom-right-radius: 0;
}}
QPushButton#segmento[lado="der"] {{
    border-top-right-radius: 6px; border-bottom-right-radius: 6px;
    border-top-left-radius: 0; border-bottom-left-radius: 0; border-left: none;
}}
QPushButton#segmento:hover {{ color: {p.texto}; background-color: {p.campo_hover}; }}
QPushButton#segmento[lado="izq"]:checked {{
    background-color: {p.borde}; color: {p.texto}; border-color: {p.borde_fuerte};
}}
QPushButton#segmento[lado="der"]:checked {{
    background-color: {p.rojo}; color: {p.texto_sobre_acento}; border-color: {p.rojo};
}}

/* ===== Dotaciones por Unidad =========================================== */
QFrame#tarjetaUnidad {{
    background-color: {p.panel};
    border: 1px solid {p.borde_suave};
    border-left: 3px solid {p.azul};
    border-radius: 10px;
}}
QLabel#tituloUnidad {{ font-size: 14px; font-weight: 700; color: {p.texto}; }}

/* ===== Servicio en curso (dashboard) =================================== */
QFrame#tarjetaEnCurso {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {_rgba(p.rojo, 0.22)}, stop:1 {p.superficie});
    border: 1px solid {_rgba(p.rojo, 0.7)};
    border-left: 4px solid {p.rojo};
    border-radius: 12px;
}}
QLabel#tituloEnCurso {{ color: {p.rojo_hover}; font-size: 12px; font-weight: 800; letter-spacing: 1.4px; }}
QLabel#parteEnCurso {{ color: {p.texto}; font-size: 18px; font-weight: 700; }}
QLabel#detalleEnCurso {{ color: {p.texto_secundario}; font-size: 12px; }}
QLabel#etiquetaCronometro {{ color: {p.texto_tenue}; font-size: 10px; font-weight: 700; letter-spacing: 1.2px; }}
QLabel#cronometroEnCurso {{
    color: {p.texto}; font-size: 30px; font-weight: 700;
    font-family: 'Cascadia Mono', 'Consolas', monospace;
}}

/* ===== Clima y guía telefónica (dashboard) ============================== */
QFrame#tarjetaClima, QFrame#tarjetaGuia {{
    background-color: {p.superficie};
    border: 1px solid {p.borde_suave};
    border-radius: 12px;
}}
QLabel#tituloTarjeta {{ font-size: 14px; font-weight: 700; color: {p.texto}; }}
QLabel#estadoClima {{ font-size: 11px; color: {p.texto_tenue}; }}
QLabel#estadoClima[tono="alerta"] {{ color: {p.ambar}; font-weight: 600; }}
QLabel#iconoClima {{ font-size: 44px; }}
QLabel#temperaturaClima {{ font-size: 34px; font-weight: 700; color: {p.texto}; }}
QLabel#descripcionClima {{ font-size: 13px; color: {p.texto_secundario}; }}
QLabel#etiquetaDatoClima {{ font-size: 10px; font-weight: 700; letter-spacing: 1px; color: {p.texto_tenue}; }}
QLabel#valorDatoClima {{ font-size: 14px; font-weight: 600; color: {p.texto}; }}
QFrame#tarjetaHora {{
    background-color: {p.panel};
    border: 1px solid {p.borde_suave};
    border-radius: 8px;
    min-width: 62px;
}}
QLabel#horaPronostico {{ font-size: 11px; font-weight: 700; color: {p.texto_secundario}; }}
QLabel#iconoPronostico {{ font-size: 20px; }}
QLabel#tempPronostico {{ font-size: 15px; font-weight: 700; color: {p.texto}; }}
QLabel#detallePronostico {{ font-size: 10px; color: {p.texto_tenue}; }}
QLineEdit#buscadorGuia {{ font-size: 14px; padding: 8px 12px; }}
QFrame#filaContacto {{
    background-color: {p.panel};
    border: 1px solid {p.borde_suave};
    border-radius: 8px;
}}
QFrame#filaContacto:hover {{ border-color: {_rgba(p.azul, 0.55)}; }}
QLabel#nombreContacto {{ font-size: 14px; font-weight: 700; color: {p.texto}; }}
QLabel#detalleContacto {{ font-size: 12px; color: {p.texto_secundario}; }}
QLabel#telefonoContacto {{
    font-size: 22px; font-weight: 700; color: {p.azul_texto};
    font-family: 'Cascadia Mono', 'Consolas', monospace;
}}

/* ===== Dashboard ======================================================== */
QFrame#kpiCard {{
    background-color: {p.superficie};
    border: 1px solid {p.borde_suave};
    border-radius: 12px;
}}
QFrame#kpiCard:hover {{ border-color: {_rgba(p.azul, 0.6)}; }}
QLabel#kpiIcono {{ font-size: 22px; }}
QLabel#kpiValor {{ font-size: 28px; font-weight: 700; color: {p.texto}; }}
QLabel#kpiTitulo {{
    font-size: 11px; font-weight: 700; color: {p.texto_secundario}; letter-spacing: 0.8px;
}}

/* ===== Firma del Encargado ============================================== */
QLabel#badgeSinFirmar, QLabel#badgeFirmado {{
    border-radius: 11px; padding: 3px 11px; font-size: 11px; font-weight: 700;
}}
QLabel#badgeSinFirmar {{
    color: {p.texto_secundario}; background-color: {p.panel}; border: 1px solid {p.borde};
}}
QLabel#badgeFirmado {{
    color: {p.verde_texto}; background-color: {_rgba(p.verde, 0.16)}; border: 1px solid {_rgba(p.verde, 0.55)};
}}

/* ===== Progreso, menús, estado ========================================= */
QProgressBar {{
    background-color: {p.panel};
    border: 1px solid {p.borde_suave};
    border-radius: 6px;
    min-height: 12px;
    max-height: 12px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    border-radius: 5px;
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p.azul}, stop:1 {p.azul_hover});
}}
QMenu {{ background-color: {p.panel}; border: 1px solid {p.borde}; border-radius: 6px; padding: 4px; }}
QMenu::item {{ padding: 6px 16px; border-radius: 4px; }}
QMenu::item:selected {{ background-color: {seleccion}; }}
QStatusBar {{
    background-color: {p.fondo_hundido};
    border-top: 1px solid {p.borde_suave};
    color: {p.texto_secundario};
    font-size: 12px;
}}
QStatusBar QLabel {{ color: {p.texto_secundario}; }}
QStatusBar[tono="ok"] {{ color: {p.verde_texto}; }}
QStatusBar[tono="error"] {{ color: {p.ambar}; }}
QMessageBox QLabel {{ color: {p.texto}; font-size: 13px; }}
QMessageBox QPushButton {{ min-width: 84px; }}

/* ===== Diálogo de progreso RUBA ======================================== */
QLabel#mensajeProgreso[tono="ok"] {{ color: {p.verde_texto}; font-weight: 700; }}
QLabel#mensajeProgreso[tono="error"] {{ color: {p.rojo_hover}; }}
"""


# ---------------------------------------------------------------------------
# Aplicación del tema
# ---------------------------------------------------------------------------

def _ruta_config() -> Path:
    from app.paths import get_writable_dir

    return get_writable_dir("data") / "config.json"


def tema_guardado() -> str:
    try:
        datos = json.loads(_ruta_config().read_text(encoding="utf-8"))
        nombre = datos.get("ui", {}).get("tema", TEMA_POR_DEFECTO)
    except (OSError, ValueError, AttributeError):
        nombre = TEMA_POR_DEFECTO
    return nombre if nombre in TEMAS else TEMA_POR_DEFECTO


def guardar_tema(nombre: str) -> None:
    ruta = _ruta_config()
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    except (OSError, ValueError):
        datos = {}
    datos.setdefault("ui", {})["tema"] = nombre
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def _paleta_qt(p: Paleta) -> QPalette:
    """Colores para lo que Qt pinta sin QSS (placeholder, links, cursores)."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.fondo,
        QPalette.ColorRole.WindowText: p.texto,
        QPalette.ColorRole.Base: p.campo,
        QPalette.ColorRole.AlternateBase: p.fila_alterna,
        QPalette.ColorRole.Text: p.texto,
        QPalette.ColorRole.Button: p.panel,
        QPalette.ColorRole.ButtonText: p.texto,
        QPalette.ColorRole.Highlight: p.azul,
        QPalette.ColorRole.HighlightedText: p.texto_sobre_acento,
        QPalette.ColorRole.ToolTipBase: p.panel,
        QPalette.ColorRole.ToolTipText: p.texto,
        QPalette.ColorRole.PlaceholderText: p.texto_tenue,
        QPalette.ColorRole.Link: p.azul_texto,
        QPalette.ColorRole.BrightText: p.rojo_hover,
    }
    for rol, color in roles.items():
        pal.setColor(rol, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.texto_tenue))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.texto_tenue))
    return pal


def aplicar_tema(app: QApplication, nombre: Optional[str] = None) -> Paleta:
    """Aplica el tema (por nombre, o el guardado en config.json) a toda la app."""
    global PALETA
    PALETA = TEMAS.get(nombre or tema_guardado(), OSCURO)
    app.setStyle("Fusion")  # base neutra: nada del look nativo Win32
    app.setPalette(_paleta_qt(PALETA))
    fuente = QFont(FUENTE, 10)
    fuente.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(fuente)
    app.setStyleSheet(construir_qss(PALETA))
    return PALETA


# ---------------------------------------------------------------------------
# Helpers para el código de UI
# ---------------------------------------------------------------------------

def color(token: str) -> str:
    """Color del tema activo por nombre de token (ej. color("verde_texto"))."""
    return getattr(PALETA, token)


def repulir(widget: QWidget) -> None:
    """Re-evalúa el QSS de un widget tras cambiarle una propiedad dinámica."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_propiedad(widget: QWidget, nombre: str, valor) -> None:
    if widget.property(nombre) != valor:
        widget.setProperty(nombre, valor)
        repulir(widget)


def marcar_invalido(widget: QWidget, invalido: bool = True) -> None:
    """Borde rojo sutil en un campo obligatorio vacío o incorrecto."""
    set_propiedad(widget, "invalido", bool(invalido))


def set_tono(widget: QWidget, tono: str) -> None:
    """Tono de un chip / barra de estado: ok | alerta | error | info | neutro."""
    set_propiedad(widget, "tono", tono)


def etiqueta_requerida(texto: str) -> str:
    """Texto enriquecido para la etiqueta de un campo obligatorio."""
    return f'{texto} <span style="color:{PALETA.rojo};">*</span>'


def aplicar_sombra(widget: QWidget, tono: str = "tarjeta") -> None:
    """Elevación sutil: 'tarjeta' (neutra) o 'rojo' (resplandor del botón principal)."""
    sombra = QGraphicsDropShadowEffect(widget)
    if tono == "rojo":
        c = QColor(PALETA.rojo)
        c.setAlpha(110)
        sombra.setBlurRadius(22)
        sombra.setOffset(0, 4)
    else:
        c = QColor(*PALETA.sombra)
        sombra.setBlurRadius(20)
        sombra.setOffset(0, 3)
    sombra.setColor(c)
    widget.setGraphicsEffect(sombra)


def estilizar_tabla(tabla, alto_fila: int = 38) -> None:
    """Filas alternadas, alto de fila y encabezado cómodos, sin rectángulo
    de foco punteado."""
    from PySide6.QtCore import Qt

    tabla.setAlternatingRowColors(True)
    tabla.setShowGrid(False)
    tabla.verticalHeader().setDefaultSectionSize(alto_fila)
    tabla.horizontalHeader().setMinimumHeight(36)
    tabla.horizontalHeader().setHighlightSections(False)
    tabla.setFocusPolicy(Qt.FocusPolicy.NoFocus if tabla.editTriggers() == tabla.EditTrigger.NoEditTriggers
                         else Qt.FocusPolicy.StrongFocus)


# Compatibilidad: código viejo que hacía app.setStyleSheet(APP_QSS).
APP_QSS = construir_qss(OSCURO)
