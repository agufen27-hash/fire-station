"""Mapa táctico con Leaflet + OpenStreetMap (QWebEngineView): estado en
Python, clics que llegan por el puente JS y llamadas encoladas a la página."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import pytest
from PySide6.QtWidgets import QApplication

from app.paths import get_resource_path
from app.ui.widgets import map_widget
from app.ui.widgets.map_widget import RUTA_PAGINA_MAPA, MapWidget, _esperar

pytestmark = pytest.mark.rapido

CLICS_POLIGONO = [(-33.6280, -64.0245), (-33.6280, -64.0215), (-33.6300, -64.0215), (-33.6300, -64.0245)]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def mapa(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(map_widget, "get_writable_dir", lambda sub: tmp_path)
    monkeypatch.setattr(map_widget, "_PERFIL", None)
    w = MapWidget()
    yield w
    w.deleteLater()


def test_assets_del_mapa_existen():
    pagina = get_resource_path(RUTA_PAGINA_MAPA)
    assert pagina.is_file()
    for asset in ("leaflet.js", "leaflet.css", "qwebchannel.js"):
        assert (pagina.parent / asset).is_file(), asset
    html = pagina.read_text(encoding="utf-8")
    assert "tile.openstreetmap.org" in html and "new QWebChannel" in html


def test_clic_del_operador_marca_el_punto_y_emite(mapa):
    recibidas = []
    mapa.coordenadas_cambiadas.connect(lambda la, lo: recibidas.append((la, lo)))
    mapa._puente.clic(-33.629, -64.023)
    assert mapa.punto() == (-33.629, -64.023)
    assert recibidas == [(-33.629, -64.023)]
    assert mapa.obtener_datos()["latitud"] == -33.629
    assert "en línea recta" in mapa.label_distancia.text()


def test_cargar_incidente_no_emite_y_limpiar_resetea(mapa):
    recibidas = []
    mapa.coordenadas_cambiadas.connect(lambda la, lo: recibidas.append((la, lo)))
    mapa.cargar_incidente("12/2026", -33.63, -64.02, 3.5, None)
    assert mapa.punto() == (-33.63, -64.02) and recibidas == []
    assert mapa.obtener_datos()["superficie_ha"] == 3.5  # superficie estimada sin polígono
    mapa.limpiar()
    assert mapa.obtener_datos() == {"latitud": None, "longitud": None, "superficie_ha": None,
                                    "geometria_geojson": None}


def test_dibujar_poligono_con_clics_y_doble_clic(mapa):
    mapa._alternar_dibujo()
    for lat, lon in CLICS_POLIGONO:
        mapa._puente.clic(lat, lon)
    mapa._puente.clic(*CLICS_POLIGONO[-1])  # el doble clic repite el último vértice
    mapa._puente.doble_clic()
    datos = mapa.obtener_datos()
    assert datos["latitud"] is None  # en modo dibujo los clics no mueven la chincheta
    anillo = json.loads(datos["geometria_geojson"])["geometry"]["coordinates"][0]
    assert len(anillo) == 5 and anillo[0] == anillo[-1]
    assert 5.5 < datos["superficie_ha"] < 7.0


def test_escape_cancela_el_dibujo(mapa):
    mapa._alternar_dibujo()
    mapa._puente.clic(*CLICS_POLIGONO[0])
    mapa._puente.tecla("escape")
    assert mapa.obtener_datos()["geometria_geojson"] is None
    assert mapa._modo == map_widget.MODO_NORMAL


def test_editar_vertice_recalcula_superficie(mapa):
    mapa._alternar_dibujo()
    for lat, lon in CLICS_POLIGONO:
        mapa._puente.clic(lat, lon)
    mapa._puente.tecla("enter")
    antes = mapa.obtener_datos()["superficie_ha"]
    mapa._habilitar_edicion_poligono()
    mapa._puente.vertice_movido(2, -33.6320, -64.0215)  # estira el borde sur
    assert mapa.obtener_datos()["superficie_ha"] > antes
    mapa._puente.clic(-33.5, -64.5)  # clic fuera de los vértices: termina la edición sin mover el punto
    assert mapa._modo == map_widget.MODO_NORMAL and mapa.punto() is None


def test_solo_lectura_no_acepta_clics(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(map_widget, "get_writable_dir", lambda sub: tmp_path)
    w = MapWidget(modo_operativo=True)
    w._puente.clic(-33.6, -64.0)
    assert w.punto() is None


def test_llamadas_antes_de_cargar_se_encolan_en_orden(mapa):
    if mapa.pagina_cargada:
        pytest.skip("la página ya cargó")
    mapa.encuadrar(-33.63, -64.02, 2.0)
    mapa.centrar_en(-33.64, -64.03)
    assert mapa._pendientes[-2:] == ["fs.encuadrar(-33.63, -64.02, 2.0);", "fs.centrar(-33.64, -64.03);"]


def test_dentro_del_mapa_valida_rangos_wgs84(mapa):
    assert mapa.dentro_del_mapa(-33.6, -64.0) and mapa.dentro_del_mapa(60.0, 120.0)
    assert not mapa.dentro_del_mapa(-91, 0) and not mapa.dentro_del_mapa(0, 181)


def test_la_pagina_carga_y_responde_al_puente(mapa):
    """Integración real con QtWebEngine: la página avisa `listo` y ejecuta lo encolado."""
    mapa.resize(800, 600)
    mapa.show()
    if not mapa.pagina_cargada:
        _esperar(20000, mapa._puente.pagina_lista)
    if not mapa.pagina_cargada:
        pytest.skip("QtWebEngine no levantó en este entorno")
    mapa.fijar_punto(-33.629, -64.023, emitir=False)
    resultado = {}
    _esperar(300)
    mapa._pagina.runJavaScript("JSON.stringify(siniestro.getLatLng())", 0, lambda r: resultado.setdefault("r", r))
    _esperar(1500)
    assert json.loads(resultado["r"]) == {"lat": -33.629, "lng": -64.023}
