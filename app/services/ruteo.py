"""
Ruteo cuartel -> lugar del siniestro vía OSRM (Open Source Routing Machine,
servidor público y gratuito), para el trazado automático de la ruta en el
mapa táctico (ver app/ui/widgets/map_widget.py).

La consulta sale desde Python (urllib en un QThread aparte, igual que la
geocodificación) y no desde el JS del mapa: la página de Leaflet se carga
como file:// y QtWebEngine bloquea por defecto los fetch/XHR de un archivo
local hacia internet (las teselas sí cargan porque son <img>).

Sin internet, o si OSRM no responde o no encuentra ruta, NUNCA se levanta
un error: se devuelve una línea recta con la distancia geodésica
(haversine) y `offline=True`, y el mapa la dibuja discontinua.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from app.services.geocodificacion import USER_AGENT
from app.services.red import obtener_json

TIMEOUT_SEG = 6.0
OSRM_URL = (
    "https://router.project-osrm.org/route/v1/driving/"
    "{lon_origen},{lat_origen};{lon_destino},{lat_destino}?overview=full&geometries=geojson"
)
RADIO_TIERRA_KM = 6371.0088



@dataclass
class ResultadoRuta:
    """`coordenadas`: lista de [lon, lat] (orden GeoJSON). Con `offline=True`
    son solo los dos extremos y `duracion_min` es None (sin estimación)."""

    distancia_km: float
    duracion_min: Optional[float]
    coordenadas: List[List[float]] = field(default_factory=list)
    offline: bool = False
    motivo: str = ""


def distancia_geodesica_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en línea recta sobre la esfera (fórmula de haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * RADIO_TIERRA_KM * math.asin(min(1.0, math.sqrt(a)))


def ruta_en_linea_recta(origen: Tuple[float, float], destino: Tuple[float, float], motivo: str = "") -> ResultadoRuta:
    (lat_o, lon_o), (lat_d, lon_d) = origen, destino
    return ResultadoRuta(
        distancia_km=distancia_geodesica_km(lat_o, lon_o, lat_d, lon_d),
        duracion_min=None,
        coordenadas=[[lon_o, lat_o], [lon_d, lat_d]],
        offline=True,
        motivo=motivo,
    )


def consultar_osrm(origen: Tuple[float, float], destino: Tuple[float, float]) -> ResultadoRuta:
    """Ruta por calle según OSRM. Puede levantar excepción (red, timeout,
    respuesta sin ruta): el que llama decide el fallback."""
    (lat_o, lon_o), (lat_d, lon_d) = origen, destino
    url = OSRM_URL.format(lon_origen=lon_o, lat_origen=lat_o, lon_destino=lon_d, lat_destino=lat_d)
    datos = obtener_json(url, timeout=TIMEOUT_SEG, headers={"User-Agent": USER_AGENT})
    if datos.get("code") != "Ok" or not datos.get("routes"):
        raise ValueError(datos.get("message") or f"OSRM respondió '{datos.get('code')}'")
    ruta = datos["routes"][0]
    return ResultadoRuta(
        distancia_km=float(ruta["distance"]) / 1000.0,
        duracion_min=float(ruta["duration"]) / 60.0,
        coordenadas=ruta["geometry"]["coordinates"],
    )


def calcular_ruta(origen: Tuple[float, float], destino: Tuple[float, float]) -> ResultadoRuta:
    """OSRM si se puede; si no, línea recta. Nunca levanta excepción."""
    try:
        return consultar_osrm(origen, destino)
    except Exception as e:  # noqa: BLE001 - sin internet / OSRM caído -> línea recta, nunca crashea
        return ruta_en_linea_recta(origen, destino, motivo=str(e))


def formatear_distancia(km: float) -> str:
    return f"{km * 1000:.0f} m" if km < 1 else f"{km:.1f} km"


def formatear_duracion(minutos: Optional[float]) -> str:
    if minutos is None:
        return "sin estimación"
    total = max(1, round(minutos))
    if total < 60:
        return f"{total} min"
    return f"{total // 60} h {total % 60:02d} min"


class RutaWorker(QObject):
    # id_consulta, ResultadoRuta (como object: dataclass Python, no tipo Qt)
    resultado = Signal(int, object)
    terminado = Signal()

    def __init__(self, id_consulta: int, origen: Tuple[float, float], destino: Tuple[float, float]) -> None:
        super().__init__()
        self._id = id_consulta
        self._origen = origen
        self._destino = destino

    def run(self) -> None:
        try:
            self.resultado.emit(self._id, calcular_ruta(self._origen, self._destino))
        finally:
            self.terminado.emit()


def lanzar_calculo_ruta(
    id_consulta: int, origen: Tuple[float, float], destino: Tuple[float, float]
) -> Tuple[QThread, RutaWorker]:
    hilo = QThread()
    worker = RutaWorker(id_consulta, origen, destino)
    worker.moveToThread(hilo)

    hilo.started.connect(worker.run)
    worker.terminado.connect(hilo.quit)
    worker.terminado.connect(worker.deleteLater)
    hilo.finished.connect(hilo.deleteLater)

    hilo.start()
    return hilo, worker
