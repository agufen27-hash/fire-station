"""
Cartografía base del visor de mapa nativo (estilo Avenza Maps, ver
app/ui/widgets/map_widget.py): una imagen local (carta topográfica,
captura satelital) georreferenciada por sus cuatro bordes.

  - Calibración: sección "mapa" de data/config.json (lat_norte, lat_sur,
    lon_oeste, lon_este + ubicación del cuartel). Si falta, se usan los
    valores por defecto de Adelia María y zona de influencia.
  - Imagen: `resources/mapa_base.png|jpg|jpeg` en la carpeta ESCRIBIBLE
    (al lado del .exe si está empaquetado): el cuartel la reemplaza desde
    Configuración sin tocar código ni reempaquetar. Un .tif/.tiff se
    convierte a PNG al instalarlo. Si no hay ninguna, se usa el mapa que
    viene con la app (`resources/mapa_predeterminado.png`, embebido en el .exe).
  - Conversión píxel <-> GPS: lineal entre los bordes en longitud; en latitud
    lineal (equirectangular) o en la Y de Web Mercator (EPSG:3857, el de los
    GeoTIFF de QGIS/Google). La imagen tiene que estar orientada al norte y
    sin rotar.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Tuple

from app.paths import get_resource_path, get_writable_dir

EXTENSIONES_IMAGEN = (".png", ".jpg", ".jpeg")
EXTENSIONES_TIFF = (".tif", ".tiff")
NOMBRE_IMAGEN = "mapa_base"
# Mapa de la zona que viene con la app (solo lectura, embebido por tools/build_exe.py).
IMAGEN_PREDETERMINADA = "resources/mapa_predeterminado.png"
RADIO_TIERRA_KM = 6371.0088
RADIO_MERCATOR_M = 6378137.0  # esfera de EPSG:3857

PROYECCION_EQUIRECTANGULAR = "equirectangular"
PROYECCION_MERCATOR = "mercator"
PROYECCIONES = (PROYECCION_EQUIRECTANGULAR, PROYECCION_MERCATOR)


def mercator_a_lat(y: float) -> float:
    """Metros Y de Web Mercator (EPSG:3857) -> latitud en grados."""
    return math.degrees(2 * math.atan(math.exp(y / RADIO_MERCATOR_M)) - math.pi / 2)


def mercator_a_lon(x: float) -> float:
    """Metros X de Web Mercator (EPSG:3857) -> longitud en grados."""
    return math.degrees(x / RADIO_MERCATOR_M)


def lat_a_mercator(lat: float) -> float:
    return RADIO_MERCATOR_M * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


# Límites del mapa de la zona que viene con la app (Mapa.tiff, EPSG:3857):
# Norte -3944122.2065 · Sur -4008599.1016 · Este -7045543.8933 · Oeste -7169091.2358
LIMITES_MERCATOR_PREDETERMINADOS = {
    "norte": -3944122.2065, "sur": -4008599.1016, "este": -7045543.8933, "oeste": -7169091.2358,
}
# Calibración de fábrica anterior (cuadrícula de Adelia María): config.json que
# la tenga y no tenga imagen propia se migra al mapa nuevo.
_LIMITES_VIEJOS = {"lat_norte": -33.55, "lat_sur": -33.72, "lon_oeste": -64.12, "lon_este": -63.92}


@dataclass
class CalibracionMapa:
    lat_norte: float = mercator_a_lat(LIMITES_MERCATOR_PREDETERMINADOS["norte"])
    lat_sur: float = mercator_a_lat(LIMITES_MERCATOR_PREDETERMINADOS["sur"])
    lon_oeste: float = mercator_a_lon(LIMITES_MERCATOR_PREDETERMINADOS["oeste"])
    lon_este: float = mercator_a_lon(LIMITES_MERCATOR_PREDETERMINADOS["este"])
    cuartel_lat: float = -33.6324
    cuartel_lon: float = -64.0195
    proyeccion: str = PROYECCION_MERCATOR

    @classmethod
    def desde_mercator(cls, norte: float, sur: float, este: float, oeste: float,
                       cuartel_lat: float = -33.6324, cuartel_lon: float = -64.0195) -> "CalibracionMapa":
        """Límites en metros EPSG:3857 (como los da QGIS) -> calibración en grados."""
        return cls(mercator_a_lat(norte), mercator_a_lat(sur), mercator_a_lon(oeste), mercator_a_lon(este),
                   cuartel_lat, cuartel_lon, PROYECCION_MERCATOR)

    def es_valida(self) -> bool:
        limite_lat = 85 if self.proyeccion == PROYECCION_MERCATOR else 90  # Mercator diverge en los polos
        return (
            self.proyeccion in PROYECCIONES
            and -limite_lat <= self.lat_sur < self.lat_norte <= limite_lat
            and -180 <= self.lon_oeste < self.lon_este <= 180
        )

    def relacion_aspecto(self) -> float:
        """Ancho/alto en km del recuadro (para el fondo generado por código)."""
        if self.proyeccion == PROYECCION_MERCATOR:
            alto_m = lat_a_mercator(self.lat_norte) - lat_a_mercator(self.lat_sur)
            ancho_m = math.radians(self.lon_este - self.lon_oeste) * RADIO_MERCATOR_M
            return ancho_m / alto_m if alto_m else 1.0
        lat_media = math.radians((self.lat_norte + self.lat_sur) / 2)
        ancho = (self.lon_este - self.lon_oeste) * math.cos(lat_media)
        alto = self.lat_norte - self.lat_sur
        return ancho / alto if alto else 1.0


class Georreferencia:
    """Conversión píxel <-> coordenadas para una imagen de `ancho` x `alto`
    píxeles cuyos bordes son los de `calibracion`. El píxel (0, 0) es la
    esquina superior izquierda (noroeste)."""

    def __init__(self, calibracion: CalibracionMapa, ancho: float, alto: float) -> None:
        self.calibracion = calibracion
        self.ancho = float(ancho)
        self.alto = float(alto)
        self._mercator = calibracion.proyeccion == PROYECCION_MERCATOR
        # Eje vertical: latitud (equirectangular) o metros Y de Mercator.
        self._y_norte = self._eje_y(calibracion.lat_norte)
        self._y_sur = self._eje_y(calibracion.lat_sur)

    def _eje_y(self, lat: float) -> float:
        return lat_a_mercator(lat) if self._mercator else lat

    def pixel_to_coords(self, x: float, y: float) -> Tuple[float, float]:
        c = self.calibracion
        lon = c.lon_oeste + (x / self.ancho) * (c.lon_este - c.lon_oeste)
        valor = self._y_norte - (y / self.alto) * (self._y_norte - self._y_sur)
        lat = mercator_a_lat(valor) if self._mercator else valor
        return lat, lon

    def coords_to_pixel(self, lat: float, lon: float) -> Tuple[float, float]:
        c = self.calibracion
        x = (lon - c.lon_oeste) / (c.lon_este - c.lon_oeste) * self.ancho
        y = (self._y_norte - self._eje_y(lat)) / (self._y_norte - self._y_sur) * self.alto
        return x, y

    def contiene(self, lat: float, lon: float) -> bool:
        c = self.calibracion
        return c.lat_sur <= lat <= c.lat_norte and c.lon_oeste <= lon <= c.lon_este


def distancia_geodesica_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia sobre la esfera (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * RADIO_TIERRA_KM * math.asin(min(1.0, math.sqrt(a)))


def formatear_distancia(km: float) -> str:
    return f"{km * 1000:.0f} m" if km < 1 else f"{km:.1f} km"


# -- data/config.json -------------------------------------------------------------

def _ruta_config() -> Path:
    return get_writable_dir("data") / "config.json"


def _leer_config() -> dict:
    try:
        datos = json.loads(_ruta_config().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def cargar_calibracion() -> CalibracionMapa:
    """Calibración de config.json; la de fábrica si falta o es inválida
    (norte <= sur, este <= oeste, valores no numéricos)."""
    seccion = _leer_config().get("mapa")
    por_defecto = CalibracionMapa()
    if not isinstance(seccion, dict):
        return por_defecto
    try:
        valores = {
            clave: float(seccion.get(clave, valor))
            for clave, valor in asdict(por_defecto).items() if clave != "proyeccion"
        }
        # Sin "proyeccion" = config anterior a v1.6: sus límites eran equirectangulares.
        proyeccion = str(seccion.get("proyeccion", PROYECCION_EQUIRECTANGULAR)).strip().lower()
        calibracion = CalibracionMapa(**valores, proyeccion=proyeccion)
    except (TypeError, ValueError):
        print("[Mapa] Calibración inválida en config.json; se usan los valores por defecto.")
        return por_defecto
    if not calibracion.es_valida():
        print("[Mapa] Límites del mapa inconsistentes en config.json (norte/sur u oeste/este invertidos); "
              "se usan los valores por defecto.")
        return por_defecto
    return calibracion


def guardar_calibracion(calibracion: CalibracionMapa) -> None:
    """Escribe SOLO la sección "mapa" (conserva ruba, cuartel, ui, etc.)."""
    if not calibracion.es_valida():
        raise ValueError("Los límites no son consistentes: el norte debe ser mayor que el sur "
                         "y el este mayor que el oeste.")
    datos = _leer_config()
    datos["mapa"] = asdict(calibracion)
    _ruta_config().write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")


def _tiene_limites_viejos(seccion: dict) -> bool:
    try:
        return all(abs(float(seccion.get(k)) - v) < 1e-9 for k, v in _LIMITES_VIEJOS.items())
    except (TypeError, ValueError):
        return False


def asegurar_calibracion_en_config() -> None:
    """Al arrancar la app: si config.json todavía no tiene la sección "mapa",
    se agrega con los valores por defecto para que quede a mano para editar.
    Si tiene los límites de fábrica viejos (cuadrícula de Adelia María) y el
    cuartel no cargó una imagen propia, pasa a los del mapa que viene con la
    app (conserva la ubicación del cuartel)."""
    ruta = _ruta_config()
    if not ruta.exists():
        return
    datos = _leer_config()
    seccion = datos.get("mapa")
    if isinstance(seccion, dict):
        if not ("proyeccion" not in seccion and _tiene_limites_viejos(seccion)
                and ruta_imagen_propia() is None):
            return
        nueva = asdict(CalibracionMapa())
        for clave in ("cuartel_lat", "cuartel_lon"):
            if clave in seccion:
                nueva[clave] = seccion[clave]
        datos["mapa"] = nueva
    else:
        datos["mapa"] = asdict(CalibracionMapa())
    try:
        ruta.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# -- Imagen base ---------------------------------------------------------------------

def carpeta_imagen() -> Path:
    return get_writable_dir("resources")


def ruta_imagen_propia() -> Optional[Path]:
    """La imagen que cargó el cuartel (png > jpg > jpeg), o None."""
    for extension in EXTENSIONES_IMAGEN:
        ruta = carpeta_imagen() / f"{NOMBRE_IMAGEN}{extension}"
        if ruta.is_file():
            return ruta
    return None


def ruta_imagen_predeterminada() -> Optional[Path]:
    """El mapa de la zona que viene con la app, si está en el bundle."""
    ruta = get_resource_path(IMAGEN_PREDETERMINADA)
    return ruta if ruta.is_file() else None


def ruta_imagen_base() -> Optional[Path]:
    """La imagen propia del cuartel; si no hay, el mapa que viene con la app;
    None si tampoco está (se dibuja la cuadrícula)."""
    return ruta_imagen_propia() or ruta_imagen_predeterminada()


def usa_imagen_predeterminada() -> bool:
    return ruta_imagen_propia() is None and ruta_imagen_predeterminada() is not None


def convertir_tiff_a_png(origen: Path, destino: Path) -> Path:
    """Convierte un .tif/.tiff (GeoTIFF incluido: los tags de georreferencia
    se ignoran, los límites salen de la calibración) a PNG."""
    from PIL import Image  # diferido: solo hace falta al cargar un TIFF

    Image.MAX_IMAGE_PIXELS = None  # cartas grandes: el archivo es del propio cuartel
    with Image.open(origen) as imagen:
        imagen.seek(0)  # TIFF multipágina: la primera
        if imagen.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            imagen = imagen.convert("RGBA")
        imagen.save(destino, format="PNG", optimize=True)
    return destino


def es_geotiff_mercator(origen: Path) -> bool:
    """True si el TIFF declara EPSG:3857 (Web Mercator) en sus GeoKeys."""
    try:
        from PIL import Image

        with Image.open(origen) as imagen:
            claves = imagen.tag_v2.get(34735)  # GeoKeyDirectoryTag
            texto = str(imagen.tag_v2.get(34737, ""))  # GeoAsciiParamsTag
    except Exception:  # noqa: BLE001 - archivo ilegible o sin tags: no es GeoTIFF
        return False
    return (claves is not None and 3857 in tuple(claves)) or "Pseudo-Mercator" in texto


def instalar_imagen_base(origen: Path) -> Path:
    """Copia `origen` como la nueva imagen base y borra las de otra
    extensión (si no, un mapa_base.png viejo le ganaría a un .jpg nuevo).
    Un .tif/.tiff se guarda convertido a mapa_base.png."""
    extension = origen.suffix.lower()
    if extension not in EXTENSIONES_IMAGEN + EXTENSIONES_TIFF:
        raise ValueError(f"Formato no soportado: {extension or '(sin extensión)'}. Usá PNG, JPG o TIFF.")
    if extension in EXTENSIONES_TIFF:
        extension = ".png"
        destino = carpeta_imagen() / f"{NOMBRE_IMAGEN}{extension}"
        temporal = destino.with_name(f"{NOMBRE_IMAGEN}_convirtiendo.png")
        try:
            convertir_tiff_a_png(origen, temporal)
        except Exception as e:  # noqa: BLE001 - TIFF dañado/compresión rara: no se toca la imagen actual
            temporal.unlink(missing_ok=True)
            raise ValueError(f"No se pudo leer el TIFF: {e}") from e
        temporal.replace(destino)
    else:
        destino = carpeta_imagen() / f"{NOMBRE_IMAGEN}{extension}"
        if origen.resolve() != destino.resolve():
            shutil.copyfile(origen, destino)
    for otra in EXTENSIONES_IMAGEN:
        if otra != extension:
            (carpeta_imagen() / f"{NOMBRE_IMAGEN}{otra}").unlink(missing_ok=True)
    return destino


def restaurar_mapa_predeterminado(cuartel_lat: float, cuartel_lon: float) -> CalibracionMapa:
    """Borra la imagen propia y vuelve al mapa (y límites) que vienen con la app."""
    for extension in EXTENSIONES_IMAGEN:
        (carpeta_imagen() / f"{NOMBRE_IMAGEN}{extension}").unlink(missing_ok=True)
    calibracion = CalibracionMapa(cuartel_lat=cuartel_lat, cuartel_lon=cuartel_lon)
    guardar_calibracion(calibracion)
    return calibracion
