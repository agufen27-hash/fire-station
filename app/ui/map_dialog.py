"""
"📍 Marcar en Mapa": selector de punto estilo Avenza Maps para el formulario
de salida. Reutiliza el `MapWidget` de la Cartografía Táctica (imagen local
georreferenciada, sin internet -- funciona en la PC de guardia aislada):

  - Clic en el mapa: coloca / mueve el marcador de intervención.
  - Latitud / Longitud a mano (acepta "-33.6315, -64.0152" pegado en un solo
    campo y coma decimal): "Ir al punto" mueve el marcador y centra la vista.
  - Coordenadas del punto en decimal y en grados/minutos/segundos, con aviso
    si cae fuera del área de la imagen.
  - "✅ Confirmar y Guardar Imagen": PNG del área centrada en el marcador en
    data/mapas/parte_<NUMERO>_<AÑO>.png; el formulario recibe punto, ruta y
    lo que se haya dibujado (polígono del área afectada).

Centro inicial: el punto ya cargado en la salida o, si no hay, Adelia María.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.paths import get_writable_dir
from app.ui import theme
from app.ui.widgets.map_widget import MapWidget, formatear_dms

# Adelia María (plaza) -- centro del visor cuando la salida no tiene punto.
CENTRO_POR_DEFECTO: Tuple[float, float] = (-33.6315, -64.0152)

_RE_NUMERO = r"[-+]?\d{1,3}(?:[.,]\d+)?"
_RE_PAR = re.compile(rf"^\s*({_RE_NUMERO})\s*[,;\s]\s*({_RE_NUMERO})\s*$")


def parsear_coordenada(texto: str) -> Optional[float]:
    """"-33,6315" / "-33.6315" / " -33.6315° " -> float; None si no es un número."""
    limpio = (texto or "").strip().replace("°", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


def parsear_par(texto: str) -> Optional[Tuple[float, float]]:
    """"-33.6315, -64.0152" (o separado por espacio / punto y coma) -> (lat, lon)."""
    m = _RE_PAR.match((texto or "").replace("°", ""))
    if not m:
        return None
    lat, lon = parsear_coordenada(m.group(1)), parsear_coordenada(m.group(2))
    return (lat, lon) if lat is not None and lon is not None else None


def coordenadas_validas(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def ruta_imagen_parte(numero_parte: str) -> Path:
    """data/mapas/parte_<NUMERO>_<AÑO>.png ("012/2026" -> parte_012_2026.png)."""
    numero, _, anio = (numero_parte or "").strip().partition("/")
    numero = re.sub(r"[^\w-]+", "_", numero).strip("_") or "sin_numero"
    anio = re.sub(r"\D", "", anio) or str(date.today().year)
    return get_writable_dir("data") / "mapas" / f"parte_{numero}_{anio}.png"


class DialogoMarcarMapa(QDialog):
    """Modal. Al aceptar, `resultado` = {"latitud", "longitud", "ruta_imagen",
    "mapa": MapWidget.obtener_datos()}."""

    def __init__(
        self,
        numero_parte: str,
        latitud: Optional[float] = None,
        longitud: Optional[float] = None,
        superficie_ha: Optional[float] = None,
        geometria_geojson: Optional[str] = None,
        fuente_direccion: Optional[Callable[[], Tuple[str, str]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.numero_parte = numero_parte
        self.resultado: Optional[Dict] = None
        self.setWindowTitle(f"📍 Marcar en Mapa — Parte N° {numero_parte or '(nuevo)'}")
        self.setModal(True)
        self.resize(1100, 780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        ayuda = QLabel(
            "Hacé clic en el mapa para marcar el lugar de la intervención, o escribí las coordenadas y "
            "tocá <b>Ir al punto</b>. Rueda: zoom · arrastrar: mover. La imagen se genera con el zoom que "
            "tengas en pantalla, centrada en el marcador.", self)
        ayuda.setWordWrap(True)
        ayuda.setProperty("muted", True)
        layout.addWidget(ayuda)

        layout.addLayout(self._crear_fila_coordenadas())

        self.mapa = MapWidget(self, fuente_direccion=fuente_direccion)
        self.mapa.setMinimumHeight(460)
        layout.addWidget(self.mapa, 1)
        self.mapa.cargar_incidente(numero_parte, latitud, longitud, superficie_ha, geometria_geojson)
        self.mapa.coordenadas_cambiadas.connect(self._on_punto_marcado)

        self.label_punto = QLabel(self)
        self.label_punto.setObjectName("mensajeProgreso")
        self.label_punto.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label_punto)

        botones = QHBoxLayout()
        botones.addStretch(1)
        boton_cancelar = QPushButton("Cancelar", self)
        boton_cancelar.clicked.connect(self.reject)
        botones.addWidget(boton_cancelar)
        self.boton_confirmar = QPushButton("✅ Confirmar y Guardar Imagen", self)
        self.boton_confirmar.setObjectName("botonGuardar")
        self.boton_confirmar.setDefault(True)
        self.boton_confirmar.clicked.connect(self._confirmar)
        botones.addWidget(self.boton_confirmar)
        layout.addLayout(botones)

        self._centro_inicial = (latitud, longitud) if latitud is not None and longitud is not None \
            else CENTRO_POR_DEFECTO
        if latitud is not None and longitud is not None:
            self._mostrar_en_campos(latitud, longitud)
        self._actualizar_estado()

    def _crear_fila_coordenadas(self) -> QHBoxLayout:
        fila = QHBoxLayout()
        fila.setSpacing(8)
        self.entry_lat = QLineEdit(self)
        self.entry_lon = QLineEdit(self)
        for campo, texto, ejemplo in ((self.entry_lat, "Latitud", "-33.6315"), (self.entry_lon, "Longitud", "-64.0152")):
            fila.addWidget(QLabel(texto, self))
            campo.setPlaceholderText(f"Ej: {ejemplo}")
            campo.setMaximumWidth(170)
            campo.setToolTip("Grados decimales (punto o coma). También podés pegar \"lat, lon\" en Latitud.")
            campo.returnPressed.connect(self._ir_al_punto)
            fila.addWidget(campo)
        self.entry_lat.textEdited.connect(self._separar_par_pegado)
        boton_ir = QPushButton("🎯 Ir al punto", self)
        boton_ir.clicked.connect(self._ir_al_punto)
        fila.addWidget(boton_ir)
        boton_centro = QPushButton("⌖ Adelia María", self)
        boton_centro.setToolTip("Centrar la vista en Adelia María (no mueve el marcador)")
        boton_centro.clicked.connect(lambda: self.mapa.encuadrar(*CENTRO_POR_DEFECTO))
        fila.addWidget(boton_centro)
        fila.addStretch(1)
        return fila

    # -- Centro inicial ---------------------------------------------------------------

    def showEvent(self, evento) -> None:  # noqa: N802 - override de Qt
        super().showEvent(evento)
        # Después del ajuste inicial de la vista (que muestra la imagen entera):
        # zoom de barrio centrado en el punto de la salida o en Adelia María.
        QTimer.singleShot(120, lambda: self.mapa.encuadrar(*self._centro_inicial))

    # -- Coordenadas a mano -------------------------------------------------------------

    def _separar_par_pegado(self, texto: str) -> None:
        par = parsear_par(texto)
        if par is not None:
            self._mostrar_en_campos(*par)

    def _mostrar_en_campos(self, lat: float, lon: float) -> None:
        self.entry_lat.setText(f"{lat:.6f}")
        self.entry_lon.setText(f"{lon:.6f}")

    def _ir_al_punto(self) -> None:
        lat, lon = parsear_coordenada(self.entry_lat.text()), parsear_coordenada(self.entry_lon.text())
        if lat is None or lon is None:
            QMessageBox.warning(self, "Coordenadas incompletas",
                                "Escribí latitud y longitud en grados decimales (ej: -33.6315 y -64.0152).")
            return
        if not coordenadas_validas(lat, lon):
            QMessageBox.warning(self, "Coordenadas inválidas",
                                "La latitud va de -90 a 90 y la longitud de -180 a 180.")
            return
        self.mapa.fijar_punto(lat, lon, emitir=False)
        self.mapa.encuadrar(lat, lon)
        self._on_punto_marcado(lat, lon)

    def _on_punto_marcado(self, lat: float, lon: float) -> None:
        self._mostrar_en_campos(lat, lon)
        self._actualizar_estado()

    def _actualizar_estado(self) -> None:
        punto = self.mapa.punto()
        self.boton_confirmar.setEnabled(punto is not None)
        if punto is None:
            self.label_punto.setText("Sin punto marcado: hacé clic en el mapa o cargá las coordenadas.")
            theme.set_tono(self.label_punto, "alerta")
            return
        lat, lon = punto
        texto = f"📍 Punto: <b>{lat:.6f}, {lon:.6f}</b> &nbsp; ({formatear_dms(lat, lon)})"
        if not self.mapa.dentro_del_mapa(lat, lon):
            texto += " &nbsp; ⚠ Fuera del área de la imagen del mapa: la captura saldrá sin fondo."
            theme.set_tono(self.label_punto, "alerta")
        else:
            theme.set_tono(self.label_punto, "ok")
        self.label_punto.setText(texto)

    # -- Confirmar ----------------------------------------------------------------------

    def _confirmar(self) -> None:
        punto = self.mapa.punto()
        if punto is None:
            QMessageBox.warning(self, "Falta el punto", "Marcá el lugar de la intervención antes de confirmar.")
            return
        lat, lon = punto
        self.mapa.centrar_en(lat, lon)
        ruta = ruta_imagen_parte(self.numero_parte)
        try:
            self.mapa.exportar_imagen(ruta, leyenda=f"Parte N° {self.numero_parte}" if self.numero_parte else "")
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "No se pudo guardar la imagen", str(e))
            return
        self.resultado = {"latitud": lat, "longitud": lon, "ruta_imagen": str(ruta), "mapa": self.mapa.obtener_datos()}
        self.accept()
