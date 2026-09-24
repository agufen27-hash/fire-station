"""
Cartografía base del visor de mapa nativo (estilo Avenza Maps, ver
app/ui/widgets/map_widget.py): una imagen local (carta topográfica,
captura satelital) georreferenciada por sus cuatro bordes.

  - Calibración: sección "mapa" de data/config.json (lat_norte, lat_sur,
    lon_oeste, lon_este + ubicación del cuartel). Si falta, se usan los
    valores por defecto de Adelia María y zona de influencia.
  - Imagen: `resources/mapa_base.png|jpg|jpeg` en la carpeta ESCRIBIBLE
    (al lado del .exe si está empaquetado): el cuartel la reemplaza desde
    Configuración sin tocar código ni reempaquetar.
  - Conversión píxel <-> GPS: interpolación lineal entre los bordes
    (proyección equirectangular). Para una zona de ~20 km el error frente a
    una carta en Mercator/UTM es de metros, despreciable para despacho.
    La imagen tiene que estar orientada al norte y sin rotar.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Tuple

from app.paths import get_writable_dir

EXTENSIONES_IMAGEN = (".png", ".jpg", ".jpeg")
NOMBRE_IMAGEN = "mapa_base"
RADIO_TIERRA_KM = 6371.0088


@dataclass
class CalibracionMapa:
    lat_norte: float = -33.55
    lat_sur: float = -33.72
    lon_oeste: float = -64.12
    lon_este: float = -63.92
    cuartel_lat: float = -33.6324
    cuartel_lon: float = -64.0195

    def es_valida(self) -> bool:
        return (
            -90 <= self.lat_sur < self.lat_norte <= 90
            and -180 <= self.lon_oeste < self.lon_este <= 180
        )

    def relacion_aspecto(self) -> float:
        """Ancho/alto en km del recuadro (para el fondo generado por código)."""
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

    def pixel_to_coords(self, x: float, y: float) -> Tuple[float, float]:
        c = self.calibracion
        lon = c.lon_oeste + (x / self.ancho) * (c.lon_este - c.lon_oeste)
        lat = c.lat_norte - (y / self.alto) * (c.lat_norte - c.lat_sur)
        return lat, lon

    def coords_to_pixel(self, lat: float, lon: float) -> Tuple[float, float]:
        c = self.calibracion
        x = (lon - c.lon_oeste) / (c.lon_este - c.lon_oeste) * self.ancho
        y = (c.lat_norte - lat) / (c.lat_norte - c.lat_sur) * self.alto
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
        calibracion = CalibracionMapa(**{
            clave: float(seccion.get(clave, valor)) for clave, valor in asdict(por_defecto).items()
        })
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


def asegurar_calibracion_en_config() -> None:
    """Al arrancar la app: si config.json todavía no tiene la sección "mapa",
    se agrega con los valores por defecto para que quede a mano para editar."""
    ruta = _ruta_config()
    if not ruta.exists():
        return
    datos = _leer_config()
    if isinstance(datos.get("mapa"), dict):
        return
    datos["mapa"] = asdict(CalibracionMapa())
    try:
        ruta.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# -- Imagen base ---------------------------------------------------------------------

def carpeta_imagen() -> Path:
    return get_writable_dir("resources")


def ruta_imagen_base() -> Optional[Path]:
    """La imagen cargada (png > jpg > jpeg), o None si todavía no hay."""
    for extension in EXTENSIONES_IMAGEN:
        ruta = carpeta_imagen() / f"{NOMBRE_IMAGEN}{extension}"
        if ruta.is_file():
            return ruta
    return None


def instalar_imagen_base(origen: Path) -> Path:
    """Copia `origen` como la nueva imagen base y borra las de otra
    extensión (si no, un mapa_base.png viejo le ganaría a un .jpg nuevo)."""
    extension = origen.suffix.lower()
    if extension not in EXTENSIONES_IMAGEN:
        raise ValueError(f"Formato no soportado: {extension or '(sin extensión)'}. Usá PNG o JPG.")
    destino = carpeta_imagen() / f"{NOMBRE_IMAGEN}{extension}"
    if origen.resolve() != destino.resolve():
        shutil.copyfile(origen, destino)
    for otra in EXTENSIONES_IMAGEN:
        if otra != extension:
            (carpeta_imagen() / f"{NOMBRE_IMAGEN}{otra}").unlink(missing_ok=True)
    return destino
