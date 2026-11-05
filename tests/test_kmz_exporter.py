import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

import pytest

from app.utils.kmz_exporter import (
    ExportadorKMZ,
    TipoZona,
    color_kml,
    exportar_zonas_kmz,
    validar_anillo,
)

NS = {"k": "http://www.opengis.net/kml/2.2"}
TRIANGULO = [(-33.63, -64.01), (-33.64, -64.02), (-33.62, -64.03)]


def _leer_kml(ruta):
    with zipfile.ZipFile(ruta) as z:
        assert z.namelist() == ["doc.kml"]
        return ET.fromstring(z.read("doc.kml"))


def test_color_kml_orden_aabbggrr():
    assert color_kml("#FF3B30", 90) == "5a303bff"
    assert color_kml("007AFF") == "ffff7a00"
    with pytest.raises(ValueError):
        color_kml("#12345")


def test_validar_anillo_normaliza_y_rechaza():
    cerrado = TRIANGULO + [TRIANGULO[0]]
    assert validar_anillo(cerrado) == TRIANGULO
    assert validar_anillo([TRIANGULO[0], TRIANGULO[0], *TRIANGULO[1:]]) == TRIANGULO
    with pytest.raises(ValueError, match="3 vértices"):
        validar_anillo(TRIANGULO[:2])
    with pytest.raises(ValueError, match="fuera de rango"):
        validar_anillo([(-133.63, -64.01), *TRIANGULO])
    with pytest.raises(ValueError, match="inválido"):
        validar_anillo([("a", 1), *TRIANGULO])


def test_kmz_con_poligono_semitransparente_en_lon_lat(tmp_path):
    exp = ExportadorKMZ("Prueba")
    exp.agregar_zona("Perímetro", TRIANGULO, TipoZona.PERIMETRO, "Incendio")
    exp.agregar_zona("Búsqueda norte", TRIANGULO, TipoZona.BUSQUEDA)
    exp.agregar_punto("PC", -33.6315, -64.0152)
    ruta = exp.guardar(tmp_path / "zonas")          # sin extensión: se agrega .kmz
    assert ruta.name == "zonas.kmz"
    assert not list(tmp_path.glob("*.tmp"))

    raiz = _leer_kml(ruta)
    carpetas = [f.find("k:name", NS).text for f in raiz.iter(f"{{{NS['k']}}}Folder")]
    assert carpetas == ["Perímetro de interés", "Zona de búsqueda"]

    coords = raiz.find(".//k:Polygon//k:coordinates", NS).text.split()
    primero = tuple(float(v) for v in coords[0].split(",")[:2])
    assert primero == (-64.01, -33.63)               # KML = lon,lat
    assert coords[0] == coords[-1] and len(coords) == 4  # anillo cerrado

    alfas = {int(c.text[:2], 16) for c in raiz.iter(f"{{{NS['k']}}}PolyStyle") for c in c.iter(f"{{{NS['k']}}}color")}
    assert alfas and all(0 < a < 255 for a in alfas)  # relleno semitransparente
    assert raiz.find(".//k:Point/k:coordinates", NS).text.startswith("-64.0152,-33.6315")


def test_guardar_sin_contenido_falla(tmp_path):
    with pytest.raises(ValueError):
        ExportadorKMZ().guardar(tmp_path / "vacio.kmz")


def test_atajo_exportar_zonas(tmp_path):
    ruta = exportar_zonas_kmz(tmp_path / "a.kmz", [("Despliegue", TRIANGULO, TipoZona.DESPLIEGUE, "")])
    assert _leer_kml(ruta).find(".//k:Placemark/k:name", NS).text == "Despliegue"


# -- Diálogo (Qt) -------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_dialogo_exporta_en_segundo_plano(qapp, tmp_path):
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from app.ui.kmz_export_dialog import COL_INCLUIR, COL_TIPO, ExportarZonasKmzDialog, ZonaExportable

    zonas = [ZonaExportable("Parte N° 1", TRIANGULO, "uno", punto=(-33.63, -64.02)),
             ZonaExportable("Parte N° 2", TRIANGULO, "dos")]
    dlg = ExportarZonasKmzDialog(zonas)
    from PySide6.QtCore import Qt

    dlg.tabla.item(1, COL_INCLUIR).setCheckState(Qt.CheckState.Unchecked)
    combo = dlg.tabla.cellWidget(0, COL_TIPO)
    combo.setCurrentIndex(combo.findData(TipoZona.DESPLIEGUE))
    elegidas = dlg.zonas_elegidas()
    assert [(z.nombre, z.tipo) for z in elegidas] == [("Parte N° 1", TipoZona.DESPLIEGUE)]

    destino = tmp_path / "salida.kmz"
    with mock.patch.object(QFileDialog, "getSaveFileName", return_value=(str(destino), "")), \
            mock.patch.object(QMessageBox, "information") as info:
        dlg._exportar()
        assert dlg._tarea is not None and not dlg.boton_exportar.isEnabled()
        loop = QEventLoop()
        espera = QTimer()
        espera.timeout.connect(lambda: loop.quit() if dlg._tarea is None else None)
        espera.start(20)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
    assert dlg._tarea is None and dlg.ruta_exportada == destino
    info.assert_called_once()
    raiz = _leer_kml(destino)
    assert [p.find("k:name", NS).text for p in raiz.iter(f"{{{NS['k']}}}Placemark")] == ["Parte N° 1", "Parte N° 1"]
    assert dlg.boton_exportar.isEnabled()
