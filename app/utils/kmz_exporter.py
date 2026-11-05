"""
Exportador de zonas operativas a KMZ (Google Earth, Avenza Maps, Gaia GPS,
ArcGIS Field Maps...) con `simplekml`.

Recibe coordenadas en el orden "humano" (lat, lon) -- el mismo de la app y
de un GPS -- y las da vuelta al (lon, lat) que exige KML. Cada tipo de zona
tiene un estilo estándar de emergencias: borde sólido y relleno
semitransparente, para no tapar la imagen satelital de abajo.

    exp = ExportadorKMZ("Incendio forestal 59-027")
    exp.agregar_zona("Perímetro activo", [(-33.63, -64.01), (-33.64, -64.02), (-33.62, -64.03)],
                     tipo=TipoZona.PERIMETRO)
    exp.agregar_punto("Puesto de comando", -33.6315, -64.0152)
    exp.guardar(Path("zonas.kmz"))

Módulo sin Qt: se puede usar desde un hilo de trabajo o desde scripts.
"""

from __future__ import annotations

import math
import tempfile
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import simplekml

Coordenada = Tuple[float, float]  # (lat, lon)


class TipoZona(str, Enum):
    PERIMETRO = "PERIMETRO"      # perímetro de interés / área afectada
    DESPLIEGUE = "DESPLIEGUE"    # área de despliegue / sectorización
    BUSQUEDA = "BUSQUEDA"        # zona de búsqueda
    EXCLUSION = "EXCLUSION"      # zona caliente / exclusión


@dataclass(frozen=True)
class EstiloZona:
    etiqueta: str
    color_hex: str     # "#RRGGBB"
    opacidad: int      # 0-255 del relleno
    ancho_linea: float


ESTILOS: dict = {
    TipoZona.PERIMETRO: EstiloZona("Perímetro de interés", "#FF3B30", 90, 3.0),
    TipoZona.DESPLIEGUE: EstiloZona("Área de despliegue", "#007AFF", 80, 2.5),
    TipoZona.BUSQUEDA: EstiloZona("Zona de búsqueda", "#FFCC00", 80, 2.5),
    TipoZona.EXCLUSION: EstiloZona("Zona de exclusión", "#AF52DE", 100, 3.0),
}


def color_kml(color_hex: str, alfa: int = 255) -> str:
    """"#RRGGBB" + alfa -> "AABBGGRR" (el orden de color de KML)."""
    h = color_hex.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Color inválido: {color_hex!r}")
    rr, gg, bb = h[0:2], h[2:4], h[4:6]
    return f"{max(0, min(255, alfa)):02x}{bb}{gg}{rr}".lower()


def validar_coordenada(par: Sequence[float], indice: int = 0) -> Coordenada:
    """(lat, lon) numérico, finito y dentro de rango; si no, ValueError."""
    try:
        lat, lon = float(par[0]), float(par[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"Vértice {indice + 1} inválido: {par!r}") from exc
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(f"Vértice {indice + 1} no es un número finito: {par!r}")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError(f"Vértice {indice + 1} fuera de rango (lat {lat}, lon {lon}). ¿Están invertidas lat/lon?")
    return lat, lon


def validar_anillo(coordenadas: Iterable[Sequence[float]]) -> List[Coordenada]:
    """Normaliza a [(lat, lon), ...] sin repetir el primer vértice al final.
    Exige al menos 3 vértices distintos y coordenadas dentro de rango."""
    anillo: List[Coordenada] = []
    for i, par in enumerate(coordenadas):
        lat, lon = validar_coordenada(par, i)
        if not anillo or anillo[-1] != (lat, lon):  # descarta vértices repetidos seguidos
            anillo.append((lat, lon))
    if len(anillo) > 1 and anillo[0] == anillo[-1]:
        anillo.pop()
    if len(set(anillo)) < 3:
        raise ValueError("Un polígono necesita al menos 3 vértices distintos.")
    return anillo


class ExportadorKMZ:
    """Arma un documento KML con carpetas por tipo de zona y lo guarda como KMZ."""

    def __init__(self, nombre_documento: str = "Fire Station - Zonas operativas") -> None:
        self._kml = simplekml.Kml(name=nombre_documento)
        self._carpetas: dict = {}
        self._estilos: dict = {}
        self.cantidad_zonas = 0
        self.cantidad_puntos = 0

    # -- Estilos y carpetas -----------------------------------------------------------

    def _estilo(self, tipo: TipoZona) -> simplekml.Style:
        if tipo not in self._estilos:
            e = ESTILOS[tipo]
            estilo = simplekml.Style()
            estilo.linestyle.color = color_kml(e.color_hex)
            estilo.linestyle.width = e.ancho_linea
            estilo.polystyle.color = color_kml(e.color_hex, e.opacidad)
            estilo.polystyle.fill = 1
            estilo.polystyle.outline = 1
            estilo.labelstyle.scale = 0.9
            self._estilos[tipo] = estilo
        return self._estilos[tipo]

    def _carpeta(self, tipo: TipoZona):
        if tipo not in self._carpetas:
            self._carpetas[tipo] = self._kml.newfolder(name=ESTILOS[tipo].etiqueta)
        return self._carpetas[tipo]

    # -- Contenido ------------------------------------------------------------------------

    def agregar_zona(self, nombre: str, coordenadas: Iterable[Sequence[float]],
                     tipo: TipoZona = TipoZona.PERIMETRO, descripcion: str = "") -> None:
        """`coordenadas`: [(lat, lon), ...] del contorno (cerrado o no)."""
        tipo = TipoZona(tipo)
        anillo = validar_anillo(coordenadas)
        contorno = [(lon, lat) for lat, lon in anillo]
        contorno.append(contorno[0])  # KML exige el anillo cerrado
        poligono = self._carpeta(tipo).newpolygon(name=nombre or ESTILOS[tipo].etiqueta,
                                                  outerboundaryis=contorno)
        poligono.description = descripcion or ESTILOS[tipo].etiqueta
        poligono.altitudemode = simplekml.AltitudeMode.clamptoground
        poligono.tessellate = 1
        poligono.style = self._estilo(tipo)
        self.cantidad_zonas += 1

    def agregar_punto(self, nombre: str, lat: float, lon: float, descripcion: str = "",
                      color_hex: str = "#FF3B30") -> None:
        lat, lon = validar_coordenada((lat, lon))
        punto = self._kml.newpoint(name=nombre, coords=[(lon, lat)])
        punto.description = descripcion
        punto.style.iconstyle.color = color_kml(color_hex)
        punto.style.iconstyle.icon.href = "http://maps.google.com/mapfiles/kml/shapes/firedept.png"
        self.cantidad_puntos += 1

    # -- Salida ------------------------------------------------------------------------------

    def kml_texto(self) -> str:
        return self._kml.kml()

    def guardar(self, ruta: Path) -> Path:
        """Escribe el .kmz (zip con doc.kml). Atómico: se arma en un temporal
        y recién al final reemplaza el destino."""
        if not (self.cantidad_zonas or self.cantidad_puntos):
            raise ValueError("No hay zonas ni puntos para exportar.")
        ruta = Path(ruta)
        if ruta.suffix.lower() != ".kmz":
            ruta = ruta.with_suffix(".kmz")
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=ruta.parent, suffix=".kmz.tmp", delete=False) as tmp:
            temporal = Path(tmp.name)
        try:
            with zipfile.ZipFile(temporal, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr("doc.kml", self.kml_texto().encode("utf-8"))
            temporal.replace(ruta)
        except BaseException:
            temporal.unlink(missing_ok=True)
            raise
        return ruta


def exportar_zonas_kmz(ruta: Path, zonas: Iterable[Tuple[str, Iterable[Sequence[float]], TipoZona, str]],
                       nombre_documento: str = "Fire Station - Zonas operativas",
                       puntos: Optional[Iterable[Tuple[str, float, float, str]]] = None) -> Path:
    """Atajo: [(nombre, [(lat, lon), ...], tipo, descripcion)] -> archivo .kmz."""
    exp = ExportadorKMZ(nombre_documento)
    for nombre, coords, tipo, descripcion in zonas:
        exp.agregar_zona(nombre, coords, tipo, descripcion)
    for nombre, lat, lon, descripcion in puntos or ():
        exp.agregar_punto(nombre, lat, lon, descripcion)
    return exp.guardar(ruta)
