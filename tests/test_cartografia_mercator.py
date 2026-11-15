"""Mapa en Web Mercator (EPSG:3857), carga de TIFF y mapa de la zona de fábrica."""

import json

import pytest

from app.services import cartografia as c

pytestmark = pytest.mark.rapido


@pytest.fixture
def carpetas(tmp_path, monkeypatch):
    datos, recursos = tmp_path / "data", tmp_path / "resources"
    datos.mkdir()
    recursos.mkdir()
    monkeypatch.setattr(c, "_ruta_config", lambda: datos / "config.json")
    monkeypatch.setattr(c, "carpeta_imagen", lambda: recursos)
    return datos, recursos


def test_limites_predeterminados_son_los_del_mapa_tiff():
    cal = c.CalibracionMapa()
    assert cal.proyeccion == c.PROYECCION_MERCATOR and cal.es_valida()
    assert cal.lat_norte == pytest.approx(-33.367024, abs=1e-6)
    assert cal.lat_sur == pytest.approx(-33.849408, abs=1e-6)
    assert cal.lon_este == pytest.approx(-63.291198, abs=1e-6)
    assert cal.lon_oeste == pytest.approx(-64.401042, abs=1e-6)
    # El cuartel cae dentro del mapa.
    assert c.Georreferencia(cal, 1543, 805).contiene(cal.cuartel_lat, cal.cuartel_lon)


def test_georreferencia_mercator_es_lineal_en_metros():
    cal = c.CalibracionMapa()
    geo = c.Georreferencia(cal, 1000, 1000)
    # El punto medio en metros Y (no en grados) cae en la mitad de la imagen.
    y_medio = (c.LIMITES_MERCATOR_PREDETERMINADOS["norte"] + c.LIMITES_MERCATOR_PREDETERMINADOS["sur"]) / 2
    _, y = geo.coords_to_pixel(c.mercator_a_lat(y_medio), cal.lon_oeste)
    assert y == pytest.approx(500, abs=1e-6)
    lat, lon = geo.pixel_to_coords(*geo.coords_to_pixel(-33.6, -64.0))
    assert (lat, lon) == pytest.approx((-33.6, -64.0), abs=1e-9)


def test_config_sin_proyeccion_se_lee_equirectangular(carpetas):
    datos, _ = carpetas
    (datos / "config.json").write_text(json.dumps({"mapa": {
        "lat_norte": -33.0, "lat_sur": -34.0, "lon_oeste": -65.0, "lon_este": -64.0}}), encoding="utf-8")
    assert c.cargar_calibracion().proyeccion == c.PROYECCION_EQUIRECTANGULAR


def test_migra_limites_de_fabrica_viejos_y_conserva_cuartel(carpetas):
    datos, _ = carpetas
    (datos / "config.json").write_text(json.dumps({"ruba": {"usuario": "x"}, "mapa": {
        "lat_norte": -33.55, "lat_sur": -33.72, "lon_oeste": -64.12, "lon_este": -63.92,
        "cuartel_lat": -33.7, "cuartel_lon": -64.1}}), encoding="utf-8")
    c.asegurar_calibracion_en_config()
    guardado = json.loads((datos / "config.json").read_text(encoding="utf-8"))
    assert guardado["ruba"] == {"usuario": "x"}
    assert guardado["mapa"]["proyeccion"] == c.PROYECCION_MERCATOR
    assert (guardado["mapa"]["cuartel_lat"], guardado["mapa"]["cuartel_lon"]) == (-33.7, -64.1)


def test_no_migra_si_el_cuartel_tiene_imagen_propia(carpetas):
    datos, recursos = carpetas
    (recursos / "mapa_base.png").write_bytes(b"x")
    viejo = {"lat_norte": -33.55, "lat_sur": -33.72, "lon_oeste": -64.12, "lon_este": -63.92}
    (datos / "config.json").write_text(json.dumps({"mapa": viejo}), encoding="utf-8")
    c.asegurar_calibracion_en_config()
    assert json.loads((datos / "config.json").read_text(encoding="utf-8"))["mapa"] == viejo


def test_instalar_tiff_lo_convierte_a_png(carpetas, tmp_path):
    from PIL import Image

    _, recursos = carpetas
    (recursos / "mapa_base.jpg").write_bytes(b"vieja")
    origen = tmp_path / "Mapa.tiff"
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(origen, format="TIFF", compression="tiff_lzw")
    destino = c.instalar_imagen_base(origen)
    assert destino == recursos / "mapa_base.png"
    with Image.open(destino) as imagen:
        assert imagen.format == "PNG" and imagen.size == (40, 20)
    assert not (recursos / "mapa_base.jpg").exists()
    assert c.ruta_imagen_base() == destino


def test_tiff_danado_no_toca_la_imagen_actual(carpetas, tmp_path):
    _, recursos = carpetas
    (recursos / "mapa_base.png").write_bytes(b"actual")
    roto = tmp_path / "roto.tif"
    roto.write_bytes(b"no es un tiff")
    with pytest.raises(ValueError):
        c.instalar_imagen_base(roto)
    assert (recursos / "mapa_base.png").read_bytes() == b"actual"


def test_sin_imagen_propia_usa_la_predeterminada(carpetas):
    if c.ruta_imagen_predeterminada() is None:
        pytest.skip("resources/mapa_predeterminado.png no está en este checkout")
    assert c.usa_imagen_predeterminada()
    assert c.ruta_imagen_base() == c.ruta_imagen_predeterminada()


def test_desde_mercator_con_los_bordes_del_cuartel():
    m = c.LIMITES_MERCATOR_PREDETERMINADOS
    assert c.CalibracionMapa.desde_mercator(m["norte"], m["sur"], m["este"], m["oeste"]) == c.CalibracionMapa()
