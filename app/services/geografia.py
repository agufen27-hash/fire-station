"""
Importación/exportación de geometrías de campo (KML, KMZ, GPX) para el
módulo de Cartografía Táctica (ver app/ui/widgets/map_widget.py).

Deliberadamente sin `fastkml` ni `geopandas`: KML y GPX son XML simple y
`xml.etree.ElementTree` (stdlib) alcanza de sobra para extraer un polígono
(o una traza de GPS que se toma como el perímetro recorrido a pie) sin
sumar dependencias pesadas al empaquetado.

Todo el módulo trabaja con GeoJSON (dict con forma Feature/Polygon) como
formato intermedio único -- es el mismo formato que guarda el visor de
mapa (`geometria_geojson` del incidente), así que no hace falta traducir
nada más.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _escapar_xml

Punto = Tuple[float, float]  # (longitud, latitud) -- orden GeoJSON, no (lat, lon)


def _tag_local(elem: ET.Element) -> str:
    """Nombre de la etiqueta sin el namespace XML (`{...}nombre` -> `nombre`)
    -- KML/GPX de distintos exportadores (Google Earth, Avenza, Gaia GPS,
    Garmin) declaran namespaces ligeramente distintos; ignorarlo es más
    robusto que hardcodear una URI de namespace específica."""
    return elem.tag.rsplit("}", 1)[-1]


def _iterfind_local(raiz: ET.Element, nombre: str) -> Iterator[ET.Element]:
    for elem in raiz.iter():
        if _tag_local(elem) == nombre:
            yield elem


def _puntos_a_geojson_poligono(puntos: List[Punto]) -> dict:
    anillo = list(puntos)
    if anillo[0] != anillo[-1]:
        anillo.append(anillo[0])  # GeoJSON exige que el anillo cierre sobre sí mismo
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in anillo]]},
    }


def _parsear_kml(contenido: bytes) -> Optional[dict]:
    raiz = ET.fromstring(contenido)
    for elem_coordenadas in _iterfind_local(raiz, "coordinates"):
        texto = (elem_coordenadas.text or "").strip()
        if not texto:
            continue
        puntos: List[Punto] = []
        for grupo in texto.split():
            partes = grupo.split(",")
            if len(partes) >= 2:
                try:
                    puntos.append((float(partes[0]), float(partes[1])))
                except ValueError:
                    continue
        if len(puntos) >= 3:
            return _puntos_a_geojson_poligono(puntos)
    return None


def _parsear_gpx(contenido: bytes) -> Optional[dict]:
    raiz = ET.fromstring(contenido)
    for etiqueta in ("trkpt", "rtept"):
        puntos: List[Punto] = []
        for elem in _iterfind_local(raiz, etiqueta):
            lat, lon = elem.get("lat"), elem.get("lon")
            if lat is not None and lon is not None:
                try:
                    puntos.append((float(lon), float(lat)))
                except ValueError:
                    continue
        if len(puntos) >= 3:
            return _puntos_a_geojson_poligono(puntos)
    return None


def _leer_kml_de_kmz(ruta: Path) -> bytes:
    with zipfile.ZipFile(ruta) as z:
        nombres_kml = [n for n in z.namelist() if n.lower().endswith(".kml")]
        if not nombres_kml:
            raise ValueError("El KMZ no contiene ningún archivo .kml adentro.")
        return z.read(nombres_kml[0])


def importar_geometria(ruta: Path) -> dict:
    """Lee un .kml/.kmz/.gpx y devuelve un polígono GeoJSON (Feature).
    Levanta ValueError con un mensaje para mostrar al operador si el
    archivo no tiene ninguna geometría reconocible."""
    sufijo = ruta.suffix.lower()
    if sufijo == ".kmz":
        geojson = _parsear_kml(_leer_kml_de_kmz(ruta))
    elif sufijo == ".kml":
        geojson = _parsear_kml(ruta.read_bytes())
    elif sufijo == ".gpx":
        geojson = _parsear_gpx(ruta.read_bytes())
    else:
        raise ValueError(f"Formato no soportado: '{sufijo}' (usá .kml, .kmz o .gpx).")

    if geojson is None:
        raise ValueError(
            "No se encontró ningún polígono ni traza con 3 o más puntos en el archivo."
        )
    return geojson


def exportar_kml(numero_parte: str, lat: Optional[float], lng: Optional[float], geojson_texto: Optional[str]) -> str:
    """Arma un KML mínimo válido con el punto del incidente (si hay) y el
    polígono del área afectada (si hay), para compartir con Defensa Civil o
    el Plan Provincial de Manejo del Fuego."""
    partes = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        f"<name>Siniestro N° {_escapar_xml(numero_parte)}</name>",
    ]

    if lat is not None and lng is not None:
        partes.append(
            "<Placemark><name>Punto del incidente</name>"
            f"<Point><coordinates>{lng},{lat},0</coordinates></Point></Placemark>"
        )

    if geojson_texto:
        datos = json.loads(geojson_texto)
        anillo = datos["geometry"]["coordinates"][0]
        texto_coords = " ".join(f"{lon},{lat},0" for lon, lat in anillo)
        partes.append(
            "<Placemark><name>Área afectada</name><Polygon><outerBoundaryIs><LinearRing>"
            f"<coordinates>{texto_coords}</coordinates>"
            "</LinearRing></outerBoundaryIs></Polygon></Placemark>"
        )

    partes.append("</Document></kml>")
    return "\n".join(partes)
