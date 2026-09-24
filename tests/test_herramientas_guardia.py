"""Herramientas de guardia del dashboard: clima (Open-Meteo, simulado) y
guía telefónica (base SQLite temporal). Ningún test sale a internet."""

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time as reloj
from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as db
from app.models import Contacto
from app.ui.widgets import phonebook_widget as pb
from app.ui.widgets import weather_widget as ww


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _respuesta(temp=23.3, humedad=34, viento=14.4, rafagas=31.0, direccion=32, presiones=None):
    """Misma forma que la respuesta real de Open-Meteo (consultada el 23/09/2026)."""
    horas = [f"2026-09-23T{h:02d}:00" for h in range(15, 24)] + [f"2026-09-24T{h:02d}:00" for h in range(0, 4)]
    presiones = presiones or [1008.9 - 0.5 * i for i in range(len(horas))]
    return {
        "timezone": "America/Argentina/Cordoba",
        "current": {
            "time": "2026-09-23T15:15", "temperature_2m": temp, "relative_humidity_2m": humedad,
            "apparent_temperature": 21.3, "weather_code": 2, "wind_speed_10m": viento,
            "wind_direction_10m": direccion, "wind_gusts_10m": rafagas, "pressure_msl": 1008.9, "is_day": 1,
        },
        "hourly": {
            "time": horas,
            "temperature_2m": [23.2 + i * 0.1 for i in range(len(horas))],
            "weather_code": [2, 2, 3, 3, 61, 63, 0, 0, 0, 0, 1, 1, 2],
            "precipitation_probability": [0, 0, 5, 10, 60, 80, 10, 0, 0, 0, 0, 0, 0],
            "wind_speed_10m": [14.1] * len(horas),
            "wind_direction_10m": [30] * len(horas),
            "relative_humidity_2m": [34] * len(horas),
            "pressure_msl": presiones,
            "is_day": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        },
    }


def _esperar(condicion, segundos=5):
    limite = reloj.time() + segundos
    while reloj.time() < limite:
        QApplication.processEvents()
        if condicion():
            return True
        reloj.sleep(0.02)
    return False


# -- Clima --------------------------------------------------------------------

@pytest.mark.parametrize("grados, esperado", [
    (0, "N"), (32, "NNE"), (45, "NE"), (90, "E"), (180, "S"), (225, "SO"), (270, "O"), (315, "NO"), (359, "N"), (None, "—"),
])
def test_rumbo_cardinal(grados, esperado):
    assert ww.grados_a_cardinal(grados) == esperado


def test_parseo_actual_pronostico_y_tendencia():
    r = ww.parsear_open_meteo(_respuesta())
    assert (r.temperatura, r.sensacion, r.humedad) == (23.3, 21.3, 34)
    assert (r.viento_kmh, r.rafagas_kmh, r.viento_cardinal) == (14.4, 31.0, "NNE")
    assert r.presion_hpa == 1008.9
    assert r.tendencia_presion == pytest.approx(-2.0)  # 19:00 (la 1ª hora a ≥3 h) - actual
    assert len(r.pronostico) == 12
    assert r.pronostico[0].hora == datetime(2026, 9, 23, 16, 0)  # la hora en curso (15:00) no cuenta
    assert r.pronostico[3].prob_precipitacion == 60 and not r.pronostico[3].es_dia


def test_regla_30_30_30():
    assert ww.nivel_riesgo_30(33, 18, 35) == "extremo"
    assert ww.nivel_riesgo_30(33, 18, 10) == "alto"
    assert ww.nivel_riesgo_30(25, 40, 35) is None
    assert ww.nivel_riesgo_30(None, None, None) is None


def test_widget_consulta_en_hilo_y_muestra(qapp, tmp_path):
    widget = ww.WeatherWidget(consultar=lambda: _respuesta(temp=32.0, humedad=20, viento=12, rafagas=40),
                              archivo_cache=tmp_path / "clima.json", automatico=False)
    widget.actualizar()
    assert _esperar(lambda: widget.reporte is not None and widget._hilo is None)
    assert widget.label_temperatura.text() == "32.0 °C"
    assert widget._valores["viento"].text().startswith("12 km/h del NNE")
    assert "ráfagas 40" in widget._valores["viento"].text()
    assert widget._valores["presion"].text() == "1009 hPa ↘"
    assert not widget.label_riesgo.isHidden() and "EXTREMO" in widget.label_riesgo.text()  # ráfagas cuentan
    assert widget._layout_pronostico.count() == 12  # grilla de 2 x 6
    assert widget._layout_pronostico.rowCount() == 2
    assert widget.label_estado.text().startswith("Actualizado")


def test_sin_conexion_conserva_lo_ultimo_y_avisa(qapp, tmp_path):
    cache = tmp_path / "clima.json"
    cache.write_text(json.dumps({"obtenido_en": "2026-09-23T14:30:00", "datos": _respuesta()}), encoding="utf-8")

    def sin_red():
        raise OSError("red caída")

    widget = ww.WeatherWidget(consultar=sin_red, archivo_cache=cache, automatico=False)
    assert widget.reporte is not None and widget.label_temperatura.text() == "23.3 °C"  # desde el cache
    widget.actualizar()
    assert _esperar(lambda: widget._hilo is None and "Sin conexión" in widget.label_estado.text())
    assert "datos de las 14:30" in widget.label_estado.text()
    assert widget.label_temperatura.text() == "23.3 °C"


def test_intervalo_de_30_minutos():
    assert ww.INTERVALO_MS == 30 * 60 * 1000


# -- Guía telefónica ------------------------------------------------------------

@pytest.fixture
def guia(qapp, tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()
    pb.guardar_contacto(pb.DatosContacto(None, "Cuarteles Vecinos", "Bomberos Voluntarios de Vicuña Mackenna",
                                         "0358 400-0001", entidad="Federación Córdoba", rubro="Cuartel",
                                         localidad="Vicuña Mackenna"))
    pb.guardar_contacto(pb.DatosContacto(None, "Servicios de Emergencia", "Hospital Municipal", "0358 400-0002",
                                         rubro="Guardia / ambulancia", localidad="Adelia María"))
    pb.guardar_contacto(pb.DatosContacto(None, "Productores Rurales", "Establecimiento La Aguada", "0358 400-0003",
                                         rubro="Cisterna / tractor", localidad="Río Cuarto", notas="Tiene tanque de 10.000 l"))
    return pb.PhonebookWidget(confirmar=lambda pregunta: True)


def test_guia_arranca_solo_con_numeros_oficiales(qapp, tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'vacia.db'}", future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    db.init_db()
    db.init_db()  # idempotente
    with db.get_session() as session:
        assert sorted(c.telefono for c in session.query(Contacto)) == ["100", "103", "107", "911"]


@pytest.mark.parametrize("consulta, esperados", [
    ("hospital", ["Hospital Municipal"]),
    ("HOSPITÁL adelia", ["Hospital Municipal"]),            # sin acentos ni mayúsculas, palabras combinadas
    ("rio cuarto", ["Establecimiento La Aguada"]),          # por localidad
    ("cisterna", ["Establecimiento La Aguada"]),            # por rubro
    ("tanque", ["Establecimiento La Aguada"]),              # por notas
    ("federacion", ["Bomberos Voluntarios de Vicuña Mackenna"]),  # por entidad
    ("4000002", ["Hospital Municipal"]),                    # por número (solo dígitos)
    ("911", ["Emergencias 911"]),
    ("xyz inexistente", []),
])
def test_busqueda_instantanea(guia, consulta, esperados):
    guia.entry_buscar.setText(consulta)
    assert [c.nombre for c in guia.visibles()] == esperados


def test_filtro_por_categoria_y_resumen(guia):
    guia.combo_categoria.setCurrentIndex(guia.combo_categoria.findData("Servicios de Emergencia"))
    assert {c.categoria for c in guia.visibles()} == {"Servicios de Emergencia"}
    assert len(guia.visibles()) == 4  # 911, 100, 107 + Hospital
    assert guia.label_resumen.text() == "4 de 7 contactos"


def test_copiar_al_portapapeles_y_enter_copia_el_primero(guia):
    guia.entry_buscar.setText("hospital")
    guia._filas[0].boton_copiar.click()
    assert QApplication.clipboard().text() == "0358 400-0002"
    assert guia._filas[0].boton_copiar.text() == "✅ Copiado"

    QApplication.clipboard().setText("")
    guia.entry_buscar.setText("vicuña")
    guia.entry_buscar.returnPressed.emit()
    assert QApplication.clipboard().text() == "0358 400-0001"


def test_editar_y_eliminar(guia):
    guia.entry_buscar.setText("hospital")
    (hospital,) = guia.visibles()
    hospital.telefono = "0358 499-9999"
    pb.guardar_contacto(hospital)
    guia.recargar()
    assert guia.visibles()[0].telefono == "0358 499-9999"

    guia._eliminar(guia.visibles()[0])
    assert guia.visibles() == []
    with db.get_session() as session:
        assert session.query(Contacto).filter_by(nombre="Hospital Municipal").count() == 0


def test_validacion_de_contacto():
    malo = pb.DatosContacto(None, "Cuarteles Vecinos", "  ", "llamar al cabo")
    assert pb.validar_contacto(malo) == ["Falta el nombre.",
                                         "El teléfono solo puede tener números, espacios, +, -, ( ) y /."]
    assert pb.validar_contacto(pb.DatosContacto(None, "Defensa Civil", "Base", "+54 (358) 15-400-0000")) == []


def test_dashboard_integra_clima_y_guia(qapp, guia):
    from app.ui import main_window as mw

    ventana = mw.MainWindow()
    assert isinstance(ventana.widget_clima, ww.WeatherWidget)
    assert isinstance(ventana.widget_guia, pb.PhonebookWidget)
    assert ventana.widget_clima._timer.interval() == ww.INTERVALO_MS
    ventana.close()
