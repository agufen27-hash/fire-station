"""
Visor de mapa nativo estilo Avenza Maps: una imagen local georreferenciada
(`resources/mapa_base.png|jpg`, calibrada por sus bordes en data/config.json,
ver app/services/cartografia.py) dibujada con QGraphicsView/QGraphicsScene.
Sin navegador embebido ni internet: funciona igual en una PC de guardia
aislada.

  - Rueda del mouse: zoom centrado en el cursor. Clic sostenido: paneo.
  - Cuartel: marcador fijo en las coordenadas de la calibración.
  - Clic en el mapa: coloca o mueve el Lugar del Siniestro (chincheta
    roja), traza la línea cuartel -> siniestro con la distancia geodésica y
    emite `coordenadas_cambiadas` para completar "Zona / Coordenadas".
  - Polígono del área afectada (superficie y perímetro geodésicos) e
    import/export KML/KMZ/GPX, como en la versión anterior con Leaflet.

Un mismo `MapWidget`, dos usos:
  - Incrustado en el formulario de carga (`main_window.py`): editable.
  - `OperationsMapWindow`: solo lectura, con todos los incidentes que
    tienen coordenadas guardadas.

Todo el estado se guarda en coordenadas geográficas (no en píxeles): al
cambiar la imagen o la calibración, `recargar_mapa()` reproyecta todo.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services import cartografia, geografia
from app.services.cartografia import CalibracionMapa, Georreferencia
from app.services.geocodificacion import GeocodeWorker, lanzar_geocodificacion

# Valores de fábrica (la calibración real se lee de data/config.json).
CUARTEL_LAT = CalibracionMapa.cuartel_lat
CUARTEL_LON = CalibracionMapa.cuartel_lon

ANCHO_FONDO_GENERADO = 2400  # px del fondo cuadriculado cuando no hay imagen
ZOOM_MAXIMO = 40.0  # píxeles de pantalla por píxel de imagen
FACTOR_ZOOM = 1.25
TOLERANCIA_CLIC_PX = 5  # más movimiento que esto entre press y release = paneo

COLOR_SINIESTRO = QColor("#d32f2f")
COLOR_CUARTEL = QColor("#1565c0")
COLOR_POLIGONO = QColor("#ff5722")
COLOR_LINEA = QColor("#1565c0")


# ---------------------------------------------------------------------------
# Fondo generado por código (sin imagen cargada todavía)
# ---------------------------------------------------------------------------

def generar_fondo_cuadricula(calibracion: CalibracionMapa, ancho: int = ANCHO_FONDO_GENERADO) -> QPixmap:
    """Fondo claro con retícula geográfica (cada 0.01°, rotulada cada 0.05°)
    y el texto institucional. Tiene la misma calibración que tendrá la
    imagen real, así que marcar puntos y medir ya funciona sobre él."""
    alto = max(1, round(ancho / calibracion.relacion_aspecto()))
    pixmap = QPixmap(ancho, alto)
    pixmap.fill(QColor("#f8fafc"))
    geo = Georreferencia(calibracion, ancho, alto)

    pintor = QPainter(pixmap)
    pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
    fuente_rotulo = QFont("Segoe UI", 1)
    fuente_rotulo.setPixelSize(max(11, int(ancho * 0.011)))  # legible con el mapa entero a la vista
    pintor.setFont(fuente_rotulo)

    paso = 0.01
    for k in range(math.ceil(calibracion.lon_oeste / paso), math.floor(calibracion.lon_este / paso) + 1):
        x, _ = geo.coords_to_pixel(calibracion.lat_norte, k * paso)
        mayor = k % 5 == 0
        pintor.setPen(QPen(QColor("#cbd5e1" if mayor else "#e2e8f0"), 1.6 if mayor else 1))
        pintor.drawLine(QPointF(x, 0), QPointF(x, alto))
        if mayor:
            pintor.setPen(QColor("#64748b"))
            pintor.drawText(QPointF(x + 4, fuente_rotulo.pixelSize() + 4), f"{k * paso:.2f}°")
    for k in range(math.ceil(calibracion.lat_sur / paso), math.floor(calibracion.lat_norte / paso) + 1):
        _, y = geo.coords_to_pixel(k * paso, calibracion.lon_oeste)
        mayor = k % 5 == 0
        pintor.setPen(QPen(QColor("#cbd5e1" if mayor else "#e2e8f0"), 1.6 if mayor else 1))
        pintor.drawLine(QPointF(0, y), QPointF(ancho, y))
        if mayor:
            pintor.setPen(QColor("#64748b"))
            pintor.drawText(QPointF(6, y - 4), f"{k * paso:.2f}°")

    pintor.setPen(QPen(QColor("#94a3b8"), 3))
    pintor.drawRect(QRectF(1.5, 1.5, ancho - 3, alto - 3))

    # Cartel arriba (no en el centro: ahí están el pueblo y el cuartel).
    caja = QRectF(ancho * 0.14, alto * 0.05, ancho * 0.72, alto * 0.14)
    pintor.setPen(QPen(QColor("#cbd5e1"), 2))
    pintor.setBrush(QColor(255, 255, 255, 225))
    pintor.drawRoundedRect(caja, 18, 18)
    fuente_titulo = QFont("Segoe UI", 1)
    fuente_titulo.setPixelSize(max(18, int(ancho * 0.026)))
    fuente_titulo.setBold(True)
    pintor.setFont(fuente_titulo)
    pintor.setPen(QColor("#334155"))
    pintor.drawText(caja.adjusted(0, 0, 0, -caja.height() * 0.35), Qt.AlignmentFlag.AlignCenter,
                    "Mapa Operativo - Cargar imagen de zona")
    fuente_sub = QFont("Segoe UI", 1)
    fuente_sub.setPixelSize(max(12, int(ancho * 0.013)))
    pintor.setFont(fuente_sub)
    pintor.setPen(QColor("#64748b"))
    pintor.drawText(caja.adjusted(0, caja.height() * 0.45, 0, 0), Qt.AlignmentFlag.AlignCenter,
                    "Configuración → Cargar nueva imagen de mapa (.png, .jpg)")
    pintor.end()
    return pixmap


def cargar_pixmap_base(calibracion: CalibracionMapa) -> Tuple[QPixmap, bool]:
    """(pixmap, es_imagen_real). Cae al fondo generado si no hay imagen o
    no se puede leer (archivo dañado, formato no soportado)."""
    ruta = cartografia.ruta_imagen_base()
    if ruta is not None:
        # Las cartas satelitales pesan: el límite por defecto de Qt (256 MB
        # decodificados) rechaza imágenes de ~8000 x 8000 px.
        QImageReader.setAllocationLimit(1024)
        pixmap = QPixmap(str(ruta))
        if not pixmap.isNull():
            return pixmap, True
        print(f"[Mapa] No se pudo leer '{ruta}'; se usa el fondo cuadriculado.")
    return generar_fondo_cuadricula(calibracion), False


# ---------------------------------------------------------------------------
# Ítems gráficos (tamaño fijo en pantalla: ignoran el zoom)
# ---------------------------------------------------------------------------

class _MarcadorCuartel(QGraphicsItem):
    LADO = 30

    def __init__(self) -> None:
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setZValue(30)
        self.setToolTip("Cuartel de Bomberos")

    def boundingRect(self) -> QRectF:  # noqa: N802 - override de Qt
        m = self.LADO / 2 + 2
        return QRectF(-m, -m, 2 * m, 2 * m)

    def paint(self, pintor: QPainter, _opcion, _widget=None) -> None:
        pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
        lado = self.LADO
        rect = QRectF(-lado / 2, -lado / 2, lado, lado)
        pintor.setPen(QPen(QColor("#ffffff"), 2))
        pintor.setBrush(COLOR_CUARTEL)
        pintor.drawRoundedRect(rect, 6, 6)
        fuente = QFont("Segoe UI Emoji")
        fuente.setPixelSize(17)
        pintor.setFont(fuente)
        pintor.drawText(rect, Qt.AlignmentFlag.AlignCenter, "🚒")


class _MarcadorSiniestro(QGraphicsItem):
    """Chincheta roja: la punta (0, 0) es el punto exacto marcado."""

    def __init__(self, tooltip: str = "Lugar del Siniestro", escala: float = 1.0) -> None:
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setZValue(40)
        self.setToolTip(tooltip)
        self._escala = escala
        radio, alto = 11 * escala, 34 * escala
        cuerpo = QPainterPath()
        cuerpo.addEllipse(QPointF(0, -alto + radio), radio, radio)
        punta = QPainterPath()
        punta.addPolygon(QPolygonF([QPointF(-radio * 0.72, -alto + radio * 1.7), QPointF(radio * 0.72, -alto + radio * 1.7),
                                    QPointF(0, 0), QPointF(-radio * 0.72, -alto + radio * 1.7)]))
        self._forma = cuerpo.united(punta)
        self._centro = QPointF(0, -alto + radio)
        self._radio = radio

    def boundingRect(self) -> QRectF:  # noqa: N802 - override de Qt
        return self._forma.boundingRect().adjusted(-2, -2, 2, 2)

    def shape(self) -> QPainterPath:
        return self._forma

    def paint(self, pintor: QPainter, _opcion, _widget=None) -> None:
        pintor.setRenderHint(QPainter.RenderHint.Antialiasing)
        pintor.setPen(QPen(QColor("#ffffff"), 2))
        pintor.setBrush(COLOR_SINIESTRO)
        pintor.drawPath(self._forma)
        pintor.setPen(Qt.PenStyle.NoPen)
        pintor.setBrush(QColor("#ffffff"))
        pintor.drawEllipse(self._centro, self._radio * 0.38, self._radio * 0.38)


class _Vertice(QGraphicsEllipseItem):
    """Vértice arrastrable del polígono en modo edición."""

    def __init__(self, indice: int, al_mover: Callable[[int, QPointF], None]) -> None:
        super().__init__(-6, -6, 12, 12)
        self.indice = indice
        self._al_mover = al_mover
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setBrush(QColor("#ffffff"))
        self.setPen(QPen(COLOR_POLIGONO, 2))
        self.setZValue(50)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def itemChange(self, cambio, valor):  # noqa: N802 - override de Qt
        if cambio == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._al_mover(self.indice, self.pos())
        return super().itemChange(cambio, valor)


def _lapiz_cosmetico(color: QColor, ancho: float, estilo=Qt.PenStyle.SolidLine) -> QPen:
    """Grosor constante en pantalla, sin importar el zoom."""
    lapiz = QPen(color, ancho, estilo)
    lapiz.setCosmetic(True)
    lapiz.setCapStyle(Qt.PenCapStyle.RoundCap)
    lapiz.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return lapiz


# ---------------------------------------------------------------------------
# Vista: zoom con rueda, paneo con arrastre, clic sin arrastre = marcar
# ---------------------------------------------------------------------------

class _VistaMapa(QGraphicsView):
    clic = Signal(QPointF)  # posición en la escena
    doble_clic = Signal(QPointF)
    cursor_movido = Signal(QPointF)
    tecla_enter = Signal()
    tecla_escape = Signal()

    def __init__(self, escena: QGraphicsScene, parent: Optional[QWidget] = None) -> None:
        super().__init__(escena, parent)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setBackgroundBrush(QColor("#e5e9f0"))
        # Sin barras, como Avenza: se navega arrastrando (siguen funcionando ocultas).
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.rect_contenido = QRectF()
        self._ajuste_pendiente = True
        self._press_pos = None
        self._ignorar_release = False

    # -- Zoom -------------------------------------------------------------------

    def _escala_actual(self) -> float:
        return self.transform().m11()

    def _escala_ajuste(self) -> float:
        if self.rect_contenido.isEmpty():
            return 1.0
        vp = self.viewport().rect()
        return min(vp.width() / self.rect_contenido.width(), vp.height() / self.rect_contenido.height())

    def zoom(self, factor: float) -> None:
        minimo = self._escala_ajuste() * 0.5
        nueva = min(max(self._escala_actual() * factor, minimo), ZOOM_MAXIMO)
        factor = nueva / self._escala_actual()
        if abs(factor - 1.0) > 1e-6:
            self.scale(factor, factor)

    def wheelEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        pasos = evento.angleDelta().y() / 120.0
        if pasos:
            self.zoom(FACTOR_ZOOM ** pasos)
        evento.accept()

    def ver_todo(self) -> None:
        if not self.rect_contenido.isEmpty():
            self.fitInView(self.rect_contenido, Qt.AspectRatioMode.KeepAspectRatio)

    def pedir_ajuste_inicial(self) -> None:
        self._ajuste_pendiente = True
        self._intentar_ajuste_inicial()

    def _intentar_ajuste_inicial(self) -> None:
        # Mientras el widget está oculto (página no activa) el viewport mide
        # ~0 px y fitInView daría un zoom absurdo: se espera al primer
        # tamaño real.
        if self._ajuste_pendiente and self.viewport().width() > 50 and self.viewport().height() > 50:
            self._ajuste_pendiente = False
            self.ver_todo()

    def resizeEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        super().resizeEvent(evento)
        self._intentar_ajuste_inicial()

    # -- Clic vs. paneo -------------------------------------------------------------

    def mousePressEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        self._press_pos = evento.position() if evento.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(evento)

    def mouseReleaseEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        super().mouseReleaseEvent(evento)
        if evento.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        movimiento = (evento.position() - self._press_pos).manhattanLength()
        self._press_pos = None
        if self._ignorar_release:
            self._ignorar_release = False
            return
        if movimiento > TOLERANCIA_CLIC_PX:
            return  # fue un paneo, no un clic
        if isinstance(self.itemAt(evento.position().toPoint()), _Vertice):
            return  # clic sobre un vértice: lo maneja el vértice
        self.clic.emit(self.mapToScene(evento.position().toPoint()))

    def mouseDoubleClickEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        if evento.button() == Qt.MouseButton.LeftButton:
            # El release que sigue al doble clic no cuenta como otro clic.
            self._ignorar_release = True
            self._press_pos = evento.position()
            self.doble_clic.emit(self.mapToScene(evento.position().toPoint()))
        super().mouseDoubleClickEvent(evento)

    def mouseMoveEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        super().mouseMoveEvent(evento)
        self.cursor_movido.emit(self.mapToScene(evento.position().toPoint()))

    def keyPressEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        if evento.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.tecla_enter.emit()
        elif evento.key() == Qt.Key.Key_Escape:
            self.tecla_escape.emit()
        elif evento.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(FACTOR_ZOOM)
        elif evento.key() == Qt.Key.Key_Minus:
            self.zoom(1 / FACTOR_ZOOM)
        else:
            super().keyPressEvent(evento)


# ---------------------------------------------------------------------------
# Widget principal
# ---------------------------------------------------------------------------

MODO_NORMAL, MODO_DIBUJO, MODO_EDICION = "normal", "dibujo", "edicion"


class MapWidget(QWidget):
    """Visor táctico. `modo_operativo=True` lo pone en solo lectura (sin
    marcar puntos ni dibujar) para la vista general de operaciones.

    `coordenadas_cambiadas(lat, lng)` se emite solo cuando el OPERADOR
    ubica el siniestro (clic o búsqueda de dirección), no al reabrir un
    incidente guardado."""

    coordenadas_cambiadas = Signal(float, float)

    def __init__(self, parent: Optional[QWidget] = None, modo_operativo: bool = False,
                 fuente_direccion: Optional[Callable[[], Tuple[str, str]]] = None) -> None:
        super().__init__(parent)
        self.modo_operativo = modo_operativo
        # Si el formulario ya tiene el campo de dirección, el mapa busca con
        # ese texto (devuelve (dirección, localidad)) en vez de pedirlo de nuevo.
        self._fuente_direccion = fuente_direccion
        self.entry_direccion_mapa: Optional[QLineEdit] = None

        # Estado en coordenadas geográficas (sobrevive a un cambio de imagen).
        self._latitud: Optional[float] = None
        self._longitud: Optional[float] = None
        self._anillo: List[Tuple[float, float]] = []  # (lon, lat), sin repetir el primero
        self._superficie_ha: float = 0.0
        self._perimetro_m: float = 0.0
        self._numero_parte_actual = ""
        self._modo = MODO_NORMAL
        self._solo_lectura: List[Tuple[float, float, str, Optional[str]]] = []

        self._hilo_geocode: Optional[Tuple[QThread, GeocodeWorker]] = None

        # Ítems de la escena (se recrean en _redibujar_superposiciones).
        self._item_fondo: Optional[QGraphicsPixmapItem] = None
        self._item_cuartel: Optional[_MarcadorCuartel] = None
        self._item_siniestro: Optional[_MarcadorSiniestro] = None
        self._items_linea: List[QGraphicsLineItem] = []
        self._item_poligono: Optional[QGraphicsPolygonItem] = None
        self._item_trazo: Optional[QGraphicsPathItem] = None
        self._vertices: List[_Vertice] = []
        self._items_solo_lectura: List[QGraphicsItem] = []

        self.escena = QGraphicsScene(self)
        self.vista = _VistaMapa(self.escena, self)
        self.calibracion = CalibracionMapa()
        self.georreferencia = Georreferencia(self.calibracion, 1, 1)
        self.hay_imagen_real = False

        self._construir_ui()
        self.recargar_mapa()

    # -- Construcción de la UI ------------------------------------------------

    def _construir_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if not self.modo_operativo:
            layout.addLayout(self._crear_barra_busqueda())

        self.vista.setMinimumHeight(320)
        layout.addWidget(self.vista, 1)
        self._crear_superposiciones_vista()

        self.vista.clic.connect(self._on_clic_mapa)
        self.vista.doble_clic.connect(self._on_doble_clic_mapa)
        self.vista.cursor_movido.connect(self._on_cursor_movido)
        self.vista.tecla_enter.connect(self._terminar_dibujo)
        self.vista.tecla_escape.connect(self._on_escape)

        layout.addLayout(self._crear_barra_herramientas())
        if not self.modo_operativo:
            layout.addSpacing(6)
            layout.addWidget(self._crear_panel_datos())

    def _crear_barra_busqueda(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        if self._fuente_direccion is None:
            fila.addWidget(QLabel("Dirección:"))
            self.entry_direccion_mapa = QLineEdit(self)
            self.entry_direccion_mapa.setPlaceholderText('Ej: "Belgrano 58" o "Ruta 24 km 45"…')
            self.entry_direccion_mapa.returnPressed.connect(self._buscar_direccion)
            fila.addWidget(self.entry_direccion_mapa, 1)
            boton_buscar = QPushButton("🔍 Buscar dirección", self)
        else:
            boton_buscar = QPushButton("📍 Ubicar la dirección en el mapa", self)
            boton_buscar.setToolTip("Busca la 'Dirección / Lugar del hecho' cargada arriba (también con Enter).")
        self.boton_buscar = boton_buscar
        boton_buscar.clicked.connect(self._buscar_direccion)
        fila.addWidget(boton_buscar)
        if self._fuente_direccion is not None:
            fila.addStretch(1)

        boton_importar = QPushButton("📂 Importar KML/KMZ/GPX", self)
        boton_importar.clicked.connect(self._importar_geometria)
        fila.addWidget(boton_importar)

        boton_exportar = QPushButton("💾 Exportar KML", self)
        boton_exportar.clicked.connect(self._exportar_kml)
        fila.addWidget(boton_exportar)

        for boton in (boton_buscar, boton_importar, boton_exportar):
            boton.setObjectName("botonHerramienta")
        return fila

    def _crear_superposiciones_vista(self) -> None:
        """Rótulos flotantes sobre el mapa: distancia (arriba) y coordenadas
        bajo el cursor (abajo), como en Avenza."""
        estilo = ("background: rgba(255,255,255,0.93); color: #1a1d21; border: 1px solid #cfd6e2;"
                  " border-radius: 6px; padding: 4px 9px; font: 12px 'Segoe UI';")
        self.label_distancia_mapa = QLabel(self.vista.viewport())
        self.label_distancia_mapa.setStyleSheet(estilo)
        self.label_distancia_mapa.move(10, 10)
        self.label_distancia_mapa.hide()
        self.label_cursor = QLabel(self.vista.viewport())
        self.label_cursor.setStyleSheet(estilo.replace("12px", "11px"))
        self.label_cursor.hide()
        self.vista.viewport().installEventFilter(self)

    def eventFilter(self, objeto, evento) -> bool:  # noqa: N802 - override de Qt
        if objeto is self.vista.viewport():
            if evento.type() == QEvent.Type.Resize:
                self._ubicar_label_cursor()
            elif evento.type() == QEvent.Type.Leave:
                self.label_cursor.hide()
        return super().eventFilter(objeto, evento)

    def _ubicar_label_cursor(self) -> None:
        self.label_cursor.adjustSize()
        self.label_cursor.move(10, self.vista.viewport().height() - self.label_cursor.height() - 10)

    def _crear_barra_herramientas(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        botones: List[QPushButton] = []

        def boton(texto: str, accion, tooltip: str = "") -> QPushButton:
            b = QPushButton(texto, self)
            b.setToolTip(tooltip)
            b.clicked.connect(accion)
            fila.addWidget(b)
            botones.append(b)
            return b

        if not self.modo_operativo:
            boton("✖ Quitar Marcador", self._quitar_marcador,
                  "Un clic en el mapa ubica (o mueve) el Lugar del Siniestro.")
            self.boton_dibujar = boton("▱ Dibujar Polígono", self._alternar_dibujo,
                                       "Clic para cada vértice; doble clic, Enter o este botón para terminar; Esc cancela.")
            boton("✏ Editar Vértices", self._habilitar_edicion_poligono,
                  "Arrastrá los vértices; un clic fuera de ellos termina la edición.")
            boton("🗑 Borrar Polígono", self._borrar_poligono)
        fila.addStretch(1)
        boton("＋", lambda: self.vista.zoom(FACTOR_ZOOM), "Acercar (rueda del mouse)")
        boton("－", lambda: self.vista.zoom(1 / FACTOR_ZOOM), "Alejar (rueda del mouse)")
        boton("⤢ Ver todo", self.vista.ver_todo, "Ver la imagen completa")
        boton("🚒 Cuartel", self._centrar_en_cuartel, "Centrar en el cuartel")
        for b in botones:
            b.setObjectName("botonHerramienta")
        return fila

    def _crear_panel_datos(self) -> QWidget:
        caja = QGroupBox("", self)
        grid = QGridLayout(caja)

        grid.addWidget(QLabel("Coordenadas:"), 0, 0)
        self.label_coordenadas = QLabel("(sin marcar)", caja)
        grid.addWidget(self.label_coordenadas, 0, 1)

        grid.addWidget(QLabel("Superficie afectada:"), 0, 2)
        self.label_superficie = QLabel("0.00 ha (0 m²)", caja)
        grid.addWidget(self.label_superficie, 0, 3)

        grid.addWidget(QLabel("Perímetro:"), 0, 4)
        self.label_perimetro = QLabel("0 m", caja)
        grid.addWidget(self.label_perimetro, 0, 5)

        grid.addWidget(QLabel("Distancia desde el cuartel:"), 1, 0)
        self.label_distancia = QLabel("(marcá el lugar del siniestro)", caja)
        grid.addWidget(self.label_distancia, 1, 1, 1, 5)
        return caja

    # -- Calibración y conversión píxel <-> GPS ---------------------------------------

    def pixel_to_coords(self, x: float, y: float) -> Tuple[float, float]:
        """Píxel de la imagen base -> (lat, lon)."""
        return self.georreferencia.pixel_to_coords(x, y)

    def coords_to_pixel(self, lat: float, lon: float) -> Tuple[float, float]:
        """(lat, lon) -> píxel de la imagen base (puede caer fuera si el punto
        está fuera del recuadro calibrado)."""
        return self.georreferencia.coords_to_pixel(lat, lon)

    def _punto_escena(self, lat: float, lon: float) -> QPointF:
        x, y = self.coords_to_pixel(lat, lon)
        return QPointF(x, y)

    def recargar_mapa(self) -> None:
        """Vuelve a leer imagen y calibración (tras cambiarlas en
        Configuración) y reproyecta todo lo marcado."""
        self.calibracion = cartografia.cargar_calibracion()
        pixmap, self.hay_imagen_real = cargar_pixmap_base(self.calibracion)
        self.georreferencia = Georreferencia(self.calibracion, pixmap.width(), pixmap.height())

        self._salir_de_modos()
        self.escena.clear()
        self._item_siniestro = self._item_poligono = self._item_trazo = None
        self._items_linea, self._vertices, self._items_solo_lectura = [], [], []

        self._item_fondo = self.escena.addPixmap(pixmap)
        self._item_fondo.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._item_fondo.setZValue(0)
        rect = QRectF(pixmap.rect())
        self.vista.rect_contenido = rect
        # Margen alrededor para poder panear un poco más allá del borde.
        margen = max(rect.width(), rect.height()) * 0.15
        self.escena.setSceneRect(rect.adjusted(-margen, -margen, margen, margen))

        self._item_cuartel = _MarcadorCuartel()
        self._item_cuartel.setPos(self._punto_escena(self.calibracion.cuartel_lat, self.calibracion.cuartel_lon))
        self.escena.addItem(self._item_cuartel)

        self._redibujar_superposiciones()
        for lat, lon, etiqueta, geojson in self._solo_lectura:
            self._dibujar_solo_lectura(lat, lon, etiqueta, geojson)
        self.vista.pedir_ajuste_inicial()

    def _centrar_en_cuartel(self) -> None:
        if self._item_cuartel is not None:
            self.vista.centerOn(self._item_cuartel.pos())

    # -- Clics sobre el mapa ------------------------------------------------------------

    def _on_clic_mapa(self, punto: QPointF) -> None:
        if self.modo_operativo:
            return
        lat, lon = self.pixel_to_coords(punto.x(), punto.y())
        if self._modo == MODO_DIBUJO:
            self._anillo.append((lon, lat))
            self._redibujar_poligono()
            return
        if self._modo == MODO_EDICION:
            self._salir_de_modos()  # clic fuera de los vértices = terminar edición
            return
        self._on_marcador_cambiado(lat, lon)

    def _on_doble_clic_mapa(self, _punto: QPointF) -> None:
        if self._modo == MODO_DIBUJO:
            self._terminar_dibujo()

    def _on_cursor_movido(self, punto: QPointF) -> None:
        lat, lon = self.pixel_to_coords(punto.x(), punto.y())
        self.label_cursor.setText(f"{lat:.5f}, {lon:.5f}")
        self._ubicar_label_cursor()
        self.label_cursor.show()

    def _on_escape(self) -> None:
        if self._modo == MODO_DIBUJO:
            self._anillo = []
            self._salir_de_modos()
            self._on_poligono_cambiado()
        elif self._modo == MODO_EDICION:
            self._salir_de_modos()

    # -- Marcador del siniestro + línea al cuartel -------------------------------------

    def _on_marcador_cambiado(self, lat: float, lng: float) -> None:
        self._latitud, self._longitud = lat, lng
        self._redibujar_siniestro()
        self.coordenadas_cambiadas.emit(lat, lng)

    def _quitar_marcador(self) -> None:
        self._latitud = self._longitud = None
        self._redibujar_siniestro()

    def _redibujar_siniestro(self) -> None:
        for item in [self._item_siniestro, *self._items_linea]:
            if item is not None:
                self.escena.removeItem(item)
        self._item_siniestro, self._items_linea = None, []

        if self._latitud is None or self._longitud is None:
            self.label_distancia_mapa.hide()
            self._set_texto_panel("label_coordenadas", "(sin marcar)")
            self._set_texto_panel("label_distancia", "(marcá el lugar del siniestro)")
            return

        destino = self._punto_escena(self._latitud, self._longitud)
        origen = self._item_cuartel.pos()
        # Contorno blanco debajo: la línea se lee sobre cualquier carta.
        for lapiz, z in ((_lapiz_cosmetico(QColor("#ffffff"), 6), 20),
                         (_lapiz_cosmetico(COLOR_LINEA, 3, Qt.PenStyle.DashLine), 21)):
            linea = self.escena.addLine(origen.x(), origen.y(), destino.x(), destino.y(), lapiz)
            linea.setZValue(z)
            self._items_linea.append(linea)

        self._item_siniestro = _MarcadorSiniestro()
        self._item_siniestro.setPos(destino)
        self.escena.addItem(self._item_siniestro)

        km = cartografia.distancia_geodesica_km(
            self.calibracion.cuartel_lat, self.calibracion.cuartel_lon, self._latitud, self._longitud
        )
        texto = f"Distancia: {cartografia.formatear_distancia(km)}"
        fuera = not self.georreferencia.contiene(self._latitud, self._longitud)
        self.label_distancia_mapa.setText(
            f"🚒 → 📍 <b>{texto}</b> <span style='color:#667'>(línea recta)</span>"
            + ("<br><span style='color:#e65100'>⚠ Fuera del área de la imagen</span>" if fuera else "")
        )
        self.label_distancia_mapa.adjustSize()
        self.label_distancia_mapa.show()
        self._set_texto_panel("label_coordenadas", f"{self._latitud:.6f}, {self._longitud:.6f}")
        self._set_texto_panel("label_distancia", f"{cartografia.formatear_distancia(km)} en línea recta"
                              + (" (fuera del área de la imagen)" if fuera else ""))

    def _set_texto_panel(self, nombre: str, texto: str) -> None:
        label = getattr(self, nombre, None)
        if label is not None:
            label.setText(texto)

    # -- Polígono del área afectada ------------------------------------------------------

    def _alternar_dibujo(self) -> None:
        if self._modo == MODO_DIBUJO:
            self._terminar_dibujo()
            return
        self._salir_de_modos()
        self._anillo = []
        self._modo = MODO_DIBUJO
        self.vista.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.vista.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.vista.setFocus()
        self.boton_dibujar.setText("✔ Terminar Polígono")
        self._on_poligono_cambiado()

    def _terminar_dibujo(self) -> None:
        if self._modo != MODO_DIBUJO:
            return
        # El doble clic suele dejar dos vértices casi en el mismo lugar.
        while len(self._anillo) >= 2 and self._vertices_iguales(self._anillo[-1], self._anillo[-2]):
            self._anillo.pop()
        if len(self._anillo) < 3:
            self._anillo = []
        self._salir_de_modos()
        self._on_poligono_cambiado()

    def _vertices_iguales(self, a: Tuple[float, float], b: Tuple[float, float]) -> bool:
        pa, pb = self._punto_escena(a[1], a[0]), self._punto_escena(b[1], b[0])
        escala = self.vista.transform().m11() or 1.0
        return (pa - pb).manhattanLength() * escala <= TOLERANCIA_CLIC_PX

    def _habilitar_edicion_poligono(self) -> None:
        if len(self._anillo) < 3:
            return
        self._salir_de_modos()
        self._modo = MODO_EDICION
        for i, (lon, lat) in enumerate(self._anillo):
            vertice = _Vertice(i, self._on_vertice_movido)
            vertice.setPos(self._punto_escena(lat, lon))
            self.escena.addItem(vertice)
            self._vertices.append(vertice)

    def _on_vertice_movido(self, indice: int, pos: QPointF) -> None:
        if self._modo != MODO_EDICION or indice >= len(self._anillo):
            return
        lat, lon = self.pixel_to_coords(pos.x(), pos.y())
        self._anillo[indice] = (lon, lat)
        self._on_poligono_cambiado()

    def _salir_de_modos(self) -> None:
        for vertice in self._vertices:
            if vertice.scene() is not None:
                self.escena.removeItem(vertice)
        self._vertices = []
        self._modo = MODO_NORMAL
        # En este orden: setDragMode pone la mano de paneo en el viewport.
        self.vista.viewport().unsetCursor()
        self.vista.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        if hasattr(self, "boton_dibujar"):
            self.boton_dibujar.setText("▱ Dibujar Polígono")

    def _borrar_poligono(self) -> None:
        self._salir_de_modos()
        self._anillo = []
        self._on_poligono_cambiado()

    def _on_poligono_cambiado(self) -> None:
        cerrado = len(self._anillo) >= 3 and self._modo != MODO_DIBUJO
        if cerrado:
            self._superficie_ha = round(_area_geodesica_ha([list(p) for p in self._anillo]), 2)
            self._perimetro_m = _perimetro_geodesico_m(self._anillo)
        elif self._modo == MODO_DIBUJO or not self._anillo:
            self._superficie_ha = self._perimetro_m = 0.0
        self._redibujar_poligono()
        self._actualizar_labels_poligono()

    def _redibujar_poligono(self) -> None:
        for item in (self._item_poligono, self._item_trazo):
            if item is not None:
                self.escena.removeItem(item)
        self._item_poligono = self._item_trazo = None
        if not self._anillo:
            return
        puntos = QPolygonF([self._punto_escena(lat, lon) for lon, lat in self._anillo])
        if self._modo == MODO_DIBUJO:
            camino = QPainterPath()
            camino.addPolygon(puntos)
            self._item_trazo = self.escena.addPath(camino, _lapiz_cosmetico(COLOR_POLIGONO, 2, Qt.PenStyle.DashLine))
            self._item_trazo.setZValue(10)
            for p in puntos:  # vértices ya marcados, como referencia
                punto = self.escena.addEllipse(-4, -4, 8, 8, QPen(COLOR_POLIGONO, 2), QBrush(QColor("#ffffff")))
                punto.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
                punto.setPos(p)
                punto.setZValue(11)
                punto.setParentItem(self._item_trazo)
            return
        relleno = QColor(COLOR_POLIGONO)
        relleno.setAlpha(64)
        self._item_poligono = self.escena.addPolygon(puntos, _lapiz_cosmetico(COLOR_POLIGONO, 3), QBrush(relleno))
        self._item_poligono.setZValue(10)

    def _actualizar_labels_poligono(self) -> None:
        area_m2 = self._superficie_ha * 10_000
        self._set_texto_panel("label_superficie", f"{self._superficie_ha:.2f} ha ({area_m2:,.0f} m²)".replace(",", "."))
        if self._perimetro_m >= 1000:
            self._set_texto_panel("label_perimetro", f"{self._perimetro_m:.0f} m ({self._perimetro_m / 1000:.2f} km)")
        else:
            self._set_texto_panel("label_perimetro", f"{self._perimetro_m:.0f} m")

    def _geometria_geojson(self) -> Optional[str]:
        if len(self._anillo) < 3:
            return None
        anillo = [[lon, lat] for lon, lat in self._anillo]
        anillo.append(anillo[0])
        return json.dumps({"type": "Feature", "properties": {},
                           "geometry": {"type": "Polygon", "coordinates": [anillo]}})

    # -- Búsqueda de dirección (geocodificación) --------------------------------

    def _buscar_direccion(self) -> None:
        if self.modo_operativo:
            return
        if self._fuente_direccion is not None:
            direccion, localidad = self._fuente_direccion()
            direccion = direccion.strip()
            localidad = f"{localidad.strip()}, Córdoba, Argentina" if localidad.strip() else None
        else:
            direccion, localidad = self.entry_direccion_mapa.text().strip(), None
        if not direccion or self._hilo_geocode is not None:
            return

        self._habilitar_busqueda(False)
        hilo, worker = (lanzar_geocodificacion(direccion, localidad) if localidad
                        else lanzar_geocodificacion(direccion))
        self._hilo_geocode = (hilo, worker)
        worker.resultado.connect(self._on_resultado_geocode)
        hilo.finished.connect(lambda: setattr(self, "_hilo_geocode", None))

    def _habilitar_busqueda(self, habilitado: bool) -> None:
        for widget in (self.entry_direccion_mapa, getattr(self, "boton_buscar", None)):
            if widget is not None:
                widget.setEnabled(habilitado)

    def _on_resultado_geocode(self, exito: bool, lat: float, lng: float, mensaje_error: str) -> None:
        self._habilitar_busqueda(True)
        if not exito:
            QMessageBox.warning(self, "No se encontró la dirección", mensaje_error or "Intentá con otro texto.")
            return
        self._on_marcador_cambiado(lat, lng)
        if self._item_siniestro is not None:
            self.vista.centerOn(self._item_siniestro.pos())

    # -- Importar / exportar KML, KMZ, GPX ------------------------------------

    def _importar_geometria(self) -> None:
        ruta_texto, _ = QFileDialog.getOpenFileName(
            self, "Importar geometría de campo", "", "KML/KMZ/GPX (*.kml *.kmz *.gpx)"
        )
        if not ruta_texto:
            return
        try:
            geojson = geografia.importar_geometria(Path(ruta_texto))
            self._cargar_anillo_geojson(json.dumps(geojson))
        except Exception as e:  # noqa: BLE001 - cualquier archivo mal formado se reporta, no crashea
            QMessageBox.warning(self, "No se pudo importar", str(e))
            return
        if self._item_poligono is not None:
            self.vista.fitInView(self._item_poligono.boundingRect().adjusted(-80, -80, 80, 80),
                                 Qt.AspectRatioMode.KeepAspectRatio)

    def _cargar_anillo_geojson(self, geojson_texto: Optional[str]) -> None:
        self._salir_de_modos()
        self._anillo = _anillo_de_geojson(geojson_texto) if geojson_texto else []
        self._on_poligono_cambiado()

    def _exportar_kml(self) -> None:
        geojson = self._geometria_geojson()
        if self._latitud is None and not geojson:
            QMessageBox.information(self, "Nada para exportar", "Marcá un punto o dibujá un polígono primero.")
            return

        ruta_texto, _ = QFileDialog.getSaveFileName(self, "Exportar KML", "siniestro.kml", "KML (*.kml)")
        if not ruta_texto:
            return

        kml = geografia.exportar_kml(self._numero_parte_actual, self._latitud, self._longitud, geojson)
        try:
            Path(ruta_texto).write_text(kml, encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "Error al exportar", str(e))
            return
        QMessageBox.information(self, "KML exportado", f"Se guardó en:\n{ruta_texto}")

    # -- Capa de solo lectura (vista de operaciones) ------------------------------

    def limpiar_solo_lectura(self) -> None:
        for item in self._items_solo_lectura:
            if item.scene() is not None:
                self.escena.removeItem(item)
        self._items_solo_lectura = []
        self._solo_lectura = []

    def agregar_incidente_solo_lectura(self, lat: float, lon: float, etiqueta: str,
                                       geojson: Optional[str] = None) -> None:
        self._solo_lectura.append((lat, lon, etiqueta, geojson))
        self._dibujar_solo_lectura(lat, lon, etiqueta, geojson)

    def _dibujar_solo_lectura(self, lat: float, lon: float, etiqueta: str, geojson: Optional[str]) -> None:
        if geojson:
            try:
                anillo = _anillo_de_geojson(geojson)
            except (ValueError, KeyError, TypeError, IndexError):
                anillo = []
            if len(anillo) >= 3:
                relleno = QColor(COLOR_POLIGONO)
                relleno.setAlpha(38)
                poligono = self.escena.addPolygon(
                    QPolygonF([self._punto_escena(la, lo) for lo, la in anillo]),
                    _lapiz_cosmetico(COLOR_POLIGONO, 2), QBrush(relleno),
                )
                poligono.setZValue(9)
                self._items_solo_lectura.append(poligono)
        marcador = _MarcadorSiniestro(tooltip=f"Parte N° {etiqueta}" if etiqueta else "Siniestro", escala=0.8)
        marcador.setPos(self._punto_escena(lat, lon))
        self.escena.addItem(marcador)
        self._items_solo_lectura.append(marcador)

    # -- API pública para main_window.py ---------------------------------------

    def _redibujar_superposiciones(self) -> None:
        self._redibujar_siniestro()
        self._redibujar_poligono()

    def cargar_incidente(
        self, numero_parte: str, lat: Optional[float], lng: Optional[float],
        superficie_ha: Optional[float], geometria_geojson: Optional[str],
    ) -> None:
        """Muestra los datos geográficos de un incidente ya guardado (al
        reabrir/editar). No emite `coordenadas_cambiadas`."""
        self._numero_parte_actual = numero_parte
        self._latitud, self._longitud = lat, lng
        self._redibujar_siniestro()
        try:
            self._cargar_anillo_geojson(geometria_geojson)
        except (ValueError, KeyError, TypeError, IndexError) as e:
            print(f"[Mapa] Geometría guardada ilegible ({e}); se ignora.")
            self._cargar_anillo_geojson(None)
        if not self._anillo:
            # Superficie cargada sin polígono (estimada a ojo): se respeta.
            self._superficie_ha = superficie_ha or 0.0
            self._actualizar_labels_poligono()
        if self._item_siniestro is not None:
            self.vista.centerOn(self._item_siniestro.pos())

    def limpiar(self) -> None:
        """Resetea el mapa para cargar un incidente nuevo (llamado desde
        `MainWindow._limpiar_formulario()`)."""
        self.cargar_incidente("", None, None, None, None)

    # -- API del selector de punto (app/ui/map_dialog.py) ------------------------------

    def punto(self) -> Optional[Tuple[float, float]]:
        """(lat, lon) del Lugar del Siniestro, o None si no está marcado."""
        if self._latitud is None or self._longitud is None:
            return None
        return self._latitud, self._longitud

    def fijar_punto(self, lat: float, lon: float, emitir: bool = True) -> None:
        """Coloca la chincheta en coordenadas escritas a mano y centra la
        vista ahí. `emitir`: avisar como si el operador hubiera hecho clic."""
        self._latitud, self._longitud = lat, lon
        self._redibujar_siniestro()
        self.centrar_en(lat, lon)
        if emitir:
            self.coordenadas_cambiadas.emit(lat, lon)

    def centrar_en(self, lat: float, lon: float) -> None:
        self.vista.centerOn(self._punto_escena(lat, lon))

    def encuadrar(self, lat: float, lon: float, ancho_km: float = 3.0) -> None:
        """Zoom de barrio (~`ancho_km` de ancho en pantalla) centrado en el
        punto: la vista inicial del selector y la escala de la captura. Con
        la imagen entera en pantalla no se podría ni centrar ni leer el lugar."""
        grados_lon = ancho_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
        x0, _ = self.coords_to_pixel(lat, lon - grados_lon / 2)
        x1, _ = self.coords_to_pixel(lat, lon + grados_lon / 2)
        ancho_px = abs(x1 - x0)
        if ancho_px > 0:
            escala = self.vista.viewport().width() / ancho_px
            escala = min(max(escala, self.vista._escala_ajuste() * 0.5), ZOOM_MAXIMO)
            self.vista.resetTransform()
            self.vista.scale(escala, escala)
        self.centrar_en(lat, lon)

    def dentro_del_mapa(self, lat: float, lon: float) -> bool:
        """True si el punto cae dentro de la imagen calibrada."""
        return self.georreferencia.contiene(lat, lon)

    def exportar_imagen(self, ruta: Path, ancho: int = 1600, alto: int = 1000, leyenda: str = "") -> Path:
        """PNG/JPG del área del mapa CENTRADA en la chincheta, con la misma
        escala que tiene la vista en pantalla (lo que el operador está viendo)
        y una franja inferior con la leyenda y las coordenadas.

        Se dibuja la ESCENA (no una captura de la ventana): no salen el
        cursor, los carteles flotantes ni las barras, y la chincheta y el
        cuartel mantienen su tamaño (ignoran el zoom)."""
        if self._item_siniestro is None:
            raise ValueError("No hay un punto marcado en el mapa.")
        centro = self._item_siniestro.pos()
        visible = self.vista.mapToScene(self.vista.viewport().rect()).boundingRect()
        # Mismo zoom que en pantalla, recortado a la proporción de la imagen de salida.
        alto_fuente = max(visible.height(), 1.0)
        ancho_fuente = alto_fuente * ancho / alto
        if ancho_fuente < visible.width():
            ancho_fuente = visible.width()
            alto_fuente = ancho_fuente * alto / ancho
        fuente = QRectF(centro.x() - ancho_fuente / 2, centro.y() - alto_fuente / 2, ancho_fuente, alto_fuente)

        banda = 56
        imagen = QImage(ancho, alto + banda, QImage.Format.Format_ARGB32)
        imagen.fill(QColor("#e5e9f0"))
        pintor = QPainter(imagen)
        try:
            pintor.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                                  | QPainter.RenderHint.TextAntialiasing)
            self.escena.render(pintor, QRectF(0, 0, ancho, alto), fuente, Qt.AspectRatioMode.IgnoreAspectRatio)
            # Retícula fina sobre el punto: se ubica aunque la chincheta tape el lugar.
            pintor.setPen(QPen(QColor(255, 255, 255, 200), 3))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                pintor.drawLine(QPointF(ancho / 2 + dx * 14, alto / 2 + dy * 14),
                                QPointF(ancho / 2 + dx * 40, alto / 2 + dy * 40))
            pintor.setPen(QPen(COLOR_SINIESTRO, 1.5))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                pintor.drawLine(QPointF(ancho / 2 + dx * 14, alto / 2 + dy * 14),
                                QPointF(ancho / 2 + dx * 40, alto / 2 + dy * 40))
            # Franja con leyenda y coordenadas.
            pintor.fillRect(QRectF(0, alto, ancho, banda), QColor("#1f2937"))
            pintor.setPen(QColor("#ffffff"))
            fuente_texto = QFont("Segoe UI")
            fuente_texto.setPixelSize(22)
            pintor.setFont(fuente_texto)
            lat, lon = self._latitud, self._longitud
            texto = f"📍 {lat:.6f}, {lon:.6f}   ({formatear_dms(lat, lon)})"
            if leyenda:
                texto = f"{leyenda}   ·   {texto}"
            pintor.drawText(QRectF(18, alto, ancho - 36, banda),
                            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, texto)
        finally:
            pintor.end()

        ruta = Path(ruta)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        if not imagen.save(str(ruta)):
            raise OSError(f"No se pudo guardar la imagen del mapa en {ruta}")
        return ruta

    def obtener_datos(self) -> dict:
        """Lo que hay que persistir en `incidentes` al guardar."""
        return {
            "latitud": self._latitud,
            "longitud": self._longitud,
            "superficie_ha": self._superficie_ha if self._superficie_ha else None,
            "geometria_geojson": self._geometria_geojson(),
        }


# ---------------------------------------------------------------------------
# Geometría
# ---------------------------------------------------------------------------

def formatear_dms(lat: float, lon: float) -> str:
    """-33.6315, -64.0152 -> 33°37'53.4"S 64°00'54.7"W (como lo lee un GPS)."""
    def parte(valor: float, positivo: str, negativo: str) -> str:
        absoluto = abs(valor)
        grados = int(absoluto)
        minutos_float = (absoluto - grados) * 60
        minutos = int(minutos_float)
        segundos = (minutos_float - minutos) * 60
        if round(segundos, 1) >= 60:
            segundos, minutos = 0.0, minutos + 1
        return f"{grados}°{minutos:02d}'{segundos:04.1f}\"{positivo if valor >= 0 else negativo}"
    return f"{parte(lat, 'N', 'S')} {parte(lon, 'E', 'W')}"


def _anillo_de_geojson(geojson_texto: str) -> List[Tuple[float, float]]:
    """Anillo exterior (lon, lat) del primer polígono de un GeoJSON
    (Feature, FeatureCollection, Polygon o MultiPolygon), sin cerrar."""
    datos = json.loads(geojson_texto)
    if datos.get("type") == "FeatureCollection":
        datos = next((f for f in datos["features"] if f.get("geometry")), None)
        if datos is None:
            raise ValueError("El GeoJSON no tiene ninguna geometría.")
    geometria = datos.get("geometry", datos)
    coordenadas = geometria["coordinates"]
    if geometria["type"] == "MultiPolygon":
        coordenadas = coordenadas[0]
    anillo = [(float(p[0]), float(p[1])) for p in coordenadas[0]]
    if len(anillo) > 1 and anillo[0] == anillo[-1]:
        anillo.pop()
    return anillo


def _perimetro_geodesico_m(anillo: List[Tuple[float, float]]) -> float:
    total = 0.0
    for i, (lon1, lat1) in enumerate(anillo):
        lon2, lat2 = anillo[(i + 1) % len(anillo)]
        total += cartografia.distancia_geodesica_km(lat1, lon1, lat2, lon2) * 1000
    return total


def _area_geodesica_ha(anillo_lon_lat: list) -> float:
    """Superficie esférica de un polígono (fórmula del exceso esférico, la
    misma que usaba Leaflet.draw). `anillo_lon_lat`: lista de [lon, lat]."""
    radio_tierra_m = 6378137.0
    puntos = [(math.radians(lon), math.radians(lat)) for lon, lat in anillo_lon_lat]
    if puntos[0] != puntos[-1]:
        puntos.append(puntos[0])

    area = 0.0
    for i in range(len(puntos) - 1):
        lon1, lat1 = puntos[i]
        lon2, lat2 = puntos[i + 1]
        area += (lon2 - lon1) * (2 + math.sin(lat1) + math.sin(lat2))
    area = abs(area * radio_tierra_m * radio_tierra_m / 2.0)
    return area / 10_000  # m² -> ha


# ---------------------------------------------------------------------------
# Vista general de operaciones
# ---------------------------------------------------------------------------

class OperationsMapWindow(QWidget):
    """Página "🗺️ Cartografía Táctica" del dashboard: mapa de solo lectura
    con todos los incidentes que tienen coordenadas guardadas. El nombre se
    mantiene por compatibilidad; ya no es una ventana aparte."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        boton_actualizar = QPushButton("Actualizar", self)
        boton_actualizar.clicked.connect(self.refrescar)
        layout.addWidget(boton_actualizar, 0)

        self.mapa = MapWidget(self, modo_operativo=True)
        layout.addWidget(self.mapa, 1)

    def refrescar(self) -> None:
        from app.db import get_session
        from app.models import Incidente

        with get_session() as session:
            incidentes = (
                session.query(Incidente)
                .filter(Incidente.latitud.isnot(None), Incidente.longitud.isnot(None))
                .all()
            )
            datos = [(inc.numero_parte, inc.latitud, inc.longitud, inc.geometria_geojson) for inc in incidentes]

        self.mapa.limpiar_solo_lectura()
        for numero_parte, lat, lng, geojson in datos:
            self.mapa.agregar_incidente_solo_lectura(lat, lng, numero_parte or "", geojson)
