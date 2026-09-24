"""
Widget meteorológico de Adelia María (Open-Meteo, sin API key).

- Condiciones actuales: temperatura, sensación térmica, humedad, viento
  (velocidad, dirección cardinal y ráfagas) y presión con su tendencia a 3 h.
- Pronóstico de las próximas 12 horas en tarjetas compactas.
- Aviso por la "regla 30-30-30" de incendios forestales (≥30 °C, ≤30 % de
  humedad, ≥30 km/h de viento).

La consulta corre en un QThread y se repite cada 30 minutos. La última
respuesta queda en data/clima_cache.json: al arrancar (o sin internet) se
muestra lo último conocido, con su hora.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services.red import obtener_json
from app.ui import theme

LATITUD = -33.632
LONGITUD = -64.019
LOCALIDAD = "Adelia María"
ZONA_HORARIA = "America/Argentina/Cordoba"
HORAS_PRONOSTICO = 12
COLUMNAS_PRONOSTICO = 6
INTERVALO_MS = 30 * 60 * 1000
TIMEOUT_SEG = 6
USER_AGENT = "FireStationApp/1.0"
TEXTO_SIN_CONEXION = "Sin conexión meteorológica"
URL_API = "https://api.open-meteo.com/v1/forecast"

VARIABLES_ACTUALES = ("temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,"
                      "wind_direction_10m,wind_gusts_10m,pressure_msl,is_day")
VARIABLES_HORARIAS = ("temperature_2m,weather_code,precipitation_probability,wind_speed_10m,"
                      "wind_direction_10m,relative_humidity_2m,pressure_msl,is_day")

# Códigos WMO (Open-Meteo) -> (ícono de día, ícono de noche, descripción)
CODIGOS_WMO: Dict[int, Tuple[str, str, str]] = {
    0: ("☀️", "🌙", "Despejado"),
    1: ("🌤️", "🌙", "Mayormente despejado"),
    2: ("⛅", "☁️", "Parcialmente nublado"),
    3: ("☁️", "☁️", "Nublado"),
    45: ("🌫️", "🌫️", "Niebla"),
    48: ("🌫️", "🌫️", "Niebla con escarcha"),
    51: ("🌦️", "🌧️", "Llovizna débil"),
    53: ("🌦️", "🌧️", "Llovizna"),
    55: ("🌧️", "🌧️", "Llovizna intensa"),
    56: ("🌧️", "🌧️", "Llovizna helada"),
    57: ("🌧️", "🌧️", "Llovizna helada intensa"),
    61: ("🌦️", "🌧️", "Lluvia débil"),
    63: ("🌧️", "🌧️", "Lluvia"),
    65: ("🌧️", "🌧️", "Lluvia intensa"),
    66: ("🌧️", "🌧️", "Lluvia helada"),
    67: ("🌧️", "🌧️", "Lluvia helada intensa"),
    71: ("🌨️", "🌨️", "Nevada débil"),
    73: ("🌨️", "🌨️", "Nevada"),
    75: ("❄️", "❄️", "Nevada intensa"),
    77: ("🌨️", "🌨️", "Granos de nieve"),
    80: ("🌦️", "🌧️", "Chaparrones débiles"),
    81: ("🌧️", "🌧️", "Chaparrones"),
    82: ("⛈️", "⛈️", "Chaparrones violentos"),
    85: ("🌨️", "🌨️", "Chaparrones de nieve"),
    86: ("🌨️", "🌨️", "Chaparrones de nieve intensos"),
    95: ("⛈️", "⛈️", "Tormenta"),
    96: ("⛈️", "⛈️", "Tormenta con granizo"),
    99: ("⛈️", "⛈️", "Tormenta con granizo fuerte"),
}

PUNTOS_CARDINALES = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                     "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]


def grados_a_cardinal(grados: Optional[float]) -> str:
    """Rosa de 16 rumbos en castellano (O = oeste). Es la dirección DESDE la que sopla."""
    if grados is None:
        return "—"
    return PUNTOS_CARDINALES[int((grados % 360) / 22.5 + 0.5) % 16]


def icono_y_descripcion(codigo: Optional[int], es_dia: bool = True) -> Tuple[str, str]:
    dia, noche, descripcion = CODIGOS_WMO.get(codigo, ("🌡️", "🌡️", "Sin datos"))
    return (dia if es_dia else noche), descripcion


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

@dataclass
class HoraPronostico:
    hora: datetime
    temperatura: Optional[float]
    codigo: Optional[int]
    prob_precipitacion: Optional[int]
    viento_kmh: Optional[float]
    viento_cardinal: str
    humedad: Optional[int]
    es_dia: bool = True


@dataclass
class ReporteClima:
    hora: datetime
    temperatura: Optional[float]
    sensacion: Optional[float]
    humedad: Optional[int]
    viento_kmh: Optional[float]
    viento_grados: Optional[float]
    rafagas_kmh: Optional[float]
    presion_hpa: Optional[float]
    tendencia_presion: Optional[float]  # hPa en las próximas 3 h (negativo = baja)
    codigo: Optional[int]
    es_dia: bool
    pronostico: List[HoraPronostico] = field(default_factory=list)

    @property
    def viento_cardinal(self) -> str:
        return grados_a_cardinal(self.viento_grados)


def nivel_riesgo_30(temperatura: Optional[float], humedad: Optional[float], viento: Optional[float]) -> Optional[str]:
    """Regla 30-30-30: 'extremo' si se cumplen las tres, 'alto' si dos, None si menos."""
    condiciones = [
        temperatura is not None and temperatura >= 30,
        humedad is not None and humedad <= 30,
        viento is not None and viento >= 30,
    ]
    cumplidas = sum(condiciones)
    return "extremo" if cumplidas == 3 else "alto" if cumplidas == 2 else None


def consultar_open_meteo(latitud: float = LATITUD, longitud: float = LONGITUD, timeout: float = TIMEOUT_SEG) -> Dict[str, Any]:
    parametros = {
        "latitude": latitud, "longitude": longitud,
        "current": VARIABLES_ACTUALES, "hourly": VARIABLES_HORARIAS,
        "forecast_hours": HORAS_PRONOSTICO + 1,  # la primera es la hora en curso
        "timezone": ZONA_HORARIA, "wind_speed_unit": "kmh",
    }
    url = f"{URL_API}?{urllib.parse.urlencode(parametros)}"
    # obtener_json resuelve el certificado raíz vencido de Windows (causa de
    # que el clima nunca cargara: CERTIFICATE_VERIFY_FAILED en cada consulta).
    return obtener_json(url, timeout=timeout, headers={"User-Agent": USER_AGENT})


def parsear_open_meteo(datos: Dict[str, Any]) -> ReporteClima:
    actual = datos["current"]
    hora_actual = datetime.fromisoformat(actual["time"])
    horario = datos.get("hourly", {})
    horas = [datetime.fromisoformat(t) for t in horario.get("time", [])]

    def serie(clave: str, i: int):
        valores = horario.get(clave) or []
        return valores[i] if i < len(valores) else None

    pronostico = [
        HoraPronostico(
            hora=h, temperatura=serie("temperature_2m", i), codigo=serie("weather_code", i),
            prob_precipitacion=serie("precipitation_probability", i), viento_kmh=serie("wind_speed_10m", i),
            viento_cardinal=grados_a_cardinal(serie("wind_direction_10m", i)),
            humedad=serie("relative_humidity_2m", i), es_dia=bool(serie("is_day", i) if serie("is_day", i) is not None else 1),
        )
        for i, h in enumerate(horas) if h > hora_actual
    ][:HORAS_PRONOSTICO]

    presion = actual.get("pressure_msl")
    tendencia = None
    en_3h = next((i for i, h in enumerate(horas) if (h - hora_actual).total_seconds() >= 3 * 3600), None)
    if presion is not None and en_3h is not None and serie("pressure_msl", en_3h) is not None:
        tendencia = round(serie("pressure_msl", en_3h) - presion, 1)

    return ReporteClima(
        hora=hora_actual, temperatura=actual.get("temperature_2m"), sensacion=actual.get("apparent_temperature"),
        humedad=actual.get("relative_humidity_2m"), viento_kmh=actual.get("wind_speed_10m"),
        viento_grados=actual.get("wind_direction_10m"), rafagas_kmh=actual.get("wind_gusts_10m"),
        presion_hpa=presion, tendencia_presion=tendencia, codigo=actual.get("weather_code"),
        es_dia=bool(actual.get("is_day", 1)), pronostico=pronostico,
    )


def ruta_cache() -> Path:
    from app.paths import get_writable_dir

    return get_writable_dir("data") / "clima_cache.json"


# ---------------------------------------------------------------------------
# Hilo de consulta
# ---------------------------------------------------------------------------

class ClimaWorker(QObject):
    listo = Signal(dict)
    fallo = Signal(str)
    terminado = Signal()

    def __init__(self, consultar: Callable[[], Dict[str, Any]]) -> None:
        super().__init__()
        self._consultar = consultar

    def run(self) -> None:
        try:
            self.listo.emit(self._consultar())
        except Exception as e:  # noqa: BLE001 - sin internet / API caída: se informa y se sigue
            self.fallo.emit(f"{type(e).__name__}: {e}")
        finally:
            self.terminado.emit()


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class _TarjetaHora(QFrame):
    def __init__(self, h: HoraPronostico, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("tarjetaHora")
        self.setMinimumWidth(64)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 6)
        layout.setSpacing(2)
        icono, descripcion = icono_y_descripcion(h.codigo, h.es_dia)
        self.setToolTip(f"{h.hora:%H:%M} · {descripcion} · humedad {h.humedad}%")
        for texto, nombre in (
            (f"{h.hora:%H} h", "horaPronostico"),
            (icono, "iconoPronostico"),
            (f"{h.temperatura:.0f}°" if h.temperatura is not None else "—", "tempPronostico"),
            (f"💨 {h.viento_kmh:.0f} {h.viento_cardinal}" if h.viento_kmh is not None else "", "detallePronostico"),
            (f"💧 {h.prob_precipitacion}%" if h.prob_precipitacion else "", "detallePronostico"),
        ):
            etiqueta = QLabel(texto, self)
            etiqueta.setObjectName(nombre)
            etiqueta.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(etiqueta)


class WeatherWidget(QFrame):
    """Tarjeta de clima para el dashboard. `consultar` y `archivo_cache` se
    pueden inyectar (tests); `automatico=False` no dispara consultas solas."""

    actualizado = Signal(object)  # ReporteClima

    # Los tests lo apagan (tests/conftest.py): nada de consultas reales a la red.
    AUTOMATICO_POR_DEFECTO = True

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        consultar: Optional[Callable[[], Dict[str, Any]]] = None,
        archivo_cache: Optional[Path] = None,
        automatico: Optional[bool] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("tarjetaClima")
        self._consultar = consultar or consultar_open_meteo
        self._archivo_cache = archivo_cache
        # Hilo y worker quedan referenciados acá hasta que el hilo termina:
        # sin esta referencia Python los recolecta antes de emitir `listo`.
        self._hilo: Optional[Tuple[QThread, ClimaWorker]] = None
        self.reporte: Optional[ReporteClima] = None
        self._obtenido_en: Optional[datetime] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        cabecera = QHBoxLayout()
        titulo = QLabel(f"🌤️  Clima · {LOCALIDAD}", self)
        titulo.setObjectName("tituloTarjeta")
        cabecera.addWidget(titulo)
        cabecera.addStretch(1)
        self.label_estado = QLabel("Consultando…", self)
        self.label_estado.setObjectName("estadoClima")
        cabecera.addWidget(self.label_estado)
        self.boton_actualizar = QPushButton("↻", self)
        self.boton_actualizar.setObjectName("botonQuitarFila")
        self.boton_actualizar.setToolTip("Actualizar ahora")
        self.boton_actualizar.setFixedWidth(32)
        self.boton_actualizar.clicked.connect(self.actualizar)
        cabecera.addWidget(self.boton_actualizar)
        layout.addLayout(cabecera)

        principal = QHBoxLayout()
        principal.setSpacing(14)
        self.label_icono = QLabel("🌡️", self)
        self.label_icono.setObjectName("iconoClima")
        principal.addWidget(self.label_icono)
        columna = QVBoxLayout()
        columna.setSpacing(0)
        self.label_temperatura = QLabel("—", self)
        self.label_temperatura.setObjectName("temperaturaClima")
        self.label_descripcion = QLabel("", self)
        self.label_descripcion.setObjectName("descripcionClima")
        columna.addWidget(self.label_temperatura)
        columna.addWidget(self.label_descripcion)
        principal.addLayout(columna)
        principal.addStretch(1)
        layout.addLayout(principal)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(2)
        self._valores: Dict[str, QLabel] = {}
        for i, (clave, titulo_dato) in enumerate((
            ("sensacion", "Sensación"), ("humedad", "Humedad"), ("viento", "Viento"), ("presion", "Presión"),
        )):
            etiqueta = QLabel(titulo_dato.upper(), self)
            etiqueta.setObjectName("etiquetaDatoClima")
            valor = QLabel("—", self)
            valor.setObjectName("valorDatoClima")
            grid.addWidget(etiqueta, 0, i)
            grid.addWidget(valor, 1, i)
            self._valores[clave] = valor
        layout.addLayout(grid)

        self.label_riesgo = QLabel("", self)
        self.label_riesgo.setObjectName("chip")
        self.label_riesgo.setWordWrap(True)
        self.label_riesgo.setVisible(False)
        layout.addWidget(self.label_riesgo)

        subtitulo = QLabel("PRÓXIMAS HORAS", self)
        subtitulo.setObjectName("etiquetaDatoClima")
        layout.addWidget(subtitulo)
        # 12 horas de un vistazo: 2 filas x 6, sin scroll.
        self._fila_pronostico = QWidget(self)
        self._layout_pronostico = QGridLayout(self._fila_pronostico)
        self._layout_pronostico.setContentsMargins(0, 0, 0, 0)
        self._layout_pronostico.setSpacing(6)
        layout.addWidget(self._fila_pronostico)

        self._cargar_cache()
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVALO_MS)
        self._timer.timeout.connect(self.actualizar)
        if self.AUTOMATICO_POR_DEFECTO if automatico is None else automatico:
            self._timer.start()
            QTimer.singleShot(0, self.actualizar)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.detener)

    def detener(self) -> None:
        """Al cerrar la app: no destruir un QThread que sigue esperando a la API."""
        self._timer.stop()
        if self._hilo is not None:
            hilo, _ = self._hilo
            try:
                hilo.quit()
                hilo.wait((2 * TIMEOUT_SEG + 2) * 1000)
            except RuntimeError:
                pass  # el hilo ya fue destruido

    # -- Consulta en segundo plano ------------------------------------------------

    def actualizar(self) -> None:
        if self._hilo is not None:
            return  # ya hay una consulta en curso
        self.label_estado.setText("Actualizando…")
        hilo = QThread()
        worker = ClimaWorker(self._consultar)
        worker.moveToThread(hilo)
        hilo.started.connect(worker.run)
        worker.listo.connect(self._on_datos)
        worker.fallo.connect(self._on_fallo)
        worker.terminado.connect(hilo.quit)
        worker.terminado.connect(worker.deleteLater)
        hilo.finished.connect(hilo.deleteLater)
        hilo.finished.connect(self._on_hilo_terminado)
        self._hilo = (hilo, worker)
        hilo.start()

    def _on_hilo_terminado(self) -> None:
        self._hilo = None

    def _on_datos(self, datos: Dict[str, Any]) -> None:
        self.label_estado.setToolTip("")
        try:
            reporte = parsear_open_meteo(datos)
        except (KeyError, TypeError, ValueError) as e:
            self._on_fallo(f"Respuesta inesperada de Open-Meteo ({e})")
            return
        self._obtenido_en = datetime.now()
        self._guardar_cache(datos)
        self.mostrar(reporte)
        theme.set_tono(self.label_estado, "neutro")
        self.label_estado.setText(f"Actualizado {self._obtenido_en:%H:%M}")

    def _on_fallo(self, mensaje: str) -> None:
        print(f"[Clima] No se pudo consultar Open-Meteo: {mensaje}")
        theme.set_tono(self.label_estado, "alerta")
        cuando = f" · datos de las {self._obtenido_en:%H:%M}" if self._obtenido_en and self.reporte else ""
        self.label_estado.setText(f"⚠ {TEXTO_SIN_CONEXION}{cuando}")
        self.label_estado.setToolTip(mensaje)
        if self.reporte is None:
            # Sin cache previo: que la tarjeta no quede en "—" sin explicación.
            self.label_icono.setText("📡")
            self.label_descripcion.setText(f"{TEXTO_SIN_CONEXION}. Se reintenta cada 30 min (o con ↻).")

    # -- Cache ------------------------------------------------------------------------

    def _ruta_cache(self) -> Path:
        return self._archivo_cache or ruta_cache()

    def _guardar_cache(self, datos: Dict[str, Any]) -> None:
        try:
            self._ruta_cache().write_text(
                json.dumps({"obtenido_en": self._obtenido_en.isoformat(), "datos": datos}), encoding="utf-8",
            )
        except OSError:
            pass  # el cache es una comodidad, no una necesidad

    def _cargar_cache(self) -> None:
        try:
            guardado = json.loads(self._ruta_cache().read_text(encoding="utf-8"))
            reporte = parsear_open_meteo(guardado["datos"])
            self._obtenido_en = datetime.fromisoformat(guardado["obtenido_en"])
        except (OSError, KeyError, TypeError, ValueError):
            return
        self.mostrar(reporte)
        self.label_estado.setText(f"Datos de las {self._obtenido_en:%H:%M}")

    # -- Presentación ---------------------------------------------------------------

    def mostrar(self, r: ReporteClima) -> None:
        self.reporte = r
        icono, descripcion = icono_y_descripcion(r.codigo, r.es_dia)
        self.label_icono.setText(icono)
        self.label_temperatura.setText(f"{r.temperatura:.1f} °C" if r.temperatura is not None else "—")
        self.label_descripcion.setText(descripcion)
        self._valores["sensacion"].setText(f"{r.sensacion:.1f} °C" if r.sensacion is not None else "—")
        self._valores["humedad"].setText(f"{r.humedad} %" if r.humedad is not None else "—")
        viento = "—"
        if r.viento_kmh is not None:
            viento = f"{r.viento_kmh:.0f} km/h del {r.viento_cardinal}"
            if r.rafagas_kmh:
                viento += f"\nráfagas {r.rafagas_kmh:.0f} km/h"
        self._valores["viento"].setText(viento)
        presion = "—"
        if r.presion_hpa is not None:
            flecha = ""
            if r.tendencia_presion is not None:
                flecha = " ↘" if r.tendencia_presion <= -1 else " ↗" if r.tendencia_presion >= 1 else " →"
            presion = f"{r.presion_hpa:.0f} hPa{flecha}"
        self._valores["presion"].setText(presion)
        if r.tendencia_presion is not None:
            self._valores["presion"].setToolTip(f"Tendencia a 3 h: {r.tendencia_presion:+.1f} hPa (nivel del mar)")

        riesgo = nivel_riesgo_30(r.temperatura, r.humedad, max(r.viento_kmh or 0, r.rafagas_kmh or 0))
        self.label_riesgo.setVisible(riesgo is not None)
        if riesgo:
            theme.set_tono(self.label_riesgo, "error" if riesgo == "extremo" else "alerta")
            self.label_riesgo.setText(
                "🔥 Regla 30-30-30 cumplida: riesgo EXTREMO de propagación de incendios" if riesgo == "extremo"
                else "🔥 Dos condiciones de la regla 30-30-30: riesgo ALTO de incendios"
            )

        while self._layout_pronostico.count():
            item = self._layout_pronostico.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, hora in enumerate(r.pronostico):
            self._layout_pronostico.addWidget(_TarjetaHora(hora, self._fila_pronostico), i // COLUMNAS_PRONOSTICO,
                                              i % COLUMNAS_PRONOSTICO)
        self.actualizado.emit(r)
