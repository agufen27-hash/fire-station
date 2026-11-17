"""
Visor de mapa interactivo: Leaflet + OpenStreetMap dentro de un
QWebEngineView (app/ui/map_assets/mapa.html). Sin API keys: teselas de
OpenStreetMap (calles) y Esri World Imagery (satelital), con zoom y paneo
libres a cualquier escala.

Todo se maneja en WGS84 (latitud/longitud): Leaflet proyecta a Web Mercator
solo para dibujar, así que no hay calibración de imagen ni desfasaje.

  - Rueda: zoom. Arrastrar: paneo. Doble clic: acercar.
  - Cuartel: marcador fijo en las coordenadas de Configuración.
  - Clic en el mapa: coloca o mueve el Lugar del Siniestro (chincheta roja),
    traza la línea cuartel -> siniestro con la distancia geodésica y emite
    `coordenadas_cambiadas` para completar "Zona / Coordenadas".
  - Polígono del área afectada (superficie y perímetro geodésicos) e
    import/export KML/KMZ/GPX.

Python es el dueño del estado (punto, polígono, modos); la página solo
dibuja lo que se le manda con `runJavaScript` (`fs.*`) y avisa clics, teclas
y vértices movidos por QWebChannel (`_PuenteMapa`). Las llamadas hechas
antes de que la página termine de cargar se encolan y se ejecutan en orden.

Las teselas quedan en un caché de disco (data/mapa_web/): las zonas ya
vistas vuelven a verse sin internet mientras el caché las conserve.

Un mismo `MapWidget`, dos usos:
  - Incrustado en el formulario de carga (`main_window.py`): editable.
  - `OperationsMapWindow`: solo lectura, con todos los incidentes que
    tienen coordenadas guardadas.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from PySide6.QtCore import QEventLoop, QObject, QPointF, QRectF, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPen
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
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

from app import __version__
from app.paths import get_resource_path, get_writable_dir
from app.services import cartografia, geografia
from app.services.cartografia import CalibracionMapa
from app.services.geocodificacion import GeocodeWorker, lanzar_geocodificacion

# Valores de fábrica (la ubicación real del cuartel se lee de data/config.json).
CUARTEL_LAT = CalibracionMapa.cuartel_lat
CUARTEL_LON = CalibracionMapa.cuartel_lon

RUTA_PAGINA_MAPA = "app/ui/map_assets/mapa.html"
ANCHO_VISTA_INICIAL_KM = 6.0  # vista de arranque: el pueblo entero alrededor del cuartel
TOLERANCIA_VERTICE_M = 0.5  # vértices más cercanos que esto son el mismo (doble clic)
TIMEOUT_CAPTURA_MS = 8000  # espera máxima a que carguen las teselas antes de capturar
CACHE_TESELAS_BYTES = 400 * 1024 * 1024

COLOR_SINIESTRO = QColor("#d32f2f")

MODO_NORMAL, MODO_DIBUJO, MODO_EDICION = "normal", "dibujo", "edicion"


# ---------------------------------------------------------------------------
# Perfil web compartido (caché de teselas persistente)
# ---------------------------------------------------------------------------

_PERFIL: Optional[QWebEngineProfile] = None


def perfil_mapa() -> QWebEngineProfile:
    """Perfil con nombre (no el off-the-record por defecto): caché HTTP en
    disco para que las teselas ya vistas se carguen al instante y, mientras
    el caché las conserve, también sin internet. User-Agent propio: la
    política de uso de tile.openstreetmap.org pide identificar la app."""
    global _PERFIL
    if _PERFIL is None:
        carpeta = get_writable_dir("data") / "mapa_web"
        perfil = QWebEngineProfile("fire_station_mapa", QApplication.instance())
        perfil.setPersistentStoragePath(str(carpeta))
        perfil.setCachePath(str(carpeta / "cache"))
        perfil.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        perfil.setHttpCacheMaximumSize(CACHE_TESELAS_BYTES)
        perfil.setHttpUserAgent(f"FireStation/{__version__} (Bomberos Voluntarios; mapa de guardia)")
        _PERFIL = perfil
    return _PERFIL


class _PaginaMapa(QWebEnginePage):
    """La vista nunca navega fuera de mapa.html: un link (atribución,
    tooltip) se abre en el navegador del sistema."""

    def acceptNavigationRequest(self, url: QUrl, tipo, es_principal: bool) -> bool:  # noqa: N802 - override de Qt
        if url.isLocalFile() or url.scheme() in ("data", "about", "qrc"):
            return True
        if es_principal:
            QDesktopServices.openUrl(url)
        return False

    def createWindow(self, _tipo):  # noqa: N802 - override de Qt (target="_blank")
        return None


class _PuenteMapa(QObject):
    """Objeto `puente` de la página: JS -> Python."""

    clic_en_mapa = Signal(float, float)
    doble_clic_en_mapa = Signal()
    tecla_pulsada = Signal(str)
    vertice_soltado = Signal(int, float, float)
    pagina_lista = Signal()
    captura_preparada = Signal()

    @Slot(float, float)
    def clic(self, lat: float, lng: float) -> None:
        self.clic_en_mapa.emit(lat, lng)

    @Slot()
    def doble_clic(self) -> None:
        self.doble_clic_en_mapa.emit()

    @Slot(str)
    def tecla(self, nombre: str) -> None:
        self.tecla_pulsada.emit(nombre)

    @Slot(int, float, float)
    def vertice_movido(self, indice: int, lat: float, lng: float) -> None:
        self.vertice_soltado.emit(indice, lat, lng)

    @Slot()
    def listo(self) -> None:
        self.pagina_lista.emit()

    @Slot()
    def captura_lista(self) -> None:
        self.captura_preparada.emit()


def _esperar(milisegundos: int, senal=None) -> None:
    """Bucle de eventos local: vuelve al llegar `senal` o al vencer el tiempo."""
    bucle = QEventLoop()
    if senal is not None:
        senal.connect(bucle.quit)
    QTimer.singleShot(milisegundos, bucle.quit)
    try:
        bucle.exec()
    finally:
        if senal is not None:
            senal.disconnect(bucle.quit)


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

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

        self._latitud: Optional[float] = None
        self._longitud: Optional[float] = None
        self._anillo: List[Tuple[float, float]] = []  # (lon, lat), sin repetir el primero
        self._superficie_ha: float = 0.0
        self._perimetro_m: float = 0.0
        self._numero_parte_actual = ""
        self._modo = MODO_NORMAL
        self._solo_lectura: List[Tuple[float, float, str, Optional[str]]] = []
        self._solo_lectura_programado = False

        self._hilo_geocode: Optional[Tuple[QThread, GeocodeWorker]] = None
        self.calibracion = cartografia.cargar_calibracion()

        # Página Leaflet + puente JS <-> Python.
        self._pagina_cargada = False
        self._pendientes: List[str] = []
        self.vista = QWebEngineView(self)
        self.vista.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._pagina = _PaginaMapa(perfil_mapa(), self.vista)
        ajustes = self._pagina.settings()
        ajustes.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.vista.setPage(self._pagina)
        self._puente = _PuenteMapa(self)
        self._canal = QWebChannel(self._pagina)
        self._canal.registerObject("puente", self._puente)
        self._pagina.setWebChannel(self._canal)
        self._puente.pagina_lista.connect(self._on_pagina_lista)
        self._puente.clic_en_mapa.connect(self._on_clic_mapa)
        self._puente.doble_clic_en_mapa.connect(self._on_doble_clic_mapa)
        self._puente.tecla_pulsada.connect(self._on_tecla)
        self._puente.vertice_soltado.connect(self._on_vertice_movido)

        self._construir_ui()
        self._js("configurar", not self.modo_operativo)
        self._js("cuartel", self.calibracion.cuartel_lat, self.calibracion.cuartel_lon)
        self._js("encuadrar", self.calibracion.cuartel_lat, self.calibracion.cuartel_lon, ANCHO_VISTA_INICIAL_KM)
        self.vista.setUrl(QUrl.fromLocalFile(str(get_resource_path(RUTA_PAGINA_MAPA))))

    # -- Puente con la página ----------------------------------------------------

    def _js(self, funcion: str, *args: Any) -> None:
        """Llama `fs.<funcion>(args)` en la página (o lo encola si todavía no cargó)."""
        script = f"fs.{funcion}({', '.join(json.dumps(a) for a in args)});"
        if self._pagina_cargada:
            self._pagina.runJavaScript(script)
        else:
            self._pendientes.append(script)

    def _on_pagina_lista(self) -> None:
        if self._pagina_cargada:
            # Recarga de la página (no debería pasar): se redibuja todo el estado.
            self._sincronizar_todo()
            return
        self._pagina_cargada = True
        for script in self._pendientes:
            self._pagina.runJavaScript(script)
        self._pendientes = []

    def _sincronizar_todo(self) -> None:
        self._js("configurar", not self.modo_operativo)
        self._js("cuartel", self.calibracion.cuartel_lat, self.calibracion.cuartel_lon)
        self._redibujar_siniestro()
        self._redibujar_poligono()
        self._enviar_solo_lectura()

    @property
    def pagina_cargada(self) -> bool:
        return self._pagina_cargada

    # -- Construcción de la UI ------------------------------------------------

    def _construir_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if not self.modo_operativo:
            layout.addLayout(self._crear_barra_busqueda())

        self.vista.setMinimumHeight(320)
        layout.addWidget(self.vista, 1)

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

        boton_kmz = QPushButton("📦 Exportar KMZ", self)
        boton_kmz.setToolTip("Exporta el polígono dibujado como zona operativa (perímetro, despliegue, "
                             "búsqueda o exclusión) en un .kmz con estilo semitransparente.")
        boton_kmz.clicked.connect(self._exportar_kmz)
        fila.addWidget(boton_kmz)

        for boton in (boton_buscar, boton_importar, boton_exportar, boton_kmz):
            boton.setObjectName("botonHerramienta")
        return fila

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
        boton("＋", lambda: self._js("zoom", 1), "Acercar (rueda del mouse)")
        boton("－", lambda: self._js("zoom", -1), "Alejar (rueda del mouse)")
        boton("⤢ Ver todo", self.ver_todo, "Encuadrar el cuartel, el siniestro y las zonas dibujadas")
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

    # -- Vista -------------------------------------------------------------------

    def recargar_mapa(self) -> None:
        """Vuelve a leer la ubicación del cuartel (tras cambiarla en
        Configuración) y redibuja marcador, línea y distancia."""
        self.calibracion = cartografia.cargar_calibracion()
        self._js("cuartel", self.calibracion.cuartel_lat, self.calibracion.cuartel_lon)
        self._redibujar_siniestro()

    def _centrar_en_cuartel(self) -> None:
        self.centrar_en(self.calibracion.cuartel_lat, self.calibracion.cuartel_lon)

    def ver_todo(self) -> None:
        """Encuadra el cuartel, el siniestro, el polígono y la capa de solo lectura."""
        puntos = [(self.calibracion.cuartel_lat, self.calibracion.cuartel_lon)]
        if self._latitud is not None and self._longitud is not None:
            puntos.append((self._latitud, self._longitud))
        puntos += [(lat, lon) for lon, lat in self._anillo]
        for lat, lon, _etiqueta, geojson in self._solo_lectura:
            puntos.append((lat, lon))
            puntos += [(la, lo) for lo, la in self._anillo_seguro(geojson)]
        if len(puntos) == 1:
            self.encuadrar(*puntos[0], ancho_km=ANCHO_VISTA_INICIAL_KM)
            return
        self._ajustar_a(puntos)

    def _ajustar_a(self, puntos: List[Tuple[float, float]]) -> None:
        lats = [p[0] for p in puntos]
        lons = [p[1] for p in puntos]
        self._js("ajustar", [[min(lats), min(lons)], [max(lats), max(lons)]])

    # -- Clics y teclas sobre el mapa ------------------------------------------------

    def _on_clic_mapa(self, lat: float, lon: float) -> None:
        if self.modo_operativo:
            return
        if self._modo == MODO_DIBUJO:
            self._anillo.append((lon, lat))
            self._redibujar_poligono()
            return
        if self._modo == MODO_EDICION:
            self._salir_de_modos()  # clic fuera de los vértices = terminar edición
            return
        self._on_marcador_cambiado(lat, lon)

    def _on_doble_clic_mapa(self) -> None:
        if self._modo == MODO_DIBUJO:
            self._terminar_dibujo()

    def _on_tecla(self, nombre: str) -> None:
        if nombre == "enter":
            self._terminar_dibujo()
        elif nombre == "escape":
            self._on_escape()

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
        if self._latitud is None or self._longitud is None:
            self._js("siniestro", None, None, None)
            self._set_texto_panel("label_coordenadas", "(sin marcar)")
            self._set_texto_panel("label_distancia", "(marcá el lugar del siniestro)")
            return

        km = cartografia.distancia_geodesica_km(
            self.calibracion.cuartel_lat, self.calibracion.cuartel_lon, self._latitud, self._longitud
        )
        distancia = cartografia.formatear_distancia(km)
        rotulo = f"🚒 → 📍 <b>Distancia: {distancia}</b> <span style='color:#667'>(línea recta)</span>"
        self._js("siniestro", self._latitud, self._longitud, rotulo)
        self._set_texto_panel("label_coordenadas", f"{self._latitud:.6f}, {self._longitud:.6f}")
        self._set_texto_panel("label_distancia", f"{distancia} en línea recta")

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
        self.vista.setFocus()  # Enter / Esc llegan a la página
        self.boton_dibujar.setText("✔ Terminar Polígono")
        self._on_poligono_cambiado()

    def _terminar_dibujo(self) -> None:
        if self._modo != MODO_DIBUJO:
            return
        # El doble clic puede dejar dos vértices en el mismo lugar.
        while len(self._anillo) >= 2 and self._vertices_iguales(self._anillo[-1], self._anillo[-2]):
            self._anillo.pop()
        if len(self._anillo) < 3:
            self._anillo = []
        self._salir_de_modos()
        self._on_poligono_cambiado()

    @staticmethod
    def _vertices_iguales(a: Tuple[float, float], b: Tuple[float, float]) -> bool:
        return cartografia.distancia_geodesica_km(a[1], a[0], b[1], b[0]) * 1000 <= TOLERANCIA_VERTICE_M

    def _habilitar_edicion_poligono(self) -> None:
        if len(self._anillo) < 3:
            return
        self._salir_de_modos()
        self._modo = MODO_EDICION
        self._redibujar_poligono()
        self._js("vertices", [[lat, lon] for lon, lat in self._anillo])

    def _on_vertice_movido(self, indice: int, lat: float, lon: float) -> None:
        if self._modo != MODO_EDICION or not 0 <= indice < len(self._anillo):
            return
        self._anillo[indice] = (lon, lat)
        self._on_poligono_cambiado()

    def _salir_de_modos(self) -> None:
        if self._modo == MODO_EDICION:
            self._js("vertices", None)
        self._modo = MODO_NORMAL
        if hasattr(self, "boton_dibujar"):
            self.boton_dibujar.setText("▱ Dibujar Polígono")
        self._redibujar_poligono()

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
        self._js("poligono", [[lat, lon] for lon, lat in self._anillo], self._modo)

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
        self.encuadrar(lat, lng)

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
        if self._anillo:
            self._ajustar_a([(lat, lon) for lon, lat in self._anillo])

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

    def _exportar_kmz(self) -> None:
        if len(self._anillo) < 3:
            QMessageBox.information(self, "Nada para exportar",
                                    "Dibujá (o importá) un polígono primero.")
            return
        from app.ui.kmz_export_dialog import ExportarZonasKmzDialog, ZonaExportable

        parte = self._numero_parte_actual
        nombre = f"Parte N° {parte}" if parte else "Zona operativa"
        detalle = f"Superficie {self._superficie_ha:.2f} ha · perímetro {self._perimetro_m:.0f} m"
        punto = self.punto()
        zona = ZonaExportable(nombre, [(lat, lon) for lon, lat in self._anillo], detalle, punto=punto)
        archivo = f"zona_parte_{parte.replace('/', '-')}.kmz" if parte else None
        ExportarZonasKmzDialog([zona], self, nombre_documento=f"Fire Station - {nombre}",
                               nombre_archivo=archivo).exec()

    # -- Capa de solo lectura (vista de operaciones) ------------------------------

    def limpiar_solo_lectura(self) -> None:
        self._solo_lectura = []
        self._programar_solo_lectura()

    def agregar_incidente_solo_lectura(self, lat: float, lon: float, etiqueta: str,
                                       geojson: Optional[str] = None) -> None:
        self._solo_lectura.append((lat, lon, etiqueta, geojson))
        self._programar_solo_lectura()

    def _programar_solo_lectura(self) -> None:
        """Agregar N incidentes seguidos = UN solo redibujo en la página."""
        if not self._solo_lectura_programado:
            self._solo_lectura_programado = True
            QTimer.singleShot(0, self._enviar_solo_lectura)

    def _enviar_solo_lectura(self) -> None:
        self._solo_lectura_programado = False
        items = [
            {"lat": lat, "lng": lon, "etiqueta": etiqueta,
             "anillo": [[la, lo] for lo, la in self._anillo_seguro(geojson)] or None}
            for lat, lon, etiqueta, geojson in self._solo_lectura
        ]
        self._js("soloLectura", items)

    @staticmethod
    def _anillo_seguro(geojson: Optional[str]) -> List[Tuple[float, float]]:
        if not geojson:
            return []
        try:
            return _anillo_de_geojson(geojson)
        except (ValueError, KeyError, TypeError, IndexError):
            return []

    # -- API pública para main_window.py ---------------------------------------

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
        if lat is not None and lng is not None:
            self.centrar_en(lat, lng)

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
        """Centra la vista en el punto (mismo zoom). Llamable desde Python
        en cualquier momento: si la página no cargó, queda encolado."""
        self._js("centrar", lat, lon)

    def encuadrar(self, lat: float, lon: float, ancho_km: float = 3.0) -> None:
        """Zoom de barrio (~`ancho_km` de ancho en pantalla) centrado en el punto."""
        self._js("encuadrar", lat, lon, ancho_km)

    def dentro_del_mapa(self, lat: float, lon: float) -> bool:
        """El mapa cubre todo el mundo: solo se validan los rangos WGS84."""
        return -90 <= lat <= 90 and -180 <= lon <= 180

    def exportar_imagen(self, ruta: Path, ancho: int = 1600, alto: int = 1000, leyenda: str = "") -> Path:
        """PNG/JPG del mapa CENTRADO en la chincheta, con el zoom que tiene la
        vista en pantalla y una franja inferior con la leyenda y las
        coordenadas. Se centra, se espera a que carguen las teselas (máx.
        `TIMEOUT_CAPTURA_MS`) y se captura la vista sin los controles."""
        if self._latitud is None or self._longitud is None:
            raise ValueError("No hay un punto marcado en el mapa.")
        lat, lon = self._latitud, self._longitud

        self._js("prepararCaptura", lat, lon)
        _esperar(TIMEOUT_CAPTURA_MS, self._puente.captura_preparada)
        _esperar(200)  # que el compositor pinte el último cuadro
        captura = self.vista.grab().toImage()
        self._js("terminarCaptura")
        if captura.isNull() or captura.width() < 2 or captura.height() < 2:
            raise OSError("No se pudo capturar el mapa (¿la ventana está minimizada?).")
        captura.setDevicePixelRatio(1.0)

        # Recorte centrado con la proporción de salida (el punto queda en el centro).
        proporcion = ancho / alto
        rw, rh = captura.width(), captura.height()
        if rw / rh > proporcion:
            rw = round(rh * proporcion)
        else:
            rh = round(rw / proporcion)
        fuente = QRectF((captura.width() - rw) / 2, (captura.height() - rh) / 2, rw, rh)

        banda = 56
        imagen = QImage(ancho, alto + banda, QImage.Format.Format_ARGB32)
        imagen.fill(QColor("#e5e9f0"))
        pintor = QPainter(imagen)
        try:
            pintor.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                                  | QPainter.RenderHint.TextAntialiasing)
            pintor.drawImage(QRectF(0, 0, ancho, alto), captura, fuente)
            # Retícula fina sobre el punto: se ubica aunque la chincheta tape el lugar.
            for lapiz in (QPen(QColor(255, 255, 255, 200), 3), QPen(COLOR_SINIESTRO, 1.5)):
                pintor.setPen(lapiz)
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    pintor.drawLine(QPointF(ancho / 2 + dx * 14, alto / 2 + dy * 14),
                                    QPointF(ancho / 2 + dx * 40, alto / 2 + dy * 40))
            pintor.fillRect(QRectF(0, alto, ancho, banda), QColor("#1f2937"))
            pintor.setPen(QColor("#ffffff"))
            fuente_texto = QFont("Segoe UI")
            fuente_texto.setPixelSize(22)
            pintor.setFont(fuente_texto)
            texto = f"📍 {lat:.6f}, {lon:.6f}   ({formatear_dms(lat, lon)})"
            if leyenda:
                texto = f"{leyenda}   ·   {texto}"
            pintor.drawText(QRectF(18, alto, ancho - 36, banda),
                            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, texto)
            pintor.setPen(QColor("#9ca3af"))
            fuente_texto.setPixelSize(13)
            pintor.setFont(fuente_texto)
            pintor.drawText(QRectF(18, alto, ancho - 36, banda - 6),
                            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
                            "© colaboradores de OpenStreetMap / Esri")
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

        fila = QHBoxLayout()
        boton_actualizar = QPushButton("Actualizar", self)
        boton_actualizar.clicked.connect(self.refrescar)
        boton_kmz = QPushButton("📦 Exportar zonas a KMZ", self)
        boton_kmz.setToolTip("Exporta los polígonos de los partes (perímetros, áreas de despliegue, "
                             "zonas de búsqueda) a un .kmz para Google Earth / Avenza.")
        boton_kmz.clicked.connect(self.exportar_zonas_kmz)
        fila.addWidget(boton_actualizar)
        fila.addStretch(1)
        fila.addWidget(boton_kmz)
        layout.addLayout(fila, 0)

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

    def zonas_exportables(self) -> list:
        """Partes con polígono guardado -> ZonaExportable (más recientes primero)."""
        from app.db import get_session
        from app.models import Incidente
        from app.ui.kmz_export_dialog import ZonaExportable

        with get_session() as session:
            filas = [
                (inc.numero_parte, inc.fecha, inc.latitud, inc.longitud, inc.superficie_ha,
                 inc.geometria_geojson, inc.calle_altura, inc.localidad)
                for inc in session.query(Incidente)
                .filter(Incidente.geometria_geojson.isnot(None))
                .order_by(Incidente.fecha.desc(), Incidente.id.desc())
                .all()
            ]
        zonas = []
        for parte, fecha, lat, lng, sup, geojson, calle, localidad in filas:
            try:
                anillo = _anillo_de_geojson(geojson)
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            if len(anillo) < 3:
                continue
            detalle = " · ".join(x for x in (
                fecha.strftime("%d/%m/%Y") if fecha else "",
                f"{sup:.2f} ha" if sup else "",
                ", ".join(y for y in (calle, localidad) if y),
            ) if x)
            punto = (lat, lng) if lat is not None and lng is not None else None
            zonas.append(ZonaExportable(f"Parte N° {parte}", [(la, lo) for lo, la in anillo], detalle,
                                        punto=punto))
        return zonas

    def exportar_zonas_kmz(self) -> None:
        from app.ui.kmz_export_dialog import ExportarZonasKmzDialog

        zonas = self.zonas_exportables()
        if not zonas:
            QMessageBox.information(self, "Sin zonas", "Ningún parte guardado tiene un polígono dibujado.")
            return
        ExportarZonasKmzDialog(zonas, self).exec()
