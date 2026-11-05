import json
import struct
import zlib
from datetime import date
from unittest import mock

import pytest
import requests

from app.services import cartografia
from app.services import imagen_satelital as sat
from app.services.gee_service import GeePayloadError, GeeService, validar_pedido_pixeles

PEDIDO = sat.PedidoImagen(lat_norte=-33.55, lat_sur=-33.72, lon_oeste=-64.12, lon_este=-63.92,
                          fecha_desde=date(2026, 8, 1), fecha_hasta=date(2026, 11, 1),
                          nubosidad_max=20, resolucion_m=10)


def png(ancho, alto):
    """PNG RGB mínimo y válido de ancho x alto (gris)."""
    def bloque(tipo, datos):
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))
    filas = b"".join(b"\x00" + b"\x80\x80\x80" * ancho for _ in range(alto))
    return (b"\x89PNG\r\n\x1a\n" + bloque(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0))
            + bloque(b"IDAT", zlib.compress(filas)) + bloque(b"IEND", b""))


@pytest.fixture
def carpetas(tmp_path, monkeypatch):
    recursos = tmp_path / "resources"
    recursos.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"ruba": {"usuario": "x"}}), encoding="utf-8")
    monkeypatch.setattr(cartografia, "carpeta_imagen", lambda: recursos)
    monkeypatch.setattr(cartografia, "_ruta_config", lambda: config)
    return recursos, config


# -- Grilla ---------------------------------------------------------------------------

def test_grilla_10m_alineada_a_los_limites():
    g = sat.calcular_grilla(PEDIDO)
    ancho_km, alto_km = sat.area_km(PEDIDO)
    assert g.resolucion_m == 10
    assert abs(g.ancho - ancho_km * 100) <= 1 and abs(g.alto - alto_km * 100) <= 1
    # los bordes de la grilla coinciden exactamente con la calibración
    assert g.escala_x * g.ancho == pytest.approx(PEDIDO.lon_este - PEDIDO.lon_oeste)
    assert -g.escala_y * g.alto == pytest.approx(PEDIDO.lat_norte - PEDIDO.lat_sur)
    assert g.escala_y < 0


def test_area_grande_agranda_el_pixel_hasta_entrar_en_los_limites():
    grande = sat.PedidoImagen(-30, -34, -66, -62, date(2026, 8, 1), date(2026, 11, 1), resolucion_m=10)
    g = sat.calcular_grilla(grande)
    assert g.resolucion_m > 10
    assert g.ancho <= sat.LADO_MAXIMO and g.alto <= sat.LADO_MAXIMO
    assert g.ancho * g.alto * 3 <= sat.MAX_BYTES_PIXELES


@pytest.mark.parametrize("cambios, fragmento", [
    ({"lat_norte": -33.8}, "norte debe ser mayor"),
    ({"fecha_desde": date(2026, 12, 1)}, "anterior"),
    ({"fecha_desde": date(2016, 1, 1)}, "2017"),
    ({"nubosidad_max": 0}, "nubosidad"),
    ({"resolucion_m": 5}, "10 m"),
])
def test_pedido_invalido(cambios, fragmento):
    from dataclasses import replace

    with pytest.raises(sat.ImagenSatelitalError, match=fragmento):
        replace(PEDIDO, **cambios).validar()


def test_limites_alrededor_del_cuartel():
    n, s, o, e = sat.limites_alrededor(-33.6324, -64.0195, 10)
    p = sat.PedidoImagen(n, s, o, e, date(2026, 8, 1), date(2026, 11, 1))
    ancho, alto = sat.area_km(p)
    assert ancho == pytest.approx(20, rel=0.01) and alto == pytest.approx(20, rel=0.01)


# -- Cuerpo de computePixels ------------------------------------------------------------

def _funciones(nodo, encontradas=None):
    """Recorre la expresión y junta {functionName: [arguments...]}."""
    encontradas = {} if encontradas is None else encontradas
    if isinstance(nodo, dict):
        inv = nodo.get("functionInvocationValue")
        if inv:
            encontradas.setdefault(inv["functionName"], []).append(inv.get("arguments", {}))
        for v in nodo.values():
            _funciones(v, encontradas)
    elif isinstance(nodo, list):
        for v in nodo:
            _funciones(v, encontradas)
    return encontradas


def test_cuerpo_compute_pixels_esquema_v1_y_compuesto_sentinel2():
    g = sat.calcular_grilla(PEDIDO)
    cuerpo = sat.pedido_compute_pixels(PEDIDO, g)
    assert set(cuerpo) == {"expression", "fileFormat", "bandIds", "visualizationOptions", "grid"}
    assert cuerpo == validar_pedido_pixeles(cuerpo)            # idempotente y válido
    assert cuerpo["fileFormat"] == "PNG" and cuerpo["bandIds"] == ["B4", "B3", "B2"]
    assert cuerpo["visualizationOptions"] == {"ranges": [{"min": 0, "max": 3000}], "gamma": 1.2}
    assert cuerpo["grid"] == {
        "dimensions": {"width": g.ancho, "height": g.alto},
        "affineTransform": {"scaleX": g.escala_x, "shearX": 0, "translateX": -64.12,
                            "shearY": 0, "scaleY": g.escala_y, "translateY": -33.55},
        "crsCode": "EPSG:4326",
    }

    expr = cuerpo["expression"]
    assert expr["result"] == "0"
    raiz = expr["values"]["0"]["functionInvocationValue"]
    assert raiz["functionName"] == "reduce.median"
    f = _funciones(expr)
    assert f["ImageCollection.load"] == [{"id": {"constantValue": "COPERNICUS/S2_SR_HARMONIZED"}}]
    assert len(f["Collection.filter"]) == 3
    assert f["Filter.intersects"][0]["leftField"] == {"constantValue": ".all"}
    assert "Feature" in f and f["GeometryConstructors.Polygon"][0]["evenOdd"] == {"constantValue": True}
    anillo = f["GeometryConstructors.Polygon"][0]["coordinates"]["constantValue"][0]
    assert anillo[0] == [-64.12, -33.55] and anillo[2] == [-63.92, -33.72]
    rango = f["Filter.dateRangeContains"][0]
    assert rango["rightField"] == {"constantValue": "system:time_start"}
    assert [a["value"]["constantValue"] for a in f["Date"]] == ["2026-08-01", "2026-11-01"]
    nubes = f["Filter.lessThan"][0]
    assert nubes == {"leftField": {"constantValue": "CLOUDY_PIXEL_PERCENTAGE"}, "rightValue": {"constantValue": 20.0}}


@pytest.mark.parametrize("cambio, fragmento", [
    ({"grid": {"dimensions": {"width": 0, "height": 10}}}, "entero entre 1"),
    ({"grid": {"dimensions": {"width": 40000, "height": 10}}}, "entero entre 1"),
    ({"grid": {"crsCode": "EPSG:4326", "crsWkt": "x"}}, "no ambos"),
    ({"grid": {"affineTransform": {"scaleX": 0, "scaleY": -1}}}, "no pueden ser 0"),
    ({"grid": {"region": {}}}, "desconocido"),
    ({"grid": {"dimensions": {"width": 5000, "height": 5000}}}, "48 MB"),
    ({"name": "x"}, "desconocido"),
])
def test_pedido_pixeles_invalido(cambio, fragmento):
    base = {"expression": {"values": {"0": {"constantValue": 1}}, "result": "0"}, "fileFormat": "PNG",
            "bandIds": ["B4", "B3", "B2"]}
    with pytest.raises(GeePayloadError, match=fragmento):
        validar_pedido_pixeles({**base, **cambio})


def test_calcular_pixeles_post_y_bytes():
    session = requests.Session()
    cred = mock.Mock(valid=True, token="tok", expiry=None)
    srv = GeeService(credenciales=cred, project_id="mi-proyecto", session=session)
    resp = requests.Response()
    resp.status_code, resp._content = 200, png(3, 2)
    with mock.patch.object(session, "request", return_value=resp) as req:
        datos = srv.calcular_pixeles(sat.pedido_compute_pixels(PEDIDO))
    assert datos == png(3, 2)
    args, kwargs = req.call_args
    assert args == ("POST", "https://earthengine.googleapis.com/v1/projects/mi-proyecto/image:computePixels")
    assert json.loads(kwargs["data"])["grid"]["crsCode"] == "EPSG:4326"
    assert kwargs["timeout"][1] >= 120


# -- Descarga e instalación ---------------------------------------------------------------

def test_descargar_e_instalar(carpetas):
    recursos, config = carpetas
    (recursos / "mapa_base.jpg").write_bytes(b"vieja")
    g = sat.calcular_grilla(PEDIDO)
    servicio = mock.Mock()
    servicio.calcular_pixeles.return_value = png(g.ancho, g.alto)
    mensajes = []

    imagen = sat.descargar(PEDIDO, servicio=servicio, progreso=mensajes.append)
    assert imagen.ruta == recursos / sat.ARCHIVO_DESCARGA and imagen.ruta.is_file()
    assert servicio.calcular_pixeles.call_args.args[0]["grid"]["dimensions"] == {"width": g.ancho, "height": g.alto}
    assert len(mensajes) == 2 and "Copernicus Sentinel 2026" in imagen.atribucion

    destino, calibracion = sat.instalar(imagen, -33.6324, -64.0195)
    assert destino == recursos / "mapa_base.png" and destino.is_file()
    assert not (recursos / "mapa_base.jpg").exists() and not imagen.ruta.exists()
    guardada = json.loads(config.read_text(encoding="utf-8"))
    assert guardada["ruba"] == {"usuario": "x"}                         # el resto de config intacto
    assert guardada["mapa"]["lat_norte"] == -33.55 and guardada["mapa"]["cuartel_lon"] == -64.0195
    assert cartografia.cargar_calibracion() == calibracion
    origen = sat.origen_actual()
    assert origen["pixeles"] == [g.ancho, g.alto] and "Copernicus" in origen["atribucion"]


def test_descarga_con_tamanio_distinto_se_rechaza(carpetas):
    servicio = mock.Mock()
    servicio.calcular_pixeles.return_value = png(10, 10)
    with pytest.raises(sat.ImagenSatelitalError, match="no coincidiría"):
        sat.descargar(PEDIDO, servicio=servicio)


def test_descarga_que_no_es_png_se_rechaza(carpetas):
    servicio = mock.Mock()
    servicio.calcular_pixeles.return_value = b'{"error": "x"}'
    with pytest.raises(sat.ImagenSatelitalError, match="PNG"):
        sat.descargar(PEDIDO, servicio=servicio)


# -- Diálogo (Qt) -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _esperar(condicion, ms=5000):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    reloj = QTimer()
    reloj.timeout.connect(lambda: loop.quit() if condicion() else None)
    reloj.start(20)
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_dialogo_descarga_en_segundo_plano_e_instala(qapp, carpetas, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    from app.ui.satelital_dialog import DescargaSatelitalDialog

    recursos, _ = carpetas
    pedidos = []

    def descargar_falso(pedido):
        pedidos.append(pedido)
        g = sat.calcular_grilla(pedido)
        ruta = recursos / sat.ARCHIVO_DESCARGA
        ruta.write_bytes(png(g.ancho, g.alto))
        return sat.ImagenDescargada(ruta, pedido, g, ruta.stat().st_size)

    instalar = mock.Mock(return_value=(recursos / "mapa_base.png", None))
    dlg = DescargaSatelitalDialog(cartografia.CalibracionMapa(), descargar=descargar_falso, instalar=instalar)
    dlg.spin_radio.setValue(5)
    dlg._centrar_en_cuartel()
    assert dlg.spin_norte.value() > -33.6324 > dlg.spin_sur.value()
    assert "px" in dlg.label_estimacion.text() and dlg.boton_descargar.isEnabled()
    assert not dlg.boton_usar.isEnabled()

    dlg._iniciar_descarga()
    assert not dlg.boton_descargar.isEnabled()
    _esperar(lambda: dlg._tarea is None)
    assert dlg.imagen is not None and dlg.boton_usar.isEnabled()
    assert pedidos[0].fecha_hasta > pedidos[0].fecha_desde
    assert dlg.label_preview.pixmap() is not None and not dlg.label_preview.pixmap().isNull()

    with mock.patch.object(QMessageBox, "information"):
        dlg._usar_como_base()
    instalar.assert_called_once_with(dlg.imagen, -33.6324, -64.0195)
    assert dlg.instalada


def test_dialogo_muestra_error_de_earth_engine(qapp, carpetas):
    from PySide6.QtWidgets import QMessageBox

    from app.services.gee_service import GeeApiError
    from app.ui.satelital_dialog import DescargaSatelitalDialog

    def falla(_pedido):
        raise GeeApiError("Permission 'earthengine.computations.create' denied")

    dlg = DescargaSatelitalDialog(cartografia.CalibracionMapa(), descargar=falla)
    with mock.patch.object(QMessageBox, "critical") as critical:
        dlg._iniciar_descarga()
        _esperar(lambda: dlg._tarea is None)
    assert "computations.create" in critical.call_args.args[2]
    assert dlg.imagen is None and not dlg.boton_usar.isEnabled() and dlg.boton_descargar.isEnabled()


def test_dialogo_area_invalida_deshabilita_descarga(qapp, carpetas):
    from app.ui.satelital_dialog import DescargaSatelitalDialog

    dlg = DescargaSatelitalDialog(cartografia.CalibracionMapa())
    dlg.spin_norte.setValue(-40)
    assert not dlg.boton_descargar.isEnabled() and "⚠️" in dlg.label_estimacion.text()
