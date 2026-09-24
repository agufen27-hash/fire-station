"""
Geocodificación de direcciones (texto -> lat/lon) vía Nominatim
(OpenStreetMap), para el botón "Buscar dirección" del mapa táctico.

Corre en un QThread aparte -- igual que la sincronización con RUBA (Fase 3)
y el chequeo de conectividad (Fase 4) -- para no bloquear la UI con una
petición de red. Se dispara SOLO cuando el operador lo pide a mano con el
botón (nunca automático en cada tecla), respetando la política de uso de
Nominatim (que pide no automatizar consultas en bucle y mandar un
User-Agent identificable).
"""

from __future__ import annotations

import urllib.parse
from typing import Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from app.services.red import obtener_json

TIMEOUT_SEG = 8.0
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "FireStation-BomberosAdeliaMaria/1.0 (uso interno, geocodificacion de siniestros)"
LOCALIDAD_POR_DEFECTO = "Adelia María, Córdoba, Argentina"


def geocodificar(direccion: str, localidad: str = LOCALIDAD_POR_DEFECTO) -> Optional[Tuple[float, float]]:
    """Devuelve (lat, lon) para `direccion`, o None si Nominatim no
    encontró nada. Puede levantar una excepción de red (sin internet,
    timeout, etc.) -- la llama el caller dentro de un try/except."""
    consulta = f"{direccion}, {localidad}" if localidad else direccion
    url = f"{NOMINATIM_URL}?{urllib.parse.urlencode({'q': consulta, 'format': 'json', 'limit': 1})}"
    resultados = obtener_json(url, timeout=TIMEOUT_SEG, headers={"User-Agent": USER_AGENT})
    if not resultados:
        return None
    return float(resultados[0]["lat"]), float(resultados[0]["lon"])


class GeocodeWorker(QObject):
    resultado = Signal(bool, float, float, str)  # exito, lat, lng, mensaje_error
    terminado = Signal()

    def __init__(self, direccion: str, localidad: str = LOCALIDAD_POR_DEFECTO) -> None:
        super().__init__()
        self._direccion = direccion
        self._localidad = localidad

    def run(self) -> None:
        try:
            coords = geocodificar(self._direccion, self._localidad)
        except Exception as e:  # noqa: BLE001 - cualquier fallo de red/parseo se reporta, nunca crashea
            self.resultado.emit(False, 0.0, 0.0, str(e))
        else:
            if coords is None:
                self.resultado.emit(False, 0.0, 0.0, "No se encontró esa dirección.")
            else:
                lat, lng = coords
                self.resultado.emit(True, lat, lng, "")
        finally:
            self.terminado.emit()


def lanzar_geocodificacion(direccion: str, localidad: str = LOCALIDAD_POR_DEFECTO):
    hilo = QThread()
    worker = GeocodeWorker(direccion, localidad)
    worker.moveToThread(hilo)

    hilo.started.connect(worker.run)
    worker.terminado.connect(hilo.quit)
    worker.terminado.connect(worker.deleteLater)
    hilo.finished.connect(hilo.deleteLater)

    hilo.start()
    return hilo, worker
