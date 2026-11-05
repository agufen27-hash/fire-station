"""
Descarga de una imagen satelital de la zona desde Google Earth Engine para
usarla como imagen base del Mapa Operativo (app/services/cartografia.py).

Qué se baja: un compuesto "sin nubes" de Sentinel-2 nivel 2A armonizado
(COPERNICUS/S2_SR_HARMONIZED, 10 m por píxel): la MEDIANA de todas las
pasadas del período elegido con menos de N % de nubosidad, en color real
(bandas B4 / B3 / B2). Es la imagen de satélite pública más nítida que
ofrece Earth Engine para Argentina; NO es la de Google Earth / Google Maps
(esa no se puede usar en aplicaciones propias).

Cómo: `projects.image.computePixels` con una grilla en EPSG:4326 cuyo
origen es la esquina noroeste del recuadro: el PNG que vuelve queda
alineado EXACTAMENTE con los límites norte / sur / oeste / este, que es la
calibración lineal que usa el mapa (Georreferencia). Se descarga una vez y
después el mapa funciona sin internet.

Atribución obligatoria (Copernicus Sentinel Data Terms):
"Contiene datos modificados de Copernicus Sentinel <año>". Queda guardada en
resources/mapa_base_origen.json y en el mensaje al instalar la imagen.

Nombres de funciones y argumentos de la expresión: los que usa el cliente
oficial de Python (earthengine-api) al serializar
  ee.ImageCollection(ID).filterBounds(geom).filterDate(a, b)
    .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', n)).median()
Las claves de `Collection.filter`, `DateRange`, `reduce.median` y
`Filter.lessThan` salen de llamadas posicionales del cliente (no quedaron
confirmadas en su código): si Google las rechazara, el 400 explica cuál.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from app.services import cartografia
from app.services.cartografia import CalibracionMapa
from app.services.gee_service import (
    MAX_BYTES_PIXELES,
    MAX_LADO_PIXELES,
    GeeError,
    validar_pedido_pixeles,
)

COLECCION_SENTINEL2 = "COPERNICUS/S2_SR_HARMONIZED"
BANDAS_COLOR_REAL = ["B4", "B3", "B2"]
# Reflectancia escalada x 10000: 0-3000 = 0.0-0.3, la visualización del catálogo.
RANGO_VISUALIZACION = (0, 3000)
GAMMA = 1.2
RESOLUCIONES_M = (10, 20, 30)
LADO_MAXIMO = 12000            # px: tope práctico (QPixmap y memoria de la PC de guardia)
MARGEN_LIMITE = 0.95           # no rozar los 48 MB de computePixels
METROS_POR_GRADO_LAT = 110_574.0
METROS_POR_GRADO_LON_ECUADOR = 111_320.0
ARCHIVO_ORIGEN = "mapa_base_origen.json"
ARCHIVO_DESCARGA = "mapa_base_satelital_descarga.png"
FIRMA_PNG = b"\x89PNG\r\n\x1a\n"


class ImagenSatelitalError(GeeError):
    """El pedido es inválido o la imagen recibida no sirve."""


@dataclass(frozen=True)
class PedidoImagen:
    lat_norte: float
    lat_sur: float
    lon_oeste: float
    lon_este: float
    fecha_desde: date
    fecha_hasta: date
    nubosidad_max: float = 20.0      # % (CLOUDY_PIXEL_PERCENTAGE)
    resolucion_m: float = 10.0       # deseada; se agranda si no entra en los límites

    def validar(self) -> None:
        if not (-90 <= self.lat_sur < self.lat_norte <= 90 and -180 <= self.lon_oeste < self.lon_este <= 180):
            raise ImagenSatelitalError("Límites inválidos: el norte debe ser mayor que el sur "
                                       "y el este mayor que el oeste.")
        if self.fecha_desde >= self.fecha_hasta:
            raise ImagenSatelitalError("La fecha 'desde' tiene que ser anterior a 'hasta'.")
        if self.fecha_desde < date(2017, 3, 28):
            raise ImagenSatelitalError("Sentinel-2 nivel 2A está disponible desde el 28/03/2017.")
        if not 0 < self.nubosidad_max <= 100:
            raise ImagenSatelitalError("La nubosidad máxima va de 1 a 100 %.")
        if self.resolucion_m < 10:
            raise ImagenSatelitalError("Sentinel-2 no tiene más detalle que 10 m por píxel.")

    def calibracion(self, cuartel_lat: float, cuartel_lon: float) -> CalibracionMapa:
        return CalibracionMapa(self.lat_norte, self.lat_sur, self.lon_oeste, self.lon_este,
                               cuartel_lat, cuartel_lon)


@dataclass(frozen=True)
class Grilla:
    ancho: int
    alto: int
    resolucion_m: float          # la efectiva (puede ser mayor que la pedida)
    escala_x: float              # grados por píxel
    escala_y: float              # grados por píxel (negativa: el norte arriba)

    @property
    def megabytes(self) -> float:
        return self.ancho * self.alto * len(BANDAS_COLOR_REAL) / 1024 / 1024


@dataclass
class ImagenDescargada:
    ruta: Path
    pedido: PedidoImagen
    grilla: Grilla
    tamanio_bytes: int
    descargada: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def atribucion(self) -> str:
        anios = sorted({self.pedido.fecha_desde.year, self.pedido.fecha_hasta.year})
        return (f"Contiene datos modificados de Copernicus Sentinel {'-'.join(map(str, anios))} "
                "(procesados con Google Earth Engine)")


# -- Grilla ------------------------------------------------------------------------------

def area_km(p: PedidoImagen) -> Tuple[float, float]:
    """(ancho, alto) aproximados del recuadro en km."""
    lat_media = math.radians((p.lat_norte + p.lat_sur) / 2)
    ancho = (p.lon_este - p.lon_oeste) * METROS_POR_GRADO_LON_ECUADOR * math.cos(lat_media) / 1000
    alto = (p.lat_norte - p.lat_sur) * METROS_POR_GRADO_LAT / 1000
    return ancho, alto


def calcular_grilla(p: PedidoImagen) -> Grilla:
    """Píxeles para cubrir el recuadro a `resolucion_m`; si no entra en los
    límites de computePixels (48 MB, 32K por lado) o en LADO_MAXIMO, se
    agranda el píxel hasta que entre. Píxel "cuadrado" en metros."""
    ancho_km, alto_km = area_km(p)
    tope_bytes = MAX_BYTES_PIXELES * MARGEN_LIMITE
    tope_lado = min(LADO_MAXIMO, MAX_LADO_PIXELES)
    resolucion = max(10.0, float(p.resolucion_m))
    while True:
        ancho = max(1, math.ceil(ancho_km * 1000 / resolucion))
        alto = max(1, math.ceil(alto_km * 1000 / resolucion))
        if ancho <= tope_lado and alto <= tope_lado and ancho * alto * len(BANDAS_COLOR_REAL) <= tope_bytes:
            break
        resolucion *= 1.1
    return Grilla(ancho, alto, round(resolucion, 1),
                  (p.lon_este - p.lon_oeste) / ancho,
                  -(p.lat_norte - p.lat_sur) / alto)


# -- Expresión de Earth Engine -------------------------------------------------------------

def _const(v: Any) -> dict:
    return {"constantValue": v}


def _llamar(funcion: str, **argumentos: dict) -> dict:
    return {"functionInvocationValue": {"functionName": funcion, "arguments": argumentos}}


def expresion_compuesto_sentinel2(p: PedidoImagen) -> dict:
    """Mediana de las pasadas Sentinel-2 del período sobre el recuadro, con
    nubosidad < nubosidad_max. Las fechas van como 'AAAA-MM-DD'; `hasta`
    es exclusivo (como filterDate)."""
    rectangulo = _llamar(
        "GeometryConstructors.Polygon",
        coordinates=_const([[[p.lon_oeste, p.lat_norte], [p.lon_oeste, p.lat_sur],
                             [p.lon_este, p.lat_sur], [p.lon_este, p.lat_norte]]]),
        geodesic=_const(False),
        evenOdd=_const(True),
    )
    coleccion = _llamar("ImageCollection.load", id=_const(COLECCION_SENTINEL2))
    en_zona = _llamar("Collection.filter", collection=coleccion, filter=_llamar(
        "Filter.intersects", leftField=_const(".all"),
        rightValue=_llamar("Feature", geometry=rectangulo)))
    en_fecha = _llamar("Collection.filter", collection=en_zona, filter=_llamar(
        "Filter.dateRangeContains",
        leftValue=_llamar("DateRange",
                          start=_llamar("Date", value=_const(p.fecha_desde.isoformat())),
                          end=_llamar("Date", value=_const(p.fecha_hasta.isoformat()))),
        rightField=_const("system:time_start")))
    sin_nubes = _llamar("Collection.filter", collection=en_fecha, filter=_llamar(
        "Filter.lessThan", leftField=_const("CLOUDY_PIXEL_PERCENTAGE"),
        rightValue=_const(float(p.nubosidad_max))))
    return {"values": {"0": _llamar("reduce.median", collection=sin_nubes)}, "result": "0"}


def pedido_compute_pixels(p: PedidoImagen, grilla: Optional[Grilla] = None) -> dict:
    """Cuerpo de projects.image.computePixels (ya validado contra el esquema v1)."""
    p.validar()
    grilla = grilla or calcular_grilla(p)
    minimo, maximo = RANGO_VISUALIZACION
    return validar_pedido_pixeles({
        "expression": expresion_compuesto_sentinel2(p),
        "fileFormat": "PNG",
        "bandIds": list(BANDAS_COLOR_REAL),
        "visualizationOptions": {"ranges": [{"min": minimo, "max": maximo}], "gamma": GAMMA},
        "grid": {
            "dimensions": {"width": grilla.ancho, "height": grilla.alto},
            "affineTransform": {
                "scaleX": grilla.escala_x, "shearX": 0, "translateX": p.lon_oeste,
                "shearY": 0, "scaleY": grilla.escala_y, "translateY": p.lat_norte,
            },
            "crsCode": "EPSG:4326",
        },
    })


# -- Descarga e instalación --------------------------------------------------------------

def dimensiones_png(datos: bytes) -> Tuple[int, int]:
    """(ancho, alto) del encabezado IHDR de un PNG; ImagenSatelitalError si no es PNG."""
    if len(datos) < 24 or not datos.startswith(FIRMA_PNG) or datos[12:16] != b"IHDR":
        raise ImagenSatelitalError("Earth Engine no devolvió una imagen PNG válida.")
    return struct.unpack(">II", datos[16:24])


def descargar(p: PedidoImagen, servicio=None, carpeta: Optional[Path] = None,
              progreso: Optional[Callable[[str], None]] = None) -> ImagenDescargada:
    """Pide el compuesto a Earth Engine y lo deja en un PNG temporal (todavía
    NO es la imagen base: eso lo hace `instalar`). Bloqueante: correr en un QThread."""
    from app.services.gee_service import GeeService

    avisar = progreso or (lambda _t: None)
    grilla = calcular_grilla(p)
    cuerpo = pedido_compute_pixels(p, grilla)
    avisar(f"Pidiendo a Earth Engine {grilla.ancho} x {grilla.alto} px ({grilla.resolucion_m:g} m/px)…")
    servicio = servicio or GeeService()
    datos = servicio.calcular_pixeles(cuerpo)
    ancho, alto = dimensiones_png(datos)
    if (ancho, alto) != (grilla.ancho, grilla.alto):
        raise ImagenSatelitalError(f"La imagen recibida mide {ancho}x{alto} px y se pidió "
                                   f"{grilla.ancho}x{grilla.alto}: no coincidiría con los límites.")
    carpeta = carpeta or cartografia.carpeta_imagen()
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / ARCHIVO_DESCARGA
    temporal = ruta.with_suffix(".tmp")
    temporal.write_bytes(datos)
    temporal.replace(ruta)
    avisar(f"Imagen recibida ({len(datos) / 1024 / 1024:.1f} MB).")
    return ImagenDescargada(ruta, p, grilla, len(datos))


def instalar(imagen: ImagenDescargada, cuartel_lat: float, cuartel_lon: float) -> Tuple[Path, CalibracionMapa]:
    """La imagen descargada pasa a ser la imagen base y la calibración del
    mapa toma sus límites exactos (el cuartel se conserva). Deja el origen y
    la atribución en resources/mapa_base_origen.json."""
    calibracion = imagen.pedido.calibracion(cuartel_lat, cuartel_lon)
    destino = cartografia.instalar_imagen_base(imagen.ruta)
    cartografia.guardar_calibracion(calibracion)
    origen = {
        "fuente": f"Google Earth Engine · {COLECCION_SENTINEL2} (mediana, nubosidad < {imagen.pedido.nubosidad_max:g} %)",
        "periodo": [imagen.pedido.fecha_desde.isoformat(), imagen.pedido.fecha_hasta.isoformat()],
        "resolucion_m": imagen.grilla.resolucion_m,
        "pixeles": [imagen.grilla.ancho, imagen.grilla.alto],
        "limites": {k: v for k, v in asdict(calibracion).items() if not k.startswith("cuartel")},
        "descargada": imagen.descargada,
        "atribucion": imagen.atribucion,
    }
    (cartografia.carpeta_imagen() / ARCHIVO_ORIGEN).write_text(
        json.dumps(origen, indent=2, ensure_ascii=False), encoding="utf-8")
    if imagen.ruta.resolve() != destino.resolve():
        imagen.ruta.unlink(missing_ok=True)
    return destino, calibracion


def limites_alrededor(lat: float, lon: float, radio_km: float) -> Tuple[float, float, float, float]:
    """(norte, sur, oeste, este) de un cuadrado de lado 2 x radio centrado en el punto."""
    dlat = radio_km * 1000 / METROS_POR_GRADO_LAT
    dlon = radio_km * 1000 / (METROS_POR_GRADO_LON_ECUADOR * max(math.cos(math.radians(lat)), 0.01))
    return round(lat + dlat, 6), round(lat - dlat, 6), round(lon - dlon, 6), round(lon + dlon, 6)


def origen_actual() -> Optional[dict]:
    """Metadatos de la imagen satelital instalada (None si la imagen base es otra)."""
    ruta = cartografia.carpeta_imagen() / ARCHIVO_ORIGEN
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def nombres_para_ui() -> List[str]:
    return [f"{r} m" + (" (máximo detalle)" if r == 10 else "") for r in RESOLUCIONES_M]
